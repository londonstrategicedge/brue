#!/usr/bin/env python3
"""A local stand-in for Capital.com's Public API v1, written from Capital.com's own documentation alone.

REST answers on one loopback port and the price WebSocket on a second, because Capital.com streams from a host of its
own (wss://api-streaming-capital.backend-capital.com/connect) apart from its REST hosts. It plays the DEMO host
(https://demo-api-capital.backend-capital.com/api/v1): one login, its demo accounts, simulated prices and fills.
Standard library only, Python >= 3.10. No key, no network.

Where each answer comes from. Every behaviour carries a comment that says which of these it is:
  "documented: <URL>"         what Capital.com's documentation says or shows in its samples. The sources:
        https://open-api.capital.com/                         the API reference: samples, limits, FAQ, changelog
        https://github.com/capital-com-sv/capital-api-postman  Capital.com's Postman collection (capital.collection.json):
                                                              its saved example answers are the only place the error
                                                              bodies {"errorCode": ...} are shown
        https://github.com/capital-com-sv/capital-mcp          Capital.com's own client: how it logs in, polls /confirms,
                                                              amends working orders, streams
        help-centre articles and the UK terms, named where they are used
  "assumed, verify on demo"   the documentation is silent; the stand-in takes the likeliest reading and the real demo
                              account has to settle it. Where an adapter must survive either reading, a control switches
                              to the other one (see set_option).
  "stand-in's own"            a choice of this program alone: values, and the names of refusals Capital.com never wrote down.

    python tests/fake_capitalcom.py --port 0      # prints one JSON line (URLs, fake credentials) and serves until killed

    fake = FakeCapitalCom(market="open").start()  # in a test: fake.api_url, fake.stream_url, fake.api_key, ...
    ...; fake.stop()

Served on the REST port, under /api/v1, with the session headers CST and X-SECURITY-TOKEN except where marked:
  GET  /time (no session)          GET /ping
  GET  /session/encryptionKey (X-CAP-API-KEY)    POST /session (X-CAP-API-KEY)    GET|PUT|DELETE /session
  GET  /accounts    GET|PUT /accounts/preferences    POST /accounts/topUp
  GET  /history/activity    GET /history/transactions    GET /confirms/{dealReference}
  GET|POST /positions    GET|PUT|DELETE /positions/{dealId}    GET|POST /workingorders    PUT|DELETE /workingorders/{dealId}
  GET  /marketnavigation[/{nodeId}]    GET /markets[?searchTerm=|epics=]    GET /markets/{epic}    GET /prices/{epic}
  GET  /clientsentiment?marketIds=    GET /clientsentiment/{marketId}
  GET|POST /watchlists    GET|PUT|DELETE /watchlists/{id}    DELETE /watchlists/{id}/{epic}
Served on the stream port: GET /connect, upgraded to a WebSocket. JSON text frames; destinations marketData.subscribe,
  marketData.unsubscribe, OHLCMarketData.subscribe, OHLCMarketData.unsubscribe and ping; pushes "quote" and "ohlc.event".

Controls, for tests (no session, never rate limited, never in the request log; each is also a method of FakeCapitalCom):
  POST /_control/price      {"epic", "bid", "ask"} hold the price there | {"epic", "jump_pct"} | {"epic",
                            "trend_pct_per_s", "seconds"} | {"epic", "walk": true} release a hold | {"epic", "spread"}
  POST /_control/market     {"state": "open"|"closed"|"auto", "epic"?}    auto follows each market's openingHours
  POST /_control/clock      {"advance_s": N}      moves the stand-in's clock forward (orders are looked at on the way)
  POST /_control/fills      {"parts": [w, ...], "rest": "fill"|"cancel", "interval_s": x}   the next fill comes in parts
  POST /_control/fill_rule  {"size": N, "parts": [w, ...], "epic"?} | {"clear": true}   every order of that size, in parts
  POST /_control/refuse     {"error_code", "status": 400, "path": "/positions", "method": "POST", "count": 1,
                            "at": "request"|"confirm"}     the next such request is refused with that errorCode
  POST /_control/fail       {"status": 429|500|502|503, "count": 1, "path"?, "method"?, "page"?}   the next requests answer
                            that status; with "page": true a gateway's HTML page (CRLF lines) instead of Capital.com's JSON
  POST /_control/lose       {"path"?, "method"?, "count": 1}     the next such request is carried out, its answer lost
  POST /_control/expire     {}                            every session ends now (the next call is answered 401)
  POST /_control/key        {"revoked": true|false}       the API key stops (or starts again) opening sessions
  POST /_control/login      {"no_account": true|false}    the login has no account on this host: POST /session is answered
                            401 error.null.accountId (seen on the demo host, 2026-11-15)
  POST /_control/drop_streams {"clean": false}            cuts every WebSocket connection
  POST /_control/confirm_delay {"seconds": x}             how long a confirmation takes to appear in /confirms
  POST /_control/history    {"resolution", "days"} | {"resolution", "reset": true}    how far back bars go
  POST /_control/option     {"name", "value"}             switches an undocumented behaviour (see OPTIONS)
  POST /_control/close_as   {"deal_id", "source": "DEALER"|"SYSTEM"|"CLOSE_OUT"}   the broker closes a deal itself
  POST /_control/adjust     {"amount", "account_id"?, "type": "ADJUSTMENT"}        money moves without a trade
  POST /_control/tick       {}         one pass of the order engine and the streams, now
  POST /_control/reset      {}         accounts, prices, markets, sessions and controls as at the start
  GET  /_control/requests[?clear=1]    the request log (method, path, query, status, body; never a credential)
  GET  /_control/stream_log            what the WebSocket clients sent (destination, correlationId, payload; no tokens)
  GET  /_control/state                 clock, markets, options, sessions, streams and accounts at a glance

The market inside: 46 CFDs (16 FX pairs, 8 indices, 7 commodities, 11 shares of which 5CPSG is the documented
view-only example, 4 cryptocurrencies that trade every day). Each mid price is a seeded walk fixed by (seed, epic,
time); every quote and every bar of every resolution is read from that one walk, so they agree.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import itertools
import json
import math
import queue
import random
import re
import secrets
import socket
import struct
import sys
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

DOC = "https://open-api.capital.com/"
POSTMAN = "https://github.com/capital-com-sv/capital-api-postman"
MCP = "https://github.com/capital-com-sv/capital-mcp"

DAY_MS, MIN_MS, SEC_MS = 86400000, 60000, 1000
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ZERO, CENT = Decimal(0), Decimal("0.01")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# documented: https://open-api.capital.com/ ("Possible values are MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4,
# DAY, WEEK"), as minutes per bar
RESOLUTIONS = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15, "MINUTE_30": 30, "HOUR": 60, "HOUR_4": 240, "DAY": 1440, "WEEK": 10080}

# documented: https://open-api.capital.com/ (GET /history/activity: the FIQL fields and their values)
ACTIVITY_SOURCES = ("CLOSE_OUT", "DEALER", "SL", "SYSTEM", "TP", "USER")
ACTIVITY_STATUSES = ("ACCEPTED", "CREATED", "EXECUTED", "EXPIRED", "REJECTED", "MODIFIED", "MODIFY_REJECT", "CANCELLED",
                     "CANCEL_REJECT", "UNKNOWN")
ACTIVITY_TYPES = ("POSITION", "WORKING_ORDER", "EDIT_STOP_AND_LIMIT", "SWAP", "SYSTEM")
# documented: https://open-api.capital.com/ (GET /history/transactions, parameter `type`)
TRANSACTION_TYPES = ("INACTIVITY_FEE", "RESERVE", "VOID", "UNRESERVE", "WRITE_OFF_OR_CREDIT", "CREDIT_FACILITY", "FX_COMMISSION",
                     "COMPLAINT_SETTLEMENT", "DEPOSIT", "WITHDRAWAL", "REFUND", "WITHDRAWAL_MONEY_BACK", "TRADE", "SWAP",
                     "TRADE_COMMISSION", "TRADE_COMMISSION_GSL", "NEGATIVE_BALANCE_PROTECTION", "TRADE_CORRECTION", "CHARGEBACK",
                     "ADJUSTMENT", "BONUS", "TRANSFER", "CORPORATE_ACTION", "CONVERSION", "REBATE", "TRADE_SLIPPAGE_PROTECTION")

# Behaviours the documentation leaves open, each with the reading the stand-in starts from and the others it can play.
# set_option(name, value) switches one; reset() puts them back.
OPTIONS = {
    # which bars come back when from..to holds more than `max`: assumed, verify on demo. "oldest" follows the one
    # documented example, where `from` with max=4 answered the four bars starting at `from` (documented:
    # https://github.com/capital-com-sv/capital-api-postman, "Historical prices", "Use not default resolution, start
    # date and max parameters"); "newest" the last `max` before `to`; "error" refuses the range
    "prices_overflow": ("oldest", ("oldest", "newest", "error")),
    # what a range with no bars in it answers: assumed, verify on demo
    "prices_empty": ("error", ("error", "empty")),
    # whether the bar still forming is answered by GET /prices: assumed, verify on demo
    "prices_forming": (True, (True, False)),
    # OHLCMarketData pushes the forming bar as it changes, or each bar once when it has closed: assumed, verify on demo
    "ohlc_mode": ("forming", ("forming", "closed")),
    # the currency of a position's `upl`: assumed, verify on demo ("account" = the account's, "instrument" = the market's)
    "upl_currency": ("account", ("account", "instrument")),
    # a stop or profit leg left out of PUT /positions/{dealId}: kept (as Capital.com's own client says the broker does for
    # working orders; documented: https://github.com/capital-com-sv/capital-mcp) or cleared. Assumed, verify on demo
    "position_update_omitted": ("keep", ("keep", "clear")),
    # does a resting working order hold margin? assumed, verify on demo (the UK terms 3.15 say pending orders block
    # funds, among share-dealing wording: https://img.capital.com/docs/September-2024-CCUK-Terms-and-Conditions-_0.pdf)
    "wo_reserves_margin": (False, (False, True)),
    # does a WebSocket message carrying the tokens count as a "use" that keeps the REST session alive? assumed, verify
    # on demo; False is the careful reading (the adapter must keep REST alive with GET /ping)
    "ws_extends_session": (False, (False, True)),
    # is the session allowed trailing stops (the session's trailingStopsEnabled)? documented field:
    # https://open-api.capital.com/ ; its value on a demo login is the stand-in's own
    "trailing_enabled": (True, (True, False)),
    # does a WebSocket connection end when the session its messages carry ends (lapsed or logged out)? assumed, verify
    # on demo; True is the careful reading. A switch of account always ends it (documented: https://open-api.capital.com/,
    # "WebSocket streaming falls off when the financial account is changed")
    "stream_ends_with_session": (True, (True, False)),
}


# -- small helpers -----------------------------------------------------------------------------------------------------

def stamp(ms, millis=True):
    """documented: https://open-api.capital.com/ (times print as 2022-04-06T07:32:19.193, prices as 2022-04-06T13:18:00,
    with no zone letter)"""
    s, r = divmod(int(ms), 1000)
    t = time.gmtime(s)
    out = "%04d-%02d-%02dT%02d:%02d:%02d" % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    return out + (".%03d" % r) if millis else out


_WHEN = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)")


def parse_when(text):
    """documented: https://open-api.capital.com/ (from, to and goodTillDate are YYYY-MM-DDTHH:MM:SS, in UTC)"""
    m = _WHEN.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError("not YYYY-MM-DDTHH:MM:SS")
    return int(datetime(*map(int, m.groups()), tzinfo=timezone.utc).timestamp()) * 1000


def rnd(x, dp):
    """A price or figure as a JSON number with `dp` decimals (half up), never -0.0."""
    d = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    return float(d) + 0.0


