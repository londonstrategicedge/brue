# Brue runtime values. Value semantics follow JavaScript, which is what the
# conformance goldens encode:
#   - null and undefined are distinct (undefined only appears as a missing
#     builtin argument; expressions always produce null for na)
#   - == on colors/arrays/maps/structs is REFERENCE equality (===), so
#     Value keeps them as shared Python objects and compares identity (is)
#   - String(v) follows JS ToString (arrays join with ",", objects print
#     "[object Object]", Map prints "[object Map]")
#
# Value is one class with a `kind` tag (undefined, null, num, bool, str,
# color, array, map, udt) and a `v` payload. Construct ONLY through the
# variant constructors below (Value.Num coerces to float so int payloads
# can never leak in) and the shared singletons Value.Undefined / Value.Null.
from . import jsnum

NAN = float("nan")


class ColorVal:
    # Field-wise equality for tests and debugging only; strict_eq still
    # compares colors by identity (===).
    __slots__ = ("r", "g", "b", "a")

    def __init__(self, r, g, b, a):
        self.r = r
        self.g = g
        self.b = b
        self.a = a

    def __eq__(self, other):
        if not isinstance(other, ColorVal):
            return NotImplemented
        return (
            self.r == other.r
            and self.g == other.g
            and self.b == other.b
            and self.a == other.a
        )

    def __hash__(self):
        return id(self)


class OrderedMap:
    """Insertion-ordered string map (JS Map preserves insertion order and Brue
    map keys are always coerced to strings)."""

    __slots__ = ("entries",)

    def __init__(self):
        # list of (key, value) tuples; linear scan, maps stay small
        self.entries = []

    def get(self, k):
        for key, v in self.entries:
            if key == k:
                return v
        return None

    def set(self, k, v):
        for i, (key, _old) in enumerate(self.entries):
            if key == k:
                self.entries[i] = (key, v)
                return
        self.entries.append((k, v))

    def has(self, k):
        for key, _v in self.entries:
            if key == k:
                return True
        return False

    def remove(self, k):
        self.entries = [(key, v) for (key, v) in self.entries if key != k]

    def len(self):
        return len(self.entries)

    def clear(self):
        self.entries = []


class UdtVal:
    """A user-defined-type (struct) instance: `{ __udt: name, ...fields }`.
    Field order matters for JSON parity; __udt is conceptually first."""

    __slots__ = ("type_name", "fields")

    def __init__(self, type_name, fields):
        self.type_name = type_name
        self.fields = fields


class Value:
    __slots__ = ("kind", "v")

    def __init__(self, kind, v=None):
        self.kind = kind
        self.v = v

    # Variant constructors.
    @staticmethod
    def Num(n):
        # coerce to float: int payloads would silently change division and
        # formatting semantics downstream
        return Value("num", float(n))

    @staticmethod
    def Bool(b):
        return Value("bool", bool(b))

    @staticmethod
    def Str(s):
        return Value("str", s)

    @staticmethod
    def Color(c):
        # c: ColorVal, shared by identity
        return Value("color", c)

    @staticmethod
    def Array(items):
        # items: a Python list, shared by identity
        return Value("array", items)

    @staticmethod
    def Map(m):
        # m: OrderedMap, shared by identity
        return Value("map", m)

    @staticmethod
    def Udt(u):
        # u: UdtVal, shared by identity
        return Value("udt", u)

    # Lowercase aliases of the variant constructors.
    @staticmethod
    def str(s):
        return Value("str", s)

    @staticmethod
    def array(v):
        return Value("array", v)

    def is_na(self):
        """na test: null, or a NaN number. (undefined is NOT na here, but
        the is_na() builtin treats it as missing; see builtins.)"""
        if self.kind == "null":
            return True
        if self.kind == "num":
            return self.v != self.v
        return False

    def is_truthy(self):
        """JS truthiness: null, undefined, false, 0, NaN and "" are false."""
        k = self.kind
        if k == "null" or k == "undefined":
            return False
        if k == "bool":
            return self.v
        if k == "num":
            n = self.v
            return n == n and n != 0.0
        if k == "str":
            return len(self.v) != 0
        return True

    def num_or_nan(self):
        """`typeof v === 'number' ? v : NaN` (the binary-op coercion)"""
        if self.kind == "num":
            return self.v
        return NAN

    def num_or_zero(self):
        """`typeof v === 'number' ? v : 0` (the compound-assign coercion)"""
        if self.kind == "num":
            return self.v
        return 0.0

    def strict_eq(self, other):
        """JS strict equality (===) plus the runtime's na==na extension is
        applied by the caller; this is bare ===."""
        a = self.kind
        if a != other.kind:
            return False
        if a == "null" or a == "undefined":
            return True
        if a == "num":
            return self.v == other.v  # NaN === NaN is false
        if a == "bool" or a == "str":
            return self.v == other.v
        if a == "color" or a == "array" or a == "map" or a == "udt":
            return self.v is other.v  # reference equality
        return False

    def same_value_zero(self, other):
        """JS SameValueZero (Array.prototype.includes): like === but NaN==NaN."""
        if self.kind == "num" and other.kind == "num":
            if self.v != self.v and other.v != other.v:
                return True
        return self.strict_eq(other)

    def js_to_number(self):
        """JS ToNumber for the odd coercion paths inside builtins
        (Number(v) semantics, without string trimming subtleties beyond
        the common cases)."""
        k = self.kind
        if k == "undefined":
            return NAN
        if k == "null":
            return 0.0
        if k == "num":
            return self.v
        if k == "bool":
            return 1.0 if self.v else 0.0
        if k == "str":
            t = self.v.strip()
            if not t:
                return 0.0
            return _parse_f64(t)
        if k == "array":
            # JS: ToPrimitive -> join(',') -> ToNumber. Single-element
            # arrays coerce through their element.
            b = self.v
            if len(b) == 0:
                return 0.0
            if len(b) == 1:
                return b[0].js_to_number()
            return NAN
        return NAN

    def js_string(self):
        """JS String(v)."""
        k = self.kind
        if k == "undefined":
            return "undefined"
        if k == "null":
            return "null"
        if k == "num":
            return jsnum.js_number_to_string(self.v)
        if k == "bool":
            return "true" if self.v else "false"
        if k == "str":
            return self.v
        if k == "color" or k == "udt":
            return "[object Object]"
        if k == "map":
            return "[object Map]"
        # array: Array.prototype.toString = join(","), null/undefined -> ""
        parts = []
        for v in self.v:
            if v.kind == "null" or v.kind == "undefined":
                parts.append("")
            else:
                parts.append(v.js_string())
        return ",".join(parts)

    def js_string_or_na(self):
        """`String(v ?? 'na')`, the runtime's display coercion for str() and
        string concatenation."""
        if self.kind == "null" or self.kind == "undefined":
            return "na"
        return self.js_string()


# Shared singletons for the payload-free variants. strict_eq compares by
# kind, so fresh instances would also compare equal, but downstream code
# should use these.
Value.Undefined = Value("undefined")
Value.Null = Value("null")


def _parse_f64(t):
    # Strict numeric parse: ASCII decimal/exponent forms plus
    # inf/infinity/nan (case-insensitive, signed), as the conformance
    # goldens expect. Python's float() is looser (it accepts digit-group
    # underscores and non-ASCII unicode digits), so guard those out first.
    if "_" in t or not t.isascii():
        return NAN
    try:
        return float(t)
    except ValueError:
        return NAN
