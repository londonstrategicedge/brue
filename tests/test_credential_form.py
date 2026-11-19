"""The credential form in the handshake (SPEC 3.2).

An adapter states what the user must type before it can connect; a platform
draws its connect form from that and hands each value over at spawn as
BRUE_CRED_<KEY>. The statement is third-party text bound for a UI, so it is
sanitised on both sides of the pipe, and it names fields without ever
carrying a value.

Run: python3 -m unittest tests.test_credential_form -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brueconnect import Adapter, Connector, protocol, registry  # noqa: E402


TWO = [{"key": "api_key_id", "label": "API key ID", "type": "text"},
       {"key": "api_secret", "label": "API secret", "type": "password"}]


class TestSanitise(unittest.TestCase):
    def test_a_good_form_survives_unchanged(self):
        self.assertEqual(protocol.sanitize_credential_form(TWO), TWO)

    def test_only_the_three_known_keys_pass(self):
        """A form field may not smuggle behaviour, a value or markup hooks."""
        clean = protocol.sanitize_credential_form([{
            "key": "api_key", "label": "API key", "type": "text",
            "value": "prefilled-secret", "default": "x", "help": "<b>hi</b>",
            "required": True, "options": ["a", "b"]}])
        self.assertEqual(clean,
                         [{"key": "api_key", "label": "API key",
                           "type": "text"}])

    def test_a_key_that_cannot_be_an_env_name_drops_its_field(self):
        bad = ["", "API_KEY", "1key", "_key", "api-key", "api key", "api.key",
               "api_key\n", "k" * 33, "a/b", "a\\b", "ключ", None, 7,
               ["api_key"], {"k": 1}]
        raw = [{"key": k, "label": "x", "type": "text"} for k in bad]
        self.assertEqual(protocol.sanitize_credential_form(raw), [])
        # the longest legal key is exactly 32 characters
        ok = "k" * 32
        self.assertEqual(
            protocol.sanitize_credential_form(
                [{"key": ok, "label": "x", "type": "text"}])[0]["key"], ok)

    def test_a_repeated_key_keeps_the_first(self):
        clean = protocol.sanitize_credential_form([
            {"key": "token", "label": "First", "type": "text"},
            {"key": "token", "label": "Second", "type": "password"}])
        self.assertEqual(clean,
                         [{"key": "token", "label": "First", "type": "text"}])

    def test_an_unknown_type_drops_the_field(self):
        raw = [{"key": "a", "label": "A", "type": t}
               for t in ("select", "TEXT", "none", "", None, 3, ["text"])]
        self.assertEqual(protocol.sanitize_credential_form(raw), [])

    def test_a_missing_type_is_the_masked_one(self):
        """Of the two types, the default is the one that cannot put a secret
        on screen because an author forgot a word."""
        clean = protocol.sanitize_credential_form(
            [{"key": "api_secret", "label": "Secret"}])
        self.assertEqual(clean[0]["type"], "password")

    def test_the_label_is_capped_cleaned_and_falls_back_to_the_key(self):
        clean = protocol.sanitize_credential_form([
            {"key": "a", "label": "L" * 200, "type": "text"},
            {"key": "b", "label": "line one\nline\ttwo\x07‮", "type": "text"},
            {"key": "c", "label": "", "type": "text"},
            {"key": "d", "type": "text"},
            {"key": "e", "label": {"not": "text"}, "type": "text"}])
        self.assertEqual(len(clean[0]["label"]), 40)
        self.assertEqual(clean[1]["label"], "line one line two")
        self.assertEqual([f["label"] for f in clean[2:]], ["c", "d", "e"])

    def test_markup_in_a_label_stays_text(self):
        """The sanitiser does not pretend to make HTML safe: the rule is that
        a platform renders the label as text. What it guarantees is that the
        string is short, visible and unchanged in meaning."""
        label = '<img src=x onerror=alert(1)> "q"'
        clean = protocol.sanitize_credential_form(
            [{"key": "a", "label": label, "type": "text"}])
        self.assertEqual(clean[0]["label"], label)

    def test_at_most_eight_fields(self):
        raw = [{"key": f"k{i}", "label": f"K{i}", "type": "text"}
               for i in range(20)]
        clean = protocol.sanitize_credential_form(raw)
        self.assertEqual([f["key"] for f in clean],
                         [f"k{i}" for i in range(8)])

    def test_bad_entries_do_not_use_up_the_eight(self):
        raw = ([None, "x", 3, {"key": "BAD"}]
               + [{"key": f"k{i}", "type": "text"} for i in range(8)])
        self.assertEqual(len(protocol.sanitize_credential_form(raw)), 8)

    def test_a_runaway_list_is_not_walked_to_its_end(self):
        raw = [None] * 100000 + [{"key": "late", "type": "text"}]
        self.assertEqual(protocol.sanitize_credential_form(raw), [])

    def test_junk_is_survivable(self):
        for junk in (None, {}, "nope", 7, {"key": "a"}, [[]], [None],
                     [{"key": {"a": 1}}], [{"type": "text"}]):
            self.assertEqual(protocol.sanitize_credential_form(junk), [])

    def test_sanitising_twice_changes_nothing(self):
        once = protocol.sanitize_credential_form(
            TWO + [{"key": "x", "label": "  padded   label  "}])
        self.assertEqual(protocol.sanitize_credential_form(once), once)


class TestAdapterSide(unittest.TestCase):
    def test_an_adapter_that_asks_for_nothing_sends_no_field(self):
        class Plain(Adapter):
            pass
        self.assertNotIn("credential_form", Plain()._hello({}))

    def test_the_declared_form_is_sent_in_hello(self):
        class Keyed(Adapter):
            credential_form = TWO
        self.assertEqual(Keyed()._hello({})["credential_form"], TWO)

    def test_it_is_sanitised_on_the_way_out_and_the_author_is_told(self):
        class Careless(Adapter):
            name = "careless"
            credential_form = [
                {"key": "API-KEY", "label": "bad key", "type": "text"},
                {"key": "pin", "label": "PIN", "type": "secret"},
                {"key": "token", "label": "T" * 90, "type": "password"}]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = Careless()._hello({})
        self.assertEqual(out["credential_form"],
                         [{"key": "token", "label": "T" * 40,
                           "type": "password"}])
        self.assertIn("kept 1 of 3", err.getvalue())
        self.assertIn("SPEC 3.2", err.getvalue())

    def test_a_form_that_raises_costs_the_form_not_the_connection(self):
        class Broken(Adapter):
            name = "broken"

            @property
            def credential_form(self):
                raise RuntimeError("config service down")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = Broken()._hello({})
        self.assertNotIn("credential_form", out)
        self.assertEqual(out["mode"], "paper")
        self.assertIn("credential_form failed", err.getvalue())

    def test_hello_never_carries_a_stored_value(self):
        """SPEC 8.5 is unchanged: the form names fields, the environment
        carries values, and nothing typed is ever in a message."""
        class Keyed(Adapter):
            credential_form = TWO
        saved = dict(os.environ)
        os.environ["BRUE_CRED_API_KEY_ID"] = "PKTESTVALUE0001"
        os.environ["BRUE_CRED_API_SECRET"] = "s3cr3t-value-0002"
        try:
            wire = json.dumps(Keyed()._hello({}))
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertNotIn("PKTESTVALUE0001", wire)
        self.assertNotIn("s3cr3t-value-0002", wire)


SDK_ADAPTER = """
    import os, sys
    sys.path.insert(0, {root!r})
    from brueconnect import Adapter

    class Keyed(Adapter):
        name, version, broker = "keyed", "0.1", "Keyed Broker"
        credential_form = [
            {{"key": "api_key_id", "label": "API key ID", "type": "text"}},
            {{"key": "api_secret", "label": "API secret", "type": "password"}}]

        def handle_account_get(self, params):
            # what the platform put in the environment at spawn, by NAME and
            # length only: the proof that values arrive this way
            return {{"seen": sorted(k for k in os.environ
                                   if k.startswith("BRUE_CRED_")),
                    "id_len": len(os.environ.get("BRUE_CRED_API_KEY_ID", ""))}}

    Keyed().run()
