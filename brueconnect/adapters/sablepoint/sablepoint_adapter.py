#!/usr/bin/env python3
"""Brue Connect adapter for Sable Point Capital (multi-asset prime broker).

Sable Point is gRPC-only: one service, `sablepoint.v1.SablePoint`, spoken over
plaintext HTTP/2 (h2c, no TLS, no ALPN), with two server-streaming calls on the
same connection and every timestamp in int64 unix NANOseconds. There is no REST
surface at all, so there is no easy road: this file speaks the wire itself.

grpcio is not importable here (SPEC: an adapter is spawned by whatever python
the user's machine has), so the four layers under the venue are implemented in
this file, bottom up, on `socket` and `struct` alone:

  1. protobuf: varints, the four wire types this venue uses, and skipping
     unknown fields rather than failing on them, so a venue that adds a field
     tomorrow does not take the adapter down today.
  2. HPACK: REQUEST headers go out as literals-without-indexing, so this side
     keeps no encoder table and cannot desynchronise one. RESPONSE headers are
     really decoded, because grpc's server does use Huffman and the static and
     dynamic tables; the RFC 7541 code table is carried below as data.
  3. HTTP/2: the connection preface, SETTINGS with an ACK, PING answered with a
     PING ACK, and WINDOW_UPDATE issued as data is received. That last one is
     not decoration: an event stream that runs for hours dies silently once the
     65 KB default flow-control window empties and nobody refills it.
  4. gRPC: 1-byte flag + 4-byte length framing, and the truth read from the
     grpc-status TRAILER. HTTP 200 means nothing here; a rejected order comes
     back 200 with `grpc-status: 9` and the venue's own code in grpc-message.

Shape of the adapter above that:

  * `hello` answers COLD (SPEC 3.1): no socket, no account, no stream. The
    account is opened and the streams started in `on_first_call`.
  * one background thread holds `StreamEvents` (the account's book) and another
    holds `StreamMarketData` (quotes). Both re-open on failure; neither replays,
    so every reconnect re-reads orders, positions and fills over unary calls.
  * every venue fact is folded through a per-order / per-fill / per-position
    state machine that emits only on a TRANSITION, and fills are deduplicated on
    the venue's own `fill_id`. A re-read after a reconnect therefore cannot turn
    one fill into two on the platform's book.
  * the venue's `TradeEvent` carries no reason and no realised P/L on a close:
    a stop-loss exit and a manual close are the same POSITION_CLOSED message.
    So closes are RESOLVED rather than guessed (see `_resolve_close`): what this
    adapter itself asked for, the STOP_OUT event, the companion FILL, the exit
    print read back from `ListFills` and the balance delta the venue publishes
    are combined into one answer, and where they do not settle it the reason is
    reported as `unknown` (SPEC 5) instead of a plausible-looking invention.
"""
from __future__ import annotations

import collections
import json
import os
import re
import socket
import struct
import sys
import threading
import time

# The SDK lives at the repo root, four directories up from this file. Derived
# rather than hardcoded so the adapter runs from a checkout, from an installed
# tree, or from the desktop build's bundle without an env var to set.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

ADDR = os.environ.get("SABLEPOINT_ADDR", "127.0.0.1:7671")
# The label the venue stores against the account it opens for us. The venue
# never looks anything up by it (it says so), so it is a human breadcrumb only.
DISPLAY_NAME = os.environ.get("SABLEPOINT_DISPLAY_NAME", "LSE Terminal")

SERVICE = "/sablepoint.v1.SablePoint/"


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print("[sablepoint] %s" % msg, file=sys.stderr, flush=True)


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# =====================================================================
# Layer 1: protobuf wire format
# =====================================================================
# Encode and decode by hand. Only the four wire types this contract uses are
# implemented; groups (3 and 4) were removed from proto3 and are refused rather
# than mis-parsed.

WT_VARINT, WT_64, WT_LEN, WT_32 = 0, 1, 2, 5


