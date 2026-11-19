"""Alpaca adapter: a position's stops and targets, one test each.

Run the same way as tests/test_alpaca_faults.py: the REAL adapter against
the stand-in in this process, the broker's own book compared with what the
adapter told the platform. The faults pinned here:

  - an old position's stop and target reached a NEW position
  - scaling out in clips tripped the adapter's own "exit dying" brake
  - an exit the account's owner cancelled in Alpaca's app was made
    again (the rule "another client cancelled it, so re-make it" is
    WITHDRAWN: dropped, said once, never back by itself)
  - while several stops were put back, the platform's calls were
    refused with an untrue message and nothing was said
  - the adapter stopping inside the re-size window left the rest of
    a position without its stop, said for one start only
  - a start without the state file took a bracket's legs for a
    stranger's
  and the rule that stops and targets are declared on paper only.
(flatten_all behind the call in flight: tests/test_alpaca_handoff.py,
class Panic.)
"""
import json
import os
import threading
import time

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_faults as faults
from tests import test_alpaca_handoff as handoff

until = faults.until


class Levels(faults.Faults):
    def notices(self, r, since=0, word=""):
        return [d for d in r.events(since, "session.state")
                if d["state"] != "connected" and word in d["detail"]]

    def exits(self, r, sym="AAPL"):
        return [(o["type"], o["side"], o["qty"]) for o in r.live(sym)
                if o["type"] in ("stop", "limit")]

    def posts(self, r, word):
        """Orders the adapter SENT since the log was cleared whose client
        id starts with `word` (the stand-in keeps every order made)."""
        b = r.fake.broker
        with b.lock:
            return [o for o in b.orders.values()
                    if o.client_order_id.startswith(word)]


class OldLevels(Levels):
    """An old position's stop and target do not reach a new position."""

    def close_then(self, r, levels, order):
        r.buy("AAPL", 10)
        px = r.fake.state()["prices"]["AAPL"]
        lv = {k: round(px + d, 2) for k, d in levels.items()}
        r.ok("order.modify", dict({"client_id": r.cid("m"),
                                   "position_id": "AAPL"}, **lv))
        self.assertTrue(until(lambda: len(self.exits(r)) == len(lv), 4))
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL"})
        # ONE slow answer: the repair's next read of the symbol's orders
        r.rules.add("GET", "/v2/orders", before=1.5, count=1, name="slow")
        self.assertTrue(r.wait("position.closed", since=m, timeout=6))
        m2 = r.mark()
        cid = r.cid("n")
        r.ok("order.place", dict({"client_id": cid, "symbol": "AAPL"},
                                 **order))
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m2))
        return m2, lv

    def test_a_reversed_position_is_not_bought_back_at_the_old_target(self):
        r = self.rig()
        m, lv = self.close_then(r, {"tp": 45.0},
                                {"side": "sell", "qty": 5})
        for _ in range(8):                       # 4 s, as the script
            self.assertEqual(r.held("AAPL"), -5.0)
            self.assertEqual(self.exits(r), [])
            time.sleep(0.5)
        self.assertEqual(r.events(m, "position.closed"), [])
        self.assertEqual(r.shown(), (5.0, None, None))
        self.assertEqual(self.posts(r, "brue-tp-")[1:], [])   # the first one
        self.assertTrue(self.settled(r), self.last)

    def test_a_rebought_position_starts_with_no_stop_and_no_target(self):
        r = self.rig()
        m, lv = self.close_then(r, {"sl": -50.0, "tp": 45.0},
                                {"side": "buy", "qty": 10})
        for _ in range(8):
            self.assertEqual(self.exits(r), [])
            time.sleep(0.5)
        self.assertEqual(r.shown(), (10.0, None, None))
        self.assertEqual(r.session()["state"], "connected")

    def test_an_order_waits_for_the_symbols_guard_like_everything_else(self):
        r = self.rig()
        r.buy("AAPL", 10)
        g = r.a.prot._guard("AAPL")
        held = threading.Event()
        free = threading.Event()

        def hold():                 # the repair, between read and order
            with g:
                held.set()
                free.wait(5)
        threading.Thread(target=hold, daemon=True).start()
        self.assertTrue(held.wait(2))
        r.fake.requests(clear=True)
        rid = r.send("order.place", {"client_id": r.cid(), "symbol": "AAPL",
                                     "side": "buy", "qty": 1})
        time.sleep(0.6)
        self.assertEqual(r.log("POST", "/v2/orders"), [])
        free.set()
        self.assertIn("result", r.answer(rid))
        self.assertEqual(len(r.log("POST", "/v2/orders")), 1)


