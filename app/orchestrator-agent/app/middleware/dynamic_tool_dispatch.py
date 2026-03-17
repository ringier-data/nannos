"""Dynamic tool dispatch middleware for runtime MCP tool injection and A2A subagent handling.

This middleware enables a SINGLE graph instance to serve ALL users with different
MCP tool configurations and A2A subagent registrations:

1. **wrap_model_call**: Merges and binds tools from multiple sources:
   - Original tools from create_agent (e.g., write_todos, task)
   - Static tools (e.g., FinalResponseSchema for Bedrock)
   - User's dynamic MCP tools from GraphRuntimeContext.tool_registry
   - Enhanced "task" tool that includes both SubAgentMiddleware's agents AND A2A subagents

2. **wrap_tool_call**: Routes tool execution appropriately:
   - Tools registered with ToolNode → pass to handler (standard execution)
   - Dynamic MCP tools not in ToolNode → execute from GraphRuntimeContext.tool_registry
   - "task" tool for A2A agents → dispatch from GraphRuntimeContext.subagent_registry
   - "task" tool for built-in deepagents sub-agents → fall through to SubAgentMiddleware's handler

Architecture:
- Graph is created with standard tools (write_todos, task, etc.)
- User MCP tools are stored in GraphRuntimeContext.tool_registry (discovered at runtime)
- A2A subagents are stored in GraphRuntimeContext.subagent_registry (discovered at runtime)
- Tools are converted to dict format for model binding (bypasses ToolNode validation)
- The task tool description is enhanced to include A2A agents alongside general-purpose

Key Insight: Dict tools bypass factory.py validation (line 907: `if isinstance(t, dict): continue`)
which allows runtime tool injection without graph recreation.
"""

import asyncio
import json
import logging
import textwrap
from collections.abc import Awaitable, Callable
from typing import Any, Optional, cast

from agent_common.core.model_factory import create_model
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ContentBlock, HumanMessage, TextContentBlock, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from langsmith import traceable
from ringier_a2a_sdk.cost_tracking.callback import CostTrackingCallback
from ringier_a2a_sdk.utils import create_runnable_config

from ..models.config import GraphRuntimeContext
from ..utils import CleanupLevel, validate_and_clean_tool_dict

logger = logging.getLogger(__name__)


