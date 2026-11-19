#!/usr/bin/env python3
"""Brue Connect adapter for Alpaca (US shares and crypto, paper and live).

Alpaca's wire is plain: JSON over HTTPS with two key headers on a trading
host and a market-data host, and one websocket (`/stream` on the trading
host) that pushes every change to an order as a `trade_updates` message.
Numbers arrive as strings, times as RFC 3339 with nanoseconds, and every
order call is a REQUEST: a 200 says Alpaca has the order, never that it
filled, was replaced or was cancelled. The truth is the stream, backed by
reading orders, fills and positions again.

Shape of the thing:

  * `hello` answers COLD, and LIVE IS OPT-IN. The environment is paper
    unless `--live` or ALPACA_ENV=live is given (a flag wins over the
    variable); the key's prefix selects nothing. `mode` is a separate
    question answered by the ENDPOINTS: it is "paper" only when the
    trading host and the stream host are each `paper-api.alpaca.markets`
    exactly, or this machine (a stand-in on loopback). Any other host is
    "live" whatever the flags say, so an override that reaches the live
    host through another name can never trade real money as "paper".
    The key and the secret are checked once, cold (printable, no
    whitespace); one that cannot be a key is `unauthorized` naming the
    FIELD, and neither value can reach a message or the log. The first
    real request reads the account on the one host; a refusal is
    `unauthorized` naming the environment tried. It never tries the other.
  * Threads: the SDK's read thread only READS. It notes when each line
    was read and hands the request to a lane (see `_dispatch`): calls
    that change something are answered one at a time, in the order they
    were read; reads a few at once; each `bars.history` on a thread of
    its own. So a chart that loads for six seconds holds up nothing, and
    answers leave in any order (SPEC 1). A handler runs under ONE
    deadline of 8 s counted from when its line was READ (what it waited
    in its lane is part of its time, as it is of the platform's ten
    seconds), shared by every request and wait in it; a request whose
    time went by in line is told so at once and nothing is done. One
    more thread owns the order stream; one polls quotes; one is the
    backstop (missed fills, the periodic re-read of what Alpaca holds,
    bars, catalog and state upkeep); and one does nothing but keep the
    stops and targets right, so that a position never waits for its
    stop behind anything else. (An order that turns up after a
    `flatten_all` is dealt with on a short thread of its own: a cancel
    Alpaca does not answer must not hold the backstop.) Every thread's
    loop survives any exception and says so (`session.state`).
  * A pace PER HOST, as Alpaca counts: three requests a second to the
    trading host (orders, account, positions) and three to the data host
    (prices, bars), each inside that host's 200 a minute (`_Budget`). So
    a chart that loads and the prices of a watch list take nothing from
    an order or from a platform that polls. Who gets the next place on a
    host: `flatten_all`; a stop being put back, turn about with whatever
    a handler asks; an order; the newest end of a chart; then, turn
    about, a chart's windows and the reads, with the prices in line with
    the reads once they have waited. Below an order they go out a third
    of a second apart, and while an order is in hand they leave one of
    every three untaken, so that an order never waits a second for a
    place. A read waits its turn inside its handler's own time; a minute
    that is spent is `rate_limited`, retryable, at once, and says so.
    (Calls that change something share one lane: `flatten_all` sent
    while another of them is still being answered waits for it, at most
    its 8 s, and is then given time to do its work whatever it waited.)
  * An order whose fate is NOT KNOWN (its answer and the look-up were
    both lost) is never announced `order.rejected`: the call answers a
    retryable error, and Alpaca is asked for the client id until it says.
    A client id this profile has sent before is looked up at Alpaca
    BEFORE any pre-check, and a refusal that depends on the account (the
    margin, an open position, the clock) is only given once Alpaca has
    said the id is not an order it already holds. `order.rejected` is
    sent for a refusal that is final, not for "ask again". An answer
    that names no order (a 200 with an empty body) is no answer: the
    client id is looked up. A later request under the same id that
    Alpaca refuses, or that is never sent, says nothing of the earlier
    one: that stays asked for. Several such orders are asked for with
    ONE request a pass (the account's orders since the oldest of them,
    matched on the client id), made only while the minute has room
    beside the backstop's own reads; so is the look-up of one order
    alone on the long watch.
  * NO market-data socket. Alpaca gives most users ONE data-stream
    connection per endpoint, and an adapter that took it would knock the
    user's own tools off. Quotes are polled over REST (one request for
    all subscribed shares, one for all crypto, about every 1.5 s).
  * Fills are at-least-once on the way in and exactly-once on the way
    out: the stream, the reconnect backstop and the fills ledger all carry
    Alpaca's execution id, which is the `fill_id`; an execution already
    emitted is dropped BEFORE `emit`.
  * Identity: a netting book. `position_id` is the catalog symbol.
    `client_id` is sent as Alpaca's `client_order_id`, which Alpaca
    refuses to accept twice: that refusal is how a replayed `order.place`
    is recognised after a restart, and the original order's outcome is
    returned instead of a second order.

What Alpaca does that a platform must know (all observed on paper):

  * A crypto BUY pays its fee out of the coin bought: buy 0.0002 BTC and
    the position holds 0.0001995. The fill's `qty` is that NET amount
    (what the position gained), `price` is the executed price, and `fees`
    is the coin withheld valued at that price, in USD, negative.
  * A crypto SELL pays its fee out of the cash received, and Alpaca does
    not report the amount when it fills (it posts fee rows at the end of
    the day). `fees` is 0 on those fills and `commission` on a close only
    counts fees this session saw. The cash balance is always right.
  * Crypto positions are held per COIN: only pairs quoted in the account
    currency (`BTC/USD`) are listed, because `BTC/USDT` would spend a
    second position rather than cash.
  * Shares are whole shares only in this version (`lot_step` 1). Alpaca's
    fractional orders are day-only, cannot rest as GTC and cannot carry
    stops, which one catalog row cannot express.
  * A market order for a share while the exchange is shut is refused here
    with `market_closed`. Alpaca would QUEUE it until the next open.
  * `order.amend` is Alpaca's replace, which makes a NEW order with a new
    id. The platform keeps the id it was first given: this adapter maps it
    to the newest replacement, and reports fills and `order.amended`
    under the first id.
  * GTC is not forever at Alpaca: such an order is cancelled about 90 days
    after it was placed. `expires_at` carries Alpaca's own date.
  * The margin check for marginable shares uses the Reg T half (2x). An
    account Alpaca allows 4x intraday can therefore do less through this
    adapter than Alpaca itself would accept.
  * Share prices on a free key are IEX's alone (about 2.5% of the volume):
    for a share that trades little the quote can be dollars wide or
    minutes old, while Alpaca fills orders and triggers stops on the
    consolidated market. A quote with no bid or no ask is never sent.
  * `flatten_all` answers only when the account is flat. It is never
    refused locally (not for a 429 block, which is capped at a minute
    for everything else; not for a spent minute) and an unknown clock
    does not stop it. Every position is closed by an order of its own,
    marked as a close; Alpaca's close-all call is NOT used (its orders
    are tied to nothing here: carried out late, it sold positions twice
    and one opened after the flatten). They go out at the trading
    host's pace, three a second: about six positions are flat in three
    seconds, and more than about eighteen do not fit in one call, which
    then answers "not flat", names what is left, and closes that when
    asked again.
    What it cannot close now (a share while the exchange is KNOWN to be
    shut) makes the answer an error that names it; such a position keeps
    its stop. (Open or shut is Alpaca's `/v2/clock`, kept until the open
    or close it names, which is compared with ALPACA's time: this
    machine's clock corrected by the Date of Alpaca's answers; in the
    minute before that boundary a read is kept for one second only by
    an order or a `position.close`, never past the boundary it names,
    and `flatten_all` reads it afresh.) If it
    gives up for any other reason it cancels the closing orders it
    placed, and the stops of what is still open are put back.
    It does NOT answer "flat" while an order sent before it is of
    unknown fate (the call that sent it was cut short for flatten_all,
    or its answer was lost): it waits inside its time, then answers a
    retryable error that names the order. One that turns up while it
    waits is an order like any other from then on (cancelled if it
    rests, closed if it has filled), and the account is READ AGAIN
    before anything is said: "flat" is never judged on a read made
    before the order was seen (and in every pass the open orders are
    read first and the positions after them: a fill between two
    reads made side by side is in neither). Nor on reads that began
    while an order request was on its way to Alpaca (a call that
    changes something still being answered, a stop or target being
    placed: a POST answered slowly is not of unknown fate, and what it
    makes may land after the reads), or beside which one began: the
    account is read again. A symbol with an order of unknown fate, or
    one on its way, is not closed from flatten's first read but from a
    read made after its cancel-all (unless that order is a closing
    order of the symbol: flatten's close goes out under its client id,
    below). If an order of unknown fate turns
    up at Alpaca afterwards, what rests of it is cancelled (a retry
    under its client id is then told `x_alpaca_cancelled_after_flatten`,
    also when the cancel's answer was lost, and after a restart; not
    when Alpaca took the platform's own `order.cancel` of it first and
    that cancelled it: such a cancel is given 1.5 s to show, and one
    that was taken and not carried out is followed by this adapter's
    own); if it has FILLED,
    no order is placed against it (this adapter never sends an order
    nobody asked for): `session.state` names the symbol, the side, the
    quantity and Alpaca's time for the fill, and what the symbol holds
    at that moment. Asking `flatten_all` again closes it. Such an
    order holds `flatten_all`'s answer back for half a minute (one
    asked after that, and told by its own look-up that Alpaca does not
    hold it, is not held back by it); it is
    asked for at Alpaca for ten minutes, and when that stops it is
    SAID, naming the order. One that rests and cannot be cancelled is
    tried again, with a growing pause, for ten minutes, and what is
    said of it stays said until the order is seen ended.
    Its cancel-all (`DELETE /v2/orders`) is the one request it still
    makes that is tied to no order: whenever Alpaca carries it out, it
    cancels what rests THEN. One that ends without an answer (none, a
    5xx, a 408) is remembered for ten minutes, because it may be
    carried out late, after the user has opened a position with a stop
    and a target again or put new orders to rest. In those ten minutes
    a stop or target of ours that is found cancelled, without this
    adapter having asked, is PUT BACK at the level that was set (a
    target the market has reached meanwhile is not) and
    `session.state` says so, with the time of that cancel-all; an
    order of the user's that goes the same way is named (symbol, side,
    quantity, client id), with why, and is NOT placed again: that
    would be an order of this adapter's own. Outside those ten minutes
    an exit cancelled from outside is dropped and said, as ever.
  * A closing order is MARKED as a close (`position_intent`:
    `sell_to_close` for a long, `buy_to_close` for a short), every one
    this adapter sends: `position.close` and `flatten_all`, shares and
    crypto. Alpaca itself then refuses one that finds nothing on that
    side to close (422 "position intent mismatch", seen on paper; for
    crypto 403 "insufficient balance" when less is held than asked):
    a close that arrives late, after the position has gone, opens
    nothing. Such a refusal is not a failed order and is never sent
    again as it is: the position is read again. `position.close` then
    answers `not_found` when it is gone (or the original outcome, when
    an order under this close's own id is what closed it), and a
    retryable `x_alpaca_nothing_to_close` when Alpaca's position list
    still shows one; `flatten_all` reads on. NOT known of Alpaca
    (shares, to be seen in a market session): whether it also refuses
    a marked close for MORE than is held, or one sent while another
    closing order is still working. For those:
  * ONE closing order per symbol. While a closing order's request is on
    its way, or ended without an answer that says whether the order
    exists, every further close of that symbol (`flatten_all`, another
    `position.close`) is sent under the SAME client id: Alpaca takes an
    id once, so the position cannot be closed twice by a request that
    arrives late. The order stays remembered for ten seconds after it
    is first seen at Alpaca: a close sized from a read of the position
    made before that moment is not sent beside it (`position.close`
    answers the retryable `x_alpaca_close_pending`). An order request
    answered with a 5xx is looked up and treated as of unknown fate,
    never as refused.
    Between two `position.close` calls the order under such an id
    stands for ONE of them: the one whose request made it. Asked
    again, the close the id belongs to is answered ok only from an
    order its own request made; when another `position.close`'s
    request made it, `x_alpaca_close_not_carried_out` (final: it
    names the close that was served and asks for a new `client_id`);
    when both requests ended unanswered for the same quantity and it
    cannot be known which made it, the retryable
    `x_alpaca_close_not_known`. A call every request of which under the
    id Alpaca refused (a duplicate of the id, or nothing left to close)
    made nothing and is not taken for the one served. (An order that
    `flatten_all`'s request made under a close's id still answers that
    close: its wish covers the close's, and nothing is counted twice.)
  * `account.get`: `balance` is Alpaca's `equity` less the open
    positions' unrealized P&L, so that equity minus balance is the open
    P&L and nothing else. Both are read for that call, at the same
    instant (the account and the positions, two requests): a fill
    between the two reads can put it off by that fill's spread, a price
    by what it moved in those milliseconds. The two are checked against
    each other (the account's market value of what is held against the
    rows' own) and read again, once, when they show two moments. A
    position row with no open P&L to be had fails the call, retryable,
    rather than count as zero. It is NOT Alpaca's `cash`,
    which falls by the whole price of a purchase and by the cost of a
    crypto buy that is only resting.

Stops and targets on a position (`brackets`, `modify`), shares only:

  * They are orders ALPACA holds, never a price watched in here. A market
    order sent with `sl` and/or `tp` while flat in that symbol goes as
    Alpaca's OTO or bracket order: the exits exist from the instant the
    entry fills, and a level on the wrong side of the market refuses the
    whole order (`invalid_stops`) with nothing placed.
  * ONE component owns them (`_Protection`, below, which states the
    rules it keeps). In short: the exit orders at Alpaca are made to
    match the levels the platform set, from a fresh read of the position
    and the symbol's open orders, under a guard per symbol that closing,
    flattening, modifying and the backstop's repair all take. An exit
    with no position behind it, or for more than it, is cancelled; a
    level with no live order for about three seconds is put back, by
    whomever or whatever it was lost; a failure is `session.state:
    degraded` naming the symbol, and is tried again until it succeeds.
  * `positions.list` shows a level only while an order at Alpaca carries
    it. A level that is wanted and not held is not shown as if it were.
    Its `unrealized_pnl` is Alpaca's own figure for the row; a row
    that carries none is worked out from its market value and cost,
    and when that cannot be done the field is left out of that
    position: never 0.
  * `order.modify` moves a level by Alpaca's replace, in place: the
    position is never without its stop. Adding or removing a level
    swaps the orders (a stop, a limit, or Alpaca's OCO pair for both);
    if Alpaca refuses the new ones the old levels stand and are put back.
  * A stop or target that fills is `position.closed` with reason `sl` or
    `tp`. An order of the adapter's own that should not be there (an
    exit with no position, a close that will not end) is listed by
    `orders.list` while the session is degraded about it, and one that
    FILLS and opens a position is reported as an `order.filled` with a
    null `client_id`: a position never appears without an event.
  * `position.close` judges the request first (size, clock, Alpaca's
    wash-trade rule, whether this very close already exists at Alpaca)
    and only then takes the exits off. After a partial close they are
    re-made for what is left; after a close that did not happen, for all
    of it, and the session says the close order died. Without `qty` it
    closes the WHOLE position or nothing: when other open orders hold
    part of it (the user's resting sell) it answers `x_alpaca_qty_held`,
    as a `qty` of all of it does, naming what is free and the orders
    that hold the rest, and closes nothing. When what holds it is only
    this adapter's own closing orders, still working (an earlier
    `position.close`, a `flatten_all`), it answers the retryable
    `x_alpaca_close_pending` naming them, closes nothing, and closes
    what they leave when asked again. This adapter's own stop and
    target are not such orders. A RETRIED
    close (a client id this profile has sent) is looked up at Alpaca
    once more before it is answered `not_found` or `x_alpaca_qty_held`:
    its first request may have landed meanwhile, and its order is then
    the answer (SPEC 2).
  * At a start the broker is the truth (SPEC 7): the levels are what its
    orders carry. They are found in Alpaca's own lists by the client ids
    this adapter gives them, by the ids it wrote down (a HELD stop leg is
    in none of Alpaca's open-order lists and can only be read by id), and
    as legs under an order of this profile. A level an earlier run set
    that Alpaca no longer holds is said, not quietly put back.
  * Limits: `sl`/`tp` on `order.place` need a market order and no open
    position in the symbol (afterwards use `order.modify`); a resting
    order cannot carry them. While a stop or target rests, Alpaca takes
    no plain order that would reduce the position, on either side
    ("insufficient qty available"), and with a plain stop none that adds
    to it ("potential wash trade"): close with `position.close`, which
    takes the exits off first. Stops trigger in the regular session only.
    Crypto has no stop order at Alpaca: `sl` and `tp` are refused there
    (`trade_restricted`).

Declared absent: `trailing`, `oco` (the SPEC's free order groups),
`close_by` (netting book), `operations`, `gtd` expiry, a plain stop order
for crypto, anything outside the regular session for shares.

`history.fills` is bounded here (the request may carry no range): seven
days back unless `from` says otherwise, the newest 500 fills at most.
Remembered between runs, in BRUE_BROKER_STATE: `alpaca_state.json` (the
results of mutating calls by client id, the client ids sent, this
profile's own fills, the ids of the exit orders and the levels they
carried, the share feed found; and, each with its time and taken back
by a start when younger than ten minutes: the orders whose fate was not
known, an order being written down BEFORE its request leaves; those
this adapter had stopped asking for; the one closing order a symbol
must reckon with; and the cancel-alls of `flatten_all` that ended
without an answer. So a restart does not make `flatten_all` answer
"flat" over an order of unknown fate, does not let a late order land
unnamed, does not close under a new id a position whose close was
on the wire, and still puts back a stop that a late cancel-all takes
off. Kept by number, not by age: whose request made a closing order
under an id that more than one call used, and the late orders this
adapter asked Alpaca to cancel, so that a retry under such an id is
answered truly by whichever run is asked) and `alpaca_catalog.json`
(the instrument list, rebuilt in the background every six hours).

Configuration (environment):

  BRUE_CRED_API_KEY / BRUE_CRED_API_SECRET   the key pair the platform
                                             collected (form keys api_key,
                                             api_secret)
  ALPACA_API_KEY / ALPACA_API_SECRET         the same, for running by hand
  ALPACA_ENV = paper | live                  paper unless this says live
                                             or --live is given; a flag wins
  ALPACA_FEED = iex | sip | auto             share prices; auto asks for
                                             SIP once and falls back to IEX
  ALPACA_TRADING_URL, ALPACA_DATA_URL,
  ALPACA_STREAM_URL                          endpoints, for a test stand-in
                                             (anything not Alpaca's paper
                                             host or loopback is mode live)
  BRUE_BROKER_STATE                          private directory for caches
"""
from __future__ import annotations

import base64
import calendar
import collections
import contextlib
import datetime
import decimal
import hashlib
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
USER_AGENT = "brue-connect-alpaca/" + VERSION

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
PAPER_STREAM_URL = "wss://paper-api.alpaca.markets/stream"
LIVE_STREAM_URL = "wss://api.alpaca.markets/stream"
# The ONE name that is paper money. `mode` is "paper" for this host and for
# this machine itself (a test stand-in); every other endpoint is live.
PAPER_HOST = "paper-api.alpaca.markets"

# Alpaca allows 200 requests a minute on each host, per account, shared with
# every other tool the user runs on the same key. The window below stops
# short of that; READS stop earlier still so a close or a cancel always has
# room (SPEC 8.1), and the background threads earlier than the handlers.
BUDGET_PER_MINUTE = 180
BUDGET_RESERVE = 40          # kept for mutating calls and flatten_all
BUDGET_BACKGROUND = 100      # most the adapter's own threads may spend
BUDGET_WATCH_ROOM = 20       # ... of which the watch for orders of unknown
#                              fate leaves this many to the backstop's reads
BUDGET_BACKGROUND_DATA = 140  # ... on the data host, where they are the users
BUDGET_BULK = 110            # windows of bars read while a chart waits
BUDGET_IDLE = 80             # bars read on quietly after the answer
PACE_PER_SECOND = 3          # on each host (Alpaca counts them apart)
HANDLER_TIMEOUT = 6.0        # most one request may take
# The platform waits 10 s for any answer. ONE deadline covers a whole
# handler, every request and every wait in it, so no chain of calls can
# outlast the platform's patience and hold up the next request in the
# queue (which may be flatten_all).
HANDLER_BUDGET = 8.0
FLATTEN_BUDGET = 7.5         # s flatten_all works towards flat ...
FLATTEN_HARD = 9.3           # ... and by when it has cleaned up and answered
FLATTEN_FLOOR = 5.0          # s flatten_all works for, however long it queued
CLOSING_KEEP = 10.0          # s a closing order is remembered after it is seen
CLOCK_EDGE = 60.0            # s before the exchange's next open or close
CLOCK_EDGE_KEEP = 1.0        # ... in which a read of the clock is kept
#                              (Alpaca's time) the clock is read at every call

# Requests are handed off the read thread (see AlpacaAdapter._dispatch).
READ_WORKERS = 4             # reads answered at once
CHART_WAITERS = 8            # bars.history requests waiting at once
BARS_LOADS = 2               # charts whose windows are being read at once
READ_MARGIN = 2.0            # s a handler's read keeps for the request itself
BYE_DRAIN = 4.0              # s `bye` waits for an order in flight
BYE_TOTAL = 4.3              # ... and for a stop that is due, in all
REMAKE_SEC = 600.0           # a stop due this recently is put back at a start
NOTICE_SEC = 120.0           # how long a thing said once stays said
BLOCK_CAP = 60.0             # longest one 429 may silence a host
# An order whose fate is not known (see _resolve_unknown, _late).
UNKNOWN_SEC = 30.0           # s Alpaca is asked for it every two seconds
LATE_WATCH = 600.0           # s one that flatten_all was told of is watched,
#                              and s a late order that RESTS is cancelled at
LATE_SETTLE = 1.5            # s a cancel is given before "still working"
OWN_SEC = 120.0              # s between an order's own look-ups beside the list
WATCH_REACH = 120.0          # s the list reaches back before the oldest of them
WATCH_PAGE = 500             # rows of that list (Alpaca's most)

# Stops and targets (see _Protection).
PROTECT_GRACE = 3.0          # s a wanted level may be without a live order
PROTECT_SAY = 6.0            # s after which that is said (session.state)
ORPHAN_SAY = 2.0             # s an exit order may outlive its position unsaid
OVERSIZE_SAY = 8.0           # s one may be larger than it beside a half that
#                              is filling in parts (5 s of grace, a replace)
CLOSE_SAY = 5.0              # s a close order may work before it is shown
AUDIT_SEC = 10.0             # broker re-read while anything is protected
AUDIT_IDLE_SEC = 60.0        # ... and when nothing is
RETRY_MAX = 30.0             # longest pause between re-protection attempts
BAR_SUB_REACH = 20           # bars back a subscription will still fetch

QUOTE_POLL_SEC = 1.5
MAX_QUOTE_SYMBOLS = 200      # one share request + one crypto request a poll
BARS_MAX_COUNT = 20000
# bars.history: Alpaca builds every timeframe between a minute and a day
# from minute bars as it is asked, about 10,000 of them a request, and a
# request takes one to five seconds (seen 2026-11-10: 5000 hourly bars are
# thirty requests). So bars are read in WINDOWS, several at once, into a
# store per symbol and timeframe; a call answers with what is there when
# its own budget ends, and the rest is read quietly for the next call.
BARS_BUDGET = 6.0            # s one call may wait; the platform waits 10
BARS_FIRST = 3.0             # s after which a call answers with what it has
BARS_EARLY = 2               # windows read beside the newest end, not after
BARS_HEAD_TTL = 20.0         # s the newest end of a store is trusted
BARS_WORKERS = 6             # windows read at once while a caller waits
BARS_FILL_EXTRA = 30         # windows read quietly after the answer
BARS_STORES = 24             # (symbol, timeframe) stores kept, least used out
BARS_IDLE = 900.0            # s unused before a store is dropped
BARS_LIFE = 3600.0           # s after which a store is read afresh
BARS_END_EMPTY = 4           # empty windows in a row: before the first bar
BARS_FIRST_MS = 1451606400000    # 2016-01-01: Alpaca's history begins
H4_MS = 14_400_000
# Hours one window reaches back. Crypto has a bar every minute: 164 h is
# just under the 10,000 a request covers. Shares cost Alpaca more the longer
# the span, so two weeks; a week of one-minute bars is 2,000 rows.
BAR_WINDOW_H = {(True, True): 40, (True, False): 164,
                (False, True): 168, (False, False): 336}
# The first read of the newest end reaches this many hours behind the
# anchor (crypto, shares), so that even a 4h chart has closed bars to show
# from the one request that must succeed.
BAR_HEAD_H = {True: 24, False: 96}
HISTORY_DAYS = 7
HISTORY_MAX_FILLS = 500
CATALOG_MAX_AGE = 24 * 3600  # serve the cached catalog up to this old
CATALOG_REFRESH = 6 * 3600   # and rebuild it in the background this often
STREAM_PING_SEC = 10.0

TIMEFRAMES = {"1m": ("1Min", 60_000), "5m": ("5Min", 300_000),
              "15m": ("15Min", 900_000), "30m": ("30Min", 1_800_000),
              "1h": ("1Hour", 3_600_000), "4h": ("4Hour", 14_400_000),
              "1d": ("1Day", 86_400_000)}

# The regular session, published in the exchange's own zone: New York moves
# between UTC-5 and UTC-4, so the same hours in UTC would be wrong for half
# the year. Holidays and early closes come from Alpaca's calendar.
SESSION_SHARES = [{"days": [1, 2, 3, 4, 5], "open": "09:30",
                   "close": "16:00", "tz": "America/New_York"}]
# Crypto never shuts. Said explicitly: an empty list would mean "no calendar
# published", not "always open" (SPEC 4).
SESSION_ALWAYS = [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                   "close": "24:00", "tz": "UTC"}]

# Alpaca's published crypto fee schedule (docs: crypto-fees), taker rate by
# the account's `crypto_tier`. Only used to tell a platform what a market
# order costs; fills never use it (they report what Alpaca actually took).
CRYPTO_TAKER_FEE = {1: 0.0025, 2: 0.0022, 3: 0.0020, 4: 0.0018, 5: 0.0015,
                    6: 0.0013, 7: 0.0012, 8: 0.0010}
# Not a rate: a bound on how much of a crypto buy can plausibly be fee. A
# bigger gap between what was bought and what the position gained means the
# adapter's view of the position was stale, and it then reports the fill as
# executed rather than inventing a fee.
FEE_SANITY = Decimal("0.02")
CRYPTO_MIN_NOTIONAL = 10.0   # "cost basis must be >= minimal amount of order 10"
CRYPTO_MAX_NOTIONAL = 200000.0   # "exceeds max notional per order 200000"
MAX_QTY = 1e9                # Alpaca states no quantity cap; this is "none"

# Alpaca order status -> the SPEC's four order states. `replaced` is absent
# on purpose: a replaced order lives on as its replacement.
RESTING = {"new", "accepted", "pending_new", "partially_filled", "held",
           "pending_cancel", "pending_replace", "accepted_for_bidding",
           "calculated", "stopped", "suspended", "done_for_day"}
DONE = {"filled": "filled", "canceled": "cancelled", "expired": "expired",
        "rejected": "cancelled"}

# client_order_id prefixes for orders the ADAPTER makes. They survive a
# restart at the broker, so a fresh process still knows a stop it placed
# from a stranger's order.
P_CLOSE, P_FLAT, P_SL, P_TP, P_REPL = ("brue-close-", "brue-flat-",
                                        "brue-sl-", "brue-tp-", "brue-r:")
D0 = Decimal(0)
# `order.place` refusals that depend on the REQUEST alone. Every other
# refusal depends on the account, the clock or a price, and is only given
# once Alpaca has said the client id is not an order it already holds.
STATELESS = ("unknown_symbol", "unsupported", "invalid_expiry",
             "x_alpaca_bad_request", "x_alpaca_invalid_price")


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[alpaca] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Numbers and times. Converted once, at the boundary.
# ---------------------------------------------------------------------------

def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dec(v) -> Decimal:
    """A broker number (string, int or float) as an exact Decimal; 0 if
    absent. Floats go through repr so 0.1 is 0.1, not 0.1000000000000000055."""
    if v is None or v == "":
        return D0
    try:
        return Decimal(repr(v) if isinstance(v, float) else str(v))
    except (decimal.InvalidOperation, ValueError):
        return D0


def _fixed(v, places: int = 9) -> str:
    """Fixed-point text for the wire: never an exponent (`1e-05` is not a
    quantity Alpaca reads), at most `places` decimals, no trailing zeros."""
    d = _dec(v).quantize(Decimal(1).scaleb(-places),
                         rounding=decimal.ROUND_HALF_EVEN)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _decimals(text) -> int:
    """How many decimal places a broker increment such as "0.0001" has."""
    d = _dec(text)
    if d <= 0:
        return 0
    return max(0, -d.normalize().as_tuple().exponent)


def step_ok(qty: float, step: float) -> bool:
    """Is `qty` a whole multiple of `step`, as far as a float can say?

    Quantities travel as JSON floats, so `round(n / step) * step` arrives
    as 65.88930260400001 and an exact test refuses honest orders. The test
    is on the distance to the nearest multiple, and allows what float
    arithmetic itself can put there (a few units in the last place of the
    number) and no more, so `min_qty + step / 3` is still refused, also
    for a coin whose minimum is 200,000 units with nine decimals.
    """
    if not (step > 0) or not (qty > 0) or qty != qty or qty == math.inf:
        return False
    nearest = round(qty / step) * step
    return abs(qty - nearest) <= max(4 * math.ulp(qty), step * 1e-6)


def snap(qty: float, step: float) -> Decimal:
    """`qty` moved onto the step grid exactly, for sending."""
    s = _dec(step)
    return (_dec(qty) / s).to_integral_value(decimal.ROUND_HALF_EVEN) * s


_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)(?:[Tt ](\d\d):(\d\d):(\d\d)"
                 r"(?:\.(\d+))?\s*(Z|z|[+-]\d\d:?\d\d)?)?$")


def parse_ts(text) -> int | None:
    """RFC 3339 to UTC epoch milliseconds, or None.

    Written out because Alpaca sends nine fractional digits and, on the
    clock, a `-05:00` offset: `datetime.fromisoformat` reads neither before
    Python 3.11. A bare date is midnight UTC; no zone at all is UTC.
    """
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


def iso_ms(ms: int) -> str:
    """UTC epoch milliseconds to the RFC 3339 form Alpaca takes."""
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000))
            + ".%03dZ" % (ms % 1000))


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


# ---------------------------------------------------------------------------
# Paper or live: decided cold, from configuration alone.
# ---------------------------------------------------------------------------

def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _paper_endpoint(url: str) -> bool:
    """No real money can move through this endpoint: it is Alpaca's paper
    host by its exact name, or this machine (a test stand-in). A trailing
    dot, an address, a forwarder: all of those are NOT known to be paper."""
    host = _host(url)
    return host == PAPER_HOST or _loopback(host)


def _credential_fault(label: str, value: str) -> str:
    """Why `value` cannot be an Alpaca key, in words that never contain it.

    Checked once, here. A pasted secret with a line break in it would
    otherwise reach http.client, whose error message quotes the header
    value: the secret, on its way to the platform (SPEC 8.5).
    """
    if any(c.isspace() for c in value):
        return ("the %s has a space or a line break inside it; paste it "
                "again as one unbroken line" % label)
    if not all(33 <= ord(c) <= 126 for c in value):
        return ("the %s has a character in it that no Alpaca key has; "
                "paste it again" % label)
    return ""


def resolve_environment(argv, environ) -> dict:
    """Which Alpaca this process talks to, with no network (SPEC 3, 8.2).

    LIVE IS OPT-IN. The environment is paper unless `--live` or
    ALPACA_ENV=live is given (a flag wins over the variable; both flags
    together are live). The key's prefix selects nothing: Alpaca itself
    calls it a hint, and a wrong pair is simply refused by the host.

    `mode` is a separate question, answered by the ENDPOINTS: it is
    "paper" only when the trading host and the stream host are each
    `paper-api.alpaca.markets` exactly or a loopback address. Anything
    else is "live" whatever the flags say, so a forwarder to the live
    host can never trade real money as "paper".
    """
    key = (environ.get("BRUE_CRED_API_KEY")
           or environ.get("ALPACA_API_KEY") or "").strip()
    secret = (environ.get("BRUE_CRED_API_SECRET")
              or environ.get("ALPACA_API_SECRET") or "").strip()
    fault = (_credential_fault("API key ID", key)
             or _credential_fault("API secret key", secret))
    argv = list(argv or [])
    paper_flag, live_flag = "--paper" in argv, "--live" in argv
    named = (environ.get("ALPACA_ENV") or "").strip().lower()
    if paper_flag and live_flag:
        env, why = "live", "both --paper and --live given; live assumed"
    elif live_flag:
        env, why = "live", "flag --live"
    elif paper_flag:
        env, why = "paper", "flag --paper"
    elif named == "live":
        env, why = "live", "ALPACA_ENV=live"
    elif named == "paper":
        env, why = "paper", "ALPACA_ENV=paper"
    elif named:
        env, why = "paper", ("ALPACA_ENV is neither paper nor live; paper "
                             "is used (live must be asked for)")
    else:
        env, why = "paper", "live was not asked for"
    trading = (environ.get("ALPACA_TRADING_URL")
               or (PAPER_TRADING_URL if env == "paper"
                   else LIVE_TRADING_URL)).rstrip("/")
    stream = (environ.get("ALPACA_STREAM_URL")
              or (PAPER_STREAM_URL if env == "paper" else LIVE_STREAM_URL))
    data = (environ.get("ALPACA_DATA_URL") or DATA_URL).rstrip("/")
    mode = ("paper" if _paper_endpoint(trading) and _paper_endpoint(stream)
            else "live")
    feed = (environ.get("ALPACA_FEED") or "auto").strip().lower()
    return {"env": env, "why": why, "mode": mode, "key": key,
            "secret": secret, "cred_fault": fault,
            "trading_url": trading, "data_url": data,
            "stream_url": stream,
            "feed": feed if feed in ("iex", "sip", "auto") else "auto"}


# ---------------------------------------------------------------------------
# RFC 6455 client. Standard library only: socket, ssl, struct, base64, hashlib.
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

    Alpaca's paper order stream sends every message, the answers to `auth`
    and `listen` included, as a BINARY frame holding UTF-8 JSON. A client
    that only delivers text frames connects, authenticates and then hears
    nothing, for ever. Text and binary messages are therefore both
    delivered, decoded as UTF-8.

    The other things that break hand-written clients are handled once,
    here: client frames are masked; the 16- and 64-bit length forms;
    fragmented messages, with control frames arriving between the pieces;
    pings answered with their own payload. A frame is taken out of the
    buffer only when all of it has arrived, so a read that times out on a
    quiet stream leaves the connection usable and the caller can ping.

    Reads block. `close()` is safe from another thread: it shuts the
    socket down, which makes the blocked reader raise.
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
                     "Sec-WebSocket-Version: 13"]
            # No Sec-WebSocket-Extensions: compression is negotiated, and a
            # client that does not offer it is sent plain frames.
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
                # The only proof that what answered is a WebSocket endpoint
                # and not a proxy that happened to send a 101.
                raise WSClosed("bad Sec-WebSocket-Accept")
            self._sock.settimeout(timeout)
        except BaseException:
            try:
                self._sock.close()
            except OSError:
                pass
            raise

    # -- reading ------------------------------------------------------
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
        """One whole frame out of the buffer, or None if it is not all
        here yet. Nothing is consumed until the frame is complete."""
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
            # A server must not mask; unmasking anyway beats garbage.
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return bool(b0 & 0x80), b0 & 0x0F, data

    def recv(self, timeout: float | None = None):
        """The next message as text, or None once the peer has closed.

        Raises WSTimeout when nothing complete arrived within `timeout`
        (or the connection's own); whatever did arrive stays buffered.
        """
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
            # anything else (a stray continuation, a reserved opcode) is
            # ignored, per the forward-compatibility rule

    # -- writing ------------------------------------------------------
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
        # Client frames MUST be masked (RFC 6455 5.3), a fresh key each.
        mask = os.urandom(4)
        header += mask
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._wlock:
            self._sock.sendall(bytes(header) + body)

    def send_json(self, obj):
        self._frame(_OP_TEXT, json.dumps(obj).encode("utf-8"))

    def ping(self, payload: bytes = b""):
        self._frame(_OP_PING, payload)

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
# Alpaca's refusals, and what they are in the SPEC's vocabulary.
# ---------------------------------------------------------------------------

class AlpacaError(Exception):
    """Alpaca answered with an error, or could not be reached (status 0).

    Carries Alpaca's own message and nothing from the request: a handler
    exception is forwarded to the platform verbatim, so it must never be
    able to hold a key.
    """

    def __init__(self, status: int, message: str, code=None, body=None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.body = body if isinstance(body, dict) else {}
        self.retry_after = retry_after


# Alpaca's numeric `code` is not one per reason (40310000 is buying power,
# wash trade, minimum cost, balance and fractionability alike), so the
# reason is read from the MESSAGE. First match wins. Each row is
# (text in the message, SPEC code, `like`, retryable). A code outside the
# closed set is sent as an `x_alpaca_` extension, with `like` naming the
# standard behaviour where one is right (SPEC 6.1).
ERROR_MAP = (
    ("client_order_id must be unique", "duplicate_client_id", None, False),
    ("client_order_id must be", "x_alpaca_client_id", None, False),
    ("invalid order_id", "not_found", None, False),
    ("not found", "not_found", None, False),
    ("could not find asset", "unknown_symbol", None, False),
    ("position does not exist", "not_found", None, False),
    ("is not tradable", "trade_restricted", None, False),
    ("is not active", "trade_restricted", None, False),
    ("cannot be sold short", "trade_restricted", None, False),
    ("not allowed to short", "trade_restricted", None, False),
    ("cannot open a short sell", "trade_restricted", None, False),
    ("insufficient buying power", "insufficient_margin", None, False),
    ("insufficient qty available", "x_alpaca_qty_held", "invalid_qty", False),
    ("position intent mismatch", "x_alpaca_nothing_to_close", "not_found",
     False),
    ("cost basis must be", "invalid_qty", None, False),
    ("notional must be", "invalid_qty", None, False),
    ("exceeds max notional", "invalid_qty", None, False),
    ("qty must be", "invalid_qty", None, False),
    ("qty or notional is required", "invalid_qty", None, False),
    ("is not fractionable", "invalid_qty", None, False),
    ("fractional orders", "invalid_qty", None, False),
    ("sub-penny", "x_alpaca_invalid_price", None, False),
    ("potential wash trade", "x_alpaca_wash_trade", "trade_restricted",
     False),
    ("time_in_force", "invalid_expiry", None, False),
    ("invalid order type", "unsupported", None, False),
    ("extended hours", "unsupported", None, False),
    ("order is not open", "not_resting", None, False),
    ("stop price", "invalid_stops", None, False),
    ("stop_price", "invalid_stops", None, False),
    ("take_profit", "invalid_stops", None, False),
    ("stop_loss", "invalid_stops", None, False),
    ("limit_price", "x_alpaca_invalid_price", None, False),
    ("not authorized to trade", "trade_restricted", None, False),
    ("not allowed to trade", "trade_restricted", None, False),
    ("not allowed for account", "trade_restricted", None, False),
    ("liquidation only", "trade_restricted", None, False),
    ("pattern day trading", "trade_restricted", None, False),
    ("subscription does not permit", "x_alpaca_subscription", None, False),
    ("rate limit", "rate_limited", None, True),
)


def to_adapter_error(e: AlpacaError, env: str = "", side: str = "",
                     quote_ccy: str = "USD") -> AdapterError:
    """One Alpaca refusal as the error a platform can act on. Alpaca's own
    words are always kept in the message."""
    msg = e.message or ""
    low = msg.lower()
    said = "Alpaca: " + msg if msg else "Alpaca answered HTTP %d" % e.status
    if e.status == 0:
        if e.code == "flatten":
            return AdapterError(
                "x_alpaca_flattening", "flatten_all was asked for while "
                "this call was being answered, and it was stopped. What it "
                "had already asked of Alpaca stands; flatten_all is "
                "cancelling every order and closing every position.",
                retryable=True, like="frozen")
        if e.code == "deadline":
            return AdapterError(
                "internal", "Alpaca was too slow: the %d s this call has "
                "ran out before it was finished. Ask again; the same "
                "client_id is safe." % HANDLER_BUDGET, retryable=True)
        return AdapterError("internal", "Alpaca could not be reached: "
                            + msg, retryable=True)
    if e.status == 401 or (e.status == 403 and e.code is None
                           and "subscription" not in low):
        # Bad keys come back as 401 or as a 403 with no `code` at all.
        log("key refused on the %s host (HTTP %d %s); this adapter talks to "
            "Alpaca's paper host unless --live or ALPACA_ENV=live is given"
            % (env or "?", e.status, msg))
        return AdapterError(
            "unauthorized", "Alpaca refused this API key on its %s host. A "
            "paper key only works for paper trading and a live key only "
            "for live trading: check that the key ID and the secret key "
            "are the pair Alpaca shows for this account." % (env or "?"))
    if e.status == 429:
        if e.code == "budget":
            # Not Alpaca's refusal: this adapter did not send the
            # request (its own pace, its own share of the minute, or a
            # pause Alpaca asked for earlier). Said as whose it is.
            return AdapterError("rate_limited", "not sent, by this "
                                "adapter's own limit: " + msg,
                                retryable=True)
        return AdapterError("rate_limited", said, retryable=True)
    if "insufficient balance" in low:
        # One sentence, two meanings. On a buy the account lacks the quote
        # currency: it cannot afford the order. On a sell it holds less
        # coin than it is selling, which on a long-only instrument is an
        # attempt to go short.
        if side == "sell" and ("for " + quote_ccy.lower()) not in low:
            return AdapterError("trade_restricted", said)
        return AdapterError("insufficient_margin", said)
    if "asset" in low and "not found" in low:
        return AdapterError("unknown_symbol", said)
    for text, code, like, retryable in ERROR_MAP:
        if text in low:
            return AdapterError(code, said, retryable=retryable, like=like)
    if e.status >= 500:
        return AdapterError("internal", said, retryable=True)
    if e.status == 404:
        return AdapterError("not_found", said)
    if e.status == 403:
        # Alpaca's 403 is "this account may not do that now".
        return AdapterError("x_alpaca_403", said, like="trade_restricted")
    return AdapterError("x_alpaca_%d" % e.status, said)


class _NothingToClose(AdapterError):
    """Alpaca refused an order this adapter had marked as a CLOSE
    (`position_intent`): there is nothing, or not that much, left on
    that side of the symbol to close. Not an order that failed, and
    never sent again as it is: whoever asked reads the position again
    and answers from what is there (see AlpacaAdapter._post_close)."""

    def __init__(self, said: str):
        super().__init__("x_alpaca_nothing_to_close", said, like="not_found")


def nothing_to_close(e: AlpacaError, body: dict) -> bool:
    """Is this Alpaca's answer to a closing order that found nothing to
    close? Seen on paper (2026-11-11): 422 "position intent mismatch,
    inferred: sell_to_open, specified: sell_to_close", shares and crypto
    alike, also with the exchange shut; and for crypto, where the balance
    is checked first, 403 "insufficient balance for BTC (requested: ..,
    available: ..)" when less of the coin is held than the close asks
    for (a coin cannot be sold short)."""
    intent = str((body or {}).get("position_intent") or "")
    if not intent.endswith("_to_close"):
        return False
    low = (e.message or "").lower()
    if e.status == 422 and "position intent mismatch" in low:
        return True
    # (on a sell, a balance that is short and is not the account's own
    # currency is the coin's: see to_adapter_error)
    return (e.status == 403 and "insufficient balance" in low
            and body.get("side") == "sell" and "for usd" not in low)


# ---------------------------------------------------------------------------
# REST client: two hosts, each with its own rolling request budget.
# ---------------------------------------------------------------------------

class _Budget:
    """What the adapter may still ask of Alpaca, kept in one place.

    Alpaca counts its two hosts separately (200 requests a minute on each,
    by its own headers), and so does this: PER HOST a rolling minute and a
    pace per second. Orders and the account live on the trading host,
    prices and bars on the data host, so a chart that loads and the
    prices of a watch list take nothing from an order or from a platform
    that polls its positions. (Until 2026-11-10 the pace was one for both
    hosts, and a platform's own polling filled it before any chart or
    order was asked for.)

    Three kinds of caller draw on a host's minute: the adapter's own
    threads stop first, reads made for a handler next, and only calls that
    change something (an order, a cancel, a close) may use the last part,
    so `flatten_all` is never starved by polling (SPEC 8.1).

    `take` waits only as long as its caller can afford; a handler is told
    `rate_limited`, and truthfully why, rather than slept through a spent
    minute.
    """

    def __init__(self, per_minute=BUDGET_PER_MINUTE, reserve=BUDGET_RESERVE,
                 background=BUDGET_BACKGROUND, per_second=PACE_PER_SECOND):
        self.per_minute, self.reserve = per_minute, reserve
        self.background, self.per_second = background, per_second
        self.background_data = max(background, min(
            BUDGET_BACKGROUND_DATA, per_minute - reserve))
        self._lock = threading.Lock()
        self._minute: dict[str, collections.deque] = {}
        self._second: dict[str, collections.deque] = {}
        self._blocked: dict[str, float] = {}
        # Everyone waiting for a place right now (see take and _standing),
        # and per host the kind of request that got the last one.
        self._queue: list = []
        self._last: dict[str, str] = {}
        # Calls that change something (an order, a close) being answered
        # now: see in_hand and _pace.
        self._in_hand = 0

    @property
    def _urgent(self) -> int:
        """Callers of a handler's rank or above that are waiting now."""
        with self._lock:
            return sum(1 for w in self._queue if w["base"] >= 1)

    def _standing(self, w: dict, now: float) -> tuple:
        """Where a waiter stands for the next place on ITS host: (rank,
        seniority).

        Handlers run side by side, so a platform that polls (the
        Terminal's dock: account, positions and orders every two seconds)
        can ask for nearly a whole pace by itself, and the repair of
        several stops at once can ask for all of it. Strict rank would
        then give the others NOTHING (seen: charts of 27 bars, "quotes
        unavailable", a close refused while seven stops were put back).
        So:

          3    flatten_all, always; and putting a stop back, on every
               second place while a handler waits too
          2.5  a handler's read that has waited a whole second, on every
               second place: a platform that moves a stop as fast as it
               is answered asks for the whole pace with rank 2, and every
               read behind it is refused without this rank (in tests: four of
               five positions.list)
          2    a request made for an order: before any read
          1.7  the newest end of a chart (`head`): one request, and
               without it the chart has no answer at all
          1.5  a chart's windows WHILE ITS CALLER WAITS, on every second
               place: half the data host's pace for the few seconds a
               chart is loading
          1    a handler's read; and a quote poll that has waited 0.7 s
               (prices on a screen are not background for long). Among
               these the one that has waited longest goes first.
          0.9  putting a stop back, on the other places
          0.5  a chart's windows on the other places
          0.4  the windows of a chart whose caller has been ANSWERED and
               which reads on at speed behind the answer: after every
               chart somebody is still waiting for (asked of the waiter
               as it stands NOW, `live`: a window queued while its caller
               waited steps back the moment the caller is answered)

        Among a chart's windows the one nearest to now goes first
        (`order`), whichever chart asked first: a chart just asked for
        got its first window behind the sixth of the one before it, and a
        three-second answer with the newest end alone (traced).
          0    the adapter's own upkeep, and a quote poll just begun
        """
        rank, last = w["base"], self._last.get(w["host"])
        if w["kind"] == "head":
            rank = max(rank, 1.7)
        elif w["kind"] == "bulk":
            if w["live"] is not None and not w["live"]():
                rank = 0.4
            else:
                rank = 0.5 if last == "bulk" else 1.5
        elif w["kind"] == "protect" and last == "protect":
            rank = 0.9
        elif w["kind"] == "quotes" and now - w["since"] >= 0.7:
            rank = 1
        elif self._aged(w, now):
            rank = 2.5
        return rank, -w["order"], -w["since"]

    def _aged(self, w: dict, now: float) -> bool:
        return (w["kind"] == "read" and w["base"] == 1
                and now - w["since"] >= 1.0
                and self._last.get(w["host"]) != "aged")

    def _pace(self, w: dict, now: float) -> int:
        """Requests a second this waiter may fill on its host. The
        adapter's own threads leave one of every second to the handlers; a
        quote poll that has waited 0.7 s may take that one too (else a
        chart, which fills every second, would stop the prices
        altogether: seen against the stand-in)."""
        pace = w["pace"]
        if w["kind"] == "quotes" and now - w["since"] >= 0.7:
            pace = self.per_second
        if self._in_hand and w["base"] < 2 and now - w["since"] < 1.0:
            # An order's work is a chain of requests (the price, the
            # order, the look for its fill), each asked when the one
            # before has answered. Going first among those WAITING is not
            # enough for it: between two of its requests it is not
            # waiting, and whoever always is took every place that came
            # free (seen: a second lost per request). So while such a
            # call is in hand, everything else leaves one request of
            # every second untaken, and its next request finds a place at
            # once. (Whoever has waited a second for that is let in: a
            # platform that moves a stop as fast as it is answered would
            # otherwise keep every read out for as long as it goes on.
            # In tests: four of five positions.list refused.)
            pace = min(pace, max(1, self.per_second - 1))
        return pace

    def _gap(self, w: dict) -> float:
        """Least time since the last request to this host (anyone's)
        before this waiter may send one. Three requests a second taken the
        instant they come free go out as a burst, once a second, and
        whoever arrives in between waits for the next burst: up to a whole
        second, an order like anyone else (seen: 0.4 to 0.8 s before an
        order's first request, with a platform that polls). So everything
        below an order goes out evenly, a third of a second apart, and so
        do the requests that put stops back (several symbols at once
        would otherwise be the burst); an order and flatten take a place
        the moment the second has one."""
        if w["base"] >= 2 and w["kind"] != "protect":
            return 0.0
        return 1.0 / self.per_second - 0.01

    def in_hand(self, delta: int):
        """A call that changes something was read (+1) or answered (-1)."""
        with self._lock:
            self._in_hand = max(0, self._in_hand + delta)

    def block(self, host: str, seconds: float):
        """Alpaca said 429: nothing more to this host until then. Capped:
        one answer (or one bad header) must not silence a host for an
        hour. `flatten_all` does not honour it at all (see take)."""
        with self._lock:
            self._blocked[host] = time.monotonic() + max(
                0.0, min(seconds, BLOCK_CAP))

    def blocked_for(self, host: str) -> float:
        with self._lock:
            return max(0.0, self._blocked.get(host, 0.0) - time.monotonic())

    def _wait_needed(self, host: str, limit: int, now: float,
                     pace: int | None = None, blocked: bool = True,
                     gap: float = 0.0) -> float:
        pace = pace or self.per_second
        win = self._minute.setdefault(host, collections.deque())
        sec = self._second.setdefault(host, collections.deque())
        while win and now - win[0] >= 60.0:
            win.popleft()
        while sec and now - sec[0] >= 1.0:
            sec.popleft()
        wait = max(0.0, self._blocked.get(host, 0.0) - now) if blocked \
            else 0.0
        if len(win) >= limit:
            wait = max(wait, 60.0 - (now - win[len(win) - limit]))
        if len(sec) >= pace:
            wait = max(wait, 1.0 - (now - sec[len(sec) - pace]))
        if gap and sec:
            wait = max(wait, gap - (now - sec[-1]))
        return wait

    def take(self, host: str, kind: str = "read", max_wait: float = 1.5,
             rank: int = 0, order: int = 0, live=None):
        # `bulk` and `idle` are the long reads of bars: they stop well
        # before quotes do, so a chart cannot silence the prices.
        # `flatten` is the panic button (SPEC 8.1): it is never refused
        # HERE. Not for a 429 some other call was given a while ago (Alpaca
        # may well serve it; if not, Alpaca says so), and not for a minute
        # this process has already spent: only the pace per second holds.
        # `protect` is the repair of a stop or target: it draws on the
        # part of the minute kept for writes, like an order.
        limit = {"write": self.per_minute, "flatten": 10 ** 6,
                 "protect": self.per_minute,
                 # No order is ever sent to the data host, so the part of
                 # its minute kept back for writes is free for the newest
                 # end of a chart.
                 "head": self.per_minute,
                 "read": self.per_minute - self.reserve,
                 "background": self.background_data if host == "data"
                 else self.background,
                 # Prices on a screen: on the data host they stop last
                 # but for a chart's newest end. (A mixed list of 40
                 # symbols asks 80 times a minute; under the general
                 # background tier a chart load left it 30, and the
                 # session said "quotes unavailable": seen on paper.)
                 "quotes": self.per_minute - 10 if host == "data"
                 else self.background,
                 "bulk": min(BUDGET_BULK, self.per_minute - self.reserve),
                 "idle": min(BUDGET_IDLE, self.background),
                 # The watch for orders of unknown fate stands back
                 # BEFORE the backstop does: with the stream down the
                 # backstop's reads are how fills are heard (and they
                 # show a late order too: it reads the open orders and
                 # the fills), and a minute that is short must be theirs.
                 "watch": max(1, self.background - BUDGET_WATCH_ROOM)}[kind]
        # The adapter's own threads leave one request of every second
        # free, so a handler is never queued behind quote polling.
        pace = (max(1, self.per_second - 1)
                if kind in ("background", "quotes", "idle", "watch")
                else self.per_second)
        # `rank` 2 is given by the lane that answers orders, to every
        # request made for one. See _standing for who goes first.
        me = {"base": max(rank, 3 if kind in ("protect", "flatten")
                          else 1 if kind in ("write", "read", "head")
                          else 0),
              "kind": kind, "since": time.monotonic(), "host": host,
              "limit": limit, "pace": pace, "order": order, "live": live}
        deadline = me["since"] + max_wait
        with self._lock:
            self._queue.append(me)
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    wait = self._wait_needed(host, limit, now,
                                             self._pace(me, now),
                                             kind != "flatten",
                                             self._gap(me))
                    paused = (kind != "flatten" and wait > 0 and wait <= max(
                        0.0, self._blocked.get(host, 0.0) - now))
                    spent = len(self._minute[host]) >= limit
                    if wait <= 0:
                        # A place is free for this caller. It is taken only
                        # if nobody who could take it too stands ahead.
                        # Whether another waiter COULD take it is worked
                        # out here and now, from its own limits: a flag it
                        # set at its last look would be stale by the very
                        # milliseconds in which a place comes free, and the
                        # first to wake (not the first in line) would get
                        # it every time.
                        mine = self._standing(me, now)
                        if any(w is not me and w["host"] == host
                               and self._standing(w, now) > mine
                               and self._wait_needed(
                                   host, w["limit"], now,
                                   self._pace(w, now),
                                   w["kind"] != "flatten",
                                   self._gap(w)) <= 0
                               for w in self._queue):
                            wait = 0.02
                    if wait <= 0:
                        self._minute[host].append(now)
                        self._second[host].append(now)
                        self._last[host] = ("aged" if self._aged(me, now)
                                            else kind)
                        return
                if now + wait > deadline:
                    # code "budget": refused HERE, before anything was
                    # sent. Said as it is: a pause Alpaca asked for, a
                    # minute this process has used up, or a second in
                    # which more urgent requests went first.
                    if paused:
                        text = ("Alpaca asked for a pause on its %s host "
                                "(HTTP 429); retry in %.1fs" % (host, wait))
                    elif spent:
                        text = ("this minute's requests to Alpaca's %s host "
                                "are used up (the adapter keeps to %d a "
                                "minute for this kind of call); retry in "
                                "%.1fs" % (host, limit, wait))
                    else:
                        text = ("the adapter sends Alpaca's %s host %d "
                                "requests a second and more urgent ones "
                                "went first; nothing was sent; retry in "
                                "%.1fs" % (host, self.per_second, wait))
                    raise AlpacaError(429, text, code="budget",
                                      retry_after=wait)
                time.sleep(min(wait, 0.25) + 0.005)
        finally:
            with self._lock:
                self._queue.remove(me)


class AlpacaClient:
    """Alpaca over HTTPS. One request, one connection; errors typed."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = {"trading": cfg["trading_url"], "data": cfg["data_url"]}
        self.budget = _Budget()
        self._ctx = ssl.create_default_context()
        self.last_request_id = ""
        # Alpaca's clock less ours, in ms, from the Date header of its
        # answers (whole seconds): times of its ledger are compared with
        # it, never with a local clock that may be ahead.
        self.clock_skew_ms: int | None = None
        # The deadline of the handler running on THIS thread (monotonic),
        # or None on the adapter's own threads. See `deadline`.
        self._tl = threading.local()
        # Set while flatten_all is asked for or running: a call that
        # changes something and is still being answered makes no further
        # request (see set_request, `yields`).
        self.stand_down = threading.Event()

    # -- one deadline per handler -------------------------------------
    def set_deadline(self, seconds: float | None):
        self._tl.deadline = (None if seconds is None
                             else time.monotonic() + seconds)
        self._tl.read_at, self._tl.rank, self._tl.yields = None, 0, False

    def _yield_to_flatten(self):
        if self.stand_down.is_set() and getattr(self._tl, "yields", False):
            # code "flatten": refused HERE, before anything was sent
            raise AlpacaError(0, "flatten_all was asked for",
                              code="flatten")

    def set_request(self, read_at: float, budget: float, rank: int = 1,
                    floor: float = 0.0, yields: bool = False):
        """The request this thread is about to answer was read off the
        pipe at `read_at` and has `budget` seconds FROM THEN: what it
        spent queued behind other requests is part of its time, because
        the platform's own wait started when it sent the line. `floor`:
        seconds it gets from now whatever it has waited (flatten_all)."""
        self._tl.read_at, self._tl.rank = read_at, rank
        # `yields`: this thread answers a call that changes something. If
        # flatten_all is asked for meanwhile, it stops at its next request
        # (the panic button does not wait behind it, SPEC 8.1).
        self._tl.yields = yields
        self._tl.deadline = max(read_at + budget, time.monotonic() + floor)

    def read_at(self) -> float | None:
        """When the request being answered on this thread was read."""
        return getattr(self._tl, "read_at", None)

    def time_left(self) -> float | None:
        """Seconds the handler on this thread still has; None = no limit."""
        d = getattr(self._tl, "deadline", None)
        return None if d is None else d - time.monotonic()

    def pause(self, seconds: float):
        """Sleep, but never past the handler's deadline."""
        self._yield_to_flatten()
        left = self.time_left()
        if left is not None and left <= 0.05:
            raise AlpacaError(0, "the time for this call is used up",
                              code="deadline")
        time.sleep(seconds if left is None else min(seconds, left))

    def scrub(self, text: str) -> str:
        """`text` with the key and the secret taken out, whatever put them
        there. Nothing built from a request may reach a message unscrubbed
        (SPEC 8.5)."""
        for value in (self.cfg.get("secret"), self.cfg.get("key")):
            if value and len(value) >= 4 and value in text:
                text = text.replace(value, "[hidden]")
        return text

    def call(self, method: str, host: str, path: str, query: dict | None = None,
             body=None, kind: str = "read", timeout: float = HANDLER_TIMEOUT,
             max_wait: float = 2.5, order: int = 0, live=None, ready=None,
             until: float | None = None):
        """Parsed JSON, or None for an empty answer. Raises AlpacaError.
        `ready` is called when this request has its place in the pace,
        before it is sent (see AlpacaAdapter._account_now). `until`
        (monotonic): by when this request must have ended, however long
        it waited for its place; with no time left it is not sent."""
        if not self.cfg["key"] or not self.cfg["secret"]:
            raise AlpacaError(401, "the API key ID or the secret key is "
                              "missing")
        if self.cfg.get("cred_fault"):
            raise AlpacaError(401, self.cfg["cred_fault"])
        self._yield_to_flatten()
        left = self.time_left()
        if left is not None:
            # A handler's requests share ITS deadline: the tenth call of a
            # chain gets what the first nine left, not eight seconds more.
            if left <= 0.05:
                raise AlpacaError(0, "the time for this call is used up",
                                  code="deadline")
            if kind == "read":
                # A handler's read waits its turn inside the handler's own
                # time (handlers run side by side and share one pace with
                # a chart's history and the prices). It is refused at once
                # only when the wait is KNOWN to be longer than that: a
                # spent minute, a pause Alpaca asked for (see take).
                max_wait = max(max_wait, left - READ_MARGIN)
            timeout, max_wait = min(timeout, left), min(max_wait, left)
        url = self.base[host] + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None},
                safe="/,:")
        self.budget.take(host, kind, max_wait,
                         getattr(self._tl, "rank", 0) or 0, order, live)
        if until is not None:
            room = until - time.monotonic()
            if room <= 0.2:
                # code "deadline": refused HERE, before anything was sent
                raise AlpacaError(0, "the time for this call is used up",
                                  code="deadline")
            timeout = min(timeout, room)
        if ready is not None:
            ready()
        data = None if body is None else json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={
                "APCA-API-KEY-ID": self.cfg["key"],
                "APCA-API-SECRET-KEY": self.cfg["secret"],
                # Named: several edges answer the standard library's default
                # agent with a plain-text 403 that reads like a bad key.
                "User-Agent": USER_AGENT, "Accept": "application/json",
                "Content-Type": "application/json"})
            kw = {"timeout": timeout}
            if url.startswith("https"):
                kw["context"] = self._ctx
            with urllib.request.urlopen(req, **kw) as r:
                self.last_request_id = r.headers.get("X-Request-ID") or ""
                self._note_date(r.headers.get("Date"))
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()        # the whole body: the reason is in it
            except (OSError, http.client.HTTPException):
                raw = b""
            raise self._error(host, e.code, e.headers, raw) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, OSError):
                text = "%s: %s" % (type(reason).__name__, reason)
            else:
                text = type(reason).__name__ if not isinstance(
                    reason, str) else "request failed"
            raise AlpacaError(0, self.scrub(text)) from None
        except http.client.HTTPException as e:
            # The answer broke off (IncompleteRead, a bad status line...).
            # Whether Alpaca carried the request out is NOT known.
            raise AlpacaError(0, "the connection broke while Alpaca was "
                              "answering (%s)" % type(e).__name__) from None
        except ValueError as e:
            # Never the exception's own text: http.client's "Invalid header
            # value" QUOTES the value, and the value is the secret.
            raise AlpacaError(0, "the request could not be made (%s)"
                              % type(e).__name__) from None
        if not raw:
            return None
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError:
            raise AlpacaError(0, "Alpaca's answer was not JSON (%d bytes)"
                              % len(raw)) from None

    def _note_date(self, value):
        try:
            server = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return
        self.clock_skew_ms = int((server - time.time()) * 1000)

    def _error(self, host: str, status: int, headers, raw: bytes):
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"message": raw[:200].decode("utf-8", "replace")}
        if not isinstance(body, dict):
            body = {"message": str(body)[:200]}
        retry = None
        if status == 429:
            retry = self._retry_after(headers)
            self.budget.block(host, retry)
        return AlpacaError(status, self.scrub(str(body.get("message")
                                                  or "")),
                           body.get("code"), body, retry)

    @staticmethod
    def _retry_after(headers) -> float:
        """How long Alpaca asked us to stay away: Retry-After in seconds or
        as a date, else its X-RateLimit-Reset epoch, else the next minute."""
        now = time.time()
        value = (headers.get("Retry-After") or "").strip() if headers else ""
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(value).timestamp()
                               - now)
                except (TypeError, ValueError):
                    pass
        reset = (headers.get("X-Ratelimit-Reset") or "") if headers else ""
        try:
            return min(60.0, max(0.5, float(reset) - now))
        except ValueError:
            return 60.0 - now % 60.0 + 0.2


# ---------------------------------------------------------------------------
# The catalog: Alpaca's asset list as InstrumentSpecs. A pure function, so
# it can be tested without a broker.
# ---------------------------------------------------------------------------

def build_calendar(days, today: datetime.date) -> tuple[list, list]:
    """Alpaca's list of market days as (holidays, early_closes).

    The calendar holds trading days only, so a holiday is a weekday that is
    missing from it, and an early close is a day whose `close` is not 16:00.
    """
    by_date = {d.get("date"): d for d in days or [] if d.get("date")}
    if not by_date:
        return [], []
    last = max(by_date)
    holidays, day = [], today
    while day.isoformat() <= last:
        if day.weekday() < 5 and day.isoformat() not in by_date:
            holidays.append(day.isoformat())
        day += datetime.timedelta(days=1)
    early = [{"date": d["date"], "close": d["close"],
              "tz": "America/New_York"}
             for _, d in sorted(by_date.items())
             if d.get("close") and d["close"] < "16:00"]
    return holidays, early


def build_catalog(equities, cryptos, days, account,
                  today: datetime.date | None = None):
    """(rows, asset id -> symbol). Crypto first, then shares, each sorted.

    Crypto is first because it is the part of the catalog that is always
    open: a platform, or the conformance suite, that walks the list for
    something it can deal right now reaches it before 13,000 shut shares.
    Only active, tradable assets are listed.
    """
    account = account or {}
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    holidays, early = build_calendar(days, today)
    can_short = bool(account.get("shorting_enabled"))
    margin_2x = _num(account.get("multiplier"), 1.0) >= 2
    quote = str(account.get("currency") or "USD")
    taker = CRYPTO_TAKER_FEE.get(account.get("crypto_tier"))
    rows, ids = [], {}
    for a in sorted(cryptos or [], key=lambda x: str(x.get("symbol"))):
        sym = str(a.get("symbol") or "")
        base, _, ccy = sym.partition("/")
        if (a.get("status") != "active" or not a.get("tradable")
                or not base or ccy != quote):
            # BTC/USDT is paid for in USDT, which is a second position of
            # this account, not its cash: a trade the one-position-per-
            # symbol book cannot describe. Only pairs in the account's own
            # currency are listed.
            continue
        step = _dec(a.get("min_trade_increment")) or Decimal("0.000000001")
        low = _dec(a.get("min_order_size")) or step
        low = (low / step).to_integral_value(decimal.ROUND_CEILING) * step
        tick = _dec(a.get("price_increment")) or Decimal("0.000000001")
        row = {"symbol": sym, "name": str(a.get("name") or sym)[:60],
               "asset_class": "crypto", "base_ccy": base, "quote_ccy": ccy,
               "digits": _decimals(tick), "tick_size": float(tick),
               "lot_step": float(step), "min_qty": float(low),
               "max_qty": MAX_QTY, "min_notional": CRYPTO_MIN_NOTIONAL,
               "contract_size": 1, "margin_rate": 1.0,
               "margin_maintenance_rate": 1.0,
               "sessions": SESSION_ALWAYS, "holidays": [],
               "early_closes": [], "tradable": True, "shortable": False,
               "trade_mode": "long_only", "swap_mode": "none"}
        if taker is not None:
            row["fees"] = {"commission_per_lot": 0.0, "mode": "pct_notional",
                           "rate": taker, "min": 0.0}
        rows.append(row)
        ids[str(a.get("id"))] = sym
    for a in sorted(equities or [], key=lambda x: str(x.get("symbol"))):
        sym = str(a.get("symbol") or "")
        if (a.get("status") != "active" or not a.get("tradable") or not sym
                or a.get("class") not in (None, "us_equity")):
            continue
        marginable = bool(a.get("marginable")) and margin_2x
        maint = _num(a.get("margin_requirement_long")
                     or a.get("maintenance_margin_requirement"), 100.0) / 100
        # `borrow_status` replaces the older `easy_to_borrow`. Only easy-to-
        # borrow shares are offered short: paper has no locate for the rest.
        easy = (a.get("borrow_status") == "easy_to_borrow"
                if a.get("borrow_status") else bool(a.get("easy_to_borrow")))
        short = bool(a.get("shortable")) and easy and can_short
        row = {"symbol": sym, "name": str(a.get("name") or sym)[:60],
               "asset_class": "stock", "quote_ccy": quote, "digits": 2,
               "tick_size": 0.01, "lot_step": 1.0, "min_qty": 1.0,
               "max_qty": MAX_QTY, "contract_size": 1,
               "margin_rate": 0.5 if marginable else 1.0,
               "margin_maintenance_rate":
                   min(1.0, max(0.0, maint)) if marginable else 1.0,
               "sessions": SESSION_SHARES, "holidays": holidays,
               "early_closes": early, "tradable": True, "shortable": short,
               # Alpaca: a stop "has to be at least $0.01" beyond the
               # price it protects against; nearer is refused.
               "stops_level_points": 1}
        if not short:
            row["trade_mode"] = "long_only"
        rows.append(row)
        ids[str(a.get("id"))] = sym
    return rows, ids


# ---------------------------------------------------------------------------
# Bars: the closed bars of one symbol and timeframe, kept for a while.
# ---------------------------------------------------------------------------

class _BarStore:
    """Closed bars of one symbol and timeframe, as far back as was read.

    Laid out from an `anchor`, a multiple of four hours, so that no bar of
    any timeframe straddles a boundary: the HEAD is everything from the
    anchor to now; window 0 is the `window` milliseconds before the
    anchor, window 1 the stretch before that, and so on. Windows are read
    in any order by several threads. What may be handed out is the head
    plus the windows read WITHOUT A GAP from 0, so a slow or failed
    request in the middle is never a hole in somebody's chart.

    `window` None is a timeframe Alpaca serves whole in one request (the
    day): the head is then everything.
    """

    def __init__(self, anchor: int, window: int | None):
        self.cond = threading.Condition()
        self.rows: dict[int, list] = {}
        self.anchor, self.window = anchor, window
        self.head_floor = anchor      # how far back the newest end was read
        self.made = self.used = time.monotonic()
        self.head_at = 0.0
        self.done: set = set()
        self.empty: set = set()
        self.retry: list = []
        self.next = 0
        self.last: int | None = None      # no window beyond this one
        self.want = 0
        self.waiters: dict = {}       # request -> (count, deadline, turn)
        self.loaders = 0              # requests arranging their windows
        self.asking = 0               # callers not answered yet
        self.turns = 0                # ... of which hold a turn (BARS_LOADS)
        self.fast_until = 0.0
        self.extra = 0
        self.workers = 0
        self.quiet_busy = False
        self.head_busy = False
        self.head_error = None
        self.throttled = None
        self.fails = 0
        self.error = None
        self.dead = False

    def floor(self) -> int:
        """Bars at or after this time are held without a gap."""
        if self.window is None:
            return 0
        k = 0
        while k in self.done:
            k += 1
        if self.last is not None and k > self.last:
            return 0
        return min(self.head_floor, self.anchor - k * self.window)

    def have(self) -> int:
        floor = self.floor()
        return sum(1 for t in self.rows if t >= floor)

    def newest(self, count: int) -> list:
        """The newest `count` bars that may be handed out, oldest first."""
        floor = self.floor()
        return [self.rows[t] for t in sorted(
            t for t in self.rows if t >= floor)[-count:]]

    def finished(self) -> bool:
        """Nothing further back to read."""
        return (self.window is None
                or (self.last is not None and self.next > self.last)
                or self.anchor - self.next * self.window <= BARS_FIRST_MS)

    def take(self):
        """The number of the next window to read, or None."""
        if self.retry:
            return self.retry.pop(0)
        if self.finished():
            return None
        self.next += 1
        return self.next - 1

    def put(self, k: int, rows: list):
        """Window `k` was read whole. Several empty windows in a row mean
        the reading has gone back past the first bar there is."""
        for row in rows:
            self.rows[row[0]] = row
        self.done.add(k)
        if not rows:
            self.empty.add(k)
            if all(j in self.empty for j in range(
                    max(0, k - BARS_END_EMPTY + 1), k + 1)) and (
                    k + 1 >= BARS_END_EMPTY):
                self.last = k if self.last is None else min(self.last, k)
        if len(self.rows) > BARS_MAX_COUNT:
            # Bounded: the oldest go, and reading further back stops.
            for t in sorted(self.rows)[:len(self.rows) - BARS_MAX_COUNT]:
                del self.rows[t]
            self.last = max(self.done)


# ---------------------------------------------------------------------------
# Stops and targets: ONE owner.
# ---------------------------------------------------------------------------

class _StandDown(Exception):
    """The backstop gives way in the middle of a reconcile: a handler wants
    the symbol, or flatten_all is running. Nothing is lost by stopping:
    the symbol stays due and the next pass starts from the broker again."""


class _Protection:
    """Every stop and target of every position, owned in ONE place.

    The adapter used to let each handler arrange the exit orders itself
    (cancel here, forget there, re-make later from a list), and nothing
    compared the result with the broker: a stop could rest on a flat
    account, or a position could sit without the stop it was shown with.
    Now nothing outside this class places, moves or cancels an exit order,
    and what it keeps true is checked against Alpaca again and again:

      a. An exit order of ours with no position behind it, on the wrong
         side of it, or for more than it, is cancelled. If it is still
         there a moment later the session is degraded and the order is
         listed by `orders.list`, until it is gone.
      b. A position whose level is set and whose exit THIS ADAPTER took
         off (to re-size it after a partial close, to move it) is
         protected again. If that fails, it is said (`session.state`
         degraded, naming the symbol) and tried again with a growing
         pause. It is never forgotten: what is due is written down with
         its time and the position it is for, done before the adapter
         leaves on `bye` (or said, if there is no time), and put back at
         the next start if that is minutes away AND the position is
         still that one (same side, no larger, same cost a share,
         nothing bought into it since); otherwise it is said, with what
         was found, and nothing is placed.
         An exit that is filling in parts is never cancelled to correct
         the size of the other half of its pair: that one is brought to
         size where it stands.
         An exit that Alpaca reports cancelled or expired WITHOUT this
         adapter having asked (the account's owner in Alpaca's own app,
         another program) is NOT made again: the level is dropped, said
         once, no longer shown, and never comes back by itself. (Unless
         it was this adapter's own doing after all: a cancel-all of
         flatten_all's that Alpaca did not answer and carried out late.
         For ten minutes after such a request an exit that ends
         cancelled is put back, and that is said: see _judge.) An order
         that is not ours and holds the position's quantity is not fought.
      b2. Levels belong to the POSITION they were set for. The moment a
         fill takes the book to zero, or through it, they are gone: the
         next position in the symbol starts with none.
      c. `position.close`, `flatten_all`, `order.modify`, an `order.place`
         that carries levels, and the backstop's own repair all take ONE
         guard per symbol, so none of them can run between another's read
         of the position and its orders.
      d. A request is checked BEFORE the guard is used to release anything.
      e. At every start, reconnect and audit the exits are found again in
         Alpaca's own order lists (by the client ids this adapter gives
         them, the ids it wrote down, and as legs under an order of ours).

    `levels` is what the platform asked for. `reconcile` is the one
    function that makes Alpaca match it, always from a fresh read of the
    position and the symbol's open orders taken under the guard. Everyone
    else only says WHEN to look (`touch`): the stream thread records what
    it hears and never calls the broker; the backstop runs what is due.
    """

    def __init__(self, adapter):
        self.a = adapter
        self.levels: dict[str, dict] = {}     # sym -> {"sl", "tp"}
        self.due: dict[str, list] = {}        # sym -> [when, tries, stamp]
        self.issues: dict[str, str] = {}      # key -> what is wrong
        self.missing: dict[str, float] = {}   # sym -> since when unheld
        self.orphan: dict[str, float] = {}    # sym -> since when unbacked
        self.oversize: dict[str, float] = {}  # sym -> since when too large
        self.triggered: dict[str, float] = {}
        # sym -> times Alpaca itself ended an exit of ours (rejected it
        # after taking it); see reconcile: not a loop to spin in.
        self.died: dict[str, list] = {}
        # sym -> exits that ended cancelled or expired WITHOUT this
        # adapter asking, until reconcile has judged them (see _judge)
        self.gone: dict[str, list] = {}
        # sym -> {"sl", "tp", "since" (epoch ms), "stale"}: levels wanted
        # that no order carries. Written to the state file, so that a stop
        # that was due when the adapter stopped is not lost with it.
        self.unheld: dict[str, dict] = {}
        self.remake: set = set()      # symbols put back from an earlier run
        # sym -> when (epoch ms) the cancel-all of flatten_all's was sent
        # that took its exits off late: they are being put back (_judge)
        self.swept: dict[str, int] = {}
        self.executing: dict[str, float] = {}  # sym -> an exit is filling
        self._guards: dict[str, threading.RLock] = {}
        self._wanted: set = set()             # symbols a handler waits for
        self._stamp = 0
        self.flattening = False
        # Held by the repair thread for the length of one reconcile, so
        # that flatten_all can wait for a step in flight to come back.
        self.busy = threading.Lock()
        self.wake = threading.Event()       # there is something to look at

    def _kw(self) -> dict:
        """How this component's requests are made. On its own thread:
        ahead of any handler's call, and patient (an attempt that
        gave up for want of a slot would start again from its first
        read). Inside a handler: as that handler's own calls, under its
        deadline."""
        if self.a.client.time_left() is None:
            return {"kind": "protect", "max_wait": 6.0}
        return {"kind": "write"}

    # -- what is known here (caller holds the state lock) ---------------
    def _recs(self, sym: str, kinds) -> list:
        return [r for r in self.a._orders.values()
                if r["sym"] == sym and r["kind"] in kinds and not r["done"]]

    def exits(self, sym: str) -> list:
        """The exit orders of ours in `sym` that are open at Alpaca as far
        as this process knows, oldest first."""
        return sorted((r for r in self._recs(sym, ("sl", "tp"))
                       if not r.get("superseded")), key=lambda r: r["t0"])

    def _contingent(self, rec: dict) -> bool:
        """A leg whose own entry order is still working is not an exit
        yet: Alpaca holds it for a position that does not exist so far."""
        entry = self.a._orders.get(rec.get("entry") or "")
        return bool(entry) and not entry["done"]

    @staticmethod
    def _price(rec: dict):
        return _num(rec["o"].get("stop_price" if rec["kind"] == "sl"
                                 else "limit_price")) or None

    @staticmethod
    def _left(rec: dict) -> Decimal:
        return _dec(rec["o"].get("qty")) - _dec(rec["o"].get("filled_qty"))

    @staticmethod
    def _same(a, b) -> bool:
        """Two prices, compared as prices: 0.1 + 0.2 is 0.3 at Alpaca."""
        if a is None or b is None:
            return a is b
        return abs(a - b) < 1e-9

    def holding(self, sym: str) -> dict:
        """{"sl": price, "tp": price} of the levels an order at Alpaca is
        carrying NOW. An order whose cancel was asked for carries nothing.
        This, and not `levels`, is what `positions.list` shows."""
        out = {"sl": None, "tp": None}
        for r in self.exits(sym):
            if not r["cancel_req"]:
                out[r["kind"]] = self._price(r)
        return out

    def relevant(self, sym: str) -> bool:
        return sym in self.levels or bool(self._recs(
            sym, ("sl", "tp", "close", "flatten")))

    # -- saying when to look ---------------------------------------------
    def touch(self, sym: str, delay: float = 0.0):
        """Something about `sym` changed, or may have: look at it."""
        a = self.a
        with a._state_lock:
            self._stamp += 1
            when = time.monotonic() + delay
            cur = self.due.get(sym)
            self.due[sym] = [min(when, cur[0]) if cur else when,
                             cur[1] if cur else 0, self._stamp]
        self.wake.set()

    def moved(self, sym: str):
        """A position changed size or an order of ours ended. Caller may
        hold the state lock."""
        with self.a._state_lock:
            if self.relevant(sym):
                self.touch(sym)

    def set_levels(self, sym: str, sl, tp) -> tuple:
        """Record what the platform wants; returns what it wanted before."""
        with self.a._state_lock:
            old = self.levels.get(sym) or {}
            if sl is None and tp is None:
                self.levels.pop(sym, None)
            else:
                self.levels[sym] = {"sl": sl, "tp": tp}
            self.missing.pop(sym, None)
            self.died.pop(sym, None)
            return old.get("sl"), old.get("tp")

    def said(self, sym: str):
        """The platform has set or cleared the levels of `sym` itself:
        whatever was said about levels it lost is over."""
        with self.a._state_lock:
            self.unheld.pop(sym, None)
            self.remake.discard(sym)
            self.swept.pop(sym, None)
            self.gone.pop(sym, None)
            for key in ("stale:" + sym, "removed:" + sym, "putback:" + sym):
                self.a._notices.pop(key, None)
        self.clear("stale:" + sym, "removed:" + sym, "putback:" + sym)

    def ended(self, sym: str):
        """The position the levels of `sym` were set for is over: a fill
        took the book to zero, or through it to the other side. The levels
        go NOW, not when the repair next reads a zero position. (They used
        to wait for that read; one slow answer, and a position opened in
        between got the old stop and target: a fresh short bought back at
        the old target.) What is left of their orders is cancelled by
        reconcile, which needs no levels for that."""
        a = self.a
        with a._state_lock:
            had = self.levels.pop(sym, None)
            for store in (self.died, self.gone, self.missing, self.unheld,
                          self.executing, self.swept):
                store.pop(sym, None)
            self.remake.discard(sym)
            if had or self.relevant(sym):
                self.touch(sym)

    def fresh(self, sym: str):
        """Under the guard, before an order is placed in `sym`. Levels
        still held here although the book has NO position in the symbol
        are another position's (its end was learned from a read, and the
        repair has not looked yet): they do not reach whatever this order
        opens. Levels that wait under an entry still working are that
        entry's, and stay."""
        a = self.a
        with a._state_lock:
            if sym not in self.levels:
                return
            if (a._book.get(sym) or {}).get("qty", D0) != 0:
                return
            if any(self._contingent(r) for r in self.exits(sym)):
                return
            if any((r["o"].get("order_class") or "") in ("oto", "bracket")
                   for r in self._recs(sym, ("user",))):
                return
        self.ended(sym)

    def exit_ended(self, rec: dict, status: str):
        """An exit order of ours ended and this adapter had not asked for
        it. State lock held. Rejected by Alpaca after it was taken: it is
        made again, and counted (see reconcile). Cancelled or expired:
        somebody else's doing, or Alpaca's own pairing after a fill;
        which, reconcile judges when the fill has had time to arrive."""
        now = time.monotonic()
        if status == "rejected":
            self.died.setdefault(rec["sym"], []).append(now)
            return
        self.gone.setdefault(rec["sym"], []).append({
            "kind": rec["kind"], "price": self._price(rec), "t": now,
            "qty": _dec(rec["o"].get("qty")), "status": status})

    def _judge(self, sym: str, qty: Decimal, lost: list):
        """Exits of `sym` that ended without this adapter asking, looked
        at with the position as it is NOW (read a moment ago, after the
        grace in which a fill would have arrived). The position is still
        there, at the size the exit was made for: nothing of its pair
        filled, so it was taken off from OUTSIDE. Its level is dropped and
        said once; it is not made again. (The withdrawn rule made it
        again: a target cancelled in Alpaca's own app came back below a
        market that had moved through it and sold the position; a stop
        came back twice in silence.)

        ONE exception, because the "outside" can be this adapter itself:
        flatten_all's cancel-all (`DELETE /v2/orders`) is tied to no
        order, and one that Alpaca did not answer may be carried out
        seconds later, when the user has a new position with a stop and
        a target (they were taken off, and this method said "removed
        outside the terminal ... NOT put back").
        While such a request is remembered (AlpacaAdapter._sweep_unsaid:
        LATE_WATCH from the moment it ended unanswered), an exit that
        ended `canceled` AFTER that moment, without this adapter asking,
        is taken for that request's work: the level the user set STANDS,
        the caller (reconcile) places its order again in this very
        pass, and `session.state` says so, with the time of that
        cancel-all. It is put back as a level of an earlier moment is
        (`remake`): a target the market is at or through by now is not
        placed, and said. Outside that window the rule above holds."""
        a, out, back, swept = self.a, [], [], None
        with a._state_lock:
            lv = self.levels.get(sym)
            for g in lost:
                if (lv and lv.get(g["kind"]) is not None and qty != 0
                        and abs(qty) == g["qty"]):
                    at = a._swept_at(g["t"]) \
                        if g["status"] == "canceled" else None
                    if at is not None:
                        swept = at
                        back.append(g)
                        continue
                    lv[g["kind"]] = None
                    out.append(g)
            if lv is not None and lv.get("sl") is None \
                    and lv.get("tp") is None:
                self.levels.pop(sym, None)
            if back:
                self.remake.add(sym)
                self.swept[sym] = swept
        if back:
            self._notice(
                "putback:" + sym, "%s: its %s cancelled at Alpaca and "
                "this adapter had not asked for that: flatten_all's "
                "cancel-all of %s, which Alpaca did not answer then, was "
                "carried out late (taken for its work for %s after such "
                "a request). %s being PUT BACK as %s set; clear %s with "
                "order.modify if %s not wanted any more"
                % (sym, " and ".join(
                    "%s at %s" % ("stop" if g["kind"] == "sl" else "target",
                                  _fixed(g["price"])) for g in back)
                   + (" was" if len(back) == 1 else " were"),
                   iso_ms(swept), a._span(LATE_WATCH),
                   "It is" if len(back) == 1 else "They are",
                   "it was" if len(back) == 1 else "they were",
                   "it" if len(back) == 1 else "them",
                   "it is" if len(back) == 1 else "they are"))
        if out:
            self._notice(
                "removed:" + sym, "%s: its %s removed outside the "
                "terminal (%s at Alpaca, not by this adapter). %s NOT put "
                "back: set %s again with order.modify if %s still wanted"
                % (sym, " and ".join(
                    "%s at %s was" % ("stop" if g["kind"] == "sl"
                                      else "target", _fixed(g["price"]))
                    for g in out) if len(out) == 1 else " and ".join(
                    "%s at %s" % ("stop" if g["kind"] == "sl" else "target",
                                  _fixed(g["price"])) for g in out) + " were",
                   out[0]["status"],
                   "It is" if len(out) == 1 else "They are",
                   "it" if len(out) == 1 else "them",
                   "it is" if len(out) == 1 else "they are"))
            a._save_state()

    def _notice(self, key: str, text: str):
        """Said ONCE: a `session.state` that names it, which goes again by
        itself after NOTICE_SEC (or when the platform sets the level)."""
        with self.a._state_lock:
            self.a._notices[key] = time.monotonic()
        self.fault(key, text)

    # -- the guard ---------------------------------------------------------
    def _guard(self, sym: str) -> threading.RLock:
        with self.a._state_lock:
            g = self._guards.get(sym)
            if g is None:
                g = self._guards[sym] = threading.RLock()
            return g

    @contextlib.contextmanager
    def guard(self, sym: str, wait: float = 4.0):
        """For a handler. The backstop stands down for it at its next step
        (see _yield), so the wait is one request at most."""
        g = self._guard(sym)
        left = self.a.client.time_left()
        with self.a._state_lock:
            self._wanted.add(sym)
        try:
            ok = g.acquire(timeout=max(0.05, wait if left is None
                                       else min(wait, left)))
        finally:
            with self.a._state_lock:
                self._wanted.discard(sym)
        if not ok:
            raise AdapterError(
                "x_alpaca_busy", "the stop or target of %s is being put in "
                "place at Alpaca right now; ask again in a moment" % sym,
                retryable=True, like="frozen")
        try:
            yield
        finally:
            g.release()

    def _yield(self, sym: str, background: bool):
        if background and (self.flattening or sym in self._wanted
                           or self.a._stop.is_set()):
            raise _StandDown()

    # -- saying what is wrong ----------------------------------------------
    def fault(self, key: str, text: str):
        a = self.a
        with a._state_lock:
            if self.issues.get(key) == text:
                return
            self.issues[key] = text
            joined = "; ".join(self.issues.values())
        log(text)
        a._set_session("protect", "degraded", joined)

    def clear(self, *keys):
        a = self.a
        with a._state_lock:
            hit = [k for k in keys if self.issues.pop(k, None) is not None]
            joined = "; ".join(self.issues.values())
        if hit:
            a._set_session("protect", "degraded" if joined else "connected",
                           joined)

    def _expose(self, sym: str, recs: list, flat: bool):
        """(a): an exit that should not be there is SHOWN (orders.list)
        and SAID, until it is gone."""
        with self.a._state_lock:
            for r in recs:
                r["exposed"] = True
        self.fault("orphan:" + sym, "a stop or target order of %s is "
                   "resting at Alpaca with %s; it is being cancelled and "
                   "is listed by orders.list until it is gone"
                   % (sym, "no position behind it" if flat
                      else "more quantity than the position"))

    def _settled(self, sym: str):
        with self.a._state_lock:
            self.missing.pop(sym, None)
            self.orphan.pop(sym, None)
            self.triggered.pop(sym, None)
            # (NOT `executing`: since when an exit of this position has
            # been part-filled is a fact about the exit, not about this
            # pass. It used to be forgotten here, at every pass that
            # found the pair right, so the NEXT look at a target that
            # still rested part-filled began the five seconds of grace
            # again: order.modify, which looks once and answers, was
            # refused "busy" two times in three for as long as the
            # target rested, with no fault at all. It goes when no exit
            # is part-filled any more (reconcile) and when the position
            # ends.)
            self.a._notices.pop("close:" + sym, None)
        # ("close:": the order that was closing it died and its stop was
        # being put back. It is back, or none is wanted: nothing is wrong.)
        self.clear(sym, "orphan:" + sym, "close:" + sym)

    # -- the broker's side ---------------------------------------------------
    def _cancel(self, sym: str, recs: list):
        """Ask Alpaca to cancel these exits. 204 is "request taken"."""
        a = self.a
        with a._state_lock:
            for r in recs:
                r["cancel_req"], r["cancel_at"] = True, time.monotonic()
            wanted = bool(recs) and sym in self.levels
        if wanted:
            # A level is still wanted and its order is being taken off (a
            # re-size, a swap): from here until the new order rests the
            # position has no stop. That is written down FIRST, with the
            # time, so that an adapter that stops inside this window
            # (bye, a kill, a crash) leaves a record the next start acts
            # on (see adopt), not a bare position nobody knows about.
            a._save_state()
        for r in recs:
            try:
                a.client.call("DELETE", "trading", "/v2/orders/" + r["id"],
                              **self._kw())
            except AlpacaError as e:
                # 404/422: already gone (cancelling an OCO's limit takes
                # its stop with it).
                if e.status not in (404, 422):
                    raise

    def _held(self, sym: str) -> Decimal:
        """Alpaca's position in `sym` now, signed; 0 when there is none."""
        row = self.a._position_row(sym, **self._kw())
        if row is None:
            return D0
        qty = abs(_dec(row.get("qty")))
        return -qty if row.get("side") == "short" else qty

    def _freed(self, sym: str, others: Decimal = D0) -> tuple:
        """Wait (about 3 s at most, never past a handler's deadline) until
        Alpaca has let go of the quantity the cancelled exits held.
        `others` is what orders that are NOT ours hold in the symbol.
        Returns (freed, the position's signed quantity, Alpaca's row), as
        last read; the row is None when the position is gone."""
        a, qty, row = self.a, D0, None
        for _ in range(12):
            row = a._position_row(sym, **self._kw())
            if row is None:
                return True, D0, None
            qty = abs(_dec(row.get("qty")))
            if row.get("side") == "short":
                qty = -qty
            if abs(_dec(row.get("qty_available"))) >= abs(qty) - others:
                return True, qty, row
            a.client.pause(0.25)
        return False, qty, row

    def release(self, sym: str, exits: list, others: Decimal = D0):
        """For position.close, guard held: take the exits off so the
        quantity can be sold. The symbol is marked due FIRST, so whatever
        happens after this line (a refusal, a lost answer, an exception)
        the backstop puts back what the position should have. Returns
        Alpaca's row for the position as read once the quantity was free
        (None when there was nothing to take off, or the position went)."""
        self.touch(sym, 0.5)
        with self.a._state_lock:
            self.died.pop(sym, None)
        if not exits:
            return None
        self._cancel(sym, exits)
        freed, _, row = self._freed(sym, others)
        if not freed:
            raise AdapterError(
                "x_alpaca_qty_held", "Alpaca has not yet released the "
                "quantity of %s that its stop held; ask again in a moment"
                % sym, retryable=True, like="frozen")
        return row

    def _place(self, sym: str, long: bool, qty: Decimal, sl, tp):
        """What carries a position's levels: a stop order, a limit order,
        or Alpaca's OCO pair for both, sized to the position."""
        a = self.a
        side = "sell" if long else "buy"
        base = {"symbol": sym, "qty": _fixed(qty), "side": side,
                "time_in_force": "gtc"}
        if sl is not None and tp is not None:
            kind, body = "tp", dict(
                base, type="limit", limit_price=_fixed(tp),
                order_class="oco", take_profit={"limit_price": _fixed(tp)},
                stop_loss={"stop_price": _fixed(sl)})
        elif sl is not None:
            kind, body = "sl", dict(base, type="stop", stop_price=_fixed(sl))
        else:
            kind, body = "tp", dict(base, type="limit",
                                    limit_price=_fixed(tp))
        body["client_order_id"] = (P_SL if kind == "sl" else P_TP) \
            + uuid.uuid4().hex
        how = self._kw()
        with a._placing():
            o, _ = a._post_order(body, kind, None, side, how["kind"],
                                 how.get("max_wait", 2.5))
            with a._state_lock:
                a._track(o)
                for leg in o.get("legs") or []:
                    a._leg_kind[str(leg.get("id"))] = "sl"
                    a._track(leg)

    def _replace(self, rec: dict, price, qty=None):
        """Move one level (or, with `qty`, bring one exit to size) by
        Alpaca's replace: the old order works until the new one has taken
        its place, so the position is never bare."""
        a, kind = self.a, rec["kind"]
        coid = (P_SL if kind == "sl" else P_TP) + uuid.uuid4().hex
        with a._state_lock:
            a._intents[coid] = {"kind": kind, "cid": None}
        body = {"client_order_id": coid}
        if price is not None:
            body["stop_price" if kind == "sl" else "limit_price"] = \
                _fixed(price)
        if qty is not None:
            body["qty"] = _fixed(qty)
        try:
            new = a.client.call(
                "PATCH", "trading", "/v2/orders/" + rec["id"],
                body=body, **self._kw())
        except AlpacaError as e:
            low = e.message.lower()
            if e.status == 422 and not any(w in low
                                           for w in ("price", "stop")):
                # Mid-replace or not yet at the venue: not wrong, too
                # early (SPEC 6 `frozen`).
                raise AdapterError(
                    "x_alpaca_not_replaceable", "Alpaca: " + e.message,
                    retryable=True, like="frozen") from None
            raise
        with a._state_lock:
            a._leg_kind[str(new.get("id"))] = kind
            nrec = a._track(new)
            nrec["kind"], nrec["cid"] = kind, None
            nrec["entry"] = rec.get("entry")
            # Alpaca sends no event when a HELD leg is replaced: the old
            # order is finished as far as protection goes from here on.
            rec["superseded"] = True

    def _through(self, sym: str, long: bool, tp: float) -> bool:
        """Would a target at `tp` fill at once? True also when the price
        cannot be read: what is not known is not placed."""
        try:
            q = self.a._fetch_quotes([sym]).get(sym)
        except AlpacaError:
            q = None
        if not q:
            return True
        bid, ask = q[0], q[1]
        return bid >= tp if long else ask <= tp

    # -- THE function ----------------------------------------------------------
    def reconcile(self, sym: str, background: bool = False) -> tuple:
        """Make Alpaca hold exactly the exits `levels[sym]` asks for, for
        the position as it IS. The caller holds the symbol's guard.

        Returns ("ok", 0) when there is nothing left to do, ("wait", s)
        when it must be looked at again in s seconds, and raises when
        Alpaca refused or could not be reached; the symbol then stays due.
        """
        a = self.a
        now = time.monotonic()
        with a._state_lock:
            # A close of ours, or an entry that carries its own exits, is
            # working: the quantity is spoken for and the picture is about
            # to change. Whatever ends it marks the symbol due at once.
            # (After half a minute the broker is asked anyway.)
            working = [r for r in self._recs(sym, ("close", "flatten"))
                       if now - r["t0"] < 30.0]
            working += [r for r in self._recs(sym, ("user",))
                        if (r["o"].get("order_class") or "") in (
                            "oto", "bracket") and now - r["t0"] < 30.0]
        if working:
            return "wait", 0.5
        # Fresh truth, read under the guard: every open order in the
        # symbol (which also finds exits this process had no record of,
        # and learns how the ones that vanished ended), and THEN the
        # position, so that whatever is placed below is placed for the
        # position as it was a moment ago, not a second ago (a stop can
        # fire between two reads).
        a._refresh_orders(sym, **self._kw())
        qty = self._held(sym)
        self._yield(sym, background)
        with a._state_lock:
            every = self.exits(sym)
            waiting = [r for r in every if self._contingent(r)]
            exits = [r for r in every if r not in waiting]
            closing = self._recs(sym, ("close", "flatten"))
            want = dict(self.levels.get(sym) or {})
            side = "sell" if qty > 0 else "buy"
            # what orders that are not ours hold of the position
            others = sum((self._left(r) for r in self._recs(
                sym, ("user", "foreign")) if r["o"].get("side") == side
                and r["o"].get("status") != "held"), D0)
        wanted = {k for k in ("sl", "tp") if want.get(k) is not None}
        if closing:
            return "wait", 1.0
        if qty == 0:
            if exits:
                # (a) No position behind them. Cancel now; say so if they
                # are still there a moment later (see audit), and at once
                # if the cancel itself fails.
                try:
                    self._cancel(sym, exits)
                except AlpacaError:
                    self._expose(sym, exits, True)
                    raise
                return "wait", 0.7
            if not waiting:
                with a._state_lock:
                    self.levels.pop(sym, None)
            with a._state_lock:
                for store in (self.gone, self.died, self.unheld,
                              self.executing, self.swept):
                    store.pop(sym, None)
                self.remake.discard(sym)
                for key in ("stale:", "removed:", "held:", "putback:"):
                    a._notices.pop(key + sym, None)
            self._settled(sym)
            self.clear("stale:" + sym, "close:" + sym, "opened:" + sym,
                       "removed:" + sym, "held:" + sym, "putback:" + sym)
            return "ok", 0.0
        if waiting:
            return "wait", 2.0       # part of an entry filled; legs follow
        with a._state_lock:
            lost = list(self.gone.get(sym) or ())
        if lost:
            # Exits that ended without this adapter asking. Too early to
            # say whose doing it was while a fill may be a moment behind
            # (Alpaca cancels the other half of a pair BEFORE it reports
            # the fill): nothing is made again meanwhile.
            young = [g["t"] for g in lost if now - g["t"] < PROTECT_GRACE]
            if young:
                return "wait", PROTECT_GRACE - (now - min(young)) + 0.05
            with a._state_lock:
                self.gone.pop(sym, None)
                # The stop of a pair that is no longer `held` has
                # TRIGGERED: Alpaca took the target off itself, and the
                # stop's fill is on its way. The pair at work, nobody's
                # doing from outside.
                if any(r["kind"] == "sl" and not r["cancel_req"]
                       and (r["o"].get("order_class") or "") in (
                           "oco", "bracket")
                       and r["o"].get("status") != "held" for r in exits):
                    lost = [g for g in lost if g["kind"] != "tp"]
            self._judge(sym, qty, lost)
            with a._state_lock:
                want = dict(self.levels.get(sym) or {})
            wanted = {k for k in ("sl", "tp") if want.get(k) is not None}
        # An exit that is EXECUTING (part-filled) is left alone for a
        # moment: the rest of its fills are on their way, and so is what
        # Alpaca does to the other half of its pair. Cancelling a target
        # in the middle of its fill to "fix" its sibling's size helps
        # nobody. If it is still part-filled after a few seconds, the
        # rules below apply to it like to any other, from then on: the
        # moment is counted ONCE, from when an exit of this position was
        # first seen part-filled, not from each look (see _settled).
        with a._state_lock:
            if any(_dec(r["o"].get("filled_qty")) > 0 for r in exits):
                since = self.executing.setdefault(sym, now)
            else:
                since = self.executing.pop(sym, None) and None
        if since is not None and now - since < 5.0:
            return "wait", 0.5
        size = abs(qty)
        bad = [r for r in exits
               if r["o"].get("side") != side or self._left(r) > size]
        part = [r for r in exits if _dec(r["o"].get("filled_qty")) > 0
                and r["o"].get("side") == side and not r["cancel_req"]]
        if part and bad and wanted and not any(
                r in part or r["o"].get("side") != side for r in bad):
            # One of the pair is filling IN PARTS (a target on a thin
            # share), and the other still says the size the position
            # had. The filling one is never cancelled to correct that: it
            # would lose its place in the queue, and the position its
            # exits for a moment, at every partial fill. The other one
            # alone is brought to size where it stands, by Alpaca's
            # replace. It counts as done for a size (`resized`) only when
            # Alpaca took the replace or refused it for good; no answer, a
            # 5xx, a 429 and a request that could not be sent are tried
            # again after a growing pause, and "not yet" (a 422 of the
            # kind _replace calls too early) three times before it is
            # taken for a refusal. (It used to be marked BEFORE the one
            # attempt, whatever came of it.) While it is larger than the
            # position that is SAID, see audit.
            for r in bad:
                if r.get("resized") == size or r["cancel_req"]:
                    continue
                at = r.get("resizing") or (None, 0, 0.0)
                if at[0] == size and time.monotonic() < at[2]:
                    continue          # its pause after an answer that was none
                self._yield(sym, background)
                try:
                    self._replace(r, None, size)
                    r["resized"] = size
                except (AlpacaError, AdapterError) as e:
                    n = at[1] + 1 if at[0] == size else 1
                    again = (e.status in (0, 408, 429) or e.status >= 500) \
                        if isinstance(e, AlpacaError) \
                        else (e.retryable and n < 3)
                    pause = min(RETRY_MAX, max(2.0 ** n, getattr(
                        e, "retry_after", None) or 0.0))
                    if again:
                        r["resizing"] = (size, n, time.monotonic() + pause)
                    else:
                        r["resized"] = size
                    log("%s: the %s of a pair whose other half is filling "
                        "in parts could not be brought to %s (%s); %s"
                        % (sym, "stop" if r["kind"] == "sl" else "target",
                           _fixed(size), a._err(e) if isinstance(
                               e, AlpacaError) else e,
                           "tried again in %d s" % pause if again
                           else "left as it is"))
            return "wait", 3.0
        if bad or (exits and not wanted):
            # (a) wrong side or too large, or no level wanted any more.
            try:
                self._cancel(sym, exits)
            except AlpacaError:
                if bad:
                    self._expose(sym, bad, False)
                raise
            freed, held, _ = self._freed(sym, others)
            if not freed or held != qty:
                return "wait", 0.3   # not free yet, or it moved: look again
            exits = []
        if not wanted:
            self._settled(sym)
            return "ok", 0.0
        live = [r for r in exits if not r["cancel_req"]]
        if len(live) != len(exits):
            return "wait", 0.5       # some are on their way out: let them
        if live and len(live) == len(wanted) and {
                r["kind"] for r in live} == wanted and all(
                self._left(r) == size for r in live):
            # The right orders already: move the prices that differ.
            for r in live:
                if not self._same(self._price(r), want[r["kind"]]):
                    self._yield(sym, background)
                    self._replace(r, want[r["kind"]])
            self._settled(sym)
            return "ok", 0.0
        if live:
            # The stop of a pair whose target Alpaca has cancelled because
            # the stop TRIGGERED (it is then no longer `held`) is executing
            # too, though nothing of it has filled yet: left alone.
            fired = [r for r in live if r["kind"] == "sl"
                     and (r["o"].get("order_class") or "") in (
                         "oco", "bracket")
                     and r["o"].get("status") != "held"]
            if fired and "tp" in wanted and len(live) == 1:
                with a._state_lock:
                    since = self.triggered.setdefault(sym, now)
                if now - since < 30.0:
                    return "wait", 1.0
            self._cancel(sym, live)
            freed, held, _ = self._freed(sym, others)
            if not freed or held != qty:
                return "wait", 0.3
        self._yield(sym, background)
        with a._state_lock:
            recent = self.died[sym] = [
                t for t in self.died.get(sym, ()) if now - t < 30.0]
        if len(recent) >= 3:
            # Alpaca took the order three times in half a minute and
            # rejected it each time afterwards. Not a loop to spin in:
            # said, and tried again with a pause. (Only what ALPACA ended
            # is counted. An exit this adapter took off itself, to
            # re-size it after a partial close or because the platform
            # changed the levels, used to count too, and ordinary scaling
            # out in three clips left the rest of a position without its
            # stop for half a minute.)
            raise AdapterError(
                "x_alpaca_exit_dying", "Alpaca took the orders that carry "
                "them %d times in 30 s and rejected them each time"
                % len(recent), retryable=True)
        if sym in self.remake and want.get("tp") is not None \
                and self._through(sym, qty > 0, want["tp"]):
            # Put back from an earlier run, minutes later, or after a
            # cancel-all of flatten_all's took it off late (_judge): a
            # target the market has moved through would fill at once, as
            # a market order nobody asked for. It is not put back; said.
            with a._state_lock:
                lv = self.levels.get(sym) or {}
                lv["tp"] = None
                if lv.get("sl") is None:
                    self.levels.pop(sym, None)
                u = self.unheld.setdefault(sym, {"since": a.now_ms()})
                u.update(sl=None, tp=want["tp"], stale=True)
                swept = self.swept.pop(sym, None)
            self._notice(
                "stale:" + sym, "%s had a target at %s %s; the market is "
                "at or through it now (or its price "
                "could not be read), so it was NOT put back: set it again "
                "with order.modify if it is still wanted"
                % (sym, _fixed(want["tp"]),
                   "when this adapter last ran" if swept is None else
                   "until flatten_all's cancel-all of %s, carried out "
                   "late, cancelled it" % iso_ms(swept)))
            want["tp"] = None
            if want.get("sl") is None:
                with a._state_lock:
                    self.remake.discard(sym)
                self._settled(sym)
                return "ok", 0.0
        try:
            self._place(sym, qty > 0, size, want.get("sl"), want.get("tp"))
        except AdapterError as e:
            if background and e.code == "x_alpaca_qty_held" and others > 0:
                # An order that is not ours holds the quantity (the
                # account's owner put his own stop there, say). It is not
                # fought: said once, and the level dropped.
                with a._state_lock:
                    self.levels.pop(sym, None)
                    self.unheld.pop(sym, None)
                    self.remake.discard(sym)
                self._notice(
                    "held:" + sym, "%s: its %s NOT placed: an order at "
                    "Alpaca that is not the terminal's holds %s of the "
                    "position. The level was dropped; set it again with "
                    "order.modify when that order is gone"
                    % (sym, " and ".join(
                        "stop" if k == "sl" else "target"
                        for k in sorted(wanted)) + (
                        " was" if len(wanted) == 1 else " were"),
                       _fixed(others)))
                self._settled(sym)
                return "ok", 0.0
            # Refused. If the position has changed in the meantime (its
            # stop fired a moment ago, say) that is not a failure to cry
            # about: there is simply something else to do now.
            if getattr(e, "unknown", False) or self._held(sym) == qty:
                raise
            return "wait", 0.0
        with a._state_lock:
            self.remake.discard(sym)
            self.swept.pop(sym, None)
        # The position may have changed while the orders were on their
        # way (this was the window through which a stop reached a flat
        # account). Look again; if it did, the next pass applies (a). A
        # handler that is short of time leaves that look to the backstop,
        # which is woken for it.
        left = a.client.time_left()
        if left is not None and left < 2.5:
            self.touch(sym, 0.2)
            return "ok", 0.0
        if self._held(sym) != qty:
            return "wait", 0.0
        self._settled(sym)
        return "ok", 0.0

    # -- the backstop's part ---------------------------------------------------
    def audit(self):
        """Every pass of the backstop: the invariants against what this
        process knows (which the stream and the periodic re-read of
        Alpaca's lists keep current). Marks symbols due, exposes orders
        that should not be there, and says what has lasted too long."""
        a, now = self.a, time.monotonic()
        say, unsay, expose = [], [], []
        with a._state_lock:
            syms = set(self.levels)
            for r in a._orders.values():
                if not r["done"] and r["kind"] in ("sl", "tp", "close",
                                                   "flatten"):
                    syms.add(r["sym"])
            for sym in syms:
                qty = (a._book.get(sym) or {}).get("qty", D0)
                every = self.exits(sym)
                waiting = [r for r in every if self._contingent(r)]
                exits = [r for r in every if r not in waiting]
                closing = self._recs(sym, ("close", "flatten"))
                want = self.levels.get(sym) or {}
                wanted = {k for k in ("sl", "tp")
                          if want.get(k) is not None}
                slow = [r for r in closing if now - r["t0"] > CLOSE_SAY]
                if slow:
                    for r in slow:
                        r["exposed"] = True
                    say.append(("closing:" + sym, "the order closing %s "
                                "has been working at Alpaca for more than "
                                "%d s and has not filled; the position's "
                                "stop and target are off until it ends (it "
                                "is listed by orders.list and can be "
                                "cancelled)" % (sym, CLOSE_SAY)))
                else:
                    unsay.append("closing:" + sym)
                side = "sell" if qty > 0 else "buy"
                # (too large beside a half that is filling in parts is
                # the pair at work, not an order to expose: see reconcile)
                part = any(_dec(r["o"].get("filled_qty")) > 0
                           and r["o"].get("side") == side for r in exits)
                loose = exits if qty == 0 else [
                    r for r in exits if r["o"].get("side") != side
                    or (self._left(r) > abs(qty) and not part)]
                if loose:
                    since = self.orphan.setdefault(sym, now)
                    if sym not in self.due:
                        self.touch(sym)
                    if now - since >= ORPHAN_SAY:
                        expose.append((sym, loose, qty == 0))
                else:
                    self.orphan.pop(sym, None)
                    unsay.append("orphan:" + sym)
                # ... but it IS larger than the position: if it fires
                # before it has been brought to size (reconcile), it
                # trades the difference with nothing behind it. When
                # that has lasted OVERSIZE_SAY it is said, with both
                # quantities, until it is no longer so (in tests: a stop left
                # at 9 beside a position of 6, a stderr line, and
                # "connected").
                big = [r for r in exits if part and qty != 0
                       and r["o"].get("side") == side
                       and not r["cancel_req"]
                       and self._left(r) > abs(qty)]
                if big:
                    since = self.oversize.setdefault(sym, now)
                    if sym not in self.due:
                        self.touch(sym)
                    if now - since >= OVERSIZE_SAY:
                        r, n = big[0], abs(qty)
                        say.append(("oversize:" + sym, "%s: its %s at "
                                    "Alpaca is for %s and the position is "
                                    "%s (the other half of the pair is "
                                    "filling in parts): if it fires it %ss "
                                    "%s more than the position. %s. Look "
                                    "at %s NOW"
                                    % (sym, "stop" if r["kind"] == "sl"
                                       else "target", _fixed(self._left(r)),
                                       _fixed(n), side,
                                       _fixed(self._left(r) - n),
                                       "Alpaca would not make it smaller "
                                       "and it is left as it is"
                                       if r.get("resized") == n else
                                       "It is being made smaller, asked "
                                       "of Alpaca again until it answers",
                                       sym)))
                else:
                    self.oversize.pop(sym, None)
                    unsay.append("oversize:" + sym)
                if qty == 0:
                    self.missing.pop(sym, None)
                    if (sym in self.levels and not waiting and not closing
                            and sym not in self.due):
                        self.touch(sym)       # the levels go, once verified
                    continue
                carrying = [r for r in every if not r["cancel_req"]]
                held = {r["kind"]: r for r in carrying}
                good = (not loose and len(carrying) == len(wanted)
                        and set(held) == wanted and all(
                    self._same(self._price(r), want.get(k))
                    for k, r in held.items())
                    and all(r in waiting or self._left(r) == abs(qty)
                            for r in held.values()))
                if good:
                    self.missing.pop(sym, None)
                    if sym not in self.due:
                        unsay.append(sym)
                    continue
                since = self.missing.setdefault(sym, now)
                if (now - since >= PROTECT_GRACE and sym not in self.due
                        and not closing and not waiting):
                    self.touch(sym)           # (b)
                bare = sorted(wanted - set(held))
                if bare and now - since >= PROTECT_SAY \
                        and sym not in self.issues:
                    say.append((sym, "%s: the %s wanted for this position "
                                "%s NOT held by Alpaca, for more than %d s "
                                "now%s"
                                % (sym, " and ".join(
                                    "stop" if k == "sl" else "target"
                                    for k in bare),
                                   "is" if len(bare) == 1 else "are",
                                   PROTECT_SAY,
                                   "; a closing order is working"
                                   if closing else "; it is being put back")))
            # Nothing stays said about a symbol that has left the set
            # looked at above (its close finished, its position went).
            for sym in [s for s in self.oversize if s not in syms]:
                del self.oversize[sym]
            for key in list(self.issues):
                head, _, sym = key.rpartition(":")
                if head in ("closing", "orphan", "oversize") \
                        and sym not in syms:
                    unsay.append(key)
                elif head in ("opened", "stale", "removed", "held",
                              "stranger", "putback") and sym not in a._book:
                    unsay.append(key)
                elif not head and key not in syms and key not in self.due:
                    unsay.append(key)
        for sym, recs, flat in expose:
            self._expose(sym, recs, flat)
        for key, text in say:
            self.fault(key, text)
        if unsay:
            self.clear(*unsay)

    def loop(self):
        """The thread that repairs stops and targets, and does nothing
        else. It used to be one duty of the backstop thread, behind the
        re-read of the account, the bars and the catalog: seen on paper, a
        position waited 13 s for its stop while that thread queued for a
        ledger read. Here a position waits for nothing but Alpaca."""
        a = self.a
        while not a._stop.is_set():
            busy = False
            try:
                if a._baselined:
                    self.audit()
                    self.run_due()
                    with a._state_lock:
                        busy = bool(self.due or self.missing or self.orphan
                                    or self.oversize)
                if "repair" in a._components:
                    a._set_session("repair", "connected", "")
            except Exception as e:  # noqa: BLE001 - this thread must live
                text = "%s: %s" % (type(e).__name__, a.client.scrub(str(e)))
                log("stops and targets: " + text)
                a._set_session(
                    "repair", "degraded", "the upkeep of stops and targets "
                    "failed and is being tried again: " + text)
                busy = True
            self.wake.wait(0.5 if busy else 2.0)
            self.wake.clear()

    def run_due(self):
        """Reconcile what is due. One symbol at a time, never while
        flatten_all runs, never against a handler."""
        a = self.a
        with a._state_lock:
            now = time.monotonic()
            todo = [(s, d[2]) for s, d in self.due.items() if d[0] <= now]
        for n, (sym, stamp) in enumerate(todo):
            if self.flattening or a._stop.is_set():
                return
            if n:
                # Several symbols are due and they are done one at a
                # time: what is bare meanwhile is SAID meanwhile (seven
                # positions were bare for eleven seconds with not a word).
                self.audit()
            with a._state_lock:
                if not self.relevant(sym):
                    # No level, no exit, no close of ours: nothing to keep.
                    if (self.due.get(sym) or [0, 0, None])[2] == stamp:
                        del self.due[sym]
                    idle = True
                else:
                    idle = False
            if idle:
                with a._state_lock:
                    self.executing.pop(sym, None)
                self._settled(sym)
                continue
            g = self._guard(sym)
            if not g.acquire(blocking=False):
                continue
            try:
                with self.busy:
                    out, delay = self.reconcile(sym, background=True)
            except _StandDown:
                continue
            except (AlpacaError, AdapterError) as e:
                if isinstance(e, AlpacaError) and (
                        e.code in ("budget", "deadline")):
                    # Not Alpaca refusing anything: this process may not
                    # ask right now (its own budget, or a pause Alpaca
                    # asked for). Asked again when it may; a level that
                    # stays unheld meanwhile is said by the audit.
                    with a._state_lock:
                        d = self.due.get(sym) or [0.0, 0, stamp]
                        self.due[sym] = [time.monotonic() + min(
                            max(e.retry_after or 1.0, 0.5), RETRY_MAX),
                            d[1], d[2]]
                    continue
                why = (a._err(e) if isinstance(e, AlpacaError) else e)
                with a._state_lock:
                    d = self.due.get(sym) or [0.0, 0, stamp]
                    tries = d[1] + 1
                    pause = min(RETRY_MAX, 2.0 ** min(tries - 1, 5))
                    self.due[sym] = [time.monotonic() + pause, tries, d[2]]
                self.fault(sym, "%s: its stop/target could not be put "
                           "right at Alpaca (%s). The position may be "
                           "WITHOUT them. Trying again in %d s."
                           % (sym, why, pause))
            else:
                with a._state_lock:
                    d = self.due.get(sym)
                    if d and d[2] == stamp:      # not touched meanwhile
                        if out == "ok":
                            del self.due[sym]
                        else:
                            d[0] = time.monotonic() + delay
                if out == "ok":
                    a._save_state()      # which orders carry the levels now
            finally:
                g.release()

    def finish(self, until: float) -> bool:
        """Before the adapter leaves (`bye`): what is due is done NOW,
        for as long as there is time. A partial close answered a moment
        ago has taken the stop off to re-size it; leaving here would
        leave the rest of the position without it, with nobody running.
        True when nothing is left due."""
        a, first = self.a, True
        while True:
            with a._state_lock:
                todo = [s for s in self.due if self.relevant(s)]
                if first:
                    # due NOW, whatever moment it was put off to; once.
                    # (What Alpaca refuses keeps its growing pause.)
                    for s in todo:
                        self.due[s][0] = 0.0
                    first = False
            if not todo:
                return True
            if time.monotonic() >= until:
                return False
            self.run_due()
            time.sleep(0.15)

    def _same_position(self, sym: str, u: dict, since, fills) -> str:
        """Is the position `sym` has NOW the one a due level was written
        down for? "" when it is; else what is different, in words.

        A level due when the adapter stopped was put back at the next
        start on whatever the symbol then held: the rest sold in
        Alpaca's own app and three bought again got the old stop without
        a word; a short opened there got the old long's stop as a BUY
        stop under the market. The same position is: the same side, no
        larger, bought at the same price a share, and with no fill on its
        opening side since the level fell due (from the newest fills of
        the account, read at every start). State lock held."""
        a = self.a
        b = a._book.get(sym) or {}
        qty = b.get("qty", D0)
        side = "long" if qty > 0 else "short"
        now = "Alpaca now holds a %s of %s" % (side, _fixed(abs(qty)))
        was, held = u.get("side"), _dec(u.get("qty"))
        if was not in ("long", "short") or held <= 0:
            return "what position it was for was not written down"
        if side != was:
            return "%s; it was for a %s of %s" % (now, was, _fixed(held))
        if abs(qty) > held:
            return "%s, more than the %s it was for" % (now, _fixed(held))
        avg = _dec(u.get("avg"))
        if avg > 0 and _dec(b.get("avg")) > 0 and avg != _dec(b.get("avg")):
            return "%s at %s a share; it was for one at %s" % (
                now, _fixed(b.get("avg")), _fixed(avg))
        opening = "buy" if side == "long" else "sell"
        times = []
        for f in fills or []:
            t = parse_ts(f.get("transaction_time"))
            if t is None:
                continue
            times.append(t)
            if (t > since and a._sym(f.get("symbol")) == sym
                    and str(f.get("side") or "").startswith(opening)):
                return "%s, and %s was %s on this account since then" % (
                    now, sym, "bought" if side == "long" else "sold")
        if fills is None or (len(fills) >= 100 and min(times, default=0)
                             > since):
            return ("the account's fills since then could not be read far "
                    "enough back to know it is the same position")
        return ""

    def adopt(self, remembered: dict, unheld: dict | None = None,
              fills: list | None = None):
        """The start of a session. The broker is the source of truth
        (SPEC 7): the levels are what its orders carry NOW.

        A level an earlier run wanted and Alpaca does not hold:
          - it was DUE when that run stopped (its order had been taken
            off to re-size or move it, `unheld`, with the time) and that
            was minutes ago, AND the position is still the one it was for
            (_same_position): it is put back now, like any level that is
            due (a target only if the market is not through it);
          - anything else, or older: it is not quietly put back days
            later. It is SAID, at every start, until the platform sets or
            clears the level or the position is gone.
        Returns the second kind. State lock held."""
        a, now = self.a, self.a.now_ms()
        found: dict[str, dict] = {}
        for r in a._orders.values():
            if r["kind"] in ("sl", "tp") and not r["done"]:
                found.setdefault(r["sym"], {"sl": None, "tp": None})[
                    r["kind"]] = self._price(r)
        self.levels = found
        self.unheld, self.remake = {}, set()
        stale = []
        for sym in set(remembered or {}) | set(unheld or {}):
            old = (remembered or {}).get(sym)
            u = (unheld or {}).get(sym)
            old = old if isinstance(old, dict) else {}
            u = u if isinstance(u, dict) else {}
            if sym not in a._book:
                continue
            miss = {}
            for k in ("sl", "tp"):
                v = old.get(k) if old.get(k) is not None else u.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) \
                        and (found.get(sym) or {}).get(k) is None:
                    miss[k] = float(v)
            if not miss:
                continue
            since = u.get("since")
            due = (bool(u) and not u.get("stale")
                   and isinstance(since, (int, float))
                   and 0 <= now - since <= REMAKE_SEC * 1000)
            why = ""
            if due:
                why = self._same_position(sym, u, since, fills)
                due = not why
            if due:
                lv = self.levels.setdefault(sym, {"sl": None, "tp": None})
                lv.update(miss)
                self.unheld[sym] = {"sl": miss.get("sl"),
                                    "tp": miss.get("tp"), "since": since}
                self.remake.add(sym)
                log("%s: its %s due when this adapter last stopped, %d s "
                    "ago; being put back" % (sym, " and ".join(
                        "%s at %s" % ("stop" if k == "sl" else "target",
                                      _fixed(miss[k])) for k in sorted(miss))
                        + (" was" if len(miss) == 1 else " were"),
                        (now - since) / 1000))
            else:
                self.unheld[sym] = dict(u, **{
                    "sl": miss.get("sl"), "tp": miss.get("tp"),
                    "since": since if isinstance(since, (int, float))
                    else now, "stale": True, "why": why or u.get("why")
                    or ""})
                stale.append((sym, miss, why or u.get("why") or ""))
        for sym in self.levels:
            self.touch(sym)
        return stale


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------

class AlpacaAdapter(Adapter):
    name = "alpaca"
    version = VERSION
    broker = "Alpaca"
    # The careful value. __init__ replaces it with what the configuration
    # says, so anything that skips that step is live, never paper (the
    # base class default is "paper", which a real broker must not inherit).
    mode = "live"
    # Alpaca's trading accounts are US dollar accounts; the first call
    # checks the account's own `currency` and refuses to run on another.
    account_currency = "USD"
    # One position per symbol: a sell against a long reduces it.
    account_model = "netting"
    # Stops and targets on a position, built from orders Alpaca holds.
    # Offered on PAPER only: __init__ turns this off for an instance
    # whose environment is live. False here turns them off everywhere.
    BRACKETS = True
    # What the user must type before this adapter can connect (SPEC 3.2).
    # The values arrive only as BRUE_CRED_API_KEY and BRUE_CRED_API_SECRET
    # at spawn; Alpaca shows the secret once, when the key is generated.
    credential_form = [
        {"key": "api_key", "label": "API key ID", "type": "text"},
        {"key": "api_secret", "label": "API secret key", "type": "password"},
    ]
    # What Alpaca's terms let a platform do with this connection (SPEC 3.3).
    # Its market data is for the account holder's personal, non-commercial
    # use, so a platform keeps it on this computer: off the AI models, its
    # own servers and anything made to be shared. Documented (read
    # 2026-11-14): https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf
    # ("Content includes, without limitation: (1) account positions,
    # balances, transactions, confirmations, and order history; ... (3) market
    # data"; "Content is provided exclusively for personal and noncommercial
    # access and use"; no part of it may be "transmitted or distributed in any
    # way ... to any other computer, server, web site or other medium for
    # publication or distribution or for any commercial enterprise, without
    # Alpaca's express prior written consent"), and the Customer Agreement,
    # section 30 ("I agree not to reproduce, distribute, sell or commercially
    # exploit the market data in any manner without written consent from
    # Alpaca"). data_local_only is the market data part of that. Nothing in
    # these texts requires each order to be a person's own action.
    terms = {
        "data_local_only": True,
        "summary": ("Alpaca licenses its market data for your own personal, "
                    "non-commercial use, so its prices stay on this "
                    "computer."),
        "url": ("https://files.alpaca.markets/disclosures/library/"
                "TermsAndConditions.pdf"),
    }

    def __init__(self, argv=None, environ=None, out=None):
        super().__init__(out)
        environ = os.environ if environ is None else environ
        self.cfg = resolve_environment(
            sys.argv[1:] if argv is None else argv, environ)
        self.mode = self.cfg["mode"]
        # Paper = the environment asked for AND the hosts really talked
        # to (see resolve_environment): anything else is real money.
        self.BRACKETS = bool(type(self).BRACKETS and self.cfg["env"] == "paper"
                             and self.mode == "paper")
        self.client = AlpacaClient(self.cfg)
        self._state_dir = environ.get("BRUE_BROKER_STATE") or ""
        self._state_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False
        self._baselined = False
        self._session_start_ms = 0
        self._watermark_ms = 0
        self._account: dict | None = None
        self._account_t = 0.0
        self._account_dirty = False
        self._account_sent: dict | None = None
        self._clock: dict | None = None
        self._catalog: dict[str, dict] = {}
        self._catalog_list: list[dict] = []
        self._catalog_ready = threading.Event()
        self._catalog_built = 0.0
        self._catalog_error = ""
        self._by_asset: dict[str, str] = {}
        self._by_flat: dict[str, str] = {}
        # order id -> what this session knows about the order
        self._orders: dict[str, dict] = {}
        # client_order_id -> {"kind", "cid"}: filed BEFORE the request goes
        # out, because the stream can report a fill before the response
        # that names the order has arrived.
        self._intents: dict[str, dict] = {}
        self._ours: dict[str, str] = {}       # persisted: coid -> kind
        self._current: dict[str, str] = {}    # first order id -> newest
        self._alias: dict[str, str] = {}      # first order id -> client id
        self._seen_exec: set = set()
        self._seen_order: collections.deque = collections.deque()
        # symbol -> {"qty" signed, "avg", "fees", "cid", "opened"}
        self._book: dict[str, dict] = {}
        self._book_t: dict[str, float] = {}
        self._book_src: dict[str, str] = {}
        self._book_pre: dict[str, dict] = {}
        self._book_pre_t: dict[str, float] = {}
        self._fills: list[dict] = []
        # Stops and targets belong to ONE component (see _Protection).
        self.prot = _Protection(self)
        self._levels_remembered: dict = {}
        self._unheld_remembered: dict = {}   # sym -> level due, with time
        self._exits_remembered: dict = {}    # sym -> {order id: kind}
        # order id -> "sl" | "tp" for orders that are the adapter's but
        # that Alpaca named itself: the legs of an entry or of an OCO pair
        # (persisted; "flatten" in a file an earlier version wrote: the
        # orders of Alpaca's close-all call, which is no longer used)
        self._leg_kind: dict[str, str] = {}
        # leg id -> the ENTRY order it hangs under, while Alpaca lists
        # that entry as open (the leg is then not an exit yet)
        self._leg_entry: dict[str, str] = {}
        # group_id -> the entry order of ours that heads the group. Every
        # order of a complex order carries its group's id on the stream,
        # so a leg heard there is known as ours even when the answer that
        # would have named it was lost.
        self._groups: dict[str, str] = {}
        self._parents_asked: set = set()
        # client_order_id -> an order that was sent and whose fate is not
        # known: the answer and the look-up were both lost
        self._unknown: dict[str, dict] = {}
        self._late_due: dict[str, dict] = {}   # turned up after a flatten
        self._late_busy = False      # the thread that deals with them runs
        # client_order_id -> when (epoch ms) this adapter ASKED Alpaca to
        # cancel the order under it because it turned up after a
        # flatten_all (see _late). Written to the state file: a retry
        # under that id is told so (_replayed_place), whatever became
        # of the cancel's answer and whichever run is asked.
        self._late_cancels: dict[str, int] = {}
        self._account_said = 0.0     # when _account_now last wrote a line
        # ... and those the watch gave up on without Alpaca ever holding
        # them: one that is heard of after all is still a late order
        self._dropped: dict[str, dict] = {}
        # (when, client id, what) for each order of unknown fate SEEN at
        # Alpaca for the first time, by whichever thread saw it (see _track
        # and _flatten: a read made before that moment says nothing about
        # the order)
        self._arrived: collections.deque = collections.deque(maxlen=64)
        # symbol -> the ONE closing order of it that a further close must
        # reckon with: its POST is on the wire, or ended without an answer
        # that says whether the order exists, or the order was first seen
        # at Alpaca less than CLOSING_KEEP seconds ago (see _post_close)
        self._closing: dict[str, dict] = {}
        # client_order_id -> an order whose POST is on the wire NOW. With
        # `_unknown`, `_late_due`, `_dropped` and `_closing` it is written
        # to the state file (before the POST leaves), so that none of
        # them ends with the process: see _save_state.
        self._wire: dict[str, dict] = {}
        # How many order POSTs have begun in this process (_post_order):
        # flatten_all judges "flat" only on reads that no order request
        # was on its way at, or began beside (see _flatten).
        self._sends = 0
        # (epoch ms, monotonic) of every `DELETE /v2/orders` of
        # flatten_all's that ended WITHOUT an answer saying it was done
        # (see _sweep_unsaid): for LATE_WATCH from then Alpaca may still
        # carry it out, and it cancels whatever rests at that moment,
        # also what was placed after the flatten. Written to the state
        # file like the registries above.
        self._swept: list = []
        self._swept_orders: dict[int, list] = {}   # ... and what was said
        # client_order_id of a closing order -> whose REQUEST made it,
        # kept only for an id under which more than one call's request
        # went out (one closing order a symbol: see _post_close):
        # {"askers": {call: quantity asked}, "maker": call or None,
        # "sent": {call: its requests counted}, "refused": {call: how
        # many of them Alpaca refused}}. A call is a position.close's
        # client id, or "flatten_all:<its client id>". Written to the
        # state file (the counts once a refusal is among them): a retry
        # of the close the id belongs to is answered from it (see
        # _whose_close).
        self._served: dict[str, dict] = {}
        self._unsaved = False            # those changed since the last save
        self._coids: set = set()         # client ids of the orders tracked
        self._tick = 0                   # a logical clock, see _stamp
        self._notices: dict[str, float] = {}
        # Hand-off (see _dispatch): a queue and its workers per lane.
        self._lanes: dict[str, queue.Queue] = {}
        self._lane_lock = threading.Lock()
        self._orders_waiting = 0
        self._orders_idle = threading.Event()
        self._orders_idle.set()
        # flatten_all has a lane of its own (see _dispatch).
        self._flat_at = 0.0                  # when its line was last read
        self._flat_pending = 0
        self._flat_idle = threading.Event()  # none asked for or running
        self._flat_idle.set()
        self._orders_free = threading.Event()   # no order being answered
        self._orders_free.set()
        self._open_lock = threading.Lock()   # the session is opened once
        self._bars_slots = threading.BoundedSemaphore(BARS_LOADS)
        self._bars_waiting = 0       # charts waiting for one of those turns
        # Held while a complex order is being placed, until its legs are
        # known: the stream can announce a leg before the POST has answered.
        self._place_lock = threading.Lock()
        self._quote_sent: dict[str, tuple] = {}
        self._quote_ok = time.monotonic()
        self._stream_up = threading.Event()
        self._components: dict[str, tuple] = {}
        self._session: tuple | None = None
        self._quote_subs: set = set()
        self._quote_refused: set = set()
        self._quote_prime: set = set()
        self._quotes: dict[str, tuple] = {}
        self._quote_wake = threading.Event()
        self._quote_next = 0.0
        self._bar_subs: dict[tuple, dict] = {}
        self._bars: collections.OrderedDict = collections.OrderedDict()
        self._bars_lock = threading.Lock()
        # One quiet reader at a time over ALL stores: reading on for a
        # chart nobody is waiting for must not crowd out quotes.
        self._bars_quiet = threading.Lock()
        self._feed: str | None = None
        self._feed_at = 0.0
        self._resync = False
        self._last_sync = 0.0
        self._last_full = 0.0
        self._catalog_try = 0.0
        self._state_account = ""
        self._load_state()

    # == what this adapter is ============================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # sl/tp on a position: a stop order, a limit order or an
                # OCO pair that ALPACA holds, for share positions. Never a
                # price watched inside this process.
                "brackets": self.BRACKETS, "modify": self.BRACKETS,
                "pending": True,
                # Shares take all three. Crypto has no plain stop at Alpaca
                # ("invalid order type for crypto order"): refused per order.
                "pending_types": ["limit", "stop", "stop_limit"],
                # No gtd at Alpaca. `day` is shares only; crypto is gtc.
                "expiry": ["gtc", "day"],
                # Alpaca has a trailing stop ORDER, but not a trail on a
                # position in points that it maintains.
                "trailing": False,
                "partial_close": True,
                # Alpaca's OCO is an exit pair on one position, not a free
                # group of resting orders, which is what `oco` promises.
                "oco": False,
                "close_by": False,
                "can_list": True,
                # Seen on paper: a queued buy lowered buying power at once.
                "reserves_margin": True},
            "data": {"quotes": True, "bars": True, "history": True,
                     # Money that moves without a trade is in Alpaca's
                     # activities; not mapped in this version.
                     "operations": False,
                     "max_quote_symbols": MAX_QUOTE_SYMBOLS,
                     "max_symbols_per_request": MAX_QUOTE_SYMBOLS},
            "flatten_all": True}

    def rate_limits(self) -> dict:
        # Alpaca: 200 a minute per host, per account. The adapter paces
        # itself (the SDK does not read this); 3 a second is what it keeps
        # to on EACH of its two hosts.
        return {"requests_per_second": PACE_PER_SECOND}

    def broker_info(self) -> dict:
        return {"display_name": "Alpaca",
                "tagline": "US stocks, ETFs and crypto by API",
                "mark": "ALP",
                "website": "https://alpaca.markets",
                "support": "https://alpaca.markets/support",
                "country": "United States",
                "asset_classes": ["US stocks and ETFs", "Crypto"]}

    def _err(self, e: AlpacaError, side: str = "") -> AdapterError:
        return to_adapter_error(e, self.cfg["env"], side,
                                self.account_currency)

    def _call(self, method, host, path, query=None, body=None, **kw):
        """A broker call made for a handler: Alpaca's refusal becomes the
        typed error the platform is told."""
        try:
            return self.client.call(method, host, path, query, body, **kw)
        except AlpacaError as e:
            raise self._err(e) from None

    # == session =========================================================
    def _dispatch(self, rid: str, method: str, params: dict):
        """The read thread's whole job: note WHEN the request was read,
        and hand it on. It never waits on Alpaca.

        The SDK hands an adapter one request at a time. Answering each on
        the thread that reads them meant that one handler which waits by
        design (a chart's history: up to six seconds) held up every
        request behind it: an order sent while a chart was loading was
        answered six seconds late, and the next chart not inside the
        platform's ten seconds at all. So requests go to three lanes, and
        the answers are written as they are ready, in any order (SPEC 1):

          orders   every call that changes something (place, modify,
                   amend, cancel, close). ONE worker: they are answered
                   strictly in the order they were read.
          flatten  flatten_all, the emergency call (SPEC 8.1). It starts
                   the moment its line is read, whatever else is being
                   answered: a call on the orders lane stops at its next
                   request and says so; the ones read BEFORE it and not
                   yet begun are not carried out (and say so); the ones
                   read after it wait until it has answered.
          reads    everything else that asks Alpaca or may wait
                   (account, positions, orders, fills, catalog, quotes,
                   bar subscriptions). A few at once.
          charts   bars.history. Each waits on its own thread, for at
                   most its own time (see handle_bars_history for the
                   bound on charts being READ at once).

        `hello`, `ping` and `bye` are answered here: they touch no
        network. A request's time budget counts from the moment it was
        read (set_request), so what it waited in its lane is not time it
        has twice.
        """
        read_at = time.monotonic()
        # By the exact name only: another spelling of a method
        # ("order_place") would go to the reads lane and be carried out,
        # past the platform's arming rule.
        handler = self._handler(method)
        if method in ("hello", "ping", "bye") or handler is None:
            if method == "bye":
                # An order in flight ends first, and a stop that is DUE (a
                # partial close a moment ago took it off to re-size it) is
                # put back before this adapter leaves. The platform's
                # connector waits five seconds for this answer and then
                # ends the process, so both are bounded inside that; what
                # is still due then is in the state file, with its time.
                self._drain(BYE_DRAIN)
                until = read_at + BYE_TOTAL
                if self._baselined and until - time.monotonic() > 0.2:
                    self.client.set_deadline(until - time.monotonic())
                    try:
                        if not self.prot.finish(until):
                            with self._state_lock:
                                due = sorted(
                                    s for s in self.prot.due
                                    if s in self.prot.levels
                                    and (self._book.get(s) or {}).get(
                                        "qty", D0) != 0)
                            if due:
                                # SAID, before the answer to bye: nobody
                                # is running after it.
                                self.prot.fault(
                                    "leaving", "leaving with the stop or "
                                    "target of %s NOT back in place at "
                                    "Alpaca (it was taken off to be "
                                    "re-sized and there was no time to put "
                                    "it back). It is written down and is "
                                    "put back at the next start if that "
                                    "is within %d minutes and the position "
                                    "is still the same; until then %s"
                                    % (", ".join(due), REMAKE_SEC // 60,
                                       "it has none" if len(due) == 1
                                       else "they have none"))
                    except Exception as e:  # noqa: BLE001 - bye must answer
                        log("stops and targets at bye: %s: %s" % (
                            type(e).__name__, self.client.scrub(str(e))))
                    finally:
                        self.client.set_deadline(None)
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
                    threading.Thread(
                        target=self._lane_loop, args=(lane, q), daemon=True,
                        name="alpaca-%s-%d" % (lane, i)).start()
            if lane == "orders":
                self._orders_waiting += 1
                self._orders_idle.clear()
            elif lane == "flatten":
                # From this instant: whatever changes something stops.
                self._flat_at = read_at
                self._flat_pending += 1
                self._flat_idle.clear()
                self.client.stand_down.set()
        if lane in ("orders", "flatten"):
            self.client.budget.in_hand(+1)
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
                    self.client.budget.in_hand(-1)
                if lane == "orders":
                    self._orders_free.set()
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
        """One request, on its lane's thread, under ONE deadline that
        counts from when it was read (HANDLER_BUDGET; the platform waits
        ten seconds from its send)."""
        budget = (FLATTEN_HARD if method in ("flatten_all", "catalog.list")
                  else HANDLER_BUDGET)
        # The panic button is never told "too late": however long it
        # queued behind another order, it gets time to do its work.
        self.client.set_request(
            read_at, budget, rank=2 if lane in ("orders", "flatten") else 1,
            floor=FLATTEN_FLOOR if method == "flatten_all" else 0.0,
            yields=lane == "orders")
        left = self.client.time_left()
        if lane == "orders":
            if read_at < self._flat_at:
                # Sent before the panic button was pressed and not begun:
                # carrying it out now, behind flatten_all, is what nobody
                # asked for.
                self._send(protocol.res_error(
                    rid, "x_alpaca_flattening", "flatten_all was asked for "
                    "after this call was sent and before it was begun: it "
                    "was NOT carried out. Ask again if it is still wanted.",
                    True, "frozen"))
                return
            if not self._flat_idle.wait(timeout=max(0.05, left - 0.3)):
                self._send(protocol.res_error(
                    rid, "x_alpaca_flattening", "flatten_all is still "
                    "running; this call was not carried out. Ask again.",
                    True, "frozen"))
                return
            left = self.client.time_left()
            self._orders_free.clear()
        if left < 0.3:
            # Its time went by in the queue. Said at once and truthfully:
            # nothing was done, and asking again is safe.
            self._send(protocol.res_error(
                rid, "rate_limited", "the adapter was busy with earlier "
                "requests for %.1f s and the time for this one is up; "
                "nothing was done. Ask again." % (time.monotonic()
                                                  - read_at), True))
            return
        if not self._opened and not method.startswith(PRE_SESSION):
            # The session with Alpaca is opened ONCE, by whichever request
            # comes first; the others wait for it, inside their own time.
            if not self._open_lock.acquire(timeout=max(0.05, left - 0.3)):
                self._send(protocol.res_error(
                    rid, "internal", "the session with Alpaca is still "
                    "being opened; ask again in a moment", True))
                return
            try:
                super()._dispatch(rid, method, params)
            finally:
                self._open_lock.release()
            return
        super()._dispatch(rid, method, params)

    def _drain(self, seconds: float) -> bool:
        """Wait for the orders lane to fall idle (an order in flight is
        not abandoned half-way by a `bye` or a closed pipe)."""
        end = time.monotonic() + seconds
        return (self._orders_idle.wait(seconds) and self._flat_idle.wait(
            max(0.0, end - time.monotonic())))

    def _stamp(self) -> int:
        """A logical clock for "which is newer, this read or that fill".
        A counter, because a wall clock that ticks every 15 ms (Windows)
        cannot order two things that happen inside one tick."""
        with self._state_lock:
            self._tick += 1
            return self._tick

    def on_first_call(self):
        """Open the session: the account read that proves the key, then the
        threads. `hello` never comes here (SPEC 3.1)."""
        if not self.cfg["key"] or not self.cfg["secret"]:
            # The sentence is for the person at the platform's key form;
            # where the values travel is for whoever reads stderr.
            log("no key pair: the platform passes BRUE_CRED_API_KEY and "
                "BRUE_CRED_API_SECRET; by hand, set ALPACA_API_KEY and "
                "ALPACA_API_SECRET")
            raise AdapterError(
                "unauthorized", "Alpaca needs your API key ID and your "
                "secret key. " + (
                    "The secret key is missing." if self.cfg["key"]
                    else "The key ID is missing." if self.cfg["secret"]
                    else "Enter both to connect."))
        if self.cfg.get("cred_fault"):
            # Names the FIELD, never the value (SPEC 8.5).
            raise AdapterError("unauthorized", "Alpaca cannot be asked: "
                               + self.cfg["cred_fault"] + ".")
        acct = self._call("GET", "trading", "/v2/account")
        ccy = str(acct.get("currency") or "")
        if ccy != self.account_currency:
            # hello already said USD, cold. Every figure this adapter
            # reports would be in the wrong currency: stop here.
            raise AdapterError(
                "x_alpaca_currency", "this Alpaca account is in %s; the "
                "adapter reports %s and will not run on it"
                % (ccy or "an unnamed currency", self.account_currency))
        with self._state_lock:
            self._account, self._account_t = acct, time.monotonic()
        self._bind_account(str(acct.get("account_number") or ""))
        if not self._started:
            self._started = True
            # Alpaca's clock (from the Date header of its answer, whole
            # seconds, hence the margin), not ours: the ledger's times are
            # Alpaca's, and a local clock that runs ahead must not make
            # this session skip its own first fills.
            self._session_start_ms = (self.now_ms() - 1500
                                      + (self.client.clock_skew_ms or 0))
            self._watermark_ms = self._session_start_ms
            for target, label in ((self._catalog_thread, "catalog"),
                                  (self._stream_loop, "stream"),
                                  (self._quote_loop, "quotes"),
                                  (self._poll_loop, "poll"),
                                  (self.prot.loop, "protect")):
                threading.Thread(target=target, name="alpaca-" + label,
                                 daemon=True).start()
        if not self._baselined:
            # Orders and positions as they stand, before anything can
            # trade: later events are measured against this.
            try:
                self._sync(baseline=True)
            except AlpacaError as e:
                raise self._err(e) from None
            self._baselined = True
            self._last_full = time.monotonic()
        # Alpaca's stream has no replay. Wait a moment for it so the first
        # order's fill is heard live; if it is slow, carry on and let the
        # backstop read fills until it is up.
        up = self._stream_up.wait(3.0)
        self._set_session("session", "connected",
                          "Alpaca %s account %s" % (
                              self.cfg["env"],
                              acct.get("account_number") or ""))
        if not up:
            self._set_session("stream", "degraded", "the order stream is "
                              "not connected yet; fills are being read "
                              "by polling")

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        self.prot.wake.set()
        self._quote_wake.set()
        self._save_state()

    def _set_session(self, component: str, state: str, detail: str):
        """One state per component, one `session.state` for the worst.

        A single value would let the quote poller's recovery announce
        "connected" while the order stream is still down.
        """
        with self._state_lock:
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
    # `account.json`: that name is the platform's account pin.
    def _state_path(self, name: str) -> str:
        return os.path.join(self._state_dir, name) if self._state_dir else ""

    def _load_state(self):
        path = self._state_path("alpaca_state.json")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError) as e:
            log("state file unreadable, starting without it: %s" % e)
            return
        if not isinstance(st, dict) or st.get("env") != self.cfg["env"]:
            return
        self._state_account = str(st.get("account") or "")
        self._idempotent = dict(st.get("idem") or {})
        self._ours = dict(st.get("ours") or {})
        self._alias = dict(st.get("alias") or {})
        self._fills = list(st.get("fills") or [])
        self._leg_kind = dict(st.get("legs") or {})
        self._levels_remembered = dict(st.get("levels") or {})
        self._unheld_remembered = dict(st.get("unheld") or {})
        self._exits_remembered = dict(st.get("exits") or {})
        feed = st.get("feed")
        if feed in ("iex", "sip"):
            self._feed, self._feed_at = feed, _num(st.get("feed_at"))
        # Orders whose fate the last run did not know when it stopped, and
        # the closing orders a further close must reckon with: those
        # younger than ten minutes are this run's too (see _save_state).
        now_ms, mono = self.now_ms(), time.monotonic()

        def young(at):
            return (isinstance(at, (int, float)) and not isinstance(at, bool)
                    and 0 <= now_ms - at <= LATE_WATCH * 1000)

        def what(u, at):
            return {"kind": str(u.get("kind") or "user"),
                    "cid": u.get("cid") or None,
                    "t0": mono - (now_ms - at) / 1000.0, "next": mono,
                    "n": 0, "flat": bool(u.get("flat")),
                    "side": str(u.get("side") or ""),
                    "qty": str(u.get("qty") or ""),
                    "sym": str(u.get("sym") or "")}
        for name, store in (("unknown", self._unknown),
                            ("dropped", self._dropped)):
            rows = st.get(name)
            for coid, u in (rows if isinstance(rows, dict) else {}).items():
                if isinstance(u, dict) and young(u.get("at")):
                    # (a late order the last run had not dealt with comes
                    # back as one that is asked for: found, it is late
                    # again; see _resolve_unknown)
                    store[str(coid)] = what(u, u["at"])
                    if name == "dropped":
                        store[str(coid)]["dropped_at"] = u["at"]
        rows = st.get("closing")
        for sym, c in (rows if isinstance(rows, dict) else {}).items():
            if isinstance(c, dict) and c.get("coid") and young(c.get("at")):
                seen = c.get("seen")
                pend = {"coid": str(c["coid"]), "flying": 0,
                        "kind": str(c.get("kind") or "close"),
                        "at": c["at"], "unknown": not young(seen),
                        "who": str(c.get("who") or ""),
                        "qty": str(c.get("qty") or "")}
                if young(seen):
                    pend["seen"] = mono - (now_ms - seen) / 1000.0
                self._closing[str(sym)] = pend
        rows = st.get("late_cancels")
        for coid, at in (rows if isinstance(rows, dict) else {}).items():
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                self._late_cancels[str(coid)] = int(at)
        rows = st.get("served")
        for coid, v in (rows if isinstance(rows, dict) else {}).items():
            if isinstance(v, dict) and isinstance(v.get("askers"), dict):
                self._served[str(coid)] = {
                    "askers": {str(k): str(q or "")
                               for k, q in v["askers"].items()},
                    "maker": v.get("maker") if isinstance(
                        v.get("maker"), str) else None}
                for key in ("sent", "refused"):
                    n = v.get(key)
                    if isinstance(n, dict):
                        self._served[str(coid)][key] = {
                            str(k): c for k, c in n.items()
                            if isinstance(c, int)
                            and not isinstance(c, bool)}
        rows = st.get("cancel_all")
        for at in (rows if isinstance(rows, list) else []):
            if young(at):
                # a cancel-all of the last run's flatten_all that Alpaca
                # had not answered: it may still be carried out
                self._swept.append((at, mono - (now_ms - at) / 1000.0))
        if self._unknown or self._dropped or self._closing:
            log("from the last run: %d order(s) of unknown fate, %d no "
                "longer asked for, %d closing order(s) to reckon with"
                % (len(self._unknown), len(self._dropped),
                   len(self._closing)))

    def _save_state(self):
        path = self._state_path("alpaca_state.json")
        if not path:
            return
        with self._state_lock:
            if len(self._leg_kind) > 400:
                # Ids of orders that have ended are of no further use. The
                # ids of orders still working are never dropped by age: a
                # long-held position's legs must stay recognisable.
                for oid in [i for i in self._leg_kind
                            if (self._orders.get(i) or {}).get("done")]:
                    self._leg_kind.pop(oid, None)
                    self._leg_entry.pop(oid, None)
            for store, cap in ((self._idempotent, 2000), (self._ours, 5000),
                               (self._alias, 2000), (self._leg_kind, 4000),
                               (self._served, 500),
                               (self._late_cancels, 500)):
                for old in list(store)[:max(0, len(store) - cap)]:
                    store.pop(old, None)
            del self._fills[:-1000]
            # The exit orders believed open, by id. A HELD stop leg (the
            # stop of a bracket whose entry has filled) is in none of
            # Alpaca's open-order lists: after a restart it can only be
            # found again by its id.
            exits: dict[str, dict] = {}
            for r in self._orders.values():
                if (r["kind"] in ("sl", "tp") and not r["done"]
                        and not r.get("superseded")):
                    exits.setdefault(r["sym"], {})[r["id"]] = r["kind"]
            # Levels wanted that no order carries right now, with the time
            # that began: what is DUE. An adapter that stops with one of
            # these (bye inside a re-size, a kill) leaves it here for the
            # next start (see _Protection.adopt). One an earlier start
            # found too old to put back (`stale`) stays until the platform
            # sets or clears the level or the position is gone.
            prot, now_ms = self.prot, self.now_ms()
            unheld: dict[str, dict] = {}
            for sym, lv in prot.levels.items():
                if (self._book.get(sym) or {}).get("qty", D0) == 0:
                    continue
                hold = prot.holding(sym)
                miss = {k: lv[k] for k in ("sl", "tp")
                        if lv.get(k) is not None and hold.get(k) is None}
                if miss:
                    # ... and WHAT POSITION it is due for: its side, its
                    # size, what it cost a share and when it opened. A
                    # start puts the level back only on that position
                    # (see _Protection.adopt, _same_position).
                    b = self._book.get(sym) or {}
                    unheld[sym] = {
                        "sl": miss.get("sl"), "tp": miss.get("tp"),
                        "since": (prot.unheld.get(sym) or {}).get("since")
                        or now_ms,
                        "side": "long" if b.get("qty", D0) > 0 else "short",
                        "qty": _fixed(abs(b.get("qty", D0))),
                        "avg": _fixed(b.get("avg") or D0),
                        "opened": b.get("opened")}
            for sym, u in prot.unheld.items():
                if u.get("stale") and sym not in unheld and sym in self._book:
                    unheld[sym] = dict(u)
            prot.unheld = unheld
            # What must not end with this process (with none of it
            # written down, after a restart flatten_all answered "flat"
            # over an order of unknown fate, a late order landed unnamed,
            # and a position whose close was on the wire was closed again
            # under another id):
            #   unknown  orders whose fate Alpaca has not told, those a
            #            flatten_all found late and has not dealt with
            #            (`late`), and those whose POST is on the wire
            #            at this moment (`wire`: written BEFORE the
            #            request leaves, see _post_order);
            #   dropped  those this adapter has stopped asking for;
            #   closing  the one closing order a symbol must reckon with;
            #   cancel_all  when a cancel-all of flatten_all's ended
            #            without an answer (it may be carried out late;
            #            see _sweep_unsaid).
            # And, kept by number, not by age: whose request made a
            # closing order under an id more than one call used
            # (`served`), and the ids of late orders this adapter asked
            # Alpaca to cancel (`late_cancels`).
            # Each with its wall-clock time; a start takes back those
            # younger than ten minutes (_load_state).
            mono = time.monotonic()

            def wall(t):
                return int(now_ms - (mono - t) * 1000)

            def row(u, **more):
                out = {k: u.get(k) for k in ("kind", "cid", "side", "qty",
                                             "sym")}
                out.update(flat=bool(u.get("flat")), at=wall(u["t0"]))
                out.update(more)
                return out
            unknown = {c: row(u) for c, u in self._unknown.items()}
            for c, u in self._late_due.items():
                unknown.setdefault(c, row(u, flat=True, late=True))
            for c, w in self._wire.items():
                unknown.setdefault(c, dict(
                    {k: w.get(k) for k in ("kind", "cid", "side", "qty",
                                           "sym", "flat", "at")}, wire=True))
            dropped = {c: row(u, at=u.get("dropped_at") or now_ms)
                       for c, u in self._dropped.items()}
            closing = {s: {"coid": c["coid"], "kind": c.get("kind"),
                           "who": c.get("who"), "qty": c.get("qty"),
                           "at": c.get("at") or now_ms,
                           "seen": None if c.get("seen") is None
                           else wall(c["seen"])}
                       for s, c in self._closing.items()}
            self._unsaved = False
            st = {"v": 1, "env": self.cfg["env"], "exits": exits,
                  "unknown": unknown, "dropped": dropped, "closing": closing,
                  "cancel_all": [at for at, _ in self._swept
                                 if 0 <= now_ms - at <= LATE_WATCH * 1000],
                  "late_cancels": dict(self._late_cancels),
                  # (the counts only once a refusal makes them matter: a
                  # call without them is never taken to have made nothing)
                  "served": {c: dict({"askers": dict(v["askers"]),
                                      "maker": v.get("maker")},
                                     **({"sent": dict(v.get("sent") or {}),
                                         "refused": dict(v["refused"])}
                                        if v.get("refused") else {}))
                             for c, v in self._served.items()},
                  "unheld": {s: dict(v) for s, v in unheld.items()},
                  "account": self._state_account, "feed": self._feed,
                  "feed_at": self._feed_at, "idem": dict(self._idempotent),
                  "ours": dict(self._ours), "alias": dict(self._alias),
                  "legs": dict(self._leg_kind), "fills": list(self._fills),
                  "levels": {s: dict(v)
                             for s, v in self.prot.levels.items()}}
        self._write_json(path, st)

    def _write_json(self, path: str, obj):
        """Whole file or nothing: write beside it, then rename over it.
        One writer at a time, or two would share the temporary file."""
        with self._save_lock:
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(obj, fh)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, path)
            except OSError as e:
                log("could not save %s: %s" % (os.path.basename(path), e))

    def _bind_account(self, number: str):
        """What was remembered belongs to ONE account. A profile whose key
        now opens another (a paper account recreated, a new key) must not
        replay the old one's client ids or show its fills."""
        with self._state_lock:
            if self._state_account and self._state_account != number:
                log("the key now opens another account; remembered state "
                    "for the previous one is dropped")
                self._idempotent.clear()
                self._ours.clear()
                self._alias.clear()
                self._leg_kind.clear()
                self._levels_remembered.clear()
                self._unheld_remembered.clear()
                self._exits_remembered.clear()
                self._unknown.clear()
                self._dropped.clear()
                self._closing.clear()
                self._served.clear()
                self._late_cancels.clear()
                del self._swept[:]
                del self._fills[:]
            changed = self._state_account != number
            self._state_account = number
        if changed:
            self._save_state()

    def idempotent_store(self, client_id: str, result: dict,
                         signature=None) -> dict:
        """The SDK remembers results in memory; a restart is not a licence
        to act twice, so they are also written down (SPEC 2). Orders have
        Alpaca's own client-order-id check behind them as well; a close or
        a flatten has only this."""
        out = super().idempotent_store(client_id, result, signature)
        self._save_state()
        return out

    # == catalog =========================================================
    def _catalog_thread(self):
        try:
            self._load_catalog()
        except Exception as e:  # noqa: BLE001 - reported by catalog.list
            self._catalog_error = "%s: %s" % (type(e).__name__, e)
            log("catalog load failed: " + self._catalog_error)

    def _load_catalog(self, refresh: bool = False):
        path = self._state_path("alpaca_catalog.json")
        acct = self._account or {}
        key = [self.cfg["env"], str(acct.get("account_number") or ""),
               bool(acct.get("shorting_enabled")),
               str(acct.get("multiplier") or ""), acct.get("crypto_tier")]
        if not refresh and path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    c = json.load(fh)
                age = time.time() - _num(c.get("built_at"))
                if (c.get("v") == 2 and c.get("key") == key
                        and 0 <= age < CATALOG_MAX_AGE and c.get("rows")):
                    self._install_catalog(c["rows"], c.get("ids") or {},
                                          _num(c.get("built_at")))
                    if age < CATALOG_REFRESH:
                        return
            except (OSError, ValueError) as e:
                log("catalog cache unreadable, rebuilding: %s" % e)
        kw = {"kind": "background", "timeout": 30.0, "max_wait": 20.0}
        today = datetime.datetime.now(datetime.timezone.utc).date()
        crypto = self.client.call("GET", "trading", "/v2/assets", {
            "status": "active", "asset_class": "crypto"}, **kw)
        shares = self.client.call("GET", "trading", "/v2/assets", {
            "status": "active", "asset_class": "us_equity"}, **kw)
        days = self.client.call("GET", "trading", "/v2/calendar", {
            "start": today.isoformat(),
            "end": (today + datetime.timedelta(days=366)).isoformat()}, **kw)
        rows, ids = build_catalog(shares, crypto, days, acct, today)
        if not rows:
            raise AlpacaError(0, "Alpaca returned no tradable assets")
        built = time.time()
        self._install_catalog(rows, ids, built)
        self._catalog_error = ""
        if path:
            self._write_json(path, {"v": 2, "built_at": built, "key": key,
                                    "rows": rows, "ids": ids})

    def _install_catalog(self, rows: list, ids: dict, built: float):
        new = {r["symbol"]: r for r in rows}
        with self._state_lock:
            old, first = self._catalog, not self._catalog
            self._catalog, self._catalog_list = new, rows
            self._by_asset = dict(ids)
            self._by_flat = {s.replace("/", ""): s for s, r in new.items()
                             if r.get("asset_class") == "crypto"}
            self._catalog_built = built
        self._catalog_ready.set()
        if first:
            return
        # The list changed under a connected platform: say what (SPEC 5).
        gone = sorted(s for s in old if s not in new)
        listed = sorted(s for s in new if s not in old)
        terms = sorted(s for s in new if s in old and new[s] != old[s])
        for reason, syms in (("delisted", gone), ("listed", listed),
                             ("terms", terms)):
            for i in range(0, len(syms), 500):
                part = syms[i:i + 500]
                data = {"reason": reason, "symbols": part}
                if reason != "delisted":
                    data["instruments"] = [new[s] for s in part]
                self.emit("catalog.changed", data)

    def _catalog_wait(self) -> bool:
        """Wait for the instrument list, never past the handler's time."""
        left = self.client.time_left()
        return self._catalog_ready.wait(
            6.0 if left is None else max(0.1, left - 0.3))

    def handle_catalog_list(self, p):
        if not self._catalog_wait():
            raise AdapterError(
                "internal", "the instrument list is still being read from "
                "Alpaca" + (" (last error: %s)" % self._catalog_error
                            if self._catalog_error else ""), retryable=True)
        return {"instruments": self._catalog_list}

    def _instrument(self, symbol) -> dict:
        if not self._catalog_wait():
            raise AdapterError("internal", "the instrument list is still "
                               "being read from Alpaca", retryable=True)
        spec = self._catalog.get(str(symbol))
        if spec is None:
            raise AdapterError("unknown_symbol", "%r is not an instrument "
                               "this Alpaca account can trade" % (symbol,))
        return spec

    def _sym(self, symbol, asset_id=None) -> str:
        """Any spelling Alpaca uses, as the catalog spells it.

        Orders and assets say `BTC/USD`; positions say `BTCUSD` and carry
        an asset id that is the COIN's, found in no asset list. So: the
        catalog's own spelling, then the asset id (shares), then the
        slash-less form (crypto), else the broker's text unchanged, so a
        holding this catalog does not list is still shown.
        """
        symbol = str(symbol or "")
        if symbol in self._catalog:
            return symbol
        return (self._by_asset.get(str(asset_id or ""))
                or self._by_flat.get(symbol) or symbol)

    @staticmethod
    def _is_crypto(spec: dict | None) -> bool:
        return bool(spec) and spec.get("asset_class") == "crypto"

    # == account and clock ===============================================
    @staticmethod
    def _account_view(a: dict, positions=None) -> dict:
        """The SPEC's account figures from Alpaca's.

        `balance` is Alpaca's `equity` less the sum of the open positions'
        `unrealized_pl` (both Alpaca's own figures): what the account
        would be worth with every position counted at what it cost, so
        that equity minus balance is the open P&L and nothing else. The
        two figures come from two reads, the account and the positions,
        and those must be of ONE moment (the caller's duty, see
        _account_now): the price moving between them shows up as balance
        (in tests: off by 531 with a positions read up to 3 s old). A fill
        between them shows up only as its spread, because a
        fill moves equity and the open P&L by the same amount: the
        distance from the fill's price to the mark.

        It is NOT Alpaca's `cash`: that falls by the whole price of a
        purchase, and (seen on paper) by the cost of a crypto buy that is
        only RESTING. `cash` is not used at all. And it is no longer made
        from the account's market values and the rows' cost basis: each
        of those changes by the whole value of a fill, in its own read,
        and a fill between the two reads put balance off by that value
        (in tests: 31 of 34 answers off by 7,075 to 10,619 while 100 shares
        were bought and sold in clips). `equity` is Alpaca's
        `equity`. With no position open, balance and equity are the same
        number.
        """
        open_pl = AlpacaAdapter._open_pl
        equity = _num(a.get("equity"))
        used = _num(a.get("initial_margin"))
        balance = _dec(a.get("equity")) - sum(
            (open_pl(r) for r in positions or []), D0)
        out = {"balance": float(balance),
               "equity": equity,
               # Alpaca's own Reg T initial requirement for what is open;
               # free is what is left of equity above it, the bracket in
               # Alpaca's own buying-power formula.
               "margin_used": used,
               "margin_free": max(equity - used, 0.0),
               "currency": str(a.get("currency") or "USD")}
        if a.get("account_number"):
            out["account_id"] = str(a["account_number"])
        return out

    @staticmethod
    def _open_pl(r: dict) -> Decimal:
        """The open P&L of one of Alpaca's position rows.

        A row that carries no `unrealized_pl` (not seen at Alpaca; a
        halted or unpriced holding could give one) is never counted as
        0: balance would then hold that position's whole open P&L, and
        positions.list would show a losing position as square. It is
        worked out from the row's market value and cost when both are
        there (by magnitude and the row's side: their signs for a short
        have not been seen); otherwise there is no true figure to give,
        and that is said (account.get fails, retryable; positions.list
        leaves the field out of that position)."""
        if r.get("unrealized_pl") not in (None, ""):
            return _dec(r.get("unrealized_pl"))
        mv, cost = r.get("market_value"), r.get("cost_basis")
        if mv in (None, "") or cost in (None, ""):
            raise AdapterError(
                "x_alpaca_no_pnl", "Alpaca's row for the %s position "
                "carries no unrealized_pl, and no market_value and "
                "cost_basis to work it out from: balance (equity less "
                "the open P&L) cannot be given truthfully at this "
                "moment. Ask again." % (r.get("symbol") or "?"),
                retryable=True, like="no_price")
        gain = abs(_dec(mv)) - abs(_dec(cost))
        return -gain if (r.get("side") == "short"
                         or _dec(r.get("qty")) < 0) else gain

    def _read_account(self, kind: str = "read", ready=None) -> dict:
        acct = self.client.call("GET", "trading", "/v2/account", kind=kind,
                                ready=ready)
        with self._state_lock:
            self._account, self._account_t = acct, time.monotonic()
        return acct

    @staticmethod
    def _one_moment(acct: dict, rows: list) -> tuple:
        """(do the account and the position rows show ONE moment?, what
        the account says the positions are worth, what the rows say).

        The account carries the market value of what is held at ITS
        instant (`long_market_value`, `short_market_value`, to the cent)
        and every row its own `market_value` at THEIRS. Two sums that
        differ by more than rounding are of two prices, or of two sets
        of positions. Taken by magnitude and the row's side: the sign
        Alpaca gives a short's market value has not been seen. True when
        it cannot be told (a figure is missing)."""
        lmv, smv = acct.get("long_market_value"), acct.get(
            "short_market_value")
        if lmv in (None, "") or smv in (None, ""):
            return True, None, None
        total = D0
        for r in rows:
            if r.get("market_value") in (None, ""):
                return True, None, None
            mv = abs(_dec(r.get("market_value")))
            total += -mv if (r.get("side") == "short"
                             or _dec(r.get("qty")) < 0) else mv
        said = abs(_dec(lmv)) - abs(_dec(smv))
        # a cent for the account's own rounding, half a cent a row
        return (abs(said - total) <= Decimal("0.01")
                + Decimal("0.005") * len(rows)), said, total

    def _account_now(self, kind: str = "read") -> tuple:
        """(Alpaca's account, its position rows) of ONE moment, as far as
        that can be had from two requests.

        They are sent at the same instant (_account_pair). That makes
        them LEAVE together; on a real link they still ARRIVE tens of
        milliseconds apart, each on its own connection, and in a moving
        market that is balance off by the position times what the price
        moved (in tests: 105 for 200 ms). So the pair is
        checked against itself (_one_moment), and when it shows two
        moments both are read again, once, if the handler has the time:
        the second pair is the answer, whether it agrees (the usual
        case) or not (then it is at least the later one). A second read
        that fails leaves the first, as it would have been used before.
        """
        pair = self._account_pair(kind)
        same, said, total = self._one_moment(*pair)
        left = self.client.time_left()
        if same or (left is not None and left < READ_MARGIN):
            return pair
        try:
            again = self._account_pair(kind)
        except AlpacaError as e:
            log("account: the two reads were of two moments (the account "
                "says the positions are worth %s, the rows %s) and could "
                "not be read again (%s)" % (said, total, e.message))
            return pair
        same, said2, total2 = self._one_moment(*again)
        now = time.monotonic()
        if not same or now - self._account_said > 60.0:
            # (written down when the second pair is no better, and
            # otherwise once a minute: in a fast market this is every call)
            self._account_said = now
            log("account: the two reads were of two moments (the account "
                "says the positions are worth %s, the rows %s); read "
                "again: %s" % (said, total, "one moment" if same
                               else "two again (%s, %s), the later pair is "
                               "used" % (said2, total2)))
        return again

    def _account_pair(self, kind: str = "read") -> tuple:
        """(Alpaca's account, its position rows) read TOGETHER: two
        requests that leave at the same instant, never one of them from
        an earlier read.

        `balance` is made from one figure of each (see _account_view),
        and what happens between the two reads shows up in it. Requests
        below an order's rank leave a third of a second apart (see
        _Budget._gap), and that is too long for a position whose price is
        moving. So each of the two takes its place in the pace and then
        waits for the other to have its own, and both are sent at once.
        If either fails this fails: no balance is made from half of them.
        """
        left = self.client.time_left()
        placed = threading.Event(), threading.Event()
        out, errs = [None, None], [None, None]

        def run(i):
            def ready():
                placed[i].set()
                placed[1 - i].wait(HANDLER_TIMEOUT if left is None
                                   else max(0.0, left))
            if i:
                self.client.set_deadline(left)
            try:
                if i:
                    out[i] = self.client.call(
                        "GET", "trading", "/v2/positions", kind=kind,
                        ready=ready) or []
                else:
                    out[i] = self._read_account(kind, ready)
            except BaseException as e:  # noqa: BLE001 - raised below
                errs[i] = e
            finally:
                placed[i].set()       # the other never waits for a failure
                if i:
                    self.client.set_deadline(None)
        other = threading.Thread(target=run, args=(1,), daemon=True,
                                 name="alpaca-account")
        other.start()
        run(0)
        other.join()
        for e in errs:
            if e is not None:
                raise e
        return out[0], out[1]

    def handle_account_get(self, p):
        """balance, equity, margin_used, margin_free, currency, account_id.

        `balance` is Alpaca's equity less the open positions' unrealized
        P&L (see _account_view), so that equity - balance is the open P&L
        and nothing else. The account and the positions are read for
        THIS call, together, every time (see _account_now): a positions
        read of a moment ago is never used for it. If either read fails
        the call fails rather than answer with a balance made from half
        of them.
        """
        try:
            acct, rows = self._account_now()
        except AlpacaError as e:
            raise self._err(e) from None
        return self._account_view(acct, rows)

    def _market_open(self, kind: str = "read") -> bool | None:
        """Is the US exchange in its regular session now? None if unknown.

        From `/v2/clock`, kept until the next open or close it names (it
        cannot change before then) and at most 15 minutes. That boundary
        is in ALPACA's time, and it is compared with Alpaca's time: this
        machine's clock plus `clock_skew_ms` (from the Date of Alpaca's
        answers; whole seconds). In the CLOCK_EDGE before the boundary
        a read is kept for CLOCK_EDGE_KEEP only, and never past the
        boundary it names; flatten_all reads it at every call there.
        (It used to be compared with this machine's
        clock: one s seconds behind took a share as open for s seconds
        after the close, and flatten_all took its stop and target off
        and sent a closing order Alpaca queued to the next open. Then the
        clock was read at every call in that minute: sixteen market
        orders at once were twelve placed and four refused
        `rate_limited`.)
        """
        now = self.now_ms()
        alpaca = now + (self.client.clock_skew_ms or 0)
        c = self._clock
        # (flatten_all reads it at every call in the edge, as before: it
        # is never refused for the minute, so the read costs it no
        # refusal, and its read goes out beside its positions read, with
        # no answer of that call yet to say what time it is at Alpaca.)
        if c and now - c["at"] < 900_000 and (
                alpaca < c["until"] - CLOCK_EDGE * 1000
                or (kind != "flatten" and alpaca < c["until"]
                    and now - c["at"] < CLOCK_EDGE_KEEP * 1000)):
            return c["open"]
        try:
            raw = self.client.call("GET", "trading", "/v2/clock",
                                   kind=kind, timeout=4.0)
        except AlpacaError as e:
            log("clock unavailable: %s" % e.message)
            if c and now - c["at"] < 86_400_000:
                # One boundary can be stepped over without asking again.
                return (not c["open"]) if alpaca >= c["until"] \
                    else c["open"]
            return None
        is_open = bool(raw.get("is_open"))
        edge = parse_ts(raw.get("next_close" if is_open else "next_open"))
        self._clock = {"open": is_open, "at": now,
                       "until": edge or alpaca + 60_000}
        return is_open


    # == positions =======================================================
    def _keep_pre(self, sym: str, was: dict):
        """A positions read is about to change the book for `sym`: keep
        what it held, for the execution that explains the change (it may
        arrive a moment later on the stream, or be read from the ledger).
        The OLDEST unexplained view is the one to measure from: two reads
        in a row must not make the first read's figure the "before".
        Caller holds the state lock."""
        fresh = (time.monotonic() - self._book_pre_t.get(sym, 0.0)) < 60.0
        if not (self._book_src.get(sym) == "rest"
                and sym in self._book_pre and fresh):
            self._book_pre[sym] = was
            self._book_pre_t[sym] = time.monotonic()
        self._book_src[sym] = "rest"

    def _apply_positions(self, rows, started: int, baseline: bool = False):
        """Fold a positions read into the book.

        `started` is the logical time (_stamp) at which the request went
        out. A symbol an execution has updated since then is left alone:
        the read is the older of the two, and applying it would bring a
        closed position back. When the read changes a quantity, the old
        one is kept aside (see _keep_pre and _on_execution). The first
        read of a session keeps nothing aside: what it finds is where
        this session starts from.
        """
        with self._state_lock:
            seen, changed = set(), []
            for r in rows or []:
                sym = self._sym(r.get("symbol"), r.get("asset_id"))
                seen.add(sym)
                if self._book_t.get(sym, 0) > started:
                    continue
                qty = abs(_dec(r.get("qty")))
                if r.get("side") == "short":
                    qty = -qty
                b = self._book.get(sym)
                old = b["qty"] if b else D0
                if qty == 0:
                    continue
                if old != qty:
                    if not baseline:
                        self._keep_pre(sym, dict(b or {}, qty=old))
                    changed.append(sym)
                    if old != 0 and (old > 0) != (qty > 0):
                        # the other side: another position (b2)
                        self.prot.ended(sym)
                if b is None:
                    b = self._book[sym] = {"fees": 0.0, "cid": None,
                                           "opened": None}
                b["qty"] = qty
                b["avg"] = _dec(r.get("avg_entry_price"))
            for sym in [s for s in self._book if s not in seen]:
                if self._book_t.get(sym, 0) > started:
                    continue
                self._keep_pre(sym, self._book.pop(sym))
                changed.append(sym)
            for sym in changed:
                # Its stop and target are NOT forgotten here: the one
                # owner looks at the symbol and cancels or re-sizes them.
                self.prot.moved(sym)

    def _position_view(self, sym: str, qty: Decimal, r: dict | None) -> dict:
        b = self._book.get(sym) or {}
        r = r or {}
        # The levels an order at Alpaca is carrying NOW; a level that is
        # wanted but not held is not shown as if it were (the session
        # says so when that lasts).
        prot = self.prot.holding(sym)
        v = {"position_id": sym, "symbol": sym,
             "side": "buy" if qty > 0 else "sell", "qty": float(abs(qty)),
             "avg_price": _num(r.get("avg_entry_price"))
             or float(b.get("avg") or 0),
             "sl": prot.get("sl"), "tp": prot.get("tp"), "trail_points": 0,
             # Alpaca charges no financing on these positions. Commission
             # is what this session saw withheld (crypto buys); 0 when it
             # saw none, never a guess.
             "swap": 0.0, "commission": round(b.get("fees") or 0.0, 6),
             "opened_at": b.get("opened"), "from_order": None}
        # The open P&L is Alpaca's own figure. A row that carries none
        # (not seen at Alpaca; a halted or unpriced holding could give
        # one, and so does a position this session knows only from a
        # fill, `r` empty) used to be shown as 0.0: a position that is
        # 500 under water, shown as square. It is worked out from the
        # row's market value and cost as account.get does (_open_pl);
        # when it cannot be, the field is LEFT OUT of that position: a
        # figure the broker did not give is absent, never a default
        # (SPEC 4).
        try:
            v["unrealized_pnl"] = float(self._open_pl(r))
        except AdapterError:
            pass
        if b.get("cid"):
            v["client_id"] = b["cid"]
        return v

    def handle_positions_list(self, p):
        started = self._stamp()
        rows = self._call("GET", "trading", "/v2/positions")
        self._apply_positions(rows, started)
        out, listed = [], set()
        with self._state_lock:
            for r in rows or []:
                sym = self._sym(r.get("symbol"), r.get("asset_id"))
                listed.add(sym)
                qty = abs(_dec(r.get("qty")))
                if r.get("side") == "short":
                    qty = -qty
                if self._book_t.get(sym, 0) > started:
                    # The stream has moved this position since the read
                    # went out: the book is the newer of the two.
                    qty = (self._book.get(sym) or {}).get("qty", D0)
                if qty != 0:
                    out.append(self._position_view(sym, qty, r))
            for sym, b in self._book.items():
                if sym not in listed and self._book_t.get(sym, 0) > started:
                    out.append(self._position_view(sym, b["qty"], None))
        return {"positions": out}

    # == orders: what this session knows =================================
    def _kind(self, coid: str) -> tuple[str, str | None]:
        """(whose order this is, the platform's client id for it).

        `user` is an order the platform placed through this profile, in
        this session or an earlier one; `close`, `flatten`, `sl`, `tp` are
        orders the adapter made itself; `foreign` is anything else on the
        account (another program, Alpaca's own dashboard).
        """
        it = self._intents.get(coid)
        if it:
            return it["kind"], it.get("cid")
        if coid.startswith(P_REPL):
            cid = self._alias.get(coid.split(":")[1] if ":" in coid else "")
            return ("user" if cid else "foreign"), cid
        kind = self._ours.get(coid)
        if kind:
            return kind, coid if kind == "user" else None
        for prefix, k in ((P_CLOSE, "close"), (P_FLAT, "flatten"),
                          (P_SL, "sl"), (P_TP, "tp")):
            if coid.startswith(prefix):
                return k, None
        return "foreign", None

    def _track(self, o: dict) -> dict:
        """The session's record for a broker order, made on first sight.
        Caller holds the state lock."""
        oid = str(o.get("id"))
        rec = self._orders.get(oid)
        if rec is None:
            coid = str(o.get("client_order_id") or "")
            kind, cid = self._kind(coid)
            if oid in self._leg_kind:
                kind, cid = self._leg_kind[oid], None
            if not self.BRACKETS and kind in ("sl", "tp"):
                # No stops or targets are offered here (a live account). A
                # stop or target order an earlier run left on the account
                # is a resting order like anybody's: SHOWN to the platform
                # and cancellable there, never hidden, never managed.
                kind, cid = "foreign", None
            root = oid
            if coid.startswith(P_REPL) and coid.count(":") >= 2:
                root = coid.split(":")[1]
            # First seen already ended, and not an order this session is
            # placing right now (whose fill can beat the answer to its
            # POST): history, never announced, never "in flight".
            ended = coid not in self._intents and (
                o.get("status") in DONE or o.get("status") == "replaced")
            rec = {"id": oid, "coid": coid, "kind": kind, "cid": cid,
                   "root": root, "accepted": ended, "placed": ended,
                   "done": ended, "cancel_req": False, "late": False,
                   "t0": time.monotonic(), "o": o,
                   "sym": self._sym(o.get("symbol"), o.get("asset_id")),
                   # for exits: the entry a leg hangs under; shown to the
                   # platform (orders.list) because it should not be
                   # there; replaced by a newer order of ours
                   "entry": self._leg_entry.get(oid), "exposed": False,
                   "superseded": False, "cancel_at": 0.0}
            self._orders[oid] = rec
            self._coids.add(coid)
            # An order this adapter had stopped asking Alpaca for (see
            # _resolve_unknown) and that is heard of all the same is still
            # an order from before a flatten_all: handed to _late. One
            # that is still asked for is looked at now, not at its turn.
            gone = self._dropped.pop(coid, None)
            if gone is not None:
                self._late_due[coid] = gone
                self._unsaved = True
            if gone is not None or coid in self._unknown:
                # WHEN it was first seen, whoever saw it (the stream, the
                # backstop's look-up, flatten_all's own): what was read of
                # the account before this instant does not show what the
                # order has done.
                self._arrived.append((time.monotonic(), coid, dict(
                    gone or self._unknown[coid])))
                self._wake.set()
            for pend in self._closing.values():
                if pend["coid"] == coid and pend.get("seen") is None:
                    # Its fate is known now. The entry stays a few
                    # seconds more (see _post_close): a close sized from
                    # a read made before this instant does not count it.
                    pend["seen"], pend["unknown"] = time.monotonic(), False
                    self._unsaved = True
            self._seen_order.append(oid)
            while len(self._seen_order) > 5000:
                old = self._orders.get(self._seen_order.popleft())
                if old and old["done"]:
                    self._orders.pop(old["id"], None)
        else:
            if str(o.get("updated_at") or "") >= str(
                    rec["o"].get("updated_at") or ""):
                rec["o"] = o
            if (rec["kind"] == "foreign" and oid in self._leg_kind
                    and self.BRACKETS):
                rec["kind"], rec["cid"] = self._leg_kind[oid], None
            if oid in self._leg_entry:
                rec["entry"] = self._leg_entry[oid]
        self._by_group(rec, o)
        if rec["kind"] in ("sl", "tp") and not rec.get("leveled"):
            # An exit order of ours seen for the first time, working: the
            # level it carries IS a level of the position, whether or not
            # anything here wrote it down (a leg found again after a lost
            # answer, or under a parent in Alpaca's lists). Without this
            # an exit could look like one "no level asks for" and be
            # cancelled for it.
            rec["leveled"] = True
            price = self.prot._price(rec)
            if (o.get("status") in RESTING and not rec["cancel_req"]
                    and price is not None):
                lv = self.prot.levels.setdefault(
                    rec["sym"], {"sl": None, "tp": None})
                if lv.get(rec["kind"]) is None:
                    lv[rec["kind"]] = price
        if rec["root"] != oid and o.get("status") != "replaced":
            self._current[rec["root"]] = oid
        return rec

    def _by_group(self, rec: dict, o: dict):
        """Who a complex order's parts belong to, from the group id they
        share: the entry (ours by its client id) names the group, and a
        take-profit or stop-loss order of that group is its leg, ours too.
        Legs are heard BEFORE their entry on the stream, so both orders of
        arrival are handled. Caller holds the state lock."""
        gid = o.get("group_id")
        if not gid or not self.BRACKETS:
            return
        oid = rec["id"]
        role = {"take_profit": "tp", "stop_loss": "sl"}
        if rec["kind"] == "user" and (o.get("order_class") or "") in (
                "oto", "bracket") and gid not in self._groups:
            self._groups[gid] = oid
            for other in self._orders.values():
                oo = other["o"]
                if (other["kind"] == "foreign" and oo.get("group_id") == gid
                        and oo.get("group_role") in role):
                    self._adopt_leg(other, role[oo["group_role"]], rec)
        elif rec["kind"] == "foreign" and o.get("group_role") in role \
                and gid in self._groups:
            entry = self._orders.get(self._groups[gid])
            if entry is not None:
                self._adopt_leg(rec, role[o["group_role"]], entry)

    def _adopt_leg(self, rec: dict, kind: str, entry: dict):
        rec["kind"], rec["cid"] = kind, None
        self._leg_kind[rec["id"]] = kind
        if not entry["done"]:
            rec["entry"] = self._leg_entry[rec["id"]] = entry["id"]

    def _ensure_accepted(self, rec: dict):
        if rec["kind"] == "user" and not rec["accepted"]:
            rec["accepted"] = True
            if rec["root"] == rec["id"]:
                self.emit("order.accepted", {"client_id": rec["cid"],
                                             "order_id": rec["root"]})

    @staticmethod
    def _order_price(o: dict):
        """The SPEC's `price` for an order: the limit, or a stop's trigger."""
        if o.get("type") == "stop":
            return _num(o.get("stop_price")) or None
        return _num(o.get("limit_price")) or None

    def _on_status(self, rec: dict, o: dict):
        """An order's status as the SPEC's order events. Emits each thing
        once, whichever of the response, the stream and a re-read says it
        first. Caller holds the state lock."""
        status = str(o.get("status") or "")
        visible = rec["kind"] in ("user", "foreign")
        if status in ("new", "accepted", "held", "partially_filled"):
            # A HELD order is not announced: it is a leg waiting under
            # another order, in no open-order list of Alpaca's, and not
            # yet something the platform could act on. It is announced
            # when it goes to work (and never, if it turns out to be the
            # leg of an order of ours).
            if (visible and not rec["placed"] and o.get("type") != "market"
                    and status != "held"):
                rec["placed"] = True
                self._ensure_accepted(rec)
                if rec["root"] == rec["id"]:
                    self.emit("order.placed", {
                        "client_id": rec["cid"], "order_id": rec["root"],
                        "type": o.get("type"),
                        "price": self._order_price(o),
                        "expires_at": parse_ts(o.get("expires_at"))})
            return
        if status not in ("canceled", "expired", "rejected", "replaced"):
            return
        if rec["done"]:
            return
        rec["done"] = True
        if rec["coid"] in self._late_due:
            self._wake.set()      # what was said of it is looked at now
        if rec["kind"] == "user" and (o.get("order_class") or "") in (
                "oto", "bracket"):
            self.prot.touch(rec["sym"])       # an entry and its legs ended
        if status == "replaced":
            new = self._orders.get(str(o.get("replaced_by") or ""))
            if visible and new:
                n = new["o"]
                self.emit("order.amended", {
                    "order_id": rec["root"], "price": self._order_price(n),
                    "qty": _num(n.get("qty")),
                    "expires_at": parse_ts(n.get("expires_at"))})
            return
        if not visible:
            # One of the adapter's own orders ended without (wholly)
            # filling. Whatever it was, the symbol's owner looks again: a
            # stop that is gone is put back, a position a dead close left
            # open gets its stop back.
            if not rec["cancel_req"]:
                log("%s order %s for %s ended %s at Alpaca without "
                    "filling" % (rec["kind"], rec["id"], rec["sym"],
                                 status))
                if rec["kind"] == "close":
                    part = _dec(o.get("filled_qty"))
                    self._notices["close:" + rec["sym"]] = time.monotonic()
                    self.prot.fault(
                        "close:" + rec["sym"], "the order closing %s ended "
                        "%s at Alpaca %s: the position is still open, and "
                        "its stop and target are being put back"
                        % (rec["sym"], status, "after filling %s of %s"
                           % (_fixed(part), _fixed(o.get("qty")))
                           if part > 0 else "without filling"))
            # An exit nobody here cancelled may be Alpaca's own pairing at
            # work (the fill that explains it is a moment behind): it gets
            # its grace. Everything else is looked at now.
            if (rec["kind"] in ("sl", "tp") and not rec["cancel_req"]
                    and not rec.get("superseded")):
                self.prot.exit_ended(rec, status)
            self.prot.touch(rec["sym"], PROTECT_GRACE if rec["kind"] in (
                "sl", "tp") and not rec["cancel_req"] else 0.0)
            return
        if status == "canceled":
            data = {"order_id": rec["root"], "client_id": rec["cid"]}
            if not rec["cancel_req"]:
                # Not asked for by this session: say so (SPEC 5). And
                # WHY, when a cancel-all of flatten_all's that Alpaca
                # never answered may have been carried out late.
                why = self._swept_order(rec, o)
                data["code"] = "x_alpaca_canceled"
                data["message"] = "cancelled at Alpaca, not by this " \
                    "session" + (" (%s)" % why if why else "")
            self.emit("order.cancelled", data)
        elif status == "expired":
            self.emit("order.expired", {
                "order_id": rec["root"], "client_id": rec["cid"],
                "expiry": o.get("time_in_force")})
        else:
            self.emit("order.rejected", {
                "client_id": rec["cid"], "order_id": rec["root"],
                "code": "x_alpaca_rejected",
                "message": "Alpaca rejected the order after taking it"})


    def _rejected(self, cid: str, e: AdapterError):
        """A refusal is also an event, so a platform that only watches
        events sees the order end (SPEC 8.4)."""
        code = e.code if (e.code in protocol.ERROR_CODES
                          or e.code.startswith("x_")) else "x_" + e.code
        self.emit("order.rejected", {"client_id": cid or None, "code": code,
                                     "message": str(e)})

    # == executions: the money path ======================================
    def _on_execution(self, rec: dict, exec_id: str, price: Decimal,
                      qty: Decimal, cum: Decimal, final: bool,
                      position_qty: Decimal | None, t_ms: int):
        """One execution, reported once.

        Fed by the stream and by the backstop that reads Alpaca's fills
        ledger; both carry Alpaca's execution id. An execution already
        reported is dropped here, BEFORE anything is emitted, so the same
        fill can never reach the platform twice. Caller holds the lock.
        """
        key = rec["id"] + "|" + format(cum.normalize(), "f")
        exec_id = exec_id or key
        if exec_id in self._seen_exec or key in self._seen_exec:
            return
        self._seen_exec.add(exec_id)
        self._seen_exec.add(key)
        if qty <= 0:
            return
        o = rec["o"]
        sym = rec["sym"]
        side = "sell" if str(o.get("side") or "").startswith("sell") else "buy"
        spec = self._catalog.get(sym)
        book = self._book.get(sym)
        cur = book["qty"] if book else D0
        signed = qty if side == "buy" else -qty
        included, before = False, None
        if position_qty is None:
            prev, new = cur, cur + signed
        elif self._book_src.get(sym) == "rest" and cur == position_qty:
            # A positions read that already contains this execution got
            # here first. Measure from what the book held before it.
            included = True
            new = position_qty
            before = self._book_pre.pop(sym, None)
            prev = before["qty"] if before else new - signed
        else:
            prev, new = cur, position_qty
        # A crypto buy pays its fee out of the coin bought: the position
        # gains less than was executed. Report what it gained, and the
        # difference as the fee, valued at the executed price.
        net, fees = qty, 0.0
        if side == "buy" and self._is_crypto(spec):
            gained = new - prev
            if D0 < gained < qty and (qty - gained) <= qty * FEE_SANITY:
                net = gained
                fees = -float((qty - gained) * price)
            elif gained != qty:
                log("fill %s on %s: position moved by %s for %s bought; "
                    "reported as executed" % (exec_id, sym, gained, qty))
        effect = net if side == "buy" else -qty
        closed = D0
        if prev != 0 and (prev > 0) != (effect > 0):
            closed = min(abs(effect), abs(prev))
        opened = abs(effect) - closed
        kind = rec["kind"]
        fill = {"fill_id": exec_id, "client_id": rec["cid"],
                "order_id": rec["root"], "position_id": sym, "symbol": sym,
                "side": side, "qty": float(net), "price": float(price),
                "fees": round(fees, 6), "time": t_ms}
        self._fills.append(dict(fill))
        self._watermark_ms = max(self._watermark_ms, t_ms)
        if final:
            rec["done"] = True
        if kind in ("user", "foreign") or opened > 0:
            # An order of the adapter's own (an exit, a close) reports no
            # fill of its own: what it does is the `position.closed`
            # below. Unless it OPENED exposure, which only an order with
            # no position behind it can do: that is never silent.
            self._ensure_accepted(rec)
            if final:
                self.emit("order.filled", fill)
            else:
                fill["remaining"] = float(
                    max(_dec(o.get("qty")) - cum, D0))
                self.emit("order.partial", fill)
            if kind not in ("user", "foreign"):
                self.prot.fault(
                    "opened:" + sym, "an order of this adapter's own (%s) "
                    "filled %s %s with no position behind it and OPENED a "
                    "%s position; look at %s now"
                    % ({"sl": "a stop", "tp": "a target"}.get(
                        kind, "a closing order"), _fixed(opened), sym,
                       "long" if side == "buy" else "short", sym))
        if closed > 0:
            # After the fill, never before it: a platform that saw the
            # close first would take the fill for a new position.
            # What the position was BEFORE this execution: the book, or
            # the entry a positions read replaced a moment ago.
            was = (before if included and before else book) or {}
            avg = was.get("avg") or D0
            pnl = (price - avg) * closed * (1 if prev > 0 else -1)
            had = was.get("fees") or 0.0
            share = had * float(closed / abs(prev))
            if book and not included:
                book["fees"] = had - share
            evt = {"close_id": "c-" + exec_id, "position_id": sym,
                   "client_id": was.get("cid"),
                   "qty": float(closed), "price": float(price),
                   "realized_pnl": round(float(pnl), 6) if avg else 0.0,
                   "swap": 0.0, "commission": round(share, 6),
                   "reason": {"user": "manual", "close": "manual",
                              "flatten": "flatten", "sl": "sl",
                              "tp": "tp"}.get(kind, "unknown"),
                   "time": t_ms}
            if kind == "foreign":
                # Somebody's order closed it, but not one of ours: the
                # reason is not known here and is not guessed (SPEC 5).
                evt["reason_detail"] = ("a %s order placed outside this "
                                        "session" % o.get("type"))
            self.emit("position.closed", evt)
        if not included:
            self._book_src[sym] = "stream"
            self._book_t[sym] = self._stamp()
            self._book_pre.pop(sym, None)
            if new == 0:
                self._book.pop(sym, None)
            else:
                if book is None:
                    book = self._book[sym] = {"fees": 0.0, "cid": None,
                                              "opened": None, "avg": D0}
                if opened > 0:
                    if closed > 0 or prev == 0:
                        book.update(avg=price, fees=0.0, opened=t_ms,
                                    cid=rec["cid"] if kind == "user"
                                    else None)
                    else:
                        book["avg"] = ((abs(prev) * (book.get("avg") or D0)
                                        + opened * price) / abs(new))
                    book["fees"] = (book.get("fees") or 0.0) + fees
                book["qty"] = new
        elif book is not None and opened > 0:
            book["fees"] = (book.get("fees") or 0.0) + fees
            if prev == 0:
                book.update(opened=t_ms,
                            cid=rec["cid"] if kind == "user" else None)
        if prev != 0 and (new == 0 or (new > 0) != (prev > 0)):
            # The position is over (flat, or reversed in one fill): its
            # levels are over with it, at this instant.
            self.prot.ended(sym)
        # The size changed (or an order of ours is done): the symbol's
        # stop and target are looked at again by their one owner.
        self.prot.moved(sym)
        self._account_dirty = True
        self._wake.set()


    def _on_trade_update(self, d: dict):
        """One `trade_updates` message from the stream."""
        o = d.get("order") or {}
        if not o.get("id"):
            return
        ev = str(d.get("event") or "")
        if str(o["id"]) not in self._orders and (
                o.get("order_class") or "simple") != "simple":
            # A leg of a complex order, heard before the request that
            # made it has answered. Wait for that answer: it says whose
            # order this is.
            with self._place_lock:
                pass
        with self._state_lock:
            rec = self._track(o)
            if ev in ("fill", "partial_fill"):
                pq = d.get("position_qty")
                self._on_execution(
                    rec, str(d.get("execution_id") or ""),
                    _dec(d.get("price")), _dec(d.get("qty")),
                    _dec(o.get("filled_qty")), ev == "fill",
                    None if pq in (None, "") else _dec(pq),
                    parse_ts(d.get("timestamp")) or self.now_ms())
            elif ev == "order_replace_rejected":
                # The amend did not happen. The order is still what it was:
                # say so, in the only words the SPEC has for it.
                self._current.pop(rec["root"], None)
                if rec["root"] != rec["id"]:
                    self._current[rec["root"]] = rec["id"]
                if rec["kind"] in ("user", "foreign"):
                    self.emit("order.amended", {
                        "order_id": rec["root"],
                        "price": self._order_price(o),
                        "qty": _num(o.get("qty")),
                        "expires_at": parse_ts(o.get("expires_at"))})
            elif ev == "order_cancel_rejected":
                # The cancel was taken and NOT carried out: the order is
                # working. Whoever asked, nobody is cancelling it now
                # (a late order's is asked for again: see _late).
                rec["cancel_req"] = False
                rec.pop("user_cancel", None)
                rec.pop("user_cancel_at", None)
            else:
                self._on_status(rec, o)

    # == the backstop: read again what the stream may have missed ========
    def _sync(self, baseline: bool = False, kind: str = "background",
              light: bool = False, max_wait: float = 4.0):
        """Fills, open orders and positions, read again from REST.

        Alpaca's stream has no replay, so this runs on every (re)connect,
        while the stream is down and something is in flight, and every few
        seconds while any stop or target exists (the audit of what Alpaca
        really holds). The first run of a session only records what is
        there. Later runs report what happened since, through the same
        de-duplicating path as the stream. `light` reads the fills ledger
        alone and goes on to orders and positions only if it holds
        something new: that is the once-a-second look while an order is
        in flight, and it must not eat the budget.
        """
        call = self.client.call
        kw = {"kind": "read" if baseline else kind, "max_wait": max_wait}
        stale, strangers = [], []
        with self._sync_lock:
            execs = []
            if baseline:
                # What the ledger holds already is history. Its ids are
                # marked as seen, so nothing from before this session can
                # be announced, whatever the two clocks say.
                page = call("GET", "trading", "/v2/account/activities/FILL",
                            {"direction": "desc", "page_size": 100},
                            **kw) or []
                with self._state_lock:
                    for a in page:
                        xid = str(a.get("id") or "").split("::")[-1]
                        if xid:
                            self._seen_exec.add(xid)
            else:
                q = {"after": iso_ms(max(self._watermark_ms - 2000, 0)),
                     "direction": "asc", "page_size": 100}
                for _ in range(5):
                    page = call("GET", "trading",
                                "/v2/account/activities/FILL", q, **kw) or []
                    execs += page
                    if len(page) < 100:
                        break
                    q["page_token"] = page[-1].get("id")
                if light and not [
                        a for a in execs
                        if str(a.get("id") or "").split("::")[-1]
                        not in self._seen_exec]:
                    return
            orders = self._scan_orders(None, **kw)
            started = self._stamp()
            positions = call("GET", "trading", "/v2/positions", **kw) or []
            open_ids = {str(o.get("id")) for o in orders}
            with self._state_lock:
                for o in orders:
                    rec = self._track(o)
                    if baseline:
                        rec["accepted"] = rec["placed"] = True
                    else:
                        self._on_status(rec, o)
                hidden = [(oid, kind)
                          for ids in self._exits_remembered.values()
                          if isinstance(ids, dict)
                          for oid, kind in ids.items()
                          if oid not in self._orders][:40] if baseline else []
                unknown = {str(a.get("order_id")) for a in execs
                           if str(a.get("id") or "").split("::")[-1]
                           not in self._seen_exec
                           and str(a.get("order_id")) not in self._orders}
                gone = [r["id"] for r in self._orders.values()
                        if not r["done"] and r["id"] not in open_ids]
            if baseline:
                # The exits an earlier session left at Alpaca are found
                # again THERE: the open lists above, and, for the ones no
                # open list shows (a held stop leg), each by its id.
                for oid, kind in hidden:
                    try:
                        o = call("GET", "trading", "/v2/orders/" + oid, **kw)
                    except AlpacaError as e:
                        if e.status in (404, 422):
                            continue
                        raise
                    with self._state_lock:
                        if o.get("status") in RESTING and kind in ("sl",
                                                                   "tp"):
                            self._leg_kind.setdefault(oid, kind)
                        rec = self._track(o)
                        rec["accepted"] = rec["placed"] = True
                strangers = self._lost_legs(positions, kw)
                with self._state_lock:
                    self._apply_positions(positions, started, baseline=True)
                    stale = self.prot.adopt(self._levels_remembered,
                                            self._unheld_remembered, page)
            if not baseline:
                # Orders a fill names that this session never saw, and
                # orders that left the book: read each to learn what it is
                # or how it ended. Bounded, so one bad minute cannot spend
                # the budget.
                ended = []
                for oid in (list(unknown) + gone)[:20]:
                    try:
                        o = call("GET", "trading", "/v2/orders/" + oid, **kw)
                    except AlpacaError as e:
                        if e.status in (0, 429):
                            raise
                        continue
                    with self._state_lock:
                        rec = self._track(o)
                        if oid not in unknown:
                            self._on_status(rec, o)
                            if o.get("status") == "filled":
                                if not rec["done"]:
                                    ended.append(oid)
                                rec["done"] = True
                with self._state_lock:
                    self._apply_missed(execs, positions)
                    self._apply_positions(positions, started)
                    # An order found FILLED by its status, whose last
                    # execution is not in the ledger as THIS pass read it
                    # (the ledger was read first; the order filled
                    # between the two reads): its fill has not been
                    # reported, and the order is no longer "in flight",
                    # so nothing would look for it before the next full
                    # read. The ledger is read again at the next pass.
                    # (With the stream down that was order.filled 15 s
                    # late, whenever a full read straddled a fill: seen
                    # at every count of watched orders, the 16 and 17 s
                    # among the 2.3 s.)
                    if any(not any(str(a.get("order_id")) == oid
                                   and _dec(a.get("leaves_qty")) == 0
                                   for a in execs) for oid in ended):
                        self._resync = True
                        self._wake.set()
        for sym, text in strangers:
            self.prot.fault("stranger:" + sym, text)
        for sym, old, why in stale:
            self.prot.fault(
                "stale:" + sym, "%s had a %s when this adapter last ran and "
                "Alpaca holds none for it now%s. It was NOT put back: set it "
                "again with order.modify if it is still wanted"
                % (sym, " and ".join("%s at %s" % (
                    "stop" if k == "sl" else "target", _fixed(old[k]))
                    for k in ("sl", "tp") if old.get(k) is not None),
                   "; " + why if why else ""))

    def _lost_legs(self, positions: list, kw: dict) -> list:
        """A start that knows of no stop or target for a share position
        (this profile's state file is gone, or never held them): are the
        legs of the order that OPENED it still at Alpaca? A held stop leg
        is in none of Alpaca's open-order lists; it is found under its
        parent, which has filled, in the closed list (seen on paper).

        Found, and they can only be this position's exits (ONE filled
        parent with working legs, each leg on the closing side and for
        the whole position): they are taken as its stop and target, as if
        remembered. Found, but not that plainly (two entries, a size that
        differs): they are listed as resting orders, and said. Returns
        [(symbol, what to say)].

        Where stops and targets are not offered (a live account) nothing
        is ever taken over: what an earlier run left is listed (the held
        stop too, which no open list of Alpaca's shows) and said, with how
        to remove it.
        """
        want = {}
        with self._state_lock:
            for r in positions or []:
                sym = self._sym(r.get("symbol"), r.get("asset_id"))
                qty = abs(_dec(r.get("qty")))
                if qty == 0 or not self._is_share(sym):
                    continue
                if any(x["sym"] == sym and x["kind"] in ("sl", "tp")
                       and not x["done"] for x in self._orders.values()):
                    continue
                want[sym] = (qty, "buy" if r.get("side") == "short"
                             else "sell", str(r.get("symbol") or sym))
        if not want:
            return []
        rows = self.client.call("GET", "trading", "/v2/orders", {
            "status": "closed", "nested": "true", "limit": 200,
            "direction": "desc",
            "symbols": ",".join(sorted(w[2] for w in want.values()))},
            **kw) or []
        say = []
        with self._state_lock:
            for sym, (qty, side, _) in want.items():
                found = []
                for o in rows:
                    if (self._sym(o.get("symbol"), o.get("asset_id")) != sym
                            or (o.get("order_class") or "") not in (
                                "bracket", "oto")
                            or o.get("status") != "filled"):
                        continue
                    legs = [leg for leg in o.get("legs") or []
                            if leg.get("status") in RESTING]
                    if legs:
                        found.append(legs)
                if not found:
                    continue
                sound = self.BRACKETS and len(found) == 1 and all(
                    leg.get("side") == side
                    and abs(_dec(leg.get("qty"))) == qty for leg in found[0])
                what = []
                for legs in found:
                    for leg in legs:
                        kind = "tp" if leg.get("type") == "limit" else "sl"
                        if sound:
                            self._leg_kind[str(leg.get("id"))] = kind
                        rec = self._track(leg)
                        rec["accepted"] = rec["placed"] = True
                        rec["stranger"] = not sound
                        what.append("a %s %s %s at %s" % (
                            leg.get("side"),
                            "limit" if kind == "tp" else "stop",
                            _fixed(leg.get("qty")), _fixed(
                                leg.get("limit_price") if kind == "tp"
                                else leg.get("stop_price"))))
                if sound:
                    log("%s: found again at Alpaca under the order that "
                        "opened the position: %s" % (sym, ", ".join(what)))
                elif not self.BRACKETS:
                    say.append((sym, "%s: exit orders of an earlier run "
                                "are at Alpaca for this position (%s). This "
                                "adapter does not manage stops or targets "
                                "on this account: they are listed by "
                                "orders.list and are removed there with "
                                "order.cancel (cancelling the limit takes "
                                "the stop held under it too); until then "
                                "Alpaca refuses a close of what they hold"
                                % (sym, ", ".join(what))))
                else:
                    say.append((sym, "%s: Alpaca holds exit orders for this "
                                "position that this adapter cannot tell to "
                                "be its stop and target (%s). They are "
                                "listed by orders.list and can be cancelled "
                                "there; Alpaca refuses a close while they "
                                "hold the quantity" % (sym, ", ".join(what))))
        return say

    def _apply_missed(self, execs: list, positions: list):
        """Fills from Alpaca's ledger that the stream never delivered.

        The ledger has no `position_qty`, so what a crypto buy lost to its
        fee is taken from the position itself: what the book held before,
        plus everything executed, less what Alpaca says it holds now, is
        the coin withheld, shared over the buys by size. Caller holds the
        state lock.
        """
        held = {}
        for r in positions or []:
            q = abs(_dec(r.get("qty")))
            held[self._sym(r.get("symbol"), r.get("asset_id"))] = (
                -q if r.get("side") == "short" else q)
        todo: dict[str, list] = {}
        for a in execs or []:
            xid = str(a.get("id") or "").split("::")[-1]
            t = parse_ts(a.get("transaction_time")) or 0
            rec = self._orders.get(str(a.get("order_id")))
            if (not xid or xid in self._seen_exec or rec is None
                    or t < self._session_start_ms):
                continue
            todo.setdefault(rec["sym"], []).append((rec, a, xid, t))
        for sym, items in todo.items():
            pre = self._book_pre.get(sym)
            if (self._book_src.get(sym) == "rest" and pre is not None
                    and time.monotonic() - self._book_pre_t.get(sym, 0.0)
                    < 60.0):
                # A positions read (the platform's own `positions.list`,
                # say) has ALREADY folded these executions into the book.
                # Measured from there, a close would look like the opening
                # of a short and `position.closed` would never be sent.
                # They are replayed from the book as it stood BEFORE that
                # read: the last position the platform was told about.
                if pre.get("qty"):
                    self._book[sym] = dict(pre)
                else:
                    self._book.pop(sym, None)
                self._book_pre.pop(sym, None)
                self._book_src[sym] = "ledger"
            book = self._book.get(sym)
            running = book["qty"] if book else D0
            buys = sum((_dec(a.get("qty")) for _, a, _, _ in items
                        if a.get("side") == "buy"), D0)
            gross = sum((_dec(a.get("qty")) if a.get("side") == "buy"
                         else -_dec(a.get("qty")) for _, a, _, _ in items),
                        D0)
            short = running + gross - held.get(sym, D0)
            fee_ok = (self._is_crypto(self._catalog.get(sym)) and buys > 0
                      and D0 < short <= buys * FEE_SANITY)
            for rec, a, xid, t in items:
                qty = _dec(a.get("qty"))
                if a.get("side") == "buy":
                    net = qty - (short * qty / buys if fee_ok else D0)
                    running += net.quantize(Decimal("0.000000001"))
                else:
                    running -= qty
                self._on_execution(
                    rec, xid, _dec(a.get("price")), qty,
                    _dec(a.get("cum_qty")),
                    _dec(a.get("leaves_qty")) == 0, running, t)


    def _with_legs(self, orders: list, open_parents: bool = True) -> list:
        """A nested order list as a flat one, and the place where legs
        that Alpaca named itself are recognised as the adapter's: the stop
        under an OCO pair this adapter placed, and the stop and target
        under an entry the platform placed through this profile.
        `open_parents`: the list is of orders that are open, so a leg
        under an entry in it is still waiting for that entry to fill."""
        out = []
        for o in orders or []:
            out.append(o)
            legs = o.get("legs") or []
            if not legs:
                continue
            oid = str(o.get("id"))
            kind = self._leg_kind.get(oid) or self._kind(
                str(o.get("client_order_id") or ""))[0]
            entry = kind == "user" and (o.get("order_class") or "") in (
                "oto", "bracket")
            for leg in legs:
                lid = str(leg.get("id"))
                if kind == "tp" and leg.get("type") != "limit":
                    self._leg_kind.setdefault(lid, "sl")
                elif entry:
                    self._leg_kind.setdefault(
                        lid, "tp" if leg.get("type") == "limit" else "sl")
                    if open_parents and o.get("status") in RESTING:
                        self._leg_entry[lid] = oid
                out.append(leg)
        return out

    def _scan_orders(self, sym: str | None = None, kind: str = "write",
                     max_wait: float = 2.5, **_ignored) -> list:
        """Every open order (of one symbol, or of the account) as Alpaca
        lists it NOW, legs included.

        The nested listing: a held stop sits under its OCO limit there
        and is in no flat list; the working leg of an entry that has
        filled is a row of its own, with no parent above it (and that
        entry's HELD stop is in no open list at all: it is known by its
        id, see _refresh_orders and the start of a session). A leg that
        the listing does not explain is looked for once among the closed
        orders: its parent may be an entry of ours that filled while this
        adapter was not running.
        """
        call = self.client.call
        q = {"status": "open", "limit": 500}
        if sym:
            q["symbols"] = sym
        kw = {"kind": kind, "max_wait": max_wait}
        nested = call("GET", "trading", "/v2/orders",
                      dict(q, nested="true"), **kw) or []
        # The flat list was seen to hold the same rows as the nested one,
        # less the legs (17:47 UTC and earlier). It is read as well where
        # time does not matter (the start, the periodic audit of the whole
        # account), so that a row only it shows would still be found; a
        # look at ONE symbol inside a handler's deadline does without it.
        flat = [] if sym else call("GET", "trading", "/v2/orders",
                                   dict(q, nested="false"), **kw) or []
        with self._state_lock:
            rows = self._with_legs(nested)
            have = {str(o.get("id")) for o in rows}
            rows += [o for o in flat if str(o.get("id")) not in have]
            strangers = [
                str(o.get("id")) for o in rows
                if (o.get("order_class") or "") in ("oto", "bracket", "oco")
                and str(o.get("id")) not in self._leg_kind
                and str(o.get("id")) not in self._parents_asked
                and self._kind(str(o.get("client_order_id") or ""))[0]
                == "foreign"]
        if strangers:
            q2 = {"status": "closed", "limit": 200, "nested": "true",
                  "direction": "desc"}
            if sym:
                q2["symbols"] = sym
            try:
                closed = call("GET", "trading", "/v2/orders", q2, **kw) or []
            except AlpacaError as e:
                log("parents of %d order legs not read: %s"
                    % (len(strangers), e.message))
            else:
                with self._state_lock:
                    self._parents_asked.update(strangers)
                    have = {str(o.get("id")) for o in rows}
                    for o in self._with_legs(closed, open_parents=False):
                        # a leg of an entry of ours that is still working
                        # and that no open list showed: the held stop
                        oid = str(o.get("id"))
                        if (oid in self._leg_kind and oid not in have
                                and o.get("status") in RESTING):
                            rows.append(o)
        return rows

    def _refresh_orders(self, sym: str, kind: str = "write",
                        max_wait: float = 2.5) -> list:
        """What this process knows of `sym`'s orders, made current from
        Alpaca: every open order tracked, and every order it believed
        open that Alpaca no longer lists read by id to learn how it
        ended. Raises AlpacaError."""
        rows = self._scan_orders(sym, kind=kind, max_wait=max_wait)
        open_ids = {str(o.get("id")) for o in rows}
        with self._state_lock:
            for o in rows:
                rec = self._track(o)
                self._on_status(rec, o)
                self._still_open(rec, o)
            gone = [r["id"] for r in self._orders.values()
                    if r["sym"] == sym and not r["done"]
                    and r["id"] not in open_ids]
        for oid in gone[:8]:
            try:
                o = self.client.call("GET", "trading", "/v2/orders/" + oid,
                                     kind=kind, max_wait=max_wait)
            except AlpacaError as e:
                if e.status not in (404, 422):
                    raise
                with self._state_lock:
                    if oid in self._orders:
                        self._orders[oid]["done"] = True
                continue
            with self._state_lock:
                rec = self._track(o)
                self._on_status(rec, o)
                self._still_open(rec, o)
                if o.get("status") == "filled" and not rec["done"]:
                    # Its fills, if this session has not reported them,
                    # come from the ledger.
                    rec["done"], self._resync = True, True
        return rows

    @staticmethod
    def _still_open(rec: dict, o: dict):
        """Alpaca itself says this exit order is open and is not working
        on cancelling it, seconds after this process asked for that: the
        request never arrived (its connection broke on the way). The order
        is protection again, not one "on its way out". Read from Alpaca's
        own answer only, never from memory. Caller holds the state lock."""
        if (rec["cancel_req"] and rec["kind"] in ("sl", "tp")
                and o.get("status") in RESTING
                and o.get("status") != "pending_cancel"
                and time.monotonic() - rec.get("cancel_at", 0.0) > 5.0):
            rec["cancel_req"] = False

    def _position_row(self, sym: str, kind: str = "write",
                      max_wait: float = 2.5):
        """Alpaca's position in `sym` now, or None when there is none."""
        flat = urllib.parse.quote(sym.replace("/", ""), safe="")
        try:
            return self.client.call("GET", "trading", "/v2/positions/"
                                    + flat, kind=kind, max_wait=max_wait)
        except AlpacaError as e:
            if e.status == 404:
                return None
            raise

    def _no_brackets(self) -> AdapterError:
        """sl/tp asked of an instance that does not offer them: refused
        whole, loudly, before anything is sent. Never an order placed
        without the stop it was asked to carry."""
        if type(self).BRACKETS and self.cfg["env"] != "paper":
            return AdapterError(
                "unsupported", "stops and targets are not offered on a LIVE "
                "Alpaca account by this adapter yet (paper accounts only): "
                "NOTHING was sent. Send the order without sl and tp, and "
                "keep the stop where you can see it")
        return AdapterError("unsupported", "stops and targets on a "
                            "position are not offered by this adapter: "
                            "nothing was sent")

    def handle_order_modify(self, p):
        """Set, move or clear (null) the stop and target of a position.

        The request is judged first; then the levels are handed to their
        one owner, which makes Alpaca match them (a price moved in place,
        or the orders swapped). If Alpaca refuses, the OLD levels stand
        and are put back before this call answers with the refusal.
        """
        if not self.BRACKETS:
            raise self._no_brackets()
        if p.get("trail_points"):
            raise AdapterError("unsupported", "Alpaca does not keep a "
                               "trailing stop on a position")
        sym = str(p.get("position_id") or "")
        if self._is_crypto(self._catalog.get(sym)):
            raise AdapterError(
                "trade_restricted", "Alpaca has no stop order for crypto, "
                "so a stop or target cannot be set on %s" % sym)
        prot = self.prot
        with prot.guard(sym):
            row = self._read_position(sym)
            long = row.get("side") != "short"
            with self._state_lock:
                cur = dict(prot.levels.get(sym) or {})
            sl = p["sl"] if "sl" in p else cur.get("sl")
            tp = p["tp"] if "tp" in p else cur.get("tp")
            self._check_levels(long, sl, tp)
            # as they will be sent, so that what is wanted and what Alpaca
            # holds can be compared (0.1 + 0.2 is sent as 0.3)
            sl = None if sl is None else float(_fixed(sl))
            tp = None if tp is None else float(_fixed(tp))
            # A level already through the market is refused HERE, before
            # any order at Alpaca is touched (Alpaca refuses it too: "stop
            # price must be less than current price"). Only levels that
            # changed.
            spec = self._catalog.get(sym)
            px = self._price(spec, "sell" if long else "buy") if spec \
                else None
            if px:
                for label, new, old, wrong in (
                        ("stop", sl, cur.get("sl"), (lambda v: v >= px)
                         if long else (lambda v: v <= px)),
                        ("target", tp, cur.get("tp"), (lambda v: v <= px)
                         if long else (lambda v: v >= px))):
                    if new is not None and new != old and wrong(new):
                        raise AdapterError(
                            "invalid_stops", "the %s of a %s position must "
                            "be %s the market; %s is at %s, %s was asked"
                            % (label, "long" if long else "short",
                               "below" if (label == "stop") == long
                               else "above", sym, _fixed(px), _fixed(new)))
            old = prot.set_levels(sym, sl, tp)
            try:
                out, _ = prot.reconcile(sym)
                if out != "ok":
                    raise AdapterError(
                        "x_alpaca_not_replaceable", "the orders that carry "
                        "the stop and target of %s are busy at Alpaca (an "
                        "order in the symbol is still working); ask again "
                        "in a moment" % sym, retryable=True, like="frozen")
            except BaseException as e:
                # The old levels are what stands. Put them back now if
                # there is time; the backstop does it otherwise.
                prot.set_levels(sym, *old)
                prot.touch(sym)
                try:
                    prot.reconcile(sym)
                except (AlpacaError, AdapterError, _StandDown) as e2:
                    if old != (None, None):
                        prot.fault(sym, "%s: Alpaca refused the new stop/"
                                   "target and the old one is not back in "
                                   "place yet (%s): the position may have "
                                   "NO stop; it is being put back"
                                   % (sym, self._err(e2) if isinstance(
                                       e2, AlpacaError) else e2))
                if isinstance(e, AlpacaError):
                    raise self._err(e) from None
                raise
            prot.said(sym)
        self._save_state()
        return {}

    # == orders: placing =================================================
    def _price(self, spec: dict, side: str) -> float | None:
        """A current price to size a check against: the ask for a buy, the
        bid for a sell. None when Alpaca has none to give; the local check
        is then skipped and Alpaca is the judge."""
        sym = spec["symbol"]
        q = self._quotes.get(sym)
        if q and time.monotonic() - q[3] < 5.0:
            return q[1] if side == "buy" else q[0]
        try:
            if self._is_crypto(spec):
                raw = self.client.call(
                    "GET", "data", "/v1beta3/crypto/us/latest/quotes",
                    {"symbols": sym}, timeout=4.0)
                row = (raw.get("quotes") or {}).get(sym) or {}
                last = 0.0
            else:
                raw = self.client.call(
                    "GET", "data", "/v2/stocks/snapshots",
                    {"symbols": sym, "feed": self._share_feed()},
                    timeout=4.0)
                snapshot = raw.get(sym) or {}
                row = snapshot.get("latestQuote") or {}
                last = _num((snapshot.get("latestTrade") or {}).get("p"))
        except AlpacaError as e:
            log("no price for %s: %s" % (sym, e.message))
            return None
        bid, ask = _num(row.get("bp")), _num(row.get("ap"))
        if bid > 0 and ask > 0:
            self._quotes[sym] = (bid, ask, parse_ts(row.get("t")) or 0,
                                 time.monotonic())
        # A one-sided IEX quote is not a market; the last trade is the
        # better estimate for a margin check (it is never emitted as a
        # quote).
        px = ask if side == "buy" else bid
        return px if px > 0 else (last if last > 0 else None)

    def _check_qty(self, spec: dict, qty) -> float:
        """SPEC 8.6: off the step, under the minimum or over the maximum
        is `invalid_qty`, decided here and never sent to be rounded.
        (Alpaca TRUNCATES a crypto quantity with too many decimals and
        fills it.)"""
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise AdapterError("invalid_qty", "qty must be a number")
        q = float(qty)
        if not (q > 0) or q != q or q == float("inf"):
            raise AdapterError("invalid_qty", "qty must be above zero")
        step, low, high = spec["lot_step"], spec["min_qty"], spec["max_qty"]
        if not step_ok(q, step):
            raise AdapterError(
                "invalid_qty", "%s trades in steps of %s; %r is not a "
                "whole number of them" % (spec["symbol"], _fixed(step), qty))
        if q < low - step * 1e-6:
            raise AdapterError("invalid_qty", "the smallest %s order is %s"
                               % (spec["symbol"], _fixed(low)))
        if q > high:
            raise AdapterError("invalid_qty", "the largest %s order is %s"
                               % (spec["symbol"], _fixed(high)))
        return q

    def _check_margin(self, spec: dict, qty: float, px: float):
        """Refuse what the account cannot carry, from the same numbers the
        catalog and `account.get` publish (decision recorded in the module
        docstring: half for marginable shares, all of it for the rest)."""
        rate = float(spec.get("margin_rate") or 1.0)
        need = qty * px * float(spec.get("contract_size") or 1) * rate
        for attempt in (0, 1):
            a = self._account or {}
            if rate >= 1.0:
                free = _num(a.get("non_marginable_buying_power"))
            else:
                free = max(_num(a.get("equity"))
                           - _num(a.get("initial_margin")), 0.0)
            fresh = time.monotonic() - self._account_t < 60.0
            if attempt == 0 and (need > free * 0.5 or not fresh):
                # Close to the limit, or the figures are old: read them
                # again before deciding. Small orders skip the round trip.
                # If Alpaca cannot be asked, decide on the figures held.
                try:
                    self._read_account()
                    continue
                except AlpacaError as e:
                    log("account not re-read for the margin check: %s"
                        % e.message)
                    if not a:
                        return
            if need > free + 1e-9:
                raise AdapterError(
                    "insufficient_margin", "this order needs %.2f %s and "
                    "the account has %.2f free for it"
                    % (need, self.account_currency, free))
            return

    def _check_order(self, spec: dict, side: str, typ: str, p: dict) -> dict:
        """Everything that can be refused without asking Alpaca, refused
        here with its exact code; returns the order body to send."""
        sym, crypto = spec["symbol"], self._is_crypto(spec)
        if typ not in ("market", "limit", "stop", "stop_limit"):
            raise AdapterError("unsupported",
                               "order type %r is not offered" % (typ,))
        if p.get("trail_points"):
            raise AdapterError("unsupported", "Alpaca does not keep a "
                               "trailing stop on a position")
        if p.get("oco_group"):
            raise AdapterError("unsupported", "order groups (oco) are not "
                               "offered")
        if p.get("sl") is not None or p.get("tp") is not None:
            try:
                self._check_bracket(spec, side, typ, p)
            except AdapterError as e:
                e.stateless = True
                raise
        if crypto and typ == "stop":
            raise AdapterError("unsupported", "Alpaca has no plain stop "
                               "order for crypto; stop_limit is available")
        expiry = p.get("expiry") or "gtc"
        if expiry not in ("gtc", "day") or p.get("expiry_at") is not None:
            raise AdapterError("invalid_expiry", "Alpaca honours gtc and "
                               "day; there is no gtd")
        if crypto and expiry == "day":
            raise AdapterError("invalid_expiry",
                               "crypto orders are gtc only at Alpaca")
        try:
            qty = self._check_qty(spec, p.get("qty"))
        except AdapterError as e:
            e.stateless = True    # about the number sent, not the account
            raise
        price, stop = p.get("price"), p.get("stop_price")
        for label, value, needed in (
                ("price", price, typ != "market"),
                ("stop_price", stop, typ == "stop_limit")):
            if needed and not (isinstance(value, (int, float))
                               and not isinstance(value, bool)
                               and value > 0):
                raise AdapterError("x_alpaca_invalid_price", "a %s order "
                                   "needs a %s above zero" % (typ, label))
        with self._state_lock:
            held = (self._book.get(sym) or {}).get("qty", D0)
        held_f = float(held)
        if side == "sell" and qty > max(held_f, 0.0) + spec["lot_step"] * 1e-6 \
                and not spec.get("shortable"):
            raise AdapterError(
                "trade_restricted", "%s cannot be sold short at Alpaca: "
                "selling %s would exceed the %s held"
                % (sym, _fixed(qty), _fixed(max(held_f, 0.0))))
        if not crypto and typ == "market":
            is_open = self._market_open()
            if is_open is False:
                # Alpaca would take this order and hold it until the next
                # open, which a market order may not do (SPEC 4).
                raise AdapterError("market_closed", "the US exchange is "
                                   "shut; a market order for %s cannot "
                                   "fill now" % sym)
            if is_open is None:
                raise AdapterError(
                    "x_alpaca_clock_unavailable", "could not confirm that "
                    "the exchange is open, and Alpaca would queue the order "
                    "if it is not", retryable=True, like="market_closed")
        opening = (side == "buy" and held_f >= 0) or (side == "sell"
                                                      and held_f <= 0)
        if opening:
            px = (float(price) if typ in ("limit", "stop_limit")
                  else float(price) if typ == "stop"
                  else self._price(spec, side))
            if px:
                floor = float(spec.get("min_notional") or 0)
                if floor and qty * px < floor:
                    raise AdapterError(
                        "invalid_qty", "%s of %s is worth about %.2f %s; "
                        "Alpaca's smallest order is %.0f %s"
                        % (_fixed(qty), sym, qty * px, spec["quote_ccy"],
                           floor, spec["quote_ccy"]))
                self._check_margin(spec, qty, px)
                if crypto and qty * px > CRYPTO_MAX_NOTIONAL:
                    raise AdapterError(
                        "invalid_qty", "Alpaca takes at most %.0f %s of "
                        "crypto in one order" % (CRYPTO_MAX_NOTIONAL,
                                                 spec["quote_ccy"]))
        body = {"symbol": sym, "side": side, "type": typ,
                "qty": _fixed(snap(qty, spec["lot_step"])),
                # A market order fills now or not at all: `day` for shares
                # so nothing is left over for tomorrow. Crypto has no day.
                "time_in_force": ("gtc" if crypto else "day")
                if typ == "market" else expiry}
        if typ in ("limit", "stop_limit"):
            body["limit_price"] = _fixed(price)
        if typ == "stop":
            body["stop_price"] = _fixed(price)
        if typ == "stop_limit":
            body["stop_price"] = _fixed(stop)
        return body


    def _check_bracket(self, spec: dict, side: str, typ: str, p: dict):
        if self._is_crypto(spec):
            # A code the platform can act on by trying another instrument:
            # Alpaca simply has no stop order for crypto.
            raise AdapterError(
                "trade_restricted", "Alpaca has no stop order for crypto, "
                "so a stop or target cannot be attached to %s"
                % spec["symbol"])
        if not self.BRACKETS:
            raise self._no_brackets()
        if typ != "market":
            raise AdapterError(
                "unsupported", "a stop or target can be sent with a market "
                "order, or set on the position afterwards with "
                "order.modify; not on a resting order")
        self._check_levels(side == "buy", p.get("sl"), p.get("tp"))

    @staticmethod
    def _check_levels(long: bool, sl, tp):
        for label, v in (("sl", sl), ("tp", tp)):
            if v is not None and not (isinstance(v, (int, float))
                                      and not isinstance(v, bool) and v > 0):
                raise AdapterError("invalid_stops", "%s must be a price "
                                   "above zero" % label)
        if sl is not None and tp is not None and (sl >= tp) == long:
            raise AdapterError(
                "invalid_stops", "the stop of a %s position must be %s its "
                "target" % ("long" if long else "short",
                            "below" if long else "above"))

    def _find_by_client_id(self, coid: str, kind: str = "write"):
        """The order Alpaca holds under this client id, or None."""
        try:
            return self.client.call(
                "GET", "trading", "/v2/orders:by_client_order_id",
                {"client_order_id": coid}, kind=kind)
        except AlpacaError as e:
            if e.status == 404:
                return None
            raise

    def _sent_before(self, coid: str) -> bool:
        """Has this profile sent an order under this client id before, as
        far as it remembers (this session, or the state file)?"""
        with self._state_lock:
            return (coid in self._intents or coid in self._ours
                    or coid in self._unknown)

    @staticmethod
    def _fate_unknown(e: AlpacaError) -> AdapterError:
        """The error for "this order may or may not exist at Alpaca".
        Retryable, and NEVER accompanied by `order.rejected`: an order
        whose fate is not known has not ended."""
        err = AdapterError(
            "internal", "%s; whether the order "
            "exists there is not known. Sending the same request again "
            "with the same client_id is safe: Alpaca takes an id only "
            "once, and it is asked for the id before anything else is "
            "done." % ("Alpaca answered, but %s" % e.message
                       if e.code == "no_id" else
                       "Alpaca did not answer (%s)" % (
                e.message or "no answer") if e.status < 400 else
                "the answer was HTTP %d (%s), which does not say whether "
                "Alpaca took the order" % (e.status, e.message)),
            retryable=True)
        err.unknown = True
        return err if e.code != "flatten" else to_adapter_error(e)

    def _prior_order(self, cid: str):
        """The order Alpaca already holds under this client id, or None.
        When Alpaca cannot be asked, nothing may be decided: the
        retryable "not known" error."""
        try:
            return self._find_by_client_id(cid)
        except AlpacaError as e:
            raise self._fate_unknown(e) from None

    def _post_order(self, body: dict, kind: str, cid: str | None,
                    side: str, call_kind: str = "write",
                    max_wait: float = 2.5,
                    timeout: float = HANDLER_TIMEOUT,
                    until: float | None = None,
                    note: dict | None = None):
        """Send one order, never twice. Returns (order, replayed).
        `note`, when given, is told `refused` when Alpaca refused THIS
        request as a duplicate of the client id (it made nothing; the
        order returned is an earlier request's).

        The client id is filed first, in memory (the stream may report
        the fill before this call returns) and on disk, with the order
        as one that is on the wire (so that a retry after a crash is
        recognised as one, and an adapter that stops with the request
        under way starts again knowing of an order to ask Alpaca for).
        Alpaca refuses a client id it has seen, so a duplicate refusal
        means the order ALREADY exists (an earlier attempt whose answer
        was lost, or a replay after a restart) and that order is the
        answer. When the request itself failed on the way, the order may
        or may not exist: it is looked up, not resent; and if the look-up
        fails too, its fate is unknown and that is what is said. An
        answer that is not Alpaca's own verdict on the order (a 5xx, a
        408: a gateway in front of a backend that may have got the
        request) is the same case: looked up, and of unknown fate if it
        is not there, never "nothing exists" (in tests: a 504, the order
        carried out three seconds later).
        """
        coid = body["client_order_id"]
        with self._state_lock:
            self._intents[coid] = {"kind": kind, "cid": cid}
            # (sent again under the same id, as the platform was told it
            # may: whatever arrives under it now is THIS wish; unless
            # this request makes no order, see below)
            earlier = [(store, store.pop(coid, None)) for store in (
                self._unknown, self._late_due, self._dropped)]
            if bool(cid) and kind in ("user", "close"):
                self._ours[coid] = kind
            # On the wire from here until its request has ended. That is
            # WRITTEN DOWN before the request leaves, with the closing
            # order of its symbol (_post_close): an adapter that stops
            # with an order on the wire (a kill, a crash, the platform
            # closing it) starts again knowing of an order whose fate it
            # must ask Alpaca for (see _save_state, _load_state).
            wire = self._wire.setdefault(coid, {
                "kind": kind, "cid": cid, "n": 0, "at": self.now_ms(),
                "side": str(body.get("side") or ""),
                "qty": str(body.get("qty") or ""),
                "sym": self._sym(body.get("symbol")),
                "flat": bool(self.prot.flattening
                             or self.client.stand_down.is_set())})
            wire["n"] += 1
            self._sends += 1
        made = False
        try:
            # (inside: whatever happens, the entry on the wire ends with
            # this request; flatten_all waits on one that does not)
            self._save_state()
            out = self._post_once(body, kind, cid, side, call_kind,
                                  max_wait, timeout, until, note)
            made = True
            return out
        finally:
            with self._state_lock:
                wire["n"] -= 1
                if wire["n"] <= 0 and self._wire.get(coid) is wire:
                    del self._wire[coid]
                if not made and coid not in self._unknown:
                    # THIS request made no order, and it is not itself
                    # of unknown fate: Alpaca refused it, or it was never
                    # sent. That says nothing of an EARLIER request under
                    # the id whose fate was not known: it may still
                    # arrive, and it stays asked for (and, if a
                    # flatten_all was told of it, watched) exactly as it
                    # was. (It was forgotten, for every kind of order
                    # but a close: the order then landed unwatched and
                    # unnamed.)
                    for store, was in earlier:
                        if was is not None:
                            store.setdefault(coid, was)
                self._unsaved = True      # (the backstop writes it soon)

    def _post_once(self, body: dict, kind: str, cid: str | None,
                   side: str, call_kind: str, max_wait: float,
                   timeout: float, until: float | None,
                   note: dict | None = None):
        """The request of _post_order, and what its answer means."""
        coid = body["client_order_id"]
        note = {} if note is None else note
        try:
            o = self.client.call("POST", "trading", "/v2/orders",
                                 body=body, kind=call_kind,
                                 max_wait=max_wait, timeout=timeout,
                                 until=until)
            if isinstance(o, dict) and o.get("id"):
                return o, False
            # An answer that names no order (a 2xx with an empty body, or
            # one without an id: a gateway's, not Alpaca's own) says
            # nothing about the order. It is NOT an order: Alpaca is
            # asked for the client id, as after no answer at all. (It
            # used to be tracked as an order with the id "None", and the
            # platform was answered {"order_id": "None"}.)
            raise AlpacaError(0, "its answer named no order", code="no_id")
        except AlpacaError as e:
            unique = (e.status == 422
                      and "must be unique" in e.message.lower())
            if unique:
                note["refused"] = True    # THIS request made nothing
            unsaid = e.status == 0 or e.status >= 500 or e.status == 408
            if nothing_to_close(e, body):
                # A closing order, and Alpaca found nothing (left) on that
                # side to close: THIS request made no order. Whether an
                # earlier one under this id did is for _post_close to ask.
                raise _NothingToClose("Alpaca: " + e.message) from None
            if e.code in ("deadline", "flatten", "budget") or not (
                    unique or unsaid):
                # Alpaca refused it, or it was never sent: nothing exists.
                raise self._err(e, side) from None
            with self._state_lock:
                rec = next((r for r in self._orders.values()
                            if r["coid"] == coid), None)
            if rec is not None and (unique or rec.get("by_flatten")):
                # Alpaca holds an order under this id already and this
                # session knows it: this request was refused as its
                # duplicate, or flatten_all sent (or found) the order
                # under this very id while this request was on its way
                # (see _post_close). That order is the answer; it is not
                # looked up again and never filed as of unknown fate.
                return rec["o"], True
            try:
                found = self._find_by_client_id(coid)
            except AlpacaError as e2:
                self._remember_unknown(coid, kind, cid, body)
                raise self._fate_unknown(e2 if e2.message else e) from None
            if found is None:
                if unique:
                    raise self._err(e, side) from None
                # Not there NOW. A request that broke on the way can still
                # arrive: it is looked for a little longer.
                self._remember_unknown(coid, kind, cid, body)
                raise self._fate_unknown(e) from None
            return found, True

    def _remember_unknown(self, coid: str, kind: str, cid, body: dict):
        # `flat`: flatten_all was asked for AFTER this order was sent (the
        # call that sent it was cut short for it, or one is running). Such
        # an order must not arrive unnoticed once the panic button has
        # been answered: see _flatten (which does not answer "flat" while
        # one is outstanding) and _late (when it turns up: cancelled if it
        # rests, SAID if it has filled, and never an order against it).
        flat = bool(self.prot.flattening or self.client.stand_down.is_set()
                    or (self.client.read_at() or float("inf"))
                    < self._flat_at)
        with self._state_lock:
            self._unknown[coid] = {
                "kind": kind, "cid": cid, "t0": time.monotonic(),
                "next": time.monotonic() + 1.0, "n": 0, "flat": flat,
                "side": str(body.get("side") or ""),
                "qty": str(body.get("qty") or ""),
                "sym": self._sym(body.get("symbol"))}
        self._save_state()        # (it does not end with this process)
        self._wake.set()

    def _said(self, coid: str, u: dict) -> str:
        """An order of unknown fate, in words: "buy 10 AAPL (client id x)"."""
        return "%s %s %s (%s)" % (u.get("side") or "order", u.get("qty") or "",
                                  u.get("sym") or "", u.get("cid") or coid)

    def _late_what(self, coid: str, u: dict) -> str:
        return "%s, %s," % (
            "a closing order of flatten_all's own whose answer never came"
            if u.get("kind") == "flatten" else "an order sent before "
            "flatten_all whose answer never came", self._said(coid, u))

    @staticmethod
    def _span(seconds: float) -> str:
        return "%d minutes" % (seconds // 60) if seconds >= 120 \
            else "%d s" % seconds

    def _late(self, coid: str, u: dict) -> bool:
        """An order that was sent BEFORE flatten_all, and whose fate was
        not known then, has turned up at Alpaca. This adapter places
        NOTHING against it: an order of its own is an order nobody asked
        for. What still rests of it is cancelled (a cancel is safe to
        ask twice), and what happened is SAID (`session.state`):
        cancelled with nothing filled, or FILLED, how much and when by
        Alpaca's own record, and that what the symbol holds now is the
        user's to deal with. True when there is nothing left to do
        about it. While it RESTS and cannot be cancelled this is tried
        again, with a growing pause, for LATE_WATCH (see
        _resolve_unknown); then it is said that this has stopped, with
        the order's id; and whatever was said of an order that rests is
        withdrawn when the order is seen ended, by anyone. One whose
        cancel the PLATFORM asked for (order.cancel), and Alpaca took
        before any of this method's, is left to that cancel for
        LATE_SETTLE and not claimed; still resting after that, or told
        `order_cancel_rejected`, it was not carried out, and the order
        is asked for here again like any other.

        (In tests: an order.place whose POST reached Alpaca 9 s late;
        flatten_all answered {"closed": 1} at 6.4 s; the order filled at
        9 s and ten shares stood open behind a "flat" answer. And a
        market order sent from here to close "what it filled" sold ten
        shares the user had bought on purpose after the flatten, the late
        order having filled BEFORE the flatten, which had closed it
        already; and it sold fifteen for a fill of ten when its own
        answer was lost. Whether what a late order bought is still held,
        and whose the position is by now, cannot be known in here.)
        """
        with self._state_lock:
            rec = next((r for r in self._orders.values()
                        if r["coid"] == coid), None)
        if rec is None:
            return False
        sym, key = rec["sym"], "late:" + coid
        what = "%s is at Alpaca" % self._late_what(coid, u)
        kw = {"kind": "flatten", "timeout": 4.0, "max_wait": 2.0}

        def over():
            # Not this method's any more: asked for again by the platform
            # meanwhile, so it is wanted (_replayed_place); or a new
            # flatten_all has taken over what is left. Nothing more is
            # done about it here, and nothing said. (Under the state lock.)
            return bool(rec.get("again") or rec.get("by_flatten")) \
                or coid not in self._late_due

        def say(text, done=True):
            with self._state_lock:
                if over():
                    return True
                if done:
                    self.prot._notice(key, "%s: %s" % (what, text))
                    return True
                # Said as it is: after the last try nothing is "being
                # tried" any more, and how to cancel it is said instead.
                self.prot.fault(key, "%s %s; %s. Look at %s NOW" % (
                    what, text, "this adapter has STOPPED trying after %s: "
                    "cancel it yourself if it rests (order.cancel with "
                    "order_id %s; orders.list shows it)"
                    % (self._span(LATE_WATCH), rec["root"])
                    if u.get("last") else "this is being tried again", sym))
                # It stays said for as long as the order is not seen
                # ended (it used to go by itself twelve minutes later,
                # whether or not the order still rested).
                self._notices.pop(key, None)
                return False
        with self._state_lock:
            if over():
                return True
        try:
            try:
                o = self.client.call("GET", "trading", "/v2/orders/"
                                     + rec["id"], **kw) or rec["o"]
            except AlpacaError:
                with self._state_lock:
                    if not rec["done"]:
                        raise
                    o = rec["o"]      # it has ended: the stream said so
            if o.get("status") in RESTING:
                # The platform's own order.cancel of it was taken by
                # Alpaca a moment ago: the order is being cancelled, and
                # NOT by this adapter. It is given what is left of
                # LATE_SETTLE to show that (Alpaca was seen to take 0.8 s),
                # with nothing asked and nothing claimed meanwhile, and is
                # read again. (A late DELETE of this method's was answered
                # 204 too, Alpaca answers that twice, and the order was
                # then called "cancelled by this adapter after
                # flatten_all".)
                with self._state_lock:
                    hold = (rec.get("user_cancel_at", 0.0) + LATE_SETTLE
                            - time.monotonic()) if rec.get("user_cancel") \
                        else 0.0
                if hold > 0:
                    t_end = time.monotonic() + hold
                    while time.monotonic() < t_end and not rec["done"] \
                            and rec.get("user_cancel"):
                        time.sleep(0.1)
                    o = self.client.call("GET", "trading", "/v2/orders/"
                                         + rec["id"], **kw) or o
            if o.get("status") in RESTING:
                with self._state_lock:
                    if over():
                        return True
                    if rec.pop("user_cancel", None):
                        # Taken, and LATE_SETTLE later still working: that
                        # cancel was not carried out (Alpaca's
                        # `order_cancel_rejected`, which the stream may not
                        # bring). Nobody is cancelling the order: it is
                        # asked for here, as for any late order. (Were the
                        # asking to stop once the platform's cancel had
                        # been taken, such an order could fill after
                        # flatten_all.)
                        rec.pop("user_cancel_at", None)
                    rec["cancel_req"] = True
                    rec["cancel_at"] = time.monotonic()
                    # ASKED FOR, from here on, whatever becomes of the
                    # answer; written down before the request leaves. If
                    # the order is then seen cancelled, it was this
                    # adapter that had it cancelled. (The mark used to be
                    # set on Alpaca's answer alone: with the answer lost,
                    # and after a restart, a retry under the id was told
                    # "resting" of an order that was gone.)
                    rec["late_asked"] = True
                    new = coid not in self._late_cancels
                    if new:
                        self._late_cancels[coid] = self.now_ms()
                if new:
                    self._save_state()
                try:
                    self.client.call("DELETE", "trading", "/v2/orders/"
                                     + rec["id"], **kw)
                    with self._state_lock:
                        # Alpaca took the cancel (unless it had taken the
                        # platform's first, while this one was on its way)
                        if not rec.get("user_cancel"):
                            rec["late_cancel"] = True
                except AlpacaError as e:
                    if e.status not in (404, 422):
                        raise
                # Alpaca was seen to take 0.8 s to show a cancel: it is
                # given LATE_SETTLE before "still working" may be said
                # (less when the stream has told of the order's end).
                t_end = time.monotonic() + LATE_SETTLE
                while time.monotonic() < t_end and not rec["done"]:
                    time.sleep(0.1)
                o = self.client.call("GET", "trading", "/v2/orders/"
                                     + rec["id"], **kw) or o
        except AlpacaError as e:
            return say("and could not be read or cancelled (%s)"
                       % self._err(e), False)
        with self._state_lock:
            self._track(o)
            heard = [f["time"] for f in self._fills
                     if f.get("order_id") == rec["root"] and f.get("time")]
            # (a statement that it rests and could not be cancelled)
            stale = key in self.prot.issues and key not in self._notices
        status, filled = str(o.get("status") or ""), _dec(o.get("filled_qty"))
        if status == "canceled" and rec.get("late_asked") \
                and not rec.get("user_cancel"):
            # asked for here, and now seen cancelled: this adapter's doing
            rec["late_cancel"] = True
        if status in RESTING:
            return say("and still working (%s) although its cancel was "
                       "asked for" % status, False)
        if filled <= 0:
            if stale and not rec.get("late_cancel"):
                # It was said to rest and to resist its cancel, and it
                # has ended without a cancel of this method's being taken
                # (the user's own order.cancel, Alpaca itself): what was
                # said is withdrawn, and there is nothing more to say.
                with self._state_lock:
                    if not over():
                        self.prot.clear(key)
                return True
            return say("it %s; nothing of it filled" % (
                "is cancelled" if status == "canceled"
                else "has ended (%s)" % status))
        # The time is Alpaca's own for the fill. Whether that was before or
        # after flatten_all answered is not claimed, and neither is whose
        # the position is by now. What the symbol holds at this moment is
        # read once and said, so that "yours to deal with" is not said of
        # a position flatten_all has taken care of already.
        at = parse_ts(o.get("filled_at")) or (max(heard) if heard else None)
        part = filled < _dec(o.get("qty"))
        try:
            row = self._position_row(sym, kind="flatten", max_wait=2.0)
            now = ". %s holds nothing at Alpaca at this moment: no position " \
                "of it is left open" % sym if row is None else \
                ": %s holds %s %s at Alpaca at this moment, and that is " \
                "yours to deal with (positions.list)" % (
                    sym, _fixed(abs(_dec(row.get("qty")))),
                    "short" if row.get("side") == "short" else "long")
        except AlpacaError as e:
            now = ": what %s holds now could not be read (%s) and is yours " \
                "to deal with (positions.list)" % (sym, self._err(e))
        return say("it FILLED %s%s%s%s. This adapter has placed NO order "
                   "against it and will not%s" % (
                       _fixed(filled),
                       " of %s" % _fixed(o.get("qty")) if part else "",
                       " at %s by Alpaca's record" % iso_ms(at) if at
                       else " (Alpaca's record of the order carries no time "
                       "for that)",
                       "; the rest of it %s" % (
                           "was cancelled" if status == "canceled"
                           else "ended (%s)" % status) if part else "", now))

    def _find_listed(self, ask: list, **how) -> tuple:
        """ONE request for all the orders of unknown fate in `ask`
        [(client id, entry)]: every order of the account submitted since
        the oldest of them was sent (two minutes before that, for the
        two clocks), newest first, matched on `client_order_id`. Returns
        ({client id: order}, the page was full). A full page (500 rows)
        does not reach back to all of them: what it does not show is
        then not known to be absent (see _resolve_unknown). Raises
        AlpacaError.

        (It used to be one look-up an order a pass. Twelve orders on the
        ten-minute watch were 72 requests a minute out of the 100 the
        adapter's own threads may use, and with the stream down the
        backstop, which is then how fills are heard, was refused its
        reads: order.filled 16 s after the answer with three orders on
        watch, 24 s with six, over 40 s with twelve.)"""
        now_ms, mono = self.now_ms(), time.monotonic()
        since = min(now_ms - (mono - u["t0"]) * 1000.0 for _, u in ask) \
            + (self.client.clock_skew_ms or 0) - WATCH_REACH * 1000.0
        rows = self.client.call("GET", "trading", "/v2/orders", {
            "status": "all", "after": iso_ms(int(max(since, 0))),
            "direction": "desc", "limit": WATCH_PAGE, "nested": "false"},
            **(how or {"kind": "watch", "max_wait": 0.5})) or []
        want, found = {c for c, _ in ask}, {}
        for o in rows:
            coid = str(o.get("client_order_id") or "")
            if coid in want:
                found.setdefault(coid, o)
        return found, len(rows) >= WATCH_PAGE

    def _resolve_unknown(self):
        """The backstop: ask Alpaca about every order whose fate is not
        known, until it says. Found: the order is tracked like any other
        and its events follow. Not found for half a minute: it never
        arrived, and nothing was ever announced about it.

        One that a flatten_all was told of (`flat`) is not let go at
        half a minute: flatten_all's own answer promised that it is
        watched. It stays here, asked for every ten seconds, for
        LATE_WATCH (it no longer holds a flatten_all's answer back: see
        _flatten); when that is over it is SAID that this adapter has
        stopped asking, naming the order, and should it be heard of
        after all (_track) it is still handed to _late. (Dropped at 30 s
        without a word, an order landed at 42 s and was neither
        cancelled nor named.)

        What a pass costs: ONE request, however many orders are due
        (_find_listed), instead of one each. A single order that is due
        is still asked for by its own id (one request either way, and
        the exact one). An order's own look-up is made
        beside the list only: before it is let go (the list alone never
        decides that an order did not arrive); when the page was full;
        and once in OWN_SEC as a check on the list, one order a pass at
        most. And the watch STANDS BACK when the minute is short: its
        requests (the list, and the look-up of an order on the long
        watch) are made only while the adapter's own threads have used
        less than their share less BUDGET_WATCH_ROOM (`watch` in
        _Budget.take), so that what is left is the backstop's. Refused
        that way, it asks again at its next turn and decides nothing
        meanwhile. An order's OWN look-up in its first half minute, or
        before it is let go, is made whatever the minute holds; the list,
        which comes first when several are due, stands back even then.
        What to do about one that has turned up after a
        flatten_all (_late: up to three requests and a wait, four
        seconds each when Alpaca stalls) is no longer done on this
        thread, which has the fills to hear: see _late_pass."""
        now = time.monotonic()
        with self._state_lock:
            # (one on the long watch that the stream has told of is not
            # left until its next turn, ten seconds away)
            todo = [(c, u) for c, u in self._unknown.items()
                    if u["next"] <= now or (u.get("watch")
                                            and c in self._coids)]
            if todo and len(self._unknown) > len(todo):
                # One is due: all of them are asked for with it. The
                # list costs the same for one as for twelve, and they
                # stay in step (three orders sent half a second apart
                # used to come due in two groups, one of them a single
                # order with a look-up of its own every pass).
                todo = list(self._unknown.items())
        ask = []
        for coid, u in todo:
            with self._state_lock:
                if coid in self._coids:       # the stream told us meanwhile
                    if self._unknown.pop(coid, None) and u.get("flat"):
                        self._late_due[coid] = u
                    self._unsaved = True
                    continue
            ask.append((coid, u))
        found, listed, full = {}, False, False
        if len(ask) > 1:
            try:
                found, full = self._find_listed(ask)
                listed = True
            except AlpacaError:
                for _, u in ask:
                    u["n"] += 1
                    u["next"] = now + min(10.0, 1.5 ** u["n"])
                ask = []
            # (the one whose own look-up is oldest comes first)
            ask.sort(key=lambda cu: cu[1].get("own", cu[1]["t0"]))
        checked = going = 0
        for coid, u in ask:
            o, age, own = found.get(coid), now - u["t0"], True
            if o is None and listed:
                # Not in the list. Asked for by its own id: always before
                # it is let go (three a pass, the others at the next);
                # else one order a pass at most, when the page was full
                # or its last own look-up is OWN_SEC old.
                if age > (LATE_WATCH if u.get("flat") else UNKNOWN_SEC):
                    if going >= 3:
                        u["next"] = now + 2.0
                        continue
                    going += 1
                elif not checked and (full or now - u.get(
                        "own", u["t0"]) >= OWN_SEC):
                    checked += 1
                else:
                    own = False
            if o is None and own:
                try:
                    o = self._find_by_client_id(
                        coid, "watch" if u.get("watch") and age <= LATE_WATCH
                        else "write")
                    u["own"] = now
                except AlpacaError:
                    u["n"] += 1
                    u["next"] = now + min(10.0, 1.5 ** u["n"])
                    continue
            if o is None:
                if age <= UNKNOWN_SEC:
                    u["next"] = now + 2.0
                elif u.get("flat") and age <= LATE_WATCH:
                    u["watch"], u["next"] = True, now + 10.0
                else:
                    with self._state_lock:
                        gone = self._unknown.pop(coid, None) is u \
                            and u.get("flat")
                        self._unsaved = True
                        if gone:
                            u["dropped_at"] = self.now_ms()
                            self._dropped[coid] = u
                            while len(self._dropped) > 200:
                                self._dropped.pop(next(iter(self._dropped)))
                    if gone:
                        self.prot._notice(
                            "late:" + coid, "%s has NOT arrived at Alpaca "
                            "in %s: this adapter has STOPPED asking Alpaca "
                            "for it. If this session still hears of it, it "
                            "is cancelled if it rests and named here if it "
                            "has filled; no order is placed against it"
                            % (self._late_what(coid, u),
                               self._span(LATE_WATCH)))
                continue
            with self._state_lock:
                # (still this pass's to hand on: not sent again, and not
                # found by a flatten_all, since `todo` was made)
                mine = self._unknown.get(coid) is u
            try:
                self._adopt_order(o, u["kind"], u["cid"])    # (takes it out)
            except AlpacaError:
                u["next"] = now + 2.0
                continue
            with self._state_lock:
                if mine and u.get("flat"):
                    self._late_due[coid] = u
                self._unsaved = True
        # Orders that turned up after a flatten_all: what is due of them
        # now is handed to a thread of its own, one at a time.
        due = []
        with self._state_lock:
            for coid, u in list(self._late_due.items()):
                rec = next((r for r in self._orders.values()
                            if r["coid"] == coid), None)
                ended = rec is not None and rec["done"]
                if rec is None and u.get("stopped"):
                    self._late_due.pop(coid, None)
                # Tried again with a growing pause for as long as it
                # rests, for LATE_WATCH from the first try (it was eight
                # tries in five seconds). Then it is said that this has
                # stopped, and the order is only looked at once more: when
                # it is seen ended, whoever ended it.
                if not ended and (u.get("stopped")
                                  or u.get("late_next", 0.0) > now):
                    continue
                due.append((coid, u))
            start = bool(due) and not self._late_busy \
                and not self.prot.flattening
            if start:
                self._late_busy = True
        if start:
            threading.Thread(target=self._late_pass, args=(due,),
                             name="alpaca-late", daemon=True).start()
        with self._state_lock:
            old = [c for c, u in self._unknown.items()
                   if now - u["t0"] > 10.0]
        if old:
            self._set_session(
                "unknown", "degraded", "whether %d order(s) reached Alpaca "
                "is not known yet (%s); Alpaca is being asked"
                % (len(old), ", ".join(old[:3])))
        elif "unknown" in self._components:
            self._set_session("unknown", "connected", "")

    def _late_pass(self, due: list):
        """What is due about the orders that turned up after a
        flatten_all (_late), on a thread of its own: one such thread at
        a time, started by the backstop, which goes on with its own work
        meanwhile. (It was done on the backstop's thread. A cancel that
        Alpaca does not answer holds its caller four seconds, each try
        of each order; with the stream down that thread is how fills are
        heard.)"""
        try:
            for coid, u in due:
                if self.prot.flattening or self._stop.is_set():
                    break             # a flatten_all is running: its work
                with self._state_lock:
                    if coid not in self._late_due:
                        continue
                    now = time.monotonic()
                    u.setdefault("late_t0", now)
                    u["tries"] = u.get("tries", 0) + 1
                    u["last"] = now - u["late_t0"] >= LATE_WATCH
                done = self._late(coid, u)
                with self._state_lock:
                    if done:
                        self._late_due.pop(coid, None)
                        self._unsaved = True
                    elif u["last"]:
                        u["stopped"] = True
                    else:
                        u["late_next"] = time.monotonic() + min(
                            RETRY_MAX, 0.5 * 2 ** min(u["tries"] - 1, 8))
        except Exception as e:  # noqa: BLE001 - said, and tried again
            log("late orders: %s: %s" % (type(e).__name__,
                                         self.client.scrub(str(e))))
        finally:
            with self._state_lock:
                self._late_busy = False
            self._wake.set()

    def _adopt_order(self, o: dict, kind: str, cid) -> dict:
        """An order of ours that Alpaca holds and this session has no
        record of (its answer was lost, or it is from an earlier run):
        track it, with its legs, and say what has not been said. Returns
        the record. Raises AlpacaError if its legs cannot be read."""
        coid = str(o.get("client_order_id") or "")
        entry = kind == "user" and (o.get("order_class") or "") in (
            "oto", "bracket")
        if entry and not o.get("legs"):
            # The exits Alpaca made with it are part of the order.
            o = self.client.call(
                "GET", "trading", "/v2/orders/" + str(o.get("id")),
                {"nested": "true"}, kind="write") or o
        with self._state_lock:
            first = str(o.get("id")) not in self._orders
            self._intents.setdefault(coid, {"kind": kind, "cid": cid})
            if cid and kind in ("user", "close"):
                self._ours[coid] = kind
            rec = self._track(o)
            rec["kind"], rec["cid"] = kind, cid
            if entry:
                self._file_legs(rec, o)
            status = str(o.get("status") or "")
            if status in DONE or status == "replaced":
                # Ended already: never "in flight". Fills this session has
                # not reported (if they are this session's) come from the
                # ledger.
                rec["done"] = True
                if first and _dec(o.get("filled_qty")) > 0:
                    self._resync = True
            else:
                self._ensure_accepted(rec)
                self._on_status(rec, o)
            self._unknown.pop(coid, None)
        self.prot.moved(rec["sym"])
        self._wake.set()
        return rec

    def handle_order_place(self, p):
        cid = str(p.get("client_id") or "")
        side = p.get("side")
        typ = p.get("type") or "market"
        sym = str(p.get("symbol") or "")
        exits = (p.get("sl") is not None or p.get("tp") is not None) \
            and self.BRACKETS
        with contextlib.ExitStack() as stack:
            # Every order takes the symbol's guard, like everything else
            # that can change what its stop and target must be: the
            # repair is never between its read of the position and its
            # order while this one changes the position under it. And
            # levels of a position that is over do not reach what this
            # order opens (see _Protection.fresh).
            if self.BRACKETS and sym in self._catalog:
                stack.enter_context(self.prot.guard(sym))
                self.prot.fresh(sym)
            asked, prior, bracket, body = False, None, None, None
            if cid and self._sent_before(cid):
                # A client id this profile has sent before: a replay.
                # Alpaca is asked FIRST. The pre-checks below would judge
                # it against an account the first attempt has already
                # changed, and refuse an order that exists.
                asked, prior = True, self._prior_order(cid)
            if prior is None:
                try:
                    spec = self._instrument(p.get("symbol"))
                    if side not in ("buy", "sell"):
                        raise AdapterError("x_alpaca_bad_request",
                                           "side must be buy or sell")
                    body = self._check_order(spec, side, typ, p)
                    body["client_order_id"] = cid or "brue-o-" \
                        + uuid.uuid4().hex
                    bracket = self._bracket_legs(spec, side, p, body)
                except AdapterError as e:
                    if e.retryable:
                        raise     # not an end: nothing is announced
                    if (cid and not asked and e.code not in STATELESS
                            and not getattr(e, "stateless", False)):
                        # The account may look like this BECAUSE an
                        # earlier attempt went through and this profile
                        # no longer remembers it. Alpaca knows.
                        prior = self._prior_order(cid)
                    if prior is None:
                        self._rejected(cid, e)
                        raise
            if prior is not None:
                return self._replayed_place(prior, p, cid, side, typ)
            if bracket:
                stack.enter_context(self._placing())
            try:
                o, replayed = self._post_order(body, "user", cid or None,
                                               side)
            except AdapterError as e:
                if e.code == "x_alpaca_qty_held":
                    with self._state_lock:
                        guarded = bool(self.prot.exits(spec["symbol"]))
                    if guarded:
                        # Seen on paper: while a stop or target rests,
                        # Alpaca takes no plain order that would reduce
                        # the position, on either side.
                        e = AdapterError(
                            e.code, "%s. The stop/target of the %s "
                            "position holds its quantity at Alpaca: close "
                            "it with position.close (which takes them off "
                            "first), or clear them with order.modify"
                            % (e, spec["symbol"]), like=e.like)
                if not e.retryable and not getattr(e, "unknown", False):
                    self._rejected(cid, e)
                raise e
            if replayed:
                return self._replayed_place(o, p, cid, side, typ)
            with self._state_lock:
                rec = self._track(o)
                self._ensure_accepted(rec)
                if bracket:
                    self._file_legs(rec, o)
                self._on_status(rec, o)
                root = rec["root"]
        if bracket:
            self._save_state()
        self._wake.set()
        return {"order_id": root,
                "status": "accepted" if typ == "market" else "resting"}

    @contextlib.contextmanager
    def _placing(self):
        """Held while a complex order is placed and until its legs are
        known. A handler never waits for it longer than it has."""
        left = self.client.time_left()
        # The adapter's own threads wait their turn; a handler waits no
        # longer than it has.
        if not self._place_lock.acquire(timeout=-1 if left is None
                                        else max(0.05, left)):
            raise AdapterError("x_alpaca_busy", "another order with a stop "
                               "or target is being placed; ask again in a "
                               "moment", retryable=True, like="frozen")
        try:
            yield
        finally:
            self._place_lock.release()

    def _replayed_place(self, o: dict, p: dict, cid: str, side, typ):
        """Answer an `order.place` from the order Alpaca already holds
        under its client id: the original outcome, and nothing done a
        second time (SPEC 2)."""
        sym = self._sym(o.get("symbol"), o.get("asset_id"))
        spec = self._catalog.get(sym) or {}
        sl, tp = p.get("sl"), p.get("tp")
        klass = "" if (o.get("order_class") or "") == "simple" \
            else (o.get("order_class") or "")
        try:
            qty = _dec(_fixed(snap(float(p.get("qty")), spec["lot_step"])
                              if spec else p.get("qty")))
            same = (sym == str(p.get("symbol")) and o.get("side") == side
                    and o.get("type") == typ
                    and _dec(o.get("qty")) == qty
                    and klass == ("bracket" if sl is not None
                                  and tp is not None else "oto"
                                  if sl is not None or tp is not None
                                  else "")
                    and (typ not in ("limit", "stop_limit")
                         or _dec(o.get("limit_price"))
                         == _dec(_fixed(p.get("price"))))
                    and (typ != "stop" or _dec(o.get("stop_price"))
                         == _dec(_fixed(p.get("price"))))
                    and (typ != "stop_limit" or _dec(o.get("stop_price"))
                         == _dec(_fixed(p.get("stop_price")))))
        except (TypeError, ValueError, KeyError):
            same = False
        if not same:
            # Alpaca holds a DIFFERENT order under this id (SPEC 6).
            raise AdapterError(
                "duplicate_client_id", "this client_id was already used "
                "at Alpaca for another order")
        try:
            rec = self._adopt_order(o, "user", cid or None)
        except AlpacaError as e:
            raise self._fate_unknown(e) from None
        with self._state_lock:
            late = rec.get("late_cancel") or (
                rec["coid"] in self._late_cancels
                and not rec.get("user_cancel"))
        if late and o.get("status") == "canceled":
            # The order this id made turned up after a flatten_all and
            # this adapter CANCELLED it (see _late: it asked Alpaca for
            # that, in this run or an earlier one, and the order is
            # cancelled). "resting", the
            # answer it would have had, is not true of it any more, and
            # nothing is sent a second time under an id Alpaca has taken:
            # said, so that the platform does not show a working order
            # that does not exist. It has had its `order.cancelled`; no
            # second ending is announced.
            done = _dec(o.get("filled_qty"))
            raise AdapterError(
                "x_alpaca_cancelled_after_flatten", "the order sent under "
                "this client_id reached Alpaca after flatten_all had been "
                "asked for, and this adapter cancelled it there (%s). It "
                "does not exist any more and is not sent again: if it is "
                "still wanted, send it under a NEW client_id" % (
                    "%s of %s had filled before that, and that fill stands"
                    % (_fixed(done), _fixed(o.get("qty"))) if done > 0
                    else "nothing of it filled"))
        # Asked for again, as the platform was told it may: the order is
        # wanted NOW, whatever a flatten_all before this meant for it. It
        # is not cancelled as a late one (see _late), and what was said
        # of it while that was being tried is unsaid.
        with self._state_lock:
            rec["again"] = True
        self.prot.clear("late:" + rec["coid"])
        self._save_state()
        return {"order_id": rec["root"],
                "status": "accepted" if typ == "market" else "resting"}

    def _bracket_legs(self, spec, side, p, body):
        """A market order that carries a stop and/or a target is sent as
        Alpaca's OTO (one of them) or bracket (both): Alpaca holds the
        exit orders from the instant the entry fills, and refuses the lot
        in one piece if a level is on the wrong side of the market."""
        sl, tp = p.get("sl"), p.get("tp")
        if sl is None and tp is None:
            return None
        sym = spec["symbol"]
        with self._state_lock:
            held = (self._book.get(sym) or {}).get("qty", D0)
            working = bool(self.prot.exits(sym))
        if held != 0 or working:
            # Alpaca attaches exits to the order that OPENS a position. A
            # second set beside an open position would cover only part of
            # it, and Alpaca refuses a buy while a sell stop rests.
            raise AdapterError(
                "x_alpaca_position_open", "there is already a position in "
                "%s: set its stop and target with order.modify" % sym,
                like="trade_restricted")
        body["order_class"] = ("bracket" if sl is not None
                               and tp is not None else "oto")
        if sl is not None:
            body["stop_loss"] = {"stop_price": _fixed(sl)}
        if tp is not None:
            body["take_profit"] = {"limit_price": _fixed(tp)}
        body["time_in_force"] = "gtc"     # the exits must outlive the day
        return {"sl": sl, "tp": tp, "qty": _dec(body["qty"])}


    def _file_legs(self, rec: dict, o: dict):
        """Remember the exit orders Alpaca made with an entry: they are
        the position's stop and target, and until the entry has filled
        they wait under it. The levels recorded are the ones the legs
        CARRY. Caller holds the state lock (and the placing lock)."""
        lv, working = {"sl": None, "tp": None}, o.get("status") in RESTING
        for leg in o.get("legs") or []:
            kind = "tp" if leg.get("type") == "limit" else "sl"
            lid = str(leg.get("id"))
            self._leg_kind[lid] = kind
            if working:
                self._leg_entry[lid] = rec["id"]
            lrec = self._track(leg)
            lrec["kind"], lrec["cid"] = kind, None
            lrec["entry"] = self._leg_entry.get(lid)
            if leg.get("status") in RESTING:
                lv[kind] = self.prot._price(lrec)
        if lv["sl"] is not None or lv["tp"] is not None:
            self.prot.set_levels(rec["sym"], lv["sl"], lv["tp"])

    # == orders: listing, cancelling, amending ===========================
    def _order_view(self, rec: dict, o: dict) -> dict:
        status, typ = str(o.get("status") or ""), o.get("type")
        resting = status in RESTING
        qty, filled = _num(o.get("qty")), _num(o.get("filled_qty"))
        ended = (o.get("filled_at") or o.get("canceled_at")
                 or o.get("expired_at") or o.get("failed_at"))
        return {"order_id": rec["root"], "client_id": rec["cid"],
                "symbol": rec["sym"], "type": typ, "side": o.get("side"),
                "qty": qty, "filled_qty": filled,
                "remaining_qty": max(qty - filled, 0.0),
                "price": self._order_price(o),
                "stop_price": (_num(o.get("stop_price")) or None)
                if typ == "stop_limit" else None,
                "sl": None, "tp": None,
                "expiry": o.get("time_in_force"),
                # Alpaca's own date: its GTC ends about 90 days out.
                "expires_at": parse_ts(o.get("expires_at")),
                "state": "resting" if resting
                else DONE.get(status, "cancelled"),
                "placed_at": parse_ts(o.get("submitted_at")
                                      or o.get("created_at")),
                "done_at": None if resting else parse_ts(ended),
                "fill_price": _num(o.get("filled_avg_price")) or None,
                "reason": None if resting else
                ("x_alpaca_rejected" if status == "rejected"
                 else DONE.get(status, "cancelled"))}

    def handle_orders_list(self, p):
        state = (p or {}).get("state")
        q = {"status": "open" if state == "resting" else "all",
             "limit": 500, "nested": "false", "direction": "desc"}
        if state != "resting":
            q["after"] = iso_ms(self.now_ms() - HISTORY_DAYS * 86_400_000)
        rows = self._call("GET", "trading", "/v2/orders", q) or []
        out = []
        with self._state_lock:
            seen = {str(o.get("id")) for o in rows}
            # A held leg that a start found under a filled parent and
            # could not call this position's own (see _lost_legs) is in
            # none of Alpaca's open lists: it is listed from here.
            rows = list(rows) + [
                r["o"] for r in self._orders.values()
                if r.get("stranger") and not r["done"]
                and r["id"] not in seen]
            for o in rows:
                rec = self._track(o)
                # The adapter's own closes and protective orders are not
                # the platform's orders, and a replaced order lives on
                # under its replacement. EXCEPT one that should not be
                # there (an exit with no position behind it, a close that
                # will not end): that one is shown, so the platform can
                # see what the session is degraded about, and cancel it.
                shown = rec["kind"] in ("user", "foreign") or (
                    rec.get("exposed") and o.get("status") in RESTING)
                if not shown or o.get("status") == "replaced":
                    continue
                view = self._order_view(rec, o)
                if not state or view["state"] == state:
                    out.append(view)
        return {"orders": out}

    def _live_order(self, order_id) -> tuple[dict, dict]:
        """(record, order) for the id the platform holds, followed through
        any replacements to the order that is live at Alpaca now."""
        oid = self._current.get(str(order_id), str(order_id))
        for _ in range(6):
            try:
                o = self.client.call("GET", "trading", "/v2/orders/"
                                     + urllib.parse.quote(oid, safe=""))
            except AlpacaError as e:
                if e.status in (404, 422):
                    raise AdapterError("not_found", "Alpaca has no order "
                                       "%s" % order_id) from None
                raise self._err(e) from None
            with self._state_lock:
                rec = self._track(o)
            if o.get("status") == "replaced" and o.get("replaced_by"):
                oid = str(o["replaced_by"])
                continue
            return rec, o
        raise AdapterError("internal", "order %s has too long a chain of "
                           "replacements to follow" % order_id)

    def handle_order_cancel(self, p):
        rec, o = self._live_order(p.get("order_id"))
        if str(o.get("status")) not in RESTING:
            # Alpaca answers 204 to cancelling an order that is already
            # cancelled, so the state is read first to tell the two apart.
            raise AdapterError("not_resting", "order %s is %s"
                               % (p.get("order_id"), o.get("status")))
        rec["cancel_req"] = True
        try:
            self.client.call("DELETE", "trading", "/v2/orders/" + rec["id"],
                             kind="write")
        except AlpacaError as e:
            rec["cancel_req"] = False
            if e.status == 422:
                raise AdapterError("not_resting", "Alpaca: %s"
                                   % e.message) from None
            raise self._err(e) from None
        # 204 is "request taken". `order.cancelled` follows when it is done.
        with self._state_lock:
            if not rec.get("late_cancel"):
                # Alpaca took the PLATFORM's cancel before any of this
                # adapter's (a late order's, see _late): it is the
                # user's, and _late, which may yet find the order still
                # resting for a moment, does not claim it. Not "cancelled
                # by this adapter after flatten_all" (_replayed_place).
                # (An earlier run's asking, in the state file, was not
                # taken either: the order was resting for this one.)
                # Taken is not carried out: _late holds back for
                # LATE_SETTLE from this moment only.
                rec["user_cancel"] = True
                rec["user_cancel_at"] = time.monotonic()
                rec.pop("late_asked", None)
                if self._late_cancels.pop(rec["coid"], None) is not None:
                    self._unsaved = True
        self._wake.set()
        return {}

    def handle_order_amend(self, p):
        if p.get("sl") is not None or p.get("tp") is not None:
            raise AdapterError("unsupported", "a stop or target on a "
                               "resting order is not offered")
        if p.get("expiry_at") is not None or p.get("expiry") == "gtd":
            raise AdapterError("invalid_expiry", "there is no gtd at Alpaca")
        rec, o = self._live_order(p.get("order_id"))
        status, typ = str(o.get("status")), o.get("type")
        if status not in RESTING:
            raise AdapterError("not_resting", "order %s is %s"
                               % (p.get("order_id"), status))
        spec = self._catalog.get(rec["sym"]) or {}
        body = {}
        if p.get("qty") is not None:
            if spec:
                self._check_qty(spec, p["qty"])
            body["qty"] = _fixed(p["qty"])
        if p.get("price") is not None:
            body["stop_price" if typ == "stop" else "limit_price"] = \
                _fixed(p["price"])
        if p.get("stop_price") is not None:
            if typ != "stop_limit":
                raise AdapterError("x_alpaca_invalid_price", "only a "
                                   "stop_limit order has a stop_price")
            body["stop_price"] = _fixed(p["stop_price"])
        if p.get("expiry") is not None:
            if p["expiry"] not in ("gtc", "day") or (
                    self._is_crypto(spec) and p["expiry"] == "day"):
                raise AdapterError("invalid_expiry", "Alpaca honours gtc "
                                   "and, for shares, day")
            body["time_in_force"] = p["expiry"]
        if not body:
            return {}
        # Alpaca's replace makes a NEW order. Its client id names the first
        # order of the chain, so any later session can still tell which
        # order of the platform's this is.
        root = rec["root"]
        body["client_order_id"] = coid = "%s%s:%s" % (
            P_REPL, root, uuid.uuid4().hex[:10])
        with self._state_lock:
            if rec["cid"]:
                self._alias[root] = rec["cid"]
            self._intents[coid] = {"kind": rec["kind"], "cid": rec["cid"]}
        try:
            new = self.client.call("PATCH", "trading", "/v2/orders/"
                                   + rec["id"], body=body, kind="write")
        except AlpacaError as e:
            if e.status == 422 and "not open" in e.message.lower():
                raise AdapterError("not_resting",
                                   "Alpaca: " + e.message) from None
            if e.status == 422 and status in (
                    "accepted", "pending_new", "pending_cancel",
                    "pending_replace"):
                # Alpaca will not replace an order in these states (one
                # queued for the open, for instance). Not wrong, too early.
                raise AdapterError(
                    "x_alpaca_not_replaceable", "Alpaca: %s (the order is "
                    "%s)" % (e.message, status), retryable=True,
                    like="frozen") from None
            raise self._err(e, str(o.get("side"))) from None
        with self._state_lock:
            nrec = self._track(new)
            nrec["accepted"] = nrec["placed"] = True
            self._current[root] = nrec["id"]
        self._save_state()
        # `order.amended` is sent when Alpaca confirms the replacement
        # (its `replaced` event): a 200 here is only "request taken".
        self._wake.set()
        return {}

    # == closing =========================================================
    def _read_position(self, sym: str) -> dict:
        """For a handler: the position, or `not_found`."""
        try:
            row = self._position_row(sym)
        except AlpacaError as e:
            raise self._err(e) from None
        if row is None:
            raise AdapterError("not_found", "there is no open position "
                               "in %s" % sym)
        return row

    def _close_body(self, sym: str, row: dict, qty: Decimal, coid: str):
        """A closing order: a market order MARKED as a close. Alpaca's
        `position_intent` makes the broker itself refuse it when there is
        nothing on that side of the symbol to close (seen on paper,
        2026-11-11: 422 "position intent mismatch, inferred:
        sell_to_open, specified: sell_to_close", at once, shares and
        crypto, also with the exchange shut). An unmarked market order is
        tied to nothing: sent twice, or after the position has gone, it
        opens the other side. Every closing order this adapter sends is
        built here. The stops and targets are NOT marked (what Alpaca
        does with the field on an order that rests was not observed)."""
        spec = self._catalog.get(sym)
        short = row.get("side") == "short"
        return {"symbol": sym if spec else str(row.get("symbol")),
                "side": "buy" if short else "sell",
                "type": "market", "qty": _fixed(qty),
                "time_in_force": "gtc" if self._is_crypto(spec) else "day",
                "client_order_id": coid,
                "position_intent": "buy_to_close" if short
                else "sell_to_close"}

    def _post_close(self, sym: str, row: dict, qty: Decimal, kind: str,
                    cid: str | None, call_kind: str = "write",
                    timeout: float = HANDLER_TIMEOUT, max_wait: float = 2.5,
                    read_at: float | None = None,
                    until: float | None = None, who: str | None = None):
        """Send a closing order for `sym`, sized from `row` (Alpaca's
        position as read at `read_at`, monotonic). Returns (order,
        replayed, other). `who`: the call this is sent for, when it is
        not a position.close with a client id (flatten_all).

        Two things keep a position from being closed twice.

        ALPACA'S OWN CHECK: the order is marked as a close (_close_body).
        One that arrives when nothing is left on that side is refused by
        Alpaca itself (_NothingToClose). That is not an order that
        failed and it is not sent again: Alpaca is asked whether an
        order under this id exists (an earlier request under it may be
        what closed the position: then THAT is the answer, `replayed`);
        if not, _NothingToClose is raised, and the caller reads the
        position again and answers from what is there.

        ONE CLOSING ORDER A SYMBOL, for what that check was not seen to
        cover (a close for more than is left; a second close while the
        first is still working). While the POST of a closing order of
        `sym` is on the wire, or ended without an answer that says
        whether the order exists, every further close of `sym`
        (flatten_all, a flatten_all asked again, another position.close)
        goes out under THAT order's client id: Alpaca takes an id once,
        so whichever arrives first is the one closing order and the
        other is refused as a duplicate. Before the id is used again
        Alpaca is asked for it; the order found, or the one a duplicate
        refusal names, is returned with `replayed` true and NOTHING more
        is sent: what is still to close is decided from a new read of
        the position (the order found may have been for part of it, or
        be an old one). `other`: the id was another call's;
        position.close then answers a retryable error that says so,
        flatten_all reads on.

        The entry is kept for CLOSING_KEEP seconds after its order is
        first known to be at Alpaca (it used to be dropped on first
        sight: a close answered 504 that landed in flatten_all's first
        second was sold again from flatten's first read). A close sized
        from a read made BEFORE that moment does not count the order:
        nothing is sent for it, the order is returned as above. A read
        made after it does count it (Alpaca's `qty` and `qty_available`
        do): that close is a new wish and goes out under an id of its
        own. An entry whose every POST was refused or never sent is let
        go at once.

        WHOSE REQUEST MADE THE ORDER is written down whenever a request
        goes out under an id that is not its own call's (`_served`):
        who asked under the id, for how much, and whose request Alpaca
        answered with the order. One order then stands for ONE call's
        wish, and only that call is answered from it (_whose_close;
        in tests without that: close A for 5 of 10 answered 504 and never
        carried out, close B for 5 sent under A's id and carried out, A
        retried and answered ok from B's order: ten counted closed,
        five were).
        """
        now = time.monotonic()
        who = who or cid or "position.close"
        with self._state_lock:
            for s_, p in list(self._closing.items()):
                if (p.get("seen") is not None and p["flying"] <= 0
                        and now - p["seen"] > CLOSING_KEEP):
                    del self._closing[s_]
            pend = self._closing.get(sym)
            if (pend is not None and pend.get("seen") is not None
                    and read_at is not None and read_at > pend["seen"]):
                pend = None           # counted in this read: a new wish
            again = pend is not None
            if pend is None:
                pend = self._closing[sym] = {
                    "coid": cid or (P_FLAT if kind == "flatten"
                                    else P_CLOSE) + uuid.uuid4().hex,
                    "flying": 0, "unknown": False, "kind": kind,
                    "at": self.now_ms(), "who": who, "qty": _fixed(qty)}
            pend["flying"] += 1
            coid = pend["coid"]
            rec = None
            if again and pend.get("seen") is not None:
                rec = next((r for r in self._orders.values()
                            if r["coid"] == coid), None)
        other, known = again and coid != cid, False
        try:
            if rec is not None:
                # Known to be at Alpaca, and not in the read this close
                # was sized from: nothing is sent beside it.
                known = True
                return rec["o"], True, other
            if again:
                try:
                    o = self._find_by_client_id(coid, call_kind)
                except AlpacaError:
                    o = None      # cannot be asked: the POST below says
                if o is not None:
                    known = True
                    return o, True, other
            # (The entry made above is written down BEFORE the request
            # leaves, by _post_order's own save: an adapter that stops
            # with this POST on the wire must not close the symbol under
            # another id at its next start.)
            body = self._close_body(sym, row, qty, coid)
            counted, note = False, {}
            with self._state_lock:
                earlier = self._unknown.get(coid)
                mine = (pend.get("who") or coid) == who
                if (not mine or coid in self._served) \
                        and not coid.startswith((P_CLOSE, P_FLAT)):
                    # A request under an id another call's request has
                    # used (a position.close's client id: one this
                    # adapter made up is asked for again by nobody): both
                    # are written down, with what each asked for (before
                    # the request leaves: _post_order saves).
                    s = self._served.setdefault(
                        coid, {"askers": {}, "maker": None})
                    askers = s["askers"]
                    askers.setdefault(pend.get("who") or coid, str(
                        pend.get("qty") or (earlier or {}).get("qty") or ""))
                    # Each request of a call that FIRST asked under the id
                    # after this entry was made is counted, and so is each
                    # that Alpaca refused (below): a call all of whose
                    # requests were refused made nothing. (The requests of
                    # the call the entry was made for are not all counted:
                    # some went out before it.)
                    sent = s.setdefault("sent", {})
                    if who in sent or who not in askers:
                        sent[who] = sent.get(who, 0) + 1
                        counted = True
                    askers[who] = _fixed(qty)

            def refused():
                # Alpaca refused THIS request: it made nothing. Written
                # down, so that _whose_close does not suspect it of the
                # order (in tests without that: X1 answered 504 and
                # carried out; X2's request under X1's id refused "must
                # be unique"; X1 retried was told "neither was answered:
                # send it again", and a user who obeyed closed twice).
                with self._state_lock:
                    s = self._served.get(coid)
                    if counted and s is not None:
                        no = s.setdefault("refused", {})
                        no[who] = no.get(who, 0) + 1
            try:
                o, replayed = self._post_order(
                    body, kind, cid, body["side"], call_kind,
                    max_wait=max_wait, timeout=timeout, until=until,
                    note=note)
                if not replayed:
                    with self._state_lock:
                        if coid in self._served:
                            # Alpaca answered THIS request with the order
                            self._served[coid]["maker"] = who
                elif note.get("refused"):
                    refused()
            except _NothingToClose:
                refused()
                # Has an order under this id closed it (an earlier
                # request of this very call that arrived first; the one
                # flatten_all sent under this id meanwhile)? What this
                # session already knows is not asked for again.
                with self._state_lock:
                    had = next((r for r in self._orders.values()
                                if r["coid"] == coid), None)
                if had is not None:
                    o = had["o"]
                else:
                    try:
                        o = self._find_by_client_id(coid, call_kind)
                    except AlpacaError as e:
                        o, why = None, e
                    else:
                        why = None
                    if o is None:
                        # THIS request made no order. An earlier one
                        # under the id whose fate was not known is still
                        # of unknown fate (the refusal says nothing of
                        # it): it stays asked for, as it was.
                        with self._state_lock:
                            if earlier is not None:
                                self._unknown.setdefault(coid, earlier)
                        if why is not None:
                            # (Alpaca cannot be asked: nothing is decided)
                            raise self._fate_unknown(why) from None
                        raise
                replayed = True
            known = True
            return o, replayed, other
        finally:
            with self._state_lock:
                pend["flying"] -= 1
                if known:
                    if pend.get("seen") is None:
                        pend["seen"] = time.monotonic()
                    pend["unknown"] = False
                elif coid in self._unknown:
                    pend["unknown"] = True
                if (not known and pend["flying"] <= 0
                        and not pend["unknown"]
                        and pend.get("seen") is None
                        and self._closing.get(sym) is pend):
                    del self._closing[sym]
                self._unsaved = True

    def _is_share(self, sym: str) -> bool:
        spec = self._catalog.get(sym)
        return not (self._is_crypto(spec)
                    or (not spec and str(sym).endswith("USD")))

    def handle_position_close(self, p):
        """Close a position, or `qty` of it, with a market order.

        In this order, so that a refusal can never cost a position its
        stop: (1) the request is judged (the number, the size, the clock,
        Alpaca's wash-trade rule, whether this very close already exists
        at Alpaca); (2) only then, under the symbol's guard, are the stop
        and target taken off and the quantity waited for; (3) the closing
        order goes out, marked as a close, and is tracked. Whatever
        happens after (2), the symbol is already marked for its owner:
        what the position should have is put back, for all of it if the
        close did not happen and for what is left if it did. A closing
        order that Alpaca refuses because it finds nothing left to close
        is answered from a new read of the position (_nothing_left).
        """
        sym = str(p.get("position_id") or "")
        cid = str(p.get("client_id") or "")
        want = p.get("qty")
        spec = self._catalog.get(sym) or {}
        prot = self.prot
        qty = None
        if want is not None:
            if spec:
                self._check_qty(dict(spec, min_qty=spec["lot_step"]), want)
            elif (isinstance(want, bool) or not isinstance(
                    want, (int, float)) or not want > 0):
                raise AdapterError("invalid_qty", "qty must be a number "
                                   "above zero")
            qty = _dec(_fixed(want))
        with prot.guard(sym):
            retry = bool(cid) and self._sent_before(cid)
            if retry:
                # This profile has sent this close before. Alpaca is asked
                # before anything is touched (F1b).
                prior = self._prior_order(cid)
                if prior is not None:
                    return self._replayed_close(prior, cid)
            t_row = time.monotonic()      # (the read is made after this)
            try:
                row = self._read_position(sym)
            except AdapterError as e:
                if retry and e.code == "not_found":
                    again = self._retried_close(cid)
                    if again is not None:
                        return again
                raise
            total = abs(_dec(row.get("qty")))
            long = row.get("side") != "short"
            if qty is not None and qty > total:
                raise AdapterError("invalid_qty", "the position in %s is "
                                   "%s" % (sym, _fixed(total)))
            if self._is_share(sym):
                is_open = self._market_open()
                if is_open is False:
                    raise AdapterError("market_closed", "the US exchange "
                                       "is shut; %s cannot be closed now"
                                       % sym)
                if is_open is None:
                    # Not known is not shut. Alpaca would HOLD a
                    # closing order sent to a shut exchange until the next
                    # open, with the stop off meanwhile: nothing is
                    # touched, and the caller is told to ask again.
                    raise AdapterError(
                        "x_alpaca_clock_unavailable", "could not confirm "
                        "that the exchange is open, and Alpaca would hold "
                        "the closing order if it is not; nothing was "
                        "touched. Ask again.", retryable=True)
            if self._is_share(sym):
                try:
                    self._refresh_orders(sym)
                except AlpacaError as e:
                    raise self._err(e) from None
            # (Crypto has no stop order at Alpaca, so there is nothing of
            # this adapter's to find or take off first: its close does
            # not spend a request on looking.)
            side = "sell" if long else "buy"
            with self._state_lock:
                exits = prot.exits(sym)
                rest = prot._recs(sym, ("user", "foreign"))
                against = [r for r in rest if r["o"].get("side") != side
                           and (r["o"].get("order_class") or "") in (
                               "", "simple")
                           and r["o"].get("status") != "held"]
                others = sum((prot._left(r) for r in rest
                              if r["o"].get("side") == side
                              and r["o"].get("status") != "held"), D0)
            if exits and against:
                # Alpaca refuses a market order while an order of the same
                # account rests on the other side ("potential wash
                # trade"). Refused HERE, with the stop still on.
                raise AdapterError(
                    "x_alpaca_wash_trade", "Alpaca would refuse to close "
                    "%s while your %s order %s is open in it (its wash-"
                    "trade rule). Cancel that order first. The position's "
                    "stop and target were not touched."
                    % (sym, against[0]["o"].get("side"),
                       against[0]["root"]), like="trade_restricted")
            if exits and cid and not retry:
                # About to take a stop off: is this close one Alpaca
                # already has (sent by a run this profile has no memory
                # of)? Then there is nothing to do, and nothing to undo.
                prior = self._prior_order(cid)
                if prior is not None:
                    return self._replayed_close(prior, cid)
            prot.clear("close:" + sym)
            try:
                t_rel = time.monotonic()
                fresh = prot.release(sym, exits, others)
                if exits:
                    # sized from the read that saw the quantity free
                    if fresh is None:
                        t_rel = time.monotonic()
                    row, t_row = fresh or self._read_position(sym), t_rel
                avail = abs(_dec(row.get("qty_available")))
                # No `qty` is the WHOLE position, and it is judged as an
                # explicit qty of all of it is: what other orders hold is
                # not left behind under an "ok". (It used to close what
                # was free and answer ok: a long 10 beside the user's
                # resting SELL 4 sold 6, said nothing of the 4. This
                # adapter's own stop and target are not "other orders":
                # they were taken off above.)
                size = abs(_dec(row.get("qty"))) if qty is None else qty
                if size <= 0 or size > avail:
                    again = self._retried_close(cid) if retry else None
                    if again is not None:
                        return again
                    if qty is None:
                        busy = self._own_close_working(sym, row, avail)
                        if busy is not None:
                            raise busy
                    raise self._qty_held(sym, row, avail, qty)
                try:
                    o, replayed, other = self._post_close(
                        sym, row, size, "close", cid or None, read_at=t_row)
                except _NothingToClose as e:
                    raise self._nothing_left(sym, row, e) from None
                if replayed and not other and cid:
                    # found under this close's own id: its own order?
                    self._whose_close(o, cid)
            except BaseException as e:
                prot.touch(sym)      # the exits may be off: put back
                if isinstance(e, AlpacaError):
                    raise self._err(e) from None
                raise
            with self._state_lock:
                # Tracked: with the stream down it is polled once a second
                # until it has filled, so its `position.closed` is not
                # late and cannot be lost.
                rec = self._track(o)
                if replayed and o.get("status") in DONE:
                    rec["done"] = True
                    self._resync = True
            prot.touch(sym, 0.3)
            if other and replayed:
                # A close of this symbol sent BEFORE this one, and not
                # answered then, is at Alpaca: THAT is the closing order.
                # Nothing was sent for this call (it would be a second
                # close), and nothing is answered as done: when that
                # order arrived, and whether it is what this call asked
                # for, is for a new read of the position to say.
                raise AdapterError(
                    "x_alpaca_close_pending", "a close of %s sent before "
                    "this one (%s %s, client id %s, now %s) is at Alpaca, "
                    "and it is not in the read of the position this call "
                    "was sized from (it had not been answered, or it "
                    "arrived a moment ago); this one was NOT sent, so "
                    "that the position is not closed twice. Read the "
                    "position and ask again for what is left."
                    % (sym, o.get("side"), _fixed(o.get("qty")),
                       o.get("client_order_id"), o.get("status")),
                    retryable=True, like="frozen")
        self._wake.set()
        return {}

    def _retried_close(self, cid: str):
        """A position.close under a client id this profile has sent is
        about to be answered `not_found` or `x_alpaca_qty_held`: its own
        id is asked for at Alpaca ONCE more. Its first request may have
        landed after the look-up made at the start of this call and
        before the position was read, and that order is what closed the
        position or holds it. The original outcome (SPEC 2): what
        _replayed_close answers, or None when there is no such order.
        (It was answered "Nothing was closed", not retryable, while its
        own order sold the position.)"""
        prior = self._prior_order(cid)
        return None if prior is None else self._replayed_close(prior, cid)

    def _own_close_working(self, sym: str, row: dict,
                           avail: Decimal) -> AdapterError | None:
        """position.close without qty, and what is not free of `sym` is
        held by closing orders of THIS adapter's alone (an earlier
        position.close, a flatten_all's) that are still working: the
        retryable `x_alpaca_close_pending`, naming them, or None when
        any other order holds part of it, or when the ones known do not
        account for all that is held. Nothing was closed: what that
        order leaves is closed when asked again. (It was answered the
        final `x_alpaca_qty_held` "Cancel them first": the platform does
        not see such an order in orders.list, and cancelling it would
        undo the close it was answered for.)"""
        whole = abs(_dec(row.get("qty")))
        side = "buy" if row.get("side") == "short" else "sell"
        with self._state_lock:
            holding = [r for r in self.prot._recs(sym, (
                "user", "foreign", "close", "flatten"))
                if r["o"].get("side") == side
                and r["o"].get("status") != "held"]
            mine = [r for r in holding if r["kind"] in ("close", "flatten")]
            covered = sum((self.prot._left(r) for r in mine), D0)
            said = ["%s %s %s (client id %s, order_id %s, %s)" % (
                side, _fixed(self.prot._left(r)), sym, r["coid"], r["root"],
                r["o"].get("status")) for r in mine]
        if not mine or len(mine) < len(holding) or covered < whole - avail:
            return None
        return AdapterError(
            "x_alpaca_close_pending", "a closing order of this adapter's "
            "is still working in %s: %s. position.close without qty closes "
            "the WHOLE position (%s %s), and while that order holds %s of "
            "it nothing more is sent, so that the position is not closed "
            "twice. Nothing was closed for this call; %s is free now. Ask "
            "again once it has filled, for what is left." % (
                sym, "; ".join(said[:4]), _fixed(whole),
                "short" if side == "buy" else "long",
                _fixed(max(whole - avail, D0)), _fixed(avail)),
            retryable=True, like="frozen")

    def _qty_held(self, sym: str, row: dict, avail: Decimal,
                  want) -> AdapterError:
        """position.close for more than is free to close of `sym` (`want`
        None: the whole position): what is free, what is held, and the
        orders this session knows of that hold it. Nothing was closed."""
        whole = abs(_dec(row.get("qty")))
        side = "buy" if row.get("side") == "short" else "sell"
        with self._state_lock:
            by = ["%s %s %s %s (%s, order_id %s)" % (
                r["o"].get("type") or "order", side,
                _fixed(self.prot._left(r)), sym,
                "a closing order of this adapter's"
                if r["kind"] in ("close", "flatten")
                else "client id %s" % (r["cid"] or r["coid"]), r["root"])
                for r in self.prot._recs(sym, ("user", "foreign", "close",
                                               "flatten"))
                if r["o"].get("side") == side
                and r["o"].get("status") != "held"]
        return AdapterError(
            "x_alpaca_qty_held", "%s%s of the %s position is free to "
            "close; the rest (%s) is held by other open orders in %s: %s. "
            "Nothing was closed%s" % (
                "position.close without qty closes the WHOLE position (%s "
                "%s), and " % (_fixed(whole), "short" if side == "buy"
                               else "long") if want is None else "",
                _fixed(avail), sym, _fixed(max(whole - avail, D0)), sym,
                "; ".join(by[:4]) + (" and %d more" % (len(by) - 4)
                                     if len(by) > 4 else "")
                if by else "they are listed by orders.list",
                ". Cancel them first, or close %s with qty"
                % _fixed(avail) if want is None and avail > 0 else ""),
            like="invalid_qty")

    def _nothing_left(self, sym: str, row: dict, e: AdapterError):
        """position.close: Alpaca refused the closing order because it
        found nothing (left) on that side to close, and holds no order
        under this close's id. Nothing was traded and nothing is sent
        again. The position is read once more and the answer is what is
        there: gone -> `not_found`, as for any position that does not
        exist (it was closed by something else: a stop, a flatten_all,
        another close, the account's owner at Alpaca); still there ->
        a retryable error that names what is held, because Alpaca and
        its own position list then say two things and only a new
        request can tell."""
        side = "short" if row.get("side") == "short" else "long"
        try:
            now = self._position_row(sym)
        except AlpacaError:
            now = False
        if now is None:
            return AdapterError(
                "not_found", "there is no open position in %s: Alpaca "
                "found nothing left to close (%s). It was closed before "
                "this order arrived; nothing was traded for this call"
                % (sym, e))
        if now is False:
            held = "what %s holds now could not be read" % sym
        else:
            held = "%s reads as %s %s now" % (
                sym, _fixed(abs(_dec(now.get("qty")))),
                "short" if now.get("side") == "short" else "long")
        return AdapterError(
            "x_alpaca_nothing_to_close", "Alpaca refused the order "
            "closing the %s of %s %s: it found nothing, or less than "
            "that, left to close (%s). Nothing was traded for this call; "
            "%s. Read the position and ask again for what is left."
            % (side, _fixed(abs(_dec(row.get("qty")))), sym, e, held),
            retryable=True, like="frozen")

    def _replayed_close(self, o: dict, cid: str) -> dict:
        """A `position.close` whose order Alpaca already holds: the
        original outcome, nothing done twice, no stop touched. (If it
        is this close's order: see _whose_close.)"""
        self._whose_close(o, cid)
        rec = self._adopt_order(o, "close", cid)
        self.prot.touch(rec["sym"])
        return {}

    def _whose_close(self, o: dict, cid: str):
        """An order is at Alpaca under the client id of the
        position.close `cid`. Was it THIS close's request that made it?
        Usually nobody else's could have. But while a close is of
        unknown fate, every further close of its symbol goes out under
        its id (_post_close), and Alpaca takes the id from whichever
        request reaches it first. Between two position.close calls the
        order then stands for the wish of the ONE whose request made
        it, and that call has been answered for it: their quantities
        add up, and an order counted for both is a close the user
        believes in and does not have. Returns when the order may be
        answered as this close's; raises:

          - `x_alpaca_close_not_carried_out`, final: another
            position.close's request made the order (Alpaca answered
            that request with it, or the order's quantity is what that
            call alone asked for). This close was NOT carried out, and
            nothing can be sent again under an id Alpaca has taken: the
            error names the close that was served and asks for a NEW
            client_id. (It used to answer ok: close A for 5 of 10
            answered 504 and never carried out, close B for 5 carried
            out under A's id, A retried: both ok, ten counted closed,
            five were.)
          - `x_alpaca_close_not_known`, retryable: this close's request
            and another position.close's both ended without an answer,
            they asked for the same quantity, and ONE order exists.
            Which of the two made it cannot be known; ok is not guessed.

        A call every request of which under the id Alpaca REFUSED (as a
        duplicate of the id, or finding nothing left to close) made
        nothing and is no suspect. (In tests without that: close X1
        answered 504 and carried out 3 s later; X2's request under X1's
        id refused as a duplicate; X1 retried was told "not known ...
        neither was answered", which was untrue of X2's, and asked to
        send the close again: the user who obeyed closed twice.)

        An order that flatten_all's request made under this close's id
        is answered as this close's, as it always was: flatten_all's
        wish is "everything closed", it covers the close's, and nothing
        is counted twice. What served whom is in the state file
        (_save_state, `served`)."""
        coid = str(o.get("client_order_id") or cid)
        with self._state_lock:
            s = self._served.get(coid)
            askers = dict(s["askers"]) if s else {}
            maker = s.get("maker") if s else None
            sent = dict(s.get("sent") or {}) if s else {}
            no = dict(s.get("refused") or {}) if s else {}
        # A call every request of which Alpaca refused (a duplicate of the
        # id, or nothing left to close) made nothing: it is no suspect.
        # (The call Alpaca answered with the order is never one of them.)
        nothing = {w for w, n in sent.items() if n and no.get(w, 0) >= n
                   and w != maker}
        askers = {w: q for w, q in askers.items() if w not in nothing}

        def flat(w):
            return str(w or "").startswith("flatten_all:")
        closes = [w for w in askers if w != cid and not flat(w)]
        if not closes or maker == cid or flat(maker):
            return
        suspects = [maker] if maker else closes
        if maker is None:
            # Not answered to anyone. The quantity may still tell: a
            # request makes an order for what IT asked.
            same = [w for w, q in askers.items() if q not in (None, "")
                    and _dec(q) == _dec(o.get("qty"))]
            if same and all(w == cid or flat(w) for w in same):
                return
            if same:
                suspects = [w for w in same if w != cid and not flat(w)]
                maker = suspects[0] if cid not in same else None

        def call(w):
            return ("position.close with client_id %s" % w
                    if w != "position.close" else
                    "a position.close sent without a client_id")
        what = "%s %s %s, %s" % (o.get("side"), _fixed(o.get("qty")),
                                 self._sym(o.get("symbol"),
                                           o.get("asset_id")),
                                 o.get("status"))
        who = " or ".join(call(w) for w in suspects)
        if maker is None:
            raise AdapterError(
                "x_alpaca_close_not_known", "whether this close was "
                "carried out is NOT known and cannot be told from "
                "Alpaca: ONE order (%s) exists under its client_id, and "
                "the request of this close and that of %s both went out "
                "under that id, for the same quantity, and neither was "
                "answered. One of them was carried out, once. Read "
                "the position, and send what is still to close under a "
                "NEW client_id" % (what, who),
                retryable=True, like="frozen")
        raise AdapterError(
            "x_alpaca_close_not_carried_out", "this close was NOT "
            "carried out: its own request never made an order at "
            "Alpaca. The order that exists there under its client_id "
            "(%s) was made by the request of another call, %s, which "
            "went out under this id (one closing order a symbol) and "
            "has been answered for it. Nothing is sent again under an "
            "id Alpaca has taken: if this close is still wanted, read "
            "the position and send it under a NEW client_id"
            % (what, who))

    def handle_flatten_all(self, p):
        """Cancel every resting order, close every position, and answer
        only when the account is flat (SPEC 8.1).

        The panic button: its requests are never refused locally (not for
        a 429 block, not for a spent minute), the backstop's repair of
        stops stands down while it runs, and an unknown clock does not
        stop it (Alpaca is the judge). Every position gets ONE market
        order of its own, marked as a close and sized to what Alpaca
        says is free, however many there are (Alpaca's close-all call
        is not used: see _flatten). A share while the exchange is KNOWN
        to be shut cannot be closed: it is not touched, it keeps its
        stop, and the answer is an error that names it.

        If the account is not flat when the time is up, this call does
        not leave its own work behind: the closing orders it placed and
        that are still working are cancelled, every position still open
        is handed to the owner of the stops (which puts them back), and
        the error says what is open and why.
        """
        prot = self.prot
        # FLATTEN_BUDGET from when the request was READ (the platform's
        # ten seconds started then); never less than FLATTEN_FLOOR minus
        # the time kept for cleaning up, however long it queued.
        left = self.client.time_left()
        t_end = time.monotonic() + (FLATTEN_BUDGET if left is None else max(
            0.5, left - (FLATTEN_HARD - FLATTEN_BUDGET)))
        prot.flattening = True
        got = []

        def stand():
            # A call that changes something and is still being answered
            # has been told to stop (client.stand_down, set when this
            # request's line was READ): it makes no further request and
            # lets go of its symbol. A repair step in flight comes back
            # likewise. Neither is waited for longer than two seconds
            # (flatten's first reads go out meanwhile).
            t_wait = time.monotonic() + 2.0
            self._orders_free.wait(2.0)
            if prot.busy.acquire(timeout=max(0.05, t_wait
                                             - time.monotonic())):
                got.append(1)
        try:
            return self._flatten(t_end, stand, "flatten_all:%s"
                                 % (p.get("client_id") or ""))
        except AlpacaError as e:
            raise self._err(e) from None
        finally:
            if got:
                prot.busy.release()
            prot.flattening = False
            self._wake.set()

    def _flatten(self, t_end: float, stand=None,
                 who: str = "flatten_all:") -> dict:
        prot = self.prot
        t_call = time.monotonic()         # (when this call's work began)
        kw = {"kind": "flatten", "timeout": 4.0, "max_wait": 1.5}
        mine = threading.Lock()

        def call(method, host, path, query=None, lost=None):
            """One of flatten's own requests. An answer that may be better
            next time (none at all, a 5xx, a 429) is asked for again while
            there is time: the panic button does not end on one bad
            answer (SPEC 8.1). All of these are safe to repeat. `lost`
            is told of every attempt that failed (the cancel-all: an
            attempt that was not answered may still be carried out, also
            when the next one works)."""
            while True:
                try:
                    return self.client.call(method, host, path, query, **kw)
                except AlpacaError as e:
                    if lost is not None:
                        lost(e)
                    again = e.status in (0, 429) or e.status >= 500
                    if (not again or e.code == "deadline"
                            or time.monotonic() > t_end - 1.0):
                        raise
                    time.sleep(0.3)

        def beside(*fns):
            """Requests that do not depend on each other go out side by
            side, the first on this thread. On a slow link the panic
            button's time is the number of round trips it makes ONE AFTER
            ANOTHER (seen: 1.5 s each, seven in a row: not flat inside the
            platform's ten seconds, with an account that was flat a moment
            later)."""
            left = self.client.time_left()
            out, errs = [None] * len(fns), [None] * len(fns)

            def run(i):
                if i:
                    self.client.set_deadline(left)
                try:
                    out[i] = fns[i]()
                except BaseException as e:  # noqa: BLE001 - raised below
                    errs[i] = e
                finally:
                    if i:
                        self.client.set_deadline(None)
            threads = [threading.Thread(target=run, args=(i,), daemon=True,
                                        name="alpaca-flatten")
                       for i in range(1, len(fns))]
            for t in threads:
                t.start()
            run(0)
            for t in threads:
                t.join()
            for e in errs:
                if e is not None:
                    raise e
            return out

        def cancel(oid):
            try:
                call("DELETE", "trading", "/v2/orders/" + oid)
            except AlpacaError as e:
                if e.status not in (404, 422):
                    raise

        def held(rows):
            return {self._sym(r.get("symbol"), r.get("asset_id")): r
                    for r in rows or [] if _dec(r.get("qty")) != 0}

        def positions():
            return call("GET", "trading", "/v2/positions")

        def resting():
            return call("GET", "trading", "/v2/orders", {
                "status": "open", "limit": 500, "nested": "false"}) or []

        def quiet():
            """(no order request is on its way to Alpaca now, how many
            order POSTs have begun so far). On its way: a call of the
            orders lane is being answered (place, modify, amend, cancel,
            close), or a POST of _post_order has not ended (a call's, a
            stop or target of the protection's, one begun before this
            call)."""
            with self._state_lock:
                return (self._orders_free.is_set() and not self._wire,
                        self._sends)

        # Was a call that changes something still being answered when this
        # began? Then what is read now may be a moment behind it.
        # (an order answered this instant has not yet left its lane: 50 ms)
        clear = self._orders_free.wait(0.05)
        with self._state_lock:
            for u in self._unknown.values():
                u["flat"] = True
            odd = {u.get("sym") for u in self._late_due.values()}
            taken = ["late:" + c for c in self._late_due]
            self._late_due.clear()        # ... and this call's own work now
            self._unsaved = True
        prot.clear(*taken)        # (what was said of them: this call answers)
        again, waiting, landed = set(), [], []

        def unknown():
            """Orders sent before this began whose fate Alpaca has not
            told (the call that sent one was cut short, or its answer was
            lost): asked for by their client id, now. One that is found
            is an order like any other from here on: cancelled below if
            it rests, closed if it has filled. Returns those still not
            known; while there is one, the account is not known to be
            flat."""
            with self._state_lock:
                # (One that Alpaca has not held for UNKNOWN_SEC is on the
                # backstop's long watch and no longer holds this answer
                # back, as it did not when it was dropped at that age: a
                # panic button that cannot say "flat" for ten minutes
                # after one lost answer is no panic button. If it turns
                # up it is cancelled or SAID, see _resolve_unknown.)
                todo = [(c, dict(u)) for c, u in self._unknown.items()
                        if not u.get("watch")]
                ask = [(c, u) for c, u in todo if c not in self._coids]
            left, listed, full = [], None, True
            if len(ask) > 1:
                # Several: ONE request for all of them (_find_listed),
                # not one each in every pass of this loop (with twelve
                # that was this call's whole pace, and a minute's worth
                # of requests the backstop then had to do without). One
                # the list does not show is not known: it holds the
                # answer back, as one that is not there does.
                try:
                    listed, full = self._find_listed(ask, **kw)
                except AlpacaError:
                    listed = {}       # (no answer: it decides nothing)
            for coid, u in todo:
                with self._state_lock:
                    known = coid in self._coids
                # Alpaca ANSWERED that it does not hold it: a 404 for its
                # own id, or a list (not a full page) without it.
                absent = False
                if not known:
                    o = None
                    if listed is None:
                        try:
                            o = self.client.call(
                                "GET", "trading",
                                "/v2/orders:by_client_order_id",
                                {"client_order_id": coid}, **kw)
                        except AlpacaError as e:
                            absent = e.status == 404   # (else: no answer)
                    else:
                        o = listed.get(coid)
                        absent = o is None and not full
                    if o is not None:
                        try:
                            self._adopt_order(o, u["kind"], u["cid"])
                            known = True
                        except AlpacaError:
                            pass
                if known:
                    with self._state_lock:
                        self._unknown.pop(coid, None)
                elif absent and t_call - u["t0"] > UNKNOWN_SEC:
                    # Not there, and older than UNKNOWN_SEC when this call
                    # began: on the long watch from here, as the backstop
                    # puts it after a look-up of its own with that answer.
                    # (Only the backstop did, and its look-up waits behind
                    # this call's requests: flatten_all asked again and
                    # again said "not known to be flat" for 75 s over one
                    # order that never came. One that comes of age while
                    # this call works is left to the backstop, as before.
                    # A look-up that got no answer decides nothing, here
                    # as there.)
                    with self._state_lock:
                        real = self._unknown.get(coid)
                        if real is not None and real["t0"] == u["t0"] \
                                and real.get("flat"):
                            real["watch"] = True
                            real["next"] = time.monotonic() + 10.0
                            self._unsaved = True
                        else:
                            left.append((coid, u))
                else:
                    left.append((coid, u))
            return left
        t_first = time.monotonic()        # (the first read is made after)
        _, is_open, rows = beside(
            stand or (lambda: None),
            lambda: self._market_open(kind="flatten"), positions)
        live = held(rows)
        first = set(live)
        # Unknown is not shut: only a share while the exchange is KNOWN
        # to be shut is left alone, with its exits.
        shut = {s for s in live if is_open is False and self._is_share(s)}
        with self._state_lock:
            keep = {r["id"] for s in shut for r in prot.exits(s)}
            busy = set()
            for rec in self._orders.values():
                if not rec["done"]:
                    busy.add(rec["sym"])
                    if rec["id"] not in keep:
                        rec["cancel_req"] = True
                        rec["cancel_at"] = time.monotonic()
            # ... and every symbol with an order of unknown fate, one whose
            # POST is on the wire, or one that turned up after an earlier
            # flatten_all (taken above): such an order may land after the
            # first read, and a close sized from that read would then be
            # for more than is held. These are closed in the loop below,
            # from a read made after the cancel-all. (In tests: SELL 4 of a
            # long 10 answered 504 and carried out 0.5-1.2 s later;
            # flatten's first close, sized from its first read, sold 10
            # more: short 4 for a moment, bought back. That
            # narrows the window to the loop's read and the close sent
            # from it; it does not close it while what Alpaca does with a
            # marked close for more than is held is not known.)
            # A CLOSING order of the symbol (the one `_closing` keeps) is
            # not one of them: flatten's close of that symbol goes out
            # under its id and is answered from it (_post_close), and the
            # order is flatten's to wait for. (Held back to the loop, it
            # was found working there with nothing free, and cancelled as
            # an order the cancel-all had not reached: the test
            # KeptAFewSeconds, tests/test_alpaca_closing_orders.py.)
            closing = {p.get("coid") for p in self._closing.values()}
            busy |= odd | {u.get("sym") for store in (self._unknown,
                                                      self._wire)
                           for c, u in store.items() if c not in closing}
        placed, sent, stuck, swept, last = [], {}, {}, set(), {}

        def close(sym, row, read_at, early=False):
            """One market order, marked as a close, for what Alpaca said
            was free of `sym` in `row` (read at `read_at`)."""
            avail = abs(_dec(row.get("qty_available")))
            try:
                # Every position gets an order of its own, and they go
                # out as fast as the trading host's pace lets them: each
                # waits for its place there (it is not refused and asked
                # again), until the moment after which it could not be
                # answered inside this call's time. A stalled POST is
                # not flatten's whole time (its request ends a moment
                # before this call's work does; one begun in the last
                # second still has a second, as it always had), and
                # what is sent again goes under the same id (see
                # _post_close).
                now = time.monotonic()
                o, replayed, _ = self._post_close(
                    sym, row, avail, "flatten", None, "flatten",
                    timeout=kw["timeout"], read_at=read_at,
                    max_wait=max(0.2, t_end - 1.3 - now),
                    until=max(t_end - 0.3, now + 1.0), who=who)
            except _NothingToClose as e:
                # Alpaca found nothing left of it to close: it went
                # while this order was on its way (a stop, another
                # close, a close of this adapter's that landed late).
                # Not a refusal to give up on and nothing to send again:
                # the loop below reads the account again.
                with mine:
                    last[sym] = "Alpaca found nothing left of it to close " \
                        "a moment ago (%s)" % e
                    sent[sym] = time.monotonic()
                return
            except AdapterError as e:
                # No answer, a 5xx, a 429: tried again until the time is
                # up. A refusal is final for this call (but not one given
                # while the orders were still being cancelled).
                with mine:
                    if e.retryable or early:
                        last[sym] = str(e)
                    else:
                        stuck[sym] = str(e)
                return
            with self._state_lock:
                rec = self._track(o)
                rec["by_flatten"] = True      # (never a "late" order)
            with mine:
                placed.append(rec)
                if not (replayed and o.get("status") in DONE):
                    # (an earlier close that has ENDED is no order on its
                    # way: what it left is closed at the next read)
                    sent[sym] = time.monotonic()

        def cancel_all():
            if keep:
                for o in self._scan_orders(None, kind="flatten",
                                           max_wait=1.5):
                    if str(o.get("id")) not in keep:
                        cancel(str(o.get("id")))
            else:
                # The one request of this call that is tied to nothing:
                # it cancels whatever rests when it ARRIVES. It stays (it
                # is one request, and this is the panic button); one that
                # ends without an answer is remembered, see _sweep_unsaid.
                call("DELETE", "trading", "/v2/orders",
                     lost=self._sweep_unsaid)

        try:
            todo = [s for s in live if s not in shut]
            # The orders are cancelled FIRST and alone: a closing order
            # sent beside the cancel-all is cancelled by it (seen on the
            # stand-in: two of three). Then every position that nothing
            # held when this began is closed at once, side by side, from
            # that first read; what an exit held is closed below, when
            # Alpaca has let go of it.
            #
            # However many positions there are, each is closed by an
            # order of its OWN, marked as a close, through _post_close.
            # (More than four used to go through Alpaca's close-all,
            # DELETE /v2/positions: one request, orders under ids Alpaca
            # makes, tied to nothing here. In tests: that request 4 to 5 s
            # late, and the positions closed one by one meanwhile were
            # sold twice, four of six left short; answered 504 and carried
            # out, eleven sells for six positions; 12 s late, it sold a
            # position opened AFTER flatten_all had answered. It is not
            # used any more. The price is time: a request a position,
            # three a second.)
            cancel_all()
            cancelled = time.monotonic()
            free = [] if not clear else [
                s for s in todo if s not in busy
                and abs(_dec(live[s].get("qty_available")))
                >= abs(_dec(live[s].get("qty")))]
            if free:
                beside(*[(lambda s=s: close(s, live[s], t_first, True))
                         for s in free])
                time.sleep(0.3)
            calm = True
            while True:
                began, landed = time.monotonic(), []
                # Was an order request on its way when this pass's reads
                # BEGAN? (noted before the first of them leaves)
                free0, sends0 = quiet()
                # The open orders are read FIRST and the positions after
                # them, in every pass: an order that is not open in the
                # first read has ended, and what it filled is in the
                # second. (They used to go out side by side unless an
                # order of unknown fate had turned up: a fill between
                # the two answers, of an order this session KNEW, a
                # resting one whose cancel Alpaca had not carried out
                # yet, was then in neither, and the account was judged
                # flat with that position open. The price is one round
                # trip a pass where the link is slower than the pace.)
                rest = resting()
                rows = positions()
                live = held(rows)
                first |= set(live)    # (one that opened while this ran)
                pending = [s for s in live
                           if s not in stuck and s not in shut]
                if not pending:
                    if live:
                        break         # only what cannot be closed is left
                    # Flat. An order that is still being cancelled can
                    # still fill, and so can one a call that was told to
                    # stop had already sent: the answer waits until
                    # Alpaca lists none, that call has come back, AND no
                    # order it sent is of unknown fate (a call that gave
                    # up on its request is "back"; its order is not).
                    waiting = unknown()
                    own = {r["id"] for r in placed}
                    for o in rest:
                        # one that turned up after the cancel-all
                        oid = str(o.get("id"))
                        if oid not in keep and oid not in own \
                                and oid not in again:
                            again.add(oid)
                            cancel(oid)
                    # ... AND none of unknown fate was first SEEN at
                    # Alpaca since this pass's reads began, by this
                    # call's look-up just now, the backstop's or the
                    # stream: such an order is "known" from that instant,
                    # and the reads above were made before it. They say
                    # nothing about it; the account is read again. (In tests:
                    # an order.place answered 504 landed between a pass's
                    # reads and its look-up, the look-up found it, and
                    # flatten_all answered {"closed": 1} with its ten
                    # shares held: 26 of 96 runs.)
                    with self._state_lock:
                        landed = [a for a in self._arrived if a[0] >= began]
                    if landed:
                        if time.monotonic() > t_end - 0.4:
                            break
                        continue
                    # ... AND no order request was on its way to Alpaca
                    # when this pass's reads began, and none began since:
                    # what such a request makes may land after the reads,
                    # and they say nothing about it. The account is read
                    # again (at once if it has been answered meanwhile).
                    # (In tests: an order.place whose POST took 3 s was
                    # answered while a pass's positions read, made before
                    # the order, was on its way back; the lane was free
                    # when flatten looked, and flatten_all answered
                    # {"closed": 1} with ten shares held. A slow POST
                    # needs no 5xx and is never "unknown".)
                    free1, sends1 = quiet()
                    calm = free0 and sends1 == sends0
                    if not calm:
                        if time.monotonic() > t_end - 0.4:
                            break
                        if not free1:
                            time.sleep(0.3)
                        continue
                    if not rest and not waiting \
                            and self._orders_free.is_set():
                        break
                    if time.monotonic() > t_end - 0.4:
                        break
                    time.sleep(0.3)
                    continue
                jobs = []
                for sym in pending:
                    if time.monotonic() > t_end - 0.8:
                        break         # the deadline is looked at per order
                    row, now = live[sym], time.monotonic()
                    avail = abs(_dec(row.get("qty_available")))
                    age = now - sent.get(sym, -9.0)
                    if avail <= 0:
                        # Held: by this call's own order still working, or
                        # by an order the cancel-all did not reach (a leg
                        # of an entry that has filled; a stop that was on
                        # its way). Look at the symbol itself, once.
                        if (age > 2.0 and now - cancelled > 0.6
                                and sym not in swept):
                            swept.add(sym)
                            own = {r["id"] for r in placed}
                            for o in self._scan_orders(
                                    sym, kind="flatten", max_wait=1.0):
                                oid = str(o.get("id"))
                                if oid not in keep and oid not in own:
                                    cancel(oid)
                        continue
                    if age < 2.0:
                        continue      # its order is on its way
                    jobs.append(lambda s=sym, r=row: close(s, r, began))
                if jobs:
                    beside(*jobs)
                if time.monotonic() > t_end - 0.8:
                    break
                time.sleep(0.3)
            if not self._stream_up.is_set():
                # Nobody is listening for the closing fills: read them
                # now, so each close is reported before this call answers.
                try:
                    self._sync(kind="flatten")
                except AlpacaError as e:
                    log("fills after flatten not read yet: %s" % e.message)
            closed = len([s for s in first if s not in live])
            if live:
                self._flatten_leave([r for r in placed if r["sym"] in live],
                                    keep, cancel, kw)
                left = ", ".join("%s (%s)" % (s, (
                    "the exchange is shut; its stop and target were kept"
                    if s in shut else stuck.get(s) or last.get(s)
                    or ("its closing order had not filled in time and was "
                        "cancelled" if s in sent else "there was no time "
                        "left to send its closing order")))
                    for s in sorted(live))
                only_shut = all(s in shut for s in live)
                raise AdapterError(
                    "market_closed" if only_shut else "internal",
                    "not flat: closed %d of %d positions; still open: %s. "
                    "Resting orders were cancelled; stops and targets of "
                    "what is still open are being put back."
                    % (closed, len(first), left), retryable=not only_shut)
            if landed:
                # The time ran out before the account could be read again:
                # what was read does not show these orders. They are
                # handed on as any order that turns up after a flatten_all
                # is (_late: cancelled if it rests, SAID if it has
                # filled), and the answer is not "flat".
                with self._state_lock:
                    for _, coid, u in landed:
                        self._late_due.setdefault(coid, dict(u, flat=True))
                self._wake.set()
                raise AdapterError(
                    "internal", "not known to be flat: %s, sent BEFORE "
                    "flatten_all and not answered then, arrived at Alpaca "
                    "while this call was ending, and there was no time "
                    "left to read the account again after it. If it rests "
                    "it is cancelled; if it has FILLED, session.state "
                    "names it and this adapter places no order against "
                    "it. Ask again."
                    % ", ".join(self._said(c, u) for _, c, u in landed[:3]),
                    retryable=True)
            if rest:
                raise AdapterError(
                    "internal", "every position is closed, but %d "
                    "order(s) are still being cancelled at Alpaca; ask "
                    "again" % len(rest), retryable=True)
            if waiting:
                raise AdapterError(
                    "internal", "not known to be flat: every position is "
                    "closed and no order rests, but %s sent BEFORE "
                    "flatten_all %s not been answered by Alpaca and may "
                    "still arrive there. %s watched: if it arrives and "
                    "rests it is cancelled; if it arrives and FILLS, this "
                    "adapter places NO order against it: session.state "
                    "names it and that position is yours to close. Ask "
                    "again."
                    % (", ".join(self._said(c, u) for c, u in waiting[:3]),
                       "has" if len(waiting) == 1 else "have",
                       "It is" if len(waiting) == 1 else "They are"),
                    retryable=True)
            if not self._orders_free.is_set():
                raise AdapterError(
                    "internal", "every position is closed and no order "
                    "rests, but a call that was being answered when "
                    "flatten_all was asked for has not come back from "
                    "Alpaca yet; ask again", retryable=True)
            if not calm:
                with self._state_lock:
                    still = [self._said(c, w) for c, w in self._wire.items()]
                raise AdapterError(
                    "internal", "not known to be flat: every position is "
                    "closed and no order rests as Alpaca last listed them, "
                    "but an order %swas on its way to Alpaca when that read "
                    "began, %s, and there was no time left to read the "
                    "account again. Ask again." % (
                        "(%s) " % ", ".join(still[:3]) if still else "",
                        "and it has not been answered yet" if still
                        else "and it was answered after the read"),
                    retryable=True)
            return {"closed": closed}
        finally:
            # Every symbol this call touched is looked at by the owner of
            # the stops: gone positions lose their levels, open ones get
            # their exits back.
            for sym in first | set(live):
                prot.touch(sym, 0.3)      # dropped there if nothing to keep

    def _flatten_leave(self, placed: list, keep: set, cancel, kw: dict):
        """flatten_all is giving up: nothing it started stays working
        behind the platform's back. Best effort inside the little
        time that is left; the audit catches what this misses."""
        with self._state_lock:
            still = [r for r in placed if not r["done"]]
            for r in still:
                r["cancel_req"], r["cancel_at"] = True, time.monotonic()
        try:
            if still and (keep or len(still) <= 3):
                for r in still:
                    try:
                        self.client.call("DELETE", "trading", "/v2/orders/"
                                         + r["id"], **kw)
                    except AlpacaError as e:
                        if e.status not in (404, 422):
                            raise
            elif still:
                try:
                    self.client.call("DELETE", "trading", "/v2/orders", **kw)
                except AlpacaError as e:
                    self._sweep_unsaid(e)
                    raise
        except AlpacaError as e:
            log("flatten could not cancel its own closing orders: %s"
                % e.message)

    def _sweep_unsaid(self, e: AlpacaError):
        """A cancel-all of flatten_all's (`DELETE /v2/orders`) ended in
        `e`. That request is tied to nothing: whenever Alpaca carries it
        out, it cancels every order that rests THEN. Answered, it is
        done and over. Ended WITHOUT an answer that says so (none at
        all, a 5xx, a 408: the test _post_once uses for an order), it
        may still be carried out, seconds later: after flatten_all has
        answered, when the user has opened a position with a stop and a
        target again, or put new orders to rest (in tests: nine seconds late,
        the stop and target of a position opened after the flatten were
        cancelled and "removed outside the terminal ... NOT put back" was
        said of them; on a live account the user's later resting orders
        went).

        The moment is remembered for LATE_WATCH, in the state file too.
        While it is, a stop or target of ours that is found cancelled
        without this adapter having asked is PUT BACK and that is said
        (_Protection._judge), and an order of the user's that goes the
        same way is named, with why, and NOT placed again: that would be
        an order of this adapter's own (_swept_order). A request this
        adapter refused itself (its own pace, the handler's time) was
        never sent and is not remembered; neither is one Alpaca
        answered, whatever the answer."""
        if e.code in ("deadline", "flatten", "budget"):
            return
        if not (e.status == 0 or e.status >= 500 or e.status == 408):
            return
        at = self.now_ms()
        with self._state_lock:
            self._swept.append((at, time.monotonic()))
            del self._swept[:-16]
        log("flatten_all's cancel-all (DELETE /v2/orders) of %s ended "
            "without an answer (%s): if Alpaca carries it out late it "
            "cancels what rests then; watched for %s"
            % (iso_ms(at), e.message or "HTTP %d" % e.status,
               self._span(LATE_WATCH)))
        self._save_state()

    def _swept_at(self, before: float | None = None):
        """The time (epoch ms) of the newest cancel-all of flatten_all's
        that ended without an answer less than LATE_WATCH ago (and was
        sent before the monotonic moment `before`, when given); None
        when there is none: then nothing here applies."""
        now = self.now_ms()
        with self._state_lock:
            self._swept = [s for s in self._swept
                           if 0 <= now - s[0] <= LATE_WATCH * 1000]
            for at, mono in reversed(self._swept):
                if before is None or mono <= before:
                    return at
        return None

    def _swept_order(self, rec: dict, o: dict) -> str:
        """An order that is not an exit of ours (the platform's, or
        anybody's on this account) has ended cancelled and this adapter
        had not asked for that. While a cancel-all of flatten_all's is
        remembered as unanswered (_sweep_unsaid), that is the probable
        cause, and it is SAID: `session.state` names the order (symbol,
        side, quantity, client id) and why it probably went. It is NOT
        placed again: only the user can say whether it is still wanted,
        and an order sent here would be one nobody asked for. One
        statement a cancel-all, naming every order it took; it goes by
        itself after NOTICE_SEC, as everything said once does. Returns
        the cause in words for the order's own event, "" when no such
        request is remembered. State lock held."""
        at = self._swept_at()
        if at is None:
            return ""
        said = self._swept_orders.setdefault(at, [])
        for old in [k for k in self._swept_orders if k != at
                    and "swept:%d" % k not in self.prot.issues]:
            del self._swept_orders[old]
        said.append("%s %s %s %s (client id %s, order_id %s)" % (
            o.get("type") or "order", o.get("side") or "",
            _fixed(o.get("qty")) if o.get("qty") not in (None, "")
            else "", rec["sym"], rec["cid"] or rec["coid"] or "none",
            rec["root"]))
        why = "probably by flatten_all's cancel-all of %s, which Alpaca " \
            "did not answer then and carried out late" % iso_ms(at)
        self.prot._notice(
            "swept:%d" % at, "%d order(s) of yours ended CANCELLED at "
            "Alpaca and this adapter had not asked for that: %s%s; %s. "
            "NOT placed again by this adapter (an order of its own is "
            "one nobody asked for): place %s again if %s still wanted"
            % (len(said), "; ".join(said[:6]),
               " and %d more" % (len(said) - 6) if len(said) > 6 else "",
               why, "it" if len(said) == 1 else "them",
               "it is" if len(said) == 1 else "they are"))
        return why

    # == history =========================================================
    def handle_history_fills(self, p):
        """Alpaca's fills ledger merged with this session's own fills.

        Bounded here, since the request may carry no range: from `from`
        (default seven days back) to `to`, the newest 500 at most. Rows
        this adapter reported itself carry what it reported (a crypto buy
        NET of its fee, with the fee); rows only Alpaca's ledger knows
        carry the executed quantity and no fee, because Alpaca's ledger
        has none.
        """
        p = p or {}
        now = self.now_ms()
        t_from = int(_num(p.get("from"), now - HISTORY_DAYS * 86_400_000))
        t_to = int(_num(p.get("to"), now + 60_000))
        q = {"after": iso_ms(max(t_from - 1, 0)), "until": iso_ms(t_to + 1),
             "direction": "desc", "page_size": 100}
        rows = []
        for _ in range(HISTORY_MAX_FILLS // 100):
            page = self._call("GET", "trading",
                              "/v2/account/activities/FILL", q) or []
            rows += page
            if len(page) < 100:
                break
            q["page_token"] = page[-1].get("id")
        out = {}
        with self._state_lock:
            mine = {f["fill_id"]: f for f in self._fills}
            for a in rows:
                xid = str(a.get("id") or "").split("::")[-1]
                t = parse_ts(a.get("transaction_time")) or 0
                if xid in mine or not xid:
                    continue
                oid = str(a.get("order_id") or "")
                rec = self._orders.get(oid)
                sym = self._sym(a.get("symbol"))
                out[xid] = {
                    "fill_id": xid, "order_id": rec["root"] if rec else oid,
                    "position_id": sym,
                    "client_id": rec["cid"] if rec else None, "symbol": sym,
                    "side": "sell" if str(a.get("side")).startswith("sell")
                    else "buy", "qty": _num(a.get("qty")),
                    "price": _num(a.get("price")), "fees": 0.0, "time": t}
            for xid, f in mine.items():
                if t_from <= f["time"] <= t_to:
                    out[xid] = dict(f)
        fills = sorted(out.values(), key=lambda f: (f["time"], f["fill_id"]))
        return {"fills": fills[-HISTORY_MAX_FILLS:]}


    # == market data: quotes, by polling =================================
    def _share_feed(self) -> str:
        """`iex` or `sip` for share prices.

        ALPACA_FEED decides. `auto` asks for one SIP quote: a key without
        the paid subscription is told 403 "subscription does not permit
        querying recent SIP data", and IEX it is from then on (remembered
        for a day, so an upgrade is noticed).
        """
        want = self.cfg["feed"]
        if want in ("iex", "sip"):
            return want
        if self._feed and time.time() - self._feed_at < 86400:
            return self._feed
        try:
            self.client.call("GET", "data", "/v2/stocks/quotes/latest",
                             {"symbols": "SPY", "feed": "sip"}, timeout=4.0)
            feed = "sip"
        except AlpacaError as e:
            if e.status != 403 and "subscription" not in e.message.lower():
                return self._feed or "iex"   # could not ask: do not remember
            feed = "iex"
        self._feed, self._feed_at = feed, time.time()
        self._save_state()
        return feed

    def handle_quotes_subscribe(self, p):
        symbols = list(dict.fromkeys(
            str(s) for s in (p or {}).get("symbols") or []))
        for s in symbols:
            self._instrument(s)
        now, cached = time.monotonic(), []
        with self._state_lock:
            for s in symbols:
                if s not in self._quote_subs:
                    if len(self._quote_subs) >= MAX_QUOTE_SYMBOLS:
                        self._quote_refused.add(s)
                        continue
                    self._quote_subs.add(s)
                q = self._quotes.get(s)
                if q and now - q[3] < QUOTE_POLL_SEC:
                    cached.append((s, q))
                else:
                    self._quote_prime.add(s)
            refused = sorted(self._quote_refused)
        # The latest quote at once, also for a symbol already subscribed:
        # a platform that asks again is pricing something now. A fresh one
        # is sent from here; otherwise the poller is woken to fetch it
        # (no price request on the read thread).
        for s, q in cached:
            self.emit("quote", {"symbol": s, "bid": q[0], "ask": q[1],
                                "time": q[2]})
        self._quote_cap(refused)
        self._quote_wake.set()
        return {}

    def handle_quotes_unsubscribe(self, p):
        with self._state_lock:
            for s in (p or {}).get("symbols") or []:
                self._quote_subs.discard(str(s))
                self._quote_refused.discard(str(s))
                self._quote_prime.discard(str(s))
            refused = sorted(self._quote_refused)
        self._quote_cap(refused)
        return {}

    def _quote_cap(self, refused: list):
        """More symbols asked for than the request budget can price: take
        what fits and SAY what was left out (SPEC 3), never leave prices
        that silently do not arrive."""
        if refused:
            self._set_session(
                "quote_cap", "degraded", "%d symbols are over the cap of "
                "%d priced at once and get no quotes: %s"
                % (len(refused), MAX_QUOTE_SYMBOLS,
                   ", ".join(refused[:8]) + ("..." if len(refused) > 8
                                             else "")))
        elif "quote_cap" in self._components:
            self._set_session("quote_cap", "connected", "")

    def _fetch_quotes(self, symbols: list) -> dict:
        """symbol -> (bid, ask, time) for those with a two-sided quote.

        A quote with no bid or no ask is not a market (before the open
        IEX shows an ask of 0) and is not turned into a price.
        """
        crypto = [s for s in symbols
                  if self._is_crypto(self._catalog.get(s))]
        shares = [s for s in symbols if s not in crypto]
        kw = {"kind": "quotes", "timeout": 6.0, "max_wait": 3.0}
        out = {}
        for path, syms, extra in (
                ("/v1beta3/crypto/us/latest/quotes", crypto, {}),
                ("/v2/stocks/quotes/latest", shares, None)):
            if not syms:
                continue
            if extra is None:
                extra = {"feed": self._share_feed()}
            raw = self.client.call("GET", "data", path,
                                   dict(extra, symbols=",".join(syms)), **kw)
            for sym, row in ((raw or {}).get("quotes") or {}).items():
                bid, ask = _num(row.get("bp")), _num(row.get("ap"))
                if bid > 0 and ask > 0:
                    out[sym] = (bid, ask,
                                parse_ts(row.get("t")) or self.now_ms())
        return out

    def _quote_step(self) -> float:
        """One turn of the quote poller; returns how long to wait."""
        now = time.monotonic()
        with self._state_lock:
            subs = set(self._quote_subs)
            prime = self._quote_prime & subs
            self._quote_prime.clear()
        if not subs:
            return 60.0
        due = now >= self._quote_next
        want = subs if due else prime
        if not want:
            return max(0.05, self._quote_next - now)
        if due:
            # Set before the request, so a slow answer cannot shorten the
            # period and spend the minute's budget.
            self._quote_next = now + QUOTE_POLL_SEC
        try:
            got = self._fetch_quotes(sorted(want))
        except AlpacaError as e:
            with self._state_lock:
                self._quote_prime |= prime
            wait = max(QUOTE_POLL_SEC, min(e.retry_after or 0.0, 60.0))
            if (e.status == 429 and wait <= QUOTE_POLL_SEC
                    and time.monotonic() - self._quote_ok < 10.0):
                # Handlers had every request of the last seconds: quotes
                # pause for a moment. Not an outage until it lasts.
                self._quote_next = time.monotonic() + 0.5
                return 0.5
            self._set_session("quotes", "degraded", "quotes unavailable "
                              "(%s); retrying in %.0fs" % (e.message, wait))
            self._quote_next = time.monotonic() + wait
            return wait
        self._set_session("quotes", "connected", "")
        mono = self._quote_ok = time.monotonic()
        for sym, (bid, ask, t) in got.items():
            with self._state_lock:
                if sym not in self._quote_subs:
                    continue      # unsubscribed while the request was out
                self._quotes[sym] = (bid, ask, t, mono)
                # A poll sends a quote only when it is a new one: a symbol
                # nobody is quoting stays silent rather than looking live.
                # A symbol just asked for is always answered.
                if (sym not in prime
                        and self._quote_sent.get(sym) == (bid, ask, t)):
                    continue
                self._quote_sent[sym] = (bid, ask, t)
            self.emit("quote", {"symbol": sym, "bid": bid, "ask": ask,
                                "time": t})
        return max(0.05, self._quote_next - time.monotonic())

    def _quote_loop(self):
        while not self._stop.is_set():
            try:
                delay = self._quote_step()
            except Exception as e:  # noqa: BLE001 - the poller must live
                log("quote poll: %s: %s" % (type(e).__name__, e))
                delay = 5.0
            self._quote_wake.wait(delay)
            self._quote_wake.clear()

    # == market data: bars ===============================================
    def _bar_spec(self, p):
        spec = self._instrument((p or {}).get("symbol"))
        tf = (p or {}).get("timeframe")
        if tf not in TIMEFRAMES:
            raise AdapterError("unsupported", "timeframe %r is not one of "
                               "%s" % (tf, " ".join(TIMEFRAMES)))
        return spec, tf

    def _bar_query(self, spec: dict, tf: str) -> tuple[str, dict]:
        if self._is_crypto(spec):
            return "/v1beta3/crypto/us/bars", {"timeframe": TIMEFRAMES[tf][0]}
        return "/v2/stocks/bars", {"timeframe": TIMEFRAMES[tf][0],
                                   "feed": self._share_feed(),
                                   "adjustment": "raw"}

    @staticmethod
    def _bar_row(b: dict, width: int, now: int):
        """[t, o, h, l, c, vol] for a CLOSED bar, else None. A bar is
        closed when its whole width has passed; Alpaca also returns the
        one still forming."""
        t = parse_ts(b.get("t"))
        if t is None or t + width > now:
            return None
        return [t, _num(b.get("o")), _num(b.get("h")), _num(b.get("l")),
                _num(b.get("c")), _num(b.get("v"))]


    def _bar_store(self, spec: dict, tf: str) -> _BarStore:
        """The store for a symbol and timeframe, made on first use. Kept
        small and young: the least recently used goes when there are too
        many, an unused one after a quarter of an hour, any after an hour
        (a late correction to a bar must not live for ever)."""
        key, mono = (spec["symbol"], tf), time.monotonic()
        with self._bars_lock:
            for k in [k for k, v in self._bars.items()
                      if mono - v.used > BARS_IDLE
                      or mono - v.made > BARS_LIFE]:
                self._bars.pop(k).dead = True
            store = self._bars.get(key)
            if store is None:
                hours = None if tf == "1d" else BAR_WINDOW_H[
                    (self._is_crypto(spec), tf == "1m")]
                store = self._bars[key] = _BarStore(
                    self.now_ms() // H4_MS * H4_MS,
                    hours * 3_600_000 if hours else None)
                while len(self._bars) > BARS_STORES:
                    self._bars.popitem(last=False)[1].dead = True
            else:
                self._bars.move_to_end(key)
            store.used = mono
            return store

    def _bar_fetch(self, spec: dict, tf: str, start_ms: int, end_ms,
                   kind: str, max_wait: float = 8.0, order: int = 0,
                   live=None) -> list:
        """The closed bars with start <= t <= end, however many pages
        Alpaca cuts them into."""
        sym, width = spec["symbol"], TIMEFRAMES[tf][1]
        path, q = self._bar_query(spec, tf)
        q.update(symbols=sym, sort="desc", limit=10000,
                 start=iso_ms(start_ms))
        if end_ms is not None:
            q["end"] = iso_ms(end_ms)
        rows = []
        for _ in range(8):
            raw = self.client.call("GET", "data", path, q, kind=kind,
                                   timeout=10.0, max_wait=max_wait,
                                   order=order, live=live) or {}
            now = self.now_ms()
            for b in (raw.get("bars") or {}).get(sym) or []:
                row = self._bar_row(b, width, now)
                if row:
                    rows.append(row)
            if not raw.get("next_page_token"):
                break
            q["page_token"] = raw["next_page_token"]
        return rows


    def _bar_head(self, store: _BarStore, spec: dict, tf: str):
        """Read the newest end again, on a thread of its own: from the
        last bar held to now. Tried twice; the outcome is left in the
        store for whoever is waiting."""
        err = None
        for _ in (0, 1):
            with store.cond:
                if store.window is None:
                    start = max(store.rows, default=BARS_FIRST_MS)
                elif store.head_at:
                    start = max((t for t in store.rows
                                 if t >= store.anchor), default=store.anchor)
                else:
                    start = store.anchor - 3_600_000 * BAR_HEAD_H[
                        self._is_crypto(spec)]
            try:
                # The newest end is small, and without it there is no
                # answer: it goes before any poll (see _Budget._standing).
                rows = self._bar_fetch(spec, tf, start, None, "head", 5.0)
            except AlpacaError as e:
                err = e
                continue
            except Exception as e:  # noqa: BLE001 - a thread must not die
                err = AlpacaError(0, "%s: %s" % (type(e).__name__, e))
                continue
            with store.cond:
                for row in rows:
                    store.rows[row[0]] = row
                if store.window is not None:
                    store.head_floor = min(store.head_floor, start)
                store.head_at, err = time.monotonic(), None
            break
        with store.cond:
            store.head_busy, store.head_error = False, err
            store.cond.notify_all()

    def _bar_worker(self, store: _BarStore, spec: dict, tf: str):
        """Read windows into a store until it holds what was asked for.

        While the caller waits, several of these run at once. After its
        budget ends ONE goes on quietly, as a background reader, for a
        bounded number of windows, so the next call finds more.
        """
        while True:
            with store.cond:
                fast = time.monotonic() < store.fast_until
                k = None
                if (not store.dead and store.have() < store.want
                        and (fast or (store.extra > 0
                                      and not store.quiet_busy))):
                    k = store.take()
                if k is None:
                    store.workers -= 1
                    store.cond.notify_all()
                    return
                if not fast:
                    store.extra -= 1
                    store.quiet_busy = True
            end = store.anchor - k * store.window
            rows, err = None, None
            quiet = not fast and self._bars_quiet.acquire(timeout=60.0)
            try:
                if fast:
                    # At speed; behind every chart somebody is still
                    # waiting for, once this one's caller has its answer.
                    rows = self._bar_fetch(
                        spec, tf, end - store.window, end - 1, "bulk",
                        max(0.2, store.fast_until - time.monotonic()),
                        order=k, live=lambda: store.asking > 0)
                elif quiet:
                    rows = self._bar_fetch(
                        spec, tf, end - store.window, end - 1, "idle", 20.0)
                else:
                    err = AlpacaError(429, "other charts are being read",
                                      code="budget")
            except AlpacaError as e:
                err = e
            except Exception as e:  # noqa: BLE001 - a thread must not die
                err = AlpacaError(0, "%s: %s" % (type(e).__name__, e))
            finally:
                if quiet:
                    self._bars_quiet.release()
            with store.cond:
                if not fast:
                    store.quiet_busy = False
                if err is None:
                    store.fails = 0
                    store.put(k, rows)
                else:
                    store.retry.append(k)     # first thing the next reads
                    if err.code == "budget":
                        # Our own request budget, not Alpaca failing: this
                        # reader stops; the next call starts it again.
                        store.throttled = err
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
        """The newest `count` closed bars, oldest first.

        Answers as soon as it has what was asked. Failing that, with what
        is in hand BARS_FIRST seconds after the request was READ: the
        newest end and the windows that have arrived by then (measured on
        paper, 2026-11-10: the newest end alone costs 0.25 to 0.65 s for a
        share and 0.3 to 1.8 s for a crypto pair; six seconds of the old
        chart under a new instrument is a long time). And NEVER later than
        BARS_BUDGET, which is how long it waits for the newest end itself.
        It runs on a thread of its own (see _dispatch) and holds up no
        other request.

        Fewer bars than asked is a true answer. The reading goes on behind
        it, at speed until the request's BARS_BUDGET is up and quietly
        after that, so asking again returns more. It is an error only
        when there is not one bar to give, and then a retryable one.
        """
        spec, tf = self._bar_spec(p)
        count = (p or {}).get("count")
        if (isinstance(count, bool) or not isinstance(count, int)
                or not 1 <= count <= BARS_MAX_COUNT):
            raise AdapterError("unsupported", "count must be a whole number "
                               "from 1 to %d" % BARS_MAX_COUNT)
        sym, t0 = spec["symbol"], time.monotonic()
        # From when the request was READ off the pipe, not from now: the
        # platform's ten seconds started when it sent the line, and
        # whatever this request waited before getting here is gone.
        read = self.client.read_at() or t0
        deadline = read + BARS_BUDGET
        first = min(deadline, read + BARS_FIRST)
        try:
            self._bar_query(spec, tf)          # settles the share feed once
        except AlpacaError as e:
            raise self._err(e) from None
        store = self._bar_store(spec, tf)
        me, handed, answered = object(), False, threading.Event()
        try:
            with store.cond:
                store.asking += 1
                store.waiters[me] = (count, deadline, False)
                store.want = max(w[0] for w in store.waiters.values())
                store.error, store.throttled, store.fails = None, None, 0
                stale = t0 - store.head_at > BARS_HEAD_TTL
                if stale and not store.head_busy:
                    store.head_busy, store.head_error = True, None
                    threading.Thread(target=self._bar_head, daemon=True,
                                     args=(store, spec, tf),
                                     name="alpaca-bars-head").start()
                # The newest end goes FIRST and alone for a moment: Alpaca
                # answers slower the more it is asked at once (seen: the
                # small newest-end request took over 6 s behind six
                # windows), and without the newest end there is nothing to
                # hand out. EVERY request gets this read at once, whatever
                # else is loading: it is one request, and it is what makes
                # the answer a chart.
                # Further back is read in windows, behind this answer if
                # need be: see _bars_load, which then owns the entry in
                # `waiters` (what is wanted stays wanted while it reads).
                # A request that is plainly for more than the newest end
                # brings (about `est` bars) starts on them AT ONCE, a
                # couple beside the newest end: waiting for it first left
                # a three-second answer with one window (seen on paper).
                def load():
                    nonlocal handed
                    handed = True
                    store.loaders += 1
                    threading.Thread(
                        target=self._bars_load, daemon=True,
                        args=(store, spec, tf, count, deadline, me,
                              answered), name="alpaca-bars-load").start()
                est = 0.0
                if stale and store.window is not None:
                    est = BAR_HEAD_H[self._is_crypto(spec)] * 3_600_000 / (
                        TIMEFRAMES[tf][1]) * (
                        1.0 if self._is_crypto(spec) else 0.19)
                if count > store.have() + est and (
                        store.retry or not store.finished()):
                    load()
                lead = min(time.monotonic() + 1.5, deadline)
                while stale and store.head_busy and time.monotonic() < lead:
                    store.cond.wait(0.05)
                if not handed and count > store.have() and (
                        store.retry or not store.finished()):
                    load()
            with store.cond:
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    # Older bars with the newest hours missing would be a
                    # chart that stops in the past without saying so:
                    # nothing is handed out until the newest end has been
                    # read at least once.
                    if not (store.head_busy and stale):
                        coming = store.loaders > 0 or store.workers > 0
                        if store.have() >= count or not coming:
                            break
                        if now >= first and store.head_at:
                            break     # the first answer: what is in hand
                    store.cond.wait(min(0.25, max(0.01, (
                        first if now < first else deadline) - now)))
                rows = store.newest(count) if store.head_at else []
                failed = store.head_error or store.error or store.throttled
                loading = (store.workers > 0 or store.head_busy
                           or store.loaders > 0)
        finally:
            answered.set()
            with store.cond:
                store.asking -= 1
                if not handed:
                    store.waiters.pop(me, None)
                    if store.waiters:
                        store.want = max(
                            w[0] for w in store.waiters.values())
        if rows or not (failed or loading):
            return {"bars": rows}
        if failed and failed.status in (401, 403):
            raise self._err(failed)
        if failed and failed.code == "budget":
            raise AdapterError(
                "rate_limited", "Alpaca allows 200 data requests a minute "
                "and this minute's are used up, so there are no %s bars "
                "for %s yet. Ask again in a moment." % (tf, sym),
                retryable=True)
        raise AdapterError(
            "rate_limited" if failed and failed.status == 429
            else "internal", "Alpaca has not sent any %s bars for %s yet "
            "(%s). Ask again in a moment." % (
                tf, sym, failed.message if failed and failed.message
                else "it is still answering"), retryable=True)

    def _bars_load(self, store: _BarStore, spec: dict, tf: str, count: int,
                   deadline: float, me, answered: threading.Event):
        """Arrange the reading of windows for ONE request, behind its
        answer if need be, until that request's time (`deadline`) is up.

        Windows are read several at once, and that is BOUNDED: at most
        BARS_LOADS charts do it at a time (windows spread thin over many
        charts give each a gap, not bars; and they are what fills the
        data host's pace). A chart over the bound waits for its turn
        inside its own time. If none comes, ONE quiet reader goes on for
        it, so that asking again finds more. A chart that has its turn
        keeps it while its windows are read at speed, also after its
        caller has been answered: but then it gives the turn up the
        moment another chart, whose caller is still waiting, wants one.
        """
        turn = counted = full = False
        began = time.monotonic()
        try:
            with self._bars_lock:
                self._bars_waiting += 1
            try:
                turn = self._bars_slots.acquire(timeout=max(
                    0.0, deadline - time.monotonic() - 0.5))
            finally:
                with self._bars_lock:
                    self._bars_waiting -= 1
            with store.cond:
                if turn:
                    store.turns += 1
                while True:
                    now = time.monotonic()
                    missing = count - store.have()
                    if (missing <= 0 or store.dead
                            or not (store.retry or not store.finished())):
                        break
                    if not full:
                        store.extra = BARS_FILL_EXTRA
                        want = 1
                        if turn:
                            store.waiters[me] = (count, deadline, True)
                            store.fast_until = max(store.fast_until,
                                                   deadline)
                            # About how many windows that is: a bar a
                            # minute for crypto, a regular session a day
                            # for shares.
                            per = store.window / TIMEFRAMES[tf][1] * (
                                1.0 if self._is_crypto(spec) else 0.19)
                            want = min(BARS_WORKERS,
                                       int(missing / max(per, 1.0)) + 1)
                            # Beside the newest end only a couple: Alpaca
                            # answers slower the more it is asked at once
                            # (seen: the newest end over 6 s behind six
                            # windows), and the newest end is the answer.
                            if store.head_busy and now - began < 1.5:
                                want = min(want, BARS_EARLY)
                            else:
                                full = True
                        else:
                            full = True
                        for _ in range(max(0, want - store.workers)):
                            store.workers += 1
                            threading.Thread(
                                target=self._bar_worker, daemon=True,
                                args=(store, spec, tf),
                                name="alpaca-bars").start()
                    if not counted:
                        store.loaders -= 1
                        counted = True
                        store.cond.notify_all()
                    if not turn or now >= deadline or (
                            full and store.workers <= 0):
                        break
                    if answered.is_set() and self._bars_waiting > 0:
                        # Its caller has an answer; another chart's is
                        # waiting. The quiet reader goes on for this one.
                        store.turns -= 1
                        if store.turns <= 0:
                            store.fast_until = min(store.fast_until, now)
                        turn = False
                        self._bars_slots.release()
                        break
                    store.cond.wait(0.1)
        finally:
            with store.cond:
                if not counted:
                    store.loaders -= 1
                if turn:
                    store.turns -= 1
                store.waiters.pop(me, None)
                if store.waiters:
                    store.want = max(w[0] for w in store.waiters.values())
                store.cond.notify_all()
            if turn:
                self._bars_slots.release()

    def handle_bars_subscribe(self, p):
        spec, tf = self._bar_spec(p)
        width, now = TIMEFRAMES[tf][1], self.now_ms()
        with self._state_lock:
            # The watermark starts at the bar now forming, so nothing
            # already closed is replayed; that bar is sent when it closes.
            self._bar_subs.setdefault((spec["symbol"], tf), {
                "mark": now // width * width - 1, "tries": 0,
                "next": (now // width + 1) * width + 3000})
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, p):
        spec, tf = self._bar_spec(p)
        with self._state_lock:
            self._bar_subs.pop((spec["symbol"], tf), None)
        return {}

    def _poll_bars(self):
        """Closed bars for every subscription that is due: one request per
        asset class and timeframe, a watermark so no bar repeats."""
        now = self.now_ms()
        with self._state_lock:
            due = [k for k, v in self._bar_subs.items() if now >= v["next"]]
        groups: dict[tuple, list] = {}
        for sym, tf in due:
            spec = self._catalog.get(sym)
            if spec:
                groups.setdefault((self._is_crypto(spec), tf), []).append(
                    spec)
        for (_, tf), specs in groups.items():
            width = TIMEFRAMES[tf][1]
            syms = [s["symbol"] for s in specs]
            with self._state_lock:
                marks = [self._bar_subs[(s, tf)]["mark"] for s in syms
                         if (s, tf) in self._bar_subs]
            if not marks:
                continue
            path, q = self._bar_query(specs[0], tf)
            # Never further back than a few bars: a symbol that prints no
            # bar (halted, or thin on IEX) must not drag the window of
            # the whole group back with it. And every page is read:
            # Alpaca's `limit` counts bars over ALL the symbols, one
            # symbol after the other, so the first page alone starves
            # whichever symbols come last.
            q.update(symbols=",".join(syms), sort="asc", limit=10000,
                     start=iso_ms(max(min(marks) + 1,
                                      now - BAR_SUB_REACH * width)))
            bars: dict[str, list] = {}
            for _ in range(5):
                try:
                    raw = self.client.call("GET", "data", path, q,
                                           kind="background", max_wait=2.0)
                except AlpacaError as e:
                    log("bars %s: %s" % (tf, e.message))
                    break
                for sym, rows in ((raw or {}).get("bars") or {}).items():
                    bars.setdefault(sym, []).extend(rows or [])
                if not (raw or {}).get("next_page_token"):
                    break
                q["page_token"] = raw["next_page_token"]
            for sym in syms:
                fresh = []
                with self._state_lock:
                    sub = self._bar_subs.get((sym, tf))
                    if sub is None:
                        continue
                    for b in bars.get(sym) or []:
                        row = self._bar_row(b, width, now)
                        if row and row[0] > sub["mark"]:
                            fresh.append(row)
                    fresh.sort()
                    edge = (now // width + 1) * width + 3000
                    if fresh:
                        sub["mark"], sub["tries"] = fresh[-1][0], 0
                        sub["next"] = edge
                    else:
                        # Not there yet (or no trade in that bar): look
                        # again soon, then less often, never past the
                        # next boundary.
                        sub["tries"] += 1
                        sub["next"] = min(edge, now + (
                            5000, 15000, 60000, 300000)[
                                min(sub["tries"], 4) - 1])
                for row in fresh:
                    self.emit("bar", {"symbol": sym, "timeframe": tf,
                                      "bar": row})

    # == the order stream ================================================
    def _stream_reply(self, ws: _WebSocket, stream: str) -> dict:
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            text = ws.recv(timeout=10.0)
            if text is None:
                break
            try:
                d = json.loads(text)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("stream") == stream:
                return d.get("data") or {}
        raise WSClosed("no %s answer from Alpaca's stream" % stream)

    def _pump(self, ws: _WebSocket):
        while not self._stop.is_set():
            try:
                text = ws.recv(timeout=STREAM_PING_SEC)
            except WSTimeout:
                # An order stream is silent whenever nobody trades. Ping,
                # so a dead connection is found by us and not by a fill
                # that never arrives.
                if time.monotonic() - ws.last_rx > STREAM_PING_SEC * 2.5:
                    raise WSClosed("no answer to ping") from None
                ws.ping(b"brue")
                continue
            if text is None:
                raise WSClosed("closed by Alpaca (code %s)" % ws.close_code)
            try:
                d = json.loads(text)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("stream") == "trade_updates":
                try:
                    self._on_trade_update(d.get("data") or {})
                except Exception as e:  # noqa: BLE001 - one bad message
                    log("trade update not handled: %s: %s"
                        % (type(e).__name__, e))
                    self._resync = True
                    self._wake.set()
            elif d.get("action") == "error":
                raise WSClosed("Alpaca stream error: %s" % (
                    (d.get("data") or {}).get("error_message"),))

    def _stream_loop(self):
        """Own the `trade_updates` stream for the life of the process:
        connect, authenticate, listen, read; on any failure say so, back
        off and start again. There is no replay, so every (re)connect is
        followed by a read of what was missed."""
        backoff = 1.0
        while not self._stop.is_set():
            ws, up_at, detail = None, None, "not connected"
            try:
                ws = _WebSocket(self.cfg["stream_url"], timeout=10.0,
                                headers={"User-Agent": USER_AGENT})
                # The form Alpaca's own answer asks for; the SDKs' older
                # `authenticate` form is answered "being deprecated".
                ws.send_json({"action": "auth", "key": self.cfg["key"],
                              "secret": self.cfg["secret"]})
                if self._stream_reply(ws, "authorization").get(
                        "status") != "authorized":
                    raise WSClosed("Alpaca refused the key on the stream")
                ws.send_json({"action": "listen",
                              "data": {"streams": ["trade_updates"]}})
                if "trade_updates" not in (self._stream_reply(
                        ws, "listening").get("streams") or []):
                    raise WSClosed("Alpaca did not start trade_updates")
                up_at = time.monotonic()
                self._stream_up.set()
                if self._baselined:
                    try:
                        self._sync()
                    except AlpacaError as e:
                        log("catch-up after connect failed: " + e.message)
                        self._resync = True
                self._set_session("stream", "connected", "")
                self._pump(ws)
            except (OSError, WSTimeout, ValueError) as e:
                detail = self.client.scrub(str(e) or type(e).__name__)
            except Exception as e:  # noqa: BLE001 - this thread must live
                # Anything at all (a broken answer to the catch-up read, a
                # bug): said, and the loop starts again. A stream thread
                # that dies leaves fills to polling for the rest of the
                # process and nobody told.
                detail = "%s: %s" % (type(e).__name__,
                                     self.client.scrub(str(e)))
                self._resync = True
            finally:
                self._stream_up.clear()
                if ws is not None:
                    ws.close()
            if self._stop.is_set():
                break
            log("order stream: " + detail)
            self._set_session("stream", "degraded", "the order stream is "
                              "down (%s); fills are read by polling until "
                              "it is back" % detail)
            self._wake.set()
            if up_at and time.monotonic() - up_at > 30.0:
                backoff = 1.0
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    # == the backstop thread =============================================
    def _poll_once(self):
        now = time.monotonic()
        prot = self.prot
        with self._state_lock:
            flying = [r for r in self._orders.values()
                      if not r["done"] and r["kind"] != "foreign"
                      and r["o"].get("type") == "market"]
            late = [r["cid"] or r["id"] for r in flying
                    if r["kind"] == "user" and now - r["t0"] > 30.0]
            guarded = bool(prot.levels) or any(
                not r["done"] and r["kind"] in ("sl", "tp", "close",
                                                "flatten")
                for r in self._orders.values())
        up = self._stream_up.is_set()
        # (The stops and targets are NOT this thread's work: they have a
        # thread of their own, see _Protection.loop. What this thread
        # reads below is what that one checks them against.)
        if self._baselined and not prot.flattening:
            # Read Alpaca again: at once when something says the picture
            # may be wrong; every 15 s while the stream is down; and as
            # the AUDIT of what Alpaca really holds, every few seconds
            # while any stop or target exists and now and then otherwise.
            full = (self._resync or now - self._last_full >= (
                AUDIT_SEC if guarded else AUDIT_IDLE_SEC)
                or (not up and now - self._last_full >= 15.0))
            if full or (not up and flying and now - self._last_sync >= 1.0):
                self._last_sync = now
                was = self._resync
                try:
                    # With the stream up this is the safety net, and it
                    # steps aside quickly when handlers are busy; with the
                    # stream down it is how fills are heard, and it waits.
                    self._sync(light=not full, max_wait=1.5 if up and not
                               was else 4.0)
                    if full:
                        self._last_full = now
                        if was:
                            self._resync = False
                except AlpacaError as e:
                    if e.code == "budget" and up and not was:
                        # The safety net found no free slot (a chart is
                        # loading, a platform is busy). Nothing is wrong:
                        # the stream is up. It steps back for a few
                        # seconds instead of asking on every pass.
                        self._last_full = now + 5.0 - (
                            AUDIT_SEC if guarded else AUDIT_IDLE_SEC)
                    else:
                        log("backstop read failed: %s" % e.message)
        self._resolve_unknown()
        # SPEC 8.4: an order that has not ended in 30 s is said out loud.
        if late:
            self._set_session(
                "orders", "degraded", "no fill, rejection or cancel from "
                "Alpaca after 30s for: %s" % ", ".join(late[:5]))
        elif "orders" in self._components:
            self._set_session("orders", "connected", "")
        if self._account_dirty and self._baselined:
            self._account_dirty = False
            try:
                view = self._account_view(*self._account_now("background"))
                if view != self._account_sent:
                    self._account_sent = view
                    self.emit("account.updated", view)
            except AlpacaError:
                self._account_dirty = True
            except AdapterError as e:
                # (a row with no open P&L to be had: no figure is sent
                # that is not known; the next change asks again)
                log("account.updated not sent: %s" % e)
            self._save_state()
        if self._unsaved:
            # (the orders of unknown fate and the closing orders as they
            # stand now: see _save_state)
            self._save_state()
        prot.wake.set()       # look at what the reads above have learned
        for key, t in list(self._notices.items()):
            if now - t > NOTICE_SEC:
                self._notices.pop(key, None)
                prot.clear(key)
        self._poll_bars()
        if (self._catalog_ready.is_set()
                and time.time() - self._catalog_built > CATALOG_REFRESH
                and now - self._catalog_try > 900.0):
            self._catalog_try = now
            try:
                self._load_catalog(refresh=True)
            except AlpacaError as e:
                log("catalog refresh failed: %s" % e.message)
        with self._state_lock:
            # (an order on the long watch is asked for every ten seconds,
            # and a late one this has stopped cancelling is only looked
            # at when it ends: neither keeps this loop at its quick step)
            return bool(flying or self._account_dirty or any(
                not u.get("watch") for u in self._unknown.values()) or any(
                not u.get("stopped") for u in self._late_due.values()))

    def _poll_loop(self):
        busy = False
        while not self._stop.is_set():
            delay = 0.5 if busy else 5.0
            with self._state_lock:
                if self._bar_subs:
                    soon = min(v["next"] for v in self._bar_subs.values())
                    delay = min(delay, max(0.2, (soon - self.now_ms())
                                           / 1000.0))
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                busy = self._poll_once()
                if "backstop" in self._components:
                    self._set_session("backstop", "connected", "")
            except Exception as e:  # noqa: BLE001 - the backstop must live
                text = "%s: %s" % (type(e).__name__,
                                   self.client.scrub(str(e)))
                log("backstop: " + text)
                # It carries the missed fills AND the stops: if it cannot
                # run, that is said, not just logged.
                self._set_session(
                    "backstop", "degraded", "the adapter's own upkeep "
                    "(missed fills, stops and targets) failed and is "
                    "being tried again: " + text)
                busy = True


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a
    # pipe buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    adapter = AlpacaAdapter()
    adapter.run()
    # The pipe closed (or `bye`): an order still being carried out on its
    # lane is not abandoned half-way when the process (and its daemon
    # threads) ends. It has its own time, counted from when it was read,
    # and that is how long this waits at most.
    adapter._drain(HANDLER_BUDGET + 1.5)
    if adapter._unsaved:
        adapter._save_state()     # (how that order ended, for the next run)


if __name__ == "__main__":
    main()