class ScalingOut(Levels):
    """Scaling out in clips keeps a stop under what is left, and does not
    trip the adapter's own "exit dying" brake."""

    def watch(self, r, sym="AAPL"):
        """Seconds (in tenths) seen with a position and NO stop under it."""
        bare, stop = [], threading.Event()

        def look():
            t0 = time.monotonic()
            while not stop.is_set():
                if r.held(sym) != 0 and not r.stops(sym):
                    bare.append(round(time.monotonic() - t0, 1))
                stop.wait(0.1)
        th = threading.Thread(target=look, daemon=True)
        th.start()
        return bare, lambda: (stop.set(), th.join(2))

    def longest(self, bare):
        """The longest run of consecutive bare tenths, in seconds."""
        best = run = 0
        for a, b in zip([None] + bare, bare):
            run = run + 1 if a is not None and b - a < 0.25 else 1
            best = max(best, run)
        return best / 10.0

    def test_five_partial_closes_keep_a_stop_under_what_is_left(self):
        r = self.rig()
        r.long_with_stop("AAPL", 12, 300.0)
        bare, done = self.watch(r)
        m = r.mark()
        for i in range(5):
            r.ok("position.close", {"client_id": r.cid("x"),
                                    "position_id": "AAPL", "qty": 2})
            left = 12.0 - 2 * (i + 1)
            self.assertTrue(until(
                lambda: r.stops("AAPL") == [(left, 300.0)], 5),
                "after clip %d: %s" % (i + 1, r.live("AAPL")))
        time.sleep(1.5)
        done()
        self.assertEqual(r.stops("AAPL"), [(2.0, 300.0)])
        # a second or two while each stop is re-sized; never half a minute
        self.assertLess(self.longest(bare), 3.0, bare)
        self.assertEqual(self.notices(r, m, "rejected them"), [])
        self.assertEqual(self.notices(r, m, "WITHOUT"), [])
        self.assertTrue(self.settled(r), self.last)

    def test_changing_the_set_of_levels_five_times_is_never_refused(self):
        r = self.rig()
        r.long_with_stop("AAPL", 10, 300.0)
        bare, done = self.watch(r)
        m = r.mark()
        for tp in (400.0, None, 400.0, None, 400.0):
            kind, out = r.req("order.modify", {
                "client_id": r.cid("m"), "position_id": "AAPL", "tp": tp})
            self.assertEqual(kind, "ok", out)
            time.sleep(0.3)
        done()
        self.assertTrue(until(lambda: r.shown() == (10.0, 300.0, 400.0), 5),
                        r.shown())
        self.assertLess(self.longest(bare), 3.0, bare)
        self.assertEqual(self.notices(r, m, "rejected them"), [])

    def test_the_brake_counts_what_alpaca_ended_not_what_the_adapter_took_off(self):
        """Still a brake: an order Alpaca takes and then rejects, three
        times in half a minute, is not made a fourth time at once."""
        r = self.rig()
        r.long_with_stop("AAPL", 10, 300.0)
        prot = r.a.prot
        m = r.mark()
        with r.a._state_lock:
            rec = prot.exits("AAPL")[0]
            for _ in range(3):
                prot.exit_ended(rec, "rejected")
        prot._cancel("AAPL", [rec])       # and the fourth is due
        prot.touch("AAPL")
        said = until(lambda: self.notices(r, m, "rejected them each time"), 6)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("AAPL", said[0]["detail"])
        self.assertEqual(r.stops("AAPL"), [])
        self.assertEqual(len(self.posts(r, "brue-sl-")), 1)
        # the platform's own word ends the pause
        r.ok("order.modify", {"client_id": r.cid("m"),
                              "position_id": "AAPL", "sl": 301.0})
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 301.0)], 5),
                        r.live("AAPL"))