def _uvarint(buf: bytes, i: int):
    shift = 0
    val = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _uvarint_enc(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def pb_parse(buf: bytes) -> dict:
    """Wire bytes -> {field_number: [raw values]}.

    Unknown fields land in the same dict and are simply never read, which is
    what the SPEC's forward-compatibility rule asks of a decoder: a venue that
    ships a new field must not break an integrator who has not regenerated.
    Repeated fields keep every occurrence; scalars take the last, which is what
    proto3 says about a field that appears twice.
    """
    out: dict = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _uvarint(buf, i)
        fno, wt = key >> 3, key & 7
        if wt == WT_VARINT:
            v, i = _uvarint(buf, i)
        elif wt == WT_64:
            v, i = buf[i:i + 8], i + 8
        elif wt == WT_LEN:
            ln, i = _uvarint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == WT_32:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("proto wire type %d is not used by this contract"
                             % wt)
        out.setdefault(fno, []).append(v)
    return out


def f_int(d: dict, no: int, default: int = 0) -> int:
    v = d.get(no)
    if not v:
        return default
    n = v[-1]
    if not isinstance(n, int):
        return default
    # int64 on the wire is a plain varint, so a negative value arrives as its
    # two's complement. Every id and timestamp here is positive; this exists so
    # one would not be read as 1.8e19 if it ever were not.
    return n - (1 << 64) if n >= (1 << 63) else n


def f_dbl(d: dict, no: int, default: float = 0.0) -> float:
    v = d.get(no)
    if not v or not isinstance(v[-1], (bytes, bytearray)) or len(v[-1]) != 8:
        return default
    return struct.unpack("<d", bytes(v[-1]))[0]


def f_str(d: dict, no: int, default: str = "") -> str:
    v = d.get(no)
    if not v or not isinstance(v[-1], (bytes, bytearray)):
        return default
    return bytes(v[-1]).decode("utf-8", "replace")


def f_msg(d: dict, no: int) -> dict:
    v = d.get(no)
    if not v or not isinstance(v[-1], (bytes, bytearray)):
        return {}
    return pb_parse(bytes(v[-1]))


def f_rep(d: dict, no: int) -> list:
    return [pb_parse(bytes(x)) for x in (d.get(no) or [])
            if isinstance(x, (bytes, bytearray))]


def e_varint(no: int, v) -> bytes:
    v = int(v or 0)
    if v == 0:
        return b""          # proto3: a default-valued scalar is not sent
    if v < 0:
        v += 1 << 64
    return _uvarint_enc(no << 3) + _uvarint_enc(v)


def e_dbl(no: int, v) -> bytes:
    v = float(v or 0.0)
    if v == 0.0:
        return b""
    return _uvarint_enc((no << 3) | WT_64) + struct.pack("<d", v)


def e_str(no: int, s) -> bytes:
    if not s:
        return b""
    b = s.encode("utf-8")
    return _uvarint_enc((no << 3) | WT_LEN) + _uvarint_enc(len(b)) + b


# =====================================================================
# Layer 2: HPACK (RFC 7541)
# =====================================================================
# Requests leave as literals WITHOUT indexing, so this side never maintains an
# encoder dynamic table and can never desynchronise the server's copy of one.
# Responses must be decoded properly: grpc's server Huffman-codes its literals
# and indexes what it repeats.

HPACK_STATIC = (
    (':authority', ''), (':method', 'GET'), (':method', 'POST'),
    (':path', '/'), (':path', '/index.html'), (':scheme', 'http'),
    (':scheme', 'https'), (':status', '200'), (':status', '204'),
    (':status', '206'), (':status', '304'), (':status', '400'),
    (':status', '404'), (':status', '500'), ('accept-charset', ''),
    ('accept-encoding', 'gzip, deflate'), ('accept-language', ''),
    ('accept-ranges', ''), ('accept', ''),
    ('access-control-allow-origin', ''), ('age', ''), ('allow', ''),
    ('authorization', ''), ('cache-control', ''), ('content-disposition', ''),
    ('content-encoding', ''), ('content-language', ''), ('content-length', ''),
    ('content-location', ''), ('content-range', ''), ('content-type', ''),
    ('cookie', ''), ('date', ''), ('etag', ''), ('expect', ''),
    ('expires', ''), ('from', ''), ('host', ''), ('if-match', ''),
    ('if-modified-since', ''), ('if-none-match', ''), ('if-range', ''),
    ('if-unmodified-since', ''), ('last-modified', ''), ('link', ''),
    ('location', ''), ('max-forwards', ''), ('proxy-authenticate', ''),
    ('proxy-authorization', ''), ('range', ''), ('referer', ''),
    ('refresh', ''), ('retry-after', ''), ('server', ''), ('set-cookie', ''),
    ('strict-transport-security', ''), ('transfer-encoding', ''),
    ('user-agent', ''), ('vary', ''), ('via', ''), ('www-authenticate', ''),
)

# RFC 7541 Appendix B, the canonical Huffman code, as DATA: one code per symbol
# 0..255 plus EOS at 256, and the bit length of each. Carried as a table and
# turned into a lookup below rather than written out as branching code, because
# a hand-transcribed decoder is wrong in exactly one entry and the failure looks
# like a corrupt header rather than like a typo.
_HUFF_CODES = (
    0x00001ff8, 0x007fffd8, 0x0fffffe2, 0x0fffffe3, 0x0fffffe4, 0x0fffffe5,
    0x0fffffe6, 0x0fffffe7, 0x0fffffe8, 0x00ffffea, 0x3ffffffc, 0x0fffffe9,
    0x0fffffea, 0x3ffffffd, 0x0fffffeb, 0x0fffffec, 0x0fffffed, 0x0fffffee,
    0x0fffffef, 0x0ffffff0, 0x0ffffff1, 0x0ffffff2, 0x3ffffffe, 0x0ffffff3,
    0x0ffffff4, 0x0ffffff5, 0x0ffffff6, 0x0ffffff7, 0x0ffffff8, 0x0ffffff9,
    0x0ffffffa, 0x0ffffffb, 0x00000014, 0x000003f8, 0x000003f9, 0x00000ffa,
    0x00001ff9, 0x00000015, 0x000000f8, 0x000007fa, 0x000003fa, 0x000003fb,
    0x000000f9, 0x000007fb, 0x000000fa, 0x00000016, 0x00000017, 0x00000018,
    0x00000000, 0x00000001, 0x00000002, 0x00000019, 0x0000001a, 0x0000001b,
    0x0000001c, 0x0000001d, 0x0000001e, 0x0000001f, 0x0000005c, 0x000000fb,
    0x00007ffc, 0x00000020, 0x00000ffb, 0x000003fc, 0x00001ffa, 0x00000021,
    0x0000005d, 0x0000005e, 0x0000005f, 0x00000060, 0x00000061, 0x00000062,
    0x00000063, 0x00000064, 0x00000065, 0x00000066, 0x00000067, 0x00000068,
    0x00000069, 0x0000006a, 0x0000006b, 0x0000006c, 0x0000006d, 0x0000006e,
    0x0000006f, 0x00000070, 0x00000071, 0x00000072, 0x000000fc, 0x00000073,
    0x000000fd, 0x00001ffb, 0x0007fff0, 0x00001ffc, 0x00003ffc, 0x00000022,
    0x00007ffd, 0x00000003, 0x00000023, 0x00000004, 0x00000024, 0x00000005,
    0x00000025, 0x00000026, 0x00000027, 0x00000006, 0x00000074, 0x00000075,
    0x00000028, 0x00000029, 0x0000002a, 0x00000007, 0x0000002b, 0x00000076,
    0x0000002c, 0x00000008, 0x00000009, 0x0000002d, 0x00000077, 0x00000078,
    0x00000079, 0x0000007a, 0x0000007b, 0x00007ffe, 0x000007fc, 0x00003ffd,
    0x00001ffd, 0x0ffffffc, 0x000fffe6, 0x003fffd2, 0x000fffe7, 0x000fffe8,
    0x003fffd3, 0x003fffd4, 0x003fffd5, 0x007fffd9, 0x003fffd6, 0x007fffda,
    0x007fffdb, 0x007fffdc, 0x007fffdd, 0x007fffde, 0x00ffffeb, 0x007fffdf,
    0x00ffffec, 0x00ffffed, 0x003fffd7, 0x007fffe0, 0x00ffffee, 0x007fffe1,
    0x007fffe2, 0x007fffe3, 0x007fffe4, 0x001fffdc, 0x003fffd8, 0x007fffe5,
    0x003fffd9, 0x007fffe6, 0x007fffe7, 0x00ffffef, 0x003fffda, 0x001fffdd,
    0x000fffe9, 0x003fffdb, 0x003fffdc, 0x007fffe8, 0x007fffe9, 0x001fffde,
    0x007fffea, 0x003fffdd, 0x003fffde, 0x00fffff0, 0x001fffdf, 0x003fffdf,
    0x007fffeb, 0x007fffec, 0x001fffe0, 0x001fffe1, 0x003fffe0, 0x001fffe2,
    0x007fffed, 0x003fffe1, 0x007fffee, 0x007fffef, 0x000fffea, 0x003fffe2,
    0x003fffe3, 0x003fffe4, 0x007ffff0, 0x003fffe5, 0x003fffe6, 0x007ffff1,
    0x03ffffe0, 0x03ffffe1, 0x000fffeb, 0x0007fff1, 0x003fffe7, 0x007ffff2,
    0x003fffe8, 0x01ffffec, 0x03ffffe2, 0x03ffffe3, 0x03ffffe4, 0x07ffffde,
    0x07ffffdf, 0x03ffffe5, 0x00fffff1, 0x01ffffed, 0x0007fff2, 0x001fffe3,
    0x03ffffe6, 0x07ffffe0, 0x07ffffe1, 0x03ffffe7, 0x07ffffe2, 0x00fffff2,
    0x001fffe4, 0x001fffe5, 0x03ffffe8, 0x03ffffe9, 0x0ffffffd, 0x07ffffe3,
    0x07ffffe4, 0x07ffffe5, 0x000fffec, 0x00fffff3, 0x000fffed, 0x001fffe6,
    0x003fffe9, 0x001fffe7, 0x001fffe8, 0x007ffff3, 0x003fffea, 0x003fffeb,
    0x01ffffee, 0x01ffffef, 0x00fffff4, 0x00fffff5, 0x03ffffea, 0x007ffff4,
    0x03ffffeb, 0x07ffffe6, 0x03ffffec, 0x03ffffed, 0x07ffffe7, 0x07ffffe8,
    0x07ffffe9, 0x07ffffea, 0x07ffffeb, 0x0ffffffe, 0x07ffffec, 0x07ffffed,
    0x07ffffee, 0x07ffffef, 0x07fffff0, 0x03ffffee, 0x3fffffff,
)
_HUFF_LENS = bytes.fromhex(
    "0d171c1c1c1c1c1c1c181e1c1c1e1c1c1c1c1c1c1c1c1e1c1c1c1c1c1c1c1c1c060a0a0c"
    "0d06080b0a0a080b080606060505050606060606060607080f060c0a0d06070707070707"
    "070707070707070707070707070707070807080d130d0e060f0506050605060606050707"
    "0606060506070605050607070707070f0b0e0d1c14161414161616171617171717171817"
    "181816171817171717151617161717181615141616171715171616181516171715151615"
    "1716171714161616171616171a1a1413161716191a1a1a1b1b1a181913151a1b1b1a1b18"
    "15151a1a1c1b1b1b14181415161515171616191918181a171a1b1a1a1b1b1b1b1b1c1b1b"
    "1b1b1b1a1e")

# (bit length, code) -> symbol. A prefix code, so the pair is unique and a
# bit-at-a-time walk terminates the moment it matches.
_HUFF_LOOKUP = {(ln, code): sym for sym, (code, ln)
                in enumerate(zip(_HUFF_CODES, _HUFF_LENS))}
_HUFF_MAXBITS = 30
_HUFF_EOS = 256


def huff_decode(data: bytes) -> bytes:
    out = bytearray()
    code = 0
    nbits = 0
    for byte in data:
        for shift in (7, 6, 5, 4, 3, 2, 1, 0):
            code = (code << 1) | ((byte >> shift) & 1)
            nbits += 1
            sym = _HUFF_LOOKUP.get((nbits, code))
            if sym is None:
                if nbits > _HUFF_MAXBITS:
                    raise ValueError("huffman: no code matches")
                continue
            if sym == _HUFF_EOS:
                raise ValueError("huffman: EOS inside a string")
            out.append(sym)
            code, nbits = 0, 0
    # What is left must be the padding: fewer than 8 bits, all ones. Anything
    # else is a truncated string, and accepting it would hand the caller a
    # header value that is quietly missing its tail.
    if nbits > 7 or (nbits and code != (1 << nbits) - 1):
        raise ValueError("huffman: bad padding")
    return bytes(out)


def _hp_int(value: int, prefix: int, first: int = 0) -> bytes:
    mask = (1 << prefix) - 1
    if value < mask:
        return bytes([first | value])
    out = bytearray([first | mask])
    value -= mask
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def hpack_literal(name: str, value: str) -> bytes:
    """One header as a literal NEVER INDEXED, with no Huffman coding.

    The whole point: a request header block that carries no reference to any
    table means this side has no encoder state at all, so nothing here can drift
    out of step with the server's decoder. It costs bytes on a connection that
    sends a handful of small headers per call, which is a trade worth making.
    """
    n = name.encode("utf-8")
    v = value.encode("utf-8")
    return (_hp_int(0, 4, 0x10) + _hp_int(len(n), 7) + n
            + _hp_int(len(v), 7) + v)


class HpackDecoder:
    """RFC 7541 decoder: static table, dynamic table with eviction, Huffman."""

    def __init__(self, max_size: int = 4096):
        self.dyn: collections.deque = collections.deque()
        self.max_size = max_size
        self.size = 0

    def _add(self, name: str, value: str):
        self.dyn.appendleft((name, value))
        self.size += len(name) + len(value) + 32
        self._evict()

    def _evict(self):
        while self.size > self.max_size and self.dyn:
            n, v = self.dyn.pop()
            self.size -= len(n) + len(v) + 32

    def _entry(self, idx: int):
        if idx <= 0:
            raise ValueError("hpack index 0 is not an entry")
        if idx <= len(HPACK_STATIC):
            return HPACK_STATIC[idx - 1]
        j = idx - len(HPACK_STATIC) - 1
        if j >= len(self.dyn):
            raise ValueError("hpack index %d past the dynamic table" % idx)
        return self.dyn[j]

    @staticmethod
    def _int(data: bytes, i: int, prefix: int):
        mask = (1 << prefix) - 1
        v = data[i] & mask
        i += 1
        if v < mask:
            return v, i
        m = 0
        while True:
            b = data[i]
            i += 1
            v += (b & 0x7F) << m
            m += 7
            if not b & 0x80:
                return v, i

    def _string(self, data: bytes, i: int):
        huff = bool(data[i] & 0x80)
        ln, i = self._int(data, i, 7)
        raw = bytes(data[i:i + ln])
        i += ln
        if len(raw) != ln:
            raise ValueError("hpack: truncated string")
        s = huff_decode(raw) if huff else raw
        return s.decode("utf-8", "replace"), i

    def decode(self, data: bytes) -> list:
        out = []
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b & 0x80:                       # indexed header field
                idx, i = self._int(data, i, 7)
                out.append(self._entry(idx))
            elif b & 0x40:                     # literal, incremental indexing
                idx, i = self._int(data, i, 6)
                if idx:
                    name = self._entry(idx)[0]
                else:
                    name, i = self._string(data, i)
                value, i = self._string(data, i)
                self._add(name, value)
                out.append((name, value))
            elif b & 0x20:                     # dynamic table size update
                new_max, i = self._int(data, i, 5)
                self.max_size = new_max
                self._evict()
            else:                              # literal, without/never indexed
                idx, i = self._int(data, i, 4)
                if idx:
                    name = self._entry(idx)[0]
                else:
                    name, i = self._string(data, i)
                value, i = self._string(data, i)
                out.append((name, value))
        return out


# =====================================================================
# Layer 3: HTTP/2 with prior knowledge (h2c)
# =====================================================================

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_RST = 0x3
FRAME_SETTINGS = 0x4
FRAME_PING = 0x6
FRAME_GOAWAY = 0x7
FRAME_WINDOW_UPDATE = 0x8
FRAME_CONTINUATION = 0x9

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

SETTING_ENABLE_PUSH = 0x2
SETTING_INITIAL_WINDOW = 0x4
SETTING_MAX_FRAME = 0x5

# This side's receive windows. Large, and topped up as data is consumed: a
# server-streaming call that outlives its 65 KB default window simply stops,
# with no error anywhere, and looks exactly like a venue that went quiet.
STREAM_WINDOW = 1 << 20
MAX_WINDOW = (1 << 31) - 1
WINDOW_REFILL = 1 << 16


class WireError(Exception):
    """The connection, not the venue, failed. Always worth one retry."""


class GrpcError(Exception):
    """A call that ended with a non-zero grpc-status.

    The status is in the TRAILERS, not in :status. A venue rejection arrives as
    HTTP 200 with `grpc-status: 9`, and an adapter that reads the HTTP status
    reports every rejected order as a success.
    """

    def __init__(self, status: int, message: str):
        super().__init__("grpc-status %d: %s" % (status, message))
        self.status = status
        self.message = message


class _H2Stream:
    __slots__ = ("id", "cv", "headers", "trailers", "buf", "done", "error",
                 "consumed")

    def __init__(self, sid: int):
        self.id = sid
        self.cv = threading.Condition()
        self.headers = None
        self.trailers = None
        self.buf = bytearray()
        self.done = False
        self.error = None
        self.consumed = 0


class H2Connection:
    """One h2c connection, multiplexing unary calls and server streams.

    A single reader thread owns the socket and fans frames out to per-stream
    buffers; every caller thread waits on its own stream's condition. That is
    the whole reason HTTP/2 is worth implementing rather than opening a socket
    per call: the event stream, the quote stream and a `PlaceOrder` share one
    TCP connection and cannot head-of-line block each other.
    """

    def __init__(self, host: str, port: int, authority: str):
        self.authority = authority
        self.sock = socket.create_connection((host, port), timeout=15)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._wlock = threading.Lock()
        self._streams: dict = {}
        self._next_id = 1
        self._hpack = HpackDecoder()
        self._conn_consumed = 0
        self._max_frame = 16384
        self._pending_headers = None   # (stream_id, bytearray, end_stream)
        self.dead = None
        self._send_preface()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- writing ------------------------------------------------------
    def _send_preface(self):
        settings = (struct.pack(">HI", SETTING_ENABLE_PUSH, 0)
                    + struct.pack(">HI", SETTING_INITIAL_WINDOW, STREAM_WINDOW))
        with self._wlock:
            self.sock.sendall(PREFACE)
            self.sock.sendall(self._frame_bytes(FRAME_SETTINGS, 0, 0, settings))
            # The CONNECTION window is not covered by SETTINGS_INITIAL_WINDOW_SIZE
            # (that one is per stream), so it starts at 65535 no matter what we
            # ask for and has to be raised explicitly, once, right here.
            self.sock.sendall(self._frame_bytes(
                FRAME_WINDOW_UPDATE, 0, 0,
                struct.pack(">I", MAX_WINDOW - 65535)))

    @staticmethod
    def _frame_bytes(ftype: int, flags: int, sid: int, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload))[1:] + bytes([ftype, flags])
                + struct.pack(">I", sid & 0x7FFFFFFF) + payload)

    def _write(self, ftype: int, flags: int, sid: int, payload: bytes = b""):
        with self._wlock:
            if self.dead:
                raise WireError(self.dead)
            try:
                self.sock.sendall(self._frame_bytes(ftype, flags, sid, payload))
            except OSError as e:
                raise WireError("send failed: %s" % e) from None

    # -- reading ------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise WireError("connection closed by the venue")
            out += chunk
        return bytes(out)

    def _read_loop(self):
        try:
            while True:
                head = self._recv_exact(9)
                length = (head[0] << 16) | (head[1] << 8) | head[2]
                ftype, flags = head[3], head[4]
                sid = struct.unpack(">I", head[5:9])[0] & 0x7FFFFFFF
                payload = self._recv_exact(length) if length else b""
                self._on_frame(ftype, flags, sid, payload)
        except Exception as e:      # noqa: BLE001 - every failure ends the same
            self._fail("%s: %s" % (type(e).__name__, e))

    def _fail(self, reason: str):
        self.dead = reason
        try:
            self.sock.close()
        except OSError:
            pass
        for st in list(self._streams.values()):
            with st.cv:
                if not st.done:
                    st.error = reason
                    st.done = True
                    st.cv.notify_all()

    def _on_frame(self, ftype: int, flags: int, sid: int, payload: bytes):
        if ftype == FRAME_SETTINGS:
            if not flags & FLAG_ACK:
                i = 0
                while i + 6 <= len(payload):
                    ident, val = struct.unpack(">HI", payload[i:i + 6])
                    if ident == SETTING_MAX_FRAME:
                        self._max_frame = val
                    i += 6
                self._write(FRAME_SETTINGS, FLAG_ACK, 0)
            return
        if ftype == FRAME_PING:
            if not flags & FLAG_ACK:
                # Unanswered PINGs are how a grpc server decides a client is
                # gone and drops the connection under a live event stream.
                self._write(FRAME_PING, FLAG_ACK, 0, payload)
            return
        if ftype == FRAME_GOAWAY:
            code = struct.unpack(">I", payload[4:8])[0] if len(payload) >= 8 else 0
            self._fail("GOAWAY from the venue, error %d" % code)
            return
        if ftype == FRAME_WINDOW_UPDATE:
            return              # our requests are far smaller than any window
        if ftype in (FRAME_HEADERS, FRAME_CONTINUATION):
            self._on_headers(ftype, flags, sid, payload)
            return
        if ftype == FRAME_DATA:
            self._on_data(flags, sid, payload)
            return
        if ftype == FRAME_RST:
            code = struct.unpack(">I", payload[:4])[0] if len(payload) >= 4 else 0
            st = self._streams.get(sid)
            if st is not None:
                with st.cv:
                    if not st.done:
                        st.error = "stream reset by the venue, error %d" % code
                    st.done = True
                    st.cv.notify_all()
            return
        # PRIORITY, PUSH_PROMISE and anything unknown: ignored by design. An
        # extension frame type MUST be discarded rather than treated as an
        # error (RFC 7540 4.1), and push is disabled in our SETTINGS anyway.

    def _on_headers(self, ftype: int, flags: int, sid: int, payload: bytes):
        if ftype == FRAME_HEADERS:
            body = payload
            if flags & FLAG_PADDED:
                pad = body[0]
                body = body[1:len(body) - pad]
            if flags & FLAG_PRIORITY:
                body = body[5:]
            self._pending_headers = [sid, bytearray(body),
                                     bool(flags & FLAG_END_STREAM)]
        else:
            if self._pending_headers is None or self._pending_headers[0] != sid:
                raise WireError("CONTINUATION for the wrong stream")
            self._pending_headers[1] += payload
        if not flags & FLAG_END_HEADERS:
            return
        sid, block, end_stream = self._pending_headers
        self._pending_headers = None
        # Decoded HERE, in arrival order, on the single reader thread: HPACK is
        # connection-global and stateful, so decoding a block out of order (or
        # on two threads) corrupts the dynamic table for every later block.
        headers = self._hpack.decode(bytes(block))
        st = self._streams.get(sid)
        if st is None:
            return
        with st.cv:
            if st.headers is None:
                st.headers = headers
            else:
                st.trailers = headers
            if end_stream:
                st.done = True
            st.cv.notify_all()

    def _on_data(self, flags: int, sid: int, payload: bytes):
        body = payload
        if flags & FLAG_PADDED:
            pad = body[0]
            body = body[1:len(body) - pad]
        # Credit is returned for the WHOLE frame including padding, which is
        # what the peer spent.
        self._refill(sid, len(payload))
        st = self._streams.get(sid)
        if st is None:
            return
        with st.cv:
            st.buf += body
            if flags & FLAG_END_STREAM:
                st.done = True
            st.cv.notify_all()

    def _refill(self, sid: int, n: int):
        self._conn_consumed += n
        if self._conn_consumed >= WINDOW_REFILL:
            inc, self._conn_consumed = self._conn_consumed, 0
            try:
                self._write(FRAME_WINDOW_UPDATE, 0, 0, struct.pack(">I", inc))
            except WireError:
                return
        st = self._streams.get(sid)
        if st is None:
            return
        st.consumed += n
        if st.consumed >= WINDOW_REFILL and not st.done:
            inc, st.consumed = st.consumed, 0
            try:
                self._write(FRAME_WINDOW_UPDATE, 0, sid, struct.pack(">I", inc))
            except WireError:
                pass

    # -- calls --------------------------------------------------------
    def start(self, path: str, message: bytes, metadata, timeout=None):
        hdrs = [(":method", "POST"), (":scheme", "http"), (":path", path),
                (":authority", self.authority),
                ("te", "trailers"),
                ("content-type", "application/grpc+proto"),
                ("user-agent", "brue-connect-sablepoint/1.0")]
        if timeout:
            hdrs.append(("grpc-timeout", "%dS" % int(timeout)))
        hdrs.extend(metadata or ())
        block = b"".join(hpack_literal(n, v) for n, v in hdrs)
        frame = b"\x00" + struct.pack(">I", len(message)) + message
        with self._wlock:
            if self.dead:
                raise WireError(self.dead)
            sid = self._next_id
            self._next_id += 2
            st = _H2Stream(sid)
            self._streams[sid] = st
            try:
                # Stream ids must be OPENED in increasing order, so the id is
                # taken and its HEADERS written under one lock; allocating
                # outside it lets two threads open 3 before 1, which the server
                # answers with PROTOCOL_ERROR.
                self.sock.sendall(self._frame_bytes(
                    FRAME_HEADERS, FLAG_END_HEADERS, sid, block))
                self.sock.sendall(self._frame_bytes(
                    FRAME_DATA, FLAG_END_STREAM, sid, frame))
            except OSError as e:
                self._streams.pop(sid, None)
                raise WireError("send failed: %s" % e) from None
        return st

    def cancel(self, st: _H2Stream):
        try:
            self._write(FRAME_RST, 0, st.id, struct.pack(">I", 8))  # CANCEL
        except WireError:
            pass
        self.release(st)

    def release(self, st: _H2Stream):
        self._streams.pop(st.id, None)

    def close(self):
        self.dead = self.dead or "closed locally"
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _grpc_status(st: _H2Stream):
    """(status, message) from the trailers, falling back to the headers.

    A call refused before any message rides back as `Trailers-Only`: ONE
    HEADERS frame carrying :status, grpc-status and grpc-message with
    END_STREAM set and no trailers at all. Reading only the trailers there
    turns every authentication failure into a silent empty reply.
    """
    fields = dict(st.trailers or []) or dict(st.headers or [])
    status = fields.get("grpc-status")
    msg = fields.get("grpc-message", "")
    if msg:
        # grpc-message is percent-encoded on the wire (ASCII only).
        msg = re.sub(r"%([0-9A-Fa-f]{2})",
                     lambda m: chr(int(m.group(1), 16)), msg)
    if status is None:
        http = dict(st.headers or []).get(":status")
        if http and http != "200":
            return 2, "HTTP %s from the venue" % http
        return None, msg
    try:
        return int(status), msg
    except ValueError:
        return 2, msg


