"""Capital.com adapter against the stand-in (tests/fake_capitalcom.py).

Each test spawns the adapter the way a platform does (the SDK's Connector,
the fake credentials and endpoints in its environment, a state directory of
its own) and reads what a platform would be told. The stand-in plays
Capital.com's demo host from its documentation; where the documentation is
silent it plays one reading by default and the tests switch it to the other
(set_option) where the adapter must survive both.
"""
import http.client
import io
import json
import os
import sys
import tempfile
import time
import unittest

from brueconnect import Connector, ConnectorError
from brueconnect.adapters.capitalcom import capitalcom_adapter as mod
from tests.fake_capitalcom import FakeCapitalCom

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(ROOT, "brueconnect", "adapters", "capitalcom",
                       "capitalcom_adapter.py")
TMP = os.environ.get("TMPDIR") or None


class StandIn(unittest.TestCase):
    MARKET = "open"

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCapitalCom(market=cls.MARKET, balances=(
            ("USD", "10000"), ("GBP", "5000"))).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.fake.requests(clear=True)
        self.fake.stream_log(clear=True)
        self.state = tempfile.mkdtemp(prefix="cc-test-", dir=TMP)
        self.err = tempfile.NamedTemporaryFile(prefix="cc-err-", dir=TMP,
                                               delete=False)
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass
            for pipe in (getattr(c.proc, "stdin", None),
                         getattr(c.proc, "stdout", None)):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
        self.err.close()

    def env(self, **kw):
        e = dict(os.environ)
        e.update({"BRUE_CRED_API_KEY": self.fake.api_key,
                  "BRUE_CRED_IDENTIFIER": self.fake.identifier,
                  "BRUE_CRED_PASSWORD": self.fake.password,
                  "CAPITALCOM_API_URL": self.fake.api_url,
                  "CAPITALCOM_STREAM_URL": self.fake.stream_url,
                  "BRUE_BROKER_STATE": self.state, "PYTHONPATH": ROOT})
        e.pop("CAPITALCOM_ENV", None)
        e.pop("BRUE_ACCOUNT_ID", None)
        e.update(kw)
        return e

    def connect(self, argv=("--paper",), reconcile=True, **env):
        c = Connector([sys.executable, ADAPTER] + list(argv),
                      env=self.env(**env), stderr=self.err)
        self.conns.append(c)
        if reconcile:
            c.connect()
        else:
            c.hello()
        return c

    def posts(self, path="/api/v1/positions"):
        return [r for r in self.fake.requests() if r["method"] == "POST"
                and r["path"] == path]

    @staticmethod
    def seen(c, name, pred=lambda d: True, since=0):
        with c._event_cv:
            events = list(c.events)[since:]
        return [d for n, d in events if n == name and pred(d)]

    def place(self, c, cid, symbol="EURUSD", side="buy", qty=1000, **kw):
        p = {"client_id": cid, "symbol": symbol, "side": side, "qty": qty}
        p.update(kw)
        return c.request("order.place", p)

    def filled(self, c, cid, mark=0, timeout=10):
        return c.wait_event("order.filled", lambda d: d["client_id"] == cid,
                            timeout=timeout, since=mark)

    def quote(self, epic):
        return self.fake.market.quote(epic)


