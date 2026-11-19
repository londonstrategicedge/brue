# Brue Connect

The broker door for Brue trade-entry scripts and for any platform. Brue is
for writing trade entries (buy, sell, close) and connecting them to brokers;
Brue Connect is the connecting half. A platform (a terminal, a strategy
runner, a notebook, a Brue script) talks to a broker through an ADAPTER: a
local process that speaks one line-delimited JSON protocol on stdio. Write
that adapter once and your broker works in every platform built on this
contract.

Any broker becomes one adapter. The reference paper simulator is an adapter
with no special access, and nothing anywhere in the platform may special-case
a broker by name.

**Who this package is for.** Two audiences, one contract:

- **Trading through a broker?** You want a platform built on this contract
  (the LSE Terminal, or `pip install brue-language` for scripts), not the
  connector directly; it ships inside both and stays out of sight.
- **Integrating your broker or venue?** You are in the right place. Read
  SPEC.md (the law), answer ONBOARDING.md, write one adapter, then prove it:
  run the conformance suite against your adapter command (Install and
  "Prove it" below say how). Nothing FAILs = your broker works in every
  platform on this contract.

## Read this first

1. **[SPEC.md](SPEC.md)** is the contract, and it is normative. It is
   complete enough to implement an adapter in any language that can read and
   write lines of JSON: envelope, handshake, every method and event, the
   closed error set, reconciliation, and the safety requirements.
2. **[ONBOARDING.md](ONBOARDING.md)** is the questionnaire to send a broker
   before anyone writes code. Every question in it exists because a real
   integration got that fact wrong quietly.
3. This file is the practical part: the SDK, the environment, how to prove
   you are conformant, and how to get listed.

## Install

Brue Connect ships inside the Brue package, so one install gives both the
language and the connector:

```
pip install brue-language
```

There is no separate `brue-connect` package on PyPI. Use `brue-language`
0.0.2 or newer: 0.0.1 carried an older `brueconnect` without the credential
form of SPEC 3.2. The SDK also reaches you in two other ways:

- **Inside the LSE Terminal.** The desktop app carries it in its `brue-sdk`
  folder. When the Terminal starts an adapter it names the folder that holds
  its SDK in `BRUE_CONNECT_SDK`; an adapter that puts that folder first on
  `sys.path` before `from brueconnect import ...` always speaks the
  Terminal's own SDK, whatever Python runs it. The starter adapter does.
- **From a checkout of this repository:** `pip install -e /path/to/brue`.

If an earlier setup installed a separate `brue-connect`, remove it first
(`pip uninstall brue-connect`), then reinstall Brue
(`pip install --force-reinstall brue-language`, or
`pip install --force-reinstall -e /path/to/brue`), so the connector inside
Brue is the one imported.

The SDK is a normal Python package with no dependencies. The conformance
command below runs your adapter as a program of its own, and both it and
the adapter must be able to `import brueconnect`; if the adapter cannot, it
dies on its first line. After `pip install -e` that import works from
anywhere. With only the LSE Terminal's copy, name its `brue-sdk` folder in
`PYTHONPATH` for the command: the Terminal's "+ Add your own broker" card
shows the whole line, with your computer's paths, under "For developers:
test your adapter on this computer". (When the Terminal itself runs an adapter it names the folder
in `BRUE_CONNECT_SDK`, as above.)

## Your own broker in the LSE Terminal

Start from `brueconnect/templates/my_broker_adapter.py`. As it comes it
trades a practice market made inside the file, so you can add it and use it
before you change a line; each place your broker's own call belongs is
marked `# YOUR BROKER:`. In the LSE Terminal, Connections, then
"+ Add your own broker", has the steps and the download of this file.

## The shortest real adapter

Python adapters subclass `Adapter` and implement `handle_<method>` (dots
become underscores: `order.place` is `handle_order_place`). The base class
owns the stdio loop, dispatch, event sequencing, the handshake and the
idempotency store, so you write broker logic and nothing else. It dispatches
the methods of SPEC section 4 by their exact names only: `order_place` is
not `order.place`, and is answered `unsupported` (SPEC 1). An adapter that
routes requests itself asks `self._handler(method)` for the handler.

