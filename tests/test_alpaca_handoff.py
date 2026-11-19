"""Alpaca adapter: requests are handed off the read thread.

Seen in the Terminal: with a chart loading, an order sent a second later
was answered after six, the next chart not at all inside the platform's
ten seconds ("bars.history timed out after 10.0s"). One thread read a
request, answered it, and only then read the next; `bars.history` waits on
Alpaca for up to six seconds by design; and a request's own time began
when its handler began, not when the platform sent it.

These tests drive the REAL adapter against the stand-in in this process,
and they feed it the way a platform does: lines through its own read loop
(`Adapter.run`), so that a line is read when the loop comes back for it
and not before. What they hold the adapter to:

  - a line is read at once, whatever is being answered;
  - calls that change something are carried out in the order sent;
  - an order, a read and a ping are not kept waiting by a chart;
  - a request's time counts from when its line was read;
  - no more charts are read at once than the bound, and the one over it
    is answered inside its own time with what there is;
  - the session with Alpaca is opened once;
  - the pace is shared: an order first, a chart about every second
    request while its caller waits, prices never stopped.
"""
import json
import os
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch

from brueconnect import protocol
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_faults as faults

until = faults.until


class TimedWire(faults.Wire):
    """The platform's end, with the moment each answer arrived."""

    def __init__(self):
        super().__init__()
        self.at = {}

    def write(self, text):
        super().write(text)
        now = time.monotonic()
        with self.cv:
            for rid in self.answers:
                self.at.setdefault(rid, now)


class Piped(faults.Rig):
    """The rig, with the platform's lines going through the adapter's own
    read loop on its own thread, as they do from a pipe."""

    PACE = 40           # the stand-in is on this machine

    def start(self, reconcile=True):
        self.wire = TimedWire()
        self.a = mod.AlpacaAdapter(argv=self.argv, environ=self.env(),
                                   out=self.wire)
        b = self.a.client.budget
        b.per_second, b.per_minute = self.PACE, 100000
        b.background = b.background_data = 100000
        self.sent, self.lines = {}, queue.Queue()
        self.ended = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True,
                                       args=(self.a, self.lines))
        self.reader.start()
        if reconcile:
            self.ok("catalog.list")
            self.ok("positions.list")
        return self

    def _read(self, a, lines):
        a.run(iter(lines.get, None))
        self.ended.set()

    def send(self, method, params=None):
        self._n += 1
        rid = "r%d" % self._n
        self.sent[rid] = time.monotonic()
        self.lines.put(protocol.req(rid, method, dict(params or {})))
        return rid

    def lat(self, rid):
        """Seconds from the platform's send to the answer's arrival."""
        self.answer(rid)
        return self.wire.at[rid] - self.sent[rid]

    def answered(self):
        """Ids of the answers, in the order they were written."""
        with self.wire.cv:
            raw = list(self.wire.raw)
        return [m["id"] for m in map(json.loads, raw)
                if m.get("type") == "res"]

    def stop_adapter(self):
        if self.a is not None:
            self.lines.put(None)
        super().stop_adapter()

    def made(self):
        """Symbols of the orders the broker holds, in the order made."""
        b = self.fake.broker
        with b.lock:
            return [o.inst.symbol for o in sorted(
                b.orders.values(), key=lambda o: o.created_ns)]


class HandOff(faults.Faults):
    RIG = Piped

    def rig(self, **kw):
        r = self.RIG(**kw)
        self.rigs.append(r)
        return r

    def set(self, name, value):
        p = patch.object(mod, name, value, create=True)
        p.start()
        self.addCleanup(p.stop)

    def slow_bars(self, r, seconds, count=400):
        for path in ("/v2/stocks/bars", "/v1beta3/crypto/us/bars"):
            r.rules.add("GET", path, before=seconds, count=count,
                        name="bars")


