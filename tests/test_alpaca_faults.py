"""Alpaca adapter: faults on the way to Alpaca and back, one test each.

Most tests drive the REAL adapter (its threads, its HTTP, its websocket
client, the SDK's dispatch with its idempotency store) against the stand-in
`tests/fake_alpaca.py` in this process, on loopback only. Faults the
stand-in cannot make itself come from `Rules`: an answer LOST after the
request was carried out, an answer cut in half, a request refused or
delayed. A delay is what makes a race happen every time instead of once in
eleven runs.

What each test asserts is the thing a trader would lose money to: what
Alpaca really holds, compared with what the adapter told the platform.
"""
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests.fake_alpaca import FakeAlpaca

_tl = threading.local()


class Rules:
    """Faults for the stand-in. A rule matches a method, a path prefix and
    a substring of the body, `count` times, and then does one of: `drop`
    (carry the request out, then end the socket with no answer),
    `truncate` (send half the body), `fail` (refuse with that status,
    nothing carried out), `before` / `after` (sleep around carrying it
    out)."""

    def __init__(self, fake):
        self.fake, self.rules, self.hits = fake, [], []
        self._answer, self._send = fake._answer, fake._send
        fake._answer, fake._send = self.answer, self.send

    def add(self, method=None, path=None, body=None, count=1, drop=False,
            before=0.0, after=0.0, name="", fail=0, truncate=False,
            then=None):
        """`then`: another rule, with count 0, that this one switches on
        when it is hit ("the look-up that FOLLOWS the lost answer")."""
        r = dict(method=method, path=path, body=body, count=count,
                 drop=drop, before=before, after=after, name=name,
                 fail=fail, truncate=truncate, then=then)
        self.rules.append(r)
        return r

    def clear(self):
        for r in self.rules:
            r["count"] = 0

    def hit(self, name):
        return [h for h in self.hits if h == name]

    def _match(self, method, path, data):
        for r in self.rules:
            if r["count"] <= 0:
                continue
            if r["method"] and r["method"] != method:
                continue
            if r["path"] and not path.startswith(r["path"]):
                continue
            if r["body"] and r["body"].encode() not in (data or b""):
                continue
            r["count"] -= 1
            return r
        return None

    def answer(self, method, path, q, data, headers, service):
        r = self._match(method, path, data)
        _tl.rule = r
        if r:
            self.hits.append(r["name"])
            if r["then"] is not None:
                r["then"]["count"] += 1
            if r["before"]:
                time.sleep(r["before"])
            if r["fail"]:
                return r["fail"], {"Content-Type": "application/json"}, {
                    "code": r["fail"] * 100000,
                    "message": "internal server error occurred"}
        out = self._answer(method, path, q, data, headers, service)
        if r and r["after"]:
            time.sleep(r["after"])
        return out

    def send(self, h, status, headers, out):
        r = getattr(_tl, "rule", None)
        _tl.rule = None
        if r and (r["truncate"] or r["drop"]):
            if r["truncate"]:
                body = out if isinstance(out, bytes) else json.dumps(
                    out, separators=(",", ":")).encode()
                h.send_response(status)
                h.send_header("Content-Type", "application/json")
                h.send_header("Content-Length", str(len(body) + 64))
                h.end_headers()
                h.wfile.write(body[:max(1, len(body) // 2)])
                h.wfile.flush()
            h.close_connection = True
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return None
        return self._send(h, status, headers, out)


class Wire:
    """What the adapter writes to the platform: answers by id, events in
    order. Stands where stdout would be."""

    def __init__(self):
        self.cv = threading.Condition()
        self.answers, self.events, self.raw = {}, [], []

    def write(self, text):
        with self.cv:
            for line in text.splitlines():
                if not line.strip():
                    continue
                self.raw.append(line)
                msg = json.loads(line)
                if msg.get("type") == "res":
                    self.answers[msg["id"]] = msg
                elif msg.get("type") == "event":
                    self.events.append((msg["event"], msg.get("data") or {}))
            self.cv.notify_all()

    def flush(self):
        pass


def until(fn, timeout=8.0, step=0.05):
    """Poll `fn` until it returns something true; that value, or None."""
    end = time.monotonic() + timeout
    while True:
        got = fn()
        if got or time.monotonic() >= end:
            return got or None
        time.sleep(step)


class Rig:
    """The stand-in, and an adapter talking to it as a platform's would."""

    def __init__(self, market="open", env=None, start=True):
        self.fake = FakeAlpaca(market=market, rate_limit=100000).start()
        self.rules = Rules(self.fake)
        self.state = tempfile.mkdtemp(prefix="alpaca-faults-")
        self.extra_env = dict(env or {})
        self.argv = ["--paper"]
        self.a = self.wire = None
        self._n = 0
        if start:
            self.start()

    def env(self):
        e = {"BRUE_CRED_API_KEY": self.fake.key_id,
             "BRUE_CRED_API_SECRET": self.fake.secret,
             "ALPACA_TRADING_URL": self.fake.url,
             "ALPACA_DATA_URL": self.fake.url,
             "ALPACA_STREAM_URL": self.fake.stream_url,
             "ALPACA_FEED": "iex", "BRUE_BROKER_STATE": self.state}
        e.update(self.extra_env)
        return e

    def start(self, reconcile=True):
        self.wire = Wire()
        self.a = mod.AlpacaAdapter(argv=self.argv, environ=self.env(),
                                   out=self.wire)
        # The stand-in is on this machine: no need to pace at 3 a second.
        b = self.a.client.budget
        b.per_second, b.per_minute = 40, 100000
        b.background = b.background_data = 100000
        if reconcile:
            self.ok("catalog.list")
            self.ok("positions.list")
        return self

    def stop_adapter(self):
        if self.a is not None:
            self.a.on_shutdown()
            self.a = None

    def restart(self, forget=False):
        """A new adapter process on the same account. `forget`: without
        the state directory (what it remembered is gone)."""
        self.stop_adapter()
        self.fake.drop_streams()
        if forget:
            shutil.rmtree(self.state, ignore_errors=True)
            self.state = tempfile.mkdtemp(prefix="alpaca-faults-")
        return self.start()

    def close(self):
        try:
            self.stop_adapter()
        finally:
            self.fake.stop()
            shutil.rmtree(self.state, ignore_errors=True)

    # -- the platform's side
    def cid(self, tag="c"):
        self._n += 1
        return "%s-%d-%d" % (tag, id(self) % 100000, self._n)

    def req(self, method, params=None):
        """("ok", result) or ("err", {"code", "message", "retryable",
        "like"}); `self.took` is how long the adapter needed."""
        rid, t = self.send(method, params), time.monotonic()
        msg = self.answer(rid)
        self.took = time.monotonic() - t
        if "error" in msg:
            return "err", msg["error"]
        return "ok", msg.get("result")

    def send(self, method, params=None):
        """Write one request to the adapter, as a platform's connector
        does, and return its id at once. The adapter's read thread only
        hands a request on; the answer comes when it is ready."""
        self._n += 1
        rid = "r%d" % self._n
        self.a._dispatch(rid, method, dict(params or {}))
        return rid

    def answer(self, rid, timeout=12.0):
        """Wait for the answer to request `rid` (the platform waits 10 s)."""
        end = time.monotonic() + timeout
        with self.wire.cv:
            while rid not in self.wire.answers:
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError("no answer to %s in %.0f s"
                                         % (rid, timeout))
                self.wire.cv.wait(left)
            return self.wire.answers[rid]

    def ok(self, method, params=None):
        kind, out = self.req(method, params)
        if kind != "ok":
            raise AssertionError("%s -> %s" % (method, out))
        return out

    def mark(self):
        with self.wire.cv:
            return len(self.wire.events)

    def events(self, since=0, name=None):
        with self.wire.cv:
            evs = list(self.wire.events[since:])
        return [d for n, d in evs if n == name] if name else evs

    def wait(self, name, pred=None, timeout=8.0, since=0):
        return until(lambda: next((d for d in self.events(since, name)
                                   if pred is None or pred(d)), None),
                     timeout)

    def session(self):
        got = self.events(0, "session.state")
        return got[-1] if got else {"state": "connected", "detail": ""}

    # -- the broker's side, read straight from the stand-in
    def live(self, sym=None):
        b = self.fake.broker
        with b.lock:
            return [dict(id=o.id, coid=o.client_order_id, sym=o.inst.symbol,
                         side=o.side, type=o.type, qty=float(o.qty),
                         status=o.status, cls=o.order_class,
                         stop=None if o.stop_price is None
                         else float(o.stop_price),
                         limit=None if o.limit_price is None
                         else float(o.limit_price))
                    for o in b.live.values()
                    if sym is None or o.inst.symbol == sym]

    def held(self, sym):
        b = self.fake.broker
        with b.lock:
            pos = b.positions.get(sym.replace("/", ""))
            return float(pos[1]) if pos else 0.0

    def stops(self, sym):
        return [(o["qty"], o["stop"]) for o in self.live(sym)
                if o["type"] == "stop"]

    def other(self, method, path, body=None):
        """A request by ANOTHER client of the same account."""
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.fake.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"APCA-API-KEY-ID": self.fake.key_id,
                     "APCA-API-SECRET-KEY": self.fake.secret,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    def log(self, method=None, path=None):
        return [q for q in self.fake.requests()
                if (method is None or q["method"] == method)
                and (path is None or q["path"].startswith(path))]

    # -- common beginnings
    def buy(self, sym="AAPL", qty=10, **more):
        m, cid = self.mark(), self.cid()
        out = self.ok("order.place", dict(
            {"client_id": cid, "symbol": sym, "side": "buy", "qty": qty},
            **more))
        if not self.wait("order.filled", lambda d: d["client_id"] == cid,
                         since=m):
            raise AssertionError("no fill for %s" % sym)
        return cid, out

    def long_with_stop(self, sym="AAPL", qty=10, sl=300.0):
        """A long position with a plain stop order under it."""
        self.buy(sym, qty)
        self.ok("order.modify", {"client_id": self.cid("m"),
                                 "position_id": sym, "sl": sl})
        if not until(lambda: self.stops(sym) == [(float(qty), sl)]):
            raise AssertionError("no stop at the broker: %s" % self.live())

    def shown(self, sym="AAPL"):
        for p in self.ok("positions.list")["positions"]:
            if p["position_id"] == sym:
                return p["qty"], p["sl"], p["tp"]
        return None


class Faults(unittest.TestCase):
    """The base: quick timers, one rig, and the check every test ends
    with, whatever else it asserts."""

    QUICK = {"PROTECT_GRACE": 1.0, "PROTECT_SAY": 2.0, "ORPHAN_SAY": 0.6,
             "CLOSE_SAY": 1.0, "AUDIT_SEC": 2.0}

    def setUp(self):
        for name, value in self.QUICK.items():
            p = patch.object(mod, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        self.rigs = []
        self.addCleanup(lambda: [r.close() for r in self.rigs])
        # the adapter's stderr is noise here (ALPACA_TEST_STDERR=1 shows it)
        self.err = io.StringIO()
        if not os.environ.get("ALPACA_TEST_STDERR"):
            p = patch.object(mod.sys, "stderr", self.err)
            p.start()
            self.addCleanup(p.stop)

    def rig(self, **kw):
        r = Rig(**kw)
        self.rigs.append(r)
        return r

    def settled(self, r, sym="AAPL", timeout=8.0, hold=1.2):
        """The invariant, read at the BROKER: no exit order of ours without
        a position behind it or for more than it, and the adapter shows
        exactly the levels the broker holds. It must be true and STAY true
        for `hold` seconds (a glimpse between two steps is not a state).
        `self.last` is the last (position, exits, shown) seen."""
        good = [None]

        def look():
            ok = once()
            if not ok:
                good[0] = None
                return False
            good[0] = good[0] or time.monotonic()
            return time.monotonic() - good[0] >= hold

        def once():
            pos = r.held(sym)
            exits = [o for o in r.live(sym)
                     if o["coid"].startswith(("brue-sl-", "brue-tp-"))
                     or o["cls"] in ("oto", "bracket", "oco")]
            shown = r.shown(sym)
            self.last = (pos, [(o["type"], o["side"], o["qty"], o["status"])
                               for o in exits], shown)
            if pos == 0:
                return not exits and shown is None
            if any(o["qty"] > abs(pos) for o in exits):
                return False
            sl = [o["stop"] for o in exits if o["type"] == "stop"]
            return (shown is not None
                    and shown[1] == (sl[0] if sl else None))
        return until(look, timeout, 0.15)


class Protection(Faults):
    def test_no_stop_is_left_resting_on_a_flat_account(self):
        # Partial close, then the rest closed WHILE the stop for what was
        # left is on its way to Alpaca. The stop used to arrive after the
        # position had gone: a sell stop on a flat account, in no list.
        r = self.rig()
        r.long_with_stop()
        r.rules.add(method="POST", path="/v2/orders", body="brue-sl-",
                    before=1.2, name="stop for the rest")
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(lambda: r.rules.hit("stop for the rest"), 6))
        kind, out = r.req("position.close", {"client_id": r.cid("y"),
                                             "position_id": "AAPL"})
        self.assertEqual(kind, "ok", out)
        self.assertTrue(until(lambda: r.held("AAPL") == 0, 6))
        time.sleep(1.6)       # the delayed stop has landed by now, if ever
        self.assertTrue(self.settled(r), self.last)
        self.assertEqual(r.live("AAPL"), [])
        # and the market falling through 300 opens nothing
        r.fake.set_price("AAPL", price=295.0)
        time.sleep(1.0)
        self.assertEqual(r.held("AAPL"), 0)
        self.assertEqual(r.session()["state"], "connected", r.session())

    def test_an_exit_order_with_no_position_is_cancelled_said_and_shown(self):
        # A stop of ours resting with nothing behind it (left by an earlier
        # run). It must go; while it cannot be cancelled the session says
        # so and orders.list shows it.
        r = self.rig(start=False)
        st, o = r.other("POST", "/v2/orders", {
            "symbol": "AAPL", "side": "sell", "type": "stop", "qty": "6",
            "stop_price": "300", "time_in_force": "gtc",
            "client_order_id": "brue-sl-left-behind"})
        self.assertEqual(st, 200, o)
        r.rules.add(method="DELETE", path="/v2/orders/", fail=500, count=4,
                    name="cancel refused")
        r.start()
        said = until(lambda: r.session()["state"] == "degraded"
                     and "AAPL" in r.session()["detail"] and r.session(), 8)
        self.assertTrue(said, r.session())
        listed = r.ok("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(x["symbol"], x["type"], x["side"], x["qty"])
                          for x in listed], [("AAPL", "stop", "sell", 6.0)])
        # the refusals end: it is cancelled, and the session recovers
        self.assertTrue(until(lambda: not r.live("AAPL"), 20), r.live())
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 6), r.session())
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])

    def test_an_exit_that_fires_with_no_position_is_never_silent(self):
        r = self.rig(start=False)
        r.other("POST", "/v2/orders", {
            "symbol": "AAPL", "side": "sell", "type": "stop", "qty": "6",
            "stop_price": "300", "time_in_force": "gtc",
            "client_order_id": "brue-sl-left-behind"})
        r.rules.add(method="DELETE", path="/v2/orders", fail=500, count=99)
        r.start()
        m = r.mark()
        r.fake.set_price("AAPL", price=295.0)
        self.assertTrue(until(lambda: r.held("AAPL") == -6, 5))
        fill = r.wait("order.filled", lambda d: d["symbol"] == "AAPL",
                      since=m)
        self.assertTrue(fill, r.events(m))
        self.assertEqual((fill["side"], fill["qty"], fill["client_id"]),
                         ("sell", 6.0, None))
        self.assertTrue(until(lambda: "OPENED" in r.session()["detail"], 4),
                        r.session())

    def test_a_short_never_keeps_a_buy_stop_it_no_longer_needs(self):
        # A short with a stop above it (an OTO leg). Covered with a plain
        # BUY, the buy stop stayed behind: a long would open on the next
        # rally, with no event.
        r = self.rig()
        m, cid = r.mark(), r.cid()
        r.ok("order.place", {"client_id": cid, "symbol": "SPY",
                             "side": "sell", "qty": 10, "sl": 800.0})
        self.assertTrue(r.wait("order.filled", since=m))
        self.assertEqual(r.shown("SPY"), (10.0, 800.0, None))
        # Seen on paper since (2026-11-10 17:47 UTC): Alpaca REFUSES that
        # plain buy while the stop holds the quantity. The adapter says
        # what to do instead, and nothing was touched.
        kind, out = r.req("order.place", {
            "client_id": r.cid(), "symbol": "SPY", "side": "buy", "qty": 10})
        self.assertEqual((kind, out.get("code"), out.get("like")),
                         ("err", "x_alpaca_qty_held", "invalid_qty"), out)
        self.assertIn("position.close", out["message"])
        self.assertEqual((r.held("SPY"), r.shown("SPY")),
                         (-10.0, (10.0, 800.0, None)))
        # however the position goes, its stop goes with it. Here: the
        # position vanishes with the stop on (closed by another client of
        # the account through a door that leaves the stop resting).
        m = r.mark()
        b = r.fake.broker
        with b.lock:
            b.positions.pop("SPY", None)
        r.fake.broker.poke()
        self.assertTrue(self.settled(r, "SPY", timeout=12), self.last)
        self.assertEqual(r.live("SPY"), [])
        # the market going through 800 opens nothing
        r.fake.set_price("SPY", price=805.0)
        time.sleep(1.0)
        self.assertEqual(r.held("SPY"), 0)

    def test_a_refused_close_never_touches_the_stop(self):
        r = self.rig()
        r.long_with_stop()
        r.fake.requests(clear=True)
        for qty, code in ((11, "invalid_qty"), (2.5, "invalid_qty")):
            kind, out = r.req("position.close", {
                "client_id": r.cid("x"), "position_id": "AAPL", "qty": qty})
            self.assertEqual((kind, out.get("code")), ("err", code), out)
        # (d) the request was judged before anything was released
        self.assertEqual(r.log("DELETE"), [])
        self.assertEqual(r.stops("AAPL"), [(10.0, 300.0)])
        self.assertEqual(r.shown(), (10.0, 300.0, None))

    def test_a_retried_close_does_not_take_the_stop_off_again(self):
        # The close went through; its answer and the look-up were lost, so
        # the platform was told to try again. The retry used to cancel the
        # stop of what was left and leave it off.
        r = self.rig()
        r.long_with_stop()
        cid = r.cid("x")
        lookup = r.rules.add(method="GET", count=0, fail=500,
                             path="/v2/orders:by_client_order_id")
        r.rules.add(method="POST", path="/v2/orders", body=cid, drop=True,
                    then=lookup)
        m = r.mark()
        params = {"client_id": cid, "position_id": "AAPL", "qty": 4}
        kind, out = r.req("position.close", params)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertTrue(r.wait("position.closed", lambda d: d["qty"] == 4.0,
                               since=m))
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, 300.0)]),
                        r.live())
        r.fake.requests(clear=True)
        self.assertEqual(r.req("position.close", params), ("ok", {}))
        time.sleep(1.5)
        self.assertEqual(r.log("DELETE"), [])
        self.assertEqual((r.held("AAPL"), r.stops("AAPL")),
                         (6.0, [(6.0, 300.0)]))
        self.assertEqual(r.shown(), (6.0, 300.0, None))
        self.assertEqual(len(r.events(m, "position.closed")), 1)

    def test_a_close_order_that_dies_is_said_and_the_stop_comes_back(self):
        r = self.rig()
        r.long_with_stop()
        r.fake.set_fill_delay(60)         # the venue takes orders, fills none
        m = r.mark()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"}), ("ok", {}))
        # a close that will not end is said, and listed so it can be ended
        self.assertTrue(until(lambda: "closing AAPL" in r.session()["detail"],
                              5), r.session())
        listed = r.ok("orders.list", {"state": "resting"})["orders"]
        self.assertEqual([(x["symbol"], x["type"], x["side"])
                          for x in listed], [("AAPL", "market", "sell")])
        self.assertEqual(r.shown(), (10.0, None, None))   # no stop: not shown
        # Alpaca ends it without a fill (here: somebody cancels it)
        st, _ = r.other("DELETE", "/v2/orders/" + [
            o["id"] for o in r.live("AAPL") if o["type"] == "market"][0])
        self.assertEqual(st, 204)
        said = until(lambda: next((d for d in r.events(m, "session.state")
                                   if "ended canceled" in d["detail"]), None))
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertEqual(said["state"], "degraded")
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 300.0)]),
                        r.live())
        self.assertEqual(r.shown(), (10.0, 300.0, None))
        self.assertEqual(r.events(m, "position.closed"), [])

    def test_a_cancel_whose_answer_is_lost_does_not_leave_the_position_bare(self):
        r = self.rig()
        r.long_with_stop()
        for method, params in (
                ("order.modify", {"position_id": "AAPL", "sl": 300.0,
                                  "tp": 400.0}),
                ("position.close", {"position_id": "AAPL", "qty": 4})):
            with self.subTest(method):
                r.rules.add(method="DELETE", path="/v2/orders/", drop=True)
                kind, out = r.req(method, dict(params,
                                               client_id=r.cid("x")))
                if kind == "err":
                    self.assertTrue(out["retryable"], out)
                # whatever was answered: Alpaca holds a stop for what is
                # held, and that is what positions.list shows
                self.assertTrue(self.settled(r), self.last)
                self.assertNotEqual(self.last[1], [], self.last)
                self.assertEqual(self.last[2][1], 300.0, self.last)

    def test_a_restart_finds_the_legs_of_an_entry_that_has_filled(self):
        for label, levels in (("bracket", {"sl": 300.0, "tp": 400.0}),
                              ("oto", {"sl": 300.0})):
            with self.subTest(label):
                r = self.rig()
                r.buy("AAPL", 10, **levels)
                want = (10.0, levels.get("sl"), levels.get("tp"))
                self.assertEqual(r.shown(), want)
                r.restart()
                self.assertEqual(r.shown(), want)
                # its own exits are not the platform's orders ...
                self.assertEqual(
                    r.ok("orders.list", {"state": "resting"})["orders"], [])
                # ... and both of these work on them
                r.ok("order.modify", {"client_id": r.cid("m"),
                                      "position_id": "AAPL", "sl": 310.0})
                self.assertEqual(r.shown()[1], 310.0)
                m = r.mark()
                self.assertEqual(r.req("position.close", {
                    "client_id": r.cid("x"), "position_id": "AAPL"}),
                    ("ok", {}))
                self.assertTrue(r.wait("position.closed", since=m))
                self.assertTrue(self.settled(r), self.last)
                self.assertEqual(r.live("AAPL"), [])

    def test_without_its_memory_the_legs_are_found_at_alpaca(self):
        # The state directory is gone. The legs are found under the order
        # that opened the position (the held stop is in no open list),
        # and are the position's stop and target as before (the cases
        # that are not that plain: tests/test_alpaca_levels.py,
        # WithoutMemory).
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)
        r.restart(forget=True)
        self.assertEqual(r.shown(), (10.0, 300.0, 400.0))
        m = r.mark()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(self.settled(r), self.last)

    def test_a_close_the_wash_trade_rule_would_refuse_touches_no_stop(self):
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0)                  # the stop is an OTO leg
        kind, out = r.req("order.place", {
            "client_id": r.cid(), "symbol": "AAPL", "side": "buy", "qty": 1,
            "type": "limit", "price": 200.0})
        self.assertEqual(kind, "ok", out)             # the user's own bid
        time.sleep(0.3)
        r.fake.requests(clear=True)
        kind, out = r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL", "qty": 4})
        self.assertEqual((kind, out.get("code"), out.get("like")),
                         ("err", "x_alpaca_wash_trade", "trade_restricted"),
                         out)
        self.assertEqual(r.log("DELETE"), [])
        self.assertEqual(r.stops("AAPL"), [(10.0, 300.0)])
        self.assertEqual(r.shown(), (10.0, 300.0, None))
        self.assertEqual(r.session()["state"], "connected")

    def test_a_stop_cancelled_by_somebody_else_is_not_put_back(self):
        # It is not made again. The rule "a level the platform set is the
        # platform's until IT clears it" is WITHDRAWN: the account's owner
        # cancelling his stop in Alpaca's own app is not to be overruled,
        # and a target made again under a market that had moved sold the
        # position. Dropped, said once, not shown
        # (tests/test_alpaca_levels.py, RemovedOutside, has the cases).
        r = self.rig()
        r.long_with_stop()
        m = r.mark()
        st, _ = r.other("DELETE", "/v2/orders/" + r.live("AAPL")[0]["id"])
        self.assertEqual(st, 204)
        self.assertTrue(until(lambda: not r.live("AAPL"), 3))
        time.sleep(4.0)
        self.assertEqual((r.live("AAPL"), r.shown()),
                         ([], (10.0, None, None)))
        self.assertTrue([d for d in r.events(m, "session.state")
                         if "removed outside the terminal" in d["detail"]])

    def test_a_refused_stop_is_said_naming_the_symbol_and_tried_again(self):
        # (b): "if that fails it is said loudly and retried, never forgotten"
        r = self.rig()
        r.long_with_stop()
        r.rules.add(method="POST", path="/v2/orders", body="brue-sl-",
                    fail=500, count=3, name="refused")
        # the adapter takes the stop off itself, to re-size it
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        said = until(lambda: r.session()["state"] == "degraded"
                     and "AAPL" in r.session()["detail"] and r.session(), 8)
        self.assertTrue(said, r.session())
        self.assertEqual(r.shown(), (6.0, None, None))    # not held: not shown
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, 300.0)], 25),
                        r.live())
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 6), r.session())
        self.assertGreaterEqual(len(r.rules.hit("refused")), 2)

    def test_a_cancel_that_never_arrived_does_not_hide_a_stop_that_is_there(self):
        # The request to cancel the stop is REFUSED on the way (nothing is
        # carried out). The stop is still at Alpaca: it must be shown, and
        # nothing may claim for ever that the position has none.
        r = self.rig()
        r.long_with_stop()
        r.rules.add(method="DELETE", path="/v2/orders/", fail=500,
                    name="never arrives")
        kind, out = r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL", "qty": 4})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertEqual(r.rules.hit("never arrives"), ["never arrives"])
        self.assertEqual((r.held("AAPL"), r.stops("AAPL")),
                         (10.0, [(10.0, 300.0)]))
        self.assertTrue(until(lambda: r.shown() == (10.0, 300.0, None), 12),
                        r.shown())
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 8), r.session())
        self.assertTrue(self.settled(r), self.last)
        # and the close, asked again, goes through
        m = r.mark()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL", "qty": 4}),
            ("ok", {}))
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(lambda: r.stops("AAPL") == [(6.0, 300.0)], 6),
                        r.live())

    def test_a_level_with_float_noise_is_not_moved_for_ever(self):
        r = self.rig()
        r.buy("AAPL", 5)
        noisy = 300 + 0.1 + 0.2                    # 300.30000000000001
        r.ok("order.modify", {"position_id": "AAPL", "sl": noisy})
        self.assertEqual(r.stops("AAPL"), [(5.0, 300.3)])
        r.fake.requests(clear=True)
        time.sleep(3.0)                            # several backstop passes
        self.assertEqual((r.log("PATCH"), r.log("POST"), r.log("DELETE")),
                         ([], [], []))
        self.assertEqual(r.shown(), (5.0, 300.3, None))
        self.assertEqual(r.session()["state"], "connected")

    def test_an_exit_firing_in_clips_right_after_the_entry_raises_no_alarm(self):
        # The repair is at work for the fresh position when its stop fires
        # (and, for a pair made by order.modify, fires in three parts).
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)
        m = r.mark()
        r.fake.set_price("AAPL", price=295.0)        # at once
        closed = r.wait("position.closed", since=m)
        self.assertEqual((closed["qty"], closed["reason"]), (10.0, "sl"))
        r.buy("MSFT", 9)
        r.ok("order.modify", {"position_id": "MSFT", "sl": 500.0,
                              "tp": 600.0})
        r.fake.next_fill(clips=3, interval=0.2)
        r.fake.set_price("MSFT", price=495.0)
        self.assertTrue(until(lambda: r.held("MSFT") == 0, 6))
        self.assertTrue(until(lambda: len([
            c for c in r.events(m, "position.closed")
            if c["position_id"] == "MSFT"]) == 3, 6), r.events(m))
        self.assertEqual([(c["qty"], c["reason"])
                          for c in r.events(m, "position.closed")
                          if c["position_id"] == "MSFT"], [(3.0, "sl")] * 3)
        time.sleep(1.5)
        self.assertEqual([d for d in r.events(m, "session.state")
                          if d["state"] != "connected"], [])
        self.assertTrue(self.settled(r, "AAPL"), self.last)
        self.assertTrue(self.settled(r, "MSFT"), self.last)

    def test_what_was_said_about_a_slow_close_is_unsaid_when_it_ends(self):
        r = self.rig()
        r.buy("AAPL", 10)
        r.fake.set_fill_delay(2.2)       # slower than CLOSE_SAY (1 s here)
        m = r.mark()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(until(lambda: "closing AAPL" in r.session()["detail"],
                              4), r.session())
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 5), r.session())
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])

    def test_low_a_bracket_stop_firing_is_not_a_false_alarm(self):
        # Alpaca cancels the target the moment the stop triggers; the
        # stop's fill comes a moment later. That is the pair working.
        r = self.rig()
        r.buy("AAPL", 10, sl=300.0, tp=400.0)
        r.fake.set_fill_delay(1.6)
        m = r.mark()
        r.fake.set_price("AAPL", price=295.0)
        closed = r.wait("position.closed", since=m, timeout=8)
        self.assertTrue(closed, r.events(m))
        self.assertEqual(closed["reason"], "sl")
        time.sleep(1.0)
        self.assertEqual([d for d in r.events(m, "session.state")
                          if d["state"] != "connected"], [])
        self.assertTrue(self.settled(r), self.last)