class RemovedOutside(Levels):
    """An exit the account's owner cancelled in Alpaca's app stays
    cancelled: dropped, said once, never back by itself."""

    QUICK = dict(Levels.QUICK, NOTICE_SEC=4.0)

    def test_a_target_cancelled_in_alpacas_app_is_not_sold_into_a_higher_market(self):
        r = self.rig()
        r.buy("AAPL", 10)
        px = r.fake.state()["prices"]["AAPL"]
        tp = round(px + 3, 2)
        r.ok("order.modify", {"client_id": r.cid("m"),
                              "position_id": "AAPL", "tp": tp})
        self.assertTrue(until(lambda: self.exits(r), 4))
        m = r.mark()
        target = r.live("AAPL")[0]
        self.assertEqual(r.other("DELETE", "/v2/orders/" + target["id"])[0],
                         204)
        self.assertTrue(until(lambda: not r.live("AAPL"), 3))
        r.fake.set_price("AAPL", price=px + 6)       # through the old target
        for _ in range(10):                          # 5 s
            self.assertEqual(r.held("AAPL"), 10.0)
            self.assertEqual(self.exits(r), [])
            time.sleep(0.5)
        self.assertEqual(r.events(m, "position.closed"), [])
        self.assertEqual(len(self.posts(r, "brue-tp-")), 1)   # never again
        said = self.notices(r, m, "removed outside the terminal")
        self.assertEqual(len(said), 1, r.events(m, "session.state"))
        self.assertIn("AAPL", said[0]["detail"])
        self.assertIn("target", said[0]["detail"])
        self.assertEqual(r.shown(), (10.0, None, None))
        # said once: it goes again by itself
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 8), r.session())

    def test_a_stop_cancelled_outside_is_dropped_said_once_and_stays_gone(self):
        r = self.rig()
        r.long_with_stop()
        m = r.mark()
        r.other("DELETE", "/v2/orders/" + r.live("AAPL")[0]["id"])
        self.assertTrue(until(lambda: not r.live("AAPL"), 3))
        self.assertEqual(r.shown(), (10.0, None, None))     # at once
        time.sleep(5.0)
        self.assertEqual(r.live("AAPL"), [])
        self.assertEqual(len(self.posts(r, "brue-sl-")), 1)
        said = self.notices(r, m, "removed outside the terminal")
        self.assertEqual(len(said), 1, r.events(m, "session.state"))
        self.assertIn("stop at 300", said[0]["detail"])
        # the platform sets it again: it is there again, and nothing said
        r.ok("order.modify", {"client_id": r.cid("m"),
                              "position_id": "AAPL", "sl": 300.0})
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 300.0)], 5))
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 3), r.session())
        self.assertTrue(self.settled(r), self.last)

    def test_the_owners_own_stop_in_its_place_is_not_fought(self):
        r = self.rig()
        r.long_with_stop()
        m = r.mark()
        r.other("DELETE", "/v2/orders/" + r.live("AAPL")[0]["id"])
        self.assertTrue(until(lambda: not r.live("AAPL"), 3))
        st, own = r.other("POST", "/v2/orders", {
            "symbol": "AAPL", "qty": "10", "side": "sell", "type": "stop",
            "stop_price": "320", "time_in_force": "gtc"})
        self.assertEqual(st, 200, own)
        time.sleep(5.0)
        self.assertEqual(r.stops("AAPL"), [(10.0, 320.0)])   # his, alone
        self.assertEqual(len(self.posts(r, "brue-sl-")), 1)
        self.assertEqual(self.notices(r, m, "WITHOUT"), [])
        self.assertEqual(len(self.notices(r, m, "removed outside")), 1)
        self.assertEqual(r.shown(), (10.0, None, None))
        listed = r.ok("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(x["type"], x["side"], x["client_id"])
                          for x in listed], [("stop", "sell", None)])
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 8), r.session())

    def test_a_target_that_fills_is_the_pair_at_work_not_a_removal(self):
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)
        m = r.mark()
        r.fake.set_price("AAPL", price=401.0)
        closed = r.wait("position.closed", since=m, timeout=8)
        self.assertTrue(closed, r.events(m))
        self.assertEqual(closed["reason"], "tp")
        time.sleep(2.5)
        self.assertEqual(self.notices(r, m), [])
        self.assertTrue(self.settled(r), self.last)


