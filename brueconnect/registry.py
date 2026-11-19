"""Broker registry: brokers are configuration, not code.

A broker on this platform is a NAMED PROFILE: the adapter command that speaks
for it plus the environment that configures it. Everything else, credentials,
cached identity, journals, lives in a state directory that belongs to that
profile alone. Switching broker is switching a name; nothing else moves.

The registry file is JSON at ``~/.brue-connect/brokers.json`` (the directory
moves with ``BRUE_CONNECT_HOME``):

    {"brokers": {
        "acmefx": {
            "command": "python3 /path/to/acmefx_adapter.py",
            "argv": ["python3", "/path/to/acmefx_adapter.py"],
            "env": {"ACMEFX_CREDS": "{state}/credentials.json",
                    "ACMEFX_ACCOUNT_TYPE": "RAW"},
            "description": "AcmeFX demo server"
        }
    }, "default": "paper"}

``argv`` is what is spawned: a list, one element per argument, so a path
with a space or a Windows backslash is exactly what was registered. The
``command`` string beside it is the same thing written for a person (and for
a reader older than ``argv``, which splits it by POSIX shell rules). A file
written before ``argv`` existed has only ``command`` and keeps working as it
always did. When both are present ``argv`` wins: to change what a profile
runs by hand, edit ``argv`` or delete it.

A profile name is 2 to 32 characters of ``a-z 0-9 _ -`` starting with a
letter or digit. It becomes a directory name, so it is held to a shape that
is the same directory on every operating system.

Six profiles ship built in: ``paper`` and ``novafx`` (served by the
bundled adapters), ``lse-sim`` (the hosted LSE demo account, needs your
LSE API key), and one for each broker adapter the kit ships, each on that
broker's paper, practice or demo account and needing your own key or token
for it (the entries of ``_builtin`` name them, and each one's
description says what it needs). A file entry with the same name overrides
them.

Isolation is structural, not disciplinary: every profile gets its own
``<home>/state/<name>/`` directory, the literal ``{state}`` in any env value
expands to it, and the spawned adapter additionally receives
``BRUE_BROKER_STATE`` pointing at it. Two profiles can wrap the SAME adapter
with different credentials and never share a file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile

_PKG = os.path.dirname(os.path.abspath(__file__))

# A profile name is a directory name (<home>/state/<name>) on whatever system
# the user runs. Lower case so it cannot be two names on one filesystem and
# one on another; no dot, slash, backslash or colon, so it cannot leave the
# state directory on any of them. fullmatch: `$` would pass a trailing newline.
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
NAME_RULE = ("2 to 32 characters: lower case letters, digits, - and _, "
             "starting with a letter or a digit")


class RegistryError(Exception):
    pass


def valid_name(name) -> bool:
    """Whether `name` may be registered as a profile (see NAME_RULE)."""
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def home() -> str:
    return os.environ.get("BRUE_CONNECT_HOME",
                          os.path.expanduser("~/.brue-connect"))


def _config_path() -> str:
    return os.path.join(home(), "brokers.json")


def _write_private_json(path: str, obj, indent=None) -> None:
    """Write JSON so a reader sees the old file or the new one, never half.

    A temporary file of its own name in the same directory (two writers
    cannot share it), created 0600 (it never exists with wider permissions,
    which matters for the credential file and for a profile whose env holds a
    token), flushed to disk, then renamed over the target. A failure leaves
    the old file untouched and no temporary file behind.
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)   # a writer's folder, if not yet made
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _clean_argv(argv) -> list[str]:
    """An argument list fit to store and spawn, or RegistryError."""
    if (not isinstance(argv, (list, tuple)) or not argv
            or not all(isinstance(a, str) for a in argv)):
        raise RegistryError("a command is a non-empty list of strings")
    if not argv[0].strip():
        raise RegistryError("the command's first word is empty")
    if any("\x00" in a for a in argv):
        raise RegistryError("a command argument contains a NUL character")
    return list(argv)


