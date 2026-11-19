"""SDK unit tests: envelope round-trips and a live subprocess conversation
with the mini adapter. Run: python3 -m unittest discover -s tests -v"""
import io
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brueconnect import (Connector, ConnectorError, NotArmed,  # noqa: E402
                          protocol)


class TestEnvelope(unittest.TestCase):
    def test_roundtrip(self):
        for line in [protocol.req("r1", "ping"),
                     protocol.res("r1", {"ok": True}),
                     protocol.res_error("r2", "invalid_qty", "no", False),
                     protocol.event(3, "quote", {"symbol": "X"})]:
            msg = protocol.parse_line(line)
            self.assertEqual(msg["v"], protocol.PROTOCOL_VERSION)

    def test_res_exclusivity(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_line('{"v":1,"id":"a","type":"res"}')
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_line(
                '{"v":1,"id":"a","type":"res","result":{},"error":{}}')

    def test_unknown_code_becomes_an_extension(self):
        """An unrecognised code is namespaced, not thrown away.

        Collapsing it to `internal` would tell the platform a broker's own
        vocabulary was a crash and destroy the only information it had.
        SPEC section 6 reserves the x_ prefix for exactly this.
        """
        msg = protocol.parse_line(protocol.res_error("r", "made_up", "m"))
        self.assertEqual(msg["error"]["code"], "x_made_up")
        self.assertIn("[made_up]", msg["error"]["message"])

    def test_spec_codes_survive_the_wire(self):
        """Every code the SPEC documents must reach the platform intact.

        A whitelist that falls behind the SPEC silently relabels documented
        codes as extensions.
        """
        for code in ("requote", "trade_restricted", "frozen", "no_price",
                     "invalid_expiry", "not_resting", "insufficient_funds",
                     "oco", "market_closed", "insufficient_margin"):
            msg = protocol.parse_line(protocol.res_error("r", code, "m"))
            self.assertEqual(msg["error"]["code"], code)


class TestConversation(unittest.TestCase):
    def setUp(self):
        self.c = Connector([sys.executable, str(ROOT / "tests" / "mini_adapter.py")])
        self.c.connect()

    def tearDown(self):
        self.c.close()

    def test_handshake_and_reconcile(self):
        self.assertEqual(self.c.handshake["mode"], "paper")
        self.assertTrue(self.c.handshake["capabilities"]["flatten_all"])
        self.assertIn("TEST", self.c.catalog)
        self.assertEqual(self.c.account["balance"], 10000.0)
        self.assertEqual(self.c.positions, {})

    def test_order_lifecycle_and_idempotency(self):
        r1 = self.c.request("order.place", {"client_id": "c-1", "symbol": "TEST",
                                            "side": "buy", "qty": 1})
        fill = self.c.wait_event("order.filled", lambda d: d["client_id"] == "c-1")
        self.assertEqual(fill["qty"], 1)
        # replay with the same client_id: same result, no second fill
        r2 = self.c.request("order.place", {"client_id": "c-1", "symbol": "TEST",
                                            "side": "buy", "qty": 1})
        self.assertEqual(r1, r2)
        fills = [d for (n, d) in list(self.c.events) if n == "order.filled"]
        self.assertEqual(len(fills), 1)
        # snapshot tracking picked the position up
        self.assertEqual(len(self.c.positions), 1)

    def test_errors_are_typed(self):
        with self.assertRaises(ConnectorError) as cm:
            self.c.request("order.place", {"client_id": "c-2", "symbol": "NOPE",
                                           "side": "buy", "qty": 1})
        self.assertEqual(cm.exception.code, "unknown_symbol")
        with self.assertRaises(ConnectorError) as cm:
            self.c.request("no.such.method")
        self.assertEqual(cm.exception.code, "unsupported")

    def test_flatten_all(self):
        self.c.request("order.place", {"client_id": "c-3", "symbol": "TEST",
                                       "side": "sell", "qty": 2})
        self.c.wait_event("order.filled", lambda d: d["client_id"] == "c-3")
        out = self.c.request("flatten_all")
        self.assertEqual(out["closed"], 1)
        self.c.wait_event("position.closed", lambda d: d["reason"] == "flatten")
        self.assertEqual(self.c.positions, {})

    def test_ping(self):
        t = self.c.request("ping")["time"]
        self.assertIsInstance(t, int)

    def test_identity_without_broker_info(self):
        """A broker that fills in nothing still gets a usable picker row.

        broker_info is optional (SPEC 3.1), so identity() has to fall back to
        the handshake's own `broker` string rather than leaving a platform
        with a filename to display.
        """
        ident = self.c.identity()
        self.assertEqual(ident["display_name"], "mini test broker")
        self.assertEqual(ident["mode"], "paper")
        self.assertEqual(ident["symbols"], 1)
        self.assertEqual(self.c.broker_info, {})


class TestStrayStdout(unittest.TestCase):
    """What an adapter prints on stdout that is not a protocol message is
    skipped, as ever, and its last lines are kept so a platform can show
    them when the adapter fails (a print() left in an adapter, or a file
    that is not an adapter at all)."""

    def run_script(self, source):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "script.py"
            path.write_text(source)
            c = Connector([sys.executable, str(path)])
            try:
                with self.assertRaises(ConnectorError) as cm:
                    c.hello(timeout=20)
            finally:
                c.close()
        return c, cm.exception

    def test_a_script_that_prints_and_ends_leaves_its_lines(self):
        c, e = self.run_script("print('hello from a script')\nprint()\n"
                               "print('  and a second line  ')\n")
        self.assertEqual(e.code, "internal")
        self.assertIn("adapter disconnected", str(e))
        self.assertEqual(list(c.stray_lines),
                         ["hello from a script", "and a second line"])

    def test_only_the_last_lines_are_kept_each_cut_short(self):
        c, _ = self.run_script(
            "for i in range(50): print('line %d' % i)\nprint('x' * 5000)\n")
        lines = list(c.stray_lines)
        self.assertEqual(len(lines), Connector.STRAY_LINES)
        self.assertEqual(lines[0], "line %d" % (51 - Connector.STRAY_LINES))
        self.assertEqual(lines[-1], "x" * Connector.STRAY_CHARS)

    def test_protocol_lines_are_not_stray(self):
        c = Connector([sys.executable, str(ROOT / "tests" / "mini_adapter.py")])
        try:
            c.connect()
            c.request("ping")
        finally:
            c.close()
        self.assertEqual(list(c.stray_lines), [])


class TestSnapshotTracking(unittest.TestCase):
    """The catalog is not static (SPEC 5), so the snapshot must not be.

    A catalog read once at connect and never again leaves a suspended symbol
    tradable and repriced terms stale. A platform reading that snapshot would
    keep offering an instrument the broker has stopped dealing and size
    orders off numbers it no longer honours.
    """

    def setUp(self):
        self.c = Connector(["true"])  # never started; _track needs no process
        self.c.catalog = {"EURUSD": {"symbol": "EURUSD", "tradable": True,
                                     "margin_rate": 0.02}}

    def test_inline_specs_are_applied(self):
        self.c._track("catalog.changed", {
            "reason": "terms", "symbols": ["EURUSD"],
            "instruments": [{"symbol": "EURUSD", "tradable": False,
                             "margin_rate": 0.05}]})
        self.assertEqual(self.c.catalog["EURUSD"]["margin_rate"], 0.05)
        self.assertFalse(self.c.catalog["EURUSD"]["tradable"])
        self.assertFalse(self.c.catalog_stale)  # nothing to re-read

    def test_without_inline_specs_the_catalog_is_flagged_stale(self):
        self.c._track("catalog.changed",
                      {"reason": "suspended", "symbols": ["EURUSD"]})
        # The SPEC says re-issue catalog.list; that cannot happen on the reader
        # thread, so the flag is how a request thread learns to do it.
        self.assertTrue(self.c.catalog_stale)
        self.assertEqual(self.c.last_catalog_change["reason"], "suspended")

    def test_expiry_removes_the_instrument(self):
        self.c._track("catalog.changed",
                      {"reason": "expired", "symbols": ["EURUSD"]})
        self.assertNotIn("EURUSD", self.c.catalog)

    def test_session_state_is_kept_for_the_platform_to_show(self):
        self.assertEqual(self.c.session_state["state"], "connected")
        self.c._track("session.state",
                      {"state": "degraded", "detail": "stream capped at 30"})
        self.assertEqual(self.c.session_state["state"], "degraded")
        self.assertIn("capped", self.c.session_state["detail"])


class TestQuoteSubscription(unittest.TestCase):
    """A broker states what it can stream; the platform stays inside it.

    A strategy watching a basket needs many symbols at once, so "subscribe to
    what is on screen" is not a general rule. What is general: the broker
    declares its limits, the platform never exceeds them, and whatever did not
    fit comes back named instead of silently unpriced.
    """

    def setUp(self):
        self.c = Connector(["true"])
        self.c.alive = True
        self.c.proc = type("P", (), {"stdin": object()})()
        self.sent = []
        self.c.request = lambda m, p=None, timeout=10.0: (
            self.sent.append((m, list(p["symbols"]))) or {})

    def _caps(self, **data):
        self.c.handshake = {"capabilities": {"data": {"quotes": True, **data}}}

    def test_no_declared_limit_sends_everything_at_once(self):
        self._caps()
        out = self.c.subscribe([f"S{i}" for i in range(120)])
        self.assertEqual(len(out["subscribed"]), 120)
        self.assertEqual(out["refused"], [])
        self.assertEqual(len(self.sent), 1)

    def test_requests_are_paged_at_the_declared_size(self):
        self._caps(max_symbols_per_request=40)
        self.c.subscribe([f"S{i}" for i in range(100)])
        self.assertEqual([len(syms) for _, syms in self.sent], [40, 40, 20])

    def test_the_cap_is_respected_and_the_remainder_is_named(self):
        self._caps(max_quote_symbols=30, max_symbols_per_request=40)
        out = self.c.subscribe([f"S{i}" for i in range(45)])
        self.assertEqual(len(out["subscribed"]), 30)
        self.assertEqual(len(out["refused"]), 15)
        self.assertEqual(out["refused"][0], "S30")   # in the order asked
        self.assertEqual(sum(len(s) for _, s in self.sent), 30)

    def test_the_cap_counts_what_is_already_streaming(self):
        self._caps(max_quote_symbols=3)
        self.c.subscribe(["A", "B"])
        out = self.c.subscribe(["C", "D", "E"])
        self.assertEqual(out["subscribed"], ["C"])
        self.assertEqual(out["refused"], ["D", "E"])
        self.assertEqual(self.c.subscribed, ["A", "B", "C"])

    def test_resubscribing_the_same_symbol_costs_nothing(self):
        self._caps(max_quote_symbols=2)
        self.c.subscribe(["A", "B"])
        out = self.c.subscribe(["A", "B"])
        self.assertEqual(out["subscribed"], [])
        self.assertEqual(out["refused"], [])

    def test_unsubscribing_frees_room(self):
        self._caps(max_quote_symbols=2)
        self.c.subscribe(["A", "B"])
        self.c.unsubscribe(["A"])
        self.assertEqual(self.c.subscribed, ["B"])
        self.assertEqual(self.c.subscribe(["C"])["subscribed"], ["C"])


class TestArming(unittest.TestCase):
    """One rule, in one place, for every surface (SPEC 3).

    Several implementations of the live-order gate would give several answers
    to "how does this platform authorise a live order", and the weakest one
    wins wherever it is used.
    """

    def setUp(self):
        self.c = Connector(["true"])  # not started; the gate is client-side
        self.c.alive = True           # request() gets past the liveness check
        self.c.proc = type("P", (), {"stdin": object()})()
        self.c._greeted = True        # as after a hello: the mode below is read

    def _try(self, method):
        try:
            self.c.request(method, {})
        except Exception as e:
            return e
        return None

    def test_paper_needs_no_arming(self):
        self.c.handshake = {"mode": "paper"}
        self.assertTrue(self.c.is_paper)
        # gets past the gate and fails later, on the wire, not on the rule
        self.assertNotIsInstance(self._try("order.place"), NotArmed)

    def test_live_is_refused_until_armed(self):
        self.c.handshake = {"mode": "live"}
        for method in sorted(Connector.MUTATING):
            self.assertIsInstance(self._try(method), NotArmed, method)
        self.c.arm("a test said so")
        for method in sorted(Connector.MUTATING):
            self.assertNotIsInstance(self._try(method), NotArmed, method)

    def test_an_unknown_mode_is_treated_as_live(self):
        for mode in ({}, {"mode": None}, {"mode": "demo"}, {"mode": "LIVE"}):
            self.c.handshake = dict(mode)
            self.c.disarm()
            self.assertFalse(self.c.is_paper, str(mode))
            self.assertIsInstance(self._try("order.place"), NotArmed, str(mode))

    def test_reading_an_account_is_not_trading_it(self):
        self.c.handshake = {"mode": "live"}
        for method in ("account.get", "positions.list", "orders.list",
                       "quotes.subscribe", "bars.history", "history.fills",
                       "history.operations"):
            self.assertNotIsInstance(self._try(method), NotArmed, method)

    def test_arming_records_why_and_needs_a_reason(self):
        self.c.handshake = {"mode": "live"}
        with self.assertRaises(ConnectorError):
            self.c.arm("   ")
        self.assertFalse(self.c.armed)
        self.c.arm("operator clicked through the live warning")
        self.assertEqual(self.c.armed_for,
                         "operator clicked through the live warning")
        self.c.disarm()
        self.assertIsNone(self.c.armed_for)

    def test_arming_cannot_outlive_the_connection(self):
        """A fresh Connector is unarmed, with nothing to remember to clear."""
        self.c.handshake = {"mode": "live"}
        self.c.arm("first session")
        second = Connector(["true"])
        self.assertFalse(second.armed)
        self.assertIsNone(second.armed_for)

    def test_a_mutating_method_cannot_be_added_without_classifying_it(self):
        """The MUTATING set is the whole rule, so it must cover the SPEC.

        Reads the method table out of SPEC.md rather than a copy of it: a
        method added to the protocol and not classified here would otherwise
        reach a real-money broker unarmed, which is the failure this whole class
        exists to prevent.
        """
        spec = (ROOT / "SPEC.md").read_text()
        documented = set(re.findall(r"^\| `([a-z_]+\.[a-z_]+|flatten_all)` \|",
                                    spec, re.M))
        self.assertIn("order.place", documented)  # the parse still works
        unclassified = documented - Connector.MUTATING - {
            # read-only, deliberately outside the gate
            # (auth.* included: arming exists so no MONEY moves un-armed, and
            # the login neither places nor cancels anything. Gating auth
            # would also invert the flow: the user cannot arm a broker they
            # have not connected, and cannot connect before logging in.)
            "auth.status", "auth.begin", "auth.logout", "accounts.list",
            "hello", "catalog.list", "account.get", "positions.list",
            "orders.list", "quotes.subscribe", "quotes.unsubscribe",
            "bars.history",
            "bars.subscribe", "bars.unsubscribe", "history.fills",
            "history.operations",
            # events, which are not calls at all
            "order.accepted", "order.rejected", "order.placed",
            "order.amended", "order.promoted", "order.cancelled",
            "order.expired", "order.filled", "order.partial",
            "position.closed", "position.trailed", "account.updated",
            "catalog.changed", "session.state", "margin.warning",
            "balance.operation",
        }
        self.assertEqual(unclassified, set(),
                         f"SPEC methods nobody classified: {unclassified}")


class _Pipe:
    """An adapter's stdin that keeps what is written to it."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


class TestExactNames(unittest.TestCase):
    """A method is its exact name in SPEC 4 and nothing else. The arming,
    idempotency, handshake and by-hand checks know a mutating method by its
    exact name, and an adapter finds its handler from the name, so another
    spelling of one (order_place, flatten.all) would reach the handler past
    every one of those checks."""

    # Other spellings of protocol methods, and names of none at all.
    ALIASES = ("order_place", "flatten.all", "position_close",
               "Order.Place", "ORDER.PLACE", "order.place ", " order.place",
               "orderplace", "order..place", "order_modify", "order.close",
               "positions_list", "no.such.method", "")

    def _connector(self, mode):
        c = Connector(["true"])       # never started; the pipe is a list
        c.alive = True
        c.proc = type("P", (), {"stdin": _Pipe()})()
        c._greeted = True
        c.handshake = {"mode": mode}
        return c

    def test_the_protocols_methods_are_the_specs_tables(self):
        """protocol.METHODS is section 4's method tables, read out of
        SPEC.md, and holds every mutating method."""
        spec = (ROOT / "SPEC.md").read_text()
        section = spec[spec.index("## 4. Methods"):spec.index("## 5. Events")]
        documented, in_table = set(), False
        for line in section.splitlines():
            if line.startswith("| method |"):
                in_table = True             # a method table's header
            elif not line.startswith("|"):
                in_table = False
            elif in_table:
                m = re.match(r"\| `([a-z_]+(?:\.[a-z_]+)?)` \|", line)
                if m:
                    documented.add(m.group(1))
        self.assertIn("flatten_all", documented)   # the parse still works
        self.assertNotIn("market", documented)     # an order type, not a call
        self.assertEqual(set(protocol.METHODS), documented)
        self.assertLessEqual(Connector.MUTATING, protocol.METHODS)

    def test_the_connector_sends_no_other_name(self):
        """Refused before anything is written, as `unsupported` (SPEC 1),
        on paper and live, armed or not, with or without parameters."""
        for mode, armed in (("live", False), ("live", True),
                            ("paper", False)):
            c = self._connector(mode)
            if armed:
                c.arm("a test said so")
            for name in self.ALIASES + (None, 7):
                with self.subTest(mode=mode, armed=armed, name=name):
                    with self.assertRaises(ConnectorError) as cm:
                        c.request(name, {"client_id": "x1", "symbol": "TEST",
                                         "side": "buy", "qty": 1})
                    self.assertEqual(cm.exception.code, "unsupported")
                    self.assertNotIsInstance(cm.exception, NotArmed)
                    self.assertIn("not a method of this protocol",
                                  str(cm.exception))
            self.assertEqual(c.proc.stdin.lines, [], (mode, armed))
            # the protocol's own name is judged by the rule, as before
            if mode == "live" and not armed:
                with self.assertRaises(NotArmed):
                    c.request("order.place", {"client_id": "x2"})
            self.assertEqual(c.proc.stdin.lines, [], (mode, armed))

    def test_the_suites_probe_sends_its_own_names_only(self):
        """The conformance suite still asks an adapter names that are not the
        protocol's (SPEC 1: it must answer them unsupported). The probe sends
        the kit's own names, with no parameters, and nothing else."""
        c = self._connector("live")
        for name in Connector.UNKNOWN_PROBES:
            self.assertNotIn(name, protocol.METHODS)
            with self.assertRaises(TimeoutError):  # nobody answers here
                c.probe_unknown(name, timeout=0.01)
        sent = [protocol.parse_line(ln) for ln in c.proc.stdin.lines]
        self.assertEqual([(m["method"], m["params"]) for m in sent],
                         [(n, {}) for n in Connector.UNKNOWN_PROBES])
        self.assertIn("order_place", Connector.UNKNOWN_PROBES)
        for name in ("order.place", "flatten.all", "flatten_all", "x"):
            with self.subTest(name=name):
                with self.assertRaises(ConnectorError):
                    c.probe_unknown(name, timeout=0.01)
        self.assertEqual(len(c.proc.stdin.lines), len(sent))

    def test_the_kits_adapter_base_dispatches_exact_names_only(self):
        """Another spelling of a method is unsupported and its handler never
        runs; the exact name still reaches it."""
        from brueconnect import Adapter

        class Recorder(Adapter):
            broker = "Recorder"

            def __init__(self):
                super().__init__(out=io.StringIO())
                self.ran = []

            def handle_order_place(self, params):
                self.ran.append("order.place")
                return {"order_id": "1", "status": "accepted"}

            def handle_flatten_all(self, params):
                self.ran.append("flatten_all")
                return {"closed": 1}

            def handle_position_close(self, params):
                self.ran.append("position.close")
                return {}

            def handle_positions_list(self, params):
                self.ran.append("positions.list")
                return {"positions": []}

        a = Recorder()
        for i, name in enumerate(self.ALIASES):
            a._dispatch("r%d" % i, name, {"client_id": "a%d" % i})
        said = [json.loads(ln) for ln in a._out.getvalue().splitlines()]
        self.assertEqual(len(said), len(self.ALIASES))
        for msg in said:
            self.assertEqual(msg["error"]["code"], "unsupported", msg)
        self.assertEqual(a.ran, [])
        a._dispatch("ok1", "order.place", {"client_id": "c1"})
        a._dispatch("ok2", "flatten_all", {"client_id": "c2"})
        self.assertEqual(a.ran, ["order.place", "flatten_all"])

    def test_no_adapter_of_the_kit_finds_a_handler_by_itself(self):
        """An adapter that routes requests itself (on lanes, say) asks the
        base for the handler, which knows exact names only, rather than
        deriving the handler's name from whatever name it was sent."""
        own = re.compile(r"""["']handle_["']\s*\+|f["']handle_\{""")
        base = ROOT / "brueconnect" / "adapter.py"
        found = []
        for path in sorted((ROOT / "brueconnect").rglob("*.py")):
            if path != base and own.search(path.read_text(encoding="utf-8")):
                found.append(str(path.relative_to(ROOT)))
        self.assertEqual(found, [])


class TestLazySession(unittest.TestCase):
    def test_hello_opens_no_broker_session(self):
        """A picker handshakes to learn a name; that must not cost a login."""
        from brueconnect import Adapter

        class Lazy(Adapter):
            broker = "Lazy"

            def __init__(self):
                super().__init__()
                self.opened = 0

            def on_first_call(self):
                self.opened += 1

            # (a method of the protocol: the base dispatches no other name)
            def handle_catalog_list(self, params):
                return {"instruments": []}

        a = Lazy()
        a._hello({})
        a._dispatch("r0", "ping", {})
        a._dispatch("r1", "no.such.method", {})
        a._dispatch("r1b", "catalog_list", {})   # another spelling: refused
        self.assertEqual(a.opened, 0)
        a._dispatch("r2", "catalog.list", {})
        a._dispatch("r3", "catalog.list", {})
        self.assertEqual(a.opened, 1)  # once, on the first real call

    def test_a_failed_open_does_not_latch(self):
        from brueconnect import Adapter, AdapterError

        class Flaky(Adapter):
            broker = "Flaky"
            tries = 0

            def on_first_call(self):
                type(self).tries += 1
                if self.tries < 2:
                    raise AdapterError("unauthorized", "broker was down")

            def handle_account_get(self, params):
                return {"balance": 1.0}

        a = Flaky()
        a._dispatch("r1", "account.get", {})
        a._dispatch("r2", "account.get", {})
        self.assertEqual(Flaky.tries, 2)  # retried rather than bricked


class TestBrokerInfo(unittest.TestCase):
    """Display identity is third-party text bound for a UI (SPEC 3.1)."""

    def test_hostile_fields_are_dropped_not_rejected(self):
        clean = protocol.sanitize_broker_info({
            "display_name": "Acme" + "!" * 200,
            "tagline": "line one\nline two\x07",
            "website": "javascript:alert(1)",
            "support": "http://plain.example",
            "mark": "TOOLONG",
            "regulated_by": "CySEC 000/00",
            "asset_classes": ["FX", "", None, "Metals"],
            "mode": "paper", "extra": "ignored",
        })
        # capped, not refused: a careless field never costs a connection
        self.assertEqual(len(clean["display_name"]), 40)
        self.assertEqual(clean["tagline"], "line one line two")
        self.assertEqual(clean["mark"], "TOO")
        # only https and mailto survive; nothing else is displayable safely
        self.assertNotIn("website", clean)
        self.assertNotIn("support", clean)
        # a bare string where a list belongs is a typo, not a failure
        self.assertEqual(clean["regulated_by"], ["CySEC 000/00"])
        self.assertEqual(clean["asset_classes"], ["FX", "Metals"])
        # broker_info may never carry behaviour, so unknown keys never pass
        self.assertNotIn("mode", clean)
        self.assertNotIn("extra", clean)

    def test_good_fields_survive(self):
        clean = protocol.sanitize_broker_info({
            "display_name": "AcmeFX Markets", "mark": "AF",
            "website": "https://acmefx.example",
            "support": "mailto:desk@acmefx.example"})
        self.assertEqual(clean["display_name"], "AcmeFX Markets")
        self.assertEqual(clean["website"], "https://acmefx.example")
        self.assertEqual(clean["support"], "mailto:desk@acmefx.example")

    def test_junk_types_are_survivable(self):
        for junk in (None, [], "nope", 7, {"display_name": {"a": 1}}):
            self.assertIsInstance(protocol.sanitize_broker_info(junk), dict)

    def test_adapter_sanitises_on_the_way_out(self):
        """An adapter cannot put a hostile field on the wire by accident."""
        from brueconnect import Adapter

        class Bad(Adapter):
            broker = "bad broker"

            def broker_info(self):
                return {"display_name": "x" * 500,
                        "website": "javascript:alert(1)"}

        out = Bad()._hello({})
        self.assertEqual(len(out["broker_info"]["display_name"]), 40)
        self.assertNotIn("website", out["broker_info"])


class TestSharedCodeNamesNoBroker(unittest.TestCase):
    """Shared code and its tests name no broker; a broker's name belongs in
    its own adapter's files. The kit's shared code is its modules outside
    adapters/<broker>/, its conformance suite and runner, and its tests not
    named after an adapter. Their comments and docstrings name none of the
    brokers the kit ships an adapter for; an adapter's own files and tests,
    identifiers and test data may."""

    # The brokers and trading platforms the rule covers: the three the kit
    # ships an adapter for (keyed by their adapter folders) and the other
    # names a comment could hold. Each without regard to case, and whole
    # where a shorter word could hold it.
    # The kit's own sample venues (Meridian, SablePoint, Pemberton, NovaFX and
    # the rest) are not brokers: they stay out. So do the bare codes of a
    # platform (MT4, MT5) that a shipped broker names its sub-accounts by.
    BROKERS = {
        "alpaca": r"alpaca", "oanda": r"oanda",
        "capitalcom": r"capital\s*\.?\s*com\b",
        "etoro": r"\be-?toro\b", "ctrader": r"\bc-?trader\b",
        "spotware": r"\bspotware\b", "ig": r"\big\s+(?:group|markets|index)\b",
        "saxo": r"\bsaxo(?:trader\w*|bank|\b)",
        "interactive brokers": r"interactive[\s-]*brokers",
        "pepperstone": r"pepperstone", "ic markets": r"\bic\s+markets\b",
        "fxpro": r"\bfxpro\b", "fxcm": r"\bfxcm\b", "dukascopy": r"dukascopy",
        "cmc markets": r"\bcmc\s+markets\b", "plus500": r"plus\s*500",
        "forex.com": r"forex\.com", "city index": r"\bcity\s+index\b",
        "stonex": r"\bstonex\b", "swissquote": r"swissquote",
        "trading 212": r"trading\s*212", "tradestation": r"tradestation",
        "tastytrade": r"tasty(?:trade|works)", "schwab": r"\bschwab\b",
        "ameritrade": r"ameritrade", "tradier": r"\btradier\b",
        "webull": r"\bwebull\b", "public.com": r"public\.com",
        "moomoo": r"\bmoomoo\b", "futu": r"\bfutu\b",
        "e*trade": r"\be\s*\*\s*trade\b|\betrade\b",
        "questrade": r"questrade", "robinhood": r"robinhood",
        "freetrade": r"\bfreetrade\b", "hargreaves": r"hargreaves",
        "degiro": r"degiro", "coinbase": r"coinbase",
        "crypto.com": r"crypto\.com",
        "bitstamp": r"bitstamp",
        "revolut": r"\brevolut\b", "bybit": r"\bbybit\b",
        "binance": r"binance", "bitpanda": r"bitpanda",
        "bitfinex": r"bitfinex", "metatrader": r"meta\s*trader",
        "metaquotes": r"metaquotes", "rithmic": r"rithmic",
        "tradelocker": r"tradelocker", "match-trader": r"match[\s-]?trader\b",
        "projectx": r"\bprojectx\b", "topstep": r"topstep",
        "dxtrade": r"dxtrade", "devexperts": r"devexperts",
        "tradovate": r"tradovate", "ninjatrader": r"ninjatrader",
        "metaapi": r"\bmetaapi\b", "snaptrade": r"snaptrade",
        "ib gateway": r"\bib\s+gateway\b", "tradingview": r"trading\s*view",
        # More names a comment could hold.
        "avatrade": r"avatrade", "admirals": r"\badmirals\b",
        "exness": r"exness", "tickmill": r"tickmill",
        "vantage": r"\bvantage\b", "thinkmarkets": r"think\s*markets",
        "fp markets": r"\bfp\s+markets\b", "skilling": r"\bskilling\b",
        "spreadex": r"spreadex", "deriv": r"\bderiv\b",
        "lightspeed": r"lightspeed", "firstrade": r"firstrade",
        "ally invest": r"\bally\s+invest\b",
        "tiger brokers": r"\btiger\s+brokers\b", "zerodha": r"zerodha",
        "lynx": r"\blynx\b", "scalable capital": r"scalable\s+capital",
        "trade republic": r"trade\s+republic", "lightyear": r"\blightyear\b",
        "bitget": r"bitget", "archax": r"archax", "kucoin": r"kucoin",
        "bitvavo": r"bitvavo", "luno": r"\bluno\b", "coinjar": r"coinjar",
        "zumo": r"\bzumo\b", "sierra chart": r"sierra\s+chart",
        "trading technologies": r"trading\s+technologies",
        "quantower": r"quantower", "morgan stanley": r"morgan\s+stanley",
        "lightstreamer": r"lightstreamer",
    }
    # Names that are also letters, a short word, or an everyday word, matched
    # only as each firm writes them: two capitals alone (never inside a word
    # such as the three-letter one above, nor the two-letter one lower-cased
    # or in "config"). A capitalised everyday word is a name only where no
    # everyday tail follows it (a " to ", a parenthesised "(the model)"); the
    # two-letter pronoun only in its possessive; a verb-or-beast name only
    # capitalised. The everyday word a loose pattern would otherwise catch
    # stays out.
    CASED = {"ig": r"\bIG's\b", "ibkr": r"\bIBKR\b",
             "lmax": r"\bLMAX\b|\bLmax\b", "xtb": r"\bXTB\b",
             "cqg": r"\bCQG\b", "okx": r"\bOKX\b", "xm": r"\bXM\b",
             "ftmo": r"\bFTMO\b", "atas": r"\bATAS\b", "tws": r"\bTWS\b",
             "uphold": r"\bUphold\b", "kraken": r"\bKraken\b",
             "one trading": r"\bOne\s+Trading\b",
             "fidelity": r"\bFidelity\b(?!\s+to\b)",
             "gemini": r"\bGemini\b(?!\s*\(the\s+model\))"}

    @classmethod
    def names(cls, but=()):
        """One pattern for every name above, less those keyed in `but`."""
        return re.compile("|".join(
            ["(?i:%s)" % p for k, p in cls.BROKERS.items() if k not in but]
            + ["(?-i:%s)" % p for k, p in cls.CASED.items() if k not in but]))

    ADAPTERS = ("alpaca", "oanda", "capitalcom", "lse_sim", "atlas", "helix",
                "meridian", "northgate", "novafx", "paper", "pemberton",
                "quantex", "sablepoint", "vertex")

    @classmethod
    def own(cls, stem: str):
        """The adapter a test file belongs to, by its name, or None."""
        stem = stem.lower()
        for a in cls.ADAPTERS:
            if (a in stem.split("_") or stem.startswith(("test_" + a,
                                                         "fake_" + a,
                                                         "test_fake_" + a,
                                                         "run_conformance_"
                                                         + a))):
                return a
        return None

    def shared_files(self):
        pkg = ROOT / "brueconnect"
        files = [p for p in pkg.glob("*.py")]
        files += list((pkg / "adapters").glob("*.py"))
        for sub in ("templates", "conformance", "runner"):
            files += list((pkg / sub).rglob("*.py"))
        for p in (ROOT / "tests").glob("*.py"):
            stem = p.stem.lower()
            if stem.startswith(("fake_", "run_conformance_", "test_fake_")):
                continue
            if self.own(stem):
                continue
            files.append(p)
        return sorted(set(files))

    @staticmethod
    def words(path):
        """(line, text) of each comment and docstring of a Python file."""
        import ast
        import tokenize
        out = []
        with open(path, "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT:
                    out.append((tok.start[0], tok.string))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in [tree] + [n for n in ast.walk(tree) if isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]:
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.append((node.body[0].lineno, doc))
        return out

    def test_comments_and_docstrings_of_shared_code_name_no_broker(self):
        files = self.shared_files()
        names = {p.relative_to(ROOT).as_posix() for p in files}
        for must in ("brueconnect/client.py", "brueconnect/registry.py",
                     "brueconnect/conformance/run.py",
                     "brueconnect/runner/live_driver.py", "tests/test_sdk.py",
                     "tests/test_terms.py",
                     "tests/test_conformance_ranking.py"):
            self.assertIn(must, names)
        for leaves in ("tests/test_oanda_core.py", "tests/fake_capitalcom.py",
                       "tests/test_alpaca_core.py",
                       "brueconnect/adapters/oanda/oanda_adapter.py"):
            self.assertNotIn(leaves, names)
        found = []
        names = self.names()
        for path in files:
            for line, text in self.words(path):
                if names.search(text):
                    found.append("%s:%d: %s" % (
                        path.relative_to(ROOT).as_posix(), line,
                        " ".join(text.split())[:100]))
        self.assertEqual(found, [], "\n".join(found))

    def test_every_name_is_found_and_no_word_that_is_not_one(self):
        """Every name is found, as its firm writes it; a word that is not a
        name is not."""
        names = self.names()
        for text in ("eToro's public API", "the cTrader Open API",
                     "Spotware's", "IG's REST API", "IG Group", "IG Index",
                     "Interactive Brokers", "IBKR", "Saxo Bank", "Capital.com",
                     "capital . com", "OANDA", "Oanda", "Alpaca",
                     "Pepperstone", "IC Markets", "FxPro", "FXCM",
                     "Dukascopy", "CMC Markets", "Plus500", "FOREX.com",
                     "City Index", "StoneX", "Swissquote", "LMAX Global",
                     "Trading 212", "TradeStation", "tastytrade",
                     "Charles Schwab", "TD Ameritrade", "Tradier", "Webull",
                     "Public.com", "moomoo", "Futu", "E*TRADE", "Questrade",
                     "Robinhood", "Fidelity", "Freetrade",
                     "Hargreaves Lansdown", "DEGIRO", "Coinbase",
                     "Crypto.com", "Kraken", "Bitstamp", "Gemini",
                     "Revolut X", "OKX", "Bybit", "Binance", "Bitpanda",
                     "Bitfinex", "MetaTrader 5", "MetaQuotes", "Rithmic",
                     "TradeLocker", "Match-Trader", "ProjectX", "TopstepX",
                     "DXtrade", "Devexperts", "Tradovate", "NinjaTrader",
                     "MetaApi", "CQG", "SnapTrade", "XTB",
                     # more names, and other spellings of names above
                     "AvaTrade", "Admirals", "Exness", "XM", "Tickmill",
                     "Vantage", "ThinkMarkets", "FP Markets", "Skilling",
                     "Spreadex", "Deriv", "FTMO", "Lightspeed", "Firstrade",
                     "Ally Invest", "Tiger Brokers", "Zerodha", "Lynx",
                     "Scalable Capital", "Trade Republic", "Lightyear",
                     "Bitget", "Archax", "KuCoin", "Bitvavo", "Uphold",
                     "Luno", "CoinJar", "Zumo", "One Trading", "Sierra Chart",
                     "Trading Technologies", "Quantower", "ATAS",
                     "Morgan Stanley", "Lightstreamer", "Interactive-Brokers",
                     "InteractiveBrokers", "IB Gateway", "TWS",
                     "Meta Trader 5", "Match Trader", "SaxoTraderGO",
                     "tastyworks", "e-toro", "Lmax", "TradingView",
                     "Trading View"):
            with self.subTest(name=text):
                self.assertTrue(names.search(text), text)
        for text in ("BIG", "big", "Ig", "ig", "config", "signal", "rig",
                     "IGNORE", "high fidelity", "the future", "a forex pair",
                     "public code", "capital", "trading", "markets", "sig",
                     "lmax", "okx", "xtbs", "MT4-linked sub-accounts", "MT5",
                     "Meridian", "SablePoint", "Pemberton", "NovaFX",
                     "Northgate", "Helix", "Quantex", "Vertex", "Atlas",
                     # ordinary phrases a shared comment may hold
                     "the capital committed to a position",
                     "capital, compounded", "Fidelity to the SPEC matters",
                     "Gemini (the model)", "a kraken of threads",
                     "the IG of a request", "a clear advantage",
                     "deriving the mid", "it is derived", "a derivative"):
            with self.subTest(word=text):
                self.assertIsNone(names.search(text), text)

    def test_an_adapters_own_files_name_no_other_broker(self):
        """An adapter's own files and tests may name their own broker, not
        another: a comment that says it does a thing as another broker's
        adapter does it says so without that broker's name."""
        found, seen = [], {}
        for a in self.ADAPTERS:
            others = self.names(but=(a,))
            files = list((ROOT / "brueconnect" / "adapters" / a).rglob("*.py"))
            files += [p for p in (ROOT / "tests").glob("*.py")
                      if self.own(p.stem) == a]
            seen[a] = len(files)
            for path in sorted(files):
                for line, text in self.words(path):
                    for m in others.finditer(text):
                        found.append("%s:%d: %s" % (
                            path.relative_to(ROOT).as_posix(), line,
                            m.group(0)))
        for a in ("alpaca", "oanda", "capitalcom"):
            self.assertGreater(seen[a], 4, a)     # its adapter, stand-in, tests
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    unittest.main()
