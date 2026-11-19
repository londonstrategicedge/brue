"""Alpaca adapter: closing orders and flatten_all's "flat", one test a door.

Run as tests/test_alpaca_close_once.py is: the REAL adapter against the
stand-in in this process, the broker's own book beside what the adapter
told the platform. Every "late" and every 504 here is the test's own fault
injection: Alpaca has not been seen to carry a request out late. The
faults pinned here:

  - flatten_all answered {"closed": 1} with a position open: it
    judged "flat" on a read made BEFORE an order of unknown fate was
    found at Alpaca (26 of 96 runs).
    -> a read made before such an order was first seen says nothing
       about it: the account is read again, open orders first.
  - the close-all path (more than four positions) sold positions
    twice when its one request was late or answered 504, and sold a
    position opened after the flatten.
  - a close answered 504 that landed in flatten_all's first second
    was sold again from flatten's first read.
  - a close of unknown fate that landed after the position's stop
    had fired sold ten more: short ten.
    -> every closing order is MARKED as a close (`position_intent`):
       Alpaca itself refuses one that finds nothing to close (seen
       on paper, 2026-11-11), and that refusal is answered from a
       new read of the position. No close-all: one marked order a
       position. The one-closing-order entry is kept a few seconds
       after its order is first seen.

  - order.modify was refused "busy" two times in three while a
    target rested part-filled, with no fault at all: the five
    seconds a part-filled exit is left alone began anew at every
    look that found the pair right.
    -> they are counted once, from when the exit was first seen
       part-filled.

  - nothing of the orders of unknown fate, the late ones or the
    closing orders in flight survived a restart: flatten_all said
    "flat" at once, a late order landed unnamed, a position whose
    close was on the wire was closed again under another id.
    -> they are in the state file, written before the POST that
       makes them, and a start takes back those younger than ten
       minutes.

The stand-in refuses a marked close with nothing to close exactly as
Alpaca was seen to. What Alpaca was NOT seen to do (a marked close for
more than is held; one beside a working closing order) the stand-in does
NOT do here either: it accepts and trades such an order, the worst case.
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brueconnect.adapter import AdapterError
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_faults as faults
from tests import test_alpaca_orders as orders
from tests import test_alpaca_changed_meanwhile as changed_meanwhile

until = changed_meanwhile.until

G504 = (504, {"Content-Type": "application/json"},
        {"message": "upstream request timeout"})


class Gate:
    """Between the adapter and the stand-in: a test's own answers.

    `hold(match)`: the first request that matches is answered 504 at
    once and NOT carried out; `land()` carries it out (the order arrives
    at Alpaca then) and waits for the stand-in's engine. `on(match, fn)`:
    `fn(carry)` answers a matching request; `carry()` is the stand-in's
    own answer."""

    def __init__(self, r):
        self.r, self.inner = r, r.rules._answer
        self.held, self.hooks, self.lock = [], [], threading.Lock()
        self.seen = []
        r.rules._answer = self.answer

    @staticmethod
    def order(sym):
        return lambda method, path, q, data: (
            method == "POST" and path == "/v2/orders"
            and ('"%s"' % sym).encode() in (data or b""))

    def hold(self, match, name="held"):
        slot = {"match": match, "args": None, "landed": False, "name": name}
        self.held.append(slot)
        return slot

    def on(self, match, fn):
        self.hooks.append((match, fn))

    def land(self, slot, settle=True):
        """The held request reaches Alpaca NOW. Its answer goes nowhere."""
        with self.lock:
            if slot["landed"] or slot["args"] is None:
                return None
            slot["landed"] = True
        out = self.inner(*slot["args"])
        if settle:
            self.r.fake.settle()
        return out

    def bodies(self, word=""):
        """The body of every order POSTed since this gate was set, whose
        client id starts with `word`."""
        out = []
        for _, method, path, _, data in list(self.seen):
            if method == "POST" and path == "/v2/orders":
                body = json.loads(data)
                if str(body.get("client_order_id") or "").startswith(word):
                    out.append(body)
        return out

    def answer(self, method, path, q, data, headers, service):
        args = (method, path, q, data, headers, service)
        self.seen.append((time.monotonic(), method, path, dict(q or {}),
                          data))
        for slot in self.held:
            if slot["args"] is None and slot["match"](method, path, q, data):
                slot["args"] = args
                return G504
        for match, fn in self.hooks:
            if match(method, path, q, data):
                return fn(lambda: self.inner(*args))
        return self.inner(*args)


class ClosingOrders(changed_meanwhile.SentAndSaid):
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
        """Read at the BROKER: sold once, never short, nothing bought
        back."""
        got = self.filled(r, sym)
        self.assertGreaterEqual(low, 0.0, got)           # never short
        self.assertEqual(r.held(sym), 0.0, got)
        self.assertEqual(
            (sum(q for _, side, q in got if side == "sell"),
             sum(q for _, side, q in got if side == "buy")),
            (bought, bought), got)
        self.assertEqual(self.notices(r, 0, "OPENED"), [])

    def start(self, argv=None, syms=(("MSFT", 5), ("AAPL", 10))):
        r = faults.Rig(start=False)
        self.rigs.append(r)
        if argv:
            r.argv = argv
        r.start()
        for sym, qty in syms:
            r.buy(sym, qty)
        time.sleep(0.5)
        return r


class Unanswered(ClosingOrders):
    """MSFT 5 held; order.place BUY 10 AAPL is answered 504 and NOT
    carried out yet (the test says when it lands)."""

    def begin(self):
        r = self.rig()
        r.buy("MSFT", 5)
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))
        cid = r.cid("P")
        kind, out = r.req("order.place", {"client_id": cid, "symbol": "AAPL",
                                          "side": "buy", "qty": 10})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("HTTP 504", out["message"])
        return r, gate, slot, cid

    def flat_when_ok(self, r, flat, held_then):
        """The one thing a panic button may not do: answer ok while the
        account holds something."""
        kind, out = flat
        if kind == "ok":
            self.assertEqual(held_then, {}, "flatten_all answered %s with "
                             "%s held at Alpaca" % (out, held_then))
        else:
            self.assertTrue(out.get("retryable"), out)
        return kind

    def now_held(self, r):
        b = r.fake.broker
        with b.lock:
            return {k: float(v[1]) for k, v in b.positions.items()}


class FlatOnAStaleRead(Unanswered):
    """MSFT 5 held; order.place BUY 10 AAPL is answered 504; flatten_all
    is asked at once; the order is carried out later. With the delay
    swept from 0.3 to 7.2 s, flatten_all answered flat over the open
    position 26 times in 96. Here the order is made to land at the two
    instants that matter, every time."""

    def test_an_order_that_lands_between_a_pass_s_reads_and_its_look_up(self):
        # flatten_all reads the account (flat), THEN asks Alpaca for the
        # order of unknown fate by its client id; the order has arrived
        # and filled in between. The look-up finds it: "known". It used
        # to answer {"closed": 1} from the read made before it.
        r, gate, slot, cid = self.begin()
        with r.a._state_lock:
            # (the backstop's own look-up, every two seconds, is kept out
            # of this test: the look-up that finds it is flatten's)
            for u in r.a._unknown.values():
                u["next"] = time.monotonic() + 60.0
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/orders:by_client_order_id"
                and q.get("client_order_id") == cid,
                lambda carry: (gate.land(slot), carry())[1])
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        held = self.now_held(r)
        self.assertTrue(slot["landed"])
        self.assertEqual(self.flat_when_ok(r, flat, held), "ok", flat)
        self.assertEqual(flat[1], {"closed": 2}, flat)
        self.assertEqual(r.held("AAPL"), 0.0)
        # the ten bought were sold once, by flatten_all
        got = self.filled(r)
        self.assertEqual(sorted((side, q) for _, side, q in got),
                         [("buy", 10.0), ("sell", 10.0)], got)

    def test_an_order_the_stream_tells_of_between_the_reads_and_the_look_up(self):
        # The same instant, another door: the order arrives after the
        # pass's reads were MADE, and it is the stream (or the backstop)
        # that tells of it first. flatten's own look-up then has nothing
        # left to find; the answer was "flat" all the same.
        r, gate, slot, cid = self.begin()
        with r.a._state_lock:
            for u in r.a._unknown.values():
                u["next"] = time.monotonic() + 60.0
        read = threading.Event()

        def positions(carry):
            out = carry()
            if armed:
                read.set()
            return out

        def open_orders(carry):
            out = carry()                 # what Alpaca lists NOW: nothing
            if armed and not slot["landed"]:
                read.wait(1.0)            # (the positions were read too)
                gate.land(slot)           # ... and then the order arrives
                until(lambda: cid in r.a._coids, 2.0)    # the stream says so
            return out
        armed = []
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/positions", positions)
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/orders" and q.get("nested") == "false"
                and "direction" not in q, open_orders)
        # (armed once MSFT is gone: the pass that would say "flat")
        threading.Thread(target=lambda: (until(
            lambda: r.held("MSFT") == 0.0, 6.0), time.sleep(0.05),
            armed.append(1)), daemon=True).start()
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        held = self.now_held(r)
        self.assertTrue(slot["landed"])
        self.assertEqual(self.flat_when_ok(r, flat, held), "ok", flat)
        self.assertEqual(flat[1], {"closed": 2}, flat)
        self.assertEqual(r.held("AAPL"), 0.0)


class FlatOutOfTime(Unanswered):
    """The same, scaled as changed_meanwhile.LateOrder is: flatten_all
    works for 3 s."""

    QUICK = changed_meanwhile.LateOrder.QUICK

    def test_with_no_time_left_to_read_again_it_does_not_say_flat(self):
        # The order turns up at flatten_all's last look-up: there is no
        # time for another read. The answer is then "not known to be
        # flat", retryable, never ok from the read made before; and the
        # order is handed on like any that turns up after a flatten_all:
        # SAID, with what the symbol holds, and nothing placed against it.
        r, gate, slot, cid = self.begin()
        with r.a._state_lock:
            for u in r.a._unknown.values():
                u["next"] = time.monotonic() + 60.0
        sent = [None]

        def lookup(carry):
            now = time.monotonic()
            if sent[0] is None or now - sent[0] < 1.5 or slot["landed"]:
                return carry()            # not there yet: 404
            time.sleep(max(0.0, sent[0] + 2.75 - now))
            gate.land(slot)
            return carry()
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/orders:by_client_order_id"
                and q.get("client_order_id") == cid, lookup)
        m = r.mark()
        sent[0] = time.monotonic()
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        held = self.now_held(r)
        self.assertTrue(slot["landed"])
        self.assertEqual(held, {"AAPL": 10.0})
        self.assertEqual(self.flat_when_ok(r, flat, held), "err", flat)
        self.assertIn("not known to be flat", flat[1]["message"])
        self.assertIn("buy 10 AAPL", flat[1]["message"])
        said = until(lambda: self.notices(r, m, "FILLED 10"), 6)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("NO order against it", said[-1]["detail"])
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        # asked again, as the answer says: flat, and the ten sold once
        again = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual(again, ("ok", {"closed": 1}))
        self.assertEqual(self.now_held(r), {})
        self.assertEqual(r.events(m, "order.rejected"), [])


class Marked(ClosingOrders):
    """Every closing order this adapter sends says that it is one."""

    def test_every_closing_order_is_marked_as_a_close(self):
        r = self.start(syms=(("AAPL", 10), ("MSFT", 5), ("BTC/USD", 0.01)))
        m, cid = r.mark(), r.cid()
        r.ok("order.place", {"client_id": cid, "symbol": "SPY",
                             "side": "sell", "qty": 4})          # a short
        self.assertTrue(r.wait("order.filled", since=m))
        r.ok("order.modify", {"client_id": r.cid("m"), "position_id": "MSFT",
                              "sl": 300.0})
        self.assertTrue(until(lambda: r.stops("MSFT")))
        gate = Gate(r)
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        r.ok("position.close", {"client_id": r.cid("y"),
                                "position_id": "SPY"})
        self.assertTrue(until(lambda: r.held("SPY") == 0.0))
        # (the stop of MSFT is made again by the adapter: NOT marked)
        r.ok("order.modify", {"client_id": r.cid("m"), "position_id": "MSFT",
                              "sl": 310.0, "tp": 900.0})
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("F")}),
                         ("ok", {"closed": 3}))
        closes = gate.bodies(mod.P_CLOSE) + gate.bodies("x-") \
            + gate.bodies("y-") + gate.bodies(mod.P_FLAT)
        got = sorted((b["symbol"], b["side"], b["position_intent"])
                     for b in closes if "position_intent" in b)
        self.assertEqual(got, [
            ("AAPL", "sell", "sell_to_close"),
            ("AAPL", "sell", "sell_to_close"),
            ("BTC/USD", "sell", "sell_to_close"),
            ("MSFT", "sell", "sell_to_close"),
            ("SPY", "buy", "buy_to_close")], closes)
        self.assertEqual(len(closes), 5, closes)
        exits = gate.bodies(mod.P_SL) + gate.bodies(mod.P_TP)
        self.assertTrue(exits)
        self.assertEqual([b for b in exits if "position_intent" in b], [])
        self.assertEqual(r.log("DELETE", "/v2/positions"), [])


class NothingToClose(ClosingOrders):
    """Alpaca's own check, as it was seen: a closing order that finds
    nothing on that side to close is refused. That is answered from a
    new read of the position, and nothing is opened."""

    def gone_first(self, r, sym="AAPL", qty=10):
        """The position is sold by ANOTHER client of the account in the
        instant before this adapter's closing order arrives."""
        gate, hit = Gate(r), []

        def sell_first(carry):
            if not hit:
                hit.append(1)
                st, o = r.other("POST", "/v2/orders", {
                    "symbol": sym, "qty": str(qty), "side": "sell",
                    "type": "market", "time_in_force": "gtc" if "/" in sym
                    else "day"})
                assert st == 200, o
                r.fake.settle()
            return carry()
        gate.on(Gate.order(sym), sell_first)
        return gate

    @staticmethod
    def mine(gate):
        """The orders the ADAPTER sent (the other client's carry no
        client id of their own)."""
        return [b for b in gate.bodies() if b.get("client_order_id")]

    def test_position_close_of_a_position_that_went_answers_not_found(self):
        r = self.start(argv=["--live"])
        gate = self.gone_first(r)
        m = r.mark()
        kind, out = r.req("position.close", {"client_id": r.cid("X"),
                                             "position_id": "AAPL"})
        low = self.lowest(r, 1.5)
        # (it used to be sent unmarked: it sold ten more, short ten, "ok")
        self.assertEqual((kind, out.get("code")), ("err", "not_found"), out)
        self.assertIn("nothing left to close", out["message"])
        self.assertIn("position intent mismatch", out["message"])
        self.once(r, low)
        # the adapter's one closing order, not sent again
        self.assertEqual(len(self.mine(gate)), 1, gate.bodies())
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertEqual(r.held("MSFT"), 5.0)

    def test_flatten_all_reads_again_and_goes_on(self):
        r = self.start(argv=["--live"])
        gate = self.gone_first(r)
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        low = self.lowest(r, 1.5)
        self.assertEqual(flat, ("ok", {"closed": 2}))
        self.once(r, low)
        self.assertEqual(r.held("MSFT"), 0.0)
        self.assertEqual(len([b for b in gate.bodies(mod.P_FLAT)
                              if b["symbol"] == "AAPL"]), 1)

    def test_the_coin_that_went_is_not_found_either(self):
        # Crypto: Alpaca checks the balance first, and that is what was
        # seen for a close with nothing held: 403 "insufficient balance
        # for BTC". The same answer: the position is read again.
        r = self.rig()
        r.buy("BTC/USD", 0.01)
        time.sleep(0.5)
        held = r.held("BTC/USD")
        gate = self.gone_first(r, "BTC/USD", held)
        kind, out = r.req("position.close", {"client_id": r.cid("X"),
                                             "position_id": "BTC/USD"})
        self.assertEqual((kind, out.get("code")), ("err", "not_found"), out)
        self.assertIn("insufficient balance for BTC", out["message"])
        self.assertEqual(r.held("BTC/USD"), 0.0)
        self.assertEqual(len(self.mine(gate)), 1, gate.bodies())

    def test_a_close_that_lands_after_the_stop_has_fired_opens_nothing(self):
        # A bracket of ten; position.close is answered 504 (its order not
        # carried out yet); the stop and target are put back; the price
        # falls through the stop, which sells the ten; THEN the close
        # lands.
        r = self.rig()
        px = r.fake.state()["prices"]["AAPL"]
        r.fake.set_price("AAPL", price=px)
        sl, tp = round(px - 30, 2), round(px + 30, 2)
        r.buy("AAPL", 10, sl=sl, tp=tp)
        self.assertTrue(until(lambda: r.shown("AAPL") == (10.0, sl, tp)))
        gate = Gate(r)
        slot = gate.hold(lambda method, path, q, data: method == "POST"
                         and path == "/v2/orders"
                         and b'"market"' in (data or b""))
        m = r.mark()
        kind, out = r.req("position.close", {"client_id": r.cid("X1"),
                                             "position_id": "AAPL"})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        # the stop is back, and fires
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, sl)], 8),
                        r.live())
        r.fake.set_price("AAPL", price=round(sl - 1, 2))
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 5))
        self.assertTrue(r.wait("position.closed",
                               lambda d: d["reason"] == "sl", since=m))
        # ... and now the close arrives at Alpaca
        status, _, body = gate.land(slot)
        self.assertEqual(status, 422, body)      # (it used to be taken)
        self.assertEqual(body["code"], 42210000, body)
        self.assertIn("position intent mismatch", body["message"])
        low = self.lowest(r, 2.0)
        self.once(r, low)
        self.assertEqual(self.notices(r, m, "OPENED"), [])


