"""Alpaca adapter: the hand-written websocket client against a loopback server.

Alpaca's paper order stream sends every message as a BINARY frame holding
JSON. A client that drops binary frames connects, authenticates and hears
nothing. These tests speak RFC 6455 to the client from a scripted server on
127.0.0.1: binary, fragmented, long, interleaved with pings, cut in half by
a read timeout. Standard library only; nothing leaves the machine.
"""
import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest

from brueconnect.adapters.alpaca import alpaca_adapter as mod

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONT, TEXT, BIN, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def frame(opcode: int, payload: bytes = b"", fin: bool = True) -> bytes:
    """A server frame: never masked."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack("!H", n)
    else:
        head.append(127)
        head += struct.pack("!Q", n)
    return bytes(head) + payload


def read_exact(conn, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise OSError("client went away")
        out += chunk
    return out


def read_frame(conn):
    """(opcode, payload, masked) for one client frame."""
    b0, b1 = read_exact(conn, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", read_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", read_exact(conn, 8))[0]
    masked = bool(b1 & 0x80)
    key = read_exact(conn, 4) if masked else b""
    data = read_exact(conn, n)
    if masked:
        data = bytes(c ^ key[i % 4] for i, c in enumerate(data))
    return b0 & 0x0F, data, masked


class Server:
    """One connection, one script, on an ephemeral loopback port."""

    def __init__(self, script, status="101 Switching Protocols",
                 accept=None):
        self.script, self.status, self.accept = script, status, accept
        self.headers, self.seen, self.error = {}, [], None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.url = "ws://127.0.0.1:%d/stream" % self.sock.getsockname()[1]
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self.sock.accept()
            with conn:
                conn.settimeout(5.0)
                head = b""
                while b"\r\n\r\n" not in head:
                    head += conn.recv(4096)
                lines = head.decode("latin-1").split("\r\n")
                self.request_line = lines[0]
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    self.headers[name.strip().lower()] = value.strip()
                accept = self.accept or base64.b64encode(hashlib.sha1((
                    self.headers.get("sec-websocket-key", "") + GUID
                ).encode()).digest()).decode()
                conn.sendall(("HTTP/1.1 %s\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\n"
                              "Sec-WebSocket-Accept: %s\r\n\r\n"
                              % (self.status, accept)).encode())
                self.script(self, conn)
        except Exception as e:  # noqa: BLE001 - reported by the test
            self.error = e
        finally:
            self.done.set()
            self.sock.close()

    def expect(self, conn):
        got = read_frame(conn)
        self.seen.append(got)
        return got


class WebSocketClient(unittest.TestCase):
    def connect(self, script, **kw):
        self.server = Server(script, **{k: kw.pop(k) for k in
                                        ("status", "accept") if k in kw})
        ws = mod._WebSocket(self.server.url, timeout=3.0, **kw)
        self.addCleanup(ws.close)
        return ws

    def finish(self):
        self.assertTrue(self.server.done.wait(5.0), "server script hung")
        if self.server.error:
            raise self.server.error

    def test_binary_frame_carrying_json_is_delivered(self):
        auth = {"stream": "authorization",
                "data": {"action": "authenticate", "status": "authorized"}}

        def script(s, conn):
            conn.sendall(frame(BIN, json.dumps(auth).encode()))
            conn.sendall(frame(TEXT, b'{"stream":"listening"}'))
        ws = self.connect(script)
        self.assertEqual(json.loads(ws.recv()), auth)
        self.assertEqual(json.loads(ws.recv()), {"stream": "listening"})
        self.finish()

    def test_fragmented_binary_with_a_ping_between_the_pieces(self):
        body = json.dumps({"stream": "trade_updates", "data": {
            "event": "fill", "qty": "0.0002", "note": "x" * 500}}).encode()
        a, b, c = body[:100], body[100:300], body[300:]

        def script(s, conn):
            conn.sendall(frame(BIN, a, fin=False))
            conn.sendall(frame(PING, b"are-you-there"))
            conn.sendall(frame(CONT, b, fin=False))
            conn.sendall(frame(CONT, c, fin=True))
            s.expect(conn)
        ws = self.connect(script)
        self.assertEqual(ws.recv().encode(), body)
        self.finish()
        self.assertEqual(self.server.seen, [(PONG, b"are-you-there", True)])

    def test_the_long_length_forms(self):
        mid, big = b"m" * 300, b"b" * 70011

        def script(s, conn):
            conn.sendall(frame(BIN, mid))
            conn.sendall(frame(TEXT, big))
        ws = self.connect(script)
        self.assertEqual(ws.recv(), mid.decode())
        self.assertEqual(ws.recv(), big.decode())
        self.finish()

    def test_client_frames_are_masked_and_headers_are_sent(self):
        def script(s, conn):
            s.expect(conn)
            s.expect(conn)
            s.expect(conn)
        ws = self.connect(script, headers={"User-Agent": "brue-test/1"})
        ws.send_json({"action": "auth", "key": "k", "secret": "s"})
        ws.send_json({"pad": "y" * 70000})
        ws.ping(b"hb")
        self.finish()
        (op1, d1, m1), (op2, d2, m2), (op3, d3, m3) = self.server.seen
        self.assertEqual((op1, m1), (TEXT, True))
        self.assertEqual(json.loads(d1),
                         {"action": "auth", "key": "k", "secret": "s"})
        self.assertEqual((op2, m2, len(json.loads(d2)["pad"])),
                         (TEXT, True, 70000))
        self.assertEqual((op3, d3, m3), (PING, b"hb", True))
        self.assertEqual(self.server.headers["user-agent"], "brue-test/1")
        self.assertEqual(self.server.headers["sec-websocket-version"], "13")
        self.assertNotIn("sec-websocket-extensions", self.server.headers)
        self.assertEqual(self.server.request_line, "GET /stream HTTP/1.1")

    def test_a_read_timeout_does_not_lose_half_a_frame(self):
        body = b'{"stream":"trade_updates","data":{"event":"new"}}'
        whole = frame(BIN, body)
        go = threading.Event()

        def script(s, conn):
            conn.sendall(whole[:7])
            go.wait(5.0)
            conn.sendall(whole[7:])
            conn.sendall(frame(BIN, b'{"n":2}'))
        ws = self.connect(script)
        with self.assertRaises(mod.WSTimeout):
            ws.recv(timeout=0.15)
        with self.assertRaises(mod.WSTimeout):
            ws.recv(timeout=0.05)
        go.set()
        self.assertEqual(ws.recv(timeout=3.0).encode(), body)
        self.assertEqual(ws.recv(timeout=3.0), '{"n":2}')
        self.finish()

    def test_a_quiet_stream_times_out_and_stays_usable(self):
        def script(s, conn):
            s.expect(conn)                      # the client's ping
            conn.sendall(frame(PONG, b"hb"))
            conn.sendall(frame(BIN, b'{"late":true}'))
        ws = self.connect(script)
        before = ws.last_rx
        with self.assertRaises(mod.WSTimeout):
            ws.recv(timeout=0.1)
        time.sleep(0.01)
        ws.ping(b"hb")
        self.assertEqual(ws.recv(timeout=3.0), '{"late":true}')
        self.assertGreater(ws.last_rx, before)
        self.finish()

    def test_peer_close_is_echoed_and_reported(self):
        def script(s, conn):
            conn.sendall(frame(BIN, b'{"last":1}'))
            conn.sendall(frame(CLOSE, struct.pack("!H", 1001) + b"bye"))
            s.expect(conn)
        ws = self.connect(script)
        self.assertEqual(ws.recv(), '{"last":1}')
        self.assertIsNone(ws.recv())
        self.assertEqual(ws.close_code, 1001)
        self.finish()
        self.assertEqual(self.server.seen,
                         [(CLOSE, struct.pack("!H", 1001), True)])

    def test_a_dropped_connection_raises(self):
        def script(s, conn):
            conn.sendall(frame(BIN, b'{"a":1}')[:5])
        ws = self.connect(script)
        with self.assertRaises(mod.WSClosed):
            ws.recv(timeout=3.0)
        self.finish()

    def test_an_oversize_frame_is_refused_before_it_is_read(self):
        def script(s, conn):
            conn.sendall(frame(BIN, b"z" * 5000))
        ws = self.connect(script, max_message=1024)
        with self.assertRaises(mod.WSClosed) as ctx:
            ws.recv()
        self.assertEqual(ctx.exception.code, 1009)

    def test_upgrade_refused_and_wrong_accept(self):
        with self.assertRaises(mod.WSClosed):
            self.connect(lambda s, conn: None, status="403 Forbidden")
        with self.assertRaises(mod.WSClosed):
            self.connect(lambda s, conn: None, accept="bm90IHRoZSBrZXk=")


if __name__ == "__main__":
    unittest.main()
