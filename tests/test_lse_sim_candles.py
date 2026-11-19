"""The broker's closed-bar history contract, exercised without an account."""
import os
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from brueconnect.adapters.lse_sim import lse_sim_adapter as mod


class History(unittest.TestCase):
    def setUp(self):
        self.a = mod.LSESimAdapter()
        self.a._catalog = {'BTC/USD': {'symbol': 'BTC/USD'}}
        self.calls = []
        self.now = 1793078501

    def answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        q = parse_qs(urlsplit(url).query)
        end = mod.dt.datetime.fromisoformat(q['end'][0]).timestamp()
        return [dict(ts=mod.dt.datetime.fromtimestamp(end-60*(i+1), mod.dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f'),
                     open=10, high=12, low=9, close=11, volume=3)
                for i in range(int(q['limit'][0]))]

    def read(self, count=10):
        with patch.object(mod.time, 'time', return_value=self.now):
            return self.a.handle_bars_history({'symbol':'BTC/USD', 'timeframe':'1m', 'count':count})['bars']

    def test_full_paging_with_exclusive_boundaries(self):
        self.a._http = self.answer
        bars = self.read(6000)
        self.assertEqual(len(bars), 6000)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(c[1].startswith('/vault/candles?') and not c[2] for c in self.calls))
        self.assertEqual({b[0] for b in bars}, set(range(bars[0][0], bars[-1][0]+1,60000)))
        self.assertLess(bars[-1][0], (self.now//60*60)*1000)

    @unittest.skipUnless(hasattr(time, "tzset"), "process timezone override requires tzset")
    def test_naive_vault_times_are_utc_in_a_non_utc_process(self):
        previous = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'Pacific/Honolulu'; time.tzset()
            self.a._http = self.answer
            bars = self.read(1)
            self.assertEqual(bars[0][0], (self.now//60*60-60)*1000)
        finally:
            if previous is None: os.environ.pop('TZ', None)
            else: os.environ['TZ'] = previous
            time.tzset()

    def test_empty_history_is_empty(self):
        self.a._http = lambda *a, **kw: []
        self.assertEqual(self.read(), [])

    def test_short_page_stops_without_inventing_rows(self):
        self.a._http = lambda *a, **kw: self.answer(*a, **kw)[:3]
        self.assertEqual(len(self.read(20)), 3)
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_or_wrong_order_is_not_silently_accepted(self):
        def bad(*a, **kw):
            rows = self.answer(*a, **kw)
            rows[1] = rows[0]
            return rows
        self.a._http = bad
        with self.assertRaisesRegex(mod.AdapterError, 'times do not move backwards'):
            self.read()

    def test_remote_error_survives_without_partial_history(self):
        def fail(*a, **kw): raise mod.AdapterError('rate_limited','customer call limit',True)
        self.a._http = fail
        with self.assertRaisesRegex(mod.AdapterError, 'customer call limit'):
            self.read()

    def test_closed_bar_event_is_emitted_once(self):
        self.a._http = self.answer
        self.a._bar_subs = {('BTC/USD','1m')}
        events = []
        self.a.emit = lambda *a: events.append(a)
        with patch.object(mod.time, 'time', return_value=self.now):
            self.a._poll_bars(); self.a._poll_bars()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]['bar'][0], (self.now//60*60-60)*1000)

    def test_malformed_shape_and_invalid_count_fail(self):
        self.a._http = lambda *a, **kw: {'data':[]}
        with self.assertRaisesRegex(mod.AdapterError, 'invalid candle response'):
            self.read()
        for count in (-1,100001,'wrong'):
            with self.assertRaises(mod.AdapterError): self.read(count)


if __name__ == '__main__': unittest.main()
