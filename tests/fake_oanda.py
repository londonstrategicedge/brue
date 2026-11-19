#!/usr/bin/env python3
"""A local stand-in for OANDA's v20 API (the fxTrade Practice hosts): REST on one loopback port, the two chunked
JSON-lines streams on a second, as OANDA serves them from two hosts. For testing an adapter with no key and no network.

Written on 2026-11-14 from OANDA's official documentation alone (no adapter for OANDA existed yet), read that day:
  https://developer.oanda.com/rest-live-v20/  development-guide, authentication, best-practices, troubleshooting-errors,
      introduction, account-ep, account-df, instrument-df, pricing-ep, pricing-df, pricing-common-df, primitives-df,
      order-ep, order-df, trade-ep, trade-df, position-ep, position-df, transaction-ep, transaction-df
  https://legal.oanda.com/oel/execution_policy_oel/en                     how orders fill (OANDA Europe, May 2026)
  https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf   margin, hedging, financing
  https://www.oanda.com/uk-en/trading/hours-of-operation/                 trading hours
and from what was measured over weeks on a live account: streams closed from OANDA's side, one 401 on a reconnect with
a valid token, the daily 16:59:05-17:04:55 New York non-tradeable spell, each instrument's latest price re-sent with its
original time on connect, the TRY pairs' 07:00-16:00 London hours.

Every behaviour carries a comment: "documented: <URL>" (what a page says), "measured on a live account" (what was seen
there), or "stand-in's own" (a choice the documentation does not settle; the real practice account must settle it).
Pure standard library, Python 3.10+.

    python tests/fake_oanda.py --port 0          # prints one JSON line (urls, token, accounts) and serves until killed

    fake = FakeOanda().start()                   # in a test: fake.rest_url, fake.stream_url, fake.token, fake.account_ids
    ...; fake.stop()

Served as OANDA serves them ("Authorization: Bearer <token>"; numbers as strings; RFC 3339 or UNIX times):
  REST    GET /v3/accounts   GET /v3/accounts/{id}[/summary|/instruments|/changes]   PATCH /v3/accounts/{id}/configuration
          GET /v3/accounts/{id}/instruments/{instrument}/candles   GET /v3/accounts/{id}/candles/latest
          GET /v3/accounts/{id}/pricing
          POST|GET /v3/accounts/{id}/orders   GET .../pendingOrders   GET|PUT .../orders/{spec}
          PUT .../orders/{spec}/cancel   PUT .../orders/{spec}/clientExtensions
          GET .../trades   GET .../openTrades   GET .../trades/{spec}   PUT .../trades/{spec}/close|clientExtensions|orders
          GET .../positions   GET .../openPositions   GET .../positions/{instrument}   PUT .../positions/{instrument}/close
          GET .../transactions   GET .../transactions/{id}   GET .../transactions/idrange   GET .../transactions/sinceid
  stream  GET /v3/accounts/{id}/pricing/stream   GET /v3/accounts/{id}/transactions/stream   (chunked, one JSON per line)

Control, for tests (on the REST port; no token, never rate limited, never in the request log; each is also a method):
  POST /_control/price        {"instrument", "bid", "ask"} hold there | {"instrument", "mid", "spread"?} | {"instrument",
                              "walk": true} resume the walk | {"instrument", "jump_pct"} | {"instrument", "ramp_to", "seconds"}
  POST /_control/market       {"state": "open" | "closed" | "auto", "instrument"?}   auto follows OANDA's hours
  POST /_control/clock        {"advance_s": N} | {"now": "RFC 3339"} | {"reset": true}
  POST /_control/liquidity    {"instrument", "units": N | null, "count"?}   the next `count` fills (all, when no count)
                              see only N units: a market FOK is killed, a market IOC part-fills, a resting order waits
  POST /_control/refuse       {"reason": <documented reject or cancel reason>, "count"?, "kind"?}   the next order request
  POST /_control/token        {"action": "revoke" | "rotate" | "restore"}           returns {"token"}
  POST /_control/connects     {"count", "status": 401, "where": "stream" | "rest" | "any"}   refuse though the token is good
  POST /_control/drop_streams {"clean": false, "kind": "pricing" | "transactions" | null}
  POST /_control/fail         {"status": 429 | 500 | 502 | 503 | 504, "count", "path"?}
  POST /_control/redirect     {"count", "path"?, "method"?, "status": 307 | 308 | 301 | 302 | 303, "location"?,
                              "kind": "rest" | "stream"}   answered so (Location: `location`, or the same request on its
                              own host), not carried out; "stream" for the streaming host (the default is "rest")
  POST /_control/transfer     {"account", "amount", "reason"?, "comment"?}            TRANSFER_FUNDS
  POST /_control/dividend     {"instrument", "per_unit"}                               DIVIDEND_ADJUSTMENT
  POST /_control/add_account  {"currency", "hedging", "alias"}                         seen only after a new token
  POST /_control/options      {"foreign_account_status": 401|403, "client_id_lookup": "pending"|"any",
                              "closeout_extensions": "documented"|"fill"|"none"}   a close's marks kept as documented, or not
  POST /_control/reset  {}    POST /_control/settle {"timeout_s"}
  GET  /_control/requests[?clear=1]   GET /_control/state   GET /_control/transactions?account=   GET /_control/violations

The market inside (catalog values are stand-in's own; the field names are documented): currency pairs EUR_USD GBP_USD
USD_JPY EUR_GBP AUD_USD USD_CHF USD_CAD NZD_USD EUR_JPY USD_TRY, metals XAU_USD XAG_USD, CFDs UK100_GBP SPX500_USD
WTICO_USD UK10YB_GBP. Each price is a seeded walk fixed by (seed, instrument, time) from 2005-01-01, so a 5,000-bar
page exists at every granularity, and candles of every granularity are read from that one path.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import hashlib
import json
import math
import random
import re
import select
import socket
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

try:                                    # stdlib since 3.9; only used for an alignmentTimezone the stand-in has no rule for
    import zoneinfo
except ImportError:                     # pragma: no cover
    zoneinfo = None

NS = 10 ** 9
MS_NS = 10 ** 6
HOUR_MS = 3600000
DAY_MS = 86400000
D0 = Decimal(0)
Q4 = Decimal("0.0001")
# documented: https://developer.oanda.com/rest-live-v20/introduction/ "Access historical pricing information dating back
# to 2005". The walk starts on 2005-01-01 00:00 UTC; no candle exists before it.
HISTORY_START_MS = 1104537600000
DAY0 = HISTORY_START_MS // DAY_MS

DOCS = "https://developer.oanda.com/rest-live-v20/"
URLS = {
    "development-guide": DOCS + "development-guide/", "authentication": DOCS + "authentication/",
    "best-practices": DOCS + "best-practices/", "errors": DOCS + "troubleshooting-errors/",
    "introduction": DOCS + "introduction/", "account-ep": DOCS + "account-ep/", "account-df": DOCS + "account-df/",
    "instrument-df": DOCS + "instrument-df/", "pricing-ep": DOCS + "pricing-ep/", "pricing-df": DOCS + "pricing-df/",
    "pricing-common-df": DOCS + "pricing-common-df/", "primitives-df": DOCS + "primitives-df/",
    "order-ep": DOCS + "order-ep/", "order-df": DOCS + "order-df/", "trade-ep": DOCS + "trade-ep/",
    "trade-df": DOCS + "trade-df/", "position-ep": DOCS + "position-ep/", "position-df": DOCS + "position-df/",
    "transaction-ep": DOCS + "transaction-ep/", "transaction-df": DOCS + "transaction-df/",
    "execution-policy": "https://legal.oanda.com/oel/execution_policy_oel/en",
    "sub-accounts": "https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf",
    "hours": "https://www.oanda.com/uk-en/trading/hours-of-operation/",
    "measured": "measured on a live account",
}

# documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ (the messages, word for word)
MSG_UNAUTHORIZED = "Insufficient authorization to perform request."
MSG_FORBIDDEN = "The provided request was forbidden."
MSG_MISSING = "The request was missing required data"
MSG_NO_TRADE = "The trade ID specified does not exist"
MSG_NO_ORDER = "The order ID specified does not exist"
MSG_NO_TRANSACTION = "The transaction ID specified does not exist"


def invalid(name):
    # documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ gives "Invalid value specified for
    # 'instrument'"; the same wording for the other parameters is stand-in's own.
    return "Invalid value specified for '%s'" % name


# ---------------------------------------------------------------- what the documentation enumerates

# documented: https://developer.oanda.com/rest-live-v20/instrument-df/ CandlestickGranularity (21 values); the length of
# the fixed ones in milliseconds. W and M have none ("aligned to start of week", "aligned to first day of the month").
GRANULARITIES = {"S5": 5000, "S10": 10000, "S15": 15000, "S30": 30000, "M1": 60000, "M2": 120000, "M4": 240000,
                 "M5": 300000, "M10": 600000, "M15": 900000, "M30": 1800000, "H1": 3600000, "H2": 7200000,
                 "H3": 10800000, "H4": 14400000, "H6": 21600000, "H8": 28800000, "H12": 43200000, "D": 86400000,
                 "W": None, "M": None}
# documented: https://developer.oanda.com/rest-live-v20/instrument-df/ "H2 ... H12, D: day alignment"; the rest of the
# fixed ones have minute or hour alignment.
DAY_ALIGNED = frozenset(("H2", "H3", "H4", "H6", "H8", "H12", "D"))
# documented: https://developer.oanda.com/rest-live-v20/instrument-df/ WeeklyAlignment
WEEKLY_ALIGNMENTS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_NAMES = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")   # primitives-df DayOfWeek

# documented: https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionType (the 38 values the page lists)
TRANSACTION_TYPES = (
    "CREATE", "CLOSE", "REOPEN", "CLIENT_CONFIGURE", "CLIENT_CONFIGURE_REJECT", "TRANSFER_FUNDS", "TRANSFER_FUNDS_REJECT",
    "MARKET_ORDER", "MARKET_ORDER_REJECT", "FIXED_PRICE_ORDER", "LIMIT_ORDER", "LIMIT_ORDER_REJECT", "STOP_ORDER",
    "STOP_ORDER_REJECT", "MARKET_IF_TOUCHED_ORDER", "MARKET_IF_TOUCHED_ORDER_REJECT", "TAKE_PROFIT_ORDER",
    "TAKE_PROFIT_ORDER_REJECT", "STOP_LOSS_ORDER", "STOP_LOSS_ORDER_REJECT", "GUARANTEED_STOP_LOSS_ORDER",
    "GUARANTEED_STOP_LOSS_ORDER_REJECT", "TRAILING_STOP_LOSS_ORDER", "TRAILING_STOP_LOSS_ORDER_REJECT", "ORDER_FILL",
    "ORDER_CANCEL", "ORDER_CANCEL_REJECT", "ORDER_CLIENT_EXTENSIONS_MODIFY", "ORDER_CLIENT_EXTENSIONS_MODIFY_REJECT",
    "TRADE_CLIENT_EXTENSIONS_MODIFY", "TRADE_CLIENT_EXTENSIONS_MODIFY_REJECT", "MARGIN_CALL_ENTER", "MARGIN_CALL_EXTEND",
    "MARGIN_CALL_EXIT", "DELAYED_TRADE_CLOSURE", "DAILY_FINANCING", "DIVIDEND_ADJUSTMENT", "RESET_RESETTABLE_PL")

# documented: https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionFilter. ORDER, FUNDING and ADMIN are
# groups whose members the page does not list; the grouping below is stand-in's own.
FILTER_GROUPS = {
    "ORDER": frozenset(t for t in TRANSACTION_TYPES if t.endswith(("_ORDER", "_ORDER_REJECT")) or t.startswith(
        ("ORDER_", "TRADE_CLIENT_EXTENSIONS"))),
    "FUNDING": frozenset(("TRANSFER_FUNDS", "TRANSFER_FUNDS_REJECT", "DAILY_FINANCING", "DIVIDEND_ADJUSTMENT")),
    "ADMIN": frozenset(("CREATE", "CLOSE", "REOPEN", "CLIENT_CONFIGURE", "CLIENT_CONFIGURE_REJECT",
                        "RESET_RESETTABLE_PL", "MARGIN_CALL_ENTER", "MARGIN_CALL_EXTEND", "MARGIN_CALL_EXIT",
                        "DELAYED_TRADE_CLOSURE")),
}
# documented: https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionFilter, the 42 values the page lists
# (read 2026-11-14): every value of it and nothing else is accepted as a `type` filter. Not every TransactionType is
# one: FIXED_PRICE_ORDER and DIVIDEND_ADJUSTMENT are not (a stand-in that accepts every TransactionType hides an
# adapter that filters on DIVIDEND_ADJUSTMENT).
TRANSACTION_FILTERS = frozenset("""
ORDER FUNDING ADMIN CREATE CLOSE REOPEN CLIENT_CONFIGURE CLIENT_CONFIGURE_REJECT TRANSFER_FUNDS TRANSFER_FUNDS_REJECT
MARKET_ORDER MARKET_ORDER_REJECT LIMIT_ORDER LIMIT_ORDER_REJECT STOP_ORDER STOP_ORDER_REJECT MARKET_IF_TOUCHED_ORDER
MARKET_IF_TOUCHED_ORDER_REJECT TAKE_PROFIT_ORDER TAKE_PROFIT_ORDER_REJECT STOP_LOSS_ORDER STOP_LOSS_ORDER_REJECT
GUARANTEED_STOP_LOSS_ORDER GUARANTEED_STOP_LOSS_ORDER_REJECT TRAILING_STOP_LOSS_ORDER TRAILING_STOP_LOSS_ORDER_REJECT
ONE_CANCELS_ALL_ORDER ONE_CANCELS_ALL_ORDER_REJECT ONE_CANCELS_ALL_ORDER_TRIGGERED ORDER_FILL ORDER_CANCEL
ORDER_CANCEL_REJECT ORDER_CLIENT_EXTENSIONS_MODIFY ORDER_CLIENT_EXTENSIONS_MODIFY_REJECT TRADE_CLIENT_EXTENSIONS_MODIFY
TRADE_CLIENT_EXTENSIONS_MODIFY_REJECT MARGIN_CALL_ENTER MARGIN_CALL_EXTEND MARGIN_CALL_EXIT DELAYED_TRADE_CLOSURE
DAILY_FINANCING RESET_RESETTABLE_PL""".split())

# documented: https://developer.oanda.com/rest-live-v20/transaction-df/ TransactionRejectReason (all 198 values)
REJECT_REASONS = frozenset("""
INTERNAL_SERVER_ERROR INSTRUMENT_PRICE_UNKNOWN ACCOUNT_NOT_ACTIVE ACCOUNT_LOCKED ACCOUNT_ORDER_CREATION_LOCKED
ACCOUNT_CONFIGURATION_LOCKED ACCOUNT_DEPOSIT_LOCKED ACCOUNT_WITHDRAWAL_LOCKED ACCOUNT_ORDER_CANCEL_LOCKED
INSTRUMENT_NOT_TRADEABLE PENDING_ORDERS_ALLOWED_EXCEEDED ORDER_ID_UNSPECIFIED ORDER_DOESNT_EXIST
ORDER_IDENTIFIER_INCONSISTENCY TRADE_ID_UNSPECIFIED TRADE_DOESNT_EXIST TRADE_IDENTIFIER_INCONSISTENCY INSUFFICIENT_MARGIN
INSTRUMENT_MISSING INSTRUMENT_UNKNOWN UNITS_MISSING UNITS_INVALID UNITS_PRECISION_EXCEEDED UNITS_LIMIT_EXCEEDED
UNITS_MINIMUM_NOT_MET PRICE_MISSING PRICE_INVALID PRICE_PRECISION_EXCEEDED PRICE_DISTANCE_MISSING PRICE_DISTANCE_INVALID
PRICE_DISTANCE_PRECISION_EXCEEDED PRICE_DISTANCE_MAXIMUM_EXCEEDED PRICE_DISTANCE_MINIMUM_NOT_MET TIME_IN_FORCE_MISSING
TIME_IN_FORCE_INVALID TIME_IN_FORCE_GTD_TIMESTAMP_MISSING TIME_IN_FORCE_GTD_TIMESTAMP_IN_PAST PRICE_BOUND_INVALID
PRICE_BOUND_PRECISION_EXCEEDED ORDERS_ON_FILL_DUPLICATE_CLIENT_ORDER_IDS TRADE_ON_FILL_CLIENT_EXTENSIONS_NOT_SUPPORTED
CLIENT_ORDER_ID_INVALID CLIENT_ORDER_ID_ALREADY_EXISTS CLIENT_ORDER_TAG_INVALID CLIENT_ORDER_COMMENT_INVALID
CLIENT_TRADE_ID_INVALID CLIENT_TRADE_ID_ALREADY_EXISTS CLIENT_TRADE_TAG_INVALID CLIENT_TRADE_COMMENT_INVALID
ORDER_FILL_POSITION_ACTION_MISSING ORDER_FILL_POSITION_ACTION_INVALID TRIGGER_CONDITION_MISSING TRIGGER_CONDITION_INVALID
ORDER_PARTIAL_FILL_OPTION_MISSING ORDER_PARTIAL_FILL_OPTION_INVALID INVALID_REISSUE_IMMEDIATE_PARTIAL_FILL
ORDERS_ON_FILL_RMO_MUTUAL_EXCLUSIVITY_MUTUALLY_EXCLUSIVE_VIOLATION
ORDERS_ON_FILL_RMO_MUTUAL_EXCLUSIVITY_GSLO_EXCLUDES_OTHERS_VIOLATION TAKE_PROFIT_ORDER_ALREADY_EXISTS
TAKE_PROFIT_ORDER_WOULD_VIOLATE_FIFO_VIOLATION_SAFEGUARD TAKE_PROFIT_ON_FILL_PRICE_MISSING TAKE_PROFIT_ON_FILL_PRICE_INVALID
TAKE_PROFIT_ON_FILL_PRICE_PRECISION_EXCEEDED TAKE_PROFIT_ON_FILL_TIME_IN_FORCE_MISSING TAKE_PROFIT_ON_FILL_TIME_IN_FORCE_INVALID
TAKE_PROFIT_ON_FILL_GTD_TIMESTAMP_MISSING TAKE_PROFIT_ON_FILL_GTD_TIMESTAMP_IN_PAST TAKE_PROFIT_ON_FILL_CLIENT_ORDER_ID_INVALID
TAKE_PROFIT_ON_FILL_CLIENT_ORDER_TAG_INVALID TAKE_PROFIT_ON_FILL_CLIENT_ORDER_COMMENT_INVALID
TAKE_PROFIT_ON_FILL_TRIGGER_CONDITION_MISSING TAKE_PROFIT_ON_FILL_TRIGGER_CONDITION_INVALID STOP_LOSS_ORDER_ALREADY_EXISTS
STOP_LOSS_ORDER_GUARANTEED_REQUIRED STOP_LOSS_ORDER_GUARANTEED_PRICE_WITHIN_SPREAD STOP_LOSS_ORDER_GUARANTEED_NOT_ALLOWED
STOP_LOSS_ORDER_GUARANTEED_HALTED_CREATE_VIOLATION STOP_LOSS_ORDER_GUARANTEED_HALTED_TIGHTEN_VIOLATION
STOP_LOSS_ORDER_GUARANTEED_HEDGING_NOT_ALLOWED STOP_LOSS_ORDER_GUARANTEED_MINIMUM_DISTANCE_NOT_MET
STOP_LOSS_ORDER_NOT_CANCELABLE STOP_LOSS_ORDER_NOT_REPLACEABLE STOP_LOSS_ORDER_GUARANTEED_LEVEL_RESTRICTION_EXCEEDED
STOP_LOSS_ORDER_PRICE_AND_DISTANCE_BOTH_SPECIFIED STOP_LOSS_ORDER_PRICE_AND_DISTANCE_BOTH_MISSING
STOP_LOSS_ORDER_WOULD_VIOLATE_FIFO_VIOLATION_SAFEGUARD STOP_LOSS_ORDER_RMO_MUTUAL_EXCLUSIVITY_MUTUALLY_EXCLUSIVE_VIOLATION
STOP_LOSS_ORDER_RMO_MUTUAL_EXCLUSIVITY_GSLO_EXCLUDES_OTHERS_VIOLATION STOP_LOSS_ON_FILL_REQUIRED_FOR_PENDING_ORDER
STOP_LOSS_ON_FILL_GUARANTEED_NOT_ALLOWED STOP_LOSS_ON_FILL_GUARANTEED_REQUIRED STOP_LOSS_ON_FILL_PRICE_MISSING
STOP_LOSS_ON_FILL_PRICE_INVALID STOP_LOSS_ON_FILL_PRICE_PRECISION_EXCEEDED STOP_LOSS_ON_FILL_GUARANTEED_MINIMUM_DISTANCE_NOT_MET
STOP_LOSS_ON_FILL_GUARANTEED_LEVEL_RESTRICTION_EXCEEDED STOP_LOSS_ON_FILL_DISTANCE_INVALID
STOP_LOSS_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED STOP_LOSS_ON_FILL_DISTANCE_PRECISION_EXCEEDED
STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_SPECIFIED STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_MISSING
STOP_LOSS_ON_FILL_TIME_IN_FORCE_MISSING STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID STOP_LOSS_ON_FILL_GTD_TIMESTAMP_MISSING
STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST STOP_LOSS_ON_FILL_CLIENT_ORDER_ID_INVALID STOP_LOSS_ON_FILL_CLIENT_ORDER_TAG_INVALID
STOP_LOSS_ON_FILL_CLIENT_ORDER_COMMENT_INVALID STOP_LOSS_ON_FILL_TRIGGER_CONDITION_MISSING
STOP_LOSS_ON_FILL_TRIGGER_CONDITION_INVALID GUARANTEED_STOP_LOSS_ORDER_ALREADY_EXISTS GUARANTEED_STOP_LOSS_ORDER_REQUIRED
GUARANTEED_STOP_LOSS_ORDER_PRICE_WITHIN_SPREAD GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED
GUARANTEED_STOP_LOSS_ORDER_HALTED_CREATE_VIOLATION GUARANTEED_STOP_LOSS_ORDER_CREATE_VIOLATION
GUARANTEED_STOP_LOSS_ORDER_HALTED_TIGHTEN_VIOLATION GUARANTEED_STOP_LOSS_ORDER_TIGHTEN_VIOLATION
GUARANTEED_STOP_LOSS_ORDER_HEDGING_NOT_ALLOWED GUARANTEED_STOP_LOSS_ORDER_MINIMUM_DISTANCE_NOT_MET
GUARANTEED_STOP_LOSS_ORDER_NOT_CANCELABLE GUARANTEED_STOP_LOSS_ORDER_HALTED_NOT_CANCELABLE
GUARANTEED_STOP_LOSS_ORDER_NOT_REPLACEABLE GUARANTEED_STOP_LOSS_ORDER_HALTED_NOT_REPLACEABLE
GUARANTEED_STOP_LOSS_ORDER_LEVEL_RESTRICTION_VOLUME_EXCEEDED GUARANTEED_STOP_LOSS_ORDER_LEVEL_RESTRICTION_PRICE_RANGE_EXCEEDED
GUARANTEED_STOP_LOSS_ORDER_PRICE_AND_DISTANCE_BOTH_SPECIFIED GUARANTEED_STOP_LOSS_ORDER_PRICE_AND_DISTANCE_BOTH_MISSING
GUARANTEED_STOP_LOSS_ORDER_WOULD_VIOLATE_FIFO_VIOLATION_SAFEGUARD
GUARANTEED_STOP_LOSS_ORDER_RMO_MUTUAL_EXCLUSIVITY_MUTUALLY_EXCLUSIVE_VIOLATION
GUARANTEED_STOP_LOSS_ORDER_RMO_MUTUAL_EXCLUSIVITY_GSLO_EXCLUDES_OTHERS_VIOLATION
GUARANTEED_STOP_LOSS_ON_FILL_REQUIRED_FOR_PENDING_ORDER GUARANTEED_STOP_LOSS_ON_FILL_NOT_ALLOWED
GUARANTEED_STOP_LOSS_ON_FILL_REQUIRED GUARANTEED_STOP_LOSS_ON_FILL_PRICE_MISSING GUARANTEED_STOP_LOSS_ON_FILL_PRICE_INVALID
GUARANTEED_STOP_LOSS_ON_FILL_PRICE_PRECISION_EXCEEDED GUARANTEED_STOP_LOSS_ON_FILL_MINIMUM_DISTANCE_NOT_MET
GUARANTEED_STOP_LOSS_ON_FILL_LEVEL_RESTRICTION_VOLUME_EXCEEDED
GUARANTEED_STOP_LOSS_ON_FILL_LEVEL_RESTRICTION_PRICE_RANGE_EXCEEDED GUARANTEED_STOP_LOSS_ON_FILL_DISTANCE_INVALID
GUARANTEED_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED GUARANTEED_STOP_LOSS_ON_FILL_DISTANCE_PRECISION_EXCEEDED
GUARANTEED_STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_SPECIFIED GUARANTEED_STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_MISSING
GUARANTEED_STOP_LOSS_ON_FILL_TIME_IN_FORCE_MISSING GUARANTEED_STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID
GUARANTEED_STOP_LOSS_ON_FILL_GTD_TIMESTAMP_MISSING GUARANTEED_STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST
GUARANTEED_STOP_LOSS_ON_FILL_CLIENT_ORDER_ID_INVALID GUARANTEED_STOP_LOSS_ON_FILL_CLIENT_ORDER_TAG_INVALID
GUARANTEED_STOP_LOSS_ON_FILL_CLIENT_ORDER_COMMENT_INVALID GUARANTEED_STOP_LOSS_ON_FILL_TRIGGER_CONDITION_MISSING
GUARANTEED_STOP_LOSS_ON_FILL_TRIGGER_CONDITION_INVALID TRAILING_STOP_LOSS_ORDER_ALREADY_EXISTS
TRAILING_STOP_LOSS_ORDER_WOULD_VIOLATE_FIFO_VIOLATION_SAFEGUARD
TRAILING_STOP_LOSS_ORDER_RMO_MUTUAL_EXCLUSIVITY_MUTUALLY_EXCLUSIVE_VIOLATION
TRAILING_STOP_LOSS_ORDER_RMO_MUTUAL_EXCLUSIVITY_GSLO_EXCLUDES_OTHERS_VIOLATION TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MISSING
TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_INVALID TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_PRECISION_EXCEEDED
TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MINIMUM_NOT_MET
TRAILING_STOP_LOSS_ON_FILL_TIME_IN_FORCE_MISSING TRAILING_STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID
TRAILING_STOP_LOSS_ON_FILL_GTD_TIMESTAMP_MISSING TRAILING_STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST
TRAILING_STOP_LOSS_ON_FILL_CLIENT_ORDER_ID_INVALID TRAILING_STOP_LOSS_ON_FILL_CLIENT_ORDER_TAG_INVALID
TRAILING_STOP_LOSS_ON_FILL_CLIENT_ORDER_COMMENT_INVALID TRAILING_STOP_LOSS_ORDERS_NOT_SUPPORTED
TRAILING_STOP_LOSS_ON_FILL_TRIGGER_CONDITION_MISSING TRAILING_STOP_LOSS_ON_FILL_TRIGGER_CONDITION_INVALID
CLOSE_TRADE_TYPE_MISSING CLOSE_TRADE_PARTIAL_UNITS_MISSING CLOSE_TRADE_UNITS_EXCEED_TRADE_SIZE CLOSEOUT_POSITION_DOESNT_EXIST
CLOSEOUT_POSITION_INCOMPLETE_SPECIFICATION CLOSEOUT_POSITION_UNITS_EXCEED_POSITION_SIZE CLOSEOUT_POSITION_REJECT
CLOSEOUT_POSITION_PARTIAL_UNITS_MISSING MARKUP_GROUP_ID_INVALID POSITION_AGGREGATION_MODE_INVALID ADMIN_CONFIGURE_DATA_MISSING
MARGIN_RATE_INVALID MARGIN_RATE_WOULD_TRIGGER_CLOSEOUT ALIAS_INVALID CLIENT_CONFIGURE_DATA_MISSING
MARGIN_RATE_WOULD_TRIGGER_MARGIN_CALL AMOUNT_INVALID INSUFFICIENT_FUNDS AMOUNT_MISSING FUNDING_REASON_MISSING
OCA_ORDER_IDS_STOP_LOSS_NOT_ALLOWED CLIENT_EXTENSIONS_DATA_MISSING REPLACING_ORDER_INVALID REPLACING_TRADE_ID_INVALID
ORDER_CANCEL_WOULD_TRIGGER_CLOSEOUT
""".split())

# documented: https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason (all 65 values)
CANCEL_REASONS = frozenset("""
INTERNAL_SERVER_ERROR ACCOUNT_LOCKED ACCOUNT_NEW_POSITIONS_LOCKED ACCOUNT_ORDER_CREATION_LOCKED ACCOUNT_ORDER_FILL_LOCKED
CLIENT_REQUEST MIGRATION MARKET_HALTED LINKED_TRADE_CLOSED TIME_IN_FORCE_EXPIRED INSUFFICIENT_MARGIN FIFO_VIOLATION
BOUNDS_VIOLATION CLIENT_REQUEST_REPLACED DIVIDEND_ADJUSTMENT_REPLACED INSUFFICIENT_LIQUIDITY
TAKE_PROFIT_ON_FILL_GTD_TIMESTAMP_IN_PAST TAKE_PROFIT_ON_FILL_LOSS LOSING_TAKE_PROFIT STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST
STOP_LOSS_ON_FILL_LOSS STOP_LOSS_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED STOP_LOSS_ON_FILL_REQUIRED
STOP_LOSS_ON_FILL_GUARANTEED_REQUIRED STOP_LOSS_ON_FILL_GUARANTEED_NOT_ALLOWED
STOP_LOSS_ON_FILL_GUARANTEED_MINIMUM_DISTANCE_NOT_MET STOP_LOSS_ON_FILL_GUARANTEED_LEVEL_RESTRICTION_EXCEEDED
STOP_LOSS_ON_FILL_GUARANTEED_HEDGING_NOT_ALLOWED STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID
STOP_LOSS_ON_FILL_TRIGGER_CONDITION_INVALID GUARANTEED_STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST
GUARANTEED_STOP_LOSS_ON_FILL_LOSS GUARANTEED_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED
GUARANTEED_STOP_LOSS_ON_FILL_REQUIRED GUARANTEED_STOP_LOSS_ON_FILL_NOT_ALLOWED
GUARANTEED_STOP_LOSS_ON_FILL_MINIMUM_DISTANCE_NOT_MET GUARANTEED_STOP_LOSS_ON_FILL_LEVEL_RESTRICTION_VOLUME_EXCEEDED
GUARANTEED_STOP_LOSS_ON_FILL_LEVEL_RESTRICTION_PRICE_RANGE_EXCEEDED GUARANTEED_STOP_LOSS_ON_FILL_HEDGING_NOT_ALLOWED
GUARANTEED_STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID GUARANTEED_STOP_LOSS_ON_FILL_TRIGGER_CONDITION_INVALID
TAKE_PROFIT_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED TRAILING_STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST
CLIENT_TRADE_ID_ALREADY_EXISTS POSITION_CLOSEOUT_FAILED OPEN_TRADES_ALLOWED_EXCEEDED PENDING_ORDERS_ALLOWED_EXCEEDED
TAKE_PROFIT_ON_FILL_CLIENT_ORDER_ID_ALREADY_EXISTS STOP_LOSS_ON_FILL_CLIENT_ORDER_ID_ALREADY_EXISTS
GUARANTEED_STOP_LOSS_ON_FILL_CLIENT_ORDER_ID_ALREADY_EXISTS TRAILING_STOP_LOSS_ON_FILL_CLIENT_ORDER_ID_ALREADY_EXISTS
POSITION_SIZE_EXCEEDED HEDGING_GSLO_VIOLATION ACCOUNT_POSITION_VALUE_LIMIT_EXCEEDED INSTRUMENT_BID_REDUCE_ONLY
INSTRUMENT_ASK_REDUCE_ONLY INSTRUMENT_BID_HALTED INSTRUMENT_ASK_HALTED STOP_LOSS_ON_FILL_GUARANTEED_BID_HALTED
STOP_LOSS_ON_FILL_GUARANTEED_ASK_HALTED GUARANTEED_STOP_LOSS_ON_FILL_BID_HALTED GUARANTEED_STOP_LOSS_ON_FILL_ASK_HALTED
FIFO_VIOLATION_SAFEGUARD_VIOLATION FIFO_VIOLATION_SAFEGUARD_PARTIAL_CLOSE_VIOLATION
ORDERS_ON_FILL_RMO_MUTUAL_EXCLUSIVITY_MUTUALLY_EXCLUSIVE_VIOLATION
""".split())

# documented: https://developer.oanda.com/rest-live-v20/transaction-df/ the description of each reason the stand-in
# raises, word for word (curly apostrophes made plain). A reject's errorMessage carrying its reason's description is
# stand-in's own; the field names errorCode and errorMessage are documented on order-ep.
REASON_TEXT = {
    "INTERNAL_SERVER_ERROR": "An unexpected internal server error has occurred",
    "INSTRUMENT_PRICE_UNKNOWN": "The system was unable to determine the current price for the Order's instrument",
    "ACCOUNT_NOT_ACTIVE": "The Account is not active",
    "ACCOUNT_LOCKED": "The Account is locked",
    "ACCOUNT_ORDER_CREATION_LOCKED": "The Account is locked for Order creation",
    "ACCOUNT_ORDER_CANCEL_LOCKED": "The Account is locked for Order cancellation",
    "INSTRUMENT_NOT_TRADEABLE": "The instrument specified is not tradeable by the Account",
    "PENDING_ORDERS_ALLOWED_EXCEEDED": "Creating the Order would result in the maximum number of allowed pending Orders being exceeded",
    "ORDER_DOESNT_EXIST": "The Order specified does not exist",
    "ORDER_IDENTIFIER_INCONSISTENCY": "The Order ID and client Order ID specified do not identify the same Order",
    "TRADE_DOESNT_EXIST": "The Trade specified does not exist",
    "INSUFFICIENT_MARGIN": "The Account had insufficient margin to perform the action specified.",
    "INSTRUMENT_MISSING": "Order instrument has not been specified",
    "INSTRUMENT_UNKNOWN": "The instrument specified is unknown",
    "UNITS_MISSING": "Order units have not been not specified",
    "UNITS_INVALID": "Order units specified are invalid",
    "UNITS_PRECISION_EXCEEDED": "The units specified contain more precision than is allowed for the Order's instrument",
    "UNITS_LIMIT_EXCEEDED": "The units specified exceeds the maximum number of units allowed",
    "UNITS_MINIMUM_NOT_MET": "The units specified is less than the minimum number of units required",
    "PRICE_MISSING": "The price has not been specified",
    "PRICE_INVALID": "The price specified is invalid",
    "PRICE_PRECISION_EXCEEDED": "The price specified contains more precision than is allowed for the instrument",
    "PRICE_DISTANCE_MISSING": "The price distance has not been specified",
    "PRICE_DISTANCE_INVALID": "The price distance specified is invalid",
    "PRICE_DISTANCE_PRECISION_EXCEEDED": "The price distance specified contains more precision than is allowed for the instrument",
    "PRICE_DISTANCE_MAXIMUM_EXCEEDED": "The price distance exceeds that maximum allowed amount",
    "PRICE_DISTANCE_MINIMUM_NOT_MET": "The price distance does not meet the minimum allowed amount",
    "TIME_IN_FORCE_MISSING": "The TimeInForce field has not been specified",
    "TIME_IN_FORCE_INVALID": "The TimeInForce specified is invalid",
    "TIME_IN_FORCE_GTD_TIMESTAMP_MISSING": "The TimeInForce is GTD but no GTD timestamp is provided",
    "TIME_IN_FORCE_GTD_TIMESTAMP_IN_PAST": "The TimeInForce is GTD but the GTD timestamp is in the past",
    "PRICE_BOUND_INVALID": "The price bound specified is invalid",
    "PRICE_BOUND_PRECISION_EXCEEDED": "The price bound specified contains more precision than is allowed for the Order's instrument",
    "CLIENT_ORDER_ID_INVALID": "The client Order ID specified is invalid",
    "CLIENT_ORDER_ID_ALREADY_EXISTS": "The client Order ID specified is already assigned to another pending Order",
    "CLIENT_ORDER_TAG_INVALID": "The client Order tag specified is invalid",
    "CLIENT_ORDER_COMMENT_INVALID": "The client Order comment specified is invalid",
    "CLIENT_TRADE_ID_INVALID": "The client Trade ID specified is invalid",
    "CLIENT_TRADE_ID_ALREADY_EXISTS": "The client Trade ID specified is already assigned to another open Trade",
    "ORDER_FILL_POSITION_ACTION_INVALID": "The OrderFillPositionAction specified is invalid",
    "TRIGGER_CONDITION_INVALID": "The TriggerCondition specified is invalid",
    "TAKE_PROFIT_ON_FILL_PRICE_MISSING": "The Take Profit on fill specified does not provide a price",
    "TAKE_PROFIT_ON_FILL_PRICE_INVALID": "The Take Profit on fill specified contains an invalid price",
    "TAKE_PROFIT_ON_FILL_PRICE_PRECISION_EXCEEDED": "The Take Profit on fill specified contains a price with more precision than is allowed by the Order's instrument",
    "TAKE_PROFIT_ON_FILL_TIME_IN_FORCE_INVALID": "The Take Profit on fill specifies an invalid TimeInForce",
    "TAKE_PROFIT_ON_FILL_GTD_TIMESTAMP_MISSING": "The Take Profit on fill specifies a GTD TimeInForce but does not provide a GTD timestamp",
    "TAKE_PROFIT_ON_FILL_GTD_TIMESTAMP_IN_PAST": "The Take Profit on fill specifies a GTD timestamp that is in the past",
    "STOP_LOSS_ON_FILL_PRICE_MISSING": "The Stop Loss on fill specified does not provide a price",
    "STOP_LOSS_ON_FILL_PRICE_INVALID": "The Stop Loss on fill specifies an invalid price",
    "STOP_LOSS_ON_FILL_PRICE_PRECISION_EXCEEDED": "The Stop Loss on fill specifies a price with more precision than is allowed by the Order's instrument",
    "STOP_LOSS_ON_FILL_DISTANCE_INVALID": "The Stop Loss on fill distance is invalid",
    "STOP_LOSS_ON_FILL_DISTANCE_PRECISION_EXCEEDED": "The Stop Loss on fill distance contains more precision than is allowed by the instrument",
    "STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_SPECIFIED": "The Stop Loss on fill contains both the price and distance fields.",
    "STOP_LOSS_ON_FILL_PRICE_AND_DISTANCE_BOTH_MISSING": "The Stop Loss on fill contains neither the price nor distance fields.",
    "STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID": "The Stop Loss on fill specifies an invalid TimeInForce",
    "STOP_LOSS_ON_FILL_GTD_TIMESTAMP_MISSING": "The Stop Loss on fill specifies a GTD TimeInForce but does not provide a GTD timestamp",
    "STOP_LOSS_ON_FILL_GTD_TIMESTAMP_IN_PAST": "The Stop Loss on fill specifies a GTD timestamp that is in the past",
    "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MISSING": "The Trailing Stop Loss on fill specified does not provide a distance",
    "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_INVALID": "The Trailing Stop Loss on fill distance is invalid",
    "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_PRECISION_EXCEEDED": "The Trailing Stop Loss on fill distance contains more precision than is allowed by the instrument",
    "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MAXIMUM_EXCEEDED": "The Trailing Stop Loss on fill price distance exceeds the maximum allowed amount",
    "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MINIMUM_NOT_MET": "The Trailing Stop Loss on fill price distance does not meet the minimum allowed amount",
    "TRAILING_STOP_LOSS_ON_FILL_TIME_IN_FORCE_INVALID": "The Trailing Stop Loss on fill specifies an invalid TimeInForce",
    "GUARANTEED_STOP_LOSS_ON_FILL_NOT_ALLOWED": "An attempt to create a pending Order was made with a Guaranteed Stop Loss Order on fill that was explicitly configured to be guaranteed, however guaranteed Stop Loss Orders are not allowed",
    "GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED": "An attempt was made to create a Guaranteed Stop Loss Order, however the Account's configuration does not allow Guaranteed Stop Loss Orders",
    "CLOSE_TRADE_PARTIAL_UNITS_MISSING": "The request to close a Trade partially did not specify the number of units to close",
    "CLOSE_TRADE_UNITS_EXCEED_TRADE_SIZE": "The request to partially close a Trade specifies a number of units that exceeds the current size of the given Trade",
    "CLOSEOUT_POSITION_DOESNT_EXIST": "The Position requested to be closed out does not exist",
    "CLOSEOUT_POSITION_INCOMPLETE_SPECIFICATION": "The request to closeout a Position was specified incompletely",
    "CLOSEOUT_POSITION_UNITS_EXCEED_POSITION_SIZE": "A partial Position closeout request specifies a number of units that exceeds the current Position",
    "CLOSEOUT_POSITION_PARTIAL_UNITS_MISSING": "The request to partially closeout a Position did not specify the number of units to close.",
    "ALIAS_INVALID": "The account alias string provided is invalid",
    "MARGIN_RATE_INVALID": "The margin rate provided is invalid",
    "MARGIN_RATE_WOULD_TRIGGER_CLOSEOUT": "The margin rate provided would cause an immediate margin closeout",
    "CLIENT_CONFIGURE_DATA_MISSING": "No configuration parameters provided",
    "AMOUNT_INVALID": "Funding is not possible because the requested transfer amount is invalid",
    "INSUFFICIENT_FUNDS": "The Account does not have sufficient balance to complete the funding request",
    "CLIENT_EXTENSIONS_DATA_MISSING": "Neither Order nor Trade on Fill client extensions were provided for modification",
    "REPLACING_ORDER_INVALID": "The Order to be replaced has a different type than the replacing Order.",
    "REPLACING_TRADE_ID_INVALID": "The replacing Order refers to a different Trade than the Order that is being replaced.",
    "TAKE_PROFIT_ORDER_ALREADY_EXISTS": "A Take Profit Order for the specified Trade already exists",
    "STOP_LOSS_ORDER_ALREADY_EXISTS": "A Stop Loss Order for the specified Trade already exists",
    "TRAILING_STOP_LOSS_ORDER_ALREADY_EXISTS": "A Trailing Stop Loss Order for the specified Trade already exists",
    "TRADE_ID_UNSPECIFIED": "Neither the Trade ID nor client Trade ID are specified",
    "ORDER_ID_UNSPECIFIED": "Neither the Order ID nor client Order ID are specified",
    # OrderCancelReason descriptions (documented, the same page)
    "MARKET_HALTED": "Filling the Order wasn't possible because the Order's instrument was halted.",
    "INSUFFICIENT_LIQUIDITY": "Filling the Order wasn't possible because enough liquidity available.",
    "BOUNDS_VIOLATION": "Filling the Order would have violated the Order's price bound.",
    "FIFO_VIOLATION": "Filling the Order would have resulted in a a FIFO violation.",
    "TIME_IN_FORCE_EXPIRED": "The time in force specified for this order has passed.",
    "LINKED_TRADE_CLOSED": "The Order is linked to an open Trade that was closed.",
    "CLIENT_REQUEST": "The Order was cancelled explicitly at the request of the client.",
    "CLIENT_REQUEST_REPLACED": "The Order was cancelled for replacement at the request of the client.",
}


def reason_text(reason):
    return REASON_TEXT.get(reason) or reason.replace("_", " ").capitalize()       # stand-in's own for the others


# ---------------------------------------------------------------- numbers

def dec(v):
    """A number of a request (JSON string or number) as a Decimal; None when it is not a finite number. Documented:
    https://developer.oanda.com/rest-live-v20/primitives-df/ DecimalNumber and PriceValue are strings; a JSON number is
    taken too, by its literal (stand-in's own)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        d = Decimal(v)
    elif isinstance(v, float):
        d = Decimal(repr(v))
    elif isinstance(v, str):
        s = v.strip()
        if not s or not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", s):
            return None
        d = Decimal(s)
    else:
        return None
    return d if d.is_finite() else None


def places(d):
    """How many decimals a Decimal was written with ("1.10" has 2)."""
    exp = d.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def money(d):
    """AccountUnits: documented as a decimal string whose precision 'depends on the Account's home currency'
    (https://developer.oanda.com/rest-live-v20/primitives-df/). Four decimals for every currency is stand-in's own
    (the documented examples print four and five)."""
    s = format(Decimal(d).quantize(Q4, rounding=ROUND_HALF_UP), "f")
    return "0.0000" if s == "-0.0000" else s


def plain(d):
    """A DecimalNumber as plain digits, no exponent, no trailing zeros ("100", "-100", "1.5"); stand-in's own spelling."""
    s = format(Decimal(d), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def factor(x):
    """A conversion factor or rate as a DecimalNumber string (stand-in's own: up to ten decimals)."""
    s = format(Decimal(x).quantize(Decimal("1e-10"), rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".")
    return s or "0"


def rnd(x, dp):
    """A float price on its instrument's grid, half up, as a Decimal."""
    scale = 10 ** dp
    return Decimal(math.floor(x * scale + 0.5)) / Decimal(scale)


def pstr(d, dp):
    """A PriceValue: documented as 'A decimal number encodes as a string. The amount of precision provided depends on the
    Instrument.' (https://developer.oanda.com/rest-live-v20/pricing-common-df/); displayPrecision decimals here."""
    return format(Decimal(d).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP), "f")


# ---------------------------------------------------------------- time

class Stamp:
    """A DateTime in a body. Documented (https://developer.oanda.com/rest-live-v20/primitives-df/ AcceptDatetimeFormat):
    RFC3339 is "YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ" and UNIX is "12345678.000000123"; the header picks one per request."""
    __slots__ = ("ns",)

    def __init__(self, ns):
        self.ns = int(ns)

    def render(self, fmt):
        return unix_str(self.ns) if fmt == "UNIX" else rfc3339(self.ns)

    def __repr__(self):
        return "Stamp(%s)" % rfc3339(self.ns)


def rfc3339(ns):
    s, f = divmod(int(ns), NS)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s)) + ".%09dZ" % f


