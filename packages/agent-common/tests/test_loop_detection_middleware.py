"""Unit tests for LoopDetectionMiddleware.

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

from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware


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


class TestForceStop:
    """Test force-stop behavior that strips tool_calls from AIMessage."""

    @pytest.mark.asyncio
    async def test_force_stop_strips_tool_calls_after_threshold(self):
        """After force_stop_after consecutive blocks, tool_calls are stripped from AIMessage."""
        from langchain_core.messages import AIMessage

        middleware = RepeatedToolCallMiddleware(max_repeats=3, force_stop_after=2, window_size=20)

        # Simulate history where tool has been called 5 times with same args
        # (3 allowed + 2 blocked = exceeds force_stop_after=2)
        args_hash = middleware._hash_args({"path": "/file.txt"})
        tool_history = [args_hash] * 5

        ai_message = AIMessage(
            content="",
            id="msg-123",
            tool_calls=[{"name": "read_personal_file", "args": {"path": "/file.txt"}, "id": "tc-1"}],
        )

        state = {
            "messages": [ai_message],
            "tool_call_history": {"read_personal_file": tool_history},
        }

        result = await middleware.aafter_model(state, MagicMock())

        assert result is not None
        # Should return a modified AIMessage (same ID, no tool_calls)
        assert "messages" in result
        modified_msg = result["messages"][0]
        assert isinstance(modified_msg, AIMessage)
        assert modified_msg.id == "msg-123"
        assert modified_msg.tool_calls == []

    @pytest.mark.asyncio
    async def test_no_force_stop_below_threshold(self):
        """Below force_stop_after threshold, returns error ToolMessages (not force-stop)."""
        from langchain_core.messages import AIMessage

        middleware = RepeatedToolCallMiddleware(max_repeats=3, force_stop_after=3, window_size=20)

        # 4 calls = 1 block (just past max_repeats=3), not enough for force_stop_after=3
        args_hash = middleware._hash_args({"path": "/file.txt"})
        tool_history = [args_hash] * 3

        ai_message = AIMessage(
            content="",
            id="msg-456",
            tool_calls=[{"name": "read_personal_file", "args": {"path": "/file.txt"}, "id": "tc-2"}],
        )

        state = {
            "messages": [ai_message],
            "tool_call_history": {"read_personal_file": tool_history},
        }

        result = await middleware.aafter_model(state, MagicMock())

        assert result is not None
        # Should return error ToolMessage, NOT a modified AIMessage
        from langchain_core.messages import ToolMessage

        assert "messages" in result
        assert len(result["messages"]) == 1
        assert isinstance(result["messages"][0], ToolMessage)
        assert result["messages"][0].status == "error"

    @pytest.mark.asyncio
    async def test_force_stop_same_tool_with_equal_window_and_threshold(self):
        """Force-stop must trigger for same_tool even when window_size == max_tool_repeats.

        Regression test: previously, window trimming capped repeat_count at
        window_size+1, so force_stop_after could never be reached. The model
        would loop block→retry indefinitely, eventually returning a stale
        structured_response from a previous sub-agent.
        """
        from langchain_core.messages import AIMessage

        middleware = RepeatedToolCallMiddleware(
            max_repeats=5, max_tool_repeats=10, window_size=10, force_stop_after=3, dispatch_tools=set()
        )

        # Simulate: 10 allowed calls, then 3 consecutive blocks (calls 11, 12, 13).
        # On each block, aafter_model should grow the history (no window trim).
        # By the 3rd block, repeat_count should be 13 and force-stop fires.
        tool_history = [f"hash_{i}" for i in range(10)]  # 10 unique calls

        for block_round in range(3):
            ai_message = AIMessage(
                content="",
                id=f"msg-fs-{block_round}",
                tool_calls=[
                    {"name": "search", "args": {"q": f"query_{10 + block_round}"}, "id": f"tc-fs-{block_round}"}
                ],
            )
            state = {"messages": [ai_message], "tool_call_history": {"search": list(tool_history)}}
            result = await middleware.aafter_model(state, MagicMock())

            assert result is not None
            # Update tool_history with what middleware returned
            tool_history = result["tool_call_history"]["search"]

            if block_round < 2:
                # First 2 blocks: error ToolMessages (not force-stop yet)
                from langchain_core.messages import ToolMessage

                assert len(result["messages"]) == 1
                assert isinstance(result["messages"][0], ToolMessage)
            else:
                # 3rd block: force-stop — AIMessage with stripped tool_calls
                assert len(result["messages"]) == 1
                assert isinstance(result["messages"][0], AIMessage)
                assert result["messages"][0].tool_calls == []

    """Test dispatch_tools parameter for exempting dispatcher tools from max_tool_repeats."""

    def test_dispatch_tool_exempt_from_max_tool_repeats(self):
        """Dispatch tools should NOT be blocked by max_tool_repeats."""
        middleware = RepeatedToolCallMiddleware(
            max_repeats=5, max_tool_repeats=3, window_size=20, dispatch_tools={"task"}
        )

        # 5 calls with different args — exceeds max_tool_repeats=3 but should NOT trigger
        history = ["hash_a", "hash_b", "hash_c", "hash_d"]
        is_loop, count, loop_type = middleware._check_for_loop("task", "hash_e", history)

        assert is_loop is False

    def test_dispatch_tool_still_checked_for_same_args(self):
        """Dispatch tools should still be blocked by max_repeats (same args)."""
        middleware = RepeatedToolCallMiddleware(
            max_repeats=3, max_tool_repeats=5, window_size=20, dispatch_tools={"task"}
        )

        # 3 calls with identical args + 1 current = 4, exceeds max_repeats=3
        history = ["same_hash", "same_hash", "same_hash"]
        is_loop, count, loop_type = middleware._check_for_loop("task", "same_hash", history)

        assert is_loop is True
        assert loop_type == "same_args"
        assert count == 4

    def test_non_dispatch_tool_still_checked_for_max_tool_repeats(self):
        """Non-dispatch tools should still be blocked by max_tool_repeats."""
        middleware = RepeatedToolCallMiddleware(
            max_repeats=5, max_tool_repeats=3, window_size=20, dispatch_tools={"task"}
        )

        # 'other_tool' is NOT in dispatch_tools, so max_tool_repeats applies
        history = ["hash_a", "hash_b", "hash_c"]
        is_loop, count, loop_type = middleware._check_for_loop("other_tool", "hash_d", history)

        assert is_loop is True
        assert loop_type == "same_tool"
        assert count == 4

    def test_eval_exempt_from_max_tool_repeats_with_distinct_code(self):
        """The PTC code interpreter (`eval`) is a gateway tool: many *distinct*
        calls (different code) must NOT be blocked by max_tool_repeats — a normal
        multi-step PTC agent would otherwise be force-stopped mid-task."""
        middleware = RepeatedToolCallMiddleware(
            max_repeats=5, max_tool_repeats=10, window_size=10, dispatch_tools={"task", "eval"}
        )

        # 12 distinct eval calls — exceeds max_tool_repeats=10 but exempt.
        history = [f"hash{i}" for i in range(11)]
        is_loop, _count, _loop_type = middleware._check_for_loop("eval", "hash_new", history)

        assert is_loop is False

    def test_eval_still_blocked_on_identical_code(self):
        """`eval` remains subject to max_repeats: identical code repeated is a real
        loop (mode='call' → identical code yields identical result, no progress)."""
        middleware = RepeatedToolCallMiddleware(
            max_repeats=5, max_tool_repeats=10, window_size=20, dispatch_tools={"task", "eval"}
        )

        history = ["same"] * 5  # 5 identical + current = 6 > max_repeats=5
        is_loop, count, loop_type = middleware._check_for_loop("eval", "same", history)

        assert is_loop is True
        assert loop_type == "same_args"
        assert count == 6

    @pytest.mark.asyncio
    async def test_dispatch_tool_not_blocked_in_aafter_model(self):
        """Full integration: dispatch tool with many different-args calls is not blocked."""
        from langchain_core.messages import AIMessage

        middleware = RepeatedToolCallMiddleware(
            max_repeats=5, max_tool_repeats=3, window_size=20, dispatch_tools={"task"}
        )

        # Simulate 10 previous calls to 'task' with different args
        history = {
            "task": [f"hash_{i}" for i in range(10)],
        }

        ai_message = AIMessage(
            content="",
            id="msg-dispatch",
            tool_calls=[{"name": "task", "args": {"description": "new task", "subagent_type": "gp"}, "id": "tc-d1"}],
        )

        state = {"messages": [ai_message], "tool_call_history": history}
        result = await middleware.aafter_model(state, MagicMock())

        # Should only update history, no blocked calls
        assert result is not None
        assert "tool_call_history" in result
        # No error messages — just history update
        assert "messages" not in result

    def test_dispatch_tools_default_includes_task(self):
        """dispatch_tools defaults to {'task'}."""
        middleware = RepeatedToolCallMiddleware(max_repeats=3)
        assert middleware.dispatch_tools == {"task"}


class TestErrorMessages:
    """Test that error messages are actionable and distinct per loop type."""

    def test_same_args_error_message(self):
        """same_args error should tell model not to retry with same arguments."""
        middleware = RepeatedToolCallMiddleware(max_repeats=3)
        info = {
            "tool_call": {"id": "tc-1"},
            "tool_name": "read_file",
            "loop_type": "same_args",
            "description": "Tool 'read_file' called 4 times with identical arguments",
            "repeat_count": 4,
        }
        msg = middleware._build_error_message(info)
        assert "same arguments" in msg.lower()
        assert "BLOCKED" in msg
        assert "respond to the user" in msg

    def test_same_tool_error_message(self):
        """same_tool error should tell model to stop and respond."""
        middleware = RepeatedToolCallMiddleware(max_repeats=3, max_tool_repeats=5)
        info = {
            "tool_call": {"id": "tc-2"},
            "tool_name": "search",
            "loop_type": "same_tool",
            "description": "Tool 'search' called 6 times (with 5 different argument sets)",
            "repeat_count": 6,
        }
        msg = middleware._build_error_message(info)
        assert "many times" in msg.lower()
        assert "BLOCKED" in msg
        assert "respond to the user" in msg
