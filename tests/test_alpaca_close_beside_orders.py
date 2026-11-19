"""Alpaca adapter: a close or a flatten_all beside other orders, one class
an item.

Run as tests/test_alpaca_unanswered.py is: the REAL adapter against the
stand-in in this process (the broker's own book beside what the adapter
told the platform). Every slow POST, every 504 and every late request
here is the test's own fault injection: Alpaca has not been seen to
carry a request out late, nor timed answering a POST slowly. The faults
pinned here:

  - flatten_all answered {"closed": 1} with ten shares held: an
    order.place whose POST took 3 s was answered while a pass's
    positions read, made before the order, was on its way back; the
    orders lane was free when flatten looked. A slow POST needs no
    5xx and is never "unknown".
    -> a pass whose reads began while an order request was on its
       way (the orders lane busy, or a POST of _post_order not
       ended: a call's, the protection's own stop or target, one
       begun before flatten_all), or beside which one began, does
       not judge "flat": the account is read again.

  - close X1 answered 504 and carried out 3 s later; close X2's
    request under X1's id (one closing order a symbol) refused by
    Alpaca as a duplicate. X1 retried was told "not known ... neither
    was answered ... send it again under a NEW client_id": X2's WAS
    answered, by a refusal that proves it made nothing.
    -> a call every request of which under the id Alpaca refused (a
       duplicate, or nothing left to close) is no suspect.

  - position.close without qty beside the user's own resting SELL 4
    of a long 10 sold 6 and answered ok.
    -> no qty is the whole position: x_alpaca_qty_held, as for a qty
       of all of it, naming the orders that hold the rest; nothing
       closed. The adapter's own stop and target are no such orders.

  - flatten_all's first close of a symbol was sized from its first
    read, made before an order of unknown fate (SELL 4 of a long 10,
    answered 504) landed: on the stand-in's default for a marked
    close for more than is held, short 4 for a moment.
    -> a symbol with an order of unknown fate, on the wire, or late
       is closed in the loop, from a read made after the cancel-all
       (not for a closing order of the symbol: flatten's close goes
       out under its id, see KeptAFewSeconds in
       tests/test_alpaca_closing_orders.py).

  - the clock cache compared Alpaca's next close with this machine's
    clock: one behind Alpaca's took a share as open after the close.
    -> compared with Alpaca's time (clock_skew_ms), and read again in
       the minute before the boundary.

  - the platform's own order.cancel of a late order taken by Alpaca
    first, then _late's DELETE answered 204 as well: the retry was
    told "this adapter cancelled it".
    -> once the platform's cancel was taken, _late claims nothing.
"""
import email.utils
import io
import shutil
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from brueconnect.adapter import AdapterError
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import fake_alpaca
from tests import test_alpaca_faults as faults
from tests import test_alpaca_orders as orders
from tests import test_alpaca_changed_meanwhile as changed_meanwhile
from tests import test_alpaca_late_watch as late_watch
from tests import test_alpaca_closing_orders as closing_orders

until = changed_meanwhile.until
Gate = closing_orders.Gate


def positions_read(method, path, q, data):
    return method == "GET" and path == "/v2/positions"


def stop_post(method, path, q, data):
    return (method == "POST" and path == "/v2/orders"
            and b'"brue-sl-' in (data or b""))


