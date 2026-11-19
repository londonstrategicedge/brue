"""Alpaca adapter: the long watch for a late order, a stop re-sized beside
a part-filled target, and balance of one moment, one test each.

Run as tests/test_alpaca_after_flatten.py is: the REAL adapter against the
stand-in in this process, the broker's own book beside what the adapter
told the platform. The faults pinned here:

  - an order of unknown fate that flatten_all had been told of was
    forgotten after 30 s without a word; landing later it was neither
    cancelled nor named
    -> watched for ten minutes; SAID when that stops; one heard of
       after that is still cancelled or named
  - a late order that rests was given up on after eight tries in five
    seconds, and what was said of it stayed said after it had ended
    (and went by itself twelve minutes later while it still rested)
    -> tried with a growing pause for ten minutes; then how to cancel
       it is said; withdrawn when the order is seen ended, by anyone
  - the quantity replace of a stop beside a part-filled target was
    tried once, and a stop larger than its position was not said
  - balance was made from two moments whenever the two reads ARRIVED
    apart; a position row without unrealized_pl counted as zero
  - a same-client_id retry of a late order this adapter had cancelled
    was answered "resting"; the notice of a late fill called a
    position flatten_all had closed "yours to deal with"; "still
    working" was said 0.6 s after a cancel Alpaca takes 0.8 s to show
"""
import threading
import time
from unittest.mock import patch

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_changed_meanwhile as changed_meanwhile
from tests import test_alpaca_after_flatten as after_flatten

until = changed_meanwhile.until


class Late(after_flatten.Late):
    def landed(self, r, cid, timeout=8.0):
        got = until(lambda: self.made(r, cid), timeout)
        self.assertTrue(got, "the order never reached the broker")
        return got[0]

    def said_all(self, r, since):
        return [d["detail"] for d in self.notices(r, since,
                                                  "before flatten_all")]

    def told(self, r, since, words, timeout=6.0):
        """The notice about the late order that carries `words`, waited
        for (what is said of it changes as it is dealt with)."""
        got = until(lambda: [d for d in self.said_all(r, since)
                             if words in d], timeout)
        self.assertTrue(got, self.said_all(r, since))
        return got[-1]


