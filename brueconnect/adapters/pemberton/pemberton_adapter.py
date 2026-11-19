#!/usr/bin/env python3
"""Brue Connect adapter for Pemberton and Hale (Electronic Order Service).

EOS is a 2003-era back office with a web server bolted on: every operation is
an HTTP POST of an XML document to /xmlapi/<Operation>, every reply is HTTP
200 whether it worked or not (the truth is a `<Fault>` element in the body),
every decimal on the wire uses a comma, every timestamp is `DD/MM/YYYY
HH:MM:SS` UTC, and the one report that is not XML (History) is a
semicolon-separated CSV download. There is NO push channel of any kind and the
venue says so in as many words, so this adapter owns the polling and turns
polled state into the protocol's events itself.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no account, no login, no poll. The
    account is opened or loaded, the sessions are logged in and the pollers
    start in `on_first_call`, so a platform's connection picker can learn what
    this broker is called without opening a session on it.
  * EOS throttles Quote and OrderStatus to ONE call per 2 seconds, and the
    allowance is PER SESSION, not per account. A single session would price
    the six-instrument universe once every twelve seconds, which is not a
    quote feed. So the client holds a small POOL of logged-in sessions and
    spends their buckets in turn; four sessions is two polls a second, enough
    to refresh the whole universe every ~3 s, and small enough to stay a
    polite guest on a shared facility.
  * TransactionList is the fill detector, not OrderStatus. It is not
    throttled, it is per account rather than per order, and it is the only
    call that reports a deal EOS made without being asked. Every poll
    recomputes the book from the ledger and emits only on a transition, so a
    row seen twice never becomes two fills. OrderStatus is the backstop for
    the states a deal cannot show: WORKING (a limit is resting), CANCELLED,
    EXPIRED, REFUSED.
  * positions are derived, because EOS has no position id and no position
    side. Portfolio reports an unsigned Quantity, so a short Special Future
    and a long one look identical there; the SIGN comes from netting the deal
    ledger, and the position id is minted from the deal that opened it
    (`<code>@<deal>`), which is stable across a restart because it is
    computed from the venue's own history rather than remembered.
  * money is parsed as Decimal and stays Decimal through the cost-basis and
    realised-P&L arithmetic, converting to float only at the wire. The venue
    deals whole shares at two decimals and reconciles to the cent; float
    averaging drifts off it, and a position's average price is the number
    every later P&L figure is measured from.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from xml.etree import ElementTree as ET

# The adapter is spawned by whatever python the user's machine has, from
# whatever working directory the platform happens to be in, so the SDK is
# located relative to this file rather than trusted to be on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

BASE = os.environ.get("PEMBERTON_BASE_URL", "http://127.0.0.1:7651")
# Optional: a Pemberton account the OPERATOR owns. Left unset (the normal
# case) the adapter opens its own demonstration account on first use and keeps
# the credentials in its private state directory. Credentials never appear in
# a protocol message either way (SPEC 8.5).
ENV_ACCOUNT = os.environ.get("PEMBERTON_ACCOUNT") or ""
ENV_PIN = os.environ.get("PEMBERTON_PIN") or ""
HOLDER = os.environ.get("PEMBERTON_HOLDER", "LSE Terminal")

# EOS: "no more than one call to either operation, per session, within any
# 2-second window". Local margin on top, because the venue measures the window
# from ITS clock and a PH-429 costs the whole slot rather than delaying it.
POLL_THROTTLE = 2.0
POLL_MARGIN = 0.25
# Sessions held open concurrently. Each one carries its own Quote/OrderStatus
# allowance, so this number IS the poll bandwidth: 4 sessions = 2 polls/s.
# Deliberately not larger. The guide calls EOS "a shared facility used by many
# back offices at once" and asks for a reasonable cadence; four logins for one
# terminal is a fair reading of that, forty would not be.
POOL_SIZE = 4
# Target refresh for one subscribed symbol. Matches the venue's own window, so
# up to POOL_SIZE symbols refresh at the venue's full rate and a longer list
# simply stretches, rather than starving the tail.
QUOTE_INTERVAL = 2.0

# Ledger cadence. EOS documents no limit outside Quote/OrderStatus, which is
# an invitation to be sensible rather than to spin: idle is one call every
# 1.5 s, and only while an order is actually in flight does it tighten to
# something that feels like a fill notification.
LEDGER_IDLE = 1.5
LEDGER_BUSY = 0.35
ACCOUNT_INTERVAL = 10.0
BAR_INTERVAL = 60.0
# SPEC 8.4: an order that has not reached a terminal state by now is reported
# as a degraded session rather than quietly waited on.
ORDER_DEADLINE = 30.0
# How long an order may sit at PENDING before it is worth spending a throttled
# OrderStatus call on it. A market order in an open market usually deals
# inside one ledger poll, and the answer arrives free that way.
STATUS_AFTER = 1.2

# EOS granularities. HOUR and DAY are the only two that exist; 4h is folded
# from HOUR buckets here because the fold is exact (4 whole hours) and a
# platform asking for it would otherwise get nothing. The minute timeframes
# are NOT synthesised: EOS publishes no intraday data below the hour and
# inventing bars from quotes would be manufacturing history.
TF_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
TF_GRANULARITY = {"1h": "HOUR", "4h": "HOUR", "1d": "DAY"}
# "`From` may not be more than 30 days before today" (guide 5.2).
HISTORY_MAX_DAYS = 30

# Special Futures are the 8002xx range (guide 4.1). The code prefix is the
# venue's own documented symbology, not a hardcoded instrument list: any new
# 8002xx contract is picked up as a future, and the classification is
# cross-checked against the instrument's published trading hours below.
FUTURE_PREFIX = "8002"
# "Each contract references ten shares of the underlying" (guide 4.1).
FUTURE_CONTRACT_SIZE = 10
# "Dealing is margined at 10 percent of consideration" for the futures; the
# cash equities are "covered in full by cleared funds, with no margin lending
# and no leverage", which is a margin rate of 1.
FUTURE_MARGIN_RATE = Decimal("0.10")
EQUITY_MARGIN_RATE = Decimal("1")

SESSIONS_24_7 = [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                  "close": "24:00", "tz": "UTC"}]
_HOURS_RE = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s*UTC", re.I)

D0 = Decimal("0")
D2 = Decimal("0.01")


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[pemberton] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ wire types

def _dec(text, default=D0) -> Decimal:
    """A Pemberton decimal string -> Decimal. Comma is the decimal separator
    in BOTH directions and there is no thousands separator, so the only
    transformation needed is the swap; anything else is a malformed field and
    becomes the default rather than an exception in a poller."""
    if text is None:
        return default
    if isinstance(text, Decimal):
        return text
    try:
        return Decimal(str(text).strip().replace(",", "."))
    except Exception:  # noqa: BLE001 - any unparseable value, same outcome
        return default


def _fmt_dec(value: Decimal, digits: int = 2) -> str:
    q = Decimal(1).scaleb(-digits)
    return str(Decimal(value).quantize(q, rounding=ROUND_HALF_UP)).replace(
        ".", ",")


def _int(text, default=0) -> int:
    try:
        return int(str(text).strip())
    except Exception:  # noqa: BLE001
        return default


def _money(value: Decimal) -> float:
    """Decimal -> the JSON number the protocol carries, rounded to the cent
    the venue itself reconciles to."""
    return float(Decimal(value).quantize(D2, rounding=ROUND_HALF_UP))


def _ms(wire) -> int:
    """`DD/MM/YYYY HH:MM:SS` (or `DD/MM/YYYY`) UTC -> epoch milliseconds.

    EOS uses this format and no other, anywhere, in both directions, and it is
    always UTC even though the guide describes trading hours in Brussels local
    time as well. Returns 0 rather than raising: a missing timestamp must not
    take down a fill.
    """
    text = (wire or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return int(dt.timestamp() * 1000)
    return 0


def _wire_date(ms: float) -> str:
    """Epoch milliseconds -> the `DD/MM/YYYY` EOS expects back."""
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%d/%m/%Y")


class PembertonFault(Exception):
    """A `<Fault>` document. HTTP status is always 200 (guide 2.1), so this is
    the only place a failure can be seen at all."""

    def __init__(self, code: str, message: str = "", retry_after: float = 0.0):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retry_after = retry_after


# EOS fault codes -> the SPEC section 6 closed set, with retryability.
# PH-400 is deliberately absent: it means "malformed request", which from an
# adapter is a fault in THIS file, and its one non-adapter meaning (an
# OrderNumber that is not ours) is handled at the call site where the context
# to say `not_found` exists.
FAULT_MAP = {
    "PH-401": ("unauthorized", True),
    "PH-402": ("unauthorized", False),
    # Long only on the cash equities: the market is open and the instrument is
    # fine, this account may simply not do that to it (SPEC 6's own words for
    # trade_restricted). Not `unsupported`, which is about methods.
    "PH-403": ("trade_restricted", False),
    "PH-404": ("unknown_symbol", False),
    "PH-405": ("market_closed", False),
    "PH-406": ("invalid_qty", False),
    # Cash cover on the equities and 10% margin on the futures fail with the
    # same code; both are "you cannot open this position", which is
    # insufficient_margin. insufficient_funds is reserved by SPEC 6 for a
    # withdrawal, and EOS has no withdrawal operation.
    "PH-407": ("insufficient_margin", False),
    "PH-410": ("not_resting", False),
    "PH-429": ("rate_limited", True),
    "PH-500": ("internal", True),
}


def to_adapter_error(f: PembertonFault) -> AdapterError:
    hit = FAULT_MAP.get(f.code)
    if hit is None:
        # An unmapped code keeps its identity as an x_ extension instead of
        # being flattened to `internal`, which would destroy the only
        # information the platform had (SPEC 6). PH-416, the 30-day history
        # window, arrives here on purpose: the closed set has no word for it.
        code = "x_pemberton_" + re.sub(r"[^a-z0-9]+", "_", f.code.lower())
        return AdapterError(code, f"{f.code}: {f.message}", False)
    return AdapterError(hit[0], f"{f.code}: {f.message}", hit[1])


class PembertonClient:
    """XML over HTTP, with the session pool and the poll throttle inside.

    Callers ask for an operation and get an ElementTree back; who is logged
    in, whose 2-second allowance is being spent and what to do about an
    expired token are all settled in here, once.
    """

    def __init__(self, base: str, state_dir: str):
        self.base = base.rstrip("/")
        self.state_dir = state_dir
        # credentials.json, NOT account.json: account.json is the registry's
        # pin file (the platform's explicit account choice), and a platform
        # account pick would overwrite credentials stored there, orphaning
        # the demo account. An older account.json credential file still loads
        # below.
        self.cred_path = os.path.join(state_dir, "credentials.json")
        self.legacy_cred_path = os.path.join(state_dir, "account.json")
        self.account = ""
        self.pin = ""
        self._from_env = False
        self._lock = threading.Lock()
        # One entry per pooled session: its token and the monotonic time its
        # Quote/OrderStatus allowance next frees up.
        self._slots = [{"token": None, "next_poll": 0.0, "lock":
                        threading.Lock()} for _ in range(POOL_SIZE)]
        self._rr = 0

    # -- credentials ---------------------------------------------------
    def load_account(self):
        """The account this adapter trades, from the operator or from its own
        state directory.

        Registration is open (guide 3.1), so an unconfigured profile opens its
        own demonstration account rather than refusing to start. The file
        lives in the profile's private state dir because a NEW account is an
        EMPTY account: two profiles sharing one file would trade one book, and
        opening a fresh account on every start would orphan yesterday's
        positions silently.
        """
        if ENV_ACCOUNT and ENV_PIN:
            self.account, self.pin, self._from_env = ENV_ACCOUNT, ENV_PIN, True
            return
        for path in (self.cred_path, self.legacy_cred_path):
            try:
                with open(path) as fh:
                    saved = json.load(fh)
            except (OSError, ValueError) as e:
                if path == self.legacy_cred_path:
                    log(f"account file unreadable ({e}); opening a new account")
                continue
            if saved.get("account") and saved.get("pin"):
                if saved.get("base") and saved["base"] != self.base:
                    # An account number is only meaningful at the venue that
                    # issued it. Pointed at a different EOS, the stored one is
                    # somebody else's number, not ours.
                    log(f"stored account belongs to {saved['base']}, not "
                        f"{self.base}; opening a new one")
                else:
                    self.account, self.pin = saved["account"], saved["pin"]
                    return
        self._open_account()

    def _open_account(self):
        r = self.call("OpenAccount", {"HolderName": HOLDER}, anon=True)
        self.account = (r.findtext("AccountNumber") or "").strip()
        self.pin = (r.findtext("Pin") or "").strip()
        if not self.account or not self.pin:
            raise AdapterError("internal", "OpenAccount returned no account")
        log(f"opened account {self.account} "
            f"({r.findtext('OpeningBalance')} {r.findtext('Currency')})")
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.cred_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"account": self.account, "pin": self.pin,
                           "base": self.base, "holder": HOLDER}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cred_path)
        except OSError as e:
            # Not fatal for this run, but every restart would open a new
            # account and lose the book, so it is said loudly.
            log(f"WARNING: could not persist credentials to {self.cred_path}: "
                f"{e}; a restart will not find this account")
        else:
            # Remove an older account.json copy; a platform choice file
            # (marked "chosen") is not ours to delete.
            try:
                with open(self.legacy_cred_path) as fh:
                    legacy = json.load(fh)
                if isinstance(legacy, dict) and legacy.get("pin") \
                        and not legacy.get("chosen"):
                    os.remove(self.legacy_cred_path)
            except (OSError, ValueError):
                pass

    # -- sessions ------------------------------------------------------
    def _token(self, slot: dict, force: bool = False) -> str:
        with slot["lock"]:
            if slot["token"] and not force:
                return slot["token"]
            slot["token"] = None
            try:
                r = self.call("Login", {"AccountNumber": self.account,
                                        "Pin": self.pin}, anon=True)
            except PembertonFault as f:
                if f.code == "PH-402" and not self._from_env:
                    # The venue does not know this account any more (its book
                    # was reset). A demonstration account we opened ourselves
                    # is replaceable; one the OPERATOR named is not, and
                    # quietly trading a different account would be the worst
                    # possible recovery.
                    log("stored account was refused (PH-402); the venue no "
                        "longer knows it, opening a fresh one")
                    self._open_account()
                    r = self.call("Login", {"AccountNumber": self.account,
                                            "Pin": self.pin}, anon=True)
                else:
                    raise
            slot["token"] = (r.findtext("SessionToken") or "").strip()
            if not slot["token"]:
                raise AdapterError("unauthorized", "Login returned no token")
            return slot["token"]

    def _next_slot(self) -> dict:
        """Round robin for unthrottled calls, so every pooled session keeps
        being used and none of them idles into the 15-minute expiry."""
        with self._lock:
            slot = self._slots[self._rr % len(self._slots)]
            self._rr += 1
        return slot

    def _reserve_poll_slot(self) -> tuple:
        """The session whose Quote/OrderStatus allowance frees up soonest, and
        how long to wait for it. Reserved inside the lock so two threads can
        never spend the same 2-second window."""
        with self._lock:
            slot = min(self._slots, key=lambda s: s["next_poll"])
            now = time.monotonic()
            ready = max(slot["next_poll"], now)
            slot["next_poll"] = ready + POLL_THROTTLE + POLL_MARGIN
            return slot, max(0.0, ready - now)

    def defer_slot(self, slot: dict, seconds: float):
        with self._lock:
            slot["next_poll"] = max(slot["next_poll"],
                                    time.monotonic() + seconds)

    # -- the wire ------------------------------------------------------
    def _post(self, op: str, fields: dict, token: str | None):
        root = ET.Element(op + "Request")
        if token is not None:
            # "the session token as its FIRST child element" (guide 3.3).
            ET.SubElement(root, "SessionToken").text = token
        for name, value in fields.items():
            if value is None:
                continue
            ET.SubElement(root, name).text = str(value)
        # Genuinely ISO-8859-1, not merely labelled (guide 2.2). Anything
        # outside Latin-1 degrades to a numeric character reference exactly as
        # EOS itself does on the way back, rather than raising.
        body = ('<?xml version="1.0" encoding="ISO-8859-1"?>'
                + ET.tostring(root, encoding="unicode")).encode(
                    "ISO-8859-1", errors="xmlcharrefreplace")
        req = urllib.request.Request(
            f"{self.base}/xmlapi/{op}", data=body, method="POST",
            headers={"Content-Type": "application/xml; charset=ISO-8859-1",
                     "Accept": "application/xml, text/csv"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            # EOS answers 200 for everything it understands, so a real status
            # here is the web server, not the venue.
            raise PembertonFault("PH-500", f"HTTP {e.code} from EOS") from None
        except (urllib.error.URLError, OSError) as e:
            raise PembertonFault("PH-500",
                                 f"{type(e).__name__}: {e}") from None

    @staticmethod
    def _parse(ctype: str, raw: bytes):
        """Body -> ElementTree, or CSV text for the one operation that answers
        with a download. THE 200 TRAP (guide 2.1): the fault check happens
        here, on every single response, before any caller sees a body."""
        if ctype.lower().startswith("text/csv"):
            return raw.decode("ISO-8859-1")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise PembertonFault("PH-500", f"unparseable body: {e}") from None
        if root.tag == "Fault":
            raise PembertonFault(
                (root.findtext("Code") or "PH-500").strip(),
                (root.findtext("Message") or "").strip(),
                float(_int(root.findtext("RetryAfterSeconds"), 0)))
        return root

    def call(self, op: str, fields: dict = None, anon: bool = False,
             throttled: bool = False, retries: int = 2):
        """One EOS operation. Session, throttle, relogin and retry live here.

        `throttled` marks the two operations that share the per-session
        2-second allowance (Quote and OrderStatus); everything else runs on a
        round-robin session with no wait.
        """
        fields = fields or {}
        pinned = None
        for attempt in range(retries + 1):
            last = attempt >= retries
            if anon:
                slot, token, wait = None, None, 0.0
            elif pinned is not None:
                # A retry after a relogin goes back to the SAME session. The
                # round robin would hand the next attempt a different slot,
                # whose token is just as dead, and a pool of four would need
                # four retries to work through what is one expired session.
                # A freshly issued token also carries no poll history, so the
                # throttle window does not need reserving again.
                slot, wait = pinned, 0.0
            elif throttled:
                slot, wait = self._reserve_poll_slot()
                token = None
            else:
                slot, wait, token = self._next_slot(), 0.0, None
            if wait:
                time.sleep(wait)
            if slot is not None:
                token = self._token(slot)
            try:
                return self._parse(*self._post(op, fields, token))
            except PembertonFault as f:
                if f.code == "PH-401" and slot is not None and not last:
                    # The sliding session died (15 minutes idle, or the venue
                    # restarted). One silent relogin: a platform should never
                    # see a session it does not know exists.
                    self._token(slot, force=True)
                    pinned = slot
                    continue
                if f.code == "PH-429" and not last:
                    # The local budget and the venue's clock disagreed. Push
                    # this session's window out by what it asked for and try
                    # again rather than losing the poll.
                    delay = f.retry_after or POLL_THROTTLE
                    if slot is not None:
                        self.defer_slot(slot, delay)
                    time.sleep(min(delay, 2.5))
                    continue
                if f.code == "PH-500" and not last:
                    time.sleep(0.3)
                    continue
                raise


class PembertonAdapter(Adapter):
    name = "pemberton"
    version = "1.0"
    broker = "Pemberton and Hale"
    # Truthful (SPEC 8.2): the venue's own guide opens with "Pemberton and
    # Hale is a simulated venue for integration testing. All prices are
    # synthetic. No real money is involved."
    mode = "paper"
    # "Accounts are EUR accounts" (guide 1); re-read from AccountSummary on
    # first call and complained about on stderr if the venue ever disagrees.
    account_currency = "EUR"
    # One Portfolio Line per instrument, and an opposing deal nets the holding
    # down rather than opening a second one: BUY 2 then SELL 5 on a Special
    # Future leaves one line of 3, not two lines.
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        state = os.environ.get("BRUE_BROKER_STATE") or os.path.expanduser(
            "~/.brue-connect/state/pemberton")
        self.api = PembertonClient(BASE, state)
        self._state_lock = threading.RLock()
        # Serialises the ledger fold. flatten_all drives it from the request
        # thread while the poller is driving it from its own, and the
        # high-water mark plus the emit are only idempotent as a pair: two
        # threads folding the same new row would announce one deal twice.
        self._ledger_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._instruments: dict[str, dict] = {}   # code -> catalog spec
        # code -> {net, avg, pos_id, opened_at, commission, client_id,
        #          from_order}; the book, netted from the deal ledger.
        self._book: dict[str, dict] = {}
        self._deal_hwm = 0                        # highest deal Number applied
        self._deal_realized: dict[int, Decimal] = {}
        self._orders: dict[str, dict] = {}        # order number -> our view
        self._by_client: dict[str, str] = {}
        self._close_intent: dict[str, str] = {}   # order number -> reason
        self._quote_syms: list[str] = []
        self._quote_last: dict[str, float] = {}
        self._quote_seen: dict[str, tuple] = {}
        self._snapshot_wanted: list[str] = []
        self._bars: dict[tuple, dict] = {}        # (code, tf) -> {last, next}
        self._account_seen: dict | None = None
        self._session_state = None

    # == handshake =====================================================
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # EOS has no stop loss, no take profit and no trailing field
                # anywhere on the wire: PlaceOrder carries Code, Direction,
                # Quantity, OrderType, LimitPrice and Reference, and nothing
                # else exists. Declared false so a platform refuses the
                # feature loudly instead of degrading silently (SPEC 3).
                "brackets": False,
                "modify": False,
                "trailing": False,
                # A holding can be sold in part: any quantity up to the netted
                # position is an ordinary order.
                "partial_close": True,
                "pending": True,
                # OrderType is MARKET or LIMIT. There is no stop order of any
                # kind, so neither `stop` nor `stop_limit` is claimed.
                "pending_types": ["limit"],
                # EOS accepts no expiry instruction at all and never expires a
                # resting order on its own: a working limit survives the close
                # and deals when the market next opens. That is `gtc` and only
                # gtc; `day` and `gtd` are refused with invalid_expiry.
                "expiry": ["gtc"],
                # A resting order holds nothing: EOS checks cover at
                # placement and again when it deals, so an order accepted
                # comfortably can still die at the moment it should have
                # filled. That is exactly the consequence SPEC 4 wants
                # declared.
                "reserves_margin": False,
                "oco": False,
                "close_by": False,
                # THE one that matters here: EOS has no operation that
                # enumerates orders. OrderStatus needs an OrderNumber you
                # already hold, so an order placed by another session is
                # invisible to this adapter forever. `orders.list` answers
                # from what this process placed and remembers, and saying so
                # is better than letting a platform read an empty list as
                # "no resting orders" (SPEC 3).
                "can_list": False,
            },
            "data": {
                "quotes": True,
                "bars": True,
                "history": True,
                # EOS reports no deposit, dividend, credit or adjustment: the
                # only thing that moves a balance is a deal and its
                # commission. There is nothing for history.operations to
                # return, so it is not claimed.
                "operations": False,
                # THE BROKER DECIDES THESE (SPEC 3). Quote takes one code per
                # call and one call per session per 2 s, so the pool's
                # bandwidth is the cap: POOL_SIZE sessions refresh
                # POOL_SIZE symbols at the venue's full rate. The whole
                # universe is six instruments and a six-symbol cycle costs
                # ~3 s, which is still a usable feed, so the cap is set there
                # rather than at the arithmetic minimum.
                "max_quote_symbols": 6,
                "max_symbols_per_request": 6,
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # What the PLATFORM may send us. EOS documents no limit outside the
        # shared Quote/OrderStatus bucket, which this adapter absorbs itself,
        # so the honest number is what the venue will comfortably take from
        # one integrator rather than a headline.
        return {"requests_per_second": 10}

    def broker_info(self) -> dict:
        # The broker's own words only. The guide states no website, no support
        # address and no regulator, so none are invented here (SPEC 3.1 is
        # explicit that this field is not for that). "Brussels" and "1893" are
        # its own first sentence.
        return {"display_name": "Pemberton and Hale",
                "tagline": "Electronic Order Service, Brussels, since 1893",
                "mark": "PH",
                "asset_classes": ["Cash equities", "Special Futures"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the broker something happens here, not in
        `hello` (SPEC 3.1): the account, the logins, the catalog, the first
        ledger read and the pollers."""
        try:
            self.api.load_account()
        except PembertonFault as f:
            raise to_adapter_error(f)
        self._load_instruments()
        summary = self._call("AccountSummary")
        ccy = (summary.findtext("Currency") or "").strip()
        if ccy and ccy != self.account_currency:
            # `hello` has already gone out; loud on stderr is the only honest
            # move left.
            log(f"WARNING: account currency is {ccy!r}, but the handshake "
                f"declared {self.account_currency!r}")
        # Adopt the existing ledger WITHOUT emitting: a restart must not
        # replay every fill this account has ever had as fresh news. SPEC 7
        # says the platform reconciles by asking, and positions.list will tell
        # it what is open.
        self._sync_ledger(emit=False)
        self._stop.clear()
        threading.Thread(target=self._ledger_loop, daemon=True).start()
        threading.Thread(target=self._bucket_loop, daemon=True).start()
        self._set_session("connected",
                          f"polling EOS as account {self.api.account}")

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()

    def _call(self, op: str, fields: dict = None, **kw):
        """An EOS operation on behalf of a REQUEST, with the venue's fault
        vocabulary already translated. The pollers call the client directly
        because they want to log a fault and carry on, not answer one."""
        try:
            return self.api.call(op, fields, **kw)
        except PembertonFault as f:
            raise to_adapter_error(f)

    # == catalog =======================================================
    def _load_instruments(self):
        root = self._call("Instruments")
        found = {}
        for el in root.findall("Instrument"):
            spec = self._spec(el)
            found[spec["symbol"]] = spec
        if not found:
            raise AdapterError("internal", "Instruments returned nothing")
        with self._state_lock:
            self._instruments = found

    @staticmethod
    def _sessions_for(code: str, hours: str) -> list:
        """`TradingHours` free text -> SPEC session windows.

        EOS publishes hours as a sentence ("07:00-15:30 UTC, Monday to
        Friday"), which is not a calendar a platform can gate on. The two
        shapes it uses are recognised by their wording rather than by the
        instrument's code, so a new instrument with either wording is covered;
        anything else publishes NO calendar, which SPEC 3 is emphatic means
        "unknown", not "closed".
        """
        text = (hours or "").strip()
        low = text.lower()
        if "24/7" in low or "no scheduled closures" in low:
            # Said explicitly as a seven-day window, never as an empty list:
            # "always open" and "no calendar published" look identical
            # otherwise and only one of them is safe to gate orders on.
            return [dict(s) for s in SESSIONS_24_7]
        m = _HOURS_RE.search(text)
        if m and "monday to friday" in low:
            return [{"days": [1, 2, 3, 4, 5], "open": m.group(1),
                     "close": m.group(2), "tz": "UTC"}]
        log(f"unrecognised TradingHours for {code}: {text!r}; publishing no "
            f"calendar")
        return []

    def _spec(self, el) -> dict:
        code = (el.findtext("Code") or "").strip()
        name = (el.findtext("Name") or code).strip()
        hours = (el.findtext("TradingHours") or "").strip()
        is_future = code.startswith(FUTURE_PREFIX)
        # The code range is the venue's documented symbology; the trading
        # hours are an independent signal for the same fact. If they ever
        # disagree, somebody should hear about it before a contract is sized
        # at the wrong multiplier.
        if is_future != ("24/7" in hours):
            log(f"{code} ({name!r}): code range says "
                f"{'future' if is_future else 'equity'} but TradingHours says "
                f"{hours!r}; treating it as the code range says")
        contract = FUTURE_CONTRACT_SIZE if is_future else 1
        margin = FUTURE_MARGIN_RATE if is_future else EQUITY_MARGIN_RATE
        rate = _dec(el.findtext("CommissionPercent")) / Decimal(100)
        return {
            "symbol": code,
            "name": name,
            # SPEC publishes no closed vocabulary for asset_class; these are
            # the words the bundled adapters use.
            "asset_class": "future" if is_future else "equity",
            # Nothing here is a currency pair, so there is no base currency to
            # state. Absent is the honest answer.
            "base_ccy": None,
            "quote_ccy": (el.findtext("Currency") or "EUR").strip(),
            "digits": 2,
            "tick_size": 0.01,
            "tick_value": round(0.01 * contract, 10),
            # "Quantities are always whole shares" (guide 2.3): the step is
            # one, and a fractional order is refused before it is sent.
            "lot_step": 1,
            "min_qty": _int(el.findtext("MinimumOrder"), 1),
            "max_qty": _int(el.findtext("MaximumOrder"), 0),
            "min_notional": 0,
            "contract_size": contract,
            "margin_rate": float(margin),
            "margin_maintenance_rate": float(margin),
            "margin_tiers": None,
            "sessions": self._sessions_for(code, hours),
            # EOS publishes no holiday calendar and no early closes; its
            # equity window is a fixed UTC week (guide 4).
            "holidays": [],
            "early_closes": [],
            "tradable": True,
            # "short sales are permitted on this class, and on this class
            # only; the cash equities remain long only" (guide 4.1).
            "shortable": bool(is_future),
            "trade_mode": "full" if is_future else "long_only",
            # No stops of any kind exist, so there is no minimum distance.
            "stops_level_points": 0,
            "fees": {"commission_per_lot": 0.0, "mode": "pct_notional",
                     "rate": float(rate),
                     "min": _money(_dec(el.findtext("CommissionMinimum")))},
            # EOS charges no financing anywhere: the Special Futures are
            # perpetual and carry no funding, and the equities are paid for in
            # full. 0 with swap_mode "none" says "not charged", which is the
            # truth, rather than a number in unknown units.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            # "There is no expiry date and no delivery of shares" (guide 4.1).
            "expires_at": None, "successor": None, "dividend": None,
            # Extra, and ignorable by the unknown-fields rule: EOS is the only
            # thing that knows which real-world instrument a 6-digit code is,
            # and dropping it would throw that away.
            "isin": (el.findtext("Isin") or "").strip(),
        }

    def handle_catalog_list(self, params):
        self._load_instruments()
        with self._state_lock:
            return {"instruments": [dict(s) for s in
                                    self._instruments.values()]}

    def _instrument(self, code) -> dict:
        code = str(code or "").strip()
        with self._state_lock:
            spec = self._instruments.get(code)
        if spec is None:
            self._load_instruments()      # a catalog can change; re-read once
            with self._state_lock:
                spec = self._instruments.get(code)
        if spec is None:
            raise AdapterError("unknown_symbol", f"no such code {code!r}")
        return spec

    # == account =======================================================
    def _account_view(self) -> dict:
        """balance / equity / margin_used / margin_free from the two reports
        that exist.

        AccountSummary gives cash, portfolio value and TotalValue, which the
        guide calls account equity. Neither report states margin, so it is
        computed the way the venue itself charges it: cost basis for the cash
        equities (which is the cleared funds actually committed) and 10% of
        consideration for the Special Futures. Balance is equity less what is
        still unrealised, which is what a ledger balance means.
        """
        summary = self._call("AccountSummary")
        equity = _dec(summary.findtext("TotalValue"))
        margin = D0
        unrealised = D0
        for line in self._call("Portfolio").findall("Line"):
            code = (line.findtext("Code") or "").strip()
            try:
                spec = self._instrument(code)
            except AdapterError:
                continue
            qty = _dec(line.findtext("Quantity"))
            avg = _dec(line.findtext("AveragePrice"))
            unrealised += _dec(line.findtext("UnrealisedResult"))
            margin += (avg * qty * Decimal(spec["contract_size"])
                       * Decimal(str(spec["margin_rate"])))
        return {"balance": _money(equity - unrealised),
                "equity": _money(equity),
                "margin_used": _money(margin),
                "margin_free": _money(equity - margin),
                "currency": (summary.findtext("Currency") or "EUR").strip()}

    def handle_account_get(self, params):
        view = self._account_view()
        with self._state_lock:
            self._account_seen = dict(view)
        return view

    # == positions =====================================================
    def handle_positions_list(self, params):
        lines = self._call("Portfolio").findall("Line")
        out = []
        for line in lines:
            code = (line.findtext("Code") or "").strip()
            qty = _dec(line.findtext("Quantity"))
            unreal = _dec(line.findtext("UnrealisedResult"))
            with self._state_lock:
                b = dict(self._book.get(code) or {})
            if b and abs(b["net"]) != qty:
                # The broker is the source of truth (SPEC 7). Reported, not
                # silently reconciled: the next ledger poll settles it, and an
                # adapter quietly rewriting the broker's number is how a
                # platform ends up trusting a fiction.
                log(f"{code}: Portfolio says {qty} but the deal ledger nets "
                    f"{b['net']}; showing the broker's size")
            side, pos_id, opened, avg = self._position_identity(
                code, b, qty, unreal, _dec(line.findtext("AveragePrice")),
                _dec(line.findtext("CurrentPrice")))
            pos = {"position_id": pos_id,
                   "symbol": code,
                   "side": side,
                   "qty": float(qty),
                   "avg_price": _money(avg),
                   "sl": None, "tp": None, "trail_points": 0,
                   "unrealized_pnl": _money(unreal),
                   # EOS charges no financing at all, and it DOES report
                   # commission per deal, so the split is real rather than a
                   # zero standing in for "this broker never told me".
                   "swap": 0.0,
                   "commission": _money(-(b.get("commission") or D0)),
                   "opened_at": opened,
                   "from_order": b.get("from_order")}
            if b.get("client_id"):
                pos["client_id"] = b["client_id"]
            out.append(pos)
        return {"positions": out}

    def _position_identity(self, code, b, qty, unreal, avg, current):
        """Side, id and opening time for a Portfolio line.

        Portfolio reports an UNSIGNED Quantity and no direction at all, so a
        short Special Future is indistinguishable from a long one there. The
        deal ledger is the authority; the fallback exists only for a holding
        older than the ledger window, and it says on stderr that it guessed.
        """
        if b:
            return ("buy" if b["net"] > 0 else "sell", b["pos_id"],
                    b["opened_at"], b["avg"] or avg)
        spec = self._instrument(code)
        side = "buy"
        if spec["shortable"] and current != avg:
            # (current - avg) * direction has the sign of the unrealised
            # result. Only usable when the two prices differ, and only worth
            # trying on the one class that can be short.
            side = "buy" if (unreal > 0) == (current > avg) else "sell"
            log(f"{code}: no ledger history for this holding; side inferred "
                f"as {side} from the unrealised result")
        return side, f"{code}@0", 0, avg

    # == orders ========================================================
    def _validate_qty(self, spec: dict, raw) -> int:
        """Whole shares, within the instrument's own minimum and maximum.

        EOS answers a bad size with PH-406 whatever is wrong with it, and a
        fractional quantity is not even representable on its wire (the
        Quantity element is a plain integer), so the check happens here where
        the reason can be said out loud.
        """
        try:
            qty = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            raise AdapterError("invalid_qty", f"qty {raw!r} is not a number")
        if qty != qty.to_integral_value():
            raise AdapterError(
                "invalid_qty",
                f"qty {raw} is not a whole number of shares; EOS deals whole "
                f"shares only (lot_step 1)")
        n = int(qty)
        if n <= 0:
            raise AdapterError("invalid_qty", f"qty must be positive, got {n}")
        if n < spec["min_qty"]:
            raise AdapterError("invalid_qty",
                               f"qty {n} below the minimum {spec['min_qty']}")
        if spec["max_qty"] and n > spec["max_qty"]:
            raise AdapterError("invalid_qty",
                               f"qty {n} above the maximum {spec['max_qty']}")
        return n

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        symbol = str(params.get("symbol") or "").strip()
        side = str(params.get("side") or "").lower()
        otype = str(params.get("type") or "market").lower()
        # Everything that decides what gets DEALT. EOS will not refuse a
        # conflicting reuse for us: a duplicate Reference returns the ORIGINAL
        # order whatever the new request said, which is precisely the silent
        # hazard SPEC 6 exists to stop.
        sig = (symbol, side, str(params.get("qty")), otype,
               str(params.get("price")), str(params.get("stop_price")),
               str(params.get("expiry")), str(params.get("expiry_at")))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("invalid_qty",
                               f"side must be buy or sell, got "
                               f"{params.get('side')!r}")
        spec = self._instrument(symbol)
        qty = self._validate_qty(spec, params.get("qty"))
        for field, why in (("sl", "stop loss"), ("tp", "take profit"),
                           ("trail_points", "trailing stop")):
            if params.get(field) is not None:
                raise AdapterError(
                    "unsupported",
                    f"EOS has no {why}: PlaceOrder carries no such field")
        if otype in ("stop", "stop_limit"):
            raise AdapterError("unsupported",
                               f"EOS deals MARKET and LIMIT only, not "
                               f"{otype!r}")
        if otype not in ("market", "limit"):
            raise AdapterError("unsupported", f"unknown order type {otype!r}")
        expiry = str(params.get("expiry") or "gtc").lower()
        if expiry != "gtc":
            # Declared in capabilities.orders.expiry, so a platform should
            # never send anything else; EOS has no expiry instruction at all
            # and a resting order simply waits.
            raise AdapterError("invalid_expiry",
                               f"EOS honours gtc only, not {expiry!r}")
        fields = {"Code": symbol, "Direction": side.upper(),
                  "Quantity": str(qty), "OrderType": otype.upper(),
                  "Reference": client_id}
        price = None
        if otype == "limit":
            if params.get("price") is None:
                raise AdapterError("x_invalid_price",
                                   "a limit order needs `price`")
            price = _dec(str(params["price"]))
            fields["LimitPrice"] = _fmt_dec(price, spec["digits"])
        return self._send_order(client_id, sig, symbol, side, qty, otype,
                                price, fields)

    def _send_order(self, client_id, sig, symbol, side, qty, otype, price,
                    fields):
        try:
            r = self.api.call("PlaceOrder", fields)
        except PembertonFault as f:
            err = to_adapter_error(f)
            # SPEC 8.4: no mutating call may be silently dropped. A refusal is
            # a terminal outcome and is announced as one, so a platform
            # watching only the event stream still sees the order end.
            if client_id:
                self.emit("order.rejected", {"client_id": client_id,
                                             "code": err.code,
                                             "message": str(err)})
            raise err
        order_id = (r.findtext("OrderNumber") or "").strip()
        with self._state_lock:
            self._orders[order_id] = {
                "order_id": order_id, "client_id": client_id,
                "symbol": symbol, "side": side, "qty": Decimal(qty),
                "filled": D0, "type": otype, "price": price,
                "state": "new", "placed_at": int(time.time() * 1000),
                "placed_mono": time.monotonic(), "last_status": 0.0,
                "done_at": None, "reason": None, "degraded": False}
            if client_id:
                self._by_client[client_id] = order_id
        if client_id:
            self.emit("order.accepted", {"client_id": client_id,
                                         "order_id": order_id})
        # "Every accepted PlaceOrder call answers PENDING, regardless of order
        # type" (guide 6.1), so the response says nothing about what happened.
        # The status here is the SPEC's transport-level word for the order
        # TYPE, and the truth arrives as events from the ledger poll.
        result = {"order_id": order_id,
                  "status": "resting" if otype == "limit" else "accepted"}
        self._wake.set()
        if client_id:
            return self.idempotent_store(client_id, result, sig)
        return result

    def handle_orders_list(self, params):
        """What THIS process placed and remembers.

        EOS has no enumerate operation, which is why `orders.can_list` is
        false: an order placed from another session can never appear here. A
        platform is told that in the handshake and marks its order view
        partial; answering with the honest subset still beats refusing.
        """
        want = (params or {}).get("state")
        out = []
        with self._state_lock:
            rows = [dict(o) for o in self._orders.values()]
        for o in rows:
            state = {"new": "resting", "resting": "resting",
                     "filled": "filled", "cancelled": "cancelled",
                     "expired": "expired",
                     "refused": "cancelled"}.get(o["state"], "resting")
            if want and state != want:
                continue
            out.append({
                "order_id": o["order_id"], "client_id": o["client_id"],
                "symbol": o["symbol"], "type": o["type"], "side": o["side"],
                "qty": float(o["qty"]),
                "price": float(o["price"]) if o["price"] is not None else None,
                "stop_price": None, "sl": None, "tp": None,
                "expiry": "gtc", "expires_at": None,
                "state": state, "placed_at": o["placed_at"],
                "done_at": o["done_at"],
                "fill_price": None, "reason": o["reason"]})
        return {"orders": out}

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "").strip()
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        with self._state_lock:
            known = self._orders.get(order_id)
        try:
            self.api.call("CancelOrder", {"OrderNumber": order_id})
        except PembertonFault as f:
            if f.code == "PH-410":
                # PH-410 is one code for two conditions: the order is not open
                # any more, and the order is not ours at all. Only this side
                # can tell them apart, and only for the orders it placed.
                raise AdapterError(
                    "not_resting" if known else "not_found",
                    f"{f.code}: {f.message}"
                    + ("" if known else " (this adapter never placed that "
                                        "order; EOS cannot enumerate orders, "
                                        "so it may belong to another session)"))
            raise to_adapter_error(f)
        self._finish_order(order_id, "cancelled")
        return {}

    def handle_order_amend(self, params):
        # EOS has PlaceOrder and CancelOrder and nothing between them. SPEC 3
        # asks for a loud refusal rather than an emulation; cancel-and-replace
        # would hand back a NEW order number, and `order.amended` has no field
        # to carry one, so the emulation could not even be described here.
        raise AdapterError("unsupported",
                           "EOS cannot amend a working order; cancel it and "
                           "place a new one")

    def handle_order_modify(self, params):
        raise AdapterError("unsupported",
                           "EOS has no stop loss or take profit to modify")

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported", "EOS has no close-by")

    def handle_history_operations(self, params):
        raise AdapterError("unsupported",
                           "EOS reports no non-trade ledger entries")

    # == closing =======================================================
    def handle_position_close(self, params):
        pos_id = str(params.get("position_id") or "").strip()
        if not pos_id:
            raise AdapterError("not_found", "position_id is required")
        client_id = params.get("client_id")
        if client_id:
            prior = self.idempotent_check(client_id,
                                          ("close", pos_id,
                                           str(params.get("qty"))))
            if prior is not None:
                return prior
        code, b = self._position_for(pos_id)
        qty = params.get("qty")
        want = abs(b["net"]) if qty is None else Decimal(str(qty))
        if want <= 0 or want > abs(b["net"]):
            raise AdapterError("invalid_qty",
                               f"cannot close {want} of a position of "
                               f"{abs(b['net'])}")
        self._close_leg(code, b, int(want), "manual")
        result = {}
        if client_id:
            self.idempotent_store(client_id, result,
                                  ("close", pos_id, str(params.get("qty"))))
        return result

    def _position_for(self, pos_id: str):
        with self._state_lock:
            for code, b in self._book.items():
                if b["pos_id"] == pos_id:
                    return code, dict(b)
        # Not in the book: it may have closed a moment ago, or never existed.
        # One ledger refresh settles which, since the ledger is the only place
        # a position id comes from at all.
        self._sync_ledger()
        with self._state_lock:
            for code, b in self._book.items():
                if b["pos_id"] == pos_id:
                    return code, dict(b)
        raise AdapterError("not_found", f"no open position {pos_id!r}")

    def _close_leg(self, code: str, b: dict, qty: int, reason: str):
        """The opposite deal. EOS has no close operation: a position is closed
        by dealing against it, which is also why the close inherits the market
        calendar (a cash equity cannot be closed outside its hours, and saying
        so is more honest than pretending to queue it)."""
        side = "sell" if b["net"] > 0 else "buy"
        # A Reference minted here, so a retry of the same close is idempotent
        # AT THE VENUE too, not merely in this process.
        ref = f"close-{uuid.uuid4().hex[:16]}"
        fields = {"Code": code, "Direction": side.upper(),
                  "Quantity": str(qty), "OrderType": "MARKET",
                  "Reference": ref}
        try:
            r = self.api.call("PlaceOrder", fields)
        except PembertonFault as f:
            raise to_adapter_error(f)
        order_id = (r.findtext("OrderNumber") or "").strip()
        with self._state_lock:
            # Only this side knows whether a close was a position.close or one
            # leg of a flatten_all; EOS sees an ordinary deal either way.
            self._close_intent[order_id] = reason
            self._orders[order_id] = {
                "order_id": order_id, "client_id": None, "symbol": code,
                "side": side, "qty": Decimal(qty), "filled": D0,
                "type": "market", "price": None, "state": "new",
                "placed_at": int(time.time() * 1000),
                "placed_mono": time.monotonic(), "last_status": 0.0,
                "done_at": None, "reason": None, "degraded": False}
        self._wake.set()
        return order_id

    def handle_flatten_all(self, params):
        """Close everything, and do not come back until the book agrees.

        SPEC 8.1 wants this to work while other calls are failing, so each leg
        is attempted independently and one refusal never abandons the rest.
        A cash equity outside its hours genuinely cannot be closed (EOS
        refuses a MARKET order with PH-405 and never queues one); that is
        reported as a degraded session rather than swallowed.
        """
        self._sync_ledger()
        with self._state_lock:
            legs = [(code, dict(b)) for code, b in self._book.items()
                    if b["net"] != 0]
        closed, refused = 0, {}
        for code, b in legs:
            try:
                self._close_leg(code, b, int(abs(b["net"])), "flatten")
                closed += 1
            except AdapterError as e:
                refused[code] = e.code
                log(f"flatten: {code} did not close: {e}")
        deadline = time.time() + 8.0
        while closed and time.time() < deadline:
            self._sync_ledger()
            with self._state_lock:
                left = [c for c, b in self._book.items() if b["net"] != 0
                        and c not in refused]
            if not left:
                break
            time.sleep(0.3)
        if refused:
            self._set_session(
                "degraded",
                "flatten_all could not close " + ", ".join(
                    f"{c} ({why})" for c, why in sorted(refused.items())))
        return {"closed": closed}

    # == market data ===================================================
    def handle_quotes_subscribe(self, params):
        symbols = [str(s).strip() for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._instrument(s)          # unknown_symbol before subscribing
        cap = self.capabilities()["data"]["max_quote_symbols"]
        dropped = []
        with self._state_lock:
            for s in symbols:
                if s in self._quote_syms:
                    continue
                if len(self._quote_syms) >= cap:
                    dropped.append(s)
                    continue
                self._quote_syms.append(s)
                self._quote_last[s] = 0.0
            # A snapshot for each new subscription, jumping the poll queue.
            # Done on the poller rather than in this handler because each
            # snapshot spends a 2-second allowance, and six of them in a row
            # would hold the response past the platform's own deadline.
            self._snapshot_wanted.extend(s for s in symbols
                                         if s not in dropped)
        if dropped:
            # SPEC 3: an adapter capped lower than asked takes what it can and
            # reports the rest as degraded, rather than leaving prices that
            # silently never arrive.
            self._set_session("degraded",
                              f"quote capacity is {cap} symbols; not priced: "
                              + ", ".join(dropped))
        self._wake.set()
        return {}

    def handle_quotes_unsubscribe(self, params):
        drop = {str(s).strip() for s in (params.get("symbols") or [])}
        with self._state_lock:
            self._quote_syms = [s for s in self._quote_syms if s not in drop]
            for s in drop:
                self._quote_last.pop(s, None)
                self._quote_seen.pop(s, None)
        return {}

    def _poll_quote(self, code: str, force: bool = False):
        try:
            r = self.api.call("Quote", {"Code": code}, throttled=True)
        except PembertonFault as f:
            log(f"quote {code}: {f}")
            return
        bid = _dec(r.findtext("Bid"))
        ask = _dec(r.findtext("Ask"))
        stamp = (bid, ask)
        with self._state_lock:
            unchanged = self._quote_seen.get(code) == stamp
            self._quote_seen[code] = stamp
        # There is no stream, so every quote is a poll; re-publishing a price
        # that has not moved would turn a 2-second poll into a fake tick. The
        # snapshot on subscribe is the deliberate exception, so a subscriber
        # always gets a price even from a market that has stopped moving.
        if unchanged and not force:
            return
        self.emit("quote", {"symbol": code, "bid": float(bid),
                            "ask": float(ask),
                            "time": _ms(r.findtext("Time"))})

    def _history_rows(self, code: str, gran: str, days: int) -> list:
        """One History download, parsed. Semicolon separated, comma decimals,
        `DD/MM/YYYY` plus `HH:MM:SS`, and only bars for which the market was
        actually open (there is no row for an untraded hour)."""
        now = time.time()
        # "`From` may not be more than 30 days before today" (PH-416), and the
        # window is dates, not timestamps, so it is clamped here rather than
        # discovered as a fault.
        days = max(1, min(days, HISTORY_MAX_DAYS - 1))
        start = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)
        body = self.api.call("History", {
            "Code": code, "Granularity": gran,
            "From": start.strftime("%d/%m/%Y"),
            # "a `To` reaching into today or later is simply clipped to the
            # most recently completed bar", so today is always the right end.
            "To": _wire_date(now * 1000)})
        rows = []
        reader = csv.reader(io.StringIO(body), delimiter=";")
        for i, row in enumerate(reader):
            if i == 0 or len(row) < 7:
                continue          # header, or the trailing blank line
            ts = _ms(f"{row[0]} {row[1]}")
            rows.append([ts, float(_dec(row[2])), float(_dec(row[3])),
                         float(_dec(row[4])), float(_dec(row[5])),
                         float(_int(row[6]))])
        rows.sort(key=lambda b: b[0])
        return rows

    @staticmethod
    def _fold(rows: list, seconds: int) -> list:
        """HOUR rows -> 4-hour buckets, in epoch MILLISECONDS.

        A bucket is emitted once its window has ELAPSED, not once four hourly
        rows have been collected. EOS returns no row for an hour the market
        was shut (guide 5.2), and an equity that opens at 07:00 has one hour
        in its first bucket of the day and four in the next; counting rows
        would silently delete the open and the close of every session. The
        clock test is what the SPEC's closed-bars-only rule actually asks
        for, and it is the only one that can be applied to a calendar this
        adapter does not own.
        """
        span = seconds * 1000
        now_ms = time.time() * 1000
        out, bucket = [], {}
        for ts, o, h, l, c, v in rows:
            key = (ts // span) * span
            b = bucket.get(key)
            if b is None:
                bucket[key] = [key, o, h, l, c, v]
            else:
                b[2] = max(b[2], h)
                b[3] = min(b[3], l)
                b[4] = c
                b[5] += v
        for key in sorted(bucket):
            if key + span <= now_ms:
                out.append(bucket[key])
        return out

    def handle_bars_history(self, params):
        code = str(params.get("symbol") or "").strip()
        self._instrument(code)
        tf = str(params.get("timeframe") or "1h")
        if tf not in TF_SECONDS:
            raise AdapterError(
                "unsupported",
                f"EOS publishes HOUR and DAY bars only; {tf!r} is not "
                f"available (1h, 4h and 1d are)")
        count = max(1, _int(params.get("count"), 200))
        seconds = TF_SECONDS[tf]
        # The window is expressed in whole days because EOS takes dates, not
        # timestamps. One extra day covers the partial day at each end.
        days = int(count * seconds / 86400) + 2
        try:
            rows = self._history_rows(code, TF_GRANULARITY[tf], days)
        except PembertonFault as f:
            raise to_adapter_error(f)
        if seconds == 14400:
            rows = self._fold(rows, seconds)
        return {"bars": rows[-count:]}

    def handle_bars_subscribe(self, params):
        code = str(params.get("symbol") or "").strip()
        self._instrument(code)
        tf = str(params.get("timeframe") or "1h")
        if tf not in TF_SECONDS:
            raise AdapterError("unsupported",
                               f"EOS publishes HOUR and DAY bars only; "
                               f"{tf!r} is not available")
        with self._state_lock:
            self._bars.setdefault((code, tf),
                                  {"last": 0, "next": 0.0, "primed": False})
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._state_lock:
            self._bars.pop((str(params.get("symbol") or "").strip(),
                            str(params.get("timeframe") or "1h")), None)
        return {}

    # == history =======================================================
    def handle_history_fills(self, params):
        since = params.get("from")
        fields = {}
        if since:
            # `Since` is a DATE and includes the whole of that day, so the
            # exact millisecond is filtered here instead.
            fields["Since"] = _wire_date(float(since))
        root = self._call("TransactionList", fields)
        to = float(params.get("to") or 0) or None
        fills = []
        for t in root.findall("Transaction"):
            when = _ms(t.findtext("Time"))
            if since and when < float(since):
                continue
            if to and when > to:
                continue
            number = _int(t.findtext("Number"))
            fills.append({
                "fill_id": str(number),
                "order_id": (t.findtext("OrderNumber") or "").strip() or None,
                "symbol": (t.findtext("Code") or "").strip(),
                "side": "buy" if (t.findtext("Direction") or "").strip()
                        == "BUY" else "sell",
                "qty": float(_int(t.findtext("Quantity"))),
                "price": _money(_dec(t.findtext("Price"))),
                "fees": _money(-_dec(t.findtext("Commission"))),
                "realized_pnl": _money(
                    self._deal_realized.get(number, D0)),
                "time": when})
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == the ledger state machine ======================================
    def _sync_ledger(self, emit: bool = True):
        """One TransactionList, folded into the book.

        This is the whole event source. EOS never volunteers anything, so a
        deal is only ever learned by asking for the ledger again, and the
        ledger restates every row it has already given us. Nothing is emitted
        for a row at or below the high-water mark, so a poll that finds
        nothing new is silent, which is the only way a polled venue can be
        made to look like a stream without inventing traffic.
        """
        with self._ledger_lock:
            self._sync_ledger_locked(emit)

    def _sync_ledger_locked(self, emit: bool):
        try:
            root = self.api.call("TransactionList")
        except PembertonFault as f:
            self._set_session("degraded", f"ledger poll failed: {f}")
            return
        if (self._session_state and self._session_state[0] == "degraded"
                and self._session_state[1].startswith("ledger poll failed")):
            # The venue answered again. A degradation the platform can no
            # longer act on is noise, so the recovery is announced; the other
            # degradations (a capped quote list, an order past its deadline)
            # are still true and are left standing.
            self._set_session("connected",
                              f"polling EOS as account {self.api.account}")
        rows = []
        for t in root.findall("Transaction"):
            rows.append({
                "number": _int(t.findtext("Number")),
                "order_id": (t.findtext("OrderNumber") or "").strip(),
                "code": (t.findtext("Code") or "").strip(),
                "side": "buy" if (t.findtext("Direction") or "").strip()
                        == "BUY" else "sell",
                "qty": Decimal(_int(t.findtext("Quantity"))),
                "price": _dec(t.findtext("Price")),
                "fee": _dec(t.findtext("Commission")),
                "time": _ms(t.findtext("Time"))})
        rows.sort(key=lambda r: r["number"])
        for row in rows:
            if row["number"] <= self._deal_hwm:
                continue
            try:
                self._apply_deal(row, emit)
            except Exception as e:      # noqa: BLE001 - one bad row only
                log(f"deal {row['number']}: {type(e).__name__}: {e}")
            self._deal_hwm = row["number"]
        self._check_order_deadlines()

    def _apply_deal(self, row: dict, emit: bool):
        """One deal against the netted book, emitting on transitions only."""
        code = row["code"]
        try:
            spec = self._instrument(code)
        except AdapterError:
            log(f"deal {row['number']} references unknown code {code}")
            return
        contract = Decimal(spec["contract_size"])
        signed = row["qty"] if row["side"] == "buy" else -row["qty"]
        price, fee = row["price"], row["fee"]
        realized = D0
        with self._state_lock:
            b = self._book.get(code)
            meta = self._orders.get(row["order_id"])
            intent = self._close_intent.get(row["order_id"])
        closed_qty = D0
        closed_id = None
        closed_client = None
        closed_commission = D0
        remaining_after = D0

        if b and b["net"] != 0 and (b["net"] > 0) != (signed > 0):
            # An opposing deal: it closes as much of the holding as it covers,
            # and anything left over opens a new position the other way round
            # (EOS allows exactly that on the Special Futures: BUY 2 then
            # SELL 5 leaves a short 3 under one Portfolio line).
            closed_qty = min(abs(b["net"]), abs(signed))
            direction = Decimal(1) if b["net"] > 0 else Decimal(-1)
            realized = (price - b["avg"]) * direction * closed_qty * contract
            # Commission is charged per ORDER, so the opening fees a position
            # carries are attributed to the shares they bought: the closed
            # fraction takes its share and the rest stays with what is left.
            share = (b["commission"] * closed_qty / abs(b["net"])
                     if b["net"] else D0)
            closed_commission = share + fee
            closed_id = b["pos_id"]
            # SPEC 4: the client id on a position is the one that OPENED it,
            # so it is read before the book entry is torn down; the closing
            # order's own id would name the wrong order entirely.
            closed_client = b.get("client_id")
            new_net = b["net"] + signed
            with self._state_lock:
                if new_net == 0:
                    self._book.pop(code, None)
                elif (new_net > 0) == (b["net"] > 0):
                    b["net"] = new_net
                    b["commission"] = b["commission"] - share
                    remaining_after = abs(new_net)
                else:
                    # Flipped through zero: a brand new position, opened by
                    # this very deal and identified by it.
                    self._book[code] = self._new_position(
                        code, new_net, price, D0, row, meta)
        else:
            with self._state_lock:
                if not b:
                    self._book[code] = self._new_position(
                        code, signed, price, fee, row, meta)
                else:
                    total = abs(b["net"]) + abs(signed)
                    b["avg"] = ((b["avg"] * abs(b["net"])
                                 + price * abs(signed)) / total)
                    b["net"] = b["net"] + signed
                    b["commission"] = b["commission"] + fee

        self._deal_realized[row["number"]] = realized
        remaining_on_order = None
        with self._state_lock:
            live = self._book.get(code)
            position_id = (live or {}).get("pos_id") or closed_id
            if meta is not None:
                # Counted for EVERY order this process placed, including the
                # closing legs it generates itself. Counting it only where a
                # fill is announced would leave an internal close sitting at
                # `new` for ever, keep the pollers busy and eventually report
                # an ordinary close as a degraded session.
                meta["filled"] = meta["filled"] + row["qty"]
                remaining_on_order = meta["qty"] - meta["filled"]
                if remaining_on_order <= 0:
                    meta["state"] = "filled"
                    meta["done_at"] = row["time"]
        if emit:
            if meta is not None and meta.get("client_id"):
                self._emit_fill(row, meta, position_id, remaining_on_order)
            if closed_qty > 0:
                self._emit_close(row, closed_id, closed_qty, price, realized,
                                 closed_commission, intent, meta,
                                 remaining_after, closed_client)

    def _new_position(self, code, net, price, commission, row, meta) -> dict:
        return {"net": net, "avg": price, "commission": commission,
                "pos_id": f"{code}@{row['number']}",
                "opened_at": row["time"],
                # SPEC 4: a position's client_id is the id of the order that
                # OPENED it, so the first writer wins and a later order that
                # merely adds to the holding does not rename it.
                "client_id": (meta or {}).get("client_id"),
                "from_order": ((meta or {}).get("type")
                               if (meta or {}).get("type") != "market"
                               else None)}

    def _emit_fill(self, row, meta, position_id, remaining):
        """order.filled / order.partial, for orders the PLATFORM placed.

        A close this adapter generated itself has no client_id and is not the
        platform's order; its consequence is the position.closed event, and
        announcing a fill nobody asked for would put an order on the
        platform's book that it never placed.
        """
        data = {"client_id": meta["client_id"], "order_id": meta["order_id"],
                "position_id": position_id, "symbol": row["code"],
                "side": row["side"], "qty": float(row["qty"]),
                "price": _money(row["price"]),
                "fees": _money(-row["fee"]), "time": row["time"]}
        if remaining > 0:
            data["remaining"] = float(remaining)
            self.emit("order.partial", data)
        else:
            self.emit("order.filled", data)

    def _emit_close(self, row, position_id, qty, price, realized, commission,
                    intent, meta, remaining, opened_by):
        reason = intent
        if reason is None:
            # A deal from an order this process placed closed it: the client
            # did it, which is `manual`. Anything else (an order from another
            # session, which EOS cannot even enumerate for us) is genuinely
            # unattributable, and SPEC 5 is explicit that `unknown` beats a
            # plausible guess.
            reason = "manual" if (meta and meta.get("client_id")) else "unknown"
        data = {"position_id": position_id, "qty": float(qty),
                "price": _money(price), "realized_pnl": _money(realized),
                # EOS charges no financing of any kind; the commission is its
                # own reported number. Reporting 0 swap here means "none was
                # charged", which is true, rather than "this broker never
                # told me".
                "swap": 0.0, "commission": _money(-commission),
                "reason": reason, "time": row["time"]}
        if remaining:
            data["remaining"] = float(remaining)
        if opened_by:
            data["client_id"] = opened_by
        self.emit("position.closed", data)

    # == order status backstop =========================================
    def _poll_order_status(self, order_id: str):
        """OrderStatus for one order, spending a throttled allowance.

        Only reached for an order the ledger has not resolved, which is what
        it is for: a resting limit produces no deal at all, and a cancelled,
        expired or refused order never will.
        """
        try:
            r = self.api.call("OrderStatus", {"OrderNumber": order_id},
                              throttled=True)
        except PembertonFault as f:
            if f.code == "PH-400":
                # "An OrderNumber that does not belong to your account, or
                # that has never existed, fails with PH-400" (guide 6.2). For
                # a number EOS itself just issued us that should be
                # impossible, so it is terminated as a rejection rather than
                # reported as a cancellation that nobody performed.
                log(f"order {order_id}: EOS does not recognise it")
                self._finish_order(order_id, "refused",
                                   "EOS does not recognise this order number")
                return
            log(f"order status {order_id}: {f}")
            return
        status = (r.findtext("Status") or "").strip().upper()
        reason = (r.findtext("Reason") or "").strip()
        with self._state_lock:
            meta = self._orders.get(order_id)
            if meta is None or meta["state"] not in ("new", "resting"):
                return
            prev = meta["state"]
            client_id = meta["client_id"]
            price = meta["price"]
        if status == "WORKING" and prev != "resting":
            with self._state_lock:
                self._orders[order_id]["state"] = "resting"
            self.emit("order.placed", {
                "client_id": client_id, "order_id": order_id, "type": "limit",
                "price": float(price) if price is not None else None,
                # EOS honours no expiry instruction and never times an order
                # out on its own, so there is no expiry to report.
                "expires_at": None})
        elif status == "CANCELLED":
            self._finish_order(order_id, "cancelled")
        elif status == "EXPIRED":
            self._finish_order(order_id, "expired")
        elif status == "REFUSED":
            self._finish_order(order_id, "refused", reason)
        # DEALT is deliberately ignored here: the ledger owns fills, and
        # emitting one from this side too would be the second source of truth
        # that turns one deal into two.

    def _finish_order(self, order_id: str, state: str, detail: str = ""):
        with self._state_lock:
            meta = self._orders.get(order_id)
            if meta is None or meta["state"] in ("filled", "cancelled",
                                                 "expired", "refused"):
                return
            was = meta["state"]
            meta["state"] = state
            meta["done_at"] = int(time.time() * 1000)
            client_id = meta["client_id"]
        if state == "cancelled":
            # `code` present means the BROKER killed it (SPEC 5). A cancel the
            # platform asked for carries none, and this adapter only ever
            # cancels on request.
            self.emit("order.cancelled", {"order_id": order_id,
                                          "client_id": client_id})
        elif state == "expired":
            self.emit("order.expired", {"order_id": order_id,
                                        "client_id": client_id,
                                        "expiry": "gtc"})
        elif state == "refused":
            with self._state_lock:
                self._orders[order_id]["reason"] = "internal"
            # Not expected: this venue refuses synchronously, as a Fault on
            # PlaceOrder. Handled anyway because the guide documents REFUSED
            # as a status, and its free-text Reason is passed through verbatim
            # rather than parsed into a code that would only ever be a guess.
            self.emit("order.rejected", {
                "client_id": client_id, "code": "internal",
                "message": detail or "refused by the dealing desk"})
        return was

    def _check_order_deadlines(self):
        overdue = []
        now = time.monotonic()
        with self._state_lock:
            for oid, meta in self._orders.items():
                if (meta["state"] == "new" and not meta["degraded"]
                        and now - meta["placed_mono"] > ORDER_DEADLINE):
                    meta["degraded"] = True
                    overdue.append(oid)
        for oid in overdue:
            # SPEC 8.4: an order that has not terminated inside the declared
            # timeout is reported, not quietly waited on.
            self._set_session("degraded",
                              f"order {oid} has not reached a terminal state "
                              f"within {int(ORDER_DEADLINE)}s")

    # == the pollers ===================================================
    def _ledger_loop(self):
        """The unthrottled lane: deals, the account, and closed bars.

        TransactionList, Portfolio, AccountSummary and History carry no
        documented rate limit, but the guide asks callers not to read that as
        an invitation, so this idles at one call every 1.5 s and only tightens
        while an order is actually in flight.
        """
        next_account = 0.0
        while not self._stop.is_set():
            busy = bool(self._inflight())
            self._wake.wait(LEDGER_BUSY if busy else LEDGER_IDLE)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._sync_ledger()
                if time.monotonic() >= next_account:
                    next_account = time.monotonic() + ACCOUNT_INTERVAL
                    self._poll_account()
                self._poll_bars()
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log(f"ledger poll: {type(e).__name__}: {e}")

    def _inflight(self) -> list:
        with self._state_lock:
            return [dict(m) for m in self._orders.values()
                    if m["state"] == "new"]

    def _poll_account(self):
        try:
            view = self._account_view()
        except (PembertonFault, AdapterError) as e:
            log(f"account poll: {e}")
            return
        with self._state_lock:
            unchanged = self._account_seen == view
            self._account_seen = dict(view)
        if not unchanged:
            self.emit("account.updated", view)

    def _poll_bars(self):
        now = time.monotonic()
        with self._state_lock:
            due = [(k, dict(v)) for k, v in self._bars.items()
                   if v["next"] <= now]
            for k, _ in due:
                self._bars[k]["next"] = now + BAR_INTERVAL
        for (code, tf), state in due:
            seconds = TF_SECONDS[tf]
            try:
                rows = self._history_rows(code, TF_GRANULARITY[tf],
                                          int(seconds * 4 / 86400) + 2)
            except (PembertonFault, AdapterError) as e:
                log(f"bar poll {code} {tf}: {e}")
                continue
            if seconds == 14400:
                rows = self._fold(rows, seconds)
            if not state["primed"]:
                # A subscription is for what happens NEXT. The first poll
                # necessarily downloads a window of history (EOS has no "since
                # my last call" report), and replaying two days of hourly bars
                # as fresh events would be a subscription pretending to be a
                # backfill; bars.history is where a platform asks for that.
                with self._state_lock:
                    if (code, tf) in self._bars:
                        self._bars[(code, tf)].update(
                            {"primed": True,
                             "last": rows[-1][0] if rows else 0})
                continue
            for bar in rows:
                # CLOSED bars only (SPEC). History returns completed buckets,
                # and the clock check keeps a bucket that is still forming out
                # of the feed even if EOS ever changes its mind about that.
                if bar[0] <= state["last"] or bar[0] + seconds * 1000 > \
                        time.time() * 1000:
                    continue
                self.emit("bar", {"symbol": code, "timeframe": tf,
                                  "bar": bar})
                with self._state_lock:
                    if (code, tf) in self._bars:
                        self._bars[(code, tf)]["last"] = bar[0]

    def _bucket_loop(self):
        """The throttled lane: Quote and OrderStatus, which share one
        allowance per session (guide 8).

        One job at a time, because the client paces itself on the pool's
        buckets: the loop asks for work, the client waits for whichever
        session's 2-second window frees up first, and the cadence falls out of
        that instead of being timed here.
        """
        while not self._stop.is_set():
            job = self._next_bucket_job()
            if job is None:
                self._stop.wait(0.2)
                continue
            kind, key = job
            try:
                if kind == "snapshot":
                    self._poll_quote(key, force=True)
                elif kind == "quote":
                    self._poll_quote(key)
                else:
                    self._poll_order_status(key)
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log(f"bucket poll {kind} {key}: {type(e).__name__}: {e}")

    def _next_bucket_job(self):
        """What the next throttled call should be, in priority order.

        A subscription's first price jumps the queue (nobody should wait a
        cycle to see a market at all), then an order whose fate is still
        unknown, then the stalest quote. Each job marks itself before the call
        so a slow round trip cannot be picked twice.
        """
        now = time.monotonic()
        with self._state_lock:
            if self._snapshot_wanted:
                return "snapshot", self._snapshot_wanted.pop(0)
            for oid, meta in self._orders.items():
                if meta["state"] not in ("new", "resting"):
                    continue
                # An order whose fate is unknown is worth the allowance every
                # cycle; one already known to be resting is not. A working
                # limit can sit on the book for days, and polling it at the
                # venue's full rate would spend the entire quote budget on an
                # answer that is not going to change.
                wait = POLL_THROTTLE if meta["state"] == "new" else 15.0
                if (now - meta["placed_mono"] > STATUS_AFTER
                        and now - meta["last_status"] > wait):
                    meta["last_status"] = now
                    return "status", oid
            due = [s for s in self._quote_syms
                   if now - self._quote_last.get(s, 0.0) >= QUOTE_INTERVAL]
            if due:
                code = min(due, key=lambda s: self._quote_last.get(s, 0.0))
                self._quote_last[code] = now
                return "quote", code
        return None

    def _set_session(self, state: str, detail: str):
        if self._session_state == (state, detail):
            return
        self._session_state = (state, detail)
        self.emit("session.state", {"state": state, "detail": detail})


def main():
    # Line-buffered stdout: every protocol line must leave the process the
    # moment it is written, or a platform waits on a response sitting in a
    # pipe buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    PembertonAdapter().run()


if __name__ == "__main__":
    main()