class Reading(HandOff):
    def test_an_order_a_read_and_a_ping_are_not_kept_waiting_by_a_chart(self):
        r = self.rig()
        # Alpaca's real pace, so that the chart's windows and the order
        # also meet in the request budget, not only in the lanes.
        r.a.client.budget.per_second = 3
        self.slow_bars(r, 0.8)
        chart = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                        "count": 5000})
        time.sleep(0.3)
        cid, m = r.cid(), r.mark()
        order = r.send("order.place", {"client_id": cid, "symbol": "MSFT",
                                       "side": "buy", "qty": 1})
        dock = r.send("positions.list")
        ping = r.send("ping")
        self.assertLess(r.lat(ping), 0.3)
        self.assertIn("result", r.answer(order), r.answer(order))
        self.assertLess(r.lat(order), 1.5)
        self.assertNotIn(chart, r.wire.answers, "the chart was to be still "
                         "loading when the order was answered")
        # The read shares three requests a second with the order (first)
        # and the chart (every second one): slower than alone, and still
        # not behind the chart.
        self.assertIn("result", r.answer(dock), r.answer(dock))
        self.assertLess(r.lat(dock), 4.0)
        self.assertNotIn(chart, r.wire.answers)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m))
        got = r.answer(chart)
        self.assertTrue(got.get("result", {}).get("bars"), got)
        self.assertLess(r.lat(chart), mod.BARS_BUDGET + 0.6)
        r.rules.clear()

    def test_four_charts_asked_together_are_each_answered_in_their_time(self):
        self.set("BARS_BUDGET", 3.0)
        r = self.rig()
        self.slow_bars(r, 0.4)
        asked = [r.send("bars.history", {"symbol": s, "timeframe": tf,
                                         "count": 5000})
                 for s, tf in (("AAPL", "1h"), ("MSFT", "1h"),
                               ("BTC/USD", "1h"), ("SPY", "15m"))]
        for rid in asked:
            got = r.answer(rid)
            self.assertTrue(got.get("result", {}).get("bars"), got)
            # from the SEND, though three of them were behind another
            self.assertLess(r.lat(rid), 3.0 + 0.6)
        r.rules.clear()


class Ordering(HandOff):
    def test_calls_that_change_something_are_carried_out_in_the_order_sent(self):
        r = self.rig()
        r.rules.add("POST", "/v2/orders", before=0.8, count=1, name="slow")
        syms = ["AAPL", "MSFT", "SPY"]
        ids = [r.send("order.place", {"client_id": r.cid(), "symbol": s,
                                      "side": "buy", "qty": 1})
               for s in syms]
        read = r.send("orders.list", {"state": "resting"})
        for rid in ids:
            self.assertIn("result", r.answer(rid), r.answer(rid))
        r.answer(read)
        self.assertEqual(r.made(), syms)
        order = r.answered()
        self.assertEqual([i for i in order if i in ids], ids)
        # a read is not an order: it did not wait behind the slow one
        self.assertLess(order.index(read), order.index(ids[0]))
        self.assertLess(r.lat(read), 0.7)

    def test_a_retry_sent_while_the_first_is_in_flight_is_not_a_second_order(self):
        r = self.rig()
        r.rules.add("POST", "/v2/orders", before=0.8, count=1, name="slow")
        p = {"client_id": r.cid(), "symbol": "AAPL", "side": "buy", "qty": 3}
        first, again = r.send("order.place", p), r.send("order.place", p)
        a, b = r.answer(first), r.answer(again)
        self.assertIn("result", a, a)
        self.assertEqual(a["result"], b.get("result"), b)
        self.assertTrue(until(lambda: r.held("AAPL") == 3.0, 5), r.held("AAPL"))
        time.sleep(0.5)
        self.assertEqual(r.made(), ["AAPL"])
        self.assertEqual(r.held("AAPL"), 3.0)

    def test_bye_right_behind_an_order_does_not_abandon_it(self):
        r = self.rig()
        r.rules.add("POST", "/v2/orders", before=0.8, count=1, name="slow")
        order = r.send("order.place", {"client_id": r.cid(), "symbol": "AAPL",
                                       "side": "buy", "qty": 2})
        bye = r.send("bye")
        r.answer(bye)
        self.assertTrue(r.ended.wait(3))
        self.assertIn("result", r.wire.answers.get(order, {}),
                      "the order was not answered before the adapter left")
        seq = r.answered()
        self.assertLess(seq.index(order), seq.index(bye))
        self.assertEqual(r.made(), ["AAPL"])

    def test_the_pipe_closing_right_behind_an_order_does_not_abandon_it(self):
        """The whole program (`main`), its input ending right after the
        order's line: the order is carried out AND answered before the
        process would end."""
        r = self.rig(start=False)
        r.rules.add("POST", "/v2/orders", before=1.0, count=1, name="slow")
        wire, made = TimedWire(), []

        class Kept(mod.AlpacaAdapter):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                made.append(self)

        self.addCleanup(lambda: [a.on_shutdown() for a in made])
        lines = [protocol.req("o1", "order.place", {
            "client_id": r.cid(), "symbol": "AAPL", "side": "buy", "qty": 2})]
        with patch.object(mod, "AlpacaAdapter", Kept), \
                patch.object(sys, "argv", ["alpaca_adapter.py", "--paper"]), \
                patch.object(sys, "stdin", iter(lines)), \
                patch.object(sys, "stdout", wire), \
                patch.dict(os.environ, r.env()):
            mod.main()
        self.assertIn("result", wire.answers.get("o1", {}), wire.answers)
        self.assertEqual(r.made(), ["AAPL"])