```python
from brueconnect import Adapter, AdapterError

class MyBroker(Adapter):
    name, version, broker = "mybroker", "0.1", "My Broker"
    mode = "paper"                     # "live" means real money (SPEC 8.2)
    account_currency, account_model = "USD", "netting"

    # capabilities() is a METHOD you override, not a class attribute. It
    # returns the SPEC section 3 capabilities object; anything you leave out
    # is treated as a feature you do not have.
    def capabilities(self):
        return {"orders": {"market": True, "partial_close": False},
                "data": {"quotes": False, "bars": False, "history": False},
                "flatten_all": True}

    def on_first_call(self):
        """Log in HERE, never in your startup path: `hello` must stay
        answerable cold so a picker can learn your name for free."""

    def handle_catalog_list(self, params):
        return {"instruments": [...]}   # InstrumentSpec, SPEC section 4

    def handle_account_get(self, params):
        return {"balance": 0.0, "equity": 0.0, "margin_used": 0.0,
                "margin_free": 0.0, "currency": "USD"}

    def handle_positions_list(self, params):
        return {"positions": []}

    def handle_order_place(self, params):
        prior = self.idempotent_replay(params["client_id"])
        if prior is not None:
            return prior                # a replay places nothing
        # Conformance REQUIRES you to reject bad orders (SPEC 8.6), so a real
        # handler validates before it deals:
        #   unknown symbol       -> AdapterError("unknown_symbol", ...)
        #   qty off lot_step, or  <min_qty / >max_qty -> "invalid_qty"
        #   cannot afford margin -> "insufficient_margin"
        ...                             # deal it at your broker
        self.emit("order.filled", {...})
        return self.idempotent_store(params["client_id"], {"order_id": "1"})

    def handle_flatten_all(self, params):
        return {"closed": 0}            # REQUIRED of every adapter

    def handle_history_fills(self, params):
        return {"fills": []}            # REQUIRED: the ledger, oldest first

if __name__ == "__main__":
    MyBroker().run()
```

This is a skeleton, not a conformant adapter: it leaves out validation, a
real catalog, and event handling. It shows the SHAPE. The conformance suite
is what tells you when you are actually done.

`idempotent_replay(client_id)` returns the stored result of a `client_id` you
have already seen (or `None`), and `idempotent_store(client_id, result)`
records one; together they make a replayed `order.place` return its original
outcome and place nothing. Raise `AdapterError(code, message, retryable)`
inside a handler to return a typed error; `code` must come from the closed
set in SPEC section 6, or carry an `x_` prefix if it is yours. One of that
set belongs in `on_first_call` alone: `reconnect`, when the session shows
that your cold `hello` said something untrue (an account's currency or model
you could only guess). Remember what you read in `BRUE_BROKER_STATE` first,
so your next `hello` says it; the platform then starts you again, once, and
the SDK's `Connector` does that inside `connect()`. Use
`self.emit(name, data)` for events; it is thread-safe. Guard your own state
with your own locks (any name is fine; the SDK's own lock is private and
cannot collide), and do not hold a lock across `emit()` longer than you must.

Working examples in this repo, the starter first, then smallest first:

| Adapter | Lines | Why read it |
|---|---|---|
| `brueconnect/templates/my_broker_adapter.py` | ~750 | the starter: a whole adapter on a practice market, with each broker call to replace marked `# YOUR BROKER:` |
| `tests/mini_adapter.py` | 84 | the whole contract, one instrument |
| `brueconnect/adapters/novafx/` | 164 | foreign vocabulary on a shared engine |
| `brueconnect/adapters/paper/` | 662 | the reference adapter |
| `brueconnect/adapters/meridian/` | ~2000 | a full integration: OAuth login, account selection, an at-least-once stream, REST as the backstop |