class Watched(Late):
    """The order reaches Alpaca after the 30 s at which it used to be
    dropped."""

    def test_one_that_lands_after_half_a_minute_is_still_named(self):
        # after30: a market order. It fills: nothing is placed against
        # it, and it is SAID (it used to be: ten shares, "connected").
        # (38 s: it used to be dropped by the first look-up more than
        # 30 s after its call gave up, 33 to 36 s after it was sent.)
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {}, land=38.0)
        self.assertIn("It is watched", flat.get("error", {}).get(
            "message", ""), flat)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m,
                               timeout=44), r.events(m, "session.state"))
        detail = self.said(r, m, 6.0)
        self.assertIn("buy 10 AAPL (%s)" % cid, detail)
        self.assertIn("FILLED 10 at ", detail)
        self.assertIn("NO order against it", detail)
        time.sleep(2.0)
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])    # and nothing sent

    def test_a_flatten_asked_again_while_it_is_watched_is_not_held_back(self):
        # The decision that goes with the long watch: after UNKNOWN_SEC
        # the order no longer holds flatten_all's own answer back (as it
        # did not when it was dropped at that age); it stays watched.
        for name, value in (("UNKNOWN_SEC", 3.0), ("LATE_WATCH", 60.0)):
            p = patch.object(mod, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {}, land=20.0)
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        self.assertTrue(until(lambda: (r.a._unknown.get(cid) or {}).get(
            "watch"), 8), r.a._unknown)
        # (ten seconds after it was sent the session says so)
        self.assertTrue(until(lambda: "not known yet" in r.session()[
            "detail"], 10), r.session())
        again = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        self.assertEqual(again.get("result"), {"closed": 0}, again)
        # ... and it is still being asked for, and still said
        self.assertIn(cid, r.a._unknown)
        self.assertIn("not known yet", r.session()["detail"])

    def test_when_the_watch_stops_it_is_said_and_one_that_lands_later_is_still_cancelled(self):
        # after30_rest: a limit order far below. The watch is 8 s here;
        # the order lands at 38 s (it used to rest, uncancelled, unsaid).
        for name, value in (("UNKNOWN_SEC", 3.0), ("LATE_WATCH", 8.0),
                            ("NOTICE_SEC", 4.0)):
            p = patch.object(mod, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        r = self.rig()
        m, cid, placed, flat, t = self.late(
            r, {"type": "limit", "price": 200.0}, land=38.0)
        self.assertIn("It is watched", flat.get("error", {}).get(
            "message", ""), flat)
        stopped = until(lambda: [d for d in self.said_all(r, m)
                                 if "STOPPED asking" in d], 20)
        self.assertTrue(stopped, r.events(m, "session.state"))
        # naming the symbol, the side, the quantity and the client id
        self.assertIn("buy 10 AAPL (%s)" % cid, stopped[-1])
        self.assertIn("has NOT arrived at Alpaca in 8 s", stopped[-1])
        self.assertEqual(self.made(r, cid), [])          # not there yet
        order = self.landed(r, cid, 34.0)
        self.assertTrue(until(lambda: order.status == "canceled", 10),
                        order.status)
        self.told(r, m, "is cancelled; nothing of it filled")
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def test_sent_again_after_the_watch_was_given_up_it_is_a_new_wish_and_stays(self):
        # (a guard for the registry of given-up ids: the platform sends
        # the order again under its id, as it was told it may. That is
        # an order asked for NOW, not the late one turning up.)
        for name, value in (("UNKNOWN_SEC", 2.0), ("LATE_WATCH", 4.0)):
            p = patch.object(mod, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        r = self.rig()
        r.buy("MSFT", 5)
        # refused WITHOUT being carried out, which the adapter cannot know
        r.rules.add("POST", "/v2/orders", body='"AAPL"', fail=500, count=1,
                    name="P 500")
        m, cid = r.mark(), r.cid("P")
        p = {"client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10,
             "type": "limit", "price": 200.0}
        kind, out = r.req("order.place", p)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        flat = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        self.told(r, m, "STOPPED asking", 16)
        self.assertIn(cid, r.a._dropped)
        self.assertEqual(r.req("order.place", p)[0], "ok")
        order = self.landed(r, cid)
        time.sleep(3.0)
        self.assertEqual(order.status, "new")            # it rests, wanted
        self.assertEqual((r.a._dropped, r.a._late_due), ({}, {}))
        self.assertEqual(self.sent(r, "AAPL"), [cid])


class Resting(Late):
    """A late LIMIT order rests, and Alpaca answers 500 to every DELETE of
    one order for a while."""

    def resting(self, r):
        m, cid, placed, flat, t = self.late(
            r, {"type": "limit", "price": 200.0})
        bad = r.rules.add("DELETE", "/v2/orders/", fail=500, count=10 ** 6,
                          name="DELETE 500")
        return m, cid, bad, self.landed(r, cid)

    def test_it_is_cancelled_once_alpaca_lets_it_however_long_that_takes(self):
        r = self.rig()
        m, cid, bad, order = self.resting(r)
        time.sleep(12.0)                 # the outage: 12 s
        self.assertEqual(order.status, "new")
        self.assertGreater(len(r.rules.hit("DELETE 500")), 2)
        bad["count"] = 0
        # the next try is at most 16 s away (0.5 s doubling)
        self.assertTrue(until(lambda: order.status == "canceled", 20),
                        (order.status, self.said_all(r, m)))
        self.told(r, m, "is cancelled; nothing of it filled")
        self.assertEqual([d for d in self.said_all(r, m) if "STOPPED" in d],
                         [])
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def stopped(self, r):
        """The same with the outage outlasting the watch (6 s here)."""
        p = patch.object(mod, "LATE_WATCH", 6.0, create=True)
        p.start()
        self.addCleanup(p.stop)
        m, cid, bad, order = self.resting(r)
        got = until(lambda: [d for d in self.said_all(r, m)
                             if "STOPPED trying" in d], 16)
        self.assertTrue(got, self.said_all(r, m))
        return m, cid, bad, order, got[-1]

    def test_when_it_stops_trying_it_says_how_to_cancel_the_order(self):
        r = self.rig()
        m, cid, bad, order, last = self.stopped(r)
        self.assertIn("order.cancel with order_id %s" % order.id, last)
        self.assertIn("Look at AAPL NOW", last)
        self.assertNotIn("tried again", last)
        self.assertEqual(order.status, "new")
        resting = r.ok("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([o["order_id"] for o in resting], [order.id])

    def test_what_was_said_is_withdrawn_when_the_user_cancels_it_himself(self):
        r = self.rig()
        m, cid, bad, order, last = self.stopped(r)
        bad["count"] = 0
        self.assertEqual(r.req("order.cancel", {
            "client_id": r.cid("k"), "order_id": order.id}), ("ok", {}))
        self.assertTrue(until(lambda: order.status == "canceled", 4))
        # (it used to say "could not be read or cancelled" for twelve
        # minutes more)
        self.assertTrue(until(lambda: r.session()["state"] == "connected",
                              4), r.session())
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def test_still_working_is_not_said_of_a_cancel_alpaca_shows_after_most_of_a_second(self):
        # Seen: Alpaca took 0.8 s to show a cancel. The DELETE is
        # answered at once and carried out 0.9 s later.
        r = self.rig()
        inner = r.rules._answer

        def answer(method, path, q, data, headers, service):
            if method == "DELETE" and path.startswith("/v2/orders/"):
                threading.Timer(0.9, inner, (method, path, q, data, headers,
                                             service)).start()
                return 204, {}, None
            return inner(method, path, q, data, headers, service)
        r.rules._answer = answer
        m, cid, placed, flat, t = self.late(
            r, {"type": "limit", "price": 200.0})
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: order.status == "canceled", 8),
                        order.status)
        self.told(r, m, "is cancelled; nothing of it filled")
        self.assertEqual([d for d in self.said_all(r, m)
                          if "still working" in d], [])


class Lows(Late):
    def test_a_retry_of_a_late_order_this_adapter_cancelled_is_told_so(self):
        # The declared case: it used to answer {"status": "resting"} for
        # an order that no longer exists.
        r = self.rig()
        p = {"symbol": "AAPL", "side": "buy", "qty": 10, "type": "limit",
             "price": 200.0}
        m, cid, placed, flat, t = self.late(r, {"type": "limit",
                                                "price": 200.0})
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: order.status == "canceled", 8),
                        order.status)
        self.told(r, m, "is cancelled; nothing of it filled")
        kind, out = r.req("order.place", dict(p, client_id=cid))
        self.assertEqual(kind, "err", out)
        self.assertEqual((out["code"], out["retryable"]),
                         ("x_alpaca_cancelled_after_flatten", False), out)
        self.assertIn("flatten_all", out["message"])
        self.assertIn("NEW client_id", out["message"])
        self.assertEqual(self.sent(r, "AAPL"), [cid])    # nothing sent
        # one terminal event for the order, and it was the cancel
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertEqual(len([d for d in r.events(m, "order.cancelled")
                              if d["client_id"] == cid]), 1)

    def test_the_notice_of_a_late_fill_says_what_the_symbol_holds_now(self):
        # It landed and filled after flatten_all had answered: the ten
        # shares are there, and that is what is said.
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {})
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m,
                               timeout=8), r.events(m))
        detail = self.said(r, m)
        self.assertIn("AAPL holds 10 long at Alpaca at this moment", detail)
        self.assertIn("yours to deal with", detail)
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def test_a_position_flatten_has_closed_is_not_called_yours_to_deal_with(self):
        # P filled BEFORE flatten_all, which closed its ten and answered
        # {"closed": 2}. What is said afterwards used to end "what AAPL
        # holds now is yours to deal with".
        r = self.rig()
        r.buy("MSFT", 5)
        r.rules.add("POST", "/v2/orders", body='"AAPL"', drop=True, count=1,
                    name="P answer lost")
        lost = r.rules.add("GET", "/v2/orders:by_client_order_id", drop=True,
                           count=40, name="look-ups lost")
        r.rules.add("DELETE", "/v2/orders", before=1.5, count=1,
                    name="slow cancel-all")
        m, cid = r.mark(), r.cid("P")
        r.send("order.place", {"client_id": cid, "symbol": "AAPL",
                               "side": "buy", "qty": 10})
        time.sleep(0.3)
        flat = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        self.assertEqual(flat.get("result"), {"closed": 2}, flat)
        lost["count"] = 0
        detail = self.said(r, m, 8.0)
        self.assertIn("FILLED 10 at ", detail)
        self.assertIn("AAPL holds nothing at Alpaca at this moment", detail)
        self.assertNotIn("yours to deal with", detail)
        self.assertEqual(r.held("AAPL"), 0.0)


