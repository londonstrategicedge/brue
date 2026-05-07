# Brue Language Reference

Brue is the scripting language for London Strategic Edge. It is Python-like, indentation-based, and runs bar-by-bar on candlestick chart data inside an isolated WebWorker.

> **For AI models:** Read this entire document before generating Brue code. Every function, identifier, and example below is verified against the runtime. If a function is not listed here, it does not exist. Do not invent functions from Pine Script, MQL4/5, or earlier Brue versions.

---

## Behaviour Rules (read before every response)

### Ask before you code — missing critical parameters

Before writing a strategy script, identify what is missing using the rules below, then ask for all missing items in one short message. Do NOT guess defaults. Do NOT generate the script until you have the answers.

**Symbol and timeframe — declare them on `strategy()`, always:**
- Every strategy that uses any bar-count-dependent indicator (`ema`, `sma`, `rsi`, `atr`, `macd`, `bb`, `adx`, `stoch`, `cci`, `supertrend`, `ichimoku`, or any function that takes a period/length argument) MUST declare both `symbol="..."` and `timeframe="..."` on `strategy(...)`. A 14-bar ATR on 1m vs 1D are completely different numbers; a strategy without an explicit timeframe is unreproducible.
- Pure event-driven strategies (NFP release, earnings beat, economic data surprise) with fixed pip/point/dollar stops are the one exception — timeframe is irrelevant because the event fires on one bar regardless of candle size.
- Do NOT use `input_timeframe(...)` to set the script's timeframe — `input_timeframe` is a UI control that returns a string but does not bind data. Only `strategy(timeframe="...")` actually drives bar fetching.
- When the user names a symbol or timeframe in the prompt, use that. When they say "this symbol" or "the chart's timeframe", default to the chart's currently loaded values, but still write them explicitly so the script is portable.

**Stop loss — always ask if not specified:**
- ATR multiple (e.g. 1.5×ATR(14)) — timeframe-dependent, so ask timeframe too
- Fixed pips/points/dollars (e.g. 20 pips, $2.00) — timeframe-independent
- Swing high/low — needs a lookback period, ask timeframe
- None — valid, but confirm explicitly

**Take profit — always ask if not specified:**
- Fixed R multiple (e.g. 2R = 2× the stop distance)
- Fixed pips/points/dollars
- Trailing stop
- Exit on signal flip
- None — valid, but confirm explicitly

**Also ask if genuinely ambiguous:**
- Direction: long only, short only, or both?
- Entry trigger: if the user's description could mean multiple things

**When NOT to ask anything:** Drawing-only scripts (shapes, labels, tables, hlines) with no `entry()`/`exit()` calls. Just write them.

### Scale-mismatch check — never cross two series on different scales

Before writing `crossover(A, B)` or `crossunder(A, B)`, verify that A and B live on the same numeric scale. If they don't, the cross will literally never happen and the strategy will fire zero trades.

Common scale mismatches to NEVER write:
- ❌ `crossover(rsi_val, ema_val)` — RSI is 0-100, EMA is a price level (e.g. 5500). Will never cross.
- ❌ `crossover(rsi_val, close)` — same problem.
- ❌ `crossover(stoch_val, sma_val)` — Stoch is 0-100, SMA is a price level.
- ❌ `crossover(macd_line, close)` — MACD is a small number around 0, close is hundreds or thousands.

Correct patterns when the user says "RSI/EMA cross" (ambiguous — ask if unsure):
- ✅ **RSI vs its own EMA**: `rsi_ema = ema(rsi_val, 9); if crossover(rsi_val, rsi_ema):` — both are 0-100.
- ✅ **EMA as trend filter alongside RSI**: `if rsi_val < 30 and close > ema_val:` (long mean-reversion only when above 200-EMA trend).
- ✅ **MACD line vs MACD signal**: same MACD scale.

When the user names two indicators and says "cross", default to interpretation 2 (use one as a filter for the other) and EXPLAIN the choice in your reply, OR ask which they meant. Never silently emit a cross between incompatible scales.

The same rule applies to fixed thresholds: don't write `if rsi_val > 50` and call it a "trend filter" when the user is on a price-level series — that's RSI, fine, but `if close > 50` on gold would always be true.

### Threshold realism — pick numbers that actually fire

When picking entry thresholds (correlation levels, RSI levels, z-score cutoffs, etc.), pick values that match how the indicator actually behaves on the asset, not arbitrary round numbers. Common LoRA mistakes that produce zero trades:

