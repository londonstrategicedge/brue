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
  connector. One wire protocol, every broker an adapter, the built-in paper
  account first. It ships with the language, so there is nothing else to
  install; the language calls it.

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

## Status

Version 0.0.1. Pure Python, stdlib-only engine (pandas is optional and
only used by the convenience frames in `brue.live` and
`brue.Indicator.compute`).

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
