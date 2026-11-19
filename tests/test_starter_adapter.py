"""The starter adapter a Terminal user downloads and begins from:
brueconnect/templates/my_broker_adapter.py.

What the Terminal and the user rely on: a handshake answered cold with the
agreed identity and key form; an empty key box named in an `unauthorized`
answer; the SDK of whoever starts it (BRUE_CONNECT_SDK, put first on
sys.path) under whatever Python runs it, from any folder; a practice market
that quotes, charts and trades; and the conformance suite passing with no
FAIL as delivered.

Run: python3 -m unittest tests.test_starter_adapter -v
"""
import ast
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brueconnect import Connector, ConnectorError  # noqa: E402

STARTER = ROOT / "brueconnect" / "templates" / "my_broker_adapter.py"
FORM = [{"key": "api_key", "label": "API key", "type": "text"},
        {"key": "api_secret", "label": "API secret", "type": "password"}]
# "the system python3", as a user's machine has it: not this test's own
# interpreter on purpose when they differ.
PYTHON = shutil.which("python3") or sys.executable
# Runs the starter's top level (its SDK lookup and its imports) without
# starting it, then says which brueconnect that Python imported.
PROBE = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='probe'); "
         "import brueconnect; print(brueconnect.__file__)")


def adapter_env(**extra):
    """What a platform hands the adapter: no PYTHONPATH, no BRUE_* of ours,
    the SDK named in BRUE_CONNECT_SDK, plus `extra`."""
    env = {k: v for k, v in os.environ.items()
           if k != "PYTHONPATH" and not k.startswith("BRUE_")}
    env["BRUE_CONNECT_SDK"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def talk(lines, env, cwd):
    """Run the starter as a platform would, send `lines`, return its answers."""
    out = subprocess.run([PYTHON, str(STARTER)], input="".join(
        json.dumps(m) + "\n" for m in lines), capture_output=True, text=True,
        env=env, cwd=cwd, timeout=30)
    return [json.loads(x) for x in out.stdout.splitlines() if x.strip()], out


HELLO = {"v": 1, "id": "h", "type": "req", "method": "hello",
         "params": {"protocol": 1, "client": {"name": "test", "version": "0"}}}
BYE = {"v": 1, "id": "b", "type": "req", "method": "bye", "params": {}}


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class Handshake(TempDir):
    def test_hello_is_answered_cold_with_the_agreed_identity_and_form(self):
        state = self.tmp / "state"
        answers, out = talk([HELLO, BYE], adapter_env(BRUE_BROKER_STATE=str(state)),
                            self.tmp)
        hello = answers[0]["result"]
        self.assertEqual(hello["adapter"], {"name": "my-broker", "version": "0.1"})
        self.assertEqual((hello["broker"], hello["mode"]), ("My Broker", "paper"))
        self.assertEqual(hello["credential_form"], FORM)
        self.assertIs(hello["capabilities"]["flatten_all"], True)
        self.assertEqual(hello["capabilities"]["data"],
                         {"quotes": True, "bars": True, "history": True})
        self.assertEqual(answers[1]["result"], {})
        # Cold: no key was set and nothing was opened. The practice account
        # is the "broker", and its folder was never made.
        self.assertFalse(state.exists())
        self.assertEqual(out.stderr, "")

    def _refusal(self, **keys):
        c = Connector([PYTHON, str(STARTER)], env=adapter_env(
            BRUE_BROKER_STATE=str(self.tmp / "state"), **keys))
        self.addCleanup(c.close)
        c.hello()
        try:
            c.request("catalog.list")
        except ConnectorError as e:
            return e
        return None

    def test_an_empty_box_is_unauthorized_and_named(self):
        e = self._refusal(BRUE_CRED_API_KEY="k1")
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("API secret box is empty", str(e))
        self.assertNotIn("API key", str(e))
        e = self._refusal(BRUE_CRED_API_SECRET="s1")
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("API key box is empty", str(e))
        self.assertNotIn("API secret", str(e))
        e = self._refusal(BRUE_CRED_API_KEY="  ", BRUE_CRED_API_SECRET="")
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("API key and API secret boxes are empty", str(e))
        # any two values are accepted by the practice market
        self.assertIsNone(self._refusal(BRUE_CRED_API_KEY="k", BRUE_CRED_API_SECRET="s"))

    def _shell_refusal(self, starter=STARTER, **keys):
        """As the card's test command runs it: the SDK on PYTHONPATH and no
        BRUE_CONNECT_SDK, which the Terminal always sets."""
        env = adapter_env(BRUE_BROKER_STATE=str(self.tmp / "state"),
                          PYTHONPATH=str(ROOT), **keys)
        del env["BRUE_CONNECT_SDK"]
        c = Connector([PYTHON, str(starter)], env=env)
        self.addCleanup(c.close)
        c.hello()
        try:
            c.request("catalog.list")
        except ConnectorError as e:
            return e
        return None

    def test_run_outside_the_terminal_it_names_the_variables_to_set(self):
        e = self._shell_refusal(BRUE_CRED_API_KEY="k1")
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("BRUE_CRED_API_SECRET is not set", str(e))
        self.assertNotIn("BRUE_CRED_API_KEY", str(e))
        self.assertNotIn("row", str(e))          # no Terminal row in a shell
        e = self._shell_refusal()
        self.assertIn("BRUE_CRED_API_KEY and BRUE_CRED_API_SECRET are not set", str(e))
        self.assertIsNone(self._shell_refusal(BRUE_CRED_API_KEY="k",
                                              BRUE_CRED_API_SECRET="s"))

    def test_boxes_changed_to_a_username_and_password_take_those_names(self):
        """The walkthrough's "Make it yours": the boxes changed to fit a broker.
        Each box then comes from BRUE_CRED_ and its own key in capitals."""
        src = STARTER.read_text(encoding="utf-8")
        old_form = ('{"key": "api_key", "label": "API key", "type": "text"},\n'
                    '        {"key": "api_secret", "label": "API secret", "type": "password"},')
        old_login = "def broker_login(self, api_key, api_secret):"
        self.assertEqual((src.count(old_form), src.count(old_login)), (1, 1))
        src = src.replace(old_form, (
            '{"key": "username", "label": "Username", "type": "text"},\n'
            '        {"key": "password", "label": "Password", "type": "password"},'))
        src = src.replace(old_login, "def broker_login(self, username, password):")
        mine = self.tmp / "acme_adapter.py"
        mine.write_text(src, encoding="utf-8")
        e = self._shell_refusal(starter=mine, BRUE_CRED_API_KEY="practice",
                                BRUE_CRED_API_SECRET="practice")
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("BRUE_CRED_USERNAME and BRUE_CRED_PASSWORD are not set", str(e))
        self.assertIsNone(self._shell_refusal(starter=mine, BRUE_CRED_USERNAME="u",
                                              BRUE_CRED_PASSWORD="p"))


class SdkLookup(TempDir):
    def _probe(self, env):
        return subprocess.run([PYTHON, "-c", PROBE, str(STARTER)], env=env,
                              cwd=self.tmp, capture_output=True, text=True,
                              timeout=30)

    def test_it_speaks_the_sdk_named_in_BRUE_CONNECT_SDK_from_any_folder(self):
        out = self._probe(adapter_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(Path(out.stdout.strip()).resolve(),
                         (ROOT / "brueconnect" / "__init__.py").resolve())
        # and the program itself, started the same way, sends this kit's
        # handshake (the credential form is SPEC 1.10)
        answers, _ = talk([HELLO, BYE], adapter_env(
            BRUE_BROKER_STATE=str(self.tmp / "s")), self.tmp)
        self.assertEqual(answers[0]["result"]["credential_form"], FORM)

    def test_without_the_variable_this_python_finds_another_sdk_or_none(self):
        """The control: it is BRUE_CONNECT_SDK that decides, not the folder."""
        env = adapter_env()
        del env["BRUE_CONNECT_SDK"]
        out = self._probe(env)
        mine = (ROOT / "brueconnect" / "__init__.py").resolve()
        if out.returncode == 0 and Path(out.stdout.strip()).resolve() == mine:
            self.skipTest(f"{PYTHON} imports this very kit without the variable")
        self.assertTrue(out.returncode != 0 or Path(out.stdout.strip()).resolve() != mine)


class Practice(TempDir):
    def setUp(self):
        super().setUp()
        self.env = adapter_env(BRUE_BROKER_STATE=str(self.tmp / "state"),
                               BRUE_CRED_API_KEY="practice",
                               BRUE_CRED_API_SECRET="practice")
        self.c = self._connect()

    def _connect(self):
        c = Connector([PYTHON, str(STARTER)], env=self.env)
        self.addCleanup(c.close)
        c.connect()
        return c

    def _bid(self, symbol):
        mark = self.c.mark()
        self.c.request("quotes.subscribe", {"symbols": [symbol]})
        return self.c.wait_event("quote", lambda d: d["symbol"] == symbol,
                                 timeout=5, since=mark)["bid"]

    def test_the_catalog_is_three_made_up_instruments(self):
        self.assertEqual(set(self.c.catalog),
                         {"PRACTICE-FX", "PRACTICE-SHARE", "PRACTICE-COIN"})
        self.assertEqual(self.c.account["balance"], 10000.0)
        self.assertEqual(self.c.account["currency"], "USD")

    def test_quotes_come_about_once_a_second(self):
        mark = self.c.mark()
        self.c.request("quotes.subscribe", {"symbols": ["PRACTICE-FX"]})
        first = self.c.wait_event("quote", lambda d: d["symbol"] == "PRACTICE-FX",
                                  timeout=3, since=mark)
        self.assertLess(first["bid"], first["ask"])
        self.assertLess(abs(first["time"] - time.time() * 1000), 5000)
        later = self.c.wait_event(
            "quote", lambda d: d["symbol"] == "PRACTICE-FX" and d["time"] > first["time"],
            timeout=4, since=mark)
        self.assertLessEqual(later["time"] - first["time"], 3000)
        self.c.request("quotes.unsubscribe", {"symbols": ["PRACTICE-FX"]})

    def test_bars_are_closed_bars_and_every_timeframe_agrees(self):
        now = time.time() * 1000
        ones = self.c.request("bars.history", {"symbol": "PRACTICE-COIN",
                                               "timeframe": "1m", "count": 120})["bars"]
        self.assertEqual(len(ones), 120)
        for b in ones:
            t, o, h, low, c, v = b
            self.assertEqual(t % 60_000, 0)
            self.assertTrue(low <= min(o, c) <= max(o, c) <= h and v > 0, b)
        self.assertEqual([b[0] for b in ones], sorted(b[0] for b in ones))
        self.assertLessEqual(ones[-1][0] + 60_000, now + 1000)  # closed
        for b in self.c.request("bars.history", {"symbol": "PRACTICE-COIN",
                                                 "timeframe": "15m", "count": 4})["bars"]:
            inside = [x for x in ones if b[0] <= x[0] < b[0] + 900_000]
            if len(inside) == 15:
                self.assertEqual(b[1:], [inside[0][1], max(x[2] for x in inside),
                                         min(x[3] for x in inside), inside[-1][4],
                                         sum(x[5] for x in inside)])
        t0 = time.time()
        days = self.c.request("bars.history", {"symbol": "PRACTICE-FX", "timeframe": "1d",
                                               "count": 5000}, timeout=30)["bars"]
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(days[0][0], 1577836800000)  # 2020-01-01, the first day
        with self.assertRaises(ConnectorError) as e:
            self.c.request("bars.history", {"symbol": "PRACTICE-FX", "timeframe": "2m"})
        self.assertEqual(e.exception.code, "unsupported")

    def test_a_market_buy_fills_and_a_close_closes(self):
        bid = self._bid("PRACTICE-SHARE")
        mark = self.c.mark()
        self.c.request("order.place", {"client_id": "t-buy", "symbol": "PRACTICE-SHARE",
                                       "side": "buy", "qty": 2, "sl": round(bid - 5, 2),
                                       "tp": round(bid + 5, 2)})
        fill = self.c.wait_event("order.filled", lambda d: d["client_id"] == "t-buy",
                                 timeout=5, since=mark)
        self.assertEqual((fill["qty"], fill["side"]), (2, "buy"))
        held = [p for p in self.c.request("positions.list")["positions"]
                if p["position_id"] == fill["position_id"]]
        self.assertEqual([(p["sl"], p["tp"]) for p in held],
                         [(round(bid - 5, 2), round(bid + 5, 2))])
        self.c.request("order.modify", {"client_id": "t-mod",
                                        "position_id": fill["position_id"],
                                        "sl": round(bid - 6, 2)})
        held = [p for p in self.c.request("positions.list")["positions"]
                if p["position_id"] == fill["position_id"]]
        self.assertEqual([(p["sl"], p["tp"]) for p in held],
                         [(round(bid - 6, 2), round(bid + 5, 2))])  # tp left alone
        self.c.request("position.close", {"client_id": "t-close",
                                          "position_id": fill["position_id"]})
        closed = self.c.wait_event("position.closed",
                                   lambda d: d["position_id"] == fill["position_id"],
                                   timeout=5, since=mark)
        self.assertEqual(closed["reason"], "manual")
        self.assertEqual(self.c.request("positions.list")["positions"], [])
        fills = self.c.request("history.fills")["fills"]
        self.assertEqual([(f["side"], f["qty"]) for f in fills], [("buy", 2), ("sell", 2)])
        self.assertEqual(fills[1]["realized_pnl"], closed["realized_pnl"])
        self.assertEqual(self.c.request("account.get")["balance"],
                         round(10000 + closed["realized_pnl"], 2))
        time.sleep(1.5)  # the pump has looked at the closes again: told once
        self.assertEqual(sum(1 for n, d in list(self.c.events) if n == "position.closed"
                             and d["close_id"] == closed["close_id"]), 1)

    def test_a_stop_is_kept_and_filled_by_the_practice_market(self):
        bid = self._bid("PRACTICE-SHARE")
        mark = self.c.mark()
        # a stop already above the price: the market fills it at the next second
        self.c.request("order.place", {"client_id": "t-stop", "symbol": "PRACTICE-SHARE",
                                       "side": "buy", "qty": 1, "sl": round(bid + 1, 2)})
        fill = self.c.wait_event("order.filled", lambda d: d["client_id"] == "t-stop",
                                 timeout=5, since=mark)
        closed = self.c.wait_event("position.closed",
                                   lambda d: d["position_id"] == fill["position_id"],
                                   timeout=6, since=mark)
        self.assertEqual(closed["reason"], "sl")
        self.assertTrue(closed["close_id"])

    def test_flatten_all_closes_everything(self):
        mark = self.c.mark()
        for i in range(2):
            self.c.request("order.place", {"client_id": f"t-f{i}", "symbol": "PRACTICE-FX",
                                           "side": "sell", "qty": 0.01})
        self.assertEqual(self.c.request("flatten_all", {"client_id": "t-flat"}),
                         {"closed": 2})
        self.assertEqual(self.c.request("positions.list")["positions"], [])
        reasons = [d["reason"] for n, d in list(self.c.events)[mark:]
                   if n == "position.closed"]
        self.assertEqual(reasons, ["flatten", "flatten"])

    def test_bad_orders_are_refused_with_the_right_code(self):
        for params, code in (({"symbol": "AAPL", "qty": 1}, "unknown_symbol"),
                             ({"symbol": "PRACTICE-FX", "qty": 0.015}, "invalid_qty"),
                             ({"symbol": "PRACTICE-FX", "qty": 50}, "insufficient_margin")):
            with self.assertRaises(ConnectorError) as e:
                self.c.request("order.place", dict(params, client_id=code, side="buy"))
            self.assertEqual(e.exception.code, code)

    def test_one_coin_the_tickets_first_size_fits_the_practice_account(self):
        """The Terminal's ticket starts at 1 unit, and the sidebar's first
        instrument is the coin: that first order must not be refused for
        margin on the 10,000 USD account."""
        self.c.request("order.place", {"client_id": "t-coin1", "symbol": "PRACTICE-COIN",
                                       "side": "buy", "qty": 1})
        self.assertGreater(self.c.request("account.get")["margin_free"], 0)
        self.c.request("flatten_all")

    def test_positions_survive_a_restart(self):
        self.c.request("order.place", {"client_id": "t-keep", "symbol": "PRACTICE-COIN",
                                       "side": "buy", "qty": 0.01})
        self.c.close()
        again = self._connect()
        self.assertEqual([p["client_id"] for p in again.positions.values()], ["t-keep"])


class PracticePrices(unittest.TestCase):
    """History and live prices come from one path: each closed minute is
    exactly its sixty one-second quotes."""

    def test_a_minute_bar_is_its_sixty_quotes(self):
        spec = importlib.util.spec_from_file_location("starter_probe", STARTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        market = mod.PracticeMarket(None)
        for symbol in ("PRACTICE-FX", "PRACTICE-SHARE", "PRACTICE-COIN"):
            for t, o, h, low, c, _ in market.bars(symbol, "1m", 30):
                bids = [market._quote(symbol, s)[0] for s in range(t // 1000, t // 1000 + 60)]
                self.assertEqual([o, h, low, c], [bids[0], max(bids), min(bids), bids[-1]])


class Source(unittest.TestCase):
    src = STARTER.read_text(encoding="utf-8")

    def test_python_310_and_the_standard_library_only(self):
        tree = ast.parse(self.src, feature_version=(3, 10))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module.split(".")[0])
        self.assertEqual(names - set(sys.stdlib_module_names), {"brueconnect"})

    def test_each_broker_function_marks_where_the_broker_call_goes(self):
        tree = ast.parse(self.src)
        lines = self.src.splitlines()
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MyBroker")
        funcs = [f for f in cls.body if isinstance(f, ast.FunctionDef)
                 and f.name.startswith("broker_")]
        self.assertEqual(len(funcs), 11)
        for f in funcs:
            body = lines[f.lineno - 1:f.end_lineno]
            self.assertTrue(any(x.strip().startswith("# YOUR BROKER:") for x in body), f.name)
        self.assertEqual(sum(x.strip().startswith("# YOUR BROKER:") for x in lines), 11)


class Conformance(TempDir):
    def test_the_suite_finds_no_failure_as_delivered(self):
        """As a Terminal starts it: BRUE_CONNECT_SDK, a state folder, two
        keys, no PYTHONPATH; run from the SDK's folder, restartable. (The
        Terminal's own test line, PYTHONPATH and a bare file name from the
        folder it was saved in, is run by the Terminal's tests.)"""
        env = adapter_env(BRUE_BROKER_STATE=str(self.tmp / "state"),
                          BRUE_CRED_API_KEY="practice", BRUE_CRED_API_SECRET="practice")
        out = subprocess.run(
            [PYTHON, "-m", "brueconnect.conformance.run", "--restartable", "--adapter",
             f"{shlex.quote(PYTHON)} {shlex.quote(str(STARTER))}"],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=600)
        report = out.stdout
        self.assertEqual(out.returncode, 0, report + out.stderr)
        self.assertNotIn("[FAIL]", report)
        self.assertRegex(report, r"RESULT: \d+ pass, \d+ skip, 0 fail")
        # the two skips are the suite's honest ones: no interactive login and
        # no size that part-fills (the practice market fills in full)
        skips = [x for x in report.splitlines() if "[SKIP]" in x]
        self.assertEqual(len(skips), 2, report)
        self.assertTrue(any("interactive auth" in x for x in skips))
        self.assertTrue(any("partial fill" in x for x in skips))


if __name__ == "__main__":
    unittest.main()
