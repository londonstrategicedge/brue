#!/usr/bin/env python3
"""The conformance suite against the Capital.com adapter, pointed at the
stand-in (tests/fake_capitalcom.py) on this machine.

Starts the stand-in on 127.0.0.1 (REST and stream on ports of their own),
gives the adapter its fake credentials and endpoints through the
environment the suite spawns it with (as a platform does: BRUE_CRED_*,
CAPITALCOM_API_URL, CAPITALCOM_STREAM_URL, a fresh BRUE_BROKER_STATE), runs

    python -m brueconnect.conformance.run --adapter "<python> <adapter> --paper"
        --restartable --partial-hint ETHUSD:0.2

prints the suite's output and exits with its code.

The stand-in runs as a demo account would: `--market auto` (each market on
its own opening hours, so the suite finds a shut market for its
market_closed check and an open one, the cryptocurrencies, on any day), a
starting balance of 10,000 (a new demo has 1,000; the suite's flatten
scenario holds three minimum orders of the first market that will deal,
BTCUSD at about 900 each with 50% margin; Capital.com's demo can be topped up
to 100,000, documented: https://open-api.capital.com/), and ETHUSD orders of
0.2 filled in two parts (the stand-in's --partial-rule; the suite's
--partial-hint).

Market noise, as the kit's other conformance runner has it. The suite's
bracket scenario sets its stop the broker's minimum distance (plus a tick)
under the bid and trails it up every 5 s, six minutes to fire, so it fires
only when the bid falls back that far between two trails. Inside a minute
the stand-in's price moves along a line between the minute's open and
close, so in a minute that climbs it does not fall back, and the scenario
timed out about one run in two. A real market's
prices go both ways all the time. So, unless --no-noise is given, every
market that is open is moved up or down at random twice a second, through
the stand-in's own `jump_pct` control (the price carries on from where it
was moved). Where a stop may sit a tick from the price, one to three ticks
of noise are enough; here the least distance is Capital.com's own
(minStopOrProfitDistance: a percentage of the price for most markets), so
each move is 0.3 to 0.8 of that distance: a few moves together reach a
trailed stop, and no one move crosses a stop set from a fresh quote.
Nothing about the adapter changes.

    python tests/run_conformance_capitalcom.py [--market auto|open|closed]
        [--balance N] [--no-restart] [--no-partial] [--seed N] [--no-noise]
"""
from __future__ import annotations

import argparse
import os
import random
import shlex
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from fake_capitalcom import FakeCapitalCom  # noqa: E402

ADAPTER = os.path.join(ROOT, "brueconnect", "adapters", "capitalcom",
                       "capitalcom_adapter.py")
PARTIAL = ("ETHUSD", 0.2)


# Before its first trail (the suite trails the bracket scenario's stop every
# 5 s), a freshly opened stop-protected position is held clear of its stop,
# so it is not stopped out before that trail and the suite exercises
# order.modify (with symmetric noise the stop fires first, and order.modify
# goes almost untested).
GRACE_MS = 6500


def _fresh_stop_floors(fake: FakeCapitalCom, now: int) -> dict:
    """For each epic holding a stop-protected position opened within the last
    GRACE_MS, the levels the market must stay clear of: {epic: (min_bid,
    max_ask)}. min_bid is the highest such long's entry (hold the bid at or
    above it), max_ask the lowest such short's entry. A long's stop sits
    below its entry and a short's above, so holding the market at the entry
    keeps a fresh stop out of reach until its first trail, whatever the
    price path does. Never raises (a deal may open or close as it reads)."""
    longs: dict = {}
    shorts: dict = {}
    try:
        for acct in fake.broker.accounts.values():
            for d in list(acct.deals.values()):
                if d.stop is None or not (0 <= now - d.created < GRACE_MS):
                    continue
                if d.direction == "BUY":
                    longs[d.epic] = max(longs.get(d.epic, d.level), d.level)
                else:
                    shorts[d.epic] = min(shorts.get(d.epic, d.level), d.level)
    except Exception:  # noqa: BLE001 - a race against the order engine
        pass
    return {e: (longs.get(e), shorts.get(e))
            for e in set(longs) | set(shorts)}


