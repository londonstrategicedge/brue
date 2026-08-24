# The indicator functions the Brue builtins call, with JS number semantics.
# Numeric rules:
#  - float accumulation ORDER is fixed (running sums stay running sums;
#    a rolling recompute would round differently)
#  - Math.max/Math.min propagate NaN; empty spreads give -Inf/+Inf
#  - warm-up NaN padding and the NaN-safe re-smoothing helpers are kept
#  - periods stay floats in comparisons; fractional indexes read as NaN
#    (JS undefined) and Array.slice truncates, exactly like V8
#  - IEEE f64 ops that Python floats do not mirror directly get helpers:
#    _fdiv (x / 0.0 must give signed inf or NaN, Python raises instead),
#    _sqrt (negative input must give NaN, math.sqrt raises), _floor
#    (NaN and inf must pass through, math.floor raises), and _fmax/_fmin
#    implement NaN-ignoring max/min, which the conformance goldens require
#    (if one argument is NaN the other is returned; Python's max()/min()
#    builtins do not do that). Every max/min in this file goes through
#    _fmax/_fmin unless its operands are NaN-checked explicitly first.
from __future__ import annotations

import math
from dataclasses import dataclass

from .jsmath import js_exp, js_log, js_log10, js_pow

NAN = float("nan")
INF = float("inf")


def is_nan(x):
    return x != x


def _get_f(data, x):
    """data[x] where x may be fractional or out of bounds: JS undefined,
    which reads as NaN in arithmetic."""
    # Integral, non-negative, in range. NaN and inf are never integral, so
    # the finiteness check first is equivalent and keeps math.trunc from
    # raising.
    if math.isfinite(x) and x == math.trunc(x) and x >= 0.0 and x < len(data):
        return data[int(x)]
    return NAN


def _js_slice(data, start, end):
    """JS Array.prototype.slice with ToIntegerOrInfinity coercion."""
    length = float(len(data))

    def norm(v):
        # trunc keeps +-inf; math.trunc would raise on them
        if v == INF or v == -INF:
            t = v
        else:
            t = float(math.trunc(v))
        if t < 0.0:
            # max(length + t, 0): t may be -inf, the sum is then -inf, so 0.0
            r = length + t
            return r if r > 0.0 else 0.0
        # min(t, length)
        return t if t < length else length

    s = int(norm(0.0 if is_nan(start) else start))
    e = int(norm(0.0 if is_nan(end) else end))
    if s >= e:
        return []
    return list(data[s:e])


def _max_of(arr):
    """Math.max(...arr): NaN if any NaN; -Infinity when empty."""
    m = -INF
    for v in arr:
        if is_nan(v):
            return NAN
        if v > m:
            m = v
    return m


def _min_of(arr):
    """Math.min(...arr): NaN if any NaN; +Infinity when empty."""
    m = INF
    for v in arr:
        if is_nan(v):
            return NAN
        if v < m:
            m = v
    return m


def _filter_valid(data):
    return [v for v in data if not is_nan(v)]


def _find_first_valid(data):
    for p, v in enumerate(data):
        if not is_nan(v):
            return p
    return -1


def _v_or0(v):
    """`v || 0` on a volume slot"""
    if is_nan(v) or v == 0.0:
        return 0.0
    return v


def _fmax(a, b):
    """IEEE maxNum: if one argument is NaN the other is returned; ties
    between +0.0 and -0.0 prefer +0.0."""
    if a != a:
        return b
    if b != b:
        return a
    if a > b:
        return a
    if a < b:
        return b
    if a == 0.0 and math.copysign(1.0, a) < 0.0:
        return b
    return a


def _fmin(a, b):
    """IEEE minNum: if one argument is NaN the other is returned; ties
    between +0.0 and -0.0 prefer -0.0."""
    if a != a:
        return b
    if b != b:
        return a
    if a < b:
        return a
    if a > b:
        return b
    if a == 0.0 and math.copysign(1.0, a) > 0.0:
        return b
    return a


def _fdiv(a, b):
    """IEEE f64 division: x / 0.0 yields signed inf (or NaN for 0/0 and
    NaN/0); Python raises ZeroDivisionError, so route divisions through
    here."""
    if b == 0.0:
        if a != a or a == 0.0:
            return NAN
        # sign of the result is sign(a) xor sign(b); b may be -0.0
        r = math.copysign(INF, a)
        return r if math.copysign(1.0, b) > 0.0 else -r
    return a / b


def _sqrt(x):
    """IEEE sqrt: NaN for negative input (math.sqrt raises).
    sqrt(-0.0) stays -0.0 (x < 0.0 is False for -0.0), like IEEE."""
    if x != x or x < 0.0:
        return NAN
    return math.sqrt(x)


def _floor(x):
    """IEEE floor: NaN/inf pass through (math.floor raises on them)."""
    if x != x or x == INF or x == -INF:
        return x
    return float(math.floor(x))


def calculate_rsi(closes, period):
    rsi = []
    if len(closes) < period + 1.0:
        return rsi
    gains = 0.0
    losses = 0.0
    # for (i = 1; i <= period; i++)
    i = 1.0
    while i <= period:
        change = _get_f(closes, i) - _get_f(closes, i - 1.0)
        if change > 0.0:
            gains += change
        else:
            losses += abs(change)
        i += 1.0
    avg_gain = _fdiv(gains, period)
    avg_loss = _fdiv(losses, period)

    j = 0.0
    while j < period:
        rsi.append(NAN)
        j += 1.0
    rsi.append(100.0 if avg_loss == 0.0 else 100.0 - _fdiv(100.0, 1.0 + _fdiv(avg_gain, avg_loss)))

    i = period + 1.0
    while i < len(closes):
        change = _get_f(closes, i) - _get_f(closes, i - 1.0)
        gain = change if change > 0.0 else 0.0
        loss = abs(change) if change < 0.0 else 0.0
        avg_gain = _fdiv(avg_gain * (period - 1.0) + gain, period)
        avg_loss = _fdiv(avg_loss * (period - 1.0) + loss, period)
        rsi.append(100.0 if avg_loss == 0.0 else 100.0 - _fdiv(100.0, 1.0 + _fdiv(avg_gain, avg_loss)))
        i += 1.0
    return rsi


def _first_finite_index(data):
    # A series starts at its first finite value. Chained indicators
    # (tsi(...), stoch_rsi(...)) carry a NaN warmup head; if the running
    # sum or seed ingested it, sma/ema would stay NaN forever. Interior
    # NaNs (invalid arithmetic mid-series) still propagate: only the
    # leading warmup is skipped, and a clean input (fv == 0) reduces to
    # the plain loop.
    for i, v in enumerate(data):
        if v == v:  # not NaN; infinities count as values
            return i
    return None


def calculate_ema(data, period):
    ema = []
    if not data:
        return ema
    fv = _first_finite_index(data)
    if fv is None:
        return [NAN] * len(data)
    multiplier = _fdiv(2.0, period + 1.0)
    ema_value = data[fv]
    for i in range(len(data)):
        fi = float(i - fv)
        if fi < period - 1.0:
            ema.append(NAN)
        elif fi == period - 1.0:
            s = _js_slice(data, float(fv), float(fv) + period)
            # left-to-right running sum
            sum_ = 0.0
            for b in s:
                sum_ = sum_ + b
            ema_value = _fdiv(sum_, period)
            ema.append(ema_value)
        else:
            ema_value = (data[i] - ema_value) * multiplier + ema_value
            ema.append(ema_value)
    return ema


def calculate_sma(data, period):
    sma = []
    if not data:
        return sma
    fv = _first_finite_index(data)
    if fv is None:
        return [NAN] * len(data)
    sum_ = 0.0
    for i in range(len(data)):
        if i < fv:
            sma.append(NAN)
            continue
        fi = float(i - fv)
        sum_ += data[i]
        if fi >= period:
            sum_ -= _get_f(data, float(i) - period)
        if fi < period - 1.0:
            sma.append(NAN)
        else:
            sma.append(_fdiv(sum_, period))
    return sma


def calculate_smma(data, period):
    smma = []
    if not data:
        return smma
    sum_ = 0.0
    for i in range(len(data)):
        fi = float(i)
        if fi < period - 1.0:
            sum_ += data[i]
            smma.append(NAN)
        elif fi == period - 1.0:
            sum_ += data[i]
            smma.append(_fdiv(sum_, period))
        else:
            prev = smma[i - 1]
            smma.append(_fdiv(prev * (period - 1.0) + data[i], period))
    return smma


@dataclass
class Macd:
    macd: list
    signal: list
    histogram: list


def calculate_macd(closes, fast, slow, signal_p):
    fast_ema = calculate_ema(closes, fast)
    slow_ema = calculate_ema(closes, slow)
    macd_line = []
    for f, s in zip(fast_ema, slow_ema):
        macd_line.append(f - s)
    signal_line = calculate_ema(_filter_valid(macd_line), signal_p)

    padded_signal = []
    signal_idx = 0
    for m in macd_line:
        if is_nan(m) or signal_idx >= len(signal_line):
            padded_signal.append(NAN)
        else:
            padded_signal.append(signal_line[signal_idx])
            signal_idx += 1
    histogram = []
    for m, s in zip(macd_line, padded_signal):
        histogram.append(m - s)
    return Macd(macd=macd_line, signal=padded_signal, histogram=histogram)


@dataclass
class Bands:
    upper: list
    middle: list
    lower: list