class TestOrders(StandIn):
    def test_a_market_order_fills_at_capitalcoms_price_and_closes(self):
        c = self.connect()
        mark = c.mark()
        r = self.place(c, "a1")
        self.assertEqual(r["status"], "accepted")
        acc = c.wait_event("order.accepted", lambda d: d["client_id"] == "a1",
                           since=mark)
        f = self.filled(c, "a1", mark)
        self.assertEqual(acc["order_id"], r["order_id"])
        self.assertEqual((f["order_id"], f["side"], f["qty"], f["fees"]),
                         (r["order_id"], "buy", 1000.0, 0.0))
        pos = c.request("positions.list")["positions"]
        p = [x for x in pos if x["position_id"] == f["position_id"]][0]
        # the price is Capital.com's own: the deal's level, not a quote
        self.assertEqual((p["avg_price"], p["client_id"], p["qty"],
                          p["side"]), (f["price"], "a1", 1000.0, "buy"))
        c.request("position.close", {"client_id": "x1",
                                     "position_id": f["position_id"]})
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == f["position_id"],
                          since=mark)
        self.assertEqual((cl["reason"], cl["qty"], cl["swap"],
                          cl["commission"], cl["client_id"]),
                         ("manual", 1000.0, 0.0, 0.0, "a1"))
        self.assertAlmostEqual(cl["realized_pnl"],
                               round((cl["price"] - f["price"]) * 1000, 2),
                               places=2)
        self.assertEqual(c.request("positions.list")["positions"], [])
        ids = {x["fill_id"] for x in c.request("history.fills")["fills"]}
        self.assertIn(f["fill_id"], ids)
        self.assertIn("x-" + cl["close_id"], ids)
        # a close asked again with its own client_id: the original outcome
        self.assertEqual(c.request("position.close", {
            "client_id": "x1", "position_id": f["position_id"]}), {})
        with self.assertRaises(ConnectorError) as cm:
            c.request("position.close", {"client_id": "x2",
                                         "position_id": f["position_id"]})
        self.assertEqual(cm.exception.code, "not_found")

    def test_stops_and_targets_are_capitalcoms_and_a_stop_fires(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        sl, tp = round(bid - 0.005, 5), round(ask + 0.005, 5)
        mark = c.mark()
        self.place(c, "b1", sl=sl, tp=tp)
        f = self.filled(c, "b1", mark)
        p = [x for x in c.request("positions.list")["positions"]
             if x["position_id"] == f["position_id"]][0]
        self.assertEqual((p["sl"], p["tp"]), (sl, tp))
        self.fake.set_price("EURUSD", bid=round(sl - 0.001, 5),
                            ask=round(sl - 0.0009, 5))
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == f["position_id"],
                          since=mark, timeout=15)
        self.assertEqual(cl["reason"], "sl")
        self.assertEqual(cl["price"], round(sl - 0.001, 5))

    def test_a_target_fires(self):
        c = self.connect()
        bid, ask = self.quote("GOLD")
        tp = round(ask + 5, 2)
        mark = c.mark()
        self.place(c, "t1", symbol="GOLD", qty=1, tp=tp)
        f = self.filled(c, "t1", mark)
        self.fake.set_price("GOLD", bid=round(tp + 1, 2), ask=round(tp + 1.3,
                                                                    2))
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == f["position_id"],
                          since=mark, timeout=15)
        self.assertEqual(cl["reason"], "tp")

    def test_modify_keeps_what_it_is_not_asked_to_change(self):
        # whether Capital.com keeps a leg left out of PUT /positions is not
        # documented: the stand-in plays both readings
        for reading in ("keep", "clear"):
            self.fake.set_option("position_update_omitted", reading)
            c = self.connect()
            bid, ask = self.quote("EURUSD")
            sl, tp = round(bid - 0.004, 5), round(ask + 0.004, 5)
            mark = c.mark()
            self.place(c, "m-" + reading, sl=sl, tp=tp)
            f = self.filled(c, "m-" + reading, mark)
            sl2 = round(sl + 0.001, 5)
            c.request("order.modify", {"client_id": "mm-" + reading,
                                       "position_id": f["position_id"],
                                       "sl": sl2})
            p = [x for x in c.request("positions.list")["positions"]
                 if x["position_id"] == f["position_id"]][0]
            self.assertEqual((p["sl"], p["tp"]), (sl2, tp), reading)
            c.request("order.modify", {"client_id": "mc-" + reading,
                                       "position_id": f["position_id"],
                                       "sl": None})
            p = [x for x in c.request("positions.list")["positions"]
                 if x["position_id"] == f["position_id"]][0]
            self.assertEqual((p["sl"], p["tp"]), (None, tp), reading)
            c.request("flatten_all", {"client_id": "fl-" + reading})
            c.close()

    def test_a_trailing_stop_is_capitalcoms_and_its_moves_are_told(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "tr1", trail_points=50)
        f = self.filled(c, "tr1", mark)
        p = [x for x in c.request("positions.list")["positions"]
             if x["position_id"] == f["position_id"]][0]
        self.assertEqual(p["trail_points"], 50)
        self.fake.set_price("EURUSD", trend_pct_per_s=0.02, seconds=20)
        t = c.wait_event("position.trailed",
                         lambda d: d["position_id"] == f["position_id"],
                         since=mark, timeout=15)
        self.assertEqual(t["trail_points"], 50)
        # documented: SILVER's trailingStopsPreference is NOT_AVAILABLE
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "tr2", symbol="SILVER", qty=1, trail_points=50)
        self.assertEqual((cm.exception.code, cm.exception.effective_code),
                         ("x_capitalcom_no_trailing", "trade_restricted"))

    def test_a_working_order_rests_moves_and_fills(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        level = round(ask - 0.002, 5)
        mark = c.mark()
        r = self.place(c, "w1", type="limit", price=level)
        self.assertEqual(r["status"], "resting")
        placed = c.wait_event("order.placed", lambda d: d["client_id"] == "w1",
                              since=mark)
        self.assertEqual((placed["order_id"], placed["price"],
                          placed["type"]), (r["order_id"], level, "limit"))
        rows = c.request("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(o["order_id"], o["client_id"], o["price"],
                           o["state"], o["remaining_qty"]) for o in rows],
                         [(r["order_id"], "w1", level, "resting", 1000.0)])
        level2 = round(level - 0.001, 5)
        c.request("order.amend", {"client_id": "w1a",
                                  "order_id": r["order_id"], "price": level2})
        am = c.wait_event("order.amended",
                          lambda d: d["order_id"] == r["order_id"],
                          since=mark)
        self.assertEqual(am["price"], level2)
        self.fake.set_price("EURUSD", bid=round(level2 - 0.0002, 5),
                            ask=round(level2 - 0.0001, 5))
        f = self.filled(c, "w1", mark, timeout=15)
        self.assertEqual(f["order_id"], r["order_id"])
        p = [x for x in c.request("positions.list")["positions"]
             if x["position_id"] == f["position_id"]][0]
        self.assertEqual(p["from_order"], "limit")
        self.assertEqual(c.request("orders.list",
                                   {"state": "resting"})["orders"], [])

    def test_a_working_order_is_cancelled_once(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        mark = c.mark()
        r = self.place(c, "c1", type="stop", price=round(ask + 0.003, 5))
        c.request("order.cancel", {"client_id": "c1x",
                                   "order_id": r["order_id"]})
        ev = c.wait_event("order.cancelled",
                          lambda d: d["order_id"] == r["order_id"],
                          since=mark)
        self.assertNotIn("code", ev)
        self.assertEqual(c.request("orders.list",
                                   {"state": "resting"})["orders"], [])
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.cancel", {"client_id": "c1y",
                                       "order_id": r["order_id"]})
        self.assertEqual(cm.exception.code, "not_resting")
        ended = c.request("orders.list", {"state": "cancelled"})["orders"]
        self.assertEqual([o["order_id"] for o in ended], [r["order_id"]])

    def test_a_good_till_date_order_expires(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        mark = c.mark()
        at = int(c.request("ping")["time"]) + 90_000
        r = self.place(c, "g1", type="limit", price=round(ask - 0.003, 5),
                       expiry="gtd", expiry_at=at)
        rows = c.request("orders.list", {"state": "resting"})["orders"]
        self.assertEqual(rows[0]["expiry"], "gtd")
        self.assertEqual(rows[0]["expires_at"], at // 1000 * 1000)
        self.fake.advance(120)
        ev = c.wait_event("order.expired",
                          lambda d: d["order_id"] == r["order_id"],
                          since=mark, timeout=20)
        self.assertEqual(ev["client_id"], "g1")

    def test_refusals_are_typed_and_end_the_order(self):
        c = self.connect()
        # held still, so a stop one tick under the bid stays one
        self.fake.set_price("EURUSD", bid=1.085, ask=1.08506)
        bid, ask = self.quote("EURUSD")
        cases = [
            ({"symbol": "NOPE"}, "unknown_symbol"),
            ({"qty": 100.5}, "invalid_qty"),
            ({"qty": 50}, "invalid_qty"),
            ({"qty": 6_000_000}, "invalid_qty"),
            ({"qty": 5_000_000}, "insufficient_margin"),
            ({"sl": round(bid + 0.001, 5)}, "invalid_stops"),
            # one tick under the bid: Capital.com's own minimum refuses it
            # (documented error.invalid.stoploss.minvalue)
            ({"sl": round(bid - 0.00001, 5)}, "invalid_stops"),
            ({"type": "stop_limit", "price": round(ask + 0.002, 5),
              "stop_price": round(ask + 0.001, 5)}, "unsupported"),
            ({"type": "limit", "price": round(ask - 0.002, 5),
              "expiry": "day"}, "invalid_expiry"),
            ({"type": "limit", "price": round(ask + 0.002, 5)},
             "x_capitalcom_invalid_level"),
        ]
        for k, (params, code) in enumerate(cases):
            cid = "r%d" % k
            mark = c.mark()
            with self.assertRaises(ConnectorError, msg=code) as cm:
                self.place(c, cid, **params)
            self.assertEqual(cm.exception.code, code, params)
            ev = c.wait_event("order.rejected",
                              lambda d: d["client_id"] == cid, since=mark,
                              timeout=5)
            self.assertEqual(ev["code"], code)
        self.fake.set_market("closed", "EURUSD")
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "rc")
        self.assertEqual(cm.exception.code, "market_closed")
        # only the stop one tick from the bid reached Capital.com (its own
        # minimum refused it); everything else was refused before sending
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(c.request("positions.list")["positions"], [])


class TestOnce(StandIn):
    def test_a_replay_answers_the_first_outcome_and_sends_nothing(self):
        c = self.connect()
        mark = c.mark()
        r1 = self.place(c, "o1")
        self.filled(c, "o1", mark)
        r2 = self.place(c, "o1")
        self.assertEqual(r1, r2)
        self.assertEqual(len(self.posts()), 1)
        time.sleep(1.0)
        self.assertEqual(len(self.seen(c, "order.filled",
                                       lambda d: d["client_id"] == "o1")), 1)
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "o1", side="sell")
        self.assertEqual(cm.exception.code, "duplicate_client_id")

    def test_a_lost_answer_is_found_and_never_sent_again(self):
        c = self.connect()
        self.fake.lose_next_answer("/positions", "POST")
        mark = c.mark()
        try:
            r = self.place(c, "l1")
        except ConnectorError as e:
            self.assertTrue(e.retryable, e)
            r = None
        end = time.time() + 15
        while r is None and time.time() < end:
            try:
                r = self.place(c, "l1")
            except ConnectorError as e:
                self.assertTrue(e.retryable, e)
                time.sleep(0.5)
        self.assertIsNotNone(r)
        f = self.filled(c, "l1", mark)
        self.assertEqual(r["order_id"], f["order_id"])
        self.assertEqual(len(self.posts()), 1)
        time.sleep(1.0)
        self.assertEqual(len(self.seen(c, "order.filled",
                                       lambda d: d["client_id"] == "l1")), 1)
        pos = c.request("positions.list")["positions"]
        self.assertEqual([p["client_id"] for p in pos], ["l1"])

    def test_a_crash_with_an_order_of_unknown_fate_does_not_send_it_again(self):
        c = self.connect()
        self.fake.lose_next_answer("/positions", "POST")
        self.fake.fail_next(500, count=500, path="/workingorders")
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "k1")
        self.assertTrue(cm.exception.retryable)
        c.proc.kill()               # a crash: no bye
        c.proc.wait()
        with self.fake._guard:
            self.fake._faults = []
        c2 = self.connect()
        mark = c2.mark()
        f = None
        try:
            f = self.filled(c2, "k1", 0, timeout=5)
        except TimeoutError:
            pass
        r = self.place(c2, "k1")
        f = f or self.filled(c2, "k1", mark)
        self.assertEqual(r["order_id"], f["order_id"])
        self.assertEqual(len(self.posts()), 1)
        pos = c2.request("positions.list")["positions"]
        self.assertEqual([p["client_id"] for p in pos], ["k1"])

    def test_a_restart_keeps_the_client_ids_of_positions(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "p1")
        f = self.filled(c, "p1", mark)
        c.proc.kill()
        c.proc.wait()
        c2 = self.connect()
        pos = c2.request("positions.list")["positions"]
        self.assertEqual([(p["position_id"], p["client_id"]) for p in pos],
                         [(f["position_id"], "p1")])
        # the platform's replay after the restart: the original outcome
        self.assertEqual(self.place(c2, "p1")["order_id"], f["order_id"])
        self.assertEqual(len(self.posts()), 1)


