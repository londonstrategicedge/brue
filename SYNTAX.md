# The Brue Language

Brue is the trade-entry language: a script writes buy/sell/close decisions
over bars, and the same file runs identically in a historical run and live
against a broker through Brue Connect. Brue is for writing trade entries and
connecting them to brokers; nothing else is Brue.

A Brue script is pure trading logic. There is no header, no configuration
block, and no ceremony: every fact in a script is a fact about the strategy
itself. What the script trades, at what quantity, from which data source, at
which timeframe: all of it is written where it acts, readable from the text
alone. Everything that belongs to a *run* (capital, costs, the date window)
or to a *host* (chart placement, display names) lives outside the script.

Two kinds of script exist, told apart by one observable fact:

- A **trading script** places orders. It is held to total strictness: every
  series names its market and timeframe, every order names its market and
  quantity, and nothing draws.
- An **indicator script** places no orders. The chart that hosts it is its
  context, so bare `close`/`open`/... refer to that chart's series and
  `plot()` and colors are legal.

## A complete strategy

```brue
if XAUUSD.h1.close > 3400:
    buy("EURUSD", 0.5, sl=EURUSD.h1.close * 0.995, tp=EURUSD.h1.close * 1.01)
if XAUUSD.h1.close < 3300:
    close("EURUSD")
```

Run it over history: `python -m brue run gold_filter.brue eurusd_h1.csv
--data XAUUSD-h1=xauusd_h1.csv --capital 100000` (or `brue.run_script_full`
from Python, section 21). Reading the file tells you everything: it watches
hourly gold, trades EURUSD in half-unit clips with a bracket, and exits when
gold falls back. Nothing is bound at deployment.

## A complete indicator

```brue
# title: EMA ribbon
# panel: price
plot(ema(close, 8), title="ema8")
plot(ema(close, 21), title="ema21")
plot(ema(close, 55), title="ema55")
```

No orders, so it is an indicator: bare series mean "the chart I am on", and
the leading comment pragmas are host metadata (the terminal reads `# title:`
and `# panel: price|below`; the language ignores comments).

## Table of contents

