"""brue CLI.

  brue run <script.brue> <bars.csv> [--capital N] [--commission N]
                                    [--slippage N] [--commission-type percent|fixed]
                                    [--from T] [--to T] [--resample TF]
                                    [--data NAME=path ...]
  brue resample <in.csv> <tf> <out.csv>
  brue gen <n> <seed> <out.csv>
  brue mathcheck

`run` is the engine's raw door: it prints the canonical execution JSON the
conformance suite byte-locks. brue run executes scripts over bars; research
happens in plain Python on the results.
"""
from __future__ import annotations

import sys

MASK64 = (1 << 64) - 1


def main(argv=None):
    args = list(sys.argv if argv is None else argv)
    cmd = args[1] if len(args) > 1 else None
    if cmd == "run" and len(args) >= 4:
        return cmd_run(args[2], args[3], args[4:])
    if cmd == "resample" and len(args) >= 5:
        return cmd_resample(args[2], args[3], args[4])
    if cmd == "gen" and len(args) >= 5:
        return cmd_gen(args[2], args[3], args[4])
    if cmd == "mathcheck":
        return cmd_mathcheck()
    sys.stderr.write(
        "usage: brue run <script.brue> <bars.csv> [--capital N] [--commission N] [--slippage N]\n"
        "                [--commission-type percent|fixed] [--from T] [--to T] [--resample TF]\n"
        "                [--data NAME=path ...]   (T: epoch ms or YYYY-MM-DD)\n"
        "       brue resample <in.csv> <tf> <out.csv>   (tf: 5m, 1h, 4h, 1d, 1w; epoch-aligned)\n"
        "       brue gen <n> <seed> <out.csv>\n"
        "       brue mathcheck\n")
    return 2


class RunSetup:
    def __init__(self):
        self.source = ""
        self.bars = []
        self.external = None
        self.options = None


def parse_run_setup(script_path, bars_path, rest):
    from . import parse_bars_csv, parse_time_arg
    from .data import parse_data_file, parse_timeframe_ms, resample_bars
    from .runtime import ExternalData, RunOptions

    setup = RunSetup()
    try:
        with open(script_path, encoding="utf-8") as f:
            setup.source = f.read()
    except OSError as e:
        raise ValueError(f"cannot read {script_path}: {e}")
    try:
        with open(bars_path, encoding="utf-8") as f:
            csv = f.read()
    except OSError as e:
        raise ValueError(f"cannot read {bars_path}: {e}")
    try:
        setup.bars = parse_bars_csv(csv)
    except ValueError as e:
        raise ValueError(f"bad csv: {e}")
    setup.external = ExternalData()
    setup.options = RunOptions()
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--resample":
            if i + 1 >= len(rest):
                raise ValueError("--resample needs a timeframe like 5m, 1h, 1d")
            tf = parse_timeframe_ms(rest[i + 1])
            if tf is None:
                raise ValueError(f"bad --resample timeframe '{rest[i + 1]}'")
            setup.bars = resample_bars(setup.bars, tf)
            i += 2
            continue
        if flag in ("--capital", "--commission", "--slippage"):
            # Run-level account/cost settings: never in the script text.
            if i + 1 >= len(rest):
                raise ValueError(f"{flag} needs a number")
            try:
                v = float(rest[i + 1])
            except ValueError:
                raise ValueError(f"bad {flag} value '{rest[i + 1]}'")
            setattr(setup.options, flag[2:], v)
            i += 2
            continue
        if flag == "--commission-type":
            if i + 1 >= len(rest) or rest[i + 1] not in ("percent", "fixed"):
                raise ValueError("--commission-type needs percent|fixed")
            setup.options.commission_type = rest[i + 1]
            i += 2
            continue
        if flag in ("--from", "--to"):
            if i + 1 >= len(rest):
                raise ValueError(f"{flag} needs a value (epoch ms or YYYY-MM-DD)")
            t = parse_time_arg(rest[i + 1])
            if t is None:
                raise ValueError(f"bad {flag} value '{rest[i + 1]}': epoch ms or YYYY-MM-DD")
            if flag == "--from":
                setup.options.trade_from = t
            else:
                setup.options.trade_to = t
            i += 2
            continue
        if flag == "--data":
            if i + 1 >= len(rest):
                raise ValueError("--data needs NAME=path")
            spec = rest[i + 1]
            if "=" not in spec:
                raise ValueError(f"--data needs NAME=path, got '{spec}'")
            name, path = spec.split("=", 1)
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                raise ValueError(f"cannot read {path}: {e}")
            try:
                kind, payload = parse_data_file(path, content)
            except ValueError as e:
                raise ValueError(f"bad data file: {e}")
            if kind == "snapshots":
                setup.external.dataset_snapshots[name] = payload
            elif kind == "foreign_bars":
                setup.external.use_bars[name] = payload
            else:
                setup.external.series_tables[name] = payload
            i += 2
            continue
        raise ValueError(f"unknown argument '{flag}'")
    return setup


