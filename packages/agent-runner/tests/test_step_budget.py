"""agent-runner's turn budget.

The reported defect: `agent-runner` and `agent-common`'s `dynamic_agent` compile
the *same* sub-agent graph through `build_sub_agent_graph`, but each bound its own
hand-written super-step constant — 50 here, 75 there. A scheduled job pointed at a
sub-agent therefore died on `GraphRecursionError` where the identical agent,
delegated from a conversation, answered fine.

The derivation and its unit tests live in `agent_common.core.step_budget`, and the
default they share is now written down there once. What is tested here is what
agent-runner alone is responsible for: that it reads its own env name, and that
neither retired super-step name moves it.
"""

from __future__ import annotations

import pytest
from agent_common.core.step_budget import (
    DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS,
    LEGACY_RECURSION_LIMIT_ENV,
    LEGACY_SUB_AGENT_RECURSION_LIMIT_ENV,
    resolve_max_model_calls,
)

from agent.core import _MAX_MODEL_CALLS_ENV


@pytest.fixture(autouse=True)
def _unset_ambient_budget(monkeypatch):
    """These assertions describe the *default*, so the tuning knob this PR
    introduces must not be allowed to leak in from a developer's shell."""
    monkeypatch.delenv(_MAX_MODEL_CALLS_ENV, raising=False)


def test_the_budget_is_expressed_in_model_calls_under_its_own_name():
    """50 was a super-step count. The unit changed, and so did the name — sharing
    one env var across four consumers was a defect distinct from the shared unit."""
    assert _MAX_MODEL_CALLS_ENV == "AGENT_RUNNER_MAX_MODEL_CALLS_PER_TURN"
    assert (
        resolve_max_model_calls(_MAX_MODEL_CALLS_ENV, DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS)
        == DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS
    )


@pytest.mark.parametrize("legacy", [LEGACY_RECURSION_LIMIT_ENV, LEGACY_SUB_AGENT_RECURSION_LIMIT_ENV])
def test_a_retired_super_step_name_no_longer_moves_this_service(monkeypatch, legacy):
    """`MAX_RECURSION_LIMIT=50` is the exact value that produced the observed failure.

    Asserted through `resolve_max_model_calls` rather than by reloading `agent.core`:
    the module-level constant is read at import time, and `importlib.reload` would
    swap every class in the module for the rest of the session while sibling test
    modules still hold the pre-reload objects.
    """
    monkeypatch.setenv(legacy, "50")

    assert (
        resolve_max_model_calls(_MAX_MODEL_CALLS_ENV, DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS)
        == DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS
    )


def test_the_service_can_still_be_tuned_apart_from_its_siblings(monkeypatch):
    """Separate names exist so scheduled jobs can get a longer leash than
    interactive ones without touching the orchestrator or the in-process sub-agents."""
    monkeypatch.setenv(_MAX_MODEL_CALLS_ENV, "60")
    monkeypatch.setenv("SUB_AGENT_MAX_MODEL_CALLS_PER_TURN", "10")

    assert resolve_max_model_calls(_MAX_MODEL_CALLS_ENV, DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS) == 60
