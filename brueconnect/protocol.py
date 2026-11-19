"""Wire protocol primitives: envelopes, error codes, parsing.

One JSON object per LF-terminated line. Three shapes: req, res, event
(SPEC.md section 1). This module is shared by both sides and has no I/O.
"""
from __future__ import annotations

import json
import re
import unicodedata

PROTOCOL_VERSION = 1

# The closed set from SPEC section 6. This list is NORMATIVE at the wire: a
# code missing from it is relabelled by res_error as an `x_` extension, so a
# platform cannot branch on it (a requote it is never told about is one it
# cannot act on), and nothing fails loudly. Keep it in step with the SPEC on
# every revision.
ERROR_CODES = {
    # 1.0
    "unauthorized", "market_closed", "unknown_symbol", "invalid_qty",
    "invalid_stops", "insufficient_margin", "not_found",
    "duplicate_client_id", "unsupported", "rate_limited", "internal",
    # 1.2
    "requote", "trade_restricted", "frozen", "no_price", "invalid_expiry",
    "not_resting",
    # 1.3
    "insufficient_funds", "oco",
    # 1.6: the broker needs the HUMAN before it can open a session. Distinct
    # from `unauthorized` (credentials present but wrong/expired at the
    # venue): `auth_required` means no amount of retrying helps until the
    # user completes the broker's own login, so a platform reacts by calling
    # `auth.begin`, not by re-sending the request.
    "auth_required",
    # 1.11: the adapter learned at connect what changes its handshake (hello
    # is cold, SPEC 3.1: an account's currency or model is read only once a
    # session is open) and has remembered it, so it must be started again
    # and the next process's hello says it. A platform stops that process
    # and starts it again, once; asking the same process again never helps.
    # The SDK's Connector does it inside connect() (client.py, reconcile).
    "reconnect",
}

# Every method of the protocol: SPEC section 4's tables, name for name. A
# method is its exact name and nothing else (SPEC 1): the platform's rules
# know a mutating method by its exact name, and an adapter finds its handler
# from the name, so any other spelling of one (order_place, flatten.all)
# would reach the handler with none of those rules applied. The Connector
# sends no other name, and an adapter on the kit's base dispatches no other.
METHODS = frozenset({
    # session
    "hello", "ping", "bye",
    # interactive login (1.6) and choosing an account (1.7)
    "auth.status", "auth.begin", "auth.logout", "accounts.list",
    # catalog, account, market data
    "catalog.list", "account.get",
    "quotes.subscribe", "quotes.unsubscribe",
    "bars.history", "bars.subscribe", "bars.unsubscribe",
    # orders and positions
    "order.place", "order.modify", "orders.list", "order.amend",
    "order.cancel", "position.close_by", "history.operations",
    "positions.list", "position.close", "flatten_all", "history.fills",
})

TIMEFRAMES_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

# --- broker display identity (SPEC 3.1) ---------------------------------
# What a broker says about ITSELF, so a platform's connection picker can show
# the broker's own name and terms next to everyone else's without the platform
# shipping a table of broker names. Every field is optional and every field is
# decoration: none of it identifies the broker (the registry profile name does
# that) and none of it may change behaviour.
#
# It is third-party text arriving over a pipe and heading for a UI, so it is
# sanitised HERE, once, for every platform: length caps, no control
# characters, no scheme but https/mailto. A platform that renders it must
# still render it as text, never as markup. Sanitising in each UI instead is
# how one careless template becomes an injection in every platform.
BROKER_INFO_TEXT = {"display_name": 40, "tagline": 80, "mark": 3,
                    "country": 40}
BROKER_INFO_URL = ("website", "support")
# field -> (max items, max length per item)
BROKER_INFO_LISTS = {"regulated_by": (6, 40), "asset_classes": (12, 24)}

_WS = re.compile(r"\s+")


def _visible(value: str) -> str:
    """Every invisible character replaced by a space.

    Not just ASCII control codes. Unicode category C covers the formatting
    characters that make text render as something other than what it says:
    U+202E RIGHT-TO-LEFT OVERRIDE reverses everything after it, and
    zero-width spaces and joiners let a name look identical to another one
    while comparing differently. In a picker where the whole point is telling
    two brokers apart, a character that is present but invisible is an
    impersonation tool, so none of them survive.

    Replaced with a space rather than removed, so stripping a separator can
    never fuse two words into one.
    """
    return "".join(" " if unicodedata.category(ch)[0] == "C" else ch
                   for ch in value)


