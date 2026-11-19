#!/usr/bin/env python3
"""Brue Connect adapter for Vertex Derivatives (USDC-margined crypto futures).

Vertex is ONE WebSocket and nothing else. Authentication, the instrument
catalog, candles, live ticks, order entry, positions and the account summary
all ride the same socket as JSON-RPC 2.0, multiplexed by request id, with
server-pushed `subscription` and `heartbeat` notifications interleaved between
the replies. There is no REST surface to fall back on, so this file implements
RFC 6455 by hand (handshake, masked client frames, the 7/16/64-bit length
forms, ping/pong, close, continuation) on a raw socket, because the adapter is
spawned by whatever python the user's machine has and a pip dependency would
make the broker permanently unconnectable.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no socket, no registration, no auth, no
    catalog. All of it happens in `on_first_call`, so a platform's connection
    picker can learn what this broker calls itself without opening a session
    on it.
  * three threads. The READER owns the socket and does nothing that can
    block on a reply (it answers the heartbeat fire-and-forget and queues
    everything else), because it is the only thread that can deliver a
    reply and a reply-waiting reader is a deadlock. The DISPATCHER drains
    that queue and is allowed to call the venue, so a fill for an order this
    process has never heard of can be resolved with a lookup. The POLLER is
    the backstop for everything a notification may drop.
  * the venue's signature quirk is the heartbeat: a `test_request`
    notification every 15 s, answered with `public/test` inside 10 s or the
    socket dies 4008. It is answered on the reader thread and it BYPASSES
    this adapter's own request budget, because the venue counts a rate-limited
    `public/test` as a missed heartbeat and closing the connection to save a
    token is a poor trade.
  * every venue timestamp is MICROseconds and every Brue timestamp is
    milliseconds, so the conversion happens once, at this boundary, in `_ms()`
    and nowhere else. Every venue amount is a COUNT OF CONTRACTS, never a coin
    quantity; the coin size lives in the InstrumentSpec's `contract_size`,
    which is exactly what that field is for.
  * notifications are treated as AT LEAST ONCE and as possibly-lost in the
    same breath. A fill is announced once per `trade_id`, whether it arrived
    inline in the placing call's own response or over `user.trades` (the venue
    sends BOTH for the same fill and says so), and the poller re-reads order
    state and the position book so a dropped frame costs latency, never
    correctness.
  * this venue publishes no realised P/L and has no close-position event: a
    position simply reads smaller, or vanishes. `position.closed` is therefore
    reconstructed from the position book's own numbers (the size that went and
    the venue's own average price, against the price of the fill that did it),
    and the reason is recorded on the way IN by whoever asked for the close.
    Where nothing asked for it (a liquidation, an expiry closeout) the reason
    is `unknown`, which SPEC 5 prefers to a plausible guess.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import queue
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

# The SDK, found from this file rather than from the caller's cwd: the adapter
# is spawned by a platform that has no reason to know where the package lives.
_SDK = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

# The venue binds one port and serves the JSON-RPC socket at a fixed path. A
# configured URL that names no path gets it appended rather than rejected: a
# broker directory entry stores host:port, and "ws://host:7641" is the shape
# a human writes down.
WS_URL = os.environ.get("VERTEX_WS_URL", "ws://127.0.0.1:7641")
WS_PATH = "/ws/api/v2"

# Vertex: 20 requests/second sustained per CONNECTION, bursting to 40. Sit
# deliberately under it instead of discovering it with a 10028: a rate-limited
# `public/test` still counts as a missed heartbeat, so the budget that matters
# is the one that always leaves room for it.
RATE_CAPACITY = 20
RATE_PER_SEC = 12.0

# The venue sends a heartbeat every 15 s and closes 4008 if no `public/test`
# answers it within 10 s. Nothing else on the socket substitutes for it, so a
# read that goes quiet for longer than two heartbeat cycles is a dead socket,
# not a slow one.
SOCK_READ_TIMEOUT = 45.0

# `public/auth` is good for 900 s ON THIS SOCKET. Re-authenticate early rather
# than discover the expiry as a 13009 in the middle of an order.
AUTH_TTL = 900.0
AUTH_REFRESH_AT = 780.0

POLL_INTERVAL = 2.0
BAR_POLL_INTERVAL = 5.0
CATALOG_POLL_INTERVAL = 60.0
# How long a recorded close intent stays authoritative. Long enough that a
# slow venue still gets the right `reason`, short enough that an intent whose
# close never happened cannot mislabel an unrelated close minutes later.
INTENT_TTL = 60.0

# SPEC timeframe -> (venue resolution string, bucket seconds). The venue
# accepts these seven and nothing else.
TF = {"1m": ("1", 60), "5m": ("5", 300), "15m": ("15", 900),
      "30m": ("30", 1800), "1h": ("60", 3600), "4h": ("240", 14400),
      "1d": ("1D", 86400)}

PRIVATE_CHANNELS = ["user.orders", "user.trades", "user.portfolio"]

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[vertex] {msg}", file=sys.stderr, flush=True)


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _ms(us) -> int:
    """Vertex microseconds -> epoch milliseconds (SPEC 2).

    The venue is emphatic that every timestamp in every message is
    microseconds. Doing this anywhere but here is how a platform ends up
    showing a fill in the year 57000.
    """
    return int(round(_num(us) / 1000.0))


def _us(ms) -> int:
    return int(round(_num(ms) * 1000.0))


def _digits_for(tick: float) -> int:
    """Decimal places implied by a tick size (0.001 -> 3)."""
    s = format(float(tick), ".10f").rstrip("0")
    return len(s.split(".")[1]) if "." in s and s.split(".")[1] else 0


# ======================================================================
# RFC 6455 client. socket + struct + hashlib + base64, no dependency.
# ======================================================================

class WSClosed(Exception):
    """The socket went away: peer close frame, EOF, or a broken read."""

    def __init__(self, code=None, reason=""):
        super().__init__(f"websocket closed ({code}): {reason}")
        self.code = code
        self.reason = reason


class WebSocket:
    """One client connection, framing included.

    Deliberately minimal: this venue speaks text frames and answers pings, so
    what is implemented is what is on the wire plus the parts of the RFC a
    server may use without warning (16/64-bit lengths, fragmentation, ping
    payload echo). Client frames are always masked, which is not optional:
    an unmasked client frame is a protocol error and a conformant server
    closes on it.
    """

    def __init__(self, url: str, connect_timeout: float = 15.0):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise WSClosed(None, f"unsupported scheme {u.scheme!r} in {url!r}")
        secure = u.scheme == "wss"
        host = u.hostname
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        sock = socket.create_connection((host, port), timeout=connect_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if secure:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)
        self._sock = sock
        self._buf = b""
        self._send_lock = threading.Lock()
        self.closed = False
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        hostheader = host if port in (80, 443) else f"{host}:{port}"
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostheader}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n").encode("ascii")
        sock.sendall(handshake)
        head = self._read_until(b"\r\n\r\n").decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if "101" not in status:
            raise WSClosed(None, f"upgrade refused: {status.strip()}")
        want = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        got = ""
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip()
        if got != want:
            # A wrong accept key means whatever answered is not a WebSocket
            # server; talking frames at it would hang rather than fail.
            raise WSClosed(None, "bad Sec-WebSocket-Accept")
        sock.settimeout(SOCK_READ_TIMEOUT)

    # -- reading ------------------------------------------------------
    def _recv_into_buf(self):
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout as e:
            raise WSClosed(None, "read timed out (no heartbeat)") from e
        except OSError as e:
            raise WSClosed(None, f"{type(e).__name__}: {e}") from e
        if not chunk:
            raise WSClosed(None, "peer closed the connection")
        self._buf += chunk

    def _read_until(self, sep: bytes) -> bytes:
        while sep not in self._buf:
            self._recv_into_buf()
        head, self._buf = self._buf.split(sep, 1)
        return head + sep

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._recv_into_buf()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(n) if n else b""
        if masked and data:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return fin, opcode, data

    def recv_message(self) -> str:
        """The next application message, control frames handled internally."""
        frags = []
        kind = None
        while True:
            fin, opcode, data = self._read_frame()
            if opcode == 0x8:                       # close
                code, reason = None, ""
                if len(data) >= 2:
                    code = struct.unpack("!H", data[:2])[0]
                    reason = data[2:].decode("utf-8", "replace")
                self._send_frame(0x8, data[:125])   # echo, then we are done
                self.closed = True
                raise WSClosed(code, reason)
            if opcode == 0x9:                       # ping: pong the payload
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode in (0x1, 0x2):
                frags = [data]
                kind = opcode
            elif opcode == 0x0:
                frags.append(data)
            else:
                raise WSClosed(None, f"unknown opcode {opcode}")
            if fin:
                payload = b"".join(frags)
                if kind == 0x2:
                    # This venue is text-only; a binary frame means we are
                    # talking to something else entirely.
                    raise WSClosed(None, "unexpected binary frame")
                return payload.decode("utf-8", "replace")

    # -- writing ------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        body = bytearray(payload)
        for i in range(len(body)):
            body[i] ^= mask[i % 4]
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        with self._send_lock:
            if self.closed:
                raise WSClosed(None, "send on a closed socket")
            try:
                self._sock.sendall(header + mask + bytes(body))
            except OSError as e:
                self.closed = True
                raise WSClosed(None, f"send failed: {e}") from e

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self):
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass


# ======================================================================
# JSON-RPC 2.0 over that socket, with auth, budget and reconnection.
# ======================================================================

class VertexError(Exception):
    """A JSON-RPC error object from the venue, or a transport failure."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


