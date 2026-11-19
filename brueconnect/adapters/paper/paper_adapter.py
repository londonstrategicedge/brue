"""The built-in paper broker. Implements every capability of SPEC v1.

This adapter is deliberately the strictest broker in the set: idempotent orders,
session gating, lot-step validation, margin checks, server-side brackets with
stop-beats-target, netting or hedging account models, persistent state across
restarts, and a deterministic synthetic market so every test run is
reproducible. If a platform works against this, a lenient real broker is the
easy case.

Market model: each instrument has a seeded random-walk quote path on a
synthetic clock (default 2000 ms of market time per tick, one tick every
--tick-ms of real time). Bars aggregate from the same path, so history and
stream can never disagree.

Usage: python3 paper_adapter.py [--state-dir D] [--model netting|hedging]
                                [--tick-ms 25] [--sim-step-ms 2000]
                                [--seed 7] [--balance 10000]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Importable three ways: as brueconnect.adapters.paper (installed wheel),
# as a file from a checkout (bootstrap the repo root), or via -m from the
# repo root. The wheel path needs no sys.path surgery at all.
try:
    from brueconnect import Adapter, AdapterError
    from brueconnect.protocol import TIMEFRAMES_MS
except ImportError:  # file-run from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from brueconnect import Adapter, AdapterError
    from brueconnect.protocol import TIMEFRAMES_MS

T0 = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

INSTRUMENTS = [
    {"symbol": "EURUSD", "name": "Euro vs US Dollar", "asset_class": "fx",
     "base_ccy": "EUR", "quote_ccy": "USD", "digits": 5, "tick_size": 0.00001,
     "tick_value": 1.0, "lot_step": 0.01, "min_qty": 0.01, "max_qty": 500.0,
     "min_notional": 0, "contract_size": 100000,
     "margin_rate": 0.0333, "margin_maintenance_rate": 0.01665,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "00:00", "close": "24:00",
                   "tz": "UTC"}],
     "holidays": ["2026-12-25"], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 0,
     "fees": {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
              "min": 0.0},
     "swap_long": -0.6, "swap_short": 0.1, "swap_mode": "points",
     "swap_3day_dow": 3,
     "_price0": 1.08500, "_spread": 0.00012, "_sigma": 0.00018,
     "_partial_above": 5.0},
    {"symbol": "AAPL", "name": "Apple Inc", "asset_class": "stock",
     "base_ccy": "AAPL", "quote_ccy": "USD", "digits": 2, "tick_size": 0.01,
     "tick_value": 0.01, "lot_step": 1.0, "min_qty": 1.0, "max_qty": 10000.0,
     "min_notional": 0, "contract_size": 1,
     "margin_rate": 0.25, "margin_maintenance_rate": 0.125,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "09:30", "close": "16:00",
                   "tz": "America/New_York"}],
     "holidays": ["2026-12-25", "2026-01-01"], "tradable": True,
     "shortable": True, "trade_mode": "full", "stops_level_points": 0,
     # A per-share rate: mode/rate/min say so explicitly, and
     # commission_per_lot carries the same number for SPEC 1.0 readers.
     "fees": {"commission_per_lot": 0.005, "mode": "per_share", "rate": 0.005,
              "min": 1.0},
     "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
     "swap_3day_dow": 5,
     "_price0": 212.40, "_spread": 0.04, "_sigma": 0.0009,
     "_partial_above": 500.0},
    # Spot-style like BTCUSD (contract_size 1): qty is ounces, so the natural
    # sentence "buy 20 gold" is literally brue.buy("XAUUSD", 20). A lot-based
    # gold contract (100 oz/lot) would make qty=20 a $6M+ order that no demo
    # account can margin.
    {"symbol": "XAUUSD", "name": "Gold vs US Dollar (spot)",
     "asset_class": "metal",
     "base_ccy": "XAU", "quote_ccy": "USD", "digits": 2, "tick_size": 0.01,
     "tick_value": 0.01, "lot_step": 0.01, "min_qty": 0.01, "max_qty": 5000.0,
     "min_notional": 0, "contract_size": 1,
     "margin_rate": 0.05, "margin_maintenance_rate": 0.025,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "00:00", "close": "24:00",
                   "tz": "UTC"}],
     "holidays": ["2026-12-25"], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 0,
     "fees": {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
              "min": 0.0},
     "swap_long": -1.9, "swap_short": 0.9, "swap_mode": "points",
     "swap_3day_dow": 3,
     "_price0": 3327.50, "_spread": 0.35, "_sigma": 0.0004,
     "_partial_above": 200.0},
    {"symbol": "BTCUSD", "name": "Bitcoin vs US Dollar", "asset_class": "crypto",
     "base_ccy": "BTC", "quote_ccy": "USD", "digits": 1, "tick_size": 0.1,
     "tick_value": 0.1, "lot_step": 0.001, "min_qty": 0.001, "max_qty": 100.0,
     "min_notional": 10, "contract_size": 1,
     "margin_rate": 0.5, "margin_maintenance_rate": 0.25,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                   "close": "24:00", "tz": "UTC"}],
     "holidays": [], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 0,
     "fees": {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
              "min": 0.0},
     "swap_long": 0.0, "swap_short": 0.0, "swap_mode": "none",
     "_price0": 67200.0, "_spread": 12.0, "_sigma": 0.0011,
     "_partial_above": 5.0},
    # Permanently closed instrument so the market_closed path is testable
    # deterministically, whatever the clock says.
    {"symbol": "XCLOSED", "name": "Always Closed", "asset_class": "test",
     "base_ccy": "X", "quote_ccy": "USD", "digits": 2, "tick_size": 0.01,
     "lot_step": 1.0, "min_qty": 1.0, "max_qty": 10.0, "contract_size": 1,
     "margin_rate": 1.0, "sessions": [], "holidays": [], "tradable": True,
     "shortable": True, "fees": {"commission_per_lot": 0.0},
     "swap_long": 0.0, "swap_short": 0.0,
     "_price0": 100.0, "_spread": 0.02, "_sigma": 0.0, "_partial_above": 1e9},
]

class Walk:
    """Deterministic quote path per symbol; extended lazily, never mutated."""

    def __init__(self, spec: dict, seed: int):
        self.spec = spec
        self.rng = random.Random(f"{seed}:{spec['symbol']}")
        self.mids = [spec["_price0"]]

    def mid(self, i: int) -> float:
        while len(self.mids) <= i:
            p = self.mids[-1]
            p *= 1.0 + self.rng.gauss(0.0, self.spec["_sigma"])
            self.mids.append(max(p, self.spec["tick_size"]))
        return self.mids[i]

    def quote(self, i: int) -> tuple[float, float]:
        m = self.mid(i)
        h = self.spec["_spread"] / 2.0
        d = self.spec["digits"]
        return round(m - h, d), round(m + h, d)


class PaperAdapter(Adapter):
    name = "paper"
    version = "0.1"
    broker = "brue paper simulator"
    mode = "paper"

    def __init__(self, args, instruments=None):
        super().__init__()
        self.args = args
        # Injectable so derived brokers (fake/demo brokers with their own
        # catalog and terms) reuse the whole fill engine unchanged; the
        # bundled paper catalog is only the default.
        self.instruments = instruments if instruments is not None else INSTRUMENTS
        # Union across instruments, read absence-tolerant in catalog.list:
        # SPEC 1.1 catalog fields are optional, so entries may legitimately
        # carry different key sets (XCLOSED stays a minimal 1.0-style entry
        # to keep that path tested).
        self.public_fields = sorted({k for spec in self.instruments for k in spec
                                     if not k.startswith("_")})
        self.account_model = args.model
        self.lockstate = threading.RLock()
        self.walks = {s["symbol"]: Walk(s, args.seed) for s in self.instruments}
        self.specs = {s["symbol"]: s for s in self.instruments}
        self.tick_index = 0
        self.balance = float(args.balance)
        self.positions: dict[str, dict] = {}
        self.fills: list[dict] = []
        self._pid = 0
        self.quote_subs: set[str] = set()
        self.bar_subs: set[tuple[str, str]] = set()
        # Pre-elapse market time so history exists at connect: N complete
        # 1m bars (the walk is deterministic, so this is pure fast-forward).
        if args.warmup_bars > 0:
            self.tick_index = int(args.warmup_bars * 60_000 / args.sim_step_ms) + 1
        self._load_state()

    def broker_info(self) -> dict:
        # Not a broker and says so: no website, no licence, no country. A
        # picker showing this next to an actual broker must read as obviously
        # different, and the honest way to get that is to leave the fields
        # a broker fills in empty.
        return {"display_name": "Paper simulator",
                "tagline": "Built in, offline, synthetic prices",
                "mark": "PS",
                "asset_classes": ["FX", "Metals", "Indices"]}

    # == synthetic market ================================================
    def now_ms(self) -> int:
        return T0 + self.tick_index * self.args.sim_step_ms

    def quote_at(self, symbol: str, i: int | None = None) -> tuple[float, float]:
        return self.walks[symbol].quote(self.tick_index if i is None else i)

    def clock_loop(self):
        while True:
            time.sleep(self.args.tick_ms / 1000.0)
            with self.lockstate:
                self.tick_index += 1
                i, t = self.tick_index, self.now_ms()
                for sym in list(self.quote_subs):
                    bid, ask = self.quote_at(sym, i)
                    self.emit("quote", {"symbol": sym, "bid": bid, "ask": ask,
                                        "time": t})
                for (sym, tf) in list(self.bar_subs):
                    self._maybe_emit_bar(sym, tf, i)
                self._check_brackets()
                self._check_stop_out()

    def _bar_for(self, sym: str, tf_ms: int, bucket: int) -> list | None:
        """Aggregate one closed bar from the quote path, mid prices."""
        i0 = max(0, (bucket * tf_ms - T0) // self.args.sim_step_ms)
        i1 = ((bucket + 1) * tf_ms - T0) // self.args.sim_step_ms
        if i1 <= i0:
            return None
        w = self.walks[sym]
        mids = [w.mid(i) for i in range(int(i0), int(i1))]
        d = self.specs[sym]["digits"]
        return [bucket * tf_ms, round(mids[0], d), round(max(mids), d),
                round(min(mids), d), round(mids[-1], d), float(len(mids))]

    def _maybe_emit_bar(self, sym: str, tf: str, i: int):
        tf_ms = TIMEFRAMES_MS[tf]
        t_prev = T0 + (i - 1) * self.args.sim_step_ms
        t_now = T0 + i * self.args.sim_step_ms
        if t_prev // tf_ms != t_now // tf_ms:  # bucket boundary crossed
            bar = self._bar_for(sym, tf_ms, t_prev // tf_ms)
            if bar:
                self.emit("bar", {"symbol": sym, "timeframe": tf, "bar": bar})

    # == sessions ========================================================
    def is_open(self, spec: dict, t_ms: int) -> bool:
        if not spec["sessions"]:
            return False
        for s in spec["sessions"]:
            local = datetime.fromtimestamp(t_ms / 1000.0, ZoneInfo(s["tz"]))
            if local.strftime("%Y-%m-%d") in spec["holidays"]:
                continue
            if local.isoweekday() not in s["days"]:
                continue
            oh, om = map(int, s["open"].split(":"))
            if s["close"] == "24:00":
                ch, cm = 24, 0
            else:
                ch, cm = map(int, s["close"].split(":"))
            hm = local.hour * 60 + local.minute
            if oh * 60 + om <= hm < ch * 60 + cm:
                return True
        return False

    # == account math ====================================================
    def _unrealized(self, p: dict) -> float:
        bid, ask = self.quote_at(p["symbol"])
        spec = self.specs[p["symbol"]]
        px = bid if p["side"] == "buy" else ask
        sign = 1.0 if p["side"] == "buy" else -1.0
        return sign * (px - p["avg_price"]) * p["qty"] * spec["contract_size"]

    def _margin_of(self, symbol: str, qty: float, price: float) -> float:
        spec = self.specs[symbol]
        return abs(qty) * spec["contract_size"] * price * spec["margin_rate"]

    def _account(self) -> dict:
        unreal = sum(self._unrealized(p) for p in self.positions.values())
        used = sum(self._margin_of(p["symbol"], p["qty"], p["avg_price"])
                   for p in self.positions.values())
        equity = self.balance + unreal
        return {"balance": round(self.balance, 2), "equity": round(equity, 2),
                "margin_used": round(used, 2),
                "margin_free": round(equity - used, 2), "currency": "USD"}

    # == handlers ========================================================
    def capabilities(self):
        return {"orders": {"market": True, "brackets": True, "modify": True,
                           "pending": False, "trailing": False,
                           "partial_close": True},
                "data": {"quotes": True, "bars": True, "history": True},
                "flatten_all": True}

    def handle_catalog_list(self, params):
        with self.lockstate:
            return {"instruments": [{k: s[k] for k in self.public_fields if k in s}
                                    for s in self.instruments]}

    def handle_account_get(self, params):
        with self.lockstate:
            return self._account()

    def handle_quotes_subscribe(self, params):
        with self.lockstate:
            for s in params.get("symbols", []):
                if s not in self.specs:
                    raise AdapterError("unknown_symbol", f"{s!r} not in catalog")
                self.quote_subs.add(s)
        return {}

    def handle_quotes_unsubscribe(self, params):
        with self.lockstate:
            for s in params.get("symbols", []):
                self.quote_subs.discard(s)
        return {}

    def handle_bars_history(self, params):
        sym, tf = params.get("symbol"), params.get("timeframe")
        count = int(params.get("count") or 100)
        if sym not in self.specs:
            raise AdapterError("unknown_symbol", f"{sym!r} not in catalog")
        if tf not in TIMEFRAMES_MS:
            raise AdapterError("unsupported", f"timeframe {tf!r}")
        with self.lockstate:
            tf_ms = TIMEFRAMES_MS[tf]
            last_complete = (self.now_ms() // tf_ms) - 1
            first = max(T0 // tf_ms, last_complete - count + 1)
            bars = []
            for b in range(int(first), int(last_complete) + 1):
                bar = self._bar_for(sym, tf_ms, b)
                if bar:
                    bars.append(bar)
            return {"bars": bars}

    def handle_bars_subscribe(self, params):
        sym, tf = params.get("symbol"), params.get("timeframe")
        if sym not in self.specs:
            raise AdapterError("unknown_symbol", f"{sym!r} not in catalog")
        if tf not in TIMEFRAMES_MS:
            raise AdapterError("unsupported", f"timeframe {tf!r}")
        with self.lockstate:
            self.bar_subs.add((sym, tf))
        return {}

    def handle_bars_unsubscribe(self, params):
        with self.lockstate:
            self.bar_subs.discard((params.get("symbol"), params.get("timeframe")))
        return {}

    def handle_positions_list(self, params):
        with self.lockstate:
            out = []
            for p in self.positions.values():
                q = dict(p)
                q["unrealized_pnl"] = round(self._unrealized(p), 2)
                out.append(q)
            return {"positions": out}

    def handle_order_place(self, params):
        with self.lockstate:
            cid = str(params.get("client_id") or "")
            if not cid:
                raise AdapterError("invalid_qty", "client_id is required")
            prior = self.idempotent_replay(cid)
            if prior is not None:
                if prior.get("_params") != self._order_sig(params):
                    raise AdapterError("duplicate_client_id",
                                       "client_id reused with different params")
                return {k: v for k, v in prior.items() if not k.startswith("_")}
            sym = params.get("symbol")
            spec = self.specs.get(sym)
            if spec is None:
                raise AdapterError("unknown_symbol", f"{sym!r} not in catalog")
            if not spec["tradable"]:
                raise AdapterError("market_closed", f"{sym} is not tradable")
            if not self.is_open(spec, self.now_ms()):
                raise AdapterError("market_closed",
                                   f"{sym} session is closed at "
                                   f"{self.now_ms()}", retryable=True)
            side = params.get("side")
            if side not in ("buy", "sell"):
                raise AdapterError("invalid_qty", "side must be buy|sell")
            qty = float(params.get("qty") or 0.0)
            step = spec["lot_step"]
            if qty < spec["min_qty"] or qty > spec["max_qty"]:
                raise AdapterError("invalid_qty",
                                   f"qty {qty} outside [{spec['min_qty']}, "
                                   f"{spec['max_qty']}]")
            if abs(qty / step - round(qty / step)) > 1e-9:
                raise AdapterError("invalid_qty",
                                   f"qty {qty} is not a multiple of lot_step {step}")
            bid, ask = self.quote_at(sym)
            sl = params.get("sl")
            tp = params.get("tp")
            # stops_level enforcement (SPEC 1.1): a declared distance MUST be
            # rejected on violation, never silently adjusted. SL/TP execute on
            # the exit side, so the distance is measured from that side.
            dist = float(spec.get("stops_level_points") or 0) * spec["tick_size"]
            if dist > 0:
                exit_px = bid if side == "buy" else ask
                for label, level, ok in (
                        ("sl", sl, lambda v: (exit_px - v if side == "buy"
                                              else v - exit_px) >= dist),
                        ("tp", tp, lambda v: (v - exit_px if side == "buy"
                                              else exit_px - v) >= dist)):
                    if level is not None and not ok(float(level)):
                        raise AdapterError(
                            "invalid_stops",
                            f"{label} {level} is within {spec['stops_level_points']}"
                            f" points of {exit_px} (min distance {dist:.{spec['digits']}f})")
            px = ask if side == "buy" else bid
            need = self._margin_of(sym, qty, px)
            acct = self._account()
            if need > acct["margin_free"]:
                raise AdapterError("insufficient_margin",
                                   f"need {need:.2f}, free {acct['margin_free']:.2f}")
            self._pid += 1
            oid = f"o{self._pid}"
            result = {"order_id": oid, "status": "accepted",
                      "_params": self._order_sig(params)}
            self.idempotent_store(cid, result)
            self.emit("order.accepted", {"client_id": cid, "order_id": oid})
            if qty >= spec["_partial_above"]:
                half = round(qty / 2 / step) * step
                self._fill(cid, oid, sym, side, half, px, sl, tp,
                           partial_remaining=round(qty - half, 8))
                self._fill(cid, oid, sym, side, round(qty - half, 8), px, sl, tp)
            else:
                self._fill(cid, oid, sym, side, qty, px, sl, tp)
            self._save_state()
            return {k: v for k, v in result.items() if not k.startswith("_")}

    @staticmethod
    def _order_sig(params: dict) -> list:
        return [params.get("symbol"), params.get("side"),
                params.get("qty"), params.get("sl"), params.get("tp")]

    def _fill(self, cid, oid, sym, side, qty, px, sl, tp,
              partial_remaining: float = 0.0):
        spec = self.specs[sym]
        fees = round(spec["fees"]["commission_per_lot"] * qty, 4)
        self.balance -= fees
        t = self.now_ms()
        pid = self._apply_fill(cid, sym, side, qty, px, sl, tp)
        # A dedup id per execution (SPEC 5). This simulator emits each fill
        # exactly once, so a fresh id is correct; an at-least-once venue
        # would key it on the broker's own execution id instead.
        fill = {"fill_id": self.next_fill_id(), "client_id": cid,
                "order_id": oid, "position_id": pid,
                "symbol": sym, "side": side, "qty": qty, "price": px,
                "fees": fees, "time": t}
        self.fills.append(fill)
        if partial_remaining > 0:
            self.emit("order.partial", dict(fill, remaining=partial_remaining))
        else:
            self.emit("order.filled", fill)
        self.emit("account.updated", self._account())

    def _apply_fill(self, cid, sym, side, qty, px, sl, tp) -> str:
        if self.account_model == "hedging":
            self._pid += 1
            pid = f"p{self._pid}"
            self.positions[pid] = {"position_id": pid, "client_id": cid,
                                   "symbol": sym, "side": side, "qty": qty,
                                   "avg_price": px, "sl": sl, "tp": tp,
                                   "opened_at": self.now_ms()}
            return pid
        # netting: one position per symbol
        pid = f"net-{sym}"
        p = self.positions.get(pid)
        if p is None:
            self.positions[pid] = {"position_id": pid, "client_id": cid,
                                   "symbol": sym, "side": side, "qty": qty,
                                   "avg_price": px, "sl": sl, "tp": tp,
                                   "opened_at": self.now_ms()}
            return pid
        if p["side"] == side:
            total = p["qty"] + qty
            p["avg_price"] = (p["avg_price"] * p["qty"] + px * qty) / total
            p["qty"] = total
            if sl is not None:
                p["sl"] = sl
            if tp is not None:
                p["tp"] = tp
            return pid
        # opposite side: reduce, close, or flip
        if qty < p["qty"] - 1e-12:
            self._realize(p, qty, px, "manual")
            p["qty"] = round(p["qty"] - qty, 8)
            return pid
        remainder = round(qty - p["qty"], 8)
        self._realize(p, p["qty"], px, "manual")
        del self.positions[pid]
        self.emit("position.closed", self._closed_evt(p, p["qty"], px, "manual"))
        if remainder > 1e-12:
            self.positions[pid] = {"position_id": pid, "client_id": cid,
                                   "symbol": sym, "side": side,
                                   "qty": remainder, "avg_price": px,
                                   "sl": sl, "tp": tp,
                                   "opened_at": self.now_ms()}
        return pid

    def _realize(self, p: dict, qty: float, px: float, reason: str) -> float:
        spec = self.specs[p["symbol"]]
        sign = 1.0 if p["side"] == "buy" else -1.0
        pnl = sign * (px - p["avg_price"]) * qty * spec["contract_size"]
        self.balance += pnl
        return pnl

    def _closed_evt(self, p, qty, px, reason):
        spec = self.specs[p["symbol"]]
        sign = 1.0 if p["side"] == "buy" else -1.0
        pnl = sign * (px - p["avg_price"]) * qty * spec["contract_size"]
        # swap and commission are separate from realized_pnl on purpose: a
        # platform that cannot split market movement from carry and fees
        # shows the holder a number that hides what the position cost to
        # hold. This simulator charges neither, so it reports an explicit
        # zero rather than omitting the fields and leaving the platform to
        # guess whether they were zero or simply unavailable.
        return {"close_id": self.next_fill_id(),
                "position_id": p["position_id"], "client_id": p["client_id"],
                "qty": qty, "price": px, "realized_pnl": round(pnl, 2),
                "swap": round(p.get("swap", 0.0), 2),
                "commission": round(p.get("commission", 0.0), 2),
                "reason": reason, "time": self.now_ms()}

    def handle_order_modify(self, params):
        with self.lockstate:
            p = self.positions.get(params.get("position_id"))
            if p is None:
                raise AdapterError("not_found", "no such position")
            if "sl" in params:
                p["sl"] = params["sl"]
            if "tp" in params:
                p["tp"] = params["tp"]
            self._save_state()
            return {}

    def handle_position_close(self, params):
        with self.lockstate:
            p = self.positions.get(params.get("position_id"))
            if p is None:
                raise AdapterError("not_found", "no such position")
            qty = float(params.get("qty") or p["qty"])
            qty = min(qty, p["qty"])
            self._close_position(p, qty, "manual")
            self._save_state()
            return {}

    def _close_position(self, p: dict, qty: float, reason: str,
                        at_price: float | None = None):
        bid, ask = self.quote_at(p["symbol"])
        px = at_price if at_price is not None else (bid if p["side"] == "buy" else ask)
        self._realize(p, qty, px, reason)
        evt = self._closed_evt(p, qty, px, reason)
        if qty >= p["qty"] - 1e-12:
            self.positions.pop(p["position_id"], None)
        else:
            p["qty"] = round(p["qty"] - qty, 8)
        self.emit("position.closed", evt)
        self.emit("account.updated", self._account())

    def handle_flatten_all(self, params):
        with self.lockstate:
            n = len(self.positions)
            for p in list(self.positions.values()):
                self._close_position(p, p["qty"], "flatten")
            self._save_state()
            return {"closed": n}

    def handle_history_fills(self, params):
        with self.lockstate:
            f = params.get("from")
            t = params.get("to")
            out = [x for x in self.fills
                   if (f is None or x["time"] >= f) and (t is None or x["time"] <= t)]
            return {"fills": out}

    # == brackets and stop-out (clock thread) ============================
    def _check_brackets(self):
        for p in list(self.positions.values()):
            bid, ask = self.quote_at(p["symbol"])
            mark = bid if p["side"] == "buy" else ask
            sl, tp = p.get("sl"), p.get("tp")
            hit_sl = sl is not None and (mark <= sl if p["side"] == "buy" else mark >= sl)
            hit_tp = tp is not None and (mark >= tp if p["side"] == "buy" else mark <= tp)
            if hit_sl:  # stop beats target, always
                self._close_position(p, p["qty"], "sl", at_price=sl)
            elif hit_tp:
                self._close_position(p, p["qty"], "tp", at_price=tp)
        if self.positions:
            self._save_state()

    def _check_stop_out(self):
        acct = self._account()
        if acct["margin_used"] <= 0:
            return
        level = acct["equity"] / acct["margin_used"]
        if level < 0.5 and self.positions:
            self.emit("margin.warning", {"level": round(level, 4)})
            worst = min(self.positions.values(), key=self._unrealized)
            self._close_position(worst, worst["qty"], "stop_out")
            self._save_state()

    # == persistence (reconnect survives a process restart) ==============
    def _state_file(self) -> Path | None:
        if not self.args.state_dir:
            return None
        d = Path(self.args.state_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d / "paper_state.json"

    def _save_state(self):
        f = self._state_file()
        if not f:
            return
        state = {"balance": self.balance, "positions": self.positions,
                 "fills": self.fills[-5000:], "pid": self._pid,
                 "tick_index": self.tick_index,
                 "idempotent": self._idempotent}
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, f)

    def _load_state(self):
        f = self._state_file()
        if not f or not f.exists():
            return
        state = json.loads(f.read_text())
        self.balance = state["balance"]
        self.positions = state["positions"]
        self.fills = state["fills"]
        self._pid = state["pid"]
        self.tick_index = state["tick_index"]
        self._idempotent = state["idempotent"]

    def on_shutdown(self):
        with self.lockstate:
            self._save_state()


def main():
    ap = argparse.ArgumentParser()
    # BRUE_BROKER_STATE is the platform's door for "your private state dir"
    # (SPEC 8.5): the platform sets only the env, never the flag, so an
    # adapter that reads only the flag silently loses the whole paper book
    # (positions, fills, balance) on every restart. The flag still wins when
    # given, which is how conformance pins a scratch dir.
    ap.add_argument("--state-dir",
                    default=os.environ.get("BRUE_BROKER_STATE", ""))
    ap.add_argument("--model", choices=["netting", "hedging"], default="netting")
    ap.add_argument("--tick-ms", type=int, default=25)
    ap.add_argument("--sim-step-ms", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--warmup-bars", type=int, default=0,
                    help="start with N complete 1m bars of history")
    args = ap.parse_args()
    a = PaperAdapter(args)
    threading.Thread(target=a.clock_loop, daemon=True).start()
    a.emit("session.state", {"state": "connected", "detail": "paper ready"})
    a.run()


if __name__ == "__main__":
    main()