def unix_str(ns):
    s, f = divmod(int(ns), NS)
    return "%d.%09d" % (s, f)


_RFC = re.compile(r"(\d{4})-(\d\d)-(\d\d)(?:[Tt ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,9}))?)?)?\s*(Z|z|[+-]\d\d:?\d\d)?")


def parse_time(text):
    """A request DateTime in nanoseconds since 1970. Both documented formats are read whatever the header says (stand-in's
    own leniency): RFC 3339 with Z or an offset, a bare date, or UNIX seconds with up to nine decimals."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no time")
    t = text.strip()
    if re.fullmatch(r"\d+(\.\d{1,9})?", t):
        whole, _, frac = t.partition(".")
        return int(whole) * NS + int((frac + "000000000")[:9])
    m = _RFC.fullmatch(t)
    if not m:
        raise ValueError("not a time: %r" % text)
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    secs = int(datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), tzinfo=timezone.utc).timestamp())
    if tz and tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        secs -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return secs * NS + int(((frac or "") + "000000000")[:9])


class Clock:
    """The server's clock: real time plus an offset that controls move forward. Stamps never repeat."""

    def __init__(self):
        self.offset_ns, self._last, self._lock = 0, 0, threading.Lock()

    def now_ns(self):
        return time.time_ns() + self.offset_ns

    def now_ms(self):
        return self.now_ns() // MS_NS

    def stamp(self):
        with self._lock:
            n = max(self.now_ns(), self._last + 1)
            self._last = n
            return n

    def stamp_at(self, ns):
        """A stamp at a given instant (an engine event in the past of a clock jump), never before the last one."""
        with self._lock:
            n = max(int(ns), self._last + 1)
            self._last = n
            return n


# ---------------------------------------------------------------- time zones (the stand-in's own rules, no tzdata needed)

def _sunday(y, m, n):
    """The n-th Sunday of a month (n = -1: the last)."""
    if n > 0:
        d = date(y, m, 1)
        d += timedelta((6 - d.weekday()) % 7)
        return d + timedelta(7 * (n - 1))
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(1)
    return d - timedelta((d.weekday() - 6) % 7)


def _epoch(d, hh=0, mm=0, ss=0):
    return int(datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=timezone.utc).timestamp())


class Zone:
    """A UTC offset with daylight saving, for the zones OANDA's hours are written in; the dates are the public rules (US
    since 2007 and before, EU, New Zealand since 2007 and before)."""

    def __init__(self, name, std, rule=None, dst=3600):
        self.name, self.std, self.rule, self.dst = name, std, rule, dst
        self._years = {}

    def _bounds(self, y):
        """(start, end) of daylight saving in UTC seconds for year y, or None."""
        b = self._years.get(y)
        if b is None and self.rule:
            std, dst = self.std, self.std + self.dst
            if self.rule == "us":
                if y >= 2007:
                    s, e = _sunday(y, 3, 2), _sunday(y, 11, 1)
                else:
                    s, e = _sunday(y, 4, 1), _sunday(y, 10, -1)
                b = (_epoch(s, 2) - std, _epoch(e, 2) - dst)
            elif self.rule == "eu":
                b = (_epoch(_sunday(y, 3, -1), 1), _epoch(_sunday(y, 10, -1), 1))
            elif self.rule == "nz":                  # southern: daylight from September/October into the next year
                start = _sunday(y, 9, -1) if y >= 2007 else _sunday(y, 10, 1)
                end = _sunday(y, 4, 1) if y >= 2008 else _sunday(y, 3, 3)
                b = (_epoch(start, 2) - std, _epoch(end, 3) - dst)
            self._years[y] = b
        return b

    def offset(self, utc_s):
        if not self.rule:
            return self.std
        y = time.gmtime(utc_s).tm_year
        s, e = self._bounds(y)
        if self.rule == "nz":
            return self.std + self.dst if (utc_s >= s or utc_s < e) else self.std
        return self.std + self.dst if s <= utc_s < e else self.std

    def local_to_utc(self, d, hh=0, mm=0, ss=0):
        naive = _epoch(d, hh, mm, ss)
        guess = naive - self.std
        off = self.offset(guess)
        utc = naive - off
        off2 = self.offset(utc)
        return naive - off2 if off2 != off else utc

    def local_date(self, utc_s):
        return (datetime.fromtimestamp(utc_s + self.offset(utc_s), tz=timezone.utc)).date()

    def local_hms(self, utc_s):
        t = time.gmtime(utc_s + self.offset(utc_s))
        return t.tm_hour, t.tm_min, t.tm_sec


class _InfoZone:
    """Any other IANA zone, through the standard library's zoneinfo (needs the system's tz database)."""

    def __init__(self, name):
        self.name, self.tz = name, zoneinfo.ZoneInfo(name)

    def offset(self, utc_s):
        return int(datetime.fromtimestamp(utc_s, tz=self.tz).utcoffset().total_seconds())

    def local_to_utc(self, d, hh=0, mm=0, ss=0):
        return int(datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=self.tz).timestamp())

    def local_date(self, utc_s):
        return datetime.fromtimestamp(utc_s, tz=self.tz).date()

    def local_hms(self, utc_s):
        t = datetime.fromtimestamp(utc_s, tz=self.tz)
        return t.hour, t.minute, t.second


_ZONES = {}
for _names, _args in ((("UTC", "Etc/UTC", "GMT", "Etc/GMT", "Z"), (0,)),
                      (("America/New_York", "US/Eastern", "EST5EDT"), (-18000, "us")),
                      (("America/Chicago", "US/Central", "CST6CDT"), (-21600, "us")),
                      (("Europe/London",), (0, "eu")),
                      (("Europe/Berlin", "Europe/Paris", "Europe/Zurich", "Europe/Warsaw", "CET"), (3600, "eu")),
                      (("Pacific/Auckland", "NZ"), (43200, "nz")),
                      (("Asia/Tokyo",), (32400,)), (("Asia/Singapore", "Asia/Hong_Kong"), (28800,))):
    for _n in _names:
        _ZONES[_n] = Zone(_n, *_args)
UTC = _ZONES["UTC"]
NEW_YORK = _ZONES["America/New_York"]


def zone(name):
    """A zone by IANA name; None when neither the stand-in's rules nor the system's tz database know it."""
    z = _ZONES.get(name)
    if z is None and zoneinfo is not None and isinstance(name, str) and name:
        try:
            z = _ZONES[name] = _InfoZone(name)
        except Exception:                  # ZoneInfoNotFoundError, ValueError for odd names
            return None
    return z


def seed_of(*parts):
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


# ---------------------------------------------------------------- the instruments and their hours

# The values are stand-in's own (plausible for an OANDA Europe v20 account); every field name is documented:
# https://developer.oanda.com/rest-live-v20/primitives-df/ Instrument. UK v20 deals "CFDs on forex, metals, indices,
# commodities and bonds" (documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf).
# ladder: the liquidity of each price bucket (documented: https://developer.oanda.com/rest-live-v20/pricing-common-df/ PriceBucket {price, liquidity}); each deeper
# bucket is one tick worse (stand-in's own). triple: the weekday financing is charged three days (stand-in's own).
_FX = dict(type="CURRENCY", pip=-4, dp=5, tup=0, min="1", max_units="100000000", margin="0.0333", min_tsl="0.00050",
           max_tsl="1.00000", min_gsl="0.00100", triple=2, ladder=(1000000, 2000000, 5000000), sched="FX")
_JPY = dict(_FX, pip=-2, dp=3, min_tsl="0.050", max_tsl="100.000", min_gsl="0.100")
CATALOG = [
    dict(_FX, name="EUR_USD", display="EUR/USD", start=1.16, vol=0.0045, spread=0.00014, long="-0.0371", short="0.0098"),
    dict(_FX, name="GBP_USD", display="GBP/USD", start=1.33, vol=0.005, spread=0.00018, long="-0.0262", short="-0.0041"),
    dict(_JPY, name="USD_JPY", display="USD/JPY", start=150.0, vol=0.005, spread=0.014, long="0.0303", short="-0.0612"),
    dict(_FX, name="EUR_GBP", display="EUR/GBP", start=0.87, vol=0.0035, spread=0.00016, long="-0.0201", short="-0.0063"),
    dict(_FX, name="AUD_USD", display="AUD/USD", margin="0.05", start=0.66, vol=0.006, spread=0.00014, long="-0.0227",
         short="-0.0081"),
    dict(_FX, name="USD_CHF", display="USD/CHF", start=0.80, vol=0.0045, spread=0.00017, long="0.0261", short="-0.0544"),
    dict(_FX, name="USD_CAD", display="USD/CAD", start=1.38, vol=0.004, spread=0.00022, long="-0.0042", short="-0.0213"),
    dict(_FX, name="NZD_USD", display="NZD/USD", margin="0.05", start=0.58, vol=0.006, spread=0.00021, long="-0.0236",
         short="-0.0072", sched="FX_NZD"),
    dict(_JPY, name="EUR_JPY", display="EUR/JPY", margin="0.05", start=172.0, vol=0.006, spread=0.021, long="0.0118",
         short="-0.0452"),
    dict(_FX, name="USD_TRY", display="USD/TRY", margin="0.05", start=41.0, vol=0.006, spread=0.008, long="-0.3812",
         short="0.2215", sched="TRY", ladder=(250000, 500000, 1000000)),
    dict(_FX, name="XAU_USD", type="METAL", display="Gold", pip=-2, dp=3, margin="0.05", min_tsl="0.050",
         max_tsl="1000.000", min_gsl="1.000", start=2650.0, vol=0.009, spread=0.35, long="-0.0559", short="0.0119",
         sched="METAL", ladder=(500, 1000, 2500)),
    dict(_FX, name="XAG_USD", type="METAL", display="Silver", margin="0.10", start=31.0, vol=0.014, spread=0.025,
         long="-0.0559", short="0.0119", sched="METAL", ladder=(10000, 25000, 50000)),
    dict(_FX, name="UK100_GBP", type="CFD", display="UK 100", pip=0, dp=1, tup=1, min="0.1", max_units="1000",
         margin="0.05", min_tsl="5.0", max_tsl="10000.0", min_gsl="10.0", triple=4, start=8200.0, vol=0.009,
         spread=1.0, long="-0.0657", short="0.0157", sched="UK100", ladder=(50, 100, 250)),
    dict(_FX, name="SPX500_USD", type="CFD", display="US SPX 500", pip=0, dp=1, tup=1, min="0.1", max_units="1000",
         margin="0.05", min_tsl="5.0", max_tsl="10000.0", min_gsl="10.0", triple=4, start=5900.0, vol=0.01,
         spread=0.5, long="-0.0612", short="0.0112", sched="US_INDEX", ladder=(50, 100, 250)),
    dict(_FX, name="WTICO_USD", type="CFD", display="West Texas Oil", pip=-2, dp=3, max_units="100000", margin="0.10",
         min_tsl="0.050", max_tsl="100.000", min_gsl="0.100", triple=4, start=70.0, vol=0.018, spread=0.03,
         long="-0.0412", short="0.0021", sched="WTI", ladder=(2000, 5000, 10000)),
    dict(_FX, name="UK10YB_GBP", type="CFD", display="UK 10Y Gilt", pip=-2, dp=3, max_units="100000", margin="0.20",
         min_tsl="0.050", max_tsl="100.000", min_gsl="0.100", triple=4, start=95.0, vol=0.004, spread=0.03,
         long="-0.0388", short="-0.0012", sched="GILT", ladder=(2000, 5000, 10000)),
]

_SUN_THU = (6, 0, 1, 2, 3)            # date.weekday(): Monday 0 ... Sunday 6
_MON_FRI = (0, 1, 2, 3, 4)
# (zone, start weekdays, start h:m:s, days later the window ends, end h:m:s)
SCHEDULES = {
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/ forex "Sun-Fri: 17:05 - 16:59" New York with
    # "a six minute break between 16:59 - 17:05". The seconds are measured on a live account (the last tradeable
    # price before 16:59:05, tradeable:false from 16:59:05, tradeable again at 17:04:55 New York).
    "FX": ("America/New_York", _SUN_THU, (17, 4, 55), 1, (16, 59, 5)),
    "FX_NZD": ("America/New_York", _SUN_THU, (17, 4, 55), 1, (16, 59, 5)),
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, TRY pairs "Mon-Fri: 08:00 - 17:00" Central Europe time; measured the same on a live
    # account, as 07:00-16:00 London. CET and London change their clocks at the same instant.
    "TRY": ("Europe/London", _MON_FRI, (7, 0, 0), 0, (16, 0, 0)),
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, XAU and XAG "Sun-Fri: 18:05 - 16:59" New York
    "METAL": ("America/New_York", _SUN_THU, (18, 5, 0), 1, (16, 59, 0)),
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, UK 100 (London) Mon-Fri 01:01 - 20:59
    "UK100": ("Europe/London", _MON_FRI, (1, 1, 0), 0, (20, 59, 0)),
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, US SPX 500 (Chicago) Sun-Fri 17:01 - 15:59
    "US_INDEX": ("America/Chicago", _SUN_THU, (17, 1, 0), 1, (15, 59, 0)),
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, West Texas Oil (New York) Sun-Fri 18:01 - 16:59
    "WTI": ("America/New_York", _SUN_THU, (18, 1, 0), 1, (16, 59, 0)),
    # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, UK 10Y Gilt (London) Mon-Fri 08:01 - 17:59
    "GILT": ("Europe/London", _MON_FRI, (8, 1, 0), 0, (17, 59, 0)),
}
# documented: https://www.oanda.com/uk-en/trading/hours-of-operation/, "There is an additional 6-minute break for NZD pairs starting at 06:59 Auckland time."
NZD_BREAK = ("Pacific/Auckland", (6, 59, 0), (7, 5, 0))
# Not modelled (stand-in's own): the holiday hours OANDA publishes on a separate page, and Brent's extra breaks.