class SlowOrderAndFlatten(closing_orders.ClosingOrders):
    """MSFT 5 held. The POST of an order is held at the stand-in (not
    carried out) until flatten_all's wait loop makes a positions read
    with nothing in it; that read is made, then its answer is held 0.4 s
    while the POST is let through (carried out, filled, answered at
    once)."""

    def hold_until_flat_read(self, r, gate, match, gone=("MSFT", "AAPL")):
        """`match`: the POST held. The positions read let through is the
        one of a pass of flatten_all's wait loop (its open orders read,
        `resting()`, comes first and arms it; the adapter's other reads
        of the positions, after a fill, do not)."""
        go, armed = threading.Event(), threading.Event()
        log = []

        def post(carry):
            log.append("post reached")
            go.wait(7.0)
            log.append("post let through")
            return carry()

        def open_orders(carry):
            out = carry()
            if not go.is_set() and all(r.held(s) == 0 for s in gone):
                armed.set()
            return out

        def positions(carry):
            out = carry()
            if armed.is_set() and not go.is_set():
                log.append("flat read made")
                go.set()
                time.sleep(0.4)
            return out
        gate.on(match, post)
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/orders" and q.get("status") == "open"
                and str(q.get("limit")) == "500", open_orders)
        gate.on(positions_read, positions)
        return go, log

    def slow_place(self, argv=None):
        r = self.start(argv=argv, syms=(("MSFT", 5),))
        gate = Gate(r)
        go, log = self.hold_until_flat_read(r, gate, Gate.order("AAPL"))
        rid_p = r.send("order.place", {"client_id": r.cid("P"),
                                       "symbol": "AAPL", "side": "buy",
                                       "qty": 10})
        time.sleep(0.3)
        rid_f = r.send("flatten_all", {"client_id": r.cid("F")})
        msg = r.answer(rid_f, timeout=14)
        held = self.now_held(r)               # at the moment of the answer
        place = r.answer(rid_p, timeout=14)
        self.assertIn("flat read made", log)  # the case was made
        self.assertIn("result", place, place)  # the order WAS placed
        if "result" in msg:
            self.assertEqual({k: v for k, v in held.items() if v},
                             {}, "flatten_all answered %s with %s held"
                             % (msg["result"], held))
        else:
            self.assertTrue(msg["error"].get("retryable"), msg)
        low = self.lowest(r, 2.0)
        return r, msg, low

    def now_held(self, r):
        b = r.fake.broker
        with b.lock:
            return {k: float(v[1]) for k, v in b.positions.items()}

    def test_a_slow_order_answered_during_a_pass_is_read_again(self):
        r, msg, low = self.slow_place()
        self.assertEqual(msg.get("result"), {"closed": 2}, msg)
        self.once(r, low)                     # bought 10, sold 10, once

    def test_the_same_on_a_live_account(self):
        r, msg, low = self.slow_place(argv=["--live"])
        self.assertEqual(msg.get("result"), {"closed": 2}, msg)
        self.once(r, low)

    def stop_on_the_wire(self, r, gate, px):
        """The protection's own repair step, as its thread runs one
        (run_due: the busy lock held for the step): a stop for AAPL 10
        whose POST is slow. flatten_all's stand() waits two seconds for
        the step and goes on without it."""
        done = {}

        def step():
            with r.a.prot.busy:
                try:
                    r.a.prot._place("AAPL", True, Decimal(10),
                                    round(px - 30, 2), None)
                    done["ok"] = True
                except Exception as e:  # noqa: BLE001 - shown below
                    done["err"] = e
        t = threading.Thread(target=step, daemon=True)
        t.start()
        self.assertTrue(until(lambda: gate.seen and any(
            stop_post(m, p, q, d) for _, m, p, q, d in gate.seen), 3))
        return t, done

    def test_a_stop_that_lands_during_a_pass_is_cancelled_before_flat(self):
        r = self.start(syms=(("AAPL", 10),))
        px = r.fake.state()["prices"]["AAPL"]
        gate = Gate(r)
        go, log = self.hold_until_flat_read(r, gate, stop_post,
                                            gone=("AAPL",))
        t, done = self.stop_on_the_wire(r, gate, px)
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        rests = [o for o in r.live() if o["sym"] == "AAPL"]
        t.join(8)
        self.assertIn("flat read made", log)
        self.assertTrue(done.get("ok"), done)       # the stop WAS placed
        self.assertEqual((kind, out), ("ok", {"closed": 1}))
        # no sell stop left resting on the flat account it answered for
        self.assertEqual(rests, [], rests)
        self.assertEqual(r.held("AAPL"), 0.0)

    def test_a_stop_still_on_its_way_at_the_end_is_no_flat(self):
        # The stop's POST is not let through before flatten_all answers.
        r = self.start(syms=(("AAPL", 10),))
        px = r.fake.state()["prices"]["AAPL"]
        gate = Gate(r)
        go = threading.Event()
        gate.on(stop_post, lambda carry: (go.wait(12.0), carry())[1])
        t, done = self.stop_on_the_wire(r, gate, px)
        try:
            kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
            pending = not go.is_set()
        finally:
            go.set()
        t.join(10)
        self.assertTrue(pending)
        self.assertEqual(kind, "err", out)
        self.assertTrue(out["retryable"], out)
        self.assertIn("not known to be flat", out["message"])
        self.assertIn("AAPL", out["message"])