class TestWords(StandIn):
    """The order's and the close's words for an answer that was lost put
    the not-known words first and name Capital.com once, and a gateway's
    page that is not JSON does not go into the messages raw, CRLFs and all
    (flatten_all's failure list quotes them). A gateway in front of
    Capital.com answers here with its HTML page (the stand-in's
    fail_next(page=True))."""

    def test_an_order_whose_answer_was_lost_says_not_known_first(self):
        c = self.connect()
        self.fake.fail_next(502, count=1, path="/positions", method="POST",
                            page=True)
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "pg1")
        said = str(cm.exception)
        self.assertTrue(cm.exception.retryable)
        self.assertTrue(said.startswith(
            "internal: Not known yet whether Capital.com took the order: it "
            "answered HTTP 502 (Bad Gateway). It is NOT sent again"), said)
        self.assertEqual(said.count("Capital.com"), 1, said)
        self.assertNotRegex(said, r"[\r\n<>]")
        self.assertEqual(len(self.posts()), 1)

    def test_a_close_whose_answer_was_lost_says_not_known_first(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "pc1")
        fill = self.filled(c, "pc1", mark)
        self.fake.fail_next(502, count=1, path="/positions/", method="DELETE",
                            page=True)
        with self.assertRaises(ConnectorError) as cm:
            c.request("position.close", {"client_id": "pc1-x",
                                         "position_id": fill["position_id"]})
        said = str(cm.exception)
        self.assertTrue(cm.exception.retryable)
        self.assertTrue(said.startswith(
            "internal: Not known yet whether Capital.com closed "), said)
        self.assertIn(": it answered HTTP 502 (Bad Gateway). Asking again "
                      "with the same client_id is safe.", said)
        self.assertEqual(said.count("Capital.com"), 1, said)
        self.assertNotRegex(said, r"[\r\n<>]")

    def test_flatten_all_says_what_it_could_not_close_on_one_line(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "fl1")
        self.filled(c, "fl1", mark)
        self.fake.fail_next(502, count=20, path="/positions/",
                            method="DELETE", page=True)
        with self.assertRaises(ConnectorError) as cm:
            c.request("flatten_all", {"client_id": "fl-x"})
        said = str(cm.exception)
        self.assertEqual(cm.exception.code, "x_capitalcom_not_flat", said)
        self.assertIn("Bad Gateway", said)
        self.assertNotRegex(said, r"[\r\n<>]")


