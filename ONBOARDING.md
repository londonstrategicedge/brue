# Broker onboarding questionnaire

Send this to a broker before anyone writes an adapter.

Every question here comes from a real integration where the answer was
missing and someone had to discover it by placing orders and watching what
happened. Integrating eight simulated venues, each on a different wire
protocol, produced this list: these are the facts an integrator cannot
infer, cannot safely guess, and will get wrong quietly if nobody asks.

Answers belong in your API documentation. Where the documentation already
says it, point us at the section rather than repeating it.

## Money and margin

1. Does a RESTING order (limit, stop, stop limit) reserve margin when it is
   placed, or is margin only checked when it triggers? This is the single
   most-missed fact. Four of eight venues leave it unstated and an
   integrator has to measure it by placing an order and watching the
   balance. If margin is only checked at trigger, an order accepted
   comfortably can die at the moment it should have filled, and the platform
   must be told that is possible.
2. What is the maintenance margin rate, as distinct from the initial rate?
   If you publish only a liquidation ratio, state which way round it is
   (equity over margin, or margin over equity). Read backwards, it says
   every position liquidates instantly.
3. What currency is the account denominated in, and is every money figure
   you report already converted into it?

## Closing a position

4. When a position closes, do you report WHY: manually, stop loss, take
   profit, flatten, stop out, expiry? If you do not, say so plainly. We
   would far rather receive "unknown" than have an integrator infer the
   reason from the exit price, which is a guess a user will read as fact.
5. Do you report the swap or financing and the commission charged over the
   position's life, separately from the profit? If you report only a
   combined figure, say so.
6. Does a close produce an entry on your order or execution feed, or only a
   position snapshot? Two venues signal a close ONLY by a position
   snapshot with no price, no fee and no order id, which forces the exit
   price to be reconstructed from a separate trade ledger.

## Orders

7. Is there a call that lists ALL open orders? If there is not, say so
   explicitly. One venue has none, which means an adapter can only report
   orders it placed itself and remembered, and any order placed elsewhere is
   invisible after a restart.
8. If a client sends the same client order id twice with DIFFERENT details,
   what do you do? Most brokers return the original order and a success
   status. That is fine, but we need to know, because it means your system
   is not protecting the client from a duplicate-id bug.
9. In what ORDER do you validate an order: symbol, session, quantity, price?
   Three venues check the session before the quantity, so the same bad
   order returns a different error code depending on the time of day.
10. Which order types do you really accept in the environment we will
    connect to? One venue documents a type that its deployment rejects
    outright.

## Sessions and calendar

11. What are the trading hours, in UTC, per instrument or per group? If an
    instrument trades continuously, say so explicitly rather than leaving
    the calendar blank; blank means "unknown", not "always open".
12. Does your API expose the broker's own clock? If your demo environment
    runs on a simulated or offset clock, we must know, because we evaluate
    your calendar against your time and not ours.

## Data scoping

13. Is every history endpoint scoped to the authenticated account? A venue
    must never return one account's ledger for another; ask how the ledger
    is scoped. If any endpoint is account-agnostic, tell us before we ship
    it to a client's screen.

## Feed semantics

14. Is your event stream at-least-once or exactly-once? One venue emits a
    single order placement twice, which double-counts a fill in a platform
    that trusts it.
15. In your event payloads, does any field named `type` or similar carry the
    EVENT kind, or the ORDER kind? One venue's execution feed puts the
    order's type in the slot its documentation says holds the event type, so
    following the documentation exactly means cancellations and expiries
    are never seen at all.

## Logging in

16. How does a HUMAN log in? If you support OAuth2 authorization-code with
    PKCE, say so and we are close to done: we register as a public client,
    you allow a loopback redirect (`http://127.0.0.1:<any port>/callback`,
    RFC 8252, because a desktop app cannot keep a secret and cannot own a
    public URL), and the user types their password on YOUR page and never
    into our software. If you only issue API keys, say that instead; the
    user pastes them once and they stay on the user's machine.
17. How long does a session last, and what ends it? We expect the short
    access token to be refreshed silently and the REFRESH token to be the
    real session, lasting months. Name its life.
18. Do your refresh tokens survive YOUR restarts and deploys? If they live
    only in memory, every one of your clients is signed out every time you
    release, and neither they nor we can explain why. This is not
    hypothetical: a reference venue shipped that bug.
19. What, exactly, invalidates a session: logout, password change, a new
    login elsewhere, a device limit, an idle timeout? We destroy a stored
    token the moment you refuse it, so we need to know which of these are
    real so we can tell the user the truth.
20. Can we revoke on demand, and at what endpoint? "Log out" must actually
    log out at your end, not just forget locally.

## Accounts

21. Can one login reach MORE THAN ONE account (a spread bet beside a CFD, a
    currency each, a demo next to the live ones)? If so, how do we list
    them, and how do we name one on a request (a header, a path segment, a
    field)?
22. For each account, what can you tell us: an id, a label the client would
    recognise, its currency, its product type, and crucially whether it is
    LIVE or DEMO? Only the id is required. We will show exactly what you
    send and will not invent a label, because an account labelled by a guess
    is worse than one labelled by its number.
23. If a request names an account that login does not own, do you refuse it,
    or do you quietly act on the default account? Refusing is the only safe
    answer, and it needs its own error code so we can tell it apart from a
    bad token.
24. Is there a primary or default account when we name none?

## Currency conversion

25. Is EVERY money figure you report already converted into the account's
    currency: balance, equity, margin used, realised and unrealised P/L,
    commission, swap, and any funding or adjustment? We hold no exchange
    rates and will never convert. Anything you send in another currency will
    be displayed as though it were the account's.
26. Can you also give us the value of one tick, per contract, IN THE
    ACCOUNT'S CURRENCY (`tick_value_account`)? You already compute this to
    charge margin. With it, a client sizes a position and estimates margin
    in their own money. Without it we show the instrument's currency and
    say so; we will not cross your own tradable FX pair to derive it,
    because your conversion rate and your quoted pair are allowed to differ
    and a near-miss wearing your name is worse than an honest foreign
    number.
27. Which rate do you convert at, and when is it struck: mid or a side of
    the spread, continuously for open P/L, at execution for realised P/L and
    commission? We will not second-guess it; we ask so we can explain your
    numbers to your client rather than shrug.
