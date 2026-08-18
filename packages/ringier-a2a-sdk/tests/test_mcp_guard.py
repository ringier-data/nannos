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

from ringier_a2a_sdk.utils.mcp_guard import (
    DEFAULT_MAX_EVENT_BYTES,
    DEFAULT_WARN_EVENT_BYTES,
    McpEventTooLargeError,
    _server_slug,
    _utf8_size,
    install_mcp_size_guard,
    install_mcp_size_guard_from_env,
    int_env,
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


@pytest.mark.asyncio
async def test_small_event_parses_and_delivers(real_transport):
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    complete = await t._handle_sse_event(sse(SMALL_RESULT), send)
    delivered = await drain_one(recv)
    # A JSONRPC response both completes the stream and reaches the session.
    assert complete is True
    assert not isinstance(delivered, Exception)


@pytest.mark.asyncio
async def test_warn_level_logs_slug_but_still_parses(real_transport, caplog):
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    # Valid JSONRPC padded past the warn level but under the cap.
    padded = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"pad": "x" * (WARN + 1)}})
    assert WARN < len(padded) <= CAP
    with caplog.at_level(logging.WARNING, logger="ringier_a2a_sdk.utils.mcp_guard"):
        complete = await t._handle_sse_event(sse(padded), send)
    delivered = await drain_one(recv)
    assert complete is True
    assert not isinstance(delivered, Exception)
    assert any("fat-server" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
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
    with caplog.at_level(logging.ERROR, logger="ringier_a2a_sdk.utils.mcp_guard"):
        complete = await t._handle_sse_event(sse("x" * (CAP + 1)), send)

    assert complete is True, "must report completion so the SDK does not reconnect and replay"
    delivered = await drain_one(recv)
    assert isinstance(delivered, McpEventTooLargeError)
    assert delivered.server == "fat-server"
    assert delivered.size_bytes == CAP + 1
    assert any("fat-server" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_small_json_response_delegates_to_sdk(real_transport):
    class FakeResponse:
        async def aread(self):
            return SMALL_RESULT.encode()

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    await t._handle_json_response(FakeResponse(), send)
    delivered = await drain_one(recv)
    assert not isinstance(delivered, Exception)


@pytest.mark.asyncio
async def test_multibyte_payload_measured_in_bytes(real_transport):
    """4-byte emoji: character count sits under the cap, byte size over it."""
    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    payload = "\U0001f600" * ((CAP // 4) + 1)  # chars ~ CAP/4, bytes > CAP
    assert len(payload) <= CAP < len(payload.encode())
    complete = await t._handle_sse_event(sse(payload), send)
    assert complete is True
    assert isinstance(await drain_one(recv), McpEventTooLargeError)


@pytest.mark.asyncio
async def test_priming_and_non_message_events_untouched(real_transport):
    t = real_transport()
    send, _recv = anyio.create_memory_object_stream(10)
    # Priming event: no data, only an ID (resumability) — SDK returns False.
    assert await t._handle_sse_event(SimpleNamespace(data="", event="message", id="7", retry=None), send) is False
    # Non-message events are never parsed and must not be size-checked.
    assert await t._handle_sse_event(sse("x" * (CAP + 1), event="ping"), send) is False


@pytest.mark.asyncio
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
    with caplog.at_level(logging.WARNING, logger="ringier_a2a_sdk.utils.mcp_guard"):
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
    with caplog.at_level(logging.WARNING, logger="ringier_a2a_sdk.utils.mcp_guard"):
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


@pytest.mark.asyncio
async def test_oversized_with_recoverable_id_becomes_jsonrpc_error(real_transport):
    """When the payload's id is recoverable, the rejection is a routable
    JSONRPCError — the form the session actually matches to a pending request."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCError

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    payload = '{"jsonrpc":"2.0","id":42,"result":{"pad":"' + "x" * (CAP + 1) + '"}}'
    complete = await t._handle_sse_event(sse(payload), send)
    assert complete is True
    delivered = await drain_one(recv)
    assert isinstance(delivered, SessionMessage)
    assert isinstance(delivered.message.root, JSONRPCError)
    assert delivered.message.root.id == 42
    assert "fat-server" in delivered.message.root.error.message


@pytest.mark.asyncio
async def test_session_pending_request_actually_fails(real_transport):
    """End-to-end across the session boundary: a ClientSession's pending
    list_tools request must FAIL (not hang) when the guard rejects the
    oversized response. This is the scenario that survived round 2's fix —
    a bare Exception on the read stream routes to a no-op handler while
    send_request waits with timeout=None."""
    from mcp import ClientSession
    from mcp.shared.exceptions import McpError
    from mcp.shared.message import SessionMessage

    t = real_transport()

    # session <- read_recv ; guard writes into read_send
    read_send, read_recv = anyio.create_memory_object_stream(16)
    write_send, write_recv = anyio.create_memory_object_stream(16)

    async with ClientSession(read_recv, write_send) as session:
        result: dict = {}

        async def run_request():
            try:
                await session.send_request(_list_tools_request(), _ListToolsResultType())
            except McpError as e:
                result["error"] = e
            except Exception as e:  # pragma: no cover - diagnostic
                result["error"] = e

        def _list_tools_request():
            from mcp.types import ClientRequest, ListToolsRequest

            return ClientRequest(ListToolsRequest(method="tools/list"))

        def _ListToolsResultType():
            from mcp.types import ListToolsResult

            return ListToolsResult

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_request)
            # Capture the outgoing request to learn its id.
            with anyio.fail_after(2):
                outgoing: SessionMessage = await write_recv.receive()
            request_id = outgoing.message.root.id

            # The server "responds" with an oversized payload carrying that id.
            payload = (
                '{"jsonrpc":"2.0","id":' + str(request_id) + ',"result":{"tools":[],"pad":"' + "x" * (CAP + 1) + '"}}'
            )
            complete = await t._handle_sse_event(sse(payload), read_send)
            assert complete is True

            with anyio.fail_after(2):
                while "error" not in result:
                    await anyio.sleep(0.01)

    err = result["error"]
    assert "fat-server" in str(err), f"pending request must fail with the guard's message, got: {err!r}"


def test_extract_request_id_variants():
    from ringier_a2a_sdk.utils.mcp_guard import _extract_request_id

    assert _extract_request_id('{"jsonrpc":"2.0","id":42,"result":{}}') == 42
    assert _extract_request_id('{"jsonrpc":"2.0","id":-7,"result":{}}') == -7
    assert _extract_request_id('{"jsonrpc":"2.0","id":"abc","result":{}}') == "abc"
    assert _extract_request_id("no ids here") is None
    # bytes path scans past the 4KB head, matching the str path's behaviour
    late = b'{"jsonrpc":"2.0","result":{"pad":"' + b"x" * 5000 + b'"},"id":9}'
    assert _extract_request_id(late) == 9


# ── install_mcp_size_guard_from_env ───────────────────────────────────────────
#
# The entry point voice-agent and agent-runner call at startup: services with no
# typed settings object of their own get the same thresholds from env, so the
# guard cannot end up installed with silently different limits per service
# (ringier-data/nannos#155).


@pytest.fixture
def captured_install(monkeypatch):
    """Capture the thresholds install_mcp_size_guard_from_env resolves."""
    seen = {}

    def fake_install(max_event_bytes: int, warn_event_bytes: int) -> None:
        seen["max"] = max_event_bytes
        seen["warn"] = warn_event_bytes

    monkeypatch.setattr("ringier_a2a_sdk.utils.mcp_guard.install_mcp_size_guard", fake_install)
    return seen


def test_from_env_uses_shared_defaults_when_unset(monkeypatch, captured_install):
    monkeypatch.delenv("MCP_SSE_MAX_EVENT_BYTES", raising=False)
    monkeypatch.delenv("MCP_SSE_WARN_EVENT_BYTES", raising=False)
    install_mcp_size_guard_from_env()
    assert captured_install == {"max": DEFAULT_MAX_EVENT_BYTES, "warn": DEFAULT_WARN_EVENT_BYTES}


def test_from_env_reads_overrides(monkeypatch, captured_install):
    monkeypatch.setenv("MCP_SSE_MAX_EVENT_BYTES", "2048")
    monkeypatch.setenv("MCP_SSE_WARN_EVENT_BYTES", "0")  # 0 disables the warn threshold
    install_mcp_size_guard_from_env()
    assert captured_install == {"max": 2048, "warn": 0}


@pytest.mark.parametrize("garbage", ["", "   ", "10MB", "not-a-number"])
def test_from_env_falls_back_on_unparseable_value(monkeypatch, captured_install, garbage):
    """A typo in a manifest must not leave the pod unguarded — fail safe, not open."""
    monkeypatch.setenv("MCP_SSE_MAX_EVENT_BYTES", garbage)
    monkeypatch.delenv("MCP_SSE_WARN_EVENT_BYTES", raising=False)
    install_mcp_size_guard_from_env()
    assert captured_install["max"] == DEFAULT_MAX_EVENT_BYTES


def test_from_env_actually_installs_the_guard(monkeypatch):
    """End-to-end: the env path patches the real transport, like a service startup."""
    import mcp.client.streamable_http as sdk

    monkeypatch.setattr(sdk.StreamableHTTPTransport, "_handle_sse_event", sdk.StreamableHTTPTransport._handle_sse_event)
    monkeypatch.setattr(
        sdk.StreamableHTTPTransport, "_handle_json_response", sdk.StreamableHTTPTransport._handle_json_response
    )
    monkeypatch.setenv("MCP_SSE_MAX_EVENT_BYTES", str(CAP))
    monkeypatch.setenv("MCP_SSE_WARN_EVENT_BYTES", str(WARN))
    install_mcp_size_guard_from_env()
    assert getattr(sdk.StreamableHTTPTransport._handle_sse_event, "_nannos_mcp_guard", False)


# ── original_request_id routing ───────────────────────────────────────────────
#
# The SDK rewrites a response's id to ``original_request_id`` on the
# resumption/reconnect path (``_handle_sse_event``). A rejection routed by the
# id scraped from the payload would then name the PRE-resumption request and
# match nothing — hanging the very request it is supposed to fail.


@pytest.mark.asyncio
async def test_rejection_uses_original_request_id_when_the_sdk_supplies_one(real_transport):
    from mcp.types import JSONRPCError

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    # payload says id=1; the SDK is resuming request id=99
    payload = '{"jsonrpc":"2.0","id":1,"result":{"pad":"' + "x" * (CAP + 1) + '"}}'
    complete = await t._handle_sse_event(sse(payload), send, 99)
    assert complete is True
    delivered = await drain_one(recv)
    assert isinstance(delivered.message.root, JSONRPCError)
    assert delivered.message.root.id == 99, "must route to the SDK's id, not the payload's"


@pytest.mark.asyncio
async def test_rejection_falls_back_to_the_scraped_id(real_transport):
    """Without a resumption id the payload scrape is still the right answer."""
    from mcp.types import JSONRPCError

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    payload = '{"jsonrpc":"2.0","id":7,"result":{"pad":"' + "x" * (CAP + 1) + '"}}'
    await t._handle_sse_event(sse(payload), send)
    delivered = await drain_one(recv)
    assert isinstance(delivered.message.root, JSONRPCError)
    assert delivered.message.root.id == 7


@pytest.mark.asyncio
async def test_original_request_id_is_forwarded_on_the_pass_through_path(real_transport):
    """Under the cap the SDK must still receive the id, or it cannot rewrite it."""
    from mcp.types import JSONRPCResponse

    t = real_transport()
    send, recv = anyio.create_memory_object_stream(10)
    await t._handle_sse_event(sse(SMALL_RESULT), send, 42)
    delivered = await drain_one(recv)
    assert isinstance(delivered.message.root, JSONRPCResponse)
    assert delivered.message.root.id == 42, "guard must not swallow the SDK's id rewrite"


# ── int_env ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 120),
        ("", 120),
        ("   ", 120),
        ("nope", 120),
        ("30s", 120),
        ("0", 0),
        ("45", 45),
        (" 45 ", 45),
    ],
)
def test_int_env_fails_safe(monkeypatch, raw, expected):
    """A typo must fall back to the default, never raise into the caller."""
    if raw is None:
        monkeypatch.delenv("SOME_KNOB", raising=False)
    else:
        monkeypatch.setenv("SOME_KNOB", raw)
    assert int_env("SOME_KNOB", 120) == expected
