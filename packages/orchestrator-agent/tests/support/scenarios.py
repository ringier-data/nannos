"""Data-driven orchestrator scenarios, shared by both test tiers.

A scenario says what the orchestrator should *do* with a user request: which
sub-agents it delegates to, what it tells them, which tools it calls, how the
turn ends. One YAML file drives two very different runs:

- **mock tier** (``tests/test_scenarios_mock.py``) — the model is scripted to do
  exactly what the scenario expects, and the assertions run against the resulting
  real graph state. This does not test the model at all; it tests the *scenario*.
  If a scenario cannot pass even when the model behaves perfectly, it is wrong —
  a typo in a sub-agent name, an unobservable tool, an impossible expectation.
  Catching that costs milliseconds instead of an LLM call.

- **real tier** (``tests/integration/test_scenarios_real.py``) — the same
  expectations, a live model, nothing scripted. This is the one that answers
  "does the orchestrator actually route correctly", and the only one that can
  fail for interesting reasons.

Both call :func:`assert_scenario`, so an expectation cannot mean one thing
cheaply and another thing expensively.

Schema notes that reflect how the orchestrator really works:

- Delegation targets are ``task(subagent_type=...)`` values — there is no
  ``delegate_to_x`` tool, and no primary/secondary distinction in state.
- ``tools`` may only name **orchestrator-visible** tools (``task``,
  ``write_todos``, ``get_current_time``, ``FinalResponseSchema``, docstore and
  MCP tools). A sub-agent's internal tools run in another process behind A2A and
  never appear here, so expecting ``send_slack_message`` can only ever fail.
- ``instructions`` matches substrings against what a sub-agent was handed (the
  ``description`` argument of the ``task`` call). Substring, not equality: the
  model phrases hand-offs freely and an exact match would fail on rewording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import AIMessage

from .extraction import delegated_agents, delegations, final_text, task_state, tool_names
from .graph_harness import final_response, task_call, tool_call
from .mock_subagents import MockSubAgent

DATASETS_DIR = Path(__file__).parent.parent / "datasets"


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    reply: str = "Done."


@dataclass(frozen=True)
class Expectations:
    delegations_required: list[str] = field(default_factory=list)
    delegations_forbidden: list[str] = field(default_factory=list)
    ordered: bool = False
    instructions: dict[str, list[str]] = field(default_factory=dict)
    tools_required: list[str] = field(default_factory=list)
    tools_forbidden: list[str] = field(default_factory=list)
    task_state: str | None = None
    response_contains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    query: str
    subagents: list[SubAgentSpec]
    expect: Expectations

    def mock_subagents(self) -> list[MockSubAgent]:
        """Fresh mocks for this scenario — never reuse across tests, they record calls."""
        return [MockSubAgent(s.name, s.description, reply=s.reply) for s in self.subagents]


def load_scenarios(filename: str) -> list[Scenario]:
    """Parse a dataset file into Scenarios, failing loudly on a malformed entry."""
    path = DATASETS_DIR / filename
    raw = yaml.safe_load(path.read_text()) or []

    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        scenario_id = entry.get("id") or f"{filename}[{index}]"
        if scenario_id in seen:
            raise ValueError(f"{path}: duplicate scenario id {scenario_id!r}")
        seen.add(scenario_id)

        expect = entry.get("expect") or {}
        delegations_cfg = expect.get("delegations") or {}
        tools_cfg = expect.get("tools") or {}

        scenarios.append(
            Scenario(
                id=scenario_id,
                description=entry.get("description", ""),
                query=entry["input"]["query"],
                subagents=[
                    SubAgentSpec(
                        name=s["name"],
                        description=s.get("description", ""),
                        reply=s.get("reply", "Done."),
                    )
                    for s in (entry.get("subagents") or [])
                ],
                expect=Expectations(
                    delegations_required=list(delegations_cfg.get("required") or []),
                    delegations_forbidden=list(delegations_cfg.get("forbidden") or []),
                    ordered=bool(delegations_cfg.get("ordered", False)),
                    instructions={k: list(v) for k, v in (expect.get("instructions") or {}).items()},
                    tools_required=list(tools_cfg.get("required") or []),
                    tools_forbidden=list(tools_cfg.get("forbidden") or []),
                    task_state=expect.get("task_state"),
                    response_contains=list(expect.get("response_contains") or []),
                ),
            )
        )
    return scenarios


def scripted_responses(scenario: Scenario) -> list[AIMessage]:
    """Model turns that satisfy the scenario exactly — the mock tier's script.

    Derived from the expectations rather than written by hand, which is what
    makes the mock run a check on the scenario: the model is made to behave
    perfectly, so any failure is the scenario's fault, not the model's.
    """
    responses: list[AIMessage] = [
        tool_call(name, call_id=f"tool-{i}") for i, name in enumerate(scenario.expect.tools_required) if name != "task"
    ]

    for i, agent in enumerate(scenario.expect.delegations_required):
        # Give the sub-agent an instruction containing whatever the scenario
        # says it should receive, so the instruction assertions are exercised.
        wanted = " ".join(scenario.expect.instructions.get(agent, []))
        responses.append(task_call(agent, f"handle this: {wanted}".strip(), call_id=f"task-{i}"))

    message = " ".join(scenario.expect.response_contains) or "Done."
    responses.append(final_response(message, task_state=scenario.expect.task_state or "completed"))
    return responses


def assert_scenario(scenario: Scenario, state: Any, agents: list[MockSubAgent]) -> None:
    """Check a finished turn against the scenario. Used by both tiers."""
    expect = scenario.expect
    actual = delegated_agents(state)
    by_name = {a.name: a for a in agents}

    for agent in expect.delegations_required:
        assert agent in actual, f"[{scenario.id}] expected delegation to {agent!r}, got {actual}"

    for agent in expect.delegations_forbidden:
        assert agent not in actual, f"[{scenario.id}] must not delegate to {agent!r}, got {actual}"

    if expect.ordered and len(expect.delegations_required) > 1:
        positions = [actual.index(a) for a in expect.delegations_required]
        assert positions == sorted(positions), (
            f"[{scenario.id}] expected {expect.delegations_required} in order, got {actual}"
        )

    for agent, needles in expect.instructions.items():
        mock = by_name.get(agent)
        assert mock is not None, f"[{scenario.id}] scenario expects instructions for unregistered agent {agent!r}"
        for needle in needles:
            assert mock.called_with_substring(needle), (
                f"[{scenario.id}] {agent!r} never received {needle!r}; got {mock.received}"
            )

    called = tool_names(state)
    for tool in expect.tools_required:
        assert tool in called, f"[{scenario.id}] expected tool {tool!r}, got {called}"
    for tool in expect.tools_forbidden:
        assert tool not in called, f"[{scenario.id}] tool {tool!r} should not have been called, got {called}"

    if expect.task_state:
        actual_state = task_state(state)
        assert actual_state == expect.task_state, (
            f"[{scenario.id}] expected task_state {expect.task_state!r}, got {actual_state!r}"
        )

    if expect.response_contains:
        text = final_text(state).lower()
        for needle in expect.response_contains:
            assert needle.lower() in text, f"[{scenario.id}] response missing {needle!r}; got: {text[:300]}"


def describe_outcome(state: Any) -> dict[str, Any]:
    """Compact summary of what a turn did — for failure output and eval logging."""
    return {
        "delegations": [{"subagent": d.subagent, "completed": d.completed} for d in delegations(state)],
        "tools": tool_names(state),
        "task_state": task_state(state),
        "response": final_text(state)[:300],
    }