def split_command(command: str, windows: bool | None = None) -> list[str]:
    """One command line to an argument list, by the rules of THIS system.

    POSIX rules everywhere but Windows. There, POSIX rules read every
    backslash as an escape and turn ``C:\\Users\\me\\adapter.py`` into
    ``C:Usersmeadapter.py``, so a Windows command line is split on spaces
    outside double quotes with backslashes left alone, and the quotes around
    a whole argument removed. `windows` forces one reading or the other (for
    tests); None means ask the running system.
    """
    if windows is None:
        windows = os.name == "nt"
    try:
        if not windows:
            return shlex.split(command)
        parts = shlex.split(command, posix=False)
    except ValueError as e:
        raise RegistryError(f"cannot read the command line: {e}")
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p
            for p in parts]


def profile_argv(profile: dict) -> list[str]:
    """What a loaded profile spawns: its argv list, else its command split.

    The one place that decision is made, for `resolve` and for any platform
    that wants to show a user what a profile runs.
    """
    argv = profile.get("argv")
    if (isinstance(argv, list) and argv
            and all(isinstance(a, str) for a in argv)):
        return list(argv)
    return shlex.split(profile.get("command") or "")


def _python() -> str:
    """The interpreter built-in profiles are spawned with (see _builtin)."""
    frozen = bool(getattr(sys, "frozen", False))
    return os.environ.get("BRUE_CONNECT_PYTHON",
                          "python3" if frozen else sys.executable)


def _builtin_argv(command: str) -> list[str] | None:
    """Recover the argv of a built-in from its command string, exactly.

    A built-in is written as ``"<python> <adapter path>[ args]"`` and then
    split by shell rules, which breaks as soon as either path holds a space
    or a backslash (``C:\\Program Files\\...``). Both paths are known here,
    so they are cut out whole instead of guessed: the interpreter is the
    known prefix, the adapter is the longest remainder that is a file, and
    only what follows it is split. None when the command is not of that
    shape; the caller then splits it as before.
    """
    py = _python()
    if not command.startswith(py + " "):
        return None
    rest = command[len(py) + 1:]
    cuts = [len(rest)] + [i for i in range(len(rest) - 1, 0, -1)
                          if rest[i] == " "]
    for cut in cuts:
        if os.path.isfile(rest[:cut]):
            try:
                return [py, rest[:cut]] + shlex.split(rest[cut + 1:])
            except ValueError:
                return None
    return None


_BARE_PYTHON = ("python", "python3", "python.exe", "python3.exe")


def _findable(argv: list[str], env: dict) -> list[str]:
    """Rescue a bare ``python3`` that this machine does not have.

    ``brueconnect add --file`` stores the interpreter as the bare word
    ``python3`` so a profile survives a Python upgrade. A stock Windows
    Python installs no ``python3``; there the word is replaced by the
    interpreter the built-ins use. Only when the word is NOT on the PATH the
    adapter would be spawned with: a profile that finds its interpreter today
    is spawned exactly as before.
    """
    word = argv[0]
    if word.lower() not in _BARE_PYTHON:
        return argv
    if shutil.which(word, path=env.get("PATH")):
        return argv
    py = _python()
    if py and py != word and (os.path.isabs(py) or shutil.which(py)):
        return [py] + argv[1:]
    return argv