def calculate_bollinger_bands(closes, period, std_dev):
    middle = calculate_sma(closes, period)
    upper = []
    lower = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            upper.append(NAN)
            lower.append(NAN)
        else:
            mean = middle[i]
            sum_squares = 0.0
            j = fi - period + 1.0
            while j <= fi:
                sum_squares += js_pow(_get_f(closes, j) - mean, 2.0)
                j += 1.0
            variance = _fdiv(sum_squares, period)
            std = _sqrt(variance)
            upper.append(mean + std_dev * std)
            lower.append(mean - std_dev * std)
    return Bands(upper=upper, middle=middle, lower=lower)


def calculate_atr(highs, lows, closes, period):
    atr = []
    if len(highs) < 2:
        return atr
    true_ranges = []
    for i in range(len(highs)):
        if i == 0:
            true_ranges.append(highs[i] - lows[i])
        else:
            # NaN-ignoring max here (not _js_max3), matching the conformance goldens
            tr = _fmax(_fmax(highs[i] - lows[i], abs(highs[i] - closes[i - 1])), abs(lows[i] - closes[i - 1]))
            true_ranges.append(tr)
    sum_ = 0.0
    for i in range(len(true_ranges)):
        fi = float(i)
        if fi < period - 1.0:
            sum_ += true_ranges[i]
            atr.append(NAN)
        elif fi == period - 1.0:
            sum_ += true_ranges[i]
            atr.append(_fdiv(sum_, period))
        else:
            prev = atr[i - 1]
            atr.append(_fdiv(prev * (period - 1.0) + true_ranges[i], period))
    return atr


def _js_max3(a, b, c):
    """Math.max(a, b, c) in JS: NaN if any operand is NaN (_fmax ignores
    NaN, so it cannot stand in for Math.max)."""
    if is_nan(a) or is_nan(b) or is_nan(c):
        return NAN
    return _fmax(_fmax(a, b), c)


@dataclass
class Stoch:
    k: list
    d: list


def calculate_stochastic(highs, lows, closes, k_period, d_period, smooth):
    raw_k = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < k_period - 1.0:
            raw_k.append(NAN)
        else:
            highest = -INF
            lowest = INF
            j = fi - k_period + 1.0
            while j <= fi:
                h = _get_f(highs, j)
                l = _get_f(lows, j)
                if h > highest:
                    highest = h
                if l < lowest:
                    lowest = l
                j += 1.0
            range_ = highest - lowest
            raw_k.append(50.0 if range_ == 0.0 else _fdiv(closes[i] - lowest, range_) * 100.0)

    k_first_valid = _find_first_valid(raw_k)
    if k_first_valid < 0:
        k = list(raw_k)
    else:
        k = [NAN] * k_first_valid
        k.extend(calculate_sma(raw_k[k_first_valid:], smooth))
    d_first_valid = _find_first_valid(k)
    if d_first_valid < 0:
        d = list(k)
    else:
        d = [NAN] * d_first_valid
        d.extend(calculate_sma(k[d_first_valid:], d_period))
    return Stoch(k=k, d=d)


def calculate_williams_r(highs, lows, closes, period):
    wr = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            wr.append(NAN)
        else:
            highest = -INF
            lowest = INF
            j = fi - period + 1.0
            while j <= fi:
                h = _get_f(highs, j)
                l = _get_f(lows, j)
                if h > highest:
                    highest = h
                if l < lowest:
                    lowest = l
                j += 1.0
            range_ = highest - lowest
            wr.append(-50.0 if range_ == 0.0 else _fdiv(highest - closes[i], range_) * -100.0)
    return wr


def calculate_cci(highs, lows, closes, period):
    cci = []
    tp = []
    for i in range(len(highs)):
        tp.append((highs[i] + lows[i] + closes[i]) / 3.0)
    for i in range(len(tp)):
        fi = float(i)
        if fi < period - 1.0:
            cci.append(NAN)
        else:
            sum_ = 0.0
            j = fi - period + 1.0
            while j <= fi:
                sum_ += _get_f(tp, j)
                j += 1.0
            sma = _fdiv(sum_, period)
            md_sum = 0.0
            j = fi - period + 1.0
            while j <= fi:
                md_sum += abs(_get_f(tp, j) - sma)
                j += 1.0
            mean_dev = _fdiv(md_sum, period)
            cci.append(0.0 if mean_dev == 0.0 else _fdiv(tp[i] - sma, 0.015 * mean_dev))
    return cci


@dataclass
class Adx:
    adx: list
    plus_di: list
    minus_di: list


