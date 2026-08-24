"""Tests for POST /api/v1/mcp/tools/invoke.

The endpoint runs a real MCP tool with the user's credentials so the scheduler UI can
show a genuine payload before a watch condition is written against it. The gateway call
itself is tested in test_mcp_gateway_client.py; what matters here is the risk gate (an
unscored tool must not run unattended) and trimming a response for the browser.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from console_backend.routers import mcp_router
from console_backend.routers.mcp_router import MCPToolInvokeRequest, invoke_mcp_tool
from console_backend.services.mcp_tool_client import GatewayError, ToolCallResult

@pytest.fixture
def user():
    return SimpleNamespace(sub="user-sub-1", id="user-1", email="a@b.ch")


def _request(score: float | None, *extra_scores: float) -> MagicMock:
    """A request whose risk service reports the given score(s) for any tool name."""
    rows = [
        {"base_score": s, "risk_factors": {"writes": False}}
        for s in ([score, *extra_scores] if score is not None else list(extra_scores))
    ]
    risk_service = MagicMock()
    risk_service.get_scores_by_tool = AsyncMock(return_value=rows)
    request = MagicMock()
    request.app.state.tool_risk_service = risk_service
    return request


def _describes_a_read(monkeypatch, value: bool) -> None:
    """Stub the "does the gateway call this a GET?" fallback for unscored tools."""
    monkeypatch.setattr(mcp_router, "_describes_a_read", AsyncMock(return_value=value))


def _gateway(monkeypatch, result: dict, *, is_error: bool = False) -> AsyncMock:
    """Stub token resolution and the tool call at the client boundary."""
    monkeypatch.setattr(mcp_router, "get_user_subject_token", AsyncMock(return_value="subject"))
    monkeypatch.setattr(mcp_router, "token_for", AsyncMock(return_value="tok"))
    stub = AsyncMock(return_value=ToolCallResult(result=result, elapsed_ms=7, is_error=is_error))
    monkeypatch.setattr(mcp_router, "call_tool", stub)
    return stub


class TestRiskGate:
    @pytest.mark.asyncio
    async def test_console_tools_can_be_previewed(self, user, monkeypatch):
        # They were refused while only the gateway could be reached from here. The tool
        # client calls this backend's own /mcp mount now, so they preview like any other.
        stub = _gateway(monkeypatch, {"tools": []})
        body = MCPToolInvokeRequest(tool_name="console_list_mcp_tools")
        result = await invoke_mcp_tool(body, _request(0.0), AsyncMock(), user)
        assert result.result == {"tools": []}
        stub.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unscored_tool_requires_acknowledgement(self, user, monkeypatch):
        _describes_a_read(monkeypatch, False)
        body = MCPToolInvokeRequest(tool_name="gdrive_delete_file")
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(body, _request(None), AsyncMock(), user)
        assert exc.value.status_code == 428
        # The caller needs the risk back to explain the confirmation it is asking for.
        assert exc.value.detail["risk"]["known"] is False
        assert "not been assessed" in exc.value.detail["message"]

    @pytest.mark.asyncio
    async def test_unscored_read_endpoint_runs_without_acknowledgement(self, user, monkeypatch):
        # The table is filled lazily by agent runs, so most tools have no score. A
        # GET endpoint cannot change state, which is enough to skip the prompt.
        _describes_a_read(monkeypatch, True)
        _gateway(monkeypatch, {})
        body = MCPToolInvokeRequest(tool_name="alloy-riad-dev_get__campaign__id__sync-status")
        result = await invoke_mcp_tool(body, _request(None), AsyncMock(), user)
        assert result.risk.known is False

    @pytest.mark.asyncio
    async def test_a_recorded_score_wins_over_the_description(self, user, monkeypatch):
        # A heuristic must never downgrade a risk somebody recorded.
        _describes_a_read(monkeypatch, True)
        body = MCPToolInvokeRequest(tool_name="looks_like_a_get_but_writes")
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(body, _request(0.9), AsyncMock(), user)
        assert exc.value.status_code == 428

    @pytest.mark.asyncio
    async def test_the_worst_of_several_scores_decides(self, user):
        # The same tool reached through two servers gets scored twice and the rows
        # disagree (github_get_me is 1.0 under "github" and 0.1 under "_self").
        body = MCPToolInvokeRequest(tool_name="github_get_me")
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(body, _request(0.1, 1.0), AsyncMock(), user)
        assert exc.value.status_code == 428
        assert exc.value.detail["risk"]["base_score"] == 1.0

    @pytest.mark.asyncio
    async def test_high_risk_tool_requires_acknowledgement(self, user):
        body = MCPToolInvokeRequest(tool_name="gmail_trash_message")
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(body, _request(0.8), AsyncMock(), user)
        assert exc.value.status_code == 428

    @pytest.mark.asyncio
    async def test_read_only_tool_runs_without_acknowledgement(self, user, monkeypatch):
        _gateway(monkeypatch, {"status": "ACTIVE"})
        body = MCPToolInvokeRequest(tool_name="naonous_get_campaign")
        result = await invoke_mcp_tool(body, _request(0.1), AsyncMock(), user)
        assert result.result == {"status": "ACTIVE"}
        assert result.risk.base_score == 0.1

    @pytest.mark.asyncio
    async def test_acknowledgement_lets_an_unscored_tool_run(self, user, monkeypatch):
        _describes_a_read(monkeypatch, False)
        _gateway(monkeypatch, {})
        body = MCPToolInvokeRequest(tool_name="unknown_tool", acknowledge_risk=True)
        result = await invoke_mcp_tool(body, _request(None), AsyncMock(), user)
        assert result.risk.known is False

    @pytest.mark.asyncio
    async def test_a_failing_risk_lookup_does_not_block_the_call(self, user, monkeypatch):
        _describes_a_read(monkeypatch, False)
        _gateway(monkeypatch, {})
        request = _request(None)
        request.app.state.tool_risk_service.get_scores_by_tool = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        body = MCPToolInvokeRequest(tool_name="some_tool", acknowledge_risk=True)
        result = await invoke_mcp_tool(body, request, AsyncMock(), user)
        assert result.result == {}


class TestGatewayErrors:
    @pytest.mark.asyncio
    async def test_a_gateway_error_becomes_502(self, user, monkeypatch):
        _gateway(monkeypatch, {})
        monkeypatch.setattr(
            mcp_router, "call_tool", AsyncMock(side_effect=GatewayError("upstream exploded"))
        )
        body = MCPToolInvokeRequest(tool_name="nope", acknowledge_risk=True)
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(body, _request(0.1), AsyncMock(), user)
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_a_timeout_becomes_504(self, user, monkeypatch):
        _gateway(monkeypatch, {})
        monkeypatch.setattr(
            mcp_router,
            "call_tool",
            AsyncMock(side_effect=GatewayError("'slow' did not respond within 30s")),
        )
        body = MCPToolInvokeRequest(tool_name="slow", acknowledge_risk=True)
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(body, _request(0.1), AsyncMock(), user)
        assert exc.value.status_code == 504

    @pytest.mark.asyncio
    async def test_tool_level_failure_is_reported_not_raised(self, user, monkeypatch):
        # isError lives inside a successful JSON-RPC reply; the payload is still useful.
        _gateway(monkeypatch, {"output": "campaign 999 not found"}, is_error=True)
        body = MCPToolInvokeRequest(tool_name="naonous_get_campaign", acknowledge_risk=True)
        result = await invoke_mcp_tool(body, _request(0.1), AsyncMock(), user)
        assert result.is_error is True
        assert result.result == {"output": "campaign 999 not found"}


class TestTruncation:
    @pytest.mark.asyncio
    async def test_oversized_results_are_replaced_with_a_summary(self, user, monkeypatch):
        big = {"rows": ["x" * 100 for _ in range(2000)], "count": 2000}
        _gateway(monkeypatch, big)
        body = MCPToolInvokeRequest(tool_name="gworkspace_read_sheet_values", acknowledge_risk=True)
        result = await invoke_mcp_tool(body, _request(0.1), AsyncMock(), user)
        assert result.truncated is True
        assert result.result["_truncated"] is True
        assert "count" in result.result["keys"]


class TestRiskLookupKeying:
    """Scores are keyed per server, but the caller may not know which slug was used."""

    @pytest.mark.asyncio
    async def test_a_score_is_found_whatever_slug_the_caller_passes(self, user, monkeypatch):
        # Stored slugs come from the agent runtime ("github", "_self"), not from what
        # the gateway reports to this endpoint, so the name alone has to be enough.
        _gateway(monkeypatch, {})
        request = _request(0.05)
        body = MCPToolInvokeRequest(
            tool_name="gdrive_get_file_metadata", server_slug="Google Drive"
        )
        result = await invoke_mcp_tool(body, request, AsyncMock(), user)
        assert result.risk.base_score == 0.05
        # Looked up by name only — the slug is not part of the query.
        request.app.state.tool_risk_service.get_scores_by_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_slug_never_reaches_the_gateway_call(self, user, monkeypatch):
        # It is a lookup key only: a wrong slug must not be able to narrow the call and
        # hide the tool. (That the call itself is unscoped is the client's own test.)
        stub = _gateway(monkeypatch, {})
        body = MCPToolInvokeRequest(tool_name="some_tool", server_slug="wrong", acknowledge_risk=True)
        await invoke_mcp_tool(body, _request(None), AsyncMock(), user)
        _, kwargs = stub.await_args
        assert "wrong" not in str(kwargs)
