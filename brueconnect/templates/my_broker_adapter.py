"""A starter adapter: your own broker in the LSE Terminal.

An adapter is the small program the LSE Terminal runs to trade through one
broker. The Terminal sends it requests (prices, bars, orders) and it answers.
The Brue Connect SDK it is built on does the talking, so this file only says
what your broker does.

TRY IT AS IT IS. As delivered it trades a practice market made inside this
file: three made-up instruments, prices worked out from the clock, and a
practice account of 10,000 USD. It sends nothing over the network. In the LSE
Terminal open Connections, press "+ Add your own broker", give it a name and
the full path of this file, and press "Test and add". Type anything in its two
key boxes, press Connect, then Use.

MAKE IT YOURS. The file has three parts.
  1. Your broker: the functions named broker_... Each holds a line starting
     "# YOUR BROKER:" where your broker's own call belongs. Replace the
     practice body under it with that call and return the same shape.
  2. The protocol: turns those answers into what the Terminal reads. You can
     normally leave it alone.
  3. The practice market. Delete it once nothing in part 1 uses it.
Change name, broker and mode in the class too. mode must be "live" if the
account trades real money.

CHECK IT before you trade with it. In the LSE Terminal, the "+ Add your own
broker" card shows the command that tests this file on your computer, under
"For developers: test your adapter on this computer", and says how to run it;
the walkthrough's step
"Test it" has the rest. It places small orders, so use the practice market
or a demo account. Nothing may FAIL; a SKIP is a test that does not apply.
The command hands the file its keys as BRUE_CRED_ and each key box's key in
capitals: BRUE_CRED_API_KEY and BRUE_CRED_API_SECRET as delivered. If you
change the boxes, change those names to match (a "username" box is
BRUE_CRED_USERNAME). Without the Terminal, in this file's folder, with
brueconnect importable and those variables set (any text for the practice
market), it is
    python3 -m brueconnect.conformance.run --adapter "python3 my_broker_adapter.py"
SPEC.md, in the Brue repository
(https://github.com/londonstrategicedge/brue/blob/main/SPEC.md), is the full contract.
"""
import json
import math
import os
import random
import sys
import threading
import time
from functools import lru_cache

# Use the SDK of the program that started this file. The LSE Terminal names
# the folder that holds its own copy in BRUE_CONNECT_SDK; putting that folder
# first means no other copy this Python can find is used instead.
if os.environ.get("BRUE_CONNECT_SDK"):
    sys.path.insert(0, os.environ["BRUE_CONNECT_SDK"])

from brueconnect import TIMEFRAMES_MS, Adapter, AdapterError  # noqa: E402

PUMP_EVERY_S = 1.0  # how often prices and closed positions are asked for;
                    # raise it if your broker limits how often you may ask
MAX_BARS = 5000     # the most bars one answer carries


