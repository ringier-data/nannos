"""Unit tests for LoopDetectionMiddleware in orchestrator-agent.

Tests cover:
- Sliding window tracking of tool calls
- Tool signature computation with argument hashing
- Loop detection with configurable max_repeats and window_size
- Interrupt mechanism when loop detected
- Tool call history state management and pruning
- Pattern detection scenarios (same tool, different tools, mixed patterns)
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage

from app.middleware.loop_detection_middleware import RepeatedToolCallMiddleware


@pytest.fixture
def middleware():
    """Create middleware instance with default settings."""
    # Import here to avoid circular import

    return RepeatedToolCallMiddleware(max_repeats=3, window_size=10)


@pytest.fixture
def mock_request():
    """Create a mock tool call request."""
    request = MagicMock()
    request.tool_call = {"name": "test_tool", "args": {"param": "value"}}
    request.runtime = MagicMock()
    request.runtime.state = {}
    return request


class TestToolSignatureComputation:
    """Test tool signature hashing."""

    def test_hash_args_with_identical_args(self):
        """Test tool signature computation with identical arguments."""

        middleware = RepeatedToolCallMiddleware(max_repeats=3, window_size=10)
        args1 = {"param1": "value1", "param2": "value2"}
        args2 = {"param1": "value1", "param2": "value2"}

        hash1 = middleware._hash_args(args1)
        hash2 = middleware._hash_args(args2)

        assert hash1 == hash2
        assert len(hash1) == 16  # 16-char hash

    def test_hash_args_with_different_args(self):
        """Test tool signature computation with different arguments."""

        middleware = RepeatedToolCallMiddleware(max_repeats=3, window_size=10)
        args1 = {"param1": "value1"}
        args2 = {"param1": "value2"}

        hash1 = middleware._hash_args(args1)
        hash2 = middleware._hash_args(args2)

        assert hash1 != hash2

    def test_hash_args_order_independent(self):
        """Test that argument order doesn't affect hash."""
        from app.middleware.loop_detection_middleware import RepeatedToolCallMiddleware

        middleware = RepeatedToolCallMiddleware(max_repeats=3, window_size=10)
        args1 = {"a": 1, "b": 2, "c": 3}
        args2 = {"c": 3, "a": 1, "b": 2}

        hash1 = middleware._hash_args(args1)
        hash2 = middleware._hash_args(args2)

        assert hash1 == hash2

    def test_hash_args_with_non_serializable_args(self):
        """Test fallback for non-serializable arguments."""

        middleware = RepeatedToolCallMiddleware(max_repeats=3, window_size=10)

        class NonSerializable:
            pass

        args = {"obj": NonSerializable()}
        hash_result = middleware._hash_args(args)

        assert isinstance(hash_result, str)
        assert len(hash_result) == 16


