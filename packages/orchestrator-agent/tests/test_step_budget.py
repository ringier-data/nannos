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
from agent_common.models.base import ThinkingLevel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field, PrivateAttr

from app.core.graph_factory import GraphFactory
from app.core.step_budget import (
    classify_nodes,
    recursion_limit_for,
    steps_per_model_call,
)
from app.core.time_tools import create_time_tool
from app.models.config import (
    DEFAULT_MAX_MODEL_CALLS_PER_TURN,
    LEGACY_RECURSION_LIMIT_ENV,
    MAX_MODEL_CALLS_PER_TURN_ENV,
    MIN_MAX_MODEL_CALLS_PER_TURN,
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


def _compiled_graph(model: BaseChatModel | None = None, *, thinking: ThinkingLevel | None = None):
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
    return factory._create_graph(MODEL_TYPE, thinking)


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


async def _measure_super_steps(model_calls: int, thinking: ThinkingLevel | None = None) -> int:
    """Run a turn spending exactly *model_calls* model calls; count super-steps.

    `stream_mode="updates"` yields once per node execution, which is what
    `recursion_limit` counts.
    """
    script = [_tool_turn(i) for i in range(model_calls - 1)] + [_final_turn()]
    graph = _compiled_graph(_ScriptedModel(responses=script), thinking=thinking)

    steps = 0
    async for _ in graph.astream(
        {"messages": [HumanMessage("go")]},
        config={"configurable": {"thread_id": f"budget-{thinking}-{model_calls}"}},
        context=_runtime_context(),
        stream_mode="updates",
    ):
        steps += 1
    return steps


# ---------------------------------------------------------------------------
# The derivation, checked against reality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("thinking", [None, ThinkingLevel.high], ids=["tool_strategy", "thinking"])
async def test_marginal_cost_of_a_model_call_matches_the_derivation(thinking):
    """The load-bearing assertion of this whole module.

    Every extra model call must cost exactly what `steps_per_model_call` counted
    from the node names. Measured across several turn lengths, because a single
    data point cannot distinguish a per-call cost from a fixed overhead.

    Both structured-output strategies are covered. They build the *same* nodes but
    traverse them differently, and an earlier version of this test only ran the
    `thinking=None` path — which is how the base-step off-by-one below survived.
    """
    graph = _compiled_graph(thinking=thinking)
    derived = steps_per_model_call(graph)

    measurements = {n: await _measure_super_steps(n, thinking) for n in (1, 2, 3, 4)}
    marginals = [measurements[n + 1] - measurements[n] for n in (1, 2, 3)]

    assert set(marginals) == {derived}, (
        f"derived {derived} steps per model call, but measured marginals {marginals} "
        f"from {measurements}. The middleware stack and the derivation disagree."
    )


@pytest.mark.parametrize("thinking", [None, ThinkingLevel.high], ids=["tool_strategy", "thinking"])
@pytest.mark.parametrize("model_calls", [1, 2, 3])
async def test_the_budget_is_never_below_what_a_turn_actually_costs(thinking, model_calls):
    """The invariant that matters, on every path.

    A budget one step short is a `GraphRecursionError` fired *after* the answer is
    composed — the exact user-visible bug this PR fixes — so the derived limit must
    cover the real cost with slack to spare, never the reverse.

    This is why `base_steps` counts all per-turn hooks instead of subtracting the
    `tools` step the closing call skips. It skips it only under `ToolStrategy`:
    with thinking enabled, `FinalResponseSchema` is bound as a `return_direct`
    tool and the closing call routes through `tools` like any other. Subtracting
    would be exact for the first and one short for the second.
    """
    graph = _compiled_graph(thinking=thinking)
    derived = recursion_limit_for(graph, model_calls)

    measured = await _measure_super_steps(model_calls, thinking)

    assert derived >= measured, (
        f"derived budget {derived} is below the measured cost {measured} for "
        f"{model_calls} model call(s) at thinking={thinking}. A turn would die one "
        f"step from the end, after composing its answer."
    )
    # Slack is expected, but a whole extra cycle would mean the derivation has
    # drifted into guesswork rather than counting.
    assert derived - measured < steps_per_model_call(graph), (
        f"derived budget {derived} exceeds the measured {measured} by a full model "
        f"call or more; the derivation is over-counting, not rounding up."
    )


def test_an_unknown_node_inflates_the_budget_and_warns(caplog):
    """Production must not treat a node it does not understand as free.

    A `.before_tools`, an async-named hook, a renamed core node: anything
    unrecognised is charged at the per-model-call rate, so the budget errs large,
    and logged so it gets fixed. Counting it as zero would shrink the budget
    toward the truncation this module exists to prevent — and unlike
    `test_every_node_is_classified` below, this holds for stacks that only exist
    in a deployment, not in CI.
    """

    class _GraphWithMysteryNode:
        nodes = ["__start__", "model", "tools", "Some.before_model", "Mystery.before_tools"]

    with caplog.at_level("WARNING"):
        per_call = steps_per_model_call(_GraphWithMysteryNode())

    # 1 known hook + 2 core + 1 unknown, charged as if it ran every cycle.
    assert per_call == 4
    assert "Mystery.before_tools" in caplog.text


def test_the_core_cycle_cost_is_counted_not_assumed():
    """`model` and `tools` are counted from the classification, not hardcoded to 2.

    langchain only adds a `tools` node when the graph has tools, and a rename
    would land both in `unclassified` — either way an assumed 2 would be a
    plausible-looking wrong number.
    """

    class _GraphWithoutTools:
        nodes = ["__start__", "model", "Some.after_model"]

    assert steps_per_model_call(_GraphWithoutTools()) == 2  # model + 1 hook, no tools


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


def test_the_legacy_warning_does_not_read_as_obsolete(monkeypatch, caplog):
    """The wording is the whole point of this warning, so it is pinned.

    `MAX_RECURSION_LIMIT` is not dead: agent-common's `dynamic_agent` reads it as
    the fallback bound for every local sub-agent in this same process, and
    agent-runner and ringier-a2a-sdk read it too. An operator who takes "no longer
    configures the orchestrator" as "safe to remove" silently changes every
    sub-agent's recursion bound — so the warning has to say both things and name
    the variable that decouples them.
    """
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    with caplog.at_level("WARNING"):
        _resolve_max_model_calls_per_turn()

    text = caplog.text
    assert "do not unset" in text.lower(), "the warning must not read as 'this is obsolete'"
    assert "SUB_AGENT_RECURSION_LIMIT" in text, "must name the variable that decouples the two"
    assert "sub-agent" in text.lower()


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


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_a_non_positive_budget_is_clamped_rather_than_bricking_every_turn(monkeypatch, caplog, raw):
    """0 is not a small budget, it is a broken deployment.

    The derived limit would collapse to the per-turn overhead, so every request
    would exhaust it in its first super-steps and answer "I've been working on
    this for a while and need to take a break" having done nothing — with nothing
    in the logs pointing at the env var.
    """
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, raw)

    with caplog.at_level("WARNING"):
        resolved = _resolve_max_model_calls_per_turn()

    assert resolved == MIN_MAX_MODEL_CALLS_PER_TURN
    assert MAX_MODEL_CALLS_PER_TURN_ENV in caplog.text


def test_an_implausibly_large_budget_is_honoured_but_flagged(monkeypatch, caplog):
    """Not clamped — a long budget can be deliberate — but a typo'd 2500 leaves no
    runaway protection at all, which is worth noticing before it costs money."""
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, "2500")

    with caplog.at_level("WARNING"):
        resolved = _resolve_max_model_calls_per_turn()

    assert resolved == 2500
    assert "runaway" in caplog.text.lower()
