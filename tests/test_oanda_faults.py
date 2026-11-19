"""OANDA adapter: faults in the book, in lost answers and in the paper
profile, each pinned by a test. The faults:

  - the book replaced by a newer list of OANDA's trades, and the
    transactions that list already held applied on top of it again
  - a numeric position.close qty sent as "ALL"
  - an order whose answer was lost sent again 5 s later
  - two processes of one profile overwriting each other's state file
  - CLIENT_TRADE_ID_ALREADY_EXISTS announced as order.rejected
  - order.modify (and the levels kept on a netting position) putting a
    stop already through the market on a trade
  - history.operations filtering on DIVIDEND_ADJUSTMENT, no
    TransactionFilter value
  - a carried-out amend (or cancel) whose answer was lost answered
    not_resting, kept as final
  - the `--paper` profile reaching OANDA's live hosts through an inherited
    OANDA_REST_URL / OANDA_STREAM_URL
  - orders.list with no state leaving out older resting orders
  - the gap filler moving its watermark to OANDA's lastTransactionID

The stand-in's tests drive the adapter as a platform does (spawned through
the SDK's Connector, the stand-in's fake token, loopback only); the cold ones
feed an adapter in this process with OANDA's documented answers. The race
tests open the window on purpose: the transaction stream refused (OANDA ends
streams every few hours, measured on a live account) and each step taken
just after one of the adapter's 2-second reads of OANDA's record.

Run: python3 -m unittest tests.test_oanda_faults -v
"""
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brueconnect import Connector, ConnectorError, protocol, registry  # noqa
from brueconnect.adapters.oanda import oanda_adapter as mod  # noqa: E402
from tests.fake_oanda import FakeOanda  # noqa: E402
from tests.test_oanda_core import OANDA_EUR_USD  # noqa: E402
from tests.test_oanda_standin import LEVELS_WAIT, StandIn, wait  # noqa

# documented: https://developer.oanda.com/rest-live-v20/transaction-df/
# TransactionFilter, every value the page lists (read 2026-11-14)
DOCUMENTED_FILTERS = frozenset("""ORDER FUNDING ADMIN CREATE CLOSE REOPEN
CLIENT_CONFIGURE CLIENT_CONFIGURE_REJECT TRANSFER_FUNDS TRANSFER_FUNDS_REJECT
MARKET_ORDER MARKET_ORDER_REJECT LIMIT_ORDER LIMIT_ORDER_REJECT STOP_ORDER
STOP_ORDER_REJECT MARKET_IF_TOUCHED_ORDER MARKET_IF_TOUCHED_ORDER_REJECT
TAKE_PROFIT_ORDER TAKE_PROFIT_ORDER_REJECT STOP_LOSS_ORDER
STOP_LOSS_ORDER_REJECT GUARANTEED_STOP_LOSS_ORDER
GUARANTEED_STOP_LOSS_ORDER_REJECT TRAILING_STOP_LOSS_ORDER
TRAILING_STOP_LOSS_ORDER_REJECT ONE_CANCELS_ALL_ORDER
ONE_CANCELS_ALL_ORDER_REJECT ONE_CANCELS_ALL_ORDER_TRIGGERED ORDER_FILL
ORDER_CANCEL ORDER_CANCEL_REJECT ORDER_CLIENT_EXTENSIONS_MODIFY
ORDER_CLIENT_EXTENSIONS_MODIFY_REJECT TRADE_CLIENT_EXTENSIONS_MODIFY
TRADE_CLIENT_EXTENSIONS_MODIFY_REJECT MARGIN_CALL_ENTER MARGIN_CALL_EXTEND
MARGIN_CALL_EXIT DELAYED_TRADE_CLOSURE DAILY_FINANCING
RESET_RESETTABLE_PL""".split())


# ---------------------------------------------------------------------------
# Against the stand-in
# ---------------------------------------------------------------------------

class Base(StandIn):
    """The stand-in harness, and the levers these tests use."""

    def last_seq(self):
        rows = self.fake.requests()
        return rows[-1]["seq"] if rows else 0

    def next_record_read(self, after_seq, timeout=12.0):
        """Wait until the adapter has just read OANDA's record (while the
        transaction stream is down it does so every 2 s)."""
        def seen():
            for r in self.fake.requests():
                if r["seq"] > after_seq and str(r["path"]).endswith(
                        "/transactions/sinceid"):
                    return r["seq"]
            return None
        got = wait(seen, timeout, 0.005)
        if not got:
            raise AssertionError("the adapter did not read OANDA's record")
        return got

    def stream_down(self, c):
        self.fake.refuse_connects(100000, 503, "stream")
        self.fake.drop_streams(clean=False)
        ok = wait(lambda: c.session_state.get("state") != "connected"
                  and "transaction" in c.session_state.get("detail", ""), 15)
        if not ok:
            raise AssertionError("the transaction stream did not go down: %r"
                                 % (c.session_state,))

    def stream_up(self, c):
        self.fake.refuse_connects(0, 503, "stream")
        wait(lambda: c.session_state.get("state") == "connected", 40)

    def oanda_trades(self, account=0):
        aid = self.fake.account_ids[account]
        return self.direct("GET", "/v3/accounts/%s/openTrades" % aid)["trades"]

    def oanda_units(self, sym="EUR_USD", account=0):
        return sum(float(t["currentUnits"]) for t in self.oanda_trades(account)
                   if t["instrument"] == sym)

    def open_long(self, c, cid, qty=1000, **kw):
        self.place(c, cid, qty=qty, **kw)
        return c.wait_event("order.filled", lambda d: d["client_id"] == cid)

    @staticmethod
    def after(c, mark=0):
        """The events since c.mark() returned `mark` (as (name, data))."""
        with c._event_cv:
            evs, total = list(c.events), c._events_total
        return evs[max(0, mark - (total - len(evs))):]

    def events(self, c, name, since=0):
        return [d for n, d in self.after(c, since) if n == name]


class BookCountsEachTransactionOnce(Base):
    """SPEC 7: the broker is the truth, and nothing of it is counted
    twice."""

    def test_a_reduce_made_elsewhere_is_counted_once(self):
        c = self.spawn(0)                                   # netting, GBP
        aid = self.fake.account_ids[0]
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        self.open_long(c, "b1-open", qty=1000)
        self.stream_down(c)
        s = self.next_record_read(self.last_seq())
        # another tool of the user's (or OANDA itself) reduces the position
        self.direct("PUT", "/v3/accounts/%s/positions/EUR_USD/close" % aid,
                    {"longUnits": "400", "shortUnits": "NONE"})
        m = c.mark()
        pos = c.request("positions.list")["positions"]
        self.assertEqual([(p["position_id"], p["qty"]) for p in pos],
                         [("EUR_USD", 600.0)])
        self.next_record_read(s)                # the next read of the record
        c.wait_event("position.closed", lambda d: d["qty"] == 400.0,
                     timeout=10, since=m)
        # The user closes 200 of the 600 shown, at once: 400 stay.
        c.request("position.close", {"client_id": "b1-part",
                                     "position_id": "EUR_USD", "qty": 200})
        self.assertEqual(self.oanda_units(), 400.0,
                         "a close of 200 of the 600 shown")
        self.next_record_read(self.last_seq())
        closes = [d["qty"] for n, d in self.after(c, m)
                  if n == "position.closed"]
        self.assertEqual(closes, [400.0, 200.0])
        self.stream_up(c)
        self.assertEqual(c.request("positions.list")["positions"][0]["qty"],
                         400.0)

    def test_a_stop_that_fired_is_never_put_on_the_next_position(self):
        c = self.spawn(0)
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        self.open_long(c, "b2-open", qty=1000, sl=1.09900)
        self.assertEqual(c.request("positions.list")["positions"][0]["sl"],
                         1.099)
        self.stream_down(c)
        s = self.next_record_read(self.last_seq())
        # the market falls through the stop; OANDA closes the position
        self.fake.set_price("EUR_USD", bid=1.09800, ask=1.09810)
        m = c.mark()
        self.assertEqual(c.request("positions.list")["positions"], [])
        self.next_record_read(s)
        cl = c.wait_event("position.closed", lambda d: d["reason"] == "sl",
                          timeout=10, since=m)
        self.assertEqual(cl["qty"], 1000.0)
        self.stream_up(c)
        # A NEW long, with no stop asked for
        self.open_long(c, "b2-new", qty=1000)
        time.sleep(LEVELS_WAIT)
        new_tid = [t for t in self.txs() if t["type"] == "ORDER_FILL"
                   and t.get("tradeOpened")][-1]["tradeOpened"]["tradeID"]
        stops = [t for t in self.txs() if t["type"] == "STOP_LOSS_ORDER"
                 and t.get("tradeID") == new_tid]
        self.assertEqual(stops, [], "a stop nobody asked for was put on the "
                         "new position")
        self.assertEqual(self.oanda_units(), 1000.0)
        self.assertIsNone(c.request("positions.list")["positions"][0]["sl"])

    def test_a_close_of_a_qty_closes_that_qty(self):
        """The position grew at OANDA (a resting buy filled) before the book
        heard of it; a close of 1000 closes 1000, never "ALL"."""
        c = self.spawn(0)
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        self.open_long(c, "b3-open", qty=1000)
        r = self.place(c, "b3-limit", qty=500, type="limit", price=1.09950)
        self.assertEqual(r["status"], "resting")
        self.stream_down(c)
        self.next_record_read(self.last_seq())
        self.fake.set_price("EUR_USD", bid=1.09940, ask=1.09948)
        self.assertEqual(self.oanda_units(), 1500.0)
        c.request("position.close", {"client_id": "b3-close",
                                     "position_id": "EUR_USD", "qty": 1000})
        close = [t for t in self.txs() if t["type"] == "MARKET_ORDER"
                 and t.get("longPositionCloseout")][-1]
        self.assertEqual(close["longPositionCloseout"]["units"], "1000")
        self.assertEqual(self.oanda_units(), 500.0)
        self.stream_up(c)


class LevelsStayWithTheirPosition(Base):
    """A stop or target already through the market is never sent to a
    trade (OANDA fills it at once: trade-ep, stopLossOrderFillTransaction
    "immediately fills the Trade's new Stop Order")."""

    def test_order_modify_through_the_market_is_refused(self):
        c = self.spawn(0)
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        self.open_long(c, "l1-open", qty=1000)
        m = c.mark()
        for kw in ({"sl": 1.10050}, {"tp": 1.09950}, {"sl": 1.10000}):
            with self.assertRaises(ConnectorError) as e:
                c.request("order.modify", dict(kw, client_id="l1-%s" % kw,
                                               position_id="EUR_USD"))
            self.assertEqual(e.exception.code, "invalid_stops", kw)
        time.sleep(1.0)
        self.assertEqual(self.oanda_units(), 1000.0)
        self.assertFalse(self.events(c, "position.closed", m))
        self.assertFalse([t for t in self.oanda_trades()
                          if t.get("stopLossOrder") or
                          t.get("takeProfitOrder")])
        # a stop on the right side is set
        c.request("order.modify", {"client_id": "l1-ok", "position_id":
                                   "EUR_USD", "sl": 1.09900, "tp": 1.10100})
        t = self.oanda_trades()[0]
        self.assertEqual((t["stopLossOrder"]["price"],
                          t["takeProfitOrder"]["price"]),
                         ("1.09900", "1.10100"))
        # a short: the stop above the ask, the target below it
        c2 = self.spawn(1)                                  # hedging
        f = self.open_long(c2, "l1-short", side="sell")
        with self.assertRaises(ConnectorError) as e:
            c2.request("order.modify", {"client_id": "l1-s1", "position_id":
                                        f["position_id"], "sl": 1.10005})
        self.assertEqual(e.exception.code, "invalid_stops")

    def test_a_kept_level_through_the_market_is_not_put_on_a_new_trade(self):
        """Netting: the position's stop is put on each trade it gains, but
        not when the market is already through it."""
        c = self.spawn(0)
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        c.wait_event("quote", lambda d: d["symbol"] == "EUR_USD")
        # (the resting buy first: OANDA's stand-in looks at orders in id
        # order, so it fills before the position's stop at one price)
        r = self.place(c, "l2-lim", qty=500, type="limit", price=1.09950)
        self.assertEqual(r["status"], "resting")
        self.open_long(c, "l2-open", qty=1000, sl=1.09900)
        m = c.mark()
        self.fake.set_price("EUR_USD", bid=1.09800, ask=1.09810)
        c.wait_event("order.filled", lambda d: d["client_id"] == "l2-lim",
                     since=m, timeout=10)
        c.wait_event("position.closed", lambda d: d["reason"] == "sl",
                     since=m, timeout=10)
        st = c.wait_event("session.state", lambda d: "already through" in
                          d["detail"], since=m, timeout=LEVELS_WAIT + 4)
        self.assertIn("EUR_USD", st["detail"])
        time.sleep(1.0)
        trades = self.oanda_trades()
        self.assertEqual([float(t["currentUnits"]) for t in trades], [500.0])
        self.assertFalse(trades[0].get("stopLossOrder"))