class ManySymbols(Levels):
    """While several stops are put back, a close is carried out, the bare
    positions are said, and a refusal for want of a place says so."""

    SYMS = ("AAPL", "MSFT", "SPY", "XRX", "DTI", "SNDL")

    def test_a_close_is_carried_out_and_the_bare_ones_are_said_while_stops_are_put_back(self):
        r = self.rig()
        for sym in self.SYMS:
            r.buy(sym, 10)
            px = r.fake.state()["prices"][sym]
            r.ok("order.modify", {"client_id": r.cid("m"), "position_id": sym,
                                  "sl": round(px * 0.8, 2)})
        self.assertTrue(until(lambda: all(r.stops(s) for s in self.SYMS), 6))
        # Alpaca's pace, as in life, and every stop to be made again at
        # once (taken off by the adapter itself: a re-size on all six)
        r.a.client.budget.per_second = 3
        m = r.mark()
        with r.a._state_lock:
            recs = [x for s in self.SYMS for x in r.a.prot.exits(s)]
        for x in recs:
            r.a.prot._cancel(x["sym"], [x])
        for s in self.SYMS:
            r.a.prot.touch(s)
        time.sleep(0.8)
        kind, out = r.req("position.close", {"client_id": r.cid("x"),
                                             "position_id": "SNDL"})
        self.assertEqual(kind, "ok", out)            # was: rate_limited
        self.assertLess(r.took, 6.0)
        self.assertTrue(until(lambda: all(
            r.stops(s) for s in self.SYMS[:-1]), 25), [
            (s, r.stops(s)) for s in self.SYMS])
        said = self.notices(r, m, "NOT held by Alpaca")
        self.assertTrue(said, "bare positions and not a word: %s"
                        % r.events(m, "session.state"))
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 8), r.session())

    def test_a_refusal_for_want_of_a_place_says_so(self):
        bud = mod._Budget(per_second=1, per_minute=100000)
        bud.take("trading", "protect", 0)
        with self.assertRaises(mod.AlpacaError) as ctx:
            bud.take("trading", "read", 0.2, rank=1)
        text = ctx.exception.message
        self.assertIn("more urgent ones went first", text)
        self.assertIn("nothing was sent", text)
        self.assertNotIn("spent", text)


