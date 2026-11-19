# Brue's job: the decision layer

Brue is the trade-entry language: buy/sell/close scripts over bars that run
identically in a historical run and live against a broker through Brue
Connect. Brue is for writing trade entries and connecting them to brokers;
nothing else is Brue.

One sentence: given bars, Brue decides what a strategy does, and it makes
those decisions identically everywhere. Everything else in a trading
pipeline belongs to something else.

```
  DATA             ->      BRUE          ->     EXECUTION          ->  RESULTS
  pandas / feeds /      the strategy         historical: simulated     trade records,
  broker bars / csv     brain                fills inside Brue         log() lines
                                             live: order intents  ->   Brue Connect
                                                                       broker adapter
                                                                       (user's account)
```

## What Brue OWNS

1. **Indicators.** ema, rsi, atr and the rest of the 144 builtins, computed
   with fixed, conformance-locked semantics. Same inputs, same bits, on
   every implementation.
2. **Strategy logic.** Entries, exits, conditions, long/short. The part a
   trader actually thinks about, written in a language built for exactly
   that.
3. **Position rules.** Sizing (including risk-based sizing off stop
   distance), stops, targets, trailing stops, commission.
4. **In a historical run: the simulator.** Next-bar-open fills (an order
   placed on the signal bar fills at the NEXT bar's open, never the signal
   bar), bracket handling, and the trade records. This is where "same
   script, same numbers" is enforced. (There is no stats block, equity
   frame or research helper in the engine: Brue executes, research
   happens in plain Python on the host.)
5. **Live is the same script.** buy()/sell()/close() are the one order
   vocabulary. In a historical run they drive the simulator; in a live run
   the identical calls become broker order intents handed to Brue Connect.
   The RUNNER decides historical vs live, never the script (no mode= flag).

## What Brue deliberately does NOT do

- It never fetches data. Bars come in from the host (pandas frame, csv,
  a feed, a broker terminal).
- It never holds broker credentials.
- It never talks to a broker directly. Executing an intent is Brue
  Connect's job, through whatever adapter the user connects with their own
  account; broker credentials live only on the trader's machine.
- It never touches the filesystem or network from inside a script. A
  strategy is pure logic over bars, which is what makes it safe to run in
  a sandbox, in a browser, or on someone else's machine.

That boundary is the product: the strategy is a portable artifact. The
same script runs in the desktop terminal, a Python session via
`brue.run_script(source, bars)`, and live against any connected broker
through Brue Connect, and every one of those produces bit-identical
decisions because they are all the same conformance-tested engine. The pipe around the strategy is swappable; the
strategy is yours.

## How the pieces line up in Python

```python
import brue                    # decisions: Brue's job; data is the host's job

bars = brue.parse_bars_csv(open("EURJPY_1h.csv").read())
out = brue.run_script(open("trend.brue").read(), bars)   # historical run
out.commands                   # trade_entry / trade_exit records with pnl
out.to_json()                  # the canonical execution JSON

brue.login(api_key="lse_...")  # then the same vocabulary goes live
brue.buy("EURJPY", 0.5); brue.close("EURJPY")
```

`brue.run_script_full` adds named feeds (`ExternalData`) and run parameters
(`RunOptions`); `brue.Indicator` exposes an indicator script's plot() series
to chart hosts. See SYNTAX.md section 21.

The engine is locked by the conformance corpus: every construct's output
is byte-compared against frozen goldens (`conformance/run_all_py.sh`).

## What the package delivers

- **Engine.** The tokenizer, parser and runtime for the language, pure
  Python, stdlib only.
- **Python entry points.** `brue.run_script`, `brue.run_script_full`
  (named feeds via `ExternalData`, run parameters via `RunOptions`),
  `brue.parse_bars_csv`, `brue.Indicator`, and the `python -m brue` CLI.
- **Live driver.** `brue.live` (login, buy/sell/close, positions, balance,
  history) plus `RunOptions.capture_intents`, which derives each closed
  bar's order intents from the script's own window state so live and
  historical runs share one decision path.
- **Broker adapters.** Brue Connect (`brueconnect`, shipped inside this
  package; its guide is `BRUE_CONNECT.md`): the wire standard, the paper
  account, and the broker adapters behind it. Credentials stay on the
  trader's machine.
- **Spec and conformance suite.** `SYNTAX.md` is the rulebook;
  `conformance/` holds the corpus and the frozen goldens so third-party
  tooling can target the language without semantic drift.
