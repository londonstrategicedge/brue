"""brue.live: the trader-facing trading surface. One key, zero ceremony.

    import brue
    brue.login("lse_...")        # the only credential that ever appears
    brue.buy("XAUUSD", 20)       # routed to the paper account every LSE
    brue.positions()             # key gets by default
    brue.close("XAUUSD")

The broker relationship belongs to the LSE account, not the code: every key
trades the built-in paper account. Brue Connect is internal plumbing; users
never import it.

Safety rails, same doctrine as the strategy live driver: identity verified
before any order (brue.login), catalog tradability gate, lot-step rounding
with loud refusal, idempotent client order ids (a transport retry can never
double-fill), an append-only journal of every mutating call, and broker truth
read back after every action (fills and closes come from the broker's events,
never assumed).
"""
from __future__ import annotations

import atexit
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from . import auth


class BrueLiveError(Exception):
    """A trading call was refused, by brue's own rails or by the broker."""


def _state_root() -> Path:
    # Overridable so tests and build-time doc examples get a throwaway
    # account instead of touching the user's persistent paper state.
    return Path(os.environ.get("BRUE_PAPER_STATE")
                or Path.home() / ".local" / "state" / "brue")


_lock = threading.RLock()
_live: "_Session | None" = None


class _Session:
    """Owns the connector to the active broker adapter (paper for now)."""

    def __init__(self):
        acct = auth.account()  # raises BrueAuthError if not logged in
        try:
            from brueconnect import Connector, ConnectorError
        except ImportError:
            raise BrueLiveError(
                "the Brue Connect component is missing; reinstall with "
                "`pip install --upgrade brue-lang` to restore it") from None
        self._ConnectorError = ConnectorError
        root = _state_root()
        (root / "paper").mkdir(parents=True, exist_ok=True)
        self.journal_path = root / "journal.jsonl"
        # --balance seeds only a FRESH account; an existing paper account
        # (its state file) persists across sessions, that is the product.
        self.c = Connector([sys.executable, "-m",
                            "brueconnect.adapters.paper.paper_adapter",
                            "--state-dir", str(root / "paper"),
                            "--balance", "100000", "--warmup-bars", "240"])
        h = self.c.connect()
        if not self.c.is_paper:
            # A SCOPE decision, not a safety check: the connector already
            # refuses a mutating call on anything unarmed, and this surface
            # simply does not offer the arming ceremony yet. Asked via the
            # SDK's own predicate so "what counts as paper" is defined once,
            # in one place, for every surface.
            raise BrueLiveError(
                f"adapter mode {h.get('mode')!r} is not armable from this "
                "surface yet; only the paper account trades here today")
        # Unique per process: interactive calls are distinct orders by
        # design, but each order's id is stable across transport retries.
        self._token = secrets.token_hex(4)
        self._n = 0
        self.journal("session", {
            "broker": h.get("broker"), "mode": h.get("mode"),
            "key_id": acct.key_id, "user_id": acct.user_id,
            "plan_id": acct.plan_id})

    def journal(self, kind: str, payload: dict):
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time() * 1000), "kind": kind,
                                **payload}) + "\n")

    def spec(self, symbol: str) -> dict:
        s = self.c.catalog.get(symbol)
        if s is None:
            raise BrueLiveError(
                f"{symbol!r} is not in your broker's catalog "
                f"({', '.join(sorted(self.c.catalog))})")
        if not s.get("tradable", False):
            raise BrueLiveError(f"{symbol} is not tradable right now")
        return s

    def round_qty(self, spec: dict, qty: float) -> float:
        step = spec["lot_step"]
        r = round(round(float(qty) / step) * step, 10)
        if r < spec["min_qty"]:
            raise BrueLiveError(
                f"qty {qty} is below {spec['symbol']}'s minimum "
                f"{spec['min_qty']} (lot step {step})")
        return min(r, spec["max_qty"])

    def order(self, side: str, symbol: str, qty: float,
              sl: float | None = None, tp: float | None = None) -> dict:
        spec = self.spec(symbol)
        q = self.round_qty(spec, qty)
        self._n += 1
        params = {"client_id": f"py-{self._token}-{self._n}",
                  "symbol": symbol, "side": side, "qty": q}
        if sl is not None:
            params["sl"] = round(float(sl), spec["digits"])
        if tp is not None:
            params["tp"] = round(float(tp), spec["digits"])
        mark = self.c.mark()
        self.journal("order.place", params)
        try:
            try:
                res = self.c.request("order.place", params)
            except TimeoutError:
                # The order may or may not have reached the broker. Resending
                # the SAME client_id is safe by contract: the adapter either
                # replays the original result or places the order exactly
                # once. Without this, a slow pipe turns into an unknown fill.
                self.journal("order.retry", {"client_id": params["client_id"]})
                res = self.c.request("order.place", params)
        except self._ConnectorError as e:
            self.journal("order.refused", {"client_id": params["client_id"],
                                           "code": e.code, "message": str(e)})
            raise BrueLiveError(f"order refused by broker: {e}") from None
        # Broker truth, not assumption: the fill events (already emitted
        # before order.place returned) are the record we report from. A
        # large order may fill in parts; wait for the final 'order.filled'
        # then aggregate every part under this client_id.
        cid = params["client_id"]
        self.c.wait_event("order.filled",
                          lambda d: d.get("client_id") == cid,
                          timeout=10, since=mark)
        with self.c._event_cv:
            fills = [d for n, d in self.c.events
                     if n in ("order.filled", "order.partial")
                     and d.get("client_id") == cid]
        qty_f = sum(f["qty"] for f in fills)
        px = sum(f["qty"] * f["price"] for f in fills) / qty_f
        out = {"order_id": res.get("order_id"), "client_id": cid,
               "symbol": symbol, "side": side, "qty": round(qty_f, 10),
               "price": round(px, spec["digits"]),
               "fees": round(sum(f["fees"] for f in fills), 4),
               "position_id": fills[-1].get("position_id")}
        self.journal("order.filled", out)
        return out

    def close_positions(self, symbol: str | None, qty: float | None) -> list:
        poss = self.c.request("positions.list")["positions"]
        targets = [p for p in poss if symbol is None or p["symbol"] == symbol]
        if not targets:
            raise BrueLiveError(
                "no open position" + (f" in {symbol}" if symbol else ""))
        if qty is not None and len(targets) > 1:
            raise BrueLiveError(
                "qty= needs a single position; name the symbol")
        mark = self.c.mark()
        out = []
        for p in targets:
            params = {"position_id": p["position_id"]}
            if qty is not None:
                params["qty"] = float(qty)
            self.journal("position.close", params)
            try:
                self.c.request("position.close", params)
            except self._ConnectorError as e:
                raise BrueLiveError(f"close refused: {e}") from None
            evt = self.c.wait_event(
                "position.closed",
                lambda d, pid=p["position_id"]: d.get("position_id") == pid,
                timeout=10, since=mark)
            done = {"symbol": p["symbol"], "side": p["side"],
                    "qty": evt["qty"], "price": evt["price"],
                    "realized_pnl": evt["realized_pnl"]}
            self.journal("position.closed", done)
            out.append(done)
        return out


