#!/usr/bin/env python3
"""The LSE demo account as a brue-connect broker.

The LSE simulated-trading engine (`/sim/accounts`, `/sim/orders`, ...)
reached through the same protocol as every other broker, so a platform has
one quote path, one account reader, one position list and one order
function whichever account it trades, and the demo account is certified by
the same conformance suite as any broker.

    python3 lse_sim_adapter.py

Configuration is environment only; no credential ever travels in a protocol
message (SPEC 8.5):

    LSE_API_KEY / BRUE_API_KEY    the user's LSE key (required)
    LSE_SIM_API                   default https://api.londonstrategicedge.com
    LSE_DATA_API                  default https://data-api.londonstrategicedge.com
    LSE_SIM_ACCOUNT               account name or id; default is the first one
    BRUE_BROKER_STATE             where the learned account currency is cached

What the broker cannot do, declared rather than emulated: trailing stops,
OCO, close-by, and `history.operations`. A partial close IS supported, built
here from an opposing order on the netting book, so no platform has to build
that order for itself. Sessions are reported as 24/7 because the engine has
no clock gate: it fills whenever a tick arrives.
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from email.utils import parsedate_to_datetime
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

# The CDN in front of the API answers the stdlib default agent with a
# plain-text 403 from datacenter IPs, which a JSON client reads as a parse
# error rather than as "identify yourself". Every request says who it is.
UA = "brue-connect-lse-sim/1.0"

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# The engine's rejection vocabulary, mapped to the protocol's closed set. An
# unrecognised reason stays `internal` and keeps its text, so a new engine
# message is visible rather than mislabelled.
REASONS = {
    "insufficient margin": "insufficient_margin",
    "market closed": "market_closed",
    "unknown symbol": "unknown_symbol",
    "qty below step": "invalid_qty",
    "no position": "not_found",
    "no working order": "not_resting",
    "account not found": "not_found",
    "bad side/type": "unsupported",
    "limit_price required": "invalid_stops",
    "stop_price required": "invalid_stops",
}

# Refusals the engine phrases as a sentence rather than a code word, matched on
# a fragment so a reworded message still lands somewhere sensible.
REASON_FRAGMENTS = (
    ("no fx cross", "trade_restricted"),
    ("cannot be valued", "trade_restricted"),
    ("insufficient margin", "insufficient_margin"),
    ("unknown symbol", "unknown_symbol"),
)


def _iso_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class LSESimAdapter(Adapter):
    name = "lse-sim"
    version = "1.0"
    broker = "LSE Demo Account"
    mode = "paper"          # the engine has no real money in it, ever
    account_currency = "USD"   # replaced by the account's own, cached below
    account_model = "netting"  # one position per symbol, keyed by symbol

    def __init__(self):
        super().__init__()
        self.api = os.environ.get(
            "LSE_SIM_API", "https://api.londonstrategicedge.com").rstrip("/")
        self.data_api = os.environ.get(
            "LSE_DATA_API",
            "https://data-api.londonstrategicedge.com").rstrip("/")
        self.key = (os.environ.get("LSE_API_KEY")
                    or os.environ.get("BRUE_API_KEY") or "")
        # BRUE_ACCOUNT_ID is the SPEC 1.7 binding the platform sets when the
        # user picks an account, and it must win over LSE_SIM_ACCOUNT, this
        # adapter's own older knob: reading only the private name makes every
        # platform-side choice a silent no-op and binds the login's first
        # account instead.
        self.want_account = (os.environ.get("BRUE_ACCOUNT_ID", "").strip()
                             or os.environ.get("LSE_SIM_ACCOUNT", ""))
        state = os.environ.get("BRUE_BROKER_STATE", "")
        # cache.json, NOT account.json: account.json is the registry's pin
        # file (the user's explicit account choice). A cache written there
        # comes back as a hard pin on the next spawn, so a key switch is
        # refused with a "no demo account" error for the stale id, and the
        # cache write can clobber a real user choice. The old account.json
        # path stays readable for the currency only.
        self.cache_path = os.path.join(state, "cache.json") if state else ""
        self._old_cache = os.path.join(state, "account.json") if state else ""
        self.account_id: int | None = None
        self._catalog: dict = {}
        self._quote_syms: set[str] = set()
        self._quote_lock = threading.Lock()
        self._quote_send_lock = threading.Lock()
        self._quote_wakeup = threading.Event()
        self._quote_pending = deque()
        self._quote_queued: set[str] = set()
        self._quote_next_at = 0.0
        self._quote_thread = None
        self._session_lock = threading.Lock()
        self._session_components = {}
        self._session_reported = None
        self._bar_subs: set[tuple[str, str]] = set()
        self._bar_last: dict[tuple[str, str], int] = {}
        # order_id -> client_id. The engine has no client_id of its own and
        # sends None, but the platform correlates fills by it (SPEC 4), so the
        # translation is this adapter's job.
        self._client_ids: dict[str, str] = {}
        # position_id -> client_id, learned from fills seen in THIS session
        self._pos_clients: dict[str, str] = {}
        # How many order.place calls are in flight. The engine emits
        # order.accepted and order.filled DURING the POST that creates the
        # order, so the stream can see an event microseconds before the
        # response that reveals which client_id it belongs to. While a
        # placement is in flight, an unmapped order event is worth waiting
        # for; while none is, it belongs to another session (the website, say)
        # and must not stall this one.
        self._placing = 0
        self._place_lock = threading.Lock()
        self._stop = threading.Event()
        # Set once the event stream is live. An adapter must not answer its
        # first request before it can receive events: the engine emits a fill
        # to whoever is listening AT THAT MOMENT and never replays it, so an
        # order placed during the connect window fills for real and the
        # platform never hears about it.
        self._stream_ready = threading.Event()
        self._load_cache()

    # ------------------------------------------------------------ config
    def _load_cache(self):
        """The learned account currency, so `hello` is truthful cold.

        SPEC 3.1: a platform may handshake purely to learn what a broker is
        called, so the handshake must not need a session. The currency is the
        one fact hello states that only the broker knows, so it is cached
        beside the profile's state rather than fetched on every launch.
        """
        for path in (self.cache_path, self._old_cache):
            try:
                with open(path) as fh:
                    d = json.load(fh)
                if d.get("api") == self.api and d.get("currency"):
                    self.account_currency = d["currency"]
                    return
            except Exception:
                continue

    def _save_cache(self):
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "w") as fh:
                # Currency only. The resolved account_id must never be
                # persisted: written into any file the registry reads, it
                # comes back as an explicit pin (see __init__); the pin's one
                # legitimate writer is the platform.
                json.dump({"api": self.api,
                           "currency": self.account_currency}, fh)
        except OSError:
            pass
        # Drop an account.json cache that this adapter itself wrote, so its
        # stale account_id cannot linger looking like a choice. A real
        # platform choice (marked "chosen") is not the adapter's to delete.
        try:
            with open(self._old_cache) as fh:
                old = json.load(fh)
            if isinstance(old, dict) and "api" in old \
                    and not old.get("chosen"):
                os.remove(self._old_cache)
        except Exception:
            pass

    # -------------------------------------------------------------- http
    def _http(self, method: str, url: str, body=None, base: str | None = None):
        if not self.key:
            raise AdapterError(
                "unauthorized",
                "no LSE API key: set LSE_API_KEY or BRUE_API_KEY. Get one at "
                "https://londonstrategicedge.com/data")
        full = (base or self.api) + url
        raw = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(full, data=raw, method=method)
        req.add_header("x-api-key", self.key)
        req.add_header("user-agent", UA)
        if body is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            detail = payload[:200]
            try:
                d = json.loads(payload)
                detail = d.get("detail") if isinstance(d, dict) else detail
                if isinstance(detail, list):     # validation errors
                    detail = json.dumps(detail)[:200]
            except ValueError:
                pass
            code = {401: "unauthorized", 403: "unauthorized", 404: "not_found",
                    429: "rate_limited"}.get(e.code, "internal")
            if isinstance(detail, str):
                low = detail.strip().lower()
                code = REASONS.get(low, code)
                if code == "internal":
                    for frag, mapped in REASON_FRAGMENTS:
                        if frag in low:
                            code = mapped
                            break
            error = AdapterError(code, f"{detail}", e.code in (429, 502, 503))
            if e.code == 429:
                now = time.time()
                retry = e.headers.get("Retry-After") if e.headers else None
                try:
                    seconds = float(retry)
                    if not math.isfinite(seconds):
                        raise ValueError("non-finite Retry-After")
                except (TypeError, ValueError):
                    try:
                        seconds = parsedate_to_datetime(retry).timestamp() - now
                    except (TypeError, ValueError, OverflowError):
                        # The price board's key cap uses a fixed epoch minute
                        # and currently sends no Retry-After for that cap.
                        seconds = (int(now // 60) + 1) * 60 - now
                error.retry_after_s = max(0.0, seconds) + random.uniform(.1, .4)
            raise error
        except Exception as e:
            raise AdapterError("internal", f"{type(e).__name__}: {e}", True)
        try:
            return json.loads(text) if text else {}
        except ValueError:
            # A non-JSON body means the edge answered, not the engine.
            raise AdapterError("internal",
                               f"non-JSON response from {full}: {text[:120]}")

    # ------------------------------------------------------- first use
    def on_first_call(self):
        """Resolve the account and start the event stream.

        Deliberately not at startup: `hello` must stay answerable without a
        session so a connection picker can learn this broker's name for free.
        """
        accounts = self._http("GET", "/sim/accounts")
        if not isinstance(accounts, list) or not accounts:
            raise AdapterError("unauthorized",
                               "this key has no demo account; open one at "
                               "https://londonstrategicedge.com")
        chosen = None
        if self.want_account:
            for a in accounts:
                if (str(a.get("id")) == self.want_account
                        or a.get("name") == self.want_account):
                    chosen = a
                    break
            if chosen is None:
                names = ", ".join(str(a.get("name")) for a in accounts)
                raise AdapterError(
                    "not_found",
                    f"no demo account {self.want_account!r}; have: {names}")
        else:
            chosen = accounts[0]
        self.account_id = int(chosen["id"])
        self.account_currency = chosen.get("currency") or "USD"
        self._save_cache()
        print(f"# lse-sim: account {chosen.get('name')} (id {self.account_id}, "
              f"{self.account_currency})", file=sys.stderr)
        threading.Thread(target=self._stream_forever, daemon=True).start()
        # Wait for it, but never fail on it: a broker whose event path is down
        # can still be traded, and the platform is told it is degraded rather
        # than being refused a connection.
        if not self._stream_ready.wait(15.0):
            print("# lse-sim: event stream not up after 15s; continuing "
                  "degraded", file=sys.stderr)

    def _acct(self) -> int:
        if self.account_id is None:
            self.on_first_call()
        return int(self.account_id)

    # ---------------------------------------------------------- catalog
    def capabilities(self) -> dict:
        return {
            "orders": {
                "market": True, "brackets": True, "modify": True,
                "pending": True, "trailing": False,
                # Implemented HERE, by an opposing order on a netting book,
                # which is what a partial close is. The engine has no partial
                # exit call, and the platform must not have to know that.
                "partial_close": True,
                "pending_types": ["limit", "stop"],
                # Working orders rest until filled or cancelled: no expiry
                # kinds beyond good-till-cancelled.
                "expiry": ["gtc"],
                "oco": False, "close_by": False,
                # Margin is checked at fill time, not at placement.
                "reserves_margin": False,
            },
            "data": {"quotes": True, "bars": True, "history": True,
                     # Declared, not discovered by the platform hitting them.
                     # The price board takes 50 symbols per call and is rate
                     # limited per key, so 100 symbols is what this adapter
                     # serves as rotating pages under one shared request
                     # budget. A platform that wants more knows before asking.
                     "max_quote_symbols": 100,
                     "max_symbols_per_request": 50,
                     # No ledger endpoint: a balance move that is not a fill
                     # has no explanation available, and saying so is better
                     # than attributing it to trading.
                     "operations": False},
            "flatten_all": True,
        }

    def rate_limits(self) -> dict:
        # The account endpoints are rate limited per key; the price board is
        # the tighter of the two.
        return {"requests_per_second": 2}

    def broker_info(self) -> dict:
        return {"display_name": "LSE Demo Account",
                "tagline": "London Strategic Edge simulated trading",
                "mark": "LSE",
                "website": "https://londonstrategicedge.com",
                "support": "mailto:support@londonstrategicedge.com",
                "country": "United Kingdom",
                "regulated_by": ["Simulated account, no client money"],
                "asset_classes": ["FX", "Shares", "Indices", "Commodities",
                                  "Crypto", "ETFs"]}

    def handle_catalog_list(self, params: dict) -> dict:
        data = self._http("GET", "/sim/specs")
        out = []
        for spec in data.get("instruments", []):
            spec = dict(spec)
            # The engine has no clock gate at all: it fills whenever a tick
            # arrives for the symbol and refuses nothing on time of day. That
            # is a 24/7 session, and saying so explicitly is the difference
            # between "always open" and "we did not tell you" (SPEC 3), which
            # an empty list cannot express. An instrument whose real market is
            # shut simply gets no ticks, and an order that cannot fill rests.
            spec.setdefault("holidays", [])
            if not spec.get("sessions"):
                spec["sessions"] = [{"days": [1, 2, 3, 4, 5, 6, 7],
                                     "open": "00:00", "close": "24:00",
                                     "tz": "UTC"}]
            out.append(spec)
        self._catalog = {s["symbol"]: s for s in out}
        return {"instruments": out}

    def _spec(self, symbol: str) -> dict:
        if not self._catalog:
            self.handle_catalog_list({})
        spec = self._catalog.get(symbol)
        if spec is None:
            raise AdapterError("unknown_symbol", f"{symbol} is not dealt here")
        return spec

    # ---------------------------------------------------------- account
    def _account_row(self) -> dict:
        for a in self._http("GET", "/sim/accounts"):
            if int(a["id"]) == self._acct():
                return a
        raise AdapterError("not_found", "the demo account is gone")

    def handle_account_get(self, params: dict) -> dict:
        a = self._account_row()
        used = float(a.get("used_margin") or 0)
        equity = float(a.get("equity") or a.get("balance") or 0)
        return {"balance": float(a.get("balance") or 0), "equity": equity,
                "margin_used": used, "margin_free": equity - used,
                "currency": a.get("currency") or self.account_currency}

    def handle_positions_list(self, params: dict) -> dict:
        rows = self._http("GET", f"/sim/positions?account_id={self._acct()}")
        out = []
        for p in rows:
            qty = float(p["qty"])
            out.append({
                "position_id": str(p["id"]),
                "symbol": p["symbol"],
                "side": "buy" if qty > 0 else "sell",
                "qty": abs(qty),
                "avg_price": float(p["avg_price"]),
                "sl": p.get("sl_price") or 0,
                "tp": p.get("tp_price") or 0,
                "trail_points": 0,
                "unrealized_pnl": float(p.get("unrealized_pnl") or 0),
                # The engine charges neither: a position's cost is in its
                # fill price, and reporting a fabricated zero would be a lie
                # only if it charged something. It does not.
                "swap": 0.0, "commission": 0.0,
                "opened_at": _iso_ms(p.get("opened_at")),
                # Known only for orders this session placed, and only until a
                # restart: the engine's book is keyed by symbol and keeps no
                # link to the order that opened a position. Optional by
                # contract, so absent rather than invented.
                "client_id": self._pos_clients.get(str(p["id"])),
                "from_order": None,
            })
        return {"positions": out}

    def handle_orders_list(self, params: dict) -> dict:
        rows = self._http("GET", f"/sim/orders?account_id={self._acct()}")
        out = []
        for o in rows:
            out.append({
                "order_id": str(o["id"]),
                "client_id": self._client_ids.get(str(o["id"])),
                "symbol": o["symbol"], "side": o["side"],
                "type": o.get("otype") or "market",
                "qty": float(o["qty"]),
                "price": o.get("limit_price") or 0,
                "stop_price": o.get("stop_price") or 0,
                "sl": o.get("sl_price") or 0, "tp": o.get("tp_price") or 0,
                "state": "resting",
                "expiry": "gtc", "expires_at": None,
                "placed_at": _iso_ms(o.get("created_at")),
            })
        return {"orders": out}

    def handle_history_fills(self, params: dict) -> dict:
        rows = self._http(
            "GET", f"/sim/fills?account_id={self._acct()}&limit=500")
        return {"fills": [{
            "symbol": f["symbol"], "side": f["side"], "qty": float(f["qty"]),
            "price": float(f["price"]),
            "realized_pnl": float(f["realized_pnl"] or 0),
            "time": _iso_ms(f.get("ts")),
        } for f in rows]}

    # ----------------------------------------------------------- trading
    def handle_order_place(self, params: dict) -> dict:
        cid = params.get("client_id")
        if not cid:
            raise AdapterError("invalid_qty", "client_id is required")
        sym = params.get("symbol", "")
        spec = self._spec(sym)
        qty = float(params.get("qty") or 0)
        side = params.get("side")
        otype = params.get("type") or "market"
        sig = (sym, side, qty, otype, params.get("price"),
               params.get("stop_price"), params.get("sl"), params.get("tp"))
        replay = self.idempotent_check(cid, sig)
        if replay is not None:
            return replay
        if side not in ("buy", "sell"):
            raise AdapterError("unsupported", f"side {side!r}")
        if otype not in ("market", "limit", "stop"):
            raise AdapterError("unsupported",
                               f"order type {otype!r}; this broker deals "
                               "market, limit and stop")
        if qty < float(spec["min_qty"]) or qty > float(spec["max_qty"]):
            raise AdapterError(
                "invalid_qty",
                f"{qty} outside {spec['min_qty']}..{spec['max_qty']}")
        # The engine ROUNDS an off-step quantity to the nearest step and fills
        # it. That is a silent substitution: the platform asked for 1.5 and
        # owns 2. SPEC 3 forbids exactly this, so the adapter refuses on the
        # published step instead of letting it through.
        step = float(spec.get("lot_step") or 0)
        if step > 0:
            steps = qty / step
            if abs(steps - round(steps)) > 1e-9:
                raise AdapterError(
                    "invalid_qty",
                    f"{qty} is not a multiple of the {step} lot step")
        if params.get("trail_points"):
            raise AdapterError("unsupported", "this broker cannot trail")
        if spec.get("trade_mode") == "close_only":
            raise AdapterError("trade_restricted", f"{sym} is close-only")
        if side == "sell" and not spec.get("shortable"):
            raise AdapterError("trade_restricted", f"{sym} is not shortable")
        body = {"account_id": self._acct(), "symbol": sym, "side": side,
                "otype": otype, "qty": qty}
        if otype == "limit":
            body["limit_price"] = params.get("price")
        if otype == "stop":
            # SPEC 4: a plain stop carries its trigger in `price` (only
            # stop_limit has a distinct stop_price). `stop_price` is accepted
            # as an alias so a caller that sends only that field still works.
            trigger = params.get("price")
            if trigger is None:
                trigger = params.get("stop_price")
            body["stop_price"] = trigger
        if params.get("sl"):
            body["sl_price"] = params["sl"]
        if params.get("tp"):
            body["tp_price"] = params["tp"]
        with self._place_lock:
            self._placing += 1
        try:
            res = self._http("POST", "/sim/orders", body)
            oid = str(res.get("id"))
            # Recorded before the in-flight counter drops, so a stream frame
            # waiting on this id finds it the instant its wait may end.
            self._client_ids[oid] = cid
        finally:
            with self._place_lock:
                self._placing -= 1
        status = res.get("status")
        if status == "rejected":
            reason = (res.get("reason") or "rejected").strip()
            raise AdapterError(REASONS.get(reason.lower(), "internal"), reason)
        if otype == "market" and status == "working":
            # The engine fills a market order from its live quote cache, so a
            # market order still working means there is no quote for this
            # symbol right now: a shut market, an exchange lunch break, a
            # stalled feed. Leaving it to rest would fill it at some unrelated
            # future price, which is not what a market order means, so it is
            # cancelled and the platform is told in the protocol's own words.
            try:
                self._http("POST", "/sim/orders/cancel",
                           {"account_id": self._acct(), "order_id": int(oid)})
            except (AdapterError, ValueError):
                pass
            raise AdapterError(
                "no_price",
                f"no current quote for {sym}: the market order could not be "
                "filled and has been cancelled")
        out = {"order_id": oid,
               "status": "resting" if status == "working" else "accepted"}
        return self.idempotent_store(cid, out, sig)

    def handle_order_cancel(self, params: dict) -> dict:
        oid = str(params.get("order_id") or "")
        try:
            self._http("POST", "/sim/orders/cancel",
                       {"account_id": self._acct(), "order_id": int(oid)})
        except ValueError:
            raise AdapterError("not_found", f"order {oid!r}")
        self.emit("order.cancelled", {"order_id": oid,
                                      "client_id": self._client_ids.get(oid)})
        return {}

    def handle_order_amend(self, params: dict) -> dict:
        raise AdapterError("unsupported",
                           "this broker cannot amend a resting order; cancel "
                           "it and place a new one")

    def _position(self, position_id: str) -> dict:
        for p in self._http("GET", f"/sim/positions?account_id={self._acct()}"):
            if str(p["id"]) == str(position_id):
                return p
        raise AdapterError("not_found", f"position {position_id!r}")

    def handle_order_modify(self, params: dict) -> dict:
        pos = self._position(params.get("position_id"))
        # The engine's modify takes BOTH levels every call and reads a missing
        # one as "clear", so the current values are carried through when the
        # platform only sends one (SPEC: null clears, absent leaves alone).
        sl = params["sl"] if "sl" in params else pos.get("sl_price")
        tp = params["tp"] if "tp" in params else pos.get("tp_price")
        if params.get("trail_points"):
            raise AdapterError("unsupported", "this broker cannot trail")
        self._http("POST", "/sim/positions/modify",
                   {"account_id": self._acct(), "symbol": pos["symbol"],
                    "sl_price": sl or None, "tp_price": tp or None})
        return {}

    def handle_position_close(self, params: dict) -> dict:
        pos = self._position(params.get("position_id"))
        held = abs(float(pos["qty"]))
        want = float(params.get("qty") or 0) or held
        if want > held + 1e-9:
            raise AdapterError("invalid_qty",
                               f"{want} exceeds the {held} held")
        if want < held - 1e-9:
            # Partial: an opposing market order for the requested size. On a
            # netting book the engine nets it into the position, which is
            # exactly a partial exit; the platform sends one call either way.
            side = "sell" if float(pos["qty"]) > 0 else "buy"
            with self._place_lock:
                self._placing += 1
            try:
                res = self._http("POST", "/sim/orders", {
                    "account_id": self._acct(), "symbol": pos["symbol"],
                    "side": side, "otype": "market", "qty": want})
            finally:
                with self._place_lock:
                    self._placing -= 1
            if res.get("status") == "rejected":
                reason = (res.get("reason") or "rejected").strip()
                raise AdapterError(REASONS.get(reason.lower(), "internal"),
                                   reason)
            if res.get("status") == "working":
                try:
                    self._http("POST", "/sim/orders/cancel",
                               {"account_id": self._acct(),
                                "order_id": int(res.get("id"))})
                except (AdapterError, ValueError):
                    pass
                raise AdapterError("no_price",
                                   f"no current quote for {pos['symbol']}: "
                                   "the partial close could not be filled")
            return {}
        q = urllib.parse.urlencode({"account_id": self._acct(),
                                    "symbol": pos["symbol"]})
        res = self._http("POST", f"/sim/positions/close?{q}")
        # The engine answers a full close with the closing order's outcome,
        # and a rejection in it (a closed market, say) must surface: swallowed,
        # the platform reports "closed" while the position stays open.
        if isinstance(res, dict) and res.get("status") == "rejected":
            reason = (res.get("reason") or "rejected").strip()
            raise AdapterError(REASONS.get(reason.lower(), "internal"),
                               reason)
        return {}

    def handle_flatten_all(self, params: dict) -> dict:
        """Emulated: the engine has no close-everything call.

        SPEC 8.1 requires a platform to always be able to get flat, so the
        adapter provides it by closing one at a time and cancelling what
        rests. Reported as available because it works, not because the broker
        has the endpoint.
        """
        closed = 0
        for p in self._http("GET", f"/sim/positions?account_id={self._acct()}"):
            q = urllib.parse.urlencode({"account_id": self._acct(),
                                        "symbol": p["symbol"]})
            try:
                self._http("POST", f"/sim/positions/close?{q}")
                closed += 1
            except AdapterError:
                pass  # already gone, or its market has no quote right now
        for o in self._http("GET", f"/sim/orders?account_id={self._acct()}"):
            try:
                self._http("POST", "/sim/orders/cancel",
                           {"account_id": self._acct(),
                            "order_id": int(o["id"])})
            except AdapterError:
                pass
        return {"closed": closed}

    # -------------------------------------------------------- market data
    def handle_quotes_subscribe(self, params: dict) -> dict:
        syms = [s for s in params.get("symbols", []) if s]
        for s in syms:
            self._spec(s)          # unknown symbol fails loudly, not silently
        with self._quote_lock:
            self._quote_syms.update(syms)
            self._queue_quotes_locked(syms)
        # Subscription acknowledgement never waits for a price request or
        # its budget: orders use this same protocol read loop.
        self._quote_wakeup.set()
        return {}

    def handle_quotes_unsubscribe(self, params: dict) -> dict:
        with self._quote_lock:
            self._quote_syms.difference_update(s for s in params.get("symbols", []) if s)
            self._quote_pending = deque(s for s in self._quote_pending if s in self._quote_syms)
            self._quote_queued = set(self._quote_pending)
        self._quote_wakeup.set()
        return {}

    def _queue_quotes_locked(self, syms):
        for symbol in syms:
            if symbol in self._quote_syms and symbol not in self._quote_queued:
                self._quote_pending.append(symbol)
                self._quote_queued.add(symbol)

    def _set_session_state(self, component, data):
        """Quote recovery must not hide a degraded execution stream."""
        with self._session_lock:
            self._session_components[component] = dict(data)
            problems = [v for v in self._session_components.values()
                        if v.get("state", "connected") != "connected"]
            state = ("disconnected" if any(v.get("state") == "disconnected" for v in problems)
                     else "degraded" if problems else "connected")
            result = {"state": state, "detail": "; ".join(v.get("detail", "") for v in problems)}
            if result != self._session_reported:
                self._session_reported = result
                self.emit("session.state", result)

    def _quote_step(self):
        """One request at most, with a shared two-second budget across pages and primes."""
        if not self._quote_send_lock.acquire(blocking=False):
            return .1
        chunk = []
        try:
            with self._quote_lock:
                if self._stop.is_set():
                    return 60.0
                wait = self._quote_next_at - time.monotonic()
                if wait > 0:
                    return wait
                if not self._quote_pending:
                    self._queue_quotes_locked(sorted(self._quote_syms))
                while self._quote_pending and len(chunk) < 50:
                    symbol = self._quote_pending.popleft()
                    self._quote_queued.discard(symbol)
                    if symbol in self._quote_syms:
                        chunk.append(symbol)
                if not chunk:
                    return 60.0
                self._quote_next_at = time.monotonic() + 2.0
            q = urllib.parse.urlencode({"symbols": ",".join(chunk)})
            d = self._http("GET", f"/v1/prices?{q}", base=self.data_api)
            if self._stop.is_set():
                return 60.0
            if not isinstance(d, dict) or not isinstance(d.get("data"), list):
                raise AdapterError("internal", "invalid price response: expected data array", True)
            for row in d["data"]:
                bid, ask = row.get("bid"), row.get("ask")
                if bid is None or ask is None:
                    continue
                with self._quote_lock:
                    if row.get("symbol") not in self._quote_syms:
                        continue
                    self.emit("quote", {"symbol": row["symbol"],
                                        "bid": float(bid), "ask": float(ask),
                                        "time": _iso_ms(row.get("ts"))})
            self._set_session_state("quotes", {"state": "connected", "detail": ""})
            return 2.0
        except Exception as error:
            with self._quote_lock:
                retry = getattr(error, "retry_after_s", 2.0)
                self._quote_next_at = max(self._quote_next_at, time.monotonic() + retry)
                wait = max(2.0, self._quote_next_at - time.monotonic())
            self._set_session_state("quotes", {"state": "degraded",
                "detail": f"Quotes unavailable: {error}. Retrying in {wait:.0f}s."})
            return wait
        finally:
            # Round-robin pages keep held positions moving even while new
            # tickets are added. A subscription appears only once in the queue.
            with self._quote_lock:
                if chunk:
                    # Count from completion: even a variable request-setup
                    # delay cannot squeeze 31 attempts into a fixed minute.
                    self._quote_next_at = max(self._quote_next_at, time.monotonic() + 2.0)
                self._queue_quotes_locked(chunk)
            self._quote_send_lock.release()

    def handle_bars_history(self, params: dict) -> dict:
        sym = params.get("symbol", "")
        self._spec(sym)
        tf = params.get("timeframe", "1m")
        if tf not in TF_MS:
            raise AdapterError("unsupported", f"timeframe {tf!r}")
        try:
            count = int(params.get("count") or 100)
        except (TypeError, ValueError):
            raise AdapterError("unsupported", "bar count must be an integer")
        if not 1 <= count <= 100_000:
            raise AdapterError("unsupported", "bar count must be between 1 and 100000")
        # The data service's compatibility reader returns bare rows, newest first.
        # An exclusive UTC bucket boundary excludes the forming candle. This
        # also makes _poll_bars satisfy the protocol's closed-bar contract.
        width = TF_MS[tf] // 1000
        stop = int(time.time()) // width * width
        bars = []
        while len(bars) < count:
            page_size = min(count - len(bars), 5000)
            end = dt.datetime.fromtimestamp(stop, dt.timezone.utc).isoformat()
            q = urllib.parse.urlencode({"symbol": sym, "timeframe": tf,
                                        "order": "desc", "limit": page_size,
                                        "end": end})
            rows = self._http("GET", f"/vault/candles?{q}")
            if not isinstance(rows, list) or len(rows) > page_size:
                raise AdapterError("internal", "invalid candle response from LSE")
            page = []
            previous = stop * 1000
            try:
                for row in rows:
                    stamp = dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=dt.timezone.utc)
                    millis = int(stamp.timestamp() * 1000)
                    if millis >= previous:
                        raise ValueError("candle times do not move backwards")
                    previous = millis
                    page.append([millis, float(row["open"]), float(row["high"]),
                                 float(row["low"]), float(row["close"]),
                                 float(row.get("volume") or 0)])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise AdapterError("internal", f"invalid candle response from LSE: {exc}")
            bars.extend(page)
            if len(page) < page_size:
                break
            stop = page[-1][0] // 1000
        bars.reverse()
        return {"bars": bars}

    def handle_bars_subscribe(self, params: dict) -> dict:
        sym, tf = params.get("symbol", ""), params.get("timeframe", "1m")
        self._spec(sym)
        if tf not in TF_MS:
            raise AdapterError("unsupported", f"timeframe {tf!r}")
        self._bar_subs.add((sym, tf))
        return {}

    def handle_bars_unsubscribe(self, params: dict) -> dict:
        self._bar_subs.discard((params.get("symbol", ""),
                                params.get("timeframe", "1m")))
        return {}

    # ------------------------------------------------------------ events
    def _stream_forever(self):
        """Execution reports from the engine's own stream, translated.

        The engine already speaks this protocol's event shapes; what it cannot
        do is name the platform's `client_id`, because it has no such column.
        Every event is stamped from the order_id map on the way out, since a
        platform correlates a fill to the order it sent by that id and nothing
        else.

        Without the `websockets` package (the wheel declares no dependencies)
        there is no stream, so the adapter polls and says so: `session.state:
        degraded` is the protocol's word for exactly this, and a platform
        that is told can tell the user.
        """
        try:
            from websockets.sync.client import connect
        except ImportError:
            self._set_session_state("stream", {
                "state": "degraded",
                "detail": "no websocket client installed (pip install "
                          "websockets); fills are polled, not streamed"})
            self._stream_ready.set()
            self._poll_forever()
            return
        url = (self.api.replace("https://", "wss://")
               .replace("http://", "ws://") + "/sim/stream")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(url, user_agent_header=UA,
                             open_timeout=20) as ws:
                    ws.send(json.dumps({"api_key": self.key}))
                    # The snapshot frame IS the acknowledgement: the engine
                    # sends it only after the key checked out and this
                    # connection is registered for events. Anything earlier
                    # would call the stream ready while fills could still be
                    # emitted to nobody.
                    self._on_frame(json.loads(ws.recv()))
                    self._stream_ready.set()
                    self._set_session_state("stream",
                              {"state": "connected", "detail": ""})
                    backoff = 1.0
                    while not self._stop.is_set():
                        self._on_frame(json.loads(ws.recv()))
            except Exception as e:
                self._stream_ready.clear()
                if self._stop.is_set():
                    return
                self._set_session_state("stream",
                          {"state": "degraded",
                           "detail": f"stream reconnecting: {str(e)[:90]}"})
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            # Bars ride the same thread's cadence when the stream is up; the
            # engine streams ticks to nobody, so closed bars are polled.
            self._poll_bars()

    def _on_frame(self, msg: dict):
        if msg.get("type") == "snapshot":
            return  # the platform reconciles through catalog/account/positions
        name, data = msg.get("event"), dict(msg.get("data") or {})
        if not name:
            return
        oid = str(data.get("order_id") or "")
        if oid and not data.get("client_id"):
            if oid not in self._client_ids:
                self._await_client_id(oid)
            cid = self._client_ids.get(oid)
            if cid:
                data["client_id"] = cid
        if name == "account.updated":
            self.emit("account.updated", {
                "balance": data.get("balance"), "equity": data.get("equity"),
                "margin_used": data.get("margin_used"),
                "margin_free": data.get("margin_free"),
                "currency": data.get("currency") or self.account_currency})
            return
        if name == "order.rejected":
            reason = (data.get("message") or data.get("code") or "").strip()
            data["code"] = REASONS.get(reason.lower(),
                                       data.get("code") or "internal")
            data["message"] = reason
        if name in ("order.filled", "order.partial") and "position_id" in data:
            data["position_id"] = str(data["position_id"])
            if data.get("client_id"):
                self._pos_clients[data["position_id"]] = data["client_id"]
        if name == "position.closed" and "position_id" in data:
            data["position_id"] = str(data["position_id"])
            # swap and commission are REQUIRED on position.closed (SPEC 5):
            # the engine does not separate carry from P&L, and the spec's
            # word for that is an explicit 0, never an omission.
            data.setdefault("swap", 0)
            data.setdefault("commission", 0)
        self.emit(name, data)

    def _await_client_id(self, order_id: str, timeout: float = 3.0):
        """Briefly wait for the id map to catch up with the event.

        Only while a placement is actually in flight. An event the platform
        cannot correlate is worse than an event that arrives 20 ms late: the
        platform is told an order filled but not WHICH of its orders, and
        SPEC 4 gives it nothing else to match on.
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            with self._place_lock:
                if not self._placing:
                    return
            if order_id in self._client_ids:
                return
            time.sleep(0.01)

    def _poll_forever(self):
        """Fills without a stream: watch positions and orders change.

        Coarser than the stream by design, and announced as degraded rather
        than presented as equivalent.
        """
        seen_pos: dict = {}
        while not self._stop.is_set():
            time.sleep(2.0)
            try:
                rows = {str(p["id"]): p for p in self._http(
                    "GET", f"/sim/positions?account_id={self._acct()}")}
            except Exception:
                continue
            for pid, p in rows.items():
                if pid not in seen_pos:
                    self.emit("order.filled", {
                        "client_id": None, "order_id": None,
                        "position_id": pid, "symbol": p["symbol"],
                        "side": "buy" if float(p["qty"]) > 0 else "sell",
                        "qty": abs(float(p["qty"])),
                        "price": float(p["avg_price"]), "fees": 0,
                        "time": _iso_ms(p.get("opened_at"))})
            for pid in list(seen_pos):
                if pid not in rows:
                    self.emit("position.closed", {
                        "position_id": pid, "client_id": None, "qty": 0,
                        "price": 0, "realized_pnl": 0, "swap": 0,
                        "commission": 0, "reason": "manual",
                        "time": int(time.time() * 1000)})
            seen_pos = rows
            self._poll_bars()

    def _poll_bars(self):
        """Closed bars, polled: the engine's stream carries no market data."""
        for (sym, tf) in list(self._bar_subs):
            try:
                bars = self.handle_bars_history(
                    {"symbol": sym, "timeframe": tf, "count": 2})["bars"]
            except Exception:
                continue
            if not bars:
                continue
            last = bars[-1]
            key = (sym, tf)
            if self._bar_last.get(key) == last[0]:
                continue
            self._bar_last[key] = last[0]
            self.emit("bar", {"symbol": sym, "timeframe": tf, "bar": last})

    # ------------------------------------------------------------ quotes
    def start_quote_poll(self):
        def loop():
            while not self._stop.is_set():
                wait = self._quote_step()
                self._quote_wakeup.wait(min(60.0, max(.01, wait)))
                self._quote_wakeup.clear()
        with self._quote_lock:
            if self._quote_thread is not None and self._quote_thread.is_alive():
                return
            self._quote_thread = threading.Thread(target=loop, daemon=True, name="lse-sim-quotes")
            self._quote_thread.start()

    def on_shutdown(self):
        self._stop.set()
        self._quote_wakeup.set()


def main() -> int:
    ad = LSESimAdapter()
    ad.start_quote_poll()
    ad.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
