"""An adapter that answers a connect `reconnect` (SPEC 6, revision 1.11).

`hello` is answered cold (SPEC 3.1), so an adapter states its account's
currency and model before it has read them. When the session it opens shows
that the handshake was wrong, the adapter remembers what it read and answers
`reconnect`: it must be started again, and the next process's hello says the
truth. The kit's Connector does that by itself, inside the connect, once:

  - one connect() connects: the process is stopped, a new one started, its
    hello read and the connect's reads made again;
  - nothing of the last process stands, an arming made before the connect
    included (a restart keeps nothing of the last process);
  - a second `reconnect`, from the process started for the first, is an
    error in plain words, and nothing is started a third time;
  - an extension whose `like` is `reconnect` is treated as it; one without
    is not;
  - any read of the connect may say it, the resting orders' too, which is
    then not taken for a partial view;
  - outside a connect (a hello alone, a plain request, a reconciliation
    later in the session) it is raised as it is and nothing is started;
  - each process started is handed to on_start, so a platform that took the
    adapter's stderr drains the new process's pipe as well;
  - and the closed set of codes is the SPEC's own list.

Run: python3 -m unittest tests.test_reconnect -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brueconnect import Connector, ConnectorError, NotArmed, protocol  # noqa: E402

RELEARNING_ADAPTER = """
    import json, os, sys
    sys.path.insert(0, {root!r})
    from brueconnect import Adapter, AdapterError

    # An adapter whose handshake guesses its account's currency until it has
    # read it, as a cold hello must, and learns the truth when its session
    # opens. Its plan (a JSON file):
    #   code, like: its answer when the guess was wrong ("reconnect");
    #   at: where it finds out ("open": the session's opening; or a method);
    #   keeps: whether it remembers what it read, in its state folder;
    #   mode, pending: its handshake's mode, and resting orders declared;
    #   later: from this call of account.get on, it answers `reconnect`;
    #   chatter: the characters a process after the first writes to stderr
    #     before its catalog answer.
    # Each process counts itself in a file as it starts, logs each method it
    # is asked as "<process>:<method>", and after its hello says its session
    # state and prints a line that is not the protocol, both naming itself.
    STATE = os.environ["BRUE_BROKER_STATE"]
    with open(os.path.join(STATE, "count"), "a") as fh:
        fh.write(".")
    N = os.path.getsize(os.path.join(STATE, "count"))
    PLAN = json.load(open({plan!r}))
    TRUTH = "USD"
    LEARNED = os.path.join(STATE, "learned")
    SAID = open(LEARNED).read().strip() if os.path.exists(LEARNED) else "GBP"


    def note(text):
        with open({log!r}, "a") as fh:
            fh.write("%d:%s\\n" % (N, text))


    class Relearning(Adapter):
        name, version, broker = "relearning", "1", "Relearn Markets"
        mode = PLAN.get("mode", "paper")
        account_currency = SAID
        account_model = "netting"

        def __init__(self):
            super().__init__()
            self.reads = 0

        def capabilities(self):
            caps = super().capabilities()
            if PLAN.get("pending"):
                caps["orders"].update(pending=True, pending_types=["limit"],
                                      expiry=["gtc"])
            return caps

        def _dispatch(self, rid, method, params):
            note(method)
            super()._dispatch(rid, method, params)
            if method == "hello":
                self.emit("session.state", {{"state": "degraded",
                                             "detail": "process %d" % N}})
                print("process %d printed this" % N, flush=True)

        def wrong(self, where):
            if PLAN.get("at", "open") != where or SAID == TRUTH:
                return
            if PLAN.get("keeps", True):
                with open(LEARNED, "w") as fh:
                    fh.write(TRUTH)
            raise AdapterError(
                PLAN.get("code", "reconnect"),
                "the handshake said %s before the account was read; it is "
                "%s. That is now remembered: start this adapter again."
                % (SAID, TRUTH), like=PLAN.get("like"))

        def on_first_call(self):
            self.wrong("open")

        def handle_catalog_list(self, p):
            self.wrong("catalog.list")
            if N > 1 and PLAN.get("chatter"):
                sys.stderr.write("x" * PLAN["chatter"] + "\\n")
                sys.stderr.write("process %d wrote its lines\\n" % N)
                sys.stderr.flush()
            return {{"instruments": [{{"symbol": "TEST"}}]}}

        def handle_account_get(self, p):
            self.wrong("account.get")
            self.reads += 1
            if PLAN.get("later") and self.reads >= PLAN["later"]:
                raise AdapterError("reconnect", "the account changed")
            return {{"balance": 100.0, "equity": 100.0, "currency": SAID,
                     "process": N}}

        def handle_positions_list(self, p):
            return {{"positions": []}}

        def handle_orders_list(self, p):
            self.wrong("orders.list")
            return {{"orders": []}}

        def handle_order_place(self, p):
            note("carried-out")
            return {{"order_id": "o1", "status": "filled"}}


    Relearning().run()
