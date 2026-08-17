"""Tests for the inbound MCP message size guard.

The guard exists because one gateway server's 21.9MB ``tools/list`` event,
parsed without any bound, transiently allocated gigabytes and OOMKilled the
prod pod (ringier-data/nannos#152).

These tests run against the REAL ``StreamableHTTPTransport`` (not a stub):
the rejection contract only works if it matches the SDK's call sites, which
swallow raised exceptions and reconnect-with-replay. The contract under test:

* under every threshold -> parsed and delivered to the session, untouched
* over the warn level   -> delivered, but logged with the server slug
* over the cap          -> NOT parsed; McpEventTooLargeError is delivered via
                           read_stream_writer (failing the pending request)
                           and the handler reports the stream complete (True),
                           so the SDK closes the response instead of
                           reconnecting with Last-Event-ID and replaying.
"""

import json
import logging
from types import SimpleNamespace

import anyio
import pytest

from app.core.mcp_guard import (
    McpEventTooLargeError,
    _server_slug,
    _utf8_size,
    install_mcp_size_guard,
)

CAP = 1000
WARN = 100

GATEWAY_URL = "https://gateway.example/mcp?includeOnlyServerSlugs=fat-server"
CONSOLE_URL = "http://console.example/mcp"

# A minimal, valid JSONRPC response the SDK can parse end-to-end.
SMALL_RESULT = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})


@pytest.fixture
def real_transport(monkeypatch):
    """A real StreamableHTTPTransport with the guard installed.

    The guard patches the SDK class in-process; restore the originals after
    each test so the patch (and its captured thresholds) cannot leak into
    other test modules.
    """
    import mcp.client.streamable_http as sdk

    monkeypatch.setattr(sdk.StreamableHTTPTransport, "_handle_sse_event", sdk.StreamableHTTPTransport._handle_sse_event)
    monkeypatch.setattr(
        sdk.StreamableHTTPTransport, "_handle_json_response", sdk.StreamableHTTPTransport._handle_json_response
    )
    install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=WARN)

    def make(url: str = GATEWAY_URL):
        return sdk.StreamableHTTPTransport(url)

    return make


def sse(data: str, event: str = "message", id: str | None = "evt-1"):
    return SimpleNamespace(data=data, event=event, id=id, retry=None)


async def drain_one(receiver):
    with anyio.fail_after(2):
        return await receiver.receive()


async def test_small_event_parses_and_delivers(real_transport):
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    complete = await t._handle_sse_event(sse(SMALL_RESULT), send)
    delivered = await drain_one(recv)
    # A JSONRPC response both completes the stream and reaches the session.
    assert complete is True
    assert not isinstance(delivered, Exception)


async def test_warn_level_logs_slug_but_still_parses(real_transport, caplog):
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    # Valid JSONRPC padded past the warn level but under the cap.
    padded = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"pad": "x" * (WARN + 1)}})
    assert WARN < len(padded) <= CAP
    with caplog.at_level(logging.WARNING, logger="app.core.mcp_guard"):
        complete = await t._handle_sse_event(sse(padded), send)
    delivered = await drain_one(recv)
    assert complete is True
    assert not isinstance(delivered, Exception)
    assert any("fat-server" in r.getMessage() for r in caplog.records)


async def test_oversized_event_fails_request_without_reconnect_replay(real_transport, caplog):
    """The critical contract, against the real SDK handler.

    Raising out of the handler would be swallowed by every SDK call site
    (except Exception -> logger.debug) followed by a Last-Event-ID reconnect
    replaying the same payload — a hang plus replay loop, worse than the OOM.
    The guard must instead deliver the error to the session AND report the
    stream complete.
    """
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    with caplog.at_level(logging.ERROR, logger="app.core.mcp_guard"):
        complete = await t._handle_sse_event(sse("x" * (CAP + 1)), send)

    assert complete is True, "must report completion so the SDK does not reconnect and replay"
    delivered = await drain_one(recv)
    assert isinstance(delivered, McpEventTooLargeError)
    assert delivered.server == "fat-server"
    assert delivered.size_bytes == CAP + 1
    assert any("fat-server" in r.getMessage() for r in caplog.records)


async def test_oversized_json_response_fails_request(real_transport):
    """The application/json body path runs the same parse and is guarded too."""

    class FakeResponse:
        async def aread(self):
            return b"x" * (CAP + 1)

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    await t._handle_json_response(FakeResponse(), send)
    delivered = await drain_one(recv)
    assert isinstance(delivered, McpEventTooLargeError)


