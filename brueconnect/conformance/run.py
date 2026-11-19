"""brue-connect conformance suite.

Runs a scripted set of scenarios against ANY adapter command and reports
PASS / SKIP / FAIL per scenario. An adapter is conformant when nothing
FAILs. SKIPs are printed with the reason (a capability the adapter does not
declare, or a hint the caller did not provide) so silent non-coverage is
impossible.

Usage:
  python -m brueconnect verify                    (certify a registered broker)
  python -m brueconnect.conformance.run --adapter "<adapter command>"
  optional: --partial-hint EURUSD:6      (a symbol:qty known to part-fill)
            --restartable                (adapter persists state; reconnect
                                          scenario kills and relaunches it)
            --auth-driver "cmd"          (interactive-auth adapters only: a
                                          command that stands in for the
                                          human + browser. It gets the
                                          auth.begin url as its last argv and
                                          must complete the broker's login
                                          page. Without it, an adapter that
                                          needs a login and has none stored
                                          cannot be certified and the suite
                                          says so instead of failing noise.)
"""
from __future__ import annotations

import argparse
import math
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brueconnect import (Connector, ConnectorError, Disconnected,  # noqa: E402
                         protocol)

# A quote this many ticks wide, at its widest in the sample, still counts as
# tight (see rank_by_movement).
TIGHT_SPREAD_TICKS = 5


def rank_by_movement(cands: list, quotes: list) -> list:
    """Symbols on which a minimum-distance stop is most likely to be
    touched, best first. `cands` are catalog instruments; `quotes` is the
    sample, in order: (symbol, bid, ask).

    Movement is measured against the spread that was OBSERVED, at its
    widest in the sample, not against the tick size. On a quote feed that
    shows one venue's book (a free feed) a thinly quoted share flickers
    between a tight and a very wide quote: its mid "moves" by dollars while
    the share itself trades in a cent, and a stop set a few ticks under its
    bid then rests untouched for the whole scenario (seen three times in a
    day: quotes of 29.78 / 34.71 on a share trading at 34.70). Such a quote
    moves at most half its own widest spread; an instrument that really
    travels moves many times its spread. And instruments whose spread stays
    within a few ticks come first, whatever the others score: a stop under
    a tight bid is a stop near the market.
    """
    lo: dict[str, float] = {}
    hi: dict[str, float] = {}
    wide: dict[str, float] = {}
    n: dict[str, int] = {}
    for sy, bid, ask in quotes:
        mid = (bid + ask) / 2.0
        lo[sy] = min(lo.get(sy, mid), mid)
        hi[sy] = max(hi.get(sy, mid), mid)
        wide[sy] = max(wide.get(sy, 0.0), ask - bid)
        n[sy] = n.get(sy, 0) + 1
    ranked = []
    for i in cands:
        sy = i["symbol"]
        # Five ticks in the sample window, not three: an instrument that
        # barely quotes will not travel to a stop either, and picking it
        # turns the scenario into a six-minute wait for nothing.
        if n.get(sy, 0) < 5:
            continue
        tick = i["tick_size"]
        dist = (float(i.get("stops_level_points") or 0) + 2) * tick + 0.0
        spread = max(wide[sy], tick)
        moved = hi[sy] - lo[sy]
        tight = spread <= TIGHT_SPREAD_TICKS * tick * (1 + 1e-9)
        ranked.append((tight, moved / max(dist + spread, 1e-12), sy))
    return [sy for _, _, sy in sorted(ranked, reverse=True)]


REQUIRED_SPEC_FIELDS = ["symbol", "asset_class", "quote_ccy", "digits",
                        "tick_size", "lot_step", "min_qty", "max_qty",
                        "contract_size", "margin_rate", "sessions",
                        "holidays", "tradable", "shortable"]
REQUIRED_ACCOUNT_FIELDS = ["balance", "equity", "margin_used", "margin_free",
                           "currency"]


def cid() -> str:
    return "conf-" + uuid.uuid4().hex[:12]


# A refusal that says "not this instrument, right now" rather than "your
# request is wrong". The suite moves to the next candidate on these instead of
# failing the broker, because a catalog of thousands legitimately contains
# instruments a given account cannot trade at this moment: a shut market, an
# unaffordable minimum size, a currency the broker cannot value today. A suite
# that demands the first symbol it fancies is testing luck, not conformance.
TRY_ANOTHER = {"market_closed", "trade_restricted", "insufficient_margin",
               "no_price", "invalid_qty", "requote", "frozen"}


class _Heard(Connector):
    """The suite's Connector: what the adapter writes to its stderr comes to
    the suite, which prints each line in the report as the adapter's own
    (Suite._adapter_said). Inherited, an adapter's forged "RESULT:" and
    "[PASS]" lines would come before the suite's own in a combined log.
    Every process it starts is drained, by a thread of its own (an
    undrained pipe would block a chatty adapter)."""

    def __init__(self, cmd: list[str], said, drains: list):
        super().__init__(cmd, stderr=subprocess.PIPE)
        self._said, self._drains = said, drains

    def start(self):
        super().start()
        pipe = self.proc.stderr if self.proc is not None else None
        if pipe is not None:
            t = threading.Thread(target=self._drain, args=(pipe,),
                                 daemon=True, name="adapter-stderr")
            t.start()
            self._drains.append(t)

    def _drain(self, pipe):
        try:
            for raw in iter(lambda: pipe.readline(4000), ""):
                self._said(raw)
        except (OSError, ValueError):
            pass                        # the pipe closed under the reader


