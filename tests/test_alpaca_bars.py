"""Alpaca adapter: bars.history inside its own time budget.

Alpaca builds every timeframe between a minute and a day from minute bars
as it is asked, about 10,000 of them a request, a second or more each: the
5,000 hourly bars a chart asks for are thirty requests. So the adapter reads
bars in windows, several at once, into a store, answers with what it holds
when its budget ends, and reads on quietly. The scripted Alpaca here answers
by start and end like the real one and can be made slow or broken. No
network.
"""
import contextlib
import io
import json
import time
import unittest
from unittest.mock import patch

from brueconnect.adapter import AdapterError
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests.test_alpaca_orders import make

NOW = mod.parse_ts("2026-11-10T16:20:30Z")
H = 3_600_000
ANCHOR = NOW // mod.H4_MS * mod.H4_MS


class Bars:
    """Alpaca's bars endpoints, scripted: a bar in every bucket between
    `first` and now (the one still forming included, as Alpaca does)."""

    def __init__(self, a, b, first="2021-01-01T00:00:00Z", delay=None,
                 fail=None, trading=None):
        self.a, self.first = a, mod.parse_ts(first)
        self.delay, self.fail, self.trading = delay, fail, trading
        self.asked = []
        a.now_ms = lambda: NOW
        b.on("GET", "/v1beta3/crypto/us/bars", self.answer)
        b.on("GET", "/v2/stocks/bars", self.answer)

    def answer(self, q, body, path):
        width = {v[0]: v[1] for v in mod.TIMEFRAMES.values()}[q["timeframe"]]
        start = mod.parse_ts(q["start"])
        end = mod.parse_ts(q["end"]) if q.get("end") else NOW
        self.asked.append((start, q.get("end") and end, dict(q)))
        if self.delay:
            time.sleep(self.delay(start, q.get("end") and end) or 0)
        if self.fail:
            err = self.fail(start, q.get("end") and end)
            if err:
                raise err
        t = max(start, self.first)
        t = (t + width - 1) // width * width
        rows = []
        while t <= min(end, NOW):
            if self.trading is None or self.trading(t):
                rows.append({"t": mod.iso_ms(t), "o": 1, "h": 2, "l": 0.5,
                             "c": 1.5, "v": t % 97})
            t += width
        rows.reverse()                      # sort=desc
        return {"bars": {q["symbols"]: rows}, "next_page_token": None}


