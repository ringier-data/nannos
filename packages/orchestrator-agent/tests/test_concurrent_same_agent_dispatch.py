"""One live task per sub-agent: the concurrency guard on ``task`` dispatch.

Two ``task`` calls to the same sub-agent in one assistant message run on one
LangGraph checkpoint thread (``{conversation}::{agent}``), so their writes
interleave and the loser's conversation is overwritten — a "who am I on GitHub"
delegation came back answering about ad campaigns because it resumed on the
campaign delegation's state, with its authorization prompt lost on the way.

Isolating the threads would not be enough (neither the client's authorization
answer nor a later ``task`` call can say WHICH of two parked tasks it means), so
the second concurrent call is refused instead. See the commentary above
``surplus_same_agent_call``.

Covers:
- the verdict itself, including the parallel case that must keep working
  (different agents) and the sequential case that must not be touched;
- determinism across a resume replay, which is what stops a refusal from
  flipping into a second execution mid-turn;
- the refusal message reaching the model instead of a dispatch, on both the sync
  and async tool-call paths.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.middleware.dynamic_tool_dispatch import (
    DynamicToolDispatchMiddleware,
    surplus_same_agent_call,
)
from app.models.config import GraphRuntimeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_call(call_id: str, subagent_type: str, description: str = "do the thing") -> dict:
    return {
        "id": call_id,
        "name": "task",
        "args": {"subagent_type": subagent_type, "description": description},
    }


def _state(*tool_calls: dict, extra_messages: list | None = None) -> dict:
    """State whose last assistant message issued ``tool_calls``."""
    return {
        "messages": [
            HumanMessage(content="do two things"),
            *(extra_messages or []),
            AIMessage(content="", tool_calls=list(tool_calls)),
        ]
    }


def _context() -> GraphRuntimeContext:
    return GraphRuntimeContext(
        user_id="u1",
        user_sub="sub1",
        name="Test",
        email="test@example.com",
        subagent_registry={},
    )


def _request(state: dict, tool_call: dict) -> MagicMock:
    request = MagicMock()
    request.tool_call = tool_call
    request.runtime.state = state
    request.runtime.context = _context()
    request.runtime.config = {"configurable": {"thread_id": "conv-1"}}
    return request


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class TestSurplusSameAgentCall:
    def test_lone_call_runs(self):
        call = _task_call("tc-1", "general-purpose")
        assert surplus_same_agent_call(_state(call), call) is None

    def test_second_call_to_same_agent_is_refused(self):
        first = _task_call("tc-1", "general-purpose", "who am I on GitHub")
        second = _task_call("tc-2", "general-purpose", "list campaigns")
        state = _state(first, second)

        assert surplus_same_agent_call(state, first) is None
        assert surplus_same_agent_call(state, second) == "tc-1"

    def test_third_call_is_refused_too(self):
        calls = [_task_call(f"tc-{i}", "general-purpose") for i in range(1, 4)]
        state = _state(*calls)

        verdicts = [surplus_same_agent_call(state, c) for c in calls]
        assert verdicts == [None, "tc-1", "tc-1"]

    def test_different_agents_in_parallel_both_run(self):
        """The parallelism worth having: this must not regress."""
        github = _task_call("tc-1", "github-agent")
        jira = _task_call("tc-2", "jira-agent")
        state = _state(github, jira)

        assert surplus_same_agent_call(state, github) is None
        assert surplus_same_agent_call(state, jira) is None

    def test_same_agent_in_separate_messages_both_run(self):
        """Sequential re-delegation is the continuity path, not a collision."""
        earlier = _task_call("tc-1", "general-purpose")
        later = _task_call("tc-2", "general-purpose")
        state = _state(
            later,
            extra_messages=[
                AIMessage(content="", tool_calls=[earlier]),
                ToolMessage(content="done", name="task", tool_call_id="tc-1"),
            ],
        )

        assert surplus_same_agent_call(state, later) is None

    def test_other_tools_alongside_are_ignored(self):
        task = _task_call("tc-2", "general-purpose")
        state = _state(
            {"id": "tc-1", "name": "write_todos", "args": {}},
            task,
            {"id": "tc-3", "name": "write_todos", "args": {}},
        )

        assert surplus_same_agent_call(state, task) is None

    @pytest.mark.parametrize(
        "tool_call",
        [
            {"id": "tc-1", "name": "task", "args": {}},
            {"id": "tc-1", "name": "task"},
            {"name": "task", "args": {"subagent_type": "general-purpose"}},
        ],
        ids=["no-subagent-type", "no-args", "no-id"],
    )
    def test_unreadable_call_is_not_refused(self, tool_call):
        """A malformed call is dispatch's problem to report, not ours to swallow.

        State holds a well-formed call because ``AIMessage`` will not accept these
        shapes — they can only arrive as the argument, so that is where they are fed.
        """
        state = _state(_task_call("tc-1", "general-purpose"))
        assert surplus_same_agent_call(state, tool_call) is None

    def test_missing_messages_is_not_refused(self):
        call = _task_call("tc-1", "general-purpose")
        assert surplus_same_agent_call({}, call) is None

    def test_call_absent_from_history_is_not_refused(self):
        """No issuing message to judge by (a hand-built or replayed-away call)."""
        call = _task_call("tc-9", "general-purpose")
        state = _state(_task_call("tc-1", "general-purpose"))
        assert surplus_same_agent_call(state, call) is None


class TestVerdictSurvivesReplay:
    """A resume replays the same assistant message, so the verdict cannot flip.

    This is the property that matters after an authorization interrupt: the
    refused sibling must still be refused on the replay, or the turn quietly
    grows a second execution on the thread the guard exists to protect.
    """

    def test_same_verdicts_on_replay(self):
        first = _task_call("tc-1", "general-purpose", "who am I on GitHub")
        second = _task_call("tc-2", "general-purpose", "list campaigns")
        issuing = AIMessage(content="", tool_calls=[first, second])

        attempt = {"messages": [HumanMessage(content="both please"), issuing]}
        # The replay carries the same assistant message with the refusal already
        # answered — LangGraph re-executes the step, it does not rebuild it.
        replay = {
            "messages": [
                HumanMessage(content="both please"),
                issuing,
                ToolMessage(content="refused", name="task", tool_call_id="tc-2"),
            ]
        }

        assert surplus_same_agent_call(attempt, first) == surplus_same_agent_call(replay, first)
        assert surplus_same_agent_call(attempt, second) == surplus_same_agent_call(replay, second)
        assert surplus_same_agent_call(replay, second) == "tc-1"

    def test_verdict_ignores_sibling_order_in_args(self):
        """Identical descriptions still resolve to one owner, by call id."""
        first = _task_call("tc-1", "general-purpose", "same text")
        second = _task_call("tc-2", "general-purpose", "same text")
        state = _state(first, second)

        assert surplus_same_agent_call(state, first) is None
        assert surplus_same_agent_call(state, second) == "tc-1"


# ---------------------------------------------------------------------------
# The refusal the model reads
# ---------------------------------------------------------------------------


class TestRefusalMessage:
    def test_names_the_agent_and_forbids_retry(self):
        call = _task_call("tc-2", "github-agent")
        msg = DynamicToolDispatchMiddleware._refuse_concurrent_same_agent(call, "tc-1")

        assert msg.tool_call_id == "tc-2"
        assert msg.name == "task"
        assert "github-agent" in msg.content
        assert "NOT executed" in msg.content
        assert "Do not retry" in msg.content

    def test_is_not_an_error(self):
        """``status="error"`` would invite a bug report for working-as-designed."""
        msg = DynamicToolDispatchMiddleware._refuse_concurrent_same_agent(
            _task_call("tc-2", "github-agent"), "tc-1"
        )
        assert getattr(msg, "status", "success") != "error"
        assert "[ERROR_TYPE" not in msg.content


# ---------------------------------------------------------------------------
# Wired into both tool-call paths, ahead of dispatch
# ---------------------------------------------------------------------------


class TestGuardShortCircuitsDispatch:
    @pytest.fixture
    def middleware(self):
        return DynamicToolDispatchMiddleware()

    @pytest.mark.asyncio
    async def test_async_refuses_without_dispatching(self, middleware, monkeypatch):
        dispatch = AsyncMock()
        monkeypatch.setattr(middleware, "_adispatch_task_tool", dispatch)
        handler = AsyncMock()

        first = _task_call("tc-1", "general-purpose")
        second = _task_call("tc-2", "general-purpose")
        request = _request(_state(first, second), second)

        result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "tc-2"
        assert "Do not retry" in result.content
        # Neither the registry dispatch nor the built-in fallback ran.
        dispatch.assert_not_called()
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_still_dispatches_the_owner(self, middleware, monkeypatch):
        expected = ToolMessage(content="ok", name="task", tool_call_id="tc-1")
        dispatch = AsyncMock(return_value=expected)
        monkeypatch.setattr(middleware, "_adispatch_task_tool", dispatch)

        first = _task_call("tc-1", "general-purpose")
        second = _task_call("tc-2", "general-purpose")
        request = _request(_state(first, second), first)

        result = await middleware.awrap_tool_call(request, AsyncMock())

        assert result is expected
        dispatch.assert_awaited_once()

    def test_sync_refuses_without_dispatching(self, middleware, monkeypatch):
        dispatch = MagicMock()
        monkeypatch.setattr(middleware, "_dispatch_task_tool", dispatch)
        handler = MagicMock()

        first = _task_call("tc-1", "general-purpose")
        second = _task_call("tc-2", "general-purpose")
        request = _request(_state(first, second), second)

        result = middleware.wrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "tc-2"
        dispatch.assert_not_called()
        handler.assert_not_called()
