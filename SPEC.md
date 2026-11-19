# Brue Connect protocol, version 1, catalog revision 1.11 (draft)

The universal broker connector. A platform (terminal, runner, notebook) talks
to a broker through an ADAPTER: a local process speaking this protocol on
stdio. Any broker becomes one adapter; the reference paper simulator is an
adapter with no special access. Adapters can be written in any language that
can read and write lines of JSON.

## 1. Transport and envelope

One JSON object per line (LF-terminated, UTF-8) in each direction. Three
envelope shapes:

Request (platform to adapter):

```json
{"v": 1, "id": "r42", "type": "req", "method": "order.place", "params": {}}
```

Response (adapter to platform, exactly one per request, any order):

```json
{"v": 1, "id": "r42", "type": "res", "result": {}}
{"v": 1, "id": "r42", "type": "res", "error": {"code": "invalid_qty", "message": "...", "retryable": false}}
```

Event (adapter to platform, unsolicited, monotonically increasing seq):

```json
{"v": 1, "type": "event", "seq": 7, "event": "order.filled", "data": {}}
```

Rules:

- `v` is the protocol major version. An adapter that cannot speak the
  requested version says so in `hello` and the platform disconnects.
- `id` is opaque to the adapter; it echoes it verbatim.
- A response carries `result` or `error`, never both.
- Events carry `seq` starting at 1 per session so a platform can detect gaps.
- Unknown fields are ignored everywhere (forward compatibility). Unknown
  METHODS return `error.code = "unsupported"`.
- A method is its exact name in section 4 and nothing else. Any other
  spelling of one (`order_place`, `flatten.all`, `Order.Place`) is an unknown
  method: the adapter answers it `unsupported` and does nothing for it. A
  platform's rules (arming, section 3; `client_id`, section 2) know a mutating
  method by its exact name, so an adapter that maps another spelling onto it
  carries out an order no rule has seen. The SDK's `Connector` sends no name
  outside section 4, and the conformance suite asks two that are not (one
  spells `order.place` another way, with no parameters).

## 2. Time, ids, money

- Every timestamp in every message is UTC epoch milliseconds. Adapters
  convert broker-local time at the boundary; the platform never sees it.
- Every mutating request carries a platform-generated `client_id` (unique
  string): `order.place`, `order.modify`, `order.amend`, `order.cancel`,
  `position.close`, `position.close_by` and `flatten_all` all take one.
  Adapters MUST be idempotent on it: a request whose `client_id` was already
  seen returns the ORIGINAL outcome and does nothing a second time. This is
  not only for orders. A `position.close` retried after a dropped response
  must not close twice, and a retried `flatten_all` must not re-close a
  position the user has since reopened. The platform side of the guarantee
  (a `client_id` reused with DIFFERENT parameters is refused
  `duplicate_client_id`) is enforced once in the SDK's `Connector` for every
  mutating method, so an adapter inherits it; the adapter's own duty is to
  return the prior result rather than act again.
- Quantities and prices are JSON numbers in instrument units as declared by
  the catalog (qty in lots/units per `lot_step`, prices in quote currency).
- **The broker converts; the platform never does.** Every figure that is
  MONEY IN THE CLIENT'S ACCOUNT arrives already converted into the account's
  currency, at the rate the broker actually charges: balance, equity, margin
  used, realised and unrealised P/L, commission, swap, and every
  `balance.operation`. The platform holds no exchange rates, and it will not
  cross a broker's own tradable FX pair to make one, because a broker's
  conversion rate and its quoted pair are allowed to differ and a near-miss
  presented as the broker's number is worse than an honest foreign-currency
  one.

  What is NOT converted is anything describing the CONTRACT rather than the
  balance: the price, `tick_size`, `tick_value` and `min_notional` stay in
  the instrument's `quote_ccy`. That leaves one gap, pre-trade sizing in the
  trader's own money, and `tick_value_account` (section 4) is the single
  optional field that closes it. Where a broker does not send it, the
  platform shows the `quote_ccy` figure and names that currency rather than
  guessing.

## 3. Handshake

Every request and response example from here on shows only `method`/`params`
or `result` for readability. The full envelope from section 1 ALWAYS applies:
a real request on the wire is `{"v":1,"id":"...","type":"req","method":...,
"params":...}` and a real response carries `v`/`id`/`type:"res"`. Parse the
envelope, not the abbreviation.

First request is always `hello`:

```json
{"method": "hello", "params": {"protocol": 1, "client": {"name": "lse-terminal", "version": "0.1"}}}
```

Response:

```json
{"result": {
  "protocol": 1,
  "adapter": {"name": "paper", "version": "0.1"},
  "broker": "brue paper simulator",
  "mode": "paper",
  "account_currency": "USD",
  "account_model": "netting",
  "capabilities": {
    "orders": {"market": true, "brackets": true, "modify": true,
               "pending": false, "trailing": false, "partial_close": true,
               "pending_types": [], "expiry": [], "oco": false,
               "close_by": false},
    "data": {"quotes": true, "bars": true, "history": true,
             "operations": false,
             "max_quote_symbols": 30, "max_symbols_per_request": 50},
    "flatten_all": true
  },
  "rate_limits": {"requests_per_second": 50}
}}
```

Rules:

- `mode` is `"paper"` or `"live"`. The platform MUST surface it to the user
  at all times and MUST require explicit arming before sending mutating
  calls to a `"live"` adapter. Only `"paper"` is exempt: a missing or
  unrecognised mode is treated as live, so a broker that says nothing gets
  the careful path rather than the cheap one. The mutating calls are
  `order.place`, `order.modify`, `order.amend`, `order.cancel`,
  `position.close`, `position.close_by` and `flatten_all`; reading an
  account is not trading it. The SDK enforces this inside `Connector`
  (`is_paper`, `arm(reason)`, and a refusal in `request`), so every platform
  built on it inherits one rule rather than writing its own. A broker whose
  terms keep live orders by hand takes mutating calls only on an arming
  that carries the platform's `by_hand` word (3.3).
- `account_model` is `"netting"` (one position per symbol) or `"hedging"`
  (many). Platforms must handle both; scripts never see the difference.
- `flatten_all: true` is REQUIRED. An adapter without it is non-conformant.
- One adapter process serves ONE account. Choosing among a login's several
  accounts is adapter configuration, not protocol.
- Anything absent from `capabilities` is absent from the broker. The
  platform either refuses the feature loudly or emulates it and marks the
  result as emulated. Silent degradation is forbidden.
- `orders.pending` true means the adapter implements section 4's resting
  order methods and events. It MUST then also send `pending_types`, listing
  which of `limit stop stop_limit` the broker actually accepts, and
  `expiry`, listing which of `gtc day gtd` it honours. A capability flag
  with an empty list is a contradiction and the platform disconnects.
- `orders.can_list` false means the broker has NO call that enumerates
  orders, so `orders.list` cannot be answered. Default true. A platform MUST
  then skip `orders.list` during reconciliation and mark its order view as
  PARTIAL rather than treating an empty result as "no resting orders". This
  exists because some venues have no enumerate call at all: the
  adapter can only report orders it placed itself and remembered, and any
  order placed by another session is invisible to it forever. Saying so is
  better than a platform quietly believing it has reconciled.
- `orders.oco` true means `order.place` accepts an `oco_group` and the
  BROKER cancels the siblings when one of the group fills. A platform must
  not emulate this by racing to cancel the other leg itself: on a fast move
  it loses the race and the client holds both.
