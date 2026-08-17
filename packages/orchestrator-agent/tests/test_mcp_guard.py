"""Tests for the inbound MCP SSE event size guard.

The guard exists because one gateway server's 21.9MB ``tools/list`` event,
parsed without any bound, transiently allocated gigabytes and OOMKilled the
prod pod (ringier-data/nannos#152). These tests pin the contract:

* under the cap        -> passes through to the original handler untouched
* over the warn level  -> passes through, but is logged with the server slug
* over the cap         -> rejected BEFORE the original handler (i.e. before the
                          parse) with an error naming the server and sizes
"""

import logging
from types import SimpleNamespace

import pytest

import app.core.mcp_guard as mcp_guard
from app.core.mcp_guard import McpEventTooLargeError, _server_slug, install_mcp_size_guard

CAP = 1000
WARN = 100

GATEWAY_URL = "https://gateway.example/mcp?includeOnlyServerSlugs=fat-server"
CONSOLE_URL = "http://console.example/mcp"


@pytest.fixture
def transport_cls(monkeypatch):
    """Install the guard against a stand-in transport class.

    The guard patches ``StreamableHTTPTransport._handle_sse_event`` at class
    level; using a stand-in keeps the real SDK class pristine for other tests
    and lets each test start from an unguarded state (the module is idempotent
    via a global flag, which we reset).
    """
    calls: list[str] = []

    class FakeTransport:
        def __init__(self, url: str) -> None:
            self.url = url

        async def _handle_sse_event(self, sse, *args, **kwargs):
            calls.append(sse.data)
            return True

    monkeypatch.setattr(mcp_guard, "_installed", False)

    import mcp.client.streamable_http as real_module

    monkeypatch.setattr(real_module, "StreamableHTTPTransport", FakeTransport)
    install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=WARN)
    FakeTransport.handled = calls  # type: ignore[attr-defined]
    return FakeTransport


def sse(data: str):
    return SimpleNamespace(data=data, event="message", id=None)


async def test_small_event_passes_through(transport_cls):
    t = transport_cls(GATEWAY_URL)
    assert await t._handle_sse_event(sse("x" * 10)) is True
    assert transport_cls.handled == ["x" * 10]


async def test_warn_level_logs_slug_but_passes(transport_cls, caplog):
    t = transport_cls(GATEWAY_URL)
    with caplog.at_level(logging.WARNING, logger="app.core.mcp_guard"):
        assert await t._handle_sse_event(sse("x" * (WARN + 1))) is True
    assert transport_cls.handled, "event over the warn level must still be handled"
    assert any("fat-server" in r.getMessage() for r in caplog.records)


async def test_oversized_event_rejected_before_parse(transport_cls, caplog):
    t = transport_cls(GATEWAY_URL)
    with caplog.at_level(logging.ERROR, logger="app.core.mcp_guard"):
        with pytest.raises(McpEventTooLargeError) as exc:
            await t._handle_sse_event(sse("x" * (CAP + 1)))
    # The whole point: the original handler (the parse) must never see it.
    assert transport_cls.handled == []
    assert exc.value.server == "fat-server"
    assert exc.value.size_bytes == CAP + 1
    assert "fat-server" in str(exc.value)


async def test_empty_priming_event_passes(transport_cls):
    """Resumability priming events have no data and must not be broken."""
    t = transport_cls(GATEWAY_URL)
    assert await t._handle_sse_event(SimpleNamespace(data="", event="message", id="7")) is True


async def test_non_gateway_url_falls_back_to_host(transport_cls):
    t = transport_cls(CONSOLE_URL)
    with pytest.raises(McpEventTooLargeError) as exc:
        await t._handle_sse_event(sse("x" * (CAP + 1)))
    assert exc.value.server == "console.example"


def test_server_slug_extraction():
    assert _server_slug(GATEWAY_URL) == "fat-server"
    assert _server_slug(CONSOLE_URL) == "console.example"
    assert _server_slug("not a url") == "not a url"


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(mcp_guard, "_installed", False)

    class FakeTransport:
        def __init__(self, url):
            self.url = url

        async def _handle_sse_event(self, sse, *a, **kw):
            return True

    import mcp.client.streamable_http as real_module

    monkeypatch.setattr(real_module, "StreamableHTTPTransport", FakeTransport)
    install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=WARN)
    first = FakeTransport._handle_sse_event
    install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=WARN)
    assert FakeTransport._handle_sse_event is first, "second install must not re-wrap"
