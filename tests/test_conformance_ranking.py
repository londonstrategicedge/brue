"""The conformance suite's choice of instrument for its stop scenario.

`rank_by_movement` is given recorded quote sequences. The wide one is what
a broker's free quote feed (one exchange's book) showed on 2026-11-10 for
shares the suite then picked: the stop it set rested untouched for six
minutes and the scenario FAILED an adapter that had done nothing wrong (MOD
at 14:54, HMC at 18:01, AMPL at 20:35 UTC).
"""
import unittest

from brueconnect.conformance.run import rank_by_movement


def inst(symbol, tick=0.01, stops=0):
    return {"symbol": symbol, "tick_size": tick, "stops_level_points": stops}


def flicker(symbol, n=12):
    """HMC, 18:01 UTC: the share trades at 34.70; the feed shows 29.78 /
    34.71 and now and then a real bid. The mid jumps by 2.4 dollars."""
    out = []
    for k in range(n):
        out.append((symbol, 34.67 if k % 3 == 2 else 29.78, 34.71))
    return out


def moving(symbol, start=350.00, step=0.04, spread=0.02, n=12):
    """A liquid share: two cents wide, drifting four cents a quote."""
    return [(symbol, round(start + k * step, 2),
             round(start + k * step + spread, 2)) for k in range(n)]


def still(symbol, bid=120.00, spread=0.01, n=12):
    return [(symbol, bid, round(bid + spread, 2)) for _ in range(n)]


class Ranking(unittest.TestCase):
    def test_a_wide_flickering_quote_loses_to_a_tight_moving_one(self):
        cands = [inst("HMC"), inst("AAPL")]
        quotes = flicker("HMC") + moving("AAPL")
        self.assertEqual(rank_by_movement(cands, quotes), ["AAPL", "HMC"])
        # the old measure (movement of the mid over the tick size) put the
        # flickering quote first, by a wide margin
        mid = [(b + a) / 2 for _, b, a in flicker("HMC")]
        old_hmc = (max(mid) - min(mid)) / 0.03
        old_aapl = (11 * 0.04) / 0.03
        self.assertGreater(old_hmc, 5 * old_aapl)

    def test_tight_instruments_come_before_wide_ones_whatever_they_score(self):
        # a tight share that hardly moved is still a better place for a
        # stop a few ticks under the bid than a five-dollar-wide quote
        cands = [inst("HMC"), inst("QUIET"), inst("AAPL")]
        quotes = flicker("HMC") + still("QUIET") + moving("AAPL")
        self.assertEqual(rank_by_movement(cands, quotes),
                         ["AAPL", "QUIET", "HMC"])

    def test_among_wide_ones_movement_over_the_observed_spread_decides(self):
        # crypto on a retail feed: every spread is many ticks. The one
        # that travels several spreads beats the one that flickers inside
        # its own.
        cands = [inst("BTC/USD", tick=0.01), inst("THIN/USD", tick=0.01)]
        btc = [("BTC/USD", 67000.0 + 15 * k, 67012.0 + 15 * k)
               for k in range(12)]
        thin = [("THIN/USD", 10.00 if k % 2 else 9.40, 10.60)
                for k in range(12)]
        self.assertEqual(rank_by_movement(cands, btc + thin),
                         ["BTC/USD", "THIN/USD"])

    def test_an_instrument_that_barely_quotes_is_left_out(self):
        cands = [inst("AAPL"), inst("RARE")]
        quotes = moving("AAPL") + moving("RARE", n=4)
        self.assertEqual(rank_by_movement(cands, quotes), ["AAPL"])

    def test_a_simulator_with_exact_ticks_ranks_as_before(self):
        # one tick wide everywhere: the observed spread IS the tick, and
        # the order is movement over stop distance, as it was
        cands = [inst("EURUSD", tick=0.00001, stops=10),
                 inst("BTCUSD", tick=0.1, stops=10)]
        eur = [("EURUSD", 1.08500 + 0.00002 * k, 1.08501 + 0.00002 * k)
               for k in range(12)]
        btc = [("BTCUSD", 68000.0 + 4.0 * k, 68000.1 + 4.0 * k)
               for k in range(12)]
        self.assertEqual(rank_by_movement(cands, eur + btc),
                         ["BTCUSD", "EURUSD"])


if __name__ == "__main__":
    unittest.main()
