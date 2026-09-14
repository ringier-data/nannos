"""agent-runner's turn budget.

The reported defect: `agent-runner` and `agent-common`'s `dynamic_agent` compile
the *same* sub-agent graph through `build_sub_agent_graph`, but each bound its own
hand-written super-step constant — 50 here, 75 there. A scheduled job pointed at a
sub-agent therefore died on `GraphRecursionError` where the identical agent,
delegated from a conversation, answered fine.

The derivation and its unit tests live in `agent_common.core.step_budget`. What is
tested here is what agent-runner alone is responsible for: that its default is the
sub-agent default rather than a second number, and that it reads its own env name.
"""

from __future__ import annotations

import importlib

from agent_common.agents.dynamic_agent import DEFAULT_SUB_AGENT_MAX_MODEL_CALLS
from agent_common.core.step_budget import LEGACY_RECURSION_LIMIT_ENV

from agent.core import _DEFAULT_MAX_MODEL_CALLS, _MAX_MODEL_CALLS_ENV, _MAX_MODEL_CALLS_PER_TURN


def test_the_two_sub_agent_paths_no_longer_disagree():
    """The reported defect, stated as an invariant.

    The two callers may still be configured apart — that is what the separate env
    names are for — but their *defaults* must be one deliberate number for one
    graph, not two constants free to drift again.
    """
    assert _DEFAULT_MAX_MODEL_CALLS == DEFAULT_SUB_AGENT_MAX_MODEL_CALLS


def test_the_budget_is_expressed_in_model_calls_not_super_steps():
    """50 was a super-step count that worked out to roughly six model calls on this
    graph. A default that is still in that range would mean the unit never changed."""
    assert _MAX_MODEL_CALLS_PER_TURN == _DEFAULT_MAX_MODEL_CALLS
    assert _MAX_MODEL_CALLS_ENV == "AGENT_RUNNER_MAX_MODEL_CALLS_PER_TURN"


def test_the_legacy_shared_name_no_longer_moves_this_service(monkeypatch):
    """`MAX_RECURSION_LIMIT=50` was the value that produced the observed failure.

    It is read at import time, so the module is reloaded under the env var rather
    than trusting the constant captured at collection.
    """
    monkeypatch.setenv(LEGACY_RECURSION_LIMIT_ENV, "50")

    import agent.core as core

    reloaded = importlib.reload(core)
    try:
        assert reloaded._MAX_MODEL_CALLS_PER_TURN == DEFAULT_SUB_AGENT_MAX_MODEL_CALLS
    finally:
        monkeypatch.delenv(LEGACY_RECURSION_LIMIT_ENV, raising=False)
        importlib.reload(core)