- `orders.close_by` true means `position.close_by` is implemented. It is the
  one operation a netting-only platform has no words for, and on a hedging
  book it saves the client a round trip and a spread.
- `data.operations` true means `history.operations` is implemented and
  `balance.operation` events are emitted. Where it is false, a balance that
  moves without a fill has no explanation available and the platform must
  say so rather than silently attributing it to trading.
- `data.max_quote_symbols` is how many symbols the broker will stream to one
  connection at once, and `data.max_symbols_per_request` how many may be named
  in a single `quotes.subscribe`. Both OPTIONAL; absent means the broker states
  no limit. THE BROKER DECIDES THESE, and a platform that knows them up front
  can plan instead of discovering a cap by hitting it.

  This matters because a platform does not only price what is on screen. A
  strategy watching a basket ("when gold, EURUSD, USDJPY and GBPUSD are all
  overbought, sell the index") needs every one of those streaming at once,
  plus the instrument it trades, none of which the user is looking at.

  A platform MUST NOT exceed a declared cap, and when it wants more than the
  cap it MUST choose which symbols to keep DELIBERATELY and surface the rest
  as unpriced. It must not quietly send the whole list and hope. An adapter
  whose broker caps it lower than requested does the same: it takes what it
  can, and reports the remainder as `session.state: degraded` rather than
  leaving prices that silently never arrive.
- `orders.trailing` true means `trail_points` is accepted on `order.place`
  and `order.modify` and maintained BY THE BROKER. A platform must never
  emulate a trailing stop by sending modifications on every tick; if the
  broker cannot trail, the platform refuses the feature loudly.

### 3.1 Broker display identity (1.4)

`hello` may carry an optional `broker_info`: what the broker wants a human to
see about it. A platform shows connections in a picker, and without this the
only thing it can show about an adapter it has never met is a filename. With
it, a broker nobody has pre-integrated appears in that picker under its own
name, alongside every other connection.

```json
"broker_info": {
  "display_name": "AcmeFX Markets",
  "tagline": "ECN pricing, 24/5 FX and metals",
  "mark": "AF",
  "website": "https://acmefx.example",
  "support": "mailto:support@acmefx.example",
  "country": "Cyprus",
  "regulated_by": ["CySEC 000/00"],
  "asset_classes": ["FX", "Metals", "Indices"]
}
```

| Field | Cap | Meaning |
|---|---|---|
| `display_name` | 40 chars | what to call you in a menu; falls back to `broker` |
| `tagline` | 80 chars | one line under the name |
| `mark` | 3 chars | monogram for a small tile, when a platform draws one |
| `website` | https only | where a user reads your terms |
| `support` | https or mailto | where a user asks for help |
| `country` | 40 chars | where you are established |
| `regulated_by` | 6 x 40 chars | licences, as you would print them |
| `asset_classes` | 12 x 24 chars | what you deal, in your own words |

Rules:

- Every field is OPTIONAL and every field is DECORATION. An adapter that
  sends none of it still works, and a platform must not read behaviour out of
  any of it: what you can trade comes from `catalog.list`, what the platform
  may do comes from `capabilities`, and whether the money is real comes from
  `mode`. None of it may be overridden here.
- `broker_info` is NOT identity. The connection is identified by its local
  profile name, which the user chose (`brueconnect add <name>`). A platform
  MUST display that name too, so a broker cannot become another by copying
  its `display_name`, and MUST NOT let `broker_info` decide trust, routing or
  ordering.
- `mode` still wins over anything written here. A `live` adapter with a
  reassuring tagline is still `live` and still needs explicit arming.
- The text is third-party and platform-bound, so a platform MUST sanitise it:
  cap the lengths above, strip control characters, allow no URL scheme but
  `https:` and `mailto:`, and render it as text and never as markup. The SDK
  does this for you in `protocol.sanitize_broker_info`, applied on both sides
  of the pipe; oversized, malformed and hostile fields are dropped rather
  than rejected, so a careless field never costs a broker its connection.
- No logos, no images, no colours. A picker draws the monogram in its own
  house style. This is the one place a broker could have pushed remote assets
  into every platform's UI, and it deliberately cannot.
- `hello` SHOULD be answerable without opening a session on the broker. A
  platform may handshake an adapter purely to learn what to call it, so a
  login, an account read or a market-data stream in your startup path becomes
  a bill for being listed in a menu. Put them in the SDK's `on_first_call`
  hook (or the equivalent) and they happen on the first request that actually
  needs the broker. A platform, for its part, MUST cache what it learns and
  MUST NOT handshake a broker repeatedly to redraw a list.

### 3.2 What the user must type: the credential form (1.10)

`hello` may carry an optional `credential_form`: the adapter's own statement
of what a user has to type before it can connect. A platform draws its
connect form from it, so a broker nobody has pre-integrated can ask for its
keys in the same screen as every listed one, with no entry in anyone's
directory.

```json
"credential_form": [
  {"key": "api_key_id", "label": "API key ID", "type": "text"},
  {"key": "api_secret", "label": "API secret", "type": "password"}
]
```

| Field | Rule | Meaning |
|---|---|---|
| `key` | `^[a-z][a-z0-9_]{0,31}$` | names the value; it reaches you as `BRUE_CRED_<KEY>`, upper-cased |
| `label` | 40 chars of plain text | what to print beside the input; falls back to `key` |
| `type` | `"text"` or `"password"` | whether the platform hides what is typed; absent means `"password"` |

At most 8 fields.

Rules:

- The form NAMES fields and never carries a value. What the user types
  reaches the adapter exactly as section 8 already says and no other way: as
  `BRUE_CRED_<KEY>` in its environment at the next spawn. No message in
  either direction holds a credential, and `hello` still carries no secret.
- `hello` is answered cold, before any credential exists, so the form is a
  constant of the adapter's configuration. It is not fetched from the broker
  and does not change with what has been typed.
- It is OPTIONAL and it is DECORATION, on the same terms as `broker_info`. An
  adapter that sends none still works, and an older platform ignores the
  field by the unknown-fields rule; its user then supplies `BRUE_CRED_*` some
  other way. A platform must not read behaviour out of it. There is no
  "required" flag on purpose: an adapter that cannot proceed without a value
  answers `unauthorized` with a sentence that names what is missing, which is
  true whatever the platform drew.
- Where a platform already knows a broker's fields from its own listing, the
  listing wins. The handshake's form is for the broker it has no listing for.
- The text is third-party and platform-bound, so a platform MUST sanitise it
  and MUST render every label as text, never as markup. The SDK does the
  sanitising in `protocol.sanitize_credential_form`, applied on both sides of
  the pipe. A malformed entry is dropped rather than rejected, so a careless
  field never costs a broker its connection: an entry that is not an object,
  whose `key` does not match the pattern or repeats an earlier one, or whose
  `type` is present and is neither of the two; and every entry after the
  eighth. A `label` is cleaned and capped exactly as a `broker_info` string.
  Nothing but the three fields above survives.
- A platform keeps what the user typed on the user's own machine only
  (section 8). It SHOULD show which fields already hold a value without ever
  showing the value, MUST NOT treat an input left blank as an instruction to
  delete a stored value, and SHOULD offer forgetting the stored values as an
  action of its own.

### 3.3 What the broker's terms allow (1.11)

`hello` may carry an optional `terms`: what the broker's own agreement with
its customer lets a platform do with this connection. Some brokers license
their prices to the account holder alone, and some require every order to be
placed by a person. The adapter is the one place that knows its broker, so it
says so here, and every platform can keep the user inside those terms
without a table of brokers of its own.

```json
"terms": {
  "data_local_only": true,
  "orders_by_hand_only": false,
  "summary": "The broker's prices are licensed to you alone: they stay on this computer.",
  "url": "https://broker.example/legal/api-licence"
}
```

| Field | Rule | Meaning |
|---|---|---|
| `data_local_only` | absent, `false` or `null` means false; any other value means true | the broker's market data is for the account holder, on this computer |
| `orders_by_hand_only` | the same | on a live account, each order follows a person's own action for it |
| `summary` | 200 chars of plain text | the terms in one sentence, in words a user reads |
| `url` | https only | where the user reads the terms themselves |

Rules:

- Unlike `broker_info` and `credential_form`, `terms` is NOT decoration. A
  platform MUST act on both flags. Each one only takes something away from
  the platform, so an adapter gains nothing by declaring one, and a platform
  can believe it.
- The flags fail closed. Any value but absent, `false` or `null` counts as
  true, and a `terms` that is present, not `null` and not an object sets
  both (`"terms": null` reads as no `terms`). A typo that weakened a licence
  term would put the user's account at risk; one that strengthens it costs a
  feature, and the platform says which.
- `data_local_only` true: the platform may show this connection's market data
  (quotes, bars, history, and the current price in its positions) to the user
  and use it on the user's own computer, in charts, indicators and the user's
  strategies. It MUST NOT send that data, or anything that reproduces it, off
  the computer: not to an AI model, hosted or the user's own; not to the
  platform's servers, analytics, telemetry or crash reports; not into a file
  or an export made to be shared. A chart image, a table of prices and a
  candle file all reproduce it. Where a feature would do any of these, the
  platform refuses the feature for this connection and says why, naming the
  broker's terms. Sending less without saying so is forbidden, as everywhere
  in section 3. The account's own records (its balance, the sizes and entry
  prices of its positions, the levels the user set, the user's own orders
  and fills) are not market data. A figure marked to the current price
  reproduces it and goes with it: a position's unrealised P/L or current
  value, and the account's equity, margin in use, free margin and margin
  level.
