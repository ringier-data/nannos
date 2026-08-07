"""Tests for MCP error classification, formatting, and the streamable-HTTP guard."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ringier_a2a_sdk.utils.mcp_errors import (
    MCPUnexpectedContentTypeError,
    _UpstreamDedupFilter,
    format_mcp_error,
    guarded_streamable_http,
    install_streamable_http_guard,
)


# ---------------------------------------------------------------------------
# Helpers: tiny HTTP server that always responds with text/html.
# ---------------------------------------------------------------------------


class _HtmlOnlyHandler(BaseHTTPRequestHandler):
    def _serve_html(self) -> None:
        body = b"<!DOCTYPE html><html><body>Login redirect</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            try:
                self.rfile.read(length)
            except Exception:
                pass
        self._serve_html()

    def do_GET(self) -> None:  # noqa: N802
        self._serve_html()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Silence the default stderr access log during tests.
        return


@asynccontextmanager
async def _html_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = HTTPServer(("127.0.0.1", port), _HtmlOnlyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Classification & formatting
# ---------------------------------------------------------------------------


def test_unexpected_content_type_message_includes_url_and_hint():
    err = MCPUnexpectedContentTypeError(
        url="https://gw.example/mcp",
        server_slug="console",
        content_type="text/html; charset=utf-8",
        body_snippet="<!DOCTYPE html>\n<html>",
    )
    msg = str(err)
    assert "text/html" in msg
    assert "https://gw.example/mcp" in msg
    assert "console" in msg
    assert "auth redirect" in msg
    assert "<!DOCTYPE html>" in msg


def test_format_mcp_error_handles_unexpected_content_type():
    err = MCPUnexpectedContentTypeError(
        url="https://gw.example/mcp",
        server_slug=None,
        content_type="text/html",
    )
    out = format_mcp_error(err)
    assert "text/html" in out
    assert "https://gw.example/mcp" in out


def test_format_mcp_error_handles_exception_group_wrapper():
    inner = MCPUnexpectedContentTypeError(
        url="https://gw.example/mcp",
        server_slug="gateway",
        content_type="text/html",
    )
    # Python 3.11+ ExceptionGroup
    group = ExceptionGroup("mcp failed", [inner])
    out = format_mcp_error(group)
    assert "text/html" in out
    assert "https://gw.example/mcp" in out


def test_is_retryable_returns_false_for_unexpected_content_type():
    from ringier_a2a_sdk.utils.mcp_errors import is_retryable_mcp_error

    err = MCPUnexpectedContentTypeError(
        url="https://gw.example/mcp",
        server_slug=None,
        content_type="text/html",
    )
    assert is_retryable_mcp_error(err) is False
    # And wrapped in an ExceptionGroup (the shape anyio/MCP produces).
    group = ExceptionGroup("mcp failed", [err])
    assert is_retryable_mcp_error(group) is False


# ---------------------------------------------------------------------------
# Dedup filter
# ---------------------------------------------------------------------------


def _make_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="mcp.client.streamable_http",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_dedup_filter_suppresses_repeats_within_window_and_passes_new_endpoints():
    f = _UpstreamDedupFilter(window_seconds=60.0)
    msg_a = "Unexpected content type: text/html; charset=utf-8"
    msg_b = "Unexpected content type: text/plain"

    # First occurrence for endpoint A passes.
    assert f.filter(_make_record(msg_a)) is True
    # Repeats for A within window are suppressed.
    assert f.filter(_make_record(msg_a)) is False
    assert f.filter(_make_record(msg_a)) is False
    # A different endpoint (different log line) passes immediately.
    assert f.filter(_make_record(msg_b)) is True
    assert f.filter(_make_record(msg_b)) is False

    # Unrelated log lines are always allowed through.
    assert f.filter(_make_record("Something else entirely")) is True
    assert f.filter(_make_record("Something else entirely")) is True


def test_dedup_filter_allows_new_occurrence_after_window():
    f = _UpstreamDedupFilter(window_seconds=0.05)
    msg = "Unexpected content type: text/html"
    assert f.filter(_make_record(msg)) is True
    assert f.filter(_make_record(msg)) is False
    # Wait past the window.
    import time

    time.sleep(0.1)
    assert f.filter(_make_record(msg)) is True


# ---------------------------------------------------------------------------
# Streamable-HTTP guard: end-to-end with a real local HTML server.
# ---------------------------------------------------------------------------


def _find_unexpected_ct(err: BaseException) -> MCPUnexpectedContentTypeError | None:
    """Walk an exception chain / ExceptionGroup looking for our typed exception."""
    seen: set[int] = set()
    stack: list[BaseException] = [err]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, MCPUnexpectedContentTypeError):
            return cur
        if cur.__class__.__name__ == "ExceptionGroup":
            stack.extend(getattr(cur, "exceptions", []) or [])
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        if cur.__context__ is not None:
            stack.append(cur.__context__)
    return None


def test_streamable_http_guard_fails_fast_on_html_response():
    """An MCP endpoint returning text/html must raise within seconds, not hang."""
    install_streamable_http_guard()

    # Importing here to keep test-time dependency on the upstream mcp client local.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _run() -> BaseException:
        async with _html_server() as url:

            async def _attempt() -> None:
                # Real-world shape: the guard wraps the whole MCP call so that
                # the patched transport (running in subtasks of streamablehttp_client)
                # inherits the contextvar set here.
                async with guarded_streamable_http(url=url, server_slug="test"):
                    async with streamablehttp_client(url) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()

            try:
                await asyncio.wait_for(_attempt(), timeout=10.0)
            except BaseException as exc:  # noqa: BLE001 - test wants the actual exception
                return exc
            raise AssertionError("expected the call to raise but it returned cleanly")

    err = asyncio.run(_run())
    found = _find_unexpected_ct(err)
    assert found is not None, f"Expected MCPUnexpectedContentTypeError, got {err!r}"
    assert "text/html" in found.content_type
    assert found.server_slug == "test"
    assert found.url and found.url.startswith("http://127.0.0.1:")


def test_streamable_http_guard_install_is_idempotent():
    install_streamable_http_guard()
    install_streamable_http_guard()
    install_streamable_http_guard()

    from mcp.client.streamable_http import StreamableHTTPTransport

    assert getattr(StreamableHTTPTransport, "_ringier_streamable_guard_installed", False) is True
