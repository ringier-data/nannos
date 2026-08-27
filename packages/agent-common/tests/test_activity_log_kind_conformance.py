"""Pin the activity-log ``kind`` vocabulary to the repo-root a2a-extensions.json.

A mid-turn note (``notify_user``) is an ordinary activity-log status update with
``kind="note"`` in the message metadata. Clients drop kinds they do not know, so
a kind emitted here but absent from the registry / the SDK renders as nothing.
"""

import json
from pathlib import Path
from typing import get_args

from agent_common.a2a.stream_events import ActivityLogMeta
from agent_common.core.notify_user_tool import NOTE_KIND

_REGISTRY = Path(__file__).resolve().parents[2].parent / "a2a-extensions.json"


def _registry_kinds() -> list[str]:
    return json.loads(_REGISTRY.read_text())["activityLogKinds"]


def _meta_kinds() -> list[str]:
    # Optional[Literal[...]] -> unwrap the Optional, then the Literal.
    inner = [a for a in get_args(ActivityLogMeta.model_fields["kind"].annotation) if a is not type(None)]
    assert len(inner) == 1
    return list(get_args(inner[0]))


def test_activity_log_meta_kinds_match_repo_registry() -> None:
    assert sorted(_meta_kinds()) == sorted(_registry_kinds()), (
        "ActivityLogMeta.kind Literal diverged from a2a-extensions.json — "
        "update both (and embed-sdk extensions.ts ACTIVITY_LOG_KINDS) together"
    )


def test_notify_user_emits_a_registered_kind() -> None:
    assert NOTE_KIND in _registry_kinds()