def money(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def dec(x):
    """A JSON number from a request as an exact Decimal; ValueError for anything else (booleans included)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError("not a number")
    try:
        d = Decimal(repr(x)) if isinstance(x, float) else Decimal(x)
    except InvalidOperation:
        raise ValueError("not a number")
    if not d.is_finite():
        raise ValueError("not finite")
    return d


def num(d):
    return float(d) + 0.0


def _u(key, *parts):
    h = hashlib.blake2b("|".join((key,) + tuple(str(p) for p in parts)).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 18446744073709551616.0


def _rng(key, *parts):
    h = hashlib.blake2b("|".join((key,) + tuple(str(p) for p in parts)).encode(), digest_size=8).digest()
    return random.Random(int.from_bytes(h, "big"))


class _Lru:
    def __init__(self, limit):
        self.limit, self.items, self.lock = limit, collections.OrderedDict(), threading.Lock()

    def get(self, key, make):
        with self.lock:
            hit = self.items.get(key)
            if hit is not None:
                self.items.move_to_end(key)
                return hit
        made = make()
        with self.lock:
            self.items[key] = made
            while len(self.items) > self.limit:
                self.items.popitem(last=False)
        return made


class CapError(Exception):
    """A refusal as Capital.com writes one: an HTTP status and {"errorCode": ...}.
    documented: https://github.com/capital-com-sv/capital-api-postman (every saved error example has this body)"""

    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


# -- the instruments ---------------------------------------------------------------------------------------------------

def _week(weekday, fri=None, sat=(), sun=()):
    return {"mon": list(weekday), "tue": list(weekday), "wed": list(weekday), "thu": list(weekday),
            "fri": list(weekday if fri is None else fri), "sat": list(sat), "sun": list(sun), "zone": "UTC"}


# documented: https://open-api.capital.com/ (instrument.openingHours: per weekday a list of "HH:MM - HH:MM", zone UTC;
# "metal" is the sample's silver week). The other weeks are the stand-in's own, in the same shape.
_HOURS = {
    "metal": _week(["00:00 - 21:59", "23:05 - 00:00"], fri=["00:00 - 21:59"], sun=["23:05 - 00:00"]),
    "fx": _week(["00:00 - 21:59", "22:05 - 00:00"], fri=["00:00 - 21:59"], sun=["22:05 - 00:00"]),
    "index": _week(["00:00 - 21:59", "23:00 - 00:00"], fri=["00:00 - 21:59"], sun=["23:00 - 00:00"]),
    "europe": _week(["07:00 - 20:59"]),
    "us_share": _week(["14:30 - 20:59"]),
    "asia_share": _week(["01:00 - 08:59"]),
    "crypto": _week(["00:00 - 00:00"], sat=["00:00 - 00:00"], sun=["00:00 - 00:00"]),
}

# per type: guaranteedStopAllowed, minGuaranteedStopDistance, minStopOrProfitDistance, maxStopOrProfitDistance,
# trailingStopsPreference, overnight (longRate, shortRate), volume a minute. Shapes documented:
# https://open-api.capital.com/ (GET /markets/{epic}: units POINTS or PERCENTAGE); the commodity row is the sample's
# silver; the rest are the stand-in's own values. A POINTS distance is in price units; None = five ticks.
_KIND = {
    "CURRENCIES": (True, ("PERCENTAGE", 0.3), ("POINTS", None), ("PERCENTAGE", 30), "AVAILABLE", (-0.0071, -0.0015), 120),
    "INDICES": (True, ("PERCENTAGE", 0.5), ("PERCENTAGE", 0.01), ("PERCENTAGE", 60), "AVAILABLE", (-0.0205, 0.0045), 180),
    "COMMODITIES": (True, ("PERCENTAGE", 1), ("PERCENTAGE", 0.01), ("PERCENTAGE", 60), "AVAILABLE", (-0.0199715, 0.0117515), 160),
    "SHARES": (False, ("PERCENTAGE", 2), ("PERCENTAGE", 0.05), ("PERCENTAGE", 75), "AVAILABLE", (-0.0228, 0.0061), 90),
    "CRYPTOCURRENCIES": (False, ("PERCENTAGE", 5), ("PERCENTAGE", 0.1), ("PERCENTAGE", 90), "AVAILABLE", (-0.0616, -0.0137), 60),
}

# epic, symbol, instrumentName, type, currency, mid, spread, decimals, week, minDealSize, maxDealSize, minSizeIncrement,
# daily volatility. stand-in's own values, except SILVER (lotSize 1, minDealSize 1, maxDealSize 100000, minSizeIncrement
# 0.1, three decimals) and 5CPSG (CLOSED, VIEW_ONLY), which are the documented samples: https://open-api.capital.com/
_TABLE = (
    ("EURUSD", "EUR/USD", "EUR/USD", "CURRENCIES", "USD", 1.0850, 0.00006, 5, "fx", 100, 5000000, 1, 0.005),
    ("GBPUSD", "GBP/USD", "GBP/USD", "CURRENCIES", "USD", 1.2700, 0.00009, 5, "fx", 100, 5000000, 1, 0.006),
    ("USDJPY", "USD/JPY", "USD/JPY", "CURRENCIES", "JPY", 150.20, 0.007, 3, "fx", 100, 5000000, 1, 0.006),
    ("AUDUSD", "AUD/USD", "AUD/USD", "CURRENCIES", "USD", 0.6550, 0.00006, 5, "fx", 100, 5000000, 1, 0.007),
    ("USDCAD", "USD/CAD", "USD/CAD", "CURRENCIES", "CAD", 1.3650, 0.00010, 5, "fx", 100, 5000000, 1, 0.005),
    ("USDCHF", "USD/CHF", "USD/CHF", "CURRENCIES", "CHF", 0.8850, 0.00010, 5, "fx", 100, 5000000, 1, 0.006),
    ("NZDUSD", "NZD/USD", "NZD/USD", "CURRENCIES", "USD", 0.6050, 0.00010, 5, "fx", 100, 5000000, 1, 0.007),
    ("EURGBP", "EUR/GBP", "EUR/GBP", "CURRENCIES", "GBP", 0.8545, 0.00010, 5, "fx", 100, 5000000, 1, 0.004),
    ("EURJPY", "EUR/JPY", "EUR/JPY", "CURRENCIES", "JPY", 162.95, 0.012, 3, "fx", 100, 5000000, 1, 0.006),
    ("GBPJPY", "GBP/JPY", "GBP/JPY", "CURRENCIES", "JPY", 190.75, 0.020, 3, "fx", 100, 5000000, 1, 0.007),
    ("EURCHF", "EUR/CHF", "EUR/CHF", "CURRENCIES", "CHF", 0.9600, 0.00015, 5, "fx", 100, 5000000, 1, 0.004),
    ("AUDJPY", "AUD/JPY", "AUD/JPY", "CURRENCIES", "JPY", 98.40, 0.015, 3, "fx", 100, 5000000, 1, 0.008),
    ("EURAUD", "EUR/AUD", "EUR/AUD", "CURRENCIES", "AUD", 1.6560, 0.00015, 5, "fx", 100, 5000000, 1, 0.006),
    ("GBPCHF", "GBP/CHF", "GBP/CHF", "CURRENCIES", "CHF", 1.1240, 0.00030, 5, "fx", 100, 5000000, 1, 0.006),
    ("CADJPY", "CAD/JPY", "CAD/JPY", "CURRENCIES", "JPY", 110.00, 0.015, 3, "fx", 100, 5000000, 1, 0.007),
    ("AUDCAD", "AUD/CAD", "AUD/CAD", "CURRENCIES", "CAD", 0.8940, 0.00020, 5, "fx", 100, 5000000, 1, 0.005),
    ("US500", "US500", "US 500", "INDICES", "USD", 5900.0, 0.4, 1, "index", 0.1, 1000, 0.1, 0.010),
    ("US100", "US100", "US Tech 100", "INDICES", "USD", 20600.0, 1.0, 1, "index", 0.1, 1000, 0.1, 0.012),
    ("US30", "US30", "US Wall Street 30", "INDICES", "USD", 43500.0, 2.4, 1, "index", 0.1, 1000, 0.1, 0.009),
    ("UK100", "UK100", "UK 100", "INDICES", "GBP", 8250.0, 1.0, 1, "europe", 0.1, 1000, 0.1, 0.009),
    ("DE40", "DE40", "Germany 40", "INDICES", "EUR", 19500.0, 1.2, 1, "europe", 0.1, 1000, 0.1, 0.010),
    ("FR40", "FR40", "France 40", "INDICES", "EUR", 7300.0, 1.0, 1, "europe", 0.1, 1000, 0.1, 0.010),
    ("J225", "J225", "Japan 225", "INDICES", "JPY", 38500.0, 7.0, 0, "index", 0.1, 1000, 0.1, 0.012),
    ("AU200", "AU200", "Australia 200", "INDICES", "AUD", 8200.0, 1.0, 1, "index", 0.1, 1000, 0.1, 0.009),
    ("GOLD", "Gold", "Gold", "COMMODITIES", "USD", 2650.00, 0.30, 2, "metal", 0.01, 1000, 0.01, 0.012),
    ("SILVER", "Silver", "Silver", "COMMODITIES", "USD", 31.000, 0.020, 3, "metal", 1, 100000, 0.1, 0.018),
    ("OIL_CRUDE", "Oil - Crude", "US Crude Oil", "COMMODITIES", "USD", 70.00, 0.03, 2, "metal", 1, 10000, 0.1, 0.020),
    ("OIL_BRENT", "Oil - Brent", "Brent Crude Oil", "COMMODITIES", "USD", 74.00, 0.03, 2, "metal", 1, 10000, 0.1, 0.019),
    ("NATURALGAS", "Natural Gas", "Natural Gas", "COMMODITIES", "USD", 3.000, 0.005, 3, "metal", 100, 1000000, 100, 0.030),
    ("COPPER", "Copper", "Copper", "COMMODITIES", "USD", 4.2000, 0.0020, 4, "metal", 10, 100000, 1, 0.015),
    ("PLATINUM", "Platinum", "Platinum", "COMMODITIES", "USD", 950.0, 1.2, 1, "metal", 0.1, 1000, 0.1, 0.015),
    ("AAPL", "AAPL", "Apple Inc", "SHARES", "USD", 230.00, 0.10, 2, "us_share", 1, 10000, 1, 0.018),
    ("MSFT", "MSFT", "Microsoft Corp", "SHARES", "USD", 420.00, 0.20, 2, "us_share", 1, 10000, 1, 0.016),
    ("TSLA", "TSLA", "Tesla Inc", "SHARES", "USD", 250.00, 0.25, 2, "us_share", 1, 10000, 1, 0.035),
    ("AMZN", "AMZN", "Amazon.com Inc", "SHARES", "USD", 200.00, 0.12, 2, "us_share", 1, 10000, 1, 0.020),
    ("NVDA", "NVDA", "NVIDIA Corp", "SHARES", "USD", 140.00, 0.08, 2, "us_share", 1, 10000, 1, 0.030),
    ("META", "META", "Meta Platforms Inc", "SHARES", "USD", 580.00, 0.30, 2, "us_share", 1, 10000, 1, 0.022),
    ("GOOGL", "GOOGL", "Alphabet Inc - A", "SHARES", "USD", 175.00, 0.10, 2, "us_share", 1, 10000, 1, 0.019),
    ("NFLX", "NFLX", "Netflix Inc", "SHARES", "USD", 750.00, 0.50, 2, "us_share", 1, 10000, 1, 0.024),
    ("AMD", "AMD", "Advanced Micro Devices Inc", "SHARES", "USD", 140.00, 0.10, 2, "us_share", 1, 10000, 1, 0.030),
    ("INTC", "INTC", "Intel Corp", "SHARES", "USD", 22.00, 0.04, 2, "us_share", 1, 10000, 1, 0.025),
    ("5CPSG", "5CPsg", "Silverlake Axis", "SHARES", "SGD", 0.320, 0.009, 3, "asia_share", 100, 1000000, 100, 0.020),
    ("BTCUSD", "BTC/USD", "Bitcoin / USD", "CRYPTOCURRENCIES", "USD", 90000.00, 60.00, 2, "crypto", 0.01, 100, 0.01, 0.035),
    ("ETHUSD", "ETH/USD", "Ethereum / USD", "CRYPTOCURRENCIES", "USD", 3200.00, 3.00, 2, "crypto", 0.1, 1000, 0.01, 0.040),
    ("LTCUSD", "LTC/USD", "Litecoin / USD", "CRYPTOCURRENCIES", "USD", 90.00, 0.40, 2, "crypto", 1, 10000, 0.1, 0.045),
    ("XRPUSD", "XRP/USD", "Ripple / USD", "CRYPTOCURRENCIES", "USD", 0.6000, 0.0030, 4, "crypto", 100, 1000000, 1, 0.050),
)
_VIEW_ONLY = {"5CPSG"}                       # documented: https://open-api.capital.com/ (sample: CLOSED, ["VIEW_ONLY"])
_NO_TRAILING = {"SILVER"}                    # documented: https://open-api.capital.com/ (sample: NOT_AVAILABLE)

# the accounts' leverage per asset class: the `available` lists are documented: https://open-api.capital.com/ (GET
# /accounts/preferences sample); the `current` values are the stand-in's own (the UK retail maximum in each list)
DEFAULT_LEVERAGE = (("SHARES", 5, [1, 2, 3, 5]), ("CURRENCIES", 30, [1, 10, 20, 30]), ("INDICES", 20, [1, 10, 20]),
                    ("CRYPTOCURRENCIES", 2, [1, 2]), ("COMMODITIES", 10, [1, 5, 10, 20]))


def _window(text):
    """'HH:MM - HH:MM' as minutes [start, end): the end is the last open minute, '00:00' meaning midnight.
    assumed, verify on demo (the sample's silver ends '21:59' where the older Postman example says '22:00')"""
    a, b = (part.strip() for part in text.split("-"))
    start = int(a[:2]) * 60 + int(a[3:])
    end = int(b[:2]) * 60 + int(b[3:])
    return start, 1440 if end == 0 else end + 1


class Instrument:
    def __init__(self, row):
        (self.epic, self.symbol, self.name, self.type, self.currency, self.mid, self.spread, self.dp, week,
         min_deal, max_deal, step, self.vol) = row
        self.tick = 10.0 ** -self.dp
        self.lot = 1                                         # stand-in's own (the documented samples show lotSize 1)
        self.min_deal, self.max_deal, self.step = Decimal(repr(min_deal)), Decimal(repr(max_deal)), Decimal(repr(step))
        self.hours = _HOURS[week]
        self.windows = {day: [_window(w) for w in self.hours[day]] for day in DAYS}
        gsl, gsl_min, stop_min, stop_max, trailing, (self.long_rate, self.short_rate), self.vol_minute = _KIND[self.type]
        self.gsl_allowed, self.gsl_min, self.stop_max = gsl, gsl_min, stop_max
        self.stop_min = ("POINTS", round(5 * self.tick, self.dp)) if stop_min[1] is None else stop_min
        self.trailing = "NOT_AVAILABLE" if self.epic in _NO_TRAILING else trailing
        self.view_only = self.epic in _VIEW_ONLY

    def in_hours(self, ms):
        wd = time.gmtime(ms // 1000).tm_wday
        minute = (ms // MIN_MS) % 1440
        return any(a <= minute < b for a, b in self.windows[DAYS[wd]])

    @staticmethod
    def distance(rule, ref):
        """A dealing-rule distance in price units at reference price `ref`."""
        unit, value = rule
        return ref * value / 100.0 if unit == "PERCENTAGE" else float(value)


# -- the price path ----------------------------------------------------------------------------------------------------

class Path:
    """The mid price of one instrument at any moment. stand-in's own: a seeded walk, fixed by (seed, epic, time).

    Days: each trading day's open is a mean-reverting walk of daily returns; its high and low are drawn per day. Inside
    a day, 1440 minute points form a bridge from that open to the next day's open that touches the day's high and low
    exactly; inside a minute, 60 one-second points touch that minute's high and low. A bar of any resolution, the daily
    summary and the streamed quotes are all read from these, so the 4-hour bar is the aggregate of its minutes.
    """

    KAPPA = 0.002
    REF = (date(2026, 1, 1) - date(1970, 1, 1)).days

    def __init__(self, key, mid0, vol, day_off, vol_day, days, secs):
        self.key, self.mid0, self.vol, self.off, self.vol_day = key, mid0, vol, day_off, vol_day
        self._days, self._secs = days, secs
        self._fwd, self._back, self._lock = [0.0], [0.0], threading.Lock()

    def day_of(self, ms):
        return (ms - self.off) // DAY_MS

    def day_start(self, d):
        return d * DAY_MS + self.off

    def _cum(self, d):
        i = d - self.REF
        with self._lock:
            arr = self._fwd if i >= 0 else self._back
            j = abs(i)
            while len(arr) <= j:
                k = len(arr)
                if i >= 0:
                    z = (_u(self.key, "r", self.REF + k - 1) - 0.5) * 3.4641016
                    arr.append(arr[-1] * (1 - self.KAPPA) + self.vol * z)
                else:
                    z = (_u(self.key, "r", self.REF - k) - 0.5) * 3.4641016
                    arr.append(arr[-1] * (1 - self.KAPPA) - self.vol * z)
            return arr[j]

    def summary(self, d):
        """(open, high, low, close, volume) of trading day d."""
        o = self.mid0 * math.exp(self._cum(d))
        c = self.mid0 * math.exp(self._cum(d + 1))
        h = max(o, c) * math.exp(self.vol * (0.1 + 0.5 * _u(self.key, "h", d)))
        lo = min(o, c) * math.exp(-self.vol * (0.1 + 0.5 * _u(self.key, "l", d)))
        v = int(self.vol_day * (0.6 + 0.8 * _u(self.key, "v", d)))
        return o, h, lo, c, v

    def minutes(self, d):
        """(points[0..1440], highs[0..1439], lows[0..1439], volumes[0..1439]) of day d: points[m] opens minute m."""
        return self._days.get((self.key, d), lambda: self._make(d))

    def _make(self, d):
        o, h, lo_d, c, v = self.summary(d)
        rand = _rng(self.key, "m", d).random
        n = 1440
        w = list(itertools.accumulate([rand() - 0.5 for _ in range(n)], initial=0.0))
        end = w[n]
        b = [w[k] - end * k / n for k in range(n + 1)]
        l0, l1, lh, ll = math.log(o), math.log(c), math.log(h), math.log(lo_d)
        lin = [l0 + (l1 - l0) * k / n for k in range(n + 1)]
        up = [(lh - lin[k]) / b[k] for k in range(1, n) if b[k] > 0]
        dn = [(lin[k] - ll) / -b[k] for k in range(1, n) if b[k] < 0]
        a, z = (min(up) if up else 0.0), (min(dn) if dn else 0.0)
        x = [lin[k] + (a * b[k] if b[k] > 0 else z * b[k]) for k in range(n + 1)]
        if not up:
            x[n // 3] = lh
        if not dn:
            x[2 * n // 3] = ll
        p = [math.exp(t) for t in x]
        p[0], p[n] = o, c
        span = (h - lo_d) / 50.0
        highs, lows = [], []
        for m in range(n):
            top, bot = (p[m], p[m + 1]) if p[m] >= p[m + 1] else (p[m + 1], p[m])
            highs.append(min(h, top + span * 0.6 * rand()))
            lows.append(max(lo_d, bot - span * 0.6 * rand()))
        weights = [0.3 + rand() for _ in range(n)]
        total = sum(weights)
        vols = [int(v * wt / total) for wt in weights]
        for k in range(v - sum(vols)):
            vols[(k * 7) % n] += 1
        return p, highs, lows, vols

    def seconds(self, d, m):
        """The 60 one-second mids of minute m of day d."""
        return self._secs.get((self.key, d, m), lambda: self._make_seconds(d, m))

    def _make_seconds(self, d, m):
        p, highs, lows, _ = self.minutes(d)
        o, c, top, bot = p[m], p[m + 1], highs[m], lows[m]
        rand = _rng(self.key, "s", d, m).random
        amp = (top - bot) * 0.35
        out = [min(top, max(bot, o + (c - o) * s / 60.0 + amp * (rand() - 0.5))) for s in range(60)]
        out[0] = o
        s_hi = 1 + int(rand() * 58)
        s_lo = 1 + int(rand() * 58)
        if s_lo == s_hi:
            s_lo = 1 + (s_hi % 58)
        out[s_hi], out[s_lo] = top, bot
        return out

    def mid(self, ms):
        d = self.day_of(ms)
        into = ms - self.day_start(d)
        return self.seconds(d, into // MIN_MS)[(into % MIN_MS) // SEC_MS]


class _Seg:
    """One price override, from `start` on. kind: hold (bid/ask until `end`), jump (x mult), trend (pct a second for
    dur ms), spread (a new spread). stand-in's own."""
    __slots__ = ("kind", "start", "end", "bid", "ask", "mid", "mult", "pct", "dur", "release", "spread")

    def __init__(self, kind, start, **kw):
        self.kind, self.start, self.end, self.release = kind, start, None, 1.0
        self.bid = self.ask = self.mid = self.mult = self.pct = self.dur = self.spread = None
        for k, v in kw.items():
            setattr(self, k, v)


class _Agg:
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self):
        self.o, self.h, self.l, self.c, self.v = None, -math.inf, math.inf, None, 0

    def add(self, o, h, lo, c, v=0):
        if self.o is None:
            self.o = o
        if h > self.h:
            self.h = h
        if lo < self.l:
            self.l = lo
        self.c = c
        self.v += v


class Market:
    """Prices, bars, market status and the instruments' descriptions."""

    # stand-in's own: bars go back to Monday 2005-01-03 at every resolution, so a 5,000-bar chart pages at any of them
    HISTORY_START = int(datetime(2005, 1, 3, tzinfo=timezone.utc).timestamp()) * 1000

    def __init__(self, seed, mode, day_start_min, clock):
        if mode not in ("open", "closed", "auto"):
            raise ValueError("market must be open, closed or auto")
        self.seed, self.clock = seed, clock
        self.instruments = [Instrument(r) for r in _TABLE]
        self.by_epic = {i.epic: i for i in self.instruments}
        self.off = int(day_start_min) * MIN_MS
        days, secs = _Lru(800), _Lru(6000)
        self.paths = {i.epic: Path("%d|%s" % (seed, i.epic), i.mid, i.vol, self.off, i.vol_minute * 1440, days, secs)
                      for i in self.instruments}
        self.lock = threading.RLock()
        self.start_mode = mode
        self.reset()

    def reset(self):
        with self.lock:
            self.mode, self.modes, self.segments = self.start_mode, {}, {}
            self.history = {}            # resolution -> (days back from now) or absent
            self._ha = _Lru(4000)

    def find(self, epic):
        return self.by_epic.get(epic) if isinstance(epic, str) else None

    # -- status
    def status(self, inst, now):
        """documented: https://open-api.capital.com/ (marketStatus TRADEABLE or CLOSED in the samples); which one is
        stand-in's own: the control, or (auto) the instrument's own openingHours on the stand-in's clock"""
        if inst.view_only:
            return "CLOSED"
        mode = self.modes.get(inst.epic, self.mode)
        if mode == "open":
            return "TRADEABLE"
        if mode == "closed":
            return "CLOSED"
        return "TRADEABLE" if inst.in_hours(now) else "CLOSED"

    def modes_of(self, inst):
        # documented: https://open-api.capital.com/ (marketModes ["REGULAR"], and ["VIEW_ONLY"] on the closed sample share)
        return ["VIEW_ONLY"] if inst.view_only else ["REGULAR"]

    # -- overrides
    def _factor(self, epic, t):
        f = 1.0
        for s in self.segments.get(epic, ()):
            if s.start > t:
                break
            if s.kind == "jump":
                f *= s.mult
            elif s.kind == "trend":
                f *= (1.0 + s.pct / 100.0) ** ((min(t, s.start + s.dur) - s.start) / 1000.0)
            elif s.kind == "hold":
                if s.end is None or t < s.end:
                    return f, s
                f *= s.release
        return f, None

    def _spread(self, epic, t):
        sp = self.by_epic[epic].spread
        for s in self.segments.get(epic, ()):
            if s.start > t:
                break
            if s.kind == "spread":
                sp = s.spread
        return sp

    def _first_seg(self, epic):
        segs = self.segments.get(epic)
        return segs[0].start if segs else None

    def quote(self, epic, t=None):
        """(bid, ask) as quoted at t: the bid on the instrument's tick, the ask the spread above it."""
        t = self.clock() if t is None else t
        inst = self.by_epic[epic]
        with self.lock:
            f, hold = self._factor(epic, t)
            if hold is not None:
                return rnd(hold.bid, inst.dp), rnd(hold.ask, inst.dp)
            sp = self._spread(epic, t)
        bid = rnd(self.paths[epic].mid(t) * f - sp / 2.0, inst.dp)
        return bid, rnd(bid + sp, inst.dp)

    def mid(self, epic, t=None):
        b, a = self.quote(epic, t)
        return (b + a) / 2.0

    def set_price(self, epic, now, bid=None, ask=None, mid=None, jump_pct=None, trend_pct_per_s=None, seconds=None, walk=None,
                  spread=None):
        inst = self.find(epic)
        if inst is None:
            raise KeyError("no such epic: %r" % (epic,))
        with self.lock:
            segs = self.segments.setdefault(epic, [])
            active = segs[-1] if segs and segs[-1].kind == "hold" and segs[-1].end is None else None
            if active is not None and (walk or bid is not None or mid is not None or jump_pct is not None or trend_pct_per_s is not None):
                active.end = now
                before, _ = self._factor_before(epic, active, now)
                active.release = active.mid / (self.paths[epic].mid(now) * before)
            if spread is not None:
                segs.append(_Seg("spread", now, spread=float(spread)))
            if bid is not None or ask is not None or mid is not None:
                if mid is not None:
                    sp = self._spread(epic, now)
                    bid, ask = float(mid) - sp / 2.0, float(mid) + sp / 2.0
                if bid is None or ask is None:
                    raise ValueError("give both bid and ask, or mid")
                bid, ask = rnd(bid, inst.dp), rnd(ask, inst.dp)
                if ask < bid:
                    raise ValueError("ask below bid")
                segs.append(_Seg("hold", now, bid=bid, ask=ask, mid=(bid + ask) / 2.0))
            elif jump_pct is not None:
                segs.append(_Seg("jump", now, mult=1.0 + float(jump_pct) / 100.0))
            elif trend_pct_per_s is not None:
                segs.append(_Seg("trend", now, pct=float(trend_pct_per_s), dur=int(float(seconds or 60) * 1000)))
        self._ha = _Lru(4000)                    # heikin-ashi opens read the bars an override may have changed
        b, a = self.quote(epic, now)
        return {"epic": epic, "bid": b, "ask": a}

    def _factor_before(self, epic, seg, t):
        f = 1.0
        for s in self.segments.get(epic, ()):
            if s is seg:
                break
            if s.kind == "jump":
                f *= s.mult
            elif s.kind == "trend":
                f *= (1.0 + s.pct / 100.0) ** ((min(t, s.start + s.dur) - s.start) / 1000.0)
            elif s.kind == "hold":
                f *= s.release
        return f, None

    # -- bars
    def history_start(self, res, now):
        days = self.history.get(res)
        return self.HISTORY_START if days is None else now - int(days * DAY_MS)

    def step_ms(self, res):
        return RESOLUTIONS[res] * MIN_MS

    def base_ms(self, res):
        # assumed, verify on demo: every bar is aligned to the stand-in's trading day (UTC midnight unless day_start_min
        # moves it), so HOUR_4 bars start at 00:00, 04:00, ... and a WEEK bar at the start of the trading day that holds
        # Monday 00:00 UTC (Monday 00:00 itself, or Sunday 22:00 when the day starts at 22:00)
        if res != "WEEK":
            return self.off
        monday = 4 * DAY_MS                                  # 1970-01-05, a Monday
        return ((monday - self.off) // DAY_MS) * DAY_MS + self.off

    def bar_start(self, res, t):
        step, base = self.step_ms(res), self.base_ms(res)
        return base + ((t - base) // step) * step

    def bar(self, epic, res, t0, now):
        """((bo, bh, bl, bc), (ao, ah, al, ac), volume) of the bar starting t0, rounded; cut at `now` while it is forming.
        Every value is read the way a quote is (the bid on the tick, the ask the spread above it), so bars and quotes agree."""
        inst, path, dp = self.by_epic[epic], self.paths[epic], self.by_epic[epic].dp
        end = t0 + self.step_ms(res)
        forming = now < end
        whole_end = max(t0, (now // MIN_MS) * MIN_MS) if forming else end      # the whole minutes inside the bar
        with self.lock:
            segs = list(self.segments.get(epic, ()))
        first = segs[0].start if segs else None
        clean_end = whole_end if first is None else min(whole_end, max(t0, (first // MIN_MS) * MIN_MS))
        bid, ask = _Agg(), _Agg()

        def add_mid(o, h, lo, c, v, f, sp):
            half = sp / 2.0
            bo, bh, bl, bc = rnd(o * f - half, dp), rnd(h * f - half, dp), rnd(lo * f - half, dp), rnd(c * f - half, dp)
            bid.add(bo, bh, bl, bc, v)
            ask.add(rnd(bo + sp, dp), rnd(bh + sp, dp), rnd(bl + sp, dp), rnd(bc + sp, dp))

        def add_samples(samples, close, v):
            bid.add(samples[0][0], max(x[0] for x in samples), min(x[0] for x in samples), close[0], v)
            ask.add(samples[0][1], max(x[1] for x in samples), min(x[1] for x in samples), close[1])

        if clean_end > t0:                         # untouched by any override: straight from the path
            m = self._mid_span(path, t0, clean_end)
            add_mid(m.o, m.h, m.l, m.c, m.v, 1.0, inst.spread)
        t = clean_end
        while t < whole_end:                       # after an override: minute by minute, second by second where it changes
            d = path.day_of(t)
            mi = (t - path.day_start(d)) // MIN_MS
            p, highs, lows, vols = path.minutes(d)
            kind = self._minute_kind(epic, segs, t)
            if kind is None:
                add_samples([self.quote(epic, t + s * SEC_MS) for s in range(60)], self.quote(epic, t + MIN_MS), vols[mi])
            elif kind[0] == "hold":
                hb, ha = kind[1], kind[2]
                bid.add(hb, hb, hb, hb, vols[mi])
                ask.add(ha, ha, ha, ha)
            else:
                add_mid(p[mi], highs[mi], lows[mi], p[mi + 1], vols[mi], kind[1], kind[2])
            t += MIN_MS
        if forming and now >= t0:                  # the minute that is running: its seconds so far, the last one now
            t = whole_end
            n = (now - t) // SEC_MS + 1
            samples = [self.quote(epic, t + s * SEC_MS) for s in range(n - 1)] + [self.quote(epic, now)]
            d = path.day_of(t)
            v = path.minutes(d)[3][(t - path.day_start(d)) // MIN_MS] * n // 60
            add_samples(samples, samples[-1], v)
        return (bid.o, bid.h, bid.l, bid.c), (ask.o, ask.h, ask.l, ask.c), bid.v

    def _minute_kind(self, epic, segs, tm):
        """How minute [tm, tm+60s) is priced: ("const", factor, spread) when no override changes inside it, ("hold",
        bid, ask) when a hold covers all of it, None when it has to be read second by second."""
        te = tm + MIN_MS
        for s in segs:
            if tm < s.start < te:
                return None
            if s.kind == "trend" and s.start < te and s.start + s.dur > tm:
                return None
            if s.kind == "hold" and s.end is not None and tm < s.end < te:
                return None
        with self.lock:
            f, hold = self._factor(epic, tm)
            sp = self._spread(epic, tm)
        if hold is not None:
            return ("hold", hold.bid, hold.ask)
        return ("const", f, sp)

    @staticmethod
    def _mid_span(path, a, b):
        """Mid (o, h, l, c, v) over whole minutes [a, b): whole days from the day summary, the rest from minute points."""
        agg, t = _Agg(), a
        while t < b:
            d = path.day_of(t)
            ds = path.day_start(d)
            if t == ds and b >= ds + DAY_MS:
                agg.add(*path.summary(d))
                t = ds + DAY_MS
                continue
            p, highs, lows, vols = path.minutes(d)
            m0 = (t - ds) // MIN_MS
            m1 = min(1440, (b - ds) // MIN_MS)
            agg.add(p[m0], max(highs[m0:m1]), min(lows[m0:m1]), p[m1], sum(vols[m0:m1]))
            t = ds + m1 * MIN_MS
        return agg

    def bars(self, epic, res, t_from, t_to, limit, prefer, now, forming=True):
        """The bar starts in [t_from, t_to] that exist (history start .. now), at most `limit`, `prefer` oldest|newest,
        and the bars. Returns (starts_total, [(t0, bid, ask, vol)])."""
        step, base = self.step_ms(res), self.base_ms(res)
        lo = max(t_from if t_from is not None else -10 ** 18, self.history_start(res, now))
        last_ok = now if forming else now - step
        hi = min(t_to if t_to is not None else 10 ** 18, last_ok)
        first = base + -((base - lo) // step) * step          # ceil to the grid
        last = base + ((hi - base) // step) * step            # floor to the grid
        if last < first:
            return 0, []
        total = (last - first) // step + 1
        if total > limit:
            if prefer == "newest":
                first = last - (limit - 1) * step
            else:
                last = first + (limit - 1) * step
        out = []
        t = first
        while t <= last:
            b, a, v = self.bar(epic, res, t, now)
            out.append((t, b, a, v))
            t += step
        return total, out

    def ohlc(self, epic, res, kind, t0, now):
        """The bid bar of an ohlc.event: classic, or heikin-ashi from the bars before it."""
        inst = self.by_epic[epic]
        (o, h, lo, c), _, _ = self.bar(epic, res, t0, now)
        if kind != "heikin-ashi":
            return o, h, lo, c
        ha_open = self._ha.get((epic, res, t0), lambda: self._ha_open(epic, res, t0, now))
        ha_close = (o + h + lo + c) / 4.0
        return (rnd(ha_open, inst.dp), rnd(max(h, ha_open, ha_close), inst.dp), rnd(min(lo, ha_open, ha_close), inst.dp),
                rnd(ha_close, inst.dp))

    def _ha_open(self, epic, res, t0, now):
        # stand-in's own: heikin-ashi seeded 30 bars back, the usual recurrence after that
        step = self.step_ms(res)
        (o, h, lo, c), _, _ = self.bar(epic, res, t0 - 30 * step, now)
        ha_o, ha_c = (o + c) / 2.0, (o + h + lo + c) / 4.0
        for k in range(29, 0, -1):
            (o, h, lo, c), _, _ = self.bar(epic, res, t0 - k * step, now)
            ha_o, ha_c = (ha_o + ha_c) / 2.0, (o + h + lo + c) / 4.0
        return (ha_o + ha_c) / 2.0

    # -- descriptions
    def fields(self, inst, now, leverage):
        """Every market field any endpoint shows, for this instrument now."""
        bid, ask = self.quote(inst.epic, now)
        path = self.paths[inst.epic]
        d = path.day_of(now)
        (_, dh, dl, _), _, _ = self.bar(inst.epic, "DAY", path.day_start(d), now)
        (_, _, _, prev), _, _ = self.bar(inst.epic, "DAY", path.day_start(d - 1), now)
        net = rnd(bid - prev, inst.dp)
        when = now - now % SEC_MS
        return {
            "instrumentName": inst.name, "expiry": "-", "marketStatus": self.status(inst, now), "epic": inst.epic,
            "symbol": inst.symbol, "instrumentType": inst.type, "lotSize": inst.lot, "high": dh, "low": dl,
            "percentageChange": rnd(net / prev * 100.0, 4) if prev else 0.0, "netChange": net, "bid": bid, "offer": ask,
            "updateTime": self.local(when), "updateTimeUTC": stamp(when), "delayTime": 0, "streamingPricesAvailable": True,
            "scalingFactor": 1, "marketModes": self.modes_of(inst), "pipPosition": max(0, inst.dp - 1), "tickSize": rnd(inst.tick, inst.dp),
            "_margin": 100.0 / leverage,
        }

    local_ms = 3 * 3600 * 1000                   # replaced by FakeCapitalCom's local_offset_h

    def local(self, ms, millis=True):
        """documented: https://open-api.capital.com/ (every time is given twice: a local one such as createdDate and
        snapshotTime, 1 to 3 hours from the UTC one in the samples). Its offset is the session's timezoneOffset here
        (stand-in's own)."""
        return stamp(ms + self.local_ms, millis)


# row layouts: which market fields each endpoint shows, in the order the documented samples show them
# documented: https://open-api.capital.com/ (GET /markets sample)
ROW_LIST = ("delayTime", "epic", "symbol", "netChange", "lotSize", "expiry", "instrumentType", "instrumentName", "high", "low",
            "percentageChange", "updateTime", "updateTimeUTC", "bid", "offer", "streamingPricesAvailable", "marketStatus",
            "scalingFactor", "marketModes", "pipPosition", "tickSize")
# documented: https://open-api.capital.com/ (the `market` of GET /positions and the `marketData` of GET /workingorders)
ROW_DEAL = ("instrumentName", "expiry", "marketStatus", "epic", "symbol", "instrumentType", "lotSize", "high", "low",
            "percentageChange", "netChange", "bid", "offer", "updateTime", "updateTimeUTC", "delayTime", "streamingPricesAvailable",
            "scalingFactor", "marketModes")
# documented: https://open-api.capital.com/ (GET /watchlists/{watchlistId} sample)
ROW_WATCH = ("instrumentName", "expiry", "epic", "symbol", "instrumentType", "marketStatus", "lotSize", "high", "low",
             "percentageChange", "netChange", "bid", "offer", "updateTime", "updateTimeUTC", "delayTime", "streamingPricesAvailable",
             "scalingFactor", "marketModes", "pipPosition", "tickSize")
# documented: https://github.com/capital-com-sv/capital-api-postman ("Success: List of markets in category with limit = 2")
ROW_NAV = ("delayTime", "epic", "netChange", "lotSize", "expiry", "instrumentType", "instrumentName", "high", "low",
           "percentageChange", "updateTime", "updateTimeUTC", "bid", "offer", "streamingPricesAvailable", "marketStatus", "scalingFactor")


# -- the broker --------------------------------------------------------------------------------------------------------

class Deal:
    """An open position: one deal, one dealId. documented: https://open-api.capital.com/ (GET /positions)"""
    __slots__ = ("deal_id", "order_id", "epic", "direction", "size", "level", "created", "leverage", "gsl", "stop", "profit",
                 "trailing", "trail_dist", "next_swap")


class Order:
    """A working order. documented: https://open-api.capital.com/ (GET /workingorders)"""
    __slots__ = ("deal_id", "epic", "direction", "size", "level", "kind", "gtd", "created", "leverage", "gsl", "trailing",
                 "stop", "profit", "parts", "next_part_at", "rest", "interval")
    # stop/profit: ("level", x) | ("distance", x) | None


class Account:
    def __init__(self, account_id, name, currency, symbol, cash, preferred):
        self.id, self.name, self.currency, self.symbol, self.preferred = account_id, name, currency, symbol, preferred
        self.cash = money(cash)
        self.status = "ENABLED"                           # documented: https://open-api.capital.com/ (GET /accounts sample)
        self.account_type = "CFD"                         # documented: https://open-api.capital.com/ (accountType "CFD")
        self.deals, self.orders = collections.OrderedDict(), collections.OrderedDict()
        self.activities, self.transactions = [], []       # (ms, row)
        self.hedging = False                              # documented: https://open-api.capital.com/ (hedgingMode false)
        self.leverage = {k: cur for k, cur, _ in DEFAULT_LEVERAGE}
        self.topups_s, self.topups_day = collections.deque(), collections.deque()


class Session:
    __slots__ = ("cst", "token", "account_id", "created", "last", "alive")

    def __init__(self, cst, token, account_id, now):
        self.cst, self.token, self.account_id, self.created, self.last, self.alive = cst, token, account_id, now, now, True


class _Window:
    """At most `limit` events in any `span_ms`."""

    def __init__(self, limit, span_ms):
        self.limit, self.span, self.times = limit, span_ms, collections.deque()

    def ok(self, now):
        while self.times and self.times[0] <= now - self.span:
            self.times.popleft()
        return len(self.times) < self.limit

    def add(self, now):
        self.times.append(now)


_CURRENCY_SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€"}
# how the stand-in converts between currencies (the account's and an instrument's): at its own FX mids, stand-in's own
_FX_USD = {"EUR": ("EURUSD", False), "GBP": ("GBPUSD", False), "AUD": ("AUDUSD", False), "NZD": ("NZDUSD", False),
           "JPY": ("USDJPY", True), "CAD": ("USDCAD", True), "CHF": ("USDCHF", True)}


class Broker:
    """The login, its accounts, the sessions, the orders and the order engine. No HTTP here."""

    def __init__(self, market, seed, identifier, password, api_key, balances, confirm_delay, session_ttl, limits, clock_offset_ms=0):
        self.market, self.seed = market, seed
        self.identifier, self.password, self.api_key = identifier, password, api_key
        self.balances, self.start_confirm_delay, self.session_ttl, self.limits = balances, confirm_delay, session_ttl, limits
        self.lock = threading.RLock()
        self.offset_ms = clock_offset_ms
        self.market.clock = self.now
        self.reset()

    def reset(self):
        with self.lock:
            rng = random.Random(self.seed)
            self._ids = rng
            self._prefix = rng.getrandbits(32)
            self._counter = 0x80560000 + rng.getrandbits(12)
            self._refs = 10000000 + rng.getrandbits(20)
            self.client_id = "%08d" % (33000000 + self.seed % 1000000)
            self.accounts = collections.OrderedDict()
            for k, (cur, cash) in enumerate(self.balances):
                aid = "%017d" % (33000000000000001 + k + (self.seed % 1000) * 100)
                self.accounts[aid] = Account(aid, cur, cur, _CURRENCY_SYMBOL.get(cur, cur), cash, k == 0)
            self.sessions = {}
            self.confirms = {}
            self.options = {k: v[0] for k, v in OPTIONS.items()}
            self.confirm_delay = self.start_confirm_delay
            self.key_revoked = False
            self.no_account = False                      # see login()
            self.next_parts = None                       # (weights, rest, interval)
            self.fill_rules = []                         # (epic or None, size Decimal, weights)
            self.rejects = []                            # {"kind", "reason", "count"}
            self.watchlists = collections.OrderedDict()
            self._watch_ids = 123456
            lim = self.limits
            self.w_user = _Window(lim["rate_per_s"], 1000)
            self.w_login = _Window(lim["session_per_s"], 1000)
            self.w_spacing = _Window(1, int(lim["order_spacing"] * 1000)) if lim["order_spacing"] > 0 else None
            self.w_hour = _Window(lim["orders_per_hour"], 3600 * 1000)

    # -- clock and ids
    def now(self):
        return int(time.time() * 1000) + self.offset_ms

    def _deal_id(self):
        # documented: https://open-api.capital.com/ (dealIds look like 006011e7-0001-54c4-0000-000080560043)
        self._counter += 1
        return "%08x-0001-54c4-0000-%012x" % (self._prefix, self._counter)

    def _uuid(self):
        return str(uuid.UUID(int=self._ids.getrandbits(128), version=4))

    def _reference(self):
        self._refs += 1
        return str(self._refs)

    # -- sessions
    def session_for(self, cst, token, touch=True):
        """The live session these tokens name, or None. documented: https://open-api.capital.com/ ("Both tokens are
        valid for 10 minutes after the last use")"""
        if not isinstance(cst, str) or not isinstance(token, str):
            return None
        s = self.sessions.get(cst)
        now = self.now()
        if s is None or not s.alive or s.token != token:
            return None
        if now - s.last > self.session_ttl * 1000:
            s.alive = False
            return None
        if touch:
            s.last = now
        return s

    def login(self, api_key, body):
        """POST /session: (status, body, headers)."""
        now = self.now()
        if not api_key:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: API key missing": 400 error.null.api.key)
            raise CapError(400, "error.null.api.key")
        if not self.w_login.ok(now):
            # documented: https://open-api.capital.com/ ("POST /session: 1 request per second per API key") and
            # https://github.com/capital-com-sv/capital-api-postman ("Error: Request limit exceeded": 429 error.too-many.requests)
            raise CapError(429, "error.too-many.requests")
        self.w_login.add(now)
        if api_key != self.api_key or self.key_revoked:
            # stand-in's own code; the 401 for a revoked or expired key is documented:
            # https://help.capitalccuk.com/hc/en-us/articles/36673475487250 ("401 Unauthorized: API key expired or revoked")
            raise CapError(401, "error.invalid.api.key")
        if not isinstance(body, dict):
            raise CapError(400, "error.invalid.details")       # stand-in's own (status); the code is the documented one below
        if body.get("encryptedPassword") is True:
            # stand-in's own: the encrypted login is not modelled (it needs RSA); such a login is refused like a wrong password
            raise CapError(401, "error.invalid.details")
        if body.get("identifier") != self.identifier or body.get("password") != self.password:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Invalid credentials": 401 error.invalid.details)
            raise CapError(401, "error.invalid.details")
        if self.no_account:
            # seen on Capital.com's demo host, 2026-11-15, with a real login: POST /session answered 401 {"errorCode":
            # "error.null.accountId"}, a code the documentation does not list. Read as: this login has no account on this
            # host (its key made while the platform was on the live account, or a login with no demo account). Played for
            # a login flagged so (set_no_account), after the key and the password have been checked.
            raise CapError(401, "error.null.accountId")
        current = next(a for a in self.accounts.values() if a.preferred)   # documented: currentAccountId is the preferred one (sample)
        cst, token = secrets.token_urlsafe(18), secrets.token_urlsafe(24)
        self.sessions[cst] = Session(cst, token, current.id, now)
        # documented: https://open-api.capital.com/ (the POST /session response sample, field for field)
        out = {"accountType": current.account_type, "accountInfo": self.figures(current, now), "currencyIsoCode": current.currency,
               "currencySymbol": current.symbol, "currentAccountId": current.id, "streamingHost": self.streaming_host,
               "accounts": [{"accountId": a.id, "accountName": a.name, "preferred": a.preferred, "accountType": a.account_type,
                             "currency": a.currency, "symbol": a.symbol, "balance": self.figures(a, now)} for a in self.accounts.values()],
               "clientId": self.client_id, "timezoneOffset": self.local_offset_h,
               "hasActiveDemoAccounts": True, "hasActiveLiveAccounts": False,   # stand-in's own: a demo-only login
               "trailingStopsEnabled": self.options["trailing_enabled"]}
        # documented: https://open-api.capital.com/ (CST and X-SECURITY-TOKEN come back as response headers)
        return 200, out, {"CST": cst, "X-SECURITY-TOKEN": token}

    streaming_host = "ws://127.0.0.1:0/"
    local_offset_h = 3

    def session_view(self, sess):
        acct = self.accounts[sess.account_id]
        # documented: https://open-api.capital.com/ (GET /session sample)
        return {"clientId": self.client_id, "accountId": acct.id, "timezoneOffset": self.local_offset_h, "locale": "en",
                "currency": acct.currency, "streamEndpoint": self.streaming_host}

    def switch(self, sess, body):
        aid = body.get("accountId") if isinstance(body, dict) else None
        if aid not in self.accounts:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Invalid accountId": 400 error.invalid.accountId)
            raise CapError(400, "error.invalid.accountId")
        if aid == sess.account_id:
            # assumed, verify on demo: not in the documentation; Capital.com is reported to refuse a switch to the
            # account already in use. An adapter that switches only when it must is right either way.
            raise CapError(400, "error.not-different.accountId")
        sess.account_id = aid
        # documented: https://open-api.capital.com/ (PUT /session response sample)
        return {"trailingStopsEnabled": self.options["trailing_enabled"], "dealingEnabled": True,
                "hasActiveDemoAccounts": True, "hasActiveLiveAccounts": False}

    def logout(self, sess):
        sess.alive = False
        return {"status": "SUCCESS"}                     # documented: https://open-api.capital.com/ (DELETE /session)

    def expire_all(self):
        with self.lock:
            for s in self.sessions.values():
                s.alive = False

    # -- money
    def usd_per(self, cur, now):
        if cur == "USD":
            return 1.0
        if cur in _FX_USD:
            epic, inverse = _FX_USD[cur]
            m = self.market.mid(epic, now)
            return 1.0 / m if inverse else m
        return 0.74                                      # stand-in's own: SGD, the only other currency an instrument has

    def conv(self, cur_from, cur_to, now):
        return 1.0 if cur_from == cur_to else self.usd_per(cur_from, now) / self.usd_per(cur_to, now)

    def _close_price(self, inst, direction, now):
        bid, ask = self.market.quote(inst.epic, now)
        return bid if direction == "BUY" else ask

    def upl(self, acct, deal, now, currency=None):
        """A deal's profit or loss now, in `currency` (the account's unless told), at the price it would close at."""
        inst = self.market.by_epic[deal.epic]
        px = self._close_price(inst, deal.direction, now)
        diff = (px - deal.level) if deal.direction == "BUY" else (deal.level - px)
        pnl = diff * float(deal.size) * inst.lot
        return pnl * self.conv(inst.currency, currency or acct.currency, now)

    def margin_of(self, acct, epic, size, price, now, leverage=None):
        inst = self.market.by_epic[epic]
        lev = leverage or acct.leverage[inst.type]
        return float(size) * inst.lot * price / lev * self.conv(inst.currency, acct.currency, now)

    def margin_used(self, acct, now):
        """assumed, verify on demo: a deal holds size x price / leverage. documented:
        https://capital.com/en-gb/analysis/what-is-hedging-mode-and-how-to-use-it-in-trading ("Margin applies to the
        larger side of your hedge"), so in hedging mode each market counts its larger side only."""
        sides = collections.defaultdict(lambda: [0.0, 0.0])
        total = 0.0
        for d in acct.deals.values():
            m = self.margin_of(acct, d.epic, d.size, self.market.mid(d.epic, now), now, d.leverage)
            if acct.hedging:
                sides[d.epic][0 if d.direction == "BUY" else 1] += m
            else:
                total += m
        total += sum(max(a, b) for a, b in sides.values())
        if self.options["wo_reserves_margin"]:
            total += sum(self.margin_of(acct, o.epic, o.size, o.level, now, o.leverage) for o in acct.orders.values())
        return total

    def figures(self, acct, now):
        """documented: https://open-api.capital.com/ (balance {balance, deposit, profitLoss, available}; the sample has
        balance = deposit + profitLoss). assumed, verify on demo: deposit is the cash, profitLoss the open deals' P/L,
        available what is left after margin."""
        upl = sum(self.upl(acct, d, now) for d in acct.deals.values())
        balance = acct.cash + Decimal(repr(upl))
        available = balance - Decimal(repr(self.margin_used(acct, now)))
        return {"balance": num(money(balance)), "deposit": num(acct.cash), "profitLoss": num(money(Decimal(repr(upl)))),
                "available": num(money(available))}

    # -- records
    def _activity(self, acct, kind, source, status, epic, deal_id, now, **details):
        inst = self.market.by_epic.get(epic)
        row = {"date": self.market.local(now), "dateUTC": stamp(now), "epic": epic, "dealId": deal_id, "source": source,
               "type": kind, "status": status}
        if details:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Filter list by dealId with enabled
            # detailed view": details {dealReference, marketName, currency, size, direction, level, guaranteedStop})
            row["details"] = {"dealReference": details.get("reference", "p_" + deal_id), "marketName": inst.name if inst else epic,
                              "currency": details.get("currency", inst.currency if inst else acct.currency),
                              "size": details.get("size", 0), "direction": details.get("direction"), "level": details.get("level", 0),
                              "guaranteedStop": details.get("gsl", False)}
        acct.activities.append((now, row))

    def _transaction(self, acct, kind, note, name, amount, now):
        # documented: https://open-api.capital.com/ (GET /history/transactions sample: `dateUtc` spelt so, `size` a string)
        acct.transactions.append((now, {"date": self.market.local(now), "dateUtc": stamp(now), "instrumentName": name,
                                        "transactionType": kind, "note": note, "reference": self._reference(),
                                        "size": repr(float(amount)), "currency": acct.currency, "status": "PROCESSED"}))

    def _confirm(self, ref, body, now):
        body["_visible"] = now + int(self.confirm_delay * 1000)
        self.confirms[ref] = body

    def confirm(self, ref):
        c = self.confirms.get(ref)
        if c is None or self.now() < c["_visible"]:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Deal confirmation not found": 404
            # error.not-found.dealReference). That a fresh one may not be there yet is assumed, verify on demo, from
            # Capital.com's own client polling it for up to 15 s (https://github.com/capital-com-sv/capital-mcp)
            raise CapError(404, "error.not-found.dealReference")
        return {k: v for k, v in c.items() if not k.startswith("_")}

    # -- the order checks shared by positions and working orders
    def _basics(self, body):
        if not isinstance(body, dict):
            raise CapError(400, "error.invalid.request")            # stand-in's own
        direction = body.get("direction")
        if direction not in ("BUY", "SELL"):
            # documented: https://open-api.capital.com/ ('Must be "BUY" or "SELL"'); the code is the stand-in's own
            raise CapError(400, "error.invalid.direction")
        epic = body.get("epic")
        if not epic:
            raise CapError(400, "error.null.epic")                 # documented code (Postman, GET /markets); its use here stand-in's own
        inst = self.market.find(epic)
        if inst is None:
            # documented code: https://github.com/capital-com-sv/capital-api-postman ("Error: Market not found": 404
            # error.not-found.epic); that an order for an unknown epic answers it is assumed, verify on demo
            raise CapError(404, "error.not-found.epic")
        try:
            size = dec(body.get("size"))
        except ValueError:
            raise CapError(400, "error.invalid.size")              # stand-in's own
        if size <= 0:
            raise CapError(400, "error.invalid.size")              # stand-in's own
        # stand-in's own codes, in the documented "error.invalid.stoploss.minvalue: 17.146" pattern
        if size < inst.min_deal:
            raise CapError(400, "error.invalid.size.minvalue: %s" % _plain(inst.min_deal))
        if size > inst.max_deal:
            raise CapError(400, "error.invalid.size.maxvalue: %s" % _plain(inst.max_deal))
        if (size / inst.step) != (size / inst.step).to_integral_value():
            raise CapError(400, "error.invalid.size.increment: %s" % _plain(inst.step))
        if inst.view_only:
            raise CapError(400, "error.invalid.market.viewonly")   # stand-in's own (the documented VIEW_ONLY market)
        return inst, direction, size

    def _flags(self, acct, inst, body):
        gsl, trailing = body.get("guaranteedStop", False), body.get("trailingStop", False)
        if not isinstance(gsl, bool) or not isinstance(trailing, bool):
            raise CapError(400, "error.invalid.request")           # stand-in's own
        if gsl and trailing:
            # documented: https://open-api.capital.com/ (guaranteedStop "Cannot be set if trailingStop is true"); code stand-in's own
            raise CapError(400, "error.invalid.guaranteedstop")
        if gsl and acct.hedging:
            # documented: https://open-api.capital.com/ ("Cannot be set if hedgingMode is true"); code stand-in's own
            raise CapError(400, "error.invalid.guaranteedstop")
        if gsl and not inst.gsl_allowed:
            raise CapError(400, "error.invalid.guaranteedstop")    # documented field guaranteedStopAllowed; code stand-in's own
        if gsl and not any(body.get(k) is not None for k in ("stopLevel", "stopDistance", "stopAmount")):
            # documented: https://open-api.capital.com/ ("If guaranteedStop equals true, then set stopLevel, stopDistance or stopAmount")
            raise CapError(400, "error.invalid.guaranteedstop")
        if trailing:
            if body.get("stopDistance") is None:
                # documented: https://open-api.capital.com/ ("If trailingStop equals true, then set stopDistance")
                raise CapError(400, "error.invalid.trailingstop")
            if not self.options["trailing_enabled"] or inst.trailing != "AVAILABLE":
                # documented fields: trailingStopsEnabled (session) and trailingStopsPreference (market); code stand-in's own
                raise CapError(400, "error.invalid.trailingstop")
        return gsl, trailing

    @staticmethod
    def _leg(body, kind):
        """('level', x) | ('distance', x) | ('amount', x) | None for kind 'stop' or 'profit'. stand-in's own: when several
        are given, the level wins over the distance and the distance over the amount."""
        for form in ("Level", "Distance", "Amount"):
            v = body.get(kind + form)
            if v is not None:
                try:
                    x = float(dec(v))
                except ValueError:
                    raise CapError(400, "error.invalid.%s" % ("stoploss" if kind == "stop" else "takeprofit"))   # stand-in's own
                if form != "Level" and x <= 0:
                    raise CapError(400, "error.invalid.%s" % ("stoploss" if kind == "stop" else "takeprofit"))   # stand-in's own
                return (form.lower(), x)
        return None

    def _leg_level(self, acct, inst, direction, leg, anchor, size, kind, now):
        """A leg as a price level, from `anchor` (the deal's or the order's level). assumed, verify on demo: a distance
        is in price units from the anchor; an amount is the account-currency loss or gain at that level."""
        if leg is None:
            return None
        form, x = leg
        if form == "level":
            return rnd(x, inst.dp)
        if form == "amount":
            x = x / (float(size) * inst.lot * self.conv(inst.currency, acct.currency, now))
        sign = -1.0 if (kind == "stop") == (direction == "BUY") else 1.0
        return rnd(anchor + sign * x, inst.dp)

    def _check_levels(self, inst, direction, ref, stop, profit, gsl=False):
        """The documented refusals of a stop or profit level too near or too far.
        documented: https://github.com/capital-com-sv/capital-api-postman (400 "error.invalid.stoploss.minvalue: 17.146",
        "error.invalid.takeprofit.maxvalue: 31.817", "error.invalid.stoploss.maxvalue: 24.302", "error.invalid.takeprofit.
        minvalue: 24.302"): the bound is the nearest allowed LEVEL. assumed, verify on demo: the bounds are the
        dealingRules' min/maxStopOrProfitDistance from `ref`."""
        dmin, dmax = inst.distance(inst.stop_min, ref), inst.distance(inst.stop_max, ref)
        if gsl:
            dmin = max(dmin, inst.distance(inst.gsl_min, ref))
        tol = inst.tick / 2.0
        below = (rnd(ref - dmax, inst.dp), rnd(ref - dmin, inst.dp))
        above = (rnd(ref + dmin, inst.dp), rnd(ref + dmax, inst.dp))
        for kind, level in (("stoploss", stop), ("takeprofit", profit)):
            if level is None:
                continue
            lo, hi = below if (kind == "stoploss") == (direction == "BUY") else above
            if level < lo - tol:
                raise CapError(400, "error.invalid.%s.minvalue: %s" % (kind, _fmt(lo, inst.dp)))
            if level > hi + tol:
                raise CapError(400, "error.invalid.%s.maxvalue: %s" % (kind, _fmt(hi, inst.dp)))

    def _take_reject(self, kind):
        for r in self.rejects:
            if r["kind"] == kind and r["count"] > 0:
                r["count"] -= 1
                return r["reason"]
        return None

    def _parts(self, inst, size):
        """The sizes an execution comes in: the next-fill control, then a size rule, else one."""
        weights, rest, interval = None, "fill", 0.0
        if self.next_parts is not None:
            weights, rest, interval = self.next_parts
            self.next_parts = None
        else:
            for epic, rule_size, w in self.fill_rules:
                if (epic is None or epic == inst.epic) and rule_size == size:
                    weights = w
                    break
        if not weights or len(weights) < 2:
            return [size], rest, interval
        total = sum(weights)
        out, left = [], size
        for w in weights[:-1]:
            part = (size * Decimal(repr(w)) / Decimal(repr(total)) / inst.step).to_integral_value(rounding="ROUND_DOWN") * inst.step
            part = max(inst.step, min(part, left - inst.step))
            if part <= 0 or left - part <= 0:
                break
            out.append(part)
            left -= part
        out.append(left)
        return out, rest, interval

    def _new_deal(self, acct, inst, direction, size, level, order_id, stop, profit, gsl, trailing, trail_dist, source, now):
        d = Deal()
        d.deal_id, d.order_id, d.epic, d.direction, d.size, d.level = self._deal_id(), order_id, inst.epic, direction, size, level
        d.created, d.leverage, d.gsl, d.stop, d.profit = now, acct.leverage[inst.type], gsl, stop, profit
        d.trailing, d.trail_dist = trailing, trail_dist
        d.next_swap = self._next_swap(now)
        acct.deals[d.deal_id] = d
        self._activity(acct, "POSITION", source, "ACCEPTED", inst.epic, d.deal_id, now, size=num(size), direction=direction,
                       level=level, gsl=gsl)
        return d

    def made_on_platform(self, acct, epic, direction, size, open_level, now, close_level=None, source="USER"):
        """A deal opened (and, when close_level is given, closed) directly on
        Capital.com's platform, not through this API: the activity it leaves
        (an open POSITION row, and a close POSITION row carrying the closing
        direction and level), and, while open, the position itself. For the
        stand-in's own tests of the activity history that fills.list reads.
        Returns the dealId."""
        inst = self.market.by_epic[epic]
        d = self._new_deal(acct, inst, direction, money(size), open_level, None, None, None, False, False, None, source, now)
        did = d.deal_id
        if close_level is not None:
            self._close_qty(acct, d, money(size), close_level, source, now + 1000)
        return did

    @staticmethod
    def _next_swap(now):
        # documented: https://open-api.capital.com/ (overnightFee.swapChargeTimestamp 1707948000000 = 22:00 UTC, and
        # swapChargeInterval 1440 minutes); every day at 22:00 UTC is the stand-in's reading of that
        base = (now // DAY_MS) * DAY_MS + 22 * 3600 * 1000
        return base if base > now else base + DAY_MS

    def _close_qty(self, acct, deal, qty, price, source, now):
        """Close `qty` of a deal at `price`: the money, the activity and the transaction. Returns the P/L."""
        inst = self.market.by_epic[deal.epic]
        diff = (price - deal.level) if deal.direction == "BUY" else (deal.level - price)
        pnl = money(Decimal(repr(diff * float(qty) * inst.lot * self.conv(inst.currency, acct.currency, now))))
        acct.cash += pnl
        deal.size -= qty
        closing = "SELL" if deal.direction == "BUY" else "BUY"
        # documented: https://github.com/capital-com-sv/capital-api-postman (a close is a POSITION row whose details carry
        # the closing direction and level; its source says who closed it: USER, SL, TP, CLOSE_OUT, DEALER, SYSTEM)
        self._activity(acct, "POSITION", source, "ACCEPTED", deal.epic, deal.deal_id, now, size=num(qty), direction=closing,
                       level=price, gsl=deal.gsl)
        # documented: https://open-api.capital.com/ (transactions: TRADE, "Trade closed", the result in `size`)
        self._transaction(acct, "TRADE", "Trade closed", deal.epic, pnl, now)
        if deal.size <= 0:
            del acct.deals[deal.deal_id]
        return pnl

    def _execute(self, acct, inst, direction, size, price, order_id, legs, gsl, trailing, trail_dist, source, now, parts=None):
        """An execution at market: in standard mode it first offsets the opposite deals, oldest first; the rest opens
        new deals (one per part). Returns (affected, opened)."""
        affected, opened, left = [], [], size
        if not acct.hedging:
            # documented: https://capital.com/en-gb/analysis/what-is-hedging-mode-and-how-to-use-it-in-trading (with
            # hedging mode off, an opposite position "automatically offsets or closes your existing trade").
            # assumed, verify on demo: oldest deal first; a partial offset shrinks that deal
            for d in [x for x in acct.deals.values() if x.epic == inst.epic and x.direction != direction]:
                if left <= 0:
                    break
                take = min(left, d.size)
                self._close_qty(acct, d, take, price, source, now)
                # assumed, verify on demo: the status words of affectedDeals beyond the documented OPENED
                affected.append({"dealId": d.deal_id, "status": "FULLY_CLOSED" if d.deal_id not in acct.deals else "PARTIALLY_CLOSED"})
                left -= take
        if left > 0:
            sizes = parts if parts else [left]
            if sum(sizes) != left:
                sizes = [left]
            for k, part in enumerate(sizes):
                # assumed, verify on demo: each further part of a split fill is one tick worse
                px = rnd(price + (k * inst.tick if direction == "BUY" else -k * inst.tick), inst.dp)
                stop = self._leg_level(acct, inst, direction, legs[0], px, part, "stop", now)
                profit = self._leg_level(acct, inst, direction, legs[1], px, part, "profit", now)
                d = self._new_deal(acct, inst, direction, part, px, order_id, stop, profit, gsl, trailing, trail_dist, source, now)
                opened.append(d)
                affected.append({"dealId": d.deal_id, "status": "OPENED"})   # documented: https://open-api.capital.com/
        return affected, opened

    # -- positions
    def open_position(self, acct, body):
        """POST /positions. documented: https://open-api.capital.com/ (answers {"dealReference": "o_..."}; "a successful
        response does not always mean that the position has been successfully opened": GET /confirms/{dealReference})"""
        now = self.now()
        inst, direction, size = self._basics(body)
        gsl, trailing = self._flags(acct, inst, body)
        stop_leg, profit_leg = self._leg(body, "stop"), self._leg(body, "profit")
        bid, ask = self.market.quote(inst.epic, now)
        # documented: https://github.com/capital-com-sv/capital-mcp ("All orders placed through the Public API are
        # executed as market orders"): a buy at the ask, a sell at the bid
        price = ask if direction == "BUY" else bid
        ref_close = bid if direction == "BUY" else ask
        stop = self._leg_level(acct, inst, direction, stop_leg, price, size, "stop", now)
        profit = self._leg_level(acct, inst, direction, profit_leg, price, size, "profit", now)
        self._check_levels(inst, direction, ref_close, stop, profit, gsl)
        trail_dist = float(dec(body["stopDistance"])) if trailing else None
        ref = "o_" + self._uuid()                         # documented: https://open-api.capital.com/ ("o_98c0de50-...")
        order_id = self._deal_id()
        confirm = {"date": stamp(now), "status": "OPEN", "dealStatus": "ACCEPTED", "epic": inst.epic, "dealReference": ref,
                   "dealId": order_id, "affectedDeals": [], "level": price, "size": num(size), "direction": direction,
                   "guaranteedStop": gsl, "trailingStop": trailing}
        reason = self._take_reject("positions")
        if reason is None and self.market.status(inst, now) != "TRADEABLE":
            reason = "MARKET_CLOSED"                      # stand-in's own reason; that it is a REJECTED confirm: assumed, verify on demo
        if reason is None:
            offset = self._offset_size(acct, inst, direction, size)
            need = self.margin_of(acct, inst.epic, size - offset, price, now)
            freed = self.margin_of(acct, inst.epic, offset, price, now)
            if need > self.figures(acct, now)["available"] + freed + 1e-9:
                reason = "INSUFFICIENT_FUNDS"             # stand-in's own reason; assumed, verify on demo
        if reason is not None:
            self._reject(acct, confirm, reason, inst, now)
        else:
            parts, rest, _ = self._parts(inst, size)
            if rest == "cancel" and len(parts) > 1:
                parts = parts[:1]                         # the rest is never filled
            filled = sum(parts)
            affected, opened = self._execute(acct, inst, direction, filled, price, order_id, (stop_leg, profit_leg), gsl, trailing,
                                             trail_dist, "USER", now, parts if len(parts) > 1 else None)
            confirm["affectedDeals"] = affected
            confirm["size"] = num(filled)
            if opened:
                # assumed, verify on demo: the confirmation's level is the size-weighted level of what opened
                confirm["level"] = rnd(sum(d.level * float(d.size) for d in opened) / float(sum(d.size for d in opened)), inst.dp)
            else:
                confirm["status"] = "CLOSED"
        self._confirm(ref, confirm, now)
        if confirm["dealStatus"] == "ACCEPTED":
            for a in confirm["affectedDeals"]:
                if a["status"] == "OPENED":
                    self._confirm("p_" + a["dealId"], dict(confirm), now)
        return {"dealReference": ref}

    def _offset_size(self, acct, inst, direction, size):
        if acct.hedging:
            return ZERO
        held = sum((d.size for d in acct.deals.values() if d.epic == inst.epic and d.direction != direction), ZERO)
        return min(size, held)

    def _reject(self, acct, confirm, reason, inst, now):
        # assumed, verify on demo: a refused deal is dealStatus REJECTED with nothing in affectedDeals; the `reason`
        # field is the stand-in's own (the documentation names no reason field); `status` is then null
        confirm.update({"status": None, "dealStatus": "REJECTED", "affectedDeals": [], "reason": reason})
        self._activity(acct, "POSITION", "USER", "REJECTED", inst.epic, confirm["dealId"], now, size=confirm["size"],
                       direction=confirm["direction"], level=confirm["level"], reference=confirm["dealReference"])

    def position_view(self, acct, d, now):
        inst = self.market.by_epic[d.epic]
        cur = acct.currency if self.options["upl_currency"] == "account" else inst.currency
        # documented: https://open-api.capital.com/ (GET /positions sample, field for field)
        pos = {"contractSize": inst.lot, "createdDate": self.market.local(d.created), "createdDateUTC": stamp(d.created),
               "dealId": d.deal_id, "dealReference": "p_" + d.deal_id, "workingOrderId": d.order_id, "size": num(d.size),
               "leverage": d.leverage, "upl": rnd(self.upl(acct, d, now, cur), 4), "direction": d.direction, "level": d.level,
               "currency": cur, "guaranteedStop": d.gsl}
        # assumed, verify on demo: the samples have no stop or profit; these names follow the request fields
        if d.stop is not None:
            pos["stopLevel"] = d.stop
        if d.trailing:
            pos["trailingStop"] = True
            pos["trailingStopDistance"] = d.trail_dist
        if d.profit is not None:
            pos["profitLevel"] = d.profit
        return {"position": pos, "market": self.market_row(inst, now, ROW_DEAL, acct)}

    def market_row(self, inst, now, keys, acct=None):
        f = self.market.fields(inst, now, (acct or self.current_any()).leverage[inst.type])
        return {k: f[k] for k in keys}

    def current_any(self):
        return next(iter(self.accounts.values()))

    def get_deal(self, acct, deal_id):
        d = acct.deals.get(deal_id) if isinstance(deal_id, str) else None
        if d is None:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Position not found": 404 error.not-found.dealId)
            raise CapError(404, "error.not-found.dealId")
        return d

    def amend_position(self, acct, deal_id, body):
        """PUT /positions/{dealId}: the stop and the profit only. documented: https://open-api.capital.com/"""
        now = self.now()
        d = self.get_deal(acct, deal_id)
        if not isinstance(body, dict):
            raise CapError(400, "error.invalid.request")             # stand-in's own
        inst = self.market.by_epic[d.epic]
        gsl, trailing = body.get("guaranteedStop", False), body.get("trailingStop", False)
        # assumed, verify on demo: an omitted guaranteedStop or trailingStop turns the flag off, as Capital.com's own
        # client says the broker does on a working order (https://github.com/capital-com-sv/capital-mcp)
        self._flags(acct, inst, dict(body, guaranteedStop=gsl, trailingStop=trailing))
        keep = self.options["position_update_omitted"] == "keep"
        stop, profit = d.stop, d.profit
        # assumed, verify on demo: a distance or amount here counts from the price the deal would close at now
        # (the documentation: "Distance between current and stop loss triggering price")
        current = self._close_price(inst, d.direction, now)
        new_stop = new_profit = False
        if any(k in body for k in ("stopLevel", "stopDistance", "stopAmount")):
            stop = self._leg_level(acct, inst, d.direction, self._leg(body, "stop"), current, d.size, "stop", now)   # null clears (stand-in's own)
            new_stop = True
        elif not keep:
            stop = None
        if any(k in body for k in ("profitLevel", "profitDistance", "profitAmount")):
            profit = self._leg_level(acct, inst, d.direction, self._leg(body, "profit"), current, d.size, "profit", now)
            new_profit = True
        elif not keep:
            profit = None
        # stand-in's own: only the legs this request sets are held to the distance rules; a kept leg stays as it is
        self._check_levels(inst, d.direction, current, stop if new_stop else None, profit if new_profit else None, gsl)
        d.stop, d.profit, d.gsl = stop, profit, gsl
        d.trailing, d.trail_dist = trailing, (float(dec(body["stopDistance"])) if trailing else None)
        self._activity(acct, "EDIT_STOP_AND_LIMIT", "USER", "ACCEPTED", d.epic, d.deal_id, now, size=num(d.size),
                       direction=d.direction, level=d.level, gsl=d.gsl)
        ref = "p_" + d.deal_id                           # documented: https://open-api.capital.com/ (PUT answers "p_<dealId>")
        # assumed, verify on demo: the amend's confirmation (status words beyond the documented sample)
        self._confirm(ref, {"date": stamp(now), "status": "OPEN", "dealStatus": "ACCEPTED", "epic": d.epic, "dealReference": ref,
                            "dealId": d.deal_id, "affectedDeals": [{"dealId": d.deal_id, "status": "AMENDED"}], "level": d.level,
                            "size": num(d.size), "direction": d.direction, "guaranteedStop": d.gsl, "trailingStop": d.trailing}, now)
        return {"dealReference": ref}

    def close_position(self, acct, deal_id):
        """DELETE /positions/{dealId}: the whole deal, at market. documented: https://open-api.capital.com/ (no size;
        answers {"dealReference": "p_<dealId>"}; a gone deal is 404 error.not-found.dealId)"""
        now = self.now()
        d = self.get_deal(acct, deal_id)
        inst = self.market.by_epic[d.epic]
        ref = "p_" + d.deal_id
        closing = "SELL" if d.direction == "BUY" else "BUY"
        price = self._close_price(inst, d.direction, now)
        confirm = {"date": stamp(now), "status": "CLOSED", "dealStatus": "ACCEPTED", "epic": d.epic, "dealReference": ref,
                   "dealId": d.deal_id, "affectedDeals": [{"dealId": d.deal_id, "status": "FULLY_CLOSED"}], "level": price,
                   "size": num(d.size), "direction": closing, "guaranteedStop": d.gsl, "trailingStop": d.trailing}
        reason = self._take_reject("close")
        if reason is None and self.market.status(inst, now) != "TRADEABLE":
            reason = "MARKET_CLOSED"            # assumed, verify on demo: a close while the market is shut is a REJECTED confirm
        if reason is not None:
            confirm.update({"status": "OPEN", "dealStatus": "REJECTED", "affectedDeals": [], "reason": reason})
            self._activity(acct, "POSITION", "USER", "REJECTED", d.epic, d.deal_id, now, size=num(d.size), direction=closing,
                           level=price, gsl=d.gsl)
        else:
            self._close_qty(acct, d, d.size, price, "USER", now)
        self._confirm(ref, confirm, now)
        return {"dealReference": ref}

    def close_as(self, acct, deal_id, source):
        """A close the broker makes itself (a dealer, the system, a margin close-out)."""
        if source not in ACTIVITY_SOURCES:
            raise ValueError("source must be one of %s" % (ACTIVITY_SOURCES,))
        with self.lock:
            now = self.now()
            d = self.get_deal(acct, deal_id)
            inst = self.market.by_epic[d.epic]
            return num(self._close_qty(acct, d, d.size, self._close_price(inst, d.direction, now), source, now))

    # -- working orders
    def _order_level(self, inst, direction, kind, level, now):
        bid, ask = self.market.quote(inst.epic, now)
        px = ask if direction == "BUY" else bid
        # stand-in's own: a LIMIT must sit below the market for a buy (above for a sell), a STOP beyond it; one that
        # would trigger at once is refused
        wrong = (level >= px) if (kind == "LIMIT") == (direction == "BUY") else (level <= px)
        if wrong:
            raise CapError(400, "error.invalid.level")

    def place_order(self, acct, body):
        """POST /workingorders. documented: https://open-api.capital.com/ (LIMIT or STOP at `level`, optional
        goodTillDate in UTC; answers {"dealReference": "o_..."})"""
        now = self.now()
        inst, direction, size = self._basics(body)
        kind = body.get("type")
        if kind not in ("LIMIT", "STOP"):
            raise CapError(400, "error.invalid.type")              # documented: "Must be LIMIT or STOP"; code stand-in's own
        try:
            level = rnd(float(dec(body.get("level"))), inst.dp)
        except ValueError:
            raise CapError(400, "error.invalid.level")             # stand-in's own
        if level <= 0:
            raise CapError(400, "error.invalid.level")
        gtd = self._gtd(body.get("goodTillDate"), now)
        gsl, trailing = self._flags(acct, inst, body)
        stop_leg, profit_leg = self._leg(body, "stop"), self._leg(body, "profit")
        self._order_level(inst, direction, kind, level, now)
        stop = self._leg_level(acct, inst, direction, stop_leg, level, size, "stop", now)
        profit = self._leg_level(acct, inst, direction, profit_leg, level, size, "profit", now)
        self._check_levels(inst, direction, level, stop, profit, gsl)
        ref = "o_" + self._uuid()
        o = Order()
        o.deal_id, o.epic, o.direction, o.size, o.level, o.kind, o.gtd = self._deal_id(), inst.epic, direction, size, level, kind, gtd
        o.created, o.leverage, o.gsl, o.trailing = now, acct.leverage[inst.type], gsl, trailing
        # the legs are kept as the distance from the order's level, the way GET /workingorders shows them
        o.stop = None if stop is None else ("distance", abs(stop - level))
        o.profit = None if profit is None else ("distance", abs(profit - level))
        o.parts, o.next_part_at, o.rest, o.interval = None, None, "fill", 0.0
        confirm = {"date": stamp(now), "status": "OPEN", "dealStatus": "ACCEPTED", "epic": inst.epic, "dealReference": ref,
                   "dealId": o.deal_id, "affectedDeals": [{"dealId": o.deal_id, "status": "OPENED"}], "level": level,
                   "size": num(size), "direction": direction, "guaranteedStop": gsl, "trailingStop": trailing}
        reason = self._take_reject("workingorders")
        if reason is None and self.options["wo_reserves_margin"]:
            if self.margin_of(acct, inst.epic, size, level, now) > self.figures(acct, now)["available"] + 1e-9:
                reason = "INSUFFICIENT_FUNDS"
        if reason is not None:
            confirm.update({"status": None, "dealStatus": "REJECTED", "affectedDeals": [], "reason": reason})
            self._activity(acct, "WORKING_ORDER", "USER", "REJECTED", inst.epic, o.deal_id, now, size=num(size),
                           direction=direction, level=level, reference=ref)
        else:
            acct.orders[o.deal_id] = o
            # documented: https://open-api.capital.com/ (GET /history/activity sample: WORKING_ORDER, USER, ACCEPTED)
            self._activity(acct, "WORKING_ORDER", "USER", "ACCEPTED", inst.epic, o.deal_id, now, size=num(size),
                           direction=direction, level=level, gsl=gsl, reference=ref)
        self._confirm(ref, confirm, now)
        return {"dealReference": ref}

    def _gtd(self, value, now):
        if value is None:
            return None
        try:
            gtd = parse_when(value)
        except ValueError:
            raise CapError(400, "error.invalid.goodTillDate")      # stand-in's own
        if gtd <= now:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Cancellation date in the past":
            # 400 error.not-in-future.goodTillDate)
            raise CapError(400, "error.not-in-future.goodTillDate")
        return gtd

    def order_view(self, acct, o, now):
        inst = self.market.by_epic[o.epic]
        # documented: https://open-api.capital.com/ (GET /workingorders sample, field for field)
        wo = {"dealId": o.deal_id, "direction": o.direction, "epic": o.epic, "orderSize": num(o.size), "leverage": o.leverage,
              "orderLevel": o.level, "timeInForce": "GOOD_TILL_DATE" if o.gtd else "GOOD_TILL_CANCELLED"}
        if o.gtd:
            wo["goodTillDate"] = self.market.local(o.gtd)
            wo["goodTillDateUTC"] = stamp(o.gtd)
        wo.update({"createdDate": self.market.local(o.created), "createdDateUTC": stamp(o.created), "guaranteedStop": o.gsl,
                   "orderType": o.kind})
        # documented: https://open-api.capital.com/ (the sample's BUY LIMIT at 20 shows stopDistance -3, profitDistance 3:
        # the legs are signed from the order's level)
        below = -1.0 if o.direction == "BUY" else 1.0
        if o.stop is not None:
            wo["stopDistance"] = rnd(below * o.stop[1], inst.dp)
        if o.profit is not None:
            wo["profitDistance"] = rnd(-below * o.profit[1], inst.dp)
        wo.update({"trailingStop": o.trailing, "currencyCode": inst.currency})
        return {"workingOrderData": wo, "marketData": self.market_row(inst, now, ROW_DEAL, acct)}

    def get_order(self, acct, deal_id):
        o = acct.orders.get(deal_id) if isinstance(deal_id, str) else None
        if o is None:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Order not found": 404 error.not-found.dealId)
            raise CapError(404, "error.not-found.dealId")
        return o

    def amend_order(self, acct, deal_id, body):
        """PUT /workingorders/{dealId}. documented: https://github.com/capital-com-sv/capital-mcp (Capital.com's own client:
        level, stops, goodTillDate and timeInForce left out "are preserved by the broker"; guaranteedStop and trailingStop
        left out are reset to false; epic and size cannot be amended)"""
        now = self.now()
        o = self.get_order(acct, deal_id)
        if not isinstance(body, dict):
            raise CapError(400, "error.invalid.request")
        inst = self.market.by_epic[o.epic]
        gsl, trailing = body.get("guaranteedStop", False), body.get("trailingStop", False)
        merged = dict(body, guaranteedStop=gsl, trailingStop=trailing)
        if gsl and not any(merged.get(k) is not None for k in ("stopLevel", "stopDistance", "stopAmount")) and o.stop is not None:
            merged["stopDistance"] = o.stop[1]
        self._flags(acct, inst, merged)
        level = o.level
        if body.get("level") is not None:
            try:
                level = rnd(float(dec(body["level"])), inst.dp)
            except ValueError:
                raise CapError(400, "error.invalid.level")
            self._order_level(inst, o.direction, o.kind, level, now)
        gtd = o.gtd
        if body.get("timeInForce") == "GOOD_TILL_CANCELLED":
            gtd = None                                     # documented: https://github.com/capital-com-sv/capital-mcp
        if body.get("goodTillDate") is not None:
            gtd = self._gtd(body["goodTillDate"], now)
        stop, profit = o.stop, o.profit
        if any(body.get(k) is not None for k in ("stopLevel", "stopDistance", "stopAmount")):
            lv = self._leg_level(acct, inst, o.direction, self._leg(body, "stop"), level, o.size, "stop", now)
            stop = ("distance", abs(lv - level))
        if any(body.get(k) is not None for k in ("profitLevel", "profitDistance", "profitAmount")):
            lv = self._leg_level(acct, inst, o.direction, self._leg(body, "profit"), level, o.size, "profit", now)
            profit = ("distance", abs(lv - level))
        sl = None if stop is None else self._leg_level(acct, inst, o.direction, stop, level, o.size, "stop", now)
        tp = None if profit is None else self._leg_level(acct, inst, o.direction, profit, level, o.size, "profit", now)
        self._check_levels(inst, o.direction, level, sl, tp, gsl)
        o.level, o.gtd, o.stop, o.profit, o.gsl, o.trailing = level, gtd, stop, profit, gsl, trailing
        self._activity(acct, "WORKING_ORDER", "USER", "MODIFIED", o.epic, o.deal_id, now, size=num(o.size), direction=o.direction,
                       level=level, gsl=gsl)
        ref = "o_" + self._uuid()                          # documented: https://open-api.capital.com/ (PUT answers a new "o_...")
        self._confirm(ref, {"date": stamp(now), "status": "OPEN", "dealStatus": "ACCEPTED", "epic": o.epic, "dealReference": ref,
                            "dealId": o.deal_id, "affectedDeals": [{"dealId": o.deal_id, "status": "AMENDED"}], "level": level,
                            "size": num(o.size), "direction": o.direction, "guaranteedStop": gsl, "trailingStop": trailing}, now)
        return {"dealReference": ref}

    def cancel_order(self, acct, deal_id):
        """DELETE /workingorders/{dealId}. documented: https://open-api.capital.com/ (answers {"dealReference": "o_..."})"""
        now = self.now()
        o = self.get_order(acct, deal_id)
        del acct.orders[o.deal_id]
        self._activity(acct, "WORKING_ORDER", "USER", "CANCELLED", o.epic, o.deal_id, now, size=num(o.size), direction=o.direction,
                       level=o.level, gsl=o.gsl)
        ref = "o_" + self._uuid()
        # assumed, verify on demo: the cancel's confirmation
        self._confirm(ref, {"date": stamp(now), "status": "DELETED", "dealStatus": "ACCEPTED", "epic": o.epic, "dealReference": ref,
                            "dealId": o.deal_id, "affectedDeals": [{"dealId": o.deal_id, "status": "DELETED"}], "level": o.level,
                            "size": num(o.size), "direction": o.direction, "guaranteedStop": o.gsl, "trailingStop": o.trailing}, now)
        return {"dealReference": ref}

    # -- the engine
    def tick(self):
        """One pass: lapsed sessions, working orders, stops and limits, close-out, overnight fees. Returns the CSTs of
        the sessions that ended in it."""
        with self.lock:
            now = self.now()
            ended = []
            for s in self.sessions.values():
                if s.alive and now - s.last > self.session_ttl * 1000:
                    s.alive = False
                    ended.append(s.cst)
            for acct in self.accounts.values():
                self._orders_tick(acct, now)
                self._deals_tick(acct, now)
                self._closeout(acct, now)
                self._swaps(acct, now)
            return ended

    def _orders_tick(self, acct, now):
        for o in list(acct.orders.values()):
            inst = self.market.by_epic[o.epic]
            if o.next_part_at is None and o.gtd is not None and now >= o.gtd:
                del acct.orders[o.deal_id]
                # documented: https://open-api.capital.com/ (activity status EXPIRED); source SYSTEM is assumed, verify on demo
                self._activity(acct, "WORKING_ORDER", "SYSTEM", "EXPIRED", o.epic, o.deal_id, now, size=num(o.size),
                               direction=o.direction, level=o.level, gsl=o.gsl)
                continue
            if self.market.status(inst, now) != "TRADEABLE":
                continue
            bid, ask = self.market.quote(o.epic, now)
            if o.next_part_at is not None:
                if now >= o.next_part_at:
                    self._fill_part(acct, o, inst, ask if o.direction == "BUY" else bid, now)
                continue
            if o.kind == "LIMIT":
                hit = ask <= o.level if o.direction == "BUY" else bid >= o.level
            else:
                hit = ask >= o.level if o.direction == "BUY" else bid <= o.level
            if hit:
                # stand-in's own: a buy triggers on the ask, a sell on the bid, and it fills at market (documented:
                # https://github.com/capital-com-sv/capital-mcp, every API order "executed as market orders")
                self._trigger(acct, o, inst, ask if o.direction == "BUY" else bid, now)

    def _trigger(self, acct, o, inst, price, now):
        if not self.options["wo_reserves_margin"]:
            offset = self._offset_size(acct, inst, o.direction, o.size)
            if self.margin_of(acct, o.epic, o.size - offset, price, now) > self.figures(acct, now)["available"] + 1e-9:
                del acct.orders[o.deal_id]
                # assumed, verify on demo: an order that cannot be afforded when it triggers is dropped
                self._activity(acct, "WORKING_ORDER", "SYSTEM", "REJECTED", o.epic, o.deal_id, now, size=num(o.size),
                               direction=o.direction, level=o.level, gsl=o.gsl)
                return
        parts, rest, interval = self._parts(inst, o.size)
        o.parts, o.rest, o.interval = list(parts), rest, interval
        self._fill_part(acct, o, inst, price, now)

    def _fill_part(self, acct, o, inst, price, now):
        """One part of a triggered working order. assumed, verify on demo: while parts remain the order stays listed
        with the size still to fill; with rest=cancel it goes after its first part."""
        part = o.parts.pop(0)
        trail = o.stop[1] if (o.trailing and o.stop) else None
        self._execute(acct, inst, o.direction, part, price, o.deal_id, (o.stop, o.profit), o.gsl, o.trailing, trail, "SYSTEM", now)
        o.size -= part
        if o.parts and o.rest != "cancel" and o.size > 0:
            o.next_part_at = now + int(o.interval * 1000)
            return
        del acct.orders[o.deal_id]
        status = "EXECUTED" if not o.parts or o.size <= 0 else "CANCELLED"
        self._activity(acct, "WORKING_ORDER", "SYSTEM", status, o.epic, o.deal_id, now, size=num(part), direction=o.direction,
                       level=price, gsl=o.gsl)

    def _deals_tick(self, acct, now):
        for d in list(acct.deals.values()):
            inst = self.market.by_epic[d.epic]
            if self.market.status(inst, now) != "TRADEABLE":
                continue
            bid, ask = self.market.quote(d.epic, now)
            if d.trailing and d.trail_dist:
                # documented: https://open-api.capital.com/ (trailingStop with stopDistance); that it follows the price
                # it would close at, one way only, is the stand-in's own
                want = rnd(bid - d.trail_dist, inst.dp) if d.direction == "BUY" else rnd(ask + d.trail_dist, inst.dp)
                if d.stop is None or (want > d.stop if d.direction == "BUY" else want < d.stop):
                    d.stop = want
            px = bid if d.direction == "BUY" else ask
            if d.stop is not None and (bid <= d.stop if d.direction == "BUY" else ask >= d.stop):
                # stand-in's own: a stop fills at the market, a guaranteed one exactly at its level
                self._close_qty(acct, d, d.size, d.stop if d.gsl else px, "SL", now)
                if d.gsl:
                    fee = money(Decimal(repr(float(d.size) * inst.lot * d.stop * 0.003 * self.conv(inst.currency, acct.currency, now))))
                    acct.cash -= fee
                    # documented: https://open-api.capital.com/ (transaction type TRADE_COMMISSION_GSL); the 0.3% is stand-in's own
                    self._transaction(acct, "TRADE_COMMISSION_GSL", "Guaranteed stop fee", d.epic, -fee, now)
            elif d.profit is not None and (bid >= d.profit if d.direction == "BUY" else ask <= d.profit):
                self._close_qty(acct, d, d.size, px, "TP", now)

    def _closeout(self, acct, now):
        """documented: https://img.capital.com/docs/September-2024-CCUK-Terms-and-Conditions-_0.pdf (Schedule 2.11: "The
        standardised margin close-out rule has been set at 50% of the total initial margin ... imposed on a gradual
        basis"). stand-in's own: one deal a pass, the worst first."""
        if not acct.deals:
            return
        margin = self.margin_used(acct, now)
        if margin <= 0:
            return
        fig = self.figures(acct, now)
        if fig["balance"] > margin * 0.5:
            return
        worst = min(acct.deals.values(), key=lambda d: self.upl(acct, d, now))
        inst = self.market.by_epic[worst.epic]
        if self.market.status(inst, now) == "TRADEABLE":
            self._close_qty(acct, worst, worst.size, self._close_price(inst, worst.direction, now), "CLOSE_OUT", now)

    def _swaps(self, acct, now):
        for d in list(acct.deals.values()):
            n = 0
            while now >= d.next_swap and n < 60:
                inst = self.market.by_epic[d.epic]
                rate = inst.long_rate if d.direction == "BUY" else inst.short_rate
                at = d.next_swap
                fee = money(Decimal(repr(float(d.size) * inst.lot * self.market.mid(d.epic, at) * rate / 100.0
                                         * self.conv(inst.currency, acct.currency, at))))
                if fee:
                    acct.cash += fee
                    # documented: https://open-api.capital.com/ (transaction type SWAP); the % a day reading of
                    # longRate/shortRate and the note are assumed, verify on demo
                    self._transaction(acct, "SWAP", "Overnight fee", d.epic, fee, at)
                d.next_swap += DAY_MS
                n += 1

    # -- history
    def activity(self, acct, q):
        """GET /history/activity. documented: https://open-api.capital.com/ (from/to filter on dateUTC, at most one day
        apart, one of them alone gives a one-day range; lastPeriod in seconds, default 600, max 86400; detailed; dealId;
        a FIQL filter on epic, source, status, type)"""
        now = self.now()
        lo, hi = self._range(q, now, one_day=True)
        rows = []
        flt = _fiql(q.get("filter"), ("epic", "source", "status", "type"))
        for at, row in acct.activities:
            if not (lo <= at <= hi):
                continue
            if q.get("dealId") and row["dealId"] != q["dealId"]:
                continue
            if flt is not None and not flt(row):
                continue
            out = dict(row)
            if q.get("detailed") != "true":
                out.pop("details", None)
            rows.append(out)
        rows.reverse()       # documented: https://github.com/capital-com-sv/capital-api-postman (the detailed example: newest first)
        return {"activities": rows}

    def transactions(self, acct, q):
        """GET /history/transactions. documented: https://open-api.capital.com/ (from, to, lastPeriod default 600, type)"""
        now = self.now()
        lo, hi = self._range(q, now, one_day=False)
        kind = q.get("type")
        if kind is not None and kind not in TRANSACTION_TYPES:
            raise CapError(400, "error.invalid.type")             # stand-in's own
        rows = [dict(r) for at, r in acct.transactions if lo <= at <= hi and (kind is None or r["transactionType"] == kind)]
        rows.reverse()       # documented: https://open-api.capital.com/ (the sample: the newest first)
        return {"transactions": rows}

    def _range(self, q, now, one_day):
        frm = to = None
        try:
            frm = parse_when(q["from"]) if q.get("from") else None
        except ValueError:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Invalid date format": 400 error.invalid.from)
            raise CapError(400, "error.invalid.from")
        try:
            to = parse_when(q["to"]) if q.get("to") else None
        except ValueError:
            raise CapError(400, "error.invalid.to")                # stand-in's own, by the documented one above
        if frm is not None and to is not None and frm > to:
            raise CapError(400, "error.invalid.daterange")         # stand-in's own
        if one_day:
            if frm is not None and to is None:
                to = frm + DAY_MS
            elif to is not None and frm is None:
                frm = to - DAY_MS
            if frm is not None and to - frm > DAY_MS:
                # documented: https://open-api.capital.com/ (changelog 2023-07-04: "the date range ... is limited to 1
                # day"); the code is the stand-in's own
                raise CapError(400, "error.invalid.daterange")
        if frm is None and to is None:
            try:
                period = int(q.get("lastPeriod", 600))
            except ValueError:
                raise CapError(400, "error.invalid.lastPeriod")    # stand-in's own
            if period < 0 or (one_day and period > 86400):
                raise CapError(400, "error.invalid.lastPeriod")    # documented max 86400 (activity); code stand-in's own
            return now - period * 1000, now
        return (frm if frm is not None else -10 ** 18), (to if to is not None else now)


def _plain(d):
    s = format(d.normalize(), "f")
    return s


def _fmt(x, dp):
    s = ("%." + str(dp) + "f") % x
    return s.rstrip("0").rstrip(".") if "." in s else s


def _fiql(text, fields):
    """A FIQL filter: `,` is OR, `;` is AND (binding tighter), each term field==value or field!=value.
    documented: https://open-api.capital.com/ (GET /history/activity `filter`: FIQL on epic, source, status, type)"""
    if not text:
        return None
    ors = []
    for group in text.split(","):
        terms = []
        for term in group.split(";"):
            m = re.fullmatch(r"\s*(\w+)\s*(==|!=)\s*([\w.-]+)\s*", term)
            if m is None or m.group(1) not in fields:
                raise CapError(400, "error.invalid.filter")         # stand-in's own
            terms.append((m.group(1), m.group(2) == "==", m.group(3)))
        ors.append(terms)
    return lambda row: any(all((row.get(f) == v) == eq for f, eq, v in terms) for terms in ors)


# -- the WebSocket plumbing -------------------------------------------------------------------------------------------

def _frame(opcode, payload):
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else b"\x7e" + struct.pack(">H", n) if n < 65536 else b"\x7f" + struct.pack(">Q", n))
    return head + payload


class _Conn:
    """One WebSocket connection to /connect. stand-in's own: every frame the server sends is a TEXT frame of JSON."""

    def __init__(self, handler, fake):
        self.rfile, self.sock, self.fake = handler.rfile, handler.connection, fake
        self.out, self.open = queue.Queue(), True
        self.quotes, self.ohlc = set(), set()
        self.sent, self.bars = {}, {}
        self.cst = None
        self.last_msg = fake.broker.now()
        self._writer = threading.Thread(target=self._write, name="fake-capitalcom-stream", daemon=True)
        self._writer.start()

    def send(self, obj):
        if self.open:
            self.out.put(_frame(1, json.dumps(obj, separators=(",", ":")).encode()))

    def _write(self):
        while True:
            data = self.out.get()
            if data is None:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                return

    def _read(self, n):
        data = self.rfile.read(n) if n else b""
        if len(data) < n:
            raise EOFError
        return data

    def run(self):
        parts = []
        try:
            while self.open:
                b0, b1 = self._read(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                payload = self._read(n)
                if mask:
                    payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
                opcode = b0 & 0x0F
                if opcode == 8:
                    self.close(True, payload[:2] or b"\x03\xe8")
                    return
                if opcode == 9:
                    self.out.put(_frame(10, payload))        # RFC 6455 ping answered with its payload
                elif opcode in (0, 1, 2):
                    parts.append(payload)
                    if b0 & 0x80:
                        self.fake._ws_message(self, b"".join(parts))
                        parts = []
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.close()

    def close(self, clean=False, code=b"\x03\xe8"):
        if not self.open:
            return
        self.open = False
        with self.fake._guard:
            self.fake._conns.discard(self)
        if clean:
            self.out.put(_frame(8, code))
            self.out.put(None)
            self._writer.join(1.0)
        else:
            self.out.put(None)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], OSError):       # a client that hung up is not the stand-in's fault
            return
        super().handle_error(request, client_address)


class _RestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "fake-capitalcom", ""

    def log_message(self, *args):
        pass

    def _go(self):
        self.server.fake._handle(self)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _go


class _StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "fake-capitalcom-stream", ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.fake._upgrade(self)


# -- the stand-in ------------------------------------------------------------------------------------------------------

_JSON = "application/json"
_MARKET_DATA = frozenset(("time", "prices", "markets", "market", "nav", "sentiments", "sentiment"))



def _gateway_page(status: int) -> bytes:
    """A gateway's answer page (nginx's shape), CRLF lines: what a proxy in front of an API sends for a 5xx."""
    words = {500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Temporarily Unavailable"}[status]
    return ("<html>\r\n<head><title>%d %s</title></head>\r\n<body>\r\n<center><h1>%d %s</h1></center>\r\n"
            "<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n" % (status, words, status, words)).encode()

class FakeCapitalCom:
    """The stand-in. start() serves on 127.0.0.1 (loopback only): REST at `port`, the stream at `stream_port` (0 picks
    free ones); stop() ends both."""

    # documented: https://help.capitalccuk.com/hc/en-us/articles/30648881738898 ("Your demo account starts with a default
    # balance of 1,000"); the second account, and its 1,000 GBP, are the stand-in's own
    def __init__(self, seed=7, market="auto", port=0, stream_port=0, identifier="demo.trader@example.com",
                 password="fake-capitalcom-key-password", api_key="FAKECAPITALCOMDEMOKEY0000000001",
                 balances=(("USD", "1000"), ("GBP", "1000")), local_offset_h=3, day_start_min=0, confirm_delay=0.1,
                 session_ttl=600, stream_idle=600, rate_per_s=10, session_per_s=1, order_spacing=0.1, orders_per_hour=1000,
                 topups_per_s=10, topups_per_day=100, tick=0.1, max_epics=40):
        # documented: https://open-api.capital.com/ (limits: 10 requests a second per user; POST /session 1 a second per
        # API key; "1 request per 0.1 seconds per user when opening positions or creating orders"; on demo POST /positions
        # and POST /workingorders 1000 an hour; POST /accounts/topUp 10 a second and 100 a day; sessions 10 minutes;
        # "The maximum number of epics: 40"; ping the stream "at least once every 10 minutes"). Tests may loosen them.
        limits = {"rate_per_s": rate_per_s, "session_per_s": session_per_s, "order_spacing": order_spacing,
                  "orders_per_hour": orders_per_hour}
        self.topups_per_s, self.topups_per_day, self.max_epics = topups_per_s, topups_per_day, max_epics
        self.stream_idle, self.tick_s = stream_idle, tick
        self.market = Market(seed, market, day_start_min, clock=lambda: int(time.time() * 1000))
        self.market.local_ms = int(local_offset_h * 3600 * 1000)
        self.broker = Broker(self.market, seed, identifier, password, api_key, balances, confirm_delay, session_ttl, limits)
        self.broker.local_offset_h = local_offset_h
        self.seed = seed
        self._port, self._stream_port = port, stream_port
        self._rest = self._stream = None
        self._threads = []
        self._conns, self._guard = set(), threading.Lock()
        self._log, self._ws_log, self._seq = [], [], 0
        self._faults, self._refusals, self._losses = [], [], []
        self._stop = threading.Event()

    # -- life
    def start(self):
        self._rest = _Server(("127.0.0.1", self._port), _RestHandler)
        self._stream = _Server(("127.0.0.1", self._stream_port), _StreamHandler)
        for srv in (self._rest, self._stream):
            srv.fake = self
        self.broker.streaming_host = self.streaming_host
        self._stop.clear()
        self._threads = [threading.Thread(target=self._rest.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-capitalcom-rest", daemon=True),
                         threading.Thread(target=self._stream.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-capitalcom-ws", daemon=True),
                         threading.Thread(target=self._engine, name="fake-capitalcom-engine", daemon=True)]
        for t in self._threads:
            t.start()
        return self

    def stop(self):
        if self._rest is None:
            return
        self._stop.set()
        self.drop_streams()
        for srv in (self._rest, self._stream):
            srv.shutdown()
            srv.server_close()
        for t in self._threads:
            t.join(5)
        self._rest = self._stream = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- addresses and the fake credentials
    @property
    def port(self):
        return self._rest.server_address[1]

    @property
    def stream_port(self):
        return self._stream.server_address[1]

    @property
    def rest_url(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def api_url(self):
        # documented: https://open-api.capital.com/ (every path is under /api/v1)
        return self.rest_url + "/api/v1"

    @property
    def streaming_host(self):
        # documented: https://open-api.capital.com/ (the session's streamingHost "wss://api-streaming-capital.backend-capital.com/")
        return "ws://127.0.0.1:%d/" % self.stream_port

    @property
    def stream_url(self):
        # documented: https://open-api.capital.com/ ("connect to wss://api-streaming-capital.backend-capital.com/connect")
        return self.streaming_host + "connect"

    @property
    def control_url(self):
        return self.rest_url + "/_control"

    api_key = property(lambda self: self.broker.api_key)
    identifier = property(lambda self: self.broker.identifier)
    password = property(lambda self: self.broker.password)

    @property
    def accounts(self):
        return [{"accountId": a.id, "currency": a.currency, "preferred": a.preferred} for a in self.broker.accounts.values()]

    # -- controls (each is also a POST /_control/...)
    def set_price(self, epic, bid=None, ask=None, mid=None, jump_pct=None, trend_pct_per_s=None, seconds=None, walk=None, spread=None):
        with self.broker.lock:
            return self.market.set_price(epic, self.broker.now(), bid, ask, mid, jump_pct, trend_pct_per_s, seconds, walk, spread)

    def set_market(self, state, epic=None):
        if state not in ("open", "closed", "auto"):
            raise ValueError("state must be open, closed or auto")
        with self.market.lock:
            if epic is None:
                self.market.mode, self.market.modes = state, {}
            elif self.market.find(epic) is None:
                raise KeyError("no such epic: %r" % (epic,))
            else:
                self.market.modes[epic] = state
        return {"mode": self.market.mode, "modes": dict(self.market.modes)}

    def advance(self, seconds):
        """Move the stand-in's clock forward. The order engine looks along the way (every minute of the span, at most 2000
        looks), so a stop the price crossed in between is hit and a goodTillDate on the way expires."""
        span = int(float(seconds) * 1000)
        if span < 0:
            raise ValueError("the clock only moves forward; reset puts it back")
        with self.broker.lock:
            base = self.broker.offset_ms
            looks = max(1, min(2000, span // MIN_MS))
            for i in range(1, looks + 1):
                self.broker.offset_ms = base + span * i // looks
                self.broker.tick()
        self._step()
        return {"now": stamp(self.broker.now())}

    def fill_next(self, parts, rest="fill", interval=0.0):
        """The next execution (a market order, or a working order when it triggers) comes in parts of these weights:
        rest="fill" fills all of it (each part its own deal), rest="cancel" fills the first part only."""
        if rest not in ("fill", "cancel") or not isinstance(parts, (list, tuple)) or not parts or any(float(w) <= 0 for w in parts):
            raise ValueError("parts: positive weights; rest: fill or cancel")
        with self.broker.lock:
            self.broker.next_parts = ([float(w) for w in parts], rest, float(interval))

    def fill_rule(self, size=None, parts=None, epic=None, clear=False):
        """Every order of exactly this size (on this epic, when given) fills in parts, as a market order split in deals."""
        with self.broker.lock:
            if clear:
                self.broker.fill_rules = []
                return []
            self.broker.fill_rules.append((epic, dec(size), [float(w) for w in (parts or [1, 1])]))
            return [(e, num(s), w) for e, s, w in self.broker.fill_rules]

    def refuse_next(self, error_code, status=400, path="/positions", method="POST", count=1, at="request"):
        """The next `count` matching requests are refused with this errorCode: at="request" answers it at once; at="confirm"
        takes the order (a dealReference comes back) and its confirmation says REJECTED with that reason."""
        if at not in ("request", "confirm"):
            raise ValueError("at must be request or confirm")
        with self._guard:
            if at == "request":
                self._refusals.append({"code": error_code, "status": int(status), "path": path, "method": method, "count": int(count)})
                return
        kind = {"/positions": "positions", "/workingorders": "workingorders"}.get(path, "close" if path.startswith("/positions/") else None)
        if kind is None:
            raise ValueError("a confirm refusal is for /positions, /workingorders or /positions/{dealId}")
        with self.broker.lock:
            self.broker.rejects.append({"kind": kind, "reason": error_code, "count": int(count)})

    def fail_next(self, status=500, count=1, path=None, retry_after=None, method=None, page=False):
        """The next `count` requests (to paths under /api/v1 starting with `path`, of `method`) answer `status` without
        acting. With `page`, a 5xx is answered with a gateway's HTML page (text/html, CRLF lines), as a proxy in front of
        Capital.com would answer it (stand-in's own: Capital.com's own 5xx bodies are not documented)."""
        if int(status) not in (429, 500, 502, 503):
            raise ValueError("status must be 429, 500, 502 or 503")
        if page and int(status) == 429:
            raise ValueError("a page is for a 5xx")
        with self._guard:
            self._faults.append({"status": int(status), "count": int(count), "path": path, "retry_after": retry_after,
                                 "method": method.upper() if method else None, "page": bool(page)})

    def lose_next_answer(self, path=None, method=None, count=1):
        """The next `count` matching requests are carried out, then the connection is cut without an answer."""
        with self._guard:
            self._losses.append({"path": path, "method": method, "count": int(count)})

    def expire_sessions(self):
        """Every session ends now: the next call with its tokens is answered 401 (a new POST /session works). Their
        streams end at the next engine pass unless set_option("stream_ends_with_session", False)."""
        with self.broker.lock:
            dead = [s.cst for s in self.broker.sessions.values() if s.alive]
            self.broker.expire_all()
        return len(dead)

    def set_key_revoked(self, revoked=True):
        with self.broker.lock:
            self.broker.key_revoked = bool(revoked)
            if revoked:
                self.broker.expire_all()
        return {"revoked": self.broker.key_revoked}

    def set_no_account(self, flag=True):
        """The login has no account on this host (seen on the demo host, 2026-11-15: a key made while the platform was
        on the live account): its next POST /session is answered 401 error.null.accountId, though the key and the
        password are good. reset() clears it."""
        with self.broker.lock:
            self.broker.no_account = bool(flag)
        return {"no_account": self.broker.no_account}

    def drop_streams(self, clean=False):
        """Cut every WebSocket connection: the socket simply ends (no close frame) unless clean=True."""
        with self._guard:
            conns = list(self._conns)
        for c in conns:
            c.close(clean)
        return len(conns)

    def set_confirm_delay(self, seconds):
        with self.broker.lock:
            self.broker.confirm_delay = max(0.0, float(seconds))

    def set_history(self, resolution, days=None, reset=False):
        """How far back bars of this resolution go (days before now); reset puts back the default (2005-01-03).
        assumed, verify on demo: the real depth per resolution is not documented."""
        if resolution not in RESOLUTIONS:
            raise ValueError("no such resolution")
        with self.market.lock:
            if reset or days is None:
                self.market.history.pop(resolution, None)
            else:
                self.market.history[resolution] = float(days)

    def set_option(self, name, value):
        if name not in OPTIONS or value not in OPTIONS[name][1]:
            raise ValueError("options: %s" % {k: v[1] for k, v in OPTIONS.items()})
        with self.broker.lock:
            self.broker.options[name] = value
        return dict(self.broker.options)

    def close_as(self, deal_id, source="DEALER", account_id=None):
        acct = self._account(account_id, deal_id)
        return self.broker.close_as(acct, deal_id, source)

    def adjust_balance(self, amount, account_id=None, kind="ADJUSTMENT", note="Adjustment"):
        if kind not in TRANSACTION_TYPES:
            raise ValueError("no such transaction type")
        with self.broker.lock:
            acct = self._account(account_id)
            amt = money(dec(amount))
            acct.cash += amt
            self.broker._transaction(acct, kind, note, "", amt, self.broker.now())
            return num(acct.cash)

    def tick(self):
        self._step()

    def reset(self):
        """Accounts, prices, markets, sessions, options and every control as at the start. The request log is kept
        (clear it with requests(clear=True)); open WebSocket connections are cut."""
        with self.broker.lock:
            self.market.reset()
            self.broker.reset()
            self.broker.offset_ms = 0
        with self._guard:
            self._faults, self._refusals, self._losses = [], [], []
        self.drop_streams()

    def requests(self, clear=False):
        with self._guard:
            out = list(self._log)
            if clear:
                self._log = []
            return out

    def stream_log(self, clear=False):
        with self._guard:
            out = list(self._ws_log)
            if clear:
                self._ws_log = []
            return out

    def state(self):
        b = self.broker
        with b.lock:
            now = b.now()
            with self._guard:
                conns = [{"quotes": sorted(c.quotes), "ohlc": sorted("%s:%s:%s" % k for k in c.ohlc)} for c in self._conns]
            return {"now": stamp(now), "clock_offset_s": b.offset_ms / 1000.0, "market": self.market.mode, "modes": dict(self.market.modes),
                    "options": dict(b.options), "confirm_delay_s": b.confirm_delay, "key_revoked": b.key_revoked,
                    "no_account": b.no_account,
                    "sessions": sum(1 for s in b.sessions.values() if s.alive), "streams": conns,
                    "next_parts": b.next_parts, "fill_rules": [(e, num(s), w) for e, s, w in b.fill_rules],
                    "accounts": [{"accountId": a.id, "currency": a.currency, "cash": num(a.cash), "deals": len(a.deals), "orders": len(a.orders),
                                  "hedgingMode": a.hedging} for a in b.accounts.values()],
                    "faults": [dict(f) for f in self._faults if f["count"] > 0],
                    "refusals": [dict(f) for f in self._refusals if f["count"] > 0] + [dict(r) for r in b.rejects if r["count"] > 0],
                    "losses": [dict(f) for f in self._losses if f["count"] > 0]}

    def _account(self, account_id=None, deal_id=None):
        accts = self.broker.accounts
        if account_id is not None:
            if account_id not in accts:
                raise KeyError("no such account")
            return accts[account_id]
        if deal_id is not None:
            for a in accts.values():
                if deal_id in a.deals:
                    return a
            raise KeyError("no such deal")
        return next(iter(accts.values()))

    def _control(self, method, name, q, body):
        if name == "price":
            return self.set_price(**{k: body[k] for k in ("epic", "bid", "ask", "mid", "jump_pct", "trend_pct_per_s", "seconds", "walk", "spread") if k in body})
        if name == "market":
            return self.set_market(body["state"], body.get("epic"))
        if name == "clock":
            return self.advance(body["advance_s"])
        if name == "fills":
            self.fill_next(body["parts"], body.get("rest", "fill"), body.get("interval_s", 0.0))
            return {"next_parts": self.broker.next_parts}
        if name == "fill_rule":
            return {"fill_rules": self.fill_rule(body.get("size"), body.get("parts"), body.get("epic"), bool(body.get("clear")))}
        if name == "refuse":
            self.refuse_next(body["error_code"], body.get("status", 400), body.get("path", "/positions"), body.get("method", "POST"),
                             body.get("count", 1), body.get("at", "request"))
            return {"refusals": self.state()["refusals"]}
        if name == "fail":
            self.fail_next(body.get("status", 500), body.get("count", 1), body.get("path"), body.get("retry_after"),
                           body.get("method"), body.get("page", False))
            return {"faults": self.state()["faults"]}
        if name == "lose":
            self.lose_next_answer(body.get("path"), body.get("method"), body.get("count", 1))
            return {"losses": self.state()["losses"]}
        if name == "expire":
            return {"expired": self.expire_sessions()}
        if name == "key":
            return self.set_key_revoked(bool(body.get("revoked", True)))
        if name == "login":
            return self.set_no_account(bool(body.get("no_account", True)))
        if name == "drop_streams":
            return {"dropped": self.drop_streams(bool(body.get("clean")))}
        if name == "confirm_delay":
            self.set_confirm_delay(body["seconds"])
            return {"confirm_delay_s": self.broker.confirm_delay}
        if name == "history":
            self.set_history(body["resolution"], body.get("days"), bool(body.get("reset")))
            return {"history_days": dict(self.market.history)}
        if name == "option":
            return {"options": self.set_option(body["name"], body["value"])}
        if name == "close_as":
            return {"pnl": self.close_as(body["deal_id"], body.get("source", "DEALER"), body.get("account_id"))}
        if name == "adjust":
            return {"cash": self.adjust_balance(body["amount"], body.get("account_id"), body.get("type", "ADJUSTMENT"), body.get("note", "Adjustment"))}
        if name == "tick":
            self.tick()
            return {"now": stamp(self.broker.now())}
        if name == "reset":
            self.reset()
            return {"reset": True}
        if name == "requests":
            return {"requests": self.requests(clear=method == "DELETE" or q.get("clear") in ("1", "true"))}
        if name == "stream_log":
            return {"messages": self.stream_log(clear=q.get("clear") in ("1", "true"))}
        if name == "state":
            return self.state()
        raise KeyError("no such control: " + name)

    # -- the engine
    def _engine(self):
        while not self._stop.wait(self.tick_s):
            try:
                self._step()
            except Exception:              # a fault of the stand-in itself: said aloud, then the next pass
                traceback.print_exc()

    def _step(self):
        self.broker.tick()
        if self.broker.options["stream_ends_with_session"]:
            with self.broker.lock:
                alive = {cst for cst, s in self.broker.sessions.items() if s.alive}
            with self._guard:
                stale = [c for c in self._conns if c.cst is not None and c.cst not in alive]
            for c in stale:
                c.close()
        self._push()

    def _close_sessions(self, csts):
        dead = set(csts)
        with self._guard:
            conns = [c for c in self._conns if c.cst in dead]
        for c in conns:
            c.close()

    def _push(self):
        now = self.broker.now()
        with self._guard:
            conns = list(self._conns)
        if not conns:
            return
        quotes, statuses = {}, {}
        mode = self.broker.options["ohlc_mode"]
        for c in conns:
            if now - c.last_msg > self.stream_idle * 1000:
                # documented: https://open-api.capital.com/ ("ping service at least once every 10 minutes"); that the
                # server then ends the connection (a close frame, 1000) is assumed, verify on demo
                c.close(clean=True)
                continue
            for epic in list(c.quotes):
                inst = self.market.by_epic[epic]
                st = statuses.get(epic) or statuses.setdefault(epic, self.market.status(inst, now))
                if st != "TRADEABLE":
                    continue                     # stand-in's own: a shut market streams nothing
                q = quotes.get(epic) or quotes.setdefault(epic, self.market.quote(epic, now))
                if c.sent.get(epic) != q:
                    c.sent[epic] = q
                    second = now // SEC_MS
                    # documented: https://open-api.capital.com/ (the "quote" message: epic, product, bid, bidQty, ofr,
                    # ofrQty, timestamp); the quantities are the stand-in's own
                    c.send({"status": "OK", "destination": "quote", "payload": {
                        "epic": epic, "product": "CFD", "bid": q[0], "bidQty": float(1000 + int(_u("bq", epic, second) * 4000)),
                        "ofr": q[1], "ofrQty": float(1000 + int(_u("aq", epic, second) * 4000)), "timestamp": now}})
            for key in sorted(c.ohlc):
                epic, res, kind = key
                inst = self.market.by_epic[epic]
                st = statuses.get(epic) or statuses.setdefault(epic, self.market.status(inst, now))
                if st != "TRADEABLE":
                    continue
                t0 = self.market.bar_start(res, now)
                prev = c.bars.get(key)
                if prev is not None and prev[0] != t0:
                    final = self.market.ohlc(epic, res, kind, prev[0], now)
                    if mode == "closed" or final != prev[1]:
                        c.send(self._ohlc_msg(epic, res, kind, prev[0], final))
                vals = self.market.ohlc(epic, res, kind, t0, now) if (mode == "forming" or prev is None or prev[0] != t0) else prev[1]
                if mode == "forming" and (prev is None or prev[0] != t0 or prev[1] != vals):
                    c.send(self._ohlc_msg(epic, res, kind, t0, vals))
                c.bars[key] = (t0, vals)

    @staticmethod
    def _ohlc_msg(epic, res, kind, t0, vals):
        # documented: https://open-api.capital.com/ (the "ohlc.event" message: resolution, epic, type, priceType "bid", t, h, l, o, c)
        return {"status": "OK", "destination": "ohlc.event", "payload": {
            "resolution": res, "epic": epic, "type": kind, "priceType": "bid", "t": t0, "h": vals[1], "l": vals[2], "o": vals[0], "c": vals[3]}}

    # -- the stream
    def _upgrade(self, h):
        path = urlsplit(h.path).path
        if path != "/connect" or (h.headers.get("Upgrade") or "").lower() != "websocket" or not h.headers.get("Sec-WebSocket-Key"):
            body = b'{"errorCode":"error.not-found"}'           # stand-in's own
            h.send_response(404)
            h.send_header("Content-Type", _JSON)
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(body)
            h.close_connection = True
            return
        accept = base64.b64encode(hashlib.sha1((h.headers["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
        conn = _Conn(h, self)
        with self._guard:
            self._conns.add(conn)
        try:
            h.send_response(101, "Switching Protocols")
            h.send_header("Upgrade", "websocket")
            h.send_header("Connection", "Upgrade")
            h.send_header("Sec-WebSocket-Accept", accept)
            h.end_headers()
            h.wfile.flush()
            conn.run()
        finally:
            conn.close()
            h.close_connection = True

    def _ws_message(self, conn, data):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return                                  # stand-in's own: a frame that is not JSON is ignored
        if not isinstance(msg, dict):
            return
        dest, corr = msg.get("destination"), msg.get("correlationId")
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        with self._guard:
            self._ws_log.append({"at": stamp(self.broker.now()), "destination": dest, "correlationId": corr,
                                 "has_tokens": bool(msg.get("cst")) and bool(msg.get("securityToken")),
                                 "payload": {k: v for k, v in payload.items() if k in ("epics", "resolutions", "type", "types")}})

        def reply(status, body):
            # documented: https://open-api.capital.com/ (answers carry status, destination, correlationId and payload)
            conn.send({"status": status, "destination": dest, "correlationId": corr, "payload": body})

        with self.broker.lock:
            # documented: https://open-api.capital.com/ (every message carries cst and securityToken from the session)
            sess = self.broker.session_for(msg.get("cst"), msg.get("securityToken"), touch=self.broker.options["ws_extends_session"])
            if sess is None:
                reply("ERROR", {"errorCode": "error.invalid.session.token"})       # stand-in's own
                return
            conn.cst = sess.cst
            conn.last_msg = self.broker.now()
            if dest == "ping":
                reply("OK", {})                     # documented: https://open-api.capital.com/ (ping answers an empty payload)
                return
            if dest in ("marketData.subscribe", "marketData.unsubscribe", "OHLCMarketData.subscribe", "OHLCMarketData.unsubscribe"):
                epics = payload.get("epics")
                if not isinstance(epics, list) or not epics or not all(isinstance(e, str) for e in epics):
                    reply("ERROR", {"errorCode": "error.invalid.epics"})            # stand-in's own
                    return
                if dest == "marketData.subscribe":
                    self._subscribe_quotes(conn, epics, reply)
                elif dest == "marketData.unsubscribe":
                    for e in epics:
                        conn.quotes.discard(e)
                        conn.sent.pop(e, None)
                    # documented: https://open-api.capital.com/ (the unsubscribe answer: every epic named, "PROCESSED")
                    reply("OK", {"subscriptions": {e: "PROCESSED" for e in epics}})
                elif dest == "OHLCMarketData.subscribe":
                    self._subscribe_ohlc(conn, epics, payload, reply)
                else:
                    self._unsubscribe_ohlc(conn, epics, payload, reply)
                return
            reply("ERROR", {"errorCode": "error.invalid.destination"})              # stand-in's own

    def _login_conns(self):
        with self._guard:
            return [c for c in self._conns if c.open]

    def _subscribe_quotes(self, conn, epics, reply):
        known = [e for e in epics if self.market.find(e)]
        taken = set().union(*(c.quotes for c in self._login_conns() if c is not conn)) if len(self._conns) > 1 else set()
        if len(taken | conn.quotes | set(known)) > self.max_epics:
            # documented: https://open-api.capital.com/ ("The maximum number of epics: 40"). stand-in's own: the cap counts
            # every connection of the login, and a request that would pass it is refused whole
            reply("ERROR", {"errorCode": "error.invalid.epics.size"})
            return
        conn.quotes |= set(known)
        # documented: https://open-api.capital.com/ ({"subscriptions": {"OIL_CRUDE": "PROCESSED"}}); "ERROR" for an epic
        # that does not exist is the stand-in's own
        reply("OK", {"subscriptions": {e: ("PROCESSED" if e in known else "ERROR") for e in epics}})

    def _subscribe_ohlc(self, conn, epics, payload, reply):
        resolutions = payload.get("resolutions") or ["MINUTE"]      # documented default MINUTE
        kind = payload.get("type") or "classic"                    # documented default classic
        if not isinstance(resolutions, list) or any(r not in RESOLUTIONS for r in resolutions):
            reply("ERROR", {"errorCode": "error.invalid.resolution"})   # code documented for REST /prices; here stand-in's own
            return
        if kind not in ("classic", "heikin-ashi"):
            reply("ERROR", {"errorCode": "error.invalid.type"})          # stand-in's own
            return
        known = [e for e in epics if self.market.find(e)]
        taken = set()
        for c in self._login_conns():
            taken |= {k[0] for k in c.ohlc}
        if len(taken | set(known)) > self.max_epics:
            reply("ERROR", {"errorCode": "error.invalid.epics.size"})   # as for quotes; stand-in's own
            return
        subs = {}
        for e in epics:
            for r in resolutions:
                if e in known:
                    conn.ohlc.add((e, r, kind))
                subs["%s:%s:%s" % (e, r, kind)] = "PROCESSED" if e in known else "ERROR"
        # documented: https://open-api.capital.com/ ({"subscriptions": {"OIL_CRUDE:MINUTE_5:classic": "PROCESSED"}})
        reply("OK", {"subscriptions": subs})

    def _unsubscribe_ohlc(self, conn, epics, payload, reply):
        # documented: https://open-api.capital.com/ (name an epic and every resolution and type of it goes unless
        # `resolutions` or `types` narrow it; the answer lists what was removed)
        res = payload.get("resolutions") or list(RESOLUTIONS)
        kinds = payload.get("types") or ["classic", "heikin-ashi"]
        gone = sorted(k for k in conn.ohlc if k[0] in epics and k[1] in res and k[2] in kinds)
        for k in gone:
            conn.ohlc.discard(k)
            conn.bars.pop(k, None)
        reply("OK", {"subscriptions": {"%s:%s:%s" % k: "PROCESSED" for k in gone}})

    # -- one REST request
    def _handle(self, h):
        parts = urlsplit(h.path)
        path, method = parts.path, h.command
        q = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        try:
            data = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        except (ValueError, OSError):
            data = b""
        if path.startswith("/_control/"):
            try:
                body = json.loads(data) if data.strip() else {}
                if not isinstance(body, dict):
                    raise ValueError("the body must be a JSON object")
                status, out = 200, self._control(method, path[10:].strip("/"), q, body)
            except (KeyError, ValueError, TypeError) as e:
                status, out = 400, {"error": str(e) or e.__class__.__name__}
            return self._send(h, status, {"Content-Type": _JSON}, out)
        try:
            status, headers, out, lost = self._answer(method, path, q, data, h.headers)
        except Exception:                  # a fault of the stand-in itself: said aloud, and answered rather than dropped
            traceback.print_exc()
            status, headers, out, lost = 500, {"Content-Type": _JSON}, {"errorCode": "error.internal"}, False
        self._record(method, path, parts.query, status, h.headers, data)
        if lost:
            h.close_connection = True
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        self._send(h, status, headers, out)

    def _record(self, method, path, query, status, headers, data):
        body = None
        if not path.rstrip("/").endswith("/session"):          # a session body holds the password: never kept
            try:
                body = json.loads(data) if data.strip() else None
            except ValueError:
                body = "<not JSON>"
        with self._guard:
            self._seq += 1
            # never a credential: only whether each header came
            self._log.append({"seq": self._seq, "at": stamp(self.broker.now()), "method": method, "path": path, "query": query,
                              "status": status, "has_api_key": bool(headers.get("X-CAP-API-KEY")), "has_cst": bool(headers.get("CST")),
                              "has_security_token": bool(headers.get("X-SECURITY-TOKEN")), "user_agent": headers.get("User-Agent"),
                              "body": body})

    def _send(self, h, status, headers, out):
        body = b"" if out is None else out if isinstance(out, bytes) else json.dumps(out, separators=(",", ":")).encode()
        try:
            h.send_response(status)
            for k, v in headers.items():
                h.send_header(k, v)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        except OSError:
            h.close_connection = True

    def _take(self, items, path, method):
        with self._guard:
            for f in items:
                if f["count"] > 0 and (not f.get("path") or path.startswith(f["path"])) and (not f.get("method") or f["method"] == method):
                    f["count"] -= 1
                    return f
        return None

    def _answer(self, method, path, q, data, headers):
        """(status, headers, body, lost) for one request."""
        ctype = {"Content-Type": _JSON}
        if not path.startswith("/api/v1/"):
            return 404, ctype, {"errorCode": "error.not-found"}, False          # stand-in's own
        sub = path[len("/api/v1"):]
        segs = [unquote(s) for s in sub.split("/") if s]
        route = _route(method, segs)
        if route is None:
            return 404, ctype, {"errorCode": "error.not-found"}, False          # stand-in's own
        if route == "method":
            return 405, ctype, {"errorCode": "error.method.not-allowed"}, False # stand-in's own
        name, access = route
        b = self.broker
        try:
            body = json.loads(data) if data.strip() else None
        except ValueError:
            body = ValueError
        with b.lock:
            sess = acct = None
            if access == "session":
                sess = b.session_for(headers.get("CST"), headers.get("X-SECURITY-TOKEN"))
                if sess is None:
                    # stand-in's own code; the 401 for a lapsed session is assumed, verify on demo (Capital.com's own
                    # client treats 401 and 403 as an expired session: https://github.com/capital-com-sv/capital-mcp)
                    return 401, ctype, {"errorCode": "error.invalid.session.token"}, False
                acct = b.accounts[sess.account_id]
            fault = self._take(self._faults, sub, method)
            if fault is not None:
                if fault["status"] == 429:
                    extra = {"Retry-After": str(fault["retry_after"])} if fault.get("retry_after") is not None else {}
                    return 429, dict(ctype, **extra), {"errorCode": "error.too-many.requests"}, False
                if fault.get("page"):
                    return fault["status"], {"Content-Type": "text/html"}, _gateway_page(fault["status"]), False
                return fault["status"], ctype, {"errorCode": "error.internal"}, False     # stand-in's own body
            now = b.now()
            if access == "session":
                if not b.w_user.ok(now):
                    # documented: https://open-api.capital.com/ ("The maximum request rate is 10 per second per user");
                    # the 429 body is the documented one for POST /session (Postman), assumed elsewhere
                    return 429, ctype, {"errorCode": "error.too-many.requests"}, False
                creating = method == "POST" and segs in (["positions"], ["workingorders"])
                if creating:
                    if b.w_spacing is not None and not b.w_spacing.ok(now):
                        # documented: https://open-api.capital.com/ ("1 request per 0.1 seconds per user when opening
                        # positions or creating orders. Otherwise the position/order requests are going to be rejected")
                        return 429, ctype, {"errorCode": "error.too-many.requests"}, False
                    if not b.w_hour.ok(now):
                        # documented: https://open-api.capital.com/ ("POST /positions and POST /workingorders: 1000 requests
                        # per hour" on Demo)
                        return 429, ctype, {"errorCode": "error.too-many.requests"}, False
                    if b.w_spacing is not None:
                        b.w_spacing.add(now)
                    b.w_hour.add(now)
                b.w_user.add(now)
            refusal = self._take(self._refusals, sub, method)
            if refusal is not None:
                return refusal["status"], ctype, {"errorCode": refusal["code"]}, False
            if body is ValueError:
                return 400, ctype, {"errorCode": "error.invalid.request"}, False      # stand-in's own
            if name not in _MARKET_DATA:
                status, out, extra = self._run(name, segs, q, body, sess, acct, headers)
        if name in _MARKET_DATA:                       # read-only and slow to build: outside the broker's lock
            status, out, extra = self._run(name, segs, q, body, sess, acct, headers)
        lost = self._take(self._losses, sub, method) is not None
        if isinstance(out, tuple):                     # (content type, body)
            ctype = {"Content-Type": out[0]}
            out = out[1]
        return status, dict(ctype, **extra), out, lost

    def _run(self, name, segs, q, body, sess, acct, headers):
        try:
            return getattr(self, "_r_" + name)(segs, q, body, sess, acct, headers)
        except CapError as e:
            return e.status, {"errorCode": e.code}, {}

    # -- the routes: each answers (status, body, extra headers)
    def _r_time(self, segs, q, body, sess, acct, headers):
        return 200, {"serverTime": self.broker.now()}, {}      # documented: https://open-api.capital.com/ (GET /time, no session)

    def _r_ping(self, segs, q, body, sess, acct, headers):
        return 200, {"status": "OK"}, {}                       # documented: https://open-api.capital.com/ (GET /ping)

    def _r_encryption_key(self, segs, q, body, sess, acct, headers):
        key = headers.get("X-CAP-API-KEY")
        if not key:
            raise CapError(400, "error.null.api.key")         # the documented POST /session refusal; assumed here
        if key != self.broker.api_key or self.broker.key_revoked:
            raise CapError(401, "error.invalid.api.key")      # stand-in's own
        # documented: https://open-api.capital.com/ ({"encryptionKey": <base64 RSA public key>, "timeStamp": ms}).
        # stand-in's own: the key is a placeholder, not a usable RSA key
        return 200, {"encryptionKey": base64.b64encode(hashlib.sha512(b"fake-capitalcom-encryption-key").digest() * 4).decode(),
                     "timeStamp": self.broker.now()}, {}

    def _r_session_post(self, segs, q, body, sess, acct, headers):
        return self.broker.login(headers.get("X-CAP-API-KEY"), body)

    def _r_session_get(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.session_view(sess), {}

    def _r_session_put(self, segs, q, body, sess, acct, headers):
        out = self.broker.switch(sess, body)
        # documented: https://open-api.capital.com/ ("WebSocket streaming falls off when the financial account is changed")
        threading.Thread(target=self._close_sessions, args=([sess.cst],), daemon=True).start()
        return 200, out, {}

    def _r_session_delete(self, segs, q, body, sess, acct, headers):
        out = self.broker.logout(sess)          # its streams end at the next engine pass (option stream_ends_with_session)
        # documented: https://open-api.capital.com/ (DELETE /session answers {"status": "SUCCESS"} as text/plain)
        return 200, ("text/plain", json.dumps(out, separators=(",", ":")).encode()), {}

    def _r_accounts(self, segs, q, body, sess, acct, headers):
        b, now = self.broker, self.broker.now()
        # documented: https://open-api.capital.com/ (GET /accounts sample, field for field)
        return 200, {"accounts": [{"accountId": a.id, "accountName": a.name, "status": a.status, "accountType": a.account_type,
                                   "preferred": a.preferred, "balance": b.figures(a, now), "currency": a.currency, "symbol": a.symbol}
                                  for a in b.accounts.values()]}, {}

    def _r_prefs_get(self, segs, q, body, sess, acct, headers):
        # documented: https://open-api.capital.com/ (GET /accounts/preferences sample)
        return 200, {"hedgingMode": acct.hedging,
                     "leverages": {k: {"current": acct.leverage[k], "available": list(av)} for k, _, av in DEFAULT_LEVERAGE}}, {}

    def _r_prefs_put(self, segs, q, body, sess, acct, headers):
        if not isinstance(body, dict):
            raise CapError(400, "error.invalid.request")
        lev = body.get("leverages") or {}
        allowed = {k: av for k, _, av in DEFAULT_LEVERAGE}
        if not isinstance(lev, dict) or any(k not in allowed or v not in allowed[k] for k, v in lev.items()):
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Invalid leverage value": 400
            # error.invalid.leverage.value)
            raise CapError(400, "error.invalid.leverage.value")
        hedging = body.get("hedgingMode", acct.hedging)
        if not isinstance(hedging, bool):
            raise CapError(400, "error.invalid.request")
        acct.leverage.update(lev)
        acct.hedging = hedging
        return 200, {"status": "SUCCESS"}, {}                  # documented: https://open-api.capital.com/

    def _r_topup(self, segs, q, body, sess, acct, headers):
        """documented: https://open-api.capital.com/ (POST /accounts/topUp {"amount"}: min -400000, max 400000; "10
        requests per second, 100 requests per account per day"; "The balance of the Demo account cannot exceed 100000";
        answers {"successful": true})"""
        now = self.broker.now()
        try:
            amount = dec((body or {}).get("amount"))
        except (ValueError, AttributeError):
            raise CapError(400, "error.invalid.amount")        # stand-in's own
        w_s = [t for t in acct.topups_s if t > now - 1000]
        w_d = [t for t in acct.topups_day if t > now - DAY_MS]
        acct.topups_s, acct.topups_day = collections.deque(w_s), collections.deque(w_d)
        if len(w_s) >= self.topups_per_s or len(w_d) >= self.topups_per_day:
            raise CapError(429, "error.too-many.requests")     # body assumed as for POST /session
        if not (-400000 <= amount <= 400000) or acct.cash + amount > 100000 or acct.cash + amount < 0:
            raise CapError(400, "error.invalid.amount")        # stand-in's own code
        acct.topups_s.append(now)
        acct.topups_day.append(now)
        acct.cash += money(amount)
        # assumed, verify on demo: a top-up is a DEPOSIT (a cut a WITHDRAWAL) in the transaction history
        self.broker._transaction(acct, "DEPOSIT" if amount >= 0 else "WITHDRAWAL", "Demo top-up", "", money(amount), now)
        return 200, {"successful": True}, {}

    def _r_activity(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.activity(acct, q), {}

    def _r_transactions(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.transactions(acct, q), {}

    def _r_confirm(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.confirm(segs[1]), {}

    def _r_positions(self, segs, q, body, sess, acct, headers):
        now = self.broker.now()
        return 200, {"positions": [self.broker.position_view(acct, d, now) for d in acct.deals.values()]}, {}

    def _r_position_open(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.open_position(acct, body), {}

    def _r_position_get(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.position_view(acct, self.broker.get_deal(acct, segs[1]), self.broker.now()), {}

    def _r_position_put(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.amend_position(acct, segs[1], body), {}

    def _r_position_delete(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.close_position(acct, segs[1]), {}

    def _r_orders(self, segs, q, body, sess, acct, headers):
        now = self.broker.now()
        return 200, {"workingOrders": [self.broker.order_view(acct, o, now) for o in acct.orders.values()]}, {}

    def _r_order_open(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.place_order(acct, body), {}

    def _r_order_put(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.amend_order(acct, segs[1], body), {}

    def _r_order_delete(self, segs, q, body, sess, acct, headers):
        return 200, self.broker.cancel_order(acct, segs[1]), {}

    def _r_markets(self, segs, q, body, sess, acct, headers):
        b, now = self.broker, self.broker.now()
        if "searchTerm" in q:            # documented: https://open-api.capital.com/ (searchTerm wins over epics)
            term = q["searchTerm"].strip().lower()
            if not term:
                # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Empty searchTerm parameter": 400)
                raise CapError(400, "error.null.searchTerm")
            # stand-in's own matching: the term inside the epic, the symbol or the name
            found = [i for i in self.market.instruments if term in i.epic.lower() or term in i.name.lower() or term in i.symbol.lower()]
            return 200, {"markets": [b.market_row(i, now, ROW_LIST, acct) for i in found]}, {}
        if "epics" in q:
            epics = [e for e in q["epics"].split(",") if e]
            if not epics:
                raise CapError(400, "error.null.epic")   # documented: https://github.com/capital-com-sv/capital-api-postman
            if len(epics) > 50:
                raise CapError(400, "error.size.epics")  # documented limit (50 epics); the code is the stand-in's own
            found = [self.market.find(e) for e in epics]
            found = [i for i in found if i is not None]
            if not found:
                raise CapError(404, "error.not-found.epic")   # stand-in's own (an unknown epic among known ones is left out)
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Successful response: epics": marketDetails)
            return 200, {"marketDetails": [self._details(i, now, acct) for i in found]}, {}
        # documented: https://open-api.capital.com/ (changelog 2023-08-04: with no parameter, every market)
        return 200, {"markets": [b.market_row(i, now, ROW_LIST, acct) for i in self.market.instruments]}, {}

    def _details(self, inst, now, acct):
        f = self.market.fields(inst, now, acct.leverage[inst.type])

        def rule(unit_value):
            return {"unit": unit_value[0], "value": unit_value[1]}
        # documented: https://open-api.capital.com/ (GET /markets/{epic} sample, field for field)
        return {
            "instrument": {"epic": inst.epic, "symbol": inst.symbol, "expiry": "-", "name": inst.name, "lotSize": inst.lot,
                           "type": inst.type, "guaranteedStopAllowed": inst.gsl_allowed, "streamingPricesAvailable": True,
                           "currency": inst.currency, "marginFactor": rnd(f["_margin"], 2), "marginFactorUnit": "PERCENTAGE",
                           "openingHours": {k: list(v) if isinstance(v, list) else v for k, v in inst.hours.items()},
                           "overnightFee": {"longRate": inst.long_rate, "shortRate": inst.short_rate,
                                            "swapChargeTimestamp": Broker._next_swap(now), "swapChargeInterval": 1440}},
            "dealingRules": {"minStepDistance": rule(("POINTS", rnd(inst.tick, inst.dp))), "minDealSize": rule(("POINTS", num(inst.min_deal))),
                             "maxDealSize": rule(("POINTS", num(inst.max_deal))), "minSizeIncrement": rule(("POINTS", num(inst.step))),
                             "minGuaranteedStopDistance": rule(inst.gsl_min), "minStopOrProfitDistance": rule(inst.stop_min),
                             "maxStopOrProfitDistance": rule(inst.stop_max), "marketOrderPreference": "AVAILABLE_DEFAULT_ON",
                             "trailingStopsPreference": inst.trailing},
            "snapshot": {"marketStatus": f["marketStatus"], "netChange": f["netChange"], "percentageChange": f["percentageChange"],
                         "updateTime": f["updateTime"], "delayTime": 0, "bid": f["bid"], "offer": f["offer"], "high": f["high"],
                         "low": f["low"], "decimalPlacesFactor": inst.dp, "scalingFactor": 1, "marketModes": f["marketModes"]},
        }

    def _r_market(self, segs, q, body, sess, acct, headers):
        inst = self.market.find(segs[1])
        if inst is None:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Market not found": 404 error.not-found.epic)
            raise CapError(404, "error.not-found.epic")
        return 200, self._details(inst, self.broker.now(), acct), {}

    def _r_prices(self, segs, q, body, sess, acct, headers):
        """GET /prices/{epic}. documented: https://open-api.capital.com/ (resolution, default MINUTE; max, default 10, at
        most 1000; from and to "Filtration by date based on snapshotTimeUTC"; with no parameter "the minute prices within
        the last 10 minutes"; bars carry openPrice, closePrice, highPrice and lowPrice as {bid, ask} and lastTradedVolume)"""
        inst = self.market.find(segs[1])
        if inst is None:
            raise CapError(404, "error.not-found.epic")       # documented code; for /prices assumed, verify on demo
        res = q.get("resolution", "MINUTE")
        if res not in RESOLUTIONS:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Invalid resolution parameter")
            raise CapError(400, "error.invalid.resolution")
        try:
            limit = int(q.get("max", 10))
        except ValueError:
            raise CapError(400, "error.invalid.max")          # stand-in's own
        if not 1 <= limit <= 1000:
            raise CapError(400, "error.invalid.max")          # documented max 1000; refusing more is the stand-in's own
        frm = to = None
        try:
            frm = parse_when(q["from"]) if q.get("from") else None
        except ValueError:
            raise CapError(400, "error.invalid.from")         # the documented activity code; for /prices assumed
        try:
            to = parse_when(q["to"]) if q.get("to") else None
        except ValueError:
            raise CapError(400, "error.invalid.to")           # stand-in's own
        if frm is not None and to is not None and frm > to:
            raise CapError(400, "error.invalid.daterange")    # stand-in's own
        opts = self.broker.options
        if frm is not None and to is not None:
            prefer = opts["prices_overflow"]
        elif frm is not None:
            prefer = "oldest"            # documented by the one example: `from` with `max` answers the bars from `from`
        else:
            prefer = "newest"            # assumed, verify on demo: `to` alone (or nothing) answers the newest before it
        now = self.broker.now()
        total, bars = self.market.bars(inst.epic, res, frm, to, limit, "newest" if prefer == "error" else prefer, now,
                                       forming=opts["prices_forming"])
        if prefer == "error" and total > limit:
            raise CapError(400, "error.invalid.max.daterange")   # stand-in's own (the "error" reading of an over-full range)
        if not bars and opts["prices_empty"] == "error":
            raise CapError(404, "error.prices.not-found")         # assumed, verify on demo
        local = self.market.local_ms
        prices = [{"snapshotTime": stamp(t + local, False), "snapshotTimeUTC": stamp(t, False),
                   "openPrice": {"bid": b[0], "ask": a[0]}, "closePrice": {"bid": b[3], "ask": a[3]},
                   "highPrice": {"bid": b[1], "ask": a[1]}, "lowPrice": {"bid": b[2], "ask": a[2]}, "lastTradedVolume": v}
                  for t, b, a, v in bars]
        return 200, {"prices": prices, "instrumentType": inst.type}, {}

    # market navigation: stand-in's own tree, in the documented shapes (https://open-api.capital.com/: GET
    # /marketnavigation gives {"nodes": [{"id", "name"}]}; a node gives sub-nodes, or its markets)
    _NAV = (
        ("hierarchy_v1.commons_group", "commons_group", (
            ("hierarchy_v1.commons.most_traded", "Most Traded", ("EURUSD", "US500", "GOLD", "AAPL", "BTCUSD")),
            ("hierarchy_v1.commons.recently_traded", "Recently Traded", ()),
            ("hierarchy_v1.commons.new", "New", ("AUDCAD",)),
            ("hierarchy_v1.commons.top_gainers", "Top Risers", ("NVDA", "TSLA")),
            ("hierarchy_v1.commons.top_losers", "Top Fallers", ("INTC",)),
            ("hierarchy_v1.commons.most_volatile", "Most Volatile", ("NATURALGAS", "XRPUSD")),
            ("hierarchy_v1.commons.weekend_trading", "Weekend Trading", ("BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD")))),
        ("hierarchy_v1.currencies_group", "currencies_group", (
            ("hierarchy_v1.currencies.majors", "Majors", ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD")),
            ("hierarchy_v1.currencies.crosses", "Crosses", ("EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "EURAUD", "GBPCHF", "CADJPY", "AUDCAD")))),
        ("hierarchy_v1.indices_group", "indices_group", (
            ("hierarchy_v1.indices.major", "Major indices", ("US500", "US100", "US30", "UK100", "DE40", "FR40", "J225", "AU200")),)),
        ("hierarchy_v1.commodities_group", "commodities_group", (
            ("hierarchy_v1.commodities.metals", "Metals", ("GOLD", "SILVER", "COPPER", "PLATINUM")),
            ("hierarchy_v1.commodities.energy", "Energy", ("OIL_CRUDE", "OIL_BRENT", "NATURALGAS")))),
        ("hierarchy_v1.oil_markets_group", "oil_markets_group", (
            ("hierarchy_v1.oil_markets.all", "Oil", ("OIL_CRUDE", "OIL_BRENT")),)),
        ("hierarchy_v1.shares_group", "shares_group", (
            ("hierarchy_v1.shares.us", "US shares", ("AAPL", "MSFT", "TSLA", "AMZN", "NVDA", "META", "GOOGL", "NFLX", "AMD", "INTC")),
            ("hierarchy_v1.shares.asia", "Asian shares", ("5CPSG",)))),
        ("hierarchy_v1.cryptocurrencies_group", "cryptocurrencies_group", (
            ("hierarchy_v1.cryptocurrencies.all", "Cryptocurrencies", ("BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD")),)),
    )

    def _r_nav(self, segs, q, body, sess, acct, headers):
        if len(segs) == 1:
            return 200, {"nodes": [{"id": i, "name": n} for i, n, _ in self._NAV]}, {}
        try:
            limit = int(q.get("limit", 500))
        except ValueError:
            raise CapError(400, "error.invalid.limit")
        if not 1 <= limit <= 500:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Invalid limit parameter", limit=0);
            # https://open-api.capital.com/ (limit default 500, max 500)
            raise CapError(400, "error.invalid.limit")
        node, now = segs[1], self.broker.now()
        for gid, _, kids in self._NAV:
            if node == gid:
                return 200, {"nodes": [{"id": i, "name": n} for i, n, _ in kids][:limit]}, {}
            for kid, _, epics in kids:
                if node == kid:
                    rows = [self.broker.market_row(self.market.by_epic[e], now, ROW_NAV, acct) for e in epics[:limit]]
                    return 200, {"nodes": [], "markets": rows}, {}
        raise CapError(404, "error.not-found.nodeId")              # stand-in's own

    def _sentiment(self, epic):
        long_ = rnd(30 + 60 * _u(str(self.seed), "sentiment", epic), 2)
        # documented: https://open-api.capital.com/ ({"marketId", "longPositionPercentage", "shortPositionPercentage"});
        # the figures are the stand-in's own
        return {"marketId": epic, "longPositionPercentage": long_, "shortPositionPercentage": rnd(100 - long_, 2)}

    def _r_sentiments(self, segs, q, body, sess, acct, headers):
        ids = [e for e in (q.get("marketIds") or "").split(",") if e]
        found = [e for e in ids if self.market.find(e)]
        if not found:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Market not found", marketIds=NOTFOUND)
            raise CapError(404, "error.not-found.epic")
        return 200, {"clientSentiments": [self._sentiment(e) for e in found]}, {}

    def _r_sentiment(self, segs, q, body, sess, acct, headers):
        if not self.market.find(segs[1]):
            raise CapError(404, "error.not-found.epic")           # documented (Postman)
        return 200, self._sentiment(segs[1]), {}

    # watchlists, in the documented shapes: https://open-api.capital.com/ and the Postman examples
    def _r_watchlists(self, segs, q, body, sess, acct, headers):
        return 200, {"watchlists": [{"id": i, "name": w["name"], "editable": True, "deleteable": True, "defaultSystemWatchlist": False}
                                    for i, w in self.broker.watchlists.items()]}, {}

    def _r_watchlist_new(self, segs, q, body, sess, acct, headers):
        name = (body or {}).get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not 1 <= len(name) <= 20:
            # documented: https://github.com/capital-com-sv/capital-api-postman ("Error: Incorrect name length")
            raise CapError(400, "error.size.createwatchlistrequest.name")
        epics = body.get("epics") or []
        good = [e for e in epics if isinstance(e, str) and self.market.find(e)]
        b = self.broker
        b._watch_ids += 1
        wid = str(b._watch_ids)
        b.watchlists[wid] = {"name": name, "epics": list(dict.fromkeys(good))}
        # documented: https://github.com/capital-com-sv/capital-api-postman ("SUCCESS", or "SUCCESS_NOT_ALL_INSTRUMENTS_ADDED")
        return 200, {"watchlistId": wid, "status": "SUCCESS" if len(good) == len(epics) else "SUCCESS_NOT_ALL_INSTRUMENTS_ADDED"}, {}

    def _watchlist(self, wid, code="error.invalid.watchlist"):
        w = self.broker.watchlists.get(wid)
        if w is None:
            raise CapError(404, code)
        return w

    def _r_watchlist_get(self, segs, q, body, sess, acct, headers):
        w = self._watchlist(segs[1])                               # documented: 404 error.invalid.watchlist (Postman)
        now = self.broker.now()
        return 200, {"markets": [self.broker.market_row(self.market.by_epic[e], now, ROW_WATCH, acct) for e in w["epics"]]}, {}

    def _r_watchlist_add(self, segs, q, body, sess, acct, headers):
        w = self._watchlist(segs[1])                               # stand-in's own code for an unknown watchlist here
        epic = body.get("epic") if isinstance(body, dict) else None
        if not self.market.find(epic):
            raise CapError(404, "error.not-found.epic")            # documented (Postman, "Error: Instrument not found")
        if epic not in w["epics"]:
            w["epics"].append(epic)
        return 200, {"status": "SUCCESS"}, {}

    def _r_watchlist_delete(self, segs, q, body, sess, acct, headers):
        self._watchlist(segs[1], "error.not-found.watchlist")     # documented (Postman, "Error: Watchlist not found")
        del self.broker.watchlists[segs[1]]
        return 200, {"status": "SUCCESS"}, {}

    def _r_watchlist_remove(self, segs, q, body, sess, acct, headers):
        w = self._watchlist(segs[1], "error.not-found.watchlist")
        if segs[2] not in w["epics"]:
            raise CapError(404, "error.not-found.epic")            # documented (Postman, "Instrument not found in the watchlist")
        w["epics"].remove(segs[2])
        return 200, {"status": "SUCCESS"}, {}


def _route(method, segs):
    """(handler name, access) for a path under /api/v1, None when there is no such path, "method" when the path exists
    but not with this method. access: public | login | key | session."""
    n = len(segs)
    head = segs[0] if segs else ""
    table = None
    if n == 1:
        table = {
            "time": {"GET": ("time", "public")},
            "ping": {"GET": ("ping", "session")},
            "session": {"GET": ("session_get", "session"), "POST": ("session_post", "login"), "PUT": ("session_put", "session"),
                        "DELETE": ("session_delete", "session")},
            "accounts": {"GET": ("accounts", "session")},
            "positions": {"GET": ("positions", "session"), "POST": ("position_open", "session")},
            "workingorders": {"GET": ("orders", "session"), "POST": ("order_open", "session")},
            "marketnavigation": {"GET": ("nav", "session")},
            "markets": {"GET": ("markets", "session")},
            "clientsentiment": {"GET": ("sentiments", "session")},
            "watchlists": {"GET": ("watchlists", "session"), "POST": ("watchlist_new", "session")},
        }.get(head)
    elif n == 2:
        if segs == ["session", "encryptionKey"]:
            table = {"GET": ("encryption_key", "key")}
        elif segs == ["accounts", "preferences"]:
            table = {"GET": ("prefs_get", "session"), "PUT": ("prefs_put", "session")}
        elif segs == ["accounts", "topUp"]:
            table = {"POST": ("topup", "session")}
        elif segs == ["history", "activity"]:
            table = {"GET": ("activity", "session")}
        elif segs == ["history", "transactions"]:
            table = {"GET": ("transactions", "session")}
        else:
            table = {
                "confirms": {"GET": ("confirm", "session")},
                "positions": {"GET": ("position_get", "session"), "PUT": ("position_put", "session"), "DELETE": ("position_delete", "session")},
                "workingorders": {"PUT": ("order_put", "session"), "DELETE": ("order_delete", "session")},
                "marketnavigation": {"GET": ("nav", "session")},
                "markets": {"GET": ("market", "session")},
                "prices": {"GET": ("prices", "session")},
                "clientsentiment": {"GET": ("sentiment", "session")},
                "watchlists": {"GET": ("watchlist_get", "session"), "PUT": ("watchlist_add", "session"), "DELETE": ("watchlist_delete", "session")},
            }.get(head)
    elif n == 3 and head == "watchlists":
        table = {"DELETE": ("watchlist_remove", "session")}
    if table is None:
        return None
    return table.get(method) or "method"


def main(argv=None):
    ap = argparse.ArgumentParser(description="A local stand-in for Capital.com's Public API v1 (demo), REST and stream, 127.0.0.1 only.")
    ap.add_argument("--port", type=int, default=0, help="the REST port; 0 picks a free one; both ports are printed")
    ap.add_argument("--stream-port", type=int, default=0, help="the WebSocket port; 0 picks a free one")
    ap.add_argument("--seed", type=int, default=7, help="fixes every price and every id")
    ap.add_argument("--market", choices=("auto", "open", "closed"), default="auto",
                    help="auto (the start) follows each market's openingHours; open or closed holds every market so")
    ap.add_argument("--balance", default="1000", help="the USD demo account's cash at the start, 1000 as on a new demo (the GBP one has 1000)")
    ap.add_argument("--partial-rule", action="append", default=[], metavar="EPIC:SIZE",
                    help="every order of that size on that epic fills in two parts (for the conformance --partial-hint)")
    ap.add_argument("--confirm-delay", type=float, default=0.1, help="seconds before a confirmation appears in /confirms")
    args = ap.parse_args(argv)
    fake = FakeCapitalCom(seed=args.seed, market=args.market, port=args.port, stream_port=args.stream_port,
                          balances=(("USD", args.balance), ("GBP", "1000")), confirm_delay=args.confirm_delay).start()
    for rule in args.partial_rule:
        epic, _, size = rule.partition(":")
        fake.fill_rule(float(size), [1, 1], epic)
    print(json.dumps({"port": fake.port, "stream_port": fake.stream_port, "rest_url": fake.rest_url, "api_url": fake.api_url,
                      "streaming_host": fake.streaming_host, "stream_url": fake.stream_url, "control_url": fake.control_url,
                      "api_key": fake.api_key, "identifier": fake.identifier, "password": fake.password, "accounts": fake.accounts,
                      "seed": args.seed, "market": args.market}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
