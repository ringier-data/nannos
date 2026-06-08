"""Unit tests for ``StructuredResponseStreamer`` task_state awareness.

The orchestrator routes an intermediate ``working`` message (e.g. narration
emitted while delegating to a sub-agent) to the thinking channel instead of the
visible response. That decision relies on the streamer surfacing ``task_state``
before any ``message`` delta is emitted.
"""

from __future__ import annotations

from ringier_a2a_sdk.utils.streaming import StructuredResponseStreamer


def _feed_tool_call(streamer: StructuredResponseStreamer, full_args_json: str, *, name="FinalResponseSchema"):
    """Feed an args JSON string as a sequence of small tool_call_chunks."""
    deltas = []
    # First chunk carries the tool name; subsequent chunks carry args slices.
    streamer.feed({"name": name, "args": ""})
    for i in range(0, len(full_args_json), 7):
        d = streamer.feed({"name": None, "args": full_args_json[i : i + 7]})
        if d:
            deltas.append(d)
    return deltas


class TestTaskStateFromToolCall:
    def test_working_is_flagged_and_message_extracted(self):
        s = StructuredResponseStreamer("FinalResponseSchema")
        deltas = _feed_tool_call(s, '{"task_state": "working", "message": "Delegating now"}')
        assert s.task_state == "working"
        assert s.is_working is True
        assert "".join(deltas) == "Delegating now"

    def test_completed_is_not_working(self):
        s = StructuredResponseStreamer("FinalResponseSchema")
        deltas = _feed_tool_call(s, '{"task_state": "completed", "message": "All done"}')
        assert s.task_state == "completed"
        assert s.is_working is False
        assert "".join(deltas) == "All done"

    def test_task_state_known_before_first_message_delta(self):
        """task_state precedes message in the schema, so is_working must be set
        by the time the first message delta is produced."""
        s = StructuredResponseStreamer("FinalResponseSchema")
        s.feed({"name": "FinalResponseSchema", "args": ""})
        # Feed only up to the start of the message value.
        s.feed({"name": None, "args": '{"task_state": "working", "mess'})
        assert s.is_working is True  # known already, before any message char
        d = s.feed({"name": None, "args": 'age": "Hi'})
        assert d == "Hi"
        assert s.is_working is True

    def test_reset_between_schema_responses(self):
        s = StructuredResponseStreamer("FinalResponseSchema")
        _feed_tool_call(s, '{"task_state": "working", "message": "wip"}')
        assert s.is_working is True
        # A fresh FinalResponseSchema tool call resets task_state.
        _feed_tool_call(s, '{"task_state": "completed", "message": "final"}')
        assert s.task_state == "completed"
        assert s.is_working is False


class TestTaskStateFromContent:
    def test_working_message_from_plain_text(self):
        s = StructuredResponseStreamer("FinalResponseSchema")
        out = s.feed_content('{"task_state": "working", "message": "I will delegate"}')
        assert s.is_working is True
        assert out == "I will delegate"

    def test_completed_message_from_plain_text(self):
        s = StructuredResponseStreamer("FinalResponseSchema")
        out = s.feed_content('{"task_state": "completed", "message": "Result text"}')
        assert s.is_working is False
        assert out == "Result text"

    def test_non_schema_text_passthrough_without_task_state(self):
        s = StructuredResponseStreamer("FinalResponseSchema")
        out = s.feed_content("just some plain prose")
        assert out == "just some plain prose"
        assert s.task_state is None
        assert s.is_working is False
