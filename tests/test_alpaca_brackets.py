"""Alpaca adapter: a position's stop and target, held by the broker.

Never a price watched inside the adapter: an OTO or bracket order when the
position is opened, a stop, a limit or an OCO pair afterwards, replaced in
place when a level moves. What is tested is that the orders AT THE BROKER
always match what `positions.list` claims, and that losing one is said out
loud.

The adapter makes Alpaca match the wanted levels from a fresh read of the
position and the symbol's open orders, so most of these run against the
stand-in `tests/fake_alpaca.py` (a broker with state), on loopback, through
the rig of `tests/test_alpaca_faults.py`. The few that are about one
function in isolation use the scripted routes of `tests/test_alpaca_orders`.
The races and lost answers are in `tests/test_alpaca_faults.py`.
"""
import threading
import time
import unittest

from brueconnect.adapter import AdapterError
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests.test_alpaca_faults import Faults, until
from tests.test_alpaca_orders import make, names, only, order

D = mod.Decimal


def leg(oid, typ="stop", status="held", cls="oto", **kw):
    return order(oid, "server-" + oid, sym="AAPL", side="sell", typ=typ,
                 qty=kw.pop("qty", "5"), status=status, order_class=cls,
                 group_role="stop_loss" if typ == "stop" else "take_profit",
                 **kw)


def entry_route(legs):
    def post(q, body, path):
        return order("e1", body["client_order_id"], sym="AAPL",
                     qty=body["qty"], status="pending_new",
                     order_class=body.get("order_class", ""), legs=legs)
    return post


