"""The live driver: run a Brue script against any brue-connect adapter.

Doctrine (provable backtest parity): on every CLOSED bar the driver re-runs
the script over the full window with capture_intents and reads the decisions
of the final bar. The script's state namespaces are its own deterministic
simulation of the window, exactly as in a backtest of the same bars; the
driver translates the final-bar intents into connector orders. Client order
ids are deterministic functions of (run key, bar time, intent), so a
restarted driver that re-derives the same decision re-sends the same id and
the adapter's idempotency absorbs it: warmup replay and crash recovery are
the same code path.

Safety: tradability gate from the catalog before every send, lot-step
rounding with loud refusal, an audit journal of every mutating call, and
optional flatten-on-exit. Points-based exits (profit=/loss=) need a fill
price the driver does not predict; the driver forwards absolute sl/tp only
and refuses the rest loudly.

Usage, from the repository root:
  python3 -m brueconnect.runner.live_driver --script demo/trend.brue \
      --symbol EURUSD --timeframe 1m \
      --adapter "python3 -m brueconnect.adapters.paper.paper_adapter --warmup-bars 240" \
      --warmup 60 --bars 40 --flatten-on-exit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# Override for the Brue engine: BRUE_ENGINE_PATH, or a `brue` checkout next
# to this repo. In a normal install neither exists and the installed brue
# package is used.
_engine = os.environ.get("BRUE_ENGINE_PATH") or str(ROOT.parent / "brue")
if os.path.isdir(_engine):
    sys.path.insert(0, _engine)

from brueconnect import Connector, ConnectorError  # noqa: E402
from brue import run_script_full  # noqa: E402
from brue.runtime import BarData, ExternalData, RunOptions  # noqa: E402


class LiveDriver:
    def __init__(self, args):
        self.args = args
        self.source = Path(args.script).read_text(encoding="utf-8")
        self.symbol = args.symbol
        self.tf = args.timeframe
        # Stable across restarts: same script+market -> same run key -> the
        # re-derived decision for a bar reuses the same client_id.
        self.run_key = hashlib.sha256(
            f"{self.source}|{self.symbol}|{self.tf}".encode()).hexdigest()[:10]
        if getattr(args, "broker", None):
            self.c = Connector.for_broker(args.broker)
        else:
            self.c = Connector(shlex.split(args.adapter))
        self.window: list[BarData] = []
        self.entry_map: dict[str, str] = {}  # script entry id -> client_id
        self.placed = 0
        self.skipped = 0
        self.journal_path = Path(args.journal) if args.journal else None

    # == plumbing ========================================================
    def journal(self, kind: str, payload: dict):
        if not self.journal_path:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a") as f:
            f.write(json.dumps({"t": int(time.time() * 1000), "kind": kind,
                                **payload}) + "\n")

    def log(self, msg: str):
        print(f"[driver] {msg}", flush=True)

    # == market rules ====================================================
    def spec(self) -> dict:
        return self.c.catalog[self.symbol]

    def is_open(self, t_ms: int) -> bool:
        spec = self.spec()
        if not spec["sessions"]:
            return False
        for s in spec["sessions"]:
            local = datetime.fromtimestamp(t_ms / 1000.0, ZoneInfo(s["tz"]))
            if local.strftime("%Y-%m-%d") in spec["holidays"]:
                continue
            if local.isoweekday() not in s["days"]:
                continue
            oh, om = map(int, s["open"].split(":"))
            ch, cm = (24, 0) if s["close"] == "24:00" else map(int, s["close"].split(":"))
            hm = local.hour * 60 + local.minute
            if oh * 60 + om <= hm < ch * 60 + cm:
                return True
        return False

    def round_qty(self, qty: float) -> float | None:
        spec = self.spec()
        step = spec["lot_step"]
        r = round(qty / step) * step
        r = round(r, 10)
        if r < spec["min_qty"]:
            return None
        return min(r, spec["max_qty"])

    # == the loop ========================================================
    def run(self) -> int:
        # Identity gate BEFORE the broker handshake. Paper runs never need a
        # key (local, open-source). Arming LIVE requires a verified LSE
        # account: brue.login() against the LSE key check, identity
        # journaled. A key offered without --arm-live is verified too (fail
        # loud beats discovering a dead key at arm time).
        account = None
        if self.args.arm_live or self.args.api_key:
            from brue.auth import BrueAuthError, login
            try:
                account = login(api_key=self.args.api_key or None)
            except BrueAuthError as e:
                self.log(f"account verification failed: {e}")
                if self.args.arm_live:
                    self.log("--arm-live requires a verified LSE API key "
                             "(--api-key, BRUE_API_KEY or LSE_API_KEY)")
                    return 2
            if account is not None:
                self.log(f"account verified: plan={account.plan_id} "
                         f"key={account.key_id[:8]}...")
                self.journal("account", {
                    "key_id": account.key_id, "user_id": account.user_id,
                    "plan_id": account.plan_id,
                    "environment": account.environment})

        h = self.c.connect()
        self.log(f"connected: {h['broker']} mode={h['mode']} "
                 f"model={h['account_model']}")
        # The RULE is the Connector's: it refuses a mutating call on a broker
        # that is not paper and not armed, so this is a pre-flight that fails
        # in the first second instead of on the first signal, plus the extra
        # condition this surface adds (a verified LSE account). Both read the
        # SDK's own predicate; neither re-implements it.
        # A broker whose terms have each order on a live account follow a
        # person's own action (SPEC 3.3, orders_by_hand_only) is never traded
        # by this runner live: every order it sends comes from a signal, not
        # from the user. Refused before arming, with the broker's own words.
        # On paper the flag does not bind.
        if self.c.terms.get("orders_by_hand_only") and not self.c.is_paper:
            self.log(self.c.by_hand_refusal() + " Run this strategy on the "
                     "broker's demo account, or place the orders yourself.")
            return 2
        # Whatever the terms say, they are shown when a strategy starts on a
        # connection, paper or live, before it is armed (SPEC 3.3).
        if self.c.terms.get("summary"):
            self.log(f"the broker's terms: {self.c.terms['summary']}")
        if not self.c.is_paper:
            if not self.args.arm_live:
                self.log(f"mode={h['mode']} without --arm-live: refusing to trade")
                return 2
            if account is None:
                self.log("no verified account: refusing to trade real money")
                return 2
            self.c.arm(f"--arm-live, LSE key {account.key_id[:8]}")
            self.journal("armed", {"reason": self.c.armed_for})
            self.log(f"ARMED: {self.c.armed_for}")
        if self.symbol not in self.c.catalog:
            self.log(f"{self.symbol} not in catalog")
            return 2
        # A bar strategy needs bars. SPEC 3: what is absent from capabilities
        # is absent from the broker, so ask before requesting it and say so
        # plainly. Requesting it anyway gives an unhandled traceback, which
        # tells an adapter author nothing about what their broker is missing.
        # (A part sent as null, or as anything but an object, is absent too;
        # so are capabilities that are not an object: a list read as one
        # would end the runner with a traceback.)
        caps = h.get("capabilities")
        data = caps.get("data") if isinstance(caps, dict) else None
        data = data if isinstance(data, dict) else {}
        missing = [k for k in ("bars", "history") if not data.get(k)]
        if missing:
            self.log(f"this broker declares no {' and no '.join(missing)}; "
                     f"a bar strategy cannot run against it")
            return 2
        hist = self.c.request("bars.history", {
            "symbol": self.symbol, "timeframe": self.tf,
            "count": self.args.warmup})
        self.window = [BarData(time=b[0], open=b[1], high=b[2], low=b[3],
                               close=b[4], volume=b[5])
                       for b in hist["bars"]]
        self.log(f"warmup: {len(self.window)} bars of history")
        self.c.request("bars.subscribe", {"symbol": self.symbol,
                                          "timeframe": self.tf})
        self.c.on("order.filled", lambda d: self.log(
            f"FILL {d['side']} {d['qty']} @ {d['price']} ({d['client_id']})"))
        self.c.on("order.rejected", lambda d: self.log(
            f"REJECTED {d['client_id']}: {d['code']} {d['message']}"))
        self.c.on("position.closed", lambda d: self.log(
            f"CLOSED {d['position_id']} reason={d['reason']} "
            f"pnl={d['realized_pnl']}"))

        bars_done = 0
        mark = self.c.mark()
        while bars_done < self.args.bars:
            bar = self.c.wait_event(
                "bar", lambda d: d["symbol"] == self.symbol
                and d["timeframe"] == self.tf,
                timeout=self.args.bar_timeout, since=mark)
            mark = self.c.mark()
            b = bar["bar"]
            self.window.append(BarData(time=b[0], open=b[1], high=b[2],
                                       low=b[3], close=b[4], volume=b[5]))
            bars_done += 1
            n_intents = self.on_closed_bar(b)
            # Liveness is unambiguous: EVERY processed bar heartbeats into
            # the journal, so silence longer than a bar interval means dead
            # or feed-stalled, never "quiet strategy". The status file is
            # the same fact for pollers (a UI, a script, `stat`): rewritten
            # atomically each bar; a stale mtime IS the alarm.
            self.journal("bar", {"bar_time": b[0], "intents": n_intents,
                                 "orders_placed": self.placed})
            self.write_status(b[0], bars_done)
        summary = {"bars": bars_done, "orders_placed": self.placed,
                   "intents_skipped": self.skipped,
                   "account": self.c.request("account.get"),
                   "open_positions": len(
                       self.c.request("positions.list")["positions"])}
        if self.args.flatten_on_exit:
            out = self.c.request("flatten_all")
            summary["flattened"] = out["closed"]
        self.log("summary: " + json.dumps(summary))
        self.journal("summary", summary)
        self.c.close()
        return 0

    def write_status(self, bar_time: int, bars_done: int):
        if not self.args.status:
            return
        status = {"pid": os.getpid(), "script": str(self.args.script),
                  "symbol": self.symbol, "timeframe": self.tf,
                  "broker": self.c.handshake.get("broker"),
                  "mode": self.c.handshake.get("mode"),
                  "last_bar_time": bar_time, "bars_done": bars_done,
                  "orders_placed": self.placed,
                  "intents_skipped": self.skipped,
                  "updated_at": int(time.time() * 1000)}
        p = Path(self.args.status)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(status))
        os.replace(tmp, p)

    def on_closed_bar(self, b: list) -> int:
        opts = RunOptions()
        opts.capture_intents = True
        out = run_script_full(self.source, self.window, ExternalData(), {}, opts)
        raw = json.loads(out.to_json())
        if not raw.get("success"):
            self.log(f"script error: {raw.get('errors')}")
            return 0
        # The script's own log() lines are its voice: surface the CURRENT
        # bar's lines live (window re-runs repeat old ones; the bar_time
        # filter keeps exactly the new decisions' diagnostics).
        for lg in raw.get("logs", []):
            if lg.get("time") == b[0]:
                self.log(f"script: {lg.get('message')}")
                self.journal("script_log", {"bar_time": b[0],
                                            "message": lg.get("message")})
        for i, intent in enumerate(out.intents):
            self.execute_intent(intent, b[0], i)
        return len(out.intents)

    def execute_intent(self, intent: dict, bar_time: int, seq: int):
        self.journal("intent", {"bar_time": bar_time, "intent": intent})
        if intent["kind"] == "close":
            cid = self.entry_map.get(intent["entry_id"])
            targets = [p for p in
                       self.c.request("positions.list")["positions"]
                       if p["symbol"] == self.symbol
                       and (cid is None or p.get("client_id") == cid)]
            for p in targets:
                try:
                    self.c.request("position.close",
                                   {"position_id": p["position_id"]})
                except ConnectorError as e:
                    self.log(f"close {p['position_id']} failed: {e.code}")
            return
        # entry intent
        if not self.is_open(bar_time):
            self.skipped += 1
            self.log(f"gated: {self.symbol} session closed at {bar_time}")
            return
        qty = self.round_qty(intent["qty"])
        if qty is None:
            self.skipped += 1
            self.log(f"refused: qty {intent['qty']} below min lot")
            return
        br = intent.get("bracket") or {}
        if br.get("profit_points") or br.get("loss_points"):
            self.skipped += 1
            self.log("refused: points-based exits are not translatable "
                     "pre-fill; use absolute sl=/tp=")
            return
        client_id = f"{self.run_key}-{bar_time}-{intent['id']}-{seq}"
        params = {"client_id": client_id, "symbol": self.symbol,
                  "side": "buy" if intent["direction"] == "long" else "sell",
                  "qty": qty}
        if br.get("stop_price") is not None:
            params["sl"] = round(br["stop_price"], self.spec()["digits"])
        if br.get("limit_price") is not None:
            params["tp"] = round(br["limit_price"], self.spec()["digits"])
        try:
            self.c.request("order.place", params)
            self.entry_map[intent["id"]] = client_id
            self.placed += 1
            self.journal("order.place", params)
        except ConnectorError as e:
            self.log(f"order refused by broker: {e.code} {e}")
            self.journal("order.refused", {"params": params, "code": e.code})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--adapter",
                    help="raw adapter command (quoted); prefer --broker")
    ap.add_argument("--broker",
                    help="a broker registered with `brueconnect add`; "
                         "the switch point: change this name and NOTHING "
                         "else changes")
    ap.add_argument("--warmup", type=int, default=100)
    ap.add_argument("--bars", type=int, default=30,
                    help="process this many closed bars, then stop")
    ap.add_argument("--bar-timeout", type=float, default=120.0)
    ap.add_argument("--flatten-on-exit", action="store_true")
    ap.add_argument("--arm-live", action="store_true",
                    help="required to trade a mode=live adapter; needs a "
                         "verified LSE API key")
    ap.add_argument("--api-key", default="",
                    help="LSE API key for account verification (falls back "
                         "to BRUE_API_KEY / LSE_API_KEY / ~/.config/brue)")
    ap.add_argument("--journal", default="")
    ap.add_argument("--status", default="",
                    help="path to a status json rewritten atomically on "
                         "every processed bar; a stale mtime means the "
                         "strategy is not running")
    args = ap.parse_args()
    if not args.adapter and not args.broker:
        ap.error("one of --broker or --adapter is required")
    sys.exit(LiveDriver(args).run())


if __name__ == "__main__":
    main()
