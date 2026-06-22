"""Unit tests for embedding readiness — the signal behind the catalog gate and System Status.

Covers the silent-misconfiguration case: a default embedding alias that is set but no longer
registered on the gateway must read as 'degraded' (not 'ready'), so a stale catalog stops
looking healthy.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import console_backend.services.model_status as model_status
from console_backend.services.feature_status import (
    _catalog_feature,
    _chat_tiers_feature,
    get_embedding_readiness,
)
from console_backend.services.model_gateway_service import ModelGatewayError

_NOTE = "images in documents"


def _make_request(defaults: dict, *, gateway_models=None, gateway_raises=False, litellm_models=None):
    """Fake Request whose app.state exposes the two services get_model_registry/readiness use.

    ``litellm_models`` maps model_name → litellm_params.model so get_model() returns a realistic
    deployment dict (used by the catalog's text-only-embedding note).
    """
    model_defaults_service = SimpleNamespace(
        get_all=AsyncMock(return_value=defaults),
        get_alias_tiers=AsyncMock(return_value={}),
    )
    litellm_models = litellm_models or {}

    async def _get_model(name):
        if name in litellm_models:
            return {"model_name": name, "litellm_params": {"model": litellm_models[name]}}
        return None

    if gateway_raises:
        list_models = AsyncMock(side_effect=ModelGatewayError("gateway down"))
        get_model = AsyncMock(side_effect=ModelGatewayError("gateway down"))
    else:
        list_models = AsyncMock(return_value=[{"model_name": m} for m in (gateway_models or [])])
        get_model = AsyncMock(side_effect=_get_model)
    model_gateway_service = SimpleNamespace(list_models=list_models, get_model=get_model)
    app = SimpleNamespace(state=SimpleNamespace(
        model_defaults_service=model_defaults_service,
        model_gateway_service=model_gateway_service,
    ))
    return SimpleNamespace(app=app)


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """get_model_registry caches the gateway registry ~30s in a module global — reset per test."""
    model_status._cache = None
    yield
    model_status._cache = None


@pytest.mark.asyncio
async def test_disabled_when_no_default():
    status, alias, reason = await get_embedding_readiness(_make_request({}), db=None)
    assert status == "disabled"
    assert alias is None
    assert "No default" in reason


@pytest.mark.asyncio
async def test_ready_when_default_registered():
    req = _make_request({"embedding": "gemini-embedding-2"}, gateway_models=["gemini-embedding-2", "claude-x"])
    status, alias, reason = await get_embedding_readiness(req, db=None)
    assert status == "ready"
    assert alias == "gemini-embedding-2"
    assert reason is None


@pytest.mark.asyncio
async def test_degraded_when_default_not_registered():
    # The silent-misconfig case: a leftover default alias the gateway no longer knows about.
    req = _make_request({"embedding": "gemini-embedding-2"}, gateway_models=["claude-x"])
    status, alias, reason = await get_embedding_readiness(req, db=None)
    assert status == "degraded"
    assert alias == "gemini-embedding-2"
    assert "not registered" in reason


@pytest.mark.asyncio
async def test_fails_open_when_gateway_unreachable():
    # Gateway unreadable → don't hard-disable (matches the retirement checks).
    req = _make_request({"multimodal_embedding": "gemini-embedding-2"}, gateway_raises=True)
    status, alias, _ = await get_embedding_readiness(req, db=None)
    assert status == "ready"
    assert alias == "gemini-embedding-2"


@pytest.mark.asyncio
async def test_multimodal_default_preferred_over_text():
    req = _make_request(
        {"embedding": "text-emb", "multimodal_embedding": "mm-emb"},
        gateway_models=["mm-emb", "text-emb"],
    )
    _, alias, _ = await get_embedding_readiness(req, db=None)
    assert alias == "mm-emb"


@pytest.mark.asyncio
async def test_catalog_limited_with_caveat_for_non_fusion_model():
    # Nova embeds text only via our Gemini client → catalog is "limited", images dropped.
    req = _make_request(
        {"multimodal_embedding": "nova-2-multimodal-embeddings-v1:0"},
        gateway_models=["nova-2-multimodal-embeddings-v1:0"],
        litellm_models={"nova-2-multimodal-embeddings-v1:0": "bedrock/amazon.nova-2-multimodal-embeddings-v1:0"},
    )
    feature = await _catalog_feature(req, db=None)
    # "limited" when Google OAuth is configured; otherwise "degraded" (Drive) wins — but the
    # caveat is surfaced either way.
    assert feature.status in ("limited", "degraded")
    assert feature.caveat is not None and _NOTE in feature.caveat
    assert _NOTE not in feature.detail  # caveat lives in its own field, not the detail line


@pytest.mark.asyncio
async def test_catalog_no_caveat_for_gemini_embedding():
    # Gemini Embedding 2 genuinely fuses text+image → no caveat, not "limited".
    req = _make_request(
        {"multimodal_embedding": "gemini-embedding-2"},
        gateway_models=["gemini-embedding-2"],
        litellm_models={"gemini-embedding-2": "vertex_ai/gemini-embedding-2"},
    )
    feature = await _catalog_feature(req, db=None)
    assert feature.caveat is None
    assert feature.status != "limited"


@pytest.mark.asyncio
async def test_catalog_caveat_omitted_when_model_lookup_fails():
    # Gateway hiccup on get_model → fail open, no (possibly wrong) caveat.
    req = _make_request(
        {"multimodal_embedding": "gemini-embedding-2"},
        gateway_raises=True,  # readiness fails open to ready; get_model also raises
    )
    feature = await _catalog_feature(req, db=None)
    assert feature.caveat is None
    assert feature.status != "limited"


# --- Chat model tiers row ---
_REG = {"sonnet", "haiku", "opus"}


def test_chat_tiers_ready_when_both_optional_tiers_set_and_live():
    f = _chat_tiers_feature({"chat": "sonnet", "chat:low": "haiku", "chat:premium": "opus"}, _REG)
    assert f.status == "ready"
    assert "Low: haiku" in f.detail and "Premium: opus" in f.detail


def test_chat_tiers_limited_when_a_tier_is_unset():
    f = _chat_tiers_feature({"chat": "sonnet", "chat:low": "haiku"}, _REG)
    assert f.status == "limited"
    assert "Premium: not set" in f.detail
    assert "Premium" in f.caveat  # names the missing tier
    assert f.remediation


def test_chat_tiers_degraded_when_a_tier_points_at_retired_model():
    f = _chat_tiers_feature({"chat": "sonnet", "chat:low": "haiku", "chat:premium": "opus-gone"}, _REG)
    assert f.status == "degraded"
    assert "opus-gone" in f.caveat


def test_chat_tiers_fails_open_when_registry_unknown():
    # Gateway unreadable (registered=None): a set tier isn't flagged retired.
    f = _chat_tiers_feature({"chat": "sonnet", "chat:low": "haiku", "chat:premium": "opus"}, None)
    assert f.status == "ready"