class Budget(HandOff):
    def test_time_spent_waiting_in_line_is_part_of_a_requests_own_time(self):
        """One reader, busy for 1.5 s. The request behind it has three
        seconds from when its line was READ: it is answered about three
        seconds after it was sent, not 1.5 + 3."""
        self.set("READ_WORKERS", 1)
        r = self.rig()
        self.set("HANDLER_BUDGET", 3.0)
        real = r.a.handle_positions_list
        r.a.handle_positions_list = lambda p: (time.sleep(1.5), real(p))[1]
        r.rules.add("GET", "/v2/orders", before=6.0, count=40, name="hang")
        first = r.send("positions.list")
        second = r.send("orders.list", {"state": "resting"})
        self.assertIn("result", r.answer(first))
        got = r.answer(second)
        self.assertIn("error", got, got)
        self.assertTrue(got["error"]["retryable"], got)
        self.assertGreater(r.lat(second), 2.4)
        self.assertLess(r.lat(second), 3.6)
        r.rules.clear()

    def test_a_request_whose_time_went_by_in_line_is_told_so_and_nothing_is_done(self):
        self.set("READ_WORKERS", 1)
        r = self.rig()
        self.set("HANDLER_BUDGET", 2.0)
        real, ran = r.a.handle_positions_list, []
        r.a.handle_positions_list = lambda p: (time.sleep(2.2), real(p))[1]
        listed = r.a.handle_orders_list
        r.a.handle_orders_list = lambda p: (ran.append(1), listed(p))[1]
        r.send("positions.list")
        second = r.send("orders.list", {"state": "resting"})
        got = r.answer(second)
        self.assertEqual(got.get("error", {}).get("code"), "rate_limited",
                         got)
        self.assertTrue(got["error"]["retryable"])
        self.assertIn("nothing was done", got["error"]["message"])
        self.assertEqual(ran, [], "the handler ran with no time left")
        # said when its turn came, well inside the platform's ten seconds
        self.assertLess(r.lat(second), 2.8)

    def test_the_panic_button_is_never_told_its_time_went_by_in_line(self):
        """flatten_all behind an order that hangs: it still gets time to
        do its work, and the account ends flat."""
        r = self.rig()
        r.buy("AAPL", 4)
        self.set("HANDLER_BUDGET", 2.0)
        self.set("FLATTEN_HARD", 2.5)
        self.set("FLATTEN_BUDGET", 2.0)
        self.set("FLATTEN_FLOOR", 3.0)
        r.rules.add("POST", "/v2/orders", body="MSFT", before=2.6, count=1,
                    name="hang")
        r.send("order.place", {"client_id": r.cid(), "symbol": "MSFT",
                               "side": "buy", "qty": 1})
        flat = r.send("flatten_all", {"client_id": r.cid("f")})
        got = r.answer(flat)
        self.assertIn("result", got, got)
        self.assertTrue(until(lambda: r.held("AAPL") == 0, 6), r.held("AAPL"))

    def test_a_handlers_read_waits_its_turn_inside_its_own_time(self):
        """While flatten_all takes every request of the pace for three
        seconds, a handler's read is not refused after 2.5: it has
        eight seconds, and waits. It IS refused at once when the wait is
        known to be longer than its time (a pause Alpaca asked for)."""
        r = self.rig()
        c = r.a.client
        c.budget.per_second = 3
        stop = time.monotonic() + 3.2

        def urgent():
            while time.monotonic() < stop:
                try:
                    c.budget.take("trading", "flatten", 1.0)
                except mod.AlpacaError:
                    pass
        for _ in range(3):
            threading.Thread(target=urgent, daemon=True).start()
        time.sleep(0.4)
        t = time.monotonic()
        c.set_request(t, 8.0, rank=1)
        try:
            self.assertTrue(c.call("GET", "trading", "/v2/clock"))
            self.assertGreater(time.monotonic() - t, 2.5)
            c.budget.block("trading", 30.0)
            t = time.monotonic()
            c.set_request(t, 8.0, rank=1)
            with self.assertRaises(mod.AlpacaError) as e:
                c.call("GET", "trading", "/v2/clock")
            self.assertEqual(e.exception.code, "budget")
            self.assertLess(time.monotonic() - t, 0.3)
        finally:
            c.set_deadline(None)
            c.budget.block("trading", 0.0)


