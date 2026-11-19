"""Alpaca adapter: orders, fills and closes against a scripted broker.

The adapter object is built in-process; its REST client and `emit` are
replaced, so every test states exactly what Alpaca answered and reads
exactly what a platform would be told. The rules under test are the ones
that cost money when broken: an order is never placed twice, a fill is
never reported twice, a refusal is never swallowed. No network.
"""
import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from brueconnect.adapter import AdapterError
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests.test_alpaca_core import ACCOUNT, CALENDAR, TODAY, coin, share

ENV = {"BRUE_CRED_API_KEY": "PKTESTKEY", "BRUE_CRED_API_SECRET": "s3cret",
       "ALPACA_FEED": "iex"}
QUOTE = {"quotes": {"BTC/USD": {"bp": 73990.0, "ap": 74000.0,
                                "t": "2026-11-10T13:00:00.5Z"}}}
SNAP = {"AAPL": {"latestQuote": {"bp": 350.0, "ap": 350.1,
                                 "t": "2026-11-10T15:00:00Z"},
                 "latestTrade": {"p": 350.05}}}
OPEN = {"is_open": True, "next_close": "2099-01-01T16:00:00-05:00",
        "next_open": "2099-01-02T09:30:00-05:00"}
SHUT = {"is_open": False, "next_open": "2099-01-01T09:30:00-05:00",
        "next_close": "2099-01-01T16:00:00-05:00"}