class Orders(Faults):
    def test_a_bracket_whose_answer_is_lost_keeps_its_exits(self):
        # Alpaca took the order and made its legs; the answer never came.
        # The order was found again by its client id, but its exits were
        # not: sl/tp null, the legs listed as a stranger's orders.
        r = self.rig()
        r.rules.add(method="POST", path="/v2/orders", body="order_class",
                    drop=True)
        m, cid = r.mark(), r.cid()
        kind, out = r.req("order.place", {
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10,
            "sl": 300.0, "tp": 400.0})
        self.assertEqual(kind, "ok", out)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m))
        self.assertEqual([d["client_id"]
                          for d in r.events(m, "order.accepted")], [cid])
        self.assertEqual(r.shown(), (10.0, 300.0, 400.0))
        self.assertEqual(r.events(m, "order.placed"), [])
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])
        m = r.mark()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(self.settled(r), self.last)
        self.assertEqual(r.live("AAPL"), [])

    def test_a_bracket_of_unknown_fate_is_not_announced_as_a_strangers(self):
        # The bracket reached Alpaca; its answer AND the look-up were lost.
        # Its legs are heard on the stream with nothing to say whose they
        # are, except the group id they share with the entry. They must
        # not be announced as somebody else's resting orders, nor be
        # cancelled as exits "no level asks for".
        r = self.rig()
        cid = r.cid()
        lookup = r.rules.add(method="GET", count=0, fail=500,
                             path="/v2/orders:by_client_order_id")
        r.rules.add(method="POST", path="/v2/orders", body=cid, drop=True,
                    then=lookup)
        # ... and Alpaca cannot be asked for a while yet
        r.rules.add(method="GET", path="/v2/orders:by_client_order_id",
                    fail=500, count=3, name="still cannot be asked")
        params = {"client_id": cid, "symbol": "AAPL", "side": "buy",
                  "qty": 10, "sl": 300.0, "tp": 400.0}
        m = r.mark()
        kind, out = r.req("order.place", params)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m))
        self.assertTrue(until(lambda: r.shown() == (10.0, 300.0, 400.0), 12),
                        r.shown())
        time.sleep(2.5)           # past the grace: nothing was cancelled
        self.assertEqual(sorted((o["type"], o["status"])
                                for o in r.live("AAPL")),
                         [("limit", "new"), ("stop", "held")])
        self.assertEqual(r.events(m, "order.placed"), [])
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertEqual(r.ok("orders.list", {"state": "resting"})["orders"],
                         [])
        # while Alpaca cannot be asked, the retry is told the same thing
        kind, out = r.req("order.place", params)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        r.rules.clear()
        kind, out = r.req("order.place", params)         # and then answered
        self.assertEqual(kind, "ok", out)
        self.assertEqual(r.shown(), (10.0, 300.0, 400.0))
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertEqual(len(r.log("POST", "/v2/orders")), 1)
        self.assertTrue(self.settled(r), self.last)

    def test_an_order_of_unknown_fate_is_never_announced_rejected(self):
        # The order reached Alpaca; its answer AND the look-up were lost.
        # "Rejected" was announced for an order that then filled, and the
        # retry the message advised was refused again (for lack of the
        # margin the first attempt was using).
        r = self.rig()
        cid = r.cid()
        lookup = r.rules.add(method="GET", count=0, fail=500,
                             path="/v2/orders:by_client_order_id")
        r.rules.add(method="POST", path="/v2/orders", body=cid, drop=True,
                    then=lookup)
        params = {"client_id": cid, "symbol": "AAPL", "side": "buy",
                  "qty": 400}
        m = r.mark()
        kind, out = r.req("order.place", params)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertTrue(r.wait("order.filled",
                               lambda d: d["client_id"] == cid, since=m))
        kind, out = r.req("order.place", params)       # the advised retry
        self.assertEqual(kind, "ok", out)
        time.sleep(0.6)
        mine = [n for n, d in r.events(m) if d.get("client_id") == cid]
        self.assertNotIn("order.rejected", mine)
        self.assertEqual((mine.count("order.accepted"),
                          mine.count("order.filled")), (1, 1), mine)
        self.assertEqual(r.held("AAPL"), 400)           # one order, not two
        self.assertEqual(len(r.log("POST", "/v2/orders")), 1)

    def test_a_replay_this_profile_has_forgotten_is_asked_of_alpaca_first(self):
        # The state directory is gone. The replay's pre-checks see an
        # account that the ORIGINAL order has already changed; Alpaca is
        # asked for the client id before any of them may refuse.
        r = self.rig()
        cid, first = r.buy("AAPL", 400)
        r.restart(forget=True)
        m = r.mark()
        kind, out = r.req("order.place", {
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 400})
        self.assertEqual((kind, out), ("ok", first))
        time.sleep(0.6)
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertEqual(r.held("AAPL"), 400)
        # and an order that ended long ago is not "in flight" for ever
        self.assertEqual([x["id"] for x in r.a._orders.values()
                          if not x["done"]], [])
        # the same id for ANOTHER order is still a conflict
        kind, out = r.req("order.place", {
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 401})
        self.assertEqual((kind, out["code"]), ("err", "duplicate_client_id"))

    def test_a_close_with_the_stream_down_is_still_reported(self):
        # Stream down; the platform reads positions.list right after its
        # close. That read used to swallow the close: no position.closed,
        # ever, and a phantom short in the adapter's book.
        r = self.rig()
        r.buy("AAPL", 10)

        def refuse(h):
            h.send_response(503)
            h.send_header("Content-Length", "0")
            h.end_headers()
            h.close_connection = True
        r.fake._upgrade = refuse
        r.fake.drop_streams()
        self.assertTrue(until(lambda: r.session()["state"] == "degraded"
                              and "stream" in r.session()["detail"], 15),
                        r.session())
        time.sleep(1.2)       # the backstop's first read after the drop
        m, t0 = r.mark(), time.monotonic()
        self.assertEqual(r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(until(lambda: r.held("AAPL") == 0, 3))
        self.assertEqual(r.ok("positions.list")["positions"], [])
        closed = r.wait("position.closed", since=m, timeout=22)
        self.assertTrue(closed, r.events(m))
        self.assertEqual((closed["qty"], closed["reason"]), (10.0, "manual"))
        # the close is tracked, so it is polled every second: not 15 s late
        self.assertLess(time.monotonic() - t0, 6.0)
        self.assertEqual(r.ok("positions.list")["positions"], [])
        self.assertEqual(len(r.events(m, "position.closed")), 1)

    def test_an_unknown_clock_is_never_market_closed(self):
        r = self.rig()
        r.buy("AAPL", 5)
        r.restart()                   # a fresh session: no clock remembered
        r.rules.add(path="/v2/clock", fail=500, count=99)
        r.fake.requests(clear=True)
        kind, out = r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL"})
        self.assertEqual(kind, "err", out)
        self.assertNotEqual(out["code"], "market_closed", out)
        self.assertNotEqual(out.get("like"), "market_closed", out)
        self.assertTrue(out["retryable"], out)
        self.assertEqual((r.log("POST"), r.log("DELETE"), r.held("AAPL")),
                         ([], [], 5.0))
        # the panic button does not wait for a clock: Alpaca is the judge
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("f")}),
                         ("ok", {"closed": 1}))
        self.assertEqual(r.held("AAPL"), 0)