def _builtin() -> dict:
    # sys.executable is the interpreter that imported us (the right one in
    # any pip/venv install, including Windows where bare "python3" is not on
    # PATH); a frozen host app is NOT a python and must not be spawned, so
    # fall back to python3 there. BRUE_CONNECT_PYTHON overrides either way.
    frozen = bool(getattr(sys, "frozen", False))
    py = os.environ.get("BRUE_CONNECT_PYTHON",
                        "python3" if frozen else sys.executable)
    return {
        "paper": {
            "command": f"{py} {os.path.join(_PKG, 'adapters', 'paper', 'paper_adapter.py')}",
            "env": {},
            "description": "reference paper simulator (built in)",
        },
        "novafx": {
            "command": f"{py} {os.path.join(_PKG, 'adapters', 'novafx', 'novafx_adapter.py')}",
            "env": {},
            "description": "NovaFX fixture: foreign terms on the paper engine (built in)",
        },
        # The LSE demo account, reached through the same contract as any
        # other broker. It is a profile here, not a special case: that is the
        # point of it being an adapter.
        "lse-sim": {
            "command": f"{py} {os.path.join(_PKG, 'adapters', 'lse_sim', 'lse_sim_adapter.py')}",
            "env": {"LSE_SIM_STATE": "{state}"},
            "description": "LSE demo account (needs your LSE API key)",
        },
        # A real broker's PAPER account. `--paper` pins the environment, so
        # this profile can never reach the broker's live host whatever key
        # it is given; a live profile is something a user adds on purpose.
        "alpaca": {
            "command": f"{py} {os.path.join(_PKG, 'adapters', 'alpaca', 'alpaca_adapter.py')} --paper",
            "env": {},
            "description": "Alpaca paper trading (needs your Alpaca paper API key)",
        },
        # The same for a broker with practice hosts: `--paper` pins them,
        # and on practice the adapter takes no other endpoint but this
        # machine (an endpoint variable inherited from the platform's
        # environment is ignored, and said), so this profile can never reach
        # the broker's live hosts whatever token it is given.
        "oanda": {
            "command": f"{py} {os.path.join(_PKG, 'adapters', 'oanda', 'oanda_adapter.py')} --paper",
            "env": {},
            "description": "OANDA practice (needs your OANDA practice API token)",
        },
        # A broker's DEMO account. `--paper` pins the demo host, so this
        # profile can never reach the broker's live host whatever key or
        # environment variable it is given (another host is refused, never
        # used); a live profile is something a user adds on purpose.
        "capitalcom": {
            "command": f"{py} {os.path.join(_PKG, 'adapters', 'capitalcom', 'capitalcom_adapter.py')} --paper",
            "env": {},
            "description": "Capital.com demo account (needs your Capital.com "
                           "demo API key, login email and the key's password)",
        },
    }


def load() -> dict:
    """All profiles, built-ins first, file entries overriding by name."""
    profiles = _builtin()
    path = _config_path()
    default = "paper"
    from_file: set = set()
    if os.path.exists(path):
        try:
            with open(path) as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise RegistryError(f"cannot read {path}: {e}")
        if not isinstance(raw, dict):
            raise RegistryError(f"cannot read {path}: not a JSON object")
        for name, prof in (raw.get("brokers") or {}).items():
            if not isinstance(prof, dict):
                raise RegistryError(
                    f"broker {name!r} in {path} has no command")
            argv = prof.get("argv")
            if not (isinstance(argv, list) and argv
                    and all(isinstance(a, str) for a in argv)):
                argv = None
            command = prof.get("command")
            if not command and argv:
                command = shlex.join(argv)
            if not command or not isinstance(command, str):
                raise RegistryError(
                    f"broker {name!r} in {path} has no command")
            profiles[name] = {"command": command,
                              "env": dict(prof.get("env") or {}),
                              "description": prof.get("description", "")}
            if argv:
                # Preferred over the string wherever a profile is spawned
                # (profile_argv); absent for a file written before it existed.
                profiles[name]["argv"] = list(argv)
            from_file.add(name)
        default = raw.get("default", default)
    for name, prof in profiles.items():
        if name not in from_file:
            # Shipped with this package, not written by the user. A platform
            # that lists "what the user linked" needs to tell the two apart,
            # and the name alone cannot say: a file entry may reuse it.
            prof["builtin"] = True
            argv = _builtin_argv(prof["command"])
            if argv:
                prof["argv"] = argv
    return {"brokers": profiles, "default": default}


def _state_path(name: str) -> str:
    """The profile's state directory, as a path only -- NOT made. A read of
    a credential or an account must not create the folder: the folder is
    made only when something is written (state_dir, called by a writer or
    before a spawn)."""
    return os.path.join(home(), "state", name)


