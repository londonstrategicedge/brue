"""Alpaca adapter: retried requests, one class an item.

Run as tests/test_alpaca_close_beside_orders.py is: the REAL adapter
against the stand-in in this process (the broker's own book beside what
the adapter told the platform). Every 504, every late request and every
cancel that is answered and not carried out here is the test's own fault
injection: Alpaca has not been seen to do any of them. The faults pinned
here:

  - a late order whose platform order.cancel Alpaca answered 204 and
    did not carry out (Alpaca's documented `order_cancel_rejected`)
    was never cancelled again: _late stopped asking once the
    platform's cancel had been taken, and the order filled after
    flatten_all.
    -> the platform's taken cancel is given LATE_SETTLE; still
       resting after that, the order is asked for again, and that
       cancel, when it is what cancels it, is claimed.
       `order_cancel_rejected` clears the platform's mark.

  - a retried position.close whose own first request landed between
    the retry's look-up by client id and its read of the position
    was told `not_found`, or `x_alpaca_qty_held` "Nothing was
    closed" (final), while its own order closed the position.
    -> before either answer, a retry asks for its own id once more
       and is answered from that order (SPEC 2).

  - a close without qty while only this adapter's OWN closing order
    (an earlier close's) still held the rest was refused, final,
    `x_alpaca_qty_held` "Cancel them first": an order the platform
    does not see, whose cancel undoes a close.
    -> the retryable `x_alpaca_close_pending`, naming it; asked
       again once it has filled, the rest is closed. A user's order
       among the holders is `x_alpaca_qty_held` as before.

  - flatten_all asked again and again answered "not known to be
    flat" for 75 s over one order that never arrived (UNKNOWN_SEC is
    30 s): only the backstop put it on the long watch, after a
    look-up of its own, and that look-up waited behind flatten's.
    -> flatten's own look-up, answered without it, puts an order
       older than UNKNOWN_SEC on the long watch, as the backstop's
       does. A look-up with no answer decides nothing.

  - in the minute before the close every share order read the clock:
    sixteen at once on a 150 ms link were twelve placed and four
    refused `rate_limited`.
    -> inside CLOCK_EDGE a read is kept for one second, never past
       the boundary it names (in Alpaca's time). flatten_all still
       reads it at every call there (see ClockBehindAlpaca in
       tests/test_alpaca_close_beside_orders.py).
"""
import email.utils
import threading
import time
import unittest
from unittest.mock import patch

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import fake_alpaca
from tests import test_alpaca_faults as faults
from tests import test_alpaca_changed_meanwhile as changed_meanwhile
from tests import test_alpaca_late_watch as late_watch
from tests import test_alpaca_closing_orders as closing_orders

until = changed_meanwhile.until
Gate = closing_orders.Gate
JSON = {"Content-Type": "application/json"}


