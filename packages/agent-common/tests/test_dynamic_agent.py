"""Unit tests for DynamicLocalAgentRunnable and LocalSubAgentConfig."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import Tool

from a2a.types import TaskState
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

        async def empty_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        mock_graph.astream = empty_stream

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
        assert result.state == TaskState.completed
        assert result.is_complete is True
        assert result.requires_input is False

    @pytest.mark.asyncio
    async def test_process_returns_input_required_response(self, basic_config, mock_model):
        """Test that _astream_impl yields input_required state via structured output."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        mock_graph = AsyncMock()

        async def empty_stream(*args, **kwargs):
            return
            yield

        mock_graph.astream = empty_stream

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
        assert result.state == TaskState.input_required
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

        async def empty_stream(*args, **kwargs):
            return
            yield

        mock_graph.astream = empty_stream

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
        assert result.state == TaskState.failed
        assert result.is_complete is True

    @pytest.mark.asyncio
    async def test_process_fallback_when_no_structured_response(self, basic_config, mock_model):
        """Test fallback to completed state when no structured response is found."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        mock_graph = AsyncMock()

        async def empty_stream(*args, **kwargs):
            return
            yield

        mock_graph.astream = empty_stream

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
        assert result.state == TaskState.completed
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
