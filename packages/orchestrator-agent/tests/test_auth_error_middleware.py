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
async def test_regular_tool_with_free_text_401_passes_through(middleware):
    """A regular tool returning a plain-text 401 (no structured payload) passes through.

    Detection is deliberately structured-only (see module docstring): free-text
    auth phrasing is ambiguous against arbitrary tool payload data, so it is left
    for the LLM to interpret rather than deterministically intercepted here.
    """
    request = _make_request("some_api_tool")
    result_msg = ToolMessage(
        content="Error: HTTP Error 401: Client error '401 Unauthorized' for url 'http://example.com'",
        tool_call_id="tc-3",
    )
    handler = AsyncMock(return_value=result_msg)

    result = await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)
    assert result is result_msg  # Passed through without interrupt


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
async def test_detect_auth_error_free_text_alone_not_detected(middleware):
    """Free-text auth phrasing alone (no structured payload) is NOT detected.

    Detection is deliberately structured-only. These phrases are ambiguous
    against arbitrary tool payload data (see
    test_successful_result_with_auth_flavored_text_not_intercepted), so they're
    intentionally left for the LLM to interpret rather than pattern-matched here.
    """
    for pattern in ["authentication required", "401 unauthorized", "access denied"]:
        result = middleware._detect_auth_error(f"Error: {pattern}")
        assert result is None, f"Free-text pattern should not be detected: {pattern}"


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
async def test_detect_auth_error_need_credentials_marker_text(middleware):
    """The `"errorCode":"need-credentials"` field is matched even without a full,
    parseable JSON envelope around it (e.g. wrapped by ToolRetryMiddleware).

    Plain "secondary authorization" wording alone, without the field, is NOT
    matched — that's free text, left for the LLM (see
    test_detect_auth_error_free_text_alone_not_detected).
    """
    assert middleware._detect_auth_error("This tool requires secondary authorization.") is None
    assert middleware._detect_auth_error('prefix "errorCode":"need-credentials" suffix') is not None


@pytest.mark.asyncio
async def test_detect_auth_error_bare_need_credentials_word_not_detected(middleware):
    """The bare word "need-credentials" outside of the `errorCode` field is NOT detected.

    Regression: step 2 used to be a bare substring check for "need-credentials"
    anywhere in the content, so ordinary business data merely mentioning that
    word in prose (e.g. a support ticket referencing an unrelated API's error)
    would false-positive an interrupt — the same class of bug issue #130 was
    filed for, just triggered by a different token.
    """
    content = '{"ticket_notes": "customer support case mentions a need-credentials error from a partner API"}'
    assert middleware._detect_auth_error(content) is None


@pytest.mark.asyncio
async def test_successful_result_with_auth_flavored_text_not_intercepted(middleware):
    """A successful tool result whose payload happens to contain auth-flavored
    free text (e.g. a CRM note saying a customer's "access denied" a request)
    must NOT trigger an interrupt.

    Regression: `_detect_auth_error` previously had a loose free-text fallback
    that ran on every successful ToolMessage unconditionally, so business data
    merely containing one of the hardcoded phrases falsely triggered
    auth-required. Detection is now structured-only (see module docstring),
    so this is no longer possible regardless of status.
    """
    request = _make_request("eval")
    content = (
        '{"Note": [{"Body": "Kunde hat schriftlich bestätigt: access denied für weitere Angebote."}]}'
    )
    result_msg = ToolMessage(content=content, tool_call_id="tc-6")  # status defaults to "success"
    handler = AsyncMock(return_value=result_msg)

    result = await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)
    assert result is result_msg  # Passed through without interrupt


