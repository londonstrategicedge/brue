"""Tests of tests/fake_oanda.py, the local stand-in for OANDA's v20 API.

Each test names the documentation it checks the stand-in against (its URL in the docstring, under
https://developer.oanda.com/rest-live-v20/ unless another is given), or says that it checks a behaviour measured on
a live account, or one that is the stand-in's own. Loopback only; no key, no network.

Run: PYTHONPATH=. python3 -m unittest tests.test_fake_oanda -v     (or: python3 -m unittest discover -s tests)
"""
import ast
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_oanda  # noqa: E402
from fake_oanda import FakeOanda  # noqa: E402

DOCS = "https://developer.oanda.com/rest-live-v20/"
RFC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{9}Z$")
UNIX = re.compile(r"^\d+\.\d{9}$")
NUM = re.compile(r"^-?\d+(\.\d+)?$")
MONEY = re.compile(r"^-?\d+\.\d{4}$")

# documented: https://developer.oanda.com/rest-live-v20/account-df/ Account, in the documented order (the conditional
# ones left out)
ACCOUNT_KEYS = ["id", "alias", "currency", "createdByUserID", "createdTime", "guaranteedStopLossOrderMode",
                "resettablePLTime", "openTradeCount", "openPositionCount", "pendingOrderCount", "hedgingEnabled",
                "unrealizedPL", "NAV", "marginUsed", "marginAvailable", "positionValue", "marginCloseoutUnrealizedPL",
                "marginCloseoutNAV", "marginCloseoutMarginUsed", "marginCloseoutPercent", "marginCloseoutPositionValue",
                "withdrawalLimit", "marginCallMarginUsed", "marginCallPercent", "balance", "pl", "resettablePL",
                "financing", "commission", "dividendAdjustment", "guaranteedExecutionFees", "marginCallExtensionCount",
                "lastTransactionID"]
# documented: https://developer.oanda.com/rest-live-v20/primitives-df/ Instrument
INSTRUMENT_KEYS = ["name", "type", "displayName", "pipLocation", "displayPrecision", "tradeUnitsPrecision",
                   "minimumTradeSize", "maximumTrailingStopDistance", "minimumGuaranteedStopLossDistance",
                   "minimumTrailingStopDistance", "maximumPositionSize", "maximumOrderUnits", "marginRate", "commission",
                   "guaranteedStopLossOrderMode", "financing", "tags"]
# documented: https://developer.oanda.com/rest-live-v20/pricing-df/ ClientPrice (the deprecated ones the stand-in
# gives on REST come after)
PRICE_KEYS = ["type", "instrument", "time", "status", "tradeable", "bids", "asks", "closeoutBid", "closeoutAsk"]
# documented: https://developer.oanda.com/rest-live-v20/trade-df/ Trade
TRADE_KEYS = ["id", "instrument", "price", "openTime", "state", "initialUnits", "initialMarginRequired", "currentUnits",
              "realizedPL", "unrealizedPL", "marginUsed", "closingTransactionIDs", "financing", "dividendAdjustment"]


class Answer:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw
        try:
            self.json = json.loads(raw) if raw else None
        except ValueError:
            self.json = None


def until(fn, timeout=6.0, step=0.02):
    end = time.monotonic() + timeout
    while True:
        got = fn()
        if got or time.monotonic() >= end:
            return got
        time.sleep(step)


class Stream:
    """A client of one stream, reading its lines in a thread as an adapter would with http.client."""

    def __init__(self, fake, path, token=None, headers=None, timeout=15):
        self.conn = http.client.HTTPConnection("127.0.0.1", fake.stream_port, timeout=timeout)
        h = {"Authorization": "Bearer " + (token or fake.token)}
        h.update(headers or {})
        self.conn.request("GET", path, headers=h)
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.items, self.ended, self.error = [], threading.Event(), None
        if self.status == 200:
            threading.Thread(target=self._read, daemon=True).start()
        else:
            self.body = json.loads(self.resp.read() or b"null")
            self.ended.set()

    def _read(self):
        try:
            while True:
                line = self.resp.readline()
                if not line:
                    break
                self.items.append((time.monotonic(), json.loads(line)))
        except Exception as e:                      # an abrupt end shows here (IncompleteRead, reset)
            self.error = e
        finally:
            self.ended.set()

    def objs(self, kind=None):
        return [o for _, o in list(self.items) if kind is None or o.get("type") == kind]

    def wait(self, pred, timeout=6.0):
        return until(lambda: next((o for _, o in list(self.items) if pred(o)), None), timeout)

    def close(self):
        try:
            self.conn.sock.shutdown(2)
        except Exception:
            pass
        self.conn.close()


class Base(unittest.TestCase):
    MARKET = "open"
    KW = {}
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        kw = dict(market=cls.MARKET, new_connections_per_second=None, rate_limit=None, heartbeat=0.5)
        kw.update(cls.KW)
        cls.fake = FakeOanda(**kw).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.fake.requests(clear=True)
        self.conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=60)
        ids = self.fake.account_ids
        self.A = ids[0]
        self.H = ids[1] if len(ids) > 1 else None
        self.M = ids[2] if len(ids) > 2 else None
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            s.close()
        self.conn.close()

    def call(self, method, path, body=None, token=None, headers=None, auth=True):
        h = {"User-Agent": "fake-oanda-test/1"}
        if auth:
            h["Authorization"] = "Bearer " + (self.fake.token if token is None else token)
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        data = body if body is None or isinstance(body, (bytes, str)) else json.dumps(body)
        for attempt in (1, 2):
            try:
                self.conn.request(method, path, body=data, headers=h)
                r = self.conn.getresponse()
                return Answer(r.status, r.headers, r.read())
            except (http.client.HTTPException, OSError):
                self.conn.close()
                self.conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=60)
                if attempt == 2:
                    raise

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def p(self, rest="", acct=None):
        return "/v3/accounts/%s%s" % (acct or self.A, rest)

    def order(self, acct=None, expect=201, **order):
        r = self.call("POST", self.p("/orders", acct), {"order": order})
        self.assertEqual(r.status, expect, r.raw)
        return r.json

    def put(self, rest, body=None, acct=None, expect=200):
        r = self.call("PUT", self.p(rest, acct), body if body is not None else {})
        self.assertEqual(r.status, expect, r.raw)
        return r.json

    def price(self, name, acct=None):
        r = self.get(self.p("/pricing?instruments=" + name, acct))
        self.assertEqual(r.status, 200, r.raw)
        q = r.json["prices"][0]
        return D(q["bids"][0]["price"]), D(q["asks"][0]["price"])

    def hold(self, name, bid, ask):
        self.fake.set_price(name, bid=bid, ask=ask)

    def txs(self, acct=None, since=0, type_=None):
        r = self.get(self.p("/transactions/sinceid?id=%d%s" % (since, "&type=" + type_ if type_ else ""), acct))
        self.assertEqual(r.status, 200, r.raw)
        return r.json["transactions"]

    def trades(self, acct=None):
        return self.get(self.p("/openTrades", acct)).json["trades"]

    def stream(self, rest, acct=None, **kw):
        s = Stream(self.fake, self.p(rest, acct), **kw)
        self.streams.append(s)
        return s

    def candles(self, name, expect=200, **params):
        q = "&".join("%s=%s" % (k, v) for k, v in params.items())
        r = self.get(self.p("/instruments/%s/candles?%s" % (name, q)))
        self.assertEqual(r.status, expect, r.raw)
        return r.json


def ny_instant(fake, hh, mm, ss=0, weekdays=(0, 1, 2, 3)):
    """The next hh:mm:ss New York, on one of `weekdays` (Monday 0), at least a minute after the stand-in's now; RFC 3339."""
    z = fake_oanda.NEW_YORK
    now_s = fake.clock.now_ns() // 10 ** 9
    d = z.local_date(now_s)
    for i in range(0, 15):
        day = d + timedelta(i)
        if day.weekday() in weekdays:
            t = z.local_to_utc(day, hh, mm, ss)
            if t > now_s + 60:
                return fake_oanda.rfc3339(t * 10 ** 9)
    raise AssertionError("no such day")


def ms_of(text):
    return fake_oanda.parse_time(text) // 10 ** 6


# ---------------------------------------------------------------------------------------------------------------
# The stand-in itself: what it is made of
# ---------------------------------------------------------------------------------------------------------------

class TheFile(unittest.TestCase):
    @staticmethod
    def source():
        with open(os.path.join(HERE, "fake_oanda.py")) as f:
            return f.read()

    def test_it_is_python_3_10_and_standard_library_only(self):
        """Standard library only, nothing newer than Python 3.10."""
        src = self.source()
        tree = ast.parse(src, feature_version=(3, 10))
        stdlib = {"__future__", "argparse", "bisect", "collections", "gzip", "hashlib", "json", "math", "random", "re",
                  "select", "socket", "sys", "threading", "time", "datetime", "decimal", "http", "urllib", "zoneinfo"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertIn(a.name.split(".")[0], stdlib)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module.split(".")[0], stdlib)

    def test_every_documented_list_is_whole(self):
        """https://developer.oanda.com/rest-live-v20/transaction-df/: 198 TransactionRejectReason values, 65 OrderCancelReason values, the transaction types;
        https://developer.oanda.com/rest-live-v20/instrument-df/: 21 CandlestickGranularity values."""
        self.assertEqual(len(fake_oanda.REJECT_REASONS), 198)
        self.assertEqual(len(fake_oanda.CANCEL_REASONS), 65)
        self.assertEqual(len(fake_oanda.TRANSACTION_TYPES), 38)
        self.assertEqual(list(fake_oanda.GRANULARITIES), ["S5", "S10", "S15", "S30", "M1", "M2", "M4", "M5", "M10", "M15",
                                                          "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D", "W", "M"])
        for reason in fake_oanda.REASON_TEXT:
            self.assertTrue(reason in fake_oanda.REJECT_REASONS or reason in fake_oanda.CANCEL_REASONS, reason)

    def test_every_behaviour_says_where_it_comes_from(self):
        """Every behaviour copied from the broker says "documented: <URL>" or is marked as the stand-in's
        own; here, at least that the documentation pages are cited and the stand-in's own choices are marked."""
        src = self.source()
        self.assertEqual(re.findall(r"documented: (?!https?://)(\S+)", src), ["<URL>\"", "the"],
                         "every citation names its URL")
        for page in ("development-guide", "authentication", "troubleshooting-errors", "account-ep", "account-df",
                     "instrument-df", "pricing-ep", "pricing-df", "pricing-common-df", "primitives-df", "order-ep",
                     "order-df", "trade-ep", "trade-df", "position-ep", "position-df", "transaction-ep", "transaction-df"):
            self.assertIn(DOCS + page + "/", src, page)
        self.assertGreater(src.count("documented:"), 60)
        self.assertGreater(src.count("stand-in's own"), 30)
        self.assertIn("measured on a live account", src)

    def test_the_command_line_prints_one_json_line_and_serves(self):
        """`python tests/fake_oanda.py --port 0` prints one JSON line with its URLs and serves until killed."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_oanda.py"), "--port", "0"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            info = json.loads(proc.stdout.readline())
            for key in ("rest_url", "stream_url", "port", "stream_port", "token", "accounts", "control_url", "env"):
                self.assertIn(key, info)
            self.assertTrue(info["rest_url"].startswith("http://127.0.0.1:"))
            self.assertNotEqual(info["port"], info["stream_port"])
            c = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=10)
            try:
                c.request("GET", "/v3/accounts", headers={"Authorization": "Bearer " + info["token"]})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual([a["id"] for a in json.loads(r.read())["accounts"]], info["accounts"])
            finally:
                c.close()
            self.assertEqual(info["env"]["BRUE_CRED_TOKEN"], info["token"])
            self.assertIsNone(proc.poll(), "it keeps serving")
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()


# ---------------------------------------------------------------------------------------------------------------
# Authentication and its refusals
# ---------------------------------------------------------------------------------------------------------------

class Authentication(Base):
    def test_a_bearer_token_opens_the_api_and_nothing_else_does(self):
        """https://developer.oanda.com/rest-live-v20/authentication/: "Authorization: Bearer <token>"; https://developer.oanda.com/rest-live-v20/troubleshooting-errors/: 401 "Insufficient authorization to
        perform request." with {"errorMessage"}."""
        self.assertEqual(self.get("/v3/accounts").status, 200)
        for kw in ({"auth": False}, {"token": "0" * 32 + "-" + "0" * 32},
                   {"auth": False, "headers": {"Authorization": "Basic " + self.fake.token}},
                   {"auth": False, "headers": {"Authorization": self.fake.token}}):
            r = self.get("/v3/accounts", **kw)
            self.assertEqual((r.status, r.json), (401, {"errorMessage": "Insufficient authorization to perform request."}), kw)

    def test_an_account_the_token_does_not_reach_is_forbidden(self):
        """https://developer.oanda.com/rest-live-v20/troubleshooting-errors/: "Invalid AccountID" is "The provided request was forbidden." (listed under 401); the
        403 reading is an option of the stand-in."""
        r = self.get("/v3/accounts/101-004-1111111-001/summary")
        self.assertEqual((r.status, r.json), (401, {"errorMessage": "The provided request was forbidden."}))
        self.fake.set_options(foreign_account_status=403)
        self.assertEqual(self.get("/v3/accounts/101-004-1111111-001/summary").status, 403)
        self.assertEqual(self.get("/v3/accounts/not-an-account/summary").status, 400)

    def test_a_revoked_token_stops_at_once_and_a_new_one_works(self):
        """https://developer.oanda.com/rest-live-v20/authentication/: OANDA keeps no copy of a token; a lost one is revoked and a new one generated."""
        old = self.fake.token
        self.fake.revoke_token()
        self.assertEqual(self.get("/v3/accounts", token=old).status, 401)
        new = self.fake.rotate_token()["token"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.get("/v3/accounts", token=new).status, 200)
        self.assertEqual(self.get("/v3/accounts", token=old).status, 401)

    def test_one_token_reaches_every_sub_account_but_not_one_added_after_it(self):
        """https://developer.oanda.com/rest-live-v20/authentication/: a token "will grant access to all of your sub-accounts"; a sub-account added later needs a
        new token (the help page the 2026-11-11 research quoted)."""
        ids = [a["id"] for a in self.get("/v3/accounts").json["accounts"]]
        self.assertEqual(ids, [self.A, self.H, self.M])
        new = self.fake.add_account("EUR", hedging=False, alias="Later")["id"]
        self.assertNotIn(new, [a["id"] for a in self.get("/v3/accounts").json["accounts"]])
        self.assertEqual(self.get(self.p("/summary", new)).status, 401)
        tok = self.fake.rotate_token()["token"]
        self.assertIn(new, [a["id"] for a in self.get("/v3/accounts", token=tok).json["accounts"]])
        self.assertEqual(self.get(self.p("/summary", new), token=tok).json["account"]["currency"], "EUR")

    def test_a_401_on_a_reconnect_with_a_good_token(self):
        """measured on a live account: a reconnect refused with 401 although the token was valid; the next
        try worked, and GET /v3/accounts with the same token answered."""
        self.fake.refuse_connects(1, 401, "stream")
        s = self.stream("/pricing/stream?instruments=EUR_USD")
        self.assertEqual((s.status, s.body), (401, {"errorMessage": "Insufficient authorization to perform request."}))
        self.assertEqual(self.get("/v3/accounts").status, 200)
        self.assertEqual(self.stream("/pricing/stream?instruments=EUR_USD").status, 200)

    def test_the_token_is_never_in_the_request_log(self):
        """A credential is never written to a log; the stand-in keeps only whether a request carried one."""
        self.get("/v3/accounts")
        self.get("/v3/accounts", token="bad")
        log = self.fake.requests()
        self.assertEqual([e["authorized"] for e in log], [True, False])
        self.assertNotIn(self.fake.token, json.dumps(log))