class NothingToCloseAnswers(unittest.TestCase):
    """position.close's answers to that refusal, against a scripted
    broker (tests/test_alpaca_orders.py): what Alpaca says is stated."""

    MISMATCH = mod.AlpacaError(
        422, "position intent mismatch, inferred: sell_to_open, "
        "specified: sell_to_close", 42210000)

    def setUp(self):
        p = patch.object(mod.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_an_order_under_this_close_s_own_id_is_the_original_outcome(self):
        # The first request of this close was carried out and answered
        # nothing; the platform asks again, and Alpaca (which was asked
        # for the id a moment too early) says of the new request
        # "nothing to close". The order under the id is what closed it.
        a, b = orders.make()
        b.on("GET", "/v2/positions/BTCUSD", orders.position())
        b.on("POST", "/v2/orders", self.MISMATCH)
        b.on("GET", "/v2/orders:by_client_order_id", [
            orders.Step(mod.AlpacaError(404, "order not found", 40410000)),
            orders.Step(orders.order("x1", "X1", side="sell",
                                     qty="0.0001995", status="filled",
                                     filled="0.0001995"))])
        a._intents["X1"] = {"kind": "close", "cid": "X1"}    # sent before
        try:
            out = a.handle_position_close({"client_id": "X1",
                                           "position_id": "BTC/USD"})
        except AdapterError as e:
            out = (e.code, str(e))
        self.assertEqual(out, {})
        self.assertEqual(len(b.sent("POST")), 1)             # not sent again

    def test_gone_is_not_found_and_still_there_is_said_and_retryable(self):
        a, b = orders.make()
        b.on("POST", "/v2/orders", self.MISMATCH)
        b.on("GET", "/v2/positions/BTCUSD", [
            orders.Step(orders.position()),
            orders.Step(mod.AlpacaError(404, "position does not exist",
                                        40410000))])
        with self.assertRaises(AdapterError) as ctx:
            a.handle_position_close({"client_id": "Y1",
                                     "position_id": "BTC/USD"})
        self.assertEqual((ctx.exception.code, ctx.exception.retryable),
                         ("not_found", False))
        self.assertEqual(a._closing, {})         # refused: nothing exists
        # ... and when Alpaca's own list still shows the position
        b.on("GET", "/v2/positions/BTCUSD", orders.position())
        with self.assertRaises(AdapterError) as ctx:
            a.handle_position_close({"client_id": "Y2",
                                     "position_id": "BTC/USD"})
        e = ctx.exception
        self.assertEqual((e.code, e.retryable, e.like),
                         ("x_alpaca_nothing_to_close", True, "frozen"))
        self.assertIn("reads as 0.0001995 long now", str(e))
        self.assertEqual(len(b.sent("POST")), 2)     # one a call, no more
        self.assertEqual([n for n, _ in a.events
                          if n == "order.rejected"], [])

    def test_an_earlier_close_of_unknown_fate_stays_asked_for(self):
        # X1 was sent and never answered; Y1 goes out under X1's id (one
        # closing order a symbol) and is refused: nothing to close. That
        # says nothing of X1, which may still arrive: it is still asked
        # for, and the symbol's next close still goes under its id.
        a, b = orders.make()
        a._closing["BTC/USD"] = {"coid": "X1", "flying": 0, "unknown": True}
        a._unknown["X1"] = {"kind": "close", "cid": "X1", "flat": True,
                            "t0": time.monotonic() - 3.0, "n": 1,
                            "next": time.monotonic() + 60.0, "side": "sell",
                            "qty": "0.0001995", "sym": "BTC/USD"}
        b.on("POST", "/v2/orders", self.MISMATCH)
        b.on("GET", "/v2/positions/BTCUSD", [
            orders.Step(orders.position()),
            orders.Step(mod.AlpacaError(404, "position does not exist",
                                        40410000))])
        with self.assertRaises(AdapterError) as ctx:
            a.handle_position_close({"client_id": "Y1",
                                     "position_id": "BTC/USD"})
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(b.sent("POST")[0][3]["client_order_id"], "X1")
        self.assertTrue(a._unknown["X1"]["flat"])
        self.assertEqual((a._closing["BTC/USD"]["coid"],
                          a._closing["BTC/USD"]["unknown"]), ("X1", True))

    def test_when_alpaca_cannot_be_asked_nothing_is_decided(self):
        a, b = orders.make()
        b.on("GET", "/v2/positions/BTCUSD", orders.position())
        b.on("POST", "/v2/orders", self.MISMATCH)
        b.on("GET", "/v2/orders:by_client_order_id",
             mod.AlpacaError(0, "timed out"))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_position_close({"client_id": "Z1",
                                     "position_id": "BTC/USD"})
        self.assertEqual((ctx.exception.code, ctx.exception.retryable),
                         ("internal", True))
        self.assertIn("not known", str(ctx.exception))


class KeptAFewSeconds(ClosingOrders):
    """position.close AAPL is answered 504 and carried out a moment
    later, in flatten_all's first second: after flatten's first read of
    the positions, before its first closes, which are sized from that
    read."""

    def lands_after_the_first_read(self, r, gate, slot, x):
        def cancel_all(carry):
            out = carry()
            gate.land(slot, settle=False)        # the close arrives NOW
            until(lambda: x in r.a._coids, 2.0)  # ... and the stream says so
            return out
        gate.on(lambda method, path, q, data: method == "DELETE"
                and path == "/v2/orders", cancel_all)

    def run_it(self, fill_delay):
        r = self.start(argv=["--live"])
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))
        x = r.cid("X")
        kind, out = r.req("position.close", {"client_id": x,
                                             "position_id": "AAPL"})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.lands_after_the_first_read(r, gate, slot, x)
        if fill_delay:
            r.fake.set_fill_delay(fill_delay)
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        low = self.lowest(r, fill_delay + 2.0)
        self.assertTrue(slot["landed"])
        self.once(r, low)
        self.assertEqual(flat, ("ok", {"closed": 2}))
        # ONE closing order for AAPL was ever at Alpaca: the close's own
        self.assertEqual([c for c, side, _ in self.filled(r)
                          if side == "sell"], [x])
        return r, gate

    def test_a_close_that_lands_in_flatten_s_first_second_is_sold_once(self):
        self.run_it(0.0)

    def test_and_while_it_is_still_working_nothing_is_sent_beside_it(self):
        # Not seen of Alpaca: what it does with a marked close while
        # another closing order for the whole position is still working.
        # The stand-in accepts and trades it (the worst case), so it is
        # the kept entry alone that stops the second sale here.
        r, gate = self.run_it(1.2)
        self.assertEqual([b for b in gate.bodies(mod.P_FLAT)
                          if b["symbol"] == "AAPL"], [])

    def test_a_close_asked_after_one_has_been_seen_is_a_new_wish(self):
        # (a guard, true before as well) Scaling out: two partial
        # closes one after the other are two wishes, and the second is
        # sized from a read that counts the first.
        r = self.start(argv=["--live"])
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("X1"), "position_id": "AAPL", "qty": 4}),
            ("ok", {}))
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("X2"), "position_id": "AAPL", "qty": 3}),
            ("ok", {}))
        self.assertTrue(until(lambda: r.held("AAPL") == 3.0), r.held("AAPL"))
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("F")}),
                         ("ok", {"closed": 2}))
        got = self.filled(r)
        self.assertEqual(sorted(q for _, side, q in got if side == "sell"),
                         [3.0, 3.0, 4.0], got)


