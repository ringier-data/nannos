"""A concurrency refusal must not shadow the owner's A2A tracking ids.

``A2ATaskTrackingMiddleware.before_model`` reads the ToolMessage at the end of
the step to persist ``task_id``/``context_id``. A refused same-agent sibling
(``DynamicToolDispatchMiddleware``) is a ``task`` ToolMessage that lands *after*
the owner's, because parallel siblings are written in ``tool_calls`` order — so
reading strictly the last message would drop the owner's metadata. When the owner
parked on ``input-required``/``auth-required``, that metadata is the only record
of the ``task_id`` the next delegation needs to resume it: losing it silently
turns "the parked task is resumable" into "the parked task is gone".
"""

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.middleware.a2a_tracking import A2ATaskTrackingMiddleware
from app.middleware.dynamic_tool_dispatch import CONCURRENT_SAME_AGENT_MESSAGE
from app.middleware.task_refusal import CONCURRENT_TASK_REFUSAL_KEY

AGENT = "github-agent"


def _issuing_message(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"subagent_type": AGENT, "description": f"work {call_id}"},
                "id": call_id,
                "type": "tool_call",
            }
            for call_id in call_ids
        ],
    )


def _owner_result(parked: bool = True) -> ToolMessage:
    return ToolMessage(
        content="Authorization needed before I can answer.",
        name="task",
        tool_call_id="call_owner",
        additional_kwargs={
            "a2a_metadata": {
                "task_id": "task-parked-1",
                "context_id": "ctx-1",
                "is_complete": not parked,
                "requires_auth": parked,
                "state": "auth-required" if parked else "completed",
            }
        },
    )


def _refusal() -> ToolMessage:
    return ToolMessage(
        content=CONCURRENT_SAME_AGENT_MESSAGE.format(agent=AGENT),
        name="task",
        tool_call_id="call_refused",
        additional_kwargs={CONCURRENT_TASK_REFUSAL_KEY: True},
    )


def _run(messages: list) -> dict | None:
    middleware = A2ATaskTrackingMiddleware()
    return middleware.before_model({"messages": messages, "a2a_tracking": {}}, MagicMock())


def test_refusal_does_not_hide_the_parked_owners_task_id():
    update = _run(
        [
            HumanMessage(content="two things"),
            _issuing_message("call_owner", "call_refused"),
            _owner_result(parked=True),
            _refusal(),
        ]
    )

    assert update is not None, "the owner's a2a_metadata was skipped entirely"
    tracking = update["a2a_tracking"][AGENT]
    # Kept because the task is parked, not complete — this is what a follow-up
    # delegation needs to resume it rather than start blank.
    assert tracking["task_id"] == "task-parked-1"
    assert tracking["context_id"] == "ctx-1"
    assert tracking["requires_auth"] is True


def test_completed_owner_still_clears_its_task_id_behind_a_refusal():
    update = _run(
        [
            HumanMessage(content="two things"),
            _issuing_message("call_owner", "call_refused"),
            _owner_result(parked=False),
            _refusal(),
        ]
    )

    assert update is not None
    tracking = update["a2a_tracking"][AGENT]
    assert "task_id" not in tracking
    assert tracking["context_id"] == "ctx-1"


def test_refusal_alone_records_nothing():
    """No owner result to read (it parked before producing one, or never ran)."""
    assert (
        _run(
            [
                HumanMessage(content="two things"),
                _issuing_message("call_refused"),
                _refusal(),
            ]
        )
        is None
    )


def test_refusal_wording_does_not_delete_the_owners_task_id():
    """The stale-task heuristic fires on a result that says a task "does not exist".

    The refusal talks about a task at length; if its wording ever drifted into
    that phrase, this middleware would delete the *owner's* live ``task_id`` and
    mark it complete — losing the parked task the guard was protecting.
    """
    content = CONCURRENT_SAME_AGENT_MESSAGE.format(agent=AGENT).lower()
    assert not ("task" in content and "does not exist" in content)