# ---------------------------------------------------------------------------------------------------------------
# Accounts and choosing one; the account; instruments
# ---------------------------------------------------------------------------------------------------------------

class Accounts(Base):
    def test_the_list_has_ids_tags_and_the_mt4_link(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ GET /v3/accounts: accounts (AccountProperties: id, mt4AccountID only for MT4, tags); https://developer.oanda.com/rest-live-v20/account-df/
        AccountID "{siteID}-{divisionID}-{userID}-{accountNumber}"."""
        r = self.get("/v3/accounts")
        self.assertEqual(r.status, 200)
        rows = r.json["accounts"]
        for row in rows:
            self.assertRegex(row["id"], r"^\d{3}-\d{3}-\d+-\d{3}$")
            self.assertEqual(row["tags"], [])
        self.assertEqual([("mt4AccountID" in row) for row in rows], [False, False, True])
        self.assertEqual(r.headers["Content-Type"], "application/json")
        self.assertTrue(r.headers["RequestID"].isdigit())

    def test_details_and_summary_print_money_as_strings_in_the_home_currency(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ GET /v3/accounts/{id} and /summary: account and lastTransactionID; https://developer.oanda.com/rest-live-v20/account-df/: AccountUnits and
        DecimalNumber are strings, counts are integers, hedgingEnabled a boolean."""
        full = self.get(self.p()).json
        summary = self.get(self.p("/summary")).json
        self.assertEqual(list(summary), ["account", "lastTransactionID"])
        acct = summary["account"]
        self.assertEqual(list(acct), ACCOUNT_KEYS)
        for key in ("balance", "NAV", "unrealizedPL", "marginUsed", "marginAvailable", "pl", "financing", "withdrawalLimit"):
            self.assertRegex(acct[key], MONEY, key)
        self.assertIsInstance(acct["openTradeCount"], int)
        self.assertIs(acct["hedgingEnabled"], False)
        self.assertEqual((acct["currency"], acct["balance"]), ("GBP", "100000.0000"))
        self.assertRegex(acct["createdTime"], RFC)
        self.assertEqual(full["account"]["trades"], [])
        self.assertEqual(full["account"]["orders"], [])
        self.assertTrue(self.get(self.p("/summary", self.H)).json["account"]["hedgingEnabled"])

    def test_times_come_in_the_format_the_header_asks_for(self):
        """https://developer.oanda.com/rest-live-v20/primitives-df/ AcceptDatetimeFormat: RFC3339 "YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ" or UNIX "12345678.000000123"."""
        self.assertRegex(self.get(self.p("/summary")).json["account"]["createdTime"], RFC)
        r = self.get(self.p("/summary"), headers={"Accept-Datetime-Format": "UNIX"})
        self.assertRegex(r.json["account"]["createdTime"], UNIX)
        self.assertEqual(self.get(self.p("/summary"), headers={"Accept-Datetime-Format": "ISO"}).status, 400)

    def test_instruments_have_the_documented_fields(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ GET .../instruments (instruments, lastTransactionID; ?instruments= filters); https://developer.oanda.com/rest-live-v20/primitives-df/
        Instrument: pipLocation and displayPrecision integers, the sizes and rates strings, financing with
        financingDaysOfWeek."""
        r = self.get(self.p("/instruments"))
        rows = r.json["instruments"]
        self.assertEqual({x["type"] for x in rows}, {"CURRENCY", "METAL", "CFD"})
        eu = next(x for x in rows if x["name"] == "EUR_USD")
        self.assertEqual(list(eu), INSTRUMENT_KEYS)
        self.assertEqual((eu["pipLocation"], eu["displayPrecision"], eu["tradeUnitsPrecision"]), (-4, 5, 0))
        self.assertIsInstance(eu["minimumTradeSize"], str)
        self.assertEqual([d["dayOfWeek"] for d in eu["financing"]["financingDaysOfWeek"]],
                         ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"])
        jp = next(x for x in rows if x["name"] == "USD_JPY")
        self.assertEqual((jp["pipLocation"], jp["displayPrecision"]), (-2, 3))
        one = self.get(self.p("/instruments?instruments=XAU_USD,UK100_GBP")).json["instruments"]
        self.assertEqual([x["name"] for x in one], ["XAU_USD", "UK100_GBP"])
        self.assertEqual(self.get(self.p("/instruments?instruments=NOPE_USD")).status, 400)

    def test_configuration_sets_alias_and_margin_rate(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ PATCH .../configuration {alias, marginRate}: 200 clientConfigureTransaction; 400 with
        clientConfigureRejectTransaction (https://developer.oanda.com/rest-live-v20/transaction-df/ CLIENT_CONFIGURE_DATA_MISSING, MARGIN_RATE_INVALID)."""
        r = self.call("PATCH", self.p("/configuration"), {"alias": "Mine", "marginRate": "0.05"})
        self.assertEqual(r.status, 200, r.raw)
        self.assertEqual(r.json["clientConfigureTransaction"]["type"], "CLIENT_CONFIGURE")
        acct = self.get(self.p("/summary")).json["account"]
        self.assertEqual((acct["alias"], acct["marginRate"]), ("Mine", "0.05"))
        r = self.call("PATCH", self.p("/configuration"), {})
        self.assertEqual((r.status, r.json["errorCode"]), (400, "CLIENT_CONFIGURE_DATA_MISSING"))
        self.assertEqual(r.json["clientConfigureRejectTransaction"]["type"], "CLIENT_CONFIGURE_REJECT")
        r = self.call("PATCH", self.p("/configuration"), {"marginRate": "7"})
        self.assertEqual(r.json["errorCode"], "MARGIN_RATE_INVALID")

    def test_the_account_opened_with_create_and_a_deposit(self):
        """https://developer.oanda.com/rest-live-v20/transaction-df/ CreateTransaction and TransferFundsTransaction; the starting balance is the stand-in's own."""
        first = self.get(self.p("/transactions/idrange?from=1&to=2")).json["transactions"]
        self.assertEqual([t["type"] for t in first], ["CREATE", "TRANSFER_FUNDS"])
        self.assertEqual(first[0]["homeCurrency"], "GBP")
        self.assertEqual((first[1]["fundingReason"], first[1]["accountBalance"]), ("CLIENT_FUNDING", "100000.0000"))


# ---------------------------------------------------------------------------------------------------------------
# Prices: the snapshot and the stream
# ---------------------------------------------------------------------------------------------------------------

class Pricing(Base):
    def test_a_snapshot_carries_bids_asks_and_liquidity(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ GET .../pricing: prices (ClientPrice), time; https://developer.oanda.com/rest-live-v20/pricing-common-df/ PriceBucket {price string,
        liquidity}; https://developer.oanda.com/rest-live-v20/pricing-df/ ClientPrice fields."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        r = self.get(self.p("/pricing?instruments=EUR_USD,UK100_GBP"))
        self.assertEqual(r.status, 200, r.raw)
        self.assertEqual(list(r.json), ["prices", "time"])
        eu = r.json["prices"][0]
        self.assertEqual(list(eu)[:len(PRICE_KEYS)], PRICE_KEYS)
        self.assertEqual((eu["type"], eu["instrument"], eu["status"], eu["tradeable"]), ("PRICE", "EUR_USD", "tradeable", True))
        self.assertEqual(eu["bids"][0], {"price": "1.10000", "liquidity": 1000000})
        self.assertEqual(eu["asks"][0]["price"], "1.10014")
        self.assertTrue(all(isinstance(b["liquidity"], int) for b in eu["bids"]))
        self.assertLess(D(eu["bids"][1]["price"]), D(eu["bids"][0]["price"]))
        self.assertRegex(eu["time"], RFC)
        self.assertEqual(r.json["prices"][1]["instrument"], "UK100_GBP")
        self.assertRegex(r.json["prices"][1]["bids"][0]["price"], r"^\d+\.\d$")

    def test_since_home_conversions_and_the_deprecated_fields(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/: since ("only prices ... changed after"), includeHomeConversions (HomeConversions currency,
        accountGain, accountLoss, positionValue), includeUnitsAvailable; https://developer.oanda.com/rest-live-v20/pricing-df/ deprecated
        quoteHomeConversionFactors."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        first = self.get(self.p("/pricing?instruments=EUR_USD")).json
        later = self.get(self.p("/pricing?instruments=EUR_USD&since=" + first["time"])).json
        self.assertEqual(later["prices"], [])
        self.hold("EUR_USD", "1.10010", "1.10024")
        later = self.get(self.p("/pricing?instruments=EUR_USD&since=" + first["time"])).json
        self.assertEqual(len(later["prices"]), 1)
        r = self.get(self.p("/pricing?instruments=EUR_USD,XAU_USD&includeHomeConversions=true")).json
        self.assertEqual([h["currency"] for h in r["homeConversions"]], ["EUR", "USD", "XAU"])
        for h in r["homeConversions"]:
            self.assertEqual(list(h), ["currency", "accountGain", "accountLoss", "positionValue"])
            self.assertLessEqual(D(h["accountGain"]), D(h["positionValue"]))
            self.assertGreaterEqual(D(h["accountLoss"]), D(h["positionValue"]))
        p = r["prices"][0]
        self.assertEqual(list(p["unitsAvailable"]), ["default", "reduceFirst", "reduceOnly", "openOnly"])
        self.assertIn("positiveUnits", p["quoteHomeConversionFactors"])
        self.assertEqual(self.get(self.p("/pricing?instruments=EUR_USD&includeUnitsAvailable=false")).json["prices"][0]
                         .get("unitsAvailable"), None)
        self.assertEqual(self.get(self.p("/pricing")).status, 400)
        self.assertEqual(self.get(self.p("/pricing?instruments=EUR_XXX")).status, 400)

    def test_the_stream_is_chunked_json_lines_with_a_snapshot_and_heartbeats(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ GET .../pricing/stream "served from the streaming URLs": chunked, one JSON object per line,
        Content-Type application/octet-stream, heartbeats {type HEARTBEAT, time}; snapshot on connect (default true)."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        s = self.stream("/pricing/stream?instruments=EUR_USD,XAU_USD")
        self.assertEqual(s.status, 200)
        self.assertEqual(s.resp.getheader("Content-Type"), "application/octet-stream")
        self.assertEqual(s.resp.getheader("Transfer-Encoding"), "chunked")
        self.assertTrue(s.wait(lambda o: o.get("type") == "HEARTBEAT", 3))
        prices = s.objs("PRICE")
        self.assertEqual([p["instrument"] for p in prices[:2]], ["EUR_USD", "XAU_USD"])
        self.assertEqual(prices[0]["bids"][0]["price"], "1.10000")
        hb = s.objs("HEARTBEAT")[0]
        self.assertEqual(sorted(hb), ["time", "type"])

    def test_the_snapshot_repeats_the_latest_price_with_its_original_time(self):
        """measured on a live account: on connecting, a pricing stream first sends each instrument's latest
        price with its original time; https://developer.oanda.com/rest-live-v20/pricing-ep/ snapshot=false sends none."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        t = self.get(self.p("/pricing?instruments=EUR_USD")).json["prices"][0]["time"]
        time.sleep(0.3)
        s = self.stream("/pricing/stream?instruments=EUR_USD")
        first = s.wait(lambda o: o.get("type") == "PRICE", 3)
        self.assertEqual(first["time"], t)
        quiet = self.stream("/pricing/stream?instruments=EUR_USD&snapshot=false")
        self.assertTrue(quiet.wait(lambda o: o.get("type") == "HEARTBEAT", 3))
        self.assertEqual(quiet.objs("PRICE"), [])

    def test_at_most_four_prices_a_second_each_the_one_in_effect_at_the_end_of_the_window(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/: "at most 4 prices per second (every 250 milliseconds) for each instrument"; "only the price in
        effect at the end of the window is sent"."""
        s = self.stream("/pricing/stream?instruments=EUR_USD&snapshot=false")
        time.sleep(0.4)
        for i in range(40):                                  # 40 prices in about 0.4 s
            self.hold("EUR_USD", "1.1%04d" % (i * 2), "1.1%04d" % (i * 2 + 2))
            time.sleep(0.01)
        self.hold("EUR_USD", "1.20000", "1.20010")
        last = s.wait(lambda o: o.get("type") == "PRICE" and o["bids"][0]["price"] == "1.20000", 3)
        self.assertTrue(last)
        got = [t for t, o in s.items if o.get("type") == "PRICE"]
        self.assertLessEqual(len(got), 6, "40 prices in 0.4 s come through as at most a few windows")
        gaps = [b - a for a, b in zip(got, got[1:])]
        self.assertTrue(all(g >= 0.2 for g in gaps), gaps)

    def test_a_closed_market_sends_one_non_tradeable_price_then_only_heartbeats(self):
        """measured on a live account: at the daily spell OANDA sends one PRICE with tradeable:false, then only
        HEARTBEATs; https://developer.oanda.com/rest-live-v20/pricing-df/ PriceStatus non-tradeable (deprecated status)."""
        s = self.stream("/pricing/stream?instruments=EUR_USD&snapshot=false")
        time.sleep(0.3)
        self.fake.set_market("closed", "EUR_USD")
        off = s.wait(lambda o: o.get("type") == "PRICE" and o["tradeable"] is False, 3)
        self.assertEqual(off["status"], "non-tradeable")
        n = len(s.objs("PRICE"))
        time.sleep(1.2)
        self.assertEqual(len(s.objs("PRICE")), n, "nothing but heartbeats while closed")
        self.assertTrue(s.objs("HEARTBEAT"))
        snap = self.get(self.p("/pricing?instruments=EUR_USD")).json["prices"][0]
        self.assertIs(snap["tradeable"], False)
        self.fake.set_market("open", "EUR_USD")
        self.assertTrue(s.wait(lambda o: o.get("type") == "PRICE" and o["tradeable"] is True, 3))

    def test_streams_end_from_the_servers_side_cleanly_or_not(self):
        """measured on a live account: OANDA ends streams from its side every few hours, sometimes all of an
        account's at once. Clean: the chunked body ends; abrupt: the socket ends mid-stream."""
        a = self.stream("/pricing/stream?instruments=EUR_USD")
        b = self.stream("/transactions/stream")
        self.assertTrue(a.wait(lambda o: True, 3))
        self.assertEqual(self.fake.drop_streams(clean=True), 2)
        self.assertTrue(a.ended.wait(3) and b.ended.wait(3))
        self.assertIsNone(a.error)
        c = self.stream("/pricing/stream?instruments=EUR_USD")
        self.assertTrue(c.wait(lambda o: True, 3))
        self.fake.drop_streams(clean=False)
        self.assertTrue(c.ended.wait(3))

    def test_streams_are_only_on_the_streaming_host(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/, https://developer.oanda.com/rest-live-v20/transaction-ep/: the streams are "served by the streaming URLs"; the REST host refusing them and
        the streaming host refusing REST paths is the stand-in's own."""
        self.assertEqual(self.get(self.p("/pricing/stream?instruments=EUR_USD")).status, 404)
        c = http.client.HTTPConnection("127.0.0.1", self.fake.stream_port, timeout=5)
        c.request("GET", self.p("/summary"), headers={"Authorization": "Bearer " + self.fake.token})
        self.assertEqual(c.getresponse().status, 404)
        c.close()

    def test_heartbeats_every_five_seconds_by_default(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ "Heartbeats are sent every 5 seconds"; https://developer.oanda.com/rest-live-v20/transaction-ep/ "TransactionHeartbeats are sent every 5
        seconds"."""
        with FakeOanda(new_connections_per_second=None) as five:
            s = Stream(five, "/v3/accounts/%s/transactions/stream" % five.account_ids[0])
            try:
                beats = lambda: [t for t, o in list(s.items) if o.get("type") == "HEARTBEAT"]     # noqa: E731
                self.assertTrue(until(lambda: len(beats()) >= 2, 13))
                hb = beats()[:2]
                self.assertAlmostEqual(hb[1] - hb[0], 5.0, delta=0.5)
            finally:
                s.close()


# ---------------------------------------------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------------------------------------------

class Candles(Base):
    def test_every_granularity_lines_up_with_its_documented_alignment(self):
        """https://developer.oanda.com/rest-live-v20/instrument-df/ CandlestickGranularity: S5..M1 minute alignment, M2..H1 hour alignment, H2..D day alignment, W
        start of the week, M first day of the month; https://developer.oanda.com/rest-live-v20/pricing-ep/ dailyAlignment, alignmentTimezone, weeklyAlignment.
        At dailyAlignment=0 alignmentTimezone=UTC every day-aligned candle starts on a UTC multiple of its length."""
        fixed = fake_oanda.GRANULARITIES
        for g in fixed:
            c = self.candles("EUR_USD", granularity=g, count=6, price="B", dailyAlignment=0, alignmentTimezone="UTC",
                             weeklyAlignment="Monday")
            self.assertEqual((c["instrument"], c["granularity"]), ("EUR_USD", g))
            rows = c["candles"]
            self.assertEqual(len(rows), 6, g)
            times = [ms_of(x["time"]) for x in rows]
            self.assertEqual(times, sorted(set(times)), g)
            for x, t in zip(rows, times):
                self.assertEqual(list(x), ["time", "bid", "volume", "complete"])
                if fixed[g] is not None:
                    self.assertEqual(t % fixed[g], 0, (g, x["time"]))
                elif g == "W":
                    self.assertEqual(datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%a %H:%M"), "Mon 00:00")
                else:
                    self.assertEqual(datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%d %H:%M"), "01 00:00")
            self.assertTrue(all(x["complete"] for x in rows[:-1]), g)

    def test_the_default_day_starts_at_five_pm_new_york_and_the_week_on_friday(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/: dailyAlignment default 17, alignmentTimezone default America/New_York, weeklyAlignment default
        Friday; "the returned times will still be represented in UTC"."""
        z = fake_oanda.NEW_YORK
        for x in self.candles("EUR_USD", granularity="D", count=8)["candles"]:
            s = ms_of(x["time"]) // 1000
            self.assertEqual(z.local_hms(s), (17, 0, 0), x["time"])
        for x in self.candles("EUR_USD", granularity="W", count=4)["candles"]:
            s = ms_of(x["time"]) // 1000
            self.assertEqual((z.local_date(s).weekday(), z.local_hms(s)), (4, (17, 0, 0)), x["time"])
        for x in self.candles("EUR_USD", granularity="H4", count=12)["candles"]:
            s = ms_of(x["time"]) // 1000
            self.assertEqual((z.local_hms(s)[0] - 17) % 4, 0, x["time"])

    def test_count_defaults_to_500_and_stops_at_5000(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ count "[default=500, maximum=5000]"; "Count should not be specified if both the start and end
        parameters are provided"."""
        self.assertEqual(len(self.candles("EUR_USD", granularity="M1")["candles"]), 500)
        rows = self.candles("EUR_USD", granularity="M1", count=5000, price="B")["candles"]
        self.assertEqual(len(rows), 5000)
        self.assertEqual(self.candles("EUR_USD", 400, granularity="M1", count=5001)["errorMessage"],
                         "Maximum value for 'count' exceeded")
        a, b = rows[0]["time"], rows[-1]["time"]
        self.candles("EUR_USD", 400, granularity="M1", count=10, **{"from": a, "to": b})
        self.candles("EUR_USD", 400, granularity="S5", **{"from": "2025-01-06T00:00:00Z", "to": "2025-01-20T00:00:00Z"})
        self.candles("EUR_USD", 400, granularity="M1", count=0)

    def test_a_5000_bar_chart_pages_back_without_a_gap_or_an_overlap(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ from, to, count; includeFirst (default true). Paging back from `to`: the next page ends where the
        last one began."""
        page1 = self.candles("EUR_USD", granularity="M5", count=5000, price="B", dailyAlignment=0,
                             alignmentTimezone="UTC")["candles"]
        page2 = self.candles("EUR_USD", granularity="M5", count=5000, price="B", to=page1[0]["time"])["candles"]
        self.assertEqual(len(page2), 5000)
        self.assertLess(ms_of(page2[-1]["time"]), ms_of(page1[0]["time"]))
        self.assertEqual(ms_of(page1[0]["time"]) - ms_of(page2[-1]["time"]) >= 300000, True)
        both = [x["time"] for x in page2 + page1]
        self.assertEqual(len(both), len(set(both)))
        fwd = self.candles("EUR_USD", granularity="M5", count=3, price="B", **{"from": page2[-1]["time"]})["candles"]
        self.assertEqual(fwd[0]["time"], page2[-1]["time"])
        nxt = self.candles("EUR_USD", granularity="M5", count=3, price="B", includeFirst="false",
                           **{"from": page2[-1]["time"]})["candles"]
        self.assertEqual(nxt[0]["time"], page1[0]["time"])

    def test_bid_ask_and_mid_and_a_coarse_candle_is_the_aggregate_of_fine_ones(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ price "M", "B", "A" in any combination (https://developer.oanda.com/rest-live-v20/primitives-df/ PricingComponent); https://developer.oanda.com/rest-live-v20/instrument-df/ Candlestick
        {time, bid, ask, mid, volume, complete}, CandlestickData {o, h, l, c} strings; volume "The number of prices
        created during the time-range"."""
        hour = self.candles("EUR_USD", granularity="H1", count=3, price="BAM")["candles"][0]
        self.assertEqual(list(hour), ["time", "bid", "ask", "mid", "volume", "complete"])
        start = ms_of(hour["time"])
        mins = self.candles("EUR_USD", granularity="M1", price="BAM",
                            **{"from": hour["time"], "to": fake_oanda.rfc3339((start + 3600000) * 10 ** 6)})["candles"]
        for comp in ("bid", "ask", "mid"):
            agg = {"o": mins[0][comp]["o"], "c": mins[-1][comp]["c"],
                   "h": max((x[comp]["h"] for x in mins), key=D), "l": min((x[comp]["l"] for x in mins), key=D)}
            self.assertEqual(hour[comp], agg, comp)
            self.assertLess(D(hour["bid"]["l"]), D(hour["ask"]["l"]))
        self.assertEqual(hour["volume"], sum(x["volume"] for x in mins))
        for x in mins:
            for v in x["bid"].values():
                self.assertRegex(v, r"^\d\.\d{5}$")

    def test_units_widens_bid_and_ask_by_the_book(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ units "The number of units used to calculate the volume-weighted average bid and ask prices in the
        returned candles" (default 1)."""
        a = self.candles("EUR_USD", granularity="H1", count=2, price="BA")["candles"][0]
        b = self.candles("EUR_USD", granularity="H1", count=2, price="BA", units=8000000)["candles"][0]
        self.assertLess(D(b["bid"]["o"]), D(a["bid"]["o"]))
        self.assertGreater(D(b["ask"]["o"]), D(a["ask"]["o"]))

    def test_the_last_candle_is_incomplete_and_smooth_opens_at_the_previous_close(self):
        """https://developer.oanda.com/rest-live-v20/instrument-df/ complete: "A complete candlestick is one whose ending time is not in the future"; https://developer.oanda.com/rest-live-v20/pricing-ep/
        smooth: "A smoothed candlestick uses the previous candle's close price as its open price"."""
        rows = self.candles("EUR_USD", granularity="M1", count=4, price="B")["candles"]
        self.assertFalse(rows[-1]["complete"])
        self.assertTrue(all(x["complete"] for x in rows[:-1]))
        smooth = self.candles("EUR_USD", granularity="M1", count=4, price="B", smooth="true")["candles"]
        for a, b in zip(smooth, smooth[1:]):
            self.assertEqual(b["bid"]["o"], a["bid"]["c"])

    def test_history_goes_back_to_2005_and_no_further(self):
        """https://developer.oanda.com/rest-live-v20/introduction/: "Access historical pricing information dating back to 2005"."""
        rows = self.candles("EUR_USD", granularity="M", count=5000, price="M")["candles"]
        self.assertGreater(len(rows), 250)
        self.assertTrue(rows[0]["time"].startswith("2005-01"), rows[0]["time"])
        old = self.candles("EUR_USD", granularity="D", count=3, **{"from": "2004-06-01T00:00:00Z"})["candles"]
        self.assertGreaterEqual(old[0]["time"], "2005-01-01")

    def test_weekends_and_the_try_pairs_hours_have_no_candles(self):
        """https://www.oanda.com/uk-en/trading/hours-of-operation/: forex Sunday 17:05 to Friday 16:59 New York, TRY pairs Monday to Friday 08:00 to 17:00 Central Europe
        (measured on a live account: 07:00 to 16:00 London). History before the stand-in started follows the
        hours."""
        d = date.today() - timedelta(10)
        sat = d - timedelta((d.weekday() - 5) % 7)
        rows = self.candles("EUR_USD", granularity="H1", price="B", **{"from": sat.isoformat() + "T00:00:00Z",
                                                                       "to": sat.isoformat() + "T23:00:00Z"})["candles"]
        self.assertEqual(rows, [])
        wed = sat - timedelta(3)
        rows = self.candles("USD_TRY", granularity="H1", price="B", **{"from": wed.isoformat() + "T00:00:00Z",
                                                                       "to": wed.isoformat() + "T23:59:00Z"})["candles"]
        london = fake_oanda._ZONES["Europe/London"]
        hours = [london.local_hms(ms_of(x["time"]) // 1000)[0] for x in rows]
        self.assertEqual(hours, list(range(7, 16)))

    def test_refusals_of_a_bad_request(self):
        """https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ 400 "Invalid value specified for 'instrument'"; the same wording for the other
        parameters is the stand-in's own."""
        r = self.get(self.p("/instruments/NOPE_USD/candles?granularity=M1"))
        self.assertEqual((r.status, r.json), (400, {"errorMessage": "Invalid value specified for 'instrument'"}))
        for params in ({"granularity": "M3"}, {"price": "X"}, {"price": "BB"}, {"dailyAlignment": "24"},
                       {"alignmentTimezone": "Mars/Base"}, {"weeklyAlignment": "Funday"}, {"smooth": "yes"},
                       {"from": "yesterday"}, {"units": "-1"}):
            self.candles("EUR_USD", 400, **params)

    def test_latest_candles(self):
        """https://developer.oanda.com/rest-live-v20/pricing-ep/ GET .../candles/latest candleSpecifications "EUR_USD:S10:BM": latestCandles
        (Array[CandlestickResponse])."""
        r = self.get(self.p("/candles/latest?candleSpecifications=EUR_USD:M1:BM,XAU_USD:H1:A"))
        self.assertEqual(r.status, 200, r.raw)
        got = r.json["latestCandles"]
        self.assertEqual([(x["instrument"], x["granularity"]) for x in got], [("EUR_USD", "M1"), ("XAU_USD", "H1")])
        self.assertEqual(set(got[0]["candles"][-1]), {"time", "bid", "mid", "volume", "complete"})
        self.assertEqual(self.get(self.p("/candles/latest?candleSpecifications=EUR_USD:M1")).status, 400)

    def test_the_instrument_scoped_candles_are_refused(self):
        """The /v3/instruments/{instrument}/candles page (instrument-ep) answers 404 and has left the documentation as
        of 2026-11-14: the stand-in serves only the account-scoped endpoint."""
        self.assertEqual(self.get("/v3/instruments/EUR_USD/candles?granularity=M1").status, 404)


class CandlesUnderTheHours(Base):
    MARKET = "auto"

    def test_the_daily_spell_of_tradeable_false(self):
        """measured on a live account: every trading day at 16:59:05 New York time OANDA sends one PRICE per
        quoted pair with tradeable: false, until 17:04:55 New York; https://www.oanda.com/uk-en/trading/hours-of-operation/: "a six minute break between 16:59 -
        17:05"."""
        self.fake.set_clock(ny_instant(self.fake, 16, 59, 0))
        q = self.get(self.p("/pricing?instruments=EUR_USD")).json["prices"][0]
        self.assertIs(q["tradeable"], True)
        self.fake.advance(10)
        q = self.get(self.p("/pricing?instruments=EUR_USD")).json["prices"][0]
        self.assertIs(q["tradeable"], False)
        z = fake_oanda.NEW_YORK
        self.assertEqual(z.local_hms(ms_of(q["time"]) // 1000), (16, 59, 5))
        r = self.order(type="MARKET", instrument="EUR_USD", units="100")
        self.assertEqual(r["orderCancelTransaction"]["reason"], "MARKET_HALTED")
        self.fake.advance(5 * 60 + 47)
        q = self.get(self.p("/pricing?instruments=EUR_USD")).json["prices"][0]
        self.assertIs(q["tradeable"], True)
        self.assertEqual(z.local_hms(ms_of(q["time"]) // 1000)[:2], (17, 4))
        spell = self.candles("EUR_USD", granularity="M1", price="B",
                             **{"from": ny_instant_back(q["time"], 7 * 60), "to": q["time"]})["candles"]
        minutes = [z.local_hms(ms_of(x["time"]) // 1000)[:2] for x in spell]
        self.assertIn((16, 59), minutes)
        for m in ((17, 0), (17, 1), (17, 2), (17, 3)):
            self.assertNotIn(m, minutes)


def ny_instant_back(text, seconds):
    return fake_oanda.rfc3339(fake_oanda.parse_time(text) - seconds * 10 ** 9)


# ---------------------------------------------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------------------------------------------

class MarketOrders(Base):
    def test_a_market_order_fills_at_the_ask_and_opens_a_trade(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ POST .../orders: 201 "The Order was created as specified", Location, orderCreateTransaction
        (MARKET_ORDER, reason CLIENT_ORDER, timeInForce FOK by default, positionFill DEFAULT), orderFillTransaction
        (ORDER_FILL, tradeOpened {tradeID, units, price}), relatedTransactionIDs, lastTransactionID; https://developer.oanda.com/rest-live-v20/trade-df/: the
        TradeID is the id of the transaction that opened it."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        r = self.call("POST", self.p("/orders"), {"order": {"type": "MARKET", "instrument": "EUR_USD", "units": "100"}})
        self.assertEqual(r.status, 201, r.raw)
        j = r.json
        self.assertEqual(list(j), ["orderCreateTransaction", "orderFillTransaction", "relatedTransactionIDs",
                                   "lastTransactionID"])
        c, f = j["orderCreateTransaction"], j["orderFillTransaction"]
        self.assertEqual((c["type"], c["reason"], c["timeInForce"], c["positionFill"], c["units"]),
                         ("MARKET_ORDER", "CLIENT_ORDER", "FOK", "DEFAULT", "100"))
        self.assertEqual(list(c)[:7], ["id", "time", "userID", "accountID", "batchID", "requestID", "type"])
        self.assertEqual((f["type"], f["reason"], f["orderID"], f["price"], f["units"]),
                         ("ORDER_FILL", "MARKET_ORDER", c["id"], "1.10014", "100"))
        self.assertEqual(f["tradeOpened"]["tradeID"], f["id"])
        self.assertEqual(f["batchID"], c["id"])
        self.assertEqual(j["relatedTransactionIDs"], [c["id"], f["id"]])
        self.assertEqual(j["lastTransactionID"], f["id"])
        self.assertTrue(r.headers["Location"].endswith("/v3/accounts/%s/orders/%s" % (self.A, c["id"])))
        self.assertEqual(f["requestID"], r.headers["RequestID"])
        for key in ("pl", "financing", "commission", "accountBalance", "halfSpreadCost"):
            self.assertRegex(f[key], MONEY, key)
        self.assertEqual(sorted(f["fullPrice"]), ["asks", "bids", "closeoutAsk", "closeoutBid", "timestamp"])
        t = self.trades()[0]
        self.assertEqual(list(t)[:len(TRADE_KEYS)], TRADE_KEYS)
        self.assertEqual((t["id"], t["price"], t["currentUnits"], t["state"]), (f["id"], "1.10014", "100", "OPEN"))

    def test_a_sell_fills_at_the_bid_and_a_large_one_walks_the_book(self):
        """https://developer.oanda.com/rest-live-v20/pricing-common-df/ PriceBucket; https://developer.oanda.com/rest-live-v20/order-df/: a fill's price for its size (fullVWAP)."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        f = self.order(type="MARKET", instrument="EUR_USD", units="-100")["orderFillTransaction"]
        self.assertEqual((f["price"], f["units"]), ("1.10000", "-100"))
        f = self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="2000000")["orderFillTransaction"]
        self.assertEqual(D(f["fullVWAP"]), (D("1.10014") + D("1.10015")) / 2)

    def test_a_closed_market_cancels_a_market_order_at_once(self):
        """https://www.oanda.com/uk-en/trading/hours-of-operation/: "Existing positions cannot be closed, and new positions cannot be opened, when markets are closed";
        https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason MARKET_HALTED; https://developer.oanda.com/rest-live-v20/order-ep/ orderCancelTransaction "Only provided when the Order
        was immediately cancelled"."""
        self.fake.set_market("closed", "EUR_USD")
        j = self.order(type="MARKET", instrument="EUR_USD", units="100")
        self.assertEqual((j["orderCancelTransaction"]["type"], j["orderCancelTransaction"]["reason"]),
                         ("ORDER_CANCEL", "MARKET_HALTED"))
        self.assertNotIn("orderFillTransaction", j)
        o = self.get(self.p("/orders/" + j["orderCreateTransaction"]["id"])).json["order"]
        self.assertEqual(o["state"], "CANCELLED")

    def test_not_enough_margin_cancels_it(self):
        """execution policy 8.2 (https://legal.oanda.com/oel/execution_policy_oel/en): "The Trading Platforms will reject
        Orders and Trade requests if you have insufficient Margin available"; OrderCancelReason INSUFFICIENT_MARGIN."""
        self.hold("XAU_USD", "2000.000", "2000.400")
        j = self.order(acct=self.H, type="MARKET", instrument="XAU_USD", units="2000")
        self.assertEqual(j["orderCancelTransaction"]["reason"], "INSUFFICIENT_MARGIN")
        self.assertEqual(self.trades(self.H), [])

    def test_fill_or_kill_against_the_book_and_immediate_or_cancel_in_part(self):
        """execution policy (https://legal.oanda.com/oel/execution_policy_oel/en) Annex 1: Market FOK "There are no partial fills ... rejected/cancelled in the case where the
        order size exceeds available liquidity"; https://developer.oanda.com/rest-live-v20/order-df/ TimeInForce IOC "Immediately partially filled Or Cancelled";
        OrderCancelReason INSUFFICIENT_LIQUIDITY. The control next_fill shapes the book."""
        self.fake.next_fill("EUR_USD", 400)
        j = self.order(type="MARKET", instrument="EUR_USD", units="1000")
        self.assertEqual(j["orderCancelTransaction"]["reason"], "INSUFFICIENT_LIQUIDITY")
        self.assertNotIn("orderFillTransaction", j)
        self.fake.next_fill("EUR_USD", 400)
        j = self.order(type="MARKET", instrument="EUR_USD", units="1000", timeInForce="IOC")
        self.assertEqual(j["orderFillTransaction"]["units"], "400")
        self.assertEqual(j["orderCancelTransaction"]["reason"], "INSUFFICIENT_LIQUIDITY")
        self.assertEqual([t["type"] for t in self.txs(since=int(j["orderCreateTransaction"]["id"]) - 1)],
                         ["MARKET_ORDER", "ORDER_FILL", "ORDER_CANCEL"])
        j = self.order(type="MARKET", instrument="EUR_USD", units="1000")
        self.assertIn("orderFillTransaction", j, "the cap was for one fill")

    def test_a_price_bound_cancels_a_fill_beyond_it(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ MarketOrder priceBound "The worst price that the client is willing to have the Market Order filled
        at"; OrderCancelReason BOUNDS_VIOLATION."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        j = self.order(type="MARKET", instrument="EUR_USD", units="100", priceBound="1.10010")
        self.assertEqual(j["orderCancelTransaction"]["reason"], "BOUNDS_VIOLATION")
        j = self.order(type="MARKET", instrument="EUR_USD", units="100", priceBound="1.10020")
        self.assertIn("orderFillTransaction", j)

    def test_documented_rejections(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ 400: orderRejectTransaction (MARKET_ORDER_REJECT with rejectReason), relatedTransactionIDs,
        lastTransactionID, errorCode, errorMessage; https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionRejectReason."""
        cases = [({"instrument": "EUR_USD", "units": "1.5"}, "UNITS_PRECISION_EXCEEDED"),
                 ({"instrument": "EUR_USD", "units": "0"}, "UNITS_INVALID"),
                 ({"instrument": "EUR_USD"}, "UNITS_MISSING"),
                 ({"instrument": "UK100_GBP", "units": "0.05"}, "UNITS_PRECISION_EXCEEDED"),
                 ({"instrument": "UK100_GBP", "units": "2000"}, "UNITS_LIMIT_EXCEEDED"),
                 ({"instrument": "NOPE_USD", "units": "1"}, "INSTRUMENT_UNKNOWN"),
                 ({"units": "1"}, "INSTRUMENT_MISSING"),
                 ({"instrument": "EUR_USD", "units": "1", "timeInForce": "GTC"}, "TIME_IN_FORCE_INVALID"),
                 ({"instrument": "EUR_USD", "units": "1", "positionFill": "SOMETIMES"}, "ORDER_FILL_POSITION_ACTION_INVALID"),
                 ({"instrument": "EUR_USD", "units": "1", "takeProfitOnFill": {"price": "1.123456"}},
                  "TAKE_PROFIT_ON_FILL_PRICE_PRECISION_EXCEEDED"),
                 ({"instrument": "EUR_USD", "units": "1", "stopLossOnFill": {"price": "1.0", "distance": "0.01"}},
                  "STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_SPECIFIED"),
                 ({"instrument": "EUR_USD", "units": "1", "trailingStopLossOnFill": {"distance": "0.00001"}},
                  "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MINIMUM_NOT_MET"),
                 ({"instrument": "EUR_USD", "units": "1", "guaranteedStopLossOnFill": {"price": "1.0"}},
                  "GUARANTEED_STOP_LOSS_ON_FILL_NOT_ALLOWED"),
                 ({"instrument": "EUR_USD", "units": "1", "clientExtensions": {"id": "x" * 200}}, "CLIENT_ORDER_ID_INVALID")]
        for fields, reason in cases:
            r = self.call("POST", self.p("/orders"), {"order": dict(fields, type="MARKET")})
            self.assertEqual(r.status, 400, (fields, r.raw))
            j = r.json
            self.assertEqual(list(j), ["orderRejectTransaction", "relatedTransactionIDs", "lastTransactionID",
                                       "errorCode", "errorMessage"])
            self.assertEqual((j["orderRejectTransaction"]["type"], j["orderRejectTransaction"]["rejectReason"],
                              j["errorCode"]), ("MARKET_ORDER_REJECT", reason, reason), fields)
            self.assertEqual(j["relatedTransactionIDs"], [j["orderRejectTransaction"]["id"]])
        self.assertEqual(self.call("POST", self.p("/orders"), {"nothing": 1}).json,
                         {"errorMessage": "The request was missing required data"})
        self.assertEqual(self.call("POST", self.p("/orders"), "{not json").status, 400)

    def test_a_take_profit_or_stop_loss_that_would_fill_at_once_cancels_the_order(self):
        """https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason TAKE_PROFIT_ON_FILL_LOSS, STOP_LOSS_ON_FILL_LOSS ("would have been filled
        immediately"), LOSING_TAKE_PROFIT."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        for fields, reason in (({"takeProfitOnFill": {"price": "1.09990"}}, "TAKE_PROFIT_ON_FILL_LOSS"),
                               ({"takeProfitOnFill": {"price": "1.10010"}}, "LOSING_TAKE_PROFIT"),
                               ({"stopLossOnFill": {"price": "1.10005"}}, "STOP_LOSS_ON_FILL_LOSS")):
            j = self.order(type="MARKET", instrument="EUR_USD", units="100", **fields)
            self.assertEqual(j["orderCancelTransaction"]["reason"], reason)

    def test_a_refusal_asked_for_by_a_control(self):
        """The control refuse_next: a documented TransactionRejectReason answers 400 with a *_REJECT; a documented
        OrderCancelReason creates the order and cancels it at once."""
        self.fake.refuse_next("ACCOUNT_LOCKED")
        r = self.call("POST", self.p("/orders"), {"order": {"type": "MARKET", "instrument": "EUR_USD", "units": "1"}})
        self.assertEqual((r.status, r.json["errorCode"], r.json["errorMessage"]), (400, "ACCOUNT_LOCKED", "The Account is locked"))
        self.fake.refuse_next("MARKET_HALTED")
        j = self.order(type="MARKET", instrument="EUR_USD", units="1")
        self.assertEqual(j["orderCancelTransaction"]["reason"], "MARKET_HALTED")
        self.assertIn("orderFillTransaction", self.order(type="MARKET", instrument="EUR_USD", units="1"))
        with self.assertRaises(ValueError):
            self.fake.refuse_next("NOT_A_REASON")
        with self.assertRaises(ValueError):
            self.fake.refuse_next("MARKET_HALTED", kind="cancel")
        self.fake.refuse_next("ACCOUNT_LOCKED", kind="position_close")
        r = self.call("PUT", self.p("/positions/EUR_USD/close"), {"longUnits": "ALL"})
        self.assertEqual((r.status, r.json["errorCode"], r.json["longOrderRejectTransaction"]["type"]),
                         (400, "ACCOUNT_LOCKED", "MARKET_ORDER_REJECT"))
        self.fake.refuse_next("MARKET_HALTED", kind="position_close")
        j = self.put("/positions/EUR_USD/close", {"longUnits": "ALL"})
        self.assertEqual(j["longOrderCancelTransaction"]["reason"], "MARKET_HALTED")
        self.assertEqual(self.put("/positions/EUR_USD/close", {"longUnits": "ALL"})["longOrderFillTransaction"]["units"],
                         "-1")


class RestingOrders(Base):
    def test_a_limit_order_rests_and_fills_when_the_price_comes_to_it(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ LimitOrderRequest (timeInForce GTC default, triggerCondition DEFAULT: "compare its price to the ask
        for long Orders"); execution policy (https://legal.oanda.com/oel/execution_policy_oel/en) "Limit GTC Full"; OrderFillReason LIMIT_ORDER."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        j = self.order(type="LIMIT", instrument="EUR_USD", units="100", price="1.09950")
        c = j["orderCreateTransaction"]
        self.assertEqual((c["type"], c["timeInForce"], c["triggerCondition"]), ("LIMIT_ORDER", "GTC", "DEFAULT"))
        self.assertEqual(list(j), ["orderCreateTransaction", "relatedTransactionIDs", "lastTransactionID"])
        pend = self.get(self.p("/pendingOrders")).json["orders"]
        self.assertEqual([(o["id"], o["state"], o["type"]) for o in pend], [(c["id"], "PENDING", "LIMIT")])
        self.hold("EUR_USD", "1.09940", "1.09949")
        o = self.get(self.p("/orders/" + c["id"])).json["order"]
        self.assertEqual(o["state"], "FILLED")
        fill = self.get(self.p("/transactions/" + o["fillingTransactionID"])).json["transaction"]
        self.assertEqual((fill["reason"], fill["price"]), ("LIMIT_ORDER", "1.09949"))
        self.assertNotIn("requestID", fill, "requestID only on transactions a client request made")
        self.assertEqual(o["tradeOpenedID"], fill["id"])

    def test_a_marketable_limit_fills_at_once_at_the_better_price(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ orderFillTransaction "Only provided when the Order was immediately filled"; execution policy (https://legal.oanda.com/oel/execution_policy_oel/en): a limit
        fills "provided that the most recent price is the same or better than the specified client limit price"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        j = self.order(type="LIMIT", instrument="EUR_USD", units="100", price="1.10100")
        self.assertEqual(j["orderFillTransaction"]["price"], "1.10014")

    def test_stop_and_market_if_touched(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ StopOrder, MarketIfTouchedOrder (initialMarketPrice); execution policy (https://legal.oanda.com/oel/execution_policy_oel/en) "Market GTC Full": a stop is
        triggered when the price "is the same or worse than the specified trigger level"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        stop = self.order(type="STOP", instrument="EUR_USD", units="100", price="1.10100")["orderCreateTransaction"]
        mit = self.order(type="MARKET_IF_TOUCHED", instrument="EUR_USD", units="-100", price="1.10200")["orderCreateTransaction"]
        self.assertIn("initialMarketPrice", self.get(self.p("/orders/" + mit["id"])).json["order"])
        self.hold("EUR_USD", "1.10090", "1.10104")
        self.assertEqual(self.get(self.p("/orders/" + stop["id"])).json["order"]["state"], "FILLED")
        self.assertEqual(self.get(self.p("/orders/" + mit["id"])).json["order"]["state"], "PENDING")
        self.hold("EUR_USD", "1.10200", "1.10214")
        self.assertEqual(self.get(self.p("/orders/" + mit["id"])).json["order"]["state"], "FILLED")

    def test_a_triggered_order_the_book_cannot_take_waits_in_full(self):
        """https://legal.oanda.com/oel/execution_policy_oel/en Annex 1, "Market GTC Full": "In the case where the order
        size exceeds available liquidity, then the order returns to the pending state in the OANDA group order book in the
        full amount"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        o = self.order(type="STOP", instrument="EUR_USD", units="1000", price="1.10100")["orderCreateTransaction"]
        self.fake.set_liquidity("EUR_USD", 500)
        self.hold("EUR_USD", "1.10100", "1.10114")
        self.assertEqual(self.get(self.p("/orders/" + o["id"])).json["order"]["state"], "PENDING")
        self.fake.set_liquidity("EUR_USD", None)
        self.fake.settle()
        self.hold("EUR_USD", "1.10101", "1.10115")
        order = self.get(self.p("/orders/" + o["id"])).json["order"]
        self.assertEqual(order["state"], "FILLED")
        fill = self.get(self.p("/transactions/" + order["fillingTransactionID"])).json["transaction"]
        self.assertEqual(fill["units"], "1000")

    def test_cancel_and_cancel_again(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ PUT .../orders/{orderSpecifier}/cancel: 200 orderCancelTransaction (reason CLIENT_REQUEST); 404 "The
        Account or Order specified does not exist." with orderCancelRejectTransaction (ORDER_DOESNT_EXIST)."""
        o = self.order(type="LIMIT", instrument="EUR_USD", units="100", price="0.50000")["orderCreateTransaction"]
        j = self.put("/orders/%s/cancel" % o["id"])
        self.assertEqual((j["orderCancelTransaction"]["type"], j["orderCancelTransaction"]["reason"]),
                         ("ORDER_CANCEL", "CLIENT_REQUEST"))
        j = self.put("/orders/%s/cancel" % o["id"], expect=404)
        self.assertEqual((j["orderCancelRejectTransaction"]["rejectReason"], j["errorCode"]),
                         ("ORDER_DOESNT_EXIST", "ORDER_DOESNT_EXIST"))
        self.assertEqual(self.get(self.p("/orders/999999")).json, {"errorMessage": "The order ID specified does not exist"})

    def test_replace_is_cancel_and_create_with_a_new_id(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ PUT .../orders/{orderSpecifier}: "Replace an Order ... by simultaneously cancelling it and creating a
        replacement Order": 201 orderCancelTransaction (CLIENT_REQUEST_REPLACED, replacedByOrderID),
        orderCreateTransaction (reason REPLACEMENT, replacesOrderID), Location; REPLACING_ORDER_INVALID "The Order to be
        replaced has a different type than the replacing Order."."""
        old = self.order(type="LIMIT", instrument="EUR_USD", units="100", price="0.50000",
                         clientExtensions={"id": "mine"})["orderCreateTransaction"]
        r = self.call("PUT", self.p("/orders/@mine"), {"order": {"type": "LIMIT", "instrument": "EUR_USD", "units": "200",
                                                                 "price": "0.51000", "clientExtensions": {"id": "mine"}}})
        self.assertEqual(r.status, 201, r.raw)
        cancel, create = r.json["orderCancelTransaction"], r.json["orderCreateTransaction"]
        self.assertEqual((cancel["orderID"], cancel["reason"], cancel["replacedByOrderID"]),
                         (old["id"], "CLIENT_REQUEST_REPLACED", create["id"]))
        self.assertEqual((create["reason"], create["replacesOrderID"], create["units"]), ("REPLACEMENT", old["id"], "200"))
        self.assertNotEqual(create["id"], old["id"])
        self.assertTrue(r.headers["Location"].endswith("/orders/" + create["id"]))
        self.assertEqual(self.get(self.p("/orders/@mine")).json["order"]["id"], create["id"])
        r = self.call("PUT", self.p("/orders/" + create["id"]), {"order": {"type": "STOP", "instrument": "EUR_USD",
                                                                           "units": "1", "price": "2.00000"}})
        self.assertEqual((r.status, r.json["errorCode"]), (400, "REPLACING_ORDER_INVALID"))
        self.assertEqual(r.json["orderRejectTransaction"]["intendedReplacesOrderID"], create["id"])

    def test_client_ids_are_unique_only_among_pending_orders(self):
        """https://developer.oanda.com/rest-live-v20/transaction-df/ CLIENT_ORDER_ID_ALREADY_EXISTS "already assigned to another pending Order"; https://developer.oanda.com/rest-live-v20/order-df/
        OrderSpecifier "@" + client id. An "@id" resolving only pending orders is the stand-in's own (an option)."""
        self.order(type="LIMIT", instrument="EUR_USD", units="1", price="0.50000", clientExtensions={"id": "a"})
        r = self.call("POST", self.p("/orders"), {"order": {"type": "LIMIT", "instrument": "EUR_USD", "units": "1",
                                                            "price": "0.50000", "clientExtensions": {"id": "a"}}})
        self.assertEqual(r.json["errorCode"], "CLIENT_ORDER_ID_ALREADY_EXISTS")
        f = self.order(type="MARKET", instrument="EUR_USD", units="1", clientExtensions={"id": "m"})["orderFillTransaction"]
        self.assertEqual(f["clientOrderID"], "m")
        self.assertEqual(self.get(self.p("/orders/@m")).status, 404)
        self.fake.set_options(client_id_lookup="any")
        self.assertEqual(self.get(self.p("/orders/@m")).json["order"]["state"], "FILLED")
        self.order(type="MARKET", instrument="EUR_USD", units="1", clientExtensions={"id": "m"})

    def test_gtd_and_gfd_expire(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ TimeInForce GTD "cancelled at the provided time", GFD "cancelled at 5pm New York time";
        TIME_IN_FORCE_GTD_TIMESTAMP_MISSING, _IN_PAST; OrderCancelReason TIME_IN_FORCE_EXPIRED."""
        now = self.fake.clock.now_ns()
        gtd = self.order(type="LIMIT", instrument="EUR_USD", units="1", price="0.50000", timeInForce="GTD",
                         gtdTime=fake_oanda.rfc3339(now + 60 * 10 ** 9))["orderCreateTransaction"]
        gfd = self.order(type="LIMIT", instrument="EUR_USD", units="1", price="0.50000",
                         timeInForce="GFD")["orderCreateTransaction"]
        r = self.call("POST", self.p("/orders"), {"order": {"type": "LIMIT", "instrument": "EUR_USD", "units": "1",
                                                            "price": "0.5", "timeInForce": "GTD"}})
        self.assertEqual(r.json["errorCode"], "TIME_IN_FORCE_GTD_TIMESTAMP_MISSING")
        r = self.call("POST", self.p("/orders"), {"order": {"type": "LIMIT", "instrument": "EUR_USD", "units": "1",
                                                            "price": "0.5", "timeInForce": "GTD",
                                                            "gtdTime": "2020-01-01T00:00:00Z"}})
        self.assertEqual(r.json["errorCode"], "TIME_IN_FORCE_GTD_TIMESTAMP_IN_PAST")
        self.fake.advance(120)
        o = self.get(self.p("/orders/" + gtd["id"])).json["order"]
        self.assertEqual(o["state"], "CANCELLED")
        cancel = self.get(self.p("/transactions/" + o["cancellingTransactionID"])).json["transaction"]
        self.assertEqual(cancel["reason"], "TIME_IN_FORCE_EXPIRED")
        self.assertEqual(self.get(self.p("/orders/" + gfd["id"])).json["order"]["state"], "PENDING")
        self.fake.set_clock(ny_instant(self.fake, 17, 0, 30, weekdays=range(7)))
        self.assertEqual(self.get(self.p("/orders/" + gfd["id"])).json["order"]["state"], "CANCELLED")

    def test_lists_page_newest_first_with_a_link(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ GET .../orders: state (default PENDING), instrument, count (default 50, maximum 500), beforeID ("The
        maximum Order ID to return"), Link header "A link to the next page of results"."""
        ids = [self.order(type="LIMIT", instrument="EUR_USD", units="1", price="0.5%d" % i)["orderCreateTransaction"]["id"]
               for i in range(5)]
        r = self.get(self.p("/orders?count=2"))
        self.assertEqual([o["id"] for o in r.json["orders"]], ids[::-1][:2])
        link = r.headers["Link"]
        self.assertIn('rel="next"', link)
        self.assertIn("beforeID=%d" % (int(ids[3]) - 1), link)
        nxt = link[link.index("/v3/"):link.index(">")]
        self.assertEqual([o["id"] for o in self.get(nxt).json["orders"]], ids[::-1][2:4])
        self.assertEqual(self.get(self.p("/orders?count=501")).status, 400)
        self.assertEqual(self.get(self.p("/orders?state=BOGUS")).status, 400)
        self.assertEqual(len(self.get(self.p("/orders?state=ALL&count=500")).json["orders"]), 5)

    def test_several_pending_orders_at_one_price_are_noted(self):
        """Terms of Business 13.11(f)(iii) (https://legal.oanda.com/oel/fxtrade_customer_agreement_oel/en): "placing
        multiple smaller sized pending orders at or near the same price level instead of placing a single order". OANDA's
        reaction is not documented; the stand-in notes it for tests."""
        self.order(type="LIMIT", instrument="EUR_USD", units="10", price="0.50000")
        self.assertEqual(self.fake.violations(), [])
        self.order(type="LIMIT", instrument="EUR_USD", units="10", price="0.50000")
        self.assertIn("13.11", self.fake.violations()[0]["rule"])

    def test_client_extensions_on_an_mt4_account_are_noted(self):
        """https://developer.oanda.com/rest-live-v20/transaction-df/ ClientExtensions: "Do not set, modify, or delete this field if your account is associated with
        MT4". What OANDA does then is not documented; the stand-in takes the order and notes it."""
        self.order(acct=self.M, type="MARKET", instrument="EUR_USD", units="1")
        self.assertEqual(self.fake.violations(), [])
        self.order(acct=self.M, type="MARKET", instrument="EUR_USD", units="1", clientExtensions={"id": "x"})
        self.assertIn("MT4", self.fake.violations()[0]["rule"])

    def test_order_client_extensions_can_be_changed(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ PUT .../orders/{orderSpecifier}/clientExtensions: 200 orderClientExtensionsModifyTransaction; 400
        CLIENT_EXTENSIONS_DATA_MISSING."""
        o = self.order(type="LIMIT", instrument="EUR_USD", units="1", price="0.50000")["orderCreateTransaction"]
        j = self.put("/orders/%s/clientExtensions" % o["id"], {"clientExtensions": {"id": "late", "tag": "t"}})
        self.assertEqual(j["orderClientExtensionsModifyTransaction"]["clientExtensionsModify"], {"id": "late", "tag": "t"})
        self.assertEqual(self.get(self.p("/orders/@late")).json["order"]["id"], o["id"])
        j = self.put("/orders/%s/clientExtensions" % o["id"], {}, expect=400)
        self.assertEqual(j["errorCode"], "CLIENT_EXTENSIONS_DATA_MISSING")


# ---------------------------------------------------------------------------------------------------------------
# Trades, their stops and targets; positions (netting, hedging, FIFO)
# ---------------------------------------------------------------------------------------------------------------

class Trades(Base):
    def test_take_profit_stop_loss_and_trailing_stop_on_fill(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ takeProfitOnFill, stopLossOnFill, trailingStopLossOnFill (https://developer.oanda.com/rest-live-v20/transaction-df/ *Details); their orders
        are created with reason ON_FILL right after the fill; https://developer.oanda.com/rest-live-v20/trade-df/ Trade carries them as takeProfitOrder etc."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        j = self.order(type="MARKET", instrument="EUR_USD", units="100", takeProfitOnFill={"price": "1.11000"},
                       stopLossOnFill={"distance": "0.00500"}, trailingStopLossOnFill={"distance": "0.00300"},
                       tradeClientExtensions={"id": "t1"})
        fill = j["orderFillTransaction"]
        txs = self.txs(since=int(fill["id"]))
        self.assertEqual([(t["type"], t["reason"]) for t in txs], [("TAKE_PROFIT_ORDER", "ON_FILL"),
                                                                    ("STOP_LOSS_ORDER", "ON_FILL"),
                                                                    ("TRAILING_STOP_LOSS_ORDER", "ON_FILL")])
        self.assertTrue(all(t["tradeID"] == fill["id"] and t["orderFillTransactionID"] == fill["id"] for t in txs))
        t = self.get(self.p("/trades/@t1")).json["trade"]
        self.assertEqual(t["clientExtensions"], {"id": "t1"})
        self.assertEqual(t["takeProfitOrder"]["price"], "1.11000")
        self.assertEqual(t["stopLossOrder"]["price"], "1.09500")
        self.assertEqual(t["trailingStopLossOrder"]["trailingStopValue"], "1.09700")
        summary = self.get(self.p()).json["account"]["trades"][0]
        self.assertEqual(sorted(k for k in summary if k.endswith("OrderID")),
                         ["stopLossOrderID", "takeProfitOrderID", "trailingStopLossOrderID"])

    def test_a_stop_loss_closes_the_trade_and_cancels_its_target(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ StopLossOrder; OrderFillReason STOP_LOSS_ORDER; OrderCancelReason LINKED_TRADE_CLOSED "The Order is
        linked to an open Trade that was closed"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        fill = self.order(type="MARKET", instrument="EUR_USD", units="100", takeProfitOnFill={"price": "1.11000"},
                          stopLossOnFill={"price": "1.09000"})["orderFillTransaction"]
        self.hold("EUR_USD", "1.08990", "1.09004")
        txs = self.txs(since=int(fill["id"]) + 2)
        self.assertEqual([(t["type"], t.get("reason")) for t in txs], [("ORDER_FILL", "STOP_LOSS_ORDER"),
                                                                        ("ORDER_CANCEL", "LINKED_TRADE_CLOSED")])
        closed = txs[0]["tradesClosed"][0]
        self.assertEqual((closed["tradeID"], closed["units"], closed["price"]), (fill["id"], "-100", "1.08990"))
        t = self.get(self.p("/trades/" + fill["id"])).json["trade"]
        self.assertEqual((t["state"], t["currentUnits"], t["averageClosePrice"]), ("CLOSED", "0", "1.08990"))
        self.assertIn("closeTime", t)
        self.assertEqual(self.get(self.p("/trades?state=CLOSED")).json["trades"][0]["id"], fill["id"])

    def test_a_trailing_stop_follows_the_price_and_fires(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ TrailingStopLossOrder: "The trailing stop value will trail (follow) the market price"; OEL
        sub-accounts PDF: "The stop price level is continually adjusted based on incoming quotes"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        fill = self.order(type="MARKET", instrument="EUR_USD", units="100",
                          trailingStopLossOnFill={"distance": "0.00100"})["orderFillTransaction"]
        self.hold("EUR_USD", "1.10200", "1.10214")
        tsl = self.get(self.p("/trades/" + fill["id"])).json["trade"]["trailingStopLossOrder"]
        self.assertEqual(tsl["trailingStopValue"], "1.10100")
        self.hold("EUR_USD", "1.10150", "1.10164")
        self.assertEqual(self.get(self.p("/trades/" + fill["id"])).json["trade"]["trailingStopLossOrder"]
                         ["trailingStopValue"], "1.10100", "it never moves back")
        self.hold("EUR_USD", "1.10100", "1.10114")
        t = self.get(self.p("/trades/" + fill["id"])).json["trade"]
        self.assertEqual(t["state"], "CLOSED")
        fills = self.txs(since=int(fill["id"]), type_="ORDER_FILL")
        self.assertEqual(fills[0]["reason"], "TRAILING_STOP_LOSS_ORDER")

    def test_close_all_or_part_of_a_trade(self):
        """https://developer.oanda.com/rest-live-v20/trade-ep/ PUT .../trades/{tradeSpecifier}/close {units: "ALL" or a number}: 200 orderCreateTransaction
        (MARKET_ORDER with tradeClose, reason TRADE_CLOSE), orderFillTransaction (MARKET_ORDER_TRADE_CLOSE, tradeReduced
        or tradesClosed); 400 CLOSE_TRADE_UNITS_EXCEED_TRADE_SIZE; 404 when it does not exist."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        fill = self.order(type="MARKET", instrument="EUR_USD", units="1000")["orderFillTransaction"]
        j = self.put("/trades/%s/close" % fill["id"], {"units": "400"})
        c, f = j["orderCreateTransaction"], j["orderFillTransaction"]
        self.assertEqual((c["type"], c["reason"], c["tradeClose"]), ("MARKET_ORDER", "TRADE_CLOSE",
                                                                    {"tradeID": fill["id"], "units": "400"}))
        self.assertEqual((f["reason"], f["tradeReduced"]["units"], f["units"]), ("MARKET_ORDER_TRADE_CLOSE", "-400", "-400"))
        self.assertRegex(f["tradeReduced"]["realizedPL"], MONEY)
        self.assertEqual(self.trades()[0]["currentUnits"], "600")
        j = self.put("/trades/%s/close" % fill["id"], {"units": "601"}, expect=400)
        self.assertEqual(j["errorCode"], "CLOSE_TRADE_UNITS_EXCEED_TRADE_SIZE")
        j = self.put("/trades/%s/close" % fill["id"], {})
        self.assertEqual(j["orderFillTransaction"]["tradesClosed"][0]["units"], "-600")
        j = self.put("/trades/%s/close" % fill["id"], {}, expect=404)
        self.assertEqual(j["errorCode"], "TRADE_DOESNT_EXIST")
        self.assertEqual(self.get(self.p("/trades/424242")).json, {"errorMessage": "The trade ID specified does not exist"})

    def test_a_closed_market_cancels_a_trade_close(self):
        """https://www.oanda.com/uk-en/trading/hours-of-operation/: "Existing positions cannot be closed ... when markets are closed"; MARKET_HALTED."""
        fill = self.order(type="MARKET", instrument="EUR_USD", units="100")["orderFillTransaction"]
        self.fake.set_market("closed", "EUR_USD")
        j = self.put("/trades/%s/close" % fill["id"], {})
        self.assertEqual(j["orderCancelTransaction"]["reason"], "MARKET_HALTED")
        self.assertEqual(self.trades()[0]["currentUnits"], "100")

    def test_change_replace_and_cancel_a_trades_orders(self):
        """https://developer.oanda.com/rest-live-v20/trade-ep/ PUT .../trades/{tradeSpecifier}/orders: "If takeProfit is set to null, the Take Profit Order will be
        cancelled if it exists"; a replacement cancels (CLIENT_REQUEST_REPLACED) and creates (REPLACEMENT), and "is
        inherited by the replacement Order" for sub-fields left out; *OrderFillTransaction "only if immediately
        filled"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        fill = self.order(type="MARKET", instrument="EUR_USD", units="100", takeProfitOnFill={"price": "1.11000"},
                          stopLossOnFill={"price": "1.09000", "timeInForce": "GFD"})["orderFillTransaction"]
        j = self.put("/trades/%s/orders" % fill["id"], {"stopLoss": {"price": "1.09500"}, "takeProfit": None})
        self.assertEqual(j["takeProfitOrderCancelTransaction"]["reason"], "CLIENT_REQUEST")
        self.assertEqual(j["stopLossOrderCancelTransaction"]["reason"], "CLIENT_REQUEST_REPLACED")
        new = j["stopLossOrderTransaction"]
        self.assertEqual((new["reason"], new["price"], new["timeInForce"]), ("REPLACEMENT", "1.09500", "GFD"))
        self.assertEqual(j["stopLossOrderCancelTransaction"]["replacedByOrderID"], new["id"])
        t = self.get(self.p("/trades/" + fill["id"])).json["trade"]
        self.assertNotIn("takeProfitOrder", t)
        j = self.put("/trades/%s/orders" % fill["id"], {"takeProfit": {"price": "1.12000"}})
        self.assertEqual(j["takeProfitOrderTransaction"]["reason"], "CLIENT_ORDER")
        j = self.put("/trades/%s/orders" % fill["id"], {"stopLoss": {"price": "1.10005"}})
        self.assertEqual(j["stopLossOrderFillTransaction"]["reason"], "STOP_LOSS_ORDER")
        self.assertEqual(self.trades(), [])

    def test_trade_client_extensions(self):
        """https://developer.oanda.com/rest-live-v20/trade-ep/ PUT .../trades/{tradeSpecifier}/clientExtensions: tradeClientExtensionsModifyTransaction;
        CLIENT_TRADE_ID_ALREADY_EXISTS "already assigned to another open Trade"."""
        a = self.order(type="MARKET", instrument="EUR_USD", units="1", tradeClientExtensions={"id": "x"})
        b = self.order(type="MARKET", instrument="GBP_USD", units="1")["orderFillTransaction"]
        j = self.put("/trades/%s/clientExtensions" % b["id"], {"clientExtensions": {"id": "x"}}, expect=400)
        self.assertEqual(j["errorCode"], "CLIENT_TRADE_ID_ALREADY_EXISTS")
        j = self.put("/trades/%s/clientExtensions" % b["id"], {"clientExtensions": {"id": "y", "comment": "c"}})
        self.assertEqual(j["tradeClientExtensionsModifyTransaction"]["tradeClientExtensionsModify"],
                         {"id": "y", "comment": "c"})
        self.assertEqual(self.get(self.p("/trades/@y")).json["trade"]["id"], b["id"])
        r = self.call("POST", self.p("/orders"), {"order": {"type": "MARKET", "instrument": "EUR_USD", "units": "1",
                                                            "tradeClientExtensions": {"id": "x"}}})
        self.assertEqual(r.json["errorCode"], "CLIENT_TRADE_ID_ALREADY_EXISTS")
        self.assertTrue(a)


class Positions(Base):
    def test_netting_reduces_first_then_opens_the_other_way(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ OrderPositionFill DEFAULT: "REDUCE_FIRST behaviour for non-client hedging Accounts"; REDUCE_FIRST
        "always fully reduce an existing Position before opening a new Position"; https://developer.oanda.com/rest-live-v20/transaction-df/ TradeReduce,
        tradesClosed, tradeOpened."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        a = self.order(type="MARKET", instrument="EUR_USD", units="1000")["orderFillTransaction"]
        f = self.order(type="MARKET", instrument="EUR_USD", units="-400")["orderFillTransaction"]
        self.assertEqual((f["tradeReduced"]["tradeID"], f["tradeReduced"]["units"]), (a["id"], "-400"))
        self.assertNotIn("tradeOpened", f)
        f = self.order(type="MARKET", instrument="EUR_USD", units="-1000")["orderFillTransaction"]
        self.assertEqual(f["tradesClosed"][0]["units"], "-600")
        self.assertEqual(f["tradeOpened"]["units"], "-400")
        pos = self.get(self.p("/positions/EUR_USD")).json["position"]
        self.assertEqual((pos["long"]["units"], pos["short"]["units"]), ("0", "-400"))
        self.assertNotIn("averagePrice", pos["long"])
        self.assertEqual(pos["short"]["tradeIDs"], [f["id"]])

    def test_hedging_holds_both_sides_and_margins_the_larger_leg(self):
        """https://developer.oanda.com/rest-live-v20/order-df/ DEFAULT: "OPEN_ONLY behaviour for client hedging Accounts"; https://developer.oanda.com/rest-live-v20/position-df/ Position long and short;
        https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf: margin "for the larger leg trades ... or for the long leg if the long and short trade
        quantities exactly offset"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="1000")
        one = D(self.get(self.p("/summary", self.H)).json["account"]["marginUsed"])
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="-600")
        self.assertEqual(len(self.trades(self.H)), 2)
        pos = self.get(self.p("/positions/EUR_USD", self.H)).json["position"]
        self.assertEqual((pos["long"]["units"], pos["short"]["units"]), ("1000", "-600"))
        self.assertEqual(D(self.get(self.p("/summary", self.H)).json["account"]["marginUsed"]), one)

    def test_close_a_position_side_by_side(self):
        """https://developer.oanda.com/rest-live-v20/position-ep/ PUT .../positions/{instrument}/close {longUnits, shortUnits: "ALL", "NONE" or a number}: 200
        longOrderCreateTransaction (longPositionCloseout), longOrderFillTransaction (MARKET_ORDER_POSITION_CLOSEOUT);
        400 CLOSEOUT_POSITION_DOESNT_EXIST, CLOSEOUT_POSITION_UNITS_EXCEED_POSITION_SIZE."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="1000")
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="500")
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="-300")
        j = self.put("/positions/EUR_USD/close", {"longUnits": "1200", "shortUnits": "NONE"}, acct=self.H)
        c, f = j["longOrderCreateTransaction"], j["longOrderFillTransaction"]
        self.assertEqual((c["reason"], c["longPositionCloseout"]), ("POSITION_CLOSEOUT",
                                                                    {"instrument": "EUR_USD", "units": "1200"}))
        self.assertEqual(f["reason"], "MARKET_ORDER_POSITION_CLOSEOUT")
        self.assertEqual(([x["units"] for x in f["tradesClosed"]], f["tradeReduced"]["units"]), (["-1000"], "-200"))
        j = self.put("/positions/EUR_USD/close", {"longUnits": "301"}, acct=self.H, expect=400)
        self.assertEqual(j["errorCode"], "CLOSEOUT_POSITION_UNITS_EXCEED_POSITION_SIZE")
        j = self.put("/positions/EUR_USD/close", {"longUnits": "ALL", "shortUnits": "ALL"}, acct=self.H)
        self.assertEqual(set(j) & {"longOrderFillTransaction", "shortOrderFillTransaction"},
                         {"longOrderFillTransaction", "shortOrderFillTransaction"})
        j = self.put("/positions/EUR_USD/close", {"longUnits": "ALL"}, acct=self.H, expect=400)
        self.assertEqual(j["errorCode"], "CLOSEOUT_POSITION_DOESNT_EXIST")
        self.assertEqual(self.get(self.p("/openPositions", self.H)).json["positions"], [])
        self.assertEqual([p["instrument"] for p in self.get(self.p("/positions", self.H)).json["positions"]], ["EUR_USD"])

    def test_a_position_close_puts_its_extensions_on_the_order_it_makes(self):
        """https://developer.oanda.com/rest-live-v20/position-ep/ longClientExtensions and shortClientExtensions, "The client extensions to add
        to the MarketOrder used to close the long [short] position"; https://developer.oanda.com/rest-live-v20/transaction-df/ MarketOrderTransaction
        clientExtensions, "Client Extensions to add to the Order (only provided if the Order is being created with client extensions)", and
        OrderFillTransaction clientOrderID, "The client Order ID of the Order filled (only provided if the client has assigned one)". A
        MARKET_ORDER transaction made before the extensions are put on the order leaves OANDA's record of a close with none of them,
        and a close whose answer was lost is then never found by its id."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        ext = {"id": "c-1", "tag": "a-tag", "comment": "the close"}

        def closed(j, side, acct, cid):
            c, f = j[side + "OrderCreateTransaction"], j[side + "OrderFillTransaction"]
            self.assertEqual(c["reason"], "POSITION_CLOSEOUT")
            self.assertEqual(f["orderID"], c["id"])
            if cid is None:
                self.assertNotIn("clientExtensions", c)
                self.assertNotIn("clientOrderID", f)
                return c
            want = dict(ext, id=cid)
            self.assertEqual(c["clientExtensions"], want)
            self.assertEqual(f["clientOrderID"], cid)
            # in the record too, and on the order itself
            rec = {t["id"]: t for t in self.txs(acct)}
            self.assertEqual(rec[c["id"]]["clientExtensions"], want)
            self.assertEqual(rec[f["id"]]["clientOrderID"], cid)
            order = self.get(self.p("/orders/" + c["id"], acct)).json["order"]
            self.assertEqual(order["clientExtensions"], want)
            return c

        # netting: a long, part then the rest; a short, whole
        self.order(type="MARKET", instrument="EUR_USD", units="1000")
        j = self.put("/positions/EUR_USD/close", {"longUnits": "400", "shortUnits": "NONE",
                                                  "longClientExtensions": dict(ext, id="n-long-1")})
        self.assertEqual(closed(j, "long", self.A, "n-long-1")["longPositionCloseout"],
                         {"instrument": "EUR_USD", "units": "400"})
        j = self.put("/positions/EUR_USD/close", {"longUnits": "ALL", "shortUnits": "NONE"})
        closed(j, "long", self.A, None)                      # none given: none on it
        self.order(type="MARKET", instrument="EUR_USD", units="-1000")
        j = self.put("/positions/EUR_USD/close", {"longUnits": "NONE", "shortUnits": "ALL",
                                                  "shortClientExtensions": dict(ext, id="n-short")})
        closed(j, "short", self.A, "n-short")
        # hedging: both sides at once, each with its own
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="1000")
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="-600")
        j = self.put("/positions/EUR_USD/close", {"longUnits": "ALL", "shortUnits": "300",
                                                  "longClientExtensions": dict(ext, id="h-long"),
                                                  "shortClientExtensions": dict(ext, id="h-short")}, acct=self.H)
        closed(j, "long", self.H, "h-long")
        closed(j, "short", self.H, "h-short")
        # extensions OANDA would not take are refused, and no order is made
        j = self.put("/positions/EUR_USD/close", {"shortUnits": "ALL", "shortClientExtensions": {"id": ""}},
                     acct=self.H, expect=400)
        self.assertEqual(j["shortOrderRejectTransaction"]["rejectReason"], "CLIENT_ORDER_ID_INVALID")
        self.assertEqual(self.get(self.p("/positions/EUR_USD", self.H)).json["position"]["short"]["units"], "-300")

    def test_money_is_in_the_account_currency(self):
        """https://developer.oanda.com/rest-live-v20/account-df/ AccountUnits "a quantity of an Account's home currency"; https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf: margin converted at
        "The current mid-price for the Home/Instrument currency pair, e.g. GBPUSD for a EURUSD trade in a GBP-denominated
        account"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        self.hold("GBP_USD", "1.25000", "1.25010")
        f = self.order(type="MARKET", instrument="EUR_USD", units="10000")["orderFillTransaction"]
        im = D(f["tradeOpened"]["initialMarginRequired"])
        want = D(10000) * D("1.10007") / D("1.25005") * D("0.0333")
        self.assertAlmostEqual(float(im), float(want), places=2)
        self.assertEqual(f["homeConversionFactors"]["gainQuoteHome"]["factor"][:5], "0.791")


class FifoAccount(Base):
    KW = {"division": "us"}

    def test_trades_close_oldest_first(self):
        """https://help.oanda.com/us/en/faqs/account-types-and-leverage-us.htm: FIFO applies to US accounts, no hedging; https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason
        FIFO_VIOLATION. Cancelling a close of the newer trade is the stand-in's own form of it."""
        acct = self.get(self.p("/summary")).json["account"]
        self.assertEqual((acct["currency"], acct["hedgingEnabled"]), ("USD", False))
        a = self.order(type="MARKET", instrument="EUR_USD", units="100")["orderFillTransaction"]
        b = self.order(type="MARKET", instrument="EUR_USD", units="100")["orderFillTransaction"]
        j = self.put("/trades/%s/close" % b["id"], {})
        self.assertEqual(j["orderCancelTransaction"]["reason"], "FIFO_VIOLATION")
        j = self.put("/trades/%s/close" % a["id"], {})
        self.assertIn("orderFillTransaction", j)


# ---------------------------------------------------------------------------------------------------------------
# Transactions, changes, the stream of them; money that moves without a trade
# ---------------------------------------------------------------------------------------------------------------

class Transactions(Base):
    def test_pages_idrange_sinceid_one_and_the_type_filter(self):
        """https://developer.oanda.com/rest-live-v20/transaction-ep/: GET .../transactions {from, to, pageSize, type, count, pages (idrange URLs),
        lastTransactionID}, pageSize default 100 maximum 1000; /idrange (from, to); /sinceid (id); /{id}; 404 "The
        transaction ID specified does not exist"."""
        for _ in range(3):
            self.order(type="MARKET", instrument="EUR_USD", units="1")
        j = self.get(self.p("/transactions?pageSize=3")).json
        self.assertEqual(list(j), ["from", "to", "pageSize", "count", "pages", "lastTransactionID"])
        self.assertEqual((j["count"], j["pageSize"], j["lastTransactionID"]), (8, 3, "8"))
        self.assertEqual(len(j["pages"]), 3)
        self.assertTrue(j["pages"][0].endswith("/transactions/idrange?from=1&to=3"))
        page = self.get(j["pages"][1][j["pages"][1].index("/v3/"):]).json["transactions"]
        self.assertEqual([t["id"] for t in page], ["4", "5", "6"])
        fills = self.get(self.p("/transactions?type=ORDER_FILL")).json
        self.assertEqual((fills["count"], fills["type"]), (3, ["ORDER_FILL"]))
        self.assertEqual([t["type"] for t in self.txs(since=6)], ["MARKET_ORDER", "ORDER_FILL"])
        self.assertEqual(self.get(self.p("/transactions/7")).json["transaction"]["type"], "MARKET_ORDER")
        self.assertEqual(self.get(self.p("/transactions/99")).json, {"errorMessage": "The transaction ID specified does not exist"})
        self.assertEqual(self.get(self.p("/transactions?pageSize=1001")).status, 400)
        self.assertEqual(self.get(self.p("/transactions?type=NOPE")).status, 400)
        self.assertEqual(self.get(self.p("/transactions/sinceid?id=99")).status, 416)

    def test_the_transaction_stream_carries_every_transaction_in_order_and_heartbeats_with_the_last_id(self):
        """https://developer.oanda.com/rest-live-v20/transaction-ep/ GET .../transactions/stream: Transaction and TransactionHeartbeat objects, one per line;
        https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionHeartbeat {type HEARTBEAT, lastTransactionID, time}."""
        s = self.stream("/transactions/stream")
        self.assertTrue(s.wait(lambda o: o.get("type") == "HEARTBEAT", 3))
        self.assertEqual(s.objs("HEARTBEAT")[0]["lastTransactionID"], "2")
        self.hold("EUR_USD", "1.10000", "1.10014")
        self.order(type="MARKET", instrument="EUR_USD", units="1", stopLossOnFill={"price": "1.00000"})
        self.assertTrue(s.wait(lambda o: o.get("type") == "STOP_LOSS_ORDER", 3))
        got = [o for o in s.objs() if o["type"] != "HEARTBEAT"]
        self.assertEqual([(o["id"], o["type"]) for o in got], [("3", "MARKET_ORDER"), ("4", "ORDER_FILL"),
                                                               ("5", "STOP_LOSS_ORDER")])
        self.assertTrue(s.wait(lambda o: o.get("type") == "HEARTBEAT" and o["lastTransactionID"] == "5", 3))
        u = self.stream("/transactions/stream", headers={"Accept-Datetime-Format": "UNIX"})
        self.order(type="MARKET", instrument="EUR_USD", units="1")
        self.assertRegex(u.wait(lambda o: o.get("type") == "ORDER_FILL", 3)["time"], UNIX)

    def test_changes_since_a_transaction(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ GET .../changes?sinceTransactionID: changes (AccountChanges: ordersCreated, ordersCancelled,
        ordersFilled, ordersTriggered, tradesOpened, tradesReduced, tradesClosed, positions, transactions), state
        (AccountChangesState), lastTransactionID; changes "only provided when sinceTransactionID is supplied"."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        self.order(type="MARKET", instrument="EUR_USD", units="100", trailingStopLossOnFill={"distance": "0.00100"})
        j = self.get(self.p("/changes?sinceTransactionID=2")).json
        self.assertEqual(list(j), ["changes", "state", "lastTransactionID"])
        ch = j["changes"]
        self.assertEqual(list(ch), ["ordersCreated", "ordersCancelled", "ordersFilled", "ordersTriggered", "tradesOpened",
                                    "tradesReduced", "tradesClosed", "positions", "transactions"])
        self.assertEqual([o["type"] for o in ch["ordersCreated"]], ["MARKET", "TRAILING_STOP_LOSS"])
        self.assertEqual(len(ch["tradesOpened"]), 1)
        self.assertEqual(j["state"]["orders"][0]["trailingStopValue"], "1.09900")
        self.assertEqual(sorted(j["state"]["orders"][0]), ["id", "isTriggerDistanceExact", "trailingStopValue",
                                                           "triggerDistance"])
        self.assertNotIn("changes", self.get(self.p("/changes")).json)

    def test_funding_dividends_and_daily_financing(self):
        """https://developer.oanda.com/rest-live-v20/transaction-df/ TRANSFER_FUNDS, DIVIDEND_ADJUSTMENT (openTradeDividendAdjustments), DAILY_FINANCING
        (positionFinancings, openTradeFinancings); https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf: financing is booked daily at "5pm ET"."""
        self.fake.transfer(self.H, "2500")
        self.assertEqual(self.txs(self.H, since=2)[0]["type"], "TRANSFER_FUNDS")
        self.hold("SPX500_USD", "5900.0", "5900.5")
        f = self.order(acct=self.H, type="MARKET", instrument="SPX500_USD", units="2")["orderFillTransaction"]
        self.fake.dividend("SPX500_USD", "1.5")
        # DIVIDEND_ADJUSTMENT is a TransactionType but no TransactionFilter value: refused as a filter; it comes under
        # the group FUNDING (that grouping is stand-in's own; the page lists no group's members).
        self.assertEqual(self.get(self.p("/transactions/sinceid?id=0&type=DIVIDEND_ADJUSTMENT", self.H)).status, 400)
        div = [t for t in self.txs(self.H, type_="FUNDING") if t["type"] == "DIVIDEND_ADJUSTMENT"][0]
        self.assertEqual((div["dividendAdjustment"], div["openTradeDividendAdjustments"][0]["tradeID"]), ("3.0000", f["id"]))
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="10000")
        self.fake.set_clock(ny_instant(self.fake, 17, 0, 30))
        fin = self.txs(self.H, type_="DAILY_FINANCING")
        self.assertEqual(len(fin), 1)
        pf = {p["instrument"]: p for p in fin[0]["positionFinancings"]}
        self.assertEqual(set(pf), {"EUR_USD", "SPX500_USD"})
        self.assertLess(D(pf["EUR_USD"]["financing"]), 0, "a long EUR_USD pays the documented longRate here")
        self.assertEqual(pf["EUR_USD"]["openTradeFinancings"][0]["financingRate"], "-0.0371")
        bal = D(self.get(self.p("/summary", self.H)).json["account"]["balance"])
        self.assertEqual(D(fin[0]["accountBalance"]), bal)

    def test_a_margin_call_then_a_close_out(self):
        """https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf: "Margin_% = (50% * Margin_Used) / NAV_mid"; a margin call at "Margin Close-Out
        Percentage >= 50%", a close-out at ">= 100%" with full liquidation; https://developer.oanda.com/rest-live-v20/transaction-df/ MARGIN_CALL_ENTER,
        MARGIN_CALL_EXIT, MarketOrderMarginCloseout, OrderFillReason MARKET_ORDER_MARGIN_CLOSEOUT."""
        self.hold("EUR_USD", "1.10000", "1.10010")
        self.order(acct=self.H, type="MARKET", instrument="EUR_USD", units="2000000")
        self.fake.set_price("EUR_USD", mid=1.075, spread=0.0001)
        self.assertEqual([t["type"] for t in self.txs(self.H, since=2)][-1], "MARGIN_CALL_ENTER")
        pct = D(self.get(self.p("/summary", self.H)).json["account"]["marginCloseoutPercent"])
        self.assertTrue(D("0.5") <= pct < 1, pct)
        self.fake.set_price("EUR_USD", mid=1.06, spread=0.0001)
        txs = self.txs(self.H, since=2)
        self.assertEqual([(t["type"], t.get("reason")) for t in txs[-3:]],
                         [("MARKET_ORDER", "MARGIN_CLOSEOUT"), ("ORDER_FILL", "MARKET_ORDER_MARGIN_CLOSEOUT"),
                          ("MARGIN_CALL_EXIT", None)])
        self.assertEqual(txs[-3]["marginCloseout"], {"reason": "MARGIN_CHECK_VIOLATION"})
        self.assertEqual(self.trades(self.H), [])


class Wire(Base):
    def test_gzip_when_asked_and_a_wrong_method(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ examples: "Content-Encoding: gzip"; https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ 405 "the endpoint was accessed with the
        wrong HTTP method"."""
        import gzip
        r = self.get("/v3/accounts", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(r.raw))["accounts"][0]["id"], self.A)
        self.assertEqual(self.call("DELETE", self.p("/orders")).status, 405)
        self.assertEqual(self.call("POST", self.p("/summary"), {}).status, 405)

    def test_a_trades_stop_loss_through_the_orders_endpoint(self):
        """https://developer.oanda.com/rest-live-v20/order-ep/ POST .../orders with a StopLossOrderRequest {tradeID, price}; https://developer.oanda.com/rest-live-v20/transaction-df/
        STOP_LOSS_ORDER_ALREADY_EXISTS, TRADE_DOESNT_EXIST, GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED."""
        self.hold("EUR_USD", "1.10000", "1.10014")
        fill = self.order(type="MARKET", instrument="EUR_USD", units="100")["orderFillTransaction"]
        j = self.order(type="STOP_LOSS", tradeID=fill["id"], price="1.09000")
        self.assertEqual((j["orderCreateTransaction"]["type"], j["orderCreateTransaction"]["reason"]),
                         ("STOP_LOSS_ORDER", "CLIENT_ORDER"))
        self.assertEqual(self.get(self.p("/trades/" + fill["id"])).json["trade"]["stopLossOrder"]["price"], "1.09000")
        r = self.call("POST", self.p("/orders"), {"order": {"type": "STOP_LOSS", "tradeID": fill["id"], "price": "1.08"}})
        self.assertEqual((r.status, r.json["errorCode"]), (400, "STOP_LOSS_ORDER_ALREADY_EXISTS"))
        r = self.call("POST", self.p("/orders"), {"order": {"type": "TAKE_PROFIT", "tradeID": "424242", "price": "1.2"}})
        self.assertEqual((r.status, r.json["errorCode"]), (404, "TRADE_DOESNT_EXIST"))
        r = self.call("POST", self.p("/orders"), {"order": {"type": "GUARANTEED_STOP_LOSS", "tradeID": fill["id"],
                                                            "price": "1.08"}})
        self.assertEqual(r.json["errorCode"], "GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED")

    def test_changes_past_the_last_transaction(self):
        """https://developer.oanda.com/rest-live-v20/account-ep/ GET .../changes lists 416 among its errors; giving it for an id past the last transaction is the
        stand-in's own."""
        self.assertEqual(self.get(self.p("/changes?sinceTransactionID=99")).status, 416)
        self.assertEqual(self.get(self.p("/changes?sinceTransactionID=x")).status, 400)


# ---------------------------------------------------------------------------------------------------------------
# The documented limits
# ---------------------------------------------------------------------------------------------------------------

class Limits(unittest.TestCase):
    def test_more_than_120_requests_a_second_get_429(self):
        """https://developer.oanda.com/rest-live-v20/development-guide/: "120 requests per second. Excess requests will receive HTTP 429 error." (per address)."""
        with FakeOanda(new_connections_per_second=None) as fake:
            c = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
            h = {"Authorization": "Bearer " + fake.token}
            statuses = []
            t0 = time.monotonic()
            for _ in range(130):
                c.request("GET", "/v3/accounts", headers=h)
                r = c.getresponse()
                r.read()
                statuses.append(r.status)
            took = time.monotonic() - t0
            self.assertLess(took, 1.0, "the test needs the 130 requests inside one second")
            self.assertEqual(statuses.count(200), 120)
            self.assertEqual(set(statuses[120:]), {429})
            time.sleep(1.05)
            c.request("GET", "/v3/accounts", headers=h)
            self.assertEqual(c.getresponse().status, 200)
            c.close()

    def test_more_than_two_new_connections_a_second_are_ended_unanswered(self):
        """https://developer.oanda.com/rest-live-v20/development-guide/: "Client is allowed to make no more than 2 new connections per second. Excess connections
        will be rejected." The form (the connection ended unanswered) is the stand-in's own."""
        with FakeOanda() as fake:
            h = {"Authorization": "Bearer " + fake.token}
            results = []
            for _ in range(3):
                c = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
                try:
                    c.request("GET", "/v3/accounts", headers=h)
                    results.append(c.getresponse().status)
                except (http.client.HTTPException, OSError) as e:
                    results.append(type(e).__name__)
                finally:
                    c.close()
            self.assertEqual(results[:2], [200, 200])
            self.assertNotIsInstance(results[2], int, results)
            time.sleep(1.05)
            c = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
            c.request("GET", "/v3/accounts", headers=h)
            self.assertEqual(c.getresponse().status, 200)
            c.close()

    def test_more_than_20_streams_are_refused(self):
        """https://developer.oanda.com/rest-live-v20/development-guide/: "20 active streams. Requests above this threshold will be rejected." The 429 is the
        stand-in's own."""
        with FakeOanda(new_connections_per_second=None, stream_limit=20) as fake:
            path = "/v3/accounts/%s/pricing/stream?instruments=EUR_USD" % fake.account_ids[0]
            open_ = [Stream(fake, path) for _ in range(20)]
            try:
                self.assertEqual({s.status for s in open_}, {200})
                extra = Stream(fake, path)
                open_.append(extra)
                self.assertEqual(extra.status, 429)
                open_[0].close()
                self.assertTrue(until(lambda: fake.state()["streams"] == 19, 3))
                again = Stream(fake, path)
                open_.append(again)
                self.assertEqual(again.status, 200)
            finally:
                for s in open_:
                    s.close()

    def test_a_forced_fault(self):
        """The control fail_next: the next REST request answers the status asked for."""
        with FakeOanda(new_connections_per_second=None) as fake:
            fake.fail_next(503, 1, "/v3/accounts")
            c = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
            h = {"Authorization": "Bearer " + fake.token}
            c.request("GET", "/v3/accounts", headers=h)
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read())), (503, {"errorMessage": "Service Unavailable"}))
            c.request("GET", "/v3/accounts", headers=h)
            self.assertEqual(c.getresponse().status, 200)
            c.close()


# ---------------------------------------------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------------------------------------------

class Controls(Base):
    def test_every_control_is_also_an_http_call(self):
        """Control methods for tests; the reference stand-in's pattern of POST /_control/<name>."""
        def ctl(name, body=None, method="POST"):
            c = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=10)
            c.request(method, "/_control/" + name, body=json.dumps(body or {}))
            r = c.getresponse()
            out = (r.status, json.loads(r.read()))
            c.close()
            return out
        self.assertEqual(ctl("price", {"instrument": "EUR_USD", "bid": "1.20000", "ask": "1.20010"})[1]["bid"], "1.20000")
        self.assertEqual(self.price("EUR_USD"), (D("1.20000"), D("1.20010")))
        self.assertEqual(ctl("market", {"state": "closed", "instrument": "EUR_USD"})[0], 200)
        self.assertEqual(ctl("market", {"state": "open"})[0], 200)
        self.assertEqual(ctl("liquidity", {"instrument": "EUR_USD", "units": 10, "count": 1})[1]["units"], "10")
        self.assertEqual(ctl("refuse", {"reason": "ACCOUNT_LOCKED"})[0], 200)
        self.assertEqual(ctl("refuse", {"reason": "nope"})[0], 400)
        self.assertEqual(ctl("connects", {"count": 0})[0], 200)
        self.assertEqual(ctl("clock", {"advance_s": 1})[0], 200)
        self.assertEqual(ctl("transfer", {"account": self.A, "amount": "1"})[1]["type"], "TRANSFER_FUNDS")
        self.assertEqual(ctl("options", {"client_id_lookup": "any"})[1]["client_id_lookup"], "any")
        self.assertIn("modes", ctl("state", method="GET")[1])
        self.assertEqual(ctl("transactions?account=" + self.A, method="GET")[1]["transactions"][0]["type"], "CREATE")
        tok = ctl("token", {"action": "rotate"})[1]["token"]
        self.assertEqual(self.get("/v3/accounts", token=tok).status, 200)
        self.assertEqual(ctl("reset")[0], 200)
        self.assertEqual(self.get("/v3/accounts").status, 200)
        self.assertEqual(ctl("nonsense")[0], 400)
        self.assertNotIn("/_control", json.dumps(self.fake.requests()))

    def test_a_redirect_when_asked_carries_nothing_out(self):
        """measured on a live account (OANDA's live API, 2026-11-15: two account summary reads
        answered HTTP 307, and a read a minute later did not). The control redirect_next answers the next matching
        requests with that status and a Location (by default the same request on the stand-in itself; where OANDA's
        pointed was not recorded: stand-in's own); the request is not carried out."""
        before = self.get(self.p("/summary")).json["account"]["lastTransactionID"]
        out = self.fake.redirect_next(count=1, path=self.p("/summary"), method="GET")
        self.assertEqual(out["redirects"][0]["status"], 307)
        r = self.get(self.p("/summary?x=1"))
        self.assertEqual(r.status, 307)
        self.assertEqual(r.headers["Location"], self.fake.rest_url + self.p("/summary?x=1"))
        self.assertEqual(self.get(self.p("/summary")).status, 200)            # count 1: once
        # a write answered so is not carried out
        self.fake.redirect_next(count=1, path=self.p("/orders"), method="POST", status=308,
                                location="https://api-fxtrade.oanda.com" + self.p("/orders"))
        r = self.call("POST", self.p("/orders"), {"order": {"type": "MARKET", "instrument": "EUR_USD", "units": "1"}})
        self.assertEqual((r.status, r.headers["Location"]), (308, "https://api-fxtrade.oanda.com" + self.p("/orders")))
        self.assertEqual(self.trades(), [])
        self.assertEqual(self.get(self.p("/summary")).json["account"]["lastTransactionID"], before)
        log = [(q["method"], q["status"]) for q in self.fake.requests() if q["path"].endswith(("/summary", "/orders"))]
        self.assertIn(("GET", 307), log)
        self.assertIn(("POST", 308), log)
        # another method or path is not answered so; nor anything once reset
        self.fake.redirect_next(count=5, path=self.p("/orders"), method="POST")
        self.assertEqual(self.get(self.p("/summary")).status, 200)
        self.fake.reset()
        self.assertEqual(self.fake.state()["redirects"], [])
        self.order(type="MARKET", instrument="EUR_USD", units="1")
        for bad in ({"status": 200}, {"status": 304}, {"count": 0, "status": 307}):
            with self.assertRaises(ValueError):
                self.fake.redirect_next(**bad)
        c = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=10)
        c.request("POST", "/_control/redirect", body=json.dumps({"count": 1, "status": 307}))
        self.assertEqual(c.getresponse().status, 200)
        c.close()
        self.assertEqual(self.get("/v3/accounts").status, 307)

    def test_a_stream_is_redirected_when_asked(self):
        """redirect_next(kind="stream") answers the next matching request on the streaming host with that status and a
        Location on the streaming host (by default the same request there); nothing is streamed for it. stand-in's own:
        OANDA's live 307s were seen on REST reads only."""
        path = self.p("/pricing/stream?instruments=EUR_USD")
        out = self.fake.redirect_next(count=1, kind="stream", path=self.p("/pricing/stream"))
        self.assertEqual([(r["kind"], r["status"]) for r in out["redirects"]], [("stream", 307)])
        self.assertEqual(self.get(self.p("/summary")).status, 200)               # REST is not redirected by it
        s = self.stream("/pricing/stream?instruments=EUR_USD")
        self.assertEqual(s.status, 307)
        self.assertEqual(s.resp.getheader("Location"), self.fake.stream_url + path)
        s2 = self.stream("/pricing/stream?instruments=EUR_USD")
        self.assertEqual(s2.status, 200)                                         # count 1: once
        self.assertTrue(s2.wait(lambda o: o.get("type") == "PRICE"))
        log = [(q["host"], q["status"]) for q in self.fake.requests() if q["path"] == self.p("/pricing/stream")]
        self.assertEqual(log[:1], [("stream", 307)])
        # a REST redirect does not touch the streams
        self.fake.redirect_next(count=1, path=self.p("/pricing/stream"))
        s3 = self.stream("/pricing/stream?instruments=EUR_USD")
        self.assertEqual(s3.status, 200)
        self.fake.reset()
        for bad in ({"kind": "elsewhere"}, {"kind": None}):
            with self.assertRaises(ValueError):
                self.fake.redirect_next(**bad)
        c = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=10)
        c.request("POST", "/_control/redirect", body=json.dumps({"count": 1, "kind": "stream"}))
        self.assertEqual(c.getresponse().status, 200)
        c.close()
        self.assertEqual(self.fake.state()["redirects"][0]["kind"], "stream")

    def test_a_ramp_moves_the_price_in_a_straight_line(self):
        """The control set_price(ramp_to=, seconds=): the stand-in's own way to walk a price for a trailing stop."""
        self.hold("EUR_USD", "1.10000", "1.10010")
        self.fake.set_price("EUR_USD", ramp_to=1.11, seconds=1)
        a = self.price("EUR_USD")[0]
        time.sleep(1.2)
        self.fake.settle()
        b = self.price("EUR_USD")[0]
        self.assertGreater(b, a)
        self.assertEqual(b, D("1.10995"))

    def test_reset_puts_everything_back(self):
        """The control reset: accounts, prices, markets, clock, tokens and controls as at the start."""
        self.order(type="MARKET", instrument="EUR_USD", units="1")
        self.fake.set_market("closed")
        self.fake.advance(3600)
        self.fake.revoke_token()
        self.fake.reset()
        self.assertEqual(self.get(self.p("/summary")).json["account"]["lastTransactionID"], "2")
        self.assertEqual(self.fake.state()["clock_offset_s"], 0)
        self.assertTrue(self.get(self.p("/pricing?instruments=EUR_USD")).json["prices"][0]["tradeable"])


if __name__ == "__main__":
    unittest.main()