class CancelTakenNotCarriedOut(late_watch.Late):
    """A late LIMIT order P (client id P) rests after flatten_all has
    answered; _late's DELETEs are refused (500). The platform cancels P
    with order.cancel, and that DELETE is answered 204 and NOT carried out
    (the test's gate)."""

    P = {"symbol": "AAPL", "side": "buy", "qty": 10, "type": "limit",
         "price": 200.0}

    def taken_not_done(self, r, fails=3):
        """P late and resting, `fails` of _late's DELETEs refused; then the
        platform's order.cancel, answered 204 and not carried out."""
        r = r or self.rig()
        gate = Gate(r)
        bad = r.rules.add("DELETE", "/v2/orders/", fail=500, count=10 ** 6,
                          name="DELETE 500")
        m, cid, placed, flat, t = self.late(r, dict(self.P))
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: len(r.rules.hit("DELETE 500"))
                              >= fails, 12), r.rules.hits)
        bad["count"] = 0            # (_late's next try is 2 s away)
        taken = []

        def nothing(carry):
            taken.append(time.monotonic())
            return 204, JSON, None
        gate.on(lambda method, path, q, data: not taken and method ==
                "DELETE" and path == "/v2/orders/" + order.id, nothing)
        self.assertEqual(r.req("order.cancel", {
            "client_id": r.cid("k"), "order_id": order.id}), ("ok", {}))
        self.assertEqual(len(taken), 1)       # the platform's, not _late's
        self.assertEqual(order.status, "new")
        return r, cid, order

    def cancelled_by_this_adapter(self, r, cid, order):
        self.assertTrue(until(lambda: order.status == "canceled", 8),
                        order.status)
        r.fake.set_price("AAPL", price=150.0)   # (P would fill now)
        time.sleep(1.0)
        self.assertEqual(order.filled_qty, 0)
        self.assertEqual(r.held("AAPL"), 0.0)
        # This adapter's DELETE is the one that cancelled it: said so.
        kind, out = r.req("order.place", dict(self.P, client_id=cid))
        self.assertEqual((kind, out.get("code")),
                         ("err", "x_alpaca_cancelled_after_flatten"), out)
        self.assertEqual(self.sent(r, "AAPL"), [cid])   # nothing else sent

    def test_still_resting_after_the_platforms_cancel_it_is_cancelled(self):
        r, cid, order = self.taken_not_done(None)
        self.cancelled_by_this_adapter(r, cid, order)

    def test_order_cancel_rejected_clears_the_platforms_mark(self):
        r, cid, order = self.taken_not_done(None)
        with r.a._state_lock:
            rec = next(x for x in r.a._orders.values() if x["coid"] == cid)
        self.assertTrue(rec.get("user_cancel"))
        b = r.fake.broker
        with b.lock:                 # Alpaca's documented stream event
            b._emit("order_cancel_rejected", order, exec_id=False)
        self.assertTrue(until(lambda: not rec.get("user_cancel"), 3), rec)
        self.cancelled_by_this_adapter(r, cid, order)


class RetriedCloseLandsMeanwhile(closing_orders.ClosingOrders):
    """AAPL long 10; position.close X without qty is answered 504 and NOT
    carried out yet; X is sent again under its client id, as the error
    invites. X's own first request lands after the retry's look-up by
    client id and before its read of the position (the test lands it as
    that read reaches the stand-in)."""

    def retried(self, fills_at_once):
        r = self.start(syms=(("AAPL", 10),))
        if not fills_at_once:
            r.fake.set_fill_delay(2.0)
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))
        x = {"client_id": r.cid("X"), "position_id": "AAPL"}
        kind, out = r.req("position.close", x)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("HTTP 504", out["message"])
        looked = []

        def look(carry):
            looked.append(time.monotonic())
            return carry()

        def read(carry):
            if looked and not slot["landed"]:
                gate.land(slot)               # X's own request lands now
            return carry()
        gate.on(lambda method, path, q, data: method == "GET" and path ==
                "/v2/orders:by_client_order_id" and not slot["landed"], look)
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/positions/AAPL", read)
        again = r.req("position.close", x)
        self.assertTrue(looked and slot["landed"])    # the case was made
        self.assertEqual(again, ("ok", {}))   # answered from X's own order
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 6))
        self.assertEqual(r.req("position.close", x), ("ok", {}))
        time.sleep(1.0)
        # one sell of 10, under X's own id: nothing closed twice
        self.assertEqual([(c, q) for c, side, q in self.filled(r)
                          if side == "sell"], [(x["client_id"], 10.0)])
        self.assertEqual(r.held("AAPL"), 0.0)

    def test_it_lands_and_fills_before_the_read(self):
        # (it was: not_found "there is no open position in AAPL")
        self.retried(fills_at_once=True)

    def test_it_lands_and_holds_the_position_at_the_read(self):
        # (it was: x_alpaca_qty_held "... Nothing was closed", final)
        self.retried(fills_at_once=False)


