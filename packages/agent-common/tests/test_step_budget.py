"""The turn budget: model calls in, LangGraph super-steps out.

The original bug was that `MAX_RECURSION_LIMIT=50` capped a turn at six model
calls, because the limit counts super-steps and every middleware hook is its own
node. The fix expresses the budget in model calls and derives the multiplier from
the compiled graph.

It was fixed for the orchestrator first, and this module exists because the same
hand-written constants survived on the sub-agent paths: `dynamic_agent` bound 75
super-steps and `agent-runner` bound 50 to the *same* graph, so a scheduled run
of a sub-agent died on `GraphRecursionError` where a delegated one succeeded.

These tests exist to stop the derivation going quietly wrong, which is the only
way this bug can come back. Two of them do real work against a real
`build_sub_agent_graph`:

- `test_marginal_cost_of_a_model_call_matches_the_derivation` measures the
  super-steps of an actual graph run and compares them against what
  `step_budget` computed from the node names. If LangGraph renames a hook, or
  starts running one twice per cycle, the measurement and the derivation diverge
  and this fails.
- `test_every_node_of_the_sub_agent_graph_is_classified` fails when the graph
  grows a node shape the derivation does not understand, rather than silently
  mis-counting it.

Everything here runs offline: no gateway, no credentials, no model.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field, PrivateAttr

from agent_common.agents.dynamic_agent import (
    _SUB_AGENT_MAX_MODEL_CALLS,
    SUB_AGENT_MAX_MODEL_CALLS_ENV,
)
from agent_common.core import step_budget
from agent_common.core.graph_utils import build_sub_agent_graph
from agent_common.core.step_budget import (
    DEFAULT_MAX_MODEL_CALLS_PER_TURN,
    LEGACY_RECURSION_LIMIT_ENV,
    LEGACY_SUB_AGENT_RECURSION_LIMIT_ENV,
    MIN_MAX_MODEL_CALLS,
    classify_nodes,
    recursion_limit_for,
    resolve_max_model_calls,
    steps_per_model_call,
)

_ENV = "TEST_MAX_MODEL_CALLS_PER_TURN"


@tool
def ping() -> str:
    """Return pong."""
    return "pong"


class _ScriptedModel(BaseChatModel):
    """Replays canned turns. Deliberately local to this module.

    langchain's stock fakes inherit a `bind_tools` that raises
    NotImplementedError, and the sub-agent graph always binds tools, so a turn
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


def _compiled_graph(model: BaseChatModel | None = None, *, exclude_deep_agents_middlewares: bool = False):
    """A real sub-agent graph, as `dynamic_agent` and agent-runner build it.

    Both callers currently take the deep-agents stack. The lean variant is
    exercised too because `exclude_deep_agents_middlewares` is a supported knob
    that removes per-turn hook nodes, and the derivation has to stay right on a
    stack that no test would otherwise cover.
    """
    return build_sub_agent_graph(
        model=model or _ScriptedModel(responses=[]),
        tools=[ping],
        system_prompt="You are a test sub-agent.",
        checkpointer=MemorySaver(),
        exclude_deep_agents_middlewares=exclude_deep_agents_middlewares,
    )


def _tool_turn(index: int) -> AIMessage:
    """A model turn that calls a real tool, so the cycle enters `tools`."""
    return AIMessage(
        content="",
        tool_calls=[{"id": f"c{index}", "name": "ping", "args": {}, "type": "tool_call"}],
    )


async def _measure_super_steps(model_calls: int, *, exclude_deep_agents_middlewares: bool) -> int:
    """Run a turn spending exactly *model_calls* model calls; count super-steps.

    `stream_mode="updates"` yields once per node execution, which is what
    `recursion_limit` counts.
    """
    script = [_tool_turn(i) for i in range(model_calls - 1)] + [AIMessage(content="Done.")]
    graph = _compiled_graph(
        _ScriptedModel(responses=script),
        exclude_deep_agents_middlewares=exclude_deep_agents_middlewares,
    )

    steps = 0
    async for _ in graph.astream(
        {"messages": [HumanMessage("go")]},
        config={
            "configurable": {"thread_id": f"budget-{exclude_deep_agents_middlewares}-{model_calls}"},
            # Explicitly unbounded. `_compiled_graph` returns the bare builder output,
            # so without this LangGraph's own default of 25 caps the measurement — and
            # one added middleware would fail the derivation tests below with a
            # `GraphRecursionError` that looks like a broken derivation when it is not.
            "recursion_limit": 10_000,
        },
        stream_mode="updates",
    ):
        steps += 1
    return steps


