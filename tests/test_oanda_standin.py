"""OANDA adapter against its stand-in (tests/fake_oanda.py), as a platform
drives it: spawned through the SDK's Connector with the stand-in's fake token,
pointed at the stand-in on loopback (OANDA_REST_URL, OANDA_STREAM_URL). The
stand-in enforces OANDA's documented per-address limits (120 requests a
second, 20 streams, 2 new connections a second) throughout.

Run: python3 -m unittest tests.test_oanda_standin -v
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brueconnect import (Connector, ConnectorError, NotArmed,  # noqa: E402
                         protocol, registry)
from tests.fake_oanda import FakeOanda  # noqa: E402

ADAPTER = os.path.join(ROOT, "brueconnect", "adapters", "oanda",
                       "oanda_adapter.py")
# Longer than the adapter's LEVELS_EVERY (5 s): a level it would put back
# has been put back by then.
LEVELS_WAIT = 6.5


def wait(cond, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(step)
    return cond()


class StandIn(unittest.TestCase):
    division = "uk"
    fake: FakeOanda = None

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOanda(division=cls.division).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.dir = tempfile.mkdtemp(prefix="oanda-test-")
        self.conns, self.files = [], []

    def tearDown(self):
        if getattr(self, "_direct", None) is not None:
            self._direct.close()
            self._direct = None
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass
            # Connector.close ends the process, not its pipes' file objects
            for pipe in (getattr(c.proc, "stdin", None),
                         getattr(c.proc, "stdout", None)):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
        for f in self.files:
            f.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def env(self, account=None, **extra):
        env = dict(os.environ)
        for k in ("BRUE_ACCOUNT_ID", "OANDA_ENV", "BRUE_CRED_TOKEN"):
            env.pop(k, None)
        env.update({"OANDA_REST_URL": self.fake.rest_url,
                    "OANDA_STREAM_URL": self.fake.stream_url,
                    "BRUE_CRED_TOKEN": self.fake.token or "",
                    "BRUE_BROKER_STATE": self.dir,
                    "PYTHONPATH": ROOT})
        if account is not None:
            env["BRUE_ACCOUNT_ID"] = self.fake.account_ids[account]
        env.update(extra)
        return env

    def remember(self):
        """What `accounts.list` leaves in the profile's state for the next
        cold hello (the adapter's own file, oanda_state.json), written
        directly: a second adapter process inside the same second would use
        up OANDA's two new connections a second on its own. The real flow is
        Accounts.test_accounts_are_listed_before_any_session and
        test_a_cold_guess_that_was_wrong_is_refused_then_right."""
        path = os.path.join(self.dir, "oanda_state.json")
        st = {}
        if os.path.exists(path):
            with open(path) as fh:
                st = json.load(fh)
        st.setdefault("version", 1)
        st["accounts"] = {a.id: {"currency": a.currency,
                                 "hedging": a.hedging, "alias": a.alias,
                                 "mt4": a.mt4 is not None}
                          for a in self.fake.broker.accounts.values()}
        with open(path, "w") as fh:
            json.dump(st, fh)

    def spawn(self, account=0, prime=True, connect=True, args=("--paper",),
              **extra):
        """An adapter bound to one of the stand-in's sub-accounts, after the
        platform has listed the accounts (as the Terminal does)."""
        cmd = [sys.executable, ADAPTER] + list(args)
        if prime:
            self.remember()
        err = open(os.path.join(self.dir, "stderr-%d.txt" % len(self.files)),
                   "w")
        self.files.append(err)
        c = Connector(cmd, env=self.env(account, **extra), stderr=err)
        self.conns.append(c)
        if connect:
            c.connect()
        else:
            c.hello()
        return c

    def stderr(self) -> str:
        out = ""
        for f in self.files:
            f.flush()
            with open(f.name) as fh:
                out += fh.read()
        return out

    def txs(self, account=0):
        return self.fake.transactions(self.fake.account_ids[account])

    def direct(self, method, path, body=None):
        """A request straight to the stand-in, as another tool of the user
        would make it: on ONE kept-alive connection, because the stand-in
        holds this test to OANDA's two new connections a second too."""
        for attempt in range(3):
            conn = getattr(self, "_direct", None)
            if conn is None:
                conn = self._direct = http.client.HTTPConnection(
                    "127.0.0.1", self.fake.port, timeout=10)
            try:
                conn.request(method, path, body=None if body is None
                             else json.dumps(body),
                             headers={"Authorization": "Bearer "
                                      + self.fake.token,
                                      "Content-Type": "application/json"})
                r = conn.getresponse()
                data = r.read()
                if r.status >= 400:
                    raise AssertionError("%s %s: %d %s" % (
                        method, path, r.status, data[:200]))
                return json.loads(data)
            except (OSError, http.client.HTTPException):
                conn.close()
                self._direct = None
                time.sleep(1.1)
        raise AssertionError("the stand-in did not answer %s %s" % (method,
                                                                    path))

    def place(self, c, cid, sym="EUR_USD", side="buy", qty=1000, **kw):
        p = {"client_id": cid, "symbol": sym, "side": side, "qty": qty}
        p.update(kw)
        return c.request("order.place", p)


class Accounts(StandIn):
    def test_accounts_are_listed_before_any_session(self):
        c = self.spawn(account=None, prime=False, connect=False)
        rows = c.request("accounts.list")["accounts"]
        self.assertEqual([r["id"] for r in rows], self.fake.account_ids)
        self.assertEqual([r.get("label") for r in rows],
                         ["Primary", "Hedging", "MT4"])
        self.assertEqual([r.get("currency") for r in rows],
                         ["GBP", "USD", "GBP"])
        self.assertTrue(all(r["mode"] == "paper" for r in rows))
        self.assertFalse(any(r["selected"] for r in rows))
        self.assertEqual(rows[2].get("type"), "mt4")
        # several accounts and no binding: never one picked for the user
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        self.assertEqual(e.exception.code, "x_oanda_choose_account")

    def test_an_account_the_token_does_not_reach_is_refused(self):
        c = self.spawn(account=None, prime=False, connect=False,
                       BRUE_ACCOUNT_ID="101-004-7240139-099")
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        self.assertEqual((e.exception.code, e.exception.like),
                         ("x_oanda_account_not_reachable", "unauthorized"))
        # a sub-account opened after the token was made: needs a new token
        new = self.fake.add_account("EUR", alias="Later")["id"]
        c2 = self.spawn(account=None, prime=False, connect=False,
                        BRUE_ACCOUNT_ID=new)
        with self.assertRaises(ConnectorError) as e:
            c2.request("account.get")
        self.assertIn("new token", str(e.exception))

    def test_a_cold_guess_that_was_wrong_is_refused_then_right(self):
        # The hedging USD account, never listed before: hello guessed.
        c = self.spawn(account=1, prime=False, connect=False)
        self.assertEqual(c.handshake["account_model"], "netting")
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        # The protocol's own code (SPEC 6, revision 1.11): a platform starts
        # the adapter again, as the kit's Connector does inside connect()
        # (tests/test_oanda_reconnect.py). A plain request is not a connect.
        self.assertEqual(e.exception.code, "reconnect")
        c.close()
        c = self.spawn(account=1, prime=False)
        self.assertEqual((c.handshake["account_model"],
                          c.handshake["account_currency"]),
                         ("hedging", "USD"))
        self.assertEqual(c.account["currency"], "USD")

    def test_a_refused_token_is_unauthorized_and_names_the_host(self):
        c = self.spawn(account=0, prime=False, connect=False,
                       BRUE_CRED_TOKEN="0" * 32 + "-" + "1" * 32)
        with self.assertRaises(ConnectorError) as e:
            c.request("catalog.list")
        self.assertEqual(e.exception.code, "unauthorized")
        self.assertIn("127.0.0.1", str(e.exception))
        self.assertNotIn("0" * 32, str(e.exception) + self.stderr())

    def test_no_token_is_unauthorized_naming_what_is_missing(self):
        c = self.spawn(account=0, prime=False, connect=False,
                       BRUE_CRED_TOKEN="")
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        self.assertEqual(e.exception.code, "unauthorized")
        self.assertIn("API token", str(e.exception))

    def test_the_registry_profile_is_pinned_to_practice(self):
        prof = registry.load()["brokers"]["oanda"]
        self.assertTrue(prof["builtin"])
        self.assertEqual(prof["argv"][-1], "--paper")
        self.assertTrue(prof["argv"][1].endswith(os.path.join(
            "adapters", "oanda", "oanda_adapter.py")))
        # even told OANDA_ENV=live, the profile reaches the practice hosts
        c = Connector(prof["argv"], env=dict(self.env(), OANDA_ENV="live",
                                             OANDA_REST_URL="",
                                             OANDA_STREAM_URL=""))
        self.conns.append(c)
        h = c.hello()
        self.assertEqual(h["mode"], "paper")
        self.assertTrue(c.terms["data_local_only"])
        self.assertFalse(c.terms["orders_by_hand_only"])


class UsAccount(StandIn):
    division = "us"

    def test_a_token_with_one_account_binds_it(self):
        c = self.spawn(account=None)
        self.assertEqual(c.account["account_id"], self.fake.account_ids[0])
        self.assertEqual(c.account["currency"], "USD")


class Trading(StandIn):
    def test_market_round_trip_on_netting(self):
        c = self.spawn(0)
        m = c.mark()
        r = self.place(c, "rt-1")
        self.assertEqual(r["status"], "accepted")
        f = c.wait_event("order.filled", lambda d: d["client_id"] == "rt-1",
                         since=m)
        self.assertEqual((f["position_id"], f["qty"], f["order_id"]),
                         ("EUR_USD", 1000.0, r["order_id"]))
        names = [n for n, d in list(c.events)[-5:]]
        self.assertLess(names.index("order.accepted"),
                        names.index("order.filled"))
        pos = c.request("positions.list")["positions"]
        self.assertEqual([(p["position_id"], p["qty"], p["client_id"])
                          for p in pos], [("EUR_USD", 1000.0, "rt-1")])
        c.request("position.close", {"client_id": "rt-c",
                                     "position_id": "EUR_USD"})
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == "EUR_USD", since=m)
        self.assertEqual((cl["reason"], cl["qty"]), ("manual", 1000.0))
        self.assertEqual(c.request("positions.list")["positions"], [])
        fills = c.request("history.fills")["fills"]
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[0]["fill_id"], f["fill_id"])
        # the platform's client id travels as OANDA's clientExtensions.id
        mo = [t for t in self.txs() if t["type"] == "MARKET_ORDER"
              and t.get("reason") == "CLIENT_ORDER"]
        self.assertEqual(mo[0]["clientExtensions"]["id"], "rt-1")

    def test_a_replay_after_a_restart_does_not_trade_twice(self):
        c = self.spawn(0)
        r1 = self.place(c, "rs-1")
        c.wait_event("order.filled", lambda d: d["client_id"] == "rs-1")
        c.proc.kill()
        c.proc.wait()
        c2 = self.spawn(0, prime=False)
        r2 = self.place(c2, "rs-1")
        self.assertEqual(r1, r2)
        mo = [t for t in self.txs() if t["type"] == "MARKET_ORDER"
              and (t.get("clientExtensions") or {}).get("id") == "rs-1"]
        self.assertEqual(len(mo), 1)
        self.assertEqual(c2.positions["EUR_USD"]["client_id"], "rs-1")

    def test_a_final_refusal_is_the_answer_to_its_replay(self):
        c = self.spawn(0)
        m = c.mark()
        for _ in range(2):
            with self.assertRaises(ConnectorError) as e:
                self.place(c, "rf-1", qty=0.5)
            self.assertEqual(e.exception.code, "invalid_qty")
        rej = [d for n, d in list(c.events) if n == "order.rejected"]
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0]["client_id"], "rf-1")
        # the adapter's own check, beside the SDK's (fresh connection)
        c2 = self.spawn(0, prime=False)
        with self.assertRaises(ConnectorError) as e:
            self.place(c2, "rf-1", qty=7)
        self.assertEqual(e.exception.code, "duplicate_client_id")
        self.assertGreater(c.mark(), m)

    def test_a_lost_answer_is_never_announced_nor_doubled(self):
        """An order whose answer was lost is never sent again: OANDA's
        record answers every retry, and while it shows nothing the retry is
        told "not known", nothing is announced, and session.state says so.
        (Final after UNKNOWN_WATCH: tests/test_oanda_faults.py, cold.)"""
        c = self.spawn(0)
        path = "/v3/accounts/%s/orders" % self.fake.account_ids[0]
        self.fake.fail_next(504, count=1, path=path)
        m = c.mark()
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "lost-1")
        self.assertEqual((e.exception.code, e.exception.retryable),
                         ("internal", True))
        # said first, so a display cut short keeps it
        self.assertTrue(str(e.exception).startswith(
            "internal: Not known yet whether OANDA carried out the order: it "
            "answered HTTP 504 (Gateway Timeout)."), str(e.exception))
        # the words name the order by what it does, not its client_id
        st = c.wait_event("session.state",
                          lambda d: "the order to buy 1000 EUR_USD"
                          in d["detail"], timeout=5, since=m)
        self.assertEqual(st["state"], "degraded")
        self.assertIn("NOT sent again", st["detail"])
        self.assertNotIn("lost-1", st["detail"])
        # asked again at once, and after the 5 s the old rule waited: still
        # not known, and nothing is sent
        for pause in (0, 5.2):
            time.sleep(pause)
            with self.assertRaises(ConnectorError) as e:
                self.place(c, "lost-1")
            self.assertEqual((e.exception.code, e.exception.retryable),
                             ("internal", True))
            self.assertIn("not in OANDA's record yet", str(e.exception))
        self.assertFalse([d for n, d in list(c.events)
                          if n == "order.rejected"])
        self.assertEqual([t for t in self.txs() if t["type"] in (
            "MARKET_ORDER", "ORDER_FILL")], [])
        posts = [r for r in self.fake.requests() if r["method"] == "POST"
                 and r["path"] == path]
        self.assertEqual(len(posts), 1, posts)

    def test_a_lost_answer_whose_order_did_arrive_is_found(self):
        c = self.spawn(0)
        aid = self.fake.account_ids[0]
        self.fake.fail_next(504, count=1, path="/v3/accounts/%s/orders" % aid)
        with self.assertRaises(ConnectorError):
            self.place(c, "arr-1")
        # It reached OANDA after all (the answer was what got lost).
        made = self.direct("POST", "/v3/accounts/%s/orders" % aid, {
            "order": {"type": "MARKET", "instrument": "EUR_USD",
                      "units": "1000", "timeInForce": "FOK",
                      "positionFill": "DEFAULT",
                      "clientExtensions": {"id": "arr-1"},
                      "tradeClientExtensions": {"id": "arr-1"}}})
        r = self.place(c, "arr-1")
        self.assertEqual(r["order_id"], made["orderCreateTransaction"]["id"])
        mo = [t for t in self.txs() if t["type"] == "MARKET_ORDER"]
        self.assertEqual(len(mo), 1)

    def test_insufficient_margin_is_refused(self):
        c = self.spawn(0)
        m = c.mark()
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "im-1", qty=50_000_000)
        self.assertEqual(e.exception.code, "insufficient_margin")
        rej = c.wait_event("order.rejected",
                           lambda d: d["client_id"] == "im-1", since=m)
        self.assertEqual(rej["code"], "insufficient_margin")
        self.assertFalse([t for t in self.txs() if t["type"] ==
                          "MARKET_ORDER"])

    def test_a_shut_market_refuses_with_market_closed(self):
        c = self.spawn(0)
        self.fake.set_market("closed")
        m = c.mark()
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "mc-1")
        self.assertEqual(e.exception.code, "market_closed")
        c.wait_event("order.rejected", lambda d: d["client_id"] == "mc-1",
                     since=m)

    def test_what_oanda_cannot_take_is_refused_with_its_code(self):
        c = self.spawn(0)
        for kw, code in (({"type": "stop_limit", "price": 1.1,
                           "stop_price": 1.2}, "unsupported"),
                         ({"type": "limit", "price": 1.123456},
                          "x_oanda_price_precision"),
                         ({"sym": "UK100_GBP", "qty": 0.15}, "invalid_qty"),
                         ({"sym": "NOPE_XYZ"}, "unknown_symbol"),
                         ({"type": "limit", "price": 1.1, "expiry": "gtd",
                           "expiry_at": 1000}, "invalid_expiry")):
            kw = dict(kw)
            sym = kw.pop("sym", "EUR_USD")
            with self.assertRaises(ConnectorError) as e:
                self.place(c, "bad-%s" % code, sym=sym, **kw)
            self.assertEqual(e.exception.code, code, kw)


