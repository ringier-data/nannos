"""Model-lifecycle enrichment for the sub-agent read path.

console-backend is the single source of truth for what happens when a sub-agent's model
is retired: the gateway owns which models exist, and the model_defaults store
owns the per-role default. This module combines them to annotate config versions with
``model_retired`` / ``effective_model`` so every reader — the console UI and the
orchestrator — consumes the same resolved decision instead of re-deriving it.
"""

import logging
import time
from typing import TYPE_CHECKING

from .model_gateway_service import ModelGatewayError

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..models.sub_agent import SubAgentConfigVersion
    from ..models.user import UserSettings

logger = logging.getLogger(__name__)

# Mirror /api/v1/models' cache window so we don't hit the gateway on every config read.
_CACHE_TTL_SECONDS = 30.0
# (timestamp, registered_aliases | None, defaults {role: alias}, alias_tiers {alias: [roles]})
_cache: tuple[float, set[str] | None, dict[str, str], dict[str, list[str]]] | None = None

# A sub-agent's model_tier → the model_defaults role it resolves to ("standard" is the plain
# chat default). Keep in sync with ModelTier (models/sub_agent.py) and VALID_ROLES.
_TIER_ROLE = {"low": "chat:low", "standard": "chat", "premium": "chat:premium"}

# Capability ordering of the optional chat tiers. When a retired model served more than one,
# we degrade toward the HIGHEST it held so a premium pick is never silently downgraded.
# ("chat"/standard isn't here — it resolves to the standard default directly.)
_TIER_RANK = {"chat:premium": 2, "chat:low": 1}


async def get_model_registry(
    request: "Request", db: "AsyncSession"
) -> tuple[set[str] | None, dict[str, str], dict[str, list[str]]]:
    """Return ``(registered_aliases, defaults, alias_tiers)`` for retirement/tier resolution,
    cached ~30s.

    ``registered_aliases`` is ``None`` when the gateway list can't be read — callers MUST
    treat that as "unknown" and not flag anything retired (fail open), exactly like
    agent-common's is_valid_model. ``defaults`` is the {role: alias} model_defaults map (incl.
    the chat tiers), DB-backed so it's available even when the gateway is down. ``alias_tiers``
    is {alias: chat-tier role} — the tier each alias last served, for within-tier degradation.
    """
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1], _cache[2], _cache[3]

    defaults = await request.app.state.model_defaults_service.get_all(db)
    alias_tiers = await request.app.state.model_defaults_service.get_alias_tiers(db)

    registered: set[str] | None
    try:
        raw = await request.app.state.model_gateway_service.list_models()
        registered = {m.get("model_name") for m in raw if m.get("model_name")}
    except ModelGatewayError as e:
        logger.warning("Gateway list failed (%s); not flagging any model as retired", e)
        registered = None

    _cache = (now, registered, defaults, alias_tiers)
    return registered, defaults, alias_tiers


def _tier_alias(model_tier: str | None, defaults: dict[str, str]) -> str | None:
    """Resolve a model_tier to its configured alias: the chat:<tier> slot, falling back to the
    standard chat default when the tier slot is unset (so a new tier degrades gracefully)."""
    if not model_tier:
        return None
    role = _TIER_ROLE.get(model_tier, "chat")
    return defaults.get(role) or defaults.get("chat")


def _retirement_fallback(
    alias: str | None,
    registered: set[str] | None,
    defaults: dict[str, str],
    alias_tiers: dict[str, list[str]],
) -> str | None:
    """The alias a retired CONCRETE model degrades to. If the model served chat tiers
    (alias_tiers), degrade to the CURRENT default of the highest tier it held — so a retired
    premium model lands on the premium successor, not the standard default. A model may have
    served several tiers; we try them high→low and take the first live successor. Falls back to
    the standard chat default when the alias has no tier history (or no successor is live).
    """
    chat_default = defaults.get("chat")
    roles = alias_tiers.get(alias, []) if alias else []
    for role in sorted((r for r in roles if r in _TIER_RANK), key=lambda r: _TIER_RANK[r], reverse=True):
        successor = defaults.get(role)
        # Only follow a successor that is actually live (avoid degrading onto another dead alias).
        if successor and (registered is None or successor in registered):
            return successor
    return chat_default


def resolve_alias_status(
    alias: str | None,
    registered: set[str] | None,
    fallback: str | None,
) -> tuple[bool, str | None]:
    """Resolve one model alias to ``(retired, effective_alias)``.

    No alias → ``(False, None)`` (caller inherits its own default). Still registered, or
    registry unknown (gateway unreadable → fail open) → ``(False, alias)``. Retired → degrade
    to ``fallback``, or pass the alias through unchanged when no fallback is configured (the
    gateway then rejects it — surfacing the misconfiguration rather than masking it).
    """
    if not alias:
        return False, None
    if registered is None or alias in registered:
        return False, alias
    return True, fallback or alias


def annotate_config_version(
    cv: "SubAgentConfigVersion | None",
    registered: set[str] | None,
    defaults: dict[str, str],
    alias_tiers: dict[str, list[str]],
) -> None:
    """Set ``model_retired`` / ``effective_model`` on a config version in place.

    A tier-bound version (model_tier set, model None) resolves to the tier's current default
    alias — the indirection that lets a retired model be replaced by repointing one slot. A
    concrete-model version degrades, on retirement, to its model's last tier successor (see
    ``_retirement_fallback``) rather than always the standard default.
    """
    if cv is None:
        return
    if getattr(cv, "model_tier", None):
        requested = _tier_alias(cv.model_tier, defaults)
        fallback = defaults.get("chat")
    else:
        requested = cv.model
        fallback = _retirement_fallback(cv.model, registered, defaults, alias_tiers)
    cv.model_retired, cv.effective_model = resolve_alias_status(requested, registered, fallback)


async def annotate_models(
    request: "Request",
    db: "AsyncSession",
    config_versions: "list[SubAgentConfigVersion | None]",
) -> None:
    """Annotate a batch of config versions, fetching the registry once."""
    if not config_versions:
        return
    registered, defaults, alias_tiers = await get_model_registry(request, db)
    for cv in config_versions:
        annotate_config_version(cv, registered, defaults, alias_tiers)


async def annotate_user_settings(request: "Request", db: "AsyncSession", settings: "UserSettings") -> None:
    """Resolve the user's preferred model lifecycle so the Settings UI can show
    "<preferred> (retired) -> <effective>" instead of a blank picker. A retired preferred
    model degrades within its tier, same as a sub-agent's concrete model."""
    registered, defaults, alias_tiers = await get_model_registry(request, db)
    fallback = _retirement_fallback(settings.preferred_model, registered, defaults, alias_tiers)
    settings.preferred_model_retired, settings.effective_preferred_model = resolve_alias_status(
        settings.preferred_model, registered, fallback
    )
