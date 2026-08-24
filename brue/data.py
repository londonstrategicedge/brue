# Host-side data readers: turn files into ExternalData channels. This is
# deliberately OUTSIDE the engine (runtime.py never reads a file); the CLI
# and library callers use these, other hosts (notebook, bindings) bring
# their own tables. stdlib-only, including the JSON parser below (the json
# module is not used: key ORDER and duplicate keys must survive exactly as
# the conformance goldens expect).
#
# Shape detection for `--data NAME=path`:
#   *.json                                -> keyed event snapshots (PIT
#                                            .latest/.prev), either a bare
#                                            array or {"snapshots": [...]}
#   *.csv with header time,open,high,low,close,volume
#                                          -> foreign symbol bars (use SYMBOL)
#   *.csv with header time,<anything else> -> plain series table
#                                            (alias.column per bar, as-of)
#
# Error mapping: every reader raises ValueError with a fixed message text;
# callers (cli.py) catch ValueError.
#
# Also housed here: parse_bars_csv and parse_time_arg (re-exported by
# __init__.py).
import functools
import math
from typing import List, Optional, Tuple

from .runtime import BarData, DatasetSnapshot, ForeignBar, SeriesTable
from .value import OrderedMap, UdtVal, Value


def _rust_parse_f64(s: str) -> Optional[float]:
    # Strict float parse: correctly rounded (same as Python float() and JS
    # Number() on the same text), accepts inf/infinity/nan and a leading
    # sign, but NOT underscores. Python's float() accepts "1_0", so that
    # is rejected here explicitly.
    if "_" in s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _f64_or_nan(s: str) -> float:
    # trimmed parse, NaN on failure
    v = _rust_parse_f64(s.strip())
    return v if v is not None else float("nan")


def _rust_trunc(x: float) -> float:
    # Truncate toward zero; identity on NaN/inf (math.trunc raises there).
    if not math.isfinite(x):
        return x
    return float(math.trunc(x))


class DataFile:
    """One parsed --data file. Each variant constructor returns a
    ("kind", payload) tuple so callers can unpack
    `kind, payload = parse_data_file(...)` (the shape cli.py matches on).
    Kinds: "snapshots", "foreign_bars", "series"."""

    @staticmethod
    def Snapshots(value: List[DatasetSnapshot]):
        return ("snapshots", value)

    @staticmethod
    def ForeignBars(value: List[ForeignBar]):
        return ("foreign_bars", value)

    @staticmethod
    def Series(value: SeriesTable):
        return ("series", value)


def parse_data_file(path: str, content: str):
    """Parse one data file's content into its detected channel. `path` is
    used only for extension detection and error messages."""
    if path.lower().endswith(".json"):
        return DataFile.Snapshots(parse_snapshots_json(content))
    # CSV: header decides bars vs table
    lines = _rust_lines(content)
    li = 0
    header = None
    while True:
        if li >= len(lines):
            raise ValueError("{}: empty file".format(path))
        l = lines[li]
        li += 1
        if l.strip() == "":
            continue
        header = l.strip()
        break
    cols = [c.strip() for c in header.split(",")]
    if not cols or cols[0] != "time":
        first = cols[0] if cols else ""
        raise ValueError("{}: first CSV column must be 'time', got '{}'".format(path, first))
    if (
        len(cols) == 6
        and cols[1] == "open"
        and cols[2] == "high"
        and cols[3] == "low"
        and cols[4] == "close"
        and cols[5] == "volume"
    ):
        # Foreign OHLCV bars. Same number parsing as the primary bars
        # reader: the correctly-rounded parser == JS Number().
        bars: List[ForeignBar] = []
        for line in lines[li:]:
            line = line.strip()
            if line == "":
                continue
            p = line.split(",")
            if len(p) < 6:
                raise ValueError("{}: expected 6 columns, got {}".format(path, len(p)))
            f = _f64_or_nan
            bars.append(
                ForeignBar(
                    time=f(p[0]),
                    open=f(p[1]),
                    high=f(p[2]),
                    low=f(p[3]),
                    close=f(p[4]),
                    volume=f(p[5]),
                )
            )
        return DataFile.ForeignBars(bars)
    # Plain series table: time + N numeric columns.
    names = cols[1:]
    if not names:
        raise ValueError("{}: a series table needs at least one column besides time".format(path))
    times: List[float] = []
    columns: List[Tuple[str, List[float]]] = [(n, []) for n in names]
    for line in lines[li:]:
        line = line.strip()
        if line == "":
            continue
        p = line.split(",")
        if len(p) != len(columns) + 1:
            raise ValueError("{}: expected {} columns, got {}".format(path, len(columns) + 1, len(p)))
        f = _f64_or_nan
        times.append(f(p[0]))
        for i, (_, v) in enumerate(columns):
            v.append(f(p[i + 1]))
    return DataFile.Series(SeriesTable(times=times, columns=columns))


