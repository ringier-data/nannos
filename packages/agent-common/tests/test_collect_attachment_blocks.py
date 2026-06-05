"""Tests for collect_attachment_blocks_from_messages."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_common.backends.attachments_store import collect_attachment_blocks_from_messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILE_BLOCK = {"type": "file", "url": "https://s3.example/doc.txt", "mime_type": "text/plain", "filename": "doc.txt"}
_IMAGE_BLOCK = {"type": "image", "url": "https://s3.example/photo.png", "mime_type": "image/png", "filename": "photo.png"}
_SHEET_BLOCK = {"type": "file", "url": "https://s3.example/sheet.csv", "mime_type": "text/csv", "filename": "sheet.csv"}


def _orc_msg(blocks: list[dict], text: str = "hi") -> HumanMessage:
    """Orchestrator-style HumanMessage: blocks in additional_kwargs, plain text content."""
    return HumanMessage(content=text, additional_kwargs={"file_blocks": blocks})


def _agent_msg(blocks: list[dict], text: str = "hi") -> HumanMessage:
    """Sub-agent-style HumanMessage: blocks embedded in a multimodal content list."""
    content = [{"type": "text", "text": text}, *blocks]
    return HumanMessage(content=content)


def _text_msg(text: str = "follow-up") -> HumanMessage:
    """Plain text message with no file blocks."""
    return HumanMessage(content=text)


# ---------------------------------------------------------------------------
# Basic collection
# ---------------------------------------------------------------------------


def test_collects_orchestrator_style_blocks():
    msgs = [_orc_msg([_FILE_BLOCK])]
    assert collect_attachment_blocks_from_messages(msgs) == [_FILE_BLOCK]


def test_collects_agent_style_blocks():
    msgs = [_agent_msg([_IMAGE_BLOCK])]
    result = collect_attachment_blocks_from_messages(msgs)
    assert result == [_IMAGE_BLOCK]


def test_returns_empty_for_no_blocks():
    msgs = [_text_msg(), AIMessage(content="ok"), _text_msg()]
    assert collect_attachment_blocks_from_messages(msgs) == []


def test_returns_empty_for_empty_message_list():
    assert collect_attachment_blocks_from_messages([]) == []


# ---------------------------------------------------------------------------
# Accumulation across turns
# ---------------------------------------------------------------------------


def test_accumulates_blocks_across_turns():
    """Files from multiple turns are all collected."""
    msgs = [
        _orc_msg([_FILE_BLOCK]),   # turn 1
        _text_msg(),                # turn 2 — no files
        _orc_msg([_SHEET_BLOCK]),  # turn 3
    ]
    result = collect_attachment_blocks_from_messages(msgs)
    assert _FILE_BLOCK in result
    assert _SHEET_BLOCK in result


def test_most_recent_wins_on_filename_collision():
    """When the same filename appears in two turns, the newer version is kept."""
    old_block = {"type": "file", "url": "https://s3.example/v1/doc.txt", "filename": "doc.txt"}
    new_block = {"type": "file", "url": "https://s3.example/v2/doc.txt", "filename": "doc.txt"}
    msgs = [
        _orc_msg([old_block]),  # turn 1
        _orc_msg([new_block]),  # turn 2 — re-uploaded same filename
    ]
    result = collect_attachment_blocks_from_messages(msgs)
    assert len(result) == 1
    assert result[0]["url"] == "https://s3.example/v2/doc.txt"


def test_non_file_content_blocks_ignored():
    """Text and other non-file content blocks in a multimodal message are skipped."""
    content = [
        {"type": "text", "text": "here is a file"},
        _IMAGE_BLOCK,
    ]
    msgs = [HumanMessage(content=content)]
    result = collect_attachment_blocks_from_messages(msgs)
    assert result == [_IMAGE_BLOCK]


# ---------------------------------------------------------------------------
# Message count limit
# ---------------------------------------------------------------------------


def test_respects_max_messages_limit():
    """Stops scanning after max_messages regardless of remaining content."""
    # 25 messages, each with a distinct file; limit=20 → only 20 newest collected
    msgs = [_orc_msg([{"type": "file", "url": f"https://s3.example/f{i}.txt", "filename": f"f{i}.txt"}]) for i in range(25)]
    result = collect_attachment_blocks_from_messages(msgs, max_messages=20)
    assert len(result) == 20
    # The 20 newest (indices 5-24) should be present; the 5 oldest (0-4) should not
    collected_names = {b["filename"] for b in result}
    for i in range(5, 25):
        assert f"f{i}.txt" in collected_names
    for i in range(5):
        assert f"f{i}.txt" not in collected_names


def test_default_limit_is_20():
    msgs = [_orc_msg([{"type": "file", "url": f"https://s3.example/f{i}.txt", "filename": f"f{i}.txt"}]) for i in range(25)]
    assert len(collect_attachment_blocks_from_messages(msgs)) == 20


# ---------------------------------------------------------------------------
# Mixed message types (non-HumanMessage rows don't crash)
# ---------------------------------------------------------------------------


def test_ignores_ai_and_tool_messages():
    msgs = [
        _orc_msg([_FILE_BLOCK]),
        AIMessage(content="thinking…"),
        ToolMessage(content="result", tool_call_id="t1"),
        _text_msg(),
    ]
    result = collect_attachment_blocks_from_messages(msgs)
    assert result == [_FILE_BLOCK]


# ---------------------------------------------------------------------------
# HITL / multi-turn scenarios
# ---------------------------------------------------------------------------


def test_hitl_resume_finds_blocks_from_interrupted_turn():
    """Simulates a checkpoint where the interrupted turn's message has file_blocks."""
    msgs = [
        _orc_msg([_FILE_BLOCK], text="please analyse this"),
        AIMessage(content="", tool_calls=[{"name": "risky_tool", "args": {}, "id": "tc1", "type": "tool_call"}]),
        # graph interrupted here — no further messages yet
    ]
    result = collect_attachment_blocks_from_messages(msgs)
    assert result == [_FILE_BLOCK]


