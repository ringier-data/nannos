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
