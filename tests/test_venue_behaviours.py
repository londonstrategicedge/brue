"""Tests for behaviour learned from the eight simulated venues.

Each one pins a venue behaviour the protocol has to allow for.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from brueconnect.client import Connector, ConnectorError  # noqa: E402
from brueconnect import protocol  # noqa: E402

SIM = [sys.executable, os.path.join(HERE, "sim_adapter.py")]


def connect(**env):
    e = dict(os.environ)
    e.update({k: str(v) for k, v in env.items()})
    c = Connector(SIM, env=e)
    c.connect()
    return c


class TestIdempotencyOwnedByPlatform(unittest.TestCase):
    """Four of eight venues accept a conflicting client_id replay and
    return the ORIGINAL order with a success status. The platform must not
    depend on the broker to catch that."""

    def test_conflicting_replay_refused_even_by_a_weak_broker(self):
        c = connect(SIM_WEAK_IDEM=1)
        try:
            c.request("order.place", {"client_id": "dup-1", "symbol": "SIMUSD",
                                      "side": "buy", "qty": 0.01})
            with self.assertRaises(ConnectorError) as ctx:
                # same id, different side: this is a client bug, not a retry
                c.request("order.place", {"client_id": "dup-1",
                                          "symbol": "SIMUSD",
                                          "side": "sell", "qty": 0.01})
            self.assertEqual(ctx.exception.code, "duplicate_client_id")
        finally:
            c.close()

    def test_identical_replay_still_allowed(self):
        """An honest retry must remain safe; idempotency is the point."""
        c = connect(SIM_WEAK_IDEM=1)
        try:
            p = {"client_id": "same-1", "symbol": "SIMUSD",
                 "side": "buy", "qty": 0.01}
            a = c.request("order.place", dict(p))
            b = c.request("order.place", dict(p))
            self.assertEqual(a["order_id"], b["order_id"])
        finally:
            c.close()

    def test_signature_ignores_key_order_and_float_spelling(self):
        s1 = protocol.order_signature(
            "order.place", {"client_id": "x", "symbol": "S", "qty": 0.10})
        s2 = protocol.order_signature(
            "order.place", {"qty": 0.1, "symbol": "S", "client_id": "y"})
        self.assertEqual(s1, s2)


class TestCannotListOrders(unittest.TestCase):
    """One venue has no enumerate-orders call at all. Reconciliation must
    not demand one; it admits the view is partial."""

    def test_can_list_false_marks_the_order_view_partial(self):
        c = connect(SIM_CAN_LIST=0)
        try:
            c.reconcile()
            self.assertTrue(c.orders_partial,
                            "platform must know its order view is incomplete")
            self.assertEqual(c.orders, {})
        finally:
            c.close()

    def test_can_list_true_reconciles_normally(self):
        c = connect(SIM_CAN_LIST=1)
        try:
            c.reconcile()
            self.assertFalse(c.orders_partial)
        finally:
            c.close()


class TestCloseReasonHonesty(unittest.TestCase):
    """Two venues report only that a position is gone. Guessing the
    reason produces a confident lie on a user's screen."""

    def test_unknown_reason_survives_the_wire(self):
        c = connect(SIM_REASON="unknown")
        try:
            got = {}
            c.on("position.closed", lambda d: got.update(d))
            c.request("order.place", {"client_id": "k1", "symbol": "SIMUSD",
                                      "side": "buy", "qty": 0.01})
            pos = c.request("positions.list")["positions"]
            c.request("position.close", {"position_id": pos[0]["position_id"]})
            c.wait_event("position.closed", timeout=10)
            self.assertEqual(got.get("reason"), "unknown")
            self.assertEqual(got.get("reason_detail"), "simulated close")
        finally:
            c.close()

    def test_close_carries_the_carry_and_fee_split(self):
        c = connect()
        try:
            got = {}
            c.on("position.closed", lambda d: got.update(d))
            c.request("order.place", {"client_id": "k2", "symbol": "SIMUSD",
                                      "side": "buy", "qty": 0.01})
            pos = c.request("positions.list")["positions"]
            c.request("position.close", {"position_id": pos[0]["position_id"]})
            c.wait_event("position.closed", timeout=10)
            for f in ("swap", "commission"):
                self.assertIn(f, got, f"position.closed must carry {f}")
                self.assertIsInstance(got[f], (int, float))
        finally:
            c.close()


class TestBrokerClock(unittest.TestCase):
    """ping must answer from the adapter's own clock: a ping on wall time
    beside events on a simulated clock makes the suite judge a broker's
    calendar against the wrong time."""

    def test_ping_matches_the_adapters_own_clock(self):
        import subprocess, json, time
        env = dict(os.environ)
        p = subprocess.Popen(
            [sys.executable, os.path.join(
                os.path.dirname(HERE), "brueconnect", "adapters", "paper",
                "paper_adapter.py"), "--tick-ms", "500"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            bufsize=1, env=env)
        try:
            for msg in ({"v": 1, "id": "h", "type": "req", "method": "hello",
                         "params": {"protocol": 1,
                                    "client": {"name": "t", "version": "1"}}},
                        {"v": 1, "id": "p", "type": "req", "method": "ping",
                         "params": {}}):
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
            deadline = time.time() + 10
            ping_t = None
            while time.time() < deadline and ping_t is None:
                line = p.stdout.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("id") == "p":
                    ping_t = o["result"]["time"]
            self.assertIsNotNone(ping_t, "no ping reply")
            # The paper simulator's clock is anchored well away from wall time
            # on purpose; ping must report ITS clock, not ours.
            self.assertLess(ping_t, time.time() * 1000 - 86400_000,
                            "ping is answering from wall time again")
        finally:
            p.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
