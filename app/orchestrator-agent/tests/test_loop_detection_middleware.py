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
        history = ["hash1", "hash1"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 0  # No loop detected, count is 0

    def test_check_for_loop_at_threshold(self, middleware):
        """Test that loop is detected when max_repeats threshold is hit."""
        # History has 3 instances of hash1, current call makes it 4 (exceeds threshold of 3)
        history = ["hash1", "hash1", "hash1"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 4

    def test_check_for_loop_above_threshold(self, middleware):
        """Test detection when exceeding threshold."""
        # History has 5 instances of hash1, current call makes it 6 (exceeds threshold of 3)
        history = ["hash1"] * 5

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 6

    def test_check_for_loop_different_tool_same_args(self, middleware):
        """Test that different tools don't interfere."""
        # History contains only tool_a calls (tool_b is tracked separately)
        # 2 instances of hash1 in tool_a history + 1 current = 3 (but count is 0 when no loop)
        history = ["hash1", "hash1"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 0  # No loop detected

    def test_check_for_loop_same_tool_different_args(self, middleware):
        """Test that different arguments don't count as loop."""
        # History for tool_a, but only hash1 calls matter (hash2 is different)
        # 2 instances of hash1 + 1 current = 3 (but count is 0 when no loop)
        history = ["hash1", "hash2", "hash1"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 0  # No loop detected


class TestToolCallHistoryManagement:
    """Test tool call history state management.

    NOTE: These tests are disabled because they test awrap_tool_call which
    doesn't exist in this middleware. This middleware uses aafter_model hook.
    TODO: Rewrite these to test aafter_model behavior.
    """

    pass


class TestInterruptMechanism:
    """Test interrupt mechanism when loop detected."""

    @pytest.mark.asyncio
    async def test_interrupt_triggered_on_loop(self, middleware, mock_request):
        """Test that interrupt is triggered when loop is detected."""
        # Pre-fill history to trigger loop
        hash_val = middleware._hash_args({"param": "value"})
        mock_request.runtime.state = {"tool_call_history": [hash_val for _ in range(3)]}

        # Note: interrupt() is called from langgraph in actual code
        # We can't easily test this without full integration
        # This test documents that loop detection occurs at the right threshold
        is_loop, count, loop_type = middleware._check_for_loop(
            "test_tool", hash_val, mock_request.runtime.state["tool_call_history"]
        )

        assert is_loop is True
        assert count >= middleware.max_repeats

    # NOTE: test_no_interrupt_below_threshold removed as it tests awrap_tool_call which doesn't exist


class TestPatternDetection:
    """Test various loop pattern detection scenarios."""

    @pytest.mark.asyncio
    async def test_same_tool_repeated_pattern(self, middleware, mock_request):
        """Test detection of same tool being called repeatedly."""
        # Fill history with same tool/args
        same_hash = middleware._hash_args({"param": "value"})
        mock_request.runtime.state = {"tool_call_history": [same_hash for _ in range(3)]}

        is_loop, count, loop_type = middleware._check_for_loop(
            "test_tool", same_hash, mock_request.runtime.state["tool_call_history"]
        )

        assert is_loop is True
        assert count == 4

    def test_alternating_tool_pattern(self, middleware):
        """Test detection of alternating tool calls."""
        hash_a = middleware._hash_args({"a": 1})
        hash_b = middleware._hash_args({"b": 2})

        history = [hash_a, hash_b, hash_a, hash_b, hash_a]

        # Check tool_a pattern
        is_loop, count, loop_type = middleware._check_for_loop("tool_a", hash_a, history)
        assert is_loop is True
        assert count == 4

    def test_mixed_tool_pattern(self, middleware):
        """Test detection of complex patterns with multiple tools."""
        hash_a = middleware._hash_args({"a": 1})
        hash_b = middleware._hash_args({"b": 2})
        hash_c = middleware._hash_args({"c": 3})

        history = [hash_a, hash_b, hash_c, hash_a, hash_a]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", hash_a, history)
        assert is_loop is True
        assert count == 4

    def test_no_loop_with_different_tools(self, middleware):
        """Test that different tools don't trigger loop detection."""
        history = ["hash_a", "hash_b", "hash_c", "hash_d"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash_a", history)
        assert is_loop is False
        assert count == 0  # No loop detected


class TestEdgeCases:
    """Test edge cases for loop detection."""

    def test_empty_tool_call_history(self, middleware):
        """Test behavior with empty history."""
        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", [])

        assert is_loop is False
        assert count == 0  # No loop detected

    def test_single_tool_call(self, middleware):
        """Test that single tool call doesn't trigger detection."""
        history = ["hash1"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 0  # No loop detected

    def test_exact_threshold_boundary(self, middleware):
        """Test behavior at exact max_repeats threshold."""
        history = ["hash1"] * 3

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 4

    def test_just_below_threshold(self):
        """Test behavior just below threshold."""

        middleware = RepeatedToolCallMiddleware(max_repeats=5, window_size=10)

        history = ["hash1"] * 4

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is False
        assert count == 0  # No loop detected (5 repeats < 5 threshold)

    def test_custom_configuration(self):
        """Test middleware with custom configuration."""

        middleware = RepeatedToolCallMiddleware(max_repeats=2, window_size=5)

        assert middleware.max_repeats == 2
        assert middleware.window_size == 5

        history = ["hash1", "hash1"]

        is_loop, count, loop_type = middleware._check_for_loop("tool_a", "hash1", history)

        assert is_loop is True
        assert count == 3