def _rust_lines(content: str):
    # Split on \n, strip one trailing \r per line, and do NOT emit a final
    # empty line after a trailing \n. Python's splitlines() would also split
    # on \v, \f, \x1c.. and U+2028, so it is not used.
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [l[:-1] if l.endswith("\r") else l for l in lines]


# ---- Snapshots JSON -----------------------------------------------------
# Each snapshot object must carry a numeric "timestamp" (same unit as bar
# times) and a string "key"; every entry of the object, including those
# two, becomes a snapshot field in file order (member access sees exactly
# the file's keys).


def parse_snapshots_json(content: str) -> List[DatasetSnapshot]:
    root = parse_json(content)
    if isinstance(root, JVal.Arr):
        arr = root.value
    elif isinstance(root, JVal.Obj):
        found = next((e for e in root.value if e[0] == "snapshots"), None)
        if found is not None and isinstance(found[1], JVal.Arr):
            arr = found[1].value
        else:
            raise ValueError('snapshots JSON must be an array or {"snapshots": [...]}')
    else:
        raise ValueError('snapshots JSON must be an array or {"snapshots": [...]}')
    out: List[DatasetSnapshot] = []
    for i, item in enumerate(arr):
        if not isinstance(item, JVal.Obj):
            raise ValueError("snapshot {} is not an object".format(i))
        entries = item.value
        timestamp: Optional[float] = None
        key: Optional[str] = None
        for k, v in entries:
            if k == "timestamp":
                if isinstance(v, JVal.Num):
                    timestamp = v.value
            elif k == "key":
                if isinstance(v, JVal.Str):
                    key = v.value
        if timestamp is None:
            raise ValueError("snapshot {} has no numeric 'timestamp'".format(i))
        if key is None:
            raise ValueError("snapshot {} has no string 'key'".format(i))
        fields = [(k, jval_to_value(v)) for k, v in entries]
        out.append(DatasetSnapshot(timestamp=timestamp, key=key, fields=fields))
    return out


def jval_to_value(v) -> Value:
    if isinstance(v, JVal.Null):
        return Value.Null
    if isinstance(v, JVal.Bool):
        return Value.Bool(v.value)
    if isinstance(v, JVal.Num):
        return Value.Num(v.value)
    if isinstance(v, JVal.Str):
        return Value.str(v.value)
    # Nested structures are converted ONCE at load, so repeated member
    # access returns the same reference.
    if isinstance(v, JVal.Arr):
        return Value.Array([jval_to_value(x) for x in v.value])
    # JVal.Obj
    fields = OrderedMap()
    for k, val in v.value:
        fields.set(k, jval_to_value(val))
    return Value.Udt(UdtVal("__object", fields))


# ---- Minimal JSON parser (stdlib-only) ----------------------------------
# Full JSON: null/true/false, numbers (via the correctly-rounded float
# parser, which matches V8's JSON.parse for the same text), strings with
# every escape including \uXXXX surrogate pairs, arrays, objects with key
# order preserved. No extensions, no trailing commas.


class JVal:
    """Parsed JSON value. Variant payloads in .value."""

    class Null:
        pass

    class Bool:
        def __init__(self, value: bool) -> None:
            self.value = value

    class Num:
        def __init__(self, value: float) -> None:
            self.value = value

    class Str:
        def __init__(self, value: str) -> None:
            self.value = value

    class Arr:
        def __init__(self, value: list) -> None:
            self.value = value

    class Obj:
        def __init__(self, value: List[Tuple[str, "JVal"]]) -> None:
            self.value = value