class OneByOne(ClosingOrders):
    """Six positions. They used to go through Alpaca's close-all (DELETE
    /v2/positions), asked once; that request late or answered 504, the
    positions were closed one by one as well and sold twice. There is no
    such request any more."""

    SIX = (("AAPL", 10), ("MSFT", 5), ("SPY", 4), ("XRX", 30), ("DTI", 7),
           ("SNDL", 200))

    def test_six_positions_each_by_its_own_marked_order(self):
        r = self.start(argv=["--live"], syms=self.SIX)
        gate = Gate(r)
        r.fake.requests(clear=True)
        m = r.mark()
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual(flat, ("ok", {"closed": 6}))
        self.assertEqual(r.log("DELETE", "/v2/positions"), [])
        bodies = gate.bodies(mod.P_FLAT)
        self.assertEqual(sorted((b["symbol"], float(b["qty"]),
                                 b["position_intent"]) for b in bodies),
                         sorted((s, float(q), "sell_to_close")
                                for s, q in self.SIX), bodies)
        for sym, qty in self.SIX:
            got = self.filled(r, sym)
            self.assertEqual(
                (sum(q for _, side, q in got if side == "sell"),
                 r.held(sym)), (float(qty), 0.0), got)
        closes = until(lambda: len(r.events(m, "position.closed")) == 6
                       and r.events(m, "position.closed"), 5)
        self.assertTrue(closes, r.events(m))
        self.assertEqual({c["reason"] for c in closes}, {"flatten"})

    def test_at_the_real_pace_six_are_flat_well_inside_one_call(self):
        # Three requests a second to the trading host: two reads, the
        # cancel-all, six closing orders, two reads. Each order waits
        # for its place; none is refused here and asked again.
        r = self.start(argv=["--live"], syms=self.SIX)
        r.a.client.budget.per_second = mod.PACE_PER_SECOND
        time.sleep(1.1)
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual(flat, ("ok", {"closed": 6}))
        self.assertLess(r.took, 5.0)
        self.assertEqual(len(r.log("POST", "/v2/orders")), 12)   # 6 + 6

    def test_a_close_in_flight_and_six_positions(self):
        # position.close AAPL reaches Alpaca 3 s late, flatten_all 0.3 s
        # after it. (A guard: this case was sound with the close-all too,
        # because a close in flight kept the close-all away.)
        r = self.start(argv=["--live"], syms=self.SIX)
        r.rules.add("POST", "/v2/orders", body='"AAPL"', before=3.0,
                    count=1, name="close late")
        r.send("position.close", {"client_id": r.cid("X"),
                                  "position_id": "AAPL"})
        time.sleep(0.3)
        flat = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        low = self.lowest(r, 4.5)
        self.once(r, low)
        self.assertEqual(flat.get("result"), {"closed": 6}, flat)
        self.assertEqual(r.log("DELETE", "/v2/positions"), [])


