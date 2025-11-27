"""Dynamic tool dispatch middleware for runtime tool injection.

This middleware enables a SINGLE graph instance to serve ALL users with different
tool and subagent configurations:

1. **wrap_model_call**: Merges and binds tools from multiple sources:
   - Original tools from create_agent (e.g., write_todos)
   - Static tools (e.g., FinalResponseSchema for Bedrock)
   - User's dynamic tools from GraphRuntimeContext.tool_registry
   - Dynamic "task" tool for subagents from GraphRuntimeContext.subagent_registry

2. **wrap_tool_call**: Routes tool execution appropriately:
   - Tools registered with ToolNode → pass to handler (standard execution)
   - Dynamic tools not in ToolNode → execute from GraphRuntimeContext.tool_registry
   - "task" tool → dispatch to subagent from GraphRuntimeContext.subagent_registry

Architecture:
- Graph is created with standard tools (write_todos, etc.)
- User tools are stored in GraphRuntimeContext.tool_registry (discovered at runtime)
- User subagents are stored in GraphRuntimeContext.subagent_registry (discovered at runtime)
- Tools are converted to dict format for model binding (bypasses ToolNode validation)
- Dynamic tool calls are dispatched directly from registry

Key Insight: Dict tools bypass factory.py validation (line 907: `if isinstance(t, dict): continue`)
which allows runtime tool injection without graph recreation.
"""

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
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)