def _read_message(st: _H2Stream, deadline: float):
    """One gRPC message off a stream, or None once the stream has ended."""
    with st.cv:
        while True:
            if len(st.buf) >= 5:
                ln = struct.unpack(">I", bytes(st.buf[1:5]))[0]
                if len(st.buf) >= 5 + ln:
                    compressed = st.buf[0]
                    msg = bytes(st.buf[5:5 + ln])
                    del st.buf[:5 + ln]
                    if compressed:
                        raise WireError("compressed message, but no "
                                        "grpc-encoding was negotiated")
                    return msg
            if st.error:
                raise WireError(st.error)
            if st.done:
                return None
            remaining = deadline - time.time()
            if remaining <= 0:
                raise WireError("timed out waiting for the venue")
            st.cv.wait(min(remaining, 0.5))


# =====================================================================
# Layer 4: the Sable Point client
# =====================================================================

# grpc status code -> (SPEC error code, retryable) for the statuses this venue
# documents. The venue's OWN code travels in the details string, so the pair is
# what identifies a rejection; see ERROR_MAP.
GRPC_UNAVAILABLE = 14

# (grpc status, venue detail) -> SPEC section 6 code + retryable.
ERROR_MAP = {
    (3, "INVALID_QTY"): ("invalid_qty", False),
    (3, "INVALID_PARAMS"): ("x_invalid_params", False),
    (3, "INVALID_EXPIRY"): ("invalid_expiry", False),
    (9, "MARKET_CLOSED"): ("market_closed", False),
    (9, "INSUFFICIENT_MARGIN"): ("insufficient_margin", False),
    # The instrument reached a listed expiry: the market is not shut for the
    # day, this contract has stopped dealing entirely. `trade_restricted` is
    # the SPEC code for "the clock is fine, this account may not do this to
    # this instrument", which is exactly the condition.
    (9, "INSTRUMENT_DISABLED"): ("trade_restricted", False),
    (5, "UNKNOWN_SYMBOL"): ("unknown_symbol", False),
    (5, "UNKNOWN_ORDER"): ("not_found", False),
    (5, "UNKNOWN_POSITION"): ("not_found", False),
    (10, "ORDER_NOT_WORKING"): ("not_resting", False),
    (16, "BAD_TOKEN"): ("unauthorized", False),
    (13, "INTERNAL"): ("internal", True),
}


def to_adapter_error(e: GrpcError) -> AdapterError:
    hit = ERROR_MAP.get((e.status, e.message.strip().upper()))
    if hit is None:
        if e.status == GRPC_UNAVAILABLE:
            return AdapterError("internal", "venue unavailable: %s" % e.message,
                                True)
        # An unmapped venue code keeps its identity as an x_ extension instead
        # of being flattened to `internal`, which would throw away the only
        # thing the platform could have acted on (SPEC 6).
        slug = re.sub(r"[^a-z0-9_]+", "_", e.message.lower()).strip("_")
        return AdapterError("x_sablepoint_" + (slug or "error"), str(e), False)
    code, retry = hit
    return AdapterError(code, "%s: %s" % (e.message, code), retry)


