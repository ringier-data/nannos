"""Dataset scenarios, real tier — does the model actually route correctly?

Same scenarios and the same ``assert_scenario`` as ``tests/test_scenarios_mock.py``,
with one difference that is the entire point: nothing is scripted. The model sees
the user's query and the registered sub-agents' descriptions, and decides for
itself. This is the only tier that can answer the ticket's "ensure agent triggers
function correctly".

Sub-agents are still mocks — a real Slack client would post real messages — but
the *routing decision* is genuine, and the descriptions the model routes on are
the scenario's own.

Non-deterministic by nature. A single failure is a data point, not a verdict;
that is what the planned pass-ratio gate is for.

Requires a reachable Model Gateway. See the conftest docstring for setup.
"""

from __future__ import annotations

import pytest
from a2a.types import Part
from agent_common.models.base import ModelType
from langsmith import testing as t

from app.core.turn_state import TurnState
from tests.support.scenarios import (
    assert_scenario,
    describe_outcome,
    load_scenarios,
)

from .conftest import one_model_per_provider

SCENARIOS = load_scenarios("core_routing.yaml")

pytestmark = [pytest.mark.integration, pytest.mark.slow]


async def run_turn(
    agent,
    query: str,
    user_config,
    config: dict,
    model_type: ModelType,
    attachments: list[Part] | None = None,
) -> dict:
    """Drive one real orchestrator turn and return the final graph state.

    ``stream()`` already performs an end-of-stream ``aget_state`` and parks the
    result on the ``turn_state`` carrier, so passing one avoids a second read and
    gives the extraction helpers exactly what the executor sees.

    When the stream aborts (recursion limit, provider error) ``stream()`` logs and
    returns without populating the carrier. Read the checkpointer directly in that
    case: an abort with no state dump tells you nothing about *why* the turn
    failed, and a runaway loop is only diagnosable from the message sequence.
    """
    turn_state = TurnState()
    # Text first, then file parts — the shape a client actually sends, so
    # agent.stream does the real parsing into pending_file_blocks.
    parts = [Part(text=query), *(attachments or [])]
    async for _ in agent.stream(parts, user_config, config=config, turn_state=turn_state):
        pass

    if turn_state.captured:
        return turn_state.final_values or {}

    graph = await agent.get_or_create_graph(model_type=model_type, thinking_level=None)
    snapshot = await graph.aget_state(config)
    values = getattr(snapshot, "values", None) or {}
    raise AssertionError(
        "agent.stream aborted without capturing state (see the logged error above).\n"
        f"Turn reached {len(values.get('messages') or [])} messages:\n"
        + _summarize_messages(values)
    )


def _summarize_messages(values: dict) -> str:
    """One line per message — enough to see a loop without dumping full content."""
    lines = []
    for i, msg in enumerate(values.get("messages") or []):
        kind = type(msg).__name__
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            detail = ", ".join(f"{c.get('name')}({c.get('args', {}).get('subagent_type', '')})" for c in calls)
        else:
            detail = repr(getattr(msg, "content", ""))[:110]
        lines.append(f"  {i:>3} {kind:<14} {detail}")
    return "\n".join(lines)


@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", one_model_per_provider())
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_scenario_against_real_model(
    scenario,
    model_type: ModelType,
    patched_agent,
    user_config_with_subagents,
    make_config,
):
    agents = scenario.mock_subagents()
    user_config = user_config_with_subagents(*agents)
    user_config.model = model_type

    t.log_inputs({"scenario": scenario.id, "model": model_type, "query": scenario.query})

    state = await run_turn(
        patched_agent,
        scenario.query,
        user_config,
        make_config(model_type),
        model_type,
        attachments=scenario.attachment_parts(),
    )

    outcome = describe_outcome(state)
    t.log_outputs(outcome)

    try:
        assert_scenario(scenario, state, agents)
    except AssertionError:
        # Without this the failure reads "expected slack-notifier" with no clue
        # what the model did instead — the difference between a five-minute and
        # a fifty-minute diagnosis on a non-deterministic test.
        print(f"\n[{scenario.id} / {model_type}] outcome: {outcome}")
        raise
