"""Playbook injection middleware for the orchestrator agent.

Reads the orchestrator's AGENTS.md from the user's persistent filesystem
and appends it to the system prompt on each model call.

This is the orchestrator-side equivalent of DynamicLocalAgentRunnable._build_playbook_addendum().
Sub-agents load playbooks in _ensure_agent(); the orchestrator loads them per-call via this middleware.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_common.core.playbook_reader import PlaybookReaderService
from agent_common.middleware.utils import append_to_system_message
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)

# Agent name used for the orchestrator's own playbook (AGENTS.md)
ORCHESTRATOR_PLAYBOOK_NAME = "orchestrator"


class PlaybookInjectionMiddleware(AgentMiddleware[AgentState, GraphRuntimeContext]):
    """Middleware that injects playbook content into the orchestrator's system prompt.

    On each model call, reads AGENTS.md from the user's personal and group
    namespaces for the orchestrator and appends the content to the system prompt.

    Requires a document store to be configured. If no store is available,
    passes through without modification.
    """

    state_schema = AgentState
    tools: list[Any] = []

    def __init__(self, store: Any = None):
        """Initialize with optional store reference.

        Args:
            store: AsyncPostgresStore instance. If None, middleware is a no-op.
        """
        self._store = store
        self._reader: PlaybookReaderService | None = None
        if store:
            self._reader = PlaybookReaderService(store)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Sync wrap - delegates to async version since store reads are async."""
        # PlaybookReaderService requires async; sync path passes through
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Inject orchestrator playbook into system prompt.

        Args:
            request: Model request containing the system prompt
            handler: Async callback to execute the model

        Returns:
            Model response
        """
        if not self._reader:
            return await handler(request)

        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            return await handler(request)

        addendum = await self._build_playbook_addendum(user_context)
        if addendum:
            new_system_message = append_to_system_message(request.system_message, addendum)
            request = request.override(system_message=new_system_message)

        return await handler(request)

    async def _build_playbook_addendum(self, user_context: GraphRuntimeContext) -> str:
        """Build playbook addendum for the orchestrator.

        Args:
            user_context: Runtime context with user_id and groups

        Returns:
            Formatted string to append to system prompt, or empty string
        """
        user_id = user_context.user_id
        groups = getattr(user_context, "groups", None)
        group_ids = groups if groups else None

        logger.debug(f"PlaybookInjectionMiddleware: Loading playbooks for user_id={user_id}, group_ids={group_ids}")

        parts: list[str] = []

        try:
            # Load AGENTS.md for the orchestrator
            group_content, personal_content = await self._reader.read_agents_md(
                user_id=user_id,
                agent_name=ORCHESTRATOR_PLAYBOOK_NAME,
                group_ids=group_ids,
            )

            if group_content:
                parts.append(f"<group_playbook>\n{group_content}\n</group_playbook>")

            if personal_content:
                parts.append(f"<personal_playbook>\n{personal_content}\n</personal_playbook>")

            if group_content and personal_content:
                parts.append(
                    "<playbook_conflict_resolution>\n"
                    "If the personal playbook contradicts the group playbook, follow the personal playbook.\n"
                    "</playbook_conflict_resolution>"
                )

        except Exception as e:
            logger.warning(f"PlaybookInjectionMiddleware: Failed to load playbooks for user {user_id}: {e}")
            return ""

        if not parts:
            return ""

        return "\n\n" + "\n".join(parts)
