# Brue Language Reference

Brue is the scripting language for London Strategic Edge. It is Python-like, indentation-based, and runs bar-by-bar on candlestick chart data inside an isolated WebWorker.

> **For AI models:** Read this entire document before generating Brue code. Every function, identifier, and example below is verified against the runtime. If a function is not listed here, it does not exist. Do not invent functions from Pine Script, MQL4/5, or earlier Brue versions.

---

## What Brue does, and what it does NOT do

Brue is a **strategy + chart-drawing layer**. It is NOT the chart's indicator system.

A Brue script can:
- **Draw markers on the chart**: `shape`, `label`, `hline`, `bgcolor`, `table`
- **Run a backtest**: `entry`, `exit`, `close_all` plus the `position.*` and `strategy.*` namespaces
- **Send live orders to the user's paper account**: `buy`, `sell`, `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, `close`
- **Compute on price**: 143 built-in functions (moving averages, oscillators, trend, volatility, volume, statistics, math, time)
- **Pull a second instrument** with `use SYMBOL [at TF] [as ALIAS]` and reference it as `alias.close`, `alias.high`, etc.

A Brue script cannot:
- **Draw continuous indicator lines** (no `plot()`). For EMA / RSI / MACD lines on the chart, add them from the chart's built-in indicator menu.
- **Fetch data from arbitrary symbols / timeframes / fundamentals** at runtime. Use `use SYMBOL` for cross-pair; the engine pre-fetches that dataset for you.
- **Fire alerts** to a notification queue. Mark interesting bars with `shape()` instead.

If you came from Pine Script, Brue is closer to Pine's `strategy()` + drawing primitives than to Pine's `indicator()` + `plot()`. The line/box/polyline/alert APIs do not exist.

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
10. [Types and Enums](#types-and-enums)
11. [Strategy Execution](#strategy-execution)
12. [Live Trading Verbs](#live-trading-verbs)
13. [User Inputs](#user-inputs)
14. [Drawing Primitives](#drawing-primitives)
15. [Colors](#colors)
16. [Math and Logic](#math-and-logic)
17. [Time Functions](#time-functions)
18. [Moving Averages (15)](#moving-averages)
19. [Oscillators (31)](#oscillators)
20. [Trend Indicators (15)](#trend-indicators)
21. [Volatility (16)](#volatility)
22. [Volume Indicators (14)](#volume-indicators)
23. [Statistics and Regression (8)](#statistics-and-regression)
24. [Removed and Unsupported](#removed-and-unsupported)

---

## Script Structure

Every script begins with one declaration and only one declaration: `strategy(...)`. There is no `indicator()` keyword.

```python
strategy("My Script", overlay=true)
```

**`strategy()` parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| title | string (required, positional) |, | Name shown in the legend |
| `overlay` | bool | `true` | `true` draws on the price chart, `false` opens a separate panel |
| `capital` / `initial_capital` | number | `10000` | Backtest starting equity |
| `commission` | number | `0` | Commission rate |
| `commission_type` | string | `"percent"` | `"percent"` or `"fixed"` |
| `slippage` | number | `0` | Slippage in price units |
| `pyramiding` | number | `0` | Max additional same-direction entries |
| `default_qty` | number | `1` | Default order size |
| `currency` | string | `"USD"` | Account currency label |

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
strategy("EUR vs GBP")
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
hour(time)         # 0-23
minute(time)       # 0-59
second(time)       # 0-59
```

Example: London session is 08:00-16:00 UTC, Mon-Fri.

```python
in_london = hour(time) >= 8 and hour(time) < 16
weekday   = dayofweek(time) >= 1 and dayofweek(time) <= 5
if in_london and weekday:
    bgcolor(rgba(0, 100, 255, 0.04))
```

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
| `plot()` | Removed (throws RuntimeError) | Add the indicator from the chart's built-in indicator menu, or draw with `shape` / `label` / `hline` |
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

### 1. EMA crossover signals (drawings only)

```python
strategy("EMA Crossover", overlay=true)

fast_len = input(9,  "Fast EMA length")
slow_len = input(21, "Slow EMA length")

fast = ema(close, fast_len)
slow = ema(close, slow_len)

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

### 3. Bollinger band breaks (signals only, add the bands from the indicator menu)

```python
strategy("BB Breaks", overlay=true)

length = input(20,  "BB length", min=5, max=200)
mult   = input(2.0, "BB std-dev multiplier")

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
