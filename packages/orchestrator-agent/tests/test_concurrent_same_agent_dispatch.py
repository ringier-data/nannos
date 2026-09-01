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
- the verdict itself: the parallel case that must keep working (different
  agents), the sequential case that must not be touched, and the agents whose
  thread this middleware does not own (left to deepagents' own answers);
- fail-*closed* on malformed history, since a silently inactive guard is the one
  outcome worse than a false refusal;
- determinism across a resume replay, which is what stops a refusal from
  flipping into a second execution mid-turn;
- the refusal message reaching the model instead of a dispatch, on both the sync
  and async tool-call paths, and its tag, without which downstream consumers read
  it as the sub-agent's answer (``test_stream_handler.py``,
  ``test_a2a_tracking_step_results.py``).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.middleware.dynamic_tool_dispatch import (
    CONCURRENT_SAME_AGENT_MESSAGE,
    DynamicToolDispatchMiddleware,
    surplus_same_agent_call,
)
from app.middleware.task_refusal import is_concurrent_task_refusal
from app.models.config import GraphRuntimeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Agents whose checkpoint thread the middleware owns (stand-in registry).
KNOWN_AGENTS = ("general-purpose", "github-agent", "jira-agent")


def verdict(state, tool_call, known_agents=KNOWN_AGENTS):
    """``surplus_same_agent_call`` with the registry defaulted for brevity."""
    return surplus_same_agent_call(state, tool_call, known_agents)


def _task_call(call_id, subagent_type: str, description: str = "do the thing") -> dict:
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
        subagent_registry={name: {"description": name} for name in KNOWN_AGENTS},
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
        assert verdict(_state(call), call) is None

    def test_second_call_to_same_agent_is_refused(self):
        first = _task_call("tc-1", "general-purpose", "who am I on GitHub")
        second = _task_call("tc-2", "general-purpose", "list campaigns")
        state = _state(first, second)

        assert verdict(state, first) is None
        assert verdict(state, second) == "tc-1"

    def test_third_call_is_refused_too(self):
        calls = [_task_call(f"tc-{i}", "general-purpose") for i in range(1, 4)]
        state = _state(*calls)

        assert [verdict(state, c) for c in calls] == [None, "tc-1", "tc-1"]

    def test_different_agents_in_parallel_both_run(self):
        """The parallelism worth having: this must not regress."""
        github = _task_call("tc-1", "github-agent")
        jira = _task_call("tc-2", "jira-agent")
        state = _state(github, jira)

        assert verdict(state, github) is None
        assert verdict(state, jira) is None

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

        assert verdict(state, later) is None

    def test_other_tools_alongside_are_ignored(self):
        task = _task_call("tc-2", "general-purpose")
        state = _state(
            {"id": "tc-1", "name": "write_todos", "args": {}},
            task,
            {"id": "tc-3", "name": "write_todos", "args": {}},
        )

        assert verdict(state, task) is None


class TestAgentsWeDoNotOwn:
    """Only registry agents have a ``{conversation}::{agent}`` thread to protect.

    Anything else falls through to ``SubAgentMiddleware``, which runs its
    sub-agent inline against the parent's config — nothing shared to corrupt. And
    a typo'd name must reach deepagents' "does not exist, the only allowed types
    are […]", which teaches the model the real names, instead of being told that
    a non-existent agent is busy and must not be reported as unavailable.
    """

    def test_unknown_agent_is_left_to_fall_through(self):
        first = _task_call("tc-1", "genral-purpose")
        second = _task_call("tc-2", "genral-purpose")
        state = _state(first, second)

        assert verdict(state, first) is None
        assert verdict(state, second) is None

    def test_empty_registry_refuses_nothing(self):
        first = _task_call("tc-1", "general-purpose")
        second = _task_call("tc-2", "general-purpose")
        state = _state(first, second)

        assert verdict(state, second, known_agents=()) is None
        assert verdict(state, second, known_agents=None) is None


class TestFailsClosed:
    """A guard that quietly stops guarding is worse than one that over-refuses."""

    def test_owner_without_an_id_still_owns(self):
        """Ownership is positional; comparing a ``None`` owner id would allow both.

        ``ToolCall["id"]`` is optional (streaming reassembly, synthetic history),
        and the id-comparison this replaced returned "allowed" for *every* sibling
        when the first one had no id — disabling the guard exactly when the history
        is malformed.
        """
        first = _task_call(None, "general-purpose")
        second = _task_call("tc-2", "general-purpose")
        state = _state(first, second)

        assert verdict(state, second) == "<no id>"
        assert verdict(state, first) is None  # unreadable argument, dispatch reports it

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
        assert verdict(state, tool_call) is None

    def test_missing_messages_is_not_refused(self):
        call = _task_call("tc-1", "general-purpose")
        assert verdict({}, call) is None

    def test_unreadable_state_warns(self, caplog):
        """A state shape the guard cannot read must be visible, not silent.

        Every test here hands it a dict, so a future state schema that breaks the
        read would otherwise leave the guard inactive with nothing in the log.
        """
        call = _task_call("tc-1", "general-purpose")
        with caplog.at_level("WARNING"):
            assert verdict(["not", "a", "mapping"], call) is None
        assert "concurrency guard inactive" in caplog.text

    def test_call_absent_from_history_is_not_refused(self):
        """No issuing message to judge by (a hand-built or replayed-away call)."""
        call = _task_call("tc-9", "general-purpose")
        state = _state(_task_call("tc-1", "general-purpose"))
        assert verdict(state, call) is None


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

        assert verdict(attempt, first) == verdict(replay, first)
        assert verdict(attempt, second) == verdict(replay, second)
        assert verdict(replay, second) == "tc-1"

    def test_verdict_ignores_sibling_order_in_args(self):
        """Identical descriptions still resolve to one owner, by call id."""
        first = _task_call("tc-1", "general-purpose", "same text")
        second = _task_call("tc-2", "general-purpose", "same text")
        state = _state(first, second)

        assert verdict(state, first) is None
        assert verdict(state, second) == "tc-1"


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

    def test_is_tagged_so_consumers_can_tell_it_apart(self):
        """Untagged, this is indistinguishable from a delegation result — and it
        lands last, so "the latest task result" resolves to it."""
        msg = DynamicToolDispatchMiddleware._refuse_concurrent_same_agent(
            _task_call("tc-2", "github-agent"), "tc-1"
        )
        assert is_concurrent_task_refusal(msg)
        assert not is_concurrent_task_refusal(ToolMessage(content="real answer", name="task", tool_call_id="tc-1"))

    def test_does_not_trip_the_stale_task_heuristic(self):
        """``A2ATaskTrackingMiddleware`` deletes a live ``task_id`` when a result
        says a task "does not exist". The wording must never look like that."""
        content = CONCURRENT_SAME_AGENT_MESSAGE.format(agent="github-agent").lower()
        assert "does not exist" not in content


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
        assert is_concurrent_task_refusal(result)
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
        assert is_concurrent_task_refusal(result)
        dispatch.assert_not_called()
        handler.assert_not_called()
