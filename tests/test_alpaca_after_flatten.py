"""Alpaca adapter: an order from before a flatten_all that turns up after
it, and balance read while fills arrive, one test each.

Run as tests/test_alpaca_changed_meanwhile.py is: the REAL adapter against
the stand-in in this process, the broker's own book beside what the
adapter told the platform. The faults pinned here:

  - the adapter's own "undo" order sold a position the user had
    opened on purpose after flatten_all (the late order had filled
    BEFORE the flatten, which had closed it already)
  - a lost answer to that undo order made it close MORE than the
    late order had filled
    -> the rule now: this adapter never sends an order nobody asked
       for. An order from before a flatten that turns up afterwards
       is cancelled if it rests and SAID if it has filled.
  - balance was off by the whole value of the shares that filled
    between its two reads
    -> equity less the positions' open P&L, the two read TOGETHER
  - the interrupted order asked for again under the same
    client_id once it has landed: the answer is that order, from
    Alpaca, and nothing is done to it afterwards
"""
import threading
import time
from unittest.mock import patch

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_orders as orders
from tests import test_alpaca_changed_meanwhile as changed_meanwhile

until = changed_meanwhile.until


class Late(changed_meanwhile.SentAndSaid):
    """MSFT 5 held; order.place AAPL buy 10 (client id P); flatten_all
    read 0.3 s later. Scaled as changed_meanwhile.LateOrder is: the order
    call has 2.5 s, flatten 3 s."""

    QUICK = changed_meanwhile.LateOrder.QUICK
    late = changed_meanwhile.LateOrder.late

    def said(self, r, since, timeout=8.0):
        got = until(lambda: self.notices(r, since, "before flatten_all"),
                    timeout)
        self.assertTrue(got, r.events(since, "session.state"))
        return got[-1]["detail"]

    def filled_at(self, r, cid):
        """When Alpaca's own record of the order says it filled."""
        _, o = r.other("GET", "/v2/orders/" + self.made(r, cid)[0].id)
        return o["filled_at"]