class Charts(HandOff):
    """BARS_LOADS: how many charts have their windows read at once."""

    def hang_windows(self, r, symbol, seconds, head=False):
        """Alpaca slow to answer for the WINDOWS of one symbol (the
        requests with an end; the newest end has none), or (`head`) for
        its newest end."""
        inner = r.fake._answer

        def answer(method, path, q, data, headers, service):
            if (path.endswith("/bars") and q.get("symbols") == symbol
                    and ("end" in q) != head):
                time.sleep(seconds)
            return inner(method, path, q, data, headers, service)
        r.fake._answer = answer

    class Counted:
        """The bound, with how many held it at once and how many in all."""

        def __init__(self, sem):
            self.sem, self.lock = sem, threading.Lock()
            self.now = self.peak = self.turns = 0

        def acquire(self, *a, **kw):
            got = self.sem.acquire(*a, **kw)
            if got:
                with self.lock:
                    self.now += 1
                    self.turns += 1
                    self.peak = max(self.peak, self.now)
            return got

        def release(self):
            with self.lock:
                self.now -= 1
            self.sem.release()

    def windows(self, r):
        """Record, at the stand-in, every request for a WINDOW of bars
        (the ones with an end; the newest end has none): (symbol, from,
        to) on this process's clock."""
        spans, inner = [], r.fake._answer

        def answer(method, path, q, data, headers, service):
            t = time.monotonic()
            try:
                return inner(method, path, q, data, headers, service)
            finally:
                if path.endswith("/bars") and "end" in (q or {}):
                    spans.append((q.get("symbols"), t, time.monotonic()))
        r.fake._answer = answer
        return spans

    def test_the_chart_over_the_bound_waits_its_turn_and_gets_it(self):
        self.set("BARS_LOADS", 1)
        self.set("BARS_BUDGET", 5.0)
        r = self.rig()
        slots = r.a._bars_slots = self.Counted(r.a._bars_slots)
        spans = self.windows(r)
        self.slow_bars(r, 0.15)
        one = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                      "count": 150})
        two = r.send("bars.history", {"symbol": "MSFT", "timeframe": "1h",
                                      "count": 150})
        for rid in (one, two):
            got = r.answer(rid)
            self.assertEqual(len(got.get("result", {}).get("bars", [])), 150,
                             got if "error" in got else len(
                                 got["result"]["bars"]))
            self.assertLess(r.lat(rid), 5.0 + 0.6)
        self.assertEqual((slots.peak, slots.turns), (1, 2))
        # and at the broker: one chart's windows were all asked for
        # before the other's first (a window still on its way back when
        # the turn passes on is not a second chart being read)
        a = sorted(s[1] for s in spans if s[0] == "AAPL")
        m = sorted(s[1] for s in spans if s[0] == "MSFT")
        self.assertTrue(a and m, spans)
        self.assertTrue(a[-1] <= m[0] or m[-1] <= a[0], (a, m))
        r.rules.clear()

    def test_the_chart_whose_turn_does_not_come_answers_with_what_there_is(self):
        """One chart keeps the only turn for all of its time. The second
        is not left to time out: it answers inside ITS time with the
        newest bars (read at once, whatever else is loading), and asking
        again finds more.

        The second is asked once the first holds the turn, and the first
        is given a longer time (6 s, answered at its end) than the second's
        whole wait for a turn (its 3 s, less half a second); the second's
        windows are slow at Alpaca (0.3 s each), as the first's are. With
        0.2 s slept between the two, 3 s given to both and the second's
        windows read at the stand-in's speed, the outcome hangs on timing
        ("5000 not less than 5000" under load): the first chart's loader
        can take the turn after the second's; the second can be read over
        half a second after the first, still be waiting when the first
        gives the turn up at its answer, and get it ("(1, 2) != (1, 1)");
        and the quiet reader that goes on for a chart whose turn has not
        come, from half a second before its answer, reads about 40 ms a
        window, enough for 4,000 to 5,000 bars by the answer."""
        self.set("BARS_LOADS", 1)
        self.set("BARS_BUDGET", 6.0)
        self.set("BARS_FIRST", 6.0)
        r = self.rig()
        slots = r.a._bars_slots = self.Counted(r.a._bars_slots)
        self.hang_windows(r, "AAPL", 3.5)
        self.hang_windows(r, "MSFT", 0.3)
        one = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                      "count": 5000})
        self.assertTrue(until(lambda: slots.turns == 1, 10, 0.005),
                        "the first chart never took its turn")
        self.set("BARS_BUDGET", 3.0)          # the second chart's own time
        self.set("BARS_FIRST", 3.0)
        two = r.send("bars.history", {"symbol": "MSFT", "timeframe": "1h",
                                      "count": 5000})
        got = r.answer(two)
        bars = got.get("result", {}).get("bars")
        self.assertTrue(bars, got)
        self.assertLess(len(bars), 5000)
        self.assertLess(r.lat(two), 3.0 + 0.6)
        self.assertTrue(r.answer(one).get("result", {}).get("bars"))
        self.assertEqual((slots.peak, slots.turns), (1, 1))
        # An adapter whose waiting chart waits past its own time for the
        # turn (acquire without a timeout) does not answer the second until
        # it takes the turn the first gives up at its answer, a beat later
        # (the loader looks every 0.1 s). The count read right after the
        # first's answer would pass such an adapter, so it is read again
        # after a moment, before any new request takes a turn.
        time.sleep(0.3)
        self.assertEqual((slots.peak, slots.turns), (1, 1))
        # the newest end is there: the last bar is the last closed hour
        self.assertLess(r.a.now_ms() - bars[-1][0], 3 * 3_600_000 + 96 * 3_600_000)
        r.rules.clear()
        more = until(lambda: len(r.ok("bars.history", {
            "symbol": "MSFT", "timeframe": "1h", "count": 5000})["bars"])
            > len(bars), 12, 0.5)
        self.assertTrue(more, "asking again found no more bars")