class Flatten(Faults):
    def test_flatten_right_after_a_partial_close_gets_flat(self):
        # The backstop is putting the stop back for what a partial close
        # left, when flatten_all arrives. The stop used to land behind
        # flatten's one cancel-all and hold the quantity for its whole 8 s.
        r = self.rig()
        r.long_with_stop()
        r.rules.add(method="POST", path="/v2/orders", body="brue-sl-",
                    before=1.0, name="stop for the rest")
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        self.assertTrue(until(lambda: r.rules.hit("stop for the rest"), 6))
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out), ("ok", {"closed": 1}))
        self.assertEqual(r.held("AAPL"), 0)
        self.assertTrue(self.settled(r), self.last)
        self.assertEqual(r.live(), [])

    def test_flatten_tries_again_after_a_refusal_that_may_pass(self):
        r = self.rig()
        r.long_with_stop()
        r.rules.add(method="POST", path="/v2/orders", body="brue-flat-",
                    fail=500, name="once")
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out), ("ok", {"closed": 1}))
        self.assertEqual((r.held("AAPL"), r.rules.hit("once")),
                         (0.0, ["once"]))

    def test_one_bad_answer_to_its_own_reads_does_not_end_flatten(self):
        # Not only a refused closing order: a 500 to flatten's read of the
        # positions, or to its cancel-all, used to end the panic button.
        for method, path in (("GET", "/v2/positions"),
                             ("DELETE", "/v2/orders")):
            with self.subTest(path):
                r = self.rig()
                r.long_with_stop()
                r.rules.add(method=method, path=path, fail=500, name="bad")
                kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
                self.assertEqual((kind, out), ("ok", {"closed": 1}))
                self.assertEqual((r.held("AAPL"), r.rules.hit("bad")),
                                 (0.0, ["bad"]))
                self.assertTrue(self.settled(r), self.last)

    def test_flatten_that_cannot_close_puts_the_stop_back(self):
        r = self.rig()
        r.long_with_stop()
        r.rules.add(method="POST", path="/v2/orders", body="brue-flat-",
                    fail=500, count=99)
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("AAPL", out["message"])
        self.assertLess(r.took, 9.6)
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 300.0)], 8),
                        r.live())
        self.assertTrue(self.settled(r), self.last)

    def test_a_429_is_capped_and_never_locks_flatten_out(self):
        r = self.rig()
        r.buy("AAPL", 5)
        r.fake.fail_next(count=1, status=429, retry_after=3600,
                         path="/v2/account")
        kind, out = r.req("account.get")
        self.assertEqual((kind, out["code"]), ("err", "rate_limited"), out)
        pause = r.a.client.budget._blocked["trading"] - time.monotonic()
        self.assertLessEqual(pause, 60.5)         # not the hour it asked
        r.fake.requests(clear=True)
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out), ("ok", {"closed": 1}))
        self.assertTrue(r.log("POST", "/v2/orders"))
        self.assertEqual(r.held("AAPL"), 0)
        # everything else is told what is true: a pause Alpaca asked for
        kind, out = r.req("account.get")
        self.assertEqual((kind, out["code"], out["retryable"]),
                         ("err", "rate_limited", True), out)
        self.assertIn("429", out["message"])

    def test_many_positions_are_closed_by_one_call(self):
        # ("one call" is one flatten_all.) Alpaca's close-all for more
        # than four positions (ONE DELETE /v2/positions and no order of
        # the adapter's) is not used: that request, late or answered 504,
        # sold positions twice and one opened after the flatten. The rule
        # is no close-all, and every position closed by an order of its
        # own (tests/test_alpaca_closing_orders.py OneByOne has the rest).
        r = self.rig()
        syms = ["AAPL", "MSFT", "SPY", "XRX", "DTI", "SNDL"]
        for sym in syms:
            r.buy(sym, 2)
        r.fake.requests(clear=True)
        m = r.mark()
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out), ("ok", {"closed": 6}))
        self.assertEqual(r.log("DELETE", "/v2/positions"), [])
        self.assertEqual(len(r.log("POST", "/v2/orders")), 6)
        closes = until(lambda: len(r.events(m, "position.closed")) == 6
                       and r.events(m, "position.closed"), 5)
        self.assertTrue(closes, r.events(m))
        self.assertEqual({c["reason"] for c in closes}, {"flatten"})
        self.assertEqual({c["position_id"] for c in closes}, set(syms))
        # the adapter's own closing orders are not announced as a
        # stranger's
        self.assertEqual(r.events(m, "order.filled"), [])

    def test_flatten_that_gives_up_leaves_no_order_working(self):
        # The exchange stops dealing while the adapter still believes it
        # open. Alpaca TAKES the closing order and holds it. flatten_all
        # used to give up and leave it working, hidden, with the stop gone.
        r = self.rig()
        r.long_with_stop()
        r.fake.set_market("shut")
        kind, out = r.req("flatten_all", {"client_id": r.cid("f")})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("AAPL", out["message"])
        self.assertLess(r.took, 9.6)
        self.assertTrue(until(
            lambda: not [o for o in r.live("AAPL")
                         if o["type"] == "market"], 4), r.live())
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 300.0)], 8),
                        r.live())
        self.assertEqual(r.shown(), (10.0, 300.0, None))
        self.assertEqual(r.held("AAPL"), 10)