class RefusedAsADuplicate(closing_orders.ClosingOrders):
    """AAPL held. X1 closes part and is answered 504; X2 (another client
    id) goes out under X1's id, and X1's own request reaches Alpaca first
    (the test lands it just before X2's): X2's is refused."""

    def close(self, r, cid, qty=None):
        p = {"client_id": cid, "position_id": "AAPL"}
        if qty is not None:
            p["qty"] = qty
        return r.req("position.close", p)

    def x1_lands_first(self, held, qty):
        r = self.start(syms=(("AAPL", held),))
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))          # X1: 504, held
        gate.on(Gate.order("AAPL"),
                lambda carry: (gate.land(slot), carry())[1])
        x1, x2 = r.cid("X1"), r.cid("X2")
        kind, out = self.close(r, x1, qty)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("HTTP 504", out["message"])
        kind, out = self.close(r, x2, qty)            # X1 lands, X2 refused
        self.assertEqual((kind, out.get("code")),
                         ("err", "x_alpaca_close_pending"), out)
        self.assertTrue(slot["landed"])
        self.assertEqual(r.held("AAPL"), float(held - qty))
        return r, x1, x2

    def sells(self, r):
        return sorted((c[:2], q) for c, side, q in self.filled(r)
                      if side == "sell")

    def test_x1_retried_is_answered_from_its_own_order(self):
        r, x1, x2 = self.x1_lands_first(15, 5)
        self.assertEqual(self.close(r, x2, 5), ("ok", {}))   # X2: its own 5
        self.assertTrue(until(lambda: r.held("AAPL") == 5.0, 4))
        self.assertEqual(self.close(r, x1, 5), ("ok", {}))
        r.restart()
        self.assertEqual(self.close(r, x1, 5), ("ok", {}))   # and after
        self.assertEqual(r.held("AAPL"), 5.0)
        self.assertEqual(self.sells(r), [("X1", 5.0), ("X2", 5.0)])

    def test_nothing_left_to_close_is_the_same_proof(self):
        # X1 closes ALL of it; X2's request under X1's id then finds
        # nothing to close: refused, as Alpaca was seen to.
        r, x1, x2 = self.x1_lands_first(10, 10)
        self.assertEqual(self.close(r, x1, 10), ("ok", {}))
        self.assertEqual(r.held("AAPL"), 0.0)
        self.assertEqual(self.sells(r), [("X1", 10.0)])