- **Correlation breakdown**: `correlation(close, other.close, N)` is Pearson on raw prices. For trending pairs (FX/equity index) it sits near +1 most of the time. Use `< 0.5` for "breakdown" (not `< 0.6`), and `> 0.95` for "very tight" (not `> 0.8`). For correlation on returns (which Brue does NOT have natively — you'd compute `change(close)`), use `< 0.3` and `> 0.7`.
- **RSI overbought/oversold**: 70/30 is the textbook default. For mean-reversion on already-ranging assets use 80/20. Anything tighter than 65/35 fires too often.
- **Z-score**: standard cutoffs are ±2 (rare event) or ±1.5 (frequent event). ±3 is almost never.
- **ATR-based stops**: 1×ATR is tight (gets stopped often), 2×ATR is room to breathe, 3×ATR is loose. Match to the entry's expected hold time.

When thresholds depend on asset behaviour you don't have data for, ADD a debug output: `t.cell(0, 0, f"corr={corr_val:.2f}", color=white)` so the user can see the live value range and adjust. Better: ask the user "what threshold range have you seen this hit on this asset?"

**Format:** One message, bullet list, concise. Example:
> Quick questions before I write this:
> - **Timeframe** — which candle size? (needed because this uses ATR)
> - **Stop loss** — ATR-based, fixed pips, or something else?
> - **Take profit** — fixed R, trailing, or exit on flip?

Once the user answers, write the script immediately without asking again.

---

## What Brue does, and what it does NOT do

Brue is a **strategy + chart-drawing layer**. It is NOT the chart's indicator system.

A Brue script can:
- **Draw markers on the chart**: `shape`, `label`, `hline`, `bgcolor`, `table`
- **Run a backtest**: `entry`, `exit`, `close_all` plus the `position.*` and `strategy.*` namespaces
- **Send live orders to the user's paper account**: `buy`, `sell`, `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, `close`
- **Compute on price**: 143 built-in functions (moving averages, oscillators, trend, volatility, volume, statistics, math, time)
- **Pull a second instrument** with `use SYMBOL [at TF] [as ALIAS]` and reference it as `alias.close`, `alias.high`, etc.
- **Query external datasets** with `use "econ"`, `use "earnings"`, `use "cot"`, `use "insider"` and call `.latest(key)` / `.prev(key)` for point-in-time correct field access.

A Brue script cannot:
- **Render arbitrary continuous series** with `plot()`. The `plot()` call exists, but it only registers chart-native indicators (`ema`, `sma`, `rsi`, `macd`, `bb`, `stoch`, `atr`, `vwap`, `adx`, `cci`, `roc`) on the host's indicator panel. `plot(some_arbitrary_expression)` is a no-op. For arbitrary series, add an indicator from the chart's built-in indicator menu directly.
- **Fire alerts** to a notification queue. Mark interesting bars with `shape()` instead.

If you came from Pine Script, Brue's `plot()` works the same way as Pine's: bare `ema(close, 20)` is a value, `plot(ema(close, 20))` is what makes the chart show it. The line/box/polyline/alert APIs do not exist; mark bars with `shape()` and `label()` instead.

---

## Table of Contents

1. [Script Structure](#script-structure)
2. [Execution Model](#execution-model)
3. [Built-in Price Variables](#built-in-price-variables)
4. [Variables and Data Types](#variables-and-data-types)
5. [History Operator](#history-operator)
6. [Operators](#operators)
7. [Control Flow](#control-flow)
8. [User-Defined Functions](#user-defined-functions)
9. [Cross-Pair Access](#cross-pair-access)
10. [External Datasets](#external-datasets)
11. [Types and Enums](#types-and-enums)
12. [Strategy Execution](#strategy-execution)
13. [Live Trading Verbs](#live-trading-verbs)
14. [User Inputs](#user-inputs)
15. [Drawing Primitives](#drawing-primitives)
16. [Plotting Indicators](#plotting-indicators)
17. [Colors](#colors)
18. [Math and Logic](#math-and-logic)
19. [Time Functions](#time-functions)
20. [Moving Averages (15)](#moving-averages)
21. [Oscillators (31)](#oscillators)
22. [Trend Indicators (15)](#trend-indicators)
23. [Volatility (16)](#volatility)
24. [Volume Indicators (14)](#volume-indicators)
25. [Statistics and Regression (8)](#statistics-and-regression)
26. [Removed and Unsupported](#removed-and-unsupported)

---

## Script Structure

Every script begins with one declaration and only one declaration: `strategy(...)`. There is no `indicator()` keyword.

```python
strategy("My Script", overlay=true,
         symbol="NVDA", timeframe="1H",
         capital=10000)
```

**`strategy()` parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| title | string (required, positional) |, | Name shown in the legend |
| `overlay` | bool | `true` | `true` draws on the price chart, `false` opens a separate panel |
| `symbol` | string literal | inherited from chart | Pin the script to a specific symbol so backtests are reproducible across charts. When set, the runtime fetches bars for this symbol regardless of which pair the chart is showing. When omitted, the chart's loaded symbol drives data |
| `timeframe` | string literal | inherited from chart | Pin the script to a specific bar size (`"1m"`, `"5m"`, `"15m"`, `"1H"`, `"4H"`, `"1D"`, `"1W"`). Required when the script uses any bar-count-dependent indicator (`ema`, `sma`, `rsi`, `atr`, `macd`, `bb`, `adx`, `stoch`, `cci`, `supertrend`, `ichimoku`) — a 14-bar ATR on 1m vs 1D are completely different numbers |
| `capital` / `initial_capital` | number | `10000` | Backtest starting equity |
| `commission` | number | `0` | Commission rate |
| `commission_type` | string | `"percent"` | `"percent"` or `"fixed"` |
| `slippage` | number | `0` | Slippage in price units |
| `pyramiding` | number | `0` | Max additional same-direction entries |
| `default_qty` | number | `1` | Default order size |
| `default_qty_type` | string | `"contracts"` | `"contracts"`, `"cash"`, or `"percent_of_equity"` |
| `precision` | number | inferred | Decimal places to display in P&L summaries |
| `currency` | string | `"USD"` | Account currency label |

**Closed kwarg set.** Anything not in the table above is rejected at parse time with a "did you mean..." suggestion. Typos like `comission=0.001` are now hard errors instead of silently dropped. The closed set is the contract; new kwargs require a language change, not a runtime guess.

A strategy script that does not call `entry()` / `exit()` / `close_all()` simply behaves as a "drawings only" script. The backtest engine spins up but stays idle. There is no separate "indicator mode".

---

## Execution Model

- The script runs **once per bar**, oldest to newest.
- Every variable gets a fresh value each bar unless declared `persist` (kept across bars) or `const` (set once).
- Every regular variable is implicitly a **series**, it has a value on every bar, accessible with `[N]` history.
- Two function flavours:
  - **Series-level** (`sma`, `rsi`, `macd`, …), computed once over the whole dataset, then read per bar. Cached.
  - **Bar-level** (`abs`, `max`, `crossover`, `is_na`, …), called every bar.
- Scripts run in a WebWorker with a 10-second timeout. If the script hangs it is killed; it cannot freeze the chart.
- Live trading verbs (`buy`, `sell`, `close`, etc.) are **silently no-op on every historical bar** and only fire on the most recent bar of the run, so replaying a script never leaks fake orders.

---

## Built-in Price Variables

Available on every bar without import.

| Variable | Type | Description |
|----------|------|-------------|
| `open` | float | Bar open |
| `high` | float | Bar high |
| `low` | float | Bar low |
| `close` | float | Bar close |
| `volume` | float | Bar volume |
| `hl2` | float | `(high + low) / 2` |
| `hlc3` | float | `(high + low + close) / 3` |
| `ohlc4` | float | `(open + high + low + close) / 4` |
| `time` | int | Bar open time, Unix milliseconds |
| `bar_index` | int | 0-based bar number |
| `last_bar_index` | int | Index of the final bar |
| `last_bar` | bool | `true` only on the final bar |
| `is_first_bar` | bool | `true` when `bar_index == 0` |
| `is_realtime` | bool | `true` only on the final bar (historical mode); reserved for future live ticks |
| `is_new_bar` | bool | `true` (always true in historical mode; reserved for live mode) |

Direction constants used by trade calls: `long`, `short` (bare identifiers, not strings).

---

## Variables and Data Types

```python
# Regular variable, resets every bar
x = close * 2

# Persistent: keeps its value across bars
persist counter = 0
counter = counter + 1

# Constant: set once, immutable
const THRESHOLD = 70
```

Compound assignment is supported: `+=`, `-=`, `*=`, `/=`.

**Types** (inferred, never declared):
- `float`, `14`, `3.14`, `close`, `sma(close, 20)`
- `bool`, `true`, `false`, `close > open`
- `string`, `"hello"`, f-string `f"Price: {close:.2f}"`
- `color`, `red`, `rgb(255, 0, 0)`, `rgba(255, 0, 0, 0.5)`, `with_alpha(red, 0.3)`, `#FF5733`, `#FF573380`
- `na`, missing value (like NaN). Literal: `na`. Helpers: `is_na(x)`, `coalesce(x, default)`
- `array`, `[1, 2, 3]`
- `map`, `{key: value}`

Multi-assignment for tuple-returning builtins:

```python
upper, mid, lower = bb(close, 20, 2.0)
macd_line, signal, hist = macd(close, 12, 26, 9)
k, d = stoch_rsi(close, 14, 14, 3, 3)
```

---

## History Operator

`[N]` returns the value N bars ago. Works on any expression, including function calls.

```python
close[1]                  # previous bar's close
high[5]                   # high 5 bars back
rsi(close, 14)[1]         # previous RSI value
sma(close, 20)[3]         # SMA from 3 bars ago

if rsi(close, 14) > rsi(close, 14)[1]:
    shape(triangle_up, location=below_bar, color=green)
```

Returns `na` if the index goes before the first bar.

---

## Operators

- Arithmetic: `+`, `-`, `*`, `/`, `%`, `**` (power)
- Comparison: `==`, `!=`, `<`, `<=`, `>`, `>=`
- Logical: `and`, `or`, `not` (keywords, not symbols)
- Ternary: `value_if_true if cond else value_if_false`

```python
bar_color = green if close > open else red
trend = "up" if close > sma(close, 50) else "down"
```

---

## Control Flow

```python
# if / elif / else
if close > sma(close, 200):
    bgcolor(rgba(0, 255, 0, 0.05))
elif close < sma(close, 200):
    bgcolor(rgba(255, 0, 0, 0.05))
else:
    bgcolor(rgba(128, 128, 128, 0.02))

# for-loop with range
for i in range(5):
    hline(close + i * 10, color=gray)

# for-loop over array
for v in [1, 2, 3]:
    # ...
    pass  # not actually a keyword; bodies just need at least one statement

# while-loop
persist level = close
while level < high:
    level = level + atr(14)

# break / continue work as expected
```

---

## User-Defined Functions

```python
def trend_score(len):
    adx_val, plus_di, minus_di = adx(len)
    slope = linreg_slope(close, len)
    alignment = 1 if plus_di > minus_di else -1
    return adx_val * alignment

score = trend_score(14)
```

Default parameter values:

```python
def my_rsi(src, len=14):
    return rsi(src, len)
```

---

## Cross-Pair Access

Bring a second instrument into the script with `use`. The line lives **between the `strategy(...)` declaration and the first executable statement**, not inside the body.

```python
strategy("EUR vs GBP", symbol="EUR/USD", timeframe="1H")
use "GBP/USD" as gbp           # quoted symbol, custom alias
use SPY at 1D as bench         # different timeframe, custom alias
use AAPL                       # alias defaults to AAPL

# Body
spread = close - gbp.close
if correlation(close, gbp.close, 30) > 0.6:
    # ...
```

**Foreign field access:** after `use FOO as f`, you can read `f.open`, `f.high`, `f.low`, `f.close`, `f.volume`, `f.hl2`, `f.hlc3`, `f.ohlc4`. Other field names error out instead of silently returning `na`.

Multiple `use` lines are allowed. Quoted symbols (anything containing `/` or special characters) require `as ALIAS` because `EUR/USD` is not a valid bare identifier.

The host pre-fetches and time-aligns the foreign series (forward-filled to the chart's bar grid) before the script runs, so foreign reads are zero-cost during execution.

### Multi-timeframe foreign access

`use SYMBOL at TIMEFRAME as alias` fetches the foreign symbol at the requested timeframe and forward-fills it onto the script's primary bar grid. The forward-fill is the standard MTF lookback discipline: between higher-TF bar closes, the foreign value stays at its most recent close — no lookahead. When a new higher-TF bar closes, the value updates on the next primary bar. Pine Script's `request.security(..., lookahead=barmerge.lookahead_off)` is the same shape.

```python
strategy("MTF: 1H trend, 5m entry",
         symbol="NVDA", timeframe="5m")
use NVDA at 1H as h1
use NVDA at 1D as d1     # broader regime

trend_up_1h    = h1.close > ema(h1.close, 50)
trend_up_daily = d1.close > ema(d1.close, 200)
pullback_5m    = close < open and rsi(close, 14) < 40

if trend_up_1h and trend_up_daily and pullback_5m and position.size == 0:
    entry("L", long)
```

Cross-pair plus cross-TF is the same syntax — three foreign series at three different timeframes, all forward-filled and indexed against the primary bar grid:

```python
strategy("Pair trade with HTF gate",
         symbol="EUR/USD", timeframe="15m")
use "GBP/USD" at 4H as gbp_4h
use SPY      at 1D as spy_d1
use DXY      as dxy              # same-TF as primary
```

Two `use` lines on the same symbol but different timeframes each get their own fetch (and their own alias). Two `use` lines on the same `(symbol, timeframe)` share one fetch; the dedupe key is the pair, not the alias.

---

## External Datasets

Brue ships four curated datasets that carry point-in-time (PIT) correct fundamental and macro data. Declare them with `use "DATASET_NAME" as ALIAS` alongside your OHLCV `use` lines:

```python
strategy("Gold macro filter")
use "XAU/USD" as gold          # OHLCV cross-pair (existing feature)
use "econ" as eco              # economic calendar releases
use "earnings" as er           # public-company quarterly earnings
use "cot" as cot_data          # CFTC Commitment of Traders
use "insider" as ins           # SEC insider + senate trades
```

**Key rules:**
- Dataset aliases use `use "name" as alias` (always quoted, `as ALIAS` required).
- Dataset aliases do **not** support `at TIMEFRAME`. Writing `use "econ" at 1h as eco` is a parse error.
- Access data via `.latest(key)` and `.prev(key)` — not via `.open`, `.close`, etc.
- Fields are `na` (null) before the first snapshot for that key in the chart's date range. Test with `is_na(snap.field)`.
- PIT guarantee: on bar `N` at time `T`, `.latest(key)` returns the most recent snapshot with `release_time <= T`. The script never sees future data.

### `.latest(key)` and `.prev(key)`

```python
nfp = eco.latest("NFP")       # most recent NFP snapshot at or before this bar
nfp_prev = eco.prev("NFP")    # second-most-recent NFP snapshot (one release back)
```

Both return a snapshot object or `na` if fewer snapshots have arrived than required. Access fields directly on the returned object:

```python
if not is_na(nfp.actual):
    label(bar_index, high, f"NFP {nfp.actual}", color=green)
```

`prev()` requires at least two snapshots to have arrived; it returns `na` until the second release.

### Dataset: `"econ"` — Economic Calendar

Covers major macro releases (NFP, CPI, FOMC, PMI, GDP, PPI, retail sales, etc.) with actual, forecast, and previous values.

**Keys:** the indicator nickname, e.g. `"NFP"`, `"CPI"`, `"CORE_CPI"`, `"FOMC_RATE"`, `"ISM_MFG"`, `"GDP"`, `"PCE"`, `"RETAIL_SALES"`, `"PPI"`, `"IJC"`, `"JOLTS"`, `"UMICH"`.

| Field | Type | Description |
|-------|------|-------------|
| `actual` | float \| na | The released value |
| `forecast` | float \| na | Median analyst consensus before release |
| `previous` | float \| na | Prior period's actual |
| `surprise` | float \| na | `actual - forecast` (na if either is missing) |
| `change` | float \| na | `actual - previous` (na if either is missing) |
| `severity` | int | 1 = low, 2 = medium, 3 = high impact |

```python
strategy("NFP momentum")
use "econ" as eco

nfp = eco.latest("NFP")
if not is_na(nfp.actual):
    col = green if nfp.surprise > 0 else red
    shape(circle, bar_index, close, color=col, size="small")
```

### Dataset: `"earnings"` — Quarterly Earnings

SEC income-statement filings for publicly traded companies. Returns the most recent **quarterly** report (Q1-Q4) for the symbol. Annual (FY) rows are excluded to avoid same-day timestamp collisions between the quarterly and annual filings.

**Keys:** ticker symbol, e.g. `"NVDA"`, `"AAPL"`, `"MSFT"`, `"XOM"`.

| Field | Type | Description |
|-------|------|-------------|
| `eps` | float \| na | Basic EPS (quarterly) |
| `eps_diluted` | float \| na | Diluted EPS |
| `revenue` | float \| na | Total revenue |
| `net_income` | float \| na | Net income |
| `ebitda` | float \| na | EBITDA |
| `gross_margin` | float \| na | Gross profit / revenue (0.0 - 1.0) |
| `operating_margin` | float \| na | Operating income / revenue |
| `net_margin` | float \| na | Net income / revenue |
| `rd_expense` | float \| na | R&D expense |
| `sga_expense` | float \| na | SG&A expense |
| `period` | string \| na | Filing period: `"Q1"`, `"Q2"`, `"Q3"`, `"Q4"` |

```python
strategy("NVDA earnings filter")
use "earnings" as er

nvda = er.latest("NVDA")
if not is_na(nvda.eps) and nvda.eps > nvda[1].eps:
    shape(arrow_up, bar_index, low, color=green, text="EPS beat")
```

### Dataset: `"cot"` — CFTC Commitment of Traders

Weekly CFTC COT report, released every Friday at 15:30 ET. Covers futures and options combined for major commodities, currencies, and financials.

**Keys:** LSE symbol format, e.g. `"XAU/USD"`, `"GBP/USD"`, `"EUR/USD"`, `"CRUDE/USD"`, `"COFFEE/USD"`, `"COPPER/USD"`, `"US10Y"`.

| Field | Type | Description |
|-------|------|-------------|
| `net_noncomm` | float \| na | Non-commercial net (longs minus shorts) |
| `net_comm` | float \| na | Commercial net (longs minus shorts) |
| `noncomm_long` | float \| na | Non-commercial long contracts |
| `noncomm_short` | float \| na | Non-commercial short contracts |
| `pct_noncomm_long` | float \| na | % of open interest held long by non-commercials |
| `pct_noncomm_short` | float \| na | % of open interest held short by non-commercials |
| `open_interest` | float \| na | Total open interest |
| `change_noncomm_long` | float \| na | Week-on-week change in non-commercial longs |
| `change_noncomm_short` | float \| na | Week-on-week change in non-commercial shorts |

```python
strategy("Gold COT positioning")
use "cot" as cot_data

cur = cot_data.latest("XAU/USD")
prv = cot_data.prev("XAU/USD")
if not is_na(cur.net_noncomm) and not is_na(prv.net_noncomm):
    if cur.net_noncomm > prv.net_noncomm:
        bgcolor(rgba(0, 255, 0, 0.05))
    elif cur.net_noncomm < prv.net_noncomm:
        bgcolor(rgba(255, 0, 0, 0.05))
```

### Dataset: `"insider"` — SEC Insider and Senate Trades

SEC Form 4 insider transactions plus STOCK Act congressional disclosures.

**Keys:** ticker symbol, e.g. `"NVDA"`, `"AAPL"`, `"TSLA"`.

> **Performance note:** The insider table contains millions of rows. Only symbols explicitly referenced in `.latest("SYMBOL")` or `.prev("SYMBOL")` calls are fetched — static analysis scans the script source before execution.

| Field | Type | Description |
|-------|------|-------------|
| `direction` | string \| na | `"buy"` or `"sell"` |
| `txn_type` | string \| na | Transaction type code (e.g. `"P"` = purchase, `"S"` = sale) |
| `shares` | float \| na | Number of shares traded |
| `price_per_share` | float \| na | Transaction price |
| `total_value` | float \| na | `shares * price_per_share` |

```python
strategy("Insider buy signal")
use "insider" as ins

nvda_trade = ins.latest("NVDA")
if not is_na(nvda_trade.direction) and nvda_trade.direction == "buy":
    shape(circle, bar_index, low, color=blue, size="small", text="INS")
```

### Complete multi-dataset example

```python
strategy("Macro + COT Gold filter",
         overlay=true, capital=10000, default_qty=1)

use "XAU/USD" as gold
use "econ" as eco
use "cot" as cot_data

nfp       = eco.latest("NFP")
gold_cot  = cot_data.latest("XAU/USD")
gold_prev = cot_data.prev("XAU/USD")

# COT positioning improving AND last NFP was negative (risk-off)
cot_rising = not is_na(gold_cot.net_noncomm) and not is_na(gold_prev.net_noncomm) and
             gold_cot.net_noncomm > gold_prev.net_noncomm
nfp_weak   = not is_na(nfp.actual) and nfp.actual < 0

if cot_rising and nfp_weak:
    entry("long_gold", long)
    shape(arrow_up, bar_index, low, color=gold, size="large", text="MACRO LONG")

if position.size > 0 and cot_rising == false:
    exit("cot_exit", from_entry="long_gold")
```

---

## Types and Enums

User-defined struct types group related fields:

```python
type Zone:
    top: float = 0.0
    bottom: float = 0.0
    bars_held: int = 0

z = Zone.new()
z.top = high
z.bottom = low
```

Enums prevent typo-prone string compares:

```python
enum Signal:
    buy
    sell
    neutral

s = Signal.buy
if s == Signal.buy:
    shape(arrow_up, location=below_bar, color=green)
```

---

## Strategy Execution

A `strategy()` declaration always activates the engine. Calling these only does anything inside a strategy script (in practice, every script, there is no other declaration mode).

### entry(id, direction, qty=N)

Open a new position. `direction` is the bare identifier `long` or `short`, not a string.

```python
entry("breakout", long, qty=1)
entry("fade",     short, qty=2)
entry("flat_buy", long)              # uses default_qty from the declaration
```

Behaviour:
- Reaching `pyramiding` cap silently skips additional same-direction entries.
- Entering opposite direction auto-closes the open position (reversal).
- Slippage applied: long fills higher, short fills lower.
- Commission charged on entry.

### exit(id, from_entry=, qty=, stop=, limit=, profit=, loss=)

Close a position. Must be called every bar for `stop` / `limit` to be checked.

```python
# Market exit immediately
exit("flatten", from_entry="breakout")

# Stop loss at price 95
exit("sl", from_entry="breakout", stop=95)

# Take profit at 110
exit("tp", from_entry="breakout", limit=110)

# SL/TP relative to entry price (in price points)
exit("bracket", from_entry="breakout", profit=10, loss=5)

# Partial close
exit("scale", from_entry="breakout", qty=1)
```

Stop and limit are checked against the current bar's high / low. If both trigger on the same bar, stop is assumed to fill first (conservative).

**CRITICAL pattern — stop/limit exit must live in its own block, not inside the entry block.**
The entry fires once; the exit must run every bar so the engine can check each bar's high/low.
Use `position.entry_price` (not the entry bar's `close`) for stop/limit math.

```python
# CORRECT — entry and exit in separate blocks
strategy("Bracketed long")
atr_val = atr(14)

if rsi(close, 14) < 30 and position.size == 0:
    entry("long", long)

if position.size > 0:
    exit("bracket", from_entry="long",
         stop=position.entry_price - atr_val,
         limit=position.entry_price + 2 * atr_val)

# WRONG — exit inside the entry block only runs on the signal bar, then never again
# if rsi(close, 14) < 30 and position.size == 0:
#     entry("long", long)
#     exit("bracket", ...)   ← this fires once and is forgotten
```

For both long and short with symmetric stops:

```python
strategy("Long/Short Bracketed")
fast = ema(close, 9)
slow = ema(close, 21)
atr_val = atr(14)

if crossover(fast, slow) and position.size == 0:
    entry("long", long)

if crossunder(fast, slow) and position.size == 0:
    entry("short", short)

if position.size > 0:
    exit("long_bracket", from_entry="long",
         stop=position.entry_price - atr_val,
         limit=position.entry_price + 2 * atr_val)

if position.size < 0:
    exit("short_bracket", from_entry="short",
         stop=position.entry_price + atr_val,
         limit=position.entry_price - 2 * atr_val)
```

### close_all()

Flatten everything at the current close. Runs at the end of the backtest automatically.

```python
if rsi(close, 14) > 80:
    close_all()
```

### position namespace (read-only)

| Field | Description |
|-------|-------------|
| `position.size` | Net size: positive long, negative short, 0 flat |
| `position.avg_price` | Weighted average entry price |
| `position.unrealized_pnl` | Mark-to-market P&L on the open position |
| `position.entry_bar` | Bar index of the first entry |
| `position.entry_time` | Unix-ms time of the first entry |
| `position.entry_price` | Price of the first entry |

### strategy namespace (read-only)

| Field | Description |
|-------|-------------|
| `strategy.equity` | Capital + realized + unrealized |
| `strategy.initial_capital` | From the `strategy(...)` declaration |
| `strategy.net_profit` | Total realized P&L |
| `strategy.gross_profit` | Sum of winning trade P&L |
| `strategy.gross_loss` | Sum of losing trade P&L (negative) |
| `strategy.open_trades` | Count of open positions |
| `strategy.closed_trades` | Count of completed trades |
| `strategy.win_rate` | Win % (0-100) |
| `strategy.max_drawdown` | Peak-to-trough equity decline |
| `strategy.commission` | Total commission paid |

### barstate namespace (read-only)

`last_bar`, `is_first_bar`, etc. are also exposed via this namespace for Pine compatibility:

| Field | Equivalent |
|-------|-----------|
| `barstate.islast` | same as `last_bar` |
| `barstate.isfirst` | same as `is_first_bar` |
| `barstate.isconfirmed` | always `true` (offline mode) |
| `barstate.ishistory` | always `true` (offline mode) |
| `barstate.isnew` | always `true` (offline mode) |
| `barstate.isrealtime` | `false` on history; `true` on the final bar in live mode |

---

## Live Trading Verbs

Bare verbs send real orders to the user's paper account. They are **only fired on the final bar** of a run, so historical bars are silently no-op. This lets you replay a script over years of history without leaking fills.

| Verb | Purpose |
|------|---------|
| `buy(qty=, stop_loss=, take_profit=)` | Market long |
| `sell(qty=, stop_loss=, take_profit=)` | Market short |
| `buy_limit(price, qty=, ...)` | Long limit order |
| `sell_limit(price, qty=, ...)` | Short limit order |
| `buy_stop(price, qty=, ...)` | Long stop order |
| `sell_stop(price, qty=, ...)` | Short stop order |
| `close(symbol=)` | Close the open position (current symbol if omitted) |
| `close_all(symbol=)` | Close everything (live mode, when used outside a strategy backtest) |

```python
strategy("RSI fade live")
rsi_val = rsi(close, 14)

if rsi_val > 80 and close < close[1]:
    sell(qty=1, stop_loss=high + 2, take_profit=close - 5)

if rsi_val < 20 and close > close[1]:
    buy(qty=1, stop_loss=low - 2, take_profit=close + 5)
```

The host posts these to the paper-trading API. On historical bars they are skipped silently.

---

## User Inputs

`input()` and its 6 typed variants register settings widgets in the chart's settings dialog. The user picks values per placement; the script source is never edited.

### input(default, "Title", min=, max=, step=, options=)

The general-purpose input. The widget rendered depends on the type of `default`:

```python
length     = input(14,    "RSI length", min=2, max=200, step=1)
threshold  = input(70.5,  "Threshold")
flag       = input(true,  "Use trend filter")
fast_color = input("green", "Fast colour")
mode       = input("ema", "Smoothing", options=["sma", "ema", "wma"])
```

Returns the user-overridden value, or the default if the user has not changed it.

### Typed input variants (snake_case, NOT dot syntax)

The functions are `input_timeframe`, `input_symbol`, etc. There is no `input.timeframe(...)` namespace.

```python
tf  = input_timeframe("1h",        "Higher TF")     # timeframe dropdown
sym = input_symbol(   "BTCUSD",    "Compare")       # symbol picker
ses = input_session(  "0930-1600", "Session")       # session window
px  = input_price(    100.50,      "Stop level")    # interactive price level
txt = input_text_area("",          "Strategy notes")# multi-line textarea
t   = input_time(     "09:30",     "Open at")       # HH:MM time picker
```

Wrap every tunable value in `input()` (or a typed variant). A well-written script has a rich settings dialog; a lazy script has none.

---

## Drawing Primitives

These are the chart-drawing calls. None of them register as registry builtins; they are dispatched directly by the runtime as render commands.

### shape(type, location=, color=, size=, text=, text_color=)

Discrete marker on the current bar.

| Param | Default | Notes |
|-------|---------|-------|
| `type` | required | Identifier (not string): `circle`, `diamond`, `square`, `triangle_up`, `triangle_down`, `arrow_up`, `arrow_down`, `cross`, `plus`, `flag`, `star` |
| `location` | `above_bar` | Identifier: `above_bar`, `below_bar`, `at_bar`, `top`, `bottom` |
| `color` | `green` | Any color value |
| `size` | `"normal"` | `"tiny"`, `"small"`, `"normal"`, `"large"`, `"huge"` |
| `text` | `""` | Optional label inside / near shape |
| `text_color` | `white` | Text color |

> **Important:** shape types and locations are **identifiers**, not strings. Write `arrow_up` not `"arrow_up"`, write `below_bar` not `"below_bar"`.

```python
shape(arrow_up, location=below_bar, color=green, size="large", text="BUY")
```

### label(text, y=, location=, color=, bg=, size=, tooltip=, align=)

Free text. Either set `y=` to anchor at a specific price level, or use `location=` for relative placement.

```python
label("Resistance", y=high, color=red, bg=rgba(255, 0, 0, 0.2))
label("BUY", location=below_bar, color=green)
```

### bgcolor(color, panel=)

Tint the current bar's column background.

```python
if rsi(close, 14) > 70:
    bgcolor(rgba(255, 0, 0, 0.1))
```

### hline(price, color=, style=, width=, title=, panel=)

Horizontal line at a fixed price level.

```python
hline(70, color=red,   style="dashed")
hline(30, color=green, style="dashed")
hline(50, color=gray,  style="dotted", width=1, title="Mid")
```

`style` is one of `"solid"`, `"dashed"`, `"dotted"`. `panel` is `"overlay"` or `"below"`.

### table(position=, rows=, cols=, bg=) and t.cell(...)

Fixed-position dashboard table. Wrap creation in `if last_bar:` so it only renders once.

```python
if last_bar:
    t = table(position="top_right", rows=4, cols=2, bg=rgba(0, 0, 0, 0.85))
    t.cell(0, 0, "Metric",   color=gray)
    t.cell(0, 1, "Value",    color=gray)
    t.cell(1, 0, "RSI(14)",  color=white)
    t.cell(1, 1, f"{rsi(close, 14):.1f}", color=red if rsi(close, 14) > 70 else white)
    t.cell(2, 0, "ATR(14)",  color=white)
    t.cell(2, 1, f"{atr(14):.2f}", color=white)
```

Positions: `"top_left"`, `"top_center"`, `"top_right"`, `"middle_left"`, `"middle_center"`, `"middle_right"`, `"bottom_left"`, `"bottom_center"`, `"bottom_right"`.

`t.cell(row, col, text, color=, bg=, align=, size=)` populates a cell. `align` is `"left"`, `"center"`, or `"right"`.

---

## Plotting Indicators

### plot(call_or_var)

Explicit opt-in to register a chart-native indicator. Mirrors Pine Script's `plot()` semantics: bare `ema(close, 20)` is a value used in your script's logic, `plot(ema(close, 20))` is what makes the EMA line appear on the chart.

The runtime evaluates `plot()` as a no-op pass-through (returns its argument unchanged). The visual effect comes from a static AST scan at script Run time: any `plot(...)` wrapping a recognised indicator call gets registered with the chart host's indicator panel, where the user can recolour, resize, hide, or delete the line via the standard cog.

**What `plot()` recognises (constant-period args required):**

| Wrapped call | Registers |
|--------------|-----------|
| `plot(ema(close, N))`  / `plot(sma(close, N))`  / `plot(smma(close, N))` | Moving average line on the price overlay |
| `plot(rsi(close, N))` | RSI subplot |
| `plot(macd(close, F, S, Sig))` | MACD subplot (line + signal + histogram) |
| `plot(bb(close, N, K))` | Bollinger Bands on the price overlay |
| `plot(stoch(high, low, close, K, D, Sm))` | Stochastic subplot |
| `plot(atr(N))` | ATR subplot |
| `plot(vwap())` | VWAP on the price overlay |
| `plot(adx(N))` | ADX subplot |
| `plot(cci(close, N))` | CCI subplot |
| `plot(roc(close, N))` | ROC subplot |

`plot()` also traces back through top-level assignments. These are equivalent:

```python
# Direct
plot(ema(close, 20))

# Via assignment - the extractor resolves `fast` to its registerable call
fast = ema(close, 20)
plot(fast)
```

**What `plot()` does NOT do:**

- `plot(close * 2)`, `plot(my_correlation_value)`, `plot(some_helper_function(close))` — anything that isn't a direct registerable call (or a variable holding one) is silently ignored. Brue does not render arbitrary continuous series; for those, use the chart host's built-in indicator menu directly.
- `plot(ema(close, len))` where `len` is dynamic (changes during the script) — only constant-arg periods register. `len = input(20, "Length")` and `len = 20` are both fine because the extractor resolves them; `len` reassigned inside an `if` is not.
- Anything visual on the chart canvas itself — `plot()` does not paint pixels. It hands the request to the chart host's indicator system and the host's renderer takes over.

**Why this design:**

The previous version of Brue auto-promoted every recognised indicator call to a chart-native indicator regardless of how the result was used. That was friendly for `ema(close, 20)` written on a line by itself, but it spawned mystery subplots whenever someone used the same calls as intermediates — `correlation(roc(close, 1), roc(other.close, 1), 50)` would silently add a ROC subplot the user never asked for. Pine semantics resolve the ambiguity: bare calls are values, `plot()` is the explicit "I want this on the chart" opt-in.

```python
strategy("EMA Crossover", overlay=true)

fast = ema(close, 9)    # value, used in logic below
slow = ema(close, 21)   # value, used in logic below

plot(fast)              # explicitly: show this line on the chart
plot(slow)              # explicitly: show this line on the chart

if crossover(fast, slow):
    shape(arrow_up, location=below_bar, color=green, size="large", text="BUY")
if crossunder(fast, slow):
    shape(arrow_down, location=above_bar, color=red, size="large", text="SELL")
```

---

## Colors

### Named (21)

`red`, `green`, `blue`, `yellow`, `orange`, `purple`, `pink`, `cyan`, `white`, `black`, `gray`, `lime`, `teal`, `navy`, `maroon`, `olive`, `aqua`, `fuchsia`, `silver`, `gold`, `transparent`.

### Constructors

```python
rgb(255, 87, 51)                # opaque
rgba(255, 87, 51, 0.5)          # 50% alpha
with_alpha(red, 0.3)            # take a named color, set alpha
"#FF5733"                       # hex literal
"#FF573380"                     # hex with alpha (last 2 chars)
```

### Dynamic colors

```python
bar_color = green if close > open else red
plot_color = green if rsi(close, 14) < 30 else red if rsi(close, 14) > 70 else gray
```

---

## Math and Logic

24 bar-level functions: `abs`, `max`, `min`, `round`, `floor`, `ceil`, `sqrt`, `pow`, `log`, `log10`, `exp`, `sign`, `highest`, `lowest`, `sum`, `crossover`, `crossunder`, `cross`, `rising`, `falling`, `change`, `barssince`, `is_na`, `coalesce`.

```python
abs(-3.5)                  # 3.5
max(close, open)           # bar-by-bar max
min(low, sma(low, 20))
round(rsi(close, 14), 1)
sqrt(stddev(close, 20))
pow(2, 10)                 # 1024
log(close)
sign(close - close[1])     # 1, 0, or -1

highest(high, 20)          # rolling max over last 20 bars
lowest(low, 20)            # rolling min over last 20 bars
sum(volume, 50)            # rolling sum

crossover(fast, slow)      # true on the bar fast crosses ABOVE slow
crossunder(fast, slow)     # true on the bar fast crosses BELOW slow
cross(a, b)                # true on any cross (either direction)
rising(close, 3)           # true if close has risen for 3 bars
falling(close, 3)
change(close, 1)           # close - close[1]; default lookback=1
barssince(close > open)    # bars since condition was last true; na if never

is_na(x)
coalesce(x, fallback)      # returns fallback if x is na
```

---

## Time Functions

7 time getters that work on a millisecond timestamp (typically `time`):

```python
year(time)         # 2026
month(time)        # 1-12
dayofmonth(time)   # 1-31
dayofweek(time)    # 0=Sun, 1=Mon, ... 6=Sat
hour(time)         # 0-23 UTC
minute(time)       # 0-59
second(time)       # 0-59
```

### in_session()

Returns `true` if the current bar falls inside the given trading session. Handles DST automatically via IANA timezones.

**Named sessions** (DST-aware, Mon-Fri only):

```python
if in_session("london"):    # 08:00-16:30 Europe/London
if in_session("new_york"):  # 09:30-16:00 America/New_York
if in_session("tokyo"):     # 09:00-15:30 Asia/Tokyo
if in_session("sydney"):    # 10:00-16:00 Australia/Sydney
if in_session("frankfurt"): # 09:00-17:30 Europe/Berlin
```

**Custom sessions** — `"HH:MM-HH:MM"` or `"HHMM-HHMM"` with a `tz=` IANA timezone kwarg. Runs all 7 days unless you add your own day filter.

```python
# All bars in the given window, any day
if in_session("08:00-17:00", tz="Europe/London"):
    entry("buy", long)

# Overnight session (crosses midnight) — works correctly
if in_session("22:00-05:00", tz="America/New_York"):
    bgcolor(rgba(255, 200, 0, 0.05))

# Pair with input_session() for a user-configurable window
ses = input_session("0930-1600", "Session filter")
if in_session(ses, tz="America/New_York"):
    entry("buy", long)

# Combine named session with a day filter
if in_session("london") and dayofweek(time) != 5:  # Mon-Thu only
    entry("buy", long)
```

All time getters return UTC values. Use `in_session()` whenever you need local-clock session logic.

---

## Moving Averages

15 functions. All return a single float per bar.

| Function | Signature |
|----------|-----------|
| `sma(source, length)` | Simple |
| `ema(source, length)` | Exponential |
| `wma(source, length)` | Weighted |
| `hma(source, length)` | Hull (faster, less lag) |
| `dema(source, length)` | Double EMA |
| `tema(source, length)` | Triple EMA |
| `smma(source, length)` | Smoothed (Wilder's) |
| `vwma(source, length)` | Volume-weighted |
| `vwap(source=hlc3)` | Volume-weighted average price (session) |
| `alma(source, length, offset=0.85, sigma=6)` | Arnaud Legoux, Gaussian-weighted |
| `kama(source, length)` | Kaufman Adaptive |
| `zlema(source, length)` | Zero-Lag EMA |
| `t3(source, length, factor=0.7)` | Tillson T3 |
| `lsma(source, length)` | Least Squares (linreg over window) |
| `mcginley(source, length)` | McGinley Dynamic |

---

## Oscillators

31 functions. Several return tuples; destructure with multi-assign.

| Function | Returns |
|----------|---------|
| `rsi(source, length)` | float |
| `macd(source, fast, slow, signal)` | (macd_line, signal, histogram) |
| `stoch(source, high_src, low_src, length)` | float (%K) |
| `stoch_rsi(source, rsi_len, stoch_len, k_smooth, d_smooth)` | (k, d) |
| `cci(source, length)` | float |
| `mfi(length)` | float (volume-weighted RSI) |
| `williams_r(length)` | float |
| `roc(source, length)` | float (% change over N) |
| `mom(source, length)` | float (close − close[N]) |
| `ao(fast=5, slow=34)` | float (Awesome Oscillator) |
| `tsi(source, long=25, short=13)` | float |
| `dpo(source, length=21)` | float |
| `coppock(source)` | float |
| `trix(source, length=15)` | float |
| `ultimate_osc(fast=7, mid=14, slow=28)` | float |
| `kst(source)` | float |
| `fisher(length=10)` | (fisher, trigger) |
| `stc(...)` | float (Schaff Trend Cycle) |
| `rvi(length)` | float (Relative Vigor Index) |
| `connors_rsi(...)` | float |
| `cmo(source, length)` | float |
| `apo(source, fast, slow)` | float |
| `ppo(source, fast, slow, signal)` | float |
| `pvo(fast, slow, signal)` | float (volume PPO) |
| `qstick(length)` | float |
| `bop()` | float (Balance of Power) |
| `psych_line(length=20)` | float |
| `pfe(length=10)` | float (Polarized Fractal Efficiency) |
| `ulcer_index(length=14)` | float |
| `smi(length=10, smooth=3)` | float (Stochastic Momentum Index) |
| `rel_vol_index(length=14)` | float |

---

## Trend Indicators

15 functions.

| Function | Returns |
|----------|---------|
| `adx(length)` | (adx, +di, −di) |
| `supertrend(length=10, mult=3.0)` | (supertrend, direction) |
| `sar(start=0.02, inc=0.02, max=0.2)` | float (Parabolic SAR) |
| `ichimoku(tenkan, kijun, senkou)` | (tenkan, kijun, senkou_a, senkou_b, chikou) |
| `aroon(length=14)` | (up, down) |
| `alligator()` | (jaw, teeth, lips) |
| `gator()` | (upper, lower) |
| `vortex(length=14)` | (vi+, vi−) |
| `choppiness(length=14)` | float |
| `elder_ray(length=13)` | (bull_power, bear_power) |
| `mass_index(length=25)` | float |
| `chande_kroll(...)` | (long_stop, short_stop) |
| `zigzag(...)` | float (smoothed zigzag) |
| `fractals()` | (up_fractal, down_fractal) |
| `vhf(length=28)` | float (Vertical Horizontal Filter) |

---

## Volatility

16 functions.

| Function | Returns |
|----------|---------|
| `bb(source, length=20, mult=2.0)` | (upper, mid, lower) |
| `bb_percent(source, length, mult)` | float (%B) |
| `bb_width(source, length, mult)` | float |
| `keltner(length=20, mult=2.0)` | (upper, mid, lower) |
| `donchian(length=20)` | (upper, mid, lower) |
| `stddev(source, length)` | float |
| `hist_vol(length, periods=252)` | float (annualized) |
| `chaikin_vol(length=10)` | float |
| `tr()` | float (True Range) |
| `natr(length=14)` | float (normalized ATR) |
| `atr(length=14)` | float |
| `squeeze()` | (momentum, is_squeeze) |
| `chandelier(length=22, mult=3.0)` | (long_stop, short_stop) |
| `envelopes(source, length, percent)` | (upper, lower) |
| `acc_bands(length=20)` | (upper, mid, lower) |
| `price_channel(length=20)` | (upper, lower) |

---

## Volume Indicators

14 functions.

| Function | Returns |
|----------|---------|
| `obv()` | float (On-Balance Volume) |
| `cmf(length=20)` | float (Chaikin Money Flow) |
| `adl()` | float (Accumulation / Distribution Line) |
| `force_index(length=13)` | float |
| `eom(length=14, divisor=10000)` | float (Ease of Movement) |
| `vol_sma(length=20)` | float |
| `nvi()` | float (Negative Volume Index) |
| `pvi()` | float (Positive Volume Index) |
| `pvt()` | float (Price Volume Trend) |
| `vroc(length=14)` | float (Volume RoC) |
| `net_volume()` | float |
| `twiggs_mf(length=21)` | float (Twiggs Money Flow) |
| `vol_osc(short=5, long=10)` | float |
| `klinger(...)` | float (Klinger Volume Oscillator) |

---

## Statistics and Regression

8 functions.

| Function | Returns |
|----------|---------|
| `correlation(source1, source2, length)` | float [−1, 1] |
| `linreg(source, length, offset=0)` | float (regression value) |
| `linreg_slope(source, length)` | float |
| `r_squared(source, length)` | float [0, 1] |
| `zscore(source, length)` | float |
| `beta(source, market, length)` | float |
| `spread(source1, source2)` | float (= source1 − source2) |
| `ratio(source1, source2)` | float (= source1 / source2) |

---

## Removed and Unsupported

These names appear in older docs, Pine Script, or AI training data. They do not exist in the current Brue runtime.

| Name | Status | Use instead |
|------|--------|-------------|
| `indicator()` declaration | Removed | `strategy(...)` is the only declaration |
| `plot(arbitrary_expression)` | No-op | `plot()` only registers chart-native indicators (`plot(ema(close, 20))` etc). For arbitrary series, add an indicator from the chart host's built-in indicator menu. See [Plotting Indicators](#plotting-indicators). |
| `request(tf, expr)` | Not implemented | `use SYMBOL at TF as alias` then read `alias.close`, etc. |
| `request_security(symbol, tf, expr)` | Not implemented | `use SYMBOL [at TF] as alias` |
| `request_financial(...)` | Not implemented |, |
| `request_economic(...)` | Not implemented |, |
| `alert(msg, freq)` | Not implemented | Mark the bar with `shape` / `label`; user gets a visual notification |
| `alertcondition(cond, ...)` | Not implemented |, |
| `line_new`, `line_set_*`, `line_get_*`, `line_delete` | Not implemented | Combine `hline` + `shape` for similar effects |
| `box_new`, `box_set_*`, `box_get_*`, `box_delete` | Not implemented |, |
| `polyline_new`, `polyline_delete` | Not implemented |, |
| `barcolor()` | Not implemented | `bgcolor()` is the available substitute |
| `plotcandle()`, `plotarrow()` | Not implemented |, |
| `persist_tick` keyword | Not implemented | `persist` is the only persistence keyword |
| `input.timeframe(...)`, `input.symbol(...)` (dot syntax) | Not implemented | `input_timeframe(...)`, `input_symbol(...)` (underscore) |
| `var`, `varip` | Not implemented | `persist`, `const` |

---

## Complete Working Examples

### 1. EMA crossover signals (with both EMAs visible)

```python
strategy("EMA Crossover", overlay=true)

fast_len = input(9,  "Fast EMA length")
slow_len = input(21, "Slow EMA length")

fast = ema(close, fast_len)
slow = ema(close, slow_len)

# Explicit opt-in: show both EMA lines on the chart. Drop the plot()
# calls if you only want the BUY/SELL arrows.
plot(fast)
plot(slow)

if crossover(fast, slow):
    shape(arrow_up,   location=below_bar, color=green, size="large", text="BUY")
if crossunder(fast, slow):
    shape(arrow_down, location=above_bar, color=red,   size="large", text="SELL")
```

### 2. RSI extremes with bgcolor heatmap

```python
strategy("RSI Heatmap", overlay=true)

rsi_val = rsi(close, 14)

if rsi_val > 70: bgcolor(rgba(255,   0,   0, 0.10))
if rsi_val < 30: bgcolor(rgba(  0, 255,   0, 0.10))

if rsi_val > 70:
    shape(circle, location=above_bar, color=red,   size="small")
if rsi_val < 30:
    shape(circle, location=below_bar, color=green, size="small")
```

### 3. Bollinger band breaks (with bands visible)

```python
strategy("BB Breaks", overlay=true)

length = input(20,  "BB length", min=5, max=200)
mult   = input(2.0, "BB std-dev multiplier")

# plot(bb(...)) registers the Bollinger Bands as a chart-native overlay.
plot(bb(close, length, mult))

mid       = sma(close, length)
deviation = stddev(close, length) * mult
upper     = mid + deviation
lower     = mid - deviation

if close > upper:
    shape(triangle_down, location=above_bar, color=red,   size="small", text="UP")
if close < lower:
    shape(triangle_up,   location=below_bar, color=green, size="small", text="DN")
```

### 4. EMA crossover backtest with ATR-based bracket exit

```python
strategy("EMA Cross + ATR bracket",
         overlay        = true,
         capital        = 10000,
         commission     = 0.0001,
         commission_type= "percent",
         default_qty    = 1,
         pyramiding     = 0)

fast_len   = input(9,   "Fast EMA")
slow_len   = input(21,  "Slow EMA")
atr_len    = input(14,  "ATR length")
atr_stop   = input(2.0, "Stop = ATR x")
rr         = input(2.0, "Reward:risk")

fast    = ema(close, fast_len)
slow    = ema(close, slow_len)
atr_pts = atr(atr_len)

# Show the EMAs on the chart so the user can eyeball the crossovers.
# atr_pts is intentionally NOT plotted - it's only used for stop sizing.
plot(fast)
plot(slow)

go_long = crossover(fast, slow)
go_flat = crossunder(fast, slow)

if go_long and position.size == 0:
    entry("long", long, qty=1)
    stop_dist   = atr_pts * atr_stop
    profit_dist = stop_dist * rr
    exit("bracket",
         from_entry = "long",
         stop       = close - stop_dist,
         limit      = close + profit_dist)
    shape(arrow_up, location=below_bar, color=green, size="small")
    label("BUY", color=green)

if position.size > 0 and go_flat:
    exit("flip", from_entry="long")
    shape(arrow_down, location=above_bar, color=red, size="small")
```

### 5. Cross-pair pairs trade with z-score entry

```python
strategy("EUR vs GBP pairs", overlay=true,
         capital=10000, commission=0.0001, commission_type="percent",
         default_qty=1)

use "GBP/USD" as gbp

window     = input(60,  "Rolling window")
corr_floor = input(0.5, "Min |corr| to trade")
entry_z    = input(2.0, "Entry |zscore|")
exit_z     = input(0.3, "Exit |zscore|")

corr = correlation(close, gbp.close, window)
z    = zscore(close - gbp.close, window)

active = corr > corr_floor

if active and z < -entry_z and position.size == 0:
    entry("long_eur", long, qty=1)
    shape(arrow_up,   location=below_bar, color=green, size="small")
if active and z > entry_z and position.size == 0:
    entry("short_eur", short, qty=1)
    shape(arrow_down, location=above_bar, color=red,   size="small")
if position.size > 0 and z > -exit_z:
    exit("close_long",  from_entry="long_eur")
if position.size < 0 and z < exit_z:
    exit("close_short", from_entry="short_eur")
```

### 6. Live RSI fade (bare verbs, fires only on the latest bar)

```python
strategy("Live RSI fade")

rsi_val = rsi(close, 14)
atr_pts = atr(14)

if rsi_val > 80 and close < close[1]:
    sell(qty=1, stop_loss=high + atr_pts, take_profit=close - atr_pts * 2)

if rsi_val < 20 and close > close[1]:
    buy(qty=1, stop_loss=low - atr_pts, take_profit=close + atr_pts * 2)
```

### 7. Last-bar dashboard table

```python
strategy("Dashboard", overlay=true)

rsi_val          = rsi(close, 14)
adx_val, _, _    = adx(14)
atr_val          = atr(14)

if last_bar:
    t = table(position="top_right", rows=4, cols=2, bg=rgba(0, 0, 0, 0.85))
    t.cell(0, 0, "Metric", color=gray)
    t.cell(0, 1, "Value",  color=gray)

    rsi_color = red if rsi_val > 70 else green if rsi_val < 30 else white
    t.cell(1, 0, "RSI(14)", color=white)
    t.cell(1, 1, f"{rsi_val:.1f}", color=rsi_color)
    t.cell(2, 0, "ADX(14)", color=white)
    t.cell(2, 1, f"{adx_val:.1f}", color=green if adx_val > 25 else gray)
    t.cell(3, 0, "ATR(14)", color=white)
    t.cell(3, 1, f"{atr_val:.4f}", color=white)
```

### 8. NFP + COT combined macro strategy

```python
strategy("NFP COT Gold",
         overlay=true, capital=10000, commission=0.0001, default_qty=1)

use "econ" as eco
use "cot" as cot_data

nfp_thresh = input(-50, "NFP threshold (entry when below)")

nfp      = eco.latest("NFP")
cur_cot  = cot_data.latest("XAU/USD")
prev_cot = cot_data.prev("XAU/USD")

cot_rising = not is_na(cur_cot.net_noncomm) and not is_na(prev_cot.net_noncomm) and
             cur_cot.net_noncomm > prev_cot.net_noncomm
nfp_weak   = not is_na(nfp.actual) and nfp.actual < nfp_thresh

if cot_rising and nfp_weak and position.size == 0:
    entry("long", long)
    shape(arrow_up, bar_index, low, color=gold, size="large")

if position.size > 0 and not cot_rising:
    exit("cot_exit", from_entry="long")
    shape(arrow_down, bar_index, high, color=gray, size="small")
```

### 9. Earnings beat scanner

```python
strategy("Earnings beat", overlay=true)

ticker = input("NVDA", "Symbol")

snap      = er.latest(ticker)
snap_prev = er.prev(ticker)

use "earnings" as er

beat = not is_na(snap.eps) and not is_na(snap_prev.eps) and snap.eps > snap_prev.eps
miss = not is_na(snap.eps) and not is_na(snap_prev.eps) and snap.eps < snap_prev.eps

if beat:
    shape(arrow_up,   bar_index, low,  color=green, size="small", text="BEAT")
if miss:
    shape(arrow_down, bar_index, high, color=red,   size="small", text="MISS")
```
