"""Tests for the registry service."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.a2a_utils.models import LocalLangGraphSubAgentConfig
from app.core.registry import RegistryConfig, RegistryService, User


@pytest.fixture
def registry_config():
    """Create test registry configuration."""
    return RegistryConfig(playground_backend_url="http://test-backend:5001")


@pytest.fixture
def registry_service(registry_config):
    """Create registry service instance."""
    return RegistryService(config=registry_config)


class TestRegistryService:
    """Test RegistryService class."""

    @pytest.mark.asyncio
    async def test_get_user_success_with_remote_agents(self, registry_service):
        """Test get_user successfully fetches and converts remote sub-agents."""
        mock_sub_agents_response = {
            "items": [
                {
                    "id": 1,
                    "name": "jira-agent",
                    "description": "JIRA integration agent",
                    "owner_user_id": "test-user",
                    "type": "remote",
                    "current_version": 1,
                    "default_version": 1,
                    "config_version": {
                        "id": 1,
                        "sub_agent_id": 1,
                        "version": 1,
                        "description": "JIRA integration agent",
                        "model": None,
                        "agent_url": "https://jira-agent.example.com/a2a",
                        "system_prompt": None,
                        "mcp_tools": [],
                        "status": "approved",
                        "created_at": "2024-01-01T00:00:00",
                    },
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            ],
            "total": 1,
        }
        mock_settings_response = {
            "data": {
                "user_id": "test-user-id",
                "language": "de",
                "custom_prompt": "Always be concise.",
            }
        }

        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()

            # Create response objects for each endpoint
            mock_sub_agents_resp = MagicMock()
            mock_sub_agents_resp.status_code = 200
            mock_sub_agents_resp.json.return_value = mock_sub_agents_response

            mock_settings_resp = MagicMock()
            mock_settings_resp.status_code = 200
            mock_settings_resp.json.return_value = mock_settings_response

            # Return different responses based on the URL
            async def mock_get(url, **kwargs):
                if url == "/api/v1/sub-agents":
                    return mock_sub_agents_resp
                elif url == "/api/v1/auth/me/settings":
                    return mock_settings_resp
                raise ValueError(f"Unexpected URL: {url}")

            mock_client.get = mock_get
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 1
            assert "https://jira-agent.example.com/a2a" in user.agent_metadata
            assert len(user.local_subagents) == 0
            assert user.language == "de"
            assert user.custom_prompt == "Always be concise."

    @pytest.mark.asyncio
    async def test_get_user_success_with_local_agents(self, registry_service):
        """Test get_user successfully fetches and converts local sub-agents."""
        mock_response = {
            "items": [
                {
                    "id": 2,
                    "name": "data-analyst",
                    "description": "Analyzes data and generates insights",
                    "owner_user_id": "test-user",
                    "type": "local",
                    "current_version": 1,
                    "default_version": 1,
                    "config_version": {
                        "id": 2,
                        "sub_agent_id": 2,
                        "version": 1,
                        "description": "Analyzes data and generates insights",
                        "model": "gpt-4",
                        "agent_url": None,
                        "system_prompt": "You are a data analysis expert.",
                        "mcp_url": "https://mcp.example.com",
                        "mcp_tools": [],
                        "status": "approved",
                        "created_at": "2024-01-01T00:00:00",
                    },
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            ],
            "total": 1,
        }

        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 200
            mock_response_obj.json.return_value = mock_response
            mock_client.get = AsyncMock(return_value=mock_response_obj)
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 0
            assert len(user.local_subagents) == 1

            local_agent = user.local_subagents[0]
            assert local_agent.name == "data-analyst"
            assert local_agent.description == "Analyzes data and generates insights"
            assert local_agent.system_prompt == "You are a data analysis expert."
            assert local_agent.mcp_tools is None  # No tools specified
            assert local_agent.model_name == "gpt-4"

    @pytest.mark.asyncio
    async def test_get_user_success_with_mixed_agents(self, registry_service):
        """Test get_user successfully fetches both remote and local sub-agents."""
        mock_response = {
            "items": [
                {
                    "id": 1,
                    "name": "jira-agent",
                    "description": "JIRA integration",
                    "owner_user_id": "test-user",
                    "type": "remote",
                    "current_version": 1,
                    "default_version": 1,
                    "config_version": {
                        "id": 1,
                        "sub_agent_id": 1,
                        "version": 1,
                        "description": "JIRA integration",
                        "model": None,
                        "agent_url": "https://jira.example.com/a2a",
                        "system_prompt": None,
                        "mcp_tools": [],
                        "status": "approved",
                        "created_at": "2024-01-01T00:00:00",
                    },
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                },
                {
                    "id": 2,
                    "name": "slack-agent",
                    "description": "Slack integration",
                    "owner_user_id": "test-user",
                    "type": "remote",
                    "current_version": 1,
                    "default_version": 1,
                    "config_version": {
                        "id": 2,
                        "sub_agent_id": 2,
                        "version": 1,
                        "description": "Slack integration",
                        "model": None,
                        "agent_url": "https://slack.example.com/a2a",
                        "system_prompt": None,
                        "mcp_tools": [],
                        "status": "approved",
                        "created_at": "2024-01-01T00:00:00",
                    },
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                },
                {
                    "id": 3,
                    "name": "code-reviewer",
                    "description": "Reviews code",
                    "owner_user_id": "test-user",
                    "type": "local",
                    "current_version": 1,
                    "default_version": 1,
                    "config_version": {
                        "id": 3,
                        "sub_agent_id": 3,
                        "version": 1,
                        "description": "Reviews code",
                        "model": None,
                        "agent_url": None,
                        "system_prompt": "Review code for issues.",
                        "mcp_tools": [],
                        "status": "approved",
                        "created_at": "2024-01-01T00:00:00",
                    },
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                },
            ],
            "total": 3,
        }

        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 200
            mock_response_obj.json.return_value = mock_response
            mock_client.get = AsyncMock(return_value=mock_response_obj)
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert len(user.agent_metadata) == 2
            assert len(user.local_subagents) == 1

    @pytest.mark.asyncio
    async def test_get_user_no_access_token(self, registry_service):
        """Test get_user returns empty user when no access token provided."""
        user = await registry_service.get_user("test-user-id", access_token=None)

        assert user is not None
        assert user.id == "test-user-id"
        assert len(user.agent_metadata) == 0
        assert len(user.local_subagents) == 0

    @pytest.mark.asyncio
    async def test_get_user_authentication_failure(self, registry_service):
        """Test get_user handles 401 authentication failure."""
        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 401
            mock_client.get = AsyncMock(return_value=mock_response_obj)
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="invalid-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 0

    @pytest.mark.asyncio
    async def test_get_user_server_error(self, registry_service):
        """Test get_user handles server errors gracefully."""
        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 500
            mock_response_obj.text = "Internal Server Error"
            mock_client.get = AsyncMock(return_value=mock_response_obj)
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 0

    @pytest.mark.asyncio
    async def test_get_user_timeout_error(self, registry_service):
        """Test get_user handles timeout errors gracefully."""
        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Request timed out"))
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 0

    @pytest.mark.asyncio
    async def test_get_user_connection_error(self, registry_service):
        """Test get_user handles connection errors gracefully."""
        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Failed to connect"))
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 0

    @pytest.mark.asyncio
    async def test_get_user_empty_response(self, registry_service):
        """Test get_user handles empty sub-agents list."""
        mock_response = {"items": [], "total": 0}

        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 200
            mock_response_obj.json.return_value = mock_response
            mock_client.get = AsyncMock(return_value=mock_response_obj)
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert len(user.agent_metadata) == 0
            assert len(user.local_subagents) == 0

    @pytest.mark.asyncio
    async def test_get_user_settings_fetch_failure_uses_defaults(self, registry_service):
        """Test that settings fetch failure falls back to defaults gracefully."""
        mock_sub_agents_response = {"items": [], "total": 0}

        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()

            # Sub-agents succeeds, settings fails
            mock_sub_agents_resp = MagicMock()
            mock_sub_agents_resp.status_code = 200
            mock_sub_agents_resp.json.return_value = mock_sub_agents_response

            mock_settings_resp = MagicMock()
            mock_settings_resp.status_code = 500  # Server error

            async def mock_get(url, **kwargs):
                if url == "/api/v1/sub-agents":
                    return mock_sub_agents_resp
                elif url == "/api/v1/auth/me/settings":
                    return mock_settings_resp
                raise ValueError(f"Unexpected URL: {url}")

            mock_client.get = mock_get
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            # Should use defaults when settings fetch fails
            assert user.language == "en"
            assert user.custom_prompt is None

    @pytest.mark.asyncio
    async def test_get_user_settings_with_null_custom_prompt(self, registry_service):
        """Test that null custom_prompt in settings response is handled correctly."""
        mock_sub_agents_response = {"items": [], "total": 0}
        mock_settings_response = {
            "data": {
                "user_id": "test-user-id",
                "language": "fr",
                "custom_prompt": None,
            }
        }

        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()

            mock_sub_agents_resp = MagicMock()
            mock_sub_agents_resp.status_code = 200
            mock_sub_agents_resp.json.return_value = mock_sub_agents_response

            mock_settings_resp = MagicMock()
            mock_settings_resp.status_code = 200
            mock_settings_resp.json.return_value = mock_settings_response

            async def mock_get(url, **kwargs):
                if url == "/api/v1/sub-agents":
                    return mock_sub_agents_resp
                elif url == "/api/v1/auth/me/settings":
                    return mock_settings_resp
                raise ValueError(f"Unexpected URL: {url}")

            mock_client.get = mock_get
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user("test-user-id", access_token="test-token")

            assert user is not None
            assert user.language == "fr"
            assert user.custom_prompt is None

    @pytest.mark.asyncio
    async def test_close(self, registry_service):
        """Test close properly closes the HTTP client."""
        # Create the client first
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.is_closed = False
            mock_client_class.return_value = mock_client

            await registry_service._get_client()
            registry_service._client = mock_client

            await registry_service.close()

            mock_client.aclose.assert_called_once()


class TestRegistryConfig:
    """Test RegistryConfig class."""

    def test_default_config(self):
        """Test default configuration values."""
        config = RegistryConfig()
        # Default should be localhost for local development
        assert "localhost" in config.playground_backend_url or "5001" in config.playground_backend_url

    def test_custom_config(self):
        """Test custom configuration values."""
        config = RegistryConfig(playground_backend_url="https://api.example.com")
        assert config.playground_backend_url == "https://api.example.com"


class TestUserModel:
    """Test User model."""

    def test_user_defaults(self):
        """Test User model default values."""
        user = User(id="test-id")

        assert user.id == "test-id"
        assert user.agent_metadata == {}
        assert user.tool_names == []
        assert user.language == "en"
        assert user.custom_prompt is None
        assert user.local_subagents == []

    def test_user_with_values(self):
        """Test User model with all values set."""
        local_agent = LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="test-agent",
            description="Test agent",
            system_prompt="You are a test agent.",
        )

        user = User(
            id="test-id",
            agent_metadata={
                "https://agent1.example.com": {"sub_agent_id": 1, "name": "Agent 1", "description": "First agent"},
                "https://agent2.example.com": {"sub_agent_id": 2, "name": "Agent 2", "description": "Second agent"},
            },
            tool_names=["tool1", "tool2"],
            language="de",
            custom_prompt="Always be helpful and concise.",
            local_subagents=[local_agent],
        )

        assert user.id == "test-id"
        assert len(user.agent_metadata) == 2
        assert len(user.tool_names) == 2
        assert user.language == "de"
        assert user.custom_prompt == "Always be helpful and concise."
        assert len(user.local_subagents) == 1