class Levels(StandIn):
    def open_trades(self, account=0):
        return self.direct("GET", "/v3/accounts/%s/openTrades"
                           % self.fake.account_ids[account])["trades"]

    def test_a_netting_position_keeps_its_levels_on_every_trade(self):
        c = self.spawn(0)
        bid = c.request("quotes.subscribe", {"symbols": ["EUR_USD"]}) or \
            c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")["bid"]
        sl, tp = round(bid - 0.01, 5), round(bid + 0.01, 5)
        self.place(c, "lv-1", sl=sl, tp=tp)
        c.wait_event("order.filled", lambda d: d["client_id"] == "lv-1")
        # an add carries the position's levels
        self.place(c, "lv-2")
        c.wait_event("order.filled", lambda d: d["client_id"] == "lv-2")
        trades = self.open_trades()
        self.assertEqual(len(trades), 2)
        self.assertEqual({t["stopLossOrder"]["price"] for t in trades},
                         {"%.5f" % sl})
        self.assertEqual({t["takeProfitOrder"]["price"] for t in trades},
                         {"%.5f" % tp})
        pos = c.request("positions.list")["positions"][0]
        self.assertEqual((pos["sl"], pos["tp"], pos["qty"]), (sl, tp, 2000.0))
        # levels on an add: refused, set them on the position
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "lv-3", sl=sl)
        self.assertEqual((e.exception.code, e.exception.like),
                         ("x_oanda_position_open", "trade_restricted"))
        # order.modify moves them on every trade; null clears
        c.request("order.modify", {"client_id": "lv-m", "position_id":
                                   "EUR_USD", "sl": round(sl - 0.001, 5),
                                   "tp": None})
        trades = self.open_trades()
        self.assertEqual({t["stopLossOrder"]["price"] for t in trades},
                         {"%.5f" % round(sl - 0.001, 5)})
        self.assertFalse([t for t in trades if t.get("takeProfitOrder")])

    def test_a_resting_fill_that_adds_is_given_the_levels(self):
        c = self.spawn(0)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        sl = round(q["bid"] - 0.01, 5)
        self.place(c, "rf-1", sl=sl)
        c.wait_event("order.filled", lambda d: d["client_id"] == "rf-1")
        # a buy limit above the ask fills at once at the market
        self.place(c, "rf-2", type="limit", price=round(q["ask"] + 0.002, 5))
        c.wait_event("order.filled", lambda d: d["client_id"] == "rf-2")
        ok = wait(lambda: all(t.get("stopLossOrder") for t in
                              self.open_trades()) and len(
            self.open_trades()) == 2, timeout=12, step=0.5)
        self.assertTrue(ok, self.open_trades())
        self.assertEqual({t["stopLossOrder"]["price"]
                          for t in self.open_trades()}, {"%.5f" % sl})

    def test_stops_and_targets_close_with_their_reason(self):
        c = self.spawn(1)                         # hedging
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        m = c.mark()
        self.place(c, "st-1", sl=round(q["bid"] - 0.002, 5))
        f1 = c.wait_event("order.filled", lambda d: d["client_id"] == "st-1")
        self.place(c, "st-2", tp=round(q["ask"] + 0.002, 5))
        f2 = c.wait_event("order.filled", lambda d: d["client_id"] == "st-2")
        self.assertNotEqual(f1["position_id"], f2["position_id"])
        self.fake.set_price("EUR_USD", bid=round(q["bid"] - 0.005, 5),
                            ask=round(q["bid"] - 0.005 + 0.00014, 5))
        cl = c.wait_event("position.closed", lambda d: d["position_id"] ==
                          f1["position_id"], since=m)
        self.assertEqual(cl["reason"], "sl")
        self.fake.set_price("EUR_USD", bid=round(q["ask"] + 0.005, 5),
                            ask=round(q["ask"] + 0.005 + 0.00014, 5))
        cl = c.wait_event("position.closed", lambda d: d["position_id"] ==
                          f2["position_id"], since=m)
        self.assertEqual(cl["reason"], "tp")

    def test_a_trailing_stop_is_oandas_and_its_moves_are_said(self):
        c = self.spawn(1)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        m = c.mark()
        self.place(c, "tr-1", trail_points=100)   # 0.00100
        f = c.wait_event("order.filled", lambda d: d["client_id"] == "tr-1")
        pos = c.request("positions.list")["positions"][0]
        self.assertEqual(pos["trail_points"], 100.0)
        up = round(q["bid"] + 0.003, 5)
        self.fake.set_price("EUR_USD", bid=up, ask=round(up + 0.00014, 5))
        tr = c.wait_event("position.trailed", lambda d: d["position_id"] ==
                          f["position_id"] and d["sl"] > q["bid"] - 0.0005,
                          timeout=12, since=m)
        self.assertAlmostEqual(tr["sl"], up - 0.001, 5)
        self.assertEqual(tr["trail_points"], 100.0)
        self.fake.set_price("EUR_USD", bid=round(up - 0.002, 5),
                            ask=round(up - 0.002 + 0.00014, 5))
        cl = c.wait_event("position.closed", lambda d: d["position_id"] ==
                          f["position_id"], since=m)
        self.assertEqual((cl["reason"], cl.get("reason_detail")),
                         ("sl", "trailing stop"))

    def test_a_level_taken_off_while_away_is_said_not_put_back(self):
        c = self.spawn(0)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        self.place(c, "aw-1", sl=round(q["bid"] - 0.01, 5))
        c.wait_event("order.filled", lambda d: d["client_id"] == "aw-1")
        c.proc.kill()
        c.proc.wait()
        tid = self.open_trades()[0]["id"]
        self.direct("PUT", "/v3/accounts/%s/trades/%s/orders" % (
            self.fake.account_ids[0], tid), {"stopLoss": None})
        time.sleep(1.2)
        c2 = self.spawn(0, prime=False)
        st = c2.wait_event("session.state", lambda d: "not put back" in
                           d["detail"], timeout=10)
        self.assertIn("EUR_USD", st["detail"])
        time.sleep(LEVELS_WAIT)
        self.assertFalse(self.open_trades()[0].get("stopLossOrder"))
        self.assertIsNone(c2.request("positions.list")["positions"][0]["sl"])

    def test_a_level_already_on_the_wrong_side_is_refused_here(self):
        c = self.spawn(0)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        m = c.mark()
        cases = ({"sl": round(q["ask"] + 0.002, 5)},
                 {"tp": round(q["bid"] - 0.002, 5)},
                 {"type": "limit", "price": 1.0, "sl": 1.01},
                 {"side": "sell", "sl": round(q["bid"] - 0.002, 5)})
        for i, kw in enumerate(cases):
            with self.assertRaises(ConnectorError) as e:
                self.place(c, "ws-%d" % i, **kw)
            self.assertEqual(e.exception.code, "invalid_stops", kw)
            # a final refusal: its one terminal event
            rej = c.wait_event("order.rejected", lambda d, i=i:
                               d["client_id"] == "ws-%d" % i, timeout=2,
                               since=m)
            self.assertEqual(rej["code"], "invalid_stops")
        # nothing reached OANDA
        self.assertEqual([t for t in self.txs() if t["type"] in (
            "MARKET_ORDER", "LIMIT_ORDER")], [])

    def test_a_market_that_moves_past_the_stop_is_a_requote(self):
        """The stop was below the bid the platform was sent; by the fill the
        bid is under it: OANDA cancels the order STOP_LOSS_ON_FILL_LOSS, and
        that is the SPEC's requote, retryable, with nothing announced."""
        c = self.spawn(1)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        sl = round(q["bid"] - 0.00002, 5)
        low = round(q["bid"] - 0.0003, 5)
        m = c.mark()
        self.fake.set_price("EUR_USD", bid=low, ask=round(low + 0.00014, 5))
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "rq-1", sl=sl)
        self.assertEqual((e.exception.code, e.exception.retryable),
                         ("requote", True))
        self.assertIn("STOP_LOSS_ON_FILL_LOSS", str(e.exception))
        self.assertFalse([d for n, d in list(c.events)
                          if n == "order.rejected"
                          and d.get("client_id") == "rq-1"])
        # asked again once the market is back above the stop: it deals
        self.fake.set_price("EUR_USD", bid=q["bid"], ask=q["ask"])
        r = self.place(c, "rq-1", sl=sl)
        self.assertEqual(r["status"], "accepted")
        c.wait_event("order.filled", lambda d: d["client_id"] == "rq-1",
                     since=m)

    def test_a_trail_outside_oandas_range_is_refused(self):
        c = self.spawn(1)
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "tr-x", trail_points=1)     # under 0.00050
        self.assertEqual(e.exception.code, "invalid_stops")

    def test_hedging_partial_close_leaves_the_rest(self):
        c = self.spawn(1)
        m = c.mark()
        self.place(c, "pc-1", qty=1000)
        f = c.wait_event("order.filled", lambda d: d["client_id"] == "pc-1")
        self.place(c, "pc-2", qty=500, side="sell")
        c.wait_event("order.filled", lambda d: d["client_id"] == "pc-2")
        pos = c.request("positions.list")["positions"]
        self.assertEqual(sorted((p["side"], p["qty"]) for p in pos),
                         [("buy", 1000.0), ("sell", 500.0)])
        c.request("position.close", {"client_id": "pc-c",
                                     "position_id": f["position_id"],
                                     "qty": 400})
        cl = c.wait_event("position.closed", lambda d: d["position_id"] ==
                          f["position_id"], since=m)
        self.assertEqual(cl["qty"], 400.0)
        pos = {p["position_id"]: p for p in
               c.request("positions.list")["positions"]}
        self.assertEqual(pos[f["position_id"]]["qty"], 600.0)
        with self.assertRaises(ConnectorError) as e:
            c.request("position.close", {"client_id": "pc-d",
                                         "position_id": f["position_id"],
                                         "qty": 601})
        self.assertEqual(e.exception.code, "invalid_qty")


