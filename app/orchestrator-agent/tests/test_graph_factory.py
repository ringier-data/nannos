"""Unit tests for GraphFactory class.

Tests the centralized graph factory architecture:
- Model creation and caching (Bedrock vs OpenAI)
- Shared checkpointer for conversation continuity
- Middleware stack assembly with DynamicToolDispatchMiddleware
- Graph creation and caching per model type
"""

from unittest.mock import Mock, patch

from app.core.graph_factory import GraphFactory
from app.middleware import DynamicToolDispatchMiddleware
from app.models import AgentSettings


class TestGraphFactoryInitialization:
    """Test GraphFactory initialization."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_initialization_creates_checkpointer(self, mock_dynamodb_saver):
        """Test GraphFactory creates a shared DynamoDB checkpointer."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2

        factory = GraphFactory(config=config, thinking=False)

        mock_dynamodb_saver.assert_called_once()
        assert factory._checkpointer is not None

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_initialization_creates_middleware_instances(self, mock_dynamodb_saver):
        """Test GraphFactory creates middleware instances."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2

        factory = GraphFactory(config=config, thinking=False)

        assert factory._a2a_middleware is not None
        assert factory._auth_middleware is not None
        assert factory._todo_middleware is not None
        assert factory._retry_middleware is not None

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_a2a_middleware_property(self, mock_dynamodb_saver):
        """Test a2a_middleware property returns the middleware instance."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2

        factory = GraphFactory(config=config, thinking=False)

        assert factory.a2a_middleware is factory._a2a_middleware