class TestSession(StandIn):
    def test_a_lapsed_session_logs_in_again_on_the_same_account(self):
        gbp = [a["accountId"] for a in self.fake.accounts
               if a["currency"] == "GBP"][0]
        c = self.connect(BRUE_ACCOUNT_ID=gbp)
        acct = c.request("account.get")
        self.assertEqual((acct["currency"], acct["account_id"]), ("GBP", gbp))
        mark = c.mark()
        self.place(c, "s1")
        self.filled(c, "s1", mark)
        self.fake.expire_sessions()
        self.place(c, "s2")
        self.filled(c, "s2", mark)
        deals = {a["accountId"]: a["deals"] for a in
                 self.fake.state()["accounts"]}
        self.assertEqual(deals[gbp], 2)
        self.assertEqual(sum(deals.values()), 2)
        logins = [r for r in self.fake.requests()
                  if r["path"] == "/api/v1/session"]
        self.assertGreaterEqual(len([r for r in logins
                                     if r["method"] == "POST"]), 2)
        self.assertGreaterEqual(len([r for r in logins
                                     if r["method"] == "PUT"]), 2)
        c.close()
        # the next hello names the account's currency, cold
        h = self.connect(reconcile=False, BRUE_ACCOUNT_ID=gbp).handshake
        self.assertEqual(h["account_currency"], "GBP")

    def test_accounts_are_listed_before_the_session(self):
        c = self.connect(reconcile=False)
        rows = c.request("accounts.list")["accounts"]
        self.assertEqual(sorted(r["currency"] for r in rows), ["GBP", "USD"])
        for r in rows:
            self.assertEqual((r["mode"], r["type"]), ("paper", "cfd"))
            self.assertNotIn("selected", r)
        c.request("account.get")
        rows = c.request("accounts.list")["accounts"]
        self.assertEqual([r["currency"] for r in rows if r["selected"]],
                         ["USD"])

    def test_an_account_this_login_does_not_have_is_refused(self):
        c = self.connect(reconcile=False, BRUE_ACCOUNT_ID="99999")
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        self.assertEqual((cm.exception.code, cm.exception.effective_code),
                         ("x_capitalcom_account", "not_found"))

    def test_a_wrong_password_is_unauthorized_and_said_once(self):
        c = self.connect(reconcile=False, BRUE_CRED_PASSWORD="wrong-one")
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        self.assertEqual(cm.exception.code, "unauthorized")
        self.assertIn("demo host", str(cm.exception))
        self.assertNotIn("wrong-one", str(cm.exception))
        logins = [r for r in self.fake.requests()
                  if r["path"] == "/api/v1/session" and r["method"] == "POST"]
        self.assertEqual(len(logins), 1)
        self.fake.set_key_revoked(True)
        c2 = self.connect(reconcile=False)
        with self.assertRaises(ConnectorError) as cm:
            c2.request("catalog.list")
        self.assertEqual(cm.exception.code, "unauthorized")

    def test_a_login_with_no_account_on_the_demo_host_is_said_so(self):
        """2026-11-15, a real login on the demo host: Capital.com answered
        POST /session 401 error.null.accountId and the platform was told the
        key was refused, with advice about lost and paused keys. Told now:
        no account for this login on the demo host, the likely cause and
        how to make a key that reaches the demo account, in the adapter's
        own words. One login is tried, as before."""
        self.fake.set_no_account(True)
        c = self.connect(reconcile=False)
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        e = cm.exception
        self.assertEqual((e.code, e.effective_code),
                         ("x_capitalcom_no_account", "unauthorized"))
        self.assertIn("found no account for this login on its demo host",
                      str(e))
        self.assertIn("Likely the API key was made while Capital.com's "
                      "platform was on a live account", str(e))
        self.assertIn("switched to Demo (Settings > API integrations)",
                      str(e))
        self.assertNotIn(" row", str(e))
        self.assertNotIn("refused the API key", str(e))
        for v in (self.fake.api_key, self.fake.password):
            self.assertNotIn(v, str(e))
        logins = [r for r in self.fake.requests()
                  if r["path"] == "/api/v1/session" and r["method"] == "POST"]
        self.assertEqual([r["status"] for r in logins], [401])

    def test_missing_values_are_named(self):
        c = self.connect(reconcile=False, BRUE_CRED_IDENTIFIER="")
        with self.assertRaises(ConnectorError) as cm:
            c.request("account.get")
        self.assertEqual(cm.exception.code, "unauthorized")
        self.assertIn("login email", str(cm.exception))
        self.assertEqual(self.fake.requests(), [])