class NoOrderOfItsOwn(Late):
    """This adapter places no order of its own against a late order,
    whatever has filled of it and whatever answers are lost."""

    def test_a_position_opened_after_flatten_is_never_sold_for_an_order_from_before_it(self):
        # P is carried out and FILLS at once, but its answer and
        # the look-ups are lost; flatten closes what it sees, P's ten
        # included; then the user buys ten AAPL on purpose.
        r = self.rig()
        r.buy("MSFT", 5)
        r.rules.add("POST", "/v2/orders", body='"AAPL"', drop=True, count=1,
                    name="P answer lost")
        lost = r.rules.add("GET", "/v2/orders:by_client_order_id", drop=True,
                           count=40, name="look-ups lost")
        # (At Alpaca's pace flatten takes two seconds, and the backstop is
        # the one that finds P, from the stream, while it runs. Here the
        # stand-in answers at once, so flatten is held up to the same
        # effect; and the backstop's read of P is slow, so that the
        # user's order is in before it has looked.)
        r.rules.add("DELETE", "/v2/orders", before=1.5, count=1,
                    name="slow cancel-all")
        r.rules.add("GET", "/v2/orders/", before=2.0, count=1,
                    name="slow read of P")
        m, cid = r.mark(), r.cid("P")
        r.send("order.place", {"client_id": cid, "symbol": "AAPL",
                               "side": "buy", "qty": 10})
        time.sleep(0.3)
        flat = r.answer(r.send("flatten_all", {"client_id": r.cid("F")}))
        self.assertEqual(flat.get("result"), {"closed": 2}, flat)
        self.assertEqual((r.held("AAPL"), r.held("MSFT")), (0.0, 0.0))
        lost["count"] = 0
        new, _ = r.buy("AAPL", 10)           # on purpose, a new client id
        time.sleep(6.0)
        # the user's ten are still there, and nobody sold anything
        self.assertEqual(r.held("AAPL"), 10.0)
        mine = [c for c in self.sent(r, "AAPL") if c.startswith("brue-")]
        self.assertEqual(len(mine), 1, self.sent(r, "AAPL"))  # flatten's own
        self.assertEqual(sorted(self.sent(r, "AAPL")),
                         sorted([cid, new] + mine))
        self.assertEqual([c["qty"] for c in self.closes(r, m, "AAPL")],
                         [10.0])                         # flatten's, once
        self.assertEqual(r.shown("AAPL")[0], 10.0)
        # P was looked at (the slow read is the backstop's), and what is
        # said of it is true: when it filled, and nothing "closed again".
        self.assertTrue(r.rules.hit("slow read of P"))
        detail = self.said(r, m, 2.0)
        self.assertIn("buy 10 AAPL", detail)
        self.assertIn("FILLED 10 at %s by Alpaca's record" % mod.iso_ms(
            mod.parse_ts(self.filled_at(r, cid))), detail)
        self.assertNotIn("closed", detail)
        self.assertNotIn("after flatten_all had answered", detail)

    def test_no_order_follows_a_late_fill_whatever_answers_are_lost(self):
        # P reaches Alpaca late; flatten has answered "not known
        # to be flat"; the user buys five AAPL on purpose; P lands: 15.
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {})
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        new, _ = r.buy("AAPL", 5)
        # (the fault injected: the answer to an order of the adapter's own
        # and one look-up are lost. The adapter sends no such order.)
        r.rules.add("POST", "/v2/orders", body="brue-flat-", drop=True,
                    count=5, name="undo answer lost")
        r.rules.add("GET", "/v2/orders:by_client_order_id", drop=True,
                    count=2, name="look-up lost")
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m,
                               timeout=8), r.events(m))
        time.sleep(7.0)
        self.assertEqual(r.held("AAPL"), 15.0)
        self.assertEqual(sorted(self.sent(r, "AAPL")), sorted([cid, new]))
        self.assertEqual(r.rules.hit("undo answer lost"), [])
        self.assertEqual(self.closes(r, m, "AAPL"), [])
        self.assertEqual(r.shown("AAPL")[0], 15.0)
        detail = self.said(r, m, 2.0)
        self.assertIn("buy 10 AAPL", detail)
        self.assertIn("FILLED 10 at %s by Alpaca's record" % mod.iso_ms(
            mod.parse_ts(self.filled_at(r, cid))), detail)
        self.assertNotIn("closed", detail)

    def test_a_late_order_filled_in_part_is_cancelled_for_the_rest_and_said(self):
        # P is for nine and fills three
        # before the backstop has found it.
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {"qty": 9}, land=5.0)
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        r.fake.next_fill(clips=3, interval=6.0)      # for P, when it lands
        detail = self.said(r, m, 10.0)
        self.assertTrue(until(lambda: not r.live("AAPL"), 4), r.live())
        time.sleep(3.0)
        self.assertEqual(r.held("AAPL"), 3.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(self.made(r, cid)[0].status, "canceled")
        self.assertEqual(self.closes(r, m, "AAPL"), [])
        self.assertIn("buy 9 AAPL", detail)
        self.assertIn("FILLED 3 of 9 at ", detail)
        self.assertIn("the rest of it was cancelled", detail)
        self.assertIn("NO order against it", detail)

    def test_a_late_sell_that_opens_a_short_is_said_and_left_alone(self):
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {"side": "sell"})
        self.assertIn("sell 10 AAPL", flat.get("error", {}).get(
            "message", ""), flat)
        detail = self.said(r, m)
        time.sleep(3.0)
        self.assertEqual(r.held("AAPL"), -10.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(self.closes(r, m, "AAPL"), [])
        self.assertIn("sell 10 AAPL", detail)
        self.assertIn("FILLED 10 at %s by Alpaca's record" % mod.iso_ms(
            mod.parse_ts(self.filled_at(r, cid))), detail)

    def stream_down(self, r):
        """Nothing tells of an order but Alpaca being asked for it."""
        def refuse(h):
            h.send_response(503)
            h.send_header("Content-Length", "0")
            h.end_headers()
            h.close_connection = True
        r.fake._upgrade = refuse
        r.fake.drop_streams()
        self.assertTrue(until(lambda: r.session()["state"] == "degraded"
                              and "stream" in r.session()["detail"], 15),
                        r.session())
        time.sleep(1.2)       # the backstop's first read after the drop

    def test_one_found_by_asking_alpaca_not_by_the_stream_is_cancelled_all_the_same(self):
        # The order is handed on whoever finds it, not only when the
        # STREAM has told of it. Looked up by its client id (the stream
        # down, or slower than the look-up), it is not adopted and then
        # neither cancelled nor mentioned.
        r = self.rig()
        self.stream_down(r)
        m, cid, placed, flat, t = self.late(
            r, {"type": "limit", "price": 200.0})
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        self.assertTrue(until(lambda: self.made(r, cid), 6))   # it arrives
        self.assertTrue(until(
            lambda: self.made(r, cid)[0].status == "canceled", 8),
            self.made(r, cid)[0].status)
        detail = self.said(r, m)
        self.assertIn("buy 10 AAPL", detail)
        self.assertIn("is cancelled; nothing of it filled", detail)
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def test_one_found_by_asking_alpaca_that_has_filled_is_said_all_the_same(self):
        r = self.rig()
        self.stream_down(r)
        m, cid, placed, flat, t = self.late(r, {})
        self.assertIn("may still arrive", flat.get("error", {}).get(
            "message", ""), flat)
        detail = self.said(r, m, 12.0)
        self.assertIn("buy 10 AAPL", detail)
        self.assertIn("FILLED 10 at %s by Alpaca's record" % mod.iso_ms(
            mod.parse_ts(self.filled_at(r, cid))), detail)
        time.sleep(3.0)
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(self.closes(r, m, "AAPL"), [])

    def test_what_cannot_be_read_is_said_truthfully_to_the_last_try(self):
        # The order rests at Alpaca and cannot be read by its id: said
        # while it is tried, and said DIFFERENTLY once it is given up.
        # (It is tried for LATE_WATCH, ten minutes, with a growing pause.
        # Six seconds here.)
        watch = patch.object(mod, "LATE_WATCH", 6.0, create=True)
        watch.start()
        self.addCleanup(watch.stop)
        r = self.rig()
        m, cid, placed, flat, t = self.late(
            r, {"type": "limit", "price": 200.0})
        r.rules.add("GET", "/v2/orders/", fail=500, count=200,
                    name="no read by id")
        trying = until(lambda: [d for d in self.notices(
            r, m, "before flatten_all") if "tried again" in d["detail"]], 10)
        self.assertTrue(trying, r.events(m, "session.state"))
        # tried at 0.5, 1.5, 3.5 and 7.5 s; then it is no longer tried,
        # and "is being tried again" may not be what the session says
        time.sleep(12.0)
        last = self.notices(r, m, "before flatten_all")[-1]["detail"]
        self.assertNotIn("tried again", last)
        self.assertIn("STOPPED trying", last)
        self.assertIn("Look at AAPL NOW", last)
        self.assertIn("could not be read or cancelled", trying[-1]["detail"])
        self.assertEqual(self.sent(r, "AAPL"), [cid])    # and nothing sent
        r.rules.clear()


class AskedAgain(Late):
    """The platform retries the interrupted order, same client_id, once it
    has landed."""

    P = {"symbol": "AAPL", "side": "buy", "qty": 10}

    def test_once_it_has_landed_the_answer_is_that_order_and_its_position_stays(self):
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {})
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m,
                               timeout=8), r.events(m))
        self.said(r, m)                  # it was dealt with: said, no order
        time.sleep(1.0)
        r.fake.requests(clear=True)
        kind, out = r.req("order.place", dict(self.P, client_id=cid))
        self.assertEqual(kind, "ok", out)
        orders = self.made(r, cid)
        self.assertEqual(len(orders), 1)
        # the original order's outcome, as Alpaca holds it ...
        self.assertEqual(out, {"order_id": orders[0].id,
                               "status": "accepted"})
        # ... ASKED of Alpaca for this answer, and nothing sent again
        self.assertEqual(len(r.log("GET", "/v2/orders:by_client_order_id")),
                         1, r.log())
        self.assertEqual(r.log("POST", "/v2/orders"), [])
        time.sleep(3.0)
        # and it is true: the ten shares are there and stay there
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(self.closes(r, m, "AAPL"), [])
        self.assertEqual(r.shown("AAPL")[0], 10.0)
        fills = [d for d in r.events(m, "order.filled")
                 if d["client_id"] == cid]
        self.assertEqual([(d["order_id"], d["qty"]) for d in fills],
                         [(orders[0].id, 10.0)])         # told once
        # a third time: the same answer, still nothing sent
        self.assertEqual(r.req("order.place", dict(self.P, client_id=cid)),
                         ("ok", out))
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def test_asked_for_again_before_it_was_dealt_with_it_is_wanted_and_stays(self):
        # It has landed and rests; the backstop has not got to it yet
        # (it cannot read it by its id). The platform asks for it again:
        # it is answered from Alpaca, and it is not cancelled afterwards.
        r = self.rig()
        p = dict(self.P, type="limit", price=200.0)
        m, cid, placed, flat, t = self.late(r, {"type": "limit",
                                                "price": 200.0})
        r.rules.add("GET", "/v2/orders/", fail=500, count=200,
                    name="no read by id")
        self.assertTrue(until(lambda: [d for d in self.notices(
            r, m, "before flatten_all") if "tried again" in d["detail"]],
            10), r.events(m, "session.state"))
        kind, out = r.req("order.place", dict(p, client_id=cid))
        r.rules.clear()
        self.assertEqual(kind, "ok", out)
        order = self.made(r, cid)[0]
        self.assertEqual(out, {"order_id": order.id, "status": "resting"})
        time.sleep(4.0)
        self.assertEqual([o["coid"] for o in r.live("AAPL")], [cid])
        self.assertEqual(self.sent(r, "AAPL"), [cid])
        self.assertEqual(r.events(m, "order.cancelled"), [])
        # what was said while it was being tried is unsaid: nothing is
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 4), r.session())