1. [Script structure](#1-script-structure)
2. [Series name their world: symbols, sources, timeframes](#2-series-name-their-world)
3. [Trading scripts and indicator scripts](#3-trading-scripts-and-indicator-scripts)
4. [Execution model](#4-execution-model)
5. [Trading](#5-trading)
6. [Position and account state](#6-position-and-account-state)
7. [Values and types](#7-values-and-types)
8. [Operators](#8-operators)
9. [Variables: persist and const](#9-variables-persist-and-const)
10. [Control flow](#10-control-flow)
11. [Functions](#11-functions)
12. [The history operator](#12-the-history-operator)
13. [What counts as a series source](#13-what-counts-as-a-series-source)
14. [log(): diagnostics](#14-log-diagnostics)
15. [User inputs](#15-user-inputs)
16. [Indicator scripts: drawing and colors](#16-indicator-scripts-drawing-and-colors)
17. [Event datasets and data tables](#17-event-datasets-and-data-tables)
18. [Sessions and time](#18-sessions-and-time)
19. [Built-in function reference](#19-built-in-function-reference)
20. [Errors: loud or quiet, never wrong](#20-errors-loud-or-quiet-never-wrong)
21. [Running scripts](#21-running-scripts)
22. [Known limits and open questions](#22-known-limits-and-open-questions)

## 1. Script structure

A script is a sequence of statements, one per line, starting at line one.
Blocks are introduced by a colon and indentation, Python style. Any
consistent indent works. Comments start with `#` and run to the end of the
line; `//` is a parse error.

There is no header of any kind. A script that begins with `strategy(...)`
is rejected with a message that explains the migration: the header's old
contents all had better homes (orders carry the market and quantity;
capital and costs are run parameters; display hints belong to the host).
`use` is likewise rejected: referencing a symbol *is* its declaration.

Reserved words: `strategy, input, persist, const, if, elif, else, for, in,
while, break, continue, def, return, true, false, na, range, export, type,
enum, use, at, as, and, or, not`. `strategy`, `use` and `at` are reserved
only so their appearance produces a teaching error instead of a confusing
one; `export` is reserved for a future module system.

## 2. Series name their world

Every series reference in a trading script is a chain that names the
symbol, optionally the source, and always the timeframe:

```brue
EURUSD.h1.close             # EURUSD, LSE naming (the default source), hourly
XAUUSD.mybroker.h4.close   # gold as the source "mybroker" prices it, 4-hour
```

- **Symbol**: the instrument, in LSE naming when no source is given.
- **Source** (optional): a named pricing source, e.g. a specific broker.
  Different sources are genuinely different series (their spreads, their
  volume), and the reference says whose world a condition lives in.
- **Timeframe** (required): a token from the family `m1 m5 m15 m30 m90 h1
  h2 h4 d1 ...` (`m`/`h`/`d` plus a positive number). An `ema(x, 12)` on
  m5 and on d1 are different strategies; the text must say which.
- **Field**: `open, high, low, close, volume, hl2, hlc3, ohlc4`.

**The feed key is the chain joined by hyphens.** `EURUSD.h1.close` reads
the feed `"EURUSD-h1"`; `XAUUSD.mybroker.h4.close` reads
`"XAUUSD-mybroker-h4"`. The hyphen form lives in strings and feed keys
only, because in an expression a bare hyphen is subtraction.

Feeds are honest about their cadence:

- A feed supplied under a plain name (`"XAUUSD"`, `--data XAUUSD=path`)
  gets its token derived from the actual median bar spacing and registers
  under the full key.
- A feed supplied under a token key (`"XAUUSD-h1"`) is validated: if the
  bars are actually spaced differently, the run refuses with the measured
  token. Feeding 1-minute bars under `-h1` is a loud lie, not a subtle bug.

Misses are always loud. A reference with no timeframe says so and lists the
tokens that were fed; a chain nobody fed names the exact key to supply.
There is no quiet `na` for a misspelled or missing world.

Several cadences of one symbol can be fed at once (`"EURUSD-m5"` and
`"EURUSD-h1"`): the classic higher-timeframe filter. When the *traded*
market is fed at several cadences, the engine clock runs on the finest one
and the coarser feeds align as-of, latest completed value at or before each
bar, never anything from the future.

Orders name the symbol only (`buy("EURUSD", ...)`): an execution has no
timeframe.

## 3. Trading scripts and indicator scripts

One rule separates the two, keyed to the presence of any order verb:

| | trading script (has orders) | indicator script (no orders) |
|---|---|---|
| bare `close`, `open`, ... | error: name the market | legal: the chart's series |
| `plot()`, colors, drawing | error: strategies do not draw | legal |
| `log()` | legal | legal |
| orders | the point | absent by definition |

Why: where money moves, nothing may be implicit. An indicator, by contrast,
is genuinely a function of whatever chart hosts it; forcing an EMA ribbon
to name a market would be its own kind of nonsense.

## 4. Execution model

The engine runs the script once per bar, oldest to newest. Assignments made
at bar `n` are visible at bar `n+1`; series history builds as bars advance.
There is no lookahead: nothing on bar `n` can read bar `n+1`.

Order verbs called while processing bar `n` queue and fill at the OPEN of
bar `n+1`, plus slippage. At the end of the data, an order placed on the
final bar fills at that bar's close and any open position is closed at the
final close, so a backtest's results never carry open exposure.

The run window is a run parameter (`trade_from`/`trade_to`, CLI
`--from`/`--to`): entries are only placed on bars inside the window, exits
are never gated, and anything still open is flattened on the first bar past
`trade_to`. Indicators still compute over all bars, so a window never
changes warm-up.

## 5. Trading

Three verbs. The market is a string literal, the quantity is explicit, and
the engine never second-guesses an order.

| call | meaning |
|---|---|
| `buy("EURUSD", qty, sl=, tp=)` | Market order to open or add to a LONG position in EURUSD |
| `sell("EURUSD", qty, sl=, tp=)` | Market order to open or add to a SHORT position |
| `close("EURUSD")` | Flatten the EURUSD position |

```brue
if crossover(ema(EURUSD.h1.close, 12), ema(EURUSD.h1.close, 26)):
    buy("EURUSD", 0.25, sl=EURUSD.h1.close * 0.97, tp=EURUSD.h1.close * 1.06)
if crossunder(ema(EURUSD.h1.close, 12), ema(EURUSD.h1.close, 26)):
    close("EURUSD")
```

- The symbol must be a string literal: what a script trades is readable
  from its text, so a computed symbol is refused.
- `qty` is required and positive. There is no default quantity anywhere.
- `sl=`/`tp=` attach a protective bracket to that entry: for a long, `sl`
  is a stop below and `tp` a target above, mirrored for a short. If one bar
  touches both, the stop fills, not the target.
- Unknown order arguments are errors, never silently ignored.
- Buying while short (or selling while long) is a netting reversal: the
  opposite position closes first, then the new one opens, both at the next
  bar's open.
- Same-direction entries STACK (normal netting, averaged entry price). The
  engine never drops an order your code issued; if you want a cap, write
  the cap: `if strategy.open_trades < 3: buy("EURUSD", 1)`.
- One traded market per script for now (a second symbol in orders is a loud
  error); the multi-market engine is the next major stage.

Pending entries (`buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`) are
not supported yet and fail loudly rather than pretending.

## 6. Position and account state

Two read-only namespaces expose the simulator's state, marked to the
current bar's close:

| `position.` | meaning |
|---|---|
| `size` | Net position in units, negative when short |
| `avg_price` | Average entry price of what is open |
| `unrealized_pnl` | Open profit and loss |
| `entry_bar` / `entry_time` / `entry_price` | The earliest open entry |

| `strategy.` | meaning |
|---|---|
| `equity` | Capital plus realized plus unrealized |
| `initial_capital` | The RUN's capital parameter |
| `net_profit`, `gross_profit`, `gross_loss` | Realized |
| `open_trades` / `closed_trades` | Counts (`open_trades` is the stacking gate) |
| `win_rate`, `max_drawdown`, `commission` | Run-to-date statistics |

(Naming under review: these will become `account.*` and
`position("SYMBOL").*` when the multi-market engine lands; the current
names are documented because they are what runs today.)

## 7. Values and types

**Numbers.** 64-bit floats with JavaScript arithmetic semantics, so a
strategy computes bit-identically everywhere. Division or modulo by zero is
`NaN`, not an error. `%` is remainder with the sign of the dividend. `**`
is exponentiation. Very large and very small numbers print in scientific
notation exactly as JavaScript prints them.

**Strings.** Double-quoted. `+` concatenates and coerces anything.
F-strings interpolate expressions with an optional `.Nf` precision spec:
`f"close={EURUSD.h1.close:.2f}"`. Methods: `.upper() .lower()
.contains(sub) .starts_with(p) .ends_with(p) .replace(a, b) .split(sep)
.trim() .length()`.

**Booleans.** `true` / `false`.

**na.** The missing value for legitimately absent DATA: out-of-range
history, missing map keys, missing event fields, invalid arithmetic.
`na == na` is `true`, `str(na)` is `"na"`, `is_na(x)` tests,
`coalesce(x, fallback)` substitutes. Unknown NAMES are never na; they are
errors (section 20).

**Arrays.** `a = [3, 1, 4]`, index read/write, `.size()` (alias
`.length`), `.max() .min() .avg() .sum() .first() .last() .includes(v)
.join(sep)`.

**Maps.** `m = { "alpha": 1.5, beta: 2.5 }`; read with `.get(k)`, dot, or
brackets; write with `.put/.set`, dot, or brackets; `.contains/.has,
.remove, .keys(), .values(), .size`. Missing keys read `na`.

**Custom types and enums.**

```brue
type Zone:
    top: float = 0.0
    bottom: float = 0.0
    tag: string = "zone"
enum Signal:
    buy = "Buy Signal"
    sell
z = Zone.new(101.5, 99.5)
s = Signal.sell            # members are string-valued; bare = own name
```

`Type.new(...)` takes values positionally in field order; omitted fields
keep their defaults.

**Colors** exist as values in indicator scripts only (section 16).

## 8. Operators

| group | operators |
|---|---|
| arithmetic | `+  -  *  /  %  **` |
| comparison | `==  !=  <  <=  >  >=`, chainable: `1 < 2 < 3` |
| logic | `and  or  not` |
| ternary | `value_if if cond else value_else` and `cond ? a : b` |
| assignment | `=  +=  -=  *=  /=` |
| history | `series[n]` |
| member | `obj.field`, `obj[key]`, method calls |

## 9. Variables: persist and const

A plain assignment creates a per-bar variable, recomputed every bar.
`persist` declares a variable that KEEPS its value across bars; `const`
declares a named constant. The idiomatic bar counter:

```brue
persist n = 0
n = n + 1
if (n - 1) % 13 == 0:
    log(f"every 13th bar, starting at the first: {EURUSD.h1.close}")
```

## 10. Control flow

`if/elif/else`, `for x in range(...)` (Python-style `range`, negative steps
included), `for x in array`, `while`, `break`, `continue`.

## 11. Functions

```brue
def stop_below(price, pct=2):
    return price * (1 - pct / 100)
```

`def` defines a function with optional parameter defaults; functions take
series sources as arguments and can call indicator builtins on them.

**Multiple return values** are read by destructuring:
`m_line, m_signal, m_hist = macd(EURUSD.h1.close, 12, 26, 9)`. Extra names
beyond what the builtin returns read `na`. Member access on a multi-value
result (`m.macd`) is a loud error that names the fix.

## 12. The history operator

`series[n]` reads the value `n` bars back, on any series expression:
`EURUSD.h1.close[1]`, `sma(EURUSD.h1.close, 5)[2]`,
`(EURUSD.h1.high - EURUSD.h1.low)[3]`. Reading past the start of the data
reads `na`.

## 13. What counts as a series source

Indicator builtins that need history take a "source":

- a series chain field: `EURUSD.h1.close`, `XAUUSD.mybroker.h4.hl2`
- in indicator scripts, a bare price series: `close`, `hl2`, ...
- the result of another series builtin, directly or via a variable
- an inline expression of the above: `zscore(EURUSD.h1.close - EURUSD.h1.open, 60)`

A plain variable holding a per-bar computed expression is NOT a source (it
captures one number per bar, not the series); the engine rejects it and the
fix is to inline the expression. Indicators with a warm-up emit `na` until
ready; `sma`/`ema` skip a leading `na` head so chaining just works.

## 14. log(): diagnostics

`log(message)` records a line with the bar's timestamp. In a backtest the
lines come back on the result (`logs` in the output JSON); live, they land
in the run's journal. This is the diagnostic channel for trading scripts;
chart annotation is not (section 3).

```brue
if XAUUSD.mybroker.h1.close > XAUUSD.h1.close + 2:
    log(f"mybroker gold rich by {XAUUSD.mybroker.h1.close - XAUUSD.h1.close:.2f}")
```

## 15. User inputs

`input(default, "Title", min=, max=, step=, options=)` declares a value the
host can override without editing the script: a settings panel, a kwarg, an
optimizer sweep. The input's type follows its default (number, bool, or
string); a host override that cannot be read as that type is a loud error
(string forms coerce: `"55.5"` works for a numeric input). Overrides are
keyed by declaration order: `input_0`, `input_1`, ...

The old typed pickers (`input_symbol`, `input_timeframe`, ...) do not
exist: a script's symbols and timeframes are written in its text, not
chosen from a dropdown.

## 16. Indicator scripts: drawing and colors

In a script with no orders, drawing calls emit commands for the host's
chart; in a headless run they are collected with the results.

| call | notes |
|---|---|
| `plot(value, title=)` | Draw a series; returns the value |
| `hline(price, color=, style=, width=, title=, panel=)` | Horizontal level |
| `bgcolor(color)` | Tint the bar's background |
| `fill(id1, id2, color=, title=)` | Shade between two plots (by title) |
| `shape(kind, location=, color=, size=, text=, text_color=)` | Mark the bar |
| `label(text, x=, y=, location=, color=, bg=, size=, tooltip=, align=)` | Text at a bar |
| `table(position=, bg=)` then `t.cell(row, col, text, color, bg)` | A stats table |

Colors: named (`red, green, blue, ... gold, transparent`), hex literals
(`#FF3322`, alpha as a fourth byte), constructors `rgb(r,g,b)` /
`rgba(r,g,b,a)`, derived `with_alpha(color, a)`. Named colors sit at the
very end of identifier resolution, so a variable of the same name shadows
the color. Locations are bare words (`above_bar`, `below_bar`); sizes are
strings.

Panel placement and display names are host metadata, not language: the
terminal reads leading `# title:` and `# panel: price|below` comment
pragmas.

## 17. Event datasets and data tables

Point-in-time event tables are read with as-of accessors; they have no
timeframe (events happen when they happen):

```brue
n = econ.latest("NFP")     # most recent event at or before this bar
p = econ.prev("NFP")       # the one before that
if n != na:
    log(str(n.actual) + "|" + str(n.forecast) + "|" + str(n.surprise))
```

Known dataset names: `econ`, `earnings`, `cot`, `insider`; a host can feed
custom keyed event tables under any name. Event objects expose whatever
fields the feed carries; a missing key, feed, or field reads `na`.

Plain data tables (a host-fed table of timestamped columns) are read as
`tablename.column`, as-of aligned, usable as indicator sources. Unknown
columns error with the available list.

## 18. Sessions and time

`in_session(spec, tz=)` is `true` when the bar's timestamp falls inside a
trading session: a named session (`"london"`, `"new_york"`, `"tokyo"`,
`"sydney"`, `"frankfurt"`) or an `"HHMM-HHMM"` range with an IANA `tz=`.

Calendar parts, all UTC, all taking a timestamp: `year(time)`,
`month(time)`, `dayofmonth(time)`, `dayofweek(time)` (0 is Sunday),
`hour(time)`, `minute(time)`, `second(time)`. `time` is the current bar's
epoch-milliseconds timestamp and is available in every script.

## 19. Built-in function reference

144 built-in functions. Defaults are shown inline; a parameter without a
default is required. "source" means anything from
[section 13](#13-what-counts-as-a-series-source).

NOTE (open item): the functions below that take NO source read the traded
market's bars implicitly (`atr(14)`, `obv()`, `adx(14)`, ...). Per-market
forms arrive with the multi-market engine.

### Moving averages (15)

| function | notes |
|---|---|
| `sma(source, length)` | Simple |
| `ema(source, length)` | Exponential |
| `wma(source, length)` | Linear-weighted |
| `hma(source, length)` | Hull |
| `dema(source, length)` | Double exponential |
| `tema(source, length)` | Triple exponential |
| `smma(source, length)` | Smoothed (RMA) |
| `vwma(source, length)` | Volume-weighted |
| `vwap(source=hlc3)` | Running volume-weighted average price, cumulative from the first bar (no session anchor) |
| `alma(source, length=9, offset=0.85, sigma=6)` | Arnaud Legoux |
| `kama(source, length=10, fast_period=2, slow_period=30)` | Kaufman adaptive |
| `zlema(source, length=21)` | Zero-lag exponential |
| `t3(source, length=5, v_factor=0.7)` | Tillson T3 |
| `lsma(source, length=25)` | Least-squares |
| `mcginley(source, length=14)` | McGinley dynamic |

### Momentum and oscillators (31)

| function | returns |
|---|---|
| `rsi(source, length)` | Relative strength index |
| `macd(source, fast, slow, signal)` | `(line, signal, histogram)` |
| `stoch(source, high_src, low_src, length)` | Raw stochastic %K |
| `cci(source, length)` | Commodity channel index |
| `mfi(length)` | Money flow index |
| `williams_r(length)` | Williams %R |
| `roc(source, length)` | Rate of change, percent |
| `mom(source, length)` | Momentum, difference |
| `ao(fast=5, slow=34)` | Awesome oscillator |
| `tsi(source, long_length=25, short_length=13)` | True strength index |
| `dpo(source, length=21)` | Detrended price oscillator |
| `stoch_rsi(source, rsi_length=14, stoch_length=14, k_smooth=3, d_smooth=3)` | `(k, d)` |
| `coppock(source)` | Coppock curve |
| `trix(source, length=15)` | TRIX |
| `ultimate_osc(fast=7, mid=14, slow=28)` | Ultimate oscillator |
| `kst(source)` | Know sure thing |
| `fisher(length=10)` | `(fisher, trigger)` |
| `stc(source, fast=23, slow=50, cycle=10)` | Schaff trend cycle |
| `rvi(length=10)` | `(rvi, signal)` |
| `connors_rsi(source, rsi_period=3, streak_period=2, rank_period=100)` | Connors RSI |
| `cmo(source, length=9)` | Chande momentum |
| `apo(source, fast=12, slow=26)` | Absolute price oscillator |
| `ppo(source, fast=12, slow=26, signal=9)` | `(ppo, signal, histogram)` |
| `pvo(fast=12, slow=26, signal=9)` | `(pvo, signal, histogram)`, on volume |
| `qstick(length=8)` | QStick |
| `bop(length=14)` | Balance of power |
| `psych_line(source, length=12)` | Psychological line |
| `pfe(source, length=10)` | Polarized fractal efficiency |
| `ulcer_index(source, length=14)` | Ulcer index |
| `smi(length=13, smooth_k=25, smooth_d=2)` | `(smi, signal)` |
| `rel_vol_index(source, length=10, smoothing=14)` | Relative volatility index |

### Volatility, bands, channels (16)

| function | returns |
|---|---|
| `bb(source, length, mult=2)` | `(upper, middle, lower)` Bollinger |
| `atr(length)` | Average true range |
| `keltner(length, mult=1.5, atr_length=10)` | `(upper, middle, lower)` |
| `donchian(length)` | `(upper, middle, lower)` |
| `stddev(source, length)` | Rolling standard deviation |
| `hist_vol(source, length)` | Historical volatility |
| `chaikin_vol(length)` | Chaikin volatility |
| `tr()` | True range |
| `natr(length)` | Normalized ATR, percent |
| `squeeze(bb_length=20, bb_mult=2, kc_length=20, kc_mult=1.5)` | `(squeeze_on, momentum)` |
| `chandelier(length=22, mult=3)` | `(exit_long, exit_short)` |
| `bb_percent(source, length, mult=2)` | %B |
| `bb_width(source, length, mult=2)` | Band width |
| `envelopes(source, length, percent=2.5)` | `(upper, middle, lower)` |
| `acc_bands(length)` | `(upper, mid, lower)` acceleration bands |
| `price_channel(length)` | `(upper, mid, lower)` |

### Trend (15)

| function | returns |
|---|---|
| `adx(length)` | `(adx, plus_di, minus_di)` |
| `supertrend(length=10, mult=3)` | `(line, direction)` |
| `sar(start=0.02, step=0.02, max=0.2)` | `(sar, direction)` parabolic SAR |
| `ichimoku(tenkan=9, kijun=26, senkou_b=52)` | `(tenkan, kijun, senkou_a, senkou_b, chikou)` |
| `aroon(length)` | `(up, down, oscillator)` |
| `alligator()` | `(jaw, teeth, lips)` |
| `gator()` | `(upper, lower)` |
| `vortex(length)` | `(vi_plus, vi_minus)` |
| `choppiness(length)` | Choppiness index |
| `elder_ray(length=13)` | `(bull_power, bear_power)` |
| `mass_index(length=25)` | Mass index |
| `chande_kroll(p=10, q=9, x=1)` | `(stop_long, stop_short)` |
| `zigzag(deviation=5)` | Zigzag pivots |
| `fractals(period=2)` | `(up, down)` Williams fractals |
| `vhf(length=28)` | Vertical horizontal filter |

### Volume (14)

| function | notes |
|---|---|
| `obv()` | On-balance volume |
| `cmf(length)` | Chaikin money flow |
| `adl()` | Accumulation/distribution line |
| `force_index(length=13)` | Elder force index |
| `eom(length=14)` | Ease of movement |
| `vol_sma(length=20)` | Volume SMA |
| `nvi()` | Negative volume index |
| `pvi()` | Positive volume index |
| `pvt()` | Price volume trend |
| `vroc(length=14)` | Volume rate of change |
| `net_volume(length=14)` | Net volume |
| `twiggs_mf(length=21)` | Twiggs money flow |
| `vol_osc(fast=5, slow=10)` | Volume oscillator |
| `klinger(fast=34, slow=55, signal=13)` | `(kvo, signal)` |

### Statistics and regression (8)

| function | notes |
|---|---|
| `correlation(source_a, source_b, length)` | Rolling Pearson correlation |
| `linreg(source, length, offset=0)` | Linear regression value |
| `linreg_slope(source, length)` | Regression slope |
| `r_squared(source, length)` | Fit quality |
| `zscore(source, length)` | Standard score |
| `beta(target, benchmark, length)` | Rolling beta |
| `spread(a, b)` | `a - b` as a series (usable as a source) |
| `ratio(a, b)` | `a / b` as a series (usable as a source) |

### Series logic (12)

| function | notes |
|---|---|
| `highest(source, length)` / `lowest(source, length)` | Rolling extremes |
| `sum(source, length)` | Rolling sum |
| `crossover(a, b)` | `true` on the bar `a` crosses above `b` |
| `crossunder(a, b)` | `true` on the bar `a` crosses below `b` |
| `cross(a, b)` | Either direction |
| `rising(source, length)` / `falling(source, length)` | Monotone over the window |
| `change(source, length=1)` | Difference vs `length` bars back |
| `barssince(condition)` | Bars since the condition was last true |
| `is_na(x)` | Missing test |
| `coalesce(x, default)` | Substitute for missing |

### Scalar math (13)

`abs(x)`, `max(a, b)`, `min(a, b)`, `round(x, digits=0)`, `floor(x)`,
`ceil(x)`, `sqrt(x)`, `pow(x, y)`, `ln(x)`, `log10(x)`, `exp(x)`,
`sign(x)`. Natural log is `ln` because `log` is the diagnostic printer:
`ln` computes, `log` prints, never confused.

### Time (7)

`year(t)`, `month(t)`, `dayofmonth(t)`, `dayofweek(t)`, `hour(t)`,
`minute(t)`, `second(t)`. All UTC; `t` is usually `time`.

### Orders and runtime services (not counted above)

The order verbs `buy`, `sell`, `close` (section 5); `log`, `input`,
`range`, `in_session`, `str`; and in indicator scripts the drawing calls
and color constructors (section 16).

## 20. Errors: loud or quiet, never wrong

The design rule: anything that would change trading behavior fails LOUDLY
with a message that names the fix; anything legitimately absent as DATA
reads `na` QUIETLY so a script can degrade gracefully. Nothing in between.

Loud (script error, with a suggestion):

- an unknown identifier, with a did-you-mean: `threshhold` for `threshold`
  is an error at the exact line and column, never a quiet `na` that silently
  disables a strategy
- a `strategy()` header, or a `use` statement
- a bare series or a drawing call in a trading script
- a series reference that names no timeframe, or a chain nobody fed (the
  message names the fed tokens / the exact feed key)
- a feed whose declared timeframe token contradicts its actual bar spacing
- a computed order symbol, a second traded market, a missing quantity, an
  unknown order argument
- `bar_index`, `barstate.*`, `last_bar`, `is_realtime` and the rest of the
  chart/backtest fiction family
- the `entry()/exit()/close_all()` layer and the typed `input_*` pickers
- an unknown function or kwarg (with did-you-mean), a missing required
  argument, malformed syntax
- a per-bar computed variable used as a series source
- member access on a multi-value result (destructure instead)
- a host input override that cannot be read as the input's declared type
- the pending order verbs (`buy_limit` and friends)

Quiet (`na`):

- history reads past the start of the data
- missing map keys, missing event-dataset keys and fields
- invalid arithmetic (division by zero and friends)

## 21. Running scripts

The same script text runs two ways: over history (the engine simulates the
fills, the CLI and `run_script*` below) and live (the identical order calls
go to the broker through Brue Connect, `brue.login` then `brue.buy` /
`brue.sell` / `brue.close`). Brue reports what it executed; there is no
stats block, no equity frame, and no pandas indicator surface in the
package. Research on the results happens in plain Python on the host.

### Python

```python
import brue
from brue.runtime import ExternalData, RunOptions

# 1. Bars in: parse_bars_csv reads a time,open,high,low,close,volume csv
#    (epoch ms times) into the engine's bar list.
eur = brue.parse_bars_csv(open("eurusd_h1.csv").read())
gold = brue.parse_bars_csv(open("xauusd_h1.csv").read())

# 2. Run over history. run_script(source, bars) is the one-feed door;
#    run_script_full adds named feeds and run parameters.
out = brue.run_script(open("trend.brue").read(), eur)
out = brue.run_script_full(
    open("gold_filter.brue").read(), eur,
    ExternalData(use_bars={"XAUUSD-h1": gold}),      # every non-primary feed
    {},                                              # input() overrides
    RunOptions(capital=100_000, commission=0.001,
               commission_type="percent", slippage=0.0,
               trade_from=brue.parse_time_arg("2024-01-01"),
               trade_to=brue.parse_time_arg("2025-01-01")))

out.success                 # False when the script was loud (out.errors)
out.commands                # trade_entry / trade_exit records with price, qty, pnl
out.logs                    # (time_ms, message) from log()
out.to_json()               # the canonical JSON the conformance suite locks

# 3. Indicator scripts: plot() calls become named per-bar series.
ind = brue.Indicator(open("ema_ribbon.brue").read())
ind.series(eur)             # {"ema8": [...], "ema21": [...], ...}
ind.compute(eur)            # the same as a pandas DataFrame (pandas optional)

# 4. Live: one key, then the order vocabulary goes to the broker.
brue.login(api_key="lse_...")            # or BRUE_API_KEY / LSE_API_KEY env,
                                         # or ~/.config/brue/config.json
brue.buy("EURUSD", 0.5, sl=1.05, tp=1.12)
brue.sell("XAUUSD", 1)
brue.close("EURUSD")                     # close(None) flattens every position
brue.positions(); brue.balance(); brue.history("EURUSD", "1h", 200)
```

`RunOptions` fields: `capital`, `commission`, `commission_type`
(`"percent"`|`"fixed"`), `slippage`, `trade_from`/`trade_to` (the entry
window, epoch ms; `parse_time_arg` accepts epoch ms or YYYY-MM-DD),
`primary_symbol` (names the primary bars' market for an orderless script).
`ExternalData` carries `use_bars` (named foreign bar feeds), `dataset_snapshots`
(event datasets) and `series_tables` (plain time-indexed tables), each keyed
by the name the script uses. In `use_bars` the traded market's own bars are
the primary `bars` argument (finest cadence when several are fed); a plain
key gets its timeframe token derived from the bars, a token-suffixed key
(`XAUUSD-h1`) is validated against them.

### Command line

```
python -m brue run <script.brue> <bars.csv> [--data KEY=path ...]
       [--capital N] [--commission N] [--commission-type percent|fixed]
       [--slippage N] [--from T] [--to T] [--resample TF]
python -m brue resample <in.csv> <tf> <out.csv>
python -m brue gen <n> <seed> <out.csv>
python -m brue mathcheck
```

`run` prints the canonical execution JSON. `--data KEY=path` feeds series
and datasets (csv for bars, json for event snapshots and tables); a plain
KEY gets its timeframe token derived from the bars, a token-suffixed KEY
(`XAUUSD-h1`) is validated against them.

## 22. Known limits and open questions

The honest list, and exactly where design feedback is most useful:

1. **Pending entries.** Market orders plus protective brackets only;
   `buy_limit` and friends are rejected loudly.
2. **One traded market per script.** Reads span any number of worlds;
   orders name one market. The multi-market engine (several positions in
   one script, `position("SYMBOL")`, `account.*`) is the next major stage.
3. **No-source builtins read the traded market implicitly** (`atr(14)`,
   `obv()`, ...). Their per-market forms arrive with the multi-market work.
4. **No engine resampling yet.** Each declared timeframe must be fed at
   that cadence; one feed serving several tokens via engine-side
   aggregation is future work (`python -m brue resample` covers the gap).
5. **Symbols that are not identifiers** (`DEMO:BTC`) can be traded but not
   read as chains; hosts should feed identifier-safe names.
6. **End-of-data settlement.** A backtest fills a final-bar order at that
   bar's close and flattens at the end so results carry no open exposure;
   whether open exposure should instead be reported as-is is under review.
7. **Two ternary spellings** exist; one should probably win.
8. **Input override keys** are positional (`input_0`); named keys are under
   consideration.
