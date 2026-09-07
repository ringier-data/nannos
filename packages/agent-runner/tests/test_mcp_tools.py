"""agent.mcp_tools — shared catalogue + call-time bearer tokens for scheduled runs.

Mirrors ``agent-common/tests/test_dynamic_agent.py::
test_with_a_token_provider_tools_are_token_free_and_mint_per_call`` for the runner.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agent_common.core.catalogue_ingest import (
    StatelessListError,
    StatelessListUnsupported,
    reset_stateless_memo,
    stateless_supported,
)
from agent_common.core.token_provider import UserTokenProvider
from agent_common.core.tool_catalogue import LazyMcpTool

from agent.mcp_tools import McpToolResolver

GATEWAY_URL = "https://gateway.example/mcp"
CONSOLE_URL = "https://console.example/mcp"


def _jwt(aud: str, ttl: float = 900) -> str:
    def seg(o: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg({'exp': time.time() + ttl, 'aud': aud})}.sig"


def _tools_list_reply(names: list[str]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": n,
                    "description": f"{n} does things",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                }
                for n in names
            ]
        },
    }


class Req(SimpleNamespace):
    def override(self, **kw: Any) -> Req:
        return Req(**{**self.__dict__, **kw})


@pytest.fixture(autouse=True)
def _fresh_memo():
    reset_stateless_memo()
    yield
    reset_stateless_memo()


@pytest.fixture
def exchanges() -> list[str]:
    return []


@pytest.fixture
def provider(exchanges: list[str]) -> UserTokenProvider:
    minted: dict[str, str] = {}

    async def exchange(*, subject_token: str, target_client_id: str, requested_scopes: list[str]) -> str:
        assert subject_token == "user-token"
        exchanges.append(target_client_id)
        return minted.setdefault(target_client_id, _jwt(target_client_id))

    return UserTokenProvider("user-token", exchange)


def _resolver(provider: UserTokenProvider, **kw: Any) -> McpToolResolver:
    return McpToolResolver(
        token_provider=provider,
        gateway_url=GATEWAY_URL,
        gateway_client_id="gatana",
        console_mcp_url=CONSOLE_URL,
        console_client_id="agent-console",
        timeout=timedelta(seconds=5),
        **kw,
    )


def _serve(catalogues: dict[str, list[str]], seen: list[httpx.Request]):
    """An httpx transport answering stateless tools/list per URL with the given tool names."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert json.loads(request.content)["method"] == "tools/list"
        return httpx.Response(200, json=_tools_list_reply(catalogues[str(request.url)]))

    return httpx.MockTransport(handler)


def _patch_http(transport: httpx.MockTransport):
    real = httpx.AsyncClient
    return patch("agent.mcp_tools.httpx.AsyncClient", lambda **kw: real(transport=transport, **kw))