def parse_json(text: str):
    # Positions are BYTE offsets into the UTF-8 encoding (error messages
    # report byte positions). pos is a 1-element list so the helpers can
    # advance it in place.
    b = text.encode("utf-8")
    pos = [0]
    v = _parse_value(b, pos)
    _skip_ws(b, pos)
    if pos[0] != len(b):
        raise ValueError("unexpected trailing content at byte {}".format(pos[0]))
    return v


def _skip_ws(b: bytes, pos) -> None:
    while pos[0] < len(b) and b[pos[0]] in (0x20, 0x09, 0x0A, 0x0D):
        pos[0] += 1


def _parse_value(b: bytes, pos):
    _skip_ws(b, pos)
    if pos[0] >= len(b):
        raise ValueError("unexpected end of JSON")
    c = b[pos[0]]
    if c == ord("n"):
        _expect_lit(b, pos, "null")
        return JVal.Null()
    if c == ord("t"):
        _expect_lit(b, pos, "true")
        return JVal.Bool(True)
    if c == ord("f"):
        _expect_lit(b, pos, "false")
        return JVal.Bool(False)
    if c == ord('"'):
        return JVal.Str(_parse_string(b, pos))
    if c == ord("["):
        pos[0] += 1
        items = []
        _skip_ws(b, pos)
        if pos[0] < len(b) and b[pos[0]] == ord("]"):
            pos[0] += 1
            return JVal.Arr(items)
        while True:
            items.append(_parse_value(b, pos))
            _skip_ws(b, pos)
            nxt = b[pos[0]] if pos[0] < len(b) else None
            if nxt == ord(","):
                pos[0] += 1
            elif nxt == ord("]"):
                pos[0] += 1
                return JVal.Arr(items)
            else:
                raise ValueError("expected ',' or ']' at byte {}".format(pos[0]))
    if c == ord("{"):
        pos[0] += 1
        entries = []
        _skip_ws(b, pos)
        if pos[0] < len(b) and b[pos[0]] == ord("}"):
            pos[0] += 1
            return JVal.Obj(entries)
        while True:
            _skip_ws(b, pos)
            if pos[0] >= len(b) or b[pos[0]] != ord('"'):
                raise ValueError("expected object key at byte {}".format(pos[0]))
            key = _parse_string(b, pos)
            _skip_ws(b, pos)
            if pos[0] >= len(b) or b[pos[0]] != ord(":"):
                raise ValueError("expected ':' at byte {}".format(pos[0]))
            pos[0] += 1
            val = _parse_value(b, pos)
            entries.append((key, val))
            _skip_ws(b, pos)
            nxt = b[pos[0]] if pos[0] < len(b) else None
            if nxt == ord(","):
                pos[0] += 1
            elif nxt == ord("}"):
                pos[0] += 1
                return JVal.Obj(entries)
            else:
                raise ValueError("expected ',' or '}' at byte {}".format(pos[0]))
    if c == ord("-") or (ord("0") <= c <= ord("9")):
        return _parse_number(b, pos)
    raise ValueError("unexpected character '{}' at byte {}".format(chr(c), pos[0]))


def _expect_lit(b: bytes, pos, lit: str) -> None:
    lb = lit.encode("ascii")
    if len(b) - pos[0] >= len(lb) and b[pos[0] : pos[0] + len(lb)] == lb:
        pos[0] += len(lb)
    else:
        raise ValueError("invalid literal at byte {}".format(pos[0]))


def _parse_number(b: bytes, pos):
    start = pos[0]
    if pos[0] < len(b) and b[pos[0]] == ord("-"):
        pos[0] += 1
    while pos[0] < len(b) and ord("0") <= b[pos[0]] <= ord("9"):
        pos[0] += 1
    if pos[0] < len(b) and b[pos[0]] == ord("."):
        pos[0] += 1
        while pos[0] < len(b) and ord("0") <= b[pos[0]] <= ord("9"):
            pos[0] += 1
    if pos[0] < len(b) and b[pos[0]] in (ord("e"), ord("E")):
        pos[0] += 1
        if pos[0] < len(b) and b[pos[0]] in (ord("+"), ord("-")):
            pos[0] += 1
        while pos[0] < len(b) and ord("0") <= b[pos[0]] <= ord("9"):
            pos[0] += 1
    s = b[start : pos[0]].decode("utf-8")
    # No underscore check needed: the scanner above only admits [-+.0-9eE].
    try:
        return JVal.Num(float(s))
    except ValueError:
        raise ValueError("bad number '{}' at byte {}".format(s, start))


