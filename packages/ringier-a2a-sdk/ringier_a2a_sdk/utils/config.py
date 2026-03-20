"""Utility for creating standardized RunnableConfig objects.

This module provides a centralized function for creating LangChain RunnableConfig
objects with consistent structure for cost tracking, metadata, and checkpointing
across orchestrator and all sub-agents.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Import ContextVar for sub_agent_id (used by remote agents)
try:
    from ..middleware.sub_agent_id_middleware import current_sub_agent_id

    _has_sub_agent_id_contextvar = True
except ImportError:
    current_sub_agent_id = None
    _has_sub_agent_id_contextvar = False


def create_runnable_config(
    user_sub: str,
    conversation_id: str,
    user_id: str,
    assistant_id: str,
    thread_id: Optional[str] = None,
    checkpoint_ns: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    scheduled_job_id: Optional[int] = None,
    sub_agent_id: Optional[int] = None,
    cost_logger: Optional[Any] = None,
    **extra_configurable,
) -> Dict[str, Any]:
    """
    Create a standardized RunnableConfig for LangGraph agent invocations.

    This utility function creates a properly formatted RunnableConfig dict with:
    - Cost tracking tags (user_sub, conversation, sub_agent, scheduled_job)
    - LangChain callbacks for automatic cost tracking (if cost_logger provided)
    - Metadata for document store namespace isolation (user_id, assistant_id)
    - Checkpoint configuration for multi-turn conversations

    This is the single source of truth for RunnableConfig creation across the system.
    Both the orchestrator and all sub-agents should use this to ensure consistency.

    Args:
        user_sub: User's OIDC subject identifier for cost attribution
        conversation_id: Conversation ID for cost attribution and tool result scoping
        user_id: User's stable database ID for document store namespace (user-scoped files)
        assistant_id: Assistant ID for filesystem namespace (channel-scoped or personal files)
        thread_id: Thread ID for checkpointing (defaults to conversation_id)
        checkpoint_ns: Checkpoint namespace for isolation (e.g., "general-purpose", "task-scheduler")
        checkpointer: Checkpointer instance (for __pregel_checkpointer)
        scheduled_job_id: Optional scheduled job ID for cost attribution
        sub_agent_id: Optional sub-agent ID for cost attribution (explicit override;
            if not provided, falls back to ContextVar from SubAgentIdMiddleware)
        cost_logger: Optional CostLogger instance for generating callbacks
        **extra_configurable: Additional configurable parameters

    Returns:
        Dict with 'configurable', 'tags', 'callbacks', and 'metadata' keys

    Usage in orchestrator (_adispatch_task_tool):
        ```python
        from ringier_a2a_sdk.utils import create_runnable_config

        config = create_runnable_config(
            user_sub=user_context.user_sub,
            conversation_id=orchestrator_conversation_id,
            user_id=user_context.user_id,
            assistant_id=parent_config["metadata"]["assistant_id"],
            thread_id=f"{conversation_id}::general-purpose",
            checkpoint_ns="general-purpose",
            checkpointer=checkpointer,
            cost_logger=cost_logger,
        )
        await runnable.ainvoke(state, config)
        ```

    Usage in sub-agents (for nested invocations):
        ```python
        # Inherit parent config and override only isolation parameters
        nested_config = {
            **parent_config,
            "configurable": {
                **parent_config.get("configurable", {}),
                "thread_id": f"{conversation_id}::nested-agent",
                "checkpoint_ns": "nested-agent",
            }
        }
        await graph.ainvoke(messages, nested_config)
        ```

    Note:
        - Automatically adds user_sub:{user_sub} and conversation:{conversation_id} tags
        - Automatically adds sub_agent:{sub_agent_id} tag if provided explicitly or from ContextVar
        - Automatically adds scheduled_job:{id} tag when scheduled_job_id is provided
        - Includes cost tracking callbacks if cost_logger provided
        - Metadata includes conversation_id, user_id, assistant_id for storage backends
    """
    # Construct cost tracking tags
    tags = [
        f"user_sub:{user_sub}",
        f"conversation:{conversation_id}",
    ]

    # Add sub_agent tag: explicit parameter takes precedence over ContextVar
    resolved_sub_agent_id = sub_agent_id
    if resolved_sub_agent_id is None and _has_sub_agent_id_contextvar and current_sub_agent_id:
        resolved_sub_agent_id = current_sub_agent_id.get()
    if resolved_sub_agent_id is not None:
        tags.append(f"sub_agent:{resolved_sub_agent_id}")

    # Add scheduled_job tag when running as part of a scheduled job
    if scheduled_job_id is not None:
        tags.append(f"scheduled_job:{scheduled_job_id}")

    # Build configurable dict
    configurable = extra_configurable.copy()
    if thread_id is not None:
        configurable["thread_id"] = thread_id
    if checkpoint_ns is not None:
        configurable["checkpoint_ns"] = checkpoint_ns
    if checkpointer is not None:
        configurable["__pregel_checkpointer"] = checkpointer

    # Build metadata dict for storage backends (IndexingStoreBackend, FilesystemMiddleware)
    # CRITICAL: These values must be consistent across orchestrator and all sub-agents
    # to ensure files written by one agent can be read by another in the same namespace.
    metadata = {
        "conversation_id": conversation_id,  # Required for conversation-scoped tool results
        "user_id": user_id,  # Stable database ID for user-scoped files
        "assistant_id": assistant_id,  # For channel-scoped files (channel_id or user_id)
    }

    # Build callbacks list
    callbacks = []
    if cost_logger is not None:
        try:
            from ..cost_tracking import CostTrackingCallback

            callbacks = [CostTrackingCallback(cost_logger, sub_agent_id=resolved_sub_agent_id)]
        except ImportError:
            logger.debug("CostTrackingCallback not available, skipping callback generation")

    return {
        "configurable": configurable,
        "tags": tags,
        "callbacks": callbacks,
        "metadata": metadata,
    }