class TestStatelessListing:
    @pytest.mark.asyncio
    async def test_tools_are_token_free_lazy_and_mint_per_call(self, provider, exchanges):
        """The listing carries the exchanged bearer; the tools it yields carry none and an
        interceptor mints one per call (memoised until leeway, so one exchange per audience)."""
        seen: list[httpx.Request] = []
        transport = _serve({GATEWAY_URL: ["github_search", "jira_list"], CONSOLE_URL: ["console_create_skill"]}, seen)
        with _patch_http(transport):
            tools = {t.name: t for t in await _resolver(provider).resolve(["github_search", "console_create_skill"])}

        assert set(tools) == {"github_search", "console_create_skill"}, "whitelist filtering kept"
        assert exchanges == ["gatana", "agent-console"], "one exchange per audience, via the provider"
        listed = {str(r.url): r.headers["Authorization"] for r in seen}
        assert listed[GATEWAY_URL].startswith("Bearer ") and listed[CONSOLE_URL].startswith("Bearer ")

        for tool in tools.values():
            assert isinstance(tool, LazyMcpTool)
            assert not tool.schema_decoded, "schemas decode lazily"
            assert not (tool._connection.get("headers") or {}), "no Authorization header on the tool connection"
            assert tool._interceptors and len(tool._interceptors) == 1

        seen_headers: dict[str, str] = {}

        async def handler(req: Req) -> str:
            seen_headers.update(req.headers)
            return "ok"

        await tools["github_search"]._interceptors[0](Req(server_name="gateway", headers=None), handler)
        assert seen_headers["Authorization"].startswith("Bearer ") and exchanges == ["gatana", "agent-console"], (
            "memoised"
        )
        await tools["console_create_skill"]._interceptors[0](Req(server_name="console", headers={"X": "1"}), handler)
        assert seen_headers["X"] == "1" and exchanges == ["gatana", "agent-console"]

        assert stateless_supported(GATEWAY_URL) is True
        assert stateless_supported(CONSOLE_URL) is True

    @pytest.mark.asyncio
    async def test_no_mcp_types_retained_after_discovery(self, provider):
        transport = _serve({GATEWAY_URL: ["github_search"]}, [])
        with _patch_http(transport):
            (tool,) = await _resolver(provider).resolve(["github_search"])
        import gc

        import mcp.types

        gc.collect()
        assert not [o for o in gc.get_objects() if isinstance(o, mcp.types.Tool)]
        assert tool.catalogue_entry.decode_schema()["properties"] == {"q": {"type": "string"}}

    @pytest.mark.asyncio
    async def test_leeway_above_token_lifetime_forces_one_exchange_per_call(self, provider, exchanges):
        """The MCP_TOKEN_LEEWAY_SECONDS QA lever from #170: memoised tokens never count as fresh."""
        provider._leeway = 100_000
        with _patch_http(_serve({GATEWAY_URL: ["github_search"]}, [])):
            (tool,) = await _resolver(provider).resolve(["github_search"])

        async def handler(req: Req) -> str:
            return "ok"

        await tool._interceptors[0](Req(server_name="gateway", headers=None), handler)
        await tool._interceptors[0](Req(server_name="gateway", headers=None), handler)
        assert exchanges == ["gatana"] * 4, "up-front + listing + one exchange per call"

    @pytest.mark.asyncio
    async def test_only_needed_servers_are_listed(self, provider, exchanges):
        seen: list[httpx.Request] = []
        with _patch_http(_serve({GATEWAY_URL: ["github_search"]}, seen)):
            resolver = _resolver(provider)
            tools = await resolver.resolve(["github_search", "not_offered"])
        assert [t.name for t in tools] == ["github_search"]
        assert {str(r.url) for r in seen} == {GATEWAY_URL}, "console not touched"
        assert exchanges == ["gatana"]
        assert resolver.stats["unresolved"] == ["not_offered"]

    @pytest.mark.asyncio
    async def test_every_run_lists_with_its_own_token_and_never_binds_another_users_view(self, provider):
        """A Gatana profile can hide tools per user: a name offered by another run's listing must
        not be bound for a user whose own listing does not offer it."""
        seen: list[httpx.Request] = []
        with _patch_http(_serve({GATEWAY_URL: ["github_search", "jira_list"]}, seen)):
            await _resolver(provider).resolve(["github_search", "jira_list"])
        assert len(seen) == 1

        async def exchange_b(**kw: Any) -> str:
            return _jwt("gatana")

        with _patch_http(_serve({GATEWAY_URL: ["github_search"]}, seen)):  # user B's view lacks jira_list
            resolver = _resolver(UserTokenProvider("user-b-token", exchange_b))
            tools = await resolver.resolve(["github_search", "jira_list"])
        assert len(seen) == 2, "the second run listed again with its own token"
        assert [t.name for t in tools] == ["github_search"]
        assert resolver.stats["unresolved"] == ["jira_list"]

    @pytest.mark.asyncio
    async def test_scheduler_tools_are_console_tools(self, provider, exchanges):
        seen: list[httpx.Request] = []
        with _patch_http(_serve({CONSOLE_URL: ["scheduler_list_jobs"]}, seen)):
            (tool,) = await _resolver(provider).resolve(["scheduler_list_jobs"])
        assert {str(r.url) for r in seen} == {CONSOLE_URL} and exchanges == ["agent-console"]
        assert tool._connection["url"] == CONSOLE_URL

    @pytest.mark.asyncio
    async def test_a_failing_exchange_fails_discovery_even_after_an_earlier_listing(self, provider):
        with _patch_http(_serve({GATEWAY_URL: ["github_search"]}, [])):
            await _resolver(provider).resolve(["github_search"])

        async def broken(**kw: Any) -> str:
            raise RuntimeError("invalid_grant")

        with pytest.raises(RuntimeError, match="invalid_grant"):
            await _resolver(UserTokenProvider("user-token", broken)).resolve(["github_search"])

    @pytest.mark.asyncio
    async def test_listing_honours_the_run_mcp_timeout(self, provider):
        captured: dict[str, Any] = {}
        real = httpx.AsyncClient

        def factory(**kw: Any) -> httpx.AsyncClient:
            captured.update(kw)
            return real(transport=_serve({GATEWAY_URL: ["github_search"]}, []), **kw)

        with patch("agent.mcp_tools.httpx.AsyncClient", factory):
            await _resolver(provider).resolve(["github_search"])
        assert captured["timeout"] == 5.0 and captured["follow_redirects"] is True


