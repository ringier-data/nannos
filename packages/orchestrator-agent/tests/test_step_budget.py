"""The recursion budget is spent in super-steps, not model calls.

``MAX_RECURSION_LIMIT`` bounds LangGraph *super-steps*. Every middleware hook is
its own graph node, so a single model call costs several steps — currently ~8.
That multiplier is invisible at the call site, which is how the limit came to be
set to 50: a sensible number of model calls, and a crippling number of steps. A
two-delegation request exhausted it and the user was told the task needed more
steps, discarding an answer the orchestrator had already produced correctly.

These tests make the multiplier visible. They run a real graph with a scripted
model, so the step counts are the ones production pays. If someone adds a
middleware, the per-call cost rises here and the headroom assertion tightens —
at review time rather than in production.

No LLM, no gateway.
"""

from __future__ import annotations

import logging

import pytest

from app.models.config import (
    DEFAULT_MAX_MODEL_CALLS_PER_TURN,
    LEGACY_RECURSION_LIMIT_ENV,
    MAX_MODEL_CALLS_PER_TURN_ENV,
    AgentSettings,
    _resolve_max_model_calls_per_turn,
)
from tests.support.graph_harness import (
    final_response,
    runtime_context,
    scripted_graph,
    task_call,
    turn_config,
    user_turn,
)
from tests.support.mock_subagents import MockSubAgent
from tests.support.scripted_model import ScriptedChatModel

# The app's own constants, not a private copy. The limit is derived from them, so
# a copy here could agree with the tests while disagreeing with production — and
# the arithmetic would have three homes again. What makes these a *pin* rather
# than a tautology is that `_super_steps` counts the steps of a real graph run:
# add a middleware and the measurement diverges from the constant.
STEPS_PER_MODEL_CALL = AgentSettings.STEPS_PER_MODEL_CALL
BASE_STEPS = AgentSettings.BASE_STEPS


async def _super_steps(delegations: int) -> int:
    """Count graph super-steps for a turn with N delegations plus a final response."""
    agents = [MockSubAgent(f"agent-{i}", f"Agent {i}.", reply="ok") for i in range(max(delegations, 1))]
    responses = [task_call(f"agent-{i}", f"step {i}", call_id=f"c{i}") for i in range(delegations)]
    responses.append(final_response("Done."))

    graph = scripted_graph(ScriptedChatModel(responses=responses))
    steps = 0
    async for _ in graph.astream(
        user_turn("go"),
        config=turn_config(f"budget-{delegations}"),
        context=runtime_context(*agents),
        stream_mode="updates",
    ):
        steps += 1
    return steps


@pytest.mark.parametrize("delegations", [0, 1, 2, 3])
async def test_step_cost_is_linear_in_model_calls(delegations):
    """steps = BASE + PER_CALL * model_calls, where model_calls = delegations + 1.

    A failure here means the middleware stack changed. That is allowed — but the
    constants above and the headroom check below must be updated deliberately,
    because every middleware hook silently reduces everyone's turn budget.
    """
    model_calls = delegations + 1
    expected = BASE_STEPS + STEPS_PER_MODEL_CALL * model_calls

    assert await _super_steps(delegations) == expected


async def test_configured_limit_affords_a_realistic_multi_step_turn():
    """The regression that mattered.

    A real two-delegation turn is not 3 model calls — the orchestrator spends
    calls on write_todos between delegations and on filesystem exploration
    before them. Observed on live models: 6 calls for two delegations, 6 for one.
    Budget for at least 12 so ordinary work is not truncated.
    """
    affordable_model_calls = (AgentSettings.MAX_RECURSION_LIMIT - BASE_STEPS) // STEPS_PER_MODEL_CALL

    assert affordable_model_calls >= 12, (
        f"MAX_RECURSION_LIMIT={AgentSettings.MAX_RECURSION_LIMIT} affords only "
        f"{affordable_model_calls} model calls at {STEPS_PER_MODEL_CALL} super-steps each. "
        "Real turns spend calls on todo bookkeeping and file exploration as well as "
        "delegation; at 6 an ordinary two-delegation request is truncated mid-turn."
    )


async def test_limit_still_bounds_a_runaway_loop():
    """The limit exists to stop infinite loops, so raising it must not disable it.

    deepagents defaults to 1000; staying well under that keeps a stuck graph from
    burning a large amount of tokens before it is cut off.
    """
    assert AgentSettings.MAX_RECURSION_LIMIT < 500, (
        "the recursion limit is the only backstop against a runaway agent loop; "
        "keep it well below the deepagents default of 1000"
    )


# ---------------------------------------------------------------------------
# The limit is derived, and derived from an unshared env var
# ---------------------------------------------------------------------------


def test_the_limit_is_derived_from_the_step_cost():
    """One source of truth for the arithmetic.

    It used to live in three places — this module, the config comment and
    AGENTS.md — and the AGENTS.md copy was wrong within a commit of the config
    changing.
    """
    assert AgentSettings.MAX_RECURSION_LIMIT == (
        AgentSettings.BASE_STEPS
        + AgentSettings.STEPS_PER_MODEL_CALL * AgentSettings.MAX_MODEL_CALLS_PER_TURN
    )


def test_the_derived_limit_affords_exactly_the_configured_model_calls():
    """The round trip: super-steps back to the unit that was actually chosen."""
    affordable = (AgentSettings.MAX_RECURSION_LIMIT - AgentSettings.BASE_STEPS) // AgentSettings.STEPS_PER_MODEL_CALL

    assert affordable == AgentSettings.MAX_MODEL_CALLS_PER_TURN


def test_the_shared_env_var_no_longer_configures_the_orchestrator(monkeypatch):
    """The reported design bug.

    `MAX_RECURSION_LIMIT` is read by agent-runner and ringier-a2a-sdk (default 50)
    and agent-common (75). A deployment pinning 50 — the orchestrator's own former
    default — used to silently reinstate the truncation, and CI could not catch it:
    the env var is unset there, so the tests stayed green against 200.
    """
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    assert _resolve_max_model_calls_per_turn() == DEFAULT_MAX_MODEL_CALLS_PER_TURN


def test_setting_the_shared_env_var_warns_that_it_is_ignored(monkeypatch, caplog):
    """Ignoring it silently would be its own trap."""
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    with caplog.at_level(logging.WARNING):
        _resolve_max_model_calls_per_turn()

    assert LEGACY_RECURSION_LIMIT_ENV in caplog.text
    assert MAX_MODEL_CALLS_PER_TURN_ENV in caplog.text


def test_nothing_is_warned_about_when_the_shared_var_is_unset(monkeypatch, caplog):
    monkeypatch.delenv(LEGACY_RECURSION_LIMIT_ENV, raising=False)

    with caplog.at_level(logging.WARNING):
        _resolve_max_model_calls_per_turn()

    assert caplog.text == ""


def test_the_budget_is_configured_in_model_calls(monkeypatch):
    """The knob is in the unit a human can reason about."""
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, "40")

    assert _resolve_max_model_calls_per_turn() == 40


def test_a_malformed_value_falls_back_rather_than_crashing_at_import(monkeypatch):
    monkeypatch.setenv(MAX_MODEL_CALLS_PER_TURN_ENV, "twenty")

    assert _resolve_max_model_calls_per_turn() == DEFAULT_MAX_MODEL_CALLS_PER_TURN
