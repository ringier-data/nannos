"""The turn budget: model calls in, LangGraph super-steps out.

The reported bug was that `MAX_RECURSION_LIMIT=50` capped a turn at six model
calls, because the limit counts super-steps and every middleware hook is its own
node. The fix expresses the budget in model calls and derives the multiplier from
the compiled graph.

These tests exist to stop the derivation going quietly wrong, which is the only
way this bug can come back. Two of them do real work:

- `test_marginal_cost_of_a_model_call_matches_the_derivation` measures the
  super-steps of an actual graph run and compares them against what
  `step_budget` computed from the node names. If LangGraph renames a hook, or
  starts running one twice per cycle, the measurement and the derivation diverge
  and this fails.
- `test_every_node_is_classified` fails when the graph grows a node shape the
  derivation does not understand, rather than silently mis-counting it.

Everything here runs offline: no gateway, no credentials, no model.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field, PrivateAttr

from app.core.graph_factory import GraphFactory
from app.core.step_budget import (
    base_steps,
    classify_nodes,
    recursion_limit_for,
    steps_per_model_call,
)
from app.core.time_tools import create_time_tool
from app.models.config import (
    DEFAULT_MAX_MODEL_CALLS_PER_TURN,
    LEGACY_RECURSION_LIMIT_ENV,
    MAX_MODEL_CALLS_PER_TURN_ENV,
    AgentSettings,
    GraphRuntimeContext,
    _resolve_max_model_calls_per_turn,
)

MODEL_TYPE = "claude-sonnet-4.5"


class _ScriptedModel(BaseChatModel):
    """Replays canned turns. Deliberately local to this module.

    langchain's stock fakes inherit a `bind_tools` that raises
    NotImplementedError, and the orchestrator graph always binds tools, so a turn
    dies before it starts. This is the minimum needed to drive a real graph and
    count its steps.
    """

    responses: list[AIMessage] = Field(default_factory=list)
    _cursor: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-step-budget"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._cursor >= len(self.responses):
            raise AssertionError(
                f"scripted model exhausted after {self._cursor} calls; the turn took an unexpected path"
            )
        message = self.responses[self._cursor]
        self._cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:
        return self


def _compiled_graph(model: BaseChatModel | None = None):
    """A real compiled orchestrator graph with test doubles for the stores.

    Reaches past the public API because GraphFactory offers no injection seam.
    Only the persistence layer and the model are substituted; the middleware
    stack, and therefore the node count this module measures, is production's.

    A model is always substituted, even for the tests that never run a turn:
    `_create_graph` builds one eagerly, and the real factory refuses without
    `LLM_GATEWAY_URL` — which would make these tests need a gateway to count
    nodes.
    """
    factory = GraphFactory(config=AgentSettings(), cost_logger=None)
    factory._checkpointer = MemorySaver()
    factory._store = InMemoryStore()
    factory._store_setup_complete = True
    factory._static_tools_cache = [create_time_tool()]
    factory._create_model = lambda *_a, **_k: model or _ScriptedModel(responses=[])  # type: ignore[method-assign]
    return factory._create_graph(MODEL_TYPE, None)


def _runtime_context() -> GraphRuntimeContext:
    return GraphRuntimeContext(
        user_id="test-user",
        user_sub="test-sub",
        name="Test User",
        email="test@local",
        tool_registry={},
        subagent_registry={},
    )


def _tool_turn(index: int) -> AIMessage:
    """A model turn that calls a real static tool, so the cycle enters `tools`."""
    return AIMessage(
        content="",
        tool_calls=[{"id": f"c{index}", "name": "get_current_time", "args": {}, "type": "tool_call"}],
    )


def _final_turn() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "final",
                "name": "FinalResponseSchema",
                "args": {"task_state": "completed", "message": "Done.", "include_subagent_output": False},
                "type": "tool_call",
            }
        ],
    )


async def _measure_super_steps(model_calls: int) -> int:
    """Run a turn spending exactly *model_calls* model calls; count super-steps.

    `stream_mode="updates"` yields once per node execution, which is what
    `recursion_limit` counts.
    """
    script = [_tool_turn(i) for i in range(model_calls - 1)] + [_final_turn()]
    graph = _compiled_graph(_ScriptedModel(responses=script))

    steps = 0
    async for _ in graph.astream(
        {"messages": [HumanMessage("go")]},
        config={"configurable": {"thread_id": f"budget-{model_calls}"}},
        context=_runtime_context(),
        stream_mode="updates",
    ):
        steps += 1
    return steps


# ---------------------------------------------------------------------------
# The derivation, checked against reality
# ---------------------------------------------------------------------------


async def test_marginal_cost_of_a_model_call_matches_the_derivation():
    """The load-bearing assertion of this whole module.

    Every extra model call must cost exactly what `steps_per_model_call` counted
    from the node names. Measured across several turn lengths, because a single
    data point cannot distinguish a per-call cost from a fixed overhead.
    """
    graph = _compiled_graph(_ScriptedModel(responses=[_final_turn()]))
    derived = steps_per_model_call(graph)

    measurements = {n: await _measure_super_steps(n) for n in (1, 2, 3, 4)}
    marginals = [measurements[n + 1] - measurements[n] for n in (1, 2, 3)]

    assert set(marginals) == {derived}, (
        f"derived {derived} steps per model call, but measured marginals {marginals} "
        f"from {measurements}. The middleware stack and the derivation disagree."
    )


async def test_base_steps_matches_the_measured_fixed_overhead():
    """The part of a turn that does not scale with model calls.

    Notably one less than the per-turn hook count: the closing model call emits
    `FinalResponseSchema` and ends the turn without entering `tools`, so the last
    cycle is a step short. That `- 1` is measured here rather than trusted.
    """
    graph = _compiled_graph(_ScriptedModel(responses=[_final_turn()]))
    per_call = steps_per_model_call(graph)

    measured_for_one = await _measure_super_steps(1)

    assert base_steps(graph) == measured_for_one - per_call, (
        f"base_steps()={base_steps(graph)} but a 1-call turn measured {measured_for_one} "
        f"super-steps against {per_call} per call"
    )


def test_every_node_is_classified():
    """An unclassified node means the derivation is silently mis-counting.

    This is the guard that makes the whole approach safe to leave unattended: if
    LangGraph adds a hook type (a `.before_tools`, say) it lands in
    `unclassified` and this fails, instead of being quietly omitted from the
    per-call cost and tightening every turn's budget.
    """
    buckets = classify_nodes(_compiled_graph())

    assert buckets["unclassified"] == [], (
        f"unrecognised graph nodes: {buckets['unclassified']}. "
        "app/core/step_budget.py needs to learn how often they run."
    )
    assert buckets["core"], "neither `model` nor `tools` was found — node naming changed"
    assert buckets["per_model_call"], "no per-model-call hooks found — hook naming changed"


def test_fifty_steps_was_six_model_calls():
    """The reported bug, in the unit that makes it obvious.

    Keeps the finding legible: the old default was not "50 of something
    generous", it was six model calls, which an ordinary two-delegation turn
    exceeds. If this number ever climbs to something comfortable, the middleware
    stack got cheaper and the incident is worth re-reading.
    """
    graph = _compiled_graph()

    affordable = (50 - base_steps(graph)) // steps_per_model_call(graph)

    assert affordable == 6


def test_the_graph_is_compiled_with_the_derived_limit():
    """The wiring, not just the arithmetic: GraphFactory must apply it."""
    graph = _compiled_graph()
    expected = recursion_limit_for(graph, AgentSettings.MAX_MODEL_CALLS_PER_TURN)

    assert graph.config is not None
    assert graph.config.get("recursion_limit") == expected


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_the_shared_env_var_no_longer_configures_the_orchestrator(monkeypatch):
    """The reported design bug.

    `MAX_RECURSION_LIMIT` is read by agent-runner and ringier-a2a-sdk (default
    50) and agent-common (75). A deployment pinning 50 — the orchestrator's own
    former default — used to silently reinstate the truncation, and CI could not
    catch it: the env var is unset there, so the tests stayed green against the
    intended budget.
    """
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    assert _resolve_max_model_calls_per_turn() == DEFAULT_MAX_MODEL_CALLS_PER_TURN


def test_setting_the_legacy_name_warns(monkeypatch, caplog):
    """Ignoring it silently would be worse than either honouring or refusing it —
    an operator who set it must be told it no longer applies, and what to set."""
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    with caplog.at_level("WARNING"):
        _resolve_max_model_calls_per_turn()

    assert MAX_MODEL_CALLS_PER_TURN_ENV in caplog.text


def test_the_orchestrator_budget_is_configurable(monkeypatch):
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, "40")

    assert _resolve_max_model_calls_per_turn() == 40


def test_a_malformed_budget_falls_back_rather_than_crashing(monkeypatch):
    """This is read at import time, so raising would take the process down."""
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, "40s")

    assert _resolve_max_model_calls_per_turn() == DEFAULT_MAX_MODEL_CALLS_PER_TURN


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_budget_falls_back(monkeypatch, raw):
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, raw)

    assert _resolve_max_model_calls_per_turn() == DEFAULT_MAX_MODEL_CALLS_PER_TURN
