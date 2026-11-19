#!/usr/bin/env python3
"""Brue Connect adapter for Capital.com (CFDs; demo and live).

Capital.com's Public API v1 (documented: https://open-api.capital.com/) is
JSON over HTTPS on one host per environment, plus one WebSocket that carries
prices and nothing else. A session is opened with the user's own API key,
login email and the key's own password; it is two tokens (CST and
X-SECURITY-TOKEN) that live for ten minutes after their last use. Every order
call is a REQUEST: its answer is a dealReference, and "a successful response
does not always mean that the position has been successfully opened", so the
truth is GET /confirms/{dealReference} and the positions list. There is no
order or position stream and no client order reference.

Shape of the thing:

  * `hello` answers COLD, and LIVE IS OPT-IN. The environment is demo unless
    `--live` or CAPITALCOM_ENV=live is given (a flag wins over the variable;
    both flags together are live). `mode` is a separate question answered
    by the ENDPOINTS: it is "paper" only when the REST host is Capital.com's
    demo host exactly (demo-api-capital.backend-capital.com) or this machine
    (a stand-in on loopback), and the stream host is Capital.com's streaming
    host or this machine. The streaming host is the same for demo and live
    (documented: https://open-api.capital.com/) and carries prices only.
    Anything else is "live" whatever the flags say.
  * Credentials arrive as BRUE_CRED_API_KEY, BRUE_CRED_IDENTIFIER and
    BRUE_CRED_PASSWORD (the credential form's keys). They are checked cold
    for shape, and none of the three can reach a message, the log or a file.
    The first request that needs the broker logs in; a refusal is
    `unauthorized` naming the environment tried. The other environment is
    never tried.
  * ONE account per process (SPEC 3). The account is bound at the first
    call (BRUE_ACCOUNT_ID, else the login's own current account) and EVERY
    later login is switched back to it before any other request is made:
    a new Capital.com session starts on the login's preferred account, and
    an order sent on the tokens of a fresh session before the switch would
    land on the wrong account.
  * Threads: the SDK's read thread only reads and hands requests to lanes
    (the reference adapter's design): calls that change something one at a
    time in the order read; `flatten_all` on a lane of its own that starts
    the moment it is read; reads a few at once; each `bars.history` on a
    thread of its own. One deadline per request counts from when its line
    was read. One thread polls the account (positions, working orders,
    confirmations, activity, transactions); one owns the price stream.
  * A pace of 8 requests a second in all (Capital.com allows 10 a second
    per user), shared by priority: flatten_all, then orders, then the
    handlers' reads and a chart's newest end, then a chart's windows, then
    the adapter's own polling. Order posts keep the documented 0.1 s apart,
    logins one a second, and on demo the 1,000 order posts an hour.
  * Orders are journalled BEFORE they are sent (BRUE_BROKER_STATE,
    capitalcom_state.json, written and flushed to disk): a client_id that
    is in the journal is NEVER sent again. An answer that is lost (no
    answer, a 5xx, a broken connection) leaves an order of unknown fate:
    the call answers a retryable error, and the order is looked for by its
    epic, direction, size and time in the positions and the working orders
    until it is found or a minute has passed; never in place of an order
    Capital.com acknowledged (a dealReference), whose confirmation names
    what it made. It is never announced `order.rejected` unless Capital.com
    refused it. An order of the last run is looked for again by this one.
  * Fills: the price of an execution is the one Capital.com reports (its
    confirmation and the deal's own level), never the requested one ("All
    orders placed through the Public API are executed as market orders",
    documented: https://github.com/capital-com-sv/capital-mcp). Each opened
    deal is one execution and its dealId is the stable `fill_id`. Stops,
    targets, trailing stops and margin close-outs are Capital.com's: they
    are found by polling and reported as `position.closed` with the reason
    from the activity history's `source`.
  * Prices shown are the BID (LSE's rule): bars are built from the bid side
    of Capital.com's bid/ask bars; quotes carry bid and ask.

What Capital.com does that a platform must know:

  * A position is ONE DEAL (`position_id` is its dealId), so the book is a
    hedging book: several deals per symbol. In Capital.com's standard
    (non-hedging) mode an order opposite to open deals offsets them
    (documented: https://capital.com/en-gb/analysis/what-is-hedging-mode-and-how-to-use-it-in-trading);
    that is reported as the order's fills and a `position.closed` for each
    deal it reduced.
  * A deal closes whole or not at all (DELETE /positions/{dealId} takes no
    size): `partial_close` is false.
  * There is no client order reference anywhere in the API, so an order
    placed outside this adapter (Capital.com's own platform) appears as a
    fill with a null client_id when it is first seen.
  * Swap (overnight funding) and fees are Capital.com transactions not
    linked to a deal: `swap` and `commission` on a position are 0, and the
    charges arrive as `balance.operation` of kind `fee`.

Configuration (environment):

  BRUE_CRED_API_KEY / BRUE_CRED_IDENTIFIER / BRUE_CRED_PASSWORD
                              the platform's form: the API key, the login
                              email, the API key's own password
  CAPITALCOM_API_KEY / CAPITALCOM_IDENTIFIER / CAPITALCOM_PASSWORD
                              the same, for running by hand
  CAPITALCOM_ENV = demo | live   demo unless this says live or --live is
                              given; a flag wins
  CAPITALCOM_API_URL, CAPITALCOM_STREAM_URL
                              endpoints, for a test stand-in on this
                              machine. Demo uses no other host (anything
                              else is for --live, and is live); a
                              cleartext one off this machine is never used,
                              nor one that is not a plain host and port (a
                              user name, a backslash, a %-escape, brackets
                              around anything but one IPv6 address, a port
                              that is not a number, a space or a line
                              break). Requests go straight to the endpoint:
                              no proxy, and no redirect is followed
  BRUE_ACCOUNT_ID             the account this process deals on
  BRUE_BROKER_STATE           private directory for the journal and caches
"""
from __future__ import annotations

import base64
import calendar
import collections
import datetime
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
import socket
import ssl
import struct
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal
from email.utils import parsedate_to_datetime

# The SDK lives at the repo root, three levels up from this file. Resolved
# from __file__ so the adapter always runs against the SDK it ships with,
# whatever else is installed for the interpreter that spawned it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect import protocol  # noqa: E402
from brueconnect.adapter import (  # noqa: E402
    MUTATING, PRE_SESSION, Adapter, AdapterError)

VERSION = "0.1"
USER_AGENT = "brue-connect-capitalcom/" + VERSION

# documented: https://open-api.capital.com/ ("https://api-capital.backend-capital.com/",
# "https://demo-api-capital.backend-capital.com/", every path under /api/v1,
# and "wss://api-streaming-capital.backend-capital.com/connect" for both)
LIVE_API_URL = "https://api-capital.backend-capital.com/api/v1"
DEMO_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"
# The ONE name that is demo money, and the streaming host demo and live share.
DEMO_HOST = "demo-api-capital.backend-capital.com"
STREAM_HOST = "api-streaming-capital.backend-capital.com"

# documented: https://open-api.capital.com/ (FAQ "Do you have any limitations
# on your API?": "The maximum request rate is 10 per second per user", "1 per
# 0.1 seconds per user when opening positions or creating orders", "POST
# /session endpoint limit is 1 request per second per API key", "POST
# /positions and POST /workingorders endpoint limit is 1000 requests per hour
# in the Demo account", "The WebSocket API allows subscription to a maximum
# of 40 instruments"). The adapter keeps under each.
PACE_PER_SECOND = 8          # of Capital.com's 10: two left for the user's own
FLATTEN_PER_SECOND = 9       # the panic button may use one more
ORDER_GAP = 0.12             # s between two order posts (Capital.com: 0.1)
LOGIN_GAP = 1.1              # s between two logins (Capital.com: 1 a second)
DEMO_ORDERS_PER_HOUR = 1000
ORDERS_HOUR_KEEP = 10        # of those, kept back for flatten_all's closes
BACKGROUND_PER_SECOND = 3    # the adapter's own polling, at most
BULK_PER_SECOND = 5          # a chart's windows while its caller waits
IDLE_PER_SECOND = 1          # ... and after it has been answered
BLOCK_CAP = 10.0             # longest one 429 may silence the adapter
HANDLER_TIMEOUT = 6.0        # most one request may take
# The platform waits 10 s for any answer. ONE deadline covers a whole
# handler, every request and every wait in it.
HANDLER_BUDGET = 8.0
FLATTEN_HARD = 9.3           # s flatten_all works towards flat and answers
FLATTEN_FLOOR = 5.0          # s flatten_all works for, however long it queued
FLATTEN_WAIT_POST = 1.5      # s it waits for an order post on the wire
READ_WORKERS = 4
CHART_WAITERS = 8
BARS_LOADS = 2               # charts whose windows are read at speed at once
BYE_DRAIN = 4.0
# documented: https://open-api.capital.com/ ("Both tokens are valid for 10
# minutes after the last use"; "Session is active for 10 minutes"; the stream:
# "ping service at least once every 10 minutes")
SESSION_IDLE_PING = 240.0    # s without a request before GET /ping is sent
STREAM_PING_SEC = 60.0
STREAM_BACKOFF_MAX = 30.0
MAX_QUOTE_SYMBOLS = 40
QUOTE_FALLBACK_SEC = 2.0     # REST prices while the stream is down
DETAILS_PER_CALL = 50        # documented: GET /markets?epics=, 50 at most
PRICES_MAX = 1000            # documented: GET /prices max, at most 1000
# Confirmations: Capital.com's own client polls GET /confirms every 0.5 s for
# up to 15 s (documented: https://github.com/capital-com-sv/capital-mcp).
CONFIRM_POLL = 0.25
CONFIRM_WAIT = 4.5           # s an order handler waits for its confirmation
CONFIRM_KEEP = 120.0         # s the poller keeps asking for one after that
UNKNOWN_SEC = 60.0           # s an order of unknown fate is looked for
UNKNOWN_KEEP = 600.0         # s it is remembered (a late arrival is said)
MATCH_SLACK_MS = 3000        # deals this much older than the post can match
# assumed, verify on demo: an order is dealt at once or not at all ("All
# orders placed through the Public API are executed as market orders",
# documented: https://github.com/capital-com-sv/capital-mcp), so a deal opened
# (or a working order made) more than this after the post is not its outcome
MATCH_LATE_MS = int(UNKNOWN_SEC * 1000)
# a deal that an order with a dealReference could have made waits for that
# order's confirmation at most this long (the order is given up on, as
# unconfirmed, after CONFIRM_KEEP anyway)
HOLD_MAX_MS = int((CONFIRM_KEEP + 10) * 1000)
# documented: https://open-api.capital.com/ (GET /history/activity: status
# ACCEPTED, CREATED, EXECUTED, EXPIRED, REJECTED, MODIFIED, MODIFY_REJECT,
# CANCELLED, CANCEL_REJECT, UNKNOWN). Which of them a deal's own rows carry
# is not documented (assumed, verify on demo): ACCEPTED or EXECUTED (or none
# given) is a deal that opened or closed; any other word, REJECTED above all,
# is not (a refused close must not be read as the close).
DONE_STATUS = ("ACCEPTED", "EXECUTED", "")
POLL_FAST = 1.5              # s between reads while anything is open
POLL_SLOW = 5.0
ACCOUNT_POLL = 5.0
TX_POLL = 30.0
CLOCK_POLL = 1800.0
GONE_RETRIES = 6             # polls an unexplained disappearance waits
CACHE_FRESH = 1.0            # s a read of the book serves a handler
CATALOG_MAX_AGE = 24 * 3600
CATALOG_REFRESH = 6 * 3600
HISTORY_DAYS = 7
HISTORY_MAX_FILLS = 500
LEDGER_KEEP = 1000

# bars.history (see _BarStore): Capital.com gives 1,000 bars a request
# (documented). A window asks for at most 999 bar starts (997 between `from`
# and `to` plus one bar of slack at each end, so it does not matter whether
# Capital.com counts the ends in), so which 1,000 an over-full range returns
# (undocumented) never arises; one that comes back full is split anyway.
BARS_SPAN = 997
BARS_SPAN_MAX = BARS_SPAN * 16
BARS_TARGET = 900
BARS_MAX_COUNT = 20000
BARS_BUDGET = 6.0            # s one call may wait; the platform waits 10
BARS_FIRST = 3.0             # s after which a call answers with what it has
BARS_HEAD_TTL = 20.0
BARS_WORKERS = 4
BARS_FILL_EXTRA = 40         # windows read quietly after the answer
BARS_STORES = 24
BARS_IDLE = 900.0
BARS_LIFE = 3600.0
BARS_EMPTY_DAYS = 10         # days with no bars at all: history has begun
BAR_SUB_REACH = 20

# SPEC timeframe -> Capital.com resolution (documented: https://open-api.capital.com/,
# "MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK")
TIMEFRAMES = {"1m": ("MINUTE", 60_000), "5m": ("MINUTE_5", 300_000),
              "15m": ("MINUTE_15", 900_000), "30m": ("MINUTE_30", 1_800_000),
              "1h": ("HOUR", 3_600_000), "4h": ("HOUR_4", 14_400_000),
              "1d": ("DAY", 86_400_000)}

# Capital.com instrumentType -> the kit's asset_class words.
ASSET_CLASS = {"CURRENCIES": "fx", "SHARES": "stock", "INDICES": "index",
               "COMMODITIES": "commodity", "CRYPTOCURRENCIES": "crypto"}
# documented: https://help.capitalccuk.com/hc/en-us/articles/4409685697810
# (updated 2026-08-20): from 11 May 2026 most FX pairs and precious metals take
# the three-day overnight adjustment on Wednesday, USD/CAD and USD/TRY on
# Thursday; commodities (from 13 March 2026) on Friday.
PRECIOUS = ("GOLD", "SILVER", "PLATINUM", "PALLADIUM")
THURSDAY_FX = ("USDCAD", "USDTRY")

# Capital.com's activity `source` -> the SPEC's close reason (documented
# sources: CLOSE_OUT, DEALER, SL, SYSTEM, TP, USER; https://open-api.capital.com/).
CLOSE_REASON = {"SL": "sl", "TP": "tp", "CLOSE_OUT": "stop_out",
                "USER": "manual"}
D0 = Decimal(0)


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print("[capitalcom] %s" % msg, file=sys.stderr, flush=True)


def _one_line(text, limit: int) -> str:
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
    its text without tags. Raw, such a page would go into the message CRLFs
    and all, and flatten_all's failure list would quote it.

    The title's text is taken; the page is unescaped (an escaped tag
    revealed); then `scrub` takes the credentials out of the unescaped
    text, so a credential that itself holds '<' or '>' is hidden whole
    before the tags are taken out, or the stray '<'/'>' mapped, can cut it;
    then the tags are taken out whole, each one even if its own text holds
    a '>' (_TAG), and what prints as nothing is dropped (_visible); then
    `scrub` again (a credential a tag split); then any stray '<'/'>' becomes
    a space and the words are cut. Without these steps a credential split by
    a tag, a soft hyphen or a zero-width character reaches the words in
    readable pieces."""
    m = re.search(r"<title[^>]*>(.*?)</title\s*>", text, re.I | re.S)
    join = "" if m else " "
    text = scrub(html.unescape(m.group(1) if m else text))
    text = _visible(_TAG.sub(join, text))
    text = _one_line(scrub(text).replace("<", " ").replace(">", " "), limit)
    return re.sub(r"^%d\s+" % status, "", text) if status else text


# ---------------------------------------------------------------------------
# Numbers and times. Converted once, at the boundary.
# ---------------------------------------------------------------------------

def _num(v, default=None):
    """A broker number as a float, or `default`. Booleans are not numbers."""
    if isinstance(v, bool) or v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _dec(v) -> Decimal:
    """A number as an exact Decimal; 0 if absent. Floats go through repr so
    0.1 is 0.1, not 0.1000000000000000055."""
    if v is None or v == "" or isinstance(v, bool):
        return D0
    try:
        d = Decimal(repr(v) if isinstance(v, float) else str(v))
    except (decimal.InvalidOperation, ValueError):
        return D0
    return d if d.is_finite() else D0


def _fixed(v, places: int = 10) -> str:
    """Fixed-point text: never an exponent, no trailing zeros."""
    d = _dec(v).quantize(Decimal(1).scaleb(-places),
                         rounding=decimal.ROUND_HALF_EVEN)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _wire(v):
    """A quantity or a price for Capital.com's JSON: a number that is
    exactly the decimal the platform meant (0.1, not 0.1000000000000000055),
    an int when it is whole."""
    d = _dec(v)
    if d == d.to_integral_value():
        return int(d)
    return float(_fixed(d))


def _decimals(v) -> int:
    d = _dec(v)
    if d <= 0:
        return 0
    return max(0, -d.normalize().as_tuple().exponent)


def step_ok(qty: float, step: float) -> bool:
    """Is `qty` a whole multiple of `step`, as far as a float can say? (The
    reference adapter's test: on the distance to the nearest multiple, with
    what float arithmetic itself can put there and no more.)"""
    if not (step > 0) or not (qty > 0) or qty != qty or qty == math.inf:
        return False
    nearest = round(qty / step) * step
    return abs(qty - nearest) <= max(4 * math.ulp(qty), step * 1e-6)


def on_grid(price: float, tick: float) -> bool:
    if not (tick > 0) or price != price:
        return False
    nearest = round(price / tick) * tick
    return abs(price - nearest) <= max(4 * math.ulp(price), tick * 1e-6)


_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)(?:[Tt ](\d\d):(\d\d)(?::(\d\d))?"
                 r"(?:\.(\d+))?\s*(Z|z|[+-]\d\d:?\d\d)?)?$")


def parse_utc(text) -> int | None:
    """Capital.com's UTC times (`snapshotTimeUTC`, `createdDateUTC`,
    `dateUTC`, `dateUtc`: "2022-04-06T07:32:19.193", no zone letter) to UTC
    epoch ms, or None. documented: https://open-api.capital.com/ (each time is
    given twice, local and UTC; only the *UTC one is ever read here)."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text)
    if not isinstance(text, str):
        return None
    m = _TS.match(text.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, zone = m.groups()
    secs = calendar.timegm((int(y), int(mo), int(d), int(h or 0),
                            int(mi or 0), int(s or 0)))
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        secs -= sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)
    ms = int((frac or "0")[:3].ljust(3, "0"))
    return secs * 1000 + ms


def utc_text(ms: int) -> str:
    """UTC epoch ms to Capital.com's YYYY-MM-DDTHH:MM:SS (documented:
    https://open-api.capital.com/, from, to and goodTillDate, in UTC)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(ms) // 1000))


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Demo or live: decided cold, from configuration alone.
# ---------------------------------------------------------------------------

def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _scheme(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).scheme or "").lower()
    except ValueError:
        return ""


def _private(url: str, scheme: str) -> bool:
    """Nothing sent to `url` can be read on the way: it is `scheme` (https,
    wss), or it never leaves this machine (SPEC 8.5: the API key and its
    password travel in the login, the session's tokens in every request and
    every stream message)."""
    return _scheme(url) == scheme or _loopback(_host(url))


def _demo_endpoint(url: str) -> bool:
    """No real money can move through this REST endpoint: Capital.com's demo
    host by its exact name over https, or this machine (a test stand-in). A
    trailing dot, an address, a forwarder, plain http: none of those is
    KNOWN to be Capital.com's demo."""
    host = _host(url)
    return (host == DEMO_HOST and _scheme(url) == "https") or _loopback(host)


def _paper_stream(url: str) -> bool:
    """The stream carries prices only, and its one documented host is the
    same for demo and live; anything but that host (over wss) or this
    machine is not known to be what the demo account uses."""
    host = _host(url)
    return (host == STREAM_HOST and _scheme(url) == "wss") or _loopback(host)


def _where(url: str) -> str:
    """An endpoint as it may be quoted: its scheme and host, nothing else
    (a URL can carry a user name and a password)."""
    return "%s://%s" % (_scheme(url) or "?", _host(url) or "?")


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

    Every rule here judges the host urlparse names (`_host`), and urllib
    connects to all of what stands before the path. Where the two read a URL
    differently the host judged is not the host reached, and the rules mean
    nothing: https://<live host>@127.0.0.1 would be judged as this machine
    (mode "paper", plain http allowed) while urllib looks up "<live
    host>@127.0.0.1". So the host is read here as it is written, by this one
    rule, and must be the host urlsplit gives: nothing then depends on the
    interpreter's own parser (Python 3.10.0-3.10.15 and 3.11.0-3.11.3 read
    http://<name>[::1]:9 as '::1'), and urlsplit's dropping of tabs and line
    breaks (which urllib keeps) changes nothing either. An endpoint of
    Capital.com's has none of what is refused here."""
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return ("has a space, a tab or a line break in it",
                "write it again on one line")
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme:
        return ("is not a URL this adapter can read",
                "give it as scheme://host/path")
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


def _shape_fault(name: str, url: str) -> str:
    shape = _endpoint_shape(url)
    return "%s %s, %s" % (name, shape[0], shape[1]) if shape else ""


def _credential_fault(label: str, value: str, header: bool) -> str:
    """Why `value` cannot be what Capital.com asks for, in words that never
    contain it. A value with a line break in it would otherwise reach
    http.client, whose error message quotes a header value: the key, on its
    way to the platform (SPEC 8.5). The key travels in a header; the login
    email and the password travel in the JSON body, where only control
    characters are wrong."""
    if header and any(c.isspace() for c in value):
        return ("the %s has a space or a line break inside it; paste it "
                "again as one unbroken line" % label)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ("the %s has a line break or another control character in "
                "it; type it again" % label)
    if header and not all(33 <= ord(c) <= 126 for c in value):
        return ("the %s has a character in it that no Capital.com API key "
                "has; paste it again" % label)
    return ""


def resolve_environment(argv, environ) -> dict:
    """Which Capital.com this process talks to, with no network (SPEC 3, 8.2).

    DEMO IS THE DEFAULT. The environment is demo unless `--live` or
    CAPITALCOM_ENV=live is given (a flag wins over the variable; --paper and
    --demo both pin demo; --paper with --live is live, since live was asked
    for). `mode` is decided by the ENDPOINTS (see _demo_endpoint).
    """
    def cred(name):
        return environ.get("BRUE_CRED_" + name) or environ.get(
            "CAPITALCOM_" + name) or ""
    # The key and the email are stripped (a pasted line break); the password
    # loses only line breaks at its ends: a space may be part of it.
    key = cred("API_KEY").strip()
    ident = cred("IDENTIFIER").strip()
    password = cred("PASSWORD").strip("\r\n")
    fault = (_credential_fault("API key", key, True)
             or _credential_fault("login email", ident, False)
             or _credential_fault("API key password", password, False))
    argv = list(argv or [])
    demo_flag = "--paper" in argv or "--demo" in argv
    live_flag = "--live" in argv
    named = (environ.get("CAPITALCOM_ENV") or "").strip().lower()
    if demo_flag and live_flag:
        env, why = "live", "both --paper and --live given; live assumed"
    elif live_flag:
        env, why = "live", "flag --live"
    elif demo_flag:
        env, why = "demo", "flag --paper"
    elif named == "live":
        env, why = "live", "CAPITALCOM_ENV=live"
    elif named in ("demo", "paper"):
        env, why = "demo", "CAPITALCOM_ENV=" + named
    elif named:
        env, why = "demo", ("CAPITALCOM_ENV is neither demo nor live; demo "
                            "is used (live must be asked for)")
    else:
        env, why = "demo", "live was not asked for"
    home = DEMO_API_URL if env == "demo" else LIVE_API_URL
    api = ((environ.get("CAPITALCOM_API_URL") or "").strip()
           or home).rstrip("/")
    try:
        parsed = urllib.parse.urlparse(api)
        if parsed.path in ("", "/"):
            api += "/api/v1"     # a bare host: the documented root
    except ValueError:
        pass
    stream = (environ.get("CAPITALCOM_STREAM_URL") or "").strip() \
        or STREAM_URL
    # An endpoint that cannot be used is NOT used, and the first call that
    # needs Capital.com says why (endpoint_fault): the process keeps
    # Capital.com's own endpoint for its environment, so nothing can reach
    # the other one by any path. Two rules:
    # - nothing that carries a credential or a session token travels in
    #   clear to another machine (SPEC 8.5), in either environment;
    # - DEMO IS DEMO: the default, --paper and --demo talk to Capital.com's
    #   demo host or this machine only, whatever CAPITALCOM_API_URL says
    #   (a variable left in a shell must not take the Terminal's demo row to
    #   the live host). Another host is for --live alone.
    # First, in either environment: the host those rules judge must be the
    # host that is reached (_endpoint_shape).
    faults = []
    shape = _shape_fault("CAPITALCOM_API_URL", api)
    if shape:
        faults.append(shape)
        api = home
    shape = _shape_fault("CAPITALCOM_STREAM_URL", stream)
    if shape:
        faults.append(shape)
        stream = STREAM_URL
    if not _private(api, "https"):
        faults.append("CAPITALCOM_API_URL is %s, not https: the API key and "
                      "its password would travel in clear" % _where(api))
        api = home
    if not _private(stream, "wss"):
        faults.append("CAPITALCOM_STREAM_URL is %s, not wss: the session's "
                      "tokens would travel in clear" % _where(stream))
        stream = STREAM_URL
    if env == "demo" and not _demo_endpoint(api):
        faults.append("CAPITALCOM_API_URL names %s, which is not Capital.com's "
                      "demo host: demo (the default, and --paper) talks to "
                      "%s or to this machine only; give --live to use another "
                      "host" % (_where(api), DEMO_HOST))
        api = DEMO_API_URL
    if env == "demo" and not _paper_stream(stream):
        faults.append("CAPITALCOM_STREAM_URL names %s, which is not "
                      "Capital.com's streaming host: demo (the default, and "
                      "--paper) streams from %s or from this machine only"
                      % (_where(stream), STREAM_HOST))
        stream = STREAM_URL
    mode = ("paper" if _demo_endpoint(api) and _paper_stream(stream)
            else "live")
    return {"env": env, "why": why, "mode": mode, "key": key,
            "identifier": ident, "password": password, "cred_fault": fault,
            "api_url": api, "stream_url": stream,
            "endpoint_fault": "; ".join(faults)}


# ---------------------------------------------------------------------------
# RFC 6455 client. Standard library only: socket, ssl, struct, base64, hashlib.
# (The reference adapter's.)
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = (0x0, 0x1, 0x2,
                                                              0x8, 0x9, 0xA)


class WSClosed(OSError):
    """The connection is over: closed by the peer, or broken."""

    def __init__(self, reason: str, code: int | None = None):
        super().__init__(reason)
        self.code = code


class WSTimeout(Exception):
    """Nothing complete arrived in time. The connection is still good."""


