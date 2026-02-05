"""Unit tests for thinking level configuration in model factory."""

from unittest.mock import Mock, patch

from app.core.model_factory import (
    create_model,
    get_thinking_budget,
)
from app.models.base import ThinkingLevel
from app.models.config import AgentSettings


class TestGetThinkingBudget:
    """Test get_thinking_budget() mapping function."""

    def test_minimal_thinking_level(self):
        """Test minimal thinking level returns 1024 tokens."""
        budget = get_thinking_budget(ThinkingLevel.minimal)
        assert budget == 1024

    def test_low_thinking_level(self):
        """Test low thinking level returns 4096 tokens."""
        budget = get_thinking_budget(ThinkingLevel.low)
        assert budget == 4096

    def test_medium_thinking_level(self):
        """Test medium thinking level returns 10000 tokens."""
        budget = get_thinking_budget(ThinkingLevel.medium)
        assert budget == 10000

    def test_high_thinking_level(self):
        """Test high thinking level returns 16000 tokens."""
        budget = get_thinking_budget(ThinkingLevel.high)
        assert budget == 16000


class TestCreateModelWithThinking:
    """Test create_model() with thinking level parameters."""

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_claude_sonnet_with_thinking_minimal(self, mock_chat_bedrock, mock_boto_client):
        """Test Claude Sonnet with minimal thinking level."""
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"
        mock_boto_client.return_value = Mock()

        create_model("claude-sonnet-4.5", config, thinking_level=ThinkingLevel.minimal)

        mock_chat_bedrock.assert_called_once()
        call_kwargs = mock_chat_bedrock.call_args[1]

        assert "thinking" in call_kwargs
        assert call_kwargs["thinking"]["type"] == "enabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 1024
        assert call_kwargs["temperature"] == 1.0

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_claude_sonnet_with_thinking_high(self, mock_chat_bedrock, mock_boto_client):
        """Test Claude Sonnet with high thinking level."""
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"
        mock_boto_client.return_value = Mock()

        create_model("claude-sonnet-4.5", config, thinking_level=ThinkingLevel.high)

        mock_chat_bedrock.assert_called_once()
        call_kwargs = mock_chat_bedrock.call_args[1]

        assert "thinking" in call_kwargs
        assert call_kwargs["thinking"]["type"] == "enabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 16000
        assert call_kwargs["temperature"] == 1.0

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_claude_haiku_with_thinking(self, mock_chat_bedrock, mock_boto_client):
        """Test Claude Haiku with thinking level (now supports Extended Thinking)."""
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"
        mock_boto_client.return_value = Mock()

        create_model("claude-haiku-4-5", config, thinking_level=ThinkingLevel.low)

        mock_chat_bedrock.assert_called_once()
        call_kwargs = mock_chat_bedrock.call_args[1]

        assert "thinking" in call_kwargs
        assert call_kwargs["thinking"]["type"] == "enabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 4096
        assert call_kwargs["temperature"] == 1.0

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_claude_sonnet_without_thinking(self, mock_chat_bedrock, mock_boto_client):
        """Test Claude Sonnet without thinking level (None)."""
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"
        mock_boto_client.return_value = Mock()

        create_model("claude-sonnet-4.5", config, thinking_level=None)

        mock_chat_bedrock.assert_called_once()
        call_kwargs = mock_chat_bedrock.call_args[1]

        assert "thinking" in call_kwargs
        assert call_kwargs["thinking"]["type"] == "disabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 0
        assert call_kwargs["temperature"] == 0.0

    @patch("app.core.model_factory.ChatGoogleGenerativeAI")
    def test_gemini_with_thinking_medium(self, mock_gemini):
        """Test Gemini model with medium thinking level."""
        config = Mock(spec=AgentSettings)

        with patch.dict(
            "os.environ",
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_LOCATION": "us-central1",
            },
        ):
            create_model("gemini-3-pro-preview", config, thinking_level=ThinkingLevel.medium)

        mock_gemini.assert_called_once()
        call_kwargs = mock_gemini.call_args[1]

        assert call_kwargs["thinking_level"] == "medium"
        assert call_kwargs["include_thoughts"] is True
        assert call_kwargs["temperature"] == 1.0

    @patch("app.core.model_factory.ChatGoogleGenerativeAI")
    def test_gemini_without_thinking(self, mock_gemini):
        """Test Gemini model without thinking level."""
        config = Mock(spec=AgentSettings)

        with patch.dict(
            "os.environ",
            {
                "GCP_PROJECT_ID": "test-project",
                "GCP_LOCATION": "us-central1",
            },
        ):
            create_model("gemini-3-pro-preview", config, thinking_level=None)

        mock_gemini.assert_called_once()
        call_kwargs = mock_gemini.call_args[1]

        assert call_kwargs["thinking_level"] is None
        assert call_kwargs["include_thoughts"] is False

    @patch("app.core.model_factory.AzureChatOpenAI")
    def test_gpt4o_ignores_thinking_level(self, mock_azure):
        """Test that GPT-4o ignores thinking level parameter (not supported)."""
        config = Mock(spec=AgentSettings)
        config.get_azure_openai_endpoint.return_value = "https://test.openai.azure.com/"
        config.get_azure_openai_api_key.return_value = "test-key"

        with patch("app.core.model_factory.logger") as mock_logger:
            create_model("gpt4o", config, thinking_level=ThinkingLevel.low)

            # Should log a warning about thinking not being supported
            mock_logger.warning.assert_called_once()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "Thinking mode is only supported" in warning_msg

        # Model should still be created normally
        mock_azure.assert_called_once()


class TestThinkingLevelCaching:
    """Test that models are cached by (model_type, thinking_level) tuple."""

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_different_thinking_levels_create_separate_instances(self, mock_chat_bedrock, mock_boto_client):
        """Test that different thinking levels create separate model instances."""
        from app.core.graph_factory import GraphFactory

        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"
        config.checkpoint_config = Mock()
        config.checkpoint_config.table_name = "test-table"
        config.checkpoint_config.s3_bucket = "test-bucket"
        mock_boto_client.return_value = Mock()

        with patch("app.core.graph_factory.DynamoDBSaver"):
            factory = GraphFactory(config)

            # Create model with low thinking
            model1 = factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.low)

            # Create model with high thinking
            model2 = factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.high)

            # Should have created two separate instances
            assert len(factory._models) == 2
            assert ("claude-sonnet-4.5", ThinkingLevel.low) in factory._models
            assert ("claude-sonnet-4.5", ThinkingLevel.high) in factory._models

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_same_thinking_level_reuses_instance(self, mock_chat_bedrock, mock_boto_client):
        """Test that same thinking level reuses cached model instance."""
        from app.core.graph_factory import GraphFactory

        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"
        config.checkpoint_config = Mock()
        config.checkpoint_config.table_name = "test-table"
        config.checkpoint_config.s3_bucket = "test-bucket"
        mock_boto_client.return_value = Mock()

        with patch("app.core.graph_factory.DynamoDBSaver"):
            factory = GraphFactory(config)

            # Create model with low thinking twice
            model1 = factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.low)
            model2 = factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.low)

            # Should reuse the same instance
            assert model1 is model2
            assert len(factory._models) == 1
