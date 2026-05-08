# Brue Changelog

## v0.9.1 (2026-05-08)

Patch release. v0.9 shipped the language semantics for `use SYMBOL at TF
as alias` and the editor's `strategy(symbol=, timeframe=)` binding, but
the production data path had five wiring bugs that made every fetch
return empty. Same-day patch fixes the entire chain end-to-end.

**Fixed (production data wiring)**

- PostgRESTDataProvider's base URL was `/brue` subpath; corrected to
  the bare PostgREST root. Every fetch was 404ing silently and the
  fallback to chart bars was masking the real failure.
- Symbol -> table resolution now reads `x_pricecache` (the public-
  facing view of `x_op.candle_table`) instead of using a hardcoded
  `candles_<x>` formula. US equities live under `d_candles_*`, not
  `candles_*` — without this lookup, NVDA / AAPL / TSLA / 4000+ stocks
  were unreachable.
- `BarData.time` is unix milliseconds throughout (matches the type
  comment, `ProCandlestickChart`'s `Date.getTime()`, and the runtime's
  `tsMs` in_session math). resolveBars and useForeignBars no longer
  multiply chartBars.time by 1000 — that produced year-57805 ISO
  strings PostgREST rejected. PostgRESTDataProvider also returns
  `time` in ms (was returning seconds, breaking the secondary
  foreign-bar fetch when its own output was fed back as the primary
  time grid).
- The editor's Run path now fetches foreign-symbol data via
  `buildUseData` for any non-dataset `use` declarations on the AST.
  Previously `useHeadlessBrue` did this but `BrueScriptEditor.handleRun`
  bypassed it, so MTF aliases like `h1.close` always read as `na` in
  the editor's Output panel even when the chart-side render path
  worked.

**Result**: the worked example in SYNTAX.md (`use NVDA at 1H as h1` on
a NVDA 5m primary) now fires real trades end-to-end. Verified in the
editor's Output panel: `bound: NVDA @ 5m (2000 bars, declared)`,
followed by labels showing the actual forward-filled 1H NVDA close
on every 5m bar.

**Lesson logged for future contributors:** when shipping a new data
provider, run one live probe against the production endpoint with
real chart bars before declaring done. The contract tests covered
behaviour through a Fixture provider three different ways and
production was still 400ing on every call.

## v0.9 (2026-05-07)

Multi-timeframe foreign access lifts. `use SYMBOL at TIMEFRAME as alias` now
actually fetches at the requested timeframe and forward-fills onto the
primary bar grid. Same-TF behaviour is unchanged. Pine Script's
`request.security(..., lookahead=barmerge.lookahead_off)` is the equivalent.

```python
strategy("MTF: 1H trend, 5m entry",
         symbol="NVDA", timeframe="5m")
use NVDA at 1H as h1
use NVDA at 1D as d1

if h1.close > ema(h1.close, 50) and d1.close > ema(d1.close, 200) \
   and rsi(close, 14) < 40 and position.size == 0:
    entry("L", long)
```

**Implementation**

- buildUseData routes through the DataProvider abstraction (Step 3 of
  v0.8) for both same-TF (1m fetch) and cross-TF (TF-bucketed fetch) paths.
- Dedupe key is now (canonical, timeframe), so two scripts using SPY at
  1H and 1D share their respective fetches but don't collide.
- The forward-fill is the existing as-of-join — once a foreign bar at
  time F is seen, it stays in `last` until a later one arrives. Higher-TF
  foreigns naturally produce stairs across the primary grid.

**Limitation removed**

The v0.8 `use SYMBOL at TIMEFRAME` limitation note is gone. Cross-TF
foreign access works.

## v0.8 (2026-05-07)

`strategy()` accepts `symbol` and `timeframe`, and the kwarg list is now closed.

**Added**

- `strategy("X", symbol="NVDA", timeframe="1H")` declares the data context
  for the script. The runtime resolves bars from the declared `(symbol,
  timeframe)` instead of inheriting from whatever pair the chart happens to
  be loaded on. Both kwargs must be string literals (not expressions) so the
  controller can read them statically before fetching bars. Both are optional
  for back-compat — when omitted, the chart's loaded symbol/timeframe still
  drive the data feed.
- A 14-bar ATR on a 1m chart is a different number from a 14-bar ATR on a 1D
  chart. Strategies that use any bar-count-dependent indicator (`ema`, `sma`,
  `rsi`, `atr`, `macd`, `bb`, `adx`, `stoch`, `cci`, `supertrend`, `ichimoku`)
  should now declare `timeframe=` so backtests are reproducible regardless of
  what's loaded in the chart.

**Changed**