class TestSdkFallback:
    @pytest.mark.asyncio
    async def test_refusal_marks_endpoint_and_falls_back_to_sdk(self, provider, caplog):
        fallback = AsyncMock()
        with (
            patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=StatelessListUnsupported("HTTP 400")),
            patch("agent_common.core.catalogue_ingest.fetch_catalogue_mcp", fallback),
            patch("agent.mcp_tools.MultiServerMCPClient") as client_cls,
        ):
            from agent_common.core.tool_catalogue import build_server_catalogue, make_catalogue_tool

            fallback.return_value = build_server_catalogue(
                "gateway",
                [
                    make_catalogue_tool(
                        server_name="gateway", name="github_search", description="", input_schema={"type": "object"}
                    )
                ],
                source="mcp",
            )
            resolver = _resolver(provider)
            (tool,) = await resolver.resolve(["github_search"])
            listing_connection = client_cls.call_args.args[0]["gateway"]

        assert stateless_supported(GATEWAY_URL) is False
        assert "refuses stateless tools/list" in caplog.text
        assert listing_connection["headers"]["Authorization"].startswith("Bearer "), "the SDK listing used the bearer"
        assert not (tool._connection.get("headers") or {}), "the tool still has no credential"
        assert resolver.stats["source"] == {"gateway": "mcp"}

    @pytest.mark.asyncio
    async def test_transient_error_falls_back_without_marking_endpoint(self, provider):
        from agent_common.core.tool_catalogue import build_server_catalogue, make_catalogue_tool

        cat = build_server_catalogue(
            "gateway",
            [
                make_catalogue_tool(
                    server_name="gateway", name="github_search", description="", input_schema={"type": "object"}
                )
            ],
            source="mcp",
        )
        with (
            patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=StatelessListError("502")),
            patch("agent_common.core.catalogue_ingest.fetch_catalogue_mcp", AsyncMock(return_value=cat)),
            patch("agent.mcp_tools.MultiServerMCPClient"),
        ):
            (tool,) = await _resolver(provider).resolve(["github_search"])
        assert tool.name == "github_search"
        assert stateless_supported(GATEWAY_URL) is None

    @pytest.mark.asyncio
    async def test_stateless_disabled_goes_straight_to_sdk(self, provider):
        from agent_common.core.tool_catalogue import build_server_catalogue, make_catalogue_tool

        cat = build_server_catalogue(
            "gateway",
            [
                make_catalogue_tool(
                    server_name="gateway", name="github_search", description="", input_schema={"type": "object"}
                )
            ],
            source="mcp",
        )
        with (
            patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=AssertionError("must not be called")),
            patch("agent_common.core.catalogue_ingest.fetch_catalogue_mcp", AsyncMock(return_value=cat)),
            patch("agent.mcp_tools.MultiServerMCPClient"),
        ):
            (tool,) = await _resolver(provider, stateless_list=False).resolve(["github_search"])
        assert tool.name == "github_search"


class TestRunnerWiring:
    def test_core_no_longer_lists_the_whole_gateway(self):
        import inspect

        import agent.core as core

        src = inspect.getsource(core)
        assert "MultiServerMCPClient" not in src and "get_tools()" not in src
        assert "McpToolResolver(" in src and "UserTokenProvider(" in src
