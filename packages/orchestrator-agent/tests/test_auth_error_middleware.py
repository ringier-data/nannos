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


# The structured MCP error a foundry/gateway tool raises when secondary auth is needed.
_NEED_CREDS_JSON = (
    '{"errorCode":"need-credentials",'
    '"authorizeUrl":"https://gatana.ai/api/v1/mcp-servers/oauth/gt_XUsYCrdSw0/begin",'
    '"message":"This tool requires secondary authorization. You must tell the end-user '
    'to please go to the authorizeUrl. After this is done, you can retry the tool call and it will work."}'
)
# How ToolRetryMiddleware (on_failure="continue") surfaces that exception to the
# OUTER auth middleware once retry_on returns False — the JSON is embedded, not the
# whole payload. This is the shape that previously defeated detection.
_RETRY_WRAPPED = (
    f"Tool 'foundry-rms_get-ontology-rid' failed after 1 attempt with ToolException: {_NEED_CREDS_JSON}. "
    "Please try again."
)


@pytest.mark.asyncio
async def test_detect_auth_error_retry_wrapped_envelope(middleware):
    """need-credentials embedded in ToolRetryMiddleware's envelope is detected.

    Regression: the auth middleware sits OUTER to ToolRetryMiddleware, so it
    receives the wrapped "Tool 'X' failed after N attempts with ToolException: {...}"
    string rather than the raw JSON. Detection must still find the marker and
    extract the authorize URL.
    """
    result = middleware._detect_auth_error(_RETRY_WRAPPED)
    assert result is not None
    assert result["error_code"] == "need-credentials"
    assert result["auth_url"] == "https://gatana.ai/api/v1/mcp-servers/oauth/gt_XUsYCrdSw0/begin"
    assert "secondary authorization" in result["auth_message"]


@pytest.mark.asyncio
async def test_detect_auth_error_list_content_blocks(middleware):
    """Provider content blocks (list form, e.g. Gemini/Anthropic) are normalised.

    Regression: detection previously coerced non-str content to "" and silently
    missed the error.
    """
    content = [{"type": "text", "text": _RETRY_WRAPPED}]
    result = middleware._detect_auth_error(content)
    assert result is not None
    assert result["error_code"] == "need-credentials"
    assert result["auth_url"].endswith("/gt_XUsYCrdSw0/begin")


@pytest.mark.asyncio
async def test_detect_auth_error_secondary_authorization_text(middleware):
    """The 'secondary authorization' / 'need-credentials' wording is matched as text."""
    assert middleware._detect_auth_error("This tool requires secondary authorization.") is not None
    assert middleware._detect_auth_error("errorCode need-credentials returned") is not None


@pytest.mark.asyncio
async def test_retry_wrapped_tool_message_triggers_interrupt(middleware):
    """A tool result carrying the retry-wrapped need-credentials error interrupts.

    End-to-end at the middleware boundary: when the (inner) ToolRetryMiddleware
    has already converted the ToolException into an error ToolMessage, the outer
    auth middleware must still fire interrupt() rather than passing the message
    through to the LLM (which would only relay the URL as prose).
    """
    request = _make_request("foundry-rms_get-ontology-rid")
    result_msg = ToolMessage(content=_RETRY_WRAPPED, tool_call_id="tc-5", status="error")
    handler = AsyncMock(return_value=result_msg)

    with pytest.raises(Exception):
        # interrupt() raises GraphInterrupt out of the (non-graph) test context.
        await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)