class Brackets(Faults):
    """Against the stand-in: what Alpaca holds, and what the adapter says."""

    def rig(self, **kw):
        """A rig with AAPL held at 352.5. The stand-in's AAPL walks between
        about 342 and 363 over a day, and the levels set here (a stop at
        345, a target at 360) are each on their own side of 352.5."""
        r = super().rig(**kw)
        r.fake.set_price("AAPL", price=352.5)
        return r

    def exits(self, r, sym="AAPL"):
        """(type, side, qty, price, class, status) of the live exit orders."""
        return sorted((o["type"], o["side"], o["qty"],
                       o["stop"] if o["type"] == "stop" else o["limit"],
                       o["cls"], o["status"]) for o in r.live(sym))

    def test_entry_with_a_stop_is_one_atomic_order_at_alpaca(self):
        r = self.rig()
        r.fake.set_fill_delay(0.6)       # see the legs BEFORE the entry fills
        m, cid = r.mark(), r.cid()
        out = r.ok("order.place", {"client_id": cid, "symbol": "AAPL",
                                   "side": "buy", "qty": 5, "sl": 345.0})
        self.assertEqual(out["status"], "accepted")
        entry = [o for o in r.live("AAPL") if o["coid"] == cid][0]
        self.assertEqual((entry["type"], entry["cls"], entry["qty"]),
                         ("market", "oto", 5.0))
        self.assertEqual([x for x in self.exits(r) if x[0] == "stop"],
                         [("stop", "sell", 5.0, 345.0, "oto", "held")])
        # the leg waits for its entry: it is not an exit without a position
        time.sleep(0.3)
        self.assertEqual(r.session()["state"], "connected")
        self.assertEqual(len(r.live("AAPL")), 2)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m))
        self.assertEqual(r.shown(), (5.0, 345.0, None))
        self.assertEqual([n for n, d in r.events(m)
                          if n.startswith("order.")],
                         ["order.accepted", "order.filled"])
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])

    def test_both_levels_is_a_bracket(self):
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0, tp=360.0)
        self.assertEqual(self.exits(r), [
            ("limit", "sell", 5.0, 360.0, "bracket", "new"),
            ("stop", "sell", 5.0, 345.0, "bracket", "held")])
        self.assertEqual(r.shown(), (5.0, 345.0, 360.0))

    def test_the_stop_filling_is_a_close_with_reason_sl(self):
        r = self.rig()
        cid, _ = r.buy("AAPL", 5, sl=345.0)
        m = r.mark()
        r.fake.set_price("AAPL", price=344.0)
        c = r.wait("position.closed", since=m)
        self.assertTrue(c, r.events(m))
        self.assertEqual((c["reason"], c["qty"], c["client_id"],
                          c["position_id"]), ("sl", 5.0, cid, "AAPL"))
        self.assertTrue(c["close_id"].startswith("c-"))
        self.assertLess(c["realized_pnl"], 0)
        # an exit order is not the platform's order: no fill, no placed
        time.sleep(0.5)
        self.assertEqual([n for n, d in r.events(m)
                          if n.startswith("order.")], [])
        self.assertTrue(self.settled(r), self.last)
        self.assertNotIn("AAPL", r.a.prot.levels)

    def test_a_target_filling_in_two_parts(self):
        r = self.rig()
        r.buy("AAPL", 6, sl=345.0, tp=360.0)
        r.fake.next_fill(clips=2, interval=0.3)
        m = r.mark()
        r.fake.set_price("AAPL", price=361.0)
        self.assertTrue(until(
            lambda: len(r.events(m, "position.closed")) == 2, 6),
            r.events(m))
        closes = r.events(m, "position.closed")
        self.assertEqual([(c["reason"], c["qty"]) for c in closes],
                         [("tp", 3.0), ("tp", 3.0)])
        self.assertNotEqual(closes[0]["close_id"], closes[1]["close_id"])
        # the sibling stop, cancelled by Alpaca, is not a lost stop
        time.sleep(1.0)
        self.assertEqual([d for d in r.events(m, "session.state")
                          if d["state"] != "connected"], [])
        self.assertTrue(self.settled(r), self.last)

    def test_moving_the_stop_replaces_it_in_place(self):
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0)
        r.fake.requests(clear=True)
        for price in (346.0, 347.5):
            r.ok("order.modify", {"position_id": "AAPL", "sl": price})
            self.assertTrue(until(lambda: [x[3] for x in self.exits(r)]
                                  == [price], 3), self.exits(r))
            self.assertEqual(r.shown(), (5.0, price, None))
        # Alpaca's replace: the position was never without its stop
        self.assertEqual((len(r.log("PATCH")), r.log("DELETE"),
                          r.log("POST")), (2, [], []))
        # the same level again touches nothing
        r.ok("order.modify", {"position_id": "AAPL", "sl": 347.5})
        self.assertEqual(len(r.log("PATCH")), 2)

    def test_adding_a_target_swaps_the_stop_for_an_oco_pair(self):
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0)
        r.ok("order.modify", {"position_id": "AAPL", "tp": 360.0})
        self.assertTrue(until(lambda: self.exits(r) == [
            ("limit", "sell", 5.0, 360.0, "oco", "new"),
            ("stop", "sell", 5.0, 345.0, "oco", "held")], 3), self.exits(r))
        self.assertEqual(r.shown(), (5.0, 345.0, 360.0))
        # both move in place; clearing the target leaves a plain stop
        r.ok("order.modify", {"position_id": "AAPL", "sl": 346.0,
                              "tp": 361.0})
        self.assertTrue(until(lambda: [x[3] for x in self.exits(r)]
                              == [361.0, 346.0], 3), self.exits(r))
        r.ok("order.modify", {"position_id": "AAPL", "tp": None})
        self.assertTrue(until(lambda: self.exits(r) == [
            ("stop", "sell", 5.0, 346.0, "", "new")], 3), self.exits(r))
        self.assertEqual(r.shown(), (5.0, 346.0, None))
        r.ok("order.modify", {"position_id": "AAPL", "sl": None})
        self.assertTrue(until(lambda: self.exits(r) == [], 3))
        self.assertEqual(r.shown(), (5.0, None, None))
        self.assertEqual(r.session()["state"], "connected")

    def test_a_level_through_the_market_touches_nothing_at_alpaca(self):
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0)
        r.fake.requests(clear=True)
        for params, word in (({"sl": 400.0}, "below"),
                             ({"tp": 350.0}, "above"),
                             ({"sl": 350.0, "tp": 349.0}, "below")):
            kind, out = r.req("order.modify",
                              dict(params, position_id="AAPL"))
            self.assertEqual((kind, out["code"]), ("err", "invalid_stops"),
                             out)
            self.assertIn(word, out["message"])
        self.assertEqual((r.log("DELETE"), r.log("PATCH"), r.log("POST")),
                         ([], [], []))
        self.assertEqual(r.shown(), (5.0, 345.0, None))

    def test_a_refused_new_level_puts_the_old_stop_back(self):
        # The stop is taken off to make room for the pair, and Alpaca will
        # not take the pair. A mistyped level must not cost the stop.
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0)
        r.rules.add(method="POST", path="/v2/orders", body='"oco"',
                    fail=500, name="pair refused")
        kind, out = r.req("order.modify", {"position_id": "AAPL",
                                           "tp": 360.0})
        self.assertEqual(kind, "err", out)
        self.assertEqual(r.rules.hit("pair refused"), ["pair refused"])
        self.assertTrue(until(lambda: self.exits(r) == [
            ("stop", "sell", 5.0, 345.0, "", "new")], 4), self.exits(r))
        self.assertEqual(r.shown(), (5.0, 345.0, None))
        self.assertEqual(r.a.prot.levels["AAPL"], {"sl": 345.0, "tp": None})

    def test_no_stop_at_all_is_said_as_loudly_as_possible(self):
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0)
        r.rules.add(method="POST", path="/v2/orders", fail=500, count=3,
                    name="nothing is taken")
        kind, out = r.req("order.modify", {"position_id": "AAPL",
                                           "tp": 360.0})
        self.assertEqual(kind, "err", out)
        state = r.session()
        self.assertEqual(state["state"], "degraded", state)
        self.assertIn("AAPL", state["detail"])
        self.assertIn("NO stop", state["detail"])
        self.assertEqual(r.shown(), (5.0, None, None))   # shown as it is
        # ... and it is not left there: the old stop comes back
        self.assertTrue(until(lambda: self.exits(r) == [
            ("stop", "sell", 5.0, 345.0, "", "new")], 12), self.exits(r))
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 5), r.session())

    def test_closing_part_re_makes_the_stop_for_what_is_left(self):
        r = self.rig()
        r.buy("AAPL", 5, sl=345.0, tp=360.0)
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 2})
        c = r.wait("position.closed", since=m)
        self.assertEqual((c["qty"], c["reason"]), (2.0, "manual"))
        self.assertTrue(until(lambda: self.exits(r) == [
            ("limit", "sell", 3.0, 360.0, "oco", "new"),
            ("stop", "sell", 3.0, 345.0, "oco", "held")], 5), self.exits(r))
        self.assertEqual(r.shown(), (3.0, 345.0, 360.0))
        # adding to the position re-sizes them too
        r.buy("AAPL", 4)
        self.assertTrue(until(lambda: [x[2] for x in self.exits(r)]
                              == [7.0, 7.0], 6), self.exits(r))
        self.assertEqual(r.shown(), (7.0, 345.0, 360.0))

    def test_a_new_session_finds_the_stops_an_earlier_one_left(self):
        r = self.rig()
        r.buy("AAPL", 5)
        r.ok("order.modify", {"position_id": "AAPL", "sl": 345.0,
                              "tp": 360.0})       # an OCO pair of its own
        r.buy("MSFT", 2)
        r.ok("order.modify", {"position_id": "MSFT", "sl": 500.0})
        r.restart()
        self.assertEqual((r.shown("AAPL"), r.shown("MSFT")),
                         ((5.0, 345.0, 360.0), (2.0, 500.0, None)))
        self.assertEqual(r.a.prot.levels, {
            "AAPL": {"sl": 345.0, "tp": 360.0},
            "MSFT": {"sl": 500.0, "tp": None}})
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])
        # found without what the earlier session wrote down, too: the
        # client ids this adapter gives its own orders say whose they are
        r.restart(forget=True)
        self.assertEqual((r.shown("AAPL"), r.shown("MSFT")),
                         ((5.0, 345.0, 360.0), (2.0, 500.0, None)))

    def test_a_level_alpaca_no_longer_holds_at_the_start_is_said_not_remade(self):
        # While the adapter was not running, the stop was cancelled at
        # Alpaca. The broker is the truth at a start (SPEC 7): the level
        # is not shown, not quietly put back, and the session says so.
        r = self.rig()
        r.long_with_stop()
        r.stop_adapter()
        r.other("DELETE", "/v2/orders/" + r.live("AAPL")[0]["id"])
        self.assertTrue(until(lambda: not r.live("AAPL"), 3))
        r.start()
        self.assertEqual(r.shown(), (10.0, None, None))
        state = r.session()
        self.assertEqual(state["state"], "degraded", state)
        self.assertIn("AAPL", state["detail"])
        self.assertIn("300", state["detail"])
        time.sleep(2.5)
        self.assertEqual(r.live("AAPL"), [])
        # the platform sets it again: held, shown, and nothing left to say
        r.ok("order.modify", {"position_id": "AAPL", "sl": 300.0})
        self.assertEqual(r.shown(), (10.0, 300.0, None))
        self.assertEqual(r.session()["state"], "connected", r.session())

    def test_flatten_keeps_the_stop_of_a_position_it_cannot_close(self):
        r = self.rig()
        r.long_with_stop()
        r.buy("BTC/USD", 0.001)
        r.fake.set_market("shut")
        r.restart()                     # a session that knows it is shut
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out["code"], out["retryable"]),
                         ("err", "market_closed", False), out)
        self.assertIn("closed 1 of 2", out["message"])
        self.assertIn("AAPL", out["message"])
        self.assertEqual((r.held("BTC/USD"), r.held("AAPL")), (0.0, 10.0))
        self.assertEqual(r.stops("AAPL"), [(10.0, 300.0)])
        self.assertEqual(r.shown(), (10.0, 300.0, None))
        self.assertEqual([o for o in r.live() if o["type"] == "market"], [])


