"""System feature readiness — what's enabled, what's degraded, and how to enable it.

Powers the admin System Status page and the catalog embedding gate. The console has many
optional, configuration-gated features (catalog, external skill search, phone verification,
…) plus model-gateway-dependent ones (chat, embeddings). When a default model is set but no
longer registered on the gateway, or an optional integration's credentials are missing, the
app degrades silently — this module makes that state legible.

A feature is one of:
  - ready:    configured and its dependencies are reachable/valid
  - limited:  working, but with a capability caveat the admin should know about (e.g. the
              catalog's embedding model embeds text only, so document images aren't searchable)
  - degraded: configured but a dependency is currently unmet (e.g. the default model isn't
              registered on the gateway, or the gateway is unreachable)
  - disabled: not configured at all (an optional feature left off)

The model-gateway lookup reuses model_status.get_model_registry (cached ~30s, fails open
when the gateway is unreadable — exactly like the retirement checks).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..config import config
from .model_gateway_service import ModelGatewayError
from .model_status import get_model_registry

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

Status = Literal["ready", "limited", "degraded", "disabled"]


@dataclass
class FeatureStatus:
    """One row of the System Status page."""

    key: str
    name: str
    status: Status
    detail: str
    remediation: str | None = None
    caveat: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            "caveat": self.caveat,
        }


# --- Embedding readiness (shared with the catalog gate) ---


def resolve_embedding_readiness(
    defaults: dict[str, str], registered: set[str] | None
) -> tuple[Status, str | None, str | None]:
    """Pure embedding-readiness decision → ``(status, configured_alias, reason)``.

    The single source of truth shared by the System Status page and the catalog sync worker
    (via ``CatalogSyncPipeline.resolve_embedding_readiness``), so the UI and the worker can
    never disagree on whether indexing can actually run.

    Embeddings are "configured" only when a default alias is set AND that alias is currently
    registered on the gateway — a default pointing at a retired/unregistered model is the
    silent-misconfiguration case (a stale catalog looks healthy while indexing/search can't
    actually run). Fails open (``ready``) when the gateway list is unknown (``registered is
    None``), matching the retirement checks, so a transient gateway outage doesn't
    hard-disable the feature.
    """
    alias = defaults.get("multimodal_embedding") or defaults.get("embedding")
    if not alias:
        return "disabled", None, "No default embedding model is set."
    if registered is None:
        return "ready", alias, None  # gateway unreadable → fail open
    if alias not in registered:
        return (
            "degraded",
            alias,
            f"The default embedding model '{alias}' is not registered on the Model Gateway.",
        )
    return "ready", alias, None


async def get_embedding_readiness(
    request: "Request", db: "AsyncSession"
) -> tuple[Status, str | None, str | None]:
    """Embedding readiness for the System Status page (see ``resolve_embedding_readiness``)."""
    defaults = await request.app.state.model_defaults_service.get_all(db)
    registered, _, _ = await get_model_registry(request, db)
    return resolve_embedding_readiness(defaults, registered)


async def is_embedding_ready(request: "Request", db: "AsyncSession") -> bool:
    """True when catalog indexing/search can actually embed (see get_embedding_readiness)."""
    status, _, _ = await get_embedding_readiness(request, db)
    return status == "ready"


# --- Full system status ---


async def collect_system_status(request: "Request", db: "AsyncSession") -> list[FeatureStatus]:
    """Evaluate every gated/optional feature for the admin System Status page."""
    features: list[FeatureStatus] = []

    # Model Gateway — the backbone every model-dependent feature relies on.
    if not config.model_gateway.master_key.get_secret_value():
        features.append(
            FeatureStatus(
                key="model_gateway",
                name="Model Gateway",
                status="disabled",
                detail="LITELLM_MASTER_KEY is not set — the console can't manage the gateway.",
                remediation="Set LITELLM_MASTER_KEY to the LiteLLM proxy master key.",
            )
        )
        registered: set[str] | None = None
    else:
        try:
            raw = await request.app.state.model_gateway_service.list_models()
            registered = {m.get("model_name") for m in raw if m.get("model_name")}
            features.append(
                FeatureStatus(
                    key="model_gateway",
                    name="Model Gateway",
                    status="ready",
                    detail=f"Reachable — {len(registered)} model(s) registered.",
                )
            )
        except ModelGatewayError as e:
            registered = None
            features.append(
                FeatureStatus(
                    key="model_gateway",
                    name="Model Gateway",
                    status="degraded",
                    detail=f"Configured but unreachable: {e}",
                    remediation="Check LLM_GATEWAY_URL and that the LiteLLM proxy is running.",
                )
            )

    defaults = await request.app.state.model_defaults_service.get_all(db)

    # Chat — needs a default chat model that's registered on the gateway.
    features.append(_model_default_feature("chat", "Chat models", defaults.get("chat"), registered))

    # Chat tiers — which capability tiers sub-agents can bind to, and which are missing.
    features.append(_chat_tiers_feature(defaults, registered))

    # Catalog — embedding default registered + Google OAuth for source connection.
    features.append(await _catalog_feature(request, db))

    # Optional integrations gated purely on config presence.
    features.append(
        _config_feature(
            "external_skill_search",
            "External skill search",
            config.skills_registry.registry_api_key is not None,
            ready_detail="skills.sh registry search enabled.",
            disabled_detail="SKILL_REGISTRY_API_KEY not set — only Git-sourced skills are available.",
            remediation="Set SKILL_REGISTRY_API_KEY to enable registry search.",
        )
    )
    features.append(
        _config_feature(
            "playbook_docstore",
            "Playbook editing (docstore)",
            config.docstore.is_configured,
            ready_detail="Docstore configured — AGENTS.md / skill files are editable.",
            disabled_detail="Docstore not configured — playbook editing is unavailable.",
            remediation="Set DOCSTORE_HOST and DOCSTORE_PASSWORD (or the POSTGRES_* fallbacks).",
        )
    )
    features.append(
        _config_feature(
            "keycloak_group_sync",
            "Keycloak group sync",
            bool(config.keycloak_admin.admin_client_id and config.keycloak_admin.admin_client_secret.get_secret_value()),
            ready_detail="Group membership is synced to Keycloak.",
            disabled_detail="Admin credentials not set — groups are stored locally only.",
            remediation="Set KEYCLOAK_ADMIN_CLIENT_ID and KEYCLOAK_ADMIN_CLIENT_SECRET.",
        )
    )
    features.append(
        _config_feature(
            "phone_verification",
            "Phone verification (Twilio Verify)",
            config.twilio_verify.is_configured,
            ready_detail="SMS OTP verification enabled.",
            disabled_detail="Twilio Verify not configured — phone verification is unavailable.",
            remediation="Set TWILIO_ACCOUNT_SID, TWILIO_VERIFY_API_KEY/SECRET and TWILIO_VERIFY_SERVICE_SID.",
        )
    )
    features.append(_voice_agent_feature())
    features.append(
        _config_feature(
            "outbound_scim_nightly",
            "Outbound SCIM nightly sync",
            config.outbound_scim.nightly_sync_enabled,
            ready_detail="Nightly full-sync to external IdPs is enabled.",
            disabled_detail="Nightly sync disabled — only real-time push events are delivered.",
            remediation="Set OUTBOUND_SCIM_NIGHTLY_SYNC_ENABLED=true.",
        )
    )
    features.append(
        _config_feature(
            "langsmith_links",
            "LangSmith trace links",
            bool(config.frontend.langsmith.organization_id and config.frontend.langsmith.project_id),
            ready_detail="Trace deep-links are shown in the console.",
            disabled_detail="LangSmith IDs not set — trace links are hidden.",
            remediation="Set LANGSMITH_ORGANIZATION_ID and LANGSMITH_PROJECT_ID.",
        )
    )
    return features


def _config_feature(
    key: str,
    name: str,
    enabled: bool,
    *,
    ready_detail: str,
    disabled_detail: str,
    remediation: str,
) -> FeatureStatus:
    """A feature gated purely on configuration presence (on/off, no live dependency)."""
    if enabled:
        return FeatureStatus(key=key, name=name, status="ready", detail=ready_detail)
    return FeatureStatus(key=key, name=name, status="disabled", detail=disabled_detail, remediation=remediation)


def _model_default_feature(
    key: str, name: str, alias: str | None, registered: set[str] | None
) -> FeatureStatus:
    """A feature gated on a per-role default model being set AND registered on the gateway."""
    if not alias:
        return FeatureStatus(
            key=key,
            name=name,
            status="disabled",
            detail=f"No default {key} model is set.",
            remediation=f"Set a default {key} model in Admin → Model Gateway.",
        )
    if registered is not None and alias not in registered:
        return FeatureStatus(
            key=key,
            name=name,
            status="degraded",
            detail=f"Default '{alias}' is not registered on the gateway.",
            remediation=f"Register '{alias}' on the gateway, or pick a different default in Admin → Model Gateway.",
        )
    return FeatureStatus(key=key, name=name, status="ready", detail=f"Default model: {alias}")


def _chat_tiers_feature(defaults: dict[str, str], registered: set[str] | None) -> FeatureStatus:
    """Visibility into the chat capability tiers (standard/low/premium).

    A sub-agent can bind to a tier instead of a concrete model. The optional low/premium tiers
    degrade gracefully: an unset tier silently routes to the standard chat default, and a tier
    pointing at a retired model defeats the pick the same way — both worth surfacing. Standard's
    own health is the "Chat models" row; here we report the picks and flag the optional tiers.
    """
    rows = [("chat", "Standard"), ("chat:low", "Low"), ("chat:premium", "Premium")]
    detail = " · ".join(f"{label}: {defaults.get(role) or 'not set'}" for role, label in rows)

    optional = [("chat:low", "Low"), ("chat:premium", "Premium")]
    stale = [
        f"{label} (→ '{defaults[role]}')"
        for role, label in optional
        if defaults.get(role) and registered is not None and defaults[role] not in registered
    ]
    missing = [label for role, label in optional if not defaults.get(role)]

    if stale:
        return FeatureStatus(
            key="chat_tiers",
            name="Chat model tiers",
            status="degraded",
            detail=detail,
            caveat=f"Retired tier default: {', '.join(stale)} — sub-agents on that tier silently "
            "fall back to the standard model.",
            remediation="Repoint the retired tier default in Admin → Model Gateway.",
        )
    if missing:
        caveat = (
            f"{' and '.join(missing)} tier not set — sub-agents bound to it run on the "
            "standard chat default."
        )
        # Indexing/chunking runs on the low tier; without it, that high-volume work falls
        # back to the (more expensive) standard chat default too.
        if "Low" in missing:
            caveat += (
                " Bulk indexing/chunking also runs on the standard chat default "
                "(more expensive) until a low tier is set."
            )
        return FeatureStatus(
            key="chat_tiers",
            name="Chat model tiers",
            status="limited",
            detail=detail,
            caveat=caveat,
            remediation='Assign a model to each tier ("Default low/premium tier") in Admin → Model Gateway.',
        )
    return FeatureStatus(key="chat_tiers", name="Chat model tiers", status="ready", detail=detail)


def _voice_agent_feature() -> FeatureStatus:
    """Voice agent: outbound calls with a sub-agent personality, dispatched by the scheduler
    to the system-owned `voice-agent` service.

    The only dependency observable from console-backend is whether the service URL is wired
    (VOICE_AGENT_URL, synced to the seeded voice-agent at startup). The voice-agent pod has its
    own runtime credentials — GCP_KEY for the Gemini Live API and Twilio Voice creds to place
    calls — but console-backend neither holds nor reads those, and the voice-agent builds its
    Gemini client lazily (per call), so their presence/validity can't be introspected from here.
    We surface that as a caveat rather than guess at a status we can't actually verify.
    """
    name = "Voice agent (outbound calls)"
    if not os.getenv("VOICE_AGENT_URL"):
        return FeatureStatus(
            key="voice_agent",
            name=name,
            status="disabled",
            detail="VOICE_AGENT_URL not set — outbound voice calls are unavailable.",
            remediation="Set VOICE_AGENT_URL to the deployed voice-agent service.",
        )
    return FeatureStatus(
        key="voice_agent",
        name=name,
        status="ready",
        detail="Outbound phone calls with a sub-agent personality are enabled.",
        caveat="Placing calls also requires the voice-agent pod's own Gemini Live (GCP_KEY) and "
        "Twilio Voice credentials, which run on that service and can't be verified from here.",
    )


async def _catalog_feature(request: "Request", db: "AsyncSession") -> FeatureStatus:
    """Catalog readiness: embedding model ready AND Google OAuth configured for sources."""
    emb_status, alias, emb_reason = await get_embedding_readiness(request, db)
    google_ok = config.catalog.is_configured

    if emb_status != "ready":
        return FeatureStatus(
            key="catalog",
            name="Catalog search & indexing",
            status="disabled" if emb_status == "disabled" else "degraded",
            detail=emb_reason or "Embedding model not ready.",
            remediation="Set a default embedding model (register one, then “Make default”) in Admin → Model Gateway.",
        )

    caveat = await _text_only_embedding_caveat(request, alias)

    if not google_ok:
        return FeatureStatus(
            key="catalog",
            name="Catalog search & indexing",
            status="degraded",
            detail=f"Embedding model '{alias}' is ready, but Google Drive sources can't be connected.",
            remediation="Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET to connect Drive sources.",
            caveat=caveat,
        )
    # Text-only embedding model → "limited": indexing/search works, but document images
    # (e.g. slides) aren't embedded. A working-with-a-caveat state, not a fault.
    return FeatureStatus(
        key="catalog",
        name="Catalog search & indexing",
        status="limited" if caveat else "ready",
        detail=f"Embedding model '{alias}' ready; Google Drive connection configured.",
        caveat=caveat,
    )


async def _text_only_embedding_caveat(request: "Request", alias: str | None) -> str | None:
    """Caveat for the catalog status when the resolved embedding model embeds text only.

    Only Gemini Embedding 2 fuses text+image; with any other model (e.g. Bedrock Nova/Titan),
    image bytes in documents — slide thumbnails especially — are dropped at index time, so
    visual content never reaches the vector. Surfacing this stops "ready" from over-promising
    multimodal search. Returns None (no caveat) for fusion-capable models, and fails open to
    None when the gateway can't be read.
    """
    from ringier_a2a_sdk.embeddings import supports_image_fusion

    try:
        model = await request.app.state.model_gateway_service.get_model(alias)
    except ModelGatewayError:
        return None
    litellm_model = ((model or {}).get("litellm_params") or {}).get("model")
    if supports_image_fusion(litellm_model):
        return None
    return (
        "This model embeds text only — images in documents (e.g. slide thumbnails) are not "
        "embedded, so visual-only content won't be searchable. Set a fusion-capable model "
        "(Gemini Embedding 2) as the multimodal_embedding default to embed images."
    )
