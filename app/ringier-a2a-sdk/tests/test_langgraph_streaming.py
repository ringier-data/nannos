"""Tests for LangGraphAgent._stream_impl streaming logic."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from a2a.types import Task, TaskState
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import SecretStr

from ringier_a2a_sdk.agent.langgraph import LangGraphAgent
from ringier_a2a_sdk.models import UserConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user_config(**kwargs):
    defaults = dict(
        user_sub="test-user",
        access_token=SecretStr("tok"),
        name="Test",
        email="test@test.com",
    )
    defaults.update(kwargs)
    return UserConfig(**defaults)


def _make_task(**kwargs):
    t = Mock(spec=Task)
    t.id = kwargs.get("id", "task-1")
    t.context_id = kwargs.get("context_id", "ctx-1")
    return t


def _v2_messages_part(msg_chunk, meta=None):
    """Create a v2 stream part with type='messages'."""
    return {"type": "messages", "data": (msg_chunk, meta or {})}


def _v2_updates_part(node_name, node_data):
    """Create a v2 stream part with type='updates'."""
    return {"type": "updates", "data": {node_name: node_data}}


class ConcreteLangGraphAgent(LangGraphAgent):
    """Minimal concrete subclass for testing."""

    def __init__(self, graph=None, **kwargs):
        # Bypass __init__ to avoid calling abstract methods during construction.
        # We set attributes directly for test control.
        self._cost_tracking_enabled = False
        self._cost_logger = None
        self._sub_agent_id = None
        self._checkpointer = MagicMock()
        self._model = MagicMock()
        self._mcp_tools = []  # already "loaded"
        self._mcp_tools_lock = False
        self._graph = graph
        self._mcp_client = None
        self.tool_query_regex = None
        self.recursion_limit = 50  # Add recursion_limit attribute

    def _create_model(self):
        return self._model

    def _create_checkpointer(self):
        return self._checkpointer

    async def _get_mcp_connections(self):
        return {}

    def _get_system_prompt(self):
        return "test prompt"

    def _get_checkpoint_namespace(self):
        return "test-ns"

    async def close(self):
        pass


async def _collect(agent, query="hi", user_config=None, task=None):
    """Collect all AgentStreamResponse objects from _stream_impl."""
    uc = user_config or _make_user_config()
    t = task or _make_task()
    results = []
    async for resp in agent._stream_impl(query, uc, t):
        results.append(resp)
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamImplGraphNone:
    """Graph is None → yields failed."""

    @pytest.mark.asyncio
    async def test_graph_none_yields_failed(self):
        agent = ConcreteLangGraphAgent(graph=None)
        # Prevent _ensure_mcp_tools_loaded from overwriting _graph
        agent._ensure_mcp_tools_loaded = AsyncMock()
        responses = await _collect(agent)
        assert len(responses) == 1
        assert responses[0].state == TaskState.failed
        assert "failed to initialize" in responses[0].content


class TestStreamImplRegularTextStreaming:
    """Token-level text streaming via StreamBuffer + extract_text_from_content."""

    @pytest.mark.asyncio
    async def test_simple_string_content(self):
        """Regular string tokens are buffered and flushed."""
        chunk1 = AIMessageChunk(content="Hello world, this is a long enough sentence to flush. ")
        chunk2 = AIMessageChunk(content="And more content.")

        final_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "FinalResponseSchema", "args": {"task_state": "completed", "message": "Done"}}
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_messages_part(chunk1)
            yield _v2_messages_part(chunk2)
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        streaming = [r for r in responses if r.metadata and r.metadata.get("streaming_chunk")]
        final = [r for r in responses if not (r.metadata and r.metadata.get("streaming_chunk"))]

        assert len(streaming) >= 1
        assert len(final) == 1
        assert final[0].state == TaskState.completed
        assert final[0].content == "Done"

    @pytest.mark.asyncio
    async def test_list_content_blocks(self):
        """Content as list of blocks (Bedrock format) is extracted correctly."""
        content_blocks = [
            {"type": "text", "text": "A" * 50},
        ]
        chunk = AIMessageChunk(content=content_blocks)

        final_msg = AIMessage(
            content="done",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "FinalResponseSchema",
                    "args": {"task_state": "completed", "message": "Result"},
                }
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_messages_part(chunk)
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        all_content = "".join(r.content for r in responses if r.metadata and r.metadata.get("streaming_chunk"))
        assert "A" * 50 in all_content


class TestStreamImplFinalResponseSchema:
    """FinalResponseSchema extraction from updates and incremental streaming."""

    @pytest.mark.asyncio
    async def test_final_response_from_updates(self):
        """FinalResponseSchema in updates sets task_state and message."""
        final_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "FinalResponseSchema",
                    "args": {"task_state": "failed", "message": "Something went wrong"},
                }
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        final = responses[-1]
        assert final.state == TaskState.failed
        assert final.content == "Something went wrong"

    @pytest.mark.asyncio
    async def test_incremental_schema_streaming(self):
        """tool_call_chunks are processed by StructuredResponseStreamer for incremental streaming."""
        # Simulate tool_call_chunks arriving incrementally
        tc_chunk_1 = AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": "FinalResponseSchema", "index": 0, "args": '{"message": "Hello'}],
        )
        tc_chunk_2 = AIMessageChunk(
            content="",
            tool_call_chunks=[{"index": 0, "args": ' world, this is a test of incremental streaming"}'}],
        )

        final_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "FinalResponseSchema",
                    "args": {
                        "task_state": "completed",
                        "message": "Hello world, this is a test of incremental streaming",
                    },
                }
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_messages_part(tc_chunk_1)
            yield _v2_messages_part(tc_chunk_2)
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        streaming = [r for r in responses if r.metadata and r.metadata.get("streaming_chunk")]
        # Content should have been streamed incrementally
        streamed_text = "".join(r.content for r in streaming)
        assert "Hello world" in streamed_text

    @pytest.mark.asyncio
    async def test_input_required_state(self):
        final_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "FinalResponseSchema",
                    "args": {"task_state": "input_required", "message": "Need more info"},
                }
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        final = responses[-1]
        assert final.state == TaskState.input_required
        assert final.content == "Need more info"

    @pytest.mark.asyncio
    async def test_fallback_to_final_state(self):
        """If FinalResponseSchema not captured during stream, extract from final state."""
        # No FinalResponseSchema during stream, but present in final state
        regular_msg = AIMessage(content="Some thinking...")

        final_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "FinalResponseSchema",
                    "args": {"task_state": "completed", "message": "Extracted from state"},
                }
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [regular_msg, final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            # Only yield a regular message, no FinalResponseSchema in stream
            yield _v2_updates_part("model", {"messages": [regular_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        final = responses[-1]
        assert final.state == TaskState.completed
        assert final.content == "Extracted from state"


class TestStreamImplTodos:
    """TodoItem extraction from updates."""

    @pytest.mark.asyncio
    async def test_todo_snapshot(self):
        """Todo updates yield work_plan metadata."""
        todo_data = {
            "todos": [
                {"content": "Step 1", "status": "completed"},
                {"content": "Step 2", "status": "in_progress"},
                {"content": "Step 3", "status": "pending"},
            ]
        }

        final_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "FinalResponseSchema", "args": {"task_state": "completed", "message": "Done"}}
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_updates_part("tools", todo_data)
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        work_plan = [r for r in responses if r.metadata and r.metadata.get("work_plan")]
        assert len(work_plan) == 1

        todos = work_plan[0].metadata["todos"]
        assert len(todos) == 3
        assert todos[0].name == "Step 1"
        assert todos[0].state == "completed"
        assert todos[1].state == "working"
        assert todos[2].state == "submitted"


class TestStreamImplInterrupts:
    """Interrupt handling."""

    @pytest.mark.asyncio
    async def test_interrupt_yields_input_required(self):
        graph = MagicMock()
        state = MagicMock()
        state.interrupts = [MagicMock()]  # Non-empty → interrupt
        state.values = {}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            return
            yield  # make it an async generator

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        assert len(responses) == 1
        assert responses[0].state == TaskState.input_required
        assert "interrupted" in responses[0].content.lower()


class TestStreamImplContentFallback:
    """When no FinalResponseSchema exists, fall back to accumulated content."""

    @pytest.mark.asyncio
    async def test_fallback_to_accumulated_content(self):
        msg = AIMessage(content="Here is the answer.")

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_updates_part("model", {"messages": [msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        final = responses[-1]
        assert final.state == TaskState.completed
        assert final.content == "Here is the answer."

    @pytest.mark.asyncio
    async def test_no_content_at_all(self):
        """Completely empty stream → default message."""
        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": []}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            return
            yield

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        final = responses[-1]
        assert final.state == TaskState.completed
        assert "processed successfully" in final.content.lower()


class TestStreamImplErrorHandling:
    """Error paths."""

    @pytest.mark.asyncio
    async def test_exception_yields_failed(self):
        graph = MagicMock()

        async def failing_astream(*a, **kw):
            raise RuntimeError("boom")
            yield  # make it an async generator

        graph.astream = failing_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        assert len(responses) == 1
        assert responses[0].state == TaskState.failed
        assert "boom" in responses[0].content

    @pytest.mark.asyncio
    async def test_schema_suppresses_raw_json_content(self):
        """AIMessage with FinalResponseSchema should NOT have its .content accumulated."""
        msg_with_schema = AIMessage(
            content='{"task_state": "completed", "message": "Raw JSON"}',
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "FinalResponseSchema",
                    "args": {"task_state": "completed", "message": "Clean result"},
                }
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [msg_with_schema]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_updates_part("model", {"messages": [msg_with_schema]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        final = responses[-1]
        assert final.content == "Clean result"
        # Raw JSON should NOT appear in any response
        assert '{"task_state"' not in "".join(r.content for r in responses)


class TestStreamImplBufferFlush:
    """Verify buffer flushing behavior at end of stream."""

    @pytest.mark.asyncio
    async def test_remaining_buffer_flushed(self):
        """Short text that doesn't hit chunk_min is flushed at end of stream."""
        chunk = AIMessageChunk(content="short")  # Below 40 char threshold

        final_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "FinalResponseSchema", "args": {"task_state": "completed", "message": "Done"}}
            ],
        )

        graph = MagicMock()
        state = MagicMock()
        state.interrupts = []
        state.values = {"messages": [final_msg]}
        graph.get_state.return_value = state

        async def fake_astream(*a, **kw):
            yield _v2_messages_part(chunk)
            yield _v2_updates_part("model", {"messages": [final_msg]})

        graph.astream = fake_astream
        graph.with_config = MagicMock(return_value=graph)  # Mock with_config to return self

        agent = ConcreteLangGraphAgent(graph=graph)
        responses = await _collect(agent)

        streaming = [r for r in responses if r.metadata and r.metadata.get("streaming_chunk")]
        # "short" should be flushed in the remaining buffer flush
        assert any("short" in r.content for r in streaming)