class WholeCloseHeld(closing_orders.ClosingOrders):
    """A long position beside the user's own resting SELL limit far above,
    which holds part of it. position.close without qty (the whole
    position)."""

    def rest_beside(self, r, sym, qty, mult):
        px = r.fake.state()["prices"][sym]
        cid = r.cid("L")
        kind, out = r.req("order.place", {
            "client_id": cid, "symbol": sym, "side": "sell", "qty": qty,
            "type": "limit", "price": round(px * mult, 2)})
        self.assertEqual(kind, "ok", out)
        self.assertTrue(until(lambda: [o for o in r.live(sym)
                                       if o["coid"] == cid], 4))
        time.sleep(0.5)
        return cid, out["order_id"]

    def refused(self, r, sym, cid, oid, held, free):
        kind, out = r.req("position.close", {"client_id": r.cid("X"),
                                             "position_id": sym})
        self.assertEqual(kind, "err", out)
        self.assertEqual((out["code"], out["like"], out["retryable"]),
                         ("x_alpaca_qty_held", "invalid_qty", False), out)
        for word in ("WHOLE position", free + " of the " + sym, cid, oid,
                     "Nothing was closed"):
            self.assertIn(word, out["message"])
        time.sleep(1.0)
        key = sym.replace("/", "")
        self.assertEqual(r.held(sym), held)              # nothing closed
        self.assertEqual([o["coid"] for o in r.live(sym)], [cid])
        self.assertEqual([f for f in self.filled(r, key)
                          if f[1] == "sell"], [])
        return out

    def test_a_share(self):
        r = self.start(syms=(("AAPL", 10),))
        cid, oid = self.rest_beside(r, "AAPL", 4, 1.2)
        self.refused(r, "AAPL", cid, oid, 10.0, "6")

    def test_a_share_on_a_live_account(self):
        r = self.start(argv=["--live"], syms=(("AAPL", 10),))
        cid, oid = self.rest_beside(r, "AAPL", 4, 1.2)
        self.refused(r, "AAPL", cid, oid, 10.0, "6")

    def test_crypto(self):
        r = self.start(syms=(("BTC/USD", 0.002),))
        held = r.held("BTC/USD")
        cid, oid = self.rest_beside(r, "BTC/USD", 0.001, 1.5)
        self.refused(r, "BTC/USD", cid, oid, held,
                     mod._fixed(Decimal(str(held)) - Decimal("0.001")))

    def test_its_own_stop_is_no_other_order(self):
        # (a guard) the adapter's own stop holds all of it; the whole
        # position is closed, ok.
        r = self.rig()
        r.long_with_stop("AAPL", 10, sl=round(
            r.fake.state()["prices"]["AAPL"] - 30, 2))
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("X"), "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 4))
        self.assertTrue(until(lambda: r.live("AAPL") == [], 6), r.live())


class FirstClosesAfterAnUnknownOrder(closing_orders.ClosingOrders):
    """MSFT 5 and AAPL 10 held; order.place SELL 4 AAPL is answered 504
    and NOT carried out yet; flatten_all. The SELL 4 lands just before
    flatten's cancel-all, i.e. after its first read. The stand-in's
    default for a marked close for more than is held (NOT observed at
    Alpaca): accepted and traded."""

    def late_sell(self, intent=None):
        r = self.start()
        if intent is not None:
            r.fake.set_intent(kind=intent)
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))
        kind, out = r.req("order.place", {"client_id": r.cid("P"),
                                          "symbol": "AAPL", "side": "sell",
                                          "qty": 4})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        gate.on(lambda method, path, q, data: method == "DELETE"
                and path == "/v2/orders",
                lambda carry: (gate.land(slot), carry())[1])
        low = [r.held("AAPL")]
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                low[0] = min(low[0], r.held("AAPL"))
                time.sleep(0.01)
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        try:
            flat = r.req("flatten_all", {"client_id": r.cid("F")})
            time.sleep(2.0)
        finally:
            stop.set()
            t.join(2)
        self.assertTrue(slot["landed"])
        sold = sorted(q for _, side, q in self.filled(r) if side == "sell")
        bought = sorted(q for _, side, q in self.filled(r) if side == "buy")
        return r, flat, low[0], sold, bought

    def test_its_symbol_is_closed_from_a_read_made_after(self):
        r, flat, low, sold, bought = self.late_sell()
        self.assertEqual(flat, ("ok", {"closed": 2}))
        self.assertGreaterEqual(low, 0.0)                 # never short
        self.assertEqual((sold, bought), ([4.0, 6.0], [10.0]))
        self.assertEqual(r.held("AAPL"), 0.0)

    def test_the_same_where_alpaca_holds_a_marked_close_to_what_is_free(self):
        # (a guard) the stand-in's other guess: the close for 10 is
        # refused, the loop sells 6.
        r, flat, low, sold, bought = self.late_sell(intent=True)
        self.assertEqual(flat, ("ok", {"closed": 2}))
        self.assertGreaterEqual(low, 0.0)
        self.assertEqual((sold, bought), ([4.0, 6.0], [10.0]))


