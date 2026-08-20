"""The eval assertion vocabulary in tests/support/extraction.py.

These run in the normal suite (no credentials, no LLM) because the extraction
rules are where an eval suite silently goes wrong: read the wrong turn, or the
stale ``structured_response`` channel, and a test passes on last turn's work.
Pinning them against synthetic state costs nothing and keeps the expensive
tier honest.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.models.schemas import FinalResponseSchema
from tests.support.extraction import (
    a2a_tracking,
    delegated_agents,
    delegations,
    final_response,
    final_text,
    message_text,
    task_state,
    tool_calls,
    tool_names,
)


def _task_call(subagent: str, call_id: str = "c1", description: str = "do the thing") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "task",
                "args": {"subagent_type": subagent, "description": description},
                "type": "tool_call",
            }
        ],
    )


def _final_call(call_id: str = "f1", **args) -> AIMessage:
    payload = {"task_state": "completed", "message": "all done", **args}
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "FinalResponseSchema", "args": payload, "type": "tool_call"}],
    )


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


def test_delegation_is_read_from_task_subagent_type():
    state = {
        "messages": [
            HumanMessage("slack the summary to john"),
            _task_call("agent-runner", "c1", "fetch Q3 summary"),
            ToolMessage(content="Q3 revenue up 8%", tool_call_id="c1", name="task"),
            _task_call("slack-client", "c2", "post to @john.doe"),
            ToolMessage(content="sent", tool_call_id="c2", name="task"),
        ]
    }

    assert delegated_agents(state) == ["agent-runner", "slack-client"]

    first, second = delegations(state)
    assert (first.subagent, first.description) == ("agent-runner", "fetch Q3 summary")
    assert second.result == "sent"
    assert second.completed


def test_repeated_delegation_to_same_agent_is_deduplicated_but_ordered():
    state = {
        "messages": [
            HumanMessage("go"),
            _task_call("agent-runner", "c1"),
            _task_call("slack-client", "c2"),
            _task_call("agent-runner", "c3"),
        ]
    }
    assert delegated_agents(state) == ["agent-runner", "slack-client"]
    assert [d.subagent for d in delegations(state)] == ["agent-runner", "slack-client", "agent-runner"]


def test_attempted_delegation_without_result_is_visible_but_not_completed():
    """An interrupted or failed `task` still counts as routing intent."""
    state = {"messages": [HumanMessage("go"), _task_call("slack-client", "c1")]}

    assert delegated_agents(state) == ["slack-client"]
    assert delegated_agents(state, completed_only=True) == []
    assert delegations(state)[0].completed is False


# ---------------------------------------------------------------------------
# Turn scoping
# ---------------------------------------------------------------------------


def test_previous_turn_delegation_does_not_leak_into_this_turn():
    """The checkpointer keeps history; an unscoped read would pass on stale work."""
    state = {
        "messages": [
            HumanMessage("slack john"),
            _task_call("slack-client", "c1"),
            ToolMessage(content="sent", tool_call_id="c1", name="task"),
            HumanMessage("thanks, what time is it?"),
            AIMessage(content="It is 14:00."),
        ]
    }

    assert delegated_agents(state) == []
    assert delegated_agents(state, all_turns=True) == ["slack-client"]


# ---------------------------------------------------------------------------
# Tool visibility
# ---------------------------------------------------------------------------


def test_only_orchestrator_visible_tools_are_reported():
    """Sub-agent internals never reach orchestrator state — only the `task` blob does."""
    state = {
        "messages": [
            HumanMessage("go"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "t1", "name": "write_todos", "args": {"todos": []}, "type": "tool_call"},
                    {"id": "t2", "name": "get_current_time", "args": {}, "type": "tool_call"},
                ],
            ),
            ToolMessage(content="ok", tool_call_id="t1", name="write_todos"),
            ToolMessage(content="14:00", tool_call_id="t2", name="get_current_time"),
            _task_call("slack-client", "c1"),
            ToolMessage(content="sent", tool_call_id="c1", name="task"),
        ]
    }

    assert tool_names(state) == ["write_todos", "get_current_time", "task"]
    assert "send_slack_message" not in tool_names(state)

    by_name = {c.name: c for c in tool_calls(state)}
    assert by_name["get_current_time"].result == "14:00"


# ---------------------------------------------------------------------------
# Final response
# ---------------------------------------------------------------------------


def test_current_turn_final_response_wins_over_stale_channel():
    state = {
        "structured_response": {"task_state": "failed", "message": "stale from last turn"},
        "messages": [HumanMessage("go"), _final_call(message="fresh answer")],
    }

    assert final_response(state)["message"] == "fresh answer"
    assert final_text(state) == "fresh answer"
    assert task_state(state) == "completed"


def test_structured_response_channel_used_when_no_tool_call_this_turn():
    state = {
        "structured_response": {"task_state": "input_required", "message": "which john?"},
        "messages": [HumanMessage("slack john"), AIMessage(content="")],
    }

    assert final_text(state) == "which john?"
    assert task_state(state) == "input_required"


def test_structured_response_may_be_a_pydantic_instance_not_a_dict():
    """The graph is built with `response_format`, so LangGraph puts a validated
    FinalResponseSchema *object* in this channel — the shape production actually
    sees, and why StreamHandler reads it via getattr rather than subscripting."""
    state = {
        "structured_response": FinalResponseSchema(task_state="input_required", message="which john?"),
        "messages": [HumanMessage("slack john"), AIMessage(content="")],
    }

    assert final_response(state) == {
        "task_state": "input_required",
        "message": "which john?",
        "include_subagent_output": False,
    }
    assert final_text(state) == "which john?"
    assert task_state(state) == "input_required"


def test_include_subagent_output_appends_the_delegation_result():
    """When the model passes a sub-agent's work through verbatim, the schema tells
    it to leave `message` EMPTY and the output is appended downstream. Reading
    `message` alone reports an empty answer for a turn that answered at length —
    a real gemini run did exactly this and the assertion failed on ''."""
    state = {
        "messages": [
            HumanMessage("what was Q3 revenue?"),
            _task_call("agent-runner", "c1"),
            ToolMessage(content="Q3 revenue was 8.2 million EUR.", tool_call_id="c1", name="task"),
            _final_call(message="", include_subagent_output=True),
        ]
    }

    assert final_text(state) == "Q3 revenue was 8.2 million EUR."


def test_include_subagent_output_keeps_a_nonempty_message_as_a_preamble():
    state = {
        "messages": [
            HumanMessage("what was Q3 revenue?"),
            _task_call("agent-runner", "c1"),
            ToolMessage(content="8.2 million EUR.", tool_call_id="c1", name="task"),
            _final_call(message="Here you go:", include_subagent_output=True),
        ]
    }

    assert final_text(state) == "Here you go:\n\n8.2 million EUR."


def test_final_text_falls_back_to_last_nonempty_ai_message():
    state = {
        "messages": [
            HumanMessage("hi"),
            AIMessage(content=""),
            AIMessage(content="Hello there."),
        ]
    }
    assert final_text(state) == "Hello there."


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


def test_thinking_blocks_are_excluded_from_text():
    """Reasoning is not user-visible; letting it into assertions makes them lie."""
    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": "the user probably means John Doe"},
            {"type": "text", "text": "Sent to John."},
        ]
    )
    assert message_text(msg) == "Sent to John."


def test_plain_string_content_passes_through():
    assert message_text(AIMessage(content="plain")) == "plain"


def test_reasoning_only_content_is_empty_not_reasoning():
    """The failure mode worth naming: a turn that only thought must read as silent."""
    msg = AIMessage(content=[{"type": "thinking", "thinking": "still deciding"}])

    assert message_text(msg) == ""


def test_multiple_text_blocks_are_joined():
    msg = AIMessage(
        content=[
            {"type": "text", "text": "Sent to John."},
            {"type": "text", "text": " And to Jane."},
        ]
    )

    assert message_text(msg) == "Sent to John. And to Jane."


def test_bare_string_blocks_are_kept():
    assert message_text(AIMessage(content=["a", "b"])) == "ab"


def test_a_non_message_is_tolerated_rather_than_raising():
    """This module prefers an empty result to an AttributeError mid-assertion."""
    assert message_text(None) == ""
    assert message_text({"not": "a message"}) == ""
    assert message_text("bare string") == "bare string"


# ---------------------------------------------------------------------------
# A2A tracking channel
# ---------------------------------------------------------------------------


def test_a2a_tracking_channel_is_returned_keyed_by_subagent():
    state = {
        "messages": [HumanMessage("go")],
        "a2a_tracking": {"slack-client": {"task_id": "t-1", "state": "completed", "is_complete": True}},
    }
    assert a2a_tracking(state)["slack-client"]["task_id"] == "t-1"


def test_a2a_tracking_absent_is_empty_not_error():
    assert a2a_tracking({"messages": []}) == {}


# ---------------------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------------------


def test_non_dict_state_is_safe():
    """A harness bug should surface as an empty result, not an AttributeError."""
    for bad in (None, "nope", 42, []):
        assert delegated_agents(bad) == []
        assert tool_calls(bad) == []
        assert final_response(bad) is None
        assert final_text(bad) == ""
        assert a2a_tracking(bad) == {}
