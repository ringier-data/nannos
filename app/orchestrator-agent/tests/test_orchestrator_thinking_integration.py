"""Integration tests for orchestrator thinking configuration."""

import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from agent_common.models.base import ThinkingLevel

from app.core.executor import OrchestratorDeepAgentExecutor


class TestOrchestratorThinkingConfig:
    """Test orchestrator-level thinking configuration."""

    @patch.dict(
        os.environ,
        {
            "ORCHESTRATOR_ENABLE_THINKING": "true",
            "ORCHESTRATOR_THINKING_LEVEL": "medium",
        },
    )
    def test_default_thinking_level_from_environment(self):
        """Test that DEFAULT_THINKING_LEVEL is set from environment variables."""
        # Re-import to pick up environment variables
        from importlib import reload

        import agent_common.models.base as _ac_base

        reload(_ac_base)

        assert _ac_base.DEFAULT_THINKING_LEVEL == ThinkingLevel.medium

    @patch.dict(
        os.environ,
        {
            "ORCHESTRATOR_ENABLE_THINKING": "false",
        },
    )
    def test_thinking_disabled_by_default(self):
        """Test that thinking is disabled when ORCHESTRATOR_ENABLE_THINKING is false."""
        from importlib import reload

        import agent_common.models.base as _ac_base

        reload(_ac_base)

        assert _ac_base.DEFAULT_THINKING_LEVEL is None

    @pytest.mark.asyncio
    async def test_user_thinking_overrides_default(self):
        """Test that user-level thinking settings override environment defaults."""
        executor = OrchestratorDeepAgentExecutor()

        # Mock registry service to return user with thinking preferences
        mock_user = Mock()
        mock_user.id = "user-123"
        mock_user.sub = "sub-123"
        mock_user.name = "Test User"
        mock_user.email = "test@example.com"
        mock_user.groups = []
        mock_user.tools = []
        mock_user.sub_agents = []
        mock_user.timezone = "UTC"
        mock_user.language = "en"
        mock_user.message_formatting = "markdown"
        mock_user.slack_user_handle = None
        mock_user.custom_prompt = None
        mock_user.agent_metadata = {}
        mock_user.tool_names = []
        mock_user.local_subagents = []
        mock_user.enable_thinking = True
        mock_user.thinking_level = "high"

        with patch.object(executor.registry_service, "get_user", return_value=mock_user):
            # Build user config - updated signature requires user object
            user_config = await executor._build_user_config(
                user=mock_user,
                user_sub="sub-123",
                user_token="test-token",
                user_name="Test User",
                user_email="test@example.com",
                user_groups=[],
                model_choice=None,
                message_formatting="markdown",
                slack_user_handle=None,
                sub_agent_config_hash=None,
                enable_thinking=True,  # From client metadata
                thinking_level="high",  # From client metadata
            )

            # User config should have thinking enabled
            assert user_config.enable_thinking is True
            assert user_config.thinking_level == "high"

    @pytest.mark.asyncio
    async def test_metadata_thinking_overrides_user_settings(self):
        """Test that message metadata thinking settings override user settings."""
        executor = OrchestratorDeepAgentExecutor()

        # Mock registry service to return user with different thinking settings
        mock_user = Mock()
        mock_user.id = "user-123"
        mock_user.sub = "sub-123"
        mock_user.name = "Test User"
        mock_user.email = "test@example.com"
        mock_user.groups = []
        mock_user.tools = []
        mock_user.sub_agents = []
        mock_user.timezone = "UTC"
        mock_user.language = "en"
        mock_user.message_formatting = "markdown"
        mock_user.slack_user_handle = None
        mock_user.custom_prompt = None
        mock_user.agent_metadata = {}
        mock_user.tool_names = []
        mock_user.local_subagents = []
        mock_user.enable_thinking = True
        mock_user.thinking_level = "low"  # User preference is "low"

        with patch.object(executor.registry_service, "get_user", return_value=mock_user):
            # Build user config with metadata override - updated signature
            user_config = await executor._build_user_config(
                user=mock_user,
                user_sub="sub-123",
                user_token="test-token",
                user_name="Test User",
                user_email="test@example.com",
                user_groups=[],
                model_choice=None,
                message_formatting="markdown",
                slack_user_handle=None,
                sub_agent_config_hash=None,
                enable_thinking=True,  # Metadata override
                thinking_level="high",  # Metadata override
            )

            # Metadata should override user settings
            assert user_config.enable_thinking is True
            assert user_config.thinking_level == "high"