async def test_small_json_response_delegates_to_sdk(real_transport):
    class FakeResponse:
        async def aread(self):
            return SMALL_RESULT.encode()

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    await t._handle_json_response(FakeResponse(), send)
    delivered = await drain_one(recv)
    assert not isinstance(delivered, Exception)


async def test_multibyte_payload_measured_in_bytes(real_transport):
    """4-byte emoji: character count sits under the cap, byte size over it."""
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    payload = "\U0001f600" * ((CAP // 4) + 1)  # chars ~ CAP/4, bytes > CAP
    assert len(payload) <= CAP < len(payload.encode())
    complete = await t._handle_sse_event(sse(payload), send)
    assert complete is True
    assert isinstance(await drain_one(recv), McpEventTooLargeError)


async def test_priming_and_non_message_events_untouched(real_transport):
    t = real_transport()
    send, _recv = anyio.create_memory_object_stream(10)
    # Priming event: no data, only an ID (resumability) — SDK returns False.
    assert await t._handle_sse_event(SimpleNamespace(data="", event="message", id="7", retry=None), send) is False
    # Non-message events are never parsed and must not be size-checked.
    assert await t._handle_sse_event(sse("x" * (CAP + 1), event="ping"), send) is False


async def test_cap_zero_disables_rejection(monkeypatch, caplog):
    import mcp.client.streamable_http as sdk

    monkeypatch.setattr(sdk.StreamableHTTPTransport, "_handle_sse_event", sdk.StreamableHTTPTransport._handle_sse_event)
    monkeypatch.setattr(
        sdk.StreamableHTTPTransport, "_handle_json_response", sdk.StreamableHTTPTransport._handle_json_response
    )
    install_mcp_size_guard(max_event_bytes=0, warn_event_bytes=WARN)
    t = sdk.StreamableHTTPTransport(GATEWAY_URL)
    send, recv = anyio.create_memory_object_stream(10)
    big_but_valid = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"pad": "x" * (CAP * 2)}})
    with caplog.at_level(logging.WARNING, logger="app.core.mcp_guard"):
        complete = await t._handle_sse_event(sse(big_but_valid), send)
    assert complete is True
    assert not isinstance(await drain_one(recv), Exception), "cap=0 must disable rejection"
    assert any("fat-server" in r.getMessage() for r in caplog.records), "warn stays active"


def test_warn_above_cap_is_clamped(monkeypatch, caplog):
    import mcp.client.streamable_http as sdk

    monkeypatch.setattr(sdk.StreamableHTTPTransport, "_handle_sse_event", sdk.StreamableHTTPTransport._handle_sse_event)
    monkeypatch.setattr(
        sdk.StreamableHTTPTransport, "_handle_json_response", sdk.StreamableHTTPTransport._handle_json_response
    )
    with caplog.at_level(logging.WARNING, logger="app.core.mcp_guard"):
        install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=CAP * 10)
    assert any("clamping warn" in r.getMessage() for r in caplog.records)


def test_install_is_idempotent(monkeypatch):
    import mcp.client.streamable_http as sdk

    monkeypatch.setattr(sdk.StreamableHTTPTransport, "_handle_sse_event", sdk.StreamableHTTPTransport._handle_sse_event)
    monkeypatch.setattr(
        sdk.StreamableHTTPTransport, "_handle_json_response", sdk.StreamableHTTPTransport._handle_json_response
    )
    install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=WARN)
    first = sdk.StreamableHTTPTransport._handle_sse_event
    install_mcp_size_guard(max_event_bytes=CAP, warn_event_bytes=WARN)
    assert sdk.StreamableHTTPTransport._handle_sse_event is first, "second install must not re-wrap"


def test_server_slug_extraction():
    assert _server_slug(GATEWAY_URL) == "fat-server"
    assert _server_slug(CONSOLE_URL) == "console.example"
    assert _server_slug("not a url") == "not a url"


def test_utf8_size_bounds():
    assert _utf8_size(b"abc", 10) == 3
    assert _utf8_size("abc", 10) == 3  # 3*4 >= 10 -> exact encode, still 3
    assert _utf8_size("a" * 100, 1000) == 100  # upper bound below threshold -> cheap path
    assert _utf8_size("\U0001f600", 2) == 4  # exact path counts bytes