def cmd_run(script_path, bars_path, rest):
    from . import run_script_full
    try:
        setup = parse_run_setup(script_path, bars_path, rest)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 2
    out = run_script_full(setup.source, setup.bars, setup.external, {}, setup.options)
    sys.stdout.write(out.to_json() + "\n")
    return 0


def cmd_resample(in_path, tf, out_path):
    from . import parse_bars_csv
    from .data import parse_timeframe_ms, resample_bars
    from .jsnum import js_number_to_string
    tf_ms = parse_timeframe_ms(tf)
    if tf_ms is None:
        sys.stderr.write(f"bad timeframe '{tf}': use forms like 5m, 1h, 4h, 1d, 1w\n")
        return 2
    try:
        with open(in_path, encoding="utf-8") as f:
            csv = f.read()
    except OSError as e:
        sys.stderr.write(f"cannot read {in_path}: {e}\n")
        return 1
    try:
        bars = parse_bars_csv(csv)
    except ValueError as e:
        sys.stderr.write(f"bad csv: {e}\n")
        return 1
    res = resample_bars(bars, tf_ms)
    lines = ["time,open,high,low,close,volume"]
    for b in res:
        lines.append(",".join(js_number_to_string(v) for v in
                              (b.time, b.open, b.high, b.low, b.close, b.volume)))
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        sys.stderr.write(f"cannot write {out_path}: {e}\n")
        return 1
    sys.stderr.write(f"{len(bars)} bars -> {len(res)} bars at {tf}\n")
    return 0


def _lcg_next(state):
    """64-bit wrapping LCG."""
    state = (state * 6364136223846793005 + 1442695040888963407) & MASK64
    return state


def cmd_gen(n_str, seed_str, out_path):
    """Deterministic synthetic OHLCV: integer LCG walk, prices printed as
    exact 5-decimal strings so every engine parses identical doubles."""
    try:
        n = int(n_str)
    except ValueError:
        n = 1000
    try:
        seed = int(seed_str)
    except ValueError:
        seed = 42
    state = (seed * 6364136223846793005 + 1442695040888963407) & MASK64

    def nxt():
        nonlocal state
        state = _lcg_next(state)
        return state >> 33

    price_pips = 10_000_000  # 100.00000
    start_ms = 1_700_000_000_000
    interval_ms = 3_600_000
    lines = ["time,open,high,low,close,volume"]
    for i in range(n):
        op = price_pips
        hi = op
        lo = op
        p = op
        for _ in range(4):
            step = (nxt() % 2001) - 1000
            p += step
            if p < 100_000:
                p = 100_000
            if p > hi:
                hi = p
            if p < lo:
                lo = p
        close = p
        volume = 1000 + abs(nxt() % 9001)

        def fmt(pips):
            # pips stay non-negative here, so floor division equals
            # truncation toward zero.
            return f"{pips // 100_000}.{abs(pips % 100_000):05d}"

        lines.append(f"{start_ms + i * interval_ms},{fmt(op)},{fmt(hi)},{fmt(lo)},{fmt(close)},{volume}")
        price_pips = close
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        sys.stderr.write(f"cannot write {out_path}: {e}\n")
        return 1
    return 0


def cmd_mathcheck():
    """Bit patterns of the four fdlibm functions over a fixed deterministic
    sweep, for cross-implementation comparison."""
    import math
    import struct

    from .jsmath import js_exp, js_log, js_log10, js_pow

    def bits(x):
        return struct.unpack("<Q", struct.pack("<d", x))[0]

    xs = []
    state = 0x9E3779B97F4A7C15

    def next_f():
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK64
        return (state >> 11) / float(1 << 53)

    for i in range(2000):
        base = (i - 1000.0) / 37.0
        xs.append(base + next_f() * 0.001)
    xs.extend([0.0, 1.0, -1.0, 0.5, 2.0, 10.0, 100.0, 709.0, -745.0, 1e-8,
               1e8, 0.1, 0.2, 0.3, 2.718281828459045])
    out = []
    for x in xs:
        out.append("%016x %016x %016x %016x %016x" % (
            bits(x), bits(js_exp(x)), bits(js_log(abs(x) + 1e-12)),
            bits(js_log10(abs(x) + 1e-12)),
            # The sweep is defined with fmod (truncates toward zero);
            # Python's float % floors, which differs for negative x.
            bits(js_pow(abs(x) + 0.001, math.fmod(x * 7.0, 5.0) + 0.5))))
    sys.stdout.write("\n".join(out) + "\n")
    return 0