class WholeCloseBesideItsOwnClose(closing_orders.ClosingOrders):
    """AAPL long 10; the stand-in's fills come 1.5 s after an order is
    marketable (seen: paper fills 0.9-3 s after the POST). position.close
    X1 qty 4 -> ok; at once position.close X2 without qty: X1's own
    closing order still holds 4."""

    def close(self, r, cid, qty=None):
        p = {"client_id": cid, "position_id": "AAPL"}
        if qty is not None:
            p["qty"] = qty
        return r.req("position.close", p)

    def sells(self, r):
        return sorted((c, q) for c, side, q in self.filled(r)
                      if side == "sell")

    def test_it_is_told_to_ask_again_not_to_cancel_that_order(self):
        r = self.start(syms=(("AAPL", 10),))
        r.fake.set_fill_delay(1.5)
        x1, x2 = r.cid("X1"), r.cid("X2")
        self.assertEqual(self.close(r, x1, 4), ("ok", {}))
        kind, out = self.close(r, x2)
        self.assertEqual((kind, out.get("code"), out.get("retryable"),
                          out.get("like")),
                         ("err", "x_alpaca_close_pending", True, "frozen"),
                         out)
        for word in (x1, "sell 4 AAPL", "Nothing was closed", "Ask again"):
            self.assertIn(word, out["message"])
        self.assertNotIn("Cancel", out["message"])
        self.assertNotIn(x2, self.sent(r, "AAPL"))       # nothing sent
        self.assertTrue(until(lambda: r.held("AAPL") == 6.0, 5))
        self.assertEqual(self.close(r, x2), ("ok", {}))  # asked again
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 5))
        time.sleep(0.5)
        self.assertEqual(self.sells(r), sorted([(x1, 4.0), (x2, 6.0)]))

    def test_a_users_order_among_them_is_still_qty_held(self):
        # (a guard) the user's own resting SELL 2 holds part as well:
        # x_alpaca_qty_held, final, naming both.
        r = self.start(syms=(("AAPL", 10),))
        px = r.fake.state()["prices"]["AAPL"]
        lim = r.cid("L")
        kind, out = r.req("order.place", {
            "client_id": lim, "symbol": "AAPL", "side": "sell", "qty": 2,
            "type": "limit", "price": round(px * 1.2, 2)})
        self.assertEqual(kind, "ok", out)
        self.assertTrue(until(lambda: [o for o in r.live("AAPL")
                                       if o["coid"] == lim], 4))
        r.fake.set_fill_delay(1.5)
        x1, x2 = r.cid("X1"), r.cid("X2")
        self.assertEqual(self.close(r, x1, 4), ("ok", {}))
        kind, out = self.close(r, x2)
        self.assertEqual((kind, out.get("code"), out.get("retryable")),
                         ("err", "x_alpaca_qty_held", False), out)
        for word in (lim, "a closing order of this adapter's",
                     "Nothing was closed"):
            self.assertIn(word, out["message"])
        self.assertTrue(until(lambda: r.held("AAPL") == 6.0, 5))
        self.assertEqual(self.sells(r), [(x1, 4.0)])


