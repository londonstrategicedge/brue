# Brue

**A Python-like scripting language for trading charts.**

Brue is a small, focused language for writing chart annotations, backtests, and live trading scripts. It is bar-by-bar, indentation-based, runs in an isolated WebWorker, and ships with 143 built-in indicator and statistics functions plus the strategy / drawing / live-trading primitives.

```python
strategy("EMA Crossover", overlay=true)

fast_len = input(9,  "Fast EMA length")
slow_len = input(21, "Slow EMA length")

fast = ema(close, fast_len)
slow = ema(close, slow_len)

plot(fast)              # explicit opt-in: show fast EMA on the chart
plot(slow)              # explicit opt-in: show slow EMA on the chart

if crossover(fast, slow):
    shape(arrow_up,   location=below_bar, color=green, size="large", text="BUY")
if crossunder(fast, slow):
    shape(arrow_down, location=above_bar, color=red,   size="large", text="SELL")
```

## What's in the language

A Brue script can do any combination of:

- **Draw on the chart**: `shape`, `label`, `bgcolor`, `hline`, `table`
- **Plot indicator lines**: `plot(ema(close, 20))` registers the EMA as a chart-native indicator. Bare `ema(close, 20)` is just a value — wrap in `plot()` to show it.
- **Run a backtest**: `entry`, `exit`, `close_all` plus `position.*` and `strategy.*` namespaces
- **Send live orders**: `buy`, `sell`, `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, `close` (only fire on the most recent bar so historical replay is safe)
- **Compute indicators**: 143 built-in functions across moving averages, oscillators, trend, volatility, volume, statistics, math, and time
- **Pull a second instrument**: `use SYMBOL [at TF] [as ALIAS]` to bring another symbol or higher timeframe into the script
- **Query external datasets**: `use "econ"`, `use "earnings"`, `use "cot"`, `use "insider"` for point-in-time economic, fundamental, and positioning data

## Documentation

- [SYNTAX.md](SYNTAX.md), complete language reference. Every built-in, every primitive, every keyword, with signatures and examples. Includes the [Plotting Indicators](SYNTAX.md#plotting-indicators) section that covers the Pine-style `plot()` opt-in.
- [examples/](examples/), runnable `.brue` scripts.
- [CHANGELOG.md](CHANGELOG.md), version history.

## Design principles

1. **Python-like.** If you can read Python, you can read Brue. No `series float` qualifiers, no scope restrictions, no arbitrary plot/label limits.
2. **Bar-by-bar with implicit series.** Every variable has a value on every bar. Access history with `[N]`: `close[1]` is the previous close, `rsi(close, 14)[3]` is the RSI value from 3 bars ago.
3. **One declaration.** Every script begins with `strategy("Title", ...)`. There is no `indicator()`. A script that doesn't call `entry()` / `exit()` / `close_all()` simply behaves as a drawings-only script.
4. **Pine-style explicit `plot()`.** Bare `ema(close, 20)` returns a value — same as Pine's `ta.ema(...)`. To show the EMA on the chart, wrap it: `plot(ema(close, 20))`. The runtime hands the request to the chart host's indicator system, where the user can recolour / hide / delete it via the standard cog. Arbitrary expressions (e.g. `plot(some_correlation_value)`) are silently no-ops; for those, add an indicator from the chart host's built-in menu directly.
5. **AI-friendly.** The language is designed so AI models can write correct Brue from a single prompt. Python-like syntax, clear naming, no ambiguity. The full reference is one file (`SYNTAX.md`).

## Quick syntax tour

```python
# Variables
x = close * 2          # resets every bar
persist counter = 0    # keeps value across bars
const THRESHOLD = 70   # immutable

# History operator
delta = close - close[1]

# Multi-return functions destructure
upper, mid, lower = bb(close, 20, 2.0)

# Control flow (Python-style)
if rsi(close, 14) > 70:
    bgcolor(rgba(255, 0, 0, 0.1))
elif rsi(close, 14) < 30:
    bgcolor(rgba(0, 255, 0, 0.1))

# Cross-pair access
use "GBP/USD" as gbp
spread = close - gbp.close

# External dataset access (economic calendar, earnings, COT, insider)
use "econ" as eco
use "earnings" as er
nfp  = eco.latest("NFP")     # most recent NFP release at or before this bar
nvda = er.latest("NVDA")     # most recent NVDA quarterly earnings
if not is_na(nfp.actual) and nfp.actual < 0:
    shape(circle, bar_index, close, color=red)

# User-defined functions
def trend_score(len):
    a, plus_di, minus_di = adx(len)
    return a * (1 if plus_di > minus_di else -1)

# Inputs (auto-render in the host's settings dialog)
length = input(14, "Length", min=2, max=200)
mode   = input("ema", "Smoothing", options=["sma", "ema", "wma"])
```

See [SYNTAX.md](SYNTAX.md) for the complete reference.

## Status

Brue is in active development. The language surface is stable, but new built-in functions are added regularly. Breaking changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
