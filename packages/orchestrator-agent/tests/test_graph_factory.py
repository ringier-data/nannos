"""Unit tests for GraphFactory class.

Tests the centralized graph factory architecture:
- Initialization and middleware creation
- Middleware stack composition and order
- Static tools caching
- Graph caching behavior

Note: This file focuses on testing real behavior without excessive mocking.
Graph creation with actual models should be tested in integration tests.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest
from agent_common.middleware.storage_paths_middleware import StoragePathsInstructionMiddleware
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_aws.middleware.prompt_caching import BedrockPromptCachingMiddleware

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
    config.CHECKPOINT_POSTGRES_HOST = None
    config.CHECKPOINT_TTL_DAYS = 14
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

    def test_initialization_creates_checkpointer(self, mock_config):
        """Test GraphFactory creates a shared checkpointer (MemorySaver when no Postgres host)."""
        factory = GraphFactory(config=mock_config)

        assert factory._checkpointer is not None

    def test_initialization_creates_middleware_instances(self, mock_config):
        """Test GraphFactory creates middleware instances."""
        factory = GraphFactory(config=mock_config)

        assert factory._a2a_middleware is not None
        assert factory._auth_middleware is not None
        assert factory._todo_middleware is not None
        assert factory._retry_middleware is not None

    def test_a2a_middleware_property(self, mock_config):
        """Test a2a_middleware property returns the middleware instance."""
        factory = GraphFactory(config=mock_config)

        assert factory.a2a_middleware is factory._a2a_middleware


class TestGraphCreation:
    """Test graph caching behavior.

    Note: Tests that verify graph creation with actual models are excluded because
    they require mocking the store property, which triggers AsyncConnectionPool creation
    requiring an event loop. These scenarios should be covered by integration tests.
    """

    def test_graph_cache_dictionary_initialized(self, mock_config):
        """Test that the graph cache dictionary is initialized on factory creation."""
        factory = GraphFactory(config=mock_config)

        # Verify cache is initialized as empty dict
        assert factory._graphs == {}
        assert isinstance(factory._graphs, dict)


class TestMiddlewareStack:
    """Test middleware stack creation and composition."""

    @patch("app.core.graph_factory._has_aws_credentials", return_value=True)
    @patch("langgraph.store.postgres.aio.AsyncPostgresStore")
    def test_middleware_stack_order(self, mock_pg_store, _mock_creds, mock_config):
        """Test that middleware stack is assembled in the correct order for a Bedrock model."""
        factory = GraphFactory(config=mock_config)

        bedrock_model = MagicMock(spec=ChatBedrockConverse)
        stack = factory._create_middleware_stack(model=bedrock_model)

        # Verify correct order. The conversation-context gate is outermost (so its
        # injected gated tool flows through DynamicToolDispatch's schema-cleanup),
        # followed by DynamicTool, static content before cache point, steering after
        # cache, user prefs after steering, playbook after prefs, then the code
        # interpreter (_PTCToleranceCodeInterpreterMiddleware, which exposes the
        # eval REPL + PTC bridge and hides PTC-exposed tools from the model itself).
        assert len(stack) == 17
        assert stack[0].__class__.__name__ == "ConversationContextToolsMiddleware"
        assert isinstance(stack[1], DynamicToolDispatchMiddleware)
        assert isinstance(stack[2], StoragePathsInstructionMiddleware)
        assert isinstance(stack[3], BedrockPromptCachingMiddleware)
        # stack[4] = SteeringMiddleware (from ringier_a2a_sdk)
        assert stack[4].__class__.__name__ == "SteeringMiddleware"
        assert isinstance(stack[5], UserPreferencesMiddleware)
        # stack[6] = PlaybookInjectionMiddleware
        assert stack[6].__class__.__name__ == "PlaybookInjectionMiddleware"
        # stack[7] = CodeInterpreterMiddleware (eval REPL + PTC bridge; also hides
        # every PTC-exposed tool from the model's bound tool list)
        assert stack[7].__class__.__name__ == "_PTCToleranceCodeInterpreterMiddleware"
        # stack[8] = ToolStatusMiddleware (emits status for tool calls)
        assert stack[8].__class__.__name__ == "ToolStatusMiddleware"
        assert isinstance(stack[9], RepeatedToolCallMiddleware)
        assert isinstance(stack[10], AuthErrorDetectionMiddleware)
        assert stack[11].__class__.__name__ == "ErrorClassificationMiddleware"
        # stack[12] = ConditionalHumanInTheLoopMiddleware
        assert stack[12].__class__.__name__ == "ConditionalHumanInTheLoopMiddleware"
        assert isinstance(stack[13], ToolRetryMiddleware)
        assert isinstance(stack[14], A2ATaskTrackingMiddleware)
        assert isinstance(stack[15], TodoStatusMiddleware)
        # Innermost: strips duplicate plain-text content from AIMessages that
        # carry a FinalResponseSchema tool call.
        assert stack[16].__class__.__name__ == "FinalResponseTextStripMiddleware"

    @patch("app.core.graph_factory._has_aws_credentials", return_value=True)
    @patch("langgraph.store.postgres.aio.AsyncPostgresStore")
    def test_middleware_stack_excludes_bedrock_caching_for_non_bedrock_models(
        self, mock_pg_store, _mock_creds, mock_config
    ):
        """BedrockPromptCachingMiddleware must NOT be attached for non-Bedrock models."""
        factory = GraphFactory(config=mock_config)

        # Non-Bedrock model (e.g. OpenAI / Gemini): plain Mock that is NOT a ChatBedrockConverse
        non_bedrock_model = Mock()
        stack = factory._create_middleware_stack(model=non_bedrock_model)

        assert not any(isinstance(m, BedrockPromptCachingMiddleware) for m in stack)
        # One fewer middleware than the Bedrock case (cache middleware skipped)
        assert len(stack) == 16

    @patch("app.core.graph_factory._has_aws_credentials", return_value=True)
    @patch("langgraph.store.postgres.aio.AsyncPostgresStore")
    def test_middleware_stack_excludes_bedrock_caching_when_model_is_none(
        self, mock_pg_store, _mock_creds, mock_config
    ):
        """Default (model=None) call path must not inject Bedrock caching either."""
        factory = GraphFactory(config=mock_config)

        stack = factory._create_middleware_stack()

        assert not any(isinstance(m, BedrockPromptCachingMiddleware) for m in stack)

    @patch("app.core.graph_factory._has_aws_credentials", return_value=True)
    @patch("langgraph.store.postgres.aio.AsyncPostgresStore")
    def test_middleware_stack_dynamic_tool_dispatch_config(
        self, mock_pg_store, _mock_creds, mock_config
    ):
        """Test that DynamicToolDispatchMiddleware is configured correctly."""
        factory = GraphFactory(config=mock_config)

        stack = factory._create_middleware_stack()
        dynamic_middleware = stack[1]

        assert isinstance(dynamic_middleware, DynamicToolDispatchMiddleware)
        # Static tools are added directly to graph, not via middleware
        assert dynamic_middleware.static_tools == {}


class TestStaticTools:
    """Test static tools caching and retrieval."""

    @patch("app.core.graph_factory._has_aws_credentials", return_value=True)
    @patch("langgraph.store.postgres.aio.AsyncPostgresStore")
    def test_get_static_tools_returns_cached_tools(self, mock_pg_store, _mock_creds, mock_config):
        """Test that get_static_tools returns the same cached list on repeated calls."""
        factory = GraphFactory(config=mock_config)

        tools1 = factory.get_static_tools()
        tools2 = factory.get_static_tools()

        # Verify same object returned (cached)
        assert tools1 is tools2
        # Verify it's a list
        assert isinstance(tools1, list)

    @patch("app.core.graph_factory._has_aws_credentials", return_value=True)
    @patch("langgraph.store.postgres.aio.AsyncPostgresStore")
    def test_static_tools_include_time_and_file(self, mock_pg_store, _mock_creds, mock_config):
        """Test that static tools list includes expected tools."""
        factory = GraphFactory(config=mock_config)

        tools = factory.get_static_tools()

        # Verify it's a list of tools
        assert isinstance(tools, list)
        assert len(tools) == 3  # 3 core tools; playbook tools removed (replaced by console MCP)

        # Get tool names
        tool_names = [tool.name for tool in tools]

        # Verify expected tools present
        assert "generate_presigned_url" in tool_names
        assert "get_current_time" in tool_names
        assert "copy_file" in tool_names
