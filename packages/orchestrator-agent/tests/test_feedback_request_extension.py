"""Tests for A2A extension helpers (Phase 2) — feedback request extension."""

from app.core.a2a_extensions import (
    ALL_EXTENSIONS,
    FEEDBACK_REQUEST_EXTENSION,
    new_feedback_request_message,
)


def test_feedback_request_extension_in_all():
    assert FEEDBACK_REQUEST_EXTENSION in ALL_EXTENSIONS


def test_feedback_request_extension_uri():
    assert FEEDBACK_REQUEST_EXTENSION == "urn:nannos:a2a:feedback-request:1.0"


def test_new_feedback_request_message_basic():
    msg = new_feedback_request_message(context_id="ctx-1", task_id="task-1")

    assert msg.extensions == [FEEDBACK_REQUEST_EXTENSION]
    assert msg.context_id == "ctx-1"
    assert msg.task_id == "task-1"
    assert len(msg.parts) == 1
    data_part = msg.parts[0].root
    assert data_part.data == {"sub_agents": []}


def test_new_feedback_request_message_with_sub_agents():
    msg = new_feedback_request_message(
        context_id="ctx-1",
        task_id="task-1",
        sub_agents_involved=["search-agent", "writer-agent"],
    )

    data_part = msg.parts[0].root
    assert data_part.data["sub_agents"] == ["search-agent", "writer-agent"]


def test_new_feedback_request_message_unique_ids():
    msg1 = new_feedback_request_message()
    msg2 = new_feedback_request_message()
    assert msg1.message_id != msg2.message_id
