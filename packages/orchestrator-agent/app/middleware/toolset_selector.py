"""Toolset selector middleware for general-purpose agent's smart tool filtering.

This middleware implements toolset selection specifically for the general-purpose
(GP) agent.  It filters tools in two phases, **both executed inside the
middleware** (cached across model calls within the same GP invocation):

1. **Server-level filtering (Phase 1)**: When the total tool count exceeds
   ``TOOLSET_SELECTION_THRESHOLD`` (default 50), an LLM selects which MCP
   server slugs are relevant.  Tools are then filtered to only those from the
   selected servers.  Tools without ``server_name`` metadata (e.g. docstore,
   filesystem) are always kept.

2. **Tool-level selection (Phase 2)**: If the remaining tools still exceed
   ``TOOL_SELECTION_THRESHOLD`` (default 20), a second LLM call picks the most
   relevant individual tools.

Both phases are cached per-invocation via a mutable dict stored in a
``contextvars.ContextVar``.  This achieves two goals simultaneously:

1. **Cross-node persistence**: LangGraph copies context references for each node,
   so all nodes within one graph invocation share the same mutable dict object.
   Writes in the first model node are visible in subsequent model nodes.

2. **Concurrent-invocation isolation**: ``asyncio.gather`` (used by ToolNode for
   parallel tool calls) gives each task its own ContextVar binding.  Two parallel
   GP invocations get independent cache dicts.

Call ``clear_cache()`` before each new invocation to create a fresh dict in the
current task's context.

Architecture:
  - The orchestrator calls ``task(subagent_type="general-purpose", ...)``
  - ``DynamicToolDispatchMiddleware._adispatch_task_tool`` intercepts the call
  - The GP DynamicLocalAgentRunnable runs with this middleware in its extra_middlewares:
    1. ``ToolsetSelectorMiddleware`` — reads ALL tools from ``request.tools``,
       Phase 1 server selection + Phase 2 tool selection (both cached)
    2. Standard common middleware stack (FilesystemMiddleware, caching, retry, etc.)
"""

import contextvars
import logging
import textwrap
from collections.abc import Awaitable, Callable
from typing import Annotated

from agent_common.core.model_factory import create_model, get_default_fast_model, require_default_model
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.constants import TAG_NOSTREAM
from pydantic import BaseModel, Field
from ringier_a2a_sdk.cost_tracking import CostLogger, CostTrackingCallback

from ..models.config import AgentSettings

logger = logging.getLogger(__name__)

# Per-invocation cache using a mutable container in a ContextVar.
# Why this pattern:
# 1. asyncio.gather (used by LangGraph ToolNode for parallel tool calls) gives each
#    task its own context copy — so concurrent GP invocations are isolated.
# 2. LangGraph copies context for each node, but all nodes within one invocation
#    share the SAME mutable dict object (reference is copied, not the dict).
#    This means writes in one node (model call 1) are visible in subsequent nodes
#    (model call 2), solving the cross-node persistence problem.
# 3. clear_cache() creates a NEW dict in the current task's context, breaking the
#    link with any previously shared dict.
_cache_var: contextvars.ContextVar[dict[str, list]] = contextvars.ContextVar("toolset_selector_cache")


class ServerSelection(BaseModel):
    """Selected MCP servers for the task."""

    servers: Annotated[list[str], Field(description="List of selected MCP server slugs")]


class ToolSelection(BaseModel):
    """Selected tools for the task."""

    tools: Annotated[list[str], Field(description="List of selected tool names in order of relevance")]


