#!/usr/bin/env python3
"""Brue Connect adapter for Atlas Clearing (institutional execution desk).

Atlas is two protocols wearing one API key: ordinary JSON REST under `/rest`
for everything that mutates or enumerates, and STOMP 1.2 carried inside a
WebSocket under `/stomp` for prices and a private per-account execution feed.
There is no third-party client library in this process: RFC 6455 (the
handshake, masked client frames, the length forms, ping/pong, close) and STOMP
on top of it (CONNECT/CONNECTED, SUBSCRIBE, MESSAGE, DISCONNECT, NUL-terminated
frames, negotiated heart-beating) are both implemented here from `socket`,
`ssl`, `struct`, `hashlib` and `base64`, because an adapter that needs a pip
install is an adapter the user's bare system python cannot run.

Everything Atlas says about time is epoch SECONDS (often fractional);
everything Brue Connect says about time is epoch MILLISECONDS, so the
conversion happens once, at this boundary, in `_ms()` and nowhere else.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no registration, no account read, no
    socket. Registration is especially costly to do early, because at Atlas a
    new registration is a NEW EMPTY ACCOUNT: handshaking a picker would
    silently orphan yesterday's positions. All of it lives in
    `on_first_call`, and the issued key is persisted in the profile's own
    state directory so a restart returns to the SAME account.
  * one background thread owns the WebSocket. It re-establishes the socket on
    any failure, re-SUBSCRIBEs every price topic and the execution queue, and
    re-reads orders and positions afterwards, because the execution queue is
    explicitly "going forward from the moment you subscribe (no backfill, no
    snapshot)": a gap is only recoverable from REST.
  * the feed is treated as AT LEAST ONCE and every broker fact goes through a
    state machine keyed on the venue's own ids (fillId, orderId + state,
    positionId), so a repeated frame is a no-op instead of a second fill on
    the platform's book. The synchronous REST responses feed the SAME state
    machine, which is why a market order reports `order.filled` the instant
    the POST returns and the identical frame arriving later changes nothing.
  * REST is the backstop for everything the feed is supposed to say: resting
    orders in flight and closes in flight are polled to a conclusion, and a
    position that vanishes without a frame is reported closed with
    `reason: "unknown"` rather than guessed at.

Venue quirks that shaped the code, each also commented where it bites:

  * the executions feed's documented `"type"` discriminator is DESTROYED on
    every ORDER_UPDATE (the order payload carries its own `type`, MARKET or
    LIMIT, and wins the merge), so messages are classified structurally.
  * a bare STOP carries its trigger in `stopPrice` when placed and in
    `limitPrice` when amended. Documented by Atlas, and handled in one place.
  * `Idempotency-Key` replay returns the ORIGINAL order even when the body is
    completely different, so a conflicting reuse looks like a success. This
    adapter refuses it as `duplicate_client_id` itself (SPEC 6).
  * `POST /rest/orders` is capped at 10 per rolling second and counts REFUSED
    attempts, so walking a catalog of shut instruments will hit the cap. A
    local budget sits under it deliberately rather than discovering it.

Configuration: `ATLAS_BASE_URL` (default http://127.0.0.1:7661) and the
adapter's private state dir `BRUE_BROKER_STATE`. Optional `ATLAS_DESK` is the
free-text desk label Atlas echoes nowhere and only stores. No credential ever
appears in a protocol message (SPEC 8.5).
"""
from __future__ import annotations

import base64
import hashlib
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

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

# The venue root, NOT the /rest prefix: the same host and port carry REST at
# /rest and the STOMP WebSocket at /stomp, so one variable configures both. A
# base that already ends in /rest is accepted and trimmed, because that is the
# string the documentation prints and somebody will paste it.
BASE = (os.environ.get("ATLAS_BASE_URL")
        or "http://127.0.0.1:7661").rstrip("/")
if BASE.endswith("/rest"):
    BASE = BASE[:-5]
DESK = os.environ.get("ATLAS_DESK", "lse-terminal")

# Atlas caps POST /rest/orders at 10 per rolling second per key and counts
# every attempt that carried an Idempotency-Key, including the ones it then
# refuses. A platform reconciling a catalog by trying an order on each symbol
# therefore hits the cap with REJECTIONS, which is exactly what the conformance
# suite does. Sit under the cap with headroom instead: a 429 costs a whole
# request cycle, a short wait costs milliseconds.
ORDER_RATE_MAX = 8
ORDER_RATE_WINDOW = 1.2

# STOMP heart-beating. Atlas offers exactly one contract besides "off": ask for
# anything nonzero and it answers 10000,10000, sends a lone LF every 10 s, and
# drops a socket that has been silent for 20 s. Accepting that contract means
# honouring it, so this sends its own LF at half the interval (a beat lost to
# scheduling must not cost the connection) and treats a longer silence than the
# venue's own promise as a dead socket worth replacing.
HEARTBEAT_MS = 10000
HEARTBEAT_SEND = 5.0
STREAM_READ_TIMEOUT = 25.0

# How long a close waits for the venue's own POSITION_CLOSED frame before the
# poller finishes it from REST. The frame carries the exact realised P/L, the
# exact close price and the REASON; everything reconstructed without it is
# arithmetic standing in for a broker number, so the frame is worth waiting for.
CLOSE_CONFIRM_GRACE = 2.0

# SPEC 8.4: a mutating call that has not reached a terminal state inside the
# declared timeout is reported as a degraded session rather than waited on.
ORDER_TIMEOUT = 30.0

TF_TO_GRANULARITY = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
                     "1h": "H1", "4h": "H4", "1d": "D"}
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
              "4h": 14400, "1d": 86400}

# Atlas's asset classes -> the SPEC's asset_class vocabulary. SPECIAL_FUTURE is
# Atlas's own product name for a cash-settled perpetual on a US equity
# reference; it files under the generic "future" because a platform grouping a
# picker should not need to learn one venue's marketing. That they never expire
# is said in `expires_at: null`, which is the field the SPEC added for it.
ASSET_CLASS = {"FX": "fx", "METAL": "metal", "INDEX": "index",
               "SPECIAL_FUTURE": "future"}

# Atlas publishes trading hours as a `calendar` object with two shapes. Both are
# translated into the SPEC's session windows; anything unrecognised publishes NO
# calendar rather than a guessed one (SPEC 3: an empty list means "no calendar
# published", NOT "closed", and the adapter stays the final judge either way).
#
# "Sun 21:05 UTC" through "Fri 21:00 UTC" with no daily close is one continuous
# week, so it is three windows and not five: Sunday's tail, four whole days, and
# Friday's head. Writing it as 24h Mon-Fri would claim the venue is shut on
# Sunday evening, when it is dealing.
SESSIONS_24_5 = [
    {"days": [7], "open": "21:05", "close": "24:00", "tz": "UTC"},
    {"days": [1, 2, 3, 4], "open": "00:00", "close": "24:00", "tz": "UTC"},
    {"days": [5], "open": "00:00", "close": "21:00", "tz": "UTC"},
]
# Explicitly seven days, NOT an empty list: "always open" and "no calendar
# published" look identical otherwise and only one of them is safe for a
# platform to gate orders on (SPEC 3).
SESSIONS_24_7 = [
    {"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00",
     "tz": "UTC"},
]

# Atlas's wire error codes -> the SPEC section 6 closed set, with the HTTP
# status where the same code means two things.
#   INVALID_UNITS is deliberately one code for a bad size, a bad side, a bad
#   type, a bad timeInForce and a missing price (the venue's own table says
#   so), which is why sizes and sides are validated HERE against the catalog:
#   the platform then gets `invalid_qty` for a size problem and only a size
#   problem.
#   MISSING_IDEMPOTENCY_KEY can only mean this adapter forgot to send one,
#   which is a fault in this file, not a broker condition; `internal` is the
#   honest code for it.
ERROR_MAP = {
    "UNAUTHORIZED": ("unauthorized", False),
    "UNKNOWN_SYMBOL": ("unknown_symbol", False),
    "INVALID_UNITS": ("invalid_qty", False),
    "INSUFFICIENT_MARGIN": ("insufficient_margin", False),
    "MARKET_CLOSED": ("market_closed", False),
    "GTD_IN_PAST": ("invalid_expiry", False),
    "ORDER_NOT_WORKING": ("not_resting", False),
    "NOT_FOUND": ("not_found", False),
    "MISSING_IDEMPOTENCY_KEY": ("internal", False),
    "RATE_LIMITED": ("rate_limited", True),
    "INTERNAL": ("internal", True),
}

# Atlas order states -> the SPEC's Order.state vocabulary. RECEIVED is a
# transient the documentation says you will not normally observe over REST, but
# it DOES arrive on the executions queue (every order is announced there before
# it is matched), so it is mapped rather than left to fall through.
ORDER_STATE = {"RECEIVED": "resting", "WORKING": "resting",
               "COMPLETE": "filled", "CANCELLED": "cancelled",
               "EXPIRED": "expired", "REJECTED": "rejected"}

TYPE_TO_WIRE = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP",
                "stop_limit": "STOP_LIMIT"}
WIRE_TO_TYPE = {v: k for k, v in TYPE_TO_WIRE.items()}

# Atlas's close reasons -> the SPEC's closed reason set on `position.closed`.
# Atlas calls a flatten leg "manual" like any other client close, so the
# adapter's own reason wins where it knows better (see `_close_position`).
CLOSE_REASON = {"manual": "manual", "tp": "tp", "sl": "sl",
                "stop_out": "stop_out", "expired": "expired"}

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[atlas] {msg}", file=sys.stderr, flush=True)


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _ms(sec) -> int:
    """Atlas epoch seconds (int or decimal) -> epoch milliseconds (SPEC 2)."""
    if sec is None:
        return 0
    try:
        return int(round(float(sec) * 1000.0))
    except (TypeError, ValueError):
        return 0


def _sec(ms) -> float:
    """Epoch milliseconds -> the epoch seconds Atlas expects back."""
    return _num(ms) / 1000.0


