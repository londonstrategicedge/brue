#!/usr/bin/env python3
"""A local stand-in for Alpaca: paper trading REST, market-data REST and the order stream, on one loopback port.

Built from what Alpaca's paper API was seen to answer on 2026-11-10 (statuses, bodies, field order, numbers as
strings, event order) and, where nothing was seen, from Alpaca's documentation; those places carry the comment
"documented, not observed". Places that are neither say "stub's own". Pure standard library, Python 3.10+.

Brought into line on 2026-11-10 (17:50 to 18:25 UTC) with what was then seen on paper: a fill's id in the ledger IS
its execution id on the stream; a `held` leg is never a row of a status=open order list, and the held stop of a
bracket whose entry has filled is in none (it is found by id or under the closed parent); DELETE /v2/orders names the
rows that list shows and a held leg goes with its sibling; a covering buy is refused while a buy order holds the
short; a leg's `held` event comes before its entry's pending_new; a firing stop is not cancelled together with the
limit it hangs under; DELETE /v2/positions with positions open answers one {"symbol", "status", "body"} row per
position.

Brought into line on 2026-11-11 with what was seen on paper that night (04:54 UTC): an order whose
`position_intent` says CLOSE (`sell_to_close`, `buy_to_close`) and that has nothing on that side to close is refused,
422 {"code": 42210000, "message": "position intent mismatch, inferred: sell_to_open, specified: sell_to_close"}, at
once, also with the exchange shut; for crypto the balance check answers first (403 insufficient balance). An order
without the field, or marked `..._to_open`, is taken as before. DELETE /v2/positions/{symbol} with nothing held is 404
"position not found: SYMBOL". NOT seen, for shares: an order marked as a close for MORE than is held, and one marked
as a close while other working orders hold the position's quantity. For those two the stand-in takes the case that is
WORST for whoever relies on the mark: it ACCEPTS the order and trades it (the first through zero, into the other
side). `set_intent(kind=True)` (POST /_control/intent) is the other guess: such an order is held to what is free, as
an unmarked one is ("insufficient qty available"). Where the mismatch stands among the other checks is the stub's own
(for a share: before the client_order_id is looked at, so a close sent twice under one id is told "mismatch" once the
first has filled, not "must be unique").

    python tests/fake_alpaca.py --port 0            # prints {"port": N, ...} and serves until killed
    ALPACA_TRADING_URL=http://127.0.0.1:N  ALPACA_DATA_URL=http://127.0.0.1:N  ALPACA_STREAM_URL=ws://127.0.0.1:N/stream

    fake = FakeAlpaca(key_id="k", secret="s").start()   # in a test; fake.port, fake.url, fake.stream_url
    ...; fake.stop()

Served as Alpaca serves them (the two APCA-API-* headers, or HTTP Basic):
  trading   GET /v2/account  /v2/clock  /v2/calendar  /v2/assets[/{symbol or id}]
            POST|GET|DELETE /v2/orders   GET|PATCH|DELETE /v2/orders/{id}   GET /v2/orders:by_client_order_id
            GET|DELETE /v2/positions[/{symbol or id}]   GET /v2/account/activities[/{type}]
  data      GET /v2/stocks/{quotes,trades,bars}/latest  /v2/stocks/snapshots  /v2/stocks/bars
            GET /v2/stocks/{symbol}/{quotes,trades,bars}/latest  /v2/stocks/{symbol}/snapshot  /v2/stocks/{symbol}/bars
            GET /v1beta3/crypto/{loc}/latest/{quotes,trades,bars}  /v1beta3/crypto/{loc}/snapshots  /v1beta3/crypto/{loc}/bars
  stream    GET /stream (websocket): auth, listen, trade_updates; every frame the server sends is BINARY

Control, for tests (no key needed, never rate limited, never in the request log; each is also a method of FakeAlpaca):
  POST /_control/price    {"symbol", "price"} hold the price there | {"symbol", "jump_pct"} | {"symbol",
                          "trend_pct_per_s", "seconds"} | {"symbol", "walk": true} resume the seeded walk; optional "spread"
  POST /_control/market   {"state": "open" | "shut" | "auto"}        auto follows the calendar
  POST /_control/clock    {"advance_s": N} | {"now": "RFC 3339"} | {"reset": true}     moves the server's clock
  POST /_control/fills    {"clips": N, "interval_s": x} the next order to fill does so in N partial clips;
                          {"delay_s": y} every fill waits y seconds from the moment the order is marketable
  POST /_control/intent   {"kind": false | true}   an order marked as a close for more than is free to close (NOT observed):
                          false (the start) = accepted and traded, the worst case; true = refused as an unmarked one is
  POST /_control/drop_streams   {"clean": false}                      cuts every websocket connection
  POST /_control/fail     {"status": 429 | 500, "count": K, "retry_after": 1, "path": "/v2/orders"}   the next K requests
  POST /_control/reset    {}                                           the account, prices, market and controls as at the start
  POST /_control/settle   {"timeout_s": 5}                             returns when the order engine has nothing due
  GET  /_control/requests[?clear=1]    the request log: method, path, query, status, user_agent, service
  GET  /_control/events                every trade_updates message sent so far
  GET  /_control/state                 market, clock and control settings

The market inside: BTC/USD and ETH/USD (always open) and AAPL MSFT SPY (liquid), XRX (not shortable), DTI (not
fractionable), YI (neither), PMNT (not tradable, no market data), SNDL (priced under 1 USD here). Each price is a
seeded walk fixed by (seed, symbol, time); quotes, trades and bars are all read from that one path.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import calendar as _cal
import functools
import hashlib
import heapq
import json
import queue
import random
import re
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

NS = 10 ** 9
ZERO = Decimal(0)
CENT = Decimal("0.01")
NINE = Decimal("0.000000001")
CRYPTO_FEE = Decimal("0.0025")          # taker fee, tier 1: seen as 0.0002 BTC bought, 0.0001995 held
CRYPTO_MIN_COST = Decimal(10)
CRYPTO_MAX_NOTIONAL = Decimal(200000)
MARKET_COLLAR = Decimal("1.02")         # the notional of a market order is priced 2% through the ask (seen in the 200000 refusal)
CAT_FEE = Decimal("0.000003")           # per share, inside the cost_basis of the buying-power refusal
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------- numbers and time

def dec(v):
    """A number of a request (JSON string or number) as a Decimal; None when it is not a finite number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def num(d):
    """Alpaca's number as a string: plain digits, no exponent, no trailing zeros ("100000", "0.0001995")."""
    if d is None:
        return None
    s = format(Decimal(d), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def cents(d):
    return Decimal(d).quantize(CENT, rounding=ROUND_HALF_UP)


def trunc9(d):
    return Decimal(d).quantize(NINE, rounding=ROUND_DOWN)


def ts(ns, micro=False):
    """RFC 3339 in UTC with the fraction's trailing zeros dropped, as Alpaca prints (0 to 9 digits)."""
    if ns is None:
        return None
    if micro:
        ns = (ns + 500) // 1000 * 1000
    s, f = divmod(ns, NS)
    out = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s))
    if f:
        out += "." + ("%09d" % f).rstrip("0")
    return out + "Z"


def parse_time(text):
    """RFC 3339 or YYYY-MM-DD to epoch nanoseconds; ValueError otherwise."""
    text = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)(?:[Tt ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,9}))?)?\s*([Zz]|[+-]\d\d:?\d\d)?)?", text)
    if not m:
        raise ValueError(text)
    y, mo, d, hh, mi, ss, frac, zone = m.groups()
    secs = _cal.timegm((int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0)))
    if zone and zone not in "Zz":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        secs -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return secs * NS + int((frac or "0").ljust(9, "0"))


def _nth_weekday(year, month, weekday, n):
    """The n-th (1-based) given weekday of a month; n = -1 is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta((weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month, _cal.monthrange(year, month)[1])
    return last - timedelta((last.weekday() - weekday) % 7)


@functools.lru_cache(maxsize=None)
def _dst_bounds(year):
    start = _nth_weekday(year, 3, 6, 2)      # second Sunday of March, 02:00 EST
    end = _nth_weekday(year, 11, 6, 1)       # first Sunday of November, 02:00 EDT
    return (_cal.timegm((year, 3, start.day, 7, 0, 0)), _cal.timegm((year, 11, end.day, 6, 0, 0)))


def ny_offset(epoch_s):
    """New York's offset from UTC in seconds (-14400 or -18000), computed, so no tz database is needed."""
    a, b = _dst_bounds(time.gmtime(epoch_s).tm_year)
    return -14400 if a <= epoch_s < b else -18000


def ny_epoch(d, hh=0, mm=0):
    """A New York wall time as epoch seconds."""
    wall = _cal.timegm((d.year, d.month, d.day, hh, mm, 0))
    return wall + 18000 if ny_offset(wall + 18000) == -18000 else wall + 14400


def ny_date(epoch_s):
    return date(*time.gmtime(epoch_s + ny_offset(epoch_s))[:3])


def ny_stamp(ns):
    """The clock's form: New York offset, not Z ("2026-11-10T07:51:18.511250253-05:00")."""
    s, f = divmod(ns, NS)
    off = ny_offset(s)
    out = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s + off))
    if f:
        out += "." + ("%09d" % f).rstrip("0")
    return out + ("-04:00" if off == -14400 else "-05:00")


# ---------------------------------------------------------------- the exchange calendar (rules; 2026-11 and 2026-12 match the real one)