class ToolsetSelectorMiddleware(AgentMiddleware[AgentState, None]):
    """Middleware for smart toolset selection in the general-purpose agent.

    Filters tools in two phases (both executed here, cached across model calls):
    1. Server-level (Phase 1): LLM selects relevant MCP server slugs when total
       tool count > ``TOOLSET_SELECTION_THRESHOLD``.
    2. Tool-level (Phase 2): LLM selects individual tools when remaining
       count > ``TOOL_SELECTION_THRESHOLD``.

    Always includes static orchestrator tools (time, presigned_url, docstore).

    Cache uses a ``ContextVar`` holding a **mutable dict** to achieve both:
    - Cross-node persistence (LangGraph nodes share the same dict reference)
    - Concurrent-invocation isolation (each ``asyncio.Task`` from gather has
      its own ContextVar binding, set by ``clear_cache()`` before graph execution)
    """

    def __init__(
        self,
        always_include: list[str] | None = None,
        cost_logger: CostLogger | None = None,
        compression_server_slug: str | None = None,
    ):
        """Initialize the toolset selector.

        Args:
            always_include: Tool names to always include regardless of filtering.
                These are essential orchestrator tools that GP agent needs.
            cost_logger: Optional CostLogger for tracking tool-selection LLM costs.
            compression_server_slug: Gatana compression server slug. Tools from
                this server are auto-included when any selected tool comes from
                a compression-enabled MCP server.
        """
        super().__init__()
        self.always_include = always_include or []
        self._cost_logger = cost_logger
        self._compression_server_slug = compression_server_slug

    def clear_cache(self) -> None:
        """Reset cache for the current invocation.

        Must be called BEFORE graph execution starts (in _astream_impl) so that
        the fresh dict is visible to all LangGraph nodes via context copy.
        """
        _cache_var.set({})

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage:
        """Filter tools before model call based on server selection and LLM selection.

        Args:
            request: Model request with tools
            handler: Async callback to execute the model

        Returns:
            Model response from handler with filtered tools
        """
        # Get all available tools from request.tools
        all_tools: list[BaseTool] = []
        for tool in request.tools or []:
            if isinstance(tool, BaseTool):
                all_tools.append(tool)

        if not all_tools:
            return await handler(request)

        # Use cached selection from a previous model call in this GP invocation
        # (the graph makes multiple model calls in a loop; avoid re-running LLMs)
        cache = _cache_var.get({})
        cached = cache.get("tools")
        if cached is not None:
            filtered_tools = cached
            logger.debug(f"ToolsetSelector: Using cached selection ({len(filtered_tools)} tools)")
        else:
            filtered_tools = await self._select_tools(all_tools, request.messages)
            # Cache into the mutable dict — visible to subsequent nodes
            cache["tools"] = filtered_tools

        # Always include essential orchestrator tools (even if not in filtered set)
        filtered_names = {t.name for t in filtered_tools}
        always_included = [
            tool for tool in all_tools if tool.name in self.always_include and tool.name not in filtered_names
        ]
        filtered_tools.extend(always_included)
        filtered_names.update(t.name for t in always_included)

        # Auto-include all tools from the Gatana compression server when any
        # selected tool is from a compression-enabled server
        if self._compression_server_slug:
            has_compression_server = any(
                tool.metadata and tool.metadata.get("compression_enabled") for tool in filtered_tools
            )
            if has_compression_server:
                compression_tools = [
                    tool
                    for tool in all_tools
                    if tool.metadata
                    and tool.metadata.get("server_name") == self._compression_server_slug
                    and tool.name not in filtered_names
                ]
                if compression_tools:
                    logger.debug(
                        f"ToolsetSelector: Auto-including {len(compression_tools)} tools from "
                        f"compression server '{self._compression_server_slug}'"
                    )
                    filtered_tools.extend(compression_tools)

        # Preserve provider-specific tool dicts from original request
        provider_tools = [tool for tool in request.tools if isinstance(tool, dict)]

        # Override request with filtered tools
        modified_request = request.override(tools=[*filtered_tools, *provider_tools])
        return await handler(modified_request)

    async def _select_tools(
        self,
        all_tools: list[BaseTool],
        messages: list,
    ) -> list[BaseTool]:
        """Run Phase 1 (server selection) and Phase 2 (tool selection) in sequence.

        Phase 1 triggers when total tool count > TOOLSET_SELECTION_THRESHOLD.
        Phase 2 triggers when remaining tool count > TOOL_SELECTION_THRESHOLD.
        Both phases use the same lightweight LLM (the fleet's cheap chat tier).

        Args:
            all_tools: All available tools (MCP + base)
            messages: Conversation messages (for LLM context)

        Returns:
            Filtered list of tools
        """
        filtered_tools = all_tools

        # Phase 1: Server-level selection
        server_threshold = AgentSettings.TOOLSET_SELECTION_THRESHOLD
        if len(filtered_tools) > server_threshold:
            logger.info(
                f"ToolsetSelector Phase 1: {len(filtered_tools)} tools > threshold ({server_threshold}), "
                "performing LLM server selection"
            )
            filtered_tools = await self._llm_select_servers(filtered_tools, messages)
            logger.info(f"ToolsetSelector Phase 1: Filtered to {len(filtered_tools)} tools")

        # Phase 2: Tool-level selection
        tool_threshold = AgentSettings.TOOL_SELECTION_THRESHOLD
        if len(filtered_tools) > tool_threshold:
            logger.info(
                f"ToolsetSelector Phase 2: {len(filtered_tools)} tools > threshold ({tool_threshold}), "
                "performing LLM tool selection"
            )
            filtered_tools = await self._llm_select_tools(
                filtered_tools,
                messages,
                max_tools=tool_threshold,
            )
            logger.info(f"ToolsetSelector Phase 2: LLM selected {len(filtered_tools)} tools")

        return filtered_tools

    async def _llm_select_servers(
        self,
        tools: list[BaseTool],
        messages: list,
    ) -> list[BaseTool]:
        """Phase 1: Use LLM to select relevant MCP servers, then filter tools.

        Groups tools by their ``server_name`` metadata, asks an LLM to pick the
        relevant servers, and returns only tools from those servers.  Tools
        without server metadata (static/base tools) are always kept.

        Args:
            tools: All available tools
            messages: Conversation messages (for LLM context)

        Returns:
            Tools from selected servers + tools without server metadata
        """
        # Group tools by server slug
        server_tools: dict[str, list[str]] = {"base": []}
        for tool in tools:
            server_slug = tool.metadata.get("server_name") if tool.metadata else None
            if server_slug:
                server_tools.setdefault(server_slug, []).append(tool.name)
            else:
                server_tools["base"].append(tool.name)

        # Only one real server (+ base) → nothing to select
        real_servers = [s for s in server_tools if s != "base"]
        if len(real_servers) <= 1:
            logger.debug(f"ToolsetSelector Phase 1: Only {len(real_servers)} server(s), skipping selection")
            return tools

        try:
            last_user_message = self._get_last_user_message(messages)
            if not last_user_message:
                return tools

            model = self._create_selection_model()

            server_list_str = "\n".join(
                f"- {slug}: {len(tool_names)} tools ({', '.join(tool_names[:5])}{'...' if len(tool_names) > 5 else ''})"
                for slug, tool_names in server_tools.items()
            )

            system_prompt = textwrap.dedent(f"""\
                You are selecting relevant MCP server toolsets for a task.

                Available MCP Servers:
                {server_list_str}

                Select the MCP server slugs that are most relevant to accomplishing
                the user's task. Return ONLY the selected server slugs as a JSON
                array of strings.""")

            structured_model = model.with_structured_output(ServerSelection)
            response: ServerSelection = await structured_model.ainvoke(  # type: ignore[assignment]
                [
                    {"role": "system", "content": system_prompt},
                    last_user_message,
                ],
                config={"tags": [TAG_NOSTREAM]},
            )

            selected_slugs = set(response.servers) | {"base"}  # always keep base
            logger.info(f"ToolsetSelector Phase 1: LLM selected servers: {response.servers}")

            filtered = []
            for tool in tools:
                server_slug = tool.metadata.get("server_name") if tool.metadata else None
                if server_slug in selected_slugs or not server_slug:
                    filtered.append(tool)
            return filtered

        except Exception as e:
            logger.error(f"Server selection failed: {e}", exc_info=True)
            return tools  # fallback: keep all tools

    def _get_last_user_message(self, messages: list) -> HumanMessage | None:
        """Extract the last user message from the conversation."""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                return message
        logger.warning("No user message found for tool selection")
        return None

    def _create_selection_model(self):
        """Create a lightweight LLM for selection tasks, with cost tracking."""
        callbacks = []
        if self._cost_logger:
            callbacks.append(CostTrackingCallback(self._cost_logger))

        return create_model(
            model_type=get_default_fast_model() or require_default_model(),
            thinking_level=None,
            callbacks=callbacks if callbacks else None,
        )

    async def _llm_select_tools(
        self,
        tools: list[BaseTool],
        messages: list,
        max_tools: int,
    ) -> list[BaseTool]:
        """Use LLM to select most relevant tools for the task.

        Args:
            tools: Available tools to select from
            messages: Conversation messages (for context)
            max_tools: Maximum number of tools to select

        Returns:
            Selected tools (subset of input tools)
        """
        try:
            # Get last user message for context
            last_user_message = self._get_last_user_message(messages)
            if not last_user_message:
                return tools[:max_tools]

            # Build tool list for LLM
            tool_list_str = "\n".join(f"- {tool.name}: {tool.description or 'No description'}" for tool in tools)

            system_prompt = textwrap.dedent(f"""\
                You are selecting the most relevant tools for a task.

                Available Tools:
                {tool_list_str}

                Select up to {max_tools} tools that are relevant to the user's task.
                Only select tools that are actually needed — do NOT pad the list.
                Return tool names in order of relevance (most relevant first).""")

            model = self._create_selection_model()
            structured_model = model.with_structured_output(ToolSelection)

            response: ToolSelection = await structured_model.ainvoke(  # type: ignore[assignment]
                [
                    {"role": "system", "content": system_prompt},
                    last_user_message,
                ],
                config={"tags": [TAG_NOSTREAM]},
            )

            # Filter tools to selected ones (preserve order from LLM)
            selected_tools = []
            for name in response.tools:
                for tool in tools:
                    if tool.name == name and tool not in selected_tools:
                        selected_tools.append(tool)
                        break
                if len(selected_tools) >= max_tools:
                    break

            if not selected_tools:
                logger.warning("LLM selected no valid tools, falling back to first N")
                return tools[:max_tools]

            return selected_tools

        except Exception as e:
            logger.error(f"LLM tool selection failed: {e}", exc_info=True)
            # Fallback to first N tools
            return tools[:max_tools]
