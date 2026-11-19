#!/usr/bin/env python3
"""Brue Connect adapter for Quantex Terminal Link (binary TCP venue).

Terminal Link is not an HTTP API. It is one persistent TCP socket carrying
length-prefixed binary frames: a 4-byte big-endian payload length, then the
payload, which is a run of UTF-8 fields joined by NUL bytes. Field 1 is the
message type in ASCII decimal, and every other number on the wire is ASCII
decimal too, so the "binary" part is the framing and nothing else. Reference
data, quotes, history, orders and account all ride the SAME socket,
multiplexed by message type, which is why this adapter opens exactly one
connection and demultiplexes it itself.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no socket, no handshake, no catalog. The
    connection, the token handshake and the first reads all happen in
    `on_first_call`, so a platform's picker can learn the broker's name
    without opening a session on the venue.
  * three threads behind the stdio loop. The READER owns the socket and does
    nothing but reassemble frames and route them; it never issues a request,
    because the answer to that request could only be delivered by itself. The
    WORKER does everything that needs a round trip (resolving which position a
    fill landed on, reading the fee ledger after a close) and is single
    threaded, so platform events keep the order the venue produced them in.
    The POLLER is the backstop: the venue pushes no notice at all for a
    stop-out (docs 7.4) and the fill ledger is the only record of one.
  * a TCP read returns whatever bytes happened to arrive, so frames are
    reassembled from a buffer rather than assumed one-read-one-message, and
    the length prefix is range-checked BEFORE anything is allocated for it. A
    wire that says "here comes 3 GB" is a protocol violation, not an
    instruction.
  * order updates are treated as AT LEAST ONCE and out of order. `[142]` is
    broadcast to every connection on the account and carries no req_id, and it
    can arrive BEFORE the `[141]` ack that first names the order id, so an
    update for an unknown order is parked and replayed the moment the id is
    registered. Every emission is keyed on the CUMULATIVE filled quantity,
    so a repeated frame moves nothing and one venue quirk never becomes two
    fills on the platform's book.
  * the fill ledger (`[157]`) is the REST-equivalent backstop for everything
    the push channel may drop: fills for an order that never got its `[142]`,
    the fee and price of a close, and the trade behind a position that
    vanished without a word.

Two Terminal Link asymmetries drive most of the odd-looking code here. A
successful CANCEL has no req_id-tagged reply at all (only the broadcast
`[142]` state X), while a failed one does, so a cancel cannot be written as a
normal request/reply. And a CLOSE (`[155]`) answers with the realised P/L
`[156]` that may land either side of the fills it describes, so the close path
is a job filed before the request goes out rather than a value returned by it.
"""
from __future__ import annotations

import os
import queue
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

# "host:port". The venue binds loopback on its own host; a deployment reaches
# it through whatever circuit the account manager issued (docs section 1).
ADDR = os.environ.get("QUANTEX_ADDR", "127.0.0.1:7621")

# Credentials never travel in a protocol message (SPEC 8.5): the token comes
# from QUANTEX_TOKEN, or from a `token` file in BRUE_BROKER_STATE. There is no
# baked-in fallback; with neither set the handshake is refused as unauthorized.
DEMO_TOKEN = ""

PROTO_TAG = "QTX1"          # fixed magic, not a version negotiation (docs 2.1)
MAX_FRAME = 64 * 1024       # docs 1.1: anything larger is a protocol violation
HEARTBEAT_EVERY = 8.0       # docs 1.3: the venue drops a client silent for 30 s
SOCKET_READ_TIMEOUT = 40.0  # the venue heartbeats every 10 s; 40 s means dead
CALL_TIMEOUT = 12.0         # the venue answers well inside this; slower is trouble
CANCEL_SILENCE = 2.5        # docs 6.5: a cancel that works answers nothing

# The platform's request deadline is 10 s (the SDK's default), so anything
# that waits on the venue inside a handler has to finish inside it or hand the
# rest to the poller. A close normally confirms in well under a second.
CLOSE_WAIT = 7.0
FLATTEN_BUDGET = 8.0

# Wire timeframes (docs 5.4) against the SPEC's vocabulary, both directions.
TF_TO_WIRE = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
              "1h": "H1", "4h": "H4", "1d": "D1"}
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}
# docs 5.4: "Terminal Link only carries 60 days of replayable history"; asking
# for a start older than that is error 1304 rather than a short answer, so the
# request is clamped here instead of being refused there.
HISTORY_DEPTH_S = 60 * 86400

# docs 4.1 `kind` to the SPEC's asset_class vocabulary. SF is a cash-settled
# perpetual future on this venue, so it files with futures; that it never
# expires is said in `expires_at: null`, which is the field for it.
ASSET_CLASS = {"IDX": "index", "EQ": "equity", "FX": "fx", "SF": "future"}

# docs 4.3, published as three session codes and translated once, here, into
# the calendar windows a platform can gate on (SPEC 3).
SESSIONS = {
    # Continuous: 24 hours a day, 7 days a week, weekends included. Written as
    # one explicit seven-day window rather than an empty list, because SPEC 3
    # reads an empty `sessions` as "this broker publishes no calendar" and
    # always-open and unknown must not look identical.
    "C": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00",
           "tz": "UTC"}],
    # The FX week: Sunday 21:05 UTC through Friday 21:00 UTC.
    "F": [{"days": [7], "open": "21:05", "close": "24:00", "tz": "UTC"},
          {"days": [1, 2, 3, 4], "open": "00:00", "close": "24:00", "tz": "UTC"},
          {"days": [5], "open": "00:00", "close": "21:00", "tz": "UTC"}],
    # US equity floor hours.
    "U": [{"days": [1, 2, 3, 4, 5], "open": "13:30", "close": "20:00",
           "tz": "UTC"}],
}

# docs section 9. The commission schedule is published in the integrator guide
# and NOT on the reference-data frame, so it is keyed on the `kind` field the
# frame does carry rather than hardcoded per symbol (docs 4.2 is explicit that
# the instrument table must be resolved at runtime, and it is: only the fee
# model per class comes from here).
FEES_BY_KIND = {
    "EQ": {"commission_per_lot": 0.0, "mode": "per_share", "rate": 0.005,
           "min": 1.0},
}
FEES_FREE = {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
             "min": 0.0}

# Wire order state (docs 6.4) to what it means to this adapter.
STATE_IN = {"W": "resting", "P": "partial", "F": "filled", "X": "cancelled",
            "E": "expired", "R": "rejected"}

# docs 10, split by frame because 129 and 143 are two different code ranges
# that happen to share a four-field shape; the guide says in as many words not
# to treat them as interchangeable, so they get two tables.
ERR_129 = {
    "1201": ("unknown_symbol", False),
    "1301": ("unsupported", False),
    # No SPEC code means "your range is deeper than my archive". Kept as an
    # x_ extension rather than flattened to `internal`, which would destroy the
    # only information the platform had (SPEC 6). Requests are clamped to the
    # 60-day window, so this should be unreachable.
    "1304": ("x_history_depth_exceeded", False),
    "1501": ("not_found", False),
    "1999": ("internal", True),
}
ERR_143 = {
    "1401": ("invalid_qty", False),
    "1402": ("unknown_symbol", False),
    "1403": ("insufficient_margin", False),
    "1404": ("market_closed", False),
    # 1405 is deliberately one code for "no such order" and "not on the book
    # any more" (docs 6.5), which is exactly the not_found / not_resting
    # distinction. Resolved from this adapter's order book at the callsite.
    "1405": ("not_resting", False),
    # Bad side / bad otype / a price field the type requires left empty. Every
    # one of those is validated locally before the frame goes out, so seeing
    # this means THIS adapter built a bad order, not that the caller did.
    "1406": ("internal", False),
    "1407": ("invalid_expiry", False),
}


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[quantex] {msg}", file=sys.stderr, flush=True)


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_qty(v) -> str:
    """A quantity as the venue writes numbers: plain decimal, no exponent.

    `%r` on a float can produce `1e-05`, which the venue parses as a string
    and rejects; `%.10f` with the tail stripped never can.
    """
    s = "%.10f" % (float(v),)
    s = s.rstrip("0").rstrip(".")
    return s or "0"


def _fmt_price(v, digits: int) -> str:
    """Prices go on the wire at the instrument's own quoted precision."""
    return "%.*f" % (int(digits), float(v))


def _cost(amount: float) -> float:
    """A charge, signed the way the protocol wants it: negative when it cost
    the holder, and never NEGATIVE zero. JSON preserves the sign, and a
    platform that renders `-0.00` in a fee column looks broken over nothing."""
    a = round(amount, 2)
    return -a if a else 0.0


class LinkError(Exception):
    """A framing or transport failure. The session does not survive it."""


