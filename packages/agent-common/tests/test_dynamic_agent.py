"""Unit tests for DynamicLocalAgentRunnable and LocalSubAgentConfig."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from a2a.types import TaskState
from langchain_core.messages import HumanMessage
from langchain_core.tools import Tool

from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from agent_common.a2a.stream_events import ErrorEvent, TaskUpdate
from agent_common.a2a.structured_response import SubAgentResponseSchema
from agent_common.agents.dynamic_agent import (
    DynamicLocalAgentRunnable,
    create_dynamic_local_subagent,
)


class TestDynamicLocalAgentRunnable:
    """Tests for DynamicLocalAgentRunnable."""

    @pytest.fixture
    def mock_model(self):
        """Create a mock LangChain model."""
        return MagicMock()

    @pytest.fixture
    def basic_config(self):
        """Create a basic config without MCP URL."""
        return LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="test-agent",
            description="A test agent for unit testing",
            system_prompt="You are a helpful test assistant.",
        )

    @pytest.fixture
    def mcp_config(self):
        """Create a config with MCP tools."""
        return LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="mcp-agent",
            description="Agent with MCP tools",
            system_prompt="You are an expert with tools.",
            mcp_tools=["tool1", "tool2"],
        )

    def test_name_property(self, basic_config, mock_model):
        """Test that name property returns config name."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)
        assert runnable.name == "test-agent"

    def test_description_property(self, basic_config, mock_model):
        """Test that description property returns config description."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)
        assert runnable.description == "A test agent for unit testing"

    def test_initial_state(self, basic_config, mock_model):
        """Test that agent is not created on initialization."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)
        assert runnable._agent is None
        assert runnable._discovered_tools is None

    def test_inherits_orchestrator_tools(self, basic_config, mock_model):
        """Test that no tool is inherited when no MCP tools specified."""

        # Use a proper Tool with actual function
        def test_func(x: str) -> str:
            return f"Result: {x}"

        mock_tool = Tool(name="test_tool", description="A test tool", func=test_func)
        orchestrator_tools = [mock_tool]

        runnable = DynamicLocalAgentRunnable(
            config=basic_config,
            model=mock_model,
            orchestrator_tools=orchestrator_tools,
        )

        # Tools should be from orchestrator since no MCP tools specified
        effective_tools = runnable._get_effective_tools()
        assert len(effective_tools) == 0

    @pytest.mark.asyncio
    async def test_lazy_agent_creation(self, basic_config, mock_model):
        """Test that agent is created lazily on first invocation."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Agent should not exist yet
        assert runnable._agent is None

        # Mock create_agent to return a mock graph
        mock_graph = AsyncMock()
        mock_graph.with_config = MagicMock(return_value=mock_graph)
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Test response")],
            }
        )

        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph):
            await runnable._ensure_agent()

        # Agent should now exist
        assert runnable._agent is not None

    @pytest.mark.asyncio
    async def test_process_returns_success_response(self, basic_config, mock_model):
        """Test that _astream_impl yields completed TaskUpdate with correct A2A state."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock graph.astream to yield no parts (fast path to retrieve_final_state)
        mock_graph = AsyncMock()
        mock_graph.with_config = MagicMock(return_value=mock_graph)

        async def empty_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        mock_graph.astream = empty_stream
        mock_state = MagicMock()
        mock_state.interrupts = []
        mock_graph.aget_state = AsyncMock(return_value=mock_state)

        # Mock retrieve_final_state with structured response
        final_state = {
            "messages": [MagicMock(content="Task completed successfully.")],
            "structured_response": SubAgentResponseSchema(
                task_state="completed",
                message="Task completed successfully.",
            ),
        }

        with (
            patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph),
            patch("agent_common.agents.dynamic_agent.retrieve_final_state", return_value=final_state),
        ):
            events = [
                event
                async for event in runnable._astream_impl(
                    input_data=SubAgentInput(
                        a2a_tracking={}, messages=[HumanMessage(content="Please complete the task.")]
                    ),
                    config={"configurable": {"thread_id": "test", "checkpoint_ns": ""}},
                )
            ]

        # Find terminal TaskUpdate
        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        result = terminal.data
        assert result.state == TaskState.TASK_STATE_COMPLETED
        assert result.is_complete is True
        assert result.requires_input is False

    @pytest.mark.asyncio
    async def test_process_returns_input_required_response(self, basic_config, mock_model):
        """Test that _astream_impl yields input_required state via structured output."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        mock_graph = AsyncMock()
        mock_graph.with_config = MagicMock(return_value=mock_graph)

        async def empty_stream(*args, **kwargs):
            return
            yield

        mock_graph.astream = empty_stream
        mock_state = MagicMock()
        mock_state.interrupts = []
        mock_graph.aget_state = AsyncMock(return_value=mock_state)

        final_state = {
            "messages": [MagicMock(content="What is the project name?")],
            "structured_response": SubAgentResponseSchema(
                task_state="input_required",
                message="What is the project name?",
            ),
        }

        with (
            patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph),
            patch("agent_common.agents.dynamic_agent.retrieve_final_state", return_value=final_state),
        ):
            events = [
                event
                async for event in runnable._astream_impl(
                    input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Create a ticket")]),
                    config={"configurable": {"thread_id": "test", "checkpoint_ns": ""}},
                )
            ]

        terminal = [e for e in events if isinstance(e, TaskUpdate)][-1]
        result = terminal.data
        assert result.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert result.is_complete is False
        assert result.requires_input is True

    @pytest.mark.asyncio
    async def test_process_returns_failed_on_error(self, basic_config, mock_model):
        """Test that _astream_impl yields ErrorEvent on exception."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock build_sub_agent_graph to raise an exception
        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", side_effect=Exception("Model error")):
            events = [
                event
                async for event in runnable._astream_impl(
                    input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Do something")]),
                    config={"configurable": {"thread_id": "test", "checkpoint_ns": ""}},
                )
            ]

        # Should yield an ErrorEvent
        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert "Model error" in events[0].error

    @pytest.mark.asyncio
    async def test_mcp_tools_config(self, mcp_config, mock_model):
        """Test that agent can be configured with MCP tool names."""
        runnable = DynamicLocalAgentRunnable(
            config=mcp_config,
            model=mock_model,
            orchestrator_tools=[],
        )

        # Config should have MCP tools specified
        assert runnable.config.mcp_tools == ["tool1", "tool2"]
        assert runnable.config.name == "mcp-agent"

    @pytest.mark.asyncio
    async def test_process_with_bedrock_tool_call(self, basic_config, mock_model):
        """Test that _astream_impl handles Bedrock-style tool call responses."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        mock_graph = AsyncMock()
        mock_graph.with_config = MagicMock(return_value=mock_graph)

        async def empty_stream(*args, **kwargs):
            return
            yield

        mock_graph.astream = empty_stream
        mock_state = MagicMock()
        mock_state.interrupts = []
        mock_graph.aget_state = AsyncMock(return_value=mock_state)

        # Bedrock-style: SubAgentResponseSchema in tool_calls, no structured_response key
        mock_message = MagicMock()
        mock_message.tool_calls = [
            {
                "name": "SubAgentResponseSchema",
                "args": {"task_state": "failed", "message": "Could not complete the task."},
            }
        ]

        final_state = {
            "messages": [mock_message],
        }

        with (
            patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph),
            patch("agent_common.agents.dynamic_agent.retrieve_final_state", return_value=final_state),
        ):
            events = [
                event
                async for event in runnable._astream_impl(
                    input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Do something")]),
                    config={"configurable": {"thread_id": "test", "checkpoint_ns": ""}},
                )
            ]

        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        result = terminal.data
        assert result.state == TaskState.TASK_STATE_FAILED
        assert result.is_complete is True

    @pytest.mark.asyncio
    async def test_process_fallback_when_no_structured_response(self, basic_config, mock_model):
        """Test fallback to completed state when no structured response is found."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        mock_graph = AsyncMock()
        mock_graph.with_config = MagicMock(return_value=mock_graph)

        async def empty_stream(*args, **kwargs):
            return
            yield

        mock_graph.astream = empty_stream
        mock_state = MagicMock()
        mock_state.interrupts = []
        mock_graph.aget_state = AsyncMock(return_value=mock_state)

        # No structured_response key → _translate_agent_result falls back to completed
        final_state = {
            "messages": [MagicMock(content="Some response without structured output")],
        }

        with (
            patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph),
            patch("agent_common.agents.dynamic_agent.retrieve_final_state", return_value=final_state),
        ):
            events = [
                event
                async for event in runnable._astream_impl(
                    input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Do something")]),
                    config={"configurable": {"thread_id": "test", "checkpoint_ns": ""}},
                )
            ]

        terminal = next(e for e in events if isinstance(e, TaskUpdate) and e.data.is_complete)
        result = terminal.data
        assert result.state == TaskState.TASK_STATE_COMPLETED
        assert result.is_complete is True


