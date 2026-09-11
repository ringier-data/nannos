"""Dataset scenarios, mock tier — validates the scenarios, not the model.

Each scenario runs through a real graph with the model scripted to do exactly
what the scenario expects. So these tests deliberately cannot tell you whether
the orchestrator routes correctly; the routing decision is handed to it.

What they *can* tell you is whether a scenario is well-formed: that the named
sub-agents register and dispatch, that the expected tools are orchestrator-
visible, that the instruction substrings survive the hand-off, and that the
assertions read the state channels correctly. A scenario that fails here would
fail in the real tier for reasons that have nothing to do with the model — so
catching it costs milliseconds instead of an LLM call.

The expensive tier runs the same expectations in
``tests/integration/test_scenarios_real.py``.
"""

from __future__ import annotations

import pytest

from tests.support.graph_harness import runtime_context, scripted_graph, turn_config, user_turn
from tests.support.scenarios import (
    assert_scenario,
    describe_outcome,
    load_scenarios,
    scripted_responses,
)
from tests.support.scripted_model import ScriptedChatModel

SCENARIOS = load_scenarios("core_routing.yaml", "failure_propagation.yaml")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_scenario_is_satisfiable(scenario):
    """With a perfectly-behaved model, the scenario's expectations must hold."""
    agents = scenario.mock_subagents()
    model = ScriptedChatModel(responses=scripted_responses(scenario))

    graph = scripted_graph(model)
    state = await graph.ainvoke(
        user_turn(scenario.query),
        config=turn_config(f"mock-{scenario.id}"),
        # agent.stream() is what normally populates pending_file_blocks from the
        # message's file parts; invoking the graph directly skips it, so the
        # scenario's attachments are parsed and injected here instead.
        context=runtime_context(*agents, pending_file_blocks=await scenario.attachment_blocks()),
    )

    try:
        assert_scenario(scenario, state, agents)
        _assert_attachments_were_appended(scenario, agents)
    except AssertionError:
        # The outcome summary is what makes a dataset failure diagnosable —
        # without it you get "expected slack-notifier" and no idea what happened.
        print(f"\n[{scenario.id}] outcome: {describe_outcome(state)}")
        raise


def _assert_attachments_were_appended(scenario, agents) -> None:
    """Check the middleware appended the attachment section, not just the script.

    ``expect.instructions`` cannot prove attachment forwarding in this tier: the
    scripted instruction is *built from* those needles, so a URL appears whether
    or not the orchestrator forwarded anything. The "[Attached files]" header is
    added by DynamicToolDispatchMiddleware and by nothing else, so it is the one
    marker here that means forwarding actually happened.
    """
    if not scenario.attachments:
        return
    for agent in agents:
        if agent.called and set(agent.input_modes) <= {"text", "text/plain"}:
            assert any("[Attached files]" in got for got in agent.received), (
                f"[{scenario.id}] {agent.name!r} was delegated to with attachments pending "
                f"but received no file section; got: {agent.received}"
            )


def test_dataset_only_expects_orchestrator_visible_tools():
    """Sub-agent tools never reach orchestrator state, so expecting one is a
    scenario bug that would otherwise surface as a confusing real-tier failure."""
    visible = {
        "task",
        "write_todos",
        "get_current_time",
        "FinalResponseSchema",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "eval",
    }
    for scenario in SCENARIOS:
        unexpected = set(scenario.expect.tools_required) - visible
        assert not unexpected, (
            f"[{scenario.id}] expects tools the orchestrator cannot see: {sorted(unexpected)}. "
            "Sub-agent tools run behind A2A in another process."
        )


def test_dataset_instructions_reference_registered_subagents():
    """An instruction keyed to an unregistered agent can never match."""
    for scenario in SCENARIOS:
        registered = {s.name for s in scenario.subagents}
        for agent in scenario.expect.instructions:
            assert agent in registered, f"[{scenario.id}] instructions for unregistered sub-agent {agent!r}"


def test_dataset_failure_scenarios_do_not_expect_success():
    """A scenario whose every required delegation fails must not expect success.

    The trap this closes: add ``fails:`` to a sub-agent, forget to change
    ``task_state``, and the dataset now asserts the orchestrator *should* report
    ``completed`` after a failed delegation — the exact bug these scenarios exist
    to catch, encoded as the expectation. Neither tier would object: the mock
    tier scripts its final response from ``expect``, and the real tier would just
    reward a model for hiding the failure.

    Deliberately narrow. One failing sub-agent among several can legitimately end
    ``completed`` if the orchestrator was expected to recover another way, so
    this only fires when *nothing* required of the turn succeeded.
    """
    for scenario in SCENARIOS:
        required = scenario.expect.delegations_required
        specs = {s.name: s for s in scenario.subagents}
        if not required or not all(specs[a].outcome_message for a in required if a in specs):
            continue
        assert scenario.expect.task_state not in (None, "completed"), (
            f"[{scenario.id}] every required delegation returns a non-success state, but the "
            f"scenario expects task_state={scenario.expect.task_state!r}. "
            "Set 'failed' or 'input_required', or the scenario asserts the bug."
        )


def test_dataset_delegation_expectations_are_consistent():
    """A scenario cannot both require and forbid the same sub-agent, and every
    named agent must actually be registered or the model could never pick it."""
    for scenario in SCENARIOS:
        registered = {s.name for s in scenario.subagents}
        required = set(scenario.expect.delegations_required)
        forbidden = set(scenario.expect.delegations_forbidden)

        assert not (required & forbidden), f"[{scenario.id}] both requires and forbids {sorted(required & forbidden)}"
        assert required <= registered, f"[{scenario.id}] requires unregistered {sorted(required - registered)}"
        assert forbidden <= registered, (
            f"[{scenario.id}] forbids {sorted(forbidden - registered)}, which is not registered — "
            "the model could not have picked it, so the expectation proves nothing"
        )