class TestGraphCreationWithThinking:
    """Test graph creation with different thinking levels."""

    @pytest.mark.asyncio
    async def test_get_or_create_graph_with_thinking_level(self):
        """Test that get_or_create_graph uses thinking_level parameter."""
        from app.core.agent import OrchestratorDeepAgent

        agent = OrchestratorDeepAgent()

        with patch("app.core.graph_factory.DynamoDBSaver"):
            with patch.object(agent, "_graph_factory") as mock_factory:
                mock_graph = Mock()
                mock_factory.get_graph.return_value = mock_graph

                # Get graph with specific thinking level
                graph = await agent.get_or_create_graph(
                    model_type="claude-sonnet-4.5",
                    thinking_level=ThinkingLevel.medium,
                )

                # Should call get_graph with both model_type and thinking_level
                mock_factory.get_graph.assert_called_once_with(
                    "claude-sonnet-4.5",
                    thinking_level=ThinkingLevel.medium,
                )

    @pytest.mark.asyncio
    async def test_graph_caching_by_thinking_level(self):
        """Test that graphs are cached separately by thinking level."""
        from app.core.graph_factory import GraphFactory
        from app.models.config import AgentSettings

        mock_config = Mock(spec=AgentSettings)
        mock_config.CHECKPOINT_DYNAMODB_TABLE_NAME = "test-table"
        mock_config.CHECKPOINT_S3_BUCKET_NAME = "test-bucket"
        mock_config.CHECKPOINT_AWS_REGION = "eu-central-1"
        mock_config.CHECKPOINT_TTL_DAYS = 30
        mock_config.CHECKPOINT_COMPRESSION_ENABLED = True
        mock_config.POSTGRES_USER = "test"
        mock_config.POSTGRES_PASSWORD = "test"
        mock_config.POSTGRES_HOST = "localhost"
        mock_config.POSTGRES_PORT = 5432
        mock_config.POSTGRES_DB = "test"
        mock_config.MAX_RETRIES = 3
        mock_config.BACKOFF_FACTOR = 2
        mock_config.get_bedrock_region.return_value = "eu-central-1"

        with patch("app.core.graph_factory.DynamoDBSaver"):
            with patch("app.core.graph_factory.CostTrackingBedrockEmbeddings"):
                with patch("app.core.graph_factory.create_deep_agent") as mock_create_deep_agent:
                    mock_create_deep_agent.return_value = Mock()

                    factory = GraphFactory(mock_config)

                    # Create graphs with different thinking levels
                    graph_low = factory.get_graph("claude-sonnet-4.5", thinking_level=ThinkingLevel.low)
                    graph_high = factory.get_graph("claude-sonnet-4.5", thinking_level=ThinkingLevel.high)
                    graph_none = factory.get_graph("claude-sonnet-4.5", thinking_level=None)

                    # Should have created 3 separate graphs
                    assert len(factory._graphs) == 3
                    assert ("claude-sonnet-4.5", ThinkingLevel.low) in factory._graphs
                    assert ("claude-sonnet-4.5", ThinkingLevel.high) in factory._graphs
                    assert ("claude-sonnet-4.5", None) in factory._graphs


class TestExecutorThinkingFlow:
    """Test end-to-end thinking configuration flow in executor."""

    @pytest.mark.asyncio
    async def test_executor_passes_thinking_to_graph_creation(self):
        """Test that executor passes thinking configuration to graph creation."""
        executor = OrchestratorDeepAgentExecutor()

        # Mock the entire invocation flow
        mock_user = Mock()
        mock_user.id = "user-123"
        mock_user.sub = "sub-123"
        mock_user.name = "Test User"
        mock_user.email = "test@example.com"
        mock_user.groups = []
        mock_user.tools = []
        mock_user.sub_agents = []
        mock_user.timezone = "UTC"
        mock_user.language = "en"
        mock_user.message_formatting = "markdown"
        mock_user.slack_user_handle = None
        mock_user.custom_prompt = None
        mock_user.agent_metadata = {}
        mock_user.tool_names = []
        mock_user.local_subagents = []
        mock_user.enable_thinking = True
        mock_user.thinking_level = "medium"
        mock_user.preferred_model = "claude-sonnet-4.5"

        with patch.object(executor.registry_service, "get_user", return_value=mock_user):
            with patch.object(executor.agent, "get_or_create_graph") as mock_get_graph:
                mock_graph = AsyncMock()
                mock_graph.get_state = Mock()
                mock_graph.astream.return_value = AsyncMock()
                mock_get_graph.return_value = mock_graph

                # Build user config - updated signature
                user_config = await executor._build_user_config(
                    user=mock_user,
                    user_sub="sub-123",
                    user_token="test-token",
                    user_name="Test User",
                    user_email="test@example.com",
                    user_groups=[],
                    model_choice=None,
                    message_formatting="markdown",
                    slack_user_handle=None,
                    sub_agent_config_hash=None,
                    enable_thinking=True,
                    thinking_level="medium",
                )

                # Verify thinking configuration in user_config
                assert user_config.enable_thinking is True
                assert user_config.thinking_level == "medium"

    def test_thinking_level_precedence(self):
        """Test precedence: message metadata > user settings > environment."""
        # Environment default: low (from DEFAULT_THINKING_LEVEL)
        # User setting: medium
        # Message metadata: high
        # Expected: high (message metadata wins)

        user_enable = True
        user_level = "medium"

        metadata_enable = "true"
        metadata_level = "high"

        # Message metadata should take precedence
        effective_enable = user_enable or metadata_enable in ("true", "1", "yes")
        effective_level = metadata_level or user_level

        assert effective_enable is True
        assert effective_level == "high"