def _parse_string(b: bytes, pos) -> str:
    # caller guarantees b[pos] == '"'
    pos[0] += 1
    out: List[str] = []
    while True:
        if pos[0] >= len(b):
            raise ValueError("unterminated string")
        c = b[pos[0]]
        if c == ord('"'):
            pos[0] += 1
            return "".join(out)
        if c == ord("\\"):
            pos[0] += 1
            if pos[0] >= len(b):
                raise ValueError("unterminated escape")
            e = b[pos[0]]
            pos[0] += 1
            if e == ord('"'):
                out.append('"')
            elif e == ord("\\"):
                out.append("\\")
            elif e == ord("/"):
                out.append("/")
            elif e == ord("b"):
                out.append("\u0008")
            elif e == ord("f"):
                out.append("\u000c")
            elif e == ord("n"):
                out.append("\n")
            elif e == ord("r"):
                out.append("\r")
            elif e == ord("t"):
                out.append("\t")
            elif e == ord("u"):
                hi = _parse_hex4(b, pos)
                # surrogate pair: a high surrogate must be followed by
                # \uDC00..DFFF; unpaired surrogates become the replacement
                # character (the output is UTF-8, which cannot hold a lone
                # surrogate).
                if 0xD800 <= hi < 0xDC00:
                    if (
                        pos[0] < len(b)
                        and b[pos[0]] == ord("\\")
                        and pos[0] + 1 < len(b)
                        and b[pos[0] + 1] == ord("u")
                    ):
                        save = pos[0]
                        pos[0] += 2
                        lo = _parse_hex4(b, pos)
                        if 0xDC00 <= lo < 0xE000:
                            cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                            out.append(chr(cp))
                        else:
                            pos[0] = save
                            out.append("�")
                    else:
                        out.append("�")
                elif 0xDC00 <= hi < 0xE000:
                    out.append("�")
                else:
                    out.append(chr(hi))
            else:
                raise ValueError("bad escape '\\{}' at byte {}".format(chr(e), pos[0] - 1))
        else:
            # copy one UTF-8 scalar (multi-byte safe). The byte length comes
            # from the lead byte; input originated from a Python str, so the
            # bytes are always valid UTF-8.
            if c < 0x80:
                ln = 1
            elif c < 0xE0:
                ln = 2
            elif c < 0xF0:
                ln = 3
            else:
                ln = 4
            try:
                ch = b[pos[0] : pos[0] + ln].decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("invalid utf-8")
            out.append(ch)
            pos[0] += ln


def _parse_hex4(b: bytes, pos) -> int:
    if len(b) - pos[0] < 4:
        raise ValueError("bad \\u escape")
    s = b[pos[0] : pos[0] + 4]
    # int(_, 16) also accepts "+f00", " f00", "0x2a" and underscores; JSON
    # allows hex digits only, so validate the exact grammar.
    if not all(c in b"0123456789abcdefABCDEF" for c in s):
        raise ValueError("bad \\u escape")
    v = int(s.decode("ascii"), 16)
    pos[0] += 4
    return v


# ---- OHLCV resampling ---------------------------------------------------


def parse_timeframe_ms(tf: str) -> Optional[float]:
    """"5m" / "1h" / "1d" style timeframe to milliseconds. Weeks are the
    largest unit offered: months are not a fixed number of ms and would need
    calendar bucketing, so they are deliberately not accepted here."""
    tf = tf.strip()
    if len(tf) < 2:
        return None
    num, unit = tf[:-1], tf[-1:]
    n = _rust_parse_f64(num)
    if n is None:
        return None
    if not (n > 0.0) or n != _rust_trunc(n):
        return None
    if unit == "s":
        unit_ms = 1_000.0
    elif unit == "m":
        unit_ms = 60_000.0
    elif unit == "h":
        unit_ms = 3_600_000.0
    elif unit == "d":
        unit_ms = 86_400_000.0
    elif unit == "w":
        unit_ms = 604_800_000.0
    else:
        return None
    return n * unit_ms