class DynamicToolDispatchMiddleware(AgentMiddleware[AgentState, GraphRuntimeContext]):
    """Middleware for runtime tool injection and dynamic dispatch.

    Enables per-user tool injection without graph recreation:

    1. **Model Binding** (wrap_model_call): Merges tools from original request,
       static tools, and GraphRuntimeContext.tool_registry into dict format for model binding.

    2. **Tool Dispatch** (wrap_tool_call): Routes tool execution:
       - ToolNode-registered tools → standard handler execution
       - Dynamic tools → execute from GraphRuntimeContext.tool_registry
       - "task" tool → dispatch to subagent from GraphRuntimeContext.subagent_registry

    Example:
        ```python
        # Graph can have standard tools; user tools are added at runtime
        agent = create_agent(
            model=model,
            tools=[write_todos],  # Standard tools work normally
            middleware=[DynamicToolDispatchMiddleware(static_tools=[final_response])],
            context_schema=GraphRuntimeContext,
        )

        # User tools are added via GraphRuntimeContext at invocation time
        user_context = GraphRuntimeContext(
            user_id="user1",
            tool_registry={"mcp_tool": mcp_tool},
            subagent_registry={"jira-agent": jira_subagent},
        )
        agent.invoke({"messages": [...]}, context=user_context)
        ```
    """

    state_schema = AgentState
    tools: list[BaseTool] = []  # No tools registered with middleware itself

    # Task tool description template for subagents
    TASK_TOOL_DESCRIPTION = """Launch an ephemeral subagent to handle complex, multi-step independent tasks with isolated context windows.

Available agent types and the tools they have access to:
{available_agents}

When using the Task tool, you must specify a subagent_type parameter to select which agent type to use.

## Usage notes:
1. Launch multiple agents concurrently whenever possible, to maximize performance
2. Each agent invocation is stateless - provide complete context in the description
3. The agent's outputs should generally be trusted
4. Clearly tell the agent whether you expect it to create content, perform analysis, or research"""

    def __init__(self, static_tools: list[BaseTool] | None = None):
        """Initialize the middleware.

        Args:
            static_tools: Optional list of static tools that are always available
                regardless of user context (e.g., FinalResponseSchema for Bedrock).
        """
        self.static_tools = {t.name: t for t in (static_tools or [])}
        # Cache for dynamically created task tools per user
        self._task_tool_cache: dict[str, BaseTool] = {}

    def _create_task_tool(self, user_context: GraphRuntimeContext) -> BaseTool | None:
        """Create the 'task' tool as a StructuredTool for subagent dispatch.

        Creates a proper StructuredTool (like SubAgentMiddleware does) which works
        with both OpenAI and Anthropic models via LangChain's convert_to_openai_tool.

        Args:
            user_context: User context containing subagent_registry

        Returns:
            StructuredTool for the task tool, or None if no subagents
        """
        if not user_context.subagent_registry:
            return None

        # Check cache first (keyed by sorted subagent names to handle same set)
        cache_key = ",".join(sorted(user_context.subagent_registry.keys()))
        if cache_key in self._task_tool_cache:
            return self._task_tool_cache[cache_key]

        # Build subagent descriptions and valid names for the enum
        subagent_descriptions = []
        subagent_names = list(user_context.subagent_registry.keys())
        for name, subagent in user_context.subagent_registry.items():
            description = subagent.get("description", f"Subagent: {name}")
            subagent_descriptions.append(f"- {name}: {description}")

        subagent_description_str = "\n".join(subagent_descriptions)
        task_description = self.TASK_TOOL_DESCRIPTION.format(available_agents=subagent_description_str)

        # Create a StructuredTool like SubAgentMiddleware does
        # This approach works with all LLM providers (OpenAI, Anthropic, etc.)
        def task_func(description: str, subagent_type: str) -> str:
            """Placeholder - actual execution happens in wrap_tool_call."""
            # This function signature defines the tool schema
            # Actual dispatch is handled by _dispatch_task_tool in wrap_tool_call
            return f"Task dispatched to {subagent_type}"

        async def task_afunc(description: str, subagent_type: str) -> str:
            """Async placeholder - actual execution happens in awrap_tool_call."""
            return f"Task dispatched to {subagent_type}"

        # Build the tool with proper schema
        # Note: We add metadata about valid subagent types in the description
        # since StructuredTool doesn't support enum constraints directly
        tool = StructuredTool.from_function(
            name="task",
            func=task_func,
            coroutine=task_afunc,
            description=f"{task_description}\n\nValid subagent_type values: {subagent_names}",
        )

        # Cache the tool
        self._task_tool_cache[cache_key] = tool
        return tool

    def _get_tools_as_dicts(
        self, user_context: GraphRuntimeContext, original_tools: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Get all tools available to the user as OpenAI-format dicts.

        Merges tools from multiple sources (in order of precedence):
        1. Original tools from request (e.g., write_todos from create_deep_agent)
        2. Static tools from middleware initialization (e.g., FinalResponseSchema)
        3. User's dynamic tools from tool_registry (can override earlier tools)
        4. Dynamic "task" tool for subagents (if subagent_registry is populated)

        Tools are converted to dict format to bypass LangGraph's tool validation.

        Args:
            user_context: User context containing tool_registry and subagent_registry
            original_tools: Original tools from the request (from create_agent/create_deep_agent)

        Returns:
            List of tools in OpenAI function calling dict format
        """
        tool_dicts: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        # 1. Add original tools from the request first (e.g., write_todos)
        # These are tools that create_deep_agent provides by default
        for tool in original_tools or []:
            if isinstance(tool, BaseTool):
                if tool.name not in seen_names:
                    tool_dicts.append(convert_to_openai_tool(tool))
                    seen_names.add(tool.name)
            elif isinstance(tool, dict):
                name = tool.get("function", {}).get("name") or tool.get("name")
                if name and name not in seen_names:
                    tool_dicts.append(tool)
                    seen_names.add(name)

        # 2. Add static tools from middleware (e.g., FinalResponseSchema)
        for tool in self.static_tools.values():
            if tool.name not in seen_names:
                tool_dicts.append(convert_to_openai_tool(tool))
                seen_names.add(tool.name)

        # 3. Add user's dynamic tools (may override previous tools by name)
        for name, tool in user_context.tool_registry.items():
            if name in seen_names:
                # User tool overrides existing tool - remove old and add user's
                tool_dicts = [t for t in tool_dicts if t.get("function", {}).get("name") != name]
            if isinstance(tool, BaseTool):
                tool_dicts.append(convert_to_openai_tool(tool))
            elif isinstance(tool, dict):
                # Already in dict format
                tool_dicts.append(tool)
            seen_names.add(name)

        # 4. Add dynamic task tool if user has subagents
        # Use StructuredTool which works with both OpenAI and Anthropic models
        task_tool = self._create_task_tool(user_context)
        if task_tool and "task" not in seen_names:
            tool_dicts.append(convert_to_openai_tool(task_tool))

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
    ) -> ToolMessage | Command:
        """Dispatch 'task' tool call to the appropriate subagent.

        Args:
            tool_call: The tool call with name, id, args
            user_context: User context with subagent_registry
            state: Current agent state
            config: Runtime config

        Returns:
            ToolMessage or Command with subagent result
        """
        tool_call_id = tool_call["id"]
        args = tool_call.get("args", {})
        description = args.get("description", "")
        subagent_type = args.get("subagent_type", "")

        # Look up subagent
        subagent = user_context.subagent_registry.get(subagent_type)
        if subagent is None:
            available = list(user_context.subagent_registry.keys())
            return ToolMessage(
                content=f"Error: Subagent '{subagent_type}' not found. Available: {available}",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

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

        try:
            # Invoke the subagent runnable
            # NOTE: A2AClientRunnable only takes state, not config like standard LangChain runnables
            result = runnable.invoke(subagent_state)

            # Extract the result message content
            if isinstance(result, dict) and "messages" in result:
                messages = result["messages"]
                if messages:
                    content = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
                else:
                    content = str(result)
            else:
                content = str(result)

            # Return Command with state update (similar to SubAgentMiddleware)
            state_update = (
                {k: v for k, v in result.items() if k not in excluded_keys} if isinstance(result, dict) else {}
            )
            return Command(
                update={
                    **state_update,
                    "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)],
                }
            )

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
    ) -> ToolMessage | Command:
        """Async dispatch 'task' tool call to the appropriate subagent.

        Args:
            tool_call: The tool call with name, id, args
            user_context: User context with subagent_registry
            state: Current agent state
            config: Runtime config

        Returns:
            ToolMessage or Command with subagent result
        """
        tool_call_id = tool_call["id"]
        args = tool_call.get("args", {})
        description = args.get("description", "")
        subagent_type = args.get("subagent_type", "")

        # Look up subagent
        subagent = user_context.subagent_registry.get(subagent_type)
        if subagent is None:
            available = list(user_context.subagent_registry.keys())
            return ToolMessage(
                content=f"Error: Subagent '{subagent_type}' not found. Available: {available}",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

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

        try:
            # Invoke the subagent runnable asynchronously
            # NOTE: A2AClientRunnable only takes state, not config like standard LangChain runnables
            result = await runnable.ainvoke(subagent_state)

            # Extract the result message content
            if isinstance(result, dict) and "messages" in result:
                messages = result["messages"]
                if messages:
                    content = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
                else:
                    content = str(result)
            else:
                content = str(result)

            # Return Command with state update (similar to SubAgentMiddleware)
            state_update = (
                {k: v for k, v in result.items() if k not in excluded_keys} if isinstance(result, dict) else {}
            )
            return Command(
                update={
                    **state_update,
                    "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)],
                }
            )

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
        if tool_name == "task":
            return self._dispatch_task_tool(
                tool_call=tool_call,
                user_context=user_context,
                state=request.runtime.state,
                config=request.runtime.config,
            )

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
        if tool_name == "task":
            return await self._adispatch_task_tool(
                tool_call=tool_call,
                user_context=user_context,
                state=request.runtime.state,
                config=request.runtime.config,
            )

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
