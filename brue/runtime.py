# The runtime. Executes the AST bar by bar; the scope model,
# series-promotion rules, cache-key fingerprints and dispatch order are all
# pinned by the conformance goldens. Every observable quirk is deliberate:
# the multi-return-as-expression recompute, global series promotion from
# function bodies, `long`/`short` resolving before user variables, the
# barHistory pre-scan, compound-assign coercion to 0, and the na==na rule.
#
# Cross-module enum conventions: value.Value and builtins.ArgVal/ExecResult
# are tagged classes: variants are built by name (Value.Num(n),
# ExecResult.Series(s)), discrimination is `.kind` ("num"/"str"/... and
# "series"/"tuple"/"scalar"), the Value payload is `.v`, the
# ArgVal/ExecResult payload is `.value`, and the payload-free variants
# (Value.Null, Value.Undefined, ArgVal.Missing) are shared singleton
# instances. output.Command variants are nested dataclasses
# (Command.Shape(...)). Values are shared by reference and never copied,
# so two reads of one stored Value are the same object.
import math
import re
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Optional

from . import ast
from . import builtins
from . import errors
from . import jsmath
from . import jsnum
from . import output
from . import session
from . import strategy
from . import value

# Local aliases for the cross-module types used throughout.
Value = value.Value
ColorVal = value.ColorVal
OrderedMap = value.OrderedMap
UdtVal = value.UdtVal
BrueError = errors.BrueError
find_closest_match = errors.find_closest_match
js_number_to_string = jsnum.js_number_to_string
js_to_fixed = jsnum.js_to_fixed
get_builtin = builtins.get_builtin
all_builtin_names = builtins.all_builtin_names
ArgVal = builtins.ArgVal
Ctx = builtins.Ctx
ExecResult = builtins.ExecResult
ParamType = builtins.ParamType
StrategyConfig = strategy.StrategyConfig
StrategyEngine = strategy.StrategyEngine
Command = output.Command
ErrorInfo = output.ErrorInfo
ExecOutput = output.ExecOutput
InputInfo = output.InputInfo
LiveOrder = output.LiveOrder
TableCell = output.TableCell
trade_command_to_output = output.trade_command_to_output
Loc = ast.Loc

NAN = float("nan")


@dataclass
class BarData:
    open: float
    high: float
    low: float
    close: float
    volume: float
    time: float


PRICE_SERIES_NAMES = [
    "close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4", "time", "bar_index",
]

FOREIGN_FIELDS = ["open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"]

NAMED_COLOR_DEFS = [
    ("red", 255.0, 0.0, 0.0, 1.0),
    ("green", 0.0, 128.0, 0.0, 1.0),
    ("blue", 0.0, 0.0, 255.0, 1.0),
    ("yellow", 255.0, 255.0, 0.0, 1.0),
    ("orange", 255.0, 165.0, 0.0, 1.0),
    ("purple", 128.0, 0.0, 128.0, 1.0),
    ("pink", 255.0, 192.0, 203.0, 1.0),
    ("cyan", 0.0, 255.0, 255.0, 1.0),
    ("white", 255.0, 255.0, 255.0, 1.0),
    ("black", 0.0, 0.0, 0.0, 1.0),
    ("gray", 128.0, 128.0, 128.0, 1.0),
    ("lime", 0.0, 255.0, 0.0, 1.0),
    ("teal", 0.0, 128.0, 128.0, 1.0),
    ("navy", 0.0, 0.0, 128.0, 1.0),
    ("maroon", 128.0, 0.0, 0.0, 1.0),
    ("olive", 128.0, 128.0, 0.0, 1.0),
    ("aqua", 0.0, 255.0, 255.0, 1.0),
    ("fuchsia", 255.0, 0.0, 255.0, 1.0),
    ("silver", 192.0, 192.0, 192.0, 1.0),
    ("gold", 255.0, 215.0, 0.0, 1.0),
    ("transparent", 0.0, 0.0, 0.0, 0.0),
]


def color_to_css(c):
    """rgba(r,g,b,a) CSS string with JS number formatting."""
    return "rgba({},{},{},{})".format(
        js_number_to_string(c.r),
        js_number_to_string(c.g),
        js_number_to_string(c.b),
        js_number_to_string(c.a),
    )


class StoredSeries:
    """Tagged union: StoredSeries.Nums (list of floats) and StoredSeries.Vals
    (list of Values), payload in `.value`."""

    def len(self):
        raise NotImplementedError

    def at(self, i):
        raise NotImplementedError


class _StoredSeriesNums(StoredSeries):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def len(self):
        return len(self.value)

    def at(self, i):
        return Value.Num(self.value[i])


class _StoredSeriesVals(StoredSeries):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def len(self):
        return len(self.value)

    def at(self, i):
        # Values are shared by reference: return the stored object itself.
        return self.value[i]


StoredSeries.Nums = _StoredSeriesNums
StoredSeries.Vals = _StoredSeriesVals


@dataclass
class FunctionData:
    params: list
    body: list


@dataclass
class Scope:
    series_vars: dict = field(default_factory=dict)
    bar_vars: dict = field(default_factory=dict)
    persist_vars: dict = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    functions: dict = field(default_factory=dict)
    bound_arg_exprs: Optional[dict] = None


class Flow:
    """Control-flow signal: Normal/Break/Continue are singleton instances,
    Flow.Return carries the value in `.value`."""

    def is_signal(self):
        return self is not Flow.Normal


class _FlowReturn(Flow):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


Flow.Normal = Flow()
Flow.Break = Flow()
Flow.Continue = Flow()
Flow.Return = _FlowReturn


# == External data (the host-fed side of `use`) ==========================
# The host hands over RAW time-sorted rows and the ENGINE does every as-of
# alignment (foreign bars and dataset snapshots alike), so no host can
# introduce lookahead bias. Times must be in the same unit as the primary
# bars' `time` values (epoch ms everywhere in this project).


@dataclass
class ForeignBar:
    """One raw bar of a foreign symbol (`use SPY`), time-ascending."""
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class DatasetSnapshot:
    """One point-in-time event row of a keyed dataset (`use "econ"` or a
    host-supplied custom event table). `fields` keeps every field of the
    source row in its original order, including the timestamp and key
    entries, because member access on a snapshot sees exactly those keys."""
    timestamp: float
    key: str
    fields: list  # list of (name, Value) pairs


@dataclass
class SeriesTable:
    """A plain time-indexed table of numeric columns: `use funding` + a
    host-supplied table makes `funding.rate` a bar-aligned series. Column
    vectors are row-aligned with `times`."""
    times: list
    columns: list  # list of (name, list of floats) pairs


@dataclass
class ExternalData:
    """Everything a host can feed the engine, keyed by the `use` statement's
    canonical name (NOT the alias; the engine maps canonical to alias from
    the parsed program, so hosts never need to read scripts)."""
    use_bars: dict = field(default_factory=dict)
    dataset_snapshots: dict = field(default_factory=dict)
    series_tables: dict = field(default_factory=dict)


@dataclass
class AlignedBar:
    """Forward-filled foreign bar values for one primary bar slot."""
    open: float
    high: float
    low: float
    close: float
    volume: float


