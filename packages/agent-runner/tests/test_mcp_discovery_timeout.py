"""The discovery backstop on `get_tools()`.

Nothing else bounds this await. `StreamableHttpConnection`'s `sse_read_timeout`
is DEPRECATED and no longer used by the MCP SDK (>=1.25), and
`ClientSession.send_request` waits with `timeout=None` — so a stalled server, or
the size guard's id-less rejection path (which closes the stream as *complete*
rather than stalled, meaning no read timeout could fire anyway), would hang a
scheduled job forever. See ringier-data/nannos#155.
"""

import asyncio

import pytest

from agent import core


class _HangingClient:
    async def get_tools(self):
        await asyncio.Event().wait()


class _FastClient:
    async def get_tools(self):
        return ["tool-a", "tool-b"]


@pytest.mark.asyncio
async def test_hanging_get_tools_is_reaped(monkeypatch):
    monkeypatch.setattr(core, "_MCP_DISCOVERY_TIMEOUT_S", 1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            core._get_tools_bounded(_HangingClient()),
            timeout=10,  # test-level guard: a regression must fail, not hang the suite
        )


@pytest.mark.asyncio
async def test_non_positive_disables_the_backstop(monkeypatch):
    monkeypatch.setattr(core, "_MCP_DISCOVERY_TIMEOUT_S", 0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(core._get_tools_bounded(_HangingClient()), timeout=1)


@pytest.mark.asyncio
async def test_fast_discovery_is_unaffected(monkeypatch):
    monkeypatch.setattr(core, "_MCP_DISCOVERY_TIMEOUT_S", 120)
    assert await core._get_tools_bounded(_FastClient()) == ["tool-a", "tool-b"]
