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


# Thinking level literal for extended thinking configuration
class ThinkingLevel(str, Enum):
    minimal = "minimal"
    low = "low"
    medium = "medium"
    high = "high"


def _resolve_default_model() -> ModelType:
    """Resolve the default model, falling back if the configured model is unavailable.

    Imports MODEL_CONFIG lazily to avoid circular imports.
    """
    from agent_common.core.model_factory import MODEL_CONFIG

    configured: str = os.getenv("DEFAULT_MODEL", "claude-sonnet-4.5")
    if configured in MODEL_CONFIG:
        return configured  # type: ignore[return-value]

    if MODEL_CONFIG:
        fallback = next(iter(MODEL_CONFIG))
        logger.warning(
            "DEFAULT_MODEL '%s' is not available (no credentials). Falling back to '%s'.",
            configured,
            fallback,
        )
        return fallback  # type: ignore[return-value]

    raise RuntimeError(
        f"DEFAULT_MODEL '{configured}' is not available and no other models have credentials configured. "
        "Set OPENAI_COMPATIBLE_BASE_URL for a local model, or provide cloud credentials."
    )


# Lazy default model — resolved on first access via _resolve_default_model()
_default_model: ModelType | None = None


def get_resolved_default_model() -> ModelType:
    """Get the default model, resolving lazily on first call."""
    global _default_model
    if _default_model is None:
        _default_model = _resolve_default_model()
    return _default_model


# DEFAULT_MODEL kept for backward compat — reads env var directly.
# Use get_resolved_default_model() for runtime resolution that respects available credentials.
DEFAULT_MODEL: ModelType = os.getenv("DEFAULT_MODEL", "claude-sonnet-4.5")  # type: ignore
DEFAULT_THINKING_LEVEL: ThinkingLevel | None = (
    ThinkingLevel(os.getenv("ORCHESTRATOR_THINKING_LEVEL", ThinkingLevel.low))
    if os.getenv("ORCHESTRATOR_ENABLE_THINKING", "false").lower() == "true"
    else None
)