class AtlasError(Exception):
    """An {"error": {"code", "message"}} response from Atlas."""

    def __init__(self, status: int, body):
        self.status = status
        body = body if isinstance(body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        self.code = err.get("code") or f"HTTP-{status}"
        self.detail = err.get("message") or str(body)[:200]
        super().__init__(f"{self.code} ({status}): {self.detail}")


def to_adapter_error(e: AtlasError) -> AdapterError:
    hit = ERROR_MAP.get(e.code)
    if hit is None:
        # An unknown venue code keeps its identity as an x_ extension rather
        # than being flattened to `internal`, which would destroy the only
        # information the platform had (SPEC 6).
        code, retry = "x_atlas_" + e.code.lower().replace("-", "_"), False
    else:
        code, retry = hit
    return AdapterError(code, f"{e.code}: {e.detail}", retry)


# ======================================================================
# RFC 6455: a WebSocket client, by hand
# ======================================================================

class WebSocket:
    """One client WebSocket connection over a blocking socket.

    Only what a STOMP feed needs, but that includes the parts a naive
    implementation skips and a real server eventually uses: continuation
    frames (a long MESSAGE may arrive fragmented), control frames interleaved
    mid-message (ping must be answered or the peer disconnects), and the two
    extended length forms. Client-to-server frames are ALWAYS masked; an
    unmasked client frame is a protocol error the server must close on, and
    that is the single most common hand-rolled-client bug.
    """

    def __init__(self, url: str, timeout: float = 20.0):
        parsed = urllib.parse.urlsplit(url)
        self.secure = parsed.scheme == "wss"
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if self.secure else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.sock = None
        self._buf = bytearray()
        # One writer at a time: the heartbeat thread and any handler thread
        # both send, and two interleaved frames on one socket is a corrupt
        # stream, not two messages.
        self._send_lock = threading.Lock()
        self._closed = False

    # -- lifecycle ----------------------------------------------------
    def connect(self):
        raw = socket.create_connection((self.host, self.port), self.timeout)
        if self.secure:
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=self.host)
        self.sock = raw
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (f"GET {self.path} HTTP/1.1\r\n"
               f"Host: {self.host}:{self.port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {nonce}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode("ascii"))
        head = self._read_http_head()
        status = head.split("\r\n", 1)[0]
        if " 101" not in status:
            raise OSError(f"websocket upgrade refused: {status.strip()!r}")
        # The accept hash is checked, not assumed. It is the only proof that
        # what answered is a WebSocket peer that read OUR key, rather than a
        # proxy or a cached 101 replaying someone else's handshake.
        want = base64.b64encode(hashlib.sha1(
            (nonce + WS_GUID).encode("ascii")).digest()).decode("ascii")
        got = ""
        for line in head.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        if got != want:
            raise OSError("websocket accept hash mismatch")
        self.sock.settimeout(STREAM_READ_TIMEOUT)

    def _read_http_head(self) -> str:
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("connection closed during websocket handshake")
            self._buf.extend(chunk)
        head, _, rest = bytes(self._buf).partition(b"\r\n\r\n")
        self._buf = bytearray(rest)
        return head.decode("latin-1")

    def close(self, code: int = 1000):
        """Best-effort closing handshake, then the socket."""
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, struct.pack("!H", code))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # -- frames -------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes):
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            if self._closed or self.sock is None:
                raise OSError("websocket is closed")
            self.sock.sendall(header + mask + masked)

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def _need(self, n: int):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("websocket closed by peer")
            self._buf.extend(chunk)

    def _read_frame(self):
        """One raw frame: (fin, opcode, payload)."""
        self._need(2)
        b0, b1 = self._buf[0], self._buf[1]
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        length, offset = b1 & 0x7F, 2
        if length == 126:
            self._need(4)
            length = struct.unpack("!H", bytes(self._buf[2:4]))[0]
            offset = 4
        elif length == 127:
            self._need(10)
            length = struct.unpack("!Q", bytes(self._buf[2:10]))[0]
            offset = 10
        mask = None
        if b1 & 0x80:   # a server MUST NOT mask, but decode it if it does
            self._need(offset + 4)
            mask = bytes(self._buf[offset:offset + 4])
            offset += 4
        self._need(offset + length)
        payload = bytes(self._buf[offset:offset + length])
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        del self._buf[:offset + length]
        return fin, opcode, payload

    def recv_text(self) -> str:
        """The next complete TEXT message, control frames handled in passing.

        Blocks until a whole message is assembled: a fragmented message is one
        message, and handing its first fragment to a STOMP parser would produce
        a frame with a truncated body and no NUL.
        """
        parts: list[bytes] = []
        kind = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:                      # close
                raise OSError("websocket closed by peer")
            if opcode == 0x9:                      # ping -> pong, same payload
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode in (0x1, 0x2):
                kind = opcode
                parts = [payload]
            elif opcode == 0x0:
                parts.append(payload)
            if fin:
                if kind == 0x1:
                    return b"".join(parts).decode("utf-8", "replace")
                parts, kind = [], None             # binary: not our protocol


# ======================================================================
# STOMP 1.2, on top of that
# ======================================================================

def stomp_frame(command: str, headers: dict, body: str = "") -> str:
    """One STOMP frame, NUL-terminated.

    No header escaping is applied on purpose: Atlas states it "splits only on
    the first colon" and asks for destinations that CONTAIN a colon
    (/topic/prices/FX:EURUSD). Escaping the colon per the STOMP 1.2 letter
    would send `FX\\cEURUSD`, which this venue would not recognise. The values
    this adapter sends are symbols and ids from the venue's own tables, so
    none of them can carry a newline.
    """
    out = [command]
    out.extend(f"{k}:{v}" for k, v in headers.items())
    return "\n".join(out) + "\n\n" + body + "\0"


def parse_stomp(text: str):
    """One frame from the wire -> (command, headers, body), or None for a beat.

    A heart-beat is a bare LF and not a frame at all, and a frame may arrive
    with beats stuck to the front of it, so leading newlines are stripped
    before anything is parsed.
    """
    text = text.lstrip("\r\n")
    if not text:
        return None
    if text.endswith("\0"):
        text = text[:-1]
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    command = lines[0].strip()
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        # First value wins, per STOMP 1.2's repeated-header rule.
        headers.setdefault(name, value)
    return command, headers, body


# ======================================================================
# REST
# ======================================================================