# ---------------------------------------------------------------------------
# The derivation, checked against a real sub-agent graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exclude", [False, True], ids=["deep_agents", "lean"])
async def test_marginal_cost_of_a_model_call_matches_the_derivation(exclude):
    """The load-bearing assertion of this whole module.

    Every extra model call must cost exactly what `steps_per_model_call` counted
    from the node names. Measured across several turn lengths, because a single
    data point cannot distinguish a per-call cost from a fixed overhead.
    """
    graph = _compiled_graph(exclude_deep_agents_middlewares=exclude)
    derived = steps_per_model_call(graph)

    measurements = {n: await _measure_super_steps(n, exclude_deep_agents_middlewares=exclude) for n in (1, 2, 3, 4)}
    marginals = [measurements[n + 1] - measurements[n] for n in (1, 2, 3)]

    assert set(marginals) == {derived}, (
        f"derived {derived} steps per model call, but measured marginals {marginals} "
        f"from {measurements}. The middleware stack and the derivation disagree."
    )


@pytest.mark.parametrize("exclude", [False, True], ids=["deep_agents", "lean"])
@pytest.mark.parametrize("model_calls", [1, 2, 3])
async def test_the_budget_is_never_below_what_a_turn_actually_costs(exclude, model_calls):
    """The invariant that matters.

    A budget one step short is a `GraphRecursionError` fired *after* the answer is
    composed, so the derived limit must cover the real cost with slack to spare,
    never the reverse.
    """
    graph = _compiled_graph(exclude_deep_agents_middlewares=exclude)
    derived = recursion_limit_for(graph, model_calls)

    measured = await _measure_super_steps(model_calls, exclude_deep_agents_middlewares=exclude)

    assert derived >= measured, (
        f"derived budget {derived} is below the measured cost {measured} for "
        f"{model_calls} model call(s). A turn would die one step from the end, "
        f"after composing its answer."
    )
    # Slack is expected, but a whole extra cycle would mean the derivation has
    # drifted into guesswork rather than counting.
    assert derived - measured < steps_per_model_call(graph), (
        f"derived budget {derived} exceeds the measured {measured} by a full model "
        f"call or more; the derivation is over-counting, not rounding up."
    )


@pytest.mark.parametrize("exclude", [False, True], ids=["deep_agents", "lean"])
def test_every_node_of_the_sub_agent_graph_is_classified(exclude):
    """An unclassified node means the derivation is silently mis-counting.

    This is the guard that makes the whole approach safe to leave unattended: if
    LangGraph adds a hook type (a `.before_tools`, say) it lands in
    `unclassified` and this fails, instead of being quietly omitted from the
    per-call cost and tightening every turn's budget.
    """
    buckets = classify_nodes(_compiled_graph(exclude_deep_agents_middlewares=exclude))

    assert buckets["unclassified"] == [], (
        f"unrecognised graph nodes: {buckets['unclassified']}. "
        "agent_common/core/step_budget.py needs to learn how often they run."
    )
    assert buckets["core"], "neither `model` nor `tools` was found — node naming changed"
    assert buckets["per_model_call"], "no per-model-call hooks found — hook naming changed"


def test_an_unknown_node_inflates_the_budget_and_warns(caplog):
    """Production must not treat a node it does not understand as free.

    A `.before_tools`, an async-named hook, a renamed core node: anything
    unrecognised is charged at the per-model-call rate, so the budget errs large,
    and logged so it gets fixed. Counting it as zero would shrink the budget
    toward the truncation this module exists to prevent — and unlike
    `test_every_node_of_the_sub_agent_graph_is_classified`, this holds for stacks
    that only exist in a deployment, not in CI.
    """

    class _GraphWithMysteryNode:
        nodes = ["__start__", "model", "tools", "Some.before_model", "Mystery.before_tools"]

    # The warning is deduplicated per distinct shape per process, so this asserts on
    # the first sighting regardless of what ran before it.
    step_budget._warned_shapes.discard(("Mystery.before_tools",))

    with caplog.at_level("WARNING"):
        per_call = steps_per_model_call(_GraphWithMysteryNode())

    # 1 known hook + 2 core + 1 unknown, charged as if it ran every cycle.
    assert per_call == 4
    assert "Mystery.before_tools" in caplog.text


