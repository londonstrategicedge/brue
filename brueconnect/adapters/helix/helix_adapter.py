#!/usr/bin/env python3
"""Brue Connect adapter for Helix Digital (USDT-margined perpetual swaps).

Helix is a crypto derivatives venue: four linear perpetuals (BTC, ETH, SOL,
XRP against USDT), one-way netting, 24/7, no expiry. It speaks HMAC-signed
form-encoded REST on one port and RFC 6455 WebSockets on the next one up: a
public market-data stream and a private order/account push stream, both on
`/stream`, told apart only by a `listenKey` query parameter.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no registration, no signature, no socket.
    Registering costs a whole new (empty) account at this venue, so a picker
    that handshakes an adapter to learn its name must not trigger one. All of
    it lives in `on_first_call`.
  * every signed request is HMAC-SHA256 over the EXACT bytes sent. The payload
    is built once, signed, and `&signature=` appended last; nothing re-encodes
    or re-orders it afterwards, because the venue verifies the bytes rather
    than a re-parse of them. GET/DELETE sign the query string, POST/PUT sign
    the body. One method, `HelixClient._signed`, owns that so no caller can
    get it subtly wrong on the one request that matters.
  * the WebSocket is spoken by hand over a socket (`_WebSocket`): handshake,
    masked client frames, the 7/16/64-bit length forms, continuation frames,
    ping/pong and close. STDLIB ONLY is not a style preference here; this
    process is spawned by whatever python is on the user's machine, so a pip
    import would make the broker permanently unconnectable.
  * two background threads hold the two streams and a third is the backstop
    poller. Both streams reconnect on their own, and every reconnect re-reads
    orders and positions from REST, because Helix replays nothing.
  * both streams are treated as AT LEAST ONCE and the REST responses describe
    the same facts a beat earlier or later, so nothing is emitted from a
    message: everything folds into a per-order and per-symbol state machine
    and a Brue event goes out only on a TRANSITION. A market order's HTTP
    response, its `ORDER_TRADE_UPDATE` frame and a poll of `GET /order` all
    carry the same `executedQty`, and a platform must see one fill, not three.
  * a Helix position has no id of its own. The venue is one-way netting and
    every position endpoint is keyed by symbol, so the SYMBOL is the position
    identity. That is not a shortcut: it is the only identity that survives a
    reconnect, which a synthesized one would not.
  * the venue never says WHY a position closed. Its engine records a reason
    internally but no wire surface carries it, so the reason is DERIVED from
    the protective levels this adapter itself set and the price the close
    printed at, and anything those cannot explain is reported `unknown`
    rather than guessed (SPEC 5).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The SDK lives at the repo root, three levels up from this file. Resolved from
# __file__ rather than hardcoded so the adapter runs the same whether it is
# spawned from a checkout, from site-packages, or from a frozen desktop build
# that only sets cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

BASE = os.environ.get("HELIX_BASE_URL", "http://127.0.0.1:7611")


def _default_ws_url(base: str) -> str:
    """The stream port is the REST port plus one, on every Helix deployment.

    Documented as 7611/7612, so a caller who configured only the REST
    endpoint gets a working stream instead of a silent connection to
    localhost. A deployment that does not keep the two ports adjacent must
    set HELIX_WS_URL explicitly; that always wins, and this is only the
    fallback.
    """
    u = urllib.parse.urlparse(base)
    return "ws://%s:%d/stream" % (u.hostname or "127.0.0.1", (u.port or 80) + 1)


WS_URL = os.environ.get("HELIX_WS_URL") or _default_ws_url(BASE)
# Optional: credentials supplied by the operator instead of self-registration.
# Both must be present to be used. SPEC 8.5: they come from the environment or
# the adapter's own state directory, and never from a protocol message.
# BRUE_CRED_* is the platform's own channel (SPEC 8.5): whatever the user
# typed into this broker's credential form arrives that way, named after the
# form's fields. It wins over the adapter-specific names, which stay for
# anyone running this adapter by hand outside a platform. Absent means
# self-register a demo account, so a user who types nothing still connects.
ENV_KEY = (os.environ.get("BRUE_CRED_API_KEY")
           or os.environ.get("HELIX_API_KEY") or "")
ENV_SECRET = (os.environ.get("BRUE_CRED_API_SECRET")
              or os.environ.get("HELIX_API_SECRET") or "")
# The name this adapter registers under when it has no stored credentials.
ACCOUNT_NAME = os.environ.get("HELIX_ACCOUNT_NAME", "brue-connect")

# Helix budgets 1200 weight per rolling 60 s per apiKey; `account` and `klines`
# weigh 5, everything else 1. Sit under it deliberately rather than discovering
# it with a 429: the platform's requests have 10 s deadlines and a Retry-After
# measured in tens of seconds would blow all of them at once.
WEIGHT_BUDGET = 1000
WEIGHT_WINDOW = 60.0
WEIGHT = {"/api/v1/account": 5, "/api/v1/klines": 5}

# Signed requests carry a clock-drift tolerance. The adapter also measures its
# offset against GET /time, so this is headroom for jitter, not for a wrong
# clock. The venue caps it at 60000 silently.
RECV_WINDOW = 30000

# SPEC 8.4: a mutating call that has not reached a terminal state inside the
# declared timeout is reported as `session.state: degraded`, never waited on
# in silence.
ORDER_DEADLINE = 30.0
# A listenKey dies 30 minutes after the last keepalive and holding the socket
# open does NOT renew it (docs 6.3), so the keepalive runs on its own clock.
LISTEN_KEY_KEEPALIVE = 600.0

TF_TO_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                  "1h": "1h", "4h": "4h", "1d": "1d"}
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
# How often a bar subscription re-reads klines. klines weighs 5, so a tighter
# loop would spend the whole minute budget on a single chart.
BAR_POLL = 15.0

TYPE_TO_WIRE = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP_MARKET",
                "stop_limit": "STOP_LIMIT"}
WIRE_TO_TYPE = {v: k for k, v in TYPE_TO_WIRE.items()}

# Helix trades 24/7 with no maintenance window and no weekend closure, and it
# says so in `tradingHours`. Published as one explicit seven-day window rather
# than as an empty list, because SPEC 3 is emphatic that an empty `sessions`
# means "this broker publishes no calendar" and not "this market is shut", so
# always-open and unknown-calendar look identical unless a venue that really is
# always open says so. Only one of the two is safe to gate orders on.
SESSIONS_24_7 = [
    {"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00",
     "tz": "UTC"},
]


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print("[helix] " + msg, file=sys.stderr, flush=True)


def _num(v, default=0.0) -> float:
    """Helix prints nearly every number as a string (docs 9)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dec(value: float, places: int) -> str:
    """Fixed-point, never scientific notation.

    `str(0.00001)` is fine but `str(1e-05)` is not: the venue parses the
    parameter with float() but the signature covers the exact characters, so a
    quantity that renders as an exponent is signed and sent as one and any
    later reformatting breaks the signature. Fixed-point everywhere removes
    the question.
    """
    return "%.*f" % (places, value + 0.0)


def _places_of(step: float) -> int:
    """Decimal places implied by a quantity step (0.001 -> 3)."""
    s = ("%.10f" % step).rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


class HelixError(Exception):
    """A `{"code": <int>, "msg": "..."}` refusal from Helix."""

    def __init__(self, status: int, body):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.code = int(self.body.get("code") or -status or 0)
        self.msg = str(self.body.get("msg") or body)[:200]
        super().__init__("%s (HTTP %s): %s" % (self.code, status, self.msg))


# Helix's error codes -> the SPEC section 6 closed set.
#
# -1013 is deliberately one code for four different filters and names which
# tripped in `msg`; all four are size problems, so all four are `invalid_qty`.
# -1021 (clock outside recvWindow) has no SPEC code of its own: it is a fault
# of THIS side that a retry after re-reading the venue's clock genuinely
# fixes, so it is retryable `internal` rather than `unauthorized`, which would
# send a platform hunting for bad credentials.
# -1102 is a malformed request; the adapter validates before sending, so one
# arriving here is an adapter bug and is reported as `internal` rather than
# blamed on the caller's parameters.
ERROR_MAP = {
    -1000: ("internal", True),
    -1003: ("rate_limited", True),
    -1013: ("invalid_qty", False),
    -1021: ("internal", True),
    -1022: ("unauthorized", False),
    -1102: ("internal", False),
    -1121: ("unknown_symbol", False),
    -2010: ("insufficient_margin", False),
    -2011: ("not_found", False),
    -2013: ("not_found", False),
    -2015: ("unauthorized", False),
}


def to_adapter_error(e: HelixError) -> AdapterError:
    hit = ERROR_MAP.get(e.code)
    if hit is None:
        # An unknown venue code keeps its identity as an x_ extension rather
        # than being flattened to `internal`, which would destroy the only
        # information the platform had (SPEC 6).
        return AdapterError("x_helix_%d" % abs(e.code),
                            "%d: %s" % (e.code, e.msg), False)
    code, retry = hit
    return AdapterError(code, "%d: %s" % (e.code, e.msg), retry)


# ---------------------------------------------------------------------------
# RFC 6455 client. Stdlib only: socket, ssl, struct, base64, hashlib.
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = (0x0, 0x1, 0x2,
                                                              0x8, 0x9, 0xA)


