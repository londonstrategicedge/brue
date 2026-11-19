"""Tests of tests/fake_capitalcom.py, the local stand-in for Capital.com's Public API v1.

Each test checks that the stand-in answers as Capital.com's documentation says, and cites where it says so:
  DOC      https://open-api.capital.com/                         the API reference (samples, limits, FAQ, changelog)
  POSTMAN  https://github.com/capital-com-sv/capital-api-postman  Capital.com's Postman collection, its saved answers
  MCP      https://github.com/capital-com-sv/capital-mcp          Capital.com's own client
  HEDGING  https://capital.com/en-gb/analysis/what-is-hedging-mode-and-how-to-use-it-in-trading
  UK_TERMS https://img.capital.com/docs/September-2024-CCUK-Terms-and-Conditions-_0.pdf
A test of a reading the documentation leaves open says "assumed, verify on demo" and also tests the control that plays
the other reading, so an adapter can be tried against both. A test of the stand-in's own machinery says "stand-in's own".
Loopback only; no key, no network.

Run: PYTHONPATH=. python3 -m unittest tests.test_fake_capitalcom -v     (or: python3 -m unittest discover -s tests)
"""
import base64
import calendar
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_capitalcom  # noqa: E402
from fake_capitalcom import FakeCapitalCom, RESOLUTIONS  # noqa: E402

DOC = "https://open-api.capital.com/"
POSTMAN = "https://github.com/capital-com-sv/capital-api-postman"
MCP = "https://github.com/capital-com-sv/capital-mcp"
HEDGING = "https://capital.com/en-gb/analysis/what-is-hedging-mode-and-how-to-use-it-in-trading"
UK_TERMS = "https://img.capital.com/docs/September-2024-CCUK-Terms-and-Conditions-_0.pdf"

