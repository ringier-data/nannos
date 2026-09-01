"""Every sub-agent result of a step is tracked, not just the trailing message.

``A2ATaskTrackingMiddleware.before_model`` persists ``task_id``/``context_id`` from
the ToolMessages a step wrote. It used to read ``messages[-1]`` alone, which loses
one of these two shapes:

- **parallel delegation to two different agents** — both return in the same step,
  in ``tool_calls`` order, so the earlier agent's ids were dropped. If that agent
  parked on ``input-required``/``auth-required``, the ``task_id`` needed to resume
  it was gone and the next delegation to it started blank;
- **a concurrency refusal** (``DynamicToolDispatchMiddleware``) — a ``task``
  ToolMessage that always lands last and carries no metadata, so it shadowed the
  owner's.

Both are now handled by walking the trailing ToolMessages and folding every real
result into one update, keyed by ``subagent_type``.
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


class TestParallelDifferentAgents:
    """Every result of the step is tracked, not just the last one.

    Two *different* agents delegated in parallel both return in the same step, in
    ``tool_calls`` order. Reading only the trailing ToolMessage recorded the last
    one and silently dropped the earlier agent's ids — so if that agent parked on
    ``input-required``/``auth-required``, the ``task_id`` needed to resume it was
    gone and the next delegation started blank.
    """

    @staticmethod
    def _result(call_id: str, agent: str, task_id: str, parked: bool) -> ToolMessage:
        return ToolMessage(
            content=f"{agent} says something",
            name="task",
            tool_call_id=call_id,
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": task_id,
                    "context_id": f"ctx-{agent}",
                    "is_complete": not parked,
                    "requires_input": parked,
                    "state": "input-required" if parked else "completed",
                }
            },
        )

    @staticmethod
    def _two_agent_message() -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"subagent_type": "jira-agent", "description": "open ticket"},
                    "id": "call_jira",
                    "type": "tool_call",
                },
                {
                    "name": "task",
                    "args": {"subagent_type": AGENT, "description": "who am I"},
                    "id": "call_github",
                    "type": "tool_call",
                },
            ],
        )

    def test_both_agents_are_recorded(self):
        update = _run(
            [
                HumanMessage(content="two agents"),
                self._two_agent_message(),
                self._result("call_jira", "jira-agent", "task-jira-1", parked=True),
                self._result("call_github", AGENT, "task-github-1", parked=False),
            ]
        )

        assert update is not None
        tracking = update["a2a_tracking"]
        # The earlier sibling is the one that used to be lost.
        assert tracking["jira-agent"]["task_id"] == "task-jira-1"
        assert tracking["jira-agent"]["requires_input"] is True
        assert tracking[AGENT]["context_id"] == f"ctx-{AGENT}"
        assert "task_id" not in tracking[AGENT]  # completed, so cleared as before

    def test_a_refusal_between_results_does_not_stop_the_walk_back(self):
        update = _run(
            [
                HumanMessage(content="two agents plus a duplicate"),
                self._two_agent_message(),
                self._result("call_jira", "jira-agent", "task-jira-1", parked=True),
                self._result("call_github", AGENT, "task-github-1", parked=True),
                _refusal(),
            ]
        )

        assert update is not None
        assert update["a2a_tracking"]["jira-agent"]["task_id"] == "task-jira-1"
        assert update["a2a_tracking"][AGENT]["task_id"] == "task-github-1"

    def test_records_in_state_are_not_mutated(self):
        """The update must be a copy; LangGraph merges it, state is not ours to edit."""
        middleware = A2ATaskTrackingMiddleware()
        existing = {AGENT: {"task_id": "task-old", "context_id": "ctx-old"}}
        state = {
            "messages": [
                HumanMessage(content="one agent"),
                self._two_agent_message(),
                self._result("call_github", AGENT, "task-github-1", parked=True),
            ],
            "a2a_tracking": existing,
        }

        update = middleware.before_model(state, MagicMock())

        assert update["a2a_tracking"][AGENT]["task_id"] == "task-github-1"
        assert existing[AGENT]["task_id"] == "task-old", "the record held in state was mutated in place"


def test_refusal_wording_does_not_delete_the_owners_task_id():
    """The stale-task heuristic fires on a result that says a task "does not exist".

    The refusal talks about a task at length; if its wording ever drifted into
    that phrase, this middleware would delete the *owner's* live ``task_id`` and
    mark it complete — losing the parked task the guard was protecting.
    """
    content = CONCURRENT_SAME_AGENT_MESSAGE.format(agent=AGENT).lower()
    assert not ("task" in content and "does not exist" in content)
