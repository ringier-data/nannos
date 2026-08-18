"""Size guard for inbound MCP messages.

Every MCP message the orchestrator receives — tool catalogues at discovery,
tool results mid-turn, code-mode sessions — is parsed by the MCP SDK with
``JSONRPCMessage.model_validate_json`` and NO size bound
(``mcp/client/streamable_http.py``). Parsing amplifies the wire size roughly
7x into live pydantic objects, and several parses run concurrently, so a
single oversized payload can OOM the pod.

This is not hypothetical: one gateway server answered ``tools/list`` with a
21.9 MB single event; cold-cache turns peaked at 2.3-4.2 GB RSS and the pod was
OOMKilled repeatedly. Nothing named the offending server, so the failure
surfaced as an unexplained pod kill instead of a config error. The full
write-up is in ringier-data/nannos#152.

The guard wraps the transport's two inbound parse paths process-wide — the SDK
offers no hook at the parse site — and does two things:

* **rejects** any payload over ``MCP_SSE_MAX_EVENT_BYTES`` *before* it is
  parsed. Rejection follows the SDK's own parse-failure contract: the error is
  delivered through ``read_stream_writer`` so the pending request FAILS with
  :class:`McpEventTooLargeError` naming the server slug and size. It must not
  be raised out of the handler instead — every SDK call site swallows
  exceptions (``except Exception: logger.debug``) and then *reconnects with
  Last-Event-ID*, which would replay the same oversized payload and leave the
  pending request hanging on a permanently-held semaphore slot. Discovery
  already degrades gracefully per server (``_get_tools_with_retry``), so a
  rejected catalogue costs one server's tools, not the process; a rejected
  tool result surfaces as that call's error.
* **logs** every payload over ``MCP_SSE_WARN_EVENT_BYTES`` with the server
  slug, so catalogue growth is a visible trend long before it reaches the cap.

Sizes are measured in UTF-8 **bytes** (what the parser and the allocator see),
not characters: SSE ``data`` is already str-decoded, and a multibyte-heavy
catalogue can weigh up to 4x its character count. The exact encode is only
paid when the cheap character-count bounds cannot decide.

Setting a threshold to ``0`` disables that threshold.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_WRAP_MARKER = "_nannos_mcp_guard"


class McpEventTooLargeError(RuntimeError):
    """An inbound MCP message exceeded the configured size cap."""

    def __init__(self, server: str, size_bytes: int, max_bytes: int) -> None:
        self.server = server
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"MCP message from server '{server}' is {size_bytes / 2**20:.1f}MB, "
            f"exceeding the {max_bytes / 2**20:.1f}MB cap (MCP_SSE_MAX_EVENT_BYTES). "
            f"Refusing to parse it — an unbounded parse of this payload is what "
            f"OOMKilled the orchestrator (see ringier-data/nannos#152). "
            f"Trim this server's catalogue/results at the gateway, or raise the cap "
            f"only with a matching memory-limit review."
        )


def _server_slug(url: str) -> str:
    """Best-effort server identity for logs: the gateway slug, else the host.

    Gateway connections carry ``includeOnlyServerSlugs=<slug>``; other MCP
    endpoints (e.g. the console backend) are identified by their netloc.
    """
    try:
        parsed = urlparse(url)
        slugs = parse_qs(parsed.query).get("includeOnlyServerSlugs")
        return slugs[0] if slugs else (parsed.netloc or url)
    except Exception:
        return url


def _utf8_size(data: str | bytes, decide_at: int) -> int:
    """Size of *data* in bytes, paying the exact encode only when it matters.

    For ``str``, character count is a lower bound and 4x it an upper bound on
    the UTF-8 byte size; when the upper bound stays below ``decide_at`` the
    exact size is irrelevant and the lower bound is returned as-is.
    """
    if isinstance(data, bytes):
        return len(data)
    chars = len(data)
    if decide_at <= 0 or chars * 4 < decide_at:
        return chars
    return len(data.encode("utf-8", errors="replace"))


class _SizeVerdict:
    __slots__ = ("error", "size")

    def __init__(self, size: int, error: McpEventTooLargeError | None) -> None:
        self.size = size
        self.error = error


def _check(data: str | bytes, url: str, max_bytes: int, warn_bytes: int) -> _SizeVerdict:
    decide_at = min(t for t in (max_bytes, warn_bytes) if t > 0) if (max_bytes > 0 or warn_bytes > 0) else 0
    size = _utf8_size(data, decide_at)
    if max_bytes > 0 and size > max_bytes:
        return _SizeVerdict(size, McpEventTooLargeError(_server_slug(url), size, max_bytes))
    if warn_bytes > 0 and size > warn_bytes:
        logger.warning(
            "[MCP-GUARD] large MCP message: %.1fMB from server=%s "
            "(warn threshold %.1fMB, cap %s — growth here eventually becomes an outage)",
            size / 2**20,
            _server_slug(url),
            warn_bytes / 2**20,
            f"{max_bytes / 2**20:.1f}MB" if max_bytes > 0 else "disabled",
        )
    return _SizeVerdict(size, None)


# Top-level JSONRPC ids in practice appear early ({"jsonrpc":"2.0","id":..,...}),
# and the first "id" key in a response document is the top-level one whenever
# "result" follows it. A bounded head-scan plus full-scan fallback is O(n) on the
# raw string with NO parse amplification — the entire point of rejecting here.
_ID_RE = re.compile(r'"id"\s*:\s*(-?\d+|"(?:[^"\\]|\\.){1,256}")')


def _extract_request_id(data: str | bytes):
    """Best-effort top-level ``id`` of a JSONRPC payload, without parsing it.

    Returns an ``int`` or ``str``, or ``None`` when nothing safe was found.

    Known imprecision, accepted deliberately: when the head scan misses and the
    full-string fallback picks up a body-level ``"id"``, the value can collide
    with a DIFFERENT in-flight request on a multiplexed session (ids are small
    sequential ints). The blast radius is bounded: that innocent request fails
    fast with a correctly-attributed size error, and the truly pending one is
    reaped by the MCP_DISCOVERY_TIMEOUT_S backstop. Escaped-string ids are also
    returned verbatim (not unescaped) and will simply match nothing —
    degrading, like every miss here, to the bare-exception fallback.
    """
    if isinstance(data, bytes):
        head = data[:4096].decode("utf-8", errors="replace")
        m = _ID_RE.search(head) or _ID_RE.search(data.decode("utf-8", errors="replace"))
    else:
        m = _ID_RE.search(data, 0, 4096) or _ID_RE.search(data)
    if not m:
        return None
    raw = m.group(1)
    if raw.startswith('"'):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        return None


async def _reject(read_stream_writer, data, error: McpEventTooLargeError) -> None:
    """Fail the pending request per the SDK's routing rules.

    Only a ``JSONRPCError`` whose ``id`` matches the pending request completes
    ``send_request`` (which otherwise waits with ``timeout=None``); a bare
    ``Exception`` on the read stream lands in ``_handle_incoming``, a no-op.
    So: synthesize a JSONRPCError with the id recovered from the raw payload,
    falling back to the bare exception when no id can be recovered.
    """
    from mcp.shared.message import SessionMessage
    from mcp.types import INTERNAL_ERROR, ErrorData, JSONRPCError, JSONRPCMessage

    logger.error("[MCP-GUARD] %s", error)
    request_id = _extract_request_id(data)
    try:
        if request_id is not None:
            reply = JSONRPCMessage(
                JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=ErrorData(code=INTERNAL_ERROR, message=str(error)),
                )
            )
            await read_stream_writer.send(SessionMessage(reply))
        else:
            logger.warning(
                "[MCP-GUARD] could not recover a request id from the oversized payload; "
                "delivering a bare exception (the pending request will rely on the caller's timeout)"
            )
            await read_stream_writer.send(error)
    except Exception:  # writer already closed — nothing to notify
        logger.debug("[MCP-GUARD] read_stream_writer closed while rejecting oversized message")


def install_mcp_size_guard(max_event_bytes: int, warn_event_bytes: int) -> None:
    """Wrap the MCP transport's inbound parse paths with the size guard.

    Idempotent. Must be called once at startup, before any MCP session is
    opened. Patching the class methods covers every ``StreamableHTTPTransport``
    in the process — discovery, tool dispatch and code-mode sessions alike —
    which is the point: the failure mode does not care which path the fat
    payload arrives on.

    ``max_event_bytes <= 0`` disables rejection; ``warn_event_bytes <= 0``
    disables the warning. A warn threshold above an enabled cap is clamped to
    the cap (an event cannot warn after it has already been rejected).
    """
    from mcp.client.streamable_http import StreamableHTTPTransport

    if getattr(StreamableHTTPTransport._handle_sse_event, _WRAP_MARKER, False):
        return

    if max_event_bytes > 0 and warn_event_bytes > max_event_bytes:
        logger.warning(
            "MCP_SSE_WARN_EVENT_BYTES (%d) exceeds MCP_SSE_MAX_EVENT_BYTES (%d); clamping warn to the cap",
            warn_event_bytes,
            max_event_bytes,
        )
        warn_event_bytes = max_event_bytes

    original_sse = StreamableHTTPTransport._handle_sse_event
    original_json = StreamableHTTPTransport._handle_json_response

    async def _guarded_handle_sse_event(self, sse, read_stream_writer, *args, **kwargs):
        # Only "message" events are ever parsed by the SDK; priming/unknown
        # events pass through untouched.
        if getattr(sse, "event", None) == "message" and getattr(sse, "data", None):
            verdict = _check(sse.data, self.url, max_event_bytes, warn_event_bytes)
            if verdict.error is not None:
                # Fail the pending request (JSONRPCError routed by request id —
                # see _reject) and report the stream complete so the caller
                # closes the response instead of reconnecting with
                # Last-Event-ID and replaying the same oversized payload.
                await _reject(read_stream_writer, sse.data, verdict.error)
                return True
        return await original_sse(self, sse, read_stream_writer, *args, **kwargs)

    async def _guarded_handle_json_response(self, response, read_stream_writer, *args, **kwargs):
        # Same parse, different transport shape (application/json bodies).
        # httpx caches aread(), so the original's own aread() is a no-op replay.
        try:
            content = await response.aread()
        except Exception:
            return await original_json(self, response, read_stream_writer, *args, **kwargs)
        verdict = _check(content, self.url, max_event_bytes, warn_event_bytes)
        if verdict.error is not None:
            await _reject(read_stream_writer, content, verdict.error)
            return None
        return await original_json(self, response, read_stream_writer, *args, **kwargs)

    setattr(_guarded_handle_sse_event, _WRAP_MARKER, True)
    setattr(_guarded_handle_json_response, _WRAP_MARKER, True)
    StreamableHTTPTransport._handle_sse_event = _guarded_handle_sse_event
    StreamableHTTPTransport._handle_json_response = _guarded_handle_json_response
    logger.info(
        "MCP message size guard installed (cap=%s, warn=%s)",
        f"{max_event_bytes / 2**20:.1f}MB" if max_event_bytes > 0 else "disabled",
        f"{warn_event_bytes / 2**20:.1f}MB" if warn_event_bytes > 0 else "disabled",
    )
