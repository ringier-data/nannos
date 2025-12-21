"""Unit tests for DynamicLocalAgentRunnable and LocalSubAgentConfig."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.a2a.models import LocalSubAgentConfig
from app.agents.dynamic_agent import (
    DynamicLocalAgentRunnable,
    SubAgentResponseSchema,
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
        return LocalSubAgentConfig(
            name="test-agent",
            description="A test agent for unit testing",
            system_prompt="You are a helpful test assistant.",
        )

    @pytest.fixture
    def mcp_config(self):
        """Create a config with MCP URL."""
        return LocalSubAgentConfig(
            name="mcp-agent",
            description="Agent with MCP tools",
            system_prompt="You are an expert with tools.",
            mcp_gateway_url="https://tools.example.com/mcp",
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
        """Test that tools are inherited when no MCP URL is set."""
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        orchestrator_tools = [mock_tool]

        runnable = DynamicLocalAgentRunnable(
            config=basic_config,
            model=mock_model,
            orchestrator_tools=orchestrator_tools,
        )

        # Tools should be from orchestrator since no MCP URL
        effective_tools = runnable._get_effective_tools()
        assert len(effective_tools) == 1
        assert effective_tools[0].name == "test_tool"

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

        with patch("app.subagents.dynamic_agent.create_agent", return_value=mock_graph):
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

        with patch("app.subagents.dynamic_agent.create_agent", return_value=mock_graph):
            result = await runnable._process("Do something", context_id="ctx-123")

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

        with patch("app.subagents.dynamic_agent.create_agent", return_value=mock_graph):
            result = await runnable._process("Create a ticket", context_id="ctx-123")

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
        with patch("app.subagents.dynamic_agent.create_agent", side_effect=Exception("Model error")):
            result = await runnable._process("Do something", context_id="ctx-123")

        # Check A2A response format indicates failure
        # A2A protocol: failed state means task did not complete successfully, so is_complete=False
        assert "state" in result
        assert result["state"] == "failed"
        assert result["is_complete"] is False
        assert "error" in result.get("messages", [{}])[-1].content.lower() or "error" in str(result).lower()

    @pytest.mark.asyncio
    async def test_mcp_discovery_lazy(self, mcp_config, mock_model):
        """Test that MCP tools are discovered lazily on first invocation."""
        mock_tool = MagicMock()
        mock_tool.name = "discovered_tool"

        runnable = DynamicLocalAgentRunnable(
            config=mcp_config,
            model=mock_model,
            orchestrator_tools=[],
        )

        # Tools should not be discovered yet
        assert runnable._discovered_tools is None

        # Mock MCP discovery
        with patch.object(runnable, "_discover_mcp_tools", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = [mock_tool]

            # Mock create_agent
            mock_graph = AsyncMock()
            mock_graph.ainvoke = AsyncMock(
                return_value={
                    "messages": [MagicMock(content="Done")],
                    "structured_response": SubAgentResponseSchema(
                        task_state="completed",
                        message="Done",
                    ),
                }
            )

            with patch("app.subagents.dynamic_agent.create_agent", return_value=mock_graph):
                await runnable._ensure_agent()

            # MCP discovery should have been called
            mock_discover.assert_called_once()

        # Discovered tools should now be set
        assert runnable._discovered_tools is not None
        assert len(runnable._discovered_tools) == 1

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

        with patch("app.subagents.dynamic_agent.create_agent", return_value=mock_graph):
            result = await runnable._process("Do something", context_id="ctx-123")

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

        with patch("app.subagents.dynamic_agent.create_agent", return_value=mock_graph):
            result = await runnable._process("Do something", context_id="ctx-123")

        # Should fall back to completed state with warning
        assert result["state"] == "completed"
        assert result["is_complete"] is True


class TestCreateDynamicLocalSubagent:
    """Tests for create_dynamic_local_subagent factory function."""

    def test_creates_compiled_subagent(self):
        """Test that factory creates a proper CompiledSubAgent."""
        config = LocalSubAgentConfig(
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
        config = LocalSubAgentConfig(
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
