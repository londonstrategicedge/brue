"""Alpaca adapter: requests that end without an answer, one class an item.

Run as tests/test_alpaca_closing_orders.py is: the REAL adapter against the
stand-in in this process (the broker's own book beside what the adapter
told the platform), or the adapter over a scripted broker where one
answer is the whole case. Every "late", every 504 and every lost answer
here is the test's own fault injection: Alpaca has not been seen to
carry a request out late. The faults pinned here:

  - flatten_all's cancel-all (`DELETE /v2/orders`) is tied to no
    order. Not answered and carried out nine seconds later, it took
    the stop and target off a position opened AFTER the flatten
    ("removed outside the terminal ... NOT put back"), and on a live
    account the user's later resting orders, with nothing said.
    -> a cancel-all that ended without an answer is remembered for
       ten minutes (in the state file too). While it is, an exit of
       ours found cancelled without this adapter asking is put back
       and that is said; an order of the user's that went the same
       way is named, with why, and never placed again.

  - the ten-minute watch asked Alpaca for every order of unknown
    fate by its own id, one request each a pass: with the stream
    down the backstop was refused its reads and order.filled came
    16 s late with three orders on watch, over 40 s with twelve.
    And a late order's cancel that Alpaca does not answer held the
    backstop's thread four seconds a try.
    -> ONE list request a pass for all of them, matched on the
       client id; an order's own look-up beside it only before it
       is let go, when the page was full, and now and then as a
       check. The late orders are dealt with on a thread of their
       own. The watch stands back before the backstop does when
       the minute is short, and flatten_all's own wait asks the
       same way.
    Also: a full read of the backstop that straddled a fill ended the
    order from its status and left its fill to the next full read,
    15 s on.
    -> the ledger is read again at the next pass.

  - close A (5 of 10) answered 504 and never carried out; close B
    (5 of 10, another client id) went out under A's id (one closing
    order a symbol) and was carried out; A retried was answered ok
    from B's order: ten counted closed, five were.
    -> whose request made the order under an id that more than one
       call used is written down. A retry of a close whose own
       request did not make it is told so, finally, naming the call
       that was served; when it cannot be known, that is said,
       retryably. Never ok by guess.

  - the typed error for a late order this adapter cancelled was
    withheld when the cancel's answer was lost and after a restart
    (the mark was set on Alpaca's answer alone, in memory);
    -> the cancel is written down when it is ASKED for.
  - a 200 whose body named no order was taken for an order with the
    id "None";
    -> an answer without an id is no answer: the client id is
       looked up.
  - positions.list showed 0.0 open P&L for a row without one.
    -> worked out from the row's value and cost as account.get
       does; left out when it cannot be.

  - flatten_all read positions and open orders side by side: a fill
    between the two answers, of an order it knew, was in neither;
    -> open orders first, positions after them, in every pass.
  - an earlier request of unknown fate was forgotten when a later
    one under its id was refused (but for closes);
    -> it stays asked for, whatever kind of order.
  - "Alpaca:" stood before this adapter's own pace refusal.
    -> it says whose refusal it is.

  A check: an adapter killed while the POST of a STOP of its own is on
    the wire starts again with that order as one of unknown fate. When
    it turns up it is the position's stop, not a late order of the
    user's; and flatten_all after such a restart treats it as any order
    of unknown fate.
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brueconnect.adapter import AdapterError
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from tests import test_alpaca_faults as faults
from tests import test_alpaca_orders as orders
from tests import test_alpaca_changed_meanwhile as changed_meanwhile
from tests import test_alpaca_late_watch as late_watch
from tests import test_alpaca_closing_orders as closing_orders

until = changed_meanwhile.until
Gate = closing_orders.Gate


def cancel_all(method, path, q, data):
    return method == "DELETE" and path == "/v2/orders"


class LateCancelAll(closing_orders.ClosingOrders):
    """MSFT 5 held; flatten_all; its cancel-all is not answered (here: 504
    at once) and the adapter asks again, which works: flatten_all answers
    {"closed": 1}. Then the user trades on, and the first cancel-all is
    carried out."""

    def flattened(self, argv=None):
        r = self.start(argv=argv, syms=(("MSFT", 5),))
        gate = Gate(r)
        slot = gate.hold(cancel_all)
        m = r.mark()
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("F")}),
                         ("ok", {"closed": 1}))
        # (the first cancel-all was answered 504 and NOT carried out)
        self.assertIsNotNone(slot["args"])
        self.assertFalse(slot["landed"])
        return r, gate, slot, m

    def hold_price(self, r, sym):
        px = r.fake.state()["prices"][sym]
        r.fake.set_price(sym, price=px)
        return px

    def bracket(self, r, sym="AAPL", qty=10):
        px = self.hold_price(r, sym)
        sl, tp = round(px - 30, 2), round(px + 30, 2)
        r.buy(sym, qty, sl=sl, tp=tp)
        self.assertTrue(until(lambda: r.shown(sym) == (float(qty), sl, tp),
                              6), r.shown(sym))
        return px, sl, tp

    def rest(self, r, sym, side, qty, price):
        cid = r.cid("U")
        self.assertEqual(r.req("order.place", {
            "client_id": cid, "symbol": sym, "side": side, "qty": qty,
            "type": "limit", "price": price})[0], "ok")
        self.assertTrue(until(lambda: [o for o in r.live(sym)
                                       if o["coid"] == cid], 4))
        return cid

    def exits(self, r, sym="AAPL"):
        return sorted((o["type"], o["side"], o["qty"],
                       o["stop"] or o["limit"]) for o in r.live(sym)
                      if o["cls"] == "oco"
                      or o["coid"].startswith(("brue-sl-", "brue-tp-")))

    def test_the_stop_and_target_of_a_position_opened_after_it_are_put_back(self):
        r, gate, slot, m = self.flattened()
        px, sl, tp = self.bracket(r)
        first = {o["id"] for o in r.live("AAPL")}
        spx = self.hold_price(r, "SPY")
        spy = self.rest(r, "SPY", "buy", 3, round(spx - 40, 2))
        m2 = r.mark()
        gate.land(slot, settle=False)        # ... and NOW it is carried out
        self.assertTrue(until(lambda: not (first & {
            o["id"] for o in r.live("AAPL")}), 4), r.live("AAPL"))
        # the stop and the target the user set are back at Alpaca, as new
        # orders at the same levels, and shown again
        want = [("limit", "sell", 10.0, tp), ("stop", "sell", 10.0, sl)]
        self.assertTrue(until(lambda: self.exits(r) == want, 10),
                        (self.exits(r), r.events(m2, "session.state")))
        self.assertTrue(self.settled(r), self.last)
        self.assertEqual(r.shown("AAPL"), (10.0, sl, tp))
        self.assertEqual(r.held("AAPL"), 10.0)
        # ... and it was SAID, with the symbol and whose doing it was
        said = self.notices(r, m2, "PUT BACK")
        self.assertTrue(said, r.events(m2, "session.state"))
        for word in ("AAPL", "stop at", "target at", "flatten_all's "
                     "cancel-all of 20", "carried out late"):
            self.assertIn(word, said[-1]["detail"])
        self.assertEqual(self.notices(r, m2, "removed outside"), [])
        self.assertEqual(self.notices(r, m2, "NOT put back"), [])
        # The user's resting order went too. It is named, with why, and
        # this adapter has placed nothing in its stead.
        told = until(lambda: self.notices(r, m2, "NOT placed again"), 4)
        self.assertTrue(told, r.events(m2, "session.state"))
        for word in ("limit buy 3 SPY", "client id " + spy,
                     "carried out late"):
            self.assertIn(word, told[-1]["detail"])
        self.assertEqual(self.sent(r, "SPY"), [spy])
        self.assertEqual(self.made(r, spy)[0].status, "canceled")
        gone = [d for d in r.events(m2, "order.cancelled")
                if d["client_id"] == spy]
        self.assertEqual(len(gone), 1, r.events(m2))
        self.assertEqual(gone[0]["code"], "x_alpaca_canceled")
        self.assertIn("carried out late", gone[0]["message"])

    def test_on_a_live_account_the_users_orders_are_named_and_not_placed_again(self):
        r, gate, slot, m = self.flattened(argv=["--live"])
        px = self.hold_price(r, "AAPL")
        r.buy("AAPL", 10)
        up = self.rest(r, "AAPL", "sell", 10, round(px + 60, 2))
        spx = self.hold_price(r, "SPY")
        spy = self.rest(r, "SPY", "buy", 3, round(spx - 40, 2))
        m2 = r.mark()
        gate.land(slot, settle=False)
        self.assertTrue(until(lambda: not r.live(), 4), r.live())
        told = until(lambda: [d for d in self.notices(
            r, m2, "NOT placed again") if spy in d["detail"]
            and up in d["detail"]], 5)
        self.assertTrue(told, r.events(m2, "session.state"))
        text = told[-1]["detail"]
        self.assertIn("2 order(s) of yours", text)
        self.assertIn("limit sell 10 AAPL (client id %s" % up, text)
        self.assertIn("limit buy 3 SPY (client id %s" % spy, text)
        self.assertIn("flatten_all's cancel-all of 20", text)
        time.sleep(2.5)
        # nothing was placed for them, and nothing against the position
        self.assertEqual(r.live(), [])
        self.assertEqual(len(self.sent(r, "AAPL")), 2)   # the buy, the limit
        self.assertEqual(self.sent(r, "SPY"), [spy])
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(sorted(d["client_id"] for d in r.events(
            m2, "order.cancelled")), sorted([up, spy]))

    def test_a_target_the_market_went_through_meanwhile_is_not_put_back(self):
        # Put back as a level of an earlier moment is: the stop always,
        # the target only if it would not fill at once.
        r, gate, slot, m = self.flattened()
        px, sl, tp = self.bracket(r)
        m2 = r.mark()
        gate.land(slot, settle=False)
        self.assertTrue(until(lambda: not r.live("AAPL"), 4), r.live("AAPL"))
        r.fake.set_price("AAPL", price=round(tp + 5, 2))
        self.assertTrue(until(lambda: self.exits(r) == [
            ("stop", "sell", 10.0, sl)], 10), self.exits(r))
        said = until(lambda: self.notices(r, m2, "NOT put back"), 4)
        self.assertTrue(said, r.events(m2, "session.state"))
        self.assertIn("target at", said[-1]["detail"])
        self.assertIn("carried out late, cancelled it", said[-1]["detail"])
        self.assertNotIn("when this adapter last ran", said[-1]["detail"])
        self.assertEqual(r.held("AAPL"), 10.0)           # nothing was sold
        self.assertEqual(r.shown("AAPL"), (10.0, sl, None))

    def test_it_holds_after_a_restart(self):
        r, gate, slot, m = self.flattened()
        r.restart()
        self.assertEqual(len(getattr(r.a, "_swept", [])), 1)
        px, sl, tp = self.bracket(r)
        m2 = r.mark()
        gate.land(slot, settle=False)
        want = [("limit", "sell", 10.0, tp), ("stop", "sell", 10.0, sl)]
        self.assertTrue(until(lambda: not r.live("AAPL"), 4), r.live("AAPL"))
        self.assertTrue(until(lambda: self.exits(r) == want, 10),
                        (self.exits(r), r.events(m2, "session.state")))
        self.assertTrue(self.notices(r, m2, "PUT BACK"))
        self.assertEqual(self.notices(r, m2, "removed outside"), [])

    def test_outside_the_ten_minutes_the_old_rule_stands(self):
        # (a guard) The same story, but the unanswered cancel-all is more
        # than ten minutes old when the exits go: whoever took them off,
        # it was not that request. Dropped, said once, NOT put back.
        r, gate, slot, m = self.flattened()
        px, sl, tp = self.bracket(r)
        with r.a._state_lock:
            r.a._swept = [(at - 601000, mono - 601.0)
                          for at, mono in getattr(r.a, "_swept", [])]
        m2 = r.mark()
        gate.land(slot, settle=False)
        said = until(lambda: self.notices(r, m2, "removed outside"), 8)
        self.assertTrue(said, r.events(m2, "session.state"))
        self.assertIn("NOT put back", said[-1]["detail"])
        time.sleep(2.0)
        self.assertEqual(self.exits(r), [])
        self.assertEqual(self.notices(r, m2, "PUT BACK"), [])
        self.assertEqual(r.shown("AAPL"), (10.0, None, None))

    def test_a_cancel_all_that_was_answered_is_not_remembered(self):
        # (a guard) Answered, the cancel-all is over: nothing is
        # remembered, and an exit cancelled from outside afterwards is
        # dropped and said, as ever.
        r = self.rig()
        r.buy("MSFT", 5)
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("F")}),
                         ("ok", {"closed": 1}))
        self.assertEqual(getattr(r.a, "_swept", []), [])
        # and so a stop cancelled from outside afterwards stays off
        r.long_with_stop("AAPL", 10, 300.0)
        m = r.mark()
        r.other("DELETE", "/v2/orders/" + r.live("AAPL")[0]["id"])
        self.assertTrue(until(lambda: self.notices(r, m, "removed outside"),
                              8), r.events(m, "session.state"))
        self.assertEqual(self.notices(r, m, "PUT BACK"), [])


class WhatIsRemembered(unittest.TestCase):
    """Which cancel-alls count, and that they are written down (a
    scripted broker: tests/test_alpaca_orders.py)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="alpaca-r6b-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for p in (patch.object(mod.time, "sleep", lambda s: None),
                  patch.object(mod.sys, "stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def file(self):
        with open(os.path.join(self.dir, "alpaca_state.json")) as fh:
            return json.load(fh)

    def test_only_a_request_that_ended_without_an_answer(self):
        a, b = orders.make(self.dir)
        E = mod.AlpacaError
        for e, counts in (
                (E(0, "timed out"), True),
                (E(504, "upstream request timeout"), True),
                (E(500, "internal server error occurred"), True),
                (E(408, "request timeout"), True),
                # never sent: this adapter's own refusals
                (E(0, "the time for this call is used up", code="deadline"),
                 False),
                (E(429, "more urgent ones went first", code="budget"), False),
                (E(0, "flatten_all was asked for", code="flatten"), False),
                # Alpaca's own answer: it was not done, and it is over
                (E(429, "too many requests"), False),
                (E(403, "forbidden."), False),
                (E(422, "unprocessable"), False)):
            del a._swept[:]
            a._sweep_unsaid(e)
            self.assertEqual(len(a._swept), 1 if counts else 0, e.args)
            self.assertEqual(a._swept_at() is not None, counts, e.args)

    def test_it_is_in_the_state_file_and_a_start_takes_back_the_young(self):
        a, b = orders.make(self.dir)
        now, mono = a.now_ms(), time.monotonic()
        a._sweep_unsaid(mod.AlpacaError(504, "upstream request timeout"))
        self.assertEqual(len(self.file()["cancel_all"]), 1)   # at once
        a._swept.insert(0, (now - 700000, mono - 700.0))      # too old
        a._swept.insert(0, (now - 50000, mono - 50.0))
        a._save_state()
        n, _ = orders.make(self.dir)                          # the next run
        self.assertEqual(len(n._swept), 2)
        ats = [at for at, _ in n._swept]
        self.assertAlmostEqual(ats[0], now - 50000, delta=5)
        self.assertAlmostEqual(time.monotonic() - n._swept[0][1], 50.0,
                               delta=1.5)
        # one sent AFTER an exit ended does not explain that ending
        self.assertIsNone(n._swept_at(before=time.monotonic() - 60.0))
        self.assertAlmostEqual(n._swept_at(before=time.monotonic() - 10.0),
                               now - 50000, delta=5)
        # ... and another account takes none of it
        n._state_account = "PA1"
        n._bind_account("PA2")
        self.assertEqual(n._swept, [])


class OneRequestAPass(unittest.TestCase):
    """What one pass of the watch asks of Alpaca (a scripted broker: every
    request is counted)."""

    def setUp(self):
        for p in (patch.object(mod.time, "sleep", lambda s: None),
                  patch.object(mod.sys, "stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def watched(self, n, age=45.0, **more):
        a, b = orders.make()
        now = time.monotonic()
        for i in range(n):
            a._unknown["P%d" % i] = dict({
                "kind": "user", "cid": "P%d" % i, "t0": now - age - i,
                "next": now - 0.1, "n": 0, "flat": True, "watch": True,
                "side": "buy", "qty": "10", "sym": "AAPL"}, **more)
        return a, b

    @staticmethod
    def asked(b):
        """(list requests, look-ups by client id) made so far."""
        return (len([c for c in b.sent("GET", "/v2/orders")
                     if (c[2] or {}).get("status") == "all"]),
                len(b.sent("GET", "/v2/orders:by_client_order_id")))

    def test_twelve_orders_on_the_watch_cost_one_request_a_pass(self):
        a, b = self.watched(12)
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (1, 0))      # it was (0, 12)
        q = b.sent("GET", "/v2/orders")[0][2]
        self.assertEqual((q["status"], q["direction"], q["limit"],
                          q["nested"]), ("all", "desc", 500, "false"))
        # the list reaches back to before the oldest of them was sent
        oldest = a.now_ms() - (45.0 + 11) * 1000
        self.assertLess(mod.parse_ts(q["after"]), oldest - 60000)
        self.assertGreater(mod.parse_ts(q["after"]), oldest - 300000)
        # all still watched, each asked for again in ten seconds
        self.assertEqual(len(a._unknown), 12)
        now = time.monotonic()
        self.assertTrue(all(9.0 < u["next"] - now <= 10.0
                            for u in a._unknown.values()))
        self.assertEqual([n for n, _ in a.events if n != "session.state"],
                         [])

    def test_the_one_the_list_shows_is_taken_up_and_the_others_stay(self):
        a, b = self.watched(3, age=5.0, flat=False, watch=False)
        b.on("GET", "/v2/orders", lambda q, body, path: [
            orders.order("o-x", "somebody-else", sym="AAPL", qty="1"),
            orders.order("o1", "P1", sym="AAPL", qty="10", typ="limit",
                         limit_price="200")]
            if (q or {}).get("status") == "all" else [])
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (1, 0))
        self.assertEqual(sorted(a._unknown), ["P0", "P2"])
        self.assertEqual([(n, d["client_id"]) for n, d in a.events
                          if n.startswith("order.")],
                         [("order.accepted", "P1"), ("order.placed", "P1")])

    def test_one_order_alone_is_still_asked_for_by_its_own_id(self):
        # (a guard) one request either way, and the exact one: the list
        # does not replace it
        a, b = self.watched(1)
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (0, 1))

    def test_one_that_is_due_brings_the_others_along(self):
        # Three orders sent half a second apart came due in two groups,
        # and the one alone was asked for by its own id every pass.
        a, b = self.watched(3, age=5.0, watch=False)
        now = time.monotonic()
        a._unknown["P0"]["next"] = now - 0.1
        a._unknown["P1"]["next"] = now + 0.7
        a._unknown["P2"]["next"] = now + 1.4
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (1, 0))
        nexts = sorted(u["next"] for u in a._unknown.values())
        self.assertLess(nexts[-1] - nexts[0], 0.05)      # in step now
        self.assertGreater(nexts[0], now + 1.5)

    def test_none_is_let_go_on_the_list_s_word_alone(self):
        # Five are at the end of their ten minutes. The list does not
        # show them; each is asked for by its own id before it is let
        # go, three a pass. One of them is there after all.
        a, b = self.watched(5, age=mod.LATE_WATCH + 1.0)
        b.on("GET", "/v2/orders:by_client_order_id",
             lambda q, body, path: orders.order(
                 "o4", "P4", sym="AAPL", qty="10", typ="limit",
                 limit_price="200")
             if q["client_order_id"] == "P4" else mod.AlpacaError(
                 404, "order not found for it", 40410000))
        a._late_busy = True          # (what is due of P4 is not this test's)
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (1, 3))
        self.assertEqual(len(a._dropped) + len(a._late_due), 3)
        self.assertEqual(len(a._unknown), 2)
        for u in a._unknown.values():
            u["next"] = 0.0
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (2, 5))
        self.assertEqual(a._unknown, {})
        self.assertEqual(sorted(a._dropped), ["P0", "P1", "P2", "P3"])
        self.assertEqual(sorted(a._late_due), ["P4"])    # found: a late one
        said = [d["detail"] for n, d in a.events if n == "session.state"]
        self.assertTrue(said and "STOPPED asking" in said[-1], said)

    def test_a_full_page_or_an_old_check_is_one_own_look_up_a_pass(self):
        a, b = self.watched(4)
        # (a) a check on the list: two of them were last asked for by
        # their own id more than OWN_SEC ago; one a pass, the staler first
        now = time.monotonic()
        a._unknown["P1"]["own"] = now - mod.OWN_SEC - 5.0
        a._unknown["P2"]["own"] = now - mod.OWN_SEC - 50.0
        for name in ("P0", "P3"):
            a._unknown[name]["own"] = now - 1.0
        a._resolve_unknown()
        self.assertEqual(self.asked(b), (1, 1))
        self.assertEqual(b.sent("GET", "/v2/orders:by_client_order_id")
                         [-1][2]["client_order_id"], "P2")
        # (b) the page is full: it does not reach back to all of them
        page = [orders.order("f%d" % i, "other-%d" % i, sym="AAPL")
                for i in range(mod.WATCH_PAGE)]
        b.on("GET", "/v2/orders", lambda q, body, path: page
             if (q or {}).get("status") == "all" else [])
        for turn, name in ((2, "P1"), (3, "P0"), (4, "P3")):
            for u in a._unknown.values():
                u["next"] = 0.0
            a._resolve_unknown()
            self.assertEqual(self.asked(b), (turn, turn))
            self.assertEqual(b.sent("GET", "/v2/orders:by_client_order_id")
                             [-1][2]["client_order_id"], name)

    def test_the_watch_stands_back_before_the_backstop_does(self):
        # An order every six seconds with the stream down is 103 requests
        # a minute with nothing on watch: whatever the watch asks then is
        # taken from the reads that hear fills.
        b, now = mod._Budget(), time.monotonic()

        def used(n):
            b._minute["trading"] = mod.collections.deque([now - 5.0] * n)
            b._second["trading"] = mod.collections.deque()
        room = mod.BUDGET_BACKGROUND - mod.BUDGET_WATCH_ROOM
        used(room)
        with self.assertRaises(mod.AlpacaError) as ctx:
            b.take("trading", "watch", max_wait=0.0)
        self.assertEqual(ctx.exception.code, "budget")
        b.take("trading", "background", max_wait=0.0)    # the backstop may
        used(room - 1)
        b.take("trading", "watch", max_wait=0.0)         # ... and now it may
        # the list is asked for that way, and so is the look-up of ONE
        # order on the long watch; one in its first half minute, and one
        # about to be let go, are asked for whatever the minute holds
        kinds = []
        for more in ({}, {"watch": False, "age": 5.0},
                     {"age": mod.LATE_WATCH + 1.0}):
            a, br = self.watched(1, **dict({"age": 45.0}, **more))
            inner = br.call

            def call(method, host, path, query=None, body=None, **kw):
                kinds.append((path, kw.get("kind")))
                return inner(method, host, path, query, body, **kw)
            a.client.call = call
            a._resolve_unknown()
        self.assertEqual([k for _, k in kinds], ["watch", "write", "write"])
        a, br = self.watched(3)
        inner = br.call
        a.client.call = lambda method, host, path, query=None, body=None, \
            **kw: (kinds.append((path, kw.get("kind"))),
                   inner(method, host, path, query, body, **kw))[1]
        a._resolve_unknown()
        self.assertEqual(kinds[-1], ("/v2/orders", "watch"))

    def test_a_list_that_cannot_be_read_is_asked_again_and_nothing_is_decided(self):
        a, b = self.watched(3, age=mod.LATE_WATCH + 1.0)
        b.on("GET", "/v2/orders", mod.AlpacaError(0, "timed out"))
        a._resolve_unknown()
        self.assertEqual(len(b.sent("GET", "/v2/orders:by_client_order_id")),
                         0)
        self.assertEqual((len(a._unknown), a._dropped), (3, {}))
        now = time.monotonic()
        self.assertTrue(all(u["next"] > now for u in a._unknown.values()))


