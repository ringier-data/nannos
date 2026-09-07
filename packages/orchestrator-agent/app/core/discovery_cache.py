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

    user_sub, sorted(groups), entitlement_version, sub_agent_config_hash, policy_version

``entitlement_version`` is the load-bearing part. It is an opaque stamp console-backend
derives from every row that decides the user's entitlements — role, settings (tool
whitelist, bypass rules), group memberships, group default agents, sub-agent activations
and their approved versions, plus a marker the console bumps for gateway-held state such
as group → MCP-server access. The executor fetches it once per turn (one cheap in-cluster
call, ``RegistryService.get_entitlement_version``) *before* the cache lookup, so any
entitlement change makes the stale entry unreachable on the user's next turn — on every
replica, with no push-based invalidation and nothing for a mutation site to remember.
See console-backend ``services/entitlement_version.py`` for what the stamp covers.

If the stamp cannot be fetched (console-backend blip), ``resolve_entitlement_version``
falls back to the last one seen for that user, so the turn degrades to a TTL-bounded
entry rather than a cold miss or an error.

``groups`` (free, from the JWT) are kept in the key as belt-and-braces: a membership
change moves the stamp too, but the JWT view is the one that gates authorization.
``sub_agent_config_hash`` is **playground-only**: the console's "test this exact config
version" mode sends it, and it is None on a normal turn — it is not a digest of the
user's sub-agent set. ``policy_version`` (``AgentSettings.ENTITLEMENT_POLICY_VERSION``)
is the cross-cutting lever for a fleet-wide flush without per-user targeting.

The per-user *tool whitelist* (``tool_names``) is deliberately NOT part of the key:
discovery runs unfiltered (``white_list=None``) and the whitelist is applied later in
``build_runtime_context``, so the cached ``(tools, sub_agents, token_provider)`` value
does not depend on it (a whitelist change still moves the stamp, which is what refreshes
the cached ``User`` record that carries it).

Token-expiry safety
-------------------
Discovered tools carry **no** bearer token: each call mints one through the user's
``UserTokenProvider`` (``agent_common.core.token_provider``), which is cached alongside the
tools and given the user's current token at the start of every turn. The registry data,
however, was fetched with the user token and reflects entitlements tied to it, so a cache
entry is still bounded by ``min(ttl, user_token.exp - margin)``: it never outlives the user
token it was built for. The TTL itself is therefore purely a *catalogue-freshness* knob (how
long a change made on the gateway itself, invisible to the console, may go unnoticed), not a
credential bound and not an entitlement bound.
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
    entitlement_version: str | None = None,
) -> str:
    """Build a cache key from the inputs the cached value actually depends on.

    Shared by the discovery, user and embedded-runnable caches (they live in separate
    stores, so an identical key string never collides across them). ``tool_names`` is
    intentionally excluded — see the module docstring.
    """
    payload = json.dumps(
        {
            "u": user_sub,
            "g": sorted(groups or []),
            "e": entitlement_version or "",
            "c": sub_agent_config_hash or "",
            "v": policy_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# Last entitlement version successfully fetched per user_sub. Only consulted when the
# per-turn fetch fails, so a console-backend blip degrades to "reuse the current entry
# until the TTL" instead of a cold re-discovery (or an error) on every turn.
_last_entitlement_version: dict[str, str] = {}


def resolve_entitlement_version(user_sub: str, fetched: str | None) -> str | None:
    """Return the stamp to key on this turn: ``fetched`` if present, else the last one seen.

    Remembers a successful fetch for the fallback. Returns None only when the fetch failed
    and the user has never had a stamp in this process — the key then carries an empty
    stamp and the entry is simply TTL-bounded, exactly the pre-stamp behaviour.
    """
    if fetched:
        _last_entitlement_version[user_sub] = fetched
        return fetched
    last = _last_entitlement_version.get(user_sub)
    if last is not None:
        logger.warning(
            "[ENTITLEMENT-VERSION] fetch failed for user_sub=%s; reusing last known stamp",
            user_sub,
        )
    return last


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

    def __init__(
        self,
        ttl_seconds: float,
        name: str = "cache",
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
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

    def put(self, key: str, value: Any, access_token: str | None) -> None:
        expires_at = time.time() + self._ttl
        exp = token_exp(access_token)
        if exp is not None:
            expires_at = min(expires_at, exp - _TOKEN_EXP_MARGIN_S)
        if expires_at <= time.time():
            return  # token already (nearly) expired — don't cache a stale entry
        self._store[key] = _Entry(value=value, expires_at=expires_at)
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

    def clear(self) -> None:
        n = len(self._store)
        self._store.clear()
        if n:
            logger.info("[%s] cleared %d entries", self._name, n)


_discovery_cache: TtlTokenCache | None = None
_user_cache: TtlTokenCache | None = None
_embedded_runnable_cache: TtlTokenCache | None = None


def get_discovery_cache(ttl_seconds: float | None = None) -> TtlTokenCache:
    """Process-wide cache of discovered ``(tools, sub_agents, token_provider)`` tuples."""
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


def get_embedded_runnable_cache(ttl_seconds: float | None = None) -> TtlTokenCache:
    """Process-wide cache of built embedded (execute-only) sub-agent runnables.

    The embedded path builds the scoped sub-agent per turn — ``_ensure_agent`` runs an
    OAuth token exchange, MCP gateway ``list_tools`` handshakes, console-tool discovery,
    and LangGraph compilation — which is seconds of time-to-first-token on every message
    while the non-embedded path reuses ``GraphFactory._graphs``. Entries are keyed like
    the discovery cache (entitlements + sub-agent config hash + target id): the runnable's
    tools embed exchanged bearer tokens, so entries are token-bounded exactly like
    discovery entries.
    """
    global _embedded_runnable_cache
    if _embedded_runnable_cache is None:
        _embedded_runnable_cache = TtlTokenCache(
            ttl_seconds if ttl_seconds is not None else 300.0,
            name="EMBEDDED-RUNNABLE-CACHE",
        )
    return _embedded_runnable_cache


def invalidate_all() -> None:
    """Drop all cached discovery + user records and forgotten stamps (fleet-wide flush).

    Entitlement changes never need this — they move the per-user stamp in the key. It is a
    maintenance lever for the one thing the stamp cannot see: a catalogue change made on
    the gateway itself (which the TTL otherwise bounds).
    """
    if _discovery_cache is not None:
        _discovery_cache.clear()
    if _user_cache is not None:
        _user_cache.clear()
    if _embedded_runnable_cache is not None:
        _embedded_runnable_cache.clear()
    _last_entitlement_version.clear()