@pytest.mark.asyncio
async def test_gatana_gateway_need_credentials_triggers_interrupt_regardless_of_status(middleware):
    """The real-world gatana MCP gateway response format reliably interrupts.

    This is the actual shape an MCP tool call returns when the gatana gateway
    requires secondary authorization — the structured JSON, verbatim, as the
    tool's content. It must be detected even when the ToolMessage carries the
    default status="success" (some MCP adapters don't mark these as errors),
    since this is the one deterministic signal this middleware still acts on.
    """
    request = _make_request("github_search_issues")
    content = (
        '{"errorCode":"need-credentials",'
        '"authorizeUrl":"https://gatana.ai/api/v1/mcp-servers/oauth/gt_ADsagJ9hdU/begin",'
        '"message":"This tool requires secondary authorization. You must tell the end-user '
        'to please go to the authorizeUrl. After this is done, you can retry the tool call and it will work."}'
    )
    result_msg = ToolMessage(content=content, tool_call_id="tc-7")  # status defaults to "success"
    handler = AsyncMock(return_value=result_msg)

    with pytest.raises(Exception):
        # interrupt() raises GraphInterrupt out of the (non-graph) test context.
        await middleware.awrap_tool_call(request, handler)

    handler.assert_awaited_once_with(request)


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


# ---------------------------------------------------------------------------
# Resuming a paused auth interrupt
#
# On a resume, `interrupt()` RETURNS instead of raising, and what happens next
# is what decides whether a second attempt gets a card or a paragraph.
# ---------------------------------------------------------------------------


def _resumable_request(tool_name: str = "github_get_me"):
    req = MagicMock()
    req.tool_call = {"name": tool_name, "args": {}, "id": "tc-resume"}
    return req


def test_resume_decision_reads_an_explicit_client_answer(middleware):
    """A client that negotiated the extension leaves nothing to interpret."""
    approved = middleware._resume_decision({"authorization": {"decision": "approved"}})
    declined = middleware._resume_decision(
        {"authorization": {"decision": "declined", "message": "scopes too wide"}}
    )

    assert approved == ("approved", "")
    assert declined == ("declined", "scopes too wide")


def test_resume_decision_does_not_guess_at_free_text(middleware):
    """Typed words are reported as unclear, verbatim — the model judges them."""
    assert middleware._resume_decision("damn I missclicked, try again") == (
        "unclear",
        "damn I missclicked, try again",
    )
    assert middleware._resume_decision(None) == ("unclear", "")


@pytest.mark.asyncio
async def test_approved_resume_retries_the_tool(middleware):
    """An approval retries the call, which is what brings the prompt back.

    If the credential still is not there, the retry's result meets the same
    detection and interrupts again — a second card, rather than the agent
    relaying the URL as prose.
    """
    request = _resumable_request()
    retried = ToolMessage(content='{"login":"aartaria"}', tool_call_id="tc-resume")
    handler = AsyncMock(return_value=retried)

    result = await middleware._after_auth_interrupt(
        {"authorization": {"decision": "approved"}}, request, handler, "github_get_me"
    )

    assert result is retried
    handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_declined_resume_tells_the_model_to_stop_asking(middleware):
    """A refusal must not come back as the same link a second time."""
    request = _resumable_request()
    handler = AsyncMock()

    result = await middleware._after_auth_interrupt(
        {"authorization": {"decision": "declined", "message": "the permissions are too wide"}},
        request,
        handler,
        "github_get_me",
    )

    handler.assert_not_awaited()
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "tc-resume"
    assert "DECLINED" in result.content
    assert "the permissions are too wide" in result.content
    assert "Do not retry" in result.content


@pytest.mark.asyncio
async def test_unclear_resume_hands_the_reply_to_the_model(middleware):
    """Free text is passed on with both options spelled out, and nothing is run.

    This is the classifier: the model node runs next anyway, and is far better
    placed than a keyword match to tell "ok, done" from "no, too wide".
    """
    request = _resumable_request()
    handler = AsyncMock()

    result = await middleware._after_auth_interrupt(
        "damn I missclicked, try again", request, handler, "github_get_me"
    )

    handler.assert_not_awaited()
    assert isinstance(result, ToolMessage)
    assert "damn I missclicked, try again" in result.content
    assert "call the tool again" in result.content
    assert "do not repeat the authorization link" in result.content


@pytest.mark.asyncio
async def test_unclear_resume_without_a_reply_still_reads_sensibly(middleware):
    request = _resumable_request()
    result = await middleware._after_auth_interrupt(None, request, AsyncMock(), "github_get_me")
    assert "(no reply)" in result.content
