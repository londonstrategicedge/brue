"""The broker's terms in the handshake (SPEC 3.3).

An adapter states what its broker's own terms let a platform do: keep the
broker's market data on the user's computer (data_local_only), and place
orders on a live account only by a person's action (orders_by_hand_only).
Unlike broker_info and the credential form, a platform ACTS on it, and each
flag only takes something away, so both flags fail closed.

Run: python3 -m unittest tests.test_terms -v
"""
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brueconnect import Adapter, Connector, protocol  # noqa: E402
from brueconnect.client import ConnectorError, Disconnected, NotArmed  # noqa: E402

NONE = {"data_local_only": False, "orders_by_hand_only": False}
BOTH = {"data_local_only": True, "orders_by_hand_only": True}


class TestSanitise(unittest.TestCase):
    def test_no_terms_restrict_nothing(self):
        self.assertEqual(protocol.sanitize_terms(None), NONE)
        self.assertEqual(protocol.sanitize_terms({}), NONE)

    def test_each_flag_on_its_own(self):
        self.assertEqual(protocol.sanitize_terms({"data_local_only": True}),
                         {"data_local_only": True,
                          "orders_by_hand_only": False})
        self.assertEqual(protocol.sanitize_terms({"orders_by_hand_only": True}),
                         {"data_local_only": False,
                          "orders_by_hand_only": True})

    def test_plain_false_and_null_mean_false(self):
        self.assertEqual(protocol.sanitize_terms(
            {"data_local_only": False, "orders_by_hand_only": None}), NONE)

    def test_anything_else_fails_closed(self):
        """A restriction misspelt must not become no restriction."""
        for odd in ("false", "no", "", 0, 1, "true", [], {}, 0.0, "0"):
            with self.subTest(value=odd):
                got = protocol.sanitize_terms({"data_local_only": odd})
                self.assertIs(got["data_local_only"], True)
                self.assertIs(got["orders_by_hand_only"], False)

    def test_terms_that_are_not_an_object_set_both(self):
        for odd in ("data_local_only", ["data_local_only"], 1, True, 0):
            with self.subTest(value=odd):
                self.assertEqual(protocol.sanitize_terms(odd), BOTH)

    def test_the_summary_is_plain_text_capped(self):
        got = protocol.sanitize_terms(
            {"summary": "Prices‮ are​ yours\n\nalone. " + "x" * 400})
        self.assertTrue(got["summary"].startswith("Prices are yours alone. x"))
        self.assertEqual(len(got["summary"]), protocol.TERMS_SUMMARY)
        self.assertNotIn("‮", got["summary"])

    def test_the_url_is_https_only(self):
        ok = "https://broker.example/legal/api-licence"
        self.assertEqual(protocol.sanitize_terms({"url": ok})["url"], ok)
        for bad in ("http://broker.example/terms", "javascript:alert(1)",
                    "mailto:legal@broker.example", "data:text/html,x",
                    "file:///etc/passwd", 7, None):
            with self.subTest(url=bad):
                self.assertNotIn("url", protocol.sanitize_terms({"url": bad}))

    def test_unknown_fields_do_not_pass(self):
        got = protocol.sanitize_terms({"data_local_only": True,
                                       "allow_ai": True, "html": "<b>x</b>"})
        self.assertEqual(sorted(got), ["data_local_only", "orders_by_hand_only"])

    def test_sanitising_twice_changes_nothing(self):
        once = protocol.sanitize_terms({"data_local_only": "yes",
                                        "summary": " a  b ",
                                        "url": "https://x.example/t"})
        self.assertEqual(protocol.sanitize_terms(once), once)

    def test_junk_is_survivable_and_fails_closed(self):
        for junk in (object(), float("nan"), b"bytes"):
            with self.subTest(junk=junk):
                self.assertEqual(protocol.sanitize_terms(junk), BOTH)
        self.assertEqual(protocol.sanitize_terms(
            {"summary": object(), "url": ["https://x.example"]}), NONE)


class TestAdapterSide(unittest.TestCase):
    def test_an_adapter_without_terms_sends_no_field(self):
        class Plain(Adapter):
            pass
        self.assertNotIn("terms", Plain()._hello({}))

    def test_declared_terms_are_sent_in_hello(self):
        class Licensed(Adapter):
            terms = {"data_local_only": True,
                     "summary": "Prices stay on this computer.",
                     "url": "https://broker.example/legal"}
        self.assertEqual(Licensed()._hello({})["terms"],
                         {"data_local_only": True,
                          "orders_by_hand_only": False,
                          "summary": "Prices stay on this computer.",
                          "url": "https://broker.example/legal"})

    def test_a_flag_that_is_not_a_boolean_is_read_as_true_and_named(self):
        class Careless(Adapter):
            name = "careless"
            terms = {"orders_by_hand_only": "false", "url": "http://x.example"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = Careless()._hello({})
        self.assertIs(out["terms"]["orders_by_hand_only"], True)
        self.assertNotIn("url", out["terms"])
        self.assertIn("orders_by_hand_only", err.getvalue())
        self.assertIn("dropped ['url']", err.getvalue())
        self.assertIn("SPEC 3.3", err.getvalue())

    def test_terms_that_raise_declare_both_restrictions(self):
        class Broken(Adapter):
            name = "broken"

            @property
            def terms(self):
                raise RuntimeError("config service down")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = Broken()._hello({})
        self.assertEqual(out["terms"], BOTH)
        self.assertEqual(out["mode"], "paper")       # the connection survives
        self.assertIn("terms failed", err.getvalue())

    def test_terms_are_json(self):
        class Licensed(Adapter):
            terms = {"data_local_only": True, "orders_by_hand_only": True}
        json.dumps(Licensed()._hello({}))


SDK_ADAPTER = """
    import sys
    sys.path.insert(0, {root!r})
    from brueconnect import Adapter

    class Licensed(Adapter):
        name, version, broker = "licensed", "0.1", "Licensed Broker"
        terms = {{"data_local_only": True, "summary": "Prices are yours alone."}}

    Licensed().run()
"""

RAW_ADAPTER = """
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["method"] == "hello":
            result = {"protocol": 1, "adapter": {"name": "raw", "version": "1"},
                      "broker": "raw", "mode": "live",
                      "account_currency": "USD", "account_model": "netting",
                      "capabilities": {"flatten_all": True},
                      "terms": TERMS}
        else:
            result = {}
        sys.stdout.write(json.dumps({"v": 1, "id": msg["id"], "type": "res",
                                     "result": result}) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


class TestPlatformSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = dict(os.environ)
        os.environ["BRUE_CONNECT_HOME"] = os.path.join(self.tmp.name, "home")
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def _write(self, name, body):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body))
        return path

    def _hello_terms(self, path):
        c = Connector([sys.executable, path])
        before = dict(c.terms)
        try:
            c.hello()
            return before, c.terms
        finally:
            c.close()

    def test_the_connector_exposes_the_terms_after_hello(self):
        path = self._write("licensed.py", SDK_ADAPTER.format(root=str(ROOT)))
        before, after = self._hello_terms(path)
        self.assertEqual(before, NONE)        # nothing known before hello
        self.assertEqual(after, {"data_local_only": True,
                                 "orders_by_hand_only": False,
                                 "summary": "Prices are yours alone."})

    def test_an_adapter_that_says_nothing_restricts_nothing(self):
        path = self._write("raw.py", RAW_ADAPTER.replace(
            '"terms": TERMS', '"x": 0'))
        self.assertEqual(self._hello_terms(path)[1], NONE)

    def test_the_connector_fails_closed_on_what_a_raw_adapter_sends(self):
        for raw, want in (("'yes'", BOTH),
                          ("{'data_local_only': 'false'}",
                           {"data_local_only": True,
                            "orders_by_hand_only": False}),
                          ("{'orders_by_hand_only': 1, 'url': 'javascript:x'}",
                           {"data_local_only": False,
                            "orders_by_hand_only": True})):
            with self.subTest(raw=raw):
                path = self._write("raw.py", RAW_ADAPTER.replace("TERMS", raw))
                self.assertEqual(self._hello_terms(path)[1], want)


HAND = {"orders_by_hand_only": True, "summary": "Each order is yours to place."}


class TestArming(unittest.TestCase):
    """orders_by_hand_only is held where every platform consents to real
    money: Connector.arm() (SPEC 3.3, the SDK's rule)."""

    def _conn(self, mode, terms):
        c = Connector([sys.executable, "-c", "pass"])   # never started
        c.handshake = {"broker": "Hand Broker"}
        if mode is not None:
            c.handshake["mode"] = mode
        c.terms = protocol.sanitize_terms(terms)
        return c

    def test_a_live_by_hand_connection_refuses_the_usual_arming(self):
        c = self._conn("live", HAND)
        with self.assertRaises(NotArmed) as cm:
            c.arm("a strategy wants to trade")
        self.assertFalse(c.armed)
        self.assertEqual(cm.exception.code, "not_armed")
        msg = str(cm.exception)
        self.assertIn("Hand Broker", msg)
        self.assertIn("SPEC 3.3", msg)
        self.assertIn("Each order is yours to place.", msg)

    def test_the_platforms_word_arms_it(self):
        c = self._conn("live", HAND)
        self.assertEqual(c.arm("Allow trading, pressed by the user",
                               by_hand=True),
                         "Allow trading, pressed by the user")
        self.assertTrue(c.armed)

    def test_only_true_is_the_platforms_word(self):
        for odd in ("yes", 1, [1], "true"):
            with self.subTest(by_hand=odd):
                c = self._conn("live", HAND)
                with self.assertRaises(NotArmed):
                    c.arm("x", by_hand=odd)
                self.assertFalse(c.armed)

    def test_an_unknown_mode_is_not_paper(self):
        with self.assertRaises(NotArmed):
            self._conn(None, HAND).arm("x")

    def test_paper_is_not_bound(self):
        c = self._conn("paper", HAND)
        c.arm("x")
        self.assertTrue(c.armed)

    def test_a_broker_without_the_flag_arms_as_before(self):
        for terms in (None, {"data_local_only": True}):
            with self.subTest(terms=terms):
                c = self._conn("live", terms)
                c.arm("x")
                self.assertTrue(c.armed)


RECORDING_ADAPTER = """
    import json, sys
    seen = open({log!r}, "a")
    for line in sys.stdin:
        msg = json.loads(line)
        seen.write(msg["method"] + "\\n")
        seen.flush()
        if msg["method"] == "hello":
            result = {{"protocol": 1,
                      "adapter": {{"name": "hand", "version": "1"}},
                      "broker": "Hand Broker", "mode": {mode!r},
                      "account_currency": "USD", "account_model": "netting",
                      "capabilities": {{"flatten_all": True,
                                       "data": {{"bars": True,
                                                "history": True}}}},
                      "terms": {terms!r}}}
        else:
            result = {{}}
        sys.stdout.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
                                     "result": result}}) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


class TestTheKitsOwnRunners(unittest.TestCase):
    """The kit's strategy runner and conformance suite send orders nobody
    placed, so a live connection whose broker keeps orders by hand is refused
    before arming, with the reason, and is never armed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seen = os.path.join(self.tmp.name, "seen.txt")
        self.saved = dict(os.environ)
        os.environ["BRUE_CONNECT_HOME"] = os.path.join(self.tmp.name, "home")
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def _adapter(self, mode, terms=None):
        path = os.path.join(self.tmp.name, f"hand_{mode}.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(RECORDING_ADAPTER.format(
                log=self.seen, mode=mode,
                terms=HAND if terms is None else terms)))
        return f"{sys.executable} {path}"

    def _methods(self):
        try:
            with open(self.seen) as fh:
                return fh.read().split()
        except FileNotFoundError:
            return []

    def _drive(self, mode, arm_live, terms=None):
        from brueconnect.runner import live_driver
        script = os.path.join(self.tmp.name, "s.brue")
        with open(script, "w") as fh:
            fh.write("// a strategy\n")
        args = argparse.Namespace(
            script=script, symbol="EURUSD", timeframe="1m",
            adapter=self._adapter(mode, terms), broker=None, warmup=10, bars=1,
            bar_timeout=1.0, flatten_on_exit=False, arm_live=arm_live,
            api_key="", journal="", status="")
        # A verified LSE account, without the network: what --arm-live needs.
        account = types.SimpleNamespace(key_id="fixture-key-0000",
                                        user_id="u", plan_id="p",
                                        environment="test")
        out = io.StringIO()
        d = live_driver.LiveDriver(args)
        try:
            with mock.patch("brue.auth.login", return_value=account), \
                    contextlib.redirect_stdout(out):
                rc = d.run()
        finally:
            d.c.close()
        return rc, out.getvalue(), d.c

    def test_the_strategy_runner_refuses_a_live_by_hand_broker(self):
        rc, out, c = self._drive("live", arm_live=True)
        self.assertEqual(rc, 2)
        self.assertIn("Not armed: under Hand Broker's terms", out)
        self.assertIn("demo account", out)
        self.assertNotIn("ARMED", out)
        self.assertFalse(c.armed)
        self.assertEqual([m for m in self._methods()
                          if m in Connector.MUTATING], [])

    def test_on_paper_the_runner_shows_the_terms_and_goes_on(self):
        rc, out, _ = self._drive("paper", arm_live=False)
        self.assertIn("the broker's terms: Each order is yours to place.", out)
        self.assertNotIn("Not armed", out)
        # It went on past the terms, to the next check of its own.
        self.assertIn("EURUSD not in catalog", out)
        self.assertEqual(rc, 2)

    def test_the_runner_shows_the_terms_when_it_arms_a_live_connection(self):
        rc, out, c = self._drive("live", arm_live=True, terms={
            "data_local_only": True, "summary": "Prices are yours alone."})
        self.assertIn("the broker's terms: Prices are yours alone.", out)
        self.assertLess(out.index("the broker's terms"), out.index("ARMED"))
        self.assertTrue(c.armed)
        self.assertIn("EURUSD not in catalog", out)   # its next check
        self.assertEqual(rc, 2)

    def test_capabilities_that_are_not_an_object_are_read_as_none(self):
        """Read as (capabilities or {}).get("data"), capabilities sent as a
        list would end the runner with an AttributeError traceback.
        Whatever their shape, capabilities that are not an object are
        absent, and the runner says what the broker does not declare."""
        from brueconnect.runner import live_driver
        script = os.path.join(self.tmp.name, "s.brue")
        with open(script, "w") as fh:
            fh.write("// a strategy\n")
        for caps in (["flatten_all", "bars", "history"], "data", 7, True,
                     None, {"data": ["bars", "history"]}):
            with self.subTest(caps=caps):
                path = os.path.join(self.tmp.name, "caps.py")
                with open(path, "w") as fh:
                    fh.write(textwrap.dedent(CAPS_SHAPE_ADAPTER.format(
                        caps=json.dumps(caps))))
                args = argparse.Namespace(
                    script=script, symbol="EURUSD", timeframe="1m",
                    adapter=f"{sys.executable} {path}", broker=None,
                    warmup=10, bars=1, bar_timeout=1.0,
                    flatten_on_exit=False, arm_live=False, api_key="",
                    journal="", status="")
                out = io.StringIO()
                d = live_driver.LiveDriver(args)
                try:
                    with contextlib.redirect_stdout(out):
                        rc = d.run()
                finally:
                    d.c.close()
                self.assertEqual(rc, 2, out.getvalue())
                self.assertIn("this broker declares no bars and no history",
                              out.getvalue())

    def test_conformance_will_not_certify_a_live_by_hand_account(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(
            [sys.executable, "-m", "brueconnect.conformance.run",
             "--adapter", self._adapter("live"), "--live"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("refusing to place orders on a live account", p.stdout)
        self.assertIn("Hand Broker", p.stdout)
        self.assertNotIn("ARMED", p.stdout)
        self.assertEqual([m for m in self._methods()
                          if m in Connector.MUTATING], [])


SECOND_HELLO_ADAPTER = """
    import json, sys
    seen = open({log!r}, "a")
    hellos = 0
    for line in sys.stdin:
        msg = json.loads(line)
        seen.write(msg["method"] + "\\n")
        seen.flush()
        if msg["method"] == "hello":
            hellos += 1
            result = {{"protocol": 1,
                      "adapter": {{"name": "hand", "version": "1"}},
                      "broker": "Hand Brokers", "mode": {mode!r},
                      "account_currency": "USD", "account_model": "netting",
                      "capabilities": {{"flatten_all": True}}}}
            if hellos >= {from_hello}:
                if {silent!r}:
                    continue
                result["terms"] = {terms!r}
                result["protocol"] = {protocol!r}
                result["mode"] = {later_mode!r} or result["mode"]
                if {odd!r} is not None:
                    result = json.loads({odd!r})
        else:
            result = {{}}
        sys.stdout.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
                                     "result": result}}) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""

ORDER = {"client_id": "c1", "symbol": "EURUSD", "side": "buy", "qty": 1,
         "type": "market"}


class TestTheWordIsCheckedAtEachOrder(unittest.TestCase):
    """An arming made before the broker said its live orders are placed by
    hand does not cover them: the SDK checks the by_hand word at every
    mutating call, against the terms known then (SPEC 3.3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seen = os.path.join(self.tmp.name, "seen.txt")

    def _connector(self, from_hello, mode="live", terms=HAND, protocol=1,
                   later_mode=None, odd=None, silent=False):
        """The adapter above. From hello number `from_hello` on, it sends
        `terms` with `protocol` and `later_mode`, or answers `odd` (a JSON
        text) as the whole result, or does not answer (`silent`)."""
        path = os.path.join(self.tmp.name, "hand.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(SECOND_HELLO_ADAPTER.format(
                log=self.seen, from_hello=from_hello, terms=terms, mode=mode,
                protocol=protocol, later_mode=later_mode, odd=odd,
                silent=silent)))
        c = Connector([sys.executable, path])
        self.addCleanup(c.close)
        return c

    def _orders_seen(self):
        try:
            with open(self.seen) as fh:
                return [m for m in fh.read().split() if m in Connector.MUTATING]
        except FileNotFoundError:
            return []

    def test_armed_before_hello(self):
        c = self._connector(from_hello=1)
        c.arm("a bot, armed at start-up")      # nothing is known yet
        c.connect()
        self.assertTrue(c.terms["orders_by_hand_only"])
        with self.assertRaises(NotArmed) as cm:
            c.request("order.place", dict(ORDER))
        self.assertIn("order.place refused. Not armed: under Hand Brokers' "
                      "terms", str(cm.exception))
        self.assertIn("The terms: Each order is yours to place.",
                      str(cm.exception))
        self.assertEqual(self._orders_seen(), [])

    def test_terms_that_arrive_with_a_later_hello(self):
        c = self._connector(from_hello=2)
        c.connect()                              # live, no terms yet
        c.arm("armed while the broker said nothing")
        c.connect()                              # now it keeps orders by hand
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._orders_seen(), [])

    def test_only_true_is_the_word_at_the_order_too(self):
        c = self._connector(from_hello=1)
        c.arm("a bot that says yes", by_hand="yes")   # before hello: armed
        c.connect()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._orders_seen(), [])

    def test_paper_is_not_bound_at_the_order(self):
        c = self._connector(from_hello=1, mode="paper")
        c.connect()
        c.request("order.place", dict(ORDER))               # never armed
        c.arm("armed without the word")
        c.request("order.place", dict(ORDER, client_id="c2"))
        self.assertEqual(self._orders_seen(), ["order.place", "order.place"])

    def test_an_unarmed_connection_is_told_to_arm_first(self):
        """The unarmed refusal comes first and keeps its words: a platform
        reads its tail to offer the arming decision."""
        c = self._connector(from_hello=1)
        c.connect()
        with self.assertRaises(NotArmed) as cm:
            c.request("order.place", dict(ORDER))
        self.assertTrue(str(cm.exception).endswith("Call arm(reason) first."))
        self.assertEqual(self._orders_seen(), [])

    def test_the_platforms_word_lets_the_users_order_through(self):
        """The control: the same adapter, armed with the word, gets the order,
        so the two refusals above are the rule and not the fixture."""
        c = self._connector(from_hello=1)
        c.connect()
        c.arm("Allow trading, pressed by the user", by_hand=True)
        c.request("order.place", dict(ORDER))
        self.assertEqual(self._orders_seen(), ["order.place"])
        c.disarm()
        c.arm("armed again", by_hand=True)
        self.assertTrue(c.armed)

    def test_a_licence_alone_does_not_hold_the_order(self):
        """The order-time rule binds orders_by_hand_only and nothing else: a
        live connection whose terms keep only its prices here, armed the
        usual way, trades."""
        c = self._connector(from_hello=1, terms={"data_local_only": True})
        c.connect()
        c.arm("Allow trading, pressed by the user")
        c.request("order.place", dict(ORDER))
        self.assertEqual(self._orders_seen(), ["order.place"])

    def test_each_arming_replaces_the_word(self):
        """An arming without the word replaces one with it: the bot's arming
        after the user's does not inherit the user's word."""
        c = self._connector(from_hello=2)
        c.connect()                              # live, no terms yet
        c.arm("Allow trading, pressed by the user", by_hand=True)
        c.arm("a bot")
        c.connect()                              # now it keeps orders by hand
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._orders_seen(), [])


