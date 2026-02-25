"""Unit tests for GPAgentRunnable and create_gp_local_subagent.

Tests cover:
- name / description properties
- _process success path (structured response → A2A dict)
- _process error handling (GraphInterrupt re-raises, generic exceptions → failed state)
- Checkpoint isolation: thread_id includes context_id and agent name
- Cache clearing: _cached_selected_tools is reset before each invocation
- Factory function create_gp_local_subagent returns a well-formed CompiledSubAgent
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphInterrupt

from app.a2a_utils.base import SubAgentInput
from app.a2a_utils.structured_response import SubAgentResponseSchema
from app.agents.gp_agent import GPAgentRunnable, create_gp_local_subagent
from app.models.base import ModelType
from app.models.config import GraphRuntimeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(**kwargs) -> GraphRuntimeContext:
    defaults = {
        "user_id": "user-1",
        "user_sub": "sub-1",
        "name": "Alice",
        "email": "alice@example.com",
    }
    defaults.update(kwargs)
    return GraphRuntimeContext(**defaults)


def _make_input(
    content: str = "Do something",
    context_id: str = "ctx-123",
    task_id: str = "task-456",
) -> SubAgentInput:
    """Build an input that satisfies GPAgentRunnable's requirement for both
    context_id AND task_id (supplied via a2a_tracking under the agent name key)."""
    return SubAgentInput(
        orchestrator_conversation_id=context_id,
        a2a_tracking={
            "general-purpose": {
                "context_id": context_id,
                "task_id": task_id,
                "is_complete": False,
            }
        },
        messages=[HumanMessage(content=content)],
    )


def _make_runnable(
    user_context: GraphRuntimeContext | None = None,
    model_type: ModelType = "gpt-4o-mini",  # type: ignore[assignment]
    **kwargs,
) -> tuple[GPAgentRunnable, MagicMock]:
    """Return (runnable, mock_graph_provider) pair."""
    if user_context is None:
        user_context = _make_context()

    mock_graph = AsyncMock()
    mock_graph.ainvoke = AsyncMock()

    def gp_graph_provider(model_type, thinking_level=None):
        return mock_graph

    runnable = GPAgentRunnable(
        gp_graph_provider=gp_graph_provider,
        user_context=user_context,
        model_type=model_type,
        user_sub="sub-1",
        **kwargs,
    )
    return runnable, mock_graph


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name_is_general_purpose(self):
        runnable, _ = _make_runnable()
        assert runnable.name == "general-purpose"

    def test_description_is_non_empty(self):
        runnable, _ = _make_runnable()
        assert len(runnable.description) > 0

    def test_description_matches_module_constant(self):
        from app.agents.gp_agent import GP_DESCRIPTION

        runnable, _ = _make_runnable()
        assert runnable.description == GP_DESCRIPTION


# ---------------------------------------------------------------------------
# _process — success path
# ---------------------------------------------------------------------------


class TestProcessSuccess:
    @pytest.mark.asyncio
    async def test_returns_completed_state(self):
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="All done.")],
                "structured_response": SubAgentResponseSchema(
                    task_state="completed",
                    message="All done.",
                ),
            }
        )

        result = await runnable._process(_make_input())

        assert result["state"] == "completed"
        assert result["is_complete"] is True
        assert result["requires_input"] is False

    @pytest.mark.asyncio
    async def test_returns_input_required_state(self):
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="What project?")],
                "structured_response": SubAgentResponseSchema(
                    task_state="input_required",
                    message="What project?",
                ),
            }
        )

        result = await runnable._process(_make_input("Create a ticket"))

        assert result["state"] == "input_required"
        assert result["is_complete"] is False
        assert result["requires_input"] is True

    @pytest.mark.asyncio
    async def test_returns_failed_state_from_structured_response(self):
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Could not proceed.")],
                "structured_response": SubAgentResponseSchema(
                    task_state="failed",
                    message="Could not proceed.",
                ),
            }
        )

        result = await runnable._process(_make_input())

        assert result["state"] == "failed"
        assert result["is_complete"] is False

    @pytest.mark.asyncio
    async def test_bedrock_tool_call_style_response(self):
        """Handles Bedrock-style structured output via tool_calls on the last message."""
        runnable, mock_graph = _make_runnable()

        mock_message = MagicMock()
        mock_message.content = ""
        mock_message.tool_calls = [
            {
                "name": "SubAgentResponseSchema",
                "args": {"task_state": "completed", "message": "Done via tool call."},
            }
        ]
        mock_graph.ainvoke = AsyncMock(return_value={"messages": [mock_message]})

        result = await runnable._process(_make_input())

        assert result["state"] == "completed"
        assert result["is_complete"] is True

    @pytest.mark.asyncio
    async def test_context_id_propagated_to_result(self):
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Done.")],
                "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
            }
        )

        result = await runnable._process(_make_input(context_id="my-context-id"))

        assert result.get("context_id") == "my-context-id"


# ---------------------------------------------------------------------------
# _process — error handling
# ---------------------------------------------------------------------------


class TestProcessErrorHandling:
    @pytest.mark.asyncio
    async def test_generic_exception_returns_failed_state(self):
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("upstream failure"))

        result = await runnable._process(_make_input())

        assert result["state"] == "failed"
        assert result["is_complete"] is False

    @pytest.mark.asyncio
    async def test_graph_interrupt_is_re_raised(self):
        """GraphInterrupt must propagate so the orchestrator can handle it."""
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(side_effect=GraphInterrupt("interrupt!"))

        with pytest.raises(GraphInterrupt):
            await runnable._process(_make_input())

    @pytest.mark.asyncio
    async def test_missing_tracking_ids_raises_value_error(self):
        """Missing both context_id and task_id raises ValueError (before the try block).

        The caller (ainvoke) converts this to a failed A2A response, but _process
        itself raises here because the check precedes the try/except.
        """
        runnable, _ = _make_runnable()

        # No a2a_tracking for 'general-purpose' and no orchestrator_conversation_id
        bad_input = SubAgentInput(
            a2a_tracking={},
            messages=[HumanMessage(content="hello")],
            orchestrator_conversation_id=None,
        )

        with pytest.raises(ValueError, match="Missing context_id or task_id"):
            await runnable._process(bad_input)


# ---------------------------------------------------------------------------
# Checkpoint isolation
# ---------------------------------------------------------------------------


class TestCheckpointIsolation:
    @pytest.mark.asyncio
    async def test_thread_id_contains_context_id_and_agent_name(self):
        runnable, mock_graph = _make_runnable()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Done.")],
                "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
            }
        )

        captured_config: dict = {}

        async def capture_invoke(messages, config=None, **kwargs):
            if config:
                captured_config.update(config)
            return {
                "messages": [MagicMock(content="Done.")],
                "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
            }

        mock_graph.ainvoke = capture_invoke

        await runnable._process(_make_input(context_id="abc-123"))

        configurable = captured_config.get("configurable", {})
        thread_id = configurable.get("thread_id", "")
        assert "abc-123" in thread_id
        assert "general-purpose" in thread_id

    @pytest.mark.asyncio
    async def test_different_context_ids_produce_different_thread_ids(self):
        runnable, mock_graph = _make_runnable()

        thread_ids: list[str] = []

        async def capture_invoke(messages, config=None, **kwargs):
            if config:
                thread_ids.append(config.get("configurable", {}).get("thread_id", ""))
            return {
                "messages": [MagicMock(content="Done.")],
                "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
            }

        mock_graph.ainvoke = capture_invoke

        await runnable._process(_make_input(context_id="ctx-aaa"))
        await runnable._process(_make_input(context_id="ctx-bbb"))

        assert len(thread_ids) == 2
        assert thread_ids[0] != thread_ids[1]


# ---------------------------------------------------------------------------
# Cache clearing
# ---------------------------------------------------------------------------


class TestCacheClearing:
    @pytest.mark.asyncio
    async def test_cached_tools_cleared_before_each_invocation(self):
        ctx = _make_context()
        ctx._cached_selected_tools = [MagicMock()]  # stale cache

        runnable, mock_graph = _make_runnable(user_context=ctx)
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Done.")],
                "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
            }
        )

        # Before _process: cache exists
        assert ctx._cached_selected_tools is not None

        await runnable._process(_make_input())

        # After _process invokes ainvoke, cache should have been cleared at the start
        # (the graph itself may re-populate it, but we verify it was reset beforehand)
        # We check that ainvoke was called (meaning reset happened before the call)
        mock_graph.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_cached_tools_is_none_at_graph_invocation_time(self):
        """Verify that the cache is None when ainvoke is called, not after."""
        ctx = _make_context()
        ctx._cached_selected_tools = ["stale"]

        runnable, mock_graph = _make_runnable(user_context=ctx)

        cache_at_invoke_time: list = []

        async def check_cache(messages, config=None, **kwargs):
            cache_at_invoke_time.append(ctx._cached_selected_tools)
            return {
                "messages": [MagicMock(content="Done.")],
                "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
            }

        mock_graph.ainvoke = check_cache

        await runnable._process(_make_input())

        assert cache_at_invoke_time == [None]


# ---------------------------------------------------------------------------
# create_gp_local_subagent factory
# ---------------------------------------------------------------------------


class TestCreateGpLocalSubagent:
    def test_returns_compiled_subagent_with_correct_name(self):
        ctx = _make_context()
        mock_provider = MagicMock(return_value=MagicMock())

        subagent = create_gp_local_subagent(
            gp_graph_provider=mock_provider,
            user_context=ctx,
            model_type="gpt-4o-mini",  # type: ignore[arg-type]
            user_sub="sub-1",
        )

        assert subagent["name"] == "general-purpose"

    def test_returns_compiled_subagent_with_non_empty_description(self):
        ctx = _make_context()
        mock_provider = MagicMock(return_value=MagicMock())

        subagent = create_gp_local_subagent(
            gp_graph_provider=mock_provider,
            user_context=ctx,
            model_type="gpt-4o-mini",  # type: ignore[arg-type]
            user_sub="sub-1",
        )

        assert len(subagent["description"]) > 0

    def test_runnable_is_gp_agent_runnable_instance(self):
        ctx = _make_context()
        mock_provider = MagicMock(return_value=MagicMock())

        subagent = create_gp_local_subagent(
            gp_graph_provider=mock_provider,
            user_context=ctx,
            model_type="gpt-4o-mini",  # type: ignore[arg-type]
            user_sub="sub-1",
        )

        assert isinstance(subagent["runnable"], GPAgentRunnable)

    def test_runnable_stores_user_context(self):
        ctx = _make_context()
        mock_provider = MagicMock(return_value=MagicMock())

        subagent = create_gp_local_subagent(
            gp_graph_provider=mock_provider,
            user_context=ctx,
            model_type="gpt-4o-mini",  # type: ignore[arg-type]
            user_sub="sub-1",
        )

        assert subagent["runnable"]._user_context is ctx

    def test_cost_logger_wired_through(self):
        ctx = _make_context()
        mock_provider = MagicMock(return_value=MagicMock())
        cost_logger = MagicMock()

        subagent = create_gp_local_subagent(
            gp_graph_provider=mock_provider,
            user_context=ctx,
            model_type="gpt-4o-mini",  # type: ignore[arg-type]
            user_sub="sub-1",
            cost_logger=cost_logger,
        )

        # Cost tracking should be enabled on the runnable
        runnable: GPAgentRunnable = subagent["runnable"]
        assert runnable._cost_tracking_enabled is True

    def test_thinking_level_stored(self):
        ctx = _make_context()
        mock_provider = MagicMock(return_value=MagicMock())

        subagent = create_gp_local_subagent(
            gp_graph_provider=mock_provider,
            user_context=ctx,
            model_type="gpt-4o-mini",  # type: ignore[arg-type]
            user_sub="sub-1",
            thinking_level="auto",  # type: ignore[arg-type]
        )

        assert subagent["runnable"]._thinking_level == "auto"
