"""Adapter side of the protocol: subclass Adapter, implement handle_* methods.

The base class owns the stdio loop, dispatch, event sequencing, the
handshake, and the idempotency store, so a broker adapter only writes broker
logic. Handlers run on the read loop thread; adapters that need timers (bar
clocks, bracket watchers) start their own threads and use emit(), which is
thread-safe.
"""
from __future__ import annotations

import sys
import threading
import time

from . import protocol


# Methods answerable BEFORE a broker session exists. `auth.*` starts the
# login that makes a session possible; `accounts.list` answers the question
# the user settles between logging in and opening the session (which account
# should this process be). Routing either through on_first_call would
# deadlock the flow it exists to serve.
PRE_SESSION = ("auth.", "accounts.")

# The mutating methods, idempotency-guarded in _dispatch (SPEC 2/8). Same set
# the platform-side Connector.MUTATING enforces; kept here so the adapter side
# is idempotent even against a platform that is not this SDK.
MUTATING = frozenset({
    "order.place", "order.modify", "order.amend", "order.cancel",
    "position.close", "position.close_by", "flatten_all",
})


class AdapterError(Exception):
    """Raise inside a handler to return a protocol error response.

    `like` names the closed-set code an `x_` extension should be TREATED as
    (SPEC 6), so a broker can report its own reason without giving up an
    actionable one: `AdapterError("x_halted", "volatility halt",
    retryable=True, like="market_closed")`.
    """

    def __init__(self, code: str, message: str, retryable: bool = False,
                 like: str | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.like = like


class Adapter:
    name = "unnamed"
    version = "0.0"
    broker = "unknown"
    mode = "paper"  # "paper" | "live"; must be truthful (SPEC 8.2)
    account_currency = "USD"
    account_model = "netting"  # "netting" | "hedging"
    # What the user must type before you can connect (SPEC 3.2): a list of
    # {"key", "label", "type"} with type "text" or "password", at most 8.
    # A platform draws its connect form from this and hands you each value at
    # spawn as BRUE_CRED_<KEY upper-cased> (SPEC 8.5). It names fields and
    # never carries a value; hello is answered cold, so it must be a constant
    # of your configuration, not something fetched from the broker. Leave it
    # empty when nothing needs typing.
    credential_form: list = []
    # What your broker's own terms let a platform do (SPEC 3.3), when they
    # restrict it: {"data_local_only": True} when its prices are licensed to
    # the account holder alone, {"orders_by_hand_only": True} when every
    # order on a live account must be a person's own action, with a one-line
    # "summary" and the terms' "url". A platform ACTS on both flags. Leave it
    # empty when your broker's terms say neither.
    terms: dict = {}

    def __init__(self, out=None):
        self._out = out or sys.stdout
        # Name-mangled on purpose: a broker that guards its own book with
        # self._lock (an obvious name) MUST NOT be able to shadow the lock
        # emit()/_send() take, or `with self._lock: self.emit(...)` would
        # self-deadlock silently. Mangling makes the collision impossible.
        self.__wire_lock = threading.Lock()
        self.__fill_lock = threading.Lock()
        self.__fill_n = 0
        self._seq = 0
        self._running = False
        # client_id -> stored result, so replays return the original outcome
        self._idempotent: dict[str, dict] = {}
        self._opened = False  # has on_first_call run? (see _dispatch)

    # == subclass surface ================================================
    def capabilities(self) -> dict:
        return {"orders": {"market": True, "brackets": True, "modify": True,
                           "pending": False, "trailing": False,
                           "partial_close": True},
                "data": {"quotes": False, "bars": False, "history": False},
                "flatten_all": True}

    def rate_limits(self) -> dict:
        return {"requests_per_second": 50}

    def broker_info(self) -> dict:
        """What the broker wants shown about itself (SPEC 3.1).

        Override and fill in your own details: a platform's connection picker
        lists you from THIS, so an adapter nobody has ever seen appears next
        to the pre-integrated brokers with its own name, not a filename. All
        fields optional; the platform sanitises and may show fewer.
        """
        return {}

    def on_first_call(self):
        """Open the broker session here, not in your startup path.

        Called at most once, immediately before the first method that actually
        needs the broker. `hello`, `ping`, `bye` and an unsupported method
        never trigger it.

        This exists so `hello` stays answerable COLD. A platform's connection
        picker handshakes an adapter just to learn what the broker calls
        itself (SPEC 3.1), and a picker that logs into every broker it lists
        in order to draw a menu is not something a broker should have to
        accept. Put the login, the account resolution and the market-data
        stream in here and a name costs the broker nothing.

        Raise AdapterError to fail the triggering request with a typed error.
        A failure does not latch: the next call tries again, so a broker that
        was briefly unreachable at startup is not permanently broken.
        """

    def on_shutdown(self):
        """Called on bye; flush state here."""

    # == idempotency helpers =============================================
    def idempotent_replay(self, client_id: str):
        return self._idempotent.get(client_id)

    def idempotent_store(self, client_id: str, result: dict,
                         signature=None) -> dict:
        stored = dict(result)
        if signature is not None:
            stored["_sig"] = signature
        self._idempotent[client_id] = stored
        return result

    def idempotent_check(self, client_id: str, signature):
        """Replay guard with the SPEC section 6 duplicate rule built in.

        Returns the stored result for a true replay, None for a fresh id,
        and raises `duplicate_client_id` when the same id arrives carrying a
        DIFFERENT request. Storing results alone is not enough: it makes a
        conflicting reuse look like a replay and quietly hands back a ticket
        for an order the caller did not ask for, which is exactly what the
        rule exists to prevent. `signature` is anything comparable built
        from the request fields that decide what gets dealt.
        """
        prior = self._idempotent.get(client_id)
        if prior is None:
            return None
        if prior.get("_sig") != signature:
            raise AdapterError("duplicate_client_id",
                               "client_id reused with different parameters")
        return {k: v for k, v in prior.items() if not k.startswith("_")}

    # == dedup ids =======================================================
    def next_fill_id(self) -> str:
        """A fresh, stable id for a fill/close event (SPEC 5 dedup).

        Use this ONLY if your broker gives each execution no id of its own AND
        your adapter emits each fill exactly once. If your stream is
        at-least-once (you replay fills after a reconnect), you MUST instead
        key the id on the broker's OWN execution id, so a replay of the same
        fill carries the same id and the platform can dedup it. A random id
        per emission would defeat the dedup it exists for.
        """
        with self.__fill_lock:
            self.__fill_n += 1
            return f"{self.name}-f{self.__fill_n}"

    # == wire ============================================================
    # Event names that carry a dedup id (SPEC 5), and the id field each uses.
    _DEDUP_ID = {"order.filled": "fill_id", "order.partial": "fill_id",
                 "position.closed": "close_id"}

    def emit(self, name: str, data: dict):
        # Stamp a dedup id if the adapter did not set one. Correct for the
        # common case (each fill emitted exactly once); an at-least-once
        # adapter that replays a fill MUST set its own stable id (from the
        # broker's execution id) so the replay keeps the same one, and then
        # this leaves it untouched.
        field = self._DEDUP_ID.get(name)
        if field and isinstance(data, dict) and not data.get(field):
            # Stamp IN PLACE, not into a copy: an at-least-once adapter that
            # re-emits the SAME event object must send the SAME id, or the
            # dedup this exists for is defeated. Mutating a fresh event dict
            # is harmless; a re-emit of the same object now keeps its id.
            data[field] = self.next_fill_id()
        with self.__wire_lock:
            self._seq += 1
            self._out.write(protocol.event(self._seq, name, data) + "\n")
            self._out.flush()

    def _send(self, line: str):
        with self.__wire_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def run(self, inp=None):
        inp = inp or sys.stdin
        self._running = True
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = protocol.parse_line(line)
            except protocol.ProtocolError as e:
                self._send(protocol.res_error("?", "internal", str(e)))
                continue
            if msg["type"] != "req":
                continue  # adapters only consume requests
            self._dispatch(msg["id"], msg["method"], msg.get("params") or {})
            if not self._running:
                break

    def now_ms(self) -> int:
        """The adapter's clock, in epoch milliseconds.

        Override when the broker or simulator does not run on wall time.
        Every timestamp the adapter emits, including the one ping answers,
        must come from here: an adapter whose ping disagrees with its own
        event timestamps makes the platform evaluate trading sessions against
        the wrong clock.
        """
        return int(time.time() * 1000)

    def _handler(self, method):
        """The handler of a method of this protocol, found by the method's
        exact name (SPEC 1, 4); None for any other name.

        A handler's name is derived from its method's, so a name is never
        normalised: order_place or flatten.all would otherwise reach
        handle_order_place or handle_flatten_all past every rule that knows
        the method by its name (idempotency here, the platform's arming and
        by-hand rules). An adapter that routes requests itself (on lanes, say)
        asks here too."""
        if not isinstance(method, str) or method not in protocol.METHODS:
            return None
        return getattr(self, "handle_" + method.replace(".", "_"), None)

    def _dispatch(self, rid: str, method: str, params: dict):
        if method == "hello":
            self._send(protocol.res(rid, self._hello(params)))
            return
        if method == "ping":
            # now_ms() is the adapter's own clock (see its docstring): the
            # paper simulator runs a synthetic one and stamps its events with
            # it, and ping must agree with those timestamps.
            self._send(protocol.res(rid, {"time": int(self.now_ms())}))
            return
        if method == "bye":
            self.on_shutdown()
            self._send(protocol.res(rid, {}))
            self._running = False
            return
        handler = self._handler(method)
        if handler is None:
            self._send(protocol.res_error(
                rid, "unsupported", f"method {method!r} not supported"))
            return
        # auth.* runs BEFORE the session can exist: an interactive broker
        # cannot open its session until the user has logged in, and the login
        # is started by these very methods. Routing them through
        # on_first_call would deadlock the flow (every auth.begin would first
        # demand the session it exists to make possible).
        if not self._opened and not method.startswith(PRE_SESSION):
            # First method that needs the broker: open the session now (see
            # on_first_call). Left unset on failure so a transient outage at
            # startup does not brick the adapter for its whole life.
            try:
                self.on_first_call()
                self._opened = True
            except AdapterError as e:
                self._send(protocol.res_error(rid, e.code, str(e), e.retryable,
                                          e.like))
                return
            except Exception as e:
                self._send(protocol.res_error(
                    rid, "internal",
                    f"opening the broker session failed: {type(e).__name__}: {e}"))
                return
        try:
            # Idempotency for EVERY mutating method, enforced here so no
            # adapter has to remember (SPEC 2/8). A request whose client_id was
            # already seen returns the ORIGINAL result and the handler never
            # runs again, so a close/flatten/order retried after a dropped
            # response cannot act twice. A reuse with DIFFERENT params raises
            # duplicate_client_id. Handlers may still call idempotent_replay
            # themselves; this makes it automatic for the ones that do not.
            cid = params.get("client_id") if isinstance(params, dict) else None
            if method in MUTATING and cid:
                prior = self.idempotent_check(
                    cid, protocol.order_signature(method, params))
                if prior is not None:
                    self._send(protocol.res(rid, prior))
                    return
                result = handler(params)
                result = result if result is not None else {}
                self.idempotent_store(
                    cid, result, protocol.order_signature(method, params))
                self._send(protocol.res(rid, result))
                return
            result = handler(params)
            self._send(protocol.res(rid, result if result is not None else {}))
        except AdapterError as e:
            self._send(protocol.res_error(rid, e.code, str(e), e.retryable,
                                          e.like))
        except Exception as e:  # never crash the loop on one bad request
            self._send(protocol.res_error(rid, "internal", f"{type(e).__name__}: {e}"))

    def _hello(self, params: dict) -> dict:
        out = {"protocol": protocol.PROTOCOL_VERSION,
               "adapter": {"name": self.name, "version": self.version},
               "broker": self.broker,
               "mode": self.mode,
               "account_currency": self.account_currency,
               "account_model": self.account_model,
               "capabilities": self.capabilities(),
               "rate_limits": self.rate_limits()}
        # Sanitised on the way out as well as on the way in, so an adapter
        # cannot put a 4 KB string or a javascript: URL on the wire even by
        # accident. Dropping a field silently would leave the broker looking
        # half configured in every platform's picker with no way to find out,
        # so anything lost is named on stderr: stdout is the protocol, stderr
        # is free, and this is the one moment the author can be told.
        # A broker's own information is often fetched from the broker (a
        # status endpoint, a config service), so it is the one part of the
        # handshake that can fail for reasons outside the adapter. Decoration
        # must never cost a connection: an exception here becomes an
        # unnamed-but-working broker, not a dead one.
        try:
            raw = self.broker_info()
        except Exception as e:  # noqa: BLE001 - any failure, same outcome
            print(f"[{self.name}] broker_info failed, connecting without it: "
                  f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            raw = None
        info = protocol.sanitize_broker_info(raw)
        if isinstance(raw, dict) and raw:
            lost = [k for k in raw if k not in info]
            cut = [k for k, v in info.items()
                   if isinstance(v, str) and v != str(raw.get(k)).strip()]
            if lost or cut:
                print(f"[{self.name}] broker_info: "
                      + (f"dropped {lost} " if lost else "")
                      + (f"shortened {cut} " if cut else "")
                      + "(see SPEC 3.1 for the caps and allowed URL schemes)",
                      file=sys.stderr, flush=True)
        if info:
            out["broker_info"] = info
        # The credential form (SPEC 3.2) follows the same two rules: sanitised
        # on the way out, and whatever was lost is named on stderr, because a
        # field dropped in silence is a key the user is never asked for.
        try:
            raw_form = self.credential_form
        except Exception as e:  # noqa: BLE001 - a property may do anything
            print(f"[{self.name}] credential_form failed, connecting without "
                  f"it: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            raw_form = None
        form = protocol.sanitize_credential_form(raw_form)
        if raw_form:
            asked = len(raw_form) if isinstance(raw_form, (list, tuple)) else 1
            if len(form) != asked:
                print(f"[{self.name}] credential_form: kept {len(form)} of "
                      f"{asked} fields "
                      f"({', '.join(f['key'] for f in form) or 'none'}); "
                      "see SPEC 3.2 for the key pattern, the two types and "
                      "the cap", file=sys.stderr, flush=True)
        if form:
            out["credential_form"] = form
        # The broker's terms (SPEC 3.3) are not decoration: a platform acts on
        # them, and each flag only takes something away. So, unlike the two
        # above, a failure here declares BOTH restrictions rather than none,
        # and the author is told why on stderr.
        try:
            raw_terms = self.terms
        except Exception as e:  # noqa: BLE001 - a property may do anything
            print(f"[{self.name}] terms failed, declaring both restrictions: "
                  f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            raw_terms = {flag: True for flag in protocol.TERMS_FLAGS}
        if raw_terms:
            terms = protocol.sanitize_terms(raw_terms)
            if isinstance(raw_terms, dict):
                odd = [k for k in protocol.TERMS_FLAGS if k in raw_terms
                       and not isinstance(raw_terms[k], bool)
                       and raw_terms[k] is not None]
                lost = [k for k in raw_terms if k not in terms]
            else:
                odd, lost = list(protocol.TERMS_FLAGS), []
            if odd or lost:
                print(f"[{self.name}] terms: "
                      + (f"read as true (not a plain true/false): {odd} "
                         if odd else "")
                      + (f"dropped {lost} " if lost else "")
                      + "(see SPEC 3.3)", file=sys.stderr, flush=True)
            out["terms"] = terms
        return out
