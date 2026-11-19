"""The OANDA adapter's cold guess, settled inside one connect (SPEC 6,
`reconnect`).

`hello` must state the account's currency and model before anything is read
(SPEC 3.1), so with nothing remembered the adapter guesses GBP and netting,
OANDA Europe's default. A token whose one account is not (a US-division
practice token: USD, netting) used to need two connects: the first was
refused with `x_oanda_reconnect`, the second connected. The refusal is now
the protocol's `reconnect`, and the kit's Connector starts the adapter again
by itself, once, inside the one connect().

Against the stand-in (tests/fake_oanda.py), with OANDA's own limits on:
  - the one-account US token connects on one connect(), as USD netting;
  - the two processes ask OANDA what the two connects asked before and
    nothing more: the first its accounts and the summary, the second a
    whole session's opening and reads;
  - a token that reaches several accounts, none chosen, still waits on its
    chooser: refused, and started once;
  - one bound to an account whose currency and model were guessed wrong
    connects on one connect();
  - without a state folder the adapter cannot remember, so the process
    started again asks again: an error in plain words, two processes;
  - and the refusal itself is `reconnect` in the adapter's own words, for a
    platform that does not use the Connector.

Run: python3 -m unittest tests.test_oanda_reconnect -v
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brueconnect import Connector, ConnectorError  # noqa: E402
from tests.test_oanda_standin import ADAPTER, StandIn  # noqa: E402

# A read the transaction stream's thread makes from its first moment, which
# may land before or after connect() returns.
BACKGROUND = ("/transactions/sinceid", "/transactions/stream", "/pricing")


class _Reconnect(StandIn):
    def connector(self, **extra):
        """An OANDA adapter as the Terminal runs a linked row: practice, the
        stand-in's token, unbound unless BRUE_ACCOUNT_ID is given, nothing
        remembered. Its processes are counted as they start."""
        err = open(os.path.join(self.dir, "stderr-%d.txt" % len(self.files)),
                   "w")
        self.files.append(err)
        env = self.env(None, **extra)
        c = Connector([sys.executable, ADAPTER, "--paper"], env=env,
                      stderr=err)
        self.conns.append(c)
        c.started = []

        def note(proc, c=c):
            c.started.append((proc, len(self.fake.requests())))

        if hasattr(c, "on_start"):
            c.on_start(note)
        else:
            # a kit from before on_start: its own starts, counted as made
            start = c.start

            def counted(c=c, start=start):
                start()
                note(c.proc)

            c.start = counted
        return c

    def rest(self):
        """The stand-in's REST requests so far, as (method, path, status),
        the background reads left out."""
        return [(r["method"], r["path"].split("?")[0], r["status"])
                for r in self.fake.requests()
                if r["host"] == "rest"
                and not r["path"].split("?")[0].endswith(BACKGROUND)]


class OneAccount(_Reconnect):
    division = "us"

    def test_a_one_account_token_connects_on_one_connect(self):
        aid = self.fake.account_ids[0]
        self.fake.requests(clear=True)
        c = self.connector()
        hs = c.connect()
        self.assertEqual((hs["account_currency"], hs["account_model"]),
                         ("USD", "netting"))
        self.assertEqual(c.account["currency"], "USD")
        self.assertEqual(c.account["account_id"], aid)
        self.assertEqual(len(c.started), 2)
        # The first process asked for the accounts and the summary, opened
        # no stream, and refused; it has ended. OANDA refused none of it.
        self.assertEqual(c.started[0][1], 0)
        before_second = self.fake.requests()[:c.started[1][1]]
        self.assertEqual(
            [(r["host"], r["method"], r["path"].split("?")[0], r["status"])
             for r in before_second],
            [("rest", "GET", "/v3/accounts", 200),
             ("rest", "GET", "/v3/accounts/%s/summary" % aid, 200)])
        self.assertIsNotNone(c.started[0][0].poll())
        # the session the second process opened says what it is
        said = c.wait_event("session.state", lambda d: "(netting, USD)"
                            in str(d.get("detail")), timeout=5)
        self.assertIn(aid, said["detail"])

    def test_the_process_started_again_waits_its_turn_to_connect(self):
        """OANDA allows two new connections a second per address, counted
        with every process there: the process started again opens its own
        right after the first process's, so it waits its turn, and OANDA
        refuses none of them (a refused one is a request the two connects
        did not need)."""
        self.fake.requests(clear=True)
        c = self.connector()
        c.connect()
        c.request("quotes.subscribe", {"symbols": ["EUR_USD"]})
        time.sleep(4.0)
        refused = [(r["host"], r["path"].split("?")[0])
                   for r in self.fake.requests()
                   if r["status"] == 0 and not r.get("authorized")]
        self.assertEqual(refused, [])
        self.assertEqual(c.session_state.get("state"), "connected",
                         c.session_state)
        self.assertEqual(len(c.started), 2)

    def test_the_two_processes_ask_what_the_two_connects_asked(self):
        """No broker request beyond what the two connects need: the same
        REST requests, in the same order, as the two presses before."""
        aid = self.fake.account_ids[0]
        # the two connects, as they were: one refused, then one more
        self.fake.requests(clear=True)
        os.makedirs(os.path.join(self.dir, "before"))
        before = self.connector(BRUE_BROKER_STATE=os.path.join(self.dir,
                                                               "before"))
        before.hello()
        with self.assertRaises(ConnectorError) as e:
            before.request("catalog.list")
        self.assertEqual(e.exception.code, "reconnect")
        before.close()
        again = self.connector(BRUE_BROKER_STATE=os.path.join(self.dir,
                                                              "before"))
        again.connect()
        two_presses = self.rest()
        again.close()
        time.sleep(1.1)      # OANDA's two new connections a second
        # the one connect, from nothing remembered
        self.fake.requests(clear=True)
        os.makedirs(os.path.join(self.dir, "now"))
        c = self.connector(BRUE_BROKER_STATE=os.path.join(self.dir, "now"))
        c.connect()
        one_connect = self.rest()
        self.assertEqual(one_connect, two_presses)
        self.assertEqual(one_connect[:2], [
            ("GET", "/v3/accounts", 200),
            ("GET", "/v3/accounts/%s/summary" % aid, 200)])
        # nothing that changes anything at OANDA
        self.assertEqual([r for r in one_connect if r[0] != "GET"], [])


class SeveralAccounts(_Reconnect):
    division = "uk"

    def test_a_token_with_several_accounts_still_waits_for_a_choice(self):
        c = self.connector()
        with self.assertRaises(ConnectorError) as e:
            c.connect()
        self.assertEqual(e.exception.code, "x_oanda_choose_account")
        self.assertEqual(len(c.started), 1)
        rows = c.request("accounts.list")["accounts"]
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(r["selected"] for r in rows))

    def test_a_bound_account_guessed_wrong_connects_on_one_connect(self):
        c = self.connector(BRUE_ACCOUNT_ID=self.fake.account_ids[1])
        hs = c.connect()
        self.assertEqual((hs["account_model"], hs["account_currency"]),
                         ("hedging", "USD"))
        self.assertEqual(c.account["currency"], "USD")
        self.assertEqual(len(c.started), 2)

    def test_with_nothing_to_remember_in_the_second_guess_is_an_error(self):
        c = self.connector(BRUE_ACCOUNT_ID=self.fake.account_ids[1],
                           BRUE_BROKER_STATE="")
        with self.assertRaises(ConnectorError) as e:
            c.connect()
        err = e.exception
        self.assertEqual((err.code, err.retryable), ("internal", False))
        self.assertIn("OANDA", str(err))
        self.assertIn("not started a third time", str(err))
        self.assertIn("reconnect, and the handshake will say so", str(err))
        self.assertEqual(len(c.started), 2)
        time.sleep(1.0)
        self.assertEqual(len(c.started), 2)

    def test_the_refusal_is_reconnect_in_the_adapters_own_words(self):
        c = self.connector(BRUE_ACCOUNT_ID=self.fake.account_ids[1])
        c.hello()
        self.assertEqual(c.handshake["account_model"], "netting")
        with self.assertRaises(ConnectorError) as e:
            c.request("account.get")
        err = e.exception
        self.assertEqual((err.code, err.like, err.retryable),
                         ("reconnect", None, False))
        self.assertIn("this adapter's handshake said netting and GBP before "
                      "it had read the account", str(err))
        self.assertIn("That is now remembered: reconnect, and the handshake "
                      "will say so.", str(err))
        # a plain request is not a connect: nothing was started again
        self.assertEqual(len(c.started), 1)


if __name__ == "__main__":
    unittest.main()
