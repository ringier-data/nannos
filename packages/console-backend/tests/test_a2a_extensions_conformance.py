"""Pin console-backend's negotiated extension list to the repo-root registry.

This list becomes the X-A2A-Extensions header — the actual on/off switch for
extension emission in the orchestrator executor. A URN missing here is silently
never streamed, so drift is a product bug, not a style issue.
"""

import json
from pathlib import Path

from console_backend.utils.a2a_extensions import SUPPORTED_A2A_EXTENSIONS, X_A2A_EXTENSIONS_HEADER

_REGISTRY = Path(__file__).resolve().parents[2].parent / "a2a-extensions.json"


def test_supported_extensions_match_repo_registry() -> None:
    registry = json.loads(_REGISTRY.read_text())["extensions"]
    assert sorted(SUPPORTED_A2A_EXTENSIONS) == sorted(registry), (
        "console_backend/utils/a2a_extensions.py diverged from a2a-extensions.json — "
        "update both (and orchestrator + embed-sdk) together"
    )


def test_header_carries_every_extension() -> None:
    for urn in SUPPORTED_A2A_EXTENSIONS:
        assert urn in X_A2A_EXTENSIONS_HEADER
