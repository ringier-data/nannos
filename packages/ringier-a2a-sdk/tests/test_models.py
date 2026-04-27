"""Tests for common models."""

import pytest
from a2a.types import TaskState
from pydantic import SecretStr, ValidationError

from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig


class TestAgentStreamResponse:
    """Tests for AgentStreamResponse model."""

    def test_create_working_response(self):
        """Test creating a working state response."""
        response = AgentStreamResponse(state=TaskState.working, content="Processing your request...")

        assert response.state == TaskState.working
        assert response.content == "Processing your request..."
        assert response.metadata is None

    def test_create_completed_response(self):
        """Test creating a completed state response."""
        response = AgentStreamResponse(state=TaskState.completed, content="Task completed successfully")

        assert response.state == TaskState.completed
        assert response.content == "Task completed successfully"

    def test_create_response_with_metadata(self):
        """Test creating response with metadata."""
        metadata = {"task_id": "task-123", "context_id": "ctx-456", "execution_time": 2.5}

        response = AgentStreamResponse(state=TaskState.completed, content="Done", metadata=metadata)

        assert response.metadata == metadata
        assert response.metadata["task_id"] == "task-123"
        assert response.metadata["execution_time"] == 2.5

    def test_response_requires_state_and_content(self):
        """Test that state and content are required."""
        with pytest.raises(ValidationError) as exc_info:
            AgentStreamResponse()

        errors = exc_info.value.errors()
        error_fields = {error["loc"][0] for error in errors}
        assert "state" in error_fields
        assert "content" in error_fields

    def test_response_serialization(self):
        """Test response can be serialized to dict."""
        response = AgentStreamResponse(state=TaskState.working, content="Test", metadata={"key": "value"})

        data = response.model_dump()
        assert data["state"] == TaskState.working
        assert data["content"] == "Test"
        assert data["metadata"]["key"] == "value"


class TestUserConfig:
    """Tests for UserConfig model."""

    def test_create_basic_user_config(self):
        """Test creating basic user config."""
        config = UserConfig(
            user_sub="sub-123", access_token=SecretStr("secret-token"), name="John Doe", email="john@example.com"
        )

        assert config.user_sub == "sub-123"
        assert config.access_token.get_secret_value() == "secret-token"
        assert config.name == "John Doe"
        assert config.email == "john@example.com"
        assert config.language == "en"  # Default value

    def test_user_config_with_custom_language(self):
        """Test creating user config with custom language."""
        config = UserConfig(
            user_sub="sub-123",
            access_token=SecretStr("token"),
            name="Jane Doe",
            email="jane@example.com",
            language="de",
        )

        assert config.language == "de"

    def test_user_config_with_sub_agents(self):
        """Test creating user config with sub-agents."""
        sub_agents = [
            {"name": "jira-agent", "url": "http://jira-agent:8000"},
            {"name": "currency-agent", "url": "http://currency:8000"},
        ]

        config = UserConfig(
            user_sub="sub-123",
            access_token=SecretStr("token"),
            name="Test User",
            email="test@example.com",
            sub_agents=sub_agents,
        )

        assert config.sub_agents == sub_agents
        assert len(config.sub_agents) == 2

    def test_user_config_with_tools(self):
        """Test creating user config with tools."""
        tools = [{"name": "calculator", "type": "function"}, {"name": "web_search", "type": "api"}]

        config = UserConfig(
            user_sub="sub-123",
            access_token=SecretStr("token"),
            name="Test User",
            email="test@example.com",
            tools=tools,
        )

        assert config.tools == tools
        assert len(config.tools) == 2

    def test_user_config_requires_mandatory_fields(self):
        """Test that mandatory fields are required."""
        with pytest.raises(ValidationError) as exc_info:
            UserConfig()

        errors = exc_info.value.errors()
        error_fields = {error["loc"][0] for error in errors}
        assert "user_sub" in error_fields
        assert "name" in error_fields
        assert "email" in error_fields
        assert "access_token" not in error_fields

    def test_user_config_access_token_is_secret(self):
        """Test that access_token is properly protected as SecretStr."""
        config = UserConfig(
            user_sub="sub-123", access_token=SecretStr("my-secret-token"), name="Test User", email="test@example.com"
        )

        # SecretStr should mask the value in string representation
        assert "my-secret-token" not in str(config)

        # But can be accessed via get_secret_value()
        assert config.access_token.get_secret_value() == "my-secret-token"

    def test_user_config_serialization_masks_token(self):
        """Test that serialization masks the access token."""
        config = UserConfig(
            user_sub="sub-123", access_token=SecretStr("secret-token"), name="Test User", email="test@example.com"
        )

        # Model dump should mask the token
        data = config.model_dump()
        assert data["access_token"] != "secret-token"  # Should be masked

        # JSON serialization should also mask it
        json_str = config.model_dump_json()
        assert "secret-token" not in json_str

    def test_user_config_optional_fields_default_to_none(self):
        """Test that optional fields default to None."""
        config = UserConfig(
            user_sub="sub-123", access_token=SecretStr("token"), name="Test User", email="test@example.com"
        )

        assert config.sub_agents is None
        assert config.tools is None
        assert config.phone_number is None

    def test_user_config_with_phone_number(self):
        """Test creating user config with phone_number."""
        config = UserConfig(
            user_sub="sub-123",
            access_token=SecretStr("token"),
            name="Test User",
            email="test@example.com",
            phone_number="+41791234567",
        )

        assert config.phone_number == "+41791234567"