class FirstAnswer(Charts):
    """Six seconds of the old chart under a new instrument is a long
    time. A call answers with what is in hand after BARS_FIRST seconds;
    the reading goes on behind the answer; BARS_BUDGET stays the bound."""

    # the two tests of the bound itself run once, in Charts
    test_the_chart_over_the_bound_waits_its_turn_and_gets_it = None
    test_the_chart_whose_turn_does_not_come_answers_with_what_there_is = None

    def test_a_chart_is_answered_with_what_is_in_hand_and_read_on_behind_it(self):
        self.set("BARS_FIRST", 1.2)
        self.set("BARS_BUDGET", 4.0)
        r = self.rig()
        spans = self.windows(r)
        self.hang_windows(r, "AAPL", 0.5)
        one = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                      "count": 5000})
        got = r.answer(one)
        bars = got.get("result", {}).get("bars")
        self.assertTrue(bars, got)
        self.assertLess(len(bars), 5000)
        self.assertGreater(r.lat(one), 1.1)
        self.assertLess(r.lat(one), 1.2 + 0.5)
        answered = r.wire.at[one]
        # windows went on being read after the answer...
        self.assertTrue(until(lambda: [s for s in spans if s[1] > answered
                                       and s[0] == "AAPL"], 3), spans)
        # ... so that asking again a moment later returns more
        more = until(lambda: len(r.ok("bars.history", {
            "symbol": "AAPL", "timeframe": "1h", "count": 5000})["bars"])
            > len(bars), 8, 0.3)
        self.assertTrue(more, "asking again found no more bars")

    def test_a_slow_newest_end_is_waited_for_but_never_past_the_bound(self):
        self.set("BARS_FIRST", 0.8)
        self.set("BARS_BUDGET", 3.0)
        r = self.rig()
        self.hang_windows(r, "AAPL", 1.6, head=True)
        slow = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                       "count": 5000})
        got = r.answer(slow)
        self.assertTrue(got.get("result", {}).get("bars"), got)
        self.assertGreater(r.lat(slow), 1.5)        # no chart without it
        self.assertLess(r.lat(slow), 2.4)
        self.hang_windows(r, "MSFT", 6.0, head=True)
        never = r.send("bars.history", {"symbol": "MSFT", "timeframe": "1h",
                                        "count": 5000})
        got = r.answer(never)
        self.assertTrue(got.get("error", {}).get("retryable"), got)
        self.assertGreater(r.lat(never), 2.8)
        self.assertLess(r.lat(never), 3.0 + 0.6)    # the bound, kept

    def test_a_short_page_is_answered_as_soon_as_it_is_there(self):
        self.set("BARS_FIRST", 3.0)
        r = self.rig()
        page = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                       "count": 10})
        got = r.answer(page)
        self.assertEqual(len(got.get("result", {}).get("bars", [])), 10, got)
        self.assertLess(r.lat(page), 1.0)

    def test_an_answered_chart_gives_its_turn_to_one_whose_caller_waits(self):
        self.set("BARS_LOADS", 1)
        self.set("BARS_FIRST", 1.0)
        self.set("BARS_BUDGET", 5.0)
        r = self.rig()
        slots = r.a._bars_slots = self.Counted(r.a._bars_slots)
        self.hang_windows(r, "AAPL", 0.7)       # one: slow, keeps reading
        one = r.send("bars.history", {"symbol": "AAPL", "timeframe": "1h",
                                      "count": 5000})
        r.answer(one)                           # answered at about 1 s
        self.assertLess(r.lat(one), 1.6)
        two = r.send("bars.history", {"symbol": "MSFT", "timeframe": "1h",
                                      "count": 5000})
        got = r.answer(two)
        head_only = len(r.ok("bars.history", {
            "symbol": "SPY", "timeframe": "1h", "count": 10})["bars"])
        self.assertTrue(head_only)
        bars = got.get("result", {}).get("bars") or []
        # it had its turn inside its own first second: windows, not only
        # the newest end
        self.assertGreater(len(bars), 150, len(bars))
        self.assertEqual(slots.peak, 1)
        self.assertGreaterEqual(slots.turns, 2)


