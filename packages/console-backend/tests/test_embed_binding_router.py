"""Embed bindings (ADR-0006), router guard: content the host publishes is read-only on a
bound sub-agent; what the host leaves out stays editable; whole-version operations are
refused."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from console_backend.models.embed_binding import EmbedBinding, WellKnownAgentInfo
from console_backend.models.sub_agent import ModelTier, SubAgentUpdate, ThinkingLevel
from console_backend.routers.sub_agent_router import _reject_if_embed_bound

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def binding(tools=None, model_tier=None, thinking_level=None) -> EmbedBinding:
    return EmbedBinding(
        sub_agent_id=20,
        base_url="https://riad.example",
        index_url="https://riad.example/.well-known/agent-skills/index.json",
        azps=["nannos-embedded"],
        revision="abc",
        agent=WellKnownAgentInfo(
            name="A",
            description="d",
            prompt_url="u",
            prompt_digest="sha256:x",
            tools=tools,
            model_tier=model_tier,
            thinking_level=thinking_level,
        ),
        created_by="admin-1",
        created_at=NOW,
        updated_at=NOW,
    )


def request_with(binding_value):
    request = MagicMock()
    request.app.state.embed_binding_service.get_binding = AsyncMock(
        return_value=binding_value
    )
    return request


async def rejected(request, data) -> str | None:
    try:
        await _reject_if_embed_bound(request, MagicMock(), 20, data)
    except HTTPException as e:
        assert e.status_code == 409
        return str(e.detail)
    return None


@pytest.mark.asyncio
async def test_unbound_sub_agent_is_never_refused():
    request = request_with(None)
    assert await rejected(request, SubAgentUpdate(system_prompt="x")) is None
    assert await rejected(request, None) is None


@pytest.mark.asyncio
async def test_host_published_content_is_refused_but_nannos_side_settings_are_not():
    request = request_with(
        binding()
    )  # cockpit-style: no tools, model or thinking published
    assert "system_prompt" in (
        await rejected(request, SubAgentUpdate(system_prompt="x")) or ""
    )
    assert "description" in (
        await rejected(request, SubAgentUpdate(description="x")) or ""
    )
    assert "skills" in (await rejected(request, SubAgentUpdate(skills=[])) or "")
    # Nannos owns these when the host does not publish them
    assert await rejected(request, SubAgentUpdate(mcp_tools=["list_campaigns"])) is None
    assert await rejected(request, SubAgentUpdate(model_tier=ModelTier.PREMIUM)) is None
    assert (
        await rejected(
            request,
            SubAgentUpdate(enable_thinking=True, thinking_level=ThinkingLevel.HIGH),
        )
        is None
    )
    assert (
        await rejected(request, SubAgentUpdate(name="Renamed", is_public=False)) is None
    )


@pytest.mark.asyncio
async def test_published_tools_model_and_thinking_become_read_only():
    request = request_with(
        binding(tools=["list_campaigns"], model_tier="standard", thinking_level="low")
    )
    assert "mcp_tools" in (
        await rejected(request, SubAgentUpdate(mcp_tools=["x"])) or ""
    )
    assert "model_tier" in (
        await rejected(request, SubAgentUpdate(model_tier=ModelTier.LOW)) or ""
    )
    assert "thinking" in (
        await rejected(request, SubAgentUpdate(enable_thinking=False)) or ""
    )


@pytest.mark.asyncio
async def test_whole_version_operations_are_refused_on_bound_sub_agents():
    detail = await rejected(request_with(binding()), None)
    assert (
        detail is not None
        and "the version" in detail
        and "https://riad.example" in detail
    )