class ClockBehindAlpaca(closing_orders.ClosingOrders):
    """The exchange's close as ALPACA tells it, with this machine's clock
    behind Alpaca's. The stand-in follows the session calendar ("auto")
    and is set to 3 s before its next close; its Date header follows its
    own clock (as Alpaca's does), so this machine is behind it by however
    far that close is from now. The clock is read (and kept) while the
    exchange is open; then Alpaca's time steps past the close."""

    def shut_after_reading(self, argv=None):
        r = faults.Rig(market="auto", start=False)
        self.rigs.append(r)
        if argv:
            r.argv = argv
        fake = r.fake
        p = patch.object(fake_alpaca._Handler, "date_time_string",
                         lambda h, timestamp=None: email.utils.formatdate(
                             fake.market.now_ns() / 1e9, usegmt=True))
        p.start()
        self.addCleanup(p.stop)
        close = fake_alpaca.cal_next(fake.market.now_ns() // 10 ** 9, 1)
        fake.set_clock(time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(close - 3)))
        self.assertTrue(fake.market.is_open())
        r.start()
        r.buy("AAPL", 10)
        self.assertIs(r.a._market_open(), True)       # read while open
        self.assertGreater(r.a.client.clock_skew_ms, 5000)   # we are behind
        fake.advance(5)                               # Alpaca: shut now
        self.assertFalse(fake.market.is_open())
        self.assertGreater(fake.market.now_ns() // 10 ** 6, close * 1000)
        return r

    def test_flatten_all_after_the_close_says_market_closed(self):
        r = self.shut_after_reading()
        m = r.mark()
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual((kind, out.get("code")), ("err", "market_closed"),
                         out)
        self.assertIn("AAPL", out["message"])
        self.assertEqual(r.held("AAPL"), 10.0)
        # no closing order was sent to wait for the next open
        self.assertEqual([o for o in r.live("AAPL")
                          if o["coid"].startswith(mod.P_FLAT)], [])
        self.assertEqual(self.sent(r, "AAPL")[1:], [])

    def test_position_close_after_the_close_says_market_closed(self):
        r = self.shut_after_reading(argv=["--live"])
        kind, out = r.req("position.close", {"client_id": r.cid("X"),
                                             "position_id": "AAPL"})
        self.assertEqual((kind, out.get("code")), ("err", "market_closed"),
                         out)
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.sent(r, "AAPL")[1:], [])


class PlatformCancelFirst(late_watch.Late):
    """A late LIMIT order P (client id P) rests after flatten_all has
    answered and this adapter is cancelling it (_late); the platform
    cancels it too, with order.cancel, and Alpaca takes the PLATFORM's
    first. Alpaca answers 204 to a cancel of an order whose cancel it has
    taken, so _late's DELETE is answered 204 as well. P retried under its
    id: the original outcome, not "this adapter cancelled it after
    flatten_all"."""

    P = {"symbol": "AAPL", "side": "buy", "qty": 10, "type": "limit",
         "price": 200.0}

    def not_ours(self, r, cid, order):
        self.assertTrue(until(lambda: order.status == "canceled", 4))
        time.sleep(1.0)
        self.assertEqual(r.req("order.place", dict(self.P, client_id=cid)),
                         ("ok", {"order_id": order.id, "status": "resting"}))
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertNotIn(cid, r.a._late_cancels)

    def test_taken_while_its_own_cancel_is_on_the_way(self):
        r = self.rig()
        gate = Gate(r)
        platform = []

        def ours(carry):
            # _late's DELETE reaches Alpaca only after the platform's
            # order.cancel has been taken and answered
            if not platform:
                platform.append(None)
                platform[0] = r.req("order.cancel", {
                    "client_id": r.cid("k"),
                    "order_id": self.made(r, "P-")[0].id})
            return carry()
        gate.on(lambda method, path, q, data: method == "DELETE"
                and path.startswith("/v2/orders/"), ours)
        m, cid, placed, flat, t = self.late(r, dict(self.P))
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: platform and platform[0], 10))
        self.assertEqual(platform[0], ("ok", {}))
        self.not_ours(r, cid, order)

    def test_taken_and_then_found_still_resting(self):
        # The platform's cancel is taken; for a moment the order is still
        # listed as resting (Alpaca was seen to take 0.8 s to show a
        # cancel), and _late looks then: it asks for nothing more.
        r = self.rig()
        bad = r.rules.add("DELETE", "/v2/orders/", fail=500, count=10 ** 6,
                          name="DELETE 500")
        m, cid, placed, flat, t = self.late(r, dict(self.P))
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: len(r.rules.hit("DELETE 500")) >= 1,
                              10))
        bad["count"] = 0
        r.fake.broker.ack_delay = 1.0
        self.assertEqual(r.req("order.cancel", {
            "client_id": r.cid("k"), "order_id": order.id}), ("ok", {}))
        n = len(r.log("DELETE", "/v2/orders/"))
        with r.a._state_lock:
            u = dict(r.a._late_due.get(cid) or {})
        self.assertTrue(u, "P is not being dealt with as a late order")
        r.a._late(cid, u)                 # as the late thread does
        self.assertEqual(len(r.log("DELETE", "/v2/orders/")), n)
        r.fake.broker.ack_delay = 0.005
        self.not_ours(r, cid, order)