class TestAHandshakeThatIsNotOurs(unittest.TestCase):
    """A hello whose result is not an object, or not this protocol, ends the
    connection and is never kept (SPEC 1: the platform disconnects); and no
    mutating call goes out on a connection whose handshake was never read
    (SPEC 3: hello comes first). So no order reaches a broker on terms the
    kit never read, whatever order a caller arms and calls in."""

    setUp = TestTheWordIsCheckedAtEachOrder.setUp
    _connector = TestTheWordIsCheckedAtEachOrder._connector
    _orders_seen = TestTheWordIsCheckedAtEachOrder._orders_seen

    def _no_order(self, c):
        with self.assertRaises((ConnectorError, Disconnected)):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._orders_seen(), [])

    def test_armed_before_a_hello_of_another_protocol(self):
        c = self._connector(from_hello=1, protocol=2)
        c.arm("a bot, armed at start-up")
        with self.assertRaises(ConnectorError) as cm:
            c.connect()
        self.assertIn("protocol version mismatch", str(cm.exception))
        self.assertFalse(c.alive)
        self.assertEqual(c.handshake, {})
        self._no_order(c)

    def test_armed_after_it(self):
        c = self._connector(from_hello=1, protocol=2)
        with self.assertRaises(ConnectorError):
            c.connect()
        c.arm("a bot")
        self._no_order(c)

    def test_a_good_paper_hello_then_one_of_another_protocol(self):
        c = self._connector(from_hello=2, mode="paper", later_mode="live",
                            protocol=2)
        c.connect()
        self.assertTrue(c.is_paper)
        with self.assertRaises(ConnectorError):
            c.connect()
        self._no_order(c)

    def test_a_hello_that_is_not_an_object(self):
        for odd in ("[1]", '"hello"', "null"):
            with self.subTest(result=odd):
                if os.path.exists(self.seen):
                    os.remove(self.seen)
                c = self._connector(from_hello=1, odd=odd)
                with self.assertRaises(ConnectorError) as cm:
                    c.connect()
                self.assertIn("did not answer with an object", str(cm.exception))
                self.assertEqual(c.identity()["mode"], "unknown")
                c.arm("x")
                self._no_order(c)

    def test_a_hello_that_timed_out(self):
        c = self._connector(from_hello=1, silent=True)
        c.arm("a bot")
        with self.assertRaises(TimeoutError):
            c.hello(timeout=0.5)
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", dict(ORDER))
        self.assertIn("no handshake has been read", str(cm.exception))
        self.assertEqual(self._orders_seen(), [])

    def test_an_order_before_any_hello(self):
        c = self._connector(from_hello=1)
        c.start()
        c.arm("a bot")
        with self.assertRaises(ConnectorError) as cm:
            c.request("order.place", dict(ORDER))
        self.assertIn("no handshake has been read", str(cm.exception))
        self.assertEqual(self._orders_seen(), [])

    def test_a_boolean_is_not_protocol_1(self):
        """JSON's true is not a number, so `"protocol": true` is not this
        protocol, though Python reads True == 1."""
        c = self._connector(from_hello=1, mode="paper", protocol=True)
        with self.assertRaises(ConnectorError) as cm:
            c.connect()
        self.assertIn("protocol version mismatch", str(cm.exception))
        self.assertFalse(c.alive)
        self.assertEqual(c.handshake, {})
        self._no_order(c)

    def test_the_number_1_written_as_1_0_is_protocol_1(self):
        """SPEC 10 calls the version "the `hello.protocol` number", JSON has
        one number type, and an adapter may be written in any language that
        writes JSON (SPEC 1); no sentence of the SPEC asks for an integer. So
        1.0 is the number 1 and is kept."""
        c = self._connector(from_hello=1, mode="paper", protocol=1.0)
        self.assertEqual(c.connect()["protocol"], 1)
        c.request("order.place", dict(ORDER))           # paper: no arming
        self.assertEqual(self._orders_seen(), ["order.place"])

    def test_the_suites_handshake_check_reads_the_number_too(self):
        """The conformance suite's own check (s01), on the handshake the
        Connector kept, holds the same rule."""
        from brueconnect.conformance import run

        class Kept:
            """What s01 reads of a Connector."""
            is_paper = True

            def __init__(self, hs):
                self.hs = hs

            def connect(self):
                return self.hs

        for proto, want in ((True, "FAIL"), (1, "PASS"), (1.0, "PASS")):
            with self.subTest(protocol=proto):
                s = run.Suite([sys.executable, "-c", "pass"], None, False)
                s.c = Kept({"protocol": proto, "mode": "paper",
                            "account_model": "netting",
                            "adapter": {"name": "hand", "version": "1"},
                            "capabilities": {"flatten_all": True}})
                with contextlib.redirect_stdout(io.StringIO()):
                    s.s01_handshake()
                self.assertEqual(s.results[0][:2],
                                 ("handshake sane + flatten_all declared",
                                  want))

    def _suite(self, hello, error=None, once=False, silent=(), command=None):
        """The conformance suite run against an adapter that answers hello
        with `hello` (the JSON value of its result) or with `error` (the
        first hello only, when `once`), and leaves `silent` unanswered; or
        against `command` as it is."""
        path = os.path.join(self.tmp.name, "odd_hello.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(SUITE_HELLO_ADAPTER.format(
                log=self.seen, result=hello, error=error, once=once,
                silent=tuple(silent))))
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, "-m", "brueconnect.conformance.run",
             "--adapter", command or f"{sys.executable} {path}"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=120)

    def _stopped_after_s00(self, p, why):
        """One FAIL (s00's, naming `why`), the rest skipped with the reason
        said ONCE on one line (the first skip), exit 1, and no report line
        that is not one of the suite's own."""
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, out)
        self.assertNotIn("Traceback", out)
        lines = p.stdout.splitlines()
        fails = [ln for ln in lines if "[FAIL]" in ln]
        self.assertEqual(len(fails), 1, out)
        self.assertTrue(fails[0].startswith("  [FAIL] s00_interactive_auth"),
                        out)
        self.assertIn(why, fails[0])
        skips = [ln for ln in lines if "[SKIP]" in ln]
        self.assertEqual(len(skips), 14, out)
        self.assertTrue(skips[0].startswith("  [SKIP] s01_handshake :: no "
                                            "handshake was read"), out)
        for ln in skips[1:]:
            self.assertTrue(ln.endswith(":: not run: see s01_handshake"), ln)
        self.assertEqual(sum(1 for ln in lines if ln.startswith("RESULT:")),
                         1, out)
        self.assertEqual(lines[-1], "RESULT: 0 pass, 14 skip, 1 fail", out)
        for ln in lines:
            self.assertRegex(ln, r"^(conformance: |  \[(FAIL|SKIP|info)\] |"
                                 r"RESULT: )", out)

    def _methods(self):
        try:
            with open(self.seen) as fh:
                return fh.read().split()
        except FileNotFoundError:
            return []

    GOOD = {"protocol": 1, "mode": "paper", "account_model": "netting",
            "adapter": {"name": "odd", "version": "1"},
            "capabilities": {"flatten_all": True}}

    def test_the_suite_says_why_and_stops_when_no_handshake_was_read(self):
        """A hello of another protocol, one that is not an object, and one
        refused for good: the suite reports one FAIL that names the error,
        skips every later scenario, says why once, exits 1, and asks the
        adapter nothing more; never a KeyError from a handshake it never
        read (the adapter saw hello, and bye only when it speaks the
        protocol)."""
        for label, hello, error, why in (
                ("protocol 2", json.dumps(dict(self.GOOD, protocol=2)), None,
                 "protocol version mismatch"),
                ("a list", "[1]", None, "did not answer with an object"),
                ("an error", None, json.dumps({
                    "code": "internal", "message": "not today",
                    "retryable": False}), "not today")):
            with self.subTest(hello=label):
                if os.path.exists(self.seen):
                    os.remove(self.seen)
                p = self._suite(hello, error)
                self.assertNotIn("KeyError", p.stdout + p.stderr)
                self._stopped_after_s00(p, why)
                # the error is said once, by the scenario it failed
                self.assertEqual(p.stdout.count(why), 1, p.stdout)
                # An adapter that answered with an error speaks the protocol
                # and is told goodbye; one that does not is not (SPEC 1).
                self.assertEqual(self._methods(), ["hello"] if error is None
                                 else ["hello", "bye"])

    def test_a_refusal_the_adapter_says_may_pass_is_asked_again(self):
        """s00 does not stop the suite on a hello refusal the adapter says
        may pass (retryable) on a connection still open: s01's own hello
        gets past it, and the suite goes on."""
        p = self._suite(json.dumps(self.GOOD), json.dumps({
            "code": "rate_limited", "message": "slow down",
            "retryable": True}), once=True)
        out = p.stdout + p.stderr
        self.assertRegex(p.stdout, r"\[FAIL\] s00_interactive_auth :: "
                                   r"ConnectorError: rate_limited: slow down")
        self.assertIn("[PASS] handshake sane + flatten_all declared", p.stdout)
        self.assertNotIn("no handshake was read", out)
        self.assertNotIn("not run: see", out)
        self.assertEqual(self._methods()[:2], ["hello", "hello"])
        self.assertEqual(p.returncode, 1, out)

    def test_a_refusal_that_does_not_pass_stops_the_suite_after_s01(self):
        """A "may pass" refusal of every hello: s00 and s01 each FAIL with it,
        and the suite stops there, saying why once, rather than running
        every later scenario on a handshake it never read."""
        p = self._suite(json.dumps(self.GOOD), json.dumps({
            "code": "rate_limited", "message": "slow down",
            "retryable": True}))
        lines = p.stdout.splitlines()
        fails = [ln for ln in lines if "[FAIL]" in ln]
        self.assertEqual([ln.split(" :: ")[0] for ln in fails],
                         ["  [FAIL] s00_interactive_auth",
                          "  [FAIL] s01_handshake"], p.stdout)
        skips = [ln for ln in lines if "[SKIP]" in ln]
        self.assertEqual(len(skips), 13, p.stdout)
        self.assertTrue(skips[0].startswith("  [SKIP] s02_reconcile :: no "
                                            "handshake was read"), p.stdout)
        for ln in skips[1:]:
            self.assertTrue(ln.endswith(":: not run: see s02_reconcile"), ln)
        self.assertEqual(lines[-1], "RESULT: 0 pass, 13 skip, 2 fail")
        self.assertEqual(self._methods(), ["hello", "hello", "bye"])
        self.assertEqual(p.returncode, 1)

    def test_an_error_text_never_adds_lines_to_the_report(self):
        """The stop's reason is not the adapter's raw error text printed in
        every later SKIP: a message with line breaks in it would add lines
        that read as results, a forged RESULT line among them, 15 times.
        Each line of the report is one of the suite's own, and the error is
        said once."""
        forged = ("not today\n  [PASS] forged scenario\nRESULT: 15 pass, 0 "
                  "skip, 0 fail\r\n\x1b[2K   [PASS] another")
        p = self._suite(None, json.dumps({"code": "internal",
                                          "message": forged,
                                          "retryable": False}))
        self._stopped_after_s00(p, "not today")
        # Nor a marker of the report inside a line: a reader that searches
        # the report for its RESULT (as tests/test_starter_adapter does)
        # finds the suite's own.
        self.assertNotIn("[PASS]", p.stdout)
        self.assertEqual(p.stdout.count("RESULT:"), 1, p.stdout)
        self.assertEqual(re.findall(r"RESULT: \d+ pass, \d+ skip, \d+ fail",
                                    p.stdout),
                         ["RESULT: 0 pass, 14 skip, 1 fail"])
        self.assertEqual(p.stdout.count("not today"), 1, p.stdout)
        self.assertNotIn("\x1b", p.stdout)

    def test_a_hello_that_times_out_or_cannot_start_stops_the_suite(self):
        """A hello that times out (an adapter that never flushes its answer)
        and an adapter command that cannot start (no such file, or one that
        is not executable) yield no handshake either: the suite stops after
        s00, not after six FAILs and misleading SKIPs over 35 s."""
        with self.subTest(case="timed out"):
            p = self._suite(json.dumps(self.GOOD), silent=("hello",))
            self._stopped_after_s00(p, "TimeoutError")
            self.assertEqual(self._methods(), ["hello", "bye"])
        missing = os.path.join(self.tmp.name, "no-such-adapter")
        not_executable = os.path.join(self.tmp.name, "plain.py")
        with open(not_executable, "w") as fh:
            fh.write("print('not an adapter')\n")
        os.chmod(not_executable, 0o600)
        for case, command, why in (("no such file", missing,
                                    "FileNotFoundError"),
                                   ("not executable", not_executable,
                                    "PermissionError")):
            with self.subTest(case=case):
                self._stopped_after_s00(self._suite(None, command=command),
                                        why)

    def test_the_margin_scenario_reads_capabilities_a_handshake_left_out(
            self):
        """This pins s07's read of the capabilities (an s07 that reads
        handshake["capabilities"] fails here). A handshake with no
        capabilities, or null ones, lacks quotes: s07 has nothing to price
        the test with, and says so."""
        from brueconnect.conformance import run
        for hs in ({"protocol": 1, "mode": "paper"},
                   {"protocol": 1, "mode": "paper", "capabilities": None},
                   {"protocol": 1, "capabilities": {"data": None}}):
            with self.subTest(handshake=hs):
                s = run.Suite([sys.executable, "-c", "pass"], None, False)
                s.c.handshake, s.tradable_symbol = hs, "EURUSD"
                with contextlib.redirect_stdout(io.StringIO()):
                    s.s07_insufficient_margin()
                self.assertEqual(s.results, [(
                    "insufficient margin rejected", "SKIP",
                    "adapter has no quotes capability to price the test")])

    def test_null_capabilities_read_as_none(self):
        """`"capabilities": null`, which adapters in other languages often
        send, gives no AttributeError in s01, in the Connector's reconcile
        and its quote limits, or in the CLI. Null is no capabilities: s01
        FAILs flatten_all as undeclared, reconcile asks no resting orders,
        and check prints the adapter's report."""
        from brueconnect import __main__ as cli
        from brueconnect.conformance import run
        bare = dict(self.GOOD, capabilities=None)
        s = run.Suite([sys.executable, "-c", "pass"], None, False)

        class Kept:
            is_paper = True

            def connect(self):
                return bare

        s.c = Kept()
        with contextlib.redirect_stdout(io.StringIO()):
            s.s01_handshake()
        self.assertEqual(s.results[0][:2],
                         ("handshake sane + flatten_all declared", "FAIL"))
        c = self._connector(from_hello=1, mode="paper")
        c.hello()
        c.handshake = dict(c.handshake, capabilities=None)
        c.reconcile()
        self.assertEqual((c.orders, c.orders_partial, c.quote_limits()),
                         ({}, False, (0, 0)))
        out = io.StringIO()
        with mock.patch.object(cli, "_connect", return_value=c), \
                contextlib.redirect_stdout(out):
            for cmd in (cli.cmd_check, cli.cmd_orders, cli.cmd_operations):
                cmd(argparse.Namespace(broker="hand"))
        self.assertIn("caps     ", out.getvalue())
        self.assertIn("does not offer resting orders", out.getvalue())

    def test_the_suite_reads_capabilities_a_handshake_left_out(self):
        """A hello of this protocol with no `capabilities` at all: the
        bracket scenario says it has nothing to test, not KeyError."""
        p = self._suite(json.dumps({
            "protocol": 1, "mode": "paper", "account_model": "netting",
            "adapter": {"name": "bare", "version": "1"}}))
        out = p.stdout + p.stderr
        self.assertNotIn("KeyError", out)
        self.assertIn("[SKIP] bracket stop fires :: no brackets capability",
                      p.stdout)
        self.assertEqual(p.returncode, 1, out)   # flatten_all is not declared