class _WebSocket:
    """One client WebSocket connection, hand-rolled.

    Deliberately minimal and deliberately complete: the four things that break
    hand-written WebSocket clients in production are unmasked client frames
    (a conformant server MUST close the connection), the 16- and 64-bit length
    forms (which only show up once a payload crosses 125 bytes, so they pass
    every small-message test), unanswered pings (this venue heartbeats every
    20 s and hangs up on silence), and fragmentation. All four are handled
    here, once, so no caller has to think about the wire again.

    Reads block. `close()` is safe from another thread: it shuts the socket
    down, which makes the blocked reader raise, which is how the stream
    threads are stopped.
    """

    def __init__(self, url: str, timeout: float = 45.0):
        u = urllib.parse.urlparse(url)
        secure = u.scheme == "wss"
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self._buf = b""
        self._wlock = threading.Lock()
        self.close_code = None
        self._sock = socket.create_connection((host, port), timeout=15.0)
        if secure:
            self._sock = ssl.create_default_context().wrap_socket(
                self._sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        hostport = host if port in (80, 443) else "%s:%d" % (host, port)
        self._sock.sendall(
            ("GET %s HTTP/1.1\r\n"
             "Host: %s\r\n"
             "Upgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             "Sec-WebSocket-Key: %s\r\n"
             "Sec-WebSocket-Version: 13\r\n"
             "\r\n" % (path, hostport, key)).encode("ascii"))
        head = self._read_until(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101" not in lines[0]:
            raise OSError("websocket upgrade refused: %s" % lines[0][:120])
        want = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        got = ""
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        if got != want:
            # Not pedantry: the accept header is the only proof that what
            # answered is a WebSocket endpoint and not a proxy or an error page
            # that happened to send a 101.
            raise OSError("bad Sec-WebSocket-Accept")
        self._sock.settimeout(timeout)

    # -- reading ------------------------------------------------------
    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("connection closed during handshake")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """The next TEXT message, or None once the peer has closed.

        Control frames are handled here and never reach the caller: a ping is
        ponged with its own payload, a pong is dropped, and a close is echoed
        so the peer sees a clean shutdown rather than a reset.
        """
        frag_op, frag = None, b""
        while True:
            b0, b1 = self._read_exactly(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, length = b1 & 0x80, b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exactly(8))[0]
            mask = self._read_exactly(4) if masked else None
            data = self._read_exactly(length) if length else b""
            if mask:
                # A server must not mask, but unmasking anyway costs nothing
                # and beats returning garbage if one ever does.
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if opcode == _OP_PING:
                self._frame(_OP_PONG, data)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                if len(data) >= 2:
                    self.close_code = struct.unpack("!H", data[:2])[0]
                try:
                    self._frame(_OP_CLOSE, data[:2])
                except OSError:
                    pass
                return None
            if opcode == _OP_CONT:
                frag += data
                if not fin:
                    continue
                payload, was_text = frag, frag_op == _OP_TEXT
                frag_op, frag = None, b""
                if was_text:
                    return payload.decode("utf-8", "replace")
                continue
            if not fin:
                frag_op, frag = opcode, data
                continue
            if opcode == _OP_TEXT:
                return data.decode("utf-8", "replace")
            # Binary frames are not part of this venue's protocol; ignored
            # rather than treated as an error, per the forward-compat rule.

    # -- writing ------------------------------------------------------
    def _frame(self, opcode: int, payload: bytes = b""):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        # Client frames MUST be masked (RFC 6455 5.3). A fresh random key per
        # frame: the mask is not security, but a fixed one is a protocol
        # violation some servers do check for.
        mask = os.urandom(4)
        header += mask
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._wlock:
            self._sock.sendall(bytes(header) + body)

    def send_json(self, obj):
        self._frame(_OP_TEXT, json.dumps(obj).encode("utf-8"))

    def close(self):
        try:
            self._frame(_OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# REST client: signing, credentials, the weight budget, retries.
# ---------------------------------------------------------------------------

class HelixClient:
    """Signed REST, with the whole auth and rate-limit story inside.

    One place owns the signature, so no caller can build the canonical string
    slightly differently; one place owns the weight budget, so no caller can
    spend another's; one place owns the clock offset, so a drifting host does
    not turn into a scattering of -1021s across every endpoint.
    """

    def __init__(self, base: str, cred_path: str):
        self.base = base.rstrip("/")
        self.cred_path = cred_path
        self._creds = None
        self._lock = threading.Lock()
        self._spent: list = []          # (monotonic, weight)
        self.offset_ms = 0.0            # venue clock minus local clock
        self._ctx = ssl.create_default_context()

    # -- credentials ---------------------------------------------------
    def credentials(self) -> dict:
        """Register once, then reuse forever.

        Registration provisions a NEW, EMPTY account and the secret is shown
        exactly once with no recovery endpoint (docs 2.1), so registering on
        every start would silently orphan yesterday's positions and lose the
        only copy of the key. The file lives in the profile's own state
        directory so two profiles wrapping this adapter cannot end up sharing
        one account.
        """
        if self._creds:
            return self._creds
        if ENV_KEY and ENV_SECRET:
            self._creds = {"apiKey": ENV_KEY, "apiSecret": ENV_SECRET}
            return self._creds
        try:
            with open(self.cred_path) as fh:
                c = json.load(fh)
            if c.get("apiKey") and c.get("apiSecret"):
                self._creds = c
                return c
        except (OSError, json.JSONDecodeError):
            pass
        c = self._raw("POST", "/api/v1/register",
                      body=urllib.parse.urlencode({"name": ACCOUNT_NAME}))
        if not c.get("apiKey") or not c.get("apiSecret"):
            raise AdapterError("unauthorized",
                               "Helix registration returned no credentials")
        try:
            os.makedirs(os.path.dirname(self.cred_path) or ".", exist_ok=True)
            tmp = self.cred_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(c, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cred_path)
        except OSError as e:
            log("could not persist credentials to %s: %s" % (self.cred_path, e))
        log("registered account %s" % c.get("accountId"))
        self._creds = c
        return c

    @property
    def account_id(self):
        return (self._creds or {}).get("accountId")

    # -- clock ---------------------------------------------------------
    def sync_clock(self):
        """Measure the venue's clock once, so signatures are not rejected for
        a reason that has nothing to do with the signature.

        Half the round trip is charged to the network, which is the standard
        estimate and is exact enough for a 30 s window.
        """
        t0 = time.time()
        r = self._raw("GET", "/api/v1/time")
        t1 = time.time()
        server = _num(r.get("serverTime"))
        if server:
            self.offset_ms = server - (t0 + t1) / 2.0 * 1000.0

    def venue_ms(self) -> int:
        return int(time.time() * 1000 + self.offset_ms)

    # -- the wire ------------------------------------------------------
    def _budget(self, weight: int):
        """Block until this call fits inside the rolling weight window."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._spent = [x for x in self._spent
                               if now - x[0] < WEIGHT_WINDOW]
                used = sum(w for _, w in self._spent)
                if used + weight <= WEIGHT_BUDGET:
                    self._spent.append((now, weight))
                    return
                wait = WEIGHT_WINDOW - (now - self._spent[0][0]) + 0.01
            log("local weight budget exhausted, holding %.1fs" % wait)
            time.sleep(min(wait, 5.0))

    def _raw(self, method: str, path: str, query: str = "", body=None,
             headers=None):
        url = self.base + path + (("?" + query) if query else "")
        head = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = body.encode() if isinstance(body, str) else body
            head["Content-Type"] = "application/x-www-form-urlencoded"
        head.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=head,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=20,
                                        context=self._ctx) as r:
                raw = r.read().decode() or "{}"
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"msg": raw[:200]}
            if e.code == 429:
                parsed["retryAfter"] = _num(e.headers.get("Retry-After"), 1.0)
            raise HelixError(e.code, parsed) from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            # A transport failure is not a venue refusal, but callers only
            # know how to handle one exception type, so it wears the venue's
            # own "internal error" code and stays retryable.
            raise HelixError(0, {"code": -1000,
                                 "msg": "%s: %s" % (type(e).__name__, e)}) \
                from None

    def _signed(self, method: str, path: str, params: dict):
        """THE signing path (docs 2.2).

        Build the parameter string, HMAC it, append `&signature=` LAST, and
        send those exact bytes. Nothing between the signing and the send may
        re-order or re-encode a character: the venue verifies the raw payload,
        not a re-serialization of its own parse of it. GET and DELETE carry it
        in the query string; POST and PUT carry it in the body.
        """
        c = self.credentials()
        fields = dict(params)
        fields["timestamp"] = self.venue_ms()
        fields["recvWindow"] = RECV_WINDOW
        payload = urllib.parse.urlencode(fields)
        sig = hmac.new(c["apiSecret"].encode(), payload.encode(),
                       hashlib.sha256).hexdigest()
        signed = payload + "&signature=" + sig
        headers = {"X-HLX-APIKEY": c["apiKey"]}
        if method in ("GET", "DELETE"):
            return self._raw(method, path, query=signed, headers=headers)
        return self._raw(method, path, body=signed, headers=headers)

    def call(self, method: str, path: str, params=None, signed: bool = True,
             retries: int = 2):
        """One venue call, with the budget, the 429 and the clock handled."""
        for attempt in range(retries + 1):
            self._budget(WEIGHT.get(path, 1))
            try:
                if signed:
                    return self._signed(method, path, params or {})
                query = urllib.parse.urlencode(params or {})
                if method in ("GET", "DELETE"):
                    return self._raw(method, path, query=query)
                return self._raw(method, path, body=query)
            except HelixError as e:
                last = attempt >= retries
                if last:
                    raise
                if e.code == -1003:
                    # Honour Retry-After, but never sleep past the platform's
                    # own deadline: a shorter nap and a second try beats a
                    # timeout with no answer at all.
                    time.sleep(min(_num(e.body.get("retryAfter"), 1.0), 4.0))
                    continue
                if e.code == -1021:
                    # The signature was right and the clock was not. Re-measure
                    # and try once more rather than surfacing a fault the
                    # caller cannot act on.
                    log("timestamp outside recvWindow; re-syncing the clock")
                    try:
                        self.sync_clock()
                    except HelixError:
                        pass
                    continue
                if e.status == 0:
                    time.sleep(0.4)
                    continue
                raise


class HelixAdapter(Adapter):
    name = "helix"
    version = "1.0"
    broker = "Helix Digital"
    # Truthful (SPEC 8.2): the venue's own documentation opens with "Helix
    # Digital is a simulated venue for integration testing. All prices are
    # synthetic. No real money is involved."
    mode = "paper"
    # Every Helix account is denominated in USDT (docs 6.1); re-checked
    # against the account on first call and complained about on stderr if the
    # venue ever disagrees.
    account_currency = "USDT"
    # "One-way (netting) position mode: an account holds at most one open
    # position per symbol, and an opposite-side fill reduces or flips it"
    # (docs 3). There is no hedge mode.
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        state = os.environ.get("BRUE_BROKER_STATE") or os.path.expanduser(
            "~/.brue-connect/state/helix")
        self.api = HelixClient(BASE, os.path.join(state, "credentials.json"))
        self._state_lock = threading.RLock()
        self._instruments: dict = {}       # symbol -> exchangeInfo row + derived
        self._orders: dict = {}            # order_id -> our view
        self._pos: dict = {}               # symbol -> our view of the position
        self._pos_at = 0.0                 # as-of of the newest applied snapshot
        self._closing: dict = {}           # symbol -> close WE asked for
        self._pending_fee: dict = {}       # symbol -> fees not yet on a position
        self._quotes: set = set()          # symbols streaming
        self._bars: dict = {}              # (symbol, tf) -> [last bar ms, next poll]
        self._listen_key = None
        self._listen_key_at = 0.0
        self._pub_ws = None
        self._prv_ws = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._session_state = None
        self._sub_id = 0

    # == handshake =====================================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # stopLoss / takeProfit attach to the position an order opens
                # (docs 5.1) and POST /position/sltp maintains them after the
                # fact, so brackets and modify are real, broker-side features.
                "brackets": True,
                "modify": True,
                # The venue's engine has a trailing field but NO endpoint
                # exposes it: neither the order form nor /position/sltp accepts
                # a trailing distance. Declared false so the platform refuses
                # the feature loudly instead of emulating it tick by tick
                # (SPEC 3).
                "trailing": False,
                "partial_close": True,      # POST /position/close?quantity=
                "pending": True,
                # STOP_MARKET is documented (docs 5.1) and does not work: the
                # venue accepts the type, reads its trigger from `stopPrice`,
                # and then hands the matcher an order with no trigger price at
                # all, which it refuses with -1102 "invalid parameter". Sending
                # `price` as well does not help, because the order path never
                # reads `price` for that type. Verified against the venue with
                # both shapes. STOP_LIMIT, which carries
                # both fields, rests and triggers correctly. Declaring the
                # broken type anyway would have the platform offer a stop
                # order that can only ever be rejected (SPEC 3: absent means
                # absent, and silent degradation is forbidden).
                "pending_types": ["limit", "stop_limit"],
                # GTC is the only value the venue accepts and it says so:
                # "timeInForce ... `GTC` is the only supported value".
                "expiry": ["gtc"],
                # docs 9: "Resting LIMIT/STOP orders do not reserve margin
                # ahead of triggering; margin is checked against available free
                # margin at placement time". That is exactly the case SPEC's
                # boolean exists for: an order accepted comfortably can still
                # die at the moment it should have filled.
                "reserves_margin": False,
                "oco": False,
                # position.close_by is a hedging-book operation and this venue
                # is netting-only: there are never two positions to close
                # against each other.
                "close_by": False,
                "can_list": True,           # GET /openOrders
            },
            "data": {
                "quotes": True, "bars": True, "history": True,
                # Nothing but trading moves money here: no funding, no
                # dividends, no deposits or withdrawals, and no ledger endpoint
                # that could report one. A balance that moved without a fill
                # genuinely has no explanation available.
                "operations": False,
                # The venue states no subscription cap and its whole universe
                # is four symbols on one socket. Absent means "the broker
                # states no limit" (SPEC 3), which is the truth.
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # 1200 weight per rolling 60 s, and the cheapest endpoint costs 1, so
        # 20/s is the ceiling. Declared under it, because `account` and
        # `klines` cost 5 apiece and a platform pacing itself at the absolute
        # limit would trip the budget on its first chart.
        return {"requests_per_second": 15}

    def broker_info(self) -> dict:
        # The venue's own words only. Its documentation carries no company,
        # country, licence or website, and inventing them is precisely what
        # SPEC 3.1 says this field is not for.
        return {"display_name": "Helix Digital",
                "tagline": "USDT-margined perpetual swaps, 24/7",
                "mark": "HX",
                "asset_classes": ["Crypto Perpetuals"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the venue something happens here, not in
        `hello` (SPEC 3.1): credentials, clock, catalog, account, streams."""
        try:
            self._open_session()
        except HelixError as e:
            # A venue refusal here must keep its TYPE. Letting HelixError
            # escape lands in the base class's catch-all and reaches the
            # platform as `internal`, so a user who simply pasted the wrong
            # API key is told the adapter broke rather than that the key was
            # rejected. That distinction is the whole point of the code.
            raise to_adapter_error(e) from None

    def _open_session(self):
        self.api.sync_clock()
        self.api.credentials()
        self._load_instruments()
        acct = self.api.call("GET", "/api/v1/account")
        if acct.get("currency") and acct["currency"] != self.account_currency:
            # Cannot be corrected after the fact: `hello` has already gone out.
            # Loud on stderr is the only honest move left.
            log("WARNING: account currency is %r but the handshake declared %r"
                % (acct["currency"], self.account_currency))
        self._absorb_positions(acct.get("positions") or [], time.time())
        self._stop.clear()
        threading.Thread(target=self._public_loop, daemon=True).start()
        threading.Thread(target=self._private_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._sync_orders()

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        for attr in ("_pub_ws", "_prv_ws"):
            ws = getattr(self, attr, None)
            if ws is not None:
                try:
                    ws.close()
                except Exception:       # noqa: BLE001 - shutting down anyway
                    pass
        key = self._listen_key
        if key:
            try:
                self.api.call("DELETE", "/api/v1/listenKey",
                              {"listenKey": key})
            except (HelixError, AdapterError):
                pass

    def now_ms(self) -> int:
        """The VENUE's clock, not this box's (SPEC: one clock per adapter).

        Before `on_first_call` the offset is zero and this is the wall clock,
        which is correct: `ping` must answer cold too, and an unmeasured offset
        must not become a fabricated one.
        """
        return self.api.venue_ms()

    # == catalog =======================================================
    def _load_instruments(self):
        data = self.api.call("GET", "/api/v1/exchangeInfo", signed=False)
        rows = {}
        for i in data.get("symbols") or []:
            sym = i.get("symbol")
            if not sym:
                continue
            row = dict(i)
            row["_digits"] = int(_num(i.get("pricePrecision"), 2))
            row["_qty_places"] = _places_of(_num(i.get("qtyStep"), 1.0))
            rows[sym] = row
        if not rows:
            raise AdapterError("internal", "exchangeInfo listed no symbols")
        with self._state_lock:
            self._instruments = rows

    def _spec(self, i: dict) -> dict:
        sym = i["symbol"]
        digits = i["_digits"]
        tick = round(10.0 ** -digits, max(digits, 1))
        base = i.get("baseAsset") or ""
        quote = i.get("quoteAsset") or "USDT"
        return {
            "symbol": sym,
            # exchangeInfo publishes no display name, so one is composed from
            # the fields it does publish rather than carried in a hardcoded
            # table that would go stale the day the venue lists a fifth pair.
            "name": "%s/%s %s" % (base, quote,
                                  (i.get("contractType") or "").title()),
            "asset_class": "crypto",
            "base_ccy": base or None,
            "quote_ccy": quote,
            "digits": digits,
            "tick_size": tick,
            # Linear perpetual: one contract is one unit of the base asset, so
            # one tick of price is one tick of value per contract.
            "tick_value": tick,
            "lot_step": _num(i.get("qtyStep"), 1.0),
            "min_qty": _num(i.get("minQty")),
            "max_qty": _num(i.get("maxQty")),
            # Crypto venues gate on notional, not size, and this one refuses
            # below 10 USDT before it even looks at the lot filters (docs 5.3).
            "min_notional": _num(i.get("minNotional")),
            # "quantity ... in base asset units" and MIN_NOTIONAL is
            # `quantity * referencePrice`, so a contract IS one base unit.
            "contract_size": 1.0,
            "margin_rate": _num(i.get("marginRate")),
            # The venue publishes one margin rate and no maintenance rate and
            # no tiers. Omitting them lets SPEC's documented defaults apply
            # (maintenance = initial, flat rate) instead of inventing numbers.
            "margin_tiers": None,
            "sessions": [dict(s) for s in SESSIONS_24_7],
            "holidays": [],
            "early_closes": [],
            "tradable": (i.get("status") or "").upper() == "TRADING",
            "shortable": True,          # perpetual swap: both sides always
            "trade_mode": "full",
            # The venue documents no minimum distance between the market and an
            # SL/TP/stop price, and rejects none in practice.
            "stops_level_points": 0,
            "fees": {"commission_per_lot": 0.0, "mode": "pct_notional",
                     "rate": _num(i.get("takerFeeRate")), "min": 0.0},
            # No funding rate exists anywhere in this venue: positions are not
            # financed and no endpoint reports a charge. 0 with swap_mode
            # "none" says "not charged" rather than putting a number in units
            # nobody can interpret.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            "expires_at": None, "successor": None, "dividend": None,
        }

    def handle_catalog_list(self, params):
        self._load_instruments()
        with self._state_lock:
            rows = list(self._instruments.values())
        return {"instruments": [self._spec(i) for i in rows]}

    def _instrument(self, symbol) -> dict:
        with self._state_lock:
            i = self._instruments.get(symbol)
        if i is None:
            self._load_instruments()          # catalogs change; re-read once
            with self._state_lock:
                i = self._instruments.get(symbol)
        if i is None:
            raise AdapterError("unknown_symbol", "no such symbol %r" % symbol)
        return i

    # == account =======================================================
    def _account_raw(self) -> dict:
        issued = time.time()
        a = self.api.call("GET", "/api/v1/account")
        self._absorb_positions(a.get("positions") or [], issued)
        return a

    @staticmethod
    def _account_view(a: dict) -> dict:
        return {"balance": _num(a.get("balance")),
                "equity": _num(a.get("equity")),
                "margin_used": _num(a.get("marginUsed")),
                "margin_free": _num(a.get("marginFree")),
                "currency": a.get("currency") or "USDT"}

    def handle_account_get(self, params):
        return self._account_view(self._account_raw())

    # == positions =====================================================
    def _position_view(self, sym: str, p: dict) -> dict:
        out = {
            # A Helix position has no id: every position endpoint is keyed by
            # symbol and the book is one-way, so the symbol IS the identity.
            # It is also the only identity that survives a reconnect, which a
            # locally minted one would not.
            "position_id": sym,
            "symbol": sym,
            "side": p["side"],
            "qty": p["qty"],
            "avg_price": p["avg_price"],
            "sl": p.get("sl"), "tp": p.get("tp"),
            "trail_points": 0,
            "unrealized_pnl": p.get("unrealized", 0.0),
            # Fees are charged per fill and this venue finances nothing, so the
            # split is real rather than a shrug: commission is what the fills
            # that built this position actually cost, swap is zero because no
            # funding exists here (SPEC: 0 and 0 would be the answer for a
            # broker that cannot separate them, and this one can).
            "swap": 0.0,
            "commission": round(p.get("commission", 0.0), 8),
            "opened_at": int(p.get("opened_at") or 0),
            "from_order": p.get("from_order"),
        }
        if p.get("client_id"):
            out["client_id"] = p["client_id"]
        return out

    def handle_positions_list(self, params):
        self._account_raw()
        with self._state_lock:
            rows = [(s, dict(p)) for s, p in self._pos.items()]
        return {"positions": [self._position_view(s, p) for s, p in rows]}

    def _absorb_positions(self, rows: list, as_of: float):
        """Fold one account snapshot into our view, emitting on TRANSITIONS.

        Snapshots arrive from three places (the REST account read, the private
        stream's ACCOUNT_UPDATE, the poller) and any of them can be the first
        to notice a close. The comparison and the write happen together under
        one lock, so whichever observer gets there first is the ONLY one that
        sees a delta and the only one that emits. That is what stops a venue
        that reports the same fact three ways from becoming three
        `position.closed` events on the platform's book.

        Snapshots older than the newest one already applied are dropped whole.
        A REST read issued before a push frame can land after it, and applying
        it would resurrect a position that is already gone, only for the next
        snapshot to close it a second time.
        """
        deltas = []
        with self._state_lock:
            if as_of < self._pos_at:
                return
            self._pos_at = as_of
            seen = {}
            for r in rows:
                sym = r.get("symbol")
                if not sym:
                    continue
                seen[sym] = {
                    "side": "buy" if (r.get("side") or "").upper() == "LONG"
                            else "sell",
                    "qty": _num(r.get("qty")),
                    "avg_price": _num(r.get("entryPrice")),
                    # "0" is this venue's unset sentinel for sl/tp, the same
                    # one it uses for an order's price/stopPrice (docs 6.1).
                    "sl": _num(r.get("stopLoss")) or None,
                    "tp": _num(r.get("takeProfit")) or None,
                    "unrealized": _num(r.get("unrealizedPnl")),
                }
            for sym, old in list(self._pos.items()):
                cur = seen.get(sym)
                gone = cur is None or cur["qty"] <= 1e-12
                flipped = not gone and cur["side"] != old["side"]
                reduced = (not gone and not flipped
                           and cur["qty"] < old["qty"] - 1e-9)
                if not (gone or flipped or reduced):
                    continue
                if gone or flipped:
                    closed_qty, remaining = old["qty"], 0.0
                    # A flip is a close and a fresh open under one ticket, so
                    # the old record must not carry its fees or its open time
                    # into the new position.
                    self._pos.pop(sym, None)
                else:
                    closed_qty = round(old["qty"] - cur["qty"], 10)
                    remaining = cur["qty"]
                share = old.get("commission", 0.0) * (
                    closed_qty / old["qty"] if old["qty"] else 1.0)
                deltas.append((sym, dict(old), closed_qty, remaining, share))
                if remaining > 0:
                    old["qty"] = remaining
                    old["commission"] = old.get("commission", 0.0) - share
            for sym, cur in seen.items():
                if cur["qty"] <= 1e-12:
                    continue
                old = self._pos.get(sym)
                if old is None:
                    # Fees charged by a fill that landed before the snapshot
                    # showing its position. Parked by `_charge` rather than
                    # lost to that race, so a close still reports what the
                    # position cost to open.
                    cur["commission"] = self._pending_fee.pop(sym, 0.0)
                    cur["opened_at"] = 0
                    self._pos[sym] = cur
                else:
                    old.update({k: cur[k] for k in
                                ("side", "qty", "avg_price", "sl", "tp",
                                 "unrealized")})
            # Deliberately outside the loop above: dating a position costs a
            # REST read, and that must not happen under the lock.
            adopt = [s for s, p in self._pos.items()
                     if not p.get("opened_at") and not p.get("dated")]
        for sym, old, qty, remaining, share in deltas:
            self._report_close(sym, old, qty, remaining, share)
        for sym in adopt:
            self._date_position(sym)

    def _date_position(self, sym: str):
        """When did a position we did not open ourselves open?

        Nothing on the wire says: a position object is symbol, side, size,
        entry, unrealised, sl, tp and nothing else. The fill ledger does say,
        though, so the run of trades that built the current position is walked
        back from the newest until the running net matches what is open now,
        and the oldest trade in that run is the opening one. Derived from the
        venue's own records rather than stamped with the moment this process
        happened to notice, which would be a fact about the adapter, not the
        position.
        """
        with self._state_lock:
            pos = dict(self._pos.get(sym) or {})
        if not pos or pos.get("opened_at") or pos.get("dated"):
            return
        try:
            rows = self.api.call("GET", "/api/v1/userTrades",
                                 {"symbol": sym, "limit": 500})
        except (HelixError, AdapterError) as e:
            log("could not date position %s: %s" % (sym, e))
            rows = []
        want = pos["qty"] * (1.0 if pos["side"] == "buy" else -1.0)
        net, opened = 0.0, 0
        for row in reversed(rows or []):
            net += _num(row.get("qty")) * (
                1.0 if (row.get("side") or "").upper() == "BUY" else -1.0)
            if abs(net - want) < 1e-9:
                opened = int(_num(row.get("time")))
                break
        with self._state_lock:
            cur = self._pos.get(sym)
            if cur is not None and not cur.get("opened_at"):
                # 0 rather than a guess when the ledger cannot explain the
                # position; absent is the honest answer (SPEC 4 on client_id
                # takes the same line). Marked either way so an undatable
                # position does not re-read the ledger on every snapshot.
                cur["opened_at"] = opened
                cur["dated"] = True

    def _report_close(self, sym: str, old: dict, qty: float, remaining: float,
                      commission: float):
        """One position.closed, priced and reasoned from the venue's records."""
        with self._state_lock:
            job = self._closing.pop(sym, None)
        price = (job or {}).get("price")
        time_ms = (job or {}).get("time")
        if not price:
            price, time_ms = self._closing_print(sym, old, qty)
        inst = None
        try:
            inst = self._instrument(sym)
        except AdapterError:
            pass
        contract = 1.0
        digits = inst["_digits"] if inst else 8
        direction = 1.0 if old["side"] == "buy" else -1.0
        if price:
            realized = round((price - old["avg_price"]) * direction * qty
                             * contract, 2)
        else:
            # Never reached in practice (the ledger always carries the closing
            # print), but a P/L invented from no price would be worse than
            # saying nothing, and the SPEC has no way to say "unpriced".
            realized = 0.0
            log("close of %s has no printed price; realized reported as 0" % sym)
        reason, detail = self._close_reason(old, price, job, digits)
        data = {"position_id": sym, "qty": qty,
                "price": round(price, digits) if price else None,
                "realized_pnl": realized,
                # This venue charges its taker fee per fill and finances
                # nothing, so both halves of the split are known facts.
                "swap": 0.0, "commission": round(commission, 8),
                "reason": reason,
                "time": int(time_ms or self.now_ms())}
        if detail:
            data["reason_detail"] = detail
        if old.get("client_id"):
            data["client_id"] = old["client_id"]
        self.emit("position.closed", data)
        if remaining <= 0:
            self._wake.set()

    def _closing_print(self, sym: str, old: dict, qty: float):
        """The venue's own print for a close it made by itself.

        Every engine-initiated close (a stop loss, a take profit, a stop-out)
        writes a fill with a NULL orderId, which is the one thing that
        distinguishes it from an order the client sent. That row carries the
        exact price and time, so nothing here has to be estimated from a quote.
        """
        try:
            rows = self.api.call("GET", "/api/v1/userTrades",
                                 {"symbol": sym, "limit": 20})
        except (HelixError, AdapterError) as e:
            log("could not read the closing print for %s: %s" % (sym, e))
            return None, None
        want = "SELL" if old["side"] == "buy" else "BUY"
        best = None
        for row in rows or []:
            if (row.get("side") or "").upper() != want:
                continue
            if row.get("orderId") is not None:
                continue
            if abs(_num(row.get("qty")) - qty) > 1e-9:
                continue
            if best is None or _num(row.get("time")) >= _num(best.get("time")):
                best = row
        if best is None:
            return None, None
        return _num(best.get("price")), int(_num(best.get("time")))

    def _close_reason(self, old: dict, price, job, digits: int):
        """Why did this position close?

        Helix publishes no reason anywhere: its engine records one internally,
        but neither the fill ledger, the position object nor the ACCOUNT_UPDATE
        frame carries it. What IS knowable is whether this adapter asked for
        the close, and where the protective levels it set for the position sat
        relative to the price the close printed at. The venue closes a long on
        its stop at the first bid at or below the stop and prints that bid, so
        a close we did not request, printing through a stop we did set, is a
        determination and not a guess.

        Anything those two facts cannot explain is `unknown`. SPEC 5 is
        explicit that guessing is worse than admitting ignorance, because the
        platform will show "closed by your stop loss" with the same confidence
        it shows a fact.
        """
        if job:
            return job.get("reason", "manual"), None
        if price:
            tol = 10.0 ** -digits / 2.0
            long = old["side"] == "buy"
            sl, tp = old.get("sl"), old.get("tp")
            if sl and ((long and price <= sl + tol)
                       or (not long and price >= sl - tol)):
                return "sl", "closed at %s, through the stop loss at %s" % (
                    price, sl)
            if tp and ((long and price >= tp - tol)
                       or (not long and price <= tp + tol)):
                return "tp", "closed at %s, through the take profit at %s" % (
                    price, tp)
        return "unknown", ("Helix publishes no close reason; this close "
                           "matches neither the stop loss nor the take profit "
                           "on the position")

    # == orders ========================================================
    def _validate_qty(self, i: dict, qty) -> float:
        """Size checked against the catalog before the round trip.

        Helix answers below-min, above-max and off-step all with -1013 and
        names the filter in a free-text `msg`, and it checks MIN_NOTIONAL
        first, so parsing its answer back into a precise cause means trusting a
        message string. Checking here means a size problem is a size problem.
        """
        q = _num(qty, -1.0)
        step = _num(i.get("qtyStep"), 0.0)
        lo, hi = _num(i.get("minQty")), _num(i.get("maxQty"))
        if q <= 0:
            raise AdapterError("invalid_qty", "qty must be positive, got %r"
                               % (qty,))
        if lo and q < lo - 1e-12:
            raise AdapterError("invalid_qty", "qty %s below minQty %s" % (q, lo))
        if hi and q > hi + 1e-12:
            raise AdapterError("invalid_qty", "qty %s above maxQty %s" % (q, hi))
        if step > 0:
            units = q / step
            if abs(units - round(units)) > 1e-6:
                raise AdapterError("invalid_qty",
                                   "qty %s is not a multiple of qtyStep %s"
                                   % (q, step))
            q = round(units) * step
        return round(q, 10)

    def _fee_estimate(self, i: dict, qty: float, price: float) -> float:
        """The published taker schedule, as the FALLBACK only.

        A fill's own `fee` rides on the private stream frame and is the
        venue's own number; this stands in when the HTTP response beats the
        frame to us, since the response carries no fee at all. Negative,
        because a commission costs the holder.
        """
        return -round(qty * price * _num(i.get("takerFeeRate")), 8)

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        otype = (params.get("type") or "market").lower()
        # The signature is everything that decides what gets DEALT. Helix will
        # not refuse a conflicting reuse for us: a repeated newClientOrderId
        # returns the ORIGINAL order even when the symbol, side, size and price
        # all differ (docs 5.1), which is exactly the silent-wrong-order case
        # SPEC 6 reserves duplicate_client_id for.
        sig = (symbol, side, _num(params.get("qty"), -1), otype,
               params.get("price"), params.get("stop_price"),
               params.get("sl"), params.get("tp"),
               params.get("expiry"), params.get("expiry_at"))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("invalid_qty", "side must be buy or sell, got %r"
                               % (params.get("side"),))
        if otype not in TYPE_TO_WIRE:
            raise AdapterError("unsupported", "unknown order type %r" % otype)
        if otype == "stop":
            # See capabilities: the venue's STOP_MARKET path is broken and
            # answers -1102 to every shape of the request. Refused here with
            # the reason rather than passed through to collect a venue error
            # the platform cannot interpret.
            raise AdapterError(
                "unsupported",
                "Helix documents STOP_MARKET but rejects every one of them "
                "(-1102): its order path never passes the trigger to the "
                "matcher. Use stop_limit, which the venue does honour")
        i = self._instrument(symbol)
        qty = self._validate_qty(i, params.get("qty"))
        if params.get("trail_points"):
            raise AdapterError("unsupported",
                               "Helix has no broker-side trailing stop")
        expiry = (params.get("expiry") or "gtc").lower()
        if expiry != "gtc":
            # Declared in capabilities.orders.expiry, so a conformant platform
            # never sends anything else; refused rather than silently
            # downgraded to GTC, which would leave a resting order alive past
            # the day the caller meant it to die.
            raise AdapterError("invalid_expiry",
                               "Helix honours GTC only, not %r" % expiry)
        digits, places = i["_digits"], i["_qty_places"]
        body = {"symbol": symbol, "side": side.upper(),
                "type": TYPE_TO_WIRE[otype], "quantity": _dec(qty, places)}
        if otype in ("limit", "stop_limit"):
            price = params.get("price")
            if price is None:
                raise AdapterError("invalid_stops",
                                   "a %s order needs `price`" % otype)
            body["price"] = _dec(_num(price), digits)
        if otype == "stop_limit":
            trigger = params.get("stop_price")
            if trigger is None:
                raise AdapterError("invalid_stops",
                                   "a stop_limit order needs `stop_price`")
            body["stopPrice"] = _dec(_num(trigger), digits)
        for field, wire in (("sl", "stopLoss"), ("tp", "takeProfit")):
            if params.get(field) is not None:
                body[wire] = _dec(_num(params[field]), digits)
        body["newClientOrderId"] = client_id

        try:
            r = self.api.call("POST", "/api/v1/order", body)
        except HelixError as e:
            err = to_adapter_error(e)
            # SPEC 8.4: no mutating call may be silently dropped. A synchronous
            # refusal is a terminal outcome and is announced as one, so a
            # platform watching only the event stream still sees the order end.
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": err.code, "message": str(err)})
            raise err
        self._absorb_order(r)
        order_id = str(r.get("orderId"))
        status = (r.get("status") or "").upper()
        result = {"order_id": order_id,
                  # "resting" only when it really is on the book: a marketable
                  # limit deals inline here and reporting it as resting would
                  # have the platform waiting for a fill it already has.
                  "status": "resting" if (otype != "market"
                                          and status == "NEW") else "accepted"}
        self._wake.set()
        return self.idempotent_store(client_id, result, sig)

    def _order_state(self, wire_status: str, otype: str) -> str:
        return {"NEW": "resting" if otype != "market" else "accepted",
                "PARTIALLY_FILLED": "resting", "FILLED": "filled",
                "CANCELED": "cancelled", "EXPIRED": "expired"}.get(
                    wire_status, "resting")

    def _absorb_order(self, o: dict):
        """Fold one Helix order object into our view, emitting on TRANSITIONS.

        The same order arrives from the POST response, from the private
        stream (at least once), and from any poll of GET /order, all carrying
        the same cumulative `executedQty`. Everything here is keyed on a
        change in that number or in the status, so running it three times on
        one fill produces one `order.filled`.
        """
        oid = str(o.get("orderId"))
        symbol = o.get("symbol")
        wire_status = (o.get("status") or "").upper()
        otype = WIRE_TO_TYPE.get((o.get("type") or "").upper(), "market")
        side = "buy" if (o.get("side") or "").upper() == "BUY" else "sell"
        orig = _num(o.get("origQty"))
        executed = _num(o.get("executedQty"))
        avg = _num(o.get("avgPrice"))
        client_id = o.get("clientOrderId") or None
        events = []
        with self._state_lock:
            rec = self._orders.get(oid)
            if rec is None:
                # An order this process did not place: adopted after a restart,
                # or sent by another session on the same account. The venue
                # echoes the client id this adapter sent back on every copy of
                # the order, so even an adopted one can be tied to its platform
                # ticket.
                rec = {"order_id": oid, "client_id": client_id,
                       "symbol": symbol, "side": side, "type": otype,
                       "qty": orig, "price": _num(o.get("price")) or None,
                       "stop_price": _num(o.get("stopPrice")) or None,
                       "filled": 0.0, "avg": 0.0, "state": None,
                       "placed_at": int(_num(o.get("time"))), "done_at": None,
                       "deadline": time.time() + ORDER_DEADLINE,
                       "degraded": False}
                self._orders[oid] = rec
            if client_id and not rec.get("client_id"):
                rec["client_id"] = client_id
            rec["qty"] = orig or rec["qty"]
            if rec["state"] is None:
                rec["state"] = "accepted"
                events.append(("order.accepted",
                               {"client_id": rec["client_id"],
                                "order_id": oid}))
            if rec.get("type") == "stop_limit" and otype == "limit":
                # A triggered STOP_LIMIT comes back as a plain LIMIT: the venue
                # rewrites the order's own type in place rather than announcing
                # the promotion, so the type change IS the announcement.
                rec["type"] = "limit"
                events.append(("order.promoted", {
                    "order_id": oid, "from": "stop_limit", "to": "limit",
                    "price": _num(o.get("price")) or rec.get("price")}))
            if (otype != "market" and wire_status == "NEW"
                    and rec["state"] == "accepted"):
                rec["state"] = "resting"
                events.append(("order.placed", {
                    "client_id": rec["client_id"], "order_id": oid,
                    "type": otype, "price": rec["price"] or rec["stop_price"],
                    "expires_at": None}))
            clip = executed - rec["filled"]
            if clip > 1e-12:
                # The clip price: the frame's own lastFilledPrice when it has
                # one, otherwise recovered exactly from the running average,
                # which is arithmetic on two broker numbers rather than an
                # estimate. `avgPrice` alone would report the second clip of a
                # split market order at the average of both.
                px = _num(o.get("lastFilledPrice"))
                if not px:
                    px = ((avg * executed - rec["avg"] * rec["filled"]) / clip
                          if executed > 0 else avg)
                fee = (-abs(_num(o.get("fee"))) if o.get("fee") is not None
                       else None)
                rec["filled"], rec["avg"] = executed, avg
                remaining = round(orig - executed, 10)
                events.append(("_fill", {"oid": oid, "symbol": symbol,
                                         "side": side, "qty": round(clip, 10),
                                         "price": px, "fee": fee,
                                         "remaining": max(remaining, 0.0),
                                         "client_id": rec["client_id"],
                                         "time": int(_num(o.get("updateTime")))}))
            new_state = self._order_state(wire_status, otype)
            if (new_state in ("filled", "cancelled", "expired")
                    and rec["state"] not in ("filled", "cancelled", "expired")):
                rec["state"] = new_state
                rec["done_at"] = int(_num(o.get("updateTime")))
                if new_state == "cancelled":
                    # `code` present would mean the BROKER killed it (SPEC 5).
                    # Helix cancels an order only when asked, so a cancellation
                    # here is the platform's own and carries none.
                    events.append(("order.cancelled",
                                   {"order_id": oid,
                                    "client_id": rec["client_id"]}))
                elif new_state == "expired":
                    events.append(("order.expired",
                                   {"order_id": oid,
                                    "client_id": rec["client_id"],
                                    "expiry": "gtc"}))
        for name, data in events:
            if name != "_fill":
                self.emit(name, data)
                continue
            fee = data["fee"]
            if fee is None:
                try:
                    fee = self._fee_estimate(self._instrument(data["symbol"]),
                                             data["qty"], data["price"])
                except AdapterError:
                    fee = 0.0
            payload = {"client_id": data["client_id"], "order_id": data["oid"],
                       "position_id": data["symbol"], "symbol": data["symbol"],
                       "side": data["side"], "qty": data["qty"],
                       "price": data["price"], "fees": fee,
                       "time": data["time"] or self.now_ms()}
            if data["remaining"] > 1e-12:
                payload["remaining"] = data["remaining"]
                self.emit("order.partial", payload)
            else:
                self.emit("order.filled", payload)
            self._charge(data["symbol"], fee)
        if events:
            self._wake.set()

    def _charge(self, symbol: str, fee: float):
        """Remember what a fill cost, so a close can report it.

        The venue never reports a position's accumulated commission: it debits
        the balance per fill and the position object carries no fee field at
        all. Accumulating it here is the only way `position.closed` can carry a
        real number instead of a zero that means "this broker never told me".
        """
        with self._state_lock:
            pos = self._pos.get(symbol)
            if pos is not None:
                pos["commission"] = pos.get("commission", 0.0) + fee
            else:
                # The fill landed before the account snapshot that will show
                # its position. Parked so the position picks it up when it
                # appears rather than being lost to a race.
                self._pending_fee[symbol] = (
                    self._pending_fee.get(symbol, 0.0) + fee)

    def _order_view(self, rec: dict) -> dict:
        return {
            "order_id": rec["order_id"], "client_id": rec.get("client_id"),
            "symbol": rec.get("symbol"), "type": rec.get("type"),
            "side": rec.get("side"), "qty": rec.get("qty"),
            "price": rec.get("price"), "stop_price": rec.get("stop_price"),
            "sl": rec.get("sl"), "tp": rec.get("tp"),
            # GTC is the only time in force this venue accepts, so no order it
            # holds can have any other, and none of them expire.
            "expiry": "gtc", "expires_at": None,
            "state": rec.get("state") if rec.get("state") != "accepted"
            else "resting",
            "placed_at": rec.get("placed_at"), "done_at": rec.get("done_at"),
            "fill_price": rec.get("avg") or None,
            "reason": None,
        }

    def handle_orders_list(self, params):
        """Resting orders from the venue, terminal ones from memory.

        GET /openOrders returns the resting book and nothing else (docs 9): a
        market order never appears there even while its later clips are still
        in flight. So the venue answers for what is live and this process's own
        record answers for what has finished, which is everything a platform
        can be told without an endpoint that enumerates history by order.
        """
        want = (params or {}).get("state")
        rows = self.api.call("GET", "/api/v1/openOrders", {})
        for o in rows or []:
            self._absorb_order(o)
        live = {str(o.get("orderId")) for o in rows or []}
        with self._state_lock:
            recs = [dict(r) for r in self._orders.values()]
        out = []
        for rec in recs:
            if rec["order_id"] not in live and rec.get("state") in (
                    None, "accepted", "resting"):
                continue        # in flight, not on the book: not enumerable
            out.append(self._order_view(rec))
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def _symbol_of(self, order_id: str):
        with self._state_lock:
            rec = self._orders.get(order_id)
        if rec and rec.get("symbol"):
            return rec["symbol"]
        # Every order endpoint here is keyed by symbol AND id, so an id alone
        # is not enough to act on. For an order this process never saw, the
        # resting book is the only place to look it up.
        for o in self.api.call("GET", "/api/v1/openOrders", {}) or []:
            if str(o.get("orderId")) == order_id:
                self._absorb_order(o)
                return o.get("symbol")
        return None

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        symbol = self._symbol_of(order_id)
        if symbol is None:
            raise AdapterError("not_found", "unknown order %s" % order_id)
        try:
            o = self.api.call("DELETE", "/api/v1/order",
                              {"symbol": symbol, "orderId": order_id})
        except HelixError as e:
            if e.code == -2011:
                # Helix answers "already filled", "already cancelled" and
                # "never existed" with ONE code (docs 5.4), and SPEC 6 needs
                # those told apart. GET /order knows the order that exists but
                # is not resting, so one extra read buys the right code.
                raise self._cancel_refusal(symbol, order_id) from None
            raise to_adapter_error(e)
        self._absorb_order(o)
        return {}

    def _cancel_refusal(self, symbol: str, order_id: str) -> AdapterError:
        try:
            o = self.api.call("GET", "/api/v1/order",
                              {"symbol": symbol, "orderId": order_id})
        except HelixError:
            return AdapterError("not_found", "unknown order %s" % order_id)
        self._absorb_order(o)
        return AdapterError("not_resting",
                            "order %s is %s, not resting"
                            % (order_id, o.get("status")))

    def handle_order_amend(self, params):
        # Helix's order surface is POST, GET and DELETE and nothing else:
        # there is no amend endpoint in any form. Refusing loudly is what SPEC
        # 3 asks for when a capability is absent. Emulating it as
        # cancel-and-replace would hand the platform a NEW order_id, and
        # `order.amended` has no field to carry one, so the emulation could not
        # even be described on this wire.
        raise AdapterError("unsupported",
                           "Helix cannot amend a resting order; cancel it and "
                           "place a new one")

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported",
                           "Helix is a one-way book; there are never two "
                           "positions to close against each other")

    def handle_order_modify(self, params):
        """SL/TP on an open position (POST /position/sltp)."""
        sym = str(params.get("position_id") or "")
        if not sym:
            raise AdapterError("not_found", "position_id is required")
        if params.get("trail_points"):
            raise AdapterError("unsupported",
                               "Helix has no broker-side trailing stop")
        i = self._instrument(sym)
        body = {"symbol": sym}
        # An absent key leaves the level alone; an explicit null clears it
        # (SPEC 4), which this venue spells as an empty parameter value.
        for field, wire in (("sl", "stopLoss"), ("tp", "takeProfit")):
            if field in params:
                v = params[field]
                body[wire] = "" if v is None else _dec(_num(v), i["_digits"])
        if len(body) == 1:
            return {}
        try:
            p = self.api.call("POST", "/api/v1/position/sltp", body)
        except HelixError as e:
            raise to_adapter_error(e)
        with self._state_lock:
            rec = self._pos.get(sym)
            if rec is not None:
                rec["sl"] = _num(p.get("stopLoss")) or None
                rec["tp"] = _num(p.get("takeProfit")) or None
        return {}

    # == closing =======================================================
    def handle_position_close(self, params):
        sym = str(params.get("position_id") or "")
        if not sym:
            raise AdapterError("not_found", "position_id is required")
        qty = params.get("qty")
        client_id = params.get("client_id")
        if client_id:
            prior = self.idempotent_check(client_id, ("close", sym, qty))
            if prior is not None:
                return prior
        self._close(sym, qty, reason="manual")
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, ("close", sym, qty))
        return result

    def _close(self, sym: str, qty, reason: str):
        i = self._instrument(sym)
        with self._state_lock:
            known = sym in self._pos
        if not known:
            # Closing a position this process has never listed (adopted after a
            # reconnect). Read it first: the entry price it carries is what
            # turns the closing print into a realised P/L, and without it the
            # close would be reported with no number at all.
            self._account_raw()
        body = {"symbol": sym}
        if qty is not None:
            body["quantity"] = _dec(_num(qty), i["_qty_places"])
        # Filed BEFORE the request goes out, not after it comes back. The
        # ACCOUNT_UPDATE frame for this close regularly reaches the stream
        # thread before the HTTP response reaches this one, and a job filed
        # afterwards describes a close that has already been reported: the
        # stream path would find no job, decide the venue closed the position
        # by itself, and reason about a stop that was never hit. Filing first
        # means whichever path arrives first knows what this close was, and the
        # loser finds the work already done.
        with self._state_lock:
            self._closing[sym] = {"reason": reason, "since": time.time()}
        try:
            r = self.api.call("POST", "/api/v1/position/close", body)
        except HelixError as e:
            with self._state_lock:
                self._closing.pop(sym, None)
            raise to_adapter_error(e)
        closed = _num(r.get("executedQty"))
        price = _num(r.get("avgPrice"))
        when = int(_num(r.get("updateTime")) or _num(r.get("time")))
        deltas = []
        with self._state_lock:
            job = self._closing.get(sym)
            if job is not None:
                # The stream has not reported it yet: take the close here,
                # where the exact print is already in hand.
                job["price"], job["time"] = price, when
                old = self._pos.get(sym)
                if old is not None:
                    remaining = round(old["qty"] - closed, 10)
                    share = old.get("commission", 0.0) * (
                        closed / old["qty"] if old["qty"] else 1.0)
                    if remaining <= 1e-12:
                        self._pos.pop(sym, None)
                        remaining = 0.0
                    else:
                        old["qty"] = remaining
                        old["commission"] = old.get("commission", 0.0) - share
                    deltas.append((sym, dict(old), closed, remaining, share))
                    self._pos_at = time.time()
                else:
                    # The venue closed something this adapter had no record of.
                    # Reported anyway, with the print the response carries and
                    # no P/L, because a platform that asked for a close must
                    # hear that it happened (SPEC 8.4). The job stays filed so
                    # `_report_close` still takes its price and its reason.
                    log("closed %s with no local record; reporting the print "
                        "without a realised figure" % sym)
                    deltas.append((sym, {"side": "buy", "avg_price": price,
                                         "sl": None, "tp": None,
                                         "client_id": None,
                                         "qty": closed}, closed, 0.0, 0.0))
        for s, old, q, rem, share in deltas:
            self._report_close(s, old, q, rem, share)
        return closed

    def handle_flatten_all(self, params):
        """Close everything, and do not come back until the book is empty.

        A platform calls positions.list the instant this returns, so the work
        has to be finished rather than merely started. Helix closes
        synchronously (the response carries the closing print), which makes
        that possible without a wait loop. SPEC 8.1 also wants this to work
        while other calls are failing, so each position is attempted
        independently and one refusal never abandons the rest.
        """
        try:
            a = self._account_raw()
        except HelixError as e:
            raise to_adapter_error(e)
        closed = 0
        for row in a.get("positions") or []:
            sym = row.get("symbol")
            if not sym:
                continue
            try:
                self._close(sym, None, reason="flatten")
                closed += 1
            except AdapterError as e:
                log("flatten: %s did not close: %s" % (sym, e))
        # A resting order left working re-opens the position just closed, so
        # flattening a book means the orders too.
        try:
            for o in self.api.call("GET", "/api/v1/openOrders", {}) or []:
                try:
                    self._absorb_order(self.api.call(
                        "DELETE", "/api/v1/order",
                        {"symbol": o.get("symbol"),
                         "orderId": str(o.get("orderId"))}))
                except HelixError as e:
                    log("flatten: order %s not cancelled: %s"
                        % (o.get("orderId"), e))
        except HelixError as e:
            log("flatten: could not list resting orders: %s" % e)
        return {"closed": closed}

    # == market data ===================================================
    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._instrument(s)          # unknown_symbol before subscribing
        with self._state_lock:
            fresh = [s for s in symbols if s not in self._quotes]
            self._quotes.update(symbols)
            ws = self._pub_ws
        if fresh and ws is not None:
            try:
                self._ws_subscribe(ws, fresh, "SUBSCRIBE")
            except OSError as e:
                # The stream thread re-subscribes the whole set on its next
                # connection, so a failure here costs latency, not coverage.
                log("subscribe failed on the live socket: %s" % e)
        # A snapshot immediately, from the venue's own book endpoint. The
        # stream speaks only on a tick, and a caller that has just asked for a
        # price should not have to wait for the market to move to get one.
        for s in symbols:
            try:
                self._emit_book(self.api.call("GET", "/api/v1/ticker/bookTicker",
                                              {"symbol": s}, signed=False))
            except HelixError as e:
                log("quote snapshot for %s failed: %s" % (s, e))
        return {}

    def handle_quotes_unsubscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        with self._state_lock:
            drop = [s for s in symbols if s in self._quotes]
            self._quotes.difference_update(drop)
            ws = self._pub_ws
        if drop and ws is not None:
            try:
                self._ws_subscribe(ws, drop, "UNSUBSCRIBE")
            except OSError as e:
                log("unsubscribe failed on the live socket: %s" % e)
        return {}

    def _ws_subscribe(self, ws, symbols, method: str):
        with self._state_lock:
            self._sub_id += 1
            mid = self._sub_id
        # Stream names are lowercase symbol + @bookTicker, exactly (docs 4.2);
        # an unknown name rejects the WHOLE request, so only catalog symbols
        # ever reach here.
        ws.send_json({"method": method,
                      "params": [s.lower() + "@bookTicker" for s in symbols],
                      "id": mid})

    def _emit_book(self, row: dict):
        sym = row.get("symbol") or row.get("s")
        self.emit("quote", {"symbol": sym,
                            "bid": _num(row.get("bidPrice", row.get("b"))),
                            "ask": _num(row.get("askPrice", row.get("a"))),
                            "time": int(_num(row.get("time", row.get("T"))))})

    def handle_bars_history(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_INTERVAL:
            raise AdapterError("unsupported", "unknown timeframe %r" % tf)
        count = int(_num(params.get("count"), 200))
        query = {"symbol": symbol, "interval": TF_TO_INTERVAL[tf],
                 # "limit: optional, default 500, maximum 1000 (values above
                 # 1000 are silently capped)": capped here so the caller is
                 # not told it got more than it did.
                 "limit": max(1, min(count, 1000))}
        if params.get("from"):
            query["startTime"] = int(_num(params["from"]))
        if params.get("to"):
            query["endTime"] = int(_num(params["to"]))
        rows = self.api.call("GET", "/api/v1/klines", query, signed=False)
        bars = [[int(_num(r[0])), _num(r[1]), _num(r[2]), _num(r[3]),
                 _num(r[4]), _num(r[5])] for r in rows or []]
        bars.sort(key=lambda b: b[0])          # oldest first (SPEC)
        return {"bars": bars}

    def handle_bars_subscribe(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_INTERVAL:
            raise AdapterError("unsupported", "unknown timeframe %r" % tf)
        with self._state_lock:
            self._bars.setdefault((symbol, tf), [0, 0.0])
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._state_lock:
            self._bars.pop((params.get("symbol"),
                            params.get("timeframe") or "1m"), None)
        return {}

    # == history =======================================================
    def handle_history_fills(self, params):
        rows = self.api.call("GET", "/api/v1/userTrades", {"limit": 500})
        lo = _num((params or {}).get("from"), 0)
        hi = _num((params or {}).get("to"), 0)
        fills = []
        for t in rows or []:
            when = int(_num(t.get("time")))
            if lo and when < lo:
                continue
            if hi and when > hi:
                continue
            oid = t.get("orderId")
            with self._state_lock:
                rec = self._orders.get(str(oid)) if oid is not None else None
            fills.append({
                "fill_id": str(t.get("tradeId")),
                "order_id": str(oid) if oid is not None else None,
                "client_id": (rec or {}).get("client_id"),
                "position_id": t.get("symbol"),
                "symbol": t.get("symbol"),
                "side": "buy" if (t.get("side") or "").upper() == "BUY"
                        else "sell",
                "qty": _num(t.get("qty")),
                "price": _num(t.get("price")),
                "fees": -abs(_num(t.get("fee"))),
                "time": when,
            })
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == the public stream =============================================
    def _public_loop(self):
        """One public WebSocket, re-established for its whole life."""
        while not self._stop.is_set():
            ws = None
            try:
                ws = _WebSocket(self._ws_url())
                with self._state_lock:
                    self._pub_ws = ws
                    syms = sorted(self._quotes)
                if syms:
                    self._ws_subscribe(ws, syms, "SUBSCRIBE")
                self._set_session("connected", "streams open")
                while not self._stop.is_set():
                    msg = ws.recv()
                    if msg is None:
                        break
                    self._on_public(msg)
            except Exception as e:      # noqa: BLE001 - any failure reconnects
                if self._stop.is_set():
                    return
                self._set_session("degraded",
                                  "market data stream: %s: %s"
                                  % (type(e).__name__, e))
                time.sleep(1.0)
            finally:
                with self._state_lock:
                    if self._pub_ws is ws:
                        self._pub_ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:   # noqa: BLE001 - best effort
                        pass

    def _on_public(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log("unparseable public frame: %s" % raw[:120])
            return
        if "error" in msg:
            # A bad stream name rejects the whole SUBSCRIBE, so a silent
            # failure here would look like a symbol that simply never ticks.
            log("stream request refused: %s" % msg["error"])
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return                       # subscription acknowledgement
        if data.get("e") == "bookTicker":
            self.emit("quote", {"symbol": data.get("s"),
                                "bid": _num(data.get("b")),
                                "ask": _num(data.get("a")),
                                "time": int(_num(data.get("T")))})

    def _ws_url(self, listen_key=None) -> str:
        u = urllib.parse.urlparse(WS_URL)
        path = u.path or "/stream"
        base = "%s://%s%s" % (u.scheme, u.netloc, path)
        if listen_key:
            return base + "?listenKey=" + urllib.parse.quote(listen_key)
        return base

    # == the private stream ============================================
    def _private_loop(self):
        """One private WebSocket, keyed by a listenKey that has to be minted.

        A listenKey dies 30 minutes after its last keepalive and an expired one
        closes the socket with code 4000, so a reconnect always mints a fresh
        key rather than retrying the dead one. Every reconnection re-reads
        orders and positions from REST: Helix replays nothing, so the gap is
        only closable from the source of truth.
        """
        first = True
        while not self._stop.is_set():
            ws = None
            try:
                key = self._new_listen_key()
                ws = _WebSocket(self._ws_url(key))
                with self._state_lock:
                    self._prv_ws = ws
                if not first:
                    self._sync_orders()
                    self._account_raw()
                first = False
                while not self._stop.is_set():
                    msg = ws.recv()
                    if msg is None:
                        log("private stream closed by the venue (code %s)"
                            % ws.close_code)
                        break
                    self._on_private(msg)
            except Exception as e:      # noqa: BLE001 - any failure reconnects
                if self._stop.is_set():
                    return
                self._set_session("degraded",
                                  "account stream: %s: %s"
                                  % (type(e).__name__, e))
                time.sleep(1.0)
            finally:
                with self._state_lock:
                    if self._prv_ws is ws:
                        self._prv_ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:   # noqa: BLE001 - best effort
                        pass

    def _new_listen_key(self) -> str:
        r = self.api.call("POST", "/api/v1/listenKey", {})
        key = r.get("listenKey")
        if not key:
            raise AdapterError("unauthorized", "Helix issued no listenKey")
        with self._state_lock:
            self._listen_key = key
            self._listen_key_at = time.time()
        return key

    def _on_private(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log("unparseable private frame: %s" % raw[:120])
            return
        kind = msg.get("e")
        if kind == "ORDER_TRADE_UPDATE":
            o = msg.get("o") or {}
            if o.get("orderId") is not None:
                self._absorb_order(o)
        elif kind == "ACCOUNT_UPDATE":
            a = msg.get("a") or {}
            # The frame's own timestamp, translated onto this box's clock, is
            # what orders this snapshot against the REST reads it races with.
            when = _num(msg.get("T"))
            as_of = ((when - self.api.offset_ms) / 1000.0 if when
                     else time.time())
            self._absorb_positions(a.get("positions") or [], as_of)
            self.emit("account.updated", {
                "balance": _num(a.get("balance")),
                "equity": _num(a.get("equity")),
                # ACCOUNT_UPDATE carries balance, equity and the positions and
                # nothing else, so the two margin figures are computed from the
                # positions it did send, on the venue's own published rate.
                "margin_used": self._margin_used(a.get("positions") or []),
                "margin_free": round(
                    _num(a.get("equity"))
                    - self._margin_used(a.get("positions") or []), 2),
                "currency": self.account_currency})
        else:
            log("unknown private event %r" % kind)

    def _margin_used(self, rows: list) -> float:
        total = 0.0
        for r in rows or []:
            try:
                i = self._instrument(r.get("symbol"))
            except AdapterError:
                continue
            total += (_num(r.get("qty")) * _num(r.get("entryPrice"))
                      * _num(i.get("marginRate")))
        return round(total, 2)

    # == the poller ====================================================
    def _poll_loop(self):
        """The backstop for everything the streams are supposed to say.

        It sleeps unless something is in flight, so an idle adapter spends
        almost none of the weight budget; a fill or a close wakes it.
        """
        while not self._stop.is_set():
            self._wake.wait(2.0)
            self._wake.clear()
            try:
                self._keepalive()
                self._reap_orders()
                self._reap_positions()
                self._reap_bars()
                self._prune()
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log("poll: %s: %s" % (type(e).__name__, e))

    def _keepalive(self):
        with self._state_lock:
            key, at = self._listen_key, self._listen_key_at
        if not key or time.time() - at < LISTEN_KEY_KEEPALIVE:
            return
        try:
            self.api.call("PUT", "/api/v1/listenKey", {"listenKey": key})
            with self._state_lock:
                self._listen_key_at = time.time()
        except HelixError as e:
            log("listenKey keepalive failed: %s" % e)

    def _reap_orders(self):
        with self._state_lock:
            inflight = [dict(r) for r in self._orders.values()
                        if r.get("state") in (None, "accepted", "resting")
                        and r.get("filled", 0.0) < r.get("qty", 0.0) - 1e-12]
        for rec in inflight:
            if rec.get("type") != "market" and rec.get("state") == "resting":
                continue        # resting on the book is not "in flight"
            try:
                o = self.api.call("GET", "/api/v1/order",
                                  {"symbol": rec["symbol"],
                                   "orderId": rec["order_id"]})
            except HelixError as e:
                log("poll order %s: %s" % (rec["order_id"], e))
                continue
            self._absorb_order(o)
            with self._state_lock:
                cur = self._orders.get(rec["order_id"]) or {}
                overdue = (cur.get("filled", 0.0) < cur.get("qty", 0.0) - 1e-12
                           and time.time() > cur.get("deadline", 0)
                           and not cur.get("degraded"))
                if overdue:
                    cur["degraded"] = True
            if overdue:
                # SPEC 8.4: an order that has not terminated inside the
                # declared timeout is reported, not quietly waited on.
                self._set_session(
                    "degraded",
                    "order %s has not reached a terminal state within %ds"
                    % (rec["order_id"], ORDER_DEADLINE))

    def _reap_positions(self):
        with self._state_lock:
            interesting = bool(self._pos) or bool(self._closing)
        if interesting:
            try:
                self._account_raw()
            except HelixError as e:
                log("poll account: %s" % e)
        # A close the venue accepted but never carried out leaves a job behind.
        # SPEC 8.4: say so rather than holding it silently forever.
        with self._state_lock:
            stale = [s for s, j in self._closing.items()
                     if time.time() - j.get("since", 0) > ORDER_DEADLINE]
            for s in stale:
                self._closing.pop(s, None)
        for s in stale:
            self._set_session("degraded",
                              "close of %s unconfirmed after %ds"
                              % (s, ORDER_DEADLINE))

    def _reap_bars(self):
        now = time.time()
        with self._state_lock:
            due = [(k, v[0]) for k, v in self._bars.items() if v[1] <= now]
            for k, _ in due:
                self._bars[k][1] = now + BAR_POLL
        for (symbol, tf), last in due:
            try:
                rows = self.api.call("GET", "/api/v1/klines",
                                     {"symbol": symbol,
                                      "interval": TF_TO_INTERVAL[tf],
                                      "limit": 2}, signed=False)
            except HelixError as e:
                log("bar poll %s %s: %s" % (symbol, tf, e))
                continue
            for r in rows or []:
                t = int(_num(r[0]))
                if t <= last:
                    continue
                # klines returns completed candles only, so anything new here
                # is already closed and safe to publish (SPEC: CLOSED bars).
                self.emit("bar", {"symbol": symbol, "timeframe": tf,
                                  "bar": [t, _num(r[1]), _num(r[2]),
                                          _num(r[3]), _num(r[4]), _num(r[5])]})
                with self._state_lock:
                    if (symbol, tf) in self._bars:
                        self._bars[(symbol, tf)][0] = t

    def _prune(self):
        """A long-lived adapter must not carry one record per order forever."""
        cutoff = self.now_ms() - 3_600_000
        with self._state_lock:
            for oid, rec in list(self._orders.items()):
                if (rec.get("state") in ("filled", "cancelled", "expired")
                        and (rec.get("done_at") or 0) < cutoff):
                    del self._orders[oid]

    def _sync_orders(self):
        try:
            for o in self.api.call("GET", "/api/v1/openOrders", {}) or []:
                self._absorb_order(o)
        except HelixError as e:
            log("order sync failed: %s" % e)

    def _set_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a pipe
    # buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    HelixAdapter().run()


if __name__ == "__main__":
    main()