def state_dir(name: str) -> str:
    d = _state_path(name)
    os.makedirs(d, exist_ok=True)
    # Credentials live under here; nobody else needs to read them.
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def resolve(name: str | None = None) -> tuple[list[str], dict, str]:
    """A profile name to (argv, env, state_dir), ready to spawn.

    The returned env is the CURRENT environment plus the profile's own
    entries, so a profile fully controls its adapter's configuration while
    inheriting the basics (PATH, HOME). ``{state}`` in a value expands to the
    profile's private directory.
    """
    reg = load()
    name = name or reg["default"]
    prof = reg["brokers"].get(name)
    if prof is None:
        known = ", ".join(sorted(reg["brokers"]))
        raise RegistryError(f"unknown broker {name!r}; known: {known}")
    sdir = state_dir(name)
    env = dict(os.environ)
    for k, v in prof["env"].items():
        env[str(k)] = str(v).replace("{state}", sdir)
    env["BRUE_BROKER_STATE"] = sdir
    env.update(credential_env(name))
    env.update(account_env(name))
    try:
        argv = profile_argv(prof)
    except ValueError as e:
        raise RegistryError(f"broker {name!r}: cannot read its command: {e}")
    if not argv:
        raise RegistryError(f"broker {name!r} has an empty command")
    return _findable(argv, env), env, sdir


def _credentials_path(name: str) -> str:
    # Named for who put it there. An adapter that registers its OWN account
    # writes its own file (credentials.json, say); this one holds what the
    # USER typed into the platform, and the two must never collide. A path
    # only (_state_path): reading it must make no folder.
    return os.path.join(_state_path(name), "platform_credentials.json")


def credential_env(name: str) -> dict:
    """User-held credentials for a profile, as BRUE_CRED_* env (SPEC 8.5).

    Empty when the user has supplied none, which is the normal case: most
    brokers here self-register a demo account and need nothing typed.
    """
    try:
        with open(_credentials_path(name)) as fh:
            vals = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for k, v in (vals or {}).items():
        if v is None or v == "":
            continue
        out["BRUE_CRED_" + str(k).upper().replace("-", "_")] = str(v)
    return out


def _account_path(name: str) -> str:
    # A path only (_state_path): reading it must make no folder; set_account
    # makes the folder (state_dir) before it writes.
    return os.path.join(_state_path(name), "account.json")


def get_account(name: str) -> str:
    """The account this profile deals on, or "" for the login's primary.

    Only an EXPLICIT platform choice counts: a choice file is written only
    by set_account below, and anything with extra keys is some adapter's
    private state and must never become a SPEC 1.7 pin. Reading account_id
    out of whatever json sits in account.json would let an adapter's own
    cached account come back as a hard pin after a key switch and refuse the
    new login's accounts."""
    try:
        with open(_account_path(name)) as fh:
            d = json.load(fh)
        if not isinstance(d, dict) or not d.get("account_id"):
            return ""
        # set_account's shapes only: {"account_id", "chosen"}, or the bare
        # single-key {"account_id"} shape, still honoured as a real choice.
        if d.get("chosen") or set(d) == {"account_id"}:
            return str(d["account_id"])
        return ""
    except (OSError, json.JSONDecodeError):
        return ""


def set_account(name: str, account_id: str) -> str:
    """Remember which account the user picked for this profile.

    Stored rather than asked for on every connect: a chooser that reappears
    every time you open the platform is a chooser nobody wants.
    """
    path = _account_path(name)
    if not account_id:
        try:
            os.remove(path)
        except OSError:
            pass
        return ""
    # "chosen" marks this as a platform choice; get_account refuses to read a
    # pin out of any other shape (see its docstring).
    state_dir(name)                 # make the private folder (0700) to write in
    _write_private_json(path, {"account_id": str(account_id), "chosen": True})
    return str(account_id)


def account_env(name: str) -> dict:
    acct = get_account(name)
    return {"BRUE_ACCOUNT_ID": acct} if acct else {}


