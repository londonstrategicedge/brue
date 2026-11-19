"""brue: the Brue trading EXECUTION language.

One import, one key: brue.login() verifies the LSE identity, then
buy()/sell()/close() place real orders through a Brue Connect broker
adapter. Scripts (.brue) run through run_script_full; a live driver
re-derives each closed bar's order intents from the script's own
deterministic window state (capture_intents) and hands them to the
connector. brue executes; research happens in plain Python on the results.
"""
from __future__ import annotations

__version__ = "0.0.2"


def run_script(source, bars):
    from .runtime import ExternalData
    return run_script_with_data(source, bars, ExternalData())


def run_script_with_data(source, bars, external):
    from .runtime import RunOptions
    return run_script_full(source, bars, external, {}, RunOptions())


def run_script_full(source, bars, external, input_overrides, options):
    """tokenize + parse + execute; errors come back inside the ExecOutput,
    never raised."""
    from . import parser, tokenizer
    from .errors import BrueError

    # Windows editors and Python text-mode writes produce CRLF; the
    # tokenizer speaks LF. Normalize at every entry point.
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    try:
        tokens = tokenizer.tokenize(source)
    except BrueError as e:
        return _error_output(e)
    try:
        program = parser.parse(tokens)
    except BrueError as e:
        return _error_output(e)
    from .runtime import execute_with_options
    return execute_with_options(program, bars, input_overrides, external, options)


# parse_time_arg and parse_bars_csv live in data.py with strict parsing
# (float()/int() alone are looser: underscores, unicode digits); re-exported
# here as public API.
from .data import parse_bars_csv, parse_time_arg  # noqa: E402,F401
from .api import BrueScriptError, Indicator  # noqa: E402,F401
from .auth import (Account, BrueAuthError, account, current_key,  # noqa: E402,F401
                   login, logout)
# The trading surface: login -> trade. Module import is stdlib-only; the
# broker connection (Brue Connect subprocess) spins up on the first call.
from .live import (BrueLiveError, balance, buy, close, disconnect,  # noqa: E402,F401
                   flatten, history, instruments, positions, sell)


def _error_output(e):
    from .output import ErrorInfo, ExecOutput
    from .value import Value
    return ExecOutput(
        success=False,
        commands=[],
        errors=[ErrorInfo(message=e.message, line=e.loc.line, col=e.loc.col,
                          phase=e.phase, suggestion=e.suggestion)],
        inputs=[],
        live_orders=[],
        live_closes=[],
        decl_title=Value.Null,
        decl_overlay=Value.Bool(True),
        decl_symbol=None,
        decl_timeframe=None,
    )