class TestCreateDynamicLocalSubagent:
    """Tests for create_dynamic_local_subagent factory function."""

    def test_creates_compiled_subagent(self):
        """Test that factory creates a proper CompiledSubAgent."""
        config = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="factory-test",
            description="Test from factory",
            system_prompt="You are a test.",
        )
        mock_model = MagicMock()

        subagent = create_dynamic_local_subagent(config, mock_model)

        assert subagent["name"] == "factory-test"
        assert subagent["description"] == "Test from factory"
        assert "runnable" in subagent
        assert isinstance(subagent["runnable"], DynamicLocalAgentRunnable)

    def test_passes_orchestrator_tools(self):
        """Test that orchestrator tools are passed to runnable."""
        config = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="tools-test",
            description="Test with tools",
            system_prompt="You are a test.",
        )
        mock_model = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        subagent = create_dynamic_local_subagent(
            config,
            mock_model,
            orchestrator_tools=[mock_tool],
        )

        runnable = subagent["runnable"]
        assert len(runnable.orchestrator_tools) == 1
        assert runnable.orchestrator_tools[0].name == "test_tool"


class TestAttachmentMounting:
    """Tests for the per-invocation /attachments/ backend helpers."""

    @pytest.fixture
    def runnable(self):
        config = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="attach-agent",
            description="Agent under attachment test",
            system_prompt="You are a test.",
        )
        return DynamicLocalAgentRunnable(config=config, model=MagicMock())

    def test_no_attachments_returns_none(self, runnable):
        input_data = SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="just text, no files")])
        assert runnable._build_attachments_backend(input_data) is None

    def test_extracts_file_block_with_url(self, runnable):
        content = [
            {"type": "text", "text": "Please summarize"},
            {
                "type": "file",
                "url": "https://s3.example/bucket/report.pdf?sig=abc",
                "mime_type": "application/pdf",
            },
        ]
        input_data = SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content=content)])
        backend = runnable._build_attachments_backend(input_data)
        assert backend is not None
        assert "report.pdf" in backend._attachments

    def test_extracts_inline_base64_block(self, runnable):
        import base64

        b64 = base64.b64encode(b"hello").decode("ascii")
        content = [
            {"type": "image", "base64": b64, "mime_type": "image/png", "filename": "pic.png"},
        ]
        input_data = SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content=content)])
        backend = runnable._build_attachments_backend(input_data)
        assert backend is not None
        assert backend._attachments["pic.png"].inline_bytes == b"hello"

    def test_skips_block_without_source(self, runnable):
        content = [{"type": "file", "mime_type": "application/pdf"}]
        input_data = SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content=content)])
        assert runnable._build_attachments_backend(input_data) is None

    def test_derive_filename_from_url(self, runnable):
        name = runnable._derive_attachment_filename(
            {}, "https://s3.example/path/My%20Doc.pdf?x=1", "application/pdf", 0, set()
        )
        assert name == "My Doc.pdf"

    def test_derive_filename_fallback_uses_mime_extension(self, runnable):
        name = runnable._derive_attachment_filename({}, None, "application/pdf", 2, set())
        assert name.startswith("attachment_2")
        assert name.endswith(".pdf")

    def test_derive_filename_dedupes(self, runnable):
        used = {"report.pdf"}
        name = runnable._derive_attachment_filename({}, "https://s3.example/report.pdf", "application/pdf", 3, used)
        assert name != "report.pdf"
        assert name.endswith(".pdf")

    def test_compose_adds_attachments_route_to_composite(self, runnable):
        from deepagents.backends import StateBackend
        from deepagents.backends.composite import CompositeBackend

        from agent_common.backends.attachments_store import Attachment, AttachmentsStoreBackend

        att_backend = AttachmentsStoreBackend([Attachment(filename="a.txt", inline_bytes=b"x")])
        base = CompositeBackend(default=StateBackend(), routes={"/skills/": StateBackend()})
        composed = runnable._compose_backend_with_attachments(base, att_backend)
        assert isinstance(composed, CompositeBackend)
        assert "/attachments/" in composed.routes
        assert "/skills/" in composed.routes

    def test_compose_returns_base_when_no_attachments(self, runnable):
        from deepagents.backends import StateBackend

        base = StateBackend()
        assert runnable._compose_backend_with_attachments(base, None) is base

    def test_compose_wraps_non_composite_base(self, runnable):
        from deepagents.backends import StateBackend
        from deepagents.backends.composite import CompositeBackend

        from agent_common.backends.attachments_store import Attachment, AttachmentsStoreBackend

        att_backend = AttachmentsStoreBackend([Attachment(filename="a.txt", inline_bytes=b"x")])
        composed = runnable._compose_backend_with_attachments(StateBackend(), att_backend)
        assert isinstance(composed, CompositeBackend)
        assert "/attachments/" in composed.routes