class Robust(Faults):
    def test_a_secret_with_a_line_break_is_refused_by_name_never_echoed(self):
        r = self.rig(start=False, env={
            "BRUE_CRED_API_SECRET": "s3cr3t-FIRST-HALF\nSECOND-HALF"})
        r.start(reconcile=False)
        for method in ("account.get", "catalog.list", "positions.list"):
            kind, out = r.req(method)
            self.assertEqual((kind, out["code"]), ("err", "unauthorized"),
                             out)
            self.assertIn("secret", out["message"])
        said = "\n".join(r.wire.raw) + self.err.getvalue()
        self.assertNotIn("s3cr3t", said)
        self.assertNotIn("SECOND-HALF", said)
        self.assertEqual(r.fake.requests(), [])        # nothing was sent
        hello = r.req("hello", {"protocol": 1})
        self.assertEqual(hello[0], "ok")               # hello stays cold

    def test_no_exception_text_reaches_a_message_unscrubbed(self):
        cfg = mod.resolve_environment(["--paper"], {
            "BRUE_CRED_API_KEY": "PKKEYKEYKEY", "BRUE_CRED_API_SECRET":
            "TOPSECRETVALUE", "ALPACA_TRADING_URL": "http://127.0.0.1:9"})
        client = mod.AlpacaClient(cfg)
        for boom in (ValueError("Invalid header value b'TOPSECRETVALUE'"),
                     OSError("cannot reach TOPSECRETVALUE@host")):
            with patch.object(mod.urllib.request, "urlopen",
                              side_effect=boom):
                with self.assertRaises(mod.AlpacaError) as ctx:
                    client.call("GET", "trading", "/v2/account")
            self.assertEqual(ctx.exception.status, 0)
            self.assertNotIn("TOPSECRETVALUE", ctx.exception.message)

    def test_mode_is_paper_only_for_the_paper_host_or_this_machine(self):
        def mode(url, stream=None):
            env = {"BRUE_CRED_API_KEY": "PKX", "BRUE_CRED_API_SECRET": "y",
                   "ALPACA_TRADING_URL": url}
            if stream:
                env["ALPACA_STREAM_URL"] = stream
            return mod.resolve_environment(["--paper"], env)["mode"]
        for url in ("https://paper-api.alpaca.markets",
                    "https://PAPER-API.alpaca.markets/",
                    "http://127.0.0.1:8080", "http://localhost:1",
                    "http://[::1]:9", "http://127.9.9.9:1"):
            self.assertEqual(mode(url), "paper", url)
        for url in ("https://api.alpaca.markets",
                    "https://api.alpaca.markets./",
                    "https://paper-api.alpaca.markets./",
                    "https://203.0.113.7", "https://forwarder.example",
                    "https://paper-api.alpaca.markets.evil.example",
                    "https://paper-api.alpaca.markets@evil.example/",
                    "https://10.0.0.5:8443"):
            self.assertEqual(mode(url), "live", url)
        # the stream endpoint counts too
        self.assertEqual(mode("https://paper-api.alpaca.markets",
                              "wss://api.alpaca.markets/stream"), "live")
        self.assertEqual(mode("http://127.0.0.1:1",
                              "ws://127.0.0.1:1/stream"), "paper")

    def test_a_handler_answers_inside_one_deadline_however_slow_alpaca_is(self):
        r = self.rig()
        r.long_with_stop()
        slow = r.rules.add(before=2.0, count=10 ** 6)   # every request, late
        kind, out = r.req("position.close", {
            "client_id": r.cid("x"), "position_id": "AAPL", "qty": 4})
        self.assertLessEqual(r.took, 8.7, (kind, out))
        if kind == "err":
            self.assertTrue(out["retryable"], out)
        slow["count"] = 0
        # whatever that left behind: never a position bare and silent
        self.assertTrue(self.settled(r, timeout=20), self.last)
        self.assertNotEqual(self.last[1], [], self.last)

    def test_an_answer_cut_short_is_retryable_and_kills_no_thread(self):
        r = self.rig()
        r.buy("AAPL", 5)
        r.rules.add(method="GET", path="/v2/account", truncate=True)
        kind, out = r.req("account.get")
        self.assertEqual((kind, out["code"], out["retryable"]),
                         ("err", "internal", True), out)
        # the stream thread's own catch-up read gets a broken answer
        r.rules.add(method="GET", path="/v2/account/activities",
                    truncate=True, count=2, name="cut")
        r.fake.drop_streams()
        self.assertTrue(until(lambda: r.rules.hit("cut"), 10))
        self.assertTrue(until(
            lambda: r.fake.state()["listening"] >= 1, 10), r.fake.state())
        time.sleep(1.5)
        self.assertEqual(r.fake.state()["listening"], 1)
        r.buy("SPY", 1)                                 # and it still hears

    def test_one_silent_symbol_does_not_starve_the_other_bar_subscriptions(self):
        # Alpaca's rule for /v2/stocks/bars: rows come symbol by symbol and
        # `limit` counts bars over ALL symbols; the rest is behind
        # next_page_token. One subscribed share that prints no bar used to
        # push the window back until the last symbols got nothing.
        liquid = ["AAPL", "AMZN", "GOOG", "META", "MSFT", "NVDA", "SPY",
                  "TSLA"]
        thin, t0 = "ABR.PRD", 1_794_326_400_000
        clock = {"now": t0 + 30_000}
        wire = Wire()
        a = mod.AlpacaAdapter(argv=["--paper"], environ={
            "BRUE_CRED_API_KEY": "PKX", "BRUE_CRED_API_SECRET": "x",
            "ALPACA_FEED": "iex"}, out=wire)
        a.now_ms = lambda: clock["now"]
        a._catalog = {s: {"symbol": s, "asset_class": "stock"}
                      for s in liquid + [thin]}
        a._catalog_ready.set()

        def call(method, host, path, query=None, body=None, **kw):
            start = mod.parse_ts(query["start"])
            limit = min(int(query["limit"]), 100)     # Alpaca's page
            rows = []
            for sym in sorted(query["symbols"].split(",")):
                t = (start + 59_999) // 60_000 * 60_000
                while sym != thin and t <= clock["now"]:
                    rows.append((sym, {"t": mod.iso_ms(t), "o": 1, "h": 1,
                                       "l": 1, "c": 1, "v": 100}))
                    t += 60_000
            at = int(query.get("page_token") or 0)
            bars = {}
            for sym, b in rows[at:at + limit]:
                bars.setdefault(sym, []).append(b)
            more = at + limit < len(rows)
            return {"bars": bars,
                    "next_page_token": str(at + limit) if more else None}
        a.client.call = call
        for sym in liquid + [thin]:
            a.handle_bars_subscribe({"symbol": sym, "timeframe": "1m"})
        last = {}
        for minute in range(1, 241):
            clock["now"] = t0 + minute * 60_000 + 5_000
            a._poll_bars()
            for name, d in wire.events:
                if name == "bar":
                    last[d["symbol"]] = minute
            del wire.events[:]
        self.assertEqual({s: last.get(s) for s in liquid},
                         {s: 240 for s in liquid})