- `orders_by_hand_only` true, on a connection whose `mode` is not `"paper"`:
  the platform sends a mutating call (section 3) only as the direct result of
  the user's own action for that one order: a button pressed on a ticket, or
  a confirmation that shows that order and nothing else. A strategy, a
  schedule or an assistant MUST NOT send one by itself; a platform either
  asks the user to confirm each order it would send, or refuses to run the
  strategy or assistant on that connection, and says why. Stop-loss and
  take-profit orders resting at the broker are the broker's to trigger and
  are not affected. On paper the flag does not bind the platform, which
  SHOULD still show `summary` when a strategy starts there.
- `summary` and `url` are decoration, sanitised like `broker_info`. A platform
  SHOULD show `summary` beside the connection, and when the user arms a live
  connection or starts a strategy on one.
- `hello` is answered cold, so `terms` is a constant of the adapter, like the
  credential form.
- Where a platform already lists a broker with terms of its own, the stricter
  of the two applies, flag by flag. A platform that lists a broker SHOULD
  carry its terms in that listing too, so they hold before the first `hello`
  and with an adapter older than 1.11.
- The SDK applies `protocol.sanitize_terms` on both sides of the pipe and
  exposes the result as `Connector.terms`. It always has both flags, false for
  an adapter that sends no `terms`.
- The SDK enforces `orders_by_hand_only` where every platform consents to
  real money: `Connector.arm()` refuses a connection that is not paper and
  whose terms set it, unless the platform passes `by_hand=True`. That is the
  platform's statement that every mutating call it sends on the connection
  follows the user's own action for that one order. A platform passes it
  only when it holds every other sender back itself. The word is checked
  again at every mutating call, against the terms known then, so a
  connection armed before its `hello` (or before a `hello` that brought the
  flag) sends no order without it. And a connection whose handshake was
  never read sends no mutating call at all: none before `hello`, none after
  one that timed out, none to an adapter process started again before that
  process's own `hello` (a restart keeps nothing the last process said: its
  handshake, its catalog, account and book, its streams; and it clears the
  arming, even one being made as the restart lands), and a `hello` whose
  result is not an object, or not of this protocol (its `protocol` not the
  number 1: `true` is not a number), ends the connection and is never kept;
  such a `hello` answered by the last process after a restart ends nothing.
  The kit's own
  strategy runner and the conformance suite never pass `by_hand`, and
  refuse a live connection whose terms set the flag before arming, with
  the reason.

## 4. Methods

### Session

| method | params | result |
|---|---|---|
| `hello` | protocol, client | see above |
| `ping` | none | `{"time": ms}` |
| `bye` | none | `{}`; adapter flushes state and exits |

### Interactive login (capability `auth`, 1.6)

Some brokers authenticate a HUMAN, not a machine: the user logs in on the
broker's own page (username, password, 2FA) and the adapter ends up holding
a token. An adapter for such a broker declares it in the handshake:

```json
"capabilities": {"auth": {"interactive": true}, ...}
```

| method | params | result |
|---|---|---|
| `auth.status` | none | `{"authenticated": bool, "detail": str}` |
| `auth.begin` | none | `{"url": "https://...", "expires_in": s}` |
| `auth.logout` | none | `{}`; stored tokens destroyed |

Rules:

- `auth.*` is answerable WITHOUT an open broker session, like `hello`: the
  session cannot exist until the login these methods perform has happened.
- Any session-needing method called while the adapter holds no usable
  credentials fails with error `auth_required` (retryable). The platform
  reacts by calling `auth.begin` and showing the user the returned `url`,
  never by re-sending the request in a loop.
- `auth.begin` does everything broker-specific itself: it generates the
  PKCE material, starts its own loopback redirect listener on 127.0.0.1
  (ephemeral port, RFC 8252), and returns only the URL the user's browser
  must visit. When the user finishes on the broker's page the adapter
  exchanges the code, persists what it needs in ITS OWN state directory,
  and emits the `auth.completed` event. Calling `auth.begin` again
  abandons the previous attempt and starts a fresh one.
- The platform's whole job is: open `url` in the user's browser, wait for
  `auth.completed`, retry the connect. It never sees a password, a code,
  a verifier, or a token; section 8.5 is unchanged. An adapter that puts a
  secret in `auth.begin`'s result is non-conformant.
- Tokens outlive the process (the state directory), so a restart does NOT
  re-prompt the user while a stored refresh token still works. `auth.status`
  tells the truth about whether a login is needed RIGHT NOW, which may cost
  the adapter one round trip to its broker.

Event (section 5): `auth.completed` `{"ok": bool, "reason": str?}`. `ok`
false carries a human-readable `reason` (denied, timed out, venue down);
the platform shows it and may offer `auth.begin` again.

#### Session lifetime: signed in means signed in

A login is not a per-run ceremony. Once the user has completed the broker's
page, they stay signed in until something REAL ends it, and a platform that
re-prompts for any other reason is broken.

- Two tokens, two jobs. The short-lived access token is refreshed silently
  and the user never sees it expire. The REFRESH token is the session, and
  it is what must last: months, not hours.
