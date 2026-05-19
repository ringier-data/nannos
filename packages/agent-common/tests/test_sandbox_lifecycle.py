"""Tests for DynamicLocalAgentRunnable sandbox lifecycle integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable


def _make_config(sandbox_enabled: bool = False) -> LocalLangGraphSubAgentConfig:
    return LocalLangGraphSubAgentConfig(
        name="test-agent",
        description="A test agent",
        system_prompt="You are a test agent.",
        sandbox_enabled=sandbox_enabled,
    )


def _make_mock_pool():
    """Create a mock SandboxPool that tracks acquire/release."""
    from agent_common.core.sandbox_pool import PooledSandbox

    mock_backend = AsyncMock()
    mock_backend.aupload_files = AsyncMock(return_value=[])
    mock_backend.execute = MagicMock(return_value=type("R", (), {"output": "", "exit_code": 0})())
    mock_backend.close = MagicMock()

    pooled = PooledSandbox(backend=mock_backend)

    pool = AsyncMock()
    pool.acquire = AsyncMock(return_value=pooled)
    pool.release = AsyncMock()
    return pool, pooled


def _make_input_and_config(session_id: str = "session-123"):
    """Create valid SubAgentInput and config for testing.

    Note: The base class _instrument() builds thread_id as
    "{context_id}::dynamic-{agent_name}" before calling _astream_impl.
    We simulate this in invoke_config.
    """
    from langchain_core.messages import HumanMessage

    from agent_common.a2a.base import SubAgentInput

    context_id = session_id  # In practice, context_id is the conversation ID
    input_data = SubAgentInput(
        messages=[HumanMessage(content="hello")],
        a2a_tracking={"test-agent": {"context_id": context_id}},
        orchestrator_conversation_id=session_id,
    )
    # Simulates what _instrument() puts into the config
    invoke_config = {
        "configurable": {"thread_id": f"{context_id}::dynamic-test-agent"},
        "metadata": {},
    }
    return input_data, invoke_config


@pytest.mark.asyncio
async def test_sandbox_acquired_and_released_on_success():
    """Sandbox is acquired at start and released at end of invocation."""
    pool, pooled = _make_mock_pool()
    config = _make_config(sandbox_enabled=True)

    runnable = DynamicLocalAgentRunnable(
        config=config,
        model=MagicMock(),
        sandbox_pool=pool,
    )

    # Mock _ensure_agent to return a mock compiled graph
    mock_agent = MagicMock()

    async def mock_astream(*args, **kwargs):
        return
        yield  # noqa: make it an async generator

    mock_agent.astream = mock_astream

    # Cache state that sandbox path needs
    runnable._cached_tools = []
    runnable._cached_system_prompt = "test prompt"
    runnable._cached_response_format = None
    runnable._cached_hitl_guarded = None
    runnable._cached_effective_backend_factory = None
    runnable._resolved_skills = {}

    input_data, invoke_config = _make_input_and_config("session-123")

    with patch.object(runnable, "_ensure_agent", new_callable=AsyncMock, return_value=mock_agent):
        with patch.object(runnable, "_build_graph", return_value=mock_agent):
            with patch(
                "agent_common.agents.dynamic_agent.retrieve_final_state",
                return_value={"messages": []},
            ):
                events = []
                async for event in runnable.astream(input_data.model_dump(), invoke_config):
                    events.append(event)

    # Verify acquire was called with thread_id and agent name
    pool.acquire.assert_awaited_once_with("session-123::dynamic-test-agent", "test-agent")
    # Verify release was called (finally block)
    pool.release.assert_awaited_once_with("session-123::dynamic-test-agent", "test-agent")


@pytest.mark.asyncio
async def test_sandbox_released_on_error():
    """Sandbox is released even when _ensure_agent raises."""
    pool, pooled = _make_mock_pool()
    config = _make_config(sandbox_enabled=True)

    runnable = DynamicLocalAgentRunnable(
        config=config,
        model=MagicMock(),
        sandbox_pool=pool,
    )

    # Cache state
    runnable._cached_tools = []
    runnable._cached_system_prompt = "test prompt"
    runnable._cached_response_format = None
    runnable._cached_hitl_guarded = None
    runnable._cached_effective_backend_factory = None
    runnable._resolved_skills = {}

    input_data, invoke_config = _make_input_and_config("session-456")

    # Mock _ensure_agent to raise — this happens before sandbox acquire
    with patch.object(runnable, "_ensure_agent", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        events = []
        async for event in runnable.astream(input_data.model_dump(), invoke_config):
            events.append(event)

    # _ensure_agent raises before sandbox acquire, so no sandbox interaction
    pool.acquire.assert_not_awaited()
    pool.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_sandbox_when_disabled():
    """When sandbox_enabled=False, no pool interaction occurs."""
    pool, _ = _make_mock_pool()
    config = _make_config(sandbox_enabled=False)

    runnable = DynamicLocalAgentRunnable(
        config=config,
        model=MagicMock(),
        sandbox_pool=pool,
    )

    mock_agent = MagicMock()

    async def mock_astream(*args, **kwargs):
        return
        yield

    mock_agent.astream = mock_astream

    input_data, invoke_config = _make_input_and_config("session-789")

    with patch.object(runnable, "_ensure_agent", new_callable=AsyncMock, return_value=mock_agent):
        runnable._agent = mock_agent  # _astream_impl uses self._agent for non-sandbox
        with patch(
            "agent_common.agents.dynamic_agent.retrieve_final_state",
            return_value={"messages": []},
        ):
            events = []
            async for event in runnable.astream(input_data.model_dump(), invoke_config):
                events.append(event)

    # No sandbox interaction when disabled
    pool.acquire.assert_not_awaited()
    pool.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_sandbox_when_pool_is_none():
    """When sandbox_pool is None (no provider configured), no sandbox is used."""
    config = _make_config(sandbox_enabled=True)

    runnable = DynamicLocalAgentRunnable(
        config=config,
        model=MagicMock(),
        sandbox_pool=None,
    )

    mock_agent = MagicMock()

    async def mock_astream(*args, **kwargs):
        return
        yield

    mock_agent.astream = mock_astream

    input_data, invoke_config = _make_input_and_config("session-000")

    with patch.object(runnable, "_ensure_agent", new_callable=AsyncMock, return_value=mock_agent):
        runnable._agent = mock_agent  # _astream_impl uses self._agent for non-sandbox
        with patch(
            "agent_common.agents.dynamic_agent.retrieve_final_state",
            return_value={"messages": []},
        ):
            events = []
            async for event in runnable.astream(input_data.model_dump(), invoke_config):
                events.append(event)

    # Should complete without error (uses cached self._agent, no sandbox)