class Alone(unittest.TestCase):
    """One function at a time, against scripted answers."""

    def test_a_leg_heard_before_the_answer_is_still_ours(self):
        a, b = make()
        late = []

        def post(q, body, path):
            t = threading.Thread(target=a._on_trade_update, args=({
                "event": "held", "order": leg("s1", stop_price="345")},))
            t.start()
            late.append(t)
            t.join(0.2)                  # it must be waiting for us
            self.assertTrue(t.is_alive())
            return entry_route([leg("s1", stop_price="345")])(q, body, path)
        b.on("POST", "/v2/orders", post)
        a.handle_order_place({"client_id": "cid-1", "symbol": "AAPL",
                              "side": "buy", "qty": 5, "sl": 345.0})
        late[0].join(2.0)
        self.assertFalse(late[0].is_alive())
        self.assertEqual(a._orders["s1"]["kind"], "sl")
        self.assertEqual(a._orders["s1"]["entry"], "e1")
        self.assertEqual(a.prot.levels["AAPL"], {"sl": 345.0, "tp": None})
        self.assertEqual(names(a), ["order.accepted"])

    def test_refusals_before_anything_is_sent(self):
        a, b = make()
        base = {"client_id": "c", "symbol": "AAPL", "side": "buy", "qty": 5}
        cases = [({"sl": 360.0, "tp": 350.0}, "invalid_stops"),
                 ({"sl": -1}, "invalid_stops"),
                 ({"sl": 340.0, "type": "limit", "price": 345.0},
                  "unsupported"),
                 ({"symbol": "BTC/USD", "qty": 0.0002, "sl": 70000.0},
                  "trade_restricted")]
        for extra, want in cases:
            with self.assertRaises(AdapterError) as ctx:
                a.handle_order_place(dict(base, **extra))
            self.assertEqual(ctx.exception.code, want, extra)
        # these are about the request alone: Alpaca was not even asked
        self.assertEqual(b.sent("GET", "/v2/orders:by_client_order_id"), [])
        a._book["AAPL"] = {"qty": D(5), "avg": D(350), "fees": 0.0,
                           "cid": None, "opened": None}
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place(dict(base, client_id="c2", sl=340.0))
        self.assertEqual((ctx.exception.code, ctx.exception.like),
                         ("x_alpaca_position_open", "trade_restricted"))
        self.assertEqual(b.sent("POST"), [])

    def test_alpaca_refusing_the_stop_refuses_the_whole_order(self):
        a, b = make()
        b.on("POST", "/v2/orders", mod.AlpacaError(
            422, "stop_loss.stop_price must be <= base_price - 0.01",
            42210000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place({"client_id": "c1", "symbol": "AAPL",
                                  "side": "buy", "qty": 5, "sl": 400.0})
        self.assertEqual(ctx.exception.code, "invalid_stops")
        self.assertNotIn("AAPL", a.prot.levels)
        self.assertEqual(only(a, "order.rejected")["code"], "invalid_stops")

    def test_crypto_and_unknown_positions(self):
        a, b = make()
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_modify({"position_id": "BTC/USD", "sl": 1.0})
        self.assertEqual(ctx.exception.code, "trade_restricted")
        b.on("GET", "/v2/positions/AAPL", mod.AlpacaError(
            404, "position does not exist", 40410000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_modify({"position_id": "AAPL", "sl": 1.0})
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_modify({"position_id": "AAPL",
                                   "trail_points": 5})
        self.assertEqual(ctx.exception.code, "unsupported")

    def test_capabilities_say_it(self):
        a, _ = make()
        o = a.capabilities()["orders"]
        self.assertEqual((o["brackets"], o["modify"], o["trailing"],
                          o["oco"]), (True, True, False, False))
        rows = {r["symbol"]: r for r in a._catalog_list}
        self.assertEqual(rows["AAPL"]["stops_level_points"], 1)
        self.assertNotIn("stops_level_points", rows["BTC/USD"])


if __name__ == "__main__":
    unittest.main()