# What a hostile adapter puts in each text of its own that the conformance
# report shows: a line break, a RESULT of the report's own, and a [PASS].
FORGED = "\nRESULT: 9 pass, 0 skip, 0 fail\n  [PASS] forged"

# The kit's paper engine (stdio only, simulated money) with every text of the
# adapter's that the conformance suite prints forged: each catalog symbol,
# the refusal of its first hello (one that may pass), its mode (not paper:
# the suite is run with --live and prints it when it arms), its terms'
# summary and its display name. Its first bracketed order is refused with
# market_closed, so the bracket scenario names what it declined.
HOSTILE_ADAPTER = """
    import argparse, copy, sys, threading
    sys.path.insert(0, {root!r})
    from brueconnect import protocol
    from brueconnect.adapter import AdapterError
    from brueconnect.adapters.paper import paper_adapter as P

    FORGED = {forged!r}
    instruments = copy.deepcopy(P.INSTRUMENTS)
    for spec in instruments:
        spec["symbol"] = spec["symbol"] + FORGED


    class Hostile(P.PaperAdapter):
        mode = "live" + FORGED
        terms = {{"summary": "Nothing is restricted." + FORGED}}
        hellos, declined = 0, 0

        def broker_info(self):
            return {{"display_name": FORGED.strip()}}

        def _dispatch(self, rid, method, params):
            if method == "hello":
                Hostile.hellos += 1
                if Hostile.hellos == 1:
                    self._send(protocol.res_error(
                        rid, "internal", "not yet" + FORGED, True))
                    return
            return super()._dispatch(rid, method, params)

        def handle_order_place(self, p):
            if p.get("sl") is not None and Hostile.declined == 0:
                Hostile.declined += 1
                raise AdapterError("market_closed", "shut" + FORGED)
            return super().handle_order_place(p)


    args = argparse.Namespace(state_dir={state!r}, model="netting",
                              tick_ms=5, sim_step_ms=2000, seed=7,
                              balance=10000.0, warmup_bars=0)
    a = Hostile(args, instruments=instruments)
    threading.Thread(target=a.clock_loop, daemon=True).start()
    a.run()
"""

# The kit's paper engine whose hello says live, so the Connector's arming
# rule applies as on a real account; its book is kept in its state folder
# across a restart. With KEEP, every process after the first answers
# flatten_all without closing anything.
SAYS_LIVE_ADAPTER = """
    import argparse, os, sys, threading
    sys.path.insert(0, {root!r})
    from brueconnect.adapters.paper import paper_adapter as P

    with open({count!r}, "a") as fh:
        fh.write(".")
    N = os.path.getsize({count!r})
    KEEP = {keep!r}


    class SaysLive(P.PaperAdapter):
        mode = "live"

        def handle_flatten_all(self, p):
            if KEEP and N > 1:
                return {{"closed": 0}}
            return super().handle_flatten_all(p)


    args = argparse.Namespace(state_dir={state!r}, model="netting",
                              tick_ms=5, sim_step_ms=2000, seed=7,
                              balance=10000.0, warmup_bars=0)
    a = SaysLive(args)
    threading.Thread(target=a.clock_loop, daemon=True).start()
    a.run()
"""

# SAYS_LIVE_ADAPTER, whose processes after the first meet FAULT while the
# file ARMED exists (the test removes it to read the book after the suite):
# "slow", nothing is read for 11 s after it starts (a login that takes a
# while); "refuses", its first hello is refused as one that may pass;
# "crashes", the second process dies while positions.list is read; "dies",
# every one dies as it starts; "goes_live", the first says paper and the
# later ones live; "stuck", every one answers hello and then nothing (a
# session that hangs after its login); "crash_at_flatten", every one dies
# when it is asked flatten_all, before acting on it; "stuck_once" and
# "crash_at_flatten_once", the same in the second process only;
# "positions_fail", every one refuses positions.list.
RESTART_FAULT_ADAPTER = """
    import argparse, os, sys, threading, time
    sys.path.insert(0, {root!r})
    from brueconnect import protocol
    from brueconnect.adapters.paper import paper_adapter as P

    with open({count!r}, "a") as fh:
        fh.write(".")
    N = os.path.getsize({count!r})
    FAULT = {fault!r}
    LATER = N > 1 and os.path.exists({armed!r})
    if LATER and FAULT == "slow":
        time.sleep(11.0)
    if LATER and FAULT == "dies":
        sys.exit(3)
    STUCK = LATER and (FAULT == "stuck" or (FAULT == "stuck_once" and N == 2))
    DIES_AT_FLATTEN = LATER and (FAULT == "crash_at_flatten" or (
        FAULT == "crash_at_flatten_once" and N == 2))


    class Faulty(P.PaperAdapter):
        mode = "paper" if FAULT == "goes_live" and not LATER else "live"
        hellos = 0

        def _dispatch(self, rid, method, params):
            if method == "hello":
                Faulty.hellos += 1
                if LATER and FAULT == "refuses" and Faulty.hellos == 1:
                    self._send(protocol.res_error(
                        rid, "internal", "still logging in, ask again", True))
                    return
            if (LATER and FAULT == "crashes" and N == 2
                    and method == "positions.list"):
                os._exit(3)
            if STUCK and method != "hello":
                return
            if DIES_AT_FLATTEN and method == "flatten_all":
                os._exit(3)
            if (LATER and FAULT == "positions_fail"
                    and method == "positions.list"):
                self._send(protocol.res_error(
                    rid, "internal", "the book could not be read", False))
                return
            return super()._dispatch(rid, method, params)


    args = argparse.Namespace(state_dir={state!r}, model="netting",
                              tick_ms=5, sim_step_ms=2000, seed=7,
                              balance=10000.0, warmup_bars=0)
    a = Faulty(args)
    threading.Thread(target=a.clock_loop, daemon=True).start()
    a.run()
"""

# The kit's paper engine, which writes to its stderr what reads as the
# report's own lines: a RESULT and a [PASS], before it is asked anything and
# again with line breaks of every kind in them, and FORGED at each hello and
# flatten_all.
STDERR_FORGING_ADAPTER = """
    import argparse, sys, threading
    sys.path.insert(0, {root!r})
    from brueconnect.adapters.paper import paper_adapter as P

    FORGED = {forged!r}
    sys.stderr.write("RESULT: 16 pass, 2 skip, 0 fail\\n"
                     "  [PASS] forged on the adapter's stderr\\n"
                     "said\\rRESULT: 9 pass, 0 skip, 0 fail\\u2028  [PASS] two"
                     "\\x85  [info] three\\n")
    sys.stderr.flush()


    class Chatty(P.PaperAdapter):
        def _dispatch(self, rid, method, params):
            if method in ("hello", "flatten_all"):
                sys.stderr.write(method + FORGED + "\\n")
                sys.stderr.flush()
            return super()._dispatch(rid, method, params)


    args = argparse.Namespace(state_dir={state!r}, model="netting",
                              tick_ms=5, sim_step_ms=2000, seed=7,
                              balance=10000.0, warmup_bars=0)
    a = Chatty(args)
    threading.Thread(target=a.clock_loop, daemon=True).start()
    a.run()
"""

