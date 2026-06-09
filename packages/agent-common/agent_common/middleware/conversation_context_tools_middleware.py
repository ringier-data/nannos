"""Conversation-context tool gate.

Some tools only make sense in a specific *conversation context* (the runtime
situation a turn executes in), which is a distinct axis from resource
visibility. See ``agent_common/core/CONTEXT.md`` for the full glossary.

Conversation context is derived from the runtime metadata key
``scope ∈ {"personal", "channel"}`` (set by the orchestrator executor):

- ``scope == "channel"`` → domain context **channel** (a multi-user surface).
- anything else (``"personal"``, missing) → domain context **direct** (a 1:1
  conversation: web/Slack/Chat DM, email, or a scheduled background run).

This middleware is an **additive** gate: gated tools are *not* carried in any
static tool list. They are injected into the bound tool set only when the
current conversation context allows them, and removed otherwise. This keeps the
static tool lists honest — a tool that only makes sense in channels is never
advertised in a direct conversation, so the model never emits a spurious call
(and never triggers a spurious HITL prompt).

The first gated tool is ``read_personal_file`` (allowed only in ``channel``):
reading a user's personal files from a shared channel is the only context where
a cross-namespace personal read is meaningful, and it is HITL-guarded.
"""

import logging
from typing import Awaitable, Callable, NamedTuple

from langchain.agents.middleware.types import AgentMiddleware, ModelCallResult, ModelRequest, ModelResponse
from langchain_core.tools import BaseTool
from langgraph.config import get_config

logger = logging.getLogger(__name__)


class ContextGatedTool(NamedTuple):
    """A tool that is only bound in specific conversation contexts.

    Attributes:
        tool: The tool instance to inject when the context matches.
        allowed_contexts: The conversation contexts (subset of
            ``{"direct", "channel"}``) in which the tool should be bound.
    """

    tool: BaseTool
    allowed_contexts: frozenset[str]


def _derive_context() -> str:
    """Derive the conversation context from runtime metadata.

    Returns:
        ``"channel"`` when ``metadata["scope"] == "channel"``, otherwise
        ``"direct"`` (the domain term for the ``"personal"`` scope value and the
        default when no scope is present).
    """
    config = get_config()
    if not config:
        return "direct"
    scope = config.get("metadata", {}).get("scope")
    return "channel" if scope == "channel" else "direct"


class ConversationContextToolsMiddleware(AgentMiddleware):
    """Inject/strip conversation-context-gated tools at model-call time.

    Generic: configured with a list of :class:`ContextGatedTool` instances (fixed
    tool objects, the per-agent case) and/or ``runtime_gated_tools`` (tool *names*
    resolved from the runtime ``tool_registry`` at model-call time, for graphs that
    are shared across users and therefore cannot hold a per-user tool instance —
    e.g. the orchestrator's single-graph-per-model). At each model call it rebuilds
    ``request.tools`` so that exactly the gated tools allowed in the current
    conversation context are present (and any stray bound copy of a gated tool is
    removed for de-duplication).
    """

    def __init__(
        self,
        context_gated_tools: list[ContextGatedTool] | None = None,
        runtime_gated_tools: dict[str, frozenset[str]] | None = None,
    ) -> None:
        """Initialize the gate.

        Args:
            context_gated_tools: Fixed tool instances to gate by conversation
                context (used when the tool object is known at graph-build time).
            runtime_gated_tools: Mapping of tool *name* to its allowed contexts.
                The tool instance is resolved from ``request.runtime.context``'s
                ``tool_registry`` at model-call time. Use this for graphs shared
                across users where the per-user tool instance is only available at
                invocation time (e.g. the orchestrator main graph).
        """
        super().__init__()
        self._gated_tools = context_gated_tools or []
        self._runtime_gated_tools = runtime_gated_tools or {}
        self._gated_names = {g.tool.name for g in self._gated_tools} | set(self._runtime_gated_tools)

    @staticmethod
    def _runtime_tool_registry(request: ModelRequest) -> dict:
        """Best-effort lookup of the runtime ``tool_registry`` (duck-typed).

        Keeps this middleware generic: it does not depend on any concrete runtime
        context type, only on the presence of a ``tool_registry`` dict attribute.
        """
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        registry = getattr(context, "tool_registry", None)
        return registry if isinstance(registry, dict) else {}

    def _resolve_gated_tools(self, request: ModelRequest) -> list[ContextGatedTool]:
        """Combine fixed gated tools with runtime-resolved ones for this call."""
        if not self._runtime_gated_tools:
            return self._gated_tools

        registry = self._runtime_tool_registry(request)
        resolved: list[ContextGatedTool] = []
        for name, contexts in self._runtime_gated_tools.items():
            tool = registry.get(name)
            if isinstance(tool, BaseTool):
                resolved.append(ContextGatedTool(tool, contexts))
            else:
                logger.debug(
                    "ConversationContextToolsMiddleware: runtime gated tool '%s' not found in tool_registry; skipping",
                    name,
                )
        return [*self._gated_tools, *resolved]

    def _apply(self, request: ModelRequest) -> ModelRequest:
        """Rebuild the bound tool set for the current conversation context."""
        context = _derive_context()
        effective = self._resolve_gated_tools(request)

        # Drop any bound copy of a gated tool (de-dup); keep everything else
        # (including provider-specific tool dicts) untouched.
        retained = [
            tool
            for tool in (request.tools or [])
            if not (isinstance(tool, BaseTool) and tool.name in self._gated_names)
        ]

        # Inject the gated tools allowed in this context.
        injected = [g.tool for g in effective if context in g.allowed_contexts]
        if injected:
            logger.debug(
                "ConversationContextToolsMiddleware: injecting %s for context '%s'",
                [t.name for t in injected],
                context,
            )

        if not injected and len(retained) == len(request.tools or []):
            # No change (nothing to inject, nothing stripped) — avoid override.
            return request

        return request.override(tools=[*retained, *injected])

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Apply the context gate before the model call."""
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of :meth:`wrap_model_call`."""
        return await handler(self._apply(request))
