"""Per-user caches for the request hot path.

Two things were recomputed on *every* turn even though they're identical turn-to-turn
for the same user/entitlements (the compiled graph is already cached):

  * capability discovery — MCP tools + sub-agents (~2s: gatana token exchange +
    ``fetch_available_servers`` + hundreds of per-server ``list_tools`` handshakes);
  * the registry user fetch (~1s: 4 concurrent console-backend calls).

Both are memoized here.

Keying
------
``cache_key`` folds in the inputs that determine *which* tools/sub-agents a user is
entitled to *and* that the cached value actually depends on::

    user_sub, sorted(groups), sub_agent_config_hash, policy_version

Note that the per-user *tool whitelist* (``tool_names``) is deliberately NOT part of the
key: discovery runs unfiltered (``white_list=None``) and the whitelist is applied later in
``build_runtime_context``, so the cached ``(tools, sub_agents)`` value does not depend on
it. Keying on it would only fragment the cache. A whitelist change (or any other per-user
entitlement field carried on the cached ``User`` — role, bypass rules, catalog access) is
propagated by console-backend calling ``invalidate_users`` for the affected user(s); see
``main.invalidate_discovery_cache``.

Group-membership changes invalidate **automatically** (the next request carries a
different group set → different key → miss). ``policy_version`` is a cross-cutting
invalidation lever (see ``AgentSettings.ENTITLEMENT_POLICY_VERSION`` and ``invalidate_all``)
for a fleet-wide flush without per-user targeting — bump it (or call ``invalidate_all``)
and every entry is recomputed.

Token-expiry safety
-------------------
Discovered tools embed an *exchanged* bearer token (gatana/console) in their MCP connection,
and the registry data was fetched with the user token, so a cache entry must not outlive
those tokens. Each entry is bounded by ``min(ttl, user_token.exp - margin)``.

The user token's ``exp`` is used as the bound because the exchanged tokens are not available
at ``put`` time. This is safe **only while** the exchanged gatana/console tokens are minted
with a lifetime at least as long as the user token's remaining validity — which holds when
those OIDC clients use (at least) the realm's default access-token lifespan. To keep that
assumption robust the default TTL is kept well below a typical realm access-token lifespan,
so an entry can never outlive a freshly-minted exchanged token even if a client is
configured with a shorter lifespan. If you shorten the gatana/console token lifespan below
``AGENT_DISCOVERY_CACHE_TTL``, lower the TTL to match.
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

# Hard cap on entries per cache so a long-lived process with high user/entitlement churn
# cannot grow unbounded (entries for users who never return are otherwise only reclaimed
# lazily when their own key is read again). When exceeded we drop expired entries first,
# then evict the entry closest to expiry.
_DEFAULT_MAX_ENTRIES = 5000


def cache_key(
    user_sub: str,
    groups: list[str] | None,
    sub_agent_config_hash: str | None,
    policy_version: str = "0",
) -> str:
    """Build a cache key from the inputs the cached value actually depends on.

    Shared by both the discovery cache and the user cache (they live in separate stores,
    so an identical key string never collides across them). ``tool_names`` is intentionally
    excluded — see the module docstring.
    """
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
    owner: str | None = None  # user_sub this entry belongs to, for scoped invalidation


class TtlTokenCache:
    """A TTL cache whose entries are additionally bounded by a bearer token's expiry."""

    def __init__(self, ttl_seconds: float, name: str = "cache", max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._ttl = ttl_seconds
        self._name = name
        self._max_entries = max_entries
        self._store: dict[str, _Entry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.time():
            self._store.pop(key, None)
            return None
        return entry.value

    def put(self, key: str, value: Any, access_token: str | None, owner: str | None = None) -> None:
        expires_at = time.time() + self._ttl
        exp = token_exp(access_token)
        if exp is not None:
            expires_at = min(expires_at, exp - _TOKEN_EXP_MARGIN_S)
        if expires_at <= time.time():
            return  # token already (nearly) expired — don't cache a stale entry
        self._store[key] = _Entry(value=value, expires_at=expires_at, owner=owner)
        if len(self._store) > self._max_entries:
            self._evict()

    def _evict(self) -> None:
        """Bound the store size: drop expired entries first, then the entry closest to expiry."""
        now = time.time()
        expired = [k for k, e in self._store.items() if e.expires_at <= now]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max_entries:
            oldest = min(self._store, key=lambda k: self._store[k].expires_at)
            self._store.pop(oldest, None)

    def invalidate_owner(self, owner: str) -> int:
        """Drop every entry belonging to ``owner`` (a user_sub). Returns the count removed."""
        keys = [k for k, e in self._store.items() if e.owner == owner]
        for k in keys:
            self._store.pop(k, None)
        if keys:
            logger.info("[%s] invalidated %d entries for owner=%s", self._name, len(keys), owner)
        return len(keys)

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


def invalidate_users(user_subs: list[str]) -> int:
    """Drop cached discovery + user records for the given users only. Returns total removed.

    This is the targeted path: console-backend computes the set of users whose entitlements
    changed (the members of an affected group, or a single user whose role/whitelist/bypass
    rules changed) and asks the orchestrator to flush just those, so an entitlement change
    for one group cannot trigger an expensive re-discovery storm for every active user.
    """
    removed = 0
    for sub in user_subs:
        if _discovery_cache is not None:
            removed += _discovery_cache.invalidate_owner(sub)
        if _user_cache is not None:
            removed += _user_cache.invalidate_owner(sub)
    return removed


def invalidate_all() -> None:
    """Drop all cached discovery + user records (fleet-wide flush, no per-user targeting).

    Used for the unscoped admin/maintenance flush. Prefer ``invalidate_users`` for routine
    entitlement changes so a single group edit does not evict every user's cache.
    """
    if _discovery_cache is not None:
        _discovery_cache.clear()
    if _user_cache is not None:
        _user_cache.clear()
