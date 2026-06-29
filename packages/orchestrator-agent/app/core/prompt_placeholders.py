"""Whitelisted ``{{TOKEN}}`` placeholder resolution for system prompts.

Used for both DB-stored sub-agent prompts (resolved at materialization time) and
static built-in prompts. Placeholders are resolved from the environment rather than
baked in, so the same prompt stays portable across deployments. Each resolver must
have a sensible dev fallback so prompts still work locally.
"""

import os
from typing import Any

_PROMPT_PLACEHOLDERS: dict[str, Any] = {
    "CONSOLE_FRONTEND_URL": lambda: os.environ.get("CONSOLE_FRONTEND_URL", "http://localhost:5173").rstrip("/"),
}


def resolve_prompt_placeholders(prompt: str) -> str:
    """Substitute whitelisted ``{{TOKEN}}`` placeholders in a system prompt."""
    for token, resolver in _PROMPT_PLACEHOLDERS.items():
        placeholder = "{{" + token + "}}"
        if placeholder in prompt:
            prompt = prompt.replace(placeholder, resolver())
    return prompt
