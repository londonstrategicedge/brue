# Brue

Brue is the trade-entry language. A Brue script writes buy/sell/close
decisions over bars, and the same file runs identically in a historical run
and live against a broker. Brue is for two things and nothing else: writing
trade entries, and connecting them to brokers.

Two products, one install:

- **Brue, the language** (this package, `import brue`): the script engine
  and the Python entry points. `.brue` scripts are pure trading logic: no
  header, no credentials, no configuration block. What a script trades, at
  what quantity, from which data source, at which timeframe is written where
  it acts.
- **Brue Connect** (`brueconnect`, bundled inside this package): the broker
  connector. One wire protocol, every broker an adapter: the built-in paper
  account, the LSE demo account, adapters for supported brokers, and a
  starter adapter to fill in for any other broker. It ships with the language, so
  there is nothing else to install; the language calls it.

```
pip install brue-language
```

(`brue-language` is the distribution; the import is `brue`. The bare `brue` name
on PyPI belongs to someone else.)

## A script

```brue
fast = ema(EURUSD.h1.close, 9)
slow = ema(EURUSD.h1.close, 21)
if crossover(fast, slow):
    buy("EURUSD", 0.5, sl=EURUSD.h1.close * 0.995, tp=EURUSD.h1.close * 1.01)
if crossunder(fast, slow):
    close("EURUSD")
```

Over history, from the command line:

```
python -m brue run trend.brue eurusd_h1.csv --capital 100000 --commission 0.001
```

The same, from Python, and then live:

```python
import brue

bars = brue.parse_bars_csv(open("eurusd_h1.csv").read())
out = brue.run_script(open("trend.brue").read(), bars)      # historical run
out.commands                                                # entries, exits, pnl

brue.login(api_key="lse_...")          # the only credential that ever appears
brue.buy("EURUSD", 0.5, sl=1.05, tp=1.12)                   # live, via Brue Connect
brue.close("EURUSD")
```

`brue.buy`, `brue.sell` and `brue.close` trade the built-in paper account on
your machine. To trade at a broker (the LSE demo account, a supported
broker, or an adapter of your own), run the script with
`python -m brueconnect.runner.live_driver --broker <name>`, or in the LSE
Terminal.

Historical runs simulate the fills (next-bar-open, brackets, commission,
slippage) and report what was executed: the trade records, the log() lines,
the canonical JSON. There is no stats block, no equity frame and no pandas
indicator surface in this package; research on the results happens in plain
Python on the host. Indicator scripts (`plot()` calls, no orders) are exposed
through `brue.Indicator` for chart hosts. The language reference is
`SYNTAX.md`; the boundary of what Brue owns is `ARCHITECTURE.md`.

## Why the decisions can be trusted

The engine is conformance-locked: `conformance/run_all_py.sh` runs the
corpus (every construct of the language plus the loud-error cases) and
byte-compares the execution JSON against locked goldens. A fill or a formula
cannot silently drift, and a live run re-derives the same order intents from
the same script text.

## Brue Connect

A platform (the LSE Terminal, a notebook, a Brue script) talks to a broker
through an adapter: a small program on your own machine that speaks one
line-delimited JSON protocol on stdio. Write the adapter once and the broker
works in every platform built on the protocol. Broker keys are kept on your
machine and reach only the adapter, never a protocol message.

- `brueconnect/`: the SDK (`Adapter`, `Connector`, the registry of linked
  brokers), the adapters that ship, the starter adapter
  `brueconnect/templates/my_broker_adapter.py`, and the conformance suite
  that proves an adapter (`python -m brueconnect.conformance.run`).
- [SPEC.md](https://github.com/londonstrategicedge/brue/blob/main/SPEC.md): the protocol. It is normative.
- [ONBOARDING.md](https://github.com/londonstrategicedge/brue/blob/main/ONBOARDING.md): the questionnaire for a broker, before
  anyone writes code.
- [BRUE_CONNECT.md](https://github.com/londonstrategicedge/brue/blob/main/BRUE_CONNECT.md): how to write, test and list an adapter.
- `tests/`: the connector's tests (`python3 -m unittest discover -s tests`,
  about 20 minutes).

## Status

Version 0.0.2. Pure Python: the engine and the connector use the standard
library only (pandas is optional and only used by the convenience frames in
`brue.live` and `brue.Indicator.compute`; `websockets`, when it is installed,
lets the LSE demo adapter stream its fills).

## Account login

Anything account-bound (LSE data, placing orders) starts with your LSE API
key, the same key you use on the data platform:

```python
import brue
brue.login(api_key="lse_...")   # or BRUE_API_KEY / LSE_API_KEY env,
brue.account()                  # or ~/.config/brue/config.json
```

Historical runs on your own bars never need a key: the engine runs fully
offline.