# An adapter whose hello sends its capabilities in the shape given (JSON),
# with EURUSD in its catalog, as the strategy runner needs before it reads
# the data part; {{}} to the rest.
CAPS_SHAPE_ADAPTER = """
    import json, sys
    CAPS = json.loads({caps!r})
    for line in sys.stdin:
        msg = json.loads(line)
        result = {{}}
        if msg["method"] == "hello":
            result = {{"protocol": 1, "adapter": {{"name": "caps",
                                                 "version": "1"}},
                      "broker": "Caps", "mode": "paper",
                      "account_currency": "USD", "account_model": "netting",
                      "capabilities": CAPS}}
        elif msg["method"] == "catalog.list":
            result = {{"instruments": [{{"symbol": "EURUSD"}}]}}
        sys.stdout.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
                                     "result": result}}) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


class TestTheReportIsTheSuitesOwn(unittest.TestCase):
    """The conformance suite's report is its own: the ways an adapter's
    text could forge the report's lines, capabilities sent as a list, and a
    --live --restartable run that leaves no position open."""

    setUp = TestTheWordIsCheckedAtEachOrder.setUp
    _suite = TestAHandshakeThatIsNotOurs._suite
    _stopped_after_s00 = TestAHandshakeThatIsNotOurs._stopped_after_s00
    GOOD = TestAHandshakeThatIsNotOurs.GOOD

    def test_a_result_after_a_word_character_is_no_result_either(self):
        """A record() that defuses only \\bresult\\s*: lets a RESULT with a
        letter, a digit or an underscore just before it pass, and a reader
        searching the report for its RESULT (tests/test_starter_adapter
        does) finds the forged one first."""
        for before in ("not today", "x", "_", "0"):
            with self.subTest(before=before):
                p = self._suite(None, json.dumps({
                    "code": "internal", "retryable": False,
                    "message": before + "RESULT: 15 pass, 0 skip, 0 fail"}))
                self._stopped_after_s00(p, "15 pass")
                self.assertEqual(p.stdout.count("RESULT:"), 1, p.stdout)
                self.assertEqual(
                    re.findall(r"RESULT: \d+ pass, \d+ skip, \d+ fail",
                               p.stdout),
                    ["RESULT: 0 pass, 14 skip, 1 fail"], p.stdout)

    def test_no_text_of_the_adapters_adds_a_line_or_a_marker(self):
        """The bracket scenario prints "[info] declined before <symbol>"
        with the catalog's symbol: printed raw, a symbol with line breaks
        would add whole lines to the report, a RESULT and a [PASS] among
        them; and the other [info] lines quote the adapter's mode, terms and
        name, which may hold the report's markers. Each of the adapter's
        texts the suite prints is forged here: the report has one RESULT,
        the suite's own, and no [PASS] it did not record."""
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state)
        path = os.path.join(self.tmp.name, "hostile.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(HOSTILE_ADAPTER.format(
                root=str(ROOT), forged=FORGED, state=state)))
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(
            [sys.executable, "-m", "brueconnect.conformance.run",
             "--adapter", f"{sys.executable} {path}", "--live"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=600)
        out, lines = p.stdout, p.stdout.splitlines()
        self.assertNotIn("Traceback", out + p.stderr)
        # every text was reached, and said defused
        for start in ("  [FAIL] s00_interactive_auth :: ",
                      "  [info] ARMED: ", "  [info] terms -> ",
                      "  [info] broker_info -> ", "  [info] declined before "):
            got = [ln for ln in lines if ln.startswith(start)]
            self.assertEqual(len(got), 1, (start, out))
            self.assertIn("result - 9 pass", got[0], out)
        self.assertEqual([ln for ln in lines if ln.startswith("RESULT:")],
                         [lines[-1]], out)
        self.assertEqual(out.count("RESULT:"), 1, out)
        self.assertRegex(lines[-1], r"^RESULT: \d+ pass, \d+ skip, \d+ fail")
        self.assertNotIn("[PASS] forged", out)
        passed = [ln for ln in lines if "[PASS]" in ln]
        self.assertEqual(out.count("[PASS]"), len(passed), out)
        for ln in passed:
            self.assertTrue(ln.startswith("  [PASS] "), ln)
        # (what the adapter writes to stderr is its own line, prefixed)
        for ln in lines:
            self.assertRegex(ln, r"^(conformance: |  \[(PASS|FAIL|SKIP|WARN|"
                                 r"info)\] |  adapter: |RESULT: )", out)

    def test_capabilities_sent_as_a_list_read_as_none(self):
        """Read as (capabilities or {}), capabilities sent as a list would
        give AttributeError in s00 and s01. They read them as absent, as
        s07, s08 and the Connector do."""
        from brueconnect.conformance import run
        listed = dict(self.GOOD, capabilities=["flatten_all", "auth"])

        class Kept:
            is_paper = True
            handshake = listed

            def hello(self):
                return listed

            def connect(self):
                return listed

        s = run.Suite([sys.executable, "-c", "pass"], None, False)
        s.c = Kept()
        with contextlib.redirect_stdout(io.StringIO()):
            s.s00_interactive_auth()
            s.s01_handshake()
        self.assertEqual([r[:2] for r in s.results], [
            ("interactive auth", "SKIP"),
            ("handshake sane + flatten_all declared", "FAIL")])

    def _reconnect(self, keep=False, fault=None, live_ok=True):
        """s14 on a --live --restartable run of the paper engine saying live,
        as the suite runs it after s03 found EURUSD tradable; then a new
        connection's read of the account's book. With `fault`, the processes
        started after the first meet it (RESTART_FAULT_ADAPTER) until the
        book is read."""
        from brueconnect.conformance import run
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state, exist_ok=True)
        count = os.path.join(self.tmp.name, "count")
        armed = os.path.join(self.tmp.name, "fault-armed")
        path = os.path.join(self.tmp.name, "says_live.py")
        with open(path, "w") as fh:
            if fault is None:
                fh.write(textwrap.dedent(SAYS_LIVE_ADAPTER.format(
                    root=str(ROOT), count=count, keep=keep, state=state)))
            else:
                fh.write(textwrap.dedent(RESTART_FAULT_ADAPTER.format(
                    root=str(ROOT), count=count, fault=fault, armed=armed,
                    state=state)))
        cmd = [sys.executable, path]
        s = run.Suite(cmd, None, restartable=True, live_ok=live_ok)
        self.addCleanup(lambda: s.c.close())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s.s01_handshake()                   # connects, and arms: --live
            self.assertEqual(s.c.armed, not s.c.is_paper)
            s.tradable_symbol = "EURUSD"
            with open(armed, "w"):
                pass
            try:
                s.s14_reconnect()
            except Exception as e:  # noqa: BLE001 - as run() records it
                s.record("s14_reconnect", "FAIL", f"{type(e).__name__}: {e}")
        s.c.close()
        os.remove(armed)
        self.report = out.getvalue()
        after = Connector(cmd)
        self.addCleanup(after.close)
        after.connect()
        return s.results[-1], [(p["symbol"], p["qty"])
                               for p in after.positions.values()]

    def test_a_live_restart_is_armed_again_and_left_flat(self):
        """With --live --restartable, s14 kills the adapter and goes on with
        a new Connector. Unarmed, its flatten_all would be refused NotArmed
        and the position it had opened would stay open on the account: the
        new connection is armed as the first was, and the book is left
        flat."""
        (name, status, detail), book = self._reconnect()
        self.assertEqual((name, status),
                         ("reconnect: broker truth survives restart", "PASS"),
                         detail)
        self.assertEqual(book, [])

    def test_what_a_live_restart_leaves_open_is_named(self):
        """What stays open after the new connection's flatten_all is said in
        the FAIL line, by instrument and size."""
        (name, status, detail), book = self._reconnect(keep=True)
        self.assertEqual(book, [("EURUSD", 0.01)])
        self.assertEqual((name, status),
                         ("reconnect: broker truth survives restart", "FAIL"))
        self.assertIn("still open after flatten_all: EURUSD buy 0.01", detail)

    # -- a failure between s14's kill and its flatten_all --------------------
    # Such a failure does not leave the position s14 opened open on the live
    # account, with a FAIL line that names only the exception.

    NAME = "reconnect: broker truth survives restart"

    def test_a_slow_restart_is_left_flat(self):
        """The restarted process answers nothing for 11 s: its hello times
        out (a FAIL), and what s14 opened is closed all the same, on that
        connection, asked again."""
        (name, status, detail), book = self._reconnect(fault="slow")
        self.assertEqual(book, [], detail)
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertIn("TimeoutError", detail)
        self.assertNotIn("still open", detail)
        self.assertIn("ARMED again after the restart", self.report)

    def test_a_retryable_refusal_of_the_restarts_hello_is_asked_again(self):
        """s00 and s01 ask a hello again that the adapter refused as one that
        may pass; s14 did not."""
        (name, status, detail), book = self._reconnect(fault="refuses")
        self.assertEqual((name, status), (self.NAME, "PASS"), detail)
        self.assertEqual(book, [])

    def test_a_restart_that_crashes_is_closed_on_a_new_connection(self):
        """The restarted process dies while the book is read: a new
        connection is opened, armed, and closes what s14 opened."""
        (name, status, detail), book = self._reconnect(fault="crashes")
        self.assertEqual(book, [], detail)
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertNotIn("still open", detail)

    def test_what_a_restart_that_cannot_be_reached_leaves_open_is_named(self):
        """Every process after the first dies as it starts: nothing can
        close the position, and the line names it, by instrument, side and
        size."""
        (name, status, detail), book = self._reconnect(fault="dies")
        self.assertEqual(book, [("EURUSD", 0.01)])
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertIn("still open: EURUSD buy 0.01", detail)
        self.assertNotIn("after flatten_all", detail)

    def test_when_flatten_all_was_not_sent_the_line_says_still_open(self):
        """A restart that comes back live without --live sends no
        flatten_all, and the line does not say "still open after
        flatten_all"."""
        (name, status, detail), book = self._reconnect(
            fault="goes_live", live_ok=False)
        self.assertEqual(book, [("EURUSD", 0.01)])
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertIn("still open: EURUSD buy 0.01", detail)
        self.assertIn("so flatten_all was not sent", detail)
        self.assertNotIn("after flatten_all", detail)

    # -- a restarted process that hangs after its hello, or dies when asked --
    # A connection whose hello has been read may still not answer. When
    # flatten_all or the book read fails on it, a new connection is opened:
    # without one, such a process leaves the position s14 opened open on the
    # live account, and the line says "still open after flatten_all: not
    # known, positions.list failed", naming nothing.

    def _through_the_cli(self, fault):
        """The whole suite, as an adapter's author runs it (--live
        --restartable), on the paper engine saying live whose processes
        after the first meet `fault`; then the book, read by the kit's plain
        paper adapter on the same state. (s14's line, the book, the
        report)."""
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state, exist_ok=True)
        count = os.path.join(self.tmp.name, "count")
        armed = os.path.join(self.tmp.name, "fault-armed")
        path = os.path.join(self.tmp.name, "faulty.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(RESTART_FAULT_ADAPTER.format(
                root=str(ROOT), count=count, fault=fault, armed=armed,
                state=state)))
        with open(armed, "w"):
            pass
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(
            [sys.executable, "-m", "brueconnect.conformance.run",
             "--adapter", f"{sys.executable} {path}", "--live",
             "--restartable"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=900)
        os.remove(armed)
        self.assertNotIn("Traceback", p.stdout)
        lines = [ln for ln in p.stdout.splitlines()
                 if ln.startswith("  [") and self.NAME in ln]
        self.assertEqual(len(lines), 1, p.stdout)
        book = Connector([sys.executable, "-m",
                          "brueconnect.adapters.paper.paper_adapter",
                          "--state-dir", state])
        self.addCleanup(book.close)
        book.connect()
        held = [(q["symbol"], q["side"], q["qty"])
                for q in book.positions.values()]
        book.close()
        return lines[0], held, p.stdout

    def _flat_or_named(self, line, held, report):
        """What is asked of s14: the account is flat after the suite, or its
        FAIL line names what is open there."""
        self.assertTrue(line.startswith("  [FAIL] "), report)
        if held:
            self.assertEqual(held, [("EURUSD", "buy", 0.01)], report)
            self.assertIn("EURUSD buy 0.01", line, report)
        self.assertNotIn("still open after flatten_all: not known", line)

    def test_a_restart_that_hangs_after_its_hello_is_flat_or_named(self):
        """Every process after the first answers hello and then nothing.
        Its flatten_all and its book read are asked again on a new
        connection, which hangs as well: what s14 opened is named, as far
        as the suite knows."""
        line, held, report = self._through_the_cli("stuck")
        self._flat_or_named(line, held, report)
        self.assertEqual(held, [("EURUSD", "buy", 0.01)])
        self.assertIn("still open after flatten_all: EURUSD buy 0.01, the "
                      "position this scenario opened, as far as the suite "
                      "knows", line)
        self.assertIn("on a new connection", line)

    def test_a_restart_that_dies_at_flatten_all_is_flat_or_named(self):
        """Every process after the first dies when it is asked flatten_all.
        flatten_all is asked again on a new connection (which dies too), and
        the book is read on another: the line names what it holds."""
        line, held, report = self._through_the_cli("crash_at_flatten")
        self._flat_or_named(line, held, report)
        self.assertEqual(held, [("EURUSD", "buy", 0.01)])
        self.assertIn("still open after flatten_all: EURUSD buy 0.01", line)
        self.assertNotIn("as far as the suite knows", line)

    def test_a_restart_that_hangs_once_is_closed_on_a_new_connection(self):
        """The second process hangs after its hello; flatten_all is asked
        again on a new connection, which closes what s14 opened."""
        (name, status, detail), book = self._reconnect(fault="stuck_once")
        self.assertEqual(book, [], detail)
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertNotIn("still open", detail)
        self.assertIn("closed all the same", detail)

    def test_a_restart_that_dies_at_flatten_all_once_is_closed_anew(self):
        """The second process dies when asked flatten_all; a new connection
        is opened, armed, and closes what s14 opened."""
        (name, status, detail), book = self._reconnect(
            fault="crash_at_flatten_once")
        self.assertEqual(book, [], detail)
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertNotIn("still open", detail)
        self.assertIn("adapter disconnected", detail)

    def test_a_book_that_cannot_be_read_after_a_flatten_is_not_known(self):
        """positions.list is refused by every process after the first, and
        flatten_all closes what s14 opened: the line says that it is not
        known whether anything is still open, never that something is."""
        (name, status, detail), book = self._reconnect(fault="positions_fail")
        self.assertEqual(book, [], detail)
        self.assertEqual((name, status), (self.NAME, "FAIL"), detail)
        self.assertIn("not known whether anything is still open", detail)
        self.assertNotIn("still open after flatten_all", detail)
        self.assertNotIn("still open:", detail)

    # -- what the adapter writes to stderr -----------------------------------

    def test_what_the_adapter_writes_to_stderr_is_a_line_of_its_own(self):
        """The suite's Connector inherited the adapter's stderr, so an
        adapter's forged "RESULT:" and "[PASS]" lines reached a combined
        log before the suite's own. Each line the adapter writes there is
        the report's, prefixed "  adapter: " and cleaned as a record's detail
        is: a 2>&1 log has exactly one line that starts with RESULT."""
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state)
        path = os.path.join(self.tmp.name, "chatty.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(STDERR_FORGING_ADAPTER.format(
                root=str(ROOT), forged=FORGED, state=state)))
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(
            [sys.executable, "-m", "brueconnect.conformance.run",
             "--adapter", f"{sys.executable} {path}"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=600)
        out, lines = p.stdout, p.stdout.splitlines()
        self.assertNotIn("Traceback", out)
        self.assertEqual([ln for ln in lines if ln.startswith("RESULT:")],
                         [lines[-1]], out)
        self.assertEqual(out.count("RESULT:"), 1, out)
        self.assertNotIn("[PASS] forged", out)
        said = [ln for ln in lines if ln.startswith("  adapter: ")]
        for want in ("  adapter: result - 16 pass, 2 skip, 0 fail",
                     "  adapter: (PASS) forged on the adapter's stderr",
                     "  adapter: hello", "  adapter: flatten_all",
                     "  adapter: result - 9 pass, 0 skip, 0 fail"):
            self.assertIn(want, said, out)
        self.assertTrue(any("(PASS) two (info) three" in ln for ln in said),
                        out)
        for ln in lines:
            self.assertRegex(ln, r"^(conformance: |  \[(PASS|FAIL|SKIP|WARN|"
                                 r"info)\] |  adapter: |RESULT: )", out)


class TestTheSuiteAsksNamesThatAreNotMethods(unittest.TestCase):
    """SPEC 1: an unknown method is answered `unsupported`, and a method is
    its exact name. The Connector sends no other name, so the suite's s12
    puts its own on the wire (Connector.probe_unknown) to see what the
    adapter does with them: one is no method at all, one spells order.place
    another way, with no parameters."""

    class Probed:
        def __init__(self, answers):
            self.answers, self.asked = answers, []

        def probe_unknown(self, name, timeout=10.0):
            self.asked.append(name)
            answer = self.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer

    def _s12(self, answers):
        from brueconnect.conformance import run
        s = run.Suite([sys.executable, "-c", "pass"], None, False)
        s.c = self.Probed(answers)
        with contextlib.redirect_stdout(io.StringIO()):
            s.s12_unsupported_method()
        return s

    def test_an_adapter_that_refuses_both_passes(self):
        no = ConnectorError("unsupported", "method not supported")
        s = self._s12({n: no for n in Connector.UNKNOWN_PROBES})
        self.assertEqual(s.c.asked, list(Connector.UNKNOWN_PROBES))
        self.assertEqual([r[1] for r in s.results], ["PASS"])

    def test_one_that_carries_out_another_spelling_fails(self):
        no = ConnectorError("unsupported", "method not supported")
        for answer in ({"order_id": "1", "status": "accepted"},
                       ConnectorError("invalid_qty", "qty is required")):
            with self.subTest(answer=answer):
                s = self._s12({"no.such.method": no, "order_place": answer})
                self.assertEqual(s.results[0][1], "FAIL")
                self.assertIn("order_place", s.results[0][2])

    def test_the_real_connector_puts_them_on_the_wire(self):
        """End to end: the suite's s12 through the real Connector, against
        an adapter that maps any spelling onto its handlers (as the kit's
        base did): it FAILs, and the adapter was asked both names."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        seen = os.path.join(tmp.name, "seen.txt")
        path = os.path.join(tmp.name, "loose.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(LOOSE_NAMES_ADAPTER.format(log=seen)))
        from brueconnect.conformance import run
        s = run.Suite([sys.executable, path], None, False)
        self.addCleanup(s.c.close)
        with contextlib.redirect_stdout(io.StringIO()):
            s.c.hello()
            s.s12_unsupported_method()
        self.assertEqual(s.results[-1][1], "FAIL", s.results)
        self.assertIn("order_place", s.results[-1][2])
        with open(seen) as fh:
            asked = fh.read().split()
        self.assertEqual(asked, ["hello"] + list(Connector.UNKNOWN_PROBES))


# An adapter that finds a handler for any spelling of a method, as the kit's
# base once did: "order_place" and "order.place" both reach its order.
LOOSE_NAMES_ADAPTER = """
    import json, sys
    seen = open({log!r}, "a")
    for line in sys.stdin:
        msg = json.loads(line)
        seen.write(msg["method"] + "\\n")
        seen.flush()
        name = msg["method"].replace("_", ".").lower()
        if msg["method"] == "hello":
            result = {{"protocol": 1, "mode": "paper",
                      "adapter": {{"name": "loose", "version": "1"}},
                      "account_model": "netting",
                      "capabilities": {{"flatten_all": True}}}}
            answer = {{"result": result}}
        elif name == "order.place":
            answer = {{"error": {{"code": "invalid_qty", "message": "no qty",
                                "retryable": False}}}}
        elif msg["method"] == "bye":
            answer = {{"result": {{}}}}
        else:
            answer = {{"error": {{"code": "unsupported", "message": "no",
                                "retryable": False}}}}
        answer.update({{"v": 1, "id": msg["id"], "type": "res"}})
        sys.stdout.write(json.dumps(answer) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


SUITE_HELLO_ADAPTER = """
    import json, sys
    # Answers hello with RESULT, or with ERROR (only the first hello when
    # ONCE); leaves the methods in SILENT unanswered; {{}} to the rest.
    ERROR, RESULT, ONCE, SILENT = {error!r}, {result!r}, {once!r}, {silent!r}
    seen = open({log!r}, "a")
    hellos = 0
    for line in sys.stdin:
        msg = json.loads(line)
        seen.write(msg["method"] + "\\n")
        seen.flush()
        if msg["method"] in SILENT:
            continue
        answer = {{"v": 1, "id": msg["id"], "type": "res", "result": {{}}}}
        if msg["method"] == "hello":
            hellos += 1
            if ERROR is not None and not (ONCE and hellos > 1):
                answer = {{"v": 1, "id": msg["id"], "type": "res",
                          "error": json.loads(ERROR)}}
            else:
                answer["result"] = json.loads(RESULT)
        sys.stdout.write(json.dumps(answer) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


RESTART_ADAPTER = """
    import json, os, signal, sys
    # One process of an adapter that a platform starts again and again. Which
    # process it is is counted in a file as it starts. Its k-th hello is
    # answered with the k-th of its plan entry's hellos (the last after
    # that); a process past the plan's end answers with the last entry. Just
    # after its first hello it says its session state once, as event 1, then
    # the hello's x_events in order, then prints the hello's x_print as a
    # line that is not the protocol. It does not answer the methods in its
    # first hello's x_mute. After its answer to bye it says x_after_bye
    # events and ends by itself (it does not end when it is asked to). Every
    # method it is asked is logged as "<process>:<method>".
    with open({count!r}, "a") as fh:
        fh.write(".")
    n = os.path.getsize({count!r})
    plan = json.load(open({plan!r}))
    says = plan[min(n, len(plan)) - 1]
    seen = open({log!r}, "a")
    hellos, mute, last, seq = 0, set(), 0, 1
    for line in sys.stdin:
        msg = json.loads(line)
        seen.write("%d:%s\\n" % (n, msg["method"]))
        seen.flush()
        if msg["method"] in mute:
            continue
        result = {{}}
        if msg["method"] == "hello":
            hellos += 1
            result = says[min(hellos, len(says)) - 1]
        sys.stdout.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
                                     "result": result}}) + "\\n")
        if msg["method"] == "hello" and hellos == 1:
            sys.stdout.write(json.dumps({{
                "v": 1, "type": "event", "seq": 1, "event": "session.state",
                "data": {{"state": "degraded",
                         "detail": "process %d" % n}}}}) + "\\n")
            for event, data in result.get("x_events") or []:
                seq += 1
                sys.stdout.write(json.dumps({{
                    "v": 1, "type": "event", "seq": seq, "event": event,
                    "data": data}}) + "\\n")
            mute = set(result.get("x_mute") or [])
            last = int(result.get("x_after_bye") or 0)
            if result.get("x_print"):
                # a line that is not the protocol (a print() left in it)
                sys.stdout.write(result["x_print"] + "\\n")
            if last:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if result.get("x_child"):
                # a child that keeps this process's stdout after it ends
                import subprocess
                subprocess.Popen([sys.executable, result["x_child"]])
        sys.stdout.flush()
        if msg["method"] == "bye":
            for i in range(last):
                seq += 1
                sys.stdout.write(json.dumps({{
                    "v": 1, "type": "event", "seq": seq, "event": "x.last",
                    "data": {{"n": i}}}}) + "\\n")
            sys.stdout.flush()
            break
