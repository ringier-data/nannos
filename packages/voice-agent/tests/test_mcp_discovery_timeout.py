"""The discovery backstop on the per-call ``tools/list`` fetch.

``ClientSession.send_request`` awaits with ``timeout=None`` and this session
sets no ``sse_read_timeout``, so a stalled — or size-guard-rejected without a
recoverable request id (``ringier_a2a_sdk.utils.mcp_guard``) — ``tools/list``
would otherwise hang call setup forever, with the caller hearing dead air.

The backstop is deliberately scoped to discovery: a session-wide
``read_timeout_seconds`` would also cap legitimate slow tool calls mid-call.
See ringier-data/nannos#155.
"""

import asyncio
from types import SimpleNamespace

import pytest

from voice_agent.agent import GeminiLiveAgent


class _HangingSession:
    """An MCP session whose catalogue fetch never returns."""

    async def list_tools(self):
        await asyncio.Event().wait()


class _EmptySession:
    async def list_tools(self):
        return SimpleNamespace(tools=[])


def _stub_agent():
    """Minimal stand-in for the bits _init_mcp_tools touches before it returns."""
    return SimpleNamespace(tool_map={}, mcp_tool_filter=None, session_id="test-call")


@pytest.mark.asyncio
async def test_hanging_list_tools_is_reaped(monkeypatch):
    monkeypatch.setenv("MCP_DISCOVERY_TIMEOUT_S", "1")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            GeminiLiveAgent._init_mcp_tools(_stub_agent(), _HangingSession(), asyncio.Queue(), None),
            timeout=10,  # test-level guard: a regression must fail, not hang the suite
        )


@pytest.mark.asyncio
async def test_zero_disables_the_backstop(monkeypatch):
    """0 opts out — the await is then unbounded again, so only the outer wait_for fires."""
    monkeypatch.setenv("MCP_DISCOVERY_TIMEOUT_S", "0")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            GeminiLiveAgent._init_mcp_tools(_stub_agent(), _HangingSession(), asyncio.Queue(), None),
            timeout=1,
        )


@pytest.mark.asyncio
async def test_fast_discovery_is_unaffected(monkeypatch):
    """The common path must not pay for the backstop.

    An empty gateway catalogue still yields the locally-defined declarations
    (the large-result search helpers), so assert the call completes and adds no
    gateway tools rather than pinning an exact count.
    """
    monkeypatch.setenv("MCP_DISCOVERY_TIMEOUT_S", "120")
    agent = _stub_agent()
    declarations = await GeminiLiveAgent._init_mcp_tools(agent, _EmptySession(), asyncio.Queue(), None)
    assert all(d.name in agent.tool_map for d in declarations)
