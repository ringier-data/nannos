"""Every sub-agent result of a step is tracked, not just the trailing message.

``A2ATaskTrackingMiddleware.before_model`` persists ``task_id``/``context_id`` from
the ToolMessages a step wrote. It used to read ``messages[-1]`` alone, which loses
**parallel delegation to two different agents** — both return in the same step, in
``tool_calls`` order, so the earlier agent's ids were dropped. If that agent parked
on ``input-required``/``auth-required``, the ``task_id`` needed to resume it was
gone and the next delegation to it started blank.

Handled by walking the trailing ToolMessages and folding every result into one
update, keyed by ``subagent_type``. Two delegations to the SAME agent in one step
are ordinary results too now (each is its own A2A task; the sub-agent's in-process
server runs them one after the other), so the last one written wins the record.
"""

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.middleware.a2a_tracking import A2ATaskTrackingMiddleware

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


def _run(messages: list) -> dict | None:
    middleware = A2ATaskTrackingMiddleware()
    return middleware.before_model({"messages": messages, "a2a_tracking": {}}, MagicMock())


def test_two_tasks_for_one_agent_in_a_step_leave_the_later_record():
    """Same agent twice in one step: both are real results; the later one is the record.

    A parked earlier task is not lost to the model — its ToolMessage carries the
    ids — but the tracking record, which names the task a follow-up continues,
    follows the most recent result.
    """
    second = ToolMessage(
        content="Listed 3 repos.",
        name="task",
        tool_call_id="call_second",
        additional_kwargs={
            "a2a_metadata": {"task_id": "task-2", "context_id": "ctx-1", "is_complete": True, "state": "completed"}
        },
    )
    update = _run(
        [
            HumanMessage(content="two things"),
            _issuing_message("call_owner", "call_second"),
            _owner_result(parked=True),
            second,
        ]
    )

    assert update is not None
    tracking = update["a2a_tracking"][AGENT]
    assert tracking["context_id"] == "ctx-1"
    assert "task_id" not in tracking  # the later task completed, so nothing is left to continue


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


class TestStaleTaskPhraseIsOnlyAHeuristic:
    """"task … does not exist" in a result's text is a guess, not an error code.

    It exists to break retry loops when a sub-agent has cleaned up a task the
    orchestrator still holds an id for. But a genuine answer can contain the
    phrase — *"that task does not exist — did you mean X?"* is a natural
    ``input-required`` park — so when there is no stale ``task_id`` to clear, the
    result must still be read for the ids it carries. Returning early there loses
    the ``task_id`` of a first-delegation park: the task cannot be resumed, and
    the ``context_id`` goes with it, restarting the sub-agent conversation.
    """

    @staticmethod
    def _parked_result_mentioning_a_missing_task() -> ToolMessage:
        return ToolMessage(
            content="That task does not exist — did you mean PROJ-123? Tell me and I'll continue.",
            name="task",
            tool_call_id="call_owner",
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": "task-parked-1",
                    "context_id": "ctx-1",
                    "is_complete": False,
                    "requires_input": True,
                    "state": "input-required",
                }
            },
        )

    def test_ids_are_recorded_when_there_is_nothing_stale_to_clear(self):
        update = _run(
            [
                HumanMessage(content="close that ticket"),
                _issuing_message("call_owner"),
                self._parked_result_mentioning_a_missing_task(),
            ]
        )

        assert update is not None, "the phrase suppressed a real result"
        tracking = update["a2a_tracking"][AGENT]
        assert tracking["task_id"] == "task-parked-1"
        assert tracking["context_id"] == "ctx-1"
        assert tracking["requires_input"] is True

    def test_a_stale_id_is_still_cleared_and_the_metadata_left_alone(self):
        """The loop-breaker still wins when there IS a stale id: clear and stop."""
        middleware = A2ATaskTrackingMiddleware()
        state = {
            "messages": [
                HumanMessage(content="close that ticket"),
                _issuing_message("call_owner"),
                self._parked_result_mentioning_a_missing_task(),
            ],
            "a2a_tracking": {AGENT: {"task_id": "task-stale", "context_id": "ctx-old"}},
        }

        update = middleware.before_model(state, MagicMock())

        tracking = update["a2a_tracking"][AGENT]
        assert "task_id" not in tracking, "the stale id must be cleared to break the retry loop"
        assert tracking["is_complete"] is True
        assert tracking["context_id"] == "ctx-old"
