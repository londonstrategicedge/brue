#!/usr/bin/env python3
"""Brue Connect adapter for Meridian Markets (CFD / spread betting venue).

Meridian speaks OAuth2 client-credentials REST plus one Server-Sent-Events
stream. Everything it says about time is an ISO 8601 UTC string; everything
Brue Connect says about time is epoch milliseconds, so the conversion is done
once, at this boundary, in `_ms()` and nowhere else.

Shape of the thing:

  * `hello` answers COLD (SPEC 3.1): no token, no account read, no stream. All
    of that happens in `on_first_call`, so a platform's connection picker can
    learn the broker's name without costing it a login.
  * one background thread holds the SSE stream. Meridian ties a stream to the
    300 s life of the token that opened it and sends `event: goodbye` when it
    dies, so the thread re-tokens and reconnects, and re-reads orders and
    positions afterwards because Meridian explicitly does not replay.
  * the stream is treated as AT LEAST ONCE. The venue can deliver the same
    `execution` frame twice, so every broker fact goes through a per-order /
    per-deal state machine and a Brue event is emitted only on a state
    transition. Otherwise one broker quirk becomes two fills
    on the platform's book.
  * REST is the backstop for everything the stream is supposed to tell us.
    Meridian's own documentation nominates `GET /v1/orders` and
    `GET /v1/positions` as the source of truth after any gap, so orders in
    flight and closes in flight are also polled to a conclusion. A dropped
    frame therefore costs latency, never correctness.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# __file__-derived so the adapter runs the same in-repo and installed,
# exactly like the other venue adapters.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

BASE = os.environ.get("MERIDIAN_BASE_URL", "http://127.0.0.1:7631")
COMPANY = os.environ.get("MERIDIAN_COMPANY", "Brue Connect Integration")

# How this adapter authenticates (SPEC 1.6, capability `auth`):
#   "register" - machine-to-machine: self-register a client, then OAuth2
#                client_credentials. Today's default, used by conformance and
#                anything headless. The account is the CLIENT'S own.
#   "login"    - interactive: the USER logs in on Meridian's page
#                (authorization-code + PKCE, RFC 8252 loopback redirect) and
#                the adapter holds their tokens. The account is the USER'S.
# Two modes because they answer different questions ("whose account is
# this?"): a headless integration wants register, a human at a terminal wants
# login.
AUTH_MODE = os.environ.get("MERIDIAN_AUTH", "register")
# The public client identity Meridian knows this platform by. Public by
# definition (it rides in a browser URL); PKCE, not a secret, binds the code.
OAUTH_CLIENT = os.environ.get("MERIDIAN_OAUTH_CLIENT", "lse-terminal")
# How long one auth.begin attempt stays valid before the adapter gives up,
# closes its loopback listener and emits auth.completed ok=false.
AUTH_ATTEMPT_TTL = 300.0
# Which of the login's accounts this process deals on (SPEC 1.7). Set by the
# platform at spawn from the user's pick; empty means the login's primary,
# which is what a single-account integration always gets.
ACCOUNT_ID = os.environ.get("BRUE_ACCOUNT_ID", "").strip()

# Meridian: 120 requests per rolling 60 s per access token. Sit under it
# deliberately rather than discovering it with a 429: the suite's requests have
# 10 s deadlines and a Retry-After of tens of seconds would blow every one of
# them at once.
RATE_MAX = 100
RATE_WINDOW = 60.0

TF_TO_RESOLUTION = {"1m": "MINUTE", "5m": "MINUTE_5", "15m": "MINUTE_15",
                    "30m": "MINUTE_30", "1h": "HOUR", "4h": "HOUR_4",
                    "1d": "DAY"}
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# Meridian's asset types -> the SPEC's asset_class vocabulary. SPECIAL_FUTURE
# arrived with the Special Futures product and maps to the generic "future":
# the SPEC publishes no closed vocabulary for asset_class (the bundled
# adapters use fx / stock / index / metal / crypto), and a platform grouping a
# picker by it should file these with futures rather than under a product name
# only this venue uses. That they never expire is not said here: it is said in
# `expires_at: null`, which is the field the SPEC added for exactly that.
ASSET_CLASS = {"INDEX": "index", "EQUITY": "equity", "FX": "fx",
               "SPECIAL_FUTURE": "future"}

# Sessions, as published in `tradingHours` free text. Meridian ships trading
# hours as an ENGLISH SENTENCE ("24 hours, Sunday 21:05 UTC to Friday 21:00
# UTC"), which is not a calendar a platform can gate on, so the two shapes it
# actually uses are translated here into the SPEC's session windows. Parsed by
# recognising the sentence rather than by keying off the instrument type, so a
# new epic with either wording is covered and anything else falls through to an
# empty list (SPEC 3: empty means "no calendar published", NOT "closed").
SESSIONS_24_5 = [
    {"days": [7], "open": "21:05", "close": "24:00", "tz": "UTC"},
    {"days": [1, 2, 3, 4], "open": "00:00", "close": "24:00", "tz": "UTC"},
    {"days": [5], "open": "00:00", "close": "21:00", "tz": "UTC"},
]
SESSIONS_US_CASH = [
    {"days": [1, 2, 3, 4, 5], "open": "13:30", "close": "20:00", "tz": "UTC"},
]
# "24/7, no scheduled closures": the Special Futures. Published as one explicit
# seven-day window and NOT as an empty list, because SPEC 3 is emphatic that an
# empty `sessions` means "this broker publishes no calendar" rather than "this
# market is shut" - so always-open and unknown-calendar look identical unless a
# broker that really is always open says so, and only one of the two is safe
# for a platform to gate orders on.
SESSIONS_24_7 = [
    {"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00",
     "tz": "UTC"},
]

# How long the poller lets Meridian's own close confirmation arrive before it
# finishes a close from REST alone. The confirmation frame carries the EXACT
# realised P/L and lands about a second after the 202; anything the poller
# computes instead is arithmetic standing in for a broker number.
CLOSE_CONFIRM_GRACE = 3.0

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
                  r"(?:\.(\d{1,6}))?Z?$")


def _ms(iso) -> int:
    """Meridian ISO 8601 UTC string -> epoch milliseconds (SPEC 2).

    Hand-rolled rather than `datetime.fromisoformat`, which on some 3.x cannot
    read the trailing Z that Meridian puts on EVERY timestamp. Returns 0 for
    anything unparseable: a missing timestamp must not take down a fill.
    """
    if isinstance(iso, (int, float)):
        return int(iso)
    m = _ISO.match(str(iso or "").strip())
    if not m:
        return 0
    y, mo, d, h, mi, s, frac = m.groups()
    days = _days_from_civil(int(y), int(mo), int(d))
    secs = days * 86400 + int(h) * 3600 + int(mi) * 60 + int(s)
    ms = int((frac or "0").ljust(3, "0")[:3])
    return secs * 1000 + ms


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days since 1970-01-01 (Howard Hinnant's civil_from_days, inverted).

    No timezone database, no DST, no locale: Meridian is UTC-only and so is the
    protocol, so the whole conversion is integer arithmetic.
    """
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _iso(ms: int) -> str:
    """Epoch ms -> the ISO 8601 UTC string Meridian expects back."""
    secs, rem = divmod(int(ms), 1000)
    t = time.gmtime(secs)
    return (f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T"
            f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}.{rem:03d}Z")


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def log(msg: str):
    """stderr, never stdout: stdout is the protocol."""
    print(f"[meridian] {msg}", file=sys.stderr, flush=True)


class MeridianError(Exception):
    """An RFC 7807 problem+json response from Meridian."""

    def __init__(self, status: int, body):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.code = self.body.get("meridianCode") or f"HTTP-{status}"
        self.detail = (self.body.get("detail") or self.body.get("title")
                       or str(body)[:200])
        super().__init__(f"{self.code} ({status}): {self.detail}")


# Meridian's error codes -> the SPEC section 6 closed set. M-1020 is
# deliberately one code for two conditions ("Rather than mint a new code ...
# both conditions are M-1020; the HTTP status tells them apart"), which is
# exactly the distinction between `not_found` and `not_resting`, so this table
# is keyed on the PAIR the document tells you to switch on.
ERROR_MAP = {
    ("M-1001", None): ("unauthorized", False),
    ("M-1002", None): ("unauthorized", True),
    ("M-1005", None): ("insufficient_margin", False),
    ("M-1010", None): ("market_closed", False),
    ("M-1011", None): ("invalid_qty", False),
    ("M-1012", None): ("unknown_symbol", False),
    ("M-1015", None): ("invalid_expiry", False),
    ("M-1020", 404): ("not_found", False),
    ("M-1020", 409): ("not_resting", False),
    ("M-1025", None): ("unsupported", False),
    ("M-1030", None): ("rate_limited", True),
    ("M-1099", None): ("internal", True),
}


def to_adapter_error(e: MeridianError) -> AdapterError:
    hit = ERROR_MAP.get((e.code, e.status)) or ERROR_MAP.get((e.code, None))
    if hit is None:
        # An unknown venue code keeps its identity as an x_ extension rather
        # than being flattened to `internal`, which would destroy the only
        # information the platform had (SPEC 6, and protocol.res_error does the
        # same thing on the wire).
        code, retry = "x_meridian_" + e.code.lower().replace("-", "_"), False
    else:
        code, retry = hit
    return AdapterError(code, f"{e.code}: {e.detail}", retry)