# The adapter file unchanged, run as its main() runs it, except that its hello
# says "live" (on the stand-in's loopback hosts it says "paper"), so the
# platform's arming and by-hand rules apply as on Capital.com's live host.
# Every method name it is sent is written to `trace` first. It will not
# start unless both endpoints are this machine.
LIVE_MODE_WRAPPER = """
import importlib.util, ipaddress, sys, urllib.parse
sys.path.insert(0, {root!r})
spec = importlib.util.spec_from_file_location("capitalcom_adapter",
                                              {adapter!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["capitalcom_adapter"] = mod
spec.loader.exec_module(mod)
try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass
adapter = mod.CapitalComAdapter()
for key in ("api_url", "stream_url"):
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
adapter.run()
adapter._drain(mod.HANDLER_BUDGET + 1.5)
adapter._save_state()
"""


class TestExactNames(StandIn):
    """The Connector, the adapter with a live hello, unarmed, the stand-in
    behind it. The arming and by-hand rules know order.place by its exact
    name; a handler found by method.replace(".", "_") would let
    "order_place" reach the order handler on a live connection whose terms
    keep orders by hand. No spelling but the exact one reaches a handler."""

    def test_another_spelling_of_a_method_trades_nothing(self):
        trace = os.path.join(self.state, "received.txt")
        path = os.path.join(self.state, "live_mode.py")
        with open(path, "w") as fh:
            fh.write(LIVE_MODE_WRAPPER.format(root=ROOT, adapter=ADAPTER,
                                              trace=trace))
        c = Connector([sys.executable, path, "--live"], env=self.env(),
                      stderr=self.err)
        self.conns.append(c)
        c.connect()
        self.assertEqual((c.handshake.get("mode"), c.is_paper, c.armed,
                          c.terms["orders_by_hand_only"]),
                         ("live", False, False, True))
        self.fake.requests(clear=True)
        order = {"symbol": "EURUSD", "side": "buy", "qty": 1000}
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", dict(order, client_id="n-dotted"))
        self.assertEqual(cm.exception.code, "not_armed")
        for name in ("order_place", "flatten.all", "position_close"):
            with self.subTest(name=name):
                with self.assertRaises(ConnectorError) as cm:
                    c.request(name, dict(order, client_id="n-" + name))
                self.assertEqual(cm.exception.code, "unsupported")
        # Written past the Connector's rule, as a platform that is not this
        # SDK may: the adapter refuses them itself.
        for name in ("order_place", "flatten.all"):
            with self.subTest(wire=name):
                with self.assertRaises(ConnectorError) as cm:
                    c._send(name, dict(order, client_id="w-" + name), 15)
                self.assertEqual(cm.exception.code, "unsupported")
        time.sleep(0.6)
        self.assertEqual([(r["method"], r["path"]) for r in
                          self.fake.requests() if r["method"] != "GET"
                          and r["path"] != "/api/v1/session"], [])
        with open(trace) as fh:
            got = fh.read().split()
        self.assertEqual([m for m in got if m in ("order_place",
                                                  "flatten.all",
                                                  "position_close")],
                         ["order_place", "flatten.all"])
        # The control: armed with the user's word, the exact name is
        # carried out.
        c.arm("Allow trading, pressed by the user", by_hand=True)
        mark = c.mark()
        c.request("order.place", dict(order, client_id="n-armed"))
        self.filled(c, "n-armed", mark)
        self.assertEqual(len(self.posts()), 1)


