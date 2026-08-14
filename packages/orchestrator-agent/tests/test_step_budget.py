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

import pytest

from app.models.config import AgentSettings
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

# Measured cost of the current graph. Both are asserted rather than assumed: if a
# middleware is added or removed these change, and the point of this module is
# that such a change is never silent.
STEPS_PER_MODEL_CALL = 8
BASE_STEPS = 2


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
