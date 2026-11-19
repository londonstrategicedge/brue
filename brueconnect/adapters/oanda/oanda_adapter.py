#!/usr/bin/env python3
"""Brue Connect adapter for OANDA (the v20 REST API: forex, metals and index,
commodity and bond CFDs; practice and live).

OANDA's wire is plain: JSON over HTTPS on a REST host, with one personal
bearer token, and two chunked HTTP streams of JSON lines on a stream host
(prices, and every transaction of the account). Numbers arrive as strings,
times as UNIX seconds with nine decimals (this adapter asks for that format),
and an order request is answered with the transactions it made: a market
order's fill, or its cancellation, is in the answer itself. Everything that
happens later (a resting order that fills, a stop that triggers, financing,
a transfer) arrives on the transaction stream, numbered; a gap is filled from
`/transactions/sinceid`.

Shape of the thing:

  * `hello` answers COLD and LIVE IS OPT-IN. The environment is practice
    unless `--live` or OANDA_ENV=live is given (a flag wins over the
    variable; both flags together are live). `mode` is answered by the HOSTS:
    "paper" only when the REST host is `api-fxpractice.oanda.com` and the
    stream host `stream-fxpractice.oanda.com` exactly, or this machine (a
    stand-in on loopback). Any other host is "live" whatever the flags say.
    The token selects nothing (OANDA gives it no mark of its environment).
    A plain-http endpoint that is not this machine is refused: the token
    would cross the network in clear.
  * One token reaches every v20 sub-account of its login (SPEC 4, *Choosing
    an account*): `accounts.list` answers before any session, and the
    platform binds a process to one with BRUE_ACCOUNT_ID. With no binding, a
    token that reaches one account uses it; one that reaches several is
    refused (`x_oanda_choose_account`): the adapter never picks for the user.
    `hello` must state the account's currency and its model (netting or
    hedging) before anything is read, so both are remembered per account in
    BRUE_BROKER_STATE when `accounts.list` or a session reads them; if a cold
    `hello` had to guess and the guess was wrong, the session is refused
    with the protocol's `reconnect` (SPEC 6), in words that also tell a
    platform that does not know the code to connect again: the next start
    says the truth. The SDK's Connector starts this adapter again by itself,
    once, inside the one connect.
  * Threads: the SDK's read thread only READS and hands each request to a
    lane: calls that change something one at a time in the order read;
    `flatten_all` on a lane of its own, which every other change yields to;
    reads a few at once; each `bars.history` on a thread of its own. Every
    handler runs under ONE deadline of 8 s counted from when its line was read
    (the platform waits 10). One thread reads the transaction stream, one the
    price stream, and one backstop fills gaps, watches orders whose answer was
    lost, keeps the stops and targets of a netting position on all of its
    trades, polls trailing stops, bar subscriptions and the account.
  * Limits (documented, per requesting IP address: 120 requests a second, 20
    streams, 2 new connections a second): REST goes over a few kept-alive
    connections, at most 50 requests a second; every new connection, REST or
    stream, waits its turn at least 0.55 s after the last, the last of any
    process of the profile (its time is kept in the state folder, so a
    process started again at once waits for it); a 429 pauses REST
    by itself (no Retry-After is documented), doubling up to 8 s.
  * Identity. A NETTING sub-account (the UK default) has one position per
    instrument: `position_id` is the instrument (`EUR_USD`), although OANDA
    keeps one Trade per opening fill under it. A HEDGING sub-account has one
    position per Trade: `position_id` is the trade id. The platform's
    `client_id` travels as `clientExtensions.id` of the order and of the
    trade it opens (hashed to `brue-<32 hex>` when it does not look like a
    plain id), never on an MT4-linked sub-account (OANDA: "Do not set,
    modify, or delete this field if your account is associated with MT4").
  * Idempotency (SPEC 2) is this adapter's: OANDA refuses a repeated client
    id only while that order is pending or its trade open, so a market order
    sent twice would trade twice. Results are kept by client id in the state
    file, final refusals too, and every order or close request is WRITTEN
    DOWN before it leaves. An order or a close is sent ONCE: when its answer
    is lost it is never sent again, because OANDA may still carry it out
    late and nothing there would stop a second one (a netting reduce opens
    no trade; an MT4-linked account carries no client id). OANDA's own
    record is read instead (`/transactions/sinceid` from the last
    transaction seen before it was sent) for the order under its client id:
    found, it is the answer; not found, the call answers a retryable "not
    known" error, nothing is announced, and `session.state` says so; a retry
    is told the same while OANDA's record is watched for it (two minutes).
    After that it is final: `x_oanda_not_carried_out` (with `order.rejected`
    for an order), and placing it again is a new order under a new client
    id, the platform's decision; or, for a close whose position the record
    shows closed or reduced since it was sent by anything not marked as it
    (a closeout with no marks, which may be this one; a stop, a target,
    another tool's close of its trade), `x_oanda_close_unmarked`, which says
    what the record shows. The record is read from the highest transaction
    id seen when the request left. One that turns up later still is applied
    and reported. The words name a request by what it does and when it was
    sent ("the close of 1000 EUR_USD sent at 22:31:05 UTC"), never by the
    platform's client_id.
  * The book is made of OANDA's transactions, each applied ONCE: the answer
    to a request, the stream and the gap filler all carry OANDA's
    transaction id (a fill's `fill_id`). OANDA's list of open trades is
    taken as the book only when it is of exactly the transactions applied
    so far; a newer one is caught up first (its transactions applied, their
    events sent), an older one refreshes the figures only.
  * The state file is shared by every process of the profile (one lists the
    accounts while another trades): each process writes only what IT
    changed, onto the file as it is on disk, under a lock, and each
    account's memory is kept apart.

What OANDA does that a platform must know:

  * Market orders are sent fill-or-kill (OANDA's documented default): all or
    nothing, never part. OANDA's execution policy says its FX, metal, index
    and commodity orders never fill in parts at all (an IOC market order is
    the one exception, and this adapter does not send one), so no
    `order.partial` is ever sent. A market order OANDA cancels at once
    (INSUFFICIENT_MARGIN, MARKET_HALTED, INSUFFICIENT_LIQUIDITY, a stop or
    target on the wrong side) is refused with the mapped code and
    `order.rejected`.
  * Resting orders: `limit` and `stop` (OANDA's LIMIT and STOP). No
    stop-limit exists (a STOP's priceBound only cancels it on a gap), no OCO,
    no close-by. Expiry `gtc`, `day` (GFD: cancelled at 5 pm New York) and
    `gtd`. Margin is checked when one triggers, not when it is placed.
    `order.amend` is OANDA's replace: a new order id each time; the platform
    keeps the first one, mapped here to the newest.
  * Stops, targets and trailing stops are orders OANDA holds on each trade.
    On a netting position the levels the user sets are put on every one of
    its trades (`order.modify` sets each; a market order that adds to the
    position carries them; a resting order's fill that adds a trade is given
    them within seconds). `sl`/`tp` on an order that ADDS to an open
    position of the same side, or only reduces one, are refused
    (`x_oanda_position_open`): set the position's levels with
    `order.modify`. Trades whose levels differ are shown as such: the
    position's `sl`/`tp` are then null and `levels` lists each trade's.
  * `trail_points` is OANDA's trailing stop distance in ticks; OANDA moves
    it (`position.trailed` is read from OANDA's account changes every 3 s).
  * Money is all in the account's home currency (OANDA's AccountUnits).
    Financing is booked by OANDA every day at 5 pm New York: it reaches the
    platform as a `balance.operation` (kind `other`) and in each position's
    `swap`.
  * Not available to EU residents (OANDA TMS Brokers) or to clients of OANDA
    Global Markets: OANDA offers its v20 API to neither, and no token will
    work for them. Spread-bet sub-accounts are not documented over v20.

Configuration (environment):

  BRUE_CRED_TOKEN                     the personal access token the platform
                                      collected (form key `token`); set it by
                                      hand to run without a platform. No other
                                      variable is read for it.
  BRUE_ACCOUNT_ID                     the v20 account this process deals on
  OANDA_ENV = practice | live         practice unless this says live or
                                      --live is given; a flag wins
  OANDA_REST_URL, OANDA_STREAM_URL    endpoints, for a test stand-in. On
                                      practice (the default, and always with
                                      --paper) only OANDA's practice hosts or
                                      this machine are taken; anything else
                                      is ignored, and said. On live they are
                                      taken as given (anything but OANDA's
                                      practice hosts or loopback is mode
                                      live). One that is not a plain host and
                                      port (a user name, a backslash, a
                                      %-escape, brackets around anything but
                                      one IPv6 address, a port that is not a
                                      number, a space or a line break) is
                                      refused, and the process keeps its own
                                      endpoint. A request answered 307 or 308
                                      is followed only if its method is GET,
                                      only once, and only to the same scheme,
                                      host and port as the endpoint it was
                                      sent to (its path and query may
                                      change), on REST and the streams alike.
                                      Nothing else is followed. A request
                                      that is not a GET (whatever its kind,
                                      the levels kept on a trade included)
                                      answered 307 or 308 was not carried
                                      out: refused, and never sent again (an
                                      order so refused is final for its
                                      client_id, order.rejected); answered
                                      another 3xx it is not sent again
                                      either, and its fate is not known
                                      until OANDA's record says. Another 3xx
                                      on a GET, a second redirect, or a
                                      target on another scheme, host or port
                                      is refused. A refusal says which and
                                      names the target's host, never its
                                      path or query (OandaClient._follow)
  BRUE_BROKER_STATE                   private directory: oanda_state.json
"""
from __future__ import annotations

import collections
import decimal
import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
import queue
import re
import select
import socket
import ssl
import sys
import threading
import time
import unicodedata
import urllib.parse
import uuid
from decimal import Decimal

try:                       # POSIX: one writer of the state file at a time
    import fcntl
except ImportError:        # pragma: no cover - Windows
    fcntl = None
try:                       # Windows: the same, with the C runtime's lock
    import msvcrt
except ImportError:
    msvcrt = None

# The SDK lives at the repo root, three levels up from this file. Resolved from
# __file__ so the adapter always runs against the SDK it ships with.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect import protocol  # noqa: E402
from brueconnect.adapter import (  # noqa: E402
    MUTATING, PRE_SESSION, Adapter, AdapterError)

VERSION = "0.1"
USER_AGENT = "brue-connect-oanda/" + VERSION
DOCS = "https://developer.oanda.com/rest-live-v20/"

# documented: https://developer.oanda.com/rest-live-v20/development-guide/
# (fxTrade Practice and fxTrade, a REST host and a stream host each)
PRACTICE_REST_URL = "https://api-fxpractice.oanda.com"
PRACTICE_STREAM_URL = "https://stream-fxpractice.oanda.com"
LIVE_REST_URL = "https://api-fxtrade.oanda.com"
LIVE_STREAM_URL = "https://stream-fxtrade.oanda.com"
# The ONLY names that are paper money: no real money can move through them.
PRACTICE_REST_HOST = "api-fxpractice.oanda.com"
PRACTICE_STREAM_HOST = "stream-fxpractice.oanda.com"

# What OANDA's licence asks (SPEC 3.3). documented:
# https://legal.oanda.com/?code=api_licence_agreement&language=en (OANDA Europe
# Limited, April 2026): 4.3(d) the OANDA Rates are not to be disclosed "in any
# form whatsoever" to any other person; 18.1, 18.2 no communicating or
# re-transmitting Licensed Material; 18.6 Internal Use only; 8(d) an automated
# system "must be fully verified and tested in the OANDA Demo environment prior
# to its use"; 8(e) it "must be closely supervised" and "should not be left to
# run unattended". Nothing there makes each live order a person's own action.
TERMS_URL = "https://legal.oanda.com/?code=api_licence_agreement&language=en"
TERMS_SUMMARY = ("OANDA's prices are licensed to you alone and stay on this "
                 "computer; OANDA's licence asks that a strategy be tested on "
                 "practice first and watched while it runs on a live account.")

# documented: https://developer.oanda.com/rest-live-v20/development-guide/
# "120 requests per second", "20 active streams", "no more than 2 new
# connections per second", applied per requesting IP address; best-practices:
# at most 100 a second on an open connection. The adapter keeps well under
# both, because the address is shared with whatever else the user runs.
REST_PER_SECOND = 50
CONNECT_SPACING = 0.55       # s between two new connections (2 a second)
REST_POOL = 4                # kept-alive REST connections at most
REQUEST_TIMEOUT = 7.0
IDLE_REUSE_WRITE = 60.0      # a connection idle longer is not used for an order
PAUSE_429 = 1.0              # first pause after a 429; doubles to PAUSE_429_MAX
PAUSE_429_MAX = 8.0
# documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ and
# transaction-ep: a heartbeat every 5 seconds on both streams.
STREAM_READ_TIMEOUT = 20.0   # four heartbeats missed: the stream is dead
STREAM_LINE_MAX = 4 * 1024 * 1024

HANDLER_BUDGET = 8.0         # s, from when the request's line was read
FLATTEN_BUDGET = 9.3
FLATTEN_FLOOR = 5.0          # s flatten_all works for however long it queued
READ_WORKERS = 4
CHART_WAITERS = 8
BYE_DRAIN = 4.0

# An order or close whose answer was lost is never sent again (see
# _replay_unknown): OANDA's record is read for it for this long after the
# loss, and only then is it final that OANDA did not carry it out. assumed,
# verify on demo: OANDA states no limit on how late a request may still be
# carried out. measured on a live account: OANDA's hosts sit behind
# Cloudflare, whose default wait for an origin's answer is 100 s (Cloudflare's
# own figure, not OANDA's, and not re-read).
UNKNOWN_WATCH = 120.0        # s an order of unknown fate is looked for
EMPTY_GAP_READS = 3          # identical empty answers before a gap is closed
HISTORY_DAYS = 7
HISTORY_MAX_FILLS = 500
CATALOG_REFRESH = 6 * 3600
CHANGES_POLL = 3.0           # s between reads of trailing stops
LEVELS_EVERY = 5.0           # s between checks of a netting position's levels
ACCOUNT_IDLE_POLL = 30.0
QUOTE_POLL_SEC = 1.0         # REST prices while the price stream is down
KEEP_RESULTS = 4000          # results and refusals remembered by client id

# documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ candles
# "count" default 500, maximum 5000; count is not to be given with both from
# and to. A chart of 5,000 bars is one request.
BARS_PAGE = 5000
BARS_MAX_COUNT = 20000
BARS_FIRST = 3.0             # s after which bars.history answers with what it has
BARS_BUDGET = 6.0
BARS_HEAD_TTL = 2.0          # s the newest end of a store is trusted
BARS_STORES = 24
BARS_IDLE = 900.0

# SPEC timeframes -> OANDA CandlestickGranularity (documented:
# https://developer.oanda.com/rest-live-v20/instrument-df/).
TIMEFRAMES = {"1m": ("M1", 60_000), "5m": ("M5", 300_000),
              "15m": ("M15", 900_000), "30m": ("M30", 1_800_000),
              "1h": ("H1", 3_600_000), "4h": ("H4", 14_400_000),
              "1d": ("D", 86_400_000)}

ISO_DAY = {"MONDAY": 1, "TUESDAY": 2, "WEDNESDAY": 3, "THURSDAY": 4,
           "FRIDAY": 5, "SATURDAY": 6, "SUNDAY": 7}


def _sessions_around(open_hm: str, close_hm: str, tz: str) -> list:
    """Sunday's open to Friday's close, shut each weekday from close to open
    (in `tz`): the shape OANDA's hours page gives forex and metals."""
    return [{"days": [7], "open": open_hm, "close": "24:00", "tz": tz},
            {"days": [1, 2, 3, 4], "open": "00:00", "close": close_hm,
             "tz": tz},
            {"days": [1, 2, 3, 4], "open": open_hm, "close": "24:00",
             "tz": tz},
            {"days": [5], "open": "00:00", "close": close_hm, "tz": tz}]


# documented: https://www.oanda.com/uk-en/trading/hours-of-operation/ forex
# "Sun-Fri 17:05 - 16:59" New York with "a six minute break between 16:59 -
# 17:05"; metals "Sun-Fri 18:05 - 16:59" New York; TRY pairs "Mon-Fri 08:00 -
# 17:00" Central Europe time. measured on a live account (tradeable
# false from 16:59:05 to 17:04:55 New York; TRY 07:00-16:00 London, the same
# instants). NZD pairs' extra 06:59-07:05 Auckland break is NOT in these
# windows (a break in another zone has no form here): OANDA's own refusal
# (MARKET_HALTED) stays the judge, as it does on holidays, which the API does
# not publish. Index, commodity and bond CFDs keep the hours of their own
# markets, one by one; they are sent with no calendar (SPEC 4: unknown).
SESSION_FX = _sessions_around("17:05", "16:59", "America/New_York")
SESSION_METAL = _sessions_around("18:05", "16:59", "America/New_York")
SESSION_TRY = [{"days": [1, 2, 3, 4, 5], "open": "08:00", "close": "17:00",
                "tz": "Europe/Berlin"}]

# OANDA's transaction types (documented:
# https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionType).
CREATE_TYPES = {"MARKET_ORDER": "MARKET", "LIMIT_ORDER": "LIMIT",
                "STOP_ORDER": "STOP",
                "MARKET_IF_TOUCHED_ORDER": "MARKET_IF_TOUCHED",
                "FIXED_PRICE_ORDER": "FIXED_PRICE"}
DEPENDENT_TYPES = {"TAKE_PROFIT_ORDER": "tp", "STOP_LOSS_ORDER": "sl",
                   "GUARANTEED_STOP_LOSS_ORDER": "sl",
                   "TRAILING_STOP_LOSS_ORDER": "trail"}
ENTRY_TYPES = ("LIMIT", "STOP", "MARKET_IF_TOUCHED")
SPEC_TYPE = {"MARKET": "market", "LIMIT": "limit", "STOP": "stop",
             "MARKET_IF_TOUCHED": "market_if_touched"}
TIF_OF = {"gtc": "GTC", "day": "GFD", "gtd": "GTD"}
EXPIRY_OF = {"GTC": "gtc", "GFD": "day", "GTD": "gtd"}
# documented: https://developer.oanda.com/rest-live-v20/transaction-df/
# OrderFillReason -> how a position.closed is explained (SPEC 5)
FROM_ORDER = {"LIMIT_ORDER": "limit", "STOP_ORDER": "stop",
              "MARKET_IF_TOUCHED_ORDER": "market_if_touched"}
D0 = Decimal(0)

# OANDA's refusals (a *_REJECT transaction's rejectReason, an ORDER_CANCEL's
# reason, or errorCode) in the SPEC's vocabulary. documented:
# https://developer.oanda.com/rest-live-v20/transaction-df/
# TransactionRejectReason and OrderCancelReason. A row matches the reason
# exactly, or, with a leading "*", anywhere inside it. First match wins. Each
# is (match, code, like, retryable); an x_oanda_ code nominates the closed-set
# code it should be treated as (SPEC 6.1) where one is right.
REASON_MAP = (
    ("*CLIENT_ORDER_ID_ALREADY_EXISTS", "duplicate_client_id", None, False),
    ("*CLIENT_TRADE_ID_ALREADY_EXISTS", "duplicate_client_id", None, False),
    ("*INSUFFICIENT_MARGIN", "insufficient_margin", None, False),
    ("INSUFFICIENT_FUNDS", "insufficient_funds", None, False),
    ("INSUFFICIENT_LIQUIDITY", "x_oanda_insufficient_liquidity", "requote",
     True),
    ("BOUNDS_VIOLATION", "requote", None, True),
    ("MARKET_HALTED", "market_closed", None, False),
    ("*_HALTED", "market_closed", None, False),
    ("INSTRUMENT_PRICE_UNKNOWN", "no_price", None, True),
    ("INSTRUMENT_UNKNOWN", "unknown_symbol", None, False),
    ("INSTRUMENT_NOT_TRADEABLE", "trade_restricted", None, False),
    ("*_REDUCE_ONLY", "trade_restricted", None, False),
    ("*LOCKED", "trade_restricted", None, False),
    ("ACCOUNT_NOT_ACTIVE", "trade_restricted", None, False),
    ("*FIFO", "x_oanda_fifo", "trade_restricted", False),
    ("HEDGING_GSLO_VIOLATION", "trade_restricted", None, False),
    ("OPEN_TRADES_ALLOWED_EXCEEDED", "x_oanda_limit_reached",
     "trade_restricted", False),
    ("PENDING_ORDERS_ALLOWED_EXCEEDED", "x_oanda_limit_reached",
     "trade_restricted", False),
    ("ACCOUNT_POSITION_VALUE_LIMIT_EXCEEDED", "x_oanda_limit_reached",
     "invalid_qty", False),
    ("POSITION_SIZE_EXCEEDED", "invalid_qty", None, False),
    # A stop or target that the FILL price would trigger at once: the levels
    # were on the right side of the prices the platform was sent (checked
    # before sending, see _check_levels_now), so the market moved past them
    # between the quote and the fill. SPEC 6 `requote`: retryable.
    ("TAKE_PROFIT_ON_FILL_LOSS", "requote", None, True),
    ("STOP_LOSS_ON_FILL_LOSS", "requote", None, True),
    ("LOSING_TAKE_PROFIT", "requote", None, True),
    ("*PRICE_DISTANCE", "invalid_stops", None, False),
    ("*TAKE_PROFIT", "invalid_stops", None, False),
    ("*STOP_LOSS", "invalid_stops", None, False),
    ("*UNITS", "invalid_qty", None, False),
    ("*TIME_IN_FORCE", "invalid_expiry", None, False),
    ("*GTD", "invalid_expiry", None, False),
    ("ORDER_DOESNT_EXIST", "not_found", None, False),
    ("TRADE_DOESNT_EXIST", "not_found", None, False),
    ("CLOSEOUT_POSITION_DOESNT_EXIST", "not_found", None, False),
    ("*PRICE_PRECISION_EXCEEDED", "x_oanda_price_precision", None, False),
    ("PRICE_INVALID", "x_oanda_price_invalid", None, False),
    ("PRICE_MISSING", "x_oanda_price_invalid", None, False),
    ("INTERNAL_SERVER_ERROR", "internal", None, True),
)


def map_reason(reason: str):
    """(code, like, retryable) for one of OANDA's reason names."""
    r = str(reason or "").upper()
    for match, code, like, retryable in REASON_MAP:
        if (match[1:] in r) if match.startswith("*") else (r == match):
            return code, like, retryable
    tail = re.sub(r"[^a-z0-9_]+", "_", r.lower()).strip("_") or "refused"
    return "x_oanda_" + tail, None, False


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[oanda] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# The state file, shared by every process of one profile.
# ---------------------------------------------------------------------------

