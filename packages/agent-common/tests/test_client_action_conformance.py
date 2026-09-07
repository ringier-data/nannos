"""Pin the client-action kind vocabulary to the repo-root a2a-extensions.json.

The kinds exist in two languages (the tool's arg schema here, the widget's zod
boundary in embed-sdk) plus the risk scorer's deterministic score table. A kind
present in the tool but missing from the zod union is emitted by the agent,
silently refused by the widget, and reported to the user as done — so every copy
pins against the same registry and drift fails tests.
"""

import json
from pathlib import Path
from typing import get_args

from agent_common.core.client_action_tool import ClientActionInput
from agent_common.core.tool_risk_scorer import _CLIENT_ACTION_KIND_SCORES

_REGISTRY = Path(__file__).resolve().parents[2].parent / "a2a-extensions.json"


def _registry_kinds() -> list[str]:
    return json.loads(_REGISTRY.read_text())["clientActionKinds"]


def test_tool_kinds_match_repo_registry() -> None:
    tool_kinds = list(get_args(ClientActionInput.model_fields["kind"].annotation))
    assert sorted(tool_kinds) == sorted(_registry_kinds()), (
        "client_action_tool.py kind Literal diverged from a2a-extensions.json — "
        "update both (and embed-sdk schemas.ts) together"
    )


def test_every_kind_has_a_deterministic_risk_score() -> None:
    # Extra pre-listed kinds (e.g. planned ones) are fine — unknown kinds also
    # fail safe to the gating default — but a live kind must be scored on purpose.
    missing = [k for k in _registry_kinds() if k not in _CLIENT_ACTION_KIND_SCORES]
    assert not missing, f"kinds without a deterministic risk score: {missing}"
