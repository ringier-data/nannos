"""Pin the HITL decision vocabulary to the repo-root a2a-extensions.json registry.

The types exist in two languages: the set the server accepts here, and the
``Decision`` union in embed-sdk ``approval-codec.ts``. The server treats a type it
does not recognise as a REJECTION (an unreadable answer must never read as
consent), so a type added on one side only does not fail loudly at the boundary —
it silently blocks the call the human meant to allow. Both copies therefore pin
against the same registry and drift fails tests.
"""

import json
from pathlib import Path

from agent_common.core.hitl_resume import HITL_DECISION_TYPES

_REGISTRY = Path(__file__).resolve().parents[2].parent / "a2a-extensions.json"


def _registry_types() -> list[str]:
    return json.loads(_REGISTRY.read_text())["hitlDecisionTypes"]


def test_decision_types_match_repo_registry() -> None:
    assert sorted(HITL_DECISION_TYPES) == sorted(_registry_types()), (
        "hitl_resume.py HITL_DECISION_TYPES diverged from a2a-extensions.json — "
        "update both (and embed-sdk approval-codec.ts) together"
    )


def test_approve_and_reject_are_always_available() -> None:
    # ``edit`` is conditional (an action's ``allowed_decisions`` may omit it, and
    # PTC-gated calls always do), but a gate the human can neither allow nor deny
    # is not a gate.
    assert {"approve", "reject"} <= set(HITL_DECISION_TYPES)