class Resting(StandIn):
    def test_a_limit_order_from_placed_to_filled(self):
        c = self.spawn(0)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        px = round(q["ask"] - 0.005, 5)
        m = c.mark()
        r = self.place(c, "lim-1", type="limit", price=px, sl=round(
            px - 0.01, 5))
        self.assertEqual(r["status"], "resting")
        placed = c.wait_event("order.placed", lambda d: d["client_id"] ==
                              "lim-1", since=m)
        self.assertEqual((placed["type"], placed["price"]), ("limit", px))
        rows = c.request("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(o["order_id"], o["state"], o["sl"]) for o in rows],
                         [(r["order_id"], "resting", round(px - 0.01, 5))])
        c.request("order.amend", {"client_id": "lim-a", "order_id":
                                  r["order_id"], "price": round(px - 0.001,
                                                                5)})
        am = c.wait_event("order.amended", lambda d: d["order_id"] ==
                          r["order_id"], since=m)
        self.assertEqual(am["price"], round(px - 0.001, 5))
        rows = c.request("orders.list", {"state": "resting"})["orders"]
        self.assertEqual(rows[0]["order_id"], r["order_id"])   # kept
        low = round(px - 0.003, 5)
        self.fake.set_price("EUR_USD", bid=low, ask=round(low + 0.00014, 5))
        f = c.wait_event("order.filled", lambda d: d["client_id"] == "lim-1",
                         since=m)
        self.assertEqual(f["order_id"], r["order_id"])
        with self.assertRaises(ConnectorError) as e:
            c.request("order.cancel", {"client_id": "lim-x",
                                       "order_id": r["order_id"]})
        self.assertEqual(e.exception.code, "not_resting")
        pos = c.request("positions.list")["positions"][0]
        self.assertEqual(pos["sl"], round(px - 0.01, 5))

    def test_cancel_expiry_and_day_orders(self):
        c = self.spawn(0)
        m = c.mark()
        r = self.place(c, "cx-1", type="limit", price=1.0)
        c.request("order.cancel", {"client_id": "cx-c",
                                   "order_id": r["order_id"]})
        cx = c.wait_event("order.cancelled", lambda d: d["order_id"] ==
                          r["order_id"], since=m)
        self.assertNotIn("code", cx)          # the platform cancelled it
        now = c.request("ping")["time"]
        r2 = self.place(c, "gtd-1", type="stop", price=2.0, expiry="gtd",
                        expiry_at=now + 3000)
        self.fake.advance(10)
        ex = c.wait_event("order.expired", lambda d: d["order_id"] ==
                          r2["order_id"], since=m)
        self.assertEqual(ex["expiry"], "gtd")
        r3 = self.place(c, "day-1", type="limit", price=1.0, expiry="day")
        row = [o for o in c.request("orders.list", {"state": "resting"})[
            "orders"] if o["order_id"] == r3["order_id"]][0]
        from brueconnect.adapters.oanda.oanda_adapter import \
            next_new_york_five
        self.assertEqual(row["expires_at"],
                         next_new_york_five(row["placed_at"]))
        self.assertEqual(row["expiry"], "day")


