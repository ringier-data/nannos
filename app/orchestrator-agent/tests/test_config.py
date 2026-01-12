"""Unit tests for configuration models and settings."""

import pytest

from app.models.config import AgentSettings, ResponseFormat


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


class TestAgentSettings:
    """Tests for AgentSettings configuration."""

    def test_retry_configuration(self):
        """Test retry configuration constants."""
        assert AgentSettings.MAX_RETRIES == 3
        assert AgentSettings.BACKOFF_FACTOR == 3.0

    def test_cache_configuration(self):
        """Test cache configuration."""
        assert AgentSettings.AGENT_DISCOVERY_CACHE_TTL == 30

    def test_dynamodb_configuration(self):
        """Test DynamoDB checkpoint configuration."""
        assert AgentSettings.CHECKPOINT_DYNAMODB_TABLE_NAME == "dev-alloy-infrastructure-agents-langgraph-checkpoints"
        assert AgentSettings.CHECKPOINT_TTL_DAYS == 14
        assert AgentSettings.CHECKPOINT_AWS_REGION == "eu-central-1"
        assert AgentSettings.CHECKPOINT_MAX_RETRIES == 5

    def test_system_instruction_not_empty(self):
        """Test that system instruction is defined."""
        assert len(AgentSettings.SYSTEM_INSTRUCTION) > 0
        assert "orchestrator agent" in AgentSettings.SYSTEM_INSTRUCTION.lower()