class SablePointClient:
    """The venue, as calls. Owns the connections, the token and the retries.

    Two connections, not one: unary RPCs on their own, the two long-lived
    server streams on another. They multiplex perfectly well together, but a
    stream that wedges (or the reconnect that follows) then cannot delay an
    order going out, and a `PlaceOrder` is the one call where latency is a
    trading cost rather than an inconvenience.
    """

    def __init__(self, addr: str, state_dir: str):
        host, _, port = addr.rpartition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 7671)
        self.authority = "%s:%d" % (self.host, self.port)
        self.state_dir = state_dir
        self.token = None
        self.account_id = None
        self._conns: dict = {"rpc": None, "stream": None}
        self._lock = threading.Lock()

    # -- credentials --------------------------------------------------
    def _token_path(self) -> str:
        # token.json, NOT account.json: account.json is the registry's pin
        # file (the platform's explicit account choice), so a token store
        # under that name would leak the account_id into the pin and could
        # be clobbered by a platform account pick, losing the token and
        # therefore the account. An older account.json token store is still
        # read below so no existing account is orphaned.
        return os.path.join(self.state_dir, "token.json")

    def _legacy_token_path(self) -> str:
        return os.path.join(self.state_dir, "account.json")

    def load_token(self):
        """The token IS the account (SPEC 8.5: it never leaves this process).

        Persisted because opening a fresh one is not free: `OpenAccount` mints a
        brand new, empty account every time it is called, so registering on
        every start would orphan yesterday's positions and make a reconnect look
        like a flat book.
        """
        for path in (self._token_path(), self._legacy_token_path()):
            try:
                with open(path) as fh:
                    data = json.load(fh)
                if data.get("api_token") and data.get("addr") == self.authority:
                    self.token = data["api_token"]
                    self.account_id = data.get("account_id")
                    return True
            except (OSError, ValueError):
                continue
        return False

    def store_token(self):
        path = self._token_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"api_token": self.token,
                           "account_id": self.account_id,
                           "addr": self.authority}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            log("could not persist the account token to %s: %s" % (path, e))
        # Remove an older account.json token copy so its account_id cannot
        # read as a pin; a platform choice file (marked "chosen") is not ours
        # to delete.
        try:
            with open(self._legacy_token_path()) as fh:
                legacy = json.load(fh)
            if isinstance(legacy, dict) and legacy.get("api_token") \
                    and not legacy.get("chosen"):
                os.remove(self._legacy_token_path())
        except (OSError, ValueError):
            pass

    def open_account(self):
        body = e_str(1, DISPLAY_NAME)
        reply = pb_parse(self.call("OpenAccount", body, auth=False))
        self.account_id = f_str(reply, 1)
        self.token = f_str(reply, 2)
        if not self.token:
            raise AdapterError("unauthorized",
                               "the venue issued no api_token")
        self.store_token()
        log("opened account %s at %s" % (self.account_id, self.authority))

    def ensure_account(self):
        if self.token and self.account_id:
            return
        if self.load_token():
            try:
                self.call("GetAccount", b"")
                return
            except GrpcError as e:
                if e.status != 16:
                    raise
                # The venue keeps issued tokens in memory only, so its own
                # restart invalidates ours while the account itself survives in
                # its database, unreachable. Nothing to recover: say so, and
                # open a fresh account rather than fail every call forever.
                log("the stored token is no longer accepted (the venue was "
                    "restarted); opening a new account")
                self.token = self.account_id = None
        self.open_account()

    # -- connections --------------------------------------------------
    def _conn(self, kind: str) -> H2Connection:
        with self._lock:
            c = self._conns.get(kind)
            if c is not None and not c.dead:
                return c
            c = H2Connection(self.host, self.port, self.authority)
            self._conns[kind] = c
            return c

    def _drop(self, kind: str, conn):
        with self._lock:
            if self._conns.get(kind) is conn:
                self._conns[kind] = None
        try:
            conn.close()
        except Exception:       # noqa: BLE001 - closing is best effort
            pass

    def close(self):
        for kind in list(self._conns):
            c = self._conns.get(kind)
            if c is not None:
                self._drop(kind, c)

    # -- unary --------------------------------------------------------
    def call(self, method: str, body: bytes, auth: bool = True,
             timeout: float = 8.0, retries: int = 1) -> bytes:
        meta = [("x-api-token", self.token)] if (auth and self.token) else []
        last = None
        for attempt in range(retries + 1):
            conn = None
            try:
                conn = self._conn("rpc")
                st = conn.start(SERVICE + method, body, meta, timeout=timeout)
                deadline = time.time() + timeout
                try:
                    messages = []
                    while True:
                        m = _read_message(st, deadline)
                        if m is None:
                            break
                        messages.append(m)
                finally:
                    conn.release(st)
                status, msg = _grpc_status(st)
                if status:
                    raise GrpcError(status, msg)
                return messages[0] if messages else b""
            except WireError as e:
                last = e
                if conn is not None:
                    self._drop("rpc", conn)
                if attempt >= retries:
                    break
                time.sleep(0.25)
        raise AdapterError("internal", "venue unreachable: %s" % last, True)

    # -- server streaming ---------------------------------------------
    def open_stream(self, method: str, body: bytes):
        conn = self._conn("stream")
        st = conn.start(SERVICE + method, body,
                        [("x-api-token", self.token)] if self.token else [])
        return conn, st


# =====================================================================
# The adapter
# =====================================================================

ASSET_CLASS = {0: "fx", 1: "metal", 2: "index", 3: "crypto", 4: "equity",
               5: "future"}

TF_TO_WIRE = {"1m": 0, "5m": 1, "15m": 2, "30m": 3, "1h": 4, "4h": 5, "1d": 6}
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

SIDE_TO_WIRE = {"buy": 0, "sell": 1}
SIDE_FROM_WIRE = {0: "buy", 1: "sell"}
OTYPE_TO_WIRE = {"market": 0, "limit": 1, "stop": 2, "stop_limit": 3}
TIF_TO_WIRE = {"gtc": 0, "day": 1, "gtd": 2}
TIF_FROM_WIRE = {0: "gtc", 1: "day", 2: "gtd"}

ORDER_STATE = {0: "resting", 1: "resting", 2: "resting", 3: "filled",
               4: "cancelled", 5: "expired", 6: "cancelled"}

EV_ORDER_UPDATE, EV_FILL = 0, 1
EV_POS_OPENED, EV_POS_UPDATED, EV_POS_CLOSED = 2, 3, 4
EV_ACCOUNT, EV_STOP_OUT = 5, 6

# Sessions, translated from the venue's own free-text `session_hours`. The venue
# publishes an ENGLISH SENTENCE, which is not a calendar a platform can gate on,
# so the three sentences it uses are recognised by their wording (not by asset
# class, so a new instrument with either wording is covered) and anything else
# publishes NO calendar. SPEC 3 is emphatic that an empty list means "no
# calendar published" and NOT "closed", which is also why 24/7 is written out as
# an explicit seven-day window rather than left empty: always-open and unknown
# must not look the same to a platform deciding whether to send an order.
SESSIONS_24_7 = [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                  "close": "24:00", "tz": "UTC"}]
SESSIONS_24_5 = [
    {"days": [7], "open": "21:05", "close": "24:00", "tz": "UTC"},
    {"days": [1, 2, 3, 4], "open": "00:00", "close": "24:00", "tz": "UTC"},
    {"days": [5], "open": "00:00", "close": "21:00", "tz": "UTC"},
]
_WEEKDAY_WINDOW = re.compile(
    r"monday\s*-\s*friday\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")

# How long a close waits for the rest of its own story (the companion FILL, the
# ACCOUNT_UPDATED that carries the realised figure, the STOP_OUT) before it is
# reported with what is known. Long enough that the venue's own follow-up events
# have landed, short enough that a platform is not left holding a position it no
# longer has.
CLOSE_GRACE = 1.2

# SPEC 8.4: a market order that has not terminated within this many seconds is
# announced as a degraded session rather than silently waited on.
ORDER_DEADLINE = 30.0