def resample_bars(bars: List[BarData], tf_ms: float) -> List[BarData]:
    """Aggregate bars into `tf_ms` buckets aligned to the epoch (floor of
    time/tf, NEVER local-calendar truncation: the host timezone must not move
    bucket edges). Input is sorted by time first, so out-of-order rows in a
    user CSV cannot smear a bucket. Per bucket: first open, max high, min
    low, last close, summed volume; the bar's time is the bucket start."""
    if not bars or not (tf_ms > 0.0):
        return []
    # Stable sort by time where a NaN time compares Equal (keeps its input
    # position), as the conformance goldens expect; cmp_to_key over < and >
    # gives exactly that comparator.
    def cmp(a: BarData, b: BarData) -> int:
        if a.time < b.time:
            return -1
        if a.time > b.time:
            return 1
        return 0

    sorted_bars = sorted(bars, key=functools.cmp_to_key(cmp))

    out: List[BarData] = []
    current_bucket = float("nan")  # NaN != anything, so the first row always opens a bucket
    for b in sorted_bars:
        # Dirty rows are dropped WHOLE: partial NaN handling would be
        # asymmetric (a NaN high in the first row of a bucket sticks, in a
        # later row it vanishes, and a NaN volume poisons the whole sum).
        if (
            not math.isfinite(b.time)
            or not math.isfinite(b.open)
            or not math.isfinite(b.high)
            or not math.isfinite(b.low)
            or not math.isfinite(b.close)
        ):
            continue
        vol = b.volume if math.isfinite(b.volume) else 0.0
        bucket = math.floor(b.time / tf_ms) * tf_ms
        if bucket != current_bucket:
            current_bucket = bucket
            out.append(
                BarData(
                    time=bucket,
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=vol,
                )
            )
        else:
            last = out[-1]
            if b.high > last.high:
                last.high = b.high
            if b.low < last.low:
                last.low = b.low
            last.close = b.close
            last.volume += vol
    return out


# ---- Bars CSV + host time args ------------------------------------------


def parse_bars_csv(content: str) -> List[BarData]:
    """Parse a bars CSV: header `time,open,high,low,close,volume`, one bar
    per line. Numbers parse with the correctly-rounded decimal parser, which
    matches JS Number() on the same strings."""
    bars: List[BarData] = []
    for i, line in enumerate(_rust_lines(content)):
        line = line.strip()
        if line == "":
            continue
        if i == 0 and line.startswith("time"):
            continue
        parts = line.split(",")
        if len(parts) < 6:
            raise ValueError("line {}: expected 6 columns, got {}".format(i + 1, len(parts)))
        f = _f64_or_nan
        bars.append(
            BarData(
                time=f(parts[0]),
                open=f(parts[1]),
                high=f(parts[2]),
                low=f(parts[3]),
                close=f(parts[4]),
                volume=f(parts[5]),
            )
        )
    return bars


def _rust_parse_i64(s: str) -> Optional[int]:
    # Strict integer parse: optional sign then ASCII digits only. Python's
    # int() also accepts surrounding whitespace, underscores and unicode
    # digits, so validate the exact grammar first.
    if s == "":
        return None
    body = s[1:] if s[0] in "+-" else s
    if body == "" or not all("0" <= c <= "9" for c in body):
        return None
    return int(s)


def parse_time_arg(s: str) -> Optional[float]:
    """Parse a host time argument: either raw epoch milliseconds or a UTC
    calendar date "YYYY-MM-DD" (midnight). Epoch math only, never local time
    (the server's timezone must not shift a backtest window)."""
    s = s.strip()
    if s != "" and all("0" <= c <= "9" for c in s):
        return _rust_parse_f64(s)
    parts = s.split("-")
    if len(parts) != 3:
        return None
    y = _rust_parse_i64(parts[0])
    m = _rust_parse_i64(parts[1])
    d = _rust_parse_i64(parts[2])
    if y is None or m is None or d is None:
        return None
    if not (1 <= m <= 12) or not (1 <= d <= 31):
        return None
    # Howard Hinnant's days_from_civil: days since 1970-01-01 in the
    # proleptic Gregorian calendar. The era step is defined with integer
    # division truncating toward zero; the numerator is pre-adjusted for
    # that, so // over abs() reproduces it.
    y_adj = y - 1 if m <= 2 else y
    num = y_adj if y_adj >= 0 else y_adj - 399
    era = abs(num) // 400 if num >= 0 else -(abs(num) // 400)
    yoe = y_adj - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    days = era * 146097 + doe - 719468
    return float(days * 86_400_000)
