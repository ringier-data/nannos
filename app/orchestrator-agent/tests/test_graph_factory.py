"""Unit tests for GraphFactory class.

Tests the centralized graph factory architecture:
- Initialization and middleware creation
- Middleware stack composition and order
- Static tools caching
- Graph caching behavior

Note: This file focuses on testing real behavior without excessive mocking.
Graph creation with actual models should be tested in integration tests.
"""

from unittest.mock import Mock, patch

import pytest
from langchain.agents.middleware import ToolRetryMiddleware

from app.core.graph_factory import GraphFactory
from app.middleware import (
    A2ATaskTrackingMiddleware,
    AuthErrorDetectionMiddleware,
    DynamicToolDispatchMiddleware,
    RepeatedToolCallMiddleware,
    TodoStatusMiddleware,
    UserPreferencesMiddleware,
)
from app.models.config import AgentSettings


@pytest.fixture
def mock_config():
    """Create a properly configured mock AgentSettings."""
    config = Mock(spec=AgentSettings)
    config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
    config.CHECKPOINT_TTL_DAYS = 14
    config.CHECKPOINT_AWS_REGION = "eu-west-1"
    config.CHECKPOINT_MAX_RETRIES = 3
    config.MAX_RETRIES = 3
    config.BACKOFF_FACTOR = 2
    # Postgres settings
    config.POSTGRES_USER = "testuser"
    config.POSTGRES_PASSWORD = "testpass"  # pragma: allowlist secret
    config.POSTGRES_HOST = "localhost"
    config.POSTGRES_PORT = "5432"
    config.POSTGRES_DB = "testdb"
    config.get_bedrock_region = Mock(return_value="eu-central-1")
    # Add missing method mocks
    config.get_azure_deployment = Mock(return_value="gpt-4o")
    config.get_azure_model_name = Mock(return_value="gpt-4o")
    config.get_bedrock_model_id = Mock(return_value="anthropic.claude-sonnet")
    return config


class TestGraphFactoryInitialization:
    """Test GraphFactory initialization."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_initialization_creates_checkpointer(self, mock_dynamodb_saver, mock_config):
        """Test GraphFactory creates a shared DynamoDB checkpointer."""
        factory = GraphFactory(config=mock_config)

        mock_dynamodb_saver.assert_called_once()
        assert factory._checkpointer is not None

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_initialization_creates_middleware_instances(self, mock_dynamodb_saver, mock_config):
        """Test GraphFactory creates middleware instances."""
        factory = GraphFactory(config=mock_config)

        assert factory._a2a_middleware is not None
        assert factory._auth_middleware is not None
        assert factory._todo_middleware is not None
        assert factory._retry_middleware is not None

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_a2a_middleware_property(self, mock_dynamodb_saver, mock_config):
        """Test a2a_middleware property returns the middleware instance."""
        factory = GraphFactory(config=mock_config)

        assert factory.a2a_middleware is factory._a2a_middleware


class TestGraphCreation:
    """Test graph caching behavior.

    Note: Tests that verify graph creation with actual models are excluded because
    they require mocking the store property, which triggers AsyncConnectionPool creation
    requiring an event loop. These scenarios should be covered by integration tests.
    """

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_graph_cache_dictionary_initialized(self, mock_dynamodb, mock_config):
        """Test that the graph cache dictionary is initialized on factory creation."""
        factory = GraphFactory(config=mock_config)

        # Verify cache is initialized as empty dict
        assert factory._graphs == {}
        assert isinstance(factory._graphs, dict)


class TestMiddlewareStack:
    """Test middleware stack creation and composition."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_middleware_stack_order(self, mock_dynamodb, mock_config):
        """Test that middleware stack is assembled in the correct order."""
        factory = GraphFactory(config=mock_config)

        stack = factory._create_middleware_stack()

        # Verify correct order (DynamicTool must be first)
        assert len(stack) == 7
        assert isinstance(stack[0], DynamicToolDispatchMiddleware)
        assert isinstance(stack[1], UserPreferencesMiddleware)
        assert isinstance(stack[2], RepeatedToolCallMiddleware)
        assert isinstance(stack[3], AuthErrorDetectionMiddleware)
        assert isinstance(stack[4], ToolRetryMiddleware)
        assert isinstance(stack[5], A2ATaskTrackingMiddleware)
        assert isinstance(stack[6], TodoStatusMiddleware)

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_middleware_stack_dynamic_tool_dispatch_config(self, mock_dynamodb, mock_config):
        """Test that DynamicToolDispatchMiddleware is configured correctly."""
        factory = GraphFactory(config=mock_config)

        stack = factory._create_middleware_stack()
        dynamic_middleware = stack[0]

        assert isinstance(dynamic_middleware, DynamicToolDispatchMiddleware)
        # Static tools are added directly to graph, not via middleware
        assert dynamic_middleware.static_tools == {}


class TestStaticTools:
    """Test static tools caching and retrieval."""

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_get_static_tools_returns_cached_tools(self, mock_dynamodb, mock_config):
        """Test that get_static_tools returns the same cached list on repeated calls."""
        factory = GraphFactory(config=mock_config)

        tools1 = factory.get_static_tools()
        tools2 = factory.get_static_tools()

        # Verify same object returned (cached)
        assert tools1 is tools2
        # Verify it's a list
        assert isinstance(tools1, list)

    @patch("app.core.graph_factory.DynamoDBSaver")
    def test_static_tools_include_time_and_file(self, mock_dynamodb, mock_config):
        """Test that static tools list includes expected tools."""
        factory = GraphFactory(config=mock_config)

        tools = factory.get_static_tools()

        # Verify it's a list of tools
        assert isinstance(tools, list)
        assert len(tools) == 2  # FinalResponseSchema only added when with_response_tool=True

        # Get tool names
        tool_names = [tool.name for tool in tools]

        # Verify expected tools present
        assert "generate_presigned_url" in tool_names
        assert "get_current_time" in tool_names
