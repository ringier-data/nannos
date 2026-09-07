"""Tests for the shared MCP gateway client.

Both the scheduler (evaluating a watch) and the console (previewing a check) call tools
through here, so how a response is unwrapped is a contract between them: if they folded
a tool's output differently, the preview would show a payload the job never sees.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from console_backend.services import mcp_tool_client as tc
from console_backend.services.mcp_tool_client import (
    GatewayError,
    GatewayTimeout,
    ToolNotFound,
    call_tool,
    content_to_result,
    extract_result,
    parse_envelope,
)


def _reply(monkeypatch, payload: dict) -> MagicMock:
    response = MagicMock()
    response.headers = {"content-type": "application/json"}
    response.json.return_value = payload
    response.raise_for_status = MagicMock()

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    # The module holds one pooled client for the process (llm_gateway's pattern), so the
    # stub replaces what the pool hands out rather than the AsyncClient constructor.
    monkeypatch.setattr(tc._client, "get", lambda: client)
    return client


class TestContentBlocks:
    def test_json_text_block_is_parsed(self):
        assert content_to_result([{"type": "text", "text": '{"a": 1}'}]) == {"a": 1}

    def test_plain_text_block_is_wrapped(self):
        assert content_to_result([{"type": "text", "text": "not json"}]) == {"output": "not json"}

    def test_multiple_text_blocks_are_joined(self):
        blocks = [{"type": "text", "text": '{"a":'}, {"type": "text", "text": " 1}"}]
        assert content_to_result(blocks) == {"a": 1}

    def test_non_text_blocks_are_ignored(self):
        blocks = [{"type": "image", "data": "…"}, {"type": "text", "text": '{"a": 1}'}]
        assert content_to_result(blocks) == {"a": 1}

    def test_top_level_json_array_is_wrapped(self):
        # A JSONPath needs a mapping at the root.
        assert content_to_result([{"type": "text", "text": "[1, 2]"}]) == {"output": [1, 2]}

    def test_empty_block_list_is_an_empty_dict(self):
        assert content_to_result([]) == {}

    def test_dict_passes_through(self):
        assert content_to_result({"a": 1}) == {"a": 1}


class TestExtractResult:
    def test_no_content_blocks_folds_to_empty(self):
        # Key presence, not truthiness: this fell through to the raw JSON-RPC result once.
        assert extract_result({"content": []}) == {}

    def test_structured_content_wins(self):
        assert extract_result({"structuredContent": {"a": 1}, "content": []}) == {"a": 1}


class TestEnvelopeParsing:
    def test_sse_reply_is_decoded(self):
        response = MagicMock()
        response.headers = {"content-type": "text/event-stream"}
        response.text = 'event: message\ndata: {"result": {"content": []}}\n\n'
        assert parse_envelope(response) == {"result": {"content": []}}

    def test_undecodable_sse_reply_is_a_gateway_error(self):
        response = MagicMock()
        response.headers = {"content-type": "text/event-stream"}
        response.text = "data: not json\n\n"
        with pytest.raises(GatewayError):
            parse_envelope(response)

    def test_a_notification_streamed_first_is_skipped(self):
        # A server may stream progress ahead of the reply. Taking the first `data:` line
        # would hand that notification back as the tool's answer, and the condition would
        # then be evaluated against {}.
        response = MagicMock()
        response.headers = {"content-type": "text/event-stream"}
        response.text = (
            'data: {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}\n\n'
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}\n\n'
        )
        assert parse_envelope(response, expect_id=1) == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": []},
        }

    def test_a_reply_for_another_id_is_skipped(self):
        response = MagicMock()
        response.headers = {"content-type": "text/event-stream"}
        response.text = (
            'data: {"id": 7, "result": {"content": [{"type": "text", "text": "other"}]}}\n\n'
            'data: {"id": 1, "result": {"content": []}}\n\n'
        )
        assert parse_envelope(response, expect_id=1)["id"] == 1

    def test_data_without_a_space_is_sse_legal(self):
        response = MagicMock()
        response.headers = {"content-type": "text/event-stream"}
        response.text = 'data:{"result": {"content": []}}\n\n'
        assert parse_envelope(response) == {"result": {"content": []}}


class TestCallTool:
    @pytest.mark.asyncio
    async def test_an_explicit_null_error_is_not_a_failure(self, monkeypatch):
        # Some frameworks always emit both members. Keying on presence made every such
        # reply a GatewayError("unknown gateway error"), failing the run and eventually
        # auto-pausing the job, though the tool answered correctly.
        _reply(
            monkeypatch,
            {"error": None, "result": {"content": [{"type": "text", "text": '{"status": "OK"}'}]}},
        )
        call = await call_tool("tok", "some_tool", {})
        assert call.result == {"status": "OK"}

    @pytest.mark.asyncio
    async def test_a_successful_call(self, monkeypatch):
        _reply(monkeypatch, {"result": {"content": [{"type": "text", "text": '{"status": "OK"}'}]}})
        call = await call_tool("tok", "some_tool", {"a": 1})
        assert call.result == {"status": "OK"}
        assert call.is_error is False

    @pytest.mark.asyncio
    async def test_the_call_is_not_scoped_to_a_server(self, monkeypatch):
        # tools/call resolves by name; narrowing to a slug the caller guessed wrong would
        # hide the tool and fail the call outright.
        client = _reply(monkeypatch, {"result": {"content": []}})
        await call_tool("tok", "some_tool")
        assert "includeOnlyServerSlugs" not in client.post.await_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unknown_tool_is_distinguished(self, monkeypatch):
        # A job referencing a tool the gateway dropped is a configuration problem, not an
        # outage, and should not be retried as one.
        _reply(monkeypatch, {"error": {"code": -32602, "message": "unknown tool: nope"}})
        with pytest.raises(ToolNotFound):
            await call_tool("tok", "nope")

    @pytest.mark.asyncio
    async def test_a_tool_reporting_not_found_is_not_a_missing_tool(self, monkeypatch):
        # The tool exists and answered; the thing it was asked about does not. Classifying
        # on the message put this in front of the user as a 404 "tool is not available".
        _reply(monkeypatch, {"error": {"code": -32000, "message": "campaign 999 not found"}})
        with pytest.raises(GatewayError) as exc:
            await call_tool("tok", "naonous_get_campaign")
        assert not isinstance(exc.value, ToolNotFound)

    @pytest.mark.asyncio
    async def test_a_timeout_is_its_own_type(self, monkeypatch):
        import httpx

        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ReadTimeout("too slow"))
        monkeypatch.setattr(tc._client, "get", lambda: client)
        with pytest.raises(GatewayTimeout):
            await call_tool("tok", "slow_tool", timeout=1.0)

    @pytest.mark.asyncio
    async def test_other_gateway_errors_are_gateway_errors(self, monkeypatch):
        _reply(monkeypatch, {"error": {"code": -32000, "message": "upstream exploded"}})
        with pytest.raises(GatewayError):
            await call_tool("tok", "some_tool")

    @pytest.mark.asyncio
    async def test_a_tool_level_failure_is_reported_not_raised(self, monkeypatch):
        # isError lives inside a successful reply; the payload is still useful.
        _reply(
            monkeypatch,
            {"result": {"isError": True, "content": [{"type": "text", "text": "not found"}]}},
        )
        call = await call_tool("tok", "some_tool")
        assert call.is_error is True
        assert call.result == {"output": "not found"}

    @pytest.mark.asyncio
    async def test_a_timeout_is_a_gateway_error(self, monkeypatch):
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.TimeoutException("too slow"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(tc.httpx, "AsyncClient", MagicMock(return_value=client))
        with pytest.raises(GatewayError, match="did not respond"):
            await call_tool("tok", "slow_tool")

    @pytest.mark.asyncio
    async def test_json_is_still_importable(self):
        # Guard against the module losing its json import in a refactor.
        assert json.dumps(content_to_result({"a": 1})) == '{"a": 1}'
