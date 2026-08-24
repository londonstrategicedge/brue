# Bit-exact implementations of the fdlibm transcendental functions V8 uses
# for Math.exp / Math.log / Math.log10 / Math.pow (V8's src/base/ieee754.cc
# is the same classic Sun fdlibm code). The platform libm is correctly
# rounded on some of these where fdlibm is not, so calling Python's
# math.exp/log would diverge from V8 by 1 ulp on some inputs; these
# functions exist purely so brue matches V8 bit-for-bit.
#
# Constants are the canonical fdlibm decimal literals; each was chosen by
# Sun to be exactly representable, so decimal parsing reproduces the exact
# bits.
#
# The i32/u32 bit-twiddling of the C source maps onto plain Python ints.
# Python's infinite-precision two's complement makes signed AND/OR/shift
# behave identically for i32-range values; the only places that need an
# explicit `& 0xffffffff` are where the C code wraps or casts through
# u32, and from_parts masks its inputs so wrapped high words are safe.
import math
import struct

NAN = float("nan")
INFINITY = float("inf")


def _bits(x):
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def _from_bits(b):
    return struct.unpack("<d", struct.pack("<Q", b & 0xFFFFFFFFFFFFFFFF))[0]


def hi(x):
    # high 32 bits as a SIGNED i32 (the fdlibm code branches on its sign)
    h = _bits(x) >> 32
    return h - 0x100000000 if h >= 0x80000000 else h


def lo(x):
    return _bits(x) & 0xFFFFFFFF


def from_parts(h, l):
    return _from_bits(((h & 0xFFFFFFFF) << 32) | (l & 0xFFFFFFFF))


def set_hi(x, h):
    return from_parts(h, lo(x))


def set_lo(x, l):
    return from_parts(hi(x), l)


def _div(a, b):
    # IEEE 754 division returns inf/NaN on a zero divisor where Python
    # raises ZeroDivisionError, so the few fdlibm paths that divide by a
    # possible zero route through this helper.
    if b == 0.0:
        if a != a or a == 0.0:
            return NAN
        s = math.copysign(1.0, a) * math.copysign(1.0, b)
        return s * INFINITY
    return a / b


TWO54 = 1.80143985094819840000e+16


def scalbn(x, n):
    """fdlibm scalbn"""
    hx = hi(x)
    lx = lo(x)
    k = (hx & 0x7ff00000) >> 20
    if k == 0:
        # 0 or subnormal
        if (lx | (hx & 0x7fffffff)) == 0:
            return x
        x *= TWO54
        hx2 = hi(x)
        k = ((hx2 & 0x7ff00000) >> 20) - 54
        if n < -50000:
            return 1.0e-300 * x  # underflow
    if k == 0x7ff:
        return x + x  # NaN or Inf
    k += n
    if k > 0x7fe:
        return 1.0e300 * (1.0e300 if x > 0.0 else -1.0e300)  # overflow
    if k > 0:
        return set_hi(x, (hi(x) & ~0x7ff00000) | (k << 20))
    if k <= -54:
        if n > 50000:
            return 1.0e300 * (1.0e300 if x > 0.0 else -1.0e300)  # overflow
        return 1.0e-300 * (1.0e-300 if x > 0.0 else -1.0e-300)  # underflow
    k += 54  # subnormal result
    x2 = set_hi(x, (hi(x) & ~0x7ff00000) | (k << 20))
    return x2 * 5.55111512312578270212e-17  # 2^-54