def calculate_adx(highs, lows, closes, period):
    adx = []
    plus_di = []
    minus_di = []

    if len(highs) < period + 1.0:
        return Adx(adx=[], plus_di=[], minus_di=[])

    tr = []
    plus_dm = []
    minus_dm = []
    for i in range(len(highs)):
        if i == 0:
            tr.append(highs[i] - lows[i])
            plus_dm.append(0.0)
            minus_dm.append(0.0)
        else:
            tr.append(_js_max3(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if (up_move > down_move and up_move > 0.0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0.0) else 0.0)

    smoothed_tr = calculate_smma(tr, period)
    smoothed_plus_dm = calculate_smma(plus_dm, period)
    smoothed_minus_dm = calculate_smma(minus_dm, period)

    raw_plus_di = []
    raw_minus_di = []
    dx = []
    for i in range(len(highs)):
        if is_nan(smoothed_tr[i]) or smoothed_tr[i] == 0.0:
            raw_plus_di.append(NAN)
            raw_minus_di.append(NAN)
            dx.append(NAN)
        else:
            p_di = _fdiv(smoothed_plus_dm[i], smoothed_tr[i]) * 100.0
            m_di = _fdiv(smoothed_minus_dm[i], smoothed_tr[i]) * 100.0
            raw_plus_di.append(p_di)
            raw_minus_di.append(m_di)
            di_sum = p_di + m_di
            dx.append(0.0 if di_sum == 0.0 else _fdiv(abs(p_di - m_di), di_sum) * 100.0)

    smoothed_adx = calculate_smma(_filter_valid(dx), period)

    adx_idx = 0
    for i in range(len(highs)):
        if is_nan(dx[i]):
            adx.append(NAN)
        elif adx_idx < len(smoothed_adx):
            adx.append(smoothed_adx[adx_idx])
            adx_idx += 1
        else:
            adx.append(NAN)
        plus_di.append(raw_plus_di[i])
        minus_di.append(raw_minus_di[i])

    return Adx(adx=adx, plus_di=plus_di, minus_di=minus_di)


def calculate_roc(closes, period):
    roc = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            roc.append(NAN)
        else:
            prev_close = _get_f(closes, fi - period)
            roc.append(0.0 if prev_close == 0.0 else _fdiv(closes[i] - prev_close, prev_close) * 100.0)
    return roc


@dataclass
class Ichimoku:
    tenkan: list
    kijun: list
    senkou_a: list
    senkou_b: list
    chikou: list


def calculate_ichimoku(highs, lows, closes, tenkan_period, kijun_period, senkou_b_period, displacement):
    length = len(closes)
    tenkan = []
    kijun = []
    senkou_a = []
    senkou_b = []
    chikou = []

    def get_mid_point(start, period):
        if start + period > len(highs):
            return NAN
        highest = -INF
        lowest = INF
        j = start
        while j < start + period:
            h = _get_f(highs, j)
            l = _get_f(lows, j)
            if h > highest:
                highest = h
            if l < lowest:
                lowest = l
            j += 1.0
        return (highest + lowest) / 2.0

    for i in range(length):
        fi = float(i)
        tenkan.append(
            get_mid_point(fi - tenkan_period + 1.0, tenkan_period)
            if fi >= tenkan_period - 1.0
            else NAN
        )
        kijun.append(
            get_mid_point(fi - kijun_period + 1.0, kijun_period)
            if fi >= kijun_period - 1.0
            else NAN
        )
        chikou.append(closes[i])

    i = 0.0
    while i < length + displacement:
        if i < displacement:
            senkou_a.append(NAN)
            senkou_b.append(NAN)
        else:
            src_idx = i - displacement
            t = _get_f(tenkan, src_idx)
            k = _get_f(kijun, src_idx)
            senkou_a.append(NAN if (is_nan(t) or is_nan(k)) else (t + k) / 2.0)
            senkou_b.append(
                get_mid_point(src_idx - senkou_b_period + 1.0, senkou_b_period)
                if src_idx >= senkou_b_period - 1.0
                else NAN
            )
        i += 1.0

    return Ichimoku(tenkan=tenkan, kijun=kijun, senkou_a=senkou_a, senkou_b=senkou_b, chikou=chikou)


@dataclass
class Sar:
    sar: list
    direction: list


def calculate_parabolic_sar(highs, lows, af_start, af_step, af_max):
    sar = []
    direction = []
    if len(highs) < 2:
        return Sar(sar=[], direction=[])

    is_long = highs[1] > highs[0]
    af = af_start
    ep = highs[0] if is_long else lows[0]
    sar_value = lows[0] if is_long else highs[0]

    for i in range(len(highs)):
        if i < 2:
            sar.append(sar_value)
            direction.append(1.0 if is_long else -1.0)
            continue
        prev_sar = sar_value
        sar_value = prev_sar + af * (ep - prev_sar)
        if is_long:
            # Math.min(sarValue, lows[i-1], lows[i-2]): NaN propagates
            if is_nan(sar_value) or is_nan(lows[i - 1]) or is_nan(lows[i - 2]):
                sar_value = NAN
            else:
                sar_value = _fmin(_fmin(sar_value, lows[i - 1]), lows[i - 2])
            if lows[i] < sar_value:
                is_long = False
                sar_value = ep
                ep = lows[i]
                af = af_start
            elif highs[i] > ep:
                ep = highs[i]
                af = _fmin(af + af_step, af_max)
        else:
            if is_nan(sar_value) or is_nan(highs[i - 1]) or is_nan(highs[i - 2]):
                sar_value = NAN
            else:
                sar_value = _fmax(_fmax(sar_value, highs[i - 1]), highs[i - 2])
            if highs[i] > sar_value:
                is_long = True
                sar_value = ep
                ep = highs[i]
                af = af_start
            elif lows[i] < ep:
                ep = lows[i]
                af = _fmin(af + af_step, af_max)
        sar.append(sar_value)
        direction.append(1.0 if is_long else -1.0)

    return Sar(sar=sar, direction=direction)


def calculate_keltner_channels(highs, lows, closes, ema_period, atr_period, multiplier):
    middle = calculate_ema(closes, ema_period)
    atr = calculate_atr(highs, lows, closes, atr_period)
    upper = []
    lower = []
    for i in range(len(closes)):
        # atr is shorter than closes only when len(highs) < 2, which cannot
        # happen here; guard anyway so a short read gives NaN (JS undefined)
        a = atr[i] if i < len(atr) else NAN
        if is_nan(middle[i]) or is_nan(a):
            upper.append(NAN)
            lower.append(NAN)
        else:
            upper.append(middle[i] + multiplier * a)
            lower.append(middle[i] - multiplier * a)
    return Bands(upper=upper, middle=middle, lower=lower)


@dataclass
class Supertrend:
    supertrend: list
    direction: list


def calculate_supertrend(highs, lows, closes, period, multiplier):
    atr = calculate_atr(highs, lows, closes, period)
    st = []
    dir_ = []
    prev_upper = 0.0
    prev_lower = 0.0

    for i in range(len(closes)):
        a = atr[i] if i < len(atr) else NAN
        if is_nan(a):
            st.append(NAN)
            dir_.append(1.0)
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        upper_band = hl2 + multiplier * a
        lower_band = hl2 - multiplier * a
        if i > 0:
            upper_band = upper_band if (upper_band < prev_upper or closes[i - 1] > prev_upper) else prev_upper
            lower_band = lower_band if (lower_band > prev_lower or closes[i - 1] < prev_lower) else prev_lower
        prev_dir = dir_[i - 1] if i > 0 else 1.0
        if prev_dir == -1.0 and closes[i] > prev_upper:
            d = 1.0
        elif prev_dir == 1.0 and closes[i] < prev_lower:
            d = -1.0
        else:
            d = prev_dir
        st.append(lower_band if d == 1.0 else upper_band)
        dir_.append(d)
        prev_upper = upper_band
        prev_lower = lower_band
    return Supertrend(supertrend=st, direction=dir_)


def calculate_donchian(highs, lows, period):
    upper = []
    middle = []
    lower = []
    for i in range(len(highs)):
        fi = float(i)
        if fi < period - 1.0:
            upper.append(NAN)
            middle.append(NAN)
            lower.append(NAN)
            continue
        hh = _max_of(_js_slice(highs, fi - period + 1.0, fi + 1.0))
        ll = _min_of(_js_slice(lows, fi - period + 1.0, fi + 1.0))
        upper.append(hh)
        lower.append(ll)
        middle.append((hh + ll) / 2.0)
    return Bands(upper=upper, middle=middle, lower=lower)


@dataclass
class Aroon:
    up: list
    down: list
    oscillator: list


def calculate_aroon(highs, lows, period):
    up = []
    down = []
    osc = []
    for i in range(len(highs)):
        fi = float(i)
        if fi < period:
            up.append(NAN)
            down.append(NAN)
            osc.append(NAN)
            continue
        h_slice = _js_slice(highs, fi - period, fi + 1.0)
        l_slice = _js_slice(lows, fi - period, fi + 1.0)
        high_idx = 0.0
        low_idx = 0.0
        j = 0.0
        while j <= period:
            hj = _get_f(h_slice, j)
            hh = _get_f(h_slice, high_idx)
            lj = _get_f(l_slice, j)
            llv = _get_f(l_slice, low_idx)
            if hj >= hh:
                high_idx = j
            if lj <= llv:
                low_idx = j
            j += 1.0
        u = _fdiv(high_idx, period) * 100.0
        d = _fdiv(low_idx, period) * 100.0
        up.append(u)
        down.append(d)
        osc.append(u - d)
    return Aroon(up=up, down=down, oscillator=osc)


def calculate_envelopes(closes, period, percent):
    middle = calculate_sma(closes, period)
    factor = percent / 100.0
    upper = []
    lower = []
    for v in middle:
        upper.append(NAN if is_nan(v) else v * (1.0 + factor))
        lower.append(NAN if is_nan(v) else v * (1.0 - factor))
    return Bands(upper=upper, middle=middle, lower=lower)


def calculate_dema(data, period):
    ema1 = calculate_ema(data, period)
    ema2 = calculate_ema(_filter_valid(ema1), period)
    result = []
    e2i = 0
    for i in range(len(data)):
        if is_nan(ema1[i]) or e2i >= len(ema2) or is_nan(ema2[e2i]):
            result.append(NAN)
            if not is_nan(ema1[i]):
                e2i += 1
        else:
            result.append(2.0 * ema1[i] - ema2[e2i])
            e2i += 1
    return result


def calculate_tema(data, period):
    ema1 = calculate_ema(data, period)
    valid1 = _filter_valid(ema1)
    ema2 = calculate_ema(valid1, period)
    valid2 = _filter_valid(ema2)
    ema3 = calculate_ema(valid2, period)

    result = [NAN] * len(data)
    offset1 = _find_first_valid(ema1)
    offset2 = offset1 + _find_first_valid(ema2)
    offset3 = offset2 + _find_first_valid(ema3)

    for i in range(len(ema3)):
        if not is_nan(ema3[i]):
            idx = offset3 + i
            if 0 <= idx < len(data):
                e1 = ema1[idx]
                e2 = _get_f(ema2, float(idx) - float(offset1))
                e3 = ema3[i]
                if not is_nan(e1) and not is_nan(e2) and not is_nan(e3):
                    result[idx] = 3.0 * e1 - 3.0 * e2 + e3
    return result


def calculate_wma(data, period):
    result = []
    denom = (period * (period + 1.0)) / 2.0
    for i in range(len(data)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        sum_ = 0.0
        valid = True
        j = 0.0
        while j < period:
            val = _get_f(data, fi - period + 1.0 + j)
            if is_nan(val):
                valid = False
                break
            sum_ += val * (j + 1.0)
            j += 1.0
        result.append(_fdiv(sum_, denom) if valid else NAN)
    return result


def calculate_hma(data, period):
    half_period = _floor(period / 2.0)
    sqrt_period = _floor(_sqrt(period))
    wma1 = calculate_wma(data, half_period)
    wma2 = calculate_wma(data, period)
    diff = []
    for i in range(len(data)):
        diff.append(NAN if (is_nan(wma1[i]) or is_nan(wma2[i])) else 2.0 * wma1[i] - wma2[i])
    return calculate_wma(diff, sqrt_period)


def calculate_momentum(closes, period):
    out = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            out.append(NAN)
        else:
            out.append(closes[i] - _get_f(closes, fi - period))
    return out


def calculate_awesome_oscillator(highs, lows):
    median_price = []
    for h, l in zip(highs, lows):
        median_price.append((h + l) / 2.0)
    sma5 = calculate_sma(median_price, 5.0)
    sma34 = calculate_sma(median_price, 34.0)
    out = []
    for v, s in zip(sma5, sma34):
        out.append(NAN if (is_nan(v) or is_nan(s)) else v - s)
    return out


def calculate_mfi(highs, lows, closes, volumes, period):
    mfi = []
    tp = []
    for i in range(len(highs)):
        tp.append((highs[i] + lows[i] + closes[i]) / 3.0)
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            mfi.append(NAN)
            continue
        pos_flow = 0.0
        neg_flow = 0.0
        j = fi - period + 1.0
        while j <= fi:
            tpj = _get_f(tp, j)
            tpj1 = _get_f(tp, j - 1.0)
            if j >= 0.0 and math.isfinite(j) and j == math.trunc(j) and int(j) < len(volumes):
                vol = _v_or0(volumes[int(j)])
            else:
                vol = 0.0  # volumes[undefined] || 0
            raw_mf = tpj * vol
            if tpj > tpj1:
                pos_flow += raw_mf
            elif tpj < tpj1:
                neg_flow += raw_mf
            j += 1.0
        mfi.append(100.0 if neg_flow == 0.0 else 100.0 - _fdiv(100.0, 1.0 + _fdiv(pos_flow, neg_flow)))
    return mfi


def _ema_nan_safe(data, period):
    """NaN-safe EMA re-smoothing helper used by TSI"""
    first = _find_first_valid(data)
    if first < 0:
        return list(data)
    v = [NAN] * first
    v.extend(calculate_ema(data[first:], period))
    return v


@dataclass
class TwoLine:
    a: list
    b: list


def calculate_tsi(closes, long_period, short_period, signal_period):
    mom_arr = []
    abs_mom = []
    for i in range(len(closes)):
        if i == 0:
            mom_arr.append(0.0)
            abs_mom.append(0.0)
            continue
        m = closes[i] - closes[i - 1]
        mom_arr.append(m)
        abs_mom.append(abs(m))
    ds = _ema_nan_safe(calculate_ema(mom_arr, long_period), short_period)
    ads = _ema_nan_safe(calculate_ema(abs_mom, long_period), short_period)
    tsi_vals = []
    for v, a in zip(ds, ads):
        tsi_vals.append(NAN if (is_nan(v) or is_nan(a) or a == 0.0) else _fdiv(v, a) * 100.0)
    sig = calculate_ema(_filter_valid(tsi_vals), signal_period)
    signal = []
    si = 0
    for v in tsi_vals:
        if is_nan(v):
            signal.append(NAN)
        elif si < len(sig):
            signal.append(sig[si])
            si += 1
        else:
            signal.append(NAN)
    return TwoLine(a=tsi_vals, b=signal)


def calculate_trix(closes, period, signal_period):
    ema1 = calculate_ema(closes, period)
    ema2 = calculate_ema(_filter_valid(ema1), period)
    ema3 = calculate_ema(_filter_valid(ema2), period)
    trix_vals = [NAN] * len(closes)
    start_offset = len(closes) - len(ema3)
    for i in range(1, len(ema3)):
        if not is_nan(ema3[i]) and not is_nan(ema3[i - 1]) and ema3[i - 1] != 0.0:
            idx = start_offset + i
            if 0 <= idx < len(trix_vals):
                trix_vals[idx] = _fdiv(ema3[i] - ema3[i - 1], ema3[i - 1]) * 10000.0
    valid_trix = _filter_valid(trix_vals)
    sig_ema = calculate_ema(valid_trix, signal_period)
    signal = [NAN] * len(closes)
    si = 0
    for i in range(len(closes)):
        if not is_nan(trix_vals[i]):
            if si < len(sig_ema):
                signal[i] = sig_ema[si]
                si += 1
    return TwoLine(a=trix_vals, b=signal)


def calculate_ultimate_oscillator(highs, lows, closes, fast, med, slow):
    uo = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < slow:
            uo.append(NAN)
            continue
        bp7 = 0.0
        tr7 = 0.0
        bp14 = 0.0
        tr14 = 0.0
        bp28 = 0.0
        tr28 = 0.0
        j = fi - slow + 1.0
        while j <= fi:
            prev_c = _get_f(closes, j - 1.0)
            cj = _get_f(closes, j)
            lj = _get_f(lows, j)
            hj = _get_f(highs, j)
            min_lc = NAN if (is_nan(lj) or is_nan(prev_c)) else _fmin(lj, prev_c)
            max_hc = NAN if (is_nan(hj) or is_nan(prev_c)) else _fmax(hj, prev_c)
            bp_val = cj - min_lc
            tr_val = max_hc - min_lc
            if j > fi - fast:
                bp7 += bp_val
                tr7 += tr_val
            if j > fi - med:
                bp14 += bp_val
                tr14 += tr_val
            bp28 += bp_val
            tr28 += tr_val
            j += 1.0
        avg1 = 0.0 if tr7 == 0.0 else _fdiv(bp7, tr7)
        avg2 = 0.0 if tr14 == 0.0 else _fdiv(bp14, tr14)
        avg3 = 0.0 if tr28 == 0.0 else _fdiv(bp28, tr28)
        uo.append(((4.0 * avg1 + 2.0 * avg2 + avg3) / 7.0) * 100.0)
    return uo


def calculate_dpo(closes, period):
    sma = calculate_sma(closes, period)
    shift = _floor(period / 2.0) + 1.0
    out = []
    for i in range(len(closes)):
        sma_idx = float(i) + shift
        v = _get_f(sma, sma_idx)
        if sma_idx < len(sma) and not is_nan(v):
            out.append(closes[i] - v)
        else:
            out.append(NAN)
    return out


def _sma_nan_safe(data, period):
    first = _find_first_valid(data)
    if first < 0:
        return list(data)
    v = [NAN] * first
    v.extend(calculate_sma(data[first:], period))
    return v


def calculate_kst(closes):
    roc1_p, roc2_p, roc3_p, roc4_p = (10.0, 15.0, 20.0, 30.0)
    sma1, sma2, sma3, sma4 = (10.0, 10.0, 10.0, 15.0)
    signal_period = 9.0
    r1 = _sma_nan_safe(calculate_roc(closes, roc1_p), sma1)
    r2 = _sma_nan_safe(calculate_roc(closes, roc2_p), sma2)
    r3 = _sma_nan_safe(calculate_roc(closes, roc3_p), sma3)
    r4 = _sma_nan_safe(calculate_roc(closes, roc4_p), sma4)
    kst_vals = []
    for i in range(len(closes)):
        if is_nan(r1[i]) or is_nan(r2[i]) or is_nan(r3[i]) or is_nan(r4[i]):
            kst_vals.append(NAN)
        else:
            kst_vals.append(r1[i] * 1.0 + r2[i] * 2.0 + r3[i] * 3.0 + r4[i] * 4.0)
    valid_kst = _filter_valid(kst_vals)
    sig_sma = calculate_sma(valid_kst, signal_period)
    signal = [NAN] * len(closes)
    si = 0
    for i in range(len(closes)):
        if not is_nan(kst_vals[i]):
            if si < len(sig_sma):
                signal[i] = sig_sma[si]
                si += 1
    return TwoLine(a=kst_vals, b=signal)


def calculate_stoch_rsi(closes, rsi_period, k_period, d_period):
    rsi_vals = calculate_rsi(closes, rsi_period)
    k = []
    for i in range(len(rsi_vals)):
        fi = float(i)
        if is_nan(rsi_vals[i]) or fi < rsi_period + k_period - 1.0:
            k.append(NAN)
            continue
        valid_count = 0.0
        max_ = -INF
        min_ = INF
        j = fi - k_period + 1.0
        while j <= fi:
            v = _get_f(rsi_vals, j)
            if not is_nan(v):
                valid_count += 1.0
                if v > max_:
                    max_ = v
                if v < min_:
                    min_ = v
            j += 1.0
        if valid_count < k_period:
            k.append(NAN)
            continue
        k.append(50.0 if max_ == min_ else _fdiv(rsi_vals[i] - min_, max_ - min_) * 100.0)
    d = calculate_sma(k, d_period)
    return Stoch(k=k, d=d)


def calculate_bb_percent(closes, period, std_dev):
    bb = calculate_bollinger_bands(closes, period, std_dev)
    out = []
    for i in range(len(closes)):
        c = closes[i]
        if is_nan(bb.upper[i]) or is_nan(bb.lower[i]):
            out.append(NAN)
            continue
        range_ = bb.upper[i] - bb.lower[i]
        out.append(0.5 if range_ == 0.0 else _fdiv(c - bb.lower[i], range_))
    return out


def calculate_bb_width(closes, period, std_dev):
    bb = calculate_bollinger_bands(closes, period, std_dev)
    out = []
    for i in range(len(bb.upper)):
        u = bb.upper[i]
        if is_nan(u) or is_nan(bb.lower[i]) or is_nan(bb.middle[i]) or bb.middle[i] == 0.0:
            out.append(NAN)
        else:
            out.append(_fdiv(u - bb.lower[i], bb.middle[i]) * 100.0)
    return out


def calculate_historical_volatility(closes, period):
    hv = []
    returns = [NAN]
    for i in range(1, len(closes)):
        returns.append(0.0 if closes[i - 1] == 0.0 else js_log(_fdiv(closes[i], closes[i - 1])))
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            hv.append(NAN)
            continue
        sum_ = 0.0
        valid_count = 0.0
        j = fi - period + 1.0
        while j <= fi:
            r = _get_f(returns, j)
            if not is_nan(r):
                sum_ += r
                valid_count += 1.0
            j += 1.0
        if valid_count < 2.0:
            hv.append(NAN)
            continue
        mean = _fdiv(sum_, valid_count)
        sum_sq = 0.0
        j = fi - period + 1.0
        while j <= fi:
            r = _get_f(returns, j)
            if not is_nan(r):
                sum_sq += js_pow(r - mean, 2.0)
            j += 1.0
        std_dev = _sqrt(_fdiv(sum_sq, valid_count - 1.0))
        hv.append(std_dev * math.sqrt(252.0) * 100.0)
    return hv


def calculate_chaikin_volatility(highs, lows, ema_period, roc_period):
    hl_range = []
    for h, l in zip(highs, lows):
        hl_range.append(h - l)
    ema = calculate_ema(hl_range, ema_period)
    out = []
    for i in range(len(ema)):
        fi = float(i)
        v = ema[i]
        prev = _get_f(ema, fi - roc_period)
        if is_nan(v) or fi < roc_period or is_nan(prev) or prev == 0.0:
            out.append(NAN)
        else:
            out.append(_fdiv(v - prev, prev) * 100.0)
    return out


def calculate_std_dev(closes, period):
    result = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        sum_ = 0.0
        j = fi - period + 1.0
        while j <= fi:
            sum_ += _get_f(closes, j)
            j += 1.0
        mean = _fdiv(sum_, period)
        variance = 0.0
        j = fi - period + 1.0
        while j <= fi:
            d = _get_f(closes, j) - mean
            variance += d * d  # JS x**2 is x*x for f64
            j += 1.0
        result.append(_sqrt(_fdiv(variance, period)))
    return result


def calculate_obv(closes, volumes):
    obv = [_v_or0(volumes[0] if volumes else 0.0)]
    for i in range(1, len(closes)):
        vol = _v_or0(volumes[i] if i < len(volumes) else NAN)
        if closes[i] > closes[i - 1]:
            obv.append(obv[i - 1] + vol)
        elif closes[i] < closes[i - 1]:
            obv.append(obv[i - 1] - vol)
        else:
            obv.append(obv[i - 1])
    return obv


def calculate_cmf(highs, lows, closes, volumes, period):
    cmf = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            cmf.append(NAN)
            continue
        mfv_sum = 0.0
        vol_sum = 0.0
        j = fi - period + 1.0
        while j <= fi:
            hj = _get_f(highs, j)
            lj = _get_f(lows, j)
            cj = _get_f(closes, j)
            hl = hj - lj
            mfm = 0.0 if hl == 0.0 else _fdiv((cj - lj) - (hj - cj), hl)
            if j >= 0.0 and math.isfinite(j) and j == math.trunc(j) and int(j) < len(volumes):
                vol = _v_or0(volumes[int(j)])
            else:
                vol = 0.0
            mfv_sum += mfm * vol
            vol_sum += vol
            j += 1.0
        cmf.append(0.0 if vol_sum == 0.0 else _fdiv(mfv_sum, vol_sum))
    return cmf


def calculate_adl(highs, lows, closes, volumes):
    adl = []
    cum = 0.0
    for i in range(len(closes)):
        hl = highs[i] - lows[i]
        mfm = 0.0 if hl == 0.0 else _fdiv((closes[i] - lows[i]) - (highs[i] - closes[i]), hl)
        cum += mfm * _v_or0(volumes[i])
        adl.append(cum)
    return adl


def calculate_force_index(closes, volumes, period):
    raw = [0.0]
    for i in range(1, len(closes)):
        raw.append((closes[i] - closes[i - 1]) * _v_or0(volumes[i]))
    return calculate_ema(raw, period)


def calculate_eom(highs, lows, volumes, period):
    raw = [0.0]
    for i in range(1, len(highs)):
        dm = (highs[i] + lows[i]) / 2.0 - (highs[i - 1] + lows[i - 1]) / 2.0
        v = volumes[i]
        vol = 1.0 if (is_nan(v) or v == 0.0) else v
        r = highs[i] - lows[i]
        range_ = 1.0 if (is_nan(r) or r == 0.0) else r
        br = _fdiv(vol, range_)
        raw.append(_fdiv(dm, br))
    return calculate_sma(raw, period)


def calculate_volume_sma(volumes, period):
    return calculate_sma(volumes, period)


def calculate_correlation(data1, data2, period):
    result = []
    for i in range(len(data1)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        x = _js_slice(data1, fi - period + 1.0, fi + 1.0)
        y = _js_slice(data2, fi - period + 1.0, fi + 1.0)
        sum_x_vals = 0.0
        for b in x:
            sum_x_vals = sum_x_vals + b
        mx = _fdiv(sum_x_vals, period)
        sum_y_vals = 0.0
        for b in y:
            sum_y_vals = sum_y_vals + b
        my = _fdiv(sum_y_vals, period)
        num = 0.0
        dx = 0.0
        dy = 0.0
        j = 0.0
        while j < period:
            xv = _get_f(x, j)
            yv = _get_f(y, j)
            num += (xv - mx) * (yv - my)
            dx += js_pow(xv - mx, 2.0)
            dy += js_pow(yv - my, 2.0)
            j += 1.0
        denom = _sqrt(dx * dy)
        result.append(0.0 if denom == 0.0 else _fdiv(num, denom))
    return result


def calculate_linear_regression(closes, period, deviations):
    middle = []
    upper = []
    lower = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            middle.append(NAN)
            upper.append(NAN)
            lower.append(NAN)
            continue
        slice_ = _js_slice(closes, fi - period + 1.0, fi + 1.0)
        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0
        j = 0.0
        while j < period:
            sj = _get_f(slice_, j)
            sum_x += j
            sum_y += sj
            sum_xy += j * sj
            sum_x2 += j * j
            j += 1.0
        slope = _fdiv(period * sum_xy - sum_x * sum_y, period * sum_x2 - sum_x * sum_x)
        intercept = _fdiv(sum_y - slope * sum_x, period)
        reg_value = intercept + slope * (period - 1.0)
        mse = 0.0
        j = 0.0
        while j < period:
            sj = _get_f(slice_, j)
            mse += js_pow(sj - (intercept + slope * j), 2.0)
            j += 1.0
        std_err = _sqrt(_fdiv(mse, period))
        middle.append(reg_value)
        upper.append(reg_value + deviations * std_err)
        lower.append(reg_value - deviations * std_err)
    return Bands(upper=upper, middle=middle, lower=lower)


def calculate_coppock(closes):
    long_roc, short_roc, wma_period = (14.0, 11.0, 10.0)
    roc1 = calculate_roc(closes, long_roc)
    roc2 = calculate_roc(closes, short_roc)
    combined = []
    for v, r2 in zip(roc1, roc2):
        combined.append(NAN if (is_nan(v) or is_nan(r2)) else v + r2)
    return calculate_wma(combined, wma_period)


def calculate_alma(data, period, offset, sigma):
    result = []
    m = offset * (period - 1.0)
    s = _fdiv(period, sigma)
    for i in range(len(data)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        norm = 0.0
        sum_ = 0.0
        j = 0.0
        while j < period:
            w = js_exp(_fdiv(-((j - m) * (j - m)), 2.0 * s * s))
            norm += w
            sum_ += _get_f(data, fi - period + 1.0 + j) * w
            j += 1.0
        result.append(_fdiv(sum_, norm))
    return result


def calculate_kama(data, period, fast_period, slow_period):
    result = []
    fast_sc = _fdiv(2.0, fast_period + 1.0)
    slow_sc = _fdiv(2.0, slow_period + 1.0)
    for i in range(len(data)):
        fi = float(i)
        if fi < period:
            result.append(NAN)
            continue
        direction = abs(data[i] - _get_f(data, fi - period))
        volatility = 0.0
        j = fi - period + 1.0
        while j <= fi:
            volatility += abs(_get_f(data, j) - _get_f(data, j - 1.0))
            j += 1.0
        er = 0.0 if volatility == 0.0 else _fdiv(direction, volatility)
        sc = js_pow(er * (fast_sc - slow_sc) + slow_sc, 2.0)
        prev = data[i] if is_nan(result[i - 1]) else result[i - 1]
        result.append(prev + sc * (data[i] - prev))
    return result


def calculate_zlema(data, period):
    lag = _floor((period - 1.0) / 2.0)
    adjusted = []
    for i in range(len(data)):
        fi = float(i)
        if fi >= lag:
            adjusted.append(2.0 * data[i] - _get_f(data, fi - lag))
        else:
            adjusted.append(data[i])
    return calculate_ema(adjusted, period)


def calculate_t3(data, period, v_factor):
    def nan_to_zero(v):
        return [0.0 if is_nan(x) else x for x in v]

    e1 = calculate_ema(data, period)
    e2 = calculate_ema(nan_to_zero(e1), period)
    e3 = calculate_ema(nan_to_zero(e2), period)
    e4 = calculate_ema(nan_to_zero(e3), period)
    e5 = calculate_ema(nan_to_zero(e4), period)
    e6 = calculate_ema(nan_to_zero(e5), period)
    c1 = -(v_factor * v_factor * v_factor)
    c2 = 3.0 * v_factor * v_factor + 3.0 * v_factor * v_factor * v_factor
    c3 = -6.0 * v_factor * v_factor - 3.0 * v_factor - 3.0 * v_factor * v_factor * v_factor
    c4 = 1.0 + 3.0 * v_factor + v_factor * v_factor * v_factor + 3.0 * v_factor * v_factor
    out = []
    for i in range(len(e3)):
        if i >= len(e6) or is_nan(e6[i]):
            out.append(NAN)
        else:
            out.append(c1 * e6[i] + c2 * e5[i] + c3 * e4[i] + c4 * e3[i])
    return out


def calculate_lsma(data, period):
    result = []
    for i in range(len(data)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0
        j = 0.0
        while j < period:
            dj = _get_f(data, fi - period + 1.0 + j)
            sum_x += j
            sum_y += dj
            sum_xy += j * dj
            sum_x2 += j * j
            j += 1.0
        slope = _fdiv(period * sum_xy - sum_x * sum_y, period * sum_x2 - sum_x * sum_x)
        intercept = _fdiv(sum_y - slope * sum_x, period)
        result.append(intercept + slope * (period - 1.0))
    return result


def calculate_mcginley(data, period):
    if not data:
        # [data[0]] with data[0] undefined is [NaN]; callers never pass empty
        return [NAN]
    result = [data[0]]
    for i in range(1, len(data)):
        prev = result[i - 1]
        if is_nan(prev):
            result.append(data[i])
            continue
        ratio = _fdiv(data[i], prev)
        result.append(prev + _fdiv(data[i] - prev, period * js_pow(ratio, 4.0)))
    return result


@dataclass
class Vortex:
    vi_plus: list
    vi_minus: list


def calculate_vortex(highs, lows, closes, period):
    vi_plus = []
    vi_minus = []
    for i in range(len(highs)):
        fi = float(i)
        if fi < period:
            vi_plus.append(NAN)
            vi_minus.append(NAN)
            continue
        vm_plus = 0.0
        vm_minus = 0.0
        tr = 0.0
        j = fi - period + 1.0
        while j <= fi:
            hj = _get_f(highs, j)
            lj = _get_f(lows, j)
            cj1 = _get_f(closes, j - 1.0)
            lj1 = _get_f(lows, j - 1.0)
            hj1 = _get_f(highs, j - 1.0)
            vm_plus += abs(hj - lj1)
            vm_minus += abs(lj - hj1)
            tr += _js_max3(hj - lj, abs(hj - cj1), abs(lj - cj1))
            j += 1.0
        vi_plus.append(0.0 if tr == 0.0 else _fdiv(vm_plus, tr))
        vi_minus.append(0.0 if tr == 0.0 else _fdiv(vm_minus, tr))
    return Vortex(vi_plus=vi_plus, vi_minus=vi_minus)


def calculate_choppiness(highs, lows, closes, period):
    result = []
    for i in range(len(highs)):
        fi = float(i)
        if fi < period:
            result.append(NAN)
            continue
        atr_sum = 0.0
        j = fi - period + 1.0
        while j <= fi:
            hj = _get_f(highs, j)
            lj = _get_f(lows, j)
            cj1 = _get_f(closes, j - 1.0)
            atr_sum += _js_max3(hj - lj, abs(hj - cj1), abs(lj - cj1))
            j += 1.0
        hh = _max_of(_js_slice(highs, fi - period + 1.0, fi + 1.0))
        ll = _min_of(_js_slice(lows, fi - period + 1.0, fi + 1.0))
        range_ = hh - ll
        if range_ == 0.0:
            result.append(50.0)
        else:
            result.append(_fdiv(100.0 * js_log10(_fdiv(atr_sum, range_)), js_log10(period)))
    return result


@dataclass
class ElderRay:
    bull_power: list
    bear_power: list


def calculate_elder_ray(highs, lows, closes, period):
    ema = calculate_ema(closes, period)
    bull_power = []
    for i in range(len(highs)):
        bull_power.append(NAN if is_nan(ema[i]) else highs[i] - ema[i])
    bear_power = []
    for i in range(len(lows)):
        bear_power.append(NAN if is_nan(ema[i]) else lows[i] - ema[i])
    return ElderRay(bull_power=bull_power, bear_power=bear_power)


def calculate_mass_index(highs, lows, period):
    hl_range = []
    for h, l in zip(highs, lows):
        hl_range.append(h - l)
    ema1 = calculate_ema(hl_range, 9.0)
    ema1_zeroed = [0.0 if is_nan(v) else v for v in ema1]
    ema2 = calculate_ema(ema1_zeroed, 9.0)
    ratio = []
    for i in range(len(ema1)):
        v = ema1[i]
        if is_nan(v) or is_nan(ema2[i]) or ema2[i] == 0.0:
            ratio.append(NAN)
        else:
            ratio.append(_fdiv(v, ema2[i]))
    result = []
    for i in range(len(ratio)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        sum_ = 0.0
        valid = True
        j = fi - period + 1.0
        while j <= fi:
            r = _get_f(ratio, j)
            if is_nan(r):
                valid = False
                break
            sum_ += r
            j += 1.0
        result.append(sum_ if valid else NAN)
    return result


@dataclass
class ChandeKroll:
    stop_long: list
    stop_short: list


def calculate_chande_kroll_stop(highs, lows, closes, p, q, x):
    atr = calculate_atr(highs, lows, closes, p)
    first_high_stop = []
    first_low_stop = []
    for i in range(len(closes)):
        fi = float(i)
        a = atr[i] if i < len(atr) else NAN
        if is_nan(a):
            first_high_stop.append(NAN)
            first_low_stop.append(NAN)
            continue
        start = _fmax(fi - p + 1.0, 0.0)
        first_high_stop.append(_max_of(_js_slice(highs, start, fi + 1.0)) - x * a)
        first_low_stop.append(_min_of(_js_slice(lows, start, fi + 1.0)) + x * a)
    stop_long = []
    stop_short = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < q - 1.0 or is_nan(first_high_stop[i]):
            stop_long.append(NAN)
            stop_short.append(NAN)
            continue
        s = _fmax(fi - q + 1.0, 0.0)
        valid_h = _filter_valid(_js_slice(first_high_stop, s, fi + 1.0))
        valid_l = _filter_valid(_js_slice(first_low_stop, s, fi + 1.0))
        stop_long.append(_max_of(valid_h) if valid_h else NAN)
        stop_short.append(_min_of(valid_l) if valid_l else NAN)
    return ChandeKroll(stop_long=stop_long, stop_short=stop_short)


def calculate_lin_reg_slope(data, period):
    result = []
    for i in range(len(data)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0
        j = 0.0
        while j < period:
            dj = _get_f(data, fi - period + 1.0 + j)
            sum_x += j
            sum_y += dj
            sum_xy += j * dj
            sum_x2 += j * j
            j += 1.0
        result.append(_fdiv(period * sum_xy - sum_x * sum_y, period * sum_x2 - sum_x * sum_x))
    return result


def calculate_price_channel(highs, lows, period):
    upper = []
    middle = []
    lower = []
    for i in range(len(highs)):
        fi = float(i)
        if fi < period:
            upper.append(NAN)
            middle.append(NAN)
            lower.append(NAN)
            continue
        hh = _max_of(_js_slice(highs, fi - period, fi))
        ll = _min_of(_js_slice(lows, fi - period, fi))
        upper.append(hh)
        lower.append(ll)
        middle.append((hh + ll) / 2.0)
    return Bands(upper=upper, middle=middle, lower=lower)


@dataclass
class Ppo:
    ppo: list
    signal: list
    histogram: list


def calculate_ppo(closes, fast, slow, signal):
    fast_ema = calculate_ema(closes, fast)
    slow_ema = calculate_ema(closes, slow)
    ppo_line = []
    for f, s in zip(fast_ema, slow_ema):
        if is_nan(f) or is_nan(s) or s == 0.0:
            ppo_line.append(NAN)
        else:
            ppo_line.append(_fdiv(f - s, s) * 100.0)
    valid_ppo = _filter_valid(ppo_line)
    sig_ema = calculate_ema(valid_ppo, signal)
    sig_line = [NAN] * len(closes)
    si = 0
    for i in range(len(closes)):
        if not is_nan(ppo_line[i]):
            if si < len(sig_ema):
                sig_line[i] = sig_ema[si]
                si += 1
    hist = []
    for v, s in zip(ppo_line, sig_line):
        hist.append(NAN if (is_nan(v) or is_nan(s)) else v - s)
    return Ppo(ppo=ppo_line, signal=sig_line, histogram=hist)


def calculate_pvo(volumes, fast, slow, signal):
    return calculate_ppo(volumes, fast, slow, signal)


def calculate_cmo(closes, period):
    result = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            result.append(NAN)
            continue
        sum_up = 0.0
        sum_down = 0.0
        j = fi - period + 1.0
        while j <= fi:
            diff = _get_f(closes, j) - _get_f(closes, j - 1.0)
            if diff > 0.0:
                sum_up += diff
            else:
                sum_down += abs(diff)
            j += 1.0
        total = sum_up + sum_down
        result.append(0.0 if total == 0.0 else _fdiv(sum_up - sum_down, total) * 100.0)
    return result


@dataclass
class Fisher:
    fisher: list
    trigger: list


def calculate_fisher(highs, lows, period):
    fisher = []
    trigger = []
    val = 0.0
    prev_fish = 0.0
    for i in range(len(highs)):
        fi = float(i)
        if fi < period - 1.0:
            fisher.append(NAN)
            trigger.append(NAN)
            continue
        hh = _max_of(_js_slice(highs, fi - period + 1.0, fi + 1.0))
        ll = _min_of(_js_slice(lows, fi - period + 1.0, fi + 1.0))
        mid = (highs[i] + lows[i]) / 2.0
        range_ = hh - ll
        raw = 0.0 if range_ == 0.0 else 0.33 * 2.0 * (_fdiv(mid - ll, range_) - 0.5) + 0.67 * val
        # clamp to [-0.999, 0.999]: NaN stays NaN (comparisons are false)
        val = raw
        if val < -0.999:
            val = -0.999
        elif val > 0.999:
            val = 0.999
        # Math.max(-0.999, Math.min(0.999, raw)) with NaN: min(0.999, NaN)=NaN, max(-0.999,NaN)=NaN
        if is_nan(raw):
            val = NAN
        fish = 0.5 * js_log(_fdiv(1.0 + val, 1.0 - val)) + 0.5 * prev_fish
        trigger.append(prev_fish)
        fisher.append(fish)
        prev_fish = fish
    return Fisher(fisher=fisher, trigger=trigger)


def calculate_stc(closes, fast, slow, cycle):
    smoothing = 3.0
    # The slow EMA is the same for every element, so compute it once.
    fast_ema = calculate_ema(closes, fast)
    slow_ema = calculate_ema(closes, slow)
    macd = []
    for f, s in zip(fast_ema, slow_ema):
        macd.append(NAN if (is_nan(f) or is_nan(s)) else f - s)

    def stoch1(data):
        r = []
        for i in range(len(data)):
            fi = float(i)
            if fi < cycle - 1.0 or is_nan(data[i]):
                r.append(NAN)
                continue
            sl = _filter_valid(_js_slice(data, fi - cycle + 1.0, fi + 1.0))
            if len(sl) < 2:
                r.append(NAN)
                continue
            max_ = _max_of(sl)
            min_ = _min_of(sl)
            r.append(50.0 if max_ == min_ else _fdiv(data[i] - min_, max_ - min_) * 100.0)
        return r

    def smooth_pass(pf, n):
        pf = list(pf)
        for _ in range(int(smoothing)):
            e = calculate_ema(_filter_valid(pf), smoothing)
            mapped = [NAN] * n
            idx = 0
            for i in range(len(pf)):
                if not is_nan(pf[i]) and idx < len(e):
                    mapped[i] = e[idx]
                    idx += 1
            pf = mapped
        return pf

    pf = smooth_pass(stoch1(macd), len(closes))
    return smooth_pass(stoch1(pf), len(closes))


def calculate_rvi(opens, highs, lows, closes, period):
    num = []
    den = []
    for i in range(len(closes)):
        if i < 3:
            num.append(0.0)
            den.append(0.0)
            continue
        num.append(
            ((closes[i] - opens[i])
             + 2.0 * (closes[i - 1] - opens[i - 1])
             + 2.0 * (closes[i - 2] - opens[i - 2])
             + (closes[i - 3] - opens[i - 3]))
            / 6.0
        )
        den.append(
            ((highs[i] - lows[i])
             + 2.0 * (highs[i - 1] - lows[i - 1])
             + 2.0 * (highs[i - 2] - lows[i - 2])
             + (highs[i - 3] - lows[i - 3]))
            / 6.0
        )
    rvi = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period + 2.0:
            rvi.append(NAN)
            continue
        sn = 0.0
        sd = 0.0
        j = fi - period + 1.0
        while j <= fi:
            sn += _get_f(num, j)
            sd += _get_f(den, j)
            j += 1.0
        rvi.append(0.0 if sd == 0.0 else _fdiv(sn, sd))
    signal = []
    for i in range(len(rvi)):
        if i < 3 or is_nan(rvi[i]):
            signal.append(NAN)
            continue
        signal.append((rvi[i] + 2.0 * rvi[i - 1] + 2.0 * rvi[i - 2] + rvi[i - 3]) / 6.0)
    return TwoLine(a=rvi, b=signal)


def calculate_klinger(highs, lows, closes, volumes, fast, slow, signal):
    vf = [0.0]
    for i in range(1, len(closes)):
        hlc = highs[i] + lows[i] + closes[i]
        prev_hlc = highs[i - 1] + lows[i - 1] + closes[i - 1]
        trend = 1.0 if hlc > prev_hlc else -1.0
        dm = highs[i] - lows[i]
        if i > 1 and (hlc > prev_hlc) == (prev_hlc > (highs[i - 2] + lows[i - 2] + closes[i - 2])):
            cm = dm + abs(dm) if vf[i - 1] != 0.0 else dm
        else:
            cm = dm
        vf.append(0.0 if cm == 0.0 else _v_or0(volumes[i]) * abs(_fdiv(2.0 * dm, cm) - 1.0) * trend)
    fast_ema = calculate_ema(vf, fast)
    slow_ema = calculate_ema(vf, slow)
    kl = []
    for f, s in zip(fast_ema, slow_ema):
        kl.append(NAN if (is_nan(f) or is_nan(s)) else f - s)
    valid_kl = _filter_valid(kl)
    sig_ema = calculate_ema(valid_kl, signal)
    sig = [NAN] * len(closes)
    si = 0
    for i in range(len(kl)):
        if not is_nan(kl[i]) and si < len(sig_ema):
            sig[i] = sig_ema[si]
            si += 1
    return TwoLine(a=kl, b=sig)


def calculate_connors_rsi(closes, rsi_period, streak_period, rank_period):
    rsi = calculate_rsi(closes, rsi_period)
    streak = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            streak.append(streak[i - 1] + 1.0 if streak[i - 1] > 0.0 else 1.0)
        elif closes[i] < closes[i - 1]:
            streak.append(streak[i - 1] - 1.0 if streak[i - 1] < 0.0 else -1.0)
        else:
            streak.append(0.0)
    streak_rsi = calculate_rsi(streak, streak_period)
    pct_rank = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < rank_period:
            pct_rank.append(NAN)
            continue
        change = closes[i] - closes[i - 1]
        count = 0.0
        j = fi - rank_period
        while j < fi:
            if _get_f(closes, j + 1.0) - _get_f(closes, j) < change:
                count += 1.0
            j += 1.0
        pct_rank.append(_fdiv(count, rank_period) * 100.0)
    # rsi and streak_rsi may be SHORTER than closes (calculateRSI returns []
    # for tiny inputs); undefined reads become NaN
    out = []
    for i in range(len(closes)):
        r = rsi[i] if i < len(rsi) else NAN
        s = streak_rsi[i] if i < len(streak_rsi) else NAN
        p = pct_rank[i]
        if is_nan(r) or is_nan(s) or is_nan(p):
            out.append(NAN)
        else:
            out.append((r + s + p) / 3.0)
    return out


def calculate_apo(closes, fast, slow):
    f = calculate_ema(closes, fast)
    s = calculate_ema(closes, slow)
    out = []
    for v, sv in zip(f, s):
        out.append(NAN if (is_nan(v) or is_nan(sv)) else v - sv)
    return out


def calculate_qstick(opens, closes, period):
    co = []
    for c, o in zip(closes, opens):
        co.append(c - o)
    return calculate_sma(co, period)


def calculate_bop(opens, highs, lows, closes, period):
    raw = []
    for i in range(len(closes)):
        c = closes[i]
        hl = highs[i] - lows[i]
        raw.append(0.0 if hl == 0.0 else _fdiv(c - opens[i], hl))
    return calculate_sma(raw, period)


def calculate_psych_line(closes, period):
    result = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            result.append(NAN)
            continue
        ups = 0.0
        j = fi - period + 1.0
        while j <= fi:
            if _get_f(closes, j) > _get_f(closes, j - 1.0):
                ups += 1.0
            j += 1.0
        result.append(_fdiv(ups, period) * 100.0)
    return result


def calculate_pfe(closes, period, smoothing):
    raw = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            raw.append(NAN)
            continue
        cp = _get_f(closes, fi - period)
        price_move = _sqrt(js_pow(closes[i] - cp, 2.0) + period * period)
        path_length = 0.0
        j = fi - period + 1.0
        while j <= fi:
            path_length += _sqrt(1.0 + js_pow(_get_f(closes, j) - _get_f(closes, j - 1.0), 2.0))
            j += 1.0
        if path_length == 0.0:
            pfe = 0.0
        else:
            pfe = _fdiv(price_move, path_length) * 100.0 * (1.0 if closes[i] > cp else -1.0)
        raw.append(pfe)
    first_valid = _find_first_valid(raw)
    if first_valid < 0:
        return raw
    out = [NAN] * first_valid
    out.extend(calculate_ema(raw[first_valid:], smoothing))
    return out


def calculate_ulcer_index(closes, period):
    result = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        max_close = _max_of(_js_slice(closes, fi - period + 1.0, fi + 1.0))
        sum_sq = 0.0
        j = fi - period + 1.0
        while j <= fi:
            pct_dd = _fdiv(_get_f(closes, j) - max_close, max_close) * 100.0
            sum_sq += pct_dd * pct_dd
            j += 1.0
        result.append(_sqrt(_fdiv(sum_sq, period)))
    return result


def calculate_natr(highs, lows, closes, period):
    atr = calculate_atr(highs, lows, closes, period)
    out = []
    for i in range(len(atr)):
        v = atr[i]
        if is_nan(v) or closes[i] == 0.0:
            out.append(NAN)
        else:
            out.append(_fdiv(v, closes[i]) * 100.0)
    return out


def calculate_true_range(highs, lows, closes):
    if not highs:
        return [NAN]
    tr = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr.append(_js_max3(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return tr


@dataclass
class Squeeze:
    squeeze: list
    momentum: list


def calculate_squeeze(highs, lows, closes, bb_period, bb_mult, kc_period, kc_mult):
    bb = calculate_bollinger_bands(closes, bb_period, bb_mult)
    kc = calculate_keltner_channels(highs, lows, closes, kc_period, 10.0, kc_mult)
    squeeze = []
    for i in range(len(bb.upper)):
        u = bb.upper[i]
        squeeze.append(
            (not is_nan(u)) and (not is_nan(kc.upper[i])) and u < kc.upper[i] and bb.lower[i] > kc.lower[i]
        )
    # The Donchian channel is the same for every element, so compute it once.
    dc = calculate_donchian(highs, lows, bb_period)
    momentum = []
    for i in range(len(closes)):
        c = closes[i]
        if is_nan(bb.middle[i]):
            momentum.append(NAN)
            continue
        dc_mid = dc.middle[i] if not is_nan(dc.middle[i]) else bb.middle[i]
        momentum.append(c - (dc_mid + bb.middle[i]) / 2.0)
    return Squeeze(squeeze=squeeze, momentum=momentum)


@dataclass
class Chandelier:
    exit_long: list
    exit_short: list


def calculate_chandelier_exit(highs, lows, closes, period, multiplier):
    atr = calculate_atr(highs, lows, closes, period)
    exit_long = []
    exit_short = []
    for i in range(len(closes)):
        fi = float(i)
        a = atr[i] if i < len(atr) else NAN
        if fi < period - 1.0 or is_nan(a):
            exit_long.append(NAN)
            exit_short.append(NAN)
            continue
        hh = _max_of(_js_slice(highs, fi - period + 1.0, fi + 1.0))
        ll = _min_of(_js_slice(lows, fi - period + 1.0, fi + 1.0))
        exit_long.append(hh - multiplier * a)
        exit_short.append(ll + multiplier * a)
    return Chandelier(exit_long=exit_long, exit_short=exit_short)


def calculate_relative_vol_index(closes, period, smoothing):
    sd = calculate_std_dev(closes, period)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(sd)):
        if is_nan(sd[i]) or is_nan(sd[i - 1]):
            gains.append(0.0)
            losses.append(0.0)
            continue
        change = sd[i] - sd[i - 1]
        gains.append(change if change > 0.0 else 0.0)
        losses.append(abs(change) if change < 0.0 else 0.0)
    avg_gain = calculate_ema(gains, smoothing)
    avg_loss = calculate_ema(losses, smoothing)
    out = []
    for g, l in zip(avg_gain, avg_loss):
        if is_nan(g) or is_nan(l):
            out.append(NAN)
            continue
        total = g + l
        out.append(50.0 if total == 0.0 else _fdiv(g, total) * 100.0)
    return out


def calculate_vhf(closes, period):
    result = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period:
            result.append(NAN)
            continue
        slice_ = _js_slice(closes, fi - period + 1.0, fi + 1.0)
        hc = _max_of(slice_)
        lc = _min_of(slice_)
        sum_change = 0.0
        j = fi - period + 2.0
        while j <= fi:
            sum_change += abs(_get_f(closes, j) - _get_f(closes, j - 1.0))
            j += 1.0
        result.append(0.0 if sum_change == 0.0 else _fdiv(abs(hc - lc), sum_change))
    return result


def calculate_acc_bands(highs, lows, closes, period):
    middle = calculate_sma(closes, period)
    upper = []
    lower = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            upper.append(NAN)
            lower.append(NAN)
            continue
        sum_u = 0.0
        sum_l = 0.0
        j = fi - period + 1.0
        while j <= fi:
            hj = _get_f(highs, j)
            lj = _get_f(lows, j)
            hl = hj - lj
            factor = 0.0 if hl == 0.0 else _fdiv(hl, (hj + lj) / 2.0)
            sum_u += hj * (1.0 + 2.0 * factor)
            sum_l += lj * (1.0 - 2.0 * factor)
            j += 1.0
        upper.append(_fdiv(sum_u, period))
        lower.append(_fdiv(sum_l, period))
    return Bands(upper=upper, middle=middle, lower=lower)


def calculate_volume_osc(volumes, fast, slow):
    fast_ma = calculate_ema(volumes, fast)
    slow_ma = calculate_ema(volumes, slow)
    out = []
    for f, s in zip(fast_ma, slow_ma):
        if is_nan(f) or is_nan(s) or s == 0.0:
            out.append(NAN)
        else:
            out.append(_fdiv(f - s, s) * 100.0)
    return out


def calculate_nvi(closes, volumes):
    nvi = [1000.0]
    for i in range(1, len(closes)):
        if _v_or0(volumes[i]) < _v_or0(volumes[i - 1]):
            nvi.append(nvi[i - 1] + _fdiv(nvi[i - 1] * (closes[i] - closes[i - 1]), closes[i - 1]))
        else:
            nvi.append(nvi[i - 1])
    return nvi


def calculate_pvi(closes, volumes):
    pvi = [1000.0]
    for i in range(1, len(closes)):
        if _v_or0(volumes[i]) > _v_or0(volumes[i - 1]):
            pvi.append(pvi[i - 1] + _fdiv(pvi[i - 1] * (closes[i] - closes[i - 1]), closes[i - 1]))
        else:
            pvi.append(pvi[i - 1])
    return pvi


def calculate_pvt(closes, volumes):
    pvt = [0.0]
    for i in range(1, len(closes)):
        pct_change = 0.0 if closes[i - 1] == 0.0 else _fdiv(closes[i] - closes[i - 1], closes[i - 1])
        pvt.append(pvt[i - 1] + pct_change * _v_or0(volumes[i]))
    return pvt


def calculate_vroc(volumes, period):
    out = []
    for i in range(len(volumes)):
        fi = float(i)
        if fi < period:
            out.append(NAN)
        else:
            prev = _get_f(volumes, fi - period)
            out.append(0.0 if prev == 0.0 else _fdiv(volumes[i] - prev, prev) * 100.0)
    return out


def calculate_net_volume(closes, volumes, period):
    raw = []
    for i in range(len(closes)):
        c = closes[i]
        if i == 0:
            raw.append(0.0)
        elif c > closes[i - 1]:
            raw.append(_v_or0(volumes[i]))
        elif c < closes[i - 1]:
            raw.append(-_v_or0(volumes[i]))
        else:
            raw.append(0.0)
    return calculate_sma(raw, period)


def calculate_twiggs_mf(highs, lows, closes, volumes, period):
    trh = [highs[0]]
    trl = [lows[0]]
    for i in range(1, len(closes)):
        trh.append(_fmax(highs[i], closes[i - 1]))
        trl.append(_fmin(lows[i], closes[i - 1]))
        # JS Math.max(NaN, x) is NaN but _fmax(NaN, x) is x, so force NaN
        # when either operand is NaN.
        n = len(trh) - 1
        if is_nan(highs[i]) or is_nan(closes[i - 1]):
            trh[n] = NAN
        if is_nan(lows[i]) or is_nan(closes[i - 1]):
            trl[n] = NAN
    adv = []
    for i in range(len(trh)):
        h = trh[i]
        r = h - trl[i]
        if r == 0.0:
            adv.append(0.0)
        else:
            adv.append((_fdiv(closes[i] - trl[i], r) * 2.0 - 1.0) * _v_or0(volumes[i]))
    ema_adv = calculate_ema(adv, period)
    ema_vol = calculate_ema(volumes, period)
    out = []
    for a, v in zip(ema_adv, ema_vol):
        out.append(NAN if (is_nan(a) or is_nan(v) or v == 0.0) else _fdiv(a, v))
    return out


def calculate_lin_reg_r_squared(data, period):
    result = []
    for i in range(len(data)):
        fi = float(i)
        if fi < period - 1.0:
            result.append(NAN)
            continue
        slice_ = _js_slice(data, fi - period + 1.0, fi + 1.0)
        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0
        sum_y2 = 0.0
        j = 0.0
        while j < period:
            sj = _get_f(slice_, j)
            sum_x += j
            sum_y += sj
            sum_xy += j * sj
            sum_x2 += j * j
            sum_y2 += sj * sj
            j += 1.0
        num = period * sum_xy - sum_x * sum_y
        den = _sqrt((period * sum_x2 - sum_x * sum_x) * (period * sum_y2 - sum_y * sum_y))
        r = 0.0 if den == 0.0 else _fdiv(num, den)
        result.append(r * r)
    return result


def calculate_zigzag(highs, lows, closes, deviation):
    result = [NAN] * len(closes)
    if len(closes) < 2:
        return result
    trend = 1
    last_pivot_idx = 0
    last_pivot_price = closes[0]
    result[0] = closes[0]
    for i in range(1, len(closes)):
        if trend == 1:
            if highs[i] > last_pivot_price:
                last_pivot_price = highs[i]
                last_pivot_idx = i
            change = _fdiv(last_pivot_price - lows[i], last_pivot_price) * 100.0
            if change >= deviation:
                result[last_pivot_idx] = last_pivot_price
                trend = -1
                last_pivot_price = lows[i]
                last_pivot_idx = i
        else:
            if lows[i] < last_pivot_price:
                last_pivot_price = lows[i]
                last_pivot_idx = i
            change = _fdiv(highs[i] - last_pivot_price, last_pivot_price) * 100.0
            if change >= deviation:
                result[last_pivot_idx] = last_pivot_price
                trend = 1
                last_pivot_price = highs[i]
                last_pivot_idx = i
    result[last_pivot_idx] = last_pivot_price
    # Interpolate between pivots
    prev_idx = -1
    for i in range(len(result)):
        if not is_nan(result[i]):
            if prev_idx >= 0 and i - prev_idx > 1:
                start_val = result[prev_idx]
                end_val = result[i]
                for j in range(prev_idx + 1, i):
                    result[j] = start_val + (end_val - start_val) * (
                        _fdiv(float(j) - float(prev_idx), float(i) - float(prev_idx))
                    )
            prev_idx = i
    return result


@dataclass
class Fractals:
    up_fractals: list
    down_fractals: list


def calculate_fractals(highs, lows, period):
    up = [NAN] * len(highs)
    down = [NAN] * len(lows)
    if math.isfinite(period) and period == math.trunc(period) and period >= 0.0:
        p = int(period)
        n = len(highs)
        if n > p:
            for i in range(p, n - p):
                is_up = True
                is_down = True
                for j in range(1, p + 1):
                    if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                        is_up = False
                    if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                        is_down = False
                if is_up:
                    up[i] = highs[i]
                if is_down:
                    down[i] = lows[i]
    return Fractals(up_fractals=up, down_fractals=down)


@dataclass
class Alligator:
    jaw: list
    teeth: list
    lips: list


def calculate_alligator(closes):
    jaw_raw = calculate_smma(closes, 13.0)
    teeth_raw = calculate_smma(closes, 8.0)
    lips_raw = calculate_smma(closes, 5.0)

    def displace(raw, by):
        v = [NAN] * by
        keep = len(raw) - by
        if keep < 0:
            keep = 0
        v.extend(raw[:keep])
        return v

    return Alligator(
        jaw=displace(jaw_raw, 8),
        teeth=displace(teeth_raw, 5),
        lips=displace(lips_raw, 3),
    )


@dataclass
class Gator:
    upper: list
    lower: list


def calculate_gator(closes):
    a = calculate_alligator(closes)
    upper = []
    for j, t in zip(a.jaw, a.teeth):
        upper.append(NAN if (is_nan(j) or is_nan(t)) else abs(j - t))
    lower = []
    for t, l in zip(a.teeth, a.lips):
        lower.append(NAN if (is_nan(t) or is_nan(l)) else -abs(t - l))
    return Gator(upper=upper, lower=lower)


def calculate_smi(highs, lows, closes, period, smooth_k, smooth_d):
    smi_vals = []
    for i in range(len(closes)):
        fi = float(i)
        if fi < period - 1.0:
            smi_vals.append(NAN)
            continue
        hh = _max_of(_js_slice(highs, fi - period + 1.0, fi + 1.0))
        ll = _min_of(_js_slice(lows, fi - period + 1.0, fi + 1.0))
        midpoint = (hh + ll) / 2.0
        diff = closes[i] - midpoint
        range_ = hh - ll
        smi_vals.append(0.0 if range_ == 0.0 else _fdiv(diff, range_ / 2.0) * 100.0)
    smoothed = calculate_ema(_filter_valid(smi_vals), smooth_k)
    smi = [NAN] * len(closes)
    idx = 0
    for i in range(len(smi_vals)):
        if not is_nan(smi_vals[i]) and idx < len(smoothed):
            smi[i] = smoothed[idx]
            idx += 1
    valid_smi = _filter_valid(smi)
    sig_ema = calculate_ema(valid_smi, smooth_d)
    signal = [NAN] * len(closes)
    idx = 0
    for i in range(len(smi)):
        if not is_nan(smi[i]) and idx < len(sig_ema):
            signal[i] = sig_ema[idx]
            idx += 1
    return TwoLine(a=smi, b=signal)