class Flatten(StandIn):
    def test_flatten_all_cancels_what_rests_and_closes_everything(self):
        for account in (0, 1):
            self.fake.reset()
            c = self.spawn(account)
            for i in range(3):
                self.place(c, "fl-%d-%d" % (account, i))
            self.place(c, "fl-l-%d" % account, type="limit", price=1.0)
            out = c.request("flatten_all", {"client_id": "flat-%d" % account})
            self.assertEqual(out["closed"], 1 if account == 0 else 3)
            self.assertEqual(c.request("positions.list")["positions"], [])
            self.assertEqual(c.request("orders.list", {"state": "resting"})[
                "orders"], [])
            closes = [d for n, d in list(c.events) if n == "position.closed"]
            self.assertTrue(closes and all(d["reason"] == "flatten"
                                           for d in closes))
            # a replay after a new position is opened does not close it
            self.place(c, "fl-n-%d" % account)
            again = c.request("flatten_all", {"client_id": "flat-%d"
                                              % account})
            self.assertEqual(again, out)
            self.assertEqual(len(c.request("positions.list")["positions"]),
                             1)
            c.close()


class Flatten429(StandIn):
    def test_flatten_all_is_not_held_back_by_a_429_pause(self):
        """SPEC 8.1: flatten_all succeeds while other calls are rate
        limited. Two 429s make reads wait (1 s, then 2 s); flatten_all goes
        through the pause at once."""
        c = self.spawn(0)
        self.place(c, "f429-1")
        aid = self.fake.account_ids[0]
        self.fake.fail_next(429, count=2,
                            path="/v3/accounts/%s/summary" % aid)
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        self.assertEqual(e.exception.code, "rate_limited")
        t0 = time.monotonic()
        out = c.request("flatten_all", {"client_id": "f429-f"})
        self.assertEqual(out["closed"], 1)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(c.request("positions.list")["positions"], [])


    def test_flatten_all_closes_even_when_resting_orders_cannot_be_read(self):
        c = self.spawn(0)
        self.place(c, "fo-1")
        r = self.place(c, "fo-l", type="limit", price=1.0)
        aid = self.fake.account_ids[0]
        self.fake.fail_next(503, count=2,
                            path="/v3/accounts/%s/pendingOrders" % aid)
        with self.assertRaises(ConnectorError) as e:
            c.request("flatten_all", {"client_id": "fo-f"})
        self.assertEqual((e.exception.code, e.exception.retryable),
                         ("x_oanda_orders_left", True))
        self.assertIn("every position is closed (1)", str(e.exception))
        self.assertEqual(c.request("positions.list")["positions"], [])
        out = c.request("flatten_all", {"client_id": "fo-f"})
        self.assertEqual(out["closed"], 0)
        self.assertEqual(c.request("orders.list", {"state": "resting"})[
            "orders"], [])
        c.wait_event("order.cancelled", lambda d: d["order_id"] ==
                     r["order_id"])