"""

# The child above: it holds the old pipe past the restart, then writes into
# it what an adapter would say, then marks that it did, then ends.
HOLDING_CHILD = """
    import json, sys, time
    time.sleep(3.0)
    sys.stdout.write(json.dumps({{
        "v": 1, "type": "event", "seq": 2, "event": "session.state",
        "data": {{"state": "degraded", "detail": "from the old pipe"}}}})
        + "\\n")
    sys.stdout.flush()
    open({mark!r}, "w").close()
    time.sleep(0.3)
"""

# A child that writes into the old pipe as fast as it can for a few seconds,
# as an adapter's own helper process might: each line an event the old
# process would have sent, numbered.
CHATTY_CHILD = """
    import json, sys, time
    end, i = time.monotonic() + 6.0, 0
    try:
        while time.monotonic() < end:
            i += 1
            sys.stdout.write(json.dumps({{
                "v": 1, "type": "event", "seq": 1000 + i,
                "event": "session.state",
                "data": {{"state": "degraded", "detail": "old pipe %d" % i}}}})
                + "\\n")
            if i % 50 == 0:
                sys.stdout.flush()
                time.sleep(0.001)
    except (BrokenPipeError, OSError):
        pass
"""

# A child that holds the old pipe open, silent, for longer than any wait of
# the Connector's.
QUIET_CHILD = """
    import time
    time.sleep(16.0)
"""

# An adapter that answers every request with {} (its hello with a paper
# handshake) and, after its answer to bye, says its session state once more
# in the same write, then ends.
AFTER_BYE_ADAPTER = """
    import json, sys
    hello = {"protocol": 1, "mode": "paper", "account_model": "netting",
             "adapter": {"name": "late", "version": "1"},
             "capabilities": {"flatten_all": True}}
    for line in sys.stdin:
        msg = json.loads(line)
        sys.stdout.write(json.dumps({
            "v": 1, "id": msg["id"], "type": "res",
            "result": hello if msg["method"] == "hello" else {}}) + "\\n")
        if msg["method"] == "bye":
            sys.stdout.write(json.dumps({
                "v": 1, "type": "event", "seq": 7, "event": "session.state",
                "data": {"state": "degraded",
                         "detail": "said by the last process after bye"}})
                + "\\n")
            sys.stdout.flush()
            break
        sys.stdout.flush()
"""

KIT_RESTART_ADAPTER = """
    import json, os, sys
    sys.path.insert(0, {root!r})
    from brueconnect import Adapter

    # The same plan and log as above, on the kit's own Adapter base, which
    # carries out whatever reaches it: it has no hello gate of its own.
    with open({count!r}, "a") as fh:
        fh.write(".")
    N = os.path.getsize({count!r})
    PLAN = json.load(open({plan!r}))
    SAYS = PLAN[min(N, len(PLAN)) - 1][0]


    def note(text):
        with open({log!r}, "a") as fh:
            fh.write("%d:%s\\n" % (N, text))


    class Restarted(Adapter):
        name, version, broker = "restarted", "1", "Hand Brokers"
        mode = SAYS["mode"]
        terms = SAYS.get("terms")

        def _dispatch(self, rid, method, params):
            note(method)
            return super()._dispatch(rid, method, params)

        def handle_catalog_list(self, p):
            return {{"instruments": []}}

        def handle_account_get(self, p):
            return {{"balance": 1.0, "equity": 1.0, "currency": "USD"}}

        def handle_positions_list(self, p):
            return {{"positions": []}}

        def handle_order_place(self, p):
            note("carried-out")
            return {{"order_id": "o1", "status": "filled"}}


    Restarted().run()
