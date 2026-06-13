"""MCP error handling utilities for retry logic and user-friendly error messages.

This module also installs a process-wide guard around the upstream
``mcp.client.streamable_http`` transport so that endpoints returning an
unexpected content type (e.g. an HTML auth redirect or proxy error page)
fail the affected MCP request synchronously and surface a typed
:class:`MCPUnexpectedContentTypeError` instead of leaving the JSON-RPC
request hung until the caller's outer stall timeout fires.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------


class MCPUnexpectedContentTypeError(Exception):
    """Raised when an MCP endpoint returns a non-MCP content type.

    The upstream ``mcp`` SDK only logs ``Unexpected content type: ...`` and
    leaves the in-flight JSON-RPC request hanging. Our guard (see
    :func:`install_streamable_http_guard`) intercepts that path and forwards
    this typed exception so callers fail fast with full context.

    Attributes:
        url: The MCP endpoint URL that returned the bad content type.
        server_slug: Optional MCP server slug, when known by the caller.
        content_type: The offending ``Content-Type`` header value.
        body_snippet: Best-effort first ~200 bytes of the response body (or
            ``None`` if the body was not captured).
    """

    def __init__(
        self,
        url: Optional[str],
        server_slug: Optional[str],
        content_type: str,
        body_snippet: Optional[str] = None,
    ) -> None:
        self.url = url
        self.server_slug = server_slug
        self.content_type = content_type
        self.body_snippet = body_snippet
        super().__init__(_format_unexpected_content_type(url, server_slug, content_type, body_snippet))


def _likely_cause_hint(content_type: str, body_snippet: Optional[str]) -> str:
    ct = (content_type or "").lower()
    snippet_hint = ""
    if body_snippet:
        head = body_snippet.strip().splitlines()[0][:80] if body_snippet.strip() else ""
        if head:
            snippet_hint = f' — body started with "{head}"'
    if "html" in ct:
        return f"auth redirect, proxy error page, or wrong gateway URL{snippet_hint}"
    if "text/plain" in ct:
        return f"endpoint returned plain text instead of JSON/SSE — wrong URL or upstream error page{snippet_hint}"
    return f"endpoint returned non-MCP content; check URL and authentication{snippet_hint}"


def _format_unexpected_content_type(
    url: Optional[str],
    server_slug: Optional[str],
    content_type: str,
    body_snippet: Optional[str],
) -> str:
    parts = [f"MCP endpoint returned unexpected content type '{content_type}'"]
    if url:
        parts.append(f"for {url}")
    if server_slug:
        parts.append(f"(server slug: {server_slug})")
    parts.append(f"— likely cause: {_likely_cause_hint(content_type, body_snippet)}")
    return " ".join(parts)


def _find_in_exception_group(error: BaseException, cls: type) -> Optional[BaseException]:
    """Walk an ExceptionGroup (Python 3.11+) returning the first exception of *cls*, if any."""
    if isinstance(error, cls):
        return error
    if error.__class__.__name__ == "ExceptionGroup":
        for exc in getattr(error, "exceptions", []) or []:
            found = _find_in_exception_group(exc, cls)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Classification & formatting helpers
# ---------------------------------------------------------------------------


def is_retryable_mcp_error(error: Exception) -> bool:
    """Determine if an MCP error is retryable (transient).

    Retryable errors:
    - HTTP 502 Bad Gateway (gateway/backend unavailable)
    - HTTP 503 Service Unavailable
    - HTTP 504 Gateway Timeout
    - Network timeout errors

    Non-retryable errors:
    - HTTP 4xx (client errors, authentication failures)
    - Connection refused (service not running)
    - :class:`MCPUnexpectedContentTypeError` (endpoint is misconfigured, not transient)
    - Other permanent failures

    Args:
        error: Exception raised during MCP connection

    Returns:
        True if the error is transient and should be retried
    """
    # HTML / unexpected-content-type failures are misconfiguration, not transient.
    if _find_in_exception_group(error, MCPUnexpectedContentTypeError) is not None:
        return False

    # Handle ExceptionGroup (Python 3.11+) from anyio/MCP client
    if hasattr(error, "__class__") and error.__class__.__name__ == "ExceptionGroup":
        exceptions = getattr(error, "exceptions", [error])
        for exc in exceptions:
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                # Retry 502, 503, 504 (gateway/service issues)
                return status_code in (502, 503, 504)
            elif isinstance(exc, httpx.TimeoutException):
                # Retry timeouts
                return True

    # Handle direct httpx exceptions
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code in (502, 503, 504)
    elif isinstance(error, httpx.TimeoutException):
        return True

    # Don't retry connection errors (service not running)
    # Don't retry other errors (likely permanent)
    return False


def format_mcp_error(error: Exception) -> str:
    """Format MCP connection errors into user-friendly messages.

    Args:
        error: The exception raised during MCP connection

    Returns:
        User-friendly error message
    """
    # Unexpected-content-type takes precedence — even inside an ExceptionGroup.
    found = _find_in_exception_group(error, MCPUnexpectedContentTypeError)
    if isinstance(found, MCPUnexpectedContentTypeError):
        return str(found)

    # Handle ExceptionGroup (Python 3.11+) from anyio/MCP client
    if hasattr(error, "__class__") and error.__class__.__name__ == "ExceptionGroup":
        # Extract the first HTTPStatusError from the exception group
        exceptions = getattr(error, "exceptions", [error])
        for exc in exceptions:
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                url = exc.request.url

                if status_code == 502:
                    return f"MCP server gateway is unavailable (502 Bad Gateway for {url}). The backend service may be down or unreachable."
                elif status_code == 503:
                    return f"MCP server is temporarily unavailable (503 Service Unavailable for {url}). Please try again in a moment."
                elif status_code == 504:
                    return f"MCP server gateway timeout (504 Gateway Timeout for {url}). The backend service is not responding."
                elif 500 <= status_code < 600:
                    return f"MCP server error ({status_code} for {url}). The backend service encountered an error."
                elif status_code == 401:
                    return (
                        f"Authentication failed when connecting to MCP server ({url}). Please check your credentials."
                    )
                elif status_code == 403:
                    return f"Access denied to MCP server ({url}). You may not have permission to access this service."
                else:
                    return f"MCP server returned HTTP {status_code} for {url}."
            elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                return "Could not connect to MCP server. The service may be offline or network is unavailable."
            elif isinstance(exc, httpx.TimeoutException):
                return "MCP server connection timed out. The service may be slow or overloaded."

    # Handle direct httpx exceptions
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        url = error.request.url

        if status_code == 502:
            return f"MCP server gateway is unavailable (502 Bad Gateway for {url}). The backend service may be down or unreachable."
        elif status_code >= 500:
            return f"MCP server error ({status_code} for {url})."
        else:
            return f"MCP server returned HTTP {status_code} for {url}."
    elif isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "Could not connect to MCP server. The service may be offline or network is unavailable."
    elif isinstance(error, httpx.TimeoutException):
        return "MCP server connection timed out. The service may be slow or overloaded."

    # Fallback for other errors
    return f"Failed to connect to MCP server: {type(error).__name__}: {str(error)}"


# ---------------------------------------------------------------------------
# Streamable-HTTP guard: monkey-patch + log dedup
# ---------------------------------------------------------------------------


@dataclass
class _GuardContext:
    """Per-call context used by the streamable-HTTP guard to enrich captures."""

    url: Optional[str] = None
    server_slug: Optional[str] = None
    captured: list = field(default_factory=list)


_current_guard: contextvars.ContextVar[Optional[_GuardContext]] = contextvars.ContextVar(
    "_ringier_mcp_streamable_guard", default=None
)


class _UpstreamDedupFilter(logging.Filter):
    """Collapse repeated 'Unexpected content type' lines from the upstream MCP logger.

    Each connection in ``MultiServerMCPClient`` spins up its own transport, so a
    single broken endpoint can produce N identical log lines per refresh fan-out.
    This filter lets the first occurrence through within a rolling window and
    suppresses identical repeats until the window elapses, at which point a new
    line is allowed (so a still-broken endpoint stays visible over time).
    """

    def __init__(self, window_seconds: float = 30.0) -> None:
        super().__init__()
        self._window = float(window_seconds)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if not msg.startswith("Unexpected content type:"):
            return True
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(msg)
            if last is None or (now - last) > self._window:
                self._seen[msg] = now
                # Prune old entries opportunistically.
                if len(self._seen) > 64:
                    cutoff = now - self._window
                    self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
                return True
        return False


_PATCH_INSTALLED_ATTR = "_ringier_streamable_guard_installed"
_install_lock = threading.Lock()
_dedup_filter: Optional[_UpstreamDedupFilter] = None


def install_streamable_http_guard() -> None:
    """Idempotently install the streamable-HTTP guard.

    Effects:
    - Replaces ``StreamableHTTPTransport._handle_unexpected_content_type`` so
      that an unexpected content type forwards a typed exception into the
      read stream AND closes the stream — making any in-flight JSON-RPC
      request fail fast with ``CONNECTION_CLOSED`` instead of hanging.
    - Attaches a dedup filter to the ``mcp.client.streamable_http`` logger so
      the upstream warning is not repeated for the same endpoint within a
      short window.
    - Adds one structured ERROR log per occurrence to our own logger with
      the URL, server slug (if known via :func:`guarded_streamable_http`),
      content type, and a likely-cause hint.

    Safe to call from import-time and from multiple call sites.
    """
    global _dedup_filter

    with _install_lock:
        try:
            from mcp.client.streamable_http import (  # type: ignore[import-not-found]
                StreamableHTTPTransport,
            )
        except Exception:  # pragma: no cover - mcp not installed
            return

        if getattr(StreamableHTTPTransport, _PATCH_INSTALLED_ATTR, False):
            return

        async def _patched_handle_unexpected_content_type(
            self,  # type: ignore[no-untyped-def]
            content_type: str,
            read_stream_writer,
        ) -> None:
            url = getattr(self, "url", None)
            ctx = _current_guard.get()
            server_slug = ctx.server_slug if ctx is not None else None
            if ctx is not None and not ctx.url:
                ctx.url = str(url) if url is not None else None

            exc = MCPUnexpectedContentTypeError(
                url=str(url) if url is not None else None,
                server_slug=server_slug,
                content_type=content_type,
                body_snippet=None,
            )
            if ctx is not None:
                ctx.captured.append(exc)

            # One structured, actionable log per occurrence (the dedup filter on
            # the upstream logger suppresses the noisy companion line for repeats).
            logger.error(
                "MCP endpoint returned unexpected content type '%s' for %s "
                "(server_slug=%s) — likely cause: %s",
                content_type,
                url,
                server_slug,
                _likely_cause_hint(content_type, None),
            )

            # Forward the typed exception so the session's receive loop sees it,
            # then close the writer so any pending in-flight requests on this
            # session immediately receive CONNECTION_CLOSED rather than hanging.
            with contextlib.suppress(Exception):
                await read_stream_writer.send(exc)
            with contextlib.suppress(Exception):
                await read_stream_writer.aclose()

        StreamableHTTPTransport._handle_unexpected_content_type = (  # type: ignore[assignment]
            _patched_handle_unexpected_content_type
        )
        setattr(StreamableHTTPTransport, _PATCH_INSTALLED_ATTR, True)

        # Attach dedup filter to the upstream logger.
        if _dedup_filter is None:
            _dedup_filter = _UpstreamDedupFilter()
            logging.getLogger("mcp.client.streamable_http").addFilter(_dedup_filter)


@contextlib.asynccontextmanager
async def guarded_streamable_http(
    url: Optional[str] = None,
    server_slug: Optional[str] = None,
):
    """Async context manager that enriches and surfaces unexpected-content-type errors.

    Wrap MCP discovery / tool-call sites with this manager so that:

    1. The upstream "Unexpected content type" log is enriched with the caller's
       known ``server_slug`` (the transport only knows the URL).
    2. If the guard's monkey-patch fired during the block, a typed
       :class:`MCPUnexpectedContentTypeError` is raised at exit, chaining the
       generic ``Connection closed`` error the session would otherwise surface.

    The patch itself works without this wrapper — callers that don't wrap will
    still fail fast with the generic ``Connection closed`` from the session,
    plus the structured ERROR log from the patch.
    """
    install_streamable_http_guard()
    ctx = _GuardContext(url=url, server_slug=server_slug)
    token = _current_guard.set(ctx)
    try:
        try:
            yield
        except BaseException as exc:
            if ctx.captured:
                raise ctx.captured[0] from exc
            raise
        else:
            if ctx.captured:
                raise ctx.captured[0]
    finally:
        _current_guard.reset(token)


# Auto-install at import time so every MCP call site is protected without
# requiring the wrapper. Importing this module (already done by every call
# site that uses format_mcp_error / is_retryable_mcp_error) is enough.
install_streamable_http_guard()
