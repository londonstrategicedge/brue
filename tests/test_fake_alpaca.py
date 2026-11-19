"""Tests of tests/fake_alpaca.py, the local stand-in for Alpaca.

The first classes replay, request by request, what Alpaca's paper API was seen to answer on 2026-11-10 (the crypto
round trip with its fee, the duplicate client id, every refusal, share orders queued while the market is shut, a
resting order's life, the order stream's frames and events) and assert the same statuses, bodies and shapes. The
rest test the market data, the order engine and each /_control switch. Loopback only; no key, no network.

Run: PYTHONPATH=. python3 -m unittest tests.test_fake_alpaca -v     (or: python3 -m unittest discover -s tests)
"""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import unittest
from datetime import date, timedelta
from decimal import Decimal as D

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_alpaca  # noqa: E402
from fake_alpaca import FakeAlpaca  # noqa: E402

KEY, SECRET = "PKTESTTESTTESTTEST01", "not-a-real-secret"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,9})?Z$")

# Field names, in the order Alpaca printed them on 2026-11-10.
ACCOUNT_KEYS = ["id", "admin_configurations", "user_configurations", "account_number", "status", "crypto_status", "options_approved_level",
                "options_trading_level", "currency", "buying_power", "regt_buying_power", "effective_buying_power", "non_marginable_buying_power",
                "options_buying_power", "cash", "accrued_fees", "portfolio_value", "trading_blocked", "transfers_blocked", "account_blocked",
                "created_at", "trade_suspended_by_user", "multiplier", "shorting_enabled", "equity", "last_equity", "long_market_value",
                "short_market_value", "position_market_value", "initial_margin", "maintenance_margin", "last_maintenance_margin", "sma",
                "balance_asof", "crypto_tier", "intraday_adjustments", "pending_reg_taf_fees"]
ORDER_KEYS = ["id", "client_order_id", "created_at", "updated_at", "submitted_at", "filled_at", "expired_at", "canceled_at", "failed_at",
              "replaced_at", "replaced_by", "replaces", "asset_id", "symbol", "asset_class", "notional", "qty", "filled_qty", "filled_avg_price",
              "order_class", "order_type", "type", "side", "position_intent", "time_in_force", "limit_price", "stop_price", "status",
              "extended_hours", "legs", "trail_percent", "trail_price", "hwm", "subtag", "source", "expires_at"]
GROUP_ORDER_KEYS = ORDER_KEYS[:33] + ["group_id", "group_role"] + ORDER_KEYS[33:]
STREAM_ORDER_KEYS = ORDER_KEYS[:7] + ["cancel_requested_at"] + ORDER_KEYS[7:33] + ["expires_at"]
POSITION_KEYS = ["asset_id", "symbol", "exchange", "asset_class", "asset_marginable", "qty", "qty_available", "avg_entry_price", "side",
                 "market_value", "cost_basis", "unrealized_pl", "unrealized_plpc", "unrealized_intraday_pl", "unrealized_intraday_plpc",
                 "current_price", "lastday_price", "change_today"]
FILL_KEYS = ["id", "activity_type", "transaction_time", "type", "price", "qty", "side", "symbol", "leaves_qty", "order_id", "cum_qty",
             "order_status", "swap_rate"]
EVENT_KEYS = ["at", "event_id", "event", "timestamp", "order"]
FILL_EVENT_KEYS = EVENT_KEYS + ["price", "qty", "position_qty", "execution_id", "swap_rate", "settle_date"]
SHARE_ASSET_KEYS = ["id", "class", "exchange", "symbol", "name", "status", "tradable", "marginable", "maintenance_margin_requirement",
                    "margin_requirement_long", "margin_requirement_short", "shortable", "easy_to_borrow", "borrow_status", "fractionable", "attributes"]
COIN_ASSET_KEYS = SHARE_ASSET_KEYS[:13] + ["fractionable", "attributes", "min_order_size", "min_trade_increment", "price_increment"]
BAR_KEYS = ["c", "h", "l", "n", "o", "t", "v", "vw"]
STOCK_QUOTE_KEYS = ["ap", "as", "ax", "bp", "bs", "bx", "c", "t", "z"]
STOCK_TRADE_KEYS = ["c", "i", "p", "s", "t", "x", "z"]
SNAPSHOT_KEYS = ["dailyBar", "latestQuote", "latestTrade", "minuteBar", "prevDailyBar"]
SIP_REFUSAL = b'{"message":"subscription does not permit querying recent SIP data"}\n'


def cents(x):
    return D(x).quantize(D("0.01"), rounding="ROUND_HALF_UP")


def plain(x):
    """A Decimal as Alpaca prints it."""
    return fake_alpaca.num(x)


class Answer:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw
        try:
            self.json = json.loads(raw) if raw else None
        except ValueError:
            self.json = None


