"""Capital.com adapter: faults at Capital.com and between two reads, each
pinned.

Every test asserts what the adapter MUST do. The stand-in plays
Capital.com's demo host (tests/fake_capitalcom.py); where a test needs
Capital.com to answer in a way the stand-in does not choose by itself, the
test bends the in-process stand-in for that one moment and puts it back.
The offline tests touch no network at all (a connection attempt fails the
test).
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from brueconnect import Connector, ConnectorError, protocol
from brueconnect.adapter import AdapterError
from brueconnect.adapters.capitalcom import capitalcom_adapter as mod
from tests.fake_capitalcom import CapError, FakeCapitalCom

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(ROOT, "brueconnect", "adapters", "capitalcom",
                       "capitalcom_adapter.py")
TMP = os.environ.get("TMPDIR") or None
LIVE = "https://api-capital.backend-capital.com/api/v1"
CREDS = {"BRUE_CRED_API_KEY": "KEYabcdef0123456789",
         "BRUE_CRED_IDENTIFIER": "someone@example.com",
         "BRUE_CRED_PASSWORD": "pass word"}


def no_network():
    """Patches that fail the test on any connection attempt."""
    boom = AssertionError("this test must not reach any network")
    return (mock.patch("urllib.request.urlopen", side_effect=boom),
            mock.patch("socket.create_connection", side_effect=boom))


class StandIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCapitalCom(market="open", balances=(
            ("USD", "10000"), ("GBP", "5000"))).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.reset()
        self.fake.requests(clear=True)
        self.state = tempfile.mkdtemp(prefix="cc-rev-", dir=TMP)
        self.err = tempfile.NamedTemporaryFile(prefix="cc-rev-err-", dir=TMP,
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

    def at_broker(self):
        with self.fake.broker.lock:
            return sorted(d for a in self.fake.broker.accounts.values()
                          for d in a.deals)

    def terminal(self, c, cid, since=0):
        """Every terminal event of one order (SPEC 5: exactly one)."""
        return [(n, d) for n, d in list(c.events)[since:]
                if n in ("order.filled", "order.cancelled", "order.expired",
                         "order.rejected") and d.get("client_id") == cid]


# -- a close Capital.com refuses is never told as a close --------------------

class TestRefusedClose(StandIn):
    def test_a_close_refused_for_a_shut_market_stays_open(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "r11")
        did = self.filled(c, "r11", mark)["position_id"]
        time.sleep(2.0)
        self.fake.set_market("closed", "EURUSD")
        try:
            with self.assertRaises(ConnectorError) as cm:
                c.request("position.close", {"client_id": "r11x",
                                             "position_id": did})
            self.assertEqual(cm.exception.code, "market_closed")
            time.sleep(12.0)
            closed = self.seen(c, "position.closed",
                               lambda d: d["position_id"] == did, since=mark)
            still = [p["position_id"] for p in
                     c.request("positions.list")["positions"]]
        finally:
            self.fake.set_market("open", "EURUSD")
        self.assertEqual(closed, [])
        self.assertEqual((still, self.at_broker()), ([did], [did]))

    def rejected_late(self, with_row: bool):
        # Capital.com takes the close (a dealReference) and its confirmation,
        # 5 s later, says REJECTED
        c = self.connect()
        mark = c.mark()
        self.place(c, "r10")
        did = self.filled(c, "r10", mark)["position_id"]
        time.sleep(2.0)
        broker = self.fake.broker
        orig = broker._activity

        def no_rejected(acct, kind, source, status, *a, **kw):
            if status == "REJECTED":
                return None
            return orig(acct, kind, source, status, *a, **kw)
        if not with_row:
            broker._activity = no_rejected
        try:
            self.fake.set_confirm_delay(5.0)
            self.fake.refuse_next("MARKET_CLOSED", path="/positions/" + did,
                                  method="DELETE", at="confirm")
            try:
                c.request("position.close", {"client_id": "r10x",
                                             "position_id": did})
            except ConnectorError:
                pass
            # past the 7 reads of the activity history after which an
            # unexplained disappearance was told (GONE_RETRIES)
            time.sleep(18.0)
        finally:
            self.fake.set_confirm_delay(0.1)
            if not with_row:
                del broker._activity
        closed = self.seen(c, "position.closed",
                           lambda d: d["position_id"] == did, since=mark)
        still = [p["position_id"] for p in
                 c.request("positions.list")["positions"]]
        self.assertEqual(closed, [])
        self.assertEqual((still, self.at_broker()), ([did], [did]))

    def test_a_close_rejected_late_stays_open(self):
        self.rejected_late(with_row=True)       # the stand-in's REJECTED row

    def test_a_close_rejected_late_with_no_record_stays_open(self):
        self.rejected_late(with_row=False)      # no activity row at all

    def test_a_close_that_happens_is_still_told_once(self):
        # the other side of the same change: a slow ACCEPTED confirmation
        c = self.connect()
        mark = c.mark()
        self.place(c, "r10ok")
        f = self.filled(c, "r10ok", mark)
        time.sleep(2.0)
        self.fake.set_confirm_delay(5.0)
        try:
            self.assertEqual(c.request("position.close", {
                "client_id": "r10okx", "position_id": f["position_id"]}), {})
            cl = c.wait_event("position.closed", lambda d: d["position_id"]
                              == f["position_id"], since=mark, timeout=15)
        finally:
            self.fake.set_confirm_delay(0.1)
        time.sleep(3.0)
        self.assertEqual(cl["reason"], "manual")
        self.assertIsNotNone(cl["price"])
        self.assertNotIn("reason_detail", cl)
        self.assertEqual(len(self.seen(c, "position.closed", since=mark)), 1)


# -- a lapse between accounts.list and the session does not deadlock ---------

class TestSwitchAfterLapse(StandIn):
    def test_the_first_call_after_accounts_list_and_a_lapse_is_answered(self):
        gbp = [a["accountId"] for a in self.fake.accounts
               if a["currency"] == "GBP"][0]
        c = self.connect(reconcile=False, BRUE_ACCOUNT_ID=gbp)
        c.request("accounts.list")
        self.assertEqual(self.fake.expire_sessions(), 1)
        acct = c.request("account.get", timeout=12)
        self.assertEqual((acct["account_id"], acct["currency"]),
                         (gbp, "GBP"))
        self.assertEqual(c.request("flatten_all", {"client_id": "r1fa"},
                                   timeout=12), {"closed": 0})
        mark = c.mark()
        self.place(c, "r1o")
        self.filled(c, "r1o", mark)
        deals = {a["accountId"]: a["deals"] for a in
                 self.fake.state()["accounts"]}
        self.assertEqual(deals[gbp], 1)
        self.assertEqual(sum(deals.values()), 1)
        self.assertEqual(len(self.posts("/api/v1/session")), 2)


# -- a stop that fires just before an opposite order keeps its reason --------

class TestStopRace(StandIn):
    def test_a_stop_just_before_an_opposite_order_is_the_stop(self):
        # The stop fires and the user's buy goes a moment later, before the
        # adapter's next read: the buy's handler meets the stale short. (The
        # race is usually hit on the first try; both tries must come out
        # right whether it is hit or not.)
        c = self.connect()
        hit = 0
        for attempt in range(4):
            if attempt >= 2 and hit:
                break
            bid, ask = self.quote("EURUSD")
            sl = round(ask + 0.002, 5)
            mark = c.mark()
            scid, bcid = "r2s%d" % attempt, "r2b%d" % attempt
            self.place(c, scid, side="sell", sl=sl)
            deal = self.filled(c, scid, mark)["position_id"]
            time.sleep(2.0)
            mark2 = c.mark()
            self.fake.set_price("EURUSD", bid=round(sl + 0.0004, 5),
                                ask=round(sl + 0.0005, 5))
            self.fake.tick()
            self.place(c, bcid, side="buy")
            time.sleep(5.0)
            closes = self.seen(c, "position.closed",
                               lambda d: d["position_id"] == deal,
                               since=mark2)
            fills = self.seen(c, "order.filled",
                              lambda d: d["client_id"] == bcid, since=mark2)
            self.assertEqual([x["reason"] for x in closes], ["sl"], attempt)
            self.assertEqual(len(fills), 1, attempt)
            self.assertEqual(sum(x["qty"] for x in fills), 1000.0)
            self.assertEqual(len(self.terminal(c, bcid, mark2)), 1)
            pos = c.request("positions.list")["positions"]
            self.assertEqual([(p["side"], p["qty"], p["client_id"])
                              for p in pos], [("buy", 1000.0, bcid)])
            # The race was hit when the short's close is told after the
            # buy's fill: the buy's handler met the short still in the book
            # (else the poller had seen the stop first).
            order = [n for n, d in list(c.events)[mark2:]
                     if (n == "position.closed" and d["position_id"] == deal)
                     or (n == "order.filled" and d["client_id"] == bcid)]
            hit += order == ["order.filled", "position.closed"]
            c.request("flatten_all", {"client_id": "r2fa%d" % attempt})
            self.fake.set_price("EURUSD", walk=True)
            time.sleep(1.0)
        self.assertGreater(hit, 0, "the race was never hit: nothing proved")


# -- a working order filled and stopped between two reads -------------------

class TestWorkingOrderRoundTrip(StandIn):
    def test_its_fill_names_the_deal_and_the_deal_is_told_closed(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        level = round(ask - 0.003, 5)
        sl = round(level - 0.002, 5)
        mark = c.mark()
        self.place(c, "r3w", type="limit", price=level, sl=sl)
        c.wait_event("order.placed", lambda d: d["client_id"] == "r3w",
                     since=mark)
        time.sleep(2.0)
        # through the limit (a deal opens), then through its stop (it closes),
        # both between two reads of the book
        self.fake.set_price("EURUSD", bid=round(level - 0.0002, 5),
                            ask=round(level - 0.0001, 5))
        self.fake.tick()
        self.fake.set_price("EURUSD", bid=round(sl - 0.001, 5),
                            ask=round(sl - 0.0009, 5))
        self.fake.tick()
        f = self.filled(c, "r3w", mark, timeout=20)
        cl = c.wait_event("position.closed", lambda d: True, since=mark,
                          timeout=10)
        time.sleep(3.0)
        self.assertIsNotNone(f["position_id"])
        self.assertEqual(f["fill_id"], "f-" + f["position_id"])
        self.assertEqual(cl["position_id"], f["position_id"])
        self.assertEqual((cl["reason"], cl["client_id"], cl["qty"]),
                         ("sl", "r3w", 1000.0))
        self.assertIsNotNone(cl["price"])
        self.assertEqual(len(self.terminal(c, "r3w", mark)), 1)
        ledger = [(x["fill_id"], x["side"]) for x in
                  c.request("history.fills")["fills"]]
        self.assertEqual(ledger, [(f["fill_id"], "buy"),
                                  ("x-" + cl["close_id"], "sell")])
        self.assertEqual(c.request("positions.list")["positions"], [])


# -- an answer without its list is not an empty book -------------------------

class TestAnswerWithoutTheList(StandIn):
    def test_one_answer_without_a_list_ends_nothing(self):
        c = self.connect()
        mark = c.mark()
        self.place(c, "r4m")
        f = self.filled(c, "r4m", mark)
        bid, ask = self.quote("EURUSD")
        self.place(c, "r4w", type="limit", price=round(ask - 0.01, 5))
        c.wait_event("order.placed", lambda d: d["client_id"] == "r4w",
                     since=mark)
        time.sleep(2.0)
        fake = self.fake
        left = {"p": 1, "o": 1}

        def once(key, orig):
            def answer(*a):
                if left[key]:
                    left[key] -= 1
                    return 200, {}, {}
                return orig(*a)
            return answer
        fake._r_positions = once("p", type(fake)._r_positions.__get__(fake))
        fake._r_orders = once("o", type(fake)._r_orders.__get__(fake))
        try:
            # long enough for the 7 reads of the activity history after which
            # an unexplained disappearance was told (GONE_RETRIES)
            time.sleep(18.0)
        finally:
            del fake._r_positions
            del fake._r_orders
        self.assertEqual(left, {"p": 0, "o": 0})
        self.assertEqual(self.seen(c, "position.closed", since=mark), [])
        self.assertEqual(self.seen(c, "order.cancelled", since=mark), [])
        self.assertEqual([p["position_id"] for p in
                          c.request("positions.list")["positions"]],
                         [f["position_id"]])
        self.assertEqual([(o["client_id"], o["state"]) for o in
                          c.request("orders.list")["orders"]],
                         [("r4w", "resting")])

    def test_two_empty_answers_in_a_row_end_nothing_either(self):
        # Two empty answers in a row are read as an empty book (how
        # Capital.com answers one is not documented): the next read lists
        # everything again, and what was put aside is withdrawn, untold.
        c = self.connect()
        mark = c.mark()
        self.place(c, "r4n")
        f = self.filled(c, "r4n", mark)
        bid, ask = self.quote("EURUSD")
        self.place(c, "r4v", type="limit", price=round(ask - 0.01, 5))
        c.wait_event("order.placed", lambda d: d["client_id"] == "r4v",
                     since=mark)
        time.sleep(2.0)
        fake = self.fake
        left = {"p": 2, "o": 2}

        def twice(key, orig):
            def answer(*a):
                if left[key]:
                    left[key] -= 1
                    return 200, {}, {}
                return orig(*a)
            return answer
        fake._r_positions = twice("p", type(fake)._r_positions.__get__(fake))
        fake._r_orders = twice("o", type(fake)._r_orders.__get__(fake))
        try:
            time.sleep(18.0)
        finally:
            del fake._r_positions
            del fake._r_orders
        self.assertEqual(left, {"p": 0, "o": 0})
        self.assertEqual(self.seen(c, "position.closed", since=mark), [])
        self.assertEqual(self.seen(c, "order.cancelled", since=mark), [])
        self.assertEqual([p["position_id"] for p in
                          c.request("positions.list")["positions"]],
                         [f["position_id"]])
        self.assertEqual([(o["client_id"], o["state"]) for o in
                          c.request("orders.list")["orders"]],
                         [("r4v", "resting")])


class TestBookRows(unittest.TestCase):
    """What a read of the book makes of each answer, with no network."""

    def rows(self, answers):
        ad = mod.CapitalComAdapter(argv=["--paper"], environ={
            "BRUE_BROKER_STATE": tempfile.mkdtemp(prefix="cc-rev-", dir=TMP),
            "CAPITALCOM_API_URL": "http://127.0.0.1:9/api/v1"},
            out=io.StringIO())
        seq = list(answers)
        calls = []

        def call(method, path, *a, **kw):
            calls.append((method, path))
            return seq.pop(0)
        ad.client.call = call
        try:
            return ad._book_rows("/positions", "positions", "read"), calls
        except mod.CapitalError as e:
            return e, calls

    def test_each_answer(self):
        one = [{"position": {"dealId": "D1"}}]
        out, calls = self.rows([{"positions": one}])
        self.assertEqual((out, len(calls)), (one, 1))
        # once without the list: read again, and the second answer counts
        out, calls = self.rows([{}, {"positions": one}])
        self.assertEqual((out, len(calls)), (one, 2))
        # empty twice: how Capital.com says the book is empty
        out, calls = self.rows([{}, {}])
        self.assertEqual((out, len(calls)), ([], 2))
        out, calls = self.rows([None, None])
        self.assertEqual(out, [])
        # anything else without the list: a failed read, never a book
        for bad in ([{"text": "<html>"}, {"text": "<html>"}],
                    [{}, {"errorCode": "x"}], [{"positions": None}, {}]):
            out, calls = self.rows(bad)
            self.assertIsInstance(out, mod.CapitalError, bad)
            self.assertEqual(len(calls), 2, bad)


# -- amending a working order's price keeps its stop and target --------------

class TestAmendKeepsLegs(StandIn):
    def test_amending_only_the_price_keeps_the_stop_and_target(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        level = round(ask - 0.003, 5)
        sl, tp = round(level - 0.002, 5), round(level + 0.004, 5)
        mark = c.mark()
        r = self.place(c, "r7w", type="limit", price=level, sl=sl, tp=tp)
        c.wait_event("order.placed", lambda d: d["client_id"] == "r7w",
                     since=mark)
        new_level = round(level - 0.001, 5)
        self.fake.requests(clear=True)
        c.request("order.amend", {"client_id": "r7a", "order_id":
                                  r["order_id"], "price": new_level})
        o = [x for x in c.request("orders.list")["orders"]
             if x["client_id"] == "r7w" and x["state"] == "resting"][0]

        def r5(*xs):
            return tuple(round(x, 5) for x in xs)
        self.assertEqual(r5(o["price"], o["sl"], o["tp"]),
                         r5(new_level, sl, tp))
        am = c.wait_event("order.amended", lambda d: True, since=mark)
        self.assertEqual(r5(am["price"], am["sl"], am["tp"]),
                         r5(new_level, sl, tp))
        put = [x["body"] for x in self.fake.requests() if x["method"] == "PUT"
               and "/workingorders/" in x["path"]]
        self.assertEqual(len(put), 1)
        self.assertEqual((put[0]["level"], put[0]["stopLevel"],
                          put[0]["profitLevel"]), (new_level, sl, tp))


# -- an order of unknown fate never takes a later order's deal ---------------

class TestUnknownFate(StandIn):
    def test_a_later_orders_deal_is_not_credited_to_an_order_of_unknown_fate(
            self):
        c = self.connect()
        mark = c.mark()
        # A: the gateway answers 502 and Capital.com never sees it
        self.fake.refuse_next("error.gateway", status=502, path="/positions",
                              method="POST")
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "r8a")
        self.assertTrue(cm.exception.retryable)
        # B: the same order a moment later; its confirmation takes 6 s
        self.fake.set_confirm_delay(6.0)
        try:
            self.place(c, "r8b")
            fb = self.filled(c, "r8b", mark, timeout=15)
        finally:
            self.fake.set_confirm_delay(0.1)
        time.sleep(2.0)
        self.assertEqual(self.seen(c, "order.filled", lambda d:
                                   d["client_id"] == "r8a", since=mark), [])
        self.assertEqual(len(self.terminal(c, "r8b", mark)), 1)
        self.assertEqual([(p["client_id"], p["position_id"]) for p in
                          c.request("positions.list")["positions"]],
                         [("r8b", fb["position_id"])])
        self.assertEqual(len(self.posts()), 2)
        # A asked again: still not found, and still not sent again
        with self.assertRaises(ConnectorError) as cm:
            self.place(c, "r8a")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.posts()), 2)

    def test_two_orders_that_both_executed_each_get_their_own_deal(self):
        c = self.connect()
        mark = c.mark()
        # A executes and its answer is lost; B executes, confirmation slow
        self.fake.lose_next_answer("/positions", "POST")
        try:
            self.place(c, "r8c")
        except ConnectorError as e:
            self.assertTrue(e.retryable)
        self.fake.set_confirm_delay(6.0)
        try:
            self.place(c, "r8d")
            fa = self.filled(c, "r8c", mark, timeout=15)
            fb = self.filled(c, "r8d", mark, timeout=15)
        finally:
            self.fake.set_confirm_delay(0.1)
        time.sleep(2.0)
        self.assertNotEqual(fa["position_id"], fb["position_id"])
        with self.fake.broker.lock:
            made = {d.deal_id: d.created for a in
                    self.fake.broker.accounts.values()
                    for d in a.deals.values()}
        # A's deal is the older one
        self.assertLess(made[fa["position_id"]], made[fb["position_id"]])
        for cid in ("r8c", "r8d"):
            self.assertEqual(len(self.terminal(c, cid, mark)), 1, cid)
        self.assertEqual(sorted(p["client_id"] for p in
                                c.request("positions.list")["positions"]),
                         ["r8c", "r8d"])


# -- a restart after ten minutes does not call a known order lost ------------

class TestLateRestart(StandIn):
    def test_a_sent_order_is_resolved_from_its_confirmation(self):
        # The last run sent an order, Capital.com gave its dealReference, and
        # the process died before the confirmation was read; it starts again
        # eleven minutes later.
        b = self.fake.broker
        with b.lock:
            acct = next(iter(b.accounts.values()))
            ref = b.open_position(acct, {"epic": "EURUSD", "direction":
                                         "BUY", "size": 1000})["dealReference"]
            did = next(iter(acct.deals))
        params = {"client_id": "r9s", "symbol": "EURUSD", "side": "buy",
                  "qty": 1000}
        at = int(time.time() * 1000) - 11 * 60 * 1000
        entry = {"cid": "r9s", "kind": "market", "epic": "EURUSD",
                 "dir": "BUY", "size": "1000", "level": None,
                 "sig": protocol.order_signature("order.place", params),
                 "at": at, "state": "sent", "ref": ref, "order_id": ref,
                 "filled": "0", "before": [], "levels": None, "gtd": None,
                 "key": "r9s"}
        with open(os.path.join(self.state, "capitalcom_state.json"),
                  "w") as fh:
            json.dump({"v": 1, "env": "demo", "api": "127.0.0.1",
                       "account": "", "ccy": "USD", "idem": {},
                       "journal": {"r9s": entry}, "closes": {},
                       "deal_cid": {}, "deal_oid": {}, "deal_from": {},
                       "wo_oid": {}, "levels": {}, "fills": [], "seen": [],
                       "ops_seen": [], "ops_mark": 0}, fh)
        c = self.connect()
        f = self.filled(c, "r9s", 0, timeout=10)
        self.assertEqual((f["order_id"], f["position_id"]), (ref, did))
        self.assertEqual(self.place(c, "r9s"), {"order_id": ref,
                                                "status": "accepted"})
        self.assertEqual([(p["position_id"], p["client_id"]) for p in
                          c.request("positions.list")["positions"]],
                         [(did, "r9s")])
        self.assertEqual(self.posts(), [])


class TestLateRestartOffline(unittest.TestCase):
    def test_nothing_is_lost_before_it_was_looked_for(self):
        state = tempfile.mkdtemp(prefix="cc-rev-", dir=TMP)
        at = int(time.time() * 1000) - 11 * 60 * 1000

        def entry(cid, st, ref):
            p = {"client_id": cid, "symbol": "EURUSD", "side": "buy",
                 "qty": 1000}
            return {"cid": cid, "kind": "market", "epic": "EURUSD",
                    "dir": "BUY", "size": "1000", "level": None,
                    "sig": protocol.order_signature("order.place", p),
                    "at": at, "state": st, "ref": ref, "order_id": ref or
                    "cc-" + cid, "filled": "0", "before": [],
                    "levels": None, "gtd": None, "key": cid}
        journal = {"s1": entry("s1", "sent", "o_6f1c0a4e-0000-4000-8000-1"),
                   "u1": entry("u1", "unknown", None),
                   "g1": entry("g1", "sending", None),
                   "l1": entry("l1", "lost", None)}
        with open(os.path.join(state, "capitalcom_state.json"), "w") as fh:
            json.dump({"v": 1, "env": "demo", "api": "127.0.0.1",
                       "account": "", "ccy": "USD", "idem": {},
                       "journal": journal, "closes": {}, "deal_cid": {},
                       "deal_oid": {}, "deal_from": {}, "wo_oid": {},
                       "levels": {}, "fills": [], "seen": [], "ops_seen": [],
                       "ops_mark": 0}, fh)
        ad = mod.CapitalComAdapter(argv=["--paper"], environ={
            "BRUE_BROKER_STATE": state,
            "CAPITALCOM_API_URL": "http://127.0.0.1:9/api/v1"},
            out=io.StringIO())
        self.assertEqual({k: e["state"] for k, e in ad._journal.items()},
                         {"s1": "sent", "u1": "unknown", "g1": "sending",
                          "l1": "lost"})
        # a retry of the acknowledged one is answered from what is known of
        # it (Capital.com cannot be reached here): never "send it again"
        p = {"client_id": "s1", "symbol": "EURUSD", "side": "buy",
             "qty": 1000}
        out = ad._replayed_place(ad._journal["s1"], protocol.order_signature(
            "order.place", p))
        self.assertEqual(out, {"order_id": "o_6f1c0a4e-0000-4000-8000-1",
                               "status": "accepted"})
        # and the one that was given up says only what was read
        p["client_id"] = "l1"
        with self.assertRaises(AdapterError) as cm:
            ad._replayed_place(ad._journal["l1"], protocol.order_signature(
                "order.place", p))
        self.assertEqual(cm.exception.code, "x_capitalcom_not_sent_again")
        self.assertNotIn("activity", str(cm.exception))


# -- two transactions that share a reference are two operations -------------

class TestTransactionIds(unittest.TestCase):
    def test_two_fees_with_one_reference_are_two_operations(self):
        state = tempfile.mkdtemp(prefix="cc-rev-", dir=TMP)
        out = io.StringIO()
        ad = mod.CapitalComAdapter(argv=["--paper"], environ={
            "BRUE_BROKER_STATE": state,
            "CAPITALCOM_API_URL": "http://127.0.0.1:9/api/v1"}, out=out)
        now = ad.now_ms()

        def row(days_ago, size):
            t = now - days_ago * 86_400_000
            return {"date": mod.utc_text(t), "dateUtc": mod.utc_text(t)
                    + ".000", "instrumentName": "EUR/USD",
                    "transactionType": "SWAP", "note": "Overnight fee",
                    "reference": "12345678", "size": size, "currency": "USD",
                    "status": "PROCESSED"}
        rows = [row(1, "-1.25"), row(2, "-1.31")]
        # the same rows read twice (two overlapping windows) are still two
        ad._transactions = lambda lo, hi, kind="read": rows + list(rows)
        ops = ad.handle_history_operations({})["operations"]
        self.assertEqual(sorted(o["amount"] for o in ops), [-1.31, -1.25])
        self.assertEqual(len({o["operation_id"] for o in ops}), 2)
        self.assertEqual({o["reference"] for o in ops}, {"12345678"})
        ad._ops_mark = now - 1000
        ad._poll_operations()
        ad._poll_operations()
        told = [json.loads(x)["data"] for x in out.getvalue().splitlines()
                if json.loads(x).get("event") == "balance.operation"]
        self.assertEqual(sorted(o["amount"] for o in told), [-1.31, -1.25])
        # the ids are stable: what is read again is the same operation
        self.assertEqual({o["operation_id"] for o in told},
                         {o["operation_id"] for o in ops})


# -- demo reaches the demo host only, and never in clear ---------------------

class TestEndpoints(unittest.TestCase):
    def cfg(self, argv, **e):
        return mod.resolve_environment(argv, dict(CREDS, **e))

    def test_demo_never_takes_another_host(self):
        for argv, e in ((["--paper"], {"CAPITALCOM_API_URL": LIVE}),
                        ([], {"CAPITALCOM_API_URL": LIVE}),
                        (["--paper"], {"CAPITALCOM_API_URL": LIVE,
                                       "CAPITALCOM_ENV": "live"}),
                        (["--demo"], {"CAPITALCOM_API_URL":
                                      "https://example.com/api/v1"}),
                        (["--paper"], {"CAPITALCOM_STREAM_URL":
                                       "wss://prices.example/connect"})):
            cfg = self.cfg(argv, **e)
            self.assertEqual((cfg["env"], mod._host(cfg["api_url"]),
                              mod._host(cfg["stream_url"]), cfg["mode"]),
                             ("demo", mod.DEMO_HOST, mod.STREAM_HOST,
                              "paper"), (argv, e))
            self.assertTrue(cfg["endpoint_fault"], (argv, e))
            for v in CREDS.values():
                self.assertNotIn(v, cfg["endpoint_fault"])
        # what live asks for is used, and is live
        cfg = self.cfg(["--live"], CAPITALCOM_API_URL=LIVE)
        self.assertEqual((cfg["api_url"], cfg["mode"], cfg["endpoint_fault"]),
                         (LIVE, "live", ""))
        # the stand-in on this machine is allowed in both
        for argv in (["--paper"], ["--live"]):
            cfg = self.cfg(argv, CAPITALCOM_API_URL="http://127.0.0.1:9/api/v1",
                           CAPITALCOM_STREAM_URL="ws://127.0.0.1:8/connect")
            self.assertEqual((cfg["api_url"], cfg["endpoint_fault"]),
                             ("http://127.0.0.1:9/api/v1", ""))

    def test_a_cleartext_endpoint_is_never_used(self):
        for argv in (["--paper"], ["--live"]):
            for e in ({"CAPITALCOM_API_URL":
                       "http://demo-api-capital.backend-capital.com/api/v1"},
                      {"CAPITALCOM_STREAM_URL":
                       "ws://api-streaming-capital.backend-capital.com/"
                       "connect"}):
                cfg = self.cfg(argv, **e)
                self.assertTrue(cfg["endpoint_fault"], (argv, e))
                self.assertFalse(cfg["api_url"].startswith("http:"), (argv, e))
                self.assertFalse(cfg["stream_url"].startswith("ws:"),
                                 (argv, e))
        self.assertFalse(mod._demo_endpoint(
            "http://demo-api-capital.backend-capital.com/api/v1"))
        self.assertFalse(mod._paper_stream(
            "ws://api-streaming-capital.backend-capital.com/connect"))

    def test_the_first_call_is_refused_and_nothing_is_sent(self):
        p1, p2 = no_network()
        for argv, url in ((["--paper"], LIVE), (["--live"], "http://demo-api-"
                                                "capital.backend-capital.com")):
            with p1, p2:
                ad = mod.CapitalComAdapter(argv=argv, environ=dict(
                    CREDS, CAPITALCOM_API_URL=url, BRUE_BROKER_STATE=
                    tempfile.mkdtemp(prefix="cc-rev-", dir=TMP)),
                    out=io.StringIO())
                with self.assertRaises(AdapterError) as cm:
                    ad.on_first_call()
                self.assertEqual(cm.exception.code, "x_capitalcom_endpoint")
                with self.assertRaises(AdapterError) as cm:
                    ad.handle_accounts_list({})
                self.assertEqual(cm.exception.code, "x_capitalcom_endpoint")

    def test_the_terminal_row_is_demo_whatever_the_shell_says(self):
        from brueconnect import registry
        with tempfile.TemporaryDirectory(dir=TMP) as home:
            with mock.patch.dict(os.environ, {"BRUE_CONNECT_HOME": home,
                                              "CAPITALCOM_API_URL": LIVE}):
                argv, e, _ = registry.resolve("capitalcom")
                cfg = mod.resolve_environment(argv[2:], e)
                self.assertEqual((mod._host(cfg["api_url"]), cfg["mode"]),
                                 (mod.DEMO_HOST, "paper"))
                c = Connector(argv, env=dict(e, PYTHONPATH=ROOT))
                try:
                    h = c.hello()            # cold: nothing is reached
                finally:
                    c.close()
                    for pipe in (c.proc.stdin, c.proc.stdout):
                        pipe.close()
                self.assertEqual(h["mode"], "paper")


# -- flatten_all's 404 on a working order is not a cancel -------------------

class TestFlattenMeetsATriggeredOrder(StandIn):
    def test_an_order_that_triggered_under_flatten_ends_once(self):
        c = self.connect()
        bid, ask = self.quote("EURUSD")
        mark = c.mark()
        self.place(c, "fw", type="limit", price=round(ask - 0.003, 5))
        c.wait_event("order.placed", lambda d: d["client_id"] == "fw",
                     since=mark)
        fake = self.fake
        b = fake.broker

        def trigger_then_gone(segs, q, body, sess, acct, headers):
            # the order triggers a moment before the cancel reaches it
            o = acct.orders.get(segs[1])
            if o is not None:
                inst = b.market.by_epic[o.epic]
                bid_, ask_ = b.market.quote(o.epic, b.now())
                b._trigger(acct, o, inst, ask_ if o.direction == "BUY"
                           else bid_, b.now())
            raise CapError(404, "error.not-found.dealId")
        fake._r_order_delete = trigger_then_gone
        try:
            try:
                out = c.request("flatten_all", {"client_id": "fwfa"})
            except ConnectorError as e:
                out = e
        finally:
            del fake._r_order_delete
        time.sleep(6.0)
        ends = self.terminal(c, "fw", mark)
        self.assertEqual([n for n, _ in ends], ["order.filled"], ends)
        self.assertEqual(self.seen(c, "order.cancelled", since=mark), [])
        f = ends[0][1]
        cl = self.seen(c, "position.closed",
                       lambda d: d["position_id"] == f["position_id"],
                       since=mark)
        self.assertEqual([x["reason"] for x in cl], ["flatten"])
        self.assertEqual(out, {"closed": 1})
        self.assertEqual(self.at_broker(), [])


# -- a deal booked from its confirmation has its contract size ---------------

class TestContractSize(unittest.TestCase):
    def test_a_deal_from_its_confirmation_has_its_markets_contract_size(self):
        ad = mod.CapitalComAdapter(argv=["--paper"], environ={
            "BRUE_BROKER_STATE": tempfile.mkdtemp(prefix="cc-rev-", dir=TMP),
            "CAPITALCOM_API_URL": "http://127.0.0.1:9/api/v1"},
            out=io.StringIO())
        det = {"instrument": {"epic": "OIL_CRUDE", "name": "Oil",
                              "lotSize": 10, "type": "COMMODITIES",
                              "currency": "USD", "marginFactor": 10,
                              "marginFactorUnit": "PERCENTAGE"},
               "dealingRules": {"minDealSize": {"unit": "POINTS",
                                                "value": 1},
                                "minSizeIncrement": {"unit": "POINTS",
                                                     "value": 1}},
               "snapshot": {"marketStatus": "TRADEABLE", "bid": 70.0,
                            "offer": 70.02, "decimalPlacesFactor": 2}}
        spec, priv = mod.build_spec(det)
        self.assertEqual(spec["contract_size"], 10.0)
        ad._install_catalog([spec], {"OIL_CRUDE": priv}, time.time())
        ad.account_currency = "USD"
        e = {"cid": "fc1", "kind": "market", "epic": "OIL_CRUDE",
             "dir": "BUY", "size": "2", "level": None, "sig": None,
             "at": ad.now_ms(), "state": "sent", "ref": "o_1",
             "order_id": "o_1", "filled": "0", "before": [], "levels": None,
             "gtd": None, "key": "fc1"}
        ad._journal["fc1"] = e
        p1, p2 = no_network()
        with p1, p2:
            ad._settle_market(e, {"dealStatus": "ACCEPTED", "level": 70.02,
                                  "size": 2, "affectedDeals": [
                                      {"dealId": "D1", "status": "OPENED"}]})
        d = ad._deals["D1"]
        self.assertEqual(d["contract"], 10.0)
        pnl, why = ad._realized(d, 71.02, "2")
        self.assertAlmostEqual(pnl, 20.0, places=6)


if __name__ == "__main__":
    unittest.main()
