"""Smallest possible conformant-ish adapter, used by the SDK unit tests.
One instrument, instant fills, idempotent orders. Not the reference paper
adapter (that lives in adapters/paper); this exists so SDK tests do not
depend on the richer simulator."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brueconnect import Adapter, AdapterError


class MiniAdapter(Adapter):
    name = "mini"
    version = "0.1"
    broker = "mini test broker"
    mode = "paper"

    def __init__(self):
        super().__init__()
        self._n = 0
        self.positions = {}

    def capabilities(self):
        caps = super().capabilities()
        caps["data"] = {"quotes": False, "bars": False, "history": False}
        return caps

    def handle_catalog_list(self, params):
        return {"instruments": [{
            "symbol": "TEST", "name": "Test Instrument", "asset_class": "fx",
            "base_ccy": "TST", "quote_ccy": "USD", "digits": 5,
            "tick_size": 0.00001, "lot_step": 0.01, "min_qty": 0.01,
            "max_qty": 100, "contract_size": 100000, "margin_rate": 0.02,
            "sessions": [{"days": [1, 2, 3, 4, 5], "open": "00:00",
                          "close": "24:00", "tz": "UTC"}],
            "holidays": [], "tradable": True, "shortable": True,
            "fees": {"commission_per_lot": 0}, "swap_long": 0, "swap_short": 0}]}

    def handle_account_get(self, params):
        return {"balance": 10000.0, "equity": 10000.0, "margin_used": 0.0,
                "margin_free": 10000.0, "currency": "USD"}

    def handle_positions_list(self, params):
        return {"positions": list(self.positions.values())}

    def handle_order_place(self, params):
        cid = params["client_id"]
        prior = self.idempotent_replay(cid)
        if prior is not None:
            return prior
        if params.get("symbol") != "TEST":
            raise AdapterError("unknown_symbol", "only TEST exists here")
        qty = float(params.get("qty") or 0)
        if qty <= 0:
            raise AdapterError("invalid_qty", "qty must be positive")
        self._n += 1
        oid, pid = f"o{self._n}", f"p{self._n}"
        result = self.idempotent_store(cid, {"order_id": oid, "status": "accepted"})
        self.emit("order.accepted", {"client_id": cid, "order_id": oid})
        self.positions[pid] = {"position_id": pid, "client_id": cid,
                               "symbol": "TEST", "side": params["side"],
                               "qty": qty, "avg_price": 1.0, "sl": None,
                               "tp": None, "unrealized_pnl": 0.0,
                               "opened_at": 0}
        self.emit("order.filled", {"client_id": cid, "order_id": oid,
                                   "position_id": pid, "symbol": "TEST",
                                   "side": params["side"], "qty": qty,
                                   "price": 1.0, "fees": 0.0, "time": 0})
        return result

    def handle_flatten_all(self, params):
        n = len(self.positions)
        for pid in list(self.positions):
            self.positions.pop(pid)
            self.emit("position.closed", {"position_id": pid, "client_id": "",
                                          "qty": 0, "price": 1.0,
                                          "realized_pnl": 0.0,
                                          "reason": "flatten", "time": 0})
        return {"closed": n}


if __name__ == "__main__":
    MiniAdapter().run()
