# Brue Changelog

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
