"""Tests for LangGraphBedrockAgent middleware configuration."""

import os
from unittest.mock import MagicMock, patch

from ringier_a2a_sdk.agent.langgraph_bedrock import LangGraphBedrockAgent
from ringier_a2a_sdk.middleware.bedrock_prompt_caching import BedrockPromptCachingMiddleware
from ringier_a2a_sdk.middleware.tool_schema_cleaning import ToolSchemaCleaningMiddleware


class TestLangGraphBedrockAgentMiddleware:
    """Tests for LangGraphBedrockAgent middleware setup."""

    @patch.dict(
        os.environ,
        {
            "CHECKPOINT_DYNAMODB_TABLE_NAME": "test-table",
            "AWS_BEDROCK_REGION": "us-east-1",
        },
    )
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_bedrock_agent_includes_schema_cleaning_and_prompt_caching_by_default(
        self, mock_boto3_resource, mock_boto3_client
    ):
        """Test that LangGraphBedrockAgent includes both schema cleaning and prompt caching by default."""
        # Mock AWS clients
        mock_bedrock_client = MagicMock()
        mock_boto3_client.return_value = mock_bedrock_client

        mock_dynamodb = MagicMock()
        mock_boto3_resource.return_value = mock_dynamodb
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        class MinimalBedrockAgent(LangGraphBedrockAgent):
            """Minimal concrete implementation for testing."""

            async def _get_mcp_connections(self):
                return {}

            def _get_system_prompt(self):
                return "Test prompt"

            def _get_checkpoint_namespace(self):
                return "test-ns"

        agent = MinimalBedrockAgent()
        middleware = agent._get_middleware()

        # Verify that both middlewares are included in the correct order
        # Order matters: Prompt caching first (outer), then schema cleaning (inner/closest to model)
        assert len(middleware) == 2
        assert isinstance(middleware[0], BedrockPromptCachingMiddleware)
        assert isinstance(middleware[1], ToolSchemaCleaningMiddleware)

    @patch.dict(
        os.environ,
        {
            "CHECKPOINT_DYNAMODB_TABLE_NAME": "test-table",
            "AWS_BEDROCK_REGION": "us-east-1",
        },
    )
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_subclass_can_extend_middleware(self, mock_boto3_resource, mock_boto3_client):
        """Test that subclasses can extend the middleware list by calling super()."""
        # Mock AWS clients
        mock_bedrock_client = MagicMock()
        mock_boto3_client.return_value = mock_bedrock_client

        mock_dynamodb = MagicMock()
        mock_boto3_resource.return_value = mock_dynamodb
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        from langchain.agents.middleware.types import AgentMiddleware

        class CustomMiddleware(AgentMiddleware):
            """Custom test middleware."""

            pass

        class ExtendedBedrockAgent(LangGraphBedrockAgent):
            """Bedrock agent that extends middleware."""

            async def _get_mcp_connections(self):
                return {}

            def _get_system_prompt(self):
                return "Test prompt"

            def _get_checkpoint_namespace(self):
                return "test-ns"

            def _get_middleware(self):
                # Extend parent middleware instead of replacing it
                return super()._get_middleware() + [CustomMiddleware()]

        agent = ExtendedBedrockAgent()
        middleware = agent._get_middleware()

        # Verify that all three middlewares are present in the correct order
        # Prompt caching (outer) → Schema cleaning (inner) → Custom (innermost/added last)
        assert len(middleware) == 3
        assert isinstance(middleware[0], BedrockPromptCachingMiddleware)
        assert isinstance(middleware[1], ToolSchemaCleaningMiddleware)
        assert isinstance(middleware[2], CustomMiddleware)

    def test_middleware_filters_invalid_tools(self):
        """Test that ToolSchemaCleaningMiddleware filters out invalid tools."""
        from unittest.mock import MagicMock

        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.tools import tool

        @tool
        def valid_tool(x: int) -> int:
            """A valid tool."""
            return x * 2

        # Create tools with various invalid formats
        tools = [
            valid_tool,  # Valid BaseTool
            None,  # Invalid: None
            {"function": {"description": "Missing name"}},  # Invalid: missing name
            {"function": {"name": "", "description": "Empty name"}},  # Invalid: empty name
            {"function": {"name": 123, "description": "Invalid name type"}},  # Invalid: non-string name
            "not_a_tool",  # Invalid: string instead of tool
        ]

        middleware = ToolSchemaCleaningMiddleware()

        # Create a mock request with required model parameter
        mock_model = MagicMock()
        request = ModelRequest(
            system_message="Test",
            messages=[],
            tools=tools,
            model=mock_model,
        )

        # Mock handler that just returns the request
        def mock_handler(req):
            return req

        # Call the middleware
        result = middleware.wrap_model_call(request, mock_handler)

        # Should only have 1 valid tool (the valid_tool)
        assert len(result.tools) == 1
        assert result.tools[0]["function"]["name"] == "valid_tool"
