# The builtin registry: the registered builtin functions, in a fixed
# registration order (moving averages, oscillators, math, volatility,
# trend, volume, statistics, time, advanced inputs, trade verbs). Order is
# observable: find_closest_match breaks ties by first-seen, so a shuffled
# registry would change "did you mean" suggestions.
#
# Cross-module contract:
#   brue.value.Value: constructors Value.Num(n) / Value.Bool(b) /
#     Value.str(s), singleton instances Value.Null / Value.Undefined, a
#     lowercase `kind` tag ("undefined", "null", "num", "bool", "str",
#     "color", "array", "map", "udt"), and methods js_to_number() /
#     is_truthy() / js_string(). Variant tests live in _is_null_or_undefined
#     / _is_num below, the only place this module reads the tag.
#   brue.indicators: calculate_* functions; multi-output ones return
#     objects with named fields (r.macd, r.upper, r.k, r.a, ...).
#   brue.jsmath / brue.jsnum / brue.session: js_exp, js_log, js_log10,
#     js_pow, js_round, js_sign / js_number_to_string / utc_parts.

import math
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, List, Optional, Tuple

from . import indicators as ind
from . import session
from .jsmath import js_exp, js_log, js_log10, js_pow, js_round, js_sign
from .jsnum import js_number_to_string
from .value import Value

NAN = float("nan")
INFINITY = float("inf")
NEG_INFINITY = float("-inf")


class ParamType(Enum):
    Number = auto()
    String = auto()
    Bool = auto()
    Any = auto()
    Source = auto()


class DefaultVal:
    """A static registry default, converted to a runtime Value on demand.
    Variants: NONE / Num / Str. `DefaultVal.None` is not a legal Python
    attribute access, so the None variant is exposed as DefaultVal.NONE."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value=None):
        self.kind = kind  # "none" | "num" | "str"
        self.value = value

    @staticmethod
    def Num(n: float) -> "DefaultVal":
        return DefaultVal("num", n)

    @staticmethod
    def Str(s: str) -> "DefaultVal":
        return DefaultVal("str", s)

    def to_value(self) -> Optional[Value]:
        if self.kind == "none":
            return None
        if self.kind == "num":
            return Value.Num(self.value)
        return Value.str(self.value)

    def json(self) -> str:
        """JSON.stringify(defaultValue) for the missing-arg error signature."""
        if self.kind == "none":
            return "undefined"  # never hits: required params have no default
        if self.kind == "num":
            return js_number_to_string(self.value)
        return '"' + self.value + '"'


DefaultVal.NONE = DefaultVal("none")

# Short aliases for the registry table below.
DN = DefaultVal.Num
DS = DefaultVal.Str


@dataclass
class ParamDef:
    name: str
    ptype: ParamType
    required: bool
    default: DefaultVal


class ArgVal:
    """A resolved builtin argument. Sources arrive as materialized series;
    everything else is a Value. Missing = JS undefined. Variants:
    ArgVal.Source(series) / ArgVal.Val(value) / ArgVal.Missing (singleton
    instance)."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value=None):
        self.kind = kind  # "source" | "val" | "missing"
        self.value = value

    @staticmethod
    def Source(series: List[float]) -> "ArgVal":
        return ArgVal("source", series)

    @staticmethod
    def Val(v: Value) -> "ArgVal":
        return ArgVal("val", v)

    def is_missing(self) -> bool:
        return self.kind == "missing"


ArgVal.Missing = ArgVal("missing")


@dataclass
class Ctx:
    """Execution context handed to builtins (only the parts the registered
    functions actually use)."""

    bar_index: int
    bar_count: int
    opens: List[float]
    highs: List[float]
    lows: List[float]
    closes: List[float]
    volumes: List[float]
    times: List[float]
    # hasCachedSeries/cacheSeries store (only the sma builtin uses it)
    cache: dict


