"""Unit tests for DynamicLocalAgentRunnable and LocalSubAgentConfig."""

from types import SimpleNamespace
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


def _install_list_tools(mock_client_cls, **list_tools_kwargs):
    """Make the patched MultiServerMCPClient's ``session(name)`` yield a session whose
    ``list_tools`` behaves per ``list_tools_kwargs`` (AsyncMock kwargs). A ``return_value``
    given as a list of LangChain tools is converted into an MCP ``ListToolsResult``."""
    from contextlib import asynccontextmanager
    from mcp.types import ListToolsResult, Tool as MCPTool

    rv = list_tools_kwargs.get("return_value")
    if isinstance(rv, list):
        list_tools_kwargs["return_value"] = ListToolsResult(
            tools=[
                MCPTool(name=t.name, description=t.description, inputSchema={"type": "object", "properties": {}})
                for t in rv
            ],
            nextCursor=None,
        )
    client = mock_client_cls.return_value
    client.callbacks = None
    client.tool_interceptors = []

    def session(name):
        @asynccontextmanager
        async def _cm():
            sess = MagicMock()
            sess.list_tools = AsyncMock(**list_tools_kwargs)
            yield sess

        return _cm()

    client.session = session




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

    # --- Embedded Nannos: client-action gate (Phase 7 step 3) ---

    def test_client_action_disabled_by_default(self, basic_config, mock_model):
        """Ordinary sub-agents don't get the embedded machinery."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)
        assert runnable.client_action_enabled is False

    def test_client_action_enabled_flag_read(self, mock_model):
        """The embedded entrypoint config turns the capability on."""
        cfg = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="cockpit",
            description="Embedded cockpit agent",
            system_prompt="You are the cockpit assistant.",
            client_action_enabled=True,
        )
        runnable = DynamicLocalAgentRunnable(config=cfg, model=mock_model)
        assert runnable.client_action_enabled is True

    def _prime_cached_state(self, runnable):
        runnable._cached_tools = []
        runnable._cached_system_prompt = "p"
        runnable._cached_response_format = None
        runnable._cached_hitl_guarded = None
        runnable._cached_context_gated_tools = None

    def test_client_objects_middleware_attached_when_enabled(self, mock_model):
        """When enabled, _build_graph attaches ClientObjectsMiddleware."""
        from agent_common.middleware.client_objects_middleware import ClientObjectsMiddleware

        cfg = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="cockpit",
            description="Embedded cockpit agent",
            system_prompt="You are the cockpit assistant.",
            client_action_enabled=True,
        )
        runnable = DynamicLocalAgentRunnable(config=cfg, model=mock_model)
        self._prime_cached_state(runnable)
        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph") as mock_build:
            runnable._build_graph()
        mws = mock_build.call_args.kwargs.get("extra_middlewares") or []
        assert any(isinstance(mw, ClientObjectsMiddleware) for mw in mws)

    def test_client_objects_middleware_absent_when_disabled(self, basic_config, mock_model):
        """Disabled (default) sub-agents get no ClientObjectsMiddleware."""
        from agent_common.middleware.client_objects_middleware import ClientObjectsMiddleware

        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)
        self._prime_cached_state(runnable)
        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph") as mock_build:
            runnable._build_graph()
        mws = mock_build.call_args.kwargs.get("extra_middlewares") or []
        assert not any(isinstance(mw, ClientObjectsMiddleware) for mw in mws)
        assert runnable._discovered_tools is None

    def test_client_action_meta_round_trips(self):
        """The client_action directive survives the shared stream-event contract that
        _astream_impl uses to forward it to the execute-only adapter."""
        from agent_common.a2a.stream_events import ClientActionMeta, TaskUpdate, parse_event_metadata

        directive = {"kind": "apply", "payload": {"name": "Spring sale"}}
        meta = parse_event_metadata({"client_action": directive})
        assert isinstance(meta, ClientActionMeta)
        assert meta.client_action == directive
        # And it is accepted on a TaskUpdate's event_metadata union.
        assert TaskUpdate(event_metadata=meta).event_metadata is meta

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
            _install_list_tools(mock_client_cls, side_effect=ValueError("schema bug"))

            with pytest.raises(ValueError, match="schema bug"):
                await runnable._discover_mcp_tools()

        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_non_retryable_gateway_error_degrades_to_empty_list(self, runnable):
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(mock_client_cls, 
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
            _install_list_tools(mock_client_cls, side_effect=nested)
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
            _install_list_tools(mock_client_cls, 
                side_effect=ExceptionGroup("boom", [_http_error(400)])
            )
            await runnable._discover_mcp_tools()
        assert runnable._mcp_discovery_error is not None

        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(mock_client_cls, return_value=[])
            await runnable._discover_mcp_tools()
        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_discovery_error_survives_a_later_raising_call(self, runnable):
        """A degraded call followed by a *raising* call (auth failure, or any
        non-transport error) must NOT clear the error — only an actual
        successful resolution should. Otherwise _ensure_agent's guard would
        see a cleared error next to still-cached degraded tools and wrongly
        treat the instance as resolved, permanently skipping the retry it's
        meant to allow (this is the within-turn "stale pin" the reset-at-entry
        version of this method was vulnerable to)."""
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(mock_client_cls, 
                side_effect=ExceptionGroup("boom", [_http_error(400)])
            )
            await runnable._discover_mcp_tools()
        assert runnable._mcp_discovery_error is not None

        runnable.oauth2_client.exchange_token = AsyncMock(side_effect=RuntimeError("token exchange failed"))
        with pytest.raises(RuntimeError, match="token exchange failed"):
            await runnable._discover_mcp_tools()

        # The raise must not have cleared the still-outstanding degrade.
        assert runnable._mcp_discovery_error is not None

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
            _install_list_tools(mock_client_cls, 
                side_effect=ExceptionGroup("boom", [_http_error(400)])
            )
            await runnable._ensure_agent()
            assert runnable._mcp_discovery_error is not None
            assert runnable._discovered_tools is None  # not cached as "[]"

            # Second call must retry discovery (not short-circuit on the
            # _cached_tools guard) and, on success, actually resolve.
            _install_list_tools(mock_client_cls, return_value=[fake_tool("some_gateway_tool")])
            await runnable._ensure_agent()

        assert runnable._mcp_discovery_error is None
        assert [t.name for t in runnable._discovered_tools] == ["some_gateway_tool"]
        assert "tool_availability_warning" not in runnable._cached_system_prompt

    @pytest.mark.asyncio
    async def test_names_not_pre_resolved_are_listed_on_the_agents_own_connection(self, runnable):
        """A name the orchestrator did not hand over is resolved by a tools/list on this agent's
        connection with this user's token — a listing is a per-user view, never reused."""
        from agent_common.core.tool_catalogue import LazyMcpTool

        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(
                mock_client_cls,
                return_value=[Tool(name="some_gateway_tool", description="fetched", func=lambda x: x)],
            )
            tools = await runnable._discover_mcp_tools()
        assert [t.name for t in tools] == ["some_gateway_tool"]
        assert isinstance(tools[0], LazyMcpTool) and tools[0].catalogue_entry.card.description == "fetched"
        assert tools[0]._connection["headers"]["Authorization"] == "Bearer gateway-token"
        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_listed_tools_bind_to_the_connection_by_key_not_position(self, gateway_config, mock_model):
        """Console names are listed on and bound to 'console'; gateway names to the connection keyed by
        mcp_gateway_client_id. Dict order must not matter (regression for a positional gateway[0] pick)."""
        config = LocalLangGraphSubAgentConfig(
            type="langgraph", name="g", description="x", system_prompt="x", mcp_tools=["some_gateway_tool", "console_create_skill"]
        )
        oauth2_client = MagicMock()
        oauth2_client.exchange_token = AsyncMock(side_effect=lambda **kw: f"tok-{kw['target_client_id']}")
        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=mock_model,
            oauth2_client=oauth2_client,
            user_token="user-token",
            mcp_gateway_url="https://gateway.example/mcp",
            mcp_gateway_client_id="gatana",
        )
        runnable.console_backend_mcp_url = "https://console.example/mcp"
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(
                mock_client_cls,
                return_value=[
                    Tool(name="some_gateway_tool", description="", func=lambda x: x),
                    Tool(name="console_create_skill", description="", func=lambda x: x),
                ],
            )
            tools = {t.name: t for t in await runnable._discover_mcp_tools()}

        assert set(tools) == {"some_gateway_tool", "console_create_skill"}
        assert tools["some_gateway_tool"]._connection["headers"]["Authorization"] == "Bearer tok-gatana"
        assert tools["console_create_skill"]._connection["headers"]["Authorization"] == "Bearer tok-agent-console"

    @pytest.mark.asyncio
    async def test_with_a_token_provider_tools_are_token_free_and_mint_per_call(self, gateway_config, mock_model):
        """Given the orchestrator's UserTokenProvider, the sub-agent exchanges through it and the
        tools it discovers itself carry no bearer — an interceptor mints one per call."""
        from agent_common.core.token_provider import UserTokenProvider
        exchanges: list[str] = []

        import base64
        import json
        import time

        def _jwt(aud: str) -> str:
            seg = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()  # noqa: E731
            return f"{seg({'alg': 'none'})}.{seg({'exp': time.time() + 900, 'aud': aud})}.sig"

        minted: dict[str, str] = {}

        async def exchange(*, subject_token, target_client_id, requested_scopes):
            exchanges.append(target_client_id)
            return minted.setdefault(target_client_id, _jwt(target_client_id))

        provider = UserTokenProvider("user-token", exchange)
        oauth2_client = MagicMock()
        oauth2_client.exchange_token = AsyncMock(side_effect=AssertionError("must go through the provider"))
        runnable = DynamicLocalAgentRunnable(
            config=gateway_config,
            model=mock_model,
            oauth2_client=oauth2_client,
            user_token="user-token",
            mcp_gateway_url="https://gateway.example/mcp",
            mcp_gateway_client_id="gatana",
            token_provider=provider,
        )
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(mock_client_cls, return_value=[Tool(name="some_gateway_tool", description="d", func=lambda x: x)])
            (tool,) = await runnable._discover_mcp_tools()
            listing_connection = mock_client_cls.call_args.kwargs["connections"]["gatana"]

        assert exchanges == ["gatana"], "one exchange, via the provider"
        assert listing_connection["headers"] == {"Authorization": f"Bearer {minted['gatana']}"}, "listing used the bearer"
        assert not (tool._connection.get("headers") or {}), "the tool's connection carries no credential"
        assert tool._interceptors and len(tool._interceptors) == 1
        seen = {}

        class Req(SimpleNamespace):
            def override(self, **kw):
                return Req(**{**self.__dict__, **kw})

        async def handler(req):
            seen.update(req.headers)
            return "ok"

        await tool._interceptors[0](Req(server_name="gatana", headers=None), handler)
        assert seen == {"Authorization": f"Bearer {minted['gatana']}"} and exchanges == ["gatana"], "memoised: no second exchange"

    @pytest.mark.asyncio
    async def test_pre_resolved_tools_skip_exchange_and_discovery_entirely(self, gateway_config, mock_model):
        """When the orchestrator hands over its already-authenticated tools, a delegation must
        perform no token exchange, build no MCP client and open no tools/list."""
        from agent_common.core.tool_catalogue import (
            LazyMcpTool,
            build_server_catalogue,
            make_catalogue_tool,
            make_lazy_tool,
        )

        entry = make_catalogue_tool(
            server_name="some-server",
            name="some_gateway_tool",
            description="orchestrator's",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        build_server_catalogue("some-server", [entry], source="stateless")
        orch_tool = make_lazy_tool(
            entry, server_name="some-server", connection={"url": "gw", "headers": {"Authorization": "Bearer orch"}}
        )
        oauth2_client = MagicMock()
        oauth2_client.exchange_token = AsyncMock(side_effect=AssertionError("must not exchange"))
        runnable = DynamicLocalAgentRunnable(
            config=gateway_config,
            model=mock_model,
            oauth2_client=oauth2_client,
            user_token="user-token",
            mcp_gateway_url="https://gateway.example/mcp",
            mcp_gateway_client_id="gatana",
            pre_resolved_tools={"some_gateway_tool": orch_tool},
        )
        with patch(
            "agent_common.agents.dynamic_agent.MultiServerMCPClient", side_effect=AssertionError("no MCP client")
        ):
            tools = await runnable._discover_mcp_tools()

        assert [t.name for t in tools] == ["some_gateway_tool"]
        assert isinstance(tools[0], LazyMcpTool)
        assert tools[0] is not orch_tool, "a private copy — schema validation must not touch the registry entry"
        assert tools[0].catalogue_entry is orch_tool.catalogue_entry, "…but the bytes are shared"
        assert tools[0]._connection["headers"]["Authorization"] == "Bearer orch"
        oauth2_client.exchange_token.assert_not_called()
        assert runnable._mcp_discovery_error is None

    @pytest.mark.asyncio
    async def test_only_names_missing_from_pre_resolved_are_discovered(self, mock_model):
        config = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="gateway-agent",
            description="x",
            system_prompt="x",
            mcp_tools=["have_this", "need_this"],
        )
        oauth2_client = MagicMock()
        oauth2_client.exchange_token = AsyncMock(return_value="gateway-token")
        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=mock_model,
            oauth2_client=oauth2_client,
            user_token="user-token",
            mcp_gateway_url="https://gateway.example/mcp",
            mcp_gateway_client_id="gatana",
            pre_resolved_tools={"have_this": Tool(name="have_this", description="d", func=lambda x: x)},
        )
        with patch("agent_common.agents.dynamic_agent.MultiServerMCPClient") as mock_client_cls:
            _install_list_tools(
                mock_client_cls,
                return_value=[
                    Tool(name="need_this", description="d", func=lambda x: x),
                    Tool(name="have_this", description="dup", func=lambda x: x),
                ],
            )
            tools = await runnable._discover_mcp_tools()

        assert sorted(t.name for t in tools) == ["have_this", "need_this"]
        assert next(t for t in tools if t.name == "have_this").description == "d", "pre-resolved wins"
        assert oauth2_client.exchange_token.await_count == 1  # one exchange for the one connection still needed

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