def test_a_graph_that_classifies_to_nothing_is_floored_and_warned(caplog):
    """0 is not a small budget; it is a graph that dies on its first super-step.

    Reachable from a test double (`build_sub_agent_graph` patched with a
    `MagicMock`, as several suites do), an already-wrapped runnable, or a future
    graph type that does not expose `.nodes`. Before the floor these bound
    `recursion_limit=0` silently. The floored value cannot be *right* — nothing was
    counted — but it is diagnosable, and it says so.
    """

    class _EmptyGraph:
        nodes: list[str] = []

    with caplog.at_level("WARNING"):
        limit = recursion_limit_for(_EmptyGraph(), 25)

    assert limit >= 25
    assert "cannot be" in caplog.text and "derived" in caplog.text


def test_a_mock_graph_no_longer_binds_a_zero_budget():
    """The specific shape that was already live in other suites."""
    from unittest.mock import MagicMock

    assert recursion_limit_for(MagicMock(), 25) > 0


def test_the_core_cycle_cost_is_counted_not_assumed():
    """`model` and `tools` are counted from the classification, not hardcoded to 2.

    langchain only adds a `tools` node when the graph has tools, and a rename
    would land both in `unclassified` — either way an assumed 2 would be a
    plausible-looking wrong number.
    """

    class _GraphWithoutTools:
        nodes = ["__start__", "model", "Some.after_model"]

    assert steps_per_model_call(_GraphWithoutTools()) == 2  # model + 1 hook, no tools


# ---------------------------------------------------------------------------
# The wiring: both sub-agent callers must bind a *derived* limit
# ---------------------------------------------------------------------------


def test_dynamic_agent_binds_the_derived_limit(monkeypatch):
    """The wiring, not just the arithmetic: `_build_graph` must apply it.

    The bug this replaces was a hand-written 75 bound here and a hand-written 50
    bound in agent-runner, to a graph whose real per-call cost neither of them
    counted. Driving the shipped `_build_graph` keeps the assertion on the real
    bind site rather than on a re-implementation of it.
    """
    from agent_common.a2a.models import LocalLangGraphSubAgentConfig
    from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable

    graph = _compiled_graph()
    monkeypatch.setattr("agent_common.agents.dynamic_agent.build_sub_agent_graph", lambda **_kwargs: graph)

    runnable = DynamicLocalAgentRunnable(
        config=LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="budget-agent",
            description="A sub-agent for the budget test",
            system_prompt="p",
        ),
        model=_ScriptedModel(responses=[]),
    )
    runnable._cached_tools = []
    runnable._cached_system_prompt = "p"
    runnable._cached_response_format = None
    runnable._cached_hitl_guarded = None
    runnable._cached_context_gated_tools = None

    bound = runnable._build_graph()

    assert bound.config is not None
    # Against `_SUB_AGENT_MAX_MODEL_CALLS`, not the module default: the budget is
    # resolved from the env at import time, so a developer with the tuning knob this
    # PR introduces set in their shell would otherwise fail this test, and no
    # `monkeypatch.setenv` inside it could reconcile the two.
    assert bound.config["recursion_limit"] == recursion_limit_for(graph, _SUB_AGENT_MAX_MODEL_CALLS)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_each_consumer_reads_its_own_env_name(monkeypatch):
    """One name for four consumers was a defect in its own right.

    Setting the sub-agent budget must not move anyone else's, which is precisely
    what `MAX_RECURSION_LIMIT` did.
    """
    monkeypatch.setenv(SUB_AGENT_MAX_MODEL_CALLS_ENV, "40")
    monkeypatch.setenv("ORCHESTRATOR_MAX_MODEL_CALLS_PER_TURN", "7")
    monkeypatch.delenv("AGENT_RUNNER_MAX_MODEL_CALLS_PER_TURN", raising=False)

    assert resolve_max_model_calls(SUB_AGENT_MAX_MODEL_CALLS_ENV) == 40
    assert resolve_max_model_calls("AGENT_RUNNER_MAX_MODEL_CALLS_PER_TURN") == DEFAULT_MAX_MODEL_CALLS_PER_TURN