STAMP_MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}$")
STAMP_S = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
DEAL_ID = re.compile(r"^[0-9a-f]{8}-0001-54c4-0000-[0-9a-f]{12}$")
ORDER_REF = re.compile(r"^o_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Field names, in the order Capital.com's samples print them (DOC)
LOGIN_KEYS = ["accountType", "accountInfo", "currencyIsoCode", "currencySymbol", "currentAccountId", "streamingHost", "accounts",
              "clientId", "timezoneOffset", "hasActiveDemoAccounts", "hasActiveLiveAccounts", "trailingStopsEnabled"]
LOGIN_ACCOUNT_KEYS = ["accountId", "accountName", "preferred", "accountType", "currency", "symbol", "balance"]
BALANCE_KEYS = ["balance", "deposit", "profitLoss", "available"]
ACCOUNT_KEYS = ["accountId", "accountName", "status", "accountType", "preferred", "balance", "currency", "symbol"]
SESSION_KEYS = ["clientId", "accountId", "timezoneOffset", "locale", "currency", "streamEndpoint"]
CONFIRM_KEYS = ["date", "status", "dealStatus", "epic", "dealReference", "dealId", "affectedDeals", "level", "size", "direction",
                "guaranteedStop", "trailingStop"]
POSITION_KEYS = ["contractSize", "createdDate", "createdDateUTC", "dealId", "dealReference", "workingOrderId", "size", "leverage",
                 "upl", "direction", "level", "currency", "guaranteedStop"]
MARKET_DEAL_KEYS = ["instrumentName", "expiry", "marketStatus", "epic", "symbol", "instrumentType", "lotSize", "high", "low",
                    "percentageChange", "netChange", "bid", "offer", "updateTime", "updateTimeUTC", "delayTime",
                    "streamingPricesAvailable", "scalingFactor", "marketModes"]
MARKET_LIST_KEYS = ["delayTime", "epic", "symbol", "netChange", "lotSize", "expiry", "instrumentType", "instrumentName", "high", "low",
                    "percentageChange", "updateTime", "updateTimeUTC", "bid", "offer", "streamingPricesAvailable", "marketStatus",
                    "scalingFactor", "marketModes", "pipPosition", "tickSize"]
INSTRUMENT_KEYS = ["epic", "symbol", "expiry", "name", "lotSize", "type", "guaranteedStopAllowed", "streamingPricesAvailable",
                   "currency", "marginFactor", "marginFactorUnit", "openingHours", "overnightFee"]
RULE_KEYS = ["minStepDistance", "minDealSize", "maxDealSize", "minSizeIncrement", "minGuaranteedStopDistance",
             "minStopOrProfitDistance", "maxStopOrProfitDistance", "marketOrderPreference", "trailingStopsPreference"]
SNAPSHOT_KEYS = ["marketStatus", "netChange", "percentageChange", "updateTime", "delayTime", "bid", "offer", "high", "low",
                 "decimalPlacesFactor", "scalingFactor", "marketModes"]
BAR_KEYS = ["snapshotTime", "snapshotTimeUTC", "openPrice", "closePrice", "highPrice", "lowPrice", "lastTradedVolume"]
ACTIVITY_KEYS = ["date", "dateUTC", "epic", "dealId", "source", "type", "status"]
DETAIL_KEYS = ["dealReference", "marketName", "currency", "size", "direction", "level", "guaranteedStop"]
TRANSACTION_KEYS = ["date", "dateUtc", "instrumentName", "transactionType", "note", "reference", "size", "currency", "status"]


class Answer:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw
        try:
            self.json = json.loads(raw) if raw else None
        except ValueError:
            self.json = None

    @property
    def code(self):
        return (self.json or {}).get("errorCode")


class Client:
    """The least REST client that talks to the stand-in, with the session headers once logged in."""

    def __init__(self, fake):
        self.fake, self.auth = fake, {}

    def call(self, method, path, body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=15)
        h = dict(self.auth) if auth else {}
        h.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            conn.request(method, "/api/v1" + path, body=data, headers=h)
            r = conn.getresponse()
            raw = r.read()
            return Answer(r.status, {k.lower(): v for k, v in r.getheaders()}, raw)
        finally:
            conn.close()

    def login(self, check=True):
        a = self.call("POST", "/session", {"identifier": self.fake.identifier, "password": self.fake.password, "encryptedPassword": False},
                      {"X-CAP-API-KEY": self.fake.api_key}, auth=False)
        if check:
            assert a.status == 200, (a.status, a.raw)
            self.auth = {"CST": a.headers["cst"], "X-SECURITY-TOKEN": a.headers["x-security-token"]}
        return a

    def get(self, path):
        return self.call("GET", path)


class Ws:
    """The least WebSocket client for /connect: masked text frames out, frames in."""

    def __init__(self, port, path="/connect"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, port, key)).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self._more()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        self.status = int(lines[0].split()[1])
        if self.status == 101:
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            assert ("sec-websocket-accept: " + accept).lower() in [line.lower() for line in lines], lines

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

    def send(self, obj):
        payload = json.dumps(obj).encode()
        mask, n = os.urandom(4), len(payload)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(c ^ mask[i & 3] for i, c in enumerate(payload)))

    def frame(self, timeout=3.0):
        self.sock.settimeout(timeout)
        b0, b1 = self._take(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._take(8))[0]
        assert not b1 & 0x80, "a server frame must not be masked"
        return b0 & 0x0F, self._take(n)

    def message(self, timeout=3.0):
        op, payload = self.frame(timeout)
        assert op == 1, "the stand-in sends JSON text frames, got opcode %d" % op
        return json.loads(payload.decode())

    def until(self, pred, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            m = self.message(max(0.05, end - time.time()))
            if pred(m):
                return m
        raise AssertionError("no such message within %.1fs" % timeout)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Base(unittest.TestCase):
    """One stand-in per class, put back as at the start before every test (reset), a fresh session each time."""
    options = dict(market="open", rate_per_s=100000, session_per_s=100000, order_spacing=0, confirm_delay=0, orders_per_hour=100000,
                   topups_per_s=100000, balances=(("USD", "10000"), ("GBP", "5000")))

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCapitalCom(**cls.options).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.c = Client(self.fake)
        self.c.login()

    # helpers
    def quote(self, epic):
        return self.fake.market.quote(epic)

    def open(self, epic="EURUSD", direction="BUY", size=1000, **kw):
        body = dict(epic=epic, direction=direction, size=size, **kw)
        a = self.c.call("POST", "/positions", body)
        self.assertEqual(a.status, 200, a.raw)
        ref = a.json["dealReference"]
        conf = self.c.get("/confirms/" + ref)
        self.assertEqual(conf.status, 200, conf.raw)
        return ref, conf.json

    def positions(self):
        return [p["position"] for p in self.c.get("/positions").json["positions"]]

    def order(self, epic="EURUSD", direction="BUY", size=1000, kind="LIMIT", level=None, **kw):
        if level is None:
            bid, ask = self.quote(epic)
            inst = self.fake.market.by_epic[epic]
            far = 200 * inst.tick
            below = (kind == "LIMIT") == (direction == "BUY")
            level = round((ask if direction == "BUY" else bid) + (-far if below else far), inst.dp)
        a = self.c.call("POST", "/workingorders", dict(epic=epic, direction=direction, size=size, type=kind, level=level, **kw))
        return a

    def orders(self):
        return [o["workingOrderData"] for o in self.c.get("/workingorders").json["workingOrders"]]

    def activity(self, **q):
        qs = "&".join("%s=%s" % kv for kv in q.items())
        a = self.c.get("/history/activity" + ("?" + qs if qs else ""))
        self.assertEqual(a.status, 200, a.raw)
        return a.json["activities"]

    def transactions(self, **q):
        qs = "&".join("%s=%s" % kv for kv in q.items())
        a = self.c.get("/history/transactions" + ("?" + qs if qs else ""))
        self.assertEqual(a.status, 200, a.raw)
        return a.json["transactions"]

    def account(self):
        return next(a for a in self.c.get("/accounts").json["accounts"] if a["preferred"])

    def ws(self):
        w = Ws(self.fake.stream_port)
        self.addCleanup(w.close)
        return w

    def tokens(self, **payload):
        out = {"cst": self.c.auth["CST"], "securityToken": self.c.auth["X-SECURITY-TOKEN"]}
        if payload:
            out["payload"] = payload
        return out


# -- the command line --------------------------------------------------------------------------------------------------

class TestCommandLine(unittest.TestCase):
    def test_cli_prints_one_json_line_and_serves_rest_and_stream(self):
        """stand-in's own interface; the URLs it prints are the documented ones on loopback: REST under /api/v1 and the
        stream at /connect (DOC: "connect to wss://api-streaming-capital.backend-capital.com/connect")"""
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_capitalcom.py"), "--port", "0", "--market", "open"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            info = json.loads(p.stdout.readline())
            for k in ("port", "stream_port", "rest_url", "api_url", "streaming_host", "stream_url", "control_url", "api_key",
                      "identifier", "password", "accounts"):
                self.assertIn(k, info)
            self.assertTrue(info["api_url"].endswith("/api/v1"))
            self.assertEqual(info["stream_url"], "ws://127.0.0.1:%d/connect" % info["stream_port"])
            self.assertTrue(info["rest_url"].startswith("http://127.0.0.1:"))
            conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
            try:
                conn.request("GET", "/api/v1/time")
                r = conn.getresponse()
                self.assertEqual(r.status, 200)
                self.assertIn("serverTime", json.loads(r.read()))
            finally:
                conn.close()
            w = Ws(info["stream_port"])
            self.assertEqual(w.status, 101)
            w.close()
        finally:
            p.kill()
            p.wait(5)
            p.stdout.close()
            p.stderr.close()


# -- logging in and the session --------------------------------------------------------------------------------------

class TestSession(Base):
    def test_login_answers_the_documented_body_and_the_two_token_headers(self):
        """DOC (POST /session: the response sample's fields, CST and X-SECURITY-TOKEN as response headers)"""
        a = self.c.login()
        self.assertEqual(list(a.json), LOGIN_KEYS)
        self.assertTrue(a.headers["cst"] and a.headers["x-security-token"])
        self.assertEqual(a.json["accountType"], "CFD")
        self.assertEqual(list(a.json["accountInfo"]), BALANCE_KEYS)
        self.assertEqual([list(x) for x in a.json["accounts"]], [LOGIN_ACCOUNT_KEYS] * len(a.json["accounts"]))
        preferred = [x["accountId"] for x in a.json["accounts"] if x["preferred"]]
        self.assertEqual(preferred, [a.json["currentAccountId"]])
        self.assertEqual(a.json["streamingHost"], self.fake.streaming_host)
        self.assertIsInstance(a.json["timezoneOffset"], int)

    def test_login_refusals_are_the_documented_ones(self):
        """POSTMAN ("API key missing" 400 error.null.api.key; "Invalid credentials" 401 error.invalid.details)"""
        body = {"identifier": self.fake.identifier, "password": self.fake.password}
        a = self.c.call("POST", "/session", body, auth=False)
        self.assertEqual((a.status, a.code), (400, "error.null.api.key"))
        time.sleep(0.01)
        a = self.c.call("POST", "/session", dict(body, password="wrong"), {"X-CAP-API-KEY": self.fake.api_key}, auth=False)
        self.assertEqual((a.status, a.code), (401, "error.invalid.details"))
        a = self.c.call("POST", "/session", dict(body, identifier="someone@example.com"), {"X-CAP-API-KEY": self.fake.api_key}, auth=False)
        self.assertEqual((a.status, a.code), (401, "error.invalid.details"))
        a = self.c.call("POST", "/session", body, {"X-CAP-API-KEY": "NOT-THE-KEY"}, auth=False)
        self.assertEqual(a.status, 401)          # the code is the stand-in's own
        self.assertEqual(a.code, "error.invalid.api.key")

    def test_a_session_lasts_ten_minutes_after_its_last_use(self):
        """DOC ("Both tokens are valid for 10 minutes after the last use"; GET /ping keeps it alive)"""
        self.fake.advance(590)
        self.assertEqual(self.c.get("/ping").json, {"status": "OK"})
        self.fake.advance(590)
        self.assertEqual(self.c.get("/ping").status, 200)
        self.fake.advance(601)
        a = self.c.get("/ping")
        self.assertEqual(a.status, 401)          # assumed, verify on demo (MCP treats 401 as an expired session)
        self.assertEqual(a.code, "error.invalid.session.token")      # stand-in's own code
        self.c.login()
        self.assertEqual(self.c.get("/ping").status, 200)

    def test_missing_or_wrong_tokens_are_refused(self):
        """DOC (every call after the login carries CST and X-SECURITY-TOKEN); the 401 and its code are the stand-in's
        own, assumed, verify on demo"""
        for headers in ({}, {"CST": self.c.auth["CST"]}, {"CST": self.c.auth["CST"], "X-SECURITY-TOKEN": "nope"}):
            a = self.c.call("GET", "/accounts", auth=False, headers=headers)
            self.assertEqual((a.status, a.code), (401, "error.invalid.session.token"), headers.keys())

    def test_get_session(self):
        """DOC (GET /session sample: clientId, accountId, timezoneOffset, locale, currency, streamEndpoint)"""
        a = self.c.get("/session")
        self.assertEqual(list(a.json), SESSION_KEYS)
        self.assertEqual(a.json["streamEndpoint"], self.fake.streaming_host)
        self.assertEqual(a.json["accountId"], self.fake.accounts[0]["accountId"])

    def test_switching_account(self):
        """DOC (PUT /session {accountId}; its response sample) and POSTMAN ("Invalid accountId": 400 error.invalid.accountId)"""
        other = self.fake.accounts[1]["accountId"]
        a = self.c.call("PUT", "/session", {"accountId": other})
        self.assertEqual(a.status, 200)
        self.assertEqual(list(a.json), ["trailingStopsEnabled", "dealingEnabled", "hasActiveDemoAccounts", "hasActiveLiveAccounts"])
        self.assertEqual(self.c.get("/session").json["accountId"], other)
        self.assertEqual(self.c.get("/session").json["currency"], "GBP")
        a = self.c.call("PUT", "/session", {"accountId": "12345"})
        self.assertEqual((a.status, a.code), (400, "error.invalid.accountId"))

    def test_switching_to_the_account_in_use_is_refused(self):
        """assumed, verify on demo: not in DOC; Capital.com is reported to refuse it (error.not-different.accountId)"""
        a = self.c.call("PUT", "/session", {"accountId": self.fake.accounts[0]["accountId"]})
        self.assertEqual((a.status, a.code), (400, "error.not-different.accountId"))

    def test_logout_answers_success_as_text_and_ends_the_session(self):
        """DOC (DELETE /session answers {"status": "SUCCESS"}, content type text/plain)"""
        a = self.c.call("DELETE", "/session")
        self.assertEqual(a.status, 200)
        self.assertEqual(a.json, {"status": "SUCCESS"})
        self.assertTrue(a.headers["content-type"].startswith("text/plain"))
        self.assertEqual(self.c.get("/ping").status, 401)

    def test_time_needs_no_session_and_encryption_key_needs_the_api_key(self):
        """DOC (GET /time: {"serverTime"} with no session; GET /session/encryptionKey: {"encryptionKey", "timeStamp"})"""
        a = self.c.call("GET", "/time", auth=False)
        self.assertEqual(list(a.json), ["serverTime"])
        self.assertLess(abs(a.json["serverTime"] - self.fake.broker.now()), 5000)
        a = self.c.call("GET", "/session/encryptionKey", auth=False, headers={"X-CAP-API-KEY": self.fake.api_key})
        self.assertEqual(list(a.json), ["encryptionKey", "timeStamp"])
        base64.b64decode(a.json["encryptionKey"])
        self.assertEqual(self.c.call("GET", "/session/encryptionKey", auth=False).code, "error.null.api.key")

    def test_a_login_with_no_account_on_this_host_is_refused_so(self):
        """seen on Capital.com's demo host, 2026-11-15 (a real login, its key likely made while the platform was on the
        live account): POST /session answered 401 {"errorCode": "error.null.accountId"}, a code the documentation does not
        list. A login flagged so (set_no_account, POST /_control/login) is answered that way, its key and password good;
        reset() clears the flag."""
        self.fake.set_no_account(True)
        self.assertTrue(self.fake.state()["no_account"])
        a = self.c.login(check=False)
        self.assertEqual((a.status, a.code), (401, "error.null.accountId"))
        self.assertNotIn("cst", a.headers)
        self.fake.set_no_account(False)
        self.c.login()
        # over HTTP, as a test in another process would ask it
        conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=15)
        try:
            conn.request("POST", "/_control/login", body=json.dumps({"no_account": True}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            self.assertEqual((r.status, json.loads(r.read())), (200, {"no_account": True}))
        finally:
            conn.close()
        self.assertEqual(self.c.login(check=False).code, "error.null.accountId")
        self.fake.reset()
        self.assertFalse(self.fake.state()["no_account"])
        self.c.login()

    def test_controls_expire_sessions_and_revoke_the_key(self):
        """stand-in's own controls; a revoked or expired key answers 401 (help centre:
        https://help.capitalccuk.com/hc/en-us/articles/36673475487250, "401 Unauthorized: API key expired or revoked")"""
        self.assertEqual(self.fake.expire_sessions(), 1)
        self.assertEqual(self.c.get("/ping").status, 401)
        time.sleep(0.01)
        self.c.login()
        self.fake.set_key_revoked(True)
        self.assertEqual(self.c.get("/ping").status, 401)
        self.assertEqual(self.c.login(check=False).status, 401)
        self.fake.set_key_revoked(False)
        time.sleep(1.05)
        self.c.login()
        self.assertEqual(self.c.get("/ping").status, 200)


# -- accounts --------------------------------------------------------------------------------------------------------

class TestAccounts(Base):
    def test_accounts_are_listed_as_documented(self):
        """DOC (GET /accounts sample: accountId, accountName, status, accountType, preferred, balance, currency, symbol)"""
        accounts = self.c.get("/accounts").json["accounts"]
        self.assertEqual(len(accounts), 2)
        for a in accounts:
            self.assertEqual(list(a), ACCOUNT_KEYS)
            self.assertEqual(list(a["balance"]), BALANCE_KEYS)
            self.assertEqual((a["status"], a["accountType"]), ("ENABLED", "CFD"))
            self.assertTrue(re.fullmatch(r"\d{17}", a["accountId"]))
        self.assertEqual([a["currency"] for a in accounts], ["USD", "GBP"])
        self.assertEqual([a["preferred"] for a in accounts], [True, False])

    def test_balance_is_deposit_plus_profit_loss(self):
        """DOC (the sample: balance 92.89 = deposit 90.38 + profitLoss 2.51); what each field means is assumed, verify on demo"""
        self.open("EURUSD", "BUY", 10000)
        self.fake.set_price("EURUSD", jump_pct=0.5)
        b = self.account()["balance"]
        self.assertAlmostEqual(b["balance"], b["deposit"] + b["profitLoss"], delta=0.0101)    # each rounded to the cent
        self.assertGreater(b["profitLoss"], 0)
        self.assertLess(b["available"], b["balance"])

    def test_preferences_and_leverage(self):
        """DOC (GET /accounts/preferences: hedgingMode and leverages {current, available}; PUT answers SUCCESS) and
        POSTMAN ("Invalid leverage value": 400 error.invalid.leverage.value)"""
        p = self.c.get("/accounts/preferences").json
        self.assertEqual(list(p), ["hedgingMode", "leverages"])
        self.assertEqual(list(p["leverages"]), ["SHARES", "CURRENCIES", "INDICES", "CRYPTOCURRENCIES", "COMMODITIES"])
        self.assertEqual(p["leverages"]["CURRENCIES"]["available"], [1, 10, 20, 30])
        a = self.c.call("PUT", "/accounts/preferences", {"leverages": {"CURRENCIES": 10}, "hedgingMode": True})
        self.assertEqual(a.json, {"status": "SUCCESS"})
        p = self.c.get("/accounts/preferences").json
        self.assertEqual((p["hedgingMode"], p["leverages"]["CURRENCIES"]["current"]), (True, 10))
        a = self.c.call("PUT", "/accounts/preferences", {"leverages": {"CURRENCIES": 7}})
        self.assertEqual((a.status, a.code), (400, "error.invalid.leverage.value"))
        details = self.c.get("/markets/EURUSD").json["instrument"]
        self.assertEqual((details["marginFactor"], details["marginFactorUnit"]), (10.0, "PERCENTAGE"))

    def test_top_up_of_the_demo_account(self):
        """DOC (POST /accounts/topUp {"amount"} answers {"successful": true}; the demo balance cannot exceed 100000;
        amount from -400000 to 400000); that it shows as a DEPOSIT is assumed, verify on demo"""
        a = self.c.call("POST", "/accounts/topUp", {"amount": 1000})
        self.assertEqual((a.status, a.json), (200, {"successful": True}))
        self.assertEqual(self.account()["balance"]["deposit"], 11000.0)
        a = self.c.call("POST", "/accounts/topUp", {"amount": 89001})
        self.assertEqual(a.status, 400)
        a = self.c.call("POST", "/accounts/topUp", {"amount": 400001})
        self.assertEqual(a.status, 400)
        deposits = self.transactions(type="DEPOSIT")
        self.assertEqual([t["size"] for t in deposits], ["1000.0"])


# -- markets -----------------------------------------------------------------------------------------------------------

class TestMarkets(Base):
    def test_every_market_with_no_parameter(self):
        """DOC (changelog 2023-08-04: GET /markets with no parameter lists every market; the list sample's fields)"""
        markets = self.c.get("/markets").json["markets"]
        self.assertEqual(len(markets), 46)
        for m in markets:
            self.assertEqual(list(m), MARKET_LIST_KEYS)
            self.assertLess(m["bid"], m["offer"])
            self.assertTrue(STAMP_MS.match(m["updateTimeUTC"]))
        self.assertEqual({m["instrumentType"] for m in markets}, {"CURRENCIES", "INDICES", "COMMODITIES", "SHARES", "CRYPTOCURRENCIES"})

    def test_search_term_and_its_refusal(self):
        """DOC (searchTerm, which wins over epics) and POSTMAN ("Empty searchTerm parameter": 400 error.null.searchTerm)"""
        found = [m["epic"] for m in self.c.get("/markets?searchTerm=silver").json["markets"]]
        self.assertIn("SILVER", found)
        self.assertIn("5CPSG", found)        # "Silverlake Axis", as in the documented sample
        found = [m["epic"] for m in self.c.get("/markets?searchTerm=gold&epics=SILVER").json["markets"]]
        self.assertEqual(found, ["GOLD"])
        a = self.c.get("/markets?searchTerm=")
        self.assertEqual((a.status, a.code), (400, "error.null.searchTerm"))

    def test_epics_answer_market_details_up_to_fifty(self):
        """POSTMAN ("Successful response: epics": {"marketDetails": [...]}) and DOC (at most 50 epics)"""
        a = self.c.get("/markets?epics=SILVER,NATURALGAS")
        self.assertEqual(list(a.json), ["marketDetails"])
        self.assertEqual([d["instrument"]["epic"] for d in a.json["marketDetails"]], ["SILVER", "NATURALGAS"])
        many = ",".join(["EURUSD"] * 51)
        self.assertEqual(self.c.get("/markets?epics=" + many).status, 400)
        self.assertEqual(self.c.get("/markets?epics=NOTFOUND").code, "error.not-found.epic")

    def test_single_market_details(self):
        """DOC (GET /markets/{epic} sample: instrument, dealingRules and snapshot, field for field; the silver values)"""
        d = self.c.get("/markets/SILVER").json
        self.assertEqual(list(d), ["instrument", "dealingRules", "snapshot"])
        self.assertEqual(list(d["instrument"]), INSTRUMENT_KEYS)
        self.assertEqual(list(d["dealingRules"]), RULE_KEYS)
        self.assertEqual(list(d["snapshot"]), SNAPSHOT_KEYS)
        rules = d["dealingRules"]
        self.assertEqual(rules["minDealSize"], {"unit": "POINTS", "value": 1.0})
        self.assertEqual(rules["minSizeIncrement"], {"unit": "POINTS", "value": 0.1})
        self.assertEqual(rules["minStopOrProfitDistance"], {"unit": "PERCENTAGE", "value": 0.01})
        self.assertEqual(rules["maxStopOrProfitDistance"], {"unit": "PERCENTAGE", "value": 60})
        self.assertEqual(rules["trailingStopsPreference"], "NOT_AVAILABLE")
        self.assertEqual(d["instrument"]["openingHours"]["mon"], ["00:00 - 21:59", "23:05 - 00:00"])
        self.assertEqual(d["instrument"]["openingHours"]["zone"], "UTC")
        self.assertEqual(d["instrument"]["overnightFee"]["swapChargeInterval"], 1440)
        self.assertEqual(d["snapshot"]["decimalPlacesFactor"], 3)
        self.assertEqual(self.c.get("/markets/EURUSD").json["dealingRules"]["minStopOrProfitDistance"]["unit"], "POINTS")

    def test_unknown_market(self):
        """POSTMAN ("Error: Market not found": 404 error.not-found.epic)"""
        a = self.c.get("/markets/INVALID")
        self.assertEqual((a.status, a.code), (404, "error.not-found.epic"))

    def test_the_documented_view_only_share(self):
        """DOC (the list sample's 5CPSG: marketStatus CLOSED, marketModes ["VIEW_ONLY"])"""
        m = self.c.get("/markets?epics=5CPSG").json["marketDetails"][0]["snapshot"]
        self.assertEqual((m["marketStatus"], m["marketModes"]), ("CLOSED", ["VIEW_ONLY"]))

    def test_auto_follows_the_opening_hours(self):
        """DOC (openingHours per weekday in UTC); stand-in's own: with market "auto" the status follows them. On a
        Saturday at noon UTC currencies are shut and cryptocurrencies trade."""
        self.fake.set_market("auto")
        now = self.fake.broker.now()
        days = (5 - time.gmtime(now // 1000).tm_wday) % 7
        target = (now // 86400000 + days) * 86400000 + 12 * 3600 * 1000
        if target <= now:
            target += 7 * 86400000
        self.fake.advance((target - now) / 1000.0)
        self.c.login()
        status = {m["epic"]: m["marketStatus"] for m in self.c.get("/markets").json["markets"]}
        self.assertEqual(status["EURUSD"], "CLOSED")
        self.assertEqual(status["BTCUSD"], "TRADEABLE")
        self.fake.set_market("open", "EURUSD")
        self.assertEqual(self.c.get("/markets/EURUSD").json["snapshot"]["marketStatus"], "TRADEABLE")

    def test_market_navigation(self):
        """DOC (GET /marketnavigation {"nodes": [{"id", "name"}]}; a node's sub-nodes; limit at most 500) and POSTMAN
        ("Error: Invalid limit parameter": 400 error.invalid.limit; a leaf answers {"nodes": [], "markets": [...]})"""
        top = self.c.get("/marketnavigation").json["nodes"]
        self.assertIn({"id": "hierarchy_v1.commodities_group", "name": "commodities_group"}, top)
        kids = self.c.get("/marketnavigation/hierarchy_v1.commons_group").json["nodes"]
        self.assertIn({"id": "hierarchy_v1.commons.weekend_trading", "name": "Weekend Trading"}, kids)
        leaf = self.c.get("/marketnavigation/hierarchy_v1.currencies.majors?limit=2").json
        self.assertEqual(leaf["nodes"], [])
        self.assertEqual([m["epic"] for m in leaf["markets"]], ["EURUSD", "GBPUSD"])
        a = self.c.get("/marketnavigation/hierarchy_v1.commodities?limit=0")
        self.assertEqual((a.status, a.code), (400, "error.invalid.limit"))

    def test_client_sentiment(self):
        """DOC (GET /clientsentiment?marketIds= and /clientsentiment/{marketId}) and POSTMAN (unknown: 404 error.not-found.epic)"""
        many = self.c.get("/clientsentiment?marketIds=SILVER,NATURALGAS").json["clientSentiments"]
        self.assertEqual([s["marketId"] for s in many], ["SILVER", "NATURALGAS"])
        one = self.c.get("/clientsentiment/SILVER").json
        self.assertEqual(list(one), ["marketId", "longPositionPercentage", "shortPositionPercentage"])
        self.assertAlmostEqual(one["longPositionPercentage"] + one["shortPositionPercentage"], 100.0, places=2)
        self.assertEqual(self.c.get("/clientsentiment/NOTFOUND").code, "error.not-found.epic")
        self.assertEqual(self.c.get("/clientsentiment?marketIds=NOTFOUND").status, 404)

    def test_watchlists(self):
        """DOC (the six watchlist endpoints and their samples) and POSTMAN (SUCCESS_NOT_ALL_INSTRUMENTS_ADDED;
        error.size.createwatchlistrequest.name; error.invalid.watchlist; error.not-found.watchlist; error.not-found.epic)"""
        a = self.c.call("POST", "/watchlists", {"name": "Lorem", "epics": ["SILVER", "NATURALGAS", "NOTFOUND"]})
        self.assertEqual(a.json["status"], "SUCCESS_NOT_ALL_INSTRUMENTS_ADDED")
        wid = a.json["watchlistId"]
        self.assertEqual(self.c.call("POST", "/watchlists", {"name": "Lorem ipsum dolor sit amet"}).code, "error.size.createwatchlistrequest.name")
        self.assertEqual(self.c.get("/watchlists").json["watchlists"][0],
                         {"id": wid, "name": "Lorem", "editable": True, "deleteable": True, "defaultSystemWatchlist": False})
        self.assertEqual(self.c.call("PUT", "/watchlists/" + wid, {"epic": "GOLD"}).json, {"status": "SUCCESS"})
        self.assertEqual(self.c.call("PUT", "/watchlists/" + wid, {"epic": "NOTFOUND"}).code, "error.not-found.epic")
        markets = self.c.get("/watchlists/" + wid).json["markets"]
        self.assertEqual([m["epic"] for m in markets], ["SILVER", "NATURALGAS", "GOLD"])
        self.assertEqual(self.c.call("DELETE", "/watchlists/%s/GOLD" % wid).json, {"status": "SUCCESS"})
        self.assertEqual(self.c.call("DELETE", "/watchlists/%s/GOLD" % wid).code, "error.not-found.epic")
        self.assertEqual(self.c.get("/watchlists/00001").code, "error.invalid.watchlist")
        self.assertEqual(self.c.call("DELETE", "/watchlists/" + wid).json, {"status": "SUCCESS"})
        self.assertEqual(self.c.call("DELETE", "/watchlists/" + wid).code, "error.not-found.watchlist")


# -- historical prices ------------------------------------------------------------------------------------------------

def utc(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%S")) * 1000


class TestPrices(Base):
    def test_default_is_the_minute_bars_of_the_last_ten_minutes(self):
        """DOC (GET /prices/{epic} with no parameter "returns the minute prices within the last 10 minutes"; max default 10)"""
        prices = self.c.get("/prices/SILVER").json
        self.assertEqual(list(prices), ["prices", "instrumentType"])
        self.assertEqual(prices["instrumentType"], "COMMODITIES")
        times = [utc(p["snapshotTimeUTC"]) for p in prices["prices"]]
        self.assertEqual(len(times), 10)
        self.assertEqual([b - a for a, b in zip(times, times[1:])], [60000] * 9)
        self.assertLessEqual(self.fake.broker.now() - times[0], 10 * 60000)

    def test_a_bar_is_bid_and_ask_ohlc_with_two_clocks(self):
        """DOC (each bar: snapshotTime, snapshotTimeUTC, openPrice/closePrice/highPrice/lowPrice as {bid, ask},
        lastTradedVolume; snapshotTime is local, the session's timezoneOffset ahead here)"""
        offset = self.c.get("/session").json["timezoneOffset"]
        for p in self.c.get("/prices/EURUSD?resolution=HOUR&max=50").json["prices"]:
            self.assertEqual(list(p), BAR_KEYS)
            self.assertTrue(STAMP_S.match(p["snapshotTimeUTC"]) and STAMP_S.match(p["snapshotTime"]))
            self.assertEqual(utc(p["snapshotTime"]) - utc(p["snapshotTimeUTC"]), offset * 3600000)
            for k in ("openPrice", "closePrice", "highPrice", "lowPrice"):
                self.assertEqual(list(p[k]), ["bid", "ask"])
                self.assertLess(p[k]["bid"], p[k]["ask"])
            self.assertLessEqual(p["lowPrice"]["bid"], min(p["openPrice"]["bid"], p["closePrice"]["bid"]))
            self.assertGreaterEqual(p["highPrice"]["bid"], max(p["openPrice"]["bid"], p["closePrice"]["bid"]))
            self.assertIsInstance(p["lastTradedVolume"], int)

    def test_every_documented_resolution_and_the_refusal_of_others(self):
        """DOC (MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK) and POSTMAN ("Invalid resolution
        parameter": 400 error.invalid.resolution). HOUR_4, DAY and WEEK aligned to UTC midnight and Monday is assumed,
        verify on demo"""
        for res, minutes in RESOLUTIONS.items():
            prices = self.c.get("/prices/GOLD?resolution=%s&max=20" % res).json["prices"]
            times = [utc(p["snapshotTimeUTC"]) for p in prices]
            self.assertEqual(len(times), 20, res)
            self.assertEqual({b - a for a, b in zip(times, times[1:])}, {minutes * 60000}, res)
            self.assertEqual(times[0] % (min(minutes, 1440) * 60000), 0, res)
            if res == "WEEK":
                self.assertEqual(time.gmtime(times[0] // 1000).tm_wday, 0)
        a = self.c.get("/prices/SILVER?resolution=MINUTE_3")
        self.assertEqual((a.status, a.code), (400, "error.invalid.resolution"))

    def test_max_is_ten_by_default_and_at_most_a_thousand(self):
        """DOC ("Default = 10, max = 1000"); refusing more is the stand-in's own"""
        self.assertEqual(len(self.c.get("/prices/GOLD?resolution=MINUTE&max=1000").json["prices"]), 1000)
        self.assertEqual(self.c.get("/prices/GOLD?max=1001").status, 400)
        self.assertEqual(self.c.get("/prices/GOLD?max=0").status, 400)

    def test_from_with_max_answers_the_bars_starting_at_from(self):
        """POSTMAN ("Use not default resolution, start date and max parameters": resolution=MINUTE_15&max=4&from=
        2022-02-24T00:00:00 answered four bars, 00:00 to 00:45)"""
        prices = self.c.get("/prices/SILVER?resolution=MINUTE_15&max=4&from=2026-02-24T00:00:00").json["prices"]
        self.assertEqual([p["snapshotTimeUTC"] for p in prices],
                         ["2026-02-24T00:00:00", "2026-02-24T00:15:00", "2026-02-24T00:30:00", "2026-02-24T00:45:00"])

    def test_from_and_to_filter_on_the_utc_time(self):
        """DOC (from and to: "Filtration by date based on snapshotTimeUTC parameter", YYYY-MM-DDTHH:MM:SS)"""
        prices = self.c.get("/prices/SILVER?resolution=MINUTE&max=10&from=2026-02-24T00:00:00&to=2026-02-24T00:05:00").json["prices"]
        self.assertEqual([p["snapshotTimeUTC"][11:16] for p in prices], ["00:00", "00:01", "00:02", "00:03", "00:04", "00:05"])
        self.assertEqual(self.c.get("/prices/SILVER?from=2026-02-24").status, 400)

    def test_paging_back_five_thousand_bars(self):
        """DOC (at most 1000 bars a call, so the Terminal's 5,000-bar chart is five calls); paging with `to` alone and
        the newest bars before it is assumed, verify on demo. No gap and no overlap between pages."""
        for res in ("MINUTE", "HOUR_4"):
            step = RESOLUTIONS[res] * 60000
            got, to = [], None
            for _ in range(5):
                q = "/prices/EURUSD?resolution=%s&max=1000" % res + ("&to=" + time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(to / 1000)) if to else "")
                page = [utc(p["snapshotTimeUTC"]) for p in self.c.get(q).json["prices"]]
                self.assertEqual(len(page), 1000, res)
                got = page + got
                to = page[0] - step
            self.assertEqual(len(got), 5000)
            self.assertEqual({b - a for a, b in zip(got, got[1:])}, {step}, res)

    def test_an_over_full_range_has_three_possible_readings(self):
        """assumed, verify on demo: which `max` bars a from..to range holding more answers is undocumented. The start
        reading "oldest" follows POSTMAN's one example; "newest" and "error" are the others (set_option)."""
        q = "/prices/GOLD?resolution=MINUTE&max=5&from=2026-03-02T10:00:00&to=2026-03-02T11:00:00"
        self.assertEqual(self.c.get(q).json["prices"][0]["snapshotTimeUTC"], "2026-03-02T10:00:00")
        self.fake.set_option("prices_overflow", "newest")
        self.assertEqual(self.c.get(q).json["prices"][-1]["snapshotTimeUTC"], "2026-03-02T11:00:00")
        self.fake.set_option("prices_overflow", "error")
        self.assertEqual(self.c.get(q).status, 400)

    def test_history_depth_and_an_empty_range(self):
        """assumed, verify on demo: how far back bars go and what an empty range answers are undocumented. The start:
        back to 2005, and 404 error.prices.not-found; set_history and set_option play the others."""
        self.assertEqual(self.c.get("/prices/GOLD?resolution=DAY&max=5&from=2005-01-03T00:00:00").json["prices"][0]["snapshotTimeUTC"],
                         "2005-01-03T00:00:00")
        a = self.c.get("/prices/GOLD?resolution=DAY&max=5&from=2001-01-01T00:00:00&to=2001-02-01T00:00:00")
        self.assertEqual((a.status, a.code), (404, "error.prices.not-found"))
        self.fake.set_option("prices_empty", "empty")
        self.assertEqual(self.c.get("/prices/GOLD?resolution=DAY&max=5&to=2001-02-01T00:00:00").json["prices"], [])
        self.fake.set_history("MINUTE", days=1)
        prices = self.c.get("/prices/GOLD?resolution=MINUTE&max=1000&to=" + time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.gmtime(self.fake.broker.now() / 1000 - 86400 + 600))).json["prices"]
        self.assertLessEqual(len(prices), 11)

    def test_the_forming_bar(self):
        """assumed, verify on demo: whether the bar still forming comes back. Start: it does, its close the bid now."""
        last = self.c.get("/prices/BTCUSD?resolution=HOUR&max=1").json["prices"][-1]
        now = self.fake.broker.now()
        self.assertEqual(utc(last["snapshotTimeUTC"]), now - now % 3600000)
        self.fake.set_option("prices_forming", False)
        last = self.c.get("/prices/BTCUSD?resolution=HOUR&max=1").json["prices"][-1]
        self.assertEqual(utc(last["snapshotTimeUTC"]), now - now % 3600000 - 3600000)

    def test_resolutions_agree_with_each_other_and_with_quotes(self):
        """stand-in's own: one walk underlies every resolution, so an hour bar is the aggregate of its minute bars, a
        day of its hours, and a held quote shows in the forming bar"""
        hour = self.c.get("/prices/AAPL?resolution=HOUR&max=1&from=2026-03-03T14:00:00").json["prices"][0]
        mins = self.c.get("/prices/AAPL?resolution=MINUTE&max=60&from=2026-03-03T14:00:00").json["prices"]
        self.assertEqual(len(mins), 60)
        for side in ("bid", "ask"):
            self.assertEqual(hour["openPrice"][side], mins[0]["openPrice"][side])
            self.assertEqual(hour["closePrice"][side], mins[-1]["closePrice"][side])
            self.assertEqual(hour["highPrice"][side], max(m["highPrice"][side] for m in mins))
            self.assertEqual(hour["lowPrice"][side], min(m["lowPrice"][side] for m in mins))
        self.assertEqual(hour["lastTradedVolume"], sum(m["lastTradedVolume"] for m in mins))
        day = self.c.get("/prices/AAPL?resolution=DAY&max=1&from=2026-03-03T00:00:00").json["prices"][0]
        hours = self.c.get("/prices/AAPL?resolution=HOUR&max=24&from=2026-03-03T00:00:00").json["prices"]
        self.assertEqual(day["highPrice"]["bid"], max(h["highPrice"]["bid"] for h in hours))
        self.assertEqual(day["lowPrice"]["bid"], min(h["lowPrice"]["bid"] for h in hours))
        bid, ask = self.quote("AAPL")
        self.fake.set_price("AAPL", bid=round(bid * 1.05, 2), ask=round(bid * 1.05 + 0.1, 2))
        last = self.c.get("/prices/AAPL?resolution=MINUTE&max=1").json["prices"][-1]
        self.assertEqual(last["closePrice"]["bid"], round(bid * 1.05, 2))
        self.assertGreaterEqual(last["highPrice"]["bid"], round(bid * 1.05, 2))

    def test_unknown_epic(self):
        """POSTMAN code error.not-found.epic; that /prices answers it is assumed, verify on demo"""
        self.assertEqual(self.c.get("/prices/NOPE").code, "error.not-found.epic")

    def test_the_trading_day_can_start_elsewhere(self):
        """assumed, verify on demo: the DAY and HOUR_4 alignment is undocumented; day_start_min moves the stand-in's
        trading day (here to 22:00 UTC) and every bar aligns to it"""
        m = fake_capitalcom.Market(7, "open", 22 * 60, clock=lambda: 0)
        t = utc("2026-03-03T23:30:00")
        self.assertEqual(m.bar_start("DAY", t), utc("2026-03-03T22:00:00"))
        self.assertEqual(m.bar_start("HOUR_4", t), utc("2026-03-03T22:00:00"))
        self.assertEqual(m.bar_start("HOUR", t), utc("2026-03-03T23:00:00"))
        self.assertEqual(m.bar_start("WEEK", t), utc("2026-03-01T22:00:00"))      # Sunday 22:00 opens Monday's day
        (o, h, lo, c), _, _ = m.bar("GOLD", "DAY", utc("2026-03-02T22:00:00"), utc("2026-03-10T00:00:00"))
        hours = [m.bar("GOLD", "HOUR", utc("2026-03-02T22:00:00") + k * 3600000, utc("2026-03-10T00:00:00")) for k in range(24)]
        self.assertEqual((o, c), (hours[0][0][0], hours[-1][0][3]))
        self.assertEqual((h, lo), (max(x[0][1] for x in hours), min(x[0][2] for x in hours)))


# -- the stream --------------------------------------------------------------------------------------------------------

class TestStream(Base):
    def test_subscribe_answers_and_quotes_follow(self):
        """DOC (marketData.subscribe with destination, correlationId, cst, securityToken, payload.epics; the answer
        {"subscriptions": {"OIL_CRUDE": "PROCESSED"}}; "quote" messages: epic, product, bid, bidQty, ofr, ofrQty, timestamp)"""
        w = self.ws()
        w.send(dict(destination="marketData.subscribe", correlationId="1", **self.tokens(epics=["EURUSD", "OIL_CRUDE"])))
        m = w.until(lambda m: m.get("correlationId") == "1")
        self.assertEqual(m, {"status": "OK", "destination": "marketData.subscribe", "correlationId": "1",
                             "payload": {"subscriptions": {"EURUSD": "PROCESSED", "OIL_CRUDE": "PROCESSED"}}})
        q = w.until(lambda m: m.get("destination") == "quote")
        self.assertEqual(list(q), ["status", "destination", "payload"])
        self.assertEqual(list(q["payload"]), ["epic", "product", "bid", "bidQty", "ofr", "ofrQty", "timestamp"])
        self.assertEqual(q["payload"]["product"], "CFD")
        self.assertLess(q["payload"]["bid"], q["payload"]["ofr"])
        self.assertLess(abs(q["payload"]["timestamp"] - self.fake.broker.now()), 5000)

    def test_unsubscribe(self):
        """DOC (marketData.unsubscribe answers every epic named, PROCESSED)"""
        w = self.ws()
        w.send(dict(destination="marketData.subscribe", correlationId="1", **self.tokens(epics=["EURUSD"])))
        w.until(lambda m: m.get("correlationId") == "1")
        w.send(dict(destination="marketData.unsubscribe", correlationId="2", **self.tokens(epics=["EURUSD"])))
        m = w.until(lambda m: m.get("correlationId") == "2")
        self.assertEqual(m["payload"], {"subscriptions": {"EURUSD": "PROCESSED"}})
        self.assertEqual(self.fake.state()["streams"][0]["quotes"], [])

    def test_ohlc_bars_are_bid_bars_of_the_named_resolutions(self):
        """DOC (OHLCMarketData.subscribe with resolutions and type; keys "EPIC:RES:TYPE"; "ohlc.event" messages:
        resolution, epic, type, priceType "bid", t, h, l, o, c; unsubscribe lists what went)"""
        w = self.ws()
        w.send(dict(destination="OHLCMarketData.subscribe", correlationId="3",
                    **self.tokens(epics=["OIL_CRUDE", "AAPL"], resolutions=["MINUTE_5"], type="classic")))
        m = w.until(lambda m: m.get("correlationId") == "3")
        self.assertEqual(m["payload"], {"subscriptions": {"OIL_CRUDE:MINUTE_5:classic": "PROCESSED", "AAPL:MINUTE_5:classic": "PROCESSED"}})
        e = w.until(lambda m: m.get("destination") == "ohlc.event")
        self.assertEqual(list(e["payload"]), ["resolution", "epic", "type", "priceType", "t", "h", "l", "o", "c"])
        self.assertEqual((e["payload"]["resolution"], e["payload"]["priceType"]), ("MINUTE_5", "bid"))
        self.assertEqual(e["payload"]["t"] % 300000, 0)
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(e["payload"]["t"] / 1000))
        rest = self.c.get("/prices/%s?resolution=MINUTE_5&max=1&from=%s" % (e["payload"]["epic"], start)).json["prices"][0]
        self.assertEqual(e["payload"]["o"], rest["openPrice"]["bid"])
        w.send(dict(destination="OHLCMarketData.unsubscribe", correlationId="4", **self.tokens(epics=["OIL_CRUDE"])))
        m = w.until(lambda m: m.get("correlationId") == "4")
        self.assertEqual(m["payload"], {"subscriptions": {"OIL_CRUDE:MINUTE_5:classic": "PROCESSED"}})

    def test_ohlc_defaults_and_heikin_ashi(self):
        """DOC (resolutions default MINUTE, type default classic; type classic or heikin-ashi)"""
        w = self.ws()
        w.send(dict(destination="OHLCMarketData.subscribe", correlationId="5", **self.tokens(epics=["GOLD"])))
        self.assertEqual(w.until(lambda m: m.get("correlationId") == "5")["payload"], {"subscriptions": {"GOLD:MINUTE:classic": "PROCESSED"}})
        w.send(dict(destination="OHLCMarketData.subscribe", correlationId="6", **self.tokens(epics=["GOLD"], resolutions=["HOUR"], type="heikin-ashi")))
        w.until(lambda m: m.get("correlationId") == "6")
        e = w.until(lambda m: m.get("destination") == "ohlc.event" and m["payload"]["type"] == "heikin-ashi")
        p = e["payload"]
        self.assertTrue(p["l"] <= min(p["o"], p["c"]) <= max(p["o"], p["c"]) <= p["h"])
        w.send(dict(destination="OHLCMarketData.subscribe", correlationId="7", **self.tokens(epics=["GOLD"], type="renko")))
        self.assertEqual(w.until(lambda m: m.get("correlationId") == "7")["status"], "ERROR")

    def test_ohlc_closed_mode_sends_each_bar_once_it_has_closed(self):
        """assumed, verify on demo: whether OHLCMarketData sends the bar while it forms or only when it has closed. The
        start: forming; set_option("ohlc_mode", "closed") plays the other reading. A closed bar equals its REST bar."""
        self.fake.set_option("ohlc_mode", "closed")
        w = self.ws()
        w.send(dict(destination="OHLCMarketData.subscribe", correlationId="1", **self.tokens(epics=["BTCUSD"])))
        w.until(lambda m: m.get("correlationId") == "1")
        self.fake.tick()
        self.fake.advance(60)
        e = w.until(lambda m: m.get("destination") == "ohlc.event")["payload"]
        self.assertLessEqual(e["t"] + 60000, self.fake.broker.now())
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(e["t"] / 1000))
        rest = self.c.get("/prices/BTCUSD?resolution=MINUTE&max=1&from=" + start).json["prices"][0]
        self.assertEqual((e["o"], e["h"], e["l"], e["c"]), (rest["openPrice"]["bid"], rest["highPrice"]["bid"],
                                                            rest["lowPrice"]["bid"], rest["closePrice"]["bid"]))

    def test_ping(self):
        """DOC (ping: destination "ping" with the tokens; the answer status OK and an empty payload)"""
        w = self.ws()
        w.send(dict(destination="ping", correlationId="5", **self.tokens()))
        self.assertEqual(w.until(lambda m: m.get("correlationId") == "5"),
                         {"status": "OK", "destination": "ping", "correlationId": "5", "payload": {}})

    def test_at_most_forty_epics(self):
        """DOC ("The maximum number of epics: 40"); refusing the request that would pass it, across every connection of
        the login, is the stand-in's own"""
        epics = [i.epic for i in self.fake.market.instruments if not i.view_only]
        w = self.ws()
        w.send(dict(destination="marketData.subscribe", correlationId="1", **self.tokens(epics=epics[:40])))
        self.assertEqual(w.until(lambda m: m.get("correlationId") == "1")["status"], "OK")
        w2 = self.ws()
        w2.send(dict(destination="marketData.subscribe", correlationId="2", **self.tokens(epics=epics[40:41])))
        m = w2.until(lambda m: m.get("correlationId") == "2")
        self.assertEqual((m["status"], m["payload"]), ("ERROR", {"errorCode": "error.invalid.epics.size"}))

    def test_a_message_without_good_tokens_is_refused(self):
        """DOC (every message carries cst and securityToken); the ERROR answer is the stand-in's own"""
        w = self.ws()
        w.send({"destination": "ping", "correlationId": "9", "cst": "x", "securityToken": "y"})
        m = w.until(lambda m: m.get("correlationId") == "9")
        self.assertEqual((m["status"], m["payload"]), ("ERROR", {"errorCode": "error.invalid.session.token"}))

    def test_switching_account_drops_the_stream(self):
        """DOC ("WebSocket streaming falls off when the financial account is changed")"""
        w = self.ws()
        w.send(dict(destination="ping", correlationId="1", **self.tokens()))
        w.until(lambda m: m.get("correlationId") == "1")
        self.c.call("PUT", "/session", {"accountId": self.fake.accounts[1]["accountId"]})
        with self.assertRaises((EOFError, ConnectionError, OSError)):
            for _ in range(200):
                w.frame(3.0)

    def test_a_stream_ends_with_its_session(self):
        """assumed, verify on demo: whether a connection ends when the session its messages carry ends. The start: it
        does (a logout here); set_option("stream_ends_with_session", False) plays the other reading, where the socket stays
        and its next message is refused"""
        w = self.ws()
        w.send(dict(destination="ping", correlationId="1", **self.tokens()))
        w.until(lambda m: m.get("correlationId") == "1")
        self.c.call("DELETE", "/session")
        with self.assertRaises((EOFError, OSError)):
            for _ in range(200):
                w.frame(3.0)
        self.c.login()
        self.fake.set_option("stream_ends_with_session", False)
        w2 = self.ws()
        w2.send(dict(destination="ping", correlationId="2", **self.tokens()))
        w2.until(lambda m: m.get("correlationId") == "2")
        old = self.tokens()
        self.fake.expire_sessions()
        self.fake.tick()
        w2.send(dict(destination="ping", correlationId="3", **old))
        self.assertEqual(w2.until(lambda m: m.get("correlationId") == "3")["status"], "ERROR")

    def test_an_idle_connection_is_closed_after_ten_minutes(self):
        """DOC ("ping service at least once every 10 minutes"); that the server then closes it is assumed, verify on demo"""
        w = self.ws()
        w.send(dict(destination="ping", correlationId="1", **self.tokens()))
        w.until(lambda m: m.get("correlationId") == "1")
        self.fake.advance(300)
        self.c.get("/ping")                       # the REST session stays alive
        self.fake.advance(301)
        ops = []
        try:
            for _ in range(200):
                ops.append(w.frame(3.0)[0])
        except (EOFError, OSError):
            pass
        self.assertIn(8, ops)                     # a close frame

    def test_stream_messages_do_not_keep_the_rest_session_alive(self):
        """assumed, verify on demo: whether a WebSocket message counts as a use of the tokens. The careful start: no
        (set_option("ws_extends_session", True) plays the other reading)"""
        w = self.ws()
        self.fake.advance(400)
        w.send(dict(destination="ping", correlationId="1", **self.tokens()))
        w.until(lambda m: m.get("correlationId") == "1")
        self.fake.advance(250)
        self.assertEqual(self.c.get("/ping").status, 401)
        self.c.login()
        self.fake.set_option("ws_extends_session", True)
        w2 = self.ws()
        self.fake.advance(400)
        w2.send(dict(destination="ping", correlationId="2", **self.tokens()))
        w2.until(lambda m: m.get("correlationId") == "2")
        self.fake.advance(250)
        self.assertEqual(self.c.get("/ping").status, 200)

    def test_drop_streams_control_and_a_shut_market_sends_no_quotes(self):
        """stand-in's own controls: drop_streams cuts every connection; a CLOSED market streams nothing"""
        self.fake.set_market("closed", "EURUSD")
        w = self.ws()
        w.send(dict(destination="marketData.subscribe", correlationId="1", **self.tokens(epics=["EURUSD", "BTCUSD"])))
        w.until(lambda m: m.get("correlationId") == "1")
        epics = set()
        end = time.time() + 1.5
        while time.time() < end:
            m = w.message(2.0)
            if m.get("destination") == "quote":
                epics.add(m["payload"]["epic"])
        self.assertEqual(epics, {"BTCUSD"})
        self.assertEqual(self.fake.drop_streams(), 1)
        with self.assertRaises((EOFError, OSError)):
            for _ in range(200):
                w.frame(3.0)

    def test_only_connect_is_a_stream_and_the_stream_log_has_no_tokens(self):
        """DOC (the stream is at /connect); stand-in's own: its stream log keeps no token"""
        other = Ws(self.fake.stream_port, "/other")
        other.close()
        self.assertEqual(other.status, 404)
        self.fake.stream_log(clear=True)
        w = self.ws()
        w.send(dict(destination="ping", correlationId="1", **self.tokens()))
        w.until(lambda m: m.get("correlationId") == "1")
        log = json.dumps(self.fake.stream_log())
        self.assertIn('"has_tokens": true', log)
        self.assertNotIn(self.c.auth["CST"], log)
        self.assertNotIn(self.c.auth["X-SECURITY-TOKEN"], log)


# -- positions ---------------------------------------------------------------------------------------------------------

class TestPositions(Base):
    def test_a_market_buy_is_taken_and_confirmed(self):
        """DOC (POST /positions answers {"dealReference": "o_..."}; GET /confirms/{dealReference}: its sample's fields;
        the opened position's id in affectedDeals) and MCP ("executed as market orders": a buy at the ask)"""
        bid, ask = self.quote("EURUSD")
        ref, conf = self.open("EURUSD", "BUY", 1000)
        self.assertTrue(ORDER_REF.match(ref), ref)
        self.assertEqual(list(conf), CONFIRM_KEYS)
        self.assertEqual((conf["status"], conf["dealStatus"], conf["direction"]), ("OPEN", "ACCEPTED", "BUY"))
        self.assertTrue(STAMP_MS.match(conf["date"]))
        self.assertEqual(len(conf["affectedDeals"]), 1)
        self.assertEqual(conf["affectedDeals"][0]["status"], "OPENED")
        deal = conf["affectedDeals"][0]["dealId"]
        self.assertTrue(DEAL_ID.match(deal))
        self.assertNotEqual(deal, conf["dealId"])
        pos = self.positions()[0]
        self.assertEqual(pos["dealId"], deal)
        self.assertEqual(pos["level"], conf["level"])
        self.assertEqual(pos["workingOrderId"], conf["dealId"])         # assumed, verify on demo
        self.assertAlmostEqual(pos["level"], self.fake.broker.market.quote("EURUSD")[1], delta=0.01)

    def test_a_market_sell_fills_at_the_bid(self):
        """MCP ("All orders placed through the Public API are executed as market orders"): a sell at the bid"""
        self.fake.set_price("GOLD", bid=2600.00, ask=2600.30)
        _, conf = self.open("GOLD", "SELL", 1)
        self.assertEqual(conf["level"], 2600.00)
        self.assertEqual(self.positions()[0]["direction"], "SELL")

    def test_a_confirmation_can_take_a_moment(self):
        """POSTMAN ("Deal confirmation not found": 404 error.not-found.dealReference); that a fresh one is not there yet
        is assumed, verify on demo, from MCP polling /confirms every 0.5 s for up to 15 s"""
        self.fake.set_confirm_delay(1.5)
        ref = self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000}).json["dealReference"]
        a = self.c.get("/confirms/" + ref)
        self.assertEqual((a.status, a.code), (404, "error.not-found.dealReference"))
        self.assertEqual(len(self.positions()), 1)
        time.sleep(1.6)
        self.assertEqual(self.c.get("/confirms/" + ref).json["dealStatus"], "ACCEPTED")
        self.assertEqual(self.c.get("/confirms/o_00000000-0000-4000-8000-000000000000").code, "error.not-found.dealReference")

    def test_a_position_as_listed(self):
        """DOC (GET /positions and GET /positions/{dealId}: {"position", "market"} with the sample's fields); its stop and
        profit fields are assumed, verify on demo (the samples have none)"""
        _, conf = self.open("SILVER", "BUY", 1, stopDistance=1, profitDistance=2)
        row = self.c.get("/positions").json["positions"][0]
        self.assertEqual(list(row), ["position", "market"])
        self.assertEqual(list(row["position"])[:13], POSITION_KEYS)
        self.assertEqual(list(row["market"]), MARKET_DEAL_KEYS)
        p = row["position"]
        self.assertEqual(p["dealReference"], "p_" + p["dealId"])
        self.assertAlmostEqual(p["stopLevel"], round(conf["level"] - 1, 3))
        self.assertAlmostEqual(p["profitLevel"], round(conf["level"] + 2, 3))
        one = self.c.get("/positions/" + p["dealId"]).json
        self.assertEqual(one["position"]["dealId"], p["dealId"])
        a = self.c.get("/positions/006011e7-0001-54c4-0000-000000000000")
        self.assertEqual((a.status, a.code), (404, "error.not-found.dealId"))

    def test_stops_and_profits_too_near_or_too_far(self):
        """POSTMAN (400 "error.invalid.stoploss.minvalue: 17.146", "error.invalid.takeprofit.maxvalue: 31.817": the nearest
        allowed level after the colon); the bounds from minStopOrProfitDistance/maxStopOrProfitDistance (DOC) are assumed,
        verify on demo"""
        self.fake.set_price("SILVER", bid=24.475, ask=24.494)
        a = self.c.call("POST", "/positions", {"epic": "SILVER", "direction": "BUY", "size": 1, "stopLevel": 5})
        self.assertEqual((a.status, a.code), (400, "error.invalid.stoploss.minvalue: 9.79"))
        a = self.c.call("POST", "/positions", {"epic": "SILVER", "direction": "BUY", "size": 1, "profitLevel": 50})
        self.assertEqual((a.status, a.code), (400, "error.invalid.takeprofit.maxvalue: 39.16"))
        a = self.c.call("POST", "/positions", {"epic": "SILVER", "direction": "BUY", "size": 1, "stopLevel": 24.475})
        self.assertEqual(a.code, "error.invalid.stoploss.maxvalue: 24.473")
        a = self.c.call("POST", "/positions", {"epic": "SILVER", "direction": "SELL", "size": 1, "profitLevel": 24.494})
        self.assertEqual(a.code, "error.invalid.takeprofit.maxvalue: 24.492")
        self.assertEqual(self.positions(), [])

    def test_guaranteed_stop_rules(self):
        """DOC (guaranteedStop: "If guaranteedStop equals true, then set stopLevel, stopDistance or stopAmount", "Cannot be
        set if trailingStop is true", "Cannot be set if hedgingMode is true"); the refusal's code is the stand-in's own"""
        bad = [{"guaranteedStop": True}, {"guaranteedStop": True, "trailingStop": True, "stopDistance": 1}]
        for extra in bad:
            a = self.c.call("POST", "/positions", dict(epic="SILVER", direction="BUY", size=1, **extra))
            self.assertEqual(a.status, 400, extra)
        _, conf = self.open("SILVER", "BUY", 1, guaranteedStop=True, stopDistance=1)
        self.assertTrue(conf["guaranteedStop"])
        self.c.call("PUT", "/accounts/preferences", {"hedgingMode": True})
        a = self.c.call("POST", "/positions", dict(epic="SILVER", direction="BUY", size=1, guaranteedStop=True, stopDistance=1))
        self.assertEqual(a.status, 400)

    def test_trailing_stop_rules_and_the_trail(self):
        """DOC (trailingStop needs stopDistance; the market's trailingStopsPreference, silver's NOT_AVAILABLE; the session's
        trailingStopsEnabled); that the broker moves it with the price is the stand-in's own"""
        a = self.c.call("POST", "/positions", dict(epic="EURUSD", direction="BUY", size=1000, trailingStop=True))
        self.assertEqual(a.status, 400)
        a = self.c.call("POST", "/positions", dict(epic="SILVER", direction="BUY", size=1, trailingStop=True, stopDistance=1))
        self.assertEqual(a.status, 400)
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        _, conf = self.open("EURUSD", "BUY", 1000, trailingStop=True, stopDistance=0.0050)
        self.assertTrue(conf["trailingStop"])
        p = self.positions()[0]
        self.assertTrue(p["trailingStop"])
        self.assertAlmostEqual(p["stopLevel"], 1.09506, places=5)
        self.fake.set_price("EURUSD", bid=1.10200, ask=1.10206)
        self.fake.tick()
        self.assertAlmostEqual(self.positions()[0]["stopLevel"], 1.09700, places=5)
        self.fake.set_price("EURUSD", bid=1.10100, ask=1.10106)
        self.fake.tick()
        self.assertAlmostEqual(self.positions()[0]["stopLevel"], 1.09700, places=5)   # one way only
        self.fake.set_option("trailing_enabled", False)
        a = self.c.call("POST", "/positions", dict(epic="EURUSD", direction="BUY", size=1000, trailingStop=True, stopDistance=0.005))
        self.assertEqual(a.status, 400)

    def test_size_refusals(self):
        """DOC (dealingRules minDealSize, maxDealSize, minSizeIncrement); the codes are the stand-in's own, in the documented
        "error.invalid.<what>.minvalue: <bound>" pattern"""
        cases = [(0.5, "error.invalid.size.minvalue: 1"), (100001, "error.invalid.size.maxvalue: 100000"),
                 (1.05, "error.invalid.size.increment: 0.1"), (-1, "error.invalid.size"), ("1", "error.invalid.size")]
        for size, code in cases:
            a = self.c.call("POST", "/positions", {"epic": "SILVER", "direction": "BUY", "size": size})
            self.assertEqual((a.status, a.code), (400, code), size)
        self.assertEqual(self.c.call("POST", "/positions", {"epic": "SILVER", "direction": "UP", "size": 1}).status, 400)
        a = self.c.call("POST", "/positions", {"epic": "NOPE", "direction": "BUY", "size": 1})
        self.assertEqual((a.status, a.code), (404, "error.not-found.epic"))

    def test_shut_market_and_unaffordable_orders_are_rejected_confirmations(self):
        """DOC ("a successful response does not always mean that the position has been successfully opened"); that a shut
        market and missing margin show as dealStatus REJECTED is assumed, verify on demo (the reason field is the
        stand-in's own)"""
        self.fake.set_market("closed", "EURUSD")
        ref, conf = self.open("EURUSD", "BUY", 1000)
        self.assertEqual((conf["dealStatus"], conf["affectedDeals"], conf["reason"]), ("REJECTED", [], "MARKET_CLOSED"))
        self.fake.set_market("open")
        _, conf = self.open("EURUSD", "BUY", 5000000)
        self.assertEqual((conf["dealStatus"], conf["reason"]), ("REJECTED", "INSUFFICIENT_FUNDS"))
        self.assertEqual(self.positions(), [])
        self.assertEqual(self.activity()[0]["status"], "REJECTED")

    def test_amending_a_position(self):
        """DOC (PUT /positions/{dealId} answers {"dealReference": "p_<dealId>"}; the body: stops and profits only) and
        POSTMAN (404 error.not-found.dealId; "error.invalid.stoploss.maxvalue: 24.302"). Omitted legs kept, null clearing a
        leg, and the "clear" reading are assumed, verify on demo"""
        self.fake.set_price("SILVER", bid=24.300, ask=24.320)
        self.open("SILVER", "BUY", 1, stopLevel=20, profitLevel=27)
        deal = self.positions()[0]["dealId"]
        a = self.c.call("PUT", "/positions/" + deal, {"profitLevel": 28})
        self.assertEqual(a.json, {"dealReference": "p_" + deal})
        p = self.positions()[0]
        self.assertEqual((p["stopLevel"], p["profitLevel"]), (20.0, 28.0))
        self.c.call("PUT", "/positions/" + deal, {"stopLevel": None})
        self.assertNotIn("stopLevel", self.positions()[0])
        a = self.c.call("PUT", "/positions/" + deal, {"stopLevel": 24.3})
        self.assertEqual((a.status, a.code), (400, "error.invalid.stoploss.maxvalue: 24.298"))
        self.fake.set_option("position_update_omitted", "clear")
        self.c.call("PUT", "/positions/" + deal, {"stopLevel": 21})
        p = self.positions()[0]
        self.assertEqual(p["stopLevel"], 21.0)
        self.assertNotIn("profitLevel", p)
        self.assertEqual(self.c.get("/confirms/p_" + deal).json["affectedDeals"], [{"dealId": deal, "status": "AMENDED"}])
        self.assertEqual(self.c.call("PUT", "/positions/nope", {"stopLevel": 21}).code, "error.not-found.dealId")

    def test_closing_a_position_and_closing_it_again(self):
        """DOC (DELETE /positions/{dealId}: the whole deal, no size; answers {"dealReference": "p_<dealId>"}) and POSTMAN
        ("Error: Position not found": 404 error.not-found.dealId)"""
        self.open("EURUSD", "BUY", 1000)
        deal = self.positions()[0]["dealId"]
        a = self.c.call("DELETE", "/positions/" + deal)
        self.assertEqual(a.json, {"dealReference": "p_" + deal})
        self.assertEqual(self.positions(), [])
        conf = self.c.get("/confirms/p_" + deal).json
        self.assertEqual((conf["status"], conf["dealStatus"], conf["direction"]), ("CLOSED", "ACCEPTED", "SELL"))
        a = self.c.call("DELETE", "/positions/" + deal)
        self.assertEqual((a.status, a.code), (404, "error.not-found.dealId"))

    def test_a_close_while_the_market_is_shut_is_rejected(self):
        """assumed, verify on demo: a close the market cannot take now is a REJECTED confirmation and the deal stays"""
        self.open("EURUSD", "BUY", 1000)
        deal = self.positions()[0]["dealId"]
        self.fake.set_market("closed", "EURUSD")
        ref = self.c.call("DELETE", "/positions/" + deal).json["dealReference"]
        self.assertEqual(self.c.get("/confirms/" + ref).json["dealStatus"], "REJECTED")
        self.assertEqual(len(self.positions()), 1)

    def test_stop_loss_and_take_profit_are_hit(self):
        """DOC (activity source SL and TP; transactions TRADE "Trade closed"); the trigger rules (a long's stop on the
        bid, filled at market) are the stand-in's own"""
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.open("EURUSD", "BUY", 10000, stopLevel=1.09500, profitLevel=1.10500)
        self.open("EURUSD", "SELL", 10000, stopLevel=1.10500, profitLevel=1.09600)
        # the sell offset the buy in standard mode; use hedging for two sides
        self.c.call("PUT", "/accounts/preferences", {"hedgingMode": True})
        self.open("EURUSD", "BUY", 10000, stopLevel=1.09500, profitLevel=1.10500)
        self.open("EURUSD", "SELL", 10000, stopLevel=1.10500, profitLevel=1.09600)
        self.assertEqual(len(self.positions()), 2)
        self.fake.set_price("EURUSD", bid=1.09400, ask=1.09406)
        self.fake.tick()
        self.assertEqual(self.positions(), [])
        sources = [a["source"] for a in self.activity() if a["type"] == "POSITION"][:2]
        self.assertEqual(sorted(sources), ["SL", "TP"])
        trades = self.transactions(type="TRADE")
        self.assertTrue(all(t["note"] == "Trade closed" for t in trades))

    def test_a_guaranteed_stop_fills_at_its_level(self):
        """stand-in's own: a guaranteed stop closes exactly at its level, and its fee is a TRADE_COMMISSION_GSL
        transaction (that type is documented: DOC, GET /history/transactions)"""
        self.fake.set_price("GOLD", bid=2600.00, ask=2600.30)
        self.open("GOLD", "BUY", 1, guaranteedStop=True, stopLevel=2550.00)
        self.fake.set_price("GOLD", bid=2500.00, ask=2500.30)
        self.fake.tick()
        details = self.activity(detailed="true")[0]["details"]
        self.assertEqual((details["level"], details["direction"]), (2550.0, "SELL"))
        self.assertEqual(len(self.transactions(type="TRADE_COMMISSION_GSL")), 1)

    def test_standard_mode_offsets_the_opposite_deals(self):
        """HEDGING (with hedging mode off an opposite position "automatically offsets or closes your existing trade");
        oldest first, and the affectedDeals words, are assumed, verify on demo"""
        self.open("EURUSD", "BUY", 1000)
        self.open("EURUSD", "BUY", 500)
        first, second = [p["dealId"] for p in self.positions()]
        _, conf = self.open("EURUSD", "SELL", 1200)
        self.assertEqual(conf["affectedDeals"], [{"dealId": first, "status": "FULLY_CLOSED"}, {"dealId": second, "status": "PARTIALLY_CLOSED"}])
        self.assertEqual(conf["status"], "CLOSED")
        self.assertEqual([(p["dealId"], p["size"]) for p in self.positions()], [(second, 300.0)])
        _, conf = self.open("EURUSD", "SELL", 500)
        left = self.positions()
        self.assertEqual([(p["direction"], p["size"]) for p in left], [("SELL", 200.0)])
        self.assertEqual(conf["affectedDeals"][-1], {"dealId": left[0]["dealId"], "status": "OPENED"})

    def test_hedging_mode_keeps_both_sides_and_margins_the_larger(self):
        """HEDGING ("They don't offset each other"; "Margin applies to the larger side of your hedge")"""
        self.c.call("PUT", "/accounts/preferences", {"hedgingMode": True})
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.open("EURUSD", "BUY", 30000)
        one = self.account()["balance"]
        self.open("EURUSD", "SELL", 10000)
        both = self.account()["balance"]
        self.assertEqual(len(self.positions()), 2)
        self.assertAlmostEqual(one["balance"] - one["available"], both["balance"] - both["available"], delta=1.0)

    def test_close_out_at_half_the_margin(self):
        """UK_TERMS (Schedule 2.11: "The standardised margin close-out rule has been set at 50% of the total initial
        margin"); one deal a pass, the worst first, is the stand-in's own"""
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.open("EURUSD", "BUY", 250000)
        self.fake.set_price("EURUSD", bid=1.07000, ask=1.07006)
        self.fake.tick()
        self.assertEqual(self.positions(), [])
        self.assertEqual(self.activity()[0]["source"], "CLOSE_OUT")

    def test_overnight_funding_is_charged_daily(self):
        """DOC (overnightFee: longRate, shortRate, swapChargeTimestamp at 22:00 UTC, swapChargeInterval 1440; the
        transaction type SWAP); the % a day reading of the rates is assumed, verify on demo"""
        self.open("US500", "BUY", 10)
        now = self.fake.broker.now()
        nxt = (now // 86400000) * 86400000 + 22 * 3600 * 1000
        if nxt <= now:
            nxt += 86400000
        self.fake.advance((nxt - now) / 1000.0 + 60)
        self.c.login()
        swaps = self.transactions(type="SWAP", lastPeriod=86400 * 2)
        self.assertEqual(len(swaps), 1)
        self.assertLess(float(swaps[0]["size"]), 0)

    def test_the_currency_of_upl(self):
        """assumed, verify on demo: the currency of a position's upl (DOC shows a `currency` beside it). The start: the
        account's; set_option("upl_currency", "instrument") plays the market's own"""
        self.fake.set_price("USDJPY", bid=150.000, ask=150.007)
        self.open("USDJPY", "BUY", 1000)
        self.fake.set_price("USDJPY", bid=151.000, ask=151.007)
        p = self.positions()[0]
        self.assertEqual(p["currency"], "USD")
        self.assertAlmostEqual(p["upl"], (151.0 - 150.007) * 1000 / 151.0035, delta=0.01)
        self.fake.set_option("upl_currency", "instrument")
        p = self.positions()[0]
        self.assertEqual(p["currency"], "JPY")
        self.assertAlmostEqual(p["upl"], (151.0 - 150.007) * 1000, delta=0.01)

    def test_the_broker_closing_a_deal_itself(self):
        """DOC (activity sources DEALER and SYSTEM); the close_as control is the stand-in's own"""
        self.open("EURUSD", "BUY", 1000)
        deal = self.positions()[0]["dealId"]
        self.fake.close_as(deal, "DEALER")
        self.assertEqual(self.positions(), [])
        self.assertEqual(self.activity(filter="source==DEALER")[0]["dealId"], deal)


# -- working orders ----------------------------------------------------------------------------------------------------

class TestWorkingOrders(Base):
    def test_a_limit_order_as_listed(self):
        """DOC (POST /workingorders answers {"dealReference": "o_..."}; GET /workingorders: workingOrderData and marketData
        with the sample's fields; the legs as distances signed from the level, stopDistance -3 and profitDistance 3 on a
        BUY LIMIT at 20)"""
        self.fake.set_price("SILVER", bid=24.300, ask=24.320)
        a = self.c.call("POST", "/workingorders", {"epic": "SILVER", "direction": "BUY", "size": 1, "level": 20, "type": "LIMIT",
                                                    "stopLevel": 17, "profitLevel": 23, "goodTillDate": "2099-06-09T01:01:00"})
        self.assertEqual(a.status, 200, a.raw)
        self.assertTrue(ORDER_REF.match(a.json["dealReference"]))
        row = self.c.get("/workingorders").json["workingOrders"][0]
        self.assertEqual(list(row), ["workingOrderData", "marketData"])
        wo = row["workingOrderData"]
        self.assertEqual(list(wo), ["dealId", "direction", "epic", "orderSize", "leverage", "orderLevel", "timeInForce", "goodTillDate",
                                    "goodTillDateUTC", "createdDate", "createdDateUTC", "guaranteedStop", "orderType", "stopDistance",
                                    "profitDistance", "trailingStop", "currencyCode"])
        self.assertEqual((wo["orderLevel"], wo["stopDistance"], wo["profitDistance"]), (20.0, -3.0, 3.0))
        self.assertEqual((wo["timeInForce"], wo["goodTillDateUTC"]), ("GOOD_TILL_DATE", "2099-06-09T01:01:00.000"))
        self.assertEqual(list(row["marketData"]), MARKET_DEAL_KEYS)
        conf = self.c.get("/confirms/" + a.json["dealReference"]).json
        self.assertEqual((conf["dealStatus"], conf["dealId"]), ("ACCEPTED", wo["dealId"]))

    def test_order_refusals(self):
        """POSTMAN ("Cancellation date in the past": 400 error.not-in-future.goodTillDate) and DOC (type "Must be LIMIT or
        STOP"; goodTillDate as YYYY-MM-DDTHH:MM:SS); the other codes and the wrong-side refusal are the stand-in's own"""
        a = self.order("SILVER", size=1, goodTillDate="2020-01-01T00:00:00")
        self.assertEqual((a.status, a.code), (400, "error.not-in-future.goodTillDate"))
        self.assertEqual(self.order("SILVER", size=1, goodTillDate="tomorrow").code, "error.invalid.goodTillDate")
        self.assertEqual(self.order("SILVER", size=1, kind="MARKET").code, "error.invalid.type")
        bid, ask = self.quote("SILVER")
        self.assertEqual(self.order("SILVER", size=1, kind="LIMIT", level=round(ask + 1, 3)).code, "error.invalid.level")
        self.assertEqual(self.order("SILVER", size=1, kind="STOP", level=round(ask - 1, 3)).code, "error.invalid.level")
        self.assertEqual(self.orders(), [])

    def test_amending_keeps_what_is_left_out_but_resets_the_flags(self):
        """MCP (Capital.com's own client: level, stops and goodTillDate left out of PUT /workingorders/{dealId} "are
        preserved by the broker"; guaranteedStop and trailingStop left out are reset to false; GOOD_TILL_CANCELLED clears an
        expiry) and DOC (PUT answers a new "o_..." dealReference)"""
        self.fake.set_price("SILVER", bid=24.300, ask=24.320)
        a = self.order("SILVER", size=1, level=20, stopDistance=3, guaranteedStop=True, goodTillDate="2099-01-01T00:00:00")
        deal = self.orders()[0]["dealId"]
        a = self.c.call("PUT", "/workingorders/" + deal, {"profitDistance": 4})
        self.assertEqual(a.status, 200, a.raw)
        self.assertTrue(ORDER_REF.match(a.json["dealReference"]))
        wo = self.orders()[0]
        self.assertEqual((wo["orderLevel"], wo["stopDistance"], wo["profitDistance"]), (20.0, -3.0, 4.0))
        self.assertEqual(wo["goodTillDateUTC"], "2099-01-01T00:00:00.000")
        self.assertFalse(wo["guaranteedStop"])
        self.c.call("PUT", "/workingorders/" + deal, {"level": 21, "timeInForce": "GOOD_TILL_CANCELLED"})
        wo = self.orders()[0]
        self.assertEqual((wo["orderLevel"], wo["timeInForce"]), (21.0, "GOOD_TILL_CANCELLED"))
        self.assertNotIn("goodTillDate", wo)
        self.assertEqual(self.c.call("PUT", "/workingorders/nope", {"level": 21}).code, "error.not-found.dealId")

    def test_cancelling_and_cancelling_again(self):
        """DOC (DELETE /workingorders/{dealId} answers {"dealReference": "o_..."}) and POSTMAN ("Order not found": 404
        error.not-found.dealId)"""
        self.order("EURUSD")
        deal = self.orders()[0]["dealId"]
        a = self.c.call("DELETE", "/workingorders/" + deal)
        self.assertTrue(ORDER_REF.match(a.json["dealReference"]))
        self.assertEqual(self.orders(), [])
        self.assertEqual(self.c.call("DELETE", "/workingorders/" + deal).code, "error.not-found.dealId")
        self.assertEqual(self.activity()[0]["status"], "CANCELLED")

    def test_a_limit_triggers_and_fills_at_market(self):
        """MCP (every API order is "executed as market orders", so the fill is the market's, not the level); the position
        names its order in workingOrderId (DOC: the field; that it is this order's dealId is assumed, verify on demo)"""
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.order("EURUSD", "BUY", 1000, "LIMIT", level=1.09900, stopDistance=0.005)
        deal = self.orders()[0]["dealId"]
        self.fake.set_price("EURUSD", bid=1.09800, ask=1.09806)
        self.fake.tick()
        self.assertEqual(self.orders(), [])
        p = self.positions()[0]
        self.assertEqual((p["level"], p["workingOrderId"]), (1.09806, deal))
        self.assertAlmostEqual(p["stopLevel"], 1.09306, places=5)
        kinds = [(a["type"], a["status"]) for a in self.activity()[:2]]
        self.assertEqual(kinds, [("WORKING_ORDER", "EXECUTED"), ("POSITION", "ACCEPTED")])

    def test_a_sell_stop_triggers_on_the_bid(self):
        """stand-in's own trigger rule: a sell stop when the bid reaches its level, filled at the bid"""
        self.fake.set_price("GOLD", bid=2600.00, ask=2600.30)
        self.order("GOLD", "SELL", 1, "STOP", level=2590.00)
        self.fake.set_price("GOLD", bid=2595.00, ask=2595.30)
        self.fake.tick()
        self.assertEqual(len(self.orders()), 1)
        self.fake.set_price("GOLD", bid=2589.50, ask=2589.80)
        self.fake.tick()
        self.assertEqual(self.positions()[0]["level"], 2589.50)

    def test_good_till_date_expires(self):
        """DOC (goodTillDate; the activity status EXPIRED); source SYSTEM is assumed, verify on demo"""
        now = self.fake.broker.now()
        gtd = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now / 1000 + 120))
        self.order("EURUSD", goodTillDate=gtd)
        self.fake.advance(180)
        self.c.login()
        self.assertEqual(self.orders(), [])
        rows = self.activity(lastPeriod=600)
        self.assertEqual((rows[0]["type"], rows[0]["source"], rows[0]["status"]), ("WORKING_ORDER", "SYSTEM", "EXPIRED"))

    def test_margin_for_a_resting_order(self):
        """assumed, verify on demo: whether a working order holds margin (UK_TERMS 3.15 says pending orders block funds,
        among share-dealing wording). Start: it does not; set_option("wo_reserves_margin", True) plays the other reading"""
        before = self.account()["balance"]["available"]
        self.order("EURUSD", size=100000)
        self.assertEqual(self.account()["balance"]["available"], before)
        self.fake.set_option("wo_reserves_margin", True)
        self.assertLess(self.account()["balance"]["available"], before)


# -- fills in parts ----------------------------------------------------------------------------------------------------

class TestFills(Base):
    def test_the_next_fill_in_parts(self):
        """stand-in's own control: an execution split in deals, each a tick worse, the confirmation listing them all
        (live partial fills are documented by the help centre, https://help.capitalccuk.com/hc/en-us/articles/34236561519250;
        how the API shows them is assumed, verify on demo)"""
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.fake.fill_next([1, 3])
        _, conf = self.open("EURUSD", "BUY", 1000)
        self.assertEqual([a["status"] for a in conf["affectedDeals"]], ["OPENED", "OPENED"])
        self.assertEqual(conf["size"], 1000.0)
        sizes = [(p["size"], p["level"]) for p in self.positions()]
        self.assertEqual(sizes, [(250.0, 1.10006), (750.0, 1.10007)])

    def test_the_next_fill_cut_short(self):
        """stand-in's own control: only the first part fills and the rest is never filled"""
        self.fake.fill_next([1, 1], rest="cancel")
        _, conf = self.open("EURUSD", "BUY", 1000)
        self.assertEqual(conf["size"], 500.0)
        self.assertEqual([p["size"] for p in self.positions()], [500.0])

    def test_a_size_rule_for_the_conformance_partial_hint(self):
        """stand-in's own control (the conformance suite's --partial-hint): every order of that size fills in two deals"""
        self.fake.fill_rule(200, [1, 1], "EURUSD")
        _, conf = self.open("EURUSD", "BUY", 200)
        self.assertEqual(len(conf["affectedDeals"]), 2)
        _, conf = self.open("EURUSD", "BUY", 300)
        self.assertEqual(len(conf["affectedDeals"]), 1)

    def test_a_working_order_filling_over_time(self):
        """stand-in's own control: a triggered working order fills part by part; meanwhile it stays listed with the size
        still to fill"""
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.order("EURUSD", "BUY", 1000, "LIMIT", level=1.09900)
        self.fake.fill_next([1, 1], interval=1.5)
        self.fake.set_price("EURUSD", bid=1.09800, ask=1.09806)
        self.fake.tick()
        self.assertEqual([p["size"] for p in self.positions()], [500.0])
        self.assertEqual(self.orders()[0]["orderSize"], 500.0)
        time.sleep(1.6)
        self.fake.tick()
        self.assertEqual([p["size"] for p in self.positions()], [500.0, 500.0])
        self.assertEqual(self.orders(), [])


# -- the request limits, at their documented values ----------------------------------------------------------------------

class TestLimits(Base):
    options = dict(market="open", confirm_delay=0, orders_per_hour=3)

    def setUp(self):
        self.fake.reset()
        time.sleep(1.05)                  # the login allowance of the test before
        self.c = Client(self.fake)
        self.c.login()
        time.sleep(1.05)                  # and this test's own requests start in a fresh second

    def test_ten_requests_a_second(self):
        """DOC ("The maximum request rate is 10 per second per user"); POSTMAN (429 error.too-many.requests)"""
        codes = [self.c.get("/ping").status for _ in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)
        self.assertEqual(self.c.get("/ping").code, "error.too-many.requests")
        time.sleep(1.05)
        self.assertEqual(self.c.get("/ping").status, 200)

    def test_one_order_each_tenth_of_a_second(self):
        """DOC ("1 request per 0.1 seconds per user when opening positions or creating orders. Otherwise the
        position/order requests are going to be rejected")"""
        first = self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000})
        second = self.c.call("POST", "/workingorders", {"epic": "EURUSD", "direction": "BUY", "size": 1000, "type": "LIMIT", "level": 0.5})
        self.assertEqual((first.status, second.status), (200, 429))

    def test_orders_an_hour_on_demo(self):
        """DOC ("POST /positions and POST /workingorders: 1000 requests per hour" on Demo); this class runs it at 3"""
        codes = []
        for _ in range(4):
            codes.append(self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000}).status)
            time.sleep(0.11)
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_one_login_a_second(self):
        """DOC ("POST /session: 1 request per second per API key"); POSTMAN ("Request limit exceeded": 429
        error.too-many.requests)"""
        self.assertEqual(self.c.login(check=False).status, 200)
        a = self.c.login(check=False)
        self.assertEqual((a.status, a.code), (429, "error.too-many.requests"))

    def test_the_documented_defaults(self):
        """DOC (10 a second; 1 login a second; 1 order a 0.1 s; 1000 an hour on demo; 40 epics; 10-minute sessions)"""
        f = FakeCapitalCom()
        lim = f.broker.limits
        self.assertEqual((lim["rate_per_s"], lim["session_per_s"], lim["order_spacing"], lim["orders_per_hour"]), (10, 1, 0.1, 1000))
        self.assertEqual((f.max_epics, f.broker.session_ttl, f.stream_idle, f.topups_per_s, f.topups_per_day), (40, 600, 600, 10, 100))
        # https://help.capitalccuk.com/hc/en-us/articles/30648881738898 ("a default balance of 1,000")
        self.assertEqual(f.broker.accounts[f.accounts[0]["accountId"]].cash, 1000)