class Broker:
    """Scripted answers by (method, path); every call is recorded."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def on(self, method, path, answer):
        self.routes[(method, path)] = answer

    def call(self, method, host, path, query=None, body=None, **kw):
        self.calls.append((method, path, query, body))
        key = (method, path)
        if key not in self.routes:
            key = next(((m, p) for m, p in self.routes
                        if m == method and p.endswith("*")
                        and path.startswith(p[:-1])), None)
        if key is None:
            raise AssertionError("unexpected broker call %s %s"
                                 % (method, path))
        answer = self.routes[key]
        if isinstance(answer, list) and answer and isinstance(
                answer[0], Step):
            answer = answer.pop(0).value if len(answer) > 1 \
                else answer[0].value
        if callable(answer):
            answer = answer(query, body, path)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sent(self, method, path=None):
        return [c for c in self.calls if c[0] == method
                and (path is None or c[1] == path)]


class Step:
    """One answer of a sequence; the last one repeats."""

    def __init__(self, value):
        self.value = value


def make(state_dir=None, environ=None):
    env = dict(ENV, **(environ or {}))
    if state_dir:
        env["BRUE_BROKER_STATE"] = state_dir
    a = mod.AlpacaAdapter(argv=["--paper"], environ=env, out=io.StringIO())
    a.events = []
    a.emit = lambda name, data: a.events.append((name, data))
    b = Broker()
    a.client.call = b.call
    rows, ids = mod.build_catalog(
        [share("AAPL"), share("YI", shortable=False, easy_to_borrow=False,
                              borrow_status="hard_to_borrow")],
        [coin("BTC/USD"), coin("ETH/USD")], CALENDAR, ACCOUNT, TODAY)
    a._install_catalog(rows, ids, time.time())
    a._account, a._account_t = dict(ACCOUNT), time.monotonic()
    a._started = a._baselined = a._opened = True
    a._stream_up.set()
    a._session = ("connected", "Alpaca paper account PA1")
    a._components["session"] = a._session
    b.on("GET", "/v2/account", dict(ACCOUNT))
    b.on("GET", "/v1beta3/crypto/us/latest/quotes", QUOTE)
    b.on("GET", "/v2/stocks/snapshots", SNAP)
    b.on("GET", "/v2/clock", OPEN)
    # What the adapter reads on its own account before it acts: the open
    # orders of a symbol, the positions, the ledger, and whether a client
    # id is already an order at Alpaca. Empty unless a test says otherwise.
    b.on("GET", "/v2/orders", [])
    b.on("GET", "/v2/positions", [])
    b.on("GET", "/v2/account/activities/FILL", [])
    b.on("GET", "/v2/orders:by_client_order_id", mod.AlpacaError(
        404, "order not found for it", 40410000))
    a._last_full = time.monotonic()
    return a, b


def answer(a, rid, method, params, timeout=8.0):
    """Send one request through the adapter's dispatch and wait for its
    answer, as a platform does: the read thread only hands a request on,
    and the answer is written when it is ready."""
    a._dispatch(rid, method, params)
    end = time.monotonic() + timeout
    while True:
        for line in a._out.getvalue().splitlines():
            msg = json.loads(line)
            if msg.get("type") == "res" and msg.get("id") == rid:
                return msg
        if time.monotonic() > end:
            raise AssertionError("no answer to %s %s" % (rid, method))
        time.sleep(0.01)


def order(oid, coid, sym="BTC/USD", side="buy", typ="market", qty="0.0002",
          status="new", filled="0", **kw):
    o = {"id": oid, "client_order_id": coid, "symbol": sym,
         "asset_id": "id-" + sym, "side": side, "type": typ, "qty": qty,
         "filled_qty": filled, "status": status, "time_in_force": "gtc",
         "limit_price": None, "stop_price": None,
         "updated_at": "2026-11-10T13:00:00Z",
         "expires_at": "2027-02-08T21:00:00Z"}
    o.update(kw)
    return o


def fill(o, xid, price, qty, position_qty, event="fill", cum=None):
    done = dict(o, status="filled" if event == "fill"
                else "partially_filled", filled_qty=cum or qty)
    return {"event": event, "execution_id": xid, "price": price, "qty": qty,
            "position_qty": position_qty,
            "timestamp": "2026-11-10T13:02:31.655376506Z", "order": done}


def activity(xid, oid, side, qty, price, cum=None, leaves="0",
             sym="BTC/USD"):
    return {"id": "20261110080231655::" + xid, "activity_type": "FILL",
            "transaction_time": "2026-11-10T13:02:31.655Z", "type": "fill",
            "price": price, "qty": qty, "side": side, "symbol": sym,
            "leaves_qty": leaves, "order_id": oid, "cum_qty": cum or qty}


def ours(a, coid, kind="user"):
    a._intents[coid] = {"kind": kind,
                        "cid": coid if kind == "user" else None}


def names(a):
    return [n for n, _ in a.events]


def only(a, name):
    got = [d for n, d in a.events if n == name]
    assert len(got) == 1, "%d %s events: %s" % (len(got), name, a.events)
    return got[0]


def position(sym="BTCUSD", qty="0.0001995", avail=None, side="long", **kw):
    return dict({"symbol": sym, "asset_id": "coin-" + sym, "qty": qty,
                 "qty_available": qty if avail is None else avail,
                 "side": side, "avg_entry_price": "74217.02",
                 "unrealized_pl": "-0.01"}, **kw)


class Fills(unittest.TestCase):
    def test_crypto_buy_is_reported_net_of_the_coin_fee(self):
        a, _ = make()
        ours(a, "cid-1")
        o = order("o1", "cid-1")
        a._on_trade_update({"event": "new", "order": o})
        a._on_trade_update(fill(o, "x1", "74217.02", "0.0002", "0.0001995"))
        f = only(a, "order.filled")
        self.assertEqual(f["qty"], 0.0001995)          # what was gained
        self.assertEqual(f["price"], 74217.02)         # as executed
        self.assertAlmostEqual(f["fees"], -0.0000005 * 74217.02, places=6)
        self.assertLess(f["fees"], 0)
        self.assertEqual((f["fill_id"], f["client_id"], f["order_id"],
                          f["position_id"], f["symbol"], f["side"]),
                         ("x1", "cid-1", "o1", "BTC/USD", "BTC/USD", "buy"))
        self.assertEqual(f["time"], 1794315751655)
        self.assertEqual(names(a), ["order.accepted", "order.filled"])
        json.dumps(a.events, allow_nan=False)

    def test_a_sell_reports_no_fee_and_the_close_after_the_fill(self):
        a, _ = make()
        ours(a, "cid-1")
        ours(a, "cid-2")
        buy = order("o1", "cid-1")
        a._on_trade_update(fill(buy, "x1", "74217.02", "0.0002",
                                "0.0001995"))
        sell = order("o2", "cid-2", side="sell", qty="0.0001995")
        a._on_trade_update(fill(sell, "x2", "74300", "0.0001995", "0"))
        self.assertEqual(names(a), ["order.accepted", "order.filled",
                                    "order.accepted", "order.filled",
                                    "position.closed"])
        f = a.events[3][1]
        self.assertEqual((f["qty"], f["fees"], f["client_id"]),
                         (0.0001995, 0.0, "cid-2"))
        c = only(a, "position.closed")
        self.assertEqual((c["close_id"], c["position_id"], c["qty"],
                          c["reason"], c["client_id"], c["swap"]),
                         ("c-x2", "BTC/USD", 0.0001995, "manual", "cid-1",
                          0.0))
        self.assertAlmostEqual(c["realized_pnl"],
                               (74300 - 74217.02) * 0.0001995, places=6)
        # the buy's fee is the only one Alpaca let us see
        self.assertAlmostEqual(c["commission"], -0.037109, places=6)
        self.assertNotIn("BTC/USD", a._book)

    def test_the_same_fill_is_never_emitted_twice(self):
        a, _ = make()
        ours(a, "cid-1")
        o = order("o1", "cid-1")
        msg = fill(o, "x1", "74217.02", "0.0002", "0.0001995")
        a._on_trade_update(msg)
        a._on_trade_update(json.loads(json.dumps(msg)))        # replayed
        with a._state_lock:                                    # the ledger
            a._apply_missed([activity("x1", "o1", "buy", "0.0002",
                                      "74217.02")], [position()])
        self.assertEqual(names(a).count("order.filled"), 1)
        self.assertEqual(len(a._fills), 1)

    def test_ledger_first_then_stream_is_still_one_fill(self):
        a, _ = make()
        ours(a, "cid-1")
        o = order("o1", "cid-1", status="filled", filled="0.0002")
        with a._state_lock:
            a._track(o)
            a._apply_missed([activity("x1", "o1", "buy", "0.0002",
                                      "74217.02")], [position()])
        a._on_trade_update(fill(o, "x1", "74217.02", "0.0002", "0.0001995"))
        f = only(a, "order.filled")
        # No position_qty in the ledger: the fee is what the position did
        # not gain.
        self.assertEqual(f["qty"], 0.0001995)
        self.assertAlmostEqual(f["fees"], -0.037109, places=6)
        self.assertEqual(f["fill_id"], "x1")

    def test_a_fill_from_before_this_session_is_not_announced(self):
        a, _ = make()
        a._session_start_ms = mod.parse_ts("2026-11-10T14:00:00Z")
        with a._state_lock:
            a._track(order("o1", "someone-else"))
            a._apply_missed([activity("x1", "o1", "buy", "0.0002",
                                      "74217.02")], [position()])
        self.assertEqual(a.events, [])

    def test_partial_then_final_ends_in_exactly_one_filled(self):
        a, _ = make()
        ours(a, "cid-1")
        o = order("o1", "cid-1", sym="AAPL", qty="10")
        a._on_trade_update(fill(o, "x1", "350.10", "4", "4",
                                event="partial_fill"))
        a._on_trade_update(fill(o, "x2", "350.12", "6", "10", cum="10"))
        self.assertEqual(names(a), ["order.accepted", "order.partial",
                                    "order.filled"])
        part, last = a.events[1][1], a.events[2][1]
        self.assertEqual((part["qty"], part["remaining"], part["fill_id"]),
                         (4.0, 6.0, "x1"))
        self.assertEqual((last["qty"], last["fill_id"], last["fees"]),
                         (6.0, "x2", 0.0))
        self.assertNotIn("remaining", last)
        self.assertEqual(a._book["AAPL"]["qty"], 10)

    def test_the_adapters_own_close_is_a_close_not_a_fill(self):
        for kind, reason in (("close", "manual"), ("flatten", "flatten"),
                             ("sl", "sl"), ("tp", "tp")):
            a, _ = make()
            ours(a, "cid-1")
            a._on_trade_update(fill(order("o1", "cid-1", sym="AAPL",
                                          qty="5"), "x1", "350", "5", "5"))
            ours(a, "internal", kind)
            a._on_trade_update(fill(order(
                "o2", "internal", sym="AAPL", side="sell", qty="5"),
                "x2", "351", "5", "0"))
            self.assertEqual(names(a), ["order.accepted", "order.filled",
                                        "position.closed"], kind)
            c = only(a, "position.closed")
            self.assertEqual((c["reason"], c["qty"], c["realized_pnl"],
                              c["commission"]), (reason, 5.0, 5.0, 0.0))

    def test_prefix_tells_an_earlier_sessions_orders_apart(self):
        a, _ = make()
        self.assertEqual(a._kind(mod.P_CLOSE + "abc"), ("close", None))
        self.assertEqual(a._kind(mod.P_FLAT + "abc"), ("flatten", None))
        self.assertEqual(a._kind(mod.P_SL + "abc"), ("sl", None))
        self.assertEqual(a._kind(mod.P_TP + "abc"), ("tp", None))
        self.assertEqual(a._kind("made-in-the-dashboard"), ("foreign", None))

    def test_a_strangers_order_has_a_null_client_id_and_unknown_reason(self):
        a, _ = make()
        a._book["AAPL"] = {"qty": mod.Decimal(5), "avg": mod.Decimal(350),
                           "fees": 0.0, "cid": None, "opened": None}
        o = order("o9", "dashboard-uuid", sym="AAPL", side="sell", qty="5",
                  typ="stop")
        a._on_trade_update(fill(o, "x9", "340", "5", "0"))
        f = only(a, "order.filled")
        self.assertIn("client_id", f)
        self.assertIsNone(f["client_id"])
        c = only(a, "position.closed")
        self.assertEqual(c["reason"], "unknown")
        self.assertIn("outside this session", c["reason_detail"])
        self.assertNotIn("order.accepted", names(a))

    def test_a_positions_read_that_got_there_first(self):
        # The close fills; positions.list is read and already shows the
        # account flat; THEN the stream delivers the fill.
        a, b = make()
        ours(a, "cid-1")
        a._on_trade_update(fill(order("o1", "cid-1"), "x1", "74217.02",
                                "0.0002", "0.0001995"))
        b.on("GET", "/v2/positions", [])
        self.assertEqual(a.handle_positions_list({}), {"positions": []})
        ours(a, "closer", "close")
        a._on_trade_update(fill(order("o2", "closer", side="sell",
                                      qty="0.0001995"), "x2", "74300",
                                "0.0001995", "0"))
        c = only(a, "position.closed")
        self.assertEqual((c["qty"], c["client_id"]), (0.0001995, "cid-1"))
        self.assertAlmostEqual(c["realized_pnl"],
                               (74300 - 74217.02) * 0.0001995, places=6)
        self.assertNotIn("BTC/USD", a._book)

    def test_a_stale_view_is_not_turned_into_a_fee(self):
        a, _ = make()
        ours(a, "cid-1")
        # The position moved by far more than was bought: our view of it
        # was old. Report the execution; invent nothing.
        a._on_trade_update(fill(order("o1", "cid-1"), "x1", "74217.02",
                                "0.0002", "0.0105"))
        f = only(a, "order.filled")
        self.assertEqual((f["qty"], f["fees"]), (0.0002, 0.0))

    def test_positions_list_uses_catalog_spelling(self):
        a, b = make()
        b.on("GET", "/v2/positions", [position(), position(
            "AAPL", "3", side="short", asset_id="id-AAPL",
            avg_entry_price="350")])
        got = a.handle_positions_list({})["positions"]
        self.assertEqual([(p["position_id"], p["symbol"], p["side"],
                           p["qty"]) for p in got],
                         [("BTC/USD", "BTC/USD", "buy", 0.0001995),
                          ("AAPL", "AAPL", "sell", 3.0)])
        for p in got:
            self.assertEqual((p["swap"], p["trail_points"]), (0.0, 0))


class OrderEvents(unittest.TestCase):
    def test_a_resting_order_is_placed_once(self):
        a, _ = make()
        ours(a, "cid-1")
        o = order("o1", "cid-1", typ="limit", limit_price="50000")
        for status in ("pending_new", "new", "new"):
            a._on_trade_update({"event": status,
                                "order": dict(o, status=status)})
        self.assertEqual(names(a), ["order.accepted", "order.placed"])
        self.assertEqual(only(a, "order.placed"), {
            "client_id": "cid-1", "order_id": "o1", "type": "limit",
            "price": 50000.0, "expires_at": 1802120400000})

    def test_an_order_first_seen_already_ended_is_history(self):
        # orders.list reads last week's orders: none of them is in flight,
        # and none is announced again.
        a, b = make()
        a._ours["old-1"] = "user"
        b.on("GET", "/v2/orders", [
            order("h1", "old-1", status="filled", filled="0.0002"),
            order("h2", "old-2", typ="limit", status="canceled")])
        got = a.handle_orders_list({})["orders"]
        self.assertEqual([(o["order_id"], o["state"], o["client_id"])
                          for o in got], [("h1", "filled", "old-1"),
                                          ("h2", "cancelled", None)])
        self.assertTrue(all(r["done"] for r in a._orders.values()))
        self.assertEqual(a.events, [])
        b.on("GET", "/v2/account", dict(ACCOUNT))
        a._poll_once()
        self.assertEqual(a.events, [])

    def test_a_market_order_is_never_placed(self):
        a, _ = make()
        ours(a, "cid-1")
        a._on_trade_update({"event": "new", "order": order("o1", "cid-1")})
        self.assertEqual(names(a), [])

    def test_cancel_says_who_cancelled(self):
        a, _ = make()
        ours(a, "cid-1")
        ours(a, "cid-2")
        asked = order("o1", "cid-1", typ="limit", limit_price="5")
        a._on_trade_update({"event": "new", "order": asked})
        a._orders["o1"]["cancel_req"] = True
        a._on_trade_update({"event": "canceled",
                            "order": dict(asked, status="canceled")})
        a._on_trade_update({"event": "canceled",
                            "order": dict(asked, status="canceled")})
        other = order("o2", "cid-2", typ="limit", limit_price="5")
        a._on_trade_update({"event": "new", "order": other})
        a._on_trade_update({"event": "canceled",
                            "order": dict(other, status="canceled")})
        got = [d for n, d in a.events if n == "order.cancelled"]
        self.assertEqual(got[0], {"order_id": "o1", "client_id": "cid-1"})
        self.assertEqual((got[1]["order_id"], got[1]["code"]),
                         ("o2", "x_alpaca_canceled"))
        self.assertEqual(len(got), 2)

    def test_expired_and_rejected(self):
        a, _ = make()
        ours(a, "cid-1")
        ours(a, "cid-2")
        day = order("o1", "cid-1", sym="AAPL", typ="limit",
                    time_in_force="day", limit_price="300")
        a._on_trade_update({"event": "new", "order": day})
        a._on_trade_update({"event": "expired",
                            "order": dict(day, status="expired")})
        mkt = order("o2", "cid-2", status="pending_new")
        a._on_trade_update({"event": "pending_new", "order": mkt})
        a._on_trade_update({"event": "rejected",
                            "order": dict(mkt, status="rejected")})
        self.assertEqual(only(a, "order.expired"), {
            "order_id": "o1", "client_id": "cid-1", "expiry": "day"})
        r = only(a, "order.rejected")
        self.assertEqual((r["client_id"], r["code"]),
                         ("cid-2", "x_alpaca_rejected"))

    def test_amend_keeps_the_platforms_order_id(self):
        a, b = make()
        ours(a, "cid-1")
        old = order("o1", "cid-1", typ="limit", limit_price="50000")
        a._on_trade_update({"event": "new", "order": old})
        b.on("GET", "/v2/orders/*", old)
        b.on("PATCH", "/v2/orders/o1", lambda q, body, path: order(
            "o2", body["client_order_id"], typ="limit", status="accepted",
            limit_price=body["limit_price"], replaces="o1"))
        self.assertEqual(a.handle_order_amend({"order_id": "o1",
                                               "price": 50100.5}), {})
        patch_body = b.sent("PATCH")[0][3]
        self.assertEqual(patch_body["limit_price"], "50100.5")
        coid = patch_body["client_order_id"]
        self.assertTrue(coid.startswith("brue-r:o1:"))
        self.assertNotIn("order.amended", names(a))   # not confirmed yet
        new = order("o2", coid, typ="limit", limit_price="50100.5",
                    replaces="o1")
        a._on_trade_update({"event": "pending_replace", "order": dict(
            old, status="pending_replace", replaced_by="o2")})
        a._on_trade_update({"event": "accepted",
                            "order": dict(new, status="accepted")})
        a._on_trade_update({"event": "replaced", "order": dict(
            old, status="replaced", replaced_by="o2")})
        self.assertEqual(only(a, "order.amended"), {
            "order_id": "o1", "price": 50100.5, "qty": 0.0002,
            "expires_at": 1802120400000})
        self.assertEqual(names(a).count("order.placed"), 1)
        self.assertEqual(a._current["o1"], "o2")
        # The replacement fills: reported under the id the platform holds.
        a._on_trade_update(fill(new, "x1", "50100", "0.0002", "0.0001995"))
        f = only(a, "order.filled")
        self.assertEqual((f["order_id"], f["client_id"]), ("o1", "cid-1"))
        # and a later cancel of "o1" would be sent to o2
        self.assertEqual(a._current.get("o1"), "o2")

    def test_cancel_reads_the_state_first(self):
        a, b = make()
        ours(a, "cid-1")
        live = order("o1", "cid-1", typ="limit", limit_price="5")
        b.on("GET", "/v2/orders/*", live)
        b.on("DELETE", "/v2/orders/o1", None)
        self.assertEqual(a.handle_order_cancel({"order_id": "o1"}), {})
        self.assertTrue(a._orders["o1"]["cancel_req"])
        b.on("GET", "/v2/orders/*", dict(live, status="filled"))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_cancel({"order_id": "o1"})
        self.assertEqual(ctx.exception.code, "not_resting")
        self.assertEqual(len(b.sent("DELETE")), 1)
        b.on("GET", "/v2/orders/*", mod.AlpacaError(
            404, "order not found for nope", 40410000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_cancel({"order_id": "nope"})
        self.assertEqual(ctx.exception.code, "not_found")


class Placing(unittest.TestCase):
    def refuse(self, a, params):
        base = {"client_id": "c-" + str(len(a.events)), "symbol": "BTC/USD",
                "side": "buy", "qty": 0.0002}
        before = len(a.events)
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place(dict(base, **params))
        rejected = a.events[before:]
        self.assertEqual([n for n, _ in rejected], ["order.rejected"])
        self.assertEqual(rejected[0][1]["client_id"], base["client_id"])
        return ctx.exception

    def test_refusals_are_decided_here_and_never_sent(self):
        a, b = make()
        cases = [
            ({"symbol": "NO_SUCH_SYMBOL_XYZ", "qty": 1}, "unknown_symbol"),
            ({"qty": 0.000013362 + 1e-9 / 3.0}, "invalid_qty"),
            ({"qty": 0.00000001}, "invalid_qty"),          # under min_qty
            ({"qty": 0.000013362}, "invalid_qty"),         # under 10 USD
            ({"qty": 2e9}, "invalid_qty"),                 # over max_qty
            ({"qty": 2.0}, "insufficient_margin"),         # 148,000 USD
            ({"symbol": "AAPL", "qty": 1000}, "insufficient_margin"),
            ({"symbol": "AAPL", "qty": 1.5}, "invalid_qty"),
            ({"sl": 70000.0}, "trade_restricted"),
            ({"tp": 80000.0}, "trade_restricted"),
            ({"type": "stop", "price": 70000.0}, "unsupported"),
            ({"expiry": "day", "type": "limit", "price": 70000.0},
             "invalid_expiry"),
            ({"expiry": "gtd", "expiry_at": 1, "type": "limit",
              "price": 70000.0}, "invalid_expiry"),
            ({"trail_points": 50}, "unsupported"),
            ({"type": "limit"}, "x_alpaca_invalid_price"),
            ({"side": "sell"}, "trade_restricted"),        # crypto short
            ({"symbol": "YI", "side": "sell", "qty": 1}, "trade_restricted"),
        ]
        for params, want in cases:
            self.assertEqual(self.refuse(a, params).code, want, params)
        self.assertEqual(b.sent("POST"), [])

    def test_a_market_order_for_a_shut_share_is_refused_not_queued(self):
        a, b = make()
        b.on("GET", "/v2/clock", SHUT)
        e = self.refuse(a, {"symbol": "AAPL", "qty": 1})
        self.assertEqual(e.code, "market_closed")
        self.assertEqual(b.sent("POST"), [])
        # a resting order may wait for the open: that is what it is for
        b.on("POST", "/v2/orders", lambda q, body, path: order(
            "o1", body["client_order_id"], sym="AAPL", typ="limit",
            qty="1", status="accepted", limit_price="300"))
        r = a.handle_order_place({"client_id": "c-lim", "symbol": "AAPL",
                                  "side": "buy", "qty": 1, "type": "limit",
                                  "price": 300.0})
        self.assertEqual(r, {"order_id": "o1", "status": "resting"})

    def test_an_unknown_clock_is_not_guessed(self):
        a, b = make()
        b.on("GET", "/v2/clock", mod.AlpacaError(0, "timed out"))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place({"client_id": "c-clock", "symbol": "AAPL",
                                  "side": "buy", "qty": 1})
        e = ctx.exception
        self.assertEqual((e.code, e.like, e.retryable),
                         ("x_alpaca_clock_unavailable", "market_closed",
                          True))
        # "ask again" is not the end of an order: nothing is announced, so
        # the retry under the same client_id cannot follow a "rejected"
        self.assertEqual(a.events, [])
        self.assertEqual(b.sent("POST"), [])

    def test_a_refusal_that_depends_on_the_account_asks_alpaca_first(self):
        # The account may look short of margin BECAUSE this very order
        # went through earlier and this profile has no memory of it.
        a, b = make()
        b.on("GET", "/v2/orders:by_client_order_id", order(
            "o9", "c-big", qty="27", status="filled", filled="27"))
        r = a.handle_order_place({"client_id": "c-big", "symbol": "BTC/USD",
                                  "side": "buy", "qty": 27.0})
        self.assertEqual(r, {"order_id": "o9", "status": "accepted"})
        self.assertEqual((b.sent("POST"), names(a)), ([], []))
        # a number that is simply wrong needs no question to Alpaca
        asked = len(b.sent("GET", "/v2/orders:by_client_order_id"))
        self.refuse(a, {"qty": 0.00000001})
        self.assertEqual(len(b.sent("GET", "/v2/orders:by_client_order_id")),
                         asked)

    def test_what_is_sent(self):
        a, b = make()
        b.on("POST", "/v2/orders", lambda q, body, path: order(
            "o1", body["client_order_id"], sym=body["symbol"],
            qty=body["qty"], status="pending_new"))
        r = a.handle_order_place({"client_id": "cid-1", "symbol": "BTC/USD",
                                  "side": "buy",
                                  "qty": 0.00020000000000000001})
        self.assertEqual(r, {"order_id": "o1", "status": "accepted"})
        self.assertEqual(b.sent("POST")[0][3], {
            "symbol": "BTC/USD", "side": "buy", "type": "market",
            "qty": "0.0002", "time_in_force": "gtc",
            "client_order_id": "cid-1"})
        self.assertEqual(a.events, [("order.accepted", {
            "client_id": "cid-1", "order_id": "o1"})])
        a.handle_order_place({"client_id": "cid-2", "symbol": "AAPL",
                              "side": "buy", "qty": 3})
        share_body = b.sent("POST")[1][3]
        self.assertEqual((share_body["qty"], share_body["time_in_force"]),
                         ("3", "day"))
        a.handle_order_place({
            "client_id": "cid-3", "symbol": "AAPL", "side": "buy", "qty": 2,
            "type": "stop_limit", "stop_price": 360.0, "price": 361.25,
            "expiry": "day"})
        sl = b.sent("POST")[2][3]
        self.assertEqual((sl["type"], sl["stop_price"], sl["limit_price"],
                          sl["time_in_force"]),
                         ("stop_limit", "360", "361.25", "day"))

    def test_a_fill_that_beats_the_response_is_still_ours(self):
        a, b = make()

        def post(q, body, path):
            o = order("o1", body["client_order_id"], qty=body["qty"])
            a._on_trade_update(fill(o, "x1", "74000", "0.0002",
                                    "0.0001995"))
            return dict(o, status="pending_new")
        b.on("POST", "/v2/orders", post)
        a.handle_order_place({"client_id": "cid-1", "symbol": "BTC/USD",
                              "side": "buy", "qty": 0.0002})
        self.assertEqual(names(a), ["order.accepted", "order.filled"])
        self.assertEqual(only(a, "order.filled")["client_id"], "cid-1")

    def test_a_replayed_client_id_returns_the_original_not_a_new_order(self):
        a, b = make()
        b.on("POST", "/v2/orders", mod.AlpacaError(
            422, "client_order_id must be unique", 40010001))
        b.on("GET", "/v2/orders:by_client_order_id", order(
            "orig", "cid-1", status="filled", filled="0.0002"))
        p = {"client_id": "cid-1", "symbol": "BTC/USD", "side": "buy",
             "qty": 0.0002}
        self.assertEqual(a.handle_order_place(p),
                         {"order_id": "orig", "status": "accepted"})
        self.assertEqual(len(b.sent("POST")), 1)
        self.assertEqual(b.sent("GET", "/v2/orders:by_client_order_id")[0][2],
                         {"client_order_id": "cid-1"})
        self.assertEqual(a.events, [])     # nothing new happened
        # the same id on a DIFFERENT order is the SPEC's duplicate
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place(dict(p, qty=0.0003))
        self.assertEqual(ctx.exception.code, "duplicate_client_id")

    def test_a_lost_answer_is_looked_up_never_resent(self):
        a, b = make()
        b.on("POST", "/v2/orders", mod.AlpacaError(0, "timed out"))
        b.on("GET", "/v2/orders:by_client_order_id", order(
            "o1", "cid-1", status="new"))
        p = {"client_id": "cid-1", "symbol": "BTC/USD", "side": "buy",
             "qty": 0.0002}
        self.assertEqual(a.handle_order_place(p)["order_id"], "o1")
        self.assertEqual(len(b.sent("POST")), 1)
        # it never arrived: say so, retryable; the retry is safe
        a, b = make()
        b.on("POST", "/v2/orders", mod.AlpacaError(0, "timed out"))
        b.on("GET", "/v2/orders:by_client_order_id", mod.AlpacaError(
            404, "order not found for cid-1", 40410000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place(p)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable),
                         ("internal", True))
        self.assertEqual(len(b.sent("POST")), 1)
        # nobody answers at all: unknown, and said so
        a, b = make()
        b.on("POST", "/v2/orders", mod.AlpacaError(0, "timed out"))
        b.on("GET", "/v2/orders:by_client_order_id",
             mod.AlpacaError(0, "timed out"))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place(p)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("same client_id", str(ctx.exception))
        self.assertEqual(len(b.sent("POST")), 1)

    def test_alpacas_refusal_is_typed_and_also_an_event(self):
        a, b = make()
        b.on("POST", "/v2/orders", mod.AlpacaError(
            403, "potential wash trade detected. use complex orders",
            40310000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place({"client_id": "cid-1", "symbol": "BTC/USD",
                                  "side": "buy", "qty": 0.0002})
        self.assertEqual((ctx.exception.code, ctx.exception.like),
                         ("x_alpaca_wash_trade", "trade_restricted"))
        r = only(a, "order.rejected")
        self.assertEqual((r["client_id"], r["code"]),
                         ("cid-1", "x_alpaca_wash_trade"))
        self.assertIn("wash trade", r["message"])
        self.assertNotIn("s3cret", json.dumps(a.events))


class Closing(unittest.TestCase):
    def setUp(self):
        p = patch.object(mod.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_close_is_sized_from_what_alpaca_says_is_free(self):
        a, b = make()
        b.on("GET", "/v2/positions/BTCUSD", position())
        b.on("POST", "/v2/orders", lambda q, body, path: order(
            "c1", body["client_order_id"], side="sell", qty=body["qty"]))
        self.assertEqual(a.handle_position_close({
            "position_id": "BTC/USD", "client_id": "close-1"}), {})
        # (a closing order says that it is one, so that Alpaca itself
        # refuses it when there is nothing to close)
        self.assertEqual(b.sent("POST")[0][3], {
            "symbol": "BTC/USD", "side": "sell", "type": "market",
            "qty": "0.0001995", "time_in_force": "gtc",
            "client_order_id": "close-1",
            "position_intent": "sell_to_close"})
        self.assertEqual(a._kind("close-1")[0], "close")
        # the retried close: Alpaca has seen the id, nothing is sold twice
        b.on("POST", "/v2/orders", mod.AlpacaError(
            422, "client_order_id must be unique", 40010001))
        b.on("GET", "/v2/orders:by_client_order_id", order(
            "c1", "close-1", side="sell", qty="0.0001995", status="filled"))
        self.assertEqual(a.handle_position_close({
            "position_id": "BTC/USD", "client_id": "close-1"}), {})

    def test_partial_close_and_its_limits(self):
        a, b = make()
        b.on("GET", "/v2/positions/AAPL", position(
            "AAPL", "10", avail="4", asset_id="id-AAPL"))
        b.on("POST", "/v2/orders", lambda q, body, path: order(
            "c1", body["client_order_id"], sym="AAPL", side="sell",
            qty=body["qty"]))
        a.handle_position_close({"position_id": "AAPL", "qty": 3})
        # (that close has filled by the time the next request comes)
        b.on("GET", "/v2/orders/c1", lambda q, body, path: order(
            "c1", b.sent("POST")[0][3]["client_order_id"], sym="AAPL",
            side="sell", qty="3", status="filled", filled="3"))
        body = b.sent("POST")[0][3]
        self.assertEqual((body["qty"], body["side"], body["time_in_force"]),
                         ("3", "sell", "day"))
        self.assertTrue(body["client_order_id"].startswith(mod.P_CLOSE))
        for qty, code in ((11, "invalid_qty"), (2.5, "invalid_qty"),
                          (6, "x_alpaca_qty_held")):
            with self.assertRaises(AdapterError) as ctx:
                a.handle_position_close({"position_id": "AAPL", "qty": qty})
            self.assertEqual(ctx.exception.code, code, qty)
        self.assertEqual(len(b.sent("POST")), 1)
        b.on("GET", "/v2/positions/MSFT", mod.AlpacaError(
            404, "position does not exist", 40410000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_position_close({"position_id": "MSFT"})
        self.assertEqual(ctx.exception.code, "not_found")

    def test_flatten_returns_only_when_flat_and_sells_once(self):
        a, b = make()
        b.on("DELETE", "/v2/orders", [])
        held = position()
        b.on("GET", "/v2/positions", [
            Step([held]),                            # what there is
            Step([held]),                            # free: one sell goes
            Step([dict(held, qty_available="0")]),   # our sell is working
            Step([])])
        b.on("POST", "/v2/orders", lambda q, body, path: order(
            "f1", body["client_order_id"], side="sell", qty=body["qty"]))
        self.assertEqual(a.handle_flatten_all({}), {"closed": 1})
        posts = b.sent("POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0][3]["qty"], posts[0][3]["side"]),
                         ("0.0001995", "sell"))
        self.assertTrue(posts[0][3]["client_order_id"].startswith(mod.P_FLAT))
        self.assertEqual(len(b.sent("DELETE", "/v2/orders")), 1)
        self.assertEqual(len(b.sent("GET", "/v2/positions")), 4)
        # nothing to do: zero, and no order
        b.on("GET", "/v2/positions", [])
        self.assertEqual(a.handle_flatten_all({}), {"closed": 0})
        self.assertEqual(len(b.sent("POST")), 1)

    def test_flatten_does_not_count_what_it_could_not_close(self):
        a, b = make()
        b.on("DELETE", "/v2/orders", [])
        b.on("GET", "/v2/clock", SHUT)
        b.on("GET", "/v2/positions", [
            Step([position(), position("AAPL", "3", asset_id="id-AAPL")]),
            Step([position(), position("AAPL", "3", asset_id="id-AAPL")]),
            Step([position("AAPL", "3", asset_id="id-AAPL")])])
        b.on("POST", "/v2/orders", lambda q, body, path: order(
            "f1", body["client_order_id"], side="sell", qty=body["qty"]))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_flatten_all({})
        e = ctx.exception
        self.assertEqual(e.code, "market_closed")
        self.assertIn("closed 1 of 2", str(e))
        self.assertIn("AAPL", str(e))
        self.assertEqual([c[3]["symbol"] for c in b.sent("POST")],
                         ["BTC/USD"])

    def test_flatten_reports_a_close_alpaca_refused(self):
        a, b = make()
        b.on("DELETE", "/v2/orders", [])
        b.on("GET", "/v2/positions", [position()])
        b.on("POST", "/v2/orders", mod.AlpacaError(
            403, "account is restricted to liquidation only", 40310000))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_flatten_all({})
        self.assertEqual(ctx.exception.code, "internal")
        self.assertIn("liquidation only", str(ctx.exception))
        # Asked once right behind the cancelling of the orders (a refusal
        # THEN may be an order still on its way out), once more; final.
        self.assertEqual(len(b.sent("POST")), 2)


class Persistence(unittest.TestCase):
    def test_a_replayed_flatten_after_a_restart_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = make(d)
            a._bind_account("PA1")
            b.on("DELETE", "/v2/orders", [])
            b.on("GET", "/v2/positions", [])
            first = answer(a, "r1", "flatten_all", {"client_id": "flat-1"})
            self.assertEqual(first["result"], {"closed": 0})
            # a new process, the same profile directory
            a2, b2 = make(d)
            again = answer(a2, "r2", "flatten_all", {"client_id": "flat-1"})
            self.assertEqual(again["result"], {"closed": 0})
            self.assertEqual(b2.calls, [])          # the broker was not asked
            clash = answer(a2, "r3", "flatten_all", {"client_id": "flat-1",
                                                     "extra": 1})
            self.assertEqual(clash["error"]["code"], "duplicate_client_id")

    def test_fills_and_ownership_survive_and_follow_the_account(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = make(d)
            a._bind_account("PA1")
            ours(a, "cid-1")
            a._ours["cid-1"] = "user"
            a._on_trade_update(fill(order("o1", "cid-1"), "x1", "74217.02",
                                    "0.0002", "0.0001995"))
            a._save_state()
            a2, _ = make(d)
            self.assertEqual([f["fill_id"] for f in a2._fills], ["x1"])
            self.assertEqual(a2._fills[0]["qty"], 0.0001995)
            self.assertEqual(a2._kind("cid-1"), ("user", "cid-1"))
            # the key now opens another account: none of it applies
            a2._bind_account("PA2")
            self.assertEqual((a2._fills, a2._ours, a2._idempotent),
                             ([], {}, {}))
            a3, _ = make(d)
            self.assertEqual(a3._fills, [])

    def test_state_is_never_written_as_account_json(self):
        with tempfile.TemporaryDirectory() as d:
            a, _ = make(d)
            a._bind_account("PA1")
            a._save_state()
            self.assertEqual(os.listdir(d), ["alpaca_state.json"])
            with open(os.path.join(d, "alpaca_state.json")) as fh:
                self.assertNotIn("s3cret", fh.read())


class Quotes(unittest.TestCase):
    def test_a_one_sided_quote_is_not_a_price(self):
        a, b = make()
        b.on("GET", "/v2/stocks/quotes/latest", {"quotes": {
            "AAPL": {"bp": 334.59, "ap": 0, "t": "2026-11-09T21:00:04Z"},
            "YI": {"bp": 0, "ap": 0, "t": "2026-11-09T21:00:04Z"}}})
        a.handle_quotes_subscribe({"symbols": ["BTC/USD", "AAPL", "YI"]})
        a._quote_step()
        self.assertEqual(a.events, [("quote", {
            "symbol": "BTC/USD", "bid": 73990.0, "ask": 74000.0,
            "time": 1794315600500})])        # the quote's own time
        self.assertEqual(b.sent("GET", "/v2/stocks/quotes/latest")[0][2],
                         {"feed": "iex", "symbols": "AAPL,YI"})

    def test_subscribing_again_sends_the_latest_at_once(self):
        a, b = make()
        a.handle_quotes_subscribe({"symbols": ["BTC/USD"]})
        self.assertEqual(a.events, [])           # no request on this thread
        a._quote_step()
        a._quote_next = 0.0
        a._quote_step()                          # unchanged: not re-sent
        self.assertEqual(names(a), ["quote"])
        calls = len(b.calls)
        a.handle_quotes_subscribe({"symbols": ["BTC/USD"]})
        self.assertEqual(names(a), ["quote", "quote"])
        self.assertEqual(len(b.calls), calls)    # from the cache
        a._quotes["BTC/USD"] = a._quotes["BTC/USD"][:3] + (
            time.monotonic() - 60.0,)
        a.handle_quotes_subscribe({"symbols": ["BTC/USD"]})
        self.assertEqual(len(a.events), 2)       # stale: fetched instead
        a._quote_step()
        self.assertEqual(len(a.events), 3)

    def test_unknown_symbol_and_unsubscribe(self):
        a, b = make()
        with self.assertRaises(AdapterError) as ctx:
            a.handle_quotes_subscribe({"symbols": ["BTC/USD", "NOPE"]})
        self.assertEqual(ctx.exception.code, "unknown_symbol")
        self.assertEqual(a._quote_subs, set())
        a.handle_quotes_subscribe({"symbols": ["BTC/USD"]})
        a.handle_quotes_unsubscribe({"symbols": ["BTC/USD"]})
        self.assertEqual(a._quote_step(), 60.0)
        self.assertEqual(b.calls, [])

    def test_over_the_cap_is_said_out_loud(self):
        a, _ = make()
        with patch.object(mod, "MAX_QUOTE_SYMBOLS", 2):
            a.handle_quotes_subscribe({"symbols": ["BTC/USD", "ETH/USD",
                                                   "AAPL"]})
            self.assertEqual(a._quote_subs, {"BTC/USD", "ETH/USD"})
            state = only(a, "session.state")
            self.assertEqual(state["state"], "degraded")
            self.assertIn("AAPL", state["detail"])
            a.handle_quotes_unsubscribe({"symbols": ["AAPL"]})
        self.assertEqual(a.events[-1][1]["state"], "connected")

    def test_a_failed_poll_degrades_then_recovers(self):
        a, b = make()
        a.handle_quotes_subscribe({"symbols": ["BTC/USD"]})
        b.on("GET", "/v1beta3/crypto/us/latest/quotes",
             mod.AlpacaError(429, "rate limit exceeded", retry_after=7.0))
        self.assertEqual(a._quote_step(), 7.0)
        self.assertEqual(a.events[-1][1]["state"], "degraded")
        b.on("GET", "/v1beta3/crypto/us/latest/quotes", QUOTE)
        a._quote_next = 0.0
        a._quote_step()
        self.assertEqual([n for n, _ in a.events[-2:]],
                         ["session.state", "quote"])
        self.assertEqual(a.events[-2][1]["state"], "connected")


class Bars(unittest.TestCase):
    def bars(self, *times):
        return {"bars": {"BTC/USD": [
            {"t": mod.iso_ms(t), "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3}
            for t in times]}, "next_page_token": None}

    def test_history_is_closed_bars_oldest_first(self):
        a, b = make()
        now = 1794315600000 + 30_000              # half way into a minute
        a.now_ms = lambda: now
        m = 60_000
        base = now // m * m
        b.on("GET", "/v1beta3/crypto/us/bars", self.bars(
            base, base - m, base - 2 * m, base - 3 * m))
        got = a.handle_bars_history({"symbol": "BTC/USD", "timeframe": "1m",
                                     "count": 2})["bars"]
        self.assertEqual([r[0] for r in got], [base - 2 * m, base - m])
        self.assertEqual(got[0][1:], [1.0, 2.0, 0.5, 1.5, 3.0])
        q = b.sent("GET", "/v1beta3/crypto/us/bars")[0][2]
        self.assertEqual((q["timeframe"], q["sort"], q["symbols"]),
                         ("1Min", "desc", "BTC/USD"))
        for bad in ({"timeframe": "2m", "count": 1},
                    {"timeframe": "1m", "count": 0},
                    {"timeframe": "1m", "count": 10 ** 9}):
            with self.assertRaises(AdapterError) as ctx:
                a.handle_bars_history(dict({"symbol": "BTC/USD"}, **bad))
            self.assertEqual(ctx.exception.code, "unsupported")

    def test_subscription_sends_each_closed_bar_once_and_no_history(self):
        a, b = make()
        m = 60_000
        clock = [1794315600000 + 30_000]
        a.now_ms = lambda: clock[0]
        base = clock[0] // m * m
        a.handle_bars_subscribe({"symbol": "BTC/USD", "timeframe": "1m"})
        b.on("GET", "/v1beta3/crypto/us/bars", self.bars(base - m, base))
        a._poll_bars()
        self.assertEqual(b.calls, [])            # not due before the edge
        clock[0] = base + m + 4000
        a._poll_bars()                           # `base` has closed
        clock[0] += 6000
        a._poll_bars()
        got = [d for n, d in a.events if n == "bar"]
        self.assertEqual(got, [{"symbol": "BTC/USD", "timeframe": "1m",
                                "bar": [base, 1.0, 2.0, 0.5, 1.5, 3.0]}])
        a.handle_bars_unsubscribe({"symbol": "BTC/USD", "timeframe": "1m"})
        self.assertEqual(a._bar_subs, {})


class History(unittest.TestCase):
    def test_ledger_and_own_fills_are_one_list(self):
        a, b = make()
        ours(a, "cid-1")
        a._on_trade_update(fill(order("o1", "cid-1"), "x1", "74217.02",
                                "0.0002", "0.0001995"))
        b.on("GET", "/v2/account/activities/FILL", [
            activity("x1", "o1", "buy", "0.0002", "74217.02"),
            dict(activity("x0", "old", "sell", "0.5", "2000", sym="ETH/USD"),
                 transaction_time="2026-11-10T12:00:00Z")])
        now = mod.parse_ts("2026-11-10T13:30:00Z")
        a.now_ms = lambda: now
        fills = a.handle_history_fills({})["fills"]
        self.assertEqual([f["fill_id"] for f in fills], ["x0", "x1"])
        self.assertEqual((fills[1]["qty"], fills[1]["client_id"]),
                         (0.0001995, "cid-1"))       # as it was reported
        self.assertEqual((fills[0]["symbol"], fills[0]["side"],
                          fills[0]["qty"], fills[0]["fees"],
                          fills[0]["client_id"]),
                         ("ETH/USD", "sell", 0.5, 0.0, None))
        for f in fills:
            self.assertTrue({"price", "time", "fill_id"} <= set(f))
        q = b.sent("GET", "/v2/account/activities/FILL")[0][2]
        self.assertEqual(q["after"], mod.iso_ms(now - 7 * 86_400_000 - 1))


class Budget(unittest.TestCase):
    def test_reads_stop_before_writes_do(self):
        bud = mod._Budget(per_minute=10, reserve=4, background=3,
                          per_second=1000)
        for _ in range(3):
            bud.take("trading", "background", 0)
        with self.assertRaises(mod.AlpacaError) as ctx:
            bud.take("trading", "background", 0)
        self.assertEqual(ctx.exception.status, 429)
        for _ in range(3):
            bud.take("trading", "read", 0)
        with self.assertRaises(mod.AlpacaError):
            bud.take("trading", "read", 0)
        for _ in range(4):
            bud.take("trading", "write", 0)      # the reserve
        with self.assertRaises(mod.AlpacaError):
            bud.take("trading", "write", 0)
        bud.take("data", "read", 0)              # the other host's minute

    def test_a_429_blocks_the_host_and_handlers_are_not_slept(self):
        bud = mod._Budget(per_second=1000)
        bud.block("data", 30.0)
        t0 = time.monotonic()
        with self.assertRaises(mod.AlpacaError) as ctx:
            bud.take("data", "read", 1.5)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertGreater(ctx.exception.retry_after, 25.0)
        bud.take("trading", "read", 0)

    def test_pace_per_second(self):
        """Per host, as Alpaca counts. An order's requests (rank 2: the
        lane that answers orders) may follow each other at once, up to
        the pace; what one host was sent takes nothing from the other."""
        bud = mod._Budget(per_second=2)
        bud.take("trading", "write", 0, rank=2)
        bud.take("trading", "write", 0, rank=2)
        with self.assertRaises(mod.AlpacaError) as ctx:
            bud.take("trading", "write", 0, rank=2)
        self.assertIn("2 requests a second", ctx.exception.message)
        bud.take("data", "write", 0, rank=2)
        bud.take("data", "write", 0, rank=2)
        with self.assertRaises(mod.AlpacaError):
            bud.take("data", "write", 0, rank=2)

    def test_below_an_order_requests_go_out_evenly_not_in_a_burst(self):
        """Two a second is one every half second for everything below an
        order: a burst the instant a second has room would leave whoever
        comes next (an order too) waiting for the next burst."""
        bud = mod._Budget(per_second=2)
        bud.take("trading", "read", 0)
        with self.assertRaises(mod.AlpacaError):
            bud.take("trading", "read", 0.2)
        t0 = time.monotonic()
        bud.take("trading", "read", 1.0)
        self.assertGreater(time.monotonic() - t0, 0.2)
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_the_adapters_own_threads_leave_a_handler_room(self):
        bud = mod._Budget(per_second=3)
        bud.take("trading", "background", 0)
        bud.take("trading", "background", 0.5)
        with self.assertRaises(mod.AlpacaError):
            bud.take("trading", "background", 0.5)  # two a second, not three
        bud.take("trading", "write", 0, rank=2)    # the third: an order, now

    def test_a_spent_minute_is_said_as_a_spent_minute(self):
        bud = mod._Budget(per_minute=10, reserve=4, per_second=1000)
        for _ in range(6):
            bud.take("trading", "read", 0)
        with self.assertRaises(mod.AlpacaError) as ctx:
            bud.take("trading", "read", 0)
        self.assertIn("this minute's requests", ctx.exception.message)
        self.assertGreater(ctx.exception.retry_after, 50)

    def test_margin_is_judged_on_held_figures_when_alpaca_is_silent(self):
        a, b = make()
        b.on("GET", "/v2/account", mod.AlpacaError(429, "rate limit"))
        with self.assertRaises(AdapterError) as ctx:
            a.handle_order_place({"client_id": "c1", "symbol": "BTC/USD",
                                  "side": "buy", "qty": 27.0})
        self.assertEqual(ctx.exception.code, "insufficient_margin")
        self.assertEqual(b.sent("POST"), [])

    def test_retry_after_forms(self):
        ra = mod.AlpacaClient._retry_after
        self.assertEqual(ra({"Retry-After": "7"}), 7.0)
        self.assertAlmostEqual(ra({"X-Ratelimit-Reset":
                                   str(time.time() + 12)}), 12.0, delta=1.0)
        self.assertLessEqual(ra({}), 61.0)


if __name__ == "__main__":
    unittest.main()
