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
   - "task" tool for general-purpose → fall through to SubAgentMiddleware's handler

Architecture:
- Graph is created with standard tools (write_todos, task, etc.)
- User MCP tools are stored in GraphRuntimeContext.tool_registry (discovered at runtime)
- A2A subagents are stored in GraphRuntimeContext.subagent_registry (discovered at runtime)
- Tools are converted to dict format for model binding (bypasses ToolNode validation)
- The task tool description is enhanced to include A2A agents alongside general-purpose

Key Insight: Dict tools bypass factory.py validation (line 907: `if isinstance(t, dict): continue`)
which allows runtime tool injection without graph recreation.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command
from langsmith import traceable

from ..models.config import GraphRuntimeContext

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
       - "task" tool for general-purpose → fall through to SubAgentMiddleware's handler

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

    def __init__(self, static_tools: list[BaseTool] | None = None):
        """Initialize the middleware.

        Args:
            static_tools: Optional list of static tools that are always available
                regardless of user context (e.g., FinalResponseSchema for Bedrock).
        """
        self.static_tools = {t.name: t for t in (static_tools or [])}

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

    def _validate_tool_schema(self, tool_dict: dict[str, Any]) -> dict[str, Any]:
        """Validate and fix tool schema for OpenAI API compatibility.

        OpenAI requires that if a 'parameters' field is present, it must be a valid
        JSON Schema object with a 'properties' field (even if empty). MCP tools
        sometimes have missing or invalid parameters schemas.

        This validation is critical for streaming SSE responses. If a tool schema
        is invalid, OpenAI returns a 400 error before streaming begins, causing
        the A2A server to return JSON instead of SSE, which breaks A2A clients
        expecting text/event-stream responses.

        Args:
            tool_dict: Tool in OpenAI dict format

        Returns:
            Tool dict with validated parameters schema
        """
        function_dict = tool_dict.get("function", {})
        parameters = function_dict.get("parameters")

        # If parameters is missing or not a dict, set it to an empty object schema
        if parameters is None or not isinstance(parameters, dict):
            function_dict["parameters"] = {
                "type": "object",
                "properties": {},
            }
        # If parameters exists but missing 'properties', add it
        elif "properties" not in parameters:
            parameters["properties"] = {}

        # Ensure the updated function dict is in the tool dict
        tool_dict["function"] = function_dict
        return tool_dict

    def _get_tools_as_dicts(
        self, user_context: GraphRuntimeContext, original_tools: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Get all tools available to the user as OpenAI-format dicts.

        Merges tools from multiple sources (in order of precedence):
        1. Original tools from request (e.g., write_todos, task from create_deep_agent)
           - The "task" tool is enhanced with A2A agent descriptions from subagent_registry
        2. Static tools from middleware initialization (e.g., FinalResponseSchema)
        3. User's dynamic tools from tool_registry (can override earlier tools)

        Tools are converted to dict format to bypass LangGraph's tool validation.

        Args:
            user_context: User context containing tool_registry and subagent_registry
            original_tools: Original tools from the request (from create_agent/create_deep_agent)

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
                    tool_dict = convert_to_openai_tool(tool)
                    # Validate schema for OpenAI API compatibility
                    tool_dict = self._validate_tool_schema(tool_dict)
                    # Enhance task tool with A2A agents (description + enum)
                    if tool.name == "task":
                        tool_dict = self._enhance_task_tool_schema(tool_dict, user_context)
                    tool_dicts.append(tool_dict)
                    seen_names.add(tool.name)
            elif isinstance(tool, dict):
                name = tool.get("function", {}).get("name") or tool.get("name")
                if name and name not in seen_names:
                    # Validate schema for OpenAI API compatibility
                    tool = self._validate_tool_schema(tool)
                    # Enhance task tool with A2A agents (description + enum)
                    if name == "task":
                        tool = self._enhance_task_tool_schema(tool, user_context)
                    tool_dicts.append(tool)
                    seen_names.add(name)

        # 2. Add static tools from middleware (e.g., FinalResponseSchema)
        for tool in self.static_tools.values():
            if tool.name not in seen_names:
                tool_dict = convert_to_openai_tool(tool)
                # Validate schema for OpenAI API compatibility
                tool_dict = self._validate_tool_schema(tool_dict)
                tool_dicts.append(tool_dict)
                seen_names.add(tool.name)

        # 3. Add user's dynamic tools (may override previous tools by name)
        for name, tool in user_context.tool_registry.items():
            if name in seen_names:
                # User tool overrides existing tool - remove old and add user's
                tool_dicts = [t for t in tool_dicts if t.get("function", {}).get("name") != name]
            if isinstance(tool, BaseTool):
                tool_dict = convert_to_openai_tool(tool)
                # Ensure parameters schema is valid for OpenAI API
                tool_dict = self._validate_tool_schema(tool_dict)
                tool_dicts.append(tool_dict)
            elif isinstance(tool, dict):
                # Already in dict format, but still validate
                tool_dict = self._validate_tool_schema(tool)
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
        return Command(
            update={
                **state_update,
                "messages": [tool_message],
            }
        )

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

        # Get merged tools: original request tools + static tools + user tools + task tool
        # Pass original request.tools to preserve tools from create_deep_agent (e.g., write_todos)
        tool_dicts = self._get_tools_as_dicts(user_context, original_tools=request.tools)

        logger.debug(
            f"DynamicToolDispatchMiddleware.wrap_model_call: "
            f"Binding {len(tool_dicts)} tools as dicts for user {user_context.user_id}: "
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
        """Async version of wrap_model_call.

        Args:
            request: Model request (tools may be empty or placeholder)
            handler: Async callback to execute the model

        Returns:
            Model response
        """
        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning("DynamicToolDispatchMiddleware: No GraphRuntimeContext, passing through")
            return await handler(request)

        # Get merged tools: original request tools + static tools + user tools + task tool
        # Pass original request.tools to preserve tools from create_deep_agent (e.g., write_todos)
        tool_dicts = self._get_tools_as_dicts(user_context, original_tools=request.tools)

        logger.debug(
            f"DynamicToolDispatchMiddleware.awrap_model_call: "
            f"Binding {len(tool_dicts)} tools as dicts for user {user_context.user_id}: "
            f"{[t.get('function', {}).get('name', '?') for t in tool_dicts]}"
        )

        # Override request with user's tools (dict format bypasses validation)
        # Cast needed because list is invariant in Python typing
        return await handler(request.override(tools=cast(list[BaseTool | dict], tool_dicts)))

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
        excluded_keys = ("messages", "todos")
        subagent_state = {k: v for k, v in state.items() if k not in excluded_keys}
        subagent_state["messages"] = [HumanMessage(content=description)]

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
        excluded_keys = ("messages", "todos")
        subagent_state = {k: v for k, v in state.items() if k not in excluded_keys}
        subagent_state["messages"] = [HumanMessage(content=description)]

        # Use a traced function for proper LangSmith visibility
        @traceable(name=f"task:{subagent_type}", run_type="tool")
        async def ainvoke_a2a_agent(agent_state: dict) -> dict:
            """Invoke A2A agent asynchronously with tracing for LangSmith visibility."""
            return await runnable.ainvoke(agent_state)

        try:
            # Invoke the subagent runnable asynchronously with tracing
            result = await ainvoke_a2a_agent(subagent_state)

            # Extract content and A2A metadata, then build Command
            content, a2a_metadata = self._extract_subagent_response(result, subagent_type)
            return self._build_subagent_command(result, content, a2a_metadata, tool_call_id, excluded_keys)

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

        # Tool is NOT in ToolNode - look up from user's dynamic registry
        tool = self._lookup_tool(tool_name, user_context)

        if tool is None:
            logger.error(
                f"DynamicToolDispatchMiddleware: Tool '{tool_name}' not found "
                f"in ToolNode or user registry for user {user_context.user_id}"
            )
            return ToolMessage(
                content=f"Error: Tool '{tool_name}' is not available",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )

        logger.debug(
            f"DynamicToolDispatchMiddleware.wrap_tool_call: "
            f"Dispatching dynamic tool '{tool_name}' for user {user_context.user_id}"
        )

        # Invoke the dynamic tool directly
        try:
            # Prepare tool call args in the format tools expect
            call_args = {**tool_call, "type": "tool_call"}
            result = tool.invoke(call_args, request.runtime.config)

            # Handle different return types
            if isinstance(result, Command):
                return result
            if isinstance(result, ToolMessage):
                return result

            # Wrap raw result in ToolMessage
            return ToolMessage(
                content=str(result),
                name=tool_name,
                tool_call_id=tool_call_id,
            )

        except Exception as e:
            logger.exception(f"DynamicToolDispatchMiddleware: Tool '{tool_name}' failed: {e}")
            return ToolMessage(
                content=f"Error executing tool '{tool_name}': {e}",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )

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

        # Tool is NOT in ToolNode - look up from user's dynamic registry
        tool = self._lookup_tool(tool_name, user_context)

        if tool is None:
            logger.error(
                f"DynamicToolDispatchMiddleware: Tool '{tool_name}' not found "
                f"in ToolNode or user registry for user {user_context.user_id}"
            )
            return ToolMessage(
                content=f"Error: Tool '{tool_name}' is not available",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )

        logger.debug(
            f"DynamicToolDispatchMiddleware.awrap_tool_call: "
            f"Dispatching dynamic tool '{tool_name}' for user {user_context.user_id}"
        )

        # Invoke the dynamic tool asynchronously
        try:
            # Prepare tool call args in the format tools expect
            call_args = {**tool_call, "type": "tool_call"}
            result = await tool.ainvoke(call_args, request.runtime.config)

            # Handle different return types
            if isinstance(result, Command):
                return result
            if isinstance(result, ToolMessage):
                return result

            # Wrap raw result in ToolMessage
            return ToolMessage(
                content=str(result),
                name=tool_name,
                tool_call_id=tool_call_id,
            )

        except Exception as e:
            logger.exception(f"DynamicToolDispatchMiddleware: Tool '{tool_name}' failed: {e}")
            return ToolMessage(
                content=f"Error executing tool '{tool_name}': {e}",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )
