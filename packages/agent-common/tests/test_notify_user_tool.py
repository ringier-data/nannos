"""The notify_user tool — a mid-turn note that does NOT end the turn.

Delivery is fire-and-forget over the custom stream (no interrupt, no result), so
the graph keeps running and the A2A task stays ``working``. The tool's own return
value is guidance for the model, never user-visible text.
"""

from unittest.mock import Mock, patch

import pytest

from agent_common.core.notify_user_tool import (
    MAX_NOTE_CHARS,
    NOTIFY_USER_TOOL_NAME,
    NOTE_KIND,
    USER_NOTE_EVENT,
    _notify_user_handler,
    create_notify_user_tool,
)
from agent_common.core.tool_risk_scorer import score_tool_risk

MODULE = "agent_common.core.notify_user_tool"


class TestDelivery:
    @pytest.mark.asyncio
    async def test_emits_note_on_the_custom_stream(self):
        writer = Mock()
        with patch(f"{MODULE}.get_stream_writer", return_value=writer):
            out = await _notify_user_handler(text="Understood — pulling last week's numbers.")

        writer.assert_called_once_with(
            (USER_NOTE_EVENT, {"message": "Understood — pulling last week's numbers."})
        )
        assert "shown to the user" in out.lower()

    @pytest.mark.asyncio
    async def test_never_interrupts(self):
        """A note must not pause the graph — no interrupt() anywhere in this path."""
        writer = Mock()
        with (
            patch(f"{MODULE}.get_stream_writer", return_value=writer),
            patch("langgraph.types.interrupt", side_effect=AssertionError("must not interrupt")),
        ):
            await _notify_user_handler(text="Working on it.")
        assert writer.call_count == 1

    @pytest.mark.asyncio
    async def test_text_is_trimmed(self):
        writer = Mock()
        with patch(f"{MODULE}.get_stream_writer", return_value=writer):
            await _notify_user_handler(text="  Starting now.\n")
        assert writer.call_args[0][0][1]["message"] == "Starting now."

    @pytest.mark.asyncio
    async def test_overlong_note_is_truncated_not_rejected(self):
        writer = Mock()
        with patch(f"{MODULE}.get_stream_writer", return_value=writer):
            await _notify_user_handler(text="x" * (MAX_NOTE_CHARS + 200))
        emitted = writer.call_args[0][0][1]["message"]
        assert len(emitted) == MAX_NOTE_CHARS
        assert emitted.endswith("…")

    @pytest.mark.asyncio
    async def test_empty_note_emits_nothing(self):
        writer = Mock()
        with patch(f"{MODULE}.get_stream_writer", return_value=writer):
            out = await _notify_user_handler(text="   ")
        writer.assert_not_called()
        assert "empty" in out.lower()

    @pytest.mark.asyncio
    async def test_no_stream_writer_is_not_an_error(self):
        """A scheduled/batch run has no client attached: say so, don't fail the turn."""
        with patch(f"{MODULE}.get_stream_writer", return_value=None):
            out = await _notify_user_handler(text="Starting.")
        assert "not shown" in out.lower()

    @pytest.mark.asyncio
    async def test_stream_writer_unavailable_outside_a_run(self):
        with patch(f"{MODULE}.get_stream_writer", side_effect=RuntimeError("no run")):
            out = await _notify_user_handler(text="Starting.")
        assert "not shown" in out.lower()


class TestToolSurface:
    def test_tool_name_and_schema(self):
        tool = create_notify_user_tool()
        assert tool.name == NOTIFY_USER_TOOL_NAME
        assert set(tool.args.keys()) == {"text"}

    def test_description_states_the_turn_continues(self):
        desc = create_notify_user_tool().description.lower()
        assert "does not end it" in desc
        assert "answer" in desc  # tells the model not to put the answer here

    def test_note_kind_marker(self):
        assert NOTE_KIND == "note"


class TestRiskScoring:
    @pytest.mark.asyncio
    async def test_scored_zero_deterministically_without_a_cache(self):
        """A progress note must never raise an approval card, and must never pay for
        an LLM scoring round trip."""
        score, entry = await score_tool_risk(NOTIFY_USER_TOOL_NAME, {"text": "anything"}, cache=None)
        assert score == 0.0
        assert entry is not None and entry.base_score == 0.0
