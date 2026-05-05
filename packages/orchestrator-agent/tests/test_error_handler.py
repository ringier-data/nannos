"""Tests for domain error handler service."""

import json

import httpx
from agent_common.models.exceptions import A2AClientError
from langchain_core.tools import ToolException

from app.handlers.utils import handle_tool_failure, parse_tool_exception, should_retry


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


class TestErrorHandlerHandleToolFailure:
    """Tests for handle_tool_failure method."""

    def test_handle_tool_exception_with_credentials_error(self):
        """Test handling ToolException with need-credentials error code."""
        error_data = {
            "errorCode": "need-credentials",
            "authorizeUrl": "https://auth.example.com",
            "message": "Please authenticate",
        }
        exception = ToolException(json.dumps(error_data))

        result = handle_tool_failure(exception)
        parsed = json.loads(result)

        assert parsed["errorCode"] == "need-credentials"
        assert parsed["authorizeUrl"] == "https://auth.example.com"

    def test_handle_http_401_error(self):
        """Test handling HTTP 401 Unauthorized error."""
        response = httpx.Response(
            status_code=401, content=b"Unauthorized: Invalid token", request=httpx.Request("GET", "http://example.com")
        )
        exception = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)

        result = handle_tool_failure(exception)

        assert "Unauthorized" in result or "Invalid token" in result

    def test_handle_generic_exception(self):
        """Test handling generic exception returns actual error message."""
        exception = ValueError("Some random error")

        result = handle_tool_failure(exception)

        assert "Some random error" in result

    def test_handle_tool_exception_without_credentials_error(self):
        """Test handling ToolException without credentials error returns actual message."""
        error_data = {"errorCode": "rate-limit", "message": "Too many requests"}
        exception = ToolException(json.dumps(error_data))

        result = handle_tool_failure(exception)

        # Should return actual error message so LLM can understand and adapt
        assert "rate-limit" in result or "Too many requests" in result

    def test_handle_tool_exception_with_validation_error(self):
        """Test that validation errors are passed through for the LLM to read."""
        exception = ToolException("issue_number is required when item_type is 'issue'")

        result = handle_tool_failure(exception)

        assert "issue_number is required" in result

    def test_handle_tool_exception_with_permission_error(self):
        """Test that permission errors are passed through for the LLM to read."""
        exception = ToolException(
            "Failed to find teams: Although you appear to have the correct authorization credentials, "
            "the `gatana-ai` organization has enabled OAuth App access restrictions"
        )

        result = handle_tool_failure(exception)

        assert "OAuth App access restrictions" in result


class TestErrorHandlerShouldRetry:
    """Tests for should_retry method."""

    def test_should_not_retry_401_unauthorized(self):
        """Test that 401 errors are not retried."""
        response = httpx.Response(status_code=401, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)

        assert not should_retry(exception)

    def test_should_not_retry_400_bad_request(self):
        """Test that 400 errors are not retried (deterministic client error)."""
        response = httpx.Response(status_code=400, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Bad Request", request=response.request, response=response)

        assert not should_retry(exception)

    def test_should_not_retry_403_forbidden(self):
        """Test that 403 errors are not retried (permission issue)."""
        response = httpx.Response(status_code=403, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Forbidden", request=response.request, response=response)

        assert not should_retry(exception)

    def test_should_not_retry_404_not_found(self):
        """Test that 404 errors are not retried (resource doesn't exist)."""
        response = httpx.Response(status_code=404, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Not Found", request=response.request, response=response)

        assert not should_retry(exception)

    def test_should_not_retry_422_unprocessable(self):
        """Test that 422 errors are not retried (validation failure)."""
        response = httpx.Response(status_code=422, request=httpx.Request("GET", "http://example.com"))
        exception = httpx.HTTPStatusError("Unprocessable Entity", request=response.request, response=response)

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
        """Test that credential ToolExceptions are not retried."""
        error_data = {"errorCode": "need-credentials"}
        exception = ToolException(json.dumps(error_data))

        assert not should_retry(exception)

    def test_should_not_retry_tool_exception(self):
        """Test that ToolExceptions are never retried (LLM should read the error)."""
        exception = ToolException("issue_number is required when item_type is 'issue'")

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