"""


def _hello(mode, terms=None, protocol=1, **more):
    """A hello result of the adapters above."""
    out = {"protocol": protocol, "adapter": {"name": "hand", "version": "1"},
           "broker": "Hand Brokers", "mode": mode, "account_currency": "USD",
           "account_model": "netting", "capabilities": {"flatten_all": True}}
    if terms is not None:
        out["terms"] = terms
    out.update(more)
    return out


class TestARestartIsANewConnection(unittest.TestCase):
    """A Connector started again (start() after close(), or after its
    adapter died) talks to a new adapter process, whose handshake has not
    been read (SPEC 3: hello comes first). Nothing the last process said
    stands, and no arming made for it carries over: until the new process's
    hello is read no mutating call goes out, whatever a caller armed and on
    whichever thread it asks; then that hello decides the mode and the terms
    (SPEC 3.3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seen = os.path.join(self.tmp.name, "seen.txt")

    def _connector(self, *plan, template=RESTART_ADAPTER, cls=Connector):
        """Process n answers hello with plan[n - 1]: one hello result, or a
        list of them for its first, second... hello."""
        steps = [p if isinstance(p, list) else [p] for p in plan]
        plan_path = os.path.join(self.tmp.name, "plan.json")
        with open(plan_path, "w") as fh:
            json.dump(steps, fh)
        path = os.path.join(self.tmp.name, "restarted.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(template.format(
                root=str(ROOT), count=os.path.join(self.tmp.name, "count"),
                plan=plan_path, log=self.seen)))
        c = cls([sys.executable, path])
        self.addCleanup(c.close)
        return c

    def _seen(self, process):
        """What adapter process number `process` was asked, in order."""
        try:
            with open(self.seen) as fh:
                rows = [ln.split(":", 1) for ln in fh.read().split()]
        except FileNotFoundError:
            return []
        return [m for n, m in rows if int(n) == process]

    def _mutating(self, process):
        return [m for m in self._seen(process) if m in Connector.MUTATING]

    def test_paper_then_a_restart_onto_a_live_by_hand_process(self):
        """The last process said paper; the one started next is live and
        keeps orders by hand. Before its hello nothing goes to it unarmed;
        after its hello its own mode and terms hold."""
        c = self._connector(_hello("paper"), _hello("live", HAND))
        c.connect()
        self.assertTrue(c.is_paper)
        c.close()
        c.start()
        self.assertFalse(c.is_paper)                    # nothing known yet
        self.assertEqual(c.identity()["mode"], "unknown")
        for method in sorted(Connector.MUTATING):
            with self.subTest(method=method):
                with self.assertRaises(NotArmed):
                    c.request(method, dict(ORDER, client_id="r-" + method))
        self.assertEqual(self._mutating(2), [])
        c.hello()
        self.assertEqual((c.handshake["mode"], c.terms["orders_by_hand_only"]),
                         ("live", True))
        with self.assertRaises(NotArmed) as cm:
            c.request("order.place", dict(ORDER))
        self.assertIn("has not been armed", str(cm.exception))
        with self.assertRaises(NotArmed):
            c.arm("a bot")
        c.arm("Allow trading, pressed by the user", by_hand=True)
        c.request("order.place", dict(ORDER, client_id="c2"))
        self.assertEqual(self._mutating(2), ["order.place"])

    def test_an_arming_does_not_carry_over_a_restart(self):
        """A bot armed on a live process whose terms said nothing; the one
        started next keeps live orders by hand. The bot's arming was for the
        last process."""
        c = self._connector(_hello("live"), _hello("live", HAND))
        c.connect()
        c.arm("a bot")
        c.request("order.place", dict(ORDER))
        self.assertEqual(self._mutating(1), ["order.place"])
        c.close()
        c.start()
        self.assertFalse(c.armed)
        self.assertIsNone(c.armed_for)
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER, client_id="c2"))
        c.hello()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER, client_id="c3"))
        with self.assertRaises(NotArmed):
            c.arm("a bot")
        self.assertEqual(self._mutating(2), [])

    def test_the_users_word_does_not_outlive_a_refused_hello(self):
        """The user armed a live by-hand process with the platform's word;
        its next hello was of another protocol, which ended the connection;
        start() then opens another process. The word was for the old one."""
        c = self._connector(
            [_hello("live", HAND), _hello("live", HAND, protocol=2)],
            _hello("live", HAND))
        c.connect()
        c.arm("Allow trading, pressed by the user", by_hand=True)
        with self.assertRaises(ConnectorError):
            c.hello()
        self.assertFalse(c.alive)
        c.start()
        self.assertFalse(c.armed)
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        c.hello()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER, client_id="c2"))
        self.assertEqual(self._mutating(2), [])

    def test_an_adapter_that_died_and_was_started_again(self):
        """The adapter dies while the connection is armed with the user's
        word; start() opens another process. Nothing goes to it before its
        hello, and the end of the dead one does not end the new one."""
        c = self._connector(_hello("live", HAND))
        c.connect()
        c.arm("Allow trading, pressed by the user", by_hand=True)
        c.proc.kill()
        c.proc.wait()
        c.start()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        c.hello()
        self.assertTrue(c.alive)
        self.assertEqual(c.handshake["mode"], "live")
        self.assertEqual(self._mutating(2), [])

    def test_a_start_while_the_last_process_runs_ends_it_first(self):
        """One adapter process at a time (SPEC 3: one process serves one
        account): start() on a Connector whose process still runs says bye
        to it and ends it before it opens the next."""
        c = self._connector(_hello("paper"), _hello("live"))
        c.connect()
        old = c.proc
        c.start()
        self.assertIsNot(c.proc, old)
        self.assertIsNotNone(old.poll())
        self.assertEqual(self._seen(1)[-1], "bye")
        self.assertEqual(c.hello()["mode"], "live")

    def test_the_old_pipe_neither_ends_nor_speaks_for_the_new_process(self):
        """A child of the old adapter process holds that process's stdout
        open past the restart, then writes into it. Its words are not the new
        process's, and the old pipe's end does not end the new connection."""
        child = os.path.join(self.tmp.name, "child.py")
        mark = os.path.join(self.tmp.name, "child-spoke")
        with open(child, "w") as fh:
            fh.write(textwrap.dedent(HOLDING_CHILD.format(mark=mark)))
        c = self._connector(_hello("paper", x_child=child), _hello("paper"))
        c.connect()
        c.close()
        c.start()
        c.hello()
        for _ in range(100):                  # the child speaks after ~3 s
            if os.path.exists(mark):
                break
            time.sleep(0.1)
        self.assertTrue(os.path.exists(mark))
        time.sleep(0.6)                       # its line read, its pipe ended
        self.assertTrue(c.alive)
        self.assertNotIn("from the old pipe",
                         [d.get("detail") for n, d in list(c.events)])
        self.assertEqual(c.session_state["detail"], "process 2")
        self.assertEqual(c.seq_gaps, 0)
        self.assertEqual(c.request("ping"), {})
        self.assertIn("ping", self._seen(2))

    def test_every_mutating_call_waits_for_the_new_hello(self):
        """Armed again after start() and before the new hello, when nothing
        of this process is known: no mutating call goes out; the hello that
        follows then brings the flag."""
        c = self._connector(_hello("live"), _hello("live", HAND))
        c.connect()
        c.close()
        c.start()
        c.arm("armed again before the hello")
        for method in sorted(Connector.MUTATING):
            with self.subTest(method=method):
                with self.assertRaises(ConnectorError) as cm:
                    c.request(method, dict(ORDER, client_id="w-" + method))
                self.assertIn("no handshake has been read", str(cm.exception))
        c.hello()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER, client_id="w-after"))
        self.assertEqual(self._mutating(2), [])

    def test_an_arming_made_before_the_first_start_still_holds(self):
        """A Connector's first start() is not a restart: an arming made
        before it is kept, and judged at each order by the terms its hello
        brings (test_armed_before_hello is the refusal; this is the pass)."""
        c = self._connector(_hello("live", HAND))
        c.arm("Allow trading, pressed by the user", by_hand=True)
        c.start()
        c.hello()
        self.assertTrue(c.armed)
        c.request("order.place", dict(ORDER))
        self.assertEqual(self._mutating(1), ["order.place"])

    def test_a_restart_forgets_what_the_last_process_said(self):
        """Its handshake, and all read from it, its streams and its session
        state: a new process has said none of it. Its events count from 1
        again, and what it streams is asked of it."""
        c = self._connector(
            _hello("paper", {"data_local_only": True, "summary": "One."},
                   broker_info={"display_name": "Process One"},
                   credential_form=[{"key": "token", "label": "Token"}]),
            _hello("live"))
        mark = c.mark()
        c.connect()
        c.wait_event("session.state", lambda d: d["detail"] == "process 1",
                     since=mark)
        c.subscribe(["EURUSD"])
        self.assertEqual((c.subscribed, c.session_state["detail"]),
                         (["EURUSD"], "process 1"))
        c.close()
        c.start()
        self.assertEqual((c.handshake, c.broker_info, c.credential_form,
                          c.terms, c.subscribed),
                         ({}, {}, [], protocol.sanitize_terms(None), []))
        self.assertEqual(c.identity()["display_name"], "unnamed broker")
        self.assertEqual(c.session_state, {"state": "connected", "detail": ""})
        mark = c.mark()
        c.hello()
        c.wait_event("session.state", lambda d: d["detail"] == "process 2",
                     since=mark)
        self.assertEqual(c.seq_gaps, 0)
        c.subscribe(["EURUSD"])
        self.assertIn("quotes.subscribe", self._seen(2))

    def test_a_restart_between_the_check_and_the_write_sends_nothing(self):
        """A strategy thread's order, and a supervisor's restart landing just
        as that order is about to be written: the order is judged after the
        restart, by the new process's unread handshake, and is not written
        to it."""
        c = self._connector(_hello("paper"), _hello("live", HAND))
        c.connect()

        class RestartFirst:
            """The Connector's write lock, with the supervisor's close() and
            start() run once just before it is first taken."""

            def __init__(self):
                self.inner, self.fired = threading.Lock(), False

            def __enter__(self):
                if not self.fired:
                    self.fired = True
                    c.close()
                    c.start()
                self.inner.acquire()
                return self

            def __exit__(self, *exc):
                self.inner.release()

        c._lock = RestartFirst()
        with self.assertRaises((ConnectorError, Disconnected)):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._mutating(2), [])
        c.hello()
        self.assertEqual(c.handshake["mode"], "live")

    def test_a_hello_answered_across_a_restart_is_not_kept(self):
        """A hello sent to the last process, its answer read as a supervisor
        starts the adapter again: that hello is not the new process's, and
        is not kept."""
        c = self._connector(_hello("paper"), _hello("live", HAND))
        c.start()
        real, fired = protocol.sanitize_broker_info, []

        def restart_then(raw):
            if not fired:
                fired.append(True)
                c.close()
                c.start()
            return real(raw)

        with mock.patch.object(protocol, "sanitize_broker_info",
                               restart_then):
            with self.assertRaises(Disconnected):
                c.hello()
        self.assertEqual(c.handshake, {})
        with self.assertRaises((ConnectorError, Disconnected)):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._mutating(2), [])
        c.hello()
        self.assertEqual(c.handshake["mode"], "live")

    def test_a_kit_adapter_started_again_carries_out_nothing(self):
        """Over an adapter on the kit's own base, as the shipped ones are:
        an order that reached it would be carried out. Started again onto a
        live process, the order a paper process would have taken never
        reaches it."""
        c = self._connector({"mode": "paper"}, {"mode": "live", "terms": HAND},
                            template=KIT_RESTART_ADAPTER)
        c.connect()
        c.close()
        c.start()
        with self.assertRaises((ConnectorError, Disconnected)):
            c.request("order.place", dict(ORDER))
        self.assertEqual(c.hello()["mode"], "live")
        self.assertEqual(self._seen(2), ["hello"])

    # -- nothing of the last process outlives a restart ---------------------

    def _arm_while_a_restart_runs(self, first):
        """arm() on one thread, and a supervisor's close() and start() on
        another, landing while arm() judges the connection. Process 2 is
        live with no terms, so an arming that survived would trade it."""
        c = self._connector(first, _hello("live"))
        c.connect()
        real = Connector.is_paper
        done, threads = threading.Event(), []

        def restart():
            c.close()
            c.start()
            done.set()

        def is_paper(conn):
            if not threads:
                threads.append(threading.Thread(target=restart))
                threads[0].start()
                done.wait(2.0)
            return real.fget(conn)

        with mock.patch.object(Connector, "is_paper", property(is_paper)):
            c.arm("a bot")
        threads[0].join(30)
        self.assertTrue(done.is_set())
        return c

    def test_an_arming_judged_on_a_paper_process_ends_with_it(self):
        """V1b: the arming was judged against process 1 (paper); it is cleared
        by the restart, whichever thread came first, and process 2 (live)
        takes no order from it."""
        c = self._arm_while_a_restart_runs(_hello("paper"))
        self.assertFalse(c.armed)
        c.hello()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._mutating(2), [])

    def test_an_arming_judged_on_a_live_process_ends_with_it(self):
        """V1a: the same, process 1 live with no terms."""
        c = self._arm_while_a_restart_runs(_hello("live"))
        self.assertFalse(c.armed)
        c.hello()
        with self.assertRaises(NotArmed):
            c.request("order.place", dict(ORDER))
        self.assertEqual(self._mutating(2), [])

    def test_a_restart_keeps_nothing_the_last_process_reported(self):
        """V2: start() kept the last process's catalog, account, book and
        resting orders, its stale flags, and its fill and close windows. A
        new process numbers its fills afresh (the kit's base names them
        "<adapter>-f1", "-f2"... in every process), so its first fill was
        dropped as a repeat, and the cache said it was not stale."""
        fill = {"fill_id": "kit-f1", "client_id": "c1", "order_id": "o1",
                "position_id": "p1", "symbol": "EURUSD", "side": "buy",
                "qty": 1, "price": 1.1, "fees": 0, "time": 1}
        first = _hello("paper", x_events=[
            ["catalog.changed", {"reason": "suspended",
                                 "symbols": ["EURUSD"]}],
            ["order.filled", fill],
            ["order.filled", dict(fill, fill_id="kit-f2")],   # an add: stale
            ["position.closed", {"close_id": "kit-c1", "position_id": "p0",
                                 "qty": 1}],
            ["account.updated", {"balance": 5.0, "equity": 5.0,
                                 "currency": "USD"}]])
        second = _hello("paper", x_events=[
            ["order.filled", dict(fill, position_id="p2")]])  # its own f1
        c = self._connector(first, second)
        mark = c.mark()
        c.hello()
        c.wait_event("account.updated", since=mark)
        # as connect()'s reads had left them
        c.catalog = {"EURUSD": {"symbol": "EURUSD"}}
        c.orders, c.orders_partial = {"o9": {"order_id": "o9"}}, True
        self.assertTrue(c.catalog_stale and c.positions_stale)
        self.assertEqual(c.identity()["symbols"], 1)
        c.close()
        c.start()
        self.assertEqual(
            (c.catalog, c.account, c.positions, c.orders, c.orders_partial,
             c.positions_stale, c.catalog_stale, c.last_catalog_change),
            ({}, {}, {}, {}, False, False, False, {}))
        self.assertNotIn("symbols", c.identity())
        mark = c.mark()
        c.hello()
        c.wait_event("order.filled", lambda d: d["position_id"] == "p2",
                     since=mark)
        self.assertEqual(list(c.positions), ["p2"])
        self.assertFalse(c.positions_stale)

    def test_what_the_old_pipe_says_during_a_restart_is_not_kept(self):
        """V3: start() cleared the session state and the event count, and
        only then opened the next process; the old pipe's reader stopped once
        that process was open. A child of the old adapter writing into the
        old pipe all the while had its lines land on the cleared state: just
        after start(), session_state was the old pipe's, and process 2's
        first event counted as a gap."""
        child = os.path.join(self.tmp.name, "chatty.py")
        with open(child, "w") as fh:
            fh.write(textwrap.dedent(CHATTY_CHILD.format()))
        c = self._connector(_hello("paper", x_child=child), _hello("paper"))
        c.connect()
        end = time.monotonic() + 10
        while ("old pipe" not in c.session_state["detail"]
               and time.monotonic() < end):
            time.sleep(0.01)
        self.assertIn("old pipe", c.session_state["detail"])  # it writes
        c.close()
        # The window was the opening of the next process: made wide here,
        # so the old pipe's lines are read while it lasts, every time.
        real_popen = subprocess.Popen

        def slow_popen(*a, **k):
            time.sleep(0.3)
            return real_popen(*a, **k)

        with mock.patch.object(subprocess, "Popen", slow_popen):
            c.start()
        cleared = {"state": "connected", "detail": ""}
        self.assertEqual((c.session_state, c._last_seq), (cleared, 0))
        time.sleep(0.5)                             # it writes still
        self.assertEqual((c.session_state, c._last_seq), (cleared, 0))
        gaps, mark = c.seq_gaps, c.mark()
        c.hello()
        c.wait_event("session.state", lambda d: d["detail"] == "process 2",
                     since=mark)
        self.assertEqual(c.seq_gaps, gaps)
        self.assertEqual(c.session_state["detail"], "process 2")

    def test_a_refused_hello_of_the_last_process_does_not_end_the_next(self):
        """hello() asks whose answer it has before it judges the protocol.
        Judged first, an answer of the last process, of another protocol,
        read after a supervisor had started the adapter again, would end
        the NEW process (rc -15) and leave the connection dead."""
        c = self._connector(_hello("paper", protocol=2), _hello("live"))
        c.start()
        real, fired = c.request, []

        def answered_then_restarted(method, params=None, timeout=10.0):
            answer = real(method, params, timeout)
            if method == "hello" and not fired:
                fired.append(True)
                c.close()
                c.start()
            return answer

        c.request = answered_then_restarted
        with self.assertRaises(Disconnected):
            c.hello()
        del c.request
        self.assertTrue(c.alive)
        self.assertIsNone(c.proc.poll())                # process 2 runs
        self.assertEqual(c.hello()["mode"], "live")
        self.assertEqual(self._seen(2), ["hello"])

    def test_a_call_waiting_on_the_last_process_is_answered_by_the_restart(
            self):
        """V5: a call still waiting on the last process is answered "adapter
        disconnected" by start(), not left to its timeout: that process's
        reader cannot answer it, because a child of the adapter holds the
        old pipe open and the reader never sees its end. No test pinned it
        (without it the call waited its whole timeout)."""
        child = os.path.join(self.tmp.name, "quiet.py")
        with open(child, "w") as fh:
            fh.write(textwrap.dedent(QUIET_CHILD.format()))
        c = self._connector(_hello("paper", x_child=child, x_mute=["ping"]),
                            _hello("paper"))
        c.connect()
        got = []

        def waiting():
            t0 = time.monotonic()
            try:
                c.request("ping", timeout=12)
                got.append(("answered", time.monotonic() - t0))
            except Exception as e:  # noqa: BLE001 - kept for the asserts
                got.append((e, time.monotonic() - t0))

        t = threading.Thread(target=waiting)
        t.start()
        time.sleep(0.3)                                 # it waits
        c.close()
        c.start()
        t.join(15)
        e, took = got[0]
        self.assertIsInstance(e, ConnectorError)
        self.assertIn("adapter disconnected", str(e))
        self.assertLess(took, 8.0)

    def test_what_connect_read_from_the_last_process_is_not_kept(self):
        """connect() on one thread, a supervisor's restart landing between
        the last process's answer to catalog.list and its use: that catalog
        is not the new process's, and is not kept (start() cleared it, and
        connect() must not write it back)."""
        c = self._connector(_hello("paper"), _hello("live"))
        c.hello()
        real, fired = c.request, []

        def answered_then_restarted(method, params=None, timeout=10.0):
            answer = real(method, params, timeout)
            if method == "catalog.list" and not fired:
                fired.append(True)
                c.close()
                c.start()
                return {"instruments": [{"symbol": "EURUSD"}]}
            return answer

        c.request = answered_then_restarted
        with self.assertRaises(Disconnected):
            c.reconcile()
        del c.request
        self.assertEqual((c.catalog, c.account, c.positions), ({}, {}, {}))
        self.assertNotIn("symbols", c.identity())
        self.assertEqual(c.hello()["mode"], "live")

    def test_the_last_process_is_heard_to_its_end_before_the_next_starts(
            self):
        """start() waits (2 s at most) for the last process's reader, so
        what that process said up to the end of its pipe (here, 40 events
        after its answer to bye) is passed on, to the history and the
        handlers, before anything of it is cleared. No test pinned it."""
        c = self._connector(_hello("paper", x_after_bye=40), _hello("paper"))
        heard = []
        c.on("x.last", lambda d: (time.sleep(0.01), heard.append(d["n"])))
        c.connect()
        c.close()
        c.start()
        self.assertEqual(heard, list(range(40)))
        self.assertEqual(sum(1 for n, _ in list(c.events) if n == "x.last"),
                         40)

    # -- stray lines, streams and locks across a restart --------------------

    @staticmethod
    def _until(cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            time.sleep(0.02)
        return cond()

    def test_a_platform_that_hides_stray_lines_can_still_start(self):
        """Were start() to empty stray_lines by reading the attribute, a
        platform's own Connector that hides it (the Terminal's stand-in for
        a kit from before stray_lines) could not start at all: "The adapter
        refused the handshake: stray_lines". start() gives each process a
        new deque and reads nothing of the last one."""

        class Hidden(Connector):
            def __getattribute__(self, name):
                if name == "stray_lines":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        c = self._connector(_hello("paper"), _hello("live"), cls=Hidden)
        c.start()
        self.assertEqual(c.hello()["mode"], "paper")
        c.close()
        c.start()
        self.assertEqual(c.hello()["mode"], "live")

    def test_each_process_prints_into_its_own_stray_lines(self):
        """After a restart the reader puts what the new process prints in the
        new deque, and nothing of it in the last process's."""
        c = self._connector(_hello("paper", x_print="printed by one"),
                            _hello("paper", x_print="printed by two"))
        c.connect()
        self.assertTrue(self._until(
            lambda: "printed by one" in list(c.stray_lines)))
        before = c.stray_lines
        c.close()
        c.start()
        self.assertIsNot(c.stray_lines, before)
        self.assertEqual(list(c.stray_lines), [])
        c.hello()
        self.assertTrue(self._until(
            lambda: "printed by two" in list(c.stray_lines)))
        self.assertEqual(list(c.stray_lines), ["printed by two"])
        self.assertEqual(list(before), ["printed by one"])

    def _restart_after(self, c, method, then=None):
        """c.request wrapped: just after the last process has answered
        `method` (the first time only), a supervisor's close() and start()
        on the same thread, then then(c)."""
        real, fired = c.request, []

        def answered_then_restarted(name, params=None, timeout=10.0):
            answer = real(name, params, timeout)
            if name == method and not fired:
                fired.append(True)
                c.close()
                c.start()
                if then is not None:
                    then(c)
            return answer

        c.request = answered_then_restarted
        return fired

    def test_a_stream_the_last_process_took_is_asked_of_the_next(self):
        """subscribe() does not write the last process's answer into
        `subscribed` after a restart has emptied it, or the next process
        would never be asked for that stream: its own subscribe() of the
        symbol would send nothing (SPEC 3.3: a restart keeps nothing the
        last process said, its streams included)."""
        c = self._connector(_hello("paper"), _hello("paper"))
        c.connect()
        self._restart_after(c, "quotes.subscribe")
        with self.assertRaises(Disconnected):
            c.subscribe(["EURUSD"])
        del c.request
        self.assertEqual(c.subscribed, [])
        c.hello()
        self.assertEqual(c.subscribe(["EURUSD"])["subscribed"], ["EURUSD"])
        self.assertEqual(c.subscribed, ["EURUSD"])
        self.assertEqual(self._seen(1).count("quotes.subscribe"), 1)
        self.assertEqual(self._seen(2).count("quotes.subscribe"), 1)

    def test_a_stream_the_last_process_dropped_stays_the_next_ones(self):
        """unsubscribe() does not write its view of `subscribed` after a
        restart, over what the next process has been asked for meanwhile: a
        stream the new process sends would be taken off the list."""
        c = self._connector(_hello("paper"), _hello("paper"))
        c.connect()
        c.subscribe(["EURUSD"])

        def the_next_streams_it(conn):
            conn.hello()
            conn.subscribe(["EURUSD"])

        self._restart_after(c, "quotes.unsubscribe", the_next_streams_it)
        with self.assertRaises(Disconnected):
            c.unsubscribe(["EURUSD"])
        del c.request
        self.assertEqual(c.subscribed, ["EURUSD"])
        self.assertEqual(self._seen(2).count("quotes.subscribe"), 1)
        self.assertNotIn("quotes.unsubscribe", self._seen(2))

    def test_start_holds_the_readers_lock_while_it_clears(self):
        """This pins start()'s hold of the reader's lock. The last process
        says its session state once more after its answer to bye, and its
        reader is held inside _track for that line until the next process
        is open (3 s at most). With the lock, start() waits for the line
        and then clears it; without it, the line lands after the clear, as
        if the new process had said it."""
        path = os.path.join(self.tmp.name, "after_bye.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(AFTER_BYE_ADAPTER))
        c = Connector([sys.executable, path])
        self.addCleanup(c.close)
        c.connect()
        old, old_reader = c.proc, c._reader
        real_track = c._track

        def held(name, data):
            if "after bye" in str(data.get("detail", "")):
                end = time.monotonic() + 3.0
                while c.proc is old and time.monotonic() < end:
                    time.sleep(0.01)
            real_track(name, data)

        c._track = held
        c.close()
        c.start()
        old_reader.join(10)
        self.assertFalse(old_reader.is_alive())
        self.assertEqual(c.session_state, {"state": "connected", "detail": ""})
        del c._track
        self.assertEqual(c.hello()["mode"], "paper")

    def test_two_restarts_at_once_leave_no_process_running(self):
        """Were start() to read the process it replaces before it takes any
        lock, two threads restarting one Connector at the same moment (a
        supervisor and a watchdog) would both end the old process and each
        open one; the first one opened would be left running with nobody
        to end it, even after close()."""
        c = self._connector(_hello("paper"))
        made = []
        real_popen = subprocess.Popen

        def recording(*a, **k):
            p = real_popen(*a, **k)
            made.append(p)
            return p

        def end_what_is_left():
            for p in made:
                if p.poll() is None:
                    p.kill()
                    p.wait()

        self.addCleanup(end_what_is_left)
        with mock.patch.object(subprocess, "Popen", recording):
            c.connect()
            real_close = c.close

            def slow_close():
                time.sleep(0.3)     # both threads are past their start
                real_close()

            c.close = slow_close
            gate = threading.Barrier(2)

            def restart():
                gate.wait()
                c.start()

            threads = [threading.Thread(target=restart) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            del c.close
            self.assertEqual(c.hello()["mode"], "paper")
            c.close()
        self.assertEqual(len(made), 3)
        self.assertEqual([p.poll() is not None for p in made],
                         [True, True, True])

    # -- a call in several requests, and two starts at once ------------------

    # A broker that takes one symbol per request: subscribe() and
    # unsubscribe() send one request per chunk.
    ONE_PER_CALL = {"flatten_all": True,
                    "data": {"max_symbols_per_request": 1}}

    def test_an_unsubscribe_in_chunks_sends_the_next_process_nothing(self):
        """The restart lands after the first chunk was answered AND kept
        (interposed on _keep, which keeps it and does not raise), so it is
        the _send check, not _keep, that refuses the second chunk. The first
        stream was stopped; the second chunk is not written to the next
        process."""
        c = self._connector(_hello("paper", capabilities=self.ONE_PER_CALL),
                            _hello("paper", capabilities=self.ONE_PER_CALL))
        c.connect()
        c.subscribe(["EURUSD", "GBPUSD"])
        real_keep, fired = c._keep, []

        def restarted_after_the_first_keep(generation, change=None, **values):
            real_keep(generation, change=change, **values)
            if change is not None and not fired:    # the first chunk's keep
                fired.append(True)
                c.close()
                c.start()
                c.hello()

        c._keep = restarted_after_the_first_keep
        with self.assertRaises(Disconnected):
            c.unsubscribe(["EURUSD", "GBPUSD"])
        del c._keep
        # the first chunk was answered and kept before the restart; the
        # second was refused by the _send check, never written to the next
        # process (without that check it reaches process 2, then _keep
        # raises, so this assertNotIn is what pins the check). start()
        # emptied `subscribed`, as a restart keeps nothing (SPEC 3.3).
        self.assertEqual(c.subscribed, [])
        self.assertNotIn("quotes.unsubscribe", self._seen(2))

    def test_a_restart_on_the_calls_own_thread_inside_request(self):
        """A restart made on the call's own thread inside request(), before
        the real request runs, does not send the chunk to the next process.
        close()'s bye is the first _send on the thread: were it to take the
        thread-local generation, the chunk's own write would go
        unchecked."""
        c = self._connector(_hello("paper"), _hello("paper"))
        c.connect()
        c.subscribe(["EURUSD"])
        real, fired = c.request, []

        def restart_then_write(name, params=None, timeout=10.0):
            if name == "quotes.unsubscribe" and not fired:
                fired.append(True)
                c.close()            # a bye, on this thread, then a restart
                c.start()
                c.hello()
                c.subscribe(["EURUSD"])
            return real(name, params, timeout)

        c.request = restart_then_write
        with self.assertRaises(Disconnected):
            c.unsubscribe(["EURUSD"])
        del c.request
        # process 2 was asked for the stream (by the nested subscribe), not
        # to stop it (the stale unsubscribe), and `subscribed` lists it
        self.assertEqual(c.subscribed, ["EURUSD"])
        self.assertEqual(self._seen(2).count("quotes.subscribe"), 1)
        self.assertNotIn("quotes.unsubscribe", self._seen(2))

    def test_a_platform_that_restarts_and_asks_again_on_disconnected(self):
        """A platform Connector whose request() restarts the adapter and
        asks again when it is told Disconnected. The adapter has died, so
        the first chunk of a call begun on it is refused, the adapter
        started again, and the chunk asked again on the new process -- which
        must be refused too, so the next process is not asked to stream what
        `subscribed` will not list."""
        class Reconnecting(Connector):
            def request(self, method, params=None, timeout=10.0):
                try:
                    return super().request(method, params, timeout)
                except Disconnected:
                    if method == "bye":
                        raise
                    self.start()
                    self.hello()
                    return super().request(method, params, timeout)

        c = self._connector(_hello("paper", capabilities=self.ONE_PER_CALL),
                            _hello("paper", capabilities=self.ONE_PER_CALL),
                            cls=Reconnecting)
        c.connect()
        c.proc.kill()                       # the adapter dies
        c.proc.wait()
        self.assertTrue(self._until(lambda: not c.alive, 5.0))
        with self.assertRaises(Disconnected):
            c.subscribe(["EURUSD", "GBPUSD"])
        # whatever process 2 was asked to stream, `subscribed` must list it
        self.assertEqual(len(c.subscribed),
                         self._seen(2).count("quotes.subscribe"))
        self.assertEqual(c.subscribed, [])

    def test_a_subscribe_in_chunks_sends_the_next_process_nothing(self):
        """The restart lands after the first chunk was answered and kept. The
        next chunk is not written to the next process, which would stream a
        symbol `subscribed` does not list (unsubscribe() could not stop it,
        and the cap would not count it)."""
        c = self._connector(_hello("paper", capabilities=self.ONE_PER_CALL),
                            _hello("paper", capabilities=self.ONE_PER_CALL))
        c.connect()
        real_keep, fired = c._keep, []

        def restarted_after_the_first_chunk(generation, change=None,
                                            **values):
            real_keep(generation, change=change, **values)
            if change is not None and not fired:
                fired.append(True)
                c.close()
                c.start()
                c.hello()

        c._keep = restarted_after_the_first_chunk
        with self.assertRaises(Disconnected) as cm:
            c.subscribe(["EURUSD", "GBPUSD"])
        del c._keep
        self.assertIn("started again", str(cm.exception))
        self.assertEqual(c.subscribed, [])
        self.assertNotIn("quotes.subscribe", self._seen(2))
        self.assertEqual(c.subscribe(["GBPUSD"])["subscribed"], ["GBPUSD"])
        self.assertEqual(self._seen(2).count("quotes.subscribe"), 1)

    def test_an_unsubscribe_whose_restart_lands_before_its_write(self):
        """One chunk; the restart lands after unsubscribe() read `subscribed`
        and before its request is written (interposed on quote_limits(),
        which it calls in between), and the next process is asked for the
        stream. The request is not written to it."""
        c = self._connector(_hello("paper"), _hello("paper"))
        c.connect()
        c.subscribe(["EURUSD"])
        real_limits, fired = c.quote_limits, []

        def restarted_before_the_write():
            if not fired:
                fired.append(True)
                c.close()
                c.start()
                c.hello()
                c.subscribe(["EURUSD"])
            return real_limits()

        c.quote_limits = restarted_before_the_write
        with self.assertRaises(Disconnected):
            c.unsubscribe(["EURUSD"])
        del c.quote_limits
        self.assertEqual(c.subscribed, ["EURUSD"])
        self.assertEqual(self._seen(2).count("quotes.subscribe"), 1)
        self.assertNotIn("quotes.unsubscribe", self._seen(2))

    def test_each_unsubscribe_chunk_is_kept_as_it_is_answered(self):
        """A later chunk that fails does not undo one the adapter has
        answered: that stream is stopped, and `subscribed` no longer lists
        it (kept only after the last chunk, none would be)."""
        c = self._connector(_hello("paper", capabilities=self.ONE_PER_CALL))
        c.connect()
        c.subscribe(["EURUSD", "GBPUSD", "USDJPY"])
        real, asked = c.request, []

        def the_second_is_refused(name, params=None, timeout=10.0):
            if name == "quotes.unsubscribe":
                asked.append(list(params["symbols"]))
                if len(asked) == 2:
                    raise ConnectorError("internal", "refused", True)
            return real(name, params, timeout)

        c.request = the_second_is_refused
        with self.assertRaises(ConnectorError):
            c.unsubscribe(["EURUSD", "GBPUSD", "USDJPY"])
        del c.request
        self.assertEqual(asked, [["EURUSD"], ["GBPUSD"]])
        self.assertEqual(c.subscribed, ["GBPUSD", "USDJPY"])

    def _first_starts_at_once(self, call):
        """Two callers of a fresh Connector (no process yet) at the same
        moment, each calling `call(c)`, with the opening of a process
        slowed so both are inside start() together; then close(). The
        processes opened, and the errors the callers got."""
        c = self._connector(_hello("paper"))
        made, errors = [], []
        real_popen = subprocess.Popen

        def slow(*a, **k):
            time.sleep(0.2)
            p = real_popen(*a, **k)
            made.append(p)
            return p

        def end_what_is_left():
            for p in made:
                if p.poll() is None:
                    p.kill()
                    p.wait()

        self.addCleanup(end_what_is_left)
        gate = threading.Barrier(2)

        def go():
            gate.wait()
            try:
                call(c)
            except Exception as e:  # noqa: BLE001 - kept for the asserts
                errors.append(e)

        with mock.patch.object(subprocess, "Popen", slow):
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            c.close()
        self.assertTrue(self._until(
            lambda: all(p.poll() is not None for p in made), 5.0),
            [p.poll() for p in made])
        return made, errors

    def test_two_first_starts_at_once_leave_no_process_running(self):
        """start() reads the process it replaces under the start lock. Read
        before it, both first starts would see none, each would open one,
        and the first would be left running after close()."""
        made, errors = self._first_starts_at_once(lambda c: c.start())
        self.assertEqual(errors, [])
        self.assertEqual(len(made), 2)

    def test_two_first_hellos_at_once_leave_no_process_running(self):
        """The same, through two hello() calls on a fresh Connector,
        each of which starts it (a lazily opened connection)."""
        made, _ = self._first_starts_at_once(lambda c: c.hello(timeout=3))
        self.assertEqual(len(made), 2)


# An adapter that answers hello (paper) and then, asked `ping`, first writes
# what ITS hello's x_lines says (raw bytes, one item a line) and only then
# answers; {} to the rest.
RAW_LINES_ADAPTER = """
    import json, sys
    LINES = {lines!r}
    hello = {{"protocol": 1, "mode": "paper", "account_model": "netting",
              "adapter": {{"name": "raw", "version": "1"}},
              "capabilities": {{"flatten_all": True}}}}
    out = sys.stdout.buffer
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["method"] == "ping" and LINES:
            for raw in LINES:
                out.write(raw + b"\\n")
            LINES = []
        out.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
                              "result": hello if msg["method"] == "hello"
                              else {{}}}}).encode() + b"\\n")
        out.flush()
        if msg["method"] == "bye":
            break
"""


class TestTheReaderGoesOn(unittest.TestCase):
    """An event whose data is null or not an object does not kill the reader
    thread in _track (every later call would time out, `alive` would stay
    True, and nothing would say why). Such an event is a protocol error: its
    line is kept in stray_lines, the connection's state says so, and the
    reader goes on. No line ends it."""

    setUp = TestARestartIsANewConnection.setUp
    _connector = TestARestartIsANewConnection._connector

    def _raw(self, lines):
        path = os.path.join(self.tmp.name, "raw.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(RAW_LINES_ADAPTER.format(lines=lines)))
        c = Connector([sys.executable, path])
        self.addCleanup(c.close)
        return c

    def test_an_event_whose_data_is_not_an_object_is_a_protocol_error(self):
        bad = [["session.state", None], ["order.filled", [1, 2]],
               ["account.updated", "rich"],
               # data an object, which _track cannot apply
               ["catalog.changed", {"reason": "suspended", "symbols": 5}],
               ["order.filled", {"fill_id": ["unhashable"], "side": "buy"}]]
        c = self._connector(_hello("paper", x_events=bad))
        mark = c.mark()
        c.connect()
        self.assertEqual(c.request("ping", timeout=3), {})
        self.assertTrue(c.alive)
        self.assertEqual(c.seq_gaps, 0)
        kept = list(c.stray_lines)
        self.assertEqual(len(kept), len(bad), kept)
        for (name, _), line in zip(bad, kept):
            self.assertIn('"%s"' % name, line)
        self.assertEqual(c.session_state["state"], "degraded")
        self.assertIn("not of this protocol", c.session_state["detail"])
        self.assertIn("catalog.changed", "".join(kept))
        # none was passed on as an event, nor applied
        names = [n for n, _ in list(c.events)[mark:]]
        self.assertEqual(names, ["session.state"], names)  # "process 1"
        self.assertEqual((c.account, c.positions), ({}, {}))

    def test_a_line_that_is_not_utf8_is_a_stray_line(self):
        """SPEC 1: the lines are UTF-8. Bytes that are not ended the reader
        with UnicodeDecodeError (and the good lines read with them were
        lost); they are read as a line that is not the protocol."""
        c = self._raw([b"not \xff\xfe text", b'{"v": 1, "type": "event", '
                       b'"seq": 1, "event": "session.state", "data": '
                       b'{"state": "degraded", "detail": "caf\xc3\xa9"}}'])
        c.connect()
        self.assertEqual(c.request("ping", timeout=3), {})
        self.assertEqual(c.request("ping", timeout=3), {})
        self.assertTrue(c.alive)
        self.assertEqual(len(c.stray_lines), 1)
        self.assertIn("not ", c.stray_lines[0])
        self.assertIn("\ufffd", c.stray_lines[0])
        self.assertEqual(c.session_state["detail"], "caf\xe9")

    def test_a_line_nested_past_what_json_reads_is_a_stray_line(self):
        """json reads such a line with RecursionError, not a ProtocolError,
        and it ended the reader."""
        c = self._raw([b"[" * 100000, b'{"a": ' * 50000])
        c.connect()
        self.assertEqual(c.request("ping", timeout=3), {})
        self.assertTrue(c.alive)
        self.assertEqual(len(c.stray_lines), 2)

    def test_a_platform_that_hides_stray_lines_keeps_its_reader(self):
        """A platform's Connector that hides stray_lines (written for a kit
        from before it) starts, and its reader does not die with
        AttributeError the first time the adapter prints a line that is not
        the protocol."""

        class Hidden(Connector):
            def __getattribute__(self, name):
                if name == "stray_lines":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        c = self._connector(_hello("paper", x_print="printed by one"),
                            cls=Hidden)
        c.connect()
        time.sleep(0.3)
        self.assertEqual(c.request("ping", timeout=3), {})
        self.assertTrue(c.alive)

    def test_a_print_on_a_hidden_stray_lines_platform_stays_connected(self):
        """On a platform Connector that hides stray_lines, an adapter's plain
        print() (not the protocol) does not turn session_state degraded,
        with a detail saying the line could not be applied (an
        AttributeError) and was kept with the stray lines, neither of which
        would be true. It is just dropped; the state stays connected, and
        the reader goes on. The adapter here (RAW_LINES) reports no session
        state of its own, so connected is its own word."""

        class Hidden(Connector):
            def __getattribute__(self, name):
                if name == "stray_lines":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        path = os.path.join(self.tmp.name, "printer.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(RAW_LINES_ADAPTER.format(
                lines=[b"a print() left in the adapter"])))
        c = Hidden([sys.executable, path])
        self.addCleanup(c.close)
        c.connect()
        self.assertEqual(c.session_state["state"], "connected")
        self.assertEqual(c.request("ping", timeout=3), {})  # emits the print
        time.sleep(0.3)
        self.assertTrue(c.alive)
        self.assertEqual(c.session_state["state"], "connected",
                         c.session_state)

    def test_a_bad_event_is_applied_whole_or_not_at_all(self):
        """An event that fails partway leaves no part of itself applied.
        Applied as it is read, a catalog.changed whose second instrument's
        symbol cannot be a key would leave the first in the catalog; an
        order.filled whose position_id cannot be used would record its
        fill_id before it failed, so the adapter's corrected re-send (the
        same fill_id) would be dropped as a duplicate and the position
        would never appear. Validated first: none of the catalog change is
        applied, no fill_id is recorded, and the corrected fill is applied.
        The events are emitted on `ping`, after connect()'s reconcile, so
        the book is not overwritten by positions.list."""
        events = [
            ["catalog.changed", {"reason": "listed", "instruments": [
                {"symbol": "GOOD", "tradable": True},
                {"symbol": ["unhashable"]}]}],
            ["order.filled", {"fill_id": "f1", "position_id": ["p"],
                              "side": "buy", "qty": 1, "symbol": "X"}],
            ["order.filled", {"fill_id": "f1", "position_id": "p1",
                              "side": "buy", "qty": 1, "symbol": "X"}]]
        path = os.path.join(self.tmp.name, "events.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(EVENTS_ON_PING_ADAPTER.format(
                events=events)))
        c = Connector([sys.executable, path])
        self.addCleanup(c.close)
        c.connect()
        mark = c.mark()
        c.request("ping", timeout=3)            # emits the events
        time.sleep(0.3)
        # the catalog change was applied whole or not at all: not at all
        self.assertNotIn("GOOD", c.catalog)
        # the corrected fill was applied; the bad copy was not passed on
        self.assertIn("p1", c.positions)
        passed = [n for n, _ in list(c.events)[mark:]]
        self.assertEqual(passed.count("order.filled"), 1, passed)
        self.assertNotIn("catalog.changed", passed)
        # both bad events are kept and said; the reader went on
        self.assertTrue(c.alive)
        self.assertIn("not of this protocol", c.session_state["detail"])


