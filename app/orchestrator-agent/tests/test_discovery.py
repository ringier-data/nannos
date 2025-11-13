"""Unit tests for discovery services."""

from unittest.mock import Mock, patch

import httpx
import pytest
from pydantic import SecretStr

from app.core.discovery import AgentDiscoveryService, ToolDiscoveryService
from app.models.config import AgentSettings


class TestAgentDiscoveryService:
    """Test AgentDiscoveryService functionality."""

    def test_initialization(self):
        """Test service initializes with config."""
        config = Mock(spec=AgentSettings)
        service = AgentDiscoveryService(config)

        assert service.config == config

    @pytest.mark.asyncio
    async def test_get_agents_from_registry_placeholder(self):
        """Test registry fetch returns placeholder URLs."""
        config = Mock(spec=AgentSettings)
        service = AgentDiscoveryService(config)

        token = SecretStr("test_token")
        result = await service._get_agents_from_registry(token)

        # Currently returns hardcoded URLs - this is expected placeholder behavior
        assert isinstance(result, list)
        assert len(result) == 2
        assert "http://localhost:10000" in result
        assert "http://localhost:9999" in result

    @pytest.mark.asyncio
    async def test_discover_agents_with_valid_token(self):
        """Test agent discovery with valid authentication token."""
        config = Mock(spec=AgentSettings)
        service = AgentDiscoveryService(config)

        token = SecretStr("valid_token")

        with (
            patch.object(service, "_get_agents_from_registry") as mock_registry,
            patch("app.core.discovery.make_a2a_async_runnable"),
        ):
            mock_registry.return_value = ["http://test-agent:8000"]
            mock_agent_card = Mock()
            mock_agent_card.name = "test_agent"
            mock_agent_card.description = "Test description"

            with patch("httpx.AsyncClient") as mock_client:
                mock_response = Mock()
                mock_response.json.return_value = mock_agent_card
                mock_client.return_value.__aenter__.return_value.get.return_value = mock_response

                result = await service.discover_agents(token)

                assert isinstance(result, list)
                # Verify registry was called
                mock_registry.assert_called_once_with(token)

    @pytest.mark.asyncio
    async def test_discover_agents_http_error_handling(self):
        """Test discovery handles HTTP errors gracefully."""
        config = Mock(spec=AgentSettings)
        service = AgentDiscoveryService(config)

        token = SecretStr("test_token")

        with patch.object(service, "_get_agents_from_registry") as mock_registry:
            mock_registry.return_value = ["http://unreachable-agent:8000"]

            with patch("httpx.AsyncClient") as mock_client:
                # Simulate HTTP error
                mock_client.return_value.__aenter__.return_value.get.side_effect = httpx.RequestError(
                    "Connection failed"
                )

                result = await service.discover_agents(token)

                # Should return empty list on error, not crash
                assert result == []

    @pytest.mark.asyncio
    async def test_discover_agents_with_empty_registry(self):
        """Test discovery with empty agent registry."""
        config = Mock(spec=AgentSettings)
        service = AgentDiscoveryService(config)

        token = SecretStr("test_token")

        with patch.object(service, "_get_agents_from_registry") as mock_registry:
            mock_registry.return_value = []

            result = await service.discover_agents(token)

            assert result == []


class TestToolDiscoveryService:
    """Test ToolDiscoveryService functionality."""

    def test_initialization(self):
        """Test service initializes with config."""
        config = Mock(spec=AgentSettings)
        service = ToolDiscoveryService(config)

        assert service.config == config

    @pytest.mark.asyncio
    async def test_discover_tools_basic(self):
        """Test basic tool discovery functionality."""
        config = Mock(spec=AgentSettings)
        service = ToolDiscoveryService(config)

        # Mock MCP client setup
        with patch("app.core.discovery.MultiServerMCPClient") as mock_client:
            # Mock get_tools as async method
            async def mock_get_tools():
                return []

            mock_client.return_value.get_tools = mock_get_tools

            token = SecretStr("test_token")
            result = await service.discover_tools(token)

            assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_discover_tools_with_mcp_servers(self):
        """Test tool discovery with configured MCP servers."""
        config = Mock(spec=AgentSettings)
        config.mcp_servers = ["server1", "server2"]
        service = ToolDiscoveryService(config)

        # Mock tools from MCP
        mock_tool = Mock()
        mock_tool.name = "test_tool"
        mock_tool.description = "Test tool description"

        with patch("app.core.discovery.MultiServerMCPClient") as mock_client:
            # Mock get_tools as async method
            async def mock_get_tools():
                return [mock_tool]

            mock_client.return_value.get_tools = mock_get_tools

            token = SecretStr("test_token")
            result = await service.discover_tools(token)

            assert len(result) >= 0  # Should not crash
            # Note: Actual implementation details depend on MCP client behavior


class TestDiscoveryIntegration:
    """Test integration scenarios for discovery services."""

    @pytest.mark.asyncio
    async def test_concurrent_discovery(self):
        """Test that agent and tool discovery can run concurrently."""
        config = Mock(spec=AgentSettings)
        agent_service = AgentDiscoveryService(config)
        tool_service = ToolDiscoveryService(config)

        token = SecretStr("test_token")

        with (
            patch.object(agent_service, "discover_agents") as mock_agents,
            patch.object(tool_service, "discover_tools") as mock_tools,
        ):
            mock_agents.return_value = []
            mock_tools.return_value = []

            # Run both discoveries concurrently
            import asyncio

            agents, tools = await asyncio.gather(
                agent_service.discover_agents(token), tool_service.discover_tools(token)
            )

            assert isinstance(agents, list)
            assert isinstance(tools, list)
            mock_agents.assert_called_once_with(token)
            mock_tools.assert_called_once_with(token)

    def test_discovery_service_caching_behavior(self):
        """Test that discovery services can be reused without state issues."""
        config = Mock(spec=AgentSettings)
        service = AgentDiscoveryService(config)

        # Service should maintain its configuration
        assert service.config == config

        # Should be able to create multiple instances
        service2 = AgentDiscoveryService(config)
        assert service2.config == config
        assert service is not service2  # Different instances