class TestLoopDetection:
    """Test loop detection logic."""

    def test_check_for_loop_below_threshold(self, middleware):
        """Test that no loop is detected below max_repeats threshold."""
        history = [
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "result1"},
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "result2"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 2  # Two occurrences in history

    def test_check_for_loop_at_threshold(self, middleware):
        """Test that loop is detected when max_repeats threshold is hit."""
        history = [
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r1"},
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r2"},
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r3"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 3

    def test_check_for_loop_above_threshold(self, middleware):
        """Test detection when exceeding threshold."""
        history = [{"tool": "tool_a", "args_hash": "hash1", "result_summary": f"r{i}"} for i in range(5)]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 5

    def test_check_for_loop_different_tool_same_args(self, middleware):
        """Test that different tools don't interfere."""
        history = [
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r1"},
            {"tool": "tool_b", "args_hash": "hash1", "result_summary": "r2"},
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r3"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 2  # Only tool_a counts

    def test_check_for_loop_same_tool_different_args(self, middleware):
        """Test that different arguments don't count as loop."""
        history = [
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r1"},
            {"tool": "tool_a", "args_hash": "hash2", "result_summary": "r2"},
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r3"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 2  # Only hash1 counts


class TestToolCallHistoryManagement:
    """Test tool call history state management."""

    @pytest.mark.asyncio
    async def test_tool_call_history_initialization(self, middleware, mock_request):
        """Test that tool call history is properly initialized."""
        # Empty state
        mock_request.runtime.state = {}

        async def mock_handler(req):
            return ToolMessage(content="result", tool_call_id="123")

        await middleware.awrap_tool_call(mock_request, mock_handler)

        # History should be created
        assert "tool_call_history" in mock_request.runtime.state
        assert isinstance(mock_request.runtime.state["tool_call_history"], list)

    @pytest.mark.asyncio
    async def test_tool_call_history_tracking(self, middleware, mock_request):
        """Test that tool calls are tracked in history."""
        mock_request.runtime.state = {"tool_call_history": []}

        async def mock_handler(req):
            return ToolMessage(content="result", tool_call_id="123")

        await middleware.awrap_tool_call(mock_request, mock_handler)

        history = mock_request.runtime.state["tool_call_history"]
        assert len(history) == 1
        assert history[0]["tool"] == "test_tool"
        assert "args_hash" in history[0]

    @pytest.mark.asyncio
    async def test_sliding_window_pruning(self):
        """Test that old tool calls are pruned from history."""

        middleware = RepeatedToolCallMiddleware(max_repeats=3, window_size=5)
        mock_request = MagicMock()
        mock_request.tool_call = {"name": "tool", "args": {}}
        mock_request.runtime = MagicMock()

        # Pre-fill history with 5 calls
        mock_request.runtime.state = {
            "tool_call_history": [
                {"tool": f"tool_{i}", "args_hash": f"hash_{i}", "result_summary": ""} for i in range(5)
            ]
        }

        async def mock_handler(req):
            return ToolMessage(content="result", tool_call_id="123")

        await middleware.awrap_tool_call(mock_request, mock_handler)

        # Should have 5 items (pruned to window_size)
        history = mock_request.runtime.state["tool_call_history"]
        assert len(history) == 5

    @pytest.mark.asyncio
    async def test_result_summary_captured(self, middleware, mock_request):
        """Test that tool results are captured in history."""
        mock_request.runtime.state = {"tool_call_history": []}

        async def mock_handler(req):
            return ToolMessage(content="This is a long result " * 10, tool_call_id="123")

        await middleware.awrap_tool_call(mock_request, mock_handler)

        history = mock_request.runtime.state["tool_call_history"]
        assert len(history[0]["result_summary"]) <= 100  # First 100 chars


class TestInterruptMechanism:
    """Test interrupt mechanism when loop detected."""

    @pytest.mark.asyncio
    async def test_interrupt_triggered_on_loop(self, middleware, mock_request):
        """Test that interrupt is triggered when loop is detected."""
        # Pre-fill history to trigger loop
        mock_request.runtime.state = {
            "tool_call_history": [
                {"tool": "test_tool", "args_hash": middleware._hash_args({"param": "value"}), "result_summary": "r"}
                for _ in range(3)
            ]
        }

        async def mock_handler(req):
            return ToolMessage(content="result", tool_call_id="123")

        # Note: interrupt() is called from langgraph in actual code
        # We can't easily test this without full integration
        # This test documents that loop detection occurs at the right threshold
        is_loop, count = middleware._check_for_loop(
            "test_tool", middleware._hash_args({"param": "value"}), mock_request.runtime.state["tool_call_history"]
        )

        assert is_loop is True
        assert count >= middleware.max_repeats

    @pytest.mark.asyncio
    async def test_no_interrupt_below_threshold(self, middleware, mock_request):
        """Test that no interrupt occurs below threshold."""
        mock_request.runtime.state = {
            "tool_call_history": [
                {"tool": "test_tool", "args_hash": middleware._hash_args({"param": "value"}), "result_summary": "r"}
                for _ in range(2)
            ]
        }

        async def mock_handler(req):
            return ToolMessage(content="result", tool_call_id="123")

        result = await middleware.awrap_tool_call(mock_request, mock_handler)

        # Should execute normally
        assert isinstance(result, ToolMessage)


class TestPatternDetection:
    """Test various loop pattern detection scenarios."""

    @pytest.mark.asyncio
    async def test_same_tool_repeated_pattern(self, middleware, mock_request):
        """Test detection of same tool being called repeatedly."""
        # Fill history with same tool/args
        same_hash = middleware._hash_args({"param": "value"})
        mock_request.runtime.state = {
            "tool_call_history": [
                {"tool": "test_tool", "args_hash": same_hash, "result_summary": f"r{i}"} for i in range(3)
            ]
        }

        is_loop, count = middleware._check_for_loop(
            "test_tool", same_hash, mock_request.runtime.state["tool_call_history"]
        )

        assert is_loop is True
        assert count == 3

    def test_alternating_tool_pattern(self, middleware):
        """Test detection of alternating tool calls."""
        hash_a = middleware._hash_args({"a": 1})
        hash_b = middleware._hash_args({"b": 2})

        history = [
            {"tool": "tool_a", "args_hash": hash_a, "result_summary": "r"},
            {"tool": "tool_b", "args_hash": hash_b, "result_summary": "r"},
            {"tool": "tool_a", "args_hash": hash_a, "result_summary": "r"},
            {"tool": "tool_b", "args_hash": hash_b, "result_summary": "r"},
            {"tool": "tool_a", "args_hash": hash_a, "result_summary": "r"},
        ]

        # Check tool_a pattern
        is_loop, count = middleware._check_for_loop("tool_a", hash_a, history)
        assert is_loop is True
        assert count == 3

    def test_mixed_tool_pattern(self, middleware):
        """Test detection of complex patterns with multiple tools."""
        hash_a = middleware._hash_args({"a": 1})
        hash_b = middleware._hash_args({"b": 2})
        hash_c = middleware._hash_args({"c": 3})

        history = [
            {"tool": "tool_a", "args_hash": hash_a, "result_summary": "r"},
            {"tool": "tool_b", "args_hash": hash_b, "result_summary": "r"},
            {"tool": "tool_c", "args_hash": hash_c, "result_summary": "r"},
            {"tool": "tool_a", "args_hash": hash_a, "result_summary": "r"},
            {"tool": "tool_a", "args_hash": hash_a, "result_summary": "r"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", hash_a, history)
        assert is_loop is True
        assert count == 3

    def test_no_loop_with_different_tools(self, middleware):
        """Test that different tools don't trigger loop detection."""
        history = [
            {"tool": "tool_a", "args_hash": "hash_a", "result_summary": "r"},
            {"tool": "tool_b", "args_hash": "hash_b", "result_summary": "r"},
            {"tool": "tool_c", "args_hash": "hash_c", "result_summary": "r"},
            {"tool": "tool_d", "args_hash": "hash_d", "result_summary": "r"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash_a", history)
        assert is_loop is False
        assert count == 1


class TestEdgeCases:
    """Test edge cases for loop detection."""

    def test_empty_tool_call_history(self, middleware):
        """Test behavior with empty history."""
        is_loop, count = middleware._check_for_loop("tool_a", "hash1", [])

        assert is_loop is False
        assert count == 0

    def test_single_tool_call(self, middleware):
        """Test that single tool call doesn't trigger detection."""
        history = [
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 1

    def test_exact_threshold_boundary(self, middleware):
        """Test behavior at exact max_repeats threshold."""
        history = [{"tool": "tool_a", "args_hash": "hash1", "result_summary": "r"} for _ in range(3)]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 3

    def test_just_below_threshold(self):
        """Test behavior just below threshold."""

        middleware = RepeatedToolCallMiddleware(max_repeats=5, window_size=10)

        history = [{"tool": "tool_a", "args_hash": "hash1", "result_summary": "r"} for _ in range(4)]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 4

    def test_custom_configuration(self):
        """Test middleware with custom configuration."""

        middleware = RepeatedToolCallMiddleware(max_repeats=2, window_size=5)

        assert middleware.max_repeats == 2
        assert middleware.window_size == 5

        history = [
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r"},
            {"tool": "tool_a", "args_hash": "hash1", "result_summary": "r"},
        ]

        is_loop, count = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 2
