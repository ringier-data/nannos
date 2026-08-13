"""MCP error handling utilities for retry logic and user-friendly error messages."""

import logging

import httpx


def flatten_exceptions(error: BaseException) -> list[BaseException]:
    """Recursively flatten ExceptionGroups into their leaf exceptions.

    anyio's task groups (used by the MCP streamable-HTTP client) commonly nest
    ExceptionGroups, and mixed-cancellation groups surface as BaseExceptionGroup
    — a single-level unwrap misses both. Public so other packages needing the
    same unwrap (e.g. voice-agent's MCP gateway warm-up) share one copy instead
    of drifting independently.
    """
    if isinstance(error, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in error.exceptions:
            leaves.extend(flatten_exceptions(sub))
        return leaves
    return [error]


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
    - Other permanent failures

    Args:
        error: Exception raised during MCP connection

    Returns:
        True if the error is transient and should be retried. A group counts
        as retryable if *any* leaf is — e.g. a gateway + console connection
        failing together as [HTTPStatusError(400), ConnectTimeout()] must not
        let the non-retryable leaf mask the genuinely transient one.
    """
    for leaf in flatten_exceptions(error):
        if isinstance(leaf, httpx.HTTPStatusError):
            if leaf.response.status_code in (502, 503, 504):
                return True
        elif isinstance(leaf, httpx.TimeoutException):
            return True

    # Don't retry connection errors (service not running)
    # Don't retry other errors (likely permanent)
    return False


def get_mcp_http_status_error(error: Exception) -> httpx.HTTPStatusError | None:
    """Extract the underlying httpx.HTTPStatusError from an MCP connection error.

    Recursively unwraps ExceptionGroups (from anyio task groups used by the
    streamable HTTP client) so callers can log the server's actual response
    body instead of just the status code — raise_for_status() alone discards it.

    Args:
        error: Exception raised during MCP connection

    Returns:
        The httpx.HTTPStatusError if one is found, else None
    """
    for leaf in flatten_exceptions(error):
        if isinstance(leaf, httpx.HTTPStatusError):
            return leaf
    return None


def get_mcp_error_body(http_err: httpx.HTTPStatusError, max_len: int = 2000) -> str:
    """Safely read the response body text off an HTTPStatusError for logging.

    The MCP streamable HTTP client (mcp.client.streamable_http) calls
    raise_for_status() from inside its `async with client.stream(...)` block;
    by the time the error reaches us here, several `__aexit__` frames up, the
    stream has already been torn down and the body can never be read. This is
    expected for that transport (not for a plain, non-streaming request like
    ToolDiscoveryService's httpx.AsyncClient.get(), where the body is already
    buffered) — so it's reported as a known limitation rather than a second
    error. Callers just want a string to log either way, so this never raises.

    Args:
        http_err: The HTTPStatusError to read the body from
        max_len: Truncate the body to this many characters

    Returns:
        The response body text (truncated), or an explanatory placeholder if
        it can't be read
    """
    try:
        return http_err.response.text[:max_len]
    except httpx.ResponseNotRead:
        return "<not captured: MCP streaming transport closes the response before it can be read>"
    except Exception as e:
        return f"<body unavailable: {type(e).__name__}: {e}>"


def log_mcp_gateway_error(logger: logging.Logger, error: Exception, context: str = "") -> None:
    """Log an MCP gateway's actual HTTP response (status + body) for a connection error.

    No-op if `error` doesn't wrap an httpx.HTTPStatusError — there's nothing to
    add beyond what the caller already logged. Redacts the body for 401/403:
    an auth-rejection body can echo back token/session details that shouldn't
    land in log storage (unlike other 4xx/5xx bodies, which are safe to log —
    see get_mcp_error_body).

    Args:
        logger: The caller's module logger
        error: Exception raised during MCP connection
        context: Optional short prefix identifying the caller (e.g. "for my-agent "),
            included verbatim before the URL in the log line
    """
    http_err = get_mcp_http_status_error(error)
    if http_err is None:
        return
    status = http_err.response.status_code
    body = "<redacted: auth error body withheld from logs>" if status in (401, 403) else get_mcp_error_body(http_err)
    logger.error(f"MCP gateway response {context}({http_err.request.url}): {status} body={body!r}")


def format_mcp_error(error: Exception) -> str:
    """Format MCP connection errors into user-friendly messages.

    Args:
        error: The exception raised during MCP connection

    Returns:
        User-friendly error message
    """
    for leaf in flatten_exceptions(error):
        if isinstance(leaf, httpx.HTTPStatusError):
            status_code = leaf.response.status_code
            url = leaf.request.url

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
        elif isinstance(leaf, (httpx.ConnectError, httpx.ConnectTimeout)):
            return "Could not connect to MCP server. The service may be offline or network is unavailable."
        elif isinstance(leaf, httpx.TimeoutException):
            return "MCP server connection timed out. The service may be slow or overloaded."

    # Fallback for other errors
    return f"Failed to connect to MCP server: {type(error).__name__}: {str(error)}"
