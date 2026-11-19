"""The registry: profiles stored as argv, strict names, atomic private files.

Three things a profile file must get right once anyone but its author can
write to it (a platform's "add your own broker" does exactly that):

* what it spawns is exactly what was registered, on every operating system:
  a path with a space or a Windows backslash is one argument, not shell text;
* a name is a directory name and can never leave the state directory;
* the file and the stored keys are written whole or not at all, and are
  readable by their owner only.

Every test runs against a throwaway BRUE_CONNECT_HOME.

Run: python3 -m unittest tests.test_registry -v
"""
import contextlib
import io
import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brueconnect import Connector, registry  # noqa: E402
from brueconnect.__main__ import main as cli  # noqa: E402

MINI = str(ROOT / "tests" / "mini_adapter.py")


class Home(unittest.TestCase):
    """A scratch registry home for each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "brue-home")
        self.saved = dict(os.environ)
        os.environ["BRUE_CONNECT_HOME"] = self.home
        os.environ.pop("BRUE_CONNECT_PYTHON", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def file(self):
        with open(os.path.join(self.home, "brokers.json")) as fh:
            return json.load(fh)

    def write_file(self, raw):
        os.makedirs(self.home, exist_ok=True)
        with open(os.path.join(self.home, "brokers.json"), "w") as fh:
            json.dump(raw, fh)

    def leftovers(self, folder=None):
        folder = folder or self.home
        return [n for n in os.listdir(folder) if n.endswith(".tmp")]


class TestArgv(Home):
    def test_a_list_is_stored_and_spawned_exactly(self):
        argv = ["python3", "/home/me/my adapters/it's mine.py", "--paper",
                "--label", "two words", ""]
        registry.add("mine", argv=argv, description="Mine")
        stored = self.file()["brokers"]["mine"]
        self.assertEqual(stored["argv"], argv)
        # the string beside it means the same list to an older reader
        self.assertEqual(shlex.split(stored["command"]), argv)
        self.assertEqual(registry.load()["brokers"]["mine"]["argv"], argv)
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/python3"):
            self.assertEqual(registry.resolve("mine")[0], argv)

    def test_a_windows_path_survives_as_one_argument(self):
        """The defect this exists for: POSIX splitting eats the backslashes
        of a command typed on Windows."""
        py = r"C:\Program Files\Python312\python.exe"
        path = r"C:\Users\Jo Smith\adapters\my_adapter.py"
        self.assertEqual(shlex.split(f"{py} {path}"),
                         ["C:Program", "FilesPython312python.exe",
                          "C:UsersJo", "Smithadaptersmy_adapter.py"])
        registry.add("winlist", argv=[py, path, "--paper"])
        self.assertEqual(registry.resolve("winlist")[0],
                         [py, path, "--paper"])
        self.assertEqual(
            shlex.split(self.file()["brokers"]["winlist"]["command"]),
            [py, path, "--paper"])

    def test_a_list_given_as_the_command_is_taken_as_argv(self):
        registry.add("aslist", ["python3", "/a b/c.py"])
        self.assertEqual(self.file()["brokers"]["aslist"]["argv"],
                         ["python3", "/a b/c.py"])

    def test_a_posix_command_string_is_kept_as_typed(self):
        cmd = 'python3  "/opt/my adapters/a.py"   --tick-ms 20'
        registry.add("typed", cmd)
        stored = self.file()["brokers"]["typed"]
        self.assertEqual(stored["command"], cmd)
        self.assertEqual(stored["argv"], ["python3", "/opt/my adapters/a.py",
                                          "--tick-ms", "20"])

    def test_a_windows_command_line_is_split_by_windows_rules(self):
        line = r'python C:\Users\me\adapters\a.py "C:\My Data\cfg.json" --x'
        want = ["python", r"C:\Users\me\adapters\a.py",
                r"C:\My Data\cfg.json", "--x"]
        self.assertEqual(registry.split_command(line, windows=True), want)
        self.assertNotEqual(registry.split_command(line, windows=False), want)
        # a quoted interpreter path, the usual shape under Program Files
        self.assertEqual(
            registry.split_command(
                r'"C:\Program Files\Python312\python.exe" C:\a.py',
                windows=True),
            [r"C:\Program Files\Python312\python.exe", r"C:\a.py"])

    def test_add_on_windows_stores_the_windows_reading(self):
        line = r"python C:\Users\me\adapters\a.py --paper"
        want = ["python", r"C:\Users\me\adapters\a.py", "--paper"]
        real = registry.split_command
        with mock.patch.object(registry, "split_command",
                               lambda c: real(c, windows=True)):
            registry.add("winline", line)
        stored = self.file()["brokers"]["winline"]
        self.assertEqual(stored["argv"], want)
        # rewritten so a reader that only knows the string gets the same list
        self.assertEqual(shlex.split(stored["command"]), want)
        self.assertEqual(registry.profile_argv(
            registry.load()["brokers"]["winline"]), want)

    def test_an_unbalanced_quote_is_refused_not_guessed(self):
        with self.assertRaises(registry.RegistryError):
            registry.add("broken", 'python3 "/unterminated')
        self.assertFalse(os.path.exists(os.path.join(self.home,
                                                     "brokers.json")))

    def test_a_command_is_required(self):
        for bad in (None, "", "   ", [], [""], ["python3", 7], ["a\x00b"]):
            with self.assertRaises(registry.RegistryError, msg=repr(bad)):
                registry.add("nocmd", bad)

    def test_a_file_from_before_argv_keeps_working(self):
        self.write_file({"brokers": {"old-one": {
            "command": 'python3 "/opt/my adapters/a.py" --x',
            "env": {"A": "{state}/f"}, "description": "old"}},
            "default": "old-one"})
        reg = registry.load()
        self.assertNotIn("argv", reg["brokers"]["old-one"])
        self.assertEqual(reg["default"], "old-one")
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/python3"):
            argv, env, sdir = registry.resolve("old-one")
        self.assertEqual(argv, ["python3", "/opt/my adapters/a.py", "--x"])
        self.assertEqual(env["A"], sdir + "/f")
        self.assertEqual(env["BRUE_BROKER_STATE"], sdir)

    def test_argv_wins_over_the_string_beside_it(self):
        self.write_file({"brokers": {"both": {
            "command": "python3 /from/the/string.py",
            "argv": ["/usr/bin/env", "python3", "/from/the list.py"]}}})
        self.assertEqual(registry.resolve("both")[0],
                         ["/usr/bin/env", "python3", "/from/the list.py"])

    def test_a_malformed_argv_falls_back_to_the_string(self):
        for bad in ("python3 /x.py", [], ["python3", 5], {"0": "python3"},
                    None, 3):
            self.write_file({"brokers": {"odd": {
                "command": "/usr/bin/env true", "argv": bad}}})
            self.assertEqual(registry.resolve("odd")[0],
                             ["/usr/bin/env", "true"], msg=repr(bad))

    def test_an_entry_with_only_argv_is_a_profile(self):
        self.write_file({"brokers": {"listonly": {
            "argv": ["/usr/bin/env", "a b"]}}})
        prof = registry.load()["brokers"]["listonly"]
        self.assertEqual(shlex.split(prof["command"]), ["/usr/bin/env", "a b"])

    def test_an_entry_with_neither_is_still_an_error(self):
        self.write_file({"brokers": {"empty": {"env": {}}}})
        with self.assertRaises(registry.RegistryError):
            registry.load()

    def test_a_registered_file_really_spawns(self):
        registry.add("mini", argv=[sys.executable, MINI])
        c = Connector.for_broker("mini")
        try:
            self.assertEqual(c.hello()["adapter"]["name"], "mini")
        finally:
            c.close()


class TestInterpreter(Home):
    def test_a_bare_python_that_exists_is_left_alone(self):
        registry.add("bare", argv=["python3", MINI])
        with mock.patch.object(registry.shutil, "which",
                               return_value="/usr/bin/python3"):
            self.assertEqual(registry.resolve("bare")[0][0], "python3")

    def test_a_bare_python_this_machine_lacks_becomes_ours(self):
        """Stock Windows Python installs no `python3`; a profile stored with
        that word must still start."""
        registry.add("bare", argv=["python3", MINI, "--x"])
        with mock.patch.object(registry.shutil, "which", return_value=None):
            argv = registry.resolve("bare")[0]
        self.assertEqual(argv, [sys.executable, MINI, "--x"])

    def test_the_search_uses_the_path_the_adapter_will_get(self):
        registry.add("bare", argv=["python3", MINI],
                     env={"PATH": "/nonexistent-dir-for-test"})
        self.assertEqual(registry.resolve("bare")[0][0], sys.executable)

    def test_a_chosen_interpreter_is_never_replaced(self):
        for word in ("/opt/venv/bin/python3", "node", "./python3",
                     r"C:\Py\python.exe"):
            registry.add("chosen", argv=[word, MINI])
            with mock.patch.object(registry.shutil, "which",
                                   return_value=None):
                self.assertEqual(registry.resolve("chosen")[0][0], word)

    def test_a_frozen_host_has_no_python_of_its_own_to_offer(self):
        registry.add("bare", argv=["python3", MINI])
        with mock.patch.object(registry.sys, "frozen", True, create=True), \
                mock.patch.object(registry.shutil, "which",
                                  return_value=None):
            self.assertEqual(registry.resolve("bare")[0][0], "python3")


class TestBuiltins(Home):
    def test_builtins_carry_an_argv_of_two_whole_paths(self):
        prof = registry.load()["brokers"]["paper"]
        self.assertEqual(prof["argv"][0], sys.executable)
        self.assertTrue(prof["argv"][1].endswith(
            os.path.join("adapters", "paper", "paper_adapter.py")))
        self.assertEqual(len(prof["argv"]), 2)

    def test_an_interpreter_path_with_a_space_no_longer_breaks_them(self):
        py = "/opt/my tools/python3"
        os.environ["BRUE_CONNECT_PYTHON"] = py
        prof = registry.load()["brokers"]["lse-sim"]
        # the string form is what used to be split, into three words
        self.assertEqual(len(shlex.split(prof["command"])), 3)
        argv = registry.resolve("lse-sim")[0]
        self.assertEqual(argv[0], py)
        self.assertEqual(len(argv), 2)
        self.assertTrue(os.path.isfile(argv[1]))

    def test_a_package_path_with_a_space_or_backslash_is_one_argument(self):
        with tempfile.TemporaryDirectory() as d:
            odd = os.path.join(d, "Program Files", "x\\y", "paper_adapter.py")
            os.makedirs(os.path.dirname(odd))
            Path(odd).write_text("")
            py = registry._python()
            self.assertEqual(registry._builtin_argv(f"{py} {odd}"), [py, odd])
            self.assertEqual(
                registry._builtin_argv(f"{py} {odd} --paper --n 2"),
                [py, odd, "--paper", "--n", "2"])

    def test_an_unrecognised_shape_is_split_as_before(self):
        self.assertIsNone(registry._builtin_argv("node /x/adapter.js"))
        py = registry._python()
        self.assertIsNone(registry._builtin_argv(f"{py} /no/such/file.py"))

    def test_a_file_entry_overriding_a_builtin_is_not_rewritten(self):
        self.write_file({"brokers": {"paper": {
            "command": f"{sys.executable} {MINI} --mine"}}})
        prof = registry.load()["brokers"]["paper"]
        self.assertNotIn("argv", prof)
        self.assertEqual(registry.resolve("paper")[0],
                         [sys.executable, MINI, "--mine"])

    def test_builtins_are_marked_and_the_users_own_are_not(self):
        """A platform lists what the USER linked; a profile that ships with
        the package must not show up there as if they had."""
        registry.add("mine", argv=[sys.executable, MINI])
        reg = registry.load()["brokers"]
        self.assertTrue(reg["paper"].get("builtin"))
        self.assertTrue(reg["lse-sim"].get("builtin"))
        self.assertNotIn("builtin", reg["mine"])
        # a file entry reusing a built-in's name is the user's
        registry.add("novafx", argv=[sys.executable, MINI])
        self.assertNotIn("builtin", registry.load()["brokers"]["novafx"])

    def test_a_builtin_still_spawns(self):
        c = Connector.for_broker("paper")
        try:
            self.assertEqual(c.hello()["mode"], "paper")
        finally:
            c.close()


class TestNames(Home):
    GOOD = ["ab", "a1", "9x", "paper-fast", "my_broker", "alpaca-paper-2",
            "a" * 32, "0-0"]
    BAD = ["", "a", "A1", "Paper", "a b", "a.b", ".hidden", "..", "../x",
           "a/b", "a\\b", "..\\..\\x", "C:x", "a:b", "-ab", "_ab",
           "a" * 33, "ab\n", "ab ", " ab", "ab\x00", "naïve", None, 7, ["ab"]]

    def test_the_rule(self):
        for n in self.GOOD:
            self.assertTrue(registry.valid_name(n), n)
        for n in self.BAD:
            self.assertFalse(registry.valid_name(n), repr(n))

    def test_add_refuses_a_bad_name_and_writes_nothing(self):
        for n in self.BAD:
            with self.assertRaises(registry.RegistryError, msg=repr(n)):
                registry.add(n, argv=["python3", MINI])
        self.assertFalse(os.path.exists(self.home),
                         "a refused name must not even create the home")

    def test_a_bad_name_never_reaches_the_state_directory(self):
        """A backslash name used to become a path on Windows."""
        registry.add("ok-name", argv=["python3", MINI])
        with self.assertRaises(registry.RegistryError):
            registry.add("..\\..\\escape", argv=["python3", MINI])
        self.assertEqual(sorted(self.file()["brokers"]), ["ok-name"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "state")))

    def test_a_name_from_an_older_file_still_loads(self):
        """Strictness is for what is ADDED; a file that works today is not
        broken by the rule."""
        self.write_file({"brokers": {"My.Old Broker": {
            "command": "/usr/bin/env true"}}})
        self.assertIn("My.Old Broker", registry.load()["brokers"])
        self.assertEqual(registry.resolve("My.Old Broker")[0],
                         ["/usr/bin/env", "true"])
        self.assertTrue(registry.remove("My.Old Broker"))


class TestWrites(Home):
    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_the_registry_file_is_private_and_leaves_no_temp_file(self):
        registry.add("one", argv=["python3", MINI], env={"TOKEN_FILE": "x"})
        path = os.path.join(self.home, "brokers.json")
        if os.name == "posix":
            self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_write_keeps_the_old_file_whole(self):
        registry.add("one", argv=["python3", MINI])
        before = Path(self.home, "brokers.json").read_bytes()
        with mock.patch.object(registry.json, "dump",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                registry.add("two", argv=["python3", MINI])
            with self.assertRaises(OSError):
                registry.remove("one")
        self.assertEqual(Path(self.home, "brokers.json").read_bytes(), before)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sorted(self.file()["brokers"]), ["one"])

    def test_a_file_that_cannot_be_read_is_never_overwritten(self):
        os.makedirs(self.home)
        path = os.path.join(self.home, "brokers.json")
        Path(path).write_text('{"brokers": {"mine": {"command": "x"}')  # cut
        for call in (lambda: registry.add("two", argv=["python3", MINI]),
                     lambda: registry.remove("mine")):
            with self.assertRaises(registry.RegistryError):
                call()
        self.assertEqual(Path(path).read_text(),
                         '{"brokers": {"mine": {"command": "x"}')
        Path(path).write_text("[1, 2]")
        with self.assertRaises(registry.RegistryError):
            registry.add("two", argv=["python3", MINI])
        with self.assertRaises(registry.RegistryError):
            registry.load()

    def test_add_keeps_the_other_profiles_and_the_default(self):
        registry.add("one", argv=["python3", MINI], make_default=True)
        registry.add("two", "python3 /x.py", description="Two")
        raw = self.file()
        self.assertEqual(sorted(raw["brokers"]), ["one", "two"])
        self.assertEqual(raw["default"], "one")
        registry.add("one", argv=["python3", MINI, "--v2"])      # replace
        self.assertEqual(self.file()["brokers"]["one"]["argv"][-1], "--v2")

    def test_remove(self):
        registry.add("one", argv=["python3", MINI], make_default=True)
        registry.add("two", argv=["python3", MINI])
        self.assertFalse(registry.remove("nope"))
        self.assertTrue(registry.remove("one"))
        raw = self.file()
        self.assertEqual(sorted(raw["brokers"]), ["two"])
        self.assertEqual(raw["default"], "paper")
        self.assertEqual(self.leftovers(), [])
        # removing a profile never deletes its state: a relink finds its keys
        registry.set_credentials("two", {"k": "v"})
        registry.remove("two")
        self.assertEqual(registry.stored_credentials("two"), ["k"])


class TestStoredKeys(Home):
    def path(self, name):
        return os.path.join(self.home, "state", name,
                            "platform_credentials.json")

    def test_stored_private_and_named_not_shown(self):
        names = registry.set_credentials(
            "kb", {"api_key_id": "PK-1", "api_secret": "s3cret", "blank": ""})
        self.assertEqual(names, ["api_key_id", "api_secret"])
        self.assertEqual(registry.stored_credentials("kb"),
                         ["api_key_id", "api_secret"])
        if os.name == "posix":
            st = os.stat(self.path("kb"))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(
                os.stat(os.path.dirname(self.path("kb"))).st_mode), 0o700)
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(self.path("kb")))
             if n.endswith(".tmp")], [])
        self.assertEqual(registry.credential_env("kb"),
                         {"BRUE_CRED_API_KEY_ID": "PK-1",
                          "BRUE_CRED_API_SECRET": "s3cret"})
        for out in (names, registry.stored_credentials("kb")):
            self.assertNotIn("PK-1", json.dumps(out))
            self.assertNotIn("s3cret", json.dumps(out))

    def test_a_read_makes_no_folder_a_write_makes_it_0700(self):
        """stored_credentials() and credential_env() do not go through
        state_dir(), which makes and chmods the profile's folder: a read of
        a name that has nothing stored would create it. A read makes no
        folder; the folder appears, 0700, only when something is written."""
        state = os.path.join(self.home, "state")
        folder = os.path.join(state, "ghost")
        self.assertEqual(registry.stored_credentials("ghost"), [])
        self.assertEqual(registry.credential_env("ghost"), {})
        self.assertEqual(registry.account_env("ghost"), {})
        self.assertFalse(os.path.exists(folder), "a read made the folder")
        self.assertFalse(os.path.isdir(state), "a read made the state root")
        # writing makes the folder, 0700
        registry.set_credentials("ghost", {"token": "t"})
        self.assertTrue(os.path.isdir(folder))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(registry.stored_credentials("ghost"), ["token"])
        # a delete of a name that has nothing makes no folder either
        self.assertEqual(registry.set_credentials("gone", {}), [])
        self.assertFalse(os.path.exists(os.path.join(state, "gone")))

    def test_the_key_file_never_exists_with_wider_permissions(self):
        """Created 0600, not created open and narrowed afterwards."""
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        seen = []
        real = registry.json.dump

        def spy(obj, fh, **kw):
            seen.append(stat.S_IMODE(os.fstat(fh.fileno()).st_mode))
            return real(obj, fh, **kw)

        old = os.umask(0o000)        # the worst case: nothing masked
        try:
            with mock.patch.object(registry.json, "dump", spy):
                registry.set_credentials("kb", {"api_secret": "s3cret"})
        finally:
            os.umask(old)
        self.assertEqual(seen, [0o600])

    def test_merge_keeps_what_was_not_typed(self):
        registry.set_credentials("kb", {"api_key_id": "PK-1",
                                        "api_secret": "old"})
        names = registry.set_credentials(
            "kb", {"api_secret": "new", "api_key_id": ""}, merge=True)
        self.assertEqual(names, ["api_key_id", "api_secret"])
        self.assertEqual(registry.credential_env("kb"),
                         {"BRUE_CRED_API_KEY_ID": "PK-1",
                          "BRUE_CRED_API_SECRET": "new"})

    def test_merging_nothing_deletes_nothing(self):
        registry.set_credentials("kb", {"api_key_id": "PK-1"})
        for nothing in ({}, None, {"api_key_id": ""}, {"other": None}):
            self.assertEqual(
                registry.set_credentials("kb", nothing, merge=True),
                ["api_key_id"])
        self.assertEqual(registry.credential_env("kb"),
                         {"BRUE_CRED_API_KEY_ID": "PK-1"})

    def test_merge_into_nothing_stores_what_was_typed(self):
        self.assertEqual(
            registry.set_credentials("kb", {"token": "t"}, merge=True),
            ["token"])
        self.assertEqual(
            registry.set_credentials("fresh", {}, merge=True), [])
        self.assertFalse(os.path.exists(self.path("fresh")))

    def test_forgetting_is_the_explicit_replace(self):
        registry.set_credentials("kb", {"api_key_id": "PK-1"})
        self.assertEqual(registry.set_credentials("kb", {}), [])
        self.assertFalse(os.path.exists(self.path("kb")))
        self.assertEqual(registry.stored_credentials("kb"), [])
        self.assertEqual(registry.credential_env("kb"), {})

    def test_replace_still_replaces(self):
        registry.set_credentials("kb", {"a": "1", "b": "2"})
        self.assertEqual(registry.set_credentials("kb", {"b": "3"}), ["b"])
        self.assertEqual(registry.credential_env("kb"), {"BRUE_CRED_B": "3"})

    def test_a_damaged_key_file_reads_as_nothing_stored(self):
        registry.set_credentials("kb", {"a": "1"})
        Path(self.path("kb")).write_text("[not, a, dict")
        self.assertEqual(registry.stored_credentials("kb"), [])
        Path(self.path("kb")).write_text('["a list"]')
        self.assertEqual(registry.stored_credentials("kb"), [])
        self.assertEqual(
            registry.set_credentials("kb", {"b": "2"}, merge=True), ["b"])

    def test_the_account_choice_is_written_the_same_way(self):
        registry.set_account("kb", "ACC-1")
        self.assertEqual(registry.get_account("kb"), "ACC-1")
        self.assertEqual(
            [n for n in os.listdir(os.path.join(self.home, "state", "kb"))
             if n.endswith(".tmp")], [])
        registry.set_account("kb", "")
        self.assertEqual(registry.get_account("kb"), "")


class TestCli(Home):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli(list(argv))
            except SystemExit as e:        # argparse refusing the arguments
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_add_file_stores_a_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "my adapters", "my_adapter.py")
            os.makedirs(os.path.dirname(path))
            Path(path).write_text("")
            code, out, _ = self.run_cli(
                "add", "mine", "--file", path, "--arg=--paper", "--arg", "7",
                "--description", "Mine", "--env", "A=b=c")
            self.assertEqual(code, 0)
            self.assertIn("registered broker 'mine'", out)
            stored = self.file()["brokers"]["mine"]
            self.assertEqual(stored["argv"], ["python3", path, "--paper", "7"])
            self.assertEqual(stored["env"], {"A": "b=c"})
            self.assertEqual(stored["description"], "Mine")

    def test_add_file_makes_the_path_absolute(self):
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            Path(d, "rel.py").write_text("")
            os.chdir(d)
            try:
                self.assertEqual(self.run_cli("add", "rel", "--file",
                                              "rel.py")[0], 0)
            finally:
                os.chdir(here)
            path = self.file()["brokers"]["rel"]["argv"][1]
            self.assertTrue(os.path.isabs(path))
            self.assertEqual(os.path.basename(path), "rel.py")

    def test_add_file_that_does_not_exist_is_refused(self):
        code, _, err = self.run_cli("add", "mine", "--file", "/no/such.py")
        self.assertEqual(code, 2)
        self.assertIn("no file at", err)
        self.assertFalse(os.path.exists(os.path.join(self.home,
                                                     "brokers.json")))

    def test_add_command_still_works(self):
        code, _, _ = self.run_cli("add", "typed", "--command",
                                  f"{sys.executable} {MINI}", "--default")
        self.assertEqual(code, 0)
        self.assertEqual(self.file()["default"], "typed")
        self.assertEqual(registry.resolve()[0], [sys.executable, MINI])

    def test_one_of_file_or_command_exactly(self):
        self.assertEqual(self.run_cli("add", "x1")[0], 2)
        self.assertEqual(self.run_cli("add", "x1", "--file", MINI,
                                      "--command", "python3 x.py")[0], 2)

    def test_arg_belongs_to_file(self):
        code, _, err = self.run_cli("add", "x1", "--command", "python3 x.py",
                                    "--arg", "7")
        self.assertEqual(code, 2)
        self.assertIn("--arg goes with --file", err)

    def test_a_bad_name_is_a_plain_message(self):
        code, _, err = self.run_cli("add", "Bad Name", "--file", MINI)
        self.assertEqual(code, 2)
        self.assertIn("invalid broker name", err)
        self.assertIn("lower case", err)

    def test_brokers_lists_what_will_run(self):
        self.run_cli("add", "mine", "--file", MINI, "--arg", "two words")
        code, out, _ = self.run_cli("brokers")
        self.assertEqual(code, 0)
        self.assertIn("mine", out)
        self.assertIn("'two words'", out)

    def test_a_file_profile_connects_through_the_cli(self):
        self.run_cli("add", "mine", "--file", MINI)
        code, out, err = self.run_cli("check", "mine")
        self.assertEqual(code, 0, err)
        self.assertIn("mini test broker", out)
        self.assertIn("[mine]", out)


if __name__ == "__main__":
    unittest.main()
