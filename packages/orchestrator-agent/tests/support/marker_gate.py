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

    try:
        from _pytest.mark.expression import Expression

        compiled = Expression.compile(expression)
        without_marker = {name for name in item_markers if name != marker}
        return not compiled.evaluate(lambda name: name in without_marker)
    except Exception:
        # Private pytest API. If it ever moves, degrade to "not requested"
        # rather than to a surprise bill.
        return False


def integration_requested(markexpr: str | None, item_markers: Iterable[str]) -> bool:
    """True when the integration tier was explicitly asked for."""
    return env_opt_in() or expression_requires_marker(
        "integration", markexpr, item_markers
    )
