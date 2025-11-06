"""Unit tests for configuration models and settings."""

import os
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.models.config import UserConfig, ResponseFormat, AgentSettings


class TestResponseFormat:
    """Tests for ResponseFormat model."""
    
    def test_response_format_creation(self):
        """Test creating a ResponseFormat instance."""
        fmt = ResponseFormat(type="json")
        assert fmt.type == "json"
    
    def test_response_format_validation(self):
        """Test ResponseFormat validation."""
        with pytest.raises(Exception):
            ResponseFormat()  # Missing required field


class TestUserConfig:
    """Tests for UserConfig model."""
    
    def test_user_config_creation(self):
        """Test creating a UserConfig instance with all fields."""
        config = UserConfig(
            whoami="test_user",
            token=SecretStr("secret_token"),
            name="John",
            last_name="Doe",
            language="en",
            sub_agents=[],
            tools=[]
        )
        
        assert config.whoami == "test_user"
        assert config.token.get_secret_value() == "secret_token"
        assert config.name == "John"
        assert config.last_name == "Doe"
        assert config.language == "en"
        assert config.sub_agents == []
        assert config.tools == []
    
    def test_user_config_defaults(self):
        """Test UserConfig with default values."""
        config = UserConfig(
            whoami="test_user",
            token=SecretStr("secret_token"),
            name="John",
            last_name="Doe"
        )
        
        assert config.language == "en"
        assert config.sub_agents is None
        assert config.tools is None
    
    def test_user_config_token_secrecy(self):
        """Test that token is properly handled as secret."""
        config = UserConfig(
            whoami="test_user",
            token=SecretStr("secret_token"),
            name="John",
            last_name="Doe"
        )
        
        # Token should not appear in repr
        assert "secret_token" not in repr(config)
        # But should be accessible via get_secret_value()
        assert config.token.get_secret_value() == "secret_token"


class TestAgentSettings:
    """Tests for AgentSettings configuration."""

    def test_azure_settings(self):
        """Test Azure OpenAI settings."""
        with patch.dict(os.environ, {
            'AZURE_OPENAI_CHAT_DEPLOYMENT': 'test_deployment',
            'AZURE_OPENAI_CHAT_MODEL_NAME': 'gpt-4'
        }):
            assert AgentSettings.get_azure_deployment() == 'test_deployment'
            assert AgentSettings.get_azure_model_name() == 'gpt-4'
    
    def test_gatana_api_key(self):
        """Test Gatana API key retrieval."""
        with patch.dict(os.environ, {'GATANA_API_KEY': 'gatana_key'}):
            assert AgentSettings.get_gatana_api_key() == 'gatana_key'
    
    def test_retry_configuration(self):
        """Test retry configuration constants."""
        assert AgentSettings.MAX_RETRIES == 3
        assert AgentSettings.BACKOFF_FACTOR == 2.0
    
    def test_cache_configuration(self):
        """Test cache configuration."""
        assert AgentSettings.AGENT_DISCOVERY_CACHE_TTL == 30
    
    def test_dynamodb_configuration(self):
        """Test DynamoDB checkpoint configuration."""
        assert AgentSettings.CHECKPOINT_TABLE_NAME == "dev-alloy-infrastructure-agents-langgraph-checkpoints"
        assert AgentSettings.CHECKPOINT_TTL_DAYS == 14
        assert AgentSettings.CHECKPOINT_AWS_REGION == "eu-central-1"
        assert AgentSettings.CHECKPOINT_MAX_RETRIES == 5
    
    def test_system_instruction_not_empty(self):
        """Test that system instruction is defined."""
        assert len(AgentSettings.SYSTEM_INSTRUCTION) > 0
        assert "orchestrator agent" in AgentSettings.SYSTEM_INSTRUCTION.lower()
    