class Stopping(Levels):
    """The adapter stopping inside the re-size window (bye, kill): the rest
    of a position is not left without its stop."""

    RIG = handoff.Piped

    def rig(self, **kw):
        r = self.RIG(**kw)
        self.rigs.append(r)
        return r

    def state_file(self, r):
        return os.path.join(r.state, "alpaca_state.json")

    def test_bye_right_after_a_partial_close_leaves_the_stop_back_in_place(self):
        r = self.rig()
        r.long_with_stop("AAPL", 10, 300.0)
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        # Alpaca a little slow, so that the stop is still due at `bye`
        r.rules.add("GET", "/v2/", before=0.5, count=6, name="slow")
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertEqual(r.stops("AAPL"), [])
        bye = r.send("bye")
        r.answer(bye, timeout=6)
        self.assertLess(r.lat(bye), 4.6)        # the connector waits five
        self.assertTrue(r.ended.wait(3))
        # nobody is running any more; the broker has what it should
        self.assertEqual(r.held("AAPL"), 6.0)
        self.assertEqual(r.stops("AAPL"), [(6.0, 300.0)])

    def kill(self, r):
        """The process dies: nothing more is sent, and the state file is
        what was on disk at that instant (not what a tidy exit writes).
        Returns that file's text."""
        with open(self.state_file(r)) as fh:
            disk = fh.read()
        r.stop_adapter()
        time.sleep(0.8)      # what it had in flight has come back refused
        with open(self.state_file(r), "w") as fh:
            fh.write(disk)
        r.rules.clear()
        r.fake.drop_streams()
        return disk

    def killed_inside_the_window(self, r):
        """Long 10 with a stop; a partial close; the stop is off and not
        yet back when the process dies. Returns the state file as it was
        on disk at that moment."""
        r.long_with_stop("AAPL", 10, 300.0)
        r.rules.add("POST", "/v2/orders", body="brue-sl-", fail=500,
                    count=50, name="no stop")
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(lambda: not r.stops("AAPL"), 3))
        disk = self.kill(r)
        self.assertEqual((r.held("AAPL"), r.stops("AAPL")), (6.0, []))
        return json.loads(disk)

    def test_a_kill_inside_the_resize_window_is_put_right_at_the_next_start(self):
        r = self.rig()
        st = self.killed_inside_the_window(r)
        due = st.get("unheld", {}).get("AAPL")
        self.assertTrue(due and due["sl"] == 300.0 and due["since"], st.get(
            "unheld"))
        r.start()
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, 300.0)], 6),
                        r.live("AAPL"))
        self.assertEqual(r.shown(), (6.0, 300.0, None))
        self.assertEqual(self.notices(r), [])
        self.assertTrue(self.settled(r), self.last)

    def test_what_was_due_long_ago_is_said_at_every_start_until_it_is_set(self):
        r = self.rig()
        st = self.killed_inside_the_window(r)
        st["unheld"]["AAPL"]["since"] -= 20 * 60 * 1000      # 20 min ago
        with open(self.state_file(r), "w") as fh:
            json.dump(st, fh)
        for start in (1, 2, 3):
            r.start()
            said = until(lambda: self.notices(r, 0, "NOT put back"), 4)
            self.assertTrue(said, "start %d: %s" % (start, r.session()))
            self.assertIn("AAPL had a stop at 300", said[0]["detail"])
            time.sleep(1.5)
            self.assertEqual(r.stops("AAPL"), [])            # not by itself
            self.assertEqual(r.shown(), (6.0, None, None))
            r.stop_adapter()
            r.fake.drop_streams()
        # the platform sets it: there, and nothing is said any more
        r.start()
        r.ok("order.modify", {"client_id": r.cid("m"),
                              "position_id": "AAPL", "sl": 300.0})
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, 300.0)], 5))
        r.stop_adapter()
        r.fake.drop_streams()
        r.start()
        time.sleep(1.0)
        self.assertEqual(self.notices(r), [])

    def test_a_target_the_market_has_passed_is_not_put_back_at_a_start(self):
        r = self.rig()
        r.buy("AAPL", 10)
        px = r.fake.state()["prices"]["AAPL"]
        r.ok("order.modify", {"client_id": r.cid("m"), "position_id": "AAPL",
                              "sl": 300.0, "tp": round(px + 3, 2)})
        self.assertTrue(until(lambda: len(r.live("AAPL")) == 2, 4))
        r.rules.add("POST", "/v2/orders", body="brue-", fail=500, count=50,
                    name="no exits")
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(lambda: not r.live("AAPL"), 3))
        self.kill(r)
        r.fake.set_price("AAPL", price=px + 6)       # through the target
        r.start()
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, 300.0)], 6),
                        r.live("AAPL"))
        time.sleep(1.5)
        self.assertEqual(r.held("AAPL"), 6.0)                # not sold
        self.assertEqual(self.exits(r), [("stop", "sell", 6.0)])
        said = self.notices(r, 0, "target")
        self.assertTrue(said and "NOT put back" in said[0]["detail"],
                        r.session())


