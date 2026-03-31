"""Tests for MCP progress callback."""

from unittest.mock import MagicMock

import pytest

from ringier_a2a_sdk.utils.mcp_progress import on_mcp_progress


def _make_context(server: str = "test-server", tool: str = "test-tool"):
    ctx = MagicMock()
    ctx.server_name = server
    ctx.tool_name = tool
    return ctx


@pytest.mark.asyncio
async def test_on_mcp_progress_logs_without_error():
    """The lightweight callback should execute without raising."""
    ctx = _make_context()
    await on_mcp_progress(50, 100, "halfway", ctx)
    await on_mcp_progress(3, None, None, ctx)
