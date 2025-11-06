
import json
from typing import Any, Dict
import logging
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


def handle_auth_error(exception: Exception) -> str:
    """Handle authentication errors and return structured response for auth middleware detection."""
    logger.error(f"Handling auth error: {exception.__class__.__name__}: {exception}")
    if isinstance(exception, ToolException):
        structured_error = parse_tool_exception(exception)
        if structured_error.get("errorCode") == "need-credentials":
            # Return the original JSON structure for AuthErrorDetectionMiddleware to detect
            # This preserves errorCode, authorizeUrl, and message for proper auth handling
            return json.dumps(structured_error)
    if isinstance(exception, httpx.HTTPStatusError) and exception.response.status_code == 401:
        # TODO: Customize the message based on the auth flow
        return exception.response.text
    else:
        logger.error(f"Unexpected error occurred: {exception}")
        return "An unexpected error occurred."  # TODO: we could pass it to the llm but be careful about leaking info


def should_retry(exception: Exception) -> bool:
    """Determine if an exception should trigger a retry.
    
    Returns False for 401 errors (auth issues should not be retried automatically),
    Returns True for other HTTP errors, network errors, and A2A client errors.
    """
    logger.error(f"Evaluating retry for exception: {exception.__class__.__name__}: {exception}")
    if isinstance(exception, httpx.HTTPStatusError):
        # Don't retry 401 Unauthorized - requires user intervention
        if exception.response.status_code == 401:
            return False
    if isinstance(exception, ToolException):
        structured_error = parse_tool_exception(exception)
        if structured_error.get("errorCode") == "need-credentials":
            return False
    # Retry all other exceptions (network errors, timeouts, 5xx errors, etc.)
    return isinstance(exception, (httpx.HTTPError, A2AClientError))
