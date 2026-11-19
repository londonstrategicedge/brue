#!/usr/bin/env python3
"""NovaFX: a MADE-UP broker that proves the onboarding path end to end.

This is what an external broker's integration looks like from the
platform's side: they take the brue-connect SPEC, implement the methods
(here by reusing the bundled paper engine with their own catalog), pass the
conformance suite, and their terms flow into the platform exactly as
declared in catalog.list. Nothing in the platform knows the name "NovaFX";
it is just another adapter command.

Deliberately DIFFERENT from LSE terms everywhere it can be, so any place
the platform accidentally hardcodes LSE conventions shows up loudly:
slashless symbol names, 500x FX leverage, nonzero swaps, per-lot
commissions, a stops distance, and its own price seeds.

Usage: python3 adapters/novafx/novafx_adapter.py [same flags as paper]
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

try:
    from brueconnect.adapters.paper.paper_adapter import PaperAdapter
except ImportError:  # file-run from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from brueconnect.adapters.paper.paper_adapter import PaperAdapter  # noqa: E402

NOVA_INSTRUMENTS = [
    {"symbol": "EURUSD", "name": "Euro vs US Dollar", "asset_class": "fx",
     "base_ccy": "EUR", "quote_ccy": "USD", "digits": 5, "tick_size": 0.00001,
     "tick_value": 1.0, "lot_step": 0.01, "min_qty": 0.01, "max_qty": 200.0,
     "min_notional": 0, "contract_size": 100000,
     "margin_rate": 0.002, "margin_maintenance_rate": 0.001,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "00:00", "close": "24:00",
                   "tz": "UTC"}],
     "holidays": ["2026-12-25"], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 20,
     "fees": {"commission_per_lot": 3.5, "mode": "per_lot", "rate": 3.5,
              "min": 0.0},
     "swap_long": -7.1, "swap_short": 2.3, "swap_mode": "points",
     "swap_3day_dow": 3,
     "_price0": 1.08512, "_spread": 0.00007, "_sigma": 0.00019,
     "_partial_above": 20.0},
    {"symbol": "GBPJPY", "name": "British Pound vs Japanese Yen",
     "asset_class": "fx", "base_ccy": "GBP", "quote_ccy": "JPY", "digits": 3,
     "tick_size": 0.001, "tick_value": 100.0, "lot_step": 0.01,
     "min_qty": 0.01, "max_qty": 100.0, "min_notional": 0,
     "contract_size": 100000,
     "margin_rate": 0.005, "margin_maintenance_rate": 0.0025,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "00:00", "close": "24:00",
                   "tz": "UTC"}],
     "holidays": ["2026-12-25"], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 30,
     "fees": {"commission_per_lot": 3.5, "mode": "per_lot", "rate": 3.5,
              "min": 0.0},
     "swap_long": -18.4, "swap_short": 6.0, "swap_mode": "points",
     "swap_3day_dow": 3,
     "_price0": 191.842, "_spread": 0.018, "_sigma": 0.00021,
     "_partial_above": 15.0},
    {"symbol": "XAUUSD", "name": "Gold vs US Dollar", "asset_class": "metal",
     "base_ccy": "XAU", "quote_ccy": "USD", "digits": 2, "tick_size": 0.01,
     "tick_value": 1.0, "lot_step": 0.01, "min_qty": 0.01, "max_qty": 50.0,
     "min_notional": 0, "contract_size": 100,
     "margin_rate": 0.01, "margin_maintenance_rate": 0.005,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "01:00", "close": "23:00",
                   "tz": "UTC"}],
     "holidays": ["2026-12-25"], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 50,
     "fees": {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
              "min": 0.0},
     "swap_long": -32.0, "swap_short": 14.5, "swap_mode": "points",
     "swap_3day_dow": 3,
     "_price0": 2412.35, "_spread": 0.28, "_sigma": 0.00033,
     "_partial_above": 10.0},
    {"symbol": "US500", "name": "US 500 Index", "asset_class": "index",
     "base_ccy": "US500", "quote_ccy": "USD", "digits": 1, "tick_size": 0.1,
     "tick_value": 1.0, "lot_step": 0.1, "min_qty": 0.1, "max_qty": 100.0,
     "min_notional": 0, "contract_size": 10,
     "margin_rate": 0.01, "margin_maintenance_rate": 0.005,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5], "open": "00:00", "close": "24:00",
                   "tz": "UTC"}],
     "holidays": ["2026-12-25", "2026-01-01"], "tradable": True,
     "shortable": True, "trade_mode": "full", "stops_level_points": 10,
     "fees": {"commission_per_lot": 0.0, "mode": "none", "rate": 0.0,
              "min": 0.0},
     "swap_long": -2.1, "swap_short": -1.4, "swap_mode": "points",
     "swap_3day_dow": 5,
     "_price0": 5501.3, "_spread": 0.5, "_sigma": 0.00024,
     "_partial_above": 40.0},
    {"symbol": "BTCUSD", "name": "Bitcoin vs US Dollar",
     "asset_class": "crypto", "base_ccy": "BTC", "quote_ccy": "USD",
     "digits": 1, "tick_size": 0.1, "tick_value": 0.1, "lot_step": 0.001,
     "min_qty": 0.001, "max_qty": 20.0, "min_notional": 10,
     "contract_size": 1,
     "margin_rate": 0.1, "margin_maintenance_rate": 0.05,
     "margin_tiers": None,
     "sessions": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                   "close": "24:00", "tz": "UTC"}],
     "holidays": [], "tradable": True, "shortable": True,
     "trade_mode": "full", "stops_level_points": 0,
     "fees": {"commission_per_lot": 0.0, "mode": "pct_notional",
              "rate": 0.0005, "min": 0.1},
     "swap_long": -25.0, "swap_short": -25.0, "swap_mode": "currency_per_lot",
     "_price0": 67348.0, "_spread": 9.0, "_sigma": 0.0012,
     "_partial_above": 8.0},
]


class NovaFXAdapter(PaperAdapter):
    name = "novafx"
    version = "0.1"
    broker = "NovaFX (fictional demo broker)"
    mode = "paper"

    def __init__(self, args):
        super().__init__(args, instruments=NOVA_INSTRUMENTS)

    def broker_info(self) -> dict:
        # The fixture fills this in completely, because it is the only place
        # the platform's picker gets exercised end to end before a real
        # broker onboards. Everything here is invented; NovaFX does not exist.
        return {"display_name": "NovaFX",
                "tagline": "Fictional demo venue: FX, metals and crypto CFDs",
                "mark": "NX",
                "website": "https://novafx.example",
                "support": "mailto:desk@novafx.example",
                "country": "Nowhere",
                "regulated_by": ["Not regulated (fixture)"],
                "asset_classes": ["FX", "Metals", "Crypto"]}


def main():
    ap = argparse.ArgumentParser()
    # Same env fallback as the paper adapter: the platform hands the private
    # state dir as BRUE_BROKER_STATE (SPEC 8.5), never as a flag, and an
    # adapter reading only the flag loses its whole book on restart.
    ap.add_argument("--state-dir",
                    default=os.environ.get("BRUE_BROKER_STATE", ""))
    ap.add_argument("--model", choices=["netting", "hedging"], default="netting")
    ap.add_argument("--tick-ms", type=int, default=25)
    ap.add_argument("--sim-step-ms", type=int, default=2000)
    # different seed than paper on purpose: two brokers must never show the
    # same tape, or a cross-wired quote path would be invisible
    ap.add_argument("--seed", type=int, default=41)
    ap.add_argument("--balance", type=float, default=25000.0)
    ap.add_argument("--warmup-bars", type=int, default=0,
                    help="start with N complete 1m bars of history")
    args = ap.parse_args()
    a = NovaFXAdapter(args)
    threading.Thread(target=a.clock_loop, daemon=True).start()
    a.emit("session.state", {"state": "connected", "detail": "novafx ready"})
    a.run()


if __name__ == "__main__":
    main()
