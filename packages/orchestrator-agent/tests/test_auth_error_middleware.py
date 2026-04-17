"""Tests for AuthErrorDetectionMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage

from app.middleware.auth_error_middleware import AuthErrorDetectionMiddleware


@pytest.fixture
def middleware():
    return AuthErrorDetectionMiddleware()


def _make_request(tool_name: str, args: dict | None = None):
    """Create a minimal ToolCallRequest-like object."""
    req = MagicMock()
    req.tool_call = {"name": tool_name, "args": args or {}}
    return req


@pytest.mark.asyncio
async def test_final_response_schema_with_401_not_intercepted(middleware):
    """FinalResponseSchema mentioning a 401 should NOT trigger an interrupt.

    The LLM is *reporting* an upstream error to the user; the middleware must
    not treat that report as a fresh authentication requirement.
    """
    request = _make_request(
        "FinalResponseSchema",
        {"message": "I encountered a 401 Unauthorized error", "task_state": "input-required"},
    )
    result_msg = ToolMessage(content="FinalResponseSchema executed", tool_call_id="tc-1")
    handler = AsyncMock(return_value=result_msg)

    result = await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)
    assert result is result_msg  # Passed through without interrupt


@pytest.mark.asyncio
async def test_subagent_response_schema_with_401_not_intercepted(middleware):
    """SubAgentResponseSchema mentioning a 401 should NOT trigger an interrupt."""
    request = _make_request(
        "SubAgentResponseSchema",
        {"message": "authorization error 401 unauthorized"},
    )
    result_msg = ToolMessage(content="SubAgentResponseSchema executed", tool_call_id="tc-2")
    handler = AsyncMock(return_value=result_msg)

    result = await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)
    assert result is result_msg


@pytest.mark.asyncio
async def test_regular_tool_with_401_triggers_interrupt(middleware):
    """A regular tool returning a 401 error should trigger an interrupt."""
    request = _make_request("some_api_tool")
    result_msg = ToolMessage(
        content="Error: HTTP Error 401: Client error '401 Unauthorized' for url 'http://example.com'",
        tool_call_id="tc-3",
    )
    handler = AsyncMock(return_value=result_msg)

    with pytest.raises(Exception) as exc_info:
        # interrupt() raises GraphInterrupt which propagates out
        await middleware.awrap_tool_call(request, handler)

    # The interrupt should have been called — LangGraph raises GraphInterrupt
    # (or a similar exception) when interrupt() is invoked outside a graph context.
    handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_regular_tool_without_auth_error_passes_through(middleware):
    """A regular tool returning normal content should pass through."""
    request = _make_request("read_file")
    result_msg = ToolMessage(content="File contents here", tool_call_id="tc-4")
    handler = AsyncMock(return_value=result_msg)

    result = await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)
    assert result is result_msg


@pytest.mark.asyncio
async def test_detect_auth_error_json_format(middleware):
    """JSON auth error format is detected."""
    content = (
        '{"errorCode": "need-credentials", "authorizeUrl": "https://auth.example.com", "message": "Auth required"}'
    )
    result = middleware._detect_auth_error(content)
    assert result is not None
    assert result["error_code"] == "need-credentials"
    assert result["auth_url"] == "https://auth.example.com"


@pytest.mark.asyncio
async def test_detect_auth_error_text_patterns(middleware):
    """Text-based auth error patterns are detected."""
    for pattern in ["authentication required", "401 unauthorized", "access denied"]:
        result = middleware._detect_auth_error(f"Error: {pattern}")
        assert result is not None, f"Failed to detect pattern: {pattern}"


@pytest.mark.asyncio
async def test_detect_auth_error_normal_content(middleware):
    """Normal content is not flagged as auth error."""
    result = middleware._detect_auth_error("The file was read successfully")
    assert result is None