class DynamicToolDispatchMiddleware(AgentMiddleware[AgentState, GraphRuntimeContext]):
    """Middleware for runtime MCP tool injection and A2A subagent handling.

    Enables per-user MCP tool injection and A2A subagent handling without graph recreation:

    1. **Model Binding** (wrap_model_call): Merges tools from original request,
       static tools, and GraphRuntimeContext.tool_registry into dict format for model binding.
       Also enhances the "task" tool description to include A2A subagents from subagent_registry.

    2. **Tool Dispatch** (wrap_tool_call): Routes tool execution:
       - ToolNode-registered tools → standard handler execution
       - Dynamic MCP tools → execute from GraphRuntimeContext.tool_registry
       - "task" tool for A2A agents → dispatch from GraphRuntimeContext.subagent_registry
       - "task" tool for built-in deepagents sub-agents → fall through to SubAgentMiddleware's handler

    Example:
        ```python
        # Graph can have standard tools; user MCP tools and A2A agents are added at runtime
        agent = create_agent(
            model=model,
            tools=[write_todos],  # Standard tools work normally
            middleware=[
                DynamicToolDispatchMiddleware(static_tools=[final_response]),
            ],
            context_schema=GraphRuntimeContext,
        )

        # User MCP tools and A2A subagents are added via GraphRuntimeContext at invocation time
        user_context = GraphRuntimeContext(
            user_id="user1",
            tool_registry={"mcp_tool": mcp_tool},
            subagent_registry={"jira-agent": jira_subagent},  # A2A agents
        )
        agent.invoke({"messages": [...]}, context=user_context)
        ```
    """

    state_schema = AgentState
    tools: list[BaseTool] = []  # No tools registered with middleware itself

    def __init__(
        self,
        static_tools: list[BaseTool] | None = None,
        skip_tool_injection: bool = False,
        agent_settings: Optional[Any] = None,
        cost_logger: Optional[Any] = None,
    ):
        """Initialize the middleware.

        Args:
            static_tools: Optional list of static tools that are always available
                regardless of user context (e.g., FinalResponseSchema for Bedrock).
            skip_tool_injection: If True, awrap_model_call only converts existing
                request.tools to dicts (schema cleaning) but does NOT inject tools
                from tool_registry. Set to True by the custom GP graph where
                ToolsetSelectorMiddleware handles tool injection and filtering.
                Tool execution (awrap_tool_call) still dispatches from tool_registry.
            agent_settings: AgentSettings for model configuration (needed for create_model)
            cost_logger: CostLogger for tracking LLM costs (needed for file filtering)
        """
        self.static_tools = {t.name: t for t in (static_tools or [])}
        self.skip_tool_injection = skip_tool_injection
        self.agent_settings = agent_settings
        self.cost_logger = cost_logger

    def _enhance_task_tool_schema(
        self, task_tool_dict: dict[str, Any], user_context: GraphRuntimeContext
    ) -> dict[str, Any]:
        """Enhance the task tool's description and subagent_type enum.

        All sub-agents (both local and remote A2A) are now in subagent_registry,
        so we simply build the enum from that unified registry.

        Args:
            task_tool_dict: The task tool in OpenAI dict format
            user_context: User context with subagent_registry

        Returns:
            Enhanced task tool dict with all subagents in enum
        """
        if not user_context.subagent_registry:
            return task_tool_dict

        # Build agent descriptions and names from unified registry
        agent_descriptions = []
        agent_names = []
        for name, subagent in user_context.subagent_registry.items():
            description = subagent.get("description", f"Agent: {name}")
            agent_descriptions.append(f"- {name}: {description}")
            agent_names.append(name)

        if not agent_names:
            return task_tool_dict

        # Get the current parameters schema
        function_dict = task_tool_dict.get("function", {})
        parameters = function_dict.get("parameters", {})
        properties = parameters.get("properties", {})
        subagent_type_prop = properties.get("subagent_type", {})
        original_description = function_dict.get("description", "")

        # Enhance the description with agent info
        agent_section = "\n\nAvailable agents:\n" + "\n".join(agent_descriptions)
        enhanced_description = original_description + agent_section

        # Create enhanced subagent_type property with enum
        enhanced_subagent_type = {
            **subagent_type_prop,
            "enum": agent_names,
        }

        # Build enhanced properties
        enhanced_properties = {
            **properties,
            "subagent_type": enhanced_subagent_type,
        }

        # Build enhanced parameters
        enhanced_parameters = {
            **parameters,
            "properties": enhanced_properties,
        }

        # Build enhanced function
        enhanced_function = {
            **function_dict,
            "description": enhanced_description,
            "parameters": enhanced_parameters,
        }

        # Create a copy with enhanced function
        enhanced_tool = {
            **task_tool_dict,
            "function": enhanced_function,
        }
        return enhanced_tool

    def _enhance_scheduler_create_job_schema(
        self, scheduler_tool_dict: dict[str, Any], user_context: GraphRuntimeContext
    ) -> dict[str, Any]:
        """Enhance scheduler_create_job tool's parameters with available options.

        This prevents the agent from hallucinating values by providing:
        - check_tool: guidance on discovering valid MCP tools hierarchically
        - sub_agent_id: description listing available sub-agents
        - delivery_channel_id: reminder to check available channels

        Args:
            scheduler_tool_dict: The scheduler_create_job tool in OpenAI dict format
            user_context: User context with tool_registry and subagent_registry

        Returns:
            Enhanced tool dict with improved parameter schemas
        """
        function_dict = scheduler_tool_dict.get("function", {})
        parameters = function_dict.get("parameters", {})
        properties = parameters.get("properties", {})

        enhanced_properties = dict(properties)  # Copy to avoid modifying original

        # 2. Enhance sub_agent_id description with available sub-agents
        if user_context.subagent_registry:
            # Filter to show only sub-agents that might be automated (exclude system agents)
            automated_agents = {
                name: agent
                for name, agent in user_context.subagent_registry.items()
                if name not in ["file-analyzer", "task-scheduler"]  # Exclude system agents
            }

            if automated_agents:
                sub_agent_id_prop = properties.get("sub_agent_id", {})
                agent_list = "\n".join(
                    [f"  - {name}: {agent.get('description', 'N/A')}" for name, agent in automated_agents.items()]
                )
                enhanced_properties["sub_agent_id"] = {
                    **sub_agent_id_prop,
                    "description": (
                        sub_agent_id_prop.get("description", "ID of an existing sub-agent to execute")
                        + f"\n\nAvailable sub-agents (use playground_list_sub_agents to get IDs):\n{agent_list}"
                    ),
                }

        # 3. Enhance delivery_channel_id description with note about available channels
        delivery_channel_id_prop = properties.get("delivery_channel_id", {})
        if delivery_channel_id_prop:
            enhanced_properties["delivery_channel_id"] = {
                **delivery_channel_id_prop,
                "description": (
                    delivery_channel_id_prop.get("description", "ID of a registered delivery channel")
                    + "\n\nNote: User must have configured delivery channels in Settings. "
                    "If not available, omit this field - users will receive in-app notifications."
                ),
            }

        # Build enhanced tool
        enhanced_parameters = {
            **parameters,
            "properties": enhanced_properties,
        }

        enhanced_function = {
            **function_dict,
            "parameters": enhanced_parameters,
        }

        return {
            **scheduler_tool_dict,
            "function": enhanced_function,
        }

    def _get_tools_as_dicts(
        self,
        user_context: GraphRuntimeContext,
        original_tools: list[Any] | None = None,
        level: CleanupLevel = CleanupLevel.MINIMAL,
        inject_from_registry: bool = True,
    ) -> list[dict[str, Any]]:
        """Get all tools available to the user as OpenAI-format dicts.

        Merges tools from multiple sources (in order of precedence):
        1. Original tools from request (e.g., write_todos, task from create_deep_agent)
           - The "task" tool is enhanced with A2A agent descriptions from subagent_registry
        2. Static tools from middleware initialization (e.g., FinalResponseSchema)
        3. User's dynamic tools from tool_registry (can override earlier tools)
           Only performed when ``inject_from_registry=True``.  Set to ``False`` when
           ``skip_tool_injection=True`` (GP graph path) so that
           ``ToolsetSelectorMiddleware`` retains sole responsibility for tool injection.

        Tools are converted to dict format to bypass LangGraph's tool validation.

        Args:
            user_context: User context containing tool_registry and subagent_registry
            original_tools: Original tools from the request (from create_agent/create_deep_agent)
            level: Cleanup level to apply to schemas
            inject_from_registry: Whether to add tools from tool_registry (step 3).
                Set to False on the skip_tool_injection path.

        Returns:
            List of tools in OpenAI function calling dict format
        """
        tool_dicts: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        # 1. Add original tools from the request first (e.g., write_todos, task)
        # These are tools that create_deep_agent provides by default
        # The "task" tool gets enhanced with A2A agent descriptions and enum
        for tool in original_tools or []:
            if isinstance(tool, BaseTool):
                if tool.name not in seen_names:
                    # Create a cleaned COPY for model binding (don't modify original)
                    # NOTE: clean_tool_schema modifies in-place, so we don't use it for registry tools
                    tool_dict = convert_to_openai_tool(tool)
                    tool_dict = validate_and_clean_tool_dict(tool_dict, level)
                    # Enhance task tool with A2A agents (description + enum)
                    if tool.name == "task":
                        tool_dict = self._enhance_task_tool_schema(tool_dict, user_context)
                    tool_dicts.append(tool_dict)
                    seen_names.add(tool.name)
            elif isinstance(tool, dict):
                name = tool.get("function", {}).get("name") or tool.get("name")
                if name and name not in seen_names:
                    # Validate and clean schema
                    tool = validate_and_clean_tool_dict(tool, level)
                    # Enhance task tool with A2A agents (description + enum)
                    if name == "task":
                        tool = self._enhance_task_tool_schema(tool, user_context)
                    tool_dicts.append(tool)
                    seen_names.add(name)

        # 2. Add static tools from middleware (e.g., FinalResponseSchema)
        for tool in self.static_tools.values():
            if tool.name not in seen_names:
                # Create a cleaned COPY for model binding (don't modify original)
                tool_dict = convert_to_openai_tool(tool)
                tool_dict = validate_and_clean_tool_dict(tool_dict, level)
                tool_dicts.append(tool_dict)
                seen_names.add(tool.name)

        # 3. Add user's dynamic tools (may override previous tools by name)
        # Skipped when inject_from_registry=False (GP / skip_tool_injection path where
        # ToolsetSelectorMiddleware handles injection).
        # CRITICAL: Do NOT modify the original tools in the registry
        # They need to remain intact for tool execution in wrap_tool_call
        # NOTE: Only include tools from the whitelist for orchestrator (GP agent gets separate filtering)
        if not inject_from_registry:
            return tool_dicts

        for name, tool in user_context.tool_registry.items():
            # Skip tools not in orchestrator's whitelist
            if name not in user_context.whitelisted_tool_names:
                continue

            if name in seen_names:
                # User tool overrides existing tool - remove old and add user's
                tool_dicts = [t for t in tool_dicts if t.get("function", {}).get("name") != name]
            if isinstance(tool, BaseTool):
                # Convert to dict for model binding (creates a copy, doesn't modify original)
                tool_dict = convert_to_openai_tool(tool)
                tool_dict = validate_and_clean_tool_dict(tool_dict, level)
                # Enhance scheduler_create_job tool with MCP tools enum
                if name == "scheduler_create_job":
                    tool_dict = self._enhance_scheduler_create_job_schema(tool_dict, user_context)
                tool_dicts.append(tool_dict)
            elif isinstance(tool, dict):
                # Already in dict format, but still validate and clean
                tool_dict = validate_and_clean_tool_dict(tool, level)
                # Enhance scheduler_create_job tool with MCP tools enum
                if name == "scheduler_create_job":
                    tool_dict = self._enhance_scheduler_create_job_schema(tool_dict, user_context)
                tool_dicts.append(tool_dict)
            seen_names.add(name)

        return tool_dicts

    def _lookup_tool(self, tool_name: str, user_context: GraphRuntimeContext) -> BaseTool | None:
        """Look up a tool by name from user context or static tools.

        Args:
            tool_name: Name of the tool to look up
            user_context: User context containing tool_registry

        Returns:
            BaseTool instance or None if not found
        """
        # First check user's dynamic tools
        tool = user_context.tool_registry.get(tool_name)
        if tool:
            return tool

        # Fall back to static tools
        return self.static_tools.get(tool_name)

    def _extract_subagent_response(self, result: Any, subagent_type: str) -> tuple[str, dict[str, Any] | None]:
        """Extract content and A2A metadata from subagent result.

        Takes the subagent's final message (last in messages list) and extracts:
        - The actual response content
        - A2A metadata if present (context_id, task_id, etc.)

        Args:
            result: The result dict from subagent invocation
            subagent_type: Name of the subagent (for logging)

        Returns:
            Tuple of (content string, a2a_metadata dict or None)
        """
        content = ""
        a2a_metadata = None

        if isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
            if messages:
                # Take only the last message - this is the subagent's final synthesized response.
                # The subagent may have had multiple internal turns (tool calls, reasoning),
                # but we only return the final answer to keep the orchestrator's context clean.
                raw_content = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])

                # Try to parse JSON-wrapped A2A metadata from content
                # Format: {"content": "...", "a2a": {...}}
                if isinstance(raw_content, str):
                    try:
                        content_dict = json.loads(raw_content)
                        if isinstance(content_dict, dict) and "content" in content_dict and "a2a" in content_dict:
                            content = content_dict["content"]
                            a2a_metadata = content_dict["a2a"]
                            logger.debug(
                                f"DynamicToolDispatchMiddleware: Extracted A2A metadata for {subagent_type}: "
                                f"context_id={a2a_metadata.get('context_id')}, task_id={a2a_metadata.get('task_id')}"
                            )
                        else:
                            content = raw_content
                    except json.JSONDecodeError:
                        content = raw_content
                else:
                    content = str(raw_content)
            else:
                content = str(result)
        else:
            content = str(result)

        return content, a2a_metadata

    def _build_subagent_command(
        self,
        result: Any,
        content: str,
        a2a_metadata: dict[str, Any] | None,
        tool_call_id: str,
        excluded_keys: tuple[str, ...],
    ) -> Command:
        """Build a Command with ToolMessage from subagent result.

        Args:
            result: The result dict from subagent invocation
            content: Extracted content string
            a2a_metadata: A2A metadata dict or None
            tool_call_id: The tool call ID for the ToolMessage
            excluded_keys: State keys to exclude from state update

        Returns:
            Command with state update and ToolMessage
        """
        # Build ToolMessage with A2A metadata in additional_kwargs
        # This allows A2ATaskTrackingMiddleware.before_model to extract and persist tracking IDs
        additional_kwargs = {}
        if a2a_metadata:
            additional_kwargs["a2a_metadata"] = a2a_metadata

        tool_message = ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            additional_kwargs=additional_kwargs,
        )

        # Return Command with state update (similar to SubAgentMiddleware)
        state_update = {k: v for k, v in result.items() if k not in excluded_keys} if isinstance(result, dict) else {}

        # Note: Sub-agent output content is already stored in the ToolMessage above.
        # When include_subagent_output=true, stream_handler will extract it from messages.

        return Command(
            update={
                **state_update,
                "messages": [tool_message],
            }
        )

    # =========================================================================
    # Sub-Agent Input Construction
    # =========================================================================

    @traceable(name="DynamicToolDispatchMiddleware._filter_files_with_llm", run_type="retriever")
    async def _filter_files_with_llm(
        self,
        description: str,
        file_blocks: list[ContentBlock],
    ) -> list[ContentBlock]:
        """Use LLM to intelligently filter files based on task description.

        Uses gemini-3-flash-preview for fast filtering with minimal latency and cost tracking.
        On error, falls back to returning all files.

        Args:
            description: Task description from the LLM's tool call
            file_blocks: List of ContentBlocks (ImageContentBlock, AudioContentBlock, etc.)

        Returns:
            Filtered list of ContentBlocks relevant to the task
        """
        if not file_blocks:
            return []

        try:
            # Build file summary for the LLM
            file_summaries = []
            for idx, block in enumerate(file_blocks):
                block_type = block.get("type", "unknown")
                mime_type = block.get("mime_type", "unknown")
                # Try to extract filename from URL if available
                url = block.get("url", "")
                filename = url.split("/")[-1].split("?")[0] if url else f"file_{idx}"
                file_summaries.append(
                    {
                        "index": idx,
                        "type": block_type,
                        "mime_type": mime_type,
                        "filename": filename,
                    }
                )

            # Build prompt for file filtering
            prompt = textwrap.dedent(
                f"""\
                You are a smart file filtering assistant. Given a task description and a list of files, 
                determine which files are relevant to completing the task.

                Task: {description}

                Files:
                {json.dumps(file_summaries, indent=2)}

                Respond with JSON containing an array of file indices that are relevant to the task.
                Include a file if it might be useful, even if you're not 100% sure.
                If all files seem relevant, include all indices.
                If no files seem relevant, return an empty array.

                Response format:
                {{"relevant_indices": [0, 2, 3]}}

                Your response (JSON only):"""
            )

            # Use fast Gemini model for filtering (low latency) with cost tracking
            if self.agent_settings and self.cost_logger:
                callbacks = [CostTrackingCallback(self.cost_logger)]
                model = create_model(
                    "gemini-3-flash-preview",
                    self.agent_settings.get_bedrock_region(),
                    thinking_level=None,
                    callbacks=callbacks,
                )
                model.with_structured_output(
                    {"relevant_indices": list[int]},
                )  # Ensure structured output for easier parsing
            else:
                raise ValueError("Missing agent_settings or cost_logger for LLM-based file filtering")

            response = await model.ainvoke(prompt)
            relevant_indices = response.get("relevant_indices", [])
            if not isinstance(relevant_indices, list) or not all(isinstance(i, int) for i in relevant_indices):
                raise ValueError(f"Invalid response format for relevant_indices: {response}")

            # Filter file blocks by relevant indices
            filtered_blocks = [block for idx, block in enumerate(file_blocks) if idx in relevant_indices]

            logger.info(f"File filtering: {len(filtered_blocks)}/{len(file_blocks)} files selected as relevant to task")

            return filtered_blocks if filtered_blocks else file_blocks  # Fallback to all if none selected

        except Exception as e:
            logger.warning(
                f"File filtering failed ({type(e).__name__}: {e}), "
                f"falling back to forwarding all {len(file_blocks)} files"
            )
            return file_blocks  # Fallback to all files on error

    async def _build_subagent_human_message(
        self,
        description: str,
        user_context: GraphRuntimeContext,
        subagent: Any,
    ) -> HumanMessage:
        """Build a HumanMessage for sub-agent dispatch with intelligent file filtering.

        Uses LLM-based filtering to determine which files are relevant to the task,
        avoiding wasteful forwarding of all files to all sub-agents. Optimizes by
        skipping filtering entirely for non-multimodal agents.

        Args:
            description: Task description from the LLM's tool call
            user_context: Runtime context with pending_file_blocks
            subagent: CompiledSubAgent dict with is_multimodal metadata

        Returns:
            HumanMessage with filtered content_blocks if relevant files exist,
            or plain content otherwise
        """
        input_modes = subagent.get("runnable").input_modes
        if input_modes == ["text"]:
            # Agent doesn't support files, send text only (optimization: skip filtering)
            logger.debug(f"Subagent '{subagent.get('name', 'unknown')}' is not multimodal, sending text-only message")
            return HumanMessage(content=description)

        # Subagent is multimodal - check if we have files to forward
        if not user_context.pending_file_blocks:
            return HumanMessage(content=description)

        filtered_files = await self._filter_files_with_llm(
            description,
            list(user_context.pending_file_blocks),
        )

        # if the modes of the filtered_files don't match the subagent's input modes, we should convert them to text
        # descriptions and adding them to the text message.
        for block in filtered_files:
            block_type = block.get("type", "unknown")
            mime_type = block.get("mime_type", "unknown")
            url = block.get("url", "")
            filename = url.split("/")[-1].split("?")[0] if url else "unknown_file"
            if block_type not in input_modes:
                logger.debug(
                    f"Converting file '{filename}' of type '{block_type}' to text description for subagent "
                    f"because it doesn't support '{block_type}' input"
                )
                description_block = {
                    "type": "text",
                    "text": f"Url: {url}, File: {filename}, Type: {block_type}, MIME: {mime_type}",
                }
                description += f"\n\n{description_block['text']}"

        if filtered_files:
            text_block: TextContentBlock = {"type": "text", "text": description}
            all_blocks: list[ContentBlock] = [text_block] + filtered_files
            logger.debug(
                f"Building sub-agent HumanMessage with {len(filtered_files)} "
                f"filtered file content blocks (from {len(user_context.pending_file_blocks)} total)"
            )
            return HumanMessage(content_blocks=all_blocks)
        else:
            # No relevant files after filtering, send text only
            logger.debug("No relevant files after filtering, sending text-only message")
            return HumanMessage(content=description)

    # =========================================================================
    # Model Call Interception - Dynamic Tool Binding
    # =========================================================================

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Bind user-specific tools to the model at call time.

        This replaces request.tools with tools from GraphRuntimeContext.tool_registry,
        enabling per-user tool injection without graph recreation.

        Args:
            request: Model request (tools may be empty or placeholder)
            handler: Callback to execute the model

        Returns:
            Model response
        """
        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning("DynamicToolDispatchMiddleware: No GraphRuntimeContext, passing through")
            return handler(request)

        # Get merged tools: original request tools + static tools + (optionally) registry tools.
        # When skip_tool_injection=True, step 3 (registry injection) is skipped because
        # ToolsetSelectorMiddleware handles that on the GP graph path.
        tool_dicts = self._get_tools_as_dicts(
            user_context,
            original_tools=request.tools,
            inject_from_registry=not self.skip_tool_injection,
        )

        logger.debug(
            f"DynamicToolDispatchMiddleware.wrap_model_call"
            f"{'(skip_injection)' if self.skip_tool_injection else ''}: "
            f"Binding {len(tool_dicts)} tools as dicts for user {user_context.user_sub}: "
            f"{[t.get('function', {}).get('name', '?') for t in tool_dicts]}"
        )

        # Override request with user's tools (dict format bypasses validation)
        # Cast needed because list is invariant in Python typing
        return handler(request.override(tools=cast(list[BaseTool | dict], tool_dicts)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of wrap_model_call with progressive retry.

        Progressive cleanup sequence:
        - MINIMAL: Remove None values only
        - MODERATE: + Remove ALL enums
        - AGGRESSIVE: + Remove format/min/max/array constraints

        When skip_tool_injection=True, registry injection (step 3) is skipped so that
        ToolsetSelectorMiddleware retains sole responsibility for tool injection.

        Args:
            request: Model request with tools
            handler: Async callback to execute the model

        Returns:
            Model response
        """
        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning("DynamicToolDispatchMiddleware: No GraphRuntimeContext, passing through")
            return await handler(request)

        # Try progressive cleanup levels on INVALID_ARGUMENT errors
        # MODERATE removes all enums (solves global state space limit with 80+ tools)
        for level in [CleanupLevel.MINIMAL, CleanupLevel.MODERATE, CleanupLevel.AGGRESSIVE]:
            tool_dicts = self._get_tools_as_dicts(
                user_context,
                original_tools=request.tools,
                level=level,
                inject_from_registry=not self.skip_tool_injection,
            )
            try:
                logger.debug(
                    f"DynamicToolDispatchMiddleware.awrap_model_call: "
                    f"Binding {len(tool_dicts)} tools as dicts for user {user_context.user_sub} "
                    f"(cleanup={level.value}): "
                    f"{[t.get('function', {}).get('name', '?') for t in tool_dicts]}"
                )

                result = await handler(request.override(tools=cast(list[BaseTool | dict], tool_dicts)))
                return result
            except Exception as e:
                # Check if it's a Gemini INVALID_ARGUMENT error
                is_schema_error = (
                    ChatGoogleGenerativeAIError
                    and isinstance(e, ChatGoogleGenerativeAIError)
                    and "INVALID_ARGUMENT" in str(e)
                    and "schema" in str(e).lower()
                )

                if is_schema_error and level != CleanupLevel.AGGRESSIVE:
                    # Log and retry with next level
                    tool_count = len(tool_dicts)
                    logger.warning(
                        f"Schema validation failed with {level.value} cleanup, retrying with next level. "
                        f"User: {user_context.user_sub}, Tool count: {tool_count}, Error: {str(e)[:200]}"
                    )
                    continue
                else:
                    # Not a schema error or already tried aggressive - re-raise
                    raise

        # Should not reach here, but for type checker
        raise RuntimeError("All cleanup levels exhausted")

    # =========================================================================
    # Tool Call Interception - Dynamic Dispatch WITHOUT execute()
    # =========================================================================

    def _dispatch_task_tool(
        self,
        tool_call: Any,
        user_context: GraphRuntimeContext,
        state: Any,
        config: Any,
    ) -> ToolMessage | Command | None:
        """Dispatch 'task' tool call to the appropriate subagent.

        Args:
            tool_call: The tool call with name, id, args
            user_context: User context with subagent_registry
            state: Current agent state
            config: Runtime config

        Returns:
            ToolMessage or Command with subagent result, or None if subagent not found
            (allowing fallback to SubAgentMiddleware for general-purpose agent)
        """
        tool_call_id = tool_call["id"]
        args = tool_call.get("args", {})
        description = args.get("description", "")
        subagent_type = args.get("subagent_type", "")

        # Look up subagent in user's dynamic registry
        subagent = user_context.subagent_registry.get(subagent_type)
        if subagent is None:
            # Subagent not in dynamic registry - return None to signal fallback
            # This allows SubAgentMiddleware to handle general-purpose and other built-in agents
            logger.debug(
                f"DynamicToolDispatchMiddleware: Subagent '{subagent_type}' not in "
                f"dynamic registry, falling back to handler (SubAgentMiddleware)"
            )
            return None

        logger.debug(f"DynamicToolDispatchMiddleware: Dispatching task to subagent '{subagent_type}'")

        # Get the runnable from CompiledSubAgent
        runnable = subagent.get("runnable")
        if runnable is None:
            return ToolMessage(
                content=f"Error: Subagent '{subagent_type}' has no runnable",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

        # Prepare state for subagent (exclude messages, todos)
        # NOTE: subagent_state is a dict but it will be validated to SubAgentInput in the runnable astream method
        excluded_keys = ("messages", "todos")
        subagent_state = {k: v for k, v in state.items() if k not in excluded_keys}

        # Build sub-agent HumanMessage with intelligent file filtering.
        # Uses LLM to determine which files are relevant to the task.
        # Optimizes by skipping filtering for non-multimodal agents.
        message = asyncio.run(self._build_subagent_human_message(description, user_context, subagent))
        subagent_state["messages"] = [message]

        # Extract orchestrator's conversation_id from config.configurable.thread_id
        # This is the orchestrator's task.context_id set by the executor
        orchestrator_conversation_id = None
        if config and isinstance(config, dict):
            orchestrator_conversation_id = config.get("configurable", {}).get("thread_id")
            if orchestrator_conversation_id:
                logger.debug(f"[CONVERSATION_ID] Extracted from config.thread_id: {orchestrator_conversation_id}")

        subagent_state["orchestrator_conversation_id"] = orchestrator_conversation_id

        # Use a traced function for proper LangSmith visibility
        @traceable(name=f"task:{subagent_type}", run_type="tool")
        def invoke_a2a_agent(agent_state: dict) -> dict:
            """Invoke A2A agent with tracing for LangSmith visibility."""
            return runnable.invoke(agent_state)

        try:
            # Invoke the subagent runnable with tracing
            result = invoke_a2a_agent(subagent_state)

            # Extract content and A2A metadata, then build Command
            content, a2a_metadata = self._extract_subagent_response(result, subagent_type)
            return self._build_subagent_command(result, content, a2a_metadata, tool_call_id, excluded_keys)

        except GraphInterrupt as gi:
            # is not an error - just an interrupt from the graph execution
            logger.info(f"[DYNAMIC TOOL DISPATCH] Subagent '{subagent_type}' interrupted: {gi}")
            # Re-raise so orchestrator can handle it
            raise
        except Exception as e:
            logger.exception(f"Subagent '{subagent_type}' failed: {e}")
            return ToolMessage(
                content=f"Error executing subagent '{subagent_type}': {e}",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

    async def _adispatch_task_tool(
        self,
        tool_call: Any,
        user_context: GraphRuntimeContext,
        state: Any,
        config: Any,
    ) -> ToolMessage | Command | None:
        """Async dispatch 'task' tool call to the appropriate subagent.

        Args:
            tool_call: The tool call with name, id, args
            user_context: User context with subagent_registry
            state: Current agent state
            config: Runtime config

        Returns:
            ToolMessage or Command with subagent result, or None if subagent not found
            (allowing fallback to SubAgentMiddleware for general-purpose agent)
        """
        tool_call_id = tool_call["id"]
        args = tool_call.get("args", {})
        description = args.get("description", "")
        subagent_type = args.get("subagent_type", "")

        # Look up subagent in user's dynamic registry
        subagent = user_context.subagent_registry.get(subagent_type)
        if subagent is None:
            # Subagent not in dynamic registry - return None to signal fallback
            # This allows SubAgentMiddleware to handle built-in agents
            # If we wouldn't provide a custom `general-purpose` sub-agent in the dynamic registry,
            # then returning None here would allow the deepagents built-in `general-purpose` to handle the query.
            logger.debug(
                f"DynamicToolDispatchMiddleware: Subagent '{subagent_type}' not in "
                f"dynamic registry, falling back to handler (SubAgentMiddleware)"
            )
            return None

        logger.debug(f"DynamicToolDispatchMiddleware: Dispatching task to subagent '{subagent_type}'")

        # Get the runnable from CompiledSubAgent
        runnable = subagent.get("runnable")
        if runnable is None:
            return ToolMessage(
                content=f"Error: Subagent '{subagent_type}' has no runnable",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

        # Prepare state for subagent (exclude messages, todos)
        excluded_keys = ("messages", "todos")
        subagent_state = {k: v for k, v in state.items() if k not in excluded_keys}

        # Build sub-agent HumanMessage with intelligent file filtering.
        # Uses LLM to determine which files are relevant to the task.
        # Optimizes by skipping filtering for non-multimodal agents.
        message = await self._build_subagent_human_message(description, user_context, subagent)
        subagent_state["messages"] = [message]

        # Extract orchestrator's conversation_id from config.configurable.thread_id
        # This is the orchestrator's task.context_id set by the executor
        orchestrator_conversation_id = None
        if config and isinstance(config, dict):
            orchestrator_conversation_id = config.get("configurable", {}).get("thread_id")
            if orchestrator_conversation_id:
                logger.debug(f"[CONVERSATION_ID] Extracted from config.thread_id: {orchestrator_conversation_id}")

        subagent_state["orchestrator_conversation_id"] = orchestrator_conversation_id

        # Prepare complete config for sub-agent with correct user_id/assistant_id for namespace consistency
        # Extract values from user context and parent config
        user_id = user_context.user_id
        user_sub = user_context.user_sub
        assistant_id = config.get("metadata", {}).get("assistant_id") if isinstance(config, dict) else None

        # Create complete RunnableConfig for sub-agent using SDK utility
        # This ensures FilesystemMiddleware uses consistent (user_id, "filesystem") namespace
        subagent_config = create_runnable_config(
            user_sub=user_sub,
            conversation_id=orchestrator_conversation_id or "unknown",  # Fallback to "unknown" if not available
            user_id=user_id,
            assistant_id=assistant_id or user_id,  # Fallback to user_id if not in parent config
            thread_id=f"{orchestrator_conversation_id or 'unknown'}::{subagent_type}",  # Unique thread_id for checkpoint isolation
            checkpoint_ns=subagent_type,  # Namespace for checkpointer
        )

        # Use a traced function for proper LangSmith visibility
        @traceable(name=f"task:{subagent_type}", run_type="tool")
        async def ainvoke_a2a_agent(agent_state: dict, agent_config: dict) -> dict:
            """Invoke A2A agent asynchronously with tracing for LangSmith visibility."""
            return await runnable.ainvoke(agent_state, agent_config)

        try:
            # Invoke the subagent runnable asynchronously with tracing and prepared config
            result = await ainvoke_a2a_agent(subagent_state, subagent_config)

            # Extract content and A2A metadata, then build Command
            content, a2a_metadata = self._extract_subagent_response(result, subagent_type)
            return self._build_subagent_command(result, content, a2a_metadata, tool_call_id, excluded_keys)

        except GraphInterrupt as gi:
            # is not an error - just an interrupt from the graph execution
            logger.info(f"[DYNAMIC TOOL DISPATCH] Subagent '{subagent_type}' interrupted: {gi}")
            # Re-raise so orchestrator can handle it
            raise
        except Exception as e:
            logger.exception(f"Subagent '{subagent_type}' failed: {e}")
            return ToolMessage(
                content=f"Error executing subagent '{subagent_type}': {e}",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Dispatch tool calls - either to ToolNode or to dynamic user tools.

        This middleware handles two cases:
        1. request.tool is NOT None → Tool is registered with ToolNode (e.g., write_todos)
           → Pass through to handler for standard execution
        2. request.tool is None → Tool is dynamic (from user's registry)
           → Handle ourselves by looking up and invoking from registry

        For "task" tool: Always dispatch to subagent from GraphRuntimeContext.subagent_registry

        Args:
            request: Tool call request
            handler: Callback to execute tool via ToolNode

        Returns:
            ToolMessage with tool execution result
        """
        tool_call = request.tool_call
        tool_name = tool_call["name"]
        tool_call_id = tool_call["id"]

        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning(
                f"DynamicToolDispatchMiddleware: No GraphRuntimeContext for tool '{tool_name}', passing to handler"
            )
            return handler(request)

        # Debug logging
        logger.debug(
            f"DynamicToolDispatchMiddleware.wrap_tool_call: "
            f"tool_name='{tool_name}', request.tool={'present' if request.tool else 'None'}, "
            f"in_registry={tool_name in user_context.tool_registry}, "
            f"registry_tools={list(user_context.tool_registry.keys())}"
        )

        # Special handling for "task" tool (subagent dispatch)
        # Try dynamic registry first, fall back to handler (SubAgentMiddleware) for general-purpose
        if tool_name == "task":
            result = self._dispatch_task_tool(
                tool_call=tool_call,
                user_context=user_context,
                state=request.runtime.state,
                config=request.runtime.config,
            )
            if result is not None:
                return result
            # Subagent not in dynamic registry - fall through to handler (SubAgentMiddleware)
            logger.debug(
                "DynamicToolDispatchMiddleware.wrap_tool_call: Task tool falling back to handler for subagent dispatch"
            )
            return handler(request)

        # If tool is registered with ToolNode, let the standard handler execute it
        # This handles tools from create_deep_agent like write_todos
        if request.tool is not None:
            logger.debug(
                f"DynamicToolDispatchMiddleware.wrap_tool_call: "
                f"Tool '{tool_name}' is registered with ToolNode, passing to handler"
            )
            return handler(request)

        # Tool is NOT in ToolNode — resolve it from the user's dynamic registry and
        # inject it into the request via override(tool=...) so the full inner middleware
        # chain executes.  This ensures FilesystemMiddleware eviction, retry,
        # InjectedToolArg injection, and ToolNode error handling all fire for dynamic
        # (MCP) tools exactly as they do for statically registered tools.
        tool = self._lookup_tool(tool_name, user_context)

        if tool is None:
            logger.error(
                f"DynamicToolDispatchMiddleware: Tool '{tool_name}' not found "
                f"in ToolNode or user registry for user {user_context.user_sub}"
            )
            return ToolMessage(
                content=f"Error: Tool '{tool_name}' is not available",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )

        logger.debug(
            f"DynamicToolDispatchMiddleware.wrap_tool_call: "
            f"Dispatching dynamic tool '{tool_name}' for user {user_context.user_sub}"
        )

        return handler(request.override(tool=tool))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Async version of wrap_tool_call.

        This middleware handles two cases:
        1. request.tool is NOT None → Tool is registered with ToolNode (e.g., write_todos)
           → Pass through to handler for standard execution
        2. request.tool is None → Tool is dynamic (from user's registry)
           → Handle ourselves by looking up and invoking from registry

        For "task" tool: Always dispatch to subagent from GraphRuntimeContext.subagent_registry

        Args:
            request: Tool call request
            handler: Async callback to execute tool via ToolNode

        Returns:
            ToolMessage with tool execution result
        """
        tool_call = request.tool_call
        tool_name = tool_call["name"]
        tool_call_id = tool_call["id"]

        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning(
                f"DynamicToolDispatchMiddleware: No GraphRuntimeContext for tool '{tool_name}', passing to handler"
            )
            return await handler(request)

        # Special handling for "task" tool (subagent dispatch)
        # Try dynamic registry first, fall back to handler (SubAgentMiddleware) for general-purpose
        if tool_name == "task":
            result = await self._adispatch_task_tool(
                tool_call=tool_call,
                user_context=user_context,
                state=request.runtime.state,
                config=request.runtime.config,
            )
            if result is not None:
                return result
            # Subagent not in dynamic registry - fall through to handler (SubAgentMiddleware)
            logger.debug(
                "DynamicToolDispatchMiddleware.awrap_tool_call: Task tool falling back to handler for subagent dispatch"
            )
            return await handler(request)

        # If tool is registered with ToolNode, let the standard handler execute it
        # This handles tools from create_deep_agent like write_todos
        if request.tool is not None:
            logger.debug(
                f"DynamicToolDispatchMiddleware.awrap_tool_call: "
                f"Tool '{tool_name}' is registered with ToolNode, passing to handler"
            )
            return await handler(request)

        # Tool is NOT in ToolNode — resolve it from the user's dynamic registry and
        # inject it into the request via override(tool=...) so the full inner middleware
        # chain executes.  This ensures FilesystemMiddleware eviction, retry,
        # InjectedToolArg injection, and ToolNode error handling all fire for dynamic
        # (MCP) tools exactly as they do for statically registered tools.
        tool = self._lookup_tool(tool_name, user_context)

        if tool is None:
            logger.error(
                f"DynamicToolDispatchMiddleware: Tool '{tool_name}' not found "
                f"in ToolNode or user registry for user {user_context.user_sub}"
            )
            return ToolMessage(
                content=f"Error: Tool '{tool_name}' is not available",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )

        logger.debug(
            f"DynamicToolDispatchMiddleware.awrap_tool_call: "
            f"Dispatching dynamic tool '{tool_name}' for user {user_context.user_sub}"
        )

        return await handler(request.override(tool=tool))