class AskedAgainOverALostOrder(closing_orders.ClosingOrders):
    """MSFT 5 held; order.place BUY 10 AAPL is answered 504 and never
    carried out; flatten_all is asked again 0.5 s after every answer.
    UNKNOWN_SEC is 4 s here (30 s in the adapter)."""

    def setUp(self):
        super().setUp()
        for name, value in (("UNKNOWN_SEC", 4.0), ("LATE_WATCH", 60.0)):
            p = patch.object(mod, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def lost(self):
        r = self.start(syms=(("MSFT", 5),))
        gate = Gate(r)
        gate.hold(Gate.order("AAPL"))                 # never carried out
        cid = r.cid("P")
        kind, out = r.req("order.place", {"client_id": cid, "symbol": "AAPL",
                                          "side": "buy", "qty": 10})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        return r, cid, time.monotonic()

    def again_and_again(self, r, t0, seconds):
        """flatten_all until it answers ok or `seconds` have passed since
        the order was sent: [(age at the answer, (kind, out))]."""
        out = []
        while time.monotonic() - t0 < seconds:
            got = r.req("flatten_all", {"client_id": r.cid("F")})
            out.append((time.monotonic() - t0, got))
            if got[0] == "ok":
                break
            self.assertTrue(got[1].get("retryable"), got)
            self.assertIn("not known to be flat", got[1]["message"])
            time.sleep(0.5)
        return out

    def test_its_own_look_up_puts_it_on_the_long_watch(self):
        r, cid, t0 = self.lost()
        # The backstop's own look-up of it never gets a place (it waits
        # behind flatten_all's requests at Alpaca's pace; diag_r84).
        inner = r.a._find_by_client_id

        def starved(coid, kind="write"):
            if threading.current_thread().name == "alpaca-poll":
                raise mod.AlpacaError(429, "more urgent ones went first",
                                      code="budget")
            return inner(coid, kind)
        r.a._find_by_client_id = starved
        answers = self.again_and_again(r, t0, 25.0)
        age, last = answers[-1]
        self.assertEqual(last[0], "ok", answers)      # (MSFT closed by then)
        # answered by the first flatten_all asked after UNKNOWN_SEC, in
        # its first pass (its own look-up told 404); not 75 s later
        self.assertLess(age, 4.0 + mod.FLATTEN_BUDGET + 1.5, answers)
        self.assertEqual(r.held("MSFT"), 0.0)
        # ... and it is still watched (asked for, said if it turns up)
        self.assertTrue((r.a._unknown.get(cid) or {}).get("watch"),
                        r.a._unknown)
        self.assertEqual(self.sent(r, "AAPL"), [])

    def test_with_no_answer_time_alone_does_not_make_it_flat(self):
        # (a guard) every look-up of it fails (500): nothing is known of
        # it, and flatten_all never answers flat over it, however long it
        # has been.
        r, cid, t0 = self.lost()
        r.rules.add("GET", "/v2/orders:by_client_order_id", fail=500,
                    count=10 ** 6, name="look-ups 500")
        answers = self.again_and_again(r, t0, 14.0)
        self.assertGreater(answers[-1][0], 4.0 + 5.0)
        self.assertNotEqual(answers[-1][1][0], "ok", answers)
        self.assertIn(cid, answers[-1][1][1]["message"])
        self.assertFalse((r.a._unknown.get(cid) or {}).get("watch"))


class ClockAtTheEdge(closing_orders.ClosingOrders):
    """The stand-in follows the session calendar and is set some seconds
    before its next close; its Date header follows its own clock (as in
    ClockBehindAlpaca, tests/test_alpaca_close_beside_orders.py)."""

    def at_the_edge(self, before):
        r = faults.Rig(market="auto", start=False)
        self.rigs.append(r)
        fake = r.fake
        p = patch.object(fake_alpaca._Handler, "date_time_string",
                         lambda h, timestamp=None: email.utils.formatdate(
                             fake.market.now_ns() / 1e9, usegmt=True))
        p.start()
        self.addCleanup(p.stop)
        close = fake_alpaca.cal_next(fake.market.now_ns() // 10 ** 9, 1)
        fake.set_clock(time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(close - before)))
        self.assertTrue(fake.market.is_open())
        r.start()
        return r, close

    def reads(self, r):
        return len(r.log("GET", "/v2/clock"))

    def buy(self, r):
        return r.req("order.place", {"client_id": r.cid("B"),
                                     "symbol": "AAPL", "side": "buy",
                                     "qty": 1})

    def test_inside_the_minute_a_read_is_kept_for_a_second(self):
        r, close = self.at_the_edge(50)
        t = time.monotonic()
        self.assertIs(r.a._market_open(), True)
        n = self.reads(r)
        self.assertIs(r.a._market_open(), True)       # no second read
        self.assertEqual(self.buy(r)[0], "ok")
        self.assertEqual(self.buy(r)[0], "ok")
        self.assertLess(time.monotonic() - t, 0.9)     # (the case was made)
        self.assertEqual(self.reads(r), n)
        time.sleep(1.1)                                # ... a second only
        self.assertEqual(self.buy(r)[0], "ok")
        self.assertEqual(self.reads(r), n + 1)

    def test_never_past_the_boundary_it_names(self):
        # (a guard) read 2 s before the close; Alpaca's time then steps
        # past it and an answer says so; a market order within the second
        # is refused, from a new read.
        r, close = self.at_the_edge(2)
        t = time.monotonic()
        self.assertIs(r.a._market_open(), True)
        n = self.reads(r)
        r.fake.advance(4)
        self.assertFalse(r.fake.market.is_open())
        r.a.client.call("GET", "trading", "/v2/account")    # (a Date)
        kind, out = self.buy(r)
        self.assertLess(time.monotonic() - t, 0.9)
        self.assertEqual((kind, out.get("code")), ("err", "market_closed"),
                         out)
        self.assertEqual(self.reads(r), n + 1)
        self.assertEqual(self.sent(r, "AAPL"), [])


if __name__ == "__main__":
    unittest.main()