class AtlasClient:
    """REST, with the API key, the order-rate budget and retries inside.

    One place owns the key so no caller has to think about registration, and
    one place owns the order budget so no caller can spend someone else's.
    """

    def __init__(self, base: str, cred_path: str):
        self.base = base
        self.cred_path = cred_path
        self._creds = None
        self._lock = threading.Lock()
        self._order_calls: list[float] = []
        self._ctx = ssl.create_default_context()

    # -- credentials --------------------------------------------------
    def credentials(self) -> dict:
        """Register once, then reuse forever.

        A registration is a NEW EMPTY ACCOUNT ("anyone may create as many demo
        accounts as they need"), so registering on every start would orphan
        yesterday's positions and quietly hand the user a fresh balance. The
        file lives in the profile's own state directory, mode 0600, written
        atomically: two profiles wrapping this adapter must not share an
        account, and a half-written credential file must never exist.
        """
        with self._lock:
            if self._creds:
                return self._creds
            if os.path.exists(self.cred_path):
                try:
                    with open(self.cred_path) as fh:
                        c = json.load(fh)
                    if c.get("apiKey"):
                        self._creds = c
                        return c
                except (OSError, json.JSONDecodeError) as e:
                    log(f"credential file unreadable ({e}); registering afresh")
            c = self._raw("POST", "/rest/register", body={"desk": DESK})
            c = {"apiKey": c.get("apiKey"), "accountId": c.get("accountId"),
                 "base": self.base}
            self._persist(c)
            log(f"registered desk {DESK!r} -> account {c.get('accountId')}")
            self._creds = c
            return c

    def _persist(self, creds: dict):
        try:
            os.makedirs(os.path.dirname(self.cred_path) or ".", exist_ok=True)
            tmp = self.cred_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(creds, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cred_path)
        except OSError as e:
            log(f"could not persist credentials to {self.cred_path}: {e}")

    def forget(self):
        """Drop a key the venue no longer knows, so the next call registers.

        A key that is refused IS no key: holding it only means every call
        fails with `unauthorized` forever. This happens when a state directory
        outlives the venue's own book (a reset venue, a different Atlas
        instance behind the same URL).
        """
        with self._lock:
            self._creds = None
        try:
            os.remove(self.cred_path)
        except OSError:
            pass

    @property
    def api_key(self) -> str:
        return self.credentials().get("apiKey") or ""

    @property
    def account_id(self) -> str:
        return self.credentials().get("accountId") or ""

    # -- the wire -----------------------------------------------------
    def _order_budget(self):
        """Block until an order POST fits under Atlas's 10-per-second cap."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._order_calls = [t for t in self._order_calls
                                     if now - t < ORDER_RATE_WINDOW]
                if len(self._order_calls) < ORDER_RATE_MAX:
                    self._order_calls.append(now)
                    return
                wait = ORDER_RATE_WINDOW - (now - self._order_calls[0]) + 0.01
            time.sleep(min(max(wait, 0.01), 1.5))

    def _raw(self, method: str, path: str, body=None, headers=None,
             timeout: float = 15.0):
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=self._ctx) as r:
                raw = r.read().decode() or "{}"
                out = json.loads(raw)
                if isinstance(out, dict):
                    out["_status"] = r.status
                return out
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"error": {"code": f"HTTP-{e.code}",
                                    "message": raw[:200]}}
            raise AtlasError(e.code, parsed) from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            # Reachability and garbage answers are both "the venue did not
            # answer": status 0 marks them retryable for `call`.
            raise AtlasError(0, {"error": {
                "code": "INTERNAL",
                "message": f"{type(e).__name__}: {e}"}}) from None

    def call(self, method: str, path: str, body=None, headers=None,
             retries: int = 2, timeout: float = 15.0):
        """An authenticated /rest call, with the key and the retries here."""
        is_order_post = method == "POST" and path == "/rest/orders"
        for attempt in range(retries + 1):
            hdrs = {"X-ATLAS-KEY": self.api_key}
            hdrs.update(headers or {})
            if is_order_post:
                self._order_budget()
            try:
                return self._raw(method, path, body=body, headers=hdrs,
                                 timeout=timeout)
            except AtlasError as e:
                last = attempt >= retries
                if last:
                    raise
                if e.code == "RATE_LIMITED":
                    # The local budget should make this unreachable; if the
                    # venue disagrees, its answer wins.
                    time.sleep(0.35)
                    continue
                if e.status in (0, 500, 502, 503, 504):
                    # Safe to repeat: every mutating POST here carries an
                    # Idempotency-Key, so a retry of a request that DID land
                    # returns the original order rather than a second one.
                    time.sleep(0.3)
                    continue
                raise


# ======================================================================
# the adapter
# ======================================================================

class AtlasAdapter(Adapter):
    name = "atlas"
    version = "1.0"
    broker = "Atlas Clearing"
    # Truthful (SPEC 8.2): the venue's own documentation opens with "Atlas
    # Clearing is a simulated venue for integration testing. All prices are
    # synthetic. No real money is involved."
    mode = "paper"
    # "Each account starts with 500,000.00 USD of demo balance"; re-checked
    # against the account on first call and complained about on stderr if the
    # venue ever disagrees.
    account_currency = "USD"
    # Atlas's own words: "Atlas books on a hedging model, not a netting one.
    # Every order that opens exposure creates its own new position." Closes
    # therefore target a positionId and never a symbol.
    account_model = "hedging"

    def __init__(self, out=None):
        super().__init__(out)
        state = os.environ.get("BRUE_BROKER_STATE") or os.path.expanduser(
            "~/.brue-connect/state/atlas")
        self.api = AtlasClient(BASE, os.path.join(state, "credentials.json"))
        self._lock = threading.RLock()
        self._instruments: dict[str, dict] = {}   # wire symbol -> venue row
        self._orders: dict[str, dict] = {}        # order_id -> our view
        self._by_client: dict[str, str] = {}      # client_id -> order_id
        self._fills_seen: set = set()             # fillId -> emitted once
        self._placing = 0                         # placements in flight
        self._parked: dict[str, list] = {}        # order_id -> early frames
        self._positions: dict[str, dict] = {}     # position_id -> our view
        self._closing: dict[str, dict] = {}       # position_id -> close job
        self._final_closed: set = set()           # position_id -> reported gone
        self._quote_subs: dict[str, str] = {}     # symbol -> stomp sub id
        self._last_quote: dict[str, dict] = {}
        self._bars: dict[tuple, int] = {}         # (symbol, tf) -> last bar sec
        self._cross: dict[str, tuple] = {}        # ccy -> (rate, read_at)
        self._sub_seq = 0
        self._ws = None
        self._ws_gen = 0
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._session_state = None
        self._last_positions_poll = 0.0

    # == handshake =====================================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # stopLoss/takeProfit ride on the order that opens the
                # position and can be changed afterwards with
                # PATCH /rest/positions/{id}.
                "brackets": True,
                "modify": True,
                # Atlas's engine has a trailing field but its REST surface
                # exposes no way to set one: neither the order body nor the
                # position PATCH accepts it. Declared false so the platform
                # refuses the feature loudly instead of emulating a trail by
                # sending a modification on every tick (SPEC 3).
                "trailing": False,
                # POST /rest/positions/{id}/close takes a `units` field, and
                # "closing more than a position's remaining size closes only
                # what remains".
                "partial_close": True,
                "pending": True,
                "pending_types": ["limit", "stop", "stop_limit"],
                # GTC and GTD only: a `day` timeInForce is refused by the
                # venue before any other validation.
                "expiry": ["gtc", "gtd"],
                # Atlas checks margin when the order is POSTed, including a
                # resting one, but reserves nothing: marginUsed is the sum of
                # OPEN POSITIONS' margin, so an accepted resting order holds
                # no capital and can still die at the moment it should have
                # filled. That consequence is what the SPEC's boolean is for.
                "reserves_margin": False,
                "oco": False,
                # No close-by endpoint. On a hedging book it would have been
                # the useful one, and saying so beats emulating it with two
                # closes and a spread the client did not agree to.
                "close_by": False,
                "can_list": True,          # GET /rest/orders enumerates
            },
            "data": {
                "quotes": True, "bars": True, "history": True,
                # No ledger of non-trade money movement exists at Atlas: no
                # deposits, no dividends, no financing, no adjustments. A
                # balance that moves without a fill genuinely has no
                # explanation available here.
                "operations": False,
                # Atlas states no cap on subscriptions and the whole tradable
                # universe is eleven symbols on one socket. Absent means "the
                # broker states no limit" (SPEC 3), which is the truth.
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # The only documented limit is on order submission: 10 per rolling
        # second per key. Nothing else is throttled, so the handshake states
        # the number that actually binds a platform.
        return {"requests_per_second": 10}

    def broker_info(self) -> dict:
        # The venue's own words only. Atlas publishes no licence, no website
        # and no support address, and inventing them is exactly what SPEC 3.1
        # says this field is not for.
        return {"display_name": "Atlas Clearing",
                "tagline": "Chicago institutional execution and clearing desk",
                "mark": "AC",
                "asset_classes": ["FX", "Metals", "Indices",
                                  "Special Futures"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the venue something happens here, not in
        `hello` (SPEC 3.1): the key, the catalog, the account, the feed."""
        try:
            account = self._account_raw()
        except AdapterError as e:
            if e.code != "unauthorized":
                raise
            # A stored key the venue no longer knows (its book was reset, or
            # this URL is now a different Atlas). Registering again is the
            # only way back, and it is safe precisely because the old account
            # is unreachable anyway.
            log("stored API key was refused; registering a new demo account")
            self.api.forget()
            account = self._account_raw()
        if account.get("currency") and account["currency"] != self.account_currency:
            # Cannot be corrected after the fact: `hello` has already gone out.
            # Loud on stderr is the only honest move left.
            log(f"WARNING: account currency is {account['currency']!r} but the "
                f"handshake declared {self.account_currency!r}")
        self._load_instruments()
        self._sync_positions()
        self._sync_orders()
        self._load_position_fees()
        self._stop.clear()
        threading.Thread(target=self._stream_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        ws = self._ws
        if ws is not None:
            try:
                # A courteous DISCONNECT with a receipt, which Atlas answers
                # before it closes the socket. Dropping the TCP connection
                # instead works too, but leaves the venue to time the socket
                # out rather than being told.
                ws.send_text(stomp_frame("DISCONNECT", {"receipt": "bye-1"}))
            except OSError:
                pass
            ws.close()

    # == catalog =======================================================
    def _load_instruments(self):
        data = self._call("GET", "/rest/instruments")
        with self._lock:
            self._instruments = {i["symbol"]: i
                                 for i in data.get("instruments") or []}

    def _instrument(self, symbol) -> dict:
        with self._lock:
            inst = self._instruments.get(symbol)
        if inst is None:
            self._load_instruments()           # catalogs change; re-read once
            with self._lock:
                inst = self._instruments.get(symbol)
        if inst is None:
            raise AdapterError("unknown_symbol", f"no such symbol {symbol!r}")
        return inst

    @staticmethod
    def _sessions_for(inst: dict) -> list:
        cal = inst.get("calendar") or {}
        daily = str(cal.get("daily") or "").lower()
        if daily == "24/7" or str(cal.get("open") or "").lower() == "always":
            return [dict(s) for s in SESSIONS_24_7]
        if daily == "24h" and "sun" in str(cal.get("open") or "").lower():
            return [dict(s) for s in SESSIONS_24_5]
        log(f"unrecognised calendar for {inst.get('symbol')}: {cal!r}; "
            f"publishing no calendar")
        return []

    def _spec(self, inst: dict) -> dict:
        symbol = inst["symbol"]
        digits = int(_num(inst.get("priceDecimals"), 2))
        tick = round(10.0 ** -digits, max(digits, 1))
        contract = _num(inst.get("contractSize"), 1.0)
        rate = _num(inst.get("commissionPerUnit"))
        asset = ASSET_CLASS.get((inst.get("assetClass") or "").upper(), "cfd")
        base_ccy = None
        if asset == "fx":
            # "FX:EURUSD" -> EUR against USD. The pair code is the only place
            # Atlas encodes a base currency at all.
            tail = symbol.split(":")[-1]
            if len(tail) == 6:
                base_ccy = tail[:3]
        return {
            "symbol": symbol,
            "name": inst.get("description") or symbol,
            "asset_class": asset,
            "base_ccy": base_ccy,
            "quote_ccy": inst.get("quoteCurrency"),
            "digits": digits,
            "tick_size": tick,
            # Value of one tick per one contract, in quote_ccy. Atlas quotes
            # every product in its own contract currency, so the SPEC's own
            # default derivation is exact here rather than an approximation.
            "tick_value": round(contract * tick, 10),
            "lot_step": _num(inst.get("unitStep"), 1.0),
            "min_qty": _num(inst.get("minUnits"), 0.0),
            "max_qty": _num(inst.get("maxUnits"), 0.0),
            "min_notional": 0,
            "contract_size": contract,
            # THE percent-form trap: initialMarginPct is a PERCENTAGE (3.33
            # means 3.33%), so the SPEC's fractional margin_rate is /100
            # exactly once.
            "margin_rate": _num(inst.get("initialMarginPct")) / 100.0,
            # Atlas stops out "an account whose equity / marginUsed falls to
            # 0.5 or below", which is half of initial margin expressed as a
            # maintenance requirement. Stated rather than defaulted, because a
            # platform running stop-out math on the initial rate would warn a
            # user twice as early as the venue actually acts.
            "margin_maintenance_rate": _num(
                inst.get("initialMarginPct")) / 200.0,
            "margin_tiers": None,
            "sessions": self._sessions_for(inst),
            # Atlas publishes no holiday or early-close calendar. Empty means
            # "none published"; the venue remains the final judge and answers
            # MARKET_CLOSED.
            "holidays": [],
            "early_closes": [],
            "tradable": True,
            "shortable": True,
            "trade_mode": "full",
            # No minimum stop distance is documented or enforced: a stop one
            # tick from the market is accepted.
            "stops_level_points": 0,
            # "commissionPerUnit x units per trade, floored at a per-instrument
            # minimum". The minimum equals the per-unit rate on every Atlas
            # row, which is why any order below 1.0 unit pays the flat floor.
            "fees": {"commission_per_lot": rate, "mode": "per_lot",
                     "rate": rate, "min": rate},
            # Atlas charges no financing of any kind: Special Futures state it
            # outright and no other product publishes a swap. 0 with mode
            # "none" says "not charged", which is different from 0 points.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            # Nothing at Atlas expires: the Special Futures are perpetual and
            # everything else is spot.
            "expires_at": None, "successor": None, "dividend": None,
        }

    def handle_catalog_list(self, params):
        self._load_instruments()
        with self._lock:
            rows = list(self._instruments.values())
        return {"instruments": [self._spec(i) for i in rows]}

    # == plumbing ======================================================
    def _call(self, method: str, path: str, body=None, headers=None, **kw):
        try:
            return self.api.call(method, path, body=body, headers=headers, **kw)
        except AtlasError as e:
            raise to_adapter_error(e) from None

    def _emit_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})

    # == account =======================================================
    def _account_raw(self) -> dict:
        return self._call("GET", "/rest/account")

    @staticmethod
    def _account_view(a: dict) -> dict:
        return {"balance": _num(a.get("balance")),
                "equity": _num(a.get("equity")),
                "margin_used": _num(a.get("marginUsed")),
                "margin_free": _num(a.get("marginFree")),
                "currency": a.get("currency") or "USD"}

    def handle_account_get(self, params):
        return self._account_view(self._account_raw())

    def _usd_cross(self, ccy: str) -> float:
        """`ccy` per one USD... in the direction that converts INTO USD.

        Used only to value a close whose confirmation frame was lost on a
        non-USD instrument (UK100 in GBP, GER40 in EUR, USDJPY in JPY). Atlas
        has no quote endpoint, so the rate comes from the last closed M1 candle
        of the venue's own FX pair: its own published price, not an outside
        one. Cached for a minute, because this path is a fallback and must not
        become a per-close round trip.
        """
        if ccy == "USD" or not ccy:
            return 1.0
        hit = self._cross.get(ccy)
        if hit and time.time() - hit[1] < 60:
            return hit[0]
        pair, invert = {"EUR": ("FX:EURUSD", False),
                        "GBP": ("FX:GBPUSD", False),
                        "JPY": ("FX:USDJPY", True)}.get(ccy, (None, False))
        if pair is None:
            return 0.0
        try:
            data = self._call("GET", "/rest/candles?" + urllib.parse.urlencode(
                {"symbol": pair, "granularity": "M1", "limit": 1}))
            close = _num((data.get("candles") or [{}])[-1].get("c"))
        except (AdapterError, IndexError):
            return 0.0
        if close <= 0:
            return 0.0
        rate = (1.0 / close) if invert else close
        self._cross[ccy] = (rate, time.time())
        return rate

    # == positions =====================================================
    def _position_view(self, p: dict) -> dict:
        pid = str(p.get("positionId"))
        with self._lock:
            known = self._positions.get(pid) or {}
        out = {
            "position_id": pid,
            "symbol": p.get("symbol"),
            "side": "buy" if (p.get("side") or "").upper() == "BUY" else "sell",
            "qty": _num(p.get("units")),
            "avg_price": _num(p.get("entryPrice")),
            "sl": p.get("stopLoss"),
            "tp": p.get("takeProfit"),
            "trail_points": 0,
            "unrealized_pnl": _num(p.get("unrealizedPnl")),
            # Atlas publishes no financing at all, so 0 here is a fact and not
            # a shrug. Commission IS separated by the venue (each fill carries
            # its own), so the position reports what was really charged rather
            # than leaving it mixed into unrealized_pnl.
            "swap": 0.0,
            "commission": -round(_num(known.get("fee_left")), 2),
            "opened_at": _ms(p.get("openedAt")),
            "from_order": known.get("from_order"),
        }
        if known.get("client_id"):
            out["client_id"] = known["client_id"]
        return out

    def _positions_raw(self) -> list:
        data = self._call("GET", "/rest/positions")
        rows = data.get("positions") or []
        seen = set()
        with self._lock:
            for row in rows:
                pid = str(row.get("positionId"))
                seen.add(pid)
                rec = self._positions.setdefault(pid, {})
                rec["snapshot"] = row
                rec.setdefault("fee_total", 0.0)
                rec.setdefault("fee_left", 0.0)
                rec.setdefault("qty0", _num(row.get("units")))
            self._last_positions_poll = time.time()
        return rows

    def handle_positions_list(self, params):
        return {"positions": [self._position_view(p)
                              for p in self._positions_raw()]}

    def _sync_positions(self):
        try:
            self._positions_raw()
        except AdapterError as e:
            log(f"position sync failed: {e}")

    def _load_position_fees(self):
        """Attribute each open position's opening commission from the ledger.

        Atlas charges commission ONLY on the trade that opens a position and
        never on any close, so a position's whole fee life is the fill (or
        fills) that opened it, and `GET /rest/fills` is where those live. Read
        once at startup so positions adopted from a previous session can still
        report what they cost; live fills maintain it from then on.
        """
        try:
            fills = self._fills_page(limit=200)
        except AdapterError as e:
            log(f"could not read the fill ledger for position fees: {e}")
            return
        with self._lock:
            for f in fills:
                pid = str(f.get("positionId"))
                rec = self._positions.get(pid)
                if rec is None or f.get("orderId") is None:
                    continue
                fee = _num(f.get("commission"))
                rec["fee_total"] = _num(rec.get("fee_total")) + fee
                rec["fee_left"] = _num(rec.get("fee_left")) + fee

    def _charge_share(self, pid: str, qty: float) -> float:
        """The share of a position's commission that this close carries.

        Atlas charges once, at the open, so a partial close cannot be handed
        the whole ticket (two partials would report the fee twice) and cannot
        be handed nothing (the fee would vanish from the round trip the
        platform is showing). Pro-rata by the size the position opened at,
        with the last close taking whatever is left over from rounding.
        """
        with self._lock:
            rec = self._positions.get(pid) or {}
            total = _num(rec.get("fee_total"))
            left = _num(rec.get("fee_left"))
            qty0 = _num(rec.get("qty0")) or _num(qty) or 1.0
            share = min(left, total * (_num(qty) / qty0)) if total else 0.0
            rec["fee_left"] = round(left - share, 10)
        return round(share, 2)

    # == orders ========================================================
    def _validate_qty(self, inst: dict, qty) -> float:
        """Size checked against the catalog before the round trip.

        Atlas answers off-step, below-min, above-max, a bad side, a bad type
        and a missing price ALL with INVALID_UNITS, so its answer cannot be
        mapped back to a precise Brue code. Checking here means a size
        problem, and only a size problem, comes back as `invalid_qty`.
        """
        q = _num(qty, -1.0)
        step = _num(inst.get("unitStep"))
        lo = _num(inst.get("minUnits"))
        hi = _num(inst.get("maxUnits"))
        if q <= 0:
            raise AdapterError("invalid_qty",
                               f"qty must be positive, got {qty!r}")
        if lo and q < lo - 1e-9:
            raise AdapterError("invalid_qty", f"qty {q} below minUnits {lo}")
        if hi and q > hi + 1e-9:
            raise AdapterError("invalid_qty", f"qty {q} above maxUnits {hi}")
        if step > 0:
            units = q / step
            if abs(units - round(units)) > 1e-6:
                raise AdapterError(
                    "invalid_qty",
                    f"qty {q} is not a multiple of unitStep {step}")
            q = round(units) * step
        return round(q, 8)

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        otype = (params.get("type") or "market").lower()
        # The signature is everything that decides what gets DEALT. Atlas will
        # not police it for us: replaying an Idempotency-Key with a completely
        # different body returns the ORIGINAL order and a 200, which makes a
        # client bug look like a success (SPEC 6).
        sig = (symbol, side, _num(params.get("qty"), -1), otype,
               params.get("price"), params.get("stop_price"),
               params.get("sl"), params.get("tp"),
               params.get("expiry"), params.get("expiry_at"))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("invalid_qty",
                               f"side must be buy or sell, got "
                               f"{params.get('side')!r}")
        if otype not in TYPE_TO_WIRE:
            raise AdapterError("unsupported", f"unknown order type {otype!r}")
        if params.get("trail_points"):
            raise AdapterError(
                "unsupported",
                "Atlas has no trailing stop on its REST surface; a platform "
                "must not emulate one by modifying on every tick")
        inst = self._instrument(symbol)
        qty = self._validate_qty(inst, params.get("qty"))
        digits = int(_num(inst.get("priceDecimals"), 5))

        body = {"symbol": symbol, "side": side.upper(), "units": qty,
                "type": TYPE_TO_WIRE[otype]}
        if otype in ("limit", "stop_limit"):
            if params.get("price") is None:
                raise AdapterError("invalid_qty",
                                   f"a {otype} order needs `price`")
            body["limitPrice"] = round(_num(params["price"]), digits)
        if otype in ("stop", "stop_limit"):
            # A bare STOP has one meaningful level and Atlas carries it in
            # `stopPrice` at placement (and in `limitPrice` at amend; see
            # handle_order_amend). The SPEC gives a bare stop its trigger in
            # `price`, so either field is accepted here and lands in the one
            # place the venue reads.
            trigger = params.get("stop_price")
            if trigger is None and otype == "stop":
                trigger = params.get("price")
            if trigger is None:
                raise AdapterError("invalid_qty",
                                   f"a {otype} order needs `stop_price`")
            body["stopPrice"] = round(_num(trigger), digits)
        for key, field in (("sl", "stopLoss"), ("tp", "takeProfit")):
            if params.get(key) is not None:
                body[field] = round(_num(params[key]), digits)
        expiry = (params.get("expiry") or "gtc").lower()
        if expiry == "gtd":
            when = params.get("expiry_at")
            if not when:
                raise AdapterError("invalid_expiry",
                                   "expiry 'gtd' needs expiry_at")
            body["timeInForce"] = "GTD"
            # Atlas wants an epoch SECOND strictly in the future; a value that
            # rounds down onto the current second is refused as GTD_IN_PAST.
            body["expiresAt"] = int(_sec(when)) + 1
        elif expiry == "day":
            # Declared in capabilities.orders.expiry, so a platform should
            # never send it; Atlas honours GTC and GTD only.
            raise AdapterError("invalid_expiry",
                               "Atlas honours gtc and gtd only")
        elif expiry != "gtc":
            raise AdapterError("invalid_expiry", f"unknown expiry {expiry!r}")

        # THE RACE, and why the counter exists. Atlas matches a market order
        # INSIDE its POST handler, so the FILL frame is written to the
        # WebSocket before the HTTP response is written to the socket, and the
        # reader thread regularly wins. The order id, and therefore the only
        # mapping from it back to this `client_id`, does not exist in this
        # process until the response lands. An event emitted in that window
        # would carry `client_id: null`, and a platform waiting for its own
        # order would wait forever for a fill it had already been told about.
        # Nothing else can supply the mapping: Atlas's order rows do not echo
        # the Idempotency-Key. So while a placement is in flight, frames for
        # an unknown order are PARKED and replayed the moment the record
        # exists (and swept by the poller if they turn out to be another
        # session's order after all).
        with self._lock:
            self._placing += 1
        try:
            order = self.api.call("POST", "/rest/orders", body=body,
                                  headers={"Idempotency-Key": client_id})
        except AtlasError as e:
            with self._lock:
                self._placing -= 1
            err = to_adapter_error(e)
            # SPEC 8.4: no mutating call may be silently dropped. A synchronous
            # refusal is a terminal outcome and is announced as one, so a
            # platform watching only the event stream still sees the order end.
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": err.code,
                                         "message": str(err)})
            raise err from None

        order_id = str(order.get("orderId"))
        with self._lock:
            self._placing -= 1
            parked = self._parked.pop(order_id, [])
            self._orders[order_id] = {
                "order_id": order_id, "client_id": client_id,
                "symbol": symbol, "side": side, "qty": qty, "type": otype,
                # `price` is the order's LIVE level, whichever field carries
                # it: a bare STOP has only its trigger, and tracking that in
                # `price` is what lets one amend-detection path serve both
                # kinds (see _absorb_order).
                "price": body.get("stopPrice") if otype == "stop"
                         else body.get("limitPrice"),
                "stop_price": body.get("stopPrice"),
                # The type the order was PLACED as, kept separately because a
                # stop_limit becomes a limit when it triggers and the position
                # it opens still came from a stop_limit (SPEC: `from_order`).
                "placed_type": otype,
                "state": "new", "filled": 0.0,
                "expiry": expiry, "expires_at": body.get("expiresAt"),
                "deadline": time.time() + ORDER_TIMEOUT, "degraded": False,
            }
            self._by_client[client_id] = order_id
        self.emit("order.accepted", {"client_id": client_id,
                                     "order_id": order_id})
        # The POST answer goes through the SAME state machine the feed does.
        # A MARKET order comes back COMPLETE with its `fills` array, so the
        # platform has its fill before the WebSocket frame is written, and the
        # frame that follows finds the fillId already recorded and changes
        # nothing.
        self._absorb_order(order)
        self._replay_parked(parked)
        resting = str(order.get("state") or "").upper() in ("WORKING",
                                                            "RECEIVED")
        result = {"order_id": order_id,
                  "status": "resting" if resting else "accepted"}
        self._wake.set()
        return self.idempotent_store(client_id, result, sig)

    def _order_view(self, o: dict) -> dict:
        oid = str(o.get("orderId"))
        with self._lock:
            known = self._orders.get(oid) or {}
        state = ORDER_STATE.get(str(o.get("state") or "").upper(), "resting")
        otype = WIRE_TO_TYPE.get(str(o.get("type") or "").upper(), "market")
        # A STOP carries its trigger in limitPrice at rest and stopPrice at
        # placement; the venue serialises it consistently as stopPrice on the
        # way out, so `price` stays the level a limit would deal at and
        # `stop_price` stays the trigger.
        return {
            "order_id": oid,
            "client_id": known.get("client_id"),
            "symbol": o.get("symbol"),
            "type": otype,
            "side": "buy" if (o.get("side") or "").upper() == "BUY" else "sell",
            "qty": _num(o.get("units")),
            "price": o.get("limitPrice"),
            "stop_price": o.get("stopPrice"),
            # Protections attach to the POSITION at Atlas; a resting order
            # does not report the sl/tp it will carry, so claiming one here
            # would be inventing it.
            "sl": None, "tp": None,
            "expiry": "gtd" if (o.get("timeInForce") or "").upper() == "GTD"
                      else "gtc",
            "expires_at": _ms(o.get("expiresAt")) if o.get("expiresAt")
                          else None,
            "state": state,
            "placed_at": _ms(o.get("submittedAt")),
            "done_at": _ms(o.get("completedAt")) if o.get("completedAt")
                       else None,
            "fill_price": o.get("avgFillPrice") if state == "filled" else None,
            "reason": None,
        }

    def handle_orders_list(self, params):
        want = (params or {}).get("state")
        rows = self._call("GET", "/rest/orders").get("orders") or []
        out = [self._order_view(o) for o in rows]
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        order = self._call("DELETE", f"/rest/orders/{order_id}")
        self._absorb_order(order)
        return {}

    def handle_order_amend(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        current = self._call("GET", f"/rest/orders/{order_id}")
        otype = WIRE_TO_TYPE.get(str(current.get("type") or "").upper(),
                                 "market")
        if str(current.get("state") or "").upper() != "WORKING":
            raise AdapterError("not_resting",
                               f"order {order_id} is "
                               f"{current.get('state')}, not WORKING")
        for key, why in (("sl", "stop loss"), ("tp", "take profit")):
            if params.get(key) is not None:
                raise AdapterError(
                    "unsupported",
                    f"Atlas attaches {why} to the POSITION, not to a resting "
                    f"order; set it with order.modify once the order fills")
        body = {}
        digits = int(_num(
            self._instrument(current.get("symbol")).get("priceDecimals"), 5))
        price, stop = params.get("price"), params.get("stop_price")
        if otype == "stop":
            # THE quirk, documented by Atlas itself: a bare STOP's trigger
            # travels on `stopPrice` when you place it and on `limitPrice`
            # when you amend it. Both SPEC fields are accepted and land on the
            # one field the endpoint reads, so a platform never has to know.
            new = stop if stop is not None else price
            if new is not None:
                body["limitPrice"] = round(_num(new), digits)
        else:
            if stop is not None:
                raise AdapterError(
                    "unsupported",
                    "Atlas's amend endpoint changes an order's live price "
                    "only; a stop_limit trigger cannot be moved, cancel and "
                    "replace it")
            if price is not None:
                body["limitPrice"] = round(_num(price), digits)
        if params.get("qty") is not None:
            body["units"] = self._validate_qty(
                self._instrument(current.get("symbol")), params["qty"])
        expiry = (params.get("expiry") or "").lower()
        if params.get("expiry_at") is not None:
            body["expiresAt"] = int(_sec(params["expiry_at"])) + 1
        elif expiry == "gtd":
            raise AdapterError("invalid_expiry", "expiry 'gtd' needs expiry_at")
        if expiry == "gtc" and (current.get("timeInForce") or "").upper() == "GTD":
            raise AdapterError(
                "unsupported",
                "Atlas's amend endpoint cannot turn a GTD order back into a "
                "GTC one; cancel it and place a new order")
        if not body:
            raise AdapterError("invalid_qty",
                               "order.amend needs at least one of price, "
                               "stop_price, qty or expiry_at")
        self._absorb_order(self._call("PATCH", f"/rest/orders/{order_id}",
                                      body=body))
        return {}

    def handle_order_modify(self, params):
        """Protections live on the POSITION at Atlas, which is what this is."""
        pid = str(params.get("position_id") or "")
        if not pid:
            raise AdapterError("not_found", "position_id is required")
        if params.get("trail_points"):
            raise AdapterError("unsupported",
                               "Atlas cannot maintain a trailing stop")
        body = {}
        # A key ABSENT leaves the protection alone, a key present with null
        # CLEARS it, a key present with a number SETS it. That is Atlas's own
        # rule and the SPEC's, so the two line up with no translation beyond
        # the field names.
        if "sl" in params:
            body["stopLoss"] = params["sl"]
        if "tp" in params:
            body["takeProfit"] = params["tp"]
        if not body:
            raise AdapterError("invalid_stops",
                               "order.modify needs sl or tp")
        row = self._call("PATCH", f"/rest/positions/{pid}", body=body)
        with self._lock:
            rec = self._positions.setdefault(pid, {})
            rec["snapshot"] = row
        return {}

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported", "Atlas has no close-by endpoint")

    # == closing =======================================================
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
        self._close_position(pid, qty, reason="manual")
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, ("close", pid, qty))
        return result

    def _close_position(self, pid: str, qty, reason: str):
        with self._lock:
            rec = self._positions.get(pid) or {}
            snap = rec.get("snapshot") or {}
        if not snap:
            # The caller may be closing a position this process never listed
            # (adopted after a restart, or opened by another session).
            self._sync_positions()
            with self._lock:
                snap = (self._positions.get(pid) or {}).get("snapshot") or {}
        job = {"pid": pid, "reason": reason, "since": time.time(),
               "qty": _num(qty, _num(snap.get("units")))}
        # Filed BEFORE the POST goes out, not after it comes back: the venue's
        # own POSITION_CLOSED frame regularly reaches the reader thread before
        # the HTTP response reaches this one, and a job filed afterwards would
        # describe a close that is already finished. Whichever arrives first
        # takes the job; the loser finds nothing to do. It is also the only
        # place that knows whether this was a position.close or one leg of a
        # flatten_all, which Atlas calls "manual" either way.
        with self._lock:
            self._closing[pid] = job
        body = {} if qty is None else {"units": _num(qty)}
        try:
            resp = self._call("POST", f"/rest/positions/{pid}/close", body=body)
        except AdapterError:
            with self._lock:
                if self._closing.get(pid) is job:
                    self._closing.pop(pid, None)
            raise
        booked = _num(resp.get("realizedPnl"))
        if not resp.get("closed"):
            # A partial close produces no POSITION_CLOSED frame at Atlas, only
            # a POSITION_UPDATE with the smaller size, so this is the only
            # moment the closed quantity and its P/L are knowable. The frame
            # path is stood down for the same reason.
            with self._lock:
                if self._closing.get(pid) is job:
                    self._closing.pop(pid, None)
                rec = self._positions.setdefault(pid, {})
                # `realizedPnl` on this response is the position's CUMULATIVE
                # realised figure, not this call's, despite the documentation
                # calling it "the profit or loss this call just booked". They
                # agree on the first partial close and diverge on every one
                # after it, so the delta against what was already booked is
                # the number that is right in both readings.
                prior = _num(rec.get("realized_cum"))
                rec["realized_cum"] = booked
                delta = round(booked - prior, 2)
            self._finish_close(pid, qty=_num(resp.get("units")),
                               price=self._close_print(pid),
                               realized=delta, reason=reason, remaining=True)
        else:
            with self._lock:
                rec = self._positions.setdefault(pid, {})
                rec["realized_cum"] = booked
                job["realized"] = booked - _num(rec.get("realized_prior"))
        self._wake.set()

    def handle_flatten_all(self, params):
        """Close everything, and do not come back until the book is empty.

        SPEC 8.1 wants this to work while other calls are failing, and Atlas
        gives it the strongest possible form: ONE call that closes every
        position and cancels every working order server-side, so there is no
        per-position failure to survive in the first place. Each position is
        filed as a close in flight first, so its `position.closed` carries
        `reason: "flatten"` instead of the "manual" Atlas reports for every
        client close.
        """
        try:
            rows = self._positions_raw()
        except AdapterError as e:
            log(f"flatten: could not read positions first ({e}); "
                f"flattening anyway")
            rows = []
        now = time.time()
        with self._lock:
            for p in rows:
                pid = str(p.get("positionId"))
                self._closing.setdefault(pid, {
                    "pid": pid, "reason": "flatten", "since": now,
                    "qty": _num(p.get("units"))})
        resp = self._call("POST", "/rest/flatten")
        closed = int(_num(resp.get("closedPositions")))
        self._wake.set()
        # Atlas closes synchronously (the response is written after the book is
        # empty), so a caller reading positions.list next sees the truth. The
        # short drain is for the EVENTS: the confirmation frames are moments
        # behind, and returning before they land would make a platform's own
        # view flicker rather than settle.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with self._lock:
                pending = bool(self._closing)
            if not pending:
                break
            self._reap_closes()
            time.sleep(0.2)
        return {"closed": closed}

    def _finish_close(self, pid: str, qty=None, price=None, realized=None,
                      reason=None, time_ms=None, remaining=False,
                      broker_reason=None):
        """Report one position closing, exactly once."""
        with self._lock:
            job = self._closing.pop(pid, None)
            rec = self._positions.get(pid) or {}
            snap = rec.get("snapshot") or {}
            client_id = rec.get("client_id")
            if not remaining:
                if pid in self._final_closed:
                    return
                self._final_closed.add(pid)
        if qty in (None, 0):
            qty = _num((job or {}).get("qty"), _num(snap.get("units")))
        if price in (None, 0):
            # The close-side price of the last tick we hold, which is what a
            # close would have dealt at, before the open level, which is the
            # one price a close certainly did NOT happen at.
            last = self._last_quote.get(snap.get("symbol")) or {}
            side = (snap.get("side") or "").upper()
            price = _num(last.get("bid") if side == "BUY" else last.get("ask")) \
                or _num(snap.get("entryPrice"))
        if realized is None:
            # No frame reached us and no REST answer carried the figure: the
            # only thing left is arithmetic on the venue's own prices, which is
            # an APPROXIMATION and says so on stderr rather than passing for a
            # broker number.
            realized = self._estimate_realized(snap, price, qty)
            log(f"close {pid}: realised P/L approximated as {realized} "
                f"(no confirmation frame reached us)")
        # The adapter's own reason wins where this side knows it: only here
        # can a flatten leg be told from a plain close, and Atlas calls both
        # manual.
        if reason is None:
            reason = (job or {}).get("reason")
        if reason is None:
            key = str(broker_reason or "").lower()
            reason = CLOSE_REASON.get(key)
            if reason is None:
                if key:
                    log(f"close {pid}: unmapped Atlas reason "
                        f"{broker_reason!r}; reported as unknown")
                # SPEC 5: an adapter that cannot determine why a position
                # closed says `unknown` rather than picking a plausible value.
                reason = "unknown"
        data = {"position_id": pid, "qty": round(_num(qty), 8),
                "price": _num(price), "realized_pnl": round(_num(realized), 2),
                "swap": 0.0, "commission": -self._charge_share(pid, qty),
                "reason": reason,
                "time": time_ms or int(time.time() * 1000)}
        if broker_reason:
            data["reason_detail"] = str(broker_reason)
        if client_id:
            data["client_id"] = client_id
        self.emit("position.closed", data)
        if not remaining:
            with self._lock:
                self._positions.pop(pid, None)

    def _estimate_realized(self, snap: dict, price, qty) -> float:
        inst = self._instruments.get(snap.get("symbol")) or {}
        entry = _num(snap.get("entryPrice"))
        if not entry or not inst:
            return 0.0
        direction = 1.0 if (snap.get("side") or "").upper() == "BUY" else -1.0
        gross = ((_num(price) - entry) * direction * _num(qty)
                 * _num(inst.get("contractSize"), 1.0))
        return round(gross * self._usd_cross(inst.get("quoteCurrency")), 2)

    # == market data ===================================================
    def _next_sub_id(self) -> str:
        with self._lock:
            self._sub_seq += 1
            return f"sub-{self._sub_seq}"

    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._instrument(s)          # unknown_symbol before subscribing
        # The feed thread may still be opening the socket on the very first
        # call. Waiting briefly beats returning success on a subscription that
        # has not been sent, which a platform would read as a promise of ticks.
        self._connected.wait(5.0)
        fresh, cached = [], []
        with self._lock:
            for s in symbols:
                (cached if s in self._quote_subs else fresh).append(s)
        for s in fresh:
            sub = self._next_sub_id()
            with self._lock:
                self._quote_subs[s] = sub
            if not self._send_stomp("SUBSCRIBE", {
                    "id": sub, "destination": f"/topic/prices/{s}"}):
                self._emit_session("degraded",
                                   f"could not subscribe {s}: feed is down")
        # Atlas answers a SUBSCRIBE with an immediate snapshot, so a NEW symbol
        # needs nothing more. A repeat subscription gets no second snapshot, so
        # the last tick is republished: a caller that subscribes to price
        # something must not have to wait for the market to move, and on a shut
        # instrument it never would.
        for s in cached:
            with self._lock:
                last = self._last_quote.get(s)
            if last:
                self.emit("quote", dict(last))
        return {}

    def handle_quotes_unsubscribe(self, params):
        for s in params.get("symbols") or []:
            with self._lock:
                sub = self._quote_subs.pop(s, None)
            if sub:
                self._send_stomp("UNSUBSCRIBE", {"id": sub})
        return {}

    def _candles(self, symbol: str, tf: str, limit: int, before=None) -> dict:
        query = {"symbol": symbol, "granularity": TF_TO_GRANULARITY[tf],
                 "limit": max(1, min(int(limit), 500))}
        if before is not None:
            query["before"] = int(before)
        return self._call("GET", "/rest/candles?" + urllib.parse.urlencode(
            query, safe=":"))

    def handle_bars_history(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_GRANULARITY:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        want = max(1, int(_num(params.get("count"), 200)))
        bars, before = [], None
        # Atlas pages backwards: `before` is exclusive and `nextBefore` is the
        # oldest bar just returned, so the pages join with no gap and no
        # overlap. It stops at 90 days of depth by answering with an empty page
        # and a null nextBefore, which is the documented signal to stop rather
        # than a fault.
        while len(bars) < want:
            page = self._candles(symbol, tf, min(500, want - len(bars)), before)
            rows = page.get("candles") or []
            if not rows:
                break
            bars = [[_ms(c.get("time")), _num(c.get("o")), _num(c.get("h")),
                     _num(c.get("l")), _num(c.get("c")), _num(c.get("v"))]
                    for c in rows] + bars
            before = page.get("nextBefore")
            if before is None:
                break
        bars.sort(key=lambda b: b[0])          # oldest first (SPEC)
        return {"bars": bars[-want:]}

    def handle_bars_subscribe(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_GRANULARITY:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        with self._lock:
            # Seeded with the newest CLOSED bucket rather than 0, so a
            # subscriber is not handed the last 200 bars as if they had just
            # closed. History is what bars.history is for.
            self._bars[(symbol, tf)] = self._newest_bar_time(symbol, tf)
        self._wake.set()
        return {}

    def _newest_bar_time(self, symbol: str, tf: str) -> int:
        try:
            rows = self._candles(symbol, tf, 1).get("candles") or []
        except AdapterError:
            return 0
        return int(_num(rows[-1].get("time"))) if rows else 0

    def handle_bars_unsubscribe(self, params):
        with self._lock:
            self._bars.pop((params.get("symbol"),
                            params.get("timeframe") or "1m"), None)
        return {}

    # == history =======================================================
    def _fills_page(self, limit: int = 200, cursor=None) -> list:
        query = {"limit": max(1, min(int(limit), 200))}
        if cursor:
            query["cursor"] = cursor
        data = self._call("GET", "/rest/fills?"
                          + urllib.parse.urlencode(query))
        return data.get("fills") or []

    def handle_history_fills(self, params):
        frm = _num((params or {}).get("from"), 0)
        to = _num((params or {}).get("to"), 0)
        out, cursor, pages = [], None, 0
        while pages < 25:
            page = self._fills_page(200, cursor)
            if not page:
                break
            pages += 1
            out.extend(page)
            cursor = str(page[-1].get("fillId"))
            # Newest first, so once a page ends older than the window there is
            # nothing further back worth reading.
            if frm and _ms(page[-1].get("time")) < frm:
                break
            if len(page) < 200:
                break
        fills = []
        for f in out:
            when = _ms(f.get("time"))
            if frm and when < frm:
                continue
            if to and when > to:
                continue
            fills.append({
                "fill_id": str(f.get("fillId")),
                "order_id": str(f["orderId"]) if f.get("orderId") is not None
                            else None,
                "position_id": str(f.get("positionId")),
                "symbol": f.get("symbol"),
                "side": "buy" if (f.get("side") or "").upper() == "BUY"
                        else "sell",
                "qty": _num(f.get("units")),
                "price": _num(f.get("price")),
                # Negative because a commission costs the holder. Atlas
                # charges it on the opening print only, so a closing row
                # honestly reports 0 (and 0, not -0.0: a signed zero on the
                # wire reads as a charge too small to show).
                "fees": -abs(_num(f.get("commission"))) or 0.0,
                "time": when,
            })
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == the feed ======================================================
    def _ws_url(self) -> str:
        scheme = "wss" if BASE.startswith("https://") else "ws"
        host = BASE.split("://", 1)[-1]
        return f"{scheme}://{host}/stomp"

    def _send_stomp(self, command: str, headers: dict, body: str = "") -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            ws.send_text(stomp_frame(command, headers, body))
            return True
        except OSError as e:
            log(f"send {command} failed: {e}")
            return False

    def _stream_loop(self):
        """One WebSocket, re-established for the adapter's whole life.

        Atlas replays nothing on the execution queue ("going forward from the
        moment you subscribe, no backfill, no snapshot"), so every reconnect
        re-reads orders and positions from REST: that is the only way a fill
        or a close that happened during the gap can be found at all.
        """
        first = True
        while not self._stop.is_set():
            ws = None
            try:
                with self._lock:
                    self._ws_gen += 1
                    gen = self._ws_gen
                ws = WebSocket(self._ws_url())
                ws.connect()
                ws.send_text(stomp_frame("CONNECT", {
                    "accept-version": "1.2", "host": "atlas",
                    "login": self.api.api_key,
                    "heart-beat": f"{HEARTBEAT_MS},{HEARTBEAT_MS}"}))
                frame = parse_stomp(ws.recv_text())
                if not frame or frame[0] != "CONNECTED":
                    # The venue closes the socket after ERROR on a bad login,
                    # and it is the ONLY failure that does; anything else
                    # would have left the socket open.
                    raise OSError(f"CONNECT refused: {frame}")
                self._ws = ws
                self._connected.set()
                self._resubscribe()
                self._emit_session("connected", "price and execution feed open")
                if not first:
                    # Whatever moved while we were away is only visible here.
                    self._sync_orders()
                    self._sync_positions()
                    self._reconcile_positions()
                first = False
                threading.Thread(target=self._heartbeat_loop,
                                 args=(ws, gen), daemon=True).start()
                self._pump(ws, gen)
            except Exception as e:      # noqa: BLE001 - any failure reconnects
                if self._stop.is_set():
                    return
                self._emit_session("degraded",
                                   f"feed: {type(e).__name__}: {e}")
            finally:
                self._connected.clear()
                if ws is not None:
                    ws.close()
                if self._ws is ws:
                    self._ws = None
            if not self._stop.is_set():
                time.sleep(1.0)

    def _heartbeat_loop(self, ws, gen: int):
        """Honour the beat we negotiated.

        Atlas drops a socket after 20 s of silence from a client that
        negotiated heart-beating, so a client that asked for the contract has
        to keep it. Sent at half the interval so one late wakeup cannot cost
        the connection.
        """
        while not self._stop.is_set():
            time.sleep(HEARTBEAT_SEND)
            with self._lock:
                if gen != self._ws_gen:
                    return
            try:
                ws.send_text("\n")
            except OSError:
                return

    def _resubscribe(self):
        self._send_stomp("SUBSCRIBE", {"id": "sub-exec",
                                       "destination": "/user/queue/executions"})
        with self._lock:
            wanted = dict(self._quote_subs)
        for symbol, sub in wanted.items():
            self._send_stomp("SUBSCRIBE", {
                "id": sub, "destination": f"/topic/prices/{symbol}"})

    def _pump(self, ws, gen: int):
        while not self._stop.is_set():
            with self._lock:
                if gen != self._ws_gen:
                    return
            text = ws.recv_text()
            frame = parse_stomp(text)
            if frame is None:
                continue                        # a lone LF: the server's beat
            command, headers, body = frame
            if command == "MESSAGE":
                self._on_message(headers, body)
            elif command == "ERROR":
                # Everything except a bad login leaves the socket up, so this
                # is reported and the connection kept.
                log(f"venue ERROR frame: {headers.get('message')!r}")
                self._emit_session(
                    "degraded", f"venue said {headers.get('message')!r}")
            elif command == "RECEIPT":
                return                          # our DISCONNECT was accepted

    def _on_message(self, headers: dict, body: str):
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            log(f"unparseable MESSAGE body: {body[:120]}")
            return
        if not isinstance(payload, dict):
            return
        dest = headers.get("destination") or ""
        if dest.startswith("/topic/prices/"):
            self._on_quote(payload)
            return
        self._on_execution(payload)

    def _on_quote(self, row: dict):
        quote = {"symbol": row.get("symbol"), "bid": _num(row.get("bid")),
                 "ask": _num(row.get("ask")), "time": _ms(row.get("ts"))}
        with self._lock:
            self._last_quote[quote["symbol"]] = quote
        self.emit("quote", quote)

    # Documented discriminators on the execution queue. They are checked
    # FIRST, and structurally inferred only when the value is not one of
    # these, because of the quirk below.
    EXEC_TYPES = {"ORDER_UPDATE", "FILL", "POSITION_UPDATE", "POSITION_CLOSED"}

    def _on_execution(self, payload: dict):
        """One frame off /user/queue/executions, classified by SHAPE.

        Atlas documents a `"type"` field on every execution message, and on an
        ORDER_UPDATE that field is DESTROYED: the venue merges the order object
        over the envelope, and an order object carries its own `type` (MARKET,
        LIMIT, STOP, STOP_LIMIT), which wins. So `{"type": "MARKET", ...}` on
        this queue is not a message type at all, it is an order update whose
        discriminator was overwritten by the order's own kind. FILL,
        POSITION_UPDATE and POSITION_CLOSED payloads have no `type` key of
        their own and survive intact.

        Trusting the documented field alone would silently drop every order
        lifecycle event; keying on the fields each payload actually carries
        works under both the documented behaviour and the real one.
        """
        kind = payload.get("type")
        if kind not in self.EXEC_TYPES:
            if "closedUnits" in payload:
                kind = "POSITION_CLOSED"
            elif "fillId" in payload:
                kind = "FILL"
            elif "state" in payload and "orderId" in payload:
                kind = "ORDER_UPDATE"
            elif "entryPrice" in payload:
                kind = "POSITION_UPDATE"
            else:
                log(f"unclassifiable execution frame: {str(payload)[:160]}")
                return
        if kind == "ORDER_UPDATE":
            self._absorb_order(payload)
        elif kind == "FILL":
            self._absorb_fill(payload)
        elif kind == "POSITION_UPDATE":
            self._absorb_position(payload)
        elif kind == "POSITION_CLOSED":
            pid = str(payload.get("positionId"))
            self._finish_close(
                pid, qty=_num(payload.get("closedUnits")),
                price=_num(payload.get("closePrice")),
                realized=_num(payload.get("realizedPnl")),
                time_ms=_ms(payload.get("closedAt")),
                broker_reason=payload.get("reason"))

    # == state machines ================================================
    def _park(self, oid: str, kind: str, payload: dict) -> bool:
        """Hold a frame for an order this process has not learned the id of.

        True means "parked, do nothing more": a placement is in flight and
        this frame is probably about it. False means the order is either
        already known or genuinely not ours, and the caller carries on.
        """
        with self._lock:
            if oid in self._orders or self._placing <= 0:
                return False
            self._parked.setdefault(oid, []).append(
                (kind, payload, time.time()))
            return True

    def _replay_parked(self, parked: list):
        for kind, payload, _when in parked:
            if kind == "fill":
                self._absorb_fill(payload)
            else:
                self._absorb_order(payload)

    def _reap_parked(self):
        """Frames parked for an order that turned out not to be ours.

        A placement racing us is the normal case and is drained by the
        placement itself. What is left after the grace belongs to another
        session on the same account, and it is reported with no client_id
        rather than dropped: the platform must still see that the account
        traded (SPEC 7 adopts positions it does not recognise).
        """
        cutoff = time.time() - 5.0
        due = []
        with self._lock:
            if self._placing > 0:
                return
            for oid in list(self._parked):
                if self._parked[oid] and self._parked[oid][0][2] < cutoff:
                    due.extend(self._parked.pop(oid))
        self._replay_parked(due)

    def _absorb_order(self, o: dict):
        """Fold an order object into our view, emitting on TRANSITIONS ONLY.

        The same order arrives from the POST response, from the executions
        queue (at least once), and from the poller, so everything here is
        keyed on a state change and is safe to run any number of times.
        """
        oid = str(o.get("orderId"))
        if oid in ("None", ""):
            return
        if self._park(oid, "order", o):
            return
        state = ORDER_STATE.get(str(o.get("state") or "").upper())
        otype = WIRE_TO_TYPE.get(str(o.get("type") or "").upper())
        events = []
        with self._lock:
            rec = self._orders.get(oid)
            if rec is None:
                # An order this process did not place: adopted after a restart,
                # or placed by another session on the same account.
                rec = {"order_id": oid, "client_id": None,
                       "symbol": o.get("symbol"),
                       "side": "buy" if (o.get("side") or "").upper() == "BUY"
                               else "sell",
                       "qty": _num(o.get("units")), "type": otype or "limit",
                       "price": o.get("limitPrice"),
                       "stop_price": o.get("stopPrice"),
                       "state": "new", "filled": _num(o.get("filledUnits")),
                       "expiry": "gtd" if (o.get("timeInForce") or "").upper()
                                 == "GTD" else "gtc",
                       "expires_at": o.get("expiresAt"),
                       "deadline": time.time() + ORDER_TIMEOUT,
                       "degraded": False}
                self._orders[oid] = rec
            client_id = rec.get("client_id")
            if otype and otype != rec.get("type"):
                # A STOP_LIMIT that touched its trigger continues on the book
                # as a plain LIMIT under the same orderId; the type changing
                # under a WORKING order IS the promotion (SPEC 5).
                if rec.get("type") == "stop_limit" and otype == "limit":
                    events.append(("order.promoted", {
                        "order_id": oid, "from": "stop_limit", "to": "limit",
                        "price": o.get("limitPrice")}))
                rec["type"] = otype
            prev = rec["state"]
            if state and state != prev:
                rec["state"] = state
                if state == "resting" and rec["type"] != "market":
                    # RECEIVED and WORKING both map to `resting`, so this fires
                    # once, on the first of the two to arrive. `price` is the
                    # level the order rests AT, which for a bare STOP is its
                    # trigger and lives in stopPrice.
                    events.append(("order.placed", {
                        "client_id": client_id, "order_id": oid,
                        "type": rec["type"],
                        "price": o.get("stopPrice") if rec["type"] == "stop"
                                 else o.get("limitPrice"),
                        "expires_at": _ms(o.get("expiresAt"))
                                      if o.get("expiresAt") else None}))
                elif state == "cancelled":
                    # Atlas cancels an order only when a client asks it to
                    # (there is no broker-side cancel), and SPEC 5 says
                    # `code` is present only when the BROKER killed it.
                    events.append(("order.cancelled", {
                        "order_id": oid, "client_id": client_id}))
                elif state == "expired":
                    events.append(("order.expired", {
                        "order_id": oid, "client_id": client_id,
                        "expiry": rec.get("expiry") or "gtd"}))
                elif state == "rejected":
                    events.append(("order.rejected", {
                        "client_id": client_id, "code": "internal",
                        "message": "rejected by Atlas"}))
            # An amend is not a state change, so it is detected by the fields
            # an amend can move. Comparing against the local record keeps the
            # PATCH response and the frame that follows it to one event.
            if state == "resting":
                moved = {}
                # Which field carries this order's live level depends on its
                # kind: a bare STOP's is serialised as stopPrice (and amended
                # through limitPrice, the quirk handled in handle_order_amend),
                # everything else uses limitPrice. Comparing the wrong one
                # makes an amend invisible, which is how a moved stop can sit
                # in a platform's book at its old level.
                level = "stopPrice" if rec.get("type") == "stop" \
                    else "limitPrice"
                for field, key in ((level, "price"),
                                   ("units", "qty"),
                                   ("expiresAt", "expires_at")):
                    if field not in o:
                        continue
                    new = o.get(field)
                    old = rec.get(key)
                    if new is not None and _num(new) != _num(old):
                        rec[key] = new
                        moved[key] = new
                if moved:
                    data = {"order_id": oid}
                    if "price" in moved:
                        data["price"] = _num(moved["price"])
                    if "qty" in moved:
                        data["qty"] = _num(moved["qty"])
                    if "expires_at" in moved:
                        data["expires_at"] = _ms(moved["expires_at"])
                    events.append(("order.amended", data))
        for name, data in events:
            self.emit(name, data)
        # A COMPLETE order answer carries its own prints; feeding them through
        # the fill machine is what makes a market order's `order.filled` land
        # with the POST response instead of a WebSocket round trip later.
        for fill in o.get("fills") or []:
            self._absorb_fill(fill)

    def _absorb_fill(self, f: dict):
        """One print, reported exactly once, whichever door it came in by."""
        fill_id = str(f.get("fillId"))
        if f.get("orderId") is None:
            # A print with no order behind it is a CLOSE (take-profit,
            # stop-loss, manual close or stop-out). It is not an order fill and
            # must never be announced as one; the position close reports it.
            return
        oid = str(f.get("orderId"))
        if self._park(oid, "fill", f):
            return
        qty = _num(f.get("units"))
        pid = str(f.get("positionId"))
        with self._lock:
            if fill_id in self._fills_seen:
                return
            self._fills_seen.add(fill_id)
            rec = self._orders.get(oid) or {}
            client_id = rec.get("client_id")
            rec["filled"] = _num(rec.get("filled")) + qty
            remaining = round(_num(rec.get("qty")) - rec["filled"], 8)
            # Commission is charged on the trade that OPENS a position, so the
            # print that opened it is where the position's fee comes from.
            pos = self._positions.setdefault(pid, {})
            fee = _num(f.get("commission"))
            pos["fee_total"] = _num(pos.get("fee_total")) + fee
            pos["fee_left"] = _num(pos.get("fee_left")) + fee
            pos.setdefault("qty0", qty)
            if client_id and not pos.get("client_id"):
                # SPEC 4: a position's client_id is the id of the order that
                # OPENED it, so the first writer wins.
                pos["client_id"] = client_id
            placed_as = rec.get("placed_type") or rec.get("type")
            if placed_as and not pos.get("from_order"):
                # The type the order was PLACED as: a stop_limit that filled
                # did so as a limit, but the position came from a stop_limit
                # and that is what a platform is asking about. `null` for a
                # plain market fill (SPEC).
                pos["from_order"] = None if placed_as == "market" else placed_as
        data = {"client_id": client_id, "order_id": oid, "position_id": pid,
                "symbol": f.get("symbol"),
                "side": "buy" if (f.get("side") or "").upper() == "BUY"
                        else "sell",
                "qty": qty, "price": _num(f.get("price")),
                "fees": -abs(_num(f.get("commission"))) or 0.0,
                "time": _ms(f.get("time"))}
        if remaining > 1e-9:
            data["remaining"] = remaining
            self.emit("order.partial", data)
        else:
            self.emit("order.filled", data)

    def _absorb_position(self, p: dict):
        pid = str(p.get("positionId"))
        with self._lock:
            rec = self._positions.setdefault(pid, {})
            rec["snapshot"] = p
            rec.setdefault("qty0", _num(p.get("units")))
            rec.setdefault("fee_total", 0.0)
            rec.setdefault("fee_left", 0.0)

    def _sync_orders(self):
        try:
            for o in self._call("GET", "/rest/orders").get("orders") or []:
                self._absorb_order(o)
        except AdapterError as e:
            log(f"order sync failed: {e}")

    def _reconcile_positions(self):
        """Positions that went while nobody was listening.

        The execution queue does not replay, so a stop-loss that fired during
        a reconnect is invisible on it forever. REST is the only witness, and
        it can say only that the position is gone: no price, no P/L, no
        reason. SPEC 5 is explicit that `unknown` is the honest answer there,
        and that guessing from the exit price is the same guess one layer up.
        """
        try:
            live = {str(p.get("positionId"))
                    for p in self._call("GET", "/rest/positions").get(
                        "positions") or []}
        except AdapterError as e:
            log(f"position reconcile failed: {e}")
            return
        with self._lock:
            gone = [pid for pid, rec in self._positions.items()
                    if pid not in live and rec.get("snapshot")
                    and pid not in self._final_closed]
        for pid in gone:
            price = self._close_print(pid)
            self._finish_close(pid, price=price)

    def _close_print(self, pid: str):
        """The venue's own price for a close we never saw confirmed."""
        try:
            for f in self._fills_page(limit=50):
                if str(f.get("positionId")) == pid and f.get("orderId") is None:
                    return _num(f.get("price"))
        except AdapterError:
            pass
        return None

    # == the poller ====================================================
    def _poll_loop(self):
        """The backstop for everything the feed is supposed to say.

        It sleeps unless something is in flight, so an idle adapter costs the
        venue almost nothing; what it exists for is the frame that never
        arrives, which must cost latency and never correctness.
        """
        while not self._stop.is_set():
            self._wake.wait(2.0)
            self._wake.clear()
            try:
                self._reap_parked()
                self._reap_orders()
                self._reap_closes()
                self._reap_positions()
                self._reap_bars()
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log(f"poll: {type(e).__name__}: {e}")

    def _reap_orders(self):
        with self._lock:
            inflight = [dict(r) for r in self._orders.values()
                        if r["state"] in ("new", "resting")
                        and r["type"] != "market"]
            stuck = [dict(r) for r in self._orders.values()
                     if r["state"] == "new" and r["type"] == "market"]
        for rec in inflight + stuck:
            oid = rec["order_id"]
            try:
                self._absorb_order(self._call("GET", f"/rest/orders/{oid}"))
            except AdapterError as e:
                log(f"poll order {oid}: {e}")
                continue
            with self._lock:
                cur = self._orders.get(oid) or {}
                overdue = (cur.get("state") in ("new",)
                           and time.time() > cur.get("deadline", 0)
                           and not cur.get("degraded"))
                if overdue:
                    cur["degraded"] = True
            if overdue:
                # SPEC 8.4: an order that has not reached a terminal state
                # inside the declared timeout is reported, not waited on.
                self._emit_session(
                    "degraded",
                    f"order {oid} has not reached a terminal state in "
                    f"{int(ORDER_TIMEOUT)}s")

    def _reap_closes(self):
        with self._lock:
            jobs = [dict(j) for j in self._closing.values()]
        if not jobs:
            return
        now = time.time()
        # Atlas's own POSITION_CLOSED frame carries the exact close price, the
        # exact realised P/L and the reason, and lands within milliseconds of
        # the REST answer. Stepping in before the grace has passed would swap
        # broker numbers for local arithmetic on every single close, which is
        # the opposite of what a backstop is for.
        if all(now - j["since"] < CLOSE_CONFIRM_GRACE for j in jobs):
            return
        try:
            live = {str(p.get("positionId"))
                    for p in self._call("GET", "/rest/positions").get(
                        "positions") or []}
        except AdapterError as e:
            log(f"poll positions: {e}")
            return
        for job in jobs:
            pid = job["pid"]
            if now - job["since"] < CLOSE_CONFIRM_GRACE:
                continue
            if pid not in live:
                self._finish_close(pid, qty=job.get("qty"),
                                   price=self._close_print(pid),
                                   realized=job.get("realized"),
                                   reason=job.get("reason"))
            elif now - job["since"] > ORDER_TIMEOUT:
                with self._lock:
                    self._closing.pop(pid, None)
                self._emit_session(
                    "degraded",
                    f"close of position {pid} is unconfirmed after "
                    f"{int(ORDER_TIMEOUT)}s")

    def _reap_positions(self):
        """Catch a stop-loss, take-profit or stop-out whose frame was lost."""
        with self._lock:
            holding = any(rec.get("snapshot")
                          and pid not in self._final_closed
                          for pid, rec in self._positions.items())
            due = time.time() - self._last_positions_poll > 5.0
        if holding and due:
            self._reconcile_positions()
            with self._lock:
                self._last_positions_poll = time.time()

    def _reap_bars(self):
        with self._lock:
            wants = dict(self._bars)
        for (symbol, tf), last in wants.items():
            try:
                rows = self._candles(symbol, tf, 2).get("candles") or []
            except AdapterError as e:
                log(f"bar poll {symbol} {tf}: {e}")
                continue
            for c in rows:
                t = int(_num(c.get("time")))
                # Atlas returns only fully closed buckets, so everything here
                # is emittable; the guard is against a venue that one day
                # decides to include the forming one (SPEC: CLOSED bars only).
                if t <= last or t + TF_SECONDS[tf] > time.time():
                    continue
                self.emit("bar", {"symbol": symbol, "timeframe": tf,
                                  "bar": [_ms(t), _num(c.get("o")),
                                          _num(c.get("h")), _num(c.get("l")),
                                          _num(c.get("c")), _num(c.get("v"))]})
                with self._lock:
                    if (symbol, tf) in self._bars:
                        self._bars[(symbol, tf)] = t


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a
    # pipe buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    AtlasAdapter().run()


if __name__ == "__main__":
    main()
