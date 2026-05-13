"""Playbook injection middleware for the orchestrator agent.

Reads the orchestrator's AGENTS.md and skill index from the user's persistent
filesystem and appends them to the system prompt on each model call.

This is the orchestrator-side equivalent of DynamicLocalAgentRunnable._build_playbook_addendum().
Sub-agents load playbooks in _ensure_agent(); the orchestrator loads them per-call via this middleware.
"""

import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_common.backends.skills_store import SkillsStoreBackend
from agent_common.core.playbook_reader import PlaybookReaderService
from agent_common.core.skills_resolver import resolve_skills_for_agent
from agent_common.middleware.utils import append_to_system_message
from agent_common.models.skill import ResolvedSkill
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage as LcToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)

# Agent name used for the orchestrator's own playbook
ORCHESTRATOR_PLAYBOOK_NAME = "orchestrator"

# Context variable holding resolved skills for the current request.
# Set by PlaybookInjectionMiddleware in awrap_tool_call, read by LazySkillsBackend.
_resolved_skills_var: contextvars.ContextVar[dict[str, ResolvedSkill]] = contextvars.ContextVar(
    "_resolved_skills", default={}
)


class LazySkillsBackend(SkillsStoreBackend):
    """SkillsStoreBackend that reads from a request-scoped context var.

    Mounted at /skills/ in the orchestrator's CompositeBackend at graph creation.
    PlaybookInjectionMiddleware resolves skills per-request and populates the
    context var so ls/read_file calls see the correct user-scoped skills.
    """

    def __init__(self) -> None:
        # Skip SkillsStoreBackend.__init__() — skills come from context var
        pass

    @property  # type: ignore[override]
    def _skills(self) -> dict[str, ResolvedSkill]:
        return _resolved_skills_var.get()

    @_skills.setter
    def _skills(self, _value: Any) -> None:
        pass  # Managed by context var, not instance state


class PlaybookInjectionMiddleware(AgentMiddleware[AgentState, GraphRuntimeContext]):
    """Middleware that injects playbook content into the orchestrator's system prompt.

    On each model call:
    1. Reads AGENTS.md from the user's personal and group namespaces for the orchestrator
    2. Lists available skills and builds an index
    3. Appends all content to the system prompt

    Also caches resolved skills per user and re-sets the context var in
    awrap_tool_call so that LazySkillsBackend can serve ls/read_file calls
    in the tool node (which runs in a different context than the model node).

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
        # Cache resolved skills per user_id so awrap_tool_call can re-set
        # the context var in the tool execution context.
        self._skills_cache: dict[str, dict[str, ResolvedSkill]] = {}

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Sync wrap - delegates to async version since store reads are async."""
        # PlaybookReaderService requires async; sync path passes through
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[LcToolMessage]],
    ) -> LcToolMessage:
        """Re-set the resolved skills context var for tool execution.

        Context vars set in awrap_model_call don't propagate to the tool node
        in LangGraph. This hook runs in the tool execution context, so setting
        the var here makes it visible to LazySkillsBackend.
        """
        user_context = request.runtime.context
        if isinstance(user_context, GraphRuntimeContext) and user_context.user_id:
            cached = self._skills_cache.get(user_context.user_id)
            if cached:
                _resolved_skills_var.set(cached)
        return await handler(request)

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
            # Load AGENTS.md
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

            # Resolve skills for both the system prompt index and the
            # filesystem backend (LazySkillsBackend reads from context var).
            resolved = await resolve_skills_for_agent(
                store=self._store,
                user_id=user_id,
                agent_name=ORCHESTRATOR_PLAYBOOK_NAME,
                group_ids=group_ids or [],
                default_skills=[],
            )
            # Cache for awrap_tool_call to re-set the context var in tool node.
            self._skills_cache[user_id] = resolved
            _resolved_skills_var.set(resolved)

            if resolved:
                skill_lines = [f"- `{s.name}` ({s.scope}): {s.description}" for s in resolved.values()]
                parts.append(
                    "<available_skills>\n"
                    "The following skills are available. Use read_file('/skills/{name}/SKILL.md') to load full details:\n"
                    + "\n".join(skill_lines)
                    + "\n</available_skills>"
                )

        except Exception as e:
            logger.warning(f"PlaybookInjectionMiddleware: Failed to load playbooks for user {user_id}: {e}")
            return ""

        if not parts:
            return ""

        return "\n\n" + "\n".join(parts)
