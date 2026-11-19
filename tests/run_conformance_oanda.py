#!/usr/bin/env python3
"""The conformance suite against the OANDA adapter, pointed at the stand-in.

Starts tests/fake_oanda.py on loopback, then for each sub-account asked for
(netting: the UK default; hedging; mt4: an MT4-linked one, on which the
adapter never sets clientExtensions) runs

    python -m brueconnect.conformance.run --adapter "<python> oanda_adapter.py --paper" --restartable

with the adapter pointed at the stand-in (OANDA_REST_URL, OANDA_STREAM_URL),
the stand-in's fake token as BRUE_CRED_TOKEN, BRUE_ACCOUNT_ID set to that
sub-account and a fresh BRUE_BROKER_STATE (under TMPDIR). Before each run the
adapter is asked `accounts.list` once, as the Terminal does before it binds
an account, so the cold `hello` states the account's currency and model.

Prints the suite's output and exits with its code (non-zero if any run
failed). No --partial-hint is given, on purpose: OANDA never fills an FX,
metal, index or commodity order in parts (execution policy, Annex 1; the
adapter sends market orders fill-or-kill), so `order.partial` followed by
`order.filled` cannot happen on OANDA and the suite's partial-fill scenario
SKIPs. After each run the stand-in's violations() (clientExtensions on an
MT4-linked account, one order split into many at one price) must be empty.

Market noise. Inside an hour the stand-in's walk is a straight line between
a few knots minutes apart, so a price can climb for ten minutes without one
tick back, and the suite's bracket scenario (a stop one tick under the bid,
trailed up every 5 s, six minutes to fire) becomes a coin flip. A real
market's ticks go both ways all the time. So, unless --no-noise is given,
every instrument is moved by one to three ticks, up or down at random, twice
a second, through the stand-in's own `jump_pct` control (the walk carries on
from the moved price). Nothing about the adapter changes; the suite then
converges as it does on a broker.

    python tests/run_conformance_oanda.py [--accounts netting,hedging,mt4]
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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brueconnect import Connector  # noqa: E402
from tests.fake_oanda import FakeOanda  # noqa: E402

ADAPTER = os.path.join(ROOT, "brueconnect", "adapters", "oanda",
                       "oanda_adapter.py")
# The stand-in's three UK sub-accounts, in its order (fake_oanda.Broker.reset)
KINDS = {"netting": 0, "hedging": 1, "mt4": 2}


def noise(fake: FakeOanda, stop: threading.Event, seed: int = 11):
    """One to three ticks up or down, at random, on every instrument, twice
    a second (see the module docstring)."""
    rng = random.Random(seed)
    names = sorted(fake.market.instruments)
    while not stop.wait(0.5):
        for name in names:
            inst = fake.market.instruments[name]
            try:
                mid = float(fake.market.mid(name))
                ticks = rng.choice((-1, 1)) * rng.randint(1, 3)
                fake.set_price(name, jump_pct=ticks * float(inst.tick) / mid
                               * 100.0)
            except Exception:  # noqa: BLE001 - a closed market, a race
                pass


def run_one(fake: FakeOanda, kind: str, extra: list) -> int:
    state = tempfile.mkdtemp(prefix="conf-oanda-%s-" % kind)
    env = dict(os.environ)
    env.update({"OANDA_REST_URL": fake.rest_url,
                "OANDA_STREAM_URL": fake.stream_url,
                "BRUE_CRED_TOKEN": fake.token, "BRUE_BROKER_STATE": state,
                "PYTHONPATH": ROOT + os.pathsep + env.get("PYTHONPATH", "")})
    env.pop("BRUE_ACCOUNT_ID", None)
    env.pop("OANDA_ENV", None)
    cmd = [sys.executable, ADAPTER, "--paper"]
    # What the Terminal does before binding: list the accounts (cached in
    # the profile's state for the cold hello of the bound process).
    c = Connector(cmd, env=env)
    try:
        c.hello()
        accounts = c.request("accounts.list")["accounts"]
    finally:
        c.close()
        for pipe in (c.proc.stdin, c.proc.stdout):
            pipe.close()
    # OANDA counts new connections per address (2 a second): the bound
    # process starts after that second, as a platform's would after a list.
    time.sleep(1.2)
    aid = accounts[KINDS[kind]]["id"]
    env["BRUE_ACCOUNT_ID"] = aid
    print("== conformance, OANDA %s sub-account %s (state %s)" % (
        kind, aid, state), flush=True)
    suite = [sys.executable, "-m", "brueconnect.conformance.run",
             "--adapter", shlex.join(cmd), "--restartable"] + extra
    t0 = time.monotonic()
    code = subprocess.call(suite, cwd=ROOT, env=env)
    print("== %s: exit %d in %.0f s" % (kind, code, time.monotonic() - t0),
          flush=True)
    bad = fake.violations()
    if bad:
        print("== %s: the stand-in recorded violations: %s" % (kind, bad),
              flush=True)
        code = code or 1
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="netting,hedging,mt4",
                    help="comma list of netting, hedging, mt4")
    ap.add_argument("--suite-args", default="",
                    help="more arguments for the suite (quoted)")
    ap.add_argument("--no-noise", action="store_true",
                    help="leave the stand-in's walk as it is")
    args = ap.parse_args()
    kinds = [k.strip() for k in args.accounts.split(",") if k.strip()]
    for k in kinds:
        if k not in KINDS:
            ap.error("unknown account kind %r" % k)
    fake = FakeOanda().start()
    print("stand-in: rest %s, stream %s, market noise %s" % (
        fake.rest_url, fake.stream_url, "off" if args.no_noise else "on"),
        flush=True)
    stop = threading.Event()
    if not args.no_noise:
        threading.Thread(target=noise, args=(fake, stop), daemon=True,
                         name="market-noise").start()
    worst = 0
    try:
        for i, k in enumerate(kinds):
            if i:
                time.sleep(1.2)      # the last run's connections are a
                #                      second old: OANDA counts per address
            worst = max(worst, run_one(fake, k, shlex.split(args.suite_args)))
    finally:
        stop.set()
        fake.stop()
    return worst


if __name__ == "__main__":
    sys.exit(main())
