"""Per-user caches for the request hot path.

Two things were recomputed on *every* turn even though they're identical turn-to-turn
for the same user/entitlements (the compiled graph is already cached):

  * capability discovery — MCP tools + sub-agents (~2s: gatana token exchange +
    ``fetch_available_servers`` + hundreds of per-server ``list_tools`` handshakes);
  * the registry user fetch (~1s: 4 concurrent console-backend calls).

Both are memoized here.

Keying
------
``entitlement_key`` folds in the inputs that determine *which* tools/sub-agents a user
is entitled to::

    user_sub, sorted(groups), sub_agent_config_hash, sorted(tool_names), policy_version

Group-membership changes invalidate **automatically** (the next request carries a
different group set → different key → miss). ``policy_version`` is a cross-cutting
invalidation lever (see ``AgentSettings.ENTITLEMENT_POLICY_VERSION`` and
``invalidate_all``) for the case a group→server/tool access *policy* changes without the
user's own groups/config changing — bump it (or call ``invalidate_all``) and every entry
is recomputed.

Token-expiry safety
-------------------
Discovered tools embed the user's exchanged bearer token in their MCP connection (and the
registry data was fetched with the user token), so a cache entry must not outlive that
token. Each entry is bounded by ``min(ttl, access_token.exp - margin)``. The exchanged
gatana/console tokens are minted at discovery time with the realm lifetime, so they expire
no earlier than the user token — bounding by the user token's ``exp`` is therefore safe.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Never serve an entry within this many seconds of its bounding token's expiry, so an
# in-flight tool call can still complete with a valid token.
_TOKEN_EXP_MARGIN_S = 30.0


def entitlement_key(
    user_sub: str,
    groups: list[str] | None,
    sub_agent_config_hash: str | None,
    tool_names: list[str] | None,
    policy_version: str = "0",
) -> str:
    """Build a cache key from the inputs that determine a user's entitlements."""
    payload = json.dumps(
        {
            "u": user_sub,
            "g": sorted(groups or []),
            "c": sub_agent_config_hash or "",
            "t": sorted(tool_names or []),
            "v": policy_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def user_key(
    user_sub: str,
    groups: list[str] | None,
    sub_agent_config_hash: str | None,
    policy_version: str = "0",
) -> str:
    """Cache key for the registry user record (no tool_names — those come from the fetch)."""
    payload = json.dumps(
        {"u": user_sub, "g": sorted(groups or []), "c": sub_agent_config_hash or "", "v": policy_version},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def token_exp(access_token: str | None) -> float | None:
    """Read ``exp`` (unix seconds) from a JWT without verifying it. None if unparseable."""
    if not access_token:
        return None
    try:
        segment = access_token.split(".")[1]
        segment += "=" * (-len(segment) % 4)  # restore base64 padding
        claims = json.loads(base64.urlsafe_b64decode(segment))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TtlTokenCache:
    """A TTL cache whose entries are additionally bounded by a bearer token's expiry."""

    def __init__(self, ttl_seconds: float, name: str = "cache") -> None:
        self._ttl = ttl_seconds
        self._name = name
        self._store: dict[str, _Entry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.time():
            self._store.pop(key, None)
            return None
        return entry.value

    def put(self, key: str, value: Any, access_token: str | None) -> None:
        expires_at = time.time() + self._ttl
        exp = token_exp(access_token)
        if exp is not None:
            expires_at = min(expires_at, exp - _TOKEN_EXP_MARGIN_S)
        if expires_at <= time.time():
            return  # token already (nearly) expired — don't cache a stale entry
        self._store[key] = _Entry(value=value, expires_at=expires_at)

    def clear(self) -> None:
        n = len(self._store)
        self._store.clear()
        if n:
            logger.info("[%s] cleared %d entries", self._name, n)


_discovery_cache: TtlTokenCache | None = None
_user_cache: TtlTokenCache | None = None


def get_discovery_cache(ttl_seconds: float | None = None) -> TtlTokenCache:
    """Process-wide cache of discovered (tools, sub_agents) tuples."""
    global _discovery_cache
    if _discovery_cache is None:
        _discovery_cache = TtlTokenCache(ttl_seconds if ttl_seconds is not None else 300.0, name="DISCOVERY-CACHE")
    return _discovery_cache


def get_user_cache(ttl_seconds: float | None = None) -> TtlTokenCache:
    """Process-wide cache of registry User records."""
    global _user_cache
    if _user_cache is None:
        _user_cache = TtlTokenCache(ttl_seconds if ttl_seconds is not None else 300.0, name="USER-CACHE")
    return _user_cache


def invalidate_all() -> None:
    """Drop all cached discovery + user records.

    Call when a cross-cutting entitlement policy changes (e.g. a group→server/tool access
    mapping). Exposed so console-backend (or an admin action) can trigger invalidation
    without waiting for the TTL or bumping ENTITLEMENT_POLICY_VERSION via redeploy.
    """
    if _discovery_cache is not None:
        _discovery_cache.clear()
    if _user_cache is not None:
        _user_cache.clear()