- The adapter persists the refresh token in its own state directory so a
  restart of the ADAPTER, the platform, or the machine costs nothing.
- The broker MUST persist it too. A broker that keeps refresh tokens only in
  memory signs every client out every time it deploys, which no real broker
  does and no user can explain. (A venue that keeps refresh tokens only in
  memory logs every client out when it restarts; persist them, 90 days is a
  sane life.)
- A session ends for exactly four reasons: the user logged out
  (`auth.logout`), the credential changed at the broker, the broker revoked
  it, or it reached its documented expiry. Nothing else.
- When the broker refuses a stored refresh token, the adapter DESTROYS it
  and reports `auth_required`. Holding a credential the broker rejects is
  the same as holding none, and keeping it makes `auth.status` lie.
- `auth.status` therefore answers about RIGHT NOW, and may cost the adapter
  one round trip to find out.

### Choosing an account (capability `accounts`, 1.7)

One login usually reaches SEVERAL accounts: a spread bet account beside a
CFD one, a currency each, a demo next to the live ones. One adapter process
still serves exactly ONE account (section 3); this is how the platform finds
out what the choices are and binds the process to one of them.

A broker whose login can reach more than one account declares:

```json
"capabilities": {"accounts": {"multi": true}, ...}
```

| method | params | result |
|---|---|---|
| `accounts.list` | none | `{"accounts": [Account]}` |

Account:

```json
{"id": "8842107", "label": "Spread Bet", "currency": "GBP",
 "mode": "live", "type": "spread_bet", "balance": 25000.0, "selected": true}
```

- `id` is REQUIRED and is what the platform stores and passes back. Every
  other field is optional, because a broker that only returns ids must be
  representable: the platform then shows the id and nothing else. A platform
  MUST NOT invent a label, a type, or a currency it was not given. An
  account labelled by a guess is worse than an account labelled by its id.
- `mode` is per ACCOUNT and means the same thing it means in the handshake:
  `"live"` is real money. A login with a live and a demo account produces
  different `mode` values from the same broker, and the handshake's `mode`
  MUST be the mode of the account this process is BOUND to. Absent means
  live, the careful default (section 3).
- `label` is the broker's own name for the account, `type` its product
  vocabulary (`spread_bet`, `cfd`, `margin`, `cash`, `isa`). Both are
  display-only; nothing may branch on them.
- `selected` marks the account this process is bound to, when the adapter
  already has one.

Binding. The account is chosen BEFORE the process exists: the platform spawns
the adapter with the environment variable `BRUE_ACCOUNT_ID` set to the
chosen `id`. That is what section 3's "choosing among a login's several
accounts is adapter configuration" means in practice; the difference is that
the platform can now fill that configuration in, instead of a human editing
it. Nothing selects an account mid-session: switching account is a new
process, because positions, orders and the platform's arming decision all
belong to the process and would otherwise be silently wrong.

`accounts.list` is answerable WITHOUT an open session, like `auth.*` and
`hello`: the whole point is to answer it between logging in and opening the
session the answer decides. Where a login reaches exactly one account (or
the broker's token is already bound to one), returning that single account
is correct and the platform shows no chooser.

#### How a platform must present them

This is a presentation contract, like section 3.1's: it exists so a broker
knows what its fields become on screen, and so every broker is shown the
same way.

- EVERY account the login reaches is listed as soon as they are known, and
  stays listed while connected. Which account an order lands on is the most
  consequential fact on the screen, so it is shown, not hidden behind a
  button.
- The account IN USE is marked as such and is not offered as a choice. One
  row is a statement; the others are options.
- Each row shows, in this order: the broker's `label` (or the `id` when
  there is none), its `mode` as LIVE or DEMO, the `id`, the `type`, and the
  `balance` with its `currency`. Nothing on the row is invented: a field the
  broker did not send is absent, never guessed at or defaulted.
- LIVE and DEMO are distinguished by wording and weight, NOT by alarm
  colour. A live account is normal for most traders; colouring it like an
  error trains people to ignore the one signal that matters.
- Switching account is confirmed, not instant: the first interaction shows
  what the account is (real money or demo) and what happens next; the second
  commits. A platform MUST NOT switch the account an order would reach on a
  single stray click.
- The chooser NEVER pre-selects. Where nothing is bound yet, the platform
  asks; it does not quietly take the login's primary, which on most brokers
  is a live account.

### Catalog

| method | params | result |
|---|---|---|
| `catalog.list` | none | `{"instruments": [InstrumentSpec]}` |

InstrumentSpec:

```json
{"symbol": "EURUSD", "name": "Euro vs US Dollar", "asset_class": "fx",
 "base_ccy": "EUR", "quote_ccy": "USD", "digits": 5, "tick_size": 0.00001,
 "tick_value": 1.0, "lot_step": 0.01, "min_qty": 0.01, "max_qty": 500,
 "min_notional": 0, "contract_size": 100000,
 "margin_rate": 0.0333, "margin_maintenance_rate": 0.01665, "margin_tiers": null,
 "sessions": [{"days": [1,2,3,4,5], "open": "00:00", "close": "24:00", "tz": "UTC"}],
 "holidays": ["2026-12-25"],
 "early_closes": [{"date": "2026-12-24", "close": "18:00", "tz": "UTC"}],
 "tradable": true, "shortable": true, "trade_mode": "full",
 "stops_level_points": 0,
 "fees": {"commission_per_lot": 0, "mode": "none", "rate": 0, "min": 0},
 "swap_long": -0.5, "swap_short": 0.1, "swap_mode": "points", "swap_3day_dow": 3}
```

`sessions.days` uses ISO weekday numbers (1 = Monday). A platform gates
order sending on sessions + holidays (the tradability gate); the adapter is
still the final judge and rejects with `market_closed`.

An EMPTY `sessions` list means the broker publishes no trading calendar for
that instrument. It does NOT mean the market is closed, and a platform MUST
NOT infer closure from it: with no calendar there is nothing to gate on, so
the platform sends the order and the adapter remains the judge. A broker that
really is always open should say so explicitly, with a single 24/7 session
(`{"days": [1,2,3,4,5,6,7], "open": "00:00", "close": "24:00", "tz": "UTC"}`),
because "always open" and "unknown" look identical otherwise and only one of
them is safe to gate on. This sentence exists because an empty list is easy
to read as "permanently closed", and a broker that publishes no calendar then
disagrees with the platform about every instrument it deals.

Fields added in the 1.1 revision (all optional; platforms treat a missing
field as the stated default, per the unknown-fields rule):

- `tick_value`: value of one `tick_size` move per one contract, in
  `quote_ccy`. Not always `contract_size * tick_size` (cross-currency
  contracts), so brokers that know it MUST send it. Default: derived as
  `contract_size * tick_size`.
- `tick_value_account`: OPTIONAL. The same value as `tick_value`, but in the
  ACCOUNT's currency, converted by the BROKER at the rate it actually
  charges. Send it and a platform can size a position and estimate margin in
  the trader's own money; omit it and the platform shows the `quote_ccy`
  figure, says which currency that is, and sizes in nothing else.

  The platform NEVER derives this. It will not cross your tradable FX pair,
  it will not use a public rate, and it will not estimate: your conversion
  rate and your quoted pair are allowed to differ, and a near-miss presented
  as your number is worse than an honest foreign-currency one. This is the
  single field that closes the gap, and only you can fill it, because only
  you know the rate the client is charged.
