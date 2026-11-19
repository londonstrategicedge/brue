#!/usr/bin/env python3
"""Brue Connect adapter for Northgate Securities (institutional FIX drop).

Northgate is a London FX and bullion desk that exposes ONE thing: a FIX 4.4
session on a raw TCP socket. There is no HTTP anywhere, no REST backstop, no
security list, no account inquiry and no positions request; the rules of
engagement are explicit that this is deliberate ("position keeping is the
client's responsibility ... exactly as it would reconcile against a real prime
broker's drop copy"). Everything the platform is entitled to therefore has to
be built here, out of ExecutionReports.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no socket, no Logon, no subscription in
    the startup path. All of it happens in `on_first_call`, so a connection
    picker can learn the broker's name without opening a dealing session on
    it.
  * the session layer is hand-written on a plain socket, because FIX is the
    wire and stdlib-only is the rule: BodyLength and CheckSum exactly as FIX
    defines them, an outbound MsgSeqNum series per connection, one reader
    thread dispatching by MsgType(35), and a keepalive thread that heartbeats,
    answers TestRequest(1) and declares the session dead when the venue goes
    quiet for 2.5 heartbeat intervals. There is no store-and-resend on this
    connection (141=Y is mandatory on EVERY Logon), so a reconnect is a brand
    new session at MsgSeqNum 1 that re-subscribes market data and says so with
    `session.state` rather than pretending the book stood still.
  * THE BOOK IS KEPT HERE. Positions, balance, equity, margin and the fill
    ledger are maintained from the ExecutionReport stream and persisted to the
    adapter's state directory, because no request on this connection can ask
    the venue for any of them. Every number `account.get` and `positions.list`
    report is derived, and the one number that cannot be derived (the opening
    balance) is the figure the rules of engagement publish.
  * the stream is treated as AT LEAST ONCE even though this venue is orderly
    about it: fills are applied as the DELTA of CumQty(14), never as LastQty
    alone, so a repeated report restates a quantity already in the book and
    changes nothing. That also means a report lost to a reconnect is repaired
    by the next one instead of leaving the book short.
  * capabilities are declared to what the wire can actually do. No brackets,
    no modify, no trailing, no stop types, no bar history, no operations, and
    `can_list: false` because nothing on this connection enumerates orders:
    the adapter can only report the orders it placed and remembered, and the
    SPEC would rather a platform be told its order view is PARTIAL than let it
    read an empty list as "nothing resting".
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import sys
import threading
import time

# Importable both as part of the installed package and as the bare script the
# platform spawns (`python3 northgate_adapter.py`), where nothing has put the
# repo on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

SOH = "\x01"
BEGIN_STRING = "FIX.4.4"

# The one thing the platform configures. "host:port"; the venue binds
# 127.0.0.1 on its own host, so reaching it from another machine takes a
# port forward (SSH or similar).
ADDR = os.environ.get("NORTHGATE_ADDR", "127.0.0.1:7601")

# Credentials come from the environment (SPEC 8.5: env or the adapter's
# private state, never a protocol message; nothing below is ever echoed into a
# `hello`, an event or an error). A counterparty sets them from its onboarding
# pack without editing this file. Only the venue's CompID has a default; the
# session cannot log on until the other four are set.
SENDER_COMP_ID = os.environ.get("NORTHGATE_SENDER_COMP_ID", "")
TARGET_COMP_ID = os.environ.get("NORTHGATE_TARGET_COMP_ID", "NORTHGATE")
USERNAME = os.environ.get("NORTHGATE_USERNAME", "")
PASSWORD = os.environ.get("NORTHGATE_PASSWORD", "")
ACCOUNT = os.environ.get("NORTHGATE_ACCOUNT", "")

# 10 to 60 is the range the rules of engagement accept. 30 leaves the venue's
# "no traffic for 1.5 intervals" TestRequest a wide margin over any pause this
# adapter takes, and its "answer within one more interval" a wide margin over
# a reader thread that is briefly busy applying a burst of fills.
HEARTBEAT = 30

# The account's opening figure, published in section 8 of the rules of
# engagement. It is a CONSTANT because the venue offers no way to read the
# balance: see `_load_state` for what this means when another session has
# already traded the account.
OPENING_BALANCE = float(os.environ.get("NORTHGATE_OPENING_BALANCE", "1000000"))

# How long `order.place` waits for the venue's first ExecutionReport before it
# stops claiming the order is in hand. Under the platform's own 10 s request
# deadline on purpose: an answer of "I do not know yet" inside the deadline is
# worth more than a timeout with nothing in it, and the order record survives
# so a late fill still reaches the platform as an event.
ACK_TIMEOUT = 8.0
# How long a close waits for its fill before returning anyway. `flatten_all`
# must leave the book visibly empty when it returns (SPEC 8.1 and every
# platform that lists positions the instant it answers), and this venue fills
# a market clip in single-digit milliseconds, so this is a ceiling, not a wait.
FILL_TIMEOUT = 6.0
# SPEC 8.4: an order that has not reached a terminal state inside this is
# reported as `session.state: degraded`, never quietly waited on.
ORDER_DEADLINE = 30.0

# Northgate's universe, section 5 of the rules of engagement. This venue has
# NO security list message: SecurityListRequest is not implemented and would
# come back as a BusinessMessageReject, so the document is the only place the
# universe is published and there is nothing to download at runtime. What the
# table below is NOT is a list of symbols this adapter trusts: every entry is
# VERIFIED against the venue on connect (see `_probe_catalog`) by subscribing
# to it and keeping only the symbols the venue answers with a snapshot, so a
# symbol the desk stops listing disappears from `catalog.list` and raises
# `catalog.changed` rather than being offered to a platform forever.
# Order is the document's own, which also decides which instrument a platform
# reaches for first; FX heads the list, as it does on the desk.
INSTRUMENTS = [
    # symbol, name, class, base, digits, step, min, max, contract, margin, hours
    ("EUR/USD", "Euro / US Dollar", "fx", "EUR", "USD", 5,
     0.01, 0.01, 500, 100000, 0.0333, "fx"),
    ("GBP/USD", "Pound Sterling / US Dollar", "fx", "GBP", "USD", 5,
     0.01, 0.01, 500, 100000, 0.0333, "fx"),
    ("USD/JPY", "US Dollar / Japanese Yen", "fx", "USD", "JPY", 3,
     0.01, 0.01, 500, 100000, 0.0333, "fx"),
    ("AUD/USD", "Australian Dollar / US Dollar", "fx", "AUD", "USD", 5,
     0.01, 0.01, 500, 100000, 0.0333, "fx"),
    ("XAU/USD", "Gold Spot", "metal", "XAU", "USD", 2,
     0.01, 0.01, 100, 100, 0.05, "fx"),
    ("XAG/USD", "Silver Spot", "metal", "XAG", "USD", 3,
     0.01, 0.01, 500, 5000, 0.05, "fx"),
    # The Special Futures are cash-settled PERPETUALS on US equity reference
    # prices: no expiry date, no financing, and they deal through the weekend.
    # `expires_at: null` is what says the first part (SPEC 1.3's field for
    # exactly this), and the 24/7 session below says the second.
    ("ALPH.SF", "AlphaTech Systems Special Future", "future", None, "USD", 2,
     1.0, 1.0, 15000, 10, 0.10, "always"),
    ("USEQ.SF", "US Equity Composite Special Future", "future", None, "USD", 1,
     0.1, 0.1, 500, 25, 0.05, "always"),
]

# "Sunday 21:05 UTC to Friday 21:00 UTC", as three windows a platform can gate
# on. SPEC 3 is emphatic that an EMPTY session list means "no calendar
# published" rather than "closed", so the always-open products get one
# explicit seven-day window rather than nothing: always-open and unknown look
# identical otherwise, and only one of them is safe to gate orders on.
SESSIONS_24_5 = [
    {"days": [7], "open": "21:05", "close": "24:00", "tz": "UTC"},
    {"days": [1, 2, 3, 4], "open": "00:00", "close": "24:00", "tz": "UTC"},
    {"days": [5], "open": "00:00", "close": "21:00", "tz": "UTC"},
]
SESSIONS_24_7 = [
    {"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00",
     "tz": "UTC"},
]

# OrdRejReason(103) plus the venue's Text(58), to the SPEC section 6 closed
# set.
# Keyed on the PAIR because Northgate deliberately reuses 103=3 for two
# different conditions ("an order quantity above the instrument's maximum is
# rejected with OrdRejReason=3, the same code used for insufficient margin"),
# and 103=99 is the catch-all for six more. The text is the only thing that
# tells them apart, so it is what this table switches on.
REJECT_MAP = {
    ("1", None): ("unknown_symbol", False),
    ("3", "order exceeds limit"): ("invalid_qty", False),
    ("3", "insufficient margin"): ("insufficient_margin", False),
    ("3", None): ("insufficient_margin", False),
    ("6", None): ("duplicate_client_id", False),
    ("99", "market closed"): ("market_closed", False),
    ("99", "quantity outside limits"): ("invalid_qty", False),
    ("99", "invalid quantity"): ("invalid_qty", False),
    ("99", "order type not supported"): ("unsupported", False),
    ("99", "unsupported timeinforce"): ("invalid_expiry", False),
    ("99", "invalid expire time"): ("invalid_expiry", False),
    ("99", "instrument disabled"): ("trade_restricted", False),
    ("99", "unknown account"): ("unauthorized", False),
}


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[northgate] {msg}", file=sys.stderr, flush=True)


def _signature(*parts) -> str:
    """A replay fingerprint that survives being written to disk.

    A STRING, not the tuple it reads more naturally as: the idempotency store
    is persisted (this venue has no history to reconstruct it from), and JSON
    turns a tuple into a list, so a tuple signature compares unequal to itself
    after a restart and turns every honest replay into `duplicate_client_id`.
    """
    return json.dumps([None if p is None else str(p) for p in parts])


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fix_now() -> str:
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y%m%d-%H:%M:%S.") + "%03d" % (dt.microsecond // 1000)


def _fix_ts(ms: int) -> str:
    """Epoch ms to the UTC stamp ExpireTime(126) wants (seconds, no millis)."""
    t = time.gmtime(int(ms) // 1000)
    return time.strftime("%Y%m%d-%H:%M:%S", t)


def _ms(stamp) -> int:
    """A FIX UTCTimestamp to epoch milliseconds (SPEC 2).

    Parsed by hand rather than with strptime because the millisecond part is
    optional on this wire and strptime's %f is not, and because a timestamp
    that will not parse must cost a fill nothing: 0 tells the caller to use
    its own clock instead of raising inside a fill.
    """
    s = str(stamp or "")
    if len(s) < 17 or s[8] != "-":
        return 0
    try:
        y, mo, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
        h, mi, sec = int(s[9:11]), int(s[12:14]), int(s[15:17])
        frac = int((s[18:21] or "0").ljust(3, "0")) if len(s) > 18 else 0
    except ValueError:
        return 0
    return (_days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60
            + sec) * 1000 + frac


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days since 1970-01-01 (Howard Hinnant's civil_from_days, inverted).

    No timezone database and no DST: FIX is UTC-only and so is the protocol,
    so the whole conversion is integer arithmetic.
    """
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def encode(msg_type: str, seq: int, sender: str, target: str,
           fields: list) -> bytes:
    """One FIX message, framed exactly as the venue's own codec frames it.

    BodyLength(9) counts every byte from the start of tag 35 to the byte
    before tag 10, and CheckSum(10) is the sum of every byte up to that same
    point modulo 256 in exactly three digits. Both are computed on the ENCODED
    bytes rather than on character counts: a non-ASCII byte anywhere in a
    field (a name, a rejection text echoed back) would otherwise silently
    produce a message the venue drops as malformed.
    """
    body = "35=%s%s49=%s%s56=%s%s34=%d%s52=%s%s" % (
        msg_type, SOH, sender, SOH, target, SOH, seq, SOH, _fix_now(), SOH)
    for tag, val in fields:
        body += "%s=%s%s" % (tag, val, SOH)
    body_b = body.encode("utf-8")
    head = ("8=%s%s9=%d%s" % (BEGIN_STRING, SOH, len(body_b), SOH)).encode()
    pre = head + body_b
    return pre + ("10=%03d%s" % (sum(pre) % 256, SOH)).encode()


