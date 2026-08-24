"""Host-facing helpers over the script engine.

The engine internals speak BarData lists and ExecOutput (conformance-locked,
do not touch). This module is the indicator bridge: indicator_series() and
Indicator turn a Brue script's plot() calls into named per-bar series so a
chart host (such as the LSE Terminal) can draw them. brue executes orders;
it does not report simulated P&L.

Accepted data shapes: a pandas DataFrame (any case-insensitive subset of
time/open/high/low/close/volume columns, time in epoch s, epoch ms, or
datetimes), a list of dicts with those keys, a list of [t,o,h,l,c,v] rows,
or a path to a csv. pandas is NOT a dependency: it is duck-typed, so the
package installs clean without it.
"""
from __future__ import annotations

import json


class BrueScriptError(Exception):
    """The script failed to tokenize/parse/run; message is user-facing."""

    def __init__(self, errors):
        self.errors = errors
        first = errors[0] if errors else {}
        super().__init__(first.get("message", "script error"))


def _to_ms(t) -> float:
    """Epoch seconds, epoch ms, or datetime-like -> epoch ms."""
    if hasattr(t, "timestamp"):
        return float(t.timestamp() * 1000.0)
    x = float(t)
    # Heuristic: values below 1e11 are seconds (that is the year 5138);
    # anything bigger is already ms.
    return x * 1000.0 if x < 1e11 else x


def _bars_from_any(data):
    from .runtime import BarData

    if isinstance(data, str):
        from . import parse_bars_csv
        with open(data, encoding="utf-8") as f:
            return parse_bars_csv(f.read())

    # pandas DataFrame, duck-typed so pandas stays optional.
    if hasattr(data, "columns") and hasattr(data, "itertuples"):
        cols = {str(c).strip().lower(): c for c in data.columns}
        tcol = next((cols[k] for k in ("time", "ts", "timestamp", "date",
                                       "datetime") if k in cols), None)
        need = [k for k in ("open", "high", "low", "close") if k not in cols]
        if tcol is None or need:
            missing = (["time"] if tcol is None else []) + need
            raise ValueError(f"data is missing columns: {', '.join(missing)}")
        # The index is never used as the time column: explicit only, since
        # a wrong guess silently shifts every fill.
        out = []
        vol = cols.get("volume")
        for row in data.itertuples(index=False):
            d = dict(zip(data.columns, row))
            out.append(BarData(
                time=_to_ms(d[tcol]),
                open=float(d[cols["open"]]), high=float(d[cols["high"]]),
                low=float(d[cols["low"]]), close=float(d[cols["close"]]),
                volume=float(d[vol]) if vol is not None else 0.0))
        return out

    if isinstance(data, (list, tuple)) and data:
        first = data[0]
        if isinstance(first, dict):
            return [BarData(time=_to_ms(r.get("time", r.get("ts", r.get("timestamp")))),
                            open=float(r["open"]), high=float(r["high"]),
                            low=float(r["low"]), close=float(r["close"]),
                            volume=float(r.get("volume", 0.0)))
                    for r in data]
        if isinstance(first, (list, tuple)) and len(first) >= 5:
            return [BarData(time=_to_ms(r[0]), open=float(r[1]), high=float(r[2]),
                            low=float(r[3]), close=float(r[4]),
                            volume=float(r[5]) if len(r) > 5 else 0.0)
                    for r in data]
    raise ValueError("unsupported data shape: pass a DataFrame, list of "
                     "dicts, list of [t,o,h,l,c,v] rows, or a csv path")


def indicator_series(script: str, data, inputs: dict | None = None) -> dict:
    """
    Run a Brue script as a chart INDICATOR: every plot() call becomes a
    named series aligned to the bars. This is how chart hosts (such as the
    LSE Terminal, or a notebook) draw Brue-written indicators on live charts.

    Returns {"plots": {name: [float per bar]}, "meta": {name: kwargs},
             "overlay": bool, "inputs": [declared input() specs],
             "times": [epoch ms per bar]}.
    Raises BrueScriptError on script errors.
    """
    from . import run_script_full
    from .runtime import ExternalData, RunOptions
    from .value import Value

    bars = _bars_from_any(data)
    options = RunOptions()
    options.collect_plots = True
    overrides = {}
    for k, v in (inputs or {}).items():
        overrides[str(k)] = (Value.Bool(v) if isinstance(v, bool)
                             else Value.Num(v) if isinstance(v, (int, float))
                             else Value.Str(str(v)))
    out = run_script_full(script, bars, ExternalData(), overrides, options)
    raw = json.loads(out.to_json())
    if not raw.get("success"):
        raise BrueScriptError(raw.get("errors", []))
    n = len(bars)
    plots = {}
    for name, points in getattr(out, "plots", {}).items():
        col = [float("nan")] * n
        for bar_i, v in points:
            if 0 <= bar_i < n:
                col[bar_i] = v
        plots[name] = col
    return {"plots": plots,
            "meta": getattr(out, "plot_meta", {}),
            "overlay": bool((raw.get("declaration") or {}).get("overlay", True)),
            "inputs": raw.get("inputs", []),
            "times": [b.time for b in bars]}


class Indicator:
    """A Brue indicator as a first-class object: its plot() calls become
    named series.

        indicator = brue.Indicator(source)
        indicator.compute(df)      # pandas DataFrame, one column per plot
        indicator.series(df)       # plain dict of lists (no pandas needed)
    """

    def __init__(self, source: str):
        self.source = source

    @classmethod
    def from_file(cls, path) -> "Indicator":
        with open(path, encoding="utf-8") as f:
            return cls(f.read())

    def series(self, data, **inputs) -> dict:
        r = indicator_series(self.source, data, inputs=inputs or None)
        return r["plots"]

    def compute(self, data, **inputs):
        import pandas as pd
        r = indicator_series(self.source, data, inputs=inputs or None)
        df = pd.DataFrame(r["plots"])
        df.insert(0, "time", pd.to_datetime(r["times"], unit="ms"))
        return df

    @property
    def overlay(self) -> bool:
        return bool(indicator_series(self.source, _PROBE_ROWS)["overlay"])

    def __repr__(self):
        import re
        m = re.search(r'strategy\(\s*"([^"]*)"', self.source)
        return f'Indicator("{m.group(1) if m else "untitled"}")'


# Tiny deterministic bars for cheap structural probes (overlay flag etc).
_PROBE_ROWS = [[1700000000000 + i * 3600000, 100.0, 100.6, 99.5, 100.2, 1000.0]
               for i in range(60)]
