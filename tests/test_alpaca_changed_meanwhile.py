"""Alpaca adapter: what changed between two reads, two orders or two runs,
one test each.

Run as tests/test_alpaca_faults.py is: the REAL adapter against the
stand-in in this process, the broker's own book beside what the adapter
told the platform. The faults pinned here:

  - flatten_all answered flat while an order it had interrupted was
    still on the wire
  - balance was made from an equity read now and a positions read up
    to 3 s old
  - a level due at a stop was put back, at the next start, on whatever
    position the symbol then had
  - a target that filled in parts was cancelled, with its stop, to
    "correct" the stop's size
  - bye leaving with a stop still due said nothing; on live, a
    bracket's held stop leg from an earlier run was in no list

The tests pin what stands now (tests/test_alpaca_after_flatten.py has the
rest): the interrupted order, once it turns up, is cancelled if it rests
and SAID if it has filled, and no order is ever placed against it; balance
is equity less the positions' open P&L, the two read together.
"""
import json
import os
import threading
import time

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_faults as faults
from tests import test_alpaca_handoff as handoff
from tests import test_alpaca_levels as levels

until = faults.until


class SentAndSaid(faults.Faults):
    def notices(self, r, since=0, word=""):
        return [d for d in r.events(since, "session.state")
                if d["state"] != "connected" and word in d["detail"]]

    def made(self, r, word):
        """Every order the broker ever held whose client id starts so."""
        b = r.fake.broker
        with b.lock:
            return [o for o in b.orders.values()
                    if o.client_order_id.startswith(word)]

    def sent(self, r, sym):
        """The client id of every order the broker ever held for `sym`:
        whatever was POSTed for it, by anyone, is in here."""
        b = r.fake.broker
        with b.lock:
            return [o.client_order_id for o in b.orders.values()
                    if o.inst.symbol == sym]

    def closes(self, r, since, sym):
        return [d for d in r.events(since, "position.closed")
                if d["position_id"] == sym]


class Balance(SentAndSaid):
    """positions.list, the price moves, account.get (the dock's pattern in
    a fast market)."""

    def broker(self, r):
        _, pos = r.other("GET", "/v2/positions")
        _, acct = r.other("GET", "/v2/account")
        return sum(float(p["unrealized_pl"]) for p in pos), acct

    def moved(self, r, moves):
        """What the dock does, with the market moving between its two
        calls: -> (the adapter's account, the broker's open P&L then)."""
        r.ok("positions.list")
        for sym, pct in moves.items():
            px = r.fake.state()["prices"][sym]
            r.fake.set_price(sym, price=round(px * (1 + pct / 100.0), 2))
        acct = r.ok("account.get")
        pl, raw = self.broker(r)
        return acct, pl, raw

    def test_equity_less_balance_is_the_open_profit_whatever_moved_between_the_reads(self):
        r = self.rig()
        flat = r.ok("account.get")
        self.assertAlmostEqual(flat["balance"], flat["equity"], places=6)
        r.buy("AAPL", 300)                                   # a long
        m, cid = r.mark(), r.cid()
        r.ok("order.place", {"client_id": cid, "symbol": "MSFT",
                             "side": "sell", "qty": 50})     # a short
        self.assertTrue(r.wait("order.filled", since=m))
        r.buy("BTC/USD", 0.01)                               # crypto
        time.sleep(0.5)
        # (Each price is held where it is, and moved only by this test:
        # the stand-in's own walk crossing a cent between the adapter's
        # reads and the test's would be 3.00 on 300 shares.)
        for sym, px in r.fake.state()["prices"].items():
            if sym in ("AAPL", "MSFT", "BTC/USD"):
                r.fake.set_price(sym, price=px)
        start = r.ok("account.get")["balance"]
        seen = []
        for moves in ({"AAPL": -2.0}, {"MSFT": 3.0}, {"BTC/USD": -4.0},
                      {"AAPL": 1.5, "MSFT": -2.5, "BTC/USD": 6.0}):
            acct, pl, raw = self.moved(r, moves)
            seen.append((moves, round(acct["equity"] - acct["balance"], 2),
                         round(pl, 2), round(acct["balance"], 2)))
            self.assertAlmostEqual(acct["equity"], float(raw["equity"]),
                                   places=2)
            # the open P&L, to the cents Alpaca rounds its figures to
            self.assertAlmostEqual(acct["equity"] - acct["balance"], pl,
                                   delta=0.03, msg=seen)
            # nothing was realised: balance has not moved
            self.assertAlmostEqual(acct["balance"], start, delta=0.03,
                                   msg=seen)
        self.assertGreater(max(abs(s[1]) for s in seen), 100, seen)

    def test_a_crypto_buy_that_only_rests_does_not_move_balance(self):
        # Seen on paper (2026-11-10 23:09 UTC): a resting crypto buy
        # lowers Alpaca's `cash` by its cost at once; equity does not move.
        r = self.rig()
        r.buy("BTC/USD", 0.01)
        before = r.ok("account.get")
        _, raw0 = self.broker(r)
        px = r.fake.state()["prices"]["BTC/USD"]
        kind, out = r.req("order.place", {
            "client_id": r.cid(), "symbol": "BTC/USD", "side": "buy",
            "qty": 0.02, "type": "limit", "price": round(px * 0.5, 2)})
        self.assertEqual(kind, "ok", out)
        time.sleep(0.5)
        after = r.ok("account.get")
        pl, raw = self.broker(r)
        self.assertLess(float(raw["cash"]), float(raw0["cash"]) - 100)
        self.assertAlmostEqual(after["balance"], before["balance"], delta=0.03)
        self.assertAlmostEqual(after["equity"] - after["balance"], pl,
                               delta=0.03)

    def test_a_shorts_open_profit_is_taken_from_equity_whatever_its_cost_says(self):
        # Balance is equity less the rows' unrealized_pl and
        # nothing else: not the cost basis, whose sign for a short Alpaca
        # has not been seen to give, and not the account's market values.
        for cost, mv in (("-10000", "-9500"), ("10000", "9500")):
            acct = {"equity": "100500", "long_market_value": "0",
                    "short_market_value": mv, "initial_margin": "0"}
            row = {"side": "short", "qty": "-100", "avg_entry_price": "100",
                   "cost_basis": cost, "market_value": mv,
                   "unrealized_pl": "500"}
            view = mod.AlpacaAdapter._account_view(acct, [row])
            self.assertAlmostEqual(view["balance"], 100000.0, places=6)
            self.assertAlmostEqual(view["equity"] - view["balance"], 500.0,
                                   places=6)