class Panic(HandOff):
    """flatten_all is the emergency call (SPEC 8.1): it starts the moment
    its line is read, whatever else is being answered. Seen without that:
    behind a close on a slow link it answered after 10.5 s, or "not flat"
    after 16 s with an account that was."""

    def slow(self, r, seconds):
        r.rules.add(path="/v2/", before=seconds, count=10 ** 6, name="slow")

    def test_flatten_does_not_wait_for_the_call_being_answered(self):
        r = self.rig()
        r.long_with_stop("AAPL", 10, 300.0)
        r.buy("MSFT", 5)
        self.slow(r, 0.5)
        close = r.send("position.close", {"client_id": r.cid("x"),
                                          "position_id": "AAPL", "qty": 4})
        time.sleep(0.3)
        flat = r.send("flatten_all", {"client_id": r.cid("f")})
        got = r.answer(flat)
        self.assertEqual(got.get("result"), {"closed": 2}, got)
        # behind the close it would have been its 3 s and then its own
        self.assertLess(r.lat(flat), 4.0)
        stopped = r.answer(close)
        self.assertEqual(stopped.get("error", {}).get("code"),
                         "x_alpaca_flattening", stopped)
        self.assertTrue(stopped["error"]["retryable"])
        self.assertLess(r.wire.at[close], r.wire.at[flat])
        r.rules.clear()
        self.assertEqual((r.held("AAPL"), r.held("MSFT")), (0.0, 0.0))
        self.assertTrue(until(lambda: not r.live(), 5), r.live())
        self.assertTrue(self.settled(r))

    def test_a_call_read_before_the_panic_button_and_not_begun_is_not_carried_out(self):
        r = self.rig()
        r.buy("AAPL", 3)
        r.rules.add("POST", "/v2/orders", body="MSFT", before=1.0, count=1,
                    name="slow")
        first = r.send("order.place", {"client_id": r.cid(), "symbol": "MSFT",
                                       "side": "buy", "qty": 1})
        second = r.send("order.place", {"client_id": r.cid(), "symbol": "SPY",
                                        "side": "buy", "qty": 1})
        time.sleep(0.2)
        flat = r.send("flatten_all", {"client_id": r.cid("f")})
        after = r.send("order.place", {"client_id": r.cid(), "symbol": "SPY",
                                       "side": "buy", "qty": 2})
        got = r.answer(second)
        self.assertEqual(got.get("error", {}).get("code"),
                         "x_alpaca_flattening", got)
        self.assertIn("NOT carried out", got["error"]["message"])
        self.assertIn("result", r.answer(flat), r.answer(flat))
        r.answer(first)
        # what was sent AFTER it is a new wish: carried out when it is done
        self.assertIn("result", r.answer(after), r.answer(after))
        self.assertGreater(r.wire.at[after], r.wire.at[flat])
        self.assertTrue(until(lambda: r.held("SPY") == 2.0, 5), r.held("SPY"))
        self.assertEqual((r.held("AAPL"), r.held("MSFT")), (0.0, 0.0))

    def test_what_flatten_asks_goes_out_side_by_side(self):
        """Three positions, every answer 0.6 s late: three closing orders
        one after another would be 1.8 s of the panic button's time."""
        r = self.rig()
        for sym in ("AAPL", "MSFT", "SPY"):
            r.buy(sym, 2)
        self.slow(r, 0.6)
        flat = r.send("flatten_all", {"client_id": r.cid("f")})
        got = r.answer(flat)
        self.assertEqual(got.get("result"), {"closed": 3}, got)
        # clock+positions | cancel-all | 3 closes | positions+orders: four
        # trips (one after another it was six, and more with a slow fill)
        self.assertLess(r.lat(flat), 0.6 * 4 + 1.0)
        r.rules.clear()


class Opening(HandOff):
    def test_the_session_is_opened_once_however_many_requests_come_first(self):
        r = self.rig(start=False)
        r.start(reconcile=False)
        real, opened = r.a.on_first_call, []

        def opening():
            opened.append(1)
            time.sleep(0.4)             # every other request arrives now
            real()
        r.a.on_first_call = opening
        asked = [r.send(m, p) for m, p in (
            ("account.get", None), ("positions.list", None),
            ("orders.list", {"state": "resting"}), ("catalog.list", None),
            ("order.place", {"client_id": r.cid(), "symbol": "AAPL",
                             "side": "buy", "qty": 1}))]
        for rid in asked:
            self.assertIn("result", r.answer(rid), r.answer(rid))
        self.assertEqual(opened, [1])
        self.assertEqual(r.fake.state()["streams"], 1)