# -- the account's history --------------------------------------------------------------------------------------------

class TestHistory(Base):
    def test_activity_rows(self):
        """DOC (GET /history/activity sample: date, dateUTC, epic, dealId, source, type, status) and POSTMAN (detailed=true
        adds details {dealReference, marketName, currency, size, direction, level, guaranteedStop}; newest first; a close
        is a POSITION row with the closing direction)"""
        self.open("EURUSD", "BUY", 1000)
        deal = self.positions()[0]["dealId"]
        self.c.call("DELETE", "/positions/" + deal)
        rows = self.activity()
        self.assertEqual([list(r) for r in rows], [ACTIVITY_KEYS] * 2)
        self.assertEqual([(r["type"], r["source"], r["status"]) for r in rows], [("POSITION", "USER", "ACCEPTED")] * 2)
        detailed = self.activity(detailed="true", dealId=deal)
        self.assertEqual(list(detailed[0]["details"]), DETAIL_KEYS)
        self.assertEqual([r["details"]["direction"] for r in detailed], ["SELL", "BUY"])
        self.assertEqual(detailed[0]["details"]["dealReference"], "p_" + deal)
        self.assertTrue(rows[0]["dateUTC"] >= rows[1]["dateUTC"])

    def test_activity_filters(self):
        """DOC (dealId; a FIQL filter on epic, source, status and type)"""
        self.open("EURUSD", "BUY", 1000)
        self.open("GOLD", "BUY", 1)
        self.order("SILVER", size=1)
        self.assertEqual({r["epic"] for r in self.activity(filter="epic==GOLD")}, {"GOLD"})
        self.assertEqual({r["type"] for r in self.activity(filter="type==WORKING_ORDER")}, {"WORKING_ORDER"})
        self.assertEqual(len(self.activity(filter="epic==GOLD,epic==EURUSD")), 2)
        self.assertEqual(len(self.activity(filter="epic==GOLD;type==WORKING_ORDER")), 0)
        self.assertEqual(self.c.get("/history/activity?filter=colour==red").status, 400)

    def test_activity_range_rules(self):
        """DOC (from/to at most one day apart, one of them alone giving a one-day range; lastPeriod default 600, at most
        86400) and POSTMAN ("Invalid date format": 400 error.invalid.from)"""
        self.open("EURUSD", "BUY", 1000)
        t = time.gmtime(self.fake.broker.now() / 1000)
        today = time.strftime("%Y-%m-%dT00:00:00", t)
        self.assertEqual(len(self.activity(**{"from": today})), 1)
        self.assertEqual(self.c.get("/history/activity?from=2026-01-01T00:00:00&to=2026-01-03T00:00:00").status, 400)
        self.assertEqual(self.c.get("/history/activity?from=yesterday").code, "error.invalid.from")
        self.assertEqual(self.c.get("/history/activity?lastPeriod=86401").status, 400)
        self.fake.advance(700)
        self.c.login()
        self.assertEqual(self.activity(), [])
        self.assertEqual(len(self.activity(lastPeriod=900)), 1)

    def test_a_deal_made_on_the_platform_leaves_its_activity(self):
        """stand-in's own: a deal opened and closed directly on
        Capital.com's platform (made_on_platform), not through this API,
        leaves the activity a human's trade would -- an open POSITION row and
        a close POSITION row carrying the closing direction and level -- for
        fills.list to read."""
        acct = next(a for a in self.fake.broker.accounts.values()
                    if a.preferred)
        now = self.fake.broker.now()
        did = self.fake.broker.made_on_platform(
            acct, "EURUSD", "BUY", 2000, 1.10000, now - 60_000,
            close_level=1.10500)
        rows = self.activity(detailed="true", dealId=did)
        self.assertEqual([(r["type"], r["status"]) for r in rows],
                         [("POSITION", "ACCEPTED")] * 2)
        # newest first: the close (SELL, 1.105), then the open (BUY, 1.10)
        self.assertEqual([(r["details"]["direction"], r["details"]["level"],
                           r["details"]["size"]) for r in rows],
                         [("SELL", 1.105, 2000.0), ("BUY", 1.1, 2000.0)])
        self.assertEqual(self.positions(), [])      # opened and closed
        # an open-only platform deal leaves one row and a live position
        did2 = self.fake.broker.made_on_platform(
            acct, "GOLD", "SELL", 1, 2600.0, now - 30_000)
        self.assertEqual([(r["details"]["direction"], r["type"])
                          for r in self.activity(detailed="true", dealId=did2)],
                         [("SELL", "POSITION")])
        self.assertEqual([p["dealId"] for p in self.positions()], [did2])

    def test_transactions(self):
        """DOC (GET /history/transactions sample: date, dateUtc (so spelt), instrumentName, transactionType, note, reference,
        size as a string, currency, status; the type filter's values)"""
        self.fake.set_price("GOLD", bid=2600.00, ask=2600.30)
        self.open("GOLD", "BUY", 1)
        self.fake.set_price("GOLD", bid=2610.00, ask=2610.30)
        self.c.call("DELETE", "/positions/" + self.positions()[0]["dealId"])
        rows = self.transactions()
        self.assertEqual([list(r) for r in rows], [TRANSACTION_KEYS])
        r = rows[0]
        self.assertEqual((r["instrumentName"], r["transactionType"], r["note"], r["size"], r["currency"], r["status"]),
                         ("GOLD", "TRADE", "Trade closed", "9.7", "USD", "PROCESSED"))
        self.assertEqual(self.account()["balance"]["deposit"], 10009.7)
        self.assertEqual(self.transactions(type="SWAP"), [])
        self.assertEqual(self.c.get("/history/transactions?type=LOTTERY").status, 400)