def _session() -> _Session:
    global _live
    with _lock:
        if _live is None or not _live.c.alive:
            _live = _Session()
        return _live


def buy(symbol: str, qty: float, *, sl: float | None = None,
        tp: float | None = None) -> dict:
    """Market-buy on your account's broker (paper by default).

        brue.buy("XAUUSD", 20)                      # 20 oz of gold
        brue.buy("EURUSD", 1.0, sl=1.05, tp=1.12)   # bracketed

    Returns the broker's fill: order_id, qty, price, fees, position_id.
    """
    return _session().order("buy", symbol, qty, sl=sl, tp=tp)


def sell(symbol: str, qty: float, *, sl: float | None = None,
         tp: float | None = None) -> dict:
    """Market-sell (short, or reduce a long on a netting account)."""
    return _session().order("sell", symbol, qty, sl=sl, tp=tp)


def close(symbol: str | None = None, *, qty: float | None = None) -> list:
    """Close open positions: close("XAUUSD") one symbol, close() all,
    qty= for a partial close. Returns each close with its realized_pnl."""
    return _session().close_positions(symbol, qty)


def positions():
    """Open positions as a pandas DataFrame (empty frame when flat)."""
    import pandas as pd
    rows = _session().c.request("positions.list")["positions"]
    cols = ["symbol", "side", "qty", "avg_price", "sl", "tp",
            "unrealized_pnl", "position_id"]
    return pd.DataFrame(rows, columns=cols)


def balance() -> dict:
    """The trading account: balance, equity, margin_used, margin_free."""
    return _session().c.request("account.get")


def instruments():
    """Everything your broker lets you trade, as a pandas DataFrame."""
    import pandas as pd
    cols = ["symbol", "name", "asset_class", "digits", "lot_step",
            "min_qty", "max_qty", "contract_size", "margin_rate", "tradable"]
    rows = [{k: s.get(k) for k in cols}
            for s in _session().c.catalog.values()]
    return pd.DataFrame(rows, columns=cols).sort_values(
        "symbol", ignore_index=True)


def history(symbol: str, timeframe: str = "1m", count: int = 100):
    """Recent OHLCV bars from the broker feed as a pandas frame: lowercase
    columns, UTC DatetimeIndex. Compute indicators in plain Python on the
    result."""
    import pandas as pd
    s = _session()
    s.spec(symbol)
    try:
        bars = s.c.request("bars.history", {
            "symbol": symbol, "timeframe": timeframe, "count": int(count)})
    except s._ConnectorError as e:
        raise BrueLiveError(f"history refused: {e}") from None
    df = pd.DataFrame(bars["bars"], columns=[
        "time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df.set_index("time")


def flatten() -> dict:
    """Close everything, one call. Returns {'closed': n}."""
    s = _session()
    s.journal("flatten_all", {})
    return s.c.request("flatten_all")


def disconnect() -> None:
    """End the trading session (positions persist in the paper account)."""
    global _live
    with _lock:
        if _live is not None:
            _live.c.close()
            _live = None


# The adapter subprocess must not outlive the interpreter; positions and
# balance persist via the adapter's own state file, not the process.
atexit.register(disconnect)
