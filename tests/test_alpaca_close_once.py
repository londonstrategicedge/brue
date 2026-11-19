"""Alpaca adapter: a position is closed once, one test a door.

Run as tests/test_alpaca_after_flatten.py is: the REAL adapter against the
stand-in in this process, the broker's own book beside what the adapter
told the platform. The fault pinned here:

  a position was closed TWICE: its position.close reached Alpaca
  late (3 s is enough), flatten_all had sold it meanwhile under
  another client id, the close sold it again (short), flatten
  bought that back. The same with flatten's own closing order
  late and flatten asked again, with a second position.close
  under another client id, and with an order request answered 504
  and carried out afterwards.
  -> the rule now: never two closing orders of one symbol on their
     way, or of unknown fate, under different client ids. A
     further close of the symbol goes out under the SAME id, and
     Alpaca takes an id once. A 5xx on an order request is of
     unknown fate, never "nothing exists".
"""
import threading
import time

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_faults as faults
from tests import test_alpaca_orders as orders
from tests import test_alpaca_changed_meanwhile as changed_meanwhile

until = changed_meanwhile.until


class Once(changed_meanwhile.SentAndSaid):
    """What every test here ends with, read at the BROKER: the position
    was sold once, was never short, and nothing was bought back."""

    def filled(self, r, sym="AAPL"):
        b = r.fake.broker
        with b.lock:
            return [(o.client_order_id, o.side, float(o.filled_qty))
                    for o in b.orders.values()
                    if o.inst.symbol == sym and o.filled_qty > 0]

    def lowest(self, r, seconds, sym="AAPL"):
        end, low = time.monotonic() + seconds, r.held(sym)
        while time.monotonic() < end:
            low = min(low, r.held(sym))
            time.sleep(0.05)
        return low

    def once(self, r, low, bought=10.0, sym="AAPL"):
        got = self.filled(r, sym)
        self.assertGreaterEqual(low, 0.0, got)           # never short
        self.assertEqual(r.held(sym), 0.0, got)
        self.assertEqual(
            (sum(q for _, side, q in got if side == "sell"),
             sum(q for _, side, q in got if side == "buy")),
            (bought, bought), got)
        self.assertEqual(self.notices(r, 0, "OPENED"), [])

    def start(self, argv=None):
        r = faults.Rig(start=False)
        self.rigs.append(r)
        if argv:
            r.argv = argv
        r.start()
        r.buy("MSFT", 5)
        r.buy("AAPL", 10)
        time.sleep(0.5)
        return r


class CloseLate(Once):
    """10 AAPL held; position.close AAPL reaches Alpaca `late` seconds
    late; flatten_all is read 0.3 s after it."""

    def late(self, late, argv=None, qty=None):
        r = self.start(argv)
        r.rules.add("POST", "/v2/orders", body='"AAPL"', before=late,
                    count=1, name="close late")
        p = {"client_id": r.cid("X"), "position_id": "AAPL"}
        if qty:
            p["qty"] = qty
        close = r.send("position.close", p)
        time.sleep(0.3)
        flat = r.send("flatten_all", {"client_id": r.cid("F")})
        low = self.lowest(r, late + 3.0)
        self.once(r, low)
        self.assertEqual(r.answer(flat).get("result"), {"closed": 2})
        self.assertEqual(r.held("MSFT"), 0.0)
        return r, r.answer(close), p["client_id"]

    def test_a_close_three_seconds_late_is_not_carried_out_twice(self):
        r, close, cid = self.late(3.0)
        # one closing order, under the close's own client id
        self.assertEqual([c for c, side, _ in self.filled(r)
                          if side == "sell"], [cid])
        self.assertNotIn("error", close, close)

    def test_a_close_four_seconds_late_is_not_carried_out_twice(self):
        self.late(4.0)

    def test_a_close_nine_seconds_late_does_not_open_the_other_side(self):
        self.late(9.0)

    def test_the_same_on_a_live_account(self):
        self.late(3.0, argv=["--live"])

    def test_a_late_close_of_part_of_it_and_flatten_sell_ten_between_them(self):
        self.late(3.0, qty=4)


class FlattenAgain(Once):
    """flatten_all's OWN closing order for AAPL is stalled; flatten gives
    up (retryable) and is asked again at once."""

    QUICK = changed_meanwhile.LateOrder.QUICK

    def test_asked_again_it_sends_its_close_under_the_same_id(self):
        r = self.start()
        r.rules.add("POST", "/v2/orders", body='"AAPL"', before=6.0,
                    count=1, name="flatten's close late")
        first = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        self.assertTrue(first.get("error", {}).get("retryable"), first)
        again = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        low = self.lowest(r, 5.5)                # the first one has landed
        self.once(r, low)
        self.assertEqual(again.get("result"), {"closed": 1}, again)
        self.assertEqual(len([c for c in self.sent(r, "AAPL")
                              if c.startswith(mod.P_FLAT)]), 1,
                         self.sent(r, "AAPL"))