def _clean_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _WS.sub(" ", _visible(value)).strip()[:limit]


def _clean_url(value) -> str:
    if not isinstance(value, str):
        return ""
    # No spaces in a URL: an invisible character inside a host name is the
    # same trick as above, and here it would be inside something a user is
    # invited to trust.
    v = _WS.sub("", _visible(value)).strip()[:200]
    # An allowlist, not a blocklist: javascript:, data: and file: are the
    # obvious attacks, but so is any scheme a future browser learns.
    low = v.lower()
    if low.startswith("https://") and len(v) > 8:
        return v
    if low.startswith("mailto:") and "@" in v:
        return v
    return ""


def sanitize_broker_info(raw) -> dict:
    """A broker's self-description, safe to display. Never raises."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for field, limit in BROKER_INFO_TEXT.items():
        v = _clean_text(raw.get(field), limit)
        if v:
            out[field] = v
    for field in BROKER_INFO_URL:
        v = _clean_url(raw.get(field))
        if v:
            out[field] = v
    for field, (max_items, max_len) in BROKER_INFO_LISTS.items():
        v = raw.get(field)
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            continue
        items = [_clean_text(x, max_len) for x in v[:max_items]]
        items = [x for x in items if x]
        if items:
            out[field] = items
    return out


# --- credential form (SPEC 3.2) -----------------------------------------
# What the user must TYPE before this adapter can connect, stated by the
# adapter itself, so a broker nobody pre-integrated can ask for its keys in
# any platform's connection screen. It names fields; it never carries a
# value. What the user types reaches the adapter only as BRUE_CRED_<KEY> in
# its environment at spawn (SPEC 8.5), never in a message.
#
# Same standing as broker_info: optional, third-party, bound for a UI, so it
# is sanitised here once for every platform, and a malformed entry is dropped
# rather than costing the broker its connection.
CREDENTIAL_FORM_MAX = 8
CREDENTIAL_FORM_LABEL = 40
CREDENTIAL_FORM_TYPES = ("text", "password")
# The key becomes an environment variable name (BRUE_CRED_<KEY upper-cased>)
# and a JSON field in the profile's private credential file, so it is held to
# a shape that is safe in both. fullmatch, not match: `$` would let a trailing
# newline through.
_CREDENTIAL_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
# How many raw entries are looked at before giving up. A form is a handful of
# fields; a list of a million is not a form and must not cost a million turns.
_CREDENTIAL_FORM_SCAN = 64


def sanitize_credential_form(raw) -> list:
    """The fields an adapter asks the user for, safe to draw. Never raises.

    Returns at most CREDENTIAL_FORM_MAX entries of exactly
    ``{"key", "label", "type"}``. An entry is DROPPED when it is not an
    object, when its key does not match ``[a-z][a-z0-9_]{0,31}``, when its key
    repeats an earlier one, or when it names a type other than "text" or
    "password". A missing type is "password": of the two it is the one that
    cannot show a secret on screen by omission. The label is display text,
    cleaned and capped like every broker_info string; an entry with no usable
    label is shown under its key.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    seen: set = set()
    for field in raw[:_CREDENTIAL_FORM_SCAN]:
        if len(out) >= CREDENTIAL_FORM_MAX:
            break
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if (not isinstance(key, str) or not _CREDENTIAL_KEY.fullmatch(key)
                or key in seen):
            continue
        kind = field.get("type", "password")
        if not isinstance(kind, str) or kind not in CREDENTIAL_FORM_TYPES:
            continue
        seen.add(key)
        out.append({"key": key,
                    "label": _clean_text(field.get("label"),
                                         CREDENTIAL_FORM_LABEL) or key,
                    "type": kind})
    return out


# --- the broker's terms (SPEC 3.3) ---------------------------------------
# What the broker's own customer agreement lets a platform do with this
# connection: keep its market data on the user's computer, and place orders
# on a live account only by a person's action. Unlike broker_info and the
# credential form it is NOT decoration, a platform acts on it. Each flag only
# takes something away from the platform, so the flags fail CLOSED: anything
# but an absent value, JSON false or null counts as true, and a `terms` that
# is present but not an object sets both. A typo that weakened a licence term
# would put the user's account at risk; one that strengthens it costs a
# feature, and the platform says which.
TERMS_FLAGS = ("data_local_only", "orders_by_hand_only")
TERMS_SUMMARY = 200


