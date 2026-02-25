"""Cost tracking mixin for BaseAgent with framework-agnostic design."""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from ..cost_tracking import CostLogger

# Import ContextVar for sub_agent_id (used by remote agents)
try:
    from ..middleware.sub_agent_id_middleware import current_sub_agent_id

    _has_sub_agent_id_contextvar = True
except ImportError:
    current_sub_agent_id = None
    _has_sub_agent_id_contextvar = False

logger = logging.getLogger(__name__)


class CostTrackingMixin:
    """
    Mixin that provides cost tracking capabilities to BaseAgent implementations.

    This mixin provides:
    1. Manual cost reporting API (framework-agnostic)
    2. Optional LangChain auto-instrumentation
    3. Automatic sub_agent_id extraction from JWT token

    Usage:
        ```python
        from ringier_a2a_sdk.agent import BaseAgent

        class MyAgent(BaseAgent):  # BaseAgent already includes CostTrackingMixin
            def __init__(self, backend_url: str):
                # Initialize cost tracking
                self.enable_cost_tracking(backend_url=backend_url)

            async def stream(self, query, user_config, task):
                # Manual reporting (works with any LLM framework)
                await self.report_llm_usage(
                    user_sub=user_config.user_sub,
                    provider="openai",
                    model_name="gpt-4",
                    billing_unit_breakdown={"input_tokens": 100, "output_tokens": 50},
                    conversation_id=task.context_id
                )

                # Or use LangChain auto-instrumentation
                model = ChatOpenAI(model="gpt-4", callbacks=self.get_langchain_callbacks())
        ```
    """

    def __init__(self, *args, **kwargs):
        """Initialize the mixin (safe for multiple inheritance)."""
        super().__init__(*args, **kwargs)
        self._cost_logger: Optional[CostLogger] = None
        self._cost_tracking_enabled = False
        self._langchain_callbacks: list = []

    def enable_cost_tracking(
        self,
        backend_url: Optional[str] = None,
        cost_logger: Optional[CostLogger] = None,
        batch_size: int = 10,
        flush_interval: float = 5.0,
        sub_agent_id: Optional[int] = None,
    ) -> None:
        """
        Enable cost tracking for this agent.

        Accepts either a pre-built CostLogger instance (when the caller owns the logger
        lifecycle, e.g. a factory that shares one logger across agents) or a backend_url
        to construct a new one. Exactly one of the two must be provided.

        Args:
            backend_url: Backend API URL (e.g., "https://chat.nannos.rcplus.io/").
                Used to create a new CostLogger. Mutually exclusive with cost_logger.
            cost_logger: Existing CostLogger instance to use directly.
                Mutually exclusive with backend_url.
            batch_size: Number of records to batch before sending (ignored when cost_logger provided)
            flush_interval: Seconds to wait before auto-flushing partial batches (ignored when cost_logger provided)
            sub_agent_id: Optional sub-agent ID for cost attribution (passed by orchestrator)

        Note:
            - Manual cost reporting via report_llm_usage() works without LangChain
            - LangChain auto-instrumentation requires langchain_core to be installed
            - The sub_agent_id should be passed by the orchestrator in UserConfig.sub_agent_id
              for automatic cost attribution to the correct sub-agent.
        """
        if backend_url is None and cost_logger is None:
            raise ValueError("Either backend_url or cost_logger must be provided")
        if backend_url is not None and cost_logger is not None:
            raise ValueError("Only one of backend_url or cost_logger may be provided")

        if cost_logger is not None:
            # Use the pre-built logger directly (caller owns lifecycle)
            self._cost_logger = cost_logger
            log_context = "existing CostLogger"
        else:
            # Construct a new logger from backend_url
            self._cost_logger = CostLogger(
                backend_url=backend_url,  # type: ignore[arg-type]
                batch_size=batch_size,
                flush_interval=flush_interval,
                sub_agent_id=sub_agent_id,
            )
            log_context = f"backend={backend_url}"

        # Note: Worker will be started lazily on first request (when event loop is running)

        # Try to create LangChain callback if langchain_core is available
        # This uses lazy import to avoid forcing langchain_core dependency
        try:
            from ..cost_tracking import CostTrackingCallback

            self._langchain_callbacks = [CostTrackingCallback(self._cost_logger, sub_agent_id=sub_agent_id)]
            logger.info(
                f"Cost tracking enabled with LangChain auto-instrumentation ({log_context}, sub_agent_id={sub_agent_id})"
            )
        except ImportError as e:
            logger.info(f"Cost tracking enabled with manual instrumentation only (langchain_core not available: {e})")
            self._langchain_callbacks = []

        # Always enable cost tracking if CostLogger initialized successfully
        self._cost_tracking_enabled = True

    async def report_llm_usage(
        self,
        user_sub: str,
        billing_unit_breakdown: Dict[str, int],
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        langsmith_run_id: Optional[str] = None,
        langsmith_trace_id: Optional[str] = None,
        invoked_at: Optional[datetime] = None,
    ) -> None:
        """
        Manually report usage (framework-agnostic).

        Use this method to report usage from any LLM framework (not just LangChain).
        For remote agents, sub_agent_id is automatically read from current_sub_agent_id ContextVar
        (set by SubAgentIdMiddleware). For local agents, it's extracted from LangGraph tags.

        Args:
            user_sub: User sub from user_config (database ID for stable attribution)
            billing_unit_breakdown: Dict of billing units to counts
                Example: {"input_tokens": 100, "output_tokens": 50}
            provider: Optional Provider name ("openai", "bedrock_converse", etc.).
                Not required if mapping against agent specific rate cards.
            model_name: Optional Model identifier (e.g., "gpt-4o-2024-08-06").
                Not required if mapping against agent specific rate cards.
            conversation_id: Optional conversation ID
            langsmith_run_id: Optional LangSmith run ID
            langsmith_trace_id: Optional LangSmith trace ID
            invoked_at: Timestamp (defaults to now)

        Example:
            ```python
            # After calling any LLM
            await self.report_llm_usage(
                user_sub=user_config.user_sub,
                provider="openai",
                model_name="gpt-4o-2024-08-06",
                billing_unit_breakdown={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens
                },
                conversation_id=task.context_id
            )
            ```
        """
        if not self._cost_tracking_enabled or not self._cost_logger:
            logger.debug("Cost tracking not enabled, skipping usage report")
            return

        # For remote agents: read sub_agent_id from ContextVar (set by SubAgentIdMiddleware)
        # For local agents: will be None here, extracted from tags by callback instead
        sub_agent_id_from_contextvar = None
        if _has_sub_agent_id_contextvar and current_sub_agent_id:
            sub_agent_id_from_contextvar = current_sub_agent_id.get()
            if sub_agent_id_from_contextvar:
                logger.debug(f"[COST TRACKING] Using sub_agent_id from ContextVar: {sub_agent_id_from_contextvar}")

        self._cost_logger.log_cost_async(
            user_sub=user_sub,
            provider=provider,
            model_name=model_name,
            billing_unit_breakdown=billing_unit_breakdown,
            conversation_id=conversation_id,
            langsmith_run_id=langsmith_run_id,
            langsmith_trace_id=langsmith_trace_id,
            invoked_at=invoked_at,
            _sub_agent_id_from_tag=sub_agent_id_from_contextvar,  # Pass through as if from tag
        )

        logger.debug(
            f"Reported usage: provider={provider}, model={model_name}, units={sum(billing_unit_breakdown.values())}"
        )

    def get_langchain_callbacks(self) -> list:
        """
        Get LangChain callbacks for auto-instrumentation.

        Returns:
            List of LangChain callbacks (empty if cost tracking not enabled)

        Usage:
            ```python
            # Add to LangChain model for automatic cost tracking
            model = ChatOpenAI(
                model="gpt-4",
                callbacks=self.get_langchain_callbacks()
            )
            ```
        """
        if not self._cost_tracking_enabled:
            return []
        return self._langchain_callbacks.copy()

    def create_runnable_config(
        self,
        user_sub: str,
        conversation_id: str,
        thread_id: Optional[str] = None,
        checkpoint_ns: Optional[str] = None,
        checkpointer=None,
        **extra_configurable,
    ) -> Dict[str, Any]:
        """
        Create a RunnableConfig with cost tracking tags and callbacks.

        This helper method constructs a properly configured RunnableConfig object
        with consistent cost tracking tags and callbacks, eliminating code duplication
        across agents.

        Args:
            user_sub: User sub for cost attribution
            conversation_id: Conversation ID for cost attribution
            thread_id: Thread ID for checkpointing (defaults to conversation_id)
            checkpoint_ns: Checkpoint namespace for isolation (e.g., "agent-creator")
            checkpointer: Checkpointer instance (for __pregel_checkpointer)
            **extra_configurable: Additional configurable parameters

        Returns:
            RunnableConfig (or dict) with tags and callbacks configured for cost tracking

        Usage:
            ```python
            config = self.create_runnable_config(
                user_sub=user_config.user_sub,
                conversation_id=task.context_id,
                thread_id=task.context_id,
                checkpoint_ns="agent-creator",
                checkpointer=self._checkpointer,
            )
            await graph.astream(messages, config)
            ```

        Note:
            - Automatically adds user_sub:{user_sub} and conversation:{conversation_id} tags
            - Automatically adds sub_agent:{sub_agent_id} tag if available from ContextVar
            - Includes callbacks from get_langchain_callbacks() for automatic cost tracking
        """
        # Construct cost tracking tags

        tags = [
            f"user_sub:{user_sub}",
            f"conversation:{conversation_id}",
        ]

        # Add sub_agent tag if available from ContextVar (set by SubAgentIdMiddleware)
        if _has_sub_agent_id_contextvar and current_sub_agent_id:
            sub_agent_id = current_sub_agent_id.get()
            if sub_agent_id:
                tags.append(f"sub_agent:{sub_agent_id}")

        # Build configurable dict
        configurable = extra_configurable.copy()
        if thread_id is not None:
            configurable["thread_id"] = thread_id
        if checkpoint_ns is not None:
            configurable["checkpoint_ns"] = checkpoint_ns
        if checkpointer is not None:
            configurable["__pregel_checkpointer"] = checkpointer

        return {
            "configurable": configurable,
            "tags": tags,
            "callbacks": self.get_langchain_callbacks(),
        }

    async def flush_cost_tracking(self) -> None:
        """
        Force flush all pending cost records and shutdown the background worker.

        Call this during agent shutdown to ensure all records are sent and cleanup resources.
        """
        if self._cost_logger:
            await self._cost_logger.shutdown()
            logger.debug("Cost tracking flushed and shutdown")