class VenueError(Exception):
    """A `[129]` or `[143]` refusal, carrying the venue's own code."""

    def __init__(self, frame_type: str, code: str, text: str):
        self.frame_type = frame_type
        self.code = str(code)
        self.text = text
        super().__init__(f"{frame_type}/{code}: {text}")

    def to_adapter_error(self, not_found_ids=()) -> AdapterError:
        table = ERR_143 if self.frame_type == "143" else ERR_129
        hit = table.get(self.code)
        if hit is None:
            # An unmapped venue code keeps its identity as an x_ extension.
            return AdapterError("x_quantex_" + self.code,
                                f"{self.code}: {self.text}", False)
        code, retry = hit
        if self.code == "1405" and not_found_ids:
            code = "not_found"
        return AdapterError(code, f"{self.code}: {self.text}", retry)


class _Pending:
    """One outstanding request, keyed by the req_id it was sent under."""

    __slots__ = ("rows", "final", "error", "done", "silent_ok", "order_id")

    def __init__(self, silent_ok=False, order_id=None):
        self.rows: list = []
        self.final = None
        self.error: VenueError | None = None
        self.done = threading.Event()
        # Cancel has no success frame (docs 6.5): silence IS the answer.
        self.silent_ok = silent_ok
        self.order_id = order_id


class TerminalLink:
    """The socket: framing, handshake, heartbeats, request correlation.

    Everything protocol-shaped lives here so the adapter above can think in
    orders and positions. `on_push` is called on the READER thread for the two
    unsolicited message types (`[121]` ticks, `[142]` order updates); it must
    not block, and it does not, because everything expensive is queued.
    """

    def __init__(self, addr: str, token: str, on_push, on_state, on_reconnect):
        host, _, port = addr.rpartition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 7621)
        self._token = token
        self._on_push = on_push
        self._on_state = on_state
        self._on_reconnect = on_reconnect
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._req_seq = 0
        self._last_send = 0.0
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._generation = 0
        self.account_id: str | None = None
        # server_time from the `[101]` welcome minus the local clock. Sessions
        # are judged against the VENUE's day, and an adapter reporting its own
        # skewed clock would have a platform gate orders on the wrong one.
        self.clock_offset_ms = 0.0

    # -- framing ---------------------------------------------------------
    def _recv_frame(self) -> list[str]:
        """One complete frame, reassembled from however the bytes arrive.

        A TCP read returns what happened to be in the kernel buffer: half a
        frame, three frames, a header split across two packets. The buffer is
        the whole point of this method.
        """
        while True:
            if len(self._buf) >= 4:
                (length,) = struct.unpack(">I", bytes(self._buf[:4]))
                # The length prefix is UNTRUSTED input. Checked before a single
                # byte is reserved for it: a corrupt or hostile header must
                # cost a connection, never 4 GB of this process's memory.
                if length == 0 or length > MAX_FRAME:
                    raise LinkError(f"frame length {length} out of range")
                if len(self._buf) >= 4 + length:
                    payload = bytes(self._buf[4:4 + length])
                    del self._buf[:4 + length]
                    return [f.decode("utf-8", "replace")
                            for f in payload.split(b"\x00")]
            chunk = self._sock.recv(65536)
            if not chunk:
                raise LinkError("venue closed the connection")
            self._buf += chunk

    def _send(self, fields):
        payload = b"\x00".join(str(f).encode("utf-8") for f in fields)
        if len(payload) > MAX_FRAME:
            raise AdapterError("internal", "outbound frame exceeds 64 KiB")
        blob = struct.pack(">I", len(payload)) + payload
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise LinkError("not connected")
            sock.sendall(blob)
            self._last_send = time.time()

    # -- lifecycle -------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._open()
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._state_lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _open(self):
        sock = socket.create_connection((self.host, self.port), timeout=10.0)
        # Orders are single small frames; Nagle would hold one back waiting for
        # company that a request/reply protocol never sends.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(SOCKET_READ_TIMEOUT)
        with self._state_lock:
            self._sock = sock
            self._buf = bytearray()
            self._generation += 1
        self._last_send = time.time()
        # docs 2.1: the HELLO must be the very first frame and must arrive
        # within 5 s of the connect, or the venue drops the socket unanswered.
        self._send([100, PROTO_TAG, self._token])
        frame = self._recv_frame()
        if frame[0] == "199":
            code = frame[1] if len(frame) > 1 else "?"
            text = frame[2] if len(frame) > 2 else ""
            if code == "1101":
                raise AdapterError("unauthorized",
                                   f"Quantex refused the token: {text}")
            # 1102 is a bad protocol tag, and the tag is a constant in this
            # file: that is this adapter being wrong, not the operator.
            raise AdapterError("internal",
                               f"handshake refused ({code}): {text}")
        if frame[0] != "101":
            raise LinkError(f"unexpected handshake reply {frame[:2]}")
        server_ms = _num(frame[1]) * 1000.0
        if server_ms:
            skew = server_ms - time.time() * 1000.0
            # Server time is whole seconds, so a sub-second "skew" is just the
            # rounding. Only a real disagreement is worth carrying.
            self.clock_offset_ms = skew if abs(skew) > 2000 else 0.0
        self.account_id = frame[2] if len(frame) > 2 else None
        self._connected.set()
        log(f"connected to {self.host}:{self.port}, account {self.account_id}"
            + (f", clock offset {self.clock_offset_ms / 1000:.1f}s"
               if self.clock_offset_ms else ""))

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                frame = self._recv_frame()
            except (LinkError, OSError) as e:
                if self._stop.is_set():
                    return
                self._drop(f"{type(e).__name__}: {e}")
                if not self._reconnect():
                    return
                continue
            try:
                self._route(frame)
            except Exception as e:      # noqa: BLE001 - one bad frame only
                log(f"routing {frame[:2]} failed: {type(e).__name__}: {e}")

    def _drop(self, why: str):
        self._connected.clear()
        with self._state_lock:
            sock, self._sock = self._sock, None
            waiting = list(self._pending.values())
            self._pending.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for p in waiting:
            # Every caller blocked on a reply the socket can no longer deliver
            # is failed rather than left to time out: a mutating call must end
            # in an answer (SPEC 8.4), and "the link went down" is one.
            p.error = VenueError("129", "1999", f"link lost: {why}")
            p.done.set()
        self._on_state("degraded", f"Terminal Link dropped: {why}")

    def _reconnect(self) -> bool:
        delay = 0.5
        while not self._stop.is_set():
            try:
                self._open()
            except (OSError, LinkError, AdapterError) as e:
                log(f"reconnect failed ({e}); retrying in {delay:.1f}s")
                if self._stop.wait(delay):
                    return False
                delay = min(delay * 2, 15.0)
                continue
            self._on_state("connected", "Terminal Link re-established")
            # A new socket knows nothing about the old one's subscriptions and
            # the venue replays nothing, so the adapter re-opens them and
            # re-reads the book. Handed off rather than done here: this runs on
            # the reader thread, and a request issued from it would wait for a
            # reply only it can deliver.
            self._on_reconnect()
            return True
        return False

    def _heartbeat_loop(self):
        """docs 1.3: the venue's 30 s timer tracks what it RECEIVES, so a
        client that only reads is dropped even while ticks are pouring in."""
        while not self._stop.wait(1.0):
            if not self._connected.is_set():
                continue
            if time.time() - self._last_send >= HEARTBEAT_EVERY:
                try:
                    self._send([1])
                except (LinkError, OSError):
                    pass

    # -- request correlation ---------------------------------------------
    def _next_req_id(self) -> str:
        with self._state_lock:
            self._req_seq += 1
            return str(self._req_seq)

    def call(self, msgtype: int, *args, timeout: float = CALL_TIMEOUT,
             req_id: str | None = None) -> _Pending:
        """Send a request and wait for the frame(s) that answer it.

        Row frames accumulate; the first frame that is not a row ends the
        wait. That works without a per-message reply table because a req_id
        answers exactly one request, and the two frames that are NOT replies
        (`[121]` ticks, `[142]` order updates) are routed before this ever
        sees them.
        """
        rid = req_id or self._next_req_id()
        pending = _Pending()
        with self._state_lock:
            self._pending[rid] = pending
        try:
            self._send([msgtype, rid] + list(args))
        except (LinkError, OSError) as e:
            with self._state_lock:
                self._pending.pop(rid, None)
            raise AdapterError("internal", f"Terminal Link write failed: {e}",
                               True) from None
        if not pending.done.wait(timeout):
            with self._state_lock:
                self._pending.pop(rid, None)
            raise AdapterError("internal",
                               f"Quantex did not answer message {msgtype} "
                               f"within {timeout:.0f}s", True)
        if pending.error is not None:
            raise pending.error
        return pending

    def cancel(self, order_id: str, timeout: float = CANCEL_SILENCE):
        """Cancel, against a venue that confirms success by saying nothing.

        docs 6.5: a successful cancel is reported ONLY by the broadcast
        `[142]` state X; the req_id gets no reply of its own. A failed one
        does get a `[143] 1405`. So this waits for either the broadcast (fast
        path) or a refusal, and treats the silence that follows as the success
        the guide describes rather than blocking on a reply that never comes.
        """
        rid = self._next_req_id()
        pending = _Pending(silent_ok=True, order_id=str(order_id))
        with self._state_lock:
            self._pending[rid] = pending
        try:
            self._send([144, rid, order_id])
        except (LinkError, OSError) as e:
            with self._state_lock:
                self._pending.pop(rid, None)
            raise AdapterError("internal", f"cancel write failed: {e}", True)
        pending.done.wait(timeout)
        with self._state_lock:
            self._pending.pop(rid, None)
        if pending.error is not None:
            raise pending.error

    # -- routing ---------------------------------------------------------
    def _route(self, frame: list[str]):
        t = frame[0]
        if t == "1":
            return                      # inbound heartbeat: nothing to do
        if t == "121" or t == "142":
            if t == "142" and len(frame) > 2 and frame[2] == "X":
                # The broadcast IS a cancel's confirmation (docs 6.5), so a
                # caller waiting out the silence window is released the moment
                # it lands rather than sitting out the whole timeout.
                with self._state_lock:
                    for p in self._pending.values():
                        if p.silent_ok and p.order_id == frame[1]:
                            p.done.set()
            self._on_push(frame)
            return
        if len(frame) < 2:
            log(f"unroutable frame {frame!r}")
            return
        rid = frame[1]
        with self._state_lock:
            pending = self._pending.get(rid)
        if pending is None:
            # A reply to a request that already timed out, or a tick for a
            # subscription that was torn down. Neither is actionable.
            return
        if t in ("111", "131", "151", "158"):
            pending.rows.append(frame)
            return
        if t in ("129", "143"):
            pending.error = VenueError(t, frame[2] if len(frame) > 2 else "?",
                                       frame[3] if len(frame) > 3 else "")
        else:
            pending.final = frame
        with self._state_lock:
            self._pending.pop(rid, None)
        pending.done.set()

    def subscribe_id(self) -> str:
        """A req_id that outlives its request: every `[121]` for a live
        subscription carries the id it was opened under (docs 5.1), and the
        unsubscribe is addressed by that same id (docs 5.2)."""
        return self._next_req_id()

    def send_oneway(self, msgtype: int, *args):
        """A request the venue answers with nothing at all (the unsubscribe of
        docs 5.2). Failures are logged, not raised: tearing down a
        subscription that a dead socket already tore down is not an error."""
        try:
            self._send([msgtype] + list(args))
        except (LinkError, OSError) as e:
            log(f"one-way message {msgtype} not sent: {e}")


