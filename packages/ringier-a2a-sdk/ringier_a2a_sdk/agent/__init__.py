"""Base classes and utilities for building A2A agents."""

from .base import BaseAgent
from .cost_tracking_mixin import CostTrackingMixin
from .dynamodb_checkpointer_mixin import DynamoDBCheckpointerMixin

__all__ = [
    "BaseAgent",
    "CostTrackingMixin",
    "DynamoDBCheckpointerMixin",
    "LangGraphAgent",
    "LangGraphAnthropicAgent",
    "LangGraphBedrockAgent",
    "LangGraphGoogleGenAIAgent",
]


def __getattr__(name: str):
    """Lazy import optional agents to avoid importing optional dependencies."""
    if name == "LangGraphAgent":
        from .langgraph import LangGraphAgent

        return LangGraphAgent
    if name == "DynamoDBCheckpointerMixin":
        from .dynamodb_checkpointer_mixin import DynamoDBCheckpointerMixin

        return DynamoDBCheckpointerMixin
    if name == "LangGraphAnthropicAgent":
        try:
            from .langgraph_anthropic import LangGraphAnthropicAgent

            return LangGraphAnthropicAgent
        except ImportError as e:
            raise ImportError(
                f"LangGraphAnthropicAgent requires optional dependencies. "
                f"Install with: pip install ringier-a2a-sdk[langgraph-anthropic]. "
                f"Original error: {e}"
            ) from e
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
    if name == "LangGraphGoogleGenAIAgent":
        try:
            from .langgraph_google import LangGraphGoogleGenAIAgent

            return LangGraphGoogleGenAIAgent
        except ImportError as e:
            raise ImportError(
                f"LangGraphGoogleGenAIAgent requires optional dependencies. "
                f"Install with: pip install ringier-a2a-sdk[langgraph-google]. "
                f"Original error: {e}"
            ) from e
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
