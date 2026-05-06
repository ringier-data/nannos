"""Tests for ErrorClassificationMiddleware (Phase 2)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage

from app.middleware.error_classification_middleware import ErrorClassificationMiddleware


@pytest.fixture
def middleware():
    return ErrorClassificationMiddleware()


def _make_request(tool_name: str = "some_tool", args: dict | None = None):
    req = MagicMock()
    req.tool_call = {"name": tool_name, "args": args or {}}
    return req


# ---------------------------------------------------------------------------
# Pass-through (non-error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_error_passes_through(middleware):
    """Normal tool responses are not classified."""
    request = _make_request()
    msg = ToolMessage(content="Success: all good", tool_call_id="tc-1")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)

    assert result is msg
    assert not result.additional_kwargs.get("error_classification")


@pytest.mark.asyncio
async def test_empty_content_passes_through(middleware):
    request = _make_request()
    msg = ToolMessage(content="", tool_call_id="tc-1")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert not result.additional_kwargs.get("error_classification")


# ---------------------------------------------------------------------------
# Transient errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_timeout(middleware):
    request = _make_request()
    msg = ToolMessage(content="Error: Connection timed out after 30s", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "transient"


@pytest.mark.asyncio
async def test_classify_rate_limit(middleware):
    request = _make_request()
    msg = ToolMessage(content="Error: rate limit exceeded, retry after 60s", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "transient"


@pytest.mark.asyncio
async def test_classify_503(middleware):
    request = _make_request()
    msg = ToolMessage(content="503 Service Unavailable", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "transient"


@pytest.mark.asyncio
async def test_classify_json_429(middleware):
    """JSON response with status code 429 classified as transient."""
    request = _make_request()
    content = json.dumps({"statusCode": 429, "message": "Too many requests"})
    msg = ToolMessage(content=content, tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "transient"


@pytest.mark.asyncio
async def test_classify_json_500(middleware):
    request = _make_request()
    content = json.dumps({"status_code": 500, "error": "Internal Server Error"})
    msg = ToolMessage(content=content, tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "transient"


# ---------------------------------------------------------------------------
# Auth errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_401(middleware):
    request = _make_request()
    msg = ToolMessage(content="Error: 401 Unauthorized", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "auth"


@pytest.mark.asyncio
async def test_classify_403_forbidden(middleware):
    request = _make_request()
    msg = ToolMessage(content="HTTP 403 Forbidden", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "auth"


@pytest.mark.asyncio
async def test_classify_json_auth(middleware):
    request = _make_request()
    content = json.dumps({"statusCode": 401, "error": "Unauthorized"})
    msg = ToolMessage(content=content, tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "auth"


# ---------------------------------------------------------------------------
# Capability gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_tool_not_found(middleware):
    request = _make_request()
    msg = ToolMessage(content="Error: tool not found: search_jira", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "capability_gap"


@pytest.mark.asyncio
async def test_classify_cannot_perform(middleware):
    request = _make_request()
    msg = ToolMessage(
        content="I don't have the ability to access the database directly.",
        tool_call_id="tc-1",
        status="error",
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "capability_gap"


# ---------------------------------------------------------------------------
# User-fixable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_missing_field(middleware):
    request = _make_request()
    msg = ToolMessage(content="Error: missing required field: email", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "user_fixable"


@pytest.mark.asyncio
async def test_classify_invalid_input(middleware):
    request = _make_request()
    msg = ToolMessage(content="Error: invalid input format for date", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "user_fixable"


@pytest.mark.asyncio
async def test_classify_400_bad_request(middleware):
    request = _make_request()
    msg = ToolMessage(content="400 Bad Request: JSON body expected", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "user_fixable"


# ---------------------------------------------------------------------------
# System error (fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_unrecognized_error_as_system_error(middleware):
    """Errors that don't match known patterns fall to system_error."""
    request = _make_request()
    msg = ToolMessage(
        content="Traceback (most recent call last):\n  ...\nRuntimeError: kaboom", tool_call_id="tc-1", status="error"
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "system_error"


@pytest.mark.asyncio
async def test_classify_generic_failure(middleware):
    request = _make_request()
    msg = ToolMessage(
        content="Error: An unexpected failure occurred in the pipeline.", tool_call_id="tc-1", status="error"
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    assert result.additional_kwargs["error_classification"] == "system_error"


# ---------------------------------------------------------------------------
# Command results pass through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_result_passes_through(middleware):
    """If the handler returns a Command (not a ToolMessage), pass it through."""
    from langgraph.types import Command

    request = _make_request()
    cmd = Command(resume={"foo": "bar"})
    handler = AsyncMock(return_value=cmd)

    result = await middleware.awrap_tool_call(request, handler)
    assert result is cmd


# ---------------------------------------------------------------------------
# Classification priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_takes_priority_over_transient(middleware):
    """Auth patterns should be checked before transient (401 is both auth & could be transient)."""
    request = _make_request()
    msg = ToolMessage(content="HTTP Error 401: rate limit on auth endpoint", tool_call_id="tc-1", status="error")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)
    # 401 matches auth first
    assert result.additional_kwargs["error_classification"] == "auth"
