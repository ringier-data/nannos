import logging
import os
from enum import Enum
from typing import Literal

logger = logging.getLogger(__name__)

# Model type literal for type safety
ModelType = Literal[
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    "claude-haiku-4-5",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "local",
]


# Thinking level literal for extended thinking configuration.
# Mirrors LiteLLM's reasoning_effort vocabulary (minus "none", which the
# enable-thinking toggle covers). Per-model support is read from the gateway.
class ThinkingLevel(str, Enum):
    minimal = "minimal"
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"


def _resolve_default_model() -> ModelType:
    """The configured default model alias.

    The Model Gateway is the source of truth for which models exist (ADR-0001), so we
    simply trust the configured DEFAULT_MODEL alias; the gateway validates it at call
    time. No static registry to check against.
    """
    return os.getenv("DEFAULT_MODEL", "claude-sonnet-4.5")  # type: ignore[return-value]


# Lazy default model — resolved on first access via _resolve_default_model()
_default_model: ModelType | None = None


def get_resolved_default_model() -> ModelType:
    """Get the default model, resolving lazily on first call."""
    global _default_model
    if _default_model is None:
        _default_model = _resolve_default_model()
    return _default_model


DEFAULT_THINKING_LEVEL: ThinkingLevel | None = (
    ThinkingLevel(os.getenv("ORCHESTRATOR_THINKING_LEVEL", ThinkingLevel.low))
    if os.getenv("ORCHESTRATOR_ENABLE_THINKING", "false").lower() == "true"
    else None
)
