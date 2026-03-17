"""Tests for domain error handler service."""

import json

import httpx
from agent_common.models.exceptions import A2AClientError
from langchain_core.tools import ToolException

from app.handlers.utils import handle_auth_error, parse_tool_exception, should_retry


class TestErrorHandlerParseToolException:
    """Tests for parse_tool_exception method."""

    def test_parse_valid_json_error(self):
        """Test parsing ToolException containing valid JSON."""
        error_data = {
            "errorCode": "need-credentials",
            "message": "Authentication required",
            "authorizeUrl": "https://auth.example.com",
        }
        exception = ToolException(json.dumps(error_data))

        result = parse_tool_exception(exception)

        assert result["errorCode"] == "need-credentials"
        assert result["message"] == "Authentication required"
        assert result["authorizeUrl"] == "https://auth.example.com"

    def test_parse_invalid_json_error(self):
        """Test parsing ToolException with non-JSON message."""
        exception = ToolException("Simple error message")

        result = parse_tool_exception(exception)

        assert result["errorCode"] == "unknown-error"
        assert result["message"] == "Simple error message"

    def test_parse_empty_json_object(self):
        """Test parsing ToolException with empty JSON object."""
        exception = ToolException(json.dumps({}))

        result = parse_tool_exception(exception)

        assert isinstance(result, dict)
        assert "errorCode" not in result or result.get("errorCode") is None

    def test_parse_nested_json_structure(self):
        """Test parsing ToolException with nested JSON."""
        error_data = {"errorCode": "validation-error", "details": {"field": "username", "constraint": "required"}}
        exception = ToolException(json.dumps(error_data))

        result = parse_tool_exception(exception)

        assert result["errorCode"] == "validation-error"
        assert "details" in result
        assert result["details"]["field"] == "username"


class TestErrorHandlerHandleAuthError:
    """Tests for handle_auth_error method."""

    def test_handle_tool_exception_with_credentials_error(self):
        """Test handling ToolException with need-credentials error code."""
        error_data = {
            "errorCode": "need-credentials",
            "authorizeUrl": "https://auth.example.com",
            "message": "Please authenticate",
        }
        exception = ToolException(json.dumps(error_data))

        result = handle_auth_error(exception)
        parsed = json.loads(result)

        assert parsed["errorCode"] == "need-credentials"
        assert parsed["authorizeUrl"] == "https://auth.example.com"

    def test_handle_http_401_error(self):
        """Test handling HTTP 401 Unauthorized error."""
        response = httpx.Response(
            status_code=401, content=b"Unauthorized: Invalid token", request=httpx.Request("GET", "http://example.com")
        )
        exception = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)

        result = handle_auth_error(exception)

        assert "Unauthorized" in result or "Invalid token" in result

    def test_handle_generic_exception(self):
        """Test handling generic exception (non-auth)."""
        exception = ValueError("Some random error")

        result = handle_auth_error(exception)

        assert result == "An unexpected error occurred."

    def test_handle_tool_exception_without_credentials_error(self):
        """Test handling ToolException without credentials error code."""
        error_data = {"errorCode": "rate-limit", "message": "Too many requests"}
        exception = ToolException(json.dumps(error_data))

        result = handle_auth_error(exception)

        # Should return generic message for non-auth errors
        assert result == "An unexpected error occurred."


class TestErrorHandlerShouldRetry:
    """Tests for should_retry method."""

    def test_should_not_retry_401_unauthorized(self):
        """Test that 401 errors are not retried."""
        response = httpx.Response(status_code=401, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)

        assert not should_retry(exception)

    def test_should_retry_500_server_error(self):
        """Test that 500 errors are retried."""
        response = httpx.Response(status_code=500, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Server Error", request=response.request, response=response)

        assert should_retry(exception)

    def test_should_retry_503_service_unavailable(self):
        """Test that 503 errors are retried."""
        response = httpx.Response(status_code=503, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Service Unavailable", request=response.request, response=response)

        assert should_retry(exception)

    def test_should_not_retry_credentials_error(self):
        """Test that credential errors are not retried."""
        error_data = {"errorCode": "need-credentials"}
        exception = ToolException(json.dumps(error_data))

        assert not should_retry(exception)

    def test_should_retry_a2a_client_error(self):
        """Test that A2AClientError is retried."""
        exception = A2AClientError("Connection timeout")

        assert should_retry(exception)

    def test_should_retry_network_error(self):
        """Test that network errors are retried."""
        exception = httpx.ConnectError("Failed to connect")

        assert should_retry(exception)

    def test_should_not_retry_generic_exception(self):
        """Test that generic exceptions are not retried."""
        exception = ValueError("Invalid input")

        assert not should_retry(exception)

    def test_should_retry_timeout_error(self):
        """Test that timeout errors are retried."""
        exception = httpx.TimeoutException("Request timed out")

        assert should_retry(exception)