class ModifyBesideAPartFilledTarget(ClosingOrders):
    """A bracket of nine; the target fills three and rests, part-filled;
    the stop is brought to six beside it. Then the platform moves the
    stop, again and again. Nothing is refused by Alpaca and nothing is
    lost: 4 of 13 such requests were answered ok."""

    def part_filled(self, clips, interval):
        r = self.rig()
        px = r.fake.state()["prices"]["AAPL"]
        r.fake.set_price("AAPL", price=px)
        sl, tp = round(px - 50, 2), round(px + 50, 2)
        r.buy("AAPL", 9, sl=sl, tp=tp)
        self.assertTrue(until(lambda: r.shown("AAPL") == (9.0, sl, tp)))
        r.fake.next_fill(clips=clips, interval=interval)
        r.fake.set_price("AAPL", price=round(px + 51, 2))    # the target
        self.assertTrue(until(lambda: r.held("AAPL") == 6.0), r.held("AAPL"))
        # the pair settles: after its five seconds the stop is brought
        # to six where it stands, the target is not touched
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, sl)], 12),
                        r.live("AAPL"))
        return r, sl, tp

    def target(self, r):
        return [(o["id"], o["qty"], o["status"]) for o in r.live("AAPL")
                if o["type"] == "limit"]

    def test_the_stop_is_moved_every_time_it_is_asked(self):
        r, sl, tp = self.part_filled(3, 90.0)
        before = self.target(r)
        time.sleep(1.0)
        got = []
        for i in range(6):
            new = round(sl + 5 + i, 2)
            kind, out = r.req("order.modify", {
                "client_id": r.cid("m"), "position_id": "AAPL", "sl": new})
            got.append((kind, (out or {}).get("code")))
            if kind == "ok":
                self.assertTrue(until(
                    lambda: r.stops("AAPL") == [(6.0, new)], 4), r.live())
            time.sleep(1.0)
        self.assertEqual(got, [("ok", None)] * 6)
        self.assertEqual(r.shown("AAPL"), (6.0, round(sl + 10, 2), tp))
        # the target kept its place in the queue all along: the same
        # order, still part-filled
        self.assertEqual(self.target(r), before)
        self.assertEqual(before[0][2], "partially_filled")
        self.assertEqual(r.session()["state"], "connected")

    def test_a_later_clip_of_the_same_target_does_not_start_them_anew(self):
        # The second clip, ten seconds after the first: the stop is at
        # six by then and the pair has been found right. It is brought
        # to three where it stands at once, not after another five
        # seconds in which it is larger than the position.
        r, sl, tp = self.part_filled(3, 10.0)
        self.assertTrue(until(lambda: r.held("AAPL") == 3.0, 12),
                        r.held("AAPL"))
        t = time.monotonic()
        self.assertTrue(until(lambda: r.stops("AAPL") == [(3.0, sl)], 9),
                        r.live("AAPL"))
        self.assertLess(time.monotonic() - t, 3.5)
        # ... and a new position in the symbol has its own five seconds
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 13))
        self.assertTrue(until(lambda: "AAPL" not in r.a.prot.executing, 5),
                        r.a.prot.executing)


