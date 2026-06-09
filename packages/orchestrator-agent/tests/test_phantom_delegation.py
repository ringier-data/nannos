"""StreamHandler.is_phantom_subagent_completion — detects an 'eager completion'
where the model sets include_subagent_output=true but never called `task`.

The executor uses this to re-enter the graph with a corrective nudge instead of
surfacing an empty completion (no `task` ToolMessage means there is no sub-agent
output to append).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.handlers import StreamHandler


def _ai_task_call(call_id: str = "c1", subagent: str = "test-agent") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "task", "args": {"subagent_type": subagent}, "type": "tool_call"}],
    )


def test_phantom_when_flag_set_but_no_task_call():
    state = {
        "structured_response": {"include_subagent_output": True, "task_state": "completed", "message": ""},
        "messages": [
            HumanMessage("convert this pdf"),
            AIMessage(content=[{"type": "text", "text": '{"task_state":"completed","include_subagent_output":true}'}]),
        ],
    }
    assert StreamHandler.is_phantom_subagent_completion(state) is True


def test_not_phantom_when_subagent_actually_ran():
    state = {
        "structured_response": {"include_subagent_output": True, "task_state": "completed", "message": ""},
        "messages": [
            HumanMessage("convert this pdf"),
            _ai_task_call("c1"),
            ToolMessage(content="# markdown result", tool_call_id="c1", name="task"),
        ],
    }
    assert StreamHandler.is_phantom_subagent_completion(state) is False


def test_not_phantom_when_flag_not_set():
    state = {
        "structured_response": {"include_subagent_output": False, "task_state": "completed", "message": "done"},
        "messages": [HumanMessage("hi")],
    }
    assert StreamHandler.is_phantom_subagent_completion(state) is False


def test_current_turn_final_response_tool_call_wins_over_stale_channel():
    """A FinalResponseSchema tool call in the current turn takes precedence over a
    possibly-stale ``structured_response`` channel."""
    state = {
        "structured_response": {"include_subagent_output": False, "task_state": "completed", "message": "stale"},
        "messages": [
            HumanMessage("convert this pdf"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "f1",
                        "name": "FinalResponseSchema",
                        "args": {"include_subagent_output": True, "task_state": "completed", "message": ""},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
    }
    assert StreamHandler.is_phantom_subagent_completion(state) is True


def test_non_dict_state_is_safe():
    assert StreamHandler.is_phantom_subagent_completion(None) is False
    assert StreamHandler.is_phantom_subagent_completion("nope") is False
