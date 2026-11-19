"""brue-connect command line: one surface, every broker.

    python3 -m brueconnect brokers
    python3 -m brueconnect add acmefx --file /path/to/acmefx_adapter.py \
        --env ACMEFX_CREDS={state}/credentials.json --default
    python3 -m brueconnect add acmefx --command "python3 .../acmefx_adapter.py"
    python3 -m brueconnect check [BROKER]
    python3 -m brueconnect account [BROKER]
    python3 -m brueconnect positions [BROKER]
    python3 -m brueconnect orders [BROKER]
    python3 -m brueconnect quote SYMBOL [SYMBOL..] [--broker NAME]
    python3 -m brueconnect flatten [BROKER] --yes

Every command renders through the same formatter no matter which broker is
underneath. That sameness is the product: switching broker changes the
numbers on the screen, never the screen.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys

from . import protocol, registry
from .client import Connector, ConnectorError


def _fmt_money(v) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _fmt_ts(ms) -> str:
    if not ms:
        return "-"
    import datetime as dt
    return dt.datetime.fromtimestamp(
        ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _table(cols: list[str], rows: list[list]) -> str:
    widths = [len(c) for c in cols]
    text = [[("" if v is None else str(v)) for v in r] for r in rows]
    for r in text:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    def line(vals):
        return "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    out = [line(cols), line(["-" * w for w in widths])]
    out += [line(r) for r in text]
    return "\n".join(out)


def _connect(name: str | None) -> Connector:
    c = Connector.for_broker(name)
    c.connect()
    return c


def _caps(c: Connector, part: str) -> dict:
    """One part of the handshake's capabilities, {} when the handshake lacks
    it or sends null (or anything but an object) for it."""
    caps = c.handshake.get("capabilities")
    got = caps.get(part) if isinstance(caps, dict) else None
    return got if isinstance(got, dict) else {}


def _hdr(c: Connector, name: str) -> str:
    h = c.handshake
    # Every field read defensively. This runs against third-party adapters in
    # any language, and an incomplete handshake must print as an incomplete
    # report rather than a traceback: the author is already debugging, and a
    # KeyError from the CLI tells them nothing.
    ad = h.get("adapter") or {}
    out = [f"broker   {h.get('broker') or '(unnamed)'}  [{name}]",
           f"adapter  {ad.get('name') or '?'} {ad.get('version') or '?'}"
           f"  protocol {h.get('protocol')}",
           f"mode     {str(h.get('mode') or 'unknown').upper()}  model "
           f"{h.get('account_model')}  ccy {h.get('account_currency')}"]
    # Whatever the broker chose to say about itself (SPEC 3.1). Shown here so
    # an author can see exactly what a platform's picker will display, and
    # what got trimmed, without opening a UI.
    # What the adapter asks the user to type (SPEC 3.2), as a platform's
    # connect form will list it: label, the BRUE_CRED_ variable it arrives in.
    for f in getattr(c, "credential_form", None) or []:
        out.append(f"asks     {f['label']}  (BRUE_CRED_{f['key'].upper()}"
                   + (", hidden as typed)" if f["type"] == "password" else ")"))
    info = c.broker_info
    if info:
        bits = [info[k] for k in ("display_name", "tagline", "country")
                if info.get(k)]
        for k in ("regulated_by", "asset_classes"):
            if info.get(k):
                bits.append(", ".join(info[k]))
        for k in ("website", "support"):
            if info.get(k):
                bits.append(info[k])
        out.append("says     " + "\n         ".join(bits))
    # What the broker's terms let a platform do (SPEC 3.3), as the SDK read
    # them: unlike the two lines above, a platform ACTS on this, so an author
    # sees here how each flag was read (a typo reads as a restriction).
    if "terms" in h:
        t = getattr(c, "terms", None) or {}
        on = [k for k in protocol.TERMS_FLAGS if t.get(k)]
        out.append("terms    " + (", ".join(on) or "no restriction")
                   + (f"; {t['summary']}" if t.get("summary") else "")
                   + (f"  {t['url']}" if t.get("url") else ""))
    return "\n".join(out)


def cmd_brokers(a) -> int:
    reg = registry.load()
    rows = []
    for name in sorted(reg["brokers"]):
        p = reg["brokers"][name]
        # Shown as the argv it will be spawned with, quoted so it can be read
        # back; a profile from an older file has only its command string.
        shown = shlex.join(p["argv"]) if p.get("argv") else p["command"]
        rows.append([name + (" *" if name == reg["default"] else ""),
                     p.get("description", ""), shown])
    print(_table(["name", "description", "command"], rows))
    print(f"\n* default. Registry: {registry._config_path()}")
    return 0


def cmd_add(a) -> int:
    env = {}
    for kv in a.env or []:
        if "=" not in kv:
            print(f"--env needs KEY=VALUE, got {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        env[k] = v
    if a.file:
        # A Python adapter file. Stored as an argument LIST, so the path is
        # spawned exactly as given whatever it holds (spaces, Windows
        # backslashes), and with the bare word "python3" as the interpreter
        # so the profile survives a Python upgrade; a platform, and
        # registry.resolve, find the real interpreter at spawn.
        path = os.path.abspath(os.path.expanduser(a.file))
        if not os.path.isfile(path):
            print(f"--file: no file at {path}", file=sys.stderr)
            return 2
        registry.add(a.name, argv=["python3", path] + list(a.arg or []),
                     env=env, description=a.description or "",
                     make_default=a.default)
    else:
        if a.arg:
            print("--arg goes with --file; with --command write the "
                  "arguments into the command line", file=sys.stderr)
            return 2
        registry.add(a.name, a.command, env, a.description or "",
                     make_default=a.default)
    print(f"registered broker {a.name!r}"
          + (" (default)" if a.default else ""))
    return 0


def cmd_remove(a) -> int:
    if registry.remove(a.name):
        print(f"removed {a.name!r}")
        return 0
    print(f"{a.name!r} is not in the registry file (built-ins cannot be "
          "removed)", file=sys.stderr)
    return 1


def cmd_check(a) -> int:
    c = _connect(a.broker)
    try:
        name = a.broker or registry.load()["default"]
        print(_hdr(c, name))
        caps = c.handshake.get("capabilities")
        caps = caps if isinstance(caps, dict) else {}
        o = _caps(c, "orders")
        feats = [k for k in ("market", "brackets", "modify", "pending",
                             "trailing", "partial_close", "oco",
                             "close_by") if o.get(k)]
        if o.get("pending"):
            feats.append("types:" + ",".join(o.get("pending_types", [])))
            feats.append("expiry:" + ",".join(o.get("expiry", [])))
        d = _caps(c, "data")
        feats += [k for k in ("quotes", "bars", "history", "operations")
                  if d.get(k)]
        if caps.get("flatten_all"):
            feats.append("flatten_all")
        print("caps     " + " ".join(feats))
        groups: dict[str, int] = {}
        for i in c.catalog.values():
            groups[i.get("asset_class", "?")] = \
                groups.get(i.get("asset_class", "?"), 0) + 1
        print(f"catalog  {len(c.catalog)} instruments: "
              + "  ".join(f"{g}={n}" for g, n in sorted(groups.items())))
        acc = c.account
        print(f"account  balance {_fmt_money(acc.get('balance'))}"
              f"  equity {_fmt_money(acc.get('equity'))}"
              f"  free {_fmt_money(acc.get('margin_free'))}"
              f"  {acc.get('currency', '')}")
        print(f"open     {len(c.positions)} positions, "
              f"{len(c.orders)} resting orders")
        return 0
    finally:
        c.close()


def cmd_account(a) -> int:
    c = _connect(a.broker)
    try:
        acc = c.account
        rows = [[k, _fmt_money(acc.get(k)) if k != "currency"
                 else acc.get(k)]
                for k in ("balance", "equity", "margin_used", "margin_free",
                          "currency")]
        print(_table(["field", "value"], rows))
        return 0
    finally:
        c.close()


def cmd_positions(a) -> int:
    c = _connect(a.broker)
    try:
        rows = [[p["position_id"], p["symbol"], p["side"], p["qty"],
                 p["avg_price"], p.get("sl") or "-", p.get("tp") or "-",
                 _fmt_money(p.get("unrealized_pnl")),
                 _fmt_money(p.get("swap")),
                 _fmt_money(p.get("commission")),
                 _fmt_ts(p.get("opened_at"))]
                for p in c.positions.values()]
        print(_table(["id", "symbol", "side", "qty", "avg", "sl", "tp",
                      "upnl", "swap", "comm", "opened"], rows)
              if rows else "no open positions")
        return 0
    finally:
        c.close()


def cmd_orders(a) -> int:
    c = _connect(a.broker)
    try:
        if not _caps(c, "orders").get("pending"):
            print("this broker does not offer resting orders")
            return 0
        rows = [[o["order_id"], o["symbol"], o["type"], o["side"], o["qty"],
                 o.get("price") or "-", o.get("stop_price") or "-",
                 o["expiry"], _fmt_ts(o.get("expires_at")), o["state"]]
                for o in c.orders.values()]
        print(_table(["id", "symbol", "type", "side", "qty", "price",
                      "stop", "expiry", "expires", "state"], rows)
              if rows else "no resting orders")
        return 0
    finally:
        c.close()


def cmd_operations(a) -> int:
    c = _connect(a.broker)
    try:
        if not _caps(c, "data").get("operations"):
            print("this broker does not report balance operations; a balance "
                  "change that was not a trade cannot be explained")
            return 0
        rows = c.request("history.operations", {"limit": 50})["operations"]
        print(_table(["id", "kind", "amount", "ccy", "balance after",
                      "reference", "comment", "time"],
                     [[o["operation_id"], o["kind"],
                       _fmt_money(o["amount"]), o["currency"],
                       _fmt_money(o["balance_after"]),
                       o.get("reference") or "-",
                       (o.get("comment") or "")[:40],
                       _fmt_ts(o.get("time"))] for o in rows])
              if rows else "no balance operations")
        return 0
    finally:
        c.close()


def cmd_quote(a) -> int:
    c = _connect(a.broker)
    try:
        missing = [s for s in a.symbols if s not in c.catalog]
        if missing:
            print(f"not in this broker's catalog: {', '.join(missing)}",
                  file=sys.stderr)
            return 1
        mark = c.mark()
        c.request("quotes.subscribe", {"symbols": a.symbols})
        rows = []
        for sym in a.symbols:
            try:
                q = c.wait_event("quote", lambda d, s=sym:
                                 d["symbol"] == s, timeout=10, since=mark)
                spec = c.catalog[sym]
                spread = (q["ask"] - q["bid"]) / spec["tick_size"]
                rows.append([sym, q["bid"], q["ask"], f"{spread:.1f}",
                             _fmt_ts(q.get("time"))])
            except TimeoutError:
                rows.append([sym, "-", "-", "-", "no quote in 10s"])
        print(_table(["symbol", "bid", "ask", "spread(pts)", "time"], rows))
        return 0
    finally:
        c.close()


def cmd_verify(a) -> int:
    """The certification gate, one command.

    'How do we know a broker's setup will not glitch' has a mechanical
    answer: this passes or the broker is not done. It runs the conformance
    suite against the registered profile, with the profile's own env and
    isolated state.
    """
    import subprocess
    reg = registry.load()
    name = a.broker or reg["default"]
    prof = reg["brokers"].get(name)
    if prof is None:
        print(f"unknown broker {name!r}", file=sys.stderr)
        return 2
    _, env, sdir = registry.resolve(name)
    suite = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "conformance", "run.py")
    # The suite splits --adapter by shell rules, so hand it the profile's
    # argv quoted, not the stored string: the two differ for a built-in whose
    # path holds a space.
    cmd = [sys.executable, suite, "--adapter",
           shlex.join(registry.profile_argv(prof))]
    if a.partial_hint:
        cmd += ["--partial-hint", a.partial_hint]
    if a.restartable:
        cmd += ["--restartable"]
    # One certification at a time per profile. The suite opens positions,
    # flattens, and counts what came back; two runs sharing one broker account
    # close each other's positions and produce failures that belong to neither
    # of them, so a second run on the same profile is refused up front.
    try:
        import fcntl
    except ImportError:      # Windows: no flock; msvcrt covers the same risk
        import msvcrt
        fcntl = None
    lock_path = os.path.join(sdir, "verify.lock")
    lock = open(lock_path, "w")
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(f"another verification of {name!r} is already running "
              f"({lock_path}). One at a time: they share the broker account.",
              file=sys.stderr)
        return 2
    print(f"verifying broker {name!r} (state: {sdir})\n")
    r = subprocess.run(cmd, env=env)
    if r.returncode == 0:
        print(f"\nbroker {name!r} is CONFORMANT")
    else:
        print(f"\nbroker {name!r} FAILED conformance; it is not ready "
              "for clients", file=sys.stderr)
    return r.returncode


def cmd_flatten(a) -> int:
    if not a.yes:
        print("flatten closes EVERY position and cancels EVERY resting "
              "order on this broker. Re-run with --yes.", file=sys.stderr)
        return 2
    c = _connect(a.broker)
    try:
        r = c.request("flatten_all", timeout=120)
        print(f"closed {r.get('closed', 0)} positions")
        return 0
    finally:
        c.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="brueconnect")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("brokers", help="list registered brokers")

    p = sub.add_parser("add", help="register a broker profile")
    p.add_argument("name", help=registry.NAME_RULE)
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument("--file", metavar="PATH.py",
                     help="a Python adapter file, at any path (spaces and "
                          "Windows backslashes are safe)")
    how.add_argument("--command",
                     help="a whole command line, for an adapter that is not "
                          "a Python file; quote paths that hold spaces")
    p.add_argument("--arg", action="append", metavar="X",
                   help="an argument for the --file adapter; repeat for "
                        "more, and write --arg=--flag for one that starts "
                        "with a dash")
    p.add_argument("--env", action="append", metavar="KEY=VALUE",
                   help="{state} expands to the broker's private dir")
    p.add_argument("--description", default="")
    p.add_argument("--default", action="store_true")

    p = sub.add_parser("remove", help="remove a registered broker")
    p.add_argument("name")

    for name, hlp in (("check", "handshake, catalog and account summary"),
                      ("account", "account state"),
                      ("positions", "open positions"),
                      ("orders", "resting orders"),
                      ("operations", "balance moves that were not trades")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("broker", nargs="?", default=None)

    p = sub.add_parser("quote", help="live quotes for symbols")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--broker", default=None)

    p = sub.add_parser("verify", help="run the conformance suite against a "
                                      "registered broker")
    p.add_argument("broker", nargs="?", default=None)
    p.add_argument("--partial-hint", default=None,
                   help="SYMBOL:QTY that fills in clips on this broker")
    p.add_argument("--restartable", action="store_true",
                   help="broker persists state; test reconnect survival")

    p = sub.add_parser("flatten", help="close everything on a broker")
    p.add_argument("broker", nargs="?", default=None)
    p.add_argument("--yes", action="store_true")

    a = ap.parse_args(argv)
    fn = globals()["cmd_" + a.cmd]
    try:
        return fn(a)
    except registry.RegistryError as e:
        print(f"registry: {e}", file=sys.stderr)
        return 2
    except ConnectorError as e:
        print(f"broker error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