class Restart(ClosingOrders):
    """What a restart keeps: the orders of unknown fate, the late ones
    and the closing orders in flight."""

    QUICK = changed_meanwhile.LateOrder.QUICK

    def kill(self, r):
        """The adapter process is KILLED and started again on the same
        profile: nothing is saved on the way out, and whatever the dead
        one still had in hand does nothing more. (Here it is an object
        in this process: it is made deaf and mute.)"""
        a = r.a
        a._state_dir = ""                     # the dead write nothing

        def dead(*args, **kw):
            raise mod.AlpacaError(0, "this process was killed")
        a.client.call = dead
        a._stop.set()
        for ev in (a._wake, a.prot.wake, a._quote_wake):
            ev.set()
        r.a = None
        r.fake.drop_streams()
        return r.start()

    def test_a_close_on_the_wire_then_a_kill_then_flatten_is_one_close(self):
        # position.close X1 is on the wire (its POST reaches Alpaca 4 s
        # late); the adapter is killed at 1 s and started again;
        # flatten_all. It used to close AAPL under a new id, and X1
        # sold ten more when it landed: short ten.
        r = self.start(argv=["--live"])
        r.rules.add("POST", "/v2/orders", body='"AAPL"', before=4.0,
                    count=1, name="X1 late")
        x = r.cid("X1")
        r.send("position.close", {"client_id": x, "position_id": "AAPL"})
        self.assertTrue(until(lambda: r.rules.hit("X1 late"), 3))
        time.sleep(0.5)
        self.kill(r)
        with r.a._state_lock:
            taken_back = (dict(r.a._closing.get("AAPL") or {}).get("coid"),
                          x in r.a._unknown)
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        low = self.lowest(r, 5.0)                # X1 has landed by now
        self.once(r, low)
        self.assertEqual(flat, ("ok", {"closed": 2}))
        # (the new run knew of the close on the wire from its first line)
        self.assertEqual(taken_back, (x, True))
        self.assertEqual([c for c, side, _ in self.filled(r)
                          if side == "sell"], [x])
        # asked again under its id, as its caller would: the outcome
        self.assertEqual(r.req("position.close", {
            "client_id": x, "position_id": "AAPL"}), ("ok", {}))
        self.once(r, low)

    def unknown_then_restart(self, more):
        """order.place P is answered 504 (carried out when the
        test says); flatten_all is told of it; the adapter is closed in
        the ordinary way and started again."""
        r = self.rig()
        r.buy("MSFT", 5)
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))
        cid = r.cid("P")
        kind, out = r.req("order.place", dict({
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10},
            **more))
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual(kind, "err", out)
        self.assertIn("may still arrive", out["message"])
        r.restart()
        m = r.mark()
        # flatten_all after the restart: NOT "flat" at once
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("not known to be flat", out["message"])
        self.assertIn("buy 10 AAPL", out["message"])
        self.assertGreater(r.took, 1.5)
        return r, gate, slot, cid, m

    def test_after_a_restart_flatten_does_not_say_flat_and_the_late_fill_is_named(self):
        r, gate, slot, cid, m = self.unknown_then_restart({})
        gate.land(slot)                          # ... and now it arrives
        said = until(lambda: self.notices(r, m, "FILLED 10"), 8)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("buy 10 AAPL", said[-1]["detail"])
        self.assertIn("NO order against it", said[-1]["detail"])
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(r.ok("flatten_all", {"client_id": r.cid("F")}),
                         {"closed": 1})

    def test_after_a_restart_a_late_order_that_rests_is_still_cancelled(self):
        r, gate, slot, cid, m = self.unknown_then_restart(
            {"type": "limit", "price": 200.0})
        gate.land(slot, settle=False)
        said = until(lambda: self.notices(r, m, "is cancelled"), 10)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("buy 10 AAPL", said[-1]["detail"])
        self.assertTrue(until(lambda: not r.live(), 4), r.live())
        self.assertEqual(self.made(r, cid)[0].status, "canceled")
        self.assertEqual(r.held("AAPL"), 0.0)


