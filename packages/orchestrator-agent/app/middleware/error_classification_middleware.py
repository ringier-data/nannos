"""Error Classification Middleware for deterministic error categorization.

Inspects tool responses for error patterns and tags them with a classification
in ``metadata["error_classification"]``. Does NOT take action — the LLM decides
whether to recover or escalate based on the classification.

Classification categories:
- ``transient``: timeouts, rate limits, 5xx — ToolRetryMiddleware handles these
- ``auth``: already handled upstream by AuthErrorDetectionMiddleware
- ``capability_gap``: sub-agent can't do this, tool not found
- ``user_fixable``: bad input, missing required fields
- ``system_error``: unexpected crash, invalid response

Middleware Stack Position:
    Between AuthErrorDetectionMiddleware (inner) and ToolRetryMiddleware (inner).
    Sees errors AFTER auth is handled but BEFORE retry.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

logger = logging.getLogger(__name__)

# Conservative patterns — start narrow, expand based on actual bug reports
_TRANSIENT_PATTERNS = re.compile(
    r"timeout|timed?\s*out|rate.limit|429|503|502|504|5\d{2}\s+(?:internal|bad gateway|service unavailable)",
    re.IGNORECASE,
)

_AUTH_PATTERNS = re.compile(
    r"401|403|unauthorized|forbidden|authentication.required|need.credentials|auth_required",
    re.IGNORECASE,
)

_CAPABILITY_GAP_PATTERNS = re.compile(
    r"(?:tool|function|capability|action)\s+(?:not\s+found|not\s+available|unknown|unsupported)"
    r"|cannot\s+(?:perform|execute|handle)\s+(?:this|that)"
    r"|not\s+(?:able|capable)\s+to"
    r"|i\s+(?:don't|do\s+not)\s+have\s+(?:the\s+)?(?:ability|capability|tool)",
    re.IGNORECASE,
)

_USER_FIXABLE_PATTERNS = re.compile(
    r"(?:missing|required)\s+(?:field|parameter|argument|input)"
    r"|invalid\s+(?:input|format|value|argument)"
    r"|(?:please\s+)?provide\s+(?:a|the|your)"
    r"|400\s+bad\s+request",
    re.IGNORECASE,
)


class ErrorClassificationMiddleware(AgentMiddleware[AgentState, ContextT]):
    """Middleware that classifies tool errors by pattern matching.

    Sets ``metadata["error_classification"]`` on error ToolMessages.
    Classification only — does not take action or modify the response content.
    """

    def __init__(self) -> None:
        super().__init__()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)

        if not isinstance(result, ToolMessage):
            return result

        # Only classify error responses
        status = getattr(result, "status", None)
        content = result.content if isinstance(result.content, str) else ""

        if status != "error" and not self._looks_like_error(content):
            return result

        classification = self._classify(content)
        if classification:
            # Tag the message metadata with the classification
            additional_kwargs = getattr(result, "additional_kwargs", {}) or {}
            additional_kwargs["error_classification"] = classification
            result.additional_kwargs = additional_kwargs

            # Prepend classification to content so the model can see and act on it.
            # additional_kwargs are stripped by the model serialization layer,
            # so we must surface the classification in the visible content.
            result.content = f"[ERROR_TYPE: {classification}]\n{content}"

            tool_name = request.tool_call.get("name", "unknown")
            logger.info(f"[ERROR_CLASSIFICATION] {tool_name}: {classification}")

        return result

    def _looks_like_error(self, content: str) -> bool:
        """Heuristic: does this content look like an error response?"""
        if not content:
            return False
        lower = content.lower()
        return any(
            kw in lower for kw in ("error", "exception", "failed", "failure", "traceback", "unauthorized", "forbidden")
        )

    def _classify(self, content: str) -> str | None:
        """Classify an error response into a category.

        Order matters — check more specific patterns first.
        """
        if not content:
            return None

        # Check for structured JSON errors first
        classification = self._classify_json(content)
        if classification:
            return classification

        # Pattern-based classification
        if _AUTH_PATTERNS.search(content):
            return "auth"
        if _TRANSIENT_PATTERNS.search(content):
            return "transient"
        if _USER_FIXABLE_PATTERNS.search(content):
            return "user_fixable"
        if _CAPABILITY_GAP_PATTERNS.search(content):
            return "capability_gap"

        # Default: if it looks like an error but doesn't match known patterns,
        # classify as system_error (conservative)
        return "system_error"

    def _classify_json(self, content: str) -> str | None:
        """Try to classify from structured JSON error responses."""
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                return None

            error_code = data.get("errorCode", data.get("error_code", ""))
            status_code = data.get("statusCode", data.get("status_code", data.get("status", 0)))

            if isinstance(status_code, int):
                if status_code == 401 or status_code == 403:
                    return "auth"
                if status_code == 429 or 500 <= status_code <= 599:
                    return "transient"
                if status_code == 400:
                    return "user_fixable"

            if error_code in ("need-credentials", "auth_required", "unauthorized"):
                return "auth"
            if error_code in ("rate_limit", "timeout", "service_unavailable"):
                return "transient"

            return None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