`brueconnect/adapters/` holds eight reference adapters for simulated venues
(atlas, helix, meridian, northgate, pemberton, quantex, sablepoint, vertex),
each speaking a genuinely different wire protocol (FIX, HMAC REST, a binary
link, JSON-RPC over websocket, XML polling, STOMP, gRPC, OAuth2 REST), plus
six more: `paper` (the reference simulator), `novafx` (foreign vocabulary
on the paper engine), `lse_sim` (the LSE demo account as a broker), and
three for real brokers, `alpaca`, `oanda` and `capitalcom`: each runs on
your paper, practice or demo account unless it is started with `--live`
(or `ALPACA_ENV=live`, `OANDA_ENV=live`, `CAPITALCOM_ENV=live`). All
are pure standard library on purpose: an adapter is spawned by whatever
Python the user's machine has, so a pip dependency makes that broker
unconnectable for people who do not have it. (The LSE demo adapter uses
`websockets` when it is installed, to stream its fills; without it, it polls
them.)

## What the platform sets in your environment

Credentials never travel in a protocol message (SPEC 8.5). They reach you as
environment variables at spawn:

| Variable | Meaning |
|---|---|
| `BRUE_BROKER_STATE` | your private directory: credentials, tokens, caches. Yours alone, `0700`, and it survives restarts |
| `BRUE_CRED_<FIELD>` | what the user typed into your credential form, upper-cased (`api_key` becomes `BRUE_CRED_API_KEY`). Treat as optional |
| `BRUE_ACCOUNT_ID` | which of the login's accounts this process deals on (SPEC 1.7). A platform sets it once the user has chosen; if it is empty your adapter falls back to the login's primary. This is your fallback, not the platform's default: a platform must never auto-pick an account for the user (SPEC 1.7), but your adapter must still do something sensible when spawned bare, e.g. in conformance |

Anything else your adapter needs (an endpoint, a mode switch) is your own
variable, set from the broker's directory row.

Say which fields you need typed with one class attribute, and a platform
draws its connect form from your handshake (SPEC 3.2):

```python
class MyBroker(Adapter):
    credential_form = [
        {"key": "api_key_id", "label": "API key ID", "type": "text"},
        {"key": "api_secret", "label": "API secret", "type": "password"},
    ]
```

It names fields and never carries a value: `api_key_id` arrives as
`BRUE_CRED_API_KEY_ID` the next time you are spawned. At most 8 fields, keys
of lower case letters, digits and `_`. When a value you need is missing,
answer `unauthorized` with a sentence that says which.

Say what your broker's own terms let a platform do with one more (SPEC 3.3):

```python
class MyBroker(Adapter):
    terms = {
        "data_local_only": True,       # your prices are the account holder's alone
        "orders_by_hand_only": False,  # True: each live order is a person's own action
        "summary": "MyBroker licenses its prices to you alone, so they stay on this computer.",
        "url": "https://mybroker.example/legal/api-licence",
    }
```

Unlike the form, a platform acts on this. With `data_local_only` your prices,
and the figures worked from them (open P/L, equity, margin), never reach an
AI model, the platform's servers or a shared export. With
`orders_by_hand_only` no strategy, schedule or assistant trades a live
account, and the SDK sends an order on such a connection only on the
platform's word that each order is the user's own. Each flag only takes
something away, so any value but `False` or `None` counts as true, and a flag
you leave out is false. Leave `terms` out when your broker's terms say
neither.

## Try it in a platform

Register your adapter under a name of your own and every platform on this
contract lists it beside the built-in brokers:

```sh
python -m brueconnect add mybroker --file /path/to/my_adapter.py
python -m brueconnect check mybroker
```