def js_exp(x):
    """V8's ieee754 exp (fdlibm plus V8's exp(1)==E special case and the
    twopk-multiply tail). Verified bit-identical to node 20 via mathcheck."""
    ONE = 1.0
    HALVES = (0.5, -0.5)
    HUGE = 1.0e+300
    TWOM1000 = 9.33263618503218878990e-302
    TWO1023 = 8.988465674311579539e307
    O_THRESHOLD = 7.09782712893383973096e+02
    U_THRESHOLD = -7.45133219101941108420e+02
    LN2_HI = (6.93147180369123816490e-01, -6.93147180369123816490e-01)
    LN2_LO = (1.90821492927058770002e-10, -1.90821492927058770002e-10)
    INVLN2 = 1.44269504088896338700e+00
    P1 = 1.66666666666666019037e-01
    P2 = -2.77777777770155933842e-03
    P3 = 6.61375632143793436117e-05
    P4 = -1.65339022054652515390e-06
    P5 = 4.13813679705723846039e-08
    E = 2.718281828459045

    hx = hi(x)
    xsb = (hx >> 31) & 1
    hx &= 0x7fffffff

    if hx >= 0x40862e42:
        # |x| >= 709.78
        if hx >= 0x7ff00000:
            if ((hx & 0xfffff) | lo(x)) != 0:
                return x + x  # NaN
            return x if xsb == 0 else 0.0  # exp(+-inf)={inf,0}
        if x > O_THRESHOLD:
            return HUGE * HUGE  # overflow
        if x < U_THRESHOLD:
            return TWOM1000 * TWOM1000  # underflow

    hi_r = 0.0
    lo_r = 0.0
    k = 0
    if hx > 0x3fd62e42:
        # |x| > 0.5 ln2
        if hx < 0x3ff0a2b2:
            # |x| < 1.5 ln2
            # V8 special-cases exp(1) to return E exactly; the general
            # path below gets the last bit wrong.
            if x == 1.0:
                return E
            hi_r = x - LN2_HI[xsb]
            lo_r = LN2_LO[xsb]
            k = 1 - xsb - xsb
        else:
            k = int(INVLN2 * x + HALVES[xsb])  # f64 as i32 truncates
            t = float(k)
            hi_r = x - t * LN2_HI[0]
            lo_r = t * LN2_LO[0]
        x = hi_r - lo_r
    elif hx < 0x3e300000:
        # |x| < 2^-28
        if HUGE + x > ONE:
            return ONE + x
    else:
        k = 0

    t = x * x
    if k >= -1021:
        twopk = from_parts(0x3ff00000 + ((k & 0xFFFFFFFF) << 20), 0)
    else:
        twopk = from_parts(0x3ff00000 + (((k + 1000) & 0xFFFFFFFF) << 20), 0)
    c = x - t * (P1 + t * (P2 + t * (P3 + t * (P4 + t * P5))))
    if k == 0:
        return ONE - ((x * c) / (c - 2.0) - x)
    y = ONE - ((lo_r - (x * c) / (2.0 - c)) - hi_r)
    if k >= -1021:
        if k == 1024:
            return y * 2.0 * TWO1023
        return y * twopk
    return y * twopk * TWOM1000


def js_log(x):
    """fdlibm __ieee754_log (what V8 Math.log runs)"""
    LN2_HI = 6.93147180369123816490e-01
    LN2_LO = 1.90821492927058770002e-10
    LG1 = 6.666666666666735130e-01
    LG2 = 3.999999999940941908e-01
    LG3 = 2.857142874366239149e-01
    LG4 = 2.222219843214978396e-01
    LG5 = 1.818357216161805012e-01
    LG6 = 1.531383769920937332e-01
    LG7 = 1.479819860511658591e-01
    ZERO = 0.0

    hx = hi(x)
    lx = lo(x)

    k = 0
    if hx < 0x00100000:
        # x < 2^-1022
        if ((hx & 0x7fffffff) | lx) == 0:
            return -INFINITY  # log(+-0) = -inf
        if hx < 0:
            return NAN  # log(-#) = NaN
        k -= 54
        x *= TWO54  # subnormal, scale up
        hx = hi(x)
    if hx >= 0x7ff00000:
        return x + x
    k += (hx >> 20) - 1023
    hx &= 0x000fffff
    i = (hx + 0x95f64) & 0x100000
    x = set_hi(x, hx | (i ^ 0x3ff00000))  # normalize x or x/2
    k += i >> 20
    f = x - 1.0
    if (0x000fffff & (2 + hx)) < 3:
        # |f| < 2^-20
        if f == ZERO:
            if k == 0:
                return ZERO
            dk = float(k)
            return dk * LN2_HI + dk * LN2_LO
        r = f * f * (0.5 - 0.33333333333333333 * f)
        if k == 0:
            return f - r
        dk = float(k)
        return dk * LN2_HI - ((r - dk * LN2_LO) - f)
    s = f / (2.0 + f)
    dk = float(k)
    z = s * s
    i2 = hx - 0x6147a
    w = z * z
    j = 0x6b851 - hx
    t1 = w * (LG2 + w * (LG4 + w * LG6))
    t2 = z * (LG1 + w * (LG3 + w * (LG5 + w * LG7)))
    i2 |= j
    r = t2 + t1
    if i2 > 0:
        hfsq = 0.5 * f * f
        if k == 0:
            return f - (hfsq - s * (hfsq + r))
        return dk * LN2_HI - ((hfsq - (s * (hfsq + r) + dk * LN2_LO)) - f)
    elif k == 0:
        return f - s * (f - r)
    else:
        return dk * LN2_HI - ((s * (f - r) - dk * LN2_LO) - f)