class _FileLock:
    """An exclusive lock on `path` across processes (POSIX flock, Windows
    msvcrt): the state file is read, changed and written by one process at a
    time. Held for a few milliseconds. Where neither exists the work goes on
    without it (said once)."""

    _said = False

    def __init__(self, path: str, wait: float = 5.0):
        self.path, self.wait, self.fh = path, wait, None

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            self.fh = os.fdopen(fd, "r+b")
            if fcntl is not None:
                end = time.monotonic() + self.wait
                while True:
                    try:
                        fcntl.flock(self.fh.fileno(),
                                    fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > end:
                            raise
                        time.sleep(0.01)
            elif msvcrt is not None:
                self.fh.seek(0)
                # LK_LOCK tries for about 10 s, then raises
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
            elif not _FileLock._said:
                _FileLock._said = True
                log("no file lock on this system: the state file is written "
                    "without one")
        except OSError as e:
            log("the state file's lock could not be taken (%s); written "
                "without it" % type(e).__name__)
        return self

    def __exit__(self, *exc):
        fh, self.fh = self.fh, None
        if fh is None:
            return False
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        return False


def _norm(obj):
    """A JSON value as it is on disk (Decimals as text, tuples as lists), so
    that what is in memory and what was read can be compared."""
    return json.loads(json.dumps(obj, default=str))


_MISSING = object()


def _patch(target: dict, base: dict, mine: dict):
    """Onto `target` (the file as it is on disk now), what THIS process
    changed since `base` (what it last read or wrote): its new and changed
    keys set, the keys it removed removed. Keys another process wrote and
    this one never touched stay as they are on disk."""
    for k, v in mine.items():
        if base.get(k, _MISSING) != v:
            target[k] = v
    for k in base:
        if k not in mine:
            target.pop(k, None)


# ---------------------------------------------------------------------------
# Numbers and times, converted once, at the boundary.
# ---------------------------------------------------------------------------

def _dec(v) -> Decimal | None:
    """A broker number (OANDA sends strings) as an exact Decimal, or None.
    Floats go through repr so 0.1 stays 0.1."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        d = Decimal(repr(v) if isinstance(v, float) else str(v))
    except (decimal.InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _d0(v) -> Decimal:
    d = _dec(v)
    return D0 if d is None else d


def _fl(v) -> float:
    d = _dec(v)
    return 0.0 if d is None else float(d)


def _fmt(d: Decimal, places: int) -> str:
    """Fixed-point text with exactly `places` decimals: never an exponent."""
    q = Decimal(1).scaleb(-places) if places > 0 else Decimal(1)
    return format(d.quantize(q, rounding=decimal.ROUND_HALF_EVEN), "f")


def step_ok(qty: float, step: float) -> bool:
    """Is `qty` a whole multiple of `step`, as far as a float can say?
    (the reference adapter's test: float noise passes, `min + step/3` not)"""
    if not (step > 0) or not (qty > 0) or qty != qty or qty == math.inf:
        return False
    nearest = round(qty / step) * step
    return abs(qty - nearest) <= max(4 * math.ulp(qty), step * 1e-6)


def snap(value: float, step: Decimal) -> Decimal:
    """`value` moved onto the `step` grid exactly, for sending."""
    return (_d0(value) / step).to_integral_value(
        decimal.ROUND_HALF_EVEN) * step


_RFC = re.compile(r"^(\d{4})-(\d\d)-(\d\d)(?:[Tt ](\d\d):(\d\d)(?::(\d\d)"
                  r"(?:\.(\d+))?)?)?\s*(Z|z|[+-]\d\d:?\d\d)?$")


def to_ms(v) -> int | None:
    """An OANDA DateTime as UTC epoch milliseconds, or None. documented:
    https://developer.oanda.com/rest-live-v20/primitives-df/ DateTime: UNIX
    "12345678.000000123" (what this adapter asks for) or RFC 3339."""
    if v is None:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        whole, _, frac = s.partition(".")
        return int(whole) * 1000 + int((frac + "000")[:3])
    m = _RFC.match(s)
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, zone = m.groups()
    import calendar
    secs = calendar.timegm((int(y), int(mo), int(d), int(hh or 0),
                            int(mi or 0), int(ss or 0)))
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        secs -= sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)
    return secs * 1000 + int(((frac or "") + "000")[:3])


def unix_s(ms: int) -> str:
    """UTC epoch ms in OANDA's UNIX DateTime form ("seconds.nanoseconds")."""
    s, rem = divmod(int(ms), 1000)
    return "%d.%09d" % (s, rem * 1_000_000)


def _nth_sunday(year: int, month: int, n: int) -> int:
    """Epoch seconds of 00:00 UTC on the n-th Sunday of a month."""
    import calendar
    first = calendar.timegm((year, month, 1, 0, 0, 0))
    wday = time.gmtime(first).tm_wday            # Monday 0 ... Sunday 6
    return first + ((6 - wday) % 7 + 7 * (n - 1)) * 86400


def new_york_offset(utc_s: float) -> int:
    """New York's offset from UTC in seconds at an instant: the US rule since
    2007, summer from 02:00 local on the second Sunday in March to 02:00 local
    on the first Sunday in November. Written out so no time zone database is
    needed (a stock Windows Python has none)."""
    y = time.gmtime(utc_s).tm_year
    start = _nth_sunday(y, 3, 2) + 7 * 3600      # 02:00 EST
    end = _nth_sunday(y, 11, 1) + 6 * 3600       # 02:00 EDT
    return -4 * 3600 if start <= utc_s < end else -5 * 3600


def next_new_york_five(ms: int) -> int:
    """The next 17:00 New York after `ms` (epoch ms): when OANDA cancels a GFD
    order. documented: https://developer.oanda.com/rest-live-v20/order-df/
    TimeInForce GFD "will be cancelled at 5pm New York time"."""
    s = ms / 1000.0
    for extra in (0, 1, 2):
        off = new_york_offset(s + extra * 86400)
        local = s + off
        day = math.floor(local / 86400) + extra
        cand = day * 86400 + 17 * 3600 - off
        if cand > s:
            return int(cand * 1000)
    return int((s + 86400) * 1000)


def _host(url: str) -> str:
    # "" for a URL urlparse cannot read (a newer Python raises on brackets
    # around anything but an IPv6 address): such a URL was the end of the
    # adapter, before it could answer hello.
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Practice or live: decided cold, from configuration alone.
# ---------------------------------------------------------------------------

def _credential_fault(value: str) -> str:
    """Why `value` cannot be an OANDA token, in words that never contain it.
    Checked once, here: a pasted token with a line break in it would otherwise
    reach http.client, whose error message quotes the header value."""
    if any(c.isspace() for c in value):
        return ("the API token has a space or a line break inside it; paste "
                "it again as one unbroken line")
    if not all(33 <= ord(c) <= 126 for c in value):
        return ("the API token has a character in it that no OANDA token "
                "has; paste it again")
    return ""


# A plain endpoint's host and port, as written: a name (labels of letters,
# digits, "-" and "_", a trailing dot allowed), an IPv4 address, or ONE IPv6
# address in brackets; then a port of digits, or none.
_NETLOC = re.compile(r"(?P<name>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?)"
                     r"|\[(?P<v6>[0-9A-Fa-f:.]+)\](?::(?P<v6port>[0-9]+))?"
                     r"|(?P<hostport>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?)"
                     r":(?P<port>[0-9]+)")
_SAME_HOST = "so the host it would reach is not the one it names; give the " \
             "host alone"


def _endpoint_shape(url: str):
    """Why `url` is not a plain endpoint (a scheme, a host, an optional port,
    then its path): (what it has, what to do), or None when it is plain.

    The pool connects to the host urlsplit names, the host the mode is
    judged from, so a user part cannot take a request elsewhere (it
    can in an adapter whose HTTP client connects to all
    that stands before the path). An OANDA endpoint has none of what is
    refused here, and one written with it is not what it seems, so it is
    refused all the same, by this one rule, read off the URL as it is
    written and held to what urlsplit gives: nothing depends on the
    interpreter's own parser (Python 3.10.0-3.10.15 and 3.11.0-3.11.3 read
    http://<name>[::1]:9 as '::1'; a newer one raises on it), and tabs and
    line breaks, which urlsplit drops, are not taken either."""
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return ("has a space, a tab or a line break in it",
                "write it again on one line")
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme:
        return ("is not a URL this adapter can read",
                "give it as https://host")
    netloc = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in netloc:
        return "has a user name before its host", _SAME_HOST
    if "\\" in netloc:
        return "has a backslash before its path", _SAME_HOST
    if "%" in netloc:
        return "has a %-escape in its host", _SAME_HOST
    m = _NETLOC.fullmatch(netloc)
    v6 = m.group("v6") if m else None
    if v6 is not None:
        try:
            ipaddress.IPv6Address(v6)
        except ValueError:
            m = None
    if m is None:
        if "[" in netloc or "]" in netloc:
            return ("has brackets in its host around something that is not "
                    "one IPv6 address", _SAME_HOST)
        if ":" in netloc:
            return ("has a port that is not a number from 1 to 65535",
                    "give a port from 1 to 65535, or none")
        return ("has a host that is not a plain name or address",
                "give a host name or an address")
    host = (m.group("name") or v6 or m.group("hostport")).lower()
    port = m.group("port") or m.group("v6port")
    if port is not None and not 1 <= int(port) <= 65535:
        return ("has a port that is not a number from 1 to 65535",
                "give a port from 1 to 65535, or none")
    try:
        u = urllib.parse.urlsplit(url)
        same = ((u.hostname or "") == host
                and u.port == (int(port) if port is not None else None))
    except ValueError:
        same = False
    if not same:
        return "is read as another host by another reader", _SAME_HOST
    return None


def _location_fault(location: str) -> str:
    """Why a redirect's Location, as written, is not a plain URL ("it ..."),
    or "" when it is. Judged before it is joined to the endpoint: urljoin
    drops tabs and line breaks, keeps a backslash as a letter of the path
    where a browser reads it as "/", and takes "https:///x", "///x" and
    "/\\x" to the endpoint's own host where a browser takes them to host
    "x"; http.client cannot write a letter that is not ASCII. So an address
    that two readers read two ways is not followed, by either."""
    if any(ch.isspace() for ch in location):
        return "has a space, a tab or a line break in it"
    if not location.isprintable():
        return "has a character in it that cannot be printed"
    if not location.isascii():
        return "has a character in it that is not ASCII"
    if "\\" in location:
        return "has a backslash in it"
    m = re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", location)
    rest = location[m.end():] if m else location
    if rest.startswith("//"):
        authority = re.split(r"[/?#]", rest[2:], maxsplit=1)[0]
        # The host is the authority without any user part (before an "@")
        # or port (after a ":"). An authority of only a port (":9") or only
        # a user part ("@") has none.
        hostport = authority.rsplit("@", 1)[-1]
        if hostport.startswith("["):
            host = hostport[1:hostport.find("]")] if "]" in hostport \
                else hostport[1:]
        else:
            host = hostport.split(":", 1)[0]
        if not host:
            return "has no host after its //"
    return ""


def _endpoint_fault(name: str, url: str) -> str:
    shape = _endpoint_shape(url)
    if shape:
        return "%s %s, %s" % (name, shape[0], shape[1])
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "%s must be an https:// URL" % name
    if u.scheme == "http" and not _loopback(u.hostname.lower()):
        # The token would cross the network in clear.
        return ("%s must be https (plain http is only accepted for this "
                "machine, a test stand-in)" % name)
    return ""


def resolve_environment(argv, environ) -> dict:
    """Which OANDA this process talks to, with no network (SPEC 3, 8.2).

    PRACTICE unless `--live` or OANDA_ENV=live (a flag wins over the variable;
    both flags together are live). On practice, OANDA_REST_URL and
    OANDA_STREAM_URL are taken only when they name OANDA's practice host or
    this machine (a stand-in): anything else is ignored, and `why` says so.
    `mode` is answered by the hosts actually talked to: "paper" only when the
    REST host is api-fxpractice.oanda.com and the stream host
    stream-fxpractice.oanda.com, each exactly, or loopback.
    """
    token = (environ.get("BRUE_CRED_TOKEN") or "").strip()
    argv = list(argv or [])
    paper_flag, live_flag = "--paper" in argv, "--live" in argv
    named = (environ.get("OANDA_ENV") or "").strip().lower()
    if paper_flag and live_flag:
        env, why = "live", "both --paper and --live given; live assumed"
    elif live_flag:
        env, why = "live", "flag --live"
    elif paper_flag:
        env, why = "practice", "flag --paper"
    elif named == "live":
        env, why = "live", "OANDA_ENV=live"
    elif named in ("practice", "paper", "demo"):
        env, why = "practice", "OANDA_ENV=%s" % named
    elif named:
        env, why = "practice", ("OANDA_ENV is neither practice nor live; "
                                "practice is used (live must be asked for)")
    else:
        env, why = "practice", "live was not asked for"
    rest_set = (environ.get("OANDA_REST_URL") or "").strip()
    stream_set = (environ.get("OANDA_STREAM_URL") or "").strip()
    ignored = []
    if env == "practice":
        # DEMO IS THE DEFAULT, and --paper pins it: practice talks to OANDA's
        # practice hosts, or to a stand-in on this machine, and to nothing
        # else, whatever the environment it inherited says (a platform's own
        # environment may carry another tool's OANDA_REST_URL).
        for name, value, host in (("OANDA_REST_URL", rest_set,
                                   PRACTICE_REST_HOST),
                                  ("OANDA_STREAM_URL", stream_set,
                                   PRACTICE_STREAM_HOST)):
            h = _host(value) if value else ""
            if value and h != host and not _loopback(h):
                ignored.append(name)
        if "OANDA_REST_URL" in ignored:
            rest_set = ""
        if "OANDA_STREAM_URL" in ignored:
            stream_set = ""
        if ignored:
            why += ("; %s ignored: practice reaches only OANDA's practice "
                    "hosts or this machine" % " and ".join(ignored))
    home_rest = PRACTICE_REST_URL if env == "practice" else LIVE_REST_URL
    home_stream = (PRACTICE_STREAM_URL if env == "practice"
                   else LIVE_STREAM_URL)
    rest = (rest_set or home_rest).rstrip("/")
    stream = (stream_set or home_stream).rstrip("/")
    # An endpoint that cannot be used is not used: the process keeps its
    # environment's own, every call is refused with the fault
    # (x_oanda_endpoint), and the mode is that of the endpoints kept. Kept
    # beside its fault, an endpoint with a user part naming this machine
    # made a --live process say paper; one urlsplit cannot read ended it.
    rest_fault = _endpoint_fault("OANDA_REST_URL", rest)
    stream_fault = _endpoint_fault("OANDA_STREAM_URL", stream)
    if rest_fault:
        rest = home_rest
    if stream_fault:
        stream = home_stream
    rh, sh = _host(rest), _host(stream)
    paper = ((rh == PRACTICE_REST_HOST or _loopback(rh))
             and (sh == PRACTICE_STREAM_HOST or _loopback(sh)))
    fault = "; ".join(f for f in (rest_fault, stream_fault) if f)
    return {"env": env, "why": why, "mode": "paper" if paper else "live",
            "token": token,
            "cred_fault": _credential_fault(token) if token else "",
            "fault": fault, "rest_url": rest, "stream_url": stream,
            "rest_host": rh, "ignored": ignored}


# ---------------------------------------------------------------------------
# OANDA's refusals.
# ---------------------------------------------------------------------------

class OandaError(Exception):
    """OANDA answered with an error, or could not be reached (status 0).

    Carries OANDA's own words and nothing of the request: a handler's
    exception reaches the platform, so it must never be able to hold the
    token. `sent` is False when the request certainly never reached OANDA.
    `cause` is set when a request went out and no usable answer came back:
    what ended the wait (an exception's name, or "" when nothing more is
    known), or, with `answered`, what was wrong with the answer that came.
    """

    def __init__(self, status: int, message: str, reason: str = "",
                 body=None, code: str | None = None, sent: bool = True,
                 cause: str | None = None, answered: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason or ""
        self.body = body if isinstance(body, dict) else {}
        self.code = code
        self.sent = sent
        self.cause = cause
        self.answered = answered


def _one_line(text: str, limit: int) -> str:
    """`text` as one line: every control character and run of white space a
    single space, cut at `limit` (with "..." when cut)."""
    text = re.sub(r"\s+", " ", "".join(
        " " if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch
        for ch in str(text))).strip()
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


# A tag, so it can be taken out whole even when its own text holds a '>':
# an HTML comment (which may hold any '>'), or a tag whose quoted attribute
# value holds one. The alternatives inside the tag are told apart by their
# first character, so the match is linear.
_TAG = re.compile(r"<!--.*?-->|<(?:[^>'\"]|'[^']*'|\"[^\"]*\")*>?", re.S)


# The code points that print as nothing: Unicode's format characters (Cf)
# and the other Default_Ignorable_Code_Point characters (the combining
# grapheme joiner, the Hangul fillers, the Mongolian and variation
# selectors, the tag characters). Category Cf alone left the rest of them
# splitting a secret unseen.
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C),
              (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
              (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F),
              (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
              (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))


def _ignorable(ch: str) -> bool:
    """Whether `ch` prints as nothing (a format character, or another
    Default_Ignorable_Code_Point character)."""
    o = ord(ch)
    return (unicodedata.category(ch) == "Cf"
            or any(lo <= o <= hi for lo, hi in _IGNORABLE))


def _visible(text) -> str:
    """`text` without the characters that print as nothing (_ignorable)."""
    return "".join(ch for ch in str(text) if not _ignorable(ch))


def _is_gap(ch: str) -> bool:
    """Whether `ch` may stand between a secret's characters unseen, or be
    read past: white space, a control character _one_line turns into a
    space (NUL, DEL, the C0 and C1 controls), or a character that prints as
    nothing (_ignorable)."""
    o = ord(ch)
    return ch.isspace() or o < 0x20 or 0x7F <= o < 0xA0 or _ignorable(ch)


def _gap_class() -> str:
    """The character class, as regex source, of every gap character
    (_is_gap): white space, the C0/C1 controls and DEL, and the code points
    that print as nothing (_IGNORABLE)."""
    parts = [r"\s\x00-\x1f\x7f-\x9f"]
    for lo, hi in _IGNORABLE:
        parts.append(re.escape(chr(lo)) if lo == hi else
                     re.escape(chr(lo)) + "-" + re.escape(chr(hi)))
    return "[" + "".join(parts) + "]"


_GAP = _gap_class()


def _hide(text, secrets) -> str:
    """`text` with each secret of 4 characters or more taken out
    ("[hidden]"): as written, and also where it is split into readable
    pieces, or read past, by a gap character -- white space, a control
    character, or a character that prints as nothing (_is_gap). The split
    pattern joins the secret's non-gap characters by a run of gap characters
    (_GAP), required where the secret itself holds one; two runs never sit
    side by side and a gap character of the secret is never its own run
    beside another, so the match is linear, not quadratic (a quadratic
    match took 6 s for a password with a space against a 64 KiB page)."""
    text = str(text)
    for value in secrets:
        if not value or len(value) < 4:
            continue
        if value in text:
            text = text.replace(value, "[hidden]")
        pat, started, need = [], False, False
        for ch in value:
            if _is_gap(ch):
                need = True
                continue
            if started:
                pat.append(_GAP + ("+" if need else "*"))
            pat.append(re.escape(ch))
            started, need = True, False
        if started:
            text = re.sub("".join(pat), "[hidden]", text)
    return text


def _page_words(text: str, status: int, limit: int = 80,
                scrub=lambda t: t) -> str:
    """What an answer that is not JSON says, in one short line: a gateway's
    HTML page by its title ("Bad Gateway", the status number left out), else
    its text without tags. Raw, such a page would go into the message line
    breaks and all.

    The title's text is taken; the page is unescaped (an escaped tag
    revealed); then `scrub` takes the token out of the unescaped text, so a
    token that itself holds '<' or '>' is hidden whole before the tags are
    taken out, or the stray '<'/'>' mapped, can cut it; then the tags are
    taken out whole, each one even if its own text holds a '>' (_TAG), and
    what prints as nothing is dropped (_visible); then `scrub` again (a
    token a tag split); then any stray '<'/'>' becomes a space and the words
    are cut. The scrub comes before the cut: cut first, a token written in
    HTML escapes keeps its first characters. Split by a tag, a soft hyphen
    or a zero-width character, a token would reach the words in pieces."""
    m = re.search(r"<title[^>]*>(.*?)</title\s*>", text, re.I | re.S)
    join = "" if m else " "
    text = scrub(html.unescape(m.group(1) if m else text))
    text = _visible(_TAG.sub(join, text))
    text = _one_line(scrub(text).replace("<", " ").replace(">", " "), limit)
    return re.sub(r"^%d\s+" % status, "", text) if status else text


def _clause(e: OandaError) -> str:
    """OANDA's refusal as a clause of a longer line (flatten_all's), so
    that the line says it once, in one sentence: its words without a full
    stop at the end, or its status. With the full stop kept, flatten_all
    would say "...still wanted.. Ask again." A message that was cut keeps
    its "..." (the sign of the cut), rather than ending in a cut word with
    no sign of it."""
    msg = str(e.message or "")
    if msg.endswith("..."):
        return msg
    return msg.rstrip(" .") or ("HTTP %d" % e.status)


def unanswered(e: OandaError) -> str:
    """Why a request's fate is not known, in a few words that follow "Not
    known yet whether OANDA carried out the cancel: " (_fate_unknown): "it
    did not answer (TimeoutError)", "its answer could not be read (not JSON,
    28 bytes)", "it answered HTTP 502 (Bad Gateway)". Each word once (not
    "OANDA did not answer (OANDA did not answer (TimeoutError))"); a
    server's error keeps its own words, in one line."""
    if e.status == 0 and e.answered:
        return ("its answer could not be read (%s)" % e.cause if e.cause
                else "its answer could not be read")
    if e.status == 0:
        return ("it did not answer (%s)" % e.cause if e.cause
                else "it did not answer")
    words = _one_line(e.message or "", 80)
    return "it answered HTTP %d%s" % (e.status,
                                      " (%s)" % words if words else "")


def reject_reason(body) -> str:
    """The reason OANDA gives for a refusal: a *RejectTransaction's
    rejectReason, else errorCode (documented: order-ep, trade-ep, position-ep:
    400/404 bodies carry the reject transaction, errorCode and
    errorMessage)."""
    if not isinstance(body, dict):
        return ""
    for k, v in body.items():
        if (isinstance(k, str) and k.endswith("RejectTransaction")
                and isinstance(v, dict) and v.get("rejectReason")):
            return str(v["rejectReason"])
    code = body.get("errorCode")
    return str(code) if isinstance(code, str) and code else ""


def txs_of(body) -> list:
    """The transactions in an answer (orderCreateTransaction,
    orderFillTransaction, longOrderFillTransaction, ...), in id order."""
    out = []
    for v in (body or {}).values() if isinstance(body, dict) else ():
        if (isinstance(v, dict) and str(v.get("id") or "").isdigit()
                and v.get("type")):
            out.append(v)
    out.sort(key=lambda t: int(t["id"]))
    return out


def to_adapter_error(e: OandaError, env: str = "", host: str = "",
                     account: str = "") -> AdapterError:
    """One OANDA refusal as the error a platform can act on. OANDA's own words
    are kept in the message; never anything of the request."""
    msg = e.message or ""
    said = "OANDA: " + msg if msg else "OANDA answered HTTP %d" % e.status
    if e.reason and e.reason not in said:
        said += " (%s)" % e.reason
    if e.status == 0:
        if e.code == "endpoint":
            return AdapterError("x_oanda_endpoint", msg)
        if e.code == "flatten":
            return AdapterError(
                "x_oanda_flattening", "flatten_all was asked for while this "
                "call was being answered, and it was stopped before it sent "
                "anything more; flatten_all is cancelling resting orders and "
                "closing every position.", retryable=True, like="frozen")
        if e.code == "deadline":
            return AdapterError(
                "internal", "OANDA was too slow: the %d s this call has ran "
                "out before it was finished. Ask again; the same client_id "
                "is safe." % HANDLER_BUDGET, retryable=True)
        if e.code == "budget":
            return AdapterError("rate_limited", "not sent, by this adapter's "
                                "own limit: " + msg, retryable=True)
        if e.code == "unsendable":
            # A request http.client could not write (OandaClient._write):
            # nothing was sent, and asking again meets the same.
            return AdapterError("internal", msg)
        if e.answered:
            # OANDA answered, and what it answered could not be read (a 2xx
            # that is not JSON): never "could not be reached" (as for the
            # writes, see unanswered).
            return AdapterError("internal", "OANDA answered, but %s"
                                % unanswered(e), retryable=True)
        return AdapterError("internal", "OANDA could not be reached: " + msg,
                            retryable=True)
    if e.code == "redirect":
        # Not followed (OandaClient._follow): its own words say which rule
        # it met and name the target's host. Known: a request that is not a
        # GET answered 307 or 308 was not carried out, and an order refused
        # so is final for its client_id; asking a read again meets the same
        # answer until OANDA stops giving it.
        return AdapterError("x_oanda_redirected", msg, like="internal")
    low = msg.lower()
    if e.status in (401, 403) and "forbidden" in low:
        # documented: https://developer.oanda.com/rest-live-v20/
        # troubleshooting-errors/ "Invalid AccountID": "The provided request
        # was forbidden." (filed under 401; 403 is also read so).
        return AdapterError(
            "x_oanda_account_not_reachable", "OANDA refused account %s for "
            "this token (%s). A token reaches the sub-accounts its login had "
            "when it was made; one added later needs a new token from OANDA."
            % (account or "?", said), like="unauthorized")
    if e.status == 401:
        return AdapterError(
            "unauthorized", "OANDA refused this API token on its %s host "
            "(%s). A token made on a practice (demo) login works only on "
            "practice, and one made on a live login only on live: check that "
            "it is the token OANDA showed for this login and that it has not "
            "been revoked. OANDA does not offer its v20 API to clients of "
            "OANDA TMS Brokers (EU residents) or OANDA Global Markets."
            % (env or "?", host or "?"))
    if e.status == 403:
        # documented: troubleshooting-errors, 403 "Account Not Tradable".
        return AdapterError("x_oanda_forbidden", said + ". OANDA says this "
                            "account may not do this.",
                            like="trade_restricted")
    if e.status == 429:
        return AdapterError("rate_limited", said, retryable=True)
    if e.reason:
        code, like, retryable = map_reason(e.reason)
        if code == "not_found" and e.status == 404:
            return AdapterError("not_found", said)
        return AdapterError(code, said, retryable=retryable, like=like)
    if e.status >= 500:
        return AdapterError("internal", said, retryable=True)
    if e.status == 404:
        return AdapterError("not_found", said)
    if e.status == 400 and "'instrument" in low:
        return AdapterError("unknown_symbol", said)
    return AdapterError("x_oanda_%d" % e.status, said)


# ---------------------------------------------------------------------------
# REST and streams over http.client: few connections, kept alive, paced.
# ---------------------------------------------------------------------------

class _Gate:
    """Every NEW connection this process opens, REST or stream, waits its
    turn: documented, "no more than 2 new connections per second", counted
    per address, so with every process there. A process started right after
    another of its profile (a platform starts this adapter again for its
    `reconnect`, SPEC 6, a fraction of a second after the last one opened its
    connection) begins after the last one's turn: remember() keeps each new
    connection's time in the profile's state folder and starts from it."""

    def __init__(self, spacing: float = CONNECT_SPACING):
        self.spacing = spacing
        self._next = 0.0
        self._lock = threading.Lock()
        self._path = ""

    def remember(self, path: str):
        """Keep the time of each new connection in `path`, and wait out the
        turn of the last one kept there. A time that cannot be read, or one
        ahead of the clock, costs at most one turn."""
        self._path = path
        try:
            with open(path, encoding="utf-8") as fh:
                last = float(fh.read().strip())
        except (OSError, ValueError):
            return
        wait = min(self.spacing, max(0.0, last + self.spacing - time.time()))
        with self._lock:
            self._next = max(self._next, time.monotonic() + wait)

    def take(self, until: float | None = None):
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            if until is not None and at > until - 0.2:
                raise OandaError(0, "no time left to open a connection to "
                                 "OANDA", code="budget", sent=False)
            self._next = at + self.spacing
            path, wall = self._path, time.time() + (at - now)
        if path:
            # Written whole and renamed: a reader never sees half of it.
            tmp = "%s.%d.tmp" % (path, os.getpid())
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("%.3f\n" % wall)
                os.replace(tmp, path)
            except OSError:
                pass
        if at > now:
            time.sleep(at - now)


class _Pace:
    """Requests a second, well under OANDA's limit; a 429 pauses REST for a
    while (no Retry-After is documented), longer each time it repeats."""

    def __init__(self, per_second: int = REST_PER_SECOND):
        self.per = per_second
        self._times: collections.deque = collections.deque()
        self._blocked_until = 0.0
        self._pause = PAUSE_429
        self._lock = threading.Lock()

    def take(self, until: float | None = None, urgent: bool = False):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 1.0:
                    self._times.popleft()
                wait = 0.0
                if now < self._blocked_until and not urgent:
                    wait = self._blocked_until - now
                elif len(self._times) >= self.per:
                    wait = 1.0 - (now - self._times[0]) + 0.001
                if wait <= 0:
                    self._times.append(now)
                    return
            if until is not None and time.monotonic() + wait > until - 0.2:
                raise OandaError(0, "OANDA's request rate is used up for "
                                 "this moment", code="budget", sent=False)
            time.sleep(min(wait, 0.25))

    def blocked(self):
        with self._lock:
            self._blocked_until = time.monotonic() + self._pause
            self._pause = min(self._pause * 2, PAUSE_429_MAX)

    def eased(self):
        with self._lock:
            self._pause = PAUSE_429

    def blocked_for(self) -> float:
        with self._lock:
            return max(0.0, self._blocked_until - time.monotonic())


class _Pool:
    """Kept-alive connections to one host (documented, best-practices: keep
    connections alive; a new one is the expensive, limited thing)."""

    def __init__(self, base_url: str, gate: _Gate, ctx):
        u = urllib.parse.urlsplit(base_url)
        self.scheme = u.scheme
        self.host = u.hostname or ""
        # The port always given: with none, http.client splits the host at
        # its last colon, and http://[::1] becomes host ':' and port 1.
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.prefix = u.path.rstrip("/")
        self.gate = gate
        self.ctx = ctx
        self._idle: list = []
        self._lock = threading.Lock()

    def connect(self, timeout: float, until: float | None):
        self.gate.take(until)
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(self.host, self.port,
                                               timeout=timeout,
                                               context=self.ctx)
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                              timeout=timeout)
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise OandaError(0, "the connection to OANDA could not be made "
                             "(%s)" % type(e).__name__, code="connect",
                             sent=False) from None
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        return conn

    @staticmethod
    def fresh(conn) -> bool:
        """Is a kept-alive connection still open at the other end? One that
        is readable while idle has been closed by the server (or holds junk):
        never sent another request."""
        sock = getattr(conn, "sock", None)
        if sock is None:
            return False
        try:
            ready, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return False
        return not ready

    def take(self, timeout: float, until: float | None,
             max_idle: float | None = None):
        with self._lock:
            while self._idle:
                conn, at = self._idle.pop()
                if ((max_idle is not None and time.monotonic() - at > max_idle)
                        or not self.fresh(conn)):
                    conn.close()
                    continue
                return conn, True
        return self.connect(timeout, until), False

    def give(self, conn):
        with self._lock:
            if len(self._idle) < REST_POOL:
                self._idle.append((conn, time.monotonic()))
                return
        conn.close()

    def close_all(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _ in idle:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class OandaClient:
    """OANDA over HTTPS: one deadline per handler, typed errors, no secret in
    any message."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.gate = _Gate()
        self.pace = _Pace()
        self._ctx = ssl.create_default_context()
        self.rest = _Pool(cfg["rest_url"], self.gate, self._ctx)
        self.stream = _Pool(cfg["stream_url"], self.gate, self._ctx)
        self._tl = threading.local()
        # Set while flatten_all is asked for or running: a call that changes
        # something makes no further request (see set_request, `yields`).
        self.stand_down = threading.Event()

    # -- one deadline per handler ----------------------------------------
    def set_deadline(self, seconds: float | None):
        self._tl.deadline = (None if seconds is None
                             else time.monotonic() + seconds)
        self._tl.read_at, self._tl.yields = None, False

    def set_request(self, read_at: float, budget: float, floor: float = 0.0,
                    yields: bool = False):
        """The request this thread answers was read at `read_at` and has
        `budget` seconds from then; `floor`: seconds from now whatever it
        waited (flatten_all). `yields`: it stops at its next request if
        flatten_all is asked for meanwhile (SPEC 8.1)."""
        self._tl.read_at, self._tl.yields = read_at, yields
        self._tl.deadline = max(read_at + budget, time.monotonic() + floor)

    def read_at(self) -> float | None:
        return getattr(self._tl, "read_at", None)

    def time_left(self) -> float | None:
        d = getattr(self._tl, "deadline", None)
        return None if d is None else d - time.monotonic()

    def scrub(self, text: str) -> str:
        """`text` with the token taken out, whatever put it there, also
        where white space or a character that prints as nothing splits it
        (_hide)."""
        return _hide(text, (self.cfg.get("token") or "",))

    def _headers(self, body: bool) -> dict:
        h = {"Authorization": "Bearer " + self.cfg["token"],
             # documented: https://developer.oanda.com/rest-live-v20/
             # primitives-df/ AcceptDatetimeFormat; it sets the format of the
             # DateTime fields of the request and of the answer.
             "Accept-Datetime-Format": "UNIX",
             "Accept": "application/json", "User-Agent": USER_AGENT}
        if body:
            h["Content-Type"] = "application/json"
        return h

    def _error(self, status: int, raw: bytes) -> OandaError:
        # Every message one line, the token taken out before anything is
        # cut (a cut token would not be found).
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            # Not JSON: a gateway's page, by its title, in one short line.
            body = {"errorMessage": _page_words(
                raw[:65536].decode("utf-8", "replace"), status,
                scrub=self.scrub)}
        if not isinstance(body, dict):
            body = {"errorMessage": self.scrub(str(body))}
        # The reason (rejectReason, else errorCode) is OANDA's own text as
        # well, and to_adapter_error adds it to the message: one line, the
        # token taken out, like the message. One of OANDA's documented
        # reasons is not changed by it.
        return OandaError(status, _one_line(self.scrub(str(
            body.get("errorMessage") or "")), 200),
            reason=_one_line(self.scrub(reject_reason(body)), 80), body=body)

    def call(self, method: str, path: str, query: dict | None = None,
             body=None, kind: str = "read", timeout: float | None = None):
        """Parsed JSON (a dict). Raises OandaError. `kind`: read,
        background (a job of this adapter's), write (an order, a close, a
        cancel, an amend), flatten (never held back by a 429 pause). A
        request that is not a GET is sent once by this method, whatever its
        kind -- except that a 429 (which OANDA documents as an
        excess-requests refusal it did not carry out) is paused and sent
        once more. A job may still make a new attempt of its own."""
        if self.cfg.get("fault"):
            # An endpoint that cannot be used is never reached, nor the one
            # kept in its place (resolve_environment), whatever path asks:
            # the session's opening says why first (_need_token).
            raise OandaError(0, self.cfg["fault"], code="endpoint",
                             sent=False)
        if not self.cfg.get("token"):
            raise OandaError(401, "no API token", sent=False)
        if self.cfg.get("cred_fault"):
            raise OandaError(401, self.cfg["cred_fault"], sent=False)
        # Decided by the method alone. A GET changes nothing: safe to send
        # again whatever its kind (a flatten_all's reads keep only the kind's
        # priority). Anything else is sent once, whatever its kind: decided
        # by kind, the level-keeping PUT (kind background) would be sent
        # twice after a 502, 503 or 504, or after an answer lost or
        # unreadable.
        writes = method != "GET"
        attempts = 3
        url = self.rest.prefix + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None},
                safe=",:")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers(data is not None)
        # Where the request goes: the endpoint's pool, always (a redirect
        # is followed on the same scheme, host and port only, see _follow),
        # at `url`, which a followed redirect (once at most, `hops`) changes.
        # A redirect followed is not counted as one of the attempts.
        pool, hops, attempt = self.rest, 0, -1
        while True:
            attempt += 1
            if attempt >= attempts + hops:
                break
            if self.stand_down.is_set() and getattr(self._tl, "yields", False):
                raise OandaError(0, "flatten_all was asked for",
                                 code="flatten", sent=False)
            left = self.time_left()
            if left is not None and left <= 0.05:
                raise OandaError(0, "the time for this call is used up",
                                 code="deadline", sent=False)
            until = None if left is None else time.monotonic() + left
            tmo = timeout or REQUEST_TIMEOUT
            if left is not None:
                tmo = max(0.2, min(tmo, left))
            self.pace.take(until, urgent=kind == "flatten")
            conn, reused = pool.take(
                tmo, until, IDLE_REUSE_WRITE if writes else None)
            if writes and not reused:
                # An order never goes out as the first request of a new
                # connection: one OANDA refused (2 new a second, counted per
                # address with every other tool there) would make its fate
                # unknown. A read proves the connection first.
                conn = self._proven(conn, tmo, until)
            try:
                conn.timeout = tmo
                if conn.sock is not None:
                    conn.sock.settimeout(tmo)
                self._write(conn, method, url, data, headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                if not writes and attempt < attempts + hops - 1:
                    # documented: development-guide, "Excess connections will
                    # be rejected": a NEW connection ended before any answer
                    # is most likely one of those, so the next try waits the
                    # second out; a kept-alive one is tried again at once.
                    pause = 0.0 if reused else 1.1
                    left2 = self.time_left()
                    if left2 is None or left2 > pause + 0.5:
                        if pause:
                            time.sleep(pause)
                        continue
                # Whether OANDA carried a write out is NOT known.
                raise OandaError(0, "OANDA did not answer (%s)"
                                 % type(e).__name__, code="lost",
                                 sent=True, cause=type(e).__name__) from None
            if resp.will_close:
                conn.close()
            else:
                pool.give(conn)
            status = resp.status
            if 300 <= status < 400:
                # OANDA's live API answered account summary reads 307 on
                # 2026-11-15, and a minute later did not. A GET answered 307
                # or 308 is followed once, to the endpoint's own scheme, host
                # and port; nothing else is followed, and a request that is
                # not a GET is never sent again, whatever its kind (the
                # level-keeping PUT included): see _follow.
                url = self._follow(pool, url, method, status,
                                   resp.getheader("Location"), hops, kind)
                hops = 1
                continue
            if 200 <= status < 300:
                self.pace.eased()
                if not raw:
                    return {}
                try:
                    out = json.loads(raw)
                except ValueError:
                    # An answer came and could not be read: its cause is
                    # said as such, never as "did not answer".
                    raise OandaError(0, "OANDA's answer was not JSON (%d "
                                     "bytes)" % len(raw), code="lost",
                                     sent=True, cause="not JSON, %d bytes"
                                     % len(raw), answered=True) from None
                return out if isinstance(out, dict) else {}
            err = self._error(status, raw)
            if status == 429:
                # assumed, verify on demo: a 429 is a request OANDA did not
                # carry out (documented only as "Excess requests will
                # receive HTTP 429 error"). Paused, then once more.
                self.pace.blocked()
                wait = self.pace.blocked_for()
                if attempt == 0 and (self.time_left() is None
                                     or self.time_left() > wait + 1.0):
                    if kind != "flatten":
                        time.sleep(wait)
                    else:
                        time.sleep(min(wait, 0.5))
                    continue
                raise err
            if (status in (502, 503, 504) and not writes and attempt == 0
                    and (self.time_left() is None or self.time_left() > 1.0)):
                time.sleep(0.3)
                continue
            raise err
        raise OandaError(0, "OANDA did not answer", code="lost", sent=True,
                         cause="")

    @staticmethod
    def _write(conn, method: str, url: str, data, headers: dict):
        """conn.request, or OandaError when the request cannot be written at
        all: a path or a header http.client cannot put on the wire (a letter
        that is not ASCII, a control character). Nothing was sent, and that
        connection, in no state to be used again, is closed and never given
        back to the pool. Without this, a GET sent on to a path with a
        latin-1 letter in it escapes call() as UnicodeEncodeError, which the
        platform gets as "internal: UnicodeEncodeError: ...", and its
        connection is neither closed nor given back."""
        try:
            conn.request(method, url, body=data, headers=headers)
        except (http.client.InvalidURL, UnicodeError, ValueError,
                TypeError) as e:
            conn.close()
            raise OandaError(0, "The request could not be written for OANDA "
                             "(%s), so it was not sent." % type(e).__name__,
                             code="unsendable", sent=False) from None

    @staticmethod
    def _redirect_target(pool, url: str, location):
        """Where a Location sends a request that was sent to `pool` at `url`:
        (scheme, host, port, path and query), or, when it names no address
        that could be followed, a few words that say so without quoting it:
        none given; not a plain URL as written (_location_fault: white space
        or a character that cannot be printed, one that is not ASCII, a
        backslash, no host after a //); or, joined to the endpoint, not a
        plain URL by _endpoint_shape (a user part, a port that is not one,
        anything read two ways)."""
        if not isinstance(location, str) or not location.strip():
            return "with no address in it"
        location = location.strip()
        fault = _location_fault(location)
        if fault:
            return "to an address that is not a plain URL (it %s)" % fault
        host = "[%s]" % pool.host if ":" in pool.host else pool.host
        try:
            target = urllib.parse.urljoin(
                "%s://%s:%d%s" % (pool.scheme, host, pool.port, url),
                location)
            fault = _endpoint_shape(target)
            if fault:
                return "to an address that %s" % fault[0]
            u = urllib.parse.urlsplit(target)
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            return "to an address that is not a URL this adapter can read"
        tail = (u.path or "/") + ("?" + u.query if u.query else "")
        return (u.scheme, (u.hostname or "").lower(), port, tail)

    def _redirect_words(self, pool, where) -> str:
        """A redirect's target in words: its host, and what of the scheme,
        host and port is not the endpoint's; never its path or query. One
        that names no address to follow is said with the endpoint's host
        alone, never anything the Location holds."""
        if isinstance(where, str):
            return "a redirect %s, from %s" % (where, _one_line(
                self.scrub(pool.host.lower()), 100) or "the endpoint")
        scheme, host, port, _ = where
        name = _one_line(self.scrub(host), 100) or "no host"
        own = pool.host.lower()
        if host != own:
            return ("a redirect to %s, another host than the endpoint's (%s)"
                    % (name, own))
        if scheme != pool.scheme:
            return ("a redirect to %s over %s, where the endpoint is %s"
                    % (name, _one_line(scheme, 20), pool.scheme))
        if port != pool.port:
            return ("a redirect to %s on port %d, another port than the "
                    "endpoint's (%d)" % (name, port, pool.port))
        return "a redirect to %s, the endpoint itself" % name

    def _follow(self, pool, url: str, method: str, status: int, location,
                hops: int, kind: str = "read") -> str:
        """Where a request sent to `pool` at `url` and answered `status` (a
        3xx) goes once more: its new path and query, on the same pool. Or
        OandaError, saying which rule it met, naming the target's host and
        never its path or query (the Location may carry anything).

        Followed only when its method is GET, the answer is 307 or 308, no
        redirect has been followed for it yet (`hops`), and the Location
        (whole or relative) is on the same scheme, host and port as the
        endpoint it was sent to: OANDA's 307s of 2026-11-15 were reads, and a
        read sent on to another host, OANDA's live host from a practice
        process among them, would take the token there.

        A request that is not a GET (an order, a cancel, a close, the levels
        kept on a trade: whatever its kind) is never sent again. Answered 307
        or 308 it was not carried out at this address, so it is refused
        (code "redirect": final for an order's client_id, order.rejected).
        After another 3xx it may have been carried out (a 303 can answer a
        request done), so its fate is not known and OANDA's record answers
        for it (code "lost", _lost).

        A change one of the adapter's own jobs makes (`kind` background, the
        stop or target kept on a trade; flatten, flatten_all's own closes
        and cancels) is said in a clause that ends nothing: its job says
        what comes next ("tried again in 5 s", "Ask again."). Said in the
        words for a platform's order, "... nor sent again: send it again as
        a new request ..." would stand beside the job's "tried again"."""
        where = self._redirect_target(pool, url, location)
        to = self._redirect_words(pool, where)
        if method != "GET":
            if status in (307, 308):
                if kind in ("background", "flatten"):
                    raise OandaError(
                        status, "OANDA answered HTTP %d, %s, and a redirect "
                        "is never followed for a change" % (status, to),
                        code="redirect")
                raise OandaError(
                    status, "It was not carried out: OANDA answered HTTP %d, "
                    "%s. A request that changes something is never followed, "
                    "nor sent again: send it again as a new request if it is "
                    "still wanted." % (status, to), code="redirect")
            raise OandaError(status, "OANDA answered HTTP %d, %s, which is "
                             "not followed" % (status, to),
                             code="lost", sent=True)
        if status not in (307, 308):
            why = "only a GET answered 307 or 308 is followed"
        elif hops:
            why = "a redirect is followed once, and this was the second"
        elif isinstance(where, str):
            why = "it gives no address that can be followed"
        elif where[:3] != (pool.scheme, pool.host.lower(), pool.port):
            why = ("a redirect is followed only to the scheme, host and port "
                   "of the endpoint it was sent to")
        else:
            return where[3]
        raise OandaError(status, "OANDA answered HTTP %d, %s, and it is not "
                         "followed: %s" % (status, to, why), code="redirect")

    def _proven(self, conn, tmo: float, until: float | None):
        """`conn`, or a later new one, once a read has been answered on it.
        Raises OandaError(sent=False) when none can be had in time: the write
        was then certainly never sent."""
        for attempt in range(3):
            self.pace.take(until, urgent=True)
            try:
                conn.timeout = tmo
                if conn.sock is not None:
                    conn.sock.settimeout(tmo)
                conn.request("GET", self.rest.prefix + "/v3/accounts",
                             headers=self._headers(False))
                resp = conn.getresponse()
                resp.read()
                if not resp.will_close:
                    return conn
            except (OSError, http.client.HTTPException):
                pass
            conn.close()
            left = self.time_left()
            if attempt == 2 or (left is not None and left < 2.0):
                break
            time.sleep(1.1)
            conn = self.rest.connect(tmo, until)
        raise OandaError(0, "no connection to OANDA could be opened for the "
                         "order (OANDA allows 2 new connections a second on "
                         "this address); it was NOT sent", code="connect",
                         sent=False)

    def open_stream(self, path: str, query: dict | None = None):
        """A chunked JSON-lines stream: (connection, response). Raises
        OandaError for a refusal."""
        if self.cfg.get("fault"):
            raise OandaError(0, self.cfg["fault"], code="endpoint",
                             sent=False)
        if not self.cfg.get("token") or self.cfg.get("cred_fault"):
            raise OandaError(401, "no usable API token", sent=False)
        url = self.stream.prefix + path
        if query:
            url += "?" + urllib.parse.urlencode(query, safe=",:")
        # A stream is a GET: answered 307 or 308 it is followed once, to its
        # own scheme, host and port, on a new connection (_follow).
        hops = 0
        while True:
            conn = self.stream.connect(STREAM_READ_TIMEOUT, None)
            try:
                self._write(conn, "GET", url, None, self._headers(False))
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                raise OandaError(0, "the stream did not answer (%s)"
                                 % type(e).__name__, code="lost") from None
            if resp.status == 200:
                return conn, resp
            try:
                raw = resp.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            conn.close()
            if 300 <= resp.status < 400:
                url = self._follow(self.stream, url, "GET", resp.status,
                                   resp.getheader("Location"), hops)
                hops = 1
                continue
            raise self._error(resp.status, raw)


# ---------------------------------------------------------------------------
# The catalog: OANDA's instruments as InstrumentSpecs. A pure function.
# ---------------------------------------------------------------------------

def sessions_for(name: str, typ: str) -> list:
    if typ == "CURRENCY":
        return [dict(s) for s in (SESSION_TRY if "TRY" in name.split("_")
                                  else SESSION_FX)]
    if typ == "METAL":
        return [dict(s) for s in SESSION_METAL]
    return []


def build_instrument(row: dict) -> tuple[dict, dict] | None:
    """One OANDA Instrument (documented: https://developer.oanda.com/
    rest-live-v20/primitives-df/ Instrument) as (InstrumentSpec, the numbers
    this adapter keeps for itself), or None when a field it needs is
    missing."""
    try:
        name = str(row["name"])
        typ = str(row.get("type") or "")
        dp = int(row["displayPrecision"])
        tup = int(row.get("tradeUnitsPrecision") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    # assumed, verify on demo: the quote grid is 10^-displayPrecision (OANDA
    # refuses a price with more decimals, PRICE_PRECISION_EXCEEDED).
    tick = Decimal(1).scaleb(-dp)
    # documented: tradeUnitsPrecision "The amount of decimal places that may
    # be provided when specifying the number of units traded".
    step = Decimal(1).scaleb(-tup)
    mn = _dec(row.get("minimumTradeSize")) or step
    mx = _dec(row.get("maximumOrderUnits")) or Decimal("1e9")
    margin = _dec(row.get("marginRate")) or Decimal(1)
    base, _, quote = name.partition("_")
    fin = row.get("financing") or {}
    triple = None
    for d in fin.get("financingDaysOfWeek") or []:
        try:
            if int(d.get("daysCharged") or 0) == 3:
                triple = ISO_DAY.get(str(d.get("dayOfWeek")).upper())
        except (TypeError, ValueError):
            pass
    comm = row.get("commission") or {}
    per = D0
    traded = _dec(comm.get("unitsTraded"))
    if traded:
        per = _d0(comm.get("commission")) / traded
    minc = _d0(comm.get("minimumCommission"))
    tags = [str((t or {}).get("name") or "").lower()
            for t in row.get("tags") or [] if isinstance(t, dict)]
    if typ == "CURRENCY":
        klass = "fx"
    elif typ == "METAL":
        klass = "metal"
    else:
        # assumed, verify on demo: an ASSET_CLASS tag names a CFD's kind.
        klass = next((t for t in tags if t in ("index", "commodity", "bond")),
                     "cfd")
    spec = {
        "symbol": name, "name": str(row.get("displayName") or name),
        "asset_class": klass,
        "base_ccy": base if typ in ("CURRENCY", "METAL") else None,
        "quote_ccy": quote or None, "digits": dp, "tick_size": float(tick),
        # one unit is the contract: a tick of one unit is worth one tick, in
        # the quote currency (SPEC 4 default; OANDA sends no tick value)
        "tick_value": float(tick), "lot_step": float(step),
        "min_qty": float(mn), "max_qty": float(mx), "min_notional": 0,
        "contract_size": 1, "margin_rate": float(margin),
        # documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-
        # notable-differences-2024.pdf and the Terms of Business 9.4/9.8:
        # close-out at 50% of the margin needed.
        "margin_maintenance_rate": float(margin / 2), "margin_tiers": None,
        "sessions": sessions_for(name, typ), "holidays": [],
        "early_closes": [], "tradable": True, "shortable": True,
        "trade_mode": "full",
        # documented (sub-accounts PDF): pending orders at any price on the
        # correct side of the market; no stop distance.
        "stops_level_points": 0,
        "fees": ({"commission_per_lot": 0, "mode": "none", "rate": 0,
                  "min": 0} if per == 0 and minc == 0 else
                 {"commission_per_lot": float(per), "mode": "per_lot",
                  "rate": float(per), "min": float(minc)}),
        # documented: InstrumentFinancing longRate/shortRate are decimals
        # (0.05 is 5%); assumed, verify on demo: a year's rate.
        "swap_long": float(_d0(fin.get("longRate")) * 100),
        "swap_short": float(_d0(fin.get("shortRate")) * 100),
        "swap_mode": "pct_annual",
        "expires_at": None, "successor": None, "dividend": None,
        "pip_size": float(Decimal(1).scaleb(int(row.get("pipLocation")
                                                    or 0))),
    }
    if triple:
        spec["swap_3day_dow"] = triple
    own = {"type": typ, "dp": dp, "tup": tup, "tick": tick, "step": step,
           "min": mn, "max": mx, "margin": margin, "quote": quote,
           "min_trail": _dec(row.get("minimumTrailingStopDistance")),
           "max_trail": _dec(row.get("maximumTrailingStopDistance"))}
    return spec, own


def oanda_client_id(cid: str) -> str:
    """The platform's client_id as OANDA's ClientID. assumed, verify on demo:
    OANDA documents no length or alphabet for it, so anything that is not a
    short plain id (it also goes into a URL path as "@id") is hashed."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", cid):
        return cid
    return "brue-" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Bars: one store per (symbol, timeframe), closed bars only.
# ---------------------------------------------------------------------------

class _BarStore:
    def __init__(self):
        self.rows: dict = {}
        self.head_at = 0.0
        self.oldest_done = False
        self.lock = threading.Lock()
        self.reader = False
        self.used = time.monotonic()

    def newest(self, n: int) -> list:
        ts = sorted(self.rows)
        return [list(self.rows[t]) for t in ts[-n:]]


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------

class OandaAdapter(Adapter):
    name = "oanda"
    version = VERSION
    broker = "OANDA"
    # The careful value: __init__ replaces it with what the hosts say, so
    # anything that skips that step is live, never paper.
    mode = "live"
    # Cold guesses, replaced by what is remembered of the bound account.
    account_currency = "GBP"
    account_model = "netting"
    # What the user must type (SPEC 3.2): OANDA's personal access token. It
    # arrives only as BRUE_CRED_TOKEN at spawn; OANDA keeps no copy of it.
    credential_form = [{"key": "token", "label": "API token",
                        "type": "password"}]
    # SPEC 3.3, from OANDA's licence (see TERMS_URL above).
    terms = {"data_local_only": True, "orders_by_hand_only": False,
             "summary": TERMS_SUMMARY, "url": TERMS_URL}

    def __init__(self, argv=None, environ=None, out=None):
        super().__init__(out)
        environ = os.environ if environ is None else environ
        self.cfg = resolve_environment(
            sys.argv[1:] if argv is None else argv, environ)
        self.mode = self.cfg["mode"]
        if self.cfg.get("ignored"):
            log("%s not used: practice reaches only OANDA's practice hosts or "
                "this machine (--live, or OANDA_ENV=live, for anything else)"
                % " and ".join(self.cfg["ignored"]))
        self.client = OandaClient(self.cfg)
        self._state_dir = environ.get("BRUE_BROKER_STATE") or ""
        if self._state_dir:
            # OANDA counts new connections per address: this process's first
            # waits out the turn of the profile's last one (_Gate).
            self.client.gate.remember(
                os.path.join(self._state_dir, "oanda_connections"))
        self._want_account = (environ.get("BRUE_ACCOUNT_ID") or "").strip()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False
        self._open_lock = threading.Lock()
        # the bound account
        self._acct: str | None = None
        self._mt4 = False
        self._hedging = False
        self._summary: dict = {}
        self._account_dirty = False
        self._account_sent: dict | None = None
        self._account_t = 0.0
        # catalog
        self._catalog: dict = {}
        self._inst: dict = {}
        self._catalog_list: list = []
        self._catalog_at = 0.0
        # transactions: everything <= _contig is applied; _seen holds those
        # above it that arrived out of order
        self._contig = 0
        self._seen: set = set()
        self._high = 0
        self._beat_last = 0
        self._gap_wake = threading.Event()
        self._gap_t = 0.0
        # (since, lastTransactionID, count) of identical empty answers from
        # OANDA's record (see _read_since)
        self._empty: tuple | None = None
        # orders: OANDA order id -> what is known of it
        self._orders: dict = {}
        self._current: dict = {}      # root order id -> newest replacement
        self._cids: dict = {}         # OANDA client id -> platform client id
        # trade id -> platform client id, for trades that carry no client id
        # at OANDA (an MT4-linked account): kept so a restart still knows
        self._trade_cids: dict = {}
        self._intent_trade: dict = {}  # trade id -> (kind, cid), close on wire
        self._intent_inst: dict = {}   # instrument -> (kind, cid)
        # trades: OANDA trade id -> what is known of it
        self._trades: dict = {}
        self._levels: dict = {}       # netting: symbol -> {"sl","tp","trail"}
        self._levels_t = 0.0
        self._trailed: dict = {}      # trade id -> trailing value last said
        self._changes_t = 0.0
        # idempotency beyond the SDK's memory
        self._final: dict = {}        # cid -> final refusal
        self._journal: dict = {}      # key -> request on the wire / unknown
        self._accounts_cache: dict = {}
        # The state file (see _save_state): which account's memory is held
        # (None until the process is bound), and what this process last read
        # or wrote of it, so that a save writes only its own changes.
        self._book_id: str | None = None
        self._disk_base: dict = {"accounts": {}, "book_id": None,
                                 "book": {}}
        self._save_gen = 0
        self._saved_gen = 0
        self._unsaved = False
        self._save_lock = threading.Lock()
        # quotes
        self._subs: set = set()
        self._subs_version = 0
        self._quotes: dict = {}
        self._quote_sent: dict = {}
        self._quote_hist: dict = {}    # symbol -> (monotonic, bid, ask) sent
        self._price_conn = None
        self._price_up = False
        self._price_since = None
        self._price_wake = threading.Event()
        # bars
        self._bars: collections.OrderedDict = collections.OrderedDict()
        self._bars_lock = threading.Lock()
        self._bar_subs: dict = {}
        # session state, one per component
        self._components: dict = {}
        self._session = None
        self._notices: dict = {}
        # lanes (see _dispatch)
        self._lanes: dict = {}
        self._lane_lock = threading.Lock()
        self._orders_waiting = 0
        self._orders_idle = threading.Event()
        self._orders_idle.set()
        self._flat_at = 0.0
        self._flat_pending = 0
        self._flat_idle = threading.Event()
        self._flat_idle.set()
        self._hello_said: tuple | None = None
        self._tx_conn = None
        self._load_state()
        acc = self._cached_account(self._want_account)
        if acc:
            self.account_currency = acc.get("currency") or self.account_currency
            self.account_model = ("hedging" if acc.get("hedging")
                                  else "netting")

    # == what this adapter is ============================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # sl / tp / trail_points: orders OANDA holds on each trade
                # (stopLossOnFill, takeProfitOnFill, trailingStopLossOnFill;
                # PUT /trades/{id}/orders). documented: order-ep, trade-ep.
                "brackets": True, "modify": True,
                "pending": True,
                # documented: order-df OrderType has LIMIT and STOP; there is
                # no stop-limit (a STOP's priceBound only cancels it on a gap)
                "pending_types": ["limit", "stop"],
                # documented: order-df TimeInForce GTC, GFD (5 pm New York),
                # GTD
                "expiry": ["gtc", "day", "gtd"],
                # documented: order-df TrailingStopLossOrder, kept by OANDA
                "trailing": True,
                "partial_close": True,
                # no OCO between free orders and no close-by at OANDA
                "oco": False, "close_by": False, "can_list": True,
                # documented (OANDA Corporation's help, and the cancel reason
                # INSUFFICIENT_MARGIN): a pending entry order does not count
                # towards margin until it triggers
                "reserves_margin": False},
            "data": {"quotes": True, "bars": True, "history": True,
                     "operations": True},
            # documented: authentication, a token "will grant access to all
            # of your sub-accounts"
            "accounts": {"multi": True},
            "flatten_all": True}

    def rate_limits(self) -> dict:
        # OANDA allows 120 a second per address; one platform request can
        # cost this adapter a few of its own.
        return {"requests_per_second": 20}

    def broker_info(self) -> dict:
        return {"display_name": "OANDA",
                "tagline": "Forex, metals and index, commodity and bond CFDs "
                           "by API",
                "mark": "OA",
                "website": "https://www.oanda.com",
                "support": "https://help.oanda.com",
                # As OANDA prints them: the UK footer (OANDA Europe Limited)
                # and the developer portal's footer.
                "regulated_by": ["FCA No: 542574", "CFTC/NFA No: 0325821",
                                 "CIRO", "MAS", "ASIC AFSL No. 412981",
                                 "Kanto Local Financial Bureau No. 2137"],
                "asset_classes": ["Forex", "Metals", "Index CFDs",
                                  "Commodity CFDs", "Bond CFDs"]}

    def _send(self, line: str):
        """Every answer leaves through here: the token is taken out of it,
        whatever put it there (SPEC 8.5), the SDK's own messages for an
        unexpected exception included."""
        super()._send(self.client.scrub(line))

    def _hello(self, params: dict) -> dict:
        out = super()._hello(params)
        with self._lock:
            self._hello_said = (out.get("account_currency"),
                                out.get("account_model"))
        return out

    # == errors and calls ================================================
    def _err(self, e: OandaError) -> AdapterError:
        return to_adapter_error(
            e, "practice" if self.cfg["env"] == "practice" else "live",
            self.cfg.get("rest_host") or "",
            self._acct or self._want_account)

    def _call(self, method, path, query=None, body=None, **kw):
        try:
            return self.client.call(method, path, query, body, **kw)
        except OandaError as e:
            raise self._err(e) from None

    def _path(self, tail: str = "") -> str:
        return "/v3/accounts/%s%s" % (urllib.parse.quote(self._acct or "",
                                                         safe=""), tail)

    # == lanes ===========================================================
    def _dispatch(self, rid: str, method: str, params: dict):
        """The read thread's whole job: note WHEN the request was read, and
        hand it on to a lane; it never waits on OANDA (see the module
        docstring). `hello`, `ping` and `bye` are answered here."""
        read_at = time.monotonic()
        if not isinstance(params, dict):
            params = {}
        # By the exact name only: another spelling of a method
        # ("order_place") would go to the reads lane, past the orders lane
        # and _mutate, and be carried out.
        handler = self._handler(method)
        if method in ("hello", "ping", "bye") or handler is None:
            if method == "bye":
                self._drain(BYE_DRAIN)
            super()._dispatch(rid, method, params)
            return
        lane = ("flatten" if method == "flatten_all"
                else "orders" if method in MUTATING
                else "charts" if method == "bars.history" else "reads")
        with self._lane_lock:
            q = self._lanes.get(lane)
            if q is None:
                q = self._lanes[lane] = queue.Queue()
                for i in range({"orders": 1, "flatten": 1,
                                "reads": READ_WORKERS,
                                "charts": CHART_WAITERS}[lane]):
                    threading.Thread(target=self._lane_loop, args=(lane, q),
                                     daemon=True,
                                     name="oanda-%s-%d" % (lane, i)).start()
            if lane == "orders":
                self._orders_waiting += 1
                self._orders_idle.clear()
            elif lane == "flatten":
                self._flat_at = read_at
                self._flat_pending += 1
                self._flat_idle.clear()
                self.client.stand_down.set()
        q.put((rid, method, params, read_at))

    def _lane_loop(self, lane: str, q: "queue.Queue"):
        while True:
            rid, method, params, read_at = q.get()
            try:
                self._answer(lane, rid, method, params, read_at)
            except Exception as e:  # noqa: BLE001 - a lane outlives anything
                text = self.client.scrub("%s: %s" % (type(e).__name__, e))
                log("%s: %s" % (method, text))
                try:
                    self._send(protocol.res_error(rid, "internal", text))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.client.set_deadline(None)
                if lane == "orders":
                    with self._lane_lock:
                        self._orders_waiting -= 1
                        if self._orders_waiting <= 0:
                            self._orders_idle.set()
                elif lane == "flatten":
                    with self._lane_lock:
                        self._flat_pending -= 1
                        if self._flat_pending <= 0:
                            self.client.stand_down.clear()
                            self._flat_idle.set()

    def _answer(self, lane: str, rid: str, method: str, params: dict,
                read_at: float):
        budget = FLATTEN_BUDGET if method == "flatten_all" else HANDLER_BUDGET
        self.client.set_request(
            read_at, budget,
            floor=FLATTEN_FLOOR if method == "flatten_all" else 0.0,
            yields=lane == "orders")
        left = self.client.time_left()
        if lane == "orders":
            if read_at < self._flat_at:
                # Sent before the panic button was pressed and not begun:
                # carrying it out now, behind flatten_all, is what nobody
                # asked for.
                self._send(protocol.res_error(
                    rid, "x_oanda_flattening", "flatten_all was asked for "
                    "after this call was sent and before it was begun: it "
                    "was NOT carried out. Ask again if it is still wanted.",
                    True, "frozen"))
                return
            if not self._flat_idle.wait(timeout=max(0.05, left - 0.3)):
                self._send(protocol.res_error(
                    rid, "x_oanda_flattening", "flatten_all is still "
                    "running; this call was not carried out. Ask again.",
                    True, "frozen"))
                return
            left = self.client.time_left()
        if left < 0.3:
            self._send(protocol.res_error(
                rid, "rate_limited", "the adapter was busy with earlier "
                "requests for %.1f s and the time for this one is up; nothing "
                "was done. Ask again." % (time.monotonic() - read_at), True))
            return
        if not self._opened and not method.startswith(PRE_SESSION):
            # The session is opened ONCE, by whichever request comes first.
            if not self._open_lock.acquire(timeout=max(0.05, left - 0.3)):
                self._send(protocol.res_error(
                    rid, "internal", "the session with OANDA is still being "
                    "opened; ask again in a moment", True))
                return
            try:
                if not self._opened:
                    try:
                        self.on_first_call()
                        self._opened = True
                    except AdapterError as e:
                        self._send(protocol.res_error(
                            rid, e.code, self.client.scrub(str(e)),
                            e.retryable, e.like))
                        return
                    except Exception as e:  # noqa: BLE001
                        self._send(protocol.res_error(
                            rid, "internal", self.client.scrub(
                                "opening the session with OANDA failed: "
                                "%s: %s" % (type(e).__name__, e))))
                        return
            finally:
                self._open_lock.release()
        if method in MUTATING:
            self._mutate(rid, method, params)
        else:
            try:
                super()._dispatch(rid, method, params)
            except Exception as e:  # noqa: BLE001
                self._send(protocol.res_error(rid, "internal",
                                              self.client.scrub(str(e))))

    def _mutate(self, rid: str, method: str, params: dict):
        """A call that changes something, idempotent on its client_id (SPEC
        2): the ORIGINAL outcome for a replay, the original refusal too, and
        `duplicate_client_id` for a reuse with other parameters."""
        handler = self._handler(method)
        cid = params.get("client_id")
        cid = str(cid) if cid else ""
        sig = protocol.order_signature(method, params)
        if cid:
            try:
                prior = self.idempotent_check(cid, sig)
            except AdapterError as e:
                self._send(protocol.res_error(rid, e.code, str(e)))
                return
            if prior is not None:
                self._send(protocol.res(rid, prior))
                return
            with self._lock:
                fin = self._final.get(cid)
            if fin is not None:
                if fin.get("sig") != sig:
                    self._send(protocol.res_error(
                        rid, "duplicate_client_id",
                        "client_id reused with different parameters"))
                else:
                    self._send(protocol.res_error(
                        rid, fin["code"], fin["message"], False,
                        fin.get("like")))
                return
        try:
            result = handler(params)
        except AdapterError as e:
            text = self.client.scrub(str(e))
            if (cid and not e.retryable and not getattr(e, "unknown", False)
                    and e.code != "duplicate_client_id"):
                with self._lock:
                    self._final[cid] = {"sig": sig, "code": e.code,
                                        "message": text, "like": e.like,
                                        "at": self.now_ms()}
                    self._trim(self._final)
                    self._unsaved = True
                self._save_state()
            self._send(protocol.res_error(rid, e.code, text, e.retryable,
                                          e.like))
            return
        except Exception as e:  # noqa: BLE001 - never crash the lane
            self._send(protocol.res_error(
                rid, "internal", self.client.scrub("%s: %s" % (
                    type(e).__name__, e))))
            return
        result = result if result is not None else {}
        if cid:
            self.idempotent_store(cid, result, sig)
        self._send(protocol.res(rid, result))

    def _drain(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        return (self._orders_idle.wait(seconds) and self._flat_idle.wait(
            max(0.0, end - time.monotonic())))

    # == the session =====================================================
    def _need_token(self):
        if not self.cfg["token"]:
            log("no token: the platform passes BRUE_CRED_TOKEN (form key "
                "'token'); by hand, set BRUE_CRED_TOKEN")
            raise AdapterError(
                "unauthorized", "OANDA needs your API token (the personal "
                "access token from your OANDA login). Enter it to connect.")
        if self.cfg["cred_fault"]:
            raise AdapterError("unauthorized", "OANDA cannot be asked: "
                               + self.cfg["cred_fault"] + ".")
        if self.cfg["fault"]:
            raise AdapterError("x_oanda_endpoint", self.cfg["fault"])

    def _cached_account(self, want: str) -> dict | None:
        with self._lock:
            if want:
                return self._accounts_cache.get(want)
            if len(self._accounts_cache) == 1:
                return next(iter(self._accounts_cache.values()))
        return None

    def handle_accounts_list(self, p):
        """Every account the token reaches (SPEC 4, revision 1.7), answered
        before any session. Only OANDA's own words: an account whose summary
        cannot be read is shown by its id alone."""
        self._need_token()
        rows = self._call("GET", "/v3/accounts").get("accounts") or []
        bound = self._acct or self._want_account
        out = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            aid = str(r["id"])
            row = {"id": aid, "mode": self.mode}
            mt4 = r.get("mt4AccountID") is not None
            try:
                s = self._call("GET", "/v3/accounts/%s/summary"
                               % urllib.parse.quote(aid, safe="")
                               ).get("account") or {}
            except AdapterError as e:
                log("account %s: summary not read (%s)" % (aid, e.code))
                s = {}
            if s.get("alias"):
                row["label"] = str(s["alias"])
            if s.get("currency"):
                row["currency"] = str(s["currency"])
            if s.get("balance") is not None:
                row["balance"] = _fl(s.get("balance"))
            if mt4:
                row["type"] = "mt4"
            if s:
                row["hedging"] = bool(s.get("hedgingEnabled"))
                with self._lock:
                    self._accounts_cache[aid] = {
                        "currency": str(s.get("currency") or ""),
                        "hedging": bool(s.get("hedgingEnabled")),
                        "alias": s.get("alias"), "mt4": mt4}
                    self._unsaved = True
            row["selected"] = bool(bound) and aid == bound
            out.append(row)
        with self._lock:
            # Kept to what the token reaches now, so a cold hello that has no
            # binding can trust a single remembered account.
            ids = {r["id"] for r in out}
            for aid in list(self._accounts_cache):
                if aid not in ids:
                    self._accounts_cache.pop(aid, None)
            self._unsaved = True
        self._save_state()
        return {"accounts": out}

    def on_first_call(self):
        """Open the session: the token proven by OANDA's list of accounts,
        the account bound, its catalog, its book, then the threads. `hello`
        never comes here (SPEC 3.1)."""
        self._need_token()
        rows = self._call("GET", "/v3/accounts").get("accounts") or []
        props = {str(r.get("id")): r for r in rows
                 if isinstance(r, dict) and r.get("id")}
        if self._want_account:
            if self._want_account not in props:
                raise AdapterError(
                    "x_oanda_account_not_reachable", "this OANDA token does "
                    "not reach account %s (it reaches %s). A token reaches "
                    "the sub-accounts its login had when it was made; a "
                    "sub-account added later needs a new token."
                    % (self._want_account, ", ".join(sorted(props)) or
                       "none"), like="unauthorized")
            aid = self._want_account
        elif len(props) == 1:
            aid = next(iter(props))
        elif not props:
            raise AdapterError("x_oanda_no_account", "OANDA lists no v20 "
                               "account for this token")
        else:
            raise AdapterError(
                "x_oanda_choose_account", "this OANDA token reaches %d "
                "accounts (%s): choose one (accounts.list, then the "
                "platform sets BRUE_ACCOUNT_ID); this adapter does not "
                "pick for you." % (len(props), ", ".join(sorted(props))))
        mt4 = props[aid].get("mt4AccountID") is not None
        tags = " ".join(str((t or {}).get("name") or "") + " "
                        + str((t or {}).get("type") or "")
                        for t in props[aid].get("tags") or []
                        if isinstance(t, dict)).lower()
        if "spread" in tags:
            # assumed, verify on demo: OANDA documents no field that marks a
            # spread-bet sub-account over v20; a tag saying so is refused.
            raise AdapterError(
                "x_oanda_spread_bet", "account %s looks like a spread-bet "
                "sub-account, which this adapter does not support yet" % aid,
                like="trade_restricted")
        self._acct = aid
        summ = self._call("GET", self._path("/summary"))
        acct = summ.get("account") or {}
        ccy = str(acct.get("currency") or "")
        hedging = bool(acct.get("hedgingEnabled"))
        model = "hedging" if hedging else "netting"
        with self._lock:
            self._accounts_cache[aid] = {"currency": ccy, "hedging": hedging,
                                         "alias": acct.get("alias"),
                                         "mt4": mt4}
            self._unsaved = True
        self._save_state()
        said = self._hello_said
        if said is not None and said != (ccy, model):
            self._acct = None
            # SPEC 6's `reconnect`: start this adapter again (what was read
            # is remembered above, so its next hello says it). The words are
            # kept for a platform that does not act on the code itself.
            raise AdapterError(
                "reconnect", "this adapter's handshake said %s and "
                "%s before it had read the account; account %s is %s and %s. "
                "That is now remembered: reconnect, and the handshake will "
                "say so." % (said[1], said[0], aid, model, ccy))
        self.account_currency, self.account_model = ccy, model
        self._bind_account(aid)
        self._mt4, self._hedging = mt4, hedging
        with self._lock:
            self._summary = acct
            self._account_t = time.monotonic()
        self._load_catalog()
        if not self._started:
            # The book and the watermark from ONE answer of OANDA's: the
            # book is then of exactly the transactions up to that id, and
            # none of them is ever applied to it again (what follows is read
            # from OANDA's record from there on).
            rows, last = self._read_trades()
            with self._lock:
                self._trades = rows
                self._contig = self._high = last
                self._seen = set()
        else:
            self._sync_book()
        self._load_pending()
        self._levels_at_start()
        self._say_unknown()
        if not self._started:
            self._started = True
            for target, label in ((self._tx_loop, "transactions"),
                                  (self._price_loop, "prices"),
                                  (self._backstop, "backstop")):
                threading.Thread(target=self._keep_running,
                                 args=(target, label), name="oanda-" + label,
                                 daemon=True).start()
        self._set_session("session", "connected", "OANDA %s account %s (%s, "
                          "%s)" % (self.cfg["env"], aid, model, ccy))

    def _keep_running(self, target, label: str):
        """A thread's loop survives anything: it is started again, and said."""
        while not self._stop.is_set():
            try:
                target()
                return
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    return
                log("%s: %s; started again" % (label, self.client.scrub(
                    "%s: %s" % (type(e).__name__, e))))
                self._stop.wait(1.0)

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        self._price_wake.set()
        self._gap_wake.set()
        for conn in (self._price_conn, self._tx_conn):
            self._shut(conn)
        self._save_state()

    @staticmethod
    def _shut(conn):
        if conn is None:
            return
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _set_session(self, component: str, state: str, detail: str):
        """One state per component, one `session.state` for the worst."""
        with self._lock:
            self._components[component] = (state, detail)
            rank = {"connected": 0, "degraded": 1, "disconnected": 2}
            worst = max((s for s, _ in self._components.values()),
                        key=lambda s: rank.get(s, 1))
            if worst == "connected":
                text = self._components.get("session", ("", detail))[1]
            else:
                text = "; ".join(d for s, d in self._components.values()
                                 if s != "connected")
            if (worst, text) == self._session:
                return
            self._session = (worst, text)
        self.emit("session.state", {"state": worst, "detail": text})

    def _say(self, key: str, text: str, ttl: float | None = None):
        """A thing the user must know, said (session.state degraded) until
        cleared, or for `ttl` seconds."""
        if ttl is not None:
            with self._lock:
                self._notices["note:" + key] = time.monotonic() + ttl
        self._set_session("note:" + key, "degraded", text)

    def _tidy(self):
        """Notices whose time is up are taken back; done orders are not kept
        for ever."""
        now = time.monotonic()
        with self._lock:
            due = [k for k, t in self._notices.items() if t <= now]
            for k in due:
                self._notices.pop(k, None)
            if len(self._orders) > 5000:
                done = [oid for oid, r in self._orders.items()
                        if r.get("done") and r.get("kind") != "dependent"]
                for oid in done[:len(self._orders) - 4000]:
                    self._orders.pop(oid, None)
        for k in due:
            self._set_session(k, "connected", "")

    def _unsay(self, key: str):
        with self._lock:
            had = ("note:" + key) in self._components
        if had:
            self._set_session("note:" + key, "connected", "")

    # == persistence =====================================================
    # One small file in the profile's private directory, and beside it
    # `oanda_connections`, the time of the profile's last new connection
    # (_Gate). Never `account.json`: that name is the platform's account pin.
    def _state_path(self) -> str:
        return (os.path.join(self._state_dir, "oanda_state.json")
                if self._state_dir else "")

    # Layout (version 2): {"version": 2, "accounts": {id: what the cold hello
    # needs}, "books": {id: {"results", "final", "journal", "current", "cids",
    # "trade_cids", "levels"}}}. Every process of the profile shares the file
    # (SPEC 4: the platform lists accounts with one process while another,
    # bound, trades), so a save writes only the keys THIS process changed,
    # onto the file as it is on disk, under a lock (_FileLock, _patch): a
    # process that loaded the file earlier can never put back what another
    # has since written or removed. Each account's memory is its own: a
    # process bound to one never consults another's client ids or levels.
    BOOK_PARTS = ("results", "final", "journal", "current", "cids",
                  "trade_cids", "levels")

    def _read_file(self) -> dict:
        """The state file as it is on disk now, as version 2 ({} when there
        is none or it cannot be read). The caller holds the file lock."""
        path = self._state_path()
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError) as e:
            log("state file unreadable (%s); it is written anew"
                % type(e).__name__)
            return {}
        if not isinstance(st, dict):
            return {}
        if st.get("version") != 2:
            # version 1 held ONE account's memory at its top level
            old = {k: st.get(k) for k in self.BOOK_PARTS
                   if isinstance(st.get(k), dict)}
            st = {"version": 2, "accounts": st.get("accounts") or {},
                  "books": ({str(st["account"]): old}
                            if st.get("account") and old else {})}
        if not isinstance(st.get("accounts"), dict):
            st["accounts"] = {}
        if not isinstance(st.get("books"), dict):
            st["books"] = {}
        return st

    def _load_state(self):
        """At start, what the cold hello needs: the accounts remembered. An
        account's own memory is read when the process is bound to it
        (_bind_account), fresh, so it is what the file holds THEN."""
        if not self._state_path():
            return
        with _FileLock(self._state_path() + ".lock"):
            st = self._read_file()
        accounts = {str(k): v for k, v in st.get("accounts", {}).items()
                    if isinstance(v, dict)}
        with self._lock:
            self._accounts_cache = accounts
            self._disk_base["accounts"] = _norm(accounts)

    def _book_parts(self) -> dict:
        """The bound account's memory, part by part (the caller holds
        self._lock)."""
        return {"results": self._idempotent, "final": self._final,
                "journal": self._journal, "current": self._current,
                "cids": self._cids, "trade_cids": self._trade_cids,
                "levels": self._levels}

    def _save_state(self, wait: float | None = None):
        """This process's changes written to the shared file (see above).
        Every save carries everything changed so far, so one that finds a
        newer picture already written has nothing left to do."""
        path = self._state_path()
        if not path:
            return
        with self._lock:
            self._save_gen += 1
            gen = self._save_gen
            mine = {"accounts": _norm(self._accounts_cache),
                    "book_id": self._book_id, "book": {}}
            if self._book_id:
                mine["book"] = {k: _norm(v)
                                for k, v in self._book_parts().items()}
            self._unsaved = False
        if not self._save_lock.acquire(timeout=-1 if wait is None else wait):
            return
        try:
            if gen <= self._saved_gen:
                return
            base = self._disk_base
            with _FileLock(path + ".lock"):
                st = self._read_file() or {"version": 2, "accounts": {},
                                           "books": {}}
                _patch(st["accounts"], base.get("accounts") or {},
                       mine["accounts"])
                if mine["book_id"]:
                    book = st["books"].setdefault(mine["book_id"], {})
                    old = (base.get("book") or {}) if base.get(
                        "book_id") == mine["book_id"] else {}
                    for part, cur in mine["book"].items():
                        dst = book.get(part)
                        if not isinstance(dst, dict):
                            dst = book[part] = {}
                        _patch(dst, old.get(part) or {}, cur)
                        # (another process's additions are kept; never
                        # without bound)
                        cap = KEEP_RESULTS * (2 if part == "cids" else 1)
                        while len(dst) > cap + KEEP_RESULTS // 4:
                            dst.pop(next(iter(dst)))
                st["version"] = 2
                tmp = "%s.%d.tmp" % (path, os.getpid())
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                                 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(st, fh, default=str)
                    os.replace(tmp, path)
                except OSError as e:
                    log("state not written: %s" % type(e).__name__)
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    with self._lock:
                        self._unsaved = True
                    return
            if not mine["book_id"]:
                # (taken before the process was bound: what was read of the
                # account at binding stays the base of its next save)
                mine = dict(mine, book_id=base.get("book_id"),
                            book=base.get("book") or {})
            self._disk_base = mine
            self._saved_gen = gen
        finally:
            self._save_lock.release()

    @staticmethod
    def _trim(d: dict, keep: int = KEEP_RESULTS):
        while len(d) > keep:
            d.pop(next(iter(d)))

    def idempotent_store(self, client_id: str, result: dict,
                         signature=None) -> dict:
        """The SDK remembers results in memory; a restart is not a licence
        to act twice, so they are written down too (SPEC 2)."""
        out = super().idempotent_store(client_id, result, signature)
        with self._lock:
            self._trim(self._idempotent)
            self._unsaved = True
        self._save_state()
        return out

    def _bind_account(self, aid: str):
        """The memory of ONE account, read from the file when the process is
        bound to it (so it holds what any earlier process of the profile
        wrote, up to now). Another account's memory is never consulted: a
        profile bound elsewhere before must not replay that account's client
        ids or levels."""
        with self._lock:
            if self._book_id == aid:
                return

        def part(book, name, check=None):
            raw = book.get(name)
            return {str(k): v for k, v in (
                raw.items() if isinstance(raw, dict) else ())
                if check is None or check(v)}
        # (the save lock first, as in _save_state: no save runs between the
        # read and the memory it becomes)
        with self._save_lock:
            book = {}
            if self._state_path():
                with _FileLock(self._state_path() + ".lock"):
                    book = self._read_file().get("books", {}).get(aid) or {}
            with self._lock:
                self._book_id = aid
                self._idempotent.clear()
                self._idempotent.update(part(book, "results",
                                             lambda v: isinstance(v, dict)))
                self._final = part(book, "final", lambda v: isinstance(
                    v, dict) and v.get("code"))
                self._journal = part(book, "journal",
                                     lambda v: isinstance(v, dict))
                self._current = {k: str(v) for k, v in part(
                    book, "current").items()}
                self._cids = {k: str(v) for k, v in part(book,
                                                          "cids").items()}
                self._trade_cids = {k: str(v) for k, v in part(
                    book, "trade_cids").items()}
                self._levels = part(book, "levels",
                                    lambda v: isinstance(v, dict))
                self._disk_base = dict(self._disk_base, book_id=aid, book={
                    k: _norm(v) for k, v in self._book_parts().items()})

    # == catalog =========================================================
    def _load_catalog(self):
        rows = self._call("GET", self._path("/instruments")).get(
            "instruments") or []
        specs, own = {}, {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            built = build_instrument(r)
            if built is None:
                continue
            specs[built[0]["symbol"]] = built[0]
            own[built[0]["symbol"]] = built[1]
        if not specs:
            raise AdapterError("internal", "OANDA listed no instruments for "
                               "this account", retryable=True)
        with self._lock:
            before = set(self._catalog)
            self._catalog, self._inst = specs, own
            self._catalog_list = [specs[s] for s in sorted(specs)]
            self._catalog_at = time.monotonic()
        if before:
            gone, new = sorted(before - set(specs)), sorted(set(specs) - before)
            if gone:
                self.emit("catalog.changed", {"reason": "delisted",
                                              "symbols": gone})
            if new:
                self.emit("catalog.changed", {
                    "reason": "listed", "symbols": new,
                    "instruments": [specs[s] for s in new]})

    def handle_catalog_list(self, p):
        with self._lock:
            return {"instruments": [dict(s) for s in self._catalog_list]}

    def _instrument(self, symbol) -> tuple[dict, dict]:
        sym = str(symbol or "")
        with self._lock:
            spec, own = self._catalog.get(sym), self._inst.get(sym)
        if spec is None:
            raise AdapterError("unknown_symbol", "%r is not an instrument of "
                               "this OANDA account" % sym)
        return spec, own

    # == account =========================================================
    def _account_view(self, a: dict) -> dict:
        nav, used = _d0(a.get("NAV")), _d0(a.get("marginUsed"))
        out = {"balance": _fl(a.get("balance")), "equity": float(nav),
               "margin_used": float(used),
               "margin_free": _fl(a.get("marginAvailable")),
               "currency": str(a.get("currency") or self.account_currency),
               "account_id": self._acct,
               "unrealized_pnl": _fl(a.get("unrealizedPL"))}
        if a.get("alias"):
            out["label"] = str(a["alias"])
        if used > 0:
            out["margin_level"] = float(nav / used * 100)
        return out

    def handle_account_get(self, p):
        a = self._call("GET", self._path("/summary")).get("account") or {}
        with self._lock:
            self._summary, self._account_t = a, time.monotonic()
        view = self._account_view(a)
        with self._lock:
            self._account_sent = dict(view)
        return view

    # == the book: trades, positions =====================================
    def _trade_from(self, t: dict, keep: dict | None = None) -> dict:
        """One OANDA Trade (documented: trade-df) as the book keeps it."""
        tid = str(t.get("id"))
        ext = t.get("clientExtensions") or {}
        ocid = ext.get("id")
        rec = {"id": tid, "sym": str(t.get("instrument") or ""),
               "units": _d0(t.get("currentUnits")),
               "price": _d0(t.get("price")),
               "open_ms": to_ms(t.get("openTime")),
               "ocid": ocid, "cid": self._cid_for(ocid),
               "financing": _d0(t.get("financing")),
               "upl": _dec(t.get("unrealizedPL")),
               "commission": D0, "from": None,
               "sl": None, "tp": None, "trail": None,
               "sl_id": None, "tp_id": None, "tsl_id": None}
        for key, kind in (("stopLossOrder", "sl"), ("takeProfitOrder", "tp"),
                          ("guaranteedStopLossOrder", "sl")):
            o = t.get(key)
            if isinstance(o, dict) and o.get("state", "PENDING") == "PENDING":
                rec[kind] = _dec(o.get("price"))
                rec[kind + "_id"] = str(o.get("id"))
        o = t.get("trailingStopLossOrder")
        if isinstance(o, dict) and o.get("state", "PENDING") == "PENDING":
            rec["trail"] = _dec(o.get("distance"))
            rec["tsl_id"] = str(o.get("id"))
        if keep:
            rec["commission"] = keep.get("commission", D0)
            rec["from"] = keep.get("from")
            if not rec["cid"]:
                rec["cid"] = keep.get("cid")
        if not rec["cid"]:
            rec["cid"] = self._trade_cids.get(tid)
        return rec

    def _read_trades(self, kind: str = "read") -> tuple[dict, int]:
        r = self._call("GET", self._path("/openTrades"), kind=kind)
        last = int(r.get("lastTransactionID") or 0)
        rows = {}
        with self._lock:
            old = dict(self._trades)
        for t in r.get("trades") or []:
            if isinstance(t, dict) and t.get("id") is not None:
                rec = self._trade_from(t, old.get(str(t.get("id"))))
                rows[rec["id"]] = rec
        return rows, last

    def _sync_book(self, kind: str = "read") -> list:
        """OANDA's open trades and this adapter's book brought together
        without counting any transaction twice (SPEC 7: the broker is the
        truth; a newer list taken as the book, and the transactions it
        already held applied on top of it again, would close 600 units for a
        close of 200, and put a stop that had fired on the next position).
        The book is made of OANDA's transactions, each applied
        once. OANDA's list (GET .../openTrades, with its lastTransactionID)
        becomes the book only when it is of EXACTLY the transactions applied
        so far. One that holds transactions not applied yet is caught up
        first: they are read from OANDA's record and applied (their events go
        out). One older than the book refreshes the trades' P&L only. When
        the record cannot be read the list answers, and the book is left to
        catch up by itself. Returns the trades to answer with (copies)."""
        rows, last = self._read_trades(kind)
        with self._lock:
            behind = last > self._contig
        if behind:
            try:
                self._read_since("background" if kind == "background"
                                 else "read")
            except OandaError as e:
                log("the book could not be caught up with OANDA's record "
                    "(%s); OANDA's list of trades answers" % (
                        e.message or e.status))
        with self._lock:
            if self._contig == last and self._high == last:
                # The same point: OANDA's list is the book (anything this
                # adapter may have got wrong is put right), keeping what only
                # this adapter knows of each trade (its share of the opening
                # fill's commission, the order type it came from, the client
                # id of an MT4-linked trade), also of one the catch-up opened.
                for tid, rec in rows.items():
                    mine = self._trades.get(tid)
                    if mine is not None:
                        rec["commission"] = mine.get("commission", D0)
                        rec["from"] = mine.get("from")
                        if not rec.get("cid"):
                            rec["cid"] = mine.get("cid")
                self._trades = rows
                return [dict(t) for t in rows.values()]
            if self._contig >= last:
                # The book has applied newer transactions than the list.
                for tid, rec in rows.items():
                    mine = self._trades.get(tid)
                    if mine is not None:
                        mine["upl"] = rec["upl"]
                return [dict(t) for t in self._trades.values()]
            return [dict(t) for t in rows.values()]

    def _load_pending(self):
        """The orders resting at OANDA when the session opens: entry orders
        (ours when their client id is one this profile sent, so their fill or
        cancel is reported under the platform's ids) and the trades' stops
        and targets."""
        r = self._call("GET", self._path("/pendingOrders"))
        with self._lock:
            for o in r.get("orders") or []:
                if not isinstance(o, dict) or o.get("id") is None:
                    continue
                oid, typ = str(o["id"]), str(o.get("type") or "")
                if oid in self._orders:
                    continue
                if typ in ("TAKE_PROFIT", "STOP_LOSS", "GUARANTEED_STOP_LOSS",
                           "TRAILING_STOP_LOSS"):
                    self._orders[oid] = {"id": oid, "type": typ,
                                         "kind": "dependent", "root": oid,
                                         "trade": str(o.get("tradeID") or ""),
                                         "cid": None, "batch": ""}
                    continue
                ocid = (o.get("clientExtensions") or {}).get("id")
                mine = bool(ocid) and str(ocid) in self._cids
                self._orders[oid] = {
                    "id": oid, "sym": str(o.get("instrument") or ""),
                    "type": typ, "units": _d0(o.get("units")),
                    "price": _dec(o.get("price")),
                    "tif": o.get("timeInForce"),
                    "gtd_ms": to_ms(o.get("gtdTime")),
                    "created_ms": to_ms(o.get("createTime")),
                    "batch": "", "ocid": ocid,
                    "sl": _dec((o.get("stopLossOnFill") or {}).get("price")),
                    "tp": _dec((o.get("takeProfitOnFill") or {}).get("price")),
                    "trail": _dec((o.get("trailingStopLossOnFill") or {}).get(
                        "distance")),
                    "accepted": True, "done": False,
                    "root": self._root_of(oid),
                    "cid": (self._cids.get(str(ocid)) or None) if mine
                    else (ocid or None),
                    "kind": "user" if mine else "foreign"}

    def _levels_at_start(self):
        """At a start the broker is the truth (SPEC 7): a level an earlier run
        set that OANDA holds on NONE of the position's trades any more was
        taken off outside this connection, and is said, not put back. One
        still on some of them is still the user's, and goes on the rest (see
        _keep_levels)."""
        lost = []
        with self._lock:
            for sym in list(self._levels):
                ts = [t for t in self._trades.values() if t["sym"] == sym]
                lv = self._levels[sym]
                net = sum((t["units"] for t in ts), D0)
                side = "long" if net > 0 else "short"
                if not ts or net == 0 or lv.get("side") not in (None, side):
                    # No position, or one on the other side: the levels were
                    # another position's, over now.
                    self._levels.pop(sym, None)
                    self._unsaved = True
                    continue
                lv["side"] = side
                for k in ("sl", "tp", "trail"):
                    if lv.get(k) is not None and all(t[k] is None for t in ts):
                        lost.append((sym, k, lv[k]))
                        lv[k] = None
                if all(lv.get(k) is None for k in ("sl", "tp", "trail")):
                    self._levels.pop(sym, None)
            if lost:
                self._unsaved = True
        for sym, k, v in lost:
            self._say("gone:%s:%s" % (sym, k), "the %s of %s (%s) that was set "
                      "through this connection is no longer at OANDA; it was "
                      "taken off outside it and is not put back" % (
                          {"sl": "stop", "tp": "target",
                           "trail": "trailing stop"}[k], sym, v), ttl=120.0)

    def _net(self, sym: str) -> Decimal:
        with self._lock:
            return sum((t["units"] for t in self._trades.values()
                        if t["sym"] == sym), D0)

    def _cid_for(self, ocid) -> str | None:
        """The platform's client id for an OANDA client id: the one this
        profile mapped it from, else OANDA's own (an order placed elsewhere
        with an id of its own); None for an id of this adapter's own."""
        if not ocid:
            return None
        with self._lock:
            got = self._cids.get(str(ocid), str(ocid))
        return got or None

    @staticmethod
    def _common(values: list):
        vals = list(values)
        if not vals:
            return None, False
        first = vals[0]
        if all(v == first for v in vals):
            return first, False
        return None, True

    def _position_rows(self, trades: list, upl_of: dict | None = None) -> list:
        """The SPEC's positions from OANDA's trades (see the module
        docstring: a trade each on hedging, an instrument each on
        netting)."""
        out = []
        if self._hedging:
            for t in sorted(trades, key=lambda x: int(x["id"])):
                own = self._inst.get(t["sym"]) or {}
                tick = own.get("tick") or Decimal("0.00001")
                out.append({
                    "position_id": t["id"], "client_id": t.get("cid"),
                    "symbol": t["sym"],
                    "side": "buy" if t["units"] > 0 else "sell",
                    "qty": float(abs(t["units"])), "avg_price": float(t["price"]),
                    "sl": float(t["sl"]) if t["sl"] is not None else None,
                    "tp": float(t["tp"]) if t["tp"] is not None else None,
                    "trail_points": float(t["trail"] / tick) if t["trail"]
                    else 0,
                    "unrealized_pnl": float(t["upl"] or 0),
                    "swap": float(t["financing"]),
                    "commission": float(t.get("commission") or 0),
                    "opened_at": t.get("open_ms"), "from_order": t.get("from")})
            return out
        by: dict = {}
        for t in trades:
            by.setdefault(t["sym"], []).append(t)
        for sym in sorted(by):
            ts = sorted(by[sym], key=lambda x: int(x["id"]))
            units = sum((t["units"] for t in ts), D0)
            if units == 0:
                continue
            same = [t for t in ts if (t["units"] > 0) == (units > 0)]
            qty = abs(units)
            avg = (sum((t["price"] * abs(t["units"]) for t in same), D0)
                   / sum((abs(t["units"]) for t in same), D0))
            own = self._inst.get(sym) or {}
            tick = own.get("tick") or Decimal("0.00001")
            sl, mixed_sl = self._common([t["sl"] for t in ts])
            tp, mixed_tp = self._common([t["tp"] for t in ts])
            tr, mixed_tr = self._common([t["trail"] for t in ts])
            upl = (upl_of or {}).get(sym)
            if upl is None:
                upl = sum((t["upl"] or D0 for t in ts), D0)
            row = {"position_id": sym, "client_id": ts[0].get("cid"),
                   "symbol": sym, "side": "buy" if units > 0 else "sell",
                   "qty": float(qty), "avg_price": float(avg),
                   "sl": float(sl) if sl is not None else None,
                   "tp": float(tp) if tp is not None else None,
                   "trail_points": float(tr / tick) if tr else 0,
                   "unrealized_pnl": float(upl),
                   "swap": float(sum((t["financing"] for t in ts), D0)),
                   "commission": float(sum((t.get("commission") or D0
                                            for t in ts), D0)),
                   "opened_at": ts[0].get("open_ms"),
                   "from_order": ts[0].get("from"), "trades": len(ts)}
            if mixed_sl or mixed_tp or mixed_tr:
                # Not one level for the whole position: said, never guessed.
                row["levels"] = [{"trade_id": t["id"], "qty": float(abs(
                    t["units"])), "sl": float(t["sl"]) if t["sl"] is not None
                    else None, "tp": float(t["tp"]) if t["tp"] is not None
                    else None, "trail_points": float(t["trail"] / tick)
                    if t["trail"] else 0} for t in ts]
            out.append(row)
        return out

    def handle_positions_list(self, p):
        trades = self._sync_book()
        upl_of = None
        if not self._hedging and trades:
            # documented (sub-accounts PDF): a position's UPL is "based on
            # the sided price for the total aggregated notional amount", so
            # OANDA's own figure for the position, not the trades' sum.
            r = self._call("GET", self._path("/openPositions"))
            upl_of = {str(x.get("instrument")): _dec(x.get("unrealizedPL"))
                      for x in r.get("positions") or [] if isinstance(x, dict)}
        with self._lock:
            return {"positions": self._position_rows(trades, upl_of)}

    # == transactions in, events out =====================================
    def _ingest(self, txs):
        """Each transaction applied ONCE, in id order, from wherever it came
        (an answer, the stream, the gap filler)."""
        rows = [t for t in (txs or []) if isinstance(t, dict)
                and str(t.get("id") or "").isdigit()]
        rows.sort(key=lambda t: int(t["id"]))
        with self._lock:
            for tx in rows:
                n = int(tx["id"])
                if n <= self._contig or n in self._seen:
                    continue
                self._seen.add(n)
                if n > self._high:
                    self._high = n
                try:
                    self._apply(tx)
                except Exception as e:  # noqa: BLE001 - one bad row only
                    log("transaction %s (%s) not applied: %s: %s" % (
                        tx.get("id"), tx.get("type"), type(e).__name__, e))
            while (self._contig + 1) in self._seen:
                self._contig += 1
                self._seen.discard(self._contig)

    def _apply(self, tx: dict):
        t = str(tx.get("type") or "")
        if t in CREATE_TYPES:
            self._on_create(tx, CREATE_TYPES[t])
        elif t in DEPENDENT_TYPES:
            self._on_dependent(tx, DEPENDENT_TYPES[t])
        elif t == "ORDER_FILL":
            self._on_fill(tx)
        elif t == "ORDER_CANCEL":
            self._on_cancel(tx)
        elif t == "DAILY_FINANCING":
            self._on_financing(tx)
        elif t == "TRANSFER_FUNDS":
            self._on_operation(tx)
        elif t == "DIVIDEND_ADJUSTMENT":
            self._on_operation(tx)
        elif t in ("MARGIN_CALL_ENTER", "MARGIN_CALL_EXTEND"):
            data = {"stage": "call"}
            a = self._summary or {}
            nav, used = _d0(a.get("NAV")), _d0(a.get("marginUsed"))
            if used > 0:
                data["margin_level"] = float(nav / used * 100)
            self.emit("margin.warning", data)
        elif t == "TRADE_CLIENT_EXTENSIONS_MODIFY":
            tr = self._trades.get(str(tx.get("tradeID")))
            ext = tx.get("tradeClientExtensionsModify") or {}
            if tr is not None and ext.get("id"):
                tr["ocid"], tr["cid"] = ext["id"], self._cid_for(ext["id"])
        elif t == "CLOSE":
            self._say("closed", "OANDA has closed this account")

    def _intent_kind(self, tx: dict):
        """Whose closing order this is: the request that is on its way (filed
        before it left), else what OANDA says it is."""
        reason = str(tx.get("reason") or "")
        if reason == "MARGIN_CLOSEOUT":
            return "stop_out", None
        if reason == "DELAYED_TRADE_CLOSE":
            return "unknown", None
        tc = tx.get("tradeClose") or {}
        if tc.get("tradeID") is not None:
            got = self._intent_trade.get(str(tc["tradeID"]))
            if got:
                return got
        sym = str(tx.get("instrument") or "")
        got = self._intent_inst.get(sym)
        if got:
            return got
        return "foreign_close", None

    def _mine(self, ocid, tx: dict) -> tuple[bool, str | None]:
        """(sent by this adapter, the platform's client id or None)."""
        if ocid:
            if str(ocid) in self._cids:
                return True, self._cids[str(ocid)] or None
            return False, None
        if not self._mt4:
            return False, None
        # An MT4-linked account carries no client id: the order on its way
        # with the same instrument and units is ours.
        units = _dec(tx.get("units"))
        for key, j in self._journal.items():
            if (j.get("m") == "order.place" and j.get("sym") == tx.get(
                    "instrument") and _dec(j.get("units")) == units
                    and not j.get("oid")):
                j["oid"] = str(tx.get("id"))
                return True, j.get("cid") or None
        return False, None

    def _rec_for(self, oid: str, tx: dict) -> dict | None:
        """What is known of an order this session did not see made (one that
        was resting before it started): ours when its client id is one this
        profile sent."""
        rec = self._orders.get(oid)
        if rec is not None:
            return rec
        ocid = tx.get("clientOrderID")
        if not ocid or str(ocid) not in self._cids:
            return None
        rec = {"id": oid, "sym": str(tx.get("instrument") or ""),
               "type": "LIMIT", "root": self._root_of(oid),
               "cid": self._cids[str(ocid)] or None, "kind": "user",
               "accepted": True, "done": False, "batch": "", "tif": None}
        self._orders[oid] = rec
        return rec

    def _expires(self, rec: dict):
        if rec.get("tif") == "GTD":
            return rec.get("gtd_ms")
        if rec.get("tif") == "GFD" and rec.get("created_ms"):
            return next_new_york_five(rec["created_ms"])
        return None

    def _accept(self, rec: dict):
        if rec.get("accepted"):
            return
        rec["accepted"] = True
        self.emit("order.accepted", {"client_id": rec.get("cid"),
                                     "order_id": rec["root"]})

    def _on_create(self, tx: dict, typ: str):
        oid = str(tx["id"])
        ext = tx.get("clientExtensions") or {}
        ocid = ext.get("id")
        sl = (tx.get("stopLossOnFill") or {}).get("price")
        tp = (tx.get("takeProfitOnFill") or {}).get("price")
        trail = (tx.get("trailingStopLossOnFill") or {}).get("distance")
        rec = {"id": oid, "sym": str(tx.get("instrument") or ""),
               "type": typ, "units": _d0(tx.get("units")),
               "price": _dec(tx.get("price")), "tif": tx.get("timeInForce"),
               "gtd_ms": to_ms(tx.get("gtdTime")),
               "created_ms": to_ms(tx.get("time")),
               "batch": str(tx.get("batchID") or oid), "ocid": ocid,
               "sl": _dec(sl), "tp": _dec(tp), "trail": _dec(trail),
               "accepted": False, "done": False, "root": oid, "cid": None,
               "kind": "foreign"}
        reason = str(tx.get("reason") or "")
        replaces = tx.get("replacesOrderID")
        if replaces is not None:
            prev = self._orders.get(str(replaces)) or {}
            root = prev.get("root") or self._root_of(str(replaces))
            rec.update(root=root, cid=prev.get("cid") or self._cid_for(ocid),
                       kind=prev.get("kind") or "foreign", accepted=True)
            self._current[root] = oid
            self._unsaved = True
            self._orders[oid] = rec
            if rec["kind"] == "user":
                d = {"order_id": root}
                if rec["price"] is not None:
                    d["price"] = float(rec["price"])
                d["qty"] = float(abs(rec["units"]))
                d["sl"] = float(rec["sl"]) if rec["sl"] is not None else None
                d["tp"] = float(rec["tp"]) if rec["tp"] is not None else None
                d["expires_at"] = self._expires(rec)
                self.emit("order.amended", d)
            return
        if typ == "MARKET" and reason in ("TRADE_CLOSE", "POSITION_CLOSEOUT",
                                          "MARGIN_CLOSEOUT",
                                          "DELAYED_TRADE_CLOSE"):
            kind, cid = self._intent_kind(tx)
            rec.update(kind=kind, cid=cid)
            self._orders[oid] = rec
            return
        mine, cid = self._mine(ocid, tx)
        if mine:
            rec.update(kind="user", cid=cid)
        else:
            rec.update(kind="foreign", cid=ocid or None)
        self._orders[oid] = rec
        if rec["kind"] == "user":
            self._accept(rec)
            if typ in ENTRY_TYPES:
                self.emit("order.placed", {
                    "client_id": rec["cid"], "order_id": rec["root"],
                    "type": SPEC_TYPE.get(typ, typ.lower()),
                    "price": float(rec["price"]) if rec["price"] is not None
                    else None, "expires_at": self._expires(rec)})

    def _root_of(self, oid: str) -> str:
        for root, cur in self._current.items():
            if cur == oid:
                return root
        return oid

    def _on_dependent(self, tx: dict, kind: str):
        oid = str(tx["id"])
        tid = str(tx.get("tradeID") or "")
        self._orders[oid] = {"id": oid, "type": kind, "kind": "dependent",
                             "trade": tid, "root": oid, "cid": None,
                             "batch": str(tx.get("batchID") or oid)}
        tr = self._trades.get(tid)
        if tr is None:
            return
        if kind == "trail":
            tr["trail"], tr["tsl_id"] = _dec(tx.get("distance")), oid
            self._trailed.pop(tid, None)
        else:
            price = _dec(tx.get("price"))
            if price is None and tx.get("distance") is not None:
                d = _dec(tx.get("distance"))
                price = (tr["price"] - d) if (tr["units"] > 0) == (
                    kind == "sl") else (tr["price"] + d)
            tr[kind], tr[kind + "_id"] = price, oid

    def _clear_dependent(self, oid: str):
        for tr in self._trades.values():
            for kind in ("sl", "tp"):
                if tr.get(kind + "_id") == oid:
                    tr[kind], tr[kind + "_id"] = None, None
            if tr.get("tsl_id") == oid:
                tr["trail"], tr["tsl_id"] = None, None

    def _on_cancel(self, tx: dict):
        oid = str(tx.get("orderID") or "")
        reason = str(tx.get("reason") or "")
        rec = self._rec_for(oid, tx)
        if rec is None or rec.get("kind") == "dependent":
            self._clear_dependent(oid)
            return
        rec["done"] = True
        if reason == "CLIENT_REQUEST_REPLACED" or rec.get("kind") != "user":
            return
        root, cid = rec["root"], rec.get("cid")
        code, like, _ = map_reason(reason)
        text = "OANDA cancelled the order: %s" % reason
        # documented: transaction-df, every transaction carries the batchID
        # of the request that made it; assumed, verify on demo: an order
        # OANDA kills at once is cancelled in its own creation's batch.
        if str(tx.get("batchID") or "") == rec.get("batch"):
            # Cancelled in the very request that made it (a market order
            # OANDA could not fill, a resting one it would not take): the
            # order never was; this is its one terminal event. Unless the
            # refusal is one to retry (a requote, a thin book): then, as in
            # the reference adapter, it is no end, and nothing is announced;
            # the call's answer says it.
            if map_reason(reason)[2]:
                return
            d = {"client_id": cid, "code": code, "message": text,
                 "order_id": root}
            if like:
                d["like"] = like
            self.emit("order.rejected", d)
        elif reason == "TIME_IN_FORCE_EXPIRED":
            self.emit("order.expired", {
                "order_id": root, "client_id": cid,
                "expiry": EXPIRY_OF.get(str(rec.get("tif")), "gtd")})
        elif reason == "CLIENT_REQUEST":
            self.emit("order.cancelled", {"order_id": root, "client_id": cid})
        else:
            d = {"order_id": root, "client_id": cid, "code": code,
                 "message": text}
            self.emit("order.cancelled", d)

    def _on_fill(self, tx: dict):
        oid = str(tx.get("orderID") or "")
        rec = self._rec_for(oid, tx)
        sym = str(tx.get("instrument") or "")
        units = _d0(tx.get("units"))
        price = _dec(tx.get("fullVWAP")) or _dec(tx.get("price")) or D0
        t_ms = to_ms(tx.get("time")) or self.now_ms()
        # documented: transaction-df OrderFillTransaction commission "is
        # always represented as a positive quantity of the Account's home
        # currency, however it reduces the balance"; guaranteedExecutionFee,
        # "the total guaranteed execution fees charged" (its sign not
        # stated: assumed, verify on demo, read as a charge either way).
        fees = -(abs(_d0(tx.get("commission")))
                 + abs(_d0(tx.get("guaranteedExecutionFee"))))
        reason = str(tx.get("reason") or "")
        kind = (rec or {}).get("kind") or "foreign"
        if reason == "TAKE_PROFIT_ORDER":
            kind = "tp"
        elif reason in ("STOP_LOSS_ORDER", "GUARANTEED_STOP_LOSS_ORDER",
                        "TRAILING_STOP_LOSS_ORDER"):
            kind = "sl"
        elif reason == "MARKET_ORDER_MARGIN_CLOSEOUT":
            kind = "stop_out"
        elif reason.startswith("FIXED_PRICE_ORDER") or reason == \
                "MARKET_ORDER_DELAYED_TRADE_CLOSE":
            kind = "unknown"
        elif kind == "dependent":
            kind = "unknown"
        if rec is not None:
            rec["done"] = True
        closes = list(tx.get("tradesClosed") or [])
        if isinstance(tx.get("tradeReduced"), dict):
            closes.append(tx["tradeReduced"])
        opened = tx.get("tradeOpened") if isinstance(
            tx.get("tradeOpened"), dict) else None
        total = abs(units) or sum((abs(_d0(c.get("units"))) for c in closes),
                                  D0) + (abs(_d0(opened.get("units")))
                                         if opened else D0)
        net_before = self._net(sym)
        cid_before = None
        if not self._hedging:
            ts = sorted((t for t in self._trades.values() if t["sym"] == sym),
                        key=lambda x: int(x["id"]))
            cid_before = ts[0].get("cid") if ts else None
        closed = []
        for c in closes:
            tid = str(c.get("tradeID") or "")
            # documented: TradeReduce.units "the number of units that the
            # Trade was reduced by", no sign stated; the trade's own side
            # comes from the book, so only the size is read here.
            cu = abs(_d0(c.get("units")))
            tr = self._trades.get(tid)
            info = {"tid": tid, "units": cu,
                    "price": _dec(c.get("price")) or price,
                    "pl": _d0(c.get("realizedPL")),
                    "fin": _d0(c.get("financing")), "fin0": D0, "comm0": D0,
                    "cid": tr.get("cid") if tr else None,
                    "long": (tr["units"] > 0) if tr else (units < 0)}
            if tr is not None and tr["units"] != 0:
                frac = min(Decimal(1), cu / abs(tr["units"]))
                info["fin0"] = tr["financing"] * frac
                info["comm0"] = (tr.get("commission") or D0) * frac
                left = abs(tr["units"]) - cu
                if left <= 0:
                    del self._trades[tid]
                    self._trailed.pop(tid, None)
                else:
                    tr["units"] = left if tr["units"] > 0 else -left
                    tr["financing"] -= info["fin0"]
                    tr["commission"] = (tr.get("commission") or D0) - \
                        info["comm0"]
            closed.append(info)
        new_tid = None
        if opened:
            new_tid = str(opened.get("tradeID") or "")
            ext = opened.get("clientExtensions") or {}
            ou = abs(_d0(opened.get("units")))
            share = fees * (ou / total) if total else fees
            cid_new = self._cid_for(ext.get("id")) if ext.get("id") else (
                (rec or {}).get("cid") if kind == "user" else None)
            if cid_new and not ext.get("id"):
                self._trade_cids[new_tid] = cid_new
                self._trim(self._trade_cids)
                self._unsaved = True
            self._trades[new_tid] = {
                "id": new_tid, "sym": sym, "units": _d0(opened.get("units")),
                "price": _dec(opened.get("price")) or price, "open_ms": t_ms,
                "ocid": ext.get("id"), "cid": cid_new, "financing": D0,
                "upl": None, "commission": share,
                "from": FROM_ORDER.get(reason), "sl": None, "tp": None,
                "trail": None, "sl_id": None, "tp_id": None, "tsl_id": None}
        side = "buy" if units > 0 else "sell"
        root = (rec or {}).get("root") or oid
        if kind in ("user", "foreign") or opened:
            if self._hedging:
                pid = new_tid or (closed[0]["tid"] if closed else None)
            else:
                pid = sym
            cid = (rec or {}).get("cid") if kind in ("user", "foreign") \
                else None
            fill = {"fill_id": str(tx["id"]), "client_id": cid,
                    "order_id": root, "position_id": pid, "symbol": sym,
                    "side": side, "qty": float(total), "price": float(price),
                    "fees": float(fees), "time": t_ms}
            if kind == "user" and rec is not None:
                self._accept(rec)
            self.emit("order.filled", fill)
            if kind not in ("user", "foreign"):
                self._say("opened:" + sym, "an order that was not a new "
                          "order (%s) filled %s %s and OPENED a position; "
                          "look at %s now" % (reason, side, sym, sym))
        if closed:
            rsn = {"user": "manual", "foreign": "manual", "close": "manual",
                   "flatten": "flatten", "sl": "sl", "tp": "tp",
                   "stop_out": "stop_out",
                   "foreign_close": "manual"}.get(kind, "unknown")
            detail = None
            if kind == "foreign":
                detail = "an order placed outside this connection (OANDA: " \
                    "%s)" % reason
            elif kind == "foreign_close":
                detail = "closed outside this connection (OANDA: %s)" % reason
            elif kind == "unknown":
                detail = "OANDA: %s" % (reason or "no reason given")
            elif reason == "TRAILING_STOP_LOSS_ORDER":
                detail = "trailing stop"
            closed_units = sum((c["units"] for c in closed), D0)
            fee_share = fees * (closed_units / total) if total else D0
            groups = ([[c] for c in closed] if self._hedging else [closed])
            for g in groups:
                q = sum((c["units"] for c in g), D0)
                if q <= 0:
                    continue
                px = sum((c["price"] * c["units"] for c in g), D0) / q
                evt = {"close_id": ("c-%s-%s" % (tx["id"], g[0]["tid"])
                                    if self._hedging else "c-%s" % tx["id"]),
                       "position_id": g[0]["tid"] if self._hedging else sym,
                       "client_id": g[0]["cid"] if self._hedging
                       else cid_before,
                       "qty": float(q), "price": float(px),
                       "realized_pnl": float(sum((c["pl"] for c in g), D0)),
                       "swap": float(sum((c["fin"] + c["fin0"] for c in g),
                                         D0)),
                       "commission": float(sum((c["comm0"] for c in g), D0)
                                           + fee_share * q / closed_units),
                       "reason": rsn, "time": t_ms}
                if detail:
                    evt["reason_detail"] = detail
                self.emit("position.closed", evt)
        if not self._hedging:
            net_after = self._net(sym)
            if net_before != 0 and (net_after == 0
                                    or (net_after > 0) != (net_before > 0)):
                # The position is over (flat, or reversed in one fill): its
                # levels are over with it.
                if self._levels.pop(sym, None) is not None:
                    self._unsaved = True
            if net_after != 0 and (net_before == 0 or (net_after > 0) != (
                    net_before > 0)):
                # A NEW position: no level remembered for an earlier one is
                # ever its own (a stop that had fired, put on the next
                # position, would close it at once).
                if self._levels.pop(sym, None) is not None:
                    self._unsaved = True
                if (opened and rec is not None and kind == "user"
                        and any(rec.get(k) is not None
                                for k in ("sl", "tp", "trail"))):
                    # Opened by an order that carried levels: those are the
                    # position's.
                    self._levels[sym] = {
                        "sl": str(rec["sl"]) if rec.get("sl") is not None
                        else None,
                        "tp": str(rec["tp"]) if rec.get("tp") is not None
                        else None,
                        "trail": str(rec["trail"]) if rec.get("trail")
                        is not None else None,
                        "side": "long" if net_after > 0 else "short"}
                    self._unsaved = True
            if opened and sym in self._levels:
                self._levels_t = 0.0          # look at its levels at once
                self._wake.set()
        if kind == "stop_out":
            self.emit("margin.warning", {"stage": "stop_out"})
        self._account_dirty = True
        self._wake.set()

    def _on_financing(self, tx: dict):
        # documented: transaction-df DailyFinancingTransaction
        # positionFinancings[].openTradeFinancings[] {tradeID, financing};
        # assumed, verify on demo: negative is paid by the account.
        for pf in tx.get("positionFinancings") or []:
            for of in (pf or {}).get("openTradeFinancings") or []:
                tr = self._trades.get(str((of or {}).get("tradeID")))
                if tr is not None:
                    tr["financing"] += _d0(of.get("financing"))
        self._on_operation(tx)

    def _operation(self, tx: dict) -> dict | None:
        """A transaction that moved money without a trade, as an Operation
        (SPEC 4), or None."""
        t = str(tx.get("type") or "")
        ccy = self.account_currency
        if t == "TRANSFER_FUNDS":
            amount = _d0(tx.get("amount"))
            why = str(tx.get("fundingReason") or "")
            # documented: transaction-df FundingReason
            kind = ("adjustment" if why == "ADJUSTMENT" else "deposit"
                    if amount >= 0 else "withdrawal")
            if why in ("DIVISION_MIGRATION", "SITE_MIGRATION"):
                kind = "other"
            comment = {"ACCOUNT_TRANSFER": "transfer between accounts",
                       "CLIENT_FUNDING": "funding", "ADJUSTMENT":
                       "account adjustment"}.get(why, why.lower() or "funding")
            if tx.get("comment"):
                comment += ": " + str(tx["comment"])[:120]
            ref = str(tx.get("id"))
        elif t == "DAILY_FINANCING":
            amount = _d0(tx.get("financing"))
            kind, ref = "other", None
            comment = "OANDA's daily financing of the open positions"
        elif t == "DIVIDEND_ADJUSTMENT":
            amount = _d0(tx.get("dividendAdjustment"))
            kind, ref = "dividend", str(tx.get("instrument") or "") or None
            comment = "dividend adjustment"
        else:
            return None
        return {"operation_id": str(tx.get("id")), "kind": kind,
                "amount": float(amount), "currency": ccy,
                "balance_after": _fl(tx.get("accountBalance")),
                "reference": ref, "comment": comment,
                "time": to_ms(tx.get("time"))}

    def _on_operation(self, tx: dict):
        op = self._operation(tx)
        if op is not None:
            self.emit("balance.operation", op)
            self._account_dirty = True

    # == orders: placing =================================================
    def _check_qty(self, own: dict, qty) -> Decimal:
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise AdapterError("invalid_qty", "qty must be a number")
        q = float(qty)
        step = float(own["step"])
        if not (q > 0) or not step_ok(q, step):
            raise AdapterError("invalid_qty", "qty %s is not a whole number "
                               "of steps of %s units" % (qty, own["step"]))
        d = snap(q, own["step"])
        if d < own["min"]:
            raise AdapterError("invalid_qty", "qty %s is below OANDA's "
                               "minimum of %s units" % (qty, own["min"]))
        if d > own["max"]:
            raise AdapterError("invalid_qty", "qty %s is above OANDA's "
                               "largest order of %s units" % (qty, own["max"]))
        return d

    def _check_price(self, own: dict, v, field: str) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (
                v > 0) or v != v or v == math.inf:
            code = "invalid_stops" if field in ("sl", "tp") else \
                "x_oanda_price_invalid"
            raise AdapterError(code, "%s must be a positive price" % field)
        tick = own["tick"]
        n = Decimal(repr(float(v))) / tick
        if abs(n - n.to_integral_value()) > Decimal("0.0001"):
            # SPEC 6.1: a price off the tick grid is neither a quantity nor
            # a stop distance; OANDA's own reason is PRICE_PRECISION_EXCEEDED
            raise AdapterError(
                "x_oanda_price_precision", "%s %s has more decimals than "
                "this instrument's %d" % (field, v, own["dp"]),
                like="invalid_stops" if field in ("sl", "tp") else None)
        return snap(float(v), tick)

    def _check_trail(self, own: dict, v) -> Decimal | None:
        if v is None or v == 0:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (
                v > 0):
            raise AdapterError("invalid_stops", "trail_points must be a "
                               "positive number of ticks")
        # SPEC trail_points are ticks; OANDA's distance is in price units.
        dist = (_d0(v) * own["tick"]).quantize(own["tick"],
                                                rounding=decimal.ROUND_HALF_EVEN)
        lo, hi = own.get("min_trail"), own.get("max_trail")
        # documented: primitives-df minimum/maximumTrailingStopDistance
        if (lo is not None and dist < lo) or (hi is not None and dist > hi):
            raise AdapterError(
                "invalid_stops", "a trailing distance of %s is outside "
                "OANDA's %s to %s for this instrument" % (
                    dist, lo if lo is not None else "-",
                    hi if hi is not None else "-"))
        return dist

    @staticmethod
    def _check_levels(long: bool, sl, tp):
        if sl is not None and tp is not None and (sl >= tp) == long:
            raise AdapterError(
                "invalid_stops", "the stop of a %s position must be %s its "
                "target" % ("long" if long else "short",
                            "below" if long else "above"))

    def _expiry(self, p: dict, typ: str):
        if typ == "market":
            return "FOK", None
        expiry = p.get("expiry") or "gtc"
        if expiry not in TIF_OF:
            raise AdapterError("invalid_expiry", "expiry must be gtc, day or "
                               "gtd")
        gtd = None
        if expiry == "gtd":
            at = p.get("expiry_at")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                raise AdapterError("invalid_expiry", "gtd needs expiry_at")
            gtd = int(at)
            if gtd <= self.now_ms():
                raise AdapterError("invalid_expiry", "expiry_at is in the "
                                   "past")
        return TIF_OF[expiry], gtd

    def _levels_now(self, sym: str) -> dict | None:
        with self._lock:
            lv = self._levels.get(sym)
            return dict(lv) if lv else None

    def _attach(self, sym: str, units: Decimal, sl, tp, trail,
                typ: str) -> dict:
        """The levels an order carries (netting rules in the docstring)."""
        out = {"sl": sl, "tp": tp, "trail": trail}
        if self._hedging:
            return out
        held = self._net(sym)
        asked = any(v is not None for v in (sl, tp, trail))
        same = held != 0 and (held > 0) == (units > 0)
        reduces_only = held != 0 and not same and abs(units) <= abs(held)
        if asked and (same or reduces_only) and typ == "market":
            raise AdapterError(
                "x_oanda_position_open", "there is already a %s position in "
                "%s: set its stop and target with order.modify"
                % ("long" if held > 0 else "short", sym),
                like="trade_restricted")
        if not asked and same and typ == "market":
            lv = self._levels_now(sym)
            if lv:
                # It adds to the position: it carries the position's levels,
                # so all of the position stays protected as the user set it.
                out = {"sl": _dec(lv.get("sl")), "tp": _dec(lv.get("tp")),
                       "trail": _dec(lv.get("trail"))}
        return out

    def _order_body(self, own: dict, sym: str, typ: str, units: Decimal,
                    price, tif: str, gtd, levels: dict, ocid) -> dict:
        o = {"type": {"market": "MARKET", "limit": "LIMIT",
                      "stop": "STOP"}[typ],
             "instrument": sym, "units": _fmt(units, own["tup"]),
             "timeInForce": tif,
             # documented: order-df OrderPositionFill DEFAULT: REDUCE_FIRST on
             # a netting account, OPEN_ONLY on a hedging one
             "positionFill": "DEFAULT"}
        if typ != "market":
            o["price"] = _fmt(price, own["dp"])
            o["triggerCondition"] = "DEFAULT"
            if gtd is not None:
                o["gtdTime"] = unix_s(gtd)
        if ocid and not self._mt4:
            o["clientExtensions"] = {"id": ocid}
            o["tradeClientExtensions"] = {"id": ocid}
        if levels.get("sl") is not None:
            o["stopLossOnFill"] = {"price": _fmt(levels["sl"], own["dp"]),
                                   "timeInForce": "GTC"}
        if levels.get("tp") is not None:
            o["takeProfitOnFill"] = {"price": _fmt(levels["tp"], own["dp"]),
                                     "timeInForce": "GTC"}
        if levels.get("trail") is not None:
            o["trailingStopLossOnFill"] = {
                "distance": _fmt(levels["trail"], own["dp"]),
                "timeInForce": "GTC"}
        return {"order": o}

    def _journal_put(self, key: str, entry: dict):
        with self._lock:
            entry.setdefault("at", self.now_ms())
            # Where OANDA's record of this request starts (_lookup,
            # _earlier_change): the highest transaction id seen, not the
            # contiguous mark. Every transaction seen was in the record
            # before this request left, so none of them is its own; from the
            # contiguous mark, with one transaction not yet seen before an
            # answered close A, A's transactions would be in a later close
            # B's window, and B (lost, never carried out) would be answered
            # with A's. A transaction not seen yet below this mark is the gap
            # filler's, as any other.
            entry.setdefault("before", self._high)
            self._journal[key] = entry
            self._unsaved = True
        self._save_state()

    def _journal_drop(self, key: str):
        with self._lock:
            if self._journal.pop(key, None) is not None:
                self._unsaved = True
        self._unsay("unknown:" + key)

    @staticmethod
    def _fate_unknown(text: str, change: bool = False,
                      what: str = "") -> AdapterError:
        """The answer to a request (`what`: "the order", "the cancel"...)
        whose fate is not known yet, `text` saying why. The words that say
        the outcome is not known come first, so a display cut short (a
        ticket shows 80 characters) still says it."""
        if change:
            # an amend or a cancel (see _earlier_change)
            tail = ("Asking again with the same client_id is safe: OANDA's "
                    "record is read for it first, and it names one order, so "
                    "it cannot be carried out twice.")
        else:
            tail = ("It is NOT sent again: asking again with the same "
                    "client_id is safe, and is answered from OANDA's own "
                    "record (watched for %d s after the answer was lost)."
                    % UNKNOWN_WATCH)
        head = ("Not known yet whether OANDA carried out %s" % what if what
                else "Not known yet whether OANDA carried it out")
        err = AdapterError(
            "internal", "%s: %s. %s" % (head, text.rstrip("."), tail),
            retryable=True)
        err.unknown = True
        return err

    @staticmethod
    def _what(j: dict) -> str:
        """A request of the journal in words, for the user: what it does,
        never the platform's client_id, an id of the platform's own that the
        user never typed (as in "The close lset-close-9a9771e29b95 of
        EUR_USD may have been carried out")."""
        sym = j.get("sym") or "its instrument"

        def size(u):
            return _fmt(abs(u), 4).rstrip("0").rstrip(".")

        if j.get("m") == "order.place":
            u = _dec(j.get("units")) or D0
            return "the order to %s %s %s" % ("buy" if u > 0 else "sell",
                                              size(u), sym)
        # What the close asked OANDA for: a number of units, or "ALL" (the
        # whole position); an entry an earlier version wrote holds neither.
        u = _dec(j.get("units"))
        if j.get("trade"):
            if u:
                return "the close of %s %s of trade %s" % (size(u), sym,
                                                          j["trade"])
            return "the close of trade %s (%s)" % (j["trade"], sym)
        return "the close of %s%s" % (size(u) + " " if u else "", sym)

    @staticmethod
    def _sent_at(j: dict) -> str:
        """ " sent at HH:MM:SS UTC", when the journal says when it left."""
        at = int(j.get("at") or 0)
        return (" sent at %s UTC" % time.strftime("%H:%M:%S", time.gmtime(
            at / 1000)) if at else "")

    def _say_unknown(self, key: str | None = None):
        """SPEC 8.4: an order not yet at its end within the platform's wait
        is said (session.state degraded) while its fate is not known."""
        with self._lock:
            rows = [(k, dict(j)) for k, j in self._journal.items()
                    if (key is None or k == key) and j.get("m") in (
                        "order.place", "position.close")]

        def hms(ms):
            return time.strftime("%H:%M:%S", time.gmtime(ms / 1000))

        for k, j in rows:
            # `at` is when it was written down, just before it was sent;
            # `lost_at` when its answer was given up on (none for a request
            # that was on the wire when an earlier run stopped). The watch
            # runs from the loss (_watch_unknown). The time given up is never
            # said as the time sent.
            sent = int(j.get("at") or self.now_ms())
            lost = int(j.get("lost_at") or 0)
            when = ("was sent at %s UTC; OANDA's answer was lost at %s UTC"
                    % (hms(sent), hms(lost)) if lost else
                    "was sent at %s UTC and OANDA's answer was lost"
                    % hms(sent))
            self._say("unknown:" + k, "%s %s: it is NOT sent again; OANDA's "
                      "record is read for it until %s UTC" % (
                          self._what(j), when,
                          hms((lost or sent) + UNKNOWN_WATCH * 1000)))

    def _lost(self, key: str, e: OandaError) -> bool:
        """Whether a failed write's fate is unknown (and so written down as
        such). A refusal OANDA itself gave, or a request never sent, is
        known."""
        unsaid = e.sent and (e.status == 0 or e.status >= 500
                             or e.status == 408 or e.code == "lost")
        if unsaid:
            with self._lock:
                j = self._journal.get(key)
                if j is not None:
                    j["unknown"] = True
                    j["lost_at"] = self.now_ms()
                    self._unsaved = True
            self._save_state()
            self._say_unknown(key)
        else:
            self._journal_drop(key)
        return unsaid

    def _give_up(self, key: str, j: dict, unmarked=()) -> AdapterError:
        """An order or close whose answer was lost, and of which OANDA's
        record shows nothing UNKNOWN_WATCH after the loss: OANDA did not carry
        it out, `x_oanda_not_carried_out`. That is final for its client_id
        (the answer to every retry), and an order's one terminal event is
        `order.rejected`. It is not sent again: placing it again is a new
        request under a new client_id, the platform's decision. Said once,
        whichever of the retry and the backstop comes to it first.

        A close whose position OANDA's record shows closed or reduced since
        it was sent by anything not marked as it (`unmarked`, _closed_since:
        a closeout of its instrument, its stop, its target, another tool's
        close of its trade) is never said not to have been carried out: an
        unmarked closeout may be this one, carried out with its marks lost,
        and whatever else it is, what the close was for may be done (a
        hedging trade a closeout had closed is not said not closed). Said is
        what the record shows, final, `x_oanda_close_unmarked`."""
        what = self._what(j)
        what = what[0].upper() + what[1:] + self._sent_at(j)
        if unmarked:
            err = AdapterError(
                "x_oanda_close_unmarked", "%s may have been carried out: "
                "OANDA's answer was lost, and OANDA's record shows %s. It was "
                "not sent again: check Positions before closing it again."
                % (what, self._shows(unmarked)), like="internal")
        else:
            err = AdapterError(
                "x_oanda_not_carried_out", "%s was not carried out: OANDA's "
                "answer was lost and OANDA's record shows nothing of it %d s "
                "later. It was not sent again; send it as a new order (a new "
                "client_id) if it is still wanted." % (what, UNKNOWN_WATCH),
                like="internal")
        cid = j.get("cid") or ""
        with self._lock:
            mine = self._journal.pop(key, None) is not None
            if mine and cid and j.get("sig"):
                self._final[cid] = {"sig": j["sig"], "code": err.code,
                                    "message": str(err), "like": err.like,
                                    "at": self.now_ms()}
                self._trim(self._final)
            self._unsaved = True
        if mine:
            log("%s: %s %d s after its answer was lost; final" % (
                key, "a close of its position not marked as it in OANDA's "
                "record" if unmarked else "nothing in OANDA's record",
                UNKNOWN_WATCH))
            if j.get("m") == "order.place":
                self._rejected(cid, err)
            self._save_state()
        self._unsay("unknown:" + key)
        return err

    @staticmethod
    def _shows(items: list) -> str:
        """What OANDA's record shows (_closed_since), as words: the first
        three, and how many more."""
        words = [i["text"] for i in items[:3]]
        if len(items) > 3:
            words.append("%d more" % (len(items) - 3))
        if len(words) == 1:
            return words[0]
        return "; ".join(words[:-1]) + "; and " + words[-1]

    # What closed a trade, by the fill's reason (documented: transaction-df
    # OrderFillReason: "The Order filled was a Stop Loss Order", ... "a
    # Market Order used for a Margin Closeout", "... for a delayed Trade
    # close").
    _LEVEL_FILLS = {"STOP_LOSS_ORDER": "stop loss",
                    "GUARANTEED_STOP_LOSS_ORDER": "guaranteed stop loss",
                    "TRAILING_STOP_LOSS_ORDER": "trailing stop",
                    "TAKE_PROFIT_ORDER": "take profit"}

    def _closed_since(self, entry: dict, txs: list) -> list:
        """What OANDA's record (`txs`, the transactions since a close whose
        answer was lost was sent) shows closed or reduced what that close was
        for, by anything not marked as it: every fill that closed or reduced
        its trade (hedging), or a trade of its instrument (netting). A
        closeout of the position, a stop, a target or a trailing stop, a
        margin closeout, another tool's close of a trade, an order that
        reduced it. Only a fill counts: a closeout OANDA cancelled (its
        ORDER_CANCEL, MARKET_HALTED and the like) closed nothing, and is not
        said as a close that may be this one.

        Each item is {"text": the words, "maybe": whether it may be this
        close itself}. Only an unmarked closeout of a netting position may
        be (its marks lost): a hedging close is a trade close, found by its
        trade (_lookup), and one marked as another request, a level and a
        trade close are not this one, whatever they closed."""
        sym, trade = str(entry.get("sym") or ""), entry.get("trade")
        creates = {str(t.get("id")): t for t in txs
                   if str(t.get("type") or "") in CREATE_TYPES}
        items = []
        for f in txs:
            if f.get("type") != "ORDER_FILL":
                continue
            shut = [c for c in f.get("tradesClosed") or []
                    if isinstance(c, dict)]
            cut = (f.get("tradeReduced")
                   if isinstance(f.get("tradeReduced"), dict) else None)
            if trade:
                if any(str(c.get("tradeID")) == str(trade) for c in shut):
                    verb = "closed"
                elif cut is not None and str(cut.get("tradeID")) == str(trade):
                    verb = "reduced"
                else:
                    continue
            elif str(f.get("instrument") or "") != sym or not (shut or cut):
                continue
            create = creates.get(str(f.get("orderID") or "")) or {}
            reason = str(f.get("reason") or "")
            made_for = str(create.get("reason") or "")
            mark = ((create.get("clientExtensions") or {}).get("id")
                    or f.get("clientOrderID"))
            ms = to_ms(f.get("time"))
            at = (" at %s UTC" % time.strftime("%H:%M:%S", time.gmtime(
                ms / 1000)) if ms else "")
            maybe = False
            if (reason == "MARKET_ORDER_POSITION_CLOSEOUT"
                    or made_for == "POSITION_CLOSEOUT"
                    or create.get("longPositionCloseout")
                    or create.get("shortPositionCloseout")):
                what = "a close of %s%s" % (sym, at)
                if mark:
                    what += ", marked as another request"
                elif trade:
                    what += ", not marked as this request"
                else:
                    what += (", not marked as this request, so it may be "
                             "this one")
                    maybe = True
                text = ("trade %s %s by %s" % (trade, verb, what) if trade
                        else what)
            elif reason in self._LEVEL_FILLS:
                level = self._LEVEL_FILLS[reason]
                text = ("trade %s %s by its %s%s" % (trade, verb, level, at)
                        if trade else "a %s of %s filled%s" % (level, sym, at))
            elif reason == "MARKET_ORDER_MARGIN_CLOSEOUT" or \
                    made_for == "MARGIN_CLOSEOUT":
                text = ("trade %s %s by OANDA's margin closeout%s" % (
                    trade, verb, at) if trade
                    else "OANDA's margin closeout of %s%s" % (sym, at))
            elif reason == "MARKET_ORDER_TRADE_CLOSE" or \
                    made_for == "TRADE_CLOSE":
                which = (create.get("tradeClose") or {}).get("tradeID") or (
                    (shut or [cut or {}])[0].get("tradeID"))
                text = ("trade %s %s by a close of it%s" % (trade, verb, at)
                        if trade else "a close of %s's trade %s%s, not "
                        "marked as this request" % (sym, which, at))
            elif reason == "MARKET_ORDER_DELAYED_TRADE_CLOSE" or \
                    made_for == "DELAYED_TRADE_CLOSE":
                text = ("trade %s %s by a delayed trade close%s" % (
                    trade, verb, at) if trade
                    else "a delayed close of a trade of %s%s" % (sym, at))
            else:
                text = ("trade %s %s by an order%s" % (trade, verb, at)
                        if trade else "an order that reduced %s%s" % (sym, at))
            items.append({"text": text, "maybe": maybe})
        return items

    def _lookup(self, entry: dict, kind: str = "write",
                unmarked: list | None = None) -> dict | None:
        """OANDA's record of a request whose answer was lost: an answer-like
        dict of the transactions it made, or None when there is none. For a
        close not found there, `unmarked` (when given) receives what the
        record since it was sent shows closed or reduced its position by
        anything not marked as it (_closed_since; said by _give_up and
        _replay_unknown)."""
        before = int(entry.get("before") or 0)
        r = self.client.call("GET", self._path("/transactions/sinceid"),
                             {"id": before}, kind="read" if kind != "flatten"
                             else "flatten")
        txs = [t for t in r.get("transactions") or [] if isinstance(t, dict)]
        self._ingest(txs)
        m, ocid = entry.get("m"), entry.get("ocid")
        # The orders whose fill names this request's client id: documented
        # (transaction-df), OrderFillTransaction clientOrderID "The client
        # Order ID of the Order filled (only provided if the client has
        # assigned one)".
        named = {str(tx.get("orderID")) for tx in txs
                 if ocid and tx.get("type") == "ORDER_FILL"
                 and tx.get("clientOrderID") == ocid}
        create = None
        for tx in txs:
            typ = str(tx.get("type") or "")
            if typ not in CREATE_TYPES:
                continue
            ext = tx.get("clientExtensions") or {}
            if m == "order.place":
                if ocid:
                    hit = ext.get("id") == ocid
                else:
                    hit = (tx.get("instrument") == entry.get("sym")
                           and _dec(tx.get("units")) == _dec(entry.get("units"))
                           and not ext)
            else:
                tc = tx.get("tradeClose") or {}
                if entry.get("trade"):
                    # documented: transaction-df MarketOrderTransaction
                    # tradeClose, "only provided when the Market Order is
                    # being used to explicitly close a Trade": a trade's
                    # close names it.
                    hit = str(tc.get("tradeID")) == str(entry["trade"])
                else:
                    # A netting close is a POSITION_CLOSEOUT market order.
                    # documented: position-ep longClientExtensions and
                    # shortClientExtensions are "The client extensions to add
                    # to the MarketOrder used to close the long [short]
                    # position"; transaction-df MarketOrderTransaction
                    # clientExtensions "Client Extensions to add to the Order
                    # (only provided if the Order is being created with
                    # client extensions)", and a fill's clientOrderID (above).
                    # So it is found by its create transaction's
                    # clientExtensions.id, or by a fill that names its id.
                    # Assumed, verify on demo: that OANDA keeps the
                    # extensions a close's body gives (the one order whose
                    # extensions arrive in fields of other names) on that
                    # order and on one of those two transactions. Kept on
                    # neither, a late close is never found. On an MT4-linked
                    # account none is given (transaction-df: "Do not set,
                    # modify, or delete this field if your account is
                    # associated with MT4"): any closeout of the instrument
                    # since it was sent is taken as it.
                    pc = (tx.get("longPositionCloseout")
                          or tx.get("shortPositionCloseout") or {})
                    same = ((bool(pc) or tx.get("reason") ==
                             "POSITION_CLOSEOUT")
                            and str(pc.get("instrument")
                                    or tx.get("instrument"))
                            == entry.get("sym"))
                    hit = same and (not ocid or ext.get("id") == ocid
                                    or str(tx.get("id")) in named)
            if hit:
                create = tx
                break
        if create is None:
            if m == "position.close" and unmarked is not None:
                unmarked.extend(self._closed_since(entry, txs))
            return None
        oid = str(create["id"])
        out = {"orderCreateTransaction": create}
        for tx in txs:
            if str(tx.get("orderID") or "") != oid:
                continue
            if tx.get("type") == "ORDER_FILL":
                out["orderFillTransaction"] = tx
            elif tx.get("type") == "ORDER_CANCEL" and \
                    tx.get("reason") != "CLIENT_REQUEST_REPLACED":
                out["orderCancelTransaction"] = tx
        return out

    def _replay_unknown(self, key: str):
        """A request sent before under this key whose answer was lost (or
        that was on the wire when an earlier run stopped). It is NEVER sent
        again (sent again, an order not in OANDA's record 5 s after its
        answer was lost can be carried out twice).
        OANDA refuses a repeated client id only while its order is pending or
        its trade open (documented: transaction-df
        CLIENT_ORDER_ID_ALREADY_EXISTS "already assigned to another pending
        Order", CLIENT_TRADE_ID_ALREADY_EXISTS "...another open Trade"), so a
        netting reduce, or anything on an MT4-linked account, would be carried
        out twice. OANDA's own record answers instead: returns the answer-like
        dict when the request is there; raises the retryable "not known"
        error while it may still turn up (UNKNOWN_WATCH after the loss),
        saying what the record shows of its position meanwhile when that is
        a close of it not marked as it; and after that the final answer
        (_give_up): `x_oanda_not_carried_out`, or, for a close whose
        position the record shows closed or reduced since it was sent by
        anything not marked as it (_closed_since: a closeout, a stop, a
        target, another tool's close of its trade), `x_oanda_close_unmarked`
        with what the record shows. None when nothing was ever sent under
        this key."""
        with self._lock:
            j = self._journal.get(key)
            j = dict(j) if j else None
        if j is None:
            return None
        what = ("the earlier order" if j.get("m") == "order.place"
                else "the earlier close")
        unmarked: list = []
        try:
            found = self._lookup(j, unmarked=unmarked)
        except OandaError as e:
            raise self._fate_unknown("OANDA's record could not be read (%s)"
                                     % (e.message or e.status),
                                     what=what) from None
        if found is not None:
            self._journal_drop(key)
            return found
        lost = int(j.get("lost_at") or j.get("at") or 0)
        if self.now_ms() - lost < UNKNOWN_WATCH * 1000:
            if unmarked:
                # Said as it is (an unmarked closeout already in the record
                # is never "not in OANDA's record yet").
                raise self._fate_unknown(
                    "OANDA's record shows %s; the record is watched for it "
                    "until %s UTC" % (self._shows(unmarked), time.strftime(
                        "%H:%M:%S", time.gmtime(
                            (lost + UNKNOWN_WATCH * 1000) / 1000))),
                    what=what)
            raise self._fate_unknown("the earlier request under this "
                                     "client_id is not in OANDA's record "
                                     "yet", what=what)
        raise self._give_up(key, j, unmarked)

    def handle_order_place(self, p):
        cid = str(p.get("client_id") or "")
        key = "place:" + (cid or uuid.uuid4().hex)
        typ = p.get("type") or "market"
        if cid:
            found = self._replay_unknown(key)
            if found is not None:
                return self._placed(found, cid, typ)
        try:
            body, sym, units, ocid = self._order_request(p, typ)
        except AdapterError as e:
            if not e.retryable:
                self._rejected(cid, e)       # its one terminal event
            raise
        with self._lock:
            if ocid:
                # Filed BEFORE the request leaves: the stream can report the
                # fill before the answer that names the order arrives.
                self._cids[ocid] = cid
                self._trim(self._cids, 2 * KEEP_RESULTS)
        self._journal_put(key, {"m": "order.place", "cid": cid,
                                "ocid": ocid, "sym": sym,
                                "units": str(units),
                                "sig": protocol.order_signature(
                                    "order.place", p)})
        try:
            out = self.client.call("POST", self._path("/orders"), body=body,
                                   kind="write")
        except OandaError as e:
            if not self._lost(key, e):
                if ocid and cid and e.reason in (
                        "CLIENT_TRADE_ID_ALREADY_EXISTS",
                        "CLIENT_ORDER_ID_ALREADY_EXISTS"):
                    prior = self._prior_at_oanda(ocid, cid, sym, units, body,
                                                 typ)
                    if prior is not None:
                        return prior
                err = self._err(e)
                if not err.retryable:
                    self._rejected(cid, err)
                raise err from None
            # The answer was lost: OANDA's record is read before anything
            # is said.
            try:
                found = self._lookup(self._journal.get(key) or {})
            except OandaError:
                found = None
            if found is None:
                raise self._fate_unknown(unanswered(e),
                                         what="the order") from None
            out = found
        try:
            # Applied while the request is still written down: on an MT4
            # account that entry is how the order is known as this one.
            return self._placed(out, cid, typ)
        finally:
            self._journal_drop(key)

    def _prior_at_oanda(self, ocid: str, cid: str, sym: str, units: Decimal,
                        body: dict, typ: str) -> dict | None:
        """OANDA refused this order because an earlier request under the same
        client id is still there: its trade is open (documented:
        transaction-df CLIENT_TRADE_ID_ALREADY_EXISTS "The client Trade ID
        specified is already assigned to another open Trade") or its order
        rests (CLIENT_ORDER_ID_ALREADY_EXISTS "...another pending Order").
        When that earlier request is THIS one (the same instrument, side and
        size; for a resting order its type and price too), it was carried
        out, and its outcome is the answer (SPEC 2), never `order.rejected`
        (a filled order is never announced rejected). Its
        fill is announced again under its own fill_id (a platform that has
        it ignores the repeat; one whose earlier process ended before saying
        it learns of it). Anything else holding the id: None, and the
        refusal stands (duplicate_client_id). documented: order-df
        OrderSpecifier and trade-df TradeSpecifier, the client's id with "@"
        (order-ep's example: /orders/@my_order_100)."""
        spec = urllib.parse.quote("@" + ocid, safe="@")
        want = body.get("order") or {}
        try:
            o = self.client.call("GET", self._path("/orders/" + spec)).get(
                "order")
        except OandaError:
            o = None
        if (isinstance(o, dict) and o.get("state") == "PENDING"
                and str(o.get("type")) == str(want.get("type"))
                and o.get("instrument") == sym
                and _dec(o.get("units")) == units
                and _dec(o.get("price")) == _dec(want.get("price"))):
            oid = str(o["id"])
            with self._lock:
                rec = self._orders.get(oid)
                if rec is not None:
                    rec.update(kind="user", cid=cid)
                root = (rec or {}).get("root") or self._root_of(oid)
            log("%s: OANDA holds it already (pending order %s); that is the "
                "answer" % (cid, oid))
            return {"order_id": root, "status": "resting"}
        try:
            t = self.client.call("GET", self._path("/trades/" + spec)).get(
                "trade")
        except OandaError:
            t = None
        if not (isinstance(t, dict) and t.get("state", "OPEN") == "OPEN"
                and t.get("instrument") == sym
                and _dec(t.get("initialUnits")) == units):
            return None
        tid = str(t.get("id") or "")
        oid, fill = tid, None
        try:
            # assumed, verify on demo: a trade's id is that of the ORDER_FILL
            # that opened it (documented only as tradeOpened.tradeID); the
            # transaction read is checked to be that fill before it is used.
            tx = self.client.call("GET", self._path(
                "/transactions/" + urllib.parse.quote(tid, safe=""))).get(
                "transaction") or {}
            if (tx.get("type") == "ORDER_FILL" and str((tx.get(
                    "tradeOpened") or {}).get("tradeID") or "") == tid):
                fill, oid = tx, str(tx.get("orderID") or tid)
        except OandaError:
            pass
        with self._lock:
            root = (self._orders.get(oid) or {}).get("root") or \
                self._root_of(oid)
            tr = self._trades.get(tid)
            if tr is not None and not tr.get("cid"):
                tr["cid"] = cid
        if fill is not None:
            f = self._fill_row(fill)
            f.pop("reason_detail", None)
            f.update(client_id=cid, order_id=root)
            self.emit("order.filled", f)
        log("%s: OANDA holds its trade %s already; that is the answer"
            % (cid, tid))
        return {"order_id": root,
                "status": "accepted" if typ == "market" else "resting"}

    def _order_request(self, p: dict, typ: str):
        """An order.place checked and built: (body, symbol, signed units,
        OANDA client id). Raises the refusal."""
        side = p.get("side")
        sym = str(p.get("symbol") or "")
        cid = str(p.get("client_id") or "")
        spec, own = self._instrument(sym)
        if side not in ("buy", "sell"):
            raise AdapterError("x_oanda_bad_request", "side must be buy or "
                               "sell")
        if typ == "stop_limit":
            raise AdapterError("unsupported", "OANDA has no stop-limit order "
                               "(a STOP's price bound only cancels it on a "
                               "gap)")
        if typ not in ("market", "limit", "stop"):
            raise AdapterError("unsupported", "order type %r is not one of "
                               "market, limit, stop" % typ)
        qty = self._check_qty(own, p.get("qty"))
        units = qty if side == "buy" else -qty
        price = None
        if typ != "market":
            price = self._check_price(own, p.get("price"), "price")
            if price is None:
                raise AdapterError("x_oanda_bad_request", "a %s order needs a "
                                   "price" % typ)
        tif, gtd = self._expiry(p, typ)
        sl = self._check_price(own, p.get("sl"), "sl")
        tp = self._check_price(own, p.get("tp"), "tp")
        trail = self._check_trail(own, p.get("trail_points"))
        self._check_levels(side == "buy", sl, tp)
        levels = self._attach(sym, units, sl, tp, trail, typ)
        if typ == "market":
            ref = self._market_ref(sym, own)
            self._check_levels_now(sym, side == "buy", levels, ref)
            self._check_margin(sym, own, units, ref)
        else:
            self._check_levels_at(side == "buy", price, levels)
        ocid = None
        if not self._mt4:
            # An order sent with no client_id still carries an id of this
            # adapter's own, so that it is known as ours on the stream.
            ocid = (oanda_client_id(cid) if cid
                    else "brue-" + uuid.uuid4().hex[:24])
        body = self._order_body(own, sym, typ, units, price, tif, gtd,
                                levels, ocid)
        return body, sym, units, ocid

    def _extra_units(self, sym: str, units: Decimal) -> Decimal:
        """How many units this order adds to what carries margin: on a
        netting account the change in the net size; on a hedging one the
        change in the larger leg (documented, sub-accounts PDF: a hedged
        position is margined on its larger leg)."""
        with self._lock:
            ts = [t["units"] for t in self._trades.values() if t["sym"] == sym]
        long_u = sum((u for u in ts if u > 0), D0)
        short_u = -sum((u for u in ts if u < 0), D0)
        if self._hedging:
            nl = long_u + units if units > 0 else long_u
            ns = short_u - units if units < 0 else short_u
            return max(nl, ns) - max(long_u, short_u)
        net = long_u - short_u
        return abs(net + units) - abs(net)

    def _market_ref(self, sym: str, own: dict) -> dict | None:
        """The market as OANDA prices it now, for the checks before a market
        order: {"bid", "ask", "mid", "factor"} (factor: OANDA's own from the
        quote currency into the account's, or None). One request. documented:
        pricing-ep includeHomeConversions, pricing-df
        HomeConversions.positionValue. None when it cannot be read: OANDA is
        then the judge."""
        try:
            r = self._call("GET", self._path("/pricing"),
                           {"instruments": sym,
                            "includeHomeConversions": "true"})
        except AdapterError as e:
            log("no price read for the checks of a %s order: %s" % (sym,
                                                                  e.code))
            return None
        out = None
        for msg in r.get("prices") or []:
            got = self._price_from(msg if isinstance(msg, dict) else {})
            if got and got[0] == sym:
                bid = Decimal(repr(got[1]["bid"]))
                ask = Decimal(repr(got[1]["ask"]))
                out = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                       "factor": None}
        if out is None:
            return None
        quote = own.get("quote") or ""
        if quote == self.account_currency:
            out["factor"] = Decimal(1)
        for hc in r.get("homeConversions") or []:
            if isinstance(hc, dict) and hc.get("currency") == quote:
                out["factor"] = _dec(hc.get("positionValue"))
        return out

    def _check_levels_now(self, sym: str, long: bool, levels: dict,
                          ref: dict | None):
        """A market order's stop or target already on the wrong side of the
        market is refused here, `invalid_stops`. Judged leniently: against
        the most favourable of the prices this adapter SENT the platform in
        the last 3 s and of OANDA's price now, so that a level the platform
        set from the quote it had is never refused because the market moved
        since; OANDA's own refusal of that is a `requote`."""
        sl, tp = levels.get("sl"), levels.get("tp")
        if sl is None and tp is None:
            return
        now = time.monotonic()
        with self._lock:
            hist = [(b, a) for t, b, a in self._quote_hist.get(sym, ())
                    if now - t <= 3.0]
        bids = [Decimal(repr(b)) for b, a in hist]
        asks = [Decimal(repr(a)) for b, a in hist]
        if ref is not None:
            bids.append(ref["bid"])
            asks.append(ref["ask"])
        if not bids:
            return
        hi_bid, lo_ask = max(bids), min(asks)
        # documented: order-df OrderTriggerCondition DEFAULT, a long's exits
        # (sell orders) trigger on the bid, a short's on the ask.
        if long:
            if sl is not None and sl >= hi_bid:
                raise AdapterError("invalid_stops", "the stop %s is not below "
                                   "the bid (%s): it would close the position "
                                   "at once" % (sl, hi_bid))
            if tp is not None and tp <= lo_ask:
                raise AdapterError("invalid_stops", "the target %s is not "
                                   "above the ask (%s): it would close the "
                                   "position at a loss" % (tp, lo_ask))
        else:
            if sl is not None and sl <= lo_ask:
                raise AdapterError("invalid_stops", "the stop %s is not above "
                                   "the ask (%s): it would close the position "
                                   "at once" % (sl, lo_ask))
            if tp is not None and tp >= hi_bid:
                raise AdapterError("invalid_stops", "the target %s is not "
                                   "below the bid (%s): it would close the "
                                   "position at a loss" % (tp, hi_bid))

    @staticmethod
    def _check_levels_at(long: bool, price, levels: dict):
        """A resting order's stop and target against its own price."""
        sl, tp = levels.get("sl"), levels.get("tp")
        if price is None:
            return
        if long and ((sl is not None and sl >= price)
                     or (tp is not None and tp <= price)):
            raise AdapterError("invalid_stops", "a buy at %s needs its stop "
                               "below and its target above that price" % price)
        if not long and ((sl is not None and sl <= price)
                         or (tp is not None and tp >= price)):
            raise AdapterError("invalid_stops", "a sell at %s needs its stop "
                               "above and its target below that price" % price)

    def _check_margin(self, sym: str, own: dict, units: Decimal,
                      ref: dict | None):
        """Refuse what the account plainly cannot carry (SPEC 8.6), as the
        reference adapter does, from OANDA's own numbers: the instrument's
        margin rate, a mid price, OANDA's conversion factor into the
        account's currency and its marginAvailable. documented (sub-accounts
        PDF): "Margin_Used = Margin_Rate * Trade_Quantity *
        Instrument_To_Home_Currency_Conversion_Rate", at mid. Within 1% of
        the line, or when a figure cannot be had, OANDA is the judge."""
        extra = self._extra_units(sym, units)
        if extra <= 0 or ref is None or ref.get("factor") is None:
            return
        need = extra * ref["mid"] * own["margin"] * ref["factor"]
        with self._lock:
            a, at = dict(self._summary or {}), self._account_t
        if not a or time.monotonic() - at > 5.0 or need > _d0(
                a.get("marginAvailable")) / 2:
            try:
                a = self._call("GET", self._path("/summary")).get(
                    "account") or a
                with self._lock:
                    self._summary, self._account_t = a, time.monotonic()
            except AdapterError as e:
                log("account not re-read for the margin check: %s" % e.code)
        free = _d0(a.get("marginAvailable"))
        if a and need > free * Decimal("1.01") + Decimal("0.01"):
            raise AdapterError(
                "insufficient_margin", "this order needs about %s %s of "
                "margin and the account has %s free" % (
                    _fmt(need, 2), self.account_currency, _fmt(free, 2)))

    def _rejected(self, cid: str, e: AdapterError):
        """A refusal before any order existed at OANDA: its one terminal
        event (SPEC 5)."""
        d = {"client_id": cid or None, "code": e.code,
             "message": self.client.scrub(str(e))}
        if e.like:
            d["like"] = e.like
        self.emit("order.rejected", d)

    def _placed(self, out: dict, cid: str, typ: str) -> dict:
        """An order's answer (or OANDA's record of it) applied, and the
        platform's result."""
        self._ingest(txs_of(out))
        create = out.get("orderCreateTransaction") or {}
        oid = str(create.get("id") or "")
        if not oid:
            raise self._fate_unknown("OANDA's answer named no order",
                                     what="the order")
        cancel = out.get("orderCancelTransaction")
        if isinstance(cancel, dict) and not out.get("orderFillTransaction"):
            reason = str(cancel.get("reason") or "")
            code, like, retryable = map_reason(reason)
            if code == "requote":
                raise AdapterError(
                    code, "the market moved past this order's stop or target "
                    "between its price and its fill, and OANDA cancelled it "
                    "(%s); nothing was dealt. Ask again with the same "
                    "client_id, or with new levels." % reason, retryable=True)
            raise AdapterError(code, "OANDA cancelled the order at once: %s"
                               % reason, retryable=retryable, like=like)
        with self._lock:
            rec = self._orders.get(oid) or {}
            root = rec.get("root") or oid
        self._save_state()
        return {"order_id": root,
                "status": "accepted" if typ == "market" else "resting"}

    # == orders: listing, cancelling, amending ===========================
    def _order_view(self, o: dict) -> dict | None:
        typ = str(o.get("type") or "")
        if typ not in ENTRY_TYPES:
            return None
        oid = str(o.get("id"))
        with self._lock:
            rec = self._orders.get(oid) or {}
            root = rec.get("root") or self._root_of(oid)
        ext = o.get("clientExtensions") or {}
        cid = rec.get("cid") or self._cid_for(ext.get("id"))
        units = _d0(o.get("units"))
        qty = abs(units)
        state = str(o.get("state") or "")
        sname = {"PENDING": "resting", "TRIGGERED": "resting",
                 "FILLED": "filled", "CANCELLED": "cancelled"}.get(state,
                                                                    "cancelled")
        tif = str(o.get("timeInForce") or "GTC")
        view_rec = {"tif": tif, "gtd_ms": to_ms(o.get("gtdTime")),
                    "created_ms": to_ms(o.get("createTime"))}
        filled = qty if state == "FILLED" else D0
        reason = None
        if sname != "resting":
            reason = {"filled": "filled"}.get(sname, "cancelled")
        return {"order_id": root, "client_id": cid,
                "symbol": str(o.get("instrument") or ""),
                "type": SPEC_TYPE.get(typ, typ.lower()),
                "side": "buy" if units > 0 else "sell", "qty": float(qty),
                "filled_qty": float(filled),
                "remaining_qty": float(qty - filled)
                if sname == "resting" else 0.0,
                "price": _fl(o.get("price")) or None, "stop_price": None,
                "sl": _fl((o.get("stopLossOnFill") or {}).get("price"))
                or None,
                "tp": _fl((o.get("takeProfitOnFill") or {}).get("price"))
                or None,
                "trail_points": None,
                "expiry": EXPIRY_OF.get(tif, "gtc"),
                "expires_at": self._expires(view_rec),
                "state": sname, "placed_at": view_rec["created_ms"],
                "done_at": to_ms(o.get("filledTime") or o.get("cancelledTime")),
                "fill_price": None, "reason": reason}

    def handle_orders_list(self, p):
        state = (p or {}).get("state")
        pend = self._call("GET", self._path("/pendingOrders"))
        rows = [o for o in pend.get("orders") or [] if isinstance(o, dict)]
        if state != "resting":
            # Every resting order (pendingOrders is all of them), and the
            # 500 newest of every state beside them (documented: order-ep GET
            # .../orders count "maximum 500", "If not provided the most
            # recent Orders in the Account are returned"). Those 500 count
            # each trade's stops and targets too, so an older resting order
            # could be left out of them alone.
            r = self._call("GET", self._path("/orders"),
                           {"state": "ALL", "count": 500})
            held = {str(o.get("id")) for o in rows}
            rows += [o for o in r.get("orders") or [] if isinstance(o, dict)
                     and str(o.get("id")) not in held]
        out = []
        for o in rows:
            if not isinstance(o, dict):
                continue
            if o.get("state") == "CANCELLED" and o.get("replacedByOrderID"):
                continue          # it lives on as its replacement
            v = self._order_view(o)
            if v is not None:
                out.append(v)
        out.sort(key=lambda v: v.get("placed_at") or 0)
        return {"orders": out}

    def _live_order(self, root: str) -> dict:
        """The order the platform knows as `root` as OANDA holds it now: its
        newest replacement, followed through OANDA's own link when this
        adapter has not seen the replace yet (documented: order-df Order
        replacedByOrderID). not_resting only for an order that filled,
        expired or was cancelled (SPEC 6), never for one that was replaced."""
        with self._lock:
            cur = self._current.get(root, root)
        for _ in range(8):
            try:
                o = self.client.call("GET", self._path(
                    "/orders/" + urllib.parse.quote(cur, safe=""))).get(
                    "order")
            except OandaError as e:
                if e.status == 404:
                    raise AdapterError("not_found", "OANDA has no order %s"
                                       % root) from None
                raise self._err(e) from None
            nxt = (o or {}).get("replacedByOrderID") if isinstance(
                o, dict) else None
            if (isinstance(o, dict) and o.get("state") == "CANCELLED"
                    and nxt):
                cur = str(nxt)
                with self._lock:
                    self._current[root] = cur
                    self._unsaved = True
                continue
            break
        if not isinstance(o, dict):
            raise AdapterError("not_found", "OANDA has no order %s" % root)
        if str(o.get("type")) not in ENTRY_TYPES:
            raise AdapterError("not_found", "order %s is not a resting "
                               "order this connection can change" % root)
        if o.get("state") not in ("PENDING",):
            raise AdapterError("not_resting", "order %s is %s" % (
                root, str(o.get("state") or "not pending").lower()))
        return o

    def _earlier_change(self, key: str) -> bool:
        """Did an earlier request under this key (an amend or a cancel whose
        answer was lost, or that was on the wire when an earlier run stopped)
        do its work? Read in OANDA's record: the replace of that order
        (documented: transaction-df, the new order's replacesOrderID) or its
        cancel at the client's request (ORDER_CANCEL reason CLIENT_REQUEST).
        True: the original outcome, {}, is the answer (SPEC 2; a
        carried-out amend is not answered not_resting, with that kept as
        final). False: asking again is safe, because each names
        the ONE order it changes, and a late earlier request finds that order
        no longer pending and is refused. Raises the retryable "not known"
        error when the record cannot be read."""
        with self._lock:
            j = self._journal.get(key)
            j = dict(j) if j else None
        if j is None:
            return False
        try:
            r = self.client.call("GET", self._path("/transactions/sinceid"),
                                 {"id": int(j.get("before") or 0)})
        except OandaError as e:
            raise self._fate_unknown(
                "OANDA's record could not be read (%s)"
                % (e.message or e.status), True,
                what="the earlier cancel" if j.get("m") == "order.cancel"
                else "the earlier amend") from None
        txs = [t for t in r.get("transactions") or [] if isinstance(t, dict)]
        self._ingest(txs)
        oid = str(j.get("oid") or "")
        done = False
        for tx in txs:
            if j.get("m") == "order.cancel":
                done = (tx.get("type") == "ORDER_CANCEL"
                        and str(tx.get("orderID") or "") == oid
                        and tx.get("reason") == "CLIENT_REQUEST")
            else:
                done = str(tx.get("replacesOrderID") or "") == oid
            if done:
                break
        self._journal_drop(key)
        return done

    def handle_order_cancel(self, p):
        cid = str(p.get("client_id") or "")
        root = str(p.get("order_id") or "")
        key = "cancel:" + (cid or uuid.uuid4().hex)
        if cid and self._earlier_change(key):
            return {}
        o = self._live_order(root)
        self._journal_put(key, {"m": "order.cancel", "cid": cid,
                                "oid": str(o["id"]), "root": root})
        try:
            out = self.client.call("PUT", self._path(
                "/orders/%s/cancel" % urllib.parse.quote(str(o["id"]),
                                                         safe="")),
                kind="write")
        except OandaError as e:
            if self._lost(key, e):
                raise self._fate_unknown(unanswered(e), True,
                                         what="the cancel") from None
            if e.status == 404:
                # Gone between the read and the cancel: filled or cancelled.
                # assumed, verify on demo: OANDA answers a cancel of an order
                # no longer pending with 404 ORDER_DOESNT_EXIST.
                raise AdapterError("not_resting", "order %s is no longer "
                                   "pending at OANDA" % root) from None
            raise self._err(e) from None
        self._ingest(txs_of(out))
        self._journal_drop(key)
        return {}

    def handle_order_amend(self, p):
        cid = str(p.get("client_id") or "")
        root = str(p.get("order_id") or "")
        if p.get("stop_price") is not None:
            raise AdapterError("unsupported", "OANDA has no stop-limit "
                               "order, so there is no stop_price to amend")
        jkey = "amend:" + (cid or uuid.uuid4().hex)
        with self._lock:
            earlier = bool(cid) and jkey in self._journal
        if earlier and self._earlier_change(jkey):
            return {}
        o = self._live_order(root)
        sym = str(o.get("instrument") or "")
        spec, own = self._instrument(sym)
        units = _d0(o.get("units"))
        long = units > 0
        new = {"type": o.get("type"), "instrument": sym,
               "units": o.get("units"), "price": o.get("price"),
               "timeInForce": o.get("timeInForce") or "GTC",
               "positionFill": o.get("positionFill") or "DEFAULT",
               "triggerCondition": o.get("triggerCondition") or "DEFAULT"}
        if o.get("gtdTime") is not None:
            new["gtdTime"] = o["gtdTime"]
        # assumed, verify on demo: the replacing order may carry the same
        # client id as the one it replaces (OANDA refuses an id only while
        # ANOTHER pending order holds it, and the old one is cancelled in
        # the same request), and must, or "@id" stops finding it.
        for key in ("clientExtensions", "tradeClientExtensions"):
            if o.get(key) and not self._mt4:
                new[key] = o[key]
        for key in ("stopLossOnFill", "takeProfitOnFill",
                    "trailingStopLossOnFill", "priceBound"):
            if o.get(key) is not None:
                new[key] = o[key]
        if p.get("price") is not None:
            new["price"] = _fmt(self._check_price(own, p["price"], "price"),
                                own["dp"])
        if p.get("qty") is not None:
            q = self._check_qty(own, p["qty"])
            new["units"] = _fmt(q if long else -q, own["tup"])
        if "sl" in p:
            sl = self._check_price(own, p.get("sl"), "sl")
            if sl is None:
                new.pop("stopLossOnFill", None)
            else:
                new["stopLossOnFill"] = {"price": _fmt(sl, own["dp"]),
                                         "timeInForce": "GTC"}
        if "tp" in p:
            tp = self._check_price(own, p.get("tp"), "tp")
            if tp is None:
                new.pop("takeProfitOnFill", None)
            else:
                new["takeProfitOnFill"] = {"price": _fmt(tp, own["dp"]),
                                           "timeInForce": "GTC"}
        sl_v = _dec((new.get("stopLossOnFill") or {}).get("price"))
        tp_v = _dec((new.get("takeProfitOnFill") or {}).get("price"))
        self._check_levels(long, sl_v, tp_v)
        self._check_levels_at(long, _dec(new.get("price")),
                              {"sl": sl_v, "tp": tp_v})
        if p.get("expiry") is not None or p.get("expiry_at") is not None:
            tif, gtd = self._expiry({"expiry": p.get("expiry") or (
                "gtd" if p.get("expiry_at") is not None else "gtc"),
                "expiry_at": p.get("expiry_at")}, "limit")
            new["timeInForce"] = tif
            new.pop("gtdTime", None)
            if gtd is not None:
                new["gtdTime"] = unix_s(gtd)
        entry = {"m": "order.amend", "cid": cid, "oid": str(o["id"]),
                 "root": root}
        self._journal_put(jkey, entry)
        try:
            out = self.client.call("PUT", self._path(
                "/orders/" + urllib.parse.quote(str(o["id"]), safe="")),
                body={"order": new}, kind="write")
        except OandaError as e:
            if self._lost(jkey, e):
                raise self._fate_unknown(unanswered(e), True,
                                         what="the amend") from None
            if e.status == 404:
                # Replaced meanwhile by the earlier request under this
                # client_id, carried out late? Then that is the answer.
                if earlier:
                    self._journal_put(jkey, dict(entry))
                    if self._earlier_change(jkey):
                        return {}
                raise AdapterError("not_resting", "order %s is no longer "
                                   "pending at OANDA" % root) from None
            raise self._err(e) from None
        self._journal_drop(jkey)
        self._ingest(txs_of(out))
        cancel = out.get("replacingOrderCancelTransaction")
        if isinstance(cancel, dict):
            reason = str(cancel.get("reason") or "")
            code, like, retryable = map_reason(reason)
            raise AdapterError(code, "OANDA cancelled the amended order at "
                               "once: %s" % reason, retryable=retryable,
                               like=like)
        self._save_state()
        return {}

    # == stops and targets on a position =================================
    def _trades_of(self, pid: str, fresh: bool = False) -> tuple[str, list]:
        """(instrument, the trades) behind a position id; not_found when
        there are none. From the book, or (none there, or `fresh`) from the
        book brought up to OANDA's list (_sync_book)."""
        def pick(rows):
            if self._hedging:
                return [dict(t) for t in rows if t["id"] == pid]
            return [dict(t) for t in rows if t["sym"] == pid]
        trades = []
        if not fresh:
            with self._lock:
                trades = pick(self._trades.values())
        if not trades:
            trades = pick(self._sync_book())
        if not trades:
            raise AdapterError("not_found", "no open position %s" % pid)
        return trades[0]["sym"], sorted(trades, key=lambda t: int(t["id"]))

    def _trade_orders(self, tid: str, body: dict) -> dict:
        out = self._call("PUT", self._path(
            "/trades/%s/orders" % urllib.parse.quote(tid, safe="")),
            body=body, kind="write")
        self._ingest(txs_of(out))
        for key in ("stopLossOrderCreatedCancelTransaction",
                    "takeProfitOrderCreatedCancelTransaction"):
            c = out.get(key)
            if isinstance(c, dict):
                reason = str(c.get("reason") or "")
                code, like, retryable = map_reason(reason)
                raise AdapterError(
                    code if code.startswith("x_") or code in (
                        "invalid_stops", "market_closed") else "invalid_stops",
                    "OANDA cancelled the new level at once: %s" % reason,
                    retryable=retryable, like=like)
        return out

    def _market_now(self, sym: str, kind: str = "read") -> tuple | None:
        """(bid, ask) as OANDA prices `sym` now (documented: pricing-ep GET
        .../pricing), else the newest price the price stream brought in the
        last 10 s; None when neither can be had."""
        try:
            r = self.client.call("GET", self._path("/pricing"),
                                 {"instruments": sym}, kind=kind)
            for msg in r.get("prices") or []:
                got = self._price_from(msg if isinstance(msg, dict) else {})
                if got and got[0] == sym:
                    return (Decimal(repr(got[1]["bid"])),
                            Decimal(repr(got[1]["ask"])))
        except OandaError as e:
            log("no price read for %s: %s" % (sym, e.message or e.status))
        with self._lock:
            q = self._quotes.get(sym)
        if q and time.monotonic() - q["mono"] < 10.0:
            return Decimal(repr(q["bid"])), Decimal(repr(q["ask"]))
        return None

    @staticmethod
    def _through(long: bool, sl, tp, bid, ask) -> str | None:
        """Which of an OPEN position's levels OANDA would fill at once: a
        long's exits (sell orders) trigger on the bid, a short's (buy orders)
        on the ask (documented: order-df OrderTriggerCondition DEFAULT), and a
        level put on a trade that is already through the market is filled at
        once (documented: trade-ep PUT .../trades/{id}/orders,
        stopLossOrderFillTransaction "The Transaction created that immediately
        fills the Trade's new Stop Order")."""
        px = bid if long else ask
        if sl is not None and ((sl >= px) if long else (sl <= px)):
            return "stop"
        if tp is not None and ((tp <= px) if long else (tp >= px)):
            return "target"
        return None

    def handle_order_modify(self, p):
        pid = str(p.get("position_id") or "")
        sym, trades = self._trades_of(pid)
        spec, own = self._instrument(sym)
        long = trades[0]["units"] > 0
        body = {}
        sl = tp = trail = None
        if "sl" in p:
            sl = self._check_price(own, p.get("sl"), "sl")
            body["stopLoss"] = (None if sl is None else
                                {"price": _fmt(sl, own["dp"]),
                                 "timeInForce": "GTC"})
        if "tp" in p:
            tp = self._check_price(own, p.get("tp"), "tp")
            body["takeProfit"] = (None if tp is None else
                                  {"price": _fmt(tp, own["dp"]),
                                   "timeInForce": "GTC"})
        if "trail_points" in p:
            trail = self._check_trail(own, p.get("trail_points"))
            body["trailingStopLoss"] = (None if trail is None else
                                        {"distance": _fmt(trail, own["dp"]),
                                         "timeInForce": "GTC"})
        if not body:
            return {}
        cur_sl = sl if "sl" in p else trades[0].get("sl")
        cur_tp = tp if "tp" in p else trades[0].get("tp")
        self._check_levels(long, cur_sl, cur_tp)
        if sl is not None or tp is not None:
            # A level already through the market is refused HERE, before any
            # trade is touched (the reference adapter's rule, and order.place
            # refuses it too): OANDA would fill it at once, and a call meant
            # to protect the position would close it.
            # Judged against OANDA's price now, strictly.
            mkt = self._market_now(sym)
            if mkt is None:
                raise AdapterError(
                    "no_price", "the price of %s could not be read, so the new "
                    "stop or target cannot be checked against the market; "
                    "OANDA fills one already through the market at once. "
                    "Nothing was changed; ask again." % sym, retryable=True)
            which = self._through(long, sl, tp, *mkt)
            if which:
                px = mkt[0] if long else mkt[1]
                raise AdapterError(
                    "invalid_stops", "the %s of a %s position must be %s the "
                    "market: %s is at %s (the %s) and %s was asked. OANDA "
                    "would fill it at once and close the position; nothing "
                    "was changed." % (
                        which, "long" if long else "short",
                        "below" if (which == "stop") == long else "above",
                        sym, px, "bid" if long else "ask",
                        sl if which == "stop" else tp))
        done, failed = 0, None
        for t in trades:
            try:
                self._trade_orders(t["id"], body)
                done += 1
            except AdapterError as e:
                if e.code != "not_found":
                    failed = failed or e
        if not self._hedging and done:
            # What the user set is the position's from now on: kept, and put
            # on every trade it gains (see _attach and _keep_levels).
            with self._lock:
                lv = dict(self._levels.get(sym) or {})
                for k, v, field in (("sl", sl, "sl"), ("tp", tp, "tp"),
                                    ("trail", trail, "trail_points")):
                    if field in p:
                        lv[k] = None if v is None else str(v)
                # (the position's side: levels are never put on a position
                # on the other side, see _keep_levels)
                lv["side"] = "long" if long else "short"
                if any(lv.get(k) is not None for k in ("sl", "tp", "trail")):
                    self._levels[sym] = lv
                else:
                    self._levels.pop(sym, None)
                self._unsaved = True
            self._save_state()
        if failed is not None:
            if done:
                raise AdapterError(
                    failed.code, "%s (the levels were set on %d of the %d "
                    "trades of this position; ask again to set the rest)"
                    % (failed, done, len(trades)), retryable=True,
                    like=failed.like)
            raise failed
        return {}

    # == closing =========================================================
    def handle_position_close(self, p):
        cid = str(p.get("client_id") or "")
        pid = str(p.get("position_id") or "")
        key = "close:" + (cid or uuid.uuid4().hex)
        if cid:
            found = self._replay_unknown(key)
            if found is not None:
                self._closed_answer(found)
                return {}
        sym, trades = self._trades_of(pid)
        spec, own = self._instrument(sym)
        held_units = sum((t["units"] for t in trades), D0)
        qty = p.get("qty")
        if (isinstance(qty, (int, float)) and not isinstance(qty, bool)
                and float(qty) > float(abs(held_units))):
            # More than the book holds: the book may not have the latest
            # fill yet. Judged on OANDA's own list before it is refused.
            sym, trades = self._trades_of(pid, fresh=True)
            held_units = sum((t["units"] for t in trades), D0)
        units = self._close_units(own, qty, abs(held_units))
        sig = protocol.order_signature("position.close", p)
        if self._hedging:
            body = {"units": units}
            path = self._path("/trades/%s/close" % urllib.parse.quote(
                pid, safe=""))
            entry = {"m": "position.close", "cid": cid, "trade": pid,
                     "sym": sym, "units": units, "sig": sig}
            with self._lock:
                self._intent_trade[pid] = ("close", cid or None)
        else:
            longs = held_units > 0
            body = {"longUnits": units if longs else "NONE",
                    "shortUnits": "NONE" if longs else units}
            ocid = None
            if not self._mt4:
                ocid = "brue-c-" + hashlib.sha256(
                    key.encode("utf-8")).hexdigest()[:24]
                body["longClientExtensions" if longs
                     else "shortClientExtensions"] = {"id": ocid}
            path = self._path("/positions/%s/close" % urllib.parse.quote(
                sym, safe=""))
            entry = {"m": "position.close", "cid": cid, "sym": sym,
                     "units": units, "ocid": ocid, "sig": sig}
            with self._lock:
                self._intent_inst[sym] = ("close", cid or None)
        self._journal_put(key, entry)
        try:
            try:
                out = self.client.call("PUT", path, body=body, kind="write")
            except OandaError as e:
                if not self._lost(key, e):
                    if e.status == 404 or map_reason(e.reason)[0] == \
                            "not_found":
                        try:
                            self._sync_book()
                        except AdapterError:
                            pass
                        raise AdapterError("not_found", "the position %s is "
                                           "no longer open at OANDA" % pid
                                           ) from None
                    raise self._err(e) from None
                try:
                    found = self._lookup(self._journal.get(key) or {})
                except OandaError:
                    found = None
                if found is None:
                    raise self._fate_unknown(unanswered(e),
                                             what="the close") from None
                out = found
            try:
                self._closed_answer(out)
            finally:
                self._journal_drop(key)
        finally:
            with self._lock:
                if self._hedging:
                    self._intent_trade.pop(pid, None)
                else:
                    self._intent_inst.pop(sym, None)
        self._save_state()
        return {}

    def _close_units(self, own: dict, qty, held: Decimal) -> str:
        """OANDA's `units` for a close: "ALL" only when no qty is given (SPEC
        4: the whole position); a qty is sent as that quantity on the
        instrument's grid, ALWAYS, also when it is all the book holds
        (documented: position-ep longUnits, "a DecimalNumber representing how
        many units of the long position to close"; trade-ep units likewise).
        "ALL" for a qty closed whatever the position had grown to at OANDA
        meanwhile (1500 closed for a close of 1000)."""
        if qty is None:
            return "ALL"
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not (
                qty > 0):
            raise AdapterError("invalid_qty", "qty must be a positive number")
        slack = float(own["step"]) * 1e-6
        if float(qty) > float(held) + slack:
            raise AdapterError("invalid_qty", "qty %s is more than the "
                               "position holds (%s)" % (qty, held))
        q = min(float(qty), float(held))
        if not step_ok(q, float(own["step"])):
            raise AdapterError("invalid_qty", "qty %s is not a whole number "
                               "of steps of %s units" % (qty, own["step"]))
        return _fmt(snap(q, own["step"]), own["tup"])

    def _closed_answer(self, out: dict):
        self._ingest(txs_of(out))
        for key in ("orderCancelTransaction", "longOrderCancelTransaction",
                    "shortOrderCancelTransaction"):
            c = out.get(key)
            fill_key = key.replace("Cancel", "Fill")
            if isinstance(c, dict) and not out.get(fill_key):
                reason = str(c.get("reason") or "")
                code, like, retryable = map_reason(reason)
                raise AdapterError(code, "OANDA did not close it: %s" % reason,
                                   retryable=retryable, like=like)

    def handle_flatten_all(self, p):
        """Everything flat: resting entry orders cancelled first (so none can
        open a position meanwhile), then every position closed at OANDA's
        market, instrument by instrument, until OANDA's list of open trades
        is empty or the time is up. Never refused locally; what OANDA will
        not close now is named."""
        cid = str(p.get("client_id") or "")
        closed, problems, unlisted = 0, {}, None
        try:
            pend = self.client.call("GET", self._path("/pendingOrders"),
                                    kind="flatten").get("orders") or []
        except OandaError as e:
            # The positions are closed all the same (SPEC 8.1); what rests is
            # named at the end.
            pend, unlisted = [], e.message or "HTTP %d" % e.status
        for o in pend:
            if not isinstance(o, dict) or str(o.get("type")) not in ENTRY_TYPES:
                continue
            try:
                out = self.client.call("PUT", self._path(
                    "/orders/%s/cancel" % urllib.parse.quote(str(o["id"]),
                                                             safe="")),
                    kind="flatten")
                self._ingest(txs_of(out))
            except OandaError as e:
                if e.status != 404:
                    problems["order:%s" % o.get("id")] = (
                        "resting order %s (%s) not cancelled: %s" % (
                            o.get("id"), o.get("instrument"),
                            _clause(e)))
        flat_read = False
        for _ in range(3):
            # What to close is read from OANDA's own list (the book is NOT
            # replaced by it: its transactions reach the book once each, from
            # the answers below and the stream; see _sync_book).
            try:
                rows, last = self._read_trades(kind="flatten")
                src = list(rows.values())
                flat_read = not src
            except AdapterError:
                with self._lock:
                    src = [dict(t) for t in self._trades.values()]
            left, count = {}, {}
            for t in src:
                s = left.setdefault(t["sym"], [D0, D0])
                s[0 if t["units"] > 0 else 1] += abs(t["units"])
                count[t["sym"]] = count.get(t["sym"], 0) + 1
            if not left:
                break
            for sym, (lu, su) in sorted(left.items()):
                body = {"longUnits": "ALL" if lu > 0 else "NONE",
                        "shortUnits": "ALL" if su > 0 else "NONE"}
                if not self._mt4:
                    tag = "brue-f-" + hashlib.sha256(
                        (cid or uuid.uuid4().hex).encode()).hexdigest()[:24]
                    if lu > 0:
                        body["longClientExtensions"] = {"id": tag}
                    if su > 0:
                        body["shortClientExtensions"] = {"id": tag}
                with self._lock:
                    self._intent_inst[sym] = ("flatten", cid or None)
                before = count.get(sym, 1)
                try:
                    out = self.client.call(
                        "PUT", self._path("/positions/%s/close"
                                          % urllib.parse.quote(sym, safe="")),
                        body=body, kind="flatten")
                    self._ingest(txs_of(out))
                    got = [k for k in ("longOrderFillTransaction",
                                       "shortOrderFillTransaction")
                           if isinstance(out.get(k), dict)]
                    if got:
                        closed += before if self._hedging else 1
                        problems.pop(sym, None)
                    for k in ("longOrderCancelTransaction",
                              "shortOrderCancelTransaction"):
                        c = out.get(k)
                        if isinstance(c, dict):
                            problems[sym] = "OANDA did not close it: %s" % (
                                c.get("reason") or "cancelled")
                except OandaError as e:
                    if e.status == 404:
                        continue          # nothing left on that side
                    problems[sym] = _clause(e)
                finally:
                    with self._lock:
                        self._intent_inst.pop(sym, None)
            left_t = self.client.time_left()
            if left_t is not None and left_t < 1.0:
                break
        with self._lock:
            still = sorted({t["sym"] for t in self._trades.values()})
        if still or not flat_read:
            # "flat" only on OANDA's own word, read after the last close
            try:
                rows, last = self._read_trades(kind="flatten")
                still = sorted({t["sym"] for t in rows.values()})
            except AdapterError:
                pass
        if still:
            why = "; ".join("%s: %s" % (s, problems.get(s, "still open"))
                            for s in still)
            closed_any = "%d closed; " % closed if closed else ""
            market = any("HALTED" in str(problems.get(s, "")) for s in still)
            raise AdapterError(
                "x_oanda_not_flat", "not flat: %snot closed: %s. Ask again."
                % (closed_any, why), retryable=True,
                like="market_closed" if market else "internal")
        left_orders = sorted(v for k, v in problems.items()
                             if k.startswith("order:"))
        if unlisted is not None or left_orders:
            raise AdapterError(
                "x_oanda_orders_left", "every position is closed (%d), but "
                "resting orders may remain: %s. Ask again to cancel them."
                % (closed, "; ".join(left_orders) or "OANDA's list of them "
                   "could not be read (%s)" % unlisted), retryable=True,
                like="internal")
        self._save_state()
        return {"closed": closed}

    # == history =========================================================
    def _range(self, p: dict, default_days: int) -> tuple[int, int, bool]:
        """(from, to, whether `to` was asked for). Without one, OANDA's own
        "now" ends the range (documented: transaction-ep GET .../transactions
        `to` "[default=Current Time]"): this machine's clock, a millisecond
        coarse and perhaps behind OANDA's, would leave out the newest
        transactions."""
        now = self.now_ms()
        asked = isinstance(p.get("to"), (int, float)) and not isinstance(
            p.get("to"), bool)
        to = p.get("to") if asked else now
        frm = p.get("from") if isinstance(p.get("from"), (int, float)) and \
            not isinstance(p.get("from"), bool) else to - default_days * 86400000
        to, frm = int(min(to, now)), int(frm)
        # documented: transaction-ep, at most 365 days a request
        frm = max(frm, to - 365 * 86400000 + 1000)
        return frm, to, asked and to < now

    def _transactions(self, frm: int, to: int, types: str, cap: int,
                      send_to: bool = True) -> list:
        if frm >= to:
            return []
        q = {"from": unix_s(frm), "type": types, "pageSize": 1000}
        if send_to:
            q["to"] = unix_s(to)
        r = self._call("GET", self._path("/transactions"), q)
        pages = [u for u in r.get("pages") or [] if isinstance(u, str)]
        txs: list = []
        for url in reversed(pages):          # newest first, up to `cap`
            u = urllib.parse.urlsplit(url)
            q = dict(urllib.parse.parse_qsl(u.query))
            if not u.path.endswith("/transactions/idrange") or not q.get(
                    "from") or not q.get("to"):
                continue
            # Only ever this account's own idrange, on our own REST host.
            page = self._call("GET", self._path("/transactions/idrange"),
                              {"from": q["from"], "to": q["to"],
                               "type": types})
            txs.extend(t for t in page.get("transactions") or []
                       if isinstance(t, dict))
            if len(txs) >= cap:
                break
        txs.sort(key=lambda t: int(t.get("id") or 0))
        return txs[-cap:]

    def _fill_row(self, tx: dict) -> dict:
        sym = str(tx.get("instrument") or "")
        units = _d0(tx.get("units"))
        oid = str(tx.get("orderID") or "")
        with self._lock:
            rec = self._orders.get(oid) or {}
            root = rec.get("root") or self._root_of(oid)
        opened = tx.get("tradeOpened") or {}
        closes = list(tx.get("tradesClosed") or []) + (
            [tx["tradeReduced"]] if isinstance(tx.get("tradeReduced"), dict)
            else [])
        if self._hedging:
            pid = (str(opened["tradeID"]) if opened.get("tradeID") else
                   str(closes[0].get("tradeID")) if closes else None)
        else:
            pid = sym
        return {"fill_id": str(tx.get("id")), "order_id": root or None,
                "position_id": pid,
                "client_id": self._cid_for(tx.get("clientOrderID")),
                "symbol": sym, "side": "buy" if units > 0 else "sell",
                "qty": float(abs(units)),
                "price": _fl(tx.get("fullVWAP") or tx.get("price")),
                "fees": -float(abs(_d0(tx.get("commission")))
                               + abs(_d0(tx.get("guaranteedExecutionFee")))),
                "time": to_ms(tx.get("time")),
                "reason_detail": str(tx.get("reason") or "")}

    def handle_history_fills(self, p):
        frm, to, asked = self._range(p or {}, HISTORY_DAYS)
        txs = self._transactions(frm, to, "ORDER_FILL", HISTORY_MAX_FILLS,
                                 send_to=asked)
        return {"fills": [self._fill_row(t) for t in txs
                          if t.get("type") == "ORDER_FILL"]}

    def handle_history_operations(self, p):
        p = p or {}
        frm, to, asked = self._range(p, 30)
        limit = p.get("limit")
        limit = (int(limit) if isinstance(limit, int) and not isinstance(
            limit, bool) and 0 < limit <= 1000 else 100)
        # Only values of OANDA's TransactionFilter are sent (documented:
        # https://developer.oanda.com/rest-live-v20/transaction-df/
        # TransactionFilter: TRANSFER_FUNDS, DAILY_FINANCING and the group
        # FUNDING "Funding-related Transactions"; DIVIDEND_ADJUSTMENT is a
        # TransactionType but NOT a TransactionFilter value). assumed, verify
        # on demo: FUNDING brings each DIVIDEND_ADJUSTMENT (the page lists no
        # group's members). What else the group brings is dropped here
        # (_operation).
        txs = self._transactions(
            frm, to, "TRANSFER_FUNDS,DAILY_FINANCING,FUNDING", limit * 2,
            send_to=asked)
        ops = [self._operation(t) for t in txs]
        return {"operations": [o for o in ops if o is not None][-limit:]}

    # == market data: quotes =============================================
    def handle_quotes_subscribe(self, p):
        symbols = list(dict.fromkeys(str(s) for s in (p or {}).get(
            "symbols") or []))
        for s in symbols:
            self._instrument(s)
        now, fresh, prime = time.monotonic(), [], []
        with self._lock:
            added = [s for s in symbols if s not in self._subs]
            self._subs.update(symbols)
            if added:
                self._subs_version += 1
            for s in symbols:
                q = self._quotes.get(s)
                # A platform that asks again is pricing something now: the
                # latest price at once, as the reference adapter does. One
                # the price stream keeps current is current however old it
                # is (OANDA sends a price only when it changes).
                live = self._price_up and s not in added
                if q and (live or now - q["mono"] < 2.0):
                    fresh.append((s, q))
                else:
                    prime.append(s)
        for s, q in fresh:
            self._send_quote(s, q, force=True)
        if prime:
            try:
                self._poll_prices(prime, force=True)
            except AdapterError as e:
                log("prices for %s not read now (%s); the stream brings "
                    "them" % (",".join(prime[:5]), e.code))
        if added:
            self._repoint_prices()
        return {}

    def handle_quotes_unsubscribe(self, p):
        with self._lock:
            before = len(self._subs)
            for s in (p or {}).get("symbols") or []:
                self._subs.discard(str(s))
                self._quote_sent.pop(str(s), None)
            if len(self._subs) != before:
                self._subs_version += 1
        self._repoint_prices()
        return {}

    def _repoint_prices(self):
        """The subscription changed: the stream is reopened for the new set
        (its snapshot brings each newly added price at once)."""
        self._price_wake.set()
        self._shut_stream_soon()

    def _shut_stream_soon(self):
        with self._lock:
            conn = self._price_conn
        if conn is not None:
            self._shut(conn)

    def _send_quote(self, sym: str, q: dict, force: bool = False):
        with self._lock:
            if sym not in self._subs:
                return
            key = (q["bid"], q["ask"], q["time"], q["tradeable"])
            if not force and self._quote_sent.get(sym) == key:
                return
            self._quote_sent[sym] = key
            hist = self._quote_hist.setdefault(
                sym, collections.deque(maxlen=64))
            hist.append((time.monotonic(), q["bid"], q["ask"]))
        self.emit("quote", {"symbol": sym, "bid": q["bid"], "ask": q["ask"],
                            "time": q["time"], "tradeable": q["tradeable"]})

    def _price_from(self, msg: dict) -> tuple[str, dict] | None:
        """One ClientPrice (documented: pricing-df) as a quote: the best bid
        and ask buckets (closeout prices only when a side has no bucket)."""
        sym = str(msg.get("instrument") or "")
        bids, asks = msg.get("bids") or [], msg.get("asks") or []
        bid = _dec(bids[0].get("price")) if bids and isinstance(
            bids[0], dict) else _dec(msg.get("closeoutBid"))
        ask = _dec(asks[0].get("price")) if asks and isinstance(
            asks[0], dict) else _dec(msg.get("closeoutAsk"))
        if not sym or bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        # measured on a live account: on connecting, the stream
        # repeats each instrument's latest price with its ORIGINAL time; that
        # time is kept, never the time it arrived.
        return sym, {"bid": float(bid), "ask": float(ask),
                     "time": to_ms(msg.get("time")) or self.now_ms(),
                     "tradeable": bool(msg.get("tradeable", True)),
                     "mono": time.monotonic()}

    def _poll_prices(self, symbols: list, force: bool = False,
                     kind: str = "read"):
        if not symbols:
            return
        q = {"instruments": ",".join(sorted(symbols))}
        r = self._call("GET", self._path("/pricing"), q, kind=kind)
        for msg in r.get("prices") or []:
            got = self._price_from(msg if isinstance(msg, dict) else {})
            if got is None:
                continue
            sym, quote = got
            with self._lock:
                self._quotes[sym] = quote
            self._send_quote(sym, quote, force=force)

    def _price_loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            with self._lock:
                want = sorted(self._subs)
                version = self._subs_version
            if not want or not self._acct:
                self._set_session("prices", "connected", "")
                self._price_wake.wait(30.0)
                self._price_wake.clear()
                continue
            try:
                conn, resp = self.client.open_stream(
                    self._path("/pricing/stream"),
                    {"instruments": ",".join(want), "snapshot": "true"})
            except OandaError as e:
                self._price_down(e, want, backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            with self._lock:
                self._price_conn, self._price_up = conn, True
            self._set_session("prices", "connected", "")
            why = None
            try:
                while not self._stop.is_set():
                    if self._subs_version != version:
                        why = "resubscribe"
                        break
                    line = resp.readline(STREAM_LINE_MAX)
                    if not line:
                        why = "OANDA ended the price stream"
                        break
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "PRICE" or msg.get("bids") \
                            is not None:
                        got = self._price_from(msg)
                        if got is not None:
                            sym, quote = got
                            with self._lock:
                                self._quotes[sym] = quote
                            self._send_quote(sym, quote)
            except Exception as e:  # noqa: BLE001 - any break is a reconnect
                if self._stop.is_set():
                    return
                why = "the price stream broke (%s)" % type(e).__name__
            finally:
                with self._lock:
                    self._price_conn, self._price_up = None, False
                self._shut(conn)
            if self._stop.is_set():
                return
            if self._subs_version != version:
                time.sleep(0.25)      # gather the rest of a burst of changes
                continue
            # measured on a live account: OANDA ends streams from
            # its side every few hours; reconnect at once (the gate spaces it)
            self._set_session("prices", "degraded", "%s; reconnecting"
                              % (why or "the price stream ended"))
            time.sleep(1.0)

    def _price_down(self, e: OandaError, want: list, backoff: float):
        if e.status == 401 and "forbidden" not in (e.message or "").lower():
            bad = self._token_refused()
            text = ("OANDA refuses the API token (revoked?); prices "
                    "stopped" if bad else "OANDA refused the price stream "
                    "once with 401 although the token still works; trying "
                    "again")
        else:
            text = "the price stream is not connected (%s)" % (
                e.message or e.status)
        self._set_session("prices", "degraded", text + "; prices are read "
                          "every %g s meanwhile" % QUOTE_POLL_SEC)
        end = time.monotonic() + backoff
        while not self._stop.is_set() and time.monotonic() < end:
            try:
                self._poll_prices(want, kind="background")
            except AdapterError:
                pass
            self._price_wake.wait(QUOTE_POLL_SEC)
            self._price_wake.clear()
            with self._lock:
                if sorted(self._subs) != want:
                    return

    def _token_refused(self) -> bool:
        """measured on a live account (2026-10-28): OANDA refused a
        stream's reconnect with 401 on a token that was valid. So one 401
        after a close is checked with GET /v3/accounts before the token is
        called bad."""
        try:
            self.client.call("GET", "/v3/accounts", kind="background")
            return False
        except OandaError as e:
            return e.status == 401

    # == the transaction stream ==========================================
    def _tx_loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                conn, resp = self.client.open_stream(
                    self._path("/transactions/stream"))
            except OandaError as e:
                if e.status == 401 and "forbidden" not in (
                        e.message or "").lower():
                    bad = self._token_refused()
                    text = ("OANDA refuses the API token (revoked?)" if bad
                            else "OANDA refused the transaction stream once "
                            "with 401 although the token still works")
                    wait = 60.0 if bad else 5.0
                else:
                    text = "the transaction stream is not connected (%s)" % (
                        e.message or e.status)
                    wait = backoff
                    backoff = min(backoff * 2, 30.0)
                self._set_session("transactions", "degraded", text
                                  + "; fills are read from OANDA's record "
                                  "meanwhile")
                self._gap_wake.set()
                self._stop.wait(wait)
                continue
            backoff = 1.0
            with self._lock:
                self._tx_conn = conn
            self._set_session("transactions", "connected", "")
            # Whatever happened while it was not connected.
            self._gap_wake.set()
            self._wake.set()
            why = None
            try:
                while not self._stop.is_set():
                    line = resp.readline(STREAM_LINE_MAX)
                    if not line:
                        why = "OANDA ended the transaction stream"
                        break
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "HEARTBEAT":
                        # documented: transaction-ep, TransactionHeartbeat
                        # carries lastTransactionID: a gap shows here.
                        try:
                            n = int(msg.get("lastTransactionID") or 0)
                        except (TypeError, ValueError):
                            n = 0
                        with self._lock:
                            self._beat_last = max(self._beat_last, n)
                            gap = n > self._contig
                        if gap:
                            self._gap_wake.set()
                            self._wake.set()
                        continue
                    self._ingest([msg])
                    with self._lock:
                        gap = self._high > self._contig
                    if gap:
                        self._gap_wake.set()
                        self._wake.set()
            except Exception as e:  # noqa: BLE001 - any break is a reconnect
                if self._stop.is_set():
                    return
                why = "the transaction stream broke (%s)" % type(e).__name__
            finally:
                with self._lock:
                    self._tx_conn = None
                self._shut(conn)
            if self._stop.is_set():
                return
            self._set_session("transactions", "degraded", "%s; reconnecting"
                              % (why or "the transaction stream ended"))
            self._gap_wake.set()
            self._wake.set()
            time.sleep(1.0)

    def _fill_gap(self):
        """Every transaction after the last one applied, from OANDA's record
        (see _read_since)."""
        now = time.monotonic()
        with self._lock:
            since = self._contig
            gap = self._high > since or self._beat_last > since
            down = self._components.get("transactions", ("connected",))[0] \
                != "connected"
            stuck = self._empty is not None
        # A gap is filled at once (once a second while OANDA's answers show
        # nothing new); while the stream is down, OANDA's record is read every
        # two seconds instead.
        if not self._acct or not ((gap and now - self._gap_t >= (
                1.0 if stuck else 0.0)) or (down and now - self._gap_t >= 2.0)):
            return
        self._gap_t = now
        self._read_since("background")

    def _read_since(self, kind: str = "background") -> int:
        """Every transaction after the last one applied in order, read from
        OANDA's record (documented: transaction-ep GET .../transactions/
        sinceid, "all Transactions newer than the TransactionID"), each
        applied once (its events go out). The watermark moves to the highest
        id that answer HELD, never to the answer's lastTransactionID: a
        transaction OANDA commits between building the
        list and naming its last id is read next time, not skipped for ever,
        and so is anything an undocumented cap left out. An id below the
        highest one held that the answer does not have does not exist for
        this account. If OANDA keeps naming a newer id and returning nothing
        after the watermark (EMPTY_GAP_READS answers alike, a second apart),
        that id is taken as seen. Returns the answer's lastTransactionID."""
        with self._lock:
            since = self._contig
        r = self.client.call("GET", self._path("/transactions/sinceid"),
                             {"id": since}, kind=kind)
        rows = [t for t in r.get("transactions") or [] if isinstance(t, dict)
                and str(t.get("id") or "").isdigit()]
        self._ingest(rows)
        try:
            last = int(r.get("lastTransactionID") or 0)
        except (TypeError, ValueError):
            last = 0
        top = max((int(t["id"]) for t in rows), default=0)
        with self._lock:
            if top > self._contig:
                self._contig = top
                self._seen = {n for n in self._seen if n > top}
                self._high = max(self._high, top)
            if rows or last <= self._contig:
                self._empty = None
            elif self._empty and self._empty[:2] == (since, last):
                n = self._empty[2] + 1
                self._empty = (since, last, n)
                if n >= EMPTY_GAP_READS and self._contig == since:
                    log("OANDA's record names transaction %d and has none "
                        "after %d (%d answers alike); taken as seen"
                        % (last, since, n))
                    self._contig = last
                    self._seen = {x for x in self._seen if x > last}
                    self._high = max(self._high, last)
                    self._empty = None
            else:
                self._empty = (since, last, 1)
        return last

    # == the backstop ====================================================
    def _backstop(self):
        last_account = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            if self._gap_wake.is_set():
                self._gap_wake.clear()
            for step in (self._fill_gap, self._watch_unknown,
                         self._keep_levels, self._poll_trailing,
                         self._poll_bars, self._refresh_catalog,
                         self._bars_tidy, self._tidy):
                if self._stop.is_set():
                    return
                try:
                    step()
                except Exception as e:  # noqa: BLE001 - the backstop lives
                    log("%s: %s" % (step.__name__, self.client.scrub(
                        "%s: %s" % (type(e).__name__, e))))
            try:
                now = time.monotonic()
                with self._lock:
                    dirty = self._account_dirty
                    holding = bool(self._trades)
                if dirty or (holding and now - last_account
                             > ACCOUNT_IDLE_POLL):
                    last_account = now
                    with self._lock:
                        self._account_dirty = False
                    self._push_account()
            except Exception as e:  # noqa: BLE001
                log("account: %s" % self.client.scrub(str(e)))
            if self._unsaved:
                self._save_state()

    def _push_account(self):
        try:
            a = self.client.call("GET", self._path("/summary"),
                                 kind="background").get("account") or {}
        except OandaError:
            with self._lock:
                self._account_dirty = True
            return
        view = self._account_view(a)
        with self._lock:
            self._summary = a
            same = self._account_sent is not None and all(
                abs((view.get(k) or 0) - (self._account_sent.get(k) or 0))
                < 1e-9 for k in ("balance", "equity", "margin_used",
                                 "margin_free"))
            if not same:
                self._account_sent = dict(view)
        if not same:
            self.emit("account.updated", view)

    def _watch_unknown(self):
        """Orders and closes whose answer was lost: OANDA's record is read
        for them for UNKNOWN_WATCH; what turns up is applied (its events go
        out) and its outcome is remembered for the platform's retry."""
        now = self.now_ms()
        with self._lock:
            due = [(k, dict(j)) for k, j in self._journal.items()
                   if j.get("unknown") or now - int(j.get("at") or now)
                   > 30_000]
        for key, j in due:
            age = now - int(j.get("lost_at") or j.get("at") or now)
            if j.get("m") not in ("order.place", "position.close"):
                # an amend or a cancel: asking again is safe (each names the
                # one order it changes); it is only looked up on a retry
                if age > UNKNOWN_WATCH * 1000:
                    self._journal_drop(key)
                continue
            if age > UNKNOWN_WATCH * 1000:
                # One last look, then final (see _give_up).
                unmarked: list = []
                try:
                    found = self._lookup(j, kind="background",
                                         unmarked=unmarked)
                except OandaError:
                    continue
                if found is None:
                    self._give_up(key, j, unmarked)
                    continue
            elif now - int(j.get("looked") or 0) < 2000:
                continue
            else:
                found = None
            with self._lock:
                if key in self._journal:
                    self._journal[key]["looked"] = now
            if found is None:
                try:
                    found = self._lookup(j, kind="background")
                except OandaError:
                    continue
            if found is None:
                continue
            cid, sig = j.get("cid") or "", j.get("sig")
            try:
                if j.get("m") == "order.place":
                    res = self._placed(found, cid, "market" if str(
                        (found.get("orderCreateTransaction") or {}).get(
                            "type")) == "MARKET_ORDER" else "limit")
                else:
                    self._closed_answer(found)
                    res = {}
            except AdapterError as e:
                log("%s turned up at OANDA and was refused there: %s"
                    % (key, e.code))
                if cid and sig and not e.retryable:
                    with self._lock:
                        self._final[cid] = {"sig": sig, "code": e.code,
                                            "message": str(e), "like": e.like,
                                            "at": self.now_ms()}
                        self._unsaved = True
                self._journal_drop(key)
                continue
            # The platform's retry under this client id is answered with
            # this outcome, never sent again (SPEC 2): stored BEFORE the
            # entry is dropped, so no retry falls between the two.
            if cid and sig:
                self.idempotent_store(cid, res, sig)
            self._journal_drop(key)
            log("%s turned up at OANDA after its answer was lost; its "
                "outcome is the answer to a retry" % key)

    def _keep_levels(self):
        """A netting position's levels on every one of its trades: a trade
        that lacks one the user set for the position is given it (a resting
        order's fill added it). A trade whose level differs is left as it is
        and shown so (positions.list `levels`). Levels are only ever put on
        the position they were set for: none on a flat position (its levels
        are over), none on one of the other side, and never one that is
        already through the market (OANDA would fill it at once and close
        what it was meant to protect): that one is said and tried again when
        the market is back on its side."""
        if self._hedging or not self._acct:
            return
        now = time.monotonic()
        if now - self._levels_t < LEVELS_EVERY:
            return
        self._levels_t = now
        over = []
        with self._lock:
            work = []
            for sym in list(self._levels):
                lv = self._levels[sym]
                ts = [t for t in self._trades.values() if t["sym"] == sym]
                net = sum((t["units"] for t in ts), D0)
                side = "long" if net > 0 else "short"
                if net == 0 or lv.get("side") not in (None, side):
                    self._levels.pop(sym, None)
                    self._unsaved = True
                    over.append(sym)
                    continue
                lv["side"] = side
                own = self._inst.get(sym)
                if own is None:
                    continue
                for t in ts:
                    body = {}
                    if lv.get("sl") is not None and t["sl"] is None:
                        body["stopLoss"] = {"price": _fmt(_d0(lv["sl"]),
                                                          own["dp"]),
                                            "timeInForce": "GTC"}
                    if lv.get("tp") is not None and t["tp"] is None:
                        body["takeProfit"] = {"price": _fmt(_d0(lv["tp"]),
                                                            own["dp"]),
                                              "timeInForce": "GTC"}
                    if lv.get("trail") is not None and t["trail"] is None:
                        body["trailingStopLoss"] = {
                            "distance": _fmt(_d0(lv["trail"]), own["dp"]),
                            "timeInForce": "GTC"}
                    if body:
                        work.append((sym, t["id"], body, net > 0))
        for sym in over:
            self._unsay("levels:" + sym)
        # (each said for three rounds, and again while it holds: never left
        # standing once the position it was about is gone)
        ttl = 3 * LEVELS_EVERY
        markets: dict = {}
        held: set = set()
        for sym, tid, body, long in work:
            sl = _dec((body.get("stopLoss") or {}).get("price"))
            tp = _dec((body.get("takeProfit") or {}).get("price"))
            if sl is not None or tp is not None:
                if sym not in markets:
                    markets[sym] = self._market_now(sym, kind="background")
                mkt = markets[sym]
                if mkt is None:
                    self._say("levels:" + sym, "the stop/target of %s is not "
                              "put on its trade %s yet: its price could not "
                              "be read to check it against the market; tried "
                              "again in %d s" % (sym, tid, LEVELS_EVERY),
                              ttl=ttl)
                    continue
                # each level judged on its own: one through the market is
                # held back, the other still goes on
                for which, key, value in (("stop", "stopLoss", sl),
                                          ("target", "takeProfit", tp)):
                    if value is None or not self._through(
                            long, value if which == "stop" else None,
                            value if which == "target" else None, *mkt):
                        continue
                    body.pop(key, None)
                    held.add(sym)
                    self._say("levels:" + sym, "the %s of %s (%s) is not put "
                              "on its trade %s: the market (%s %s) is already "
                              "through it, and OANDA would close the trade at "
                              "once; tried again in %d s" % (
                                  which, sym, value, tid,
                                  "bid" if long else "ask",
                                  mkt[0] if long else mkt[1], LEVELS_EVERY),
                              ttl=ttl)
                if not body:
                    continue
            try:
                out = self.client.call("PUT", self._path(
                    "/trades/%s/orders" % urllib.parse.quote(tid, safe="")),
                    body=body, kind="background")
                self._ingest(txs_of(out))
                if sym not in held:
                    self._unsay("levels:" + sym)
            except OandaError as e:
                held.add(sym)
                # (OANDA's own words, which may end in a full stop, run on
                # into this job's: "; tried again", never ".;")
                self._say("levels:" + sym, "the stop/target of %s could not "
                          "be put on its trade %s: %s; tried again in %d s"
                          % (sym, tid, _clause(e), LEVELS_EVERY), ttl=ttl)

    def _poll_trailing(self):
        """documented: account-ep GET .../changes and account-df
        DynamicOrderState.trailingStopValue: OANDA moves a trailing stop and
        says so only here."""
        now = time.monotonic()
        with self._lock:
            trailing = any(t.get("tsl_id") for t in self._trades.values())
            since = self._contig
        if not trailing or now - self._changes_t < CHANGES_POLL:
            return
        self._changes_t = now
        r = self.client.call("GET", self._path("/changes"),
                             {"sinceTransactionID": since}, kind="background")
        self._ingest(((r.get("changes") or {}).get("transactions")) or [])
        moved = []
        with self._lock:
            by_order = {t.get("tsl_id"): t for t in self._trades.values()
                        if t.get("tsl_id")}
            for o in (r.get("state") or {}).get("orders") or []:
                t = by_order.get(str((o or {}).get("id")))
                val = _dec((o or {}).get("trailingStopValue"))
                if t is None or val is None:
                    continue
                if self._trailed.get(t["id"]) == val:
                    continue
                self._trailed[t["id"]] = val
                own = self._inst.get(t["sym"]) or {}
                tick = own.get("tick") or Decimal("0.00001")
                moved.append((t, val, float(t["trail"] / tick)
                              if t.get("trail") else 0))
        for t, val, pts in moved:
            self.emit("position.trailed", {
                "position_id": t["id"] if self._hedging else t["sym"],
                "sl": float(val), "trail_points": pts})

    def _refresh_catalog(self):
        if not self._acct or time.monotonic() - self._catalog_at \
                < CATALOG_REFRESH:
            return
        try:
            self._load_catalog()
        except AdapterError as e:
            self._catalog_at = time.monotonic() - CATALOG_REFRESH + 600
            log("catalog not refreshed: %s" % e.code)

    # == market data: bars ===============================================
    def _bar_spec(self, p):
        spec, own = self._instrument((p or {}).get("symbol"))
        tf = (p or {}).get("timeframe")
        if tf not in TIMEFRAMES:
            raise AdapterError("unsupported", "timeframe %r is not one of %s"
                               % (tf, " ".join(TIMEFRAMES)))
        return spec, tf

    def _bar_store(self, sym: str, tf: str) -> _BarStore:
        with self._bars_lock:
            store = self._bars.get((sym, tf))
            if store is None:
                store = self._bars[(sym, tf)] = _BarStore()
                while len(self._bars) > BARS_STORES:
                    self._bars.popitem(last=False)
            self._bars.move_to_end((sym, tf))
            store.used = time.monotonic()
            return store

    def _bars_tidy(self):
        with self._bars_lock:
            now = time.monotonic()
            for k in [k for k, s in self._bars.items()
                      if now - s.used > BARS_IDLE and not s.reader]:
                self._bars.pop(k, None)

    def _bar_fetch(self, sym: str, tf: str, count: int, to: int | None = None,
                   kind: str = "read") -> tuple[list, int]:
        """Closed bid candles (documented: pricing-ep, the account-scoped
        candles endpoint only; price=B; dailyAlignment=0 with
        alignmentTimezone=UTC so 4h and day bars start at UTC multiples,
        which is how the platform buckets time)."""
        q = {"price": "B", "granularity": TIMEFRAMES[tf][0],
             "count": int(count), "dailyAlignment": 0,
             "alignmentTimezone": "UTC"}
        if to is not None:
            q["to"] = unix_s(to)
        r = self._call("GET", self._path("/instruments/%s/candles"
                                         % urllib.parse.quote(sym, safe="")),
                       q, kind=kind)
        raw = [c for c in r.get("candles") or [] if isinstance(c, dict)]
        rows = []
        for c in raw:
            # documented: instrument-df Candlestick.complete "A complete
            # candlestick is one whose ending time is not in the future";
            # the forming one is not a bar yet (SPEC: closed bars).
            if not c.get("complete"):
                continue
            b = c.get("bid") or {}
            t = to_ms(c.get("time"))
            try:
                rows.append([t, float(b["o"]), float(b["h"]), float(b["l"]),
                             float(b["c"]), int(c.get("volume") or 0)])
            except (KeyError, TypeError, ValueError):
                continue
        return [r for r in rows if r[0] is not None], len(raw)

    def _bars_head(self, store: _BarStore, sym: str, tf: str, count: int,
                   kind: str = "read"):
        width = TIMEFRAMES[tf][1]
        n = count + 1
        if store.rows:
            since = (self.now_ms() - max(store.rows)) // width + 2
            n = max(n, since)
        n = min(BARS_PAGE, n)
        rows, raw_n = self._bar_fetch(sym, tf, n, kind=kind)
        if store.rows and rows and max(store.rows) < rows[0][0]:
            store.rows.clear()            # no overlap: the old end is stale
            store.oldest_done = False
        for r in rows:
            store.rows[r[0]] = r
        if raw_n < n:
            store.oldest_done = True
        store.head_at = time.monotonic()

    def _bars_older(self, store: _BarStore, sym: str, tf: str, count: int,
                    kind: str = "read"):
        if not store.rows:
            return
        oldest = min(store.rows)
        n = min(BARS_PAGE, max(2, count - len(store.rows) + 1))
        # documented: pricing-ep, count with `to` alone is allowed (never
        # with both from and to); assumed, verify on demo: whether a candle
        # starting at `to` is included. Either way it is kept once.
        rows, raw_n = self._bar_fetch(sym, tf, n, to=oldest, kind=kind)
        new = [r for r in rows if r[0] < oldest]
        for r in new:
            store.rows[r[0]] = r
        if raw_n < n or not new:
            store.oldest_done = True

    def handle_bars_history(self, p):
        """The newest `count` closed bid bars, oldest first. One request
        answers 5,000 (OANDA's maximum a request); more are read page by
        page. Answers with what it has BARS_FIRST seconds after the request
        was READ, and reads on behind the answer so that asking again
        returns more."""
        spec, tf = self._bar_spec(p)
        count = (p or {}).get("count")
        if (isinstance(count, bool) or not isinstance(count, int)
                or not 1 <= count <= BARS_MAX_COUNT):
            raise AdapterError("unsupported", "count must be a whole number "
                               "from 1 to %d" % BARS_MAX_COUNT)
        sym = spec["symbol"]
        read = self.client.read_at() or time.monotonic()
        deadline, first = read + BARS_BUDGET, read + BARS_FIRST
        store = self._bar_store(sym, tf)
        got = store.lock.acquire(timeout=max(0.05, deadline - time.monotonic()
                                             - 0.5))
        if not got:
            if store.rows and time.monotonic() - store.head_at < 60:
                return {"bars": store.newest(count)}
            raise AdapterError("internal", "the %s bars of %s are still being "
                               "read; ask again in a moment" % (tf, sym),
                               retryable=True)
        try:
            if (time.monotonic() - store.head_at > BARS_HEAD_TTL
                    or not store.rows):
                self._bars_head(store, sym, tf, count)
            while (len(store.rows) < count and not store.oldest_done
                   and time.monotonic() < first):
                self._bars_older(store, sym, tf, count)
        except AdapterError:
            if not store.rows:
                raise
        finally:
            store.lock.release()
        rows = store.newest(count)
        if len(store.rows) < count and not store.oldest_done:
            self._bars_read_on(store, sym, tf, count)
        return {"bars": rows}

    def _bars_read_on(self, store: _BarStore, sym: str, tf: str, count: int):
        with self._bars_lock:
            if store.reader:
                return
            store.reader = True

        def run():
            try:
                for _ in range(8):
                    with store.lock:
                        if len(store.rows) >= count or store.oldest_done:
                            break
                        self._bars_older(store, sym, tf, count,
                                         kind="background")
            except Exception as e:  # noqa: BLE001
                log("bars %s %s: %s" % (sym, tf, getattr(e, "code", e)))
            finally:
                with self._bars_lock:
                    store.reader = False
        threading.Thread(target=run, daemon=True, name="oanda-bars").start()

    def handle_bars_subscribe(self, p):
        spec, tf = self._bar_spec(p)
        width, now = TIMEFRAMES[tf][1], self.now_ms()
        with self._lock:
            # The watermark starts at the bar now forming: nothing already
            # closed is replayed; that bar is sent when it closes.
            self._bar_subs.setdefault((spec["symbol"], tf), {
                "mark": now // width * width - 1, "tries": 0,
                "next": (now // width + 1) * width + 2000})
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, p):
        spec, tf = self._bar_spec(p)
        with self._lock:
            self._bar_subs.pop((spec["symbol"], tf), None)
        return {}

    def _poll_bars(self):
        now = self.now_ms()
        with self._lock:
            due = [(k, dict(v)) for k, v in self._bar_subs.items()
                   if now >= v["next"]]
        for (sym, tf), sub in due:
            width = TIMEFRAMES[tf][1]
            try:
                rows, _ = self._bar_fetch(sym, tf, 4, kind="background")
            except AdapterError as e:
                log("bar %s %s: %s" % (sym, tf, e.code))
                rows = None
            fresh = sorted(r for r in rows or [] if r[0] > sub["mark"])
            edge = (now // width + 1) * width + 2000
            with self._lock:
                cur = self._bar_subs.get((sym, tf))
                if cur is None:
                    continue
                if fresh:
                    cur["mark"], cur["tries"], cur["next"] = \
                        fresh[-1][0], 0, edge
                else:
                    cur["tries"] += 1
                    cur["next"] = min(edge, now + (2000, 5000, 15000, 60000)[
                        min(cur["tries"], 4) - 1])
            for r in fresh:
                self.emit("bar", {"symbol": sym, "timeframe": tf, "bar": r})


def main():
    adapter = OandaAdapter()
    try:
        # A platform that ends the process (Connector.close after bye times
        # out) still has the state written: SIGTERM unwinds to the save below.
        import signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ImportError, AttributeError, ValueError, OSError):
        pass
    try:
        adapter.run()
    finally:
        adapter._stop.set()
        adapter._save_state(wait=2.0)


if __name__ == "__main__":
    main()