class TestQuotes(StandIn):
    def test_forty_are_streamed_the_rest_are_said(self):
        c = self.connect()
        syms = [s for s, spec in c.catalog.items() if spec["tradable"]]
        self.assertGreater(len(syms), 40)
        mark = c.mark()
        c.request("quotes.subscribe", {"symbols": syms})
        st = c.wait_event("session.state", lambda d: d["state"] == "degraded"
                          and "40" in d["detail"], since=mark)
        self.assertIn(syms[-1], st["detail"])
        q = c.wait_event("quote", lambda d: d["symbol"] == syms[0],
                         since=mark)
        self.assertLess(q["bid"], q["ask"])
        time.sleep(1.5)
        got = {d["symbol"] for d in self.seen(c, "quote", since=0)}
        self.assertNotIn(syms[-1], got)
        self.assertLessEqual(len(got), 40)
        subs = [m for m in self.fake.stream_log()
                if m["destination"] == "marketData.subscribe"]
        self.assertTrue(subs and all(m["has_tokens"] for m in subs))
        self.assertLessEqual(max(len(m["payload"]["epics"]) for m in subs),
                             40)

    def test_a_dropped_stream_comes_back(self):
        c = self.connect()
        c.request("quotes.subscribe", {"symbols": ["BTCUSD", "EURUSD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "BTCUSD")
        time.sleep(1.0)
        self.fake.drop_streams()
        mark = c.mark()
        c.wait_event("quote", lambda d: d["symbol"] == "BTCUSD", since=mark,
                     timeout=15)
        end = time.time() + 15
        while time.time() < end:
            subs = [m for m in self.fake.stream_log()
                    if m["destination"] == "marketData.subscribe"]
            if len(subs) >= 2:
                break
            time.sleep(0.3)
        self.assertGreaterEqual(len(subs), 2)
        c.request("quotes.unsubscribe", {"symbols": ["BTCUSD", "EURUSD"]})


class TestBars(StandIn):
    def check(self, c, epic, tf, count):
        res, width = mod.TIMEFRAMES[tf]
        rows = []
        for _ in range(4):
            rows = c.request("bars.history", {"symbol": epic,
                                              "timeframe": tf,
                                              "count": count})["bars"]
            if len(rows) >= count:
                break
            time.sleep(1.0)
        self.assertEqual(len(rows), count)
        ts = [r[0] for r in rows]
        self.assertEqual(ts, sorted(set(ts)))
        now = self.fake.broker.now()
        self.assertLessEqual(ts[-1] + width, now)        # closed bars only
        for r in (rows[0], rows[len(rows) // 2], rows[-1]):
            (bo, bh, bl, bc), _, vol = self.fake.market.bar(epic, res, r[0],
                                                             now)
            self.assertEqual(r[1:5], [bo, bh, bl, bc])   # at the bid
            self.assertEqual(r[5], vol)
        return rows

    def test_a_long_chart_at_the_bid_whichever_end_capitalcom_cuts(self):
        # which 1,000 bars an over-full range returns is not documented;
        # the adapter's windows never ask for more, and split what comes
        # back full: the same bars whichever way Capital.com reads it
        got = {}
        for reading in ("oldest", "newest", "error"):
            self.fake.set_option("prices_overflow", reading)
            c = self.connect()
            rows = self.check(c, "BTCUSD", "1m", 5000)
            self.assertTrue(all(b[0] - a[0] == 60_000 for a, b in zip(
                rows, rows[1:])))           # 24/7: not one bar missing
            got[reading] = rows[0][0] // 3_600_000
            c.close()
        self.assertEqual(len(set(got.values())), 1)

    def test_other_timeframes_and_a_market_with_gaps(self):
        c = self.connect()
        self.check(c, "BTCUSD", "4h", 3000)
        self.check(c, "EURUSD", "1h", 3000)
        rows = self.check(c, "EURUSD", "1d", 2000)
        self.assertTrue(all(r[0] % 86_400_000 == 0 for r in rows))

    def test_a_short_history_is_all_there_is(self):
        self.fake.set_history("HOUR", days=20)
        self.fake.set_option("prices_empty", "empty")
        c = self.connect()
        rows = []
        for _ in range(6):
            rows = c.request("bars.history", {"symbol": "BTCUSD",
                                              "timeframe": "1h",
                                              "count": 5000})["bars"]
            time.sleep(1.0)
        self.assertGreater(len(rows), 400)
        self.assertLessEqual(len(rows), 20 * 24 + 1)

    def test_a_subscription_sends_closed_bars(self):
        c = self.connect()
        mark = c.mark()
        c.request("bars.subscribe", {"symbol": "BTCUSD", "timeframe": "1m"})
        ev = c.wait_event("bar", lambda d: d["symbol"] == "BTCUSD",
                          since=mark, timeout=75)
        self.assertEqual(ev["timeframe"], "1m")
        self.assertLessEqual(ev["bar"][0] + 60_000, self.fake.broker.now())


class TestFlatten(StandIn):
    def test_flatten_closes_cancels_and_is_repeat_safe(self):
        c = self.connect()
        mark = c.mark()
        for k in range(3):
            self.place(c, "f%d" % k)
            self.filled(c, "f%d" % k, mark)
        bid, ask = self.quote("EURUSD")
        w = self.place(c, "fw", type="limit", price=round(ask - 0.003, 5))
        out = c.request("flatten_all", {"client_id": "FL1"})
        self.assertEqual(out, {"closed": 3})
        ev = c.wait_event("order.cancelled",
                          lambda d: d["order_id"] == w["order_id"],
                          since=mark)
        # the platform asked for it (flatten_all): no code (SPEC 5)
        self.assertNotIn("code", ev)
        self.assertEqual((ev["client_id"], ev["message"]),
                         ("fw", "cancelled by flatten_all"))
        self.assertEqual(c.request("positions.list")["positions"], [])
        self.assertEqual(c.request("orders.list",
                                   {"state": "resting"})["orders"], [])
        self.assertEqual(len(self.seen(c, "position.closed", since=mark)), 3)
        self.assertEqual(c.request("flatten_all", {"client_id": "FL2"}),
                         {"closed": 0})
        self.place(c, "f9")
        self.filled(c, "f9", mark)
        self.assertEqual(c.request("flatten_all", {"client_id": "FL1"}),
                         {"closed": 3})                     # a replay
        self.assertEqual(len(c.request("positions.list")["positions"]), 1)

    def test_what_cannot_be_closed_is_named(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "z1")
        f = self.filled(c, "z1", mark)
        self.fake.set_market("closed", "EURUSD")
        with self.assertRaises(ConnectorError) as cm:
            c.request("flatten_all", {"client_id": "Z1"})
        self.assertEqual((cm.exception.code, cm.exception.effective_code),
                         ("x_capitalcom_not_flat", "market_closed"))
        self.assertIn(f["position_id"], str(cm.exception))
        self.fake.set_market("open", "EURUSD")
        self.assertEqual(c.request("flatten_all", {"client_id": "Z2"}),
                         {"closed": 1})


class TestFills(StandIn):
    def test_a_fill_in_two_parts(self):
        self.fake.fill_rule(0.2, [1, 1], "ETHUSD")
        c = self.connect()
        mark = c.mark()
        self.place(c, "pp", symbol="ETHUSD", qty=0.2)
        part = c.wait_event("order.partial", lambda d: d["client_id"] == "pp",
                            since=mark)
        full = self.filled(c, "pp", mark)
        self.assertEqual((part["qty"], part["remaining"], full["qty"]),
                         (0.1, 0.1, 0.1))
        self.assertNotEqual(part["fill_id"], full["fill_id"])
        self.assertEqual(len(c.request("positions.list")["positions"]), 2)

    def test_a_fill_whose_rest_was_not_dealt_ends_cancelled(self):
        self.fake.fill_next([1, 1], rest="cancel")
        c = self.connect()
        mark = c.mark()
        self.place(c, "pc", symbol="ETHUSD", qty=0.4)
        part = c.wait_event("order.partial", lambda d: d["client_id"] == "pc",
                            since=mark)
        end = c.wait_event("order.cancelled",
                           lambda d: d["client_id"] == "pc", since=mark)
        self.assertEqual((part["qty"], end["code"]), (0.2,
                                                      "x_capitalcom_unfilled"))
        self.assertEqual(self.seen(c, "order.filled",
                                   lambda d: d["client_id"] == "pc"), [])

    def test_standard_mode_offsets_and_says_so(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "so1")
        f = self.filled(c, "so1", mark)
        self.place(c, "so2", side="sell", qty=400)
        f2 = self.filled(c, "so2", mark)
        self.assertEqual((f2["position_id"], f2["side"], f2["qty"]),
                         (f["position_id"], "sell", 400.0))
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == f["position_id"],
                          since=mark)
        self.assertEqual((cl["qty"], cl["reason"]), (400.0, "manual"))
        pos = c.request("positions.list")["positions"]
        self.assertEqual([(p["position_id"], p["qty"]) for p in pos],
                         [(f["position_id"], 600.0)])

    def test_capitalcoms_own_closes_are_told_with_their_reason(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "d1")
        f = self.filled(c, "d1", mark)
        self.fake.close_as(f["position_id"], "DEALER")
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == f["position_id"],
                          since=mark, timeout=15)
        self.assertEqual(cl["reason"], "unknown")
        self.assertIn("DEALER", cl["reason_detail"])
        self.place(c, "d2")
        f = self.filled(c, "d2", mark)
        self.fake.close_as(f["position_id"], "CLOSE_OUT")
        cl = c.wait_event("position.closed",
                          lambda d: d["position_id"] == f["position_id"],
                          since=mark, timeout=15)
        self.assertEqual(cl["reason"], "stop_out")

    def test_a_deal_made_elsewhere_is_announced(self):
        c = self.connect()
        time.sleep(1.2)          # one login a second per key (documented)
        conn = http.client.HTTPConnection("127.0.0.1", self.fake.port,
                                          timeout=10)
        conn.request("POST", "/api/v1/session", body=json.dumps({
            "identifier": self.fake.identifier,
            "password": self.fake.password}),
            headers={"X-CAP-API-KEY": self.fake.api_key,
                     "Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        auth = {"CST": r.getheader("CST"),
                "X-SECURITY-TOKEN": r.getheader("X-SECURITY-TOKEN"),
                "Content-Type": "application/json"}
        mark = c.mark()
        conn.request("POST", "/api/v1/positions", body=json.dumps({
            "epic": "GOLD", "direction": "SELL", "size": 1}), headers=auth)
        conn.getresponse().read()
        conn.close()
        f = c.wait_event("order.filled", lambda d: d["symbol"] == "GOLD",
                         since=mark, timeout=15)
        self.assertIsNone(f["client_id"])
        self.assertEqual(f["side"], "sell")


class TestPlatformHistory(StandIn):
    """From this adapter's own ledger alone, a deal opened and closed on
    Capital.com's own platform (which this adapter never saw as an open
    position) never reaches the dock's History. fills.list reads the
    activity history too (GET /history/activity, detailed), merged by
    dealId."""

    def acct(self):
        return next(a for a in self.fake.broker.accounts.values()
                    if a.preferred)

    def test_a_deal_made_and_closed_on_the_platform_is_in_the_history(self):
        now = self.fake.broker.now()
        did = self.fake.broker.made_on_platform(
            self.acct(), "EURUSD", "BUY", 2000, 1.10000, now - 3_600_000,
            close_level=1.10500, source="USER")
        c = self.connect()
        mine = [f for f in c.request("history.fills")["fills"]
                if f["position_id"] == did]
        # the open (buy) and the close (sell) both appear, at their levels
        self.assertEqual(
            sorted((f["side"], f["qty"], f["price"]) for f in mine),
            [("buy", 2000.0, 1.1), ("sell", 2000.0, 1.105)])
        for f in mine:
            self.assertEqual((f["symbol"], f["client_id"], f["order_id"],
                              f["fees"]), ("EURUSD", None, None, 0.0))
        # read again: the same two fills, each once (stable, deduped ids)
        again = sorted(f["fill_id"] for f in
                       c.request("history.fills")["fills"]
                       if f["position_id"] == did)
        self.assertEqual(again, sorted(f["fill_id"] for f in mine))
        self.assertEqual(len(set(again)), 2)

    def test_a_deal_this_adapter_made_is_not_doubled_by_the_history(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "o1", qty=1000)
        f = self.filled(c, "o1", mark)
        did = f["position_id"]
        mine = [x for x in c.request("history.fills")["fills"]
                if x["position_id"] == did]
        # one fill, from the ledger (its client_id kept), not doubled by the
        # same deal's activity row under the same dealId
        self.assertEqual(len(mine), 1, mine)
        self.assertEqual((mine[0]["client_id"], mine[0]["side"]),
                         ("o1", "buy"))
        self.assertTrue(mine[0]["fill_id"].startswith("f-"))

    def test_the_history_is_bounded_and_a_read_failure_leaves_the_ledger(self):
        now = self.fake.broker.now()
        did = self.fake.broker.made_on_platform(
            self.acct(), "EURUSD", "BUY", 1000, 1.10000, now - 3_600_000,
            close_level=1.10500)
        c = self.connect()
        mark = c.mark()
        self.place(c, "o9", qty=500)           # one in the ledger
        self.filled(c, "o9", mark)
        # the platform deal and the ledger deal both show
        all_fills = c.request("history.fills")["fills"]
        self.assertTrue(any(f["position_id"] == did for f in all_fills))
        self.assertTrue(any(f["client_id"] == "o9" for f in all_fills))
        # a `from` window after the platform deal leaves it out
        recent = c.request("history.fills", {"from": now - 60_000})["fills"]
        self.assertFalse(any(f["position_id"] == did for f in recent), recent)
        # the activity read failing leaves the ledger's own fills to answer
        self.fake.fail_next(502, count=40, path="/history/activity")
        led = c.request("history.fills")["fills"]
        self.assertTrue(any(f["client_id"] == "o9" for f in led), led)
        self.assertFalse(any(f["position_id"] == did for f in led), led)


class TestEdges(StandIn):
    def test_an_account_that_is_not_a_cfd_account_is_refused(self):
        gbp = [a["accountId"] for a in self.fake.accounts
               if a["currency"] == "GBP"][0]
        self.fake.broker.accounts[gbp].account_type = "SPREADBET"
        try:
            c = self.connect(reconcile=False, BRUE_ACCOUNT_ID=gbp)
            rows = c.request("accounts.list")["accounts"]
            self.assertIn("spreadbet", [r["type"] for r in rows])
            with self.assertRaises(ConnectorError) as cm:
                c.request("account.get")
            self.assertEqual((cm.exception.code, cm.exception.effective_code),
                             ("x_capitalcom_account_type", "unsupported"))
        finally:
            self.fake.broker.accounts[gbp].account_type = "CFD"

    def test_flatten_while_an_order_awaits_its_confirmation(self):
        c = self.connect()
        self.fake.set_confirm_delay(6.0)
        mark = c.mark()
        r = self.place(c, "cf1")
        self.assertEqual(r["status"], "accepted")     # Capital.com has it
        out = c.request("flatten_all", {"client_id": "CF"})
        self.assertEqual(out, {"closed": 1})
        # the close's own confirmation is slow too: its event follows
        # (from the activity history), and never before the fill
        c.wait_event("position.closed", since=mark, timeout=10)
        names = [(n, d.get("position_id")) for n, d in list(c.events)[mark:]
                 if n in ("order.filled", "position.closed")]
        self.assertEqual([n for n, _ in names], ["order.filled",
                                                 "position.closed"])
        self.fake.set_confirm_delay(0.1)
        time.sleep(8)            # the confirmation arrives: nothing twice
        self.assertEqual(len(self.seen(c, "order.filled",
                                       lambda d: d["client_id"] == "cf1")), 1)
        self.assertEqual(len(self.seen(c, "position.closed", since=mark)), 1)
        self.assertEqual(c.request("positions.list")["positions"], [])

    def test_a_gateways_error_is_an_order_of_unknown_fate_never_resent(self):
        c = self.connect()
        self.fake.fail_next(502, count=1, path="/positions")
        for _ in range(2):
            with self.assertRaises(ConnectorError) as cm:
                self.place(c, "u1")
            self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.seen(c, "order.rejected",
                                   lambda d: d["client_id"] == "u1"), [])
        st = c.wait_event("session.state", lambda d: d["state"] == "degraded"
                          and "u1" in d["detail"])
        self.assertIn("not sent again", st["detail"])

    def test_capitalcoms_429_on_an_order_is_sent_again_once(self):
        c = self.connect()
        self.fake.fail_next(429, count=1, path="/positions")
        mark = c.mark()
        r = self.place(c, "rl1")
        self.filled(c, "rl1", mark)
        posts = self.posts()
        self.assertEqual([p["status"] for p in posts], [429, 200])
        self.assertEqual(len(c.request("positions.list")["positions"]), 1)
        self.assertTrue(r["order_id"].startswith("o_"))

    def test_a_lapsed_session_ends_the_stream_and_prices_come_back(self):
        c = self.connect()
        c.request("quotes.subscribe", {"symbols": ["BTCUSD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "BTCUSD")
        self.fake.expire_sessions()
        time.sleep(1.0)
        mark = c.mark()
        c.wait_event("quote", lambda d: d["symbol"] == "BTCUSD", since=mark,
                     timeout=20)
        logins = [r for r in self.fake.requests()
                  if r["path"] == "/api/v1/session" and r["method"] == "POST"]
        self.assertGreaterEqual(len(logins), 2)

    def test_a_working_order_that_cannot_be_afforded_when_it_triggers(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        level = round(ask - 0.002, 5)
        mark = c.mark()
        r = self.place(c, "wm", qty=5_000_000, type="limit", price=level)
        self.assertEqual(r["status"], "resting")
        self.fake.set_price("EURUSD", bid=round(level - 0.0002, 5),
                            ask=round(level - 0.0001, 5))
        ev = c.wait_event("order.cancelled",
                          lambda d: d["order_id"] == r["order_id"],
                          since=mark, timeout=20)
        self.assertEqual(ev["code"], "x_capitalcom_rejected")
        self.assertEqual(self.seen(c, "order.filled",
                                   lambda d: d["client_id"] == "wm"), [])


class TestMoneyAndSecrets(StandIn):
    def test_money_that_moves_without_a_trade(self):
        e = self.env()
        a = mod.CapitalComAdapter(argv=["--paper"], environ=e,
                                  out=io.StringIO())
        try:
            a.on_first_call()
            a._opened = True
            a._poll_operations()
            self.fake.adjust_balance(50, note="goodwill")
            a._poll_operations()
            events = [json.loads(x) for x in a._out.getvalue().splitlines()]
            ops = [m["data"] for m in events if m.get("event")
                   == "balance.operation"]
            self.assertEqual([(o["kind"], o["amount"]) for o in ops],
                             [("adjustment", 50.0)])
            out = a.handle_history_operations({})
            self.assertEqual([o["operation_id"] for o in out["operations"]],
                             [ops[0]["operation_id"]])
        finally:
            a.on_shutdown()

    def test_no_credential_reaches_a_message_a_log_or_a_file(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "q1", sl=round(self.quote("EURUSD")[0] - 0.004, 5))
        f = self.filled(c, "q1", mark)
        c.request("quotes.subscribe", {"symbols": ["EURUSD"]})
        c.request("bars.history", {"symbol": "EURUSD", "timeframe": "1h",
                                   "count": 50})
        c.request("accounts.list")
        c.request("position.close", {"client_id": "q2",
                                     "position_id": f["position_id"]})
        with self.assertRaises(ConnectorError):
            self.place(c, "q3", symbol="NOPE")
        c.request("flatten_all", {"client_id": "q4"})
        c.close()
        bad = self.connect(reconcile=False,
                           BRUE_CRED_PASSWORD="not the password")
        with self.assertRaises(ConnectorError):
            bad.request("account.get")
        bad.close()
        secrets = [self.fake.api_key, self.fake.identifier,
                   self.fake.password, "not the password"]
        texts = [json.dumps(list(c.events)), json.dumps(list(bad.events))]
        self.err.flush()
        with open(self.err.name, encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
        for name in os.listdir(self.state):
            with open(os.path.join(self.state, name), encoding="utf-8",
                      errors="replace") as fh:
                texts.append(fh.read())
        for t in texts:
            for s in secrets:
                self.assertNotIn(s, t)


if __name__ == "__main__":
    unittest.main()