class LateOrder(SentAndSaid):
    """An order.place whose POST reaches Alpaca late (one stalled
    connection), flatten_all read 0.3 s after it. Scaled: the POST lands
    after 4 s, the order call has 2.5 s, flatten 3 s."""

    QUICK = dict(SentAndSaid.QUICK, HANDLER_BUDGET=2.5, FLATTEN_BUDGET=3.0,
                 FLATTEN_HARD=3.8, FLATTEN_FLOOR=2.0, NOTICE_SEC=6.0)

    def late(self, r, more, land=4.0):
        r.buy("MSFT", 5)
        r.rules.add("POST", "/v2/orders", body='"AAPL"', before=land,
                    count=1, name="stalled")
        m, cid = r.mark(), r.cid("P")
        place = r.send("order.place", dict({
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10},
            **more))
        time.sleep(0.3)
        t = time.monotonic()
        flat = r.send("flatten_all", {"client_id": r.cid("F")})
        return m, cid, r.answer(place), r.answer(flat), t

    def test_flatten_does_not_say_flat_while_an_order_it_cut_short_may_still_arrive(self):
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {})
        # the order call was cut short; whether its order exists is not known
        self.assertTrue(placed.get("error", {}).get("retryable"), placed)
        # flatten_all: NOT "flat". Retryable, and it names what may come.
        self.assertIn("error", flat, flat)
        self.assertTrue(flat["error"]["retryable"], flat)
        self.assertIn("may still arrive", flat["error"]["message"])
        self.assertIn("buy 10 AAPL", flat["error"]["message"])
        self.assertEqual(r.held("MSFT"), 0.0)        # what was there is closed
        # ... it arrives, and fills: ten shares behind the panic button
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m,
                               timeout=6), r.events(m))
        said = until(lambda: self.notices(r, m, "before flatten_all"), 4)
        self.assertTrue(said, r.events(m, "session.state"))
        # NOTHING is placed against it: the ten shares stay,
        # and the only order Alpaca ever held for AAPL is that one.
        time.sleep(3.0)
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(self.closes(r, m, "AAPL"), [])
        self.assertEqual(r.shown("AAPL")[0], 10.0)
        # It is SAID: the symbol, the side, the quantity, and when it
        # filled by Alpaca's own record of the order. Nothing is claimed
        # to have been closed, and flatten_all had promised no such thing.
        detail = said[-1]["detail"]
        _, o = r.other("GET", "/v2/orders/" + self.made(r, cid)[0].id)
        self.assertIn("buy 10 AAPL", detail)
        self.assertIn("FILLED 10 at %s by Alpaca's record"
                      % mod.iso_ms(mod.parse_ts(o["filled_at"])), detail)
        self.assertIn("NO order against it", detail)
        self.assertNotIn("closed", detail)
        self.assertNotIn("closed again", flat["error"]["message"])
        self.assertIn("places NO order against it",
                      flat["error"]["message"])
        # asked again, as the answer said: THAT closes them, and says so
        self.assertEqual(r.ok("flatten_all", {"client_id": r.cid("F")}),
                         {"closed": 1})
        self.assertEqual(r.held("AAPL"), 0.0)
        # said once: it goes again by itself
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 25), r.session())

    def test_one_that_arrives_and_only_rests_is_cancelled_and_said(self):
        r = self.rig()
        m, cid, placed, flat, t = self.late(
            r, {"type": "limit", "price": 200.0})
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        said = until(lambda: self.notices(r, m, "before flatten_all"), 10)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("buy 10 AAPL", said[-1]["detail"])
        self.assertIn("cancelled", said[-1]["detail"])
        # and not that it "reached Alpaca only after
        # flatten_all had answered": when it did is not known in here
        self.assertNotIn("had answered", said[-1]["detail"])
        self.assertIn("nothing of it filled", said[-1]["detail"])
        self.assertTrue(until(lambda: not r.live(), 4), r.live())
        self.assertEqual(r.held("AAPL"), 0.0)
        # it did arrive, it is cancelled at Alpaca, nothing else was sent
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(self.made(r, cid)[0].status, "canceled")

    def test_one_that_is_only_a_little_late_is_waited_for_and_closed(self):
        # landing after 1 s, it is waited for and closed with the rest
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {}, land=1.0)
        self.assertEqual(flat.get("result"), {"closed": 2}, flat)
        self.assertEqual((r.held("AAPL"), r.held("MSFT")), (0.0, 0.0))
        self.assertEqual(self.notices(r, m, "before flatten_all"), [])

    def test_sent_again_under_the_same_id_it_is_a_new_wish_and_stays(self):
        # The cut-short call said "ask again"; the platform does, AFTER
        # flatten_all. That order is wanted: it is not a late one.
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {}, land=30.0)
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        r.rules.clear()
        m2 = r.mark()
        again = r.req("order.place", {"client_id": cid, "symbol": "AAPL",
                                      "side": "buy", "qty": 10})
        self.assertEqual(again[0], "ok", again)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m2))
        time.sleep(4.0)
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.notices(r, m2, "before flatten_all"), [])
        self.assertEqual(self.sent(r, "AAPL"), [cid])


