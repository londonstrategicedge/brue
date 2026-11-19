"""Quote scheduling preserves capacity, responsiveness and visible failure state."""
import io
import threading
import unittest
from collections import Counter
from email.utils import formatdate
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from brueconnect.adapters.lse_sim import lse_sim_adapter as mod


class Quotes(unittest.TestCase):
    def setUp(self):
        self.a = mod.LSESimAdapter()
        self.a._spec = lambda s: {"symbol": s}
        self.events = []
        self.calls = []
        self.clock = 0.0
        self.patcher = patch.object(mod.time, "monotonic", lambda: self.clock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.a.emit = lambda name, data: self.events.append((name, data))
        self.a._http = self.answer

    def answer(self, method, url, **kwargs):
        symbols = parse_qs(urlsplit(url).query)["symbols"][0].split(",")
        self.calls.append((self.clock, symbols))
        return {"data": [dict(symbol=s, bid=10.25, ask=10.5,
                              ts="2026-10-27T07:30:01.123456Z") for s in symbols]}

    def subscribe(self, *symbols):
        return self.a.handle_quotes_subscribe({"symbols": list(symbols)})

    def test_acknowledgement_has_no_http_and_duplicates_are_coalesced(self):
        self.assertEqual(self.subscribe("BTC/USD", "BTC/USD"), {})
        self.subscribe("BTC/USD")
        self.assertEqual(self.calls, [])
        self.a._quote_step()
        self.assertEqual(self.calls, [(0, ["BTC/USD"])])
        self.assertEqual([e for e in self.events if e[0] == "quote"][0][1],
                         dict(symbol="BTC/USD", bid=10.25, ask=10.5, time=1793086201123))

    def test_hundred_symbols_receive_round_robin_pages_and_global_budget(self):
        names = [f"S{i:03}" for i in range(100)]
        self.subscribe(*names[:50]); self.subscribe(*names[50:])
        for now in (0, .1, 1.99, 2, 3.99, 4):
            self.clock = now; self.a._quote_step()
        self.assertEqual([x[0] for x in self.calls], [0, 2, 4])
        self.assertEqual([x[1] for x in self.calls], [names[:50], names[50:], names[:50]])

    def test_continuous_ticket_changes_do_not_starve_held_positions(self):
        held = [f"P{i:03}" for i in range(99)]
        self.subscribe(*held, "T0")
        for i in range(60):
            self.a.handle_quotes_unsubscribe({"symbols": [f"T{i}"]})
            self.subscribe(f"T{i+1}")
            self.clock = i * .5
            self.a._quote_step()
        counts = Counter(s for _, syms in self.calls for s in syms)
        self.assertTrue(all(counts[s] >= 7 for s in held), counts)
        self.assertTrue(all(len(syms) <= 50 for _, syms in self.calls))
        self.assertEqual(len(self.a._quote_pending), len(set(self.a._quote_pending)))

    def test_concurrent_prime_poll_and_unsubscribe_do_not_block_or_emit_late_quote(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            result = self.answer(*args, **kwargs)
            entered.set()
            if not release.wait(2): raise AssertionError("test did not release HTTP")
            return result
        self.a._http = slow
        self.subscribe("OLD")
        thread = threading.Thread(target=self.a._quote_step)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.subscribe("NEW"), {})
            self.a.handle_quotes_unsubscribe({"symbols": ["OLD"]})
            self.a._quote_step()
            self.assertEqual(len(self.calls), 1)
        finally:
            release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([e for e in self.events if e[0] == "quote"], [])
        self.clock = 2
        self.a._quote_step()
        self.assertEqual(self.calls[-1][1], ["NEW"])

    def test_http_completion_budget_caps_every_fixed_minute_at_thirty(self):
        self.subscribe(*(f"S{i}" for i in range(100)))
        def varying(*args, **kwargs):
            response = self.answer(*args, **kwargs)
            self.clock += (len(self.calls) % 5) * .017
            return response
        self.a._http = varying
        while self.clock < 181:
            self.subscribe("S0")
            self.a._quote_step()
            self.clock += .01
        self.assertTrue(all(n <= 30 for n in Counter(int(t // 60) for t, _ in self.calls).values()))
        self.assertTrue(all(b[0] - a[0] >= 2 for a, b in zip(self.calls, self.calls[1:])))

    def test_rate_limit_wait_includes_new_subscriptions_and_recovers(self):
        self.subscribe("OLD")
        error = mod.AdapterError("rate_limited", "customer cap", True)
        error.retry_after_s = 17.25
        self.a._http = lambda *a, **k: (_ for _ in ()).throw(error)
        self.assertEqual(self.a._quote_step(), 17.25)
        self.assertIn("customer cap", self.events[-1][1]["detail"])
        self.assertEqual(self.events[-1][1]["state"], "degraded")
        self.a._http = self.answer
        self.subscribe("NEW")
        self.clock = 17.24; self.a._quote_step()
        self.assertFalse(self.calls)
        self.clock = 17.25; self.a._quote_step()
        self.assertEqual(set(self.calls[0][1]), {"OLD", "NEW"})
        self.assertEqual(self.events[-1][1], {"state": "connected", "detail": ""})

    def test_quote_recovery_does_not_hide_execution_stream_failure(self):
        self.a._set_session_state("stream", {"state": "degraded", "detail": "stream reconnecting"})
        self.subscribe("BTC/USD"); self.a._quote_step()
        self.assertEqual([e for e in self.events if e[0] == "session.state"][-1][1],
                         {"state": "degraded", "detail": "stream reconnecting"})
        self.a._set_session_state("quotes", {"state": "degraded", "detail": "price cap"})
        self.a._set_session_state("stream", {"state": "connected", "detail": ""})
        self.assertEqual(self.events[-1][1], {"state": "degraded", "detail": "price cap"})
        self.a._set_session_state("quotes", {"state": "connected", "detail": ""})
        self.assertEqual(self.events[-1][1], {"state": "connected", "detail": ""})

    def test_malformed_response_is_visible_and_poll_recovers(self):
        self.subscribe("BTC/USD")
        for malformed in ([], {}, {"data": None}, {"data": {}}):
            self.a._http = lambda *a, **k: malformed
            self.a._quote_step()
            self.assertEqual(self.events[-1][1]["state"], "degraded")
            self.assertIn("expected data array", self.events[-1][1]["detail"])
            self.clock += 2
        self.a._http = self.answer
        self.a._quote_step()
        self.assertEqual(self.events[-1][1]["state"], "connected")

    def test_unsubscribe_removes_pending_and_shutdown_stops_future_http(self):
        self.subscribe("A", "B")
        self.a.handle_quotes_unsubscribe({"symbols": ["A"]})
        self.a._quote_step()
        self.assertEqual(self.calls[0][1], ["B"])
        self.a.on_shutdown()
        self.clock = 10; self.a._quote_step()
        self.assertEqual(len(self.calls), 1)

    def test_real_worker_is_single_and_shutdown_wakes_idle_wait(self):
        self.a.start_quote_poll()
        thread = self.a._quote_thread
        self.a.start_quote_poll()
        self.assertIs(self.a._quote_thread, thread)
        self.a.on_shutdown()
        thread.join(1)
        self.assertFalse(thread.is_alive())


class RetryAfter(unittest.TestCase):
    def check(self, header, expected):
        adapter = mod.LSESimAdapter()
        adapter.key = "unit-test-key"
        body = io.BytesIO(b'{"detail":"price request limit reached"}')
        error = HTTPError("https://test.invalid/v1/prices", 429, "limited",
                          {} if header is None else {"Retry-After": header}, body)
        with patch.object(mod.urllib.request, "urlopen", side_effect=error), \
             patch.object(mod.time, "time", return_value=1793086213), \
             patch.object(mod.random, "uniform", return_value=.25):
            with self.assertRaises(mod.AdapterError) as caught:
                adapter._http("GET", "/v1/prices")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.retry_after_s, expected + .25)
        self.assertEqual(body.tell(), len(body.getvalue()))

    def test_numeric_retry_after(self): self.check("7", 7)
    def test_http_date_retry_after(self): self.check(formatdate(1793086223, usegmt=True), 10)
    def test_no_header_uses_next_fixed_minute(self): self.check(None, 47)
    def test_invalid_header_uses_next_fixed_minute(self): self.check("NaN", 47)


if __name__ == "__main__": unittest.main()