class WithoutMemory(Levels):
    """A start without the state file."""

    def test_a_start_without_the_state_file_finds_the_brackets_legs_again(self):
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)
        r.restart(forget=True)
        # the working target is in Alpaca's open list, the held stop in
        # none: both are found under the order that opened the position
        self.assertEqual(r.shown(), (10.0, 300.0, 400.0))
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])
        self.assertEqual(self.notices(r), [])
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(lambda: r.shown() == (6.0, 300.0, 400.0), 6),
                        r.shown())
        self.assertTrue(self.settled(r), self.last)

    def test_legs_that_are_not_plainly_this_positions_are_listed_and_said(self):
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)
        b = r.fake.broker
        with b.lock:                 # the position is 15 now, however
            pos = b.positions["AAPL"]
            pos[1] = type(pos[1])(15)
        r.restart(forget=True)
        said = until(lambda: self.notices(r, 0, "cannot tell"), 4)
        self.assertTrue(said, r.session())
        self.assertIn("AAPL", said[0]["detail"])
        self.assertEqual(r.shown(), (15.0, None, None))
        listed = r.ok("orders.list", {"state": "resting"})["orders"]
        self.assertEqual(sorted((x["type"], x["side"]) for x in listed),
                         [("limit", "sell"), ("stop", "sell")])


class PaperOnly(Levels):
    """Stops and targets are declared only when the environment is
    paper."""

    def caps(self, argv, env=None):
        a = mod.AlpacaAdapter(argv=argv, environ=dict(env or {}))
        return a.capabilities()["orders"]

    def test_the_capability_is_declared_cold_per_environment(self):
        paper = self.caps(["--paper"])
        self.assertEqual((paper["brackets"], paper["modify"]), (True, True))
        self.assertEqual(self.caps([])["brackets"], True)    # paper is default
        for argv, env in ((["--live"], {}), ([], {"ALPACA_ENV": "live"}),
                          (["--live"], {"ALPACA_ENV": "paper"})):
            got = self.caps(argv, env)
            self.assertEqual((got["brackets"], got["modify"]),
                             (False, False), (argv, env))
        # the flag wins over the variable, as everywhere
        self.assertEqual(self.caps(["--paper"],
                                   {"ALPACA_ENV": "live"})["brackets"], True)
        # paper asked for, another host talked to: that is real money
        got = self.caps(["--paper"], {
            "ALPACA_TRADING_URL": "https://broker.example.com"})
        self.assertEqual((got["brackets"], got["modify"]), (False, False))

    def test_live_refuses_a_stop_or_target_loudly_and_sends_nothing(self):
        r = self.rig(start=False)
        r.argv = ["--live"]
        r.start()
        r.fake.requests(clear=True)
        kind, out = r.req("order.place", {
            "client_id": r.cid(), "symbol": "AAPL", "side": "buy", "qty": 10,
            "sl": 300.0})
        self.assertEqual((kind, out.get("code")), ("err", "unsupported"), out)
        self.assertIn("LIVE", out["message"])
        self.assertIn("NOTHING was sent", out["message"])
        self.assertEqual(r.log("POST", "/v2/orders"), [])
        r.buy("AAPL", 10)
        kind, out = r.req("order.modify", {
            "client_id": r.cid("m"), "position_id": "AAPL", "sl": 300.0})
        self.assertEqual((kind, out.get("code")), ("err", "unsupported"), out)
        self.assertEqual(r.live("AAPL"), [])
        self.assertEqual(r.shown(), (10.0, None, None))

    def test_live_shows_a_stop_an_earlier_run_left_as_an_ordinary_order(self):
        r = self.rig()
        r.long_with_stop("AAPL", 10, 300.0)         # a paper run: brue-sl-
        left = r.live("AAPL")[0]
        self.assertTrue(left["coid"].startswith("brue-sl-"))
        r.argv = ["--live"]
        r.restart()
        listed = r.ok("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(x["type"], x["side"], x["qty"], x["price"])
                          for x in listed], [("stop", "sell", 10.0, 300.0)])
        self.assertEqual(r.shown(), (10.0, None, None))   # no stop is CLAIMED
        time.sleep(2.5)
        self.assertEqual(r.stops("AAPL"), [(10.0, 300.0)])   # nor touched
        self.assertEqual(r.session()["state"], "connected")
        # ... and it can be cancelled from the platform like any order
        r.ok("order.cancel", {"client_id": r.cid("k"),
                              "order_id": listed[0]["order_id"]})
        self.assertTrue(until(lambda: not r.live("AAPL"), 4), r.live())
        time.sleep(1.5)
        self.assertEqual(r.live("AAPL"), [])              # never made again


if __name__ == "__main__":
    import unittest
    unittest.main()