# An adapter that, the first time it is asked `ping`, emits the events in
# EVENTS (as [name, data]) and then answers; {} to every method, paper
# hello. The events come after connect()'s reconcile, so positions.list
# does not overwrite what they do.
EVENTS_ON_PING_ADAPTER = """
    import json, sys
    EVENTS = {events!r}
    hello = {{"protocol": 1, "mode": "paper", "account_model": "netting",
              "adapter": {{"name": "ev", "version": "1"}},
              "capabilities": {{"flatten_all": True}}}}
    seq = 0
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["method"] == "ping" and EVENTS:
            for event, data in EVENTS:
                seq += 1
                sys.stdout.write(json.dumps({{"v": 1, "type": "event",
                    "seq": seq, "event": event, "data": data}}) + "\\n")
            EVENTS = []
        sys.stdout.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
            "result": hello if msg["method"] == "hello" else {{}}}}) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


# An adapter that answers `ping` with the raw line ANSWER (with "@ID@" for
# the request id), and every other method with a plain {} result.
ANSWER_SHAPE_ADAPTER = """
    import json, sys
    ANSWER = {answer!r}
    hello = {{"protocol": 1, "mode": "paper", "account_model": "netting",
              "adapter": {{"name": "shape", "version": "1"}},
              "capabilities": {{"flatten_all": True}}}}
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["method"] == "ping":
            sys.stdout.write(ANSWER.replace("@ID@", msg["id"]) + "\\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({{"v": 1, "id": msg["id"], "type": "res",
            "result": hello if msg["method"] == "hello" else {{}}}}) + "\\n")
        sys.stdout.flush()
        if msg["method"] == "bye":
            break