def sanitize_terms(raw) -> dict:
    """The broker's terms, safe to act on and to display. Never raises.

    Always returns both flags as booleans (both False when ``raw`` is None,
    i.e. the adapter sent no terms), plus ``summary`` (plain text, at most
    200 characters) and ``url`` (https only) when usable.
    """
    if raw is None:
        return {flag: False for flag in TERMS_FLAGS}
    if not isinstance(raw, dict):
        return {flag: True for flag in TERMS_FLAGS}
    out: dict = {flag: (flag in raw and raw[flag] is not None
                        and raw[flag] is not False)
                 for flag in TERMS_FLAGS}
    summary = _clean_text(raw.get("summary"), TERMS_SUMMARY)
    if summary:
        out["summary"] = summary
    url = _clean_url(raw.get("url"))
    if url.lower().startswith("https://"):
        out["url"] = url
    return out


class ProtocolError(Exception):
    """A line that violates the envelope rules."""


def req(rid: str, method: str, params: dict | None = None) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "id": rid, "type": "req",
                       "method": method, "params": params or {}})


def res(rid: str, result: dict) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "id": rid, "type": "res",
                       "result": result})


def sanitize_like(code: str, like) -> str | None:
    """The closed-set code an EXTENSION should be treated as (SPEC 6).

    An `x_` code is honest but inert: the platform can print it and nothing
    more, because everything it decides (retry, try another symbol, ask the
    user to fix a field) branches on the closed set. Without `like` an
    adapter author chooses between an accurate code nobody can act on and a
    near-miss standard code that is actionable but false. `like` ends the
    choice: say the true thing in `code` and the actionable thing here.

    Dropped rather than rejected, on the same principle as broker_info:
    - a fallback outside the closed set is one the platform cannot read
      either, so it is worse than none and must not mask the real code.
    - a `like` beside a code that is ALREADY standard is a contradiction;
      the standard code is what a platform branches on, so the hint is noise.
    """
    if not isinstance(like, str) or not like:
        return None
    if code in ERROR_CODES:
        return None
    if like not in ERROR_CODES:
        return None
    return like


def res_error(rid: str, code: str, message: str, retryable: bool = False,
              like=None) -> str:
    if code not in ERROR_CODES and not code.startswith("x_"):
        # An unrecognised code is an extension, not an internal fault.
        # Collapsing it to `internal` destroys the only information the
        # platform had and makes a broker's own vocabulary look like a crash;
        # the SPEC already reserves the x_ prefix for exactly this.
        message = f"[{code}] {message}"
        code = "x_" + re.sub(r"[^a-z0-9_]+", "_", code.lower()).strip("_")
    err = {"code": code, "message": message, "retryable": retryable}
    like = sanitize_like(code, like)
    if like:
        err["like"] = like
    return json.dumps({"v": PROTOCOL_VERSION, "id": rid, "type": "res",
                       "error": err})


def event(seq: int, name: str, data: dict) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "type": "event", "seq": seq,
                       "event": name, "data": data})


def parse_line(line: str) -> dict:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"not JSON: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("message is not an object")
    t = msg.get("type")
    if t == "req":
        if not isinstance(msg.get("id"), str) or not isinstance(msg.get("method"), str):
            raise ProtocolError("req needs string id and method")
    elif t == "res":
        if not isinstance(msg.get("id"), str):
            raise ProtocolError("res needs string id")
        if ("result" in msg) == ("error" in msg):
            raise ProtocolError("res carries exactly one of result|error")
    elif t == "event":
        if not isinstance(msg.get("event"), str) or not isinstance(msg.get("seq"), int):
            raise ProtocolError("event needs string event and int seq")
    else:
        raise ProtocolError(f"unknown type {t!r}")
    return msg


def order_signature(method: str, params: dict) -> str:
    """Stable fingerprint of a mutating request, ignoring client_id itself.

    Used to tell an honest retry (identical parameters, which must be safe to
    replay) from a client_id collision (different parameters, which SPEC 6
    says is duplicate_client_id). Sorted so key order never changes the
    answer, and floats normalised so 0.10 and 0.1 are the same order.
    """
    def norm(v):
        if isinstance(v, float):
            return format(v, ".10g")
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items())}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v

    body = {k: norm(v) for k, v in sorted((params or {}).items())
            if k != "client_id"}
    return method + "|" + json.dumps(body, sort_keys=True, default=str)