class Account(Faults):
    def test_balance_is_equity_less_the_open_profit_not_cash(self):
        # `balance` used to be Alpaca's cash, which falls by the whole cost
        # of a purchase: a platform's "open P&L" (equity - balance) then
        # showed the value of the holding.
        r = self.rig()
        # The stand-in's prices walk by the date, so the test sets its own
        # entry: bought near 350, marked at 360, an open profit of about 100.
        r.fake.set_price("AAPL", price=350.0)
        r.buy("AAPL", 10)
        r.fake.set_price("AAPL", price=360.0)        # an open profit
        acct = r.ok("account.get")
        _, pos = r.other("GET", "/v2/positions")
        _, raw = r.other("GET", "/v2/account")
        pl = sum(float(p["unrealized_pl"]) for p in pos)
        self.assertGreater(pl, 30)
        self.assertAlmostEqual(acct["equity"], float(raw["equity"]),
                               places=2)
        self.assertAlmostEqual(acct["equity"] - acct["balance"], pl,
                               places=2)
        self.assertGreater(acct["balance"] - float(raw["cash"]), 3000)
        r.ok("flatten_all", {"client_id": r.cid("f")})
        self.assertTrue(until(lambda: r.held("AAPL") == 0, 4))
        flat = r.ok("account.get")
        self.assertAlmostEqual(flat["balance"], flat["equity"], places=6)