class Schedule:
    """OANDA's documented weekly hours of one instrument as UTC windows [start, end) in ms, built a year at a time."""

    def __init__(self, key):
        self.key = key
        zname, self.days, self.open, self.span, self.close = SCHEDULES[key]
        self.zone = _ZONES[zname]
        self._years, self._lock = {}, threading.Lock()

    def _year(self, y):
        got = self._years.get(y)
        if got is None:
            z, out = self.zone, []
            d, last = date(y, 1, 1) - timedelta(3), date(y, 12, 31)
            while d <= last:
                if d.weekday() in self.days:
                    s = z.local_to_utc(d, *self.open) * 1000
                    e = z.local_to_utc(d + timedelta(self.span), *self.close) * 1000
                    if time.gmtime(s // 1000).tm_year == y:
                        out.extend(self._cut(s, e) if self.key == "FX_NZD" else [(s, e)])
                d += timedelta(1)
            with self._lock:
                got = self._years[y] = out
        return got

    @staticmethod
    def _cut(s, e):
        """A forex window less the NZD pairs' daily Auckland break."""
        zname, b0, b1 = NZD_BREAK
        az, out, cur = _ZONES[zname], [], s
        d = az.local_date(s // 1000) - timedelta(1)
        while True:
            bs, be = az.local_to_utc(d, *b0) * 1000, az.local_to_utc(d, *b1) * 1000
            if bs >= e:
                break
            if be > cur and bs < e:
                if bs > cur:
                    out.append((cur, bs))
                cur = max(cur, be)
            d += timedelta(1)
        if cur < e:
            out.append((cur, e))
        return out

    def windows(self, a, b):
        """The windows that overlap [a, b), whole (with their true starts)."""
        out = []
        for y in range(time.gmtime(max(0, a) // 1000).tm_year - 1, time.gmtime(max(a, b - 1, 0) // 1000).tm_year + 1):
            wins = self._year(y)
            i = bisect.bisect_right(wins, (a, BIG))
            i = max(0, i - 1)
            while i < len(wins) and wins[i][0] < b:
                if wins[i][1] > a:
                    out.append(wins[i])
                i += 1
        return out


# ---------------------------------------------------------------- the price path (stand-in's own)

def _interp(js, vs, j):
    i = bisect.bisect_right(js, j) - 1
    j0 = js[i]
    if j0 == j or i + 1 >= len(js):
        return vs[i]
    return vs[i] + (vs[i + 1] - vs[i]) * (j - j0) / (js[i + 1] - j0)


class Path:
    """One instrument's mid price, defined at every instant from 2005 on (stand-in's own model). A day's open, high, low
    and close come from a mean-reverting daily walk; each UTC hour of the day gets its own open, close, high and low inside
    the day's (a bridge between the day's open and close); inside an hour the price moves in straight lines between a
    few knots that sit on the hour's tick grid (every 250, 500 or 1000 ms) and carry the hour's high and low. So the
    extremes of any span are at knots or ends, every candle is exact at every granularity, and coarser candles are the
    aggregate of finer ones. Whether a tick is seen (market hours, a closed market) is decided outside the path."""

    KAPPA = 0.003

    def __init__(self, seed, name, start, vol):
        self.seed, self.name, self.start, self.vol = seed, name, float(start), float(vol)
        self._rng = random.Random(seed_of(seed, name, "daily"))
        self._c, self._h, self._l = [], [], []
        self._hours, self._knots = collections.OrderedDict(), collections.OrderedDict()
        self._lock = threading.RLock()

    def day(self, k):
        """(open, high, low, close) of day k (days since 1970)."""
        i = k - DAY0
        if i < 0:
            return self.start, self.start, self.start, self.start
        with self._lock:
            if i >= len(self._c):
                g, log0 = self._rng.gauss, math.log(self.start)
                while len(self._c) <= i:
                    prev = self._c[-1] if self._c else self.start
                    lp = math.log(prev)
                    c = math.exp(lp + self.KAPPA * (log0 - lp) + self.vol * g(0.0, 1.0))
                    self._h.append(max(prev, c) * math.exp(abs(g(0.0, 1.0)) * self.vol * 0.6))
                    self._l.append(min(prev, c) * math.exp(-abs(g(0.0, 1.0)) * self.vol * 0.6))
                    self._c.append(c)
            return (self._c[i - 1] if i else self.start), self._h[i], self._l[i], self._c[i]

    def hours(self, k):
        """The 24 hours of day k: (open, close, high, low, tick step ms) each; open of hour i+1 = close of hour i."""
        got = self._hours.get(k)
        if got is not None:
            return got
        o, h, l, c = self.day(k)
        r = random.Random(seed_of(self.seed, self.name, "h", k))
        g = r.gauss
        w = [0.0]
        for _ in range(24):
            w.append(w[-1] + g(0.0, 1.0))
        span = h - l
        b = [o]
        for i in range(1, 24):
            v = o + (c - o) * i / 24.0 + (w[i] - w[24] * i / 24.0) * span / 5.0
            b.append(min(max(v, l), h))
        b.append(c)
        ihi = r.randrange(24)
        ilo = r.randrange(23)
        ilo += ilo >= ihi
        got = []
        for i in range(24):
            top, bot = max(b[i], b[i + 1]), min(b[i], b[i + 1])
            hh = h if i == ihi else top + (h - top) * r.random() ** 3 * 0.5
            ll = l if i == ilo else bot - (bot - l) * r.random() ** 3 * 0.5
            got.append((b[i], b[i + 1], hh, ll, (250, 500, 500, 1000)[r.randrange(4)]))
        with self._lock:
            self._hours[k] = got
            while len(self._hours) > 6000:
                self._hours.popitem(last=False)
        return got

    def knots(self, hidx):
        """Inside hour hidx (hours since 1970): (tick indices, values, step ms, ticks in the hour)."""
        got = self._knots.get(hidx)
        if got is not None:
            return got
        a, b, hh, ll, step = self.hours(hidx // 24)[hidx % 24]
        n = HOUR_MS // step
        r = random.Random(seed_of(self.seed, self.name, "k", hidx))
        ja = r.randrange(1, n - 1)
        jb = r.randrange(1, n - 2)
        jb += jb >= ja
        pts = {0: a, n - 1: b}
        pts[ja], pts[jb] = (hh, ll) if r.random() < 0.5 else (ll, hh)
        for _ in range(6):
            j = r.randrange(1, n - 1)
            if j not in pts:
                pts[j] = ll + (hh - ll) * r.random()
        js = sorted(pts)
        got = (js, [pts[j] for j in js], step, n)
        with self._lock:
            self._knots[hidx] = got
            while len(self._knots) > 20000:
                self._knots.popitem(last=False)
        return got

    def step(self, t):
        hidx = t // HOUR_MS
        return self.hours(hidx // 24)[hidx % 24][4]

    def on_grid(self, t):
        return (t % HOUR_MS) % self.step(t) == 0

    def grid_floor(self, t):
        st = self.step(t)
        return t - (t % HOUR_MS) % st

    def value_at(self, t):
        """The value of the last grid tick at or before t."""
        if t < HISTORY_START_MS:
            return self.start
        hidx = t // HOUR_MS
        js, vs, st, n = self.knots(hidx)
        return _interp(js, vs, (t - hidx * HOUR_MS) // st)

    def ohlcv(self, a, b):
        """Over the grid ticks in [a, b): (open, high, low, close, ticks, first tick ms, last tick ms) or None."""
        a = max(a, HISTORY_START_MS)
        acc, t = None, a
        while t < b:
            hidx = t // HOUR_MS
            h0 = hidx * HOUR_MS
            e = min(b, h0 + HOUR_MS)
            bo, bc, hh, ll, st = self.hours(hidx // 24)[hidx % 24]
            n = HOUR_MS // st
            j0, j1 = -((h0 - t) // st), (e - h0 - 1) // st
            if j0 <= j1:
                if j0 == 0 and j1 == n - 1:
                    part = (bo, hh, ll, bc, n, h0, h0 + (n - 1) * st)
                else:
                    js, vs = self.knots(hidx)[:2]
                    vo, vc = _interp(js, vs, j0), _interp(js, vs, j1)
                    inner = vs[bisect.bisect_right(js, j0):bisect.bisect_left(js, j1)]
                    part = (vo, max([vo, vc] + inner), min([vo, vc] + inner), vc, j1 - j0 + 1, h0 + j0 * st, h0 + j1 * st)
                if acc is None:
                    acc = list(part)
                else:
                    acc[1], acc[2] = max(acc[1], part[1]), min(acc[2], part[2])
                    acc[3], acc[4], acc[6] = part[3], acc[4] + part[4], part[6]
            t = e
        return acc

    def ticks(self, a, b):
        """Every grid tick in [a, b) as (ms, value); for scans over short spans."""
        t = a
        while t < b:
            hidx = t // HOUR_MS
            h0 = hidx * HOUR_MS
            e = min(b, h0 + HOUR_MS)
            js, vs, st, n = self.knots(hidx)
            j = -((h0 - t) // st)
            while h0 + j * st < e:
                yield h0 + j * st, _interp(js, vs, j)
                j += 1
            t = e


class Override:
    """A price a control put over the walk from t0 on: hold a value, follow the walk times a factor, or move in a straight
    line to a value by t1. spread None keeps the instrument's own."""
    __slots__ = ("t0", "kind", "p1", "p2", "t1", "spread")

    def __init__(self, t0, kind, p1, p2=None, t1=None, spread=None):
        self.t0, self.kind, self.p1, self.p2, self.t1, self.spread = t0, kind, p1, p2, t1, spread


class Instrument:
    def __init__(self, spec, seed):
        self.spec = spec
        self.name = spec["name"]
        self.type, self.dp, self.tup = spec["type"], spec["dp"], spec["tup"]
        self.base, _, self.quote_ccy = self.name.partition("_")
        self.tick = Decimal(1).scaleb(-self.dp)
        self.min_size, self.max_units = Decimal(spec["min"]), Decimal(spec["max_units"])
        self.margin = Decimal(spec["margin"])
        self.min_tsl, self.max_tsl = Decimal(spec["min_tsl"]), Decimal(spec["max_tsl"])
        self.spread0 = float(spec["spread"])
        self.path = Path(seed, self.name, spec["start"], spec["vol"])
        self.schedule = Schedule(spec["sched"])
        self.overrides, self.modes, self.cap, self.cap_count = [], [], None, None

    def row(self):
        """The Instrument object, fields in the documented order (https://developer.oanda.com/rest-live-v20/primitives-df/)."""
        s = self.spec
        days = [{"dayOfWeek": DAY_NAMES[i], "daysCharged": 0 if i >= 5 else (3 if i == s["triple"] else 1)}
                for i in range(7)]
        return {"name": self.name, "type": self.type, "displayName": s["display"], "pipLocation": s["pip"],
                "displayPrecision": self.dp, "tradeUnitsPrecision": self.tup, "minimumTradeSize": s["min"],
                "maximumTrailingStopDistance": s["max_tsl"], "minimumGuaranteedStopLossDistance": s["min_gsl"],
                "minimumTrailingStopDistance": s["min_tsl"], "maximumPositionSize": "0",
                "maximumOrderUnits": s["max_units"], "marginRate": s["margin"],
                "commission": {"commission": "0", "unitsTraded": "1", "minimumCommission": "0"},
                # documented: https://developer.oanda.com/rest-live-v20/primitives-df/; the premium and level restriction are only provided when the mode is not
                # DISABLED. DISABLED everywhere is stand-in's own (no guaranteed stops on these accounts).
                "guaranteedStopLossOrderMode": "DISABLED",
                "financing": {"longRate": s["long"], "shortRate": s["short"], "financingDaysOfWeek": days},
                "tags": [{"type": "ASSET_CLASS", "name": self.type}]}


class Quote:
    """One price of an instrument: the tick's time, its mid and spread, and whether the instrument is tradeable."""
    __slots__ = ("inst", "ms", "mid", "spread", "tradeable", "bid", "ask")

    def __init__(self, inst, ms, mid, spread, tradeable):
        self.inst, self.ms, self.mid, self.spread, self.tradeable = inst, ms, mid, spread, tradeable
        self.bid, self.ask = side_prices(inst, mid, spread)


def side_prices(inst, mid, spread):
    """(bid, ask) on the instrument's grid from a mid and a spread; ask is at least one tick over bid. Monotone in mid."""
    bid = rnd(mid - spread / 2.0, inst.dp)
    ask = rnd(mid + spread / 2.0, inst.dp)
    if ask <= bid:
        ask = bid + inst.tick
    return bid, ask


def mid_of(inst, bid, ask):
    return ((bid + ask) / 2).quantize(inst.tick, rounding=ROUND_HALF_UP)


BIG = 1 << 62


class Market:
    """The clock, the instruments, when each one is open, and every price at any millisecond."""

    def __init__(self, clock, seed=7, mode="open"):
        self.clock, self.seed, self.mode0 = clock, seed, mode
        self.lock = threading.RLock()
        self.instruments = {}
        for spec in CATALOG:
            self.instruments[spec["name"]] = Instrument(spec, seed)
        self.start_ms = clock.now_ms()
        self.reset()

    def reset(self):
        with self.lock:
            for inst in self.instruments.values():
                inst.overrides, inst.cap, inst.cap_count = [], None, None
                # History before the stand-in started follows OANDA's hours; from the start on, the mode given (stand-in's
                # own: "open" by default, so tests run at weekends; "auto" follows the hours).
                inst.modes = [(self.start_ms, self.mode0)]

    # -- when an instrument quotes
    def set_mode(self, mode, names=None, at=None):
        if mode not in ("open", "closed", "auto"):
            raise ValueError("state must be open, closed or auto")
        at = self.clock.now_ms() if at is None else at
        with self.lock:
            for name in (names or list(self.instruments)):
                inst = self.find(name)
                inst.modes = [m for m in inst.modes if m[0] < at] + [(at, mode)]
        return at

    def windows(self, inst, a, b):
        """The visible windows [s, e) that overlap [a, b), merged, with their true starts."""
        modes = inst.modes
        pieces, prev_t, prev_m = [], -BIG, "auto"
        for t, m in modes:
            pieces.append((prev_t, t, prev_m))
            prev_t, prev_m = t, m
        pieces.append((prev_t, BIG, prev_m))
        raw = []
        for ps, pe, m in pieces:
            if pe <= a - 1 or ps >= b or ps >= pe:
                continue
            if m == "open":
                raw.append((ps, pe))
            elif m == "auto":
                for ws, we in inst.schedule.windows(max(ps, a - 1), min(pe, b)):
                    s, e = max(ws, ps), min(we, pe)
                    if s < e:
                        raw.append((s, e))
        raw.sort()
        out = []
        for s, e in raw:
            if out and s <= out[-1][1]:
                if e > out[-1][1]:
                    out[-1] = (out[-1][0], e)
            else:
                out.append((s, e))
        return [(s, e) for s, e in out if e > a and s < b]

    def last_window_before(self, inst, t):
        span = 14 * DAY_MS
        lo = t
        while lo > HISTORY_START_MS - 30 * DAY_MS:
            lo -= span
            got = [w for w in self.windows(inst, lo, t) if w[1] <= t]
            if got:
                return got[-1]
            span *= 2
        return None

    def next_window_after(self, inst, t, limit):
        span = 14 * DAY_MS
        hi = t
        while hi < limit:
            hi = min(limit, hi + span)
            got = [w for w in self.windows(inst, t, hi) if w[0] >= t]
            if got:
                return got[0]
            span *= 2
        return None

    # -- prices
    def find(self, name):
        inst = self.instruments.get(name)
        if inst is None:
            raise ValueError("unknown instrument %r" % (name,))
        return inst

    @staticmethod
    def _rec_at(inst, t):
        ovs = inst.overrides
        if not ovs or ovs[0].t0 > t:
            return None
        i = bisect.bisect_right([o.t0 for o in ovs], t) - 1
        return ovs[i]

    @staticmethod
    def _value(inst, t, rec):
        if rec is None:
            return inst.path.value_at(t)
        if rec.kind == "hold":
            return rec.p1
        if rec.kind == "scale":
            return rec.p1 * inst.path.value_at(t)
        frac = 1.0 if rec.t1 <= rec.t0 else min(1.0, max(0.0, (t - rec.t0) / float(rec.t1 - rec.t0)))
        return rec.p1 + (rec.p2 - rec.p1) * frac

    def _spread(self, inst, rec):
        return rec.spread if rec is not None and rec.spread is not None else inst.spread0

    def quote(self, inst, t=None):
        """The price in effect at t: the last tick seen at or before t. When the instrument is not quoting, its last price
        with tradeable false, stamped with the moment it stopped (measured on a live account: one PRICE with
        tradeable:false at 16:59:05)."""
        if isinstance(inst, str):
            inst = self.find(inst)
        t = self.clock.now_ms() if t is None else t
        with self.lock:
            wins = self.windows(inst, t, t + 1)
            if wins and wins[0][0] <= t < wins[0][1]:
                ws = wins[0][0]
                tick = inst.path.grid_floor(t)
                if tick < ws:
                    tick = ws
                rec = self._rec_at(inst, t)
                if rec is not None and tick < rec.t0 <= t:
                    tick = rec.t0
                rec = self._rec_at(inst, tick)
                if rec is not None and rec.kind == "hold":
                    # a held price was created once: its time is when it was set (or when the market opened on it), so a
                    # snapshot repeats that original time (stand-in's own)
                    tick = max(rec.t0, ws)
                return Quote(inst, tick, self._value(inst, tick, rec), self._spread(inst, rec), True)
            prev = self.last_window_before(inst, t)
            if prev is None:
                return Quote(inst, HISTORY_START_MS, inst.path.start, inst.spread0, False)
            last = self.quote(inst, prev[1] - 1)
            return Quote(inst, prev[1], last.mid, last.spread, False)

    def mid(self, name):
        q = self.quote(name)
        return mid_of(q.inst, q.bid, q.ask)

    def control_price(self, name, bid=None, ask=None, mid=None, spread=None, walk=None, jump_pct=None, ramp_to=None,
                      seconds=None, at=None):
        """Put a price over the walk from now on (or from `at`). bid and ask (or mid, with spread) hold it there; walk
        resumes the walk from the current price; jump_pct moves the current price; ramp_to moves it in a straight line
        over `seconds`."""
        inst = self.find(name)
        with self.lock:
            now = self.clock.now_ms() if at is None else at
            cur = self._rec_at(inst, now)
            q = self.quote(inst, now)
            value = q.mid
            sp = cur.spread if cur is not None else None
            kind = "hold" if cur is not None and cur.kind in ("hold", "ramp") else "walk"
            if bid is not None or ask is not None:
                if bid is None or ask is None:
                    raise ValueError("give both bid and ask")
                b, a = float(bid), float(ask)
                if not 0 < b < a:
                    raise ValueError("need 0 < bid < ask")
                value, sp, kind = (a + b) / 2.0, a - b, "hold"
            if mid is not None:
                value, kind = float(mid), "hold"
                if not value > 0:
                    raise ValueError("mid must be > 0")
            if spread is not None:
                sp = float(spread)
                if not sp > 0:
                    raise ValueError("spread must be > 0")
            if jump_pct is not None:
                value *= 1.0 + float(jump_pct) / 100.0
            if walk is not None:
                kind = "walk" if walk else "hold"
            del inst.overrides[bisect.bisect_right([o.t0 for o in inst.overrides], now):]
            if ramp_to is not None:
                span = int(float(seconds or 0) * 1000)
                if span <= 0 or not float(ramp_to) > 0:
                    raise ValueError("a ramp needs ramp_to > 0 and seconds > 0")
                inst.overrides.append(Override(now, "ramp", value, float(ramp_to), now + span, sp))
                inst.overrides.append(Override(now + span, "hold", float(ramp_to), spread=sp))
            elif kind == "walk":
                inst.overrides.append(Override(now, "scale", value / inst.path.value_at(now), spread=sp))
            else:
                inst.overrides.append(Override(now, "hold", value, spread=sp))
            q = self.quote(inst, now)
            return {"instrument": inst.name, "bid": pstr(q.bid, inst.dp), "ask": pstr(q.ask, inst.dp),
                    "time": rfc3339(q.ms * MS_NS), "mode": "ramp" if ramp_to is not None else kind}

    # -- liquidity (documented: https://developer.oanda.com/rest-live-v20/pricing-common-df/ PriceBucket; how an order
    #    meets it: https://legal.oanda.com/oel/execution_policy_oel/en Annex 1)
    def buckets(self, inst):
        """[(liquidity, ticks worse)] of one side, after any cap a control set."""
        out, left = [], inst.cap
        for i, size in enumerate(inst.spec["ladder"]):
            size = Decimal(size)
            if left is not None:
                if left <= 0:
                    break
                size = min(size, left)
                left -= size
            out.append((size, i))
        return out

    def vwap(self, inst, q, units, buy, unlimited=False):
        """(average price, units the book can take) for |units| against the quote's ladder; `unlimited` takes the rest
        at the deepest bucket's price (a margin close-out)."""
        want, got, value = abs(units), D0, D0
        top = q.ask if buy else q.bid
        px = top
        for size, off in self.buckets(inst):
            take = min(size, want - got)
            if take <= 0:
                break
            px = top + inst.tick * off if buy else top - inst.tick * off
            value += px * take
            got += take
        if unlimited and got < want:
            value += px * (want - got)
            got = want
        if got <= 0:
            return top, D0
        return value / got, got

    def use_cap(self, inst):
        """A fill attempt consumes one use of a counted cap."""
        if inst.cap is not None and inst.cap_count is not None:
            inst.cap_count -= 1
            if inst.cap_count <= 0:
                inst.cap, inst.cap_count = None, None

    # -- conversion to the account's currency (documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf, "The current mid-price for the
    #    Home/Instrument currency pair"; pricing-df HomeConversions {accountGain, accountLoss, positionValue})
    def rate(self, frm, to):
        if frm == to:
            return Decimal(1)
        if frm + "_" + to in self.instruments:
            return self.mid(frm + "_" + to)
        if to + "_" + frm in self.instruments:
            return Decimal(1) / self.mid(to + "_" + frm)
        if "USD" not in (frm, to):
            return self.rate(frm, "USD") * self.rate("USD", to)
        raise ValueError("no conversion from %s to %s" % (frm, to))

    # A realised amount in another currency is converted "with a fee equal to 1.0% of the relevant mid-market exchange
    # rate" (help.oanda.com/uk/en/faqs/home-currency-conversion.htm, read by the 2026-11-11 research). Applying it as
    # 0.99 for a gain and 1.01 for a loss is stand-in's own; positionValue is the mid rate.
    CONVERSION_FEE = Decimal("0.01")

    def factors(self, ccy, home):
        """(accountGain, accountLoss, positionValue) for one currency into the account's."""
        r = self.rate(ccy, home)
        if ccy == home:
            return r, r, r
        return r * (1 - self.CONVERSION_FEE), r * (1 + self.CONVERSION_FEE), r

    # -- candles
    def _pieces(self, inst, a, b, wins=None):
        """[(s, e, a tick at s, override)] of the visible parts of [a, b), cut where a control changed the price."""
        out = []
        if wins is None:
            wins = self.windows(inst, a, b)
        else:
            i = max(0, bisect.bisect_right(wins, (a, BIG)) - 1)
            wins = [w for w in wins[i:i + 64] if w[1] > a and w[0] < b]
            if len(wins) == 64:
                wins = self.windows(inst, a, b)
        ovs = inst.overrides
        t0s = [o.t0 for o in ovs]
        for ws, we in wins:
            s, e = max(ws, a), min(we, b)
            if s >= e:
                continue
            i = bisect.bisect_right(t0s, s) - 1
            rec = ovs[i] if i >= 0 else None
            tick = s == ws or (rec is not None and rec.t0 == s)
            j = i + 1
            while j < len(ovs) and ovs[j].t0 < e:
                out.append((s, ovs[j].t0, tick, rec))
                s, rec, tick = ovs[j].t0, ovs[j], True
                j += 1
            out.append((s, e, tick, rec))
        return out

    def _piece(self, inst, s, e, tick, rec):
        """(open, high, low, close, ticks, spread) of the mid over one piece, or None when no tick falls in it."""
        path = inst.path
        g = path.ohlcv(s, e)
        extra = tick and not path.on_grid(s)
        if g is None and not extra:
            return None
        n = (g[4] if g else 0) + (1 if extra else 0)
        sp = self._spread(inst, rec)
        if rec is not None and rec.kind == "hold":
            v = rec.p1
            return v, v, v, v, n, sp
        if rec is not None and rec.kind == "ramp":
            first = s if extra or g is None else g[5]
            last = g[6] if g else s
            vo, vc = self._value(inst, first, rec), self._value(inst, last, rec)
            return vo, max(vo, vc), min(vo, vc), vc, n, sp
        k = rec.p1 if rec is not None else 1.0
        if g is None:
            x = k * path.value_at(s)
            return x, x, x, x, 1, sp
        o, h, l, c = g[0] * k, g[1] * k, g[2] * k, g[3] * k
        if extra:
            x = k * path.value_at(s)
            o, h, l = x, max(h, x), min(l, x)
        return o, h, l, c, n, sp

    def candle(self, inst, a, b, comps, offsets, wins=None):
        """{component: [o, h, l, c]} and the tick count over [a, b), or None when nothing quoted in it."""
        acc, vol = None, 0
        for s, e, tick, rec in self._pieces(inst, a, b, wins):
            m = self._piece(inst, s, e, tick, rec)
            if m is None:
                continue
            o, h, l, c, n, sp = m
            vol += n
            vals = {}
            for comp in comps:
                four = []
                for x in (o, h, l, c):
                    bid, ask = side_prices(inst, x, sp)
                    four.append(bid - offsets["B"] if comp == "B" else ask + offsets["A"] if comp == "A"
                                else mid_of(inst, bid, ask))
                vals[comp] = four
            if acc is None:
                acc = vals
            else:
                for comp in comps:
                    cur, new = acc[comp], vals[comp]
                    acc[comp] = [cur[0], max(cur[1], new[1]), min(cur[2], new[2]), new[3]]
        return (acc, vol) if acc is not None else None


# ---------------------------------------------------------------- candle boundaries

class Align:
    """Where candles start. documented: https://developer.oanda.com/rest-live-v20/instrument-df/ (S5..M1 minute alignment,
    M2..H1 hour alignment, H2..D day alignment, W the start of the week, M the first day of the month) and
    https://developer.oanda.com/rest-live-v20/pricing-ep/ (dailyAlignment, default 17; alignmentTimezone, default
    America/New_York, "the returned times will still be represented in UTC"; weeklyAlignment, default Friday). Day-aligned
    candles count from the day's start in that zone; minute and hour alignment count in UTC (stand-in's own reading)."""

    def __init__(self, zone_=NEW_YORK, hour=17, weekday=4):
        self.z, self.hour, self.wday = zone_, hour, weekday

    def day_start(self, t):
        """(start ms, local date) of the trading day containing t."""
        s = t // 1000
        d = self.z.local_date(s)
        start = self.z.local_to_utc(d, self.hour)
        if start > s:
            d -= timedelta(1)
            start = self.z.local_to_utc(d, self.hour)
        return start * 1000, d

    def floor(self, gran, t):
        """(start, end) in ms of the candle of `gran` that contains t."""
        p = GRANULARITIES[gran]
        if p is not None and gran not in DAY_ALIGNED:
            s = t - t % p
            return s, s + p
        ds, d = self.day_start(t)
        if gran in DAY_ALIGNED:
            de = self.z.local_to_utc(d + timedelta(1), self.hour) * 1000
            s = ds + (t - ds) // p * p
            return s, min(s + p, de)
        if gran == "W":
            d0 = d - timedelta((d.weekday() - self.wday) % 7)
            return self.z.local_to_utc(d0, self.hour) * 1000, self.z.local_to_utc(d0 + timedelta(7), self.hour) * 1000
        d0 = date(d.year, d.month, 1)
        d1 = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return self.z.local_to_utc(d0, self.hour) * 1000, self.z.local_to_utc(d1, self.hour) * 1000


# ---------------------------------------------------------------- the broker: accounts, orders, trades, transactions

class Reject(Exception):
    """A request refused with a documented TransactionRejectReason: a *_REJECT transaction and a 400 (or 404)."""

    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason, self.status = reason, status


class HTTPError(Exception):
    """A refusal with no transaction: {"errorMessage"} (documented shape: troubleshooting-errors)."""

    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status, self.message, self.body = status, message, body


# documented: https://developer.oanda.com/rest-live-v20/order-df/ OrderType and the transaction type each creates
# (transaction-df); a FIXED_PRICE order is not one a client creates.
TX_TYPE = {"MARKET": "MARKET_ORDER", "LIMIT": "LIMIT_ORDER", "STOP": "STOP_ORDER",
           "MARKET_IF_TOUCHED": "MARKET_IF_TOUCHED_ORDER", "TAKE_PROFIT": "TAKE_PROFIT_ORDER",
           "STOP_LOSS": "STOP_LOSS_ORDER", "TRAILING_STOP_LOSS": "TRAILING_STOP_LOSS_ORDER",
           "GUARANTEED_STOP_LOSS": "GUARANTEED_STOP_LOSS_ORDER"}
ENTRY = ("LIMIT", "STOP", "MARKET_IF_TOUCHED")
DEPENDENT = ("TAKE_PROFIT", "STOP_LOSS", "TRAILING_STOP_LOSS", "GUARANTEED_STOP_LOSS")
# documented: https://developer.oanda.com/rest-live-v20/order-df/: MARKET "FOK" by default (FOK or IOC; research), MARKET_IF_TOUCHED and the trade-dependent orders
# "GTC, GFD, or GTD only", LIMIT and STOP GTC by default. LIMIT and STOP taking FOK and IOC is stand-in's own.
TIF_ALLOWED = {"MARKET": ("FOK", "IOC"), "LIMIT": ("GTC", "GTD", "GFD", "FOK", "IOC"),
               "STOP": ("GTC", "GTD", "GFD", "FOK", "IOC"), "MARKET_IF_TOUCHED": ("GTC", "GFD", "GTD"),
               "TAKE_PROFIT": ("GTC", "GFD", "GTD"), "STOP_LOSS": ("GTC", "GFD", "GTD"),
               "TRAILING_STOP_LOSS": ("GTC", "GFD", "GTD"), "GUARANTEED_STOP_LOSS": ("GTC", "GFD", "GTD")}
POSITION_FILLS = ("OPEN_ONLY", "REDUCE_FIRST", "REDUCE_ONLY", "DEFAULT")            # documented: https://developer.oanda.com/rest-live-v20/order-df/
TRIGGERS = ("DEFAULT", "INVERSE", "BID", "ASK", "MID")                             # documented: https://developer.oanda.com/rest-live-v20/order-df/
FILL_REASON = {"MARKET": "MARKET_ORDER", "LIMIT": "LIMIT_ORDER", "STOP": "STOP_ORDER",       # documented: https://developer.oanda.com/rest-live-v20/transaction-df/ OrderFillReason
               "MARKET_IF_TOUCHED": "MARKET_IF_TOUCHED_ORDER", "TAKE_PROFIT": "TAKE_PROFIT_ORDER",
               "STOP_LOSS": "STOP_LOSS_ORDER", "TRAILING_STOP_LOSS": "TRAILING_STOP_LOSS_ORDER"}
CLIENT_TEXT_MAX = 128          # stand-in's own: the documentation states no length for ClientID, ClientTag, ClientComment
PENDING_MAX = 500              # stand-in's own: the documentation names PENDING_ORDERS_ALLOWED_EXCEEDED, not the number
ACCOUNT_ID = re.compile(r"\d{3}-\d{3}-\d+-\d{3}")       # documented: https://developer.oanda.com/rest-live-v20/account-df/ "{siteID}-{divisionID}-{userID}-{accountNumber}"
USER_ID = 7240139              # stand-in's own
CREATED_NS = 1767605400 * NS   # 2026-01-05T09:30:00Z, when the stand-in's accounts were opened (stand-in's own)


def next_ny_five(ms):
    """The first 17:00 New York at or after ms: when GFD orders end (documented: https://developer.oanda.com/rest-live-v20/order-df/ TimeInForce GFD "cancelled at 5pm
    New York time") and financing is booked (documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf, EOD Time "5pm ET")."""
    s = ms // 1000
    d = NEW_YORK.local_date(s)
    t = NEW_YORK.local_to_utc(d, 17)
    if t * 1000 < ms:
        t = NEW_YORK.local_to_utc(d + timedelta(1), 17)
    return t * 1000


class Account:
    def __init__(self, aid, currency, alias=None, hedging=False, mt4=None, fifo=False, generation=0, division=4,
                 balance="100000"):
        self.id, self.currency, self.alias, self.hedging, self.mt4 = aid, currency, alias, hedging, mt4
        self.fifo, self.generation, self.division = fifo, generation, division
        self.balance, self.start_balance = Decimal(balance), Decimal(balance)
        self.pl = self.resettable = self.financing = self.commission = self.dividend = self.gef = D0
        self.margin_rate = None
        self.last_id, self.txs, self.orders, self.trades, self.pos = 0, [], {}, {}, {}
        self.margin_call, self.margin_call_ns, self.mc_extensions = False, None, 0
        self.resettable_ns = CREATED_NS


class Broker:
    """Every account the stand-in holds, its orders, trades, positions and transaction log, and the engine that fills."""

    def __init__(self, market, clock, division="uk", on_tx=None):
        self.market, self.clock, self.division = market, clock, division
        self.lock = threading.RLock()
        self.on_tx = on_tx
        self.refusals, self.violations = [], []
        self.options = {"client_id_lookup": "pending", "closeout_extensions": "documented"}
        self.reset()

    # -- the accounts a token reaches
    def reset(self):
        with self.lock:
            self.accounts = collections.OrderedDict()
            if self.division == "us":
                # documented: https://help.oanda.com/us/en/faqs/account-types-and-leverage-us.htm hedging is not
                # available and FIFO applies to US accounts
                self._open(Account("101-001-%d-001" % USER_ID, "USD", "Primary", fifo=True, division=1))
            else:
                # stand-in's own: three sub-accounts of one UK login (documented: https://developer.oanda.com/rest-live-v20/authentication/ one token reaches "all of your
                # sub-accounts", authentication; netting by default, hedging when chosen, v20 MT4 sub-accounts:
                # OEL-sub-accounts PDF)
                self._open(Account("101-004-%d-001" % USER_ID, "GBP", "Primary"))
                self._open(Account("101-004-%d-002" % USER_ID, "USD", "Hedging", hedging=True))
                self._open(Account("101-004-%d-003" % USER_ID, "GBP", "MT4", mt4=USER_ID))
            self.refusals, self.violations = [], []
            self.last_scan = self.clock.now_ms()
            self.next_finance = next_ny_five(self.last_scan + 1)

    def _open(self, acct):
        ctx = self.ctx(ns=CREATED_NS)
        self.accounts[acct.id] = acct
        site, division, user, number = acct.id.split("-")
        self._tx(acct, "CREATE", ctx, divisionID=int(division), siteID=int(site), accountUserID=int(user),
                 accountNumber=int(number), homeCurrency=acct.currency)
        self._tx(acct, "TRANSFER_FUNDS", self.ctx(ns=CREATED_NS + 1), amount=money(acct.balance),
                 fundingReason="CLIENT_FUNDING", accountBalance=money(acct.balance))
        return acct

    def add_account(self, currency="GBP", hedging=False, alias=None, generation=1):
        with self.lock:
            n = len(self.accounts) + 1
            site, division = ("101", "001") if self.division == "us" else ("101", "004")
            acct = Account("%s-%s-%d-%03d" % (site, division, USER_ID, n), currency, alias, hedging=bool(hedging),
                           fifo=self.division == "us", generation=generation, division=int(division))
            return self._open(acct).id

    # -- transactions
    def ctx(self, rid=None, ns=None):
        return {"ns": self.clock.stamp() if ns is None else ns, "rid": rid, "batch": None, "ids": []}

    def _tx(self, acct, ttype, ctx, **fields):
        acct.last_id += 1
        tid = str(acct.last_id)
        if ctx["batch"] is None:
            ctx["batch"] = tid
        # documented: https://developer.oanda.com/rest-live-v20/transaction-df/ base fields id, time, userID, accountID, batchID, requestID ("only provided for
        # Transactions generated by a Client request"), then the subtype's own with its type first
        tx = {"id": tid, "time": Stamp(ctx["ns"]), "userID": USER_ID, "accountID": acct.id, "batchID": ctx["batch"]}
        if ctx.get("rid"):
            tx["requestID"] = ctx["rid"]
        tx["type"] = ttype
        for k, v in fields.items():
            if v is not None:
                tx[k] = v
        acct.txs.append(tx)
        ctx["ids"].append(tid)
        if self.on_tx is not None:
            self.on_tx(acct, tx)
        return tx

    def answer(self, acct, ctx, **parts):
        """A body with relatedTransactionIDs and lastTransactionID (documented: https://developer.oanda.com/rest-live-v20/order-ep/, trade-ep, position-ep)."""
        out = {k: v for k, v in parts.items() if v is not None}
        out["relatedTransactionIDs"] = list(ctx["ids"])
        out["lastTransactionID"] = str(acct.last_id)
        return out

    def reject_body(self, acct, ctx, key, tx, reason):
        out = {key: tx} if tx is not None else {}
        out["relatedTransactionIDs"] = list(ctx["ids"])
        out["lastTransactionID"] = str(acct.last_id)
        out["errorCode"] = reason
        out["errorMessage"] = reason_text(reason)
        return out

    # -- figures (documented: https://developer.oanda.com/rest-live-v20/account-df/; how margin is counted: OEL-sub-accounts PDF)
    def _rate(self, acct, inst):
        r = inst.margin
        if acct.margin_rate is not None and acct.margin_rate > r:
            r = acct.margin_rate
        return r

    def trade_upl(self, acct, t, q=None, mid=False):
        """Unrealised P/L of one trade in the account's currency: at the sided price for its size (documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf
        , "The UPL for trades is calculated based on the sided price for the notional amount of each
        trade"), or at the mid for margin."""
        inst = self.market.find(t["instrument"])
        q = q or self.market.quote(inst)
        u = t["_units"]
        if mid:
            px = mid_of(inst, q.bid, q.ask)
        else:
            px, _ = self.market.vwap(inst, q, u, u < 0)
        pl = (px - t["_price"]) * u
        gain, loss, _ = self.market.factors(inst.quote_ccy, acct.currency)
        return pl * (gain if pl >= 0 else loss)

    def inst_margin(self, acct, inst, units_list, q=None):
        """Margin of one instrument's open units: rate x units x mid x conversion (documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf
        "Margin_Used = Margin_Rate * Trade_Quantity * Instrument_To_Home_Currency_Conversion_Rate"), and on a hedged
        position the larger leg only ("or for the long leg if the long and short trade quantities exactly offset")."""
        long_u = sum((u for u in units_list if u > 0), D0)
        short_u = -sum((u for u in units_list if u < 0), D0)
        size = max(long_u, short_u)
        if size == 0:
            return D0
        q = q or self.market.quote(inst)
        value = size * mid_of(inst, q.bid, q.ask) * self.market.rate(inst.quote_ccy, acct.currency)
        return value * self._rate(acct, inst)

    def open_trades(self, acct, instrument=None):
        return [t for t in acct.trades.values() if t["state"] == "OPEN" and (instrument is None or t["instrument"] == instrument)]

    def figures(self, acct):
        upl = upl_mid = margin = value = D0
        by_inst = collections.defaultdict(list)
        for t in self.open_trades(acct):
            by_inst[t["instrument"]].append(t)
        for name, trades in by_inst.items():
            inst = self.market.find(name)
            q = self.market.quote(inst)
            for t in trades:
                upl += self.trade_upl(acct, t, q)
                upl_mid += self.trade_upl(acct, t, q, mid=True)
                value += abs(t["_units"]) * mid_of(inst, q.bid, q.ask) * self.market.rate(inst.quote_ccy, acct.currency)
            margin += self.inst_margin(acct, inst, [t["_units"] for t in trades], q)
        nav, nav_mid = acct.balance + upl, acct.balance + upl_mid
        avail = max(D0, nav - margin)
        closeout_used = margin / 2            # documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf "Margin_% = (50% * Margin_Used) / NAV_mid"
        pct = closeout_used / nav_mid if nav_mid > 0 else (Decimal(0) if margin == 0 else Decimal(99))
        call_pct = margin / nav_mid if nav_mid > 0 else (Decimal(0) if margin == 0 else Decimal(99))
        return {"upl": upl, "upl_mid": upl_mid, "nav": nav, "nav_mid": nav_mid, "margin": margin, "avail": avail,
                "value": value, "closeout_used": closeout_used, "closeout_pct": pct, "call_pct": call_pct,
                "withdrawal": max(D0, min(acct.balance, avail)), "trades": sum(len(v) for v in by_inst.values()),
                "positions": len(by_inst)}

    def account_json(self, acct, full):
        """The Account (full) or AccountSummary, fields in the documented order (account-df)."""
        f = self.figures(acct)
        out = {"id": acct.id}
        if acct.alias is not None:
            out["alias"] = acct.alias
        out.update({"currency": acct.currency, "createdByUserID": USER_ID, "createdTime": Stamp(CREATED_NS),
                    "guaranteedStopLossOrderMode": "DISABLED", "resettablePLTime": Stamp(acct.resettable_ns)})
        if acct.margin_rate is not None:
            out["marginRate"] = factor(acct.margin_rate)
        pending = [o for o in acct.orders.values() if o["state"] == "PENDING"]
        out.update({"openTradeCount": f["trades"], "openPositionCount": f["positions"], "pendingOrderCount": len(pending),
                    "hedgingEnabled": acct.hedging, "unrealizedPL": money(f["upl"]), "NAV": money(f["nav"]),
                    "marginUsed": money(f["margin"]), "marginAvailable": money(f["avail"]),
                    "positionValue": money(f["value"]), "marginCloseoutUnrealizedPL": money(f["upl_mid"]),
                    "marginCloseoutNAV": money(f["nav_mid"]), "marginCloseoutMarginUsed": money(f["closeout_used"]),
                    "marginCloseoutPercent": factor(f["closeout_pct"].quantize(Decimal("0.00001"))),
                    "marginCloseoutPositionValue": factor(f["value"].quantize(Decimal("0.00001"))),
                    "withdrawalLimit": money(f["withdrawal"]), "marginCallMarginUsed": money(f["margin"]),
                    "marginCallPercent": factor(f["call_pct"].quantize(Decimal("0.00001"))),
                    "balance": money(acct.balance), "pl": money(acct.pl), "resettablePL": money(acct.resettable),
                    "financing": money(acct.financing), "commission": money(acct.commission),
                    "dividendAdjustment": money(acct.dividend), "guaranteedExecutionFees": money(acct.gef)})
        if acct.margin_call_ns is not None and acct.margin_call:
            out["marginCallEnterTime"] = Stamp(acct.margin_call_ns)
        out["marginCallExtensionCount"] = acct.mc_extensions
        out["lastTransactionID"] = str(acct.last_id)
        if full:
            out["trades"] = [self.trade_json(acct, t, summary=True) for t in sorted(self.open_trades(acct), key=_num_id)]
            out["positions"] = [self.position_json(acct, n) for n in sorted(acct.pos)]
            out["orders"] = [self.order_json(o) for o in sorted(pending, key=_num_id)]
        return out

    # -- orders, trades, positions as the API prints them
    @staticmethod
    def order_json(o):
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def trade_json(self, acct, t, summary=False):
        """Trade or TradeSummary (documented: https://developer.oanda.com/rest-live-v20/trade-df/): the summary names the dependent orders by id."""
        out = {k: v for k, v in t.items() if not k.startswith("_")}
        if t["state"] == "OPEN":
            out["unrealizedPL"] = money(self.trade_upl(acct, t))
            inst = self.market.find(t["instrument"])
            same = [x["_units"] for x in self.open_trades(acct, t["instrument"])]
            leg = self.inst_margin(acct, inst, same)
            longer = sum((u for u in same if u > 0), D0) >= -sum((u for u in same if u < 0), D0)
            mine = (t["_units"] > 0) == longer
            share = abs(t["_units"]) / max(sum((abs(u) for u in same if (u > 0) == longer), D0), Decimal(1))
            out["marginUsed"] = money(leg * share if mine else D0)
        else:
            out["unrealizedPL"], out["marginUsed"] = "0.0000", "0.0000"
        order = ("id", "instrument", "price", "openTime", "state", "initialUnits", "initialMarginRequired", "currentUnits",
                 "realizedPL", "unrealizedPL", "marginUsed", "averageClosePrice", "closingTransactionIDs", "financing",
                 "dividendAdjustment", "closeTime", "clientExtensions")
        res = {k: out[k] for k in order if k in out}
        for key, ref in (("takeProfitOrder", "_tp"), ("stopLossOrder", "_sl"), ("trailingStopLossOrder", "_tsl")):
            oid = t.get(ref)
            if oid is not None and acct.orders[oid]["state"] == "PENDING":
                if summary:
                    res[key + "ID"] = oid
                else:
                    res[key] = self.order_json(acct.orders[oid])
        return res

    def position_json(self, acct, name):
        """Position (documented: https://developer.oanda.com/rest-live-v20/position-df/): both sides, averagePrice and tradeIDs only on a side that holds units
        (stand-in's own reading)."""
        inst = self.market.find(name)
        agg = acct.pos.get(name) or self._agg({}, name)
        q = self.market.quote(inst)
        trades = self.open_trades(acct, name)
        sides, total_upl = {}, D0
        for side, sign in (("long", 1), ("short", -1)):
            mine = [t for t in trades if (t["_units"] > 0) == (sign > 0)]
            units = sum((t["_units"] for t in mine), D0)
            s = {"units": plain(units)}
            if mine:
                s["averagePrice"] = self._px(inst, sum((t["_price"] * abs(t["_units"]) for t in mine), D0) / abs(units))
                s["tradeIDs"] = [t["id"] for t in sorted(mine, key=_num_id)]
            a = agg[side]
            upl = D0
            if units:
                # documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf, "The UPL for the aggregated open position is calculated based on the
                # sided price for the total aggregated notional amount of that position"
                px, _ = self.market.vwap(inst, q, units, units < 0)
                upl = sum(((px - t["_price"]) * t["_units"] for t in mine), D0)
                gain, loss, _ = self.market.factors(inst.quote_ccy, acct.currency)
                upl = upl * (gain if upl >= 0 else loss)
            total_upl += upl
            s.update({"pl": money(a["pl"]), "unrealizedPL": money(upl), "resettablePL": money(a["resettablePL"]),
                      "financing": money(a["financing"]), "dividendAdjustment": money(a["dividendAdjustment"]),
                      "guaranteedExecutionFees": "0.0000"})
            sides[side] = s
        al, sh = agg["long"], agg["short"]
        return {"instrument": name, "pl": money(al["pl"] + sh["pl"]), "unrealizedPL": money(total_upl),
                "marginUsed": money(self.inst_margin(acct, inst, [t["_units"] for t in trades], q)),
                "resettablePL": money(al["resettablePL"] + sh["resettablePL"]),
                "financing": money(al["financing"] + sh["financing"]), "commission": money(agg["commission"]),
                "dividendAdjustment": money(al["dividendAdjustment"] + sh["dividendAdjustment"]),
                "guaranteedExecutionFees": "0.0000", "long": sides["long"], "short": sides["short"]}

    @staticmethod
    def _agg(acct, name):
        side = lambda: {"pl": D0, "resettablePL": D0, "financing": D0, "dividendAdjustment": D0}     # noqa: E731
        fresh = {"long": side(), "short": side(), "commission": D0}
        if isinstance(acct, dict):                 # a blank one, kept nowhere: a position never held
            return fresh
        return acct.pos.setdefault(name, fresh)

    @staticmethod
    def _px(inst, d):
        """A fill or average price: the grid's decimals when it is on the grid, up to four more when it is an average
        across buckets (stand-in's own)."""
        d = Decimal(d)
        exact = d.quantize(inst.tick, rounding=ROUND_HALF_UP)
        if exact == d:
            return pstr(d, inst.dp)
        s = format(d.quantize(Decimal(1).scaleb(-(inst.dp + 4)), rounding=ROUND_HALF_UP), "f").rstrip("0")
        return s if len(s.split(".")[1]) >= inst.dp else pstr(d, inst.dp)

    # -- finding things by specifier (documented: https://developer.oanda.com/rest-live-v20/order-df/ OrderSpecifier, trade-df TradeSpecifier: an id, or the
    #    client's id prefixed by "@")
    def find_order(self, acct, spec, pending_only):
        if not isinstance(spec, str) or not spec:
            return None
        if spec.startswith("@"):
            cid = spec[1:]
            # Resolving "@id" only among PENDING orders is stand-in's own (documented: https://developer.oanda.com/rest-live-v20/transaction-df/ the client id is unique only among
            # pending orders, transaction-df CLIENT_ORDER_ID_ALREADY_EXISTS); options client_id_lookup "any" takes the
            # newest order with that id in any state.
            cands = [o for o in acct.orders.values() if (o.get("clientExtensions") or {}).get("id") == cid]
            if pending_only or self.options["client_id_lookup"] == "pending":
                cands = [o for o in cands if o["state"] == "PENDING"]
            return max(cands, key=_num_id) if cands else None
        o = acct.orders.get(spec)
        if o is None or (pending_only and o["state"] != "PENDING"):
            return None
        return o

    def find_trade(self, acct, spec, open_only):
        if not isinstance(spec, str) or not spec:
            return None
        if spec.startswith("@"):
            cid = spec[1:]
            cands = [t for t in acct.trades.values() if (t.get("clientExtensions") or {}).get("id") == cid]
            if open_only or self.options["client_id_lookup"] == "pending":
                cands = [t for t in cands if t["state"] == "OPEN"]
            return max(cands, key=_num_id) if cands else None
        t = acct.trades.get(spec)
        if t is None or (open_only and t["state"] != "OPEN"):
            return None
        return t

    # -- reading a request's fields
    def _client_ext(self, raw, kind, prefix=""):
        """ClientExtensions {id, tag, comment} (documented: https://developer.oanda.com/rest-live-v20/transaction-df/). kind "ORDER" or "TRADE"."""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise Reject(prefix + "CLIENT_%s_ID_INVALID" % kind if not prefix else prefix + "CLIENT_ORDER_ID_INVALID")
        out = {}
        for key, label in (("id", "ID"), ("tag", "TAG"), ("comment", "COMMENT")):
            v = raw.get(key)
            if v is None:
                continue
            if not isinstance(v, str) or not v or len(v) > CLIENT_TEXT_MAX:
                raise Reject(prefix + ("CLIENT_ORDER_%s_INVALID" % label if prefix else "CLIENT_%s_%s_INVALID" % (kind, label)))
            out[key] = v
        return out or None

    def _price(self, inst, raw, reason_prefix=""):
        """A PriceValue on the instrument's grid (documented: https://developer.oanda.com/rest-live-v20/transaction-df/ PRICE_MISSING, PRICE_INVALID, PRICE_PRECISION_EXCEEDED)."""
        if raw is None:
            raise Reject(reason_prefix + "PRICE_MISSING")
        d = dec(raw)
        if d is None or d <= 0:
            raise Reject(reason_prefix + "PRICE_INVALID")
        if places(d.normalize()) > inst.dp:
            raise Reject(reason_prefix + "PRICE_PRECISION_EXCEEDED")
        return d

    def _gtd(self, raw, tif, prefix, now_ns):
        if tif != "GTD":
            return None
        if raw is None:
            raise Reject(prefix + ("TIME_IN_FORCE_GTD_TIMESTAMP_MISSING" if not prefix else "GTD_TIMESTAMP_MISSING"))
        try:
            ns = parse_time(raw)
        except ValueError:
            raise HTTPError(400, invalid("gtdTime"))
        if ns <= now_ns:
            raise Reject(prefix + ("TIME_IN_FORCE_GTD_TIMESTAMP_IN_PAST" if not prefix else "GTD_TIMESTAMP_IN_PAST"))
        return ns

    def _details(self, inst, raw, kind, now_ns):
        """The on-fill details (documented: https://developer.oanda.com/rest-live-v20/transaction-df/ TakeProfitDetails, StopLossDetails, TrailingStopLossDetails,
        GuaranteedStopLossDetails, each with its own *_ON_FILL_* reject reasons). Returns (echo, internal) or None."""
        if raw is None:
            return None
        p = {"tp": "TAKE_PROFIT_ON_FILL_", "sl": "STOP_LOSS_ON_FILL_", "tsl": "TRAILING_STOP_LOSS_ON_FILL_",
             "gsl": "GUARANTEED_STOP_LOSS_ON_FILL_"}[kind]
        if kind == "gsl":
            raise Reject("GUARANTEED_STOP_LOSS_ON_FILL_NOT_ALLOWED")      # every instrument here has GSLO DISABLED
        if not isinstance(raw, dict):
            raise Reject(p + ("PRICE_INVALID" if kind != "tsl" else "PRICE_DISTANCE_INVALID"))
        echo, out = {}, {}
        if kind == "tp":
            out["price"] = self._price(inst, raw.get("price"), p)
            echo["price"] = pstr(out["price"], inst.dp)
        elif kind == "sl":
            if raw.get("guaranteed") is True:
                raise Reject("STOP_LOSS_ON_FILL_GUARANTEED_NOT_ALLOWED")
            has_p, has_d = raw.get("price") is not None, raw.get("distance") is not None
            if has_p and has_d:
                raise Reject(p + "PRICE_AND_DISTANCE_BOTH_SPECIFIED")
            if not has_p and not has_d:
                raise Reject(p + "PRICE_AND_DISTANCE_BOTH_MISSING")
            if has_p:
                out["price"] = self._price(inst, raw.get("price"), p)
                echo["price"] = pstr(out["price"], inst.dp)
            else:
                d = dec(raw.get("distance"))
                if d is None or d <= 0:
                    raise Reject(p + "DISTANCE_INVALID")
                if places(d.normalize()) > inst.dp:
                    raise Reject(p + "DISTANCE_PRECISION_EXCEEDED")
                out["distance"] = d
                echo["distance"] = pstr(d, inst.dp)
        else:
            d = dec(raw.get("distance"))
            if raw.get("distance") is None:
                raise Reject(p + "PRICE_DISTANCE_MISSING")
            if d is None or d <= 0:
                raise Reject(p + "PRICE_DISTANCE_INVALID")
            if places(d.normalize()) > inst.dp:
                raise Reject(p + "PRICE_DISTANCE_PRECISION_EXCEEDED")
            if d > inst.max_tsl:
                raise Reject(p + "PRICE_DISTANCE_MAXIMUM_EXCEEDED")
            if d < inst.min_tsl:
                raise Reject(p + "PRICE_DISTANCE_MINIMUM_NOT_MET")
            out["distance"] = d
            echo["distance"] = pstr(d, inst.dp)
        tif = raw.get("timeInForce", "GTC")
        if tif not in ("GTC", "GTD", "GFD"):
            raise Reject(p + "TIME_IN_FORCE_INVALID")
        out["tif"] = echo["timeInForce"] = tif
        out["gtd"] = self._gtd(raw.get("gtdTime"), tif, p, now_ns)
        if out["gtd"] is not None:
            echo["gtdTime"] = Stamp(out["gtd"])
        ce = self._client_ext(raw.get("clientExtensions"), "ORDER", prefix=p)
        if ce:
            out["cext"] = echo["clientExtensions"] = ce
        return echo, out

    def _units(self, inst, raw):
        """documented: https://developer.oanda.com/rest-live-v20/transaction-df/ UNITS_MISSING, UNITS_INVALID, UNITS_PRECISION_EXCEEDED (tradeUnitsPrecision), UNITS_LIMIT_EXCEEDED
        (maximumOrderUnits), UNITS_MINIMUM_NOT_MET (minimumTradeSize). Zero being UNITS_INVALID is stand-in's own."""
        if raw is None:
            raise Reject("UNITS_MISSING")
        u = dec(raw)
        if u is None or u == 0:
            raise Reject("UNITS_INVALID")
        if places(u.normalize()) > inst.tup:
            raise Reject("UNITS_PRECISION_EXCEEDED")
        if abs(u) > inst.max_units:
            raise Reject("UNITS_LIMIT_EXCEEDED")
        if abs(u) < inst.min_size:
            raise Reject("UNITS_MINIMUM_NOT_MET")
        return u

    def parse(self, acct, req, replacing=None):
        """An OrderRequest (documented: https://developer.oanda.com/rest-live-v20/order-df/ *OrderRequest) read and checked; the order of the checks is stand-in's
        own (the documentation does not give one). Returns a spec; raises Reject or HTTPError."""
        typ = req.get("type", "MARKET")
        now_ns = self.clock.now_ns()
        s = {"type": typ, "raw": req}
        if typ in DEPENDENT:
            return self._parse_dependent(acct, req, s, replacing, now_ns)
        if typ == "GUARANTEED_STOP_LOSS":
            raise Reject("GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED")
        name = req.get("instrument")
        if name is None:
            raise Reject("INSTRUMENT_MISSING")
        inst = self.market.instruments.get(name) if isinstance(name, str) else None
        if inst is None:
            raise Reject("INSTRUMENT_UNKNOWN")
        s["inst"] = inst
        s["units"] = self._units(inst, req.get("units"))
        if typ in ENTRY:
            s["price"] = self._price(inst, req.get("price"))
        if req.get("priceBound") is not None and typ in ("MARKET", "STOP", "MARKET_IF_TOUCHED"):
            b = dec(req.get("priceBound"))
            if b is None or b <= 0:
                raise Reject("PRICE_BOUND_INVALID")
            if places(b.normalize()) > inst.dp:
                raise Reject("PRICE_BOUND_PRECISION_EXCEEDED")
            s["bound"] = b
        tif = req.get("timeInForce", "FOK" if typ == "MARKET" else "GTC")
        if tif not in TIF_ALLOWED[typ]:
            raise Reject("TIME_IN_FORCE_INVALID")
        s["tif"] = tif
        s["gtd"] = self._gtd(req.get("gtdTime"), tif, "", now_ns)
        pf = req.get("positionFill", "DEFAULT")
        if pf not in POSITION_FILLS:
            raise Reject("ORDER_FILL_POSITION_ACTION_INVALID")
        s["pfill"] = pf
        trig = req.get("triggerCondition", "DEFAULT")
        if trig not in TRIGGERS:
            raise Reject("TRIGGER_CONDITION_INVALID")
        s["trigger"] = trig
        s["cext"] = self._client_ext(req.get("clientExtensions"), "ORDER")
        s["tcext"] = self._client_ext(req.get("tradeClientExtensions"), "TRADE")
        cid = (s["cext"] or {}).get("id")
        if cid is not None and any(o["state"] == "PENDING" and (o.get("clientExtensions") or {}).get("id") == cid
                                   and o is not replacing for o in acct.orders.values()):
            raise Reject("CLIENT_ORDER_ID_ALREADY_EXISTS")
        tid = (s["tcext"] or {}).get("id")
        if tid is not None and any((t.get("clientExtensions") or {}).get("id") == tid for t in self.open_trades(acct)):
            raise Reject("CLIENT_TRADE_ID_ALREADY_EXISTS")
        for key, kind in (("takeProfitOnFill", "tp"), ("stopLossOnFill", "sl"), ("trailingStopLossOnFill", "tsl"),
                          ("guaranteedStopLossOnFill", "gsl")):
            s[kind] = self._details(inst, req.get(key), kind, now_ns)
        # stand-in's own: an OPEN_ONLY or REDUCE_ONLY order that cannot be honoured on a netting account now is refused
        # (the documentation gives no reason for it; verify on demo)
        opp = [t for t in self.open_trades(acct, inst.name) if (t["_units"] > 0) != (s["units"] > 0)]
        if pf == "OPEN_ONLY" and opp and not acct.hedging and typ == "MARKET":
            raise Reject("ORDER_FILL_POSITION_ACTION_INVALID")
        if pf == "REDUCE_ONLY" and typ == "MARKET" and sum((abs(t["_units"]) for t in opp), D0) < abs(s["units"]):
            raise Reject("ORDER_FILL_POSITION_ACTION_INVALID")
        if typ != "MARKET" and replacing is None and sum(1 for o in acct.orders.values() if o["state"] == "PENDING") >= PENDING_MAX:
            raise Reject("PENDING_ORDERS_ALLOWED_EXCEEDED")
        return s

    def _parse_dependent(self, acct, req, s, replacing, now_ns):
        typ = s["type"]
        if typ == "GUARANTEED_STOP_LOSS":
            raise Reject("GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED")
        tid, ctid = req.get("tradeID"), req.get("clientTradeID")
        if tid is None and ctid is None:
            raise Reject("TRADE_ID_UNSPECIFIED")
        trade = self.find_trade(acct, str(tid), True) if tid is not None else self.find_trade(acct, "@" + str(ctid), True)
        if trade is None:
            raise Reject("TRADE_DOESNT_EXIST", status=404)
        if ctid is not None and (trade.get("clientExtensions") or {}).get("id") != ctid:
            raise Reject("TRADE_IDENTIFIER_INCONSISTENCY")
        if replacing is not None and replacing.get("tradeID") != trade["id"]:
            raise Reject("REPLACING_TRADE_ID_INVALID")
        inst = self.market.find(trade["instrument"])
        s["inst"], s["trade"] = inst, trade
        if typ == "TRAILING_STOP_LOSS":
            d = dec(req.get("distance"))
            if req.get("distance") is None:
                raise Reject("PRICE_DISTANCE_MISSING")
            if d is None or d <= 0:
                raise Reject("PRICE_DISTANCE_INVALID")
            if places(d.normalize()) > inst.dp:
                raise Reject("PRICE_DISTANCE_PRECISION_EXCEEDED")
            if d > inst.max_tsl:
                raise Reject("PRICE_DISTANCE_MAXIMUM_EXCEEDED")
            if d < inst.min_tsl:
                raise Reject("PRICE_DISTANCE_MINIMUM_NOT_MET")
            s["distance"] = d
        elif typ == "STOP_LOSS":
            has_p, has_d = req.get("price") is not None, req.get("distance") is not None
            if req.get("guaranteed") is True:
                raise Reject("STOP_LOSS_ORDER_GUARANTEED_NOT_ALLOWED")
            if has_p and has_d:
                raise Reject("STOP_LOSS_ORDER_PRICE_AND_DISTANCE_BOTH_SPECIFIED")
            if not has_p and not has_d:
                raise Reject("STOP_LOSS_ORDER_PRICE_AND_DISTANCE_BOTH_MISSING")
            if has_p:
                s["price"] = self._price(inst, req.get("price"))
            else:
                d = dec(req.get("distance"))
                if d is None or d <= 0:
                    raise Reject("PRICE_DISTANCE_INVALID")
                if places(d.normalize()) > inst.dp:
                    raise Reject("PRICE_DISTANCE_PRECISION_EXCEEDED")
                s["distance"] = d
        else:
            s["price"] = self._price(inst, req.get("price"))
        tif = req.get("timeInForce", "GTC")
        if tif not in TIF_ALLOWED[typ]:
            raise Reject("TIME_IN_FORCE_INVALID")
        s["tif"] = tif
        s["gtd"] = self._gtd(req.get("gtdTime"), tif, "", now_ns)
        trig = req.get("triggerCondition", "DEFAULT")
        if trig not in TRIGGERS:
            raise Reject("TRIGGER_CONDITION_INVALID")
        s["trigger"] = trig
        s["cext"] = self._client_ext(req.get("clientExtensions"), "ORDER")
        cid = (s["cext"] or {}).get("id")
        if cid is not None and any(o["state"] == "PENDING" and (o.get("clientExtensions") or {}).get("id") == cid
                                   and o is not replacing for o in acct.orders.values()):
            raise Reject("CLIENT_ORDER_ID_ALREADY_EXISTS")
        if replacing is None:
            ref = {"TAKE_PROFIT": "_tp", "STOP_LOSS": "_sl", "TRAILING_STOP_LOSS": "_tsl"}[typ]
            if trade.get(ref) is not None and acct.orders[trade[ref]]["state"] == "PENDING":
                raise Reject(typ + "_ORDER_ALREADY_EXISTS")
        return s

    # -- the transaction and the Order a spec becomes
    @staticmethod
    def _onfill_echo(s):
        out = {}
        for key, kind in (("takeProfitOnFill", "tp"), ("stopLossOnFill", "sl"), ("trailingStopLossOnFill", "tsl")):
            if s.get(kind):
                out[key] = s[kind][0]
        if s.get("tcext"):
            out["tradeClientExtensions"] = s["tcext"]
        return out

    def _create_fields(self, s, reason, replaces=None):
        """The fields of a *_ORDER transaction, in the documented order (transaction-df)."""
        typ, inst = s["type"], s["inst"]
        f = {}
        if typ in DEPENDENT:
            t = s["trade"]
            f["tradeID"] = t["id"]
            ctid = (t.get("clientExtensions") or {}).get("id")
            if ctid:
                f["clientTradeID"] = ctid
            if s.get("price") is not None:
                f["price"] = pstr(s["price"], inst.dp)
            if s.get("distance") is not None:
                f["distance"] = pstr(s["distance"], inst.dp)
            f["timeInForce"] = s["tif"]
            if s.get("gtd") is not None:
                f["gtdTime"] = Stamp(s["gtd"])
            f["triggerCondition"] = s["trigger"]
            f["reason"] = reason
            if s.get("cext"):
                f["clientExtensions"] = s["cext"]
            if s.get("fill_tx") is not None:
                f["orderFillTransactionID"] = s["fill_tx"]
            if replaces:
                f["replacesOrderID"] = replaces
            return f
        f["instrument"] = inst.name
        f["units"] = plain(s["units"])
        if typ in ENTRY:
            f["price"] = pstr(s["price"], inst.dp)
        if s.get("bound") is not None:
            f["priceBound"] = pstr(s["bound"], inst.dp)
        f["timeInForce"] = s["tif"]
        if s.get("gtd") is not None:
            f["gtdTime"] = Stamp(s["gtd"])
        f["positionFill"] = s["pfill"]
        if typ != "MARKET":
            f["triggerCondition"] = s["trigger"]
        for key in ("tradeClose", "longPositionCloseout", "shortPositionCloseout", "marginCloseout"):
            if s.get(key):
                f[key] = s[key]
        f["reason"] = reason
        if s.get("cext"):
            f["clientExtensions"] = s["cext"]
        f.update(self._onfill_echo(s))
        if replaces:
            f["replacesOrderID"] = replaces
        return f

    def _register(self, acct, s, tx):
        """The Order object for a created order (documented: https://developer.oanda.com/rest-live-v20/order-df/), kept with what the engine needs."""
        typ, inst = s["type"], s["inst"]
        o = {"id": tx["id"], "createTime": tx["time"], "state": "PENDING"}
        if s.get("cext"):
            o["clientExtensions"] = s["cext"]
        o["type"] = typ
        for k in ("instrument", "units", "tradeID", "clientTradeID", "price", "distance", "priceBound", "timeInForce",
                  "gtdTime", "positionFill", "triggerCondition", "tradeClose", "longPositionCloseout",
                  "shortPositionCloseout", "marginCloseout"):
            if k in tx:
                o[k] = tx[k]
        if typ == "MARKET_IF_TOUCHED":
            # documented: https://developer.oanda.com/rest-live-v20/order-df/ MarketIfTouchedOrder.initialMarketPrice "The Market price at the time when the
            # MarketIfTouched Order was created"; which side of the market it uses is stand-in's own (the mid)
            q = self.market.quote(inst)
            o["initialMarketPrice"] = pstr(mid_of(inst, q.bid, q.ask), inst.dp)
            o["_initial"] = mid_of(inst, q.bid, q.ask)
        for key in ("takeProfitOnFill", "stopLossOnFill", "trailingStopLossOnFill", "tradeClientExtensions"):
            if key in tx:
                o[key] = tx[key]
        if "replacesOrderID" in tx:
            o["replacesOrderID"] = tx["replacesOrderID"]
        o.update({"_inst": inst.name, "_units": s.get("units"), "_price": s.get("price"), "_bound": s.get("bound"),
                  "_tif": s["tif"], "_gtd": s.get("gtd"), "_trigger": s.get("trigger", "DEFAULT"),
                  "_pfill": s.get("pfill", "DEFAULT"), "_tp": s.get("tp"), "_sl": s.get("sl"), "_tsl": s.get("tsl"),
                  "_tcext": s.get("tcext"), "_distance": s.get("distance")})
        if s["tif"] == "GFD":
            o["_gtd"] = next_ny_five(tx["time"].ns // MS_NS) * MS_NS
        if typ in DEPENDENT:
            t = s["trade"]
            o["_trade"] = t["id"]
            ref = {"TAKE_PROFIT": "_tp", "STOP_LOSS": "_sl", "TRAILING_STOP_LOSS": "_tsl"}[typ]
            t[ref] = o["id"]
            if typ == "STOP_LOSS" and s.get("price") is None:
                # documented: https://developer.oanda.com/rest-live-v20/transaction-df/ StopLossDetails.distance "from the Account's current price"; for a long trade the bid,
                # for a short one the ask (stand-in's own reading)
                q = self.market.quote(inst)
                px = q.bid - s["distance"] if t["_units"] > 0 else q.ask + s["distance"]
                o["price"], o["_price"] = pstr(px, inst.dp), px
            if typ == "TRAILING_STOP_LOSS":
                q = self.market.quote(inst)
                v = q.bid - s["distance"] if t["_units"] > 0 else q.ask + s["distance"]
                o["_tsv"] = v
                o["trailingStopValue"] = pstr(v, inst.dp)
        acct.orders[o["id"]] = o
        if acct.mt4 is not None and (s.get("cext") or s.get("tcext")):
            # documented: https://developer.oanda.com/rest-live-v20/transaction-df/ ClientExtensions "Do not set, modify, or delete this field if your account is
            # associated with MT4". What OANDA does then is not documented: the stand-in takes it and records it.
            self.violations.append({"rule": "clientExtensions set on an MT4-linked account", "account": acct.id,
                                    "order": o["id"], "doc": URLS["transaction-df"]})
        if typ != "MARKET" and typ in ENTRY:
            same = [x for x in acct.orders.values() if x["state"] == "PENDING" and x.get("_inst") == inst.name
                    and x["type"] in ENTRY and x.get("_price") == s["price"] and (x["_units"] > 0) == (s["units"] > 0)]
            if len(same) > 1:
                # Terms of Business 13.11(f)(iii): "placing multiple smaller sized pending orders at or near the same
                # price level instead of placing a single order". Recorded for tests; OANDA's own reaction is not
                # documented.
                self.violations.append({"rule": "several pending orders at one price (ToB 13.11(f)(iii))",
                                        "account": acct.id, "orders": [x["id"] for x in same],
                                        "doc": "https://legal.oanda.com/oel/fxtrade_customer_agreement_oel/en"})
        return o

    def _cancel(self, acct, o, reason, ctx, replaced_by=None):
        tx = self._tx(acct, "ORDER_CANCEL", ctx, orderID=o["id"],
                      clientOrderID=(o.get("clientExtensions") or {}).get("id"), reason=reason,
                      replacedByOrderID=replaced_by)
        o["state"] = "CANCELLED"
        o["cancellingTransactionID"], o["cancelledTime"] = tx["id"], tx["time"]
        if replaced_by:
            o["replacedByOrderID"] = replaced_by
        o["_done_tid"] = tx["id"]
        self._unlink(acct, o)
        return tx

    @staticmethod
    def _unlink(acct, o):
        if o.get("_trade"):
            t = acct.trades.get(o["_trade"])
            for ref in ("_tp", "_sl", "_tsl"):
                if t is not None and t.get(ref) == o["id"]:
                    t[ref] = None

    # -- requests
    def refusal_for(self, kind):
        """A refusal a control asked for: the next order request of this kind is refused with that reason."""
        for r in self.refusals:
            if r["count"] > 0 and r["kind"] in (kind, "any"):
                r["count"] -= 1
                return r["reason"]
        return None

    def create_order(self, acct, body, rid):
        """POST /v3/accounts/{accountID}/orders (documented: https://developer.oanda.com/rest-live-v20/order-ep/): 201 with orderCreateTransaction and, when the
        order filled or was cancelled at once, orderFillTransaction or orderCancelTransaction; 400 with
        orderRejectTransaction, errorCode, errorMessage."""
        with self.lock:
            req = body.get("order") if isinstance(body, dict) else None
            if not isinstance(req, dict):
                raise HTTPError(400, MSG_MISSING)
            typ = req.get("type", "MARKET")
            if typ not in TX_TYPE:
                raise HTTPError(400, invalid("order.type"))
            ctx = self.ctx(rid)
            forced = self.refusal_for("order")
            try:
                if forced in REJECT_REASONS:
                    raise Reject(forced)
                s = self.parse(acct, req)
            except Reject as e:
                rtx = self._tx(acct, TX_TYPE[typ] + "_REJECT", ctx, **self._raw_fields(req, e.reason))
                return e.status, self.reject_body(acct, ctx, "orderRejectTransaction", rtx, e.reason), {}
            tx = self._tx(acct, TX_TYPE[typ], ctx, **self._create_fields(s, "CLIENT_ORDER"))
            o = self._register(acct, s, tx)
            fill = cancel = None
            if forced is not None:                   # a documented OrderCancelReason: created, then cancelled at once
                cancel = self._cancel(acct, o, forced, ctx)
            elif typ == "MARKET":
                fill, cancel = self._execute(acct, o, ctx, immediate=True)
            elif typ in ENTRY:
                fill, cancel = self._try_entry(acct, o, ctx, created=True)
            else:
                fill = self._try_dependent(acct, o, ctx)
            body = self.answer(acct, ctx, orderCreateTransaction=tx, orderFillTransaction=fill,
                               orderCancelTransaction=cancel)
            return 201, body, {"Location": "/v3/accounts/%s/orders/%s" % (acct.id, o["id"])}

    @staticmethod
    def _raw_fields(req, reason, intended=None):
        """A *_REJECT transaction: the request's fields as sent, then intendedReplacesOrderID and rejectReason
        (documented: https://developer.oanda.com/rest-live-v20/transaction-df/)."""
        keep = ("instrument", "units", "tradeID", "clientTradeID", "price", "distance", "priceBound", "timeInForce",
                "gtdTime", "positionFill", "triggerCondition", "tradeClose", "longPositionCloseout",
                "shortPositionCloseout", "clientExtensions", "takeProfitOnFill", "stopLossOnFill",
                "trailingStopLossOnFill", "guaranteedStopLossOnFill", "tradeClientExtensions")
        out = {k: req[k] for k in keep if k in req and isinstance(req[k], (str, int, dict, list))}
        if "reason" not in out:
            out["reason"] = "CLIENT_ORDER"
        if intended:
            out["intendedReplacesOrderID"] = intended
        out["rejectReason"] = reason
        return out

    # -- filling
    def _execute(self, acct, o, ctx, immediate, q=None, closes=None, fill_reason=None):
        """Fill an order now, at the market, or cancel it with a documented reason. Returns (fill tx, cancel tx); for a
        resting order that must wait, (None, None).

        documented: https://legal.oanda.com/oel/execution_policy_oel/en Annex 1: for FX, metals, indices and
        commodities a market order is "Market FOK" ("There are no partial fills of the order. The order is
        rejected/cancelled in the case where the order size exceeds available liquidity"); a triggered stop or limit
        "returns to the pending state ... in the full amount". IOC: "Immediately partially filled Or Cancelled"
        (order-df TimeInForce): the part the book can take fills and the rest is cancelled INSUFFICIENT_LIQUIDITY."""
        inst = self.market.find(o["_inst"])
        q = q or self.market.quote(inst)
        units = o["_units"]
        buy = units > 0
        if not q.tradeable:
            # documented: https://www.oanda.com/uk-en/trading/hours-of-operation/ "Existing positions cannot be closed,
            # and new positions cannot be opened, when markets are closed";
            # https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason MARKET_HALTED
            return None, (self._cancel(acct, o, "MARKET_HALTED", ctx) if immediate else None)
        # documented: https://legal.oanda.com/oel/execution_policy_oel/en 15.1, a close-out is "a Close Out Order ... we
        # can determine ... fill quantity, aggregation, priority and pricing": the stand-in fills it whole, past the book
        # at its deepest price (stand-in's own reading)
        closeout = bool(o.get("marginCloseout"))
        px, avail = self.market.vwap(inst, q, units, buy, unlimited=closeout)
        if not closeout:
            self.market.use_cap(inst)
        fill_units = units
        partial = False
        if avail < abs(units):
            if not immediate:
                return None, None
            if o["_tif"] == "IOC" and avail > 0:
                fill_units, partial = (avail if buy else -avail), True
                px, _ = self.market.vwap(inst, q, fill_units, buy)
            else:
                return None, self._cancel(acct, o, "INSUFFICIENT_LIQUIDITY", ctx)
        if o.get("_bound") is not None and ((buy and px > o["_bound"]) or (not buy and px < o["_bound"])):
            # documented: https://developer.oanda.com/rest-live-v20/order-df/ priceBound "The worst price that the client is willing to have the Market Order filled
            # at"; StopOrder: "If the market gaps and crosses through both the price and the priceBound, the Stop Order
            # will be cancelled instead of being filled"; OrderCancelReason BOUNDS_VIOLATION
            return None, self._cancel(acct, o, "BOUNDS_VIOLATION", ctx)
        limit = o["_price"] if o["type"] in ("LIMIT", "TAKE_PROFIT") else None
        if limit is not None and ((buy and px > limit) or (not buy and px < limit)):
            # documented: https://legal.oanda.com/oel/execution_policy_oel/en "Limit GTC Full": filled only "provided that the most recent price is the same
            # or better than the specified client limit price", else it stays pending
            if immediate and o["_tif"] in ("FOK", "IOC"):
                return None, self._cancel(acct, o, "TIME_IN_FORCE_EXPIRED", ctx)       # stand-in's own reason
            return None, None
        # which trades this fill closes and what it opens
        if closes is None:
            opp = sorted((t for t in self.open_trades(acct, inst.name) if (t["_units"] > 0) != buy), key=_num_id)
            pf = o["_pfill"]
            if pf == "DEFAULT":
                # documented: https://developer.oanda.com/rest-live-v20/order-df/ OrderPositionFill DEFAULT: "REDUCE_FIRST behaviour for non-client hedging
                # Accounts" and "OPEN_ONLY behaviour for client hedging Accounts"
                pf = "OPEN_ONLY" if acct.hedging else "REDUCE_FIRST"
            closes, left = [], abs(fill_units)
            if pf in ("REDUCE_FIRST", "REDUCE_ONLY"):
                for t in opp:                                                    # oldest first (stand-in's own)
                    if left <= 0:
                        break
                    take = min(abs(t["_units"]), left)
                    closes.append((t, take))
                    left -= take
                if pf == "REDUCE_ONLY" and left > 0:
                    return None, self._cancel(acct, o, "INTERNAL_SERVER_ERROR", ctx)     # stand-in's own
            elif opp and not acct.hedging:
                return None, self._cancel(acct, o, "INTERNAL_SERVER_ERROR", ctx)         # stand-in's own
            open_units = left if buy else -left
        else:
            open_units = D0
        # margin (documented: https://legal.oanda.com/oel/execution_policy_oel/en 8.2 "The Trading Platforms will reject Orders and Trade requests if you
        # have insufficient Margin available"; OrderCancelReason INSUFFICIENT_MARGIN)
        if open_units:
            f = self.figures(acct)
            same = self.open_trades(acct, inst.name)
            closing = {t["id"]: take for t, take in closes}
            after = [t["_units"] - (take if t["_units"] > 0 else -take) for t in same for take in [closing.get(t["id"], D0)]]
            after.append(open_units)
            before_m = self.inst_margin(acct, inst, [t["_units"] for t in same], q)
            after_m = self.inst_margin(acct, inst, after, q)
            if after_m > before_m and f["margin"] - before_m + after_m > f["nav"]:
                return None, self._cancel(acct, o, "INSUFFICIENT_MARGIN", ctx)
            reason = self._onfill_loss(o, q, px, open_units > 0, inst)
            if reason:
                return None, self._cancel(acct, o, reason, ctx)
            tcid = ((o.get("_tcext") or {}) or {}).get("id")
            if tcid and any((t.get("clientExtensions") or {}).get("id") == tcid for t in self.open_trades(acct)):
                return None, self._cancel(acct, o, "CLIENT_TRADE_ID_ALREADY_EXISTS", ctx)
        reason = fill_reason or FILL_REASON.get(o["type"], "MARKET_ORDER")
        if o.get("tradeClose"):
            reason = "MARKET_ORDER_TRADE_CLOSE"
        elif o.get("longPositionCloseout") or o.get("shortPositionCloseout"):
            reason = "MARKET_ORDER_POSITION_CLOSEOUT"
        elif o.get("marginCloseout"):
            reason = "MARKET_ORDER_MARGIN_CLOSEOUT"
        fill = self._fill(acct, o, inst, q, px, fill_units, closes, open_units, reason, ctx)
        cancel = self._cancel(acct, o, "INSUFFICIENT_LIQUIDITY", ctx) if partial else None
        if partial:
            # stand-in's own: the part-filled order keeps its fill fields and ends CANCELLED for the rest
            o["state"] = "CANCELLED"
        return fill, cancel

    @staticmethod
    def _onfill_loss(o, q, px, long_trade, inst):
        """documented: https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason TAKE_PROFIT_ON_FILL_LOSS, STOP_LOSS_ON_FILL_LOSS ("would have been filled
        immediately"), LOSING_TAKE_PROFIT ("would close the new Trade at a loss"); the price each is judged at is
        stand-in's own (the bid for a long trade's exits, the ask for a short one's)."""
        tp, sl = o.get("_tp"), o.get("_sl")
        exit_px = q.bid if long_trade else q.ask
        if tp:
            p = tp[1]["price"]
            if (long_trade and p <= exit_px) or (not long_trade and p >= exit_px):
                return "TAKE_PROFIT_ON_FILL_LOSS"
            if (long_trade and p < px) or (not long_trade and p > px):
                return "LOSING_TAKE_PROFIT"
        if sl and sl[1].get("price") is not None:
            p = sl[1]["price"]
            if (long_trade and p >= exit_px) or (not long_trade and p <= exit_px):
                return "STOP_LOSS_ON_FILL_LOSS"
        return None

    def _fill(self, acct, o, inst, q, px, fill_units, closes, open_units, reason, ctx):
        """The ORDER_FILL (documented: https://developer.oanda.com/rest-live-v20/transaction-df/ OrderFillTransaction, TradeOpen, TradeReduce) and what follows it:
        the cancels of the closed trades' dependent orders (LINKED_TRADE_CLOSED) and the new trade's on-fill orders."""
        gain, loss, pv = self.market.factors(inst.quote_ccy, acct.currency)
        mid = mid_of(inst, q.bid, q.ask)
        ptxt = self._px(inst, px)
        fill_id = str(acct.last_id + 1)
        closed, reduced, pl_home, pl_quote, half_total = [], None, D0, D0, D0
        done = []
        for t, take in closes:
            sign = 1 if t["_units"] > 0 else -1
            q_pl = (px - t["_price"]) * take * sign
            h_pl = (q_pl * (gain if q_pl >= 0 else loss)).quantize(Q4, rounding=ROUND_HALF_UP)
            half = (take * abs(px - mid) * pv).quantize(Q4, rounding=ROUND_HALF_UP)
            pl_home, pl_quote, half_total = pl_home + h_pl, pl_quote + q_pl, half_total + half
            t["_units"] -= take * sign
            t["_realized"] += h_pl
            t["_closed_value"] += px * take
            t["_closed_units"] += take
            t["realizedPL"] = money(t["_realized"])
            t["currentUnits"] = plain(t["_units"])
            t["averageClosePrice"] = self._px(inst, t["_closed_value"] / t["_closed_units"])
            t.setdefault("closingTransactionIDs", []).append(fill_id)
            entry = {"tradeID": t["id"], "units": plain(-take * sign), "price": ptxt, "realizedPL": money(h_pl),
                     "financing": "0.0000", "baseFinancing": "0", "guaranteedExecutionFee": "0.0000",
                     "quoteGuaranteedExecutionFee": "0", "halfSpreadCost": money(half)}
            agg = self._agg(acct, inst.name)["long" if sign > 0 else "short"]
            agg["pl"] += h_pl
            agg["resettablePL"] += h_pl
            if t["_units"] == 0:
                t["state"] = "CLOSED"
                t["closeTime"] = Stamp(ctx["ns"])
                t["_close_tid"] = fill_id
                closed.append(entry)
                done.append(t)
            else:
                t["_reduce_tid"] = fill_id
                reduced = entry
        acct.balance += pl_home
        acct.pl += pl_home
        acct.resettable += pl_home
        trade_open = None
        if open_units:
            half = (abs(open_units) * abs(px - mid) * pv).quantize(Q4, rounding=ROUND_HALF_UP)
            half_total += half
            im = (abs(open_units) * mid * self.market.rate(inst.quote_ccy, acct.currency) * self._rate(acct, inst))
            trade_open = {"tradeID": fill_id, "units": plain(open_units), "price": ptxt,
                          "guaranteedExecutionFee": "0.0000", "quoteGuaranteedExecutionFee": "0"}
            if o.get("_tcext"):
                trade_open["clientExtensions"] = o["_tcext"]
            trade_open["halfSpreadCost"] = money(half)
            trade_open["initialMarginRequired"] = money(im)
        factors = {"gainQuoteHome": {"factor": factor(gain)}, "lossQuoteHome": {"factor": factor(loss)}}
        if inst.base in ("EUR", "GBP", "USD", "JPY", "AUD", "CHF", "CAD", "NZD", "TRY"):
            bg, bl, _ = self.market.factors(inst.base, acct.currency)
            factors.update({"gainBaseHome": {"factor": factor(bg)}, "lossBaseHome": {"factor": factor(bl)}})
        cid = (o.get("clientExtensions") or {}).get("id")
        tx = self._tx(acct, "ORDER_FILL", ctx, orderID=o["id"], clientOrderID=cid, instrument=inst.name,
                      units=plain(fill_units), gainQuoteHomeConversionFactor=factor(gain),
                      lossQuoteHomeConversionFactor=factor(loss), homeConversionFactors=factors, price=ptxt,
                      fullVWAP=ptxt, fullPrice=self.price_json(q, fill=True), reason=reason, pl=money(pl_home),
                      quotePL=factor(pl_quote), financing="0.0000", baseFinancing="0", commission="0.0000",
                      guaranteedExecutionFee="0.0000", quoteGuaranteedExecutionFee="0",
                      accountBalance=money(acct.balance), tradeOpened=trade_open, tradesClosed=closed or None,
                      tradeReduced=reduced, halfSpreadCost=money(half_total))
        o["state"] = "FILLED"
        o["fillingTransactionID"], o["filledTime"] = tx["id"], tx["time"]
        if trade_open:
            o["tradeOpenedID"] = fill_id
        if reduced:
            o["tradeReducedID"] = reduced["tradeID"]
        if closed:
            o["tradeClosedIDs"] = [c["tradeID"] for c in closed]
        o["_done_tid"] = tx["id"]
        for t in done:
            for ref in ("_tp", "_sl", "_tsl"):
                oid = t.get(ref)
                if oid is not None and acct.orders[oid]["state"] == "PENDING":
                    self._cancel(acct, acct.orders[oid], "LINKED_TRADE_CLOSED", ctx)
        if trade_open:
            self._open_trade(acct, o, inst, q, px, open_units, fill_id, tx, ctx)
        return tx

    def _open_trade(self, acct, o, inst, q, px, units, tid, fill_tx, ctx):
        """A new Trade (documented: https://developer.oanda.com/rest-live-v20/trade-df/; TradeID "derived from the TransactionID of the Transaction that opened the
        Trade") and its on-fill dependent orders, reason ON_FILL (documented: https://developer.oanda.com/rest-live-v20/transaction-df/ TakeProfitOrderReason ON_FILL ...)."""
        mid = mid_of(inst, q.bid, q.ask)
        im = abs(units) * mid * self.market.rate(inst.quote_ccy, acct.currency) * self._rate(acct, inst)
        t = {"id": tid, "instrument": inst.name, "price": self._px(inst, px), "openTime": fill_tx["time"],
             "state": "OPEN", "initialUnits": plain(units), "initialMarginRequired": money(im),
             "currentUnits": plain(units), "realizedPL": "0.0000", "closingTransactionIDs": [], "financing": "0.0000",
             "dividendAdjustment": "0.0000"}
        if o.get("_tcext"):
            t["clientExtensions"] = o["_tcext"]
        t.update({"_units": units, "_price": px, "_realized": D0, "_financing": D0, "_div": D0, "_closed_value": D0,
                  "_closed_units": D0, "_tp": None, "_sl": None, "_tsl": None, "_open_tid": tid})
        acct.trades[tid] = t
        self._agg(acct, inst.name)
        for kind, typ in (("_tp", "TAKE_PROFIT"), ("_sl", "STOP_LOSS"), ("_tsl", "TRAILING_STOP_LOSS")):
            det = o.get(kind)
            if not det:
                continue
            _, d = det
            s = {"type": typ, "inst": inst, "trade": t, "price": d.get("price"), "distance": d.get("distance"),
                 "tif": d["tif"], "gtd": d.get("gtd"), "trigger": "DEFAULT", "cext": d.get("cext"), "fill_tx": tid}
            if typ == "TRAILING_STOP_LOSS":
                s["price"] = None
            tx = self._tx(acct, TX_TYPE[typ], ctx, **self._create_fields(s, "ON_FILL"))
            self._register(acct, s, tx)
        return t

    def _cmp_price(self, o, q, buy):
        """documented: https://developer.oanda.com/rest-live-v20/order-df/ OrderTriggerCondition: DEFAULT compares "to the ask for long Orders and bid for short
        Orders", INVERSE the opposite, BID, ASK and MID regardless."""
        trig = o.get("_trigger", "DEFAULT")
        if trig == "DEFAULT":
            return q.ask if buy else q.bid
        if trig == "INVERSE":
            return q.bid if buy else q.ask
        if trig == "BID":
            return q.bid
        if trig == "ASK":
            return q.ask
        return mid_of(q.inst, q.bid, q.ask)

    def _entry_triggered(self, o, q):
        buy = o["_units"] > 0
        c = self._cmp_price(o, q, buy)
        p = o["_price"]
        if o["type"] == "LIMIT":
            # documented: https://legal.oanda.com/oel/execution_policy_oel/en "Limit GTC Full": triggered when the price "is the same or better than the
            # specified client limit price"
            return c <= p if buy else c >= p
        if o["type"] == "STOP":
            # documented: https://legal.oanda.com/oel/execution_policy_oel/en "Market GTC Full": triggered when the price "is the same or worse than the
            # specified trigger level"
            return c >= p if buy else c <= p
        # MARKET_IF_TOUCHED: touched from the side it was placed on (stand-in's own reading of order-df)
        return c <= p if p <= o["_initial"] else c >= p

    def _try_entry(self, acct, o, ctx, created=False, q=None):
        inst = self.market.find(o["_inst"])
        q = q or self.market.quote(inst)
        if not q.tradeable or not self._entry_triggered(o, q):
            if created and o["_tif"] in ("FOK", "IOC"):
                return None, self._cancel(acct, o, "TIME_IN_FORCE_EXPIRED", ctx)       # stand-in's own reason
            return None, None
        # documented: https://developer.oanda.com/rest-live-v20/order-ep/ orderFillTransaction "Only provided when the Order was immediately filled": an order that
        # is marketable when placed fills at once (stand-in's own reading for STOP and MIT placed past their level)
        return self._execute(acct, o, ctx, immediate=o["_tif"] in ("FOK", "IOC"), q=q)

    def _try_dependent(self, acct, o, ctx, q=None):
        """A trade's take profit, stop loss or trailing stop (documented: https://developer.oanda.com/rest-live-v20/order-df/; execution policy: a stop loss is
        "Market GTC Full", a take profit "Limit GTC Full"). Returns the fill transaction or None."""
        t = acct.trades.get(o["_trade"])
        if t is None or t["state"] != "OPEN":
            return None
        inst = self.market.find(t["instrument"])
        q = q or self.market.quote(inst)
        if not q.tradeable:
            return None
        long_trade = t["_units"] > 0
        c = self._cmp_price(o, q, not long_trade)          # the exit is a short order for a long trade
        if o["type"] == "TRAILING_STOP_LOSS":
            # documented: https://developer.oanda.com/rest-live-v20/order-df/ "The trailing stop value will trail (follow) the market price by the TSL order's
            # configured price distance"; OEL sub-accounts PDF "The stop price level is continually adjusted based on
            # incoming quotes"
            v = c - o["_distance"] if long_trade else c + o["_distance"]
            if (long_trade and v > o["_tsv"]) or (not long_trade and v < o["_tsv"]):
                o["_tsv"] = v
                o["trailingStopValue"] = pstr(v, inst.dp)
            hit = c <= o["_tsv"] if long_trade else c >= o["_tsv"]
        elif o["type"] == "STOP_LOSS":
            hit = c <= o["_price"] if long_trade else c >= o["_price"]
        else:
            hit = c >= o["_price"] if long_trade else c <= o["_price"]
        if not hit:
            return None
        o["_units"] = -t["_units"]
        o["_inst"] = inst.name
        o["_pfill"] = "REDUCE_ONLY"
        fill, _ = self._execute(acct, o, ctx, immediate=False, q=q, closes=[(t, abs(t["_units"]))])
        return fill

    # -- replacing, cancelling, client extensions (documented: https://developer.oanda.com/rest-live-v20/order-ep/)
    def replace_order(self, acct, spec, body, rid):
        """PUT .../orders/{orderSpecifier}: "Replace an Order in an Account by simultaneously cancelling it and creating
        a replacement Order" (documented: https://developer.oanda.com/rest-live-v20/order-ep/); the replacement gets a new id."""
        with self.lock:
            ctx = self.ctx(rid)
            old = self.find_order(acct, spec, pending_only=True)
            if old is None:
                rtx = self._tx(acct, "ORDER_CANCEL_REJECT", ctx, **self._spec_fields(spec), rejectReason="ORDER_DOESNT_EXIST")
                return 404, self.reject_body(acct, ctx, "orderCancelRejectTransaction", rtx, "ORDER_DOESNT_EXIST"), {}
            req = body.get("order") if isinstance(body, dict) else None
            if not isinstance(req, dict):
                raise HTTPError(400, MSG_MISSING)
            typ = req.get("type", "MARKET")
            if typ not in TX_TYPE:
                raise HTTPError(400, invalid("order.type"))
            forced = self.refusal_for("replace")
            try:
                if typ != old["type"]:
                    # documented: https://developer.oanda.com/rest-live-v20/transaction-df/ REPLACING_ORDER_INVALID "The Order to be replaced has a different type than the replacing
                    # Order."
                    raise Reject("REPLACING_ORDER_INVALID")
                if forced in REJECT_REASONS:
                    raise Reject(forced)
                s = self.parse(acct, req, replacing=old)
                if typ in ENTRY and s["inst"].name != old["_inst"]:
                    raise Reject("REPLACING_ORDER_INVALID")           # stand-in's own: the same instrument
            except Reject as e:
                rtx = self._tx(acct, TX_TYPE[typ] + "_REJECT", ctx, **self._raw_fields(req, e.reason, intended=old["id"]))
                return e.status, self.reject_body(acct, ctx, "orderRejectTransaction", rtx, e.reason), {}
            new_id = str(acct.last_id + 2)
            ctx_cancel = self._cancel(acct, old, "CLIENT_REQUEST_REPLACED", ctx, replaced_by=new_id)
            tx = self._tx(acct, TX_TYPE[typ], ctx, **self._create_fields(s, "REPLACEMENT", replaces=old["id"]))
            o = self._register(acct, s, tx)
            fill = cancel = None
            if forced is not None:
                cancel = self._cancel(acct, o, forced, ctx)
            elif typ in ENTRY:
                fill, cancel = self._try_entry(acct, o, ctx, created=True)
            else:
                fill = self._try_dependent(acct, o, ctx)
            body = self.answer(acct, ctx, orderCancelTransaction=ctx_cancel, orderCreateTransaction=tx,
                               orderFillTransaction=fill, replacingOrderCancelTransaction=cancel)
            return 201, body, {"Location": "/v3/accounts/%s/orders/%s" % (acct.id, o["id"])}

    @staticmethod
    def _spec_fields(spec):
        if isinstance(spec, str) and spec.startswith("@"):
            return {"clientOrderID": spec[1:]}
        return {"orderID": spec}

    def cancel_order(self, acct, spec, rid):
        """PUT .../orders/{orderSpecifier}/cancel (documented: https://developer.oanda.com/rest-live-v20/order-ep/): 200 with orderCancelTransaction (reason
        CLIENT_REQUEST); 404 with orderCancelRejectTransaction when it does not exist. An order that is no longer pending
        is answered as one that does not exist (stand-in's own: the enum has no other reason for it)."""
        with self.lock:
            ctx = self.ctx(rid)
            o = self.find_order(acct, spec, pending_only=True)
            forced = self.refusal_for("cancel")
            if o is None or forced:
                reason = forced if forced in REJECT_REASONS else "ORDER_DOESNT_EXIST"
                rtx = self._tx(acct, "ORDER_CANCEL_REJECT", ctx, **self._spec_fields(spec), rejectReason=reason)
                status = 404 if reason == "ORDER_DOESNT_EXIST" else 400
                return status, self.reject_body(acct, ctx, "orderCancelRejectTransaction", rtx, reason), {}
            tx = self._cancel(acct, o, "CLIENT_REQUEST", ctx)
            return 200, self.answer(acct, ctx, orderCancelTransaction=tx), {}

    def order_client_extensions(self, acct, spec, body, rid):
        """PUT .../orders/{orderSpecifier}/clientExtensions (documented: https://developer.oanda.com/rest-live-v20/order-ep/)."""
        with self.lock:
            ctx = self.ctx(rid)
            o = self.find_order(acct, spec, pending_only=True)
            body = body if isinstance(body, dict) else {}
            try:
                if o is None:
                    raise Reject("ORDER_DOESNT_EXIST", status=404)
                ce = self._client_ext(body.get("clientExtensions"), "ORDER")
                tce = self._client_ext(body.get("tradeClientExtensions"), "TRADE")
                if not ce and not tce:
                    raise Reject("CLIENT_EXTENSIONS_DATA_MISSING")
                cid = (ce or {}).get("id")
                if cid and any(x is not o and x["state"] == "PENDING" and (x.get("clientExtensions") or {}).get("id") == cid
                               for x in acct.orders.values()):
                    raise Reject("CLIENT_ORDER_ID_ALREADY_EXISTS")
            except Reject as e:
                rtx = self._tx(acct, "ORDER_CLIENT_EXTENSIONS_MODIFY_REJECT", ctx, **self._spec_fields(spec),
                               clientExtensionsModify=body.get("clientExtensions"),
                               tradeClientExtensionsModify=body.get("tradeClientExtensions"), rejectReason=e.reason)
                return e.status, self.reject_body(acct, ctx, "orderClientExtensionsModifyRejectTransaction", rtx,
                                                  e.reason), {}
            if acct.mt4 is not None:
                self.violations.append({"rule": "clientExtensions modified on an MT4-linked account", "account": acct.id,
                                        "order": o["id"], "doc": URLS["order-ep"]})
            if ce:
                o["clientExtensions"] = ce
            if tce:
                o["tradeClientExtensions"] = o["_tcext"] = tce
            tx = self._tx(acct, "ORDER_CLIENT_EXTENSIONS_MODIFY", ctx, orderID=o["id"],
                          clientOrderID=(o.get("clientExtensions") or {}).get("id"), clientExtensionsModify=ce,
                          tradeClientExtensionsModify=tce)
            return 200, self.answer(acct, ctx, orderClientExtensionsModifyTransaction=tx), {}

    # -- trades (documented: https://developer.oanda.com/rest-live-v20/trade-ep/)
    def close_trade(self, acct, spec, body, rid):
        """PUT .../trades/{tradeSpecifier}/close {"units": "ALL" | n} (documented: https://developer.oanda.com/rest-live-v20/trade-ep/): a MARKET_ORDER with
        tradeClose, then its fill (reason MARKET_ORDER_TRADE_CLOSE) or its cancel."""
        with self.lock:
            ctx = self.ctx(rid)
            body = body if isinstance(body, dict) else {}
            raw = body.get("units", "ALL")
            t = self.find_trade(acct, spec, open_only=True)
            close = {"tradeID": spec[1:] if spec.startswith("@") else spec, "units": raw if isinstance(raw, str) else str(raw)}
            if spec.startswith("@"):
                close = {"clientTradeID": spec[1:], "units": close["units"]}
            forced = self.refusal_for("trade_close")
            try:
                if t is None:
                    raise Reject("TRADE_DOESNT_EXIST", status=404)
                inst = self.market.find(t["instrument"])
                if raw == "ALL":
                    take = abs(t["_units"])
                else:
                    u = dec(raw)
                    if u is None or u <= 0:
                        raise Reject("UNITS_INVALID")
                    if places(u.normalize()) > inst.tup:
                        raise Reject("UNITS_PRECISION_EXCEEDED")
                    if u > abs(t["_units"]):
                        raise Reject("CLOSE_TRADE_UNITS_EXCEED_TRADE_SIZE")
                    take = u
                if forced in REJECT_REASONS:
                    raise Reject(forced)
            except Reject as e:
                rtx = self._tx(acct, "MARKET_ORDER_REJECT", ctx, tradeClose=close, reason="TRADE_CLOSE",
                               rejectReason=e.reason)
                return e.status, self.reject_body(acct, ctx, "orderRejectTransaction", rtx, e.reason), {}
            ctid = (t.get("clientExtensions") or {}).get("id")
            tc = {"tradeID": t["id"]}
            if ctid:
                tc["clientTradeID"] = ctid
            tc["units"] = "ALL" if raw == "ALL" else plain(take)
            sign = 1 if t["_units"] > 0 else -1
            s = {"type": "MARKET", "inst": inst, "units": -take * sign, "tif": "FOK", "pfill": "REDUCE_ONLY",
                 "tradeClose": tc}
            tx = self._tx(acct, "MARKET_ORDER", ctx, **self._create_fields(s, "TRADE_CLOSE"))
            o = self._register(acct, s, tx)
            fill = cancel = None
            if forced is not None:
                cancel = self._cancel(acct, o, forced, ctx)
            elif acct.fifo and any(_num_id(x) < _num_id(t) for x in self.open_trades(acct, inst.name)
                                   if (x["_units"] > 0) == (t["_units"] > 0)):
                # documented: https://help.oanda.com/us/en/faqs/account-types-and-leverage-us.htm FIFO applies to US
                # accounts; https://developer.oanda.com/rest-live-v20/transaction-df/ OrderCancelReason FIFO_VIOLATION.
                # That a trade closed before an older one on the same instrument is cancelled this way is stand-in's own.
                cancel = self._cancel(acct, o, "FIFO_VIOLATION", ctx)
            else:
                fill, cancel = self._execute(acct, o, ctx, immediate=True, closes=[(t, take)])
            body = self.answer(acct, ctx, orderCreateTransaction=tx, orderFillTransaction=fill,
                               orderCancelTransaction=cancel)
            return 200, body, {}

    def trade_client_extensions(self, acct, spec, body, rid):
        with self.lock:
            ctx = self.ctx(rid)
            t = self.find_trade(acct, spec, open_only=True)
            body = body if isinstance(body, dict) else {}
            try:
                if t is None:
                    raise Reject("TRADE_DOESNT_EXIST", status=404)
                ce = self._client_ext(body.get("clientExtensions"), "TRADE")
                if not ce:
                    raise Reject("CLIENT_EXTENSIONS_DATA_MISSING")
                if ce.get("id") and any(x is not t and (x.get("clientExtensions") or {}).get("id") == ce["id"]
                                        for x in self.open_trades(acct)):
                    raise Reject("CLIENT_TRADE_ID_ALREADY_EXISTS")
            except Reject as e:
                f = {"clientTradeID": spec[1:]} if spec.startswith("@") else {"tradeID": spec}
                rtx = self._tx(acct, "TRADE_CLIENT_EXTENSIONS_MODIFY_REJECT", ctx, **f,
                               tradeClientExtensionsModify=body.get("clientExtensions"), rejectReason=e.reason)
                return e.status, self.reject_body(acct, ctx, "tradeClientExtensionsModifyRejectTransaction", rtx,
                                                  e.reason), {}
            if acct.mt4 is not None:
                self.violations.append({"rule": "clientExtensions modified on an MT4-linked account", "account": acct.id,
                                        "trade": t["id"], "doc": URLS["trade-ep"]})
            t["clientExtensions"] = ce
            tx = self._tx(acct, "TRADE_CLIENT_EXTENSIONS_MODIFY", ctx, tradeID=t["id"], clientTradeID=ce.get("id"),
                          tradeClientExtensionsModify=ce)
            return 200, self.answer(acct, ctx, tradeClientExtensionsModifyTransaction=tx), {}

    def trade_orders(self, acct, spec, body, rid):
        """PUT .../trades/{tradeSpecifier}/orders (documented: https://developer.oanda.com/rest-live-v20/trade-ep/): create, replace or cancel the trade's take
        profit, stop loss and trailing stop. "If takeProfit is set to null, the Take Profit Order will be cancelled if it
        exists"; a field left out leaves its order alone; on a replacement the sub-fields left out are "inherited by the
        replacement Order"."""
        with self.lock:
            ctx = self.ctx(rid)
            body = body if isinstance(body, dict) else {}
            t = self.find_trade(acct, spec, open_only=True)
            if t is None:
                raise HTTPError(404, MSG_NO_TRADE)
            if body.get("guaranteedStopLoss") is not None:
                rtx = self._tx(acct, "GUARANTEED_STOP_LOSS_ORDER_REJECT", ctx, tradeID=t["id"],
                               rejectReason="GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED")
                return 400, self.reject_body(acct, ctx, "guaranteedStopLossOrderRejectTransaction", rtx,
                                             "GUARANTEED_STOP_LOSS_ORDER_NOT_ALLOWED"), {}
            plan = []
            for key, typ, ref, pre in (("takeProfit", "TAKE_PROFIT", "_tp", "takeProfit"),
                                       ("stopLoss", "STOP_LOSS", "_sl", "stopLoss"),
                                       ("trailingStopLoss", "TRAILING_STOP_LOSS", "_tsl", "trailingStopLoss")):
                if key not in body:
                    continue
                cur = acct.orders.get(t[ref]) if t.get(ref) else None
                cur = cur if cur is not None and cur["state"] == "PENDING" else None
                if body[key] is None:
                    plan.append((pre, cur, None))
                    continue
                req = dict(body[key]) if isinstance(body[key], dict) else {}
                if cur is not None:
                    for k in ("timeInForce", "gtdTime", "clientExtensions", "triggerCondition"):
                        if k not in req and k in cur:
                            req[k] = cur[k].render("RFC3339") if isinstance(cur[k], Stamp) else cur[k]
                req.update({"type": typ, "tradeID": t["id"]})
                try:
                    s = self.parse(acct, req, replacing=cur or {"tradeID": t["id"]})
                except Reject as e:
                    rtx = self._tx(acct, TX_TYPE[typ] + "_REJECT", ctx,
                                   **self._raw_fields(req, e.reason, intended=cur["id"] if cur else None))
                    return 400, self.reject_body(acct, ctx, pre + "OrderRejectTransaction", rtx, e.reason), {}
                plan.append((pre, cur, s))
            out = {}
            for pre, cur, s in plan:
                if s is None:
                    if cur is not None:
                        out[pre + "OrderCancelTransaction"] = self._cancel(acct, cur, "CLIENT_REQUEST", ctx)
                    continue
                if cur is not None:
                    new_id = str(acct.last_id + 2)
                    out[pre + "OrderCancelTransaction"] = self._cancel(acct, cur, "CLIENT_REQUEST_REPLACED", ctx,
                                                                       replaced_by=new_id)
                tx = self._tx(acct, TX_TYPE[s["type"]], ctx,
                              **self._create_fields(s, "REPLACEMENT" if cur is not None else "CLIENT_ORDER",
                                                    replaces=cur["id"] if cur is not None else None))
                out[pre + "OrderTransaction"] = tx
                o = self._register(acct, s, tx)
                fill = self._try_dependent(acct, o, ctx)
                if fill is not None:
                    out[pre + "OrderFillTransaction"] = fill
                    break
            return 200, self.answer(acct, ctx, **out), {}

    # -- positions (documented: https://developer.oanda.com/rest-live-v20/position-ep/)
    def close_position(self, acct, name, body, rid):
        """PUT .../positions/{instrument}/close {longUnits, shortUnits: "ALL" | "NONE" | n} (documented: https://developer.oanda.com/rest-live-v20/position-ep/).
        Leaving a side out when it holds nothing is not an error (stand-in's own: the documented default is ALL)."""
        with self.lock:
            ctx = self.ctx(rid)
            inst = self.market.instruments.get(name)
            if inst is None:
                raise HTTPError(400, invalid("instrument"))
            body = body if isinstance(body, dict) else {}
            out, plan = {}, []
            for side, sign in (("long", 1), ("short", -1)):
                given = side + "Units" in body
                raw = body.get(side + "Units", "ALL")
                trades = sorted((t for t in self.open_trades(acct, name) if (t["_units"] > 0) == (sign > 0)), key=_num_id)
                held = sum((abs(t["_units"]) for t in trades), D0)
                reason = None
                if raw == "NONE":
                    continue
                if raw == "ALL":
                    take = held
                else:
                    u = dec(raw)
                    if u is None or u <= 0:
                        reason = "UNITS_INVALID"
                    elif places(u.normalize()) > inst.tup:
                        reason = "UNITS_PRECISION_EXCEEDED"
                    elif u > held:
                        reason = "CLOSEOUT_POSITION_UNITS_EXCEED_POSITION_SIZE"
                    take = u
                if reason is None and held == 0:
                    if not given:
                        continue
                    reason = "CLOSEOUT_POSITION_DOESNT_EXIST"
                plan.append((side, sign, raw, trades, take, reason))
            if not plan and body.get("longUnits") == "NONE" and body.get("shortUnits") == "NONE":
                plan.append(("long", 1, "NONE", [], D0, "CLOSEOUT_POSITION_INCOMPLETE_SPECIFICATION"))
            if not plan:
                plan.append(("long", 1, "ALL", [], D0, "CLOSEOUT_POSITION_DOESNT_EXIST"))
            # documented: https://developer.oanda.com/rest-live-v20/position-ep/ longClientExtensions, shortClientExtensions "The client
            # extensions to add to the MarketOrder used to close the long [short] position": read as every order's are (_client_ext),
            # and extensions OANDA would refuse are a reject of that side's order (stand-in's own: the reason is the one an order's would be).
            cexts = {}
            for i, p in enumerate(plan):
                try:
                    cexts[p[0]] = self._client_ext(body.get(p[0] + "ClientExtensions"), "ORDER")
                except Reject as e:
                    if p[5] is None:
                        plan[i] = p[:5] + (e.reason,)
            forced = self.refusal_for("position_close")
            if forced in REJECT_REASONS:                 # a refusal a control asked for: no order is made
                plan = [p[:5] + (p[5] or forced,) for p in plan]
                forced = None
            bad = [p for p in plan if p[5]]
            if bad:
                status = 400
                for side, sign, raw, trades, take, reason in bad:
                    pc = {"instrument": name, "units": raw if isinstance(raw, str) else str(raw)}
                    rtx = self._tx(acct, "MARKET_ORDER_REJECT", ctx, instrument=name, timeInForce="FOK",
                                   positionFill="REDUCE_ONLY", reason="POSITION_CLOSEOUT", rejectReason=reason,
                                   **{side + "PositionCloseout": pc})
                    out[side + "OrderRejectTransaction"] = rtx
                body_out = self.answer(acct, ctx, **out)
                body_out["errorCode"] = bad[0][5]
                body_out["errorMessage"] = reason_text(bad[0][5])
                return status, body_out, {}
            loc = None
            for side, sign, raw, trades, take, _ in plan:
                closes, left = [], take
                for t in trades:
                    if left <= 0:
                        break
                    part = min(abs(t["_units"]), left)
                    closes.append((t, part))
                    left -= part
                s = {"type": "MARKET", "inst": inst, "units": -take * sign, "tif": "FOK", "pfill": "REDUCE_ONLY",
                     side + "PositionCloseout": {"instrument": name, "units": "ALL" if raw == "ALL" else plain(take)}}
                # documented: https://developer.oanda.com/rest-live-v20/transaction-df/ MarketOrderTransaction clientExtensions "Client
                # Extensions to add to the Order (only provided if the Order is being created with client extensions)": on the order's
                # create transaction and the order, as for every other order (s["cext"]), and so its fill's clientOrderID. Put on
                # the order after its transaction is made, the transaction would carry none.
                marks = self.options.get("closeout_extensions", "documented")
                s["cext"] = cexts.get(side) if marks == "documented" else None
                tx = self._tx(acct, "MARKET_ORDER", ctx, **self._create_fields(s, "POSITION_CLOSEOUT"))
                o = self._register(acct, s, tx)
                if marks == "fill" and cexts.get(side):
                    # stand-in's own (a control for tests): the closeout's MARKET_ORDER transaction without the extensions, its fill
                    # still naming the client's id (OrderFillTransaction clientOrderID), as OANDA's record would be if it kept them only
                    # on the order; "none": on neither. Neither is documented; an adapter must not lose a close either way.
                    o["clientExtensions"] = cexts[side]
                if forced is not None:                   # a documented OrderCancelReason: created, then cancelled
                    fill, cancel = None, self._cancel(acct, o, forced, ctx)
                else:
                    fill, cancel = self._execute(acct, o, ctx, immediate=True, closes=closes)
                out[side + "OrderCreateTransaction"] = tx
                if fill is not None:
                    out[side + "OrderFillTransaction"] = fill
                if cancel is not None:
                    out[side + "OrderCancelTransaction"] = cancel
                loc = o["id"]
            hdrs = {"Location": "/v3/accounts/%s/orders/%s" % (acct.id, loc)} if loc else {}
            return 200, self.answer(acct, ctx, **out), hdrs

    # -- the account's own settings and money moves
    def configure(self, acct, body, rid):
        """PATCH .../configuration {alias, marginRate} (documented: https://developer.oanda.com/rest-live-v20/account-ep/; reject reasons transaction-df)."""
        with self.lock:
            ctx = self.ctx(rid)
            body = body if isinstance(body, dict) else {}
            alias, rate = body.get("alias"), body.get("marginRate")
            reason = None
            if alias is None and rate is None:
                reason = "CLIENT_CONFIGURE_DATA_MISSING"
            elif alias is not None and (not isinstance(alias, str) or len(alias) > 100):
                reason = "ALIAS_INVALID"                                 # the length bound is stand-in's own
            r = None
            if reason is None and rate is not None:
                r = dec(rate)
                if r is None or not (0 < r <= 1):
                    reason = "MARGIN_RATE_INVALID"
            if reason is None and r is not None:
                old = acct.margin_rate
                acct.margin_rate = r
                f = self.figures(acct)
                acct.margin_rate = old
                if f["closeout_pct"] >= 1:
                    reason = "MARGIN_RATE_WOULD_TRIGGER_CLOSEOUT"
            if reason is not None:
                rtx = self._tx(acct, "CLIENT_CONFIGURE_REJECT", ctx, alias=alias if isinstance(alias, str) else None,
                               marginRate=rate if isinstance(rate, str) else None, rejectReason=reason)
                return 400, self.reject_body(acct, ctx, "clientConfigureRejectTransaction", rtx, reason), {}
            if alias is not None:
                acct.alias = alias
            if r is not None:
                acct.margin_rate = r
            tx = self._tx(acct, "CLIENT_CONFIGURE", ctx, alias=alias, marginRate=factor(r) if r is not None else None)
            return 200, {"clientConfigureTransaction": tx, "lastTransactionID": str(acct.last_id)}, {}

    def transfer(self, acct, amount, reason="CLIENT_FUNDING", comment=None):
        """TRANSFER_FUNDS (documented: https://developer.oanda.com/rest-live-v20/transaction-df/), as the hub would book a deposit or withdrawal."""
        with self.lock:
            ctx = self.ctx()
            a = dec(amount)
            if a is None or a == 0:
                tx = self._tx(acct, "TRANSFER_FUNDS_REJECT", ctx, amount=str(amount), fundingReason=reason,
                              comment=comment, rejectReason="AMOUNT_INVALID")
                return tx
            if a < 0 and -a > self.figures(acct)["withdrawal"]:
                return self._tx(acct, "TRANSFER_FUNDS_REJECT", ctx, amount=money(a), fundingReason=reason,
                                comment=comment, rejectReason="INSUFFICIENT_FUNDS")
            acct.balance += a
            return self._tx(acct, "TRANSFER_FUNDS", ctx, amount=money(a), fundingReason=reason, comment=comment,
                            accountBalance=money(acct.balance))

    def dividend(self, name, per_unit):
        """DIVIDEND_ADJUSTMENT on every open trade of an instrument (documented: https://developer.oanda.com/rest-live-v20/transaction-df/
        DividendAdjustmentTransaction, OpenTradeDividendAdjustment): longs are paid and shorts charged (stand-in's own)."""
        with self.lock:
            inst = self.market.find(name)
            per = dec(per_unit)
            out = []
            for acct in self.accounts.values():
                trades = self.open_trades(acct, name)
                if not trades:
                    continue
                gain, loss, _ = self.market.factors(inst.quote_ccy, acct.currency)
                ctx, items, total_q, total = self.ctx(), [], D0, D0
                for t in sorted(trades, key=_num_id):
                    qa = per * t["_units"]
                    ha = (qa * (gain if qa >= 0 else loss)).quantize(Q4, rounding=ROUND_HALF_UP)
                    t["_div"] += ha
                    t["dividendAdjustment"] = money(t["_div"])
                    self._agg(acct, name)["long" if t["_units"] > 0 else "short"]["dividendAdjustment"] += ha
                    items.append({"tradeID": t["id"], "dividendAdjustment": money(ha), "quoteDividendAdjustment": factor(qa)})
                    total_q, total = total_q + qa, total + ha
                acct.balance += total
                acct.dividend += total
                out.append(self._tx(acct, "DIVIDEND_ADJUSTMENT", ctx, instrument=name, dividendAdjustment=money(total),
                                    quoteDividendAdjustment=factor(total_q),
                                    homeConversionFactors={"gainQuoteHome": {"factor": factor(gain)},
                                                           "lossQuoteHome": {"factor": factor(loss)}},
                                    accountBalance=money(acct.balance), openTradeDividendAdjustments=items))
            return out

    # -- the engine: resting orders, trailing stops, expiry, financing, margin
    def live(self):
        """{instrument: [(account, order)]} of every pending order, and the instruments with open trades."""
        orders, trades = collections.defaultdict(list), set()
        for acct in self.accounts.values():
            for o in acct.orders.values():
                if o["state"] == "PENDING":
                    orders[o.get("_inst") or acct.trades[o["_trade"]]["instrument"]].append((acct, o))
            for t in self.open_trades(acct):
                trades.add(t["instrument"])
        return orders, trades

    def _evaluate(self, name, q, pairs):
        """Look at every pending order of one instrument against one price; what fills is stamped at that tick."""
        for acct, o in sorted(pairs, key=lambda p: _num_id(p[1])):
            if o["state"] != "PENDING":
                continue
            ctx = self.ctx(ns=self.clock.stamp_at(q.ms * MS_NS))
            if o["type"] in ENTRY:
                self._try_entry(acct, o, ctx, q=q)
            else:
                self._try_dependent(acct, o, ctx, q=q)

    def tick(self):
        """One pass of the engine at the server's now."""
        with self.lock:
            self._scan(self.last_scan, self.clock.now_ms())

    def look_now(self, at):
        """A control has just changed a price or a market at `at`: everything due before it, then every pending order
        against the prices at that instant (the engine's last look may already have been at that millisecond)."""
        with self.lock:
            self._scan(self.last_scan, at)
            orders, _ = self.live()
            for name, pairs in orders.items():
                q = self.market.quote(name, at)
                if q.tradeable:
                    self._evaluate(name, q, pairs)
            self._margin()
            self.last_scan = max(self.last_scan, at)

    def _could_trigger(self, inst, pairs, a, b):
        """Whether any order of an instrument could trigger in (a, b], judged from the span's bid and ask range; when
        none can, a trailing stop is moved to where the span's best price would have taken it."""
        got = self.market.candle(inst, a + 1, b + 1, ("B", "A"), {"B": D0, "A": D0})
        if got is None:
            return False
        bid, ask = got[0]["B"], got[0]["A"]
        lo, hi = min(bid[2], ask[2]), max(bid[1], ask[1])
        moves = []
        for acct, o in pairs:
            if o["state"] != "PENDING":
                continue
            typ, p = o["type"], o.get("_price")
            if typ in ENTRY:
                buy = o["_units"] > 0
                if typ == "LIMIT":
                    hit = lo <= p if buy else hi >= p
                elif typ == "STOP":
                    hit = hi >= p if buy else lo <= p
                else:
                    hit = lo <= p if p <= o["_initial"] else hi >= p
            else:
                t = acct.trades.get(o["_trade"])
                if t is None or t["state"] != "OPEN":
                    continue
                long_trade = t["_units"] > 0
                if typ == "TAKE_PROFIT":
                    hit = hi >= p if long_trade else lo <= p
                elif typ == "STOP_LOSS":
                    hit = lo <= p if long_trade else hi >= p
                else:
                    d = o["_distance"]
                    v = max(o["_tsv"], bid[1] - d) if long_trade else min(o["_tsv"], ask[2] + d)
                    hit = lo <= v if long_trade else hi >= v
                    moves.append((o, v, inst.dp))
            if hit:
                return True
        for o, v, dp in moves:
            o["_tsv"], o["trailingStopValue"] = v, pstr(v, dp)
        return False

    CHUNK_MS = 600000

    def _scan(self, a, b):
        """Everything due in (a, b], in ten-minute steps: each instrument's ticks against its pending orders (skipped
        where the step's price range cannot reach any of them), expiries, financing at 17:00 New York, the margin
        check."""
        t = a
        while t < b:
            c = min(b, t + self.CHUNK_MS, max(t + 1, self.next_finance))
            self._expire(t)
            orders, _ = self.live()
            for name, pairs in orders.items():
                inst = self.market.find(name)
                if c - t > 2000 and not self._could_trigger(inst, pairs, t, c):
                    continue
                for q in self.ticks(inst, t + 1, c + 1):
                    self._evaluate(name, q, pairs)
            self._expire(c)
            while self.next_finance <= c:
                self._finance(self.next_finance)
                self.next_finance = next_ny_five(self.next_finance + 1)
            self._margin()
            t = c
        self.last_scan = max(self.last_scan, b)

    def ticks(self, inst, a, b):
        """Every tick seen in [a, b) as a Quote (with what controls put over the walk)."""
        for s, e, tick, rec in self.market._pieces(inst, a, b):
            if tick and not inst.path.on_grid(s):
                yield Quote(inst, s, self.market._value(inst, s, rec), self.market._spread(inst, rec), True)
            for t, _ in inst.path.ticks(s, e):
                yield Quote(inst, t, self.market._value(inst, t, rec), self.market._spread(inst, rec), True)

    def _expire(self, now):
        for acct in self.accounts.values():
            for o in list(acct.orders.values()):
                if o["state"] == "PENDING" and o.get("_gtd") is not None and o["_gtd"] <= now * MS_NS:
                    # documented: https://developer.oanda.com/rest-live-v20/order-df/ TimeInForce GTD "will be cancelled at the provided time", GFD "at 5pm New York time";
                    # OrderCancelReason TIME_IN_FORCE_EXPIRED
                    self._cancel(acct, o, "TIME_IN_FORCE_EXPIRED", self.ctx(ns=self.clock.stamp_at(o["_gtd"])))

    def _finance(self, at):
        """DAILY_FINANCING at 17:00 New York (documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf "credited/debited to the account daily"
        at the "EOD Time ... 5pm ET"; transaction-df DailyFinancingTransaction, PositionFinancing, OpenTradeFinancing).
        Amount: units x mid x annual rate / 365 x days charged that weekday (stand-in's own arithmetic)."""
        wday = NEW_YORK.local_date(at // 1000).weekday()
        if wday >= 5:
            return                       # stand-in's own: nothing is posted on a day that charges no days (Sat, Sun)
        for acct in self.accounts.values():
            trades = [t for t in self.open_trades(acct) if t["openTime"].ns < at * MS_NS]
            if not trades:
                continue
            ctx = self.ctx(ns=self.clock.stamp_at(at * MS_NS))
            by_inst, total = collections.OrderedDict(), D0
            for t in sorted(trades, key=_num_id):
                by_inst.setdefault(t["instrument"], []).append(t)
            pfs = []
            for name, ts in by_inst.items():
                inst = self.market.find(name)
                spec = inst.spec
                days = 0 if wday >= 5 else (3 if wday == spec["triple"] else 1)
                mid = self.market.mid(name)
                gain, loss, _ = self.market.factors(inst.quote_ccy, acct.currency)
                items, sub_q, sub = [], D0, D0
                for t in ts:
                    rate = Decimal(spec["long"] if t["_units"] > 0 else spec["short"])
                    qf = abs(t["_units"]) * mid * rate * days / 365
                    hf = (qf * (gain if qf >= 0 else loss)).quantize(Q4, rounding=ROUND_HALF_UP)
                    t["_financing"] += hf
                    t["financing"] = money(t["_financing"])
                    self._agg(acct, name)["long" if t["_units"] > 0 else "short"]["financing"] += hf
                    items.append({"tradeID": t["id"], "financing": money(hf), "baseFinancing": factor(qf / mid),
                                  "quoteFinancing": factor(qf), "financingRate": factor(rate)})
                    sub_q, sub = sub_q + qf, sub + hf
                pfs.append({"instrument": name, "financing": money(sub), "baseFinancing": factor(sub_q / mid),
                            "quoteFinancing": factor(sub_q),
                            "homeConversionFactors": {"gainQuoteHome": {"factor": factor(gain)},
                                                      "lossQuoteHome": {"factor": factor(loss)}},
                            "openTradeFinancings": items, "accountFinancingMode": "DAILY"})
                total += sub
            acct.balance += total
            acct.financing += total
            self._tx(acct, "DAILY_FINANCING", ctx, financing=money(total), accountBalance=money(acct.balance),
                     accountFinancingMode="DAILY", positionFinancings=pfs)

    def _margin(self):
        """documented: https://oanda.com/assets/documents/1856/OEL-sub-accounts-notable-differences-2024.pdf: a margin call at "Margin Close-Out Percentage >= 50%", a margin close-out at
        ">= 100%", which "triggers full liquidation except for positions where the market in the underlying Instrument is
        closed or halted"; transaction-df MARGIN_CALL_ENTER, MARGIN_CALL_EXIT, MarketOrderMarginCloseout."""
        for acct in self.accounts.values():
            if not self.open_trades(acct):
                if acct.margin_call:
                    acct.margin_call = False
                    self._tx(acct, "MARGIN_CALL_EXIT", self.ctx())
                continue
            f = self.figures(acct)
            pct = f["closeout_pct"]
            if pct >= 1:
                for name in sorted({t["instrument"] for t in self.open_trades(acct)}):
                    inst = self.market.find(name)
                    q = self.market.quote(inst)
                    if not q.tradeable:
                        continue
                    for side, sign in (("long", 1), ("short", -1)):
                        trades = sorted((t for t in self.open_trades(acct, name) if (t["_units"] > 0) == (sign > 0)),
                                        key=_num_id)
                        if not trades:
                            continue
                        held = sum((abs(t["_units"]) for t in trades), D0)
                        ctx = self.ctx()
                        s = {"type": "MARKET", "inst": inst, "units": -held * sign, "tif": "FOK", "pfill": "REDUCE_ONLY",
                             "marginCloseout": {"reason": "MARGIN_CHECK_VIOLATION"}}
                        tx = self._tx(acct, "MARKET_ORDER", ctx, **self._create_fields(s, "MARGIN_CLOSEOUT"))
                        o = self._register(acct, s, tx)
                        self._execute(acct, o, ctx, immediate=True, q=q, closes=[(t, abs(t["_units"])) for t in trades])
                f = self.figures(acct)
                pct = f["closeout_pct"]
            if pct >= Decimal("0.5") and not acct.margin_call:
                acct.margin_call, acct.margin_call_ns = True, self.clock.now_ns()
                self._tx(acct, "MARGIN_CALL_ENTER", self.ctx())
            elif pct < Decimal("0.5") and acct.margin_call:
                acct.margin_call = False
                self._tx(acct, "MARGIN_CALL_EXIT", self.ctx())

    # -- transaction history (documented: https://developer.oanda.com/rest-live-v20/transaction-ep/)
    @staticmethod
    def type_filter(raw):
        if raw in (None, ""):
            return None
        names = [x for x in raw.split(",") if x]
        if not names or any(x not in TRANSACTION_FILTERS for x in names):
            raise HTTPError(400, invalid("type"))
        wanted = set()
        for x in names:
            wanted |= FILTER_GROUPS.get(x, {x})
        return wanted

    def changes(self, acct, since):
        """GET .../changes?sinceTransactionID= (documented: https://developer.oanda.com/rest-live-v20/account-ep/, account-df AccountChanges and
        AccountChangesState)."""
        with self.lock:
            out = {}
            if since is not None:
                s = int(since)
                orders = list(acct.orders.values())
                trades = list(acct.trades.values())
                touched = {t["instrument"] for t in trades
                           if int(t["_open_tid"]) > s or int(t.get("_reduce_tid") or 0) > s or int(t.get("_close_tid") or 0) > s}
                out["changes"] = {
                    "ordersCreated": [self.order_json(o) for o in orders if int(o["id"]) > s],
                    "ordersCancelled": [self.order_json(o) for o in orders if o["state"] == "CANCELLED"
                                        and int(o.get("cancellingTransactionID", 0)) > s],
                    "ordersFilled": [self.order_json(o) for o in orders if o["state"] == "FILLED"
                                     and int(o.get("fillingTransactionID", 0)) > s],
                    "ordersTriggered": [],
                    "tradesOpened": [self.trade_json(acct, t, True) for t in trades if int(t["_open_tid"]) > s],
                    "tradesReduced": [self.trade_json(acct, t, True) for t in trades if t["state"] == "OPEN"
                                      and int(t.get("_reduce_tid") or 0) > s],
                    "tradesClosed": [self.trade_json(acct, t, True) for t in trades if int(t.get("_close_tid") or 0) > s],
                    "positions": [self.position_json(acct, n) for n in sorted(touched)],
                    "transactions": [tx for tx in acct.txs if int(tx["id"]) > s]}
            f = self.figures(acct)
            state = {"unrealizedPL": money(f["upl"]), "NAV": money(f["nav"]), "marginUsed": money(f["margin"]),
                     "marginAvailable": money(f["avail"]), "positionValue": money(f["value"]),
                     "marginCloseoutUnrealizedPL": money(f["upl_mid"]), "marginCloseoutNAV": money(f["nav_mid"]),
                     "marginCloseoutMarginUsed": money(f["closeout_used"]),
                     "marginCloseoutPercent": factor(f["closeout_pct"].quantize(Decimal("0.00001"))),
                     "marginCloseoutPositionValue": factor(f["value"].quantize(Decimal("0.00001"))),
                     "withdrawalLimit": money(f["withdrawal"]), "marginCallMarginUsed": money(f["margin"]),
                     "marginCallPercent": factor(f["call_pct"].quantize(Decimal("0.00001"))),
                     "balance": money(acct.balance), "pl": money(acct.pl), "resettablePL": money(acct.resettable),
                     "financing": money(acct.financing), "commission": money(acct.commission),
                     "dividendAdjustment": money(acct.dividend), "guaranteedExecutionFees": money(acct.gef),
                     "marginCallExtensionCount": acct.mc_extensions,
                     "orders": [{"id": o["id"], "trailingStopValue": o["trailingStopValue"],
                                 "triggerDistance": pstr(abs(self._exit_price(acct, o) - o["_tsv"]),
                                                         self.market.find(acct.trades[o["_trade"]]["instrument"]).dp),
                                 "isTriggerDistanceExact": True}
                                for o in acct.orders.values() if o["state"] == "PENDING" and o["type"] == "TRAILING_STOP_LOSS"],
                     "trades": [{"id": t["id"], "unrealizedPL": money(self.trade_upl(acct, t)),
                                 "marginUsed": self.trade_json(acct, t)["marginUsed"]} for t in self.open_trades(acct)],
                     "positions": []}
            for name in sorted({t["instrument"] for t in self.open_trades(acct)}):
                p = self.position_json(acct, name)
                state["positions"].append({"instrument": name, "netUnrealizedPL": p["unrealizedPL"],
                                           "longUnrealizedPL": p["long"]["unrealizedPL"],
                                           "shortUnrealizedPL": p["short"]["unrealizedPL"], "marginUsed": p["marginUsed"]})
            if acct.margin_call:
                state["marginCallEnterTime"] = Stamp(acct.margin_call_ns)
            out["state"] = state
            out["lastTransactionID"] = str(acct.last_id)
            return out

    def _exit_price(self, acct, o):
        t = acct.trades[o["_trade"]]
        q = self.market.quote(t["instrument"])
        return q.bid if t["_units"] > 0 else q.ask

    def price_json(self, q, fill=False, home=None):
        """A ClientPrice (documented: https://developer.oanda.com/rest-live-v20/pricing-df/), fields in the documented order; in a fill, the documented example's
        keys (bids, asks, closeoutBid, closeoutAsk, timestamp)."""
        inst = q.inst
        levels = self.market.buckets(inst)
        bids = [{"price": pstr(q.bid - inst.tick * off, inst.dp), "liquidity": int(size)} for size, off in levels]
        asks = [{"price": pstr(q.ask + inst.tick * off, inst.dp), "liquidity": int(size)} for size, off in levels]
        depth = len(inst.spec["ladder"])
        # stand-in's own: the closeout prices sit one tick beyond the deepest bucket (documented only as the price used
        # "when a bid is required to closeout a Position ... yet there is no bid liquidity")
        cb, ca = pstr(q.bid - inst.tick * depth, inst.dp), pstr(q.ask + inst.tick * depth, inst.dp)
        if fill:
            return {"bids": bids, "asks": asks, "closeoutBid": cb, "closeoutAsk": ca, "timestamp": Stamp(q.ms * MS_NS)}
        return {"type": "PRICE", "instrument": inst.name, "time": Stamp(q.ms * MS_NS),
                "status": "tradeable" if q.tradeable else "non-tradeable", "tradeable": q.tradeable,
                "bids": bids, "asks": asks, "closeoutBid": cb, "closeoutAsk": ca}


def _num_id(x):
    return int(x["id"])


# ---------------------------------------------------------------- the HTTP side

def render(obj, fmt):
    """A body as JSON can carry it: DateTimes in the format asked for, internal fields dropped."""
    if isinstance(obj, dict):
        return {k: render(v, fmt) for k, v in obj.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, (list, tuple)):
        return [render(v, fmt) for v in obj]
    if isinstance(obj, Stamp):
        return obj.render(fmt)
    if isinstance(obj, Decimal):
        return plain(obj)
    return obj


# documented: https://developer.oanda.com/rest-live-v20/account-ep/ example response headers (and every -ep page's)
CORS = {"Access-Control-Allow-Headers": "Authorization, Content-Type, Accept-Datetime-Format",
        "Access-Control-Allow-Methods": "PUT, PATCH, POST, GET, OPTIONS, DELETE", "Access-Control-Allow-Origin": "*"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "fake-oanda", ""

    def setup(self):
        # Headers and body leave in two writes; without this, Nagle's algorithm against the client's delayed ACK holds
        # the body back about 40 ms on every keep-alive request (stand-in's own: the real hosts are not this slow).
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().setup()

    def log_message(self, *args):
        pass

    def _go(self):
        self.server.fake._handle(self, self.server.kind)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _go


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A client that went away is not an error of the stand-in; anything else is printed."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Stream:
    """One open stream: its socket, what it carries, and a way to end it from outside."""

    def __init__(self, kind, account, token, handler):
        self.kind, self.account, self.token, self.h = kind, account, token, handler
        self.closed, self.clean = threading.Event(), False
        self.queue = collections.deque()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.opened = time.monotonic()

    def write(self, data):
        with self.lock:
            self.h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.h.wfile.flush()

    def close(self, clean=False):
        self.clean = clean
        self.closed.set()
        self.wake.set()
        if not clean:
            try:
                self.h.connection.shutdown(socket.SHUT_RDWR)        # the socket simply ends, mid-stream
            except OSError:
                pass


class FakeOanda:
    """The stand-in. start() serves REST and streams on 127.0.0.1 (two ports; 0 picks free ones); stop() ends it.

    rate_limit, stream_limit and new_connections_per_second are OANDA's documented per-address limits (development
    guide: 120 requests a second, 20 active streams, 2 new connections a second); None switches one off. heartbeat is the
    documented 5 s (pricing-ep, transaction-ep); a test may shorten it."""

    def __init__(self, seed=7, market="open", division="uk", port=0, stream_port=0, rate_limit=120, stream_limit=20,
                 new_connections_per_second=2, heartbeat=5.0, stream_max_age=None, engine_interval=0.05):
        if market not in ("open", "closed", "auto"):
            raise ValueError("market must be open, closed or auto")
        if division not in ("uk", "us"):
            raise ValueError("division must be uk or us")
        self.seed, self.mode0, self.division = seed, market, division
        self.rate_limit, self.stream_limit, self.conn_limit = rate_limit, stream_limit, new_connections_per_second
        self.heartbeat, self.stream_max_age, self.engine_interval = float(heartbeat), stream_max_age, engine_interval
        self._guard = threading.Lock()
        self._streams = set()
        self.clock = Clock()
        self.market = Market(self.clock, seed, market)
        self.broker = Broker(self.market, self.clock, division, on_tx=self._publish)
        self._ports = (port, stream_port)
        self._servers, self._threads, self._engine = [], [], None
        self._stop = threading.Event()
        self._log, self._seq, self._rid = [], 0, 0
        self._faults, self._connects = [], {"count": 0, "status": 401, "where": "stream"}
        self._redirects = []
        self._req_times, self._conn_times = collections.deque(), collections.deque()
        self.options = {"foreign_account_status": 401}
        self._new_tokens()

    # -- credentials (documented: https://developer.oanda.com/rest-live-v20/authentication/: one personal access token "Authorization: Bearer <token>"; it "will
    #    grant access to all of your sub-accounts"; revoking it ends it; a sub-account added later needs a new token, per
    #    the help page the 2026-11-11 research quoted)
    def _make_token(self, generation):
        h = hashlib.sha256(("fake-oanda-token|%s|%d" % (self.seed, generation)).encode()).hexdigest()
        return h[:32] + "-" + h[32:64]

    def _new_tokens(self):
        self.generation = 0
        self.token0 = self._make_token(0)
        self._tokens = {self.token0: 0}

    @property
    def token(self):
        """The token that works now (None after revoke_token)."""
        return next(iter(self._tokens), None)

    @property
    def credentials(self):
        return {"token": self.token}

    def accounts_for(self, token):
        gen = self._tokens.get(token)
        if gen is None:
            return []
        return [a for a in self.broker.accounts.values() if a.generation <= gen]

    @property
    def account_ids(self):
        return [a.id for a in self.accounts_for(self.token)]

    # -- life
    def start(self):
        for i, kind in enumerate(("rest", "stream")):
            srv = _Server(("127.0.0.1", self._ports[i]), _Handler)
            srv.fake, srv.kind = self, kind
            th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                                  name="fake-oanda-" + kind)
            th.start()
            self._servers.append(srv)
            self._threads.append(th)
        self._stop.clear()
        self._engine = threading.Thread(target=self._run_engine, daemon=True, name="fake-oanda-engine")
        self._engine.start()
        return self

    def stop(self):
        if not self._servers:
            return
        self._stop.set()
        self.drop_streams(clean=False)
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        for th in self._threads:
            th.join(5)
        if self._engine is not None:
            self._engine.join(5)
        self._servers, self._threads, self._engine = [], [], None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _run_engine(self):
        while not self._stop.wait(self.engine_interval):
            try:
                self.broker.tick()
            except Exception as e:                                      # pragma: no cover - kept visible, never fatal
                sys.stderr.write("fake-oanda engine: %r\n" % (e,))

    @property
    def port(self):
        return self._servers[0].server_address[1]

    @property
    def stream_port(self):
        return self._servers[1].server_address[1]

    @property
    def rest_url(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def stream_url(self):
        return "http://127.0.0.1:%d" % self.stream_port

    # -- controls (each is also POST /_control/<name>)
    def set_price(self, instrument, bid=None, ask=None, mid=None, spread=None, walk=None, jump_pct=None, ramp_to=None,
                  seconds=None):
        with self.broker.lock:
            # the new price starts now and the engine looks at it at once, so an order it reaches is filled before this
            # returns
            at = self.clock.now_ms()
            out = self.market.control_price(instrument, bid, ask, mid, spread, walk, jump_pct, ramp_to, seconds, at=at)
            self.broker.look_now(at)
        return out

    def set_market(self, state, instrument=None):
        """open / closed / auto (OANDA's documented hours), for one instrument or all."""
        with self.broker.lock:
            at = self.market.set_mode(state, [instrument] if instrument else None, at=self.clock.now_ms())
            self.broker.look_now(at)
        return {"state": state, "instrument": instrument, "at": rfc3339(at * MS_NS)}

    def advance(self, seconds):
        """Move the server's clock forward; the engine looks at everything due in between (resting orders against the
        ticks, expiries, financing at 17:00 New York, margin)."""
        span = int(float(seconds) * NS)
        if span < 0:
            raise ValueError("the clock only moves forward; reset puts it back")
        with self.broker.lock:
            self.clock.offset_ns += span
            self.broker.tick()
        return {"now": rfc3339(self.clock.now_ns())}

    def set_clock(self, now):
        return self.advance((parse_time(now) - self.clock.now_ns()) / NS)

    def set_liquidity(self, instrument, units, count=None):
        """Cap the liquidity of an instrument's book (both sides) at `units`, for the next `count` fill attempts or until
        lifted (units None). A market FOK larger than it is killed INSUFFICIENT_LIQUIDITY, a market IOC fills what there
        is and the rest is cancelled, a triggered resting order waits (documented: https://legal.oanda.com/oel/execution_policy_oel/en Annex 1)."""
        inst = self.market.find(instrument)
        with self.broker.lock:
            if units is None:
                inst.cap, inst.cap_count = None, None
            else:
                u = dec(units)
                if u is None or u < 0:
                    raise ValueError("units must be >= 0")
                inst.cap, inst.cap_count = u, (int(count) if count is not None else None)
        return {"instrument": instrument, "units": None if inst.cap is None else plain(inst.cap), "count": inst.cap_count}

    def next_fill(self, instrument, units):
        """The next order to fill on this instrument finds only `units` in the book: the documented way an OANDA order
        fills in part (an IOC market order: part filled, the rest cancelled INSUFFICIENT_LIQUIDITY)."""
        return self.set_liquidity(instrument, units, count=1)

    def refuse_next(self, reason, count=1, kind="order"):
        """The next `count` requests of a kind (order, replace, cancel, trade_close, position_close, any) are refused
        with a documented reason: a TransactionRejectReason gives a 400 and a *_REJECT transaction; an OrderCancelReason
        gives the order created and cancelled at once."""
        if reason not in REJECT_REASONS and reason not in CANCEL_REASONS:
            raise ValueError("not a documented reject or cancel reason: %r" % (reason,))
        if kind not in ("order", "replace", "cancel", "trade_close", "position_close", "any"):
            raise ValueError("kind must be order, replace, cancel, trade_close, position_close or any")
        if kind == "cancel" and reason not in REJECT_REASONS:
            raise ValueError("a cancel request is refused with a TransactionRejectReason, not %r" % (reason,))
        with self.broker.lock:
            self.broker.refusals.append({"reason": reason, "count": int(count), "kind": kind})
        return {"refusals": [dict(r) for r in self.broker.refusals if r["count"] > 0]}

    def revoke_token(self):
        """The token stops working at once (documented: https://developer.oanda.com/rest-live-v20/authentication/, a token can be revoked); its open streams end
        (stand-in's own)."""
        with self._guard:
            gone = list(self._tokens)
            self._tokens = {}
        for s in self._stream_list():
            if s.token in gone:
                s.close(clean=False)
        return {"token": None}

    def rotate_token(self):
        """Revoke and generate: a new token, which also sees sub-accounts added since the last one."""
        self.revoke_token()
        with self._guard:
            self.generation += 1
            tok = self._make_token(self.generation)
            self._tokens = {tok: self.generation}
        return {"token": tok}

    def restore_token(self):
        with self._guard:
            self._new_tokens()
        return {"token": self.token}

    def refuse_connects(self, count=1, status=401, where="stream"):
        """The next `count` connects are refused though the token is good (measured on a live account: a 401 on
        a reconnect a second after OANDA closed the streams, with a valid token)."""
        if where not in ("stream", "rest", "any"):
            raise ValueError("where must be stream, rest or any")
        with self._guard:
            self._connects = {"count": int(count), "status": int(status), "where": where}
        return dict(self._connects)

    def drop_streams(self, clean=False, kind=None):
        """End open streams from the server's side (measured on a live account: OANDA closes streams every few
        hours, sometimes all of an account's at once). clean=True ends the chunked body properly; else the socket just
        ends."""
        n = 0
        for s in self._stream_list():
            if kind is None or s.kind == kind:
                s.close(clean)
                n += 1
        return n

    def fail_next(self, status=429, count=1, path=None):
        """The next `count` REST requests (to paths starting with `path`) are answered with this status."""
        if int(status) not in (429, 500, 502, 503, 504):
            raise ValueError("status must be 429, 500, 502, 503 or 504")
        with self._guard:
            self._faults.append({"status": int(status), "count": int(count), "path": path})
        return {"faults": [dict(f) for f in self._faults if f["count"] > 0]}

    def redirect_next(self, count=1, path=None, method=None, status=307, location=None, kind="rest"):
        """The next `count` requests on the REST host (kind "stream": on the streaming host), to paths starting with
        `path`, of `method`, are answered `status` with a Location: `location`, or by default the same request on that
        host. None is carried out, and no stream is opened for it. measured on a live account (OANDA's
        live API, 2026-11-15: two account summary reads answered HTTP 307, and a read a minute later did not); where its
        Location pointed was not recorded, so the default is stand-in's own, and so is a stream's redirect."""
        if int(status) not in (301, 302, 303, 307, 308):
            raise ValueError("status must be 301, 302, 303, 307 or 308")
        if int(count) < 1:
            raise ValueError("count must be 1 or more")
        if kind not in ("rest", "stream"):
            raise ValueError("kind must be rest or stream")
        with self._guard:
            self._redirects.append({"status": int(status), "count": int(count), "path": path,
                                    "method": method.upper() if method else None, "location": location,
                                    "kind": kind})
        return {"redirects": [dict(r) for r in self._redirects if r["count"] > 0]}

    def transfer(self, account, amount, reason="CLIENT_FUNDING", comment=None):
        acct = self.broker.accounts[account]
        return render(self.broker.transfer(acct, amount, reason, comment), "RFC3339")

    def dividend(self, instrument, per_unit):
        return render(self.broker.dividend(instrument, per_unit), "RFC3339")

    def add_account(self, currency="GBP", hedging=False, alias=None):
        """A sub-account opened in the hub now: the current token does not see it until rotate_token()."""
        return {"id": self.broker.add_account(currency, hedging, alias, generation=self.generation + 1)}

    def set_options(self, **kw):
        """foreign_account_status: 401 (the troubleshooting page's 'Invalid AccountID') or 403; client_id_lookup:
        "pending" (an "@id" finds only pending orders and open trades) or "any"; closeout_extensions: where a position
        close's client extensions are recorded, "documented" (its order's create transaction, the order, its fill's
        clientOrderID), "fill" (the order and its fill only) or "none"."""
        with self.broker.lock:
            for k, v in kw.items():
                if k == "foreign_account_status" and int(v) in (401, 403):
                    self.options[k] = int(v)
                elif k == "client_id_lookup" and v in ("pending", "any"):
                    self.broker.options[k] = v
                elif k == "closeout_extensions" and v in ("documented", "fill", "none"):
                    self.broker.options[k] = v
                else:
                    raise ValueError("no such option or value: %s=%r" % (k, v))
        return self.state()["options"]

    def reset(self):
        """Accounts, prices, markets, clock, tokens and every control as at the start; open streams end (the
        transaction ids start again). The request log is kept."""
        self.drop_streams(clean=False)
        with self.broker.lock:
            self.clock.offset_ns = 0
            self.clock._last = 0                 # the accounts start again, so may their transactions' times
            self.market.reset()
            self.broker.reset()
            self.broker.options = {"client_id_lookup": "pending", "closeout_extensions": "documented"}
            self.options = {"foreign_account_status": 401}
        with self._guard:
            self._faults, self._connects = [], {"count": 0, "status": 401, "where": "stream"}
            self._redirects = []
            self._req_times.clear()
            self._conn_times.clear()
            self._new_tokens()

    def settle(self, timeout=5.0):
        """Run the engine now; returns when it has looked at everything due."""
        self.broker.tick()
        return True

    def requests(self, clear=False):
        with self._guard:
            out = list(self._log)
            if clear:
                self._log = []
            return out

    def transactions(self, account):
        with self.broker.lock:
            return render(self.broker.accounts[account].txs, "RFC3339")

    def violations(self):
        with self.broker.lock:
            return list(self.broker.violations)

    def state(self):
        with self.broker.lock:
            m = self.market
            return {"now": rfc3339(self.clock.now_ns()), "clock_offset_s": self.clock.offset_ns / NS,
                    "modes": {n: i.modes[-1][1] for n, i in m.instruments.items()},
                    "liquidity": {n: (plain(i.cap), i.cap_count) for n, i in m.instruments.items() if i.cap is not None},
                    "refusals": [dict(r) for r in self.broker.refusals if r["count"] > 0],
                    "faults": [dict(f) for f in self._faults if f["count"] > 0], "connects": dict(self._connects),
                    "redirects": [dict(r) for r in self._redirects if r["count"] > 0],
                    "options": dict(self.options, **self.broker.options), "token_generation": self.generation,
                    "accounts": list(self.broker.accounts), "streams": len(self._streams),
                    "violations": len(self.broker.violations)}

    def _control(self, method, name, q, body):
        b = body
        if name == "price":
            return self.set_price(b["instrument"], b.get("bid"), b.get("ask"), b.get("mid"), b.get("spread"),
                                  b.get("walk"), b.get("jump_pct"), b.get("ramp_to"), b.get("seconds"))
        if name == "market":
            return self.set_market(b["state"], b.get("instrument"))
        if name == "clock":
            if b.get("reset"):
                with self.broker.lock:
                    self.clock.offset_ns = 0
                return {"now": rfc3339(self.clock.now_ns())}
            return self.set_clock(b["now"]) if "now" in b else self.advance(b["advance_s"])
        if name == "liquidity":
            return self.set_liquidity(b["instrument"], b.get("units"), b.get("count"))
        if name == "refuse":
            return self.refuse_next(b["reason"], b.get("count", 1), b.get("kind", "order"))
        if name == "token":
            act = b.get("action")
            if act not in ("revoke", "rotate", "restore"):
                raise ValueError("action must be revoke, rotate or restore")
            return getattr(self, act + "_token")()
        if name == "connects":
            return self.refuse_connects(b.get("count", 1), b.get("status", 401), b.get("where", "stream"))
        if name == "drop_streams":
            return {"dropped": self.drop_streams(bool(b.get("clean")), b.get("kind"))}
        if name == "fail":
            return self.fail_next(b.get("status", 429), b.get("count", 1), b.get("path"))
        if name == "redirect":
            return self.redirect_next(b.get("count", 1), b.get("path"), b.get("method"), b.get("status", 307),
                                      b.get("location"), b.get("kind", "rest"))
        if name == "transfer":
            return self.transfer(b["account"], b["amount"], b.get("reason", "CLIENT_FUNDING"), b.get("comment"))
        if name == "dividend":
            return {"transactions": self.dividend(b["instrument"], b["per_unit"])}
        if name == "add_account":
            return self.add_account(b.get("currency", "GBP"), bool(b.get("hedging")), b.get("alias"))
        if name == "options":
            return self.set_options(**b)
        if name == "reset":
            self.reset()
            return {"reset": True}
        if name == "settle":
            return {"settled": self.settle(float(b.get("timeout_s", 5)))}
        if name == "requests":
            return {"requests": self.requests(clear=method == "DELETE" or q.get("clear") in ("1", "true"))}
        if name == "state":
            return self.state()
        if name == "transactions":
            return {"transactions": self.transactions(q.get("account") or b.get("account"))}
        if name == "violations":
            return {"violations": self.violations()}
        raise KeyError("no such control: " + name)

    # -- one request
    def _stream_list(self):
        with self._guard:
            return list(self._streams)

    def _record(self, kind, h, method, path, query, status, authorized):
        with self._guard:
            self._seq += 1
            # the token is never kept: only whether the request carried a good one
            self._log.append({"seq": self._seq, "at": rfc3339(time.time_ns()), "host": kind, "method": method,
                              "path": path, "query": query, "status": status,
                              "user_agent": h.headers.get("User-Agent"),
                              "accept_datetime_format": h.headers.get("Accept-Datetime-Format"),
                              "client_request_id": h.headers.get("ClientRequestID"),
                              "content_type": h.headers.get("Content-Type"), "authorized": authorized})

    def _connection_ok(self):
        """documented: https://developer.oanda.com/rest-live-v20/development-guide/, "Client is allowed to make no more than 2 new connections per second. Excess
        connections will be rejected." The rejection is the connection ended unanswered (stand-in's own form)."""
        if not self.conn_limit:
            return True
        now = time.monotonic()
        with self._guard:
            while self._conn_times and now - self._conn_times[0] >= 1.0:
                self._conn_times.popleft()
            if len(self._conn_times) >= self.conn_limit:
                return False
            self._conn_times.append(now)
            return True

    def _rate_ok(self):
        """documented: https://developer.oanda.com/rest-live-v20/development-guide/, REST "120 requests per second. Excess requests will receive HTTP 429 error."
        A sliding one-second window over every REST request from this address is stand-in's own."""
        if not self.rate_limit:
            return True
        now = time.monotonic()
        with self._guard:
            while self._req_times and now - self._req_times[0] >= 1.0:
                self._req_times.popleft()
            if len(self._req_times) >= self.rate_limit:
                return False
            self._req_times.append(now)
            return True

    def _token_of(self, headers):
        """documented: https://developer.oanda.com/rest-live-v20/authentication/ "Authorization: Bearer <token>"; troubleshooting-errors: 401 "Insufficient
        authorization to perform request." for a missing or bad token."""
        scheme, _, value = (headers.get("Authorization") or "").partition(" ")
        token = value.strip()
        with self._guard:
            ok = scheme.lower() == "bearer" and token in self._tokens
        if not ok:
            raise HTTPError(401, MSG_UNAUTHORIZED)
        return token

    def _account(self, aid, token):
        """documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/, "Invalid AccountID": "The provided request was forbidden." under 401 (the
        status is an option, 403 the other reading). A malformed id answered 400 is stand-in's own."""
        if not ACCOUNT_ID.fullmatch(aid or ""):
            raise HTTPError(400, invalid("accountID"))
        acct = self.broker.accounts.get(aid)
        if acct is None or acct not in self.accounts_for(token):
            raise HTTPError(self.options["foreign_account_status"], MSG_FORBIDDEN)
        return acct

    def _handle(self, h, kind):
        parts = urlsplit(h.path)
        path, method = parts.path, h.command
        q = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        try:
            raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        except (ValueError, OSError):
            raw = b""
        if kind == "rest" and path.startswith("/_control/"):
            try:
                body = json.loads(raw) if raw.strip() else {}
                if not isinstance(body, dict):
                    raise ValueError("the body must be a JSON object")
                status, out = 200, self._control(method, path[10:].strip("/"), q, body)
            except (KeyError, ValueError, TypeError) as e:
                status, out = 400, {"error": str(e) or e.__class__.__name__}
            return self._send(h, status, out, {}, "RFC3339")
        if not getattr(h, "_counted", False):
            h._counted = True
            if not self._connection_ok():
                self._record(kind, h, method, path, parts.query, 0, False)
                h.close_connection = True
                return
        fmt = h.headers.get("Accept-Datetime-Format") or "RFC3339"
        with self._guard:
            self._rid += 1
            rid = str(10 ** 16 + self._rid * 4099 + self.seed)
        authorized = False
        try:
            if fmt not in ("RFC3339", "UNIX"):
                raise HTTPError(400, invalid("Accept-Datetime-Format"))
            token = self._token_of(h.headers)
            authorized = True
            with self._guard:
                c = self._connects
                refuse = c["count"] > 0 and c["where"] in (kind, "any")
                if refuse:
                    c["count"] -= 1
            if refuse:
                raise HTTPError(c["status"], MSG_UNAUTHORIZED if c["status"] == 401 else MSG_FORBIDDEN)
            if kind == "rest" and not self._rate_ok():
                raise HTTPError(429, "Rate limit violation: more than %d requests per second" % self.rate_limit)
            with self._guard:
                hop = next((r for r in self._redirects if r["count"] > 0 and r["kind"] == kind
                            and (not r["path"] or path.startswith(r["path"]))
                            and (not r["method"] or r["method"] == method)), None)
                if hop:
                    hop["count"] -= 1
            if hop:
                # the request is not carried out (see redirect_next); the default Location is this request again, on
                # the host it came to
                self._send(h, hop["status"], {}, {"Location": hop["location"] or h.path}, fmt, rid,
                           base=self.stream_url if kind == "stream" else self.rest_url)
                self._record(kind, h, method, path, parts.query, hop["status"], True)
                return
            if kind == "rest":
                with self._guard:
                    fault = next((f for f in self._faults if f["count"] > 0 and (not f["path"] or path.startswith(f["path"]))), None)
                    if fault:
                        fault["count"] -= 1
                if fault:
                    raise HTTPError(fault["status"], {429: "Rate limit violation", 500: "Internal Server Error",
                                                      502: "Bad Gateway", 503: "Service Unavailable",
                                                      504: "Gateway Timeout"}[fault["status"]])
            segs = [unquote(s) for s in path.split("/")[1:]]
            if kind == "stream":
                status = self._stream_route(h, method, segs, q, token, fmt, rid)
                self._record(kind, h, method, path, parts.query, status, True)
                return
            status, body, headers = self._rest(method, segs, q, raw, token, rid, h)
        except HTTPError as e:
            status, body, headers = e.status, (e.body or {"errorMessage": e.message}), {}
        self._send(h, status, body, headers, fmt, rid)
        self._record(kind, h, method, path, parts.query, status, authorized)

    def _send(self, h, status, body, headers, fmt, rid=None, base=None):
        data = json.dumps(render(body, fmt), separators=(",", ":")).encode()
        out = dict(CORS)
        out["Content-Type"] = "application/json"
        if rid:
            out["RequestID"] = rid                       # documented: https://developer.oanda.com/rest-live-v20/account-ep/ (and every -ep page) response header RequestID
        for k, v in headers.items():
            out[k] = (base or self.rest_url) + v if k == "Location" and v.startswith("/") else v
        if "gzip" in (h.headers.get("Accept-Encoding") or "") and data:
            data = gzip.compress(data)                   # documented: https://developer.oanda.com/rest-live-v20/account-ep/ examples' "Content-Encoding: gzip"
            out["Content-Encoding"] = "gzip"
        out["Content-Length"] = str(len(data))
        try:
            h.send_response(status)
            for k, v in out.items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(data)
        except OSError:
            h.close_connection = True

    # -- REST routes
    def _rest(self, method, segs, q, raw, token, rid, h):
        n = len(segs)
        if n < 2 or segs[0] != "v3":
            raise HTTPError(404, "Not Found")                                                 # stand-in's own wording
        if segs[1] == "instruments":
            # documented (by its absence): the /v3/instruments/... page (instrument-ep) answers 404 and has left the menu
            # as of 2026-11-14; candles are read from the account-scoped endpoint only
            raise HTTPError(404, "stand-in: /v3/instruments/... is not documented any more; use "
                                 "/v3/accounts/{accountID}/instruments/{instrument}/candles")
        if segs[1] != "accounts":
            raise HTTPError(404, "Not Found")
        if n == 2:
            if method != "GET":
                raise HTTPError(405, "Method Not Allowed")
            # documented: https://developer.oanda.com/rest-live-v20/account-ep/ GET /v3/accounts "accounts : (Array[AccountProperties])" {id, mt4AccountID, tags}
            rows = []
            for a in self.accounts_for(token):
                r = {"id": a.id}
                if a.mt4 is not None:
                    r["mt4AccountID"] = a.mt4
                r["tags"] = []
                rows.append(r)
            return 200, {"accounts": rows}, {}
        acct = self._account(segs[2], token)
        rest = segs[3:]
        b = self.broker

        def body():
            if not raw.strip():
                return {}
            try:
                return json.loads(raw, parse_float=Decimal)
            except ValueError:
                raise HTTPError(400, "Invalid JSON in request body")                         # stand-in's own wording

        def need(*methods):
            if method not in methods:
                raise HTTPError(405, "Method Not Allowed")

        head = rest[0] if rest else None
        if not rest:
            need("GET")
            with b.lock:
                return 200, {"account": b.account_json(acct, True), "lastTransactionID": str(acct.last_id)}, {}
        if rest == ["summary"]:
            need("GET")
            with b.lock:
                return 200, {"account": b.account_json(acct, False), "lastTransactionID": str(acct.last_id)}, {}
        if rest == ["instruments"]:
            need("GET")
            names = [x for x in (q.get("instruments") or "").split(",") if x]
            if any(x not in self.market.instruments for x in names):
                raise HTTPError(400, invalid("instruments"))
            rows = [i.row() for n_, i in self.market.instruments.items() if not names or n_ in names]
            return 200, {"instruments": rows, "lastTransactionID": str(acct.last_id)}, {}
        if rest == ["configuration"]:
            need("PATCH")
            return b.configure(acct, body(), rid)
        if rest == ["changes"]:
            need("GET")
            since = q.get("sinceTransactionID")
            if since is not None:
                if not since.isdigit():
                    raise HTTPError(400, invalid("sinceTransactionID"))
                if int(since) > acct.last_id:
                    raise HTTPError(416, "stand-in: sinceTransactionID is past the account's last transaction")
            return 200, b.changes(acct, since), {}
        if rest == ["pricing"]:
            need("GET")
            return 200, self._pricing(acct, q), {}
        if rest == ["candles", "latest"]:
            need("GET")
            return 200, self._latest(acct, q), {}
        if head == "instruments" and len(rest) == 3 and rest[2] == "candles":
            need("GET")
            return 200, self._candles(rest[1], q), {}
        if head in ("pricing", "transactions") and rest[-1] == "stream":
            # documented: https://developer.oanda.com/rest-live-v20/pricing-ep/, transaction-ep: "This endpoint is served by the streaming URLs"; the REST host
            # answering 404 is stand-in's own
            raise HTTPError(404, "stand-in: streams are served by the streaming host")
        if head == "orders":
            if len(rest) == 1:
                need("POST", "GET")
                if method == "POST":
                    return b.create_order(acct, body(), rid)
                return self._list(acct, q, "orders")
            spec = rest[1]
            if len(rest) == 2:
                need("GET", "PUT")
                if method == "PUT":
                    return b.replace_order(acct, spec, body(), rid)
                with b.lock:
                    o = b.find_order(acct, spec, pending_only=False)
                    if o is None:
                        raise HTTPError(404, MSG_NO_ORDER)         # documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ NO_SUCH_ORDER
                    return 200, {"order": b.order_json(o), "lastTransactionID": str(acct.last_id)}, {}
            if len(rest) == 3 and rest[2] == "cancel":
                need("PUT")
                return b.cancel_order(acct, spec, rid)
            if len(rest) == 3 and rest[2] == "clientExtensions":
                need("PUT")
                return b.order_client_extensions(acct, spec, body(), rid)
        if rest == ["pendingOrders"]:
            need("GET")
            with b.lock:
                rows = sorted((o for o in acct.orders.values() if o["state"] == "PENDING"), key=_num_id, reverse=True)
                return 200, {"orders": [b.order_json(o) for o in rows], "lastTransactionID": str(acct.last_id)}, {}
        if head == "trades":
            if len(rest) == 1:
                need("GET")
                return self._list(acct, q, "trades")
            spec = rest[1]
            if len(rest) == 2:
                need("GET")
                with b.lock:
                    t = b.find_trade(acct, spec, open_only=False)
                    if t is None:
                        raise HTTPError(404, MSG_NO_TRADE)         # documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ NO_SUCH_TRADE
                    return 200, {"trade": b.trade_json(acct, t), "lastTransactionID": str(acct.last_id)}, {}
            if len(rest) == 3 and rest[2] in ("close", "clientExtensions", "orders"):
                need("PUT")
                fn = {"close": b.close_trade, "clientExtensions": b.trade_client_extensions, "orders": b.trade_orders}
                return fn[rest[2]](acct, spec, body(), rid)
        if rest == ["openTrades"]:
            need("GET")
            with b.lock:
                rows = sorted(b.open_trades(acct), key=_num_id, reverse=True)
                return 200, {"trades": [b.trade_json(acct, t) for t in rows], "lastTransactionID": str(acct.last_id)}, {}
        if head == "positions":
            if len(rest) == 1:
                need("GET")
                with b.lock:
                    rows = [b.position_json(acct, n_) for n_ in sorted(acct.pos)]
                    return 200, {"positions": rows, "lastTransactionID": str(acct.last_id)}, {}
            name = rest[1]
            if len(rest) == 2:
                need("GET")
                if name not in self.market.instruments:
                    raise HTTPError(400, invalid("instrument"))
                with b.lock:
                    return 200, {"position": b.position_json(acct, name), "lastTransactionID": str(acct.last_id)}, {}
            if len(rest) == 3 and rest[2] == "close":
                need("PUT")
                return b.close_position(acct, name, body(), rid)
        if rest == ["openPositions"]:
            need("GET")
            with b.lock:
                names = sorted({t["instrument"] for t in b.open_trades(acct)})
                return 200, {"positions": [b.position_json(acct, n_) for n_ in names],
                             "lastTransactionID": str(acct.last_id)}, {}
        if head == "transactions":
            need("GET")
            return 200, self._transactions(acct, rest[1:], q), {}
        raise HTTPError(404, "Not Found")

    def _list(self, acct, q, what):
        """GET .../orders and .../trades (documented: https://developer.oanda.com/rest-live-v20/order-ep/, trade-ep): ids, state, instrument, count (default 50,
        maximum 500), beforeID ("The maximum ... ID to return"), newest first, a Link header to the next page."""
        b = self.broker
        default, states = ("PENDING", ("PENDING", "FILLED", "TRIGGERED", "CANCELLED", "ALL")) if what == "orders" else \
            ("OPEN", ("OPEN", "CLOSED", "CLOSE_WHEN_TRADEABLE", "ALL"))
        state = q.get("state", default)
        if state not in states:
            raise HTTPError(400, invalid("state"))
        try:
            count = int(q.get("count", 50))
        except ValueError:
            raise HTTPError(400, invalid("count"))
        if not 1 <= count <= 500:
            raise HTTPError(400, invalid("count"))
        before = q.get("beforeID")
        if before is not None and not before.isdigit():
            raise HTTPError(400, invalid("beforeID"))
        ids = set(x for x in (q.get("ids") or "").split(",") if x)
        with b.lock:
            pool = acct.orders.values() if what == "orders" else acct.trades.values()
            rows = [x for x in pool if (state == "ALL" or x["state"] == state)
                    and (not q.get("instrument") or x.get("instrument", x.get("_inst")) == q["instrument"])
                    and (not ids or x["id"] in ids) and (before is None or int(x["id"]) <= int(before))]
            rows.sort(key=_num_id, reverse=True)
            page, more = rows[:count], len(rows) > count
            out = [b.order_json(x) for x in page] if what == "orders" else [b.trade_json(acct, x) for x in page]
            headers = {}
            if more:
                nq = {k: v for k, v in q.items() if k != "beforeID"}
                nq["beforeID"] = str(int(page[-1]["id"]) - 1)
                headers["Link"] = '<%s/v3/accounts/%s/%s?%s>; rel="next"' % (
                    self.rest_url, acct.id, what, "&".join("%s=%s" % (k, quote(v, safe=",")) for k, v in sorted(nq.items())))
            return 200, {what: out, "lastTransactionID": str(acct.last_id)}, headers

    def _transactions(self, acct, rest, q):
        """documented: https://developer.oanda.com/rest-live-v20/transaction-ep/. GET .../transactions gives pages of idrange URLs (pageSize default 100, maximum
        1000; at most 365 days); /{id}, /idrange (from, to), /sinceid (id). A 416 for an id past the last transaction is
        stand-in's own."""
        b = self.broker
        wanted = b.type_filter(q.get("type"))
        with b.lock:
            txs = list(acct.txs)
            last = acct.last_id

            def keep(tx):
                return wanted is None or tx["type"] in wanted
            if not rest:
                try:
                    frm = parse_time(q["from"]) if q.get("from") else CREATED_NS
                    to = parse_time(q["to"]) if q.get("to") else self.clock.now_ns()
                except ValueError:
                    raise HTTPError(400, invalid("from" if q.get("from") else "to"))
                try:
                    size = int(q.get("pageSize", 100))
                except ValueError:
                    raise HTTPError(400, invalid("pageSize"))
                if not 1 <= size <= 1000:
                    raise HTTPError(400, invalid("pageSize"))
                if to - frm > 365 * DAY_MS * MS_NS:
                    raise HTTPError(400, "The maximum time range that can be requested for is 365 days")
                hits = [tx for tx in txs if frm <= tx["time"].ns <= to and keep(tx)]
                pages = []
                for i in range(0, len(hits), size):
                    chunk = hits[i:i + size]
                    url = "%s/v3/accounts/%s/transactions/idrange?from=%s&to=%s" % (self.rest_url, acct.id, chunk[0]["id"],
                                                                                   chunk[-1]["id"])
                    if q.get("type"):
                        url += "&type=" + q["type"]
                    pages.append(url)
                out = {"from": Stamp(frm), "to": Stamp(to), "pageSize": size}
                if q.get("type"):
                    out["type"] = q["type"].split(",")
                out.update({"count": len(hits), "pages": pages, "lastTransactionID": str(last)})
                return out
            if rest == ["idrange"]:
                if not q.get("from") or not q.get("to"):
                    raise HTTPError(400, MSG_MISSING)
                if not (q["from"].isdigit() and q["to"].isdigit()):
                    raise HTTPError(400, invalid("from" if not q["from"].isdigit() else "to"))
                lo, hi = int(q["from"]), int(q["to"])
                if lo > hi:
                    raise HTTPError(400, invalid("to"))
                if lo > last:
                    raise HTTPError(416, "stand-in: from is past the account's last transaction")
                return {"transactions": [tx for tx in txs if lo <= int(tx["id"]) <= hi and keep(tx)],
                        "lastTransactionID": str(last)}
            if rest == ["sinceid"]:
                if not q.get("id"):
                    raise HTTPError(400, MSG_MISSING)
                if not q["id"].isdigit():
                    raise HTTPError(400, invalid("id"))
                if int(q["id"]) > last:
                    raise HTTPError(416, "stand-in: id is past the account's last transaction")
                return {"transactions": [tx for tx in txs if int(tx["id"]) > int(q["id"]) and keep(tx)],
                        "lastTransactionID": str(last)}
            if len(rest) == 1:
                if not rest[0].isdigit():
                    raise HTTPError(400, invalid("transactionID"))
                tx = next((t for t in txs if t["id"] == rest[0]), None)
                if tx is None:
                    raise HTTPError(404, MSG_NO_TRANSACTION)   # documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ NO_SUCH_TRANSACTION
                return {"transaction": tx, "lastTransactionID": str(last)}
        raise HTTPError(404, "Not Found")

    # -- prices
    def _pricing(self, acct, q):
        """GET .../pricing (documented: https://developer.oanda.com/rest-live-v20/pricing-ep/): prices for the instruments named; with `since`, only those that
        changed after it; homeConversions when includeHomeConversions=true; `time` for the next poll's since. The
        deprecated quoteHomeConversionFactors and unitsAvailable (includeUnitsAvailable, default true) are given too."""
        names = [x for x in (q.get("instruments") or "").split(",") if x]
        if not names:
            raise HTTPError(400, MSG_MISSING)
        if any(x not in self.market.instruments for x in names):
            raise HTTPError(400, invalid("instruments"))
        since = None
        if q.get("since"):
            try:
                since = parse_time(q["since"])
            except ValueError:
                raise HTTPError(400, invalid("since"))
        flags = {}
        for k, default in (("includeUnitsAvailable", True), ("includeHomeConversions", False)):
            v = q.get(k)
            if v not in (None, "true", "false", "True", "False"):
                raise HTTPError(400, invalid(k))
            flags[k] = default if v is None else v.lower() == "true"
        b = self.broker
        prices = []
        with b.lock:
            f = b.figures(acct)
            for name in names:
                inst = self.market.find(name)
                qt = self.market.quote(inst)
                if since is not None and qt.ms * MS_NS <= since:
                    continue
                p = b.price_json(qt)
                gain, loss, _ = self.market.factors(inst.quote_ccy, acct.currency)
                p["quoteHomeConversionFactors"] = {"positiveUnits": factor(gain), "negativeUnits": factor(loss)}
                if flags["includeUnitsAvailable"]:
                    p["unitsAvailable"] = self._units_available(acct, inst, qt, f)
                prices.append(p)
            out = {"prices": prices}
            if flags["includeHomeConversions"]:
                ccys = []
                for name in names:
                    for c in name.split("_"):
                        if c not in ccys and self._is_currency(c):
                            ccys.append(c)
                out["homeConversions"] = []
                for c in ccys:
                    gain, loss, pv = self.market.factors(c, acct.currency)
                    out["homeConversions"].append({"currency": c, "accountGain": factor(gain),
                                                   "accountLoss": factor(loss), "positionValue": factor(pv)})
            out["time"] = Stamp(self.clock.now_ns())
            return out

    def _is_currency(self, c):
        try:
            self.market.rate(c, "USD")
            return True
        except (ValueError, KeyError):
            return False

    def _units_available(self, acct, inst, qt, f):
        """documented: https://developer.oanda.com/rest-live-v20/order-df/ UnitsAvailable {default, reduceFirst, reduceOnly, openOnly}, each {long, short}; the
        arithmetic (free margin over the margin of one unit) is stand-in's own."""
        b = self.broker
        per = mid_of(inst, qt.bid, qt.ask) * self.market.rate(inst.quote_ccy, acct.currency) * b._rate(acct, inst)
        can = (f["avail"] / per).quantize(Decimal(1).scaleb(-inst.tup), rounding=ROUND_DOWN) if per > 0 else D0
        trades = b.open_trades(acct, inst.name)
        long_u = sum((t["_units"] for t in trades if t["_units"] > 0), D0)
        short_u = -sum((t["_units"] for t in trades if t["_units"] < 0), D0)
        first = {"long": plain(short_u + can), "short": plain(long_u + can)}
        only = {"long": plain(can), "short": plain(can)}
        reduce_ = {"long": plain(short_u), "short": plain(long_u)}
        return {"default": only if acct.hedging else first, "reduceFirst": first, "reduceOnly": reduce_, "openOnly": only}

    # -- candles (documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ GET /v3/accounts/{accountID}/instruments/{instrument}/candles)
    def _candle_args(self, q):
        price = q.get("price", "M")
        if not price or any(c not in "MBA" for c in price) or len(set(price)) != len(price):
            raise HTTPError(400, invalid("price"))
        try:
            hour = int(q.get("dailyAlignment", 17))
        except ValueError:
            raise HTTPError(400, invalid("dailyAlignment"))
        if not 0 <= hour <= 23:
            raise HTTPError(400, invalid("dailyAlignment"))
        z = zone(q.get("alignmentTimezone", "America/New_York"))
        if z is None:
            raise HTTPError(400, invalid("alignmentTimezone"))
        wa = q.get("weeklyAlignment", "Friday")
        if wa not in WEEKLY_ALIGNMENTS:
            raise HTTPError(400, invalid("weeklyAlignment"))
        flags = {}
        for k, default in (("smooth", False), ("includeFirst", True)):
            v = q.get(k)
            if v not in (None, "true", "false", "True", "False"):
                raise HTTPError(400, invalid(k))
            flags[k] = default if v is None else v.lower() == "true"
        units = dec(q.get("units", "1"))
        if units is None or units <= 0:
            raise HTTPError(400, invalid("units"))
        return price, Align(z, hour, WEEKLY_ALIGNMENTS.index(wa)), flags, units

    def _offsets(self, inst, units):
        """documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ `units` "The number of units used to calculate the volume-weighted average bid and ask
        prices in the returned candles"; the book behind it is the stand-in's own ladder."""
        out = {}
        levels = [(Decimal(s), i) for i, s in enumerate(inst.spec["ladder"])]
        for comp in ("B", "A"):
            got, value = D0, D0
            for size, off in levels:
                take = min(size, units - got)
                if take <= 0:
                    break
                value += inst.tick * off * take
                got += take
            if got < units:
                value += inst.tick * levels[-1][1] * (units - got)
            out[comp] = (value / units).quantize(inst.tick, rounding=ROUND_HALF_UP)
        return out

    def _collect(self, inst, gran, comps, offs, align, count, frm, to, include_first, now):
        """[(start, end, {component: [o, h, l, c]}, volume)] oldest first. Only spans in which the instrument quoted get
        a candle (stand-in's own reading: no prices, no candle)."""
        m, out = self.market, []
        horizon = now + 1
        if frm is not None:
            start, end = align.floor(gran, frm)
            if not include_first:
                start, end = align.floor(gran, end)
            stop = min(to, horizon) if to is not None else horizon
            while start < stop:
                r = m.candle(inst, start, min(end, horizon), comps, offs)
                if r is not None:
                    out.append((start, end, r[0], r[1]))
                    if to is None and len(out) >= count:
                        break
                    if to is not None and len(out) > 5000:
                        raise HTTPError(400, "Maximum value for 'count' exceeded")          # stand-in's own wording
                    start, end = align.floor(gran, end)
                    continue
                nxt = m.next_window_after(inst, end, stop) if end < stop else None
                if nxt is None:
                    break
                start, end = align.floor(gran, max(end, nxt[0]))
            return out
        stop = min(to, horizon) if to is not None else horizon
        start, end = align.floor(gran, stop - 1)
        while len(out) < count and end > HISTORY_START_MS:
            r = m.candle(inst, start, min(end, horizon), comps, offs)
            if r is not None:
                out.append((start, end, r[0], r[1]))
                start, end = align.floor(gran, start - 1)
                continue
            prev = m.last_window_before(inst, start)
            if prev is None:
                break
            start, end = align.floor(gran, min(start, prev[1]) - 1)
        out.reverse()
        return out

    def _candle_rows(self, inst, rows, comps, now, smooth, before=None):
        names = {"B": "bid", "A": "ask", "M": "mid"}
        out, prev = [], before
        for start, end, vals, vol in rows:
            c = {"time": Stamp(start * MS_NS)}
            for comp in ("B", "A", "M"):
                if comp in comps:
                    o, hi, lo, cl = vals[comp]
                    if smooth and prev is not None:
                        # documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ smooth "A smoothed candlestick uses the previous candle's close price as
                        # its open price"
                        o = prev[comp][3]
                        hi, lo = max(hi, o), min(lo, o)
                    c[names[comp]] = {"o": pstr(o, inst.dp), "h": pstr(hi, inst.dp), "l": pstr(lo, inst.dp),
                                      "c": pstr(cl, inst.dp)}
            c["volume"] = vol
            c["complete"] = end <= now          # documented: https://developer.oanda.com/rest-live-v20/instrument-df/ "one whose ending time is not in the future"
            out.append(c)
            prev = vals
        return out

    def _candles(self, name, q):
        inst = self.market.instruments.get(name)
        if inst is None:
            raise HTTPError(400, invalid("instrument"))       # documented: https://developer.oanda.com/rest-live-v20/troubleshooting-errors/ "Invalid Instrument"
        gran = q.get("granularity", "S5")
        if gran not in GRANULARITIES:
            raise HTTPError(400, invalid("granularity"))
        price, align, flags, units = self._candle_args(q)
        times = {}
        for k in ("from", "to"):
            if q.get(k):
                try:
                    times[k] = parse_time(q[k]) // MS_NS
                except ValueError:
                    raise HTTPError(400, invalid(k))
        if "count" in q:
            if "from" in times and "to" in times:
                # documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ "Count should not be specified if both the start and end parameters are provided"
                raise HTTPError(400, "Count should not be specified if both the start and end parameters are provided")
            try:
                count = int(q["count"])
            except ValueError:
                raise HTTPError(400, invalid("count"))
            if count > 5000:
                raise HTTPError(400, "Maximum value for 'count' exceeded")                  # documented maximum 5000
            if count < 1:
                raise HTTPError(400, invalid("count"))
        else:
            count = 500                                                                    # documented default 500
        now = self.clock.now_ms()
        frm, to = times.get("from"), times.get("to")
        if frm is not None and frm > now:
            raise HTTPError(400, invalid("from"))                                         # stand-in's own
        if frm is not None and to is not None and to <= frm:
            raise HTTPError(400, invalid("to"))
        offs = self._offsets(inst, units)
        comps = tuple(price)
        with self.market.lock:
            rows = self._collect(inst, gran, comps, offs, align, count, frm, to, flags["includeFirst"], now)
            before = None
            if flags["smooth"] and rows:
                prev = self._collect(inst, gran, comps, offs, align, 1, None, rows[0][0], True, now)
                before = prev[0][2] if prev else None
        return {"instrument": name, "granularity": gran,
                "candles": self._candle_rows(inst, rows, comps, now, flags["smooth"], before)}

    def _latest(self, acct, q):
        """GET .../candles/latest (documented: https://developer.oanda.com/rest-live-v20/pricing-ep/, candleSpecifications "EUR_USD:S10:BM"): for each, the latest
        complete candle and the one forming now (stand-in's own reading of "most recently completed candles")."""
        specs = [x for x in (q.get("candleSpecifications") or "").split(",") if x]
        if not specs:
            raise HTTPError(400, MSG_MISSING)
        _, align, flags, units = self._candle_args(dict(q, price="M"))
        now = self.clock.now_ms()
        out = []
        for spec in specs:
            parts = spec.split(":")
            if len(parts) != 3 or parts[0] not in self.market.instruments or parts[1] not in GRANULARITIES or \
                    not parts[2] or any(c not in "MBA" for c in parts[2]):
                raise HTTPError(400, invalid("candleSpecifications"))
            inst = self.market.find(parts[0])
            comps = tuple(dict.fromkeys(parts[2]))
            with self.market.lock:
                rows = self._collect(inst, parts[1], comps, self._offsets(inst, units), align, 2, None, None, True, now)
            out.append({"instrument": parts[0], "granularity": parts[1],
                        "candles": self._candle_rows(inst, rows, comps, now, flags["smooth"])})
        return {"latestCandles": out}

    # -- streams (documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ and transaction-ep: chunked transfer encoding, one JSON object per line,
    #    heartbeats every 5 seconds, served by the streaming host)
    def _stream_route(self, h, method, segs, q, token, fmt, rid):
        def refuse(status, message):
            self._send(h, status, {"errorMessage": message}, {}, fmt, rid)
            return status
        if not (len(segs) == 5 and segs[:2] == ["v3", "accounts"] and segs[3] in ("pricing", "transactions")
                and segs[4] == "stream"):
            return refuse(404, "Not Found")
        if method != "GET":
            return refuse(405, "Method Not Allowed")
        try:
            acct = self._account(segs[2], token)
        except HTTPError as e:
            return refuse(e.status, e.message)
        insts, snapshot = [], True
        if segs[3] == "pricing":
            names = [x for x in (q.get("instruments") or "").split(",") if x]
            if not names:
                return refuse(400, MSG_MISSING)
            if any(x not in self.market.instruments for x in names):
                return refuse(400, invalid("instruments"))
            insts = [self.market.find(x) for x in dict.fromkeys(names)]
            snapshot = q.get("snapshot", "true").lower() != "false"
        with self._guard:
            if self.stream_limit and len(self._streams) >= self.stream_limit:
                full = True
            else:
                full = False
                s = _Stream(segs[3], acct.id, token, h)
                self._streams.add(s)
        if full:
            # documented: https://developer.oanda.com/rest-live-v20/development-guide/ "20 active streams. Requests above this threshold will be rejected."; the
            # 429 is stand-in's own
            return refuse(429, "stand-in: more than %d active streams from this address" % self.stream_limit)
        try:
            h.send_response(200)
            for k, v in CORS.items():
                h.send_header(k, v)
            h.send_header("Content-Type", "application/octet-stream")   # documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ stream example
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            h.wfile.flush()
            if s.kind == "pricing":
                self._pricing_stream(s, insts, snapshot, fmt)
            else:
                self._transaction_stream(s, acct, fmt)
            if s.clean:
                with s.lock:
                    h.wfile.write(b"0\r\n\r\n")
                    h.wfile.flush()
        except OSError:
            pass
        finally:
            with self._guard:
                self._streams.discard(s)
            h.close_connection = True
        return 200

    def _line(self, obj, fmt):
        return json.dumps(render(obj, fmt), separators=(",", ":")).encode() + b"\n"

    def _beat(self, s):
        if s.closed.is_set() or self._stop.is_set():
            return False
        if self.stream_max_age and time.monotonic() - s.opened >= self.stream_max_age:
            s.close(clean=True)
            return False
        try:                                    # the client went away: the stream ends now, not at the next failed write
            sock = s.h.connection
            if select.select([sock], [], [], 0)[0] and not sock.recv(1, socket.MSG_PEEK):
                s.closed.set()
                return False
        except (OSError, ValueError):
            s.closed.set()
            return False
        return True

    def _pricing_stream(self, s, insts, snapshot, fmt):
        """documented: https://developer.oanda.com/rest-live-v20/pricing-ep/ "at most 4 prices per second (every 250 milliseconds) for each instrument"; "only the
        price in effect at the end of the window is sent"; heartbeats every 5 s; snapshot (default true) on connect.
        measured on a live account: the snapshot repeats each instrument's latest price with its original time."""
        b, last = self.broker, {}
        if snapshot:
            lines = []
            for inst in insts:
                q = self.market.quote(inst)
                lines.append(self._line(b.price_json(q), fmt))
                last[inst.name] = (q.bid, q.ask, q.tradeable)
            s.write(b"".join(lines))
        else:
            for inst in insts:
                q = self.market.quote(inst)
                last[inst.name] = (q.bid, q.ask, q.tradeable)
        mono = time.monotonic
        next_win, next_hb = mono() + 0.25, mono() + self.heartbeat
        while self._beat(s):
            wait = min(next_win, next_hb) - mono()
            if wait > 0:
                s.wake.wait(wait)
                continue
            if mono() >= next_win:
                lines = []
                for inst in insts:
                    q = self.market.quote(inst)
                    key = (q.bid, q.ask, q.tradeable)
                    if last.get(inst.name) != key:
                        last[inst.name] = key
                        lines.append(self._line(b.price_json(q), fmt))
                if lines:
                    s.write(b"".join(lines))
                next_win = max(next_win + 0.25, mono())
            if mono() >= next_hb:
                s.write(self._line({"type": "HEARTBEAT", "time": Stamp(self.clock.now_ns())}, fmt))
                next_hb = max(next_hb + self.heartbeat, mono())

    def _transaction_stream(self, s, acct, fmt):
        """documented: https://developer.oanda.com/rest-live-v20/transaction-ep/, every Transaction of the account as it happens, and a TransactionHeartbeat
        {type HEARTBEAT, lastTransactionID, time} every 5 s. Nothing is replayed from before the connect (research
        reading: the stream starts "from when the request is made")."""
        mono = time.monotonic
        next_hb = mono() + self.heartbeat
        while self._beat(s):
            while s.queue:
                tx = s.queue.popleft()
                s.write(self._line(tx, fmt))
            if mono() >= next_hb:
                with self.broker.lock:
                    last = str(acct.last_id)
                s.write(self._line({"type": "HEARTBEAT", "lastTransactionID": last, "time": Stamp(self.clock.now_ns())},
                                   fmt))
                next_hb = max(next_hb + self.heartbeat, mono())
            s.wake.wait(max(0.0, min(next_hb - mono(), 0.25)))
            s.wake.clear()

    def _publish(self, acct, tx):
        for s in self._stream_list():
            if s.kind == "transactions" and s.account == acct.id:
                s.queue.append(tx)
                s.wake.set()


def main(argv=None):
    ap = argparse.ArgumentParser(description="A local stand-in for OANDA's v20 REST and streaming API (127.0.0.1 only).")
    ap.add_argument("--port", type=int, default=0, help="the REST port; 0 picks a free one")
    ap.add_argument("--stream-port", type=int, default=0, help="the streaming port; 0 picks a free one")
    ap.add_argument("--seed", type=int, default=7, help="fixes every price, the token and every id")
    ap.add_argument("--market", choices=("open", "closed", "auto"), default="open",
                    help="from the start on: always open, closed, or OANDA's documented hours (auto)")
    ap.add_argument("--division", choices=("uk", "us"), default="uk",
                    help="uk: three sub-accounts (netting GBP, hedging USD, MT4-linked GBP); us: one FIFO USD account")
    ap.add_argument("--rate-limit", type=int, default=120, help="REST requests a second (documented 120; 0 = off)")
    ap.add_argument("--stream-limit", type=int, default=20, help="active streams (documented 20; 0 = off)")
    ap.add_argument("--new-connections-per-second", type=int, default=2, help="documented 2; 0 = off")
    ap.add_argument("--heartbeat", type=float, default=5.0, help="seconds between stream heartbeats (documented 5)")
    args = ap.parse_args(argv)
    fake = FakeOanda(seed=args.seed, market=args.market, division=args.division, port=args.port,
                     stream_port=args.stream_port, rate_limit=args.rate_limit or None,
                     stream_limit=args.stream_limit or None,
                     new_connections_per_second=args.new_connections_per_second or None,
                     heartbeat=args.heartbeat).start()
    ids = fake.account_ids
    print(json.dumps({"rest_url": fake.rest_url, "stream_url": fake.stream_url, "port": fake.port,
                      "stream_port": fake.stream_port, "control_url": fake.rest_url + "/_control",
                      "token": fake.token, "accounts": ids, "market": args.market, "division": args.division,
                      "seed": args.seed,
                      "env": {"OANDA_REST_URL": fake.rest_url, "OANDA_STREAM_URL": fake.stream_url,
                              "BRUE_CRED_TOKEN": fake.token, "BRUE_ACCOUNT_ID": ids[0]}}), flush=True)
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
