"""A partial tool discovery serves its turn but is never cached.

One MCP-gateway connect timeout used to leave a console-only toolset in the per-user
discovery cache for the whole TTL: for five minutes the agent told the user "Gmail
draft access isn't available in this workspace".
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from app.core import discovery_cache as dc
from app.core.discovery import DiscoveryReport, ToolDiscoveryService
from app.core.executor import PARTIAL_DISCOVERY_TTL_S, OrchestratorDeepAgentExecutor
from app.models.config import AgentSettings


def _jwt_with_exp(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


class TestTheReportSaysWhatWasMissed:
    @pytest.mark.asyncio
    async def test_an_unreachable_gateway_marks_it_incomplete(self):
        config = Mock(spec=AgentSettings)
        config.MCP_GATEWAY_URL = "https://gateway.example/mcp"
        service = ToolDiscoveryService(config, oauth2_client=Mock())
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectTimeout("connect timeout"))
        report = DiscoveryReport()

        with patch("httpx.AsyncClient") as http:
            http.return_value.__aenter__.return_value = client
            servers = await service.fetch_available_servers("token", report)

        assert servers == []  # still tolerant: the turn goes on with what it has
        assert not report.complete
        assert "ConnectTimeout" in report.reasons[0]

    @pytest.mark.asyncio
    async def test_a_failed_discovery_marks_it_incomplete(self):
        config = Mock(spec=AgentSettings)
        service = ToolDiscoveryService(config, oauth2_client=Mock())
        service.make_token_provider = Mock(side_effect=RuntimeError("token exchange failed"))
        report = DiscoveryReport()

        assert await service.discover_tools("token", report=report) == []
        assert not report.complete

    def test_a_fresh_report_is_complete(self):
        assert DiscoveryReport().complete


class TestOnlyACompleteDiscoveryIsCached:
    @staticmethod
    def _executor(discover_tools) -> OrchestratorDeepAgentExecutor:
        executor = object.__new__(OrchestratorDeepAgentExecutor)
        executor.agent = SimpleNamespace(
            agent_discovery_service=SimpleNamespace(register_agents=AsyncMock(return_value=[])),
            tool_discovery_service=SimpleNamespace(oauth2_client=None, discover_tools=discover_tools),
        )
        return executor

    @staticmethod
    async def _build(executor: OrchestratorDeepAgentExecutor, token: str):
        user = SimpleNamespace(
            id="user-1",
            language="en",
            custom_prompt=None,
            local_subagents=[],
            agent_metadata={},
            tool_names=[],
            catalog_ids=[],
            system_role="member",
            tool_bypass_rules={},
        )
        return await executor._build_user_config(
            user=user,
            user_sub="sub-1",
            user_token=token,
            user_name="A",
            user_email="a@example.com",
            user_groups=[],
            model_choice=None,
            message_formatting="markdown",
            client_user_handle=None,
            sub_agent_config_hash=None,
        )

    def setup_method(self):
        dc._discovery_cache = None

    def teardown_method(self):
        dc._discovery_cache = None

    @pytest.mark.asyncio
    async def test_a_partial_toolset_is_cached_only_briefly(self):
        """Not for the TTL (one timeout hid Gmail for five minutes), not zero either.

        Uncached, a source that keeps failing (a dead server, a dev stack without the
        gateway) cost a full discovery on every turn.
        """

        async def partial(token, white_list=None, token_provider=None, report=None):
            report.mark_incomplete("gateway servers could not be listed (ConnectTimeout)")
            return []

        discover = AsyncMock(side_effect=partial)
        executor = self._executor(discover)
        token = _jwt_with_exp(int(time.time()) + 3600)

        await self._build(executor, token)
        await self._build(executor, token)
        assert discover.await_count == 1  # the second turn reuses the brief entry

        (entry,) = dc._discovery_cache._store.values()
        assert entry.expires_at - time.time() <= PARTIAL_DISCOVERY_TTL_S + 1

    def test_brief_is_shorter_than_the_default_full_ttl(self):
        # Round 11: at 60 s it equalled the default TTL, so one timeout hid tools as long as before.
        assert PARTIAL_DISCOVERY_TTL_S < AgentSettings.AGENT_DISCOVERY_CACHE_TTL

    def test_a_per_entry_ttl_never_outlives_the_cache_ttl(self):
        token = _jwt_with_exp(int(time.time()) + 3600)
        off = dc.TtlTokenCache(0, name="T")
        off.put("k", "partial", token, ttl_seconds=PARTIAL_DISCOVERY_TTL_S)
        assert off.get("k") is None  # caching off stores nothing, partial or not

        short = dc.TtlTokenCache(5, name="T")
        short.put("k", "partial", token, ttl_seconds=PARTIAL_DISCOVERY_TTL_S)
        assert short._store["k"].expires_at - time.time() <= 5

    @pytest.mark.asyncio
    async def test_a_missing_sub_agent_card_makes_it_partial(self):
        from app.core.discovery import AgentDiscoveryService

        service = object.__new__(AgentDiscoveryService)
        service._discover_single_agent = AsyncMock(side_effect=TimeoutError("card fetch timed out"))
        service._log_discovery_error = Mock()
        report = DiscoveryReport()

        assert await service.register_agents({"https://agent.example": {}}, "token", report=report) == []
        assert not report.complete

    @pytest.mark.asyncio
    async def test_a_complete_toolset_is_cached_as_before(self):
        discover = AsyncMock(return_value=[])
        executor = self._executor(discover)
        token = _jwt_with_exp(int(time.time()) + 3600)

        await self._build(executor, token)
        await self._build(executor, token)

        assert discover.await_count == 1
