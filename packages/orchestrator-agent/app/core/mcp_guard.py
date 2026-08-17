"""Size guard for inbound MCP SSE events.

Every MCP message the orchestrator receives — tool catalogues at discovery,
tool results mid-turn, code-mode sessions — arrives as a Server-Sent Event that
the MCP SDK parses with ``JSONRPCMessage.model_validate_json`` and NO size
bound (``mcp/client/streamable_http.py``). Parsing amplifies the wire size
roughly 7x into live pydantic objects, and several parses run concurrently, so
a single oversized payload can OOM the pod.

This is not hypothetical: in prod, one gateway server answered ``tools/list``
with a 21.9 MB single event; cold-cache turns peaked at 2.3-4.2 GB RSS and the
pod was OOMKilled repeatedly (2026-08-15/17, see
alloy-ch/rcplus-alloy-infrastructure-agents#241). Nothing named the offending
server, so the failure surfaced as an unexplained pod kill instead of a config
error.

The guard wraps ``StreamableHTTPTransport._handle_sse_event`` process-wide —
the SDK offers no hook at the parse site — and does two things:

* **rejects** any event over ``MCP_SSE_MAX_EVENT_BYTES`` *before* it is
  parsed, raising :class:`McpEventTooLargeError` that names the server slug
  and size. Discovery already degrades gracefully per server
  (``_get_tools_with_retry`` catches and logs per-slug failures), so a
  rejected catalogue costs one server's tools, not the process. A rejected
  tool result surfaces as that tool call's error.
* **logs** every event over ``MCP_SSE_WARN_EVENT_BYTES`` with the server slug,
  so catalogue growth is a visible trend long before it reaches the cap.

Sizes are measured on the raw SSE ``data`` string, before any parsing.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_installed = False


class McpEventTooLargeError(RuntimeError):
    """An inbound MCP SSE event exceeded the configured size cap."""

    def __init__(self, server: str, size_bytes: int, max_bytes: int) -> None:
        self.server = server
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"MCP SSE event from server '{server}' is {size_bytes / 2**20:.1f}MB, "
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


def install_mcp_size_guard(max_event_bytes: int, warn_event_bytes: int) -> None:
    """Wrap the MCP transport's SSE handler with the size guard. Idempotent.

    Must be called once at startup, before any MCP session is opened. Patching
    the class method covers every ``StreamableHTTPTransport`` in the process —
    discovery, tool dispatch and code-mode sessions alike — which is the point:
    the failure mode does not care which path the fat payload arrives on.
    """
    global _installed
    if _installed:
        return

    from mcp.client.streamable_http import StreamableHTTPTransport

    original = StreamableHTTPTransport._handle_sse_event

    async def _guarded_handle_sse_event(self, sse, *args, **kwargs):
        data = getattr(sse, "data", None)
        if data:
            size = len(data)
            if size > max_event_bytes:
                server = _server_slug(self.url)
                logger.error(
                    "[MCP-GUARD] rejecting %.1fMB SSE event from server=%s (cap %.1fMB)",
                    size / 2**20,
                    server,
                    max_event_bytes / 2**20,
                )
                raise McpEventTooLargeError(server, size, max_event_bytes)
            if size > warn_event_bytes:
                logger.warning(
                    "[MCP-GUARD] large SSE event: %.1fMB from server=%s "
                    "(cap %.1fMB — growth here eventually becomes an outage)",
                    size / 2**20,
                    _server_slug(self.url),
                    max_event_bytes / 2**20,
                )
        return await original(self, sse, *args, **kwargs)

    StreamableHTTPTransport._handle_sse_event = _guarded_handle_sse_event
    _installed = True
    logger.info(
        "MCP SSE size guard installed (cap=%.1fMB, warn=%.1fMB)",
        max_event_bytes / 2**20,
        warn_event_bytes / 2**20,
    )