def stored_credentials(name: str) -> list:
    """The NAMES of the fields the user has stored for a profile, sorted.

    Never a value. This is what lets a platform say "saved" beside a field,
    and leave it alone, instead of showing an empty box that a careless
    Connect then treats as "delete my key".
    """
    try:
        with open(_credentials_path(name)) as fh:
            vals = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(vals, dict):
        return []
    return sorted(str(k) for k, v in vals.items()
                  if v is not None and v != "")


def set_credentials(name: str, values: dict, merge: bool = False) -> list:
    """Store what the user typed, on this machine only, 0600.

    Returns the field NAMES stored, never the values: a caller that logs the
    return value must not thereby log a secret.

    Without `merge` the given values REPLACE whatever was stored, and an
    empty dict (or all blank values) deletes the file, which is how a user
    takes a key back.

    With `merge` the given non-blank values are laid over what is already
    stored and everything else is KEPT: a blank is "I typed nothing here",
    never "delete this". That is the call for a form the user may submit
    with some boxes left empty; forgetting a key is then always the explicit
    replace above, never a side effect.
    """
    path = _credentials_path(name)
    clean = {str(k): str(v) for k, v in (values or {}).items()
             if v is not None and str(v) != ""}
    if merge:
        try:
            with open(path) as fh:
                held = json.load(fh)
        except (OSError, json.JSONDecodeError):
            held = {}
        if not isinstance(held, dict):
            held = {}
        kept = {str(k): str(v) for k, v in held.items()
                if v is not None and str(v) != ""}
        kept.update(clean)
        clean = kept
        if not clean:
            return []
    if not clean:
        try:
            os.remove(path)
        except OSError:
            pass
        return []
    state_dir(name)                 # make the private folder (0700) to write in
    _write_private_json(path, clean)
    return sorted(clean)


def add(name: str, command=None, env: dict | None = None,
        description: str = "", make_default: bool = False,
        argv: list | None = None) -> None:
    """Register (or replace) a profile.

    Give the command as `argv`, a list with one element per argument, or as
    one `command` string (a list passed as `command` is taken as argv). The
    list is what is stored and spawned; the string form is split once, here,
    by the rules of the system it was typed on (see split_command), so a
    Windows path typed with backslashes survives.
    """
    if not valid_name(name):
        raise RegistryError(f"invalid broker name {name!r} ({NAME_RULE})")
    if argv is None and isinstance(command, (list, tuple)):
        argv, command = command, None
    if argv is not None:
        argv = _clean_argv(argv)
        command = shlex.join(argv)
    elif isinstance(command, str) and command.strip():
        argv = _clean_argv(split_command(command))
        # The stored string must mean the stored list to a reader that only
        # knows the string (it splits by POSIX rules). Kept as typed when it
        # already does; rewritten quoted when it does not (a Windows path).
        try:
            same = shlex.split(command) == argv
        except ValueError:
            same = False
        if not same:
            command = shlex.join(argv)
    else:
        raise RegistryError("a broker needs a command: an argv list or a "
                            "command line")
    if env is not None and not isinstance(env, dict):
        raise RegistryError("env is a mapping of names to values")
    os.makedirs(home(), exist_ok=True)
    path = _config_path()
    raw = {"brokers": {}, "default": "paper"}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            # Refuse rather than start a fresh file over one that cannot be
            # read: the other profiles in it are the user's, not ours to drop.
            raise RegistryError(f"cannot read {path}: {e}")
        if not isinstance(raw, dict):
            raise RegistryError(f"cannot read {path}: not a JSON object")
        if not isinstance(raw.get("brokers"), dict):
            raw["brokers"] = {}
    raw["brokers"][name] = {"command": command, "argv": argv,
                            "env": {str(k): str(v)
                                    for k, v in (env or {}).items()},
                            "description": description}
    if make_default:
        raw["default"] = name
    _write_private_json(path, raw, indent=1)


def remove(name: str) -> bool:
    path = _config_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(f"cannot read {path}: {e}")
    if not isinstance(raw, dict) or name not in (raw.get("brokers") or {}):
        return False
    del raw["brokers"][name]
    if raw.get("default") == name:
        raw["default"] = "paper"
    _write_private_json(path, raw, indent=1)
    return True
