"""Graph-level: a gated call inside ``eval`` is never labelled "Running …" unless it runs.

On an approval resume the guard re-runs the ``eval`` snippet to rediscover its pending
calls, and again once the decisions are applied. ``ToolStatusMiddleware`` sits inside the
code-interpreter middleware, so each of those runs used to emit "Running <tool>…" — before
the user's typed refusal was even read, telling them a call ran that never did. Only a real
graph with a real checkpointer, interrupt and QuickJS shows it.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.factory import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_common.core import hitl_resume, tool_call_summarizer
from agent_common.core.graph_utils import _PTCToleranceCodeInterpreterMiddleware
from agent_common.middleware.ptc_guard import wrap_tool_for_ptc
from agent_common.middleware.tool_status import TOOL_STATUS_EVENT, ToolStatusMiddleware

GATED = "scheduler_delete_job"


class Answer(BaseModel):
    text: str = Field(description="the reply")


class _JobArgs(BaseModel):
    job_id: str = Field(description="job")


class _EvalModel(BaseChatModel):
    """Deletes a job from inside ``eval``; answers once it has read the result."""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> _EvalModel:
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs):
        self.calls += 1
        assert self.calls < 10, "runaway loop"
        if isinstance(messages[-1], HumanMessage):
            code = 'const r = await tools.schedulerDeleteJob({job_id: "j1"});\nr'
            calls = [{"name": "eval", "args": {"code": code}, "id": "ev-1"}]
        else:
            calls = [{"name": "Answer", "args": {"text": "done"}, "id": "ans"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])


async def _risky(tool_name, args, *, tool=None, cache=None, server_slug="_self"):
    return 0.95, None


def _build(executions: list):
    async def _delete(job_id: str) -> str:
        executions.append(job_id)
        return "deleted"

    inner = StructuredTool.from_function(coroutine=_delete, name=GATED, description="delete", args_schema=_JobArgs)
    ptc = _PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[wrap_tool_for_ptc(inner, risk_scorer=_risky, default_risk_threshold=0.8)],
        broaden_exposure=False,
        risk_scorer=_risky,
        default_risk_threshold=0.8,
    )
    # The production order: the code interpreter OUTSIDE the status middleware.
    return create_agent(
        model=_EvalModel(),
        tools=[],
        middleware=[ptc, ToolStatusMiddleware()],
        response_format=ToolStrategy(Answer),
        checkpointer=InMemorySaver(),
    )


async def _statuses(agent, payload, config) -> list[str]:
    out: list[str] = []
    async for chunk in agent.astream(payload, config, stream_mode="custom"):
        if isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == TOOL_STATUS_EVENT:
            out.append(chunk[1]["status"])
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize(("intent", "ran"), [("reject", False), ("approve", True)])
async def test_a_typed_reply_labels_the_gated_call_only_if_it_runs(monkeypatch, intent, ran):
    async def _classify(reply, action_requests, *, question=None):
        return intent

    async def _no_summaries(*args, **kwargs):
        return None

    monkeypatch.setattr(hitl_resume, "classify_reply", _classify)
    monkeypatch.setattr(tool_call_summarizer, "attach_summaries", _no_summaries)
    executions: list = []
    agent = _build(executions)
    config = {"configurable": {"thread_id": f"ptc-replay-{intent}"}}

    first = await _statuses(agent, {"messages": [HumanMessage(content="delete job j1")]}, config)
    # The fresh run is labelled as it always was, and parks on the approval.
    assert first == [f"Running {GATED}…"]
    assert (await agent.aget_state(config)).interrupts

    reply = "No wait, keep it" if intent == "reject" else "yes go ahead"
    resumed = await _statuses(agent, Command(resume=reply), config)

    assert executions == (["j1"] if ran else [])
    running = [s for s in resumed if GATED in s]
    # A refused call is never announced; an approved one exactly once.
    assert running == ([f"Running {GATED}…"] if ran else [])