class Data(StandIn):
    def test_5000_bid_bars_in_one_request(self):
        c = self.spawn(0)
        t0 = time.monotonic()
        bars = c.request("bars.history", {"symbol": "EUR_USD",
                                          "timeframe": "1m",
                                          "count": 5000})["bars"]
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(len(bars), 5000)
        ts = [b[0] for b in bars]
        self.assertEqual(ts, sorted(ts))
        # the stand-in's own bid candles, closed ones only
        raw = self.direct("GET", "/v3/accounts/%s/instruments/EUR_USD/candles"
                          "?price=B&granularity=M1&count=50&dailyAlignment=0"
                          "&alignmentTimezone=UTC" % self.fake.account_ids[0])
        mine = {b[0]: b for b in bars}
        checked = 0
        for k in raw["candles"]:
            if not k["complete"]:
                continue
            from brueconnect.adapters.oanda.oanda_adapter import to_ms
            row = mine.get(to_ms(k["time"]))
            if row is None:
                continue
            self.assertEqual(row[1:5], [float(k["bid"][x]) for x in "ohlc"])
            checked += 1
        self.assertGreater(checked, 10)
        reqs = [r for r in self.fake.requests() if "/candles" in r["path"]]
        self.assertTrue(all("price=B" in r["query"] for r in reqs))

    def test_4h_and_day_bars_start_on_utc_multiples(self):
        c = self.spawn(0)
        for tf, width in (("4h", 14_400_000), ("1d", 86_400_000)):
            bars = c.request("bars.history", {"symbol": "EUR_USD",
                                              "timeframe": tf,
                                              "count": 200})["bars"]
            self.assertEqual(len(bars), 200)
            self.assertTrue(all(b[0] % width == 0 for b in bars), tf)

    def test_more_than_a_page_and_the_start_of_history(self):
        c = self.spawn(0)
        bars = c.request("bars.history", {"symbol": "EUR_USD",
                                          "timeframe": "1h",
                                          "count": 12000})["bars"]
        self.assertEqual(len(bars), 12000)
        self.assertEqual(len({b[0] for b in bars}), 12000)
        day = c.request("bars.history", {"symbol": "EUR_USD",
                                         "timeframe": "1d",
                                         "count": 20000})["bars"]
        self.assertLess(len(day), 20000)          # history starts in 2005
        self.assertGreaterEqual(day[0][0], 1104537600000)

    def test_quotes_stream_and_a_repeat_asks_get_one_at_once(self):
        c = self.spawn(0)
        m = c.mark()
        c.request("quotes.subscribe", {"symbols": ["EUR_USD", "XAU_USD"]})
        for s in ("EUR_USD", "XAU_USD"):
            q = c.wait_event("quote", lambda d, s=s: d["symbol"] == s,
                             since=m)
            self.assertLess(q["bid"], q["ask"])
        m = c.mark()
        c.request("quotes.subscribe", {"symbols": ["XAU_USD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "XAU_USD", timeout=1,
                     since=m)
        c.request("quotes.unsubscribe", {"symbols": ["XAU_USD"]})
        time.sleep(1.0)
        m = c.mark()
        with self.assertRaises(TimeoutError):
            c.wait_event("quote", lambda d: d["symbol"] == "XAU_USD",
                         timeout=2.5, since=m)
        # the one still asked for goes on
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD", timeout=5,
                     since=m)

    def test_streams_recover_and_a_gap_is_filled(self):
        c = self.spawn(0)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        m = c.mark()
        # an order of another tool of the user's, while the streams are down
        self.fake.drop_streams(clean=False)
        self.direct("POST", "/v3/accounts/%s/orders"
                    % self.fake.account_ids[0], {
                        "order": {"type": "MARKET", "instrument": "EUR_USD",
                                  "units": "100", "timeInForce": "FOK",
                                  "positionFill": "DEFAULT"}})
        f = c.wait_event("order.filled", lambda d: d["symbol"] == "EUR_USD",
                         timeout=15, since=m)
        self.assertIsNone(f["client_id"])
        self.assertEqual(f["qty"], 100.0)
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD", timeout=15,
                     since=m)
        ok = wait(lambda: c.session_state["state"] == "connected", 15)
        self.assertTrue(ok, c.session_state)

    def test_a_401_on_reconnect_with_a_good_token_is_rechecked(self):
        c = self.spawn(0)
        self.fake.refuse_connects(1, 401, "stream")
        m = c.mark()
        self.fake.drop_streams(clean=True, kind="transactions")
        st = c.wait_event("session.state", lambda d: "401" in d["detail"],
                          timeout=15, since=m)
        self.assertIn("token still works", st["detail"])
        c.wait_event("session.state", lambda d: d["state"] == "connected",
                     timeout=15, since=m)
        self.assertEqual(c.request("account.get")["currency"], "GBP")

    def test_a_429_is_waited_out(self):
        c = self.spawn(0)
        self.fake.fail_next(429, count=1, path="/v3/accounts/%s/summary"
                            % self.fake.account_ids[0])
        self.assertEqual(c.request("account.get")["currency"], "GBP")

    def test_operations_and_their_history(self):
        c = self.spawn(0)
        m = c.mark()
        self.fake.transfer(self.fake.account_ids[0], "-250")
        op = c.wait_event("balance.operation", since=m, timeout=15)
        self.assertEqual((op["kind"], op["amount"], op["currency"]),
                         ("withdrawal", -250.0, "GBP"))
        hist = c.request("history.operations")["operations"]
        self.assertEqual(hist[-1]["operation_id"], op["operation_id"])
        # the opening funding (January) is in a range that reaches it
        hist = c.request("history.operations", {"from": 0})["operations"]
        self.assertEqual(hist[0]["kind"], "deposit")

    def test_no_connection_was_refused_for_coming_too_fast(self):
        self.fake.requests(clear=True)
        c = self.spawn(0)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        c.request("quotes.subscribe", {"symbols": ["XAU_USD"]})
        c.request("quotes.subscribe", {"symbols": ["GBP_USD"]})
        for i in range(5):
            self.place(c, "nc-%d" % i)
        c.request("bars.history", {"symbol": "EUR_USD", "timeframe": "5m",
                                   "count": 100})
        time.sleep(2)
        refused = [r for r in self.fake.requests() if r["status"] == 0]
        self.assertEqual(refused, [])


class Mt4(StandIn):
    def test_an_mt4_account_never_gets_client_extensions(self):
        c = self.spawn(2)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        self.place(c, "m4-1", sl=round(q["bid"] - 0.01, 5))
        f = c.wait_event("order.filled", lambda d: d["client_id"] == "m4-1")
        c.request("order.modify", {"client_id": "m4-m", "position_id":
                                   f["position_id"],
                                   "tp": round(q["ask"] + 0.01, 5)})
        self.place(c, "m4-2", type="limit", price=1.0)
        c.request("position.close", {"client_id": "m4-c",
                                     "position_id": f["position_id"],
                                     "qty": 500})
        c.request("flatten_all", {"client_id": "m4-f"})
        self.assertEqual(self.fake.violations(), [])
        self.assertFalse([t for t in self.txs(2) if t.get(
            "clientExtensions") or t.get("tradeClientExtensions")])

    def test_an_mt4_position_keeps_its_client_id_across_a_restart(self):
        c = self.spawn(2)
        self.place(c, "m4-r")
        c.wait_event("order.filled", lambda d: d["client_id"] == "m4-r")
        c.proc.kill()
        c.proc.wait()
        time.sleep(1.2)
        c2 = self.spawn(2, prime=False)
        self.assertEqual(c2.positions["EUR_USD"]["client_id"], "m4-r")
        self.assertEqual(self.fake.violations(), [])


