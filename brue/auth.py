# Account identity for the brue ecosystem: brue.login(api_key=...) verifies
# the user's LSE API key (the same key they log into the data platform with)
# against the platform and caches the verified account for the session.
#
# Scope, deliberately narrow: login is REQUIRED for anything account-bound
# (LSE data, arming live trading in Brue Connect) and NOT required for the
# local math: indicators and script runs over local data never need a key
# or a network connection.
#
# stdlib-only (urllib): the brue package keeps zero hard dependencies.

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

VALIDATE_URL = os.environ.get(
    "BRUE_AUTH_URL", "https://api.londonstrategicedge.com/keys/validate")
_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "brue", "config.json")

_KEY: str | None = None
_ACCOUNT: "Account | None" = None


class BrueAuthError(Exception):
    """Raised when a key is missing, unknown, revoked, expired, or blocked."""


@dataclass
class Account:
    """The verified identity behind an API key. The raw key is never stored
    here; components that need it call current_key()."""
    key_id: str
    user_id: str
    plan_id: str
    environment: str
    permissions: list = field(default_factory=list)
    rate_limit_per_minute: int | None = None
    rate_limit_per_day: int | None = None
    features: dict | None = None

    def __repr__(self):
        return (f"Account(plan={self.plan_id!r}, environment={self.environment!r}, "
                f"key_id={self.key_id[:8]}...)")


def _resolve_key(api_key: str | None) -> str | None:
    if api_key:
        return api_key.strip()
    for env in ("BRUE_API_KEY", "LSE_API_KEY"):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            v = (json.load(f).get("api_key") or "").strip()
            if v:
                return v
    except (OSError, ValueError):
        pass
    return None


def login(api_key: str | None = None, *, timeout: float = 15.0) -> Account:
    """Verify the LSE API key and cache the account for this process.

        import brue
        brue.login(api_key="lse_...")     # or BRUE_API_KEY / LSE_API_KEY env,
        brue.account()                    # or ~/.config/brue/config.json

    Raises BrueAuthError with the service's reason on any invalid key.
    """
    global _KEY, _ACCOUNT
    key = _resolve_key(api_key)
    if not key:
        raise BrueAuthError(
            "no API key: pass brue.login(api_key=...), set BRUE_API_KEY or "
            "LSE_API_KEY, or put {\"api_key\": ...} in ~/.config/brue/config.json. "
            "Get a key at https://londonstrategicedge.com/data")
    req = urllib.request.Request(
        VALIDATE_URL,
        data=json.dumps({"key": key, "surface": "brue.login"}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "brue (+https://londonstrategicedge.com)"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read().decode()).get("reason", "")
        except ValueError:
            reason = ""
        raise BrueAuthError(
            f"key rejected ({e.code}): {reason or 'invalid key'}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise BrueAuthError(
            f"could not reach the key service ({e}); check your connection") from None
    if not payload.get("valid"):
        raise BrueAuthError(f"key rejected: {payload.get('reason', 'invalid key')}")
    _KEY = key
    _ACCOUNT = Account(
        key_id=str(payload.get("key_id", "")),
        user_id=str(payload.get("user_id", "")),
        plan_id=str(payload.get("plan_id", "")),
        environment=str(payload.get("environment", "")),
        permissions=payload.get("permissions") or [],
        rate_limit_per_minute=payload.get("rate_limit_per_minute"),
        rate_limit_per_day=payload.get("rate_limit_per_day"),
        features=payload.get("features"),
    )
    return _ACCOUNT


def account() -> Account:
    """The Account from the last successful login (raises if not logged in)."""
    if _ACCOUNT is None:
        raise BrueAuthError("not logged in: call brue.login(api_key=...) first")
    return _ACCOUNT


def current_key() -> str:
    """The verified raw key, for components that must send it (data, live)."""
    if _KEY is None:
        raise BrueAuthError("not logged in: call brue.login(api_key=...) first")
    return _KEY


def logout() -> None:
    global _KEY, _ACCOUNT
    _KEY = None
    _ACCOUNT = None