class Balance(changed_meanwhile.SentAndSaid):
    """Fills between the two reads, and the price that moves between
    them. At ALPACA's pace: the rig's forty requests a second hide what
    three a second do to two reads."""

    def paced(self, r):
        """Alpaca's pace, as the adapter runs it outside these tests; and
        when each of the two reads of an account.get reached the broker."""
        r.a.client.budget.per_second = mod.PACE_PER_SECOND
        seen, inner = [], r.fake._answer

        def answer(method, path, *rest):
            if method == "GET" and path in ("/v2/account", "/v2/positions"):
                seen.append((time.monotonic(), path))
            return inner(method, path, *rest)
        r.fake._answer = answer
        return seen

    def broker(self, r):
        _, pos = r.other("GET", "/v2/positions")
        _, acct = r.other("GET", "/v2/account")
        return sum(float(p["unrealized_pl"]) for p in pos), acct

    def test_balance_is_equity_less_the_rows_open_profit_and_nothing_else(self):
        # No cash, no market value, no cost basis: figures that move by
        # the whole value of a fill, each in its own read.
        acct = {"equity": "100500", "initial_margin": "0", "cash": "-7",
                "long_market_value": "123456", "short_market_value": "-9"}
        rows = [{"side": "long", "qty": "10", "avg_entry_price": "100",
                 "cost_basis": "999999", "market_value": "1",
                 "unrealized_pl": "700"},
                {"side": "short", "qty": "-5", "avg_entry_price": "50",
                 "cost_basis": "-5", "market_value": "-888",
                 "unrealized_pl": "-200"}]
        view = mod.AlpacaAdapter._account_view(acct, rows)
        self.assertAlmostEqual(view["balance"], 100000.0, places=6)
        self.assertAlmostEqual(view["equity"], 100500.0, places=6)
        flat = mod.AlpacaAdapter._account_view(acct, [])
        self.assertAlmostEqual(flat["balance"], flat["equity"], places=6)

    def test_one_read_failing_fails_the_call_and_keeps_the_other_waiting_for_nothing(self):
        # Each of the two waits, once it has its place in the pace, for
        # the other to have one. One that never gets a place (refused
        # before it is sent) must not hold the other back for its time.
        a, b = orders.make()
        inner = b.call

        def call(method, host, path, query=None, body=None, **kw):
            out = inner(method, host, path, query, body)     # or raises
            if kw.get("ready"):
                kw["ready"]()
            return out
        a.client.call = call
        refused = mod.AlpacaError(429, "more urgent ones went first",
                                  code="budget")
        for path in ("/v2/positions", "/v2/account"):
            b.on("GET", "/v2/account", dict(orders.ACCOUNT))
            b.on("GET", "/v2/positions", [])
            b.on("GET", path, refused)
            t = time.monotonic()
            with self.assertRaises(mod.AdapterError) as ctx:
                a.handle_account_get({})         # no balance from one half
            self.assertEqual(ctx.exception.code, "rate_limited")
            self.assertLess(time.monotonic() - t, 1.0, path)
        b.on("GET", "/v2/account", dict(orders.ACCOUNT))
        b.on("GET", "/v2/positions", [])
        view = a.handle_account_get({})
        self.assertAlmostEqual(view["balance"], view["equity"], places=6)

    def test_a_resting_crypto_buy_beside_a_held_share_does_not_move_balance(self):
        # Seen on paper (2026-11-10 23:09 UTC): a resting crypto buy
        # lowers Alpaca's `cash` by its cost at once, and not `equity`.
        r = self.rig()
        r.buy("MSFT", 20)
        # (its price held still: nothing at all may move then)
        r.fake.set_price("MSFT", price=r.fake.state()["prices"]["MSFT"])
        before = r.ok("account.get")
        _, raw0 = self.broker(r)
        kind, out = r.req("order.place", {
            "client_id": r.cid(), "symbol": "BTC/USD", "side": "buy",
            "qty": 0.01, "type": "limit", "price": 20000.0})
        self.assertEqual(kind, "ok", out)
        time.sleep(0.5)
        after = r.ok("account.get")
        pl, raw = self.broker(r)
        self.assertLess(float(raw["cash"]), float(raw0["cash"]) - 100)
        self.assertAlmostEqual(after["equity"], float(raw["equity"]),
                               places=2)
        self.assertAlmostEqual(after["equity"] - after["balance"], pl,
                               delta=0.03)
        self.assertAlmostEqual(after["balance"], before["balance"],
                               delta=0.03)

    def test_fills_between_the_two_reads_cost_a_spread_not_a_positions_value(self):
        # Prices held still; 100 AAPL bought and sold in ten clips,
        # three times, while two callers ask for the account.
        r = self.rig()
        r.buy("MSFT", 20)
        for sym in ("AAPL", "MSFT"):
            r.fake.set_price(sym, price=r.fake.state()["prices"][sym])
        seen_at = self.paced(r)
        time.sleep(0.5)
        base = r.ok("account.get")["balance"]
        _, quote = r.other("GET", "/v2/stocks/quotes/latest?symbols=AAPL")
        q = quote["quotes"]["AAPL"]
        spread = float(q["ap"]) - float(q["bp"])
        self.assertGreater(spread, 0)
        stop, seen = threading.Event(), []

        def poll():
            while not stop.is_set():
                kind, out = r.req("account.get")
                if kind == "ok":
                    seen.append(out["balance"])
        threads = [threading.Thread(target=poll) for _ in range(2)]
        [t.start() for t in threads]
        try:
            for _ in range(3):
                r.fake.next_fill(clips=10, interval=0.25)
                m = r.mark()
                r.ok("order.place", {"client_id": r.cid(), "symbol": "AAPL",
                                     "side": "buy", "qty": 100})
                self.assertTrue(r.wait("order.filled", since=m, timeout=8))
                r.fake.next_fill(clips=10, interval=0.25)
                r.ok("position.close", {"client_id": r.cid("x"),
                                        "position_id": "AAPL"})
                self.assertTrue(until(lambda: r.held("AAPL") == 0, 8))
        finally:
            stop.set()
            [t.join(15) for t in threads]
        self.assertGreater(len(seen), 15, seen)
        # What was REALISED is balance, and nothing else: it went from
        # `base` to what the broker itself says now (three times 100
        # shares bought at the ask and sold at the bid). A fill between
        # the two reads may add the spread of its clip of ten.
        pl, raw = self.broker(r)
        end = float(raw["equity"]) - pl
        self.assertLess(base - end, 3 * 100 * spread + 0.05, (base, end))
        slack = 10 * spread + 0.05
        off = sorted(round(b - base, 2) for b in seen
                     if not end - slack <= b <= base + slack)
        self.assertEqual(off, [], (len(seen), base, end, spread))
        self.together(seen_at)

    def test_a_price_moving_all_the_time_shows_only_the_instant_between_the_reads(self):
        # 300 AAPL, the market falling half a percent a second (530
        # a second on this position). Read a third of a second apart, as
        # the pace spaces two reads, that is 170 of "balance" a call.
        r = self.rig()
        r.buy("AAPL", 300)
        seen_at = self.paced(r)
        time.sleep(0.5)
        start = r.ok("account.get")["balance"]
        r.fake.set_price("AAPL", trend_pct_per_s=-0.5, seconds=30)
        seen = []
        for _ in range(8):
            r.ok("positions.list")           # the dock's pattern
            time.sleep(0.4)
            acct = r.ok("account.get")
            seen.append(round(acct["balance"] - start, 2))
            self.assertLess(acct["equity"], start - 100)     # it does move
        # nothing was realised: balance stands, to the few milliseconds
        # of that fall which lie between two requests sent together
        self.assertLess(max(abs(x) for x in seen), 30.0, seen)
        self.together(seen_at)

    def together(self, seen_at):
        """Every account read reached the broker with a positions read
        beside it: within 60 ms, where the pace alone puts 320 between."""
        accounts = [t for t, path in seen_at if path == "/v2/account"]
        rows = [t for t, path in seen_at if path == "/v2/positions"]
        self.assertGreater(len(accounts), 5)
        apart = [min(abs(t - x) for x in rows) for t in accounts]
        self.assertLess(max(apart), 0.06, sorted(apart)[-5:])


if __name__ == "__main__":
    import unittest
    unittest.main()