"""

# An adapter written WITHOUT the SDK, in the sense that matters here: nothing
# on its side sanitises anything. The platform side must not depend on the
# adapter being polite.
RAW_ADAPTER = """
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["method"] == "hello":
            result = {"protocol": 1, "adapter": {"name": "raw", "version": "1"},
                      "broker": "raw", "mode": "paper",
                      "account_currency": "USD", "account_model": "netting",
                      "capabilities": {"flatten_all": True},
                      "credential_form": HOSTILE}
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

    def test_the_connector_exposes_the_form_after_hello(self):
        path = self._write("keyed.py", SDK_ADAPTER.format(root=str(ROOT)))
        c = Connector([sys.executable, path])
        self.assertEqual(c.credential_form, [])    # nothing before hello
        try:
            c.hello()
            self.assertEqual(c.credential_form, TWO)
        finally:
            c.close()

    def test_the_connector_sanitises_what_a_raw_adapter_sends(self):
        hostile = [{"key": "ok_key", "label": "x" * 500, "type": "text",
                    "value": "leak"},
                   {"key": "../../etc", "label": "path", "type": "text"},
                   {"key": "pw", "label": "<script>1</script>",
                    "type": "password"},
                   "junk", {"key": "sel", "type": "select"}] + [
                   {"key": f"k{i}", "type": "text"} for i in range(30)]
        path = self._write("raw.py", RAW_ADAPTER.replace(
            "HOSTILE", repr(hostile)))
        c = Connector([sys.executable, path])
        try:
            c.hello()
            form = c.credential_form
        finally:
            c.close()
        self.assertEqual(len(form), 8)
        self.assertEqual(form[0], {"key": "ok_key", "label": "x" * 40,
                                   "type": "text"})
        self.assertEqual(form[1], {"key": "pw",
                                   "label": "<script>1</script>",
                                   "type": "password"})
        for f in form:
            self.assertEqual(sorted(f), ["key", "label", "type"])

    def test_a_platform_can_keep_the_last_words_of_an_adapter_that_died(self):
        """stderr is inherited unless the platform asks for it; asking is how
        a link screen shows the traceback instead of 'adapter disconnected'."""
        path = self._write("dies.py", """
            import sys
            sys.stderr.write("Traceback: this adapter cannot start\\n")
            sys.exit(3)
        """)
        with tempfile.TemporaryFile() as err:
            c = Connector([sys.executable, path], stderr=err)
            with self.assertRaises(Exception) as caught:
                c.hello(timeout=5.0)
            c.close()
            err.seek(0)
            self.assertIn(b"this adapter cannot start", err.read())
        self.assertIn("disconnected", str(caught.exception))
        self.assertIsNone(Connector(["x"]).stderr)      # default: inherited

    def test_a_handshake_without_the_field_is_an_empty_form(self):
        c = Connector([sys.executable, str(ROOT / "tests" / "mini_adapter.py")])
        try:
            c.hello()
            self.assertEqual(c.credential_form, [])
        finally:
            c.close()

    def test_what_the_user_typed_arrives_as_environment_at_spawn(self):
        """The whole loop a platform runs: read the form from the handshake,
        store what the user typed under the form's keys, spawn again, and the
        adapter finds each value in BRUE_CRED_<KEY>."""
        path = self._write("keyed.py", SDK_ADAPTER.format(root=str(ROOT)))
        registry.add("keyed", argv=[sys.executable, path])
        c = Connector.for_broker("keyed")
        try:
            c.hello()
            keys = [f["key"] for f in c.credential_form]
            self.assertEqual(c.request("account.get")["seen"], [])
        finally:
            c.close()
        registry.set_credentials("keyed", {keys[0]: "PK0001", keys[1]: "zz"})
        c = Connector.for_broker("keyed")
        try:
            c.hello()
            got = c.request("account.get")
        finally:
            c.close()
        self.assertEqual(got["seen"],
                         ["BRUE_CRED_API_KEY_ID", "BRUE_CRED_API_SECRET"])
        self.assertEqual(got["id_len"], 6)


if __name__ == "__main__":
    unittest.main()
