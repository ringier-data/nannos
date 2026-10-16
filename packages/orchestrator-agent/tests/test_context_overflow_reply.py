"""A conversation that still does not fit after deepagents' compaction ends with a reply
that says so, not the generic "unexpected error … try again" (a retry cannot fit either)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from a2a.types import Part, TaskState
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import HumanMessage

from app.core.agent import OrchestratorDeepAgent
from app.models.config import AgentSettings


def _agent_whose_graph_raises(exc: Exception) -> OrchestratorDeepAgent:
    async def astream(*_args, **_kwargs):
        raise exc
        yield  # pragma: no cover — makes this an async generator

    graph = MagicMock()
    # A non-empty checkpoint skips the first-turn conversation-origin lookup.
    graph.aget_state = AsyncMock(return_value=SimpleNamespace(values={"messages": [HumanMessage("earlier")]}))
    graph.astream = astream

    agent = OrchestratorDeepAgent.__new__(OrchestratorDeepAgent)
    agent.config = AgentSettings()
    agent._default_model_type = "any-alias"
    agent._default_thinking_level = None
    agent.get_or_create_graph = AsyncMock(return_value=graph)
    agent.build_runtime_context = MagicMock()
    agent.sandbox_pool = None
    return agent


async def _replies(agent: OrchestratorDeepAgent) -> list:
    user_config = MagicMock(user_sub="user-1")
    config = {"metadata": {}, "configurable": {"thread_id": "t-overflow"}}
    return [r async for r in agent.stream([Part(text="one more question")], user_config, config=config)]


async def test_a_conversation_too_long_after_compaction_says_so():
    agent = _agent_whose_graph_raises(ContextOverflowError("Model input still does not fit after recovery"))

    replies = await _replies(agent)

    assert replies[-1].state == TaskState.TASK_STATE_FAILED
    assert "too long" in replies[-1].content
    assert "new conversation" in replies[-1].content
    assert "try again" not in replies[-1].content.lower()


async def test_any_other_failure_keeps_the_generic_reply():
    agent = _agent_whose_graph_raises(RuntimeError("boom"))

    replies = await _replies(agent)

    assert replies[-1].state == TaskState.TASK_STATE_FAILED
    assert "unexpected error" in replies[-1].content