def align_as_of(primary_times, foreign):
    """As-of alignment: for each primary bar time T keep the most recent
    foreign bar with time <= T, forward-filled; None before the first
    foreign observation. Input order is trusted (rows are never sorted
    here), so the host must supply time-ascending rows."""
    out = []
    f_idx = 0
    last = None
    for t in primary_times:
        while f_idx < len(foreign) and foreign[f_idx].time <= t:
            b = foreign[f_idx]
            last = AlignedBar(open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
            f_idx += 1
        out.append(last)
    return out


def _snapshot_ts_cmp(a, b):
    # A NaN timestamp compares equal to everything (the conformance goldens
    # pin this); Python's default float sort does not do that, so the
    # comparator is explicit. The sort is stable.
    if a.timestamp < b.timestamp:
        return -1
    if a.timestamp > b.timestamp:
        return 1
    return 0


class DatasetAccessor:
    """Keyed point-in-time lookup: snapshots grouped by key (first-seen
    order is irrelevant, lookup is by exact key), each group stable-sorted
    by timestamp ascending, PIT linear scan per call. Every latest()/prev()
    call mints a FRESH object, so `==` between two results is false by
    reference equality."""

    def __init__(self, snapshots):
        by_key = {}
        for s in snapshots:
            by_key.setdefault(s.key, []).append(s)
        for arr in by_key.values():
            # stable sort: equal timestamps keep source order.
            arr.sort(key=cmp_to_key(_snapshot_ts_cmp))
        self.by_key = by_key

    @staticmethod
    def snapshot_value(s):
        # __dataset marker first, then every raw field in source order
        # (which may include timestamp/key entries).
        fields = OrderedMap()
        fields.set("__dataset", Value.Bool(True))
        for (k, v) in s.fields:
            fields.set(k, v)
        return Value.Udt(UdtVal(type_name="__dataset", fields=fields))

    def latest_at(self, key, bar_time):
        arr = self.by_key.get(key)
        if arr is None:
            return Value.Null
        result = None
        for s in arr:
            if s.timestamp > bar_time:
                break
            result = s
        if result is not None:
            return DatasetAccessor.snapshot_value(result)
        return Value.Null

    def prev_at(self, key, bar_time):
        arr = self.by_key.get(key)
        if arr is None:
            return Value.Null
        prev = None
        latest = None
        for s in arr:
            if s.timestamp > bar_time:
                break
            prev = latest
            latest = s
        if prev is not None:
            return DatasetAccessor.snapshot_value(prev)
        return Value.Null


@dataclass
class RunOptions:
    """Host-level run configuration. Not part of the language: the backtest
    window is a property of a RUN, not of a strategy, so it arrives from the
    CLI/embedder instead of a strategy() kwarg."""
    # Entries are only placed on bars with time >= trade_from (ms epoch).
    trade_from: Optional[float] = None
    # Entries stop after trade_to and any open position is flattened on the
    # first bar past it. Indicators still compute over ALL bars, so a
    # window never changes warm-up; it only gates trading.
    trade_to: Optional[float] = None
    # Run-level account/cost settings: capital and costs are properties of
    # a RUN, never of strategy text. When set they override whatever a
    # strategy() header declared; headerless scripts have no header at all.
    capital: Optional[float] = None
    commission: Optional[float] = None
    commission_type: Optional[str] = None
    slippage: Optional[float] = None
    # The primary bars' market name (from the data dict / host), so an
    # orderless script's qualified references still bind to the primary.
    primary_symbol: Optional[str] = None
    # Host extension: record every plot() call's per-bar value and kwargs
    # on rt.plots so a chart host can draw indicator lines. Off by default;
    # the canonical JSON output is unchanged either way (plots never
    # serialize).
    collect_plots: bool = False
    # Host extension: capture the orders the script queued on the FINAL bar
    # as order INTENTS instead of letting finalize fill them at the last
    # close. This is the live-driver primitive: re-run the window on each
    # new closed bar, read .intents, hand them to a broker connector. Off by
    # default; the canonical JSON output is unchanged either way (intents
    # never serialize).
    capture_intents: bool = False


def execute(program, bars, input_overrides, external):
    return execute_with_options(program, bars, input_overrides, external, RunOptions())


def execute_with_options(program, bars, input_overrides, external, options):
    bar_count = len(bars)
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []
    times = []
    for b in bars:
        opens.append(b.open)
        highs.append(b.high)
        lows.append(b.low)
        closes.append(b.close)
        volumes.append(b.volume)
        times.append(b.time)
    hl2 = []
    hlc3 = []
    ohlc4 = []
    for i in range(bar_count):
        hl2.append((highs[i] + lows[i]) / 2.0)
        hlc3.append((highs[i] + lows[i] + closes[i]) / 3.0)
        ohlc4.append((opens[i] + highs[i] + lows[i] + closes[i]) / 4.0)

    # Named colors are minted once so repeated reads of `red` compare equal
    # by reference.
    named_colors = {}
    for (n, r, g, b, a) in NAMED_COLOR_DEFS:
        named_colors[n] = Value.Color(ColorVal(r=r, g=g, b=b, a=a))

    # Accessor groups hold the host's snapshot objects directly (shared,
    # read-only references); nothing is copied.
    snapshots_rc = external.dataset_snapshots

    use_by_alias = {}
    dataset_aliases = {}
    dataset_accessors = {}
    aligned_use = {}
    series_by_alias = {}
    for u in program.uses:
        if u.is_dataset:
            dataset_aliases[u.alias] = Value.Udt(
                UdtVal(type_name="__dataset_accessor", fields=OrderedMap())
            )
            snaps = snapshots_rc.get(u.canonical, [])
            dataset_accessors[u.alias] = DatasetAccessor(snaps)
        elif u.canonical in external.series_tables:
            # `use NAME` + a host-supplied plain table
            # makes the alias a series dataset (alias.column reads as-of
            # values). Deliberately NOT in use_by_alias, so the
            # foreign-symbol field whitelist never applies to it.
            series_by_alias[u.alias] = external.series_tables[u.canonical]
        elif u.canonical in snapshots_rc:
            # A custom keyed event table under any
            # canonical name promotes the alias to a dataset accessor
            # (.latest/.prev), same semantics as the four built-in names.
            dataset_aliases[u.alias] = Value.Udt(
                UdtVal(type_name="__dataset_accessor", fields=OrderedMap())
            )
            dataset_accessors[u.alias] = DatasetAccessor(snapshots_rc[u.canonical])
        else:
            use_by_alias[u.alias] = ()
            if u.canonical in external.use_bars:
                aligned_use[u.alias] = align_as_of(times, external.use_bars[u.canonical])

    rt = Rt(
        program=program,
        bar_count=bar_count,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
        times=times,
        hl2=hl2,
        hlc3=hlc3,
        ohlc4=ohlc4,
        named_colors=named_colors,
        use_by_alias=use_by_alias,
        dataset_aliases=dataset_aliases,
        dataset_accessors=dataset_accessors,
        aligned_use=aligned_use,
        series_by_alias=series_by_alias,
        input_overrides=input_overrides,
        run_options=options,
    )
    # A headerless script is pure logic: qualified series only,
    # symbol-carrying orders, no chart/backtest fictions.
    rt.v2 = bool(program.declaration.headerless)
    rt.logs = []
    rt.v2_market = None
    rt.v2_trading = False
    if rt.v2:
        # The traded market is read from the script's own orders. One
        # market per script; its name binds to the primary bars so
        # SYMBOL.close is the primary series.
        # A script with NO orders is an indicator: the chart is its
        # context, so bare series and drawing stay legal there.
        found = []
        scan_order_markets_stmts(program.body, found)
        rt.v2_trading = _HAS_ORDERS in found
        distinct = sorted(set(m for m in found if m != _HAS_ORDERS))
        if len(distinct) > 1:
            return execute_throw_output(BrueError.runtime(
                "orders name {} markets ({}): one market per script for "
                "now.".format(len(distinct), ", ".join(distinct)),
                Loc(line=0, col=0),
                "Multi-market scripts are the next design stage; split "
                "into one script per traded market."))
        # Reference = declaration, and every reference names its cadence:
        # EURUSD.h1.close reads feed "EURUSD-h1"; XAUUSD.mybroker.h4.close
        # reads "XAUUSD-mybroker-h4". Feeds register under their full
        # hyphen key: a plain-named feed gets its token derived from its
        # actual bar spacing; a token-suffixed feed is VALIDATED against
        # its actual spacing (feeding 1m bars under -h1 is a loud lie).
        primary_tok = derive_tf_token(bars)
        primary_names = []
        if distinct:
            rt.v2_market = distinct[0]
            primary_names.append(rt.v2_market)
        if options.primary_symbol:
            primary_names.append(options.primary_symbol)
        for nm in primary_names:
            if primary_tok is not None:
                key = "{}-{}".format(nm, primary_tok)
                if key not in use_by_alias:
                    use_by_alias[key] = ()
                    aligned_use[key] = list(bars)
        for k, fbars in external.use_bars.items():
            tail = k.rsplit("-", 1)[-1]
            tok = derive_tf_token(fbars)
            if is_tf_token(tail):
                if tok is not None and tok != tail:
                    return execute_throw_output(BrueError.runtime(
                        "feed '{}' declares {} but its bars are spaced "
                        "{}.".format(k, tail, tok),
                        Loc(line=0, col=0),
                        "Fix the key or the data; a declared timeframe "
                        "must match the actual bar spacing."))
                key = k
            elif tok is not None:
                key = "{}-{}".format(k, tok)
            else:
                key = k
            if key not in use_by_alias:
                use_by_alias[key] = ()
                aligned_use[key] = align_as_of(times, fbars)
        for k, snaps in snapshots_rc.items():
            if k not in dataset_aliases:
                dataset_aliases[k] = Value.Udt(
                    UdtVal(type_name="__dataset_accessor",
                           fields=OrderedMap()))
                dataset_accessors[k] = DatasetAccessor(snaps)
        for k, tbl in external.series_tables.items():
            series_by_alias.setdefault(k, tbl)

    # == Declaration processing (pre-loop, outside the main try) =========
    decl = program.declaration
    if len(decl.args) > 0:
        try:
            decl_title = rt.eval_expr(decl.args[0], 0)
        except BrueError as e:
            return execute_throw_output(e)
    else:
        decl_title = Value.str("Untitled")
    overlay = Value.Bool(True)
    for kw in decl.kwargs:
        if kw.name == "overlay":
            try:
                overlay = rt.eval_expr(kw.value, 0)
            except BrueError as e:
                return execute_throw_output(e)
    rt.overlay = overlay

    # Strategy engine init (decl.mode is always 'strategy')
    config = StrategyConfig()
    for kw in decl.kwargs:
        try:
            val = rt.eval_expr(kw.value, 0)
        except BrueError as e:
            return execute_throw_output(e)
        if kw.name == "capital" or kw.name == "initial_capital":
            config.capital = val.js_to_number()
        elif kw.name == "commission":
            config.commission = val.js_to_number()
        elif kw.name == "commission_type":
            config.commission_type = val.js_string()
        elif kw.name == "slippage":
            config.slippage = val.js_to_number()
        elif kw.name == "pyramiding":
            config.pyramiding = val.js_to_number()
        elif kw.name == "default_qty":
            config.default_qty = val.js_to_number()
        elif kw.name == "default_qty_type":
            # default_qty_type: "contracts" and "fixed" both mean raw units;
            # StrategyConfig.qty_type documents the other values.
            config.qty_type = val.js_string()
        elif kw.name == "currency":
            config.currency = val.js_string()
    # Run-call overrides beat header values; headerless scripts have no
    # header, so for them these ARE the only source (engine defaults otherwise).
    if options.capital is not None:
        config.capital = float(options.capital)
    if options.commission is not None:
        config.commission = float(options.commission)
    if options.commission_type is not None:
        config.commission_type = str(options.commission_type)
    if options.slippage is not None:
        config.slippage = float(options.slippage)
    if rt.v2:
        # Headerless scripts never second-guess an explicit order: there is
        # no pyramiding knob, so same-direction entries stack (normal
        # netting). A script that wants a cap writes the cap in its own logic.
        config.pyramiding = math.inf
    rt.engine = StrategyEngine(config, bar_count)

    # == barHistory pre-scan =============================================
    history_names = set()
    scan_history_idents_stmts(program.body, history_names)
    for n in history_names:
        rt.bar_history[n] = []

    # == Main loop =======================================================
    # Pre-register top-level function definitions
    for stmt in program.body:
        if isinstance(stmt, ast.FunctionDef):
            rt.scopes[0].functions[stmt.name] = FunctionData(params=list(stmt.params), body=list(stmt.body))

    try:
        for bar in range(bar_count):
            engine = rt.engine
            if engine is not None:
                engine.process_pending_orders(bar, rt.opens[bar], rt.times[bar])
                engine.check_pending_stop_limits(bar, rt.highs[bar], rt.lows[bar], rt.times[bar])
                # Past the trade window: flatten whatever is still open
                # (fills at the next bar's open like any market close).
                # Re-issued while a position remains rather than one-shot: a
                # NaN open on the fill bar clears pending orders (base
                # semantics), and a single flag would have let the position
                # ride to the end of data. Requeueing is idempotent because
                # process_pending_orders skips entries already closed.
                if options.trade_to is not None:
                    if rt.times[bar] > options.trade_to and engine.has_open_entries():
                        engine.close_all(bar, rt.closes[bar], rt.times[bar])
            rt.input_counter = 0
            for stmt in program.body:
                if isinstance(stmt, ast.FunctionDef):
                    continue
                rt.exec_stmt(stmt, bar)
            if rt.bar_history:
                names = list(rt.bar_history.keys())
                for nm in names:
                    try:
                        v = rt.resolve_identifier(nm, bar, Loc(line=0, col=0))
                    except BrueError:
                        v = Value.Null
                    rt.bar_history[nm].append(v)
            if rt.engine is not None:
                rt.engine.update_equity(bar, rt.closes[bar])
    except BrueError as e:
        rt.errors.append(ErrorInfo(
            message=e.message,
            line=e.loc.line,
            col=e.loc.col,
            phase="runtime",
            suggestion=e.suggestion,
        ))

    captured_intents = []
    if bar_count > 0:
        engine = rt.engine
        if engine is not None:
            if options.capture_intents:
                # Harvest final-bar decisions BEFORE finalize would fill them
                # at the last close. Brackets attached via deferred exits are
                # carried on the entry intent so the connector can place
                # sl/tp with the order.
                deferred = {k: d for (k, d) in engine.deferred_exits}
                for o in engine.pending_orders:
                    if isinstance(o, strategy.PendingOrder.Entry):
                        d = deferred.get(o.id)
                        captured_intents.append({
                            "kind": "entry", "id": o.id,
                            "direction": o.direction, "qty": o.qty,
                            "bracket": None if d is None else {
                                "stop_price": d.stop_price,
                                "limit_price": d.limit_price,
                                "profit_points": d.profit_points,
                                "loss_points": d.loss_points,
                            }})
                    else:
                        captured_intents.append({
                            "kind": "close", "entry_id": o.entry_ref.id,
                            "exit_id": o.exit_id, "qty": o.qty})
                engine.pending_orders = []
            engine.finalize(
                bar_count - 1,
                rt.closes[bar_count - 1],
                rt.times[bar_count - 1],
            )
            for tc in engine.take_trade_commands():
                rt.commands.append(trade_command_to_output(tc))

    out = ExecOutput(
        success=len(rt.errors) == 0,
        commands=rt.commands,
        errors=rt.errors,
        inputs=rt.inputs,
        live_orders=rt.live_orders,
        live_closes=rt.live_closes,
        decl_title=decl_title,
        decl_overlay=overlay,
        decl_symbol=decl.symbol,
        decl_timeframe=decl.timeframe,
        logs=rt.logs,
    )
    if options.collect_plots:
        # Host-extension side channel: never serialized into to_json(), so
        # canonical output is unchanged.
        out.plots = rt.plots
        out.plot_meta = rt.plot_meta
    out.intents = captured_intents
    return out


def execute_throw_output(e):
    """An error thrown OUTSIDE the main try (declaration processing),
    wrapped into a failed ExecOutput so the host sees one result shape."""
    return ExecOutput(
        success=False,
        commands=[],
        errors=[ErrorInfo(
            message=e.message,
            line=e.loc.line,
            col=e.loc.col,
            phase="runtime",
            suggestion=e.suggestion,
        )],
        inputs=[],
        live_orders=[],
        live_closes=[],
        decl_title=Value.Null,
        decl_overlay=Value.Bool(True),
        decl_symbol=None,
        decl_timeframe=None,
    )


def scan_history_idents_expr(e, out):
    if isinstance(e, ast.History):
        source = e.source
        if isinstance(source, ast.Identifier):
            if source.name not in PRICE_SERIES_NAMES:
                out.add(source.name)
        scan_history_idents_expr(e.source, out)
        scan_history_idents_expr(e.offset, out)
        return
    if isinstance(e, ast.Binary):
        scan_history_idents_expr(e.left, out)
        scan_history_idents_expr(e.right, out)
    elif isinstance(e, ast.Unary):
        scan_history_idents_expr(e.operand, out)
    elif isinstance(e, ast.Call):
        for a in e.args:
            scan_history_idents_expr(a, out)
        for kw in e.kwargs:
            scan_history_idents_expr(kw.value, out)
    elif isinstance(e, ast.Ternary):
        scan_history_idents_expr(e.condition, out)
        scan_history_idents_expr(e.if_true, out)
        scan_history_idents_expr(e.if_false, out)
    elif isinstance(e, ast.ArrayLiteral):
        for el in e.elements:
            scan_history_idents_expr(el, out)
    elif isinstance(e, ast.MapLiteral):
        for (k, v) in e.entries:
            scan_history_idents_expr(k, out)
            scan_history_idents_expr(v, out)
    elif isinstance(e, ast.Member):
        scan_history_idents_expr(e.object, out)
    elif isinstance(e, ast.MethodCall):
        scan_history_idents_expr(e.object, out)
        for a in e.args:
            scan_history_idents_expr(a, out)
    elif isinstance(e, ast.FormatString):
        for p in e.parts:
            if isinstance(p, ast.FmtPart.Expr):
                scan_history_idents_expr(p.expr, out)


def scan_history_idents_stmts(stmts, out):
    for s in stmts:
        if isinstance(s, (ast.Assign, ast.Persist, ast.Const)):
            scan_history_idents_expr(s.value, out)
        elif isinstance(s, ast.MultiAssign):
            scan_history_idents_expr(s.value, out)
        elif isinstance(s, ast.ExprStmt):
            scan_history_idents_expr(s.expr, out)
        elif isinstance(s, ast.MemberAssign):
            scan_history_idents_expr(s.target, out)
            scan_history_idents_expr(s.value, out)
        elif isinstance(s, ast.Return):
            if s.value is not None:
                scan_history_idents_expr(s.value, out)
        elif isinstance(s, ast.If):
            scan_history_idents_expr(s.condition, out)
            scan_history_idents_stmts(s.body, out)
            for (c, b) in s.elifs:
                scan_history_idents_expr(c, out)
                scan_history_idents_stmts(b, out)
            scan_history_idents_stmts(s.else_body, out)
        elif isinstance(s, ast.For):
            scan_history_idents_expr(s.iterable, out)
            scan_history_idents_stmts(s.body, out)
        elif isinstance(s, ast.While):
            scan_history_idents_expr(s.condition, out)
            scan_history_idents_stmts(s.body, out)
        elif isinstance(s, ast.FunctionDef):
            scan_history_idents_stmts(s.body, out)
        elif isinstance(s, ast.TypeDef):
            for f in s.fields:
                if f.default_value is not None:
                    scan_history_idents_expr(f.default_value, out)


# Sentinel appended by the scanner whenever ANY order verb call appears,
# literal symbol or not: it separates trading scripts (total strictness)
# from indicator scripts (the chart is the context).
_HAS_ORDERS = "\x00orders"

# Timeframe chain tokens: m5, m90, h1, h4, d1... A qualified symbol
# reference always names its cadence (EURUSD.h1.close); the feed key is
# the chain joined by hyphens (EURUSD-h1, XAUUSD-mybroker-h4).
_TF_RE = re.compile(r"^(m|h|d)[1-9]\d*$")


def is_tf_token(name):
    return bool(_TF_RE.match(name))


def derive_tf_token(bars):
    """The canonical timeframe token of a bar feed, from its median
    spacing. The feed's actual cadence is the truth a declared token is
    validated against; median beats mean because session gaps are normal."""
    ts = [b.time for b in bars[:200]]
    ds = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if not ds:
        return None
    mins = round(ds[len(ds) // 2] / 60000.0)
    if mins <= 0:
        return None
    if mins % 1440 == 0:
        return "d{}".format(mins // 1440)
    if mins % 60 == 0:
        return "h{}".format(mins // 60)
    return "m{}".format(mins)


def scan_order_markets_expr(e, out):
    """Collect every string-literal market named by an order verb.
    Structural on purpose: what a script trades must be readable from its
    text alone, so only literal symbols count; a computed symbol is
    rejected at call time."""
    if isinstance(e, ast.Call):
        if e.callee in ("buy", "sell", "close"):
            # The bare verb name marks a TRADING script even before its
            # symbol is validated; the strictness gates key off this.
            out.append(_HAS_ORDERS)
            if len(e.args) > 0 and isinstance(e.args[0], ast.StringLiteral):
                out.append(e.args[0].value)
        for a in e.args:
            scan_order_markets_expr(a, out)
        for kw in e.kwargs:
            scan_order_markets_expr(kw.value, out)
    elif isinstance(e, ast.Binary):
        scan_order_markets_expr(e.left, out)
        scan_order_markets_expr(e.right, out)
    elif isinstance(e, ast.Unary):
        scan_order_markets_expr(e.operand, out)
    elif isinstance(e, ast.Ternary):
        scan_order_markets_expr(e.condition, out)
        scan_order_markets_expr(e.if_true, out)
        scan_order_markets_expr(e.if_false, out)
    elif isinstance(e, ast.ArrayLiteral):
        for el in e.elements:
            scan_order_markets_expr(el, out)
    elif isinstance(e, ast.MapLiteral):
        for (k, v) in e.entries:
            scan_order_markets_expr(k, out)
            scan_order_markets_expr(v, out)
    elif isinstance(e, ast.Member):
        scan_order_markets_expr(e.object, out)
    elif isinstance(e, ast.MethodCall):
        scan_order_markets_expr(e.object, out)
        for a in e.args:
            scan_order_markets_expr(a, out)
    elif isinstance(e, ast.History):
        scan_order_markets_expr(e.source, out)
        scan_order_markets_expr(e.offset, out)
    elif isinstance(e, ast.FormatString):
        for p in e.parts:
            if isinstance(p, ast.FmtPart.Expr):
                scan_order_markets_expr(p.expr, out)


def scan_order_markets_stmts(stmts, out):
    for s in stmts:
        if isinstance(s, (ast.Assign, ast.Persist, ast.Const)):
            scan_order_markets_expr(s.value, out)
        elif isinstance(s, ast.MultiAssign):
            scan_order_markets_expr(s.value, out)
        elif isinstance(s, ast.ExprStmt):
            scan_order_markets_expr(s.expr, out)
        elif isinstance(s, ast.MemberAssign):
            scan_order_markets_expr(s.target, out)
            scan_order_markets_expr(s.value, out)
        elif isinstance(s, ast.Return):
            if s.value is not None:
                scan_order_markets_expr(s.value, out)
        elif isinstance(s, ast.If):
            scan_order_markets_expr(s.condition, out)
            scan_order_markets_stmts(s.body, out)
            for (c, b) in s.elifs:
                scan_order_markets_expr(c, out)
                scan_order_markets_stmts(b, out)
            scan_order_markets_stmts(s.else_body, out)
        elif isinstance(s, ast.For):
            scan_order_markets_expr(s.iterable, out)
            scan_order_markets_stmts(s.body, out)
        elif isinstance(s, ast.While):
            scan_order_markets_expr(s.condition, out)
            scan_order_markets_stmts(s.body, out)
        elif isinstance(s, ast.FunctionDef):
            scan_order_markets_stmts(s.body, out)


class Rt:
    def __init__(
        self,
        program,
        bar_count,
        opens,
        highs,
        lows,
        closes,
        volumes,
        times,
        hl2,
        hlc3,
        ohlc4,
        named_colors,
        use_by_alias,
        dataset_aliases,
        dataset_accessors,
        aligned_use,
        series_by_alias,
        input_overrides,
        run_options,
    ):
        self.program = program
        self.bar_count = bar_count
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.volumes = volumes
        self.times = times
        self.hl2 = hl2
        self.hlc3 = hlc3
        self.ohlc4 = ohlc4
        self.named_colors = named_colors
        self.use_by_alias = use_by_alias
        self.dataset_aliases = dataset_aliases
        self.dataset_accessors = dataset_accessors
        self.aligned_use = aligned_use
        self.series_by_alias = series_by_alias
        self.table_column_cache = {}
        self.foreign_series_cache = {}
        self.commands = []
        # collect_plots host extension: plot-call-site key to list of
        # (bar_index, float value); kwargs of first sighting kept per key.
        self.plots = {}
        self.plot_meta = {}
        self._plot_calls_this_bar = 0
        self._plot_bar = -1
        # Unique-id sequence for buy()/sell() desugar so each call's bracket
        # (its sl/tp) isolates to that entry.
        self._order_seq = 0
        self.errors = []
        self.inputs = []
        self.live_orders = []
        self.live_closes = []
        # ctx.cacheSeries store (used by the sma builtin)
        self.series_cache = {}
        self.scopes = [Scope()]
        self.current = 0
        self.plot_counter = 0
        self.input_counter = 0
        self.enum_defs = {}
        self.type_defs = {}
        self.engine = None
        self.input_overrides = input_overrides
        self.run_options = run_options
        # table handle to index into commands
        self.table_commands = {}
        # cacheKey__tuple to tuple series (the key shape makes collisions
        # with user variables impossible)
        self.tuple_cache = {}
        self.bar_history = {}
        self.call_depth = 0
        self.overlay = Value.Bool(True)

    # == Fingerprint =====================================================
    def fingerprint(self, expr):
        if isinstance(expr, ast.NumberLiteral):
            return "n" + js_number_to_string(expr.value)
        if isinstance(expr, ast.StringLiteral):
            return "s" + expr.value
        if isinstance(expr, ast.BoolLiteral):
            return "btrue" if expr.value else "bfalse"
        if isinstance(expr, ast.Identifier):
            name = expr.name
            scope = self.scopes[self.current]
            bound = None
            if scope.bound_arg_exprs is not None:
                bound = scope.bound_arg_exprs.get(name)
            if bound is not None:
                bv = scope.bar_vars.get(name)
                if bv is not None and bv.kind == "num":
                    scalar = "=" + js_number_to_string(bv.num_or_nan())
                elif bv is not None and bv.kind == "str":
                    scalar = "=" + bv.js_string()
                elif bv is not None and bv.kind == "bool":
                    scalar = "=true" if bv.is_truthy() else "=false"
                else:
                    scalar = ""
                (bexpr, bscope) = bound
                # fingerprint the bound expr in the BOUND scope (read-only;
                # the scope switch is a save/restore of self.current).
                saved = self.current
                self.current = bscope
                fp = self.fingerprint(bexpr)
                self.current = saved
                return "arg_{}{}".format(fp, scalar)
            return "id_" + name
        if isinstance(expr, ast.Call):
            parts = [self.fingerprint(a) for a in expr.args]
            return "call_{}_{}".format(expr.callee, "_".join(parts))
        loc = expr.loc
        return "{}_{}_{}".format(expr.kind_str(), loc.line, loc.col)

    # == Foreign fields (use aliases) ====================================
    # Foreign field read: per (alias, field) float vector materialized once
    # from the aligned bars; every slot NaN when the host supplied no data
    # for the alias (same as an unset variable).
    def get_foreign_field(self, alias, fld):
        key = "{}:{}".format(alias, fld)
        c = self.foreign_series_cache.get(key)
        if c is not None:
            return c
        if alias not in self.use_by_alias:
            return None
        aligned = self.aligned_use.get(alias)
        if aligned is None:
            arr = [NAN] * self.bar_count
        else:
            arr = []
            for i in range(self.bar_count):
                b = aligned[i]
                if b is None:
                    arr.append(NAN)
                elif fld == "open":
                    arr.append(b.open)
                elif fld == "high":
                    arr.append(b.high)
                elif fld == "low":
                    arr.append(b.low)
                elif fld == "close":
                    arr.append(b.close)
                elif fld == "volume":
                    arr.append(b.volume)
                elif fld == "hl2":
                    arr.append((b.high + b.low) / 2.0)
                elif fld == "hlc3":
                    arr.append((b.high + b.low + b.close) / 3.0)
                elif fld == "ohlc4":
                    arr.append((b.open + b.high + b.low + b.close) / 4.0)
                else:
                    arr.append(NAN)
        self.foreign_series_cache[key] = arr
        return arr

    # == Series-table columns ============================================
    # As-of forward-fill of one table column onto the bar clock, cached per
    # (alias, column). NaN before the table's first row, same null-before-
    # first rule as align_as_of. Returns None when the column doesn't exist
    # (caller decides whether that's an error or an empty source).
    def get_table_column(self, alias, column):
        key = "{}:{}".format(alias, column)
        c = self.table_column_cache.get(key)
        if c is not None:
            return c
        table = self.series_by_alias.get(alias)
        if table is None:
            return None
        col = None
        for (n, values) in table.columns:
            if n == column:
                col = values
                break
        if col is None:
            return None
        out = []
        f_idx = 0
        last = NAN
        for t in self.times:
            while f_idx < len(table.times) and table.times[f_idx] <= t:
                last = col[f_idx]
                f_idx += 1
            out.append(last)
        self.table_column_cache[key] = out
        return out

    # == resolveSource ===================================================
    def resolve_source(self, expr, bar):
        if isinstance(expr, ast.Member):
            obj = expr.object
            if isinstance(obj, ast.Identifier):
                name = obj.name
                # Series-table column as a source: sma(funding.rate, 5).
                # Unknown columns become an empty source, the same contract
                # as unknown foreign fields.
                if name in self.series_by_alias:
                    col = self.get_table_column(name, expr.member)
                    return list(col) if col is not None else []
                if name in self.use_by_alias:
                    if expr.member not in FOREIGN_FIELDS:
                        return []
                    f = self.get_foreign_field(name, expr.member)
                    return list(f) if f is not None else []
                if self.v2 and expr.member in FOREIGN_FIELDS \
                        and not self._is_scope_name(name) \
                        and name not in ("close", "open", "high", "low",
                                         "volume", "hl2", "hlc3", "ohlc4",
                                         "time"):
                    return self._read_symbol_chain(
                        [name], expr.member,
                        getattr(expr, "loc", Loc(line=0, col=0)), 0,
                        scalar=False)
            if self.v2 and isinstance(obj, ast.Member) \
                    and expr.member in FOREIGN_FIELDS:
                chain = self._symbol_chain(obj)
                if chain is not None:
                    # ema(XAUUSD.mybroker.h4.close, 3) reads feed
                    # "XAUUSD-mybroker-h4" as an indicator source.
                    return self._read_symbol_chain(
                        chain, expr.member,
                        getattr(expr, "loc", Loc(line=0, col=0)), 0,
                        scalar=False)

        if isinstance(expr, ast.Identifier):
            name = expr.name
            if self.v2 and self.v2_trading \
                    and name in ("close", "open", "high", "low", "volume",
                                 "hl2", "hlc3", "ohlc4"):
                raise BrueError.runtime(
                    "bare '{}' does not exist in a trading script: every "
                    "series names its market.".format(name),
                    getattr(expr, "loc", Loc(line=0, col=0)),
                    "Write SYMBOL.{0} (e.g. EURUSD.{0}).".format(name))
            if name == "close":
                return list(self.closes)
            if name == "open":
                return list(self.opens)
            if name == "high":
                return list(self.highs)
            if name == "low":
                return list(self.lows)
            if name == "volume":
                return list(self.volumes)
            if name == "hl2":
                return list(self.hl2)
            if name == "hlc3":
                return list(self.hlc3)
            if name == "ohlc4":
                return list(self.ohlc4)
            if name == "time":
                return list(self.times)
            # series vars: current scope then global
            for scope_idx in (self.current, 0):
                s = self.scopes[scope_idx].series_vars.get(name)
                if s is not None:
                    if isinstance(s, StoredSeries.Nums):
                        return list(s.value)
                    return [NAN] * len(s.value)
                if scope_idx == 0:
                    break
            # bound arg exprs
            bound = None
            if self.scopes[self.current].bound_arg_exprs is not None:
                bound = self.scopes[self.current].bound_arg_exprs.get(name)
            if bound is not None:
                (bexpr, bscope) = bound
                saved = self.current
                self.current = bscope
                try:
                    return self.resolve_source(bexpr, bar)
                finally:
                    self.current = saved
            # per-bar computed variable as source: fail loud
            cur = self.scopes[self.current]
            glob = self.scopes[0]
            if (name in cur.bar_vars or name in glob.bar_vars
                    or name in cur.persist_vars or name in glob.persist_vars):
                raise BrueError.runtime(
                    "'{}' is a per-bar computed variable and cannot be used as a series source.".format(name),
                    expr.loc,
                    "Inline its expression instead: write e.g. zscore(close - open, 60), not s = close - open; zscore(s, 60).",
                )

        if isinstance(expr, ast.Call):
            series_result = self.eval_series_call_simple(expr, bar)
            if series_result is not None:
                if len(series_result) > 0:
                    return series_result
            fallback = []
            for i in range(self.bar_count):
                v = self.eval_expr(expr, i)
                if v.kind == "num":
                    fallback.append(v.num_or_nan())
                elif v.kind == "bool":
                    fallback.append(1.0 if v.is_truthy() else 0.0)
                else:
                    fallback.append(NAN)
            return fallback

        result = []
        for i in range(self.bar_count):
            v = self.eval_expr(expr, i)
            if v.kind == "num":
                result.append(v.num_or_nan())
            elif v.kind == "bool":
                result.append(1.0 if v.is_truthy() else 0.0)
            else:
                result.append(NAN)
        return result

    def eval_series_call_simple(self, expr, _bar):
        """evalSeriesCall (the resolveSource helper): positional args only,
        defaults appended for the tail, no required-arg assertion.
        Returns the series (list) or None."""
        if not isinstance(expr, ast.Call):
            return None
        callee = expr.callee
        args = expr.args
        builtin = get_builtin(callee)
        if builtin is None or not builtin.series_level:
            return None
        resolved = []
        for (i, a) in enumerate(args):
            param = builtin.params[i] if i < len(builtin.params) else None
            if param is not None and param.ptype == ParamType.Source:
                resolved.append(ArgVal.Source(self.resolve_source(a, 0)))
                continue
            resolved.append(ArgVal.Val(self.eval_expr(a, 0)))
        start = len(resolved)
        for i in range(start, len(builtin.params)):
            d = builtin.params[i].default.to_value()
            if d is not None:
                resolved.append(ArgVal.Val(d))
        result = self.run_builtin(builtin, resolved, 0)
        if result.kind == "series" and builtin.return_count == 1:
            return result.value
        return None

    def run_builtin(self, builtin, args, bar):
        ctx = Ctx(
            bar_index=bar,
            bar_count=self.bar_count,
            opens=self.opens,
            highs=self.highs,
            lows=self.lows,
            closes=self.closes,
            volumes=self.volumes,
            times=self.times,
            cache=self.series_cache,
        )
        return builtin.execute(args, ctx)

    # == orderBuiltinArgs + assertRequiredArgs ===========================
    def order_builtin_args(self, builtin, args, kwargs, loc):
        slots = list(args)
        if len(kwargs) == 0:
            return slots
        for kw in kwargs:
            idx = None
            for (i, p) in enumerate(builtin.params):
                if p.name == kw.name:
                    idx = i
                    break
            if idx is None:
                names = [p.name for p in builtin.params]
                raise BrueError.runtime(
                    "{}() has no parameter '{}'.".format(builtin.name, kw.name),
                    loc,
                    "Parameters: {}.".format(", ".join(names)),
                )
            while len(slots) <= idx:
                slots.append(None)
            if slots[idx] is not None:
                raise BrueError.runtime(
                    "{}() got two values for parameter '{}'.".format(builtin.name, kw.name),
                    loc,
                    "Remove the duplicate argument.",
                )
            slots[idx] = kw.value
        return slots

    def assert_required_args(self, builtin, args, loc):
        for (i, p) in enumerate(builtin.params):
            missing = i >= len(args) or args[i].is_missing()
            if p.required and missing:
                sig = []
                for q in builtin.params:
                    if q.required:
                        sig.append(q.name)
                    else:
                        sig.append("{}={}".format(q.name, q.default.json()))
                raise BrueError.runtime(
                    "{}() is missing required argument '{}'.".format(builtin.name, p.name),
                    loc,
                    "Signature: {}({}).".format(builtin.name, ", ".join(sig)),
                )

    def eval_series_call_from_expr(self, callee, args, kwargs, loc):
        """evalSeriesCallFromExpr: kwarg-ordered slots, defaults, assertion.
        Returns an ExecResult or None."""
        builtin = get_builtin(callee)
        if builtin is None or not builtin.series_level:
            return None
        slots = self.order_builtin_args(builtin, args, kwargs, loc)
        n = max(len(slots), len(builtin.params))
        resolved = []
        for i in range(n):
            param = builtin.params[i] if i < len(builtin.params) else None
            arg_expr = slots[i] if i < len(slots) else None
            if arg_expr is not None:
                if param is not None and param.ptype == ParamType.Source:
                    resolved.append(ArgVal.Source(self.resolve_source(arg_expr, 0)))
                    continue
                resolved.append(ArgVal.Val(self.eval_expr(arg_expr, 0)))
            else:
                if param is not None:
                    d = param.default.to_value()
                    if d is not None:
                        resolved.append(ArgVal.Val(d))
                    else:
                        if i < len(builtin.params):
                            resolved.append(ArgVal.Missing)
        self.assert_required_args(builtin, resolved, loc)
        return self.run_builtin(builtin, resolved, 0)

    # == resolveIdentifier ===============================================
    def resolve_identifier(self, name, bar, loc):
        if self.v2:
            if self.v2_trading and name in ("close", "open", "high", "low",
                                            "volume", "hl2", "hlc3", "ohlc4"):
                raise BrueError.runtime(
                    "bare '{}' does not exist in a trading script: every "
                    "series names its market.".format(name),
                    loc, "Write SYMBOL.{0} (e.g. EURUSD.{0}).".format(name))
            if name in ("bar_index", "last_bar", "last_bar_index",
                        "is_first_bar", "is_realtime", "is_new_bar",
                        "long", "short"):
                raise BrueError.runtime(
                    "'{}' is not part of the language: there is no final "
                    "bar and no chart in live trading.".format(name),
                    loc, "The clock is time; positions are read from the "
                         "account, not inferred from bar counters.")
        if name == "close":
            return Value.Num(self.closes[bar])
        if name == "open":
            return Value.Num(self.opens[bar])
        if name == "high":
            return Value.Num(self.highs[bar])
        if name == "low":
            return Value.Num(self.lows[bar])
        if name == "volume":
            return Value.Num(self.volumes[bar])
        if name == "hl2":
            return Value.Num(self.hl2[bar])
        if name == "hlc3":
            return Value.Num(self.hlc3[bar])
        if name == "ohlc4":
            return Value.Num(self.ohlc4[bar])
        if name == "time":
            return Value.Num(self.times[bar])
        if name == "bar_index":
            return Value.Num(float(bar))
        if name == "last_bar":
            return Value.Bool(bar == self.bar_count - 1)
        if name == "last_bar_index":
            return Value.Num(float(self.bar_count) - 1.0)
        if name == "is_first_bar":
            return Value.Bool(bar == 0)
        if name == "is_realtime":
            return Value.Bool(bar == self.bar_count - 1)
        if name == "is_new_bar":
            return Value.Bool(True)
        if name == "long":
            return Value.str("long")
        if name == "short":
            return Value.str("short")

        # Scope lookup: current, then global
        scope = self.scopes[self.current]
        if name in scope.constants:
            return scope.constants[name]
        if name in scope.persist_vars:
            return scope.persist_vars[name]
        if name in scope.bar_vars:
            return scope.bar_vars[name]
        s = scope.series_vars.get(name)
        if s is not None:
            return s.at(bar) if bar < s.len() else Value.Null
        if self.current != 0:
            scope = self.scopes[0]
            if name in scope.constants:
                return scope.constants[name]
            if name in scope.persist_vars:
                return scope.persist_vars[name]
            if name in scope.bar_vars:
                return scope.bar_vars[name]
            s = scope.series_vars.get(name)
            if s is not None:
                return s.at(bar) if bar < s.len() else Value.Null

        if name in self.dataset_aliases:
            return self.dataset_aliases[name]

        # Series-table alias used bare: same precise-error
        # philosophy as the foreign-symbol case below, with the table's own
        # columns in the suggestion.
        table = self.series_by_alias.get(name)
        if table is not None:
            cols = [n for (n, _) in table.columns]
            raise BrueError.runtime(
                "'{}' refers to a data table; you need to specify a column.".format(name),
                loc,
                "Try {}.{}.".format(name, cols[0] if cols else "<column>"),
            )

        if name in self.use_by_alias:
            raise BrueError.runtime(
                "'{}' refers to a foreign symbol; you need to specify a field.".format(name),
                loc,
                "Try {}.close (or .open/.high/.low/.volume/.hl2/.hlc3/.ohlc4).".format(name),
            )

        if name in self.named_colors:
            return self.named_colors[name]

        # Unknown identifier: LOUD. A silent na here would mean one
        # misspelled letter in a condition reads na (= false) and the
        # strategy trades on nothing, with no error anywhere. A typo changes
        # trading behavior, so by the loud-or-quiet law it must fail loudly;
        # quiet na stays reserved for legitimately absent DATA (history
        # before the start, unfed feeds, missing keys), never for unknown
        # NAMES.
        candidates = [
            "close", "open", "high", "low", "volume", "hl2", "hlc3",
            "ohlc4", "time", "bar_index", "last_bar", "last_bar_index",
            "is_first_bar", "is_realtime", "is_new_bar", "long", "short",
        ]
        for si in ([self.current, 0] if self.current != 0 else [0]):
            sc = self.scopes[si]
            candidates.extend(sc.constants.keys())
            candidates.extend(sc.persist_vars.keys())
            candidates.extend(sc.bar_vars.keys())
            candidates.extend(sc.series_vars.keys())
        candidates.extend(self.dataset_aliases.keys())
        candidates.extend(self.series_by_alias.keys())
        candidates.extend(self.use_by_alias.keys())
        candidates.extend(self.named_colors.keys())
        near = errors.find_closest_match(name, candidates)
        raise BrueError.runtime(
            "unknown identifier '{}'.".format(name), loc,
            "Did you mean '{}'?".format(near) if near else None)

    # == Expression evaluation ===========================================
    def eval_expr(self, expr, bar):
        if isinstance(expr, ast.NumberLiteral):
            return Value.Num(expr.value)
        if isinstance(expr, ast.StringLiteral):
            return Value.str(expr.value)
        if isinstance(expr, ast.BoolLiteral):
            return Value.Bool(expr.value)
        if isinstance(expr, ast.NaLiteral):
            return Value.Null
        if isinstance(expr, ast.ColorLiteral):
            return Value.Color(ColorVal(r=expr.r, g=expr.g, b=expr.b, a=expr.a))
        if isinstance(expr, ast.Identifier):
            return self.resolve_identifier(expr.name, bar, expr.loc)
        if isinstance(expr, ast.Binary):
            return self.eval_binary(expr.op, expr.left, expr.right, bar)
        if isinstance(expr, ast.Unary):
            v = self.eval_expr(expr.operand, bar)
            if expr.op == "-":
                return Value.Num(-v.num_or_nan() if v.kind == "num" else NAN)
            if expr.op == "not":
                return Value.Bool(not v.is_truthy())
            return Value.Null
        if isinstance(expr, ast.Call):
            return self.eval_call(expr.callee, expr.args, expr.kwargs, expr.loc, bar)
        if isinstance(expr, ast.History):
            return self.eval_history(expr.source, expr.offset, bar)
        if isinstance(expr, ast.Ternary):
            c = self.eval_expr(expr.condition, bar)
            if c.is_truthy():
                return self.eval_expr(expr.if_true, bar)
            return self.eval_expr(expr.if_false, bar)
        if isinstance(expr, ast.ArrayLiteral):
            vals = []
            for e in expr.elements:
                vals.append(self.eval_expr(e, bar))
            return Value.array(vals)
        if isinstance(expr, ast.MapLiteral):
            m = OrderedMap()
            for (k, v) in expr.entries:
                if isinstance(k, ast.Identifier):
                    key = k.name
                else:
                    key = self.eval_expr(k, bar).js_string()
                val = self.eval_expr(v, bar)
                m.set(key, val)
            return Value.Map(m)
        if isinstance(expr, ast.Member):
            return self.eval_member(expr.object, expr.member, expr.loc, bar)
        if isinstance(expr, ast.MethodCall):
            return self.eval_method_call(expr.object, expr.method, expr.args, expr.loc, bar)
        if isinstance(expr, ast.FormatString):
            result = []
            for (i, part) in enumerate(expr.parts):
                if isinstance(part, ast.FmtPart.Text):
                    result.append(part.value)
                elif isinstance(part, ast.FmtPart.Expr):
                    val = self.eval_expr(part.expr, bar)
                    spec = expr.specs[i] if i < len(expr.specs) else None
                    if spec is not None and val.kind == "num":
                        # spec is ".Nf": strip the prefix and suffix, parse N
                        # as an unsigned int (ASCII digits, optional leading '+').
                        digits = None
                        if spec.startswith(".") and spec.endswith("f") and len(spec) >= 2:
                            mid = spec[1:-1]
                            t = mid[1:] if mid.startswith("+") else mid
                            if t and all("0" <= ch <= "9" for ch in t):
                                digits = int(t)
                        if digits is not None:
                            result.append(js_to_fixed(val.num_or_nan(), digits))
                        else:
                            result.append(val.js_string_or_na())
                    else:
                        result.append(val.js_string_or_na())
            return Value.str("".join(result))
        return Value.Null

    def eval_binary(self, op, left, right, bar):
        l = self.eval_expr(left, bar)
        r = self.eval_expr(right, bar)
        ln = l.num_or_nan()
        rn = r.num_or_nan()
        if op == "+":
            if l.kind == "str" or r.kind == "str":
                return Value.str(l.js_string_or_na() + r.js_string_or_na())
            return Value.Num(ln + rn)
        if op == "-":
            return Value.Num(ln - rn)
        if op == "*":
            return Value.Num(ln * rn)
        if op == "/":
            return Value.Num(NAN if rn == 0.0 else ln / rn)
        if op == "%":
            # Brue's % is fmod (the result takes the sign of the dividend);
            # Python's % is floored modulo, so math.fmod is the right call.
            return Value.Num(NAN if rn == 0.0 else math.fmod(ln, rn))
        if op == "**":
            return Value.Num(jsmath.js_pow(ln, rn))
        if op == "==":
            return Value.Bool(l.strict_eq(r) or (l.is_na() and r.is_na()))
        if op == "!=":
            return Value.Bool(not l.strict_eq(r) and not (l.is_na() and r.is_na()))
        if op == ">":
            return Value.Bool(ln > rn)
        if op == "<":
            return Value.Bool(ln < rn)
        if op == ">=":
            return Value.Bool(ln >= rn)
        if op == "<=":
            return Value.Bool(ln <= rn)
        if op == "and":
            # Both sides were already evaluated above: Brue's and/or are
            # deliberately non-short-circuiting.
            return Value.Bool(l.is_truthy() and r.is_truthy())
        if op == "or":
            return Value.Bool(l.is_truthy() or r.is_truthy())
        return Value.Null

    def eval_history(self, source, offset, bar):
        off_val = self.eval_expr(offset, bar)
        if off_val.kind == "str":
            key = off_val.js_string()
            src = self.eval_expr(source, bar)
            if src.kind == "map":
                b = src.v
                if b.has(key):
                    got = b.get(key)
                    return got if got is not None else Value.Null
                return Value.Null
            if src.kind == "udt":
                got = src.v.fields.get(key)
                return got if got is not None else Value.Null
            return Value.Null
        if not off_val.kind == "num":
            return Value.Null
        off = off_val.num_or_nan()
        if off < 0.0:
            return Value.Null
        # Non-integer offsets are rejected: fmod(off, 1.0) is NaN for NaN/inf
        # inputs and NaN != 0.0 is true, so those are rejected too.
        if math.fmod(off, 1.0) != 0.0:
            return Value.Null
        off_u = int(off)
        if off_u > 0:
            if isinstance(source, ast.Identifier):
                name = source.name
                is_series = (name in self.scopes[self.current].series_vars
                             or name in self.scopes[0].series_vars)
                if (name not in PRICE_SERIES_NAMES
                        and not is_series
                        and name in self.bar_history):
                    hist = self.bar_history[name]
                    if bar >= off_u:
                        idx = bar - off_u
                        if idx < len(hist):
                            return hist[idx]
                    return Value.Null
        if off_u > bar:
            return Value.Null
        return self.eval_expr(source, bar - off_u)

    def eval_member(self, obj_expr, member, loc, bar):
        if isinstance(obj_expr, ast.Identifier):
            name = obj_expr.name
            # Series-table alias: any column name is a valid
            # member; unknown columns error with the available list so a
            # typo never silently reads na.
            if name in self.series_by_alias:
                col = self.get_table_column(name, member)
                if col is not None:
                    return Value.Num(col[bar])
                cols = [n for (n, _) in self.series_by_alias[name].columns]
                raise BrueError.runtime(
                    "'{}.{}' is not a column of the '{}' data table.".format(name, member, name),
                    loc,
                    "Available columns: {}.".format(", ".join(cols)),
                )
            if name in self.use_by_alias:
                if member not in FOREIGN_FIELDS:
                    raise BrueError.runtime(
                        "'{}.{}' is not a valid foreign-symbol field.".format(name, member),
                        loc,
                        "Foreign-symbol access supports: open, high, low, close, volume, hl2, hlc3, ohlc4.",
                    )
                foreign = self.get_foreign_field(name, member)
                if foreign is None:
                    return Value.Null
                if bar < len(foreign):
                    return Value.Num(foreign[bar])
                return Value.Null
            if name == "barstate":
                if self.v2:
                    raise BrueError.runtime(
                        "'barstate' is not part of the language: there is "
                        "no final bar and no chart in live trading.",
                        loc, "Decide from prices and time, not bar state.")
                if member == "islast":
                    return Value.Bool(bar == self.bar_count - 1)
                if member == "isfirst":
                    return Value.Bool(bar == 0)
                if member == "isconfirmed":
                    return Value.Bool(True)
                if member == "ishistory":
                    return Value.Bool(True)
                if member == "isnew":
                    return Value.Bool(True)
                if member == "isrealtime":
                    return Value.Bool(False)
                return Value.Null
            if name == "position" and self.engine is not None:
                close = self.closes[bar]
                r = self.engine.resolve_position(member, bar, close)
                return Value.Num(r) if r is not None else Value.Null
            if name == "strategy" and self.engine is not None:
                close = self.closes[bar]
                r = self.engine.resolve_strategy(member, bar, close)
                return Value.Num(r) if r is not None else Value.Null
            if name in self.enum_defs:
                e = self.enum_defs[name]
                got = e.get(member)
                return got if got is not None else Value.Null
            if self.v2 and member in FOREIGN_FIELDS \
                    and not self._is_scope_name(name) \
                    and name not in ("close", "open", "high", "low",
                                     "volume", "hl2", "hlc3", "ohlc4",
                                     "time"):
                # 2-segment reference (EURUSD.close): loud, either because
                # it names no timeframe or because nothing was fed.
                return self._read_symbol_chain([name], member, loc, bar,
                                               scalar=True)
        if self.v2 and member in FOREIGN_FIELDS \
                and isinstance(obj_expr, ast.Member):
            chain = self._symbol_chain(obj_expr)
            if chain is not None:
                # EURUSD.h1.close / XAUUSD.mybroker.h4.close: the feed
                # key is the chain joined by hyphens.
                return self._read_symbol_chain(chain, member, loc, bar,
                                               scalar=True)
        obj = self.eval_expr(obj_expr, bar)
        if obj.kind == "array":
            if member == "length" or member == "size":
                return Value.Num(float(len(obj.v)))
            # Multi-value builtin results are held as arrays, so `m.macd`
            # after `m = macd(...)` lands here. Arrays have no other
            # members, so this is always a script error, and the fix is to
            # destructure.
            raise BrueError.runtime(
                "'.{}' is not available on an array value.".format(member),
                loc,
                "If it came from a multi-value builtin, destructure instead: "
                "m_line, m_signal, m_hist = macd(close, 12, 26, 9).",
            )
        if obj.kind == "map":
            b = obj.v
            if b.has(member):
                got = b.get(member)
                return got if got is not None else Value.Null
            if member == "size":
                return Value.Num(float(b.len()))
        if obj.kind == "udt":
            got = obj.v.fields.get(member)
            return got if got is not None else Value.Null
        # Member access on a scalar fails loudly: a silent na would let
        # `m = macd(...)` then `m.macd` look valid and trade on missing data
        # (the language rule: anything that changes trading behavior is
        # loud). na/map/udt/array stay quiet because missing feeds, keys and
        # fields are documented as quiet-na degradation.
        if obj.kind in ("num", "str", "bool", "color"):
            raise BrueError.runtime(
                "'.{}' is not available on this value (a {}).".format(
                    member, {"num": "number", "str": "string",
                             "bool": "boolean", "color": "color"}[obj.kind]),
                loc,
                "If it came from a multi-value builtin, destructure instead: "
                "m_line, m_signal, m_hist = macd(close, 12, 26, 9).",
            )
        return Value.Null

    def eval_method_call(self, obj_expr, method, args, _loc, bar):
        # Dataset accessor: eco.latest("NFP"), er.prev("AAPL"). Checked
        # before evaluating the object so the alias identifier never
        # resolves as a variable here.
        if isinstance(obj_expr, ast.Identifier):
            name = obj_expr.name
            if name in self.dataset_aliases:
                if len(args) == 0:
                    return Value.Null
                key = self.eval_expr(args[0], bar)
                if not key.kind == "str":
                    return Value.Null
                k = key.js_string()
                bar_time = self.times[bar]
                acc = self.dataset_accessors.get(name)
                if acc is not None:
                    if method == "latest":
                        return acc.latest_at(k, bar_time)
                    if method == "prev":
                        return acc.prev_at(k, bar_time)
                    return Value.Null
                return Value.Null
            # UDT instantiation must be recognized BEFORE evaluating the
            # object: `Zone` is a type name, not a variable, and since
            # unknown identifiers are loud, evaluating it first would
            # error instead of reaching the .new() special case below.
            if method == "new" and name in self.type_defs:
                return self._udt_new(name, args, bar)
        obj = self.eval_expr(obj_expr, bar)

        if obj.kind == "array":
            avals = []
            for a in args:
                avals.append(self.eval_expr(a, bar))
            return eval_array_method(obj.v, method, avals)

        if obj.kind == "str":
            s = obj.js_string()
            if s.startswith("__table_") and method == "cell":
                cmd_idx = self.table_commands.get(s)
                if cmd_idx is not None:
                    avals = []
                    for a in args:
                        avals.append(self.eval_expr(a, bar))
                    row = or_zero(avals[0] if len(avals) > 0 else None)
                    col = or_zero(avals[1] if len(avals) > 1 else None)
                    a2 = avals[2] if len(avals) > 2 else None
                    if a2 is None or a2.kind == "null" or a2.kind == "undefined":
                        text = ""
                    else:
                        text = a2.js_string()
                    a3 = avals[3] if len(avals) > 3 else None
                    if a3 is not None and a3.kind == "color":
                        color = color_to_css(a3.v)
                    else:
                        color = "rgba(255,255,255,1)"
                    a4 = avals[4] if len(avals) > 4 else None
                    if a4 is not None and a4.kind == "color":
                        bg = color_to_css(a4.v)
                    else:
                        bg = "rgba(0,0,0,0)"
                    cmd = self.commands[cmd_idx]
                    if isinstance(cmd, Command.Table):
                        cmd.cells.append(TableCell(row=row, col=col, text=text, color=color, bg_color=bg))
                return Value.Null
            avals = []
            for a in args:
                avals.append(self.eval_expr(a, bar))
            return eval_string_method(s, method, avals)

        if obj.kind == "map":
            avals = []
            for a in args:
                avals.append(self.eval_expr(a, bar))
            return eval_map_method(obj.v, method, avals)

        return Value.Null

    def _udt_new(self, name, args, bar):
        """TypeName.new(...): defaults first, then positional overrides.
        Called from the pre-eval special case in eval_method_call; the type
        name is not a variable, so it must never reach resolve_identifier."""
        fields = self.type_defs[name]
        inst = OrderedMap()
        for f in fields:
            if f.default_value is not None:
                dv = self.eval_expr(f.default_value, bar)
            else:
                dv = Value.Null
            inst.set(f.name, dv)
        for (i, a) in enumerate(args):
            if i >= len(fields):
                break
            v = self.eval_expr(a, bar)
            inst.set(fields[i].name, v)
        return Value.Udt(UdtVal(type_name=name, fields=inst))

    def _symbol_chain(self, obj_expr):
        """[SYMBOL, seg, ...] for a Member chain rooted at a non-variable
        Identifier; None when the root is a real variable (maps, UDTs)."""
        segs = []
        e = obj_expr
        while isinstance(e, ast.Member):
            segs.append(e.member)
            e = e.object
        if not isinstance(e, ast.Identifier) or self._is_scope_name(e.name):
            return None
        segs.append(e.name)
        return list(reversed(segs))

    def _tf_keys_for(self, prefix):
        return sorted(k[len(prefix):] for k in self.use_by_alias
                      if k.startswith(prefix) and is_tf_token(k[len(prefix):]))

    def _read_symbol_chain(self, chain, field, loc, bar, scalar):
        """Resolve SYMBOL[.source].TF.field against the feed registry.
        The feed key is the chain joined by hyphens; a chain that names no
        timeframe is loud (every reference names its cadence), and so is a
        chain nobody fed."""
        key = "-".join(chain)
        if not is_tf_token(chain[-1]):
            toks = self._tf_keys_for(key + "-")
            if toks:
                raise BrueError.runtime(
                    "'{}.{}' names no timeframe.".format(
                        ".".join(chain), field),
                    loc,
                    "Every series names its cadence: {}.{}.{} (fed here: "
                    "{}).".format(".".join(chain), toks[0], field,
                                  ", ".join(toks)))
            raise BrueError.runtime(
                "no data was fed for '{}'.".format(key), loc,
                'Feed it under its timeframe key: data={{"{0}-h1": df}} '
                "or --data {0}=path (the token is derived from the "
                "bars).".format(key))
        if key not in self.use_by_alias:
            raise BrueError.runtime(
                "no data was fed for '{}'.".format(key), loc,
                'Pass data={{"{0}": df}} or --data {0}=path.'.format(key))
        f = self.get_foreign_field(key, field)
        if scalar:
            if f is not None and bar < len(f):
                return Value.Num(f[bar])
            return Value.Null
        return list(f) if f is not None else []

    def _is_scope_name(self, name):
        """Whether `name` is a live variable/constant in the current or
        global scope; a qualified symbol reference must never shadow a
        real variable."""
        for si in ([self.current, 0] if self.current != 0 else [0]):
            sc = self.scopes[si]
            if name in sc.constants or name in sc.persist_vars \
                    or name in sc.bar_vars or name in sc.series_vars:
                return True
        return False

    def _market_entry(self, oid, direction, qty, sl, tp, bar):
        """Shared market-entry mechanics: engine entry plus the optional
        protective bracket keyed to this entry."""
        close = self.closes[bar]
        time = self.times[bar]
        self.engine.entry(oid, direction, qty, bar, close, time)
        # sl/tp attach a protective bracket to THIS entry. For a
        # long, sl is a protective stop (exit if price falls to it)
        # and tp a limit (exit if price rises to it); exit() keys
        # both off from_entry so they only govern this position.
        if (sl is not None and not math.isnan(sl)) or \
                (tp is not None and not math.isnan(tp)):
            spec = strategy.ExitSpec()
            if sl is not None and not math.isnan(sl):
                spec.stop_price = sl
            if tp is not None and not math.isnan(tp):
                spec.limit_price = tp
            (c, h, l, t) = (self.closes[bar], self.highs[bar],
                            self.lows[bar], self.times[bar])
            self.engine.exit("{}_br".format(oid), oid, 0.0,
                             bar, c, h, l, t, spec)

    def _v2_order(self, name, args, kwargs, loc, bar):
        """Headerless order verbs: the market is written at the order,
        always, and the quantity is explicit. buy("EURUSD", 0.5, sl=...,
        tp=...); close("EURUSD")."""
        if len(args) < 1:
            raise BrueError.runtime(
                "{}() names its market first.".format(name), loc,
                'Write {}("EURUSD", ...).'.format(name))
        sym_v = self.eval_expr(args[0], bar)
        if sym_v.kind != "str":
            raise BrueError.runtime(
                "{}()'s first argument is the market, as a string.".format(
                    name),
                loc, 'Write {}("EURUSD", ...).'.format(name))
        sym = sym_v.js_string()
        if self.v2_market is None:
            raise BrueError.runtime(
                "the market must be a string literal in the order, so the "
                "script says what it trades.", loc,
                'Write it directly: {}("{}", ...).'.format(name, sym))
        if sym != self.v2_market:
            raise BrueError.runtime(
                "this script trades '{}'; '{}' is a second market.".format(
                    self.v2_market, sym),
                loc, "One market per script for now; multi-market orders "
                     "are the next design stage.")
        if name == "close":
            for kw in kwargs:
                if kw.name == "qty":
                    n = self.eval_expr(kw.value, bar).js_to_number()
                    if not math.isnan(n) and n > 0.0:
                        raise BrueError.runtime(
                            "partial close (qty=) is not available yet.",
                            loc, 'close("{}") flattens the position; '
                                 "partial close lands with the "
                                 "multi-market engine.".format(sym))
                else:
                    raise BrueError.runtime(
                        "unknown close() argument '{}'.".format(kw.name),
                        loc, 'close("{}") takes no other '
                             "arguments.".format(sym))
            (c, t) = (self.closes[bar], self.times[bar])
            self.engine.close_all(bar, c, t)
            return Value.Null
        if len(args) < 2:
            raise BrueError.runtime(
                "{}() needs an explicit quantity.".format(name), loc,
                'Write {}("{}", 0.5). Quantities are per order; there is '
                "no default.".format(name, sym))
        qty = self.eval_expr(args[1], bar).js_to_number()
        if math.isnan(qty) or qty <= 0.0:
            raise BrueError.runtime(
                "quantity must be a positive number.".format(), loc,
                'Write {}("{}", 0.5).'.format(name, sym))
        sl = None
        tp = None
        for kw in kwargs:
            if kw.name == "sl":
                sl = self.eval_expr(kw.value, bar).js_to_number()
            elif kw.name == "tp":
                tp = self.eval_expr(kw.value, bar).js_to_number()
            else:
                raise BrueError.runtime(
                    "unknown order argument '{}'.".format(kw.name), loc,
                    "Orders take sl= and tp= (absolute prices).")
        direction = "long" if name == "buy" else "short"
        # Trade-window gate (RunOptions): outside [from, to] the entry is
        # not placed; everything above was still evaluated, so the window
        # cannot change series promotion or caches. Exits are never gated:
        # an open position can always be managed to a close. The gate
        # belongs to the run, not the script.
        t = self.times[bar]
        if (self.run_options.trade_from is not None
                and t < self.run_options.trade_from) \
                or (self.run_options.trade_to is not None
                    and t > self.run_options.trade_to):
            return Value.Null
        self._order_seq += 1
        oid = "{}{}".format(direction, self._order_seq)
        self._market_entry(oid, direction, qty, sl, tp, bar)
        return Value.Null

    # == evalCall: intercepted names first, then user fns, then registry ==
    def eval_call(self, name, args, kwargs, loc, bar):
        if self.v2:
            if name in ("entry", "exit", "close_all"):
                raise BrueError.runtime(
                    "'{}' is not part of the language.".format(name),
                    loc, "Orders name their market: buy(\"EURUSD\", 0.5, "
                         "sl=...), close(\"EURUSD\").")
            if self.v2_trading and name in ("plot", "hline", "bgcolor",
                                            "fill", "shape", "label",
                                            "table", "rgb", "rgba",
                                            "with_alpha"):
                raise BrueError.runtime(
                    "'{}' is not allowed in a trading script: strategies "
                    "do not draw.".format(name),
                    loc, "Use log(message) for diagnostics; visuals belong "
                         "to indicator scripts (scripts with no orders).")
            if name in ("input_timeframe", "input_symbol", "input_session",
                        "input_price", "input_text_area", "input_time"):
                raise BrueError.runtime(
                    "'{}' is not part of the language.".format(name),
                    loc, "Use input(default, \"Title\"); the type follows "
                         "the default value.")
            if name == "log":
                msg = self.eval_expr(args[0], bar).js_string() if args else ""
                self.logs.append((self.times[bar], msg))
                return Value.Null
        if name == "input":
            return self.handle_input(args, kwargs, bar, loc)
        if name == "range":
            a0 = self.eval_expr(args[0], bar).js_to_number()
            if len(args) > 1:
                start = a0
                stop = self.eval_expr(args[1], bar).js_to_number()
            else:
                start = 0.0
                stop = a0
            if len(args) > 2:
                step = self.eval_expr(args[2], bar).js_to_number()
            else:
                step = 1.0
            arr = []
            if step > 0.0:
                i = start
                while i < stop:
                    arr.append(Value.Num(i))
                    i += step
            elif step < 0.0:
                i = start
                while i > stop:
                    arr.append(Value.Num(i))
                    i += step
            return Value.array(arr)
        if name == "in_session":
            if len(args) > 0:
                raw = self.eval_expr(args[0], bar)
            else:
                raw = Value.Null
            session_str = js_string_or_na_empty(raw).strip().lower().replace(" ", "_")
            tz = "UTC"
            for kw in kwargs:
                if kw.name == "tz":
                    v = self.eval_expr(kw.value, bar)
                    if v.kind == "null" or v.kind == "undefined":
                        tz = "UTC"
                    else:
                        tz = v.js_string()
            ts_ms = self.times[bar]
            ns = session.named_session(session_str)
            if ns is not None:
                return Value.Bool(session.in_session_range(
                    ts_ms, ns.sh, ns.sm, ns.eh, ns.em, ns.tz, True,
                ))
            parsed = parse_session_str(session_str)
            if parsed is not None:
                (sh, sm, eh, em) = parsed
                return Value.Bool(session.in_session_range(ts_ms, sh, sm, eh, em, tz, False))
            return Value.Bool(False)
        if name == "plot":
            if len(args) > 0:
                result = self.eval_expr(args[0], bar)
                # Host extension: record the plotted value per call site so
                # chart hosts can draw lines. Language semantics untouched
                # (plot still returns its evaluated arg; canonical JSON
                # never carries plots).
                if self.run_options.collect_plots:
                    if self._plot_bar != bar:
                        self._plot_bar = bar
                        self._plot_calls_this_bar = 0
                    idx = self._plot_calls_this_bar
                    self._plot_calls_this_bar += 1
                    meta = {}
                    for kw in kwargs:
                        try:
                            meta[kw.name] = self.eval_expr(kw.value, bar).js_string()
                        except Exception:
                            pass
                    key = meta.get("title") or f"plot_{idx + 1}"
                    if key not in self.plots:
                        self.plots[key] = []
                        self.plot_meta[key] = meta
                    self.plots[key].append((bar, result.num_or_nan()))
                return result
            return Value.Null
        if name == "shape":
            return self.handle_shape(args, kwargs, bar)
        if name == "label":
            return self.handle_label(args, kwargs, bar)
        if name == "bgcolor":
            return self.handle_bgcolor(args, kwargs, bar)
        if name == "hline":
            return self.handle_hline(args, kwargs, bar)
        if name == "fill":
            return self.handle_fill(args, kwargs, bar)
        if name == "table":
            return self.handle_table(args, kwargs, bar)

        # Strategy verbs (engine always exists: decl.mode is 'strategy')
        if self.engine is not None:
            if name == "entry":
                # Trade-window gate (RunOptions): outside [from, to] the
                # entry is silently not placed. Exits are never gated so
                # an open position can always be managed to a close.
                t = self.times[bar]
                outside_window = (
                    (self.run_options.trade_from is not None and t < self.run_options.trade_from)
                    or (self.run_options.trade_to is not None and t > self.run_options.trade_to)
                )
                if outside_window:
                    # Evaluate EXACTLY what the normal path evaluates
                    # (args 0-2 and qty kwargs, nothing more): expression
                    # evaluation has observable effects (series
                    # promotion, caches), so inside/outside-window bars
                    # must touch the same expressions.
                    for a in args[:3]:
                        self.eval_expr(a, bar)
                    for kw in kwargs:
                        if kw.name == "qty":
                            self.eval_expr(kw.value, bar)
                    return Value.Null
                entry_id = self.arg_string_or(args, 0, "entry", bar)
                direction = self.arg_string_or(args, 1, "long", bar)
                qty = 0.0
                if len(args) > 2:
                    v = self.eval_expr(args[2], bar).js_to_number()
                    qty = 0.0 if math.isnan(v) else v
                for kw in kwargs:
                    if kw.name == "qty":
                        v = self.eval_expr(kw.value, bar).js_to_number()
                        qty = 0.0 if math.isnan(v) else v
                close = self.closes[bar]
                time = self.times[bar]
                self.engine.entry(entry_id, direction, qty, bar, close, time)
                return Value.Null
            if name == "exit":
                exit_id = self.arg_string_or(args, 0, "exit", bar)
                from_entry = None
                qty = 0.0
                spec = strategy.ExitSpec()
                for kw in kwargs:
                    val = self.eval_expr(kw.value, bar)
                    if kw.name == "from_entry":
                        from_entry = val.js_string()
                    elif kw.name == "qty":
                        n = val.js_to_number()
                        qty = 0.0 if math.isnan(n) else n
                    elif kw.name == "stop":
                        spec.stop_price = val.js_to_number()
                    elif kw.name == "limit":
                        spec.limit_price = val.js_to_number()
                    elif kw.name == "profit":
                        spec.profit_points = val.js_to_number()
                    elif kw.name == "loss":
                        spec.loss_points = val.js_to_number()
                    elif kw.name == "trail_points":
                        # Trailing stop distance in price points or percent
                        # of the best price since arming. NaN means "not
                        # given" so a na-valued script arg cannot arm a NaN
                        # trail.
                        n = val.js_to_number()
                        if not math.isnan(n) and n > 0.0:
                            spec.trail_points = n
                    elif kw.name == "trail_percent":
                        n = val.js_to_number()
                        if not math.isnan(n) and n > 0.0:
                            spec.trail_percent = n
                fe_falsy = from_entry is None or from_entry == ""
                if fe_falsy and len(args) > 1:
                    v = self.eval_expr(args[1], bar)
                    from_entry = v.js_string()
                (c, h, l, t) = (self.closes[bar], self.highs[bar], self.lows[bar], self.times[bar])
                self.engine.exit(exit_id, from_entry, qty, bar, c, h, l, t, spec)
                return Value.Null
            if name == "close_all":
                (c, t) = (self.closes[bar], self.times[bar])
                self.engine.close_all(bar, c, t)
                return Value.Null
            if self.v2 and name in ("buy", "sell", "close"):
                return self._v2_order(name, args, kwargs, loc, bar)
            if name == "close":
                # close("id", qty=N) scales out N units instead of the
                # whole position (engine clamps to the open size; the
                # remainder stays open). qty absent, zero, or NaN means a
                # full close.
                qty = 0.0
                for kw in kwargs:
                    if kw.name == "qty":
                        n = self.eval_expr(kw.value, bar).js_to_number()
                        qty = 0.0 if math.isnan(n) else n
                if len(args) > 0:
                    v = self.eval_expr(args[0], bar)
                    if v.kind == "null" or v.kind == "undefined":
                        target = ""
                    else:
                        target = v.js_string()
                    if target != "":
                        (c, h, l, t) = (self.closes[bar], self.highs[bar], self.lows[bar], self.times[bar])
                        exit_id = "close_{}".format(target)
                        self.engine.exit(exit_id, target, qty, bar, c, h, l, t, strategy.ExitSpec())
                else:
                    (c, t) = (self.closes[bar], self.times[bar])
                    self.engine.close_all(bar, c, t)
                return Value.Null

            # Unified market-order verbs. ONE vocabulary for backtest and
            # live: buy()/sell() open a market position, close() flattens.
            # In a backtest run they drive the simulator here; a live host
            # runs the identical script and turns the same calls into broker
            # orders. No mode= needed: the RUNNER decides backtest vs live,
            # never the script.
            if name in ("buy", "sell"):
                direction = "long" if name == "buy" else "short"
                self._order_seq += 1
                oid = "{}{}".format(direction, self._order_seq)
                qty = 0.0
                sl = None
                tp = None
                if len(args) > 0:
                    v = self.eval_expr(args[0], bar).js_to_number()
                    qty = 0.0 if math.isnan(v) else v
                for kw in kwargs:
                    val = self.eval_expr(kw.value, bar).js_to_number()
                    if kw.name == "qty":
                        qty = 0.0 if math.isnan(val) else val
                    elif kw.name == "sl":
                        sl = val
                    elif kw.name == "tp":
                        tp = val
                self._market_entry(oid, direction, qty, sl, tp, bar)
                return Value.Null
            if name in ("buy_limit", "sell_limit", "buy_stop", "sell_stop"):
                # Pending limit/stop ENTRIES are not in the engine (it does
                # market entries + protective brackets). Reject loudly
                # rather than no-op silently.
                raise BrueError.runtime(
                    "{}() (a pending limit/stop entry) is not supported "
                    "yet.".format(name),
                    Loc(0, 0),
                    "Use buy()/sell() for a market entry with optional "
                    "sl=/tp= brackets.",
                )

        # Advanced inputs
        if name in ("input_timeframe", "input_symbol", "input_session", "input_price",
                    "input_text_area", "input_time"):
            return self.handle_advanced_input(name, args, bar, loc)

        if name == "str":
            if len(args) > 0:
                v = self.eval_expr(args[0], bar)
            else:
                v = Value.Undefined
            return Value.str(v.js_string_or_na())

        if name == "rgb":
            r = num_or_nan_raw(self.eval_expr(args[0], bar))
            g = num_or_nan_raw(self.eval_expr(args[1], bar))
            b = num_or_nan_raw(self.eval_expr(args[2], bar))
            return Value.Color(ColorVal(r=r, g=g, b=b, a=1.0))
        if name == "rgba":
            r = num_or_nan_raw(self.eval_expr(args[0], bar))
            g = num_or_nan_raw(self.eval_expr(args[1], bar))
            b = num_or_nan_raw(self.eval_expr(args[2], bar))
            a = num_or_nan_raw(self.eval_expr(args[3], bar))
            return Value.Color(ColorVal(r=r, g=g, b=b, a=a))
        if name == "with_alpha":
            c = self.eval_expr(args[0], bar)
            a = num_or_nan_raw(self.eval_expr(args[1], bar))
            if c.kind == "color":
                col = c.v
                # NaN is guarded above; clamp alpha to [0, 1].
                clamped = NAN if math.isnan(a) else min(max(a, 0.0), 1.0)
                return Value.Color(ColorVal(r=col.r, g=col.g, b=col.b, a=clamped))
            return Value.Null

        # User-defined functions (global scope first)
        func = self.scopes[0].functions.get(name)
        if func is None:
            func = self.scopes[self.current].functions.get(name)
        if func is not None:
            return self.call_user_function(func, args, bar)

        # Registered builtins
        builtin = get_builtin(name)
        if builtin is not None:
            if builtin.series_level:
                fp_parts = [self.fingerprint(a) for a in args]
                cache_key = "__call_{}_{}".format(name, "_".join(fp_parts))
                if len(kwargs) > 0:
                    kw_parts = ["{}:{}".format(k.name, self.fingerprint(k.value)) for k in kwargs]
                    cache_key += "_kw_{}".format("_".join(kw_parts))
                if cache_key not in self.scopes[0].series_vars:
                    result = self.eval_series_call_from_expr(name, args, kwargs, loc)
                    if result is not None:
                        if result.kind == "tuple" and builtin.return_count > 1:
                            self.tuple_cache["{}__tuple".format(cache_key)] = result.value
                        elif result.kind == "series":
                            self.scopes[0].series_vars[cache_key] = StoredSeries.Nums(result.value)
                        elif result.kind == "tuple":
                            # returnCount 1 with tuple result: stored in
                            # seriesVars as Vals of arrays
                            vals = [Value.array([Value.Num(x) for x in arr]) for arr in result.value]
                            self.scopes[0].series_vars[cache_key] = StoredSeries.Vals(vals)
                        # Scalar: nothing cached
                s = self.scopes[0].series_vars.get(cache_key)
                if s is not None:
                    return s.at(bar) if bar < s.len() else Value.Null
                tuple_key = "{}__tuple".format(cache_key)
                t = self.tuple_cache.get(tuple_key)
                if t is not None:
                    vals = [Value.Num(arr[bar] if bar < len(arr) else NAN) for arr in t]
                    return Value.array(vals)
                # fall through to bar-level dispatch
            # Bar-level path
            slots = self.order_builtin_args(builtin, args, kwargs, loc)
            n = max(len(slots), len(builtin.params))
            avals = []
            for i in range(n):
                e = slots[i] if i < len(slots) else None
                if e is not None:
                    avals.append(ArgVal.Val(self.eval_expr(e, bar)))
                else:
                    if i < len(builtin.params):
                        d = builtin.params[i].default.to_value()
                        if d is not None:
                            avals.append(ArgVal.Val(d))
                        else:
                            avals.append(ArgVal.Missing)
                    else:
                        avals.append(ArgVal.Missing)
            self.assert_required_args(builtin, avals, loc)
            r = self.run_builtin(builtin, avals, bar)
            if r.kind == "scalar":
                return r.value
            if r.kind == "series":
                # A series result on the bar-level path (seriesLevel fn
                # whose cache path fell through): per-bar read
                s = r.value
                if bar < len(s):
                    return Value.Num(s[bar])
                return Value.Null
            return Value.Null

        # Unknown function
        candidates = [str(s) for s in all_builtin_names()]
        for k in self.scopes[0].functions.keys():
            candidates.append(k)
        if self.current != 0:
            for k in self.scopes[self.current].functions.keys():
                candidates.append(k)
        closest = find_closest_match(name, candidates)
        if closest is not None:
            suggestion = "Did you mean '{}'?".format(closest)
        else:
            suggestion = "Check the function reference in SYNTAX.md; if a function is not listed there, it does not exist."
        raise BrueError.runtime(
            "Unknown function '{}'.".format(name),
            loc,
            suggestion,
        )

    def arg_string_or(self, args, i, default, bar):
        if i >= len(args):
            return default
        v = self.eval_expr(args[i], bar)
        if v.kind == "null" or v.kind == "undefined":
            return default
        return v.js_string()

    def call_user_function(self, func, args, bar):
        self.call_depth += 1
        if self.call_depth > 1000:
            self.call_depth -= 1
            # This message carries no "Line X, col Y" prefix (the goldens
            # pin the bare text), so bypass the formatting ctor and patch
            # the raw message in.
            err = BrueError.runtime("", Loc(line=0, col=0), None)
            err.message = "Maximum call stack size exceeded"
            err.suggestion = None
            raise err
        prev_scope = self.current
        new_scope = Scope()
        bound = {}

        # Bind arguments (evaluated in the CALLER's scope)
        values = []
        for (i, param) in enumerate(func.params):
            if i < len(args):
                v = self.eval_expr(args[i], bar)
                bound[param.name] = (args[i], prev_scope)
            elif param.default_value is not None:
                v = self.eval_expr(param.default_value, bar)
                bound[param.name] = (param.default_value, prev_scope)
            else:
                v = Value.Null
            values.append((param.name, v))
        for (k, v) in values:
            new_scope.bar_vars[k] = v
        new_scope.bound_arg_exprs = bound

        # Scopes are pushed and NEVER popped: bound_arg_exprs store scope
        # INDICES which must stay valid for later fingerprinting/resolution.
        self.scopes.append(new_scope)
        self.current = len(self.scopes) - 1

        return_value = Value.Null
        err = None
        for stmt in func.body:
            try:
                r = self.exec_stmt(stmt, bar)
            except BrueError as e:
                err = e
                break
            if isinstance(r, Flow.Return):
                return_value = r.value
                break

        self.current = prev_scope
        self.call_depth -= 1
        if err is not None:
            raise err
        return return_value

    # == Statement execution =============================================
    def exec_stmt(self, stmt, bar):
        if isinstance(stmt, ast.Assign):
            target = stmt.target
            op = stmt.op
            if bar == 0 and op == "=":
                if isinstance(stmt.value, ast.Call):
                    v = stmt.value
                    series = self.eval_series_call_from_expr(v.callee, v.args, v.kwargs, v.loc)
                    if series is not None and series.kind == "series":
                        s = series.value
                        if len(s) == self.bar_count:
                            self.scopes[0].series_vars[target] = StoredSeries.Nums(s)
                            return Flow.Normal
                    elif series is not None and series.kind == "tuple":
                        # list-of-series with length == bar_count edge (tiny datasets)
                        t = series.value
                        if len(t) == self.bar_count:
                            vals = [Value.array([Value.Num(x) for x in arr]) for arr in t]
                            self.scopes[0].series_vars[target] = StoredSeries.Vals(vals)
                            return Flow.Normal

            if op == "=" and target in self.scopes[0].series_vars:
                return Flow.Normal

            # Compound assigns evaluate the VALUE first, then read the
            # previous value; both coerce non-numbers to 0.
            val = self.eval_expr(stmt.value, bar)
            if op != "=":
                prev = self.resolve_identifier(target, bar, stmt.loc)
                p = prev.num_or_zero()
                v = val.num_or_zero()
                if op == "+=":
                    val = Value.Num(p + v)
                elif op == "-=":
                    val = Value.Num(p - v)
                elif op == "*=":
                    val = Value.Num(p * v)
                elif op == "/=":
                    val = Value.Num(p / v if v != 0.0 else NAN)
                else:
                    val = Value.Num(NAN)

            cur = self.current
            if target in self.scopes[cur].persist_vars:
                self.scopes[cur].persist_vars[target] = val
            else:
                self.scopes[cur].bar_vars[target] = val
            return Flow.Normal

        if isinstance(stmt, ast.Persist):
            cur = self.current
            if bar == 0 or stmt.target not in self.scopes[cur].persist_vars:
                v = self.eval_expr(stmt.value, bar)
                self.scopes[cur].persist_vars[stmt.target] = v
            return Flow.Normal

        if isinstance(stmt, ast.Const):
            if bar == 0:
                v = self.eval_expr(stmt.value, bar)
                cur = self.current
                self.scopes[cur].constants[stmt.target] = v
            return Flow.Normal

        if isinstance(stmt, ast.MultiAssign):
            targets = stmt.targets
            if bar == 0:
                if isinstance(stmt.value, ast.Call):
                    callee = stmt.value.callee
                    args = stmt.value.args
                    builtin = get_builtin(callee)
                    if builtin is not None and builtin.return_count > 1:
                        # positional args only; kwargs ignored here
                        resolved = []
                        for (i, a) in enumerate(args):
                            is_source = (i < len(builtin.params)
                                         and builtin.params[i].ptype == ParamType.Source)
                            if is_source:
                                resolved.append(ArgVal.Source(self.resolve_source(a, 0)))
                            else:
                                resolved.append(ArgVal.Val(self.eval_expr(a, 0)))
                        start = len(resolved)
                        for i in range(start, len(builtin.params)):
                            d = builtin.params[i].default.to_value()
                            if d is not None:
                                resolved.append(ArgVal.Val(d))
                        result = self.run_builtin(builtin, resolved, 0)
                        if result.kind == "tuple":
                            t = result.value
                            if len(t) >= len(targets):
                                for (i, target) in enumerate(targets):
                                    self.scopes[0].series_vars[target] = StoredSeries.Nums(list(t[i]))
                                return Flow.Normal
            if targets[0] in self.scopes[0].series_vars:
                return Flow.Normal
            val = self.eval_expr(stmt.value, bar)
            if val.kind == "array":
                items = val.v
                cur = self.current
                for (i, target) in enumerate(targets):
                    if i < len(items):
                        self.scopes[cur].bar_vars[target] = items[i]
                    else:
                        # Documented behavior: extra destructure names read
                        # na. Assigned explicitly, since an unassigned name
                        # would trip the loud unknown-identifier rule.
                        self.scopes[cur].bar_vars[target] = Value.Null
            return Flow.Normal

        if isinstance(stmt, ast.If):
            cond = self.eval_expr(stmt.condition, bar)
            if cond.is_truthy():
                for s in stmt.body:
                    r = self.exec_stmt(s, bar)
                    if r.is_signal():
                        return r
            else:
                handled = False
                for (ec, eb) in stmt.elifs:
                    if self.eval_expr(ec, bar).is_truthy():
                        for s in eb:
                            r = self.exec_stmt(s, bar)
                            if r.is_signal():
                                return r
                        handled = True
                        break
                if not handled and len(stmt.else_body) > 0:
                    for s in stmt.else_body:
                        r = self.exec_stmt(s, bar)
                        if r.is_signal():
                            return r
            return Flow.Normal

        if isinstance(stmt, ast.For):
            it = self.eval_expr(stmt.iterable, bar)
            if it.kind == "array":
                # Iterate a snapshot copy so body mutations of the array
                # cannot change the iteration.
                items = list(it.v)
                broke_outer = False
                for item in items:
                    if broke_outer:
                        break
                    cur = self.current
                    self.scopes[cur].bar_vars[stmt.variable] = item
                    for s in stmt.body:
                        r = self.exec_stmt(s, bar)
                        if r is Flow.Break:
                            broke_outer = True
                            break
                        if r is Flow.Continue:
                            break
                        if isinstance(r, Flow.Return):
                            return r
            return Flow.Normal

        if isinstance(stmt, ast.While):
            iterations = 0
            while True:
                if not self.eval_expr(stmt.condition, bar).is_truthy():
                    break
                if not (iterations < 10000):
                    break
                iterations += 1
                broke = False
                for s in stmt.body:
                    r = self.exec_stmt(s, bar)
                    if r is Flow.Break:
                        broke = True
                        break
                    if r is Flow.Continue:
                        break
                    if isinstance(r, Flow.Return):
                        return r
                if broke:
                    break
            return Flow.Normal

        if isinstance(stmt, ast.FunctionDef):
            if bar == 0:
                self.scopes[0].functions[stmt.name] = FunctionData(
                    params=list(stmt.params), body=list(stmt.body)
                )
            return Flow.Normal

        if isinstance(stmt, ast.Return):
            if stmt.value is not None:
                v = self.eval_expr(stmt.value, bar)
            else:
                v = Value.Null
            return Flow.Return(v)

        if isinstance(stmt, ast.Break):
            return Flow.Break
        if isinstance(stmt, ast.Continue):
            return Flow.Continue

        if isinstance(stmt, ast.ExprStmt):
            self.eval_expr(stmt.expr, bar)
            return Flow.Normal

        if isinstance(stmt, ast.MemberAssign):
            val = self.eval_expr(stmt.value, bar)
            target = stmt.target
            if isinstance(target, ast.Member):
                obj = self.eval_expr(target.object, bar)
                if obj.kind == "map":
                    obj.v.set(target.member, val)
                    return Flow.Normal
                if obj.kind == "udt":
                    obj.v.fields.set(target.member, val)
                    return Flow.Normal
                raise BrueError.runtime(
                    "Cannot assign to '.{}' on this value.".format(target.member),
                    stmt.loc,
                    "Member assignment works on struct instances (Type.new) and maps.",
                )
            if isinstance(target, ast.History):
                key = self.eval_expr(target.offset, bar)
                obj = self.eval_expr(target.source, bar)
                if obj.kind == "map":
                    obj.v.set(key.js_string(), val)
                    return Flow.Normal
                if obj.kind == "array" and key.kind == "num":
                    n = key.num_or_nan()
                    if math.fmod(n, 1.0) == 0.0 and n >= 0.0:
                        idx = int(n)
                        b = obj.v
                        if idx < len(b):
                            b[idx] = val
                        else:
                            # assigning past the end extends the array;
                            # the gap is filled with Null
                            while len(b) < idx:
                                b.append(Value.Null)
                            b.append(val)
                    return Flow.Normal
                if obj.kind == "udt" and key.kind == "str":
                    obj.v.fields.set(key.js_string(), val)
                    return Flow.Normal
                raise BrueError.runtime(
                    "Cannot assign with bracket syntax on this value.",
                    stmt.loc,
                    "Bracket assignment works on maps (m[\"key\"] = v) and arrays (a[0] = v).",
                )
            return Flow.Normal

        if isinstance(stmt, ast.TypeDef):
            if bar == 0:
                self.type_defs[stmt.name] = list(stmt.fields)
            return Flow.Normal

        if isinstance(stmt, ast.EnumDef):
            if bar == 0:
                m = {}
                for (mn, title) in stmt.members:
                    m[mn] = Value.str(title if title is not None else mn)
                self.enum_defs[stmt.name] = m
            return Flow.Normal

        return Flow.Normal

    # == Visual/input handlers ===========================================
    def _coerce_override(self, id_, override, default_val, loc):
        # Host UIs deliver form values as strings, and a string override for
        # a numeric input (input_price, input(14, ...)) would otherwise flow
        # into arithmetic as NaN silently. The override is coerced to the
        # DECLARED input's type here, at the one host boundary; an
        # uncoercible override is a loud host error, never a quiet na.
        want = default_val.kind
        have = override.kind
        if want == have or want in ("null", "undefined"):
            return override
        if want == "num":
            if have == "str":
                try:
                    return Value.Num(float(override.v.strip()))
                except ValueError:
                    pass
            elif have == "bool":
                return Value.Num(1.0 if override.v else 0.0)
        elif want == "bool" and have == "str":
            low = override.v.strip().lower()
            if low in ("true", "false"):
                return Value.Bool(low == "true")
        elif want == "str":
            return Value.str(override.js_string())
        raise BrueError.runtime(
            "Override for {} cannot be read as a {} (got {}: {}).".format(
                id_, want, have, override.js_string()),
            loc,
            "The host must send this input's override as the declared type.",
        )

    def handle_input(self, args, kwargs, bar, loc):
        if bar > 0:
            id_ = "input_{}".format(self.input_counter)
            self.input_counter += 1
            if id_ in self.input_overrides:
                default_val = (self.eval_expr(args[0], 0)
                               if len(args) > 0 else Value.Null)
                return self._coerce_override(
                    id_, self.input_overrides[id_], default_val, loc)
            if len(args) > 0:
                return self.eval_expr(args[0], 0)
            return Value.Null
        id_ = "input_{}".format(self.input_counter)
        self.input_counter += 1
        if len(args) > 0:
            default_val = self.eval_expr(args[0], 0)
        else:
            default_val = Value.Null
        if len(args) > 1:
            title = self.eval_expr(args[1], 0)
        else:
            title = Value.str(id_)
        if default_val.kind == "num":
            itype = "float"
        elif default_val.kind == "bool":
            itype = "bool"
        else:
            itype = "string"
        info = InputInfo(
            id=id_,
            itype=itype,
            title=title,
            default_value=default_val,
            extras=[],
        )
        for kw in kwargs:
            v = self.eval_expr(kw.value, 0)
            if kw.name in ("min", "max", "step", "options"):
                replaced = False
                for (idx, (k, _old)) in enumerate(info.extras):
                    if k == kw.name:
                        info.extras[idx] = (kw.name, v)
                        replaced = True
                        break
                if not replaced:
                    info.extras.append((kw.name, v))
        self.inputs.append(info)
        if id_ in self.input_overrides:
            return self._coerce_override(
                id_, self.input_overrides[id_], default_val, loc)
        return default_val

    def handle_advanced_input(self, func_name, args, bar, loc):
        if bar > 0:
            id_ = "input_{}".format(self.input_counter)
            self.input_counter += 1
            if len(args) > 0:
                default_val = self.eval_expr(args[0], 0)
            else:
                default_val = Value.Null
            if id_ in self.input_overrides:
                return self._coerce_override(
                    id_, self.input_overrides[id_], default_val, loc)
            return default_val
        id_ = "input_{}".format(self.input_counter)
        self.input_counter += 1
        if len(args) > 0:
            default_val = self.eval_expr(args[0], 0)
        else:
            default_val = Value.Null
        if len(args) > 1:
            title = Value.str(self.eval_expr(args[1], 0).js_string())
        else:
            title = Value.str(func_name.replace("input_", "", 1))
        if func_name == "input_timeframe":
            itype = "timeframe"
        elif func_name == "input_symbol":
            itype = "symbol"
        elif func_name == "input_session":
            itype = "session"
        elif func_name == "input_price":
            itype = "price"
        elif func_name == "input_text_area":
            itype = "text_area"
        elif func_name == "input_time":
            itype = "time"
        else:
            itype = "string"
        self.inputs.append(InputInfo(
            id=id_,
            itype=itype,
            title=title,
            default_value=default_val,
            extras=[],
        ))
        if id_ in self.input_overrides:
            return self._coerce_override(
                id_, self.input_overrides[id_], default_val, loc)
        return default_val

    def handle_shape(self, args, kwargs, bar):
        shape_type = "circle"
        if len(args) > 0:
            arg0 = args[0]
            if isinstance(arg0, ast.Identifier):
                shape_type = arg0.name
            else:
                v = self.eval_expr(arg0, bar)
                shape_type = v.js_string() if v.is_truthy() else "circle"
        location = "above_bar"
        color = "rgba(0,255,0,1)"
        size = "normal"
        text = ""
        text_color = "rgba(255,255,255,1)"
        for kw in kwargs:
            if kw.name == "location":
                if isinstance(kw.value, ast.Identifier):
                    location = kw.value.name
                    continue
            v = self.eval_expr(kw.value, bar)
            if kw.name == "location":
                if v.kind == "str":
                    location = v.js_string()
            elif kw.name == "color":
                if v.kind == "color":
                    color = color_to_css(v.v)
            elif kw.name == "size":
                if v.kind == "str":
                    size = v.js_string()
            elif kw.name == "text":
                if v.kind == "str":
                    text = v.js_string()
            elif kw.name == "text_color":
                if v.kind == "color":
                    text_color = color_to_css(v.v)
        self.commands.append(Command.Shape(
            bar_index=bar,
            shape_type=shape_type,
            location=location,
            color=color,
            size=size,
            text=text,
            text_color=text_color,
        ))
        return Value.Null

    def handle_label(self, args, kwargs, bar):
        if len(args) > 0:
            text = self.eval_expr(args[0], bar).js_string()
        else:
            text = ""
        x = Value.Num(float(bar))
        y = Value.Num(NAN)
        location = "above_bar"
        color = "rgba(255,255,255,1)"
        bg_color = "rgba(0,0,0,0)"
        size = "normal"
        tooltip = ""
        align = "center"
        for kw in kwargs:
            if kw.name == "location":
                if isinstance(kw.value, ast.Identifier):
                    location = kw.value.name
                    continue
            v = self.eval_expr(kw.value, bar)
            if kw.name == "x":
                if v.kind == "num":
                    x = v
            elif kw.name == "y":
                if v.kind == "num":
                    y = v
            elif kw.name == "location":
                if v.kind == "str":
                    location = v.js_string()
            elif kw.name == "color":
                if v.kind == "color":
                    color = color_to_css(v.v)
            elif kw.name == "bg":
                if v.kind == "color":
                    bg_color = color_to_css(v.v)
            elif kw.name == "size":
                if v.kind == "str":
                    size = v.js_string()
            elif kw.name == "tooltip":
                if v.kind == "str":
                    tooltip = v.js_string()
            elif kw.name == "align":
                if v.kind == "str":
                    align = v.js_string()
        self.commands.append(Command.Label(
            bar_index=bar,
            text=text,
            x=x,
            y=y,
            location=location,
            color=color,
            bg_color=bg_color,
            size=size,
            tooltip=tooltip,
            align=align,
        ))
        return Value.Null

    def handle_bgcolor(self, args, kwargs, bar):
        c = self.eval_expr(args[0], bar)
        panel = "overlay" if self.overlay.is_truthy() else "below"
        for kw in kwargs:
            if kw.name == "panel":
                panel = self.eval_expr(kw.value, bar).js_string()
        if c.kind == "color":
            color = color_to_css(c.v)
        else:
            color = "rgba(0,0,0,0)"
        self.commands.append(Command.BgColor(bar_index=bar, color=color, panel=panel))
        return Value.Null

    def handle_hline(self, args, kwargs, bar):
        if bar > 0:
            return Value.Null
        price = self.eval_expr(args[0], 0)
        color = "rgba(128,128,128,1)"
        style = "solid"
        width = Value.Num(1.0)
        title = ""
        panel = "overlay" if self.overlay.is_truthy() else "below"
        for kw in kwargs:
            v = self.eval_expr(kw.value, 0)
            if kw.name == "color":
                if v.kind == "color":
                    color = color_to_css(v.v)
            elif kw.name == "style":
                if v.kind == "str":
                    style = v.js_string()
            elif kw.name == "width":
                if v.kind == "num":
                    width = v
            elif kw.name == "title":
                if v.kind == "str":
                    title = v.js_string()
            elif kw.name == "panel":
                if v.kind == "str":
                    panel = v.js_string()
        self.commands.append(Command.HLine(
            price=price, color=color, style=style, width=width, title=title, panel=panel
        ))
        return Value.Null

    def handle_fill(self, args, kwargs, bar):
        if bar > 0:
            return Value.Null
        p1 = self.eval_expr(args[0], 0)
        p2 = self.eval_expr(args[1], 0)
        color = "rgba(0,100,255,0.1)"
        title = ""
        for kw in kwargs:
            v = self.eval_expr(kw.value, 0)
            if kw.name == "color":
                if v.kind == "color":
                    color = color_to_css(v.v)
            elif kw.name == "title":
                if v.kind == "str":
                    title = v.js_string()
        self.commands.append(Command.Fill(
            plot_id1=p1,
            plot_id2=p2,
            colors_len=self.bar_count,
            color=color,
            title=title,
        ))
        return Value.Null

    def handle_table(self, _args, kwargs, bar):
        position = "top_right"
        bg_color = "rgba(0,0,0,0.8)"
        for kw in kwargs:
            v = self.eval_expr(kw.value, bar)
            if kw.name == "position":
                if v.kind == "str":
                    position = v.js_string()
            elif kw.name == "bg":
                if v.kind == "color":
                    bg_color = color_to_css(v.v)
        table_id = "__table_{}".format(self.plot_counter)
        self.plot_counter += 1
        self.commands.append(Command.Table(position=position, bg_color=bg_color, cells=[]))
        self.table_commands[table_id] = len(self.commands) - 1
        return Value.str(table_id)

    def lookup_num_arg(self, args, kwargs, kw_name, pos_idx, bar):
        for kw in kwargs:
            if kw.name == kw_name:
                v = self.eval_expr(kw.value, bar)
                if v.kind == "null" or v.kind == "undefined":
                    return None
                return v.js_to_number()
        if len(args) > pos_idx:
            v = self.eval_expr(args[pos_idx], bar)
            if v.kind == "null" or v.kind == "undefined":
                return None
            return v.js_to_number()
        return None

    def handle_live_order(self, name, args, kwargs, bar):
        if bar != self.bar_count - 1:
            return Value.Null
        if len(args) == 0:
            symbol = ""
        else:
            v = self.eval_expr(args[0], bar)
            if v.kind == "null" or v.kind == "undefined":
                symbol = ""
            else:
                symbol = v.js_string()
        if symbol == "":
            return Value.Null
        if len(args) > 1:
            v = self.eval_expr(args[1], bar)
            if v.kind == "null" or v.kind == "undefined":
                qty = 0.0
            else:
                qty = v.js_to_number()
        else:
            qty = 0.0
        if not (qty > 0.0):
            return Value.Null
        side = "buy" if name.startswith("buy") else "sell"
        if name.endswith("_limit"):
            order_type = "limit"
        elif name.endswith("_stop"):
            order_type = "stop"
        else:
            order_type = "market"
        price = self.lookup_num_arg(args, kwargs, "price", 2, bar)
        sl = self.lookup_num_arg(args, kwargs, "sl", 3, bar)
        if sl is not None:
            stop_loss = sl
        else:
            stop_loss = self.lookup_num_arg(args, kwargs, "stop_loss", 3, bar)
        tp = self.lookup_num_arg(args, kwargs, "tp", 4, bar)
        if tp is not None:
            take_profit = tp
        else:
            take_profit = self.lookup_num_arg(args, kwargs, "take_profit", 4, bar)
        self.live_orders.append(LiveOrder(
            side=side,
            order_type=order_type,
            symbol=symbol,
            quantity=qty,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        ))
        return Value.Null


def or_zero(v):
    if v is None:
        return 0.0
    n = v.js_to_number()
    if math.isnan(n) or n == 0.0:
        return 0.0
    return n


# HH:?MM-HH:?MM, full match, ASCII digits only (re.ASCII keeps \d from
# matching Unicode digits).
_SESSION_RE = re.compile(r"(\d{1,2}):?(\d{2})-(\d{1,2}):?(\d{2})", re.ASCII)


def parse_session_str(s):
    m = _SESSION_RE.fullmatch(s)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def eval_array_method(arr, method, args):
    nums = [v.num_or_nan() for v in arr if v.kind == "num"]
    if method == "size" or method == "length":
        return Value.Num(float(len(arr)))
    if method == "max":
        if len(nums) == 0:
            return Value.Null
        m = float("-inf")
        has_nan = False
        for n in nums:
            if math.isnan(n):
                has_nan = True
            elif n > m:
                m = n
        return Value.Num(NAN if has_nan else m)
    if method == "min":
        if len(nums) == 0:
            return Value.Null
        m = float("inf")
        has_nan = False
        for n in nums:
            if math.isnan(n):
                has_nan = True
            elif n < m:
                m = n
        return Value.Num(NAN if has_nan else m)
    if method == "avg":
        if len(nums) == 0:
            return Value.Null
        total = 0.0
        for n in nums:
            total = total + n
        return Value.Num(total / float(len(nums)))
    if method == "sum":
        total = 0.0
        for n in nums:
            total = total + n
        return Value.Num(total)
    if method == "first":
        return arr[0] if len(arr) > 0 else Value.Null
    if method == "last":
        return arr[-1] if len(arr) > 0 else Value.Null
    if method == "includes":
        target = args[0] if len(args) > 0 else Value.Undefined
        for v in arr:
            if v.same_value_zero(target):
                return Value.Bool(True)
        return Value.Bool(False)
    if method == "join":
        if len(args) > 0 and args[0].kind == "str":
            sep = args[0].js_string()
        else:
            sep = ","
        parts = [v.js_string() for v in arr]
        return Value.str(sep.join(parts))
    return Value.Null


def eval_string_method(s, method, args):
    def arg_str(i):
        return args[i].js_string() if i < len(args) else "undefined"

    if method == "upper":
        return Value.str(s.upper())
    if method == "lower":
        return Value.str(s.lower())
    if method == "contains":
        return Value.Bool(arg_str(0) in s)
    if method == "starts_with":
        return Value.Bool(s.startswith(arg_str(0)))
    if method == "ends_with":
        return Value.Bool(s.endswith(arg_str(0)))
    if method == "replace":
        return Value.str(s.replace(arg_str(0), arg_str(1), 1))
    if method == "split":
        sep = arg_str(0)
        if sep == "":
            parts = [Value.str(c) for c in s]
        else:
            parts = [Value.str(p) for p in s.split(sep)]
        return Value.array(parts)
    if method == "trim":
        return Value.str(s.strip())
    if method == "length":
        return Value.Num(float(len(s)))
    return Value.Null


def eval_map_method(m, method, args):
    def key(i):
        return args[i].js_string() if i < len(args) else "undefined"

    if method == "get":
        got = m.get(key(0))
        return got if got is not None else Value.Null
    if method == "put" or method == "set":
        v = args[1] if len(args) > 1 else Value.Undefined
        m.set(key(0), v)
        return Value.Null
    if method == "contains" or method == "has":
        return Value.Bool(m.has(key(0)))
    if method == "remove" or method == "delete":
        m.remove(key(0))
        return Value.Null
    if method == "keys":
        return Value.array([Value.str(k) for (k, _v) in m.entries])
    if method == "values":
        return Value.array([v for (_k, v) in m.entries])
    if method == "size":
        return Value.Num(float(m.len()))
    if method == "clear":
        m.clear()
        return Value.Null
    return Value.Null


# Small Value coercion helpers used above.

def js_string_or_na_empty(v):
    """in_session's first arg: na/undefined read as the empty string,
    anything else as its string form."""
    if v.kind == "null" or v.kind == "undefined":
        return ""
    return v.js_string()


def num_or_nan_raw(v):
    """Raw numeric read for values stored without arithmetic (rgb
    components): numbers pass through, anything else is coerced like
    Number() so the CSS string is well formed."""
    return v.js_to_number()