- `min_notional`: minimum order value in `quote_ccy` (crypto venues gate on
  notional, not qty). Default 0.
- `margin_maintenance_rate`: maintenance margin; stop-out math runs on this,
  `margin_rate` is initial. Default: `margin_rate`.
- `margin_tiers`: array of `{"above_notional": N, "margin_rate": R}` for
  brokers that step margin by position size. `null` = flat rate.
- `trade_mode`: `"full" | "long_only" | "close_only" | "disabled"`. Richer
  than the `tradable` boolean (which stays for compatibility: `tradable` is
  false only when `trade_mode` is `"disabled"`). Default `"full"`.
- `stops_level_points`: minimum distance, in points (`tick_size` units),
  between current price and any SL/TP/stop price; brokers reject closer
  stops. 0 = no restriction. Default 0.
- `fees.mode` + `fees.rate` + `fees.min`: commission model, one of
  `"none" | "per_lot" | "per_share" | "pct_notional"`, with a minimum charge
  per order. `commission_per_lot` remains as the compatibility per-lot alias and
  MUST equal `rate` when `mode` is `"per_lot"`. Default: whatever
  `commission_per_lot` says, mode `"per_lot"`.
- `swap_mode`: units of `swap_long`/`swap_short`, one of
  `"none" | "points" | "pct_annual" | "currency_per_lot"`. Without it the
  swap numbers are uninterpretable across brokers. Default `"points"`.
- `swap_3day_dow`: ISO weekday on which triple swap is charged (3 =
  Wednesday for most FX, 5 = Friday for many index/equity CFDs). Omitted =
  no triple-swap day (e.g. crypto venues charging every calendar day).

Fields added in the 1.3 revision:

- `expires_at`: UTC epoch milliseconds at which this contract stops dealing,
  `null` for a perpetual instrument. Futures CFDs expire, and a platform that
  treats every symbol as permanent will hold a position into a close it did
  not expect.
- `successor`: the symbol that replaces this one at expiry, `null` if none.
  Brokers usually list it alongside the front contract, so it is tradable
  before the roll.
- `dividend`: `{"ex_date", "amount_per_share"}` for share and ETF CFDs, or
  `null`. Longs are paid and shorts charged on the ex-date, as a
  `balance.operation` of kind `dividend`.

Fields added in the 1.2 revision:

- `early_closes`: dates on which the instrument stops trading earlier than
  its `sessions` say. Each entry is `{"date", "close", "tz"}` and truncates
  every session window that day; windows starting after `close` do not run
  at all. `holidays` cancels a day, `early_closes` shortens one, and a date
  must not appear in both. The tradability gate MUST apply these: an
  instrument whose week says it closes at 22:00 really does stop at 18:00 on
  Christmas Eve, and a platform that ignores the field will sit waiting for
  a fill that cannot come.

### Account

| method | params | result |
|---|---|---|
| `account.get` | none | `{"balance", "equity", "margin_used", "margin_free", "currency"[, "account_id", "label"]}` |

`account_id` and `label` are OPTIONAL and identify WHICH account these
figures belong to. A platform that can reach several accounts on one login
must be able to name the one on screen, and a trader should never have to
infer that from the balance.

`currency` is the ACCOUNT's denomination and is authoritative. Every money
figure the adapter reports, in `account.get`, on a Position, on a Fill and on
an Operation, is already in it. A platform that assumes dollars because most
accounts are dollars will be wrong by an exchange rate on every number it
shows.

Money also moves for reasons that are not trades: funding, withdrawals,
credit, dividends on share and ETF CFDs, and dealer adjustments. Those arrive
as `balance.operation` events and are listable through `history.operations`
when `data.operations` is true.

Operation:

```json
{"operation_id": "op412", "kind": "dividend", "amount": -12.40,
 "currency": "USD", "balance_after": 9871.20, "reference": "AAPL.US",
 "comment": "ex-date 2026-08-07, 0.26 per share", "time": 1785340867221}
```

`kind` is `deposit`, `withdrawal`, `credit`, `dividend`, `adjustment`,
`fee` or `other`. `amount` is SIGNED in the account currency: negative took
money out. `reference` is the symbol for a dividend and the broker's own
receipt for funding. An adapter whose broker reports an unexplained balance
change sends `kind: "other"` and says so in `comment`; it must not guess.

### Market data (capability `data`)

| method | params | result |
|---|---|---|
| `quotes.subscribe` | `{"symbols": [..]}` | `{}`; `quote` events follow |
| `quotes.unsubscribe` | `{"symbols": [..]}` | `{}` |
| `bars.history` | `{"symbol", "timeframe", "count"}` | `{"bars": [[t,o,h,l,c,vol], ..]}` oldest first |
| `bars.subscribe` | `{"symbol", "timeframe"}` | `{}`; `bar` events follow, CLOSED bars only |
| `bars.unsubscribe` | `{"symbol", "timeframe"}` | `{}` |

Timeframes: `1m 5m 15m 30m 1h 4h 1d`.

### Orders

| method | params | result |
|---|---|---|
| `order.place` | `{"client_id", "symbol", "side": "buy"|"sell", "qty", "type"?, "price"?, "stop_price"?, "expiry"?, "expiry_at"?, "sl"?, "tp"?, "trail_points"?}` | `{"order_id", "status": "accepted"|"resting"}` |
| `order.modify` | `{"client_id", "position_id", "sl"?, "tp"?, "trail_points"?}` (null clears) | `{}` |
| `orders.list` | `{"state"?}` | `{"orders": [Order]}` |
| `order.amend` | `{"client_id", "order_id", "price"?, "stop_price"?, "qty"?, "sl"?, "tp"?, "expiry"?, "expiry_at"?}` | `{}` |
| `order.cancel` | `{"client_id", "order_id"}` | `{}` |
| `position.close_by` | `{"client_id", "position_id", "against_position_id"}` | `{"closed_qty", "realized_pnl"}` |
| `history.operations` | `{"from"?, "to"?, "limit"?}` | `{"operations": [Operation]}` |
| `positions.list` | none | `{"positions": [Position]}` |
| `position.close` | `{"client_id", "position_id", "qty"?}` | `{}`; fills arrive as events |
| `flatten_all` | `{"client_id"}` | `{"closed": n}` |
| `history.fills` | `{"from"?, "to"?}` | `{"fills": [Fill]}` |

`order.place` defaults to `type: "market"`. The other four types rest on the
broker's book and are available only when `orders.pending` is true and the
type appears in `capabilities.orders.pending_types`:

| type | needs | rests | becomes |
|---|---|---|---|
| `market` | nothing | never | a position immediately |
| `limit` | `price` | on the far side of the market | a position at `price` or better |
| `stop` | `price` | on the near side | a position at the market when touched |
| `stop_limit` | `stop_price` and `price` | on the near side | a `limit` at `price` when `stop_price` is touched |

Rules:

- A resting order is NOT a position. `positions.list` does not contain it,
  `orders.list` does, and the two id spaces MAY overlap on brokers that
  reuse the ticket. A platform MUST NOT assume `order_id` and
  `position_id` are distinct, and MUST NOT assume they are the same.
- `status` is `"accepted"` for a market order and `"resting"` for the rest.
  Either way it is transport-level only; the truth arrives as events.
