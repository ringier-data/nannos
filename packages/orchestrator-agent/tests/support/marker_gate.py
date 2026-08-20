"""Opt-in gate for the integration tier.

``-m "not integration"`` in addopts keeps the integration tier out of ordinary
runs, but a marker expression is *last-wins*: any user-supplied ``-m`` replaces
the default outright. Every module under ``tests/integration/`` also carries
``slow`` (and some carry ``langsmith``), so ``pytest -m slow`` selects the
integration directory and nothing else — 21 tests, ~4 minutes, ~$1.40 of real
LLM calls on any machine with gateway coordinates in ``.env``. The ``--ignore``
this PR replaced made that impossible regardless of markers, so the switch to a
marker traded a collection blind spot for a billing footgun.

This module answers one question — *did the caller actually ask for the
integration tier?* — so ``pytest_collection_modifyitems`` can skip it when the
answer is no. It lives here rather than in the integration conftest so it can
be unit-tested without a gateway, and so importing it costs nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# Escape hatch for callers that select by path or keyword rather than by marker
# (``pytest tests/integration/test_edge_cases.py``, ``-k streaming``), where
# there is no marker expression to read intent from.
ENV_OPT_IN = "RUN_INTEGRATION_TESTS"

_TRUTHY = {"1", "true", "yes", "on"}


def env_opt_in(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``RUN_INTEGRATION_TESTS`` is set to a truthy value."""
    env = os.environ if environ is None else environ
    return env.get(ENV_OPT_IN, "").strip().lower() in _TRUTHY


def expression_requires_marker(
    marker: str,
    markexpr: str | None,
    item_markers: Iterable[str],
) -> bool:
    """Was *marker* load-bearing in selecting an item that carries *item_markers*?

    Every item handed to ``pytest_collection_modifyitems`` already matched
    ``-m``. So rather than pattern-matching the expression text — which breaks on
    ``not integration``, ``integration and not langsmith``, and anything else
    with structure — re-evaluate that same expression with *marker* forced absent
    and every other marker left as-is:

    - ``-m slow`` still matches without ``integration`` -> the caller asked for
      something the integration tier merely happens to also be marked with.
      Not requested.
    - ``-m integration``, ``-m "integration and not langsmith"`` stop matching ->
      ``integration`` was the reason. Requested.

    ``-m "slow or integration"`` reads as not-requested, since ``slow`` alone
    still matches. That is the conservative direction, and ``-m integration``
    says it unambiguously.

    Anything unparseable returns False: failing closed costs a skipped test,
    failing open costs money.
    """
    expression = (markexpr or "").strip()
    if not expression:
        return False

    without_marker = {name for name in item_markers if name != marker}
    # Exactly False, not merely falsy: `_evaluate` returns None when it cannot
    # answer, and that must read as "not requested" rather than as "requested".
    return _evaluate(expression, without_marker) is False


def _evaluate(markexpr: str, markers: Iterable[str]) -> bool | None:
    """Evaluate *markexpr* against *markers*, or None if it cannot be evaluated.

    Uses pytest's own expression compiler, which is private. If it ever moves,
    callers see None and fail closed rather than guessing.
    """
    try:
        from _pytest.mark.expression import Expression

        present = set(markers)
        return Expression.compile(markexpr).evaluate(lambda name: name in present)
    except Exception:
        return None


def integration_requested(markexpr: str | None, item_markers: Iterable[str]) -> bool:
    """True when the integration tier was explicitly asked for."""
    return env_opt_in() or expression_requires_marker(
        "integration", markexpr, item_markers
    )


# ---------------------------------------------------------------------------
# The same question, asked before collection
# ---------------------------------------------------------------------------
# The integration conftest probes the gateway over the network at *import* time,
# because parametrize needs the model list while collecting. That import happens
# on every run — the directory is collected, not ignored — so a developer with
# gateway coordinates in .env paid for a live HTTP fetch on every unit run:
# measured +4.8s when the hostname does not resolve, since DNS is not bounded by
# the 2s socket timeout. Skipping the probe when the tier was never asked for
# needs the answer before any item exists to inspect, hence the two helpers below.

_MARKEXPR: str | None = None

# What integration items actually carry: `pytest.mark.integration` and
# `pytest.mark.slow` from the integration conftest's `pytestmark`, `asyncio` from
# asyncio_mode=auto, and `langsmith` on some modules.
_INTEGRATION_MARKER_SETS = (
    frozenset({"integration", "slow", "asyncio"}),
    frozenset({"integration", "slow", "asyncio", "langsmith"}),
)


def remember_markexpr(markexpr: str | None) -> None:
    """Stash the session's ``-m`` expression for code that runs before collection.

    Called from the root ``tests/conftest.py``, which is the only hook point
    guaranteed to run *before* ``tests/integration/conftest.py`` is imported —
    verified, not assumed. A subdirectory conftest cannot read ``config`` at
    module scope, and by the time its own ``pytest_configure`` fires the import
    has already happened.
    """
    global _MARKEXPR
    _MARKEXPR = markexpr


def markexpr_requests_integration(markexpr: str | None) -> bool:
    """Could *any* integration item be requested by *markexpr*?

    The pre-collection counterpart to ``integration_requested``. With no items
    to inspect, it asks over the marker sets integration items are known to
    carry and errs toward True: a false positive costs one gateway probe, while
    a false negative would break ``-m integration`` outright.

    Each candidate set must *match* the expression before being asked whether
    ``integration`` was load-bearing in that match. Skipping that check would
    ask a vacuous question — ``-m langsmith`` against a marker set with no
    ``langsmith`` in it — and answer "requested" for a run that selects nothing
    here at all.
    """
    if env_opt_in():
        return True

    expression = (markexpr or "").strip()
    if not expression:
        return False

    return any(
        _evaluate(expression, markers) is True
        and expression_requires_marker("integration", expression, markers)
        for markers in _INTEGRATION_MARKER_SETS
    )


def integration_possibly_requested() -> bool:
    """As ``markexpr_requests_integration``, for the remembered session expression."""
    return markexpr_requests_integration(_MARKEXPR)