@pytest.mark.parametrize("legacy", [LEGACY_RECURSION_LIMIT_ENV, LEGACY_SUB_AGENT_RECURSION_LIMIT_ENV])
def test_a_retired_super_step_env_var_is_ignored_but_warned_about(monkeypatch, caplog, legacy):
    """Honouring it would reinstate the bug; ignoring it silently is nearly as bad.

    An operator who pinned `MAX_RECURSION_LIMIT=50` to tame one service needs to
    be told it no longer reaches this one, and what to set instead — otherwise the
    only evidence is a `GraphRecursionError` with nothing pointing at the variable.
    """
    monkeypatch.setenv(legacy, "50")

    with caplog.at_level("WARNING"):
        resolved = resolve_max_model_calls(_ENV, 25)

    assert resolved == 25
    assert legacy in caplog.text
    assert _ENV in caplog.text, "the warning must name what to set instead"


def test_the_legacy_warning_does_not_orphan_the_sdk(monkeypatch, caplog):
    """`MAX_RECURSION_LIMIT` is retired *here*, not dead everywhere.

    `ringier-a2a-sdk` is published externally and still reads it, so an operator
    who takes "no longer configures this service" as "safe to remove" would
    silently change an externally built agent's bound. The wording is pinned
    because it is the only thing standing between them and that.
    """
    monkeypatch.delenv(LEGACY_SUB_AGENT_RECURSION_LIMIT_ENV, raising=False)
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    with caplog.at_level("WARNING"):
        resolve_max_model_calls(_ENV, 25)

    assert "ringier-a2a-sdk" in caplog.text
    assert "do not unset" in caplog.text.lower()


def test_the_advice_is_tailored_to_the_variable_actually_set(monkeypatch, caplog):
    """The two retired names need different advice, not one symmetrical paragraph.

    `SUB_AGENT_RECURSION_LIMIT` is read by nothing anywhere and can simply go.
    Telling the operator who set it that some *other* variable is still live in the
    SDK is noise, and reads as a reason not to remove the one they did set.
    """
    monkeypatch.delenv(LEGACY_RECURSION_LIMIT_ENV, raising=False)
    monkeypatch.setenv(LEGACY_SUB_AGENT_RECURSION_LIMIT_ENV, "75")

    with caplog.at_level("WARNING"):
        resolve_max_model_calls(_ENV, 25)

    assert "can be removed" in caplog.text
    assert "ringier-a2a-sdk" not in caplog.text, "the SDK caveat belongs only to MAX_RECURSION_LIMIT"


def test_the_budget_is_configurable(monkeypatch):
    monkeypatch.setenv(_ENV, "40")

    assert resolve_max_model_calls(_ENV, 25) == 40


def test_a_malformed_budget_falls_back_rather_than_crashing(monkeypatch):
    """This is read at import time in both consumers, so raising would take the
    process down at startup."""
    monkeypatch.setenv(_ENV, "40s")

    assert resolve_max_model_calls(_ENV, 25) == 25


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_budget_falls_back(monkeypatch, raw):
    monkeypatch.setenv(_ENV, raw)

    assert resolve_max_model_calls(_ENV, 25) == 25


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_a_non_positive_budget_is_clamped_rather_than_bricking_every_turn(monkeypatch, caplog, raw):
    """0 is not a small budget, it is a broken deployment.

    The derived limit would collapse to the per-turn overhead, so every request
    would exhaust it in its first super-steps and return a non-answer having done
    nothing — with nothing in the logs pointing at the env var.
    """
    monkeypatch.setenv(_ENV, raw)

    with caplog.at_level("WARNING"):
        resolved = resolve_max_model_calls(_ENV, 25)

    assert resolved == MIN_MAX_MODEL_CALLS
    assert _ENV in caplog.text


def test_an_implausibly_large_budget_is_honoured_but_flagged(monkeypatch, caplog):
    """Not clamped — a long budget can be deliberate — but a typo'd 2500 leaves no
    runaway protection at all, which is worth noticing before it costs money."""
    monkeypatch.setenv(_ENV, "2500")

    with caplog.at_level("WARNING"):
        resolved = resolve_max_model_calls(_ENV, 25)

    assert resolved == 2500
    assert "runaway" in caplog.text.lower()
