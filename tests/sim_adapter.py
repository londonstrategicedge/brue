"""A configurable adapter that simulates broker behaviours the reference
adapter cannot show: a venue that is entirely shut, a broker that cannot
enumerate its orders, one that does not know why a position closed, and one
whose idempotency is weaker than the protocol's.

Driven by env vars so one file covers every case:
  SIM_ALL_CLOSED=1     every instrument refuses with market_closed
  SIM_CAN_LIST=0       declare orders.can_list false
  SIM_REASON=unknown   what position.closed reports as its reason
  SIM_WEAK_IDEM=1      accept a conflicting client_id replay (as some venues do)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brueconnect.adapter import Adapter, AdapterError  # noqa: E402

ALL_CLOSED = os.environ.get("SIM_ALL_CLOSED") == "1"
CAN_LIST = os.environ.get("SIM_CAN_LIST", "1") == "1"
REASON = os.environ.get("SIM_REASON", "manual")
WEAK_IDEM = os.environ.get("SIM_WEAK_IDEM") == "1"

SPEC = {"symbol": "SIMUSD", "name": "Sim vs Dollar", "asset_class": "fx",
        "base_ccy": "SIM", "quote_ccy": "USD", "digits": 5,
        "tick_size": 0.00001, "tick_value": 1.0, "lot_step": 0.01,
        "min_qty": 0.01, "max_qty": 100, "contract_size": 100000,
        "margin_rate": 0.03, "sessions": [], "holidays": [],
        "tradable": True, "trade_mode": "full"}


class Sim(Adapter):
    def __init__(self):
        super().__init__()
        self.seq_pos = 0
        self.positions = {}
        self.orders = {}
        self.fills = []
        self.seen = {}

    name = "sim"
    version = "1"
    broker = "simulated venue"
    mode = "paper"
    account_currency = "USD"
    account_model = "netting"

    def capabilities(self):
        return {
            "orders": {"market": True, "brackets": False, "modify": False,
                       "pending": True, "trailing": False,
                       "partial_close": False,
                       "pending_types": ["limit"], "expiry": ["gtc"],
                       "reserves_margin": False, "can_list": CAN_LIST},
            "data": {"quotes": True, "bars": False, "history": True},
            "flatten_all": True,
        }

    def handle_catalog_list(self, params):
        return {"instruments": [SPEC]}

    def handle_account_get(self, params):
        return {"balance": 10000.0, "equity": 10000.0, "margin_used": 0.0,
                "margin_free": 10000.0, "currency": "USD"}

    def handle_positions_list(self, params):
        return {"positions": list(self.positions.values())}

    def handle_orders_list(self, params):
        if not CAN_LIST:
            raise AdapterError("unsupported", "this venue cannot list orders")
        return {"orders": list(self.orders.values())}

    def handle_history_fills(self, params):
        return {"fills": self.fills}

    def handle_order_place(self, params):
        cid = params.get("client_id")
        if ALL_CLOSED:
            raise AdapterError("market_closed", "venue is shut", retryable=True)
        if cid in self.seen and not WEAK_IDEM:
            raise AdapterError("duplicate_client_id", "reused id")
        # A venue with weak idempotency returns the ORIGINAL order and
        # calls it success, which is what four of the simulated venues do.
        if cid in self.seen:
            return self.seen[cid]
        self.seq_pos += 1
        pid = "p%d" % self.seq_pos
        oid = "o%d" % self.seq_pos
        qty = float(params.get("qty") or 0.01)
        px = 1.10000
        self.positions[pid] = {"position_id": pid, "client_id": cid,
                               "symbol": "SIMUSD",
                               "side": params.get("side"), "qty": qty,
                               "avg_price": px, "sl": None, "tp": None,
                               "unrealized_pnl": 0.0, "swap": 0.0,
                               "commission": 0.0, "opened_at": self.now_ms()}
        res = {"order_id": oid, "status": "accepted"}
        self.seen[cid] = res
        self.emit("order.filled", {"client_id": cid, "order_id": oid,
                                   "position_id": pid, "symbol": "SIMUSD",
                                   "side": params.get("side"), "qty": qty,
                                   "price": px, "fees": 0.0,
                                   "time": self.now_ms()})
        self.fills.append({"fill_id": oid, "order_id": oid, "symbol": "SIMUSD",
                           "side": params.get("side"), "qty": qty,
                           "price": px, "fees": 0.0, "time": self.now_ms()})
        return res

    def _close(self, pid, reason):
        p = self.positions.pop(pid, None)
        if p is None:
            raise AdapterError("not_found", "no such position")
        self.emit("position.closed",
                  {"position_id": pid, "client_id": p.get("client_id"),
                   "qty": p["qty"], "price": 1.10010, "realized_pnl": 1.0,
                   "swap": 0.0, "commission": 0.0, "reason": reason,
                   "reason_detail": "simulated close",
                   "time": self.now_ms()})

    def handle_position_close(self, params):
        self._close(params.get("position_id"), REASON)
        return {}

    def handle_flatten_all(self, params):
        n = len(self.positions)
        for pid in list(self.positions):
            self._close(pid, "flatten")
        return {"closed": n}


if __name__ == "__main__":
    Sim().run()
