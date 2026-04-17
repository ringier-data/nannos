"""Tests for JSON input support in dynamic tool dispatch."""

import json

import pytest

from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware


class TestJsonInputValidation:
    """Test JSON input validation for agents with application/json input mode."""

    def test_validate_json_input_text_only_agent(self):
        """Text-only agents should accept string descriptions unchanged."""
        description = "Do something"
        input_modes = ["text"]
        result = DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")
        assert result == description

    def test_validate_json_input_json_agent_with_dict(self):
        """JSON agents should accept dict descriptions unchanged."""
        description = '{"task": "analyze", "data": [1, 2, 3]}'
        input_modes = ["application/json"]
        result = DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")
        assert result == json.loads(description)

    def test_validate_json_input_json_agent_with_valid_json_string(self):
        """JSON agents should parse valid JSON strings."""
        description = '{"task": "analyze", "data": [1, 2, 3]}'
        input_modes = ["application/json"]
        result = DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")
        assert result == {"task": "analyze", "data": [1, 2, 3]}

    def test_validate_json_input_json_agent_with_invalid_json_string(self):
        """JSON agents should raise error for invalid JSON strings."""
        description = '{"task": "analyze", invalid json}'
        input_modes = ["application/json"]
        with pytest.raises(ValueError, match="not valid JSON"):
            DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")

    def test_validate_json_input_mixed_modes(self):
        """Agents with mixed input modes should prefer JSON if included."""
        description = '{"task": "analyze"}'
        input_modes = ["text", "application/json"]
        result = DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")
        assert result == json.loads(description)

    def test_validate_json_input_mixed_modes_with_text(self):
        """Agents with mixed input modes should accept fallback to text if JSON parsing fails."""
        description = "this is a text"
        input_modes = ["text", "application/json"]
        result = DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")
        assert result == description

    def test_validate_json_input_error_includes_agent_name(self):
        """Error messages should include the agent name."""
        description = '{"invalid"'
        input_modes = ["application/json"]
        with pytest.raises(ValueError, match="'test-agent'"):
            DynamicToolDispatchMiddleware._validate_json_input_for_agent(description, input_modes, "test-agent")


class TestJsonMessageContent:
    """Test JSON content block creation in messages."""

    @pytest.mark.asyncio
    async def test_build_subagent_human_message_with_json_input(self):
        """HumanMessage with JSON content blocks should be created for JSON agents."""
        from langchain_core.messages import HumanMessage

        from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
        from app.models.config import GraphRuntimeContext

        middleware = DynamicToolDispatchMiddleware()

        # Create mock subagent with JSON input mode
        subagent = {
            "name": "json-agent",
            "runnable": type("obj", (object,), {"input_modes": ["application/json"]})(),
        }

        # Create context
        context = GraphRuntimeContext(
            user_id="test-user",
            user_sub="test-sub",
            name="Test User",
            email="test@example.com",
            tool_registry={},
            subagent_registry={},
            pending_file_blocks=[],
        )

        # Test with JSON input
        json_data = {"task": "analyze", "data": [1, 2, 3]}
        message = await middleware._build_subagent_human_message(json_data, context, subagent)

        # Verify message structure
        assert isinstance(message, HumanMessage)
        assert message.content_blocks is not None
        assert len(message.content_blocks) == 1  # json block only

        # Block should be non_standard JSON
        json_block = message.content_blocks[0]
        assert json_block["type"] == "non_standard"
        assert json_block["value"]["media_type"] == "application/json"
        assert json_block["value"]["data"] == json_data

    @pytest.mark.asyncio
    async def test_build_subagent_human_message_with_text_agent(self):
        """HumanMessage with text content should be created for text-only agents."""
        from langchain_core.messages import HumanMessage

        from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
        from app.models.config import GraphRuntimeContext

        middleware = DynamicToolDispatchMiddleware()

        # Create mock text-only subagent
        subagent = {
            "name": "text-agent",
            "runnable": type("obj", (object,), {"input_modes": ["text"]})(),
        }

        # Create context
        context = GraphRuntimeContext(
            user_id="test-user",
            user_sub="test-sub",
            name="Test User",
            email="test@example.com",
            tool_registry={},
            subagent_registry={},
            pending_file_blocks=[],
        )

        # Test with text input
        description = "Do something"
        message = await middleware._build_subagent_human_message(description, context, subagent)

        # Verify message is simple text
        assert isinstance(message, HumanMessage)
        assert message.content == description
