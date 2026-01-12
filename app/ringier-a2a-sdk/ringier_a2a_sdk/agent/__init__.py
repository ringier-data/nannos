"""Base classes and utilities for building A2A agents."""

from .base import BaseAgent
from .cost_tracking_mixin import CostTrackingMixin

__all__ = ["BaseAgent", "CostTrackingMixin", "LangGraphBedrockAgent"]


def __getattr__(name: str):
    """Lazy import LangGraphBedrockAgent to avoid importing optional dependencies."""
    if name == "LangGraphBedrockAgent":
        try:
            from .langgraph_bedrock import LangGraphBedrockAgent

            return LangGraphBedrockAgent
        except ImportError as e:
            raise ImportError(
                f"LangGraphBedrockAgent requires optional dependencies. "
                f"Install with: pip install ringier-a2a-sdk[langgraph]. "
                f"Original error: {e}"
            ) from e
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
