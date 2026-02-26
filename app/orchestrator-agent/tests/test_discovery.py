"""Unit tests for discovery services."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.core.discovery import AgentDiscoveryService, ToolDiscoveryService
from app.models.config import AgentSettings


class TestAgentDiscoveryService:
    """Test AgentDiscoveryService functionality."""

    def test_initialization(self):
        """Test service initializes with config."""
        config = Mock(spec=AgentSettings)
        oauth2_client = Mock()
        service = AgentDiscoveryService(config, oauth2_client)

        assert service.config == config
        assert service.oauth2_client == oauth2_client

    @pytest.mark.asyncio
    async def test_register_agents_with_valid_urls(self):
        """Test agent registration with valid agent URLs."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client = Mock()
        service = AgentDiscoveryService(config, oauth2_client)
        agent_metadata = {
            "http://test-agent:8000": {
                "sub_agent_id": "test-id",
                "name": "Test Agent",
                "description": "Test description",
            }
        }
        token = "valid_token"

        with (
            patch("app.core.discovery.make_a2a_async_runnable") as mock_runnable,
            patch("app.core.discovery.AgentCard") as mock_agent_card_cls,
            patch("httpx.AsyncClient") as mock_client,
        ):
            # Mock AgentCard instance
            mock_agent_card = Mock()
            mock_agent_card.name = "Test Agent"
            mock_agent_card.description = "Test description"
            mock_agent_card.url = "http://test-agent:8000"
            mock_agent_card.default_input_modes = ["text"]  # Add for multimodal check
            mock_agent_card_cls.return_value = mock_agent_card

            # Mock HTTP response for agent card
            mock_response = Mock()
            mock_response.json.return_value = {
                "name": "Test Agent",
                "description": "Test description",
                "url": "http://test-agent:8000",
            }
            mock_response.raise_for_status.return_value = None

            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value = mock_http_client

            # Mock A2A runnable
            mock_runnable_instance = Mock()
            mock_runnable.return_value = mock_runnable_instance

            result = await service.register_agents(agent_metadata, token)

            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["name"] == "TestAgent"

    @pytest.mark.asyncio
    async def test_register_agents_http_error_handling(self):
        """Test registration handles HTTP errors gracefully."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client = Mock()
        service = AgentDiscoveryService(config, oauth2_client)
        agent_metadata = {
            "http://unreachable-agent:8000": {
                "sub_agent_id": "test-id",
                "name": "Unreachable Agent",
                "description": "Test description",
            }
        }
        token = "test_token"

        with patch("httpx.AsyncClient") as mock_client:
            # Simulate HTTP error
            mock_http_client = AsyncMock()
            mock_http_client.get.side_effect = httpx.RequestError("Connection failed", request=Mock())
            mock_client.return_value.__aenter__.return_value = mock_http_client

            result = await service.register_agents(agent_metadata, token)

            # Should return empty list on error, not crash
            assert result == []

    @pytest.mark.asyncio
    async def test_register_agents_with_empty_url_list(self):
        """Test registration with empty agent URL list."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client = Mock()
        service = AgentDiscoveryService(config, oauth2_client)
        agent_metadata = {}
        token = "test_token"

        result = await service.register_agents(agent_metadata, token)

        assert result == []

    @pytest.mark.asyncio
    async def test_register_agents_with_middleware(self):
        """Test agent registration with streaming middleware."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client = Mock()
        service = AgentDiscoveryService(config, oauth2_client)
        agent_metadata = {
            "http://test-agent:8000": {
                "sub_agent_id": "test-id",
                "name": "Test Agent",
                "description": "Test description",
            }
        }
        token = "valid_token"
        middleware = Mock()
        middleware.register_streaming_runnable = Mock()

        with (
            patch("app.core.discovery.make_a2a_async_runnable") as mock_runnable,
            patch("app.core.discovery.AgentCard") as mock_agent_card_cls,
            patch("httpx.AsyncClient") as mock_client,
        ):
            # Mock AgentCard instance
            mock_agent_card = Mock()
            mock_agent_card.name = "Test Agent"
            mock_agent_card.description = "Test description"
            mock_agent_card.url = "http://test-agent:8000"
            mock_agent_card.default_input_modes = ["text", "image"]  # Add for multimodal check
            mock_agent_card_cls.return_value = mock_agent_card

            # Mock HTTP response for agent card
            mock_response = Mock()
            mock_response.json.return_value = {
                "name": "Test Agent",
                "description": "Test description",
                "url": "http://test-agent:8000",
            }
            mock_response.raise_for_status.return_value = None

            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value = mock_http_client

            # Mock A2A runnable with streaming
            mock_runnable_instance = Mock()
            mock_runnable_instance._streaming_runnable = Mock()
            mock_runnable.return_value = mock_runnable_instance

            result = await service.register_agents(agent_metadata, token, streaming_middleware=middleware)

            assert len(result) == 1
            middleware.register_streaming_runnable.assert_called_once()


class TestToolDiscoveryService:
    """Test ToolDiscoveryService functionality."""

    def test_initialization(self):
        """Test service initializes with config."""
        config = Mock(spec=AgentSettings)
        oauth2_client = Mock()
        service = ToolDiscoveryService(config, oauth2_client)

        assert service.config == config
        assert service.oauth2_client == oauth2_client

    @pytest.mark.asyncio
    async def test_discover_tools_basic(self):
        """Test basic tool discovery functionality."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client = AsyncMock()
        oauth2_client.exchange_token = AsyncMock(return_value="mcp_token")
        service = ToolDiscoveryService(config, oauth2_client)

        with patch("app.core.discovery.MultiServerMCPClient") as mock_client:
            # Mock MCP client
            mock_client_instance = Mock()
            mock_client_instance.get_tools = AsyncMock(return_value=[])
            mock_client.return_value = mock_client_instance

            token = "test_token"
            result = await service.discover_tools(token)

            assert isinstance(result, list)
            assert len(result) == 0
            oauth2_client.exchange_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discover_tools_with_white_list(self):
        """Test tool discovery with white list filtering."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"
        config.MCP_GATEWAY_URL = "https://mock-gateway/mcp"

        oauth2_client = AsyncMock()
        oauth2_client.exchange_token = AsyncMock(return_value="mcp_token")
        service = ToolDiscoveryService(config, oauth2_client)

        # Mock tools from MCP
        mock_tool1 = Mock()
        mock_tool1.name = "allowed_tool"
        mock_tool1.description = "This tool is allowed"
        mock_tool1.metadata = None

        mock_tool2 = Mock()
        mock_tool2.name = "blocked_tool"
        mock_tool2.description = "This tool is blocked"
        mock_tool2.metadata = None

        with patch("app.core.discovery.MultiServerMCPClient") as mock_client:
            mock_client_instance = Mock()
            # Called as: await client.get_tools(server_name=slug)
            mock_client_instance.get_tools = AsyncMock(return_value=[mock_tool1, mock_tool2])
            mock_client.return_value = mock_client_instance

            # Mock fetch_available_servers so no real HTTP call is made
            service.fetch_available_servers = AsyncMock(return_value=[{"slug": "mock-server"}])

            token = "test_token"
            white_list = ["allowed_tool"]
            result = await service.discover_tools(token, white_list=white_list)

            assert len(result) == 1
            assert result[0].name == "allowed_tool"
            oauth2_client.exchange_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discover_tools_error_handling(self):
        """Test tool discovery handles errors gracefully."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client = AsyncMock()
        oauth2_client.exchange_token = AsyncMock(side_effect=Exception("Token exchange failed"))
        service = ToolDiscoveryService(config, oauth2_client)

        token = "test_token"
        result = await service.discover_tools(token)

        # Should return empty list on error
        assert result == []
        oauth2_client.exchange_token.assert_awaited_once()


class TestDiscoveryIntegration:
    """Test integration scenarios for discovery services."""

    @pytest.mark.asyncio
    async def test_concurrent_discovery(self):
        """Test that agent and tool discovery can run concurrently."""
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"

        oauth2_client_agents = Mock()
        oauth2_client_tools = AsyncMock()
        oauth2_client_tools.exchange_token = AsyncMock(return_value="mcp_token")

        agent_service = AgentDiscoveryService(config, oauth2_client_agents)
        tool_service = ToolDiscoveryService(config, oauth2_client_tools)

        token = "test_token"

        with patch("app.core.discovery.MultiServerMCPClient") as mock_mcp_client:
            # Mock MCP client
            mock_mcp_instance = Mock()
            mock_mcp_instance.get_tools = AsyncMock(return_value=[])
            mock_mcp_client.return_value = mock_mcp_instance

            # Run both discoveries concurrently
            import asyncio

            agents, tools = await asyncio.gather(
                agent_service.register_agents({}, token),
                tool_service.discover_tools(token),
            )

            assert isinstance(agents, list)
            assert isinstance(tools, list)