def js_log10(x):
    """fdlibm __ieee754_log10 (what V8 Math.log10 runs)"""
    IVLN10 = 4.34294481903251816668e-01
    LOG10_2HI = 3.01029995663611771306e-01
    LOG10_2LO = 3.69423907715893078616e-13

    hx = hi(x)
    lx = lo(x)

    k = 0
    if hx < 0x00100000:
        if ((hx & 0x7fffffff) | lx) == 0:
            return -INFINITY
        if hx < 0:
            return NAN
        k -= 54
        x *= TWO54
        hx = hi(x)
    if hx >= 0x7ff00000:
        return x + x
    k += (hx >> 20) - 1023
    i = ((k & 0xFFFFFFFF) & 0x80000000) >> 31  # sign bit of k as u32
    hx = (hx & 0x000fffff) | ((0x3ff - i) << 20)
    y = float(k + i)
    x = set_hi(x, hx)
    z = y * LOG10_2LO + IVLN10 * js_log(x)
    return z + y * LOG10_2HI


def js_pow(x, y):
    """Math.pow: ECMAScript special cases + V8's ieee754 pow (fdlibm with V8's
    one-line divisor deviation; verified bit-identical to node 20 via
    `brue mathcheck`)."""
    if y != y:
        return NAN
    if y == 0.0:
        return 1.0
    if x != x:
        return NAN
    if (x == 1.0 or x == -1.0) and math.isinf(y):
        return NAN
    return ieee754_pow(x, y)


def fdlibm_pow(x, y):
    """Alias kept so verification tooling can compare implementations."""
    return js_pow(x, y)