class AuthRequired(AdapterError):
    """No usable credentials, and only the USER can fix it (SPEC 1.6).

    A subclass of AdapterError so the base dispatch turns it into the
    `auth_required` protocol error wherever it surfaces: on_first_call, a
    handler, anywhere. Retryable, because the retry that matters (after the
    user finishes auth.begin) genuinely will succeed.
    """

    def __init__(self, detail="Meridian needs the user to log in; "
                              "call auth.begin and open the returned url"):
        super().__init__("auth_required", detail, True)


class MeridianClient:
    """REST + OAuth2, with the token lifecycle and the rate limit inside.

    One place owns the 300 s token, so no caller has to think about expiry, and
    one place owns the request budget, so no caller can spend someone else's.
    In "login" mode the long-lived credential is the USER'S refresh token
    (persisted in the profile's state dir); in "register" mode it is the
    client secret from self-registration. Callers never see the difference.
    """

    def __init__(self, base: str, cred_path: str, auth_mode: str = "register"):
        self.base = base.rstrip("/")
        self.cred_path = cred_path
        self.auth_mode = auth_mode
        self._creds = None
        self._token = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        self._calls: list[float] = []
        self._ctx = ssl.create_default_context()
        # login mode only: {"refreshToken": ..., "scope": ...}, same file
        # discipline as credentials (0600, atomic replace, profile-private).
        self._tokens = None
        self._tokens_loaded = False

    # -- login mode: the user's tokens ---------------------------------
    def _token_path(self) -> str:
        return os.path.join(os.path.dirname(self.cred_path) or ".",
                            "tokens.json")

    def load_tokens(self) -> dict | None:
        if not self._tokens_loaded:
            self._tokens_loaded = True
            try:
                with open(self._token_path()) as fh:
                    t = json.load(fh)
                if t.get("refreshToken"):
                    self._tokens = t
            except (OSError, json.JSONDecodeError):
                self._tokens = None
        return self._tokens

    def store_tokens(self, tokens: dict | None):
        """Persist (or on None, destroy) the user's refresh token."""
        self._tokens = tokens
        self._tokens_loaded = True
        path = self._token_path()
        try:
            if tokens is None:
                if os.path.exists(path):
                    os.remove(path)
                return
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(tokens, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            log(f"could not persist tokens to {path}: {e}")

    def adopt_access(self, access: str, expires_in: float):
        """A token just minted by the login flow becomes THE session token,
        so the first calls after login do not spend a refresh round trip."""
        with self._lock:
            self._token = access
            self._token_exp = time.time() + expires_in

    # -- credentials --------------------------------------------------
    def credentials(self) -> dict:
        """Register once, then reuse forever.

        The account is provisioned with the client ("One trading account is
        provisioned per client and is not chosen by the caller"), so a new
        registration is a NEW EMPTY ACCOUNT. Registering on every start would
        silently orphan yesterday's positions, which is also why the file lives
        in the profile's own state directory: two profiles wrapping this
        adapter must not share an account.
        """
        if self._creds:
            return self._creds
        if os.path.exists(self.cred_path):
            try:
                with open(self.cred_path) as fh:
                    c = json.load(fh)
                if c.get("clientId") and c.get("clientSecret"):
                    self._creds = c
                    return c
            except (OSError, json.JSONDecodeError) as e:
                log(f"credential file unreadable ({e}); registering afresh")
        c = self._raw("POST", "/oauth/register", body={"company": COMPANY})
        try:
            os.makedirs(os.path.dirname(self.cred_path) or ".", exist_ok=True)
            tmp = self.cred_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(c, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cred_path)
        except OSError as e:
            log(f"could not persist credentials to {self.cred_path}: {e}")
        log(f"registered client {c.get('clientId')} -> account "
            f"{c.get('accountId')}")
        self._creds = c
        return c

    def token(self, force: bool = False) -> str:
        with self._lock:
            # 30 s of headroom: a token that expires mid-request is a 401 and a
            # retry, and the document is explicit that expiry is hard.
            if not force and self._token and time.time() < self._token_exp - 30:
                return self._token
            if self.auth_mode == "login":
                t = self.load_tokens()
                if not t:
                    raise AuthRequired()
                try:
                    r = self._raw("POST", "/oauth/token", form={
                        "grant_type": "refresh_token",
                        "refresh_token": t["refreshToken"],
                        "client_id": OAUTH_CLIENT})
                except MeridianError as e:
                    if e.status in (400, 401):
                        # The refresh token is dead at the venue (revoked,
                        # or the venue restarted and forgot it). Holding a
                        # credential the broker refuses IS holding nothing:
                        # destroy it so auth.status tells the truth, and
                        # send the user back through the login.
                        self.store_tokens(None)
                        raise AuthRequired("Meridian no longer accepts the "
                                           "stored login; log in again "
                                           "(auth.begin)") from None
                    raise
            else:
                c = self.credentials()
                r = self._raw("POST", "/oauth/token", form={
                    "grant_type": "client_credentials",
                    "client_id": c["clientId"],
                    "client_secret": c["clientSecret"]})
            self._token = r["accessToken"]
            self._token_exp = time.time() + _num(r.get("expiresIn"), 300)
            return self._token

    # -- the wire -----------------------------------------------------
    def _budget(self):
        """Block until this call fits inside Meridian's rolling window."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < RATE_WINDOW]
                if len(self._calls) < RATE_MAX:
                    self._calls.append(now)
                    return
                wait = RATE_WINDOW - (now - self._calls[0]) + 0.01
            log(f"local rate budget exhausted, holding {wait:.1f}s")
            time.sleep(min(wait, 5.0))

    def _raw(self, method: str, path: str, body=None, form=None, token=None):
        url = self.base + path
        data, headers = None, {"Accept": "application/json"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
            # One process, one account: name it on every request rather than
            # relying on the venue's default, so this process can never drift
            # onto a different account than the one it was bound to.
            if ACCOUNT_ID:
                headers["X-MRD-ACCOUNT"] = ACCOUNT_ID
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=20,
                                        context=self._ctx) as r:
                raw = r.read().decode() or "{}"
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"detail": raw[:200]}
            if e.code == 429:
                parsed["retryAfter"] = _num(e.headers.get("Retry-After"), 1.0)
            raise MeridianError(e.code, parsed) from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise MeridianError(0, {"detail": f"{type(e).__name__}: {e}",
                                    "meridianCode": "M-1099"}) from None

    def call(self, method: str, path: str, body=None, retries: int = 2):
        """An authenticated /v1 call, with expiry and 429 handled once here."""
        for attempt in range(retries + 1):
            self._budget()
            try:
                return self._raw(method, path, body=body, token=self.token())
            except MeridianError as e:
                last = attempt >= retries
                if e.code == "M-1002" and not last:
                    self.token(force=True)   # hard 300 s expiry, no refresh
                    continue
                if e.code == "M-1030" and not last:
                    # Honour Retry-After, but never sleep past the platform's
                    # own deadline; a shorter nap and a second try beats a
                    # timeout with no answer at all.
                    time.sleep(min(_num(e.body.get("retryAfter"), 1.0), 5.0))
                    continue
                if e.status == 0 and not last:
                    time.sleep(0.4)
                    continue
                raise


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """The adapter's own loopback redirect target (RFC 8252).

    One real landing ends the attempt, one way or the other. Anything that is
    NOT a valid landing (a favicon probe, a stale tab, a forged callback with
    the wrong state) is refused politely WITHOUT ending the attempt: some
    other process knocking on the port must not be able to cancel the user's
    login mid-flight."""

    def do_GET(self):
        attempt = self.server.attempt
        adapter = attempt["adapter"]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback" or attempt["done"].is_set():
            self._page(404, "Nothing here.")
            return
        q = {k: v[0]
             for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if q.get("state") != attempt["state"]:
            self._page(400, "This login link does not match the attempt the "
                            "terminal started, so nothing was authorized. "
                            "Go back to the terminal and try again.")
            return
        if "code" not in q:
            reason = (q.get("error_description") or q.get("error")
                      or "the broker sent no authorization code")
            self._page(400, "The login was not completed.")
            adapter._finish_auth(attempt, False, reason)
            return
        ok, why = adapter._auth_exchange(attempt, q["code"])
        if ok:
            self._page(200, "Login complete. You can close this tab and "
                            "return to the terminal.")
            adapter._finish_auth(attempt, True)
        else:
            self._page(502, "The login could not be finished: " + why)
            adapter._finish_auth(attempt, False, why)

    def _page(self, status: int, text: str):
        body = ("<!doctype html><meta charset=utf-8><title>LSE Terminal</title>"
                "<body style='font-family:sans-serif;max-width:420px;"
                "margin:80px auto'><h3>LSE Terminal</h3>"
                f"<p>{text}</p></body>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        # stderr is the adapter's voice (SPEC: stdout is the protocol);
        # per-request access lines from the loopback are noise there.
        pass


class MeridianAdapter(Adapter):
    name = "meridian"
    version = "1.0"
    broker = "Meridian Markets"
    # Truthful (SPEC 8.2): the venue's own documentation opens with "Meridian
    # Markets is a simulated venue for integration testing. All prices are
    # synthetic. No real money is involved."
    mode = "paper"
    # "Every Meridian demo account is denominated in GBP" (doc section 6);
    # re-checked against the account on first call, and complained about on
    # stderr if the broker ever disagrees.
    account_currency = "GBP"
    # Not stated in the documentation, but Meridian NETS: a same-side order
    # adds to the deal at the average level, an opposing order reduces it and
    # confirms with `reason: "NETTED"`, and an opposing order larger than the
    # deal closes it and opens a new one the other way under a new dealId.
    # One position per symbol is netting (SPEC 3).
    account_model = "netting"

    def __init__(self, out=None):
        super().__init__(out)
        state = os.environ.get("BRUE_BROKER_STATE") or os.path.expanduser(
            "~/.brue-connect/state/meridian")
        self.api = MeridianClient(BASE, os.path.join(state, "credentials.json"),
                                  auth_mode=AUTH_MODE)
        self._accounts_cache = os.path.join(state, "accounts.json")
        # `mode` is the mode of the account this process is BOUND to, not of
        # the venue (SPEC 1.7). hello must answer cold, so the account's
        # live/demo classification is read from what the last accounts.list
        # cached rather than by opening a session here. An account we have
        # never seen classified is treated as LIVE: unknown mode takes the
        # careful path (SPEC 3), which costs an arming click and never costs
        # real money.
        if ACCOUNT_ID:
            try:
                with open(self._accounts_cache) as fh:
                    self.mode = json.load(fh).get(ACCOUNT_ID) or "live"
            except (OSError, json.JSONDecodeError):
                self.mode = "live"
        self._auth_attempt = None   # the live auth.begin, at most one
        self._state_lock = threading.RLock()
        self._instruments: dict[str, dict] = {}      # epic -> Meridian spec
        self._orders: dict[str, dict] = {}           # order_id -> our view
        self._by_client: dict[str, str] = {}         # client_id -> order_id
        self._deal_client: dict[str, str] = {}       # dealId -> client_id
        self._deal_seen: dict[str, dict] = {}        # dealId -> last position
        self._closing: dict[str, dict] = {}          # dealId -> close request
        self._epics: list[str] = []                  # quote subscriptions
        self._bars: dict[tuple, int] = {}            # (epic, tf) -> last bar t
        self._stream_gen = 0
        self._stream_resp = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._session_state = None

    # == handshake =====================================================
    def capabilities(self) -> dict:
        caps = self._capabilities()
        if AUTH_MODE == "login":
            # A machine client registered through /oauth/register owns exactly
            # one account, so the chooser only exists on the interactive path.
            caps["accounts"] = {"multi": True}
            # Declared only when this PROFILE actually authenticates a human;
            # a register-mode profile of the same adapter has no login to
            # offer and declaring one would send platforms chasing a browser
            # flow that cannot happen (SPEC 3: absent means absent).
            caps["auth"] = {"interactive": True}
        return caps

    def _capabilities(self) -> dict:
        return {
            "orders": {
                "market": True,
                # "There are no stop orders and no stop loss / take profit
                # attachment fields in this API version" (doc section 10). No
                # brackets, so nothing to modify or trail either: declared
                # false so the platform refuses the feature loudly instead of
                # degrading silently (SPEC 3).
                "brackets": False,
                "modify": False,
                "trailing": False,
                "partial_close": True,      # DELETE /v1/positions/{id} {size}
                "pending": True,            # LIMIT orders rest on the book
                "pending_types": ["limit"],
                "expiry": ["gtc", "gtd"],   # GOOD_TILL_CANCELLED / _DATE
                # Meridian checks margin when the order is POSTed (a 50-lot
                # UK.100 limit is refused M-1005) but holds nothing: an
                # accepted resting order leaves marginUsed at 0.00. The SPEC's
                # boolean has no value for "validated on entry, not reserved",
                # so it is false: the consequence the SPEC cares about (an
                # order accepted comfortably can still die at trigger) is real
                # here.
                "reserves_margin": False,
                "oco": False,
                "close_by": False,
            },
            "data": {
                "quotes": True, "bars": True, "history": True,
                # No non-TRADE rows exist in Meridian's ledger and it documents
                # no deposit/dividend/adjustment kinds, so a balance that moves
                # without a fill genuinely has no explanation available.
                "operations": False,
                # Meridian states no cap; the whole tradable universe is nine
                # epics and the stream takes them in one query string. Absent
                # means "the broker states no limit" (SPEC 3), which is true.
            },
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # 120 per rolling 60 s per access token (doc section 8), expressed in
        # the units the handshake uses.
        return {"requests_per_second": 2}

    def broker_info(self) -> dict:
        # The broker's own words only. Meridian's document has no "about us"
        # block, so there is no country, no licence and no website to quote,
        # and inventing them is exactly what SPEC 3.1 says this field is not
        # for.
        return {"display_name": "Meridian Markets",
                "tagline": "CFD and spread betting execution venue",
                "mark": "MM",
                # The venue's own product names.
                "asset_classes": ["Indices", "Equities", "FX",
                                  "Special Futures"]}

    # == session =======================================================
    def on_first_call(self):
        """Everything that costs the broker something happens here, not in
        `hello` (SPEC 3.1): register/token, read the catalog, open the stream."""
        self.api.token()
        self._load_instruments()
        acct = self._account_raw()
        if acct.get("currency") and acct["currency"] != self.account_currency:
            # Cannot be corrected after the fact: `hello` has already gone out.
            # Loud on stderr is the only honest move left.
            log(f"WARNING: account currency is {acct['currency']!r}, but the "
                f"handshake declared {self.account_currency!r}")
        self._stop.clear()
        threading.Thread(target=self._stream_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._sync_orders()
        self._sync_positions()

    def on_shutdown(self):
        self._stop.set()
        self._wake.set()
        with self._state_lock:
            resp, self._stream_resp = self._stream_resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:       # noqa: BLE001 - shutting down anyway
                pass

    # == interactive login (SPEC 1.6, capability auth) =================
    # The platform's whole part is: show auth.begin's url, wait for the
    # auth.completed event, retry the connect. Everything broker-specific
    # (PKCE, the loopback listener, the code exchange, where tokens live)
    # stays in here, which is the reason OAuth belongs in an adapter and not
    # in the platform: the NEXT broker's login can look completely different
    # without the platform changing at all.

    def _auth_only_in_login_mode(self):
        if AUTH_MODE != "login":
            raise AdapterError(
                "unsupported",
                "this profile authenticates with machine credentials; "
                "there is no interactive login (capability auth undeclared)")

    def handle_accounts_list(self, params):
        """Every account this login can deal on (SPEC 1.7).

        Answerable without an open session, because it is the question the
        user answers BEFORE there is a session: which account should this
        process be? Only the broker's own words are relayed; nothing here is
        invented, so an account the venue labels with nothing but an id
        reaches the platform as an id and is shown as one.
        """
        data = self.api.call("GET", "/v1/accounts")
        out = []
        for a in data.get("accounts") or []:
            row = {"id": a.get("accountId") or ""}
            if a.get("nickname"):
                row["label"] = a["nickname"]
            if a.get("currency"):
                row["currency"] = a["currency"]
            if a.get("accountType"):
                row["type"] = str(a["accountType"]).lower()
            if a.get("balance") is not None:
                row["balance"] = _num(a.get("balance"))
            # The venue classifies each account LIVE or DEMO; that is the
            # SPEC's `mode` for this account, and it is what decides whether
            # the platform demands arming before it will deal.
            row["mode"] = "live" if str(a.get("kind", "")).upper() == "LIVE" else "paper"
            row["selected"] = bool(
                a.get("accountId") == ACCOUNT_ID if ACCOUNT_ID
                else a.get("selected"))
            out.append(row)
        # Cached so the NEXT process bound to one of these can declare its
        # mode in a cold hello, before any session exists.
        try:
            tmp = self._accounts_cache + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({r["id"]: r["mode"] for r in out}, fh)
            os.replace(tmp, self._accounts_cache)
        except OSError as e:
            log(f"could not cache the account list: {e}")
        return {"accounts": out}

    def handle_auth_status(self, params):
        self._auth_only_in_login_mode()
        try:
            self.api.token()
            return {"authenticated": True,
                    "detail": "logged in; the stored session is accepted"}
        except AuthRequired as e:
            return {"authenticated": False, "detail": str(e)}
        except MeridianError as e:
            # Venue unreachable is not "logged out"; saying so would send the
            # user through a pointless login against a broker that is down.
            raise to_adapter_error(e)

    def handle_auth_logout(self, params):
        self._auth_only_in_login_mode()
        self._abandon_auth_attempt()
        self.api.store_tokens(None)
        self.api.adopt_access(None, 0)
        return {}

    def handle_auth_begin(self, params):
        self._auth_only_in_login_mode()
        self._abandon_auth_attempt()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(16)
        # Port 0: the OS hands out a free ephemeral port, which is exactly
        # what RFC 8252 says a native client should do, and what the venue's
        # loopback-only redirect rule exists to allow.
        server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        server.timeout = 1.0
        redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
        attempt = {
            "server": server, "verifier": verifier, "state": state,
            "redirect": redirect, "deadline": time.time() + AUTH_ATTEMPT_TTL,
            "done": threading.Event(), "cancelled": False, "adapter": self,
        }
        server.attempt = attempt
        with self._state_lock:
            self._auth_attempt = attempt
        threading.Thread(target=self._auth_listen, args=(attempt,),
                         daemon=True).start()
        url = f"{BASE}/oauth/authorize?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": OAUTH_CLIENT,
            "redirect_uri": redirect, "scope": "trade read", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        log(f"auth.begin: listening on {redirect}")
        return {"url": url, "expires_in": int(AUTH_ATTEMPT_TTL)}

    def _abandon_auth_attempt(self):
        """A newer auth.begin (or a logout) supersedes the pending one.
        Abandoned attempts emit NOTHING: the platform is now waiting on the
        new attempt, and an ok=false from the old one would read as that new
        login failing."""
        with self._state_lock:
            attempt, self._auth_attempt = self._auth_attempt, None
        if attempt is not None and not attempt["done"].is_set():
            attempt["cancelled"] = True
            attempt["done"].set()

    def _auth_listen(self, attempt):
        """Serve the loopback until the browser lands once, the attempt is
        superseded, or the clock runs out. handle_request with a 1 s timeout
        rather than serve_forever, so this thread also gets to notice the
        deadline and shutdown() can never deadlock against a handler."""
        server = attempt["server"]
        try:
            while not attempt["done"].is_set():
                if time.time() >= attempt["deadline"]:
                    self._finish_auth(attempt, False,
                                      "login timed out; nobody completed the "
                                      "broker's page in time")
                    break
                server.handle_request()
        finally:
            try:
                server.server_close()
            except OSError:
                pass

    def _finish_auth(self, attempt, ok: bool, reason: str = ""):
        if attempt["done"].is_set():
            return
        attempt["done"].set()
        with self._state_lock:
            if self._auth_attempt is attempt:
                self._auth_attempt = None
        if attempt["cancelled"]:
            return
        data = {"ok": ok}
        if not ok:
            data["reason"] = reason
            log(f"auth failed: {reason}")
        else:
            log("auth completed: user logged in, tokens stored")
        self.emit("auth.completed", data)

    def _auth_exchange(self, attempt, code: str) -> tuple[bool, str]:
        """The back-channel half: one-time code + PKCE verifier -> tokens.
        Runs inside the loopback request so the browser tab can be told the
        truth about whether the login actually finished."""
        try:
            r = self.api._raw("POST", "/oauth/token", form={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": attempt["redirect"],
                "client_id": OAUTH_CLIENT,
                "code_verifier": attempt["verifier"]})
        except MeridianError as e:
            return False, f"token exchange refused: {e.detail}"
        if not r.get("refreshToken") or not r.get("accessToken"):
            return False, "venue returned no tokens"
        self.api.store_tokens({"refreshToken": r["refreshToken"],
                               "scope": r.get("scope", "")})
        self.api.adopt_access(r["accessToken"], _num(r.get("expiresIn"), 300))
        return True, ""

    # == catalog =======================================================
    def _load_instruments(self):
        data = self.api.call("GET", "/v1/instruments")
        with self._state_lock:
            self._instruments = {i["epic"]: i
                                 for i in data.get("instruments", [])}

    @staticmethod
    def _sessions_for(inst: dict) -> list:
        """`tradingHours` free text -> SPEC session windows."""
        text = (inst.get("tradingHours") or "").lower()
        if "24/7" in text or "no scheduled closures" in text:
            return [dict(s) for s in SESSIONS_24_7]
        if "sunday" in text and "friday" in text:
            return [dict(s) for s in SESSIONS_24_5]
        if "13:30" in text and "20:00" in text:
            return [dict(s) for s in SESSIONS_US_CASH]
        # Unrecognised wording: publish NO calendar rather than a guessed one.
        # SPEC 3 is explicit that an empty list means "no calendar", not
        # "closed", and the adapter stays the final judge either way.
        log(f"unrecognised tradingHours for {inst.get('epic')}: "
            f"{inst.get('tradingHours')!r}; publishing no calendar")
        return []

    def _spec(self, inst: dict) -> dict:
        epic = inst["epic"]
        digits = int(_num(inst.get("pricePrecision"), 2))
        tick = round(10.0 ** -digits, max(digits, 1))
        contract = _num(inst.get("contractSize"), 1.0)
        comm = inst.get("commission") or {}
        model = (comm.get("model") or "NONE").upper()
        fees = {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
                "min": 0.0}
        if model == "PER_SHARE":
            fees = {"commission_per_lot": 0.0, "mode": "per_share",
                    "rate": _num(comm.get("rate")),
                    "min": _num(comm.get("minimum"))}
        elif model == "PER_LOT":
            rate = _num(comm.get("rate"))
            fees = {"commission_per_lot": rate, "mode": "per_lot",
                    "rate": rate, "min": _num(comm.get("minimum"))}
        elif model == "PCT_NOTIONAL":
            fees = {"commission_per_lot": 0.0, "mode": "pct_notional",
                    "rate": _num(comm.get("rate")),
                    "min": _num(comm.get("minimum"))}
        base = None
        if (inst.get("type") or "").upper() == "FX":
            # "FX.EURUSD" -> EUR against USD. The pair is the only place
            # Meridian encodes a base currency at all.
            tail = epic.split(".")[-1]
            if len(tail) == 6:
                base = tail[:3]
        return {
            "symbol": epic,
            "name": inst.get("name") or epic,
            "asset_class": ASSET_CLASS.get((inst.get("type") or "").upper(),
                                           "cfd"),
            "base_ccy": base,
            "quote_ccy": inst.get("currency"),
            "digits": digits,
            "tick_size": tick,
            # value of one tick per contract, in quote_ccy: the SPEC's own
            # default derivation, which is right for every Meridian product
            # (they are all quoted in their own contract currency).
            "tick_value": round(contract * tick, 10),
            # The same tick, in the ACCOUNT's money, at Meridian's own rate
            # (SPEC 4). Relayed, never computed here: the platform must not
            # cross a rate of its own, and this adapter is not a better
            # place to invent one than the platform is.
            "tick_value_account": _num(inst.get("tickValueAccount"),
                                       round(contract * tick, 10)),
            "lot_step": _num(inst.get("sizeStep"), 1.0),
            "min_qty": _num(inst.get("minSize"), 1.0),
            "max_qty": _num(inst.get("maxSize"), 0.0),
            "min_notional": 0,
            "contract_size": contract,
            # THE percent-form quirk (doc section 3): marginFactorPercent is a
            # PERCENTAGE. 5.0 means 5%, so the SPEC's fractional margin_rate is
            # /100 exactly once.
            "margin_rate": _num(inst.get("marginFactorPercent")) / 100.0,
            "margin_tiers": None,
            "sessions": self._sessions_for(inst),
            "holidays": [],          # Meridian publishes no holiday calendar
            "early_closes": [],
            "tradable": True,
            "shortable": bool(inst.get("shortable", True)),
            "trade_mode": "full",
            "stops_level_points": 0,   # no stop distance: no stops at all
            "fees": fees,
            # Meridian reports no financing anywhere in the API. 0 with
            # swap_mode "none" says "not charged / not published" rather than
            # inventing a number in unknown units.
            "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
            "expires_at": None, "successor": None, "dividend": None,
        }

    def handle_catalog_list(self, params):
        self._load_instruments()
        with self._state_lock:
            insts = list(self._instruments.values())
        return {"instruments": [self._spec(i) for i in insts]}

    def _instrument(self, symbol: str) -> dict:
        with self._state_lock:
            inst = self._instruments.get(symbol)
        if inst is None:
            self._load_instruments()          # catalogs change; re-read once
            with self._state_lock:
                inst = self._instruments.get(symbol)
        if inst is None:
            raise AdapterError("unknown_symbol", f"no such epic {symbol!r}")
        return inst

    # == account =======================================================
    def _account_raw(self) -> dict:
        data = self.api.call("GET", "/v1/accounts")
        accounts = data.get("accounts") or []
        if not accounts:
            raise AdapterError("internal", "Meridian returned no account")
        # "One adapter process serves ONE account" (SPEC 3). A login may own
        # several, so take the one this process was bound to; the venue marks
        # it `selected` because the request named it. Falling back to the
        # first row keeps a single-account client working unchanged.
        for a in accounts:
            if ACCOUNT_ID and a.get("accountId") == ACCOUNT_ID:
                return a
            if not ACCOUNT_ID and a.get("selected"):
                return a
        return accounts[0]

    @staticmethod
    def _account_view(a: dict) -> dict:
        return {"balance": _num(a.get("balance")),
                "equity": _num(a.get("equity")),
                "margin_used": _num(a.get("marginUsed")),
                "margin_free": _num(a.get("marginAvailable")),
                "currency": a.get("currency") or "GBP",
                # Which account these figures belong to (SPEC 4). A login with
                # five accounts must never leave the user guessing from the
                # balance which one is on screen.
                "account_id": a.get("accountId") or "",
                "label": a.get("nickname") or ""}

    def handle_account_get(self, params):
        return self._account_view(self._account_raw())

    # == positions =====================================================
    def _position_view(self, p: dict) -> dict:
        deal = str(p.get("dealId"))
        with self._state_lock:
            client_id = self._deal_client.get(deal)
        out = {
            "position_id": deal,
            "symbol": p.get("epic"),
            "side": "buy" if (p.get("direction") or "").upper() == "BUY"
                    else "sell",
            "qty": _num(p.get("size")),
            "avg_price": _num(p.get("openLevel")),
            "sl": None, "tp": None, "trail_points": 0,
            "unrealized_pnl": _num(p.get("profitLoss")),
            # Meridian reports one `profitLoss` and never splits financing or
            # commission out of it. SPEC: send 0 and leave the combined figure
            # in unrealized_pnl rather than inventing a split.
            "swap": 0.0, "commission": 0.0,
            "opened_at": _ms(p.get("createdDateUTC")),
            "from_order": None,
        }
        if client_id:
            out["client_id"] = client_id
        return out

    def _positions_raw(self) -> list:
        data = self.api.call("GET", "/v1/positions")
        rows = data.get("positions") or []
        with self._state_lock:
            self._deal_seen = {str(p.get("dealId")): p for p in rows}
        return rows

    def handle_positions_list(self, params):
        return {"positions": [self._position_view(p)
                              for p in self._positions_raw()]}

    def _sync_positions(self):
        try:
            self._positions_raw()
        except (MeridianError, AdapterError) as e:
            log(f"position sync failed: {e}")

    # == orders ========================================================
    def _validate_qty(self, inst: dict, qty) -> float:
        """Size checked against the catalog before the round trip.

        Meridian answers off-step, below-min and above-max ALL with M-1011
        "Invalid deal size", the same code it uses for a bad direction and a
        missing limit level, so its answer cannot be mapped back to a precise
        Brue code. Checking here means the platform gets `invalid_qty` for a
        size problem and only a size problem.
        """
        q = _num(qty, -1.0)
        step = _num(inst.get("sizeStep"), 0.0)
        lo = _num(inst.get("minSize"), 0.0)
        hi = _num(inst.get("maxSize"), 0.0)
        if q <= 0:
            raise AdapterError("invalid_qty", f"qty must be positive, got {qty!r}")
        if lo and q < lo - 1e-9:
            raise AdapterError("invalid_qty", f"qty {q} below minSize {lo}")
        if hi and q > hi + 1e-9:
            raise AdapterError("invalid_qty", f"qty {q} above maxSize {hi}")
        if step > 0:
            units = q / step
            if abs(units - round(units)) > 1e-6:
                raise AdapterError(
                    "invalid_qty", f"qty {q} is not a multiple of sizeStep {step}")
            q = round(units) * step
        return round(q, 8)

    def _commission_for(self, inst: dict, qty: float) -> float:
        """Fees for a fill, from the published schedule.

        The FALLBACK, not the first choice: a filled `execution` frame
        carries its own `fee` (see `_absorb_order`), and the broker's own
        number beats any recomputation of it. This stays for the frame that
        arrives without the field and for the order reconstructed from REST
        after a gap, and it is what the reported fee is cross-checked against.
        Negative, because a commission costs the holder.
        """
        comm = inst.get("commission") or {}
        model = (comm.get("model") or "NONE").upper()
        if model == "NONE":
            return 0.0
        rate, minimum = _num(comm.get("rate")), _num(comm.get("minimum"))
        if model == "PER_SHARE":
            charge = rate * qty * _num(inst.get("contractSize"), 1.0)
        elif model == "PER_LOT":
            charge = rate * qty
        else:
            return 0.0
        return -round(max(charge, minimum), 2)

    def handle_order_place(self, params):
        client_id = params.get("client_id")
        if not client_id:
            raise AdapterError("invalid_qty", "client_id is required")
        otype = (params.get("type") or "market").lower()
        symbol = params.get("symbol")
        side = (params.get("side") or "").lower()
        # The signature is everything that decides what gets DEALT: a replay
        # carrying different terms must be refused (SPEC 6), and Meridian will
        # not refuse it for us (a reused dealReference returns the original
        # order even when the epic, direction, size and level all differ).
        sig = (symbol, side, _num(params.get("qty"), -1), otype,
               params.get("price"), params.get("stop_price"),
               params.get("expiry"), params.get("expiry_at"))
        prior = self.idempotent_check(client_id, sig)
        if prior is not None:
            return prior

        if side not in ("buy", "sell"):
            raise AdapterError("invalid_qty", f"side must be buy or sell, "
                                              f"got {params.get('side')!r}")
        inst = self._instrument(symbol)
        qty = self._validate_qty(inst, params.get("qty"))
        for unsupported, why in (("sl", "stop loss"), ("tp", "take profit"),
                                 ("trail_points", "trailing stop")):
            if params.get(unsupported) is not None:
                raise AdapterError(
                    "unsupported",
                    f"Meridian has no {why} attachment in this API version")
        if otype in ("stop", "stop_limit"):
            raise AdapterError("unsupported",
                               f"Meridian supports MARKET and LIMIT only, "
                               f"not {otype!r}")
        if otype not in ("market", "limit"):
            raise AdapterError("unsupported", f"unknown order type {otype!r}")

        body = {"epic": symbol, "direction": side.upper(), "size": qty,
                "orderType": otype.upper(), "dealReference": client_id}
        if otype == "limit":
            price = params.get("price")
            if price is None:
                raise AdapterError("x_invalid_price",
                                   "a limit order needs `price`")
            body["level"] = round(_num(price),
                                  int(_num(inst.get("pricePrecision"), 5)))
        expiry = (params.get("expiry") or "gtc").lower()
        if expiry == "gtd":
            when = params.get("expiry_at")
            if not when:
                raise AdapterError("invalid_expiry",
                                   "expiry 'gtd' needs expiry_at")
            body["timeInForce"] = "GOOD_TILL_DATE"
            body["goodTillDate"] = _iso(int(_num(when)))
        elif expiry == "day":
            # Meridian honours GOOD_TILL_CANCELLED and GOOD_TILL_DATE only.
            # SPEC: a `day` expiry the broker does not honour is invalid_expiry
            # (declared in capabilities.orders.expiry, so a platform should
            # never send it).
            raise AdapterError("invalid_expiry",
                               "Meridian honours gtc and gtd only")
        elif expiry != "gtc":
            raise AdapterError("invalid_expiry", f"unknown expiry {expiry!r}")

        try:
            r = self.api.call("POST", "/v1/orders", body=body)
        except MeridianError as e:
            err = to_adapter_error(e)
            # SPEC 8.4: no mutating call may be silently dropped; a synchronous
            # refusal is a terminal outcome and is announced as one, so a
            # platform watching only the event stream still sees the order end.
            self.emit("order.rejected", {"client_id": client_id,
                                         "code": err.code, "message": str(err)})
            raise err

        order_id = str(r.get("orderId"))
        status = (r.get("status") or "").upper()
        with self._state_lock:
            self._orders[order_id] = {
                "order_id": order_id, "client_id": client_id, "epic": symbol,
                "side": side, "qty": qty, "type": otype,
                "price": body.get("level"), "state": "new",
                "placed_at": int(time.time() * 1000),
                "expiry": expiry, "expires_at": _ms(body.get("goodTillDate"))
                if body.get("goodTillDate") else None,
                "deadline": time.time() + 30.0, "degraded": False,
            }
            self._by_client[client_id] = order_id
        self.emit("order.accepted", {"client_id": client_id,
                                     "order_id": order_id})
        result = {"order_id": order_id,
                  "status": "resting" if otype == "limit" else "accepted"}
        # The POST response is NOT fed to the state machine: it carries only
        # dealReference/orderId/status, so absorbing it would emit an
        # `order.placed` with no epic and no level. The stream tells us in the
        # same breath, and the poller reads the real order object as a backstop.
        self._wake.set()
        return self.idempotent_store(client_id, result, sig)

    def handle_orders_list(self, params):
        want = (params or {}).get("state")
        rows = (self.api.call("GET", "/v1/orders").get("orders") or [])
        out = [self._order_view(o) for o in rows]
        if want:
            out = [o for o in out if o["state"] == want]
        return {"orders": out}

    def _order_view(self, o: dict) -> dict:
        oid = str(o.get("orderId"))
        with self._state_lock:
            known = self._orders.get(oid) or {}
        status = (o.get("status") or "").upper()
        state = {"OPEN": "resting", "FILLED": "filled", "CANCELLED": "cancelled",
                 "EXPIRED": "expired", "REJECTED": "cancelled"}.get(status,
                                                                    "resting")
        reason = o.get("reason")
        # `reason` is only carried on a terminal order, and only the broker's
        # own kills get a SPEC error code (a client cancel is not a fault).
        code = {"GOOD_TILL_DATE_REACHED": "invalid_expiry",
                "INSTRUMENT_EXPIRED": "market_closed"}.get(reason)
        level = _num(o.get("level"))
        is_limit = known.get("type") == "limit" or o.get("goodTillDate") or \
            status == "OPEN"
        return {
            "order_id": oid,
            "client_id": known.get("client_id"),
            "symbol": o.get("epic"),
            "type": "limit" if is_limit else "market",
            "side": "buy" if (o.get("direction") or "").upper() == "BUY"
                    else "sell",
            "qty": _num(o.get("size")),
            "price": level if state == "resting" else None,
            "stop_price": None, "sl": None, "tp": None,
            "expiry": "gtd" if o.get("goodTillDate") else "gtc",
            "expires_at": _ms(o.get("goodTillDate")) if o.get("goodTillDate")
            else None,
            "state": state,
            "placed_at": _ms(o.get("createdDateUTC")),
            "done_at": None if state == "resting" else _ms(
                o.get("createdDateUTC")),
            "fill_price": level if state == "filled" else None,
            "reason": code if state != "resting" else None,
        }

    def handle_order_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise AdapterError("not_found", "order_id is required")
        try:
            o = self.api.call("DELETE", f"/v1/orders/{order_id}")
        except MeridianError as e:
            raise to_adapter_error(e)
        self._absorb_order(o)
        return {}

    def handle_order_amend(self, params):
        # Meridian's order API is POST and DELETE and nothing else: there is no
        # amend endpoint in any form. Refusing loudly is what SPEC 3 asks for
        # when a capability is absent. Emulating it as cancel-and-replace would
        # hand the platform a NEW order_id, and `order.amended` has no field to
        # carry one, so the emulation could not even be described on this wire.
        raise AdapterError("unsupported",
                           "Meridian cannot amend a resting order; cancel it "
                           "and place a new one")

    def handle_position_close_by(self, params):
        raise AdapterError("unsupported", "Meridian has no close-by")

    def handle_order_modify(self, params):
        raise AdapterError("unsupported",
                           "Meridian has no stop loss / take profit "
                           "attachments in this API version")

    # == closing =======================================================
    def handle_position_close(self, params):
        deal = str(params.get("position_id") or "")
        if not deal:
            raise AdapterError("not_found", "position_id is required")
        qty = params.get("qty")
        client_id = params.get("client_id")
        if client_id:
            prior = self.idempotent_check(client_id, ("close", deal, qty))
            if prior is not None:
                return prior
        body = {}
        if qty is not None:
            body["size"] = _num(qty)
        self._close_deal(deal, body, reason="manual")
        result = {}
        if client_id:
            self.idempotent_store(client_id, result, ("close", deal, qty))
        return result

    def _close_deal(self, deal: str, body: dict, reason: str):
        with self._state_lock:
            snap = self._deal_seen.get(deal)
        if snap is None:
            # Refresh once: the caller may be closing a position this process
            # has never listed (adopted after a reconnect).
            self._sync_positions()
            with self._state_lock:
                snap = self._deal_seen.get(deal)
        job = {"deal": deal, "reason": reason,
               "qty": _num((body or {}).get("size"),
                           _num((snap or {}).get("size"))),
               "snapshot": snap, "since": time.time()}
        # Filed BEFORE the DELETE goes out, not after it comes back. Meridian's
        # confirmation frame regularly reaches the stream thread before its own
        # 202 reaches this one, and a job filed afterwards then describes a
        # close that is already finished: the poller finds that orphan, sees no
        # position, and reports a SECOND position.closed carrying a realised
        # P/L it had to approximate. Filing first means whichever of the two
        # arrives first takes the job and the loser finds nothing to do; it
        # also means the frame path knows whether this was a position.close or
        # one leg of a flatten_all, which only this side can tell (Meridian
        # calls both MANUAL).
        with self._state_lock:
            self._closing[deal] = job
        try:
            self.api.call("DELETE", f"/v1/positions/{deal}", body=body or {})
        except MeridianError as e:
            with self._state_lock:
                # Refused: nothing was closed, so nothing is in flight. Removed
                # by identity, so a retry that already filed its own job keeps
                # it.
                if self._closing.get(deal) is job:
                    self._closing.pop(deal, None)
            raise to_adapter_error(e)
        self._wake.set()

    def handle_flatten_all(self, params):
        """Close everything, and do not come back until the book is empty.

        The suite calls `positions.list` the instant this returns, and
        Meridian's closes are asynchronous (202 Accepted, confirmation later),
        so returning on the 202s would report a book that is still visibly
        full. SPEC 8.1 also wants this to work while other calls are failing,
        which is why each close is attempted independently and a failure on one
        deal never abandons the rest.
        """
        try:
            rows = self._positions_raw()
        except MeridianError as e:
            raise to_adapter_error(e)
        closed = 0
        for p in rows:
            deal = str(p.get("dealId"))
            try:
                self._close_deal(deal, {}, reason="flatten")
                closed += 1
            except AdapterError as e:
                log(f"flatten: {deal} did not close: {e}")
        # Resting orders are part of flattening a book: an order left working
        # re-opens the position the caller just closed.
        try:
            for o in (self.api.call("GET", "/v1/orders").get("orders") or []):
                try:
                    self._absorb_order(
                        self.api.call("DELETE", f"/v1/orders/{o['orderId']}"))
                except MeridianError as e:
                    log(f"flatten: order {o.get('orderId')} not cancelled: {e}")
        except MeridianError as e:
            log(f"flatten: could not list resting orders: {e}")
        deadline = time.time() + 8.0
        while closed and time.time() < deadline:
            self._reap_closes()
            with self._state_lock:
                pending = bool(self._closing)
            if not pending:
                break
            time.sleep(0.5)
        return {"closed": closed}

    # == market data ===================================================
    def handle_quotes_subscribe(self, params):
        symbols = [s for s in (params.get("symbols") or []) if s]
        for s in symbols:
            self._instrument(s)          # unknown_symbol before subscribing
        with self._state_lock:
            new = [s for s in symbols if s not in self._epics]
            self._epics.extend(new)
        if new:
            self._restart_stream()
        # A snapshot immediately, from the broker's own quote endpoint. The
        # stream only speaks on a TICK, and Meridian's ticks stop dead when a
        # market closes, so a subscriber to a shut instrument would otherwise
        # wait forever for a price the broker can state on demand.
        try:
            self._emit_quotes(symbols)
        except MeridianError as e:
            log(f"quote snapshot failed: {e}")
        return {}

    def handle_quotes_unsubscribe(self, params):
        symbols = set(params.get("symbols") or [])
        with self._state_lock:
            keep = [s for s in self._epics if s not in symbols]
            changed = len(keep) != len(self._epics)
            self._epics = keep
        if changed:
            self._restart_stream()
        return {}

    def _emit_quotes(self, symbols: list):
        if not symbols:
            return
        q = urllib.parse.quote(",".join(symbols), safe=",.")
        data = self.api.call("GET", f"/v1/quotes?epics={q}")
        for row in data.get("quotes") or []:
            self._emit_quote(row)

    def _emit_quote(self, row: dict):
        self.emit("quote", {"symbol": row.get("epic"),
                            "bid": _num(row.get("bid")),
                            # Meridian says "offer"; the protocol says "ask".
                            "ask": _num(row.get("offer")),
                            "time": _ms(row.get("updateTimeUTC"))})

    def handle_bars_history(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        res = TF_TO_RESOLUTION.get(tf)
        if res is None:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        count = int(_num(params.get("count"), 200))
        # "max defaults to 200 and is capped at 500."
        want = max(1, min(count, 500))
        query = {"epic": symbol, "resolution": res, "max": want}
        if params.get("from"):
            query["from"] = _iso(int(_num(params["from"])))
        if params.get("to"):
            query["to"] = _iso(int(_num(params["to"])))
        data = self.api.call("GET", "/v1/pricehistory?"
                             + urllib.parse.urlencode(query))
        bars = [[_ms(p.get("snapshotTimeUTC")), _num(p.get("openPrice")),
                 _num(p.get("highPrice")), _num(p.get("lowPrice")),
                 _num(p.get("closePrice")), _num(p.get("lastTradedVolume"))]
                for p in (data.get("prices") or [])]
        bars.sort(key=lambda b: b[0])          # oldest first (SPEC)
        return {"bars": bars}

    def handle_bars_subscribe(self, params):
        symbol = params.get("symbol")
        self._instrument(symbol)
        tf = params.get("timeframe") or "1m"
        if tf not in TF_TO_RESOLUTION:
            raise AdapterError("unsupported", f"unknown timeframe {tf!r}")
        with self._state_lock:
            self._bars.setdefault((symbol, tf), 0)
        self._wake.set()
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._state_lock:
            self._bars.pop((params.get("symbol"),
                            params.get("timeframe") or "1m"), None)
        return {}

    # == history =======================================================
    def handle_history_fills(self, params):
        query = {"max": 500}
        if params.get("from"):
            query["from"] = _iso(int(_num(params["from"])))
        if params.get("to"):
            query["to"] = _iso(int(_num(params["to"])))
        data = self.api.call("GET", "/v1/history/transactions?"
                             + urllib.parse.urlencode(query))
        fills = []
        for t in (data.get("transactions") or []):
            if (t.get("kind") or "").upper() != "TRADE":
                continue
            fills.append({
                "fill_id": t.get("reference"),
                "symbol": t.get("epic"),
                "side": "buy" if (t.get("direction") or "").upper() == "BUY"
                        else "sell",
                "qty": _num(t.get("size")),
                "price": _num(t.get("level")),
                "fees": -abs(_num(t.get("fee"))),
                "realized_pnl": _num(t.get("profitLoss")),
                "time": _ms(t.get("dateUTC")),
            })
        fills.sort(key=lambda f: f["time"])
        return {"fills": fills}

    # == the stream ====================================================
    def _restart_stream(self):
        """Re-open the stream with a new epic set.

        Meridian fixes a stream's symbol list in the query string that opened
        it, so changing a subscription means a new connection. The old one is
        CLOSED explicitly rather than left to notice: its reader is blocked in
        a socket read that would otherwise hold the connection (and the thread)
        for the token's full 300 s.
        """
        with self._state_lock:
            self._stream_gen += 1
            gen = self._stream_gen
            old = self._stream_resp
            self._stream_resp = None
        if old is not None:
            try:
                old.close()
            except Exception:       # noqa: BLE001 - closing is best effort
                pass
        threading.Thread(target=self._stream_loop, args=(gen,),
                         daemon=True).start()

    def _stream_loop(self, gen: int | None = None):
        """One SSE connection, re-established for its whole life.

        Meridian ties a stream to the 300 s life of the token that opened it
        and closes it with `event: goodbye`, so reconnecting is normal
        operation rather than an error path. There is no replay, and the
        document names the REST endpoints as the source of truth for the gap,
        so every reconnect re-reads orders and positions.
        """
        if gen is None:
            with self._state_lock:
                self._stream_gen += 1
                gen = self._stream_gen
        first = True
        while not self._stop.is_set():
            with self._state_lock:
                if gen != self._stream_gen:
                    return                     # superseded by a newer stream
                epics = list(self._epics)
            try:
                token = self.api.token()
                url = f"{BASE}/v1/stream?access_token={token}"
                if epics:
                    url += "&epics=" + urllib.parse.quote(",".join(epics),
                                                          safe=",.")
                req = urllib.request.Request(url, headers={
                    "Accept": "text/event-stream"})
                resp = urllib.request.urlopen(req, timeout=330)
                with self._state_lock:
                    if gen != self._stream_gen:
                        resp.close()
                        return
                    self._stream_resp = resp
                self._set_session("connected", "stream open")
                if not first:
                    self._sync_orders()
                    self._sync_positions()
                first = False
                self._pump(resp, gen)
            except Exception as e:      # noqa: BLE001 - any failure reconnects
                if self._stop.is_set():
                    return
                self._set_session("degraded", f"stream: {type(e).__name__}: {e}")
                time.sleep(1.0)

    def _pump(self, resp, gen: int):
        name, data = None, None
        for raw in resp:
            if self._stop.is_set():
                return
            with self._state_lock:
                if gen != self._stream_gen:
                    return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if name and data is not None:
                    self._on_frame(name, data)
                name, data = None, None
                continue
            if line.startswith(":"):
                continue               # keepalive; documented ": hb", sent ":hb"
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()

    def _on_frame(self, name: str, payload: str):
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            log(f"unparseable {name} frame: {payload[:120]}")
            return
        if name == "quote":
            self._emit_quote(obj)
        elif name == "execution":
            self._on_execution(obj)
        elif name == "account":
            self.emit("account.updated", self._account_view(obj))
        elif name == "goodbye":
            # Documented: {"reason": "tokenExpired"} then a clean close. Not a
            # fault, so it is not reported as one; the loop re-tokens.
            log(f"stream goodbye: {obj}")
        else:
            log(f"unknown stream event {name!r}: {payload[:120]}")

    def _on_execution(self, obj: dict):
        """One execution frame, whatever shape it arrives in.

        Section 7 promises "the order object ... for order driven events, or a
        deal shaped object for system driven closes" and never prints the deal
        shape, so this branches on what is actually present rather than on what
        was promised.
        """
        if obj.get("orderId") is not None:
            self._absorb_order(obj)
        if obj.get("dealId") is not None:
            self._absorb_deal(obj)

    # == state machines ================================================
    def _absorb_order(self, o: dict, poke: bool = False):
        """Fold a broker order object into our view, emitting on TRANSITIONS.

        The stream is at-least-once (the same frame can arrive twice), and
        the poller reads the same order again, so everything here is keyed on
        a state change and is safe to run twice.
        """
        oid = str(o.get("orderId"))
        status = (o.get("status") or "").upper()
        with self._state_lock:
            rec = self._orders.get(oid)
            if rec is None:
                # An order this process did not place (adopted after a restart,
                # or placed by another session on the same account).
                rec = {"order_id": oid, "client_id": None,
                       "epic": o.get("epic"), "side": "buy" if
                       (o.get("direction") or "").upper() == "BUY" else "sell",
                       "qty": _num(o.get("size")), "type": "limit",
                       "price": _num(o.get("level")), "state": "new",
                       "placed_at": _ms(o.get("createdDateUTC")),
                       "expiry": "gtd" if o.get("goodTillDate") else "gtc",
                       "expires_at": _ms(o.get("goodTillDate"))
                       if o.get("goodTillDate") else None,
                       "deadline": time.time() + 30.0, "degraded": False}
                self._orders[oid] = rec
            prev = rec["state"]
            if status == "OPEN":
                new = "resting" if rec.get("type") == "limit" else "working"
            else:
                new = {"FILLED": "filled", "CANCELLED": "cancelled",
                       "EXPIRED": "expired", "REJECTED": "rejected"}.get(
                           status, prev)
            if new == prev:
                return
            rec["state"] = new
            client_id = rec.get("client_id")
            deal = o.get("affectedDealId")
            if deal is not None and client_id:
                # SPEC 4: a position's client_id is the id of the order that
                # OPENED it. On a netting book every later order lands on the
                # same dealId, so the first writer wins; assigning on each
                # touch renames a position after whatever last hit it, and a
                # platform looking for the order it opened with finds a
                # stranger.
                self._deal_client.setdefault(str(deal), client_id)

        if new == "resting":
            self.emit("order.placed", {
                "client_id": client_id, "order_id": oid, "type": "limit",
                "price": _num(o.get("level")),
                "expires_at": _ms(o.get("goodTillDate"))
                if o.get("goodTillDate") else None})
        elif new == "filled":
            inst = None
            try:
                inst = self._instrument(o.get("epic"))
            except AdapterError:
                pass
            # `fillSize`, `fillPrice` and `fee` are on the FILLED frame but not
            # in the documentation, which describes this payload as "the order
            # object (section 5.2)" and prints it without them. Preferred where
            # present because they are the broker's own
            # account of what was dealt, with the documented fields behind them:
            # `size` is the order's size (the same number for a full fill) and
            # `level` is documented as "the achieved average fill price once a
            # MARKET order completes".
            qty = _num(o.get("fillSize"), _num(o.get("size"), rec.get("qty")))
            price = _num(o.get("fillPrice"), _num(o.get("level")))
            schedule = self._commission_for(inst, qty) if inst else 0.0
            if o.get("fee") is None:
                fees = schedule
            else:
                fees = -abs(_num(o.get("fee")))
                if inst is not None and abs(fees - schedule) > 0.005:
                    # Not corrected, only surfaced: the broker's charge is the
                    # true one, but a schedule the catalog publishes and does
                    # not honour is worth somebody's attention.
                    log(f"order {oid}: reported fee {fees} disagrees with the "
                        f"published schedule ({schedule}) for "
                        f"{o.get('epic')} x{qty}")
            self.emit("order.filled", {
                "client_id": client_id, "order_id": oid,
                "position_id": str(o.get("affectedDealId") or oid),
                "symbol": o.get("epic"),
                "side": rec.get("side"), "qty": qty,
                "price": price,
                "fees": fees,
                "time": _ms(o.get("createdDateUTC")) or int(time.time() * 1000)})
            self._sync_positions()
        elif new == "cancelled":
            reason = o.get("reason")
            data = {"order_id": oid, "client_id": client_id}
            if reason and reason != "CLIENT_CANCELLED":
                # `code` present means the BROKER killed it (SPEC 5); a client
                # cancellation carries none.
                data["code"] = "market_closed" if reason == "INSTRUMENT_EXPIRED" \
                    else "internal"
                data["message"] = str(reason)
            self.emit("order.cancelled", data)
        elif new == "expired":
            self.emit("order.expired", {"order_id": oid,
                                        "client_id": client_id,
                                        "expiry": "gtd"})
        elif new == "rejected":
            self.emit("order.rejected", {
                "client_id": client_id, "code": "internal",
                "message": str(o.get("reason") or "rejected by Meridian")})

    # Meridian's word on a deal frame -> the SPEC's closed reason set. MANUAL
    # and NETTED are the values the venue sends; the others are the events
    # section 7 says this frame shape exists to carry ("take profit / stop out
    # style"), mapped ahead of time so a stop-out never arrives as a plain
    # manual close. Anything unrecognised is logged and reported as manual: the
    # SPEC's set is closed and has no value for "the broker said something new".
    DEAL_CLOSE_REASON = {
        "MANUAL": "manual",
        # A netting book reducing, or flipping, a position against the client's
        # own opposing order. SPEC 5's reason set (manual, sl, tp, flatten,
        # stop_out, expired, close_by) has no word for it, and "manual" is the
        # least wrong of them: the client's own order did it and no broker-side
        # event was involved.
        "NETTED": "manual",
        "STOP_OUT": "stop_out", "MARGIN_CALL": "stop_out",
        "TAKE_PROFIT": "tp", "STOP_LOSS": "sl",
        "INSTRUMENT_EXPIRED": "expired",
    }

    def _absorb_deal(self, d: dict):
        """A deal-shaped execution frame: Meridian's view of one position.

        One arrives every time a position's size changes: when it opens, when a
        later fill adds to it, when an opposing fill nets it down, and when it
        goes. `size` on the frame is what the position has LEFT, never the
        amount that just went, so the amount that went is the difference from
        the size last seen. Reading `size` as the closed quantity is right
        only when a position is exactly halved.

        Deltas also absorb the at-least-once stream for nothing: a repeated
        frame restates a size already recorded, the delta is zero, and no
        second `position.closed` goes out.
        """
        deal = str(d.get("dealId"))
        status = (d.get("status") or "").upper()
        remaining = 0.0 if status == "CLOSED" else _num(d.get("size"))
        pnl = d.get("profitLoss")
        with self._state_lock:
            pending = self._closing.get(deal)
            snap = self._deal_seen.get(deal)
            prev = _num(snap.get("size")) if snap else None
        if prev is None:
            # Never listed by this process: the close we asked for is the only
            # size still knowable. An open is simply recorded.
            closed = _num((pending or {}).get("qty")) if pending else 0.0
        else:
            closed = round(prev - remaining, 8)
        if closed <= 1e-9:
            if status == "CLOSED" and prev is None and pending is None:
                log(f"deal {deal} closed but was never seen open; its size is "
                    f"not recoverable from the frame, so nothing is reported")
            # An open, or a fill adding to one: remember the size so the next
            # delta is measured from the right place and say nothing. The fill
            # itself is announced as `order.filled`.
            self._remember_deal(d, remaining)
            return
        # `level` is deliberately NOT passed as the close price: on a deal
        # frame it is the position's OPEN level, never the price the close was
        # done at. `_finish_close` uses the last `currentLevel` Meridian
        # published for the deal instead, which is its own documented "price
        # you would close at right now".
        self._finish_close(
            deal, qty=closed,
            realized=_num(pnl) if pnl is not None else None,
            time_ms=_ms(d.get("dateUTC")), remaining=remaining,
            broker_reason=d.get("reason"))
        if remaining > 0:
            self._remember_deal(d, remaining)

    def _remember_deal(self, d: dict, size: float):
        """Keep `_deal_seen` in step with the stream between position reads."""
        deal = str(d.get("dealId"))
        with self._state_lock:
            row = dict(self._deal_seen.get(deal) or {})
            row["dealId"] = deal
            row["size"] = size
            for frm, to in (("epic", "epic"), ("direction", "direction")):
                if d.get(frm) is not None:
                    row[to] = d[frm]
            if d.get("level") is not None and not row.get("openLevel"):
                row["openLevel"] = _num(d.get("level"))
            self._deal_seen[deal] = row

    def _realized_from_ledger(self, deal: str, job: dict, qty: float):
        """The broker's own realised figure for a close whose frame was lost.

        Safe only because the ledger is scoped to the calling account: a row
        matching on epic and size in a wider ledger could be a stranger's
        trade. That scoping is what turns section 7's "the source of truth for
        anything that may have happened while you were disconnected" into
        something an adapter can act on.

        Deliberately timid: it reads the figure only when exactly ONE row can
        be this close, and otherwise leaves the caller to approximate and say
        so. A confident wrong number on the wrong deal is worse than an honest
        estimate.
        """
        snap = job.get("snapshot") or {}
        epic = snap.get("epic")
        side = (snap.get("direction") or "").upper()
        if not epic or side not in ("BUY", "SELL"):
            return None
        # A long is closed by a sale and a short by a purchase; an opening
        # trade always carries profitLoss 0.00 (doc section 6), so a nonzero
        # one is a close or a reduce.
        want = "SELL" if side == "BUY" else "BUY"
        since = _num(job.get("since"), time.time()) - 5.0
        try:
            data = self.api.call(
                "GET", "/v1/history/transactions?" + urllib.parse.urlencode(
                    {"from": _iso(int(since * 1000)), "max": 100}))
        except MeridianError as e:
            log(f"close {deal}: ledger unreadable ({e})")
            return None
        rows = [t for t in (data.get("transactions") or [])
                if (t.get("kind") or "").upper() == "TRADE"
                and t.get("epic") == epic
                and (t.get("direction") or "").upper() == want
                and abs(_num(t.get("size")) - _num(qty)) < 1e-9
                and _num(t.get("profitLoss")) != 0.0]
        if len(rows) != 1:
            log(f"close {deal}: {len(rows)} ledger rows could be this close; "
                f"not choosing between them")
            return None
        return _num(rows[0].get("profitLoss"))

    def _finish_close(self, deal: str, price=None, qty=None, realized=None,
                      time_ms=None, remaining=None, broker_reason=None):
        with self._state_lock:
            pending = self._closing.pop(deal, None)
            snap = self._deal_seen.get(deal) or (pending or {}).get("snapshot")
            client_id = self._deal_client.get(deal)
            if remaining in (None, 0):
                self._deal_seen.pop(deal, None)
                # Read above, dropped here: nothing can arrive for a dealId
                # that is gone, and a long-lived adapter should not carry one
                # entry per position it has ever held.
                self._deal_client.pop(deal, None)
        if snap is None and pending is None:
            return
        snap = snap or {}
        if qty in (None, 0):
            qty = _num((pending or {}).get("qty"), _num(snap.get("size")))
        if price in (None, 0):
            price = _num(snap.get("currentLevel"), _num(snap.get("openLevel")))
        if realized is None:
            # No frame reached us (it can be closed before the stream is up on
            # a fresh process). Section 7 nominates the transaction ledger as
            # the source of truth for exactly this, so ask it before guessing.
            realized = self._realized_from_ledger(deal, pending or {}, qty)
            if realized is not None:
                log(f"close {deal}: realised P/L {realized} read from the "
                    f"ledger (no confirmation frame reached us)")
        if realized is None:
            # Meridian's realised figure rides on the confirmation frame, whose
            # shape the document never prints. Only when that frame is
            # lost AND the ledger cannot say which row was this close does this
            # stand in: the last unrealised P/L the position reported, scaled
            # by the fraction closed. It is an APPROXIMATION and says so on
            # stderr rather than passing for the broker's own number.
            whole = _num(snap.get("size")) or qty or 1.0
            realized = round(_num(snap.get("profitLoss")) * (qty / whole), 2)
            log(f"close {deal}: realised P/L approximated as {realized} "
                f"(no confirmation frame reached us)")
        # The adapter's own reason wins where it has one: only this side knows
        # whether a close was a single position.close or one leg of a
        # flatten_all, and Meridian calls both of them MANUAL.
        reason = (pending or {}).get("reason")
        if reason is None:
            key = str(broker_reason or "").upper()
            reason = self.DEAL_CLOSE_REASON.get(key)
            if reason is None:
                if key:
                    log(f"close {deal}: unmapped Meridian reason "
                        f"{broker_reason!r}; reported as manual")
                reason = "manual"
        data = {"position_id": deal, "qty": qty, "price": price,
                "realized_pnl": realized, "swap": 0.0, "commission": 0.0,
                "reason": reason,
                "time": time_ms or int(time.time() * 1000)}
        if client_id:
            data["client_id"] = client_id
        self.emit("position.closed", data)

    # == the poller ====================================================
    def _poll_loop(self):
        """The backstop for everything the stream is supposed to say.

        Meridian's own document nominates `GET /v1/orders` and
        `GET /v1/positions` as the source of truth after any gap, and both
        market fills and closes are asynchronous, so anything in flight is
        polled to a conclusion. It sleeps unless there is something in flight,
        so an idle adapter spends none of the 120-per-minute budget.
        """
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self._reap_orders()
                self._reap_closes()
                self._reap_bars()
            except Exception as e:      # noqa: BLE001 - a poller must not die
                log(f"poll: {type(e).__name__}: {e}")

    def _inflight(self) -> list:
        with self._state_lock:
            return [dict(r) for r in self._orders.values()
                    if r["state"] in ("new", "working")]

    def _reap_orders(self):
        for rec in self._inflight():
            oid = rec["order_id"]
            try:
                self._absorb_order(self.api.call("GET", f"/v1/orders/{oid}"))
            except MeridianError as e:
                log(f"poll order {oid}: {e}")
                continue
            with self._state_lock:
                cur = self._orders.get(oid) or {}
                overdue = (cur.get("state") in ("new", "working")
                           and time.time() > cur.get("deadline", 0)
                           and not cur.get("degraded"))
                if overdue:
                    cur["degraded"] = True
            if overdue:
                # SPEC 8.4: an order that has not terminated inside the declared
                # timeout is reported, not quietly waited on.
                self._set_session(
                    "degraded",
                    f"order {oid} has not reached a terminal state within 30s")

    def _reap_closes(self):
        with self._state_lock:
            pending = list(self._closing.values())
        if not pending:
            return
        try:
            rows = {str(p.get("dealId")): p for p in self._positions_raw()}
        except MeridianError as e:
            log(f"poll positions: {e}")
            return
        for job in pending:
            deal = job["deal"]
            still = rows.get(deal)
            # REST going quiet about a deal is not the first word on a close,
            # it is the second. Meridian's own confirmation frame carries the
            # exact realised P/L and lands about a second after the 202, so
            # finishing here immediately would swap a broker number for local
            # arithmetic on every close the stream was about to confirm. The
            # poller waits out the grace and only steps in if the frame is
            # genuinely lost, which is the job it exists to do.
            if time.time() - job["since"] < CLOSE_CONFIRM_GRACE:
                continue
            if still is None:
                self._finish_close(deal, qty=job.get("qty"))
            else:
                before = _num((job.get("snapshot") or {}).get("size"))
                now = _num(still.get("size"))
                if before and now and now < before - 1e-9:
                    # A partial close: the deal survives with less size, so the
                    # closed quantity is the difference, not the whole ticket.
                    self._finish_close(deal, qty=round(before - now, 8),
                                       remaining=now)
                elif time.time() - job["since"] > 30:
                    with self._state_lock:
                        self._closing.pop(deal, None)
                    self._set_session(
                        "degraded",
                        f"close of {deal} unconfirmed after 30s")

    def _reap_bars(self):
        with self._state_lock:
            wants = dict(self._bars)
        for (epic, tf), last in wants.items():
            try:
                data = self.api.call(
                    "GET", "/v1/pricehistory?" + urllib.parse.urlencode(
                        {"epic": epic, "resolution": TF_TO_RESOLUTION[tf],
                         "max": 2}))
            except MeridianError as e:
                log(f"bar poll {epic} {tf}: {e}")
                continue
            prices = data.get("prices") or []
            if not prices:
                continue
            # Only CLOSED bars go out (SPEC): the newest row is still forming
            # until its window has elapsed, so it is held back until then.
            for p in prices:
                t = _ms(p.get("snapshotTimeUTC"))
                if t <= last or t + TF_MS[tf] > time.time() * 1000:
                    continue
                self.emit("bar", {"symbol": epic, "timeframe": tf,
                                  "bar": [t, _num(p.get("openPrice")),
                                          _num(p.get("highPrice")),
                                          _num(p.get("lowPrice")),
                                          _num(p.get("closePrice")),
                                          _num(p.get("lastTradedVolume"))]})
                with self._state_lock:
                    if (epic, tf) in self._bars:
                        self._bars[(epic, tf)] = t

    def _sync_orders(self):
        try:
            for o in (self.api.call("GET", "/v1/orders").get("orders") or []):
                self._absorb_order(o)
        except MeridianError as e:
            log(f"order sync failed: {e}")

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
    MeridianAdapter().run()


if __name__ == "__main__":
    main()
