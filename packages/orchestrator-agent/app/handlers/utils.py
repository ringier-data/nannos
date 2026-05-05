import json
import logging
from typing import Any, Dict

import httpx
from langchain_core.tools import ToolException

from ..models import A2AClientError

logger = logging.getLogger(__name__)


def parse_tool_exception(exception: ToolException) -> Dict[str, Any]:
    """Parse a ToolException into a structured error dictionary."""
    logger.debug(f"Parsing ToolException: {exception}")
    try:
        structured_error = json.loads(str(exception))
    except json.JSONDecodeError:
        structured_error = {"errorCode": "unknown-error", "message": str(exception)}
    return structured_error


def handle_tool_failure(exception: Exception) -> str:
    """Handle tool failures and return error details for the LLM to reason about.

    For auth errors (need-credentials), returns structured JSON for AuthErrorDetectionMiddleware.
    For all other errors, returns the actual error message so the LLM can understand
    what went wrong and adapt its approach (e.g., fix parameters, skip the tool).
    """
    logger.error(f"Handling tool failure: {exception.__class__.__name__}: {exception}")
    if isinstance(exception, ToolException):
        structured_error = parse_tool_exception(exception)
        if structured_error.get("errorCode") == "need-credentials":
            # Return the original JSON structure for AuthErrorDetectionMiddleware to detect
            # This preserves errorCode, authorizeUrl, and message for proper auth handling
            return json.dumps(structured_error)
        # Return actual error message so the LLM can understand and adapt
        return str(exception)
    if isinstance(exception, httpx.HTTPStatusError) and exception.response.status_code == 401:
        return exception.response.text
    return f"Tool call failed: {exception}"


def should_retry(exception: Exception) -> bool:
    """Determine if an exception should trigger a retry.

    Only retries transient errors that may succeed on a subsequent attempt:
    - HTTP 5xx server errors (temporary server issues)
    - Network errors: connection failures, timeouts (httpx.HTTPError excluding HTTPStatusError)
    - A2A client errors (agent communication issues)

    Does NOT retry:
    - HTTP 4xx client errors (deterministic: bad request, forbidden, not found)
    - ToolException (deterministic: validation errors, permission issues — the LLM
      should see the error message and adapt its approach)
    - Any other exception type
    """
    logger.warning(f"Evaluating retry for exception: {exception.__class__.__name__}: {exception}")
    if isinstance(exception, httpx.HTTPStatusError):
        # Only retry 5xx server errors (transient)
        # 4xx errors (400, 401, 403, 404, 422) are deterministic and should not be retried
        return exception.response.status_code >= 500
    if isinstance(exception, httpx.HTTPError):
        # Network-level errors (ConnectError, TimeoutException, etc.) are transient
        return True
    if isinstance(exception, A2AClientError):
        return True
    # ToolException and all other exceptions are not retried — let the LLM see the error
    return False