# The adapter file unchanged, run as its main() runs it, except that its hello
# says "live" (on the stand-in's loopback hosts it says "paper"), so the
# platform's arming rule applies as on OANDA's live hosts. Every method name
# it is sent is written to `trace` first. It will not start unless both
# endpoints are this machine.
LIVE_MODE_WRAPPER = """
import importlib.util, ipaddress, signal, sys, urllib.parse
sys.path.insert(0, {root!r})
spec = importlib.util.spec_from_file_location("oanda_adapter", {adapter!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["oanda_adapter"] = mod
spec.loader.exec_module(mod)
adapter = mod.OandaAdapter()
for key in ("rest_url", "stream_url"):
    host = urllib.parse.urlsplit(adapter.cfg[key]).hostname or ""
    try:
        here = ipaddress.ip_address(host).is_loopback
    except ValueError:
        here = False
    if not here:
        sys.exit(3)
adapter.mode = "live"
real = adapter._dispatch


def traced(rid, method, params):
    with open({trace!r}, "a") as fh:
        fh.write(method + "\\n")
    return real(rid, method, params)


adapter._dispatch = traced
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    adapter.run()
finally:
    adapter._stop.set()
    adapter._save_state(wait=2.0)
"""


class Names(StandIn):
    """The Connector, the adapter with a live hello, unarmed, the stand-in
    behind it. The arming, idempotency, handshake and by-hand checks know a
    mutating method by its exact name; with a handler found by
    method.replace(".", "_"), "order_place" would be answered with an order
    the stand-in filled, and "flatten.all" would close the book. A method
    is its exact name in SPEC 4, and no other spelling of one reaches a
    handler."""

    def live(self):
        trace = os.path.join(self.dir, "received.txt")
        path = os.path.join(self.dir, "live_mode.py")
        with open(path, "w") as fh:
            fh.write(LIVE_MODE_WRAPPER.format(root=ROOT, adapter=ADAPTER,
                                              trace=trace))
        self.remember()
        err = open(os.path.join(self.dir, "stderr-live.txt"), "w")
        self.files.append(err)
        c = Connector([sys.executable, path, "--live"], env=self.env(0),
                      stderr=err)
        self.conns.append(c)
        c.connect()
        return c, trace

    def test_another_spelling_of_a_method_trades_nothing(self):
        c, trace = self.live()
        self.assertEqual((c.handshake.get("mode"), c.is_paper, c.armed),
                         ("live", False, False))
        self.fake.requests(clear=True)
        order = {"symbol": "EUR_USD", "side": "buy", "qty": 1}
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(order, client_id="n-dotted"))
        for name in ("order_place", "flatten.all", "position_close"):
            with self.subTest(name=name):
                with self.assertRaises(ConnectorError) as cm:
                    c.request(name, dict(order, client_id="n-" + name))
                self.assertEqual(cm.exception.code, "unsupported")
        # The same names written to the adapter past the Connector's rule,
        # as a platform that is not this SDK may: the adapter refuses them.
        for name in ("order_place", "flatten.all"):
            with self.subTest(wire=name):
                with self.assertRaises(ConnectorError) as cm:
                    c._send(name, dict(order, client_id="w-" + name), 15)
                self.assertEqual(cm.exception.code, "unsupported")
        time.sleep(0.6)
        self.assertEqual([(r["method"], r["path"]) for r in
                          self.fake.requests() if r["method"] != "GET"], [])
        self.assertEqual(c.request("positions.list")["positions"], [])
        with open(trace) as fh:
            got = fh.read().split()
        self.assertEqual([m for m in got if m not in protocol.METHODS],
                         ["order_place", "flatten.all"])
        self.assertNotIn("position_close", got)
        # The control: armed, the protocol's own name is carried out.
        c.arm("the names test's control, on the stand-in")
        c.request("order.place", dict(order, client_id="n-armed"))
        self.assertEqual([r["status"] for r in self.fake.requests()
                          if r["method"] == "POST"
                          and r["path"].endswith("/orders")], [201])


# The adapter file unchanged, run as its main() runs it, behind an audit hook
# of the test's own: a name lookup or a connect off this machine is written
# down and refused before anything leaves.
OFF_THIS_MACHINE = """
import ipaddress, runpy, sys
SEEN = {seen!r}


def here(host):
    h = str(host).strip("[]").lower().rstrip(".")
    if h in ("", "localhost") or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h.split("%")[0]).is_loopback
    except ValueError:
        return False


def hook(event, args):
    what = None
    if event in ("socket.getaddrinfo", "socket.gethostbyname") and args \\
            and not here(args[0]):
        what = "%s %r" % (event, args[0])
    elif event == "socket.connect" and len(args) > 1 \\
            and isinstance(args[1], tuple) and args[1] \\
            and not here(args[1][0]):
        what = "connect %r" % (args[1],)
    if what:
        with open(SEEN, "a") as fh:
            fh.write(what + "\\n")
        raise PermissionError("test hook: " + what)


sys.addaudithook(hook)
sys.argv = [{adapter!r}] + sys.argv[1:]
runpy.run_path({adapter!r}, run_name="__main__")
"""


