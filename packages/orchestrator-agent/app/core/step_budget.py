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
written down: it changes whenever a middleware carrying model hooks is added or
removed, and nothing forces a hand-maintained constant to be updated with it.

How the derivation works
------------------------
Middleware hook nodes are named ``<Middleware>.<hook>``, so the hook type is
readable off the node name:

- ``.before_model`` / ``.after_model`` run once per **model call**
- ``.before_agent`` / ``.after_agent`` run once per **turn**
- ``model`` and ``tools`` are the core of each cycle

Deliberately an over-estimate
-----------------------------
The budget must never be *below* what a turn really costs -- that is the bug
being replaced -- so every judgement here rounds up.

- Node count is an upper bound on a cycle's steps: a hook that returns early
  still occupies a node, while a skipped branch simply does not run.
- Nodes this module does not recognise are counted as *per model call*, the most
  expensive assumption, and logged. Ignoring them would shrink the budget toward
  exactly the truncation this exists to prevent.
- ``base_steps`` counts every per-turn hook, even though on some graphs the
  closing model call ends the turn without entering ``tools`` and one cycle is a
  step short. Whether it does depends on the structured-output strategy:
  ``ToolStrategy`` (no thinking) exits without ``tools``, while a graph that
  binds ``FinalResponseSchema`` as a ``return_direct`` tool -- thinking enabled
  on Anthropic/Bedrock, or Gemini's builtin-tools path, i.e. when
  ``select_response_format`` returns ``(None, True)`` -- routes the closing call
  through ``tools`` like any other. Subtracting the difference would be exact for
  the first and one step short for the second, and one step short is a
  ``GraphRecursionError`` fired after the answer is composed. So it is not
  subtracted. Measured in ``tests/test_step_budget.py`` against both paths.
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

    Reads ``graph.nodes`` -- the compiled ``Pregel``'s own dict -- rather than
    ``graph.get_graph().nodes``, which runs langgraph's edge-discovery simulation
    to build a drawable graph just to list names. The two differ only by
    ``__end__``, which is never counted.

    Every node lands in exactly one bucket. ``unclassified`` is the important
    one: anything in it means LangGraph grew a node shape this module does not
    understand, so the callers below charge it at the per-model-call rate and say
    so out loud.
    """
    buckets: dict[str, list[str]] = {
        "per_model_call": [],
        "per_turn": [],
        "core": [],
        "terminal": [],
        "unclassified": [],
    }
    for raw in graph.nodes:
        name = str(raw)
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


def _warn_unclassified(buckets: dict[str, list[str]]) -> None:
    if buckets["unclassified"]:
        logger.warning(
            "Unrecognised graph nodes %s are being charged at the per-model-call rate. "
            "app/core/step_budget.py needs to learn how often they run; until then the "
            "turn budget is a safe over-estimate rather than a correct one.",
            sorted(buckets["unclassified"]),
        )


def _steps_per_model_call(buckets: dict[str, list[str]]) -> int:
    # `core` is counted, not assumed to be 2: langchain only adds a `tools` node
    # when the graph has tools, and a rename would land both in `unclassified`.
    return len(buckets["per_model_call"]) + len(buckets["core"]) + len(buckets["unclassified"])


def _base_steps(buckets: dict[str, list[str]]) -> int:
    return len(buckets["per_turn"])


def steps_per_model_call(graph: Any) -> int:
    """Super-steps one model call costs in *graph*: its hooks, plus model and tools."""
    buckets = classify_nodes(graph)
    _warn_unclassified(buckets)
    return _steps_per_model_call(buckets)


def base_steps(graph: Any) -> int:
    """Super-steps a turn costs regardless of how many model calls it makes."""
    return _base_steps(classify_nodes(graph))


def recursion_limit_for(graph: Any, max_model_calls: int) -> int:
    """LangGraph ``recursion_limit`` that affords *max_model_calls* model calls.

    Classifies once: the two components are derived from a single pass, since
    this runs on every ``_create_graph`` including the cold path that rebuilds
    per turn.
    """
    buckets = classify_nodes(graph)
    _warn_unclassified(buckets)
    return _base_steps(buckets) + _steps_per_model_call(buckets) * max_model_calls
