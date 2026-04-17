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

Both phases are cached on ``GraphRuntimeContext._cached_selected_tools`` so
that subsequent model calls within the same GP invocation skip the LLM calls.

**INTEGRATION STATUS: ACTIVE**

This middleware runs inside the custom GP graph created by
:meth:`GraphFactory._create_gp_graph` (using ``langchain.agents.create_agent``).
The custom GP graph bypasses deepagents' built-in GP, which has limitations
(no context_schema, no MCP tools, no customizable middleware).

Architecture:
  - The orchestrator calls ``task(subagent_type="general-purpose", ...)``
  - ``DynamicToolDispatchMiddleware._adispatch_task_tool`` intercepts the call
  - The GP graph runs with this middleware in its stack:
    1. ``ToolsetSelectorMiddleware`` — reads ALL MCP tools from ``tool_registry``,
       Phase 1 server selection + Phase 2 tool selection (both cached)
    2. ``DynamicToolDispatchMiddleware(skip_tool_injection=True)`` — converts tools
       to dicts for model binding (Gemini compatibility), handles MCP tool execution
    3. ``ToolRetryMiddleware`` — retries on transient errors
"""

import logging
import textwrap
from collections.abc import Awaitable, Callable
from typing import Annotated

from agent_common.core.model_factory import create_model
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

from ..models.config import AgentSettings, GraphRuntimeContext

logger = logging.getLogger(__name__)


class ServerSelection(BaseModel):
    """Selected MCP servers for the task."""

    servers: Annotated[list[str], Field(description="List of selected MCP server slugs")]


class ToolSelection(BaseModel):
    """Selected tools for the task."""

    tools: Annotated[list[str], Field(description="List of selected tool names in order of relevance")]


class ToolsetSelectorMiddleware(AgentMiddleware[AgentState, GraphRuntimeContext]):
    """Middleware for smart toolset selection in the general-purpose agent.

    Filters tools in two phases (both executed here, cached across model calls):
    1. Server-level (Phase 1): LLM selects relevant MCP server slugs when total
       tool count > ``TOOLSET_SELECTION_THRESHOLD``.
    2. Tool-level (Phase 2): LLM selects individual tools when remaining
       count > ``TOOL_SELECTION_THRESHOLD``.

    Always includes static orchestrator tools (time, presigned_url, docstore).
    """

    def __init__(self, always_include: list[str] | None = None, cost_logger: CostLogger | None = None):
        """Initialize the toolset selector.

        Args:
            always_include: Tool names to always include regardless of filtering.
                These are essential orchestrator tools that GP agent needs.
            cost_logger: Optional CostLogger for tracking tool-selection LLM costs.
        """
        super().__init__()
        self.always_include = always_include or []
        self._cost_logger = cost_logger

    async def awrap_model_call(
        self,
        request: ModelRequest[GraphRuntimeContext],
        handler: Callable[[ModelRequest[GraphRuntimeContext]], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage:
        """Filter tools before model call based on server selection and LLM selection.

        Args:
            request: Model request with user context
            handler: Async callback to execute the model

        Returns:
            Model response from handler with filtered tools
        """
        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning("ToolsetSelectorMiddleware: context is not GraphRuntimeContext, skipping")
            return await handler(request)

        # Get all available tools from user context (MCP tools + request tools)
        # MCP tools are in tool_registry, request.tools has base tools (task, write_todos, etc.)
        all_tools: list[BaseTool] = []
        seen_names: set[str] = set()

        # Add MCP tools from tool_registry (discovered tools with server metadata)
        for tool_name, tool in user_context.tool_registry.items():
            if isinstance(tool, BaseTool) and tool_name not in seen_names:
                all_tools.append(tool)
                seen_names.add(tool_name)

        # Add base tools from request that aren't already included
        for tool in request.tools or []:
            if isinstance(tool, BaseTool) and tool.name not in seen_names:
                all_tools.append(tool)
                seen_names.add(tool.name)

        if not all_tools:
            return await handler(request)

        # Use cached selection from a previous model call in this GP invocation
        # (the graph makes multiple model calls in a loop; avoid re-running LLMs)
        cached_tools = getattr(user_context, "_cached_selected_tools", None)
        if cached_tools is not None:
            filtered_tools = cached_tools
            logger.debug(f"ToolsetSelector: Using cached selection ({len(filtered_tools)} tools)")
        else:
            filtered_tools = await self._select_tools(all_tools, request.messages)
            # Cache the result for subsequent model calls in this invocation
            user_context._cached_selected_tools = filtered_tools

        # Always include essential orchestrator tools (even if not in filtered set)
        always_included = [
            tool for tool in all_tools if tool.name in self.always_include and tool not in filtered_tools
        ]
        filtered_tools.extend(always_included)

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
        Both phases use the same lightweight LLM (TOOLSET_SELECTION_MODEL).

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
            model_type=AgentSettings.TOOLSET_SELECTION_MODEL,
            bedrock_region=AgentSettings.get_bedrock_region(),
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
