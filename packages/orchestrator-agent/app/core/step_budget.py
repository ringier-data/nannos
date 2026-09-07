"""Convert a turn budget in model calls into LangGraph's super-step budget.

LangGraph's ``recursion_limit`` counts **super-steps**, not model calls. Every
middleware hook is its own graph node, so one model call costs several of them.
That multiplier is invisible at the call site, which is how the limit came to be
50 -- a sensible number of model calls and a crippling number of steps. At 50 a
turn was capped at six model calls, and the orchestrator spends calls on
write_todos bookkeeping and filesystem exploration between delegations, so an
ordinary two-delegation request ("look up X and post it to Slack") exhausted the
budget and the user was asked to continue a task that had already completed
correctly.

The multiplier is therefore **derived from the compiled graph** rather than
written down. A hardcoded number is wrong twice over: it goes stale the moment a
middleware is added, and it cannot be right for more than one configuration --
the PTC middlewares appear and disappear with ``CODE_INTERPRETER_PTC``, so the
orchestrator's real stack differs between deployments.

How the derivation works
------------------------
Middleware hook nodes are named ``<Middleware>.<hook>``, so the hook type is
readable off the node name:

- ``.before_model`` / ``.after_model`` run once per **model call**
- ``.before_agent`` / ``.after_agent`` run once per **turn**
- ``model`` and ``tools`` are the core of each cycle

Measured against real graph runs (see ``tests/test_step_budget.py``): the
marginal cost of a model call is exactly ``per_call_hooks + 2``, and a turn of N
calls costs ``per_turn_hooks - 1 + N * (per_call_hooks + 2)``. The ``- 1`` is
real, not a fudge: the final model call emits ``FinalResponseSchema`` and ends
the turn without entering ``tools``, so one cycle is a step short.

Node count is an **upper bound** on the steps a cycle can spend, which is the
safe direction: a hook that returns early still occupies a node, while a skipped
branch simply does not run. Over-estimating gives the model at least the intended
number of calls; under-estimating is the bug this replaces.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PER_MODEL_CALL_HOOKS = (".before_model", ".after_model")
PER_TURN_HOOKS = (".before_agent", ".after_agent")
CORE_CYCLE_NODES = ("model", "tools")
TERMINAL_NODES = ("__start__", "__end__")


def classify_nodes(graph: Any) -> dict[str, list[str]]:
    """Partition a compiled graph's nodes by how often they run.

    Every node lands in exactly one bucket. ``unclassified`` is the important
    one: it is empty today, and anything appearing in it means LangGraph grew a
    node shape this module does not understand -- at which point the derivation
    below is quietly wrong and the test asserting it is empty fails.
    """
    buckets: dict[str, list[str]] = {"per_model_call": [], "per_turn": [], "core": [], "terminal": [], "unclassified": []}
    for name in graph.get_graph().nodes:
        if name.endswith(PER_MODEL_CALL_HOOKS):
            buckets["per_model_call"].append(name)
        elif name.endswith(PER_TURN_HOOKS):
            buckets["per_turn"].append(name)
        elif name in CORE_CYCLE_NODES:
            buckets["core"].append(name)
        elif name in TERMINAL_NODES:
            buckets["terminal"].append(name)
        else:
            buckets["unclassified"].append(name)
    return buckets


def steps_per_model_call(graph: Any) -> int:
    """Super-steps one model call costs in *graph*: its hooks, plus model and tools."""
    buckets = classify_nodes(graph)
    return len(buckets["per_model_call"]) + len(CORE_CYCLE_NODES)


def base_steps(graph: Any) -> int:
    """Super-steps a turn costs regardless of how many model calls it makes.

    The per-turn hooks, minus one for the ``tools`` step the closing model call
    never takes -- it emits the final-response envelope and the turn ends.
    """
    return max(len(classify_nodes(graph)["per_turn"]) - 1, 0)


def recursion_limit_for(graph: Any, max_model_calls: int) -> int:
    """LangGraph ``recursion_limit`` that affords *max_model_calls* model calls."""
    per_call = steps_per_model_call(graph)
    base = base_steps(graph)
    limit = base + per_call * max_model_calls
    logger.info(
        "Derived recursion_limit=%d from the compiled graph: %d base + %d per model call x %d calls",
        limit,
        base,
        per_call,
        max_model_calls,
    )
    return limit


def affordable_model_calls(graph: Any, recursion_limit: int) -> int:
    """Inverse of :func:`recursion_limit_for` -- what a given limit actually buys.

    Exists to make the old failure legible: it answers "50 steps was how many
    model calls?" (six), which is the number nobody could see at the call site.
    """
    per_call = steps_per_model_call(graph)
    if per_call <= 0:
        return 0
    return max((recursion_limit - base_steps(graph)) // per_call, 0)