class TestGraphCreation:
    """Test graph creation and caching."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    @patch("app.core.graph_factory.create_deep_agent")
    @patch("app.core.graph_factory.AzureChatOpenAI")
    def test_get_graph_creates_openai_graph(self, mock_azure, mock_create, mock_dynamodb):
        """Test that get_graph creates an OpenAI graph."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.SYSTEM_INSTRUCTION = "Test prompt"
        config.get_azure_deployment.return_value = "gpt-4o"
        config.get_azure_model_name.return_value = "gpt-4o"

        mock_model = Mock()
        mock_azure.return_value = mock_model
        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        factory = GraphFactory(config=config, thinking=False)
        result = factory.get_graph("gpt4o")

        assert result == mock_compiled
        mock_create.assert_called_once()
        # OpenAI should use response_format
        call_args = mock_create.call_args
        assert "response_format" in call_args.kwargs

    @patch("app.core.graph_factory.DynamoDBSaver")
    @patch("app.core.graph_factory.create_deep_agent")
    @patch("app.core.graph_factory.ChatBedrockConverse")
    @patch("app.core.graph_factory.isinstance", return_value=True)
    def test_get_graph_creates_bedrock_graph(self, mock_isinstance, mock_bedrock, mock_create, mock_dynamodb):
        """Test that get_graph creates a Bedrock graph."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.SYSTEM_INSTRUCTION = "Test prompt"
        config.get_bedrock_model_id.return_value = "anthropic.claude-sonnet"
        config.get_bedrock_region.return_value = "us-east-1"

        mock_model = Mock()
        mock_bedrock.return_value = mock_model
        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        factory = GraphFactory(config=config, thinking=False)
        result = factory.get_graph("claude-sonnet-4.5")

        assert result == mock_compiled
        mock_create.assert_called_once()

    @patch("app.core.graph_factory.DynamoDBSaver")
    @patch("app.core.graph_factory.create_deep_agent")
    @patch("app.core.graph_factory.AzureChatOpenAI")
    def test_get_graph_caches_result(self, mock_azure, mock_create, mock_dynamodb):
        """Test that get_graph caches and returns cached graph."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.SYSTEM_INSTRUCTION = "Test prompt"
        config.get_azure_deployment.return_value = "gpt-4o"
        config.get_azure_model_name.return_value = "gpt-4o"

        mock_azure.return_value = Mock()
        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        factory = GraphFactory(config=config, thinking=False)
        result1 = factory.get_graph("gpt4o")
        result2 = factory.get_graph("gpt4o")

        assert result1 == result2
        mock_create.assert_called_once()  # Only created once

    @patch("app.core.graph_factory.DynamoDBSaver")
    @patch("app.core.graph_factory.create_deep_agent")
    @patch("app.core.graph_factory.AzureChatOpenAI")
    def test_get_graph_uses_default_model(self, mock_azure, mock_create, mock_dynamodb):
        """Test that get_graph uses default model when none specified."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.SYSTEM_INSTRUCTION = "Test prompt"
        config.get_azure_deployment.return_value = "gpt-4o"
        config.get_azure_model_name.return_value = "gpt-4o"

        mock_azure.return_value = Mock()
        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        factory = GraphFactory(config=config, thinking=False)
        result = factory.get_graph()  # No model specified

        assert result == mock_compiled
        # Default model is gpt4o (OpenAI)
        mock_azure.assert_called_once()


class TestMiddlewareStack:
    """Test middleware stack creation."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_middleware_stack_includes_dynamic_tool_dispatch(self, mock_dynamodb):
        """Test that middleware stack includes DynamicToolDispatchMiddleware first."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2

        factory = GraphFactory(config=config, thinking=False)

        # OpenAI (non-Bedrock)
        stack = factory._create_middleware_stack(is_bedrock=False)

        # DynamicToolDispatchMiddleware should be first
        assert isinstance(stack[0], DynamicToolDispatchMiddleware)
        assert len(stack) == 6  # DynamicTool, UserPreferences, Auth, Retry, A2A, Todo

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_bedrock_middleware_has_static_tool(self, mock_dynamodb):
        """Test that Bedrock middleware includes FinalResponseSchema as static tool."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2

        factory = GraphFactory(config=config, thinking=False)

        stack = factory._create_middleware_stack(is_bedrock=True)
        dynamic_middleware = stack[0]

        assert isinstance(dynamic_middleware, DynamicToolDispatchMiddleware)
        assert "FinalResponseSchema" in dynamic_middleware.static_tools

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_openai_middleware_has_file_tools(self, mock_dynamodb):
        """Test that OpenAI middleware has file handling tools."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2

        factory = GraphFactory(config=config, thinking=False)

        stack = factory._create_middleware_stack(is_bedrock=False)
        dynamic_middleware = stack[0]

        assert isinstance(dynamic_middleware, DynamicToolDispatchMiddleware)
        # OpenAI models have generate_presigned_url tool (read_file is now a sub-agent)
        assert len(dynamic_middleware.static_tools) == 1
        assert "generate_presigned_url" in dynamic_middleware.static_tools


class TestGraphCreationParameters:
    """Test that graphs are created with correct parameters."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    @patch("app.core.graph_factory.create_deep_agent")
    @patch("app.core.graph_factory.AzureChatOpenAI")
    def test_graph_created_with_empty_tools_and_subagents(self, mock_azure, mock_create, mock_dynamodb):
        """Test that graph is created with empty tools and subagents (injected at runtime)."""
        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        config.CHECKPOINT_TTL_DAYS = 14
        config.CHECKPOINT_AWS_REGION = "eu-west-1"
        config.CHECKPOINT_MAX_RETRIES = 3
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.SYSTEM_INSTRUCTION = "Test prompt"
        config.get_azure_deployment.return_value = "gpt-4o"
        config.get_azure_model_name.return_value = "gpt-4o"

        mock_azure.return_value = Mock()
        mock_create.return_value = Mock()

        factory = GraphFactory(config=config, thinking=False)
        factory.get_graph("gpt4o")

        call_args = mock_create.call_args
        # Tools are empty (injected at runtime via middleware)
        assert call_args.kwargs["tools"] == []
        # Sub-agents are empty (injected at runtime via GraphRuntimeContext.subagent_registry)
        # This includes both local sub-agents (file-analyzer) and remote A2A agents
        subagents = call_args.kwargs["subagents"]
        assert len(subagents) == 0
