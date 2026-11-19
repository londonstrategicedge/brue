"""The Capital.com conformance runner's market noise. With symmetric noise
the suite's bracket scenario (s08) is stopped out before its first trail
(every 5 s), so order.modify, which only that trail sends, goes almost
untested. A freshly opened stop-protected position is held clear of its
stop until the grace window passes, so it reaches its first trail; after
the window the noise is symmetric and the stop can fire.

This tests the noise's rule deterministically, holding the price even when
the all-down worst case fights it. The full runner is an integration run,
not part of this unit test.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_conformance_capitalcom as runner  # noqa: E402
from fake_capitalcom import FakeCapitalCom, Deal  # noqa: E402


class _AlwaysDown:
    """An rng that moves every market the most it can, downward: the worst
    case for a long position's stop."""

    def uniform(self, a, b):
        return b

    def choice(self, seq):
        return -1


class TestTheNoiseHoldsAFreshStopClear(unittest.TestCase):
    EPIC = "US500"          # a percentage min-distance, so the noise bites

    def _fake(self):
        return FakeCapitalCom(seed=7, market="auto",
                              balances=(("USD", "10000"), ("GBP", "1000")))

    def _put_stop_deal(self, f, created, step_mult=12.0):
        """A long on EPIC, entered at the ask, with a stop a wide multiple
        of the minimum distance under the bid (as the suite sets it from
        stops_level_points), created at `created` (broker ms). Returns the
        stop level."""
        acct = next(iter(f.broker.accounts.values()))
        bid, ask = f.market.quote(self.EPIC)
        inst = f.market.by_epic[self.EPIC]
        stop = bid - step_mult * inst.distance(inst.stop_min,
                                               f.market.mid(self.EPIC))
        d = Deal()
        d.deal_id, d.epic, d.direction = "d1", self.EPIC, "BUY"
        d.level, d.stop, d.created = ask, stop, created
        acct.deals[d.deal_id] = d
        return stop

    def test_a_fresh_stop_is_held_clear_but_an_aged_one_is_reached(self):
        f = self._fake()
        rng = _AlwaysDown()
        stop = self._put_stop_deal(f, created=f.broker.now())       # fresh
        self.assertIn(self.EPIC, runner._fresh_stop_floors(f, f.broker.now()))
        # Worst case: the all-down noise fights the hold, and the price path
        # moves on its own, yet the bid never falls to the stop.
        for _ in range(40):              # ~20 s of ticks, back to back
            runner._noise_tick(f, rng)
            self.assertGreater(f.market.quote(self.EPIC)[0], stop,
                               "a fresh stop was reached before its trail")
        # Age the position past the grace window. The same all-down noise now
        # drives it down to the stop: the grace, not the noise, held it.
        acct = next(iter(f.broker.accounts.values()))
        acct.deals["d1"].created = f.broker.now() - runner.GRACE_MS - 1000
        self.assertNotIn(self.EPIC,
                         runner._fresh_stop_floors(f, f.broker.now()))
        reached = False
        for _ in range(600):
            runner._noise_tick(f, rng)
            if f.market.quote(self.EPIC)[0] <= stop:
                reached = True
                break
        self.assertTrue(reached, "aged: the noise never reached the stop, so "
                        "the test does not show the grace is what held it")

    def test_only_a_stop_protected_young_position_is_held(self):
        f = self._fake()
        now = f.broker.now()
        self.assertEqual(runner._fresh_stop_floors(f, now), {})
        acct = next(iter(f.broker.accounts.values()))
        # a deal with no stop is not held
        d = Deal()
        d.deal_id, d.epic, d.direction, d.level, d.stop, d.created = (
            "n", self.EPIC, "BUY", 100.0, None, now)
        acct.deals["n"] = d
        self.assertEqual(runner._fresh_stop_floors(f, now), {})
        # a stop-protected deal is held only inside the window
        self._put_stop_deal(f, created=now)
        self.assertIn(self.EPIC, runner._fresh_stop_floors(f, now))
        self.assertNotIn(self.EPIC, runner._fresh_stop_floors(
            f, now + runner.GRACE_MS + 1))


if __name__ == "__main__":
    unittest.main()