def settle(a, timeout=8.0):
    """Wait until no bar reader is running."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with a._bars_lock:
            stores = list(a._bars.values())
        if not any(s.workers or s.head_busy for s in stores):
            return
        time.sleep(0.02)
    raise AssertionError("bar readers still running")


def ask(a, symbol="BTC/USD", tf="1h", count=300):
    return a.handle_bars_history({"symbol": symbol, "timeframe": tf,
                                  "count": count})["bars"]


class History(unittest.TestCase):
    def setUp(self):
        self.a, self.b = make()
        self.addCleanup(lambda: settle(self.a))

    def test_newest_closed_bars_oldest_first_without_a_gap(self):
        src = Bars(self.a, self.b)
        rows = ask(self.a, count=300)
        self.assertEqual(len(rows), 300)
        times = [r[0] for r in rows]
        self.assertEqual(times, sorted(times))
        self.assertEqual({b - a for a, b in zip(times, times[1:])}, {H})
        # 16:00 is still forming at 16:20: the newest bar is 15:00
        self.assertEqual(times[-1], NOW // H * H - H)
        self.assertEqual(rows[0][1:5], [1.0, 2.0, 0.5, 1.5])
        json.dumps(rows, allow_nan=False)
        # every boundary is a multiple of four hours: no bar is cut in two
        for start, end, q in src.asked:
            self.assertEqual(start % mod.H4_MS, 0, q)
            if end is not None:
                self.assertEqual((end + 1) % mod.H4_MS, 0, q)
            self.assertEqual((q["sort"], q["timeframe"], q["limit"]),
                             ("desc", "1Hour", 10000))
        spans = {end + 1 - start for start, end, _ in src.asked
                 if end is not None}
        self.assertEqual(spans, {164 * H})       # crypto: 164 h a request

    def test_the_budget_ends_the_wait_not_the_reading(self):
        src = Bars(self.a, self.b, delay=lambda s, e: 0.12)
        with patch.object(mod, "BARS_BUDGET", 0.45):
            t0 = time.monotonic()
            rows = ask(self.a, tf="4h", count=5000)
            took = time.monotonic() - t0
        self.assertLess(took, 1.2)
        self.assertGreater(len(rows), 0)
        self.assertLess(len(rows), 5000)          # fewer, and no error
        times = [r[0] for r in rows]
        self.assertEqual({b - a for a, b in zip(times, times[1:])},
                         {4 * H})
        self.assertEqual(times[-1], ANCHOR - 4 * H)
        # the reading goes on quietly, one request at a time, and stops
        settle(self.a)
        store = self.a._bars[("BTC/USD", "4h")]
        self.assertLessEqual(store.extra, mod.BARS_FILL_EXTRA)
        asked = len(src.asked)
        again = ask(self.a, tf="4h", count=5000)
        settle(self.a)
        self.assertGreater(len(again), len(rows))  # further back than before
        self.assertEqual([r[0] for r in again][-len(rows):], times)
        windows = [(s, e) for s, e, _ in src.asked if e is not None]
        self.assertEqual(len(windows), len(set(windows)))   # none read twice
        self.assertGreater(len(src.asked), asked)

    def test_a_slow_window_is_never_a_hole_in_the_chart(self):
        w = 164 * H
        slow = (ANCHOR - 2 * w, ANCHOR - w - 1)             # window 1
        src = Bars(self.a, self.b, delay=lambda s, e: 0.9
                   if (s, e) == slow else 0)
        with patch.object(mod, "BARS_BUDGET", 0.4):
            rows = ask(self.a, count=600)                   # needs 0..3
        self.assertIn(slow, [(s, e) for s, e, _ in src.asked])
        times = [r[0] for r in rows]
        self.assertEqual(times[0], ANCHOR - w)              # head + window 0
        self.assertEqual({b - a for a, b in zip(times, times[1:])}, {H})
        settle(self.a)
        full = ask(self.a, count=600)
        self.assertEqual(len(full), 600)
        self.assertEqual({b - a for a, b in zip(
            [r[0] for r in full], [r[0] for r in full][1:])}, {H})

    def test_a_reload_does_not_pay_again(self):
        src = Bars(self.a, self.b)
        first = ask(self.a, tf="1m", count=100)
        settle(self.a)
        asked = len(src.asked)
        self.assertEqual(ask(self.a, tf="1m", count=100), first)
        self.assertEqual(ask(self.a, tf="1m", count=40), first[-40:])
        self.assertEqual(len(src.asked), asked)             # from the store
        # later, only the newest end is read again: from the last bar held
        store = self.a._bars[("BTC/USD", "1m")]
        store.head_at -= mod.BARS_HEAD_TTL + 1
        ask(self.a, tf="1m", count=100)
        settle(self.a)
        self.assertEqual(len(src.asked), asked + 1)
        start, end, _ = src.asked[-1]
        self.assertEqual((start, end), (first[-1][0], None))

    def test_only_nothing_at_all_is_an_error(self):
        boom = mod.AlpacaError(0, "TimeoutError: The read operation timed "
                               "out")
        Bars(self.a, self.b, fail=lambda s, e: boom)
        with patch.object(mod, "BARS_BUDGET", 0.5):
            with self.assertRaises(AdapterError) as ctx:
                ask(self.a, count=5000)
        e = ctx.exception
        self.assertEqual((e.code, e.retryable), ("internal", True))
        self.assertIn("has not sent any 1h bars for BTC/USD", str(e))
        self.assertIn("Ask again", str(e))
        settle(self.a)
        # Alpaca's own "slow down" keeps its code
        a, b = make()
        Bars(a, b, fail=lambda s, e: mod.AlpacaError(
            429, "rate limit exceeded", retry_after=1.0))
        with patch.object(mod, "BARS_BUDGET", 0.5):
            with self.assertRaises(AdapterError) as ctx:
                ask(a, count=50)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable),
                         ("rate_limited", True))
        settle(a)

    def test_our_own_spent_budget_is_said_in_plain_words(self):
        spent = mod.AlpacaError(429, "request budget for data is spent; "
                                "retry in 8.3s", code="budget",
                                retry_after=8.3)
        src = Bars(self.a, self.b, fail=lambda s, e: spent)
        with patch.object(mod, "BARS_BUDGET", 0.4):
            with self.assertRaises(AdapterError) as ctx:
                ask(self.a, count=5000)
        e = ctx.exception
        self.assertEqual((e.code, e.retryable), ("rate_limited", True))
        self.assertIn("200 data requests a minute", str(e))
        self.assertNotIn("budget", str(e))
        settle(self.a)
        # a reader the budget stopped does not hammer on: a handful of
        # tries in all, and the windows wait for the next call
        self.assertLess(len(src.asked), 12)
        store = self.a._bars[("BTC/USD", "1h")]
        self.assertTrue(store.retry)

    def test_bars_are_read_under_their_own_part_of_the_budget(self):
        bud = mod._Budget(per_second=1000)
        for _ in range(mod.BUDGET_IDLE):
            bud.take("data", "idle", 0)
        with self.assertRaises(mod.AlpacaError) as ctx:
            bud.take("data", "idle", 0)          # quiet reading stops first
        self.assertEqual(ctx.exception.code, "budget")
        for _ in range(mod.BUDGET_BULK - mod.BUDGET_IDLE):
            bud.take("data", "bulk", 0)
        with self.assertRaises(mod.AlpacaError):
            bud.take("data", "bulk", 0)          # then a chart's windows
        bud.take("data", "background", 0)        # quotes still flow
        for _ in range(mod.BUDGET_BACKGROUND_DATA - mod.BUDGET_BULK - 1):
            bud.take("data", "background", 0)
        with self.assertRaises(mod.AlpacaError):
            bud.take("data", "background", 0)
        bud.take("data", "head", 0)              # and a chart's newest end

    def test_an_orders_request_goes_before_a_charts_windows(self):
        # (A handler's READ shares the pace with a chart's windows while
        # the chart's caller waits: tests/test_alpaca_handoff.py. An
        # ORDER's request, rank 2, never does.)
        import threading
        bud = mod._Budget(per_second=1)
        bud.take("data", "bulk", 0)              # this second is used
        got = []
        t = threading.Thread(target=lambda: got.append(
            bud.take("data", "write", 3.0, rank=2) or "order sent"))
        t.start()
        time.sleep(0.2)
        with self.assertRaises(mod.AlpacaError):
            bud.take("data", "bulk", 1.4)        # must not jump the queue
        t.join(3.0)
        self.assertEqual(got, ["order sent"])
        self.assertEqual(bud._urgent, 0)

    def test_some_bars_fetched_is_an_answer_not_an_error(self):
        # the newest end arrives, everything older fails
        Bars(self.a, self.b, fail=lambda s, e: None if e is None
             else mod.AlpacaError(503, "upstream"))
        with patch.object(mod, "BARS_BUDGET", 0.5):
            rows = ask(self.a, tf="5m", count=5000)
        self.assertEqual([r[0] for r in rows][-1], NOW // 300000 * 300000
                         - 300000)
        # the newest end is read a day deep the first time (crypto)
        self.assertEqual(rows[0][0], ANCHOR - 24 * H)
        self.assertEqual(len(rows), (NOW - ANCHOR + 24 * H) // 300000)
        settle(self.a)
        # the next call tries the failed windows again, first
        a, b = make()
        calls = []

        def flaky(s, e):
            calls.append((s, e))
            if e is not None and len(calls) <= 3:
                return mod.AlpacaError(503, "upstream")
        Bars(a, b, fail=flaky)
        with patch.object(mod, "BARS_BUDGET", 0.5):
            ask(a, count=200)
            settle(a)
            rows = ask(a, count=200)
        self.assertEqual(len(rows), 200)
        settle(a)

    def test_a_four_hour_chart_has_bars_from_the_first_request_alone(self):
        # 16:20: the 16:00 four-hour bar is still forming, and every
        # window fails. The newest-end request alone must give a chart.
        Bars(self.a, self.b, fail=lambda s, e: None if e is None
             else mod.AlpacaError(429, "spent", code="budget"))
        with patch.object(mod, "BARS_BUDGET", 0.5):
            rows = ask(self.a, tf="4h", count=5000)
        self.assertEqual([r[0] for r in rows],
                         [ANCHOR - k * 4 * H for k in range(6, 0, -1)])
        settle(self.a)
        a, b = make()
        src = Bars(a, b, fail=lambda s, e: None if e is None
                   else mod.AlpacaError(503, "upstream"))
        with patch.object(mod, "BARS_BUDGET", 0.5):
            rows = ask(a, symbol="AAPL", tf="4h", count=5000)
        self.assertEqual(len(rows), 24)            # four days deep (shares)
        self.assertEqual(src.asked[0][0], ANCHOR - 96 * H)
        settle(a)

    def test_older_bars_without_the_newest_end_are_not_handed_out(self):
        Bars(self.a, self.b, fail=lambda s, e: mod.AlpacaError(
            0, "timed out") if e is None else None)
        with patch.object(mod, "BARS_BUDGET", 0.5):
            with self.assertRaises(AdapterError) as ctx:
                ask(self.a, count=100)
        self.assertTrue(ctx.exception.retryable)

    def test_reading_stops_at_the_first_bar_there_is(self):
        src = Bars(self.a, self.b, first="2026-10-20T00:00:00Z")
        rows = ask(self.a, count=5000)
        settle(self.a)
        rows = ask(self.a, count=5000)
        settle(self.a)
        self.assertEqual(rows[0][0], mod.parse_ts("2026-10-20T00:00:00Z"))
        self.assertEqual(len(rows), (NOW // H * H - rows[0][0]) // H)
        store = self.a._bars[("BTC/USD", "1h")]
        self.assertTrue(store.finished())
        asked = len(src.asked)
        self.assertLess(asked, 20)
        ask(self.a, count=5000)                       # nothing more to read
        settle(self.a)
        self.assertEqual(len(src.asked), asked)
        # a symbol with no bars at all: an empty answer, not an error
        a, b = make()
        Bars(a, b, first="2030-01-01T00:00:00Z")
        self.assertEqual(ask(a, count=10), [])
        settle(a)

    def test_day_bars_are_one_request(self):
        src = Bars(self.a, self.b, first="2025-01-01T05:00:00Z")
        rows = ask(self.a, symbol="AAPL", tf="1d", count=5000)
        settle(self.a)
        self.assertEqual(len(src.asked), 1)
        start, end, q = src.asked[0]
        self.assertEqual((start, end), (mod.BARS_FIRST_MS, None))
        self.assertEqual((q["timeframe"], q["feed"], q["adjustment"]),
                         ("1Day", "iex", "raw"))
        times = [r[0] for r in rows]
        self.assertEqual(times, sorted(times))
        # today's bar has not run its 24 hours: it is not closed
        self.assertLessEqual(times[-1] + 86_400_000, NOW)
        self.assertGreater(times[-1] + 2 * 86_400_000, NOW)

    def test_shares_are_asked_in_fortnights_from_the_share_feed(self):
        src = Bars(self.a, self.b, trading=lambda t: 14 <= t // H % 24 < 21
                   and (t // 86_400_000 + 4) % 7 < 5)
        rows = ask(self.a, symbol="AAPL", tf="1h", count=60)
        settle(self.a)
        self.assertEqual(len(rows), 60)
        self.assertEqual({(q["feed"], q["adjustment"])
                          for _, _, q in src.asked}, {("iex", "raw")})
        self.assertEqual({e + 1 - s for s, e, _ in src.asked
                          if e is not None}, {336 * H})
        src.asked.clear()
        ask(self.a, symbol="AAPL", tf="1m", count=3000)
        settle(self.a)
        self.assertEqual({e + 1 - s for s, e, _ in src.asked
                          if e is not None}, {168 * H})
        src.asked.clear()
        ask(self.a, tf="1m", count=3000)
        settle(self.a)
        self.assertEqual({e + 1 - s for s, e, _ in src.asked
                          if e is not None}, {40 * H})
        # and when the newest hours alone hold what was asked, no window
        # is read at all
        src.asked.clear()
        ask(self.a, symbol="ETH/USD", tf="1m", count=10)
        settle(self.a)
        self.assertEqual([e for _, e, _ in src.asked], [None])

    def test_count_and_timeframe_are_checked_first(self):
        src = Bars(self.a, self.b)
        for bad in ({"timeframe": "2m", "count": 1},
                    {"timeframe": "1m", "count": 0},
                    {"timeframe": "1m", "count": True},
                    {"timeframe": "1m", "count": mod.BARS_MAX_COUNT + 1}):
            with self.assertRaises(AdapterError) as ctx:
                self.a.handle_bars_history(dict({"symbol": "BTC/USD"},
                                                **bad))
            self.assertEqual(ctx.exception.code, "unsupported")
        self.assertEqual(src.asked, [])


class StoreBounds(unittest.TestCase):
    def test_few_stores_least_recently_used_out(self):
        a, b = make()
        Bars(a, b)
        with patch.object(mod, "BARS_STORES", 2):
            ask(a, "BTC/USD", "1m", 5)
            first = a._bars[("BTC/USD", "1m")]
            ask(a, "ETH/USD", "1m", 5)
            ask(a, "BTC/USD", "1m", 5)             # used again: stays
            ask(a, "BTC/USD", "5m", 5)
            settle(a)
            self.assertEqual(list(a._bars), [("BTC/USD", "1m"),
                                             ("BTC/USD", "5m")])
            self.assertIs(a._bars[("BTC/USD", "1m")], first)

    def test_idle_and_old_stores_are_dropped(self):
        a, b = make()
        Bars(a, b)
        ask(a, "BTC/USD", "1m", 5)
        ask(a, "ETH/USD", "1m", 5)
        settle(a)
        idle, old = a._bars[("BTC/USD", "1m")], a._bars[("ETH/USD", "1m")]
        idle.used -= mod.BARS_IDLE + 1
        old.made -= mod.BARS_LIFE + 1
        ask(a, "BTC/USD", "5m", 5)
        settle(a)
        self.assertEqual(list(a._bars), [("BTC/USD", "5m")])
        self.assertTrue(idle.dead and old.dead)
        ask(a, "BTC/USD", "1m", 5)                 # read afresh
        settle(a)
        self.assertIsNot(a._bars[("BTC/USD", "1m")], idle)

    def test_a_store_holds_a_bounded_number_of_bars(self):
        with patch.object(mod, "BARS_MAX_COUNT", 50):
            s = mod._BarStore(ANCHOR, 40 * H)
            s.next = 2
            s.put(0, [[ANCHOR - i * 60000, 1, 1, 1, 1, 1]
                      for i in range(1, 41)])
            self.assertFalse(s.finished())
            s.put(1, [[ANCHOR - 40 * H - i * 60000, 1, 1, 1, 1, 1]
                      for i in range(1, 41)])
            self.assertEqual(len(s.rows), 50)
            self.assertEqual(max(s.rows), ANCHOR - 60000)   # newest kept
            self.assertTrue(s.finished())                   # reads no more
            self.assertIsNone(s.take())

    def test_gaps_and_the_end_of_history(self):
        s = mod._BarStore(ANCHOR, 10 * H)
        s.next = 6
        row = lambda k: [[ANCHOR - k * 10 * H - H, 1, 1, 1, 1, 1]]
        s.put(0, row(0))
        s.put(2, row(2))                      # window 1 not read yet
        self.assertEqual(s.floor(), ANCHOR - 10 * H)
        self.assertEqual(s.have(), 1)
        s.put(1, row(1))
        self.assertEqual((s.floor(), s.have()), (ANCHOR - 30 * H, 3))
        for k in (3, 4, 5):
            s.put(k, [])
        self.assertFalse(s.finished())        # three empty: not yet
        s.next = 7
        s.put(6, [])
        self.assertTrue(s.finished())         # four in a row: the start
        self.assertEqual(s.floor(), 0)
        self.assertEqual(len(s.newest(10)), 3)


class KeyWording(unittest.TestCase):
    """What a person reads on a platform's card; variable names go to
    stderr, for whoever is wiring the adapter up."""

    def refuse(self, environ):
        out, err = io.StringIO(), io.StringIO()
        a = mod.AlpacaAdapter(argv=["--paper"], environ=environ, out=out)
        from tests.test_alpaca_orders import answer
        with contextlib.redirect_stderr(err):
            got = answer(a, "r1", "account.get", {})
        return got["error"], err.getvalue()

    def test_an_empty_key_box_is_said_in_plain_words(self):
        for environ, part in (({}, "Enter both"),
                              ({"BRUE_CRED_API_KEY": "PKX"},
                               "secret key is missing"),
                              ({"BRUE_CRED_API_SECRET": "s"},
                               "key ID is missing")):
            error, stderr = self.refuse(environ)
            self.assertEqual(error["code"], "unauthorized")
            self.assertIn("Alpaca needs your API key ID and your secret "
                          "key", error["message"])
            self.assertIn(part, error["message"])
            for jargon in ("BRUE_", "ALPACA_", "--paper", "environ"):
                self.assertNotIn(jargon, error["message"])
            self.assertIn("BRUE_CRED_API_KEY", stderr)
            self.assertIn("ALPACA_API_SECRET", stderr)

    def test_a_refused_key_is_said_in_plain_words(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            e = mod.to_adapter_error(mod.AlpacaError(401, "unauthorized"),
                                     "paper")
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("paper host", str(e))
        self.assertIn("key ID and the secret key", str(e))
        for jargon in ("ALPACA_ENV", "--paper", "--live"):
            self.assertNotIn(jargon, str(e))
        self.assertIn("ALPACA_ENV", err.getvalue())


if __name__ == "__main__":
    unittest.main()