class Priority(Faults):
    """Seen on paper (2026-11-10 18:12 and 18:26 UTC): after a partial
    close the stop took 13 s and 11 s to come back, because the repair's
    requests queued, at Alpaca's pace of three a second, behind a platform
    that was polling positions.list."""

    def test_putting_a_stop_back_goes_before_a_handlers_read(self):
        bud = mod._Budget(per_second=1)
        bud.take("trading", "read", 0)        # this second's one slot
        order = []

        def ask(kind):
            bud.take("trading", kind, 4.0)
            order.append(kind)
        first = threading.Thread(target=ask, args=("read",))
        first.start()
        time.sleep(0.15)                      # the read was there FIRST
        second = threading.Thread(target=ask, args=("protect",))
        second.start()
        first.join(5)
        second.join(5)
        self.assertEqual(order, ["protect", "read"])
        self.assertEqual(bud._urgent, 0)

    def test_a_polling_platform_does_not_keep_a_position_waiting_for_its_stop(self):
        r = self.rig()
        r.long_with_stop()
        r.a.client.budget.per_second = 3      # Alpaca's pace, as in life
        m = r.mark()
        r.ok("position.close", {"client_id": r.cid("x"),
                                "position_id": "AAPL", "qty": 4})
        self.assertTrue(r.wait("position.closed", since=m))
        t0, polls = time.monotonic(), 0
        while r.stops("AAPL") != [(6.0, 300.0)]:
            r.ok("positions.list")            # a platform that keeps asking
            polls += 1
            self.assertLess(time.monotonic() - t0, 6.0, polls)
        self.assertEqual(r.shown(), (6.0, 300.0, None))
        self.assertTrue(until(
            lambda: r.session()["state"] == "connected", 5), r.session())