- `expiry` is `"gtc"` (default), `"day"` or `"gtd"`. `gtd` requires
  `expiry_at`, UTC epoch milliseconds like every other timestamp. `day`
  means the end of the BROKER's trading day, which the platform cannot
  compute and MUST NOT try to; the adapter reports the resulting
  `expires_at` in the Order.
- Margin for a resting order MAY be reserved at placement or checked only at
  trigger, and the adapter MUST say which in `capabilities.orders`
  as `"reserves_margin": true|false`. Where it is false, an order accepted
  comfortably can still die at the moment it should have filled; that
  arrives as `order.cancelled` with a code, not as a fill.
- `oco_group` is a client-chosen string on `order.place`. Every resting
  order carrying the same group belongs to one set, and the first to fill
  cancels the others; the cancellations arrive as `order.cancelled` with
  `code: "oco"`. Available only when `orders.oco` is true.
- Pending orders fill in full unless the adapter also declares
  `partial_close`; brokers that clip resting orders emit `order.partial`
  exactly as they do for market orders.

Order:

```json
{"order_id": "o91", "client_id": "eurusd-1721..-4", "symbol": "EURUSD",
 "type": "limit", "side": "buy", "qty": 0.10,
 "filled_qty": 0.04, "remaining_qty": 0.06, "price": 1.08300,
 "stop_price": null, "sl": 1.08000, "tp": 1.09000,
 "expiry": "gtd", "expires_at": 1785340867221,
 "state": "resting", "placed_at": 1785300000000,
 "done_at": null, "fill_price": null, "reason": null}
```

`state` is `resting`, `filled`, `cancelled` or `expired`. `reason` is set on
everything except `resting` and carries an error code from section 6 when
the broker killed the order itself.

`qty` is the order's ORIGINAL size. `filled_qty` is how much has executed so
far and `remaining_qty` is what is still working (`qty` = `filled_qty` +
`remaining_qty`). Both are REQUIRED on any broker that can partially fill a
resting order, and they matter most at reconciliation (section 7): after a
reconnect, `orders.list` is the source of truth, and a partially-filled
resting order that reported only its original `qty` there would make the
platform double-count the executed part (already a position) against the
whole order. A broker that only ever fills in full may send
`filled_qty: 0`, `remaining_qty: qty` while resting.

Position:

```json
{"position_id": "p3", "client_id": "eurusd-1721..-1", "symbol": "EURUSD",
 "side": "buy", "qty": 0.10, "avg_price": 1.08843, "sl": 1.08300, "tp": null,
 "trail_points": 0, "unrealized_pnl": 4.20, "swap": -1.35,
 "commission": -3.50, "opened_at": 1721000000000, "from_order": null}
```

`swap` and `commission` are the financing and fees ALREADY charged against
this position, in account currency, negative when they cost the holder.
Added in 1.2: without them a platform cannot show what a position costs to
hold, and `unrealized_pnl` alone silently mixes market movement with carry.
An adapter whose broker does not separate them sends `0` and reports the
combined figure in `unrealized_pnl`; it must not invent a split.

`client_id` on a position is the client id of the order that OPENED it, and it
is OPTIONAL: on a netting book several orders merge into one position, and
after a reconnect the mapping may be gone entirely. A platform MUST identify a
position by `position_id` and must not require `client_id` to be present.

Fill:

```json
{"fill_id": "f552", "order_id": "o91", "position_id": "p3",
 "client_id": "eurusd-1721..-4", "symbol": "EURUSD", "side": "buy",
 "qty": 0.10, "price": 1.08843, "fees": -3.50, "time": 1721000000123}
```

A Fill is one execution: the fields the `order.filled`/`order.partial` events
carry (section 5), including `fill_id`. `fees` is the fee for THIS execution
in account currency, negative when it cost the holder. `history.fills`
returns these oldest first. `order_id`/`position_id`/`client_id` may be null
for a fill the broker generated itself (a stop-out has no client order).

`fill_id` identifies the execution. Its one hard job is on the LIVE
`order.filled`/`order.partial` events, where it must be STABLE so a platform
can dedupe an at-least-once stream (section 5). In `history.fills` it is the
same identity when the adapter tracks its own executions, or the broker's own
execution/trade id when the ledger is pulled from the broker; either way it is
a stable per-execution key, not a per-query value. An adapter whose broker
exposes no execution id in its history may omit it there, but never on the
live events.

`history.fills` is REQUIRED of any broker that can trade at all: it is the
ledger the platform reconciles against and is not gated by a capability. A
broker with no trading has an empty ledger, not an absent method.
Absent is the honest answer; inventing one is not.

`trail_points` is the broker-maintained trailing distance, `0` when none.
`from_order` names the order type a position came from (`"limit"`,
`"stop"`, ...) or `null` for a plain market fill.

Order acceptance is transport-level only; the truth arrives as events. A
platform must treat `accepted` as "the broker has it", nothing more.

## 5. Events

| event | data |
|---|---|
| `order.accepted` | `{"client_id", "order_id"}` |
| `order.rejected` | `{"client_id", "code", "message"}` |
| `order.placed` | `{"client_id", "order_id", "type", "price", "expires_at"}`; a resting order is live |
| `order.amended` | `{"order_id", "price"?, "qty"?, "sl"?, "tp"?, "expires_at"?}` |
| `order.promoted` | `{"order_id", "from": "stop_limit", "to": "limit", "price"}` |
| `order.cancelled` | `{"order_id", "client_id", "code"?, "message"?}`; `code` present when the broker cancelled it, absent when the platform did |
| `order.expired` | `{"order_id", "client_id", "expiry"}` |
| `order.filled` | `{"fill_id", "client_id", "order_id", "position_id", "symbol", "side", "qty", "price", "fees", "time"}` |
| `order.partial` | same as filled plus `"remaining"` |
| `position.closed` | `{"close_id", "position_id", "client_id", "qty", "price", "realized_pnl", "swap", "commission", "reason": "manual"|"sl"|"tp"|"flatten"|"stop_out"|"expired"|"close_by"|"unknown", "reason_detail"?, "time"}` |
| `position.trailed` | `{"position_id", "sl", "trail_points"}`; the broker moved a trailing stop |
| `balance.operation` | an Operation; money moved for a reason that was not a trade |
| `catalog.changed` | `{"reason", "symbols": [..], "instruments": [InstrumentSpec]?}` |
| `account.updated` | account.get payload |
| `quote` | `{"symbol", "bid", "ask", "time"}` |
| `bar` | `{"symbol", "timeframe", "bar": [t,o,h,l,c,vol]}` |
| `session.state` | `{"state": "connected"|"degraded"|"disconnected", "detail"}` |
| `margin.warning` | `{"stage": "call"|"stop_out", "margin_level"?}` |

The stream is AT LEAST ONCE (section 7), so every event that moves money or
exposure carries a STABLE dedup id, and a platform that has already applied
that id ignores the repeat:

- `order.filled` and `order.partial` carry `fill_id`, unique per execution.
- `position.closed` carries `close_id`, unique per close.

Stable means the SAME id on a re-emission of the SAME event: an adapter that
replays a fill after a reconnect MUST reuse that fill's `fill_id`, so use the
broker's own execution id where it has one. An id that changes on re-emission
is worse than none, because it defeats the dedup it exists for. The SDK
(`Connector`) dedups on these ids for every platform built on it.