def split_pairs(raw: bytes) -> list:
    """Decoded message as ORDERED (tag, value) pairs.

    A dict cannot hold a FIX message: MarketDataSnapshotFullRefresh repeats
    269/270/271 once per book side and the incremental repeats 55 as well, so
    both order and duplicates have to survive decoding. Callers that want a
    flat view of a non-repeating message ask `first_of` for it.
    """
    out = []
    for part in raw.decode("utf-8", "replace").split(SOH):
        if not part:
            continue
        tag, _, val = part.partition("=")
        out.append((tag, val))
    return out


def first_of(pairs: list) -> dict:
    """Flat view, first occurrence wins. Safe only for messages with no
    repeating group, which is every message this adapter switches on."""
    d = {}
    for tag, val in pairs:
        if tag not in d:
            d[tag] = val
    return d


class SessionDown(Exception):
    """The FIX session is not usable right now."""


class FixSession:
    """The session layer: one TCP socket, one Logon, and the reconnect.

    Kept away from the trading logic because everything in here is answerable
    without knowing what an order is: framing, sequence numbers, heartbeats,
    and the fact that this connection has no store-and-resend, so recovery is
    a fresh session rather than a replay. The adapter hands it three callbacks
    and never touches a socket.
    """

    def __init__(self, addr: str, on_message, on_state, on_reconnect):
        host, _, port = addr.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or "7601")
        self.on_message = on_message
        self.on_state = on_state
        self.on_reconnect = on_reconnect
        self.hbi = HEARTBEAT
        self.connected = False
        self._sock = None
        self._buf = b""
        self._seq_out = 0
        self._expected_in = 1
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_tx = 0.0
        self._last_rx = 0.0

    # -- lifecycle -----------------------------------------------------
    def start(self):
        """Open and log on SYNCHRONOUSLY, then supervise.

        The first connection is deliberately not delegated to the supervisor:
        a wrong password or an unreachable venue must fail the request that
        triggered `on_first_call` with a typed error, not disappear into a
        reconnect loop while the platform waits.
        """
        self._open()
        threading.Thread(target=self._supervise, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._lock:
            sock, self._sock = self._sock, None
            self.connected = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def logout(self):
        """Client-initiated Logout(5). The venue confirms and closes; the
        confirmation is not waited for because `bye` must not hang on a venue
        that has already gone."""
        try:
            self.send("5", [])
        except SessionDown:
            pass

    # -- the wire ------------------------------------------------------
    def send(self, msg_type: str, fields: list):
        with self._lock:
            sock = self._sock
            if sock is None or not self.connected:
                raise SessionDown("no FIX session")
            self._seq_out += 1
            raw = encode(msg_type, self._seq_out, SENDER_COMP_ID,
                         TARGET_COMP_ID, fields)
            try:
                sock.sendall(raw)
            except OSError as e:
                self.connected = False
                raise SessionDown(str(e)) from e
            self._last_tx = time.monotonic()

    def _next_frame(self, timeout):
        """One complete FIX message off the socket, or None when it closes.

        Framing is length-prefixed (BodyLength) rather than delimiter-based,
        so a message carrying an SOH inside a text field still frames
        correctly; leading garbage is skipped to the next BeginString rather
        than blocking the session forever.
        """
        prefix = ("8=%s%s9=" % (BEGIN_STRING, SOH)).encode()
        while True:
            buf = self._buf
            if not buf.startswith(prefix):
                idx = buf.find(prefix)
                if idx > 0:
                    self._buf = buf = buf[idx:]
                elif idx == -1 and buf:
                    self._buf = buf = b""
            if buf.startswith(prefix):
                end = buf.find(b"\x01", len(prefix))
                if end != -1:
                    try:
                        body_len = int(buf[len(prefix):end])
                    except ValueError:
                        self._buf = buf[end + 1:]
                        continue
                    total = end + 1 + body_len + 7  # "10=XXX" + SOH
                    if len(buf) >= total:
                        self._buf = buf[total:]
                        return buf[:total]
            sock = self._sock
            if sock is None:
                return None
            try:
                sock.settimeout(timeout)
                chunk = sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk
            self._last_rx = time.monotonic()

    def _open(self):
        with self._lock:
            self._buf = b""
            self._seq_out = 0
            self._expected_in = 1
            try:
                self._sock = socket.create_connection((self.host, self.port),
                                                      timeout=15)
            except OSError as e:
                raise AdapterError(
                    "internal",
                    f"Northgate is unreachable at {self.host}:{self.port}: {e}",
                    True) from None
            self.connected = True
            self._last_rx = self._last_tx = time.monotonic()
        # 141=Y on EVERY Logon, not just the first of the day: this connection
        # has no incremental-sequence resumption mode at all, so anything else
        # is answered with a Logout.
        self.send("A", [("98", "0"), ("108", str(HEARTBEAT)), ("141", "Y"),
                        ("553", USERNAME), ("554", PASSWORD)])
        deadline = time.time() + 15.0
        while time.time() < deadline:
            frame = self._next_frame(max(0.5, deadline - time.time()))
            if frame is None:
                self._drop()
                raise AdapterError("internal",
                                   "Northgate closed the socket during Logon",
                                   True)
            d = first_of(split_pairs(frame))
            self._track_seq(d)
            mt = d.get("35")
            if mt == "A":
                self.hbi = int(_num(d.get("108"), HEARTBEAT)) or HEARTBEAT
                log(f"logged on to {self.host}:{self.port} as "
                    f"{SENDER_COMP_ID}, HeartBtInt {self.hbi}s")
                return
            if mt == "5":
                text = d.get("58") or "logon refused"
                self._drop()
                # Every documented Logon failure (bad CompIDs, missing 141,
                # wrong username or password) arrives this way, and none of
                # them is fixed by trying again with the same credentials.
                raise AdapterError("unauthorized",
                                   f"Northgate refused the Logon: {text}")
        self._drop()
        raise AdapterError("internal", "Northgate did not answer the Logon",
                           True)

    def _drop(self):
        with self._lock:
            sock, self._sock = self._sock, None
            self.connected = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _track_seq(self, d: dict):
        """Inbound MsgSeqNum accounting.

        There is nothing to DO about a gap on this connection: it has no
        resend request and no store-and-resend, so a missed message is missed.
        Saying so is still worth it, because the alternative is a book that is
        quietly short a fill and a platform that never finds out.
        """
        try:
            seq = int(d.get("34"))
        except (TypeError, ValueError):
            return
        if seq > self._expected_in:
            self.on_state("degraded",
                          f"missed {seq - self._expected_in} inbound FIX "
                          f"message(s); this connection cannot replay them")
        self._expected_in = max(self._expected_in, seq) + 1

    # -- threads -------------------------------------------------------
    def _supervise(self):
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as e:      # noqa: BLE001 - any failure reconnects
                log(f"reader: {type(e).__name__}: {e}")
            if self._stop.is_set():
                return
            self._drop()
            self.on_state("degraded", "FIX session lost; reconnecting")
            delay = 1.0
            while not self._stop.is_set():
                try:
                    self._open()
                except AdapterError as e:
                    log(f"reconnect failed ({e}); retrying in {delay:.0f}s")
                    if self._stop.wait(delay):
                        return
                    delay = min(delay * 2, 15.0)
                    continue
                try:
                    self.on_reconnect()
                except Exception as e:  # noqa: BLE001 - never kill the thread
                    log(f"post-reconnect resync failed: {e}")
                break

    def _pump(self):
        while not self._stop.is_set():
            frame = self._next_frame(None)
            if frame is None:
                return
            pairs = split_pairs(frame)
            d = first_of(pairs)
            self._track_seq(d)
            mt = d.get("35")
            if mt == "0":
                continue                       # heartbeat, nothing to do
            if mt == "1":
                # TestRequest: the reply must echo TestReqID(112) on a
                # Heartbeat within one interval or the venue logs us out.
                try:
                    self.send("0", [("112", d.get("112", ""))])
                except SessionDown:
                    return
                continue
            if mt == "5":
                log(f"venue logged us out: {d.get('58') or '(no reason)'}")
                return
            try:
                self.on_message(mt, pairs, d)
            except Exception as e:      # noqa: BLE001 - one bad frame only
                log(f"handling {mt} failed: {type(e).__name__}: {e}")

    def _keepalive(self):
        while not self._stop.wait(1.0):
            if not self.connected:
                continue
            now = time.monotonic()
            if now - self._last_tx >= self.hbi:
                try:
                    self.send("0", [])
                except SessionDown:
                    continue
            # A venue that has gone quiet for 2.5 intervals is not slow, it is
            # gone: it heartbeats every interval on its own and TestRequests us
            # after 1.5. Dropping the socket here is what turns a half-open TCP
            # connection into a reconnect instead of a silent stall.
            if now - self._last_rx > self.hbi * 2.5:
                log("no traffic from the venue for "
                    f"{now - self._last_rx:.0f}s; treating the session as dead")
                self._drop()


class NorthgateAdapter(Adapter):
    name = "northgate"
    version = "1.0"
    broker = "Northgate Securities"
    # Truthful (SPEC 8.2): the venue's own rules of engagement open with
    # "Northgate Securities is a simulated venue for integration testing. All
    # prices are synthetic. No real money is involved."
    mode = "paper"
    # The account is denominated in USD (section 8 of the rules of
    # engagement). There is one account on this connection and no way to ask
    # it anything, so this is the currency for every figure below.
    account_currency = "USD"
    # Netting, and not a preference: the venue keeps ONE book per account and
    # nets an opposing order against it. A hedging view here would report
    # positions the desk does not have.
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        self._state_dir = os.environ.get("BRUE_BROKER_STATE") or \
            os.path.expanduser("~/.brue-connect/state/northgate")
        self._state_path = os.path.join(self._state_dir, "book.json")
        self._lock = threading.RLock()
        self.fix = FixSession(ADDR, self._on_message, self._set_session,
                              self._on_reconnect)
        self._specs = {row[0]: self._build_spec(row) for row in INSTRUMENTS}
        self._listed = [row[0] for row in INSTRUMENTS]   # verified on connect
        self._probe = None
        self._probed_at = 0.0
        self._orders: dict[str, dict] = {}     # ClOrdID: this side's view
        self._by_order_id: dict[str, str] = {}
        self._by_client: dict[str, str] = {}
        self._positions: dict[str, dict] = {}  # symbol: position (netting)
        self._fills: list = []
        self._balance = OPENING_BALANCE
        self._pos_seq = 0
        self._ord_seq = 0
        self._waits: dict[str, dict] = {}
        self._cancels: dict[str, dict] = {}
        self._seen_reports: dict = {}          # exec key: when, for dedupe
        self._quotes: dict[str, dict] = {}     # symbol: last bid/ask/time
        self._quote_subs: set = set()          # what the PLATFORM asked for
        self._wire_subs: set = set()           # what is subscribed on the wire
        self._session_state = None
        self._stop = threading.Event()
        self._pace_at = 0.0
        self._token = "%06x" % (int(time.time() * 1000) & 0xFFFFFF)
        self._load_state()

    # == handshake =====================================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # There are no stop loss, take profit or stop order fields
                # anywhere in this protocol ("not even as an order type that
                # gets rejected gracefully by name"), so brackets, modify and
                # trailing are all false and the platform refuses them loudly
                # instead of degrading into emulation nobody asked for.
                "brackets": False,
                "modify": False,
                "trailing": False,
                # A position is closed with an opposing market order, and the
                # venue nets it, so closing part of one is ordinary dealing.
                "partial_close": True,
                "pending": True,
                "pending_types": ["limit"],
                "expiry": ["gtc", "day", "gtd"],
                # The venue validates margin when the order is placed but
                # holds nothing for a resting one (its margin is computed from
                # open positions only), so an order accepted comfortably can
                # still die at the moment it should have filled. That is the
                # consequence the SPEC's boolean exists to warn about.
                "reserves_margin": False,
                # Nothing on this connection enumerates orders. `orders.list`
                # is answered from what this process placed and remembered,
                # and an order placed by another session on the same account
                # is invisible to it forever, so the platform is told to treat
                # its order view as PARTIAL rather than complete.
                "can_list": False,
                "oco": False,
                "close_by": False,
            },
            "data": {
                "quotes": True,
                # No historical bar or tick download exists on this connection
                # (section 6, restated in section 9's quirks).
                "bars": False,
                "history": False,
                # No deposits, dividends or dealer adjustments are reported on
                # this wire, so a balance that moves without a fill genuinely
                # has no explanation available here.
                "operations": False,
                # No cap declared: the venue publishes no per-message throttle
                # and takes the whole eight-instrument universe in one request.
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # The venue states no numeric cap ("no artificial per-message throttle
        # ... pace order and cancel requests at a reasonable dealing
        # cadence"). This is the cadence THIS ADAPTER holds itself to, and it
        # is enforced in `_pace`, so the number a platform plans against is a
        # number something actually obeys.
        return {"requests_per_second": 10}

    def broker_info(self) -> dict:
        # The desk's own words, from the rules of engagement. No licence and
        # no website appear in that document, and SPEC 3.1 is explicit that
        # inventing them is exactly what this field is not for.
        return {"display_name": "Northgate Securities",
                "tagline": "London FX and bullion dealing desk, est. 1987",
                "mark": "NG",
                "country": "United Kingdom",
                "asset_classes": ["FX", "Bullion", "Special Futures"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the venue something happens here, not in
        `hello` (SPEC 3.1): the socket, the Logon, the catalog check and the
        market data this adapter needs to value its own book."""
        self.fix.start()
        self._set_session("connected", "FIX session established")
        self._probe_catalog()
        self._resubscribe()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def on_shutdown(self):
        self._stop.set()
        self._save_state()
        self.fix.logout()
        self.fix.stop()

    def _on_reconnect(self):
        """A new session is a NEW session (141=Y, MsgSeqNum 1 both ways).

        Nothing that happened while the socket was down is replayed, so there
        is no state to re-request: what this can do is re-establish the market
        data the platform is entitled to and say plainly that the gap existed.
        Any order still in flight when the socket died can never be confirmed
        on this connection, and is reported rather than left hanging.
        """
        with self._lock:
            # A new session carries nothing over: the venue's subscription
            # list died with the old socket, so what this adapter believed was
            # on the wire is now empty and every wanted symbol is re-requested.
            self._wire_subs.clear()
        self._resubscribe()
        stranded = []
        with self._lock:
            for rec in self._orders.values():
                if rec["state"] in ("new", "accepted"):
                    stranded.append(rec["clordid"])
        if stranded:
            self._set_session(
                "degraded",
                f"{len(stranded)} order(s) were in flight when the session "
                f"dropped and this connection cannot replay their reports")
        else:
            self._set_session("connected", "FIX session re-established")

    def _set_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})

    def _watchdog(self):
        """SPEC 8.4: a mutating call may not be silently dropped."""
        while not self._stop.wait(2.0):
            late = []
            with self._lock:
                for rec in self._orders.values():
                    if rec["state"] in ("new", "accepted") \
                            and not rec.get("degraded") \
                            and time.time() * 1000 - rec["placed_at"] \
                            > ORDER_DEADLINE * 1000:
                        rec["degraded"] = True
                        late.append(rec["clordid"])
            for clordid in late:
                self._set_session(
                    "degraded",
                    f"order {clordid} has not reached a terminal state within "
                    f"{ORDER_DEADLINE:.0f}s")

    def _pace(self):
        """The dealing cadence declared in `rate_limits`, applied to the
        messages that deal. Market data and session traffic are not paced:
        the venue's advice is about order flow."""
        with self._lock:
            gap = 1.0 / _num(self.rate_limits().get("requests_per_second"), 10)
            wait = self._pace_at - time.monotonic()
            self._pace_at = max(self._pace_at, time.monotonic()) + gap
        if wait > 0:
            time.sleep(min(wait, 2.0))

    # == persistence ===================================================
    def _load_state(self):
        """The book, from disk.

        This connection has no positions request, no account snapshot and no
        history download, so a restart with nothing on disk starts blind: the
        venue will not tell this adapter what it is already holding. Persisting
        is therefore not an optimisation, it is the only way a reconnect
        reports the same book the desk has.
        """
        try:
            with open(self._state_path) as fh:
                st = json.load(fh)
        except (OSError, json.JSONDecodeError):
            log(f"no stored book at {self._state_path}; starting from the "
                f"published opening balance of {OPENING_BALANCE:.2f} USD. "
                "This account is shared and the venue publishes no balance "
                "inquiry, so a figure another session has already traded "
                "against cannot be read back.")
            return
        self._balance = _num(st.get("balance"), OPENING_BALANCE)
        self._positions = st.get("positions") or {}
        self._fills = st.get("fills") or []
        self._orders = st.get("orders") or {}
        self._by_order_id = st.get("by_order_id") or {}
        self._by_client = st.get("by_client") or {}
        self._pos_seq = int(st.get("pos_seq") or 0)
        self._ord_seq = int(st.get("ord_seq") or 0)
        # The idempotency store outlives the process too: SPEC 2 says a
        # replayed client_id returns the ORIGINAL outcome and places nothing,
        # and a restart is not a licence to place the order a second time.
        for cid, stored in (st.get("idempotent") or {}).items():
            self._idempotent[cid] = stored
        log(f"restored {len(self._positions)} position(s), balance "
            f"{self._balance:.2f} USD, {len(self._fills)} fill(s)")

    def _save_state(self):
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            # The WHOLE save is serialised, not just the snapshot: the reader
            # thread saves after every ExecutionReport and a handler saves
            # after every request, and two of them writing the same temporary
            # file race to rename it, so one loses its file underneath the
            # other and the book fails to persist at all.
            with self._lock:
                st = {"v": 1, "balance": self._balance,
                      "positions": self._positions,
                      # Bounded: the ledger is the adapter's own record and a
                      # long-lived connection should not grow a file forever.
                      "fills": self._fills[-2000:],
                      "orders": {k: {x: y for x, y in v.items()
                                     if x != "degraded"}
                                 for k, v in list(self._orders.items())[-500:]},
                      "by_order_id": self._by_order_id,
                      "by_client": self._by_client,
                      "pos_seq": self._pos_seq, "ord_seq": self._ord_seq,
                      "idempotent": self._idempotent}
                tmp = self._state_path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(st, fh)
                os.replace(tmp, self._state_path)
        except OSError as e:
            log(f"could not persist the book to {self._state_path}: {e}")

    # == catalog =======================================================
    @staticmethod
    def _build_spec(row) -> dict:
        (symbol, name, cls, base, quote, digits, step, min_q, max_q,
         contract, margin, hours) = row
        tick = round(10.0 ** -digits, max(digits, 1))
        return {
            "symbol": symbol,
            "name": name,
            "asset_class": cls,
            "base_ccy": base,
            "quote_ccy": quote,
            "digits": digits,
            "tick_size": tick,
            # Value of one tick per contract in quote_ccy. The SPEC's own
            # default derivation is right for every instrument here: each is
            # quoted in the currency its contract is denominated in.
            "tick_value": round(contract * tick, 10),
            "lot_step": step,
            "min_qty": min_q,
            "max_qty": max_q,
            "min_notional": 0,
            "contract_size": contract,
            "margin_rate": margin,
            "margin_tiers": None,
            "sessions": [dict(s) for s in
                         (SESSIONS_24_7 if hours == "always" else SESSIONS_24_5)],
            # The venue publishes no holiday or early-close calendar at all,
            # only the weekly window above.
            "holidays": [],
            "early_closes": [],
            "tradable": True,
            "shortable": True,
            "trade_mode": "full",
            # No stops exist on this connection, so there is no minimum
            # distance to declare.
            "stops_level_points": 0,
            # "No commission is charged on any instrument on this venue; the
            # only dealing cost is the bid/offer spread quoted on the book."
            "fees": {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
                     "min": 0.0},
            # Same sentence covers financing: nothing is charged for holding a
            # position, including over a weekend. `none` says that, where a 0
            # in unstated units would only look like it does.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            "expires_at": None, "successor": None, "dividend": None,
        }

    def _probe_catalog(self, timeout: float = 6.0):
        """Verify the published universe against the venue itself.

        Northgate has no security list message, so the instrument table comes
        from its rules of engagement; this asks the venue to confirm every
        entry by subscribing to it. A listed symbol answers with a
        MarketDataSnapshotFullRefresh, an unlisted one with a
        MarketDataRequestReject, so a symbol the desk has dropped leaves the
        catalog instead of being offered to a platform that would then send an
        order into a rejection.

        One request per symbol, deliberately: the venue rejects a request
        naming an unknown symbol as a WHOLE, so batching would let one retired
        instrument hide the other seven.
        """
        probe = {"pending": set(self._specs), "listed": set(),
                 "done": threading.Event()}
        with self._lock:
            self._probe = probe
        for symbol in self._specs:
            try:
                self._md_request(symbol, subscribe=True)
            except SessionDown:
                break
        probe["done"].wait(timeout)
        with self._lock:
            self._probe = None
            answered = set(probe["listed"])
            silent = set(probe["pending"])
        if silent:
            # Unanswered is not the same as delisted. Dropping an instrument
            # the venue merely did not get round to would make it untradable
            # for the whole session, so the published entry stands and the
            # gap is said out loud instead.
            log(f"no market data answer for {sorted(silent)} within "
                f"{timeout:.0f}s; keeping the published entries unverified")
        listed = [s for s in self._specs
                  if s in answered or s in silent]
        gone = [s for s in self._listed if s not in listed]
        new = [s for s in listed if s not in self._listed]
        with self._lock:
            self._listed = listed
            self._probed_at = time.time()
        # Symbols nobody asked for are dropped again: the probe is a check,
        # not a subscription, and a platform should not start receiving prices
        # it never requested. What stays is what the platform subscribed to
        # plus what this adapter has to price to value its own book.
        with self._lock:
            keep = set(self._quote_subs) | set(self._positions)
        for symbol in self._specs:
            if symbol in keep:
                continue
            try:
                self._md_request(symbol, subscribe=False)
            except SessionDown:
                break
        if gone:
            self.emit("catalog.changed", {"reason": "delisted",
                                          "symbols": gone})
        if new:
            self.emit("catalog.changed", {
                "reason": "listed", "symbols": new,
                "instruments": [self._specs[s] for s in new]})

    def handle_catalog_list(self, params):
        # Re-verified rather than served from memory, but not on every call:
        # the catalog is read on every reconciliation (SPEC 7) and a probe is
        # eight round trips.
        if time.time() - self._probed_at > 30:
            self._probe_catalog()
        with self._lock:
            listed = list(self._listed)
        return {"instruments": [self._specs[s] for s in listed]}

    def _spec(self, symbol) -> dict:
        with self._lock:
            spec = self._specs.get(symbol) if symbol in self._listed else None
        if spec is None:
            raise AdapterError("unknown_symbol",
                               f"Northgate does not list {symbol!r}")
        return spec

    # == market data ===================================================
    def _md_request(self, symbol: str, subscribe: bool):
        """MarketDataRequest(V) for one symbol.

        The MDReqID is derived from the symbol and therefore stable across
        requests, which is what lets a snapshot, an incremental and a reject
        all be attributed to the right instrument. An unsubscribe is matched
        by the Symbol list rather than by the original MDReqID (the venue is
        explicit about this), so both directions name the symbol.
        """
        fields = [("262", "MD-" + symbol), ("263", "1" if subscribe else "2"),
                  ("264", "1"), ("267", "2"), ("269", "0"), ("269", "1"),
                  ("146", "1"), ("55", symbol)]
        self.fix.send("V", fields)
        with self._lock:
            if subscribe:
                self._wire_subs.add(symbol)
            else:
                self._wire_subs.discard(symbol)

    def _resubscribe(self):
        """What the wire should be carrying: what the platform asked for, plus
        every symbol this adapter holds a position in.

        The second half is not a courtesy to the platform (those quotes are
        never emitted unless it subscribed): unrealised P/L, equity and margin
        are computed here, and with no account inquiry on this connection a
        stale price is the only thing standing behind every figure
        `account.get` reports.
        """
        with self._lock:
            want = set(self._quote_subs) | set(self._positions)
            have = set(self._wire_subs)
        # Only the difference is sent. Re-subscribing something already on the
        # wire is not free: the venue answers every subscribe with a fresh
        # snapshot, so doing it on every fill would spray the platform with
        # quotes it did not ask for.
        for symbol in want - have:
            try:
                self._md_request(symbol, subscribe=True)
            except SessionDown:
                return
        for symbol in have - want:
            try:
                self._md_request(symbol, subscribe=False)
            except SessionDown:
                return

    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._spec(s)             # unknown_symbol before anything else
        with self._lock:
            self._quote_subs.update(symbols)
        for s in symbols:
            # Re-requested even when already on the wire: the venue answers
            # every subscribe with a fresh snapshot, which is the only price a
            # subscriber to a shut market will get at all (its incrementals
            # stop while it is closed).
            try:
                self._md_request(s, subscribe=True)
            except SessionDown as e:
                raise AdapterError("internal", str(e), True) from None
        return {}

    def handle_quotes_unsubscribe(self, params):
        symbols = set(params.get("symbols") or [])
        with self._lock:
            self._quote_subs -= symbols
            keep = set(self._positions)
        for s in symbols - keep:
            try:
                self._md_request(s, subscribe=False)
            except SessionDown:
                pass
        return {}

    def _on_market_data(self, mt: str, pairs: list, d: dict):
        """Snapshot(W) and incremental(X) share one reader.

        They differ only in where the entries start (269 in a snapshot, 279 in
        an incremental) and where the symbol lives (once at the top of a
        snapshot, once per entry in an incremental), so the group is walked
        rather than indexed: a fixed field order would break on the first
        message the venue reorders.
        """
        start = "269" if mt == "W" else "279"
        entries, cur = [], None
        for tag, val in pairs:
            if tag == start:
                cur = {}
                entries.append(cur)
            if cur is not None:
                cur[tag] = val
        symbol = d.get("55") or ""
        mdreqid = d.get("262") or ""
        if symbol not in self._specs and mdreqid.startswith("MD-"):
            symbol = mdreqid[3:]
        bid = ask = None
        for e in entries:
            symbol = e.get("55", symbol)
            if e.get("269") == "0":
                bid = _num(e.get("270"))
            elif e.get("269") == "1":
                ask = _num(e.get("270"))
        if symbol not in self._specs or bid is None or ask is None:
            return
        when = _ms(d.get("52")) or self.now_ms()
        with self._lock:
            self._quotes[symbol] = {"bid": bid, "ask": ask, "time": when}
            probe = self._probe
            wanted = symbol in self._quote_subs
        if probe is not None and symbol in probe["pending"]:
            probe["pending"].discard(symbol)
            probe["listed"].add(symbol)
            if not probe["pending"]:
                probe["done"].set()
        if wanted:
            self.emit("quote", {"symbol": symbol, "bid": bid, "ask": ask,
                                "time": when})

    def _on_md_reject(self, d: dict):
        mdreqid = d.get("262") or ""
        symbol = mdreqid[3:] if mdreqid.startswith("MD-") else ""
        probe = self._probe
        if probe is not None and symbol in probe["pending"]:
            # The venue does not list it. That is the answer the probe exists
            # to get, not an error.
            probe["pending"].discard(symbol)
            if not probe["pending"]:
                probe["done"].set()
            return
        log(f"market data rejected: {d.get('58') or mdreqid}")

    def _mid(self, symbol: str):
        q = self._quotes.get(symbol)
        if not q:
            return None
        return (q["bid"] + q["ask"]) / 2.0

    def _to_usd(self, amount: float, ccy: str) -> float:
        """Quote-currency money into the account's currency.

        Only ever needed for USD/JPY on this venue, and the rate for it is the
        instrument's own price, which the adapter is already streaming. A
        currency it cannot price is returned unconverted and said out loud:
        silently mixing JPY into a USD equity figure would be wrong by two
        orders of magnitude and look perfectly plausible.
        """
        if not ccy or ccy == "USD":
            return amount
        rate = self._mid("USD/" + ccy)
        if rate:
            return amount / rate
        rate = self._mid(ccy + "/USD")
        if rate:
            return amount * rate
        log(f"no rate to value {ccy} in USD; reporting the raw figure")
        return amount

    # == the book ======================================================
    # Everything in this section exists because the connection has no account,
    # positions or history request of any kind. The desk's expectation, stated
    # in section 8, is that the counterparty keeps its own book from the
    # ExecutionReport stream, so that is what these do.

    def _pnl(self, spec: dict, side: str, entry: float, exit_px: float,
             qty: float) -> float:
        raw = (exit_px - entry) if side == "buy" else (entry - exit_px)
        return self._to_usd(raw * qty * spec["contract_size"],
                            spec["quote_ccy"])

    def _notional_usd(self, spec: dict, qty: float, price: float) -> float:
        return self._to_usd(qty * price * spec["contract_size"],
                            spec["quote_ccy"])

    def _unrealized(self, pos: dict) -> float:
        spec = self._specs[pos["symbol"]]
        q = self._quotes.get(pos["symbol"])
        if not q:
            return 0.0
        # Valued at the price the position would CLOSE at, which is the side
        # of the spread it has to cross, not the mid.
        px = q["bid"] if pos["side"] == "buy" else q["ask"]
        return self._pnl(spec, pos["side"], pos["avg_price"], px, pos["qty"])

    def _position_view(self, pos: dict) -> dict:
        out = {
            "position_id": pos["position_id"],
            "symbol": pos["symbol"],
            "side": pos["side"],
            "qty": round(pos["qty"], 8),
            "avg_price": pos["avg_price"],
            # No stop loss, take profit or trailing distance exists anywhere
            # on this connection, so these are structurally absent, not unset.
            "sl": None, "tp": None, "trail_points": 0,
            "unrealized_pnl": round(self._unrealized(pos), 2),
            # The venue charges neither commission nor financing on any
            # instrument, so this is a real zero rather than a split the
            # adapter could not obtain.
            "swap": 0.0, "commission": 0.0,
            "opened_at": pos["opened_at"],
            "from_order": pos.get("from_order"),
        }
        if pos.get("client_id"):
            out["client_id"] = pos["client_id"]
        return out

    def handle_positions_list(self, params):
        with self._lock:
            return {"positions": [self._position_view(p)
                                  for p in self._positions.values()]}

    def handle_account_get(self, params):
        with self._lock:
            unreal = sum(self._unrealized(p) for p in self._positions.values())
            # Margin on the ENTRY price, which is how the desk computes it.
            margin = sum(self._notional_usd(self._specs[p["symbol"]],
                                            p["qty"], p["avg_price"])
                         * self._specs[p["symbol"]]["margin_rate"]
                         for p in self._positions.values())
            balance = self._balance
        equity = balance + unreal
        return {"balance": round(balance, 2), "equity": round(equity, 2),
                "margin_used": round(margin, 2),
                "margin_free": round(equity - margin, 2),
                "currency": self.account_currency}

    def _apply_fill(self, rec: dict, qty: float, price: float,
                    when: int) -> tuple:
        """One clip into the netting book. Returns (position_id, events).

        Events are RETURNED rather than emitted, so the caller can send
        `order.filled` before the `position.closed` it caused: a platform
        reading them in the other order sees a position vanish before it has
        been told what closed it.
        """
        symbol = rec["symbol"]
        spec = self._specs[symbol]
        side = rec["side"]
        events = []
        with self._lock:
            pos = self._positions.get(symbol)
            pid = pos["position_id"] if pos else None
            remaining = qty
            realized_total = 0.0
            if pos is not None and pos["side"] != side:
                take = min(pos["qty"], remaining)
                realized = self._pnl(spec, pos["side"], pos["avg_price"],
                                     price, take)
                realized_total += realized
                self._balance += realized
                pos["qty"] = round(pos["qty"] - take, 8)
                remaining = round(remaining - take, 8)
                closed = {
                    "position_id": pos["position_id"],
                    "qty": round(take, 8), "price": price,
                    "realized_pnl": round(realized, 2),
                    # Nothing is charged for holding or dealing on this venue,
                    # so both are zero rather than omitted: the SPEC wants the
                    # difference between "no carry" and "never told" visible.
                    "swap": 0.0, "commission": 0.0,
                    "reason": rec.get("intent") or (
                        "manual" if rec.get("client_id") else "unknown"),
                    "time": when}
                # SPEC 4: absent is the honest answer when the mapping is
                # gone; inventing one is not.
                if pos.get("client_id"):
                    closed["client_id"] = pos["client_id"]
                events.append(("position.closed", closed))
                if pos["qty"] <= 1e-9:
                    self._positions.pop(symbol, None)
                    pos = None
            if remaining > 1e-9:
                if pos is None:
                    self._pos_seq += 1
                    pid = f"NG-P{self._pos_seq}-{symbol}"
                    pos = {"position_id": pid, "symbol": symbol, "side": side,
                           "qty": remaining, "avg_price": price,
                           "opened_at": when,
                           "client_id": rec.get("client_id"),
                           "from_order": rec.get("type")
                           if rec.get("type") == "limit" else None}
                    self._positions[symbol] = pos
                else:
                    total = pos["qty"] + remaining
                    pos["avg_price"] = round(
                        (pos["avg_price"] * pos["qty"] + price * remaining)
                        / total, spec["digits"] + 2)
                    pos["qty"] = round(total, 8)
                pid = pos["position_id"]
            self._fills.append({
                "fill_id": rec["clordid"] + "-" + str(len(self._fills) + 1),
                "order_id": rec.get("order_id"),
                "client_id": rec.get("client_id"),
                "symbol": symbol, "side": side, "qty": round(qty, 8),
                "price": price, "fees": 0.0,
                "realized_pnl": round(realized_total, 2), "time": when})
        # Durable BEFORE the caller announces the fill, not after. The book is
        # reconstructable from nothing on this connection (no positions
        # request, no history), so a process killed in the instant between
        # telling the platform about a fill and writing it down would come
        # back holding a position it had already reported and no longer knew
        # about. The conformance restart scenario kills the adapter the moment
        # `order.filled` lands, which is exactly that instant.
        self._save_state()
        return pid, events

    # == orders ========================================================
    def _next_clordid(self) -> str:
        with self._lock:
            self._ord_seq += 1
            # The venue keys its duplicate-ClOrdID replay off this string for
            # the life of the account, so it carries a per-process token: a
            # counter alone would collide with a previous run of this adapter
            # and replay somebody else's order back to this adapter.
            return f"PC{self._token}-{self._ord_seq}"

    def _record(self, clordid: str) -> dict:
        with self._lock:
            return self._orders.get(clordid)

    def _validate_qty(self, spec: dict, qty) -> float:
        """Size checked against the catalog before the round trip.

        Northgate answers below-minimum and off-step with OrdRejReason 99, the
        same code it uses for a shut market and an unsupported order type, and
        above-maximum with 3, the same code as insufficient margin. Checking
        here means a size problem reaches the platform as `invalid_qty` and
        only a size problem does.
        """
        q = _num(qty, -1.0)
        if q <= 0:
            raise AdapterError("invalid_qty",
                               f"qty must be positive, got {qty!r}")
        if q < spec["min_qty"] - 1e-9:
            raise AdapterError("invalid_qty",
                               f"qty {q} is below the {spec['symbol']} "
                               f"minimum of {spec['min_qty']} lots")
        if q > spec["max_qty"] + 1e-9:
            raise AdapterError("invalid_qty",
                               f"qty {q} is above the {spec['symbol']} "
                               f"maximum of {spec['max_qty']} lots")
        step = spec["lot_step"]
        units = q / step
        if abs(units - round(units)) > 1e-6:
            raise AdapterError("invalid_qty",
                               f"qty {q} is not a multiple of the "
                               f"{spec['symbol']} lot step {step}")
        return round(round(units) * step, 8)

    def _next_session_close(self, spec: dict) -> int:
        """When a Day order dies, in epoch ms.

        The venue accepts TimeInForce=Day but its ExecutionReport carries no
        ExpireTime back, and SPEC 4 says the platform must not compute a
        broker's trading day itself. So it is computed HERE, from the dealing
        hours the same document publishes: 21:00 UTC for FX and bullion, and
        UTC midnight for the Special Futures, which never close.
        """
        now = time.time()
        if spec["sessions"] == SESSIONS_24_7 or len(spec["sessions"]) == 1:
            return int((int(now) // 86400 + 1) * 86400 * 1000)
        day = int(now) // 86400 * 86400
        for d in range(0, 8):
            cand = day + d * 86400 + 75600      # 21:00 UTC
            if cand <= now:
                continue
            # ISO weekday of the candidate; the close has to fall on a day the
            # desk actually deals.
            dow = ((int(cand) // 86400 + 3) % 7) + 1
            if dow in (6, 7):
                continue
            return int(cand * 1000)
        return int((now + 86400) * 1000)

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        otype = (params.get("type") or "market").lower()
        # Everything that decides what gets DEALT. The venue does enforce this
        # itself (a ClOrdID reused with different terms is rejected with
        # OrdRejReason 6 rather than silently replayed), but the check runs
        # here first so the platform gets the answer without a round trip and
        # without an order id being minted for a request that must not deal.
        sig = _signature("place", symbol, side, _num(params.get("qty"), -1),
                         otype, params.get("price"), params.get("expiry"),
                         params.get("expiry_at"))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("invalid_qty",
                               f"side must be buy or sell, got "
                               f"{params.get('side')!r}")
        spec = self._spec(symbol)
        qty = self._validate_qty(spec, params.get("qty"))
        for field, what in (("sl", "stop loss"), ("tp", "take profit"),
                            ("trail_points", "trailing stop")):
            if params.get(field) is not None:
                raise AdapterError(
                    "unsupported",
                    f"Northgate has no {what} field anywhere in this "
                    f"protocol; manage protective exits from your own flow")
        if otype in ("stop", "stop_limit"):
            raise AdapterError(
                "unsupported",
                f"Northgate deals Market and Limit only; {otype!r} does not "
                f"exist on this connection")
        if otype not in ("market", "limit"):
            raise AdapterError("unsupported", f"unknown order type {otype!r}")

        fields = [("11", None), ("1", ACCOUNT), ("55", symbol),
                  ("54", "1" if side == "buy" else "2"),
                  ("38", "%.2f" % qty),
                  ("40", "1" if otype == "market" else "2")]
        price = None
        if otype == "limit":
            if params.get("price") is None:
                raise AdapterError("x_invalid_price",
                                   "a limit order needs `price`")
            price = round(_num(params.get("price")), spec["digits"])
            fields.append(("44", ("%." + str(spec["digits"]) + "f") % price))
        expiry = (params.get("expiry") or "gtc").lower()
        expires_at = None
        if expiry == "gtc":
            fields.append(("59", "1"))
        elif expiry == "day":
            fields.append(("59", "0"))
            expires_at = self._next_session_close(spec)
        elif expiry == "gtd":
            when = params.get("expiry_at")
            if not when:
                raise AdapterError("invalid_expiry",
                                   "expiry 'gtd' needs expiry_at")
            expires_at = int(_num(when))
            if expires_at <= self.now_ms():
                raise AdapterError("invalid_expiry",
                                   "expiry_at is in the past")
            fields.append(("59", "6"))
            fields.append(("126", _fix_ts(expires_at)))
        else:
            raise AdapterError("invalid_expiry", f"unknown expiry {expiry!r}")

        clordid = self._next_clordid()
        fields[0] = ("11", clordid)
        rec = {"clordid": clordid, "client_id": client_id, "order_id": None,
               "symbol": symbol, "side": side, "qty": qty, "type": otype,
               "price": price, "expiry": expiry, "expires_at": expires_at,
               "state": "new", "cum": 0.0, "avg": None,
               "placed_at": self.now_ms(), "done_at": None,
               "fill_price": None, "reason": None, "intent": None}
        self._register(rec)
        self._send_order(rec, fields)

        result = {"order_id": rec["order_id"],
                  # SPEC 4: `accepted` for a market order, `resting` for
                  # anything that sits on the book. Transport level only; the
                  # ExecutionReports carry the truth either way.
                  "status": "resting" if otype == "limit" else "accepted"}
        self._save_state()
        return self.idempotent_store(client_id, result, sig)

    def _register(self, rec: dict):
        with self._lock:
            self._orders[rec["clordid"]] = rec
            if rec.get("client_id"):
                self._by_client[rec["client_id"]] = rec["clordid"]
            self._waits[rec["clordid"]] = {"ack": threading.Event(),
                                           "done": threading.Event(),
                                           "error": None}

    def _send_order(self, rec: dict, fields: list):
        """NewOrderSingle(D), and the wait for the venue's first word on it."""
        clordid = rec["clordid"]
        wait = self._waits[clordid]         # placed by _register, always here
        self._pace()
        try:
            self.fix.send("D", fields)
        except SessionDown as e:
            with self._lock:
                self._orders.pop(clordid, None)
                self._waits.pop(clordid, None)
            raise AdapterError("internal", f"order not sent: {e}",
                               True) from None
        if not wait["ack"].wait(ACK_TIMEOUT):
            # SPEC 8.4: not silently dropped. The record stays, so a late
            # ExecutionReport still becomes an event on the platform's stream.
            self._set_session("degraded",
                              f"no ExecutionReport for order {clordid} within "
                              f"{ACK_TIMEOUT:.0f}s")
            raise AdapterError("internal",
                               "Northgate has not acknowledged the order yet; "
                               "it may still deal, and its reports will "
                               "arrive as events", True)
        err = wait["error"]
        if err is not None:
            raise err

    def _map_reject(self, code: str, text: str) -> AdapterError:
        key = (code or "99", (text or "").strip().lower())
        hit = REJECT_MAP.get(key) or REJECT_MAP.get((key[0], None))
        if hit is None:
            # An unmapped rejection keeps its identity as an extension rather
            # than being flattened into `internal`, which would throw away the
            # only thing the venue actually said (SPEC 6).
            return AdapterError("x_northgate_reject_" + (code or "99"),
                                text or "rejected by Northgate")
        return AdapterError(hit[0], f"{text or 'rejected'} (OrdRejReason "
                                    f"{code})", hit[1])

    def _finish(self, rec: dict, error: AdapterError = None, ack=True,
                done=False):
        with self._lock:
            wait = self._waits.get(rec["clordid"])
        if wait is None:
            return
        if error is not None and wait["error"] is None:
            wait["error"] = error
        if ack:
            wait["ack"].set()
        if done:
            wait["done"].set()

    # -- the ExecutionReport state machine -----------------------------
    def _on_message(self, mt: str, pairs: list, d: dict):
        if mt == "8":
            self._on_exec_report(d)
        elif mt == "9":
            self._on_cancel_reject(d)
        elif mt in ("W", "X"):
            self._on_market_data(mt, pairs, d)
        elif mt == "Y":
            self._on_md_reject(d)
        elif mt == "j":
            # BusinessMessageReject: the session stays open, so this is
            # information, not a fault. It can only mean this adapter sent a
            # MsgType the venue does not implement.
            log(f"business reject for MsgType {d.get('372')}: {d.get('58')}")
        else:
            log(f"unhandled MsgType {mt}: {d}")

    def _adopt(self, d: dict) -> dict:
        """An ExecutionReport for an order this process did not place.

        The account is shared ("treat concurrent sessions the way you would
        treat two terminals open on the same book"), and the desk nets
        everything on it into ONE book, so a fill from another session really
        does change the position this adapter is reporting. Ignoring it would
        leave a book that quietly disagrees with the desk's; it is adopted
        instead, with no client_id, which is the honest way to say the
        platform did not ask for it.
        """
        symbol = d.get("55") or ""
        rec = {"clordid": d.get("11") or ("adopted-" + (d.get("37") or "?")),
               "client_id": None, "order_id": d.get("37"),
               "symbol": symbol,
               "side": "buy" if d.get("54") == "1" else "sell",
               "qty": _num(d.get("38")), "type": "market", "price": None,
               "expiry": "gtc", "expires_at": None, "state": "accepted",
               "cum": 0.0, "avg": None, "placed_at": self.now_ms(),
               "done_at": None, "fill_price": None, "reason": None,
               "intent": None}
        with self._lock:
            self._orders[rec["clordid"]] = rec
        if symbol in self._specs:
            log(f"adopted an ExecutionReport for {rec['clordid']} "
                f"({symbol}), which this session did not place")
        return rec

    def _on_exec_report(self, d: dict):
        clordid = d.get("11") or ""
        exectype = d.get("150")
        ordstatus = d.get("39")
        cum = _num(d.get("14"))
        # At least once by assumption, not by observation: a duplicated report
        # must not be able to double a fill. The key includes CumQty so a
        # genuinely new clip is never mistaken for a repeat, and ExecID alone
        # is not trusted because it is a per-process counter at the venue and
        # restarts at 1 when it does.
        key = (clordid, d.get("17"), exectype, ordstatus, "%.8f" % cum)
        now = time.time()
        with self._lock:
            if key in self._seen_reports:
                return
            self._seen_reports[key] = now
            if len(self._seen_reports) > 4000:
                for k, t in list(self._seen_reports.items()):
                    if now - t > 3600:
                        self._seen_reports.pop(k, None)
        rec = self._record(clordid)
        if rec is None:
            if exectype == "8":
                # A rejection for an order with no record here carries nothing
                # actionable and creates no book effect.
                log(f"rejection for unknown ClOrdID {clordid}: {d.get('58')}")
                return
            rec = self._adopt(d)
        if d.get("37") and d["37"] != "NONE" and not rec.get("order_id"):
            with self._lock:
                rec["order_id"] = d["37"]
                self._by_order_id[d["37"]] = rec["clordid"]

        if exectype == "8":
            err = self._map_reject(d.get("103"), d.get("58"))
            rec["state"] = "rejected"
            rec["reason"] = err.code
            rec["done_at"] = _ms(d.get("60")) or self.now_ms()
            # SPEC 8.4: a refusal is a terminal outcome and is announced as
            # one, so a platform watching only events still sees the order end.
            self.emit("order.rejected", {"client_id": rec.get("client_id"),
                                         "code": err.code, "message": str(err)})
            self._finish(rec, error=err, done=True)
        elif exectype == "0":
            if rec["state"] == "new":
                rec["state"] = "accepted"
                self.emit("order.accepted", {"client_id": rec.get("client_id"),
                                             "order_id": rec.get("order_id")})
                self._finish(rec)
            elif rec["type"] == "limit" and rec["state"] != "resting":
                # The second 0/0 is the working ack: "a limit order that does
                # not cross the book on arrival produces an accepted report and
                # a resting report, both 150=0/39=0, with different ExecID
                # values". Only the second one means the order is live on the
                # book.
                rec["state"] = "resting"
                self.emit("order.placed", {
                    "client_id": rec.get("client_id"),
                    "order_id": rec.get("order_id"), "type": "limit",
                    "price": rec.get("price"),
                    "expires_at": rec.get("expires_at")})
        elif exectype == "F":
            self._on_fill(rec, d, cum, ordstatus)
        elif exectype == "4":
            rec["state"] = "cancelled"
            rec["done_at"] = _ms(d.get("60")) or self.now_ms()
            # No `code`: on this connection a cancellation only ever happens
            # because the platform asked for one (SPEC 5 reserves `code` for
            # cancellations the broker decided on).
            self.emit("order.cancelled", {"order_id": rec.get("order_id"),
                                          "client_id": rec.get("client_id")})
            self._finish(rec, done=True)
            self._resolve_cancel(rec["clordid"], None)
        elif exectype == "C":
            rec["state"] = "expired"
            rec["done_at"] = _ms(d.get("60")) or self.now_ms()
            self.emit("order.expired", {"order_id": rec.get("order_id"),
                                        "client_id": rec.get("client_id"),
                                        "expiry": rec.get("expiry") or "gtc"})
            self._finish(rec, done=True)
        else:
            log(f"unmapped ExecType {exectype!r} on {clordid}")
        self._save_state()

    def _on_fill(self, rec: dict, d: dict, cum: float, ordstatus: str):
        """One fill clip, measured as the DELTA of CumQty(14).

        LastQty(32) is what the report says just dealt, and CumQty is what has
        dealt in total. The delta is used because it is the only reading that
        is safe both ways: a repeated report restates a CumQty already applied
        and moves the book by zero, and a report lost to a reconnect is
        repaired by the next one instead of leaving the position short. It also
        covers the duplicate-ClOrdID replay, which restates the original
        order's state with no LastQty on it at all.
        """
        delta = round(cum - rec["cum"], 8)
        price = _num(d.get("31")) or _num(d.get("6"))
        when = _ms(d.get("60")) or self.now_ms()
        rec["avg"] = _num(d.get("6")) or rec.get("avg")
        rec["fill_price"] = rec["avg"]
        terminal = ordstatus == "2"
        if delta <= 1e-9:
            if terminal:
                rec["state"] = "filled"
                rec["done_at"] = when
                self._finish(rec, done=True)
            return
        rec["cum"] = cum
        rec["state"] = "filled" if terminal else "partial"
        pid, after = self._apply_fill(rec, delta, price, when)
        data = {"client_id": rec.get("client_id"),
                "order_id": rec.get("order_id"), "position_id": pid,
                "symbol": rec["symbol"], "side": rec["side"],
                "qty": round(delta, 8), "price": price,
                # No commission is charged on any instrument here.
                "fees": 0.0, "time": when}
        if terminal:
            self.emit("order.filled", data)
            rec["done_at"] = when
        else:
            data["remaining"] = _num(d.get("151"))
            self.emit("order.partial", data)
        for name, payload in after:
            self.emit(name, payload)
        self._resubscribe()
        if terminal:
            self._finish(rec, done=True)

    def _on_cancel_reject(self, d: dict):
        """OrderCancelReject(9): the cancel could not be honoured."""
        clordid = d.get("11") or ""
        reason = d.get("102")
        if reason == "1":
            err = AdapterError("not_found",
                               f"Northgate does not recognise the order "
                               f"{d.get('41')!r}")
        else:
            # 0 = too late to cancel. The order is not resting any more,
            # which is a different thing from an id the venue never had.
            err = AdapterError("not_resting",
                               f"order {d.get('41')!r} is no longer resting "
                               f"(OrdStatus {d.get('39')})")
        self._resolve_cancel(d.get("41") or "", err, by_cancel_id=clordid)

    def _resolve_cancel(self, orig_clordid: str, error, by_cancel_id=None):
        with self._lock:
            jobs = [j for j in self._cancels.values()
                    if j["orig"] == orig_clordid
                    or (by_cancel_id and j["clordid"] == by_cancel_id)]
        for job in jobs:
            job["error"] = error
            job["event"].set()

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        with self._lock:
            clordid = self._by_order_id.get(order_id)
            rec = self._orders.get(clordid) if clordid else None
        if rec is None:
            # Nothing on this connection enumerates orders (see `can_list`),
            # so an id this process never placed cannot be looked up anywhere.
            raise AdapterError("not_found",
                               f"no order {order_id!r} in this session's book")
        cancel_id = self._next_clordid()
        job = {"clordid": cancel_id, "orig": rec["clordid"],
               "event": threading.Event(), "error": None}
        with self._lock:
            self._cancels[cancel_id] = job
        try:
            self._pace()
            self.fix.send("F", [("41", rec["clordid"]), ("11", cancel_id),
                                ("55", rec["symbol"]),
                                ("54", "1" if rec["side"] == "buy" else "2"),
                                ("38", "%.2f" % rec["qty"])])
            if not job["event"].wait(ACK_TIMEOUT):
                raise AdapterError("internal",
                                   "Northgate has not answered the cancel yet",
                                   True)
            if job["error"] is not None:
                raise job["error"]
        except SessionDown as e:
            raise AdapterError("internal", f"cancel not sent: {e}",
                               True) from None
        finally:
            with self._lock:
                self._cancels.pop(cancel_id, None)
        self._save_state()
        return {}

    def handle_orders_list(self, params):
        """What this session placed and remembers.

        Declared `can_list: false` in the handshake, so a platform already
        knows this view is PARTIAL: there is no enumerate call on this
        connection and an order placed by another terminal on the same account
        can never appear here. Answered anyway, because the orders this
        adapter did place are real and refusing to name them helps nobody.
        """
        want = (params or {}).get("state")
        out = []
        with self._lock:
            recs = list(self._orders.values())
        for rec in recs:
            state = {"new": "resting", "accepted": "resting",
                     "resting": "resting", "partial": "resting",
                     "filled": "filled", "cancelled": "cancelled",
                     "expired": "expired",
                     "rejected": "cancelled"}.get(rec["state"], "resting")
            if rec["type"] == "market" and state == "resting":
                continue          # in flight, not resting on the book
            out.append({
                "order_id": rec.get("order_id"), "client_id": rec.get("client_id"),
                "symbol": rec["symbol"], "type": rec["type"],
                "side": rec["side"], "qty": rec["qty"],
                "price": rec.get("price") if state == "resting" else None,
                "stop_price": None, "sl": None, "tp": None,
                "expiry": rec.get("expiry") or "gtc",
                "expires_at": rec.get("expires_at"),
                "state": state, "placed_at": rec.get("placed_at"),
                "done_at": rec.get("done_at"),
                "fill_price": rec.get("fill_price") if state == "filled"
                else None,
                "reason": rec.get("reason") if state != "resting" else None})
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def handle_order_amend(self, params):
        # OrderCancelReplace (MsgType G) is named in the rules of engagement as
        # the example of a message this connection does not implement; it comes
        # back as a BusinessMessageReject. Emulating an amend as cancel-and-
        # replace would hand the platform a NEW order id, which `order.amended`
        # has no field to carry, so the emulation could not even be described.
        raise AdapterError("unsupported",
                           "Northgate has no cancel/replace on this "
                           "connection; cancel the order and place a new one")

    def handle_order_modify(self, params):
        raise AdapterError("unsupported",
                           "Northgate has no stop loss or take profit fields "
                           "anywhere in this protocol")

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported",
                           "Northgate has no close-by; it keeps one netted "
                           "book per account")

    # == closing =======================================================
    def _close(self, pos: dict, qty: float, intent: str):
        """The only way to close on this venue: deal the other way.

        There is no close request in this protocol. The desk nets an opposing
        order against the position, so a close is a market order the other
        side and a partial close is a smaller one.
        """
        symbol = pos["symbol"]
        spec = self._specs[symbol]
        side = "sell" if pos["side"] == "buy" else "buy"
        qty = self._validate_qty(spec, qty)
        clordid = self._next_clordid()
        rec = {"clordid": clordid, "client_id": None, "order_id": None,
               "symbol": symbol, "side": side, "qty": qty, "type": "market",
               "price": None, "expiry": "gtc", "expires_at": None,
               "state": "new", "cum": 0.0, "avg": None,
               "placed_at": self.now_ms(), "done_at": None,
               "fill_price": None, "reason": None,
               # Only this side knows whether a close was asked for one
               # position or as part of flattening the book; the venue calls
               # both of them an ordinary fill.
               "intent": intent}
        self._register(rec)
        self._send_order(rec, [
            ("11", clordid), ("1", ACCOUNT), ("55", symbol),
            ("54", "1" if side == "buy" else "2"),
            ("38", "%.2f" % qty), ("40", "1"), ("59", "1")])
        return rec

    def handle_position_close(self, params):
        position_id = str(params.get("position_id") or "")
        client_id = params.get("client_id")
        qty = params.get("qty")
        sig = _signature("close", position_id, qty)
        if client_id:
            prior = self.idempotent_check(client_id, sig)
            if prior is not None:
                return prior
        with self._lock:
            pos = next((p for p in self._positions.values()
                        if p["position_id"] == position_id), None)
        if pos is None:
            raise AdapterError("not_found",
                               f"no open position {position_id!r}")
        want = pos["qty"] if qty is None else _num(qty)
        if want > pos["qty"] + 1e-9:
            raise AdapterError("invalid_qty",
                               f"position {position_id} holds {pos['qty']} "
                               f"lots; cannot close {want}")
        rec = self._close(pos, want, "manual")
        # The fill is what actually closes it, and it lands within
        # milliseconds; waiting means `positions.list` tells the truth the
        # instant this returns instead of a beat later.
        wait = self._waits.get(rec["clordid"])
        if wait is not None:
            wait["done"].wait(FILL_TIMEOUT)
        self._save_state()
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, sig)
        return result

    def handle_flatten_all(self, params):
        """Close everything, and do not come back until the book is empty.

        SPEC 8.1 wants this to work while other calls are failing, so each
        position is dealt independently and one refusal never abandons the
        rest. A position whose market is shut cannot be closed by anybody at
        that moment (this venue rejects a market order outside dealing hours),
        so it is reported on stderr and left rather than counted as closed.
        """
        with self._lock:
            positions = list(self._positions.values())
        recs = []
        for pos in positions:
            try:
                recs.append(self._close(pos, pos["qty"], "flatten"))
            except AdapterError as e:
                log(f"flatten: {pos['position_id']} not closed: {e}")
            except SessionDown as e:
                log(f"flatten: {pos['position_id']} not sent: {e}")
        deadline = time.time() + FILL_TIMEOUT
        for rec in recs:
            wait = self._waits.get(rec["clordid"])
            if wait is not None:
                wait["done"].wait(max(0.1, deadline - time.time()))
        with self._lock:
            left = len(self._positions)
        closed = len(positions) - left
        # Resting orders are part of flattening a book: one left working
        # re-opens the position that was just closed.
        with self._lock:
            resting = [r for r in self._orders.values()
                       if r["state"] == "resting"]
        for rec in resting:
            try:
                self.handle_order_cancel({"order_id": rec.get("order_id")})
            except AdapterError as e:
                log(f"flatten: order {rec['clordid']} not cancelled: {e}")
        self._save_state()
        return {"closed": max(closed, 0)}

    # == history =======================================================
    def handle_history_fills(self, params):
        """The ledger this adapter kept, because the venue keeps none.

        There is no history download on this connection at all, so these are
        the fills THIS adapter saw on the ExecutionReport stream and persisted.
        Fills dealt by another session on the shared account are in here only
        if this adapter was logged on at the time, which is the same limit the
        desk's own drop-copy model imposes on any counterparty.
        """
        frm = params.get("from")
        to = params.get("to")
        with self._lock:
            rows = list(self._fills)
        if frm is not None:
            rows = [f for f in rows if f["time"] >= _num(frm)]
        if to is not None:
            rows = [f for f in rows if f["time"] <= _num(to)]
        rows.sort(key=lambda f: f["time"])
        return {"fills": rows}


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a
    # pipe buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    NorthgateAdapter().run()


if __name__ == "__main__":
    main()