class WhoseRequest(unittest.TestCase):
    """The rule of RefusedAsADuplicate by itself (a scripted broker),
    beside WhoseClose in tests/test_alpaca_unanswered.py."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="alpaca-r7-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        p = patch.object(mod.sys, "stderr", io.StringIO())
        p.start()
        self.addCleanup(p.stop)

    def judge(self, served, qty="5"):
        a, b = orders.make(self.dir)
        a._served["X1"] = served
        o = orders.order("o1", "X1", sym="AAPL", side="sell", qty=qty,
                         status="filled", filled=qty)
        try:
            a._whose_close(o, "X1")
        except AdapterError as e:
            return e.code, e.retryable
        return "ok"

    def test_case_by_case(self):
        both = {"askers": {"X1": "5", "X2": "5"}, "maker": None}
        self.assertEqual(self.judge(both), ("x_alpaca_close_not_known", True))
        # X2's one request was refused: it made nothing
        self.assertEqual(self.judge(dict(both, sent={"X2": 1},
                                         refused={"X2": 1})), "ok")

    def test_a_refusal_clears_only_a_call_all_of_whose_requests_it_was(self):
        # (a guard) X2 sent twice under the id, one refused: the other may
        # have made the order.
        both = {"askers": {"X1": "5", "X2": "5"}, "maker": None}
        self.assertEqual(self.judge(dict(both, sent={"X2": 2},
                                         refused={"X2": 1})),
                         ("x_alpaca_close_not_known", True))
        # ... and the call the entry was made for is never counted
        self.assertEqual(self.judge(dict(both, sent={"X2": 1},
                                         refused={"X1": 1})),
                         ("x_alpaca_close_not_known", True))
        # ... and maker stays maker
        self.assertEqual(self.judge(dict(both, maker="X2", sent={"X2": 1},
                                         refused={"X2": 1})),
                         ("x_alpaca_close_not_carried_out", False))

    def test_the_counts_are_written_down_once_a_refusal_is_among_them(self):
        a, b = orders.make(self.dir)
        a._served["X1"] = {"askers": {"X1": "5", "X2": "5"}, "maker": None,
                           "sent": {"X2": 1}, "refused": {"X2": 1}}
        a._served["Y1"] = {"askers": {"Y1": "5", "Y2": "5"}, "maker": None,
                           "sent": {"Y2": 1}}
        a._save_state()
        n, nb = orders.make(self.dir)                        # the next run
        self.assertEqual(n._served["X1"], a._served["X1"])
        self.assertEqual(n._served["Y1"], {"askers": {"Y1": "5", "Y2": "5"},
                                           "maker": None})


if __name__ == "__main__":
    unittest.main()