class LostAnswersNeverSentTwice(Base):
    """Orders, amends and cancels, through a loopback proxy in front of the
    stand-in's REST port that holds a request back, or forwards it and
    drops OANDA's answer. The stand-in's 2-new-connections-a-second limit
    is off here (the proxy opens connections of its own)."""

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOanda(division=cls.division,
                             new_connections_per_second=None).start()

    def setUp(self):
        super().setUp()
        self.proxy = Proxy(self.fake.port)

    def tearDown(self):
        self.proxy.close()
        super().tearDown()

    def late(self, account, side, qty, setup=None, delay=15.0):
        """The timing: OANDA carries the first request out 15 s after it was
        sent, the adapter having given up on it at 7 s; the platform asks
        again 5.6 s after that first answer (when a rule that sends it again
        after 5 s would do so), and keeps asking until it is answered."""
        c = self.spawn(account, OANDA_REST_URL=self.proxy.url)
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        if setup:
            setup(c)
        aid = self.fake.account_ids[account]
        path = "/v3/accounts/%s/orders" % aid
        self.proxy.rules.append((lambda m, p: m == "POST" and p == path,
                                 ("delay", delay)))
        m = c.mark()
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "late-1", side=side, qty=qty)
        first = e.exception
        self.assertEqual((first.code, first.retryable), ("internal", True))
        with self.assertRaises(ConnectorError) as e:
            self.place(c, "late-1", side=side, qty=qty)
        self.assertIn("not in OANDA's record yet", str(e.exception))
        time.sleep(5.6)
        r, end = None, time.monotonic() + delay + 10
        while r is None and time.monotonic() < end:
            try:
                r = self.place(c, "late-1", side=side, qty=qty)
            except ConnectorError as e:
                self.assertTrue(e.retryable, e)
                time.sleep(1.0)
        self.assertTrue(wait(lambda: any(f[1] == "late-forwarded"
                                         for f in self.proxy.fired),
                             delay + 10))
        self.assertEqual(r["status"], "accepted")
        time.sleep(1.5)
        fills = [t for t in self.txs(account) if t["type"] == "ORDER_FILL"
                 and abs(float(t["units"])) == qty
                 and (float(t["units"]) < 0) == (side == "sell")]
        self.assertEqual(len(fills), 1, "one order.place, %d executions"
                         % len(fills))
        self.assertEqual(r["order_id"], fills[0]["orderID"])
        self.assertIn("NOT sent again", str(first))
        got = [d for n, d in self.after(c, m) if n == "order.filled"
               and d.get("client_id") == "late-1"]
        self.assertEqual(len(got), 1, got)
        self.assertFalse([d for n, d in self.after(c, m)
                          if n == "order.rejected"])
        return c

    def test_a_late_netting_reduce_is_carried_out_once(self):
        def setup(c):
            self.open_long(c, "late-open", qty=1000)
        self.late(0, "sell", 400, setup)
        self.assertEqual(self.oanda_units(), 600.0)

    def test_a_late_mt4_order_is_carried_out_once(self):
        self.late(2, "buy", 1000)
        self.assertEqual(self.oanda_units(account=2), 1000.0)

    def _lost_change(self, method_path, call, params):
        c = self.spawn(0, OANDA_REST_URL=self.proxy.url)
        aid = self.fake.account_ids[0]
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        r = self.place(c, "ch-lim", qty=500, type="limit", price=1.09000)
        root = r["order_id"]
        self.stream_down(c)
        self.next_record_read(self.last_seq())
        method, tail = method_path
        path = "/v3/accounts/%s/orders/%s%s" % (aid, root, tail)
        self.proxy.rules.append((lambda m, p: m == method and p == path,
                                 ("drop_answer",)))
        p = dict(params, order_id=root)
        with self.assertRaises(ConnectorError) as e:
            c.request(call, p)
        self.assertEqual((e.exception.code, e.exception.retryable),
                         ("internal", True))
        answers = []
        for _ in range(2):
            try:
                answers.append(c.request(call, p))
            except ConnectorError as e2:
                answers.append((e2.code, e2.retryable, str(e2)[:90]))
        pend = self.direct("GET", "/v3/accounts/%s/pendingOrders" % aid)
        rest = [(o["id"], o["price"], o.get("replacesOrderID"))
                for o in pend["orders"] if o["type"] == "LIMIT"]
        self.stream_up(c)
        return c, root, answers, rest

    def test_a_carried_out_amend_whose_answer_was_lost_is_answered(self):
        c, root, answers, rest = self._lost_change(
            ("PUT", ""), "order.amend", {"client_id": "ch-am",
                                         "price": 1.09100})
        self.assertEqual(answers, [{}, {}])
        self.assertEqual([(px, rep) for _, px, rep in rest],
                         [("1.09100", root)])
        # one replace only, and the platform still knows it as `root`
        reps = [t for t in self.txs() if t.get("replacesOrderID")]
        self.assertEqual(len(reps), 1)
        rows = c.request("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(o["order_id"], o["price"]) for o in rows],
                         [(root, 1.091)])

    def test_a_carried_out_cancel_whose_answer_was_lost_is_answered(self):
        c, root, answers, rest = self._lost_change(
            ("PUT", "/cancel"), "order.cancel", {"client_id": "ch-cx"})
        self.assertEqual(answers, [{}, {}])
        self.assertEqual(rest, [])

    # -- a late close, found by its id ----------------------------------------

    def late_close(self, account, side, qty=None, marks=None, delay=9.0,
                   wait_s=None):
        """A position.close that OANDA carries out `delay` s after it was
        sent, the adapter having given up on its answer at 7 s; the platform
        asks again, once a second, until it is answered (for `wait_s`).
        `marks`: the stand-in's closeout_extensions. Returns the Connector,
        the answer or the last refusal, the closed qty said and the stand-in's
        closeouts for it."""
        c = self.spawn(account, OANDA_REST_URL=self.proxy.url)
        self.fake.set_price("EUR_USD", bid=1.10000, ask=1.10010)
        if marks:
            self.fake.set_options(closeout_extensions=marks)
        f = self.open_long(c, "lc-open", qty=1000, side=side)
        aid, pid = self.fake.account_ids[account], f["position_id"]
        path = ("/v3/accounts/%s/trades/%s/close" % (aid, pid)
                if account == 1 else
                "/v3/accounts/%s/positions/EUR_USD/close" % aid)
        self.proxy.rules.append((lambda m, p: m == "PUT" and p == path,
                                 ("delay", delay)))
        p = {"client_id": "lc-1", "position_id": pid}
        if qty is not None:
            p["qty"] = qty
        m = c.mark()
        with self.assertRaises(ConnectorError) as cm:
            c.request("position.close", p)
        self.assertEqual((cm.exception.code, cm.exception.retryable),
                         ("internal", True))
        self.assertIn("Not known yet whether OANDA carried out the close",
                      str(cm.exception))
        got, end = None, time.monotonic() + (wait_s or delay + 15)
        while time.monotonic() < end:
            try:
                got = c.request("position.close", p)
                break
            except ConnectorError as e:
                got = e
                if not e.retryable:
                    break
                time.sleep(1.0)
        self.assertTrue(wait(lambda: any(x[1] == "late-forwarded"
                                         for x in self.proxy.fired), 5))
        time.sleep(1.0)
        said = [d["qty"] for d in self.events(c, "position.closed", m)]
        outs = [t for t in self.txs(account) if t["type"] == "MARKET_ORDER"
                and (t.get("longPositionCloseout") or t.get(
                    "shortPositionCloseout") or t.get("tradeClose"))]
        return c, got, said, outs

    def found(self, account, side, qty, left, marks=None):
        """Ask again is answered with the close, which was sent once (not
        "not in OANDA's record yet" for 120 s, then "not carried out")."""
        c, got, said, outs = self.late_close(account, side, qty, marks)
        self.assertEqual(got, {}, got)
        self.assertEqual(said, [float(qty or 1000)])
        self.assertEqual(len(outs), 1, outs)
        self.assertEqual(self.oanda_units(account=account), left)
        return outs[0]

    def test_a_late_netting_close_of_a_long_is_found_by_its_id(self):
        out = self.found(0, "buy", None, 0.0)
        self.assertEqual(out["clientExtensions"]["id"][:7], "brue-c-")

    def test_a_late_netting_part_close_of_a_short_is_found_by_its_id(self):
        self.found(0, "sell", 400, -600.0)

    def test_a_late_part_close_is_found_by_its_fill_when_unmarked(self):
        """The MARKET_ORDER transaction without the extensions (were OANDA
        not to put them there), its fill naming the close's id: found by
        the fill's clientOrderID."""
        out = self.found(0, "buy", 400, 600.0, marks="fill")
        self.assertNotIn("clientExtensions", out)

    def test_a_late_close_of_a_short_is_found_by_its_fill_when_unmarked(self):
        out = self.found(0, "sell", None, 0.0, marks="fill")
        self.assertNotIn("clientExtensions", out)

    def test_a_late_hedging_part_close_is_found_by_its_trade(self):
        self.found(1, "buy", 400, 600.0)

    def test_a_late_hedging_close_of_a_short_is_found_by_its_trade(self):
        self.found(1, "sell", None, 0.0)

    def test_an_unmarked_late_close_may_be_this_one_after_the_watch(self):
        """Neither the order nor its fill marked: through the 120 s watch
        the answer is "not known", and after it never "not carried out"
        (the close was carried out), but what OANDA's record shows."""
        c, got, said, outs = self.late_close(0, "buy", marks="none",
                                             wait_s=mod.UNKNOWN_WATCH + 25)
        self.assertIsInstance(got, ConnectorError)
        self.assertEqual((got.code, got.effective_code, got.retryable),
                         ("x_oanda_close_unmarked", "internal", False))
        self.assertNotIn("not carried out", str(got))
        self.assertIn("OANDA's record shows a close of EUR_USD at", str(got))
        self.assertIn("check Positions before closing it again", str(got))
        self.assertEqual((said, len(outs), self.oanda_units()),
                         ([1000.0], 1, 0.0))
        with self.assertRaises(ConnectorError) as cm:     # final, the same
            c.request("position.close", {"client_id": "lc-1",
                                         "position_id": "EUR_USD"})
        self.assertEqual(cm.exception.code, "x_oanda_close_unmarked")