- `strategy()` kwargs are now validated against a closed set:
  `overlay`, `precision`, `capital`, `initial_capital`, `commission`,
  `commission_type`, `slippage`, `pyramiding`, `default_qty`,
  `default_qty_type`, `currency`, `symbol`, `timeframe`.
  Anything else is rejected at parse time with a Levenshtein "did you mean…"
  suggestion. The previous behaviour silently swallowed any unknown kwarg —
  `comission=0.001` (typo) shipped to production scripts where commission
  was effectively zero. This is now a hard error.

**Deprecated**

- `input_timeframe(...)` for the script's own timeframe. The function still
  exists as a UI input control but does not bind data — only
  `strategy(timeframe="...")` actually drives bar fetching. Existing scripts
  using `input_timeframe` continue to work, the value just isn't consumed.

**Known limitation**

- `use SYMBOL at TIMEFRAME as alias` is parsed but the `at TIMEFRAME` clause
  reads as `na` for every bar when the timeframe differs from the chart's.
  Multi-timeframe foreign access is on the roadmap.

## v0.7 (2026-05-04)

`plot()` is back, with Pine semantics.

**Changed**

- `plot(call)` is now an explicit opt-in to register a chart-native indicator
  on the host's indicator panel. The runtime is a no-op pass-through; the
  registration is performed by a static AST scan at script Run time. Mirrors
  Pine Script's separation: bare `ema(close, 20)` is a value, `plot(ema(close, 20))`
  is what makes the chart show it.
- `plot()` traces back through top-level assignments, so both
  `plot(ema(close, 20))` and `fast = ema(close, 20); plot(fast)` register.
- Recognised wrapped calls (constant-arg periods only): `ema`, `sma`, `smma`,
  `rsi`, `macd`, `bb`, `stoch`, `atr`, `vwap`, `adx`, `cci`, `roc`. Anything
  else inside `plot()` (arbitrary expressions, custom helpers, foreign-symbol
  results) is silently ignored — for those, use the chart host's built-in
  indicator menu directly.

**Removed (vs v0.6)**

- Implicit auto-promotion of bare `ema()` / `sma()` / `rsi()` / etc calls to
  chart-native indicators. The previous behaviour spawned mystery subplots
  whenever those calls were used as intermediates (e.g. `correlation(roc(close, 1), roc(other.close, 1), 50)`
  silently added a ROC subplot the user never asked for). To keep the EMAs
  visible on a v0.6 script, wrap the existing `ema(close, ...)` calls in
  `plot(...)`.

See [SYNTAX.md](SYNTAX.md#plotting-indicators) for the full plot() reference.

---

## v0.6 (2026-05-02)

Public release. The language surface is locked to the runtime that ships
with the chart host today.

**Surface**
- One declaration: `strategy("Title", ...)`. No `indicator()`.
- 143 registered built-in functions across 10 modules: moving averages
  (15), oscillators (31), trend (15), volatility (16), volume (14),
  statistics (8), math/logic (24), time (7), typed inputs (6), trade
  verbs (7).
- Drawing primitives: `shape`, `label`, `bgcolor`, `hline`, `table`
  (+ `t.cell(...)`).
- Strategy primitives: `entry`, `exit`, `close_all`, with `position.*`
  and `strategy.*` namespaces.
- Live trading verbs (last-bar-only): `buy`, `sell`, `buy_limit`,
  `sell_limit`, `buy_stop`, `sell_stop`, `close`.
- Cross-pair access: `use SYMBOL [at TF] [as ALIAS]` declared at the
  top of the script, then read `alias.close`, `alias.high`, etc.
- User-defined: `def`, `type` (struct), `enum`.
- Persistence: `persist`, `const`. (No `persist_tick`, no `var`.)

**Removed in this release vs older internal versions**
- `indicator(...)` declaration. Use `strategy(...)`.
- `plot()`. Brue is a strategy + drawing layer; continuous indicator
  series belong to the chart host's built-in indicator menu.
  *(Note: `plot()` was re-added in v0.7 with Pine-style explicit opt-in
  semantics. See the v0.7 entry above.)*
- `request()` / `request_security()` / `request_financial()` /
  `request_economic()`. Cross-symbol access is now `use SYMBOL`.
- `alert()` / `alertcondition()`. Mark interesting bars with `shape()`.
- `line_*` / `box_*` / `polyline_*` drawing handles.
- `barcolor()` / `plotcandle()` / `plotarrow()`.
- `persist_tick`, `var`, `varip`.
- `input.timeframe(...)` dot syntax. The functions are
  `input_timeframe(...)`, `input_symbol(...)`, etc. (underscore).

See [SYNTAX.md](SYNTAX.md) for the complete current reference.
