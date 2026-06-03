"""Tests for the registry service."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from agent_common.a2a.models import LocalLangGraphSubAgentConfig

from app.core.registry import RegistryConfig, RegistryService, User


@pytest.fixture
def registry_config():
    """Create test registry configuration."""
    return RegistryConfig(console_backend_url="http://test-backend:5001")


@pytest.fixture
def registry_service(registry_config):
    """Create registry service instance."""
    return RegistryService(config=registry_config)


@pytest.fixture
def mock_registry_service(registry_service):
    """Mock the _get_client method to return a mock HTTP client."""

    @contextmanager
    def yield_mock_client(mock_sub_agents_response, mock_settings_response):
        with patch.object(registry_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_sub_agents_resp = MagicMock()
            mock_sub_agents_resp.status_code = 200
            mock_sub_agents_resp.json.return_value = mock_sub_agents_response
            mock_settings_resp = MagicMock()
            mock_settings_resp.status_code = 200
            mock_settings_resp.json.return_value = mock_settings_response
            mock_catalogs_resp = MagicMock()
            mock_catalogs_resp.status_code = 200
            mock_catalogs_resp.json.return_value = {"items": []}
            mock_me_resp = MagicMock()
            mock_me_resp.status_code = 200
            mock_me_resp.json.return_value = {"role": "member"}

            async def mock_get(url, **kwargs):
                if url == "/api/v1/sub-agents/activated":
                    return mock_sub_agents_resp
                elif url == "/api/v1/auth/me/settings":
                    return mock_settings_resp
                elif url == "/api/v1/catalogs":
                    return mock_catalogs_resp
                elif url == "/api/v1/auth/me":
                    return mock_me_resp
                raise ValueError(f"Unexpected URL: {url}")

            mock_client.get = mock_get
            mock_get_client.return_value = mock_client

            yield registry_service

    return yield_mock_client


class TestRegistryService:
    """Test RegistryService class."""

    @pytest.mark.asyncio
    async def test_get_user_success_with_remote_agents(self, mock_registry_service):
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
                "sub": "test-user-sub",
                "language": "de",
                "custom_prompt": "Always be concise.",
                "timezone": "Europe/Zurich",
                "mcp_tools": [],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        }

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert user.sub == "test-user-sub"
            assert len(user.agent_metadata) == 1
            assert "https://jira-agent.example.com/a2a" in user.agent_metadata
            assert len(user.local_subagents) == 0
            assert user.language == "de"
            assert user.custom_prompt == "Always be concise."

    @pytest.mark.asyncio
    async def test_get_user_success_with_local_agents(self, mock_registry_service):
        """Test get_user successfully fetches and converts local sub-agents."""
        mock_sub_agents_response = {
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
                        "model": "gpt-4o",
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
        mock_settings_response = {
            "data": {
                "user_id": "test-user-id",
                "sub": "test-user-sub",
                "language": "en",
                "custom_prompt": None,
                "timezone": "Europe/Zurich",
                "mcp_tools": [],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        }

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert user.sub == "test-user-sub"
            assert len(user.agent_metadata) == 0
            assert len(user.local_subagents) == 1

            local_agent = user.local_subagents[0]
            assert local_agent.name == "data-analyst"
            assert local_agent.description == "Analyzes data and generates insights"
            assert local_agent.system_prompt == "You are a data analysis expert."
            assert local_agent.mcp_tools is None  # No tools specified
            assert local_agent.model_name == "gpt-4o"

    @pytest.mark.asyncio
    async def test_get_user_success_with_mixed_agents(self, mock_registry_service):
        """Test get_user successfully fetches both remote and local sub-agents."""
        mock_sub_agents_response = {
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
        mock_settings_response = {
            "data": {
                "user_id": "test-user-id",
                "sub": "test-user-sub",
                "language": "en",
                "custom_prompt": None,
                "timezone": "Europe/Zurich",
                "mcp_tools": [],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        }

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert user.sub == "test-user-sub"
            assert len(user.agent_metadata) == 2
            assert len(user.local_subagents) == 1

    @pytest.mark.asyncio
    async def test_get_user_no_access_token(self, registry_service):
        """Test get_user returns None when no access token provided."""
        user = await registry_service.get_user(user_sub="test-user-sub", access_token=None)

        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_authentication_failure(self, mock_registry_service):
        """Test get_user handles 401 authentication failure."""
        mock_sub_agents_response = {}  # Not used in 401 case
        mock_settings_response = {}  # Not used in 401 case

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            # Override the mock to return 401 for both endpoints
            mock_client = await registry_service._get_client()
            mock_401_resp = MagicMock()
            mock_401_resp.status_code = 401

            async def mock_get_401(url, **kwargs):
                return mock_401_resp

            mock_client.get = mock_get_401

            user = await registry_service.get_user(user_sub="test-user-sub", access_token="invalid-token")

            # When sub-agents fetch returns 401, get_user returns None
            # because _fetch_user_settings will fail to get user_id
            assert user is None

    @pytest.mark.asyncio
    async def test_get_user_server_error(self, mock_registry_service):
        """Test get_user handles server errors gracefully."""
        mock_sub_agents_response = {}  # Not used in 500 case
        mock_settings_response = {}  # Not used in 500 case

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            # Override the mock to return 500 for both endpoints
            mock_client = await registry_service._get_client()
            mock_500_resp = MagicMock()
            mock_500_resp.status_code = 500
            mock_500_resp.text = "Internal Server Error"

            async def mock_get_500(url, **kwargs):
                return mock_500_resp

            mock_client.get = mock_get_500

            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            # When settings fetch fails with 500, get_user returns None
            assert user is None

    @pytest.mark.asyncio
    async def test_get_user_timeout_error(self, registry_service, caplog):
        """Test get_user handles timeout errors gracefully."""
        with patch.object(registry_service, "_get_client") as mock_get_client, caplog.at_level("ERROR"):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Request timed out"))
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            # Timeout should return None
            assert user is None
            assert "Timeout fetching sub-agents for user sub test-user-sub" in caplog.text

    @pytest.mark.asyncio
    async def test_get_user_connection_error(self, registry_service, caplog):
        """Test get_user handles connection errors gracefully."""
        with patch.object(registry_service, "_get_client") as mock_get_client, caplog.at_level("ERROR"):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Failed to connect"))
            mock_get_client.return_value = mock_client

            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            # Connection error should return None
            assert user is None
            assert "Request error fetching sub-agents for user sub test-user-sub: Failed to connect" in caplog.text

    @pytest.mark.asyncio
    async def test_get_user_empty_response(self, mock_registry_service):
        """Test get_user handles empty sub-agents list."""
        mock_sub_agents_response = {"items": [], "total": 0}
        mock_settings_response = {
            "data": {
                "user_id": "test-user-id",
                "sub": "test-user-sub",
                "language": "en",
                "custom_prompt": None,
                "timezone": "Europe/Zurich",
                "mcp_tools": [],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        }

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert user.sub == "test-user-sub"
            assert len(user.agent_metadata) == 0
            assert len(user.local_subagents) == 0

    @pytest.mark.asyncio
    async def test_get_user_settings_fetch_failure_uses_defaults(self, mock_registry_service):
        """Test that settings fetch failure returns None (cannot construct User without settings)."""
        mock_sub_agents_response = {"items": [], "total": 0}
        mock_settings_response = {}  # Will be overridden

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            # Override to make settings fail
            mock_client = await registry_service._get_client()
            original_get = mock_client.get

            mock_settings_resp = MagicMock()
            mock_settings_resp.status_code = 500

            async def mock_get(url, **kwargs):
                if url == "/api/v1/sub-agents":
                    return await original_get(url, **kwargs)
                elif url == "/api/v1/auth/me/settings":
                    return mock_settings_resp
                raise ValueError(f"Unexpected URL: {url}")

            mock_client.get = mock_get

            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            # When settings fetch fails, get_user returns None (cannot get user_id)
            assert user is None

    @pytest.mark.asyncio
    async def test_get_user_settings_with_null_custom_prompt(self, mock_registry_service):
        """Test that null custom_prompt in settings response is handled correctly."""
        mock_sub_agents_response = {"items": [], "total": 0}
        mock_settings_response = {
            "data": {
                "user_id": "test-user-id",
                "sub": "test-user-sub",
                "language": "fr",
                "custom_prompt": None,
                "timezone": "Europe/Zurich",
                "mcp_tools": [],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        }

        with mock_registry_service(mock_sub_agents_response, mock_settings_response) as registry_service:
            user = await registry_service.get_user(user_sub="test-user-sub", access_token="test-token")

            assert user is not None
            assert user.id == "test-user-id"
            assert user.sub == "test-user-sub"
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
        assert "localhost" in config.console_backend_url or "5001" in config.console_backend_url

    def test_custom_config(self):
        """Test custom configuration values."""
        config = RegistryConfig(console_backend_url="https://api.example.com")
        assert config.console_backend_url == "https://api.example.com"


class TestUserModel:
    """Test User model."""

    def test_user_defaults(self):
        """Test User model default values."""
        user = User(id="test-id", sub="test-sub")

        assert user.id == "test-id"
        assert user.sub == "test-sub"
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
            sub="test-sub",
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
        assert user.sub == "test-sub"
        assert len(user.agent_metadata) == 2
        assert len(user.tool_names) == 2
        assert user.language == "de"
        assert user.custom_prompt == "Always be helpful and concise."
        assert len(user.local_subagents) == 1


class TestPersistBypassRules:
    """Tests for RegistryService.persist_bypass_rules."""

    @pytest.mark.asyncio
    async def test_persist_bypass_all_rule(self, registry_service):
        """Test persisting a bypass_all rule calls the correct API."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.put = AsyncMock(return_value=mock_response)

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[
                    {"key": "read_file::github", "rule": {"bypass_all": True}},
                ],
            )

        mock_client.put.assert_called_once_with(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "read_file",
                "server_slug": "github",
                "bypass_all": True,
            },
            headers={"Authorization": "Bearer test-token"},
        )

    @pytest.mark.asyncio
    async def test_persist_bypass_patterns_rule(self, registry_service):
        """Test persisting a bypass_patterns rule."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.put = AsyncMock(return_value=mock_response)

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[
                    {
                        "key": "write_file::_self",
                        "rule": {"bypass_patterns": {"path": ["/tmp/*"]}},
                    },
                ],
            )

        mock_client.put.assert_called_once_with(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "write_file",
                "server_slug": "_self",
                "bypass_patterns": {"path": ["/tmp/*"]},
            },
            headers={"Authorization": "Bearer test-token"},
        )

    @pytest.mark.asyncio
    async def test_persist_multiple_rules(self, registry_service):
        """Test persisting multiple rules makes multiple API calls."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.put = AsyncMock(return_value=mock_response)

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[
                    {"key": "tool_a::server_a", "rule": {"bypass_all": True}},
                    {"key": "tool_b::server_b", "rule": {"bypass_all": True}},
                ],
            )

        assert mock_client.put.call_count == 2

    @pytest.mark.asyncio
    async def test_persist_empty_rules_is_noop(self, registry_service):
        """Test that empty pending_rules doesn't make any API calls."""
        mock_client = AsyncMock()

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[],
            )

        mock_client.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_handles_api_failure_gracefully(self, registry_service):
        """Test that API failures are logged but don't raise."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.put = AsyncMock(return_value=mock_response)

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            # Should not raise
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[
                    {"key": "tool_a::server_a", "rule": {"bypass_all": True}},
                ],
            )

    @pytest.mark.asyncio
    async def test_persist_handles_network_error_gracefully(self, registry_service):
        """Test that network errors are caught and don't propagate."""
        mock_client = AsyncMock()
        mock_client.put = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            # Should not raise
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[
                    {"key": "tool_a::server_a", "rule": {"bypass_all": True}},
                ],
            )

    @pytest.mark.asyncio
    async def test_persist_key_without_server_slug(self, registry_service):
        """Test persisting a rule where key has no :: separator defaults to _self."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.put = AsyncMock(return_value=mock_response)

        with patch.object(registry_service, "_get_client", return_value=mock_client):
            await registry_service.persist_bypass_rules(
                access_token="test-token",
                pending_rules=[
                    {"key": "some_tool", "rule": {"bypass_all": True}},
                ],
            )

        mock_client.put.assert_called_once_with(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "some_tool",
                "server_slug": "_self",
                "bypass_all": True,
            },
            headers={"Authorization": "Bearer test-token"},
        )
