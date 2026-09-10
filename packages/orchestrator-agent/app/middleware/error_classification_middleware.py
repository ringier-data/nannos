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

from agent_common.middleware.ptc_guard import RATE_LIMIT_PATTERN, is_rate_limit_error
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

logger = logging.getLogger(__name__)

# Conservative patterns — start narrow, expand based on actual bug reports.
# Quota wording is deliberately absent here: ``_RATE_LIMIT_PATTERNS`` owns it.
_TRANSIENT_PATTERNS = re.compile(
    r"timeout|timed?\s*out|503|502|504|5\d{2}\s+(?:internal|bad gateway|service unavailable)",
    re.IGNORECASE,
)

# Quota exhaustion. Checked *between* the two auth tiers below: a provider that
# spells its quota error ``403 ... rate limit exceeded`` must not classify as
# ``auth`` on the strength of the bare status code (re-authenticating cannot fix
# a quota), while a real ``401`` on a rate-limited auth endpoint stays ``auth``.
# ``too many requests`` is how the MCP gateway words its own throttling — no
# ``429``, no ``rate limit`` — and without it that error fell through to
# ``system_error``, i.e. was reported to the model as an unexpected crash. The
# pattern is shared with the PTC guard (which returns a terminal ``rate_limited``
# payload on it) so both paths agree on what a rate limit looks like.
_RATE_LIMIT_PATTERNS = RATE_LIMIT_PATTERN

# Unambiguous authentication failures: outrank everything. Status codes are
# word-bounded: a request id containing ``401`` is not an auth failure.
_AUTH_PATTERNS = re.compile(
    r"\b401\b|unauthorized|authentication.required|need.credentials|auth_required",
    re.IGNORECASE,
)

# A bare 403 / "forbidden" is authorization *unless* the same message says it is a
# quota problem — see ``_RATE_LIMIT_PATTERNS``.
_FORBIDDEN_PATTERNS = re.compile(r"\b403\b|forbidden", re.IGNORECASE)

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


def classify_error(content: str) -> str | None:
    """Classify an error message into a category.

    Standalone function so it can be used both by
    ``ErrorClassificationMiddleware`` and by ``DynamicToolDispatchMiddleware``
    (which short-circuits before the middleware chain).

    Returns one of: ``"auth"``, ``"transient"``, ``"user_fixable"``,
    ``"capability_gap"``, ``"system_error"``, or ``None``.
    """
    if not content or _is_structured_success(content):
        return None

    # Check for structured JSON errors first
    classification = _classify_json(content)
    if classification:
        return classification

    # Pattern-based classification, most specific first. A bare ``403`` only
    # counts as auth when nothing in the message says "quota".
    if _AUTH_PATTERNS.search(content):
        return "auth"
    if _RATE_LIMIT_PATTERNS.search(content):
        return "transient"
    if _FORBIDDEN_PATTERNS.search(content):
        return "auth"
    if _TRANSIENT_PATTERNS.search(content):
        return "transient"
    if _USER_FIXABLE_PATTERNS.search(content):
        return "user_fixable"
    if _CAPABILITY_GAP_PATTERNS.search(content):
        return "capability_gap"

    # Default: if it looks like an error but doesn't match known patterns,
    # classify as system_error (conservative)
    if _looks_like_error(content):
        return "system_error"

    return None


def _is_structured_success(content: str) -> bool:
    """A PTC ``eval`` reply is a ``<result …>`` block on success and an ``<error type=…>``
    block on failure. A successful result routinely *contains* the words "error",
    "required", "failed" … (tool descriptions, JSON schemas, log lines the program
    printed), so keyword heuristics must not run on it: a grep over the tool catalogue
    was being stamped ``[ERROR_TYPE: user_fixable]`` because its schemas say
    ``"required": [...]``, and the model then treated a good result as a failure.
    """
    return "<result" in content and "<error type=" not in content


def _looks_like_error(content: str) -> bool:
    """Heuristic: does this content look like an error response?"""
    if not content or _is_structured_success(content):
        return False
    lower = content.lower()
    return any(
        kw in lower for kw in ("error", "exception", "failed", "failure", "traceback", "unauthorized", "forbidden")
    )


def _classify_json(content: str) -> str | None:
    """Try to classify from structured JSON error responses."""
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None

        error_code = data.get("errorCode", data.get("error_code", ""))
        status_code = data.get("statusCode", data.get("status_code", data.get("status", 0)))

        if isinstance(status_code, int):
            if status_code == 401:
                return "auth"
            if status_code == 403:
                # Same rule as the text tier: a 403 whose wording says "quota" is
                # rate limiting, not authorization.
                return "transient" if is_rate_limit_error(content) else "auth"
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

        if status != "error" and not _looks_like_error(content):
            return result

        classification = classify_error(content)
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