class AFillBetweenTwoReads(unittest.TestCase):
    """With the stream down, a full read of the backstop that straddles a
    fill (the ledger read before it, the order read after it) marked the
    order ended from its status. It was then no longer "in flight", the
    once-a-second look for its fill stopped, and order.filled waited for
    the next full read, 15 s later: the 16 and 17 s among the 2.3 s."""

    def setUp(self):
        for p in (patch.object(mod.time, "sleep", lambda s: None),
                  patch.object(mod.sys, "stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def flying(self):
        a, b = orders.make()
        a._stream_up.clear()
        orders.ours(a, "cid-1")
        o = orders.order("o1", "cid-1", status="accepted")
        with a._state_lock:
            a._on_status(a._track(o), o)
        filled = dict(o, status="filled", filled_qty="0.0002",
                      updated_at="2026-11-10T13:02:32Z")
        b.on("GET", "/v2/orders/*", filled)       # no longer open: filled
        return a, b, filled

    def test_the_ledger_is_read_again_at_once(self):
        a, b, filled = self.flying()
        b.on("GET", "/v2/account/activities/FILL", [])    # read BEFORE it
        a._sync()
        self.assertTrue(a._orders["o1"]["done"])
        self.assertEqual([n for n, _ in a.events if n == "order.filled"],
                         [])
        self.assertTrue(a._resync)               # it was False: 15 s late
        # the next pass is a full read, and the fill is told
        a._last_full = time.monotonic()
        b.on("GET", "/v2/account/activities/FILL", [orders.activity(
            "x1", "o1", "buy", "0.0002", "74217.02")])
        b.on("GET", "/v2/positions", [orders.position()])
        a.now_ms = lambda: 1794315752000
        a._session_start_ms = 0
        a._poll_once()
        self.assertEqual([d["client_id"] for n, d in a.events
                          if n == "order.filled"], ["cid-1"])
        self.assertFalse(a._resync)

    def test_a_pass_that_has_the_fill_asks_for_nothing_more(self):
        # (a guard: the ordinary case must not cost a second read)
        a, b, filled = self.flying()
        a._session_start_ms = 0
        b.on("GET", "/v2/account/activities/FILL", [orders.activity(
            "x1", "o1", "buy", "0.0002", "74217.02")])
        b.on("GET", "/v2/positions", [orders.position()])
        a._sync()
        self.assertEqual([d["client_id"] for n, d in a.events
                          if n == "order.filled"], ["cid-1"])
        self.assertFalse(a._resync)


class TheWatchAtWork(closing_orders.ClosingOrders):
    """The watch against the stand-in: several orders answered 504 and
    never carried out, flatten_all told of them (scaled: orders are on
    the watch after 2 s)."""

    QUICK = dict(changed_meanwhile.SentAndSaid.QUICK, UNKNOWN_SEC=2.0)

    def unknown(self, r, n):
        gate = Gate(r)
        slots, cids = [], []
        for i in range(n):
            cid = r.cid("P%d-" % i)
            slots.append(gate.hold(
                lambda method, path, q, data, cid=cid: method == "POST"
                and path == "/v2/orders" and cid.encode() in (data or b"")))
            kind, out = r.req("order.place", {
                "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10,
                "type": "limit", "price": 200.0 - i})
            self.assertEqual((kind, out.get("retryable")), ("err", True), out)
            cids.append(cid)
        # flatten_all is told of them (it holds its answer back while
        # they are younger than UNKNOWN_SEC, two seconds here)
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertTrue(kind == "ok" or out.get("retryable"), out)
        self.assertTrue(until(lambda: len(r.a._unknown) == n and all(
            u.get("watch") for u in r.a._unknown.values()), 8), r.a._unknown)
        return gate, slots, cids

    def test_six_on_the_watch_one_lands_late_and_is_found_by_the_list(self):
        r = self.rig()
        gate, slots, cids = self.unknown(r, 6)
        r.fake.requests(clear=True)
        m = r.mark()
        time.sleep(11.0)                     # a pass of the watch, or two
        ids = r.log("GET", "/v2/orders:by_client_order_id")
        lists = [q for q in r.log("GET", "/v2/orders")
                 if "status=all" in str(q["query"])]
        self.assertLessEqual(len(ids), 1, ids)       # it was six a pass
        self.assertTrue(1 <= len(lists) <= 3, lists)
        # the fourth of them reaches Alpaca now, and rests there
        gate.land(slots[3], settle=False)
        said = until(lambda: self.notices(r, m, "is cancelled"), 14)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn(cids[3], said[-1]["detail"])
        self.assertEqual(self.made(r, cids[3])[0].status, "canceled")
        self.assertEqual(sorted(r.a._unknown), sorted(
            c for c in cids if c != cids[3]))
        self.assertEqual(self.sent(r, "AAPL"), [cids[3]])
        self.assertLessEqual(len(r.log(
            "GET", "/v2/orders:by_client_order_id")), 3)

    def test_flatten_all_asks_for_all_of_them_with_one_request_a_pass(self):
        # flatten_all holds its answer back while orders sent before it
        # are of unknown fate, and asked for each by its id in every pass
        # of its loop. One of the four arrives while it waits.
        for name, value in changed_meanwhile.LateOrder.QUICK.items():
            p = patch.object(mod, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(mod, "UNKNOWN_SEC", 30.0)
        p.start()
        self.addCleanup(p.stop)
        r = self.rig()
        r.buy("MSFT", 5)
        gate = Gate(r)
        slots, cids = [], []
        for i in range(4):
            cid = r.cid("P%d-" % i)
            slots.append(gate.hold(
                lambda method, path, q, data, cid=cid: method == "POST"
                and path == "/v2/orders" and cid.encode() in (data or b"")))
            kind, out = r.req("order.place", {
                "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10,
                "type": "limit", "price": 200.0 - i})
            self.assertEqual((kind, out.get("retryable")), ("err", True), out)
            cids.append(cid)
        with r.a._state_lock:
            # (the backstop's own asking is kept out of the count)
            for u in r.a._unknown.values():
                u["next"] = time.monotonic() + 60.0
        r.fake.requests(clear=True)
        threading.Timer(1.2, gate.land, (slots[2], False)).start()
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("may still arrive", out["message"])
        ids = r.log("GET", "/v2/orders:by_client_order_id")
        self.assertEqual(ids, [])                # it was four a pass
        self.assertTrue([q for q in r.log("GET", "/v2/orders")
                         if "status=all" in str(q["query"])])
        # the one that arrived was found, and is cancelled by flatten_all
        # itself; the answer names the three that have not arrived
        self.assertTrue(until(
            lambda: self.made(r, cids[2])[0].status == "canceled", 4))
        self.assertNotIn(cids[2], out["message"])
        for i in (0, 1, 3):
            self.assertIn(cids[i], out["message"])
        self.assertEqual(r.held("MSFT"), 0.0)

    def test_a_cancel_that_alpaca_does_not_answer_does_not_hold_the_backstop(self):
        # One late order rests, and Alpaca sits on every DELETE of it for
        # longer than the four seconds the adapter gives a request. The
        # backstop (with the stream down: the thread that hears fills)
        # used to stand still for each of those four seconds.
        r = self.rig()
        gate, slots, cids = self.unknown(r, 1)
        r.rules.add("DELETE", "/v2/orders/", before=5.0, count=10 ** 6,
                    name="stalled DELETE")
        spans, inner = [], r.a._poll_once

        def timed():
            t = time.monotonic()
            try:
                return inner()
            finally:
                spans.append(time.monotonic() - t)
        r.a._poll_once = timed
        m = r.mark()
        gate.land(slots[0], settle=False)
        self.assertTrue(until(lambda: len(r.rules.hit("stalled DELETE")) >= 2,
                              20), r.rules.hits)
        time.sleep(1.0)
        self.assertLess(max(spans), 2.5, sorted(spans)[-3:])
        self.assertGreater(len(spans), 6, spans)
        # ... and what is said of the order is still said
        self.assertTrue(until(lambda: self.notices(
            r, m, "could not be read or cancelled"), 6),
            r.events(m, "session.state"))


class TwoCloses(closing_orders.ClosingOrders):
    """Two closes of part of one position: MSFT 5 and AAPL 10 held."""

    def close(self, r, cid, qty=None):
        p = {"client_id": cid, "position_id": "AAPL"}
        if qty is not None:
            p["qty"] = qty
        return r.req("position.close", p)

    def a_never_b_carried_out(self, argv=None):
        r = self.start(argv=argv)
        gate = Gate(r)
        gate.hold(Gate.order("AAPL"))        # A: 504, never carried out
        x1, x2 = r.cid("X1"), r.cid("X2")
        kind, out = self.close(r, x1, 5)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("HTTP 504", out["message"])
        self.assertEqual(self.close(r, x2, 5), ("ok", {}))       # B
        self.assertTrue(until(lambda: r.held("AAPL") == 5.0, 4))
        return r, x1, x2

    def not_carried_out(self, r, x1, x2):
        kind, out = self.close(r, x1, 5)
        self.assertEqual(kind, "err", out)
        self.assertEqual((out["code"], out["retryable"]),
                         ("x_alpaca_close_not_carried_out", False), out)
        for word in ("NOT carried out", "position.close with client_id "
                     + x2, "NEW client_id", "sell 5 AAPL"):
            self.assertIn(word, out["message"])
        return out

    def test_the_first_retried_is_told_it_was_not_carried_out(self):
        r, x1, x2 = self.a_never_b_carried_out()
        m = r.mark()
        self.not_carried_out(r, x1, x2)
        # five were closed, by ONE order, and B is the call it served
        self.assertEqual(r.held("AAPL"), 5.0)
        self.assertEqual([f for f in self.filled(r) if f[1] == "sell"],
                         [(x1, "sell", 5.0)])
        self.assertEqual(self.close(r, x2, 5), ("ok", {}))   # B, asked again
        self.not_carried_out(r, x1, x2)                      # every time
        self.assertEqual(r.held("AAPL"), 5.0)
        self.assertEqual(r.events(m, "order.rejected"), [])
        # ... and under a new id, as it says, the other five are closed
        self.assertEqual(self.close(r, r.cid("X3"), 5), ("ok", {}))
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 4))

    def test_it_holds_after_a_restart(self):
        r, x1, x2 = self.a_never_b_carried_out(argv=["--live"])
        r.restart()
        self.not_carried_out(r, x1, x2)
        self.assertEqual(r.held("AAPL"), 5.0)

    def both_unanswered(self, qty_a, qty_b):
        """A: 504, never carried out. B: carried out (under A's id), but
        its answer is lost and so is every look-up for a while."""
        r = self.start()
        gate = Gate(r)
        gate.hold(Gate.order("AAPL"))
        x1, x2 = r.cid("X1"), r.cid("X2")
        kind, out = self.close(r, x1, qty_a)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        r.rules.add("POST", "/v2/orders", body='"AAPL"', drop=True, count=1,
                    name="B answer lost")
        lost = r.rules.add("GET", "/v2/orders:by_client_order_id", fail=500,
                           count=10 ** 6, name="look-ups 500")
        kind, out = self.close(r, x2, qty_b)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertNotEqual(out["code"], "x_alpaca_close_pending", out)
        lost["count"] = 0
        self.assertTrue(until(lambda: r.held("AAPL") == 10.0 - qty_b, 4))
        return r, x1, x2

    def test_when_neither_was_answered_ok_is_not_guessed(self):
        r, x1, x2 = self.both_unanswered(5, 5)
        kind, out = self.close(r, x1, 5)
        self.assertEqual(kind, "err", out)
        self.assertEqual((out["code"], out["retryable"]),
                         ("x_alpaca_close_not_known", True), out)
        for word in ("NOT known", "ONE order", "position.close with "
                     "client_id " + x2, "NEW client_id"):
            self.assertIn(word, out["message"])
        self.assertEqual(r.held("AAPL"), 5.0)
        self.assertEqual([f for f in self.filled(r) if f[1] == "sell"],
                         [(x1, "sell", 5.0)])

    def test_the_quantity_tells_whose_request_it_was(self):
        # A asked for 3, B for 5, neither answered; the order under A's
        # id is for 5: B's request made it.
        r, x1, x2 = self.both_unanswered(3, 5)
        kind, out = self.close(r, x1, 3)
        self.assertEqual(kind, "err", out)
        self.assertEqual((out["code"], out["retryable"]),
                         ("x_alpaca_close_not_carried_out", False), out)
        self.assertIn("position.close with client_id " + x2, out["message"])
        self.assertEqual(r.held("AAPL"), 5.0)

    def test_a_close_answered_from_its_own_order_is_still_ok(self):
        # (a guard) The answer to A's own request is lost; no other call
        # used its id. Retried: the original outcome, nothing sent twice.
        r = self.start()
        r.rules.add("POST", "/v2/orders", body='"AAPL"', drop=True, count=1,
                    name="A answer lost")
        lost = r.rules.add("GET", "/v2/orders:by_client_order_id", fail=500,
                           count=10 ** 6, name="look-ups 500")
        x1 = r.cid("X1")
        kind, out = self.close(r, x1, 5)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        lost["count"] = 0
        self.assertEqual(self.close(r, x1, 5), ("ok", {}))
        self.assertTrue(until(lambda: r.held("AAPL") == 5.0, 4))
        self.assertEqual([f for f in self.filled(r) if f[1] == "sell"],
                         [(x1, "sell", 5.0)])


class WhoseClose(unittest.TestCase):
    """The rule itself, case by case (a scripted broker)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="alpaca-r6b-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        p = patch.object(mod.sys, "stderr", io.StringIO())
        p.start()
        self.addCleanup(p.stop)

    def judge(self, served, qty="5"):
        a, b = orders.make(self.dir)
        if served is not None:
            a._served["X1"] = served
        o = orders.order("o1", "X1", sym="AAPL", side="sell", qty=qty,
                         status="filled", filled=qty)
        try:
            a._whose_close(o, "X1")
        except AdapterError as e:
            return e.code, e.retryable, str(e)
        return "ok"

    def test_case_by_case(self):
        self.assertEqual(self.judge(None), "ok")
        self.assertEqual(self.judge(
            {"askers": {"X1": "5"}, "maker": None}), "ok")
        self.assertEqual(self.judge(
            {"askers": {"X1": "5", "X2": "5"}, "maker": "X1"}), "ok")
        out = self.judge({"askers": {"X1": "5", "X2": "5"}, "maker": "X2"})
        self.assertEqual(out[:2], ("x_alpaca_close_not_carried_out", False))
        out = self.judge({"askers": {"X1": "5", "X2": "5"}, "maker": None})
        self.assertEqual(out[:2], ("x_alpaca_close_not_known", True))
        # not answered to anyone, but only one of them asked for this much
        self.assertEqual(self.judge(
            {"askers": {"X1": "5", "X2": "3"}, "maker": None}), "ok")
        out = self.judge({"askers": {"X1": "3", "X2": "5"}, "maker": None})
        self.assertEqual(out[:2], ("x_alpaca_close_not_carried_out", False))
        # flatten_all's request made it (its wish covers the close's,
        # nothing is counted twice): answered as the close's, as ever
        self.assertEqual(self.judge(
            {"askers": {"X1": "5", "flatten_all:F7": "10"},
             "maker": "flatten_all:F7"}, qty="10"), "ok")
        self.assertEqual(self.judge(
            {"askers": {"X1": "5", "flatten_all:F7": "10"},
             "maker": None}, qty="10"), "ok")
        self.assertEqual(self.judge(
            {"askers": {"X1": "5", "X2": "5", "flatten_all:F7": "10"},
             "maker": "flatten_all:F7"}, qty="10"), "ok")
        # two other closes asked for this much, this one for another
        out = self.judge({"askers": {"X1": "3", "X2": "5", "X3": "5"},
                          "maker": None})
        self.assertEqual(out[:2], ("x_alpaca_close_not_carried_out", False))
        self.assertIn("client_id X2 or position.close with client_id X3",
                      out[2])

    def test_what_served_whom_is_written_before_the_request_leaves(self):
        a, b = orders.make(self.dir)
        seen = []

        def post(q, body, path):
            with open(os.path.join(self.dir, "alpaca_state.json")) as fh:
                seen.append(json.load(fh).get("served"))
            return orders.order("c1", body["client_order_id"], side="sell",
                                qty=body["qty"], sym="BTC/USD")
        # A (X1, 0.0001) is of unknown fate: its closing entry stands
        a._closing["BTC/USD"] = {"coid": "X1", "flying": 0, "unknown": True,
                                 "kind": "close", "at": a.now_ms(),
                                 "who": "X1", "qty": "0.0001"}
        a._unknown["X1"] = {"kind": "close", "cid": "X1", "flat": False,
                            "t0": time.monotonic(), "n": 0, "side": "sell",
                            "next": time.monotonic() + 60.0,
                            "qty": "0.0001", "sym": "BTC/USD"}
        a._intents["X1"] = {"kind": "close", "cid": "X1"}
        b.on("GET", "/v2/positions/BTCUSD", orders.position())
        b.on("POST", "/v2/orders", post)
        self.assertEqual(a.handle_position_close({
            "position_id": "BTC/USD", "client_id": "X2",
            "qty": 0.00005}), {})
        self.assertEqual(seen, [{"X1": {
            "askers": {"X1": "0.0001", "X2": "0.00005"}, "maker": None}}])
        self.assertEqual(a._served["X1"]["maker"], "X2")
        a._save_state()
        n, nb = orders.make(self.dir)                        # the next run
        self.assertEqual(n._served, {"X1": {
            "askers": {"X1": "0.0001", "X2": "0.00005"}, "maker": "X2"}})
        nb.on("GET", "/v2/orders:by_client_order_id", orders.order(
            "c1", "X1", side="sell", qty="0.00005", status="filled",
            filled="0.00005"))
        with self.assertRaises(AdapterError) as ctx:
            n.handle_position_close({"position_id": "BTC/USD",
                                     "client_id": "X1", "qty": 0.0001})
        self.assertEqual(ctx.exception.code, "x_alpaca_close_not_carried_out")
        self.assertEqual(nb.sent("POST"), [])
        n._state_account = "PA1"                 # ... of another account
        n._bind_account("PA-other")
        self.assertEqual(n._served, {})


class CancelledAfterFlatten(late_watch.Late):
    """x_alpaca_cancelled_after_flatten: a late LIMIT order (client id P)
    rests after flatten_all has answered, and this adapter cancels it. P
    retried under the same id."""

    P = {"symbol": "AAPL", "side": "buy", "qty": 10, "type": "limit",
         "price": 200.0}

    def typed(self, r, cid):
        kind, out = r.req("order.place", dict(self.P, client_id=cid))
        self.assertEqual(kind, "err", out)
        self.assertEqual((out["code"], out["retryable"]),
                         ("x_alpaca_cancelled_after_flatten", False), out)
        self.assertIn("NEW client_id", out["message"])
        self.assertEqual(self.sent(r, "AAPL"), [cid])    # nothing sent

    def test_when_the_answer_to_its_cancel_was_lost(self):
        # The DELETE is carried out, its answer is not seen. The order is
        # cancelled all the same, and by whom.
        r = self.rig()
        r.rules.add("DELETE", "/v2/orders/", drop=True, count=1,
                    name="DELETE answer lost")
        m, cid, placed, flat, t = self.late(r, {"type": "limit",
                                                "price": 200.0})
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: order.status == "canceled", 8),
                        order.status)
        self.assertEqual(len(r.rules.hit("DELETE answer lost")), 1)
        self.typed(r, cid)
        # ... and it is said of it as of any it cancelled
        self.told(r, m, "is cancelled; nothing of it filled", 12)

    def test_after_a_restart(self):
        # The declared case, then the adapter is closed and started again.
        # A mark kept in memory only would be gone.
        r = self.rig()
        m, cid, placed, flat, t = self.late(r, {"type": "limit",
                                                "price": 200.0})
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: order.status == "canceled", 8),
                        order.status)
        self.told(r, m, "is cancelled; nothing of it filled")
        r.restart()
        self.typed(r, cid)

    def test_killed_with_the_cancel_on_the_wire(self):
        # asked for, written down, and the process ends before Alpaca's
        # answer: the next run sees the order cancelled and knows why
        r = self.rig()
        r.rules.add("DELETE", "/v2/orders/", after=3.0, count=1,
                    name="DELETE answered late")
        m, cid, placed, flat, t = self.late(r, {"type": "limit",
                                                "price": 200.0})
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: r.rules.hit("DELETE answered late"),
                              8))
        # (carried out at once; the old process dies waiting for the
        # answer, and the new one finds the order cancelled already)
        self.assertTrue(until(lambda: order.status == "canceled", 2),
                        order.status)
        closing_orders.Restart.kill(self, r)
        self.typed(r, cid)

    def test_one_the_platform_cancelled_itself_is_not_called_that(self):
        # (a guard) Alpaca answers 500 to the adapter's own cancels; the
        # platform cancels the order with order.cancel, which Alpaca
        # takes. That is the user's cancel: asked for again under its id,
        # the order is the original outcome, not "this adapter cancelled
        # it after flatten_all".
        r = self.rig()
        bad = r.rules.add("DELETE", "/v2/orders/", fail=500, count=10 ** 6,
                          name="DELETE 500")
        m, cid, placed, flat, t = self.late(r, {"type": "limit",
                                                "price": 200.0})
        order = self.landed(r, cid)
        self.assertTrue(until(lambda: len(r.rules.hit("DELETE 500")) >= 2,
                              10))
        bad["count"] = 0
        self.assertEqual(r.req("order.cancel", {
            "client_id": r.cid("k"), "order_id": order.id}), ("ok", {}))
        self.assertTrue(until(lambda: order.status == "canceled", 4))
        time.sleep(1.0)
        self.assertEqual(r.req("order.place", dict(self.P, client_id=cid)),
                         ("ok", {"order_id": order.id, "status": "resting"}))
        self.assertEqual(self.sent(r, "AAPL"), [cid])


class NoOrderInTheAnswer(closing_orders.ClosingOrders):
    """`POST /v2/orders` is answered 200 with the body {} (a gateway's
    answer, not Alpaca's)."""

    EMPTY = (200, {"Content-Type": "application/json"}, {})

    def test_carried_out_it_is_found_by_its_client_id(self):
        r = self.rig()
        gate = Gate(r)
        gate.on(Gate.order("AAPL"), lambda carry: (carry(), self.EMPTY)[1])
        m, cid = r.mark(), r.cid("P")
        p = {"client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10}
        kind, out = r.req("order.place", p)
        made = self.made(r, cid)
        self.assertEqual(len(made), 1)
        self.assertEqual((kind, out), ("ok", {"order_id": made[0].id,
                                              "status": "accepted"}))
        self.assertNotIn("None", r.a._orders)
        got = r.wait("order.filled", lambda d: d["client_id"] == cid,
                     since=m)
        self.assertTrue(got, r.events(m))
        self.assertEqual(got["order_id"], made[0].id)
        self.assertEqual(r.req("order.place", p)[1]["order_id"], made[0].id)
        # ... and a close answered the same way closes once
        x = r.cid("X")
        self.assertEqual(r.req("position.close", {
            "client_id": x, "position_id": "AAPL"}), ("ok", {}))
        self.assertTrue(until(lambda: r.held("AAPL") == 0.0, 4))
        self.assertTrue(until(lambda: self.closes(r, m, "AAPL"), 4))
        self.assertNotIn("None", r.a._orders)
        self.assertEqual(self.sent(r, "AAPL"), [cid, x])
        self.assertEqual(r.events(m, "order.rejected"), [])

    def test_not_carried_out_it_is_of_unknown_fate_and_never_an_order(self):
        r = self.rig()
        gate = Gate(r)
        gate.on(Gate.order("AAPL"), lambda carry: self.EMPTY)
        m, cid = r.mark(), r.cid("P")
        kind, out = r.req("order.place", {
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("named no order", out["message"])
        self.assertIn("not known", out["message"])
        self.assertIn(cid, r.a._unknown)
        self.assertNotIn("None", r.a._orders)
        self.assertEqual(self.sent(r, "AAPL"), [])
        self.assertEqual(r.events(m, "order.rejected"), [])
        self.assertEqual(r.events(m, "order.accepted"), [])


class OpenProfit(unittest.TestCase):
    """positions.list and a position row that carries no
    `unrealized_pl`."""

    def rows(self, *rows):
        a, b = orders.make()
        b.on("GET", "/v2/positions", list(rows))
        return {p["position_id"]: p
                for p in a.handle_positions_list({})["positions"]}

    @staticmethod
    def row(sym, qty, side="long", **more):
        return dict({"symbol": sym, "asset_id": "id-" + sym, "qty": qty,
                     "qty_available": qty, "side": side,
                     "avg_entry_price": "350"}, **more)

    def test_it_is_alpaca_s_own_figure_when_the_row_carries_one(self):
        # (a guard) what is worked out never stands in for the figure
        # Alpaca gives
        got = self.rows(self.row("AAPL", "10", unrealized_pl="-500",
                                 market_value="3000", cost_basis="3500"))
        self.assertEqual(got["AAPL"]["unrealized_pnl"], -500.0)

    def test_without_it_the_figure_is_worked_out_from_value_and_cost(self):
        # was 0.0: a position 500 under water shown as square
        got = self.rows(
            self.row("AAPL", "10", market_value="3000", cost_basis="3500"),
            self.row("BTCUSD", "0.5", market_value="37100",
                     cost_basis="37000", unrealized_pl=None),
            # a short that has gained: by magnitude and the row's side,
            # whatever sign Alpaca gives a short's value and cost
            self.row("YI", "-4", side="short", market_value="-380",
                     cost_basis="-400", unrealized_pl=""))
        self.assertEqual(got["AAPL"]["unrealized_pnl"], -500.0)
        self.assertEqual(got["BTC/USD"]["unrealized_pnl"], 100.0)
        self.assertEqual(got["YI"]["unrealized_pnl"], 20.0)

    def test_when_it_cannot_be_worked_out_it_is_left_out_never_zero(self):
        got = self.rows(self.row("AAPL", "10", market_value="3000"),
                        self.row("YI", "5"))
        for sym in ("AAPL", "YI"):
            self.assertNotIn("unrealized_pnl", got[sym])
            # the rest of the position is there as ever
            self.assertEqual((got[sym]["side"], got[sym]["avg_price"],
                              got[sym]["swap"]), ("buy", 350.0, 0.0))
        self.assertEqual(got["AAPL"]["qty"], 10.0)
        json.dumps(got, allow_nan=False)

    def test_account_get_still_refuses_to_count_such_a_row_as_zero(self):
        # (a guard: the one derivation serves both)
        view = mod.AlpacaAdapter._account_view(
            {"equity": "100000", "initial_margin": "0", "currency": "USD"},
            [self.row("AAPL", "10", market_value="3000",
                      cost_basis="3500")])
        self.assertEqual(view["balance"], 100500.0)
        with self.assertRaises(AdapterError) as ctx:
            mod.AlpacaAdapter._account_view(
                {"equity": "100000", "initial_margin": "0"},
                [self.row("AAPL", "10")])
        self.assertEqual((ctx.exception.code, ctx.exception.retryable),
                         ("x_alpaca_no_pnl", True))


class ReadsInSequence(closing_orders.Unanswered):
    """flatten_all's wait loop reads the open orders first and the
    positions after them, in every pass, not side by side."""

    def test_a_known_order_that_fills_between_the_two_reads_is_not_missed(self):
        # MSFT 5 held; a limit BUY 10 AAPL of the user's rests (answered,
        # known). flatten_all asks for every order to be cancelled, and
        # Alpaca takes the requests but has not carried them out yet
        # (it was seen to need most of a second). In the pass that would
        # say "flat": the positions are read (none), THEN the order
        # fills, THEN the open orders are read (none).
        r = self.rig()
        r.buy("MSFT", 5)
        px = r.fake.state()["prices"]["AAPL"]
        r.fake.set_price("AAPL", price=px)
        cid = r.cid("L")
        self.assertEqual(r.req("order.place", {
            "client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10,
            "type": "limit", "price": round(px - 5, 2)})[0], "ok")
        self.assertTrue(until(lambda: r.live("AAPL"), 4))
        gate = Gate(r)
        gate.on(cancel_all, lambda carry: (
            207, {"Content-Type": "application/json"}, []))
        gate.on(lambda method, path, q, data: method == "DELETE"
                and path.startswith("/v2/orders/"),
                lambda carry: (204, {}, None))
        read, armed, done = threading.Event(), [], []

        def positions(carry):
            out = carry()
            if armed:
                read.set()
            return out

        def open_orders(carry):
            if armed and not done:
                done.append(1)
                read.wait(1.0)            # (the positions were read: none)
                r.fake.set_price("AAPL", price=round(px - 6, 2))
                until(lambda: r.held("AAPL") == 10.0, 3.0)   # ... it fills
            return carry()                # what Alpaca lists NOW
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/positions", positions)
        gate.on(lambda method, path, q, data: method == "GET"
                and path == "/v2/orders" and q.get("nested") == "false"
                and "direction" not in q, open_orders)
        threading.Thread(target=lambda: (until(
            lambda: r.held("MSFT") == 0.0, 6.0), time.sleep(0.05),
            armed.append(1)), daemon=True).start()
        flat = r.req("flatten_all", {"client_id": r.cid("F")})
        held = self.now_held(r)
        self.assertTrue(done, "the order never filled: the test's own fault")
        self.assertEqual(self.flat_when_ok(r, flat, held), "ok", flat)
        self.assertEqual(flat[1], {"closed": 2}, flat)
        self.assertEqual(r.held("AAPL"), 0.0)

    def test_every_pass_reads_the_open_orders_before_the_positions(self):
        r = self.rig()
        for sym, qty in (("MSFT", 5), ("AAPL", 10), ("SPY", 4)):
            r.buy(sym, qty)
        time.sleep(0.5)
        reads, inner = [], r.a.client.call

        def call(method, host, path, query=None, body=None, **kw):
            mine = (kw.get("kind") == "flatten" and method == "GET"
                    and path in ("/v2/positions", "/v2/orders"))
            t0 = time.monotonic()
            if mine:
                time.sleep(0.05)          # (two at once would overlap)
            try:
                return inner(method, host, path, query, body, **kw)
            finally:
                if mine:
                    reads.append((t0, time.monotonic(), path[4:]))
        r.a.client.call = call
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("F")}),
                         ("ok", {"closed": 3}))
        reads.sort()
        names = [n for _, _, n in reads]
        # the first read of all is the positions (what is there to close);
        # after it, in every pass: the open orders, and when THAT has
        # answered, the positions
        self.assertEqual(names[0], "positions", names)
        loop = reads[1:]
        self.assertGreaterEqual(len(loop), 2, names)
        self.assertEqual([n for _, _, n in loop],
                         ["orders", "positions"] * (len(loop) // 2), names)
        for (_, done, _), (begun, _, _) in zip(loop[0::2], loop[1::2]):
            self.assertLessEqual(done, begun, reads)


class StaysAskedFor(closing_orders.Unanswered):
    """A LATER request under the same client id that Alpaca refuses says
    nothing of an EARLIER one whose fate is not known."""

    def test_an_order_refused_when_sent_again_leaves_the_first_watched(self):
        r = self.rig()
        r.buy("MSFT", 5)
        gate = Gate(r)
        slot = gate.hold(Gate.order("AAPL"))     # 504, not carried out yet
        cid = r.cid("P")
        p = {"client_id": cid, "symbol": "AAPL", "side": "buy", "qty": 10,
             "type": "limit", "price": 200.0}
        kind, out = r.req("order.place", p)
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual(kind, "err", out)
        self.assertIn("may still arrive", out["message"])
        self.assertTrue(r.a._unknown[cid]["flat"])
        # sent again under its id, as the answer invited; Alpaca refuses
        # THIS request for a reason of its own
        gate.on(Gate.order("AAPL"), lambda carry: (
            403, {"Content-Type": "application/json"},
            {"code": 40310000, "message": "insufficient buying power"}))
        m = r.mark()
        kind, out = r.req("order.place", p)
        self.assertEqual((kind, out["code"]), ("err", "insufficient_margin"),
                         out)
        # the first request is still of unknown fate, and still watched
        self.assertIn(cid, r.a._unknown)
        self.assertTrue(r.a._unknown[cid]["flat"])
        # ... and when it arrives after all it is dealt with as promised
        gate.hooks.pop()
        gate.land(slot, settle=False)
        said = until(lambda: self.notices(r, m, "is cancelled"), 12)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("buy 10 AAPL", said[-1]["detail"])
        self.assertEqual(self.made(r, cid)[0].status, "canceled")
        self.assertEqual(self.sent(r, "AAPL"), [cid])

    def test_one_that_was_never_sent_leaves_it_too(self):
        # (scripted) the later request never leaves: the handler's time
        # is up before it has a place
        a, b = orders.make()
        now = time.monotonic()
        a._unknown["P1"] = {"kind": "user", "cid": "P1", "t0": now - 3.0,
                            "next": now + 60.0, "n": 1, "flat": True,
                            "side": "buy", "qty": "0.0002",
                            "sym": "BTC/USD"}
        b.on("POST", "/v2/orders", mod.AlpacaError(
            0, "the time for this call is used up", code="deadline"))
        with self.assertRaises(AdapterError):
            a._post_order({"client_order_id": "P1", "symbol": "BTC/USD",
                           "side": "buy", "qty": "0.0002"}, "user", "P1",
                          "buy")
        self.assertTrue(a._unknown["P1"]["flat"])
        self.assertAlmostEqual(now - a._unknown["P1"]["t0"], 3.0, delta=0.5)
        # ... and one that Alpaca TOOK is this wish now: not watched as
        # an order from before a flatten_all
        b.on("POST", "/v2/orders", orders.order("o1", "P1"))
        o, replayed = a._post_order(
            {"client_order_id": "P1", "symbol": "BTC/USD", "side": "buy",
             "qty": "0.0002"}, "user", "P1", "buy")
        self.assertEqual((o["id"], replayed), ("o1", False))
        self.assertNotIn("P1", a._unknown)


class WhoseRefusal(closing_orders.ClosingOrders):
    """flatten_all's "still open" text does not say "Alpaca:" before this
    adapter's OWN pace refusal."""

    QUICK = changed_meanwhile.LateOrder.QUICK

    def test_the_adapter_s_own_limit_is_not_called_alpaca_s(self):
        own = mod.to_adapter_error(mod.AlpacaError(
            429, "the adapter sends Alpaca's trading host 3 requests a "
            "second and more urgent ones went first; nothing was sent; "
            "retry in 0.3s", code="budget", retry_after=0.3))
        self.assertEqual((own.code, own.retryable), ("rate_limited", True))
        self.assertFalse(str(own).startswith("Alpaca:"), str(own))
        self.assertIn("this adapter's own limit", str(own))
        self.assertIn("nothing was sent", str(own))
        # ... and what Alpaca itself refuses is still Alpaca's
        theirs = mod.to_adapter_error(mod.AlpacaError(
            429, "too many requests.", 42910000))
        self.assertEqual((theirs.code, str(theirs)),
                         ("rate_limited", "Alpaca: too many requests."))

    def test_flatten_says_so_of_a_position_it_had_no_place_to_close(self):
        # six positions, one request a second, three seconds
        r = self.start(syms=(("MSFT", 5), ("AAPL", 10), ("SPY", 4),
                             ("XRX", 30), ("DTI", 7), ("YI", 3)))
        r.a.client.budget.per_second = 1
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("not flat", out["message"])
        self.assertIn("this adapter's own limit", out["message"])
        self.assertNotIn("(Alpaca: the adapter", out["message"])
        r.a.client.budget.per_second = 40
        self.assertEqual(r.req("flatten_all", {"client_id": r.cid("F")})[0],
                         "ok")


class ExitOnTheWire(closing_orders.ClosingOrders):
    """A check: AAPL 10 held; order.modify sets a stop at 300; the adapter
    is KILLED while the POST of that stop order is on the wire (the
    request reaches Alpaca later), and started again."""

    QUICK = changed_meanwhile.LateOrder.QUICK

    def killed(self, late):
        r = self.rig()
        r.buy("AAPL", 10)
        r.rules.add("POST", "/v2/orders", body='"stop"', before=late,
                    count=1, name="stop on the wire")
        r.send("order.modify", {"client_id": r.cid("m"),
                                "position_id": "AAPL", "sl": 300.0})
        self.assertTrue(until(lambda: r.rules.hit("stop on the wire"), 4))
        a = r.a                               # as Restart.kill, less start
        a._state_dir = ""

        def dead(*args, **kw):
            raise mod.AlpacaError(0, "this process was killed")
        a.client.call = dead
        a._stop.set()
        for ev in (a._wake, a.prot.wake, a._quote_wake):
            ev.set()
        r.a = None
        r.fake.drop_streams()
        # what the dead process left for the next one
        with open(os.path.join(r.state, "alpaca_state.json")) as fh:
            left = json.load(fh)["unknown"]
        self.assertEqual(len(left), 1, left)
        coid, row = list(left.items())[0]
        self.assertTrue(coid.startswith("brue-sl-"), coid)
        self.assertEqual({k: row.get(k) for k in (
            "kind", "cid", "side", "qty", "sym", "flat", "wire")}, {
            "kind": "sl", "cid": None, "side": "sell", "qty": "10",
            "sym": "AAPL", "flat": False, "wire": True}, row)
        return r, coid

    def quiet(self, r, since):
        """Nothing was said of a late order, and nothing cancelled."""
        said = [d["detail"] for d in self.notices(r, since)]
        self.assertEqual([d for d in said if "flatten_all" in d
                          or "cancelled" in d or "removed" in d], [], said)
        self.assertEqual(r.events(since, "order.cancelled"), [])
        self.assertEqual(r.events(since, "order.placed"), [])

    def test_landed_before_the_new_run_reads_it_is_the_position_s_stop(self):
        r, coid = self.killed(1.0)
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 300.0)], 4),
                        r.live("AAPL"))
        r.start()
        m = 0                                 # everything this run says
        self.assertTrue(until(lambda: r.shown("AAPL") == (10.0, 300.0, None),
                              6), r.shown("AAPL"))
        self.assertTrue(until(lambda: r.a._unknown == {}, 6), r.a._unknown)
        time.sleep(3.0)
        # the SAME order is the stop: not cancelled, nothing placed beside
        self.assertEqual([o["coid"] for o in r.live("AAPL")], [coid])
        self.assertEqual(r.a._late_due, {})
        self.assertEqual(len(self.sent(r, "AAPL")), 2)   # the buy, the stop
        self.quiet(r, m)
        self.assertTrue(self.settled(r), self.last)

    def test_landing_after_the_new_run_has_started_it_is_never_a_late_order(self):
        # The new run finds no stop at Alpaca and a level that was due
        # half a second ago: it puts the stop back (its duty). The first
        # request then arrives beside that stop.
        r, coid = self.killed(3.0)
        t = time.monotonic()
        r.start()
        self.assertIn(coid, r.a._unknown)             # asked for, by its id
        self.assertEqual((r.a._unknown[coid]["kind"],
                          r.a._unknown[coid]["flat"]), ("sl", False))
        self.assertTrue(until(lambda: r.stops("AAPL") == [(10.0, 300.0)], 6),
                        r.live("AAPL"))
        time.sleep(max(0.0, t + 3.5 - time.monotonic()) + 2.5)
        # one stop for ten at 300, whichever of the two requests made it
        self.assertEqual(r.stops("AAPL"), [(10.0, 300.0)], r.live("AAPL"))
        self.assertEqual(r.shown("AAPL"), (10.0, 300.0, None))
        self.assertEqual(r.held("AAPL"), 10.0)
        self.assertEqual(r.a._late_due, {})
        self.quiet(r, 0)
        self.assertTrue(self.settled(r), self.last)

    def test_flatten_all_after_such_a_restart_treats_it_as_any_unknown_order(self):
        r, coid = self.killed(7.0)
        r.start()
        m = r.mark()
        kind, out = r.req("flatten_all", {"client_id": r.cid("F")})
        # not "flat" while it may still arrive; named; retryable
        self.assertEqual((kind, out.get("retryable")), ("err", True), out)
        self.assertIn("not known to be flat", out["message"])
        self.assertIn("sell 10 AAPL", out["message"])
        self.assertIn(coid, out["message"])
        self.assertEqual(r.held("AAPL"), 0.0)        # what was there is closed
        self.assertTrue(r.a._unknown[coid]["flat"])
        # ... it arrives (a sell stop with no position behind it now): it
        # is cancelled, and that is said, as of any order from before a
        # flatten_all
        self.assertTrue(until(lambda: self.made(r, coid), 10))
        self.assertTrue(until(
            lambda: self.made(r, coid)[0].status == "canceled", 8),
            self.made(r, coid)[0].status)
        said = until(lambda: self.notices(r, m, "is cancelled"), 8)
        self.assertTrue(said, r.events(m, "session.state"))
        self.assertIn("sell 10 AAPL", said[-1]["detail"])
        self.assertEqual(r.held("AAPL"), 0.0)
        self.assertEqual(r.ok("flatten_all", {"client_id": r.cid("F")}),
                         {"closed": 0})
        self.assertEqual(r.live(), [])


if __name__ == "__main__":
    unittest.main()