# Venue error codes -> the SPEC section 6 closed set. 11044 is deliberately
# one code for several conditions on this venue ("not a resting order of
# yours" AND "you hold no position here"), which is why the callers that can
# tell those apart re-map it themselves rather than trusting this table.
ERROR_MAP = {
    -32700: ("internal", True),
    -32601: ("unsupported", False),
    # Every shape this venue calls invalid is validated here first, so a
    # -32602 that still gets through is a disagreement between this adapter
    # and the venue rather than a user error. Kept as an extension rather than
    # flattened to `internal`: the platform loses nothing and can see it.
    -32602: ("x_vertex_invalid_params", False),
    13004: ("unauthorized", False),
    13009: ("unauthorized", True),
    10004: ("unknown_symbol", False),
    11029: ("invalid_qty", False),
    10009: ("insufficient_margin", False),
    11044: ("not_found", False),
    12003: ("market_closed", False),
    10028: ("rate_limited", True),
    13666: ("internal", True),
}


def to_adapter_error(e: VertexError) -> AdapterError:
    hit = ERROR_MAP.get(e.code)
    if hit is None:
        code, retry = ("x_vertex_%s" % str(e.code).replace("-", "neg"), False)
    else:
        code, retry = hit
    return AdapterError(code, f"{e.code}: {e.detail}", retry)