`order.partial` is NEVER terminal: its `remaining` is always positive and
more executions of that order are still coming. An order ends in EXACTLY ONE
terminal event: `order.filled` (the last execution, the order is done),
`order.cancelled`, `order.expired`, or `order.rejected`. A marketable order
that fills part and cannot fill the rest (an IOC-style kill, a venue partial
then cancel) reports the executed part as `order.partial`/`order.filled` and
the unfilled remainder as `order.cancelled`. This is how a platform tells
"still working" from "done, under-filled": it never infers doneness from
`order.partial`.

`margin.warning.stage` is `call` (equity is low, a warning) or `stop_out`
(the broker is force-closing to protect itself). `margin_level` is OPTIONAL
and, when present, is `equity / margin_used` as a percentage (the MT-style
margin level), so a platform can show how close the account is. A broker that
only has the stage sends the stage.

### Catalog changes

The catalog is not static. A dealer suspends a symbol intraday, a futures CFD
expires, terms are repriced. `catalog.changed` carries `reason` of
`suspended`, `resumed`, `terms`, `listed`, `delisted` or `expired`, the
affected `symbols`, and OPTIONALLY the replacement `instruments` inline so a
platform can update without a round trip. Without the inline specs the
platform MUST re-issue `catalog.list`.

An `expired` change means positions in that symbol are gone: the broker
closes them and they arrive as `position.closed` with
`reason: "expired"`. The instrument's `successor` field, when present, names
the contract that replaces it.

A platform that reads the catalog once at connect and never listens will keep
offering a symbol the broker has stopped dealing, and will size orders off
terms that have changed. Handling this event is not optional for a broker
that declares it.

### Closing a position: what the adapter must and must not claim

`swap` and `commission` are REQUIRED on `position.closed`. They are the
financing and fees charged against the position over its life, in account
currency, negative when they cost the holder. An adapter whose broker does
not separate them sends `0` for both and reports the combined figure in
`realized_pnl`; it does not omit the fields. Omission and zero mean different
things, and a platform cannot tell "no carry was charged" from "this broker
never told me" unless the adapter says which.

`reason` MAY be `unknown`, and an adapter that cannot determine why a
position closed MUST use it rather than pick a plausible value. Several
brokers report only that a position is gone: no reason, and sometimes no
price or fee either. Guessing there is worse than admitting ignorance,
because the platform will show a user "closed by your stop loss" with the
same confidence it shows a fact. Where the broker gives a string of its own,
put it verbatim in the optional `reason_detail`; it is for humans and must
not be parsed.

A platform MUST treat `unknown` as a first-class outcome and display it as
unknown. It MUST NOT infer the reason itself from the exit price, which is
the same guess one layer up.

## 6. Errors

`error.code` is machine-readable, from this closed set (extensions must be
prefixed `x_`):

`unauthorized, market_closed, unknown_symbol, invalid_qty, invalid_stops,
insufficient_margin, not_found, duplicate_client_id, unsupported,
rate_limited, internal, requote, trade_restricted, frozen, no_price,
invalid_expiry, not_resting, insufficient_funds, oco, auth_required,
reconnect`

(`reconnect` added in the 1.11 revision: the adapter learned at connect
what changes its handshake, and must be started again. `hello` is answered
cold (3.1), so a fact an adapter can read only from an open session, such
as its account's currency or model, may turn out other than its `hello`
said. The adapter remembers what it read (its `BRUE_BROKER_STATE`, section
8.5), so the next process's `hello` says it, and answers the read that
opened its session, and every later one that needs that session, with this
code, doing nothing for them. `retryable` is false: asking that process
again cannot help. The platform ends that process and starts it again,
then `hello` and the connect's reads once more (section 7), keeping nothing
of the last process, its arming included (section 3). Once: when the
process started for it answers `reconnect` too, the adapter did not keep
what it learned, and nothing is started a third time. The new process
comes at once, so an adapter whose broker limits new connections per
address waits out the last process's turn (its time kept in the state
folder) rather than have one refused. The SDK's
`Connector` does all of this inside `connect()`, and inside a
`reconcile()` that is a process's first, so a platform built on it sees
one connect. Only a connect is answered this way, and only a connect is
started again: a `reconnect` that answers a call later in a session is
raised to the platform as it is.)

(`auth_required` added in the 1.6 revision, with the `auth` capability: the
broker needs the user to complete its interactive login before a session
can exist. Distinct from `unauthorized`, which means the credentials the
adapter HOLDS were refused; `auth_required` means there are none to send,
and the cure is `auth.begin`, not a retry.)

(`invalid_stops` added in the 1.1 revision: an SL/TP/stop price violates the
instrument's `stops_level_points` distance. A broker that declares a nonzero
`stops_level_points` MUST reject with this code rather than silently
accepting or adjusting the price.)

Codes added in the 1.2 revision, each because a real rejection had nowhere
to go:

- `requote`: the price moved between quote and fill and the broker refused
  rather than dealt. **`retryable` is true.** This is a normal outcome in a
  fast market, not a fault, and a platform that treats it as a hard failure
  will stall on it. Resending with the SAME `client_id` is correct and safe;
  idempotency (section 2) makes a double-fill impossible.
- `trade_restricted`: the market is open and the instrument is fine, but
  this account may not do this to it right now: `trade_mode` is
  `long_only`, `close_only` or `disabled`. Distinct from `market_closed`,
  which is about the clock, and from `unsupported`, which is about methods.
- `frozen`: the price is legal but the order cannot be touched because the
  market is inside the instrument's freeze distance. The request was not
  wrong; it was too late. Retry once the market moves away.
- `no_price`: the broker has no current quote for a symbol whose market is
  open, usually a feed gap on its side. Retryable.
- `invalid_expiry`: an expiry kind the broker does not honour, a `gtd` with
  no `expires_at`, or an `expires_at` in the past.
- `not_resting`: `order.cancel` or `order.amend` named an order that is not
  in `resting` state, because it already filled, expired or was cancelled.
  Distinct from `not_found`, which means the id is unknown entirely.

Codes added in the 1.3 revision:

- `insufficient_funds`: a withdrawal larger than what may leave the account.
  Distinct from `insufficient_margin`, which is about opening a position.
  Money backing an open position cannot be withdrawn, so the ceiling is free
  margin rather than balance.
- `oco`: carried on `order.cancelled` when the broker cancelled a resting
  order because a sibling in its OCO group filled. Not a fault; it is the
  group working.

### 6.1 `like`: an extension that a platform can still act on (1.9)

The closed set is what a platform can BRANCH on. An `x_` code is therefore
honest and inert: it can be displayed and nothing else. That left every
adapter author with the same bad choice on any reason the set has no word
for, and both answers lose something:

- report the true reason as an `x_` code, and the platform cannot act on it;
- report the nearest standard code, and the platform acts correctly on a
  reason that is false.

An error MAY therefore carry `like`, the closed-set code the extension should
be TREATED as:

```json
{"code": "x_halted", "like": "market_closed", "retryable": true,
 "message": "volatility halt, resolves in 20-45s"}
```

- `code` is what a platform SHOWS. `like` is what it BRANCHES on. A platform
  MUST NOT display `like` in place of `code`: the whole point is that the
  user reads the venue's real reason.
- `like` MUST be a member of the closed set above, and is meaningful ONLY
  when `code` is an `x_` extension. A `like` beside a standard code is a
  contradiction and is dropped; so is one naming a code outside the set,
  because a fallback the platform cannot read is worse than none and must
  never mask the real code.
