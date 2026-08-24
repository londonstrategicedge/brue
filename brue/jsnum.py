# ECMAScript Number formatting. Brue's observable output (str(), string
# concatenation, JSON numbers) and its internal cache keys (expression
# fingerprints like "n20") are defined by JS String(number), so this module
# must format doubles exactly the way ECMA-262 7.1.12.1 does.
#
# Python's repr(float) already produces the shortest round-trip digit
# string (the same digits ECMA requires); this module rearranges those
# digits into JS's fixed/exponential layout rules instead of Python's.
import math


def js_number_to_string(x):
    """ECMA-262 ToString(Number) for base 10. Matches V8's String(x) exactly."""
    if x != x:
        return "NaN"
    if x == 0.0:
        return "0"  # covers -0 too
    if math.isinf(x):
        return "Infinity" if x > 0.0 else "-Infinity"
    if x < 0.0:
        return "-" + js_number_to_string(-x)

    # repr gives shortest round-trip. Normalize its two layouts
    # ("ddd.ddd" and "d.ddde+NN"/"de+NN") into a bare digit string D with
    # value = D * 10^(n - k), k = len(D), which is ECMA's framing.
    sci = repr(x)
    epos = sci.find("e")
    if epos >= 0:
        mantissa = sci[:epos]
        exp10 = int(sci[epos + 1:])
    else:
        mantissa = sci
        exp10 = 0
    dot = mantissa.find(".")
    if dot >= 0:
        digits = mantissa[:dot] + mantissa[dot + 1:]
        # exponent of the last digit of the (unstripped) digit string
        last_exp = exp10 - (len(mantissa) - dot - 1)
    else:
        digits = mantissa
        last_exp = exp10
    # leading zeros come from "0.ddd" forms and carry no value; trailing
    # zeros come from "ddd.0" forms and shift the last-digit exponent
    digits = digits.lstrip("0")
    stripped = len(digits)
    digits = digits.rstrip("0")
    last_exp += stripped - len(digits)
    k = len(digits)  # number of significant digits
    n = last_exp + k  # ECMA's n: value = digits * 10^(n-k)

    if k <= n <= 21:
        # integer with trailing zeros
        return digits + "0" * (n - k)
    elif 0 < n <= 21:
        # decimal point inside the digits
        return digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        # 0.000ddd
        return "0." + "0" * (-n) + digits
    else:
        # exponential notation
        e = n - 1
        sign = "+" if e >= 0 else "-"
        mag = abs(e)
        if k == 1:
            return "{}e{}{}".format(digits, sign, mag)
        return "{}.{}e{}{}".format(digits[:1], digits[1:], sign, mag)


def js_to_fixed(x, f):
    """ECMA Number.prototype.toFixed(f). Used by the f-string ":.Nf" formatter.
    Rounds the EXACT decimal expansion of the double at digit f, ties toward
    larger magnitude (bankers'/ties-to-even rounding on the exact value
    differs, e.g. (1.125).toFixed(2) is "1.13" in JS but 1.12 under
    ties-to-even)."""
    if x != x:
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0.0 else "-Infinity"
    if abs(x) >= 1e21:
        return js_number_to_string(x)
    neg = x < 0.0  # JS: (-0).toFixed(2) == "0.00"
    ax = abs(x)

    # Print the exact decimal expansion: a double's fraction has at most
    # 1075 decimal digits, so 1100 digits of precision captures it fully
    # (CPython's formatter emits exact digits at any requested precision).
    exact = format(ax, ".1100f")
    dot = exact.find(".")
    int_part = [ord(c) - 48 for c in exact[:dot]]
    frac_part = [ord(c) - 48 for c in exact[dot + 1:]]

    # Keep f fractional digits; round half-up on the first dropped digit
    # (any nonzero tail after a 5 also rounds up; a bare 5 ties upward,
    # which is ECMA's "pick the larger n").
    digits = int_part + frac_part[: min(f, len(frac_part))]
    dropped_first = frac_part[f] if f < len(frac_part) else 0
    if dropped_first >= 5:
        # increment the digit string
        i = len(digits)
        while True:
            if i == 0:
                digits.insert(0, 1)
                break
            i -= 1
            if digits[i] == 9:
                digits[i] = 0
            else:
                digits[i] += 1
                break
    int_len = len(digits) - f
    s = ""
    if neg:
        # ECMA splits the sign off before digit selection and always
        # prepends it: (-0.004).toFixed(2) is "-0.00", sign kept.
        s += "-"
    int_str = "".join(chr(d + 48) for d in digits[:int_len])
    # strip leading zeros but keep at least one digit
    trimmed = int_str.lstrip("0")
    s += trimmed if trimmed else "0"
    if f > 0:
        s += "." + "".join(chr(d + 48) for d in digits[int_len:])
    return s
