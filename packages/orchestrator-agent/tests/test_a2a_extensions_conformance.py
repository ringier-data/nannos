"""Pin the orchestrator's extension list to the repo-root a2a-extensions.json registry.

Three runtimes carry a copy of this list (orchestrator, console-backend, embed-sdk);
each pins it against the same JSON file, so adding/removing an extension anywhere
fails tests until every copy agrees. See a2a-extensions.json for why this matters
(the console-backend copy is the emission on/off switch).
"""

import json
from pathlib import Path

from app.core.a2a_extensions import ALL_EXTENSIONS

_REGISTRY = Path(__file__).resolve().parents[2].parent / "a2a-extensions.json"


def test_all_extensions_match_repo_registry() -> None:
    registry = json.loads(_REGISTRY.read_text())["extensions"]
    assert sorted(ALL_EXTENSIONS) == sorted(registry), (
        "app/core/a2a_extensions.py ALL_EXTENSIONS diverged from a2a-extensions.json — "
        "update both (and console-backend + embed-sdk) together"
    )
