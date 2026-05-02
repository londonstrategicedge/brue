# Brue Changelog

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
- `request()` / `request_security()` / `request_financial()` /
  `request_economic()`. Cross-symbol access is now `use SYMBOL`.
- `alert()` / `alertcondition()`. Mark interesting bars with `shape()`.
- `line_*` / `box_*` / `polyline_*` drawing handles.
- `barcolor()` / `plotcandle()` / `plotarrow()`.
- `persist_tick`, `var`, `varip`.
- `input.timeframe(...)` dot syntax. The functions are
  `input_timeframe(...)`, `input_symbol(...)`, etc. (underscore).

See [SYNTAX.md](SYNTAX.md) for the complete current reference.
