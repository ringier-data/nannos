"""HTTP-level regression test for the streaming MCP keepalive.

Guards the two-part fix together:
  1. utils.mcp_keepalive emits progress/log notifications during a slow tool call;
  2. the /mcp transport is mounted as a *streaming* StreamableHTTPSessionManager
     (json_response=False, native ASGI) so those notifications flush as SSE events
     instead of being buffered until the tool returns.

If someone reverts to fastapi_mcp's buffering mount_http(), this test fails because
no notification arrives before the final result.
"""

import asyncio
import os
import socket
import threading
import time

import pytest
import uvicorn

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from console_backend.utils.mcp_keepalive import with_progress_keepalive


def _build_app(interval: float = 0.4) -> FastAPI:
    """Build an app wired exactly like app.py: keepalive + streaming SSE mount."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.stack = AsyncExitStack()
        await app.state.stack.enter_async_context(session_manager.run())
        yield
        await app.state.stack.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.post("/api/v1/slow", tags=["MCP"], operation_id="slow_tool", response_model=str)
    async def slow_tool(seconds: float = 2.0) -> str:
        await asyncio.sleep(seconds)
        return f"done after {seconds}s"

    mcp = FastApiMCP(app, include_tags=["MCP"])

    _orig = mcp._execute_api_tool

    async def _with_keepalive(*args, **kwargs):
        return await with_progress_keepalive(_orig(*args, **kwargs), mcp.server, interval=interval)

    mcp._execute_api_tool = _with_keepalive

    session_manager = StreamableHTTPSessionManager(app=mcp.server, json_response=False, stateless=False)

    async def _mcp_asgi(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    app.mount("/mcp", _mcp_asgi)
    return app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_keepalive_notifications_stream_before_result():
    port = _free_port()
    config = uvicorn.Config(_build_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # Wait for the server to accept connections.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not server.started:
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"

        notifications: list[tuple[float, str]] = []
        t0 = time.monotonic()

        async def on_progress(progress, total, message):
            notifications.append((time.monotonic() - t0, "progress"))

        async def on_logging(params):
            notifications.append((time.monotonic() - t0, "log"))

        url = f"http://127.0.0.1:{port}/mcp"
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write, logging_callback=on_logging) as session:
                await session.initialize()
                result = await session.call_tool("slow_tool", {"seconds": 2.0}, progress_callback=on_progress)
                result_at = time.monotonic() - t0

        text = result.content[0].text
        assert "done after 2.0s" in text
        assert not result.isError
        # Keepalives must have arrived, and BEFORE the result (i.e. they streamed).
        assert notifications, "no keepalive notifications received — transport is buffering"
        assert any(t < result_at - 0.2 for t, _ in notifications), (
            f"notifications did not stream before result: {notifications} vs result_at={result_at:.2f}"
        )
        assert any(kind == "progress" for _, kind in notifications)
        assert any(kind == "log" for _, kind in notifications)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
