"""Tests for MCP error handling utilities: retry classification, status/body
extraction (including nested ExceptionGroups from anyio task groups), gateway
response logging, and user-facing message formatting."""

from unittest.mock import MagicMock

import httpx
import pytest

from ringier_a2a_sdk.utils.mcp_errors import (
    format_mcp_error,
    get_mcp_error_body,
    get_mcp_http_status_error,
    is_retryable_mcp_error,
    log_mcp_gateway_error,
)


def _http_error(status: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gateway.example/mcp")
    response = httpx.Response(status, request=request, text=text)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


def _nested_group(leaf: BaseException, depth: int = 2) -> BaseException:
    """Wrap `leaf` in `depth` levels of ExceptionGroup — the shape anyio's nested
    task groups actually produce for the MCP streamable-HTTP client, and the
    exact case the pre-fix single-level unwrap missed."""
    exc: BaseException = leaf
    for _ in range(depth):
        exc = ExceptionGroup("nested", [exc])
    return exc


class TestIsRetryableMcpError:
    def test_5xx_direct(self):
        assert is_retryable_mcp_error(_http_error(503)) is True
        assert is_retryable_mcp_error(_http_error(400)) is False

    def test_timeout_direct(self):
        assert is_retryable_mcp_error(httpx.ConnectTimeout("boom")) is True

    def test_nested_exception_group(self):
        assert is_retryable_mcp_error(_nested_group(_http_error(503))) is True

    def test_nested_non_retryable_status(self):
        assert is_retryable_mcp_error(_nested_group(_http_error(400))) is False

    def test_base_exception_group(self):
        # Mixed-cancellation groups surface as BaseExceptionGroup, not ExceptionGroup.
        err = BaseExceptionGroup("mixed", [_http_error(502)])
        assert is_retryable_mcp_error(err) is True

    def test_unrelated_error_is_not_retryable(self):
        assert is_retryable_mcp_error(RuntimeError("boom")) is False


class TestGetMcpHttpStatusError:
    def test_finds_nested_status_error(self):
        original = _http_error(400)
        assert get_mcp_http_status_error(_nested_group(original)) is original

    def test_none_when_absent(self):
        assert get_mcp_http_status_error(_nested_group(ConnectionError("boom"))) is None

    def test_direct(self):
        original = _http_error(400)
        assert get_mcp_http_status_error(original) is original


class TestGetMcpErrorBody:
    def test_reads_buffered_body(self):
        err = _http_error(400, text='{"message": "nope"}')
        assert get_mcp_error_body(err) == '{"message": "nope"}'

    def test_truncates(self):
        err = _http_error(400, text="x" * 10)
        assert get_mcp_error_body(err, max_len=3) == "xxx"

    def test_unread_streaming_response_is_reported_not_raised(self):
        request = httpx.Request("POST", "https://gateway.example/mcp")
        response = httpx.Response(400, request=request, stream=httpx.SyncByteStream())
        err = httpx.HTTPStatusError("boom", request=request, response=response)
        assert "not captured" in get_mcp_error_body(err)


class TestLogMcpGatewayError:
    def test_noop_when_no_status_error(self):
        logger = MagicMock()
        log_mcp_gateway_error(logger, ConnectionError("boom"))
        logger.error.assert_not_called()

    def test_logs_body_for_non_auth_status(self):
        logger = MagicMock()
        log_mcp_gateway_error(logger, _http_error(400, text='{"message": "disabled"}'))
        logger.error.assert_called_once()
        message = logger.error.call_args[0][0]
        assert "disabled" in message
        assert "400" in message

    @pytest.mark.parametrize("status", [401, 403])
    def test_redacts_body_for_auth_status(self, status):
        logger = MagicMock()
        log_mcp_gateway_error(logger, _http_error(status, text='{"message": "token abc123 invalid"}'))
        message = logger.error.call_args[0][0]
        assert "abc123" not in message
        assert "redacted" in message

    def test_context_prefix_included_verbatim(self):
        logger = MagicMock()
        log_mcp_gateway_error(logger, _http_error(400), context="for my-agent ")
        assert "for my-agent (" in logger.error.call_args[0][0]

    def test_finds_status_error_nested_in_nested_group(self):
        # The exact regression this helper exists to fix: a body-log line that
        # silently never fired for the streamable-HTTP client's real nesting.
        logger = MagicMock()
        log_mcp_gateway_error(logger, _nested_group(_http_error(400, text="oops")))
        logger.error.assert_called_once()


class TestFormatMcpError:
    def test_direct_401_gets_specific_message(self):
        assert "Authentication failed" in format_mcp_error(_http_error(401))

    def test_direct_403_gets_specific_message(self):
        assert "Access denied" in format_mcp_error(_http_error(403))

    def test_nested_group_message(self):
        assert "temporarily unavailable" in format_mcp_error(_nested_group(_http_error(503)))

    def test_fallback_for_unrelated_error(self):
        msg = format_mcp_error(RuntimeError("weird"))
        assert "RuntimeError" in msg
