"""Tests that sub-agent dispatch errors are classified.

DynamicToolDispatchMiddleware is the outermost middleware and short-circuits
the "task" tool. Error ToolMessages from sub-agent dispatch bypass
ErrorClassificationMiddleware entirely. These tests verify that
_classify_error_message is applied so the LLM sees [ERROR_TYPE: ...] prefixes.
"""

from langchain_core.messages import ToolMessage

from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from app.middleware.error_classification_middleware import classify_error


class TestClassifyError:
    """Tests for the standalone classify_error function."""

    def test_system_error_for_attribute_error(self):
        content = "Error from subagent 'test-agent': 'dict' object has no attribute 'name'"
        assert classify_error(content) == "system_error"

    def test_system_error_for_runtime_error(self):
        content = "Error executing subagent 'my-agent': RuntimeError: unexpected state"
        assert classify_error(content) == "system_error"

    def test_system_error_for_traceback(self):
        content = "Traceback (most recent call last):\n  File ...\nKeyError: 'missing_key'"
        assert classify_error(content) == "system_error"

    def test_transient_for_timeout(self):
        content = "Error from subagent 'slow-agent': Connection timed out"
        assert classify_error(content) == "transient"

    def test_auth_for_401(self):
        content = "Error from subagent 'secure-agent': 401 Unauthorized"
        assert classify_error(content) == "auth"

    def test_capability_gap_for_tool_not_found(self):
        content = "Error: tool not found: search_database"
        assert classify_error(content) == "capability_gap"

    def test_user_fixable_for_missing_field(self):
        content = "Error: missing required field: email"
        assert classify_error(content) == "user_fixable"

    def test_none_for_normal_content(self):
        content = "All operations completed successfully"
        assert classify_error(content) is None

    def test_none_for_empty(self):
        assert classify_error("") is None


class TestClassifyErrorMessage:
    """Tests for DynamicToolDispatchMiddleware._classify_error_message."""

    def test_system_error_tagged(self):
        msg = ToolMessage(
            content="Error from subagent 'test-agent': 'dict' object has no attribute 'name'",
            name="task",
            tool_call_id="tc-1",
            status="error",
        )
        result = DynamicToolDispatchMiddleware._classify_error_message(msg)

        assert result.content.startswith("[ERROR_TYPE: system_error]")
        assert "'dict' object has no attribute 'name'" in result.content
        assert result.additional_kwargs["error_classification"] == "system_error"

    def test_transient_error_tagged(self):
        msg = ToolMessage(
            content="Error executing subagent 'slow-agent': Connection timed out after 30s",
            name="task",
            tool_call_id="tc-2",
            status="error",
        )
        result = DynamicToolDispatchMiddleware._classify_error_message(msg)

        assert result.content.startswith("[ERROR_TYPE: transient]")
        assert result.additional_kwargs["error_classification"] == "transient"

    def test_no_runnable_tagged(self):
        msg = ToolMessage(
            content="Error: Subagent 'broken-agent' has no runnable",
            name="task",
            tool_call_id="tc-3",
            status="error",
        )
        result = DynamicToolDispatchMiddleware._classify_error_message(msg)

        assert result.content.startswith("[ERROR_TYPE: system_error]")
        assert result.additional_kwargs["error_classification"] == "system_error"

    def test_no_response_tagged(self):
        msg = ToolMessage(
            content="No response received from subagent 'silent-agent'",
            name="task",
            tool_call_id="tc-4",
            status="error",
        )
        result = DynamicToolDispatchMiddleware._classify_error_message(msg)

        # status="error" but no specific pattern → defaults to system_error
        assert result.content.startswith("[ERROR_TYPE: system_error]")
        assert result.additional_kwargs["error_classification"] == "system_error"

    def test_error_status_always_classified(self):
        """Even with benign content, status='error' defaults to system_error."""
        msg = ToolMessage(
            content="Operation completed successfully",
            name="task",
            tool_call_id="tc-5",
            status="error",
        )
        result = DynamicToolDispatchMiddleware._classify_error_message(msg)

        # status="error" → always classified (defaults to system_error)
        assert result.content.startswith("[ERROR_TYPE: system_error]")
        assert result.additional_kwargs["error_classification"] == "system_error"