class Pace(unittest.TestCase):
    """`_Budget`: who gets the next request of the pace."""

    def crowd(self, bud, seconds, readers=3, charts=2):
        """Handlers and a chart's windows asking without a pause."""
        got, stop = {"read": 0, "bulk": 0, "at": []}, \
            time.monotonic() + seconds
        lock = threading.Lock()

        def ask(host, kind, rank):
            while time.monotonic() < stop:
                try:
                    bud.take(host, kind, 1.0, rank=rank)
                except mod.AlpacaError:
                    continue
                with lock:
                    got[kind] += 1
                    got["at"].append(time.monotonic())
        # on ONE host: each host has its own pace
        threads = [threading.Thread(target=ask, args=("data", "read", 1),
                                    daemon=True) for _ in range(readers)]
        threads += [threading.Thread(target=ask, args=("data", "bulk", 0),
                                     daemon=True) for _ in range(charts)]
        for t in threads:
            t.start()
        return got, threads

    def test_a_charts_windows_get_about_every_second_request_from_polling_handlers(self):
        bud = mod._Budget(per_second=10, per_minute=100000)
        for _ in range(10):         # the first second, to nobody's count
            bud.take("data", "read", 1.0, rank=1)
        got, threads = self.crowd(bud, 3.2)
        for t in threads:
            t.join(6)
        total = got["read"] + got["bulk"]
        self.assertGreater(total, 15)
        self.assertGreater(got["bulk"], 0.35 * total, got)
        self.assertGreater(got["read"], 0.35 * total, got)

    def test_an_order_in_hand_finds_a_place_for_each_of_its_requests(self):
        """An order's requests come one after another, each when the last
        has answered: between two of them it is not waiting, and handlers
        and a chart's windows, which always are, would take every place
        that comes free. While it is in hand they leave it one request of
        every second."""
        bud = mod._Budget(per_second=3, per_minute=100000)
        got, threads = self.crowd(bud, 9.0)
        time.sleep(1.2)

        def chain(n=3, gap=0.35):
            waits = []
            for _ in range(n):
                t = time.monotonic()
                bud.take("data", "write", 3.0, rank=2)
                waits.append(round(time.monotonic() - t, 3))
                time.sleep(gap)         # its round trip to Alpaca
            return waits
        alone = chain()                 # nothing says an order is in hand
        time.sleep(0.3)
        bud.in_hand(+1)
        try:
            held = chain()
        finally:
            bud.in_hand(-1)
        before = dict(got)
        time.sleep(1.5)
        # Without it each request waits for the others' next place (a
        # third of a second); with it, for nothing once the first has
        # gone (the first may meet a second that is already full).
        self.assertGreater(sum(alone[1:]), 0.4, (alone, held))
        self.assertLess(sum(held[1:]), 0.1, (alone, held))
        # and when it is answered the others have the whole pace again
        self.assertGreaterEqual(
            got["read"] + got["bulk"] - before["read"] - before["bulk"], 3)
        for t in threads:
            t.join(10)

    def test_places_come_evenly_so_an_order_never_waits_a_second_for_its_first(self):
        """Three requests a second taken the instant they come free go
        out as one burst a second, and an order arriving in between waits
        for the next burst. Below an order they go out a third of a second
        apart; the order takes a place as soon as the second has one."""
        bud = mod._Budget(per_second=3, per_minute=100000)
        got, threads = self.crowd(bud, 6.0)
        time.sleep(1.5)
        waits = []
        for i in range(5):
            time.sleep(0.47 + 0.11 * i)         # at any point of a second
            t = time.monotonic()
            bud.take("data", "write", 3.0, rank=2)
            waits.append(round(time.monotonic() - t, 3))
        for t in threads:
            t.join(8)
        at = [t for t in got["at"] if t > got["at"][0] + 1.2]
        gaps = [round(b - a, 3) for a, b in zip(at, at[1:])]
        self.assertGreater(min(gaps), 0.25, gaps)
        # the next place, or the one after it when a read that has waited
        # a whole second is let in first; never a whole second
        self.assertLess(max(waits), 0.75, waits)

    def test_each_host_has_its_own_pace(self):
        """Alpaca counts its trading host and its data host apart. A chart
        and the prices fill the data host's second; an order and a poll on
        the trading host do not wait for them, and neither host is sent
        more than its three a second."""
        bud = mod._Budget(per_second=3, per_minute=100000)
        got, threads = self.crowd(bud, 4.0)        # all on the data host
        time.sleep(1.2)
        waits = []
        for _ in range(4):
            t = time.monotonic()
            bud.take("trading", "read", 2.0, rank=1)
            waits.append(round(time.monotonic() - t, 3))
            time.sleep(0.4)
        self.assertLess(max(waits), 0.05, waits)
        for t in threads:
            t.join(6)
        at = sorted(got["at"])
        worst = max(sum(1 for x in at if a <= x < a + 1.0) for a in at)
        self.assertLessEqual(worst, 3, "the data host was sent %d requests "
                             "inside one second" % worst)

    def test_a_handler_gets_every_other_place_while_stops_are_put_back(self):
        """Seven stops to put back ask for the whole pace, one request
        after another. A close sent meanwhile was refused "request budget
        is spent". The repair and the handlers take turns."""
        bud = mod._Budget(per_second=3, per_minute=100000)
        stop, count = time.monotonic() + 4.0, {"protect": 0}

        def repair():
            while time.monotonic() < stop:
                bud.take("trading", "protect", 6.0)
                count["protect"] += 1
        threads = [threading.Thread(target=repair, daemon=True)
                   for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(1.1)
        waits = []
        for _ in range(4):
            t = time.monotonic()
            bud.take("trading", "write", 1.5, rank=2)   # raises if starved
            waits.append(round(time.monotonic() - t, 3))
        for t in threads:
            t.join(8)
        self.assertLess(max(waits), 0.8, waits)
        self.assertGreater(count["protect"], 5)         # and it went on

    def test_a_new_charts_first_window_goes_before_an_older_charts_sixth(self):
        """Among windows the one nearest to now goes first, whoever asked
        first; and the queued windows of a chart whose caller has been
        answered step back for one whose caller still waits."""
        bud = mod._Budget(per_second=1, per_minute=100000)
        bud.take("data", "head", 0)             # this second is used
        got, lock = [], threading.Lock()

        def ask(name, order, live):
            bud.take("data", "bulk", 6.0, order=order, live=live)
            with lock:
                got.append(name)
        asked = [("old chart, sixth window", 5, lambda: True),
                 ("answered chart, first window", 0, lambda: False),
                 ("new chart, second window", 1, lambda: True),
                 ("new chart, first window", 0, lambda: True)]
        threads = []
        for name, order, live in asked:
            th = threading.Thread(target=ask, args=(name, order, live),
                                  daemon=True)
            th.start()
            threads.append(th)
            time.sleep(0.05)
        for th in threads:
            th.join(8)
        self.assertEqual(got, ["new chart, first window",
                               "new chart, second window",
                               "old chart, sixth window",
                               "answered chart, first window"])

    def test_a_read_is_not_kept_out_for_as_long_as_orders_keep_coming(self):
        """A platform that moves a stop as fast as it is answered asks
        for the whole pace with an order's rank. A read behind it waits a
        second and then takes every other place (seen without that: four
        of five positions.list refused)."""
        bud = mod._Budget(per_second=3, per_minute=100000)
        stop, sent = time.monotonic() + 5.0, [0]

        def orders():
            while time.monotonic() < stop:
                bud.take("trading", "write", 3.0, rank=2)
                sent[0] += 1
        th = threading.Thread(target=orders, daemon=True)
        th.start()
        bud.in_hand(+1)
        try:
            time.sleep(1.1)
            waits = []
            for _ in range(3):
                t = time.monotonic()
                bud.take("trading", "read", 3.0, rank=1)   # raises if kept out
                waits.append(round(time.monotonic() - t, 3))
        finally:
            bud.in_hand(-1)
        th.join(8)
        # a second of waiting, then the next place the second has
        self.assertLess(max(waits), 2.3, waits)
        self.assertGreater(min(waits), 0.9, waits)      # orders went first
        self.assertGreater(sent[0], 9)                  # and kept going

    def test_the_newest_end_of_a_chart_goes_before_polls(self):
        """One request, and without it the chart has no answer: it is
        not left in line behind handlers that poll (seen on paper: 2.9 s
        of a chart's six)."""
        bud = mod._Budget(per_second=3, per_minute=100000)
        got, threads = self.crowd(bud, 6.0)
        time.sleep(1.2)
        t = time.monotonic()
        bud.take("data", "read", 5.0, rank=1)       # one more poll, in line
        poll = time.monotonic() - t
        heads = []
        for _ in range(3):
            time.sleep(0.2)
            t = time.monotonic()
            bud.take("data", "head", 5.0)
            heads.append(round(time.monotonic() - t, 3))
        self.assertLess(max(heads), 0.75, (poll, heads))
        self.assertGreater(poll, max(heads), (poll, heads))
        for t in threads:
            t.join(8)

    def test_prices_are_not_stopped_by_handlers_and_a_chart_filling_every_second(self):
        bud = mod._Budget(per_second=3, per_minute=100000)
        # the data host as it is: two charts loading and a handler's
        # read now and then (an order's price), all without a pause
        got, threads = self.crowd(bud, 5.0, readers=1)
        time.sleep(0.5)
        waits = []
        for _ in range(3):
            t = time.monotonic()
            bud.take("data", "quotes", 3.0)     # raises if it is starved
            waits.append(time.monotonic() - t)
            time.sleep(0.2)
        self.assertLess(max(waits), 2.2, waits)
        for t in threads:
            t.join(8)


if __name__ == "__main__":
    unittest.main()