class Redirects(StandIn):
    """OANDA's live API, 2026-11-15: two account summary reads answered
    HTTP 307, and a read a minute later did not. The rule: a request
    answered 307 or 308 is followed only if its method is GET, only once,
    and only to the same scheme, host and port as the endpoint it was sent
    to (the path and query may change), on REST and the streams alike.
    Nothing else is followed. A request that is not a GET answered 307 or
    308 was not carried out: refused, final for its client_id, never sent
    again; after another 3xx its fate is not known. Each refusal says which
    rule it met and names the target's host, never its path or query."""

    def spawn_quiet(self):
        """An adapter whose own reads of the summary (a poll after its first
        transactions) are over, so the next one is the test's."""
        c = self.spawn(0)
        seen, still = -1, time.monotonic()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            n = len(self.asked("/summary"))
            if n != seen:
                seen, still = n, time.monotonic()
            elif time.monotonic() - still > 1.2:
                break
            time.sleep(0.1)
        return c

    def asked(self, tail, method="GET"):
        return [r["status"] for r in self.fake.requests()
                if r["method"] == method and r["path"].endswith(tail)]

    def test_a_read_answered_307_or_308_is_followed_once(self):
        c = self.spawn_quiet()
        for status in (307, 308):
            with self.subTest(status=status):
                self.fake.requests(clear=True)
                self.fake.redirect_next(
                    count=1, status=status, method="GET",
                    path="/v3/accounts/%s/summary" % self.fake.account_ids[0])
                acct = c.request("account.get")
                self.assertIn("balance", acct)
                asked = self.asked("/summary")
                self.assertEqual(asked[:1], [status])
                self.assertIn(200, asked[1:])

    def test_a_second_redirect_is_not_followed(self):
        c = self.spawn_quiet()
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=2, method="GET", path="/v3/accounts/%s/"
                                "summary" % self.fake.account_ids[0])
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        self.assertEqual((cm.exception.code, cm.exception.effective_code),
                         ("x_oanda_redirected", "internal"))
        self.assertIn("not followed", str(cm.exception))
        self.assertIn("followed once", str(cm.exception))
        self.assertIn("127.0.0.1", str(cm.exception))
        self.assertNotIn("/v3", str(cm.exception))
        self.assertEqual(self.asked("/summary"), [307, 307])

    def test_a_summary_read_sent_to_another_path_of_its_host_is_followed(self):
        """The path and query may change: the account summary read sent on
        to the account's own path on the same host is followed once."""
        c = self.spawn_quiet()
        aid = self.fake.account_ids[0]
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=1, method="GET",
                                path="/v3/accounts/%s/summary" % aid,
                                location=self.fake.rest_url
                                + "/v3/accounts/%s?moved=1" % aid)
        acct = c.request("account.get")
        self.assertIn("balance", acct)
        asked = [(r["path"], r["query"], r["status"])
                 for r in self.fake.requests() if r["method"] == "GET"
                 and r["path"].startswith("/v3/accounts/%s" % aid)]
        self.assertEqual(asked[:2], [
            ("/v3/accounts/%s/summary" % aid, "", 307),
            ("/v3/accounts/%s" % aid, "moved=1", 200)])

    def test_a_read_is_not_sent_from_the_rest_host_to_the_stream_host(self):
        """The REST host's read sent on to the stream host (here the
        stand-in's stream port): another port, so refused, naming the host
        and the port, and nothing of it reaches the stream host."""
        c = self.spawn_quiet()
        aid = self.fake.account_ids[0]
        self.fake.requests(clear=True)
        path = "/v3/accounts/%s/summary" % aid
        self.fake.redirect_next(count=1, method="GET", path=path,
                                location=self.fake.stream_url + path)
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        said = str(cm.exception)
        self.assertEqual(cm.exception.code, "x_oanda_redirected")
        self.assertIn("127.0.0.1 on port %d" % self.fake.stream_port, said)
        self.assertIn("scheme, host and port", said)
        self.assertNotIn("/v3", said)
        time.sleep(0.3)
        self.assertEqual([r for r in self.fake.requests()
                          if r["host"] == "stream" and r["path"] == path], [])

    def test_a_paper_process_makes_no_lookup_of_the_live_host(self):
        """A --paper process whose summary read is answered 307 toward
        OANDA's live host does not follow it there with its token. It is
        refused by the rule, naming the host, and no name is looked up."""
        seen = os.path.join(self.dir, "off-this-machine.txt")
        path = os.path.join(self.dir, "hooked.py")
        with open(path, "w") as fh:
            fh.write(OFF_THIS_MACHINE.format(seen=seen, adapter=ADAPTER))
        self.remember()
        err = open(os.path.join(self.dir, "stderr-hooked.txt"), "w")
        self.files.append(err)
        c = Connector([sys.executable, path, "--paper"], env=self.env(0),
                      stderr=err)
        self.conns.append(c)
        c.connect()
        self.assertTrue(c.is_paper)
        time.sleep(2.0)
        aid = self.fake.account_ids[0]
        self.fake.redirect_next(
            count=1, method="GET", path="/v3/accounts/%s/summary" % aid,
            location="https://api-fxtrade.oanda.com/v3/accounts/%s/summary"
                     % aid)
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        said = str(cm.exception)
        self.assertEqual(cm.exception.code, "x_oanda_redirected", said)
        self.assertIn("api-fxtrade.oanda.com", said)
        self.assertNotIn("/v3", said)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(seen) and open(seen).read(),
                         "the adapter tried to leave this machine")

    def test_the_levels_put_answered_307_is_sent_once_not_followed(self):
        """Decided by the call's kind, the level-keeping PUT (a background
        call) answered 307 would be sent again, body and all, to where the
        Location pointed. It is a PUT: sent once and not followed, and the
        user is told it was not carried out; the job's next pass asks the
        endpoint again, and the stop is put on."""
        c = self.spawn(0)
        aid = self.fake.account_ids[0]
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        q = c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        sl = round(q["bid"] - 0.01, 5)
        from tests.fake_oanda import parse_time
        self.place(c, "rl-1", sl=sl)
        c.wait_event("order.filled", lambda d: d["client_id"] == "rl-1")
        time.sleep(LEVELS_WAIT)         # a levels round has seen its stop
        self.fake.requests(clear=True)
        self.fake.redirect_next(
            count=1, method="PUT", path="/v3/accounts/%s/trades/" % aid,
            location="/v3/accounts/%s/trades/0/orders?followed=1" % aid)
        mark = c.mark()
        # a buy limit above the ask fills at once, as a trade of its own
        self.place(c, "rl-2", type="limit", price=round(q["ask"] + 0.002, 5))
        c.wait_event("order.filled", lambda d: d["client_id"] == "rl-2")
        st = c.wait_event("session.state", lambda d: "could not be put"
                          in (d.get("detail") or ""), since=mark, timeout=15)
        # The words of a stop or target the adapter keeps, not of an order
        # the user sent ("nor sent again: send it again as a new request"
        # beside "tried again in 5 s", and ".;").
        self.assertRegex(st["detail"], r"^the stop/target of EUR_USD could "
                         r"not be put on its trade \d+: OANDA answered HTTP "
                         r"307, a redirect to 127\.0\.0\.1, the endpoint "
                         r"itself, and a redirect is never followed for a "
                         r"change; tried again in 5 s$")
        for wrong in ("nor sent again", "send it again", ".;", ".."):
            self.assertNotIn(wrong, st["detail"])
        ok = wait(lambda: len(self.open_trades()) == 2 and all(
            t.get("stopLossOrder") for t in self.open_trades()),
            timeout=15, step=0.5)
        self.assertTrue(ok, self.open_trades())
        puts = [r for r in self.fake.requests() if r["method"] == "PUT"]
        hit = [i for i, r in enumerate(puts) if r["status"] == 307]
        self.assertEqual(len(hit), 1, puts)
        first = puts[hit[0]]
        # Not sent again by the call: a later PUT of the same trade's levels
        # is the next pass's, LEVELS_EVERY (5 s) later, never at once (a
        # PUT of another trade's levels is that trade's own).
        for r in puts[hit[0] + 1:]:
            if r["path"] == first["path"]:
                self.assertGreater(parse_time(r["at"]) - parse_time(
                    first["at"]), 3 * 10 ** 9, puts)
        # and never followed
        self.assertEqual([r for r in self.fake.requests()
                          if "followed" in (r.get("query") or "")], [])

    def open_trades(self, account=0):
        return self.direct("GET", "/v3/accounts/%s/openTrades"
                           % self.fake.account_ids[account])["trades"]

    def test_flatten_all_whose_closes_are_answered_307_says_it_once(self):
        """flatten_all does not quote the refusal's two sentences for the
        platform's own orders and then add ". Ask again." ("...still
        wanted.. Ask again."). One sentence, once."""
        c = self.spawn(0)
        aid = self.fake.account_ids[0]
        self.place(c, "fl-1")
        c.wait_event("order.filled", lambda d: d["client_id"] == "fl-1")
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=5, method="PUT",
                                path="/v3/accounts/%s/positions/" % aid)
        with self.assertRaises(ConnectorError) as cm:
            c.request("flatten_all", {"client_id": "fl-flat"})
        e = cm.exception
        self.assertEqual((e.code, e.retryable), ("x_oanda_not_flat", True))
        self.assertEqual(str(e), "x_oanda_not_flat: not flat: not closed: "
                         "EUR_USD: OANDA answered HTTP 307, a redirect to "
                         "127.0.0.1, the endpoint itself, and a redirect is "
                         "never followed for a change. Ask again.")
        closes = self.asked("/positions/EUR_USD/close", "PUT")
        self.assertTrue(closes and set(closes) == {307}, closes)
        self.assertEqual([p["qty"] for p in
                          c.request("positions.list")["positions"]], [1000.0])

    def test_a_read_sent_on_to_a_path_that_is_not_ascii_is_refused(self):
        """An account read answered 307 to a path of its own host with a
        latin-1 letter in it does not escape as "internal:
        UnicodeEncodeError: 'ascii' codec ...". It is refused as not a plain
        URL, naming the endpoint's host, and the next read is answered."""
        c = self.spawn_quiet()
        aid = self.fake.account_ids[0]
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=1, method="GET",
                                path="/v3/accounts/%s/summary" % aid,
                                location="/v3/accounts/%s/r\xe9sum\xe9"
                                % aid)
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        e = cm.exception
        self.assertEqual((e.code, e.retryable), ("x_oanda_redirected", False))
        self.assertIn("not a plain URL", str(e))
        self.assertIn("127.0.0.1", str(e))
        for part in ("Unicode", "codec", "/v3", "sum"):
            self.assertNotIn(part, str(e))
        self.assertIn("balance", c.request("account.get"))

    def test_a_price_stream_sent_on_its_own_host_is_followed_and_streams(
            self):
        """A stream is a GET: followed once to its own host (here with a
        query of the stand-in's own, to know it), it streams; to another
        port it is refused. Followed nowhere, a price stream answered 307
        would fail, with prices polled meanwhile and the stream asked for
        later."""
        c = self.spawn_quiet()
        aid = self.fake.account_ids[0]
        path = "/v3/accounts/%s/pricing/stream" % aid
        self.fake.requests(clear=True)
        self.fake.redirect_next(
            count=1, kind="stream", method="GET", path=path,
            location=self.fake.stream_url + path
            + "?instruments=EUR_USD&snapshot=true&followed=1")
        mark = c.mark()
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD", since=mark)
        time.sleep(1.0)
        self.fake.drop_streams(True, "pricing")     # its end is logged
        self.assertTrue(wait(lambda: [
            r for r in self.fake.requests() if r["host"] == "stream"
            and r["path"] == path and r["status"] == 200], timeout=10))
        streams = [(r["status"], "followed=1" in (r["query"] or ""))
                   for r in self.fake.requests()
                   if r["host"] == "stream" and r["path"] == path]
        self.assertEqual(streams[:2], [(307, False), (200, True)])
        # (no price stream was asked for before this one)
        self.assertEqual([d for n, d in list(c.events)
                          if n == "session.state"
                          and "price stream is not connected"
                          in (d.get("detail") or "")], [])
        # sent on to another port (the stand-in's REST port): refused
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=1, kind="stream", method="GET",
                                path=path, location=self.fake.rest_url + path)
        mark = c.mark()
        c.request("quotes.subscribe", {"symbols": ["GBP_USD"]})
        st = c.wait_event("session.state", lambda d: "not followed" in (
            d.get("detail") or ""), since=mark, timeout=15)
        self.assertIn("127.0.0.1 on port %d" % self.fake.port, st["detail"])
        self.assertEqual([r for r in self.fake.requests()
                          if r["host"] == "rest" and r["path"] == path], [])

    def test_a_read_is_not_followed_anywhere_else(self):
        """A Location on another scheme or port than the endpoint's (here
        another port of this machine, which writes down what reaches it) is
        not followed: nothing reaches it, the token least of all."""
        import http.server
        import threading
        got = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                got.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = self.spawn_quiet()
        aid = self.fake.account_ids[0]
        for where in ("http://127.0.0.1:%d/v3/accounts/%s/summary"
                      % (srv.server_address[1], aid),
                      "https://127.0.0.1:%d/v3/accounts/%s/summary"
                      % (srv.server_address[1], aid)):
            with self.subTest(where=where):
                self.fake.redirect_next(count=1, method="GET", location=where,
                                        path="/v3/accounts/%s/summary" % aid)
                with self.assertRaises(ConnectorError) as cm:
                    c.request("account.get")
                self.assertEqual(cm.exception.code, "x_oanda_redirected")
        self.assertEqual(got, [])

    def test_an_order_answered_307_is_not_carried_out_nor_sent_again(self):
        c = self.spawn(0)
        aid = self.fake.account_ids[0]
        mark = c.mark()
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=1, method="POST",
                                path="/v3/accounts/%s/orders" % aid)
        p = {"client_id": "rd-1", "symbol": "EUR_USD", "side": "buy",
             "qty": 1000}
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", p)
        e = cm.exception
        self.assertEqual((e.code, e.effective_code, e.retryable),
                         ("x_oanda_redirected", "internal", False))
        self.assertIn("not carried out", str(e)[:80])
        self.assertIn("127.0.0.1", str(e))
        self.assertNotIn("/v3", str(e))
        rej = c.wait_event("order.rejected",
                           lambda d: d["client_id"] == "rd-1", since=mark)
        self.assertEqual(rej["code"], "x_oanda_redirected")
        # asked again under the same client_id: the same answer, nothing sent
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", p)
        self.assertEqual(cm.exception.code, "x_oanda_redirected")
        self.assertEqual(self.asked("/orders", "POST"), [307])
        self.assertEqual(self.oanda_trades(), [])
        self.assertEqual(c.request("positions.list")["positions"], [])

    def test_an_order_answered_another_redirect_is_of_unknown_fate(self):
        """After a 303, say, OANDA may have carried the order out (a 303 can
        answer a request done): its fate is not known, it is never sent
        again, and OANDA's record answers for it."""
        c = self.spawn(0)
        aid = self.fake.account_ids[0]
        self.fake.requests(clear=True)
        self.fake.redirect_next(count=1, method="POST", status=303,
                                path="/v3/accounts/%s/orders" % aid)
        p = {"client_id": "rd-2", "symbol": "EUR_USD", "side": "buy",
             "qty": 1000}
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", p)
        e = cm.exception
        self.assertEqual((e.code, e.retryable), ("internal", True))
        self.assertTrue(str(e).startswith(
            "internal: Not known yet whether OANDA carried out the order: it "
            "answered HTTP 303"), str(e))
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", p)
        self.assertEqual((cm.exception.code, cm.exception.retryable),
                         ("internal", True))
        self.assertEqual(self.asked("/orders", "POST"), [303])

    def oanda_trades(self):
        aid = self.fake.account_ids[0]
        return self.direct("GET", "/v3/accounts/%s/openTrades" % aid)["trades"]