class MyBroker(Adapter):
    # Who this is. Change them for your broker. mode must be "live" when the
    # account trades real money; the Terminal then asks you before it lets
    # anything trade.
    name = "my-broker"
    broker = "My Broker"
    version = "0.1"
    mode = "paper"
    account_currency = "USD"   # the currency the account is kept in
    account_model = "hedging"  # "netting" if your broker keeps one position per symbol

    # The key boxes the Terminal shows for this broker. What is typed in a box
    # reaches this program as BRUE_CRED_ and the box's key in capitals
    # (BRUE_CRED_API_KEY and BRUE_CRED_API_SECRET here; a box with the key
    # "username" arrives as BRUE_CRED_USERNAME), and broker_login gets each by
    # its key. For other boxes (a username and a password, say) change this
    # list and broker_login's names to match; the Terminal shows the new boxes
    # after the next Connect, and the test command's BRUE_CRED_ names change
    # with them.
    credential_form = [
        {"key": "api_key", "label": "API key", "type": "text"},
        {"key": "api_secret", "label": "API secret", "type": "password"},
    ]

    def __init__(self):
        super().__init__()
        # Nothing here may log in or ask your broker anything: the Terminal
        # starts this program just to learn its name. That waits for
        # on_first_call, below.
        self._lock = threading.Lock()
        self._specs = {}         # symbol -> instrument
        self._quoted = set()     # symbols whose prices are wanted
        self._bar_subs = {}      # (symbol, timeframe) -> start of the last bar sent
        self._last_quote = {}    # symbol -> (bid, ask, time) last sent
        self._closing = set()    # positions this program is closing right now
        self._closes_seen = {}   # close_id -> time, so no close is told twice
        self._closes_from = 0
        self._state = "connected"
        self._wake = threading.Event()

    # =====================================================================
    # Part 1: your broker. Each function is one question to your broker.
    # Money is in the account's currency, times are milliseconds since
    # 1970 UTC.
    # =====================================================================

    def broker_login(self, api_key, api_secret):
        """Log in, or check the key. Raise AdapterError("unauthorized",
        "a sentence saying why") when your broker refuses the key."""
        # YOUR BROKER: log in or check the key here. The practice market takes any key.
        self.market = PracticeMarket(os.environ.get("BRUE_BROKER_STATE"))

    def broker_instruments(self):
        """Every instrument you can trade, as a list of dicts shaped like
        PRACTICE_INSTRUMENTS in part 3. SPEC.md section 4 explains each
        field."""
        # YOUR BROKER: ask for the instruments and their terms.
        return self.market.instruments()

    def broker_price(self, symbol):
        """The latest price: {"bid", "ask", "time"}, time being your
        broker's own time for that price."""
        # YOUR BROKER: ask for the latest bid and ask of one symbol.
        return self.market.price(symbol)

    def broker_bars(self, symbol, timeframe, count):
        """The newest `count` bars that have closed, oldest first, each
        [start time, open, high, low, close, volume]. timeframe is one of
        1m 5m 15m 30m 1h 4h 1d. The Terminal charts the bid, so ask for
        bid prices if your broker offers a choice."""
        # YOUR BROKER: ask for the bars.
        return self.market.bars(symbol, timeframe, count)

    def broker_account(self):
        """{"balance", "equity", "margin_used", "margin_free", "currency"}."""
        # YOUR BROKER: ask for the account's balance and margin.
        return self.market.account()

    def broker_place_order(self, symbol, side, qty, sl, tp, client_id):
        """A market order, side "buy" or "sell". sl is the stop and tp the
        target, None when not wanted; your broker keeps them. Return the
        fill: {"fill_id", "order_id", "position_id", "qty", "price", "fees",
        "time"}. When your broker refuses the order, raise AdapterError with
        the code that says why, such as "insufficient_margin",
        "market_closed", "invalid_stops" or "invalid_qty" (SPEC.md
        section 6)."""
        # YOUR BROKER: place the order. If your broker takes an order
        # reference of yours, send client_id as it, so a repeated request
        # can never place a second order.
        return self.market.place(symbol, side, qty, sl, tp, client_id)

    def broker_positions(self):
        """Every open position: {"position_id", "symbol", "side", "qty",
        "avg_price", "sl", "tp", "unrealized_pnl", "opened_at"}."""
        # YOUR BROKER: ask for the open positions.
        return self.market.positions()

    def broker_set_stops(self, position_id, sl, tp):
        """Set a position's stop and target. None removes one."""
        # YOUR BROKER: change the position's stop and target.
        self.market.set_stops(position_id, sl, tp)

    def broker_close_position(self, position_id, qty):
        """Close a position at the market, or only qty of it when qty is not
        None. Return the close: {"close_id", "position_id", "qty", "price",
        "realized_pnl", "time"}."""
        # YOUR BROKER: close the position.
        return self.market.close(position_id, qty, "manual")

    def broker_closes_since(self, since_ms):
        """Every close since since_ms, whoever made it: this program, your
        broker filling a stop or a target, or you on your broker's website.
        Each like broker_close_position's, plus "reason": "sl", "tp" or
        "manual", or "unknown" when your broker does not say."""
        # YOUR BROKER: ask for the positions closed since then.
        return self.market.closes_since(since_ms)

    def broker_fills(self, start_ms, end_ms):
        """Every trade between two times (None means no limit), oldest
        first: {"fill_id", "order_id", "position_id", "symbol", "side",
        "qty", "price", "fees", "time"}."""
        # YOUR BROKER: ask for the trade history.
        return self.market.fills(start_ms, end_ms)

    # =====================================================================
    # Part 2: the protocol (SPEC.md). You can normally leave it alone.
    # =====================================================================

    def capabilities(self):
        # Set to False anything your broker cannot do. Without bars and
        # history the Terminal does not chart from this broker; without
        # modify it does not let you drag a stop or a target.
        return {"orders": {"market": True, "brackets": True, "modify": True,
                           "pending": False, "trailing": False,
                           "partial_close": True},
                "data": {"quotes": True, "bars": True, "history": True},
                "flatten_all": True}

    def on_first_call(self):
        """Runs before the first request that needs your broker."""
        typed = {f["key"]: os.environ.get("BRUE_CRED_" + f["key"].upper(), "").strip()
                 for f in self.credential_form}
        empty = [f["label"] for f in self.credential_form if not typed[f["key"]]]
        if empty and os.environ.get("BRUE_CONNECT_SDK"):
            # Started by the LSE Terminal, which always names its SDK folder.
            raise AdapterError("unauthorized", (
                f"The {empty[0]} box is empty: type your {empty[0]} in this "
                "broker's row and press Connect." if len(empty) == 1 else
                f"The {' and '.join(empty)} boxes are empty: type them in "
                "this broker's row and press Connect."))
        if empty:
            # Run by hand or by its test: say which variables are missing.
            names = [("BRUE_CRED_" + f["key"].upper()) for f in self.credential_form
                     if not typed[f["key"]]]
            raise AdapterError("unauthorized", (
                f"{' and '.join(names)} {'is' if len(names) == 1 else 'are'} not "
                "set. Outside the LSE Terminal each key box comes from BRUE_CRED_ "
                "and the box's key in capitals: set "
                + ("it" if len(names) == 1 else "them")
                + " where you run this file (any text while it trades the "
                "practice market)."))
        self.broker_login(**typed)  # each box by its key: api_key, api_secret
        self._closes_from = self.now_ms()
        threading.Thread(target=self._pump, daemon=True).start()

    def _instrument(self, symbol):
        if not self._specs:
            self._specs = {i["symbol"]: i for i in self.broker_instruments()}
        if symbol not in self._specs:
            raise AdapterError("unknown_symbol", f"{symbol!r} is not traded here")
        return self._specs[symbol]

    @staticmethod
    def _timeframe(timeframe):
        if timeframe not in TIMEFRAMES_MS:
            raise AdapterError("unsupported", f"no {timeframe!r} bars; the "
                               f"timeframes are {' '.join(TIMEFRAMES_MS)}")
        return TIMEFRAMES_MS[timeframe]

    def handle_catalog_list(self, params):
        instruments = self.broker_instruments()
        self._specs = {i["symbol"]: i for i in instruments}
        return {"instruments": instruments}

    def handle_account_get(self, params):
        return self.broker_account()

    def handle_positions_list(self, params):
        blank = {"client_id": None, "trail_points": 0, "swap": 0.0,
                 "commission": 0.0, "from_order": None}
        return {"positions": [{**blank, **p} for p in self.broker_positions()]}

    def handle_quotes_subscribe(self, params):
        symbols = list(params.get("symbols") or [])
        for s in symbols:
            self._instrument(s)
        with self._lock:
            self._quoted.update(symbols)
            for s in symbols:  # send the price now, even if it has not moved
                self._last_quote.pop(s, None)
        self._wake.set()
        return {}

    def handle_quotes_unsubscribe(self, params):
        with self._lock:
            self._quoted.difference_update(params.get("symbols") or [])
        return {}

    def handle_bars_history(self, params):
        self._instrument(params.get("symbol"))
        self._timeframe(params.get("timeframe"))
        count = max(1, min(int(params.get("count") or 100), MAX_BARS))
        return {"bars": self.broker_bars(params["symbol"], params["timeframe"], count)}

    def handle_bars_subscribe(self, params):
        symbol, timeframe = params.get("symbol"), params.get("timeframe")
        self._instrument(symbol)
        size = self._timeframe(timeframe)
        with self._lock:  # bars that closed before now were not asked for
            self._bar_subs.setdefault((symbol, timeframe),
                                      (self.now_ms() // size - 1) * size)
        return {}

    def handle_bars_unsubscribe(self, params):
        with self._lock:
            self._bar_subs.pop((params.get("symbol"), params.get("timeframe")), None)
        return {}

    def handle_order_place(self, params):
        symbol, side = params.get("symbol"), params.get("side")
        spec = self._instrument(symbol)
        if (params.get("type") or "market") != "market":
            raise AdapterError("unsupported", "only market orders are placed here")
        if side not in ("buy", "sell"):
            raise AdapterError("x_bad_side", "side must be buy or sell")
        qty = float(params.get("qty") or 0)
        steps = qty / spec["lot_step"]
        if not spec["min_qty"] <= qty <= spec["max_qty"] or abs(steps - round(steps)) > 1e-6:
            raise AdapterError("invalid_qty", f"{symbol} trades from {spec['min_qty']} "
                               f"to {spec['max_qty']} in steps of {spec['lot_step']}")
        cid = params.get("client_id")
        fill = self.broker_place_order(symbol, side, qty, params.get("sl"),
                                       params.get("tp"), cid)
        self.emit("order.accepted", {"client_id": cid, "order_id": fill["order_id"]})
        self.emit("order.filled", {
            "fill_id": fill["fill_id"], "client_id": cid,
            "order_id": fill["order_id"], "position_id": fill["position_id"],
            "symbol": symbol, "side": side, "qty": fill["qty"],
            "price": fill["price"], "fees": fill.get("fees", 0.0),
            "time": fill["time"]})
        return {"order_id": fill["order_id"], "status": "accepted"}

    def handle_order_modify(self, params):
        pid = params.get("position_id")
        held = next((p for p in self.broker_positions() if p["position_id"] == pid), None)
        if held is None:
            raise AdapterError("not_found", f"no open position {pid!r}")
        # A level left out of the request stays as it is; null removes it.
        sl = params["sl"] if "sl" in params else held.get("sl")
        tp = params["tp"] if "tp" in params else held.get("tp")
        self.broker_set_stops(pid, sl, tp)
        return {}

    def handle_position_close(self, params):
        qty = params.get("qty")
        if qty is not None and not float(qty) > 0:
            raise AdapterError("invalid_qty", "qty must be above 0")
        self._close(params.get("position_id"), qty, "manual")
        return {}

    def handle_flatten_all(self, params):
        closed, failed = 0, []
        for p in self.broker_positions():  # one failure must not stop the rest
            try:
                self._close(p["position_id"], None, "flatten")
                closed += 1
            except Exception as e:
                failed.append(f"{p['position_id']}: {e}")
        if failed:
            raise AdapterError("internal", f"closed {closed}, could not close "
                               + "; ".join(failed), retryable=True)
        return {"closed": closed}

    def handle_history_fills(self, params):
        return {"fills": self.broker_fills(params.get("from"), params.get("to"))}

    def _close(self, position_id, qty, why):
        with self._lock:
            self._closing.add(position_id)
        try:
            self._tell_close(self.broker_close_position(position_id, qty), why)
        finally:
            with self._lock:
                self._closing.discard(position_id)

    def _tell_close(self, c, why=None):
        """Send position.closed once per close, whoever finds it first."""
        with self._lock:
            if c["close_id"] in self._closes_seen:
                return
            if why is None and c["position_id"] in self._closing:
                return  # the request closing it will tell it, with its reason
            self._closes_seen[c["close_id"]] = c["time"]
        self.emit("position.closed", {
            "close_id": c["close_id"], "position_id": c["position_id"],
            "client_id": c.get("client_id"), "qty": c["qty"],
            "price": c["price"], "realized_pnl": c["realized_pnl"],
            "swap": c.get("swap", 0.0), "commission": c.get("commission", 0.0),
            "reason": why or c.get("reason") or "unknown", "time": c["time"]})

    def _pump(self):
        """About once a second, beside the requests: the latest price of each
        symbol asked for, each bar that has closed, and each position your
        broker closed by itself."""
        while True:
            self._wake.wait(PUMP_EVERY_S)
            self._wake.clear()
            try:
                self._pump_once()
                self._say("connected", "")
            except Exception as e:  # one bad answer must not stop the prices
                self._say("degraded", f"{type(e).__name__}: {e}")

    def _pump_once(self):
        with self._lock:
            symbols, bar_subs = sorted(self._quoted), dict(self._bar_subs)
        for s in symbols:
            q = self.broker_price(s)
            if self._last_quote.get(s) != (q["bid"], q["ask"], q["time"]):
                self._last_quote[s] = (q["bid"], q["ask"], q["time"])
                self.emit("quote", {"symbol": s, "bid": q["bid"],
                                    "ask": q["ask"], "time": q["time"]})
        now = self.now_ms()
        for (s, tf), sent in bar_subs.items():
            size = TIMEFRAMES_MS[tf]
            if sent >= (now // size - 1) * size:
                continue  # the newest closed bar has been sent
            bars = self.broker_bars(s, tf, 1)
            with self._lock:
                fresh = bool(bars) and (s, tf) in self._bar_subs and bars[-1][0] > sent
                if fresh:
                    self._bar_subs[(s, tf)] = bars[-1][0]
            if fresh:
                self.emit("bar", {"symbol": s, "timeframe": tf, "bar": bars[-1]})
        for c in self.broker_closes_since(self._closes_from):
            self._tell_close(c)
        # Look a minute back next time, for a close your broker records late.
        self._closes_from = max(self._closes_from, now - 60_000)
        with self._lock:
            self._closes_seen = {k: t for k, t in self._closes_seen.items()
                                 if t >= self._closes_from}

    def _say(self, state, detail):
        if state != self._state:
            self._state = state
            self.emit("session.state", {"state": state, "detail": detail})
            if detail:
                print(f"[{self.name}] {detail}", file=sys.stderr, flush=True)


# =========================================================================
# Part 3: the practice market. It stands in for your broker until part 1
# asks your own; delete it then. Its instruments trade all day, every day,
# with no fees and no overnight charges. Prices are worked out from the
# clock: each instrument has a made-up day by day path since 2020, each day
# is split into the bars inside it, and so on down to one price a second.
# So a bar always agrees with the prices inside it, and a moment always has
# the same price. The account starts at 10,000 USD and is kept in the folder
# the Terminal gives this broker (BRUE_BROKER_STATE), so positions survive a
# restart and stops and targets are honoured while this program is off.
# =========================================================================

def _practice(symbol, name, asset_class, digits, step, max_qty, size, margin):
    tick = 10.0 ** -digits
    return {"symbol": symbol, "name": name, "asset_class": asset_class,
            "quote_ccy": "USD", "digits": digits, "tick_size": tick,
            "tick_value": round(tick * size, 10), "lot_step": step,
            "min_qty": step, "max_qty": max_qty, "contract_size": size,
            "margin_rate": margin,
            "sessions": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
                          "close": "24:00", "tz": "UTC"}],
            "holidays": [], "tradable": True, "shortable": True,
            "trade_mode": "full", "stops_level_points": 0,
            "fees": {"commission_per_lot": 0, "mode": "none", "rate": 0, "min": 0},
            "swap_long": 0, "swap_short": 0, "swap_mode": "none"}


PRACTICE_INSTRUMENTS = [
    _practice("PRACTICE-FX", "Practice currency pair", "fx", 5, 0.01, 50, 100_000, 0.0333),
    _practice("PRACTICE-SHARE", "Practice share", "stock", 2, 1, 10_000, 1, 0.2),
    # 5 % margin: one coin, the ticket's first size, fits the practice account.
    _practice("PRACTICE-COIN", "Practice coin", "crypto", 1, 0.001, 10, 1, 0.05),
]
# symbol: (first price, spread, typical move in a day as a fraction)
_PATH = {"PRACTICE-FX": (1.10000, 0.00010, 0.005),
         "PRACTICE-SHARE": (150.00, 0.04, 0.015),
         "PRACTICE-COIN": (40_000.0, 15.0, 0.03)}
_FIRST_DAY = 18_262  # 2020-01-01, in days since 1970
_SIZES = (86_400, 14_400, 3_600, 1_800, 900, 300, 60, 1)  # 1d 4h 1h 30m 15m 5m 1m 1s
_days = {}
_days_lock = threading.Lock()


def _reach(rng, a, b, wander):
    """How high and how low a random path from a to b goes, `wander` being
    how far such a path strays in that time (one standard deviation)."""
    mid, half = (a + b) / 2, abs(a - b) / 2
    up = math.sqrt(half * half - wander * wander * math.log(1 - rng.random()) / 2)
    down = math.sqrt(half * half - wander * wander * math.log(1 - rng.random()) / 2)
    return mid + up, mid - down


def _day(symbol, day):
    """(open, high, low, close, volume) of one practice day."""
    first, _, move = _PATH[symbol]
    with _days_lock:
        bars = _days.setdefault(symbol, [])
        while len(bars) <= day - _FIRST_DAY:
            rng = random.Random(f"{symbol}:{_FIRST_DAY + len(bars)}")
            o = bars[-1][3] if bars else first
            # a random step that leans back toward the first price
            c = o * math.exp(move * rng.gauss(0, 1) - 0.02 * math.log(o / first))
            bars.append((o, *_reach(rng, o, c, move * o), c, rng.randint(50_000, 150_000)))
        return bars[day - _FIRST_DAY]


def _bar(symbol, level, start):
    """The practice bar of _SIZES[level] seconds that starts at `start`."""
    if level == 0:
        return _day(symbol, start // 86_400)
    parent = start - start % _SIZES[level - 1]
    return _inside(symbol, level - 1, parent)[(start - parent) // _SIZES[level]]


def _bid_at(symbol, second):
    minute = second - second % 60
    return _inside(symbol, 6, minute)[second - minute]


def _likely(v, t, t0, a, t1, b, wander):
    """How likely (as a logarithm) a random path from a at time t0 to b at
    time t1 is to be at v at time t."""
    return (-((v - a) ** 2 / (t - t0) + (v - b) ** 2 / (t1 - t)) / (2 * wander * wander)
            - 0.5 * math.log((t - t0) * (t1 - t)))


def _pick(rng, times, logs):
    top = max(logs)
    return rng.choices(times, [math.exp(x - top) for x in logs])[0]


def _through(rng, anchors, wander):
    """A random path through each (time, price) anchor in turn, read at every
    whole time from the first anchor to the last; `wander` is how far it
    strays in one unit of time."""
    t, x = anchors[0]
    out = [x]
    for t1, v1 in anchors[1:]:
        for k in range(math.floor(t) + 1, math.floor(t1) + 1):
            x += ((v1 - x) * (k - t) / (t1 - t)
                  + rng.gauss(0, wander * ((k - t) * (t1 - k) / (t1 - t)) ** 0.5))
            t = k
            out.append(x)
        if t == t1:
            out[-1] = v1
        t, x = t1, v1
    return out


@lru_cache(maxsize=20_000)
def _inside(symbol, level, start):
    """What is inside one practice bar: the bars one size down, or for a
    minute its sixty prices. They open at its open, close at its close and
    reach exactly its high and its low."""
    o, h, low, c, vol = _bar(symbol, level, start)
    n = _SIZES[level] // _SIZES[level + 1]
    wander = _PATH[symbol][2] * o * (_SIZES[level + 1] / 86_400) ** 0.5  # in one part
    rng = random.Random(f"{symbol}:{level}:{start}")
    seconds = level + 1 == len(_SIZES) - 1
    # The path passes through the high and the low at two different moments
    # (the middle of a part, or one of a minute's seconds), each chosen by how
    # easily a random path gets there from where it is.
    end = n - 1 if seconds else n
    times = list(range(1, n - 1)) if seconds else [i + 0.5 for i in range(n)]
    hi_t = _pick(rng, times, [_likely(h, t, 0, o, end, c, wander) for t in times])
    times.remove(hi_t)
    lo_t = _pick(rng, times, [_likely(low, t, 0, o, hi_t, h, wander) if t < hi_t
                              else _likely(low, t, hi_t, h, end, c, wander) for t in times])
    hi_i, lo_i = int(hi_t), int(lo_t)  # which part, or which second
    path = _through(rng, sorted([(0, o), (hi_t, h), (lo_t, low), (end, c)]), wander)
    # what strays past the high or the low is turned back inside them
    path = [min(h, max(low, 2 * h - x if x > h else 2 * low - x if x < low else x))
            for x in path]
    if seconds:
        return tuple(path)
    weights = [rng.random() + 0.5 for _ in range(n)]
    parts, done, share = [], 0, 0.0
    for i in range(n):
        up, down = _reach(rng, path[i], path[i + 1], wander)
        share += weights[i] / sum(weights)
        v = round(vol * share) - done
        done += v
        parts.append((path[i], h if i == hi_i else min(h, up),
                      low if i == lo_i else max(low, down), path[i + 1], v))
    return tuple(parts)


class PracticeMarket:
    """The practice account. Each method first closes, at the first price
    that reached them, the positions whose stop or target was reached since
    the last look, as a broker does without being asked."""

    def __init__(self, folder):
        self.specs = {i["symbol"]: i for i in PRACTICE_INSTRUMENTS}
        self.lock = threading.RLock()
        self.book = {"balance": 10_000.0, "positions": {}, "fills": [],
                     "closes": [], "orders": {}, "n": 0,
                     "checked": int(time.time())}
        self.file = None
        if folder:
            os.makedirs(folder, exist_ok=True)
            self.file = os.path.join(folder, "practice_account.json")
            if os.path.exists(self.file):
                with open(self.file, encoding="utf-8") as f:
                    self.book.update(json.load(f))

    # -- prices ---------------------------------------------------------
    def _quote(self, symbol, second):
        digits = self.specs[symbol]["digits"]
        bid = round(_bid_at(symbol, second), digits)
        return bid, round(bid + _PATH[symbol][1], digits)

    def instruments(self):
        return [dict(i) for i in PRACTICE_INSTRUMENTS]

    def price(self, symbol):
        now = int(time.time())
        bid, ask = self._quote(symbol, now)
        return {"bid": bid, "ask": ask, "time": now * 1000}

    def bars(self, symbol, timeframe, count):
        size = TIMEFRAMES_MS[timeframe] // 1000
        level, digits = _SIZES.index(size), self.specs[symbol]["digits"]
        last = int(time.time()) // size - 1  # the newest bar that has closed
        out = []
        for k in range(max(last - count + 1, _FIRST_DAY * 86_400 // size), last + 1):
            o, h, low, c, v = _bar(symbol, level, k * size)
            out.append([k * size * 1000] + [round(x, digits) for x in (o, h, low, c)] + [v])
        return out

    # -- the account ----------------------------------------------------
    def _id(self, letter):
        self.book["n"] += 1
        return f"{letter}{self.book['n']}"

    def _exit(self, p, second):
        """What closing p would get: the bid for a buy, the ask for a sell."""
        bid, ask = self._quote(p["symbol"], second)
        return bid if p["side"] == "buy" else ask

    def _pnl(self, p, qty, price):
        sign = 1 if p["side"] == "buy" else -1
        size = self.specs[p["symbol"]]["contract_size"]
        return round(sign * (price - p["avg_price"]) * qty * size, 2)

    def _save(self):
        if not self.file:
            return
        for k in ("fills", "closes"):
            del self.book[k][:-2000]
        self.book["orders"] = dict(list(self.book["orders"].items())[-2000:])
        with open(self.file + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.book, f)
        os.replace(self.file + ".tmp", self.file)

    def _first_touch(self, p, start, end):
        """(second, price, "sl" or "tp") of the first second from start to
        end whose price reaches p's stop or target, or None."""
        buy, sl, tp = p["side"] == "buy", p["sl"], p["tp"]
        spread = _PATH[p["symbol"]][1]
        tick = self.specs[p["symbol"]]["tick_size"]
        minute = start - start % 60
        while minute <= end:
            # a minute whose range cannot reach either level is skipped whole
            _, h, low, _, _ = _bar(p["symbol"], 6, minute)
            lo, hi = (low - tick, h + tick) if buy else (low + spread - tick, h + spread + tick)
            if ((sl is not None and (lo <= sl if buy else hi >= sl))
                    or (tp is not None and (hi >= tp if buy else lo <= tp))):
                for s in range(max(start, minute), min(end, minute + 59) + 1):
                    px = self._exit(p, s)
                    if sl is not None and (px <= sl if buy else px >= sl):
                        return s, px, "sl"
                    if tp is not None and (px >= tp if buy else px <= tp):
                        return s, px, "tp"
            minute += 60
        return None

    def _run_stops(self):
        now = int(time.time())
        hit = False
        for p in list(self.book["positions"].values()):
            if p["sl"] is None and p["tp"] is None:
                continue
            start = max(self.book["checked"], p["opened_at"] // 1000) + 1
            touch = self._first_touch(p, start, now)
            if touch:
                self._close(p, p["qty"], touch[1], touch[2], touch[0])
                hit = True
        self.book["checked"] = now
        if hit:
            self._save()

    def _close(self, p, qty, price, reason, second):
        pnl = self._pnl(p, qty, price)
        self.book["balance"] = round(self.book["balance"] + pnl, 2)
        p["qty"] = round(p["qty"] - qty, 10)
        if p["qty"] <= 0:
            del self.book["positions"][p["position_id"]]
        close = {"close_id": self._id("c"), "position_id": p["position_id"],
                 "client_id": p["client_id"], "symbol": p["symbol"], "qty": qty,
                 "price": price, "realized_pnl": pnl, "swap": 0.0,
                 "commission": 0.0, "reason": reason, "time": second * 1000}
        self.book["closes"].append(close)
        self.book["fills"].append({
            "fill_id": self._id("f"), "order_id": None,
            "position_id": p["position_id"], "client_id": None,
            "symbol": p["symbol"], "side": "sell" if p["side"] == "buy" else "buy",
            "qty": qty, "price": price, "fees": 0.0, "time": second * 1000,
            "realized_pnl": pnl})
        return close

    def _account(self):
        now, open_pnl, used = int(time.time()), 0.0, 0.0
        for p in self.book["positions"].values():
            spec = self.specs[p["symbol"]]
            open_pnl += self._pnl(p, p["qty"], self._exit(p, now))
            used += p["qty"] * spec["contract_size"] * p["avg_price"] * spec["margin_rate"]
        equity = self.book["balance"] + open_pnl
        return {"balance": round(self.book["balance"], 2), "equity": round(equity, 2),
                "margin_used": round(used, 2), "margin_free": round(equity - used, 2),
                "currency": "USD", "account_id": "practice", "label": "Practice account"}

    def account(self):
        with self.lock:
            self._run_stops()
            return self._account()

    def positions(self):
        with self.lock:
            self._run_stops()
            now = int(time.time())
            return [dict(p, unrealized_pnl=self._pnl(p, p["qty"], self._exit(p, now)))
                    for p in self.book["positions"].values()]

    def place(self, symbol, side, qty, sl, tp, client_id):
        with self.lock:
            self._run_stops()
            if client_id in self.book["orders"]:
                return self.book["orders"][client_id]  # the same order again: nothing new
            now = int(time.time())
            bid, ask = self._quote(symbol, now)
            price = ask if side == "buy" else bid
            spec = self.specs[symbol]
            need = qty * spec["contract_size"] * price * spec["margin_rate"]
            free = self._account()["margin_free"]
            if need > free:
                raise AdapterError("insufficient_margin", f"this order needs {need:.2f} "
                                   f"USD of margin and {free:.2f} USD is free")
            p = {"position_id": self._id("p"), "client_id": client_id,
                 "symbol": symbol, "side": side, "qty": qty, "avg_price": price,
                 "sl": sl, "tp": tp, "opened_at": now * 1000}
            self.book["positions"][p["position_id"]] = p
            fill = {"fill_id": self._id("f"), "order_id": self._id("o"),
                    "position_id": p["position_id"], "client_id": client_id,
                    "symbol": symbol, "side": side, "qty": qty, "price": price,
                    "fees": 0.0, "time": now * 1000}
            self.book["fills"].append(fill)
            if client_id:
                self.book["orders"][client_id] = fill
            self._save()
            return fill

    def _held(self, position_id):
        p = self.book["positions"].get(position_id)
        if p is None:
            raise AdapterError("not_found", f"no open position {position_id!r}")
        return p

    def set_stops(self, position_id, sl, tp):
        with self.lock:
            self._run_stops()
            p = self._held(position_id)
            p["sl"], p["tp"] = sl, tp
            self._save()

    def close(self, position_id, qty, reason):
        with self.lock:
            self._run_stops()
            p = self._held(position_id)
            now = int(time.time())
            qty = p["qty"] if qty is None else min(float(qty), p["qty"])
            close = self._close(p, qty, self._exit(p, now), reason, now)
            self._save()
            return close

    def closes_since(self, since_ms):
        with self.lock:
            self._run_stops()
            return [c for c in self.book["closes"] if c["time"] >= since_ms]

    def fills(self, start_ms, end_ms):
        with self.lock:
            self._run_stops()
            return [f for f in self.book["fills"]
                    if (start_ms is None or f["time"] >= start_ms)
                    and (end_ms is None or f["time"] <= end_ms)]


if __name__ == "__main__":
    MyBroker().run()