class CloseAgain(Once):
    """No flatten at all: a position.close whose POST is stalled answers
    "not known"; the user closes again under ANOTHER client id."""

    QUICK = dict(Once.QUICK, HANDLER_BUDGET=2.5)

    def test_a_second_close_under_another_id_does_not_sell_it_twice(self):
        r = self.start()
        r.rules.add("POST", "/v2/orders", body='"AAPL"', before=5.0,
                    count=1, name="close late")
        x = r.cid("X")
        kind, out = r.req("position.close", {"client_id": x,
                                             "position_id": "AAPL"})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        second = r.req("position.close", {"client_id": r.cid("Y"),
                                          "position_id": "AAPL"})
        low = self.lowest(r, 4.5)                # the first one has landed
        self.once(r, low)
        self.assertEqual(second, ("ok", {}))
        self.assertEqual([c for c, side, _ in self.filled(r)
                          if side == "sell"], [x])

    def test_an_earlier_close_found_at_alpaca_is_said_and_nothing_sent(self):
        # The close sent before is at Alpaca after all (here for PART of
        # the position): this call's order is not sent beside it, and
        # its answer is not "done" but: read the position and ask again.
        a, b = orders.make()
        a._closing["BTC/USD"] = {"coid": "X1", "flying": 0, "unknown": True}
        b.on("GET", "/v2/positions/*", orders.position())
        b.on("GET", "/v2/orders:by_client_order_id", orders.order(
            "x1", "X1", side="sell", qty="0.0001", status="filled",
            filled="0.0001"))
        with self.assertRaises(mod.AdapterError) as ctx:
            a.handle_position_close({"client_id": "Y1",
                                     "position_id": "BTC/USD"})
        self.assertEqual((ctx.exception.code, ctx.exception.retryable),
                         ("x_alpaca_close_pending", True))
        self.assertIn("X1", str(ctx.exception))
        self.assertEqual(b.sent("POST"), [])
        # Its fate is known now. (The entry is not dropped the moment the
        # order is known; it is kept a few seconds, marked as seen, so
        # that a close sized from a read made before that moment finds it
        # and sends nothing.)
        pend = a._closing["BTC/USD"]
        self.assertEqual((pend["coid"], pend["flying"], pend["unknown"]),
                         ("X1", 0, False))
        self.assertIsNotNone(pend.get("seen"))


class Gateway(Once):
    """POST /v2/orders is answered 504 by something in front of Alpaca,
    and the order is carried out three seconds later. (No 5xx of
    Alpaca's own has been observed; the fault is the test's own.)"""

    def gateway(self, r, late=3.0):
        inner, hit = r.rules._answer, []

        def answer(method, path, q, data, headers, service):
            if (method == "POST" and path == "/v2/orders" and not hit
                    and b'"AAPL"' in (data or b"")):
                hit.append(1)
                threading.Timer(late, inner, (method, path, q, data,
                                              headers, service)).start()
                return 504, {"Content-Type": "application/json"}, {
                    "message": "upstream request timeout"}
            return inner(method, path, q, data, headers, service)
        r.rules._answer = answer

    def test_a_close_answered_504_and_carried_out_later_is_one_close(self):
        r = self.start()
        self.gateway(r)
        x = r.cid("X")
        kind, out = r.req("position.close", {"client_id": x,
                                             "position_id": "AAPL"})
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        low = self.lowest(r, 5.0)
        self.once(r, low)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("HTTP 504", out["message"])
        self.assertEqual(flat, ("ok", {"closed": 2}))
        # asked again under its id: the order that closed it, nothing new
        self.assertEqual(r.req("position.close", {
            "client_id": x, "position_id": "AAPL"}), ("ok", {}))
        self.once(r, low)

    def test_an_order_answered_504_and_carried_out_later_is_not_nothing(self):
        r = self.rig()
        r.buy("MSFT", 5)
        self.gateway(r)
        m, cid = r.mark(), r.cid("P")
        kind, out = r.req("order.place", {"client_id": cid, "symbol": "AAPL",
                                          "side": "buy", "qty": 10})
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        time.sleep(5.0)
        # nothing stands open behind the panic button's answer
        self.assertEqual(r.held("AAPL"), 0.0, flat)
        self.assertEqual(flat, ("ok", {"closed": 2}))
        # ... and the order was never called refused: it was not known
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m))


if __name__ == "__main__":
    import unittest
    unittest.main()
