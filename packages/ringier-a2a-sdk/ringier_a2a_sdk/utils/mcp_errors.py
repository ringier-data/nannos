"""MCP error handling utilities for retry logic and user-friendly error messages."""

import httpx


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
        True if the error is transient and should be retried
    """
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


def get_mcp_http_status_error(error: Exception) -> httpx.HTTPStatusError | None:
    """Extract the underlying httpx.HTTPStatusError from an MCP connection error.

    Unwraps ExceptionGroup (from anyio task groups used by the streamable HTTP
    client) the same way is_retryable_mcp_error/format_mcp_error do, so callers
    can log the server's actual response body instead of just the status code —
    raise_for_status() alone discards it.

    Args:
        error: Exception raised during MCP connection

    Returns:
        The httpx.HTTPStatusError if one is found, else None
    """
    if hasattr(error, "__class__") and error.__class__.__name__ == "ExceptionGroup":
        exceptions = getattr(error, "exceptions", [error])
        for exc in exceptions:
            if isinstance(exc, httpx.HTTPStatusError):
                return exc

    if isinstance(error, httpx.HTTPStatusError):
        return error

    return None


def get_mcp_error_body(http_err: httpx.HTTPStatusError, max_len: int = 2000) -> str:
    """Safely read the response body text off an HTTPStatusError for logging.

    The MCP streamable HTTP client streams responses and never calls read()
    before raise_for_status(), so response.text raises httpx.ResponseNotRead
    for MCP gateway errors (though not for a plain, non-streaming request like
    ToolDiscoveryService's httpx.AsyncClient.get()). Callers just want a string
    to log either way, so this never raises.

    Args:
        http_err: The HTTPStatusError to read the body from
        max_len: Truncate the body to this many characters

    Returns:
        The response body text (truncated), or a placeholder if it can't be read
    """
    try:
        return http_err.response.text[:max_len]
    except Exception as e:
        return f"<body unavailable: {type(e).__name__}: {e}>"


def format_mcp_error(error: Exception) -> str:
    """Format MCP connection errors into user-friendly messages.

    Args:
        error: The exception raised during MCP connection

    Returns:
        User-friendly error message
    """
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