# -- the controls ------------------------------------------------------------------------------------------------------

class TestControls(Base):
    def test_refusing_the_next_request_with_a_documented_code(self):
        """stand-in's own control, with POSTMAN's codes: at the request, or as a REJECTED confirmation"""
        self.fake.refuse_next("error.invalid.stoploss.minvalue: 17.146")
        a = self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000})
        self.assertEqual((a.status, a.code), (400, "error.invalid.stoploss.minvalue: 17.146"))
        self.assertEqual(self.positions(), [])
        self.fake.refuse_next("RISK_CHECK", at="confirm")
        _, conf = self.open("EURUSD", "BUY", 1000)
        self.assertEqual((conf["dealStatus"], conf["reason"]), ("REJECTED", "RISK_CHECK"))
        self.assertEqual(self.positions(), [])
        self.open("EURUSD", "BUY", 1000)
        self.assertEqual(len(self.positions()), 1)

    def test_failing_the_next_requests(self):
        """stand-in's own control: 500 or 429 without acting (429 with the documented error.too-many.requests, POSTMAN)"""
        self.fake.fail_next(500, count=1, path="/positions")
        self.assertEqual(self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000}).status, 500)
        self.fake.fail_next(429, retry_after=2)
        a = self.c.get("/accounts")
        self.assertEqual((a.status, a.code, a.headers.get("retry-after")), (429, "error.too-many.requests", "2"))
        self.assertEqual(self.positions(), [])

    def test_failing_one_method_with_a_gateways_page(self):
        """stand-in's own control: a fault for one method only, answered with a gateway's HTML page (CRLF lines), as a
        proxy in front of Capital.com would answer a 5xx"""
        self.fake.fail_next(502, count=1, path="/positions", method="POST", page=True)
        self.assertEqual(self.c.get("/positions").status, 200)                # another method: not this fault
        a = self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000})
        self.assertEqual((a.status, a.headers.get("content-type")), (502, "text/html"))
        self.assertIn(b"<title>502 Bad Gateway</title>", a.raw)
        self.assertIn(b"\r\n", a.raw)
        self.assertEqual(self.positions(), [])                                # not acted on
        for bad in ({"status": 429, "page": True}, {"status": 200}):
            with self.assertRaises(ValueError):
                self.fake.fail_next(**bad)

    def test_losing_the_answer_of_an_order_that_was_carried_out(self):
        """stand-in's own control: the request is carried out and the connection cut without an answer (the case DOC's
        missing client reference makes hard: the order exists and nothing named it)"""
        self.fake.lose_next_answer("/positions", "POST")
        with self.assertRaises((http.client.HTTPException, ConnectionError, OSError)):
            self.c.call("POST", "/positions", {"epic": "EURUSD", "direction": "BUY", "size": 1000})
        self.assertEqual(len(self.positions()), 1)
        posts = [r for r in self.fake.requests() if r["method"] == "POST" and r["path"] == "/api/v1/positions"]
        self.assertEqual(posts[-1]["body"], {"epic": "EURUSD", "direction": "BUY", "size": 1000})

    def test_moving_the_price(self):
        """stand-in's own control: hold, jump, trend and release; a release carries on from where the hold was"""
        self.fake.set_price("GOLD", bid=2000.00, ask=2000.30)
        self.assertEqual(self.quote("GOLD"), (2000.0, 2000.3))
        self.fake.set_price("GOLD", walk=True)
        self.assertAlmostEqual(self.quote("GOLD")[0], 2000.0, delta=5)
        before = self.fake.market.mid("GOLD")
        self.fake.set_price("GOLD", jump_pct=10)
        self.assertAlmostEqual(self.fake.market.mid("GOLD") / before, 1.10, delta=0.01)
        self.fake.set_price("GOLD", spread=1.0)
        bid, ask = self.quote("GOLD")
        self.assertAlmostEqual(ask - bid, 1.0, places=6)
        start = self.fake.market.mid("GOLD")
        self.fake.set_price("GOLD", trend_pct_per_s=1, seconds=30)
        self.fake.advance(30)
        self.assertAlmostEqual(self.fake.market.mid("GOLD") / start, 1.01 ** 30, delta=0.02)

    def test_moving_the_clock_looks_at_orders_on_the_way(self):
        """stand-in's own control: advance() runs the order engine along the way"""
        self.fake.set_price("EURUSD", bid=1.10000, ask=1.10006)
        self.open("EURUSD", "BUY", 1000, stopLevel=1.09990)
        self.fake.set_price("EURUSD", walk=True)
        self.fake.set_price("EURUSD", trend_pct_per_s=-0.01, seconds=60)
        self.fake.advance(120)
        self.c.login()
        self.assertEqual(self.positions(), [])

    def test_moving_money_without_a_trade(self):
        """stand-in's own control: adjust_balance moves the cash and writes a transaction of a documented type (DOC)"""
        self.fake.adjust_balance(-12.5, kind="ADJUSTMENT", note="Dealer adjustment")
        self.assertEqual(self.account()["balance"]["deposit"], 9987.5)
        t = self.transactions(type="ADJUSTMENT")[0]
        self.assertEqual((t["size"], t["note"]), ("-12.5", "Dealer adjustment"))

    def test_reset_puts_everything_back(self):
        """stand-in's own control"""
        self.open("EURUSD", "BUY", 1000)
        self.fake.set_option("prices_overflow", "error")
        self.fake.set_market("closed")
        self.fake.reset()
        self.c.login()
        self.assertEqual(self.positions(), [])
        self.assertEqual(self.fake.state()["market"], "open")
        self.assertEqual(self.fake.state()["options"]["prices_overflow"], "oldest")

    def test_the_request_log_never_holds_a_credential(self):
        """stand-in's own: the log says which headers came, never what they held, and keeps no session body"""
        self.fake.requests(clear=True)
        self.c.login()
        self.c.get("/accounts")
        log = json.dumps(self.fake.requests())
        for secret in (self.fake.password, self.fake.api_key, self.c.auth["CST"], self.c.auth["X-SECURITY-TOKEN"]):
            self.assertNotIn(secret, log)
        self.assertIn('"has_api_key": true', log)

    def test_the_http_controls(self):
        """stand-in's own: each control is also POST /_control/<name>"""
        def post(name, body):
            conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=5)
            try:
                conn.request("POST", "/_control/" + name, body=json.dumps(body), headers={"Content-Type": "application/json"})
                r = conn.getresponse()
                return r.status, json.loads(r.read())
            finally:
                conn.close()
        self.assertEqual(post("price", {"epic": "GOLD", "bid": 1999.0, "ask": 1999.3})[0], 200)
        self.assertEqual(self.quote("GOLD"), (1999.0, 1999.3))
        self.assertEqual(post("market", {"state": "closed", "epic": "GOLD"})[1]["modes"], {"GOLD": "closed"})
        self.assertEqual(post("option", {"name": "ohlc_mode", "value": "closed"})[1]["options"]["ohlc_mode"], "closed")
        self.assertEqual(post("option", {"name": "ohlc_mode", "value": "sideways"})[0], 400)
        self.assertEqual(post("nonsense", {})[0], 400)
        status, state = post("state", {})
        self.assertEqual((status, state["modes"]), (200, {"GOLD": "closed"}))


if __name__ == "__main__":
    unittest.main()
