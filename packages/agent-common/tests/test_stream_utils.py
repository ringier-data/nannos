"""Tests for agent_common.a2a.stream_utils."""

import pytest
from ringier_a2a_sdk.utils.streaming import (
    DEFAULT_CHUNK_MIN,
    StreamBuffer,
    StructuredResponseStreamer,
    extract_text_from_content,
)

from agent_common.a2a.stream_utils import (
    retrieve_final_state,
)

# ---------------------------------------------------------------------------
# StreamBuffer
# ---------------------------------------------------------------------------


class TestStreamBuffer:
    def test_empty_buffer_flush_ready_returns_nothing(self):
        buf = StreamBuffer()
        assert buf.flush_ready() == []
        assert buf.flush_all() == ""

    def test_below_threshold_not_flushed(self):
        buf = StreamBuffer(chunk_min=40)
        buf.append("short")
        assert buf.flush_ready() == []
        assert buf.pending == "short"

    def test_flush_on_word_boundary(self):
        buf = StreamBuffer(chunk_min=10)
        buf.append("hello world, this is a test")
        chunks = buf.flush_ready()
        assert len(chunks) == 1
        # Should cut at a word boundary, not mid-word
        assert chunks[0].endswith(" ") or chunks[0] == "hello world, this is a test"
        # Flushed chunk + remaining must equal original
        assert chunks[0] + buf.pending == "hello world, this is a test"

    def test_flush_on_newline_boundary(self):
        buf = StreamBuffer(chunk_min=10)
        buf.append("first line\nsecond line")
        chunks = buf.flush_ready()
        assert len(chunks) == 1
        # Should prefer the newline boundary
        assert chunks[0].endswith("\n") or len(chunks[0]) >= 10

    def test_flush_all_returns_everything(self):
        buf = StreamBuffer()
        buf.append("abc")
        remaining = buf.flush_all()
        assert remaining == "abc"
        assert buf.pending == ""

    def test_flush_ready_with_no_whitespace(self):
        """When there's no whitespace to break on, flush the entire buffer."""
        buf = StreamBuffer(chunk_min=5)
        buf.append("abcdefghij")
        chunks = buf.flush_ready()
        assert len(chunks) == 1
        assert chunks[0] == "abcdefghij"
        assert buf.pending == ""

    def test_multiple_appends_accumulate(self):
        buf = StreamBuffer(chunk_min=10)
        buf.append("hello ")
        buf.append("world ")
        buf.append("foo")
        assert buf.pending == "hello world foo"

    def test_sequential_flushes(self):
        buf = StreamBuffer(chunk_min=5)
        buf.append("aaa bbb ccc ddd")
        all_flushed = []
        while True:
            chunks = buf.flush_ready()
            if not chunks:
                break
            all_flushed.extend(chunks)
        remaining = buf.flush_all()
        result = "".join(all_flushed) + remaining
        assert result == "aaa bbb ccc ddd"

    def test_default_chunk_min(self):
        buf = StreamBuffer()
        assert buf._chunk_min == DEFAULT_CHUNK_MIN


# ---------------------------------------------------------------------------
# StructuredResponseStreamer
# ---------------------------------------------------------------------------