class Proxy:
    """A loopback HTTP/1.1 proxy in front of the stand-in's REST port.
    rules: [(match(method, path), action)], each
    used once: ("drop_answer",) forwards the request, reads OANDA's answer and
    throws it away (the work done, the answer lost); ("delay", s) cuts the
    client loose and forwards the request s seconds later on a connection of
    its own (a request carried out late, after the client gave up)."""

    def __init__(self, upstream_port):
        self.up = upstream_port
        self.rules, self.fired = [], []
        self._lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.url = "http://127.0.0.1:%d" % self.srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass

    def _action(self, method, path):
        with self._lock:
            for i, (match, action) in enumerate(self.rules):
                if match(method, path):
                    self.rules.pop(i)
                    self.fired.append((time.time(), method, path, action))
                    return action
        return None

    def _accept(self):
        while not self._stop:
            try:
                cli, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(cli,),
                             daemon=True).start()

    @staticmethod
    def _head(sock, buf):
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return None, buf
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    def _field(head, name):
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            if k.strip().lower() == name:
                return v.strip()
        return None

    def _body(self, sock, head, rest):
        n = self._field(head, b"content-length")
        if n is not None:
            n = int(n)
            while len(rest) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                rest += chunk
            return rest[:n], rest[n:]
        te = self._field(head, b"transfer-encoding") or b""
        if b"chunked" in te.lower():
            while not rest.endswith(b"0\r\n\r\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                rest += chunk
            return rest, b""
        return b"", rest

    def _serve(self, cli):
        up, buf = None, b""
        try:
            while True:
                head, buf = self._head(cli, buf)
                if head is None:
                    return
                body, buf = self._body(cli, head, buf)
                first = head.split(b"\r\n", 1)[0].decode("latin-1").split()
                method, path = first[0], first[1].split("?")[0]
                action = self._action(method, path)
                if action and action[0] == "delay":
                    raw = head + b"\r\n\r\n" + body
                    threading.Thread(target=self._late,
                                     args=(raw, action[1]),
                                     daemon=True).start()
                    time.sleep(action[1] + 1.0)
                    return
                if up is None:
                    up = socket.create_connection(("127.0.0.1", self.up),
                                                  timeout=30)
                up.sendall(head + b"\r\n\r\n" + body)
                rhead, rrest = self._head(up, b"")
                if rhead is None:
                    return
                rbody, _ = self._body(up, rhead, rrest)
                if action and action[0] == "drop_answer":
                    return
                cli.sendall(rhead + b"\r\n\r\n" + rbody)
        except OSError:
            return
        finally:
            for s in (cli, up):
                try:
                    if s is not None:
                        s.close()
                except OSError:
                    pass

    def _late(self, raw, seconds):
        time.sleep(seconds)
        try:
            up = socket.create_connection(("127.0.0.1", self.up), timeout=30)
            up.sendall(raw)
            head, rest = self._head(up, b"")
            self._body(up, head or b"", rest)
            up.close()
            with self._lock:
                self.fired.append((time.time(), "late-forwarded", "", None))
        except OSError:
            pass


class TwoProcessesOfOneProfile(Base):
    """The unbound process that lists the accounts and the bound one that
    trades share BRUE_BROKER_STATE (SPEC 4, 8.5)."""

    def run_case(self, account, cid):
        self.remember()
        lister = self.spawn(account=None, prime=False, connect=False)
        time.sleep(1.2)
        a = self.spawn(account, prime=False)
        self.place(a, cid, qty=1000)
        a.wait_event("order.filled", lambda d: d["client_id"] == cid)
        time.sleep(1.2)
        lister.request("accounts.list")
        a.proc.kill()
        a.proc.wait()
        time.sleep(1.2)
        a2 = self.spawn(account, prime=False)
        out = self.place(a2, cid, qty=1000)
        rej = [d for n, d in list(a2.events) if n == "order.rejected"]
        fills = [t for t in self.txs(account) if t["type"] == "ORDER_FILL"
                 and float(t["units"]) == 1000.0]
        self.assertEqual(len(fills), 1, "one order.place, %d executions"
                         % len(fills))
        self.assertFalse(rej, rej)
        self.assertEqual(out["status"], "accepted")
        self.assertEqual(self.oanda_units(account=account), 1000.0)
        # the listing's own memory is in the file too
        lister.request("accounts.list")

    def test_mt4_a_restart_after_a_listing_does_not_trade_again(self):
        self.run_case(2, "tp-mt4")

    def test_netting_a_restart_after_a_listing_does_not_trade_again(self):
        self.run_case(0, "tp-net")


class ClientIdHeldAtOanda(Base):
    """OANDA refuses a client id while an earlier request under it rests
    or its trade is open; that earlier request is this one, and its outcome
    is the answer. (The profile's memory is removed by hand: lost with a
    disk, or another machine.)"""

    def forget(self, c):
        c.proc.kill()
        c.proc.wait()
        os.remove(os.path.join(self.dir, "oanda_state.json"))
        time.sleep(1.2)

    def test_a_filled_order_is_answered_with_its_fill_not_rejected(self):
        c = self.spawn(0)
        r1 = self.place(c, "held-m", qty=1000)
        f = c.wait_event("order.filled", lambda d: d["client_id"] == "held-m")
        self.forget(c)
        c2 = self.spawn(0)
        m = c2.mark()
        r2 = self.place(c2, "held-m", qty=1000)
        self.assertEqual(r2, r1)
        again = c2.wait_event("order.filled", lambda d: d["client_id"] ==
                              "held-m", since=m, timeout=5)
        self.assertEqual((again["fill_id"], again["order_id"], again["qty"]),
                         (f["fill_id"], f["order_id"], 1000.0))
        self.assertFalse(self.events(c2, "order.rejected"))
        self.assertEqual(self.oanda_units(), 1000.0)
        # a different order under the same id is still refused
        with self.assertRaises(ConnectorError) as e:
            self.place(c2, "held-m", qty=2000)
        self.assertEqual(e.exception.code, "duplicate_client_id")

    def test_a_resting_order_is_answered_with_itself(self):
        c = self.spawn(0)
        r1 = self.place(c, "held-l", qty=500, type="limit", price=1.00000)
        self.assertEqual(r1["status"], "resting")
        self.forget(c)
        c2 = self.spawn(0)
        m = c2.mark()
        r2 = self.place(c2, "held-l", qty=500, type="limit", price=1.00000)
        self.assertEqual(r2, r1)
        self.assertFalse(self.events(c2, "order.rejected", m))
        self.assertEqual(len([o for o in self.direct(
            "GET", "/v3/accounts/%s/pendingOrders" % self.fake.account_ids[0]
        )["orders"] if o["type"] == "LIMIT"]), 1)
        c2.request("order.cancel", {"client_id": "held-x",
                                    "order_id": r1["order_id"]})
        cx = c2.wait_event("order.cancelled", lambda d: d["order_id"] ==
                           r1["order_id"], since=m)
        self.assertEqual(cx["client_id"], "held-l")


class OnlyDocumentedFilters(Base):
    """history.operations asks OANDA only for TransactionFilter values, and
    dividend adjustments still come."""

    def test_history_operations(self):
        c = self.spawn(0)
        aid = self.fake.account_ids[0]
        self.fake.set_price("UK100_GBP", bid=8200.0, ask=8201.0)
        self.fake.set_market("open")
        self.place(c, "op-uk", sym="UK100_GBP", qty=2)
        c.wait_event("order.filled", lambda d: d["client_id"] == "op-uk")
        self.fake.dividend("UK100_GBP", "1.5")
        self.fake.transfer(aid, "-250")
        self.fake.requests(clear=True)
        ops = c.request("history.operations")["operations"]
        sent = [parse_qs(r["query"]).get("type", [""])[0]
                for r in self.fake.requests() if "/transactions" in r["path"]]
        self.assertTrue(sent)
        for value in sent:
            self.assertTrue(set(value.split(",")) <= DOCUMENTED_FILTERS,
                            value)
        kinds = [o["kind"] for o in ops]
        self.assertIn("dividend", kinds)
        self.assertIn("withdrawal", kinds)


class PaperProfile(unittest.TestCase):
    """Cold: the built-in profile, spawned as the Terminal spawns it,
    with another tool's OANDA_REST_URL / OANDA_STREAM_URL for the live hosts
    in the platform's own environment."""

    def test_the_paper_profile_stays_on_practice(self):
        saved = {k: os.environ.get(k) for k in ("OANDA_REST_URL",
                                                 "OANDA_STREAM_URL")}
        os.environ["OANDA_REST_URL"] = "https://api-fxtrade.oanda.com"
        os.environ["OANDA_STREAM_URL"] = "https://stream-fxtrade.oanda.com"
        try:
            argv, env, sdir = registry.resolve("oanda")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(argv[-1], "--paper")
        self.assertEqual(env.get("OANDA_REST_URL"),
                         "https://api-fxtrade.oanda.com")
        err = tempfile.TemporaryFile("w+")
        c = Connector(argv, env=env, stderr=err)
        try:
            h = c.hello()
        finally:
            c.close()
            for pipe in (c.proc.stdin, c.proc.stdout):
                try:
                    pipe.close()
                except Exception:
                    pass
        err.seek(0)
        said = err.read()
        err.close()
        self.assertEqual(h["mode"], "paper")
        self.assertIn("not used", said)
        cfg = mod.resolve_environment(argv[2:], env)
        self.assertEqual((cfg["rest_url"], cfg["stream_url"]),
                         (mod.PRACTICE_REST_URL, mod.PRACTICE_STREAM_URL))


# ---------------------------------------------------------------------------
# Cold: an adapter in this process, fed OANDA's documented answers
# ---------------------------------------------------------------------------

ACCT = "101-004-1-001"


class FakeClient:
    """Answers `OandaClient.call` from a table of (method, path suffix) ->
    answer (a dict, or a callable of query and body); records every call."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, method, path, query=None, body=None, kind="read",
                 timeout=None):
        self.calls.append((method, path, dict(query or {}), body))
        for (m, tail), ans in self.table.items():
            if m == method and path.endswith(tail):
                return ans(query or {}, body) if callable(ans) else ans
        raise mod.OandaError(404, "not in this test's table",
                             reason="NOT_FOUND")


def cold(state_dir=None, hedging=False):
    out = io.StringIO()
    env = {"BRUE_CRED_TOKEN": "t" * 24}
    if state_dir:
        env["BRUE_BROKER_STATE"] = state_dir
    a = mod.OandaAdapter(argv=["--paper"], environ=env, out=out)
    a._hedging = hedging
    a._acct = ACCT
    spec, own = mod.build_instrument(OANDA_EUR_USD)
    a._catalog, a._inst = {"EUR_USD": spec}, {"EUR_USD": own}
    a._contig = a._high = 100
    a._opened = True
    return a, out


def lines(out):
    return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


def events(out, name):
    return [m["data"] for m in lines(out)
            if m.get("type") == "event" and m.get("event") == name]


def market_fill(base, cid, units, closes=None, opened=True,
                reason="MARKET_ORDER"):
    create = {"id": str(base), "type": "MARKET_ORDER", "batchID": str(base),
              "instrument": "EUR_USD", "units": units, "timeInForce": "FOK",
              "reason": "CLIENT_ORDER", "time": "1794677547.000000000",
              "clientExtensions": {"id": cid}}
    fill = {"id": str(base + 1), "type": "ORDER_FILL", "batchID": str(base),
            "orderID": str(base), "instrument": "EUR_USD", "units": units,
            "fullVWAP": "1.10000", "reason": reason, "commission": "0",
            "time": "1794677547.100000000", "accountBalance": "100000.0"}
    if opened:
        fill["tradeOpened"] = {"tradeID": str(base + 1), "units": units,
                               "price": "1.10000",
                               "clientExtensions": {"id": cid}}
    if closes:
        fill["tradesClosed"] = closes
    return [create, fill]


class ColdBook(unittest.TestCase):
    def test_a_newer_list_is_caught_up_never_counted_twice(self):
        """positions.list reads a list of trades newer than the book;
        the transactions in between are applied once (their close is said
        once), however often they arrive afterwards."""
        a, out = cold()
        a._cids["k1"] = "k1"
        a._ingest(market_fill(101, "k1", "1000"))           # trade 102
        reduce = [{"id": "103", "type": "MARKET_ORDER", "batchID": "103",
                   "instrument": "EUR_USD", "units": "-400",
                   "reason": "POSITION_CLOSEOUT",
                   "time": "1794677550.0"},
                  {"id": "104", "type": "ORDER_FILL", "batchID": "103",
                   "orderID": "103", "instrument": "EUR_USD",
                   "units": "-400", "fullVWAP": "1.10100",
                   "reason": "MARKET_ORDER_POSITION_CLOSEOUT",
                   "time": "1794677550.1",
                   "tradeReduced": {"tradeID": "102", "units": "-400",
                                    "price": "1.10100", "realizedPL": "0.4",
                                    "financing": "0"}}]
        trade = {"id": "102", "instrument": "EUR_USD", "price": "1.10000",
                 "openTime": "1794677547.1", "currentUnits": "600",
                 "initialUnits": "1000", "state": "OPEN", "financing": "0",
                 "unrealizedPL": "0.1", "clientExtensions": {"id": "k1"}}
        a.client.call = FakeClient({
            ("GET", "/openTrades"): {"trades": [trade],
                                     "lastTransactionID": "104"},
            ("GET", "/openPositions"): {"positions": [
                {"instrument": "EUR_USD", "unrealizedPL": "0.1"}]},
            ("GET", "/transactions/sinceid"): {"transactions": reduce,
                                               "lastTransactionID": "104"}})
        pos = a.handle_positions_list({})["positions"]
        self.assertEqual([(p["position_id"], p["qty"]) for p in pos],
                         [("EUR_USD", 600.0)])
        a._ingest(reduce)                 # the stream brings them later
        a._ingest(list(reversed(reduce)))
        self.assertEqual(a._net("EUR_USD"), Decimal(600))
        closed = events(out, "position.closed")
        self.assertEqual([c["qty"] for c in closed], [400.0])
        self.assertEqual(a.handle_positions_list({})["positions"][0]["qty"],
                         600.0)

    def test_the_list_at_the_book_s_point_keeps_the_adapter_s_own(self):
        """A trade the catch-up opened keeps what only this adapter
        knows of it (its share of the fill's commission) when OANDA's list is
        then taken as the book; and its fill is announced once."""
        a, out = cold()
        a._cids["k5"] = "k5"
        txs = market_fill(101, "k5", "1000")
        txs[1]["commission"] = "1.5"
        trade = {"id": "102", "instrument": "EUR_USD", "price": "1.10000",
                 "openTime": "1794677547.1", "currentUnits": "1000",
                 "initialUnits": "1000", "state": "OPEN", "financing": "0",
                 "unrealizedPL": "0.1", "clientExtensions": {"id": "k5"}}
        a.client.call = FakeClient({
            ("GET", "/openTrades"): {"trades": [trade],
                                     "lastTransactionID": "102"},
            ("GET", "/openPositions"): {"positions": [
                {"instrument": "EUR_USD", "unrealizedPL": "0.1"}]},
            ("GET", "/transactions/sinceid"): {"transactions": txs,
                                               "lastTransactionID": "102"}})
        pos = a.handle_positions_list({})["positions"]
        self.assertEqual((pos[0]["qty"], pos[0]["commission"],
                          pos[0]["client_id"]), (1000.0, -1.5, "k5"))
        self.assertEqual([e["fill_id"] for e in events(out, "order.filled")],
                         ["102"])
        a._ingest(txs)
        self.assertEqual(len(events(out, "order.filled")), 1)

    def test_a_qty_is_never_sent_as_all(self):
        a, _ = cold()
        own = a._inst["EUR_USD"]
        self.assertEqual(a._close_units(own, 1000, Decimal(1000)), "1000")
        self.assertEqual(a._close_units(own, 1000.0, Decimal(1500)), "1000")
        self.assertEqual(a._close_units(own, None, Decimal(1500)), "ALL")
        with self.assertRaises(mod.AdapterError):
            a._close_units(own, 1001, Decimal(1000))

    def test_the_gap_filler_moves_to_what_it_read(self):
        """An answer naming a newer last id than it holds."""
        a, out = cold()
        a._beat_last = 103
        rows = [{"id": "101", "type": "CLIENT_CONFIGURE"},
                {"id": "102", "type": "CLIENT_CONFIGURE"}]
        a.client.call = FakeClient({("GET", "/transactions/sinceid"): {
            "transactions": rows, "lastTransactionID": "103"}})
        a._fill_gap()
        self.assertEqual(a._contig, 102)
        late = {"id": "103", "type": "TRANSFER_FUNDS", "amount": "-50.0",
                "fundingReason": "CLIENT_FUNDING", "accountBalance": "99950",
                "time": "1794677547.0"}
        a.client.call = FakeClient({("GET", "/transactions/sinceid"): {
            "transactions": [late], "lastTransactionID": "103"}})
        a._gap_t = 0.0
        a._fill_gap()
        self.assertEqual(a._contig, 103)
        self.assertEqual([o["operation_id"] for o in
                          events(out, "balance.operation")], ["103"])

    def test_ids_oanda_never_returns_are_not_waited_for_ever(self):
        a, _ = cold()
        a._beat_last = 105
        a.client.call = FakeClient({("GET", "/transactions/sinceid"): {
            "transactions": [], "lastTransactionID": "105"}})
        for _ in range(mod.EMPTY_GAP_READS):
            a._gap_t = 0.0
            a._fill_gap()
        self.assertEqual(a._contig, 105)

    def test_orders_list_holds_every_resting_order(self):
        a, _ = cold()
        old = {"id": "7", "type": "LIMIT", "instrument": "EUR_USD",
               "units": "100", "price": "1.00000", "timeInForce": "GTC",
               "state": "PENDING", "createTime": "1700000000.0"}
        recent = [{"id": str(1000 + i), "type": "TAKE_PROFIT", "tradeID": "5",
                   "price": "1.2", "state": "CANCELLED",
                   "createTime": "1794677547.0"} for i in range(500)]
        a.client.call = FakeClient({
            ("GET", "/pendingOrders"): {"orders": [old]},
            ("GET", "/orders"): {"orders": recent}})
        rows = a.handle_orders_list({})["orders"]
        self.assertEqual([(o["order_id"], o["state"]) for o in rows],
                         [("7", "resting")])

    def test_history_without_to_lets_oanda_end_the_range(self):
        """`to` from this machine's clock (a millisecond coarse, perhaps
        behind OANDA's) leaves out the newest transactions. Without one
        asked, none is sent (documented: transaction-ep `to`
        "[default=Current Time]")."""
        a, _ = cold()
        a.client.call = fake = FakeClient({
            ("GET", "/transactions"): {"pages": [],
                                       "lastTransactionID": "100"}})
        a.handle_history_operations({})
        a.handle_history_fills({})
        sent = [q for m, path, q, b in fake.calls
                if path.endswith("/transactions")]
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("to" not in q and "from" in q for q in sent))
        self.assertEqual(sent[0]["type"],
                         "TRANSFER_FUNDS,DAILY_FINANCING,FUNDING")
        to = a.now_ms() - 3_600_000
        a.handle_history_fills({"to": to})
        self.assertEqual(fake.calls[-1][2]["to"], mod.unix_s(to))

    def test_through_the_market(self):
        """Which level OANDA would fill at once."""
        t = mod.OandaAdapter._through
        bid, ask = Decimal("1.10000"), Decimal("1.10010")
        self.assertEqual(t(True, Decimal("1.10000"), None, bid, ask), "stop")
        self.assertIsNone(t(True, Decimal("1.09999"), None, bid, ask))
        self.assertEqual(t(True, None, Decimal("1.10000"), bid, ask),
                         "target")
        self.assertEqual(t(False, Decimal("1.10010"), None, bid, ask), "stop")
        self.assertIsNone(t(False, Decimal("1.10011"), Decimal("1.1"), bid,
                            ask))
        self.assertEqual(t(False, None, Decimal("1.10010"), bid, ask),
                         "target")


class ColdLostAnswer(unittest.TestCase):
    """Cold: what is said once OANDA's record has shown nothing for
    UNKNOWN_WATCH after the answer was lost."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oanda-fix-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def table(self):
        return FakeClient({
            ("GET", "/transactions/sinceid"): {"transactions": [],
                                               "lastTransactionID": "100"},
            ("GET", "/pricing"): {"prices": [{
                "instrument": "EUR_USD", "bids": [{"price": "1.10000"}],
                "asks": [{"price": "1.10010"}], "time": "1794677547.0"}],
                "homeConversions": []},
            ("GET", "/summary"): {"account": {"marginAvailable": "100000",
                                              "NAV": "100000",
                                              "marginUsed": "0"}},
            ("POST", "/orders"): {}})

    def test_never_sent_again_and_final_after_the_watch(self):
        a, out = cold(self.dir)
        a._bind_account(ACCT)
        a.client.call = fake = self.table()
        p = {"client_id": "g1", "symbol": "EUR_USD", "side": "buy",
             "qty": 1000}
        now = a.now_ms()
        a._journal["place:g1"] = {
            "m": "order.place", "cid": "g1", "ocid": "g1", "sym": "EUR_USD",
            "units": "1000", "before": 100, "unknown": True,
            "sig": protocol.order_signature("order.place", p),
            "at": now - 10_000, "lost_at": now - 9_000}
        a._mutate("r1", "order.place", p)            # 9 s after the loss
        self.assertFalse([c for c in fake.calls if c[0] == "POST"],
                         "the order was sent again 9 s after the loss")
        a._journal["place:g1"]["lost_at"] = now - 121_000
        a._mutate("r2", "order.place", p)            # past the watch
        a._mutate("r3", "order.place", p)            # and again
        res = [m for m in lines(out) if m.get("type") == "res"]
        self.assertEqual(res[0]["error"]["code"], "internal")
        self.assertTrue(res[0]["error"]["retryable"])
        for r in res[1:]:
            self.assertEqual((r["error"]["code"], r["error"].get("like"),
                              r["error"]["retryable"]),
                             ("x_oanda_not_carried_out", "internal", False))
        self.assertFalse([c for c in fake.calls if c[0] == "POST"],
                         "the order was sent again")
        rej = events(out, "order.rejected")
        self.assertEqual([(r["client_id"], r["code"]) for r in rej],
                         [("g1", "x_oanda_not_carried_out")])
        # final across a restart too
        a._save_state()
        b, out_b = cold(self.dir)
        b._bind_account(ACCT)
        b.client.call = self.table()
        b._mutate("r4", "order.place", p)
        r4 = [m for m in lines(out_b) if m.get("type") == "res"][0]
        self.assertEqual(r4["error"]["code"], "x_oanda_not_carried_out")

    @staticmethod
    def unanswered(kind=TimeoutError, status=None, body=b""):
        """The error OandaClient.call itself raises for a write that went
        out and got no answer (`kind` ended the wait), or (`status`) one
        answered with that status and `body`. The client is aimed at this
        machine, port 9 (nothing listens there): only its connection is
        stubbed, and anything else it opened would fail closed rather than
        reach OANDA."""
        client = mod.OandaClient(mod.resolve_environment(
            ["--paper"], {"BRUE_CRED_TOKEN": "t" * 24,
                          "OANDA_REST_URL": "http://127.0.0.1:9",
                          "OANDA_STREAM_URL": "http://127.0.0.1:9"}))
        assert (client.rest.host, client.stream.host) == ("127.0.0.1",) * 2

        class Resp:
            will_close = True

            def __init__(self):
                self.status = status

            def read(self):
                return body

        class Conn:
            sock, timeout = None, None

            def request(self, *a, **k):
                if status is None:
                    raise kind("no answer")

            def getresponse(self):
                return Resp()

            def close(self):
                pass

        client.rest.take = lambda tmo, until, max_idle=None: (Conn(), True)
        try:
            client.call("POST", "/v3/accounts/%s/orders" % ACCT, body={},
                        kind="write")
        except mod.OandaError as e:
            return e
        raise AssertionError("the write was answered")

    def said_for(self, err):
        """What place, cancel, amend and close say when their write fails
        with `err` and OANDA's record shows nothing of it."""
        a, out = cold(tempfile.mkdtemp(prefix="oanda-fix-", dir=self.dir))
        a._bind_account(ACCT)
        a._cids["k1"] = "k1"
        a._ingest(market_fill(101, "k1", "1000"))   # a long to close
        fake = self.table()

        def lost(q, b):
            raise err
        fake.table[("POST", "/orders")] = lost
        fake.table[("GET", "/orders/7")] = {"order": {
            "id": "7", "type": "LIMIT", "instrument": "EUR_USD",
            "units": "100", "price": "1.00000", "state": "PENDING",
            "timeInForce": "GTC", "createTime": "1794677547.0"}}
        fake.table[("PUT", "/orders/7/cancel")] = lost
        fake.table[("PUT", "/orders/7")] = lost
        fake.table[("PUT", "/positions/EUR_USD/close")] = lost
        a.client.call = fake
        a._mutate("r1", "order.place", {
            "client_id": "w1", "symbol": "EUR_USD", "side": "buy",
            "qty": 1000})
        a._mutate("r2", "order.cancel", {"client_id": "w2", "order_id": "7"})
        a._mutate("r3", "order.amend", {"client_id": "w3", "order_id": "7",
                                        "price": 1.0001})
        a._mutate("r4", "position.close", {"client_id": "w4",
                                           "position_id": "EUR_USD"})
        said = {m["id"]: m["error"] for m in lines(out)
                if m.get("type") == "res"}
        self.assertEqual(sorted(said), ["r1", "r2", "r3", "r4"])
        for e in said.values():           # the platform reads these two
            self.assertEqual((e["code"], e["retryable"]), ("internal", True))
        return {rid: e["message"] for rid, e in said.items()}

    WHAT = (("r1", "the order"), ("r2", "the cancel"), ("r3", "the amend"),
            ("r4", "the close"))

    def test_the_words_not_known_come_first(self):
        """The words that say the outcome is not known come first, so a
        display cut short (a ticket shows 80 characters) keeps them,
        whichever request and whatever ended the wait."""
        import http.client
        for kind in (TimeoutError, http.client.RemoteDisconnected,
                     ConnectionResetError, BrokenPipeError,
                     http.client.IncompleteRead):
            with self.subTest(kind=kind.__name__):
                said = self.said_for(self.unanswered(kind))
                for rid, what in self.WHAT:
                    self.assertTrue(said[rid].startswith(
                        "Not known yet whether OANDA carried out %s: it did "
                        "not answer (%s)." % (what, kind.__name__)),
                        said[rid])
                    self.assertIn("Not known yet", said[rid][:80])

    def test_an_answer_that_could_not_be_read_says_so_in_one_short_line(self):
        """OANDA's "answer was not JSON" is not wrapped as "OANDA did not
        answer (OANDA's answer was not JSON (28 bytes))" when OANDA has
        answered, and a gateway's HTML page does not go into the message
        raw, line breaks and all. An unreadable answer says it could not be
        read; a gateway's page is said by its title, in one short line."""
        page = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n"
                b"<body>\r\n<center><h1>502 Bad Gateway</h1></center>\r\n"
                b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n")
        e = self.unanswered(status=201, body=b"<html>oops</html>")
        self.assertEqual((e.status, e.code, e.sent), (0, "lost", True))
        self.assertTrue(e.cause, "the unreadable answer has a cause")
        said = self.said_for(e)
        for rid, what in self.WHAT:
            self.assertTrue(said[rid].startswith(
                "Not known yet whether OANDA carried out %s: its answer "
                "could not be read (not JSON, 17 bytes)." % what), said[rid])
        e = self.unanswered(status=502, body=page)
        self.assertEqual((e.status, e.message), (502, "Bad Gateway"))
        said = self.said_for(e)
        for rid, what in self.WHAT:
            self.assertTrue(said[rid].startswith(
                "Not known yet whether OANDA carried out %s: it answered HTTP "
                "502 (Bad Gateway)." % what), said[rid])
            self.assertNotRegex(said[rid], r"[\r\n<>]")
        # a page with no title: its text, one line, cut short
        e = self.unanswered(status=503, body=b"<p>Service\r\n\r\nUnavailable"
                            b" " + b"x" * 400 + b"</p>")
        self.assertRegex(e.message, r"^Service Unavailable x+\.\.\.$")
        self.assertLessEqual(len(e.message), 80)

    def test_a_lost_answer_says_its_words_once(self):
        """The client's own sentence for a write that got no answer already
        reads "OANDA did not answer (TimeoutError)"; an order's refusal
        that wraps it in the same words fills most of the 80 characters a
        ticket shows, before "not known" is reached. The same holds for the
        cancel, the amend and the close ("OANDA did not answer the cancel
        (OANDA did not answer (...))"). The words are said once."""
        import http.client
        for kind in (TimeoutError, http.client.RemoteDisconnected):
            with self.subTest(kind=kind.__name__):
                said = self.said_for(self.unanswered(kind))
                for rid, what in self.WHAT:
                    self.assertTrue(said[rid].startswith(
                        "Not known yet whether OANDA carried out %s: it did "
                        "not answer (%s)." % (what, kind.__name__)),
                        said[rid])
                    self.assertEqual(said[rid].count("did not answer"), 1,
                                     said[rid])
                    self.assertEqual(said[rid].count("OANDA"), 2, said[rid])
        # the cause, in words: a server's error keeps its own words
        for e, why in (
                (mod.OandaError(502, "Bad Gateway"),
                 "it answered HTTP 502 (Bad Gateway)"),
                (mod.OandaError(504, ""), "it answered HTTP 504"),
                (mod.OandaError(0, "OANDA did not answer (TimeoutError)",
                                code="lost", cause="TimeoutError"),
                 "it did not answer (TimeoutError)"),
                (mod.OandaError(0, "OANDA did not answer", code="lost",
                                cause=""), "it did not answer")):
            self.assertEqual(mod.unanswered(e), why)

    def test_the_session_line_says_when_it_was_sent_and_when_lost(self):
        """The line does not name the time the adapter gave up on the answer
        as the time the order was sent. It gives the journal's own send time,
        the time the answer was lost, and the watch's end from the loss."""
        a, out = cold(self.dir)
        a._bind_account(ACCT)
        sent = 1794694101780                      # 22:08:21.78 UTC
        hms = lambda ms: time.strftime("%H:%M:%S", time.gmtime(ms / 1000))
        a._journal["place:w3"] = {
            "m": "order.place", "cid": "w3", "ocid": "w3", "sym": "EUR_USD",
            "units": "1000", "before": 100, "unknown": True, "sig": "s",
            "at": sent, "lost_at": sent + 7350}
        a._say_unknown("place:w3")
        line = events(out, "session.state")[-1]["detail"]
        # named by what it does, never by the platform's client_id
        self.assertIn("the order to buy 1000 EUR_USD was sent at %s UTC; "
                      "OANDA's answer was lost at %s UTC:" % (
                          hms(sent), hms(sent + 7350)), line)
        self.assertIn("until %s UTC" % hms(sent + 7350 + 120_000), line)
        # one that was on the wire when an earlier run stopped: no loss time
        # is known, and none is made up
        a._journal["place:w4"] = {
            "m": "order.place", "cid": "w4", "ocid": "w4", "sym": "EUR_USD",
            "units": "-500", "before": 100, "sig": "s", "at": sent}
        a._say_unknown("place:w4")
        line = events(out, "session.state")[-1]["detail"]
        self.assertIn("the order to sell 500 EUR_USD was sent at %s UTC "
                      "and OANDA's answer was lost:" % hms(sent), line)
        self.assertIn("until %s UTC" % hms(sent + 120_000), line)

    def test_the_backstop_gives_the_final_answer_once(self):
        a, out = cold(self.dir)
        a._bind_account(ACCT)
        a.client.call = self.table()
        now = a.now_ms()
        a._journal["place:g2"] = {
            "m": "order.place", "cid": "g2", "ocid": "g2", "sym": "EUR_USD",
            "units": "-500", "before": 100, "unknown": True, "sig": "s",
            "at": now - 125_000, "lost_at": now - 121_000}
        a._watch_unknown()
        a._watch_unknown()
        self.assertEqual([r["client_id"] for r in
                          events(out, "order.rejected")], ["g2"])
        self.assertNotIn("place:g2", a._journal)
        self.assertEqual(a._final["g2"]["code"], "x_oanda_not_carried_out")


def closeout(base, side, units, held, ocid=None, fill_cid=None,
             sym="EUR_USD", at="1794677560", trade=None):
    """A close of a netting position as OANDA records it: its MARKET_ORDER
    (reason POSITION_CLOSEOUT, long/shortPositionCloseout) and its
    ORDER_FILL (documented: transaction-df MarketOrderTransaction,
    OrderFillTransaction; position-ep). `units` is what was asked ("ALL" or
    a number), `held` the position's size; with `trade`, a trade close
    (reason TRADE_CLOSE, tradeClose) instead. clientExtensions on the
    create, clientOrderID on the fill, only when given."""
    take = held if units == "ALL" else int(units)
    sign = "-" if side == "long" else ""
    create = {"id": str(base), "type": "MARKET_ORDER", "batchID": str(base),
              "instrument": sym, "units": sign + str(take),
              "timeInForce": "FOK", "positionFill": "REDUCE_ONLY",
              "time": at + ".000000000"}
    if trade is None:
        create["reason"] = "POSITION_CLOSEOUT"
        create[side + "PositionCloseout"] = {"instrument": sym,
                                             "units": units}
    else:
        create["reason"] = "TRADE_CLOSE"
        create["tradeClose"] = {"tradeID": trade, "units": units}
    if ocid:
        create["clientExtensions"] = {"id": ocid}
    fill = {"id": str(base + 1), "type": "ORDER_FILL", "batchID": str(base),
            "orderID": str(base), "instrument": sym,
            "units": sign + str(take), "fullVWAP": "1.10100",
            "reason": ("MARKET_ORDER_POSITION_CLOSEOUT" if trade is None
                       else "MARKET_ORDER_TRADE_CLOSE"),
            "commission": "0", "time": at + ".100000000",
            "accountBalance": "100000.0"}
    part = {"tradeID": trade or "102", "units": sign + str(take),
            "price": "1.10100", "realizedPL": "0.4", "financing": "0"}
    if take >= held:
        fill["tradesClosed"] = [part]
    else:
        fill["tradeReduced"] = part
    if fill_cid:
        fill["clientOrderID"] = fill_cid
    return [create, fill]


class ColdLateClose(unittest.TestCase):
    """A netting close whose answer was lost is found by its client id; not
    found, the user would be told after the 120 s watch that it was not
    carried out though it was. documented: position-ep longClientExtensions
    and shortClientExtensions are "The client extensions to add to the
    MarketOrder used to close the long [short] position"; transaction-df
    MarketOrderTransaction clientExtensions "only provided if the Order is
    being created with client extensions", and OrderFillTransaction
    clientOrderID "The client Order ID of the Order filled (only provided if
    the client has assigned one)"."""

    OCID = "brue-c-0123456789abcdef01234567"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oanda-late-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def lost_close(self, side, units, record, age_s=9.0, hedging=False,
                   qty=None):
        """A cold adapter holding a position of 1000 (`side`), whose close
        (`units`: what it asked OANDA) under client id "k" was lost `age_s`
        ago; OANDA's record then holds `record` (a list of transactions)
        after it. Returns the adapter, its output, and the close's params."""
        a, out = cold(tempfile.mkdtemp(prefix="c-", dir=self.dir),
                      hedging=hedging)
        a._bind_account(ACCT)
        a._cids["k1"] = "k1"
        a._ingest(market_fill(101, "k1", "1000" if side == "long"
                              else "-1000"))                 # trade 102
        p = {"client_id": "k", "position_id": "102" if hedging
             else "EUR_USD"}
        if qty is not None:
            p["qty"] = qty
        now = a.now_ms()
        entry = {"m": "position.close", "cid": "k", "sym": "EUR_USD",
                 "sig": protocol.order_signature("position.close", p),
                 "before": 102, "unknown": True,
                 "at": now - int(age_s * 1000) - 1000,
                 "lost_at": now - int(age_s * 1000)}
        if hedging:
            entry["trade"] = "102"
        else:
            entry["ocid"] = self.OCID
        a._journal["close:k"] = entry
        a.client.call = FakeClient({
            ("GET", "/transactions/sinceid"): {
                "transactions": record, "lastTransactionID": str(
                    max([102] + [int(t["id"]) for t in record]))},
            ("PUT", "/close"): {}})
        return a, out, p

    CASES = (("long", "ALL", None), ("long", "400", 400),
             ("short", "ALL", None), ("short", "400", 400))

    def answered(self, out, rid):
        return [m for m in lines(out) if m.get("type") == "res"
                and m.get("id") == rid][0]

    def test_a_late_netting_close_is_found_by_its_id(self):
        for side, units, qty in self.CASES:
            for marked in ("create", "fill"):
                with self.subTest(side=side, units=units, marked=marked):
                    rec = closeout(103, side, units, 1000,
                                   ocid=self.OCID if marked == "create"
                                   else None, fill_cid=self.OCID)
                    a, out, p = self.lost_close(side, units, rec, qty=qty)
                    a._mutate("r1", "position.close", p)
                    self.assertEqual(self.answered(out, "r1").get("result"),
                                     {}, lines(out))
                    closed = events(out, "position.closed")
                    self.assertEqual([c["qty"] for c in closed],
                                     [1000.0 if units == "ALL" else 400.0])
                    self.assertNotIn("close:k", a._journal)
                    self.assertFalse([c for c in a.client.call.calls
                                      if c[0] == "PUT"], "sent again")

    def test_a_close_marked_as_another_request_is_not_this_one(self):
        """A closeout of the instrument marked with another id, or of
        another instrument: not this close (inside the watch it is still
        "not known")."""
        for rec in (closeout(103, "long", "ALL", 1000, ocid="brue-c-other",
                             fill_cid="brue-c-other"),
                    closeout(103, "long", "ALL", 1000, ocid=self.OCID,
                             fill_cid=self.OCID, sym="GBP_USD")):
            with self.subTest(rec=rec[0].get("instrument")):
                a, out, p = self.lost_close("long", "ALL", rec)
                a._mutate("r1", "position.close", p)
                e = self.answered(out, "r1")["error"]
                self.assertEqual((e["code"], e["retryable"]),
                                 ("internal", True))
                if rec[0]["instrument"] == "GBP_USD":
                    self.assertIn("not in OANDA's record yet", e["message"])
                else:
                    # what the record shows of its position is
                    # said, as another request's close, never this one
                    self.assertIn("OANDA's record shows a close of EUR_USD "
                                  "at 17:32:40 UTC, marked as another "
                                  "request; the record is watched for it "
                                  "until", e["message"])
                    self.assertNotIn("may be this one", e["message"])

    def test_an_unmarked_closeout_after_the_watch_may_be_this_close(self):
        """After the watch, OANDA's record holds a POSITION_CLOSEOUT of the
        instrument since the close was sent that is not marked as it: never
        "not carried out" (it may be this one, carried out with its marks
        lost), but what the record shows, final."""
        hms = time.strftime("%H:%M:%S", time.gmtime(1794677560))
        for side, units, qty in self.CASES:
            for other in (None, "brue-f-flatten0123456789abcdef"):
                with self.subTest(side=side, units=units, other=other):
                    rec = closeout(103, side, units, 1000, ocid=other,
                                   fill_cid=other)
                    a, out, p = self.lost_close(side, units, rec,
                                                age_s=121.0, qty=qty)
                    a._mutate("r1", "position.close", p)
                    a._mutate("r2", "position.close", p)     # asked again
                    for rid in ("r1", "r2"):
                        e = self.answered(out, rid)["error"]
                        self.assertEqual(
                            (e["code"], e.get("like"), e["retryable"]),
                            ("x_oanda_close_unmarked", "internal", False))
                        self.assertNotIn("not carried out", e["message"])
                        # one marked as another request (flatten_all's
                        # here) is said so; only an unmarked one may be
                        # this one
                        self.assertIn(
                            "OANDA's record shows a close of EUR_USD at %s "
                            "UTC, %s" % (hms, "not marked as this request, so "
                                         "it may be this one" if other is None
                                         else "marked as another request"),
                            e["message"])
                        self.assertIn("check Positions before closing it "
                                      "again", e["message"])
                    self.assertNotIn("close:k", a._journal)
                    self.assertFalse([c for c in a.client.call.calls
                                      if c[0] == "PUT"], "sent again")

    def test_the_backstop_says_it_too(self):
        hms = time.strftime("%H:%M:%S", time.gmtime(1794677560))
        a, out, p = self.lost_close("long", "ALL", closeout(
            103, "long", "ALL", 1000), age_s=121.0)
        a._watch_unknown()
        a._watch_unknown()
        self.assertNotIn("close:k", a._journal)
        self.assertEqual(a._final["k"]["code"], "x_oanda_close_unmarked")
        self.assertIn("a close of EUR_USD at %s UTC" % hms,
                      a._final["k"]["message"])
        a._mutate("r9", "position.close", p)
        self.assertEqual(self.answered(out, "r9")["error"]["code"],
                         "x_oanda_close_unmarked")

    def test_with_no_close_in_the_record_it_was_not_carried_out(self):
        """Nothing of a closeout of the instrument since it was sent: as
        before, not carried out, final."""
        for rec in ([], closeout(103, "long", "ALL", 1000, sym="GBP_USD"),
                    market_fill(103, "k2", "500")):
            with self.subTest(rec=[t["type"] for t in rec]):
                a, out, p = self.lost_close("long", "ALL", rec, age_s=121.0)
                a._mutate("r1", "position.close", p)
                e = self.answered(out, "r1")["error"]
                self.assertEqual((e["code"], e["retryable"]),
                                 ("x_oanda_not_carried_out", False))

    def test_a_late_hedging_close_is_found_by_its_trade(self):
        """Hedging closes a trade (trade-ep PUT .../trades/{id}/close): its
        MARKET_ORDER names the trade (documented: transaction-df
        MarketOrderTransaction tradeClose, "only provided when the Market
        Order is being used to explicitly close a Trade"), so it is found
        by it, whole or in part, long or short. A closeout of the position
        is not a trade's close: after the watch, with none that names the
        trade, it was not carried out."""
        for side, units, qty in self.CASES:
            with self.subTest(side=side, units=units):
                rec = closeout(103, side, units, 1000, trade="102")
                a, out, p = self.lost_close(side, units, rec, hedging=True,
                                            qty=qty)
                a._mutate("r1", "position.close", p)
                self.assertEqual(self.answered(out, "r1").get("result"), {})
                self.assertEqual(
                    [c["qty"] for c in events(out, "position.closed")],
                    [1000.0 if units == "ALL" else 400.0])
            with self.subTest(side=side, units=units, after="watch"):
                # a closeout of the position in OANDA's record that did not
                # close this trade (another one's): nothing of this close
                rec = closeout(103, side, units, 1000)
                for c in rec[1].get("tradesClosed") or [rec[1].get(
                        "tradeReduced")]:
                    c["tradeID"] = "77"
                a, out, p = self.lost_close(side, units, rec, hedging=True,
                                            age_s=121.0, qty=qty)
                a._mutate("r1", "position.close", p)
                self.assertEqual(self.answered(out, "r1")["error"]["code"],
                                 "x_oanda_not_carried_out")


def level_fill(base, reason, side, units, held, trade="102", sym="EUR_USD",
               at="1794677570", order="90"):
    """A stop, target or trailing stop OANDA holds on `trade`, filling: its
    ORDER_FILL alone (the order was made when the level was set, before the
    close was sent). documented: transaction-df OrderFillReason
    STOP_LOSS_ORDER "The Order filled was a Stop Loss Order",
    TAKE_PROFIT_ORDER, TRAILING_STOP_LOSS_ORDER, GUARANTEED_STOP_LOSS_ORDER;
    tradesClosed "only provided when filling the Order closed open Trades",
    tradeReduced likewise for one it reduced."""
    take = held if units == "ALL" else int(units)
    sign = "-" if side == "long" else ""
    fill = {"id": str(base), "type": "ORDER_FILL", "batchID": str(base),
            "orderID": order, "instrument": sym, "units": sign + str(take),
            "fullVWAP": "1.09900", "reason": reason, "commission": "0",
            "time": at + ".100000000", "accountBalance": "99990.0"}
    part = {"tradeID": trade, "units": sign + str(take), "price": "1.09900",
            "realizedPL": "-10.0", "financing": "0"}
    if take >= held:
        fill["tradesClosed"] = [part]
    else:
        fill["tradeReduced"] = part
    return [fill]


class ColdLostCloseRecord(unittest.TestCase):
    """What OANDA's record says of a close whose answer was lost. The faults
    pinned here:

    (a) The record was read from the contiguous mark: with one transaction
    not seen yet before an answered close A, A's close was in a later close
    B's window, so B (lost, never carried out) was answered {} with A's
    transactions (hedging, MT4-linked), or told it may have been carried out,
    naming A (netting). (b) A hedging trade that the record shows closed
    since the request (by a closeout of its position, its stop or its
    target), and a netting position closed by a stop, a target or another
    tool's trade close, was told "not carried out". (c) A closeout OANDA
    cancelled was said as a close that may be this one. (d) Inside the
    watch an unmarked closeout already in the record was "not in OANDA's
    record yet". (e) The words named the request by the platform's internal
    client_id."""

    CID = "lset-close-9a9771e29b95"
    AT = "1794677570"
    HMS = time.strftime("%H:%M:%S", time.gmtime(1794677570))

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oanda-k19-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def table(record, put=None):
        """OANDA's record as /transactions/sinceid answers it (documented:
        transaction-ep, the transactions after the id given), and the
        close's PUT."""
        def sinceid(query, body):
            since = int(query.get("id") or 0)
            return {"transactions": [t for t in record
                                     if int(t["id"]) > since],
                    "lastTransactionID": str(max(
                        [since] + [int(t["id"]) for t in record]))}
        out = {("GET", "/transactions/sinceid"): sinceid}
        if put is not None:
            out[("PUT", "/close")] = put
        return FakeClient(out)

    @staticmethod
    def no_answer(query, body):
        raise mod.OandaError(0, "OANDA did not answer (TimeoutError)",
                             code="lost", sent=True, cause="TimeoutError")

    def adapter(self, hedging=False, mt4=False, side="long"):
        a, out = cold(tempfile.mkdtemp(prefix="c-", dir=self.dir),
                      hedging=hedging)
        a._bind_account(ACCT)
        a._mt4 = mt4
        a._cids["k1"] = "k1"
        a._ingest(market_fill(101, "k1", "1000" if side == "long"
                              else "-1000"))                 # trade 102
        return a, out

    def lost(self, record=(), hedging=False, mt4=False, side="long",
             qty=None, age_s=121.0):
        """A close under CID of the position (trade 102 on hedging), sent
        through the adapter and its answer lost; OANDA's record then holds
        `record` after it, and the loss is `age_s` old."""
        a, out = self.adapter(hedging=hedging, mt4=mt4, side=side)
        p = {"client_id": self.CID,
             "position_id": "102" if hedging else "EUR_USD"}
        if qty is not None:
            p["qty"] = qty
        later: list = []
        a.client.call = self.table(later, self.no_answer)
        a._mutate("r0", "position.close", p)
        e = self.answered(out, "r0")["error"]
        self.assertEqual((e["code"], e["retryable"]), ("internal", True))
        later.extend(record)
        j = a._journal["close:" + self.CID]
        j["lost_at"] = a.now_ms() - int(age_s * 1000)
        return a, out, p

    @staticmethod
    def answered(out, rid):
        return [m for m in lines(out) if m.get("type") == "res"
                and m.get("id") == rid][0]

    def final(self, a, out, p, rid="r1"):
        """Asked again past the watch: the final error, the same when asked
        once more, and nothing sent again."""
        a._mutate(rid, "position.close", p)
        a._mutate(rid + "b", "position.close", p)
        e, again = (self.answered(out, rid)["error"],
                    self.answered(out, rid + "b")["error"])
        self.assertEqual((again["code"], again["message"]),
                         (e["code"], e["message"]))
        self.assertFalse(e["retryable"])
        puts = [c for c in a.client.call.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1, "sent again")
        self.assertNotIn(self.CID, e["message"])
        return e

    def test_a_its_record_is_read_from_the_highest_id_seen(self):
        """(a) Close A answered with its transactions (104, 105) while 103
        is not seen yet; close B of the same position lost, and never
        carried out."""
        import hashlib
        fin = {"id": "103", "type": "DAILY_FINANCING", "batchID": "103",
               "financing": "-0.01", "accountBalance": "99999.99",
               "time": "1794677559.000000000"}
        for case in ("hedging", "mt4", "netting"):
            with self.subTest(case=case):
                a, out = self.adapter(hedging=case == "hedging",
                                      mt4=case == "mt4")
                pid = "102" if case == "hedging" else "EUR_USD"
                if case == "hedging":
                    txs = closeout(104, "long", "400", 1000, trade="102")
                    ans = {"orderCreateTransaction": txs[0],
                           "orderFillTransaction": txs[1],
                           "lastTransactionID": "105"}
                else:
                    mark = None if case == "mt4" else "brue-c-" + \
                        hashlib.sha256(b"close:A").hexdigest()[:24]
                    txs = closeout(104, "long", "400", 1000, ocid=mark,
                                   fill_cid=mark)
                    ans = {"longOrderCreateTransaction": txs[0],
                           "longOrderFillTransaction": txs[1],
                           "lastTransactionID": "105"}
                n = []

                def put(query, body):
                    n.append(1)
                    if len(n) == 1:
                        return ans
                    return self.no_answer(query, body)

                a.client.call = self.table([fin] + txs, put)
                a._mutate("rA", "position.close", {
                    "client_id": "A", "position_id": pid, "qty": 400})
                self.assertEqual(self.answered(out, "rA").get("result"), {})
                self.assertEqual((a._contig, a._high), (102, 105))
                pB = {"client_id": "B", "position_id": pid, "qty": 300}
                a._mutate("rB", "position.close", pB)
                self.assertNotIn("result", self.answered(out, "rB"),
                                 "B answered as done with A's close")
                self.assertEqual(a._journal["close:B"]["before"], 105)
                for rid in ("rB", "rB2"):
                    if rid == "rB2":
                        a._mutate("rB2", "position.close", pB)
                    m = self.answered(out, rid)
                    self.assertNotIn("result", m, "B answered as done with "
                                     "A's close")
                    self.assertEqual((m["error"]["code"],
                                      m["error"]["retryable"]),
                                     ("internal", True))
                a._journal["close:B"]["lost_at"] = a.now_ms() - 121_000
                a._mutate("rB3", "position.close", pB)
                e = self.answered(out, "rB3")["error"]
                self.assertEqual(e["code"], "x_oanda_not_carried_out",
                                 e["message"])
                self.assertEqual([c["qty"] for c in events(
                    out, "position.closed")], [400.0])

    def test_b_a_hedging_trade_closed_since_is_never_not_carried_out(self):
        """(b) The record since the close shows its trade closed, by a
        closeout of its position, its stop, its target or its trailing
        stop: what the record shows, final, never "not carried out"."""
        for side in ("long", "short"):
            for rec, said in (
                    (closeout(103, side, "ALL", 1000, at=self.AT),
                     "trade 102 closed by a close of EUR_USD at %s UTC"),
                    (closeout(103, side, "400", 1000, at=self.AT),
                     "trade 102 reduced by a close of EUR_USD at %s UTC"),
                    (level_fill(103, "STOP_LOSS_ORDER", side, "ALL", 1000),
                     "trade 102 closed by its stop loss at %s UTC"),
                    (level_fill(103, "TAKE_PROFIT_ORDER", side, "ALL", 1000),
                     "trade 102 closed by its take profit at %s UTC"),
                    (level_fill(103, "TRAILING_STOP_LOSS_ORDER", side, "ALL",
                                1000),
                     "trade 102 closed by its trailing stop at %s UTC")):
                with self.subTest(side=side, said=said):
                    a, out, p = self.lost(rec, hedging=True, side=side)
                    e = self.final(a, out, p)
                    self.assertEqual((e["code"], e.get("like")),
                                     ("x_oanda_close_unmarked", "internal"))
                    self.assertNotIn("not carried out", e["message"])
                    self.assertIn("OANDA's record shows " + said % self.HMS,
                                  e["message"])
                    self.assertIn("check Positions before closing it again",
                                  e["message"])
                    self.assertNotIn("may be this one", e["message"])
        # and the backstop says the same, once
        a, out, p = self.lost(level_fill(103, "STOP_LOSS_ORDER", "long",
                                         "ALL", 1000), hedging=True)
        a._watch_unknown()
        a._watch_unknown()
        self.assertEqual(a._final[self.CID]["code"], "x_oanda_close_unmarked")
        self.assertIn("trade 102 closed by its stop loss",
                      a._final[self.CID]["message"])

    def test_b_a_netting_position_closed_by_a_level_or_a_trade_close(self):
        """(b) netting: the record shows the position closed by its stop,
        its target, or another tool's close of its trade (TRADE_CLOSE):
        what the record shows, final, never "not carried out"."""
        for side in ("long", "short"):
            for rec, said in (
                    (level_fill(103, "STOP_LOSS_ORDER", side, "ALL", 1000),
                     "a stop loss of EUR_USD filled at %s UTC"),
                    (level_fill(103, "TAKE_PROFIT_ORDER", side, "400", 1000),
                     "a take profit of EUR_USD filled at %s UTC"),
                    (closeout(103, side, "ALL", 1000, trade="102",
                              at=self.AT),
                     "a close of EUR_USD's trade 102 at %s UTC, not marked "
                     "as this request")):
                for mt4 in (False, True):
                    with self.subTest(side=side, said=said, mt4=mt4):
                        a, out, p = self.lost(rec, side=side, mt4=mt4)
                        e = self.final(a, out, p)
                        self.assertEqual(e["code"], "x_oanda_close_unmarked")
                        self.assertNotIn("not carried out", e["message"])
                        self.assertIn("OANDA's record shows "
                                      + said % self.HMS, e["message"])
                        self.assertNotIn("may be this one", e["message"])

    def test_c_a_closeout_oanda_cancelled_is_never_this_one(self):
        """(c) An unmarked closeout of the instrument that OANDA cancelled
        (its ORDER_CANCEL: MARKET_HALTED, INSUFFICIENT_LIQUIDITY,
        POSITION_CLOSEOUT_FAILED) closed nothing: inside the watch still not
        in the record, after it not carried out."""
        for reason in ("MARKET_HALTED", "INSUFFICIENT_LIQUIDITY",
                       "POSITION_CLOSEOUT_FAILED"):
            create = closeout(103, "long", "ALL", 1000, at=self.AT)[0]
            cancel = {"id": "104", "type": "ORDER_CANCEL", "batchID": "103",
                      "orderID": "103", "reason": reason,
                      "time": self.AT + ".100000000"}
            for hedging in (False, True):
                with self.subTest(reason=reason, hedging=hedging):
                    a, out, p = self.lost([create, cancel], hedging=hedging,
                                          age_s=9.0)
                    a._mutate("r1", "position.close", p)
                    e = self.answered(out, "r1")["error"]
                    self.assertEqual((e["code"], e["retryable"]),
                                     ("internal", True))
                    self.assertIn("not in OANDA's record yet", e["message"])
                    a._journal["close:" + self.CID]["lost_at"] = \
                        a.now_ms() - 121_000
                    e = self.final(a, out, p, rid="r2")
                    self.assertEqual(e["code"], "x_oanda_not_carried_out",
                                     e["message"])
                    self.assertNotIn("may be this one", e["message"])

    def test_d_inside_the_watch_a_closeout_in_the_record_is_said(self):
        """(d) Asked again inside the watch, a close whose unmarked closeout
        is already in OANDA's record says so, and that it is still watched:
        not "not in OANDA's record yet"."""
        rec = closeout(103, "long", "ALL", 1000, at=self.AT)
        a, out, p = self.lost(rec, age_s=9.0)
        a._mutate("r1", "position.close", p)
        e = self.answered(out, "r1")["error"]
        self.assertEqual((e["code"], e["retryable"]), ("internal", True))
        self.assertTrue(e["message"].startswith("Not known yet whether OANDA "
                                                "carried out"), e["message"])
        self.assertNotIn("not in OANDA's record yet", e["message"])
        self.assertIn("OANDA's record shows a close of EUR_USD at %s UTC, "
                      "not marked as this request, so it may be this one"
                      % self.HMS, e["message"])
        lost = int(a._journal["close:" + self.CID]["lost_at"])
        self.assertIn("watched for it until %s UTC" % time.strftime(
            "%H:%M:%S", time.gmtime((lost + 120_000) / 1000)), e["message"])
        self.assertFalse([c for c in a.client.call.calls[1:]
                          if c[0] == "PUT"], "sent again")

    def test_e_the_words_name_the_request_by_what_it_does(self):
        """(e) The final words, and the session line, name the request by
        what it does and when it was sent, never by the platform's own
        client_id (not 'The close lset-close-9a9771e29b95 of EUR_USD may
        have been carried out')."""
        hms = lambda ms: time.strftime("%H:%M:%S", time.gmtime(ms / 1000))
        for qty, what in ((1000, "The close of 1000 EUR_USD"),
                          (400, "The close of 400 EUR_USD"),
                          (None, "The close of EUR_USD")):
            for rec in ((), closeout(103, "long", "ALL", 1000, at=self.AT)):
                with self.subTest(qty=qty, unmarked=bool(rec)):
                    a, out, p = self.lost(rec, qty=qty)
                    sent = int(a._journal["close:" + self.CID]["at"])
                    line = events(out, "session.state")[-1]["detail"]
                    self.assertNotIn(self.CID, line)
                    self.assertIn("t" + what[1:] + " was sent at %s UTC"
                                  % hms(sent), line)
                    e = self.final(a, out, p)
                    self.assertTrue(e["message"].startswith(
                        "%s sent at %s UTC " % (what, hms(sent))),
                        e["message"])
        # a hedging close names its trade; an order, what it buys or sells
        a, out, p = self.lost(hedging=True, qty=400)
        e = self.final(a, out, p)
        self.assertTrue(e["message"].startswith(
            "The close of 400 EUR_USD of trade 102 sent at "), e["message"])
        a, out = self.adapter()
        a.client.call = FakeClient({
            ("POST", "/orders"): self.no_answer,
            ("GET", "/transactions/sinceid"): {"transactions": [],
                                               "lastTransactionID": "102"}})
        q = {"client_id": "lset-order-0123456789ab", "symbol": "EUR_USD",
             "side": "sell", "qty": 500}
        a._mutate("o1", "order.place", q)
        a._journal["place:lset-order-0123456789ab"]["lost_at"] = \
            a.now_ms() - 121_000
        a._mutate("o2", "order.place", q)
        e = self.answered(out, "o2")["error"]
        self.assertEqual(e["code"], "x_oanda_not_carried_out")
        self.assertTrue(e["message"].startswith(
            "The order to sell 500 EUR_USD sent at "), e["message"])
        self.assertNotIn("lset-order", e["message"])
        self.assertIn("send it as a new order", e["message"])

    def test_f_the_docstrings_name_both_final_answers(self):
        """(f) The module's docstring and _replay_unknown's named only
        x_oanda_not_carried_out as the answer after the watch."""
        for doc in (mod.__doc__, mod.OandaAdapter._replay_unknown.__doc__):
            self.assertIn("x_oanda_not_carried_out", doc)
            self.assertIn("x_oanda_close_unmarked", doc)


class ColdWords(unittest.TestCase):
    """The words of OANDA's refusals, cold: a client aimed at this machine,
    port 9, with only its connection stubbed."""

    TOKEN = ("0123456789abcdef0123456789abcdef-"
             "fedcba9876543210fedcba9876543210")

    def client(self):
        return mod.OandaClient(mod.resolve_environment(
            ["--paper"], {"BRUE_CRED_TOKEN": self.TOKEN,
                          "OANDA_REST_URL": "http://127.0.0.1:9",
                          "OANDA_STREAM_URL": "http://127.0.0.1:9"}))

    def assertNoPartOfTheToken(self, text):
        for i in range(len(self.TOKEN) - 7):
            self.assertNotIn(self.TOKEN[i:i + 8], text)

    def test_a_token_escaped_in_a_page_is_hidden_before_it_is_cut(self):
        """A page unescaped after the first scrub and cut at 80 before the
        second keeps part of the token: a title with the token written as
        HTML escapes, 41 characters in, kept its first 37 characters in the
        message. Unescaped first, then scrubbed, then cut."""
        c = self.client()
        esc = "".join("&#%d;" % ord(ch) for ch in self.TOKEN)
        for lead in (0, 10, 40, 41, 50, 79, 100):
            for title in ("%s %s" % ("x" * lead, esc),
                          "%s %s" % ("x" * lead, self.TOKEN),
                          "%s %s tail" % ("x" * lead, esc.replace(
                              "&#48;", "&#x30;"))):
                with self.subTest(lead=lead, title=title[:60]):
                    page = ("<html><head><title>%s</title></head>"
                            "<body>%s</body></html>" % (title, esc)).encode()
                    e = c._error(502, page)
                    err = mod.to_adapter_error(e, "practice", "127.0.0.1")
                    for text in (e.message, str(err)):
                        self.assertNoPartOfTheToken(text)
                        self.assertLessEqual(len(e.message), 80)
        # a page with no title: its text, unescaped, scrubbed, then cut
        e = c._error(503, ("<p>%s %s</p>" % ("y" * 45, esc)).encode())
        self.assertNoPartOfTheToken(e.message)
        self.assertIn("[hidden]", e.message)

    # What a page may put inside a secret that a reader does not see, or
    # reads past: a tag, a soft hyphen, a character that prints as nothing.
    SPLITS = ("<wbr>", "<b>", "</b><i>", "<!-- -->", "&lt;wbr&gt;", "&shy;",
              "&#173;", "\xad", "\u200b", "&#8203;", "&ZeroWidthSpace;",
              "&zwnj;", "&zwj;", "\u2060", "\ufeff", "\u200e")

    @staticmethod
    def split(secret, by, at=(0.5,)):
        """`secret` with `by` written into it at each fraction `at`."""
        cuts = sorted({max(1, min(len(secret) - 1, int(len(secret) * f)))
                       for f in at})
        out, last = "", 0
        for k in cuts:
            out += secret[last:k] + by
            last = k
        return out + secret[last:]

    def test_a_token_split_in_a_page_is_hidden(self):
        """A page that splits the token with a tag (<wbr>, <b>), a soft
        hyphen or a character that prints as nothing does not put it in the
        words in readable pieces, and no < or > of the tags inside a <title>
        reaches the message. The title's text is taken without its tags,
        what prints as nothing is dropped, then the token is taken out (also
        where white space splits it), then the words are cut."""
        c = self.client()
        tok = self.TOKEN
        for by in self.SPLITS:
            for at in ((0.5,), (1 / 3,), (0.25, 0.75), (0.01,), (0.99,)):
                written = self.split(tok, by, at)
                for lead in (0, 30):
                    for where in ("title", "body"):
                        with self.subTest(by=by, at=at, lead=lead,
                                          where=where):
                            page = ("<html><head><title>%s %s</title></head>"
                                    "<body>z</body></html>" if where ==
                                    "title" else "<p>%s %s</p>") % (
                                        "z" * lead, written)
                            e = c._error(502, page.encode())
                            err = mod.to_adapter_error(e, "practice",
                                                       "127.0.0.1")
                            for text in (e.message, str(err)):
                                for i in range(len(tok) - 5):
                                    self.assertNotIn(tok[i:i + 6], text)
                                self.assertNotRegex(text, r"[<>]")
                            self.assertIn("[hidden]", e.message)
        # and a gateway's own words are still its words
        for status, page, words in (
                (502, b"<title>502 Bad<b> Gateway</b></title>", "Bad Gateway"),
                (503, b"<title>503 Service &lt;i&gt;Unavailable</title>",
                 "Service Unavailable"),
                (502, b"<title>Bad\xc2\xad Gate\xe2\x80\x8bway</title>",
                 "Bad Gateway")):
            with self.subTest(page=page):
                self.assertEqual(c._error(status, page).message, words)

    def test_a_reason_is_one_line(self):
        """`reason` (a reject transaction's rejectReason, else errorCode)
        is not kept as OANDA's answer has it for to_adapter_error to add:
        "OANDA: x (BAD\\r\\nREASON)"."""
        c = self.client()
        for raw in (b'{"orderRejectTransaction": {"rejectReason": '
                    b'"BAD\\r\\nREASON"}, "errorMessage": "x"}',
                    b'{"errorCode": "CODE\\nTWO", "errorMessage": "x"}',
                    b'{"errorCode": "A\\u2028B\\u0007C\\tD", '
                    b'"errorMessage": "x"}',
                    ('{"errorCode": "%s\\n", "errorMessage": "x"}'
                     % self.TOKEN).encode()):
            with self.subTest(raw=raw[:50]):
                e = c._error(400, raw)
                err = mod.to_adapter_error(e, "practice", "127.0.0.1")
                for text in (e.reason, str(err)):
                    self.assertNotRegex(text, "[\x00-\x1f\x7f  ]")
                    self.assertNoPartOfTheToken(text)
        # a reason as OANDA documents it is kept as it is, and still read
        e = c._error(400, b'{"orderRejectTransaction": {"rejectReason": '
                          b'"INSUFFICIENT_MARGIN"}, "errorMessage": "m"}')
        self.assertEqual(e.reason, "INSUFFICIENT_MARGIN")
        self.assertEqual(mod.to_adapter_error(e).code,
                         mod.map_reason("INSUFFICIENT_MARGIN")[0])

    def test_a_read_whose_answer_is_not_json_says_it_could_not_be_read(self):
        """A read answered 2xx with a body that is not JSON does not say
        "OANDA could not be reached: OANDA's answer was not JSON (17
        bytes)" when OANDA has answered. Like the writes, it says that the
        answer could not be read."""
        c = self.client()

        class Resp:
            will_close, status = True, 200

            def read(self):
                return b"<html>oops</html>"

        class Conn:
            sock, timeout = None, None

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return Resp()

            def close(self):
                pass

        c.rest.take = lambda tmo, until, max_idle=None: (Conn(), True)
        with self.assertRaises(mod.OandaError) as cm:
            c.call("GET", "/v3/accounts/%s/summary" % ACCT)
        err = mod.to_adapter_error(cm.exception, "practice", "127.0.0.1")
        self.assertEqual((err.code, err.retryable), ("internal", True))
        self.assertNotIn("could not be reached", str(err))
        self.assertIn("its answer could not be read (not JSON, 17 bytes)",
                      str(err))
        # a read that got no answer at all still says so
        e = mod.OandaError(0, "OANDA did not answer (TimeoutError)",
                           code="lost", sent=True, cause="TimeoutError")
        self.assertIn("could not be reached", str(mod.to_adapter_error(e)))


class ColdScrubGaps(unittest.TestCase):
    """The secret scrub, in the cases where a scrub leaves a secret readable
    or runs slow.

    (a) a control character _one_line turns into a space but white space
    does not (NUL, DEL, the C0 and C1 controls), raw or (through a page)
    written as a character reference, is a gap in _hide, so the pieces do
    not reach the words as spaces;
    (b) a character that prints as nothing but is not category Cf (a
    Default_Ignorable_Code_Point: U+034F, the Hangul fillers, the variation
    selectors, the tag characters) does not leave the secret whole;
    (c) markup whose own text holds '>' (a comment '<!-- > -->', a quoted
    attribute title='>') is not ended at that first '>', so the secret does
    not reach the words in readable pieces;
    (d) a secret that itself holds '<' is not cut up by the tag removal,
    which runs after the scrub;
    (e) the gap match does not backtrack quadratically on a secret with a
    space and a page with a long white-space run (6 s at 64 KiB when it
    does)."""

    TOKEN = ColdWords.TOKEN
    split = staticmethod(ColdWords.split)

    def client(self, token=None):
        return mod.OandaClient(mod.resolve_environment(
            ["--paper"], {"BRUE_CRED_TOKEN": token or self.TOKEN,
                          "OANDA_REST_URL": "http://127.0.0.1:9",
                          "OANDA_STREAM_URL": "http://127.0.0.1:9"}))

    def assertNoPiece(self, secret, text, n=6):
        for i in range(len(secret) - (n - 1)):
            self.assertNotIn(secret[i:i + n], text, repr(text))

    # Control characters _one_line maps to a space that white space (\s,
    # already a gap) does not include: NUL and the other C0 controls, DEL
    # and the C1 controls.
    CONTROLS = ("\x00", "\x01", "\x08", "\x0e", "\x1b", "\x7f", "\x81",
                "\x90", "\x9f")

    def test_a_a_control_character_that_is_not_white_space_is_a_gap(self):
        tok = self.TOKEN
        for ch in self.CONTROLS:
            written = self.split(tok, ch, (0.3, 0.6))
            with self.subTest(ch=hex(ord(ch)), via="page"):
                e = self.client()._error(502, ("<p>x %s</p>"
                                               % written).encode())
                self.assertNoPiece(tok, e.message)
                self.assertIn("[hidden]", e.message)
            with self.subTest(ch=hex(ord(ch)), via="json"):
                raw = json.dumps({"errorMessage": "x " + written}).encode()
                e = self.client()._error(400, raw)
                self.assertNoPiece(tok, e.message)
                self.assertIn("[hidden]", e.message)
        # written as a character reference in a page: unescaped before the
        # scrub, so it becomes the control character and is a gap
        for code in (1, 8, 127, 129, 0x90):
            for ref in ("&#%d;" % code, "&#x%x;" % code):
                written = self.split(tok, ref, (0.3, 0.6))
                with self.subTest(ref=ref):
                    e = self.client()._error(
                        502, ("<title>x %s</title>" % written).encode())
                    self.assertNoPiece(tok, e.message)
                    self.assertIn("[hidden]", e.message)

    # Default_Ignorable_Code_Point characters that are not category Cf.
    IGNORABLE = ("͏", "ᅟ", "ᅠ", "឴", "឵", "᠋",
                 "᠏", "ㅤ", "︀", "️", "ﾠ",
                 "\U000e0100", "\U000e01ef")

    def test_b_default_ignorable_characters_are_dropped(self):
        tok = self.TOKEN
        for ch in self.IGNORABLE:
            self.assertNotEqual(mod.unicodedata.category(ch), "Cf", hex(
                ord(ch)))
            written = self.split(tok, ch, (0.3, 0.6))
            with self.subTest(ch=hex(ord(ch)), via="page"):
                e = self.client()._error(502, ("<p>x %s</p>"
                                               % written).encode())
                self.assertNoPiece(tok, e.message)
                self.assertIn("[hidden]", e.message)
            with self.subTest(ch=hex(ord(ch)), via="json"):
                raw = json.dumps({"errorMessage": "x " + written}).encode()
                e = self.client()._error(400, raw)
                self.assertNoPiece(tok, e.message)
        self.assertEqual(mod._visible("a͏b︀c\U000e0100dㅤe"),
                         "abcde")

    def test_c_markup_whose_text_holds_a_gt_is_removed_whole(self):
        tok = self.TOKEN
        for by in ("<!-- > -->", "<!-- a > b -->", '<a title=">">',
                   '<b title="x>y">', "<!--\n>\n-->"):
            written = self.split(tok, by, (0.5,))
            for where in ("title", "body"):
                page = ("<html><head><title>x %s</title></head></html>"
                        if where == "title" else "<p>x %s</p>") % written
                with self.subTest(by=by, where=where):
                    e = self.client()._error(502, page.encode())
                    self.assertNoPiece(tok, e.message)
                    self.assertNotRegex(e.message, r"[<>]")
                    self.assertIn("[hidden]", e.message)

    def test_d_a_secret_that_holds_lt_is_scrubbed_before_tags_are_removed(
            self):
        # OANDA's token is hex and a dash, but the scrub hides whatever the
        # token is: a crafted one holding '<' (and '>') is still taken out.
        for secret in ("abc<def<ghij", "a<b>cdefghij", "x<<<<yzabcd",
                       "key<tag>value<end"):
            c = self.client(token=secret)
            for where in ("title", "body"):
                for form in (secret, "pre " + secret + " post"):
                    page = ("<title>%s</title>" if where == "title"
                            else "<p>%s</p>") % form
                    with self.subTest(secret=secret, where=where, form=form):
                        e = c._error(502, page.encode())
                        self.assertNoPiece(secret, e.message, 4)
                        self.assertNotRegex(e.message, r"[<>]")

    def test_e_the_gap_match_is_linear(self):
        # A secret with a space, and a 64 KiB page with a long white-space
        # run: a pattern that backtracks quadratically takes 6.1 s on it.
        secret = "pass word with spaces in it here and there"
        for page in ("head " + " " * 65536 + "tail",
                     secret[:5] + " " * 65536,
                     " " * 65536 + secret):
            start = time.monotonic()
            mod._hide(page, (secret,))
            with self.subTest(page=page[:12]):
                self.assertLess(time.monotonic() - start, 0.1)
        # and through _page_words, as a reflected page reaches it
        start = time.monotonic()
        mod._page_words("<p>pass" + " " * 65536 + "y</p>", 502,
                        scrub=lambda t: mod._hide(t, (secret,)))
        self.assertLess(time.monotonic() - start, 0.1)


class ColdStateFile(unittest.TestCase):
    """Cold: two adapters of one profile on one state directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oanda-fix-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def lister_table(self):
        return FakeClient({
            ("GET", "/v3/accounts"): {"accounts": [{"id": ACCT, "tags": []}]},
            ("GET", "/summary"): {"account": {"currency": "GBP",
                                              "hedgingEnabled": False,
                                              "alias": "Primary",
                                              "balance": "100000"}}})

    def test_a_listing_never_undoes_what_the_trading_process_wrote(self):
        a, _ = cold(self.dir)
        a._bind_account(ACCT)
        b, _ = cold(self.dir)               # started now: loaded the file
        b._acct = None
        b.client.call = self.lister_table()
        a.idempotent_store("x1", {"order_id": "7", "status": "accepted"},
                           "sig-1")
        a._journal_put("place:x2", {"m": "order.place", "cid": "x2"})
        a._journal_drop("place:x2")
        a._save_state()
        b.handle_accounts_list({})
        c, _ = cold(self.dir)               # a restart of the trading one
        c._bind_account(ACCT)
        self.assertEqual(c.idempotent_check("x1", "sig-1"),
                         {"order_id": "7", "status": "accepted"})
        self.assertNotIn("place:x2", c._journal)
        self.assertEqual(c._cached_account(ACCT)["currency"], "GBP")
        # and the trading process's next save keeps the listing's accounts
        a.idempotent_store("x3", {"order_id": "9", "status": "accepted"},
                           "sig-3")
        d, _ = cold(self.dir)
        d._bind_account(ACCT)
        self.assertIsNotNone(d.idempotent_check("x3", "sig-3"))
        self.assertEqual(d._cached_account(ACCT)["alias"], "Primary")

    def test_each_account_keeps_its_own_memory(self):
        a, _ = cold(self.dir)
        a._bind_account(ACCT)
        a.idempotent_store("y1", {"order_id": "1", "status": "accepted"}, "s")
        other = "101-004-1-002"
        b, _ = cold(self.dir)
        b._bind_account(other)
        self.assertIsNone(b.idempotent_check("y1", "s"))
        b.idempotent_store("y2", {"order_id": "2", "status": "accepted"}, "s")
        c, _ = cold(self.dir)
        c._bind_account(ACCT)
        self.assertIsNotNone(c.idempotent_check("y1", "s"))
        self.assertIsNone(c.idempotent_check("y2", "s"))

    def test_a_version_1_file_is_read(self):
        with open(os.path.join(self.dir, "oanda_state.json"), "w") as fh:
            json.dump({"version": 1, "account": ACCT,
                       "accounts": {ACCT: {"currency": "USD",
                                           "hedging": True}},
                       "results": {"v1": {"order_id": "3",
                                          "status": "accepted",
                                          "_sig": "s"}},
                       "levels": {"EUR_USD": {"sl": "1.09"}}}, fh)
        a, _ = cold(self.dir)
        # the one account remembered decides the cold hello
        self.assertEqual((a.account_model, a.account_currency),
                         ("hedging", "USD"))
        a._bind_account(ACCT)
        self.assertIsNotNone(a.idempotent_check("v1", "s"))
        self.assertEqual(a._levels["EUR_USD"]["sl"], "1.09")
        a._save_state()
        with open(os.path.join(self.dir, "oanda_state.json")) as fh:
            st = json.load(fh)
        self.assertEqual(st["version"], 2)
        self.assertIn("v1", st["books"][ACCT]["results"])


if __name__ == "__main__":
    unittest.main()
