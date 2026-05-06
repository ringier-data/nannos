"""Tests for interrupt() propagation through the middleware stack.

Verifies that GraphBubbleUp (raised by interrupt()) escapes ToolRetryMiddleware
and is not caught/converted to an error ToolMessage.
"""

import pytest
from langgraph.errors import GraphBubbleUp

from app.handlers.utils import should_retry


class TestShouldRetryGraphBubbleUp:
    """Verify should_retry re-raises GraphBubbleUp instead of returning bool."""

    def test_graph_bubble_up_is_reraised(self):
        """GraphBubbleUp must propagate — not be swallowed as a non-retryable error."""
        exc = GraphBubbleUp({"type": "bug_report", "reason": "test"})
        with pytest.raises(GraphBubbleUp):
            should_retry(exc)

    def test_other_exceptions_return_false(self):
        """Non-retryable exceptions return False (don't raise)."""
        assert should_retry(ValueError("bad value")) is False
        assert should_retry(RuntimeError("crash")) is False

    def test_tool_exception_returns_false(self):
        """ToolException is not retried."""
        from langchain_core.tools import ToolException

        assert should_retry(ToolException("validation failed")) is False
