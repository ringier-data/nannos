import logging
import os
from enum import Enum

logger = logging.getLogger(__name__)

# Model aliases are owned by the Model Gateway — the single source of truth for
# which models exist and what they can do. The app keeps NO static enumeration: `ModelType`
# is just a readable name for "a gateway model alias". Validity is checked against the live
# gateway (is_valid_model / resolve_chat_model) and per-model behavior is derived from the
# gateway's model_info (get_model_provider / capabilities), never from the alias string.
ModelType = str


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

    The Model Gateway is the source of truth for which models exist, so we
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