def ieee754_pow(x, y):
    """fdlibm e_pow.c as a single linear function, mirroring the C."""
    BP = (1.0, 1.5)
    DP_H = (0.0, 5.84962487220764160156e-01)
    DP_L = (0.0, 1.35003920212974897128e-08)
    ZERO = 0.0
    ONE = 1.0
    TWO = 2.0
    TWO53 = 9007199254740992.0
    HUGE = 1.0e300
    TINY = 1.0e-300
    L1 = 5.99999999999994648725e-01
    L2 = 4.28571428578550184252e-01
    L3 = 3.33333329818377432918e-01
    L4 = 2.72728123808534006489e-01
    L5 = 2.30660745775561754067e-01
    L6 = 2.06975017800338417784e-01
    P1 = 1.66666666666666019037e-01
    P2 = -2.77777777770155933842e-03
    P3 = 6.61375632143793436117e-05
    P4 = -1.65339022054652515390e-06
    P5 = 4.13813679705723846039e-08
    LG2 = 6.93147180559945286227e-01
    LG2_H = 6.93147182464599609375e-01
    LG2_L = -1.90465429995776804525e-09
    OVT = 8.0085662595372944372e-0017
    CP = 9.61796693925975554329e-01
    CP_H = 9.61796700954437255859e-01
    CP_L = -7.02846165095275826516e-09
    IVLN2 = 1.44269504088896338700e+00
    IVLN2_H = 1.44269502162933349609e+00
    IVLN2_L = 1.92596299112661746887e-08

    hx = hi(x)
    lx = lo(x)
    hy = hi(y)
    ly = lo(y)
    ix = hx & 0x7fffffff
    iy = hy & 0x7fffffff

    # +-NaN return x+y
    if (
        ix > 0x7ff00000
        or (ix == 0x7ff00000 and lx != 0)
        or iy > 0x7ff00000
        or (iy == 0x7ff00000 and ly != 0)
    ):
        return x + y

    # determine if y is an odd int when x < 0
    yisint = 0
    if hx < 0:
        if iy >= 0x43400000:
            yisint = 2  # even integer y
        elif iy >= 0x3ff00000:
            k = (iy >> 20) - 0x3ff
            if k > 20:
                j = ly >> (52 - k)
                if ((j << (52 - k)) & 0xFFFFFFFF) == ly:  # u32 shift wraps, as in the C
                    yisint = 2 - (j & 1)
            elif ly == 0:
                j = iy >> (20 - k)
                if (j << (20 - k)) == iy:
                    yisint = 2 - (j & 1)

    # special value of y
    if ly == 0:
        if iy == 0x7ff00000:
            # y is +-inf
            if (((ix - 0x3ff00000) & 0xFFFFFFFF) | lx) == 0:
                return y - y  # inf**+-1 is NaN
            elif ix >= 0x3ff00000:
                return y if hy >= 0 else ZERO
            else:
                return -y if hy < 0 else ZERO
        if iy == 0x3ff00000:
            # y is +-1
            if hy < 0:
                return _div(ONE, x)
            return x
        if hy == 0x40000000:
            return x * x  # y is 2
        if hy == 0x3fe00000 and hx >= 0:
            return math.sqrt(x)  # y is 0.5 and x >= +0

    ax = abs(x)
    # special value of x
    if lx == 0 and (ix == 0x7ff00000 or ix == 0 or ix == 0x3ff00000):
        z = ax  # x is +-0,+-inf,+-1
        if hy < 0:
            z = _div(ONE, z)
        if hx < 0:
            if ((ix - 0x3ff00000) | yisint) == 0:
                z = _div(z - z, z - z)  # (-1)**non-int is NaN
            elif yisint == 1:
                z = -z  # (x<0)**odd = -(|x|**odd)
        return z

    n_sign = ((hx & 0xFFFFFFFF) >> 31) - 1  # sign bit of hx, minus one

    # (x<0)**(non-int) is NaN
    if (n_sign | yisint) == 0:
        return _div(x - x, x - x)

    s = ONE  # sign of result
    if (n_sign | (yisint - 1)) == 0:
        s = -ONE  # (-ve)**(odd int)

    # |y| is huge
    if iy > 0x41e00000:
        # if |y| > 2**31
        if iy > 0x43f00000:
            # if |y| > 2**64, must o/uflow
            if ix <= 0x3fefffff:
                return HUGE * HUGE if hy < 0 else TINY * TINY
            if ix >= 0x3ff00000:
                return HUGE * HUGE if hy > 0 else TINY * TINY
        # over/underflow if x is not close to one
        if ix < 0x3fefffff:
            return s * HUGE * HUGE if hy < 0 else s * TINY * TINY
        if ix > 0x3ff00000:
            return s * HUGE * HUGE if hy > 0 else s * TINY * TINY
        # |1-x| is tiny <= 2**-20: log(x) by x-x^2/2+x^3/3-x^4/4
        t = ax - ONE
        w = (t * t) * (0.5 - t * (0.3333333333333333333333 - t * 0.25))
        u = IVLN2_H * t
        v = t * IVLN2_L - w * IVLN2
        t1v = u + v
        t1v = set_lo(t1v, 0)
        t1 = t1v
        t2 = v - (t1 - u)
    else:
        n = 0
        # take care of subnormal number
        if ix < 0x00100000:
            ax *= TWO53
            n -= 53
            ix = hi(ax)
        n += (ix >> 20) - 0x3ff
        j = ix & 0x000fffff
        # determine interval
        ix = j | 0x3ff00000  # normalize ix
        if j <= 0x3988e:
            k = 0  # |x|<sqrt(3/2)
        elif j < 0xbb67a:
            k = 1  # |x|<sqrt(3)
        else:
            k = 0
            n += 1
            ix -= 0x00100000
        ax = set_hi(ax, ix)

        # compute ss = s_h+s_l = (x-1)/(x+1) or (x-1.5)/(x+1.5)
        u = ax - BP[k]
        v = ONE / (ax + BP[k])
        ss = u * v
        s_h = set_lo(ss, 0)
        # t_h = ax+bp[k] High
        t_h = from_parts(((ix >> 1) | 0x20000000) + 0x00080000 + (k << 18), 0)
        t_l = ax - (t_h - BP[k])
        s_l = v * ((u - s_h * t_h) - s_h * t_l)
        # compute log(ax)
        s2 = ss * ss
        r = s2 * s2 * (L1 + s2 * (L2 + s2 * (L3 + s2 * (L4 + s2 * (L5 + s2 * L6)))))
        r += s_l * (s_h + ss)
        s2h = s_h * s_h
        t_hb = set_lo(3.0 + s2h + r, 0)
        t_lb = r - ((t_hb - 3.0) - s2h)
        # u+v = ss*(1+...)
        ub = s_h * t_hb
        vb = s_l * t_hb + t_lb * ss
        # 2/(3log2)*(ss+...)
        p_h = set_lo(ub + vb, 0)
        p_l = vb - (p_h - ub)
        z_h = CP_H * p_h
        z_l = CP_L * p_h + p_l * CP + DP_L[k]
        # log2(ax) = (ss+..)*2/(3*log2) = n + dp_h + z_h + z_l
        t = float(n)
        t1v = set_lo(((z_h + z_l) + DP_H[k]) + t, 0)
        t1 = t1v
        t2 = z_l - (((t1 - t) - DP_H[k]) - z_h)

    # split up y into y1+y2 and compute (y1+y2)*(t1+t2)
    y1 = set_lo(y, 0)
    p_l = (y - y1) * t1 + y * t2
    p_h = y1 * t1
    z = p_l + p_h
    j = hi(z)
    i = lo(z)  # only its zero-ness is tested below, so u32 form suffices
    if j >= 0x40900000:
        # z >= 1024
        if ((j - 0x40900000) | i) != 0:
            return s * HUGE * HUGE  # overflow
        if p_l + OVT > z - p_h:
            return s * HUGE * HUGE  # overflow
    elif (j & 0x7fffffff) >= 0x4090cc00:
        # z <= -1075
        if ((((j & 0xFFFFFFFF) - 0xc090cc00) & 0xFFFFFFFF) | i) != 0:  # u32 wrapping subtract
            return s * TINY * TINY  # underflow
        if p_l <= z - p_h:
            return s * TINY * TINY  # underflow
    #
    # compute 2**(p_h+p_l)
    #
    i2 = j & 0x7fffffff
    k = (i2 >> 20) - 0x3ff
    n = 0
    if i2 > 0x3fe00000:
        # |z| > 0.5, set n = [z+0.5]
        n = j + (0x00100000 >> (k + 1))
        k2 = ((n & 0x7fffffff) >> 20) - 0x3ff
        t = from_parts(n & ~(0x000fffff >> k2), 0)
        n = ((n & 0x000fffff) | 0x00100000) >> (20 - k2)
        if j < 0:
            n = -n
        p_h -= t
    t = set_lo(p_l + p_h, 0)
    u = t * LG2_H
    v = (p_l - (t - p_h)) * LG2 + t * LG2_L
    z = u + v
    w = v - (z - u)
    t_sq = z * z
    t1b = z - t_sq * (P1 + t_sq * (P2 + t_sq * (P3 + t_sq * (P4 + t_sq * P5))))
    # V8's ieee754.cc deviates from classic fdlibm here: the (w + z*w)
    # correction sits INSIDE the divisor. This is the whole difference
    # between V8 pow and both glibc and classic-fdlibm pow.
    r = (z * t1b) / ((t1b - TWO) - (w + z * w))
    z = ONE - (r - z)
    j = hi(z) + (n << 20)
    if (j >> 20) <= 0:
        z = scalbn(z, n)  # subnormal output
    else:
        z = set_hi(z, hi(z) + (n << 20))
    return s * z


def js_round(x):
    """ECMA Math.round: closest integer; ties go toward +Infinity.
    (Bare round-half-away-from-zero differs for x.5 < 0.)"""
    if not math.isfinite(x):
        return x
    if x == 0.0:
        return x  # preserves +-0
    f = float(math.floor(x))
    diff = x - f
    r = f + 1.0 if diff >= 0.5 else f
    # ECMA: result may be -0 when -0.5 <= x < 0
    if r == 0.0 and x < 0.0:
        return -0.0
    return r


def js_sign(x):
    """ECMA Math.sign: NaN gives NaN, +-0 preserved, else +-1."""
    if x != x:
        return NAN
    if x == 0.0:
        return x
    return 1.0 if x > 0.0 else -1.0