class Resize(changed_meanwhile.SentAndSaid):
    """A bracket of 9; its target fills 3 and waits; the stand-in leaves
    the held stop leg at 9 (whether Alpaca shrinks it itself has not been
    observed). About 5 s later the adapter asks Alpaca to make the stop
    6."""

    def part_filled(self, r):
        px = r.fake.state()["prices"]["AAPL"]
        r.fake.set_price("AAPL", price=px)
        sl, tp = round(px - 50, 2), round(px + 50, 2)
        r.buy("AAPL", 9, sl=sl, tp=tp)
        time.sleep(0.8)
        m = r.mark()
        r.fake.requests(clear=True)
        r.fake.next_fill(clips=3, interval=60.0)
        r.fake.set_price("AAPL", price=round(px + 51, 2))
        self.assertTrue(until(lambda: r.held("AAPL") == 6.0, 5),
                        r.held("AAPL"))
        self.assertEqual(r.stops("AAPL"), [(9.0, sl)])
        return m, sl

    def oversize(self, r, since):
        return [d["detail"] for d in self.notices(r, since, "more than the "
                                                  "position")]

    def test_a_replace_answered_500_is_asked_again_until_the_stop_fits(self):
        r = self.rig()
        r.rules.add("PATCH", "/v2/orders/", fail=500, count=1,
                    name="PATCH 500")
        m, sl = self.part_filled(r)
        # (it used to be tried once: the stop stayed at 9 for good)
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, sl)], 16),
                        (r.stops("AAPL"), r.log("PATCH")))
        self.assertEqual(len(r.rules.hit("PATCH 500")), 1)
        self.assertEqual(r.held("AAPL"), 6.0)
        # nothing stays said once it fits
        self.assertTrue(until(lambda: r.session()["state"] == "connected",
                              6), r.session())
        self.assertEqual(r.shown("AAPL")[:2], (6.0, sl))

    def test_a_stop_alpaca_will_not_make_smaller_is_said_until_it_is_no_longer_so(self):
        r = self.rig()
        r.rules.add("PATCH", "/v2/orders/", fail=403, count=10 ** 6,
                    name="PATCH 403")
        m, sl = self.part_filled(r)
        said = until(lambda: [d for d in self.oversize(r, m)
                              if "would not make it smaller" in d], 14)
        self.assertTrue(said, r.events(m, "session.state"))
        # naming the symbol and both quantities, and what it means
        self.assertIn("AAPL: its stop at Alpaca is for 9 and the position "
                      "is 6", said[-1])
        self.assertIn("sells 3 more than the position", said[-1])
        self.assertEqual(r.stops("AAPL"), [(9.0, sl)])
        # refused for good: asked once, not again
        time.sleep(7.0)
        self.assertEqual(len(r.rules.hit("PATCH 403")), 1)
        self.assertEqual(r.session()["state"], "degraded")
        # ... and withdrawn when it is no longer so: the position closed
        r.rules.clear()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL"})
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 6))
        self.assertTrue(until(lambda: r.session()["state"] == "connected",
                              8), r.session())
        self.assertEqual(r.live("AAPL"), [])

    def test_not_yet_is_asked_three_times_and_then_taken_for_a_refusal(self):
        # A 422 that names neither a price nor a stop is
        # "too early" to _replace (SPEC 6 `frozen`). Three of them in a
        # row for one size are Alpaca's answer.
        r = self.rig()
        r.rules.add("PATCH", "/v2/orders/", fail=422, count=10 ** 6,
                    name="PATCH 422")
        m, sl = self.part_filled(r)
        self.assertTrue(until(lambda: len(r.rules.hit("PATCH 422")) == 3,
                              24), r.rules.hit("PATCH 422"))
        said = until(lambda: [d for d in self.oversize(r, m)
                              if "would not make it smaller" in d], 6)
        self.assertTrue(said, self.oversize(r, m))
        # while it was still being asked, that is what was said
        self.assertTrue([d for d in self.oversize(r, m)
                         if "being made smaller" in d], self.oversize(r, m))
        time.sleep(7.0)
        self.assertEqual(len(r.rules.hit("PATCH 422")), 3)
        self.assertEqual(r.stops("AAPL"), [(9.0, sl)])
        r.rules.clear()


