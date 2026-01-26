"""Test timeout configuration for AWS Bedrock client."""

import os
from unittest.mock import Mock, patch

from app.core.model_factory import create_model
from app.models.config import AgentSettings


class TestBedrockTimeoutConfiguration:
    """Test that Bedrock client is configured with proper timeouts."""

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_bedrock_client_uses_default_timeout_values(self, mock_chat_bedrock, mock_boto_client):
        """Test that boto3 client uses default timeout values when env vars not set."""
        # Setup
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"

        mock_bedrock_client = Mock()
        mock_boto_client.return_value = mock_bedrock_client

        # Execute
        with patch.dict(os.environ, {}, clear=True):
            _ = create_model("claude-sonnet-4.5", config, thinking=False)

        # Verify boto3.client was called with proper configuration
        mock_boto_client.assert_called_once()
        call_args = mock_boto_client.call_args

        # Check service name and region
        assert call_args[0][0] == "bedrock-runtime"
        assert call_args[1]["region_name"] == "eu-central-1"

        # Check default timeout configuration
        boto_config = call_args[1]["config"]
        assert boto_config.read_timeout == 300  # 5 minutes (default)
        assert boto_config.connect_timeout == 10  # 10 seconds (default)

        # Check default retry configuration
        assert boto_config.retries["max_attempts"] == 3  # default
        assert boto_config.retries["mode"] == "adaptive"  # default

        # Verify ChatBedrockConverse was initialized with the client
        mock_chat_bedrock.assert_called_once()
        bedrock_call_kwargs = mock_chat_bedrock.call_args[1]
        assert bedrock_call_kwargs["client"] == mock_bedrock_client
        assert bedrock_call_kwargs["model"] == "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_bedrock_client_respects_environment_variables(self, mock_chat_bedrock, mock_boto_client):
        """Test that boto3 client respects custom timeout values from environment."""
        # Setup
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"

        mock_bedrock_client = Mock()
        mock_boto_client.return_value = mock_bedrock_client

        # Execute with custom environment variables
        custom_env = {
            "BEDROCK_READ_TIMEOUT": "600",  # 10 minutes
            "BEDROCK_CONNECT_TIMEOUT": "15",  # 15 seconds
            "BEDROCK_MAX_RETRY_ATTEMPTS": "5",  # 5 retries
            "BEDROCK_RETRY_MODE": "standard",  # standard mode
        }
        with patch.dict(os.environ, custom_env, clear=True):
            _ = create_model("claude-sonnet-4.5", config, thinking=False)

        # Verify custom configuration was used
        call_args = mock_boto_client.call_args
        boto_config = call_args[1]["config"]

        assert boto_config.read_timeout == 600  # Custom value
        assert boto_config.connect_timeout == 15  # Custom value
        assert boto_config.retries["max_attempts"] == 5  # Custom value
        assert boto_config.retries["mode"] == "standard"  # Custom value

    @patch("app.core.model_factory.boto3.client")
    @patch("app.core.model_factory.ChatBedrockConverse")
    def test_bedrock_client_thinking_mode_configuration(self, mock_chat_bedrock, mock_boto_client):
        """Test that thinking mode passes through correctly with timeout config."""
        # Setup
        config = Mock(spec=AgentSettings)
        config.get_bedrock_region.return_value = "eu-central-1"

        mock_boto_client.return_value = Mock()

        # Execute
        _ = create_model("claude-sonnet-4.5", config, thinking=True)

        # Verify thinking parameters are set
        mock_chat_bedrock.assert_called_once()
        bedrock_call_kwargs = mock_chat_bedrock.call_args[1]
        assert bedrock_call_kwargs["temperature"] == 1.0
        assert "thinking" in bedrock_call_kwargs["additional_model_request_fields"]
        assert bedrock_call_kwargs["additional_model_request_fields"]["thinking"]["type"] == "enabled"
        assert bedrock_call_kwargs["additional_model_request_fields"]["thinking"]["budget_tokens"] == 1024

    @patch("app.core.model_factory.AzureChatOpenAI")
    def test_azure_model_no_boto_client_created(self, mock_azure):
        """Test that Azure models don't create boto3 clients."""
        # Setup
        config = Mock(spec=AgentSettings)

        # Execute
        with patch("app.core.model_factory.boto3.client") as mock_boto_client:
            _ = create_model("gpt4o", config, thinking=False)

            # Verify boto3 client was NOT created
            mock_boto_client.assert_not_called()

            # Verify Azure client was created
            mock_azure.assert_called_once()
