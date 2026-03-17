"""Unit tests for DynamicLocalAgentRunnable and LocalSubAgentConfig."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import Tool

from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.models import LocalLangGraphSubAgentConfig
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
        """Test that _process returns A2A-compliant success response via structured output."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock the agent with structured response (OpenAI style)
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Task completed successfully.")],
                "structured_response": SubAgentResponseSchema(
                    task_state="completed",
                    message="Task completed successfully.",
                ),
            }
        )

        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph):
            result = await runnable._process(
                input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Please complete the task.")]),
                config={}
            )

        # Check A2A response format
        assert "messages" in result
        assert "state" in result
        assert result["state"] == "completed"
        assert result["is_complete"] is True
        assert result["requires_input"] is False

    @pytest.mark.asyncio
    async def test_process_returns_input_required_response(self, basic_config, mock_model):
        """Test that _process returns input_required via structured output."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock the agent with structured response indicating input required
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="What is the project name?")],
                "structured_response": SubAgentResponseSchema(
                    task_state="input_required",
                    message="What is the project name?",
                ),
            }
        )

        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph):
            result = await runnable._process(
                input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Create a ticket")]),
                config={}
            )

        # Check A2A response format indicates input required
        assert "state" in result
        assert result["state"] == "input_required"
        assert result["is_complete"] is False
        assert result["requires_input"] is True

    @pytest.mark.asyncio
    async def test_process_returns_failed_on_error(self, basic_config, mock_model):
        """Test that _process returns failed state on error."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock create_agent to raise an exception
        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", side_effect=Exception("Model error")):
            result = await runnable._process(
                input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Do something")]),
                config={}
            )

        # Check A2A response format indicates failure
        # A2A protocol: failed state means task did not complete successfully, so is_complete=False
        assert "state" in result
        assert result["state"] == "failed"
        assert result["is_complete"] is False
        assert "error" in result.get("messages", [{}])[-1].content.lower() or "error" in str(result).lower()

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
        """Test that _process handles Bedrock-style tool call responses."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock the agent with Bedrock-style tool call in messages
        mock_message = MagicMock()
        mock_message.tool_calls = [
            {
                "name": "SubAgentResponseSchema",
                "args": {"task_state": "failed", "message": "Could not complete the task."},
            }
        ]

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [mock_message],
            }
        )

        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph):
            result = await runnable._process(
                input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Do something")]),
                config={},
            )

        # Check A2A response format indicates failure
        assert result["state"] == "failed"
        assert result["is_complete"] is False

    @pytest.mark.asyncio
    async def test_process_fallback_when_no_structured_response(self, basic_config, mock_model):
        """Test fallback to completed state when no structured response is found."""
        runnable = DynamicLocalAgentRunnable(config=basic_config, model=mock_model)

        # Mock the agent without structured response (shouldn't happen but test fallback)
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "messages": [MagicMock(content="Some response without structured output")],
            }
        )

        with patch("agent_common.agents.dynamic_agent.build_sub_agent_graph", return_value=mock_graph):
            result = await runnable._process(
                input_data=SubAgentInput(a2a_tracking={}, messages=[HumanMessage(content="Do something")]),
                config={}
            )

        # Should fall back to completed state with warning
        assert result["state"] == "completed"
        assert result["is_complete"] is True


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