class Details(unittest.TestCase):
    """Details, each settled by one test."""

    def test_leg_ids_of_working_orders_are_never_dropped_for_being_old(self):
        from tests.test_alpaca_orders import make, order
        state = tempfile.mkdtemp(prefix="alpaca-faults-")
        self.addCleanup(shutil.rmtree, state, True)
        a, _ = make(state_dir=state)
        with a._state_lock:
            for i in range(10):                  # ten legs still working
                a._leg_kind["live-%d" % i] = "sl"
                a._track(order("live-%d" % i, "srv-%d" % i, sym="AAPL",
                               side="sell", typ="stop", status="held"))
            for i in range(700):                 # every in-place stop move
                a._leg_kind["old-%d" % i] = "sl"
                a._track(order("old-%d" % i, "srv-o%d" % i, sym="AAPL",
                               side="sell", typ="stop", status="replaced"))
        a._save_state()
        b, _ = make(state_dir=state)
        self.assertEqual(sorted(k for k in b._leg_kind
                                if k.startswith("live-")),
                         sorted("live-%d" % i for i in range(10)))

    def test_a_fill_and_a_read_inside_one_tick_of_the_clock_are_ordered(self):
        # Windows' monotonic clock ticks every 15 ms. A fill heard DURING
        # a positions read is newer than that read, tick or no tick.
        from tests.test_alpaca_orders import fill, make, order, position
        a, b = make()
        buy = order("o1", "cid-1", sym="AAPL", qty="5")
        with patch.object(mod.time, "monotonic", return_value=1000.0):
            a._intents["cid-1"] = {"kind": "user", "cid": "cid-1"}
            a._on_trade_update(fill(buy, "x1", "350", "5", "5"))
            sell = order("o2", "cid-2", sym="AAPL", side="sell", qty="5")
            a._intents["cid-2"] = {"kind": "user", "cid": "cid-2"}

            def positions(q, body, path):
                # the read is on its way back, still showing 5, when the
                # closing fill is heard
                a._on_trade_update(fill(sell, "x2", "351", "5", "0"))
                return [position("AAPL", "5", asset_id="id-AAPL",
                                 avg_entry_price="350")]
            b.on("GET", "/v2/positions", positions)
            self.assertEqual(a.handle_positions_list({})["positions"], [])
            self.assertNotIn("AAPL", a._book)


if __name__ == "__main__":
    unittest.main()
