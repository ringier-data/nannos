"""Build a real orchestrator graph driven by a scripted model.

This is the mock tier's top end: the graph, middleware stack, dispatch path and
state channels are all production code from ``GraphFactory``; only the LLM and
the sub-agents are substituted. Tests written against it exercise wiring that
message-level unit tests cannot reach — whether ``task`` is bound with the right
``subagent_type`` enum, whether ``a2a_tracking`` actually fills, what shape
``structured_response`` really has.

Runs with no gateway and no credentials, so it belongs in the normal suite.

Usage::

    model = ScriptedChatModel(responses=[task_call("slack-notifier", "post it"),
                                         final_response("Done.")])
    slack = MockSubAgent("slack-notifier", "Sends Slack messages.")
    graph = scripted_graph(model)
    state = await graph.ainvoke(user_turn("slack john"),
                                config=turn_config(), context=runtime_context(slack))
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from app.core.graph_factory import GraphFactory
from app.core.time_tools import create_time_tool
from app.models.config import AgentSettings, GraphRuntimeContext

from .mock_subagents import MockSubAgent, mock_subagents
from .scripted_model import ScriptedChatModel

DEFAULT_MODEL_TYPE = "claude-sonnet-4.5"
DEFAULT_THREAD_ID = "test-thread"


# The private attributes both tiers substitute. Named here so `test_graph_harness`
# can fail loudly if GraphFactory renames one — the dangerous outcome is not a
# break but a silent pass against a half-initialized factory, which voids the
# whole point of compiling the real graph.
PATCHED_ATTRIBUTES = (
    "_checkpointer",
    "_store",
    "_store_setup_complete",
    "_static_tools_cache",
    "_create_model",
)


def patched_factory(
    *,
    checkpointer: Any = None,
    store: Any = None,
    model: Any = None,
) -> GraphFactory:
    """A GraphFactory with test doubles for DynamoDB and Postgres.

    One copy of this recipe, shared by the mock tier and the integration
    conftest, because it reaches past the public API: ``GraphFactory`` offers no
    injection seam, so both tiers poke the same private attributes and a second
    copy would drift from this one.

    ``model``, when given, shadows ``_create_model`` on the *instance* rather
    than the class, so the swap needs no fixture and cannot leak between tests.
    """
    factory = GraphFactory(config=AgentSettings(), cost_logger=None)
    factory._checkpointer = MemorySaver() if checkpointer is None else checkpointer
    factory._store = InMemoryStore() if store is None else store
    factory._store_setup_complete = True
    factory._static_tools_cache = [create_time_tool()]
    if model is not None:
        factory._create_model = lambda *_args, **_kwargs: model  # type: ignore[method-assign]
    return factory


def scripted_graph(
    model: ScriptedChatModel,
    *,
    model_type: str = DEFAULT_MODEL_TYPE,
    thinking_level: Any = None,
):
    """A real compiled orchestrator graph whose LLM is ``model``.

    The model type only steers provider-shaped decisions (structured-output
    strategy, prompt caching); no model of that name is ever contacted.

    Compiled per call, deliberately. It costs ~21ms, and the alternative — one
    module-scoped graph with a per-test reset — would mean swapping the scripted
    model on shared state, which is the cross-test leakage this tier exists to
    rule out.
    """
    return patched_factory(model=model)._create_graph(model_type, thinking_level)


def runtime_context(*agents: MockSubAgent, **overrides: Any) -> GraphRuntimeContext:
    """A GraphRuntimeContext with *agents* registered and routable."""
    fields: dict[str, Any] = {
        "user_id": "test-user",
        "user_sub": "test-sub",
        "name": "Test User",
        "email": "test@local",
        "subagent_registry": {a["name"]: a for a in mock_subagents(*agents)},
    }
    fields.update(overrides)
    return GraphRuntimeContext(**fields)


def turn_config(thread_id: str = DEFAULT_THREAD_ID) -> dict[str, Any]:
    """Graph config for one conversation thread.

    Reuse the same ``thread_id`` across invocations to build genuine multi-turn
    history through the checkpointer.
    """
    return {"configurable": {"thread_id": thread_id}}


def user_turn(text: str) -> dict[str, Any]:
    """Graph input for a user message."""
    return {"messages": [HumanMessage(text)]}


# ---------------------------------------------------------------------------
# Scripted model responses
# ---------------------------------------------------------------------------


def task_call(subagent: str, description: str = "do the thing", call_id: str = "call-task") -> AIMessage:
    """A model turn that delegates to *subagent*."""
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


def tool_call(name: str, args: dict[str, Any] | None = None, call_id: str = "call-tool") -> AIMessage:
    """A model turn that calls an ordinary orchestrator tool."""
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args or {}, "type": "tool_call"}],
    )


def final_response(
    message: str = "Done.",
    *,
    task_state: str = "completed",
    include_subagent_output: bool = False,
    call_id: str = "call-final",
) -> AIMessage:
    """A model turn that closes the turn via the FinalResponseSchema envelope."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "FinalResponseSchema",
                "args": {
                    "task_state": task_state,
                    "message": message,
                    "include_subagent_output": include_subagent_output,
                },
                "type": "tool_call",
            }
        ],
    )
