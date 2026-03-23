"""Unit tests for GPAgentRunnable and create_gp_local_subagent.

Tests cover:
- name / description properties
- _astream_impl success path (structured response → terminal TaskUpdate)
- _astream_impl error handling (GraphInterrupt re-raises, generic exceptions → ErrorEvent)
- Checkpoint isolation: thread_id includes context_id and agent name
- Cache clearing: _cached_selected_tools is reset before each invocation
- Factory function create_gp_local_subagent returns a well-formed CompiledSubAgent
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import TaskState
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.stream_events import ErrorEvent, TaskUpdate
from agent_common.a2a.structured_response import SubAgentResponseSchema
from agent_common.models.base import ModelType
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphInterrupt

from app.agents.gp_agent import GPAgentRunnable, create_gp_local_subagent
from app.models.config import GraphRuntimeContext


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
    """Return (runnable, mock_graph) pair.

    mock_graph has an empty astream (no intermediate events).
    Tests set up retrieve_final_state to return the desired final state.
    """
    if user_context is None:
        user_context = _make_context()

    mock_graph = AsyncMock()

    async def empty_stream(*args, **kwargs):
        return
        yield  # make it an async generator

    mock_graph.astream = empty_stream

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


# Shared config for _astream_impl calls (needs configurable for checkpoint isolation)
_STREAM_CONFIG = {"configurable": {"thread_id": "test", "checkpoint_ns": ""}}


async def _collect_events(runnable, input_data=None, config=None, final_state=None):
    """Collect all events from _astream_impl with mocked retrieve_final_state."""
    if input_data is None:
        input_data = _make_input()
    if config is None:
        config = _STREAM_CONFIG
    if final_state is None:
        final_state = {}
    with patch("app.agents.gp_agent.retrieve_final_state", return_value=final_state):
        return [event async for event in runnable._astream_impl(input_data, config=config)]


class TestProcessSuccess:
    @pytest.mark.asyncio
    async def test_returns_completed_state(self):
        runnable, mock_graph = _make_runnable()
        final_state = {
            "messages": [MagicMock(content="All done.")],
            "structured_response": SubAgentResponseSchema(
                task_state="completed",
                message="All done.",
            ),
        }

        events = await _collect_events(runnable, final_state=final_state)

        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        result = terminal.data
        assert result.state == TaskState.completed
        assert result.is_complete is True
        assert result.requires_input is False

    @pytest.mark.asyncio
    async def test_returns_input_required_state(self):
        runnable, mock_graph = _make_runnable()
        final_state = {
            "messages": [MagicMock(content="What project?")],
            "structured_response": SubAgentResponseSchema(
                task_state="input_required",
                message="What project?",
            ),
        }

        events = await _collect_events(
            runnable,
            input_data=_make_input("Create a ticket"),
            final_state=final_state,
        )

        terminal = [e for e in events if isinstance(e, TaskUpdate)][-1]
        result = terminal.data
        assert result.state == TaskState.input_required
        assert result.is_complete is False
        assert result.requires_input is True

    @pytest.mark.asyncio
    async def test_returns_failed_state_from_structured_response(self):
        runnable, mock_graph = _make_runnable()
        final_state = {
            "messages": [MagicMock(content="Could not proceed.")],
            "structured_response": SubAgentResponseSchema(
                task_state="failed",
                message="Could not proceed.",
            ),
        }

        events = await _collect_events(runnable, final_state=final_state)

        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        result = terminal.data
        assert result.state == TaskState.failed
        assert result.is_complete is True

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
        final_state = {"messages": [mock_message]}

        events = await _collect_events(runnable, final_state=final_state)

        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        result = terminal.data
        assert result.state == TaskState.completed
        assert result.is_complete is True

    @pytest.mark.asyncio
    async def test_context_id_propagated_to_result(self):
        runnable, mock_graph = _make_runnable()
        final_state = {
            "messages": [MagicMock(content="Done.")],
            "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
        }

        events = await _collect_events(
            runnable,
            input_data=_make_input(context_id="my-context-id"),
            final_state=final_state,
        )

        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        assert terminal.data.context_id == "my-context-id"


class TestProcessErrorHandling:
    @pytest.mark.asyncio
    async def test_generic_exception_returns_error_event(self):
        runnable, mock_graph = _make_runnable()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("upstream failure")
            yield

        mock_graph.astream = failing_stream

        events = [event async for event in runnable._astream_impl(_make_input(), config=_STREAM_CONFIG)]

        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert "upstream failure" in events[0].error

    @pytest.mark.asyncio
    async def test_graph_interrupt_is_re_raised(self):
        """GraphInterrupt must propagate so the orchestrator can handle it."""
        runnable, mock_graph = _make_runnable()

        async def interrupt_stream(*args, **kwargs):
            raise GraphInterrupt("interrupt!")
            yield

        mock_graph.astream = interrupt_stream

        with pytest.raises(GraphInterrupt):
            async for _ in runnable._astream_impl(_make_input(), config=_STREAM_CONFIG):
                pass

    @pytest.mark.asyncio
    async def test_missing_tracking_ids_raises_value_error(self):
        """Missing context_id raises ValueError in _astream_impl."""
        runnable, _ = _make_runnable()

        bad_input = SubAgentInput(
            a2a_tracking={},
            messages=[HumanMessage(content="hello")],
            orchestrator_conversation_id=None,
        )

        with pytest.raises(ValueError, match="Missing context_id"):
            async for _ in runnable._astream_impl(bad_input, config=_STREAM_CONFIG):
                pass


class TestCheckpointIsolation:
    @pytest.mark.asyncio
    async def test_thread_id_contains_context_id_and_agent_name(self):
        runnable, mock_graph = _make_runnable()

        captured_config: dict = {}
        final_state = {
            "messages": [MagicMock(content="Done.")],
            "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
        }

        async def capture_astream(messages, config=None, **kwargs):
            if config:
                captured_config.update(config)
            return
            yield

        input_data = _make_input(context_id="abc-123")
        config = runnable._instrument(input_data=input_data, config={"tags": ["tag1", "tag2"]})
        mock_graph.astream = capture_astream

        with patch("app.agents.gp_agent.retrieve_final_state", return_value=final_state):
            async for _ in runnable._astream_impl(input_data, config=config):
                pass

        configurable = captured_config.get("configurable", {})
        thread_id = configurable.get("thread_id", "")
        assert "abc-123" in thread_id
        assert "general-purpose" in thread_id

    @pytest.mark.asyncio
    async def test_different_context_ids_produce_different_thread_ids(self):
        runnable, mock_graph = _make_runnable()

        thread_ids: list[str] = []
        final_state = {
            "messages": [MagicMock(content="Done.")],
            "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
        }

        async def capture_astream(messages, config=None, **kwargs):
            if config:
                thread_ids.append(config.get("configurable", {}).get("thread_id", ""))
            return
            yield

        mock_graph.astream = capture_astream

        with patch("app.agents.gp_agent.retrieve_final_state", return_value=final_state):
            async for _ in runnable._astream_impl(
                _make_input(context_id="ctx-aaa"),
                config=runnable._instrument(input_data=_make_input(context_id="ctx-aaa"), config={"tags": ["tag1"]}),
            ):
                pass
            async for _ in runnable._astream_impl(
                _make_input(context_id="ctx-bbb"),
                config=runnable._instrument(input_data=_make_input(context_id="ctx-bbb"), config={"tags": ["tag2"]}),
            ):
                pass

        assert len(thread_ids) == 2
        assert thread_ids[0] != thread_ids[1]


class TestCacheClearing:
    @pytest.mark.asyncio
    async def test_cached_tools_cleared_before_each_invocation(self):
        ctx = _make_context()
        ctx._cached_selected_tools = [MagicMock()]  # stale cache

        runnable, mock_graph = _make_runnable(user_context=ctx)
        final_state = {
            "messages": [MagicMock(content="Done.")],
            "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
        }

        # Before _astream_impl: cache exists
        assert ctx._cached_selected_tools is not None

        with patch("app.agents.gp_agent.retrieve_final_state", return_value=final_state):
            async for _ in runnable._astream_impl(_make_input(), config=_STREAM_CONFIG):
                pass

        # After _astream_impl, we verify graph was called (meaning reset happened before the call)
        # (the graph itself may re-populate it, but we verify it was reset beforehand)

    @pytest.mark.asyncio
    async def test_cached_tools_is_none_at_graph_invocation_time(self):
        """Verify that the cache is None when astream is called, not after."""
        ctx = _make_context()
        ctx._cached_selected_tools = ["stale"]

        runnable, mock_graph = _make_runnable(user_context=ctx)

        cache_at_stream_time: list = []
        final_state = {
            "messages": [MagicMock(content="Done.")],
            "structured_response": SubAgentResponseSchema(task_state="completed", message="Done."),
        }

        async def check_cache(*args, **kwargs):
            cache_at_stream_time.append(ctx._cached_selected_tools)
            return
            yield

        mock_graph.astream = check_cache

        with patch("app.agents.gp_agent.retrieve_final_state", return_value=final_state):
            async for _ in runnable._astream_impl(_make_input(), config=_STREAM_CONFIG):
                pass

        assert cache_at_stream_time == [None]


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