class SamePosition(SentAndSaid):
    """The adapter is killed inside a re-size (6 of 10 left, their stop
    due and written down); while it is down the account is traded from
    Alpaca's own app; it starts again."""

    RIG = handoff.Piped
    rig = levels.Stopping.rig
    state_file = levels.Stopping.state_file
    kill = levels.Stopping.kill
    killed_inside_the_window = levels.Stopping.killed_inside_the_window

    def elsewhere(self, r, *orders):
        for side, qty in orders:
            st, o = r.other("POST", "/v2/orders", {
                "symbol": "AAPL", "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day"})
            self.assertEqual(st, 200, o)
            time.sleep(0.3)

    def started(self, r):
        r.start()
        time.sleep(3.0)
        return self.notices(r, 0, "NOT put back")

    def test_sold_elsewhere_and_bought_again_is_another_position(self):
        r = self.rig()
        st = self.killed_inside_the_window(r)
        due = st["unheld"]["AAPL"]
        # (written down when the stop was taken off: the position was
        # still 10 then, or already 6 if the closing fill was heard first)
        self.assertEqual(due["side"], "long", due)
        self.assertIn(due["qty"], ("10", "6"), due)
        self.assertTrue(due["avg"] and due["since"], due)
        self.elsewhere(r, ("sell", 6), ("buy", 3))
        self.assertEqual(r.held("AAPL"), 3.0)
        said = self.started(r)
        self.assertEqual(r.live("AAPL"), [])            # nothing placed
        self.assertEqual(len(self.made(r, "brue-sl-")), 1)
        self.assertEqual(r.shown(), (3.0, None, None))
        self.assertTrue(said, r.session())
        self.assertIn("AAPL had a stop at 300", said[0]["detail"])
        self.assertIn("Alpaca now holds a long of 3", said[0]["detail"])

    def test_a_short_opened_elsewhere_gets_no_buy_stop(self):
        r = self.rig()
        self.killed_inside_the_window(r)
        self.elsewhere(r, ("sell", 6), ("sell", 3))
        self.assertEqual(r.held("AAPL"), -3.0)
        said = self.started(r)
        self.assertEqual(r.live("AAPL"), [])
        self.assertEqual(r.held("AAPL"), -3.0)          # not covered
        self.assertEqual(r.shown(), (3.0, None, None))
        self.assertTrue(said, r.session())
        self.assertIn("a short of 3", said[0]["detail"])
        self.assertIn("it was for a long of", said[0]["detail"])

    def test_the_same_position_made_smaller_gets_its_stop_for_what_is_left(self):
        r = self.rig()
        self.killed_inside_the_window(r)
        self.elsewhere(r, ("sell", 2))
        said = self.started(r)
        self.assertEqual(said, [])
        self.assertTrue(until(lambda: r.stops("AAPL") == [(4.0, 300.0)], 4),
                        r.live("AAPL"))
        self.assertEqual(r.shown(), (4.0, 300.0, None))