class Secrets(StandIn):
    def test_the_token_never_leaves_by_any_path(self):
        """stdout (every message), stderr and the state file, through a
        session with refusals, lost answers, a revoked token and a bad one."""
        env = self.env(0)
        tok = self.fake.token
        c = Connector([sys.executable, ADAPTER, "--paper"], env=self.env())
        self.conns.append(c)
        c.hello()
        c.request("accounts.list")
        c.close()
        lines = [
            {"method": "hello", "params": {"protocol": 1}},
            {"method": "account.get"},
            {"method": "order.place", "params": {
                "client_id": "s-1", "symbol": "EUR_USD", "side": "buy",
                "qty": 1000, "sl": 0.5}},
            {"method": "order.place", "params": {
                "client_id": "s-2", "symbol": "EUR_USD", "side": "buy",
                "qty": 0.5}},
            {"method": "positions.list"},
            {"method": "history.fills"},
            {"method": "no.such.method"}]
        p = subprocess.Popen([sys.executable, ADAPTER, "--paper"], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        try:
            for i, msg in enumerate(lines):
                msg = dict({"v": 1, "id": "r%d" % i, "type": "req",
                            "params": {}}, **msg)
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
                time.sleep(0.3)
            self.fake.fail_next(504, count=1,
                                path="/v3/accounts/%s/orders"
                                % self.fake.account_ids[0])
            p.stdin.write(json.dumps({"v": 1, "id": "r-lost", "type": "req",
                                      "method": "order.place", "params": {
                                          "client_id": "s-3",
                                          "symbol": "EUR_USD",
                                          "side": "buy", "qty": 10}}) + "\n")
            p.stdin.flush()
            time.sleep(1.0)
            self.fake.revoke_token()
            for i, method in enumerate(("account.get", "positions.list",
                                        "orders.list")):
                p.stdin.write(json.dumps({"v": 1, "id": "x%d" % i,
                                          "type": "req", "method": method,
                                          "params": {}}) + "\n")
                p.stdin.flush()
            time.sleep(2.5)
            p.stdin.write(json.dumps({"v": 1, "id": "bye", "type": "req",
                                      "method": "bye", "params": {}}) + "\n")
            p.stdin.flush()
            out, err = p.communicate(timeout=20)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn('"id": "x0"', out)
        self.assertIn("unauthorized", out)
        with open(os.path.join(self.dir, "oanda_state.json")) as fh:
            state = fh.read()
        for where, text in (("stdout", out), ("stderr", err),
                            ("state", state)):
            self.assertNotIn(tok, text, where)
            self.assertNotIn(tok[:16], text, where)
        # a malformed token is refused naming the field, never quoting it
        bad = "abc def" + tok[8:]
        c = Connector([sys.executable, ADAPTER, "--paper"],
                      env=dict(env, BRUE_CRED_TOKEN=bad))
        self.conns.append(c)
        c.hello()
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        self.assertEqual(e.exception.code, "unauthorized")
        self.assertNotIn(tok[8:], str(e.exception))


if __name__ == "__main__":
    unittest.main()