class Suite:
    def __init__(self, cmd: list[str], partial_hint: str | None,
                 restartable: bool, live_ok: bool = False,
                 auth_driver: list[str] | None = None):
        self.cmd = cmd
        self.partial_hint = partial_hint
        self.restartable = restartable
        self.live_ok = live_ok
        self.auth_driver = auth_driver
        # The report's lines, one at a time (the adapter's stderr is printed
        # from another thread), and none of the adapter's after its RESULT.
        self._out = threading.Lock()
        self._ended = False
        self._drains: list = []
        self.c = self._connector()
        self.results: list[tuple[str, str, str]] = []
        self.tradable_symbol: str | None = None
        # Why no scenario after this point can run (see _stop), and where
        # the report said it.
        self.abort_reason: str | None = None
        self._said_at: str | None = None
        self._min_qty: dict[str, tuple[float, float]] = {}

    # == helpers =========================================================
    # The report's own markers, which no detail may carry: a reader finds
    # the results by them (tests/test_starter_adapter searches for RESULT).
    # RESULT is defused wherever it stands: with a word boundary before it,
    # "xRESULT:", "_RESULT:" and "0RESULT:" would pass, and a reader
    # searching the report would find the forged one first.
    _MARKERS = re.compile(r"\[\s*(pass|fail|skip|warn|info)\s*\]",
                          re.IGNORECASE)
    _RESULT = re.compile(r"result\s*:", re.IGNORECASE)

    @classmethod
    def _clean(cls, text, limit: int = 600) -> str:
        """`text` as the report may show it: one line, without control
        characters, cut at `limit`, and without the report's own markers.
        For every line that quotes an adapter's words (a record's detail, an
        [info] line): an error text, a symbol or a name with line breaks in
        it would add lines to the report that read as results."""
        text = protocol._clean_text(str(text)[:4000], limit)
        return cls._RESULT.sub("result -", cls._MARKERS.sub(r"(\1)", text))

    def _connector(self) -> Connector:
        """A connection to the adapter whose stderr comes to the report
        (_Heard, _adapter_said)."""
        return _Heard(self.cmd, self._adapter_said, self._drains)

    def _print(self, line: str):
        with self._out:
            print(line, flush=True)

    def _adapter_said(self, raw: str):
        """One line the adapter wrote to its stderr, in the report as the
        adapter's own: prefixed "  adapter: " and cleaned by record()'s rules
        (_clean), so it is one line and carries none of the report's
        markers. Nothing of it after the report's RESULT."""
        text = self._clean(raw)
        if not text:
            return
        with self._out:
            if not self._ended:
                print("  adapter: " + text, flush=True)

    def record(self, name: str, status: str, detail: str = ""):
        # One line each, whatever it quotes: see _clean.
        detail = self._clean(detail)
        self.results.append((name, status, detail))
        self._print(f"  [{status:4}] {name}"
                    + (f" :: {detail}" if detail else ""))

    def _info(self, text: str):
        """An [info] line of the report. Its text can quote the adapter (its
        mode, its terms, its name, its symbols), so it is cleaned as a
        record's detail is (_clean)."""
        self._print("  [info] " + self._clean(text))

    def _stop(self, reason: str, said_at: str | None = None) -> str:
        """No scenario after this point can run, for `reason`: the rest are
        skipped (run). The reason is said once, on one line: by the record
        named `said_at` when that record says it, else by the first
        scenario skipped for it; each later one points there."""
        self.abort_reason = (protocol._clean_text(str(reason)[:2000], 300)
                             or "the suite cannot go on")
        self._said_at = said_at
        return self.abort_reason

    def _no_session(self, e: BaseException) -> bool:
        """Whether a hello that failed with `e` leaves no session to
        certify: the connection was ended (a hello of another protocol, or
        not an object, SPEC 1), the adapter is gone or could not start, the
        hello timed out, or the adapter refused it for good. A refusal the
        adapter says may pass (retryable) on a connection that is still open
        does not: the next hello may be answered."""
        return not (isinstance(e, ConnectorError) and e.retryable
                    and self.c.alive)

    NO_HANDSHAKE = ("no handshake was read, so there is no session to "
                    "certify (the error is above)")

    @staticmethod
    def _caps_of(h) -> dict:
        """A handshake's capabilities: {} when it lacks them, or sends null,
        a list or anything but an object for them (s00 and s01 read a list
        as an object, and raised AttributeError)."""
        caps = h.get("capabilities") if isinstance(h, dict) else None
        return caps if isinstance(caps, dict) else {}

    def _cap(self, part: str) -> dict:
        """One part of the handshake's capabilities ("data", "orders"...):
        {} when the handshake lacks it, or sends null or anything but an
        object for it, so a lacking part reads as lacking (never a KeyError
        or an AttributeError)."""
        got = self._caps_of(self.c.handshake).get(part)
        return got if isinstance(got, dict) else {}

    def quote_for(self, symbol: str, timeout=10.0) -> dict:
        """The CURRENT quote, not the first one ever seen.

        wait_event scans history from `since` before it waits, so without a
        fresh mark this returns the oldest quote still in the window. Every
        caller here is pricing something against the live market, and a
        scenario that trails a stop against a quote frozen at session start
        never moves it at all.
        """
        mark = self.c.mark()
        self.c.request("quotes.subscribe", {"symbols": [symbol]})
        return self.c.wait_event("quote", lambda d: d["symbol"] == symbol,
                                 timeout=timeout, since=mark)

    def min_order_qty(self, sym: str) -> float:
        """The smallest order `sym` takes right now.

        `min_qty` alone is not it on a venue that gates on VALUE: a crypto
        broker publishes a `min_qty` worth about a dollar beside a
        `min_notional` of ten, and refuses the first, correctly. So where
        the catalog declares a `min_notional` and a quote can be had, the
        size is the larger of `min_qty` and that notional at the ask,
        rounded UP to `lot_step`, with 5% headroom so a tick against us
        between the quote and the order does not undo it. An instrument
        with no `min_notional` gets exactly `min_qty`, as before.
        """
        spec = self.c.catalog[sym]
        qty = spec["min_qty"]
        floor = float(spec.get("min_notional") or 0)
        if floor <= 0 or not self._cap("data").get("quotes"):
            return qty
        cached = self._min_qty.get(sym)
        if cached and time.time() - cached[1] < 30:
            return cached[0]
        try:
            ask = float(self.quote_for(sym, timeout=5.0).get("ask") or 0)
        except (TimeoutError, ConnectorError):
            return qty
        if ask <= 0:
            return qty
        step = spec["lot_step"]
        need = floor * 1.05 / (ask * float(spec.get("contract_size") or 1))
        qty = max(qty, math.ceil(need / step - 1e-9) * step)
        self._min_qty[sym] = (qty, time.time())
        return qty

    def pick_tradable(self) -> str | None:
        """First instrument that is tradable and accepts an order now.

        Records why each instrument refused. A venue outside its session
        refuses every symbol with market_closed, which is not an adapter
        defect; an adapter that rejects everything for other reasons is.
        Keeping the codes lets the caller tell those apart instead of
        reporting a bare failure.
        """
        self.refusals = {}
        for sym, spec in self.c.catalog.items():
            if not spec.get("tradable"):
                self.refusals[sym] = "not tradable"
                continue
            test = cid()
            try:
                self.c.request("order.place", {
                    "client_id": test, "symbol": sym, "side": "buy",
                    "qty": self.min_order_qty(sym)})
            except ConnectorError as e:
                # Branch on effective_code, not code: a broker may refuse with
                # its OWN code (SPEC 6 `x_`) while nominating the closed-set
                # code it should be treated as. Keying off `code` alone made a
                # broker's honest vocabulary look like a hard fault and
                # aborted the run on a refusal that only meant "not this
                # symbol, right now". The broker's own code is what gets
                # recorded, because that is what a human should read.
                if e.effective_code in TRY_ANOTHER:
                    self.refusals[sym] = e.code
                    continue
                raise
            self.c.wait_event("order.filled",
                              lambda d: d["client_id"] == test, timeout=30)
            self.c.request("flatten_all")
            return sym
        return None

    def _settle(self, sym: str, want: bool, tries: int = 20) -> bool:
        """Wait until positions.list agrees a position in `sym` does (want=True)
        or does not (want=False) exist. Async venues settle a fill a beat after
        the event, so a bare read races them."""
        for _ in range(tries):
            here = any(p["symbol"] == sym
                       for p in self.c.request("positions.list")["positions"])
            if here == want:
                return True
            time.sleep(0.25)
        return False

    def _needs_fill(self, label: str) -> bool:
        """True when this scenario cannot run because s03 found nothing
        tradable. Reports SKIP rather than crashing on catalog[None], which
        is what a closed venue would otherwise produce."""
        if self.tradable_symbol:
            return False
        self.record(label, "SKIP",
                    "no tradable instrument this run (see the happy-path "
                    "scenario); this says nothing about the adapter")
        return True

    # == scenarios =======================================================
    def s00_interactive_auth(self):
        """SPEC 1.6: an adapter that authenticates a HUMAN must say so, must
        refuse session calls with `auth_required` (not hang, not lie), must
        hand out a browsable url with no secrets in it, and must emit
        `auth.completed` once the login lands. The human + browser are
        played by --auth-driver, which is how a real broker integration
        gets certified in CI too: nobody sits clicking a login page on
        every run."""
        try:
            h = self.c.hello()
        except (ConnectorError, Disconnected, OSError) as e:
            # No handshake was read. When none can be (see _no_session: a
            # hello of another protocol or not an object, which ended the
            # connection, a refusal for good, an adapter gone or one that
            # could not start, a hello that timed out) every later scenario
            # would fail for this one reason, or read a handshake that is
            # not there, so the rest is skipped. A refusal the adapter says
            # may pass is asked again by s01. Either way this scenario FAILs
            # with the error (run).
            if self._no_session(e):
                self._stop(self.NO_HANDSHAKE)
            raise
        auth = self._caps_of(h).get("auth")
        if not (isinstance(auth, dict) and auth.get("interactive")):
            self.record("interactive auth", "SKIP",
                        "capability auth not declared (machine credentials)")
            return
        st = self.c.request("auth.status", {})
        if st.get("authenticated"):
            self.record("interactive auth", "PASS",
                        "a stored login is still accepted; nothing to drive")
            return
        try:
            self.c.request("account.get", {})
            self.record("unauthenticated session call refused", "FAIL",
                        "account.get succeeded with no login at all")
        except ConnectorError as e:
            self.record("unauthenticated session call refused",
                        "PASS" if e.code == "auth_required" else "FAIL",
                        f"code={e.code}")
        b = self.c.request("auth.begin", {})
        url = str(b.get("url") or "")
        secretish = [k for k in b if any(w in k.lower() for w in
                     ("verifier", "secret", "token", "password"))]
        self.record("auth.begin returns a browsable url and no secrets",
                    "PASS" if url.startswith(("http://", "https://"))
                    and not secretish else "FAIL",
                    (f"leaked {secretish} " if secretish else "") + url[:90])
        if not self.auth_driver:
            self.record("interactive login completes", "SKIP", self._stop(
                "adapter needs an interactive login and no --auth-driver was "
                "given; open the url above as a human and re-run",
                said_at="interactive login completes"))
            return
        mark = self.c.mark()
        drv = subprocess.run(self.auth_driver + [url], capture_output=True,
                             text=True, timeout=60)
        if drv.returncode != 0:
            self.record("interactive login completes", "FAIL",
                        f"auth driver exited {drv.returncode}: "
                        f"{(drv.stderr or drv.stdout)[-160:]}")
            self._stop("login never completed")
            return
        try:
            done = self.c.wait_event("auth.completed", timeout=30, since=mark)
        except TimeoutError:
            self.record("auth.completed event, ok=true", "FAIL",
                        "driver finished but no auth.completed within 30s")
            self._stop("login never completed")
            return
        ok = bool(done and done.get("ok"))
        self.record("auth.completed event, ok=true",
                    "PASS" if ok else "FAIL", str(done)[:120])
        st = self.c.request("auth.status", {})
        self.record("auth.status flips to authenticated",
                    "PASS" if st.get("authenticated") else "FAIL",
                    st.get("detail", "")[:120])
        if not (ok and st.get("authenticated")):
            self._stop("login did not stick; no session to certify")

    def s01_handshake(self):
        try:
            h = self.c.connect()
        except (ConnectorError, Disconnected, OSError):
            # s00's hello was refused with a "may pass", and this one has
            # not passed either: no handshake, so nothing after can run.
            # (A hello that passed and a read of reconcile that failed is
            # this scenario's FAIL alone.)
            if not self.c.handshake:
                self._stop(self.NO_HANDSHAKE)
            raise
        # `adapter` is checked because platforms read it: the CLI prints it,
        # and an adapter can pass every trading scenario while being
        # unidentifiable in a connection list. Better found here than by a
        # user.
        ad = h.get("adapter") or {}
        # The number 1 (1.0 is it too); JSON's true is not a number, though
        # Python reads True == 1.
        ok = (h.get("protocol") == 1
              and not isinstance(h.get("protocol"), bool)
              and h.get("mode") in ("paper", "live")
              and h.get("account_model") in ("netting", "hedging")
              and isinstance(ad.get("name"), str) and ad.get("name")
              and isinstance(ad.get("version"), str) and ad.get("version")
              and self._caps_of(h).get("flatten_all") is True)
        self.record("handshake sane + flatten_all declared",
                    "PASS" if ok else "FAIL",
                    f"mode={h.get('mode')} adapter={ad.get('name')!r}")
        # The suite places real orders. Against anything that is not paper it
        # would be spending real money, so it stops unless the caller said so
        # in as many words. Arming here rather than checking mode by hand: the
        # Connector owns the rule, this is a caller of it.
        if not self.c.is_paper:
            if not self.live_ok:
                self.record("refusing to certify a non-paper account", "FAIL",
                            f"mode={h.get('mode')!r}; re-run with --live if "
                            "you really mean to place orders on it")
                raise SystemExit(1)
            # The suite's orders are nobody's own action, so a broker whose
            # terms keep live orders by hand (SPEC 3.3) is certified on its
            # demo, never on a live account, --live or not.
            if self.c.terms.get("orders_by_hand_only"):
                self.record("refusing to place orders on a live account "
                            "whose broker's terms keep them by hand", "FAIL",
                            self.c.by_hand_refusal() + " Certify it on the "
                            "broker's demo account.")
                raise SystemExit(1)
            self.c.arm("conformance suite, --live given")
            self._info(f"ARMED: mode={h.get('mode')!r}, {self.c.armed_for}")
        self._check_broker_info(h)
        self._check_terms(h)

    def _check_terms(self, h: dict):
        """terms is optional, but a platform ACTS on it (SPEC 3.3), so what
        it will act on is printed, and a flag it can only read by failing
        closed is a warning: the broker is held to more than it meant.
        Neither is a FAIL: a restriction declared is never unsafe."""
        if "terms" not in h:
            return
        raw = h.get("terms")
        clean = protocol.sanitize_terms(raw)
        if raw is not None and not isinstance(raw, dict):
            self.record("terms is an object", "WARN",
                        f"got {type(raw).__name__}; both restrictions apply")
        elif isinstance(raw, dict):
            odd = [k for k in protocol.TERMS_FLAGS if k in raw
                   and raw[k] is not None and not isinstance(raw[k], bool)]
            if odd:
                self.record("terms flags are true or false", "WARN",
                            f"read as true by failing closed: {odd}")
        on = [k for k in protocol.TERMS_FLAGS if clean.get(k)]
        self._info(f"terms -> {', '.join(on) or 'no restriction'}"
                   + (f"; {clean['summary']!r}" if clean.get("summary")
                      else ""))

    def _check_broker_info(self, h: dict):
        """broker_info is optional, but a broker that sends it must survive
        the platform's sanitiser intact (SPEC 3.1).

        Folded into the handshake scenario rather than given its own, because
        failing certification over a tagline would be absurd. What it catches
        is the setup mistake nobody notices: a field over the cap or on the
        wrong URL scheme is DROPPED silently, so the broker looks half
        configured in every platform's picker and has no way to find out.

        An adapter built on the SDK cleans its own broker_info before it
        reaches the wire and names what it lost on stderr, so this can only
        fire for a hand-written adapter, in any language, that puts raw
        fields on the wire. Those are exactly the ones with nobody to tell
        them.
        """
        raw = h.get("broker_info")
        if not raw:
            return  # nothing claimed, nothing to lose
        if not isinstance(raw, dict):
            self.record("broker_info is an object", "WARN",
                        f"got {type(raw).__name__}")
            return
        clean = protocol.sanitize_broker_info(raw)
        lost = [k for k in raw if k not in clean]
        # Compared against the SAME cleaning with no length cap, so only real
        # truncation is reported. Comparing against the raw string flagged
        # every field that merely had two spaces in it, and a warning that
        # cries wolf is worse than no warning.
        uncapped = {k: protocol._clean_text(raw.get(k), 10 ** 6)
                    for k in protocol.BROKER_INFO_TEXT}
        trimmed = [k for k, v in clean.items()
                   if k in uncapped and v != uncapped[k]]
        if lost or trimmed:
            detail = f"shown as {clean.get('display_name')!r}"
            detail += (f"; dropped={lost}" if lost else "")
            detail += (f"; trimmed={trimmed}" if trimmed else "")
            self.record("broker_info displays as sent", "WARN", detail)
        else:
            # Not recorded as a pass: certification is about trading, and a
            # broker should never be told its tagline failed an exam. Printed
            # so a clean run still shows what a picker will display.
            self._info(f"broker_info -> {clean.get('display_name')!r}, "
                       f"{len(clean)} fields")

    def s02_reconcile(self):
        missing = []
        if not self.c.catalog:
            missing.append("catalog empty")
        for sym, spec in self.c.catalog.items():
            for f in REQUIRED_SPEC_FIELDS:
                if f not in spec:
                    missing.append(f"{sym} lacks {f}")
        for f in REQUIRED_ACCOUNT_FIELDS:
            if f not in self.c.account:
                missing.append(f"account lacks {f}")
        self.record("reconciliation snapshots complete",
                    "FAIL" if missing else "PASS", "; ".join(missing[:4]))

    def s03_happy_path(self):
        self.tradable_symbol = self.pick_tradable()
        if not self.tradable_symbol:
            codes = set(getattr(self, "refusals", {}).values())
            closed_only = codes and codes <= {"market_closed", "not tradable"}
            # A shut venue cannot be traded by anyone; that is the calendar,
            # not the adapter. Anything else refusing every symbol is a fault.
            self.record("happy path place/fill/close",
                        "SKIP" if closed_only else "FAIL",
                        ("every instrument is outside its trading session "
                         "(%d symbols, all market_closed); trading scenarios "
                         "cannot run until the venue opens" % len(self.refusals))
                        if closed_only else
                        ("no instrument accepts an order right now: %s"
                         % sorted(codes)))
            return
        sym = self.tradable_symbol
        spec = self.c.catalog[sym]
        k = cid()
        mark = self.c.mark()
        self.c.request("order.place", {"client_id": k, "symbol": sym,
                                       "side": "buy",
                                       "qty": self.min_order_qty(sym)})
        fill = self.c.wait_event("order.filled",
                                 lambda d: d["client_id"] == k, timeout=30)
        pos = self.c.request("positions.list")["positions"]
        found = [p for p in pos if p["symbol"] == sym]
        self.c.request("position.close",
                       {"position_id": fill["position_id"]})
        closed_evt = self.c.wait_event(
            "position.closed",
            lambda d: d["position_id"] == fill["position_id"],
            timeout=30, since=mark)
        after = self.c.request("positions.list")["positions"]
        ok = bool(found) and fill["qty"] > 0 and not [
            p for p in after if p["position_id"] == fill["position_id"]]
        self.record("happy path place/fill/close", "PASS" if ok else "FAIL",
                    f"{sym} qty={fill['qty']} @ {fill['price']}")

        # SPEC lists swap and commission on position.closed. An adapter that
        # omits them passes every trading scenario while leaving a platform
        # unable to split carry from fees, so the fields are checked here.
        d = closed_evt or {}
        missing = [f for f in ("swap", "commission") if f not in d]
        nonnum = [f for f in ("swap", "commission")
                  if f in d and not isinstance(d[f], (int, float))]
        self.record("position.closed carries the carry/fee split",
                    "PASS" if not missing and not nonnum else "FAIL",
                    ("missing %s" % missing if missing else
                     "non numeric %s" % nonnum if nonnum else
                     "swap=%s commission=%s" % (d["swap"], d["commission"])))

        # Dedup ids (SPEC 5): the at-least-once trade/close stream must carry a
        # stable id or a platform cannot tell a re-emission from a new event
        # and double-counts money.
        self.record("fill and close events carry dedup ids",
                    "PASS" if fill.get("fill_id") and d.get("close_id")
                    else "FAIL",
                    "fill_id=%s close_id=%s" % (fill.get("fill_id"),
                                                d.get("close_id")))

    def s04_idempotent_replay(self):
        if self._needs_fill("idempotent replay: same result, one fill"):
            return
        sym = self.tradable_symbol
        spec = self.c.catalog[sym]
        k = cid()
        p = {"client_id": k, "symbol": sym, "side": "buy",
             "qty": self.min_order_qty(sym)}
        r1 = self.c.request("order.place", p)
        self.c.wait_event("order.filled", lambda d: d["client_id"] == k,
                          timeout=30)
        r2 = self.c.request("order.place", p)
        fills = [d for (n, d) in list(self.c.events)
                 if n == "order.filled" and d["client_id"] == k]
        ok = r1 == r2 and len(fills) == 1
        self.record("idempotent replay: same result, one fill",
                    "PASS" if ok else "FAIL",
                    f"results_equal={r1 == r2} fills={len(fills)}")

        # Idempotency is required on EVERY mutating method (SPEC 2/8), not just
        # order.place. A close retried after a dropped response must not close
        # twice; a flatten_all retried must not wipe a position opened since.
        # Checking order.place alone leaves those paths uncovered.
        pos = [p for p in self.c.request("positions.list")["positions"]
               if p["symbol"] == sym]
        if pos:
            fk = cid()
            self.c.request("flatten_all", {"client_id": fk})     # closes it
            # The user opens a FRESH position, THEN the first flatten's
            # response is retried. A non-idempotent adapter re-runs the
            # flatten and wipes the position that did not exist when the
            # flatten was first sent: the exact dropped-response hazard.
            nk = cid()
            self.c.request("order.place", {"client_id": nk, "symbol": sym,
                                           "side": "buy",
                                           "qty": self.min_order_qty(sym)})
            self.c.wait_event("order.filled",
                              lambda d: d.get("client_id") == nk, timeout=30)
            # confirm the fresh position is actually established (async venues
            # settle it a beat after the fill), so a false empty is not read
            # as the bug.
            here = self._settle(sym, want=True)
            self.c.request("flatten_all", {"client_id": fk})     # REPLAY
            still = [p for p in self.c.request("positions.list")["positions"]
                     if p["symbol"] == sym]
            self.record("flatten_all is idempotent on client_id",
                        "PASS" if (here and still) else "FAIL",
                        "held" if still else
                        ("fresh position never settled; inconclusive"
                         if not here else
                         "replayed flatten_all closed a position opened after it"))
        self.c.request("flatten_all", {"client_id": cid()})

    def s05_duplicate_conflict(self):
        if self._needs_fill("client_id reuse with different params rejected"):
            return
        sym = self.tradable_symbol
        spec = self.c.catalog[sym]
        k = cid()
        qty = self.min_order_qty(sym)
        self.c.request("order.place", {"client_id": k, "symbol": sym,
                                       "side": "buy", "qty": qty})
        self.c.wait_event("order.filled", lambda d: d["client_id"] == k,
                          timeout=30)
        try:
            self.c.request("order.place", {"client_id": k, "symbol": sym,
                                           "side": "sell", "qty": qty})
            status, detail = "FAIL", "conflicting reuse accepted"
        except ConnectorError as e:
            ok = e.code == "duplicate_client_id"
            status = "PASS" if ok else "FAIL"
            detail = f"code={e.code}"
        self.c.request("flatten_all")
        self.record("client_id reuse with different params rejected",
                    status, detail)

    def s06_reject_paths(self):
        if self._needs_fill("reject paths typed correctly"):
            return
        try:
            self.broker_ms = float(self.c.request("ping").get("time") or 0) or None
        except Exception:
            self.broker_ms = None
        sym = self.tradable_symbol
        spec = self.c.catalog[sym]
        checks = []
        try:
            self.c.request("order.place", {"client_id": cid(),
                                           "symbol": "NO_SUCH_SYMBOL_XYZ",
                                           "side": "buy", "qty": 1})
            checks.append("unknown symbol accepted")
        except ConnectorError as e:
            if e.code != "unknown_symbol":
                checks.append(f"unknown symbol -> {e.code}")
        bad_qty = self.min_order_qty(sym) + spec["lot_step"] / 3.0
        try:
            self.c.request("order.place", {"client_id": cid(), "symbol": sym,
                                           "side": "buy", "qty": bad_qty})
            checks.append("off-step qty accepted")
        except ConnectorError as e:
            if e.code != "invalid_qty":
                checks.append(f"off-step qty -> {e.code}")
        # An instrument the broker itself says is shut right now. An EMPTY
        # sessions list is NOT that (SPEC 3: it means no calendar was
        # published), and reading it as "closed" made this check demand a
        # market_closed rejection from every broker that publishes no
        # calendar.
        closed = [s for s, sp in self.c.catalog.items()
                  if sp.get("sessions")
                  and self._shut_now(sp, getattr(self, "broker_ms", None))]
        if closed:
            try:
                self.c.request("order.place", {"client_id": cid(),
                                               "symbol": closed[0],
                                               "side": "buy",
                                               "qty": self.min_order_qty(closed[0])})
                checks.append("closed market accepted")
            except ConnectorError as e:
                if e.code != "market_closed":
                    checks.append(f"closed market -> {e.code}")
        else:
            # Not silently skipped: a check that did not run must say so, or a
            # broker reads a pass as coverage it did not get.
            self._info("no instrument is outside its published sessions "
                       "right now; market_closed not exercised")
        self.record("reject paths typed correctly",
                    "FAIL" if checks else "PASS", "; ".join(checks))

    @staticmethod
    def _shut_now(spec: dict, broker_ms: float | None = None) -> bool:
        """Is the BROKER outside every session this instrument publishes?

        Evaluated on the broker's clock, not the runner's. Sessions are the
        broker's calendar, and an adapter may legitimately run on a simulated
        or offset clock; judging its calendar by the runner's wall clock gives
        a false 'closed market accepted' failure whenever the two disagree,
        which for a weekday-only instrument means every weekend.

        Weekday and clock only, and only where the session's tz is UTC (the
        only zone the suite can evaluate without a tz database); a session in
        another zone is treated as unknown rather than guessed at.
        """
        now = time.gmtime(broker_ms / 1000.0 if broker_ms else None)
        dow, mins = now.tm_wday + 1, now.tm_hour * 60 + now.tm_min
        for sess in spec.get("sessions") or []:
            if (sess.get("tz") or "UTC").upper() != "UTC":
                return False
            if dow not in (sess.get("days") or []):
                continue
            try:
                oh, om = [int(x) for x in str(sess["open"]).split(":")]
                ch, cm = [int(x) for x in str(sess["close"]).split(":")]
            except (KeyError, ValueError):
                return False
            if oh * 60 + om <= mins < (ch * 60 + cm):
                return False
        return True

    def s07_insufficient_margin(self):
        if self._needs_fill("insufficient margin rejected"):
            return
        if not self._cap("data").get("quotes"):
            self.record("insufficient margin rejected", "SKIP",
                        "adapter has no quotes capability to price the test")
            return
        sym = self.tradable_symbol
        spec = self.c.catalog[sym]
        q = self.quote_for(sym)
        equity = self.c.request("account.get")["equity"]
        need_qty = (equity * 20) / (spec["contract_size"] * q["ask"]
                                    * spec["margin_rate"])
        step = spec["lot_step"]
        qty = min(spec["max_qty"], round(need_qty / step) * step)
        # A max_qty cap can make EVERY order affordable on this instrument; an
        # accepted order then proves nothing about the margin check, so the
        # honest verdict is SKIP, not FAIL (mirrors the suite's SKIP doctrine).
        required = qty * spec["contract_size"] * q["ask"] * spec["margin_rate"]
        if required <= equity:
            self.record("insufficient margin rejected", "SKIP",
                        f"max_qty caps {sym} margin at {required:.0f} <= equity "
                        f"{equity:.0f}; cannot construct an unaffordable order")
            return
        try:
            self.c.request("order.place", {"client_id": cid(), "symbol": sym,
                                           "side": "buy", "qty": qty})
            self.c.request("flatten_all")
            self.record("insufficient margin rejected", "FAIL",
                        f"qty={qty} accepted")
        except ConnectorError as e:
            self.record("insufficient margin rejected",
                        "PASS" if e.code == "insufficient_margin" else "FAIL",
                        f"code={e.code}")

    def _most_mobile_symbol(self, sample_sec: float = 12.0):
        """Tradable symbols, most movement per stop-distance first."""
        # Spread the sample ACROSS the catalog instead of taking its
        # alphabetical head. On a five-instrument simulator the two are the
        # same; on a catalog of thousands the head can be a run of thinly
        # traded instruments, and the scenario would then pick the least
        # liquid names the broker deals and wait minutes for one to move.
        pool = [i for i in self.c.catalog.values()
                if i.get("tradable") and i.get("trade_mode", "full") == "full"
                and i.get("shortable", True)]
        want = 24
        stride = max(1, len(pool) // want)
        cands = pool[::stride][:want] or pool[:want]
        if not cands:
            return []
        syms = [i["symbol"] for i in cands]
        mark = self.c.mark()
        try:
            self.c.request("quotes.subscribe", {"symbols": syms})
        except ConnectorError:
            return []
        time.sleep(sample_sec)
        with self.c._event_cv:
            events = list(self.c.events)
        quotes = [(d["symbol"], d["bid"], d["ask"]) for name, d in events
                  if name == "quote" and d["symbol"] in self.c.catalog]
        try:
            self.c.request("quotes.unsubscribe", {"symbols": syms})
        except ConnectorError:
            pass
        # Ranked, not reduced to a single winner: the best mover may be one the
        # broker declines to deal right now, and the scenario should walk down
        # the list rather than report the broker broken.
        return rank_by_movement(cands, quotes)

    def s08_brackets(self):
        # Read as the CLI's report reads it: a handshake that lacks a part is
        # reported as lacking it, never as a KeyError.
        if not self._cap("orders").get("brackets"):
            self.record("bracket stop fires", "SKIP", "no brackets capability")
            return
        if not self._cap("data").get("quotes"):
            self.record("bracket stop fires", "SKIP", "no quotes to set a stop")
            return
        # Pick the instrument where a minimum-distance stop is most likely
        # to be touched: measure each candidate's realised movement for a few
        # seconds and take the best mover RELATIVE TO its own stop distance.
        # Hardcoding the first tradable symbol makes this scenario a coin
        # flip on brokers whose flagship pair is quiet against its stop
        # level, and a conformance suite must converge on ANY broker.
        # Walk the ranking until the broker actually deals one. On a catalog of
        # thousands the best mover can easily be an instrument this account
        # cannot trade this minute, and that is the broker being correct.
        declined = {}
        sym = spec = q = fill = None
        k, mark, sl, step = None, None, None, None
        for cand in (self._most_mobile_symbol()
                     + ([self.tradable_symbol] if self.tradable_symbol else [])):
            spec = self.c.catalog[cand]
            q = self.quote_for(cand)
            # respect a declared stops_level_points (SPEC 1.1): the SL must sit
            # beyond the broker's minimum distance from the EXIT side (the bid,
            # for a long), or a conformant broker rightly rejects it
            stops = float(spec.get("stops_level_points") or 0)
            # Sit as close as the broker allows: the trail only
            # advances on a new extreme, so a wider step needs a
            # deeper retrace and turns the scenario back into a
            # coin flip on a quiet instrument.
            step = (stops + 1) * spec["tick_size"]
            sl = round(q["bid"] - step, spec["digits"])
            k = cid()
            mark = self.c.mark()
            try:
                self.c.request("order.place", {
                    "client_id": k, "symbol": cand, "side": "buy",
                    "qty": self.min_order_qty(cand), "sl": sl})
            except ConnectorError as e:
                if e.code in TRY_ANOTHER:
                    declined[cand] = e.code
                    continue
                raise
            sym = cand
            break
        if sym is None:
            self.record("bracket stop fires", "SKIP",
                        f"no instrument accepted a bracketed order: {declined}")
            return
        if declined:
            # The symbols are the adapter's catalog's: printed raw, one with
            # line breaks in it would add whole lines to the report.
            self._info(f"declined before {sym}: {declined}")
        fill = self.c.wait_event("order.filled",
                                 lambda d: d["client_id"] == k, timeout=30)
        # Waiting for the market to travel to a fixed stop is a coin flip
        # whose odds depend entirely on the broker's volatility against its
        # own stop distance: on a quiet FX pair it can be minutes or never.
        # Trail the stop in behind the market instead, so the scenario
        # converges on any venue and exercises order.modify on the way.
        can_trail = self._cap("orders").get("modify")
        closed, deadline, trails, refused = None, time.time() + 360, 0, {}
        while closed is None and time.time() < deadline:
            try:
                closed = self.c.wait_event(
                    "position.closed",
                    lambda d: d["position_id"] == fill["position_id"],
                    timeout=5, since=mark)
                break
            except TimeoutError:
                pass
            if not can_trail:
                continue
            q = self.quote_for(sym)
            want = round(q["bid"] - step, spec["digits"])
            if want > sl:
                try:
                    self.c.request("order.modify",
                                   {"position_id": fill["position_id"],
                                    "sl": want})
                    sl = want
                    trails += 1
                except ConnectorError as e:
                    refused[e.code] = refused.get(e.code, 0) + 1
        if closed is None:
            self.c.request("flatten_all")
            self.record("bracket stop fires", "FAIL",
                        f"no position.closed within 6 minutes; sl trailed "
                        f"{trails} times to {sl}, refusals {refused or 'none'}")
            return
        self.record("bracket stop fires",
                    "PASS" if closed["reason"] == "sl" else "FAIL",
                    f"reason={closed['reason']} at {closed['price']} after "
                    f"{trails} trails, refusals {refused or 'none'}")

    def s09_partial_fill(self):
        if not self.partial_hint:
            self.record("partial fill emits order.partial", "SKIP",
                        "no --partial-hint given")
            return
        sym, qty = self.partial_hint.split(":")
        k = cid()
        self.c.request("order.place", {"client_id": k, "symbol": sym,
                                       "side": "buy", "qty": float(qty)})
        part = self.c.wait_event("order.partial",
                                 lambda d: d["client_id"] == k, timeout=30)
        full = self.c.wait_event("order.filled",
                                 lambda d: d["client_id"] == k, timeout=30)
        total = part["qty"] + full["qty"]
        self.c.request("flatten_all")
        self.record("partial fill emits order.partial",
                    "PASS" if abs(total - float(qty)) < 1e-9 else "FAIL",
                    f"partial={part['qty']} final={full['qty']}")

    def s10_flatten_under_load(self):
        if self._needs_fill("flatten_all clears book and is repeat-safe"):
            return
        sym = self.tradable_symbol
        spec = self.c.catalog[sym]
        for _ in range(3):
            k = cid()
            self.c.request("order.place", {"client_id": k, "symbol": sym,
                                           "side": "buy",
                                           "qty": self.min_order_qty(sym)})
            self.c.wait_event("order.filled", lambda d: d["client_id"] == k,
                              timeout=30)
        out = self.c.request("flatten_all")
        left = self.c.request("positions.list")["positions"]
        again = self.c.request("flatten_all")
        ok = out["closed"] >= 1 and not left and again["closed"] == 0
        self.record("flatten_all clears book and is repeat-safe",
                    "PASS" if ok else "FAIL",
                    f"closed={out['closed']} left={len(left)}")

    def s11_history(self):
        if self._needs_fill("history.fills returns the ledger"):
            return
        fills = self.c.request("history.fills")["fills"]
        ok = len(fills) >= 3 and all("price" in f and "time" in f
                                     for f in fills)
        self.record("history.fills returns the ledger",
                    "PASS" if ok else "FAIL", f"fills={len(fills)}")

    def s12_unsupported_method(self):
        """SPEC 1: an unknown method is answered `unsupported`, and a method
        is its exact name. The Connector sends no name outside the protocol,
        so the suite's own go out through probe_unknown: one is no method at
        all, and one spells order.place another way (with no parameters, so
        an adapter that maps it onto its order handler is caught without an
        order). Anything but `unsupported` for either is a FAIL."""
        wrong = []
        for name in Connector.UNKNOWN_PROBES:
            try:
                self.c.probe_unknown(name)
                wrong.append(f"{name}: accepted")
            except ConnectorError as e:
                if e.code != "unsupported":
                    wrong.append(f"{name}: code={e.code}")
        self.record("unknown method -> unsupported",
                    "FAIL" if wrong else "PASS",
                    "; ".join(wrong) or "asked "
                    + ", ".join(Connector.UNKNOWN_PROBES))

    def s13_event_contiguity(self):
        ok = self.c.seq_gaps == 0 and len(self.c.events) > 0
        self.record("event seq contiguous",
                    "PASS" if ok else "FAIL",
                    f"gaps={self.c.seq_gaps} events={len(self.c.events)}")

    def s14_reconnect(self):
        if not self.restartable:
            self.record("reconnect: broker truth survives restart", "SKIP",
                        "adapter not marked --restartable")
            return
        if self._needs_fill("reconnect: broker truth survives restart"):
            return
        sym = self.tradable_symbol
        k = cid()
        self.c.request("order.place", {"client_id": k, "symbol": sym,
                                       "side": "buy",
                                       "qty": self.min_order_qty(sym)})
        fill = self.c.wait_event("order.filled",
                                 lambda d: d["client_id"] == k, timeout=30)
        # What this scenario opened, named so when the broker cannot be read
        # after the restart.
        opened = f"{fill.get('symbol')} buy {fill.get('qty')}"
        # hard-kill: no bye, exactly like a crashed session
        self.c.proc.kill()
        self.c.proc.wait()
        time.sleep(0.2)
        self.c = self._connector()
        pos, by_cid, by_fill, failed = {}, [], [], None
        try:
            # A hello the adapter refuses as one that may pass is asked
            # again, as s00 and s01 ask it (a process that logs in as it
            # starts refuses its first).
            self._hello(self.c)
            self.c.reconcile()
            pos = dict(self.c.positions)  # copy: flatten changes the view
            # What the scenario is named for: the BROKER remembered the
            # position. client_id is the nicest proof and an optional field
            # (SPEC: a netting book merges orders into one position, and a
            # restart can lose the mapping), so falling back to symbol and
            # size tests the property rather than the bookkeeping.
            by_cid = [p for p in pos.values() if p.get("client_id") == k]
            by_fill = [p for p in pos.values()
                       if p.get("symbol") == fill["symbol"]
                       and abs(float(p.get("qty") or 0))
                       >= float(fill["qty"]) - 1e-9]
        except Exception as e:  # noqa: BLE001 - said in the line, after
            failed = f"{type(e).__name__}: {e}"
        finally:
            # Whatever happened since the kill (the new process's hello timed
            # out, it was refused, the process died), what this scenario
            # opened is closed: on a connection whose hello was read, armed
            # as s01 armed the first (a restart is a new decision,
            # Connector.start). Without this, a failure before the flatten
            # leaves the position open on a live account.
            sent, answered, why = self._close_what_was_opened()
            # Then the book, read on a connection that answers: asked once
            # more on a new connection when it cannot be read (with a
            # process that hung after its hello, or died, the line would say
            # "not known" and name nothing). None when it cannot be read at
            # all.
            left = self._still_open(why) if sent is not None else None
        how = "client_id" if by_cid else ("symbol+qty" if by_fill else "-")
        # The failure is cut, so the words about the account (what is still
        # open on it) are never cut off the end of the line by an adapter's
        # long message (record() keeps 600 characters).
        detail = (self._clean(failed, 200) if failed else
                  f"positions_after_restart={len(pos)}, matched by {how}")
        still = ("; still open after flatten_all: " if sent
                 else "; still open: ")
        if left is None and answered:
            # flatten_all was answered and the book could not be read after
            # it: neither "still open" nor "flat" is known ("still open"
            # here could be said of a flat account).
            detail += ("; flatten_all was answered; not known whether "
                       "anything is still open")
        elif left is None:
            detail += (still + f"{opened}, the position this scenario "
                       "opened, as far as the suite knows")
        elif left:
            detail += still + ", ".join(left)
        elif failed or why:
            detail += ("; what this scenario opened was closed all the same"
                       if sent else "; the account holds nothing open")
        if why:
            detail += " (" + "; ".join(why) + ")"
        self.record("reconnect: broker truth survives restart",
                    "PASS" if (by_cid or by_fill) and not failed
                    and left == [] and not why else "FAIL", detail)

    def _hello(self, conn: Connector, tries: int = 3) -> dict:
        """conn.hello(), a refusal the adapter says may pass (retryable, on
        a connection still open: _no_session) asked again, as s00 and then
        s01 ask it: up to `tries` hellos, a second apart."""
        for _ in range(tries - 1):
            try:
                return conn.hello()
            except ConnectorError as e:
                if not (e.retryable and conn.alive):
                    raise
            time.sleep(1.0)
        return conn.hello()

    # What s14's close path may meet on a connection: the adapter refused,
    # ended, or did not answer in time.
    _FAILS = (ConnectorError, Disconnected, OSError, TimeoutError)

    @classmethod
    def _what(cls, e: BaseException) -> str:
        """A failure of s14's close path, as its line quotes it: the kind
        and the adapter's words, cut so several fit on one line."""
        return cls._clean(f"{type(e).__name__}: {e}", 160)

    def _ready(self) -> str | None:
        """A connection to read and close the account on after s14's
        restart, whose hello has been read: self.c as it is; else asked
        again (a process still running whose hello timed out, or was
        refused, may pass); else a new connection in its place (_anew).
        None when one is had, else why not."""
        if self.c.alive and self.c.handshake:
            return None
        why = "the connection ended"
        if self.c.alive:
            try:
                self._hello(self.c)
                return None
            except self._FAILS as e:
                why = self._what(e)
        then = self._anew()
        if then is None:
            return None
        return (f"no connection could be opened to read or close it: "
                f"{why}; then {then}")

    def _anew(self) -> str | None:
        """A new connection in self.c's place, whose hello has been read:
        what the last one met (a refusal, its end, or no answer in time) is
        not asked of it again. Its process is ended first and at once: one
        that hangs answers no bye either. None when one is had, else what
        its hello met."""
        old = self.c
        try:
            if old.proc is not None and old.proc.poll() is None:
                old.proc.kill()
                old.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - close() below ends it either way
            pass
        try:
            old.close()
        except Exception:  # noqa: BLE001 - it is replaced either way
            pass
        self.c = self._connector()
        try:
            self._hello(self.c)
            return None
        except self._FAILS as e:
            return self._what(e)

    def _close_what_was_opened(self) -> tuple[bool | None, bool, list[str]]:
        """flatten_all after s14's restart, on a ready (_ready) and, where
        it is not paper, armed (_arm_again) connection; asked once more on a
        new connection (_anew) when it fails (a process that hung after its
        hello, or died when asked, would leave the position open).

        (whether flatten_all was sent: None when no connection could be
        had at all, so the account cannot be read either; whether it was
        answered; what was met on the way, in order)."""
        why = self._ready()
        if why is not None:
            return None, False, [why]
        notes: list[str] = []
        sent = False
        for again in (False, True):
            if again:
                then = self._anew()
                if then is not None:
                    notes.append("no new connection could be opened to ask "
                                 f"flatten_all again: {then}")
                    return sent, False, notes
            not_armed = self._arm_again()
            if not_armed is not None:
                notes.append(not_armed)
                return sent, False, notes
            sent = True
            try:
                self.c.request("flatten_all")
            except self._FAILS as e:
                notes.append(("asked again on a new connection, flatten_all: "
                              if again else "flatten_all: ") + self._what(e))
                continue
            if again:
                notes.append("asked again on a new connection, flatten_all "
                             "was answered")
            return True, True, notes
        return sent, False, notes

    def _arm_again(self) -> str | None:
        """Arm a connection the suite opened again (s14) as s01 armed the
        first: on a broker that is not paper, and only when --live was
        given. None when it may trade; else why not."""
        if self.c.is_paper:
            return None
        mode = self.c.handshake.get("mode")
        if not self.live_ok:
            return (f"the adapter came back with mode {mode!r} and --live "
                    "was not given, so flatten_all was not sent")
        try:
            self.c.arm("conformance suite, --live given, after the restart")
        except ConnectorError as e:          # not_armed: its terms, SPEC 3.3
            return f"not armed again: {e}"
        self._info(f"ARMED again after the restart: mode={mode!r}")
        return None

    def _still_open(self, notes: list[str]) -> list[str] | None:
        """What the broker's book still holds, by instrument, side and size,
        once it has had a moment to settle (as _settle: an async venue books
        a close a beat after it). Read once more on a new connection
        (_anew) when it cannot be read; None when it still cannot, with
        what was met added to `notes`."""
        anew = False
        for again in (False, True):
            # A connection that has ended is not asked: the book is read on
            # a new one at once.
            if again or not self.c.alive:
                then = self._anew()
                if then is not None:
                    notes.append("no new connection could be opened to read "
                                 f"the book: {then}")
                    return None
                anew = True
            rows = []
            try:
                for _ in range(20):
                    got = self.c.request("positions.list")
                    rows = (got.get("positions") if isinstance(got, dict)
                            else None) or []
                    if not rows:
                        break
                    time.sleep(0.25)
            except self._FAILS as e:
                notes.append(("read on a new connection, positions.list: "
                              if anew else "positions.list: ")
                             + self._what(e))
                if again:
                    return None
                continue
            if anew:
                notes.append("the book was read on a new connection")
            return [f"{p.get('symbol')} {p.get('side')} {p.get('qty')}"
                    for p in rows if isinstance(p, dict)]
        return None

    # == driver ==========================================================
    def run(self) -> int:
        scenarios = [self.s00_interactive_auth,
                     self.s01_handshake, self.s02_reconcile,
                     self.s03_happy_path, self.s04_idempotent_replay,
                     self.s05_duplicate_conflict, self.s06_reject_paths,
                     self.s07_insufficient_margin, self.s08_brackets,
                     self.s09_partial_fill, self.s10_flatten_under_load,
                     self.s11_history, self.s12_unsupported_method,
                     self.s13_event_contiguity, self.s14_reconnect]
        self._print(f"conformance: {' '.join(self.cmd)}")
        for s in scenarios:
            if self.abort_reason:
                # No login means no session; every remaining scenario would
                # fail for the same one reason. A wall of identical FAILs
                # buries the actual message, so the rest is skipped by name,
                # and the reason is said once: where it was first said, or
                # by the first scenario skipped for it.
                if self._said_at is None:
                    self.record(s.__name__, "SKIP", self.abort_reason)
                    self._said_at = s.__name__
                else:
                    self.record(s.__name__, "SKIP",
                                f"not run: see {self._said_at}")
                continue
            try:
                s()
            except Exception as e:
                self.record(s.__name__, "FAIL", f"{type(e).__name__}: {e}")
        try:
            self.c.close()
        except Exception:
            pass
        # What the adapter wrote to stderr up to its end is said before the
        # result (bounded: a child of the adapter may hold the pipe open).
        for t in list(self._drains):
            t.join(timeout=2.0)
        p = sum(1 for _, st, _ in self.results if st == "PASS")
        k = sum(1 for _, st, _ in self.results if st == "SKIP")
        f = sum(1 for _, st, _ in self.results if st == "FAIL")
        w = sum(1 for _, st, _ in self.results if st == "WARN")
        # A warning that only appears mid-scroll is a warning nobody reads.
        with self._out:
            self._ended = True
            print(f"RESULT: {p} pass, {k} skip, {f} fail"
                  + (f", {w} warn" if w else ""), flush=True)
        return 1 if f else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True,
                    help="adapter command line (quoted)")
    ap.add_argument("--partial-hint", default=None)
    ap.add_argument("--restartable", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="the adapter is NOT a paper account and you accept "
                         "that this suite will place real orders on it")
    ap.add_argument("--auth-driver", default=None,
                    help="command (quoted) that plays the human + browser "
                         "for an interactive-auth adapter; gets the "
                         "auth.begin url appended as its last argument")
    args = ap.parse_args()
    sys.exit(Suite(shlex.split(args.adapter), args.partial_hint,
                   args.restartable, args.live,
                   shlex.split(args.auth_driver) if args.auth_driver
                   else None).run())


if __name__ == "__main__":
    main()