- Both are dropped rather than rejected, so a careless field costs a broker
  nothing. `protocol.sanitize_like` does this on both sides of the pipe, and
  `ConnectorError.effective_code` is the resolved value platforms branch on.
- Nothing here widens what a platform must understand: `like` can only ever
  name a code it already handles. An older platform ignores the field by the
  unknown-fields rule (section 1) and sees exactly what it saw before, so
  this is additive and needs no version bump on the wire.
- `retryable` still belongs to the ERROR, not to `like`. A broker that
  nominates `market_closed` but knows its halt clears in seconds says
  `retryable: true` and means it.

This exists because two of the simulated venues need it: a limit price off
the tick grid, which `invalid_qty` (wrong field) and `invalid_stops` (about
SL/TP distance) both misname, and a volatility halt, which is not
`market_closed` because the market is open. Each compromise misnames the
reason, which is the tell that neither is right.

`retryable` tells the platform whether an identical retry can succeed
(`rate_limited` yes; `invalid_qty` no). `duplicate_client_id` is reserved
for pathological reuse of a client id with DIFFERENT parameters; a true
idempotent replay returns the original result instead.

THE PLATFORM OWNS THAT CHECK. An adapter SHOULD raise
`duplicate_client_id` when its broker detects a conflicting reuse, but a
platform MUST NOT depend on it. Most venues do not detect it: four of the
eight simulated venues answer a conflicting replay by returning
the ORIGINAL order and a success status, which silently hides a client bug
that could have been a wrong order. The SDK therefore enforces this inside
`Connector.request` for every adapter, so the guarantee holds even against a
broker that offers nothing. Adapters remain free to enforce it as a second
line, and the reference adapter does.

## 7. Reconciliation

The broker is the source of truth. On every connect (first or reconnect) the
platform MUST, in order: `hello`, `catalog.list`, `account.get`,
`positions.list`, and `orders.list` when `orders.pending` is true, and
REPLACE its local view with the results. Resting orders survive a
disconnection exactly as positions do, and an order that filled while the
platform was away shows up as a position it does not recognise. When one of
those reads is answered `reconnect` (section 6), the connect starts the
adapter again, once, and makes them all again from its new `hello`.

The catalog is part of that reconciliation for a reason: symbols may have
been suspended, expired or repriced while the platform was away, and the
version it cached is stale by definition. Positions the
platform does not recognize are adopted and surfaced to the user, never
auto-closed. The platform persists its `client_id` map locally so a restart
can re-associate its own orders.

Adapters SHOULD persist paper/session state across process restarts so a
reconnect exercises the same path as a reconnect to a real broker.

## 8. Safety requirements (conformance-tested)

1. `flatten_all` must succeed while other calls are failing or rate-limited.
2. `mode` must be truthful; a live adapter reporting `paper` is
   non-conformant.
3. Idempotency on `client_id` for every mutating method.
4. No mutating call may be silently dropped: every `order.place` terminates
   in `order.filled`, `order.partial`, or `order.rejected` within the
   adapter's declared timeout (default 30 s), or the adapter emits
   `session.state: degraded`.
5. Credentials are the adapter's business (env, keychain, broker terminal).
   They never appear in protocol messages. `hello` carries no secrets.

   The platform spawns an adapter with these variables, and an adapter may
   rely on them:

   - `BRUE_BROKER_STATE`: a private directory for this profile alone, where
     the adapter keeps credentials, tokens and caches. It persists across
     restarts and is not shared with another profile of the same adapter.
   - `BRUE_CRED_<FIELD>`: see below.
   - `BRUE_ACCOUNT_ID`: which account this process deals on (section 4, *Choosing an account*, revision 1.7).

   When a broker's credentials are ones the USER holds (an exchange API key
   and secret, a desk login), the platform may collect them and hand them to
   the adapter AT SPAWN as environment variables named `BRUE_CRED_<FIELD>`,
   upper-cased (`api_key` becomes `BRUE_CRED_API_KEY`). That is the whole
   mechanism: no new method, no protocol message, nothing to version. The
   platform stores them only on the user's own machine, in the profile's
   private state directory, and an adapter MUST treat them as optional
   (absent means fall back to whatever it does for a demo or self-registered
   account, or fail with `unauthorized` if the broker genuinely cannot be
   reached without them). Adapters MUST NOT log them or echo them anywhere,
   including error messages.

6. `order.place` REJECTS bad orders rather than accepting them, with the
   typed code from section 6. The suite checks three, and a broker that
   accepts any of them is non-conformant:
   - a symbol not in the catalog -> `unknown_symbol`.
   - a quantity that is not a whole multiple of `lot_step` (within a small
     tolerance), or is below `min_qty` or above `max_qty` -> `invalid_qty`.
   - an order the account cannot afford the margin for -> `insufficient_margin`.
   These follow from the catalog fields (`lot_step`, `min_qty`, `max_qty`,
   `margin_rate`), but they are stated here because they are enforced: the
   minimal "just return a fill" adapter is NOT conformant.

## 9. Conformance

An adapter is conformant when the suite reports ZERO failures:

```sh
python -m brueconnect.conformance.run --adapter "python3 path/to/my_adapter.py"
```

The bundled paper adapter is the reference adapter and must pass at all
times.

The suite spawns your adapter exactly as a platform does, so give each run
its own `BRUE_BROKER_STATE` directory. Flags:

- `--partial-hint SYMBOL:QTY`: a size known to fill partially, so the partial
  fill scenario runs instead of skipping.
- `--restartable`: your adapter persists state, so the reconnect scenario may
  kill and relaunch it.
- `--live`: you accept that the suite places REAL orders. It refuses to
  certify a non-paper account without this.
- `--auth-driver "cmd"`: for capability `auth` (section 4, *Interactive login*, revision 1.6). The command
  stands in for the human and the browser and is given `auth.begin`'s url as
  its last argument. Without it an adapter that needs a login cannot be
  certified, and the suite says so rather than failing noisily.

SKIPs are the suite honestly reporting a capability you did not declare. A
SKIP you caused by declaring a capability false to dodge a failing scenario
is not conformance; the capability rules in section 3 exist precisely so
that absence is stated rather than hidden.

## 10. Versioning

Breaking changes bump `v` and the `hello.protocol` number. Additive fields
and `x_`-prefixed extensions do not. Adapters and platforms ignore unknown
fields by law (section 1).

1.8: env var prefix settled as BRUE_*; wire protocol unchanged, still v1.

1.9: optional `like` on an error (section 6.1), naming the
closed-set code an `x_` extension should be treated as. Additive: an older
platform ignores it by the unknown-fields rule and behaves exactly as before.

1.10: optional `credential_form` in `hello` (section 3.2), the adapter's own
list of the fields a user must type before it can connect. Additive: an older
platform ignores it, an adapter that sends none is unchanged, and how a typed
value reaches the adapter (`BRUE_CRED_<FIELD>` at spawn, section 8) is
unchanged.

1.11: optional `terms` in `hello` (section 3.3), what the broker's own terms
let a platform do: keep its market data on the user's computer, and place
orders on a live account only by a person's action. Additive: an adapter that
sends none is unchanged. An older platform ignores it, so a platform that
lists a broker with such terms should carry them in its own listing as well.
Also in 1.11, the error code `reconnect` (section 6): an adapter that learned
at connect what changes its handshake is started again, once. An older
platform cannot branch on it and shows it, so an adapter's words for it
should also tell a person to connect again.
