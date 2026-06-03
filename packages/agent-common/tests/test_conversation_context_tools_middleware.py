"""Tests for ConversationContextToolsMiddleware (additive conversation-context gate).

Covers:
- _derive_context resolution from runtime scope metadata
- Gated tool injected only in its allowed context (channel) and stripped in direct
- Stray bound copies of gated tools are de-duplicated
- Non-gated tools are left untouched
"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.tools import BaseTool

from agent_common.middleware.conversation_context_tools_middleware import (
    ContextGatedTool,
    ConversationContextToolsMiddleware,
    _derive_context,
)


def _fake_tool(name: str) -> BaseTool:
    tool = MagicMock(spec=BaseTool)
    tool.name = name
    return tool


def _make_request(tools):
    req = MagicMock()
    req.tools = tools

    def _override(**kwargs):
        new_req = MagicMock()
        new_req.tools = kwargs.get("tools", tools)
        return new_req

    req.override.side_effect = _override
    return req


_GATE_PATCH = "agent_common.middleware.conversation_context_tools_middleware.get_config"


class TestDeriveContext:
    def test_no_config_is_direct(self):
        with patch(_GATE_PATCH, return_value=None):
            assert _derive_context() == "direct"

    def test_channel_scope(self):
        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            assert _derive_context() == "channel"

    def test_personal_scope_is_direct(self):
        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "personal"}}):
            assert _derive_context() == "direct"


class TestGateBehavior:
    def setup_method(self):
        self.gated = _fake_tool("read_personal_file")
        self.middleware = ConversationContextToolsMiddleware(
            [ContextGatedTool(tool=self.gated, allowed_contexts=frozenset({"channel"}))]
        )

    def test_injects_gated_tool_in_channel(self):
        base = _fake_tool("docstore_search")
        request = _make_request([base])
        received = []

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            self.middleware.wrap_model_call(request, lambda req: received.append(req) or MagicMock())

        names = [t.name for t in received[0].tools]
        assert "read_personal_file" in names
        assert "docstore_search" in names

    def test_strips_gated_tool_in_direct(self):
        base = _fake_tool("docstore_search")
        # A stray bound copy of the gated tool is present and must be removed.
        request = _make_request([base, self.gated])
        received = []

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "personal"}}):
            self.middleware.wrap_model_call(request, lambda req: received.append(req) or MagicMock())

        names = [t.name for t in received[0].tools]
        assert "read_personal_file" not in names
        assert "docstore_search" in names

    def test_dedup_in_channel(self):
        base = _fake_tool("docstore_search")
        # Stray bound copy present; gate should leave exactly one instance.
        request = _make_request([base, self.gated])
        received = []

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            self.middleware.wrap_model_call(request, lambda req: received.append(req) or MagicMock())

        names = [t.name for t in received[0].tools]
        assert names.count("read_personal_file") == 1

    def test_no_change_in_direct_without_stray(self):
        base = _fake_tool("docstore_search")
        request = _make_request([base])

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "personal"}}):
            self.middleware.wrap_model_call(request, lambda req: MagicMock())

        # Nothing to inject, nothing to strip → no override.
        request.override.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_injects_in_channel(self):
        base = _fake_tool("docstore_search")
        request = _make_request([base])
        received = []

        async def handler(req):
            received.append(req)
            return MagicMock()

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            await self.middleware.awrap_model_call(request, handler)

        names = [t.name for t in received[0].tools]
        assert "read_personal_file" in names


def _make_request_with_registry(tools, tool_registry):
    """Build a request whose runtime context carries a ``tool_registry`` dict."""
    req = MagicMock()
    req.tools = tools
    req.runtime.context.tool_registry = tool_registry

    def _override(**kwargs):
        new_req = MagicMock()
        new_req.tools = kwargs.get("tools", tools)
        return new_req

    req.override.side_effect = _override
    return req


class TestRuntimeGatedTools:
    """Gate configured with tool *names* resolved from the runtime tool_registry.

    Mirrors the orchestrator's single-graph-per-model case where the per-user
    tool instance is only available at invocation time.
    """

    def setup_method(self):
        self.middleware = ConversationContextToolsMiddleware(
            runtime_gated_tools={"read_personal_file": frozenset({"channel"})},
        )

    def test_resolves_and_injects_in_channel(self):
        gated = _fake_tool("read_personal_file")
        base = _fake_tool("docstore_search")
        request = _make_request_with_registry([base], {"read_personal_file": gated})
        received = []

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            self.middleware.wrap_model_call(request, lambda req: received.append(req) or MagicMock())

        names = [t.name for t in received[0].tools]
        assert "read_personal_file" in names
        assert "docstore_search" in names

    def test_not_injected_in_direct(self):
        gated = _fake_tool("read_personal_file")
        base = _fake_tool("docstore_search")
        request = _make_request_with_registry([base], {"read_personal_file": gated})

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "personal"}}):
            self.middleware.wrap_model_call(request, lambda req: MagicMock())

        # Nothing to inject (direct) and no stray bound copy → no override.
        request.override.assert_not_called()

    def test_missing_from_registry_is_skipped(self):
        base = _fake_tool("docstore_search")
        request = _make_request_with_registry([base], {})

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            self.middleware.wrap_model_call(request, lambda req: MagicMock())

        # Tool not resolvable → nothing injected, no override.
        request.override.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_resolves_and_injects_in_channel(self):
        gated = _fake_tool("read_personal_file")
        base = _fake_tool("docstore_search")
        request = _make_request_with_registry([base], {"read_personal_file": gated})
        received = []

        async def handler(req):
            received.append(req)
            return MagicMock()

        with patch(_GATE_PATCH, return_value={"metadata": {"scope": "channel"}}):
            await self.middleware.awrap_model_call(request, handler)

        names = [t.name for t in received[0].tools]
        assert "read_personal_file" in names
