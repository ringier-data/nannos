"""Unit tests for discovery services."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from mcp.types import ListToolsResult, Tool as MCPTool

import app.core.discovery as discovery_module
from app.core.discovery import AgentDiscoveryService, ToolDiscoveryService
from app.models.config import AgentSettings
from agent_common.core.catalogue_ingest import reset_stateless_memo
from agent_common.core.tool_catalogue import LazyMcpTool


def _mcp_tool(name: str, description: str = "") -> MCPTool:
    return MCPTool(name=name, description=description, inputSchema={"type": "object", "properties": {}})


def _mock_mcp_client(tools_by_server=None, on_list=None):
    """A MultiServerMCPClient stand-in whose ``session(name)`` yields a session with ``list_tools``.

    ``tools_by_server`` maps slug -> list[MCPTool] (default: one tool named after the slug);
    ``on_list(server_name)`` is awaited before each list for instrumentation.
    """
    client = Mock()
    client.callbacks = None
    client.tool_interceptors = []

    def session(server_name: str):
        @asynccontextmanager
        async def _cm():
            if on_list is not None:
                await on_list(server_name)
            sess = Mock()
            tools = (tools_by_server or {}).get(server_name, [_mcp_tool(f"tool_{server_name}")])
            sess.list_tools = AsyncMock(return_value=ListToolsResult(tools=tools, nextCursor=None))
            yield sess

        return _cm()

    client.session = session
    return client


def _settings(**overrides):
    config = Mock(spec=AgentSettings)
    config.get_oidc_client_id.return_value = "test_client_id"
    config.get_oidc_client_secret.return_value = Mock()
    config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
    config.get_oidc_issuer.return_value = "https://test.oidc.com"
    config.MCP_GATEWAY_URL = "https://mock-gateway/mcp"
    config.CONSOLE_BACKEND_URL = None
    config.MCP_DISCOVERY_CONCURRENCY = 5
    config.MCP_CATALOGUE_STATELESS_LIST = False
    config.MCP_TOKEN_LEEWAY_SECONDS = 90
    config.MCP_DIRECT_SERVERS = None
    for k, v in overrides.items():
        setattr(config, k, v)
    return config



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
            patch("httpx.AsyncClient") as mock_client,
        ):
            # Mock HTTP response for agent card — A2A v1.0+ ProtoJSON card shape
            # (discovery.py parses it via ParseDict into a real protobuf AgentCard).
            mock_response = Mock()
            mock_response.json.return_value = {
                "name": "Test Agent",
                "description": "Test description",
                "supportedInterfaces": [{"url": "http://test-agent:8000", "protocolBinding": "JSONRPC"}],
                "defaultInputModes": ["text"],
            }
            mock_response.raise_for_status.return_value = None

            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value = mock_http_client

            # Mock A2A runnable — the registry key comes from its tracking_key
            # (card name, spaces stripped)
            mock_runnable_instance = Mock()
            mock_runnable_instance.tracking_key = "TestAgent"
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
    @pytest.mark.parametrize(
        ("gateway_url", "expected_servers_url"),
        [
            # A host ending in one of the stripped characters (m/c/p) is exactly
            # the case the old rstrip("/mcp") corrupted — most commonly any
            # ".com" host (verified against the old code: it produces
            # "https://gateway.example.co", dropping the "m"). This is the one
            # case that actually fails without the fix; the others below don't
            # (nannos.gatana.nannos.ringier.ch ends in "i", not m/c/p — accidentally correct
            # either way — and are kept for the trailing-slash-only regression).
            ("https://gateway.example.com/mcp", "https://gateway.example.com/api/v1/mcp-servers"),
            ("https://nannos.gatana.nannos.ringier.ch/mcp", "https://nannos.gatana.nannos.ringier.ch/api/v1/mcp-servers"),
            # A trailing slash is a no-op for removesuffix("/mcp") unless the
            # slash is stripped first — regression coverage for that fix.
            ("https://gw.example/mcp/", "https://gw.example/api/v1/mcp-servers"),
            ("https://gw.example/mcp", "https://gw.example/api/v1/mcp-servers"),
        ],
    )
    async def test_fetch_available_servers_builds_correct_url(self, gateway_url, expected_servers_url):
        config = Mock(spec=AgentSettings)
        config.MCP_GATEWAY_URL = gateway_url
        service = ToolDiscoveryService(config, oauth2_client=Mock())

        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"servers": []}

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value = mock_http_client
            await service.fetch_available_servers("test_token")

        called_url = mock_http_client.get.call_args[0][0]
        assert called_url == expected_servers_url

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
            mock_client.return_value = _mock_mcp_client()

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
        config.CONSOLE_BACKEND_URL = None
        config.MCP_DISCOVERY_CONCURRENCY = 5
        config.MCP_CATALOGUE_STATELESS_LIST = False
        config.MCP_TOKEN_LEEWAY_SECONDS = 90
        config.MCP_DIRECT_SERVERS = None

        oauth2_client = AsyncMock()
        oauth2_client.exchange_token = AsyncMock(return_value="mcp_token")
        service = ToolDiscoveryService(config, oauth2_client)

        reset_stateless_memo()
        tools = [_mcp_tool("allowed_tool", "This tool is allowed"), _mcp_tool("blocked_tool", "This tool is blocked")]

        with patch("app.core.discovery.MultiServerMCPClient") as mock_client:
            mock_client.return_value = _mock_mcp_client({"mock-server": tools})

            # Mock fetch_available_servers so no real HTTP call is made
            service.fetch_available_servers = AsyncMock(return_value=[{"slug": "mock-server"}])

            token = "test_token"
            white_list = ["allowed_tool"]
            result = await service.discover_tools(token, white_list=white_list)

            assert len(result) == 1
            assert result[0].name == "allowed_tool"
            assert isinstance(result[0], LazyMcpTool)
            assert result[0].metadata["server_name"] == "mock-server"
            assert not result[0].schema_decoded, "discovery must not decode any schema"
            oauth2_client.exchange_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discover_tools_bounds_concurrent_server_fetches(self):
        """Cold discovery must not query every MCP server at once.

        An unbounded fan-out holds every server's session, response body, parsed
        schema and tool objects in memory simultaneously, so the memory peak scales
        with the size of the gateway catalogue. That spike OOMKilled the prod pod
        (2026-08-15). This pins the bound: with 20 servers and a limit of 3, no more
        than 3 fetches may ever be in flight together, and every server is still
        visited exactly once.
        """
        config = Mock(spec=AgentSettings)
        config.get_oidc_client_id.return_value = "test_client_id"
        config.get_oidc_client_secret.return_value = Mock()
        config.get_oidc_client_secret.return_value.get_secret_value.return_value = "test_secret"
        config.get_oidc_issuer.return_value = "https://test.oidc.com"
        config.MCP_GATEWAY_URL = "https://mock-gateway/mcp"
        config.CONSOLE_BACKEND_URL = None
        config.MCP_DISCOVERY_CONCURRENCY = 3
        config.MCP_CATALOGUE_STATELESS_LIST = False
        config.MCP_TOKEN_LEEWAY_SECONDS = 90
        config.MCP_DIRECT_SERVERS = None

        oauth2_client = AsyncMock()
        oauth2_client.exchange_token = AsyncMock(return_value="mcp_token")
        service = ToolDiscoveryService(config, oauth2_client)

        # The semaphore is process-wide and lazily built; clear it so this test's
        # limit applies rather than one cached by an earlier test.
        discovery_module._DISCOVERY_SEMAPHORE = None
        discovery_module._DISCOVERY_SEMAPHORE_LIMIT = None

        servers = [{"slug": f"server-{i}"} for i in range(20)]
        in_flight = 0
        max_in_flight = 0
        visited: list[str] = []

        async def on_list(server_name: str):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            visited.append(server_name)
            # Yield control so overlapping fetches actually interleave; without this
            # the coroutines would run to completion one after another and the test
            # would pass even with an unbounded gather.
            await asyncio.sleep(0)
            in_flight -= 1

        reset_stateless_memo()
        with patch("app.core.discovery.MultiServerMCPClient") as mock_client:
            mock_client.return_value = _mock_mcp_client(on_list=on_list)
            service.fetch_available_servers = AsyncMock(return_value=servers)

            result = await service.discover_tools("test_token")

        # Exactly 3: `<= 3` would also pass under full serialisation, which would
        # hide a bound that throttles far harder than configured.
        assert max_in_flight == 3, f"expected exactly 3 concurrent fetches, saw {max_in_flight}"
        assert sorted(visited) == sorted(s["slug"] for s in servers)
        assert len(result) == 20

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
            mock_mcp_client.return_value = _mock_mcp_client()

            # Run both discoveries concurrently
            import asyncio

            agents, tools = await asyncio.gather(
                agent_service.register_agents({}, token),
                tool_service.discover_tools(token),
            )

            assert isinstance(agents, list)
            assert isinstance(tools, list)
