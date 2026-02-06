"""Base agent interface for A2A protocol."""

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable

from a2a.types import Task

from ..models import AgentStreamResponse, UserConfig
from .cost_tracking_mixin import CostTrackingMixin

logger = logging.getLogger(__name__)

# Context variables for request-scoped credentials (used by cost tracking)
try:
    from ..cost_tracking.logger import set_request_access_token, set_request_user_sub

    _has_cost_tracking = True
    _set_request_access_token = set_request_access_token
    _set_request_user_sub = set_request_user_sub
except ImportError:
    _has_cost_tracking = False
    _set_request_access_token = None  # type: ignore
    _set_request_user_sub = None  # type: ignore
    logger.debug("Cost tracking not available")


class BaseAgent(CostTrackingMixin, ABC):
    """Abstract base class for A2A agents.

    Uses the Template Method pattern: subclasses implement _stream_impl(),
    while stream() handles common setup (cost tracking, credentials).
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self, *args, **kwargs):
        """Initialize base agent with cost tracking support."""
        super().__init__(*args, **kwargs)
        self._sub_agent_id = None  # Will be set from user_config on requests

    def _update_sub_agent_id_from_config(self, user_config: UserConfig) -> None:
        """Update sub_agent_id for cost attribution if provided by orchestrator.

        Called automatically by stream() before delegating to _stream_impl().
        Updates the cost logger's sub_agent_id directly for automatic attribution.

        Args:
            user_config: User configuration that may contain sub_agent_id from orchestrator
        """
        if user_config.sub_agent_id and user_config.sub_agent_id != self._sub_agent_id:
            self._sub_agent_id = user_config.sub_agent_id
            # Update cost logger's sub_agent_id for automatic attribution
            if self._cost_tracking_enabled and self._cost_logger:
                self._cost_logger.sub_agent_id = self._sub_agent_id
                logger.info(f"Updated cost tracking with sub_agent_id={self._sub_agent_id}")

    @abstractmethod
    async def close(self):
        """Cleanup resources held by the agent."""
        pass

    async def report_usage(self, user_config: UserConfig, task: Task) -> None:
        """Report standard agent request usage after streaming completes.

        In case the agent want to report additional usage, it can override this method.

        Args:
            user_config: User configuration including user_sub
            task: The task context for the current interaction
        """
        if self._cost_tracking_enabled:
            await self.report_llm_usage(
                user_sub=user_config.user_sub,
                billing_unit_breakdown={
                    "requests": 1,
                },
                conversation_id=task.context_id,
            )

    async def stream(self, query: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Stream responses for a user query following the A2A protocol.

        Template method that handles common setup before delegating to _stream_impl():
        1. Starts cost tracking worker if not already started (lazy initialization)
        2. Updates sub_agent_id for cost tracking attribution
        3. Sets request-scoped access token for cost tracking
        4. Delegates to _stream_impl() for agent-specific logic
        5. Reports standard agent request usage after streaming completes

        Subclasses may override _setup_request() for additional setup (e.g., MCP credentials).

        Args:
            query: The user's natural language query
            user_config: User configuration including user_sub and access_token
            task: The task context for the current interaction

        Yields:
            AgentStreamResponse objects with state updates and content
        """
        # Lazy start cost tracking worker on first request (when event loop is running)
        if self._cost_logger and not self._cost_logger._auto_started:
            await self._cost_logger.start()

        # Update sub_agent_id for cost tracking attribution
        self._update_sub_agent_id_from_config(user_config)

        # Set request-scoped credentials for cost tracking and tool interceptors
        if _has_cost_tracking and _set_request_user_sub and _set_request_access_token:
            _set_request_user_sub(user_config.user_sub)
            if user_config.access_token:
                access_token = user_config.access_token.get_secret_value()
                _set_request_access_token(access_token)
            logger.debug(f"Set request credentials for user {user_config.user_sub}")
        # Delegate to agent-specific implementation
        # Note: sub_agent_id is available via current_sub_agent_id ContextVar for adding to LangGraph tags
        async for response in self._stream_impl(query, user_config, task):
            yield response
        await self.report_usage(user_config, task)

    @abstractmethod
    def _stream_impl(self, query: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Agent-specific streaming implementation.

        Subclasses implement this method with their specific logic.
        The base stream() method handles common setup before calling this.

        Args:
            query: The user's natural language query
            user_config: User configuration including user_sub and access_token
            task: The task context for the current interaction

        Yields:
            AgentStreamResponse objects with state updates and content
        """
        pass