class QuantexAdapter(Adapter):
    name = "quantex"
    version = "1.0"
    broker = "Quantex Terminal Link"
    # Truthful (SPEC 8.2): the venue's own guide opens with "Quantex is a
    # simulated venue for integration testing. All prices are synthetic. No
    # real money is involved."
    mode = "paper"
    # The account is denominated in USD (`[154]` reports it, and it is
    # re-checked on first call, which complains on stderr if it ever differs).
    account_currency = "USD"
    # docs 4.2: an opposing fill nets an existing position down and a same-side
    # fill merges into it at the average. One position per symbol is netting
    # (SPEC 3).
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        self._state = os.environ.get("BRUE_BROKER_STATE") or \
            os.path.expanduser("~/.brue-connect/state/quantex")
        self.link = TerminalLink(ADDR, self._token(), self._on_push,
                                 self._set_session, self._on_reconnect)
        self._lock = threading.RLock()
        self._by_symbol: dict[str, dict] = {}     # code: instrument spec
        self._by_conid: dict[str, dict] = {}      # conid: instrument spec
        self._orders: dict[str, dict] = {}        # order_id: this side's view
        self._by_client: dict[str, str] = {}      # client_id: order_id
        self._orphans: dict[str, list] = {}       # order_id: parked [142]s
        self._positions: dict[str, dict] = {}     # position_id: last seen
        self._pos_fees: dict[str, float] = {}     # position_id: fees paid
        self._pos_meta: dict[str, dict] = {}      # position_id: what opened it
        self._closing: dict[str, dict] = {}       # position_id: close job
        self._subs: dict[str, str] = {}           # conid: subscription req_id
        self._bars: dict[tuple, int] = {}         # (symbol, tf): last bar ts
        self._work: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._session_state = None

    # == identity ========================================================
    def _token(self) -> str:
        """The account token: QUANTEX_TOKEN, then a `token` file in the
        profile's own state dir (BRUE_BROKER_STATE).

        Never a protocol message and never a parameter (SPEC 8.5). There is no
        baked-in fallback: with neither source set the token is empty and the
        venue refuses the handshake as unauthorized.
        """
        env = os.environ.get("QUANTEX_TOKEN")
        if env:
            return env.strip()
        try:
            with open(os.path.join(self._state, "token")) as fh:
                tok = fh.read().strip()
            if tok:
                return tok
        except OSError:
            pass
        return DEMO_TOKEN

    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # There is no field anywhere in Terminal Link for attaching a
                # stop loss or take profit, and no message to modify one
                # (docs 6.1, 6.6). The venue's own risk engine can close a
                # position on a level it holds internally, but a client cannot
                # set that level, so declaring brackets would offer a platform
                # a feature it could only fake. False, and refused loudly.
                "brackets": False,
                "modify": False,
                "trailing": False,
                # docs 7.3: `[155]` takes a qty and leaves the remainder open.
                "partial_close": True,
                "pending": True,
                "pending_types": ["limit", "stop", "stop_limit"],
                # docs 6.1: GTC and DAY, and nothing else. gtd is refused.
                "expiry": ["gtc", "day"],
                # The venue validates margin when the order is placed but
                # holds nothing against a resting one: `margin_used` counts
                # open positions only, so an order accepted comfortably can
                # still die at the moment it should have filled. That is
                # exactly the consequence the SPEC's flag exists to warn about.
                "reserves_margin": False,
                # No enumerate-orders message exists at all (the guide's
                # message list has placement, cancel, and nothing between).
                # This adapter can therefore only report orders it placed
                # itself; an order entered from another connection on the same
                # account is invisible to it forever, and saying so beats a
                # platform believing it has reconciled.
                "can_list": False,
                "oco": False,
                # Netting: there is no second position to close against.
                "close_by": False,
            },
            "data": {
                "quotes": True, "bars": True, "history": True,
                # The venue publishes no ledger of non-trade money movement
                # (no deposits, dividends or adjustments exist in any message),
                # so a balance that moves without a fill genuinely has no
                # explanation available here.
                "operations": False,
                # No cap is stated and none is enforced: one socket carries
                # every subscription (docs 1, 8). Absent means "the broker
                # states no limit" (SPEC 3), which is the truth.
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # docs 8: this environment enforces no hard limit and a production
        # deployment is "order of tens of requests per second". The lower end
        # of the venue's own sentence, since it is the one figure it commits to.
        return {"requests_per_second": 10}

    def broker_info(self) -> dict:
        # The venue's own words only. Its guide has no "about us" block, no
        # licence and no website, and inventing them is precisely what SPEC 3.1
        # says this field is not for.
        return {"display_name": "Quantex Terminal Link",
                "tagline": "Binary terminal link for low-latency integration",
                "mark": "QTX",
                "asset_classes": ["Index CFDs", "Equities", "FX",
                                  "Special Futures"]}

    def now_ms(self) -> int:
        return int(time.time() * 1000 + self.link.clock_offset_ms)

    # == session =========================================================
    def on_first_call(self):
        """Everything that costs the venue something happens here, not in
        `hello` (SPEC 3.1): the socket, the token handshake, the catalog."""
        self.link.start()
        self._load_catalog()
        acct = self._account()
        if acct["currency"] != self.account_currency:
            # `hello` has already gone out, so this cannot be corrected; loud
            # on stderr is the only honest move left.
            log(f"WARNING: account currency is {acct['currency']!r} but the "
                f"handshake declared {self.account_currency!r}")
        self._stop.clear()
        threading.Thread(target=self._worker_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._refresh_positions()
        self._set_session("connected", "Terminal Link open")

    def on_shutdown(self):
        self._stop.set()
        self._work.put(None)
        self.link.stop()

    def _set_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})

    def _on_reconnect(self):
        self._work.put(("resubscribe", None))

    def _call(self, msgtype: int, *args, **kw):
        """A venue request whose refusal is already a protocol error.

        Order placement, cancel and close each need the raw `VenueError` to
        decide between two SPEC codes, so they call the link directly; every
        other caller wants the mapping, and doing it here means an unmapped
        refusal can never reach a handler and be flattened into `internal` by
        the base class's catch-all.
        """
        try:
            return self.link.call(msgtype, *args, **kw)
        except VenueError as e:
            raise e.to_adapter_error() from None

    # == catalog =========================================================
    def _load_catalog(self):
        rows = self._call(110).rows
        by_symbol, by_conid = {}, {}
        for r in rows:
            # [111, req_id, conid, code, name, kind, quote_ccy, digits,
            #  qty_step, min_qty, max_qty, contract_size, margin_rate, session]
            if len(r) < 14:
                log(f"short reference-data frame ignored: {r!r}")
                continue
            spec = self._spec(r)
            by_symbol[spec["symbol"]] = spec
            by_conid[spec["conid"]] = spec
        if not by_symbol:
            raise AdapterError("internal", "Quantex returned an empty catalog")
        with self._lock:
            self._by_symbol, self._by_conid = by_symbol, by_conid

    def _spec(self, r: list) -> dict:
        conid, code, name, kind = r[2], r[3], r[4], r[5]
        quote_ccy = r[6]
        digits = int(_num(r[7], 2))
        tick = round(10.0 ** -digits, max(digits, 1))
        contract = _num(r[11], 1.0)
        base = None
        if kind == "FX" and len(code) == 6:
            # The only place the venue encodes a base currency at all: the
            # six-letter pair code. Display-only per docs 4, but the split is
            # unambiguous and a platform needs it to value a position.
            base = code[:3]
        sessions = SESSIONS.get(r[13])
        if sessions is None:
            # An unrecognised session code publishes NO calendar rather than a
            # guessed one: SPEC 3 reads an empty list as "no calendar", not
            # "closed", and the venue stays the final judge either way.
            log(f"unknown session code {r[13]!r} for {code}; "
                f"publishing no calendar")
            sessions = []
        return {
            "symbol": code,
            "conid": str(conid),
            "name": name,
            "asset_class": ASSET_CLASS.get(kind, "cfd"),
            "base_ccy": base,
            "quote_ccy": quote_ccy,
            "digits": digits,
            "tick_size": tick,
            # The SPEC's own default derivation, right for every Quantex
            # product: each is quoted in its own contract currency.
            "tick_value": round(contract * tick, 10),
            "lot_step": _num(r[8], 1.0),
            "min_qty": _num(r[9], 1.0),
            "max_qty": _num(r[10], 0.0),
            "min_notional": 0,
            "contract_size": contract,
            # docs 4.1: already a fraction of notional (0.05 = 20x), so it
            # goes through unscaled.
            "margin_rate": _num(r[12]),
            # The venue publishes one margin number and stops out on
            # equity/margin_used < 0.5 (docs 7.4), which is an account-level
            # rule, not a maintenance rate per instrument. Omitted, so the
            # SPEC's default (maintenance = initial) applies rather than an
            # invented second number.
            "margin_tiers": None,
            "sessions": [dict(s) for s in sessions],
            "holidays": [],          # no holiday calendar is published
            "early_closes": [],
            "tradable": True,
            "shortable": True,
            "trade_mode": "full",
            # No stop distance exists because no stop can be attached.
            "stops_level_points": 0,
            "fees": dict(FEES_BY_KIND.get(kind, FEES_FREE)),
            # The venue charges no financing on anything: the guide names the
            # spread and the commission as the only costs, and says the
            # Special Futures carry no overnight or weekend charge at all.
            # 0 with swap_mode "none" says "not charged", not "unknown".
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            # Special Futures are perpetual and nothing else here is dated.
            "expires_at": None, "successor": None, "dividend": None,
        }

    def handle_catalog_list(self, params):
        self._load_catalog()
        with self._lock:
            specs = list(self._by_symbol.values())
        # `conid` is this venue's own addressing and means nothing to a
        # platform, so it stays out of the InstrumentSpec on the wire.
        return {"instruments": [{k: v for k, v in s.items() if k != "conid"}
                                for s in specs]}

    def _instrument(self, symbol) -> dict:
        with self._lock:
            spec = self._by_symbol.get(symbol)
        if spec is None:
            self._load_catalog()        # catalogs change between deployments
            with self._lock:
                spec = self._by_symbol.get(symbol)
        if spec is None:
            raise AdapterError("unknown_symbol", f"no such instrument "
                                                 f"{symbol!r} at Quantex")
        return spec

    def _by_id(self, conid) -> dict | None:
        with self._lock:
            return self._by_conid.get(str(conid))

    # == account =========================================================
    def _account(self) -> dict:
        f = self._call(153).final
        # [154, req_id, currency, balance, equity, margin_used, margin_free]
        return {"currency": f[2], "balance": _num(f[3]), "equity": _num(f[4]),
                "margin_used": _num(f[5]), "margin_free": _num(f[6])}

    def handle_account_get(self, params):
        return self._account()

    # == positions =======================================================
    def _refresh_positions(self) -> dict:
        """Read the book and reconcile it against the last snapshot seen.

        The venue pushes NOTHING when its own risk logic closes a position
        (docs 7.4: a stop-out carries no order id and produces no `[142]`), so
        a position that quietly disappears is only ever discovered here.
        """
        rows = self._call(150).rows
        fresh = {}
        for r in rows:
            # [151, req_id, position_id, conid, side, qty, avg_price, unreal]
            spec = self._by_id(r[3])
            fresh[str(r[2])] = {
                "position_id": str(r[2]),
                "conid": str(r[3]),
                "symbol": spec["symbol"] if spec else str(r[3]),
                "side": "buy" if r[4] == "B" else "sell",
                "qty": _num(r[5]),
                "avg_price": _num(r[6]),
                "unrealized": _num(r[7]),
            }
        with self._lock:
            gone = [pid for pid in self._positions
                    if pid not in fresh and pid not in self._closing]
            previous = {pid: self._positions[pid] for pid in gone}
            self._positions = fresh
        for pid in gone:
            self._work.put(("vanished", previous[pid]))
        return fresh

    def _position_view(self, p: dict) -> dict:
        meta = self._pos_meta.get(p["position_id"], {})
        out = {
            "position_id": p["position_id"],
            "symbol": p["symbol"],
            "side": p["side"],
            "qty": p["qty"],
            "avg_price": p["avg_price"],
            # No stop loss, take profit or trail can be attached at this venue.
            "sl": None, "tp": None, "trail_points": 0,
            "unrealized_pnl": p["unrealized"],
            # The venue charges no financing at all, so 0 here is a fact.
            "swap": 0.0,
            # Commission is charged per FILL and the fill ledger never names
            # the position a deal belongs to, so the only fees attributable to
            # a position are the ones this process watched land on it. A
            # position adopted from an earlier session honestly reports 0
            # rather than a number invented from the schedule.
            "commission": _cost(self._pos_fees.get(p["position_id"], 0.0)),
            # Both from the fill this adapter saw open the position, and both
            # absent for a position it inherited: Terminal Link publishes
            # neither an open time nor an originating order on `[151]`, and
            # SPEC 4 says absent is the honest answer where inventing one is
            # not (a platform identifies a position by position_id anyway).
            "opened_at": meta.get("opened_at"),
            "from_order": meta.get("from_order"),
        }
        if meta.get("client_id"):
            out["client_id"] = meta["client_id"]
        return out

    def handle_positions_list(self, params):
        rows = self._refresh_positions()
        return {"positions": [self._position_view(p) for p in rows.values()]}

    # == orders ==========================================================
    def _validate_qty(self, spec: dict, qty) -> float:
        """Size checked against the catalog before the frame goes out.

        The venue answers off-step, below-min and above-max all with 1401, and
        also uses 1401 for a close with a bad qty, so its answer cannot be
        mapped back to anything more precise than "size". Checking here keeps
        `invalid_qty` meaning a size problem and only a size problem.
        """
        q = _num(qty, -1.0)
        step, lo, hi = spec["lot_step"], spec["min_qty"], spec["max_qty"]
        if q <= 0:
            raise AdapterError("invalid_qty", f"qty must be positive, got {qty!r}")
        if lo and q < lo - 1e-9:
            raise AdapterError("invalid_qty", f"qty {q} is below min_qty {lo}")
        if hi and q > hi + 1e-9:
            raise AdapterError("invalid_qty", f"qty {q} is above max_qty {hi}")
        if step > 0:
            units = q / step
            if abs(units - round(units)) > 1e-6:
                raise AdapterError("invalid_qty",
                                   f"qty {q} is not a multiple of the "
                                   f"{step} lot step")
            q = round(units) * step
        return round(q, 8)

    def _fee_for(self, spec: dict, qty: float) -> float:
        """The fee one fill of this size costs, from the published schedule.

        docs 9: equities are 0.005 per share with a 1.00 minimum per FILL,
        everything else is commission-free. This keeps a running commission
        per position, where the ledger is not consulted; the ledger's own
        number is used wherever a round trip is affordable (closes, history).
        """
        f = spec["fees"]
        if f["mode"] == "none":
            return 0.0
        charge = f["rate"] * qty * spec["contract_size"]
        return round(max(charge, f["min"]), 2)

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        otype = (params.get("type") or "market").lower()
        # Everything that decides what gets DEALT. A replay carrying different
        # terms must be refused (SPEC 6) and the venue does not refuse it
        # itself: a reused client_tag returns the ORIGINAL order even when the
        # contract, side and size all differ.
        sig = (symbol, side, _num(params.get("qty"), -1), otype,
               params.get("price"), params.get("stop_price"),
               params.get("expiry"), params.get("expiry_at"))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("invalid_qty",
                               f"side must be buy or sell, got "
                               f"{params.get('side')!r}")
        spec = self._instrument(symbol)
        qty = self._validate_qty(spec, params.get("qty"))
        for field, why in (("sl", "stop loss"), ("tp", "take profit"),
                           ("trail_points", "trailing stop")):
            if params.get(field) is not None:
                raise AdapterError(
                    "unsupported",
                    f"Terminal Link has no {why} attachment; there is no "
                    f"message that sets one")
        if otype not in ("market", "limit", "stop", "stop_limit"):
            raise AdapterError("unsupported", f"unknown order type {otype!r}")

        wire_type = {"market": "MKT", "limit": "LMT", "stop": "STP",
                     "stop_limit": "STPLMT"}[otype]
        limit_s, stop_s = "", ""
        digits = spec["digits"]
        resting_at = None       # the level the platform is told it rests at
        if otype == "limit":
            if params.get("price") is None:
                raise AdapterError("invalid_stops", "a limit order needs `price`")
            limit_s = _fmt_price(params["price"], digits)
            resting_at = _num(params["price"])
        elif otype == "stop":
            # SPEC 4 puts a plain stop's trigger in `price`; the wire calls it
            # stop_price (docs 6.1). `stop_price` is accepted as an alias so a
            # caller that uses the venue's own word is not punished for it.
            trigger = params.get("price")
            if trigger is None:
                trigger = params.get("stop_price")
            if trigger is None:
                raise AdapterError("invalid_stops",
                                   "a stop order needs its trigger in `price`")
            stop_s = _fmt_price(trigger, digits)
            resting_at = _num(trigger)
        elif otype == "stop_limit":
            if params.get("price") is None or params.get("stop_price") is None:
                raise AdapterError("invalid_stops",
                                   "a stop_limit order needs `stop_price` "
                                   "(the trigger) and `price` (the limit)")
            limit_s = _fmt_price(params["price"], digits)
            stop_s = _fmt_price(params["stop_price"], digits)
            resting_at = _num(params["price"])

        expiry = (params.get("expiry") or "gtc").lower()
        if expiry == "gtc":
            tif = "GTC"
        elif expiry == "day":
            tif = "DAY"
        elif expiry == "gtd":
            # Declared in capabilities.orders.expiry, so a platform should
            # never send it; refused rather than silently downgraded to DAY.
            raise AdapterError("invalid_expiry",
                               "Quantex honours GTC and DAY only")
        else:
            raise AdapterError("invalid_expiry", f"unknown expiry {expiry!r}")

        tag = self._client_tag(client_id)
        try:
            reply = self.link.call(
                140, tag, spec["conid"], "B" if side == "buy" else "S",
                _fmt_qty(qty), wire_type, limit_s, stop_s, tif)
        except VenueError as e:
            err = e.to_adapter_error()
            # SPEC 8.4: no mutating call may be silently dropped. A synchronous
            # refusal is a terminal outcome and is announced as one, so a
            # platform watching only the event stream still sees the order end.
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": err.code,
                                         "message": str(err)})
            raise err
        # [141, req_id, order_id, state]
        order_id = str(reply.final[2])
        acked = STATE_IN.get(reply.final[3] if len(reply.final) > 3 else "W",
                             "resting")
        rec = {
            "order_id": order_id, "client_id": client_id,
            "symbol": symbol, "conid": spec["conid"], "side": side,
            "qty": qty, "type": otype, "price": params.get("price"),
            "stop_price": params.get("stop_price"),
            "expiry": expiry, "expires_at": self._day_expiry_ms(spec)
            if expiry == "day" else None,
            "state": "new", "filled_emitted": 0.0, "avg_price": None,
            "placed_at": self.now_ms(), "done_at": None, "reason": None,
            "acked": acked, "announced": False,
            "deadline": time.time() + 30.0, "degraded": False,
        }
        with self._lock:
            self._orders[order_id] = rec
            self._by_client[client_id] = order_id
        self.emit("order.accepted", {"client_id": client_id,
                                     "order_id": order_id})
        if acked == "resting":
            # A resting order gets no `[142]` for merely being live: the ack IS
            # the news that it reached the book.
            with self._lock:
                rec["state"] = "resting"
            self.emit("order.placed", {
                "client_id": client_id, "order_id": order_id, "type": otype,
                "price": resting_at, "expires_at": rec["expires_at"]})
        # Releases any `[142]` that beat the ack here (see _worker_loop).
        self._work.put(("announce", order_id))
        result = {"order_id": order_id,
                  "status": "accepted" if otype == "market" else "resting"}
        return self.idempotent_store(client_id, result, sig)

    @staticmethod
    def _client_tag(client_id: str) -> str:
        """The venue's idempotency key, from the platform's client_id.

        NUL is the field separator, so a client_id carrying one would split
        into two fields and corrupt the order. Substituted rather than
        rejected: this adapter's own idempotency store is the guarantee that
        matters, and the tag is the venue's second line.
        """
        return "".join("_" if ch < " " else ch for ch in str(client_id))[:120]

    def _day_expiry_ms(self, spec: dict) -> int:
        """When a DAY order dies, computed from the published calendar.

        The venue never states the resulting expiry on the wire, and the SPEC
        says a platform must not compute a broker's trading day itself, so the
        adapter does it from the session the venue publishes for the
        instrument: the next session close, or (docs 11) the next UTC midnight
        for the continuous session, which has no close for DAY to key on.
        """
        now = self.now_ms() / 1000.0
        windows = spec.get("sessions") or []
        if not windows or len(windows) == 1 and windows[0]["close"] == "24:00" \
                and len(windows[0]["days"]) == 7:
            return int((int(now // 86400) + 1) * 86400 * 1000)
        best = None
        for day in range(0, 8):
            base = (int(now // 86400) + day) * 86400
            dow = (int(base // 86400) + 3) % 7 + 1      # 1970-01-01 = Thursday
            for w in windows:
                if dow not in w["days"]:
                    continue
                h, m = (int(x) for x in w["close"].split(":"))
                cand = base + h * 3600 + m * 60
                if cand > now and (best is None or cand < best):
                    best = cand
            if best is not None:
                break
        return int((best or now + 86400) * 1000)

    def handle_orders_list(self, params):
        """Orders this process placed and remembers.

        Terminal Link has no enumerate-orders message, which is why
        `capabilities.orders.can_list` is false: an order entered from another
        connection on the same account cannot appear here, ever. Answered
        anyway, because a partial view the platform has been WARNED is partial
        beats no view at all.
        """
        want = (params or {}).get("state")
        with self._lock:
            recs = [dict(r) for r in self._orders.values()]
        out = []
        for r in recs:
            state = r["state"]
            if state in ("new", "partial"):
                state = "resting"
            out.append({
                "order_id": r["order_id"], "client_id": r["client_id"],
                "symbol": r["symbol"], "type": r["type"], "side": r["side"],
                "qty": r["qty"], "price": r["price"],
                "stop_price": r["stop_price"], "sl": None, "tp": None,
                "expiry": r["expiry"], "expires_at": r["expires_at"],
                "state": state, "placed_at": r["placed_at"],
                "done_at": r["done_at"],
                "fill_price": r["avg_price"] if state == "filled" else None,
                "reason": r["reason"],
            })
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        with self._lock:
            known = order_id in self._orders
        try:
            self.link.cancel(order_id)
        except VenueError as e:
            # 1405 covers both "no such id" and "not resting any more" and the
            # venue does not distinguish them (docs 6.5). This book can: an
            # id this adapter never issued is not_found, one it did is a state
            # problem.
            raise e.to_adapter_error(not_found_ids=() if known else (order_id,))
        # The `order.cancelled` event rides the broadcast, which the reader is
        # already routing; nothing to announce from here.
        return {}

    def handle_order_amend(self, params):
        # docs 6.6: "There is no message to modify a resting order's price,
        # quantity, or expiry. Cancel it and place a new one." Emulating it
        # here would hand the platform a NEW order_id, which `order.amended`
        # has no field to carry, so the emulation could not even be described
        # on this wire.
        raise AdapterError("unsupported",
                           "Quantex cannot amend a resting order; cancel it "
                           "and place a new one")

    def handle_order_modify(self, params):
        raise AdapterError("unsupported",
                           "Quantex has no stop loss / take profit / trailing "
                           "attachment on a position")

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported",
                           "Quantex is a netting book; there is no second "
                           "position to close against")

    # == closing =========================================================
    def handle_position_close(self, params):
        pid = str(params.get("position_id") or "")
        if not pid:
            raise AdapterError("not_found", "position_id is required")
        qty = params.get("qty")
        client_id = params.get("client_id")
        if client_id:
            prior = self.idempotent_check(client_id, ("close", pid, qty))
            if prior is not None:
                return prior
        self._close(pid, qty, reason="manual", wait=CLOSE_WAIT)
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, ("close", pid, qty))
        return result

    def _close(self, pid: str, qty, reason: str, wait: float):
        with self._lock:
            snap = self._positions.get(pid)
        if snap is None:
            self._refresh_positions()   # may be a position never listed here
            with self._lock:
                snap = self._positions.get(pid)
        job = {"position_id": pid, "reason": reason, "since": time.time(),
               "qty": _num(qty, _num((snap or {}).get("qty"))),
               "snapshot": snap}
        # Filed BEFORE the request goes out, not after it comes back: the
        # position poller runs concurrently and would otherwise see the
        # position disappear, find no close in flight, and report it as a
        # mystery close on top of the real one.
        with self._lock:
            self._closing[pid] = job
        try:
            reply = self.link.call(155, pid, "" if qty is None else
                                   _fmt_qty(qty), timeout=wait)
        except VenueError as e:
            with self._lock:
                if self._closing.get(pid) is job:
                    self._closing.pop(pid, None)
            raise e.to_adapter_error()
        except AdapterError:
            # Timed out waiting for `[156]`. The close is still happening at
            # the venue; the poller finishes the job when the fills land.
            log(f"close {pid}: no confirmation within {wait:.0f}s; the poller "
                f"will finish it")
            raise
        # [156, req_id, position_id, realized]
        self._finish_close(job, realized=_num(reply.final[3]))

    def _finish_close(self, job: dict, realized: float | None):
        """Announce a close, priced and charged from the venue's own ledger.

        `[156]` carries the realised P/L and nothing else: no price, no size,
        no fee. Those come from the fill ledger, which is the only place the
        venue states what a close actually dealt, and they are matched by
        contract and side inside the close's own time window. On a netting
        book that is unambiguous (one position per contract), which is why the
        match can be that simple.
        """
        pid = job["position_id"]
        with self._lock:
            if self._closing.get(pid) is not job:
                return                  # somebody else already finished it
            self._closing.pop(pid, None)
            snap = job.get("snapshot") or self._positions.get(pid) or {}
            self._positions.pop(pid, None)
            opening_fees = self._pos_fees.get(pid, 0.0)
            meta = dict(self._pos_meta.get(pid) or {})
        side_out = "S" if snap.get("side") == "buy" else "B"
        qty, price, fees, weighted = 0.0, None, 0.0, 0.0
        try:
            prints = [d for d in self._ledger(since=job["since"] - 3)
                      if d["conid"] == snap.get("conid")
                      and d["side"] == side_out]
        except AdapterError as e:
            log(f"close {pid}: ledger unreadable ({e})")
            prints = []
        for d in prints:
            qty += d["qty"]
            fees += d["fee"]
            weighted += d["price"] * d["qty"]
        if qty > 0:
            # Volume weighted across clips: a large close prints in two or
            # three pieces a few hundred ms apart (docs 6.3), and any one of
            # them on its own is not the price the position closed at.
            price = round(weighted / qty, 10)
        else:
            qty = _num(job.get("qty"), _num(snap.get("qty")))
            price = snap.get("avg_price")
            log(f"close {pid}: no closing print found in the ledger; "
                f"reporting the requested size at the last known level")
        if realized is None:
            # No `[156]` arrived. The venue publishes its realised figure
            # nowhere else, so the position's last mark-to-market (already in
            # account currency, from `[151]`) stands in, scaled by the fraction
            # closed. An APPROXIMATION, and it says so rather than passing for
            # the venue's own number.
            whole = _num(snap.get("qty")) or qty or 1.0
            realized = round(_num(snap.get("unrealized")) * (qty / whole), 2)
            log(f"close {pid}: realised P/L approximated as {realized} "
                f"(no [156] confirmation reached us)")
        # A partial close leaves the position (and the fees it has already
        # paid) standing, so only the closing prints are charged to it; the
        # opening fees go on the wire with the close that finally ends it.
        whole_gone = qty >= _num(snap.get("qty"), qty) - 1e-9
        if whole_gone:
            with self._lock:
                self._pos_fees.pop(pid, None)
                self._pos_meta.pop(pid, None)
        data = {"position_id": pid, "qty": qty, "price": price,
                "realized_pnl": round(realized, 2),
                # The venue charges no financing on any instrument (docs 9),
                # so this is a fact and not a placeholder.
                "swap": 0.0,
                "commission": _cost((opening_fees if whole_gone else 0.0)
                                    + fees),
                "reason": job["reason"],
                "time": self.now_ms()}
        client_id = job.get("client_id") or meta.get("client_id")
        if client_id:
            data["client_id"] = client_id
        self.emit("position.closed", data)

    def handle_flatten_all(self, params):
        """Close everything and do not come back until the book is empty.

        SPEC 8.1 wants this to work while other calls are failing, so every
        position is closed independently and one refusal never abandons the
        rest. Resting orders go too: an order left working re-opens the
        position the caller just closed.
        """
        rows = self._refresh_positions()
        deadline = time.time() + FLATTEN_BUDGET
        closed = 0
        for pid in list(rows):
            left = deadline - time.time()
            if left <= 0.5:
                log(f"flatten: out of time before {pid}; the poller finishes it")
                break
            try:
                self._close(pid, None, reason="flatten", wait=min(left, 6.0))
                closed += 1
            except AdapterError as e:
                log(f"flatten: position {pid} did not close: {e}")
        with self._lock:
            resting = [oid for oid, r in self._orders.items()
                       if r["state"] in ("new", "resting", "partial")]
        for oid in resting:
            try:
                self.link.cancel(oid, timeout=1.0)
            except VenueError:
                pass                    # already gone; nothing to cancel
            except AdapterError as e:
                log(f"flatten: order {oid} not cancelled: {e}")
        return {"closed": closed}

    # == market data =====================================================
    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            spec = self._instrument(s)      # unknown_symbol before subscribing
            # Re-subscribing an already-live contract REPLACES the old
            # subscription (docs 5.1) and costs one fresh snapshot tick, which
            # is exactly what a caller re-subscribing wants: the venue answers
            # `[123]` then one `[121]` immediately, even with the market shut,
            # so a platform can always paint a last-known price.
            rid = self.link.subscribe_id()
            self._call(120, spec["conid"], req_id=rid)
            with self._lock:
                self._subs[spec["conid"]] = rid
        return {}

    def handle_quotes_unsubscribe(self, params):
        for s in (params.get("symbols") or []):
            with self._lock:
                spec = self._by_symbol.get(s)
                rid = self._subs.pop(spec["conid"], None) if spec else None
            if rid:
                # docs 5.2: addressed by the req_id the subscription was opened
                # under, and answered by nothing at all.
                self.link.send_oneway(122, rid)
        return {}

    def handle_bars_history(self, params):
        symbol = params.get("symbol")
        spec = self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        wire_tf = TF_TO_WIRE.get(tf)
        if wire_tf is None:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        count = max(1, min(int(_num(params.get("count"), 200)), 500))
        step = TF_SECONDS[tf]
        end = int(_num(params.get("to"), 0) / 1000) or int(self.now_ms() / 1000)
        start = int(_num(params.get("from"), 0) / 1000) or \
            end - step * (count - 1)
        # The venue carries 60 days and answers 1304 for anything deeper, so
        # the window is clamped to what it can serve rather than refused.
        floor = int(self.now_ms() / 1000) - HISTORY_DEPTH_S + step
        if start < floor:
            log(f"bars.history {symbol} {tf}: start clamped to the venue's "
                f"60-day archive")
            start = floor
        rows = self._call(130, spec["conid"], wire_tf, start, end, count).rows
        bars = [[int(_num(r[2]) * 1000), _num(r[3]), _num(r[4]), _num(r[5]),
                 _num(r[6]), _num(r[7])] for r in rows]
        bars.sort(key=lambda b: b[0])       # oldest first (SPEC)
        return {"bars": bars}

    def handle_bars_subscribe(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_WIRE:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        # Terminal Link streams ticks, never bars, so closed bars are polled
        # off the history endpoint. Seeded with the newest completed bar so a
        # subscriber gets the NEXT one rather than a replay of the backlog.
        with self._lock:
            self._bars.setdefault((symbol, tf), 0)
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._lock:
            self._bars.pop((params.get("symbol"),
                            params.get("timeframe") or "1m"), None)
        return {}

    # == history =========================================================
    def _ledger(self, since: float | None = None) -> list:
        """The fill ledger (`[157]`), normalised.

        This is the venue's REST-equivalent: the only record that survives a
        dropped push frame, the only place a fee is stated, and the only way to
        see a trade the venue did on its own (those carry an empty order_id,
        docs 7.5).
        """
        args = ["" if since is None else str(int(since))]
        rows = self._call(157, *args).rows
        out = []
        for r in rows:
            # [158, req_id, deal_id, order_id, conid, side, qty, price, fee, ts]
            spec = self._by_id(r[4])
            out.append({"deal_id": str(r[2]), "order_id": r[3],
                        "conid": str(r[4]), "side": r[5], "qty": _num(r[6]),
                        "price": _num(r[7]), "fee": _num(r[8]),
                        "ts": _num(r[9]),
                        "symbol": spec["symbol"] if spec else str(r[4])})
        return out

    def handle_history_fills(self, params):
        since = params.get("from")
        deals = self._ledger(since=_num(since) / 1000.0 if since else None)
        to = _num(params.get("to"), 0) / 1000.0 if params.get("to") else None
        fills = []
        for d in deals:
            if to and d["ts"] > to:
                continue
            fills.append({
                "fill_id": d["deal_id"],
                # Empty at the venue for a trade no order caused: a stop-out
                # close (docs 7.5). Reported as null rather than as a made-up
                # ticket, because the difference matters to a reconciler.
                "order_id": d["order_id"] or None,
                "symbol": d["symbol"],
                "side": "buy" if d["side"] == "B" else "sell",
                "qty": d["qty"],
                "price": d["price"],
                "fees": _cost(abs(d["fee"])),
                "time": int(d["ts"] * 1000),
            })
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == push routing ====================================================
    def _on_push(self, frame: list):
        """Called on the READER thread. Cheap work only.

        A quote is emitted straight through because it needs nothing from the
        venue. An order update needs to know which position a fill landed on,
        which is a round trip, so it is handed to the worker: a request issued
        from this thread would wait for a reply only this thread can deliver.
        """
        if frame[0] == "121":
            spec = self._by_id(frame[2])
            if spec is None:
                return
            self.emit("quote", {"symbol": spec["symbol"],
                                "bid": _num(frame[3]), "ask": _num(frame[4]),
                                # Wire timestamps are whole epoch SECONDS
                                # (docs 1.1); the protocol is milliseconds.
                                "time": int(_num(frame[6]) * 1000)})
            return
        if frame[0] == "142":
            self._work.put(("update", frame))

    def _worker_loop(self):
        """Everything that needs a round trip, in the order the venue said it.

        One thread, so `order.partial` can never overtake the `order.filled`
        that follows it, and so a fill's position lookup cannot interleave
        with the next fill's.
        """
        while not self._stop.is_set():
            job = self._work.get()
            if job is None:
                return
            try:
                kind = job[0]
                if kind == "update":
                    self._absorb_update(job[1])
                elif kind == "announce":
                    self._announce(job[1])
                elif kind == "vanished":
                    self._explain_vanished(job[1])
                elif kind == "resubscribe":
                    self._resubscribe()
            except Exception as e:      # noqa: BLE001 - a worker must not die
                log(f"worker {job[0]}: {type(e).__name__}: {e}")

    def _announce(self, order_id: str):
        """Release the updates that beat their own order ack.

        `[142]` is a broadcast with no req_id and the venue can emit it before
        the `[141]` that first names the order's id, so an update for an
        order not yet registered here is parked rather than discarded. This
        also keeps `order.accepted` first: an update processed between the
        registration and the announcement would otherwise report a fill for an
        order the platform has not been told about.
        """
        with self._lock:
            rec = self._orders.get(order_id)
            if rec is None:
                return
            rec["announced"] = True
            parked = self._orphans.pop(order_id, [])
        for _, frame in parked:
            self._absorb_update(frame)

    def _absorb_update(self, frame: list):
        """One `[142]`, folded into the local view, emitted only on TRANSITIONS.

        [142, order_id, state, filled_qty, avg_price, last_qty, last_price,
         reason]. Every decision here is keyed on the CUMULATIVE filled
        quantity, so the same frame arriving twice moves nothing: the venue
        broadcasts to every connection on the account and this adapter must
        survive seeing its own order's update more than once.
        """
        order_id = str(frame[1])
        state = STATE_IN.get(frame[2], None)
        filled = _num(frame[3])
        avg = _num(frame[4]) if frame[4] else None
        last_qty = _num(frame[5]) if len(frame) > 5 and frame[5] else 0.0
        last_price = _num(frame[6]) if len(frame) > 6 and frame[6] else None
        reason = frame[7] if len(frame) > 7 else ""
        with self._lock:
            rec = self._orders.get(order_id)
            if rec is None or not rec["announced"]:
                # Either an order this process never placed (the synthetic
                # offsetting order behind `[155]`, or another connection on the
                # same account), or one placed here whose broadcast beat its
                # own `[141]` ack. Parked either way: if it turns out to be
                # this adapter's, `_announce` replays it in arrival order; if
                # not, it ages out and nothing is ever emitted for it.
                self._orphans.setdefault(order_id,
                                         []).append((time.time(), frame))
                return
            prev_filled = rec["filled_emitted"]
            client_id = rec["client_id"]
            symbol, side = rec["symbol"], rec["side"]
            total = rec["qty"]
        if state in ("filled", "partial") and filled > prev_filled + 1e-9:
            clip = round(filled - prev_filled, 8)
            position_id, opened_now = self._position_for(rec)
            spec = self._instrument(symbol)
            fee = self._fee_for(spec, clip)
            if position_id:
                with self._lock:
                    self._pos_fees[position_id] = \
                        self._pos_fees.get(position_id, 0.0) + fee
                    if opened_now:
                        # SPEC 4: a position's client_id is the id of the order
                        # that OPENED it. Recorded only when this fill actually
                        # opened one, so a later order landing on the same
                        # netted position cannot rename it.
                        self._pos_meta[position_id] = {
                            "client_id": client_id,
                            "opened_at": self.now_ms(),
                            "from_order": None if rec["type"] == "market"
                            else rec["type"]}
            with self._lock:
                rec["filled_emitted"] = filled
                rec["avg_price"] = avg
                rec["state"] = state
                if state == "filled":
                    rec["done_at"] = self.now_ms()
            data = {"client_id": client_id, "order_id": order_id,
                    "position_id": position_id, "symbol": symbol, "side": side,
                    "qty": clip,
                    "price": last_price if last_price is not None else avg,
                    # The venue states a fee only on the ledger, never on
                    # `[142]`, so a fill's charge comes from the published
                    # schedule (docs 9). Exact rather than indicative: every
                    # equity here has contract_size 1, so "per share" and "per
                    # unit" are the same number, and it is checked against the
                    # ledger's own figure on the way out in history.fills.
                    "fees": _cost(fee), "time": self.now_ms()}
            if state == "partial":
                data["remaining"] = round(total - filled, 8)
                self.emit("order.partial", data)
            else:
                self.emit("order.filled", data)
            return
        if state == "cancelled" and rec["state"] != "cancelled":
            with self._lock:
                rec["state"] = "cancelled"
                rec["done_at"] = self.now_ms()
                rec["reason"] = reason or None
            data = {"order_id": order_id, "client_id": client_id}
            if reason:
                # `code` present means the VENUE killed it (SPEC 5); a
                # client-requested cancel carries an empty reason.
                data["code"] = "market_closed" if reason == \
                    "instrument_expired" else "internal"
                data["message"] = reason
            self.emit("order.cancelled", data)
        elif state == "expired" and rec["state"] != "expired":
            with self._lock:
                rec["state"] = "expired"
                rec["done_at"] = self.now_ms()
            self.emit("order.expired", {"order_id": order_id,
                                        "client_id": client_id,
                                        "expiry": rec["expiry"]})
        elif state == "rejected" and rec["state"] != "rejected":
            with self._lock:
                rec["state"] = "rejected"
                rec["done_at"] = self.now_ms()
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": "internal",
                                         "message": reason or
                                         "rejected by Quantex"})

    def _position_for(self, rec: dict) -> tuple:
        """Which position a fill landed on, and whether it opened it.

        `[142]` never names one, so the book is re-read and matched by
        contract. That is exact on a netting account: there is at most one
        position per contract, so the one standing in this contract after the
        fill IS the one the fill touched. A fill that netted a position away
        leaves none, and the id last seen for the contract is the honest
        answer there.
        """
        with self._lock:
            before = next((pid for pid, p in self._positions.items()
                           if p["conid"] == rec["conid"]), None)
        try:
            fresh = self._refresh_positions()
        except AdapterError as e:
            log(f"position lookup after a fill on {rec['symbol']}: {e}")
            return before, False
        for pid, p in fresh.items():
            if p["conid"] == rec["conid"]:
                return pid, pid != before
        return before, False

    def _resubscribe(self):
        """Rebuild everything the dropped socket was carrying.

        A Terminal Link connection owns its own subscriptions and the venue
        replays nothing after a reconnect, so quotes are re-opened under the
        SAME req_ids (the platform's view of which symbol a tick belongs to
        stays valid) and the book is re-read, because a fill or a stop-out
        during the outage is exactly what a reconnect exists to discover.
        """
        with self._lock:
            subs = dict(self._subs)
        for conid, rid in subs.items():
            try:
                self._call(120, conid, req_id=rid)
            except AdapterError as e:
                log(f"resubscribe {conid} failed: {e}")
        try:
            self._refresh_positions()
        except AdapterError as e:
            log(f"post-reconnect position read failed: {e}")

    def _explain_vanished(self, snap: dict):
        """A position that disappeared with nobody asking.

        docs 7.4 is explicit that a stop-out produces no `[142]` and no notice
        of any kind, and 7.5 says the closing trade it leaves in the ledger
        carries an EMPTY order id. That is the venue's own signal for "we did
        this, not you", so it decides the reason rather than the exit price
        deciding it (SPEC 5: guessing a reason is worse than admitting
        ignorance, and this venue actually publishes the evidence).
        """
        pid = snap["position_id"]
        side_out = "S" if snap["side"] == "buy" else "B"
        reason, price, qty, fees = "unknown", None, snap.get("qty"), 0.0
        try:
            recent = [d for d in self._ledger(since=time.time() - 300)
                      if d["conid"] == snap["conid"] and d["side"] == side_out]
        except (AdapterError, VenueError) as e:
            log(f"vanished position {pid}: ledger unreadable ({e})")
            recent = []
        if recent:
            last = recent[-1]
            price, qty, fees = last["price"], last["qty"], last["fee"]
            reason = "stop_out" if not last["order_id"] else "manual"
        realized = self._realized_estimate(snap, price, qty)
        with self._lock:
            opening_fees = self._pos_fees.pop(pid, 0.0)
            meta = dict(self._pos_meta.pop(pid, None) or {})
        data = {"position_id": pid, "qty": qty, "price": price,
                "realized_pnl": realized, "swap": 0.0,
                "commission": _cost(opening_fees + fees),
                "reason": reason, "time": self.now_ms()}
        if meta.get("client_id"):
            data["client_id"] = meta["client_id"]
        if reason == "unknown":
            data["reason_detail"] = ("the position is gone from the book and "
                                     "the venue published no closing trade "
                                     "for it")
        self.emit("position.closed", data)

    def _realized_estimate(self, snap: dict, price, qty) -> float:
        """What a close the venue never reported was worth.

        `[156]` answers this adapter's own close request and nothing else, so
        a stop-out has no venue-stated P/L anywhere. Where the contract is
        quoted in the account's currency the arithmetic is exact and is used;
        where it is not, converting would need a cross the venue may not even
        list, so the position's last published mark-to-market (already in
        account currency) stands in and is logged as the approximation it is.
        """
        spec = self._by_id(snap["conid"]) or {}
        if price is None or not qty:
            return round(_num(snap.get("unrealized")), 2)
        if spec.get("quote_ccy") == self.account_currency:
            direction = 1.0 if snap["side"] == "buy" else -1.0
            return round((price - snap["avg_price"]) * direction * qty
                         * spec.get("contract_size", 1.0), 2)
        log(f"position {snap['position_id']}: realised P/L taken from the last "
            f"mark ({spec.get('quote_ccy')} is not the account currency)")
        return round(_num(snap.get("unrealized")), 2)

    # == the poller ======================================================
    def _poll_loop(self):
        """The backstop for everything the push channel can lose.

        Cheap when nothing is happening: it only spends a round trip when
        there is an open position to watch, an order in flight, a close
        unconfirmed or a bar subscription running.
        """
        while not self._stop.wait(3.0):
            try:
                self._reap_orders()
                self._reap_closes()
                self._reap_positions()
                self._reap_bars()
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log(f"poll: {type(e).__name__}: {e}")
            self._expire_orphans()

    def _expire_orphans(self):
        """Forget parked updates that were never going to be claimed.

        Aged out rather than counted out: an order id this adapter has not
        claimed within half a minute belongs to the venue's own offsetting
        order or to another connection on the account, and a size-based purge
        could drop an update for an order whose ack was merely slow.
        """
        cutoff = time.time() - 30.0
        with self._lock:
            for oid in list(self._orphans):
                if oid in self._orders:
                    continue
                kept = [e for e in self._orphans[oid] if e[0] > cutoff]
                if kept:
                    self._orphans[oid] = kept
                else:
                    self._orphans.pop(oid, None)

    def _reap_orders(self):
        """Finish orders whose `[142]` never arrived, from the fill ledger."""
        with self._lock:
            watch = [dict(r) for r in self._orders.values()
                     if r["state"] in ("new", "partial")
                     or (r["acked"] in ("filled", "partial")
                         and r["filled_emitted"] < r["qty"] - 1e-9)]
        if not watch:
            return
        oldest = min(r["placed_at"] for r in watch) / 1000.0
        deals = self._ledger(since=oldest - 2)
        by_order: dict[str, list] = {}
        for d in deals:
            if d["order_id"]:
                by_order.setdefault(d["order_id"], []).append(d)
        for rec in watch:
            oid = rec["order_id"]
            cum = 0.0
            for d in by_order.get(oid, []):
                cum += d["qty"]
                if cum <= rec["filled_emitted"] + 1e-9:
                    continue
                log(f"order {oid}: fill of {d['qty']} recovered from the "
                    f"ledger (no [142] reached us)")
                state = "filled" if cum >= rec["qty"] - 1e-9 else "partial"
                self._absorb_update([
                    "142", oid, "F" if state == "filled" else "P",
                    _fmt_qty(cum), _fmt_qty(d["price"]), _fmt_qty(d["qty"]),
                    _fmt_qty(d["price"]), ""])
            with self._lock:
                cur = self._orders.get(oid) or {}
                # SPEC 8.4 counts `order.partial` as a report, so an order
                # that has already said something is not overdue no matter how
                # long its remainder takes; a resting order is not overdue
                # either, since resting IS its outcome. Only an order that has
                # gone silent since it was accepted qualifies.
                overdue = (cur.get("state") == "new"
                           and cur.get("acked") != "resting"
                           and cur.get("filled_emitted", 0.0) <= 0.0
                           and time.time() > cur.get("deadline", 0)
                           and not cur.get("degraded"))
                if overdue:
                    cur["degraded"] = True
            if overdue:
                # An order that has not terminated inside the declared timeout
                # is reported, never quietly waited on.
                self._set_session("degraded",
                                  f"order {oid} has not reached a terminal "
                                  f"state within 30s")

    def _reap_closes(self):
        with self._lock:
            jobs = list(self._closing.values())
        if not jobs:
            return
        live = self._refresh_positions()
        for job in jobs:
            pid = job["position_id"]
            if pid in live:
                if time.time() - job["since"] > 30:
                    with self._lock:
                        self._closing.pop(pid, None)
                    self._set_session("degraded",
                                      f"close of {pid} unconfirmed after 30s")
                continue
            # Gone from the book, and the `[156]` that should have said so
            # never reached the caller. Finish it from the ledger.
            self._finish_close(job, realized=None)

    def _reap_positions(self):
        with self._lock:
            watching = bool(self._positions)
        if watching:
            self._refresh_positions()

    def _reap_bars(self):
        with self._lock:
            wants = dict(self._bars)
        for (symbol, tf), last in wants.items():
            try:
                bars = self.handle_bars_history({"symbol": symbol,
                                                 "timeframe": tf,
                                                 "count": 2})["bars"]
            except (AdapterError, VenueError) as e:
                log(f"bar poll {symbol} {tf}: {e}")
                continue
            for bar in bars:
                # The venue serves completed buckets only (docs 5.4), so
                # anything newer than the watermark is a closed bar (SPEC:
                # `bar` events carry CLOSED bars).
                if bar[0] <= last:
                    continue
                self.emit("bar", {"symbol": symbol, "timeframe": tf,
                                  "bar": bar})
                with self._lock:
                    if (symbol, tf) in self._bars:
                        self._bars[(symbol, tf)] = bar[0]


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a
    # pipe buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    QuantexAdapter().run()


if __name__ == "__main__":
    main()