class PartFilled(SentAndSaid):
    """A bracket of 9; its target fills in three clips. The stand-in
    leaves the held stop leg at 9 meanwhile."""

    def test_a_target_filling_in_parts_is_never_cancelled_to_resize_its_stop(self):
        r = self.rig()
        r.buy("AAPL", 9, sl=300.0, tp=400.0)
        target = [o for o in r.live("AAPL") if o["type"] == "limit"][0]
        m = r.mark()
        r.fake.requests(clear=True)
        r.fake.next_fill(clips=3, interval=3.0)
        r.fake.set_price("AAPL", price=401.0)
        seen = []
        for _ in range(16):                          # 8 s: two clips in
            time.sleep(0.5)
            live = r.live("AAPL")
            seen.append((r.held("AAPL"), sorted(
                (o["type"], o["qty"], o["id"] == target["id"])
                for o in live)))
            if r.held("AAPL") == 0:
                break
            # the SAME target order is still working: never cancelled
            self.assertTrue([o for o in live if o["id"] == target["id"]],
                            seen)
        self.assertTrue(until(lambda: r.held("AAPL") == 0, 8), seen)
        closes = r.events(m, "position.closed")
        self.assertEqual([(c["qty"], c["reason"]) for c in closes],
                         [(3.0, "tp")] * 3, closes)
        self.assertEqual(r.log("DELETE", "/v2/orders"), [])
        self.assertEqual(self.made(r, "brue-tp-"), [])   # no new pair made
        self.assertEqual(self.notices(r, m), [])
        # the stop was brought to size where it stood (the stand-in does
        # not shrink it itself; whether Alpaca does is on the paper list)
        self.assertTrue([s for s in seen if ("stop", 6.0, False) in s[1]]
                        or [s for s in seen if ("stop", 3.0, False) in s[1]],
                        seen)
        self.assertTrue(self.settled(r), self.last)


class Lows(SentAndSaid):
    QUICK = dict(SentAndSaid.QUICK, BYE_TOTAL=1.5, BYE_DRAIN=1.0)

    def test_leaving_with_a_stop_still_due_is_said_before_the_answer_to_bye(self):
        r = handoff.Piped()
        self.rigs.append(r)
        r.long_with_stop("AAPL", 10, 300.0)
        r.rules.add("POST", "/v2/orders", body="brue-sl-", fail=500,
                    count=50, name="no stop")
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        bye = r.send("bye")
        r.answer(bye, timeout=6)
        self.assertEqual(r.stops("AAPL"), [])            # it could not
        with r.wire.cv:
            raw = [json.loads(x) for x in r.wire.raw]
        said = [i for i, x in enumerate(raw) if x.get("type") == "event"
                and x.get("event") == "session.state"
                and "leaving with the stop or target of AAPL" in
                x["data"].get("detail", "")]
        answered = [i for i, x in enumerate(raw) if x.get("id") == bye][0]
        self.assertTrue(said and said[0] < answered,
                        [x for x in raw if x.get("type") == "event"][-4:])
        r.rules.clear()

    def test_live_says_at_start_that_a_bracket_of_an_earlier_run_is_there(self):
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)        # a paper run's bracket
        r.argv = ["--live"]
        r.restart(forget=True)
        said = until(lambda: self.notices(r, 0, "earlier run"), 4)
        self.assertTrue(said, r.session())
        self.assertIn("AAPL", said[0]["detail"])
        self.assertIn("order.cancel", said[0]["detail"])
        self.assertEqual(r.shown(), (10.0, None, None))
        listed = r.ok("orders.list", {"state": "resting"})["orders"]
        # the held stop is in none of Alpaca's open lists: listed all the same
        self.assertEqual(sorted((x["type"], x["side"]) for x in listed),
                         [("limit", "sell"), ("stop", "sell")])
        time.sleep(2.0)
        self.assertEqual(len(r.live("AAPL")), 2)         # nothing touched
        limit = [x for x in listed if x["type"] == "limit"][0]
        r.ok("order.cancel", {"client_id": r.cid("k"),
                              "order_id": limit["order_id"]})
        self.assertTrue(until(lambda: not r.live("AAPL"), 4), r.live())
        m = r.mark()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(r.wait("position.closed", since=m))


if __name__ == "__main__":
    import unittest
    unittest.main()