`--file` stores the command as a list of arguments, so the path is spawned
exactly as written whatever it contains (spaces, Windows backslashes); add
`--arg X` for each argument your adapter takes (`--arg=--paper` for one that
starts with a dash). `--command "..."` is there for an adapter that is not a
Python file. A name is 2 to 32 characters of lower case letters, digits, `-`
and `_`. The LSE Terminal does the same from its Connections screen ("Add
your own broker") and runs a handshake before it stores anything.

## Prove it: the conformance suite

An adapter is conformant when the suite reports zero failures. Run it against
your adapter exactly as a platform would spawn it:

```sh
python3 -m brueconnect.conformance.run --adapter "python3 path/to/my_adapter.py"
```

That needs `brueconnect` importable (see Install). With only the LSE
Terminal's copy, put its `brue-sdk` folder on `PYTHONPATH` for the command,
which the adapter it starts inherits (the Terminal's card shows this line
for the starter adapter, with your computer's paths):

```sh
PYTHONPATH=/path/to/brue-sdk python3 -m brueconnect.conformance.run --adapter "python3 path/to/my_adapter.py"
```

Useful flags:

- `--partial-hint SYMBOL:QTY` a size known to fill partially, so the partial
  fill scenario can run.
- `--restartable` your adapter persists its state, so the reconnect scenario
  may kill and relaunch it.
- `--live` you accept that the suite will place REAL orders. It refuses to
  certify a non-paper account without this.
- `--auth-driver "cmd"` for a broker with an interactive login: a command
  that plays the human and the browser, given the login URL as its last
  argument. Without it, an adapter that needs a login cannot be certified and
  the suite says so rather than failing noisily. See
  `brueconnect/conformance/auth_drivers/meridian_login.py`.

Give each run its own state directory, the way a real profile gets one:

```sh
BRUE_BROKER_STATE=/tmp/mybroker-state \
python3 -m brueconnect.conformance.run --adapter "python3 path/to/my_adapter.py"
```

Read the SKIPs. A skip is the suite honestly reporting a capability you did
not declare; a skip you caused by declaring a capability false to dodge a
failing scenario is not conformance, it is hiding.

## Getting listed in a platform

Being conformant makes you connectable; a directory row makes you visible.
The platform reads a runtime directory, so a broker is added or delisted
without anyone updating an installed app. Your row carries:

| Field | What it is |
|---|---|
| `display_name`, `tagline` | your name and one line about you |
| `logo_svg`, `website` | your mark, rendered in the panel's ink |
| `transport` | how you speak (`rest_sse`, `fix`, `grpc`, ...), display only |
| `endpoint_host/port/path/tls` | where your adapter dials |
| `credential_form` | the fields to collect from the user before connecting; empty when you need nothing typed. With no row at all, the form in your own handshake is used (SPEC 3.2) |

Two presentation contracts in SPEC say what your fields become on screen, so
you can see the result before you send them: section 3.1 for your broker
identity, and section 4, *Choosing an account* (revision 1.7), for your
accounts.

## The rules that matter most

These are the ones integrations get wrong quietly, so they are worth stating
twice:

- `hello` answers COLD. No login, no account read, no stream in your startup
  path. A picker handshakes you just to learn your name.
- `mode` must be truthful, and it is per ACCOUNT once a login reaches
  several. Anything not `paper` is treated as real money and requires arming.
- Anything absent from `capabilities` is absent from you. The platform
  refuses the feature loudly rather than emulating it silently.
- Every money figure you report is already in the account's currency, at the
  rate you actually charge. The platform holds no exchange rates.
- Idempotency on `client_id` for every mutating call: a replay returns the
  original outcome and places nothing.
- `flatten_all` must work while everything else is failing or rate-limited.
- Persist refresh tokens. A broker that keeps them in memory signs every
  client out each time it deploys.

## Try a Brue script live on the paper adapter

From the repository root, `demo/trend.brue` trades the paper simulator on a
fast clock and stops after 40 bars:

```sh
python3 -m brueconnect.runner.live_driver --script demo/trend.brue \
    --symbol EURUSD --timeframe 1m \
    --adapter "python3 -m brueconnect.adapters.paper.paper_adapter --warmup-bars 240" \
    --warmup 60 --bars 40 --flatten-on-exit
```

`--broker <name>` in place of `--adapter` runs the same script on a broker you
registered with `python -m brueconnect add`.

## Layout

    SPEC.md                     the protocol (normative)
    ONBOARDING.md               the questionnaire for a new broker
    brueconnect/                the SDK: Adapter, Connector, protocol, registry
    brueconnect/adapters/       eight simulated-venue adapters plus paper, novafx, lse_sim, alpaca, oanda and capitalcom
    brueconnect/templates/      the starter adapter for your own broker
    brueconnect/conformance/    the suite that decides conformance
      auth_drivers/             stand-ins for a human at a login page
    tests/                      SDK unit tests (python3 -m unittest discover -s tests, about 20 minutes)
    demo/                       a Brue script to try against the paper adapter

The language itself (`brue/`, `SYNTAX.md`, `conformance/`) is described in
[README.md](README.md).
