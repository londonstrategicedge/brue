"""Account pin vs adapter state: an adapter-cached account id must never
become a pin.

The registry's account.json is the platform's EXPLICIT account choice
(SPEC 1.7). An adapter that keeps its own state (a resolved account, a
token) under that filename must not have that state read back as a hard pin
after a key switch, and a platform account pick must not clobber an
adapter's stored credentials. These tests replay the switch flows offline
and pin the contract:

* a pin comes ONLY from an explicit choice, never from adapter state;
* an lse-sim key switch binds the new login's primary account, no refusal;
* an explicit pin is still honoured, and still refused loudly when absent;
* sablepoint / pemberton keep their accounts across the state-file rename.

Run: python3 -m unittest tests.test_account_pin -v
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from brueconnect import registry  # noqa: E402
from brueconnect.adapter import AdapterError  # noqa: E402


class Env:
    """Set env vars for one test, restore after."""

    def __init__(self, **vals):
        self.vals = vals
        self.saved = {}

    def __enter__(self):
        for k, v in self.vals.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


class TestRegistryPin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(BRUE_CONNECT_HOME=self.tmp.name)
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__()
        self.tmp.cleanup()

    def _write(self, payload):
        path = os.path.join(registry.state_dir("lse-sim"), "account.json")
        with open(path, "w") as fh:
            json.dump(payload, fh)

    def test_adapter_cache_is_not_a_pin(self):
        # An adapter cache that happens to carry account_id is not a pin.
        self._write({"api": "https://api.example", "currency": "USD",
                     "account_id": 51})
        self.assertEqual(registry.get_account("lse-sim"), "")
        self.assertEqual(registry.account_env("lse-sim"), {})

    def test_sablepoint_token_is_not_a_pin(self):
        self._write({"api_token": "t", "account_id": 7, "addr": "x:1"})
        self.assertEqual(registry.get_account("lse-sim"), "")

    def test_single_key_explicit_choice_still_pins(self):
        # Earlier releases' set_account wrote exactly this single-key shape.
        self._write({"account_id": "70001"})
        self.assertEqual(registry.get_account("lse-sim"), "70001")
        self.assertEqual(registry.account_env("lse-sim"),
                         {"BRUE_ACCOUNT_ID": "70001"})

    def test_set_account_roundtrip_and_forget(self):
        registry.set_account("lse-sim", "99731")
        self.assertEqual(registry.get_account("lse-sim"), "99731")
        registry.set_account("lse-sim", "")
        self.assertEqual(registry.get_account("lse-sim"), "")


class TestLseSimKeySwitch(unittest.TestCase):
    """Key switch flows, replayed offline against a stubbed venue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _adapter(self, accounts, pin=None):
        from brueconnect.adapters.lse_sim import lse_sim_adapter as mod
        with Env(BRUE_BROKER_STATE=self.tmp.name, LSE_API_KEY="k",
                 BRUE_ACCOUNT_ID=pin, LSE_SIM_ACCOUNT=None):
            a = mod.LSESimAdapter()
        a._http = lambda method, url, body=None, base=None: (
            accounts if url == "/sim/accounts" else {})
        # The stream is not under test; report it live immediately.
        a._stream_forever = lambda: a._stream_ready.set()
        return a

    def test_key_switch_binds_new_logins_primary(self):
        # The previous key's session cached account 51 in the older shape.
        prior = os.path.join(self.tmp.name, "account.json")
        with open(prior, "w") as fh:
            json.dump({"api": "https://api.londonstrategicedge.com",
                       "currency": "USD", "account_id": 51}, fh)
        # The registry must NOT turn that cache into a pin for the spawn env.
        with Env(BRUE_CONNECT_HOME=os.path.join(self.tmp.name, "home")):
            os.makedirs(os.path.join(self.tmp.name, "home", "state",
                                     "lse-sim"), exist_ok=True)
        # New key's login has one fresh account; no pin arrives in the env.
        a = self._adapter([{"id": 99731, "name": "demo-1",
                            "currency": "USD"}])
        a.on_first_call()
        self.assertEqual(a.account_id, 99731)
        # The cache never writes account_id, and the older file is removed
        # so it can never read as a choice later.
        with open(os.path.join(self.tmp.name, "cache.json")) as fh:
            self.assertNotIn("account_id", json.load(fh))
        self.assertFalse(os.path.exists(prior))

    def test_explicit_pin_still_honoured(self):
        a = self._adapter([{"id": 51, "name": "demo-1", "currency": "USD"},
                           {"id": 70001, "name": "conformance",
                            "currency": "USD"}], pin="70001")
        a.on_first_call()
        self.assertEqual(a.account_id, 70001)

    def test_explicit_pin_absent_still_refuses(self):
        # The strict pin rule must survive: a pinned account that the login
        # cannot see is a loud refusal, never a silent rebind.
        a = self._adapter([{"id": 99731, "name": "demo-1",
                            "currency": "USD"}], pin="51")
        with self.assertRaises(AdapterError):
            a.on_first_call()

    def test_platform_choice_file_not_deleted(self):
        # An explicit choice in the older location must survive _save_cache.
        prior = os.path.join(self.tmp.name, "account.json")
        with open(prior, "w") as fh:
            json.dump({"account_id": "70001", "chosen": True}, fh)
        a = self._adapter([{"id": 70001, "name": "conformance",
                            "currency": "USD"}], pin="70001")
        a.on_first_call()
        self.assertTrue(os.path.exists(prior))


class TestSablePointRename(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self):
        from brueconnect.adapters.sablepoint.sablepoint_adapter import (
            SablePointClient)
        return SablePointClient("127.0.0.1:7671", self.tmp.name)

    def test_older_token_file_survives_rename(self):
        with open(os.path.join(self.tmp.name, "account.json"), "w") as fh:
            json.dump({"api_token": "tok", "account_id": 5,
                       "addr": "127.0.0.1:7671"}, fh)
        c = self._client()
        self.assertTrue(c.load_token())
        self.assertEqual(c.token, "tok")
        # Storing writes the new name and removes the older file.
        c.store_token()
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp.name, "token.json")))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp.name, "account.json")))
        c2 = self._client()
        self.assertTrue(c2.load_token())
        self.assertEqual(c2.token, "tok")


class TestPembertonRename(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_older_credentials_file_survives_rename(self):
        from brueconnect.adapters.pemberton.pemberton_adapter import (
            PembertonClient)
        with open(os.path.join(self.tmp.name, "account.json"), "w") as fh:
            json.dump({"account": "A1", "pin": "1234",
                       "base": "https://eos.example"}, fh)
        c = PembertonClient("https://eos.example", self.tmp.name)
        c._open_account = lambda: self.fail(
            "existing account was not loaded; a new one would orphan it")
        c.load_account()
        self.assertEqual((c.account, c.pin), ("A1", "1234"))


if __name__ == "__main__":
    unittest.main()