def _noise_tick(fake: FakeCapitalCom, rng: random.Random):
    """One pass of the market noise: every open market moved up or down by
    0.3 to 0.8 of its own minimum stop distance. A market with a fresh
    stop-protected position is held clear of that stop and ratcheted up
    (so the scenario reaches its first trail), through an absolute hold
    rather than a jump off the moving path (_fresh_stop_floors)."""
    now = fake.broker.now()
    floors = _fresh_stop_floors(fake, now)
    market = fake.market
    for inst in market.instruments:
        try:
            if market.status(inst, now) != "TRADEABLE":
                continue
            mid = market.mid(inst.epic)
            step = inst.distance(inst.stop_min, mid) * rng.uniform(0.3, 0.8)
            if inst.epic not in floors:
                fake.set_price(inst.epic, jump_pct=rng.choice((-1, 1))
                               * step / mid * 100.0)
                continue
            min_bid, max_ask = floors[inst.epic]
            bid, ask = market.quote(inst.epic)
            spread = ask - bid
            target = mid + step                       # ratchet up
            if min_bid is not None:                   # clear of a long's stop
                target = max(target, min_bid + spread / 2.0 + step)
            if max_ask is not None:                   # clear of a short's stop
                target = min(target, max_ask - spread / 2.0 - step)
            fake.set_price(inst.epic, mid=target)
        except Exception:  # noqa: BLE001 - a market shut meanwhile
            pass


def noise(fake: FakeCapitalCom, stop: threading.Event, seed: int = 11):
    """Every open market moved up or down at random, twice a second, by 0.3
    to 0.8 of its own minimum stop distance (see the module docstring); a
    market with a freshly opened stop-protected position is held clear of
    its stop until its first trail (_noise_tick)."""
    rng = random.Random(seed)
    while not stop.wait(0.5):
        _noise_tick(fake, rng)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--market", choices=("auto", "open", "closed"),
                    default="auto")
    ap.add_argument("--balance", default="10000")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--no-partial", action="store_true")
    ap.add_argument("--no-noise", action="store_true",
                    help="leave the stand-in's prices as they are")
    args = ap.parse_args(argv)
    fake = FakeCapitalCom(seed=args.seed, market=args.market,
                          balances=(("USD", args.balance), ("GBP", "1000")))
    fake.start()
    stop = threading.Event()
    if not args.no_noise:
        threading.Thread(target=noise, args=(fake, stop), daemon=True,
                         name="market-noise").start()
    try:
        if not args.no_partial:
            fake.fill_rule(PARTIAL[1], [1, 1], PARTIAL[0])
        state = tempfile.mkdtemp(prefix="capitalcom-conformance-")
        env = dict(os.environ)
        env.update({"BRUE_CRED_API_KEY": fake.api_key,
                    "BRUE_CRED_IDENTIFIER": fake.identifier,
                    "BRUE_CRED_PASSWORD": fake.password,
                    "CAPITALCOM_API_URL": fake.api_url,
                    "CAPITALCOM_STREAM_URL": fake.stream_url,
                    "BRUE_BROKER_STATE": state,
                    "PYTHONPATH": ROOT + (os.pathsep + env["PYTHONPATH"]
                                          if env.get("PYTHONPATH") else "")})
        env.pop("CAPITALCOM_ENV", None)
        adapter = shlex.join([sys.executable, ADAPTER, "--paper"])
        # -u: the suite's lines are printed as each scenario ends
        cmd = [sys.executable, "-u", "-m", "brueconnect.conformance.run",
               "--adapter", adapter]
        if not args.no_restart:
            cmd.append("--restartable")
        if not args.no_partial:
            cmd += ["--partial-hint", "%s:%s" % PARTIAL]
        print("stand-in: %s (stream %s), market %s, balance %s, market "
              "noise %s; state %s" % (fake.api_url, fake.stream_url,
                                      args.market, args.balance,
                                      "off" if args.no_noise else "on",
                                      state), flush=True)
        print("suite: %s" % shlex.join(cmd), flush=True)
        return subprocess.call(cmd, cwd=ROOT, env=env)
    finally:
        stop.set()
        fake.stop()


if __name__ == "__main__":
    sys.exit(main())
