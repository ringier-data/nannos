"""Orchestrator discovery wiring over the shared tool catalogue (agent_common.core.tool_catalogue)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.tools import BaseTool
from mcp.types import ListToolsResult, Tool as MCPTool

from agent_common.core.catalogue_ingest import (
    StatelessListError,
    StatelessListUnsupported,
    reset_stateless_memo,
)
from app.core.discovery import ToolDiscoveryService
from agent_common.core.tool_catalogue import (
    LazyMcpTool,
    build_server_catalogue,
    make_catalogue_tool,
)
from app.models.config import AgentSettings

SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "query"}},
    "required": ["q"],
}


"""Discovery wiring for the catalogue ingest paths (fast path, fallback, cache sharing)."""

# --------------------------------------------------------------------------------------
# Discovery wiring: fast path, fallback, cache sharing
# --------------------------------------------------------------------------------------


def _settings(stateless: bool) -> Mock:
    config = Mock(spec=AgentSettings)
    config.MCP_GATEWAY_URL = "https://gw/mcp"
    config.CONSOLE_BACKEND_URL = None
    config.MCP_DISCOVERY_CONCURRENCY = 5
    config.MCP_CATALOGUE_STATELESS_LIST = stateless
    config.MCP_TOKEN_LEEWAY_SECONDS = 90
    config.MCP_DIRECT_SERVERS = None
    return config


def _mcp_client(list_calls: list[str]) -> Mock:
    client = Mock()
    client.callbacks = None
    client.tool_interceptors = []

    def session(server_name: str):
        @asynccontextmanager
        async def _cm():
            list_calls.append(server_name)
            sess = Mock()
            sess.list_tools = AsyncMock(
                return_value=ListToolsResult(tools=[MCPTool(name=f"mcp_{server_name}", inputSchema=SCHEMA)], nextCursor=None)
            )
            yield sess

        return _cm()

    client.session = session
    return client


def _fast_catalogue(slug: str):
    return build_server_catalogue(
        slug, [make_catalogue_tool(server_name=slug, name=f"fast_{slug}", description="", input_schema=SCHEMA)], source="stateless"
    )


async def _discover(config: Mock, client: Mock, servers: list[dict]) -> list[BaseTool]:
    oauth2 = AsyncMock()
    oauth2.exchange_token = AsyncMock(return_value="gw_token")
    service = ToolDiscoveryService(config, oauth2)
    service.fetch_available_servers = AsyncMock(return_value=servers)
    with patch("app.core.discovery.MultiServerMCPClient", return_value=client):
        return await service.discover_tools("user_token")


class TestDiscoveryIngestPaths:
    def setup_method(self):
        reset_stateless_memo()

    @pytest.mark.asyncio
    async def test_flag_off_uses_sdk_session_only(self):
        list_calls: list[str] = []
        with patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", new_callable=AsyncMock) as fast:
            tools = await _discover(_settings(stateless=False), _mcp_client(list_calls), [{"slug": "s1"}])
        fast.assert_not_awaited()
        assert list_calls == ["s1"]
        assert [t.name for t in tools] == ["mcp_s1"]

    @pytest.mark.asyncio
    async def test_stateless_path_uses_the_servers_own_connection(self):
        """Same URL and token the SDK would use → same per-user listing, no handshake."""
        list_calls: list[str] = []
        with patch(
            "agent_common.core.catalogue_ingest.fetch_catalogue_stateless", new_callable=AsyncMock, side_effect=lambda *a, **kw: _fast_catalogue(kw["server_slug"])
        ) as fast:
            tools = await _discover(_settings(stateless=True), _mcp_client(list_calls), [{"slug": "s1"}, {"slug": "s2"}])
        assert list_calls == [], "no SDK session on the fast path"
        assert sorted(t.name for t in tools) == ["fast_s1", "fast_s2"]
        for call in fast.await_args_list:
            slug = call.kwargs["server_slug"]
            assert call.kwargs["url"] == f"https://gw/mcp?includeOnlyServerSlugs={slug}"
            assert call.kwargs["headers"] == {"Authorization": "Bearer gw_token"}
        assert all(isinstance(t, LazyMcpTool) and not t.schema_decoded for t in tools)

    @pytest.mark.asyncio
    async def test_each_user_owns_their_own_listing(self):
        """A listing is a per-user view (profiles filter tools per user): fetched per user and
        never shared between users' tools, even when the two views happen to be identical."""
        list_calls: list[str] = []
        with patch(
            "agent_common.core.catalogue_ingest.fetch_catalogue_stateless", new_callable=AsyncMock, side_effect=lambda *a, **kw: _fast_catalogue(kw["server_slug"])
        ) as fast:
            a = await _discover(_settings(stateless=True), _mcp_client(list_calls), [{"slug": "s1"}])
            b = await _discover(_settings(stateless=True), _mcp_client(list_calls), [{"slug": "s1"}])
        assert fast.await_count == 2, "per-user listing: fetched for each user"
        assert a[0].catalogue_entry is not b[0].catalogue_entry, "no process-wide catalogue registry"
        assert a[0].catalogue_entry.schema_bytes == b[0].catalogue_entry.schema_bytes
        assert a[0] is not b[0], "per-user tool objects (they carry the user's connection)"

    @pytest.mark.asyncio
    async def test_refusal_falls_back_and_is_remembered_per_url(self):
        list_calls: list[str] = []
        with patch(
            "agent_common.core.catalogue_ingest.fetch_catalogue_stateless", new_callable=AsyncMock, side_effect=StatelessListUnsupported("400 no session")
        ) as fast:
            tools = await _discover(_settings(stateless=True), _mcp_client(list_calls), [{"slug": "s1"}])
            await _discover(_settings(stateless=True), _mcp_client(list_calls), [{"slug": "s1"}])
        assert fast.await_count == 1, "the URL is remembered as unsupported; never probed again"
        assert list_calls == ["s1", "s1"]
        assert [t.name for t in tools] == ["mcp_s1"]

    @pytest.mark.asyncio
    async def test_transient_error_falls_back_for_that_server_only(self):
        list_calls: list[str] = []

        async def fast(client, *, url, headers, server_slug):
            if server_slug == "s1":
                raise StatelessListError("502")
            return _fast_catalogue(server_slug)

        with patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=fast):
            tools = await _discover(_settings(stateless=True), _mcp_client(list_calls), [{"slug": "s1"}, {"slug": "s2"}])
        assert list_calls == ["s1"]
        assert sorted(t.name for t in tools) == ["fast_s2", "mcp_s1"]

    @pytest.mark.asyncio
    async def test_http_client_follows_redirects(self):
        """console-backend mounts /mcp with a 307 → /mcp/; the fast path must follow it like the SDK does."""
        seen: dict = {}

        async def fast(client, *, url, headers, server_slug):
            seen["follow_redirects"] = client.follow_redirects
            return _fast_catalogue(server_slug)

        with patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=fast):
            await _discover(_settings(stateless=True), _mcp_client([]), [{"slug": "s1"}])
        assert seen["follow_redirects"] is True

    @pytest.mark.asyncio
    async def test_console_server_takes_the_fast_path_too(self):
        list_calls: list[str] = []
        config = _settings(stateless=True)
        config.CONSOLE_BACKEND_URL = "https://console"
        config.CONSOLE_BACKEND_CLIENT_ID = "agent-console"
        with patch(
            "agent_common.core.catalogue_ingest.fetch_catalogue_stateless", new_callable=AsyncMock, side_effect=lambda *a, **kw: _fast_catalogue(kw["server_slug"])
        ) as fast:
            tools = await _discover(config, _mcp_client(list_calls), [{"slug": "s1"}])
        assert sorted(c.kwargs["server_slug"] for c in fast.await_args_list) == ["console", "s1"]
        console_call = next(c for c in fast.await_args_list if c.kwargs["server_slug"] == "console")
        assert console_call.kwargs["url"] == "https://console/mcp"
        assert list_calls == []
        assert sorted(t.name for t in tools) == ["fast_console", "fast_s1"]