class Balance(changed_meanwhile.SentAndSaid):
    """300 AAPL, the price falling half a percent a second, nothing
    realised; ONE of the two reads of an account.get reaches the broker
    200 ms after the other."""

    def truth(self, r):
        """Cash and the cost of what is held, in one look at the
        stand-in: what `balance` is while nothing is realised."""
        b = r.fake.broker
        with b.lock:
            return float(b.cash + sum(p[2] for p in b.positions.values()))

    def reads(self, r):
        return (len(r.log("GET", "/v2/account")),
                len([q for q in r.log("GET", "/v2/positions")
                     if q["path"] == "/v2/positions"]))

    def test_reads_that_arrive_apart_are_read_again_and_balance_is_of_one_moment(self):
        r = self.rig()
        r.buy("AAPL", 300)
        time.sleep(1.5)                  # the backstop's own reads are done
        r.fake.set_price("AAPL", price=350.0)
        time.sleep(0.4)
        r.fake.set_price("AAPL", trend_pct_per_s=-0.5, seconds=40)
        time.sleep(0.5)
        truth, off, reads = self.truth(r), [], []
        for path in ("/v2/positions", "/v2/account") * 3:
            r.fake.requests(clear=True)
            late = r.rules.add("GET", path, before=0.2, count=1,
                               name="late " + path)
            acct = r.ok("account.get")
            self.assertEqual(late["count"], 0)       # the fault was met
            off.append(round(acct["balance"] - truth, 2))
            reads.append(self.reads(r))
            time.sleep(0.3)
        # (it was +105 and -105: 300 shares times 200 ms of the fall;
        # two requests sent together land a few milliseconds apart)
        self.assertLess(max(abs(x) for x in off), 15.0, off)
        # both were read again, once, for each of those answers
        self.assertEqual(reads, [(2, 2)] * 6)

    def test_reads_of_one_moment_are_not_read_twice(self):
        # (a guard, true before as well: no extra request when the two
        # reads agree, which is every call in a market that stands still)
        r = self.rig()
        r.buy("AAPL", 300)
        r.buy("BTC/USD", 0.01)
        time.sleep(1.5)
        for sym in ("AAPL", "BTC/USD"):
            r.fake.set_price(sym, price=r.fake.state()["prices"][sym])
        time.sleep(0.3)
        truth = self.truth(r)
        for _ in range(3):
            r.fake.requests(clear=True)
            acct = r.ok("account.get")
            self.assertEqual(self.reads(r), (1, 1), r.log("GET"))
            self.assertAlmostEqual(acct["balance"], truth, delta=0.03)

    ACCT = {"equity": "100100", "initial_margin": "0", "currency": "USD",
            "long_market_value": "3600", "short_market_value": "0"}
    ROW = {"symbol": "AAPL", "side": "long", "qty": "10",
           "avg_entry_price": "350", "market_value": "3600",
           "cost_basis": "3500", "unrealized_pl": "100"}

    def test_the_cross_check_is_by_magnitude_and_the_rows_side(self):
        one = mod.AlpacaAdapter._one_moment
        self.assertTrue(one(self.ACCT, [self.ROW])[0])
        self.assertTrue(one(dict(self.ACCT, long_market_value="3600.01"),
                            [self.ROW])[0])              # the cent it rounds
        self.assertFalse(one(dict(self.ACCT, long_market_value="3601.05"),
                             [self.ROW])[0])             # 1.05: two prices
        self.assertFalse(one(self.ACCT, [])[0])          # a fill between
        # a short, whichever sign Alpaca gives its market value
        for acct_mv, row_mv in (("-950", "-950"), ("950", "950"),
                                ("-950", "950")):
            acct = dict(self.ACCT, long_market_value="3600",
                        short_market_value=acct_mv)
            rows = [self.ROW, {"symbol": "MSFT", "side": "short",
                               "qty": "-10", "market_value": row_mv}]
            self.assertTrue(one(acct, rows)[0], (acct_mv, row_mv))
            self.assertFalse(one(acct, rows[:1])[0], (acct_mv, row_mv))
        # what cannot be told is not called two moments
        self.assertTrue(one({"equity": "1"}, [self.ROW])[0])
        self.assertTrue(one(self.ACCT, [{"symbol": "X", "qty": "1"}])[0])

    def scripted(self, pairs):
        """An adapter whose account and positions reads are answered
        from `pairs`, in order (the last repeats)."""
        from tests import test_alpaca_orders as orders
        a, b = orders.make()
        inner = b.call

        def call(method, host, path, query=None, body=None, **kw):
            out = inner(method, host, path, query, body)     # or raises
            if kw.get("ready"):
                kw["ready"]()
            return out
        a.client.call = call
        b.on("GET", "/v2/account", [orders.Step(p[0]) for p in pairs])
        b.on("GET", "/v2/positions", [orders.Step(p[1]) for p in pairs])
        return a, b

    def test_the_second_pair_is_the_answer_and_a_second_read_that_fails_leaves_the_first(self):
        skewed = (dict(self.ACCT, equity="100205", long_market_value="3705"),
                  [self.ROW])
        good = (self.ACCT, [self.ROW])
        later = (dict(self.ACCT, equity="100310", long_market_value="3810"),
                 [dict(self.ROW, market_value="3700", unrealized_pl="200")])
        # two moments, then one: the pair that agrees
        a, b = self.scripted([skewed, good])
        self.assertAlmostEqual(a.handle_account_get({})["balance"], 100000.0)
        self.assertEqual(len(b.sent("GET", "/v2/account")), 2)
        # two moments twice: the later pair, and no third read
        a, b = self.scripted([skewed, later])
        self.assertAlmostEqual(a.handle_account_get({})["balance"], 100110.0)
        self.assertEqual(len(b.sent("GET", "/v2/positions")), 2)
        # the second read refused: the first pair, as before this check
        a, b = self.scripted([skewed, (mod.AlpacaError(
            429, "more urgent ones went first", code="budget"), [])])
        self.assertAlmostEqual(a.handle_account_get({})["balance"], 100105.0)

    def test_a_row_without_its_open_profit_is_never_counted_as_zero(self):
        view = mod.AlpacaAdapter._account_view
        bare = {k: v for k, v in self.ROW.items() if k != "unrealized_pl"}
        # worked out from the row's market value and cost
        self.assertAlmostEqual(view(self.ACCT, [bare])["balance"], 100000.0)
        self.assertAlmostEqual(view(self.ACCT, [dict(
            bare, unrealized_pl=None)])["balance"], 100000.0)
        # a short that has gained 50, whichever signs its figures carry
        for cost, mv in (("-1000", "-950"), ("1000", "950"),
                         ("1000", "-950")):
            row = {"symbol": "MSFT", "side": "short", "qty": "-10",
                   "cost_basis": cost, "market_value": mv}
            self.assertAlmostEqual(view(self.ACCT, [row])["balance"],
                                   100050.0, msg=(cost, mv))
        # neither: there is no true balance, and that is what is said
        for row in ({"symbol": "HALT", "side": "long", "qty": "10"},
                    dict(bare, cost_basis=None),
                    dict(bare, market_value="")):
            with self.assertRaises(mod.AdapterError) as ctx:
                view(self.ACCT, [row])
            e = ctx.exception
            self.assertEqual((e.code, e.retryable, e.like),
                             ("x_alpaca_no_pnl", True, "no_price"))
            self.assertIn(row["symbol"], str(e))
        # ... through the call itself, and it is not the adapter's fault
        a, b = self.scripted([(self.ACCT, [{"symbol": "HALT", "side": "long",
                                            "qty": "10"}])])
        with self.assertRaises(mod.AdapterError) as ctx:
            a.handle_account_get({})
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("unrealized_pl", str(ctx.exception))


if __name__ == "__main__":
    import unittest
    unittest.main()