class Remembered(unittest.TestCase):
    """What is written, when, and what a start takes back (a scripted
    broker: tests/test_alpaca_orders.py)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="alpaca-r6-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for p in (patch.object(mod.time, "sleep", lambda s: None),
                  patch.object(mod.sys, "stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def file(self):
        with open(os.path.join(self.dir, "alpaca_state.json")) as fh:
            return json.load(fh)

    def test_an_order_and_its_closing_entry_are_written_before_the_post(self):
        a, b = orders.make(self.dir)
        seen = []

        def post(q, body, path):
            st = self.file()                     # the file AT this instant
            seen.append((st.get("closing"), st.get("unknown")))
            return orders.order("c1", body["client_order_id"], side="sell",
                                qty=body["qty"])
        b.on("GET", "/v2/positions/BTCUSD", orders.position())
        b.on("POST", "/v2/orders", post)
        self.assertEqual(a.handle_position_close({
            "position_id": "BTC/USD", "client_id": "X1"}), {})
        closing, unknown = seen[0]
        self.assertEqual((closing or {}).get("BTC/USD", {}).get("coid"),
                         "X1", closing)
        self.assertEqual({k: (unknown or {}).get("X1", {}).get(k)
                          for k in ("kind", "cid", "side", "qty", "sym",
                                    "wire")},
                         {"kind": "close", "cid": "X1", "side": "sell",
                          "qty": "0.0001995", "sym": "BTC/USD",
                          "wire": True}, unknown)
        # answered: no longer on the wire, and the next save says so
        self.assertTrue(a._unsaved)
        a._save_state()
        st = self.file()
        self.assertEqual(st["unknown"], {})
        self.assertIsNotNone(st["closing"]["BTC/USD"]["seen"])

    def test_a_start_takes_back_what_is_younger_than_ten_minutes(self):
        a, b = orders.make(self.dir)
        now = time.monotonic()

        def u(age, **more):
            return dict({"kind": "user", "cid": "c", "t0": now - age,
                         "next": now, "n": 3, "flat": False, "side": "buy",
                         "qty": "10", "sym": "AAPL"}, **more)
        a._unknown["U-young"] = u(12.0, flat=True)
        a._unknown["U-old"] = u(700.0, flat=True, watch=True)
        a._late_due["L-rests"] = u(40.0, flat=True, tries=4, stopped=False)
        a._dropped["D-young"] = u(650.0, flat=True,
                                  dropped_at=a.now_ms() - 50000)
        a._dropped["D-old"] = u(1900.0, flat=True,
                                dropped_at=a.now_ms() - 1300000)
        a.__dict__.setdefault("_wire", {})["W-flying"] = {
            "kind": "flatten", "cid": None, "n": 1, "at": a.now_ms() - 2000,
            "side": "sell", "qty": "5", "sym": "MSFT", "flat": True}
        a._closing["AAPL"] = {"coid": "X1", "flying": 1, "unknown": False,
                              "kind": "close", "at": a.now_ms() - 3000}
        a._closing["MSFT"] = {"coid": "X2", "flying": 0, "unknown": False,
                              "kind": "close", "at": a.now_ms() - 9000,
                              "seen": now - 8.0}
        a._closing["SPY"] = {"coid": "X3", "flying": 0, "unknown": True,
                             "kind": "close", "at": a.now_ms() - 700000}
        a._save_state()
        n, _ = orders.make(self.dir)             # the next run
        mono = time.monotonic()
        self.assertEqual(sorted(n._unknown), ["L-rests", "U-young",
                                              "W-flying"])
        young = n._unknown["U-young"]
        self.assertAlmostEqual(mono - young["t0"], 12.0, delta=1.0)
        self.assertEqual((young["flat"], young["side"], young["qty"],
                          young["sym"], young["kind"], young["cid"]),
                         (True, "buy", "10", "AAPL", "user", "c"))
        self.assertLessEqual(young["next"], mono)        # asked for at once
        # one a flatten_all had found late and not dealt with is asked
        # for again, and is late again when it is found
        self.assertTrue(n._unknown["L-rests"]["flat"])
        self.assertAlmostEqual(mono - n._unknown["L-rests"]["t0"], 40.0,
                               delta=1.0)
        wire = n._unknown["W-flying"]
        self.assertEqual((wire["kind"], wire["flat"], wire["sym"]),
                         ("flatten", True, "MSFT"))
        self.assertEqual(sorted(n._dropped), ["D-young"])
        # (its ten minutes run from when it was dropped, not from each
        # start: the next save writes the same time again)
        at = self.file()["dropped"]["D-young"]["at"]
        n._save_state()
        self.assertEqual(self.file()["dropped"]["D-young"]["at"], at)
        self.assertEqual(sorted(n._closing), ["AAPL", "MSFT"])
        # on the wire when the last run stopped: of unknown fate
        self.assertEqual({k: n._closing["AAPL"].get(k) for k in (
            "coid", "flying", "unknown", "seen")}, {
            "coid": "X1", "flying": 0, "unknown": True, "seen": None})
        # known to be at Alpaca for eight seconds: a read made now counts it
        self.assertFalse(n._closing["MSFT"]["unknown"])
        self.assertAlmostEqual(mono - n._closing["MSFT"]["seen"], 8.0,
                               delta=1.0)
        # ... and the next run knows these client ids as sent before
        self.assertTrue(n._sent_before("U-young"))

    def test_another_account_takes_none_of_it(self):
        a, b = orders.make(self.dir)
        a._state_account = "PA1"
        a._unknown["U1"] = {"kind": "user", "cid": "c", "side": "buy",
                            "t0": time.monotonic(), "next": 0.0, "n": 0,
                            "flat": True, "qty": "1", "sym": "AAPL"}
        a._closing["AAPL"] = {"coid": "X1", "flying": 0, "unknown": True,
                              "kind": "close", "at": a.now_ms()}
        a._save_state()
        n, _ = orders.make(self.dir)
        self.assertIn("U1", n._unknown)
        n._bind_account("PA2")
        self.assertEqual((n._unknown, n._closing, n._dropped), ({}, {}, {}))


if __name__ == "__main__":
    import unittest
    unittest.main()