"""


class TestAnAnswersShapeIsChecked(unittest.TestCase):
    """An answer whose error is not an object reaches the caller as a
    ConnectorError, not as AttributeError ('str' object has no attribute
    'get'); a result that is not an object is a protocol error, not
    returned as it is for a caller that reads it (reconcile) to raise
    AttributeError. The reader survives either way."""

    setUp = TestARestartIsANewConnection.setUp

    def _shape(self, answer):
        path = os.path.join(self.tmp.name,
                            "shape%d.py" % len(os.listdir(self.tmp.name)))
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(ANSWER_SHAPE_ADAPTER.format(
                answer=answer)))
        c = Connector([sys.executable, path])
        self.addCleanup(c.close)
        c.connect()
        return c

    def test_an_error_that_is_not_an_object_is_a_connector_error(self):
        for ans, text in (
                ('{"v":1,"id":"@ID@","type":"res","error":"boom"}', "boom"),
                ('{"v":1,"id":"@ID@","type":"res","error":null}', "None"),
                ('{"v":1,"id":"@ID@","type":"res","error":[1,2]}', "[1, 2]")):
            with self.subTest(answer=ans):
                c = self._shape(ans)
                with self.assertRaises(ConnectorError) as cm:
                    c.request("ping", timeout=3)
                self.assertEqual(cm.exception.code, "internal")
                self.assertIn(text, str(cm.exception))
                self.assertFalse(cm.exception.retryable)
                # the reader goes on; the next call is answered
                self.assertEqual(c.request("account.get", timeout=3), {})
                self.assertTrue(c.alive)

    def test_a_result_that_is_not_an_object_is_a_protocol_error(self):
        for ans in ('{"v":1,"id":"@ID@","type":"res","result":[1]}',
                    '{"v":1,"id":"@ID@","type":"res","result":null}',
                    '{"v":1,"id":"@ID@","type":"res","result":"x"}'):
            with self.subTest(answer=ans):
                c = self._shape(ans)
                with self.assertRaises(ConnectorError) as cm:
                    c.request("ping", timeout=3)
                self.assertEqual(cm.exception.code, "internal")
                # the reader goes on; the next call is answered
                self.assertEqual(c.request("account.get", timeout=3), {})
                self.assertTrue(c.alive)


class TestTheSentence(unittest.TestCase):
    def test_a_name_that_is_not_text_is_this_broker(self):
        for odd in (7, True, ["Hand"], {"n": 1}):
            with self.subTest(broker=odd):
                c = Connector([sys.executable, "-c", "pass"])
                c.handshake = {"broker": odd, "mode": "live"}
                c.terms = protocol.sanitize_terms(HAND)
                with self.assertRaises(NotArmed) as cm:
                    c.arm("x")
                self.assertIn("under this broker's terms", str(cm.exception))

    def test_check_shows_how_the_terms_were_read(self):
        from brueconnect.__main__ import _hdr
        c = Connector([sys.executable, "-c", "pass"])
        c.handshake = {"broker": "Hand Brokers", "mode": "live",
                       "terms": {"orders_by_hand_only": "yes"}}
        c.terms = protocol.sanitize_terms(c.handshake["terms"])
        self.assertIn("terms    orders_by_hand_only", _hdr(c, "hand"))
        c.handshake = {"broker": "Plain", "mode": "live"}
        c.terms = protocol.sanitize_terms(None)
        self.assertNotIn("terms ", _hdr(c, "plain"))

    def test_a_name_ending_in_s_and_a_summary_without_a_stop(self):
        c = Connector([sys.executable, "-c", "pass"])
        c.handshake = {"broker": "Fixture Markets", "mode": "live"}
        c.terms = protocol.sanitize_terms({"orders_by_hand_only": True,
                                           "summary": "Orders are placed by hand"})
        msg = c.by_hand_refusal()
        self.assertIn("under Fixture Markets' terms", msg)
        self.assertTrue(msg.endswith("The terms: Orders are placed by hand."))

    def test_a_name_cut_at_40_keeps_no_space_before_its_possessive(self):
        long = "Northwind Global Markets Holdings Group Ltd"   # a space at 40
        for how in ("broker", "display_name"):
            with self.subTest(name_in=how):
                c = Connector([sys.executable, "-c", "pass"])
                c.handshake = {"broker": long, "mode": "live"}
                if how == "display_name":
                    c.broker_info = protocol.sanitize_broker_info(
                        {"display_name": long})
                c.terms = protocol.sanitize_terms(HAND)
                msg = c.by_hand_refusal()
                self.assertNotIn(" 's", msg)
                self.assertIn("under Northwind Global Markets Holdings Group's "
                              "terms", msg)


class TestAlpacaDeclaresItsTerms(unittest.TestCase):
    """One shipped adapter's declared terms, as its hello sends them: its
    broker's market data is the account holder's alone (the broker's Terms
    and Conditions and Customer Agreement, section 30), so the adapter keeps
    it on the computer; nothing in those terms keeps orders by hand."""

    WANT = {"data_local_only": True, "orders_by_hand_only": False,
            "summary": ("Alpaca licenses its market data for your own "
                        "personal, non-commercial use, so its prices stay "
                        "on this computer."),
            "url": ("https://files.alpaca.markets/disclosures/library/"
                    "TermsAndConditions.pdf")}

    def test_hello_carries_them_on_paper_and_live(self):
        from brueconnect.adapters.alpaca import alpaca_adapter as mod
        for argv in (["--paper"], ["--live"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    a = mod.AlpacaAdapter(argv=argv, environ={},
                                          out=io.StringIO())
                    terms = a._hello({})["terms"]
                self.assertEqual(terms, self.WANT)
                self.assertNotIn("terms", err.getvalue())

    def test_what_it_declares_is_what_is_sent(self):
        from brueconnect.adapters.alpaca import alpaca_adapter as mod
        self.assertEqual(protocol.sanitize_terms(mod.AlpacaAdapter.terms),
                         self.WANT)


if __name__ == "__main__":
    unittest.main()
