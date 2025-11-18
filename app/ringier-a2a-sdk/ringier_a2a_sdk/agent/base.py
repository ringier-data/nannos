"""Base agent interface for A2A protocol."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable

from a2a.types import Task

from ..models import AgentStreamResponse, UserConfig


class BaseAgent(ABC):
    """Abstract base class for A2A agents.

    Defines the interface that all agents must implement to work with
    the A2A protocol and BaseAgentExecutor.
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    @abstractmethod
    async def close(self):
        """Cleanup resources held by the agent."""
        pass

    @abstractmethod
    async def stream(self, query: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Stream responses for a user query following the A2A protocol.

        This is the main entry point for the agent. It processes the user's query
        through the coordinator and yields AgentStreamResponse objects representing
        the processing status and results.

        Args:
            query: The user's natural language query
            user_config: User configuration including user_id and access_token
            task: The task context for the current interaction

        Yields:
            AgentStreamResponse objects with state updates and content
        """
        pass