class ExecResult:
    """Variants: ExecResult.Series(list) / ExecResult.Tuple(list of lists) /
    ExecResult.Scalar(value)."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value):
        self.kind = kind  # "series" | "tuple" | "scalar"
        self.value = value

    @staticmethod
    def Series(s: List[float]) -> "ExecResult":
        return ExecResult("series", s)

    @staticmethod
    def Tuple(t: List[List[float]]) -> "ExecResult":
        return ExecResult("tuple", t)

    @staticmethod
    def Scalar(v: Value) -> "ExecResult":
        return ExecResult("scalar", v)


@dataclass
class BuiltinDef:
    name: str
    params: Tuple[ParamDef, ...]
    return_count: int
    series_level: bool
    execute: Callable[[List[ArgVal], Ctx], ExecResult]


# == Value variant tests (see the module docstring contract) ==============


def _is_null_or_undefined(v: Value) -> bool:
    return getattr(v, "kind", None) in ("null", "undefined")


def _is_num(v: Value) -> bool:
    return getattr(v, "kind", None) == "num"


# == f64 semantics helpers (IEEE ops that Python's math module turns into
# exceptions or ints) =====================================================


def f64_div(a: float, b: float) -> float:
    """IEEE 754 division. Python raises ZeroDivisionError on a zero
    divisor; IEEE returns NaN for 0/0 and signed infinity otherwise, which
    is what JS numbers produce."""
    if b == 0.0:
        if a != a or a == 0.0:
            return NAN
        return INFINITY * math.copysign(1.0, a) * math.copysign(1.0, b)
    return a / b


def f64_floor(x: float) -> float:
    """IEEE floor. math.floor returns an int and raises on NaN/inf; the
    copysign step keeps floor(-0.0) == -0.0 like IEEE."""
    if math.isnan(x) or math.isinf(x):
        return x
    r = float(math.floor(x))
    return math.copysign(r, x) if r == 0.0 else r


def f64_ceil(x: float) -> float:
    """IEEE ceil. The copysign step keeps ceil(-0.5) == -0.0 like IEEE
    (Python's math.ceil would give +0)."""
    if math.isnan(x) or math.isinf(x):
        return x
    r = float(math.ceil(x))
    return math.copysign(r, x) if r == 0.0 else r


def f64_sqrt(x: float) -> float:
    """IEEE sqrt: negative input is NaN, not a ValueError.
    sqrt(-0.0) stays -0.0 (and -0.0 < 0.0 is false, so it takes the
    math.sqrt path like IEEE)."""
    if x < 0.0:
        return NAN
    return math.sqrt(x)


# == Argument helpers (JS coercion idioms) ================================


def num(args: List[ArgVal], i: int) -> float:
    """Numeric argument for arithmetic: JS ToNumber coercion, so numeric
    strings convert and anything non-numeric is NaN."""
    a = args[i] if 0 <= i < len(args) else None
    if a is not None and a.kind == "val":
        return a.value.js_to_number()
    return NAN


def or_num(args: List[ArgVal], i: int, d: float) -> float:
    """Numeric argument with `||` fallback: any falsy value (0, NaN, "",
    null, undefined, missing) gives d."""
    a = args[i] if 0 <= i < len(args) else None
    if a is not None and a.kind == "val":
        if a.value.is_truthy():
            return a.value.js_to_number()
        return d
    return d


def nn_num(args: List[ArgVal], i: int, d: float) -> float:
    """Numeric argument with `??` fallback: only null, undefined or a
    missing argument gives d; a Source argument gives NaN."""
    a = args[i] if 0 <= i < len(args) else None
    if a is None or a.kind == "missing":
        return d
    if a.kind == "val":
        if _is_null_or_undefined(a.value):
            return d
        return a.value.js_to_number()
    return NAN  # Source


def src(args: List[ArgVal], i: int) -> List[float]:
    a = args[i] if 0 <= i < len(args) else None
    if a is not None and a.kind == "source":
        return list(a.value)
    return []


def to_vec(a: List[float]) -> List[float]:
    return list(a)


def get_at(data: List[float], x: float) -> float:
    """data[x] with JS undefined semantics for fractional or out-of-bounds
    x. `x % 1.0 == 0.0` is the integrality test for every input, including
    NaN and infinity (both give a NaN remainder, so the test is False)."""
    if x % 1.0 == 0.0 and x >= 0.0 and x < len(data):
        return data[int(x)]
    return NAN


# == The registry =========================================================


def p(name: str, ptype: ParamType, required: bool, default: Optional[DefaultVal] = None) -> ParamDef:
    """Build a ParamDef; a missing default means DefaultVal.NONE."""
    return ParamDef(name, ptype, required, DefaultVal.NONE if default is None else default)


_REGISTRY = None  # built once on first use: (list of BuiltinDef, lowercase name index)


def registry():
    global _REGISTRY
    if _REGISTRY is None:
        defs = build_registry()
        index = {}
        for i, d in enumerate(defs):
            index[d.name.lower()] = i
        _REGISTRY = (defs, index)
    return _REGISTRY


def get_builtin(name: str) -> Optional[BuiltinDef]:
    defs, index = registry()
    i = index.get(name.lower())
    return defs[i] if i is not None else None


def all_builtin_names() -> List[str]:
    return [d.name for d in registry()[0]]


def build_registry() -> List[BuiltinDef]:
    v: List[BuiltinDef] = []

    # === moving averages ===============================================
    def sma_execute(args, ctx):
        source = src(args, 0)
        # cache key uses raw JS templating of the length arg + endpoints
        a1 = args[1] if len(args) > 1 else None
        if a1 is not None and a1.kind == "val":
            length_str = a1.value.js_string()
        else:
            length_str = "undefined"
        if len(source) > 0:
            src_id = "{}:{}:{}".format(
                js_number_to_string(source[0]),
                js_number_to_string(source[len(source) - 1]),
                len(source),
            )
        else:
            src_id = "empty"
        cache_key = "sma:{}:{}".format(length_str, src_id)
        cached = ctx.cache.get(cache_key)
        if cached is not None:
            return ExecResult.Series(list(cached))
        result = ind.calculate_sma(source, num(args, 1))
        ctx.cache[cache_key] = list(result)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="sma",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=sma_execute,
    ))
    v.append(BuiltinDef(
        name="ema",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_ema(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="wma",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_wma(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="hma",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_hma(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="dema",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_dema(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="tema",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_tema(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="smma",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_smma(src(args, 0), num(args, 1))),
    ))

    def vwma_execute(args, ctx):
        source = src(args, 0)
        length = num(args, 1)
        volumes = to_vec(ctx.volumes)
        weighted = [
            val * (volumes[i] if i < len(volumes) else NAN)
            for i, val in enumerate(source)
        ]
        sma_weighted = ind.calculate_sma(weighted, length)
        sma_volume = ind.calculate_sma(volumes, length)
        out = []
        for i, val in enumerate(sma_weighted):
            sv = sma_volume[i] if i < len(sma_volume) else NAN
            if sv != 0.0:
                out.append(f64_div(val, sv))
            else:
                out.append(NAN)
        return ExecResult.Series(out)

    v.append(BuiltinDef(
        name="vwma",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=vwma_execute,
    ))

    def vwap_execute(args, ctx):
        highs = ctx.highs
        lows = ctx.lows
        closes = ctx.closes
        volumes = ctx.volumes
        a0 = args[0] if len(args) > 0 else None
        if a0 is not None and a0.kind == "source":
            source = list(a0.value)
        else:
            source = [(h + lows[i] + closes[i]) / 3.0 for i, h in enumerate(highs)]
        result = []
        cum_vol = 0.0
        cum_pv = 0.0
        for i in range(len(source)):
            vol = volumes[i] if i < len(volumes) else NAN
            cum_vol += vol
            cum_pv += source[i] * vol
            result.append(cum_pv / cum_vol if cum_vol > 0.0 else NAN)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="vwap",
        params=(p("source", ParamType.Source, False, DS("hlc3")),),
        return_count=1,
        series_level=True,
        execute=vwap_execute,
    ))
    v.append(BuiltinDef(
        name="alma",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, False, DN(9.0)),
            p("offset", ParamType.Number, False, DN(0.85)),
            p("sigma", ParamType.Number, False, DN(6.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_alma(
            src(args, 0),
            or_num(args, 1, 9.0),
            nn_num(args, 2, 0.85),
            or_num(args, 3, 6.0),
        )),
    ))
    v.append(BuiltinDef(
        name="kama",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, False, DN(10.0)),
            p("fast_period", ParamType.Number, False, DN(2.0)),
            p("slow_period", ParamType.Number, False, DN(30.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_kama(
            src(args, 0),
            or_num(args, 1, 10.0),
            or_num(args, 2, 2.0),
            or_num(args, 3, 30.0),
        )),
    ))
    v.append(BuiltinDef(
        name="zlema",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(21.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_zlema(src(args, 0), or_num(args, 1, 21.0))),
    ))
    v.append(BuiltinDef(
        name="t3",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, False, DN(5.0)),
            p("v_factor", ParamType.Number, False, DN(0.7)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_t3(src(args, 0), or_num(args, 1, 5.0), nn_num(args, 2, 0.7))
        ),
    ))
    v.append(BuiltinDef(
        name="lsma",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(25.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_lsma(src(args, 0), or_num(args, 1, 25.0))),
    ))
    v.append(BuiltinDef(
        name="mcginley",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(14.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_mcginley(src(args, 0), or_num(args, 1, 14.0))),
    ))

    # === oscillators ===================================================
    v.append(BuiltinDef(
        name="rsi",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_rsi(src(args, 0), num(args, 1))),
    ))

    def macd_execute(args, _ctx):
        r = ind.calculate_macd(src(args, 0), num(args, 1), num(args, 2), num(args, 3))
        return ExecResult.Tuple([r.macd, r.signal, r.histogram])

    v.append(BuiltinDef(
        name="macd",
        params=(
            p("source", ParamType.Source, True),
            p("fast", ParamType.Number, True, DN(12.0)),
            p("slow", ParamType.Number, True, DN(26.0)),
            p("signal", ParamType.Number, True, DN(9.0)),
        ),
        return_count=3,
        series_level=True,
        execute=macd_execute,
    ))

    def stoch_execute(args, ctx):
        length = num(args, 3)
        r = ind.calculate_stochastic(ctx.highs, ctx.lows, ctx.closes, length, 3.0, 3.0)
        return ExecResult.Series(r.k)

    v.append(BuiltinDef(
        name="stoch",
        params=(
            p("source", ParamType.Source, True),
            p("high_src", ParamType.Source, True),
            p("low_src", ParamType.Source, True),
            p("length", ParamType.Number, True),
        ),
        return_count=1,
        series_level=True,
        execute=stoch_execute,
    ))
    v.append(BuiltinDef(
        name="cci",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_cci(ctx.highs, ctx.lows, ctx.closes, num(args, 1))
        ),
    ))
    v.append(BuiltinDef(
        name="mfi",
        params=(p("length", ParamType.Number, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_mfi(ctx.highs, ctx.lows, ctx.closes, ctx.volumes, num(args, 0))
        ),
    ))
    v.append(BuiltinDef(
        name="williams_r",
        params=(p("length", ParamType.Number, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_williams_r(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        ),
    ))
    v.append(BuiltinDef(
        name="roc",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_roc(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="mom",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_momentum(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="ao",
        params=(p("fast", ParamType.Number, False, DN(5.0)), p("slow", ParamType.Number, False, DN(34.0))),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(ind.calculate_awesome_oscillator(ctx.highs, ctx.lows)),
    ))
    v.append(BuiltinDef(
        name="tsi",
        params=(
            p("source", ParamType.Source, True),
            p("long_length", ParamType.Number, False, DN(25.0)),
            p("short_length", ParamType.Number, False, DN(13.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_tsi(src(args, 0), or_num(args, 1, 25.0), or_num(args, 2, 13.0), 13.0).a
        ),
    ))
    v.append(BuiltinDef(
        name="dpo",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(21.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_dpo(src(args, 0), or_num(args, 1, 21.0))),
    ))

    def stoch_rsi_execute(args, _ctx):
        source = src(args, 0)
        rsi_len = or_num(args, 1, 14.0)
        stoch_len = or_num(args, 2, 14.0)
        k_smooth = or_num(args, 3, 3.0)
        d_len = or_num(args, 4, 3.0)
        r = ind.calculate_stoch_rsi(source, rsi_len, stoch_len, k_smooth)

        # NaN-aware SMA of K over d_smooth (computed in the builtin, not the lib)
        def d_at(i):
            if i < d_len - 1.0:
                return NAN
            s = 0.0
            j = i - d_len + 1.0
            while j <= i:
                if j >= 0.0 and j % 1.0 == 0.0 and j < len(r.k):
                    kv = r.k[int(j)]
                else:
                    kv = NAN
                if math.isnan(kv):
                    return NAN
                s += kv
                j += 1.0
            return s / d_len

        d = [d_at(i) for i in range(len(r.k))]
        return ExecResult.Tuple([r.k, d])

    v.append(BuiltinDef(
        name="stoch_rsi",
        params=(
            p("source", ParamType.Source, True),
            p("rsi_length", ParamType.Number, False, DN(14.0)),
            p("stoch_length", ParamType.Number, False, DN(14.0)),
            p("k_smooth", ParamType.Number, False, DN(3.0)),
            p("d_smooth", ParamType.Number, False, DN(3.0)),
        ),
        return_count=2,
        series_level=True,
        execute=stoch_rsi_execute,
    ))
    v.append(BuiltinDef(
        name="coppock",
        params=(p("source", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_coppock(src(args, 0))),
    ))
    v.append(BuiltinDef(
        name="trix",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(15.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_trix(src(args, 0), or_num(args, 1, 15.0), 9.0).a
        ),
    ))
    v.append(BuiltinDef(
        name="ultimate_osc",
        params=(
            p("fast", ParamType.Number, False, DN(7.0)),
            p("mid", ParamType.Number, False, DN(14.0)),
            p("slow", ParamType.Number, False, DN(28.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(ind.calculate_ultimate_oscillator(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 7.0),
            or_num(args, 1, 14.0),
            or_num(args, 2, 28.0),
        )),
    ))
    v.append(BuiltinDef(
        name="kst",
        params=(p("source", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_kst(src(args, 0)).a),
    ))

    def fisher_execute(args, ctx):
        r = ind.calculate_fisher(ctx.highs, ctx.lows, or_num(args, 0, 10.0))
        return ExecResult.Tuple([r.fisher, r.trigger])

    v.append(BuiltinDef(
        name="fisher",
        params=(p("length", ParamType.Number, False, DN(10.0)),),
        return_count=2,
        series_level=True,
        execute=fisher_execute,
    ))
    v.append(BuiltinDef(
        name="stc",
        params=(
            p("source", ParamType.Source, True),
            p("fast", ParamType.Number, False, DN(23.0)),
            p("slow", ParamType.Number, False, DN(50.0)),
            p("cycle", ParamType.Number, False, DN(10.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_stc(
            src(args, 0),
            or_num(args, 1, 23.0),
            or_num(args, 2, 50.0),
            or_num(args, 3, 10.0),
        )),
    ))

    def rvi_execute(args, ctx):
        r = ind.calculate_rvi(ctx.opens, ctx.highs, ctx.lows, ctx.closes, or_num(args, 0, 10.0))
        return ExecResult.Tuple([r.a, r.b])

    v.append(BuiltinDef(
        name="rvi",
        params=(p("length", ParamType.Number, False, DN(10.0)),),
        return_count=2,
        series_level=True,
        execute=rvi_execute,
    ))
    v.append(BuiltinDef(
        name="connors_rsi",
        params=(
            p("source", ParamType.Source, True),
            p("rsi_period", ParamType.Number, False, DN(3.0)),
            p("streak_period", ParamType.Number, False, DN(2.0)),
            p("rank_period", ParamType.Number, False, DN(100.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_connors_rsi(
            src(args, 0),
            or_num(args, 1, 3.0),
            or_num(args, 2, 2.0),
            or_num(args, 3, 100.0),
        )),
    ))
    v.append(BuiltinDef(
        name="cmo",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(9.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_cmo(src(args, 0), or_num(args, 1, 9.0))),
    ))
    v.append(BuiltinDef(
        name="apo",
        params=(
            p("source", ParamType.Source, True),
            p("fast", ParamType.Number, False, DN(12.0)),
            p("slow", ParamType.Number, False, DN(26.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_apo(src(args, 0), or_num(args, 1, 12.0), or_num(args, 2, 26.0))
        ),
    ))

    def ppo_execute(args, _ctx):
        r = ind.calculate_ppo(
            src(args, 0),
            or_num(args, 1, 12.0),
            or_num(args, 2, 26.0),
            or_num(args, 3, 9.0),
        )
        return ExecResult.Tuple([r.ppo, r.signal, r.histogram])

    v.append(BuiltinDef(
        name="ppo",
        params=(
            p("source", ParamType.Source, True),
            p("fast", ParamType.Number, False, DN(12.0)),
            p("slow", ParamType.Number, False, DN(26.0)),
            p("signal", ParamType.Number, False, DN(9.0)),
        ),
        return_count=3,
        series_level=True,
        execute=ppo_execute,
    ))

    def pvo_execute(args, ctx):
        r = ind.calculate_pvo(
            ctx.volumes,
            or_num(args, 0, 12.0),
            or_num(args, 1, 26.0),
            or_num(args, 2, 9.0),
        )
        return ExecResult.Tuple([r.ppo, r.signal, r.histogram])

    v.append(BuiltinDef(
        name="pvo",
        params=(
            p("fast", ParamType.Number, False, DN(12.0)),
            p("slow", ParamType.Number, False, DN(26.0)),
            p("signal", ParamType.Number, False, DN(9.0)),
        ),
        return_count=3,
        series_level=True,
        execute=pvo_execute,
    ))
    v.append(BuiltinDef(
        name="qstick",
        params=(p("length", ParamType.Number, False, DN(8.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_qstick(ctx.opens, ctx.closes, or_num(args, 0, 8.0))
        ),
    ))
    v.append(BuiltinDef(
        name="bop",
        params=(p("length", ParamType.Number, False, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_bop(ctx.opens, ctx.highs, ctx.lows, ctx.closes, or_num(args, 0, 14.0))
        ),
    ))
    v.append(BuiltinDef(
        name="psych_line",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(12.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_psych_line(src(args, 0), or_num(args, 1, 12.0))
        ),
    ))
    v.append(BuiltinDef(
        name="pfe",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(10.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_pfe(src(args, 0), or_num(args, 1, 10.0), 5.0)
        ),
    ))
    v.append(BuiltinDef(
        name="ulcer_index",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(14.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_ulcer_index(src(args, 0), or_num(args, 1, 14.0))
        ),
    ))

    def smi_execute(args, ctx):
        r = ind.calculate_smi(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 13.0),
            or_num(args, 1, 25.0),
            or_num(args, 2, 2.0),
        )
        return ExecResult.Tuple([r.a, r.b])

    v.append(BuiltinDef(
        name="smi",
        params=(
            p("length", ParamType.Number, False, DN(13.0)),
            p("smooth_k", ParamType.Number, False, DN(25.0)),
            p("smooth_d", ParamType.Number, False, DN(2.0)),
        ),
        return_count=2,
        series_level=True,
        execute=smi_execute,
    ))
    v.append(BuiltinDef(
        name="rel_vol_index",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, False, DN(10.0)),
            p("smoothing", ParamType.Number, False, DN(14.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_relative_vol_index(
            src(args, 0),
            or_num(args, 1, 10.0),
            or_num(args, 2, 14.0),
        )),
    ))

    # === math ==========================================================
    v.append(BuiltinDef(
        name="abs",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(math.fabs(num(args, 0)))),
    ))

    def max_execute(args, _ctx):
        # variadic: filter typeof number (NaN included; NaN wins)
        m = NEG_INFINITY
        has_nan = False
        for a in args:
            if a.kind == "val" and _is_num(a.value):
                n = a.value.js_to_number()
                if math.isnan(n):
                    has_nan = True
                elif n > m:
                    m = n
        return ExecResult.Scalar(Value.Num(NAN if has_nan else m))

    v.append(BuiltinDef(
        name="max",
        params=(p("a", ParamType.Number, True), p("b", ParamType.Number, True)),
        return_count=1,
        series_level=False,
        execute=max_execute,
    ))

    def min_execute(args, _ctx):
        m = INFINITY
        has_nan = False
        for a in args:
            if a.kind == "val" and _is_num(a.value):
                n = a.value.js_to_number()
                if math.isnan(n):
                    has_nan = True
                elif n < m:
                    m = n
        return ExecResult.Scalar(Value.Num(NAN if has_nan else m))

    v.append(BuiltinDef(
        name="min",
        params=(p("a", ParamType.Number, True), p("b", ParamType.Number, True)),
        return_count=1,
        series_level=False,
        execute=min_execute,
    ))

    def round_execute(args, _ctx):
        x = num(args, 0)
        digits = nn_num(args, 1, 0.0)
        factor = js_pow(10.0, digits)
        return ExecResult.Scalar(Value.Num(f64_div(js_round(x * factor), factor)))

    v.append(BuiltinDef(
        name="round",
        params=(p("x", ParamType.Number, True), p("digits", ParamType.Number, False, DN(0.0))),
        return_count=1,
        series_level=False,
        execute=round_execute,
    ))
    v.append(BuiltinDef(
        name="floor",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(f64_floor(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        name="ceil",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(f64_ceil(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        name="sqrt",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(f64_sqrt(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        name="pow",
        params=(p("x", ParamType.Number, True), p("y", ParamType.Number, True)),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(js_pow(num(args, 0), num(args, 1)))),
    ))
    v.append(BuiltinDef(
        name="log",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(js_log(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        name="log10",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(js_log10(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        # Natural log. Named ln because log is the diagnostic printer;
        # ln computes, log prints.
        name="ln",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(js_log(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        name="exp",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(js_exp(num(args, 0)))),
    ))
    v.append(BuiltinDef(
        name="sign",
        params=(p("x", ParamType.Number, True),),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: ExecResult.Scalar(Value.Num(js_sign(num(args, 0)))),
    ))

    def highest_execute(args, _ctx):
        source = src(args, 0)
        length = num(args, 1)
        result = []
        for i in range(len(source)):
            fi = float(i)
            if fi < length - 1.0:
                result.append(NAN)
            else:
                max_val = NEG_INFINITY
                j = fi - length + 1.0
                while j <= fi:
                    if j >= 0.0 and j % 1.0 == 0.0 and j < len(source):
                        val = source[int(j)]
                    else:
                        val = NAN
                    if val > max_val:
                        max_val = val
                    j += 1.0
                result.append(max_val)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="highest",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=highest_execute,
    ))

    def lowest_execute(args, _ctx):
        source = src(args, 0)
        length = num(args, 1)
        result = []
        for i in range(len(source)):
            fi = float(i)
            if fi < length - 1.0:
                result.append(NAN)
            else:
                min_val = INFINITY
                j = fi - length + 1.0
                while j <= fi:
                    if j >= 0.0 and j % 1.0 == 0.0 and j < len(source):
                        val = source[int(j)]
                    else:
                        val = NAN
                    if val < min_val:
                        min_val = val
                    j += 1.0
                result.append(min_val)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="lowest",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=lowest_execute,
    ))

    def sum_execute(args, _ctx):
        source = src(args, 0)
        length = num(args, 1)
        result = []
        window_sum = 0.0
        for i in range(len(source)):
            fi = float(i)
            window_sum += source[i]
            if fi >= length:
                idx = fi - length
                if idx >= 0.0 and idx % 1.0 == 0.0 and idx < len(source):
                    val = source[int(idx)]
                else:
                    val = NAN
                window_sum -= val
            result.append(window_sum if fi >= length - 1.0 else NAN)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="sum",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=sum_execute,
    ))

    def crossover_execute(args, _ctx):
        a = src(args, 0)
        b = src(args, 1)
        result = [NAN]
        for i in range(1, len(a)):
            bi = b[i] if i < len(b) else NAN
            bi1 = b[i - 1] if i - 1 < len(b) else NAN
            result.append(1.0 if a[i] > bi and a[i - 1] <= bi1 else 0.0)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="crossover",
        params=(p("a", ParamType.Source, True), p("b", ParamType.Source, True)),
        return_count=1,
        series_level=True,
        execute=crossover_execute,
    ))

    def crossunder_execute(args, _ctx):
        a = src(args, 0)
        b = src(args, 1)
        result = [NAN]
        for i in range(1, len(a)):
            bi = b[i] if i < len(b) else NAN
            bi1 = b[i - 1] if i - 1 < len(b) else NAN
            result.append(1.0 if a[i] < bi and a[i - 1] >= bi1 else 0.0)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="crossunder",
        params=(p("a", ParamType.Source, True), p("b", ParamType.Source, True)),
        return_count=1,
        series_level=True,
        execute=crossunder_execute,
    ))

    def cross_execute(args, _ctx):
        a = src(args, 0)
        b = src(args, 1)
        result = [NAN]
        for i in range(1, len(a)):
            bi = b[i] if i < len(b) else NAN
            bi1 = b[i - 1] if i - 1 < len(b) else NAN
            crossed = (a[i] > bi and a[i - 1] <= bi1) or (a[i] < bi and a[i - 1] >= bi1)
            result.append(1.0 if crossed else 0.0)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="cross",
        params=(p("a", ParamType.Source, True), p("b", ParamType.Source, True)),
        return_count=1,
        series_level=True,
        execute=cross_execute,
    ))

    def rising_execute(args, _ctx):
        source = src(args, 0)
        length = num(args, 1)
        result = []
        for i in range(len(source)):
            fi = float(i)
            if fi < length:
                result.append(NAN)
                continue
            all_rising = True
            j = 1.0
            while j <= length:
                a = get_at(source, fi - j + 1.0)
                b = get_at(source, fi - j)
                if a <= b:
                    all_rising = False
                    break
                j += 1.0
            result.append(1.0 if all_rising else 0.0)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="rising",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=rising_execute,
    ))

    def falling_execute(args, _ctx):
        source = src(args, 0)
        length = num(args, 1)
        result = []
        for i in range(len(source)):
            fi = float(i)
            if fi < length:
                result.append(NAN)
                continue
            all_falling = True
            j = 1.0
            while j <= length:
                a = get_at(source, fi - j + 1.0)
                b = get_at(source, fi - j)
                if a >= b:
                    all_falling = False
                    break
                j += 1.0
            result.append(1.0 if all_falling else 0.0)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="falling",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True)),
        return_count=1,
        series_level=True,
        execute=falling_execute,
    ))

    def change_execute(args, _ctx):
        source = src(args, 0)
        length = nn_num(args, 1, 1.0)
        result = []
        for i in range(len(source)):
            fi = float(i)
            result.append(source[i] - get_at(source, fi - length) if fi >= length else NAN)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="change",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, False, DN(1.0))),
        return_count=1,
        series_level=True,
        execute=change_execute,
    ))

    def barssince_execute(args, _ctx):
        cond = src(args, 0)
        result = []
        count = NAN
        for c in cond:
            if c == 1.0:
                count = 0.0
            elif not math.isnan(count):
                count += 1.0
            result.append(count)
        return ExecResult.Series(result)

    v.append(BuiltinDef(
        name="barssince",
        params=(p("condition", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=barssince_execute,
    ))

    def is_na_execute(args, _ctx):
        a = args[0] if len(args) > 0 else None
        if a is None or a.kind == "missing":
            b = True
        elif a.kind == "val":
            if _is_null_or_undefined(a.value):
                b = True
            elif _is_num(a.value):
                b = math.isnan(a.value.js_to_number())
            else:
                b = False
        else:
            b = False
        return ExecResult.Scalar(Value.Bool(b))

    v.append(BuiltinDef(
        name="is_na",
        params=(p("x", ParamType.Any, True),),
        return_count=1,
        series_level=False,
        execute=is_na_execute,
    ))

    def coalesce_execute(args, _ctx):
        a = args[0] if len(args) > 0 else None
        if a is None or a.kind == "missing":
            is_na = True
        elif a.kind == "val":
            if _is_null_or_undefined(a.value):
                is_na = True
            elif _is_num(a.value):
                is_na = math.isnan(a.value.js_to_number())
            else:
                is_na = False
        else:
            is_na = False
        pick = 1 if is_na else 0
        ap = args[pick] if pick < len(args) else None
        if ap is not None and ap.kind == "val":
            out = ap.value
        else:
            out = Value.Undefined
        return ExecResult.Scalar(out)

    v.append(BuiltinDef(
        name="coalesce",
        params=(p("x", ParamType.Any, True), p("default", ParamType.Any, True)),
        return_count=1,
        series_level=False,
        execute=coalesce_execute,
    ))

    # === volatility ====================================================
    def bb_execute(args, _ctx):
        r = ind.calculate_bollinger_bands(src(args, 0), num(args, 1), or_num(args, 2, 2.0))
        return ExecResult.Tuple([r.upper, r.middle, r.lower])

    v.append(BuiltinDef(
        name="bb",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(20.0)),
            p("mult", ParamType.Number, False, DN(2.0)),
        ),
        return_count=3,
        series_level=True,
        execute=bb_execute,
    ))
    v.append(BuiltinDef(
        name="atr",
        params=(p("length", ParamType.Number, True, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_atr(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        ),
    ))

    def keltner_execute(args, ctx):
        r = ind.calculate_keltner_channels(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 20.0),
            or_num(args, 2, 10.0),
            or_num(args, 1, 1.5),
        )
        return ExecResult.Tuple([r.upper, r.middle, r.lower])

    v.append(BuiltinDef(
        name="keltner",
        params=(
            p("length", ParamType.Number, True, DN(20.0)),
            p("mult", ParamType.Number, False, DN(1.5)),
            p("atr_length", ParamType.Number, False, DN(10.0)),
        ),
        return_count=3,
        series_level=True,
        execute=keltner_execute,
    ))

    def donchian_execute(args, ctx):
        r = ind.calculate_donchian(ctx.highs, ctx.lows, num(args, 0))
        return ExecResult.Tuple([r.upper, r.middle, r.lower])

    v.append(BuiltinDef(
        name="donchian",
        params=(p("length", ParamType.Number, True, DN(20.0)),),
        return_count=3,
        series_level=True,
        execute=donchian_execute,
    ))
    v.append(BuiltinDef(
        name="stddev",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True, DN(20.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_std_dev(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="hist_vol",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True, DN(20.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_historical_volatility(src(args, 0), num(args, 1))
        ),
    ))

    def chaikin_vol_execute(args, ctx):
        l = num(args, 0)
        return ExecResult.Series(ind.calculate_chaikin_volatility(ctx.highs, ctx.lows, l, 10.0))

    v.append(BuiltinDef(
        name="chaikin_vol",
        params=(p("length", ParamType.Number, True, DN(10.0)),),
        return_count=1,
        series_level=True,
        execute=chaikin_vol_execute,
    ))
    v.append(BuiltinDef(
        name="tr",
        params=(),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(
            ind.calculate_true_range(ctx.highs, ctx.lows, ctx.closes)
        ),
    ))
    v.append(BuiltinDef(
        name="natr",
        params=(p("length", ParamType.Number, True, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_natr(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        ),
    ))

    def squeeze_execute(args, ctx):
        r = ind.calculate_squeeze(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 20.0),
            or_num(args, 1, 2.0),
            or_num(args, 2, 20.0),
            or_num(args, 3, 1.5),
        )
        sq_num = [1.0 if b else 0.0 for b in r.squeeze]
        return ExecResult.Tuple([sq_num, r.momentum])

    v.append(BuiltinDef(
        name="squeeze",
        params=(
            p("bb_length", ParamType.Number, False, DN(20.0)),
            p("bb_mult", ParamType.Number, False, DN(2.0)),
            p("kc_length", ParamType.Number, False, DN(20.0)),
            p("kc_mult", ParamType.Number, False, DN(1.5)),
        ),
        return_count=2,
        series_level=True,
        execute=squeeze_execute,
    ))

    def chandelier_execute(args, ctx):
        r = ind.calculate_chandelier_exit(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 22.0),
            or_num(args, 1, 3.0),
        )
        return ExecResult.Tuple([r.exit_long, r.exit_short])

    v.append(BuiltinDef(
        name="chandelier",
        params=(p("length", ParamType.Number, False, DN(22.0)), p("mult", ParamType.Number, False, DN(3.0))),
        return_count=2,
        series_level=True,
        execute=chandelier_execute,
    ))
    v.append(BuiltinDef(
        name="bb_percent",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(20.0)),
            p("mult", ParamType.Number, False, DN(2.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_bb_percent(src(args, 0), num(args, 1), or_num(args, 2, 2.0))
        ),
    ))
    v.append(BuiltinDef(
        name="bb_width",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(20.0)),
            p("mult", ParamType.Number, False, DN(2.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_bb_width(src(args, 0), num(args, 1), or_num(args, 2, 2.0))
        ),
    ))

    def envelopes_execute(args, _ctx):
        r = ind.calculate_envelopes(src(args, 0), num(args, 1), or_num(args, 2, 2.5))
        return ExecResult.Tuple([r.upper, r.middle, r.lower])

    v.append(BuiltinDef(
        name="envelopes",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(20.0)),
            p("percent", ParamType.Number, False, DN(2.5)),
        ),
        return_count=3,
        series_level=True,
        execute=envelopes_execute,
    ))

    def acc_bands_execute(args, ctx):
        r = ind.calculate_acc_bands(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        return ExecResult.Tuple([r.upper, r.middle, r.lower])

    v.append(BuiltinDef(
        name="acc_bands",
        params=(p("length", ParamType.Number, True, DN(20.0)),),
        return_count=3,
        series_level=True,
        execute=acc_bands_execute,
    ))

    def price_channel_execute(args, ctx):
        r = ind.calculate_price_channel(ctx.highs, ctx.lows, num(args, 0))
        return ExecResult.Tuple([r.upper, r.middle, r.lower])

    v.append(BuiltinDef(
        name="price_channel",
        params=(p("length", ParamType.Number, True, DN(20.0)),),
        return_count=3,
        series_level=True,
        execute=price_channel_execute,
    ))

    # === trend =========================================================
    def adx_execute(args, ctx):
        r = ind.calculate_adx(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        return ExecResult.Tuple([r.adx, r.plus_di, r.minus_di])

    v.append(BuiltinDef(
        name="adx",
        params=(p("length", ParamType.Number, True, DN(14.0)),),
        return_count=3,
        series_level=True,
        execute=adx_execute,
    ))

    def supertrend_execute(args, ctx):
        r = ind.calculate_supertrend(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 10.0),
            or_num(args, 1, 3.0),
        )
        return ExecResult.Tuple([r.supertrend, r.direction])

    v.append(BuiltinDef(
        name="supertrend",
        params=(p("length", ParamType.Number, False, DN(10.0)), p("mult", ParamType.Number, False, DN(3.0))),
        return_count=2,
        series_level=True,
        execute=supertrend_execute,
    ))

    def sar_execute(args, ctx):
        r = ind.calculate_parabolic_sar(
            ctx.highs,
            ctx.lows,
            or_num(args, 0, 0.02),
            or_num(args, 1, 0.02),
            or_num(args, 2, 0.2),
        )
        return ExecResult.Tuple([r.sar, r.direction])

    v.append(BuiltinDef(
        name="sar",
        params=(
            p("start", ParamType.Number, False, DN(0.02)),
            p("step", ParamType.Number, False, DN(0.02)),
            p("max", ParamType.Number, False, DN(0.2)),
        ),
        return_count=2,
        series_level=True,
        execute=sar_execute,
    ))

    def ichimoku_execute(args, ctx):
        r = ind.calculate_ichimoku(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 9.0),
            or_num(args, 1, 26.0),
            or_num(args, 2, 52.0),
            26.0,
        )
        bar_count = len(ctx.closes)

        def trim(a):
            return a[:bar_count]

        return ExecResult.Tuple([r.tenkan, r.kijun, trim(r.senkou_a), trim(r.senkou_b), r.chikou])

    v.append(BuiltinDef(
        name="ichimoku",
        params=(
            p("tenkan", ParamType.Number, False, DN(9.0)),
            p("kijun", ParamType.Number, False, DN(26.0)),
            p("senkou_b", ParamType.Number, False, DN(52.0)),
        ),
        return_count=5,
        series_level=True,
        execute=ichimoku_execute,
    ))

    def aroon_execute(args, ctx):
        r = ind.calculate_aroon(ctx.highs, ctx.lows, num(args, 0))
        return ExecResult.Tuple([r.up, r.down, r.oscillator])

    v.append(BuiltinDef(
        name="aroon",
        params=(p("length", ParamType.Number, True, DN(25.0)),),
        return_count=3,
        series_level=True,
        execute=aroon_execute,
    ))

    def alligator_execute(_args, ctx):
        r = ind.calculate_alligator(ctx.closes)
        return ExecResult.Tuple([r.jaw, r.teeth, r.lips])

    v.append(BuiltinDef(
        name="alligator",
        params=(),
        return_count=3,
        series_level=True,
        execute=alligator_execute,
    ))

    def gator_execute(_args, ctx):
        r = ind.calculate_gator(ctx.closes)
        return ExecResult.Tuple([r.upper, r.lower])

    v.append(BuiltinDef(
        name="gator",
        params=(),
        return_count=2,
        series_level=True,
        execute=gator_execute,
    ))

    def vortex_execute(args, ctx):
        r = ind.calculate_vortex(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        return ExecResult.Tuple([r.vi_plus, r.vi_minus])

    v.append(BuiltinDef(
        name="vortex",
        params=(p("length", ParamType.Number, True, DN(14.0)),),
        return_count=2,
        series_level=True,
        execute=vortex_execute,
    ))
    v.append(BuiltinDef(
        name="choppiness",
        params=(p("length", ParamType.Number, True, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_choppiness(ctx.highs, ctx.lows, ctx.closes, num(args, 0))
        ),
    ))

    def elder_ray_execute(args, ctx):
        r = ind.calculate_elder_ray(ctx.highs, ctx.lows, ctx.closes, or_num(args, 0, 13.0))
        return ExecResult.Tuple([r.bull_power, r.bear_power])

    v.append(BuiltinDef(
        name="elder_ray",
        params=(p("length", ParamType.Number, False, DN(13.0)),),
        return_count=2,
        series_level=True,
        execute=elder_ray_execute,
    ))
    v.append(BuiltinDef(
        name="mass_index",
        params=(p("length", ParamType.Number, False, DN(25.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_mass_index(ctx.highs, ctx.lows, or_num(args, 0, 25.0))
        ),
    ))

    def chande_kroll_execute(args, ctx):
        r = ind.calculate_chande_kroll_stop(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            or_num(args, 0, 10.0),
            or_num(args, 1, 9.0),
            or_num(args, 2, 1.0),
        )
        return ExecResult.Tuple([r.stop_long, r.stop_short])

    v.append(BuiltinDef(
        name="chande_kroll",
        params=(
            p("p", ParamType.Number, False, DN(10.0)),
            p("q", ParamType.Number, False, DN(9.0)),
            p("x", ParamType.Number, False, DN(1.0)),
        ),
        return_count=2,
        series_level=True,
        execute=chande_kroll_execute,
    ))
    v.append(BuiltinDef(
        name="zigzag",
        params=(p("deviation", ParamType.Number, False, DN(5.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_zigzag(ctx.highs, ctx.lows, ctx.closes, or_num(args, 0, 5.0))
        ),
    ))

    def fractals_execute(args, ctx):
        r = ind.calculate_fractals(ctx.highs, ctx.lows, or_num(args, 0, 2.0))
        return ExecResult.Tuple([r.up_fractals, r.down_fractals])

    v.append(BuiltinDef(
        name="fractals",
        params=(p("period", ParamType.Number, False, DN(2.0)),),
        return_count=2,
        series_level=True,
        execute=fractals_execute,
    ))
    v.append(BuiltinDef(
        name="vhf",
        params=(p("length", ParamType.Number, False, DN(28.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(ind.calculate_vhf(ctx.closes, or_num(args, 0, 28.0))),
    ))

    # === volume ========================================================
    v.append(BuiltinDef(
        name="obv",
        params=(),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(ind.calculate_obv(ctx.closes, ctx.volumes)),
    ))
    v.append(BuiltinDef(
        name="cmf",
        params=(p("length", ParamType.Number, True, DN(20.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_cmf(ctx.highs, ctx.lows, ctx.closes, ctx.volumes, num(args, 0))
        ),
    ))
    v.append(BuiltinDef(
        name="adl",
        params=(),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(
            ind.calculate_adl(ctx.highs, ctx.lows, ctx.closes, ctx.volumes)
        ),
    ))
    v.append(BuiltinDef(
        name="force_index",
        params=(p("length", ParamType.Number, False, DN(13.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_force_index(ctx.closes, ctx.volumes, or_num(args, 0, 13.0))
        ),
    ))
    v.append(BuiltinDef(
        name="eom",
        params=(p("length", ParamType.Number, False, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_eom(ctx.highs, ctx.lows, ctx.volumes, or_num(args, 0, 14.0))
        ),
    ))
    v.append(BuiltinDef(
        name="vol_sma",
        params=(p("length", ParamType.Number, False, DN(20.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_volume_sma(ctx.volumes, or_num(args, 0, 20.0))
        ),
    ))
    v.append(BuiltinDef(
        name="nvi",
        params=(),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(ind.calculate_nvi(ctx.closes, ctx.volumes)),
    ))
    v.append(BuiltinDef(
        name="pvi",
        params=(),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(ind.calculate_pvi(ctx.closes, ctx.volumes)),
    ))
    v.append(BuiltinDef(
        name="pvt",
        params=(),
        return_count=1,
        series_level=True,
        execute=lambda _args, ctx: ExecResult.Series(ind.calculate_pvt(ctx.closes, ctx.volumes)),
    ))
    v.append(BuiltinDef(
        name="vroc",
        params=(p("length", ParamType.Number, False, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(ind.calculate_vroc(ctx.volumes, or_num(args, 0, 14.0))),
    ))
    v.append(BuiltinDef(
        name="net_volume",
        params=(p("length", ParamType.Number, False, DN(14.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_net_volume(ctx.closes, ctx.volumes, or_num(args, 0, 14.0))
        ),
    ))
    v.append(BuiltinDef(
        name="twiggs_mf",
        params=(p("length", ParamType.Number, False, DN(21.0)),),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(ind.calculate_twiggs_mf(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            ctx.volumes,
            or_num(args, 0, 21.0),
        )),
    ))
    v.append(BuiltinDef(
        name="vol_osc",
        params=(p("fast", ParamType.Number, False, DN(5.0)), p("slow", ParamType.Number, False, DN(10.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, ctx: ExecResult.Series(
            ind.calculate_volume_osc(ctx.volumes, or_num(args, 0, 5.0), or_num(args, 1, 10.0))
        ),
    ))

    def klinger_execute(args, ctx):
        r = ind.calculate_klinger(
            ctx.highs,
            ctx.lows,
            ctx.closes,
            ctx.volumes,
            or_num(args, 0, 34.0),
            or_num(args, 1, 55.0),
            or_num(args, 2, 13.0),
        )
        return ExecResult.Tuple([r.a, r.b])

    v.append(BuiltinDef(
        name="klinger",
        params=(
            p("fast", ParamType.Number, False, DN(34.0)),
            p("slow", ParamType.Number, False, DN(55.0)),
            p("signal", ParamType.Number, False, DN(13.0)),
        ),
        return_count=2,
        series_level=True,
        execute=klinger_execute,
    ))

    # === statistics ====================================================
    v.append(BuiltinDef(
        name="correlation",
        params=(
            p("source_a", ParamType.Source, True),
            p("source_b", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(20.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_correlation(src(args, 0), src(args, 1), num(args, 2))
        ),
    ))
    v.append(BuiltinDef(
        name="linreg",
        params=(
            p("source", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(14.0)),
            p("offset", ParamType.Number, False, DN(0.0)),
        ),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_linear_regression(src(args, 0), num(args, 1), 0.0).middle
        ),
    ))
    v.append(BuiltinDef(
        name="linreg_slope",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True, DN(14.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(ind.calculate_lin_reg_slope(src(args, 0), num(args, 1))),
    ))
    v.append(BuiltinDef(
        name="r_squared",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True, DN(14.0))),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(
            ind.calculate_lin_reg_r_squared(src(args, 0), num(args, 1))
        ),
    ))

    def zscore_execute(args, _ctx):
        source = src(args, 0)
        length = num(args, 1)
        mean = ind.calculate_sma(source, length)
        sd = ind.calculate_std_dev(source, length)
        out = [NAN] * len(source)
        for i in range(len(source)):
            m = mean[i]
            s = sd[i]
            if not math.isfinite(m) or not math.isfinite(s) or not math.isfinite(source[i]):
                continue
            out[i] = 0.0 if s == 0.0 else (source[i] - m) / s
        return ExecResult.Series(out)

    v.append(BuiltinDef(
        name="zscore",
        params=(p("source", ParamType.Source, True), p("length", ParamType.Number, True, DN(20.0))),
        return_count=1,
        series_level=True,
        execute=zscore_execute,
    ))

    def beta_execute(args, _ctx):
        y = src(args, 0)
        x = src(args, 1)
        length = num(args, 2)
        n = min(len(y), len(x))
        out = [NAN] * n
        if length < 2.0 or float(n) < length:
            return ExecResult.Series(out)
        i = length - 1.0
        while i < n:
            iu = int(i)
            sum_x = 0.0
            sum_y = 0.0
            valid = True
            j = i - length + 1.0
            while j <= i:
                xv = get_at(x, j)
                yv = get_at(y, j)
                if not math.isfinite(xv) or not math.isfinite(yv):
                    valid = False
                    break
                sum_x += xv
                sum_y += yv
                j += 1.0
            if valid:
                mean_x = sum_x / length
                mean_y = sum_y / length
                cov = 0.0
                var_x = 0.0
                j = i - length + 1.0
                while j <= i:
                    dx = get_at(x, j) - mean_x
                    cov += dx * (get_at(y, j) - mean_y)
                    var_x += dx * dx
                    j += 1.0
                out[iu] = NAN if var_x == 0.0 else cov / var_x
            i += 1.0
        return ExecResult.Series(out)

    v.append(BuiltinDef(
        name="beta",
        params=(
            p("target", ParamType.Source, True),
            p("benchmark", ParamType.Source, True),
            p("length", ParamType.Number, True, DN(30.0)),
        ),
        return_count=1,
        series_level=True,
        execute=beta_execute,
    ))

    def spread_execute(args, _ctx):
        a = src(args, 0)
        b = src(args, 1)
        n = min(len(a), len(b))
        out = []
        for i in range(n):
            out.append(a[i] - b[i] if math.isfinite(a[i]) and math.isfinite(b[i]) else NAN)
        return ExecResult.Series(out)

    v.append(BuiltinDef(
        name="spread",
        params=(p("a", ParamType.Source, True), p("b", ParamType.Source, True)),
        return_count=1,
        series_level=True,
        execute=spread_execute,
    ))

    def ratio_execute(args, _ctx):
        a = src(args, 0)
        b = src(args, 1)
        n = min(len(a), len(b))
        out = []
        for i in range(n):
            if math.isfinite(a[i]) and math.isfinite(b[i]) and b[i] != 0.0:
                out.append(a[i] / b[i])
            else:
                out.append(NAN)
        return ExecResult.Series(out)

    v.append(BuiltinDef(
        name="ratio",
        params=(p("a", ParamType.Source, True), p("b", ParamType.Source, True)),
        return_count=1,
        series_level=True,
        execute=ratio_execute,
    ))

    # === time ==========================================================
    def time_series(source, f):
        out = []
        for t in source:
            if math.isnan(t):
                out.append(NAN)
            else:
                parts = session.utc_parts(t)
                out.append(f(parts) if parts is not None else NAN)
        return out

    v.append(BuiltinDef(
        name="year",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.year)),
    ))
    v.append(BuiltinDef(
        name="month",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.month)),
    ))
    v.append(BuiltinDef(
        name="dayofmonth",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.day)),
    ))
    v.append(BuiltinDef(
        name="dayofweek",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.weekday)),
    ))
    v.append(BuiltinDef(
        name="hour",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.hour)),
    ))
    v.append(BuiltinDef(
        name="minute",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.minute)),
    ))
    v.append(BuiltinDef(
        name="second",
        params=(p("timestamp", ParamType.Source, True),),
        return_count=1,
        series_level=True,
        execute=lambda args, _ctx: ExecResult.Series(time_series(src(args, 0), lambda pt: pt.second)),
    ))

    # === advanced inputs ===============================================
    def adv_input_str(args, default):
        a = args[0] if len(args) > 0 else None
        if a is None or a.kind == "missing":
            return ExecResult.Scalar(Value.str(default))
        if a.kind == "val":
            if _is_null_or_undefined(a.value):
                return ExecResult.Scalar(Value.str(default))
            return ExecResult.Scalar(a.value)
        return ExecResult.Scalar(Value.str(default))  # Source

    def adv_input_num(args, default):
        a = args[0] if len(args) > 0 else None
        if a is None or a.kind == "missing":
            return ExecResult.Scalar(Value.Num(default))
        if a.kind == "val":
            if _is_null_or_undefined(a.value):
                return ExecResult.Scalar(Value.Num(default))
            return ExecResult.Scalar(a.value)
        return ExecResult.Scalar(Value.Num(default))  # Source

    v.append(BuiltinDef(
        name="input_timeframe",
        params=(
            p("default_value", ParamType.String, False, DS("1D")),
            p("title", ParamType.String, False, DS("Timeframe")),
            p("tooltip", ParamType.String, False, DS("")),
        ),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: adv_input_str(args, "1D"),
    ))
    v.append(BuiltinDef(
        name="input_symbol",
        params=(
            p("default_value", ParamType.String, False, DS("")),
            p("title", ParamType.String, False, DS("Symbol")),
            p("tooltip", ParamType.String, False, DS("")),
        ),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: adv_input_str(args, ""),
    ))
    v.append(BuiltinDef(
        name="input_session",
        params=(
            p("default_value", ParamType.String, False, DS("0930-1600")),
            p("title", ParamType.String, False, DS("Session")),
            p("tooltip", ParamType.String, False, DS("")),
        ),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: adv_input_str(args, "0930-1600"),
    ))
    v.append(BuiltinDef(
        name="input_price",
        params=(
            p("default_value", ParamType.Number, False, DN(0.0)),
            p("title", ParamType.String, False, DS("Price Level")),
            p("tooltip", ParamType.String, False, DS("")),
        ),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: adv_input_num(args, 0.0),
    ))
    v.append(BuiltinDef(
        name="input_text_area",
        params=(
            p("default_value", ParamType.String, False, DS("")),
            p("title", ParamType.String, False, DS("Text")),
            p("tooltip", ParamType.String, False, DS("")),
        ),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: adv_input_str(args, ""),
    ))
    v.append(BuiltinDef(
        name="input_time",
        params=(
            p("default_value", ParamType.Number, False, DN(0.0)),
            p("title", ParamType.String, False, DS("Date/Time")),
            p("tooltip", ParamType.String, False, DS("")),
        ),
        return_count=1,
        series_level=False,
        execute=lambda args, _ctx: adv_input_num(args, 0.0),
    ))

    # === trade verbs (stubs; runtime intercepts these names first) =====
    def trade_stub(_args, _ctx):
        return ExecResult.Scalar(Value.Null)

    v.append(BuiltinDef(
        name="buy",
        params=(
            p("symbol", ParamType.String, True),
            p("qty", ParamType.Number, True),
            p("sl", ParamType.Number, False, DefaultVal.NONE),
            p("tp", ParamType.Number, False, DefaultVal.NONE),
        ),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))
    v.append(BuiltinDef(
        name="sell",
        params=(
            p("symbol", ParamType.String, True),
            p("qty", ParamType.Number, True),
            p("sl", ParamType.Number, False, DefaultVal.NONE),
            p("tp", ParamType.Number, False, DefaultVal.NONE),
        ),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))
    v.append(BuiltinDef(
        name="buy_limit",
        params=(
            p("symbol", ParamType.String, True),
            p("qty", ParamType.Number, True),
            p("price", ParamType.Number, True),
            p("sl", ParamType.Number, False, DefaultVal.NONE),
            p("tp", ParamType.Number, False, DefaultVal.NONE),
        ),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))
    v.append(BuiltinDef(
        name="sell_limit",
        params=(
            p("symbol", ParamType.String, True),
            p("qty", ParamType.Number, True),
            p("price", ParamType.Number, True),
            p("sl", ParamType.Number, False, DefaultVal.NONE),
            p("tp", ParamType.Number, False, DefaultVal.NONE),
        ),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))
    v.append(BuiltinDef(
        name="buy_stop",
        params=(
            p("symbol", ParamType.String, True),
            p("qty", ParamType.Number, True),
            p("price", ParamType.Number, True),
            p("sl", ParamType.Number, False, DefaultVal.NONE),
            p("tp", ParamType.Number, False, DefaultVal.NONE),
        ),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))
    v.append(BuiltinDef(
        name="sell_stop",
        params=(
            p("symbol", ParamType.String, True),
            p("qty", ParamType.Number, True),
            p("price", ParamType.Number, True),
            p("sl", ParamType.Number, False, DefaultVal.NONE),
            p("tp", ParamType.Number, False, DefaultVal.NONE),
        ),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))
    v.append(BuiltinDef(
        name="close",
        params=(p("target", ParamType.Any, False, DefaultVal.NONE),),
        return_count=0,
        series_level=False,
        execute=trade_stub,
    ))

    return v