def _http_error(status: int) -> Exception:
    request = httpx.Request("POST", "https://gateway.example/mcp")
    response = httpx.Response(status, request=request, text="boom")
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


class TestDiscoverMcpTools:
    """Tests for DynamicLocalAgentRunnable._discover_mcp_tools' error handling:
    graceful degradation on gateway/transport errors, no degradation for
    token-exchange (auth) failures, and stale-error cleanup on retry."""

    @pytest.fixture
    def mock_model(self):
        return MagicMock()

    @pytest.fixture
    def gateway_config(self):
        return LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="gateway-agent",
            description="Agent with gateway MCP tools",
            system_prompt="You are an expert with tools.",
            mcp_tools=["some_gateway_tool"],
        )

    @pytest.fixture
    def runnable(self, gateway_config, mock_model):
        oauth2_client = MagicMock()
        oauth2_client.exchange_token = AsyncMock(return_value="gateway-token")
        return DynamicLocalAgentRunnable(
            config=gateway_config,
            model=mock_model,
            oauth2_client=oauth2_client,
            user_token="user-token",
            mcp_gateway_url="https://gateway.example/mcp",
            mcp_gateway_client_id="gatana",
        )

    @pytest.mark.asyncio
    async def test_non_mcp_error_is_not_degraded(self, runnable):
        """A bug in our own code (not an httpx/gateway error) raised from
        inside the discovery try block must propagate, not be silently
        reported to the user as "temporarily unavailable" forever."""
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            mock_client_cls.return_value.get_tools = AsyncMock(side_effect=ValueError("schema bug"))

            with pytest.raises(ValueError, match="schema bug"):
                await runnable._discover_mcp_tools()

        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_non_retryable_gateway_error_degrades_to_empty_list(self, runnable):
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            mock_client_cls.return_value.get_tools = AsyncMock(
                side_effect=ExceptionGroup("boom", [_http_error(400)])
            )
            tools = await runnable._discover_mcp_tools()

        assert tools == []
        assert runnable._mcp_discovery_error is not None
        assert "400" in runnable._mcp_discovery_error or "MCP" in runnable._mcp_discovery_error

    @pytest.mark.asyncio
    async def test_nested_exception_group_also_degrades(self, runnable):
        # The exact shape the pre-fix single-level unwrap missed.
        nested = ExceptionGroup("outer", [ExceptionGroup("inner", [_http_error(403)])])
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            mock_client_cls.return_value.get_tools = AsyncMock(side_effect=nested)
            tools = await runnable._discover_mcp_tools()

        assert tools == []
        assert runnable._mcp_discovery_error is not None

    @pytest.mark.asyncio
    async def test_token_exchange_failure_is_not_degraded(self, runnable):
        """An auth failure exchanging the user's own token is a different
        failure class from the gateway being down — it must propagate, not
        be swallowed into the same empty-list degrade path."""
        runnable.oauth2_client.exchange_token = AsyncMock(side_effect=RuntimeError("token exchange failed"))

        with pytest.raises(RuntimeError, match="token exchange failed"):
            await runnable._discover_mcp_tools()

        # Never reached the degrade path, so no warning was recorded.
        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_retryable_token_exchange_error_is_retried_then_raised(self, runnable):
        """A transient OIDC hiccup exchanging the user's token gets the same
        retry-with-backoff treatment as a transient gateway error — it must
        not raise on attempt 1 just because it's an auth failure. Once
        retries are exhausted it still raises (not degrades): this is a
        different failure class from the gateway being down."""
        transient_error = _http_error(503)
        runnable.oauth2_client.exchange_token = AsyncMock(side_effect=transient_error)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            with pytest.raises(httpx.HTTPStatusError):
                await runnable._discover_mcp_tools()

        assert runnable.oauth2_client.exchange_token.await_count == 3  # max_retries
        assert mock_sleep.await_count == 2  # slept between attempts, not after the last
        # Never reached the degrade path, so no warning was recorded.
        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_stale_discovery_error_cleared_on_next_call(self, runnable):
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            mock_client_cls.return_value.get_tools = AsyncMock(
                side_effect=ExceptionGroup("boom", [_http_error(400)])
            )
            await runnable._discover_mcp_tools()
        assert runnable._mcp_discovery_error is not None

        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            mock_client_cls.return_value.get_tools = AsyncMock(return_value=[])
            await runnable._discover_mcp_tools()
        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_ensure_agent_retries_discovery_after_degraded_call(self, runnable):
        """A degraded _ensure_agent() call must not permanently pin the instance
        at zero MCP tools: the next call should retry discovery, not treat the
        degraded state as resolved."""
        mock_graph = MagicMock()

        def fake_tool(name):
            return Tool(name=name, description="A fake gateway tool", func=lambda x: x)

        with (
            patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph),
            patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls,
        ):
            mock_client_cls.return_value.get_tools = AsyncMock(
                side_effect=ExceptionGroup("boom", [_http_error(400)])
            )
            await runnable._ensure_agent()
            assert runnable._mcp_discovery_error is not None
            assert runnable._discovered_tools is None  # not cached as "[]"

            # Second call must retry discovery (not short-circuit on the
            # _cached_tools guard) and, on success, actually resolve.
            mock_client_cls.return_value.get_tools = AsyncMock(return_value=[fake_tool("some_gateway_tool")])
            await runnable._ensure_agent()

        assert runnable._mcp_discovery_error is None
        assert [t.name for t in runnable._discovered_tools] == ["some_gateway_tool"]
        assert "tool_availability_warning" not in runnable._cached_system_prompt

    def test_tool_availability_addendum_empty_when_no_error(self, runnable):
        assert runnable._build_tool_availability_addendum() == ""

    def test_tool_availability_addendum_is_fixed_and_url_free(self, runnable):
        runnable._mcp_discovery_error = "MCP server returned HTTP 403 for https://internal-gateway.example/mcp"
        addendum = runnable._build_tool_availability_addendum()

        assert "tool_availability_warning" in addendum
        assert "temporarily unavailable" in addendum
        # The raw, gateway-controlled error text (and any URL in it) must never
        # be interpolated into the prompt — see the addendum's own docstring.
        assert "internal-gateway.example" not in addendum
        assert "403" not in addendum
