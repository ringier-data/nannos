import os
from enum import Enum
from typing import Literal

# Model type literal for type safety
ModelType = Literal[
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    "claude-haiku-4-5",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
]


# Thinking level literal for extended thinking configuration
class ThinkingLevel(str, Enum):
    minimal = "minimal"
    low = "low"
    medium = "medium"
    high = "high"


DEFAULT_MODEL: ModelType = os.getenv("DEFAULT_MODEL", "claude-sonnet-4.5")  # type: ignore
DEFAULT_THINKING_LEVEL: ThinkingLevel | None = (
    ThinkingLevel(os.getenv("ORCHESTRATOR_THINKING_LEVEL", ThinkingLevel.low))
    if os.getenv("ORCHESTRATOR_ENABLE_THINKING", "false").lower() == "true"
    else None
)