class SablePointAdapter(Adapter):
    name = "sablepoint"
    version = "1.0"
    broker = "Sable Point Capital"
    # Truthful (SPEC 8.2): the venue's own reference opens with "Sable Point
    # Capital is a simulated venue for integration testing. All prices are
    # synthetic. No real money is involved."
    mode = "paper"
    # Every account this venue opens is USD (documented, and re-checked against
    # the account in on_first_call).
    account_currency = "USD"
    # "The account model is netting: an account holds at most one open position
    # per symbol."
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        state = os.environ.get("BRUE_BROKER_STATE") or os.path.expanduser(
            "~/.brue-connect/state/sablepoint")
        self.api = SablePointClient(ADDR, state)
        self._lk = threading.RLock()
        self._instruments: dict = {}       # symbol -> venue Instrument fields
        self._orders: dict = {}            # order_id -> our view
        self._fills_seen: set = set()      # venue fill_id, at-least-once guard
        self._pos: dict = {}               # position_id -> last known position
        self._pos_client: dict = {}        # position_id -> opening client_id
        self._pos_comm: dict = {}          # position_id -> commission charged
        self._pos_from: dict = {}          # position_id -> opening order type
        self._closing: dict = {}           # position_id -> close job filed here
        self._close_jobs: list = []        # closes awaiting resolution
        self._pending_realize: list = []   # jobs awaiting the next balance
        self._flatten_until = 0.0
        self._flatten_ids: set = set()
        self._stop_out_until = 0.0
        self._cancelled_by_us: set = set()
        self._balance = None
        self._quote_syms: list = []
        self._bars: dict = {}              # (symbol, tf) -> last emitted ms
        self._md_gen = 0
        self._md_stream = None
        self._ev_stream = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._session_state = None
        self._last_order_poll = 0.0

    # == handshake =====================================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # stop_loss / take_profit ride on PlaceOrder and are enforced
                # venue-side, not policed by the client.
                "brackets": True,
                "modify": True,             # ModifyProtection
                "trailing": True,           # trail_points, maintained by the venue
                "partial_close": True,      # ClosePosition takes a qty
                "pending": True,
                "pending_types": ["limit", "stop", "stop_limit"],
                "expiry": ["gtc", "day", "gtd"],
                # The venue checks free margin when the order is submitted but
                # holds nothing against a resting one: margin_used is computed
                # from OPEN POSITIONS only, so an order accepted comfortably can
                # still be refused the margin at the moment it triggers. That is
                # exactly the consequence the SPEC's flag exists to warn about.
                "reserves_margin": False,
                # No oco_group anywhere in the contract, and emulating it by
                # racing to cancel the sibling loses that race on a fast move
                # (SPEC 4). Absent means absent.
                "oco": False,
                # position.close_by is meaningless on a netting book: the venue
                # never lets two opposing positions exist in one symbol.
                "close_by": False,
                "can_list": True,           # ListOrders(scope=ALL)
            },
            "data": {
                "quotes": True, "bars": True, "history": True,
                # No deposits, withdrawals, dividends, financing or dealer
                # adjustments exist on this venue: the only thing that moves the
                # balance is a fill or a realised close. There is no ledger of
                # non-trade operations to list, so saying false is the truth
                # rather than a missing feature.
                "operations": False,
                # The venue states no cap on StreamMarketData, and the whole
                # catalog is seventeen symbols. Absent means "the broker states
                # no limit" (SPEC 3), which is exactly the case.
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # "Sable Point Capital ... does not throttle requests: there is no
        # RESOURCE_EXHAUSTED code". Its own advice is to build as though it
        # might, and not to poll faster than about once a second, so a modest
        # figure is published rather than an invitation to hammer it.
        return {"requests_per_second": 20}

    def broker_info(self) -> dict:
        # The venue's own words. It publishes no country, licence or website, so
        # none is invented here (SPEC 3.1 is explicit that this field is not for
        # that).
        return {"display_name": "Sable Point Capital",
                "tagline": "Multi-asset prime brokerage, gRPC only",
                "mark": "SP",
                "asset_classes": ["FX", "Metals", "Indices", "Crypto",
                                  "US Equities", "Special Futures"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the venue something happens here, not in
        `hello` (SPEC 3.1): the account, the catalog, the two streams."""
        self.api.ensure_account()
        self._load_catalog()
        acct = self._account_raw()
        ccy = f_str(acct, 2)
        if ccy and ccy != self.account_currency:
            # `hello` has already gone out and cannot be corrected; loud on
            # stderr is the only honest move left.
            log("WARNING: the account is denominated in %r but the handshake "
                "declared %r" % (ccy, self.account_currency))
        self._balance = f_dbl(acct, 3)
        self._stop.clear()
        self._spawn(self._events_loop)
        self._spawn(self._poll_loop)
        self._sync_positions()
        self._sync_orders()
        self._spawn(self._seed_fills)

    @staticmethod
    def _spawn(fn):
        threading.Thread(target=fn, daemon=True).start()

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        self.api.close()

    # == catalog =======================================================
    def _load_catalog(self):
        cat = pb_parse(self.api.call("GetCatalog", b""))
        insts = {}
        for row in f_rep(cat, 1):
            sym = f_str(row, 1)
            if sym:
                insts[sym] = row
        if not insts:
            raise AdapterError("internal", "the venue returned an empty catalog")
        with self._lk:
            self._instruments = insts

    @staticmethod
    def _sessions_for(text: str) -> list:
        t = (text or "").lower()
        if "24/7" in t or "no scheduled closures" in t:
            return [dict(s) for s in SESSIONS_24_7]
        if "24/5" in t or ("sunday" in t and "friday" in t):
            return [dict(s) for s in SESSIONS_24_5]
        m = _WEEKDAY_WINDOW.search(t)
        if m:
            return [{"days": [1, 2, 3, 4, 5], "open": m.group(1),
                     "close": m.group(2), "tz": "UTC"}]
        log("unrecognised session_hours %r; publishing no calendar for it"
            % (text,))
        return []

    def _spec(self, row: dict) -> dict:
        symbol = f_str(row, 1)
        digits = f_int(row, 5)
        tick = f_dbl(row, 6)
        contract = f_dbl(row, 10)
        quote_ccy = f_str(row, 4)
        klass = ASSET_CLASS.get(f_int(row, 3), "cfd")
        mode = (f_str(row, 12) or "none").lower()
        rate = f_dbl(row, 13)
        minimum = f_dbl(row, 14)
        fees = {"commission_per_lot": rate if mode == "per_lot" else 0.0,
                "mode": mode if mode in ("none", "per_lot", "per_share",
                                         "pct_notional") else "none",
                "rate": rate, "min": minimum}
        base = None
        if klass in ("fx", "metal") and len(symbol) == 6:
            base = symbol[:3]
        margin = f_dbl(row, 11)
        return {
            "symbol": symbol,
            "name": f_str(row, 2) or symbol,
            "asset_class": klass,
            "base_ccy": base,
            "quote_ccy": quote_ccy,
            "digits": digits,
            "tick_size": tick,
            # The SPEC's own derivation, and right for every product here: they
            # are all quoted in their own contract currency.
            "tick_value": round(contract * tick, 12),
            "lot_step": f_dbl(row, 7),
            "min_qty": f_dbl(row, 8),
            "max_qty": f_dbl(row, 9),
            "min_notional": 0,
            "contract_size": contract,
            "margin_rate": margin,
            # The venue force-closes at equity/margin_used <= 0.50, so the level
            # a position is actually maintained at is half the initial
            # requirement. Stated rather than left to default to the initial
            # rate, which would have a platform show a stop-out as impossible
            # right up to the moment it happens.
            "margin_maintenance_rate": round(margin * 0.5, 10),
            "margin_tiers": None,
            "sessions": self._sessions_for(f_str(row, 15)),
            "holidays": [],          # the venue publishes no holiday calendar
            "early_closes": [],
            "tradable": bool(f_int(row, 16)),
            "shortable": True,       # every instrument deals both ways
            "trade_mode": "full" if f_int(row, 16) else "disabled",
            "stops_level_points": 0,     # no minimum stop distance is enforced
            "fees": fees,
            # No financing of any kind: "There is no financing either; holding a
            # position overnight or across a weekend costs nothing beyond the
            # spread". swap_mode "none" says that, where 0 points would have
            # claimed a unit the venue never quoted in.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            # "none of them carry a listed expiry"; the field exists on the
            # venue's side, so this is its answer and not an assumption.
            "expires_at": None, "successor": None, "dividend": None,
        }

    def handle_catalog_list(self, params):
        self._load_catalog()
        with self._lk:
            rows = list(self._instruments.values())
        return {"instruments": [self._spec(r) for r in rows]}

    def _instrument(self, symbol) -> dict:
        with self._lk:
            row = self._instruments.get(symbol)
        if row is None:
            self._load_catalog()
            with self._lk:
                row = self._instruments.get(symbol)
        if row is None:
            raise AdapterError("unknown_symbol", "no such symbol %r" % (symbol,))
        return row

    def _digits(self, symbol) -> int:
        with self._lk:
            row = self._instruments.get(symbol)
        return f_int(row, 5) if row else 5

    # == account =======================================================
    def _account_raw(self) -> dict:
        return pb_parse(self.api.call("GetAccount", b""))

    @staticmethod
    def _account_view(a: dict) -> dict:
        return {"balance": f_dbl(a, 3), "equity": f_dbl(a, 4),
                "margin_used": f_dbl(a, 5), "margin_free": f_dbl(a, 6),
                "currency": f_str(a, 2) or "USD"}

    def handle_account_get(self, params):
        # `_balance` is deliberately NOT refreshed from here. It is the baseline
        # a close's realised P/L is measured against (see `_on_account`), and it
        # must only ever move on the venue's own ordered event stream: a read
        # landing between a close and its account update would move the baseline
        # under the very subtraction that needs it.
        return self._account_view(self._account_raw())

    # == positions =====================================================
    def _position_view(self, p: dict) -> dict:
        pid = str(f_int(p, 1))
        with self._lk:
            client_id = self._pos_client.get(pid)
            comm = self._pos_comm.get(pid, 0.0)
            frm = self._pos_from.get(pid)
        out = {
            "position_id": pid,
            "symbol": f_str(p, 2),
            "side": SIDE_FROM_WIRE.get(f_int(p, 3), "buy"),
            "qty": f_dbl(p, 4),
            "avg_price": f_dbl(p, 5),
            "unrealized_pnl": f_dbl(p, 6),
            "sl": f_dbl(p, 7) or None,
            "tp": f_dbl(p, 8) or None,
            "trail_points": f_dbl(p, 9),
            # The venue charges no financing at all, and commission is charged
            # only on the fills that OPENED or added to the position, which is
            # what has been accumulated here. Both are real figures, not the
            # "we cannot split it" zero.
            "swap": 0.0,
            "commission": -round(comm, 6),
            "opened_at": f_int(p, 10) // 1_000_000,
            "from_order": frm,
        }
        if client_id:
            out["client_id"] = client_id
        return out

    def _positions_raw(self) -> list:
        rows = f_rep(pb_parse(self.api.call("ListPositions", b"")), 1)
        with self._lk:
            self._pos = {str(f_int(p, 1)): p for p in rows}
        return rows

    def handle_positions_list(self, params):
        return {"positions": [self._position_view(p)
                              for p in self._positions_raw()]}

    def _sync_positions(self):
        try:
            self._positions_raw()
        except AdapterError as e:
            log("position sync failed: %s" % e)

    def _seed_fills(self):
        """Rebuild per-position commission from the venue's own ledger.

        A position adopted at connect (opened before this process existed) has
        no fills in this session, so `commission` on it would read 0 and claim
        the holder was never charged. ListFills carries the position id and the
        fee on every deal, so the real figure is recoverable; done off the
        connect path because it is a nicety, not a precondition.
        """
        try:
            fills = f_rep(pb_parse(self.api.call(
                "ListFills", e_varint(2, 500))), 1)
        except AdapterError as e:
            log("could not seed fills: %s" % e)
            return
        with self._lk:
            for fl in fills:
                fid = f_int(fl, 1)
                pid = str(f_int(fl, 3))
                self._fills_seen.add(fid)
                fee = f_dbl(fl, 8)
                if fee and pid in self._pos:
                    self._pos_comm[pid] = self._pos_comm.get(pid, 0.0) + fee

    # == orders ========================================================
    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("x_invalid_params", "client_id is required")
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        otype = (params.get("type") or "market").lower()
        # Everything that decides what gets DEALT. The venue will not refuse a
        # conflicting replay for us: its client_ref rule returns the ORIGINAL
        # order even when the symbol, side, size and price all differ, which is
        # precisely the silent-wrong-order case SPEC 6 exists to stop.
        sig = (symbol, side, format(_num(params.get("qty"), -1), ".10g"), otype,
               params.get("price"), params.get("stop_price"),
               params.get("sl"), params.get("tp"), params.get("trail_points"),
               params.get("expiry"), params.get("expiry_at"))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("x_invalid_params",
                               "side must be buy or sell, got %r"
                               % (params.get("side"),))
        if otype not in OTYPE_TO_WIRE:
            raise AdapterError("unsupported", "unknown order type %r" % (otype,))
        self._instrument(symbol)             # unknown_symbol before the round trip
        qty = _num(params.get("qty"), -1.0)
        if qty <= 0:
            raise AdapterError("invalid_qty",
                               "qty must be positive, got %r"
                               % (params.get("qty"),))
        price = params.get("price")
        stop_price = params.get("stop_price")
        # Checked here so the caller gets a precise answer: the venue answers a
        # missing price and a malformed side with the SAME code (INVALID_PARAMS),
        # so its reply cannot say which was wrong.
        if otype == "limit" and not price:
            raise AdapterError("x_invalid_params", "a limit order needs `price`")
        if otype == "stop" and not stop_price and not price:
            raise AdapterError("x_invalid_params",
                               "a stop order needs `stop_price`")
        if otype == "stop_limit" and not (price and stop_price):
            raise AdapterError("x_invalid_params",
                               "a stop_limit order needs `price` and "
                               "`stop_price`")
        expiry = (params.get("expiry") or "gtc").lower()
        if expiry not in TIF_TO_WIRE:
            raise AdapterError("invalid_expiry", "unknown expiry %r" % (expiry,))
        expires_ns = 0
        if expiry == "gtd":
            when = params.get("expiry_at")
            if not when:
                raise AdapterError("invalid_expiry", "expiry 'gtd' needs "
                                                     "expiry_at")
            expires_ns = int(_num(when)) * 1_000_000

        body = (e_str(1, client_id) + e_str(2, symbol)
                + e_varint(3, SIDE_TO_WIRE[side])
                + e_varint(4, OTYPE_TO_WIRE[otype])
                + e_dbl(5, qty)
                + e_dbl(6, price if otype in ("limit", "stop_limit") else 0)
                # A plain stop carries its trigger in stop_price; the SPEC lets a
                # caller put it in `price` for that type, so either is accepted.
                + e_dbl(7, (stop_price or price)
                        if otype in ("stop", "stop_limit") else 0)
                + e_dbl(8, params.get("sl") or 0)
                + e_dbl(9, params.get("tp") or 0)
                + e_dbl(10, params.get("trail_points") or 0)
                + e_varint(11, TIF_TO_WIRE[expiry])
                + e_varint(12, expires_ns))
        try:
            reply = pb_parse(self.api.call("PlaceOrder", body))
        except GrpcError as e:
            err = to_adapter_error(e)
            # SPEC 8.4: a synchronous refusal is still a terminal outcome, and a
            # platform watching only the event stream must see the order end.
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": err.code, "message": str(err)})
            raise err
        order = f_msg(reply, 1)
        order_id = str(f_int(order, 1))
        with self._lk:
            rec = self._orders.setdefault(order_id, {})
            rec.setdefault("client_id", client_id)
            rec["type"] = otype
            # Only a MARKET order is chased: it must reach a fill or a rejection
            # (SPEC 8.4), while a pending order resting is a legitimate resting
            # place to stop, and polling one until it triggers would spend the
            # venue's budget on an order that is behaving perfectly.
            rec["market"] = otype == "market"
            rec["deadline"] = time.time() + ORDER_DEADLINE if rec["market"] \
                else None
        # Fed through the same state machine as a streamed order, so the reply
        # and the event cannot produce two announcements of the same transition.
        self._absorb_order(order)
        result = {"order_id": order_id,
                  "status": "accepted" if otype == "market" else "resting"}
        self._wake.set()
        return self.idempotent_store(client_id, result, sig)

    def _order_view(self, o: dict) -> dict:
        oid = str(f_int(o, 1))
        wire_type = f_int(o, 5)
        limit_price = f_dbl(o, 8)
        stop_price = f_dbl(o, 9)
        # The venue reports a resting STOP_LIMIT as type=STOP with both prices
        # set, and a plain STOP as type=STOP with only stop_price. Once a
        # STOP_LIMIT triggers it reports type=LIMIT with stop_price 0, which is
        # an honest description of how it now deals; the local record of what
        # was placed is preferred where there is one.
        with self._lk:
            known = dict(self._orders.get(oid) or {})
        if wire_type == 0:
            otype = "market"
        elif wire_type == 1:
            otype = "limit"
        elif wire_type == 3:
            otype = "stop_limit"
        else:
            otype = "stop_limit" if limit_price else "stop"
        state = ORDER_STATE.get(f_int(o, 15), "resting")
        expiry = TIF_FROM_WIRE.get(f_int(o, 13), "gtc")
        avg = f_dbl(o, 16)
        return {
            "order_id": oid,
            "client_id": f_str(o, 2) or known.get("client_id"),
            "symbol": f_str(o, 3),
            "type": otype,
            "side": SIDE_FROM_WIRE.get(f_int(o, 4), "buy"),
            "qty": f_dbl(o, 6),
            "price": limit_price or None,
            "stop_price": stop_price or None,
            "sl": f_dbl(o, 10) or None,
            "tp": f_dbl(o, 11) or None,
            "expiry": expiry,
            "expires_at": (f_int(o, 14) // 1_000_000) or None,
            "state": state,
            "placed_at": f_int(o, 17) // 1_000_000,
            "done_at": (f_int(o, 18) // 1_000_000) or None,
            "fill_price": avg if state == "filled" else None,
            # The venue never tells a caller WHY it killed an order (the wire
            # Order has no reason field), so nothing is claimed here rather than
            # a code being guessed at.
            "reason": None,
        }

    def handle_orders_list(self, params):
        want = (params or {}).get("state")
        # scope ALL: a platform reconciling wants the terminal ones too, and the
        # filtering below is exact where the venue's WORKING scope is not.
        rows = f_rep(pb_parse(self.api.call("ListOrders", e_varint(1, 1))), 1)
        out = [self._order_view(o) for o in rows]
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def _sync_orders(self):
        try:
            rows = f_rep(pb_parse(self.api.call("ListOrders",
                                                e_varint(1, 1))), 1)
        except AdapterError as e:
            log("order sync failed: %s" % e)
            return
        for o in rows:
            self._absorb_order(o, seed=True)

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        with self._lk:
            self._cancelled_by_us.add(order_id)
        try:
            reply = pb_parse(self.api.call(
                "CancelOrder", e_varint(1, int(order_id))))
        except GrpcError as e:
            with self._lk:
                self._cancelled_by_us.discard(order_id)
            raise to_adapter_error(e)
        except ValueError:
            raise AdapterError("not_found",
                               "order_id %r is not a venue ticket" % order_id)
        self._absorb_order(f_msg(reply, 1))
        return {}

    def handle_order_amend(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        for field in ("sl", "tp"):
            if params.get(field) is not None:
                # AmendOrder carries price, stop price, qty and expiry only. The
                # protections belong to the POSITION the order will open, and
                # the venue moves them with ModifyProtection once it exists;
                # refusing loudly beats pretending an amend did something.
                raise AdapterError(
                    "unsupported",
                    "this venue cannot amend %s on a resting order; the "
                    "protection attaches to the position it opens" % field)
        expiry = (params.get("expiry") or "").lower()
        if expiry and expiry != "gtd":
            raise AdapterError("invalid_expiry",
                               "an amend can only move a gtd expiry")
        expires_ns = 0
        if params.get("expiry_at"):
            expires_ns = int(_num(params["expiry_at"])) * 1_000_000
        try:
            body = (e_varint(1, int(order_id))
                    + e_dbl(2, params.get("price") or 0)
                    + e_dbl(3, params.get("stop_price") or 0)
                    + e_dbl(4, params.get("qty") or 0)
                    + e_varint(5, expires_ns))
        except ValueError:
            raise AdapterError("not_found",
                               "order_id %r is not a venue ticket" % order_id)
        try:
            reply = pb_parse(self.api.call("AmendOrder", body))
        except GrpcError as e:
            raise to_adapter_error(e)
        self._absorb_order(f_msg(reply, 1))
        return {}

    def handle_order_modify(self, params):
        """SL / TP / trailing on an OPEN position (ModifyProtection).

        The venue reads 0 as "leave unchanged" on every one of the three, so
        clearing one needs its own flag; a null from the platform means clear
        (SPEC 4), and a missing key means leave alone. Getting that backwards
        silently ignores every request to remove a stop.
        """
        pid = params.get("position_id")
        if pid is None:
            raise AdapterError("not_found", "position_id is required")
        try:
            body = e_varint(1, int(pid))
        except (TypeError, ValueError):
            raise AdapterError("not_found",
                               "position_id %r is not a venue ticket" % (pid,))
        touched = False
        for key, value_field, clear_field in (("sl", 2, 5), ("tp", 3, 6),
                                              ("trail_points", 4, 7)):
            if key not in params:
                continue
            touched = True
            v = params.get(key)
            if v is None or _num(v, 0.0) <= 0:
                body += e_varint(clear_field, 1)
            else:
                body += e_dbl(value_field, _num(v))
        if not touched:
            return {}
        try:
            reply = pb_parse(self.api.call("ModifyProtection", body))
        except GrpcError as e:
            raise to_adapter_error(e)
        self._absorb_position(f_msg(reply, 1))
        return {}

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported",
                           "this venue nets one position per symbol; there is "
                           "no close-by")

    # == closing =======================================================
    def handle_position_close(self, params):
        pid = params.get("position_id")
        if pid is None:
            raise AdapterError("not_found", "position_id is required")
        qty = params.get("qty")
        client_id = params.get("client_id")
        sig = ("close", str(pid), format(_num(qty, 0.0), ".10g"))
        if client_id:
            prior = self.idempotent_check(client_id, sig)
            if prior is not None:
                return prior
        try:
            body = e_varint(1, int(pid)) + e_dbl(2, _num(qty, 0.0))
        except (TypeError, ValueError):
            raise AdapterError("not_found",
                               "position_id %r is not a venue ticket" % (pid,))
        # Filed BEFORE the call goes out. The venue emits POSITION_CLOSED inside
        # the same lock the RPC is still holding, so the event regularly reaches
        # the stream thread before the reply reaches this one; a job filed
        # afterwards would find a close already reported with the wrong reason.
        with self._lk:
            self._closing[str(pid)] = {"reason": "manual", "realized": None,
                                       "at": time.time()}
        try:
            reply = pb_parse(self.api.call("ClosePosition", body))
        except GrpcError as e:
            with self._lk:
                self._closing.pop(str(pid), None)
            raise to_adapter_error(e)
        with self._lk:
            job = self._closing.get(str(pid))
            if job is not None:
                # The venue's own realised figure for THIS close; nothing this
                # adapter computes can beat it.
                job["realized"] = f_dbl(reply, 2)
                job["closed_qty"] = f_dbl(reply, 1)
        self._wake.set()
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, sig)
        return result

    def handle_flatten_all(self, params):
        """Close every position and cancel every resting order.

        SPEC 8.1 wants this to work while other calls are failing, and on this
        venue it is one call that does both, so there is nothing to partially
        fail. The window is opened BEFORE the call for the same reason the close
        job above is: the venue's events arrive while the RPC is still in
        flight, and without it every leg would be reported as a manual close.
        """
        with self._lk:
            # WHICH positions, not just WHEN. A bare time window would call the
            # next unrelated close a flatten leg too, and a close reported with
            # the wrong reason is exactly the confident-and-wrong answer SPEC 5
            # is written against. A position opened after this line cannot be
            # one of the ones this call is about to close.
            self._flatten_ids = set(self._pos)
            self._flatten_until = time.time() + 5.0
        try:
            reply = pb_parse(self.api.call("FlattenAll", b""))
        except GrpcError as e:
            with self._lk:
                self._flatten_until = 0.0
            raise to_adapter_error(e)
        closed = f_int(reply, 1)
        with self._lk:
            # The venue closes inside the call, so its events have already been
            # emitted; this is only slack for the stream thread to drain them.
            self._flatten_until = time.time() + 1.0
        self._wake.set()
        return {"closed": closed}

    # == market data ===================================================
    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._instrument(s)          # unknown_symbol before subscribing
        with self._lk:
            new = [s for s in symbols if s not in self._quote_syms]
            self._quote_syms.extend(new)
        if new:
            self._restart_md()
        return {}

    def handle_quotes_unsubscribe(self, params):
        drop = set(params.get("symbols") or [])
        with self._lk:
            keep = [s for s in self._quote_syms if s not in drop]
            changed = len(keep) != len(self._quote_syms)
            self._quote_syms = keep
        if changed:
            self._restart_md()
        return {}

    def handle_bars_history(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_WIRE:
            raise AdapterError("unsupported", "unknown timeframe %r" % (tf,))
        count = int(_num(params.get("count"), 200))
        body = (e_str(1, symbol) + e_varint(2, TF_TO_WIRE[tf])
                + e_varint(3, int(_num(params.get("from"), 0)) * 1_000_000)
                + e_varint(4, int(_num(params.get("to"), 0)) * 1_000_000)
                + e_varint(5, max(1, min(count, 1000))))
        try:
            reply = pb_parse(self.api.call("GetCandles", body, timeout=20.0))
        except GrpcError as e:
            raise to_adapter_error(e)
        bars = [[f_int(c, 1) // 1_000_000, f_dbl(c, 2), f_dbl(c, 3),
                 f_dbl(c, 4), f_dbl(c, 5), f_dbl(c, 6)]
                for c in f_rep(reply, 1)]
        bars.sort(key=lambda b: b[0])        # oldest first (SPEC)
        return {"bars": bars}

    def handle_bars_subscribe(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_WIRE:
            raise AdapterError("unsupported", "unknown timeframe %r" % (tf,))
        with self._lk:
            self._bars.setdefault((symbol, tf), 0)
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._lk:
            self._bars.pop((params.get("symbol"),
                            params.get("timeframe") or "1m"), None)
        return {}

    # == history =======================================================
    def handle_history_fills(self, params):
        since_ms = int(_num((params or {}).get("from"), 0))
        to_ms = int(_num((params or {}).get("to"), 0))
        body = e_varint(1, since_ms * 1_000_000) + e_varint(2, 500)
        try:
            reply = pb_parse(self.api.call("ListFills", body))
        except GrpcError as e:
            raise to_adapter_error(e)
        fills = []
        for fl in f_rep(reply, 1):
            t = f_int(fl, 9) // 1_000_000
            if to_ms and t > to_ms:
                continue
            fills.append({
                "fill_id": str(f_int(fl, 1)),
                "order_id": str(f_int(fl, 2)) if f_int(fl, 2) else None,
                "position_id": str(f_int(fl, 3)),
                "symbol": f_str(fl, 4),
                "side": SIDE_FROM_WIRE.get(f_int(fl, 5), "buy"),
                "qty": f_dbl(fl, 6),
                "price": f_dbl(fl, 7),
                "fees": -abs(f_dbl(fl, 8)),
                "time": t,
            })
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == the event stream ==============================================
    def _events_loop(self):
        """StreamEvents, held open for the life of the adapter.

        "There is no history replay: a call only sees events from the moment it
        opens", so every reconnect re-reads orders and positions over unary
        calls rather than hoping the gap was empty.
        """
        first = True
        while not self._stop.is_set():
            conn = st = None
            try:
                conn, st = self.api.open_stream("StreamEvents", b"")
                self._ev_stream = (conn, st)
                self._set_session("connected", "event stream open")
                if not first:
                    self._sync_positions()
                    self._sync_orders()
                first = False
                while not self._stop.is_set():
                    msg = _read_message(st, time.time() + 3600)
                    if msg is None:
                        raise WireError("the venue ended the event stream")
                    self._on_trade_event(pb_parse(msg))
            except Exception as e:      # noqa: BLE001 - anything reconnects
                if self._stop.is_set():
                    return
                self._set_session("degraded",
                                  "event stream: %s: %s" % (type(e).__name__, e))
                if conn is not None:
                    self.api._drop("stream", conn)
                time.sleep(1.0)
            finally:
                self._ev_stream = None

    def _on_trade_event(self, ev: dict):
        etype = f_int(ev, 1)
        if etype == EV_ORDER_UPDATE:
            self._absorb_order(f_msg(ev, 2))
        elif etype == EV_FILL:
            order = f_msg(ev, 2)
            self._absorb_order(order)
            self._absorb_fill(order, f_msg(ev, 3))
        elif etype in (EV_POS_OPENED, EV_POS_UPDATED):
            self._absorb_position(f_msg(ev, 4))
        elif etype == EV_POS_CLOSED:
            self._on_position_closed(f_msg(ev, 4))
        elif etype == EV_ACCOUNT:
            self._on_account(f_msg(ev, 5))
        elif etype == EV_STOP_OUT:
            # The venue force-closed something for margin health. It carries the
            # post-close snapshot itself and is NOT followed by its own
            # ACCOUNT_UPDATED, so this is the account update too.
            with self._lk:
                self._stop_out_until = time.time() + CLOSE_GRACE
            self.emit("margin.warning", {"level": "stop_out"})
            self._on_account(f_msg(ev, 5))

    def _on_account(self, a: dict):
        if not a:
            return
        view = self._account_view(a)
        ready = []
        with self._lk:
            balance = view["balance"]
            # Every close that was not this adapter's ClosePosition learns its
            # realised figure HERE, from the venue's own balance: the close
            # event carries none, and the account update that follows it does.
            # Done on the stream thread, in arrival order, so two closes in a
            # stop-out cascade each take their own delta rather than sharing
            # one.
            for job in self._pending_realize:
                if job.get("realized") is None and self._balance is not None:
                    job["realized"] = round(
                        balance - self._balance + job.get("fee", 0.0), 2)
                ready.append(job)
            self._pending_realize = []
            self._balance = balance
        if ready:
            self._wake.set()
        self.emit("account.updated", view)

    # == state machines ================================================
    def _absorb_order(self, o: dict, seed: bool = False):
        """Fold one venue Order into our view, emitting on TRANSITIONS only.

        The same order arrives from the PlaceOrder reply, from ORDER_UPDATE,
        from the FILL event and from a post-reconnect ListOrders, so everything
        here is keyed on a change of state and is safe to run any number of
        times. Without that, one order becomes four announcements.

        `seed` is for the catch-up read after a (re)connect: an order this
        process has never seen is RECORDED and not announced. Every order the
        account has ever placed comes back from ListOrders, and replaying a
        month of them as live events on connect would have a platform react to
        cancellations that happened last week.
        """
        if not o:
            return
        oid = str(f_int(o, 1))
        if oid == "0":
            return
        view = self._order_view(o)
        state = view["state"]
        wire_state = f_int(o, 15)
        emits = []
        with self._lk:
            rec = self._orders.setdefault(oid, {})
            first_sight = "announced" not in rec
            rec.setdefault("client_id", view["client_id"])
            client_id = rec.get("client_id") or view["client_id"]
            prev_type = rec.get("wire_type")
            prev_price = rec.get("price")
            prev_qty = rec.get("qty")
            prev_expires = rec.get("expires_at")
            rec["announced"] = True
            rec["state"] = state
            rec["wire_type"] = f_int(o, 5)
            rec["price"] = view["price"]
            rec["qty"] = view["qty"]
            rec["expires_at"] = view["expires_at"]
            rec["symbol"] = view["symbol"]
            rec["side"] = view["side"]
            if "type" not in rec:
                rec["type"] = view["type"]
            placed_type = rec.get("type")
            # An order that has finished is no longer chased by the poller.
            if state in ("filled", "cancelled", "expired"):
                rec["deadline"] = None
            if seed and first_sight:
                rec["resting_announced"] = state == "resting"
                rec["terminal_announced"] = state != "resting"
                return
            if first_sight:
                emits.append(("order.accepted", {"client_id": client_id,
                                                 "order_id": oid}))
            # A resting order is live. Announced on the venue's WORKING (or
            # PARTIAL) state and not on its transitional NEW, which every order
            # passes through including a market order that fills in the same
            # breath, and only once: repeats of WORKING follow every amend.
            if state == "resting" and wire_state in (1, 2) \
                    and not rec.get("resting_announced") \
                    and placed_type != "market":
                rec["resting_announced"] = True
                rec["deadline"] = None
                emits.append(("order.placed", {
                    "client_id": client_id, "order_id": oid,
                    "type": placed_type, "price": view["price"],
                    "expires_at": view["expires_at"]}))
            elif state == "resting" and rec.get("resting_announced"):
                # A STOP_LIMIT that reached its trigger reports as a plain LIMIT
                # from then on; the venue calls that a promotion and so does the
                # SPEC.
                if placed_type == "stop_limit" and prev_type != 1 \
                        and f_int(o, 5) == 1:
                    emits.append(("order.promoted", {
                        "order_id": oid, "from": "stop_limit", "to": "limit",
                        "price": view["price"]}))
                elif (view["price"], view["qty"], view["expires_at"]) != \
                        (prev_price, prev_qty, prev_expires):
                    emits.append(("order.amended", {
                        "order_id": oid, "price": view["price"],
                        "qty": view["qty"],
                        "expires_at": view["expires_at"]}))
            elif state == "cancelled" and not rec.get("terminal_announced"):
                rec["terminal_announced"] = True
                data = {"order_id": oid, "client_id": client_id}
                if oid not in self._cancelled_by_us \
                        and time.time() > self._flatten_until:
                    # SPEC 5: `code` present means the BROKER killed it. The one
                    # unsolicited cancel this venue performs is on an instrument
                    # reaching a listed expiry, which it also refuses new orders
                    # for; nothing else here cancels an order we did not.
                    data["code"] = "trade_restricted"
                    data["message"] = ("cancelled by the venue; the instrument "
                                       "stopped dealing")
                self._cancelled_by_us.discard(oid)
                emits.append(("order.cancelled", data))
            elif state == "expired" and not rec.get("terminal_announced"):
                rec["terminal_announced"] = True
                emits.append(("order.expired", {
                    "order_id": oid, "client_id": client_id,
                    "expiry": view["expiry"]}))
        for name, data in emits:
            self.emit(name, data)

    def _absorb_fill(self, order: dict, fill: dict):
        """One clip. Deduplicated on the venue's own fill_id.

        The venue promises `fill.fill_id` "matches the same fill in ListFills",
        which makes the backstop poller free of consequence: a fill the stream
        dropped is picked up from the ledger, and a fill the stream already
        delivered is recognised and dropped here instead of becoming a second
        position on the platform's book.
        """
        if not fill:
            return
        fid = f_int(fill, 1)
        with self._lk:
            if fid and fid in self._fills_seen:
                return
            if fid:
                self._fills_seen.add(fid)
        oid = str(f_int(fill, 2)) or str(f_int(order, 1))
        pid = str(f_int(fill, 3))
        symbol = f_str(fill, 4) or f_str(order, 3)
        qty = f_dbl(fill, 6)
        fee = f_dbl(fill, 8)
        client_id = f_str(order, 2) or None
        with self._lk:
            rec = self._orders.get(oid)
            if client_id is None and rec:
                client_id = rec.get("client_id")
            if fee and pid != "0":
                self._pos_comm[pid] = self._pos_comm.get(pid, 0.0) + fee
            if client_id and pid != "0":
                # SPEC 4: a position's client_id is the id of the order that
                # OPENED it. On a netting book every later order lands on the
                # same position, so the first writer wins; reassigning on each
                # touch renames a position after whatever last hit it.
                self._pos_client.setdefault(pid, client_id)
            # What the position CAME FROM, from the local record of what was
            # placed, where there is one: a triggered STOP_LIMIT reports itself
            # as a plain LIMIT from the moment it promotes, so reading the type
            # off the fill would file it under the wrong order type forever.
            placed = (rec or {}).get("type")
            if placed is None:
                placed = {0: "market", 1: "limit", 2: "stop",
                          3: "stop_limit"}.get(f_int(order, 5))
            self._pos_from.setdefault(
                pid, None if placed == "market" else placed)
            # A close in flight is told what the accompanying fill cost, so the
            # realised figure taken from the balance is not short by the fee.
            for job in self._pending_realize:
                if job["position_id"] == pid:
                    job["fee"] = job.get("fee", 0.0) + fee
                    job["netted"] = True
                    job["price"] = f_dbl(fill, 7)
        remaining = f_dbl(order, 7)
        partial = f_int(order, 15) == 2 or remaining > 1e-9
        data = {"client_id": client_id, "order_id": oid, "position_id": pid,
                "symbol": symbol,
                "side": SIDE_FROM_WIRE.get(f_int(fill, 5), "buy"),
                "qty": qty, "price": f_dbl(fill, 7),
                "fees": -abs(fee),
                "time": (f_int(fill, 9) // 1_000_000) or int(time.time() * 1000)}
        if partial:
            data["remaining"] = remaining
            self.emit("order.partial", data)
        else:
            self.emit("order.filled", data)

    def _absorb_position(self, p: dict):
        if not p:
            return
        pid = str(f_int(p, 1))
        if pid == "0":
            return
        with self._lk:
            prev = self._pos.get(pid) or {}
            prev_sl = f_dbl(prev, 7)
            prev_trail = f_dbl(prev, 9)
            prev_qty = f_dbl(prev, 4) if prev else None
            self._pos[pid] = p
        qty = f_dbl(p, 4)
        if prev_qty is not None and qty < prev_qty - 1e-9:
            # The position SHRANK: a partial close, or an opposing order netting
            # it down. The venue reports both as a plain position update, so the
            # size is the only thing that says a close happened, and a platform
            # that is not told is left holding size it no longer has.
            self._file_close(pid, prev, round(prev_qty - qty, 10), remaining=qty)
            return
        sl = f_dbl(p, 7)
        trail = f_dbl(p, 9)
        if trail and sl and sl != prev_sl and prev_trail == trail:
            # The venue moved a trailing stop on its own; the platform must be
            # told, or its picture of the stop is the one it last set.
            self.emit("position.trailed", {"position_id": pid, "sl": sl,
                                           "trail_points": trail})

    def _on_position_closed(self, p: dict):
        if not p:
            return
        pid = str(f_int(p, 1))
        if pid == "0":
            return
        with self._lk:
            snapshot = self._pos.get(pid) or p
        self._file_close(pid, snapshot, f_dbl(snapshot, 4), remaining=0.0)

    def _file_close(self, pid: str, snapshot: dict, closed_qty: float,
                    remaining: float):
        """A position lost size, for a reason the wire does not carry.

        Everything that could say why arrives AROUND this moment rather than in
        it, so the close is filed here and resolved a beat later by
        `_resolve_close`. Reporting straight away would mean reporting `unknown`
        every time, including for closes this adapter asked for itself.
        """
        full = remaining <= 1e-9
        with self._lk:
            mine = self._closing.pop(pid, None)
            if mine is not None and time.time() - mine["at"] > 15.0:
                # A ticket left over from an earlier close (a partial close does
                # not empty the position, so its ticket outlived it). Attributing
                # this close to it would report a stale realised figure with full
                # confidence, which is how the wrong number gets believed.
                mine = None
            whole = f_dbl(snapshot, 4) or closed_qty or 1.0
            charged = self._pos_comm.get(pid, 0.0)
            # Commission is charged on the fills that OPENED the position, so a
            # partial close takes its share and the rest stays with what is left
            # of the position. Reporting the whole charge on each leg would bill
            # the holder twice for one entry.
            share = charged if full else round(
                charged * max(0.0, min(1.0, closed_qty / whole)), 10)
            job = {
                "position_id": pid,
                "symbol": f_str(snapshot, 2),
                "side": SIDE_FROM_WIRE.get(f_int(snapshot, 3), "buy"),
                "qty": closed_qty,
                "avg_price": f_dbl(snapshot, 5),
                "sl": f_dbl(snapshot, 7),
                "tp": f_dbl(snapshot, 8),
                "at": time.time(),
                "realized": None,
                "fee": 0.0,
                "netted": False,
                "price": None,
                "commission": share,
                "client_id": self._pos_client.get(pid),
                "mine": mine,
                "flatten": (time.time() < self._flatten_until
                            and pid in self._flatten_ids),
                "stop_out": time.time() < self._stop_out_until,
                "full": full,
            }
            if mine is not None:
                job["realized"] = mine.get("realized")
                if mine.get("closed_qty"):
                    job["qty"] = mine["closed_qty"]
            if full:
                self._pos.pop(pid, None)
            else:
                self._pos_comm[pid] = round(charged - share, 10)
            self._close_jobs.append(job)
            if job["realized"] is None:
                self._pending_realize.append(job)
        self._wake.set()

    def _resolve_close(self, job: dict):
        """Turn one filed close into a position.closed the platform can trust.

        The venue's POSITION_CLOSED says only that the position is gone. What
        actually closed it is recoverable, not guessable:
          * this adapter knows what IT asked for (ClosePosition, FlattenAll);
          * a STOP_OUT event names a margin liquidation outright;
          * the exit print in ListFills carries `order_id`, which is 0 for every
            close the VENUE initiated and non-zero when an opposing order netted
            the position down, and it carries the price the close was done at;
          * the venue triggers a stop loss when the exit side of the market
            reaches the level and a take profit when it reaches the other, and
            the two levels sit on opposite sides of the entry, so a print at or
            through exactly one of them identifies which fired.
        Where those do not settle it, the reason is `unknown` (SPEC 5) with the
        venue's own words in reason_detail, because a platform showing "closed
        by your stop loss" with the same confidence it shows a fact is worse
        than one showing that nobody knows.
        """
        pid = job["position_id"]
        price = job.get("price")
        exit_fill = None
        if not job["netted"]:
            exit_fill = self._exit_fill(pid, job["at"])
            if exit_fill:
                price = f_dbl(exit_fill, 7)
                if not job.get("mine"):
                    job["qty"] = f_dbl(exit_fill, 6) or job["qty"]
        reason, detail = "unknown", None
        if job.get("mine"):
            reason = "flatten" if job["mine"].get("reason") == "flatten" \
                else "manual"
        elif job["flatten"]:
            reason = "flatten"
        elif job["stop_out"]:
            reason = "stop_out"
        elif job["netted"] or (exit_fill is not None and f_int(exit_fill, 2)):
            # An opposing order of the client's own, netting the book down. The
            # SPEC's closed reason set has no word for it; `manual` is the least
            # wrong (the client's own order did it, no venue-side event was
            # involved) and the venue's own word is kept for a human.
            reason, detail = "manual", "netted by an opposing order"
        elif price:
            hit_sl = job["sl"] and (price <= job["sl"] if job["side"] == "buy"
                                    else price >= job["sl"])
            hit_tp = job["tp"] and (price >= job["tp"] if job["side"] == "buy"
                                    else price <= job["tp"])
            if hit_sl and not hit_tp:
                reason = "sl"
            elif hit_tp and not hit_sl:
                reason = "tp"
            else:
                detail = ("closed at %s with sl=%s tp=%s; the venue does not "
                          "say which fired" % (price, job["sl"], job["tp"]))
        if job["realized"] is None:
            # No account update reached us inside the grace. Rather than compute
            # a figure and let it pass for the venue's own, nothing is claimed
            # beyond zero and the gap is said out loud.
            log("close %s: no balance update arrived; realised P/L is not "
                "recoverable and is reported as 0" % pid)
        data = {
            "position_id": pid,
            "qty": round(job["qty"], 10),
            "price": price if price else job["avg_price"],
            "realized_pnl": round(_num(job["realized"], 0.0), 2),
            # This venue charges no financing at all, and closing is commission
            # free; the figure here is what the OPENING fills cost. Both are
            # facts, not the "cannot split it" zero.
            "swap": 0.0,
            "commission": -round(job["commission"], 6),
            "reason": reason,
            "time": int(job["at"] * 1000),
        }
        if detail:
            data["reason_detail"] = detail
        if job.get("client_id"):
            data["client_id"] = job["client_id"]
        if job.get("full"):
            with self._lk:
                self._pos_comm.pop(pid, None)
                self._pos_client.pop(pid, None)
                self._pos_from.pop(pid, None)
        self.emit("position.closed", data)

    def _exit_fill(self, pid: str, at: float):
        """The venue's own print for the deal that closed a position."""
        try:
            reply = pb_parse(self.api.call(
                "ListFills", e_varint(1, int((at - 30.0) * 1_000_000_000))
                + e_varint(2, 200)))
        except (AdapterError, GrpcError) as e:
            log("close %s: could not read the exit print: %s" % (pid, e))
            return None
        best = None
        for fl in f_rep(reply, 1):
            if str(f_int(fl, 3)) != pid:
                continue
            best = fl
        return best

    # == market data stream ============================================
    def _restart_md(self):
        with self._lk:
            self._md_gen += 1
            gen = self._md_gen
            cur = self._md_stream
            self._md_stream = None
        if cur is not None:
            conn, st = cur
            try:
                conn.cancel(st)
            except Exception:       # noqa: BLE001 - cancelling is best effort
                pass
        self._spawn(lambda: self._md_loop(gen))

    def _md_loop(self, gen: int):
        """One StreamMarketData call per symbol set.

        The venue fixes a stream's symbols in the request that opened it, so a
        changed subscription means a new call. Reconnecting always replays a
        fresh snapshot round, which is how a shut market still gets a price:
        its ticks stop, but the snapshot on open does not.
        """
        while not self._stop.is_set():
            with self._lk:
                if gen != self._md_gen:
                    return
                symbols = list(self._quote_syms)
            if not symbols:
                return
            conn = st = None
            try:
                body = b"".join(e_str(1, s) for s in symbols)
                conn, st = self.api.open_stream("StreamMarketData", body)
                with self._lk:
                    if gen != self._md_gen:
                        conn.cancel(st)
                        return
                    self._md_stream = (conn, st)
                while not self._stop.is_set():
                    with self._lk:
                        if gen != self._md_gen:
                            conn.cancel(st)
                            return
                    msg = _read_message(st, time.time() + 3600)
                    if msg is None:
                        raise WireError("the venue ended the quote stream")
                    tick = pb_parse(msg)
                    self.emit("quote", {
                        "symbol": f_str(tick, 1), "bid": f_dbl(tick, 2),
                        "ask": f_dbl(tick, 3),
                        "time": f_int(tick, 5) // 1_000_000})
            except Exception as e:      # noqa: BLE001 - anything reconnects
                if self._stop.is_set():
                    return
                with self._lk:
                    if gen != self._md_gen:
                        return
                log("quote stream: %s: %s" % (type(e).__name__, e))
                if conn is not None:
                    self.api._drop("stream", conn)
                time.sleep(1.0)

    # == the poller ====================================================
    def _poll_loop(self):
        """The backstop for everything the streams are supposed to say.

        It sleeps unless something is in flight, so an idle adapter costs the
        venue nothing; the venue's own advice is not to poll faster than about
        once a second, and this obeys it.
        """
        last_bars = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            try:
                self._reap_closes()
                self._reap_orders()
                if time.time() - last_bars > 10.0:
                    last_bars = time.time()
                    self._reap_bars()
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log("poll: %s: %s" % (type(e).__name__, e))

    def _reap_closes(self):
        now = time.time()
        ready = []
        with self._lk:
            keep = []
            for job in self._close_jobs:
                if job["realized"] is not None or now - job["at"] > CLOSE_GRACE:
                    ready.append(job)
                else:
                    keep.append(job)
            self._close_jobs = keep
            if ready:
                pending = {id(j) for j in ready}
                self._pending_realize = [j for j in self._pending_realize
                                         if id(j) not in pending]
        for job in ready:
            self._resolve_close(job)

    def _reap_orders(self):
        """Any order this adapter placed that has not finished, chased down.

        The stream is the fast path; this is the one that makes a dropped frame
        cost latency instead of correctness. A market order that has not reached
        a terminal state inside the declared timeout is announced as a degraded
        session rather than waited on in silence (SPEC 8.4).
        """
        with self._lk:
            waiting = [(oid, dict(r)) for oid, r in self._orders.items()
                       if r.get("deadline")]
        # Once a second at most: the venue asks not to be polled faster than
        # that, and a fill this misses by half a second still arrives on the
        # stream, which is the fast path this only backs up.
        if not waiting or time.time() - self._last_order_poll < 1.0:
            return
        self._last_order_poll = time.time()
        try:
            rows = f_rep(pb_parse(self.api.call("ListOrders", e_varint(1, 1))), 1)
            fills = f_rep(pb_parse(self.api.call("ListFills",
                                                 e_varint(2, 100))), 1)
        except AdapterError as e:
            log("order poll: %s" % e)
            return
        by_id = {str(f_int(o, 1)): o for o in rows}
        for oid, rec in waiting:
            o = by_id.get(oid)
            if o is None:
                continue
            self._absorb_order(o)
            for fl in fills:
                if str(f_int(fl, 2)) == oid:
                    self._absorb_fill(o, fl)
            with self._lk:
                cur = self._orders.get(oid) or {}
                overdue = (cur.get("deadline") and time.time() > cur["deadline"]
                           and cur.get("state") == "resting"
                           and cur.get("market"))
                if cur.get("state") in ("filled", "cancelled", "expired"):
                    cur["deadline"] = None
                if overdue:
                    cur["deadline"] = None
            if overdue:
                self._set_session(
                    "degraded",
                    "order %s has not reached a terminal state within %ds"
                    % (oid, int(ORDER_DEADLINE)))

    def _reap_bars(self):
        with self._lk:
            wants = dict(self._bars)
        for (symbol, tf), last in wants.items():
            try:
                reply = pb_parse(self.api.call(
                    "GetCandles",
                    e_str(1, symbol) + e_varint(2, TF_TO_WIRE[tf])
                    + e_varint(5, 2), timeout=15.0))
            except (AdapterError, GrpcError) as e:
                log("bar poll %s %s: %s" % (symbol, tf, e))
                continue
            for c in f_rep(reply, 1):
                t = f_int(c, 1) // 1_000_000
                # The venue returns completed bars only, so anything newer than
                # the last one emitted is a closed bar the platform has not seen.
                if t <= last:
                    continue
                self.emit("bar", {"symbol": symbol, "timeframe": tf,
                                  "bar": [t, f_dbl(c, 2), f_dbl(c, 3),
                                          f_dbl(c, 4), f_dbl(c, 5),
                                          f_dbl(c, 6)]})
                with self._lk:
                    if (symbol, tf) in self._bars:
                        self._bars[(symbol, tf)] = t

    def _set_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a pipe.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    SablePointAdapter().run()


if __name__ == "__main__":
    main()
