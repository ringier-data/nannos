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


# There is no env var or hardcoded default model alias. The fleet default chat model is
# owned entirely by the Model Gateway / console model_defaults store (the "chat" role) and
# resolved at runtime via model_factory.get_default_model() / require_default_model().


DEFAULT_THINKING_LEVEL: ThinkingLevel | None = (
    ThinkingLevel(os.getenv("ORCHESTRATOR_THINKING_LEVEL", ThinkingLevel.low))
    if os.getenv("ORCHESTRATOR_ENABLE_THINKING", "false").lower() == "true"
    else None
)