@functools.lru_cache(maxsize=None)
def _special_days(year):
    def observed(d):
        return d - timedelta(1) if d.weekday() == 5 else d + timedelta(1) if d.weekday() == 6 else d
    a, b, c = year % 19, year // 100, year % 100
    e = (19 * a + b - b // 4 - (b - (b + 8) // 25 + 1) // 3 + 15) % 30
    f = (32 + 2 * (b % 4) + 2 * (c // 4) - e - c % 4) % 7
    g = (a + 11 * e + 22 * f) // 451
    easter = date(year, (e + f - 7 * g + 114) // 31, (e + f - 7 * g + 114) % 31 + 1)
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    closed = {_nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), easter - timedelta(2), _nth_weekday(year, 5, 0, -1),
              observed(date(year, 7, 4)), _nth_weekday(year, 9, 0, 1), thanksgiving, observed(date(year, 12, 25))}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:               # a Saturday New Year's Day is not observed on the Friday
        closed.add(new_year + timedelta(1) if new_year.weekday() == 6 else new_year)
    if year >= 2022:
        closed.add(observed(date(year, 6, 19)))
    early = {thanksgiving + timedelta(1)}
    for d in (date(year, 12, 24), date(year, 7, 3)):
        if d.weekday() < 5 and d not in closed:
            early.add(d)
    return frozenset(closed), frozenset(early)


def trading_day(d):
    return d.weekday() < 5 and d not in _special_days(d.year)[0]


def session(d):
    """(open, close) of the regular session of a New York date in epoch seconds, or None on a closed day."""
    if not trading_day(d):
        return None
    return ny_epoch(d, 9, 30), ny_epoch(d, 13 if d in _special_days(d.year)[1] else 16, 0)


def next_trading_day(d):
    d += timedelta(1)
    while not trading_day(d):
        d += timedelta(1)
    return d


def calendar_row(d):
    early = d in _special_days(d.year)[1]
    return {"close": "13:00" if early else "16:00", "date": d.isoformat(), "open": "09:30",
            "session_close": "1700" if early else "2000", "session_open": "0400", "settlement_date": next_trading_day(d).isoformat()}


def cal_is_open(epoch_s):
    s = session(ny_date(epoch_s))
    return bool(s and s[0] <= epoch_s < s[1])


def cal_next(epoch_s, which):
    """The first session open (which=0) or close (which=1) strictly after an instant."""
    d = ny_date(epoch_s)
    while True:
        s = session(d)
        if s and s[which] > epoch_s:
            return s[which]
        d += timedelta(1)


def cal_prev_close(epoch_s):
    d = ny_date(epoch_s)
    while True:
        s = session(d)
        if s and s[1] <= epoch_s:
            return s[1]
        d -= timedelta(1)


# ---------------------------------------------------------------- the universe (asset rows as Alpaca returned them)

def _share(asset_id, exchange, symbol, name, *, tradable=True, marginable=True, margin=30, shortable=True, fractionable=True, attributes=()):
    return {"id": asset_id, "class": "us_equity", "exchange": exchange, "symbol": symbol, "name": name, "status": "active",
            "tradable": tradable, "marginable": marginable, "maintenance_margin_requirement": margin,
            "margin_requirement_long": str(margin), "margin_requirement_short": str(margin), "shortable": shortable,
            "easy_to_borrow": shortable, "borrow_status": "easy_to_borrow" if shortable else "hard_to_borrow",
            "fractionable": fractionable, "attributes": list(attributes)}


def _coin(asset_id, symbol, name, min_order_size):
    return {"id": asset_id, "class": "crypto", "exchange": "CRYPTO", "symbol": symbol, "name": name, "status": "active",
            "tradable": True, "marginable": False, "maintenance_margin_requirement": 100, "margin_requirement_long": "100",
            "margin_requirement_short": "100", "shortable": False, "easy_to_borrow": False, "fractionable": True,
            "attributes": ["fractional_eh_enabled"], "min_order_size": min_order_size, "min_trade_increment": "0.000000001",
            "price_increment": "0.000000001"}


_FULL = ("fractional_eh_enabled", "has_options", "overnight_tradable")
# (asset row, start price, day volatility, spread in basis points, shares or coins a minute, tape, quote while shut, position asset id)
_UNIVERSE = (
    (_coin("276e2673-764b-4ab6-a611-caf665ca6340", "BTC/USD", "Bitcoin  / US Dollar", "0.000013362"), 74200.0, 0.02, 2.0, 0.004, None, None,
     "64bbff51-59d6-4b3c-9351-13ad85e3c752"),    # a BTCUSD position was seen to carry this id, not the asset's own
    (_coin("a1733398-6acc-4e92-af24-0d0667f78713", "ETH/USD", "Ethereum / US Dollar", "0.000472554"), 2095.0, 0.025, 3.0, 0.15, None, None,
     "35f33a69-f5d6-4dc9-b158-4485e5e92e4b"),    # stub's own: ETH's position id was not seen; made different on purpose
    (_share("b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "NASDAQ", "AAPL", "Apple Inc. Common Stock", attributes=_FULL), 352.5, 0.012, 1.0, 8000, "C", "no_ask", None),
    (_share("b6d1aa75-5c9c-4353-a305-9e2caa1925ab", "NASDAQ", "MSFT", "Microsoft Corporation Common Stock", attributes=_FULL), 540.0, 0.011, 1.0, 5000, "C", "wide", None),
    (_share("b28f4066-5c6d-479b-a2af-85dc1a8f16fb", "ARCA", "SPY", "State Street SPDR S&P 500 ETF Trust",
            attributes=("fractional_eh_enabled", "has_options", "options_late_close", "overnight_tradable")), 749.5, 0.007, 0.5, 12000, "B", "wide", None),
    (_share("d1111e66-96bb-49e0-a0f8-f4c060cc8011", "NASDAQ", "XRX", "Xerox Holdings Corporation Common Stock", shortable=False, attributes=_FULL), 6.4, 0.02, 20.0, 900, "C", "no_ask", None),
    (_share("1be61b94-6235-4958-8120-cf59087e9d04", "NASDAQ", "DTI", "Drilling Tools International Corporation Common Stock", fractionable=False,
            attributes=("overnight_halted",)), 3.1, 0.02, 40.0, 300, "C", "no_ask", None),
    (_share("0de5f5f1-106b-40b5-a8cf-8eb9445df335", "NASDAQ", "YI", "111, Inc. American Depositary Shares", shortable=False, fractionable=False,
            attributes=("has_options", "overnight_halted")), 8.5, 0.02, 30.0, 200, "C", "no_ask", None),
    (_share("0aaf0a2f-4d4e-424f-9531-de90a2598511", "OTC", "PMNT", "Perfect Moment Ltd. Common Stock", tradable=False, marginable=False, margin=100,
            shortable=False, fractionable=False), 0.3, 0.02, 100.0, 0, None, None, None),
    (_share("2f75233a-66c2-4426-ab94-b2e7185dce9a", "NASDAQ", "SNDL", "SNDL Inc. Common Shares", margin=100, attributes=_FULL), 0.85, 0.006, 25.0, 4000, "C", "no_ask", None),
)   # SNDL's row is real; its price here (under 1 USD) is the stub's own: it traded at 1.395 on 2026-11-09


class Instrument:
    def __init__(self, row, price, day_vol, spread_bps, volume, tape, shut_quote, position_id, seed):
        self.row, self.symbol, self.id = row, row["symbol"], row["id"]
        self.crypto = row["class"] == "crypto"
        self.key = self.symbol.replace("/", "")              # positions spell crypto without the slash
        self.position_id = position_id or self.id
        self.base = self.symbol.split("/")[0]
        self.spread_bps, self.volume, self.tape, self.shut_quote = spread_bps, volume, tape, shut_quote
        self.has_data = self.crypto or tape is not None      # an OTC share has no IEX data
        self.path = Path(seed, self.symbol, price, day_vol, self.crypto, volume)
        self.salt = _seed(seed, self.symbol, "S")

    def dp(self, price):
        """Decimals of a price: cents; four decimals for a share under 1 USD."""
        return 2 if self.crypto or price >= 1 else 4


# ---------------------------------------------------------------- the price path

_DAY0 = 19723                 # 2024-01-01 in days since 1970: the walk starts there and is flat before it
_HALF = 30000.0


def _seed(*parts):
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def _unit(salt, n):
    """A fixed number in [0, 1) for (salt, n): sizes and ids that must not change between two reads."""
    x = (salt ^ (n * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 33)) * 0xFF51AFD7ED558CCD) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 33)) * 0xC4CEB9FE1A85EC53) & 0xFFFFFFFFFFFFFFFF
    return ((x ^ (x >> 33)) >> 11) / 9007199254740992.0


def _merge(a, b):
    """Two adjacent aggregates [open, high, low, close, volume, trades, price x volume] as one."""
    if a is None:
        return list(b)
    return [a[0], a[1] if a[1] >= b[1] else b[1], a[2] if a[2] <= b[2] else b[2], b[3], a[4] + b[4], a[5] + b[5], a[6] + b[6]]


class Path:
    """One symbol's walk: a day-to-day walk pulled gently back to the start price, and inside each UTC day a
    minute walk pinned to the day's two ends. The price moves in straight lines between knots at second 0 and
    second 30 of every minute, so the extremes of any span are at knots and a bar is exact, not sampled. Time is
    in whole milliseconds; all arithmetic is + - * /, so the same seed gives the same prices everywhere."""

    def __init__(self, seed, symbol, price, day_vol, crypto, volume):
        self.start, self.day_vol, self.crypto, self.volume = float(price), day_vol, crypto, volume
        self._key = (seed, symbol)
        self._daily = random.Random(_seed(seed, symbol, "D"))
        self._opens = [self.start]
        self._days, self._hours = {}, {}

    def _open(self, k):
        if k <= 0:
            return self.start
        opens, r = self._opens, self._daily.random
        while len(opens) <= k:
            p = opens[-1]
            step = (r() + r() + r() - 1.5) * 2.0 * self.day_vol
            opens.append(max(p * (1.0 + step) + 0.02 * (self.start - p), self.start * 0.05))
        return opens[k]

    def _day(self, k):
        day = self._days.get(k)
        if day is None:
            p0, p1 = self._open(k), self._open(k + 1)
            r = random.Random(_seed(*self._key, "I", k)).random
            sd = p0 * self.day_vol / 30.0
            walk, acc = [0.0] * 1441, 0.0
            for i in range(1, 1441):
                acc += (r() + r() - 1.0) * 2.45 * sd
                walk[i] = acc
            pull, floor = (p1 - p0 - acc) / 1440.0, p0 * 0.02
            M = [max(p0 + walk[i] + pull * i, floor) for i in range(1441)]
            M[1440] = p1
            X = [max((M[i] + M[i + 1]) * 0.5 + (r() - 0.5) * 1.2 * sd, floor) for i in range(1440)]
            C = [X[i] + (M[i + 1] - X[i]) * (29999 / _HALF) for i in range(1440)]
            V, N = [0] * 1440, [0] * 1440
            for i in range(1440):
                u = r()
                if self.crypto:
                    if u >= 0.35:                       # a crypto minute often has no trade at all (n 0, v 0 was seen)
                        V[i], N[i] = round(self.volume * u * u, 9), 1 + int(u * 3)
                elif self.volume:
                    V[i] = int(self.volume * (0.25 + 1.5 * u * u))
                    N[i] = 1 + V[i] // 70
            W = [(M[i] + max(M[i], X[i], C[i]) + min(M[i], X[i], C[i]) + C[i]) * 0.25 * V[i] for i in range(1440)]
            if len(self._days) >= 6:
                self._days.pop(next(iter(self._days)))
            day = self._days[k] = (M, X, C, V, N, W)
        return day

    def at(self, ms):
        """The price at a millisecond."""
        minute, r = divmod(ms, 60000)
        k, i = divmod(minute, 1440)
        if k < _DAY0:
            return self.start
        M, X = self._day(k - _DAY0)[:2]
        if r < 30000:
            return M[i] + (X[i] - M[i]) * (r / _HALF)
        return X[i] + (M[i + 1] - X[i]) * ((r - 30000) / _HALF)

    def _slice(self, k, i, j):
        M, X, C, V, N, W = self._day(k)
        return [M[i], max(max(M[i:j]), max(X[i:j]), C[j - 1]), min(min(M[i:j]), min(X[i:j]), C[j - 1]), C[j - 1],
                sum(V[i:j]), sum(N[i:j]), sum(W[i:j])]

    def minutes(self, m0, m1):
        """The aggregate of whole minutes [m0, m1) (minutes since 1970), through an hourly cache."""
        acc, m = None, max(m0, _DAY0 * 1440)
        while m < m1:
            day, i = divmod(m, 1440)
            k = day - _DAY0
            if i % 60 == 0 and m + 60 <= m1:
                hours = self._hours.get(k)
                if hours is None:
                    hours = self._hours[k] = [self._slice(k, h * 60, h * 60 + 60) for h in range(24)]
                h = i // 60
                while h < 24 and m + 60 <= m1:
                    acc = _merge(acc, hours[h])
                    m, h = m + 60, h + 1
            else:
                j = min((i // 60 + 1) * 60, i + m1 - m)
                acc = _merge(acc, self._slice(k, i, j))
                m += j - i
        return acc

    def volume_at(self, minute):
        day, i = divmod(minute, 1440)
        if day < _DAY0:
            return 0, 0
        d = self._day(day - _DAY0)
        return d[3][i], d[4][i]


# ---------------------------------------------------------------- bars: timeframes and their boundaries

_TIMEFRAME = re.compile(r"(\d+)(Min|T|Hour|H|Day|D|Week|W|Month|M)")
_UNITS = {"Min": "min", "T": "min", "Hour": "hour", "H": "hour", "Day": "day", "D": "day", "Week": "week", "W": "week", "Month": "month", "M": "month"}


def parse_timeframe(text):
    """"5Min" -> ("min", 5); None when it is not one of the documented timeframes."""
    m = _TIMEFRAME.fullmatch(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), _UNITS[m.group(2)]
    ok = {"min": 1 <= n <= 59, "hour": 1 <= n <= 23, "day": n == 1, "week": n == 1, "month": n in (1, 2, 3, 4, 6, 12)}[unit]
    return (unit, n) if ok else None


def _day_start(d, crypto):
    """Where a day bar begins: New York midnight for shares, 00:00 UTC for crypto (both seen)."""
    return (_cal.timegm((d.year, d.month, d.day, 0, 0, 0)) if crypto else ny_epoch(d)) * 1000


def _local_date(ms, crypto):
    s = ms // 1000
    return date(*time.gmtime(s)[:3]) if crypto else ny_date(s)


def bar_floor(tf, ms, crypto):
    """The start of the bar that contains a millisecond. Minute and hour bars count from 00:00 UTC (seen: 7Min at 15:03)."""
    unit, n = tf
    if unit in ("min", "hour"):
        step = n * (60000 if unit == "min" else 3600000)
        day = ms - ms % 86400000
        return day + (ms - day) // step * step
    d = _local_date(ms, crypto)
    if unit == "week":
        d -= timedelta(d.weekday())
    elif unit == "month":
        d = date(d.year, (d.month - 1) // n * n + 1, 1)
    return _day_start(d, crypto)


def bar_next(tf, ms, crypto):
    """The start of the bar after the one that starts at ms."""
    unit, n = tf
    if unit in ("min", "hour"):
        return min(ms + n * (60000 if unit == "min" else 3600000), ms - ms % 86400000 + 86400000)
    d = _local_date(ms, crypto)
    if unit == "day":
        d += timedelta(1)
    elif unit == "week":
        d += timedelta(7)
    else:
        months = d.year * 12 + d.month - 1 + n
        d = date(months // 12, months % 12 + 1, 1)
    return _day_start(d, crypto)


# ---------------------------------------------------------------- the market: clock, session, prices

class Market:
    """The server's clock, whether the share market is open, and every symbol's price at any millisecond."""

    def __init__(self, seed=7, state="open"):
        self.lock = threading.RLock()
        self.seed, self.state0 = seed, state
        self.instruments = [Instrument(*row, seed) for row in _UNIVERSE]
        self.lookup = {}
        for inst in self.instruments:
            for name in (inst.symbol, inst.key, inst.id, inst.position_id):
                self.lookup[name] = inst
        self.reset()

    def reset(self):
        with self.lock:
            self.offset_ns, self._last_stamp = 0, 0
            self.state, self.shut_at_ms = self.state0, None
            self.segments, self.spreads = {}, {}

    # -- clock
    def now_ns(self):
        return time.time_ns() + self.offset_ns

    def now_ms(self):
        return self.now_ns() // 1000000

    def stamp(self):
        """Now in nanoseconds, never the same twice (order of events is the order of stamps)."""
        with self.lock:
            n = max(self.now_ns(), self._last_stamp + 1)
            self._last_stamp = n
            return n

    def is_open(self, ns=None):
        if self.state != "auto":
            return self.state == "open"
        return cal_is_open((self.now_ns() if ns is None else ns) // NS)

    def set_state(self, state):
        if state not in ("open", "shut", "auto"):
            raise ValueError("state must be open, shut or auto")
        with self.lock:
            if state == "shut" and self.is_open():
                self.shut_at_ms = self.now_ms()
            self.state = state

    def clock(self):
        ns = self.now_ns()
        s, is_open = ns // NS, self.is_open(ns)
        if is_open:                       # forced open outside the calendar's hours: the coming session's close stands in
            close = cal_next(s, 1)
            opens = cal_next(close, 0)
        else:
            opens = cal_next(s, 0)
            close = cal_next(opens, 1)
        return {"is_open": is_open, "next_close": ny_stamp(close * NS), "next_open": ny_stamp(opens * NS), "timestamp": ny_stamp(ns)}

    def find(self, name):
        return self.lookup.get(name) or self.lookup.get(str(name).upper())

    # -- prices
    def mid(self, inst, ms):
        """The price at a millisecond: the seeded walk, or what a control put over it from some moment on."""
        segs = self.segments.get(inst.symbol)
        if segs:
            i = bisect.bisect_right(segs[0], ms) - 1
            if i >= 0:
                kind, t0, p0, x, _ = segs[1][i]
                if kind == "hold":
                    v = p0
                elif kind == "trend":
                    v = p0 * (1.0 + x * (ms - t0) / 1000.0)
                else:
                    v = p0 * inst.path.at(ms) / x
                return v if v > 0.0001 else 0.0001
        return inst.path.at(ms)

    def last(self, inst, ms):
        v = self.mid(inst, ms)
        return round(v, inst.dp(v))

    def quote(self, inst, ms):
        """(bid, ask) at a millisecond, on the symbol's tick."""
        mid = self.mid(inst, ms)
        dp = inst.dp(mid)
        tick = 10.0 ** -dp
        spread = self.spreads.get(inst.symbol)
        half = spread / 2.0 if spread is not None else max(tick, mid * inst.spread_bps / 20000.0)
        bid = max(round(mid - half, dp), tick)
        ask = round(mid + half, dp)
        if ask <= bid:
            ask = round(bid + tick, dp)
        return bid, ask

    def control_price(self, symbol, price=None, jump_pct=None, trend_pct_per_s=None, seconds=None, walk=None, spread=None):
        """Put a price over the walk from now on: hold it, jump it, trend it for some seconds, or let it walk again."""
        with self.lock:
            inst = self.find(symbol)
            if inst is None:
                raise ValueError("unknown symbol %r" % (symbol,))
            now = self.now_ms()
            starts, segs = self.segments.setdefault(inst.symbol, ([], []))
            cur = self.mid(inst, now)
            i = bisect.bisect_right(starts, now) - 1
            mode = "walk" if i < 0 else segs[i][4] if segs[i][0] == "trend" else segs[i][0]
            del starts[i + 1:], segs[i + 1:]
            if walk is not None:
                mode = "walk" if walk else "hold"
            elif price is not None:
                mode = "hold"
            if price is not None:
                cur = float(price)
            if jump_pct is not None:
                cur *= 1.0 + float(jump_pct) / 100.0
            if not cur > 0:
                raise ValueError("price must be > 0")

            def add(kind, t0, p0, x=None, after=None):
                starts.append(t0)
                segs.append((kind, t0, p0, x, after))
            if trend_pct_per_s is not None:
                span = int(float(seconds or 0) * 1000)
                if span <= 0:
                    raise ValueError("a trend needs seconds > 0")
                rate = float(trend_pct_per_s) / 100.0
                add("trend", now, cur, rate, mode)
                now, cur = now + span, max(cur * (1.0 + rate * span / 1000.0), 0.0001)
            if mode == "walk":
                add("walk", now, cur, inst.path.at(now))
            else:
                add("hold", now, cur)
            if spread is not None:
                if not float(spread) > 0:
                    raise ValueError("spread must be > 0")
                self.spreads[inst.symbol] = float(spread)
            return {"symbol": inst.symbol, "price": self.mid(inst, self.now_ms()), "mode": mode}

    # -- bars
    def _minute(self, inst, a, e, bounds):
        """The aggregate of [a, e) inside one minute, read at the knots (and either side of a control's moment)."""
        knots = {a, e - 1}
        half = a - a % 60000 + 30000
        if a < half < e:
            knots.add(half)
        for s in bounds:
            if a < s < e:
                knots.update((s - 1, s))
        vals = [self.mid(inst, k) for k in sorted(knots)]
        v, n = inst.path.volume_at(a // 60000)
        if e - a < 60000:
            part = (e - a) / 60000.0
            v, n = (round(v * part, 9) if inst.crypto else int(v * part)), int(n * part)
        hi, lo = max(vals), min(vals)
        return [vals[0], hi, lo, vals[-1], v, n, (vals[0] + hi + lo + vals[-1]) * 0.25 * v]

    def aggregate(self, inst, a, b):
        """[open, high, low, close, volume, trades, price x volume] of [a, b) in milliseconds (a on a minute); None when empty."""
        with self.lock:
            a = max(a, _DAY0 * 86400000)
            if b <= a:
                return None
            bounds = self.segments.get(inst.symbol, ((), ()))[0]
            plain_end = b - b % 60000
            if bounds:
                plain_end = min(plain_end, bounds[0] - bounds[0] % 60000)
            acc = None
            if plain_end > a:
                acc = inst.path.minutes(a // 60000, plain_end // 60000)
                a = plain_end
            while a < b:
                e = min(a - a % 60000 + 60000, b)
                acc = _merge(acc, self._minute(inst, a, e, bounds))
                a = e
            return acc

    def bar(self, inst, a, b, horizon):
        agg = self.aggregate(inst, a, min(b, horizon))
        if agg is None:
            return None
        o, h, l, c, v, n, w = agg
        vw = w / v if v else (o + h + l + c) / 4.0
        return {"c": round(c, inst.dp(c)), "h": round(h, inst.dp(h)), "l": round(l, inst.dp(l)), "n": n, "o": round(o, inst.dp(o)),
                "t": ts(a * 1000000), "v": round(v, 9) if inst.crypto else v, "vw": round(vw, 6)}

    def bars(self, insts, tf, start, end, limit, desc, resume, horizon):
        """Bars with start <= t <= end for several symbols, symbol by symbol, `limit` in all. Returns ({symbol: [bars]},
        (symbol, t) of the last bar when more remain, else None)."""
        out, count, last = {}, 0, None
        lo, hi = max(start, _DAY0 * 86400000), min(end, horizon - 1)
        for inst in sorted(insts, key=lambda i: i.symbol):
            if resume and inst.symbol < resume[0]:
                continue
            c = inst.crypto
            if desc:
                t = bar_floor(tf, hi, c)
                if resume and inst.symbol == resume[0] and resume[1] <= t:
                    t = bar_floor(tf, resume[1] - 1, c)
            else:
                t = bar_floor(tf, lo, c)
                if t < lo:
                    t = bar_next(tf, t, c)
                if resume and inst.symbol == resume[0] and resume[1] >= t:
                    t = bar_next(tf, resume[1], c)
            while lo <= t <= hi:
                if count == limit:
                    return out, last
                b = self.bar(inst, t, bar_next(tf, t, c), horizon)
                if b is not None:
                    out.setdefault(inst.symbol, []).append(b)
                    count, last = count + 1, (inst.symbol, t)
                t = bar_floor(tf, t - 1, c) if desc else bar_next(tf, t, c)
        return out, None

    # -- latest quote and trade, as the data API prints them
    def _stale_ms(self, ms):
        """The moment share quotes stopped: when the market was shut by a control, else just after the calendar's last close."""
        if self.state == "shut" and self.shut_at_ms is not None and self.shut_at_ms <= ms:
            return self.shut_at_ms
        return min(cal_prev_close(ms // 1000) * 1000 + 4695, ms)

    def stock_quote(self, inst, ms, venue):
        tape = inst.tape
        if self.is_open(ms * 1000000):
            bid, ask = self.quote(inst, ms)
            n = ms // 1000
            return {"ap": ask, "as": 100 * (1 + int(_unit(inst.salt, n) * 4)), "ax": venue, "bp": bid,
                    "bs": 100 * (1 + int(_unit(inst.salt ^ 1, n) * 4)), "bx": venue, "c": ["R"],
                    "t": ts((ms - 1) * 1000000 + int(_unit(inst.salt ^ 2, ms) * 1000000)), "z": tape}
        # shut: the quote is the stale one left at the close; some names have no ask at all (ask 0), others are far apart
        t = self._stale_ms(ms)
        mid = self.mid(inst, t)
        dp = inst.dp(mid)
        stamp = ts((t - 1) * 1000000 + int(_unit(inst.salt ^ 2, t) * 1000000))
        if inst.shut_quote == "no_ask":
            return {"ap": 0, "as": 0, "ax": " ", "bp": round(mid * 0.95, dp), "bs": 40, "bx": venue, "c": ["R"], "t": stamp, "z": tape}
        return {"ap": round(mid * 1.03, dp), "as": 100, "ax": venue, "bp": round(mid * 0.97, dp), "bs": 100, "bx": venue, "c": ["R"], "t": stamp, "z": tape}

    def _trade_ms(self, inst, ms):
        """When the latest trade printed: a fixed instant inside the previous whole second."""
        sec = ms // 1000 - 1
        return sec * 1000 + int(_unit(inst.salt ^ 3, sec) * 1000)

    def stock_trade(self, inst, ms, venue):
        t = self._trade_ms(inst, ms)
        return {"c": ["@"], "i": 1 + t // 1000 % 86400, "p": self.last(inst, t), "s": 1 + int(_unit(inst.salt ^ 4, t) * 200),
                "t": ts(t * 1000000 + int(_unit(inst.salt ^ 5, t) * 1000000)), "x": venue, "z": inst.tape}

    def crypto_quote(self, inst, ms):
        bid, ask = self.quote(inst, ms)
        size = 74.0 / ask
        return {"ap": ask, "as": round(size * (0.97 + 0.06 * _unit(inst.salt, ms)), 8), "bp": bid,
                "bs": round(size * (0.97 + 0.06 * _unit(inst.salt ^ 1, ms)), 8),
                "t": ts((ms - 1) * 1000000 + int(_unit(inst.salt ^ 2, ms) * 1000000))}

    def crypto_trade(self, inst, ms):
        t = self._trade_ms(inst, ms)
        u = _unit(inst.salt ^ 4, t)
        return {"i": 6695000000000000000 + t * 1000 + int(u * 1000), "p": self.last(inst, t), "s": round(20.0 / self.mid(inst, t) * (0.2 + u), 9),
                "t": ts(t * 1000000 + int(_unit(inst.salt ^ 5, t) * 1000000)), "tks": "B" if u < 0.5 else "S"}

    def snapshot(self, inst, ms, venue=None):
        """dailyBar, latestQuote, latestTrade, minuteBar (the minute of the latest trade), prevDailyBar."""
        c = inst.crypto
        day, minute = bar_floor(("day", 1), ms, c), self._trade_ms(inst, ms) // 60000 * 60000
        prev = bar_floor(("day", 1), day - 1, c)
        return {"dailyBar": self.bar(inst, day, bar_next(("day", 1), day, c), ms),
                "latestQuote": self.crypto_quote(inst, ms) if c else self.stock_quote(inst, ms, venue),
                "latestTrade": self.crypto_trade(inst, ms) if c else self.stock_trade(inst, ms, venue),
                "minuteBar": self.bar(inst, minute, minute + 60000, ms), "prevDailyBar": self.bar(inst, prev, day, ms)}

    def lastday(self, inst, ms):
        """The mark of the day before: the price at 16:00 New York time yesterday."""
        return self.mid(inst, ny_epoch(ny_date(ms // 1000) - timedelta(1), 16, 0) * 1000)


# ---------------------------------------------------------------- orders

OPEN_STATUSES = frozenset(("new", "partially_filled", "accepted", "pending_new", "pending_cancel", "pending_replace", "held"))
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ULID = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ACTIVITY_TYPES = frozenset("FILL TRANS MISC ACATC ACATS CFEE CSD CSW CSR DIV DIVCGL DIVCGS DIVFEE DIVFT DIVNRA DIVROC DIVTW DIVTXEX FEE INT "
                            "INTNRA INTTW JNL JNLC JNLS MA NC OPASN OPCA OPCSH OPEXC OPEXP OPXRC PTC PTR REORG SPIN SPLIT OCT FOPT SC SSO SSP".split())


class Refuse(Exception):
    """An HTTP refusal; the body's keys come out in alphabetical order, as Alpaca's did."""

    def __init__(self, status, code, message, **extra):
        body = dict(extra, code=code, message=message)
        self.status, self.body = status, {k: body[k] for k in sorted(body)}


class Order:
    filled_ns = expired_ns = cancel_requested_ns = canceled_ns = replaced_ns = None
    replaced_by = replaces = None
    notional = qty = limit_price = stop_price = trail_percent = trail_price = hwm = None
    order_class, extended_hours = "", False
    group_id = group_role = parent = peer = None
    armed = triggered = False        # armed: a held stop-loss whose other half is working; triggered: a stop that has been hit
    fill_at, clips, clip_gap = None, 0, 0.0

    def __init__(self, **fields):
        self.legs, self.filled_qty, self.filled_value = [], ZERO, ZERO
        self.reserve = self.bp_value = ZERO      # cash held by a resting crypto buy; buying power held by a share order
        self.__dict__.update(fields)

    def view(self, now_ns, persist_ns, stream=False, legs=False, status=None):
        """The order as REST prints it, or as the stream does (cancel_requested_at added; subtag, source and legs absent).
        REST was seen to print a fresh order with nanosecond stamps and source null, and the same order later with
        microsecond stamps and source "access_key"; here the change comes `persist_ns` after creation."""
        micro = not stream and persist_ns is not None and now_ns - self.created_ns >= persist_ns
        avg = None
        if self.filled_qty:
            avg = num((self.filled_value / self.filled_qty).quantize(NINE))
        d = {"id": self.id, "client_order_id": self.client_order_id, "created_at": ts(self.created_ns, micro), "updated_at": ts(self.updated_ns, micro),
             "submitted_at": ts(self.submitted_ns, micro), "filled_at": ts(self.filled_ns, micro), "expired_at": ts(self.expired_ns, micro)}
        if stream:
            d["cancel_requested_at"] = ts(self.cancel_requested_ns)
        d.update({"canceled_at": ts(self.canceled_ns, micro), "failed_at": None, "replaced_at": ts(self.replaced_ns, micro), "replaced_by": self.replaced_by,
                  "replaces": self.replaces, "asset_id": self.inst.id, "symbol": self.inst.symbol, "asset_class": self.inst.row["class"],
                  "notional": num(self.notional), "qty": num(self.qty), "filled_qty": num(self.filled_qty), "filled_avg_price": avg,
                  "order_class": self.order_class, "order_type": self.type, "type": self.type, "side": self.side, "position_intent": self.position_intent,
                  "time_in_force": self.time_in_force, "limit_price": num(self.limit_price), "stop_price": num(self.stop_price),
                  "status": status or self.status, "extended_hours": self.extended_hours,
                  "legs": [leg.view(now_ns, persist_ns) for leg in self.legs] if legs and self.legs and not stream else None,
                  "trail_percent": num(self.trail_percent), "trail_price": num(self.trail_price), "hwm": num(self.hwm)})
        if self.group_id:
            d["group_id"], d["group_role"] = self.group_id, self.group_role
        if not stream:
            d["subtag"], d["source"] = None, "access_key" if micro else None
        d["expires_at"] = ts(self.expires_ns)
        return d


class Broker:
    """The account: cash, positions, orders, activities and the trade_updates events, with one engine thread that
    acknowledges, triggers, fills, cancels and expires. Every POST, PATCH and DELETE answers with the state at that
    instant (pending_new and so on), as Alpaca does, and the engine moves it on a few milliseconds later."""

    def __init__(self, market, seed=7, cash="100000", ack_delay=0.005, tick=0.02, persist_after=5.0, on_event=None):
        self.market, self.lock, self.seed, self.cash0 = market, market.lock, seed, cents(Decimal(str(cash)))
        self.cv = threading.Condition(self.lock)
        self.ack_delay, self.tick, self.on_event = ack_delay, tick, on_event
        self.persist_ns = None if persist_after is None else int(persist_after * NS)
        self._thread, self._stop = None, False
        self.rng = random.Random(seed)       # every id comes from here: fixed by the seed, and never reused, not even after a reset
        self._ulid_ms, self._ulid_low = 0, 0
        self.account_id, self.account_number = self._uuid(), "PA3" + "".join(self.rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(9))
        self._funding = "20260706000000000::" + self._uuid()
        self.reset()

    def reset(self):
        with self.lock:
            self.cash, self.positions = self.cash0, {}
            self.orders, self.by_client, self.live = {}, {}, {}
            self.events, self.tasks, self._seq = [], [], 0
            self.fill_delay, self.next_clips = 0.0, None
            self.intent_kind = False       # see _create: a marked close for more than is free is ACCEPTED (not observed: the worst case)
            self.dirty = True
            self.activities = [(1783296000 * NS, {"id": self._funding, "activity_type": "JNLC", "date": "2026-07-06",
                                                  "created_at": "2026-07-07T16:46:56.915477Z", "net_amount": num(self.cash0), "description": "",
                                                  "status": "executed", "currency": "USD"})]
            self.cv.notify_all()

    def _uuid(self):
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    # -- engine
    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="fake-alpaca-engine", daemon=True)
        self._thread.start()

    def stop(self):
        with self.cv:
            self._stop = True
            self.cv.notify_all()
        if self._thread:
            self._thread.join(5)

    def _later(self, delay, fn, *args):
        self._seq += 1
        heapq.heappush(self.tasks, (time.monotonic() + delay, self._seq, fn, args))
        self.dirty = True
        self.cv.notify_all()

    def poke(self):
        """Something outside the engine changed (a price, the market, the clock): look at the orders now."""
        with self.cv:
            self.dirty = True
            self.cv.notify_all()

    def _run(self):
        with self.cv:
            spins = 0
            while not self._stop:
                ran = changed = False
                try:
                    while self.tasks and self.tasks[0][0] <= time.monotonic():
                        _, _, fn, args = heapq.heappop(self.tasks)
                        fn(*args)
                        ran = True
                    changed = bool(self.live) and self._match()
                except Exception:                                       # never let a bug stop the engine silently
                    import traceback
                    traceback.print_exc()
                self.dirty = ran or changed
                self.cv.notify_all()
                if self.dirty and spins < 8:
                    spins += 1
                    continue
                spins = 0
                wait = self.tick if self.live else 0.5
                if self.tasks:
                    wait = min(wait, max(self.tasks[0][0] - time.monotonic(), 0.0))
                self.cv.wait(max(wait, 0.001) if self.dirty else wait)

    def settle(self, timeout=5.0, horizon=0.25):
        """Wait until the engine has nothing due within `horizon` seconds; False on timeout."""
        end = time.monotonic() + timeout
        with self.cv:
            while True:
                now = time.monotonic()
                due = bool(self.tasks and self.tasks[0][0] <= now + horizon) or any(
                    o.fill_at is not None and o.fill_at <= now + horizon for o in self.live.values())
                if not self.dirty and not due:
                    return True
                if now >= end:
                    return False
                self.cv.wait(0.01)

    def _quote(self, inst, ms):
        bid, ask = self.market.quote(inst, ms)
        return Decimal(str(bid)), Decimal(str(ask)), Decimal(str(self.market.last(inst, ms)))

    def _match(self):
        """One look at every live order against the price now. True when anything changed."""
        market, changed, quotes = self.market, False, {}
        now_ns, mono = market.now_ns(), time.monotonic()
        ms, is_open = now_ns // 1000000, market.is_open(now_ns)
        for o in sorted(self.live.values(), key=lambda o: o.created_ns):
            if o.id not in self.live or o.status in ("pending_new", "pending_replace"):
                continue
            if o.expires_ns is not None and now_ns >= o.expires_ns:
                self._finish(o, "expired")
                changed = True
                continue
            inst = o.inst
            if not inst.crypto and not is_open:       # shares wait for the open; nothing triggers and nothing fills
                o.fill_at = None
                continue
            if inst.symbol not in quotes:
                quotes[inst.symbol] = self._quote(inst, ms)
            bid, ask, last = quotes[inst.symbol]
            if o.status == "accepted":                # a queued fractional or notional order is released at the open
                self._set(o, "new")
                changed = True
            if o.status == "held":
                if not o.armed or not self._hit(o, last):
                    continue
                peer, o.peer = o.peer, None           # the stop-loss fires: its other half is cancelled and it goes to work
                if peer is not None:
                    peer.peer = None
                    self._finish(peer, "canceled", cascade=False)     # not its legs: in an OCO the firing stop IS the limit's leg
                o.triggered = True
                self._set(o, "new")
                changed = True
            if self._marketable(o, bid, ask, last):
                if o.fill_at is None:
                    o.fill_at = mono + self.fill_delay
                    if self.next_clips and not o.clips and o.notional is None and not o.filled_qty:
                        (o.clips, o.clip_gap), self.next_clips = self.next_clips, None
                if mono >= o.fill_at:
                    self._fill_clip(o, ask if o.side == "buy" else bid)
                    changed = True
            else:
                o.fill_at = None
                if o.time_in_force in ("ioc", "fok"):     # documented, not observed: what cannot fill at once is cancelled
                    self._finish(o, "canceled")
                    changed = True
        return changed

    @staticmethod
    def _hit(o, last):
        return last >= o.stop_price if o.side == "buy" else last <= o.stop_price

    def _marketable(self, o, bid, ask, last):
        kind = o.type
        if kind == "trailing_stop":                   # documented, not observed: the stop follows the best price since entry
            if not o.triggered:
                if (last > o.hwm) if o.side == "sell" else (last < o.hwm):
                    o.hwm, o.stop_price = last, self._trail_stop(o, last)
                o.triggered = self._hit(o, last)
            return o.triggered
        if kind in ("stop", "stop_limit") and not o.triggered:
            if not self._hit(o, last):
                return False
            o.triggered = True
        if kind in ("market", "stop"):
            return True
        return ask <= o.limit_price if o.side == "buy" else bid >= o.limit_price

    @staticmethod
    def _trail_stop(o, mark):
        sign = 1 if o.side == "buy" else -1
        if o.trail_percent is not None:
            return mark * (1 + sign * o.trail_percent / 100)
        return mark + sign * o.trail_price

    def _fill_clip(self, o, price):
        if o.notional is not None:
            self._execute(o, trunc9(o.notional / price), price)
            return
        left = o.qty - o.filled_qty
        q = left
        if o.clips > 1:
            q = left / o.clips
            q = q.to_integral_value(rounding=ROUND_DOWN) if left == left.to_integral_value() and q >= 1 else trunc9(q)
            if q <= 0:
                q = left
            o.clips -= 1
            o.fill_at = time.monotonic() + o.clip_gap
        self._execute(o, q, price)

    def _execute(self, o, q, price):
        inst, t = o.inst, self.market.stamp()
        o.filled_qty += q
        o.filled_value += q * price
        done = o.notional is not None or o.filled_qty >= o.qty
        pos = self.positions.get(inst.key) or [inst, ZERO, ZERO]
        if o.side == "buy":       # a crypto buy pays its fee in the coin: less arrives than was filled
            self.cash -= cents(q * price)
            self._move(pos, trunc9(q * (1 - CRYPTO_FEE)) if inst.crypto else q, price)
        else:                     # a crypto sell pays its fee from the cash received
            self.cash += cents(q * price * (1 - CRYPTO_FEE)) if inst.crypto else cents(q * price)
            self._move(pos, -q, price)
        if pos[1]:
            self.positions[inst.key] = pos
        else:
            self.positions.pop(inst.key, None)
        left = ZERO if done else o.qty - o.filled_qty
        if done:
            o.filled_ns, o.reserve, o.bp_value = t, ZERO, ZERO
            self.live.pop(o.id, None)
        else:
            share = left / o.qty
            o.reserve, o.bp_value = cents(o.reserve * share), cents(o.bp_value * share)
        o.status, o.updated_ns = "filled" if done else "partially_filled", self.market.stamp()
        s = t // NS
        xid = self._uuid()        # seen: the uuid in a FILL activity's id IS the stream's execution_id of the same fill
        self.activities.append((t, {"id": "%s%03d::%s" % (time.strftime("%Y%m%d%H%M%S", time.gmtime(s + ny_offset(s))), t % NS // 1000000, xid),
                                    "activity_type": "FILL", "transaction_time": ts(t, True), "type": "fill" if done else "partial_fill",
                                    "price": num(price), "qty": num(q), "side": o.side, "symbol": inst.symbol, "leaves_qty": num(left),
                                    "order_id": o.id, "cum_qty": num(o.filled_qty), "order_status": o.status, "swap_rate": "1"}))
        self._emit("fill" if done else "partial_fill", o, when=t, exec_id=xid, price=num(price), qty=num(q), position_qty=num(pos[1]))
        if done:
            peer, o.peer = o.peer, None
            if peer is not None:                      # one of a pair filled: the other is cancelled
                peer.peer = None
                self._finish(peer, "canceled")
            if o.legs and o.order_class in ("bracket", "oto"):
                self._activate(o)

    @staticmethod
    def _move(pos, dq, price):
        qty, cost = pos[1], pos[2]
        if qty == 0 or (qty > 0) == (dq > 0):
            cost += dq * price
        elif abs(dq) < abs(qty):
            cost = cost * (qty + dq) / qty
        else:
            cost = (qty + dq) * price
        pos[1], pos[2] = qty + dq, cost

    def _activate(self, parent):
        """The entry of a bracket or oto filled: the take-profit goes to work; the stop-loss stays held but armed."""
        roles = {leg.group_role: leg for leg in parent.legs if leg.id in self.live}
        tp, sl = roles.get("take_profit"), roles.get("stop_loss")
        if tp is not None:
            self._set(tp, "new")
        if sl is not None:
            if tp is not None:
                sl.armed, sl.peer, tp.peer = True, tp, sl
            else:
                self._set(sl, "new")

    def _set(self, o, status):
        t = self.market.stamp()
        o.status, o.updated_ns = status, t
        self._emit(status, o, when=t)

    def _finish(self, o, status, cascade=True):
        """End a live order as canceled, expired or replaced; its legs and its other half go with it."""
        if o.id not in self.live:
            return
        del self.live[o.id]
        t = self.market.stamp()
        o.status, o.updated_ns, o.reserve, o.bp_value, o.fill_at = status, self.market.stamp(), ZERO, ZERO, None
        setattr(o, status + "_ns", t)
        self._emit(status, o, when=t)
        if cascade and status != "replaced":
            for leg in o.legs:
                self._finish(leg, status)
            peer, o.peer = o.peer, None
            if peer is not None:
                peer.peer = None
                self._finish(peer, "canceled")

    def _emit(self, event, o, when=None, exec_id=True, status=None, **extra):
        """One trade_updates message, keys in the order seen: at, event_id, event, timestamp, order, then the fill's
        price, qty and position_qty, execution_id, swap_rate, settle_date."""
        now = self.market.stamp()
        ms = now // 1000000
        self._ulid_low = self._ulid_low + 1 + self.rng.getrandbits(32) if ms == self._ulid_ms else self.rng.getrandbits(79)
        self._ulid_ms = ms
        ulid = (ms << 80) | self._ulid_low
        data = {"at": ts(now - now % 1000), "event_id": "".join(_ULID[(ulid >> (5 * i)) & 31] for i in range(25, -1, -1)),
                "event": event, "timestamp": ts(when or now), "order": o.view(now, None, stream=True, status=status)}
        data.update(extra)
        if exec_id:
            data["execution_id"] = exec_id if isinstance(exec_id, str) else self._uuid()
        if "price" in extra:
            day = ny_date((when or now) // NS)
            data["swap_rate"], data["settle_date"] = "1", (day if o.inst.crypto else next_trading_day(day)).isoformat()
        message = {"stream": "trade_updates", "data": data}
        self.events.append(message)
        if self.on_event:
            self.on_event(message)

    # -- reading the account
    def _held(self, inst, side):
        """Quantity of a symbol tied up by working orders on one side (held legs do not count)."""
        return sum((o.qty - o.filled_qty for o in self.live.values() if o.inst is inst and o.side == side and o.status != "held" and o.qty is not None), ZERO)

    def _mark(self, inst, ms):
        v = self.market.mid(inst, ms)
        return Decimal(str(round(v, inst.dp(v) + (1 if inst.crypto else 0))))

    def _figures(self):
        """The account's arithmetic. What was seen: cash moves in whole cents; a resting crypto buy lowers `cash` but
        not `equity`; queued share buys give initial_margin = half their value, buying_power = 4 x cash - their value,
        regt = 2 x (equity - initial_margin), non_marginable = equity - initial_margin. With share POSITIONS held the
        same formulas are extended (stub's own: no account with share positions was observed)."""
        ms = self.market.now_ms()
        long_mv = short_mv = crypto_mv = share_mv = maintenance = ZERO
        for inst, qty, _ in self.positions.values():
            mv = qty * self._mark(inst, ms)
            if qty > 0:
                long_mv += mv
            else:
                short_mv += mv
            if inst.crypto:
                crypto_mv += mv
            else:
                share_mv += abs(mv)
                maintenance += abs(mv) * inst.row["maintenance_margin_requirement"] / 100
        long_mv, short_mv = cents(long_mv), cents(short_mv)
        reserved = sum((o.reserve for o in self.live.values()), ZERO)
        queued = sum((o.bp_value for o in self.live.values()), ZERO)
        equity = self.cash + long_mv + short_mv
        free = equity - cents(crypto_mv) - reserved
        initial = cents((share_mv + queued) / 2)
        plain = max(free - initial, ZERO)
        return {"cash": self.cash - reserved, "equity": equity, "long": long_mv, "short": short_mv, "initial": initial,
                "maintenance": cents(maintenance), "non_marginable": plain, "regt": plain * 2,
                "buying_power": max(free * 4 - cents(share_mv) - queued, ZERO)}

    def account(self):
        with self.lock:
            f = self._figures()
            day = ny_date(self.market.now_ns() // NS) - timedelta(1)
            while not trading_day(day):
                day -= timedelta(1)
            start = num(self.cash0)
            return {"id": self.account_id, "admin_configurations": {}, "user_configurations": None, "account_number": self.account_number,
                    "status": "ACTIVE", "crypto_status": "ACTIVE", "options_approved_level": 3, "options_trading_level": 3, "currency": "USD",
                    "buying_power": num(f["buying_power"]), "regt_buying_power": num(f["regt"]), "effective_buying_power": num(f["buying_power"]),
                    "non_marginable_buying_power": num(f["non_marginable"]), "options_buying_power": num(f["non_marginable"]), "cash": num(f["cash"]),
                    "accrued_fees": "0", "portfolio_value": num(f["equity"]), "trading_blocked": False, "transfers_blocked": False,
                    "account_blocked": False, "created_at": "2026-07-05T17:57:24.247894Z", "trade_suspended_by_user": False, "multiplier": "4",
                    "shorting_enabled": True, "equity": num(f["equity"]), "last_equity": start, "long_market_value": num(f["long"]),
                    "short_market_value": num(f["short"]), "position_market_value": num(f["long"] + abs(f["short"])), "initial_margin": num(f["initial"]),
                    "maintenance_margin": num(f["maintenance"]), "last_maintenance_margin": "0", "sma": start, "balance_asof": day.isoformat(),
                    "crypto_tier": 1, "intraday_adjustments": "0", "pending_reg_taf_fees": "0"}

    def _position_view(self, pos, ms):
        inst, qty, cost = pos
        price = self._mark(inst, ms)
        yesterday = Decimal(str(round(self.market.lastday(inst, ms), 2 if inst.crypto else inst.dp(float(price)))))
        mv, six, five = qty * price, Decimal("0.000001"), Decimal("0.00001")
        pl = mv - cost
        plpc = num((pl / abs(cost)).quantize(five)) if cost else "0"
        held = self._held(inst, "sell" if qty > 0 else "buy")
        return {"asset_id": inst.position_id, "symbol": inst.key, "exchange": inst.row["exchange"], "asset_class": inst.row["class"],
                "asset_marginable": inst.row["marginable"], "qty": num(qty), "qty_available": num(qty - held if qty > 0 else qty + held),
                "avg_entry_price": num((cost / qty).quantize(NINE)), "side": "long" if qty > 0 else "short", "market_value": num(mv.quantize(six)),
                "cost_basis": num(cost.quantize(six)), "unrealized_pl": num(pl.quantize(six)), "unrealized_plpc": plpc,
                "unrealized_intraday_pl": num(pl.quantize(six)), "unrealized_intraday_plpc": plpc, "current_price": num(price),
                "lastday_price": num(yesterday), "change_today": num(((price - yesterday) / yesterday).quantize(five))}

    def list_positions(self):
        with self.lock:
            ms = self.market.now_ms()
            return [self._position_view(self.positions[k], ms) for k in sorted(self.positions)]

    def get_position(self, name):
        with self.lock:
            inst = self.market.find(name)
            pos = self.positions.get(inst.key) if inst else None
            if pos is None:
                return 404, {"code": 40410000, "message": "position does not exist"}
            return 200, self._position_view(pos, self.market.now_ms())

    # -- placing an order
    def submit(self, body):
        with self.lock:
            try:
                o = self._create(body)
            except Refuse as r:
                return r.status, r.body
            return 200, o.view(self.market.now_ns(), self.persist_ns, legs=True)

    def _expiry(self, inst, tif, now_ns):
        """expires_at as seen: a `day` order at the session's close; a share `gtc` at 16:00 New York 90 days on; a
        crypto `gtc` at the last 16:00 New York inside 90 days."""
        s = now_ns // NS
        if tif != "gtc":
            return cal_next(s, 1) * NS
        if not inst.crypto:
            return ny_epoch(ny_date(s) + timedelta(90), 16) * NS
        end = ny_epoch(ny_date(s + 90 * 86400), 16)
        return (end if end <= s + 90 * 86400 else ny_epoch(ny_date(s + 90 * 86400) - timedelta(1), 16)) * NS

    @staticmethod
    def _tick_rule(name, price):
        """Share prices: two decimals at 1 USD and above, four below."""
        if price is None:
            return
        if price <= 0:
            raise Refuse(422, 40010001, "%s must be > 0" % name)          # stub's own
        if -price.normalize().as_tuple().exponent > (2 if price >= 1 else 4):
            raise Refuse(422, 42210000, "invalid %s %s. sub-penny increment does not fulfill minimum pricing criteria" % (name, num(price)))

    def _create(self, body):
        """Validate a POST /v2/orders body in a fixed order and register the order. Every message that was seen is
        printed exactly; the ORDER of the checks when two faults meet was not observable and is the stub's own."""
        if not isinstance(body, dict):
            raise Refuse(400, 40010000, "request body format is invalid")       # stub's own
        side, kind, tif, symbol = body.get("side"), body.get("type"), body.get("time_in_force"), body.get("symbol")
        klass = body.get("order_class") or ""
        klass = "" if klass == "simple" else klass
        cid = body.get("client_order_id")
        if side not in ("buy", "sell"):
            raise Refuse(422, 40010001, "invalid side")
        if kind not in ("market", "limit", "stop", "stop_limit", "trailing_stop"):
            raise Refuse(422, 40010001, "invalid order type")                   # documented, not observed
        if tif not in ("day", "gtc", "opg", "cls", "ioc", "fok"):
            raise Refuse(422, 40010001, "invalid time_in_force")                # documented, not observed
        qty, notional = body.get("qty"), body.get("notional")
        if qty is not None and notional is not None:
            raise Refuse(400, 40010001, "only one of qty or notional is accepted")   # documented: 400; the text is the stub's own
        if qty is None and notional is None:
            raise Refuse(422, 40010001, "qty or notional is required")          # documented, not observed
        if qty is not None:
            qty = dec(qty)
            if qty is None or qty <= 0:
                raise Refuse(422, 40010001, "qty must be > 0")
            qty = trunc9(qty)       # seen: 0.0002000001 BTC accepted as 0.0002
            if qty <= 0:
                raise Refuse(422, 40010001, "qty must be > 0")
        else:
            notional = dec(notional)
            if notional is None or notional < 1:
                raise Refuse(422, 40010001, "notional must be >= 1.00")         # documented, not observed
        limit, stop = dec(body.get("limit_price")), dec(body.get("stop_price"))
        trail_price, trail_percent = dec(body.get("trail_price")), dec(body.get("trail_percent"))
        if kind == "market" and (limit is not None or stop is not None):
            raise Refuse(422, 40010001, "market orders require no stop or limit price")     # documented, not observed
        if kind == "limit" and limit is None:
            raise Refuse(422, 40010001, "limit orders require a limit price")
        if kind == "stop" and stop is None:
            raise Refuse(422, 40010001, "stop orders require a stop price")                 # documented, not observed
        if kind == "stop_limit" and (limit is None or stop is None):
            raise Refuse(422, 40010001, "stop limit orders require both stop and limit price")   # documented, not observed
        if kind == "trailing_stop" and (trail_price is None) == (trail_percent is None):
            raise Refuse(422, 40010001, "trailing stop orders must specify one of trail_price or trail_percent")   # documented, not observed
        if cid is not None and len(str(cid)) > 128:
            raise Refuse(422, 40010001, "client_order_id must be no more than 128 characters")
        if not symbol or not isinstance(symbol, str):
            raise Refuse(422, 40010001, "symbol is required")                   # stub's own
        inst = self.market.find(symbol)
        if inst is None:
            raise Refuse(422, 42210000, 'asset "%s" not found' % symbol)
        if not inst.row["tradable"]:
            raise Refuse(422, 42210000, 'asset "%s" is not tradable' % inst.symbol)
        extended = body.get("extended_hours") is True
        fractional = notional is not None or qty != qty.to_integral_value()
        if inst.crypto:
            if kind not in ("market", "limit", "stop_limit"):
                raise Refuse(422, 40010001, "invalid order type for crypto order")
            if tif not in ("gtc", "ioc"):
                raise Refuse(422, 42210000, "invalid crypto time_in_force")
            if klass:
                raise Refuse(422, 42210000, "crypto orders not allowed for advanced order_class: " + str(klass))   # documented rule; the text is the stub's own
        else:
            if extended and not (kind == "limit" and tif in ("day", "gtc")):
                raise Refuse(422, 42210000, "extended hours order must be DAY or GTC limit orders")
            self._tick_rule("limit_price", limit)
            self._tick_rule("stop_price", stop)
            if fractional:
                if not inst.row["fractionable"]:
                    raise Refuse(403, 40310000, 'asset "%s" is not fractionable' % inst.symbol)
                if tif != "day":
                    raise Refuse(422, 42210000, "fractional orders must be DAY orders")
        plan = self._legs_plan(inst, side, kind, tif, klass, body, limit, extended)
        pos = self.positions.get(inst.key)
        have = pos[1] if pos else ZERO
        # position_intent. Seen 2026-11-11 04:54 UTC: an order marked as a CLOSE with nothing on that side to close is refused
        # 422 "position intent mismatch, inferred: sell_to_open, specified: sell_to_close" (SELL 1 SPY, nothing held, the
        # exchange shut; BUY buy_to_close likewise; a BUY buy_to_close while LONG, crypto). `inferred` is what the order
        # would do to the position as it stands. A mark that says OPEN was only seen where it was true (taken).
        intent = body.get("position_intent")
        inferred = ("buy_to_close" if have < 0 else "buy_to_open") if side == "buy" else ("sell_to_close" if have > 0 else "sell_to_open")
        mismatch = intent in ("buy_to_close", "sell_to_close") and intent != inferred
        refusal = Refuse(422, 42210000, "position intent mismatch, inferred: %s, specified: %s" % (inferred, intent))
        if mismatch and not inst.crypto:      # (stub's own: before the client_order_id is looked at; see the header)
            raise refusal
        # NOT observed: a marked close for more than is free to close. The worst case unless told otherwise: it is not held
        # to what is free (the two "insufficient qty available" checks below), it is accepted and it trades.
        loose = intent in ("buy_to_close", "sell_to_close") and not mismatch and not inst.crypto and not self.intent_kind
        if cid is not None and str(cid) in self.by_client:
            raise Refuse(422, 40010001, "client_order_id must be unique")
        bid, ask, last = self._quote(inst, self.market.now_ms())
        hwm = None
        if kind == "trailing_stop":     # seen: a 5% buy at 352.55 answered hwm 352.55, stop_price 370.1775, status new
            hwm = last
            stop = (last * (1 + (1 if side == "buy" else -1) * trail_percent / 100)) if trail_percent is not None else last + (trail_price if side == "buy" else -trail_price)
        ref = limit if limit is not None else stop if stop is not None else ask if side == "buy" else bid
        value = notional if notional is not None else qty * ref
        if inst.crypto:
            if value < CRYPTO_MIN_COST:
                raise Refuse(403, 40310000, "cost basis must be >= minimal amount of order 10")
            sized = value if notional is not None or limit is not None else qty * ask * MARKET_COLLAR
            if sized > CRYPTO_MAX_NOTIONAL:
                raise Refuse(403, 40310000, "order notional %s exceeds max notional per order 200000" % num(sized))
        if kind != "trailing_stop" and not klass:      # wash trade: bracket, oco and trailing-stop orders are exempt (documented)
            for e in sorted(self.live.values(), key=lambda e: e.id):
                if e.inst is not inst or e.side == side or e.type == "trailing_stop" or e.order_class or e.status == "held":
                    continue
                hard = kind in ("market", "stop") or e.type in ("market", "stop")
                if not hard:        # two limit prices: refused only when the buy is at or above the sell (documented table)
                    buy, sell = (limit, e.limit_price) if side == "buy" else (e.limit_price, limit)
                    if buy < sell:
                        continue
                raise Refuse(403, 40310000, "potential wash trade detected. use complex orders", existing_order_id=e.id,
                             reject_reason="opposite side market/stop order exists" if hard else "opposite side limit order exists")   # the second reason's text is the stub's own
        opening = have >= 0 if side == "buy" else have <= 0
        if klass == "oco" and opening:       # documented: oco is an exit on an existing position; the refusal is the documented "insufficient qty"
            raise Refuse(403, 40310000, "insufficient qty available for order (requested: %s, available: 0)" % num(qty), available="0",
                         existing_qty=num(abs(have)), held_for_orders="0", symbol=inst.symbol)
        if side == "buy" and have < 0 and not inst.crypto and qty is not None and not loose:
            held = self._held(inst, "buy")      # seen 2026-11-10 17:47 UTC, with an OTO stop leg and with a plain buy stop resting
            if qty > -have - held and held > 0:
                raise Refuse(403, 40310000, "insufficient qty available for order (requested: %s, available: %s)" % (num(qty), num(-have - held)))
        if side == "sell":
            held = self._held(inst, "sell")
            if inst.crypto:
                want = qty if qty is not None else trunc9(notional / bid)
                if want > have - held:
                    raise Refuse(403, 40310000, "insufficient balance for %s (requested: %s, available: %s)" % (inst.base, num(want), num(have - held)),
                                 available=num(have - held), balance=num(have), symbol="USD")
            elif have > 0:
                if qty is not None and qty > have - held and not loose:       # documented, not observed
                    raise Refuse(403, 40310000, "insufficient qty available for order (requested: %s, available: %s)" % (num(qty), num(have - held)),
                                 available=num(have - held), existing_qty=num(have), held_for_orders=num(held), symbol=inst.symbol)
            else:
                if not inst.row["shortable"]:
                    raise Refuse(422, 42210000, 'asset "%s" cannot be sold short' % inst.symbol)
                if fractional:
                    raise Refuse(422, 42210000, "fractional orders cannot be sold short")   # documented, not observed
                value = qty * max(ref, ask * Decimal("1.03"))     # documented: a short is valued at max(limit, 3% over the ask)
        if mismatch:        # crypto: seen AFTER the balance check (a marked SELL of more than is held was "insufficient balance")
            raise refusal
        if opening:
            f = self._figures()
            if inst.crypto:
                if value > f["non_marginable"]:     # stub's own shape, after the BTC one: a crypto buy beyond the cash was not observed
                    raise Refuse(403, 40310000, "insufficient balance for USD (requested: %s, available: %s)" % (num(cents(value)), num(f["non_marginable"])),
                                 available=num(f["non_marginable"]), balance=num(f["cash"]), symbol="USD")
            else:
                cost = value + (qty or ZERO) * CAT_FEE
                if cost > f["buying_power"]:
                    raise Refuse(403, 40310000, "insufficient buying power", buying_power=num(f["buying_power"]), cost_basis=num(cost))
        if intent not in ("buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"):
            intent = inferred
        shut = not inst.crypto and not self.market.is_open()
        if kind == "trailing_stop" or (inst.crypto and kind == "stop_limit"):
            status = "new"            # seen: these two answer `new` at once
        elif shut and fractional:
            status = "accepted"       # seen: a fractional or notional share order sent while shut stays `accepted`
        else:
            status = "pending_new"
        o = self._register(inst, side, kind, tif, status, qty=qty, notional=notional, limit_price=limit, stop_price=stop, trail_percent=trail_percent,
                           trail_price=trail_price, hwm=hwm, order_class=klass, extended_hours=extended, position_intent=intent,
                           client_order_id=None if cid is None else str(cid), value=value if opening else ZERO, plan=plan)
        return o

    def _legs_plan(self, inst, side, kind, tif, klass, body, limit, extended):
        """The exit legs of a bracket, oto or oco order as (role, type, limit, stop). The rules are documented; none
        of the refusals here was observed, so their texts are the stub's own."""
        if not klass:
            return []
        if klass not in ("bracket", "oco", "oto"):
            raise Refuse(422, 40010001, "invalid order_class")
        tp, sl = body.get("take_profit") or {}, body.get("stop_loss") or {}
        tp_limit, sl_stop, sl_limit = dec(tp.get("limit_price")), dec(sl.get("stop_price")), dec(sl.get("limit_price"))
        for name, v in (("take_profit.limit_price", tp_limit), ("stop_loss.stop_price", sl_stop), ("stop_loss.limit_price", sl_limit)):
            self._tick_rule(name, v)
        if tif not in ("day", "gtc") or extended:
            raise Refuse(422, 42210000, "%s orders must be day or gtc and cannot be extended hours" % klass)
        if klass == "oto":
            if (tp_limit is None) == (sl_stop is None):
                raise Refuse(422, 42210000, "oto orders require exactly one of take_profit or stop_loss")
        elif tp_limit is None or sl_stop is None:
            raise Refuse(422, 42210000, "%s orders require take_profit.limit_price and stop_loss.stop_price" % klass)
        if klass == "oco" and kind != "limit":
            raise Refuse(422, 42210000, "oco orders must be limit orders")
        if tp_limit is not None and sl_stop is not None:
            exit_side = side if klass == "oco" else ("sell" if side == "buy" else "buy")
            if (tp_limit <= sl_stop) if exit_side == "sell" else (tp_limit >= sl_stop):
                raise Refuse(422, 42210000, "take_profit.limit_price must be %s stop_loss.stop_price" % (">" if exit_side == "sell" else "<"))
        plan = []
        if tp_limit is not None and klass != "oco":
            plan.append(("take_profit", "limit", tp_limit, None))
        if sl_stop is not None:
            plan.append(("stop_loss", "stop_limit" if sl_limit is not None else "stop", sl_limit, sl_stop))
        return plan

    def _register(self, inst, side, kind, tif, status, value=ZERO, plan=(), client_order_id=None, **fields):
        now = self.market.stamp()
        o = Order(id=self._uuid(), client_order_id=client_order_id or self._uuid(), created_ns=now, updated_ns=self.market.stamp(), submitted_ns=now,
                  inst=inst, side=side, type=kind, time_in_force=tif, status=status, expires_ns=self._expiry(inst, tif, now), **fields)
        if value:
            if inst.crypto:
                if side == "buy":
                    o.reserve = cents(value)
            else:
                o.bp_value = cents(value)
        if plan:
            oco = o.order_class == "oco"
            o.group_id, o.group_role = self._uuid(), "take_profit" if oco else "entry"
            exit_side = side if oco else ("sell" if side == "buy" else "buy")
            for role, leg_kind, leg_limit, leg_stop in plan:
                leg = Order(id=self._uuid(), client_order_id=self._uuid(), created_ns=now, updated_ns=self.market.stamp(), submitted_ns=now, inst=inst,
                            side=exit_side, type=leg_kind, time_in_force=tif, status="held", expires_ns=o.expires_ns, qty=o.qty, limit_price=leg_limit,
                            stop_price=leg_stop, order_class=o.order_class, position_intent=exit_side + "_to_close", parent=o, group_id=o.group_id,
                            group_role=role)
                if oco:
                    leg.armed, leg.peer, o.peer = True, o, leg
                o.legs.append(leg)
                self.orders[leg.id] = self.by_client[leg.client_order_id] = self.live[leg.id] = leg
                self._emit("held", leg, exec_id=False)      # seen 2026-11-10 14:3x UTC: a leg's `held` event comes BEFORE its entry's pending_new
        self.orders[o.id] = self.by_client[o.client_order_id] = self.live[o.id] = o
        if status == "pending_new":
            self._emit("pending_new", o, exec_id=False)
            self._later(self.ack_delay, self._ack, o)
        elif status == "accepted":
            self._emit("accepted", o, exec_id=False)      # the event of a queued fractional order was not observed
        else:
            self._emit("new", o)
        self.dirty = True
        self.cv.notify_all()
        return o

    def _ack(self, o):
        if o.id in self.live and o.status == "pending_new":
            if not o.inst.crypto:
                o.submitted_ns = self.market.stamp()      # seen on share orders: submitted_at moves to the acknowledgement
            self._set(o, "new")

    # -- reading, replacing and cancelling orders
    def _lookup(self, order_id):
        if not _UUID.fullmatch(order_id or ""):
            raise Refuse(422, 40010001, "invalid order_id")
        o = self.orders.get(order_id.lower())
        if o is None:
            raise Refuse(404, 40410000, "order not found for " + order_id)
        return o

    def get_order(self, order_id, nested=False):
        with self.lock:
            try:
                return 200, self._lookup(order_id).view(self.market.now_ns(), self.persist_ns, legs=nested)
            except Refuse as r:
                return r.status, r.body

    def get_by_client_id(self, cid):
        with self.lock:
            if not cid:
                return 422, {"code": 40010001, "message": "client_order_id is missing"}
            o = self.by_client.get(cid)
            if o is None:
                return 404, {"code": 40410000, "message": "order not found for " + cid}
            return 200, o.view(self.market.now_ns(), self.persist_ns)

    def list_orders(self, q):
        """GET /v2/orders. Seen: symbols=BTC/USD matches and BTCUSD does not; an unknown status answers like `all`;
        in a `status=all` list a bracket's legs are rows of their own unless nested.

        `status=open`, seen 2026-11-10 (13:06, 14:38, 14:39 and 17:47 UTC): a `held` leg is NEVER a row. It appears only
        under its own parent, when that parent is itself listed and nested=true (an unfilled bracket's legs under the
        entry; an OCO's stop under its limit). After a bracket's entry has FILLED, the list (nested or not) holds one
        row, the working take-profit leg, with no legs under it: the filled parent is not listed and the held stop is
        in no open list at all. It is found by its id, or under the parent in a `status=closed&nested=true` list. An
        OTO's single stop leg is `new` once the entry has filled, and is a row."""
        with self.lock:
            status, nested = q.get("status", "open"), q.get("nested") == "true"
            try:
                limit = max(1, min(int(q.get("limit", 50)), 500))
                after = (parse_time(q["after"]) + 500) // 1000 if q.get("after") else None     # compared at the microsecond, the
                until = (parse_time(q["until"]) + 500) // 1000 if q.get("until") else None     # precision an older order is printed with
            except ValueError:
                return 422, {"code": 40010001, "message": "invalid query parameter"}        # stub's own
            symbols = set(q["symbols"].split(",")) if q.get("symbols") else None
            rows = []
            for o in self.orders.values():
                is_open = o.status in OPEN_STATUSES
                if (status == "open" and (not is_open or o.status == "held")) or (status == "closed" and is_open):
                    continue
                if nested and o.parent is not None and status != "open":       # rolled up under its parent instead
                    continue
                if (symbols and o.inst.symbol not in symbols) or (q.get("side") and o.side != q["side"]):
                    continue
                if q.get("asset_class") and o.inst.row["class"] != q["asset_class"]:
                    continue
                sent = (o.submitted_ns + 500) // 1000
                if (after is not None and sent <= after) or (until is not None and sent >= until):
                    continue
                rows.append(o)
            rows.sort(key=lambda o: (o.created_ns, o.id), reverse=q.get("direction", "desc") != "asc")
            now = self.market.now_ns()
            return 200, [o.view(now, self.persist_ns, legs=nested) for o in rows[:limit]]

    def replace(self, order_id, body):
        """PATCH: a NEW order (new id, `replaces`), answered as `new`; the old one goes pending_replace, then replaced.
        The stream says pending_replace (old), accepted (new), replaced (old) and no `new` after, as seen."""
        with self.lock:
            try:
                o = self._lookup(order_id)
                if o.id not in self.live:
                    raise Refuse(422, 42210000, "order is not open")
                if o.status in ("accepted", "pending_new", "pending_cancel", "pending_replace"):      # documented rule; the text is the stub's own
                    raise Refuse(422, 42210000, "unable to replace order, order isn't sent to exchange yet")
                if not isinstance(body, dict):
                    raise Refuse(400, 40010000, "request body format is invalid")
                if o.notional is not None:
                    raise Refuse(422, 42210000, "notional orders cannot be replaced")                 # documented rule; the text is the stub's own
                qty, limit, stop = o.qty, o.limit_price, o.stop_price
                if body.get("qty") is not None:
                    qty = dec(body["qty"])
                    if qty is None or qty <= 0 or trunc9(qty) <= o.filled_qty:
                        raise Refuse(422, 40010001, "qty must be > 0")
                    qty = trunc9(qty)
                    if not o.inst.crypto and qty != qty.to_integral_value():
                        raise Refuse(422, 42210000, "qty of fractional orders cannot be changed")     # documented rule; the text is the stub's own
                if body.get("limit_price") is not None and o.type in ("limit", "stop_limit"):
                    limit = dec(body["limit_price"])
                if body.get("stop_price") is not None and o.type in ("stop", "stop_limit"):
                    stop = dec(body["stop_price"])
                if limit is None and o.type in ("limit", "stop_limit") or stop is None and o.type in ("stop", "stop_limit"):
                    raise Refuse(422, 40010001, "invalid price")                                      # stub's own
                if not o.inst.crypto:
                    self._tick_rule("limit_price", limit)
                    self._tick_rule("stop_price", stop)
                elif limit is not None and qty * limit < CRYPTO_MIN_COST:
                    raise Refuse(403, 40310000, "cost basis must be >= minimal amount of order 10")
                tif = body.get("time_in_force") or o.time_in_force
                if tif not in (("gtc", "ioc") if o.inst.crypto else ("day", "gtc", "opg", "cls", "ioc", "fok")):
                    raise Refuse(422, 40010001, "invalid time_in_force")
                cid = body.get("client_order_id")
                if cid is not None and len(str(cid)) > 128:
                    raise Refuse(422, 40010001, "client_order_id must be no more than 128 characters")
                if cid is not None and str(cid) in self.by_client:
                    raise Refuse(422, 40010001, "client_order_id must be unique")
            except Refuse as r:
                return r.status, r.body
            now, asked = self.market.stamp(), o.status
            n = Order(id=self._uuid(), client_order_id=str(cid) if cid is not None else self._uuid(), created_ns=now, updated_ns=self.market.stamp(),
                      submitted_ns=now, inst=o.inst, side=o.side, type=o.type, time_in_force=tif, status="held" if asked == "held" else "new",
                      expires_ns=o.expires_ns if tif == o.time_in_force else self._expiry(o.inst, tif, now), qty=qty, limit_price=limit, stop_price=stop,
                      trail_percent=o.trail_percent, trail_price=o.trail_price, hwm=o.hwm, order_class=o.order_class, extended_hours=o.extended_hours,
                      position_intent=o.position_intent, replaces=o.id, group_id=o.group_id, group_role=o.group_role)
            n.filled_qty, n.filled_value, n.triggered = o.filled_qty, o.filled_value, o.triggered
            ref = limit if limit is not None else stop
            if o.reserve and ref is not None:
                n.reserve = cents((qty - o.filled_qty) * ref)
            elif o.bp_value and ref is not None:
                n.bp_value = cents((qty - o.filled_qty) * ref)
            else:
                n.reserve, n.bp_value = o.reserve, o.bp_value
            self.orders[n.id] = self.by_client[n.client_order_id] = n
            o.replaced_by, o.status, o.updated_ns, o.fill_at = n.id, "pending_replace", self.market.stamp(), None
            self._later(self.ack_delay, self._replaced, o, n, asked, self.market.stamp())
            return 200, n.view(self.market.now_ns(), self.persist_ns)

    def _replaced(self, o, n, asked, asked_ns):
        if o.id not in self.live:
            return
        self._emit("pending_replace", o, when=asked_ns)
        n.submitted_ns = n.updated_ns = self.market.stamp()
        self._emit("accepted", n, exec_id=False, status="accepted")
        n.parent, n.legs, n.peer, n.armed = o.parent, o.legs, o.peer, o.armed     # the new order takes the old one's place in its group
        if o.parent is not None:
            o.parent.legs = [n if leg is o else leg for leg in o.parent.legs]
        for leg in o.legs:
            leg.parent = n
        if o.peer is not None:
            o.peer.peer = n
        o.legs, o.peer, o.parent = [], None, None
        self.live[n.id] = n
        self._finish(o, "replaced")

    def cancel(self, order_id):
        """DELETE one order: 204 says the request was taken; the engine cancels a moment later. Seen: 204 again for
        an order already cancelled."""
        with self.lock:
            try:
                o = self._lookup(order_id)
            except Refuse as r:
                return r.status, r.body
            if o.id in self.live:
                self._ask_cancel(o)
                return 204, None
            if o.status == "canceled":
                return 204, None
            return 422, {"code": 42210000, "message": 'order is already in "%s" state' % o.status}     # documented: 422; the text is the stub's own

    def _ask_cancel(self, o):
        if o.cancel_requested_ns is None:
            o.cancel_requested_ns = self.market.stamp()
            self._later(self.ack_delay, self._finish, o, "canceled")

    def cancel_all(self):
        """DELETE /v2/orders: 207 with {"id", "status": 200} per order, sorted by id, a bracket once (seen). The rows
        are the orders a `status=open` list shows: a held leg is not one of them, and goes with the order it hangs on.
        Seen 2026-11-10 17:47 UTC for a bracket whose entry had filled: one row, the working limit leg, and both legs
        were `canceled` 0.6 s later."""
        with self.lock:
            rows = []
            for o in sorted(self.live.values(), key=lambda o: o.id):
                if o.status != "held":
                    rows.append({"id": o.id, "status": 200})
                    self._ask_cancel(o)
            return 207, rows

    # -- closing positions
    def close_position(self, name, qty=None, percentage=None):
        """DELETE /v2/positions/{symbol}: a market order for the position (or `qty`, or `percentage` of it)."""
        with self.lock:
            inst = self.market.find(name)
            pos = self.positions.get(inst.key) if inst else None
            if pos is None:        # seen 2026-11-11 04:54 UTC (BTCUSD, SPY); it places nothing
                return 404, {"code": 40410000, "message": "position not found: " + (inst.key if inst else str(name))}
            have = abs(pos[1])
            side = "sell" if pos[1] > 0 else "buy"
            if qty is not None and percentage is not None:
                return 422, {"code": 40010001, "message": "only one of qty or percentage is accepted"}      # stub's own
            want = have
            if qty is not None or percentage is not None:
                n = dec(qty if qty is not None else percentage)
                if n is None or n <= 0 or (percentage is not None and n > 100):
                    return 422, {"code": 40010001, "message": "qty must be > 0" if qty is not None else "percentage must be between 0 and 100"}   # stub's own
                want = trunc9(n if qty is not None else have * n / 100)
            held = self._held(inst, side)
            if want > have - held:     # documented, not observed
                r = Refuse(403, 40310000, "insufficient qty available for order (requested: %s, available: %s)" % (num(want), num(have - held)),
                           available=num(have - held), existing_qty=num(have), held_for_orders=num(held), symbol=inst.symbol)
                return r.status, r.body
            o = self._register(inst, side, "market", "gtc" if inst.crypto else "day", "pending_new", qty=want, position_intent=side + "_to_close")
            return 200, o.view(self.market.now_ns(), self.persist_ns)

    def close_all(self, cancel_orders=False):
        """DELETE /v2/positions: 207 with {"symbol", "status", "body"} per position, `symbol` in the position's own
        spelling and `body` the closing order (seen 2026-11-10 18:25 UTC with cancel_orders=true: the resting order
        was cancelled first); [] when flat (seen)."""
        with self.lock:
            if cancel_orders:
                for o in list(self.live.values()):
                    self._finish(o, "canceled")
            rows = []
            for key in sorted(self.positions):
                status, body = self.close_position(key)
                rows.append({"symbol": key, "status": status, "body": body})
            return 207, rows

    def list_activities(self, q, only=None):
        """GET /v2/account/activities[/{type}]: newest first, `page_size` rows, `page_token` = the id of the last row sent."""
        with self.lock:
            try:
                size = int(q.get("page_size", 100))
                after = (parse_time(q["after"]) + 500) // 1000 if q.get("after") else None
                until = (parse_time(q["until"]) + 500) // 1000 if q.get("until") else None
                on = parse_time(q["date"]) // NS // 86400 if q.get("date") else None
            except ValueError:
                return 422, {"code": 40010001, "message": "invalid query parameter"}        # stub's own
            if size > 100:
                return 422, {"code": 40010001, "message": "tried to set the page size to %d, but the maximum is 100" % size}
            types = {only} if only else set(q["activity_types"].split(",")) if q.get("activity_types") else None
            for t in types or ():
                if t not in _ACTIVITY_TYPES:
                    return 422, {"code": 40010001, "message": "invalid activity type: " + t}
            rows = sorted(self.activities, key=lambda r: r[0], reverse=q.get("direction", "desc") != "asc")
            out, started = [], not q.get("page_token")
            for at, row in rows:
                if not started:
                    started = row["id"] == q["page_token"]
                    continue
                trade = row["activity_type"] == "FILL"
                if types and row["activity_type"] not in types or q.get("category") in ("trade_activity", "non_trade_activity") and trade != (q["category"] == "trade_activity"):
                    continue
                if (after is not None and (at + 500) // 1000 <= after) or (until is not None and (at + 500) // 1000 >= until):
                    continue                              # exclusive, at the microsecond transaction_time is printed with
                if on is not None and at // NS // 86400 != on:
                    continue
                if q.get("order_id") and row.get("order_id") != q["order_id"]:
                    continue
                out.append(row)
                if len(out) >= max(size, 1):
                    break
            return 200, out


# ---------------------------------------------------------------- the server

_JSON = "application/json"
_TRADING_JSON = "application/json; charset=UTF-8"
_UNAUTHORIZED = b'{"message": "unauthorized."}\n'       # seen on the trading host for a wrong key pair and for none
_NGINX_401 = (b"<html>\r\n<head><title>401 Authorization Required</title></head>\r\n<body>\r\n<center><h1>401 Authorization Required</h1></center>\r\n"
              b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n")      # seen on the data host: HTML, not JSON
_SIP_REFUSAL = "subscription does not permit querying recent SIP data"
_FEEDS = ("iex", "sip", "delayed_sip", "otc", "boats", "overnight")
_VENUE = {"A": "N", "B": "P", "C": "Q"}


class DataError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class _Plain:
    def __init__(self, text):
        self.body = text.encode()


class _Bucket:
    """The request allowance: `limit` a minute, refilled continuously (seen: Remaining back at 199 a third of a second later)."""

    def __init__(self, limit):
        self.limit, self.tokens, self.at = limit, float(limit), time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(float(self.limit), self.tokens + (now - self.at) * self.limit / 60.0)
        self.at = now
        if self.tokens < 1.0:
            return False, 0, (1.0 - self.tokens) * 60.0 / self.limit
        self.tokens -= 1.0
        return True, int(self.tokens), 0.0


def _frame(opcode, payload):
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else b"\x7e" + struct.pack(">H", n) if n < 65536 else b"\x7f" + struct.pack(">Q", n))
    return head + payload


class _Stream:
    """One websocket connection to /stream. Everything the server sends is a BINARY frame of UTF-8 JSON (seen: 16 of 16)."""

    def __init__(self, handler, fake):
        self.rfile, self.sock, self.fake = handler.rfile, handler.connection, fake
        self.out, self.authorised, self.listening, self.open = queue.Queue(), False, False, True
        self._writer = threading.Thread(target=self._write, name="fake-alpaca-stream", daemon=True)
        self._writer.start()

    def send(self, obj):
        self.out.put(_frame(2, json.dumps(obj, separators=(",", ":")).encode()))

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
            while True:
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
                    self.close(True, payload[:2])
                    return
                if opcode == 9:
                    self.out.put(_frame(10, payload))
                elif opcode in (0, 1, 2):
                    parts.append(payload)
                    if b0 & 0x80:
                        self._message(b"".join(parts))
                        parts = []
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.close()

    def _message(self, data):
        try:
            msg = json.loads(data.decode("utf-8"))
            action = msg.get("action")
        except (ValueError, AttributeError):
            return
        inner = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        if action in ("auth", "authenticate"):      # the first form was seen; the second is what Alpaca's SDKs send (documented)
            key, secret = msg.get("key") or inner.get("key_id"), msg.get("secret") or inner.get("secret_key")
            self.authorised = key == self.fake.key_id and secret == self.fake.secret
            self.send({"stream": "authorization", "data": {"action": "authenticate", "status": "authorized" if self.authorised else "unauthorized"}})
        elif action == "listen":
            if not self.authorised:                 # documented, not observed
                self.send({"stream": "authorization", "data": {"status": "unauthorized", "action": "listen"}})
                return
            streams = [s for s in inner.get("streams") or [] if s == "trade_updates"]
            self.listening = bool(streams)
            self.send({"stream": "listening", "data": {"streams": streams}})

    def close(self, clean=False, code=b"\x03\xe9"):
        if not self.open:
            return
        self.open = self.listening = False
        self.fake._streams.discard(self)
        if clean:       # a close frame (1001 "going away", or the echo of the client's own), then nothing more
            self.out.put(_frame(8, code))
            self.out.put(None)
            self._writer.join(1.0)       # let it reach the wire before the socket can be closed
            return
        self.out.put(None)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "fake-alpaca", ""

    def log_message(self, *args):
        pass

    def _go(self):
        self.server.fake._handle(self)

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _go


class FakeAlpaca:
    """The stand-in. start() serves on 127.0.0.1 (loopback only) at `port` (0 picks a free one); stop() ends it."""

    def __init__(self, key_id="PKFAKEALPACA00000001", secret="fake-alpaca-secret-not-a-real-one", plan="free", seed=7, market="open",
                 cash="100000", port=0, rate_limit=200, ack_delay=0.005, persist_after=5.0):
        if plan not in ("free", "sip"):
            raise ValueError("plan must be free or sip")
        self.key_id, self.secret, self.plan, self.rate_limit = key_id, secret, plan, rate_limit
        self.market = Market(seed, market)
        self.broker = Broker(self.market, seed, cash, ack_delay=ack_delay, persist_after=persist_after, on_event=self._broadcast)
        self._port, self._httpd, self._thread = port, None, None
        self._streams, self._guard = set(), threading.Lock()
        self._log, self._seq, self._faults, self._request_ids = [], 0, [], random.Random(seed + 1)
        self._buckets = {}
        self._refill()

    def _refill(self):
        self._buckets = {"trading": _Bucket(self.rate_limit), "data": _Bucket(self.rate_limit), "clock": _Bucket(self.rate_limit * 5)}

    # -- life
    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.fake = self
        self.broker.start()
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-alpaca-http", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is None:
            return
        self.drop_streams()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(5)
        self.broker.stop()
        self._httpd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def stream_url(self):
        return "ws://127.0.0.1:%d/stream" % self.port

    # -- controls (each is also a POST /_control/...)
    def set_price(self, symbol, price=None, jump_pct=None, trend_pct_per_s=None, seconds=None, walk=None, spread=None):
        out = self.market.control_price(symbol, price, jump_pct, trend_pct_per_s, seconds, walk, spread)
        self.broker.poke()
        return out

    def set_market(self, state):
        self.market.set_state(state)
        self.broker.poke()
        return {"state": self.market.state, "is_open": self.market.is_open()}

    def advance(self, seconds):
        """Move the server's clock forward. Live orders are looked at along the way (every 30 s of the span, at most
        5000 looks), so a limit the path crossed in between fills and a `day` order expires at its close."""
        span = int(float(seconds) * NS)
        if span < 0:
            raise ValueError("the clock only moves forward; reset puts it back")
        with self.broker.cv:
            base = self.market.offset_ns
            looks = min(5000, max(1, span // (30 * NS))) if self.broker.live else 1
            for i in range(1, looks + 1):
                self.market.offset_ns = base + span * i // looks
                for _ in range(8):
                    if not (self.broker.live and self.broker._match()):
                        break
            self.broker.poke()
        return {"now": ts(self.market.now_ns())}

    def set_clock(self, now):
        return self.advance((parse_time(now) - self.market.now_ns()) / NS)

    def next_fill(self, clips=1, interval=0.05):
        """The next order to fill does so in `clips` executions, `interval` seconds apart (partial_fill ... fill)."""
        if int(clips) < 1:
            raise ValueError("clips must be >= 1")
        with self.broker.lock:
            self.broker.next_clips = (int(clips), float(interval)) if int(clips) > 1 else None

    def set_fill_delay(self, seconds):
        """Every fill waits this long after its order becomes marketable (0 = at once)."""
        if float(seconds) < 0:
            raise ValueError("delay must be >= 0")
        with self.broker.lock:
            self.broker.fill_delay = float(seconds)

    def set_intent(self, kind=False):
        """An order marked as a close (`position_intent`) for more than is free to close, which was NOT observed at Alpaca:
        kind=False (the start) accepts and trades it; kind=True holds it to what is free, as an unmarked order."""
        with self.broker.lock:
            self.broker.intent_kind = bool(kind)
        return {"kind": self.broker.intent_kind}

    def drop_streams(self, clean=False):
        """Cut every websocket connection: the socket simply ends (no close frame) unless clean=True."""
        with self._guard:
            streams = list(self._streams)
        for s in streams:
            s.close(clean)
        return len(streams)

    def fail_next(self, count=1, status=429, retry_after=1, path=None):
        """The next `count` requests (to paths starting with `path`, when given) are answered 429 (with Retry-After) or 500."""
        if int(status) not in (429, 500):
            raise ValueError("status must be 429 or 500")
        with self._guard:
            self._faults.append({"count": int(count), "status": int(status), "retry_after": retry_after, "path": path})

    def reset(self):
        """The account, the prices, the market, the clock and every control back as they were at the start. The request
        log is kept (clear it with requests(clear=True)); websocket connections stay up."""
        with self.broker.cv:
            self.market.reset()
            self.broker.reset()
        with self._guard:
            self._faults = []
            self._refill()

    def settle(self, timeout=5.0):
        return self.broker.settle(timeout)

    def requests(self, clear=False):
        with self._guard:
            out = list(self._log)
            if clear:
                self._log = []
            return out

    def state(self):
        with self.broker.lock:
            b = self.broker
            return {"market": self.market.state, "is_open": self.market.is_open(), "now": ts(self.market.now_ns()), "clock_offset_s": self.market.offset_ns / NS,
                    "plan": self.plan, "fill_delay_s": b.fill_delay, "next_clips": b.next_clips, "intent_kind": b.intent_kind,
                    "live_orders": len(b.live), "orders": len(b.orders),
                    "positions": len(b.positions), "streams": len(self._streams), "listening": sum(1 for s in self._streams if s.listening),
                    "faults": [dict(f) for f in self._faults if f["count"] > 0],
                    "prices": {i.symbol: self.market.mid(i, self.market.now_ms()) for i in self.market.instruments}}

    def _control(self, method, name, q, body):
        if name == "price":
            return self.set_price(**{k: body[k] for k in ("symbol", "price", "jump_pct", "trend_pct_per_s", "seconds", "walk", "spread") if k in body})
        if name == "market":
            return self.set_market(body["state"])
        if name == "clock":
            if body.get("reset"):
                with self.broker.cv:
                    self.market.offset_ns, self.market._last_stamp = 0, 0
                self.broker.poke()
                return {"now": ts(self.market.now_ns())}
            return self.set_clock(body["now"]) if "now" in body else self.advance(body["advance_s"])
        if name == "fills":
            if "clips" in body:
                self.next_fill(body["clips"], body.get("interval_s", 0.05))
            if "delay_s" in body:
                self.set_fill_delay(body["delay_s"])
            return {"next_clips": self.broker.next_clips, "fill_delay_s": self.broker.fill_delay}
        if name == "intent":
            return self.set_intent(bool(body.get("kind")))
        if name == "drop_streams":
            return {"dropped": self.drop_streams(bool(body.get("clean")))}
        if name == "fail":
            self.fail_next(body.get("count", 1), body.get("status", 429), body.get("retry_after", 1), body.get("path"))
            return {"faults": [dict(f) for f in self._faults if f["count"] > 0]}
        if name == "reset":
            self.reset()
            return {"reset": True}
        if name == "settle":
            return {"settled": self.settle(float(body.get("timeout_s", 5)))}
        if name == "requests":
            return {"requests": self.requests(clear=method == "DELETE" or q.get("clear") in ("1", "true"))}
        if name == "events":
            with self.broker.lock:
                return {"events": list(self.broker.events)}
        if name == "state":
            return self.state()
        raise KeyError("no such control: " + name)

    # -- one request
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
        if path == "/stream" and (h.headers.get("Upgrade") or "").lower() == "websocket" and h.headers.get("Sec-WebSocket-Key"):
            return self._upgrade(h)
        service = "data" if path.startswith(("/v2/stocks", "/v1beta")) else "clock" if path in ("/v2/clock", "/v2/calendar") else "trading"
        status, headers, out = self._answer(method, path, q, data, h.headers, service)
        self._record(method, path, parts.query, status, h.headers, service)
        self._send(h, status, headers, out)

    def _record(self, method, path, query, status, headers, service):
        with self._guard:
            self._seq += 1
            self._log.append({"seq": self._seq, "at": ts(time.time_ns(), True), "method": method, "path": path, "query": query, "status": status,
                              "user_agent": headers.get("User-Agent"), "service": service})

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

    def _upgrade(self, h):
        accept = base64.b64encode(hashlib.sha1((h.headers["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
        self._record("GET", "/stream", "", 101, h.headers, "stream")
        stream = _Stream(h, self)
        with self._guard:
            self._streams.add(stream)
        try:
            h.send_response(101, "Switching Protocols")
            h.send_header("Upgrade", "websocket")
            h.send_header("Connection", "Upgrade")
            h.send_header("Sec-WebSocket-Accept", accept)
            h.end_headers()
            h.wfile.flush()
            stream.run()
        finally:
            stream.close()
            h.close_connection = True

    def _broadcast(self, message):
        frame = _frame(2, json.dumps(message, separators=(",", ":")).encode())
        for s in list(self._streams):
            if s.listening:
                s.out.put(frame)

    def _authorised(self, headers):
        if headers.get("APCA-API-KEY-ID") == self.key_id and headers.get("APCA-API-SECRET-KEY") == self.secret:
            return True
        basic = headers.get("Authorization") or ""
        if basic[:6].lower() == "basic ":           # documented, not observed: key id as user, secret as password
            try:
                user, _, password = base64.b64decode(basic[6:]).decode().partition(":")
            except ValueError:
                return False
            return user == self.key_id and password == self.secret
        return False

    def _answer(self, method, path, q, data, headers, service):
        """(status, headers, body) for one Alpaca request: the key, then a forced fault, then the route and the allowance."""
        rid = "%032x" % self._request_ids.getrandbits(128)
        data_host = service == "data"
        if not (path.startswith("/v1beta3/crypto/") or self._authorised(headers)):      # crypto market data needs no key (seen)
            if data_host:
                return 401, {"Content-Type": "text/html", "X-Request-ID": rid}, _NGINX_401
            return 401, {"Content-Type": _JSON, "X-Request-ID": rid, "WWW-Authenticate": 'Bearer, Basic realm="alpaca.markets", charset="UTF-8"'}, _UNAUTHORIZED
        ctype = {"Content-Type": _TRADING_JSON, "X-Request-Id": rid} if service == "trading" else {"Content-Type": _JSON, "X-Request-ID": rid}
        bucket = self._buckets[service]
        with self._guard:
            fault = next((f for f in self._faults if f["count"] > 0 and (not f["path"] or path.startswith(f["path"]))), None)
            if fault:
                fault["count"] -= 1
        if fault:
            limits = {"X-Ratelimit-Limit": str(bucket.limit), "X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": str(int(time.time()) + int(float(fault["retry_after"]) + 0.999))}
            if fault["status"] == 429:
                return 429, dict(ctype, **limits, **{"Retry-After": str(fault["retry_after"])}), {"code": 42910000, "message": "rate limit exceeded"}
            return 500, ctype, {"code": 50010000, "message": "internal server error occurred"}     # documented, not observed
        segs = [unquote(s) for s in path.split("/")[1:]]
        try:
            action = self._data_route(method, segs, q) if data_host else self._trading_route(method, segs, q, data)
        except DataError as e:
            action = (lambda e=e: (e.status, e))
        if action is None:      # seen on the trading host: 404 with no rate-limit headers. The data host's wording is the stub's own.
            return 404, ctype, {"message": "endpoint not found."} if data_host else {"code": 40410000, "message": "endpoint not found"}
        with self._guard:
            ok, left, wait = bucket.take()
        limits = {"X-Ratelimit-Limit": str(bucket.limit), "X-Ratelimit-Remaining": str(left), "X-Ratelimit-Reset": str(int(time.time() + wait + (0.999 if wait else 0)))}
        if not ok:              # documented, not observed: the body was captured by a forum user on the trading host
            return 429, dict(ctype, **limits), {"code": 42910000, "message": "rate limit exceeded"}
        try:
            status, out = action()
        except DataError as e:
            status, out = e.status, e
        if isinstance(out, DataError):
            out = json.dumps({"message": out.message}, separators=(",", ":")).encode() + b"\n"       # seen: data errors end with a newline
        elif isinstance(out, _Plain):
            return status, {"Content-Type": "text/plain; charset=utf-8", "X-Request-ID": rid}, out.body
        return status, dict(ctype, **limits), out

    # -- trading routes
    def _trading_route(self, method, segs, q, data):
        """A function that performs the request, or None when no such endpoint exists."""
        b, n = self.broker, len(segs)
        if n < 2 or segs[0] != "v2":
            return None
        head, tail = segs[1], segs[2] if n > 2 else None

        def body():
            try:
                return json.loads(data)
            except ValueError:
                return None
        table = {}
        if n == 2:
            table = {"account": {"GET": lambda: (200, b.account())},
                     "clock": {"GET": lambda: (200, self.market.clock())},
                     "calendar": {"GET": lambda: self._calendar(q)},
                     "assets": {"GET": lambda: self._assets(q)},
                     "orders": {"POST": lambda: b.submit(body()), "GET": lambda: b.list_orders(q), "DELETE": b.cancel_all},
                     "orders:by_client_order_id": {"GET": lambda: b.get_by_client_id(q.get("client_order_id"))},
                     "positions": {"GET": lambda: (200, b.list_positions()), "DELETE": lambda: self._close_all(q)}}.get(head, {})
        elif n == 3 and head == "account" and tail == "activities":
            table = {"GET": lambda: b.list_activities(q)}
        elif n == 3:
            table = {"assets": {"GET": lambda: self._asset(tail)},
                     "orders": {"GET": lambda: b.get_order(tail, q.get("nested") == "true"), "PATCH": lambda: b.replace(tail, body()), "DELETE": lambda: b.cancel(tail)},
                     "positions": {"GET": lambda: self._position(tail, None), "DELETE": lambda: self._position(tail, q)}}.get(head, {})
        elif n == 4 and segs[1:3] == ["account", "activities"]:
            kind = segs[3]
            table = {"GET": lambda: b.list_activities(q, kind) if kind in _ACTIVITY_TYPES else (422, {"code": 40010001, "message": "invalid activity type: " + kind})}
        if not table:
            return None
        return table.get(method) or (lambda: (405, {"code": 40510000, "message": "method not allowed"}))      # stub's own

    def _calendar(self, q):
        try:
            first = date.fromisoformat(q["start"][:10]) if q.get("start") else date(1970, 1, 1)
            last = date.fromisoformat(q["end"][:10]) if q.get("end") else date(2029, 12, 31)
        except ValueError:
            return 422, {"code": 40010001, "message": "invalid date"}       # stub's own
        first, last, rows = max(first, date(1970, 1, 1)), min(last, date(2029, 12, 31)), []
        while first <= last:
            if trading_day(first):
                rows.append(calendar_row(first))
            first += timedelta(1)
        return 200, rows

    def _assets(self, q):
        """GET /v2/assets. Seen: with no asset_class both shares and crypto come back. Filters as documented."""
        rows = [i.row for i in self.market.instruments]
        if q.get("status") not in (None, "", "active"):
            rows = []
        if q.get("asset_class"):
            rows = [r for r in rows if r["class"] == q["asset_class"]]
        if q.get("exchange"):
            wanted = set(q["exchange"].split(","))
            rows = [r for r in rows if r["class"] == "crypto" or r["exchange"] in wanted]
        if q.get("attributes"):
            wanted = set(q["attributes"].split(","))
            rows = [r for r in rows if wanted & set(r["attributes"])]
        return 200, rows

    def _asset(self, name):
        inst = self.market.find(name)
        if inst is None:
            return 404, {"code": 40410000, "message": "asset not found for " + name}
        return 200, inst.row

    def _position(self, name, closing):
        if "/" in name:         # seen: /v2/positions/BTC%2FUSD is a bare 404 "Not Found"; positions are spelled BTCUSD
            return 404, _Plain("Not Found")
        if closing is None:
            return self.broker.get_position(name)
        return self.broker.close_position(name, closing.get("qty"), closing.get("percentage"))

    def _close_all(self, q):
        flag = q.get("cancel_orders", "false")
        if flag not in ("true", "false"):
            return 422, {"code": 40010001, "message": "cancel_orders must be true or false"}     # documented, not observed
        return self.broker.close_all(flag == "true")

    # -- market-data routes
    def _data_route(self, method, segs, q):
        if method != "GET":
            return None
        if segs[:2] == ["v2", "stocks"]:
            rest = segs[2:]
            kinds = {("quotes", "latest"): "quote", ("trades", "latest"): "trade", ("bars", "latest"): "bar", ("snapshots",): "snapshot", ("bars",): "bars"}
            if tuple(rest) in kinds:
                return lambda: self._stocks(kinds[tuple(rest)], None, q)
            single = {("quotes", "latest"): "quote", ("trades", "latest"): "trade", ("bars", "latest"): "bar", ("snapshot",): "snapshot", ("bars",): "bars"}
            if len(rest) >= 2 and tuple(rest[1:]) in single:
                return lambda: self._stocks(single[tuple(rest[1:])], rest[0], q)
        elif segs[:2] == ["v1beta3", "crypto"] and len(segs) >= 4 and segs[2] in ("us", "us-1", "us-2", "eu-1", "bs-1"):
            kinds = {("latest", "quotes"): "quote", ("latest", "trades"): "trade", ("latest", "bars"): "bar", ("snapshots",): "snapshot", ("bars",): "bars"}
            if tuple(segs[3:]) in kinds:
                return lambda: self._crypto(kinds[tuple(segs[3:])], q)
        return None

    def _symbols(self, q, crypto):
        if not q.get("symbols"):
            raise DataError(400, "Invalid format for parameter symbols: query parameter 'symbols' is required")
        found = ((s, self.market.lookup.get(s)) for s in dict.fromkeys(q["symbols"].split(",")))     # a symbol the feed does not know is left out (seen)
        return [i for s, i in found if i is not None and i.symbol == s and i.crypto == crypto and i.has_data]

    def _bars_request(self, q, insts, crypto, horizon, single=None):
        """The bars endpoints: timeframe, start, end, limit, sort, page_token; `limit` counts bars over all symbols."""
        if "timeframe" not in q:
            raise DataError(400, "Invalid format for parameter timeframe: query parameter 'timeframe' is required")
        tf = parse_timeframe(q["timeframe"])
        if tf is None:
            raise DataError(400, "invalid timeframe: " + q["timeframe"])
        try:
            limit = int(q.get("limit", 1000))
        except ValueError:
            raise DataError(400, "Invalid format for parameter limit: error binding string parameter")      # stub's own wording
        if limit <= 0:
            raise DataError(400, "invalid limit: should be positive")
        if limit > 10000:
            raise DataError(400, "invalid limit: larger than the allowed maximum of 10000")
        if q.get("sort", "asc") not in ("asc", "desc"):
            raise DataError(400, "invalid sort")
        span = {}
        for name in ("start", "end"):
            if q.get(name):
                try:
                    span[name] = parse_time(q[name]) // 1000000
                except ValueError:
                    raise DataError(400, "Invalid format for parameter %s: error parsing '%s' as RFC3339 or 2006-01-02 time" % (name, q[name]))
        now = self.market.now_ms()
        start = span.get("start", min(bar_floor(("day", 1), now, crypto), horizon))      # documented default: the start of the current day
        resume = None
        if q.get("page_token"):
            try:
                symbol, _, at = base64.b64decode(q["page_token"]).decode().split("|")
                resume = (symbol, int(at) // 1000000)
            except ValueError:
                raise DataError(400, "invalid page_token")                                 # stub's own
        bars, more = self.market.bars(insts, tf, start, span.get("end", horizon), limit, q.get("sort") == "desc", resume, horizon)
        token = None
        if more:       # seen: base64 of SYMBOL|M|<nanoseconds of the last bar sent> (D for day, week and month bars)
            token = base64.b64encode(("%s|%s|%d" % (more[0], "D" if tf[0] in ("day", "week", "month") else "M", more[1] * 1000000)).encode()).decode()
        if single is not None:
            return 200, {"bars": bars.get(single, []), "next_page_token": token, "symbol": single}
        return 200, {"bars": bars, "next_page_token": token}

    def _stocks(self, kind, single, q):
        """Shares. A free plan gets IEX; feed=sip is refused on the latest endpoints and on bars that reach into the
        last 15 minutes, and bars with no feed named stop 15 minutes ago (all seen). --plan sip serves everything."""
        feed, now, free = q.get("feed"), self.market.now_ms(), self.plan == "free"
        if feed is not None and feed not in _FEEDS:
            raise DataError(400, "invalid feed: " + feed)
        if single is not None:
            inst = self.market.lookup.get(single)
            if inst is None or inst.symbol != single or inst.crypto or not inst.has_data:
                raise DataError(404, "symbol not found: " + single)                        # stub's own
            insts = [inst]
        else:
            insts = self._symbols(q, False)
        if kind == "bars":
            horizon = now
            if free and feed in (None, "sip"):
                horizon = now - 900000
                if q.get("end"):
                    try:
                        recent = parse_time(q["end"]) // 1000000 > horizon
                    except ValueError:
                        recent = False
                    if recent:
                        raise DataError(403, _SIP_REFUSAL)
            return self._bars_request(q, insts, False, horizon, single)
        feed = feed or ("iex" if free else "sip")
        if free and feed == "sip":
            raise DataError(403, _SIP_REFUSAL)
        at = now - 900000 if feed == "delayed_sip" else now                                # documented, not observed
        m = self.market

        def one(inst):
            venue = "V" if feed == "iex" else _VENUE.get(inst.tape, "V")
            if kind == "quote":
                return m.stock_quote(inst, at, venue)
            if kind == "trade":
                return m.stock_trade(inst, at, venue)
            if kind == "bar":
                minute = m._trade_ms(inst, at) // 60000 * 60000
                return m.bar(inst, minute, minute + 60000, at)
            return m.snapshot(inst, at, venue)
        with m.lock:
            if single is not None:
                if kind == "snapshot":
                    return 200, dict(one(insts[0]), symbol=single)
                return 200, dict(sorted({kind: one(insts[0]), "symbol": single}.items()))
            rows = {i.symbol: one(i) for i in insts}
        return 200, rows if kind == "snapshot" else {kind + "s": rows}

    def _crypto(self, kind, q):
        insts, now, m = self._symbols(q, True), self.market.now_ms(), self.market
        if kind == "bars":
            return self._bars_request(q, insts, True, now)
        with m.lock:
            if kind == "quote":
                return 200, {"quotes": {i.symbol: m.crypto_quote(i, now) for i in insts}}
            if kind == "trade":
                return 200, {"trades": {i.symbol: m.crypto_trade(i, now) for i in insts}}
            if kind == "bar":
                return 200, {"bars": {i.symbol: m.bar(i, m._trade_ms(i, now) // 60000 * 60000, m._trade_ms(i, now) // 60000 * 60000 + 60000, now) for i in insts}}
            return 200, {"snapshots": {i.symbol: m.snapshot(i, now) for i in insts}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="A local stand-in for Alpaca's paper trading and market-data APIs (127.0.0.1 only).")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port; the port is printed")
    ap.add_argument("--key-id", default="PKFAKEALPACA00000001")
    ap.add_argument("--secret", default="fake-alpaca-secret-not-a-real-one")
    ap.add_argument("--plan", choices=("free", "sip"), default="free", help="free refuses feed=sip as Alpaca's free plan does")
    ap.add_argument("--market", choices=("open", "shut", "auto"), default="open", help="the share market at the start; auto follows the calendar")
    ap.add_argument("--seed", type=int, default=7, help="fixes every price and every id")
    ap.add_argument("--cash", default="100000")
    ap.add_argument("--rate-limit", type=int, default=200, help="requests a minute, per API")
    args = ap.parse_args(argv)
    fake = FakeAlpaca(key_id=args.key_id, secret=args.secret, plan=args.plan, seed=args.seed, market=args.market, cash=args.cash, port=args.port,
                      rate_limit=args.rate_limit).start()
    print(json.dumps({"port": fake.port, "trading_url": fake.url, "data_url": fake.url, "stream_url": fake.stream_url, "key_id": fake.key_id,
                      "secret": fake.secret, "plan": fake.plan, "market": fake.market.state, "seed": args.seed}), flush=True)
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