class Ws:
    """The least websocket client that can talk to /stream: masked text frames out, any frames in."""

    def __init__(self, port, agent="fake-alpaca-test-ws/1"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: %s\r\n\r\n" % (port, key, agent)).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self._more()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        assert lines[0].split()[1] == "101", lines[0]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        assert ("sec-websocket-accept: " + accept).lower() in [line.lower() for line in lines], lines
        self.opcodes, self.raws = [], []

    def _more(self):
        data = self.sock.recv(65536)
        if not data:
            raise EOFError("the server ended the connection")
        self.buf += data

    def _take(self, n):
        while len(self.buf) < n:
            self._more()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj, opcode=1):
        payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(c ^ mask[i & 3] for i, c in enumerate(payload)))

    def frame(self, timeout=3.0):
        """(opcode, payload) of the next frame; socket.timeout when none comes."""
        self.sock.settimeout(timeout)
        b0, b1 = self._take(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._take(8))[0]
        assert not b1 & 0x80, "a server frame must not be masked"
        payload = self._take(n)
        self.opcodes.append(b0 & 0x0F)
        self.raws.append(payload)
        return b0 & 0x0F, payload

    def message(self, timeout=3.0):
        opcode, payload = self.frame(timeout)
        assert opcode == 2, "every frame from the server must be binary, got opcode %d" % opcode
        return json.loads(payload.decode("utf-8"))

    def event(self, timeout=3.0):
        m = self.message(timeout)
        assert m["stream"] == "trade_updates", m
        return m["data"]

    def quiet(self, seconds=0.25):
        try:
            got = self.frame(seconds)
        except socket.timeout:
            return True
        raise AssertionError("an unexpected frame: %r" % (got,))

    def login(self, listen=True):
        self.send({"action": "auth", "key": KEY, "secret": SECRET})
        assert self.message()["data"]["status"] == "authorized"
        if listen:
            self.send({"action": "listen", "data": {"streams": ["trade_updates"]}})
            assert self.message()["stream"] == "listening"
        return self

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Base(unittest.TestCase):
    MARKET, PLAN = "open", "free"
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeAlpaca(key_id=KEY, secret=SECRET, plan=cls.PLAN, market=cls.MARKET).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.fake.requests(clear=True)

    def call(self, method, path, body=None, auth=True, headers=None, agent="fake-alpaca-test/1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=10)
        h = {"User-Agent": agent}
        if auth:
            h.update({"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET})
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None and not isinstance(body, (bytes, str)) else body, headers=h)
            r = conn.getresponse()
            return Answer(r.status, r.headers, r.read())
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def order(self, expect=200, **fields):
        r = self.call("POST", "/v2/orders", fields)
        self.assertEqual(r.status, expect, r.raw)
        return r.json

    def refused(self, status, body, **fields):
        r = self.call("POST", "/v2/orders", fields)
        self.assertEqual((r.status, r.json), (status, body), fields)
        self.assertEqual(list(r.json), sorted(r.json), "a refusal's keys are in alphabetical order")

    def read(self, order_id, nested=False):
        r = self.get("/v2/orders/%s%s" % (order_id, "?nested=true" if nested else ""))
        self.assertEqual(r.status, 200, r.raw)
        return r.json

    def settled(self, order_id=None):
        self.assertTrue(self.fake.settle(), "the engine did not come to rest")
        return self.read(order_id) if order_id else None

    def until(self, what, seconds=4.0):
        end = time.time() + seconds
        while time.time() < end:
            got = what()
            if got:
                return got
            time.sleep(0.02)
        self.fail("timed out waiting")

    def quote(self, symbol):
        if "/" in symbol:
            q = self.get("/v1beta3/crypto/us/latest/quotes?symbols=" + symbol).json["quotes"][symbol]
        else:
            q = self.get("/v2/stocks/quotes/latest?feed=iex&symbols=" + symbol).json["quotes"][symbol]
        return D(str(q["bp"])), D(str(q["ap"]))

    def account(self):
        return self.get("/v2/account").json

    def positions(self):
        return self.get("/v2/positions").json


# ---------------------------------------------------------------------------------------------------------------
# What was seen on Alpaca paper, replayed
# ---------------------------------------------------------------------------------------------------------------

class ObservedCryptoRoundTrip(Base):
    def test_buy_fee_in_the_coin_duplicate_id_and_close(self):
        self.fake.set_price("BTC/USD", price="74200")
        bid, ask = self.quote("BTC/USD")
        before = self.account()
        self.assertEqual(list(before), ACCOUNT_KEYS)
        self.assertEqual([before[k] for k in ("cash", "equity", "buying_power", "regt_buying_power", "non_marginable_buying_power", "multiplier",
                                              "shorting_enabled", "crypto_status", "status", "accrued_fees")],
                         ["100000", "100000", "400000", "200000", "100000", "4", True, "ACTIVE", "ACTIVE", "0"])
        self.assertNotIn("pattern_day_trader", before)
        self.assertNotIn("daytrade_count", before)

        r = self.call("POST", "/v2/orders", {"symbol": "BTC/USD", "qty": "0.0002", "side": "buy", "type": "market", "time_in_force": "gtc",
                                              "client_order_id": "bcprobe-130230-1"})
        self.assertEqual(r.status, 200)                                   # 200, not 201
        o = r.json
        self.assertEqual(list(o), ORDER_KEYS)
        self.assertTrue(UUID.match(o["id"]))
        want = {"client_order_id": "bcprobe-130230-1", "filled_at": None, "expired_at": None, "canceled_at": None, "failed_at": None,
                "replaced_at": None, "replaced_by": None, "replaces": None, "asset_id": "276e2673-764b-4ab6-a611-caf665ca6340", "symbol": "BTC/USD",
                "asset_class": "crypto", "notional": None, "qty": "0.0002", "filled_qty": "0", "filled_avg_price": None, "order_class": "",
                "order_type": "market", "type": "market", "side": "buy", "position_intent": "buy_to_open", "time_in_force": "gtc",
                "limit_price": None, "stop_price": None, "status": "pending_new", "extended_hours": False, "legs": None, "trail_percent": None,
                "trail_price": None, "hwm": None, "subtag": None, "source": None}
        self.assertEqual({k: o[k] for k in want}, want)
        for k in ("created_at", "updated_at", "submitted_at"):
            self.assertTrue(STAMP.match(o[k]), o[k])
        self.assertEqual(o["created_at"], o["submitted_at"])
        # gtc is not for ever: the last 16:00 New York inside 90 days (21:00Z in winter, 20:00Z in summer)
        self.assertRegex(o["expires_at"], r"^\d{4}-\d\d-\d\dT2[01]:00:00Z$")
        left = fake_alpaca.parse_time(o["expires_at"]) - fake_alpaca.parse_time(o["created_at"])
        self.assertTrue(89 * 86400 <= left // 10 ** 9 <= 90 * 86400, left)

        filled = self.settled(o["id"])                                    # seen: filled about 2 ms after it was sent
        self.assertEqual([filled[k] for k in ("status", "filled_qty", "filled_avg_price", "created_at", "qty")],
                         ["filled", "0.0002", plain(ask), o["created_at"], "0.0002"])
        self.assertTrue(STAMP.match(filled["filled_at"]))

        # a repeated client_order_id is refused, not replayed, whether the body is the same or not
        for qty in ("0.0002", "0.0003"):
            self.refused(422, {"code": 40010001, "message": "client_order_id must be unique"}, symbol="BTC/USD", qty=qty, side="buy", type="market",
                         time_in_force="gtc", client_order_id="bcprobe-130230-1")
        r = self.get("/v2/orders:by_client_order_id?client_order_id=bcprobe-130230-1")
        self.assertEqual((r.status, r.json["id"], r.json["status"]), (200, o["id"], "filled"))
        r = self.get("/v2/orders:by_client_order_id?client_order_id=never-used")
        self.assertEqual((r.status, r.json), (404, {"code": 40410000, "message": "order not found for never-used"}))

        # the fee came out of the coin: 0.0002 filled, 0.0001995 held; the position is spelled without the slash
        held = D("0.0001995")
        rows = self.positions()
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertEqual(list(p), POSITION_KEYS)
        self.assertEqual([p[k] for k in ("symbol", "exchange", "asset_class", "asset_marginable", "qty", "qty_available", "avg_entry_price", "side")],
                         ["BTCUSD", "CRYPTO", "crypto", False, "0.0001995", "0.0001995", plain(ask), "long"])
        self.assertEqual(p["cost_basis"], plain((held * ask).quantize(D("0.000001"))))
        self.assertEqual(D(p["unrealized_pl"]), D(p["market_value"]) - D(p["cost_basis"]))
        self.assertEqual(p["asset_id"], "64bbff51-59d6-4b3c-9351-13ad85e3c752")          # not the order's asset_id
        self.assertNotEqual(p["asset_id"], o["asset_id"])
        r = self.get("/v2/positions/BTCUSD")
        self.assertEqual((r.status, r.json["qty"]), (200, "0.0001995"))
        r = self.get("/v2/positions/BTC%2FUSD")
        self.assertEqual((r.status, r.raw, r.headers["Content-Type"]), (404, b"Not Found", "text/plain; charset=utf-8"))
        r = self.get("/v2/assets/" + p["asset_id"])                        # the position's id resolves to the asset
        self.assertEqual((r.status, r.json["symbol"], r.json["id"]), (200, "BTC/USD", o["asset_id"]))

        fills = self.get("/v2/account/activities/FILL?page_size=5").json
        self.assertEqual(len(fills), 1)
        self.assertEqual(list(fills[0]), FILL_KEYS)
        self.assertRegex(fills[0]["id"], r"^\d{17}::[0-9a-f-]{36}$")
        self.assertEqual({k: fills[0][k] for k in FILL_KEYS[1:] if k != "transaction_time"},
                         {"activity_type": "FILL", "type": "fill", "price": plain(ask), "qty": "0.0002", "side": "buy", "symbol": "BTC/USD",
                          "leaves_qty": "0", "order_id": o["id"], "cum_qty": "0.0002", "order_status": "filled", "swap_rate": "1"})

        # so selling what the order says was bought is refused
        self.refused(403, {"available": "0.0001995", "balance": "0.0001995", "code": 40310000,
                           "message": "insufficient balance for BTC (requested: 0.0002, available: 0.0001995)", "symbol": "USD"},
                     symbol="BTC/USD", qty="0.0002", side="sell", type="market", time_in_force="gtc")

        r = self.call("DELETE", "/v2/positions/BTCUSD")
        self.assertEqual(r.status, 200)
        c = r.json
        self.assertEqual(list(c), ORDER_KEYS)
        self.assertEqual([c[k] for k in ("symbol", "side", "type", "time_in_force", "qty", "position_intent", "status", "order_class")],
                         ["BTC/USD", "sell", "market", "gtc", "0.0001995", "sell_to_close", "pending_new", ""])
        self.assertTrue(UUID.match(c["client_order_id"]))                  # a client id the server made
        closed = self.settled(c["id"])
        self.assertEqual([closed[k] for k in ("status", "filled_qty", "filled_avg_price")], ["filled", "0.0001995", plain(bid)])
        self.assertEqual(self.positions(), [])

        # the buy cost whole cents, the sell paid its fee out of the cash received; no fee is shown anywhere
        cash = D("100000") - cents(D("0.0002") * ask) + cents(held * bid * D("0.9975"))
        after = self.account()
        self.assertEqual([after[k] for k in ("cash", "equity", "portfolio_value", "long_market_value", "accrued_fees", "non_marginable_buying_power")],
                         [plain(cash), plain(cash), plain(cash), "0", "0", plain(cash)])
        self.assertEqual((after["buying_power"], after["regt_buying_power"]), (plain(cash * 4), plain(cash * 2)))
        self.assertLess(cash, D("100000"))

        rows = self.get("/v2/account/activities?page_size=6").json           # newest first; the funding entry is a JNLC
        self.assertEqual([(a["activity_type"], a.get("side")) for a in rows], [("FILL", "sell"), ("FILL", "buy"), ("JNLC", None)])
        self.assertEqual(list(rows[2]), ["id", "activity_type", "date", "created_at", "net_amount", "description", "status", "currency"])
        self.assertEqual((rows[2]["net_amount"], rows[2]["status"]), ("100000", "executed"))
        self.assertFalse([a for a in rows if a["activity_type"] in ("FEE", "CFEE")])

        for path in ("/v2/positions", "/v2/orders"):                         # nothing left to close or cancel
            r = self.call("DELETE", path)
            self.assertEqual((r.status, r.json), (207, []))

    def test_over_precise_quantity_is_truncated(self):
        o = self.order(symbol="BTC/USD", qty="0.0002000001", side="buy", type="market", time_in_force="gtc")
        self.assertEqual((o["qty"], o["status"]), ("0.0002", "pending_new"))
        self.assertEqual(self.settled(o["id"])["filled_qty"], "0.0002")


class ObservedRefusals(Base):
    """The observed refusals, row by row: HTTP status, code and message exactly."""

    def test_request_and_asset_refusals(self):
        self.fake.set_price("BTC/USD", price="74250", spread="15.6")          # bid 74242.2, ask 74257.8
        btc = dict(symbol="BTC/USD", side="buy", type="market", time_in_force="gtc")
        for symbol in ("NOTASYMBOL", "ZZZ/USD"):
            self.refused(422, {"code": 42210000, "message": 'asset "%s" not found' % symbol}, **dict(btc, symbol=symbol, qty="1"))
        small = {"code": 40310000, "message": "cost basis must be >= minimal amount of order 10"}
        self.refused(403, small, **dict(btc, qty="0.000001"))
        self.refused(403, small, **dict(btc, qty="0.0002", type="limit", limit_price="37120.65"))
        self.refused(403, {"code": 40310000, "message": "order notional 75742956 exceeds max notional per order 200000"}, **dict(btc, qty="1000"))
        self.refused(422, {"code": 42210000, "message": "invalid crypto time_in_force"}, **dict(btc, qty="0.0002", time_in_force="day"))
        self.refused(422, {"code": 40010001, "message": "invalid order type for crypto order"}, **dict(btc, qty="0.0002", type="stop", stop_price="89106.72"))
        for qty in ("0", "-1"):
            self.refused(422, {"code": 40010001, "message": "qty must be > 0"}, **dict(btc, qty=qty))
        self.refused(422, {"code": 40010001, "message": "limit orders require a limit price"}, **dict(btc, qty="0.0002", type="limit"))
        self.refused(422, {"code": 40010001, "message": "invalid side"}, **dict(btc, qty="0.0002", side="hold"))
        self.refused(422, {"code": 40010001, "message": "client_order_id must be no more than 128 characters"},
                     **dict(btc, qty="0.0002", type="limit", limit_price="37120.65", client_order_id="x" * 129))
        aapl = dict(symbol="AAPL", side="buy", type="market", time_in_force="day")
        self.refused(422, {"code": 42210000, "message": "invalid limit_price 100.123. sub-penny increment does not fulfill minimum pricing criteria"},
                     **dict(aapl, qty="1", type="limit", limit_price="100.123"))
        self.refused(422, {"code": 42210000, "message": "fractional orders must be DAY orders"},
                     **dict(aapl, qty="0.5", type="limit", limit_price="246.76", time_in_force="gtc"))
        self.refused(422, {"code": 42210000, "message": 'asset "PMNT" is not tradable'}, **dict(aapl, symbol="PMNT", qty="1"))
        self.refused(422, {"code": 42210000, "message": 'asset "YI" cannot be sold short'}, **dict(aapl, symbol="YI", qty="1", side="sell"))
        self.refused(403, {"code": 40310000, "message": 'asset "YI" is not fractionable'}, **dict(aapl, symbol="YI", qty="0.5"))
        self.refused(422, {"code": 42210000, "message": "extended hours order must be DAY or GTC limit orders"}, **dict(aapl, qty="1", extended_hours=True))
        r = self.get("/v2/orders/not-a-uuid")
        self.assertEqual((r.status, r.json), (422, {"code": 40010001, "message": "invalid order_id"}))
        unknown = "00000000-0000-4000-8000-000000000000"
        for method in ("DELETE", "GET"):
            r = self.call(method, "/v2/orders/" + unknown)
            self.assertEqual((r.status, r.json), (404, {"code": 40410000, "message": "order not found for " + unknown}))
        # a refusal makes no order, and its client id stays free
        self.assertEqual(self.get("/v2/orders?status=all").json, [])
        self.assertEqual(self.get("/v2/account/activities/FILL").json, [])

    def test_crypto_sell_more_than_held(self):
        self.order(symbol="BTC/USD", qty="0.0002", side="buy", type="market", time_in_force="gtc")
        self.settled()
        self.refused(403, {"available": "0.0001995", "balance": "0.0001995", "code": 40310000,
                           "message": "insufficient balance for BTC (requested: 0.0002, available: 0.0001995)", "symbol": "USD"},
                     symbol="BTC/USD", qty="0.0002", side="sell", type="market", time_in_force="gtc")

    def test_replace_a_cancelled_order(self):
        o = self.order(symbol="BTC/USD", qty="0.0005", side="buy", type="limit", limit_price="40819.7", time_in_force="gtc")
        self.settled()
        self.assertEqual(self.call("DELETE", "/v2/orders/" + o["id"]).status, 204)
        self.assertEqual(self.settled(o["id"])["status"], "canceled")
        r = self.call("PATCH", "/v2/orders/" + o["id"], {"limit_price": "1"})
        self.assertEqual((r.status, r.json), (422, {"code": 42210000, "message": "order is not open"}))


class ObservedPositionIntent(Base):
    """Seen on paper, 2026-11-11 04:54 UTC: an order MARKED as a close with nothing on that side to
    close is refused; the mark is taken where it is true; crypto checks the balance first."""

    MISMATCH = "position intent mismatch, inferred: %s, specified: %s"

    def test_a_marked_close_with_nothing_to_close_is_refused(self):
        spy = dict(symbol="SPY", qty="1", type="market", time_in_force="day")
        self.refused(422, {"code": 42210000, "message": self.MISMATCH % ("sell_to_open", "sell_to_close")},
                     **dict(spy, side="sell", position_intent="sell_to_close"))
        self.refused(422, {"code": 42210000, "message": self.MISMATCH % ("buy_to_open", "buy_to_close")},
                     **dict(spy, side="buy", position_intent="buy_to_close"))
        # nothing was made, and the client id of a refused order stays free
        self.assertEqual(self.get("/v2/orders?status=all").json, [])
        # the same order unmarked is taken as it always was (here it opens a short)
        o = self.order(**dict(spy, side="sell"))
        self.assertEqual((o["position_intent"], self.settled(o["id"])["status"]), ("sell_to_open", "filled"))
        self.assertEqual([(p["symbol"], p["qty"]) for p in self.positions()], [("SPY", "-1")])
        # ... and now a SELL marked as a close is still one with nothing on ITS side to close
        self.refused(422, {"code": 42210000, "message": self.MISMATCH % ("sell_to_open", "sell_to_close")},
                     **dict(spy, side="sell", position_intent="sell_to_close"))
        c = self.order(**dict(spy, side="buy", position_intent="buy_to_close"))
        self.assertEqual((c["position_intent"], self.settled(c["id"])["status"]), ("buy_to_close", "filled"))
        self.assertEqual(self.positions(), [])

    def test_crypto_the_mark_is_taken_where_true_and_the_balance_is_checked_first(self):
        btc = dict(symbol="BTC/USD", type="market", time_in_force="gtc")
        o = self.order(**dict(btc, qty="0.0004", side="buy", position_intent="buy_to_open"))
        self.assertEqual((o["position_intent"], self.settled(o["id"])["status"]), ("buy_to_open", "filled"))
        held = D(self.positions()[0]["qty"])
        # a BUY marked as a close while LONG: the same refusal
        self.refused(422, {"code": 42210000, "message": self.MISMATCH % ("buy_to_open", "buy_to_close")},
                     **dict(btc, qty="0.0004", side="buy", position_intent="buy_to_close"))
        # a partial close, then the rest: taken, and the Order echoes the field
        part = self.order(**dict(btc, qty="0.0002", side="sell", position_intent="sell_to_close"))
        self.assertEqual((part["position_intent"], self.settled(part["id"])["status"]), ("sell_to_close", "filled"))
        left = fake_alpaca.num(held - D("0.0002"))
        # more than is held, marked: the BALANCE answers, before the intent
        r = self.call("POST", "/v2/orders", dict(btc, qty="0.0003", side="sell", position_intent="sell_to_close"))
        self.assertEqual((r.status, r.json["message"]), (403, "insufficient balance for BTC (requested: 0.0003, available: %s)" % left))
        rest = self.order(**dict(btc, qty=left, side="sell", position_intent="sell_to_close"))
        self.assertEqual(self.settled(rest["id"])["status"], "filled")
        self.assertEqual(self.positions(), [])
        # nothing held, marked or not: the balance again
        for mark in ({}, {"position_intent": "sell_to_close"}):
            r = self.call("POST", "/v2/orders", dict(btc, qty="0.0002", side="sell", **mark))
            self.assertEqual((r.status, r.json["message"]), (403, "insufficient balance for BTC (requested: 0.0002, available: 0)"))

    def test_closing_a_position_that_is_not_there(self):
        for name in ("BTCUSD", "SPY"):
            r = self.call("DELETE", "/v2/positions/" + name)
            self.assertEqual((r.status, r.json), (404, {"code": 40410000, "message": "position not found: " + name}))
        self.assertEqual(self.get("/v2/orders?status=all").json, [])


class ObservedPositionIntentWhileShut(Base):
    """The same refusal came at once although the exchange was shut (an unmarked share order is accepted then and waits)."""
    MARKET = "shut"

    def test_refused_at_once_while_an_unmarked_order_waits(self):
        spy = dict(symbol="SPY", qty="1", side="sell", type="market", time_in_force="day")
        self.refused(422, {"code": 42210000, "message": "position intent mismatch, inferred: sell_to_open, specified: sell_to_close"},
                     **dict(spy, position_intent="sell_to_close"))
        o = self.order(**spy)
        self.assertEqual(self.settled(o["id"])["status"], "new")


class PositionIntentNotObserved(Base):
    """What Alpaca was NOT seen to do with a marked close, for shares: one for more than is held, and one sent while
    working orders hold the position's quantity. The stand-in takes the worst case (it accepts and trades the order)
    unless a test asks for the other guess."""

    def control(self, name, body=None):
        r = self.call("POST", "/_control/" + name, body if body is not None else {}, auth=False)
        self.assertEqual(r.status, 200, r.raw)
        return r.json

    def hold(self, qty="1"):
        self.fake.set_price("AAPL", price="350")
        o = self.order(symbol="AAPL", qty=qty, side="buy", type="market", time_in_force="day")
        self.settled(o["id"])

    def test_for_more_than_is_held_it_is_taken_and_goes_through_zero(self):
        self.hold("1")
        o = self.order(symbol="AAPL", qty="2", side="sell", type="market", time_in_force="day", position_intent="sell_to_close")
        self.assertEqual((o["position_intent"], self.settled(o["id"])["status"]), ("sell_to_close", "filled"))
        self.assertEqual([(p["symbol"], p["qty"], p["side"]) for p in self.positions()], [("AAPL", "-1", "short")])
        # the same order without the mark is held to what is there, as before
        self.fake.reset()
        self.hold("1")
        r = self.call("POST", "/v2/orders", dict(symbol="AAPL", qty="2", side="sell", type="market", time_in_force="day"))
        self.assertEqual((r.status, r.json["message"]), (403, "insufficient qty available for order (requested: 2, available: 1)"))

    def test_beside_a_working_order_that_holds_the_quantity_it_is_taken(self):
        self.hold("10")
        rest = self.order(symbol="AAPL", qty="10", side="sell", type="limit", limit_price="400", time_in_force="day")
        self.settled(rest["id"])
        self.assertEqual(self.positions()[0]["qty_available"], "0")
        unmarked = self.call("POST", "/v2/orders", dict(symbol="AAPL", qty="10", side="sell", type="market", time_in_force="day"))
        self.assertEqual((unmarked.status, unmarked.json["message"]), (403, "insufficient qty available for order (requested: 10, available: 0)"))
        o = self.order(symbol="AAPL", qty="10", side="sell", type="market", time_in_force="day", position_intent="sell_to_close")
        self.assertEqual(self.settled(o["id"])["status"], "filled")
        self.assertEqual(self.positions(), [])
        self.assertEqual(self.read(rest["id"])["status"], "new")         # ... and the other one still rests, with nothing behind it

    def test_the_other_guess_is_a_control(self):
        self.assertEqual(self.control("state")["intent_kind"], False)
        self.assertEqual(self.control("intent", {"kind": True}), {"kind": True})
        self.hold("1")
        marked = dict(symbol="AAPL", qty="2", side="sell", type="market", time_in_force="day", position_intent="sell_to_close")
        r = self.call("POST", "/v2/orders", marked)
        self.assertEqual((r.status, r.json["message"]), (403, "insufficient qty available for order (requested: 2, available: 1)"))
        self.assertEqual(self.fake.set_intent(kind=False), {"kind": False})
        self.assertEqual(self.call("POST", "/v2/orders", marked).status, 200)
        self.fake.set_intent(kind=True)
        self.fake.reset()                                                 # every control back as at the start
        self.assertEqual(self.control("state")["intent_kind"], False)

    def test_a_close_sent_twice_under_one_id_is_told_mismatch_once_the_first_has_filled(self):
        # stub's own: where the mismatch stands beside "client_order_id must be unique" was not observable
        self.hold("1")
        twice = dict(symbol="AAPL", qty="1", side="sell", type="market", time_in_force="day", position_intent="sell_to_close", client_order_id="close-1")
        first = self.order(**twice)
        self.refused(422, {"code": 40010001, "message": "client_order_id must be unique"}, **twice)     # while the position is still there
        self.settled(first["id"])
        self.refused(422, {"code": 42210000, "message": "position intent mismatch, inferred: sell_to_open, specified: sell_to_close"}, **twice)


class ObservedSharesWhileShut(Base):
    """08:05 New York, before the open: orders are accepted and wait; nothing fills and nothing is refused for the hour."""
    MARKET = "shut"

    def place_the_eight(self):
        self.fake.set_price("AAPL", price="352.53", spread="0.02")           # last 352.53, ask 352.54
        aapl = dict(symbol="AAPL", side="buy")
        return [self.order(**dict(aapl, qty="1", type="market", time_in_force="day", client_order_id="q-1")),
                self.order(**dict(aapl, qty="0.5", type="market", time_in_force="day", client_order_id="q-2")),
                self.order(**dict(aapl, notional="5", type="market", time_in_force="day", client_order_id="q-3")),
                self.order(**dict(aapl, qty="1", type="market", time_in_force="gtc", client_order_id="q-4")),
                self.order(**dict(aapl, qty="1", type="stop", stop_price="458.26", time_in_force="gtc", client_order_id="q-7")),
                self.order(**dict(aapl, qty="1", type="stop_limit", stop_price="458.26", limit_price="461.79", time_in_force="gtc", client_order_id="q-8")),
                self.order(**dict(aapl, qty="1", type="trailing_stop", trail_percent="5", time_in_force="gtc", client_order_id="q-9")),
                self.order(**dict(aapl, qty="1", type="limit", limit_price="246.76", time_in_force="gtc", order_class="bracket",
                                  take_profit={"limit_price": "423.01"}, stop_loss={"stop_price": "176.25"}, client_order_id="q-10"))]

    def test_queued_reserved_refused_and_cancelled(self):
        self.assertFalse(self.get("/v2/clock").json["is_open"])
        market, half, five, gtc, stop, stop_limit, trailing, bracket = orders = self.place_the_eight()
        # first answers: whole shares pending_new, fractional and notional accepted, the trailing stop new at once
        self.assertEqual([o["status"] for o in orders], ["pending_new", "accepted", "accepted", "pending_new", "pending_new", "pending_new", "new", "pending_new"])
        self.assertEqual([(five["qty"], five["notional"]), (half["qty"], half["notional"])], [(None, "5"), ("0.5", None)])
        created = fake_alpaca.parse_time(market["created_at"]) // 10 ** 9
        close = fake_alpaca.cal_next(created, 1)                              # `day` ends at the session's close ...
        self.assertEqual(market["expires_at"], fake_alpaca.ts(close * 10 ** 9))
        self.assertRegex(market["expires_at"], r"T(21|20|18|17):00:00Z$")
        ninety = fake_alpaca.ny_epoch(fake_alpaca.ny_date(created) + timedelta(90), 16)   # ... `gtc` 90 days on at 16:00 New York
        self.assertEqual([o["expires_at"] for o in (gtc, stop, stop_limit, trailing, bracket)], [fake_alpaca.ts(ninety * 10 ** 9)] * 5)
        self.assertEqual([trailing[k] for k in ("hwm", "trail_percent", "stop_price", "type")], ["352.53", "5", plain(D("352.53") * D("1.05")), "trailing_stop"])
        self.assertEqual([(stop["stop_price"], stop["limit_price"]), (stop_limit["stop_price"], stop_limit["limit_price"])],
                         [("458.26", None), ("458.26", "461.79")])
        # the bracket: its two exits are legs, held
        self.assertEqual(list(bracket), GROUP_ORDER_KEYS)
        self.assertEqual([bracket[k] for k in ("order_class", "group_role", "type", "limit_price")], ["bracket", "entry", "limit", "246.76"])
        tp, sl = bracket["legs"]
        for leg in (tp, sl):
            self.assertEqual(list(leg), GROUP_ORDER_KEYS)
            self.assertEqual([leg[k] for k in ("status", "side", "position_intent", "order_class", "legs", "group_id", "qty", "created_at", "expires_at")],
                             ["held", "sell", "sell_to_close", "bracket", None, bracket["group_id"], "1", bracket["created_at"], bracket["expires_at"]])
            self.assertTrue(UUID.match(leg["client_order_id"]))
        self.assertEqual([(tp[k], sl[k]) for k in ("group_role", "type", "limit_price", "stop_price")],
                         [("take_profit", "stop_loss"), ("limit", "stop"), ("423.01", None), (None, "176.25")])

        self.settled()
        rows = self.get("/v2/orders?status=open&limit=100&nested=true").json
        self.assertEqual([r["client_order_id"] for r in rows], ["q-10", "q-9", "q-8", "q-7", "q-4", "q-3", "q-2", "q-1"])       # newest first
        self.assertEqual([r["status"] for r in rows], ["new", "new", "new", "new", "new", "accepted", "accepted", "new"])
        self.assertEqual([leg["status"] for leg in rows[0]["legs"]], ["held", "held"])
        self.assertTrue(all(r["filled_qty"] == "0" and r["filled_at"] is None for r in rows))
        self.assertNotEqual(rows[7]["submitted_at"], rows[7]["created_at"])   # seen: a share order's submitted_at moves when it turns `new`
        self.assertEqual(rows[6]["submitted_at"], rows[6]["created_at"])
        # Without nested: a `held` leg is not a row of a status=open list. Seen for an OCO's stop (14:38 UTC) and for
        # the stop of a bracket whose entry had filled (17:47 UTC); for the legs of an UNFILLED bracket, as here, it
        # is that same rule applied, not a sight of its own. (In a status=all list legs ARE rows: seen 13:2x UTC.)
        flat = self.get("/v2/orders?status=open&limit=100").json
        self.assertEqual([r["client_order_id"] for r in flat], [r["client_order_id"] for r in rows])
        self.assertTrue(all(r["legs"] is None for r in flat))
        every = self.get("/v2/orders?status=all&limit=100").json
        self.assertEqual(len(every), 10)
        self.assertEqual(self.positions(), [])

        # queued buys reserve buying power at once
        value = sum(cents(v) for v in (D("352.54"), D("0.5") * D("352.54"), D(5), D("352.54"), D("458.26"), D("461.79"), D("352.53") * D("1.05"), D("246.76")))
        a = self.account()
        self.assertEqual([a[k] for k in ("cash", "equity", "initial_margin", "buying_power", "regt_buying_power", "non_marginable_buying_power")],
                         ["100000", "100000", plain(cents(value / 2)), plain(D(400000) - value), plain((D(100000) - cents(value / 2)) * 2),
                          plain(D(100000) - cents(value / 2))])

        # selling while own buys are open: a wash trade, naming the resting buy with the lowest id among the market and stop orders
        wash = {"code": 40310000, "existing_order_id": min(o["id"] for o in (market, half, five, gtc, stop)),
                "message": "potential wash trade detected. use complex orders", "reject_reason": "opposite side market/stop order exists"}
        self.refused(403, wash, symbol="AAPL", qty="1", side="sell", type="limit", limit_price="528.76", time_in_force="gtc")
        self.refused(403, wash, symbol="AAPL", qty="0.5", side="sell", type="market", time_in_force="day")
        self.refused(403, {"buying_power": a["buying_power"], "code": 40310000, "cost_basis": "35254000.3", "message": "insufficient buying power"},
                     symbol="AAPL", qty="100000", side="buy", type="market", time_in_force="day")

        r = self.call("DELETE", "/v2/orders")                                 # eight rows, id and status only, sorted by id; legs not listed
        self.assertEqual((r.status, r.json), (207, [{"id": i, "status": 200} for i in sorted(o["id"] for o in orders)]))
        self.settled()
        self.assertEqual(self.get("/v2/orders?status=open&limit=100").json, [])
        closed = self.get("/v2/orders?status=closed&limit=100").json
        self.assertEqual(len(closed), 10)
        self.assertTrue(all(r["status"] == "canceled" and r["canceled_at"] for r in closed))
        self.assertEqual(self.account()["buying_power"], "400000")

    def test_shares_wait_for_the_open_and_crypto_does_not(self):
        self.fake.set_price("AAPL", price="352.53", spread="0.02")
        whole = self.order(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day")
        part = self.order(symbol="AAPL", qty="0.5", side="buy", type="market", time_in_force="day")
        coin = self.order(symbol="ETH/USD", qty="0.01", side="buy", type="market", time_in_force="gtc")
        self.settled()
        time.sleep(0.15)
        self.assertEqual([self.read(o["id"])["status"] for o in (whole, part, coin)], ["new", "accepted", "filled"])
        self.assertEqual([p["symbol"] for p in self.positions()], ["ETHUSD"])
        self.fake.set_market("open")
        self.settled()
        self.assertEqual([(self.read(o["id"])["status"], self.read(o["id"])["filled_avg_price"]) for o in (whole, part)], [("filled", "352.54")] * 2)
        aapl = [p for p in self.positions() if p["symbol"] == "AAPL"][0]
        self.assertEqual([aapl[k] for k in ("qty", "side", "asset_class", "exchange", "avg_entry_price", "asset_id")],
                         ["1.5", "long", "us_equity", "NASDAQ", "352.54", "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"])
        events = [(e["data"]["event"], e["data"]["order"]["id"]) for e in self.call("GET", "/_control/events").json["events"]]
        self.assertEqual([e for e, i in events if i == part["id"]], ["accepted", "new", "fill"])
        self.assertEqual([e for e, i in events if i == whole["id"]], ["pending_new", "new", "fill"])


class ObservedRestingOrder(Base):
    def test_rest_replace_twice_cancel_twice(self):
        self.fake.set_price("BTC/USD", price="74200")
        self.order(symbol="BTC/USD", qty="0.0002", side="buy", type="market", time_in_force="gtc")
        self.settled()
        base = self.account()
        o = self.order(symbol="BTC/USD", qty="0.0004", side="buy", type="limit", limit_price="37108.82", time_in_force="gtc", client_order_id="bcprobe-130452-1")
        self.assertEqual([o[k] for k in ("status", "limit_price", "type", "qty")], ["pending_new", "37108.82", "limit", "0.0004"])
        self.assertEqual(self.settled(o["id"])["status"], "new")              # pending_new, then new
        # a resting crypto buy lowers `cash` by its cost; `equity` does not move
        a = self.account()
        self.assertEqual(D(a["cash"]), D(base["cash"]) - cents(D("0.0004") * D("37108.82")))
        self.assertEqual([a[k] for k in ("equity", "long_market_value", "initial_margin")], [base["equity"], base["long_market_value"], "0"])
        self.assertEqual((a["non_marginable_buying_power"], a["buying_power"], a["regt_buying_power"]), (a["cash"], plain(D(a["cash"]) * 4), plain(D(a["cash"]) * 2)))
        rows = self.get("/v2/orders?status=open&limit=50").json
        self.assertEqual([(r["id"], r["status"]) for r in rows], [(o["id"], "new")])

        r = self.call("PATCH", "/v2/orders/" + o["id"], {"limit_price": "40819.7"})
        self.assertEqual(r.status, 200)
        n = r.json                                                            # a NEW order
        self.assertEqual(list(n), ORDER_KEYS)
        self.assertNotEqual(n["id"], o["id"])
        self.assertEqual([n[k] for k in ("replaces", "status", "limit_price", "qty", "replaced_by", "symbol", "side", "type", "time_in_force", "expires_at")],
                         [o["id"], "new", "40819.7", "0.0004", None, "BTC/USD", "buy", "limit", "gtc", o["expires_at"]])
        self.assertTrue(UUID.match(n["client_order_id"]))                     # the server made its client id
        old = self.settled(o["id"])
        self.assertEqual([old[k] for k in ("status", "replaced_by", "limit_price", "client_order_id")], ["replaced", n["id"], "37108.82", "bcprobe-130452-1"])
        self.assertTrue(STAMP.match(old["replaced_at"]))
        self.assertEqual(self.read(n["id"])["status"], "new")

        r = self.call("PATCH", "/v2/orders/" + n["id"], {"qty": "0.0005"})   # a second replace: a new id again
        n2 = r.json
        self.assertEqual((r.status, n2["replaces"], n2["qty"], n2["limit_price"]), (200, n["id"], "0.0005", "40819.7"))
        self.assertEqual(len({o["id"], n["id"], n2["id"]}), 3)
        self.settled()

        r = self.call("DELETE", "/v2/orders/" + n2["id"])
        self.assertEqual((r.status, r.raw), (204, b""))
        gone = self.settled(n2["id"])
        self.assertEqual((gone["status"], gone["replaces"]), ("canceled", n["id"]))
        self.assertTrue(STAMP.match(gone["canceled_at"]))
        r = self.call("DELETE", "/v2/orders/" + n2["id"])                     # 204 again
        self.assertEqual((r.status, r.raw), (204, b""))
        self.assertEqual(self.account()["cash"], base["cash"])                # the reserve is back

        sl = self.order(symbol="BTC/USD", qty="0.0002", side="buy", type="stop_limit", stop_price="96521.02", limit_price="97263.49", time_in_force="gtc")
        self.assertEqual([sl[k] for k in ("status", "stop_price", "limit_price", "type")], ["new", "96521.02", "97263.49", "stop_limit"])
        self.assertEqual(self.call("DELETE", "/v2/orders/" + sl["id"]).status, 204)
        self.settled()
        self.assertEqual(self.get("/v2/orders?status=open").json, [])


class ObservedStream(Base):
    def setUp(self):
        super().setUp()
        self.sockets = []

    def tearDown(self):
        for ws in self.sockets:
            ws.close()

    def ws(self, login=True, listen=True):
        ws = Ws(self.fake.port)
        self.sockets.append(ws)
        return ws.login(listen) if login else ws

    def test_binary_frames_and_the_order_of_events(self):
        self.fake.set_price("ETH/USD", price="2095")
        bid, ask = self.quote("ETH/USD")
        ws = self.ws(login=False)
        ws.send({"action": "auth", "key": KEY, "secret": SECRET})
        self.assertEqual(ws.frame(), (2, b'{"stream":"authorization","data":{"action":"authenticate","status":"authorized"}}'))
        ws.send({"action": "listen", "data": {"streams": ["trade_updates"]}})
        self.assertEqual(ws.frame(), (2, b'{"stream":"listening","data":{"streams":["trade_updates"]}}'))

        # a market buy: pending_new, new, fill
        o = self.order(symbol="ETH/USD", qty="0.01", side="buy", type="market", time_in_force="gtc", client_order_id="bcprobe-130633-s1")
        pending, new, fill = ws.event(), ws.event(), ws.event()
        self.assertEqual([e["event"] for e in (pending, new, fill)], ["pending_new", "new", "fill"])
        self.assertEqual((list(pending), list(new), list(fill)), (EVENT_KEYS, EVENT_KEYS + ["execution_id"], FILL_EVENT_KEYS))
        for e in (pending, new, fill):
            self.assertEqual(list(e["order"]), STREAM_ORDER_KEYS)             # cancel_requested_at; no subtag, no source
            self.assertEqual((e["order"]["id"], e["order"]["client_order_id"], e["order"]["legs"]), (o["id"], "bcprobe-130633-s1", None))
            self.assertRegex(e["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z$")
            self.assertTrue(STAMP.match(e["timestamp"]))
        self.assertEqual([e["order"]["status"] for e in (pending, new, fill)], ["pending_new", "new", "filled"])
        self.assertEqual({k: pending["order"][k] for k in ORDER_KEYS if k not in ("subtag", "source")}, {k: o[k] for k in ORDER_KEYS if k not in ("subtag", "source")})
        self.assertEqual([fill[k] for k in ("price", "qty", "position_qty", "swap_rate")], [plain(ask), "0.01", "0.009975", "1"])   # the fee again
        self.assertEqual((fill["order"]["filled_qty"], fill["order"]["filled_avg_price"], fill["order"]["filled_at"]), ("0.01", plain(ask), fill["timestamp"]))
        self.assertRegex(fill["settle_date"], r"^\d{4}-\d\d-\d\d$")
        self.assertTrue(UUID.match(new["execution_id"]) and UUID.match(fill["execution_id"]) and new["execution_id"] != fill["execution_id"])

        # a resting limit: pending_new, new
        rest = self.order(symbol="ETH/USD", qty="0.02", side="buy", type="limit", limit_price="1256.51", time_in_force="gtc", client_order_id="bcprobe-130633-s2")
        self.assertEqual([(e["event"], e["order"]["id"]) for e in (ws.event(), ws.event())], [("pending_new", rest["id"]), ("new", rest["id"])])
        ws.quiet()

        # replace: pending_replace (the old order), accepted (the NEW one, a new client id), replaced (the old one); no `new` after
        n = self.call("PATCH", "/v2/orders/" + rest["id"], {"limit_price": "1298.39"}).json
        a, b, c = ws.event(), ws.event(), ws.event()
        self.assertEqual([(e["event"], e["order"]["id"], e["order"]["status"]) for e in (a, b, c)],
                         [("pending_replace", rest["id"], "pending_replace"), ("accepted", n["id"], "accepted"), ("replaced", rest["id"], "replaced")])
        self.assertEqual((a["order"]["replaced_by"], b["order"]["replaces"], b["order"]["limit_price"], c["order"]["replaced_by"]), (n["id"], rest["id"], "1298.39", n["id"]))
        self.assertEqual(("execution_id" in a, "execution_id" in b, "execution_id" in c), (True, False, True))
        self.assertEqual(c["order"]["replaced_at"], c["timestamp"])
        self.assertNotEqual(b["order"]["client_order_id"], "bcprobe-130633-s2")
        ws.quiet()
        self.assertEqual(self.read(n["id"])["status"], "new")

        # cancel: canceled
        self.assertEqual(self.call("DELETE", "/v2/orders/" + n["id"]).status, 204)
        e = ws.event()
        self.assertEqual((e["event"], e["order"]["id"], e["order"]["status"], "execution_id" in e), ("canceled", n["id"], "canceled", True))
        self.assertTrue(STAMP.match(e["order"]["cancel_requested_at"]))
        self.assertEqual(e["order"]["canceled_at"], e["timestamp"])
        self.assertLessEqual(e["order"]["cancel_requested_at"], e["order"]["canceled_at"])

        # closing the position: pending_new, new, fill with position_qty "0"
        c = self.call("DELETE", "/v2/positions/ETHUSD").json
        x, y, z = ws.event(), ws.event(), ws.event()
        self.assertEqual([(e["event"], e["order"]["id"]) for e in (x, y, z)], [("pending_new", c["id"]), ("new", c["id"]), ("fill", c["id"])])
        self.assertEqual([z[k] for k in ("price", "qty", "position_qty")], [plain(bid), "0.009975", "0"])
        self.assertEqual((z["order"]["side"], z["order"]["position_intent"]), ("sell", "sell_to_close"))
        ws.quiet()

        self.assertEqual(set(ws.opcodes), {2})                                # every one of them binary
        ids = [json.loads(raw)["data"]["event_id"] for raw in ws.raws[2:]]
        self.assertEqual(len(ids), 12)
        self.assertTrue(all(re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", i) for i in ids), ids)
        self.assertEqual(ids, sorted(ids))                                    # sortable, and sent in order
        self.assertEqual(len(set(ids)), 12)

    def test_the_sdk_login_form_wrong_keys_and_listening_first(self):
        ws = self.ws(login=False)
        ws.send({"action": "listen", "data": {"streams": ["trade_updates"]}})
        self.assertEqual(ws.message(), {"stream": "authorization", "data": {"status": "unauthorized", "action": "listen"}})
        ws.send({"action": "authenticate", "data": {"key_id": KEY, "secret_key": "wrong"}})
        self.assertEqual(ws.message(), {"stream": "authorization", "data": {"action": "authenticate", "status": "unauthorized"}})
        ws.send(json.dumps({"action": "authenticate", "data": {"key_id": KEY, "secret_key": SECRET}}).encode(), opcode=2)      # a binary frame is read too
        self.assertEqual(ws.message(), {"stream": "authorization", "data": {"action": "authenticate", "status": "authorized"}})
        ws.send({"action": "listen", "data": {"streams": ["trade_updates", "no_such_stream"]}})
        self.assertEqual(ws.message(), {"stream": "listening", "data": {"streams": ["trade_updates"]}})        # an unknown stream is left out
        ws.send(b"are you there", opcode=9)
        self.assertEqual(ws.frame(), (10, b"are you there"))                  # a ping is answered
        ws.send(struct.pack(">H", 1000), opcode=8)
        self.assertEqual(ws.frame(), (8, struct.pack(">H", 1000)))            # and a close is echoed

    def test_every_listening_connection_gets_every_event(self):
        one, two, deaf = self.ws(), self.ws(), self.ws(listen=False)
        o = self.order(symbol="BTC/USD", qty="0.0002", side="buy", type="market", time_in_force="gtc")
        for ws in (one, two):
            self.assertEqual([(ws.event()["event"], o["id"]) for _ in range(3)], [("pending_new", o["id"]), ("new", o["id"]), ("fill", o["id"])])
        deaf.quiet()
        two.send({"action": "listen", "data": {"streams": []}})              # an empty list stops the updates
        self.assertEqual(two.message(), {"stream": "listening", "data": {"streams": []}})
        self.order(symbol="BTC/USD", qty="0.0002", side="buy", type="market", time_in_force="gtc")
        self.assertEqual(one.event()["event"], "pending_new")
        two.quiet()


# ---------------------------------------------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------------------------------------------

def yesterday(hhmmss):
    return "%sT%sZ" % ((date.fromtimestamp(time.time() - 86400 * 2)).isoformat(), hhmmss)


class MarketData(Base):
    def bars(self, symbols, timeframe, extra="", status=200, path="/v2/stocks/bars", feed="&feed=iex"):
        r = self.get("%s?symbols=%s&timeframe=%s%s%s" % (path, symbols, timeframe, feed, extra))
        self.assertEqual(r.status, status, r.raw)
        return r.json

    def test_sip_is_refused_exactly_and_iex_served(self):
        now = fake_alpaca.ts(time.time_ns() // 10 ** 9 * 10 ** 9)
        for path in ("/v2/stocks/quotes/latest?symbols=AAPL,SPY&feed=sip", "/v2/stocks/trades/latest?symbols=AAPL&feed=sip", "/v2/stocks/snapshots?symbols=AAPL&feed=sip",
                     "/v2/stocks/AAPL/quotes/latest?feed=sip", "/v2/stocks/bars?symbols=AAPL&timeframe=1Min&feed=sip&limit=2&sort=desc&end=" + now,
                     "/v2/stocks/bars?symbols=AAPL&timeframe=1Min&limit=2&end=" + now):       # no feed named means sip for bars
            r = self.get(path)
            self.assertEqual((r.status, r.raw, r.headers["Content-Type"]), (403, SIP_REFUSAL, "application/json"), path)
            self.assertEqual(list(r.json), ["message"])                     # no `code`
        for path in ("/v2/stocks/quotes/latest?symbols=AAPL,SPY&feed=iex", "/v2/stocks/quotes/latest?symbols=AAPL,SPY"):    # no feed: IEX answers
            r = self.get(path)
            self.assertEqual((r.status, list(r.json), list(r.json["quotes"])), (200, ["quotes"], ["AAPL", "SPY"]))
            for q in r.json["quotes"].values():
                self.assertEqual(list(q), STOCK_QUOTE_KEYS)
                self.assertTrue(0 < q["bp"] < q["ap"] and q["bx"] == "V" and q["c"] == ["R"] and STAMP.match(q["t"]), q)
            self.assertEqual((r.headers["X-Ratelimit-Limit"], r.headers["Content-Type"]), ("200", "application/json"))
        # a symbol the feed does not know is left out, silently; so is the OTC share
        r = self.get("/v2/stocks/quotes/latest?symbols=NOTASYMBOL,AAPL,PMNT,BTC/USD&feed=iex")
        self.assertEqual((r.status, list(r.json["quotes"])), (200, ["AAPL"]))

    def test_single_symbol_forms_and_snapshots(self):
        r = self.get("/v2/stocks/AAPL/quotes/latest?feed=iex")
        self.assertEqual((r.status, list(r.json), list(r.json["quote"]), r.json["symbol"]), (200, ["quote", "symbol"], STOCK_QUOTE_KEYS, "AAPL"))
        r = self.get("/v2/stocks/AAPL/trades/latest?feed=iex")
        self.assertEqual((r.status, list(r.json), list(r.json["trade"])), (200, ["symbol", "trade"], STOCK_TRADE_KEYS))
        r = self.get("/v2/stocks/AAPL/snapshot?feed=iex")
        self.assertEqual((r.status, list(r.json)), (200, SNAPSHOT_KEYS + ["symbol"]))
        r = self.get("/v2/stocks/AAPL/bars?timeframe=1Min&limit=2&feed=iex&start=%s&end=%s" % (yesterday("15:00:00"), yesterday("15:10:00")))
        self.assertEqual((r.status, list(r.json), len(r.json["bars"]), r.json["symbol"]), (200, ["bars", "next_page_token", "symbol"], 2, "AAPL"))
        self.assertEqual(base64.b64decode(r.json["next_page_token"]).decode(),
                         "AAPL|M|%d" % fake_alpaca.parse_time(r.json["bars"][1]["t"]))     # as seen: SYMBOL|M|ns of the last bar sent
        r = self.get("/v2/stocks/bars/latest?symbols=AAPL&feed=iex")
        self.assertEqual((r.status, list(r.json["bars"]), list(r.json["bars"]["AAPL"])), (200, ["AAPL"], BAR_KEYS))
        self.assertEqual(self.get("/v2/stocks/NOTASYMBOL/quotes/latest?feed=iex").status, 404)
        # snapshots, keyed by symbol at the top level; trade, quote and bars come from one path, so they agree
        r = self.get("/v2/stocks/snapshots?symbols=AAPL,MSFT,SPY&feed=iex")
        self.assertEqual((r.status, list(r.json)), (200, ["AAPL", "MSFT", "SPY"]))
        for s in r.json.values():
            self.assertEqual(list(s), SNAPSHOT_KEYS)
            q, t, m, d, p = (s[k] for k in ("latestQuote", "latestTrade", "minuteBar", "dailyBar", "prevDailyBar"))
            self.assertEqual((list(t), list(m), list(d), list(p)), (STOCK_TRADE_KEYS, BAR_KEYS, BAR_KEYS, BAR_KEYS))
            self.assertTrue(q["bp"] < q["ap"])
            self.assertTrue(m["l"] <= t["p"] <= m["h"], (t, m))               # the trade lies inside its minute's bar
            self.assertEqual(m["t"], t["t"][:17] + "00Z")
            if m["t"] >= d["t"]:
                self.assertTrue(d["l"] <= m["l"] and m["h"] <= d["h"], (m, d))    # and the minute inside the day's
            self.assertTrue(m["l"] <= m["o"] <= m["h"] and m["l"] <= m["c"] <= m["h"])
            self.assertLess(p["t"], d["t"])
            self.assertRegex(d["t"], r"T0[45]:00:00Z$")                       # a share's day bar starts at New York midnight

    def test_bars_timeframes_are_aggregated_from_one_path(self):
        span = "&start=%s&end=%s" % (yesterday("15:00:00"), yesterday("15:59:59"))
        ones = self.bars("AAPL", "1Min", span)["bars"]["AAPL"]
        self.assertEqual(len(ones), 60)
        self.assertEqual([b["t"][14:16] for b in ones], ["%02d" % i for i in range(60)])
        for b in ones:
            self.assertEqual(list(b), BAR_KEYS)
            # (vw is printed with six decimals, low and high with the share's two: a flat minute at 349.6195 prints 349.62
            # with a vw of 349.619497, half a cent under it. Met on the data of 2026-11-09, which this test reads all of
            # 2026-11-11 local time.)
            self.assertTrue(b["l"] <= min(b["o"], b["c"]) and max(b["o"], b["c"]) <= b["h"]
                            and b["l"] - 0.005 <= b["vw"] <= b["h"] + 0.005 and b["v"] > 0 and b["n"] > 0, b)

        def same(big, small):
            self.assertEqual((big["o"], big["c"], big["h"], big["l"], big["v"], big["n"], big["t"]),
                             (small[0]["o"], small[-1]["c"], max(b["h"] for b in small), min(b["l"] for b in small), sum(b["v"] for b in small),
                              sum(b["n"] for b in small), small[0]["t"]))
            self.assertAlmostEqual(big["vw"], sum(b["vw"] * b["v"] for b in small) / sum(b["v"] for b in small), places=4)
        fives = self.bars("AAPL", "5Min", span)["bars"]["AAPL"]
        self.assertEqual(len(fives), 12)
        for i, five in enumerate(fives):
            same(five, ones[i * 5:i * 5 + 5])
        hour = self.bars("AAPL", "1Hour", span)["bars"]["AAPL"]
        self.assertEqual(len(hour), 1)
        same(hour[0], ones)
        self.assertEqual(self.bars("AAPL", "5T", span), self.bars("AAPL", "5Min", span))       # the short spellings
        self.assertEqual(self.bars("AAPL", "1H", span), self.bars("AAPL", "1Hour", span))
        sevens = self.bars("AAPL", "7Min", "&limit=3&start=" + yesterday("15:00:00"))["bars"]["AAPL"]
        self.assertEqual([b["t"][11:16] for b in sevens], ["15:03", "15:10", "15:17"])         # counted from 00:00 UTC, as seen
        # the same request twice, and from another server with the same seed, gives the same bars; another seed does not
        self.assertEqual(self.bars("AAPL", "1Min", span)["bars"]["AAPL"], ones)
        for seed, equal in ((7, True), (8, False)):
            with FakeAlpaca(key_id=KEY, secret=SECRET, seed=seed) as other:
                conn = http.client.HTTPConnection("127.0.0.1", other.port, timeout=10)
                conn.request("GET", "/v2/stocks/bars?symbols=AAPL&timeframe=1Min&feed=iex" + span, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET})
                self.assertEqual(json.loads(conn.getresponse().read())["bars"]["AAPL"] == ones, equal)
                conn.close()

    def test_bars_limit_sort_and_page_token(self):
        span = "&start=%s&end=%s" % (yesterday("15:00:00"), yesterday("15:59:59"))
        ones = self.bars("AAPL", "1Min", span)
        self.assertIsNone(ones["next_page_token"])
        ones = ones["bars"]["AAPL"]
        self.assertEqual(self.bars("AAPL", "1Min", span + "&sort=desc&limit=3")["bars"]["AAPL"], ones[:-4:-1])
        pages, token = [], ""
        for expect in (25, 25, 10):
            page = self.bars("AAPL", "1Min", span + "&limit=25" + token)
            self.assertEqual(len(page["bars"]["AAPL"]), expect)
            pages += page["bars"]["AAPL"]
            token = "&page_token=%s" % page["next_page_token"]
        self.assertEqual((pages, page["next_page_token"]), (ones, None))
        # several symbols: symbol by symbol, `limit` bars in all (seen: AAPL first, then the rest of AAPL and MSFT)
        first = self.bars("MSFT,AAPL", "1Min", span + "&limit=70")
        self.assertEqual({k: len(v) for k, v in first["bars"].items()}, {"AAPL": 60, "MSFT": 10})
        self.assertTrue(base64.b64decode(first["next_page_token"]).decode().startswith("MSFT|M|"))
        second = self.bars("MSFT,AAPL", "1Min", span + "&limit=70&page_token=" + first["next_page_token"])
        self.assertEqual(({k: len(v) for k, v in second["bars"].items()}, second["next_page_token"]), ({"MSFT": 50}, None))
        self.assertEqual(first["bars"]["MSFT"][-1]["t"][14:16] + second["bars"]["MSFT"][0]["t"][14:16], "0910")
        back = self.bars("AAPL", "1Min", span + "&sort=desc&limit=40")
        rest = self.bars("AAPL", "1Min", span + "&sort=desc&limit=40&page_token=" + back["next_page_token"])
        self.assertEqual(back["bars"]["AAPL"] + rest["bars"]["AAPL"], ones[::-1])

    def test_every_documented_timeframe_and_the_refusals(self):
        start = "&limit=3&start=2024-01-01"                                   # the walk begins there
        for tf in ("1Min", "59Min", "1T", "1Hour", "23Hour", "1H", "1Day", "1D", "1Week", "1W", "1Month", "2Month", "3Month", "4Month", "6Month", "12Month", "1M"):
            got = self.bars("AAPL", tf, start)["bars"]["AAPL"]
            self.assertEqual(len(got), 3, tf)
        self.assertTrue(all(re.search(r"T0[45]:00:00Z$", b["t"]) for b in self.bars("AAPL", "1Day", start)["bars"]["AAPL"]))
        weeks = self.bars("AAPL", "1Week", start)["bars"]["AAPL"]
        self.assertTrue(all(fake_alpaca.ny_date(fake_alpaca.parse_time(b["t"]) // 10 ** 9).weekday() == 0 for b in weeks), weeks)   # Mondays
        months = self.bars("AAPL", "1Month", start)["bars"]["AAPL"]
        self.assertTrue(all(fake_alpaca.ny_date(fake_alpaca.parse_time(b["t"]) // 10 ** 9).day == 1 for b in months), months)
        days = self.bars("AAPL", "1Day", "&start=2024-03-01T05:00:00Z&end=2024-03-31T05:00:00Z")["bars"]["AAPL"]       # a month is its days
        march = self.bars("AAPL", "1Month", "&limit=1&start=2024-03-01")["bars"]["AAPL"][0]
        self.assertEqual((len(days), march["t"], march["o"], march["c"], march["h"], march["l"], march["v"]),
                         (31, days[0]["t"], days[0]["o"], days[-1]["c"], max(b["h"] for b in days), min(b["l"] for b in days), sum(b["v"] for b in days)))
        for tf in ("7Foo", "60Min", "24Hour", "2Day", "5Month", "0Min"):
            self.assertEqual(self.bars("AAPL", tf, status=400), {"message": "invalid timeframe: " + tf})
        for extra, message in (("&limit=0", "invalid limit: should be positive"), ("&limit=10001", "invalid limit: larger than the allowed maximum of 10000"),
                               ("&sort=bogus", "invalid sort"), ("&feed=nope", "invalid feed: nope")):
            r = self.get("/v2/stocks/bars?symbols=AAPL&timeframe=1Min" + extra)
            self.assertEqual((r.status, r.raw), (400, json.dumps({"message": message}, separators=(",", ":")).encode() + b"\n"))
        r = self.get("/v2/stocks/bars?timeframe=1Min&feed=iex")
        self.assertEqual((r.status, r.json), (400, {"message": "Invalid format for parameter symbols: query parameter 'symbols' is required"}))
        r = self.get("/v2/stocks/bars?symbols=AAPL&timeframe=1Min&feed=iex&start=yesterday")
        self.assertEqual(r.status, 400)
        self.assertTrue(r.json["message"].startswith("Invalid format for parameter start: error parsing 'yesterday' as RFC3339 or 2006-01-02 time"))

    def test_bars_with_no_feed_named_stop_fifteen_minutes_ago(self):
        newest = self.get("/v2/stocks/bars?symbols=AAPL&timeframe=1Min&limit=3&sort=desc").json["bars"]["AAPL"][0]["t"]
        age = time.time() - fake_alpaca.parse_time(newest) / 10 ** 9
        self.assertTrue(900 <= age < 962, age)                                 # the newest bar is exactly 15 minutes old (delayed SIP)
        newest = self.bars("AAPL", "1Min", "&limit=3&sort=desc")["bars"]["AAPL"][0]["t"]
        self.assertLess(time.time() - fake_alpaca.parse_time(newest) / 10 ** 9, 62)            # IEX is current

    def test_crypto_quotes_bars_and_no_key(self):
        r = self.get("/v1beta3/crypto/us/latest/quotes?symbols=BTC/USD,ETH/USD")
        self.assertEqual((r.status, list(r.json), list(r.json["quotes"])), (200, ["quotes"], ["BTC/USD", "ETH/USD"]))
        for q in r.json["quotes"].values():
            self.assertEqual(list(q), ["ap", "as", "bp", "bs", "t"])
            self.assertTrue(0 < q["bp"] < q["ap"] and q["as"] > 0 and q["bs"] > 0 and STAMP.match(q["t"]))
        self.assertEqual(self.get("/v1beta3/crypto/us/latest/quotes?symbols=BTC/USD", auth=False).status, 200)   # seen: public
        self.assertEqual(self.get("/v1beta3/crypto/us/latest/quotes?symbols=ZZZ/USD,AAPL").json, {"quotes": {}})
        got = self.bars("BTC/USD", "1Min", "&limit=2&sort=desc", path="/v1beta3/crypto/us/bars", feed="")
        self.assertEqual((list(got), len(got["bars"]["BTC/USD"]), list(got["bars"]["BTC/USD"][0])), (["bars", "next_page_token"], 2, BAR_KEYS))
        self.assertLess(time.time() - fake_alpaca.parse_time(got["bars"]["BTC/USD"][0]["t"]) / 10 ** 9, 62)           # current
        hour = self.bars("BTC/USD,ETH/USD", "1Min", "&start=%s&end=%s" % (yesterday("02:00:00"), yesterday("03:59:59")), path="/v1beta3/crypto/us/bars", feed="")["bars"]
        self.assertEqual({k: len(v) for k, v in hour.items()}, {"BTC/USD": 120, "ETH/USD": 120})
        empty = [b for b in hour["BTC/USD"] if b["n"] == 0]
        self.assertTrue(empty and all(b["v"] == 0 and b["l"] <= b["vw"] <= b["h"] for b in empty))      # seen: a bar can have n 0 and v 0
        days = self.bars("BTC/USD", "1Day", "&limit=3&start=" + (date.today() - timedelta(9)).isoformat(), path="/v1beta3/crypto/us/bars", feed="")["bars"]["BTC/USD"]
        self.assertTrue(all(b["t"].endswith("T00:00:00Z") for b in days), days)                           # crypto days start at 00:00 UTC
        r = self.get("/v1beta3/crypto/us/latest/trades?symbols=BTC/USD")
        self.assertEqual(list(r.json["trades"]["BTC/USD"]), ["i", "p", "s", "t", "tks"])
        r = self.get("/v1beta3/crypto/us/snapshots?symbols=BTC/USD")
        self.assertEqual((list(r.json), list(r.json["snapshots"]["BTC/USD"])), (["snapshots"], SNAPSHOT_KEYS))
        self.assertEqual(list(self.get("/v1beta3/crypto/us/latest/bars?symbols=ETH/USD").json["bars"]["ETH/USD"]), BAR_KEYS)

    def test_share_quotes_while_shut_are_stale_and_one_sided(self):
        self.fake.set_market("shut")
        q = self.get("/v2/stocks/quotes/latest?symbols=AAPL,SPY&feed=iex").json["quotes"]
        self.assertEqual([q["AAPL"][k] for k in ("ap", "as", "ax")], [0, 0, " "])     # seen: ask 0 means NO ask
        self.assertGreater(q["AAPL"]["bp"], 0)
        self.assertGreater(q["SPY"]["ap"], q["SPY"]["bp"] * 1.05)                     # seen: 727 by 771 is not a market
        time.sleep(0.05)
        again = self.get("/v2/stocks/quotes/latest?symbols=AAPL,SPY&feed=iex").json["quotes"]
        self.assertEqual(q, again)                                                    # and it does not move
        trade = self.get("/v2/stocks/trades/latest?symbols=AAPL&feed=iex").json["trades"]["AAPL"]
        self.assertLess(time.time() - fake_alpaca.parse_time(trade["t"]) / 10 ** 9, 3)    # trades still print (extended hours)
        self.assertGreater(self.get("/v1beta3/crypto/us/latest/quotes?symbols=BTC/USD").json["quotes"]["BTC/USD"]["ap"], 0)


class MarketDataOnThePaidPlan(Base):
    PLAN = "sip"

    def test_sip_is_served(self):
        r = self.get("/v2/stocks/quotes/latest?symbols=AAPL,SPY&feed=sip")
        self.assertEqual((r.status, list(r.json["quotes"])), (200, ["AAPL", "SPY"]))
        self.assertEqual(self.get("/v2/stocks/snapshots?symbols=AAPL&feed=sip").status, 200)
        newest = self.get("/v2/stocks/bars?symbols=AAPL&timeframe=1Min&limit=3&sort=desc").json["bars"]["AAPL"][0]["t"]
        self.assertLess(time.time() - fake_alpaca.parse_time(newest) / 10 ** 9, 62)     # no 15 minute delay


# ---------------------------------------------------------------------------------------------------------------
# Reading the trading API
# ---------------------------------------------------------------------------------------------------------------

class TradingReads(Base):
    def test_clock(self):
        r = self.get("/v2/clock")
        c = r.json
        self.assertEqual((r.status, list(c), c["is_open"]), (200, ["is_open", "next_close", "next_open", "timestamp"], True))
        self.assertRegex(c["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,9})?-0[45]:00$")     # New York offset, not Z
        self.assertRegex(c["next_open"], r"T09:30:00-0[45]:00$")
        self.assertRegex(c["next_close"], r"T1[36]:00:00-0[45]:00$")
        self.assertLess(fake_alpaca.parse_time(c["next_close"]), fake_alpaca.parse_time(c["next_open"]))
        self.assertEqual((r.headers["X-Ratelimit-Limit"], r.headers["Content-Type"]), ("1000", "application/json"))
        self.fake.set_market("shut")
        c = self.get("/v2/clock").json
        self.assertFalse(c["is_open"])
        self.assertLess(fake_alpaca.parse_time(c["next_open"]), fake_alpaca.parse_time(c["next_close"]))

    def test_calendar_has_holidays_and_early_closes(self):
        rows = self.get("/v2/calendar?start=2026-11-23&end=2026-12-28").json        # the real rows of these weeks were read
        self.assertEqual(list(rows[0]), ["close", "date", "open", "session_close", "session_open", "settlement_date"])
        by = {r["date"]: r for r in rows}
        self.assertEqual(len(rows), 24)
        self.assertNotIn("2026-11-26", by)                                           # Thanksgiving
        self.assertNotIn("2026-12-25", by)
        self.assertEqual(by["2026-11-25"], {"close": "16:00", "date": "2026-11-25", "open": "09:30", "session_close": "2000", "session_open": "0400", "settlement_date": "2026-11-27"})
        self.assertEqual(by["2026-11-27"], {"close": "13:00", "date": "2026-11-27", "open": "09:30", "session_close": "1700", "session_open": "0400", "settlement_date": "2026-11-30"})
        self.assertEqual(by["2026-12-24"], {"close": "13:00", "date": "2026-12-24", "open": "09:30", "session_close": "1700", "session_open": "0400", "settlement_date": "2026-12-28"})
        self.assertTrue(all(date.fromisoformat(r["date"]).weekday() < 5 for r in rows))
        # whenever this runs: the year ahead holds an early close and a weekday on which the market does not open
        today = date.today()
        ahead = self.get("/v2/calendar?start=%s&end=%s" % (today.isoformat(), (today + timedelta(366)).isoformat())).json
        self.assertTrue([r for r in ahead if r["close"] == "13:00"])
        open_days = {r["date"] for r in ahead}
        self.assertTrue([d for d in (today + timedelta(i) for i in range(366)) if d.weekday() < 5 and d.isoformat() not in open_days])

    def test_assets(self):
        r = self.get("/v2/assets?status=active")
        rows = {a["symbol"]: a for a in r.json}
        self.assertEqual(sorted(rows), ["AAPL", "BTC/USD", "DTI", "ETH/USD", "MSFT", "PMNT", "SNDL", "SPY", "XRX", "YI"])
        for a in r.json:
            self.assertEqual(list(a), COIN_ASSET_KEYS if a["class"] == "crypto" else SHARE_ASSET_KEYS)
        flags = {s: (a["tradable"], a["marginable"], a["shortable"], a["fractionable"]) for s, a in rows.items()}
        self.assertEqual(flags, {"AAPL": (True, True, True, True), "MSFT": (True, True, True, True), "SPY": (True, True, True, True),
                                 "SNDL": (True, True, True, True), "XRX": (True, True, False, True), "DTI": (True, True, True, False),
                                 "YI": (True, True, False, False), "PMNT": (False, False, False, False), "BTC/USD": (True, False, False, True),
                                 "ETH/USD": (True, False, False, True)})
        self.assertEqual(rows["AAPL"], {"id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "class": "us_equity", "exchange": "NASDAQ", "symbol": "AAPL",
                                        "name": "Apple Inc. Common Stock", "status": "active", "tradable": True, "marginable": True,
                                        "maintenance_margin_requirement": 30, "margin_requirement_long": "30", "margin_requirement_short": "30",
                                        "shortable": True, "easy_to_borrow": True, "borrow_status": "easy_to_borrow", "fractionable": True,
                                        "attributes": ["fractional_eh_enabled", "has_options", "overnight_tradable"]})
        self.assertEqual([rows["BTC/USD"][k] for k in ("exchange", "name", "min_order_size", "min_trade_increment", "price_increment", "maintenance_margin_requirement")],
                         ["CRYPTO", "Bitcoin  / US Dollar", "0.000013362", "0.000000001", "0.000000001", 100])
        self.assertEqual([len(self.get("/v2/assets?" + q).json) for q in ("asset_class=crypto", "asset_class=us_equity", "status=inactive", "asset_class=us_equity&exchange=ARCA,OTC",
                                                                         "attributes=options_late_close")], [2, 8, 0, 2, 1])
        for name in ("AAPL", "BTC%2FUSD", "BTCUSD", rows["BTC/USD"]["id"]):
            r = self.get("/v2/assets/" + name)
            self.assertEqual((r.status, r.json), (200, rows["AAPL" if name == "AAPL" else "BTC/USD"]))
        r = self.get("/v2/assets/NOTASYMBOL")
        self.assertEqual((r.status, r.json), (404, {"code": 40410000, "message": "asset not found for NOTASYMBOL"}))

    def test_the_share_under_one_dollar_takes_four_decimals(self):
        bid, ask = self.quote("SNDL")
        self.assertTrue(0 < bid < ask < 1, (bid, ask))
        self.assertTrue(-ask.as_tuple().exponent <= 4 and ask - bid < D("0.01"), (bid, ask))     # a tick finer than a cent
        self.order(symbol="SNDL", qty="10", side="buy", type="limit", limit_price="0.2512", time_in_force="day")
        self.refused(422, {"code": 42210000, "message": "invalid limit_price 0.25123. sub-penny increment does not fulfill minimum pricing criteria"},
                     symbol="SNDL", qty="10", side="buy", type="limit", limit_price="0.25123", time_in_force="day")

    def test_keys(self):
        for headers in ({}, {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": "wrong"}, {"APCA-API-KEY-ID": "PKOTHER", "APCA-API-SECRET-KEY": SECRET}):
            r = self.get("/v2/account", auth=False, headers=headers)
            self.assertEqual((r.status, r.raw, r.headers["Content-Type"]), (401, b'{"message": "unauthorized."}\n', "application/json"))
            self.assertEqual(r.headers["WWW-Authenticate"], 'Bearer, Basic realm="alpaca.markets", charset="UTF-8"')
            self.assertIsNone(r.headers["X-Ratelimit-Limit"])
            r = self.get("/v2/stocks/quotes/latest?symbols=AAPL&feed=iex", auth=False, headers=headers)     # the data host answers in HTML
            self.assertEqual((r.status, r.headers["Content-Type"]), (401, "text/html"))
            self.assertIn(b"<title>401 Authorization Required</title>", r.raw)
        self.assertEqual(self.get("/v2/account", auth=False, headers={"apca-api-key-id": KEY, "Apca-Api-Secret-Key": SECRET}).status, 200)
        basic = base64.b64encode(("%s:%s" % (KEY, SECRET)).encode()).decode()
        self.assertEqual(self.get("/v2/account", auth=False, headers={"Authorization": "Basic " + basic}).status, 200)
        self.assertEqual(self.call("POST", "/v2/orders", {"symbol": "AAPL", "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"}, auth=False).status, 401)
        self.assertEqual(self.get("/v2/orders?status=all").json, [])

    def test_paths_that_do_not_exist(self):
        r = self.get("/v2/nonexistent")
        self.assertEqual((r.status, r.json, r.headers["X-Ratelimit-Limit"]), (404, {"code": 40410000, "message": "endpoint not found"}, None))
        r = self.get("/v2/orders:by_client_order_id")
        self.assertEqual((r.status, r.json), (422, {"code": 40010001, "message": "client_order_id is missing"}))
        r = self.get("/v2/positions/MSFT")
        self.assertEqual((r.status, r.json), (404, {"code": 40410000, "message": "position does not exist"}))
        r = self.get("/v2/account/activities/NOPE")
        self.assertEqual((r.status, r.json), (422, {"code": 40010001, "message": "invalid activity type: NOPE"}))
        r = self.get("/v2/account/activities?page_size=101")
        self.assertEqual((r.status, r.json), (422, {"code": 40010001, "message": "tried to set the page size to 101, but the maximum is 100"}))

    def test_order_list_filters_and_activity_pages(self):
        self.fake.set_price("BTC/USD", price="74200")
        made = []
        for i in range(5):
            made.append(self.order(symbol="BTC/USD" if i % 2 == 0 else "ETH/USD", qty="0.01", side="buy", type="market", time_in_force="gtc", client_order_id="f-%d" % i))
            self.settled()
        rest = self.order(symbol="BTC/USD", qty="0.001", side="sell", type="limit", limit_price="99999", time_in_force="gtc", client_order_id="f-rest")
        self.settled()
        ids = lambda q: [o["client_order_id"] for o in self.get("/v2/orders?" + q).json]      # noqa: E731
        self.assertEqual(ids(""), ["f-rest"])                                          # open is the default
        self.assertEqual(ids("status=open"), ["f-rest"])
        self.assertEqual(ids("status=closed"), ["f-4", "f-3", "f-2", "f-1", "f-0"])     # newest first
        self.assertEqual(ids("status=all&direction=asc"), ["f-0", "f-1", "f-2", "f-3", "f-4", "f-rest"])
        self.assertEqual(ids("status=bogus"), ids("status=all"))                        # seen: not refused
        self.assertEqual(ids("status=all&limit=2"), ["f-rest", "f-4"])
        self.assertEqual(ids("status=all&symbols=BTC/USD"), ["f-rest", "f-4", "f-2", "f-0"])
        self.assertEqual(ids("status=all&symbols=BTCUSD"), [])                          # seen: the slash form is the one that matches
        self.assertEqual(ids("status=all&symbols=BTC/USD,ETH/USD&side=sell"), ["f-rest"])
        self.assertEqual((len(ids("status=all&asset_class=crypto")), ids("status=all&asset_class=us_equity")), (6, []))
        self.assertEqual(ids("status=all&after=%s&direction=asc" % made[2]["submitted_at"]), ["f-3", "f-4", "f-rest"])
        self.assertEqual(ids("status=all&until=%s" % made[2]["submitted_at"]), ["f-1", "f-0"])
        self.assertEqual(self.read(rest["id"])["position_intent"], "sell_to_close")
        # activities: page_size rows, then page_token = the id of the last row sent
        fills = self.get("/v2/account/activities/FILL").json
        self.assertEqual([a["order_id"] for a in fills], [o["id"] for o in reversed(made)])
        one = self.get("/v2/account/activities/FILL?page_size=2").json
        two = self.get("/v2/account/activities/FILL?page_size=2&page_token=" + one[-1]["id"]).json
        three = self.get("/v2/account/activities/FILL?page_size=2&page_token=" + two[-1]["id"]).json
        self.assertEqual(([len(p) for p in (one, two, three)], one + two + three), ([2, 2, 1], fills))
        self.assertEqual(self.get("/v2/account/activities/FILL?direction=asc").json, fills[::-1])
        self.assertEqual([a["activity_type"] for a in self.get("/v2/account/activities?activity_types=JNLC").json], ["JNLC"])
        self.assertEqual(len(self.get("/v2/account/activities?activity_types=FILL,JNLC").json), 6)
        self.assertEqual(len(self.get("/v2/account/activities?category=non_trade_activity").json), 1)
        self.assertEqual(len(self.get("/v2/account/activities?order_id=" + made[0]["id"]).json), 1)
        self.assertEqual(self.get("/v2/account/activities/FILL?after=" + fills[2]["transaction_time"]).json, fills[:2])

    def test_an_order_is_printed_two_ways(self):
        """Seen: fresh, nanosecond stamps and source null; later the same order with microsecond stamps and source access_key."""
        with FakeAlpaca(key_id=KEY, secret=SECRET, persist_after=0.3) as other:
            def call(method, path, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", other.port, timeout=10)
                conn.request(method, path, body=json.dumps(body) if body else None, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET})
                out = json.loads(conn.getresponse().read())
                conn.close()
                return out
            o = call("POST", "/v2/orders", {"symbol": "BTC/USD", "qty": "0.001", "side": "buy", "type": "market", "time_in_force": "gtc"})
            self.assertIsNone(o["source"])
            time.sleep(0.4)
            later = call("GET", "/v2/orders/" + o["id"])
            self.assertEqual(later["source"], "access_key")
            self.assertTrue(all(len(later[k].partition(".")[2]) <= 7 for k in ("created_at", "updated_at", "submitted_at", "filled_at")), later)
            self.assertLess(abs(fake_alpaca.parse_time(later["created_at"]) - fake_alpaca.parse_time(o["created_at"])), 1000)


# ---------------------------------------------------------------------------------------------------------------
# The order engine
# ---------------------------------------------------------------------------------------------------------------

class Engine(Base):
    def hold(self, price, symbol="AAPL"):
        self.fake.set_price(symbol, price=price, spread="0.02")              # bid = price - 0.01, ask = price + 0.01

    def events(self, order_id):
        return [e["data"]["event"] for e in self.call("GET", "/_control/events").json["events"] if e["data"]["order"]["id"] == order_id]

    def position(self, symbol):
        return {p["symbol"]: p for p in self.positions()}.get(symbol)

    def test_a_resting_limit_fills_at_the_quote_when_the_price_comes_to_it(self):
        self.hold("150")
        o = self.order(symbol="AAPL", qty="10", side="buy", type="limit", limit_price="149", time_in_force="day")
        self.assertEqual(self.settled(o["id"])["status"], "new")
        self.hold("149.5")                                                    # ask 149.51: not yet
        self.assertEqual(self.settled(o["id"])["status"], "new")
        self.hold("148.9")                                                    # ask 148.91: marketable, filled at the ask, not at the limit
        done = self.settled(o["id"])
        self.assertEqual([done[k] for k in ("status", "filled_qty", "filled_avg_price")], ["filled", "10", "148.91"])
        s = self.order(symbol="AAPL", qty="10", side="sell", type="limit", limit_price="151", time_in_force="gtc")
        self.assertEqual((s["position_intent"], self.settled(s["id"])["status"]), ("sell_to_close", "new"))
        self.assertEqual(self.position("AAPL")["qty_available"], "0")
        self.hold("151.2")
        self.assertEqual([self.settled(s["id"])[k] for k in ("status", "filled_avg_price")], ["filled", "151.19"])       # a sell fills at the bid
        self.assertEqual((self.positions(), self.account()["cash"]), ([], plain(D("100000") - D("1489.10") + D("1511.90"))))

    def test_stop_and_stop_limit_are_triggered_by_the_price(self):
        self.hold("150")
        self.order(symbol="AAPL", qty="5", side="buy", type="market", time_in_force="day")
        self.settled()
        stop = self.order(symbol="AAPL", qty="5", side="sell", type="stop", stop_price="148", time_in_force="gtc")
        self.hold("148.5")
        self.assertEqual(self.settled(stop["id"])["status"], "new")
        self.hold("147.9")
        self.assertEqual([self.settled(stop["id"])[k] for k in ("status", "filled_avg_price")], ["filled", "147.89"])
        self.assertEqual(self.events(stop["id"]), ["pending_new", "new", "fill"])
        sl = self.order(symbol="AAPL", qty="1", side="buy", type="stop_limit", stop_price="152", limit_price="152.5", time_in_force="gtc")
        self.hold("151")
        self.assertEqual(self.settled(sl["id"])["status"], "new")
        self.hold("153")                                                      # through the stop, but the ask is above the limit
        self.assertEqual(self.settled(sl["id"])["status"], "new")
        self.hold("152.3")                                                    # back under the limit: it is a limit order now
        self.assertEqual([self.settled(sl["id"])[k] for k in ("status", "filled_avg_price")], ["filled", "152.31"])

    def test_a_trailing_stop_follows_the_price_and_fires(self):
        self.hold("100")
        self.order(symbol="AAPL", qty="10", side="buy", type="market", time_in_force="day")
        self.settled()
        t = self.order(symbol="AAPL", qty="10", side="sell", type="trailing_stop", trail_percent="2", time_in_force="gtc")
        self.assertEqual([t[k] for k in ("status", "hwm", "stop_price")], ["new", "100", "98"])
        self.hold("105")
        self.assertEqual([self.settled(t["id"])[k] for k in ("status", "hwm", "stop_price")], ["new", "105", "102.9"])
        self.hold("103.5")
        self.assertEqual([self.settled(t["id"])[k] for k in ("status", "hwm")], ["new", "105"])
        self.hold("102.8")
        self.assertEqual([self.settled(t["id"])[k] for k in ("status", "filled_avg_price")], ["filled", "102.79"])
        by_price = self.order(symbol="AAPL", qty="1", side="buy", type="trailing_stop", trail_price="1.5", time_in_force="day")
        self.assertEqual([by_price[k] for k in ("hwm", "stop_price", "trail_price", "trail_percent")], ["102.8", "104.3", "1.5", None])

    def test_bracket_take_profit_cancels_the_stop_loss(self):
        self.hold("150")
        b = self.order(symbol="AAPL", qty="2", side="buy", type="limit", limit_price="150.5", time_in_force="gtc", order_class="bracket",
                       take_profit={"limit_price": "155"}, stop_loss={"stop_price": "145", "limit_price": "144.5"})
        tp, sl = b["legs"]
        self.assertEqual([(leg["status"], leg["type"], leg["group_role"]) for leg in (tp, sl)], [("held", "limit", "take_profit"), ("held", "stop_limit", "stop_loss")])
        got = self.settled(b["id"])
        self.assertEqual((got["status"], got["filled_avg_price"], got["legs"]), ("filled", "150.01", None))
        nested = self.read(b["id"], nested=True)
        self.assertEqual([leg["status"] for leg in nested["legs"]], ["new", "held"])       # the limit works; the stop waits, held
        self.assertEqual([self.position("AAPL")[k] for k in ("qty", "qty_available")], ["2", "0"])
        self.hold("155.5")
        self.settled()
        self.assertEqual([(self.read(leg["id"])["status"], self.read(leg["id"])["filled_avg_price"]) for leg in (tp, sl)], [("filled", "155.49"), ("canceled", None)])
        self.assertIsNone(self.position("AAPL"))
        self.assertEqual((self.events(b["id"]), self.events(tp["id"]), self.events(sl["id"])),
                         (["pending_new", "new", "fill"], ["held", "new", "fill"], ["held", "canceled"]))
        # seen (14:3x UTC): a leg's `held` event is on the stream BEFORE its entry's pending_new
        order_of = [e["data"]["order"]["id"] for e in self.call("GET", "/_control/events").json["events"]]
        self.assertLess(max(order_of.index(tp["id"]), order_of.index(sl["id"])), order_of.index(b["id"]))

    def test_bracket_stop_loss_cancels_the_take_profit_and_cancel_takes_the_legs(self):
        self.hold("150")
        b = self.order(symbol="AAPL", qty="2", side="buy", type="market", time_in_force="day", order_class="bracket",
                       take_profit={"limit_price": "155"}, stop_loss={"stop_price": "145"})
        tp, sl = b["legs"]
        self.settled()
        self.hold("144")
        self.settled()
        self.assertEqual([(self.read(leg["id"])["status"], self.read(leg["id"])["filled_avg_price"]) for leg in (tp, sl)], [("canceled", None), ("filled", "143.99")])
        self.assertEqual((self.events(tp["id"]), self.events(sl["id"])), (["held", "new", "canceled"], ["held", "new", "fill"]))
        self.assertIsNone(self.position("AAPL"))
        # cancelling an entry that has not filled takes its legs with it
        c = self.order(symbol="AAPL", qty="1", side="buy", type="limit", limit_price="120", time_in_force="gtc", order_class="bracket",
                       take_profit={"limit_price": "155"}, stop_loss={"stop_price": "110"})
        self.settled()
        self.assertEqual(self.call("DELETE", "/v2/orders/" + c["id"]).status, 204)
        self.settled()
        self.assertEqual([self.read(i)["status"] for i in (c["id"], c["legs"][0]["id"], c["legs"][1]["id"])], ["canceled"] * 3)
        # the documented rules of the classes are enforced (the wording of these refusals is the stand-in's own)
        for fields in (dict(order_class="bracket", take_profit={"limit_price": "155"}), dict(order_class="bracket", take_profit={"limit_price": "140"}, stop_loss={"stop_price": "145"}),
                       dict(order_class="oto"), dict(order_class="oco", type="market", take_profit={"limit_price": "155"}, stop_loss={"stop_price": "145"})):
            r = self.call("POST", "/v2/orders", dict(dict(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day"), **fields))
            self.assertEqual((r.status, r.json["code"]), (422, 42210000), fields)
        r = self.call("POST", "/v2/orders", dict(symbol="BTC/USD", qty="0.001", side="buy", type="market", time_in_force="gtc", order_class="bracket",
                                                  take_profit={"limit_price": "99999"}, stop_loss={"stop_price": "50000"}))
        self.assertEqual(r.status, 422)                                       # crypto takes simple orders only

    def test_oto_and_oco(self):
        self.hold("150")
        oto = self.order(symbol="AAPL", qty="3", side="buy", type="market", time_in_force="day", order_class="oto", stop_loss={"stop_price": "145"})
        self.assertEqual([(leg["group_role"], leg["status"]) for leg in oto["legs"]], [("stop_loss", "held")])
        self.settled()
        leg = oto["legs"][0]
        self.assertEqual(self.read(leg["id"])["status"], "new")               # alone, the stop goes to work once the entry has filled
        self.assertEqual(self.call("DELETE", "/v2/orders/" + leg["id"]).status, 204)
        self.settled()
        # oco: an exit pair on the position: the limit is the parent, the stop its held leg; one cancels the other
        oco = self.order(symbol="AAPL", qty="3", side="sell", type="limit", time_in_force="gtc", order_class="oco", take_profit={"limit_price": "155"},
                         stop_loss={"stop_price": "145"}, limit_price="155")
        self.assertEqual([oco[k] for k in ("order_class", "group_role", "limit_price", "position_intent")], ["oco", "take_profit", "155", "sell_to_close"])
        self.assertEqual([(leg["group_role"], leg["status"], leg["stop_price"], leg["side"]) for leg in oco["legs"]], [("stop_loss", "held", "145", "sell")])
        self.assertEqual(self.settled(oco["id"])["status"], "new")
        self.assertEqual(self.position("AAPL")["qty_available"], "0")
        self.hold("144.5")
        self.settled()
        self.assertEqual([self.read(i)["status"] for i in (oco["id"], oco["legs"][0]["id"])], ["canceled", "filled"])
        self.assertIsNone(self.position("AAPL"))

    def test_the_legs_of_a_filled_bracket_in_the_lists_and_under_cancel_all(self):
        # seen on paper 2026-11-10 17:47 UTC
        self.hold("150")
        b = self.order(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="gtc", order_class="bracket",
                       take_profit={"limit_price": "158"}, stop_loss={"stop_price": "142"})
        tp, sl = b["legs"]
        self.settled()
        self.assertEqual([self.read(i)["status"] for i in (b["id"], tp["id"], sl["id"])], ["filled", "new", "held"])
        for query in ("status=open&nested=true", "status=open&nested=false", "status=open&nested=true&symbols=AAPL"):
            rows = self.get("/v2/orders?" + query).json       # one row: the working limit leg; the held stop is in no open list
            self.assertEqual([(o["id"], o["status"], o["legs"]) for o in rows], [(tp["id"], "new", None)], query)
        closed = self.get("/v2/orders?status=closed&nested=true&symbols=AAPL").json
        self.assertEqual([(o["id"], [(leg["id"], leg["status"]) for leg in o["legs"]]) for o in closed],
                         [(b["id"], [(tp["id"], "new"), (sl["id"], "held")])])
        self.assertEqual(self.position("AAPL")["qty_available"], "0")
        r = self.call("DELETE", "/v2/orders")                 # names the limit leg only; the held stop goes with it
        self.assertEqual((r.status, r.json), (207, [{"id": tp["id"], "status": 200}]))
        self.settled()
        self.assertEqual([self.read(i)["status"] for i in (tp["id"], sl["id"])], ["canceled", "canceled"])
        self.assertEqual(self.position("AAPL")["qty_available"], "1")
        # the other two shapes, as seen at 13:06 and 14:38: a held leg shows only under its own LISTED parent, nested
        e = self.order(symbol="AAPL", qty="1", side="buy", type="limit", limit_price="120", time_in_force="gtc", order_class="bracket",
                       take_profit={"limit_price": "158"}, stop_loss={"stop_price": "110"})
        self.settled()
        rows = [o for o in self.get("/v2/orders?status=open&nested=true").json]
        self.assertEqual([(o["id"], [leg["status"] for leg in o["legs"]]) for o in rows], [(e["id"], ["held", "held"])])
        self.assertEqual([o["id"] for o in self.get("/v2/orders?status=open&nested=false").json], [e["id"]])
        self.call("DELETE", "/v2/orders/" + e["id"])
        self.settled()
        oco = self.order(symbol="AAPL", qty="1", side="sell", type="limit", time_in_force="gtc", order_class="oco", limit_price="158",
                         take_profit={"limit_price": "158"}, stop_loss={"stop_price": "142"})
        self.settled()
        self.assertEqual([(o["id"], [leg["id"] for leg in o["legs"]]) for o in self.get("/v2/orders?status=open&nested=true").json],
                         [(oco["id"], [oco["legs"][0]["id"]])])
        self.assertEqual([(o["id"], o["legs"]) for o in self.get("/v2/orders?status=open").json], [(oco["id"], None)])

    def test_a_covering_buy_is_refused_while_a_buy_stop_holds_the_short(self):
        # seen on paper 2026-11-10 17:47 UTC
        self.hold("150")
        refusal = (403, {"code": 40310000, "message": "insufficient qty available for order (requested: 1, available: 0)"})
        cover = dict(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day")
        self.order(symbol="AAPL", qty="1", side="sell", type="market", time_in_force="gtc", order_class="oto", stop_loss={"stop_price": "158"})
        self.settled()
        self.assertEqual([self.position("AAPL")[k] for k in ("side", "qty", "qty_available")], ["short", "-1", "0"])
        r = self.call("POST", "/v2/orders", cover)            # (a) the stop is the leg of an OTO entry
        self.assertEqual((r.status, r.json), refusal)
        self.call("DELETE", "/v2/orders")
        self.settled()
        self.order(symbol="AAPL", qty="1", side="buy", type="stop", stop_price="158", time_in_force="gtc")
        self.settled()
        r = self.call("POST", "/v2/orders", cover)            # (b) the stop is a plain stop order
        self.assertEqual((r.status, r.json), refusal)
        self.call("DELETE", "/v2/orders")
        self.settled()
        self.order(**cover)                                   # nothing holds the short now: the cover is taken
        self.settled()
        self.assertIsNone(self.position("AAPL"))

    def test_a_fills_id_in_the_ledger_is_its_execution_id_on_the_stream(self):
        # seen: `20261110082455920::513b607c-...` and "execution_id":"513b607c-..."
        self.hold("150")
        self.call("POST", "/_control/fills", {"clips": 2, "interval_s": 0.05})
        o = self.order(symbol="AAPL", qty="4", side="buy", type="market", time_in_force="day")
        self.settled(o["id"])
        on_stream = [e["data"]["execution_id"] for e in self.call("GET", "/_control/events").json["events"]
                     if e["data"]["event"] in ("partial_fill", "fill")]
        in_ledger = [a["id"].split("::")[1] for a in self.get("/v2/account/activities/FILL?direction=asc").json]
        self.assertEqual((len(on_stream), on_stream), (2, in_ledger))

    def test_the_next_order_fills_in_partial_clips(self):
        self.hold("150")
        r = self.call("POST", "/_control/fills", {"clips": 3, "interval_s": 0.15})
        self.assertEqual((r.status, r.json["next_clips"]), (200, [3, 0.15]))
        o = self.order(symbol="AAPL", qty="9", side="buy", type="market", time_in_force="day")
        seen = self.until(lambda: self.read(o["id"])["status"] == "partially_filled" and self.read(o["id"]))
        self.assertIn(seen["filled_qty"], ("3", "6"))
        self.assertIsNone(seen["filled_at"])
        self.assertEqual([self.settled(o["id"])[k] for k in ("status", "filled_qty", "filled_avg_price")], ["filled", "9", "150.01"])
        events = [e["data"] for e in self.call("GET", "/_control/events").json["events"]]
        self.assertEqual([e["event"] for e in events], ["pending_new", "new", "partial_fill", "partial_fill", "fill"])
        self.assertEqual([(e["qty"], e["position_qty"], e["order"]["filled_qty"], e["order"]["status"]) for e in events[2:]],
                         [("3", "3", "3", "partially_filled"), ("3", "6", "6", "partially_filled"), ("3", "9", "9", "filled")])
        self.assertEqual(list(events[2]), FILL_EVENT_KEYS)
        fills = self.get("/v2/account/activities/FILL?direction=asc").json
        self.assertEqual([(a["type"], a["qty"], a["cum_qty"], a["leaves_qty"], a["order_status"]) for a in fills],
                         [("partial_fill", "3", "3", "6", "partially_filled"), ("partial_fill", "3", "6", "3", "partially_filled"), ("fill", "3", "9", "0", "filled")])
        again = self.order(symbol="AAPL", qty="9", side="buy", type="market", time_in_force="day")       # one shot: the order after fills whole
        self.settled()
        self.assertEqual(self.events(again["id"]), ["pending_new", "new", "fill"])

    def test_fills_can_be_delayed(self):
        self.hold("150")
        self.assertEqual(self.call("POST", "/_control/fills", {"delay_s": 0.6}).json["fill_delay_s"], 0.6)
        began = time.time()
        o = self.order(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day")
        time.sleep(0.15)
        self.assertEqual(self.read(o["id"])["status"], "new")
        self.until(lambda: self.read(o["id"])["status"] == "filled")
        self.assertGreaterEqual(time.time() - began, 0.58)
        self.call("POST", "/_control/fills", {"delay_s": 0})
        o = self.order(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day")
        self.assertEqual(self.settled(o["id"])["status"], "filled")

    def test_short_and_cover(self):
        self.hold("150")
        s = self.order(symbol="AAPL", qty="4", side="sell", type="market", time_in_force="day")
        self.assertEqual(s["position_intent"], "sell_to_open")
        self.assertEqual(self.settled(s["id"])["filled_avg_price"], "149.99")
        p = self.position("AAPL")
        self.assertEqual([p[k] for k in ("qty", "side", "avg_entry_price", "market_value", "cost_basis")], ["-4", "short", "149.99", "-600", "-599.96"])
        a = self.account()
        self.assertEqual([a[k] for k in ("cash", "short_market_value", "long_market_value", "equity")], ["100599.96", "-600", "0", "99999.96"])
        c = self.order(symbol="AAPL", qty="4", side="buy", type="market", time_in_force="day")
        self.assertEqual(c["position_intent"], "buy_to_close")
        self.settled()
        self.assertEqual((self.positions(), self.account()["cash"]), ([], "99999.92"))
        self.refused(422, {"code": 42210000, "message": 'asset "XRX" cannot be sold short'}, symbol="XRX", qty="1", side="sell", type="market", time_in_force="day")
        self.refused(403, {"code": 40310000, "message": 'asset "DTI" is not fractionable'}, symbol="DTI", qty="1.5", side="buy", type="market", time_in_force="day")

    def test_a_day_order_expires_at_the_close_and_gtc_stays(self):
        self.hold("150")
        day = self.order(symbol="AAPL", qty="1", side="buy", type="limit", limit_price="1", time_in_force="day")
        gtc = self.order(symbol="AAPL", qty="1", side="buy", type="limit", limit_price="1", time_in_force="gtc")
        self.settled()
        wait = (fake_alpaca.parse_time(day["expires_at"]) - fake_alpaca.parse_time(day["created_at"])) / 10 ** 9 + 60
        r = self.call("POST", "/_control/clock", {"advance_s": wait})
        self.assertEqual(r.status, 200)
        self.settled()
        gone, kept = self.read(day["id"]), self.read(gtc["id"])
        self.assertEqual((gone["status"], kept["status"]), ("expired", "new"))
        self.assertGreaterEqual(fake_alpaca.parse_time(gone["expired_at"]), fake_alpaca.parse_time(day["expires_at"]))
        self.assertEqual(self.events(day["id"]), ["pending_new", "new", "expired"])
        self.assertEqual(self.account()["buying_power"], "399999")             # only the gtc order still reserves

    def test_the_calendar_opens_the_market_in_auto(self):
        self.fake.set_market("auto")
        saturday = date.today() + timedelta((5 - date.today().weekday()) % 7 + 7)
        self.assertEqual(self.call("POST", "/_control/clock", {"now": saturday.isoformat() + "T15:00:00Z"}).status, 200)
        c = self.get("/v2/clock").json
        self.assertEqual((c["is_open"], c["timestamp"][:10]), (False, saturday.isoformat()))
        self.hold("150")
        o = self.order(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day")
        self.assertEqual(self.settled(o["id"])["status"], "new")              # queued over the weekend
        opens = fake_alpaca.parse_time(c["next_open"])
        self.assertEqual(self.call("POST", "/_control/clock", {"now": fake_alpaca.ts(opens + 90 * 10 ** 9)}).status, 200)
        done = self.settled(o["id"])
        self.assertEqual((done["status"], self.get("/v2/clock").json["is_open"]), ("filled", True))
        self.assertGreaterEqual(fake_alpaca.parse_time(done["filled_at"]), opens)
        self.assertEqual(self.call("POST", "/_control/clock", {"advance_s": -5}).status, 400)      # the clock does not go back

    def test_close_part_of_a_position_and_all_of_them(self):
        self.hold("150")
        self.fake.set_price("ETH/USD", price="2000")
        self.order(symbol="AAPL", qty="10", side="buy", type="market", time_in_force="day")
        self.order(symbol="ETH/USD", qty="0.01", side="buy", type="market", time_in_force="gtc")
        self.settled()
        r = self.call("DELETE", "/v2/positions/AAPL?qty=4")
        self.assertEqual((r.status, [r.json[k] for k in ("qty", "side", "type", "time_in_force", "position_intent")]), (200, ["4", "sell", "market", "day", "sell_to_close"]))
        self.settled()
        self.assertEqual(self.position("AAPL")["qty"], "6")
        r = self.call("DELETE", "/v2/positions/AAPL?percentage=50")
        self.assertEqual((r.status, r.json["qty"]), (200, "3"))
        self.settled()
        rest = self.order(symbol="AAPL", qty="3", side="sell", type="limit", limit_price="999", time_in_force="gtc")
        self.settled()
        self.assertEqual([self.position("AAPL")[k] for k in ("qty", "qty_available")], ["3", "0"])
        r = self.call("DELETE", "/v2/positions/AAPL")                         # documented: shares held by an open order cannot be closed
        self.assertEqual((r.status, r.json), (403, {"available": "0", "code": 40310000, "existing_qty": "3", "held_for_orders": "3",
                                                    "message": "insufficient qty available for order (requested: 3, available: 0)", "symbol": "AAPL"}))
        r = self.call("DELETE", "/v2/positions?cancel_orders=maybe")
        self.assertEqual((r.status, r.json), (422, {"code": 40010001, "message": "cancel_orders must be true or false"}))
        r = self.call("DELETE", "/v2/positions?cancel_orders=true")
        self.assertEqual((r.status, [(row["symbol"], row["status"], row["body"]["qty"], row["body"]["side"]) for row in r.json]),
                         (207, [("AAPL", 200, "3", "sell"), ("ETHUSD", 200, "0.009975", "sell")]))
        self.assertEqual(list(r.json[0]), ["symbol", "status", "body"])
        self.settled()
        self.assertEqual((self.positions(), self.read(rest["id"])["status"]), ([], "canceled"))
        self.assertEqual(self.call("DELETE", "/v2/positions/MSFT").status, 404)

    def test_what_cannot_fill_at_once_is_cancelled_for_ioc(self):
        self.fake.set_price("BTC/USD", price="74200")
        o = self.order(symbol="BTC/USD", qty="0.001", side="buy", type="limit", limit_price="60000", time_in_force="ioc")
        self.assertEqual(self.settled(o["id"])["status"], "canceled")
        o = self.order(symbol="BTC/USD", qty="0.001", side="buy", type="limit", limit_price="80000", time_in_force="ioc")
        self.assertEqual(self.settled(o["id"])["status"], "filled")

    def test_limit_orders_do_not_cross_into_a_wash_trade_unless_their_prices_meet(self):
        self.hold("150")
        self.order(symbol="AAPL", qty="5", side="buy", type="market", time_in_force="day")
        self.settled()
        self.order(symbol="AAPL", qty="1", side="buy", type="limit", limit_price="140", time_in_force="gtc")
        ok = self.order(symbol="AAPL", qty="1", side="sell", type="limit", limit_price="160", time_in_force="gtc")     # 140 < 160: allowed
        self.assertEqual(self.settled(ok["id"])["status"], "new")
        r = self.call("POST", "/v2/orders", dict(symbol="AAPL", qty="1", side="sell", type="limit", limit_price="139", time_in_force="gtc"))
        self.assertEqual((r.status, r.json["message"]), (403, "potential wash trade detected. use complex orders"))
        r = self.call("POST", "/v2/orders", dict(symbol="AAPL", qty="1", side="sell", type="market", time_in_force="day"))
        self.assertEqual((r.status, r.json["reject_reason"]), (403, "opposite side market/stop order exists"))
        self.order(symbol="AAPL", qty="1", side="sell", type="trailing_stop", trail_percent="3", time_in_force="gtc")   # exempt


# ---------------------------------------------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------------------------------------------

class Controls(Base):
    def control(self, name, body=None, expect=200):
        r = self.call("POST", "/_control/" + name, body if body is not None else {}, auth=False)
        self.assertEqual(r.status, expect, r.raw)
        return r.json

    def mid(self, symbol="BTC/USD"):
        bid, ask = self.quote(symbol)
        return (bid + ask) / 2

    def test_price_set_jump_trend_and_walk(self):
        self.assertEqual(self.control("price", {"symbol": "BTC/USD", "price": 50000})["mode"], "hold")
        self.assertEqual(self.quote("BTC/USD"), (D("49995.0"), D("50005.0")))          # the symbol's own spread around the price
        time.sleep(0.1)
        self.assertEqual(self.mid(), 50000)                                           # held
        self.control("price", {"symbol": "BTC/USD", "jump_pct": 10})
        self.assertEqual(self.mid(), 55000)
        self.control("price", {"symbol": "BTC/USD", "price": "50000", "spread": "10"})
        self.assertEqual(self.quote("BTC/USD"), (D("49995.0"), D("50005.0")))
        began = fake_alpaca.ts((time.time_ns() // 10 ** 9 - 60) * 10 ** 9)
        self.control("price", {"symbol": "BTC/USD", "trend_pct_per_s": 4, "seconds": 1})
        time.sleep(0.3)
        self.assertTrue(50000 < self.mid() < 52000)                                   # on its way
        time.sleep(0.9)
        self.assertEqual(self.mid(), 52000)                                           # 4% a second for 1 s, then held there
        bars = self.get("/v1beta3/crypto/us/bars?symbols=BTC/USD&timeframe=1Min&start=" + began).json["bars"]["BTC/USD"]
        self.assertTrue(min(b["l"] for b in bars) <= 50000 and max(b["h"] for b in bars) >= 55000 and bars[-1]["c"] == 52000, bars)   # the bars saw all of it
        self.assertEqual(self.control("price", {"symbol": "BTC/USD", "walk": True})["mode"], "walk")
        self.control("clock", {"advance_s": 3600})
        self.assertNotEqual(self.mid(), 52000)                                        # walking again, from where it was
        self.assertLess(abs(self.mid() - 52000), 5200)
        for bad in ({"symbol": "NOPE", "price": 1}, {"symbol": "AAPL", "price": -1}, {"symbol": "AAPL", "trend_pct_per_s": 1}, {"price": 5}):
            self.assertIn("error", self.control("price", bad, expect=400))

    def test_market_open_shut_auto(self):
        self.assertEqual(self.control("market", {"state": "shut"}), {"state": "shut", "is_open": False})
        self.assertFalse(self.get("/v2/clock").json["is_open"])
        self.assertEqual(self.control("market", {"state": "open"}), {"state": "open", "is_open": True})
        self.assertTrue(self.get("/v2/clock").json["is_open"])
        auto = self.control("market", {"state": "auto"})
        self.assertEqual(auto["is_open"], fake_alpaca.cal_is_open(int(time.time())))
        self.assertIn("error", self.control("market", {"state": "ajar"}, expect=400))

    def test_clock_moves_forward_and_resets(self):
        before = fake_alpaca.parse_time(self.get("/v2/clock").json["timestamp"])
        self.control("clock", {"advance_s": 3600})
        after = fake_alpaca.parse_time(self.get("/v2/clock").json["timestamp"])
        self.assertTrue(3600 <= (after - before) / 10 ** 9 < 3605)
        o = self.order(symbol="BTC/USD", qty="0.001", side="buy", type="market", time_in_force="gtc")
        self.assertGreater(fake_alpaca.parse_time(o["created_at"]), before + 3600 * 10 ** 9)
        self.control("clock", {"reset": True})
        self.assertLess(abs(fake_alpaca.parse_time(self.get("/v2/clock").json["timestamp"]) / 10 ** 9 - time.time()), 5)

    def test_drop_every_stream(self):
        one, two = Ws(self.fake.port).login(), Ws(self.fake.port).login()
        self.assertEqual(self.control("state")["listening"], 2)
        self.assertEqual(self.control("drop_streams"), {"dropped": 2})
        for ws in (one, two):
            with self.assertRaises((EOFError, ConnectionError)):             # the socket just ends: no close frame
                ws.frame(2)
            ws.close()
        again = Ws(self.fake.port).login()                                    # and a client may come back
        o = self.order(symbol="BTC/USD", qty="0.001", side="buy", type="market", time_in_force="gtc")
        self.assertEqual((again.event()["event"], again.event()["event"], again.event()["order"]["id"]), ("pending_new", "new", o["id"]))
        self.assertEqual(self.control("drop_streams", {"clean": True}), {"dropped": 1})
        self.assertEqual(again.frame(2)[0], 8)                                # clean: a close frame first
        again.close()

    def test_the_next_requests_fail_with_429_or_500(self):
        self.control("fail", {"status": 429, "count": 2, "retry_after": 3})
        for _ in range(2):
            r = self.get("/v2/account")
            self.assertEqual((r.status, r.json), (429, {"code": 42910000, "message": "rate limit exceeded"}))
            self.assertEqual((r.headers["Retry-After"], r.headers["X-Ratelimit-Remaining"], r.headers["X-Ratelimit-Limit"]), ("3", "0", "200"))
            self.assertGreaterEqual(int(r.headers["X-Ratelimit-Reset"]), int(time.time()) + 2)
        self.assertEqual(self.get("/v2/account").status, 200)
        self.control("fail", {"status": 500, "count": 1})
        r = self.get("/v2/stocks/quotes/latest?symbols=AAPL")
        self.assertEqual((r.status, r.json), (500, {"code": 50010000, "message": "internal server error occurred"}))
        self.assertEqual(self.get("/v2/stocks/quotes/latest?symbols=AAPL").status, 200)
        # only one path: the order call fails and makes no order; reads go through; the controls are never failed
        self.control("fail", {"status": 500, "count": 1, "path": "/v2/orders"})
        self.assertEqual(self.get("/v2/account").status, 200)
        self.assertEqual(len(self.control("state")["faults"]), 1)
        body = {"symbol": "BTC/USD", "qty": "0.001", "side": "buy", "type": "market", "time_in_force": "gtc", "client_order_id": "retry-me"}
        self.assertEqual(self.call("POST", "/v2/orders", body).status, 500)
        self.assertEqual(self.get("/v2/orders:by_client_order_id?client_order_id=retry-me").status, 404)
        self.assertEqual(self.call("POST", "/v2/orders", body).status, 200)
        self.assertIn("error", self.control("fail", {"status": 418}, expect=400))

    def test_the_allowance_is_real(self):
        r = self.get("/v2/account")
        self.assertEqual((r.headers["X-Ratelimit-Limit"], r.headers["X-Ratelimit-Remaining"]), ("200", "199"))
        self.assertLessEqual(abs(int(r.headers["X-Ratelimit-Reset"]) - time.time()), 2)
        with FakeAlpaca(key_id=KEY, secret=SECRET, rate_limit=20) as small:       # 20 a minute, so the test is quick: one more every 3 s
            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", small.port, timeout=10)
                conn.request("GET", path, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET})
                resp = conn.getresponse()
                out = (resp.status, resp.headers, json.loads(resp.read()))
                conn.close()
                return out
            left = [get("/v2/account")[1]["X-Ratelimit-Remaining"] for _ in range(20)]
            self.assertEqual(left, [str(n) for n in range(19, -1, -1)])
            status, headers, body = get("/v2/account")
            self.assertEqual((status, body), (429, {"code": 42910000, "message": "rate limit exceeded"}))
            self.assertEqual((headers["X-Ratelimit-Limit"], headers["X-Ratelimit-Remaining"]), ("20", "0"))
            self.assertTrue(time.time() <= int(headers["X-Ratelimit-Reset"]) + 1 <= time.time() + 5)
            self.assertEqual(get("/v2/stocks/quotes/latest?symbols=AAPL")[0], 200)        # the data API has its own allowance
            time.sleep(3.1)
            self.assertEqual(get("/v2/account")[0], 200)                                    # refilled as time passes

    def test_reset(self):
        ws = Ws(self.fake.port).login()
        self.fake.set_price("BTC/USD", price="50000")
        self.fake.set_market("shut")
        first = self.order(symbol="BTC/USD", qty="0.001", side="buy", type="market", time_in_force="gtc", client_order_id="once")
        self.order(symbol="AAPL", qty="1", side="buy", type="market", time_in_force="day")
        self.settled()
        account_id = self.account()["id"]
        self.control("fail", {"status": 500, "count": 5})
        self.control("fills", {"clips": 4, "delay_s": 2})
        self.control("clock", {"advance_s": 86400})
        self.settled()
        try:                                                                  # read what the stream has sent so far
            while True:
                ws.frame(0.3)
        except socket.timeout:
            pass
        self.assertEqual(self.control("reset"), {"reset": True})
        a = self.account()
        self.assertEqual([a[k] for k in ("cash", "equity", "buying_power", "initial_margin")], ["100000", "100000", "400000", "0"])
        self.assertEqual((self.positions(), self.get("/v2/orders?status=all").json), ([], []))
        self.assertEqual([x["activity_type"] for x in self.get("/v2/account/activities").json], ["JNLC"])
        state = self.control("state")
        self.assertEqual([state[k] for k in ("market", "is_open", "clock_offset_s", "fill_delay_s", "next_clips", "faults", "orders", "listening")],
                         ["open", True, 0.0, 0.0, None, [], 0, 1])
        self.assertNotEqual(self.quote("BTC/USD")[1], D("50005.0"))            # back on the walk
        o = self.order(symbol="BTC/USD", qty="0.001", side="buy", type="market", time_in_force="gtc", client_order_id="once")   # the id is free again
        self.assertEqual([ws.event()["order"]["id"] for _ in range(3)], [o["id"]] * 3)     # the stream stayed up
        self.assertNotEqual(o["id"], first["id"])                             # an id is never used twice, even across a reset
        self.assertEqual(self.account()["id"], account_id)
        ws.close()

    def test_request_log_counts_the_broker_calls(self):
        self.get("/v2/account", agent="my-adapter/9.9")
        self.call("POST", "/v2/orders", {"symbol": "BTC/USD", "qty": "0.001", "side": "buy", "type": "market", "time_in_force": "gtc"}, agent="my-adapter/9.9")
        self.get("/v1beta3/crypto/us/latest/quotes?symbols=BTC/USD", agent="my-adapter/9.9")
        self.get("/v2/account", auth=False, agent="stranger/1")
        ws = Ws(self.fake.port, agent="my-adapter-ws/9.9")
        self.control("settle")
        self.control("state")
        log = self.call("GET", "/_control/requests", auth=False).json["requests"]
        self.assertEqual([(e["method"], e["path"], e["query"], e["status"], e["user_agent"], e["service"]) for e in log],
                         [("GET", "/v2/account", "", 200, "my-adapter/9.9", "trading"), ("POST", "/v2/orders", "", 200, "my-adapter/9.9", "trading"),
                          ("GET", "/v1beta3/crypto/us/latest/quotes", "symbols=BTC/USD", 200, "my-adapter/9.9", "data"),
                          ("GET", "/v2/account", "", 401, "stranger/1", "trading"), ("GET", "/stream", "", 101, "my-adapter-ws/9.9", "stream")])
        self.assertEqual([e["seq"] for e in log], sorted(e["seq"] for e in log))
        self.assertEqual(len(self.call("GET", "/_control/requests?clear=1", auth=False).json["requests"]), 5)
        self.assertEqual(self.call("GET", "/_control/requests", auth=False).json["requests"], [])       # the controls themselves are not logged
        ws.close()

    def test_settle_state_and_unknown_controls(self):
        self.order(symbol="BTC/USD", qty="0.001", side="buy", type="market", time_in_force="gtc")
        self.assertEqual(self.control("settle", {"timeout_s": 3}), {"settled": True})
        state = self.control("state")
        self.assertEqual([state[k] for k in ("market", "plan", "orders", "live_orders", "positions")], ["open", "free", 1, 0, 1])
        self.assertEqual(sorted(state["prices"]), ["AAPL", "BTC/USD", "DTI", "ETH/USD", "MSFT", "PMNT", "SNDL", "SPY", "XRX", "YI"])
        self.assertEqual(len(self.call("GET", "/_control/events", auth=False).json["events"]), 3)
        self.assertIn("error", self.control("no_such_thing", expect=400))
        self.assertEqual(self.call("POST", "/_control/price", b"not json", auth=False).status, 400)


class CommandLine(unittest.TestCase):
    def test_it_runs_as_a_program_and_prints_its_port(self):
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_alpaca.py"), "--port", "0", "--plan", "sip", "--market", "shut", "--key-id", "PKCLI",
                                 "--secret", "cli-secret"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            info = json.loads(proc.stdout.readline())
            self.assertEqual([info[k] for k in ("key_id", "secret", "plan", "market")], ["PKCLI", "cli-secret", "sip", "shut"])
            self.assertEqual((info["trading_url"], info["data_url"], info["stream_url"]),
                             ("http://127.0.0.1:%d" % info["port"], "http://127.0.0.1:%d" % info["port"], "ws://127.0.0.1:%d/stream" % info["port"]))
            conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=10)
            headers = {"APCA-API-KEY-ID": "PKCLI", "APCA-API-SECRET-KEY": "cli-secret"}
            conn.request("GET", "/v2/clock", headers=headers)
            self.assertFalse(json.loads(conn.getresponse().read())["is_open"])
            conn.request("GET", "/v2/stocks/quotes/latest?symbols=AAPL&feed=sip", headers=headers)       # one connection, kept alive
            r = conn.getresponse()
            self.assertEqual((r.status, list(json.loads(r.read())["quotes"])), (200, ["AAPL"]))
            conn.close()
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