class VertexClient:
    """The socket, the request map, the token, and the request budget.

    One place owns each of them so no caller has to think about any of them.
    Requests are matched by JSON-RPC id through a pending map, which is what
    makes it safe for the poller, the dispatcher and the platform's own
    handler thread to have calls in flight at the same moment: without it the
    first reply to arrive would be handed to whoever asked last.
    """

    def __init__(self, url: str, state_dir: str, on_notification,
                 on_session, on_ready):
        self.url = url
        self.state_dir = state_dir
        self._on_notification = on_notification
        self._on_session = on_session
        self._on_ready = on_ready
        self._ws: WebSocket | None = None
        self._lock = threading.RLock()
        self._pending: dict[int, list] = {}   # id -> [Event, message]
        self._id = 0
        self._creds = None
        self._authed_at = 0.0
        self._auth_lock = threading.RLock()
        self._tokens = float(RATE_CAPACITY)
        self._token_at = time.monotonic()
        self._stop = threading.Event()
        self._down = threading.Event()
        self._gen = 0

    # -- credentials ---------------------------------------------------
    def _cred_path(self) -> str:
        return os.path.join(self.state_dir, "credentials.json")

    def credentials(self) -> dict:
        """Register once, then reuse forever.

        `public/register` opens a NEW demo account funded with 50000 USDC and
        the secret is returned exactly once, with no recovery call. Registering
        on every start would silently orphan yesterday's positions and hand the
        platform a different account under the same profile name, so the pair
        lives in the profile's own state directory (0600, atomic replace) and
        never on the protocol wire (SPEC 8.5).
        """
        if self._creds:
            return self._creds
        path = self._cred_path()
        try:
            with open(path) as fh:
                c = json.load(fh)
            if c.get("client_id") and c.get("client_secret"):
                self._creds = c
                return c
        except (OSError, json.JSONDecodeError):
            pass
        email = "brue-connect-%s@example.test" % secrets.token_hex(4)
        c = self.call("public/register", {"email": email}, auth=False)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(c, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            log(f"could not persist credentials to {path}: {e}")
        log(f"registered {c.get('client_id')} -> account {c.get('account')}")
        self._creds = c
        return c

    # -- lifecycle -----------------------------------------------------
    def start(self):
        self._open()
        self.authenticate()
        threading.Thread(target=self._supervise, daemon=True).start()

    def _open(self):
        url = self.url
        parts = urllib.parse.urlsplit(url)
        if parts.path in ("", "/"):
            url = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, WS_PATH, parts.query, ""))
        ws = WebSocket(url)
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._ws = ws
            self._authed_at = 0.0
        threading.Thread(target=self._reader, args=(ws, gen),
                         daemon=True).start()

    def authenticate(self, force: bool = False):
        """`public/auth` on THIS socket.

        The venue's own words: the token authorizes the CONNECTION, not the
        identity, and no method anywhere accepts a token as a parameter. So
        there is nothing to cache across sockets and a reconnect always
        re-authenticates.
        """
        with self._auth_lock:
            if not force and time.time() - self._authed_at < AUTH_REFRESH_AT:
                return
            c = self.credentials()
            self.call("public/auth", {"grant_type": "client_credentials",
                                      "client_id": c["client_id"],
                                      "client_secret": c["client_secret"]},
                      auth=False)
            self._authed_at = time.time()

    def close(self):
        self._stop.set()
        self._down.set()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()

    def _supervise(self):
        """Re-open the socket for the life of the adapter.

        A dropped socket is normal operation here, not an error path: the
        venue closes 4008 on a missed heartbeat and the auth state does not
        survive a reconnect, so reconnecting means socket, then auth, then
        every subscription again, then a re-read of the book.
        """
        backoff = 0.5
        while not self._stop.is_set():
            self._down.wait(1.0)
            if self._stop.is_set():
                return
            with self._lock:
                live = self._ws is not None and not self._ws.closed
            if live:
                self._down.clear()
                continue
            self._down.clear()
            try:
                self._open()
                self.authenticate(force=True)
            except Exception as e:      # noqa: BLE001 - any failure retries
                self._on_session("disconnected", f"reconnect failed: {e}")
                time.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2, 10.0)
                self._down.set()
                continue
            backoff = 0.5
            try:
                self._on_ready(True)
            except Exception as e:      # noqa: BLE001
                log(f"post-reconnect resync failed: {type(e).__name__}: {e}")

    # -- the reader ----------------------------------------------------
    def _reader(self, ws: WebSocket, gen: int):
        try:
            while not self._stop.is_set():
                raw = ws.recv_message()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log(f"unparseable frame: {raw[:120]}")
                    continue
                if not isinstance(msg, dict):
                    continue
                self._route(msg)
        except WSClosed as e:
            if not self._stop.is_set():
                log(f"socket down: {e}")
        except Exception as e:          # noqa: BLE001 - reader must not die silently
            if not self._stop.is_set():
                log(f"reader stopped: {type(e).__name__}: {e}")
        finally:
            ws.close()
            with self._lock:
                if self._gen == gen:
                    self._ws = None
                # Nothing can answer these now; a caller blocked on a reply
                # that will never come is worse than a typed failure.
                for slot in self._pending.values():
                    slot[1] = {"error": {"code": 0,
                                         "message": "connection lost"}}
                    slot[0].set()
                self._pending.clear()
            if not self._stop.is_set():
                self._on_session("degraded", "venue socket dropped, "
                                             "reconnecting")
                self._down.set()

    def _route(self, msg: dict):
        rid = msg.get("id")
        if rid is not None:
            with self._lock:
                slot = self._pending.pop(rid, None)
            if slot is not None:
                slot[1] = msg
                slot[0].set()
            # Otherwise it is the reply to a heartbeat answer, which is sent
            # without a waiter on purpose (below). Nothing to do with it.
            return
        method = msg.get("method")
        if method == "heartbeat":
            # THE quirk. Answered here, on the reader thread, without waiting
            # for the reply and WITHOUT spending the request budget: the venue
            # counts a rate-limited `public/test` as a missed heartbeat and
            # closes the socket 4008 ten seconds later.
            self._send_no_wait("public/test", {})
        elif method == "subscription":
            params = msg.get("params") or {}
            self._on_notification(params.get("channel"), params.get("data"))

    # -- sending -------------------------------------------------------
    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _budget(self):
        """Block until this call fits inside the connection's budget."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(RATE_CAPACITY,
                                   self._tokens + (now - self._token_at)
                                   * RATE_PER_SEC)
                self._token_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / RATE_PER_SEC + 0.005
            time.sleep(min(wait, 1.0))

    def _socket(self) -> WebSocket:
        with self._lock:
            ws = self._ws
        if ws is None or ws.closed:
            raise VertexError(0, "not connected to the venue")
        return ws

    def _send_no_wait(self, method: str, params: dict):
        try:
            self._socket().send_text(json.dumps(
                {"jsonrpc": "2.0", "id": self._next_id(),
                 "method": method, "params": params}))
        except (VertexError, WSClosed) as e:
            log(f"could not send {method}: {e}")

    def call(self, method: str, params: dict | None = None,
             timeout: float = 12.0, auth: bool = True, tries: int = 3):
        """One JSON-RPC call, with the retryable venue answers handled here."""
        last = None
        for attempt in range(tries):
            if auth:
                self.authenticate()
            self._budget()
            rid = self._next_id()
            slot = [threading.Event(), None]
            with self._lock:
                self._pending[rid] = slot
            try:
                self._socket().send_text(json.dumps(
                    {"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params or {}}))
            except (VertexError, WSClosed) as e:
                with self._lock:
                    self._pending.pop(rid, None)
                last = VertexError(0, str(e))
                self._down.set()
                time.sleep(0.3)
                continue
            if not slot[0].wait(timeout):
                with self._lock:
                    self._pending.pop(rid, None)
                raise VertexError(0, f"{method} timed out after {timeout}s")
            msg = slot[1] or {}
            if "error" in msg:
                err = msg["error"] or {}
                code = err.get("code")
                e = VertexError(code, err.get("message", ""))
                if code == 10028 and attempt < tries - 1:
                    # The budget refills continuously, so the very next
                    # request a moment later can succeed.
                    time.sleep(0.25)
                    last = e
                    continue
                if code == 13009 and attempt < tries - 1:
                    # Auth is per-connection and expires; re-auth in place
                    # rather than surfacing an unauthorized the caller can do
                    # nothing about.
                    self.authenticate(force=True)
                    last = e
                    continue
                if code == 0 and attempt < tries - 1:
                    last = e
                    time.sleep(0.3)
                    continue
                raise e
            return msg.get("result")
        raise last or VertexError(0, f"{method} failed")


# ======================================================================
# The adapter.
# ======================================================================

class VertexAdapter(Adapter):
    name = "vertex"
    version = "1.0"
    broker = "Vertex Derivatives"
    # Truthful (SPEC 8.2): the venue's own documentation opens with "Vertex
    # Derivatives is a simulated venue for integration testing. All prices are
    # synthetic. No real money is involved."
    mode = "paper"
    # "Every demo account starts with a balance of 50000 USDC" and every
    # instrument is quoted in USDC; the account summary's `currency` is
    # re-checked on first call and complained about on stderr if it disagrees.
    account_currency = "USDC"
    # "Accounts on this venue net a single position per instrument: an
    # opposite-side order reduces or flips whatever is already held rather
    # than opening a second, separately-tracked position."
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        state = os.environ.get("BRUE_BROKER_STATE") or os.path.expanduser(
            "~/.brue-connect/state/vertex")
        self.client = VertexClient(WS_URL, state, self._on_notification,
                                   self._set_session, self._on_ready)
        self._state_lock = threading.RLock()
        self._pos_fetch_lock = threading.RLock()
        self._instruments: dict[str, dict] = {}
        self._orders: dict[str, dict] = {}      # venue order_id -> our record
        self._by_label: dict[str, dict] = {}    # label (client_id) -> record
        self._seen_trades: set = set()
        self._trade_ring: collections.deque = collections.deque(maxlen=8000)
        self._pos: dict[str, dict] = {}         # symbol -> our position view
        self._fees: dict[str, float] = {}       # symbol -> fees since flat
        self._closing: dict[str, dict] = {}     # symbol -> recorded intent
        self._opened_hint: dict[str, int] = {}  # symbol -> ms we opened it
        self._quotes: list[str] = []
        self._bars: dict[tuple, int] = {}       # (symbol, tf) -> last bar ms
        self._notify_q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._session_state = None
        self._margin_warned = False
        self._last_bar_poll = 0.0
        self._last_catalog_poll = 0.0

    # == handshake =====================================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # No stop loss / take profit attachment exists anywhere in
                # this API, so there is nothing to bracket, modify or trail.
                # Declared false so the platform refuses those loudly instead
                # of degrading silently (SPEC 3).
                "brackets": False,
                "modify": False,
                "trailing": False,
                # The book NETS, so an opposite-side order for part of the
                # size is how a position is partly closed on this venue; that
                # is the venue's own mechanic, not an emulation layered on top
                # of one it lacks. `position.close` takes `qty` and uses it.
                "partial_close": True,
                "pending": True,
                # `private/buy`/`private/sell` accept type market and limit.
                # There is no stop or stop_limit order on this venue at all.
                "pending_types": ["limit"],
                # "time_in_force accepts only good_til_cancelled ... there is
                # no IOC, FOK, post-only or day-order variant on this venue."
                "expiry": ["gtc"],
                # Margin is computed from OPEN POSITIONS only: a resting limit
                # order holds nothing, so an order accepted comfortably can
                # still die at the moment it should have filled. That is
                # exactly the consequence this flag exists to warn about.
                "reserves_margin": False,
                "oco": False,
                "close_by": False,
                # "There is no bulk 'list all my orders' call." This adapter
                # answers `orders.list` from what it placed and remembered,
                # which is strictly better than nothing but is NOT an
                # enumeration: an order placed by another session on the same
                # account is invisible to it forever, and a platform must mark
                # its order view PARTIAL rather than read an empty list as
                # "no resting orders" (SPEC 3).
                "can_list": False,
            },
            "data": {
                "quotes": True,
                "bars": True,
                "history": True,
                # No deposit, withdrawal, funding or dividend mechanic exists
                # here: "balance only moves through trading (realized pnl and
                # fees)". A balance move with no fill behind it therefore has
                # no explanation this venue can give, and saying so beats
                # inventing one.
                "operations": False,
                # The venue states no cap on subscribed channels and the whole
                # universe is five instruments, so no limit is declared
                # (absent means "the broker states no limit", SPEC 3).
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # The venue allows 20/s per connection bursting to 40. This adapter
        # deliberately spends less than that: the heartbeat reply must never
        # be the request that gets a 10028, because the venue treats a
        # rate-limited `public/test` as a missed heartbeat and closes 4008.
        return {"requests_per_second": int(RATE_PER_SEC)}

    def broker_info(self) -> dict:
        # The venue's own words only. Its documentation has no "about us"
        # block, no country and no licence, and SPEC 3.1 is explicit that
        # this field is not the place to invent them.
        return {"display_name": "Vertex Derivatives",
                "tagline": "USDC-margined perpetual swaps and dated futures",
                "mark": "VX",
                "asset_classes": ["Crypto perpetuals", "Crypto futures"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the venue something happens here, not in
        `hello` (SPEC 3.1): the socket, registration, auth, the catalog, the
        private channels and the background threads."""
        self.client.start()
        self._load_instruments()
        threading.Thread(target=self._dispatch_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._on_ready(False)

    def _on_ready(self, reconnected: bool):
        """Bring a freshly authenticated socket up to date.

        Called once at startup and again after every reconnect. The venue
        replays nothing and drops every subscription with the socket, so this
        is where subscriptions are re-armed and the book is re-read; a fill
        that happened while we were away shows up as a position change here
        rather than as a fill nobody ever saw.
        """
        self.client.call("private/subscribe", {"channels": PRIVATE_CHANNELS})
        with self._state_lock:
            quotes = list(self._quotes)
        if quotes:
            self.client.call("public/subscribe",
                             {"channels": ["ticker." + s for s in quotes]})
        acct = self.client.call("private/get_account_summary") or {}
        if acct.get("currency") and acct["currency"] != self.account_currency:
            # Cannot be corrected after the fact: `hello` has already gone
            # out. Loud on stderr is the only honest move left.
            log(f"WARNING: account currency is {acct['currency']!r}, but the "
                f"handshake declared {self.account_currency!r}")
        self._sync_positions(initial=not reconnected)
        self._set_session("connected",
                          "reconnected" if reconnected else "socket open")

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        self.client.close()

    def _set_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})

    def _call(self, method: str, params: dict | None = None, **kw):
        """A venue call whose failures arrive as protocol errors."""
        try:
            return self.client.call(method, params, **kw)
        except VertexError as e:
            raise to_adapter_error(e) from None

    # == catalog =======================================================
    def _load_instruments(self) -> dict:
        rows = self._call("public/get_instruments") or []
        with self._state_lock:
            self._instruments = {r["instrument_name"]: r for r in rows}
            return dict(self._instruments)

    def _instrument(self, symbol) -> dict:
        with self._state_lock:
            inst = self._instruments.get(symbol)
        if inst is None:
            self._load_instruments()            # catalogs change; re-read once
            with self._state_lock:
                inst = self._instruments.get(symbol)
        if inst is None:
            raise AdapterError("unknown_symbol",
                               f"no such instrument {symbol!r}")
        return inst

    @staticmethod
    def _spec(inst: dict) -> dict:
        sym = inst["instrument_name"]
        base = sym.split("-")[0]
        tick = _num(inst.get("tick_size"), 0.01)
        contract = _num(inst.get("contract_size"), 1.0)
        expires_us = inst.get("expiration_timestamp")
        expires_ms = _ms(expires_us) if expires_us else None
        kind = inst.get("kind") or "perpetual"
        if expires_ms:
            when = time.gmtime(expires_ms / 1000.0)
            name = (f"{base} future expiring "
                    f"{when.tm_year:04d}-{when.tm_mon:02d}-{when.tm_mday:02d}"
                    f", {contract} {base} per contract")
        else:
            name = f"{base} perpetual swap, {contract} {base} per contract"
        active = bool(inst.get("is_active", True))
        rate = _num(inst.get("margin_rate"), 0.02)
        return {
            "symbol": sym,
            "name": name,
            # A dated future is filed as a future and a perpetual under the
            # underlying's own class: the SPEC publishes no closed vocabulary
            # for asset_class, and a platform grouping a picker by it should
            # not put a September contract next to a perp. What each one is
            # exactly is said in `expires_at`, which is the field for it.
            "asset_class": "future" if expires_ms else "crypto",
            "base_ccy": base,
            "quote_ccy": inst.get("quote_currency") or "USDC",
            "digits": _digits_for(tick),
            "tick_size": tick,
            # Account currency IS the quote currency on every instrument here
            # (USDC throughout), so one tick on one contract is worth exactly
            # contract_size * tick_size and no cross is involved.
            "tick_value": round(contract * tick, 12),
            # "Every amount/size field is a count of contracts (always a whole
            # number)": the step is one contract and there is no sub-contract
            # size on this venue.
            "lot_step": 1.0,
            "min_qty": _num(inst.get("min_trade_amount"), 1.0),
            "max_qty": _num(inst.get("max_trade_amount"), 0.0),
            "min_notional": 0,
            "contract_size": contract,
            "margin_rate": rate,
            # The venue force-closes when equity / initial_margin falls below
            # 0.5, which is the same statement as a maintenance requirement of
            # half the initial one. Derived from the venue's own stop-out rule
            # rather than left to default to the initial rate, because stop-out
            # math runs on this number (SPEC 1.1).
            "margin_maintenance_rate": round(rate * 0.5, 10),
            "margin_tiers": None,
            # "All five instruments trade continuously (no daily close, no
            # weekend close)." Published as one explicit seven-day window and
            # NOT as an empty list: SPEC 3 says an empty list means "no
            # calendar published", so always-open and unknown would otherwise
            # look identical and only one of them is safe to gate orders on.
            "sessions": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                          "close": "24:00", "tz": "UTC"}],
            "holidays": [],
            "early_closes": [],
            "tradable": active,
            "shortable": True,
            "trade_mode": "full" if active else "disabled",
            "stops_level_points": 0,       # no stops exist, so no distance
            # "taker_commission is charged on notional (contracts x
            # contract_size x price) with a 0.01 USDC minimum per fill; there
            # is no separate maker rate on this venue."
            "fees": {"commission_per_lot": 0.0, "mode": "pct_notional",
                     "rate": _num(inst.get("taker_commission")), "min": 0.01},
            # No funding rate, no financing, no swap of any kind: "balance
            # only moves through trading (realized pnl and fees)". `none` says
            # that, where a 0 in points would claim a unit that does not exist.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            "expires_at": expires_ms,
            # The venue names no replacement contract for an expiring future
            # and lists two dated futures with no stated relationship. Guessing
            # that the December contract succeeds the September one would be
            # this adapter inventing a term the broker never published.
            "successor": None,
            "dividend": None,
        }

    def handle_catalog_list(self, params):
        insts = self._load_instruments()
        return {"instruments": [self._spec(i) for i in insts.values()]}

    # == account =======================================================
    @staticmethod
    def _account_view(a: dict) -> dict:
        return {"balance": _num(a.get("balance")),
                "equity": _num(a.get("equity")),
                # The venue calls initial margin `initial_margin` and free
                # margin `available_funds`; the protocol calls them
                # margin_used and margin_free.
                "margin_used": _num(a.get("initial_margin")),
                "margin_free": _num(a.get("available_funds")),
                "currency": a.get("currency") or "USDC"}

    def handle_account_get(self, params):
        return self._account_view(
            self._call("private/get_account_summary") or {})

    # == positions =====================================================
    def _position_view(self, p: dict) -> dict:
        return {
            "position_id": p["symbol"],
            "symbol": p["symbol"],
            "side": p["side"],
            "qty": p["qty"],
            "avg_price": p["avg_price"],
            "sl": None, "tp": None, "trail_points": 0,
            "unrealized_pnl": p.get("unrealized", 0.0),
            # No financing exists on this venue, so 0 is a fact rather than a
            # placeholder; commission is the fees actually charged on the
            # fills that built this position, negative because they cost the
            # holder.
            "swap": 0.0,
            "commission": -round(self._fees.get(p["symbol"], 0.0), 8),
            "opened_at": p.get("opened_at") or 0,
            "from_order": p.get("from_order"),
        }

    def handle_positions_list(self, params):
        self._sync_positions()
        with self._state_lock:
            rows = [dict(p) for p in self._pos.values()]
        return {"positions": [self._position_view(p) for p in rows]}

    def _fetch_positions(self) -> dict:
        rows = self._call("private/get_positions") or []
        out = {}
        for r in rows:
            sym = r.get("instrument_name")
            if not sym:
                continue
            out[sym] = {
                "symbol": sym,
                "side": "buy" if r.get("direction") == "buy" else "sell",
                # "size is always positive (in contracts); direction carries
                # the sign."
                "qty": _num(r.get("size")),
                "avg_price": _num(r.get("average_price")),
                "unrealized": _num(r.get("floating_profit_loss")),
                "index_price": _num(r.get("index_price")),
            }
        return out

    def _sync_positions(self, initial: bool = False, cause: dict | None = None):
        """Read the book and turn any REDUCTION into `position.closed`.

        This venue has no close event of any kind: a position simply reads
        smaller on the next `private/get_positions`, or is gone. So the book
        is the only witness, and the fetch is serialised (one in flight at a
        time) because two overlapping reads applied out of order would either
        report a close twice or resurrect a position that has gone.
        """
        with self._pos_fetch_lock:
            live = self._fetch_positions()
            now_ms = int(time.time() * 1000)
            closes = []
            with self._state_lock:
                for sym, prev in self._pos.items():
                    cur = live.get(sym)
                    flipped = cur is not None and cur["side"] != prev["side"]
                    if cur is None or flipped:
                        gone_qty = prev["qty"]
                    else:
                        gone_qty = round(prev["qty"] - cur["qty"], 10)
                    if gone_qty <= 1e-9:
                        continue
                    intent = self._closing.pop(sym, None)
                    if intent and time.time() - intent["since"] > INTENT_TTL:
                        intent = None
                    if not initial:
                        closes.append((sym, dict(prev), gone_qty,
                                       cur is None or flipped, intent))
                rebuilt = {}
                for sym, cur in live.items():
                    old = self._pos.get(sym)
                    if old and old["side"] == cur["side"]:
                        cur["opened_at"] = old.get("opened_at")
                        cur["from_order"] = old.get("from_order")
                    else:
                        cur["opened_at"] = self._opened_hint.pop(sym, None)
                        cur["from_order"] = None
                    rebuilt[sym] = cur
                self._pos = rebuilt
            for args in closes:
                self._emit_close(*args, cause=cause, at=now_ms)
            with self._state_lock:
                # Only AFTER the closes have been reported: `_emit_close` bills
                # the position's accumulated fees to the close it is
                # describing, and dropping the pool before it reads it would
                # report a commission of zero on every position.closed.
                for sym in list(self._fees):
                    if sym not in rebuilt:
                        self._fees.pop(sym, None)
            # An adopted position (this process did not open it: a restart, a
            # reconnect, another session) has no opening time we were told, so
            # it is reconstructed from the account's own trade history once.
            for sym, cur in list(rebuilt.items()):
                if cur.get("opened_at") is None:
                    cur["opened_at"] = self._opened_at_from_trades(sym)

    def _emit_close(self, sym, prev, qty, gone, intent, cause=None, at=None):
        """One reduction of one position, described as honestly as possible."""
        inst = None
        try:
            inst = self._instrument(sym)
        except AdapterError:
            pass
        contract = _num((inst or {}).get("contract_size"), 1.0)
        price = None
        if intent and intent.get("price"):
            price = _num(intent["price"])
        elif cause and cause.get("symbol") == sym and cause.get("price"):
            price = _num(cause["price"])
        if not price:
            # Nothing told us the price the close was done at (a liquidation,
            # an expiry closeout, a fill whose notification was lost). The
            # venue's own current mid for the instrument is the closest thing
            # it publishes, and using it is said out loud rather than passed
            # off as the traded price.
            price = _num((self._pos.get(sym) or {}).get("index_price")) \
                or _num(prev.get("index_price"))
            log(f"close of {sym}: no fill price reached us; realised P/L is "
                f"computed against the venue's current mark {price}")
        direction = 1.0 if prev["side"] == "buy" else -1.0
        realized = round((price - prev["avg_price"]) * direction * qty
                         * contract, 8)
        with self._state_lock:
            pool = self._fees.get(sym, 0.0)
            share = 1.0 if gone else (qty / prev["qty"] if prev["qty"] else 1.0)
            charged = round(pool * share, 8)
            self._fees[sym] = 0.0 if gone else round(pool - charged, 8)
            client_id = (intent or {}).get("client_id")
        reason = (intent or {}).get("reason")
        if reason is None:
            expires = (inst or {}).get("expiration_timestamp")
            if expires and _ms(expires) <= int(time.time() * 1000):
                # "Dated futures go inactive at their expiration_timestamp:
                # ... any open position is force-closed at the prevailing
                # mark." That is the one uncaused close this venue explains.
                reason = "expired"
            else:
                # Nothing asked for this and the venue says nothing about why
                # (its only other closer is the silent liquidation). SPEC 5:
                # `unknown` is a first-class outcome and beats a plausible
                # guess the platform would show with the confidence of a fact.
                reason = "unknown"
        data = {"position_id": sym, "qty": qty, "price": price,
                "realized_pnl": realized, "swap": 0.0,
                "commission": -charged, "reason": reason,
                "time": at or int(time.time() * 1000)}
        if client_id:
            data["client_id"] = client_id
        self.emit("position.closed", data)

    def _opened_at_from_trades(self, sym: str) -> int:
        """When the position now open in `sym` was opened, from the ledger.

        The venue never stamps a position with an opening time, so it is
        recovered by replaying this account's own trades in that instrument
        and taking the last moment the net size went from flat to not flat.
        Returns 0 when the answer is not recoverable (the ledger is capped at
        500 trades), which is the honest answer rather than "now".
        """
        try:
            rows = self.client.call("private/get_user_trades", {"count": 500})
        except VertexError as e:
            log(f"opened_at for {sym}: ledger unreadable ({e})")
            return 0
        rows = [r for r in (rows or []) if r.get("instrument_name") == sym]
        rows.sort(key=lambda r: _num(r.get("timestamp")))
        net, opened = 0.0, 0
        for r in rows:
            signed = _num(r.get("amount")) * (1 if r.get("side") == "buy"
                                              else -1)
            if abs(net) < 1e-9 and abs(signed) > 1e-9:
                opened = _ms(r.get("timestamp"))
            net = round(net + signed, 10)
            if abs(net) < 1e-9:
                opened = 0
        return opened

    # == orders ========================================================
    def _validate_qty(self, inst: dict, qty) -> float:
        """Size checked here, before the round trip.

        The venue answers a non-integer amount, an amount below the minimum
        and an amount above the maximum all with 11029, so its answer cannot
        be turned back into a precise reason. Checking locally means the
        platform gets `invalid_qty` with a message that says which of the
        three it was.
        """
        q = _num(qty, -1.0)
        lo = _num(inst.get("min_trade_amount"), 1.0)
        hi = _num(inst.get("max_trade_amount"), 0.0)
        if q <= 0:
            raise AdapterError("invalid_qty",
                               f"qty must be positive, got {qty!r}")
        if abs(q - round(q)) > 1e-9:
            raise AdapterError(
                "invalid_qty",
                f"amounts on this venue are whole contracts; {qty!r} is not")
        q = float(round(q))
        if q < lo - 1e-9:
            raise AdapterError("invalid_qty",
                               f"qty {q:g} below the minimum {lo:g} contracts")
        if hi and q > hi + 1e-9:
            raise AdapterError("invalid_qty",
                               f"qty {q:g} above the maximum {hi:g} contracts")
        return q

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        otype = (params.get("type") or "market").lower()
        # Everything that decides what gets DEALT. A replay carrying different
        # terms must be refused (SPEC 6); the venue will not refuse it for us,
        # because a reused `label` returns the ORIGINAL order however much the
        # instrument, side, size and price differ.
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
        inst = self._instrument(symbol)
        qty = self._validate_qty(inst, params.get("qty"))
        for field, what in (("sl", "stop loss"), ("tp", "take profit"),
                            ("trail_points", "trailing stop")):
            if params.get(field) is not None:
                raise AdapterError(
                    "unsupported",
                    f"Vertex has no {what} attachment; there are no stop "
                    f"orders of any kind on this venue")
        if otype in ("stop", "stop_limit"):
            raise AdapterError(
                "unsupported",
                f"Vertex accepts market and limit orders only, not {otype!r}")
        if otype not in ("market", "limit"):
            raise AdapterError("unsupported", f"unknown order type {otype!r}")
        expiry = (params.get("expiry") or "gtc").lower()
        if expiry != "gtc":
            # "time_in_force accepts only good_til_cancelled ... there is no
            # IOC, FOK, post-only or day-order variant on this venue."
            raise AdapterError("invalid_expiry",
                               f"Vertex honours good-til-cancelled only, "
                               f"not {expiry!r}")

        body = {"instrument_name": symbol, "amount": qty, "type": otype,
                "time_in_force": "good_til_cancelled",
                # The venue's own idempotency key. Passing the platform's
                # client_id straight through means an ambiguous network
                # failure can be retried against the VENUE's guarantee and
                # not only against this process's memory of it.
                "label": client_id}
        if otype == "limit":
            price = params.get("price")
            if price is None or _num(price) <= 0:
                raise AdapterError("x_invalid_price",
                                   "a limit order needs a positive `price`")
            body["price"] = _num(price)

        rec = {"order_id": None, "client_id": client_id, "symbol": symbol,
               "side": side, "qty": qty, "type": otype,
               "price": body.get("price"), "state": "new", "kind": "order",
               "placed_at": int(time.time() * 1000), "settled": False,
               "traded": False, "said": set()}
        with self._state_lock:
            self._by_label[client_id] = rec
            # A fill on the opposite side of an open position REDUCES it here
            # (netting), so the close it causes is the client's own doing.
            # Recorded BEFORE the call, because the venue's own notification
            # regularly reaches the dispatcher before this call returns and
            # whoever reconciles first must find the reason already filed.
            held = self._pos.get(symbol)
            intent = None
            if held is not None and held["side"] != side:
                intent = {"reason": "manual", "client_id": client_id,
                          "since": time.time(), "venue_close": False}
                self._closing[symbol] = intent
        try:
            r = self._call("private/" + side, body) or {}
        except AdapterError as e:
            with self._state_lock:
                self._by_label.pop(client_id, None)
                # By identity: a concurrent close on the same instrument that
                # filed its own intent must keep it.
                if intent is not None and self._closing.get(symbol) is intent:
                    self._closing.pop(symbol, None)
            # SPEC 8.4: no mutating call may be silently dropped. A synchronous
            # refusal is a terminal outcome and is announced as one, so a
            # platform watching only events still sees the order end.
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": e.code, "message": str(e)})
            raise
        order = r.get("order") or {}
        with self._state_lock:
            # The venue has now said what became of it, so the resting
            # announcement above is safe to make.
            rec["settled"] = True
        self._absorb_order(order)
        for t in r.get("trades") or []:
            self._absorb_trade(t)
        resting = str(order.get("order_state")) == "open"
        result = {"order_id": str(order.get("order_id")),
                  "status": "resting" if resting else "accepted"}
        self._wake.set()
        return self.idempotent_store(client_id, result, sig)

    def _kind_for(self, symbol, label) -> str:
        """Is this unrecognised order the venue's own closing order?

        `private/close_position` places the flattening market order ITSELF,
        with no label, so there is no key to recognise it by and it comes back
        looking exactly like a stranger's order. The one handle left is that
        this adapter asked for a close on that instrument moments earlier, so
        an unlabelled order arriving inside a close it requested is filed as
        the close. It matters because a close announced as `order.filled`
        makes a platform re-open the very position that just went: its own
        bookkeeping adds a position back on a fill and only removes one on
        `position.closed`, so the two events race and sometimes the wrong one
        wins.
        """
        if label is not None:
            return "order"
        with self._state_lock:
            intent = self._closing.get(symbol)
        if intent and intent.get("venue_close") and \
                time.time() - intent["since"] <= INTENT_TTL:
            return "close"
        return "order"

    @staticmethod
    def _order_state(o: dict, otype: str) -> str:
        """The venue's `order_state`, read against what kind of order it is.

        `open` is one word for two different situations here: a limit order
        genuinely resting on the book, and any order (market included) in the
        instant between acceptance and settling on the venue's own event loop.
        Reading it as `resting` unconditionally would announce every market
        order as a live resting order a moment before it fills.
        """
        state = str(o.get("order_state") or "")
        if state == "open":
            return "resting" if otype == "limit" else "working"
        return {"filled": "filled", "cancelled": "cancelled",
                "expired": "expired", "rejected": "cancelled"}.get(state,
                                                                   "working")

    def _absorb_order(self, o: dict):
        """Fold one venue order object into our view, announcing each fact once.

        The placing call's own response, the `user.orders` channel and the
        poller all describe the same order, repeatedly and in no guaranteed
        order, so what has already been said is remembered per order rather
        than inferred from a state transition. Keyed on transitions alone, a
        frame arriving twice would produce two tickets on the platform's book.
        """
        if not o:
            return
        oid = str(o.get("order_id"))
        label = o.get("label")
        with self._state_lock:
            rec = self._orders.get(oid)
            if rec is None and label:
                rec = self._by_label.get(label)
            if rec is None:
                # An order this process did not place: adopted after a restart,
                # or placed by another session on the same account. It gets a
                # record so its fills are still reported, with no client_id
                # because there honestly is none.
                rec = {"order_id": oid,
                       "client_id": label,
                       "kind": self._kind_for(o.get("instrument_name"), label),
                       "symbol": o.get("instrument_name"),
                       "side": o.get("direction"), "qty": _num(o.get("amount")),
                       "type": "limit" if o.get("price") is not None
                       else "market",
                       "price": o.get("price"), "state": "new",
                       "placed_at": _ms(o.get("creation_timestamp")),
                       # Nobody in this process is waiting on a reply for it.
                       "settled": True, "traded": False, "said": set()}
            rec["order_id"] = oid
            self._orders[oid] = rec
            if label:
                self._by_label[label] = rec
            state = self._order_state(o, rec.get("type"))
            rec["state"] = state
            said = rec["said"]
            kind = rec.get("kind")
            client_id = rec.get("client_id")
            # `order.placed` is held back until the placing call has returned:
            # the venue announces acceptance BEFORE it decides whether an
            # immediately marketable limit rests or deals, and a platform told
            # an order is live on the book a beat before it fills has been
            # told something that was never true.
            settled = rec.get("settled")
            todo = []
            for name, ready in (("accepted", True),
                                ("placed", state == "resting" and settled),
                                ("cancelled", state == "cancelled"),
                                ("expired", state == "expired")):
                if ready and name not in said:
                    said.add(name)
                    todo.append(name)
            recover = (state == "filled" and not rec.get("traded")
                       and "recovered" not in said)
            if recover:
                said.add("recovered")
        if kind != "order":
            return
        for name in todo:
            if name == "accepted":
                self.emit("order.accepted", {"client_id": client_id,
                                             "order_id": oid})
            elif name == "placed":
                self.emit("order.placed", {
                    "client_id": client_id, "order_id": oid,
                    "type": rec["type"], "price": _num(o.get("price")),
                    # Good-til-cancelled is the only time in force this venue
                    # accepts, so a resting order never carries an expiry.
                    "expires_at": None})
            elif name == "cancelled":
                self.emit("order.cancelled", {"order_id": oid,
                                              "client_id": client_id})
            elif name == "expired":
                self.emit("order.expired", {"order_id": oid,
                                            "client_id": client_id,
                                            "expiry": "gtc"})
        if recover:
            # The order says it filled but no trade for it ever reached us.
            # The ledger is the backstop: without this the fill would be
            # visible in the position book and nowhere in the event stream.
            self._recover_trades(oid)

    def _recover_trades(self, order_id: str):
        try:
            rows = self.client.call("private/get_user_trades", {"count": 200})
        except VertexError as e:
            log(f"could not recover trades for order {order_id}: {e}")
            return
        for t in rows or []:
            if str(t.get("order_id")) == str(order_id):
                self._absorb_trade(t)

    def _absorb_trade(self, t: dict):
        """One fill, announced exactly once.

        The venue delivers every fill TWICE by design: inline in the placing
        call's own response and again over `user.trades`, "the notification is
        not a second fill". `trade_id` is the identity that makes the two the
        same event, so it is the key everything here turns on.
        """
        if not t:
            return
        tid = t.get("trade_id")
        if tid is None:
            return
        with self._state_lock:
            if tid in self._seen_trades:
                return
            self._seen_trades.add(tid)
            self._trade_ring.append(tid)
            if len(self._trade_ring) == self._trade_ring.maxlen:
                # Bounded memory for a process that may run for weeks; the
                # oldest ids can no longer be replayed by anything.
                self._seen_trades.discard(self._trade_ring[0])
            oid = str(t.get("order_id"))
            sym = t.get("instrument_name")
            rec = self._orders.get(oid)
            if rec is None:
                # A trade can reach us before the order object it belongs to
                # (they are two separate notifications), so the same
                # classification runs here, and the verdict is REMEMBERED: the
                # order's own frame arrives after the intent that identified
                # it has been consumed, and re-deciding then would file the
                # venue's closing order as a stranger's new order.
                rec = {"order_id": oid, "client_id": None,
                       "kind": self._kind_for(sym, None), "symbol": sym,
                       "side": t.get("side"), "qty": _num(t.get("amount")),
                       "type": "market", "price": None, "state": "filled",
                       "placed_at": _ms(t.get("timestamp")),
                       "settled": True, "said": set()}
                self._orders[oid] = rec
            rec["traded"] = True
            fee = _num(t.get("fee"))
            self._fees[sym] = round(self._fees.get(sym, 0.0) + fee, 8)
            kind = rec["kind"]
            client_id = rec.get("client_id")
            intent = self._closing.get(sym)
            if intent is not None:
                # The fill that did the closing carries the price the close
                # was actually done at, which is the number `position.closed`
                # should report rather than a mark we looked up afterwards.
                intent["price"] = _num(t.get("price"))
            if sym not in self._pos:
                self._opened_hint[sym] = _ms(t.get("timestamp"))
        qty = _num(t.get("amount"))
        price = _num(t.get("price"))
        when = _ms(t.get("timestamp"))
        if kind == "order":
            remaining = None
            if rec is not None:
                remaining = round(_num(rec.get("qty")) - qty, 10)
            data = {"client_id": client_id, "order_id": oid,
                    "position_id": sym, "symbol": sym,
                    "side": t.get("side"), "qty": qty, "price": price,
                    # The venue reports a positive charge; the protocol wants
                    # the signed effect on the holder.
                    "fees": -fee, "time": when}
            if remaining is not None and remaining > 1e-9:
                data["remaining"] = remaining
                self.emit("order.partial", data)
            else:
                self.emit("order.filled", data)
        # Whatever the fill was, the book may now hold less than it did.
        self._sync_positions(cause={"symbol": sym, "price": price,
                                    "time": when})

    def handle_orders_list(self, params):
        """What this adapter placed and remembers, refreshed from the venue.

        `can_list` is declared FALSE because this is not an enumeration: the
        venue has no call that lists an account's orders, so anything placed
        by another session is invisible here and always will be. Answering
        with the partial truth beats refusing, as long as the handshake has
        already said it is partial (SPEC 3).
        """
        want = (params or {}).get("state")
        with self._state_lock:
            ids = [oid for oid, r in self._orders.items()
                   if r.get("kind") == "order"]
        out = []
        for oid in ids:
            try:
                o = self.client.call("private/get_order_state",
                                     {"order_id": int(oid)})
            except (VertexError, ValueError):
                continue
            self._absorb_order(o)
            out.append(self._order_view(o))
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def _order_view(self, o: dict) -> dict:
        oid = str(o.get("order_id"))
        with self._state_lock:
            rec = self._orders.get(oid) or {}
        is_limit = o.get("price") is not None
        state = self._order_state(o, "limit" if is_limit else "market")
        if state == "working":
            # Not a protocol state: to a platform an order the venue has
            # accepted but not yet settled is still on its way to the book.
            state = "resting"
        return {
            "order_id": oid,
            "client_id": rec.get("client_id") or o.get("label"),
            "symbol": o.get("instrument_name"),
            "type": "limit" if is_limit else "market",
            "side": "buy" if o.get("direction") == "buy" else "sell",
            "qty": _num(o.get("amount")),
            "price": _num(o.get("price")) if is_limit else None,
            "stop_price": None, "sl": None, "tp": None,
            # Good-til-cancelled is the only time in force this venue accepts,
            # so nothing ever carries an expiry time.
            "expiry": "gtc", "expires_at": None,
            "state": state,
            "placed_at": _ms(o.get("creation_timestamp")),
            "done_at": None if state == "resting"
            else _ms(o.get("last_update_timestamp")),
            "fill_price": _num(o.get("average_price")) if state == "filled"
            else None,
            # The venue never kills an order itself except by cancelling it
            # when the instrument expires, and it does not say so on the order.
            "reason": None,
        }

    def handle_order_cancel(self, params):
        oid = params.get("order_id")
        try:
            oid_int = int(str(oid))
        except (TypeError, ValueError):
            raise AdapterError("not_found",
                               f"order_id {oid!r} is not a Vertex order id")
        try:
            o = self.client.call("private/cancel", {"order_id": oid_int})
        except VertexError as e:
            if e.code == 11044:
                # 11044 is one code for "already filled / cancelled / expired"
                # AND for "never existed or is not yours", and the SPEC has two
                # codes for those. `get_order_state` refuses only the second,
                # so asking it is what tells them apart.
                try:
                    self.client.call("private/get_order_state",
                                     {"order_id": oid_int})
                except VertexError:
                    raise AdapterError("not_found",
                                       f"no order {oid} on this account") \
                        from None
                raise AdapterError("not_resting",
                                   f"order {oid} is no longer resting") \
                    from None
            raise to_adapter_error(e) from None
        self._absorb_order(o)
        return {}

    def handle_order_amend(self, params):
        # "There is no order amend call: to change price or size, cancel and
        # place a new order." Refusing loudly is what SPEC 3 asks for;
        # emulating it as cancel-and-replace would hand back a NEW order_id
        # and `order.amended` has no field to carry one.
        raise AdapterError("unsupported",
                           "Vertex cannot amend a resting order; cancel it "
                           "and place a new one")

    def handle_order_modify(self, params):
        raise AdapterError("unsupported",
                           "Vertex has no stop loss or take profit "
                           "attachment to modify")

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported", "Vertex has no close-by")

    # == closing =======================================================
    def handle_position_close(self, params):
        """Close a position, whole or in part.

        A whole close is `private/close_position`. A PART close is an
        opposite-side market order for the contracts to shed, which on a
        netting book is not an emulation of anything: it is how this venue
        reduces a position, and the venue skips the margin check for an order
        that only reduces exposure.
        """
        sym = params.get("position_id")
        if not sym:
            raise AdapterError("not_found", "position_id is required")
        qty = params.get("qty")
        client_id = params.get("client_id")
        sig = ("close", sym, qty)
        if client_id:
            prior = self.idempotent_check(client_id, sig)
            if prior is not None:
                return prior
        self._close_symbol(sym, qty, reason="manual", client_id=client_id)
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, sig)
        return result

    def _close_symbol(self, sym: str, qty=None, reason: str = "manual",
                      client_id=None, label=None):
        with self._state_lock:
            held = self._pos.get(sym)
        if held is None:
            self._sync_positions()
            with self._state_lock:
                held = self._pos.get(sym)
        if held is None:
            raise AdapterError("not_found",
                               f"no open position in {sym!r}")
        want = _num(qty, held["qty"]) if qty is not None else held["qty"]
        if want <= 0 or want > held["qty"] + 1e-9:
            raise AdapterError("invalid_qty",
                               f"cannot close {want:g} of a {held['qty']:g} "
                               f"contract position")
        full = want >= held["qty"] - 1e-9
        # Filed BEFORE the call goes out: the venue's own notification for
        # this close regularly reaches the dispatcher thread before the reply
        # reaches this one, and whichever of the two reconciles first must
        # find the reason (and the client_id) already recorded. `venue_close`
        # says the closing order will be the venue's OWN unlabelled one, which
        # is the only way to recognise it (see _kind_for).
        with self._state_lock:
            self._closing[sym] = {"reason": reason, "client_id": client_id,
                                  "since": time.time(), "venue_close": full}
        # A label makes the close idempotent AT THE VENUE, so an ambiguous
        # failure can be retried without risking a second, opposite position.
        label = label or ("pc-" + secrets.token_hex(8))
        rec = {"order_id": None, "client_id": client_id, "symbol": sym,
               "side": "sell" if held["side"] == "buy" else "buy",
               "qty": want, "type": "market", "price": None, "state": "new",
               "kind": "close", "placed_at": int(time.time() * 1000),
               "settled": True, "traded": False, "said": set()}
        with self._state_lock:
            if not full:
                # Only the reducing order carries a label; the venue's own
                # close_position order has none to give it.
                self._by_label[label] = rec
            intent = self._closing.get(sym)
        try:
            if full:
                r = self._call("private/close_position",
                               {"instrument_name": sym}) or {}
            else:
                r = self._call("private/" + rec["side"],
                               {"instrument_name": sym, "amount": want,
                                "type": "market",
                                "time_in_force": "good_til_cancelled",
                                "label": label}) or {}
        except AdapterError:
            with self._state_lock:
                # Refused, so nothing is in flight. Removed by identity, so a
                # concurrent close that filed its own intent keeps it.
                if self._closing.get(sym) is intent:
                    self._closing.pop(sym, None)
                self._by_label.pop(label, None)
            raise
        self._absorb_order(r.get("order") or {})
        for t in r.get("trades") or []:
            self._absorb_trade(t)
        self._sync_positions()

    def handle_flatten_all(self, params):
        """Close everything and do not come back until the book is empty.

        SPEC 8.1 wants this to work while other calls are failing, so every
        position is attempted independently and one refusal never abandons the
        rest. Resting orders go first: an order left working re-opens the
        position that was just closed.
        """
        try:
            self.client.call("private/cancel_all")
        except VertexError as e:
            log(f"flatten: could not cancel resting orders: {e}")
        self._sync_positions()
        with self._state_lock:
            symbols = list(self._pos)
        closed = 0
        for sym in symbols:
            try:
                self._close_symbol(sym, reason="flatten")
                closed += 1
            except AdapterError as e:
                log(f"flatten: {sym} did not close: {e}")
        self._sync_positions()
        return {"closed": closed}

    # == market data ===================================================
    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._instrument(s)          # unknown_symbol before subscribing
        with self._state_lock:
            new = [s for s in symbols if s not in self._quotes]
            self._quotes.extend(new)
        if new:
            self._call("public/subscribe",
                       {"channels": ["ticker." + s for s in new]})
        # A snapshot immediately, from the venue's own ticker call. The
        # channel only speaks on a TICK, and ticks here are irregular
        # ("sub-second to low-second intervals, ... do not assume a fixed
        # cadence"), so a subscriber would otherwise wait an unknown time for
        # a price the venue can state on demand.
        for s in symbols:
            try:
                self._emit_quote(s, self.client.call(
                    "public/ticker", {"instrument_name": s}) or {})
            except VertexError as e:
                log(f"quote snapshot for {s} failed: {e}")
        return {}

    def handle_quotes_unsubscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        with self._state_lock:
            drop = [s for s in symbols if s in self._quotes]
            self._quotes = [s for s in self._quotes if s not in set(drop)]
        if drop:
            try:
                self.client.call("public/unsubscribe",
                                 {"channels": ["ticker." + s for s in drop]})
            except VertexError as e:
                log(f"unsubscribe failed: {e}")
        return {}

    def _emit_quote(self, symbol: str, data: dict):
        self.emit("quote", {"symbol": symbol,
                            "bid": _num(data.get("best_bid_price")),
                            "ask": _num(data.get("best_ask_price")),
                            "time": _ms(data.get("timestamp"))})

    def handle_bars_history(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        resolution, secs = TF[tf]
        count = max(1, min(int(_num(params.get("count"), 200)), 5000))
        end = int(time.time())
        if params.get("to"):
            end = int(_num(params["to"]) / 1000.0)
        start = end - (count + 2) * secs
        if params.get("from"):
            start = int(_num(params["from"]) / 1000.0)
        data = self._call("public/get_tradingview_chart_data", {
            "instrument_name": symbol, "resolution": resolution,
            "start_timestamp": _us(start * 1000),
            "end_timestamp": _us(end * 1000)}) or {}
        # Parallel arrays, oldest first: the venue's columnar candle shape (a
        # separate array per field). A short or ragged set is treated as the
        # truncation the venue warns about rather than trusted blindly into
        # an index error.
        ticks = data.get("ticks") or []
        cols = [data.get(k) or [] for k in ("open", "high", "low", "close",
                                            "volume")]
        n = min([len(ticks)] + [len(c) for c in cols])
        bars = [[_ms(ticks[i]), _num(cols[0][i]), _num(cols[1][i]),
                 _num(cols[2][i]), _num(cols[3][i]), _num(cols[4][i])]
                for i in range(n)]
        bars.sort(key=lambda b: b[0])
        return {"bars": bars[-count:]}

    def handle_bars_subscribe(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        secs = TF[tf][1]
        # The watermark starts at the bar that has ALREADY closed, so a
        # subscription delivers the buckets that close from here on and not a
        # burst of recent history nobody asked for. Past bars are
        # `bars.history`'s job and arrive there in one answer.
        watermark = (int(time.time() // secs) * secs - secs) * 1000
        with self._state_lock:
            self._bars.setdefault((symbol, tf), watermark)
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._state_lock:
            self._bars.pop((params.get("symbol"),
                            params.get("timeframe") or "1m"), None)
        return {}

    # == history =======================================================
    def handle_history_fills(self, params):
        rows = self._call("private/get_user_trades", {"count": 500}) or []
        lo = _num((params or {}).get("from"), 0.0)
        hi = _num((params or {}).get("to"), 0.0)
        fills = []
        for t in rows:
            when = _ms(t.get("timestamp"))
            if lo and when < lo:
                continue
            if hi and when > hi:
                continue
            fills.append({
                "fill_id": str(t.get("trade_id")),
                "order_id": str(t.get("order_id")),
                "symbol": t.get("instrument_name"),
                "side": t.get("side"),
                "qty": _num(t.get("amount")),
                "price": _num(t.get("price")),
                "fees": -_num(t.get("fee")),
                # This venue publishes no per-trade realised figure: a trade
                # row is a print, and what it closed is not on it. Reporting
                # arithmetic here as if it were the venue's own number is
                # exactly what SPEC 5 warns against, so the field is absent.
                "time": when,
            })
        # The venue returns newest first; the protocol reads oldest first.
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == notifications =================================================
    def _on_notification(self, channel, data):
        """Reader-thread hand-off. NOTHING here may wait on the venue."""
        if channel:
            self._notify_q.put((channel, data))

    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                channel, data = self._notify_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._on_channel(channel, data)
            except Exception as e:      # noqa: BLE001 - one bad frame only
                log(f"dispatch {channel}: {type(e).__name__}: {e}")

    def _on_channel(self, channel: str, data):
        if channel.startswith("ticker."):
            self._emit_quote(channel[len("ticker."):], data or {})
        elif channel == "user.orders":
            self._absorb_order(data or {})
        elif channel == "user.trades":
            self._absorb_trade(data or {})
        elif channel == "user.portfolio":
            view = self._account_view(data or {})
            self.emit("account.updated", view)
            self._check_margin(view)
        else:
            log(f"unknown channel {channel!r}")

    def _check_margin(self, view: dict):
        """The venue liquidates at equity/initial_margin < 0.5 with no warning
        message of its own, and says so: "watch user.portfolio if you need to
        see it coming". This is that watch, turned into the event the protocol
        has for it."""
        used = view.get("margin_used") or 0.0
        if used <= 0:
            self._margin_warned = False
            return
        level = view["equity"] / used
        if level < 0.75 and not self._margin_warned:
            self._margin_warned = True
            self.emit("margin.warning", {"level": round(level, 4)})
        elif level >= 1.0:
            self._margin_warned = False

    # == the poller ====================================================
    def _poll_loop(self):
        """The backstop for everything a notification may drop.

        The venue replays nothing, so a frame lost to a reconnect is lost for
        good; re-reading the book and any live order turns that into latency
        instead of a platform whose position list quietly disagrees with the
        broker's.
        """
        while not self._stop.is_set():
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._reap_orders()
                self._sync_positions()
                self._reap_bars()
                self._reap_catalog()
            except AdapterError as e:
                log(f"poll: {e}")
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log(f"poll: {type(e).__name__}: {e}")

    def _reap_orders(self):
        with self._state_lock:
            live = [oid for oid, r in self._orders.items()
                    if r["state"] in ("new", "working", "resting")
                    and r["order_id"]]
        for oid in live:
            try:
                self._absorb_order(self.client.call(
                    "private/get_order_state", {"order_id": int(oid)}))
            except (VertexError, ValueError) as e:
                log(f"poll order {oid}: {e}")

    def _reap_bars(self):
        if time.time() - self._last_bar_poll < BAR_POLL_INTERVAL:
            return
        self._last_bar_poll = time.time()
        with self._state_lock:
            wants = dict(self._bars)
        for (symbol, tf), last in wants.items():
            resolution, secs = TF[tf]
            end = int(time.time())
            try:
                data = self.client.call("public/get_tradingview_chart_data", {
                    "instrument_name": symbol, "resolution": resolution,
                    "start_timestamp": _us((end - 4 * secs) * 1000),
                    "end_timestamp": _us(end * 1000)}) or {}
            except VertexError as e:
                log(f"bar poll {symbol} {tf}: {e}")
                continue
            ticks = data.get("ticks") or []
            for i, raw in enumerate(ticks):
                t = _ms(raw)
                if t <= last:
                    continue
                # The venue only ever returns buckets that have fully closed,
                # which is exactly what the protocol wants emitted.
                self.emit("bar", {"symbol": symbol, "timeframe": tf, "bar": [
                    t, _num((data.get("open") or [None])[i]),
                    _num((data.get("high") or [None])[i]),
                    _num((data.get("low") or [None])[i]),
                    _num((data.get("close") or [None])[i]),
                    _num((data.get("volume") or [None])[i])]})
                with self._state_lock:
                    if (symbol, tf) in self._bars:
                        self._bars[(symbol, tf)] = t

    def _reap_catalog(self):
        """Dated futures go inactive at their expiry; the catalog is not
        static and a platform that read it once would keep offering a contract
        this venue has stopped dealing (SPEC 5)."""
        if time.time() - self._last_catalog_poll < CATALOG_POLL_INTERVAL:
            return
        self._last_catalog_poll = time.time()
        with self._state_lock:
            before = {s: bool(i.get("is_active"))
                      for s, i in self._instruments.items()}
        after = self._load_instruments()
        changed = [s for s, i in after.items()
                   if s in before and bool(i.get("is_active")) != before[s]]
        if not changed:
            return
        expired = [s for s in changed if not after[s].get("is_active")]
        for reason, syms in (("expired", expired),
                             ("resumed", [s for s in changed
                                          if s not in expired])):
            if syms:
                self.emit("catalog.changed", {
                    "reason": reason, "symbols": syms,
                    "instruments": [self._spec(after[s]) for s in syms]})


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a
    # pipe buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    VertexAdapter().run()


if __name__ == "__main__":
    main()