"""


class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.mkdir(self.state)
        self.log = os.path.join(self.tmp.name, "seen.txt")

    def connector(self, stderr=None, **plan):
        plan_path = os.path.join(self.tmp.name, "plan.json")
        with open(plan_path, "w") as fh:
            json.dump(plan, fh)
        path = os.path.join(self.tmp.name, "relearning.py")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(RELEARNING_ADAPTER.format(
                root=ROOT, plan=plan_path, log=self.log)))
        env = dict(os.environ, BRUE_BROKER_STATE=self.state)
        c = Connector([sys.executable, path], env=env, stderr=stderr)
        self.addCleanup(self._close, c)
        return c

    @staticmethod
    def _close(c):
        c.close()
        for pipe in (getattr(c.proc, "stdin", None),
                     getattr(c.proc, "stdout", None),
                     getattr(c.proc, "stderr", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # noqa: BLE001
                pass

    def starts(self) -> int:
        """How many adapter processes have started."""
        try:
            return os.path.getsize(os.path.join(self.state, "count"))
        except FileNotFoundError:
            return 0

    def seen(self, process: int) -> list:
        """What adapter process number `process` was asked, in order."""
        try:
            with open(self.log) as fh:
                rows = [ln.split(":", 1) for ln in fh.read().split()]
        except FileNotFoundError:
            return []
        return [m for n, m in rows if int(n) == process]

    def learned_already(self):
        with open(os.path.join(self.state, "learned"), "w") as fh:
            fh.write("USD")

    # -- one connect --------------------------------------------------------
    def test_one_connect_starts_the_adapter_again_and_connects(self):
        c = self.connector()
        hs = c.connect()
        self.assertEqual(hs["account_currency"], "USD")
        self.assertEqual(c.handshake["account_currency"], "USD")
        self.assertEqual(c.identity()["account_currency"], "USD")
        self.assertEqual(self.starts(), 2)
        # the first process: its hello, the read it refused, its goodbye
        self.assertEqual(self.seen(1), ["hello", "catalog.list", "bye"])
        # the second: the whole connect, nothing more
        self.assertEqual(self.seen(2), ["hello", "catalog.list",
                                        "account.get", "positions.list"])
        self.assertEqual(c.account["process"], 2)
        self.assertEqual(list(c.catalog), ["TEST"])
        self.assertTrue(c.alive)

    def test_a_restart_keeps_nothing_of_the_last_process(self):
        c = self.connector(mode="live")
        # armed before the connect: consent given while the first process
        # was there does not cover the one started for it
        c.arm("armed before the connect")
        c.connect()
        self.assertEqual(self.starts(), 2)
        self.assertFalse(c.armed)
        with self.assertRaises(NotArmed):
            c.request("order.place", {"client_id": "k1", "symbol": "TEST",
                                      "side": "buy", "qty": 1})
        self.assertNotIn("carried-out", self.seen(2))
        # what the first process said and printed is not the connection's
        c.wait_event("session.state", lambda d: d["detail"] == "process 2")
        self.assertEqual(c.session_state["detail"], "process 2")
        lines = [ln for ln in list(c.stray_lines) if "printed this" in ln]
        deadline = time.monotonic() + 5
        while not lines and time.monotonic() < deadline:
            time.sleep(0.05)
            lines = [ln for ln in list(c.stray_lines) if "printed this" in ln]
        self.assertEqual(lines, ["process 2 printed this"])
        # armed now, on the process whose hello was read, it trades
        c.arm("armed after the connect")
        c.request("order.place", {"client_id": "k2", "symbol": "TEST",
                                  "side": "buy", "qty": 1})
        self.assertEqual(self.seen(2).count("carried-out"), 1)

    def test_a_second_reconnect_is_an_error_and_nothing_starts_a_third_time(
            self):
        c = self.connector(keeps=False)
        with self.assertRaises(ConnectorError) as e:
            c.connect()
        err = e.exception
        self.assertEqual((err.code, err.retryable), ("internal", False))
        text = str(err)
        self.assertIn("Relearn Markets", text)
        self.assertIn("started again", text)
        self.assertIn("not started a third time", text)
        # the adapter's own words kept, for whoever reads the error
        self.assertIn("That is now remembered: start this adapter again.",
                      text)
        self.assertEqual(self.starts(), 2)
        time.sleep(1.0)
        self.assertEqual(self.starts(), 2)
        self.assertEqual(self.seen(2), ["hello", "catalog.list"])

    def test_an_extension_like_reconnect_is_treated_as_it(self):
        c = self.connector(code="x_relearn", like="reconnect")
        c.connect()
        self.assertEqual(self.starts(), 2)
        self.assertEqual(c.handshake["account_currency"], "USD")

    def test_an_extension_without_like_is_not(self):
        c = self.connector(code="x_relearn")
        with self.assertRaises(ConnectorError) as e:
            c.connect()
        self.assertEqual(e.exception.code, "x_relearn")
        self.assertEqual(self.starts(), 1)

    def test_a_later_read_of_the_connect_can_say_it(self):
        c = self.connector(at="account.get")
        c.connect()
        self.assertEqual(self.starts(), 2)
        self.assertEqual(self.seen(1), ["hello", "catalog.list",
                                        "account.get", "bye"])
        self.assertEqual(c.account["process"], 2)

    def test_resting_orders_answered_so_are_not_taken_for_a_partial_view(
            self):
        c = self.connector(at="orders.list", pending=True)
        c.connect()
        self.assertEqual(self.starts(), 2)
        self.assertFalse(c.orders_partial)
        self.assertEqual(self.seen(2), ["hello", "catalog.list",
                                        "account.get", "positions.list",
                                        "orders.list"])

    # -- outside a connect ---------------------------------------------------
    def test_a_hello_alone_or_a_plain_request_starts_nothing_again(self):
        c = self.connector()
        c.hello()
        with self.assertRaises(ConnectorError) as e:
            c.request("catalog.list")
        # the protocol's own code, not an extension made of it
        self.assertEqual((e.exception.code, e.exception.retryable),
                         ("reconnect", False))
        self.assertIn("That is now remembered", str(e.exception))
        self.assertEqual(self.starts(), 1)

    def test_later_in_the_session_it_is_raised_as_it_is(self):
        self.learned_already()
        c = self.connector(later=2)
        c.connect()
        self.assertEqual(self.starts(), 1)
        with self.assertRaises(ConnectorError) as e:
            c.reconcile()
        self.assertEqual(e.exception.code, "reconnect")
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        self.assertEqual(e.exception.code, "reconnect")
        time.sleep(0.5)
        self.assertEqual(self.starts(), 1)

    # -- each process handed on ------------------------------------------------
    def test_each_process_started_is_handed_to_on_start(self):
        got = []
        c = self.connector()
        c.on_start(got.append)
        c.connect()
        self.assertEqual(len(got), 2)
        self.assertIs(got[1], c.proc)
        self.assertIsNotNone(got[0].poll())       # the first has ended
        self.assertIsNone(got[1].poll())

    def test_a_platform_that_took_the_stderr_drains_each_process(self):
        """The second process writes a megabyte to stderr before it answers
        its catalog read: undrained, its pipe fills and it never answers."""
        drained = []

        def drain(proc):
            # by line: a text pipe's read(n) waits for n characters or the end
            def pump():
                for line in iter(proc.stderr.readline, ""):
                    drained.append(line)
            threading.Thread(target=pump, daemon=True).start()

        c = self.connector(stderr=subprocess.PIPE, chatter=1_000_000)
        c.on_start(drain)
        c.connect()
        self.assertEqual(self.starts(), 2)
        deadline = time.monotonic() + 5
        while (not "".join(drained).endswith("process 2 wrote its lines\n")
               and time.monotonic() < deadline):
            time.sleep(0.05)
        self.assertTrue("".join(drained).endswith(
            "process 2 wrote its lines\n"))

    # -- the code itself ---------------------------------------------------------
    def test_the_closed_set_is_the_specs_own_list(self):
        with open(os.path.join(ROOT, "SPEC.md"), encoding="utf-8") as fh:
            spec = fh.read()
        # the list after "from this closed set (extensions must be prefixed
        # `x_`):", in one pair of backticks
        m = re.search(r"from this closed set.*?\):\s*`([^`]+,[^`]+)`", spec,
                      re.S)
        self.assertIsNotNone(m)
        listed = {c.strip() for c in m.group(1).split(",")}
        self.assertGreater(len(listed), 19)
        self.assertEqual(listed, set(protocol.ERROR_CODES))
        self.assertIn("reconnect", protocol.ERROR_CODES)
        msg = protocol.parse_line(protocol.res_error("r", "reconnect", "m"))
        self.assertEqual(msg["error"]["code"], "reconnect")


if __name__ == "__main__":
    unittest.main()