def test_multi_turn_follow_up_inherits_prior_blocks():
    """A follow-up message with no blocks still sees the prior turn's files."""
    msgs = [
        _orc_msg([_FILE_BLOCK], text="here is a file"),
        AIMessage(content="Got it."),
        _text_msg("what is on page 2?"),  # current turn — no new blocks
    ]
    # The current message is the last in the list; we pass all messages.
    result = collect_attachment_blocks_from_messages(msgs)
    assert result == [_FILE_BLOCK]


def test_multi_turn_new_file_takes_precedence_over_old():
    """A new file uploaded in a later turn wins over an older file with the same name."""
    old = {"type": "file", "url": "https://s3.example/v1/report.pdf", "filename": "report.pdf"}
    new = {"type": "file", "url": "https://s3.example/v2/report.pdf", "filename": "report.pdf"}
    msgs = [
        _orc_msg([old], text="old version"),
        AIMessage(content="ok"),
        _orc_msg([new], text="updated version"),
        AIMessage(content="got it"),
        _text_msg("what changed?"),
    ]
    result = collect_attachment_blocks_from_messages(msgs)
    assert len(result) == 1
    assert result[0]["url"] == "https://s3.example/v2/report.pdf"


def test_current_message_with_new_file_merges_with_prior_files():
    """When appended as the final message, the current turn's file joins prior files."""
    prior_block = {"type": "file", "url": "https://s3.example/notes.txt", "filename": "notes.txt"}
    new_block = {"type": "file", "url": "https://s3.example/sheet.csv", "filename": "sheet.csv"}

    checkpoint_msgs = [
        _orc_msg([prior_block], text="here are my notes"),
        AIMessage(content="got it"),
    ]
    current_msg = _orc_msg([new_block], text="now compare with this sheet")

    result = collect_attachment_blocks_from_messages(checkpoint_msgs + [current_msg])
    filenames = {b["filename"] for b in result}
    assert filenames == {"notes.txt", "sheet.csv"}


def test_current_message_new_file_wins_collision_with_prior():
    """Re-uploading the same filename: the current message's version wins."""
    old = {"type": "file", "url": "https://s3.example/v1/doc.txt", "filename": "doc.txt"}
    new = {"type": "file", "url": "https://s3.example/v2/doc.txt", "filename": "doc.txt"}

    checkpoint_msgs = [_orc_msg([old], text="old doc")]
    current_msg = _orc_msg([new], text="updated doc")

    result = collect_attachment_blocks_from_messages(checkpoint_msgs + [current_msg])
    assert len(result) == 1
    assert result[0]["url"] == "https://s3.example/v2/doc.txt"