class _WebSocket:
    """One client WebSocket connection, hand-rolled.

    Text and binary messages are both delivered, decoded as UTF-8. Client
    frames are masked; the 16- and 64-bit length forms; fragmented
    messages, with control frames arriving between the pieces; pings
    answered with their own payload. A frame is taken out of the buffer only
    when all of it has arrived, so a read that times out on a quiet stream
    leaves the connection usable and the caller can ping. `close()` is safe
    from another thread.
    """

    def __init__(self, url: str, timeout: float = 30.0, headers=None,
                 max_message: int = 4 * 1024 * 1024):
        u = urllib.parse.urlparse(url)
        secure = u.scheme == "wss"
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self._buf = b""
        self._frag_op = None
        self._frag = b""
        self._wlock = threading.Lock()
        self._max = max_message
        self.close_code = None
        self.last_rx = time.monotonic()
        self._sock = socket.create_connection((host, port), timeout=10.0)
        try:
            if secure:
                self._sock = ssl.create_default_context().wrap_socket(
                    self._sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            hostport = host if port in (80, 443) else "%s:%d" % (host, port)
            lines = ["GET %s HTTP/1.1" % path, "Host: %s" % hostport,
                     "Upgrade: websocket", "Connection: Upgrade",
                     "Sec-WebSocket-Key: %s" % key,
                     "Sec-WebSocket-Version: 13",
                     "User-Agent: %s" % USER_AGENT]
            for name, value in (headers or {}).items():
                lines.append("%s: %s" % (name, value))
            self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n")
                               .encode("ascii"))
            head = self._read_head()
            first = head.split("\r\n")[0]
            if " 101" not in first:
                raise WSClosed("websocket upgrade refused: %s" % first[:120])
            want = base64.b64encode(hashlib.sha1(
                (key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            got = ""
            for line in head.split("\r\n")[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "sec-websocket-accept":
                    got = value.strip()
            if got != want:
                raise WSClosed("bad Sec-WebSocket-Accept")
            self._sock.settimeout(timeout)
        except BaseException:
            try:
                self._sock.close()
            except OSError:
                pass
            raise

    def _read_head(self) -> str:
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSClosed("connection closed during the handshake")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise WSClosed("handshake answer too long")
        head, _, self._buf = self._buf.partition(b"\r\n\r\n")
        return head.decode("latin-1")

    def _take_frame(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        length, pos = b1 & 0x7F, 2
        if length == 126:
            if len(buf) < 4:
                return None
            length, pos = struct.unpack("!H", buf[2:4])[0], 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length, pos = struct.unpack("!Q", buf[2:10])[0], 10
        if length > self._max:
            raise WSClosed("frame of %d bytes is over the %d cap"
                           % (length, self._max), 1009)
        mask = None
        if b1 & 0x80:
            if len(buf) < pos + 4:
                return None
            mask, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + length:
            return None
        data = buf[pos:pos + length]
        self._buf = buf[pos + length:]
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return bool(b0 & 0x80), b0 & 0x0F, data

    def recv(self, timeout: float | None = None):
        """The next message as text, or None once the peer has closed.
        Raises WSTimeout when nothing complete arrived within `timeout`."""
        if timeout is not None:
            self._sock.settimeout(timeout)
        while True:
            frame = self._take_frame()
            if frame is None:
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    raise WSTimeout() from None
                except ssl.SSLWantReadError:
                    continue
                if not chunk:
                    raise WSClosed("connection closed by the peer")
                self._buf += chunk
                continue
            self.last_rx = time.monotonic()
            fin, opcode, data = frame
            if opcode == _OP_PING:
                self._frame(_OP_PONG, data)
            elif opcode == _OP_PONG:
                pass
            elif opcode == _OP_CLOSE:
                if len(data) >= 2:
                    self.close_code = struct.unpack("!H", data[:2])[0]
                try:
                    self._frame(_OP_CLOSE, data[:2])
                except OSError:
                    pass
                return None
            elif opcode in (_OP_TEXT, _OP_BIN):
                if fin:
                    return data.decode("utf-8", "replace")
                self._frag_op, self._frag = opcode, data
            elif opcode == _OP_CONT and self._frag_op is not None:
                self._frag += data
                if len(self._frag) > self._max:
                    raise WSClosed("message over the %d cap" % self._max,
                                   1009)
                if fin:
                    whole, self._frag_op, self._frag = self._frag, None, b""
                    return whole.decode("utf-8", "replace")

    def _frame(self, opcode: int, payload: bytes = b""):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._wlock:
            self._sock.sendall(bytes(header) + body)

    def send_json(self, obj):
        self._frame(_OP_TEXT, json.dumps(obj).encode("utf-8"))

    def close(self):
        try:
            self._frame(_OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Capital.com's refusals, and what they are in the SPEC's vocabulary.
# ---------------------------------------------------------------------------

class CapitalError(Exception):
    """Capital.com answered with an error, or could not be reached (status 0).

    `code` is Capital.com's own errorCode (documented body {"errorCode": ...}:
    https://github.com/capital-com-sv/capital-api-postman), or one of this
    adapter's: "budget" (not sent: this adapter's own pace), "deadline" (not
    sent: the call's time is up), "flatten" (not sent: flatten_all was asked
    for). `sent` is False only when the request certainly did not reach
    Capital.com; an order whose request may have arrived is of unknown fate.
    Carries nothing from the request: a handler exception is forwarded to the
    platform, so it must never be able to hold a credential.
    """

    def __init__(self, status: int, message: str, code=None, sent=True,
                 retry_after: float | None = None, cause: str | None = None,
                 answered: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.sent = sent
        self.retry_after = retry_after
        # For a request that went out with no usable answer (status 0): what
        # ended the wait (an exception's name), or, with `answered`, what
        # was wrong with the answer that came (see unanswered()).
        self.cause = cause
        self.answered = answered

    @property
    def unanswered(self) -> bool:
        """The request may have been carried out and no verdict came back:
        no answer at all, a broken one, a gateway's 5xx/408, or a redirect
        (never followed) other than 307 and 308, which say the request was
        not carried out at that address."""
        if self.code == "redirect":
            return self.sent and self.status not in (307, 308)
        return self.sent and (self.status == 0 or self.status >= 500
                              or self.status == 408) and self.code not in (
            "budget", "deadline", "flatten")


def unanswered(e: CapitalError) -> str:
    """Why a request's fate is not known, in a few words that follow "Not
    known yet whether Capital.com took the order: " (or closed a position):
    "it did not answer (TimeoutError)", "its answer could not be read (not
    JSON, 17 bytes)", "it answered HTTP 502 (Bad Gateway)". The broker's
    name is in the sentence once, before these words (not twice, as in
    "Capital.com did not answer (Capital.com's answer was not JSON (17
    bytes)); whether it took the order is not known yet")."""
    if e.status == 0 and e.answered:
        return ("its answer could not be read (%s)" % e.cause if e.cause
                else "its answer could not be read")
    if e.status == 0:
        return ("it did not answer (%s)" % e.cause if e.cause
                else "it did not answer")
    if e.code == "redirect":
        return ("it answered HTTP %d, a redirect, which is not followed"
                % e.status)
    words = _one_line(e.message or "", 80)
    if not words or words == "HTTP %d" % e.status:
        return "it answered HTTP %d" % e.status
    return "it answered HTTP %d (%s)" % (e.status, words)


# errorCode prefix -> (SPEC code, like, retryable). First match wins. The
# documented codes (Postman: https://github.com/capital-com-sv/capital-api-postman)
# are marked D; the rest are assumed, verify on demo (named after the
# documented pattern "error.invalid.<field>").
ERROR_MAP = (
    ("error.too-many.requests", "rate_limited", None, True),          # D
    ("error.not-found.epic", "unknown_symbol", None, False),          # D
    ("error.not-found.dealid", "not_found", None, False),             # D
    ("error.not-found.dealreference", "not_found", None, False),      # D
    ("error.invalid.stoploss", "invalid_stops", None, False),         # D
    ("error.invalid.takeprofit", "invalid_stops", None, False),       # D
    ("error.not-in-future.goodtilldate", "invalid_expiry", None, False),  # D
    ("error.invalid.goodtilldate", "invalid_expiry", None, False),
    ("error.invalid.accountid", "x_capitalcom_account", "not_found", False),  # D
    ("error.invalid.size", "invalid_qty", None, False),
    ("error.invalid.level", "x_capitalcom_invalid_level", "invalid_stops",
     False),
    ("error.invalid.trailingstop", "x_capitalcom_no_trailing",
     "trade_restricted", False),
    ("error.invalid.guaranteedstop", "x_capitalcom_no_guaranteed_stop",
     "trade_restricted", False),
    ("error.invalid.market.viewonly", "trade_restricted", None, False),
    ("error.invalid.resolution", "unsupported", None, False),          # D
    ("error.null.epic", "unknown_symbol", None, False),                # D
    ("error.invalid.details", "unauthorized", None, False),            # D
    ("error.null.api.key", "unauthorized", None, False),               # D
)


def to_adapter_error(e: CapitalError, env: str = "",
                     scrub=None) -> AdapterError:
    """One Capital.com refusal as the error a platform can act on.
    Capital.com's own code is always kept in the message."""
    scrub = scrub or (lambda s: s)
    code = (e.code or "") if isinstance(e.code, str) else ""
    low = code.lower()
    said = scrub("Capital.com: %s (HTTP %d)" % (code, e.status) if code
                 else "Capital.com answered HTTP %d" % e.status
                 if e.status else "Capital.com: " + e.message)
    if e.status == 0 or code in ("budget", "deadline", "flatten"):
        if code == "flatten":
            return AdapterError(
                "x_capitalcom_flattening", "flatten_all was asked for while "
                "this call was being answered, and it was stopped before "
                "sending anything more. flatten_all is closing every "
                "position and cancelling every working order.",
                retryable=True, like="frozen")
        if code == "deadline":
            return AdapterError(
                "internal", "Capital.com was too slow: the %d s this call "
                "has ran out before it was finished. Ask again; the same "
                "client_id is safe." % HANDLER_BUDGET, retryable=True)
        if code == "budget":
            return AdapterError("rate_limited", "not sent, by this "
                                "adapter's own limit: " + scrub(e.message),
                                retryable=True)
        return AdapterError("internal", "Capital.com could not be reached: "
                            + scrub(e.message), retryable=True)
    if e.status == 429:
        return AdapterError("rate_limited", said, retryable=True)
    if code == "redirect":
        # Not followed (CapitalClient._http): the same endpoint answers the
        # same until it is changed, so asking again is no remedy.
        return AdapterError("x_capitalcom_redirected", scrub(e.message),
                            like="internal")
    if e.status in (401, 403):
        log("refused on the %s host (HTTP %d %s); this adapter talks to "
            "Capital.com's demo host unless --live or CAPITALCOM_ENV=live is "
            "given" % (env or "?", e.status, code))
        return AdapterError(
            "unauthorized", "Capital.com refused this login on its %s host "
            "(%s). Check the API key, the login email and the API key's own "
            "password (not your login password). A demo key works on the demo "
            "host only and a live key on the live host only." % (
                env or "?", code or "HTTP %d" % e.status))
    for prefix, spec, like, retryable in ERROR_MAP:
        if low.startswith(prefix):
            return AdapterError(spec, said, retryable=retryable, like=like)
    if e.status >= 500:
        return AdapterError("internal", said, retryable=True)
    if e.status == 404:
        return AdapterError("not_found", said)
    return AdapterError("x_capitalcom_%d" % e.status if not code else
                        "x_capitalcom_" + re.sub(r"[^a-z0-9]+", "_",
                                                 low).strip("_")[:40], said)


# A REJECTED confirmation's `reason` -> SPEC code. The field is in Capital.com's
# own saved example ("reason": "SUCCESS" on an accepted deal: documented,
# https://github.com/capital-com-sv/capital-api-postman, "Successful response"
# of GET /confirms); the words it takes on a refusal are NOT documented
# (assumed, verify on demo), so they are matched loosely and anything else is
# an x_ code that keeps Capital.com's word.
def reject_code(reason) -> tuple:
    word = str(reason or "").upper()
    if any(w in word for w in ("INSUFFICIENT", "FUNDS", "MARGIN")):
        return "insufficient_margin", None
    if any(w in word for w in ("MARKET_CLOSED", "CLOSED", "OFFLINE",
                               "NOT_OPEN")):
        return "market_closed", None
    if "SIZE" in word or "QUANTITY" in word:
        return "invalid_qty", None
    if any(w in word for w in ("STOP", "LIMIT", "PROFIT", "LEVEL")):
        return "invalid_stops", None
    if "PRICE" in word:
        return "x_capitalcom_price", "requote"
    return "x_capitalcom_rejected", None


# ---------------------------------------------------------------------------
# The pace: what the adapter may still ask of Capital.com, kept in one place.
# ---------------------------------------------------------------------------

class _Pace:
    """Every request to Capital.com takes a place here first.

    Capital.com counts ten requests a second per user over every endpoint
    (documented), so this keeps to eight, by priority: flatten_all (which
    may use a ninth), then calls that change something, then a handler's
    reads and a chart's newest end, then quote snapshots, then a chart's
    windows, then the adapter's own polling, which also has a ceiling of its
    own so that a handler is never queued behind it. A low-ranked request
    that has waited two seconds moves up, so polling cannot be starved by a
    chart. Order posts are kept 0.12 s apart and logins 1.1 s apart, and on
    demo the hour's 1,000 order posts are counted.

    `take` waits only as long as its caller can afford; past that it raises
    the "budget" error: nothing was sent, and the platform is told so.
    """

    RANK = {"flatten": 6, "write": 5, "login": 5, "read": 4, "head": 4,
            "quotes": 3, "bulk": 2, "background": 1, "idle": 0}
    SUB = {"bulk": BULK_PER_SECOND, "background": BACKGROUND_PER_SECOND,
           "idle": IDLE_PER_SECOND}

    def __init__(self, per_second=PACE_PER_SECOND, hour_cap=None):
        self.per_second = per_second
        self.hour_cap = hour_cap
        self._lock = threading.Lock()
        self._sec: collections.deque = collections.deque()
        self._queue: list = []
        self._blocked = 0.0
        self._order_last = -1e9
        self._order_hour: collections.deque = collections.deque()
        self._login_last = -1e9
        self._in_hand = 0

    def in_hand(self, delta: int):
        """A call that changes something was read (+1) or answered (-1).
        While one is in hand, everything below a handler's read leaves
        three places a second free: an order's work is a chain of requests,
        each asked when the one before has answered, and between two of
        them it is not waiting (the reference adapter's rule)."""
        with self._lock:
            self._in_hand = max(0, self._in_hand + delta)

    def _rank(self, w: dict, now: float) -> float:
        r = w["rank"]
        if w["kind"] in self.SUB and now - w["since"] >= 2.0:
            r += 2.5
        return r

    def _wait_needed(self, w: dict, now: float) -> float:
        kind = w["kind"]
        while self._sec and now - self._sec[0][0] >= 1.0:
            self._sec.popleft()
        if kind in ("flatten", "write", "login"):
            limit = FLATTEN_PER_SECOND
        elif self._in_hand and self.RANK.get(kind, 1) < self.RANK["read"]:
            limit = max(1, self.per_second - 3)
        else:
            limit = self.per_second
        wait = 0.0 if kind == "flatten" else max(0.0, self._blocked - now)
        if len(self._sec) >= limit:
            wait = max(wait, self._sec[len(self._sec) - limit][0] + 1.0 - now)
        sub = self.SUB.get(kind)
        if sub:
            mine = [t for t, k in self._sec if k == kind]
            if len(mine) >= sub:
                wait = max(wait, mine[len(mine) - sub] + 1.0 - now)
            if self._sec:
                # evenly, not in bursts: the next order finds a place at once
                wait = max(wait, self._sec[-1][0] + 1.0 / self.per_second
                           - 0.01 - now)
        return wait

    def take(self, kind: str = "read", max_wait: float = 1.5,
             rank: float | None = None):
        me = {"kind": kind, "since": time.monotonic(),
              "rank": self.RANK.get(kind, 1) if rank is None else rank}
        deadline = me["since"] + max(0.0, max_wait)
        with self._lock:
            self._queue.append(me)
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    wait = self._wait_needed(me, now)
                    if wait <= 0:
                        mine = self._rank(me, now)
                        if any(w is not me and self._rank(w, now) > mine
                               and self._wait_needed(w, now) <= 0
                               for w in self._queue):
                            wait = 0.01
                    if wait <= 0:
                        self._sec.append((now, kind))
                        return
                    paused = (kind != "flatten"
                              and self._blocked - now >= wait - 1e-6)
                if now + wait > deadline:
                    if paused:
                        text = ("Capital.com asked for a pause (HTTP 429); "
                                "retry in %.1fs" % wait)
                    else:
                        text = ("this adapter sends Capital.com at most %d "
                                "requests a second and more urgent ones went "
                                "first; nothing was sent; retry in %.1fs"
                                % (self.per_second, wait))
                    raise CapitalError(429, text, code="budget", sent=False,
                                       retry_after=wait)
                time.sleep(min(wait, 0.05) + 0.002)
        finally:
            with self._lock:
                self._queue.remove(me)

    def take_order(self, max_wait: float):
        """An order post's own place: 0.12 s after the last one, and on demo
        inside the hour's 1,000 (documented: https://open-api.capital.com/)."""
        end = time.monotonic() + max(0.0, max_wait)
        while True:
            with self._lock:
                now = time.monotonic()
                while self._order_hour and now - self._order_hour[0] >= 3600:
                    self._order_hour.popleft()
                if (self.hour_cap and len(self._order_hour)
                        >= self.hour_cap - ORDERS_HOUR_KEEP):
                    left = 3600 - (now - self._order_hour[0])
                    raise CapitalError(
                        429, "Capital.com's demo takes %d new orders an hour "
                        "and this hour's are used up; nothing was sent; "
                        "retry in %d min" % (self.hour_cap, left // 60 + 1),
                        code="budget", sent=False, retry_after=left)
                wait = self._order_last + ORDER_GAP - now
                if wait <= 0:
                    self._order_last = now
                    self._order_hour.append(now)
                    return
            if now + wait > end:
                raise CapitalError(429, "another order was just sent; "
                                   "nothing was sent; retry", code="budget",
                                   sent=False, retry_after=wait)
            time.sleep(wait + 0.002)

    def take_login(self, max_wait: float):
        end = time.monotonic() + max(0.0, max_wait)
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._login_last + LOGIN_GAP - now
                if wait <= 0:
                    self._login_last = now
                    return
            if now + wait > end:
                raise CapitalError(429, "Capital.com takes one login a "
                                   "second; nothing was sent; retry",
                                   code="budget", sent=False,
                                   retry_after=wait)
            time.sleep(wait + 0.002)

    def block(self, seconds: float):
        """Capital.com said 429: nothing more until then (capped; flatten_all
        does not honour it, see take)."""
        with self._lock:
            self._blocked = max(self._blocked, time.monotonic() + max(
                0.2, min(seconds, BLOCK_CAP)))


# ---------------------------------------------------------------------------
# REST client: one host, one session, typed errors.
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: the API key, the login and the
    session's tokens go to the endpoint this process was given and nowhere
    else. urllib's own handler follows one to any host, the X-Cap-Api-Key
    header with it. The answer comes back as the 3xx it is (see
    CapitalClient._http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CapitalClient:
    """Capital.com over HTTPS. One request, one connection.

    The session: POST /session with the API key in X-CAP-API-KEY and the
    login email and the key's password in the body (encryptedPassword false:
    plain over TLS; the encrypted form needs RSA, which the standard library
    lacks). documented: https://open-api.capital.com/ (the CST and
    X-SECURITY-TOKEN response headers, "valid for 10 minutes after the last
    use"). Tokens are published to other threads only after the session has
    been switched to the bound account (see `login`).
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["api_url"]
        self.pace = _Pace(hour_cap=DEMO_ORDERS_PER_HOUR
                          if cfg["env"] == "demo" else None)
        self._ctx = ssl.create_default_context()
        # Straight to the endpoint: no proxy (a proxy named in the
        # environment would take a plain http endpoint on this machine, the
        # login in clear; on Windows urllib also reads the system's), and no
        # redirect followed (_NoRedirect).
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=self._ctx))
        self._tl = threading.local()
        self.stand_down = threading.Event()
        self._login_lock = threading.Lock()
        self._lock = threading.Lock()
        self._cst = self._xst = None
        self.gen = 0
        self.last_use = 0.0
        self.info: dict = {}          # the last login's answer (no token)
        self.bound = ""               # the account every session is put on
        self.current = ""             # the account the live session is on
        self.logins = 0
        self.on_login = None          # called with the new generation
        self.skew_ms = 0              # Capital.com's clock less ours
        self.refused = None           # the last login refusal (AdapterError)

    # -- one deadline per handler (the reference adapter's) ---------------
    def set_deadline(self, seconds: float | None):
        self._tl.deadline = (None if seconds is None
                             else time.monotonic() + seconds)
        self._tl.read_at, self._tl.rank, self._tl.yields = None, None, False

    def set_request(self, read_at: float, budget: float, floor: float = 0.0,
                    yields: bool = False, rank: float | None = None):
        self._tl.read_at, self._tl.yields, self._tl.rank = read_at, yields, rank
        self._tl.deadline = max(read_at + budget, time.monotonic() + floor)

    def read_at(self) -> float | None:
        return getattr(self._tl, "read_at", None)

    def time_left(self) -> float | None:
        d = getattr(self._tl, "deadline", None)
        return None if d is None else d - time.monotonic()

    def _yield_to_flatten(self):
        if self.stand_down.is_set() and getattr(self._tl, "yields", False):
            raise CapitalError(0, "flatten_all was asked for", code="flatten",
                               sent=False)

    def pause(self, seconds: float):
        """Sleep, but never past the handler's deadline."""
        left = self.time_left()
        if left is not None and left <= 0.05:
            raise CapitalError(0, "the time for this call is used up",
                               code="deadline", sent=False)
        time.sleep(seconds if left is None else max(0.0, min(seconds, left)))

    def scrub(self, text: str) -> str:
        """`text` with every credential and token taken out, whatever put
        them there (SPEC 8.5), also where white space or a character that
        prints as nothing splits one (_hide)."""
        with self._lock:
            tokens = (self._cst, self._xst)
        return _hide(text, (self.cfg.get("password"), self.cfg.get("key"),
                            self.cfg.get("identifier")) + tokens)

    def broker_ms(self) -> int:
        return int(time.time() * 1000) + self.skew_ms

    # -- the session ------------------------------------------------------
    def has_session(self) -> bool:
        with self._lock:
            return bool(self._cst)

    def tokens(self) -> tuple | None:
        with self._lock:
            return (self._cst, self._xst, self.gen) if self._cst else None

    def session(self, kind: str = "read", max_wait: float = 2.5):
        """(cst, token, generation), logging in when there is none."""
        t = self.tokens()
        if t:
            return t
        self.login(kind, max_wait)
        t = self.tokens()
        if not t:
            raise CapitalError(0, "the login made no session", code="session",
                               sent=False)
        return t

    def lapsed(self, gen: int):
        """The tokens of generation `gen` were refused: they are dropped (a
        newer login is kept)."""
        with self._lock:
            if self.gen == gen and self._cst:
                self._cst = self._xst = None

    def login(self, kind: str = "read", max_wait: float = 2.5):
        """POST /session, then PUT /session to the bound account.

        A new session starts on the login's own current account, which is
        not necessarily this process's (assumed, verify on demo: the
        login's `currentAccountId` is its preferred account). An order sent
        on a fresh session before the switch would land on the wrong
        account, so the tokens are published to other threads only after
        it. documented: https://open-api.capital.com/ (PUT /session
        {accountId}; "WebSocket streaming falls off when the financial
        account is changed", which the stream thread hears through
        `on_login`).
        """
        with self._login_lock:
            gen = self._login_locked(kind, max_wait)
        self._announce(gen)

    def _announce(self, gen: int | None):
        """A new session's generation to its listener (the stream), outside
        the login lock."""
        cb = self.on_login
        if gen is None or cb is None:
            return
        try:
            cb(gen)
        except Exception as e:  # noqa: BLE001 - a listener must not break a login
            log("after the login: %s: %s" % (type(e).__name__,
                                             self.scrub(str(e))))

    def rebind(self, kind: str = "write", max_wait: float = 3.0):
        """Put the session that exists on the bound account (PUT /session
        {accountId}; documented: https://open-api.capital.com/).

        Under the login lock, so no other thread logs in meanwhile, and
        NEVER through call(): a 401 there logs in again, and the login takes
        this same lock (a deadlock when the tokens of a login made for
        accounts.list lapse, documented "valid for 10 minutes after the
        last use", before the session is opened). A 401
        here is a refusal, so nothing was switched: those tokens are dropped
        and a new session is made, which login switches to the bound account
        before its tokens are published."""
        gen = None
        with self._login_lock:
            t = self.tokens()
            if t is None:
                gen = self._login_locked(kind, max_wait)
            elif self.bound and self.current != self.bound:
                left = self.time_left()
                timeout = HANDLER_TIMEOUT if left is None else max(0.2, left)
                self.pace.take(kind, max_wait)
                try:
                    self._send_once("PUT", self.base + "/session",
                                    {"CST": t[0], "X-SECURITY-TOKEN": t[1]},
                                    {"accountId": self.bound}, timeout, kind)
                    switched = True
                except CapitalError as e:
                    if e.status == 401:
                        switched = False
                        self.lapsed(t[2])
                        gen = self._login_locked(kind, max_wait)
                    elif e.status == 400 and "not-different" in str(
                            e.code or ""):
                        # assumed, verify on demo (see _login_locked)
                        switched = True
                    else:
                        raise
                if switched:
                    with self._lock:
                        self.current = self.bound
                        self.last_use = time.monotonic()
        self._announce(gen)

    def _login_locked(self, kind: str, max_wait: float) -> int | None:
        """The login itself; the caller holds the login lock. The new
        session's generation, or None when there is a session already."""
        if self.has_session():
            return None
        cfg = self.cfg
        if not (cfg["key"] and cfg["identifier"] and cfg["password"]):
            raise CapitalError(401, "the API key, the login email or the "
                               "API key's password is missing",
                               code="missing", sent=False)
        if cfg.get("cred_fault"):
            raise CapitalError(401, cfg["cred_fault"], code="fault",
                               sent=False)
        left = self.time_left()
        timeout = HANDLER_TIMEOUT if left is None else max(0.2, left)
        for attempt in (0, 1):
            self.pace.take_login(max_wait)
            self.pace.take(kind, max_wait)
            try:
                status, hdrs, raw = self._http(
                    "POST", self.base + "/session",
                    {"X-CAP-API-KEY": cfg["key"]},
                    {"identifier": cfg["identifier"],
                     "password": cfg["password"],
                     "encryptedPassword": False}, timeout)
                break
            except CapitalError as e:
                # One login a second per KEY (documented), whichever
                # process made the last one: another start a moment ago
                # is waited out once, inside the call's own time.
                left = self.time_left()
                if (e.status != 429 or attempt
                        or (left is not None and left < 2.5)):
                    raise
                time.sleep(1.05)
        cst = (hdrs.get("CST") or "").strip()
        xst = (hdrs.get("X-SECURITY-TOKEN") or "").strip()
        try:
            info = json.loads(raw) if raw else {}
        except ValueError:
            info = {}
        if not cst or not xst or not isinstance(info, dict):
            raise CapitalError(0, "Capital.com's answer to the login "
                               "carried no session", sent=True)
        cur = str(info.get("currentAccountId") or "")
        bound = self.bound
        if bound and cur != bound:
            ids = [str(a.get("accountId")) for a in
                   info.get("accounts") or [] if isinstance(a, dict)]
            if ids and bound not in ids:
                raise CapitalError(400, "account %s is not one of this "
                                   "login's" % bound,
                                   code="error.invalid.accountId")
            self.pace.take(kind, max_wait)
            try:
                self._http("PUT", self.base + "/session",
                           {"CST": cst, "X-SECURITY-TOKEN": xst},
                           {"accountId": bound}, timeout)
            except CapitalError as e:
                # assumed, verify on demo (reported by users, not
                # documented): a switch to the account already in use is
                # refused with error.not-different.accountId
                if not (e.status == 400
                        and "not-different" in str(e.code or "")):
                    raise
            cur = bound
        with self._lock:
            self._cst, self._xst = cst, xst
            self.gen += 1
            gen = self.gen
            self.current, self.info = cur, info
            self.last_use = time.monotonic()
            self.logins += 1
            self.refused = None
        return gen

    def logout(self):
        t = self.tokens()
        if not t:
            return
        try:
            self._http("DELETE", self.base + "/session",
                       {"CST": t[0], "X-SECURITY-TOKEN": t[1]}, None, 2.0)
        except CapitalError:
            pass
        self.lapsed(t[2])

    # -- one request ------------------------------------------------------
    def call(self, method: str, path: str, query: dict | None = None,
             body=None, kind: str = "read", max_wait: float = 2.5,
             timeout: float = HANDLER_TIMEOUT, auth: bool = True,
             order_post: bool = False, until: float | None = None,
             _again: bool = True):
        """Parsed JSON, or None for an empty answer. Raises CapitalError."""
        self._yield_to_flatten()
        left = self.time_left()
        if left is not None:
            if left <= 0.05:
                raise CapitalError(0, "the time for this call is used up",
                                   code="deadline", sent=False)
            if kind in ("read", "head"):
                # a handler's read waits its turn inside its own time
                max_wait = max(max_wait, left - 1.5)
            timeout, max_wait = min(timeout, left), min(max_wait, left)
        rank = getattr(self._tl, "rank", None)
        t = self.session(kind, max_wait) if auth else None
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None},
                safe=",:")
        self.pace.take(kind, max_wait, rank)
        if order_post:
            left = self.time_left()
            self.pace.take_order(max_wait if left is None
                                 else max(0.0, min(max_wait, left - 0.3)))
        if until is not None:
            room = until - time.monotonic()
            if room <= 0.2:
                raise CapitalError(0, "the time for this call is used up",
                                   code="deadline", sent=False)
            timeout = min(timeout, room)
        self._yield_to_flatten()
        headers = {}
        if t:
            headers = {"CST": t[0], "X-SECURITY-TOKEN": t[1]}
        try:
            status, hdrs, raw = self._send_once(method, url, headers, body,
                                                timeout, kind)
        except CapitalError as e:
            if auth and _again and e.status == 401:
                # The tokens lapsed (ten minutes after their last use, or the
                # session was ended): a 401 is a refusal, nothing was carried
                # out, so the request goes once more on a fresh session.
                # assumed, verify on demo: the status of a lapsed session
                # (Capital.com's own client reads 401 as an expired session,
                # documented: https://github.com/capital-com-sv/capital-mcp)
                self.lapsed(t[2])
                return self.call(method, path, query, body, kind, max_wait,
                                 timeout, auth, order_post, until,
                                 _again=False)
            raise
        if auth:
            self.last_use = time.monotonic()
        if not raw:
            return None
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError:
            if (hdrs.get("Content-Type") or "").startswith("text/plain"):
                return {"text": raw[:200].decode("utf-8", "replace")}
            raise CapitalError(0, "Capital.com's answer was not JSON (%d "
                               "bytes)" % len(raw), sent=True,
                               cause="not JSON, %d bytes" % len(raw),
                               answered=True) from None

    def _send_once(self, method: str, url: str, headers: dict, body,
                   timeout: float, kind: str):
        """One request, sent again when Capital.com answers 429.

        Capital.com counts ten requests a second per LOGIN, over every
        process and tool on it, and its 429 is a refusal before anything is
        done (documented: "Otherwise the position/order requests are going
        to be rejected"), so the same request is safe to send again. Seen
        against the stand-in: an adapter started right after another one on
        the same login (a restart, a switch of account) was refused for the
        other's last requests. So the request goes again after a short
        pause, inside the call's own time: flatten_all a few times (it must
        succeed while other calls are being rate-limited, SPEC 8.1), a read
        three times, anything else twice."""
        tries = 5 if kind == "flatten" else 3 if method == "GET" else 2
        while True:
            tries -= 1
            try:
                return self._http(method, url, headers, body, timeout)
            except CapitalError as e:
                left = self.time_left()
                pause = min(0.6, e.retry_after or 0.4)
                if (e.status != 429 or tries <= 0
                        or (left is not None and left < pause + 0.8)):
                    raise
                time.sleep(pause)

    def _http(self, method: str, url: str, headers: dict, body,
              timeout: float):
        if self.cfg.get("endpoint_fault"):
            # an endpoint that cannot be used is never reached (see
            # resolve_environment); the handlers say why before this
            raise CapitalError(0, self.cfg["endpoint_fault"], code="endpoint",
                               sent=False)
        data = None if body is None else json.dumps(body).encode("utf-8")
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers)
        try:
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=hdrs)
            with self._open(req, max(0.2, timeout)) as r:
                raw = r.read()
                status, answer = r.status, r.headers
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            if 300 <= e.code < 400:
                # Not followed (_NoRedirect). 307 and 308 say the request
                # was not carried out at this address; after another 3xx it
                # may have been (a 303 can answer a request carried out), so
                # its fate is not known (CapitalError.unanswered).
                raise CapitalError(
                    e.code, "Capital.com's endpoint answered HTTP %d, a "
                    "redirect to another address, which is not followed (the "
                    "key and the session's tokens go to the endpoint this "
                    "process was given and nowhere else)" % e.code,
                    code="redirect", sent=True) from None
            raise self._error(e.code, e.headers, raw) from None
        except http.client.InvalidURL as e:
            # Raised before anything connects (a host or a path http.client
            # will not send), so nothing reached Capital.com. Never its text:
            # it quotes the URL.
            raise CapitalError(0, "the request could not be made (%s)"
                               % type(e).__name__, sent=False) from None
        except urllib.error.URLError as e:
            # Raised while connecting or sending: the request did not leave
            # whole, so Capital.com cannot have carried it out.
            reason = getattr(e, "reason", e)
            text = ("%s: %s" % (type(reason).__name__, reason)
                    if isinstance(reason, OSError) else "request failed")
            raise CapitalError(0, self.scrub(text), sent=False) from None
        except (OSError, http.client.HTTPException) as e:
            # The request left and the answer did not come back whole:
            # whether Capital.com carried it out is NOT known.
            raise CapitalError(0, "the connection broke while Capital.com "
                               "was answering (%s)" % type(e).__name__,
                               sent=True, cause=type(e).__name__) from None
        except ValueError as e:
            # never the exception's own text: http.client quotes a header
            # value, and a header value is the key
            raise CapitalError(0, "the request could not be made (%s)"
                               % type(e).__name__, sent=False) from None
        return status, answer, raw

    def _open(self, req, timeout: float):
        """One request through this client's own opener (see __init__)."""
        return self._opener.open(req, timeout=timeout)

    def _error(self, status: int, headers, raw: bytes) -> CapitalError:
        code = None
        try:
            body = json.loads(raw) if raw else {}
            if isinstance(body, dict):
                code = body.get("errorCode")
        except ValueError:
            body = None
        # Capital.com's own code, on one line with nothing of a credential
        # in it (it is quoted in messages); a documented one is unchanged.
        code = (_one_line(self.scrub(str(code)), 120) if code is not None
                else None)
        retry = None
        if status == 429:
            # Capital.com's limit is per second: half of one, unless it says
            retry = 0.5
            value = (headers.get("Retry-After") or "").strip() \
                if headers else ""
            if value:
                try:
                    retry = max(0.0, float(value))
                except ValueError:
                    try:
                        retry = max(0.0, parsedate_to_datetime(value)
                                    .timestamp() - time.time())
                    except (TypeError, ValueError):
                        pass
            self.pace.block(retry)
        # A body that is not Capital.com's JSON (a gateway's page, CRLFs
        # and all) is said by its title, on one line (_page_words).
        text = code or (_page_words(raw[:65536].decode("utf-8", "replace"),
                                    status, scrub=self.scrub) if raw else "")
        return CapitalError(status, _one_line(self.scrub(
            text or "HTTP %d" % status), 200), code=code, sent=True,
            retry_after=retry)


# ---------------------------------------------------------------------------
# The catalog: Capital.com's market details as InstrumentSpecs. Pure
# functions, so they can be tested without a broker.
# ---------------------------------------------------------------------------

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _minutes(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d\d)\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59:
        return None
    return h * 60 + mi


def hours_windows(text: str) -> list:
    """One "HH:MM - HH:MM" window as [(start, end)] minutes of its day.

    documented: https://open-api.capital.com/ (openingHours: per weekday a
    list of "HH:MM - HH:MM", e.g. "00:00 - 21:59" and "23:05 - 00:00", with
    a `zone`). An end of "00:00" is midnight. An end of HH:59 is the last
    minute OPEN: Capital.com's 2022 example writes the same silver window
    "00:00 - 22:00" (documented: https://github.com/capital-com-sv/capital-api-postman)
    where the reference writes "00:00 - 21:59" (assumed, verify on demo). A
    window whose end is before its start runs past midnight.
    """
    a, sep, b = (text or "").partition("-")
    start, end = _minutes(a), _minutes(b)
    if not sep or start is None or end is None:
        return []
    if end == 0:
        end = 1440
    elif end % 60 == 59:
        end += 1
    if end <= start:
        return [(start, 1440)] + ([(0, end)] if end > 0 else [])
    return [(start, min(end, 1440))]


def sessions_from_hours(hours) -> list:
    """openingHours as SPEC sessions: one entry per distinct window, with the
    ISO weekdays it opens on. A window past midnight is cut at midnight and
    its rest put on the next day. No openingHours at all is an EMPTY list,
    which the SPEC reads as "no calendar published" (never "closed")."""
    if not isinstance(hours, dict):
        return []
    zone = str(hours.get("zone") or "UTC")
    per_day: dict = {d: [] for d in range(1, 8)}
    seen_any = False
    for i, name in enumerate(_DAYS):
        rows = hours.get(name)
        if rows is None:
            continue
        seen_any = True
        for text in rows if isinstance(rows, list) else []:
            for k, (s, e) in enumerate(hours_windows(str(text))):
                # k 1 is the part of a window that runs past midnight: the
                # next day's (Sunday's runs into Monday)
                day = i + 1 if k == 0 else (i + 2 if i < 6 else 1)
                per_day[day].append((s, e))
    if not seen_any:
        return []
    groups: dict = {}
    for day, wins in per_day.items():
        # touching windows of one day are one window
        merged = []
        for s, e in sorted(set(wins)):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        for w in merged:
            groups.setdefault(w, []).append(day)

    def hhmm(m):
        return "%02d:%02d" % (m // 60, m % 60)
    out = [{"days": sorted(days), "open": hhmm(s), "close": hhmm(e),
            "tz": zone} for (s, e), days in groups.items()]
    out.sort(key=lambda x: (x["days"][0], x["open"]))
    return out


def in_sessions(sessions: list, ms: int) -> bool | None:
    """Is `ms` inside one of these UTC sessions? None when they say nothing
    this can judge (no calendar, or a zone other than UTC)."""
    if not sessions:
        return None
    t = time.gmtime(ms // 1000)
    dow, mins = t.tm_wday + 1, t.tm_hour * 60 + t.tm_min
    for s in sessions:
        if str(s.get("tz") or "UTC").upper() != "UTC":
            return None
        if dow not in s.get("days", ()):
            continue
        o, c = _minutes(s.get("open", "")), s.get("close", "")
        c = 1440 if c == "24:00" else _minutes(c)
        if o is not None and c is not None and o <= mins < c:
            return True
    return False


def dealing_rule(rules: dict, *names):
    """A dealingRules entry, as (unit, value), by the first of its names that
    is present. The reference's names come first; the 2022 Postman example
    calls the same things minNormalStopOrLimitDistance, maxStopOrLimitDistance
    and minControlledRiskStopDistance (documented: both sources), and
    Capital.com's own client reads the older one."""
    for n in names:
        r = rules.get(n) if isinstance(rules, dict) else None
        if isinstance(r, dict) and _num(r.get("value")) is not None:
            return (str(r.get("unit") or "POINTS").upper(),
                    _num(r.get("value")))
    return None


def rule_distance(rule, price: float | None) -> float | None:
    """A dealing-rule distance in price units at `price`: POINTS are price
    units (assumed, verify on demo: the samples give SILVER's minStepDistance
    as 0.001 POINTS with three decimals), PERCENTAGE is of the price."""
    if rule is None:
        return None
    unit, value = rule
    if unit == "PERCENTAGE":
        return None if not price else price * value / 100.0
    return value


STOP_MIN = ("minStopOrProfitDistance", "minNormalStopOrLimitDistance")
STOP_MAX = ("maxStopOrProfitDistance", "maxStopOrLimitDistance")
GSL_MIN = ("minGuaranteedStopDistance", "minControlledRiskStopDistance")


def build_spec(d: dict, row: dict | None = None) -> tuple:
    """(InstrumentSpec, private facts) from one market's details: an item of
    GET /markets?epics= or GET /markets/{epic} (documented:
    https://open-api.capital.com/, instrument / dealingRules / snapshot), and
    its row of GET /markets when there is one. (None, None) when it is not a
    market."""
    inst = d.get("instrument") if isinstance(d, dict) else None
    if not isinstance(inst, dict) or not inst.get("epic"):
        return None, None
    rules = d.get("dealingRules") or {}
    snap = d.get("snapshot") or {}
    row = row or {}
    epic = str(inst["epic"])
    typ = str(inst.get("type") or row.get("instrumentType") or "").upper()
    ccy = str(inst.get("currency") or "")
    dp = snap.get("decimalPlacesFactor")
    tick = _num(row.get("tickSize"))
    if not tick or tick <= 0:
        tick = 10.0 ** -int(dp) if isinstance(dp, int) and 0 <= dp <= 12 \
            else None
    if not tick:
        step = dealing_rule(rules, "minStepDistance")
        tick = step[1] if step and step[0] == "POINTS" and step[1] > 0 \
            else 0.01
    digits = int(dp) if isinstance(dp, int) and 0 <= dp <= 12 \
        else _decimals(_fixed(tick))
    min_deal = dealing_rule(rules, "minDealSize")
    max_deal = dealing_rule(rules, "maxDealSize")
    incr = dealing_rule(rules, "minSizeIncrement")
    min_qty = min_deal[1] if min_deal else None
    if incr and incr[1] > 0:
        lot_step = incr[1]
    elif min_qty:
        # assumed, verify on demo: Capital.com's 2022 example has no
        # minSizeIncrement; sizes are taken in steps of the minimum's last
        # decimal (0.01 when the minimum is 0.01, 1 when it is 1 or 100)
        lot_step = 10.0 ** -_decimals(_fixed(min_qty))
    else:
        lot_step = 0.01
    if not min_qty:
        min_qty = lot_step
    max_qty = max_deal[1] if max_deal and max_deal[1] > 0 else 1e9
    lot = _num(inst.get("lotSize"), 1.0) or 1.0
    mf = _num(inst.get("marginFactor"))
    unit = str(inst.get("marginFactorUnit") or "PERCENTAGE").upper()
    # documented: https://open-api.capital.com/ (marginFactor 10,
    # marginFactorUnit PERCENTAGE). Another unit is not documented; it is
    # read as a percentage and said on stderr.
    margin = (mf / 100.0) if mf and mf > 0 else None
    if unit != "PERCENTAGE" and mf:
        log("%s: marginFactorUnit %s is not documented; read as a "
            "percentage" % (epic, unit))
    modes = [str(m).upper() for m in snap.get("marketModes")
             or row.get("marketModes") or [] if isinstance(m, str)]
    if "VIEW_ONLY" in modes:
        trade_mode = "disabled"
    elif any("CLOSE" in m or "REDUCE" in m for m in modes):
        trade_mode = "close_only"
    else:
        trade_mode = "full"
    bid, offer = _num(snap.get("bid")), _num(snap.get("offer"))
    if bid is None:
        bid, offer = _num(row.get("bid")), _num(row.get("offer"))
    mid = ((bid + offer) / 2.0) if bid and offer else (bid or offer)
    smin = dealing_rule(rules, *STOP_MIN)
    dist = rule_distance(smin, mid)
    if dist is None:
        stops_points = 0
    else:
        # A percentage moves with the price: stated with a tenth to spare
        # and a point more, so that a stop the platform sets at the stated
        # distance is still one Capital.com takes a moment later.
        spare = 1.1 if smin[0] == "PERCENTAGE" else 1.0
        stops_points = int(math.ceil(dist * spare / tick - 1e-9)) + (
            1 if smin[0] == "PERCENTAGE" else 0)
    spec = {
        "symbol": epic,
        "name": str(inst.get("name") or row.get("instrumentName") or epic),
        "asset_class": ASSET_CLASS.get(typ, typ.lower() or "other"),
        "quote_ccy": ccy,
        "digits": digits,
        "tick_size": tick,
        "tick_value": lot * tick,
        "lot_step": lot_step,
        "min_qty": min_qty,
        "max_qty": max_qty,
        "min_notional": 0,
        "contract_size": lot,
        "margin_rate": margin if margin is not None else 1.0,
        # documented: https://img.capital.com/docs/September-2024-CCUK-Terms-and-Conditions-_0.pdf
        # (Schedule 2.11: "The standardised margin close-out rule has been set
        # at 50% of the total initial margin")
        "margin_maintenance_rate": (margin if margin is not None
                                    else 1.0) * 0.5,
        "margin_tiers": None,
        "sessions": sessions_from_hours(inst.get("openingHours")),
        # Capital.com publishes no holiday or early-close dates: none known
        "holidays": [],
        "early_closes": [],
        "tradable": trade_mode != "disabled",
        "shortable": True,
        "trade_mode": trade_mode,
        "stops_level_points": stops_points,
        # documented: https://help.capitalccuk.com/hc/en-us/articles/36410307943826
        # ("No commission")
        "fees": {"commission_per_lot": 0, "mode": "none", "rate": 0,
                 "min": 0},
        "expires_at": None,
        "successor": None,
        "dividend": None,
    }
    if typ == "CURRENCIES":
        pair = str(inst.get("symbol") or row.get("symbol") or "")
        base = pair.split("/")[0] if "/" in pair else epic[:3]
        if re.fullmatch(r"[A-Z]{3}", base or ""):
            spec["base_ccy"] = base
    fee = inst.get("overnightFee")
    if isinstance(fee, dict):
        lr, sr = _num(fee.get("longRate")), _num(fee.get("shortRate"))
        if lr is not None and sr is not None:
            # documented: https://help.capitalccuk.com/hc/en-us/articles/4409685697810
            # (the fee is a daily percentage of the position's value);
            # that longRate/shortRate are that daily percentage, signed as
            # what the holder receives, is assumed, verify on demo
            spec["swap_long"] = round(lr * 365, 6)
            spec["swap_short"] = round(sr * 365, 6)
            spec["swap_mode"] = "pct_annual"
            if typ == "CURRENCIES":
                spec["swap_3day_dow"] = 4 if epic in THURSDAY_FX else 3
            elif typ == "COMMODITIES":
                spec["swap_3day_dow"] = 3 if epic in PRECIOUS else 5
    private = {
        "epic": epic, "type": typ, "currency": ccy, "lot": lot,
        "trailing": str(rules.get("trailingStopsPreference") or ""),
        "gsl": bool(inst.get("guaranteedStopAllowed",
                             inst.get("controlledRiskAllowed", False))),
        "stop_min": smin, "stop_max": dealing_rule(rules, *STOP_MAX),
        "status": str(snap.get("marketStatus") or row.get("marketStatus")
                      or ""),
        "modes": modes,
        "scaling": snap.get("scalingFactor"),
        "streaming": inst.get("streamingPricesAvailable",
                              row.get("streamingPricesAvailable")),
    }
    return spec, private


def bar_row(p: dict) -> list | None:
    """One of Capital.com's bars as [t, o, h, l, c, vol] AT THE BID.

    documented: https://open-api.capital.com/ (openPrice, highPrice, lowPrice
    and closePrice each {bid, ask}, lastTradedVolume, and the start twice:
    snapshotTime local, snapshotTimeUTC UTC). Only snapshotTimeUTC is read.
    LSE's rule: a price shown is the bid; where a bar has no bid, the price
    it does have."""
    if not isinstance(p, dict):
        return None
    t = parse_utc(p.get("snapshotTimeUTC"))

    def side(name):
        v = p.get(name)
        if not isinstance(v, dict):
            return _num(v)
        b = _num(v.get("bid"))
        return b if b is not None else _num(v.get("ask"))
    o, h, lo, c = (side("openPrice"), side("highPrice"), side("lowPrice"),
                   side("closePrice"))
    if t is None or None in (o, h, lo, c):
        return None
    return [t, o, h, lo, c, _num(p.get("lastTradedVolume"), 0.0)]


# ---------------------------------------------------------------------------
# bars.history: one store of closed bars per (epic, timeframe).
# ---------------------------------------------------------------------------

class _BarStore:
    """The closed bars of one epic and timeframe, read from now backwards in
    windows of bar starts [a, b].

    `covered` holds the windows read; `floor()` is the oldest bar start below
    which nothing has been read yet, counted from the newest end with no gap,
    so an answer never has a hole in the middle. A window comes back with at
    most 999 bars (see BARS_SPAN); when Capital.com's bars are sparse
    (a share trades a few hours a day) the windows widen, and one that comes
    back full (1,000) is split in two and read again, whichever end
    Capital.com cut. A long enough run of windows with no bar at all is the
    start of the history.
    """

    def __init__(self, width: int, now_ms: int):
        self.width = width
        self.cond = threading.Condition()
        self.rows: dict = {}
        self.top = now_ms // width * width       # newest bar start to read
        self.next_b = None                       # next window's newest start
        self.span = BARS_SPAN
        self.covered: list = []                  # (a, b, n) windows read
        self.pending: list = []                  # windows to (re)read
        self.inflight: set = set()
        self.done = False
        self.head_at = 0.0
        self.head_busy = False
        self.head_error = None
        self.error = None
        self.fails = 0
        self.workers = 0
        self.want = 0
        self.fast_until = 0.0
        self.extra = 0
        self.quiet_busy = False
        self.asking = 0
        self.waiters: dict = {}
        self.used = self.made = time.monotonic()
        self.dead = False

    # (callers hold self.cond)
    def floor(self) -> int | None:
        """The oldest bar start such that everything from it to the newest
        end has been read; None before the newest end is."""
        if not self.head_at:
            return None
        lo = self.top + self.width
        for a, b, _ in sorted(self.covered, key=lambda w: -w[1]):
            if b + self.width >= lo:
                lo = min(lo, a)
        return lo

    def have(self) -> int:
        lo = self.floor()
        if lo is None:
            return 0
        return sum(1 for t in self.rows if t >= lo)

    def newest(self, count: int) -> list:
        lo = self.floor()
        if lo is None:
            return []
        ts = sorted(t for t in self.rows if t >= lo)[-count:]
        return [list(self.rows[t]) for t in ts]

    def finished(self) -> bool:
        return self.done and not self.pending and not self.inflight

    def take(self):
        """The next window to read, or None."""
        if self.pending:
            w = self.pending.pop(0)
            self.inflight.add(w)
            return w
        if self.done or not self.head_at:
            return None
        if self.next_b is None:
            self.next_b = self.top - BARS_SPAN * self.width
        b = self.next_b
        a = b - (self.span - 1) * self.width
        self.next_b = a - self.width
        self.inflight.add((a, b))
        return (a, b)

    def put(self, w: tuple, rows: list, full: bool, now_ms: int):
        a, b = w
        self.inflight.discard(w)
        for r in rows:
            if r[0] + self.width <= now_ms:
                self.rows[r[0]] = r
        n = sum(1 for r in rows if a <= r[0] <= b)
        span = (b - a) // self.width + 1
        if full and span > 3:
            # Came back full: which part Capital.com left out is not known
            # (undocumented), so both halves are read again.
            mid = a + (span // 2) * self.width
            self.pending[:0] = [(mid, b), (a, mid - self.width)]
            return
        self.covered.append((a, b, n))
        # the windows further back are sized to hold about BARS_TARGET bars
        if n == 0:
            self.span = min(BARS_SPAN_MAX, self.span * 2)
        elif n < BARS_TARGET // 2:
            self.span = max(BARS_SPAN, min(BARS_SPAN_MAX, int(
                span * BARS_TARGET / max(n, 1))))
        else:
            self.span = BARS_SPAN
        self._judge_end()

    def _judge_end(self):
        """History has begun once the oldest windows read, with nothing but
        windows above them, held no bar at all for BARS_EMPTY_DAYS days (at
        least two windows): a weekend or a holiday is never that long."""
        lo = self.floor()
        if lo is None:
            return
        empty_ms, windows = 0, 0
        for a, b, n in sorted(self.covered, key=lambda w: w[0]):
            if a < lo:
                continue
            if n:
                break
            empty_ms += b - a + self.width
            windows += 1
        if windows >= 2 and empty_ms >= BARS_EMPTY_DAYS * 86_400_000 or (
                windows >= 1 and self.width >= 86_400_000
                and empty_ms >= 365 * 86_400_000):
            self.done = True
            self.pending = [w for w in self.pending if w[1] >= lo]


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------

class CapitalComAdapter(Adapter):
    name = "capitalcom"
    version = VERSION
    broker = "Capital.com"
    # The careful value. __init__ replaces it with what the configuration
    # says, so anything that skips that step is live, never paper.
    mode = "live"
    # Not known cold: an account's currency is read at the first call (and
    # remembered for the next hello). account.get's currency is the truth.
    account_currency = "USD"
    # Every deal is a position of its own: a hedging book (see the module
    # docstring for standard mode's offsets).
    account_model = "hedging"
    # What the user must type (SPEC 3.2). The values arrive only as
    # BRUE_CRED_API_KEY, BRUE_CRED_IDENTIFIER and BRUE_CRED_PASSWORD at
    # spawn. documented: https://open-api.capital.com/ (POST /session:
    # X-CAP-API-KEY, `identifier` the login, `password` the "API key custom
    # password"); the key "will only be shown once" when it is made.
    credential_form = [
        {"key": "api_key", "label": "API key", "type": "password"},
        {"key": "identifier", "label": "Login email", "type": "text"},
        {"key": "password", "label": "API key password", "type": "password"},
    ]
    # What Capital.com's own terms let a platform do (SPEC 3.3).
    # documented: https://img.capital.com/docs/September-2024-CCUK-Terms-and-Conditions-_0.pdf
    # (UK, September 2024: 9(g) "you will not use the prices we make available
    # to you for any purpose other than for your own trading purposes, and you
    # agree not to redistribute the prices"; 3.1 "all instructions should be
    # placed manually and any use of an automated data entry system ... is
    # expressly and strictly prohibited") and Capital Com SV Investments'
    # EU terms of September 2026 (8.1, the same manual-entry clause; 8.26.1,
    # Market Data "is confidential and can not be redistributed to anyone").
    terms = {
        "data_local_only": True,
        "orders_by_hand_only": True,
        "summary": ("Capital.com's prices are for your own trading and stay "
                    "on this computer, and on a live account its terms have "
                    "you place each order yourself."),
        "url": "https://capital.com/en-gb/terms-and-policies",
    }

    def __init__(self, argv=None, environ=None, out=None):
        super().__init__(out)
        environ = os.environ if environ is None else environ
        self.cfg = resolve_environment(
            sys.argv[1:] if argv is None else argv, environ)
        self.mode = self.cfg["mode"]
        self.client = CapitalClient(self.cfg)
        self.client.on_login = self._on_login
        self._pin = str(environ.get("BRUE_ACCOUNT_ID") or "").strip()
        self._state_dir = environ.get("BRUE_BROKER_STATE") or ""
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False
        self._baselined = False
        self._hedging = False
        self._account: dict | None = None
        self._account_t = 0.0
        self._account_sent = None
        self._accounts: list = []
        self._state_account = ""
        self._state_ccy = ""
        # the book, as Capital.com last showed it
        self._deals: dict = {}          # dealId -> deal
        self._deals_t = 0.0
        self._worders: dict = {}        # dealId -> working order
        self._worders_t = 0.0
        self._gone: dict = {}           # dealId -> {"deal", "tries", ...}
        self._wo_gone: dict = {}        # dealId -> {"order", "tries"}
        # listed, not told yet: waiting for the confirmation of an order that
        # could own them (see _claimant)
        self._held: dict = {}           # dealId -> {"since", "baseline"}
        self._held_wo: dict = {}        # working order dealId -> the same
        # what this adapter did and must not do twice (persisted)
        self._journal: dict = {}        # client_id -> order entry
        self._closes: dict = {}         # client_id -> close entry
        self._deal_cid: dict = {}       # dealId -> client_id that opened it
        self._deal_oid: dict = {}       # dealId -> order_id that opened it
        self._deal_from: dict = {}      # dealId -> "limit" | "stop"
        self._wo_oid: dict = {}         # working order dealId -> order_id
        self._oid_wo: dict = {}         # order_id -> working order dealId
        self._levels: dict = {}         # dealId -> {"sl", "tp"} confirmed
        self._fills: list = []
        self._seen: collections.OrderedDict = collections.OrderedDict()
        self._ops_seen: collections.OrderedDict = collections.OrderedDict()
        self._ops_mark = 0
        # handlers in progress: epics an order is being placed on, deals
        # being closed (the poller leaves those to the handler)
        self._claims: dict = {}
        self._posting = 0
        self._post_idle = threading.Condition(self._lock)
        # dealId -> a close this adapter sent and has not told yet
        self._closing: dict = {}
        self._markets: dict = {}        # epic -> private facts
        self._catalog: dict = {}
        self._catalog_list: list = []
        self._catalog_ready = threading.Event()
        self._catalog_built = 0.0
        self._catalog_error = ""
        self._catalog_busy = False
        self._fx_cache: dict = {}
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
        self._open_lock = threading.Lock()
        # quotes
        self._quote_subs: list = []     # in the order asked, all of them
        self._quote_live: set = set()   # those the stream carries
        self._quote_refused: list = []
        self._quote_sent: dict = {}
        self._stream_up = threading.Event()
        self._stream_wake = threading.Event()
        self._stream_gen = 0
        self._stream_thread = None
        self._stream_ws = None
        self._last_rest_quotes = 0.0
        # bars
        self._bars: collections.OrderedDict = collections.OrderedDict()
        self._bars_lock = threading.Lock()
        self._bars_slots = threading.BoundedSemaphore(BARS_LOADS)
        self._bars_quiet = threading.Lock()
        self._bar_subs: dict = {}
        # timers of the poller
        self._next_account = 0.0
        self._next_tx = 0.0
        self._next_clock = 0.0
        self._unsaved = False
        self._load_state()
        if self._state_ccy and (not self._pin
                                or self._pin == self._state_account):
            self.account_currency = self._state_ccy

    # == what this adapter is ============================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # documented: https://open-api.capital.com/ (stopLevel and
                # profitLevel on POST /positions; PUT /positions/{dealId})
                "brackets": True, "modify": True,
                # documented: POST /workingorders, type LIMIT or STOP, an
                # optional goodTillDate (GTC without it). No stop-limit, no
                # day expiry, no OCO group, no close-by.
                "pending": True, "pending_types": ["limit", "stop"],
                "expiry": ["gtc", "gtd"],
                # documented: trailingStop with stopDistance, kept by
                # Capital.com; refused per market and account where it is
                # not offered (trailingStopsPreference, trailingStopsEnabled)
                "trailing": True,
                # documented: DELETE /positions/{dealId} takes no size
                "partial_close": False,
                "oco": False, "close_by": False,
                # documented: GET /workingorders lists every working order
                "can_list": True,
                # assumed, verify on demo: whether a working order holds
                # margin is not documented; false is the careful answer (an
                # order may still die when it triggers)
                "reserves_margin": False},
            "data": {"quotes": True, "bars": True, "history": True,
                     # GET /history/transactions (documented)
                     "operations": True,
                     "max_quote_symbols": MAX_QUOTE_SYMBOLS,
                     "max_symbols_per_request": MAX_QUOTE_SYMBOLS},
            # documented: GET /accounts lists the login's accounts and PUT
            # /session switches between them
            "accounts": {"multi": True},
            "flatten_all": True}

    def rate_limits(self) -> dict:
        return {"requests_per_second": PACE_PER_SECOND}

    def broker_info(self) -> dict:
        # documented: https://capital.com/en-gb/terms-and-policies (Capital Com
        # (UK) Limited, FCA register number 793714) and Capital Com SV
        # Investments Limited's EU terms (CySEC licence 319/17)
        return {"display_name": "Capital.com",
                "tagline": "CFDs on shares, indices, forex, commodities and "
                           "crypto",
                "mark": "CC",
                "website": "https://capital.com",
                "support": "https://help.capital.com",
                "country": "United Kingdom",
                "regulated_by": ["FCA 793714", "CySEC 319/17"],
                "asset_classes": ["Shares", "Indices", "Forex",
                                  "Commodities", "Cryptocurrencies"]}

    def now_ms(self) -> int:
        """Capital.com's clock: ours corrected by its GET /time."""
        return self.client.broker_ms()

    def _err(self, e: CapitalError) -> AdapterError:
        return to_adapter_error(e, self.cfg["env"], self.client.scrub)

    def _call(self, method, path, query=None, body=None, **kw):
        """A broker call made for a handler: Capital.com's refusal becomes
        the typed error the platform is told."""
        try:
            return self.client.call(method, path, query, body, **kw)
        except CapitalError as e:
            raise self._err(e) from None

    # == lanes: the read thread only reads ================================
    def _dispatch(self, rid: str, method: str, params: dict):
        """Note WHEN the request was read, and hand it on (the reference
        adapter's lanes): `orders` one at a time in the order read;
        `flatten` the moment it is read; `reads` a few at once; `charts`
        each on a thread of its own. hello, ping and bye are answered here:
        they touch no network (bye flushes the state first)."""
        read_at = time.monotonic()
        # By the exact name only: another spelling of a method
        # ("order_place") would go to the reads lane and be carried out,
        # past the platform's arming and by-hand rules.
        handler = self._handler(method)
        if method in ("hello", "ping", "bye") or handler is None:
            if method == "bye":
                self._drain(BYE_DRAIN)
                self._save_state()
                self.client.logout()
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
                                     name="capitalcom-%s-%d" % (lane, i)
                                     ).start()
            if lane == "orders":
                self._orders_waiting += 1
                self._orders_idle.clear()
            elif lane == "flatten":
                self._flat_at = read_at
                self._flat_pending += 1
                self._flat_idle.clear()
                self.client.stand_down.set()
        if lane in ("orders", "flatten"):
            self.client.pace.in_hand(+1)
        q.put((rid, method, params, read_at))

    def _lane_loop(self, lane: str, q: "queue.Queue"):
        while True:
            rid, method, params, read_at = q.get()
            try:
                self._answer(lane, rid, method, params, read_at)
            except Exception as e:  # noqa: BLE001 - a lane outlives anything
                log("%s: %s: %s" % (method, type(e).__name__,
                                    self.client.scrub(str(e))))
                try:
                    self._send(protocol.res_error(
                        rid, "internal", "%s: %s" % (
                            type(e).__name__, self.client.scrub(str(e)))))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.client.set_deadline(None)
                if lane in ("orders", "flatten"):
                    self.client.pace.in_hand(-1)
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
        budget = (FLATTEN_HARD if method in ("flatten_all", "catalog.list")
                  else HANDLER_BUDGET)
        self.client.set_request(
            read_at, budget,
            floor=FLATTEN_FLOOR if method == "flatten_all" else 0.0,
            yields=lane == "orders")
        left = self.client.time_left()
        if lane == "orders":
            if read_at < self._flat_at:
                # Sent before the panic button and not begun: carrying it
                # out now, behind flatten_all, is what nobody asked for.
                self._send(protocol.res_error(
                    rid, "x_capitalcom_flattening", "flatten_all was asked "
                    "for after this call was sent and before it was begun: "
                    "it was NOT carried out. Ask again if it is still "
                    "wanted.", True, "frozen"))
                return
            if not self._flat_idle.wait(timeout=max(0.05, left - 0.3)):
                self._send(protocol.res_error(
                    rid, "x_capitalcom_flattening", "flatten_all is still "
                    "running; this call was not carried out. Ask again.",
                    True, "frozen"))
                return
            left = self.client.time_left()
        if left < 0.3:
            self._send(protocol.res_error(
                rid, "rate_limited", "the adapter was busy with earlier "
                "requests for %.1f s and the time for this one is up; "
                "nothing was done. Ask again." % (time.monotonic() - read_at),
                True))
            return
        if not self._opened and not method.startswith(PRE_SESSION):
            # The session is opened ONCE, by whichever request comes first.
            if not self._open_lock.acquire(timeout=max(0.05, left - 0.3)):
                self._send(protocol.res_error(
                    rid, "internal", "the session with Capital.com is still "
                    "being opened; ask again in a moment", True))
                return
            try:
                super()._dispatch(rid, method, params)
            finally:
                self._open_lock.release()
            return
        super()._dispatch(rid, method, params)

    def _drain(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        return (self._orders_idle.wait(seconds) and self._flat_idle.wait(
            max(0.0, end - time.monotonic())))

    # == the session ======================================================
    def _login_error(self, e: CapitalError) -> AdapterError:
        """A refused login, in words for the person at the key form. Never
        the values; the environment tried and what it takes."""
        env = self.cfg["env"]
        code = str(e.code or "")
        if code == "error.null.accountId":
            # seen on the demo host, 2026-11-15, with a real login: POST
            # /session answered 401 error.null.accountId, a code the
            # documentation does not list, and this said Capital.com had
            # refused the key, with advice about lost and paused keys. Read
            # as: this login has no account on this host. A key belongs to
            # the account the platform was on when it was made, demo or live
            # (assumed, verify on demo).
            log("login refused on the %s host (HTTP %d %s): Capital.com "
                "found no account for this login there" % (env, e.status,
                                                          code))
            # Said as likely, since it is assumed (above), and in this
            # adapter's own words: it is run by platforms and by hand alike,
            # so it names no platform's screen or row.
            if env == "demo":
                needs = ("Likely the API key was made while Capital.com's "
                         "platform was on a live account: such a key may "
                         "not reach the demo account. Make the key with the "
                         "platform switched to Demo (Settings > API "
                         "integrations), and use that one")
            else:
                needs = ("Likely the API key was made while Capital.com's "
                         "platform was on the Demo account: such a key may "
                         "not reach a live account. Make the key with the "
                         "platform switched to the live account (Settings > "
                         "API integrations), and use that one")
            return AdapterError(
                "x_capitalcom_no_account", "Capital.com found no account "
                "for this login on its %s host (%s). %s." % (env, code,
                                                            needs),
                like="unauthorized")
        if e.status in (401, 403) or code in ("error.null.api.key",):
            log("login refused on the %s host (HTTP %d %s); this adapter "
                "talks to Capital.com's demo host unless --live or "
                "CAPITALCOM_ENV=live is given" % (env, e.status, code))
            if code == "error.invalid.details":
                what = ("refused the login email or the API key's password. "
                        "The password is the one set for the API key when it "
                        "was made, not the password of your Capital.com "
                        "login")
            else:
                what = ("refused the API key (%s). A key is shown once when "
                        "it is made in Settings > API integrations, lasts a "
                        "year unless set otherwise, and stops when it is "
                        "paused or the account is suspended" % (
                            code or "HTTP %d" % e.status))
            return AdapterError(
                "unauthorized", "Capital.com %s, on its %s host: a demo key "
                "works on the demo host only and a live key on the live host "
                "only. Capital.com's API is for its own clients: Capital Com "
                "(UK) serves no one living in the United States or the EU "
                "(EU residents are clients of its EU entity, which does not "
                "take US or Belgian residents)." % (what, env))
        if code == "error.invalid.accountId":
            return AdapterError(
                "x_capitalcom_account", "account %s is not one of this "
                "login's Capital.com accounts on its %s host; choose the "
                "account again" % (self.client.bound or self._pin, env),
                like="not_found")
        return self._err(e)

    def _on_login(self, gen: int):
        """A new session: the stream reconnects on its tokens (a switch of
        account ends the old stream; documented: https://open-api.capital.com/)."""
        self._stream_wake.set()

    def _switch(self, account_id: str):
        """Put the session on `account_id` (PUT /session; see
        CapitalClient.rebind: a session that lapsed meanwhile is replaced by
        a new one on that account)."""
        self.client.bound = account_id
        self.client.rebind("write", 3.0)
        self._stream_wake.set()

    def _endpoint_refusal(self):
        """An endpoint the configuration names and this process will not use
        (see resolve_environment): every call that needs Capital.com says so,
        and nothing is sent anywhere."""
        fault = self.cfg.get("endpoint_fault")
        if fault:
            log("not connecting: " + fault)
            raise AdapterError(
                "x_capitalcom_endpoint", "Capital.com is not asked: %s. "
                "Nothing was sent." % fault, like="unsupported")

    def on_first_call(self):
        """Log in, bind the account, read the book; then the threads.
        `hello` never comes here (SPEC 3.1)."""
        cfg = self.cfg
        self._endpoint_refusal()
        missing = [label for k, label in (
            ("key", "API key"), ("identifier", "login email"),
            ("password", "API key password")) if not cfg[k]]
        if missing:
            log("missing credentials: the platform passes BRUE_CRED_API_KEY, "
                "BRUE_CRED_IDENTIFIER and BRUE_CRED_PASSWORD; by hand, set "
                "CAPITALCOM_API_KEY, CAPITALCOM_IDENTIFIER and "
                "CAPITALCOM_PASSWORD")
            raise AdapterError(
                "unauthorized", "Capital.com needs your API key, your login "
                "email and the API key's own password. Missing: %s."
                % ", ".join(missing))
        if cfg.get("cred_fault"):
            raise AdapterError("unauthorized", "Capital.com cannot be asked: "
                               + cfg["cred_fault"] + ".")
        self._sync_clock()
        if not self.client.bound and self._pin:
            self.client.bound = self._pin
        try:
            self.client.session("read", 4.0)
        except CapitalError as e:
            raise self._login_error(e) from None
        info = self.client.info
        accounts = [a for a in info.get("accounts") or []
                    if isinstance(a, dict)]
        ids = [str(a.get("accountId")) for a in accounts]
        want = self._pin or self.client.bound or self.client.current
        if accounts and want not in ids:
            raise self._login_error(CapitalError(
                400, "not this login's", code="error.invalid.accountId"))
        self.client.bound = want
        if self.client.current != want:
            try:
                self._switch(want)
            except CapitalError as e:
                raise self._login_error(e) from None
        row = next((a for a in accounts if str(a.get("accountId")) == want),
                   {})
        kind = str(row.get("accountType") or info.get("accountType")
                   or "").upper()
        if kind and kind != "CFD":
            # documented: accountType "CFD" (samples). A spread bet sizes in
            # stake per point and prices in points (pence for UK shares),
            # and real stocks take no stop or target ("Stop loss and take
            # profit values cannot be set when conducting trades with real
            # stocks", https://open-api.capital.com/). Neither can be checked
            # on a demo (there is no UK spread-bet demo), so neither is
            # traded through this adapter until it has been.
            raise AdapterError(
                "x_capitalcom_account_type", "account %s is a Capital.com %s "
                "account; this adapter deals on CFD accounts only (a spread "
                "bet's stakes and points, and real stocks, are not verified "
                "yet). Choose a CFD account." % (want, kind),
                like="unsupported")
        ccy = str(row.get("currency") or info.get("currencyIsoCode") or "")
        if ccy:
            self.account_currency = ccy
        self._bind_state(want, ccy)
        try:
            prefs = self._call("GET", "/accounts/preferences") or {}
            self._hedging = bool(prefs.get("hedgingMode"))
        except AdapterError as e:
            log("account preferences not read (%s); standard mode assumed"
                % e)
        if not self._baselined:
            self._read_book(kind="read", baseline=True)
            self._baselined = True
            self._match_baseline()
        if not self._started:
            self._started = True
            # The poller's first reads are spread out, not added to the
            # burst a platform's reconciliation makes at a connect.
            mono = time.monotonic()
            self._next_account = mono + 3.0
            self._next_tx = mono + 6.0
            for target, label in ((self._poll_loop, "poll"),
                                  (self._catalog_thread, "catalog")):
                threading.Thread(target=target, daemon=True,
                                 name="capitalcom-" + label).start()
        self._set_session("session", "connected", "Capital.com %s account %s"
                          " (%s)%s" % (cfg["env"], want, ccy or "?",
                                       ", hedging mode" if self._hedging
                                       else ""))
        self._wake.set()

    def _match_baseline(self):
        """At a start: orders of the last run whose outcome was not known
        (their answer lost, or the process ended with them on the wire),
        matched against the book as it is now. What they made while this
        adapter was not running is reported now, under their client_ids."""
        with self._lock:
            # working orders first: a deal's own working order is known
            # before the deal is looked at
            for o in list(self._worders.values()):
                if o["id"] in self._wo_oid:
                    continue
                o["size0"] = o.get("size0") or o["size"]
                self._attribute_order(o, baseline=True)
            for d in sorted(self._deals.values(),
                            key=lambda x: x.get("opened") or 0):
                if d["id"] in self._deal_oid:
                    continue
                self._attribute(d, baseline=True)
            # A working order of the last run that is no longer working and
            # whose end nobody was told of (it expired, was cancelled or
            # filled and closed while this adapter was not running): the
            # activity history says which (a day back at most).
            now = self.now_ms()
            for e in self._journal.values():
                wo = e.get("wo")
                if (e.get("state") != "resting" or not wo
                        or wo in self._worders or wo in self._wo_gone):
                    continue
                self._wo_gone[wo] = {"order": {
                    "id": wo, "epic": e["epic"], "dir": e["dir"],
                    "size": e["size"], "size0": e["size"],
                    "level": _num(e.get("level")),
                    "type": e["kind"].upper(), "tif": "",
                    "gtd": parse_utc(e.get("gtd")),
                    "created": int(e.get("at") or now), "sl": None,
                    "tp": None, "sd": None, "pd": None, "trailing": False,
                    "gsl": False}, "tries": 0,
                    "at": max(int(e.get("at") or now), now - 86_000_000)}
            unsaved = self._unsaved
        if unsaved:
            self._save_state()

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        self._stream_wake.set()
        ws = self._stream_ws
        if ws is not None:
            ws.close()
        self._save_state()

    def _sync_clock(self):
        """Capital.com's clock (GET /time, no session; documented:
        https://open-api.capital.com/, serverTime in ms)."""
        t0 = time.time()
        try:
            out = self.client.call("GET", "/time", kind="background",
                                   max_wait=1.0, timeout=3.0, auth=False)
        except CapitalError as e:
            log("Capital.com's clock not read (%s); this machine's is used"
                % e.message)
            return
        server = _num((out or {}).get("serverTime"))
        if server:
            t1 = time.time()
            self.client.skew_ms = int(server - (t0 + t1) * 500)
        self._next_clock = time.monotonic() + CLOCK_POLL

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

    # == persistence =====================================================
    # One small file in the profile's private directory. Never
    # `account.json`: that name is the platform's account pin. Nothing in it
    # is a credential or a session token.
    def _state_path(self, name: str) -> str:
        return os.path.join(self._state_dir, name) if self._state_dir else ""

    def _load_state(self):
        path = self._state_path("capitalcom_state.json")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError) as e:
            log("state file unreadable, starting without it: %s" % e)
            return
        if (not isinstance(st, dict) or st.get("env") != self.cfg["env"]
                or st.get("api") != _host(self.cfg["api_url"])):
            return
        self._state_account = str(st.get("account") or "")
        self._state_ccy = str(st.get("ccy") or "")

        def d(name):
            v = st.get(name)
            return dict(v) if isinstance(v, dict) else {}
        self._idempotent = d("idem")
        self._journal = {k: v for k, v in d("journal").items()
                         if isinstance(v, dict)}
        self._closes = {k: v for k, v in d("closes").items()
                        if isinstance(v, dict)}
        self._deal_cid = d("deal_cid")
        self._deal_oid = d("deal_oid")
        self._deal_from = d("deal_from")
        self._wo_oid = d("wo_oid")
        self._oid_wo = {v: k for k, v in self._wo_oid.items()}
        self._levels = {k: v for k, v in d("levels").items()
                        if isinstance(v, dict)}
        self._fills = [f for f in st.get("fills") or []
                       if isinstance(f, dict)][-LEDGER_KEEP:]
        for k in st.get("seen") or []:
            self._seen[str(k)] = True
        for k in st.get("ops_seen") or []:
            self._ops_seen[str(k)] = True
        self._ops_mark = int(_num(st.get("ops_mark"), 0) or 0)
        # An order of the last run whose outcome was not known is looked for
        # by THIS run, however old it is, before anything is said of it
        # (no entry is called lost at the start, unread, whether Capital.com
        # acknowledged it or not). One Capital.com acknowledged (a
        # dealReference) is asked for by that reference; one of unknown fate
        # is looked for in the book like any other. Their windows count from
        # now (`since`), and only a deal made in its own window can be one of
        # theirs (see _fits).
        now = self.now_ms()
        for e in self._journal.values():
            if e.get("state") in ("sending", "unknown", "sent"):
                e["since"] = now
        n = sum(1 for e in self._journal.values()
                if e.get("state") in ("sending", "unknown", "sent"))
        if n:
            log("from the last run: %d order(s) whose outcome is still to be "
                "found at Capital.com; nothing is sent again for them" % n)

    def _save_state(self, sync: bool = False):
        path = self._state_path("capitalcom_state.json")
        if not path:
            return
        with self._lock:
            now = self.now_ms()
            # orders that are over and old are of no further use
            for store, cap in ((self._journal, 2000), (self._closes, 1000),
                               (self._idempotent, 2000),
                               (self._deal_cid, 4000), (self._deal_oid, 4000),
                               (self._deal_from, 2000), (self._wo_oid, 2000),
                               (self._levels, 2000)):
                for old in list(store)[:max(0, len(store) - cap)]:
                    store.pop(old, None)
            for k in [k for k, e in self._journal.items()
                      if e.get("state") in ("filled", "done", "rejected",
                                            "lost")
                      and now - int(e.get("at") or 0) > 30 * 86_400_000]:
                self._journal.pop(k, None)
            while len(self._seen) > 6000:
                self._seen.popitem(last=False)
            while len(self._ops_seen) > 2000:
                self._ops_seen.popitem(last=False)
            del self._fills[:-LEDGER_KEEP]
            live = {d: v for d, v in self._levels.items()
                    if d in self._deals or d in self._worders}
            st = {"v": 1, "env": self.cfg["env"],
                  "api": _host(self.cfg["api_url"]),
                  "account": self._state_account, "ccy": self._state_ccy,
                  "idem": dict(self._idempotent),
                  "journal": {k: dict(v) for k, v in self._journal.items()},
                  "closes": {k: dict(v) for k, v in self._closes.items()},
                  "deal_cid": dict(self._deal_cid),
                  "deal_oid": dict(self._deal_oid),
                  "deal_from": dict(self._deal_from),
                  "wo_oid": dict(self._wo_oid),
                  "levels": live if self._baselined else dict(self._levels),
                  "fills": list(self._fills),
                  "seen": list(self._seen),
                  "ops_seen": list(self._ops_seen),
                  "ops_mark": self._ops_mark}
            self._unsaved = False
        self._write_json(path, st, sync)

    def _write_json(self, path: str, obj, sync: bool = False):
        """Whole file or nothing: write beside it, then rename over it.
        `sync` also forces it to the disk (the journal entry of an order that
        is about to be sent: a crash a moment later must not forget it)."""
        with self._save_lock:
            tmp = path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(obj, fh)
                    if sync:
                        fh.flush()
                        os.fsync(fh.fileno())
                os.replace(tmp, path)
            except OSError as e:
                log("could not save %s: %s" % (os.path.basename(path), e))

    def _bind_state(self, account: str, ccy: str):
        """What was remembered belongs to ONE account. A profile whose login
        now deals on another must not replay the old one's client ids."""
        with self._lock:
            if self._state_account and self._state_account != account:
                log("this profile now deals on another account; what was "
                    "remembered for the previous one is dropped")
                for store in (self._idempotent, self._journal, self._closes,
                              self._deal_cid, self._deal_oid, self._deal_from,
                              self._wo_oid, self._oid_wo, self._levels):
                    store.clear()
                del self._fills[:]
                self._seen.clear()
                self._ops_seen.clear()
                self._ops_mark = 0
            changed = (self._state_account, self._state_ccy) != (account, ccy)
            self._state_account, self._state_ccy = account, ccy
        if changed:
            self._save_state()

    def idempotent_store(self, client_id: str, result: dict,
                         signature=None) -> dict:
        """The SDK remembers results in memory; a restart is not a licence
        to act twice, so they are written down too (SPEC 2)."""
        out = super().idempotent_store(client_id, result, signature)
        self._save_state()
        return out

    # == the catalog =====================================================
    def _catalog_thread(self):
        try:
            self._load_catalog()
        except Exception as e:  # noqa: BLE001 - reported by catalog.list
            self._catalog_error = "%s: %s" % (type(e).__name__,
                                              self.client.scrub(str(e)))
            log("catalog load failed: " + self._catalog_error)
        finally:
            self._catalog_ready.set()

    def _load_catalog(self, refresh: bool = False):
        """The cached catalog when it is young enough, else Capital.com's:
        GET /markets lists every market (documented changelog 2023-08-04:
        with no parameter, all of them), GET /markets?epics= gives their
        details 50 at a time. Batches that arrive after catalog.list has
        been answered go out as `catalog.changed` (reason `listed`, the
        instruments inline)."""
        path = self._state_path("capitalcom_catalog.json")
        if not refresh and path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    st = json.load(fh)
                age = time.time() - float(st.get("built") or 0)
                if (st.get("env") == self.cfg["env"]
                        and st.get("api") == _host(self.cfg["api_url"])
                        and st.get("account") == self.client.bound
                        and 0 <= age < CATALOG_MAX_AGE
                        and isinstance(st.get("instruments"), list)
                        and st["instruments"]):
                    with self._lock:
                        self._install_catalog(st["instruments"],
                                              st.get("markets") or {},
                                              float(st["built"]))
                    self._catalog_ready.set()
                    if age < CATALOG_REFRESH:
                        return
                    refresh = True
            except (OSError, ValueError, TypeError) as e:
                log("catalog cache unreadable (%s); reading Capital.com's"
                    % e)
        with self._lock:
            if self._catalog_busy:
                return
            self._catalog_busy = True
        try:
            self._build_catalog(refresh)
        finally:
            with self._lock:
                self._catalog_busy = False

    def _build_catalog(self, refresh: bool):
        kind = "background" if refresh else "read"
        listing = self.client.call("GET", "/markets", kind=kind,
                                   max_wait=10.0, timeout=20.0) or {}
        rows = [r for r in listing.get("markets") or []
                if isinstance(r, dict) and r.get("epic")]
        by_epic = {str(r["epic"]): r for r in rows}
        epics = list(by_epic)
        specs, private = [], {}
        first = not self._catalog
        for i in range(0, len(epics), DETAILS_PER_CALL):
            if self._stop.is_set():
                return
            batch = epics[i:i + DETAILS_PER_CALL]
            got = None
            for attempt in range(3):
                try:
                    got = self.client.call(
                        "GET", "/markets", {"epics": ",".join(batch)},
                        kind=kind, max_wait=10.0, timeout=20.0) or {}
                    break
                except CapitalError as e:
                    if e.status == 404:
                        got = {}
                        break
                    if attempt == 2:
                        raise
                    time.sleep(1.0 + attempt)
            new = []
            for det in (got or {}).get("marketDetails") or []:
                epic = str(((det or {}).get("instrument") or {}).get("epic")
                           or "")
                spec, priv = build_spec(det, by_epic.get(epic))
                if spec:
                    new.append(spec)
                    private[spec["symbol"]] = priv
            specs.extend(new)
            if first and new:
                with self._lock:
                    announce = self._catalog_ready.is_set()
                    self._install_catalog(
                        list(self._catalog_list) + new,
                        dict(self._markets, **{s["symbol"]: private[s["symbol"]]
                                               for s in new}), time.time())
                if announce:
                    self.emit("catalog.changed", {
                        "reason": "listed",
                        "symbols": [s["symbol"] for s in new],
                        "instruments": new})
        if not specs:
            raise CapitalError(0, "Capital.com listed no market details",
                               sent=True)
        with self._lock:
            before = dict(self._catalog)
            self._install_catalog(specs, private, time.time())
        self._catalog_ready.set()
        if not first:
            self._announce_changes(before)
        path = self._state_path("capitalcom_catalog.json")
        if path:
            self._write_json(path, {"env": self.cfg["env"],
                                    "api": _host(self.cfg["api_url"]),
                                    "account": self.client.bound,
                                    "built": self._catalog_built,
                                    "instruments": specs,
                                    "markets": private})

    def _install_catalog(self, specs: list, private: dict, built: float):
        """Caller holds the lock."""
        self._catalog_list = list(specs)
        self._catalog = {s["symbol"]: s for s in specs}
        self._markets = {k: v for k, v in private.items()
                         if k in self._catalog}
        self._catalog_built = built

    def _announce_changes(self, before: dict):
        with self._lock:
            now = dict(self._catalog)
        listed = [s for k, s in now.items() if k not in before]
        gone = [k for k in before if k not in now]
        terms = [s for k, s in now.items()
                 if k in before and before[k] != s]
        if listed:
            self.emit("catalog.changed", {"reason": "listed",
                                          "symbols": [s["symbol"]
                                                      for s in listed],
                                          "instruments": listed})
        if terms:
            self.emit("catalog.changed", {"reason": "terms",
                                          "symbols": [s["symbol"]
                                                      for s in terms],
                                          "instruments": terms})
        if gone:
            self.emit("catalog.changed", {"reason": "delisted",
                                          "symbols": gone})

    def handle_catalog_list(self, p):
        left = self.client.time_left() or HANDLER_BUDGET
        if not self._catalog_ready.wait(timeout=max(0.05, left - 0.6)):
            with self._lock:
                partial = list(self._catalog_list)
            if partial:
                # The rest follows as catalog.changed (see _build_catalog).
                self._catalog_ready.set()
                return {"instruments": partial}
            raise AdapterError("internal", "Capital.com's market list is "
                               "still being read; ask again in a moment",
                               retryable=True)
        with self._lock:
            out = list(self._catalog_list)
        if not out:
            raise AdapterError(
                "internal", "Capital.com's market list could not be read%s; "
                "ask again in a moment" % (
                    " (%s)" % self._catalog_error if self._catalog_error
                    else ""), retryable=True)
        return {"instruments": out}

    def _instrument(self, symbol) -> dict:
        sym = str(symbol or "")
        with self._lock:
            spec = self._catalog.get(sym)
        if spec is None and not self._catalog_ready.is_set():
            left = self.client.time_left() or 4.0
            self._catalog_ready.wait(timeout=max(0.05, min(4.0, left - 2)))
            with self._lock:
                spec = self._catalog.get(sym)
        if spec is None:
            raise AdapterError("unknown_symbol",
                               "%r is not in Capital.com's catalog" % sym)
        return spec

    # == the account =====================================================
    def _read_accounts(self, kind: str = "read") -> list:
        out = self.client.call("GET", "/accounts", kind=kind) or {}
        rows = [a for a in out.get("accounts") or [] if isinstance(a, dict)]
        with self._lock:
            self._accounts = rows
            mine = next((a for a in rows if str(a.get("accountId"))
                         == self.client.bound), None)
            if mine is not None:
                self._account, self._account_t = mine, time.monotonic()
        return rows

    def _account_view(self, a: dict) -> dict:
        """documented fields: https://open-api.capital.com/ (balance {balance,
        deposit, profitLoss, available}). Their meaning is not documented;
        the sample's arithmetic (balance = deposit + profitLoss) is read as:
        `deposit` the cash, `balance` the equity, `available` the free
        margin, so margin used is balance less available (assumed, verify on
        demo)."""
        b = a.get("balance") if isinstance(a.get("balance"), dict) else {}
        cash, equity = _num(b.get("deposit")), _num(b.get("balance"))
        free = _num(b.get("available"))
        if equity is None:
            equity = cash if cash is not None else 0.0
        if cash is None:
            cash = equity - (_num(b.get("profitLoss"), 0.0) or 0.0)
        if free is None:
            free = equity
        return {"balance": round(cash, 2), "equity": round(equity, 2),
                "margin_used": round(max(0.0, equity - free), 2),
                "margin_free": round(free, 2),
                "currency": str(a.get("currency") or self.account_currency),
                "account_id": str(a.get("accountId") or ""),
                "label": str(a.get("accountName") or "")}

    def handle_account_get(self, p):
        with self._lock:
            fresh = (self._account is not None
                     and time.monotonic() - self._account_t < CACHE_FRESH)
            acct = self._account
        if not fresh:
            try:
                self._read_accounts("read")
            except CapitalError as e:
                raise self._err(e) from None
            with self._lock:
                acct = self._account
        if acct is None:
            raise AdapterError("internal", "Capital.com did not list account "
                               "%s" % self.client.bound, retryable=True)
        return self._account_view(acct)

    def handle_accounts_list(self, p):
        """Answerable before the session is opened (SPEC 1.7): it logs in,
        if need be, and reads GET /accounts. Every account a login reaches
        on one host has that host's money: demo on the demo host (assumed,
        verify on demo: the API names no demo or live per account)."""
        cfg = self.cfg
        self._endpoint_refusal()
        if not (cfg["key"] and cfg["identifier"] and cfg["password"]):
            raise AdapterError("unauthorized", "Capital.com needs your API "
                               "key, your login email and the API key's own "
                               "password")
        if cfg.get("cred_fault"):
            raise AdapterError("unauthorized", "Capital.com cannot be asked: "
                               + cfg["cred_fault"] + ".")
        try:
            if not self.client.has_session():
                if self._sync_clock_due():
                    self._sync_clock()
                self.client.session("read", 4.0)
            rows = self._read_accounts("read")
        except CapitalError as e:
            raise self._login_error(e) from None
        bound = self.client.bound if self._opened else ""
        out = []
        for a in rows:
            aid = str(a.get("accountId") or "")
            if not aid:
                continue
            b = a.get("balance") if isinstance(a.get("balance"), dict) else {}
            row = {"id": aid, "mode": self.mode}
            if a.get("accountName"):
                row["label"] = str(a["accountName"])
            if a.get("currency"):
                row["currency"] = str(a["currency"])
            if a.get("accountType"):
                row["type"] = str(a["accountType"]).lower()
            if _num(b.get("deposit")) is not None:
                row["balance"] = _num(b.get("deposit"))
            if bound:
                row["selected"] = aid == bound
            out.append(row)
        return {"accounts": out}

    def _sync_clock_due(self) -> bool:
        return time.monotonic() >= self._next_clock

    # == the book: Capital.com's deals and working orders ================
    def _book_rows(self, path: str, key: str, kind: str,
                   max_wait: float = 3.0) -> list:
        """The list a read of the book answers (documented:
        https://open-api.capital.com/, GET /positions {"positions": [...]},
        GET /workingorders {"workingOrders": [...]}; every saved example of
        https://github.com/capital-com-sv/capital-api-postman holds a list).

        An answer without it is read again at once. Once, it is a broken
        answer and the read FAILS: never an empty book (taken as one, a
        single 200 with no list would close every position and cancel every
        working order). An empty answer twice in a row is taken as how
        Capital.com says the book is empty, and said once on stderr: how an
        empty book is answered is not documented (assumed, verify on demo: an
        empty list), and a fresh demo account's book is empty."""
        answers = []
        for _ in (0, 1):
            answer = self.client.call("GET", path, kind=kind,
                                      max_wait=max_wait)
            rows = answer.get(key) if isinstance(answer, dict) else None
            if isinstance(rows, list):
                return rows
            answers.append(answer)
        if all(a is None or a == {} for a in answers):
            with self._lock:
                first = "empty:" + key not in self._notices
                self._notices["empty:" + key] = True
            if first:
                log("Capital.com answered GET %s twice with no %s list: read "
                    "as an empty book" % (path, key))
            return []
        raise CapitalError(0, "Capital.com's answer carried no %s list; read "
                           "again" % key, sent=True)

    @staticmethod
    def _deal_of(item: dict) -> dict | None:
        """One row of GET /positions (documented: {position, market}). The
        names of a deal's stop, target and trail are NOT in Capital.com's
        samples: stopLevel, profitLevel (limitLevel), trailingStop and
        trailingStopDistance are read (assumed, verify on demo)."""
        pos = item.get("position") if isinstance(item, dict) else None
        mkt = item.get("market") if isinstance(item, dict) else None
        if not isinstance(pos, dict) or not pos.get("dealId"):
            return None
        mkt = mkt if isinstance(mkt, dict) else {}
        shows = any(k in pos for k in ("stopLevel", "profitLevel",
                                       "limitLevel", "trailingStop"))
        direction = str(pos.get("direction") or "").upper()
        tdist = _num(pos.get("trailingStopDistance"))
        if tdist is None and pos.get("trailingStop"):
            tdist = _num(pos.get("stopDistance"))
        tp = _num(pos.get("profitLevel"))
        if tp is None:
            tp = _num(pos.get("limitLevel"))
        return {"id": str(pos["dealId"]),
                "epic": str(mkt.get("epic") or pos.get("epic") or ""),
                "dir": direction, "size": _fixed(pos.get("size")),
                "level": _num(pos.get("level")),
                "sl": _num(pos.get("stopLevel")), "tp": tp,
                "trailing": bool(pos.get("trailingStop")), "tdist": tdist,
                "gsl": bool(pos.get("guaranteedStop")),
                "upl": _num(pos.get("upl")),
                "ccy": str(pos.get("currency") or ""),
                "contract": _num(pos.get("contractSize"), 1.0) or 1.0,
                "opened": parse_utc(pos.get("createdDateUTC")),
                "wo": str(pos.get("workingOrderId") or ""),
                "ref": str(pos.get("dealReference") or ""),
                "shows": shows,
                "bid": _num(mkt.get("bid")), "offer": _num(mkt.get("offer")),
                "name": str(mkt.get("instrumentName") or "")}

    @staticmethod
    def _order_of(item: dict) -> dict | None:
        """One row of GET /workingorders (documented: {workingOrderData,
        marketData}; stopDistance and profitDistance signed from the order's
        level, as the sample shows them)."""
        w = item.get("workingOrderData") if isinstance(item, dict) else None
        if not isinstance(w, dict) or not w.get("dealId"):
            return None
        m = item.get("marketData") if isinstance(item.get("marketData"),
                                                 dict) else {}
        level = _num(w.get("orderLevel"))
        direction = str(w.get("direction") or "").upper()
        sd, pd = _num(w.get("stopDistance")), _num(w.get("profitDistance"))
        sl = tp = None
        # a level from a distance is rounded to the decimals of the two, so
        # 1.12396 - 0.002 reads 1.12196 and not 1.1219599999999999
        if level is not None and sd is not None:
            sl = round(level - abs(sd) if direction == "BUY"
                       else level + abs(sd), max(_decimals(level),
                                                 _decimals(abs(sd))))
        if level is not None and pd is not None:
            tp = round(level + abs(pd) if direction == "BUY"
                       else level - abs(pd), max(_decimals(level),
                                                 _decimals(abs(pd))))
        sl = _num(w.get("stopLevel"), sl)
        tp = _num(w.get("profitLevel"), tp)
        return {"id": str(w["dealId"]),
                "epic": str(w.get("epic") or m.get("epic") or ""),
                "dir": direction, "size": _fixed(w.get("orderSize")),
                "level": level, "type": str(w.get("orderType") or "").upper(),
                "tif": str(w.get("timeInForce") or ""),
                "gtd": parse_utc(w.get("goodTillDateUTC")),
                "created": parse_utc(w.get("createdDateUTC")),
                "sl": sl, "tp": tp, "sd": sd, "pd": pd,
                "trailing": bool(w.get("trailingStop")),
                "gsl": bool(w.get("guaranteedStop"))}

    def _claim(self, key: str, seconds: float = 20.0):
        with self._lock:
            self._claims[key] = time.monotonic() + seconds

    def _unclaim(self, key: str):
        with self._lock:
            self._claims.pop(key, None)

    def _claimed(self, key: str) -> bool:
        until = self._claims.get(key)
        if until is None:
            return False
        if time.monotonic() > until:
            self._claims.pop(key, None)
            return False
        return True

    def _once(self, key: str) -> bool:
        """True the first time `key` is asked about (the dedup of every
        event that moves money or ends an order). Caller holds the lock."""
        if key in self._seen:
            return False
        self._seen[key] = True
        self._unsaved = True
        return True

    def _read_book(self, kind: str = "background", baseline: bool = False,
                   max_wait: float = 3.0):
        """Working orders FIRST, then positions: a working order that fills
        between the two reads is then seen as a deal (and, at the next read,
        as gone), never as gone with no deal (the reference adapter's rule)."""
        orders = self._book_rows("/workingorders", "workingOrders", kind,
                                 max_wait)
        positions = self._book_rows("/positions", "positions", kind, max_wait)
        with self._lock:
            self._apply_orders(orders, baseline)
            self._apply_positions(positions, baseline)
            if self._unsaved:
                self._unsaved = False
                save = True
            else:
                save = False
        if save:
            self._save_state()

    def _read_positions(self, kind: str = "read", max_wait: float = 3.0):
        positions = self._book_rows("/positions", "positions", kind, max_wait)
        with self._lock:
            self._apply_positions(positions, False)

    def _apply_orders(self, rows: list, baseline: bool):
        """Caller holds the lock."""
        new = {}
        for item in rows:
            o = self._order_of(item)
            if o:
                new[o["id"]] = o
        self._worders_t = time.monotonic()
        if baseline:
            self._worders = new
            return
        old = self._worders
        keep = {}
        for oid, o in new.items():
            # Listed: it has not ended, whatever an earlier read made of it
            # (the broker is the truth, SPEC 7).
            gone = self._wo_gone.pop(oid, None)
            if oid in old:
                keep[oid] = dict(o, size0=old[oid].get("size0") or o["size"])
                if oid in self._held_wo and oid not in self._wo_oid:
                    self._attribute_order(keep[oid])
                continue
            if gone is not None:
                keep[oid] = dict(o, size0=gone["order"].get("size0")
                                 or o["size"])
                continue            # known before it left: nothing new
            if oid not in self._wo_oid and self._claimed("epic:" + o["epic"]):
                continue            # the handler placing it says what it is
            o["size0"] = o["size"]
            keep[oid] = o
            if oid in self._wo_oid:
                continue
            self._attribute_order(o)
        for oid, o in old.items():
            if oid not in new and oid not in self._wo_gone:
                if self._claimed("wo:" + oid):
                    continue        # its cancel's handler says what happened
                self._wo_gone[oid] = {"order": o, "tries": 0,
                                      "at": self.now_ms()}
        self._worders = keep

    def _apply_positions(self, rows: list, baseline: bool):
        """Caller holds the lock."""
        new = {}
        for item in rows:
            d = self._deal_of(item)
            if d:
                new[d["id"]] = d
        self._deals_t = time.monotonic()
        if baseline:
            self._deals = new
            return
        old = self._deals
        keep = {}
        # An epic an order is being placed on is FROZEN here: its deals keep
        # the view the order's handler gives them, a new one is not looked
        # at, a gone or smaller one is not explained. The handler accounts
        # for what its order did (opened, offset, closed), and nothing is
        # told twice. The next read after it is done sees the rest.
        frozen = {k[6:] for k in list(self._claims)
                  if k.startswith("epic:") and self._claimed(k)}
        for did, d in new.items():
            was = old.get(did)
            # Listed: it has not closed, whatever an earlier read or a close
            # this adapter sent made of it (SPEC 7; a close Capital.com
            # refused must not be told as a close).
            back = self._withdraw_gone(d)
            if d["epic"] in frozen:
                if was is not None:
                    keep[did] = was
                continue
            if was is not None:
                keep[did] = d
                self._deal_changed(was, d)
                if did in self._held and did not in self._deal_oid:
                    self._attribute(d)
                continue
            keep[did] = d
            if did in self._deal_oid or back:
                continue            # attributed already, or known before
            self._attribute(d)
        for did, d in old.items():
            if did in new or did in self._gone:
                continue
            if d["epic"] in frozen:
                keep[did] = d
                continue
            if self._claimed("deal:" + did):
                continue            # the close being made says what happened
            self._gone[did] = {"deal": d, "tries": 0, "qty": d["size"],
                               "at": self.now_ms()}
        self._deals = keep

    def _withdraw_gone(self, d: dict) -> bool:
        """A listed deal is not gone: what said it was (a read that missed
        it, a close handed to the poller) is withdrawn, and a cut of its size
        that is back is too. True when something was. Caller holds the lock.
        The close's own record (_closing) stays: its confirmation still says
        whether it happened (see _poll_closes)."""
        did = d["id"]
        back = self._gone.pop(did, None) is not None
        tol = self._tol(d["epic"])
        for key in [k for k in self._gone if k.startswith(did + ":")]:
            if _dec(d["size"]) >= _dec(self._gone[key]["deal"]["size"]) - tol:
                del self._gone[key]
                back = True
        return back

    def _deal_changed(self, was: dict, d: dict):
        """One deal read again. A smaller size: part of it was closed by
        Capital.com (a close-out) or by an order elsewhere; a trailing stop
        that moved: `position.trailed`. Caller holds the lock."""
        cut = _dec(was["size"]) - _dec(d["size"])
        if cut > 0 and not self._claimed("deal:" + d["id"]):
            key = "%s:%s" % (d["id"], d["size"])
            self._gone.setdefault(key, {"deal": dict(was, id=d["id"]),
                                        "tries": 0, "qty": _fixed(cut),
                                        "partial": True,
                                        "at": self.now_ms()})
        if (d.get("trailing") and d.get("sl") is not None
                and was.get("sl") is not None and d["sl"] != was["sl"]):
            spec = self._catalog.get(d["epic"]) or {}
            tick = spec.get("tick_size") or 0
            self.emit("position.trailed", {
                "position_id": d["id"], "sl": d["sl"],
                "trail_points": round(d["tdist"] / tick, 6)
                if tick and d.get("tdist") else 0})
        lv = self._levels.get(d["id"])
        if lv is not None and d.get("shows"):
            # Capital.com shows the levels itself: those are the truth
            lv.update(sl=d.get("sl"), tp=d.get("tp"))

    # == events: each one once ===========================================
    def _emit_fill(self, cid, order_id, d: dict, qty, price, final: bool,
                   remaining, t=None, fid: str | None = None,
                   side: str | None = None) -> bool:
        """One execution. Its id is the deal's own (an opened deal is one
        execution), so a re-emission keeps it; emitted once. Caller holds
        the lock."""
        fid = fid or "f-" + d["id"]
        if not self._once("fill:" + fid):
            return False
        fill = {"fill_id": fid, "client_id": cid or None,
                "order_id": order_id, "position_id": d["id"],
                "symbol": d["epic"],
                "side": side or ("buy" if d["dir"] == "BUY" else "sell"),
                "qty": float(_dec(qty)), "price": price, "fees": 0.0,
                "time": t or self.now_ms()}
        self._fills.append(dict(fill))
        if final:
            self.emit("order.filled", fill)
        else:
            fill["remaining"] = float(max(_dec(remaining), D0))
            self.emit("order.partial", fill)
        self._account_t = 0.0
        self._wake.set()
        return True

    def _emit_closed(self, d: dict, qty, price, reason: str, t=None,
                     detail: str | None = None, close_id: str | None = None,
                     pnl="calc", full: bool = True):
        """`position.closed`, once per close. `swap` and `commission` are 0:
        Capital.com books overnight funding and fees as transactions that
        name no deal (they arrive as balance.operation). Caller holds the
        lock."""
        cid = close_id or "c-" + d["id"]
        if not self._once("close:" + cid):
            return
        when = t or self.now_ms()
        evt = {"close_id": cid, "position_id": d["id"],
               "client_id": self._deal_cid.get(d["id"]),
               "qty": float(_dec(qty)), "price": price,
               "swap": 0.0, "commission": 0.0, "reason": reason,
               "time": when}
        why = None
        if pnl == "calc":
            pnl, why = self._realized(d, price, qty)
        if pnl is not None:
            evt["realized_pnl"] = round(float(pnl), 2)
        notes = [x for x in (detail, why) if x]
        if notes:
            evt["reason_detail"] = "; ".join(notes)
        self.emit("position.closed", evt)
        self._fills.append({
            "fill_id": "x-" + cid, "client_id": None, "order_id": None,
            "position_id": d["id"], "symbol": d["epic"],
            "side": "sell" if d["dir"] == "BUY" else "buy",
            "qty": float(_dec(qty)), "price": price, "fees": 0.0,
            "time": when})
        if full:
            self._levels.pop(d["id"], None)
        self._account_t = 0.0
        self._wake.set()

    def _realized(self, d: dict, price, qty) -> tuple:
        """The realised P/L of a close, in the account's currency, when it
        can be had exactly: an instrument priced in the account's currency
        (price difference x size x contract size). Otherwise it is left out
        and said: Capital.com converts at its own rate (with a fee on the
        conversion), and its figure is a transaction that names no deal."""
        if price is None or d.get("level") is None:
            return None, "the price of this close is not known"
        spec = self._catalog.get(d["epic"]) or {}
        ccy = spec.get("quote_ccy") or (self._markets.get(d["epic"]) or {}
                                        ).get("currency")
        if not ccy or ccy != self.account_currency:
            return None, ("the realised profit or loss is booked by "
                          "Capital.com in %s at its own rate and is not "
                          "worked out here" % self.account_currency)
        diff = (price - d["level"]) if d["dir"] == "BUY" else (
            d["level"] - price)
        return diff * float(_dec(qty)) * float(d.get("contract") or 1.0), \
            None

    # == orders of unknown fate: found again by what they made ===========
    def _open_entries(self, kinds) -> list:
        return sorted((e for e in self._journal.values()
                       if e.get("state") in ("sending", "unknown", "sent")
                       and e.get("kind") in kinds),
                      key=lambda e: int(e.get("at") or 0))

    @staticmethod
    def _fits(e: dict, t, epic: str, direction: str) -> bool:
        """Could something made at `t` on `epic` in `direction` be what order
        `e` made? Its epic and direction, made from just before the post to a
        minute after it (MATCH_LATE_MS: an order of the last run, looked for
        after a restart, never takes a deal made long after it)."""
        if e["epic"] != epic or e["dir"] != direction:
            return False
        if t is None:
            return True
        at = int(e["at"])
        return at - MATCH_SLACK_MS <= t <= at + MATCH_LATE_MS

    def _deal_fits(self, e: dict, d: dict) -> bool:
        """... and a deal that was not there before it was sent, of a size
        that fits in what is still unfilled of it. Caller holds the lock."""
        if not self._fits(e, d["opened"], d["epic"], d["dir"]):
            return False
        if d["id"] in (e.get("before") or ()):
            return False
        left = _dec(e["size"]) - _dec(e.get("filled") or 0)
        return _dec(d["size"]) <= left + self._tol(e["epic"])

    def _order_fits(self, e: dict, o: dict) -> bool:
        if (e["kind"].upper() != o["type"]
                or _dec(e["size"]) != _dec(o["size"])
                or not self._fits(e, o["created"], o["epic"], o["dir"])):
            return False
        tick = (self._catalog.get(o["epic"]) or {}).get("tick_size") or 0
        return not (o["level"] is None or e.get("level") is None
                    or abs(o["level"] - float(e["level"])) > tick / 2 + 1e-12)

    @staticmethod
    def _claimant(cands: list) -> tuple:
        """Of the orders (journalled, oldest first) that could have made one
        deal or working order, which did. ("sent", e) while one Capital.com
        acknowledged (it gave a dealReference) could have: its confirmation
        names what it made (documented: GET /confirms/{dealReference},
        affectedDeals), so that is waited for, and an order of UNKNOWN fate
        must not take the thing meanwhile (or a later order's deal is
        credited to an order that never reached Capital.com, and the later
        order is told it dealt nothing). Else ("unknown", the
        oldest order of unknown fate it fits), or (None, None)."""
        sent = [e for e in cands
                if e.get("state") == "sent" and e.get("ref")]
        if sent:
            return "sent", sent[0]
        rest = [e for e in cands if e.get("state") in ("sending", "unknown")]
        return ("unknown", rest[0]) if rest else (None, None)

    def _deal_claimant(self, d: dict) -> tuple:
        return self._claimant([e for e in self._open_entries(("market",))
                               if self._deal_fits(e, d)])

    def _order_claimant(self, o: dict) -> tuple:
        return self._claimant([e for e in self._open_entries(("limit",
                                                                "stop"))
                               if self._order_fits(e, o)])

    def _match_deal_entry(self, d: dict):
        """The order a deal must be credited to NOW (flatten_all is about to
        close it): one Capital.com acknowledged, else the oldest of unknown
        fate. Caller holds the lock."""
        return self._deal_claimant(d)[1]

    def _match_order_entry(self, o: dict):
        return self._order_claimant(o)[1]

    def _attribute(self, d: dict, baseline: bool = False):
        """A deal in the book that no event has named yet: whose it is, told
        once. Held (listed, not told, looked at again at every read) while an
        acknowledged order could own it and its confirmation has not said
        (see _claimant), or while the working order it names is held. At a
        start (`baseline`), a deal that is nobody's was there before this
        run: it is reconciled (positions.list), not announced. Caller holds
        the lock."""
        did, wo = d["id"], d.get("wo") or ""
        now = self.now_ms()
        if wo and wo in self._held_wo and wo not in self._wo_oid:
            self._held.setdefault(did, {"since": now, "baseline": baseline})
            return
        if wo and (wo in self._wo_oid or (not baseline and (
                wo in self._worders or wo in self._wo_gone))):
            self._held.pop(did, None)
            self._wo_fill(d)
            return
        how, e = self._deal_claimant(d)
        if how == "sent":
            h = self._held.setdefault(did, {"since": now,
                                            "baseline": baseline})
            if now - h["since"] <= HOLD_MAX_MS:
                return
        held = self._held.pop(did, None)
        if e is not None:
            self._resolve_filled(e, [d])
            return
        if baseline or (held or {}).get("baseline"):
            return
        # A deal this adapter did not open: an order placed elsewhere. It is
        # never left to appear without an event (SPEC 5).
        self._deal_oid[did] = wo or did
        self._emit_fill(None, wo or did, d, _dec(d["size"]), d["level"],
                        final=True, remaining=None, t=d["opened"])

    def _attribute_order(self, o: dict, baseline: bool = False):
        """The same for a working order no event has named. Caller holds the
        lock."""
        oid = o["id"]
        now = self.now_ms()
        how, e = self._order_claimant(o)
        if how == "sent":
            h = self._held_wo.setdefault(oid, {"since": now,
                                               "baseline": baseline})
            if now - h["since"] <= HOLD_MAX_MS:
                return
        held = self._held_wo.pop(oid, None)
        if e is not None:
            self._resolve_resting(e, o)
            return
        if baseline or (held or {}).get("baseline"):
            return
        # A working order made elsewhere (Capital.com's own platform):
        # announced, so a platform is not left with an order it cannot see
        # come and go.
        if self._once("placed:" + oid):
            self.emit("order.placed", {
                "client_id": None, "order_id": oid,
                "type": "limit" if o["type"] == "LIMIT" else "stop",
                "price": o["level"], "expires_at": o["gtd"]})

    def _tol(self, epic: str) -> Decimal:
        step = (self._catalog.get(epic) or {}).get("lot_step") or 0
        return _dec(step) / 2 if step else Decimal("1e-9")

    def _lot(self, epic: str) -> float:
        """A market's contract size, for a deal known from its confirmation
        before the book has shown it (taken as 1, a close before the next
        read would get realised P/L wrong by that factor).
        assumed, verify on demo: a deal's own contractSize (GET /positions)
        is its market's lotSize (both 1 in the documented samples)."""
        return float((self._markets.get(epic) or {}).get("lot")
                     or (self._catalog.get(epic) or {}).get("contract_size")
                     or 1.0)

    def _settle_entry(self, e: dict, state: str, result=None, error=None,
                      ended: str | None = None, reason=None):
        """An order's outcome is known: written down, and its result is the
        answer to any retry of its client_id (SPEC 2). Caller holds the
        lock."""
        e["state"] = state
        if ended and not e.get("ended"):
            e["ended"] = {"state": ended, "at": self.now_ms(),
                          "reason": reason}
        if result is not None:
            e["result"] = dict(result)
            if e.get("cid") and e.get("sig"):
                self._idempotent[e["cid"]] = dict(result, _sig=e["sig"])
        if error is not None:
            e["error"] = dict(error)
        self._unsaved = True

    def _accepted(self, e: dict):
        if self._once("acc:" + e["order_id"]):
            self.emit("order.accepted", {"client_id": e.get("cid") or None,
                                         "order_id": e["order_id"]})

    def _resolve_filled(self, e: dict, deals: list, reduced=(),
                        executed=None, price=None, t=None):
        """Report what one market order did, each execution once: the deals
        it reduced (`reduced`: [(deal, qty)], standard mode's offsets) and
        the deals it opened. `executed` is what Capital.com says was dealt
        in all, when it says; an order dealt in full is `order.filled`, one
        dealt in part and over (its confirmation says so) ends with the
        rest `order.cancelled`. Caller holds the lock."""
        oid = e["order_id"]
        self._accepted(e)
        total = _dec(e["size"])
        done = _dec(e.get("filled") or 0)
        parts = [("cut", d, _dec(q)) for d, q in reduced] + [
            ("open", d, _dec(d["size"])) for d in deals]
        for i, (how, d, q) in enumerate(parts):
            fid = "f-" + d["id"] if how == "open" else "f-%s-%s" % (oid,
                                                                    d["id"])
            if "fill:" + fid in self._seen:
                continue
            done += q
            last = done >= total - self._tol(e["epic"])
            if how == "open":
                self._deal_oid[d["id"]] = oid
                self._held.pop(d["id"], None)
                if e.get("cid"):
                    self._deal_cid[d["id"]] = e["cid"]
                if e["kind"] in ("limit", "stop"):
                    self._deal_from[d["id"]] = e["kind"]
                lv = e.get("levels")
                if lv:
                    self._levels[d["id"]] = dict(lv)
                self._emit_fill(e.get("cid"), oid, d, q, d["level"],
                                last, total - done, t or d["opened"], fid)
            else:
                side = "buy" if e["dir"] == "BUY" else "sell"
                px = price if price is not None else d.get("level")
                self._emit_fill(e.get("cid"), oid, d, q, px, last,
                                total - done, t, fid, side=side)
                left = _dec(d["size"]) - q
                self._emit_closed(
                    d, q, px, "manual", t=t, full=left <= 0,
                    close_id="c-%s-%s" % (oid, d["id"]),
                    detail="offset by an opposite order (Capital.com's "
                           "standard mode)")
            e.setdefault("deals", []).append(d["id"])
        e["filled"] = _fixed(done)
        over = executed is not None or done >= total - self._tol(e["epic"])
        if over:
            if done < total - self._tol(e["epic"]):
                if self._once("end:" + oid):
                    self.emit("order.cancelled", {
                        "order_id": oid, "client_id": e.get("cid") or None,
                        "code": "x_capitalcom_unfilled",
                        "message": "Capital.com dealt %s of %s; the rest "
                                   "was not filled" % (_fixed(done),
                                                       e["size"])})
            else:
                self._once("end:" + oid)
            self._settle_entry(e, "filled", {"order_id": oid,
                                             "status": "accepted"})

    def _resolve_resting(self, e: dict, o: dict):
        """A working order of this adapter's is live at Capital.com.
        Caller holds the lock."""
        oid = e["order_id"]
        e["wo"] = o["id"]
        self._wo_oid[o["id"]] = oid
        self._oid_wo[oid] = o["id"]
        self._held_wo.pop(o["id"], None)
        self._accepted(e)
        if self._once("placed:" + o["id"]):
            self.emit("order.placed", {
                "client_id": e.get("cid") or None, "order_id": oid,
                "type": e["kind"], "price": o["level"],
                "expires_at": o["gtd"]})
        self._settle_entry(e, "resting", {"order_id": oid,
                                          "status": "resting"})

    def _entry_for_wo(self, wo: str):
        oid = self._wo_oid.get(wo)
        if not oid:
            return None
        return next((e for e in self._journal.values()
                     if e.get("order_id") == oid), None)

    def _wo_fill(self, d: dict):
        """A deal that a working order made (its workingOrderId names the
        order; assumed, verify on demo: the documented field's meaning).
        Caller holds the lock."""
        wo = d["wo"]
        o = (self._worders.get(wo)
             or (self._wo_gone.get(wo) or {}).get("order") or {})
        oid = self._wo_oid.get(wo) or wo
        e = self._entry_for_wo(wo)
        cid = e.get("cid") if e else None
        total = _dec(o.get("size0") or o.get("size") or d["size"])
        done = _dec((e or {}).get("filled") or 0) if e else _dec(
            self._notices.get("wo_done:" + wo) or 0)
        done += _dec(d["size"])
        if e is not None:
            e["filled"] = _fixed(done)
            e.setdefault("deals", []).append(d["id"])
        else:
            self._notices["wo_done:" + wo] = _fixed(done)
        self._deal_oid[d["id"]] = oid
        self._held.pop(d["id"], None)
        if cid:
            self._deal_cid[d["id"]] = cid
        typ = (e or {}).get("kind") or ("limit" if o.get("type") == "LIMIT"
                                        else "stop" if o else None)
        if typ:
            self._deal_from[d["id"]] = typ
        lv = (e or {}).get("levels")
        if lv:
            self._levels[d["id"]] = dict(lv)
        final = done >= total - self._tol(d["epic"])
        self._emit_fill(cid, oid, d, d["size"], d["level"], final,
                        total - done, d["opened"])
        if final:
            self._once("end:" + oid)
            if e is not None:
                self._settle_entry(e, "done", ended="filled")

    # == disappearances, explained by the activity history ===============
    def _activity_needed(self) -> int | None:
        """Seconds of activity history the unexplained ones need, or None."""
        with self._lock:
            ats = [g["at"] for g in self._gone.values()] + [
                g["at"] for g in self._wo_gone.values()]
        if not ats:
            return None
        return int(min(86400, max(120, (self.now_ms() - min(ats)) / 1000
                                  + 120)))

    @staticmethod
    def _close_row(d: dict, rows: list):
        """The activity row that closed deal `d` (rows newest first): a
        POSITION row of its dealId in the closing direction (or, with no
        direction given, after it opened) whose status says it happened
        (DONE_STATUS: a REJECTED row is a close that did NOT happen)."""
        did = d["id"]
        closing = "SELL" if d["dir"] == "BUY" else "BUY"
        for r in rows:
            if (str(r.get("dealId")) != did
                    or str(r.get("type") or "").upper() != "POSITION"
                    or str(r.get("status") or "").upper() not in DONE_STATUS):
                continue
            det = r.get("details") if isinstance(r.get("details"), dict) \
                else {}
            way = str(det.get("direction") or "").upper()
            when = parse_utc(r.get("dateUTC"))
            if way == closing or (not way and when and d.get("opened")
                                  and when > d["opened"] + 1000):
                return r
        return None

    def _close_reason(self, row: dict, did: str, own: bool) -> tuple:
        """(reason, reason_detail) of a close from its activity row's
        `source`. `own`: this adapter sent a close for this deal (a
        position.close or flatten_all whose confirmation it did not see), so
        a USER row is that close, with that close's reason."""
        src = str(row.get("source") or "").upper()
        if src == "USER" and own:
            return (self._closing.get(did) or {}).get("reason") or "manual", \
                None
        reason = CLOSE_REASON.get(src, "unknown")
        if src == "USER":
            return reason, "closed outside this adapter"
        if reason == "unknown":
            return reason, "Capital.com: closed by %s" % (src or "?")
        return reason, None

    def _tell_close(self, d: dict, qty, row: dict, close_id: str,
                    full: bool):
        """position.closed from the row that closed the deal. Caller holds
        the lock."""
        reason, detail = self._close_reason(row, d["id"], full and d["id"]
                                            in self._closing)
        det = row.get("details") if isinstance(row.get("details"), dict) \
            else {}
        self._emit_closed(d, qty, _num(det.get("level")), reason,
                          t=parse_utc(row.get("dateUTC")), detail=detail,
                          close_id=close_id, full=full)
        if full:
            self._closing.pop(d["id"], None)
            self._deals.pop(d["id"], None)

    def _wo_deals(self, o: dict, xrow: dict, rows: list, done) -> list:
        """The deals a working order made, read from the activity history
        when the book never showed them: it filled, and they closed, between
        two reads (without this such an order is told filled with no
        position, and its deal is never told closed). Their own POSITION
        rows: its epic and direction, a status that says done, within 2 s of
        its EXECUTED row, deals this adapter knows nothing of, oldest first,
        while they fit in what was left of it. assumed, verify on demo: a
        deal's activity rows name no working order (documented fields:
        dealId, epic, source, type, status, and with detailed=true
        details.size, .direction and .level: https://open-api.capital.com/,
        https://github.com/capital-com-sv/capital-api-postman), so the match
        is by epic, direction, time and size. Caller holds the lock."""
        t = parse_utc(xrow.get("dateUTC"))
        if t is None:
            return []
        left = _dec(o.get("size0") or o.get("size")) - _dec(done)
        tol = self._tol(o["epic"])
        known = set(self._deal_oid) | set(self._deals) | set(self._held) | {
            k.split(":", 1)[0] for k in self._gone}
        out = []
        for r in sorted(rows, key=lambda x: parse_utc(x.get("dateUTC")) or 0):
            det = r.get("details") if isinstance(r.get("details"), dict) \
                else {}
            did = str(r.get("dealId") or "")
            when = parse_utc(r.get("dateUTC"))
            size = _dec(det.get("size"))
            if (str(r.get("type") or "").upper() != "POSITION"
                    or str(r.get("epic") or "") != o["epic"]
                    or str(r.get("status") or "").upper() not in DONE_STATUS
                    or str(det.get("direction") or "").upper() != o["dir"]
                    or not did or did == o["id"] or did in known
                    or when is None or abs(when - t) > 2000
                    or size <= 0 or size > left + tol):
                continue
            out.append({"id": did, "epic": o["epic"], "dir": o["dir"],
                        "size": _fixed(size), "level": _num(det.get("level")),
                        "sl": None, "tp": None, "trailing": False,
                        "tdist": None, "gsl": bool(det.get("guaranteedStop")),
                        "upl": None, "ccy": "", "contract": self._lot(
                            o["epic"]), "opened": when, "wo": o["id"],
                        "ref": "", "shows": False, "bid": None,
                        "offer": None, "name": ""})
            known.add(did)
            left -= size
        return out

    def _settle_gone(self, rows: list):
        """Deals and working orders that left the book without this adapter
        closing or cancelling them, told from GET /history/activity (newest
        first; documented: https://open-api.capital.com/, `source` CLOSE_OUT,
        DEALER, SL, SYSTEM, TP or USER; `details` with detailed=true,
        documented: https://github.com/capital-com-sv/capital-api-postman).
        A deal Capital.com lists again is never here (see _withdraw_gone).
        While an order is being placed on an epic, what left the book there
        waits: the order's handler accounts for what its order did (the
        freeze, see _apply_positions). Caller holds the lock."""
        frozen = {k[6:] for k in list(self._claims)
                  if k.startswith("epic:") and self._claimed(k)}
        for key, g in list(self._gone.items()):
            d = g["deal"]
            did = d["id"]
            if d["epic"] in frozen:
                continue
            row = self._close_row(d, rows)
            if row is None and g["tries"] < GONE_RETRIES:
                g["tries"] += 1
                continue
            del self._gone[key]
            part = bool(g.get("partial"))
            cid = ("c-%s-%s" % (did, key.split(":", 1)[1]) if part
                   else "c-" + did)
            if row is None:
                self._emit_closed(
                    d, g["qty"], None, "unknown", close_id=cid, full=not part,
                    detail="Capital.com's activity history has no record of "
                           "this close")
                if not part:
                    self._closing.pop(did, None)
                    self._deals.pop(did, None)
                continue
            self._tell_close(d, g["qty"], row, cid, not part)
        now = self.now_ms()
        for wo, g in list(self._wo_gone.items()):
            o = g["order"]
            oid = self._wo_oid.get(wo) or wo
            e = self._entry_for_wo(wo)
            if "end:" + oid in self._seen:
                del self._wo_gone[wo]
                continue
            done = _dec((e or {}).get("filled") or self._notices.get(
                "wo_done:" + wo) or 0)
            mine = [r for r in rows if str(r.get("dealId")) == wo
                    and str(r.get("type") or "").upper() == "WORKING_ORDER"]
            status = str((mine[0] if mine else {}).get("status")
                         or "").upper()
            made = self._wo_deals(o, mine[0], rows, done) \
                if status == "EXECUTED" else []
            if not made and status in ("", "EXECUTED", "ACCEPTED",
                                       "MODIFIED") and (
                    g["tries"] < GONE_RETRIES):
                g["tries"] += 1       # its deal may be a read behind
                continue
            del self._wo_gone[wo]
            cid = (e or {}).get("cid") or None
            self._once("end:" + oid)
            if e is not None:
                self._settle_entry(
                    e, "done", ended="filled" if status == "EXECUTED"
                    else "expired" if status == "EXPIRED" else "cancelled",
                    reason=None if status in ("EXECUTED", "EXPIRED") or (
                        status == "CANCELLED" and e.get("cancelling"))
                    else "x_capitalcom_" + (status.lower() or "gone"))
            if status == "EXECUTED":
                # Capital.com says it executed, and its deal was never seen
                # in the book (it filled and closed between two reads, or
                # while this adapter was not running). Each deal its rows
                # show is told: the fill, naming the position, then the
                # close with its reason. What no row accounts for is told as
                # a fill with no position to name, at the order's level.
                size0 = _dec(o.get("size0") or o.get("size"))
                tol = self._tol(o.get("epic") or "")
                filled = done
                kind = (e or {}).get("kind") or (
                    "limit" if o.get("type") == "LIMIT" else "stop")
                for i, d2 in enumerate(made):
                    filled += _dec(d2["size"])
                    rest = size0 - filled
                    self._deal_oid[d2["id"]] = oid
                    if cid:
                        self._deal_cid[d2["id"]] = cid
                    self._deal_from[d2["id"]] = kind
                    self._emit_fill(cid, oid, d2, d2["size"], d2["level"],
                                    i == len(made) - 1 and rest <= tol, rest,
                                    d2["opened"])
                    if e is not None:
                        e.setdefault("deals", []).append(d2["id"])
                    crow = self._close_row(d2, rows)
                    if crow is not None:
                        self._tell_close(d2, d2["size"], crow, "c-" + d2["id"],
                                         True)
                    else:
                        # not closed by any row yet: explained when it is, or
                        # withdrawn when the book lists it after all
                        self._gone[d2["id"]] = {"deal": d2, "tries": 0,
                                                "qty": d2["size"], "at": now}
                if e is not None:
                    e["filled"] = _fixed(filled)
                det = mine[0].get("details") if isinstance(
                    mine[0].get("details"), dict) else {}
                if size0 - filled > tol:
                    fill = {"fill_id": "f-wo-" + wo, "client_id": cid,
                            "order_id": oid, "position_id": None,
                            "symbol": o.get("epic"),
                            "side": "buy" if o.get("dir") == "BUY" else "sell",
                            "qty": float(size0 - filled),
                            "price": _num(det.get("level")), "fees": 0.0,
                            "time": parse_utc(mine[0].get("dateUTC"))
                            or self.now_ms()}
                    if self._once("fill:" + fill["fill_id"]):
                        self._fills.append(dict(fill))
                        self.emit("order.filled", fill)
            elif status == "EXPIRED":
                self.emit("order.expired", {"order_id": oid, "client_id": cid,
                                            "expiry": "gtd"})
            elif status == "CANCELLED" and (e or {}).get("cancelling"):
                self.emit("order.cancelled", {"order_id": oid,
                                              "client_id": cid})
            else:
                self.emit("order.cancelled", {
                    "order_id": oid, "client_id": cid,
                    "code": ("x_capitalcom_rejected" if status == "REJECTED"
                             else "x_capitalcom_cancelled"),
                    "message": (
                        "Capital.com rejected the working order when it "
                        "triggered (activity: WORKING_ORDER %s); the usual "
                        "reason is margin" % status if status == "REJECTED"
                        else "the working order was cancelled at Capital.com,"
                        " not by this session" if status == "CANCELLED"
                        else "the working order left Capital.com's list with "
                        "%s of %s filled and no record of why" % (
                            _fixed(done), o.get("size0") or o.get("size")))})

    # == positions =======================================================
    def _position_view(self, d: dict) -> dict:
        spec = self._catalog.get(d["epic"]) or {}
        tick = spec.get("tick_size") or 0
        lv = self._levels.get(d["id"]) or {}
        sl, tp = d["sl"], d["tp"]
        if not d.get("shows"):
            # Capital.com's row names no level: those it CONFIRMED for this
            # deal (an accepted order or amend) are shown, nothing else
            sl, tp = lv.get("sl"), lv.get("tp")
        view = {"position_id": d["id"], "client_id": self._deal_cid.get(d["id"]),
                "symbol": d["epic"],
                "side": "buy" if d["dir"] == "BUY" else "sell",
                "qty": float(_dec(d["size"])), "avg_price": d["level"],
                "sl": sl, "tp": tp,
                "trail_points": (round(d["tdist"] / tick, 6)
                                 if d.get("trailing") and tick
                                 and d.get("tdist") else 0),
                "swap": 0.0, "commission": 0.0,
                "opened_at": d["opened"],
                "from_order": self._deal_from.get(d["id"])}
        # assumed, verify on demo: a deal's upl is in the currency its own
        # `currency` names; shown only when that is the account's
        if d.get("upl") is not None and d.get("ccy") == self.account_currency:
            view["unrealized_pnl"] = d["upl"]
        return view

    def handle_positions_list(self, p):
        with self._lock:
            fresh = time.monotonic() - self._deals_t < CACHE_FRESH
        if not fresh:
            try:
                self._read_book(kind="read")
            except CapitalError as e:
                raise self._err(e) from None
        with self._lock:
            return {"positions": [self._position_view(d)
                                  for d in self._deals.values()]}

    # == what a market is doing now ======================================
    def _market_now(self, epic: str, kind: str = "write") -> dict:
        """GET /markets/{epic}: the live status, prices and dealing rules
        (documented: https://open-api.capital.com/), read afresh for every
        order: a market that shut a moment ago must not be sent an order a
        cached status would let through."""
        return self._call("GET", "/markets/" + urllib.parse.quote(
            epic, safe=""), kind=kind) or {}

    def _conversion(self, src: str, dst: str) -> float | None:
        """About how many `dst` one `src` is, from Capital.com's own FX
        prices, for the margin pre-check only (never for a figure reported
        as money). None when no such market is listed."""
        if not src or not dst or src == dst:
            return 1.0
        for epic, inverse in ((src + dst, False), (dst + src, True)):
            if epic in self._catalog:
                with self._lock:
                    hit = self._fx_cache.get("fx:" + epic)
                if hit and time.monotonic() - hit[0] < 60:
                    mid = hit[1]
                else:
                    try:
                        snap = (self._market_now(epic) or {}).get(
                            "snapshot") or {}
                    except AdapterError:
                        return None
                    b, a = _num(snap.get("bid")), _num(snap.get("offer"))
                    if not b or not a:
                        return None
                    mid = (b + a) / 2.0
                    with self._lock:
                        self._fx_cache["fx:" + epic] = (time.monotonic(), mid)
                return 1.0 / mid if inverse else mid
        return None

    def _free_margin(self) -> float | None:
        with self._lock:
            acct = self._account
            fresh = acct is not None and time.monotonic() - \
                self._account_t < 2.0
        if not fresh:
            try:
                self._read_accounts("write")
            except CapitalError:
                return None
            with self._lock:
                acct = self._account
        if not acct:
            return None
        return _num((acct.get("balance") or {}).get("available"))

    # == order.place =====================================================
    def _check_qty(self, spec: dict, qty) -> str:
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise AdapterError("invalid_qty", "qty must be a number")
        q = float(qty)
        if not (q > 0) or not math.isfinite(q):
            raise AdapterError("invalid_qty", "qty must be above 0")
        step, lo, hi = spec["lot_step"], spec["min_qty"], spec["max_qty"]
        if q < lo * (1 - 1e-9):
            raise AdapterError("invalid_qty", "%s takes at least %s"
                               % (spec["symbol"], _fixed(lo)))
        if q > hi * (1 + 1e-9):
            raise AdapterError("invalid_qty", "%s takes at most %s"
                               % (spec["symbol"], _fixed(hi)))
        if not step_ok(q, step):
            raise AdapterError("invalid_qty", "%s is traded in steps of %s"
                               % (spec["symbol"], _fixed(step)))
        s = _dec(step)
        return _fixed((_dec(q) / s).to_integral_value(
            decimal.ROUND_HALF_EVEN) * s)

    def _level(self, spec: dict, v, what: str, stops: bool):
        """A price the platform gave, checked for shape and on the tick."""
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (
                v > 0) or not math.isfinite(v):
            raise AdapterError("invalid_stops" if stops else
                               "x_capitalcom_bad_price",
                               "%s must be a price above 0" % what)
        if not on_grid(float(v), spec["tick_size"]):
            raise AdapterError(
                "x_capitalcom_off_tick", "%s %s is not on %s's price grid of "
                "%s" % (what, v, spec["symbol"], _fixed(spec["tick_size"])),
                like="invalid_stops" if stops else None)
        return round(float(v), spec["digits"])

    def _order_body(self, spec: dict, side: str, typ: str, qty: str,
                    p: dict) -> tuple:
        """The request body, from what the platform asked: checked for
        everything that depends on the request alone."""
        epic = spec["symbol"]
        direction = "BUY" if side == "buy" else "SELL"
        long = side == "buy"
        body = {"epic": epic, "direction": direction, "size": _wire(qty),
                "guaranteedStop": False}
        sl = self._level(spec, p.get("sl"), "sl", True)
        tp = self._level(spec, p.get("tp"), "tp", True)
        trail = p.get("trail_points")
        level = None
        if typ in ("limit", "stop"):
            level = self._level(spec, p.get("price"), "price", False)
            if level is None:
                raise AdapterError("x_capitalcom_bad_request",
                                   "a %s order needs a price" % typ)
            body["level"] = level
            body["type"] = typ.upper()
            expiry = p.get("expiry") or ("gtd" if p.get("expiry_at")
                                         is not None else "gtc")
            if expiry == "gtd":
                at = p.get("expiry_at")
                if isinstance(at, bool) or not isinstance(at, (int, float)):
                    raise AdapterError("invalid_expiry", "gtd needs "
                                       "expiry_at, in UTC epoch ms")
                if at <= self.now_ms() + 1000:
                    raise AdapterError("invalid_expiry",
                                       "expiry_at is not in the future")
                # documented: goodTillDate YYYY-MM-DDTHH:MM:SS in UTC (to
                # the second: the order is reported with that time)
                body["goodTillDate"] = utc_text(int(at))
            elif expiry != "gtc":
                raise AdapterError("invalid_expiry", "Capital.com's working "
                                   "orders are gtc or gtd, not %r" % expiry)
        if trail not in (None, 0):
            if isinstance(trail, bool) or not isinstance(
                    trail, (int, float)) or not (trail > 0):
                raise AdapterError("invalid_stops", "trail_points must be a "
                                   "number of points above 0")
            if sl is not None:
                raise AdapterError(
                    "x_capitalcom_sl_and_trail", "Capital.com trails a stop "
                    "it sets at a distance; a stop level cannot be given "
                    "beside trail_points", like="invalid_stops")
            self._trailing_allowed(epic)
            body["trailingStop"] = True
            body["stopDistance"] = _wire(round(trail * spec["tick_size"],
                                               spec["digits"]))
        if sl is not None:
            body["stopLevel"] = sl
        if tp is not None:
            body["profitLevel"] = tp
        if sl is not None and tp is not None and (sl >= tp if long
                                                  else sl <= tp):
            raise AdapterError("invalid_stops", "the stop must be on the "
                               "losing side of the target")
        if level is not None:
            if sl is not None and (sl >= level if long else sl <= level):
                raise AdapterError("invalid_stops", "the stop must be beyond "
                                   "the order's price on the losing side")
            if tp is not None and (tp <= level if long else tp >= level):
                raise AdapterError("invalid_stops", "the target must be "
                                   "beyond the order's price on the winning "
                                   "side")
        levels = {"sl": sl, "tp": tp} if (sl is not None
                                          or tp is not None) else None
        return body, levels

    def _trailing_allowed(self, epic: str):
        priv = self._markets.get(epic) or {}
        pref = str(priv.get("trailing") or "").upper()
        if pref and pref != "AVAILABLE":
            # documented field: dealingRules.trailingStopsPreference
            # (NOT_AVAILABLE in the sample)
            raise AdapterError("x_capitalcom_no_trailing", "Capital.com "
                               "offers no trailing stop on %s (%s)"
                               % (epic, pref), like="trade_restricted")
        if self.client.info.get("trailingStopsEnabled") is False:
            raise AdapterError("x_capitalcom_no_trailing", "trailing stops "
                               "are turned off for this Capital.com login",
                               like="trade_restricted")

    def _pre_check(self, spec: dict, typ: str, body: dict, qty: str):
        """What depends on the market and the account now: the market's own
        status, the side of the levels, and the margin (a market order
        only). Capital.com stays the final judge."""
        epic = spec["symbol"]
        det = self._market_now(epic)
        snap = det.get("snapshot") or {}
        inst = det.get("instrument") or {}
        status = str(snap.get("marketStatus") or "").upper()
        modes = [str(m).upper() for m in snap.get("marketModes") or []]
        long = body["direction"] == "BUY"
        if "VIEW_ONLY" in modes:
            raise AdapterError("trade_restricted", "Capital.com shows %s as "
                               "view only" % epic)
        if status and status != "TRADEABLE" and typ == "market":
            # documented: marketStatus TRADEABLE / CLOSED (samples)
            raise AdapterError("market_closed", "Capital.com shows %s as %s "
                               "now" % (epic, status))
        bid, offer = _num(snap.get("bid")), _num(snap.get("offer"))
        if typ == "market" and (not bid or not offer):
            raise AdapterError("no_price", "Capital.com has no price for %s "
                               "right now" % epic, retryable=True)
        close_px = bid if long else offer
        entry_px = offer if long else bid
        sl, tp = body.get("stopLevel"), body.get("profitLevel")
        if typ == "market" and close_px:
            if sl is not None and (sl >= close_px if long else sl <= close_px):
                raise AdapterError("invalid_stops", "the stop %s is not on "
                                   "the losing side of %s's price %s" % (
                                       sl, epic, close_px))
            if tp is not None and (tp <= entry_px if long
                                   else tp >= entry_px):
                raise AdapterError("invalid_stops", "the target %s is not on "
                                   "the winning side of %s's price %s" % (
                                       tp, epic, entry_px))
        if typ in ("limit", "stop") and bid and offer:
            level = body["level"]
            ref = offer if long else bid
            far = level < ref if long else level > ref
            if (typ == "limit") != far:
                # assumed, verify on demo: Capital.com refuses a working
                # order that would trigger at once
                raise AdapterError(
                    "x_capitalcom_invalid_level", "a %s %s at %s would "
                    "trigger at once: %s is at %s" % (
                        "buy" if long else "sell", typ, level, epic, ref),
                    like="invalid_stops")
        if typ != "market":
            return
        # assumed, verify on demo: a deal's margin is size x lotSize x price
        # x marginFactor% (documented: marginFactor and marginFactorUnit
        # PERCENTAGE, https://open-api.capital.com/), in the instrument's
        # currency, here converted at Capital.com's own FX mid for this check
        # only; refused only when plainly over `available`
        rate = _num(inst.get("marginFactor"))
        need = None
        if rate and entry_px:
            lot = _num(inst.get("lotSize"), 1.0) or 1.0
            conv = self._conversion(str(inst.get("currency") or ""),
                                    self.account_currency)
            with self._lock:
                same = sum((_dec(d["size"]) for d in self._deals.values()
                            if d["epic"] == epic and d["dir"]
                            == body["direction"]), D0)
                opp = sum((_dec(d["size"]) for d in self._deals.values()
                           if d["epic"] == epic and d["dir"]
                           != body["direction"]), D0)
            size = _dec(qty)
            if self._hedging:
                # documented: "Margin applies to the larger side of your
                # hedge" (https://capital.com/en-gb/analysis/what-is-hedging-mode-and-how-to-use-it-in-trading)
                grow = max(same + size, opp) - max(same, opp)
            else:
                grow = max(D0, size - opp)
            if conv is not None:
                need = float(grow) * lot * entry_px * rate / 100.0 * conv
        free = self._free_margin() if need else None
        if need is not None and free is not None and need > free + 0.01:
            raise AdapterError(
                "insufficient_margin", "%s %s %s needs about %.2f %s of "
                "margin and %.2f is free" % (body["direction"].lower(), qty,
                                             epic, need, self.account_currency,
                                             free))

    def _rejected(self, cid: str, e: AdapterError):
        """A refusal is also an event, so a platform that only watches
        events sees the order end (SPEC 8.4)."""
        code = e.code if (e.code in protocol.ERROR_CODES
                          or e.code.startswith("x_")) else "x_" + e.code
        self.emit("order.rejected", {"client_id": cid or None, "code": code,
                                     "message": str(e)})

    def handle_order_place(self, p):
        cid = str(p.get("client_id") or "")
        sig = protocol.order_signature("order.place", p)
        with self._lock:
            e = self._journal.get(cid) if cid else None
        if e is not None:
            # A client_id this profile has SENT: never sent again (SPEC 2).
            return self._replayed_place(e, sig)
        try:
            spec = self._instrument(p.get("symbol"))
            side = p.get("side")
            if side not in ("buy", "sell"):
                raise AdapterError("x_capitalcom_bad_request",
                                   "side must be buy or sell")
            typ = p.get("type") or "market"
            if typ == "stop_limit":
                raise AdapterError("unsupported", "Capital.com has no "
                                   "stop-limit order")
            if typ not in ("market", "limit", "stop"):
                raise AdapterError("unsupported", "order type %r is not one "
                                   "of market, limit, stop" % (typ,))
            qty = self._check_qty(spec, p.get("qty"))
            body, levels = self._order_body(spec, side, typ, qty, p)
            self._pre_check(spec, typ, body, qty)
        except AdapterError as err:
            if not err.retryable:
                self._rejected(cid, err)
            raise
        return self._place(spec, typ, qty, body, levels, cid, sig)

    def _place(self, spec, typ, qty, body, levels, cid, sig):
        epic = spec["symbol"]
        path = "/positions" if typ == "market" else "/workingorders"
        with self._lock:
            e = {"cid": cid, "kind": typ, "epic": epic,
                 "dir": body["direction"], "size": qty,
                 "level": body.get("level"), "sig": sig,
                 "at": self.now_ms(), "state": "sending", "ref": None,
                 "order_id": "cc-" + uuid.uuid4().hex[:16], "filled": "0",
                 "before": [d for d, x in self._deals.items()
                            if x["epic"] == epic],
                 "levels": levels, "gtd": body.get("goodTillDate")}
            key = cid or e["order_id"]
            e["key"] = key
            self._journal[key] = e
            self._claims["epic:" + epic] = time.monotonic() + 30
            self._posting += 1
        try:
            # Written down and on the disk BEFORE the request leaves: a
            # crash a moment later still knows an order may be at
            # Capital.com, and never sends this client_id again.
            self._save_state(sync=True)
            try:
                out = self.client.call("POST", path, body=body, kind="write",
                                       max_wait=2.0, order_post=True)
            except CapitalError as err:
                return self._post_failed(e, err)
            finally:
                with self._lock:
                    self._posting -= 1
                    self._post_idle.notify_all()
            ref = str((out or {}).get("dealReference") or "")
            if not ref:
                # An answer that names no deal is no answer (the reference
                # adapter's rule): the order is looked for.
                return self._post_failed(e, CapitalError(
                    0, "its answer named no deal reference", sent=True,
                    cause="it named no deal reference", answered=True))
            with self._lock:
                e["ref"] = ref
                e["order_id"] = ref
                e["state"] = "sent"
                self._unsaved = True
            self._save_state()
            try:
                return self._after_post(e, spec)
            except CapitalError as err:
                # Capital.com HAS the order (it gave a reference); a read
                # after it failed. What it did is told by the events (the
                # poller asks for its confirmation).
                log("order %s: after its reference, %s" % (
                    e["order_id"], self.client.scrub(err.message)))
                with self._lock:
                    self._accepted(e)
                return {"order_id": e["order_id"],
                        "status": "accepted" if typ == "market"
                        else "resting"}
        finally:
            self._unclaim("epic:" + epic)
            self._save_state()
            self._wake.set()

    def _post_failed(self, e: dict, err: CapitalError):
        """What a failed order post means. Only Capital.com's own refusal is
        `order.rejected`; a request that did not leave (or that Capital.com
        turned away unread: a 429, a 401) is as if never sent; one whose
        answer was lost is of UNKNOWN fate and is looked for, never sent
        again."""
        cid = e.get("cid")
        mapped = self._err(err)
        if (not err.sent or err.code in ("budget", "deadline", "flatten")
                or err.status in (401, 403, 429)):
            with self._lock:
                self._journal.pop(e["key"], None)
            if not mapped.retryable:
                self._rejected(cid, mapped)
            raise mapped
        if err.unanswered:
            with self._lock:
                e["state"] = "unknown"
                self._unsaved = True
            self._save_state()
            # what it made must not be held back from the look below
            self._unclaim("epic:" + e["epic"])
            got = self._resolve_now(e)
            if got is not None:
                return got
            self._set_session("order:" + e["key"], "degraded", (
                "an order (%s %s %s, client_id %s) was sent and Capital.com's "
                "answer was lost: it is being looked for at Capital.com and is "
                "not sent again" % (e["dir"].lower(), e["size"], e["epic"],
                                    cid or "-")))
            # The not-known words first (a display cut short still says
            # it), and the name once.
            raise AdapterError(
                "internal", "Not known yet whether Capital.com took the "
                "order: %s. It is NOT sent again: asking again with the same "
                "client_id is safe and answers what was done."
                % self.client.scrub(unanswered(err)), retryable=True)
        # Capital.com's own refusal (a 4xx with its errorCode): final.
        with self._lock:
            self._settle_entry(e, "rejected", error={
                "code": mapped.code, "message": str(mapped),
                "like": mapped.like})
        self._rejected(cid, mapped)
        raise mapped

    def _confirm(self, ref: str, wait: float, kind: str = "write"):
        """GET /confirms/{dealReference} until it says ACCEPTED or REJECTED
        (documented: dealStatus), for at most `wait` seconds. None when it
        has not said yet."""
        end = time.monotonic() + wait
        left = self.client.time_left()
        if left is not None:
            end = min(end, time.monotonic() + left - 0.8)
        # a fresh confirmation takes a moment to appear (assumed, verify on
        # demo: how long), so the first look is not at once
        time.sleep(min(0.12, max(0.0, end - time.monotonic())))
        while True:
            try:
                c = self.client.call("GET", "/confirms/" + urllib.parse.quote(
                    ref, safe=""), kind=kind, max_wait=1.0)
            except CapitalError as e:
                if e.code in ("budget", "deadline", "flatten"):
                    return None
                c = None       # 404 error.not-found.dealReference: not yet
            if isinstance(c, dict) and str(c.get("dealStatus") or "").upper(
                    ) in ("ACCEPTED", "REJECTED"):
                return c
            if time.monotonic() + CONFIRM_POLL >= end:
                return None
            time.sleep(CONFIRM_POLL)

    def _after_post(self, e: dict, spec: dict):
        """The order has a dealReference: its confirmation says what it did."""
        c = self._confirm(e["ref"], CONFIRM_WAIT)
        if c is None:
            # Capital.com has the order (it gave a reference); what it did
            # is told by the events, when the confirmation appears (the
            # poller asks for it).
            with self._lock:
                self._accepted(e)
            return {"order_id": e["order_id"],
                    "status": "accepted" if e["kind"] == "market"
                    else "resting"}
        return self._apply_confirm(e, c, handler=True)

    def _apply_confirm(self, e: dict, c: dict, handler: bool = False,
                       kind: str = "write"):
        """One order's confirmation, applied: REJECTED is the refusal;
        ACCEPTED is read with the book (the deals it opened and reduced, or
        the working order it made)."""
        status = str(c.get("dealStatus") or "").upper()
        if status == "REJECTED":
            code, like = reject_code(c.get("reason"))
            err = AdapterError(code, "Capital.com rejected the order (%s)" % (
                c.get("reason") or "no reason given"), like=like,
                retryable=like == "requote")
            with self._lock:
                if err.retryable:
                    # a requote-like refusal: the platform may send it
                    # again, and that is a new request
                    self._journal.pop(e["key"], None)
                else:
                    self._settle_entry(e, "rejected", error={
                        "code": err.code, "message": str(err),
                        "like": err.like})
            self._rejected(e.get("cid"), err)
            if handler:
                raise err
            return None
        if e["kind"] == "market":
            self._settle_market(e, c, kind)
            return {"order_id": e["order_id"], "status": "accepted"}
        self._settle_working(e, c, kind)
        return {"order_id": e["order_id"], "status": "resting"}

    def _settle_market(self, e: dict, c: dict, kind: str = "write"):
        aff = [(str(a.get("dealId")), str(a.get("status") or "").upper())
               for a in c.get("affectedDeals") or [] if isinstance(a, dict)
               and a.get("dealId")]
        price = _num(c.get("level"))
        executed = _dec(c.get("size")) if c.get("size") is not None else None
        # The confirmation's `date` is not documented as UTC (only fields
        # named ...UTC are): the time of these events is this adapter's
        # clock, corrected by Capital.com's.
        when = self.now_ms()
        with self._lock:
            mine = [did for did, st in aff if st in ("OPENED", "")]
            if mine and len(mine) == len(aff) and all(
                    self._deal_oid.get(did) == e["order_id"]
                    and "fill:f-" + did in self._seen for did in mine):
                # Every deal it opened is told already, as THIS order's
                # (flatten_all closed what it made while its confirmation
                # was awaited): nothing is put back in the book.
                self._settle_entry(e, "filled", {"order_id": e["order_id"],
                                                 "status": "accepted"})
                return
            opposite = any(d["epic"] == e["epic"] and d["dir"] != e["dir"]
                           for d in self._deals.values())
            simple = (len(aff) == 1 and aff[0][1] in ("OPENED", "")
                      and aff[0][0] not in self._deals and price is not None
                      and executed is not None
                      and abs(executed - _dec(e["size"]))
                      <= self._tol(e["epic"])
                      and (self._hedging or not opposite))
            if simple:
                # One deal opened for the whole size, nothing it could have
                # offset: the confirmation says all of it (its level is the
                # price dealt). The poller's next read fills in the rest.
                d = {"id": aff[0][0], "epic": e["epic"], "dir": e["dir"],
                     "size": _fixed(executed), "level": price,
                     "sl": (e.get("levels") or {}).get("sl"),
                     "tp": (e.get("levels") or {}).get("tp"),
                     "trailing": False, "tdist": None, "gsl": False,
                     "upl": None, "ccy": "", "contract": self._lot(e["epic"]),
                     "opened": when, "wo": "", "ref": "", "shows": False,
                     "bid": None, "offer": None, "name": ""}
                self._deals[d["id"]] = d
                self._resolve_filled(e, [d], executed=executed, price=price,
                                     t=when)
                return
        rows = self._book_rows("/positions", "positions", kind, 2.0)
        parsed = {}
        for item in rows:
            d = self._deal_of(item)
            if d:
                parsed[d["id"]] = d
        with self._lock:
            opened, reduced = [], []
            for did, st in aff:
                # documented status word: OPENED (a deal this order made).
                # Any other word for a deal already held is this order
                # reducing it (standard mode; the words are not documented).
                # A deal held but credited to no order (the book read at a
                # restart) is this order's when the confirmation says OPENED.
                new_deal = (did not in self._deals or (
                    st in ("OPENED", "") and did not in self._deal_oid))
                if not new_deal:
                    was = self._deals[did]
                    now = parsed.get(did)
                    cut = _dec(was["size"]) - (_dec(now["size"]) if now
                                               else D0)
                    if cut > 0:
                        reduced.append((dict(was), cut))
                        if now is None:
                            self._deals.pop(did, None)
                        else:
                            self._deals[did] = now
                elif did in parsed:
                    opened.append(parsed[did])
                elif did in self._deals:
                    opened.append(self._deals[did])
                elif st in ("OPENED", ""):
                    # Opened and not listed (yet): its numbers are the
                    # confirmation's. Put in the book, it is told closed
                    # when the next read does not find it.
                    opened.append({
                        "id": did, "epic": e["epic"], "dir": e["dir"],
                        "size": _fixed(executed if executed is not None
                                       else e["size"]),
                        "level": price, "sl": None, "tp": None,
                        "trailing": False, "tdist": None, "gsl": False,
                        "upl": None, "ccy": "",
                        "contract": self._lot(e["epic"]),
                        "opened": when, "wo": "", "ref": "", "shows": False,
                        "bid": None, "offer": None, "name": ""})
            if not aff:
                # assumed, verify on demo: a confirmation that names no deal
                # is read from the book: the new deals of this epic and side
                # (one the poller holds for this very confirmation among them)
                opened = [d for did, d in parsed.items()
                          if (did not in self._deals or did in self._held)
                          and did not in (e.get("before") or ())
                          and d["epic"] == e["epic"] and d["dir"] == e["dir"]
                          and did not in self._deal_oid]
            if self._hedging is False:
                # Standard mode may offset without the confirmation naming the
                # deals it reduced (the words are not documented), so the book
                # says which: but ONLY up to what the confirmation says was
                # dealt beyond the deals it opened and the reductions it
                # named, oldest deal first (assumed, verify on demo), and a
                # deal only when the whole of its change fits. A deal gone for
                # another reason (its stop fired a moment before this order,
                # between two reads) is not this order's: the poller explains
                # it from the activity history (booked as the order's offset,
                # such a stop would have one order of 1000 tell two fills of
                # 1000).
                tol = self._tol(e["epic"])
                room = (executed if executed is not None else _dec(e["size"])) \
                    - sum((_dec(d["size"]) for d in opened), D0) \
                    - sum((q for _, q in reduced), D0)
                named = {w["id"] for w, _ in reduced}
                for did, was in sorted(self._deals.items(),
                                       key=lambda kv: kv[1].get("opened") or 0):
                    if room <= tol:
                        break
                    if (was["epic"] != e["epic"] or was["dir"] == e["dir"]
                            or did in named):
                        continue
                    now = parsed.get(did)
                    cut = _dec(was["size"]) - (_dec(now["size"]) if now
                                               else D0)
                    if cut <= tol or cut > room + tol:
                        continue
                    reduced.append((dict(was), cut))
                    room -= cut
                    if now is None:
                        self._deals.pop(did, None)
                    else:
                        self._deals[did] = now
            for d in opened:
                if d["id"] in parsed or "fill:f-" + d["id"] not in self._seen:
                    self._deals[d["id"]] = d
            self._resolve_filled(e, opened, reduced, executed=executed,
                                 price=price, t=when)
            self._apply_positions(rows, False)

    def _settle_working(self, e: dict, c: dict, kind: str = "write"):
        rows = self._book_rows("/workingorders", "workingOrders", kind, 2.0)
        ids = [str(a.get("dealId")) for a in c.get("affectedDeals") or []
               if isinstance(a, dict) and a.get("dealId")]
        if c.get("dealId"):
            ids.append(str(c["dealId"]))
        with self._lock:
            found = None
            for item in rows:
                o = self._order_of(item)
                if o and o["id"] in ids:
                    found = o
                    break
            if found is None:
                for item in rows:
                    o = self._order_of(item)
                    if (o and o["id"] not in self._worders
                            and self._match_order_entry(o) is e):
                        found = o
                        break
            if found is None:
                # Not listed: it may have triggered at once. Its id is the
                # confirmation's; the book tells the rest.
                wo = ids[0] if ids else ""
                found = {"id": wo, "epic": e["epic"], "dir": e["dir"],
                         "size": e["size"], "level": _num(c.get("level")),
                         "type": e["kind"].upper(), "tif": "", "gtd": None,
                         "created": int(e["at"]), "sl": None,
                         "tp": None, "sd": None, "pd": None,
                         "trailing": False, "gsl": False}
                if wo:
                    self._wo_gone.setdefault(wo, {"order": dict(
                        found, size0=e["size"]), "tries": 0,
                        "at": self.now_ms()})
            found = dict(found, size0=found.get("size0") or e["size"])
            if found["id"] and found["id"] not in self._wo_gone:
                self._worders[found["id"]] = found
            self._resolve_resting(e, found)

    def _resolve_now(self, e: dict):
        """An order whose answer was lost, looked for at once (one read of
        the book) inside the call's own time. Its result, or None."""
        try:
            self._read_book(kind="write", max_wait=1.5)
        except CapitalError:
            return None
        with self._lock:
            if e.get("state") in ("filled", "resting", "done"):
                return dict(e.get("result") or {
                    "order_id": e["order_id"],
                    "status": "accepted" if e["kind"] == "market"
                    else "resting"})
        return None

    def _replayed_place(self, e: dict, sig: str):
        """An order.place whose client_id was SENT before: the original
        outcome, and nothing sent a second time (SPEC 2)."""
        if e.get("sig") and e["sig"] != sig:
            raise AdapterError("duplicate_client_id", "this client_id was "
                               "already used for a different order")
        state = e.get("state")
        if state == "sent" and e.get("ref"):
            c = self._confirm(e["ref"], 1.5)
            if c is not None:
                out = self._apply_confirm(e, c)
                if out is None:
                    state = e.get("state")
                else:
                    return out
            else:
                return {"order_id": e["order_id"],
                        "status": "accepted" if e["kind"] == "market"
                        else "resting"}
        if state in ("sending", "unknown"):
            got = self._resolve_now(e)
            if got is not None:
                return got
            raise AdapterError(
                "internal", "the order sent under this client_id at %s UTC "
                "has not been found at Capital.com yet (its answer was lost). "
                "It is NOT sent again; ask again in a moment." % utc_text(
                    int(e.get("at") or 0)), retryable=True)
        if state in ("filled", "resting", "done"):
            return dict(e.get("result") or {
                "order_id": e["order_id"],
                "status": "accepted" if e["kind"] == "market" else "resting"})
        if state == "rejected":
            err = e.get("error") or {}
            raise AdapterError(str(err.get("code") or "x_capitalcom_rejected"),
                               str(err.get("message") or "Capital.com "
                                   "rejected this order"),
                               like=err.get("like"))
        if e.get("ref"):
            what = ("was taken by Capital.com (deal reference %s) but never "
                    "confirmed, and nothing it made was found in Capital.com's "
                    "positions or working orders while this adapter looked"
                    % e["ref"])
        else:
            what = ("lost its answer, and nothing it made was found in "
                    "Capital.com's positions or working orders in the %d s "
                    "this adapter looked" % UNKNOWN_SEC)
        raise AdapterError(
            "x_capitalcom_not_sent_again", "the order sent under this "
            "client_id at %s UTC %s. It is not sent again under this "
            "client_id. Look at the account on Capital.com's own platform "
            "before sending it again with a new client_id; if it turns up "
            "here it is reported." % (utc_text(int(e.get("at") or 0)), what),
            like="not_found")

    # == resting orders ==================================================
    def _wo_of(self, order_id) -> str:
        oid = str(order_id or "")
        return self._oid_wo.get(oid, oid)

    def _order_view(self, o: dict) -> dict:
        oid = self._wo_oid.get(o["id"], o["id"])
        e = self._entry_for_wo(o["id"])
        size0 = _dec(o.get("size0") or o["size"])
        done = _dec((e or {}).get("filled") or self._notices.get(
            "wo_done:" + o["id"]) or 0)
        return {"order_id": oid, "client_id": (e or {}).get("cid") or None,
                "symbol": o["epic"],
                "type": "limit" if o["type"] == "LIMIT" else "stop",
                "side": "buy" if o["dir"] == "BUY" else "sell",
                "qty": float(size0), "filled_qty": float(done),
                "remaining_qty": float(max(size0 - done, D0)),
                "price": o["level"], "stop_price": None,
                "sl": o.get("sl"), "tp": o.get("tp"),
                "expiry": "gtd" if o.get("gtd") else "gtc",
                "expires_at": o.get("gtd"), "state": "resting",
                "placed_at": o.get("created"), "done_at": None,
                "fill_price": None, "reason": None}

    def handle_orders_list(self, p):
        state = (p or {}).get("state")
        with self._lock:
            fresh = time.monotonic() - self._worders_t < CACHE_FRESH
        if not fresh:
            try:
                self._read_book(kind="read")
            except CapitalError as e:
                raise self._err(e) from None
        with self._lock:
            out = [self._order_view(o) for o in self._worders.values()]
            if state not in (None, "resting"):
                out = [o for o in out if o["state"] == state]
                cutoff = self.now_ms() - HISTORY_DAYS * 86_400_000
                for e in self._journal.values():
                    end = e.get("ended")
                    if (e.get("kind") in ("limit", "stop") and end
                            and end.get("state") == state
                            and int(end.get("at") or 0) >= cutoff):
                        out.append(self._ended_view(e))
            elif state is None:
                cutoff = self.now_ms() - HISTORY_DAYS * 86_400_000
                for e in self._journal.values():
                    end = e.get("ended")
                    if (e.get("kind") in ("limit", "stop") and end
                            and int(end.get("at") or 0) >= cutoff):
                        out.append(self._ended_view(e))
        return {"orders": out}

    def _ended_view(self, e: dict) -> dict:
        end = e.get("ended") or {}
        size, done = _dec(e["size"]), _dec(e.get("filled") or 0)
        return {"order_id": e["order_id"], "client_id": e.get("cid") or None,
                "symbol": e["epic"], "type": e["kind"],
                "side": "buy" if e["dir"] == "BUY" else "sell",
                "qty": float(size), "filled_qty": float(done),
                "remaining_qty": 0.0, "price": _num(e.get("level")),
                "stop_price": None, "sl": (e.get("levels") or {}).get("sl"),
                "tp": (e.get("levels") or {}).get("tp"),
                "expiry": "gtd" if e.get("gtd") else "gtc",
                "expires_at": parse_utc(e.get("gtd")),
                "state": end.get("state"), "placed_at": e.get("at"),
                "done_at": end.get("at"), "fill_price": None,
                "reason": end.get("reason")}

    def handle_order_cancel(self, p):
        oid = str((p or {}).get("order_id") or "")
        wo = self._wo_of(oid)
        if not wo:
            raise AdapterError("not_found", "order_id is missing")
        with self._lock:
            e = self._entry_for_wo(wo)
            if e is not None and e.get("state") == "done":
                end = (e.get("ended") or {}).get("state") or "over"
                raise AdapterError("not_resting", "order %s is %s"
                                   % (oid, end))
            known = wo in self._worders
        self._claim("wo:" + wo)
        try:
            try:
                out = self.client.call("DELETE", "/workingorders/"
                                       + urllib.parse.quote(wo, safe=""),
                                       kind="write")
            except CapitalError as err:
                if err.status == 404:
                    if known or e is not None:
                        # gone meanwhile: filled, expired or cancelled; the
                        # book tells which (its events follow)
                        self._unclaim("wo:" + wo)
                        try:
                            self._read_book(kind="write", max_wait=1.5)
                        except CapitalError:
                            pass
                        raise AdapterError("not_resting", "order %s is no "
                                           "longer working at Capital.com"
                                           % oid) from None
                    raise AdapterError("not_found", "Capital.com has no "
                                       "working order %s" % oid) from None
                raise self._err(err) from None
            ref = str((out or {}).get("dealReference") or "")
            if ref:
                c = self._confirm(ref, 1.5)
                if c is not None and str(c.get("dealStatus")).upper() \
                        == "REJECTED":
                    raise AdapterError("not_resting", "Capital.com did not "
                                       "cancel order %s (%s)" % (
                                           oid, c.get("reason") or "rejected"))
            with self._lock:
                o = self._worders.pop(wo, None)
                self._wo_gone.pop(wo, None)
                if e is not None:
                    e["cancelling"] = True
                    e["ended"] = {"state": "cancelled", "at": self.now_ms(),
                                  "reason": None}
                    self._settle_entry(e, "done")
                if self._once("end:" + (self._wo_oid.get(wo) or wo)):
                    self.emit("order.cancelled", {
                        "order_id": self._wo_oid.get(wo) or wo,
                        "client_id": (e or {}).get("cid") or None})
                if o is None and not known:
                    log("cancelled %s, which this session had not listed"
                        % wo)
        finally:
            self._unclaim("wo:" + wo)
            self._save_state()
        return {}

    def handle_order_amend(self, p):
        oid = str((p or {}).get("order_id") or "")
        wo = self._wo_of(oid)
        with self._lock:
            o = dict(self._worders.get(wo) or {})
            e = self._entry_for_wo(wo)
        if not o:
            try:
                self._read_book(kind="write", max_wait=1.5)
            except CapitalError as err:
                raise self._err(err) from None
            with self._lock:
                o = dict(self._worders.get(wo) or {})
            if not o:
                if e is not None or wo in self._wo_oid:
                    raise AdapterError("not_resting", "order %s is no longer "
                                       "working at Capital.com" % oid)
                raise AdapterError("not_found", "Capital.com has no working "
                                   "order %s" % oid)
        spec = self._instrument(o["epic"])
        long = o["dir"] == "BUY"
        body = {}
        if p.get("qty") is not None:
            if isinstance(p["qty"], bool) or not isinstance(
                    p["qty"], (int, float)) or _dec(p["qty"]) != _dec(
                        o.get("size0") or o["size"]):
                raise AdapterError("unsupported", "Capital.com cannot change "
                                   "a working order's size: cancel it and "
                                   "place a new one")
        level = o["level"]
        if p.get("price") is not None:
            level = self._level(spec, p["price"], "price", False)
            body["level"] = level
        if p.get("stop_price") is not None:
            raise AdapterError("unsupported", "only a stop_limit order has a "
                               "stop_price, and Capital.com has none")
        expiry = p.get("expiry")
        if expiry is None and p.get("expiry_at") is not None:
            expiry = "gtd"
        if expiry == "gtd":
            at = p.get("expiry_at")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                raise AdapterError("invalid_expiry", "gtd needs expiry_at")
            if at <= self.now_ms() + 1000:
                raise AdapterError("invalid_expiry",
                                   "expiry_at is not in the future")
            body["goodTillDate"] = utc_text(int(at))
        elif expiry == "gtc":
            # documented: https://github.com/capital-com-sv/capital-mcp
            # (timeInForce GOOD_TILL_CANCELLED takes an expiry away)
            body["timeInForce"] = "GOOD_TILL_CANCELLED"
        elif expiry is not None:
            raise AdapterError("invalid_expiry", "Capital.com's working "
                               "orders are gtc or gtd")
        for k, what in (("sl", "stopLevel"), ("tp", "profitLevel")):
            if k in p:
                if p[k] is None:
                    raise AdapterError(
                        "unsupported", "Capital.com's API offers no way to "
                        "remove a %s from a working order: cancel it and "
                        "place a new one" % ("stop" if k == "sl" else
                                             "target"))
                body[what] = self._level(spec, p[k], k, True)
        sl = body.get("stopLevel", o.get("sl"))
        tp = body.get("profitLevel", o.get("tp"))
        if level is not None and sl is not None and (sl >= level if long
                                                     else sl <= level):
            raise AdapterError("invalid_stops", "the stop must be beyond the "
                               "order's price on the losing side")
        if level is not None and tp is not None and (tp <= level if long
                                                     else tp >= level):
            raise AdapterError("invalid_stops", "the target must be beyond "
                               "the order's price on the winning side")
        if not body:
            return {}
        # documented: https://github.com/capital-com-sv/capital-mcp (left
        # out, guaranteedStop and trailingStop are reset to false; the level,
        # the stops and the expiry are kept): the flags are carried forward
        body["guaranteedStop"] = bool(o.get("gsl"))
        trailing = bool(o.get("trailing")) and "stopLevel" not in body
        body["trailingStop"] = trailing
        if trailing and o.get("sd") is not None:
            body["stopDistance"] = _wire(abs(o["sd"]))
        if "level" in body:
            # A working order keeps its stop and target as DISTANCES from
            # its level (documented: https://open-api.capital.com/, GET
            # /workingorders shows stopDistance and profitDistance; and
            # https://github.com/capital-com-sv/capital-mcp, legs left out
            # "are preserved by the broker"), so a new level alone would move
            # both. They are kept where the user set them: sent as the levels
            # they are now (PUT /workingorders takes stopLevel and
            # profitLevel, documented), unless this amend changes them. A
            # trailing stop stays a distance.
            if (not trailing and "stopLevel" not in body
                    and o.get("sl") is not None):
                body["stopLevel"] = round(float(o["sl"]), spec["digits"])
            if "profitLevel" not in body and o.get("tp") is not None:
                body["profitLevel"] = round(float(o["tp"]), spec["digits"])
            det = self._market_now(o["epic"])
            snap = det.get("snapshot") or {}
            ref_px = _num(snap.get("offer" if long else "bid"))
            typ = "limit" if o["type"] == "LIMIT" else "stop"
            if ref_px and (typ == "limit") != (level < ref_px if long
                                                else level > ref_px):
                raise AdapterError(
                    "x_capitalcom_invalid_level", "a %s %s at %s would "
                    "trigger at once: %s is at %s" % (
                        "buy" if long else "sell", typ, level, o["epic"],
                        ref_px), like="invalid_stops")
        self._claim("wo:" + wo)
        try:
            out = self._call("PUT", "/workingorders/" + urllib.parse.quote(
                wo, safe=""), body=body, kind="write")
            ref = str((out or {}).get("dealReference") or "")
            if ref:
                c = self._confirm(ref, 2.0)
                if c is not None and str(c.get("dealStatus")).upper() \
                        == "REJECTED":
                    code, like = reject_code(c.get("reason"))
                    raise AdapterError(code, "Capital.com rejected the amend "
                                       "(%s)" % (c.get("reason") or "no "
                                                 "reason given"), like=like)
            try:
                self._read_book(kind="write", max_wait=1.5)
            except CapitalError:
                pass
        finally:
            self._unclaim("wo:" + wo)
        with self._lock:
            n = self._worders.get(wo) or dict(o, level=level, sl=sl, tp=tp)
            if e is not None:
                e["level"] = n.get("level")
                if "stopLevel" in body or "profitLevel" in body:
                    e["levels"] = {"sl": n.get("sl"), "tp": n.get("tp")}
                if "goodTillDate" in body:
                    e["gtd"] = body["goodTillDate"]
                elif "timeInForce" in body:
                    e["gtd"] = None
                self._unsaved = True
            self.emit("order.amended", {
                "order_id": self._wo_oid.get(wo) or wo,
                "price": n.get("level"), "qty": float(_dec(
                    n.get("size0") or n["size"])),
                "sl": n.get("sl"), "tp": n.get("tp"),
                "expires_at": n.get("gtd")})
        self._save_state()
        return {}

    # == stops and targets on a position =================================
    def handle_order_modify(self, p):
        did = str((p or {}).get("position_id") or "")
        with self._lock:
            d = dict(self._deals.get(did) or {})
        if not d:
            try:
                self._read_positions(kind="write")
            except CapitalError as err:
                raise self._err(err) from None
            with self._lock:
                d = dict(self._deals.get(did) or {})
            if not d:
                raise AdapterError("not_found", "Capital.com has no position "
                                   "%s" % did)
        spec = self._instrument(d["epic"])
        long = d["dir"] == "BUY"
        with self._lock:
            lv = dict(self._levels.get(did) or {})
        cur_sl = d["sl"] if d.get("shows") else lv.get("sl")
        cur_tp = d["tp"] if d.get("shows") else lv.get("tp")
        sl = self._level(spec, p["sl"], "sl", True) if "sl" in p and \
            p["sl"] is not None else (None if "sl" in p else cur_sl)
        tp = self._level(spec, p["tp"], "tp", True) if "tp" in p and \
            p["tp"] is not None else (None if "tp" in p else cur_tp)
        trail = p.get("trail_points") if "trail_points" in p else (
            "keep" if d.get("trailing") else None)
        dist = None
        if trail == "keep":
            dist = d.get("tdist")
            if "sl" in p and p["sl"] is not None:
                dist = None           # a stop level replaces the trail
        elif trail not in (None, 0):
            if isinstance(trail, bool) or not isinstance(
                    trail, (int, float)) or not (trail > 0):
                raise AdapterError("invalid_stops", "trail_points must be a "
                                   "number of points above 0")
            if "sl" in p and p["sl"] is not None:
                raise AdapterError(
                    "x_capitalcom_sl_and_trail", "Capital.com trails a stop "
                    "it sets at a distance; a stop level cannot be given "
                    "beside trail_points", like="invalid_stops")
            self._trailing_allowed(d["epic"])
            dist = round(trail * spec["tick_size"], spec["digits"])
        if dist is not None:
            sl = None
        det = self._market_now(d["epic"])
        snap = det.get("snapshot") or {}
        close_px = _num(snap.get("bid" if long else "offer"))
        if close_px:
            if sl is not None and (sl >= close_px if long else sl <= close_px):
                raise AdapterError("invalid_stops", "the stop %s is not on the "
                                   "losing side of %s's price %s" % (
                                       sl, d["epic"], close_px))
            if tp is not None and (tp <= close_px if long else tp >= close_px):
                raise AdapterError("invalid_stops", "the target %s is not on "
                                   "the winning side of %s's price %s" % (
                                       tp, d["epic"], close_px))
        # The body states EVERY leg the position is to have, and names a
        # leg only to set it or to take an existing one away: whether
        # Capital.com keeps a leg that is left out is not documented
        # (assumed, verify on demo), and a stop must never go missing
        # because of a change to the target. A leg taken away is sent as
        # null (assumed, verify on demo; read back below).
        body = {"guaranteedStop": bool(d.get("gsl")) and sl is not None
                and dist is None}
        if dist is not None:
            body["trailingStop"] = True
            body["stopDistance"] = _wire(dist)
        else:
            body["trailingStop"] = False
            if sl is not None or cur_sl is not None or d.get("trailing"):
                body["stopLevel"] = sl
        if tp is not None or cur_tp is not None:
            body["profitLevel"] = tp
        self._claim("deal:" + did)
        try:
            out = self._call("PUT", "/positions/" + urllib.parse.quote(
                did, safe=""), body=body, kind="write")
            ref = str((out or {}).get("dealReference") or "")
            if ref:
                c = self._confirm(ref, 2.0)
                if c is not None and str(c.get("dealStatus")).upper() \
                        == "REJECTED":
                    code, like = reject_code(c.get("reason"))
                    raise AdapterError(code, "Capital.com rejected the change "
                                       "(%s)" % (c.get("reason") or "no "
                                                 "reason given"), like=like)
            with self._lock:
                self._levels[did] = {"sl": sl if dist is None else None,
                                     "tp": tp}
                self._unsaved = True
            try:
                self._read_positions(kind="write", max_wait=1.5)
            except CapitalError:
                pass
        finally:
            self._unclaim("deal:" + did)
        with self._lock:
            now = self._deals.get(did)
        if now is not None and now.get("shows"):
            # Read back: a leg asked to go that Capital.com still shows is
            # said, never reported as gone.
            kept = []
            if dist is None and sl is None and now.get("sl") is not None \
                    and not now.get("trailing"):
                kept.append("stop")
            if tp is None and now.get("tp") is not None:
                kept.append("target")
            if kept:
                with self._lock:
                    self._levels[did] = {"sl": now.get("sl"),
                                         "tp": now.get("tp")}
                self._save_state()
                raise AdapterError(
                    "x_capitalcom_not_cleared", "Capital.com accepted the "
                    "change but still shows the %s on %s; it is in place"
                    % (" and the ".join(kept), did), like="unsupported")
        self._save_state()
        return {}

    # == closing =========================================================
    def _handover(self, d: dict, ref: str, reason: str):
        """A close this adapter sent whose outcome it did not get to tell:
        the poller tells it (from the close's confirmation, or the activity
        history). Caller holds the lock."""
        did = d["id"]
        self._closing[did] = {"ref": ref, "reason": reason,
                              "at": self.now_ms()}
        if "close:c-" + did not in self._seen and did not in self._gone:
            self._gone[did] = {"deal": d, "tries": 0, "qty": d["size"],
                               "at": self.now_ms()}

    def _own_close(self, d: dict, price, reason: str, when=None,
                   detail=None):
        """Caller holds the lock."""
        self._deals.pop(d["id"], None)
        self._gone.pop(d["id"], None)
        self._closing.pop(d["id"], None)
        self._emit_closed(d, d["size"], price, reason, t=when, detail=detail)

    def _ensure_told(self, d: dict):
        """A deal about to be closed by flatten_all that no event has named
        yet (an order still waiting for its confirmation, or one made
        elsewhere): its fill is told first, so a platform never sees the
        close of a position it was not told of. Caller holds the lock."""
        if d["id"] in self._deal_oid:
            return
        # its own working order first: the deal names it (workingOrderId)
        if d.get("wo") and d["wo"] in self._wo_oid:
            self._wo_fill(d)
            return
        e = self._match_deal_entry(d)
        self._held.pop(d["id"], None)
        if e is not None:
            self._resolve_filled(e, [d])
            return
        self._deal_oid[d["id"]] = d.get("wo") or d["id"]
        self._emit_fill(None, d.get("wo") or d["id"], d, d["size"],
                        d["level"], final=True, remaining=None,
                        t=d.get("opened"))

    def handle_position_close(self, p):
        did = str((p or {}).get("position_id") or "")
        cid = str((p or {}).get("client_id") or "")
        if not did:
            raise AdapterError("not_found", "position_id is missing")
        with self._lock:
            d = dict(self._deals.get(did) or {})
            prior = self._closes.get(cid) if cid else None
        if prior and prior.get("deal") != did:
            raise AdapterError("duplicate_client_id", "this client_id was "
                               "already used to close another position")
        if not d:
            try:
                self._read_book(kind="write", max_wait=1.5)
            except CapitalError as err:
                raise self._err(err) from None
            with self._lock:
                d = dict(self._deals.get(did) or {})
            if not d:
                if prior:
                    return {}     # this very close, retried: it happened
                raise AdapterError("not_found", "Capital.com has no position "
                                   "%s" % did)
        qty = (p or {}).get("qty")
        if qty is not None:
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) \
                    or not (qty > 0):
                raise AdapterError("invalid_qty", "qty must be above 0")
            size = _dec(d["size"])
            if _dec(qty) > size + self._tol(d["epic"]):
                raise AdapterError("invalid_qty", "position %s holds %s"
                                   % (did, d["size"]))
            if _dec(qty) < size - self._tol(d["epic"]):
                # documented: DELETE /positions/{dealId} takes no size
                raise AdapterError("unsupported", "Capital.com closes a deal "
                                   "whole: position %s holds %s and a part "
                                   "of it cannot be closed" % (did,
                                                               d["size"]))
        self._claim("deal:" + did)
        with self._lock:
            self._closes[cid or "x-" + did] = {"deal": did,
                                               "at": self.now_ms()}
            self._unsaved = True
        told = False
        refused = False
        ref = ""
        try:
            try:
                out = self.client.call("DELETE", "/positions/"
                                       + urllib.parse.quote(did, safe=""),
                                       kind="write")
            except CapitalError as err:
                if err.status == 404:
                    # gone already: an earlier try of this close, or
                    # Capital.com's own stop or target a moment ago (the
                    # poller explains it)
                    with self._lock:
                        self._closes.pop(cid or "x-" + did, None)
                    if prior:
                        return {}
                    raise AdapterError("not_found", "Capital.com has no "
                                       "position %s now" % did) from None
                if err.unanswered:
                    with self._lock:
                        self._handover(d, "", "manual")
                    raise AdapterError(
                        "internal", "Not known yet whether Capital.com closed "
                        "%s: %s. Asking again with the same client_id is "
                        "safe." % (did, self.client.scrub(unanswered(err))),
                        retryable=True) from None
                with self._lock:
                    self._closes.pop(cid or "x-" + did, None)
                raise self._err(err) from None
            ref = str((out or {}).get("dealReference") or "")
            c = self._confirm(ref, 3.0) if ref else None
            if c is not None and str(c.get("dealStatus")).upper() \
                    == "REJECTED":
                # Refused: the deal is open, and nothing is handed to the
                # poller to tell (handed on, a close refused for a shut market
                # would be told closed seconds later).
                refused = True
                code, like = reject_code(c.get("reason"))
                with self._lock:
                    self._closes.pop(cid or "x-" + did, None)
                raise AdapterError(code, "Capital.com did not close %s (%s)"
                                   % (did, c.get("reason") or "rejected"),
                                   like=like)
            if c is not None:
                with self._lock:
                    self._own_close(d, _num(c.get("level")), "manual",
                                    None)
                    told = True
            else:
                try:
                    self._read_positions(kind="write", max_wait=1.5)
                except CapitalError:
                    pass
                with self._lock:
                    if did not in self._deals:
                        self._handover(d, ref, "manual")
                        told = True
        finally:
            with self._lock:
                if not told and ref and not refused:
                    # Taken (a dealReference) and not confirmed yet: the
                    # poller tells it when its confirmation, or the book, says
                    # what happened; never from the hand-over alone.
                    self._handover(d, ref, "manual")
            self._unclaim("deal:" + did)
            self._save_state()
            self._wake.set()
        return {}

    def handle_flatten_all(self, p):
        """Every working order cancelled and every deal closed, from a read
        made after any order post on the wire has its answer. "Flat" is
        answered only from a read made after the closes, and never while an
        order this adapter sent is of unknown fate (the reference adapter's
        rules). What cannot be closed now is named in the error."""
        t_end = (self.client.read_at() or time.monotonic()) + FLATTEN_HARD - 0.5
        with self._lock:
            end = time.monotonic() + FLATTEN_WAIT_POST
            while self._posting > 0 and time.monotonic() < end:
                self._post_idle.wait(0.05)

        def read():
            orders = self._book_rows("/workingorders", "workingOrders",
                                     "flatten", 2.0)
            positions = self._book_rows("/positions", "positions", "flatten",
                                        2.0)
            os_ = [o for o in map(self._order_of, orders) if o]
            ds = [d for d in map(self._deal_of, positions) if d]
            return os_, ds
        try:
            orders, deals = read()
        except CapitalError as err:
            raise self._err(err) from None
        closed, failed, refs = 0, {}, {}
        extra = False
        for o in orders:
            if time.monotonic() > t_end - 1.0:
                break
            self._claim("wo:" + o["id"])
            try:
                self.client.call("DELETE", "/workingorders/"
                                 + urllib.parse.quote(o["id"], safe=""),
                                 kind="flatten", max_wait=1.0)
                ok = True
            except CapitalError as err:
                ok = False
                if err.status == 404:
                    # Gone already: it filled, expired or was cancelled a
                    # moment ago. That is not a cancel of flatten_all's: the
                    # poller tells what it was (the book, then the activity
                    # history), and the book is read again below so that what
                    # it made is closed too (told cancelled here, its fill
                    # would follow as a second terminal event).
                    extra = True
                else:
                    failed[o["id"]] = "the cancel of working order %s: %s" % (
                        o["id"], _one_line(self.client.scrub(
                            err.code or err.message), 120))
            with self._lock:
                if ok:
                    self._worders.pop(o["id"], None)
                    oid = self._wo_oid.get(o["id"]) or o["id"]
                    e = self._entry_for_wo(o["id"])
                    if e is not None:
                        e["cancelling"] = True
                        self._settle_entry(e, "done", ended="cancelled",
                                           reason="x_capitalcom_flatten")
                    if self._once("end:" + oid):
                        # asked for (flatten_all is the platform's): no code
                        # (SPEC 5, "absent when the platform did")
                        self.emit("order.cancelled", {
                            "order_id": oid,
                            "client_id": (e or {}).get("cid") or None,
                            "message": "cancelled by flatten_all"})
            self._unclaim("wo:" + o["id"])
        # Orders sent before flatten_all and still waiting for their
        # confirmation: their confirmations first, so that what they made is
        # credited to them by name (never to an order of unknown fate, see
        # _claimant) before it is closed; and what they made is closed too.
        with self._lock:
            waiting = [e for e in self._open_entries(("market",))
                       if e.get("state") == "sent" and e.get("ref")]
        for e in waiting:
            if time.monotonic() > t_end - 2.5:
                break
            c = self._confirm(e["ref"], 0.4, kind="flatten")
            if c is None:
                continue
            try:
                self._apply_confirm(e, c, kind="flatten")
            except (CapitalError, AdapterError) as err:
                log("flatten_all: the confirmation of %s was not applied: %s"
                    % (e["order_id"], self.client.scrub(str(err))))
            if str(c.get("dealStatus")).upper() == "ACCEPTED":
                extra = True

        def close(batch):
            nonlocal closed
            for d in batch:
                if time.monotonic() > t_end - 0.8:
                    failed.setdefault(d["id"], "no time was left to close %s"
                                      % d["id"])
                    continue
                with self._lock:
                    self._ensure_told(d)
                    if d["id"] not in self._deals:
                        self._deals[d["id"]] = d
                self._claim("deal:" + d["id"])
                try:
                    out = self.client.call(
                        "DELETE", "/positions/" + urllib.parse.quote(
                            d["id"], safe=""), kind="flatten", max_wait=1.0)
                    refs[d["id"]] = (d, str((out or {}).get("dealReference")
                                            or ""))
                except CapitalError as err:
                    if err.status == 404:
                        continue      # gone already: not this call's close
                    with self._lock:
                        if err.unanswered:
                            self._handover(d, "", "flatten")
                    failed[d["id"]] = "the close of %s (%s %s %s): %s" % (
                        d["id"], d["dir"].lower(), d["size"], d["epic"],
                        _one_line(self.client.scrub(err.code or err.message),
                                  120))
                    self._unclaim("deal:" + d["id"])
        close(deals)
        if extra or refs:
            try:
                _, after = read()
            except CapitalError:
                after = None
            if after is not None:
                more = [d for d in after if d["id"] not in refs
                        and d["id"] not in failed]
                if more:
                    close(more)
        # The closes' outcomes: their confirmations (the price), then a read
        # that says whether anything is left.
        for did, (d, ref) in list(refs.items()):
            c = None
            if ref and time.monotonic() < t_end - 1.0:
                c = self._confirm(ref, 0.5, kind="flatten")
            if c is not None and str(c.get("dealStatus")).upper() \
                    == "REJECTED":
                failed[did] = "the close of %s (%s %s %s) was rejected (%s)" \
                    % (did, d["dir"].lower(), d["size"], d["epic"],
                       _one_line(self.client.scrub(
                           c.get("reason") or "no reason given"), 80))
                refs.pop(did)
                self._unclaim("deal:" + did)
                continue
            with self._lock:
                if c is not None:
                    self._own_close(d, _num(c.get("level")), "flatten",
                                    None)
                    closed += 1
                else:
                    self._handover(d, ref, "flatten")
        left = None
        if time.monotonic() < t_end:
            try:
                left_orders, left = read()
            except CapitalError:
                left = None
        with self._lock:
            if left is not None:
                still = {d["id"] for d in left}
                for did, (d, ref) in refs.items():
                    if did in still:
                        failed.setdefault(did, "%s (%s %s %s) is still open "
                                          "at Capital.com" % (
                                              did, d["dir"].lower(),
                                              d["size"], d["epic"]))
                    elif did in self._closing:
                        closed += 1      # its event follows from the poller
                for d in left:
                    if d["id"] not in refs:
                        failed.setdefault(d["id"], "%s (%s %s %s) is open at "
                                          "Capital.com" % (
                                              d["id"], d["dir"].lower(),
                                              d["size"], d["epic"]))
                for o in left_orders:
                    failed.setdefault("wo:" + o["id"], "working order %s (%s "
                                      "%s %s at %s) is still working" % (
                                          o["id"], o["dir"].lower(),
                                          o["size"], o["epic"], o["level"]))
                self._deals = {d["id"]: self._deals.get(d["id"], d)
                               for d in left}
                self._deals_t = time.monotonic()
            unknown = [e for e in self._open_entries(("market", "limit",
                                                      "stop"))
                       if e.get("state") in ("sending", "unknown")]
        for did in refs:
            self._unclaim("deal:" + did)
        self._save_state()
        self._wake.set()
        if left is None:
            raise AdapterError(
                "internal", "flatten_all closed %d position(s) and could not "
                "read the account again to say it is flat; ask again"
                % closed, retryable=True)
        if failed:
            shut = any("CLOSED" in t.upper() or "market_closed" in t
                       for t in failed.values())
            raise AdapterError(
                "x_capitalcom_not_flat", "flatten_all closed %d position(s); "
                "NOT flat: %s. Asking again closes what is left." % (
                    closed, "; ".join(sorted(failed.values()))),
                retryable=not shut,
                like="market_closed" if shut else "frozen")
        if unknown:
            raise AdapterError(
                "x_capitalcom_order_unknown", "flatten_all closed %d "
                "position(s), but %d order(s) sent earlier lost their answer "
                "and may still reach Capital.com: %s. Ask again in a moment."
                % (closed, len(unknown), ", ".join(
                    "%s %s %s (client_id %s)" % (e["dir"].lower(), e["size"],
                                                 e["epic"], e.get("cid") or "-")
                    for e in unknown)), retryable=True, like="frozen")
        return {"closed": closed}

    # == the ledgers =====================================================
    def handle_history_fills(self, p):
        """This profile's own ledger (kept in BRUE_BROKER_STATE): every
        execution this adapter reported, the closes included; and the
        executions of deals made on Capital.com's own platform, which this
        ledger never saw, read from GET /history/activity (_platform_fills)
        and merged in by dealId -- a deal the ledger already holds stands as
        the ledger has it. Oldest first. Bounded: seven days back unless
        `from` says otherwise, the newest 500 at most."""
        p = p or {}
        now = self.now_ms()
        lo, hi = p.get("from"), p.get("to")
        lo = int(lo) if isinstance(lo, (int, float)) and not isinstance(
            lo, bool) else now - HISTORY_DAYS * 86_400_000
        hi = int(hi) if isinstance(hi, (int, float)) and not isinstance(
            hi, bool) else now + 60_000
        with self._lock:
            rows = [dict(f) for f in self._fills
                    if lo <= int(f.get("time") or 0) <= hi]
            known = {str(f.get("position_id")) for f in self._fills
                     if f.get("position_id")}
        try:
            rows += self._platform_fills(lo, hi, known)
        except CapitalError:
            # The platform history is an addition, not the record itself: a
            # read of it that fails leaves the ledger's own fills to answer.
            pass
        rows.sort(key=lambda f: int(f.get("time") or 0))
        return {"fills": rows[-HISTORY_MAX_FILLS:]}

    def _history_activity(self, lo: int, hi: int, kind: str = "read") -> list:
        """GET /history/activity (detailed) over [lo, hi], a day a request
        (the activity history allows `from` and `to` at most one day apart,
        documented). `from` and `to` are to the second, so `to` is this
        chunk's edge second and the next chunk's `from` is the same second:
        the overlap is one duplicate, dropped by its fill_id. The newest
        chunk names no `to` (one floored to the second would leave out what
        happened in the last part of it; a `from` alone gives a one-day
        range to now)."""
        rows, t, calls = [], hi, 0
        now = self.now_ms()
        while t > lo and calls < 9:
            a = max(lo, t - 86_400_000)
            q = {"from": utc_text(a), "detailed": "true"}
            if t < now - 1000:
                # to - from is exactly a day in whole seconds (both floored):
                # within the documented one-day limit.
                q["to"] = utc_text(t)
            out = self.client.call("GET", "/history/activity", q,
                                   kind=kind) or {}
            rows.extend(out.get("activities") or [])
            t, calls = a, calls + 1
        return rows

    def _platform_fills(self, lo: int, hi: int, known: set) -> list:
        """The fills of deals made on Capital.com's platform, from the
        activity history: every POSITION execution (an open or a close) of a
        dealId this adapter's ledger does not hold (`known`). documented
        fields dealId, epic, dateUTC, type and status; and, with
        detailed=true, details.size, .direction and .level
        (https://open-api.capital.com/ and its postman collection; a close is
        a POSITION row carrying the closing direction and level, see
        _close_row). An execution read twice (the day-chunking shares an edge
        second) is the same fill_id, so it is kept once. No realized P/L,
        swap or commission: the activity history carries none, and a fill
        row holds none (a close's P/L is a position.closed, not a fill)."""
        out, seen = [], set()
        for r in self._history_activity(lo, hi):
            if not isinstance(r, dict):
                continue
            did = str(r.get("dealId") or "")
            if (str(r.get("type") or "").upper() != "POSITION"
                    or str(r.get("status") or "").upper() not in DONE_STATUS
                    or not did or did in known):
                continue
            det = r.get("details") if isinstance(r.get("details"), dict) \
                else {}
            when = parse_utc(r.get("dateUTC"))
            size = _dec(det.get("size"))
            way = str(det.get("direction") or "").upper()
            if (when is None or not (lo <= when <= hi) or size <= 0
                    or way not in ("BUY", "SELL")):
                continue
            price = _num(det.get("level"))
            key = "%s|%s|%d|%s|%s" % (did, way, when, _fixed(size),
                                      _fixed(_dec(price) if price is not None
                                             else D0))
            fid = "h-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
            if fid in seen:
                continue
            seen.add(fid)
            out.append({"fill_id": fid, "client_id": None, "order_id": None,
                        "position_id": did, "symbol": str(r.get("epic") or ""),
                        "side": "buy" if way == "BUY" else "sell",
                        "qty": float(size), "price": price, "fees": 0.0,
                        "time": when})
        return out

    # documented: https://open-api.capital.com/ (GET /history/transactions,
    # `type` values); the SPEC's kinds for them are this adapter's reading
    OP_KIND = {"DEPOSIT": "deposit", "WITHDRAWAL": "withdrawal",
               "BONUS": "credit", "CREDIT_FACILITY": "credit",
               "WRITE_OFF_OR_CREDIT": "credit", "CORPORATE_ACTION": "dividend",
               "ADJUSTMENT": "adjustment", "TRADE_CORRECTION": "adjustment",
               "COMPLAINT_SETTLEMENT": "adjustment", "REFUND": "adjustment",
               "NEGATIVE_BALANCE_PROTECTION": "adjustment",
               "TRADE_SLIPPAGE_PROTECTION": "adjustment",
               "REBATE": "adjustment", "CHARGEBACK": "adjustment",
               "SWAP": "fee", "INACTIVITY_FEE": "fee", "FX_COMMISSION": "fee",
               "TRADE_COMMISSION": "fee", "TRADE_COMMISSION_GSL": "fee"}

    def _operation(self, r: dict) -> dict | None:
        """One transaction as an Operation; None for a deal's own result
        (TRADE "Trade closed": that is the position's close)."""
        if not isinstance(r, dict):
            return None
        typ = str(r.get("transactionType") or "").upper()
        if typ == "TRADE":
            return None
        amount = _num(r.get("size"))       # documented: `size`, a string
        when = parse_utc(r.get("dateUtc") or r.get("dateUTC"))
        if amount is None or when is None:
            return None
        ref = str(r.get("reference") or "")
        kind = self.OP_KIND.get(typ, "other")
        name = str(r.get("instrumentName") or "")
        note = str(r.get("note") or "")
        if typ == "SWAP":
            comment = "overnight funding%s" % (" on " + name if name else "")
        elif kind == "other":
            comment = "Capital.com transaction %s%s" % (typ or "?", (
                ": " + note) if note else "")
        else:
            comment = note or typ.lower()
        # The id is the transaction's own fields together, never `reference`
        # alone: nothing documents a reference as unique to one transaction
        # (the sample: "12345678"), and if two share one (each night's swap
        # on one position, a fee beside its trade) the second must not
        # vanish. A transaction read again has the same reference, type, time
        # and amount, so the same id.
        key = "%s|%s|%d|%s" % (ref, typ, when, _fixed(amount))
        return {"operation_id": "tx-%s%s" % (
                    ref + "-" if ref else "",
                    hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]),
                "kind": kind, "amount": amount,
                "currency": str(r.get("currency") or self.account_currency),
                "reference": (name if kind == "dividend" and name else ref)
                or None,
                "comment": comment, "time": when}

    def _transactions(self, lo: int, hi: int, kind: str = "read") -> list:
        """GET /history/transactions over [lo, hi], a day a request (the
        activity history allows one day; the same is kept to here). The
        times are to the second, so neighbouring requests share their edge
        second (duplicates are dropped by the caller), and the newest one
        names no `to`: one floored to the second would leave out what
        happened in the last part of it (seen against the stand-in)."""
        rows, t, calls = [], hi, 0
        now = self.now_ms()
        while t > lo and calls < 8:
            a = max(lo, t - 86_400_000)
            q = {"from": utc_text(a)}
            if t < now - 1000:
                q["to"] = utc_text(t + 999)
            out = self.client.call("GET", "/history/transactions", q,
                                   kind=kind) or {}
            rows.extend(out.get("transactions") or [])
            t, calls = a, calls + 1
        return rows

    def handle_history_operations(self, p):
        p = p or {}
        now = self.now_ms()
        lo, hi = p.get("from"), p.get("to")
        lo = int(lo) if isinstance(lo, (int, float)) and not isinstance(
            lo, bool) else now - HISTORY_DAYS * 86_400_000
        hi = int(hi) if isinstance(hi, (int, float)) and not isinstance(
            hi, bool) else now
        try:
            rows = self._transactions(lo, hi)
        except CapitalError as e:
            raise self._err(e) from None
        ops, seen = [], set()
        for r in rows:
            op = self._operation(r)
            if op and op["operation_id"] not in seen and lo <= op["time"] <= hi:
                seen.add(op["operation_id"])
                ops.append(op)
        ops.sort(key=lambda o: o["time"])
        limit = p.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and \
                limit > 0:
            ops = ops[-limit:]
        return {"operations": ops}

    # == quotes ==========================================================
    def handle_quotes_subscribe(self, p):
        symbols = list(dict.fromkeys(
            str(s) for s in (p or {}).get("symbols") or []))
        for s in symbols:
            self._instrument(s)
        with self._lock:
            for s in symbols:
                if s in self._quote_live:
                    continue
                if len(self._quote_live) >= MAX_QUOTE_SYMBOLS:
                    # documented: https://open-api.capital.com/ ("The
                    # WebSocket API allows subscription to a maximum of 40
                    # instruments"): what fits is taken, the rest is SAID
                    if s not in self._quote_refused:
                        self._quote_refused.append(s)
                    continue
                self._quote_live.add(s)
                if s in self._quote_refused:
                    self._quote_refused.remove(s)
            live = [s for s in symbols if s in self._quote_live]
            refused = list(self._quote_refused)
        self._quote_cap(refused)
        self._ensure_stream()
        self._stream_wake.set()
        # The latest prices at once, also for a symbol already streaming: a
        # platform that asks again is pricing something now (and a quiet or
        # shut market may send nothing on the stream for a while).
        if live:
            try:
                self._rest_quotes(live, "quotes", force=True)
            except CapitalError as e:
                log("prices for %s not read at once: %s" % (
                    ",".join(live[:5]), e.message))
        return {}

    def handle_quotes_unsubscribe(self, p):
        with self._lock:
            for s in (p or {}).get("symbols") or []:
                self._quote_live.discard(str(s))
                if str(s) in self._quote_refused:
                    self._quote_refused.remove(str(s))
                self._quote_sent.pop(str(s), None)
            refused = list(self._quote_refused)
        self._quote_cap(refused)
        self._stream_wake.set()
        return {}

    def _quote_cap(self, refused: list):
        if refused:
            self._set_session(
                "quote_cap", "degraded", "Capital.com streams at most %d "
                "instruments at once; %d asked for over that get no quotes: "
                "%s" % (MAX_QUOTE_SYMBOLS, len(refused), ", ".join(
                    refused[:8]) + ("..." if len(refused) > 8 else "")))
        elif "quote_cap" in self._components:
            self._set_session("quote_cap", "connected", "")

    def _emit_quote(self, sym: str, bid, ask, t):
        b, a = _num(bid), _num(ask)
        if not b or not a:
            return
        with self._lock:
            if sym not in self._quote_live:
                return
            if self._quote_sent.get(sym) == (b, a, t):
                return
            self._quote_sent[sym] = (b, a, t)
        self.emit("quote", {"symbol": sym, "bid": b, "ask": a,
                            "time": int(t or self.now_ms())})

    def _rest_quotes(self, symbols: list, kind: str, force: bool = False):
        """Prices over REST: GET /markets?epics= (documented, 50 at most a
        request)."""
        for i in range(0, len(symbols), DETAILS_PER_CALL):
            batch = symbols[i:i + DETAILS_PER_CALL]
            out = self.client.call("GET", "/markets", {
                "epics": ",".join(batch)}, kind=kind, max_wait=2.0) or {}
            for det in out.get("marketDetails") or []:
                inst = (det or {}).get("instrument") or {}
                snap = (det or {}).get("snapshot") or {}
                sym = str(inst.get("epic") or "")
                t = parse_utc(snap.get("updateTimeUTC")) or self.now_ms()
                if force:
                    with self._lock:
                        self._quote_sent.pop(sym, None)
                self._emit_quote(sym, snap.get("bid"), snap.get("offer"), t)
        self._last_rest_quotes = time.monotonic()

    def _ensure_stream(self):
        with self._lock:
            if self._stream_thread is not None and \
                    self._stream_thread.is_alive():
                return
            self._stream_thread = threading.Thread(
                target=self._stream_loop, daemon=True,
                name="capitalcom-stream")
            self._stream_thread.start()

    def _stream_loop(self):
        """The price stream (documented: https://open-api.capital.com/:
        wss://api-streaming-capital.backend-capital.com/connect; every
        message carries destination, correlationId, cst, securityToken and
        payload; marketData.subscribe {"epics"}; a "quote" message {epic,
        product, bid, bidQty, ofr, ofrQty, timestamp}; a ping at least every
        10 minutes). It is opened while anything is subscribed, on the
        session's tokens, and opened again on a new session (a switch of
        account ends it). Survives anything, and says so."""
        backoff = 1.0
        corr = 0
        while not self._stop.is_set():
            with self._lock:
                want = set(self._quote_live)
            if not want:
                self._stream_up.clear()
                self._stream_wake.wait(5.0)
                self._stream_wake.clear()
                continue
            try:
                tok = self.client.session("background", 5.0)
            except CapitalError as e:
                self._set_session("stream", "degraded", "the price stream "
                                  "waits for a session (%s)" % e.message)
                self._stop.wait(backoff)
                backoff = min(STREAM_BACKOFF_MAX, backoff * 2)
                continue
            try:
                ws = _WebSocket(self.cfg["stream_url"], timeout=5.0)
            except (OSError, WSClosed) as e:
                self._set_session("stream", "degraded", "the price stream is "
                                  "not connected (%s); prices are read every "
                                  "%d s meanwhile" % (
                                      self.client.scrub(str(e))[:80],
                                      QUOTE_FALLBACK_SEC))
                self._stop.wait(backoff)
                backoff = min(STREAM_BACKOFF_MAX, backoff * 2)
                continue
            self._stream_ws = ws
            gen, subscribed = tok[2], set()
            last_ping = time.monotonic()
            why = "closed"
            try:
                self._stream_wake.clear()
                while not self._stop.is_set():
                    t = self.client.tokens()
                    if not t or t[2] != gen:
                        why = "a new session"
                        break
                    with self._lock:
                        want = set(self._quote_live)
                    if not want:
                        why = "nothing is subscribed"
                        break
                    add, drop = want - subscribed, subscribed - want
                    for dest, epics in (("marketData.unsubscribe", drop),
                                        ("marketData.subscribe", add)):
                        if epics:
                            corr += 1
                            ws.send_json({"destination": dest,
                                          "correlationId": str(corr),
                                          "cst": t[0], "securityToken": t[1],
                                          "payload": {"epics": sorted(epics)}})
                    subscribed = (subscribed - drop) | add
                    if time.monotonic() - last_ping >= STREAM_PING_SEC:
                        corr += 1
                        ws.send_json({"destination": "ping",
                                      "correlationId": str(corr),
                                      "cst": t[0], "securityToken": t[1]})
                        last_ping = time.monotonic()
                    if self._stream_wake.is_set():
                        self._stream_wake.clear()
                        continue
                    try:
                        text = ws.recv(timeout=0.5)
                    except WSTimeout:
                        continue
                    if text is None:
                        why = "Capital.com closed it (%s)" % ws.close_code
                        break
                    if not self._stream_message(text, gen):
                        why = "the session's tokens were refused"
                        break
                    backoff = 1.0
            except (OSError, WSClosed) as e:
                why = self.client.scrub(str(e))[:80] or type(e).__name__
            except Exception as e:  # noqa: BLE001 - the stream must survive
                why = "%s: %s" % (type(e).__name__,
                                  self.client.scrub(str(e))[:80])
                log("price stream: " + why)
            finally:
                ws.close()
                self._stream_ws = None
                self._stream_up.clear()
            if self._stop.is_set():
                break
            with self._lock:
                still = bool(self._quote_live)
            if still and why not in ("a new session",):
                self._set_session("stream", "degraded", "the price stream "
                                  "dropped (%s); reconnecting; prices are "
                                  "read every %d s meanwhile" % (
                                      why, QUOTE_FALLBACK_SEC))
                self._stop.wait(backoff)
                backoff = min(STREAM_BACKOFF_MAX, backoff * 2)

    def _stream_message(self, text: str, gen: int) -> bool:
        """One message of the stream. False when the session it carries is
        refused (the stream then opens again on a new one)."""
        try:
            msg = json.loads(text)
        except ValueError:
            return True
        if not isinstance(msg, dict):
            return True
        dest = str(msg.get("destination") or "")
        status = str(msg.get("status") or "").upper()
        payload = msg.get("payload") if isinstance(msg.get("payload"),
                                                   dict) else {}
        if dest == "quote":
            self._emit_quote(str(payload.get("epic") or ""),
                             payload.get("bid"), payload.get("ofr"),
                             _num(payload.get("timestamp")))
            return True
        if status == "ERROR":
            code = str(payload.get("errorCode") or "")
            if "session" in code or "token" in code:
                self.client.lapsed(gen)
                return False
            if dest.startswith("marketData") and "size" in code:
                # assumed, verify on demo: the cap counts every stream of the
                # login, so another tool of the user's may hold some of it
                self._set_session("stream", "degraded", "Capital.com refused "
                                  "the subscription (%s): another tool on "
                                  "this login may hold some of its 40 "
                                  "instruments" % code)
                return True
            log("price stream: %s %s" % (dest, code))
            return True
        if dest == "marketData.subscribe" and status == "OK":
            subs = payload.get("subscriptions") or {}
            bad = sorted(k for k, v in subs.items() if str(v).upper()
                         != "PROCESSED") if isinstance(subs, dict) else []
            self._stream_up.set()
            self._set_session("stream", "connected", "") if not bad else \
                self._set_session("stream", "degraded", "Capital.com streams "
                                  "no prices for %s" % ", ".join(bad[:8]))
        return True

    # == bars ============================================================
    def _bar_spec(self, p):
        spec = self._instrument((p or {}).get("symbol"))
        tf = (p or {}).get("timeframe")
        if tf not in TIMEFRAMES:
            raise AdapterError("unsupported", "timeframe %r is not one of %s"
                               % (tf, " ".join(TIMEFRAMES)))
        return spec, tf

    def _bar_store(self, epic: str, tf: str) -> _BarStore:
        key, mono = (epic, tf), time.monotonic()
        with self._bars_lock:
            for k in [k for k, v in self._bars.items()
                      if mono - v.used > BARS_IDLE
                      or mono - v.made > BARS_LIFE]:
                self._bars.pop(k).dead = True
            store = self._bars.get(key)
            if store is None:
                store = self._bars[key] = _BarStore(TIMEFRAMES[tf][1],
                                                    self.now_ms())
                while len(self._bars) > BARS_STORES:
                    self._bars.popitem(last=False)[1].dead = True
            else:
                self._bars.move_to_end(key)
            store.used = mono
            return store

    def _bar_fetch(self, epic: str, tf: str, a: int, b: int, kind: str,
                   max_wait: float) -> tuple:
        """The bars with starts in [a, b] (asked with a bar of slack at each
        end). (rows, full): `full` when Capital.com gave as many as it gives
        at most, or refused a wide range, so the window must be split."""
        res, width = TIMEFRAMES[tf]
        q = {"resolution": res, "max": PRICES_MAX,
             "from": utc_text(a - width), "to": utc_text(b + width)}
        try:
            raw = self.client.call("GET", "/prices/" + urllib.parse.quote(
                epic, safe=""), q, kind=kind, max_wait=max_wait,
                timeout=10.0) or {}
        except CapitalError as e:
            code = str(e.code or "").lower()
            if e.status == 404 and "epic" not in code:
                # assumed, verify on demo: a range with no bars is refused
                # (404) rather than answered empty; either reads as empty
                return [], False
            if e.status == 400 and (b - a) // width + 1 > BARS_SPAN + 2:
                return [], True
            raise
        prices = raw.get("prices") or []
        rows = [r for r in map(bar_row, prices) if r]
        return rows, len(prices) >= PRICES_MAX

    def _bar_head(self, store: _BarStore, epic: str, tf: str):
        """The newest end, read again on a thread of its own."""
        width = TIMEFRAMES[tf][1]
        err = None
        for _ in (0, 1):
            now = self.now_ms()
            top = now // width * width
            with store.cond:
                if store.head_at and store.rows:
                    a = max(max(store.rows) - width,
                            top - (BARS_SPAN - 1) * width)
                else:
                    a = top - (BARS_SPAN - 1) * width
            try:
                rows, full = self._bar_fetch(epic, tf, a, top, "head", 5.0)
            except CapitalError as e:
                err = e
                continue
            except Exception as e:  # noqa: BLE001 - a thread must not die
                err = CapitalError(0, "%s: %s" % (type(e).__name__, e))
                continue
            with store.cond:
                store.top = max(store.top, top)
                store.put((a, top), rows, False, self.now_ms())
                store.head_at, err = time.monotonic(), None
            break
        with store.cond:
            store.head_busy, store.head_error = False, err
            store.cond.notify_all()

    def _bar_worker(self, store: _BarStore, epic: str, tf: str):
        while True:
            with store.cond:
                fast = time.monotonic() < store.fast_until
                w = None
                if (not store.dead and store.have() < store.want
                        and (fast or (store.extra > 0
                                      and not store.quiet_busy))):
                    w = store.take()
                if w is None:
                    store.workers -= 1
                    store.cond.notify_all()
                    return
                if not fast:
                    store.extra -= 1
                    store.quiet_busy = True
            rows, full, err = None, False, None
            quiet = not fast and self._bars_quiet.acquire(timeout=60.0)
            try:
                if fast:
                    rows, full = self._bar_fetch(
                        epic, tf, w[0], w[1], "bulk",
                        max(0.2, store.fast_until - time.monotonic()))
                elif quiet:
                    rows, full = self._bar_fetch(epic, tf, w[0], w[1],
                                                 "idle", 20.0)
                else:
                    err = CapitalError(429, "other charts are being read",
                                       code="budget", sent=False)
            except CapitalError as e:
                err = e
            except Exception as e:  # noqa: BLE001 - a thread must not die
                err = CapitalError(0, "%s: %s" % (type(e).__name__, e))
            finally:
                if quiet:
                    self._bars_quiet.release()
            with store.cond:
                if not fast:
                    store.quiet_busy = False
                if err is None:
                    store.fails = 0
                    store.put(w, rows or [], full, self.now_ms())
                else:
                    store.inflight.discard(w)
                    store.pending.append(w)
                    if err.code == "budget":
                        store.fails = 3
                    else:
                        store.error = err
                        store.fails += 1
                    if store.fails >= 3:
                        store.workers -= 1
                        store.cond.notify_all()
                        return
                store.cond.notify_all()

    def handle_bars_history(self, p):
        """The newest `count` closed bars, oldest first, at the bid.

        Answers as soon as it has them; failing that, with what is in hand
        BARS_FIRST seconds after the request was READ (the newest end and
        the windows that have arrived), and never later than BARS_BUDGET.
        The reading goes on behind the answer, so asking again returns more
        (the reference adapter's rule). It is an error only when there is
        not one bar to give and more may come."""
        spec, tf = self._bar_spec(p)
        count = (p or {}).get("count")
        if (isinstance(count, bool) or not isinstance(count, int)
                or not 1 <= count <= BARS_MAX_COUNT):
            raise AdapterError("unsupported", "count must be a whole number "
                               "from 1 to %d" % BARS_MAX_COUNT)
        epic, t0 = spec["symbol"], time.monotonic()
        read = self.client.read_at() or t0
        deadline = read + BARS_BUDGET
        first = min(deadline, read + BARS_FIRST)
        store = self._bar_store(epic, tf)
        me = object()
        try:
            with store.cond:
                store.asking += 1
                store.waiters[me] = count
                store.want = max(store.waiters.values())
                store.error, store.fails = None, 0
                stale = t0 - store.head_at > BARS_HEAD_TTL
                if stale and not store.head_busy:
                    store.head_busy, store.head_error = True, None
                    threading.Thread(target=self._bar_head, daemon=True,
                                     args=(store, epic, tf),
                                     name="capitalcom-bars-head").start()
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if store.head_at and not (stale and store.head_busy):
                        if store.have() >= count or store.finished():
                            break
                        if store.workers < BARS_WORKERS:
                            store.fast_until = max(store.fast_until,
                                                   deadline)
                            store.extra = BARS_FILL_EXTRA
                            for _ in range(BARS_WORKERS - store.workers):
                                store.workers += 1
                                threading.Thread(
                                    target=self._bar_worker, daemon=True,
                                    args=(store, epic, tf),
                                    name="capitalcom-bars").start()
                        if now >= first:
                            break
                    elif store.head_error and not store.head_busy:
                        break
                    store.cond.wait(min(0.1, max(0.01, (
                        first if now < first else deadline) - now)))
                rows = store.newest(count) if store.head_at else []
                failed = store.head_error or store.error
                loading = (store.workers > 0 or store.head_busy
                           or not store.finished())
                if store.have() < count and not store.finished() and \
                        store.workers == 0 and store.head_at:
                    # one quiet reader goes on behind the answer
                    store.extra = BARS_FILL_EXTRA
                    store.workers += 1
                    threading.Thread(target=self._bar_worker, daemon=True,
                                     args=(store, epic, tf),
                                     name="capitalcom-bars").start()
        finally:
            with store.cond:
                store.asking -= 1
                store.waiters.pop(me, None)
                if store.waiters:
                    store.want = max(store.waiters.values())
        if rows or not (failed or loading):
            return {"bars": rows}
        if failed is not None and failed.status in (401, 403):
            raise self._err(failed)
        raise AdapterError(
            "rate_limited" if failed is not None and failed.status == 429
            else "internal", "Capital.com has not sent any %s bars for %s "
            "yet (%s). Ask again in a moment." % (
                tf, epic, failed.message if failed is not None
                else "it is still answering"), retryable=True)

    def handle_bars_subscribe(self, p):
        spec, tf = self._bar_spec(p)
        width, now = TIMEFRAMES[tf][1], self.now_ms()
        with self._lock:
            # From the bar now forming: nothing already closed is replayed;
            # that bar is sent when it closes.
            self._bar_subs.setdefault((spec["symbol"], tf), {
                "mark": now // width * width - 1, "tries": 0,
                "next": (now // width + 1) * width + 2500})
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, p):
        spec, tf = self._bar_spec(p)
        with self._lock:
            self._bar_subs.pop((spec["symbol"], tf), None)
        return {}

    def _poll_bars(self):
        """CLOSED bars for every subscription that is due (SPEC 4), read
        over REST like bars.history, at the bid, a watermark so no bar
        repeats."""
        now = self.now_ms()
        with self._lock:
            due = [k for k, v in self._bar_subs.items() if now >= v["next"]]
        for epic, tf in due:
            res, width = TIMEFRAMES[tf]
            with self._lock:
                sub = self._bar_subs.get((epic, tf))
            if sub is None:
                continue
            start = max(sub["mark"] + 1, now - BAR_SUB_REACH * width)
            try:
                rows, _ = self._bar_fetch(epic, tf, start, now, "background",
                                          2.0)
            except CapitalError as e:
                log("bars %s %s: %s" % (epic, tf, e.message))
                rows = []
            fresh = sorted(r for r in rows if r[0] > sub["mark"]
                           and r[0] + width <= now)
            edge = (now // width + 1) * width + 2500
            with self._lock:
                if (epic, tf) not in self._bar_subs:
                    continue
                if fresh:
                    sub["mark"], sub["tries"] = fresh[-1][0], 0
                    sub["next"] = edge
                else:
                    sub["tries"] += 1
                    sub["next"] = min(edge, now + (5000, 15000, 60000,
                                                   300000)[
                        min(sub["tries"], 4) - 1])
            for row in fresh:
                self.emit("bar", {"symbol": epic, "timeframe": tf,
                                  "bar": row})

    # == the poller: what nobody pushes ==================================
    def _poll_loop(self):
        """Capital.com pushes no order, fill or position change: this reads
        them. Fast while anything is open or in flight, slow otherwise.
        Survives any exception, and says so."""
        last = 0.0
        self._stop.wait(1.0)
        while not self._stop.is_set():
            gap = time.monotonic() - last
            if gap < 0.5:
                self._stop.wait(0.5 - gap)
            last = time.monotonic()
            try:
                self._poll_once()
                if "poll" in self._components and \
                        self._components["poll"][0] != "connected":
                    self._set_session("poll", "connected", "")
            except CapitalError as e:
                text = self.client.scrub(e.message)
                if e.status in (401, 403):
                    self._set_session("poll", "disconnected", "Capital.com "
                                      "refused this login (%s)" % (
                                          e.code or "HTTP %d" % e.status))
                elif e.code not in ("budget",):
                    self._set_session("poll", "degraded", "reading the "
                                      "account failed (%s); trying again"
                                      % text[:100])
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log("poll: %s: %s" % (type(e).__name__,
                                      self.client.scrub(str(e))))
                self._set_session("poll", "degraded", "reading the account "
                                  "failed (%s); trying again"
                                  % type(e).__name__)
            with self._lock:
                busy = bool(self._deals or self._worders or self._gone
                            or self._wo_gone or self._closing
                            or self._open_entries(("market", "limit",
                                                   "stop")))
            self._wake.wait(timeout=POLL_FAST if busy else POLL_SLOW)
            self._wake.clear()

    def _poll_once(self):
        mono = time.monotonic()
        if mono >= self._next_clock:
            self._sync_clock()
        # 1. orders whose confirmation the handler did not wait for
        with self._lock:
            sent = [e for e in self._open_entries(("market", "limit", "stop"))
                    if e.get("state") == "sent" and e.get("ref")]
        for e in sent:
            try:
                c = self.client.call("GET", "/confirms/" + urllib.parse.quote(
                    e["ref"], safe=""), kind="background", max_wait=2.0)
            except CapitalError as err:
                if err.status != 404:
                    raise
                c = None
            if isinstance(c, dict) and str(c.get("dealStatus") or "").upper(
                    ) in ("ACCEPTED", "REJECTED"):
                self._apply_confirm(e, c, kind="background")
            elif self.now_ms() - self._started(e) > CONFIRM_KEEP * 1000:
                with self._lock:
                    e["state"] = "unknown"
                    self._unsaved = True
            elif self.now_ms() - self._started(e) > 30_000:
                self._set_session("order:" + e["key"], "degraded", (
                    "Capital.com has not confirmed the order %s (%s %s %s) "
                    "after %d s" % (e["order_id"], e["dir"].lower(),
                                    e["size"], e["epic"],
                                    (self.now_ms() - int(e["at"])) // 1000)))
        # 2. the book (and what it explains of orders of unknown fate)
        self._read_book(kind="background")
        # 3. orders of unknown fate: found, or given up after UNKNOWN_SEC
        now = self.now_ms()
        with self._lock:
            for key, e in list(self._journal.items()):
                comp = "order:" + key
                if e.get("state") in ("sending", "unknown"):
                    if now - self._started(e) > UNKNOWN_SEC * 1000:
                        e["state"] = "lost"
                        self._unsaved = True
                        log("order %s %s %s (client_id %s) lost its answer "
                            "and was not found at Capital.com" % (
                                e["dir"].lower(), e["size"], e["epic"],
                                e.get("cid") or "-"))
            lost = [e for e in self._journal.values() if e.get("state")
                    == "lost" and now - self._started(e)
                    <= UNKNOWN_KEEP * 1000]
        for e in lost:
            self._set_session("order:" + e["key"], "degraded", (
                "an order sent at %s UTC (%s %s %s, client_id %s) lost its "
                "answer and was not found at Capital.com; it was not sent "
                "again, and is reported if it turns up" % (
                    utc_text(int(e["at"])), e["dir"].lower(), e["size"],
                    e["epic"], e.get("cid") or "-")))
        self._refresh_session()
        # 4. closes this adapter sent and did not tell: their confirmation
        self._poll_closes()
        # 5. everything else that went: the activity history says why
        need = self._activity_needed()
        if need:
            out = self.client.call("GET", "/history/activity", {
                "lastPeriod": need, "detailed": "true"},
                kind="background", max_wait=2.0) or {}
            rows = [r for r in out.get("activities") or []
                    if isinstance(r, dict)]
            with self._lock:
                self._settle_gone(rows)
            if any(str(r.get("source") or "").upper() == "CLOSE_OUT"
                   for r in rows if int(parse_utc(r.get("dateUTC")) or 0)
                   >= now - 60_000):
                with self._lock:
                    if self._once("stopout:%d" % (now // 60_000)):
                        self.emit("margin.warning", {"stage": "stop_out"})
        # 6. the account, the money that moved without a trade
        if mono >= self._next_account:
            self._next_account = mono + ACCOUNT_POLL
            self._poll_account()
        if mono >= self._next_tx:
            self._next_tx = mono + TX_POLL
            self._poll_operations()
        # 7. bars, prices while the stream is down, the session kept alive
        self._poll_bars()
        with self._lock:
            live = sorted(self._quote_live)
        if live and not self._stream_up.is_set() and (
                mono - self._last_rest_quotes >= QUOTE_FALLBACK_SEC):
            try:
                self._rest_quotes(live, "background")
            except CapitalError as e:
                log("prices over REST: %s" % e.message)
        if mono - self.client.last_use > SESSION_IDLE_PING:
            # documented: GET /ping "to keep a trading session alive"
            self.client.call("GET", "/ping", kind="background", max_wait=2.0)
        if time.time() - self._catalog_built > CATALOG_REFRESH and \
                self._catalog_built and not self._catalog_busy:
            threading.Thread(target=self._refresh_catalog, daemon=True,
                             name="capitalcom-catalog").start()
        if self._unsaved:
            self._save_state()

    @staticmethod
    def _started(e: dict) -> int:
        """When this run began looking for an order's outcome: when it was
        sent, or, for one of the last run, when this run started (see
        _load_state)."""
        return max(int(e.get("at") or 0), int(e.get("since") or 0))

    def _poll_closes(self):
        """Closes this adapter sent whose outcome it did not get to tell
        (`_closing`), by their confirmations (documented: GET
        /confirms/{dealReference}). ACCEPTED: told from it once the deal has
        left the book (a read may lag the close). REJECTED: the close did not
        happen, nothing is told, and what was handed to the poller is
        withdrawn (so a close whose confirmation comes late and says REJECTED
        is not told closed). Not confirmed after CONFIRM_KEEP while the deal
        is still listed: given up, and said on stderr."""
        with self._lock:
            mine = [(did, dict(c)) for did, c in self._closing.items()]
        now = self.now_ms()
        for did, c in mine:
            conf = None
            if c.get("ref"):
                try:
                    conf = self.client.call(
                        "GET", "/confirms/" + urllib.parse.quote(
                            c["ref"], safe=""), kind="background",
                        max_wait=2.0)
                except CapitalError as err:
                    if err.code == "budget":
                        return
                    conf = None
            status = str(conf.get("dealStatus") or "").upper() \
                if isinstance(conf, dict) else ""
            with self._lock:
                if did not in self._closing:
                    continue              # told meanwhile
                g = self._gone.get(did)
                if status == "ACCEPTED":
                    if g is not None:
                        self._own_close(g["deal"], _num(conf.get("level")),
                                        c.get("reason") or "manual", None)
                elif status == "REJECTED":
                    self._closing.pop(did, None)
                    if g is not None and did in self._deals:
                        del self._gone[did]
                    log("Capital.com refused the close of %s (%s): it is "
                        "still open" % (did, conf.get("reason") or "REJECTED"))
                elif (now - int(c.get("at") or now) > CONFIRM_KEEP * 1000
                      and g is None and did in self._deals):
                    self._closing.pop(did, None)
                    log("the close of %s sent at %s UTC was never confirmed, "
                        "and Capital.com still lists it open" % (
                            did, utc_text(int(c.get("at") or now))))

    def _refresh_session(self):
        """Orders the poller has resolved, and lost ones after UNKNOWN_KEEP:
        what was said of them is unsaid, and the platform is told so."""
        now = self.now_ms()
        with self._lock:
            settled = []
            for k in self._components:
                if not k.startswith("order:"):
                    continue
                e = self._journal.get(k[6:]) or {}
                st = e.get("state")
                if st not in ("sending", "unknown", "sent", "lost") or (
                        st == "lost" and now - self._started(e)
                        > UNKNOWN_KEEP * 1000):
                    settled.append(k)
        for k in settled:
            self._set_session(k, "connected", "")
            with self._lock:
                self._components.pop(k, None)

    def _refresh_catalog(self):
        try:
            self._load_catalog(refresh=True)
        except Exception as e:  # noqa: BLE001 - the old catalog stands
            log("catalog refresh failed: %s" % self.client.scrub(str(e)))
            self._catalog_built = time.time() - CATALOG_REFRESH + 600

    def _poll_account(self):
        self._read_accounts("background")
        with self._lock:
            acct = self._account
        if acct is None:
            return
        view = self._account_view(acct)
        key = (view["balance"], view["equity"], view["margin_used"],
               view["margin_free"])
        if key != self._account_sent:
            self._account_sent = key
            self.emit("account.updated", view)
        used = view["margin_used"]
        if used > 0:
            level = view["equity"] / used * 100.0
            # documented (11-11 research of the UK help centre): margin calls
            # under 100% and 75%, close-out from 50%
            if level < 100.0:
                with self._lock:
                    first = self._once("call:%d" % int(time.time() // 600))
                if first:
                    self.emit("margin.warning", {"stage": "call",
                                                 "margin_level":
                                                 round(level, 2)})

    def _poll_operations(self):
        """balance.operation for every transaction that is not a deal's own
        result (SPEC 5), each once; the first read only marks what is there."""
        now = self.now_ms()
        lo = self._ops_mark or now - 3_600_000
        rows = self._transactions(max(lo - 120_000, now - 86_400_000), now,
                                  kind="background")
        fresh = []
        with self._lock:
            baseline = not self._ops_mark
            for r in rows:
                op = self._operation(r)
                if op is None or op["operation_id"] in self._ops_seen:
                    continue
                self._ops_seen[op["operation_id"]] = True
                if not baseline:
                    fresh.append(op)
            self._ops_mark = now
            self._unsaved = True
        for op in sorted(fresh, key=lambda o: o["time"]):
            self.emit("balance.operation", op)


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    adapter = CapitalComAdapter()
    adapter.run()
    # The pipe closed (or `bye`): an order still being carried out on its
    # lane is not abandoned half-way when the process ends.
    adapter._drain(HANDLER_BUDGET + 1.5)
    adapter._save_state()


if __name__ == "__main__":
    main()