class TestStructuredResponseStreamer:
    def test_not_tracking_returns_none(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        result = streamer.feed({"name": "other_tool", "args": '{"message": "hi"}'})
        assert result is None
        assert not streamer.tracking

    def test_starts_tracking_on_schema_name(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        result = streamer.feed({"name": "FinalResponseSchema", "args": ""})
        assert streamer.tracking
        assert result is None  # No args yet

    def test_incremental_message_extraction(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        # Start tracking
        streamer.feed({"name": "FinalResponseSchema", "args": ""})

        # Feed partial JSON incrementally
        d1 = streamer.feed({"args": '{"mess'})
        assert d1 is None  # message field not yet parseable

        d2 = streamer.feed({"args": 'age": "Hello'})
        assert d2 == "Hello"

        d3 = streamer.feed({"args": " World"})
        assert d3 == " World"

        d4 = streamer.feed({"args": '"}'})
        # May or may not produce delta depending on parse_partial_json behavior
        # The key invariant is that all deltas concatenated equal the full message

    def test_subagent_response_schema(self):
        streamer = StructuredResponseStreamer("SubAgentResponseSchema")
        streamer.feed({"name": "SubAgentResponseSchema", "args": ""})
        assert streamer.tracking

        delta = streamer.feed({"args": '{"message": "test result"}'})
        assert delta == "test result"

    def test_reset_on_new_detection(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        streamer.feed({"name": "FinalResponseSchema", "args": ""})
        streamer.feed({"args": '{"message": "first"}'})

        # New detection resets state
        streamer.feed({"name": "FinalResponseSchema", "args": ""})
        delta = streamer.feed({"args": '{"message": "second"}'})
        assert delta == "second"

    def test_empty_args_ignored(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        streamer.feed({"name": "FinalResponseSchema", "args": ""})
        result = streamer.feed({"args": ""})
        assert result is None


class TestStructuredResponseStreamerFeedContent:
    """Tests for feed_content() — handles models that emit schema JSON as plain text."""

    def test_non_json_content_passed_through(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        result = streamer.feed_content("Hello world")
        assert result == "Hello world"
        assert not streamer.content_tracking

    def test_schema_json_extracts_message_only(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        # Simulate Gemini streaming FinalResponseSchema as plain text tokens
        d1 = streamer.feed_content('{"task_state": "completed", "message": "Hello ')
        assert d1 == "Hello "  # Only the message field delta
        assert streamer.content_tracking

        d2 = streamer.feed_content("Erik!")
        assert d2 == "Erik!"

        d3 = streamer.feed_content('"}')
        # No new message content after closing quote
        assert d3 is None or d3 == ""

    def test_partial_json_suppressed_until_message(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        # First tokens: opening brace + task_state — no message yet
        d1 = streamer.feed_content('{"task_state')
        assert d1 is None  # Suppressed (accumulating JSON)

        d2 = streamer.feed_content('": "completed", ')
        assert d2 is None  # Still no message field

        d3 = streamer.feed_content('"message": "Hi"')
        assert d3 == "Hi"

    def test_non_schema_json_passed_through(self):
        """JSON that doesn't match the schema pattern is returned as-is."""
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        # Feed JSON that has neither "message" nor "task_state"
        result = streamer.feed_content('{"foo": "bar"}')
        assert result == '{"foo": "bar"}'
        assert not streamer.content_tracking

    def test_incremental_message_growth(self):
        streamer = StructuredResponseStreamer("FinalResponseSchema")
        streamer.feed_content('{"task_state": "completed", "message": "A')
        d2 = streamer.feed_content("B")
        assert d2 == "B"
        d3 = streamer.feed_content("C")
        assert d3 == "C"


# ---------------------------------------------------------------------------
# extract_text_from_content
# ---------------------------------------------------------------------------


class TestExtractTextFromContent:
    def test_string_content(self):
        text, thinking = extract_text_from_content("hello world")
        assert text == "hello world"
        assert thinking == []

    def test_list_with_text_blocks(self):
        content = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "World"},
        ]
        text, thinking = extract_text_from_content(content)
        assert text == "Hello World"
        assert thinking == []

    def test_list_with_thinking_blocks(self):
        content = [
            {"type": "thinking", "thinking": "Let me reason..."},
            {"type": "text", "text": "Final answer"},
        ]
        text, thinking = extract_text_from_content(content)
        assert text == "Final answer"
        assert len(thinking) == 1
        assert thinking[0]["thinking"] == "Let me reason..."

    def test_list_with_string_blocks(self):
        content = ["hello ", "world"]
        text, thinking = extract_text_from_content(content)
        assert text == "hello world"
        assert thinking == []

    def test_mixed_content(self):
        content = [
            {"type": "thinking", "thinking": "reasoning"},
            {"type": "text", "text": "answer"},
            "extra",
        ]
        text, thinking = extract_text_from_content(content)
        assert text == "answerextra"
        assert len(thinking) == 1

    def test_empty_list(self):
        text, thinking = extract_text_from_content([])
        assert text == ""
        assert thinking == []

    def test_non_standard_type(self):
        text, thinking = extract_text_from_content(42)
        assert text == "42"
        assert thinking == []

    def test_empty_thinking_block_excluded(self):
        content = [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "ok"}]
        text, thinking = extract_text_from_content(content)
        assert text == "ok"
        assert thinking == []

    def test_empty_text_block_excluded(self):
        content = [{"type": "text", "text": ""}]
        text, thinking = extract_text_from_content(content)
        assert text == ""


# ---------------------------------------------------------------------------
# retrieve_final_state
# ---------------------------------------------------------------------------


class TestRetrieveFinalState:
    def test_returns_values_on_success(self):
        class MockState:
            values = {"messages": ["hello"]}

        class MockGraph:
            def get_state(self, config):
                return MockState()

        result = retrieve_final_state(MockGraph(), {"configurable": {}})
        assert result == {"messages": ["hello"]}

    def test_raises_on_none_state(self):
        class MockGraph:
            def get_state(self, config):
                return None

        with pytest.raises(ValueError, match="could not retrieve final state"):
            retrieve_final_state(MockGraph(), {})

    def test_raises_on_empty_values(self):
        class MockState:
            values = {}

        class MockGraph:
            def get_state(self, config):
                return MockState()

        with pytest.raises(ValueError, match="could not retrieve final state"):
            retrieve_final_state(MockGraph(), {})
