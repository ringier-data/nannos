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
import inspect
import json
import logging
import re
import textwrap
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterable, Optional, TypedDict, cast

from a2a.types import TaskState
from agent_common.a2a.base import LocalA2ARunnable
from agent_common.a2a.client_runnable import A2AClientRunnable
from agent_common.a2a.stream_events import (
    TERMINAL_STATES,
    ActivityLogMeta,
    ArtifactUpdate,
    ErrorEvent,
    StreamEvent,
    TaskResponseData,
    TaskUpdate,
    WorkPlanMeta,
)
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from agent_common.agents.foundry_agent import FoundryLocalAgentRunnable
from agent_common.core.model_factory import create_model
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ContentBlock, HumanMessage, SystemMessage, TextContentBlock, ToolMessage
from langchain_core.messages.content import NonStandardContentBlock
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
from langgraph.config import get_stream_writer
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from langsmith import traceable
from pydantic import BaseModel
from ringier_a2a_sdk.cost_tracking.callback import CostTrackingCallback
from ringier_a2a_sdk.models import TodoItem
from ringier_a2a_sdk.utils import create_runnable_config
from ringier_a2a_sdk.utils.schema_cleaning import CleanupLevel, validate_and_clean_tool_dict

from app.agents.file_analyzer import FileAnalyzerRunnable
from app.agents.gp_agent import GPAgentRunnable
from app.agents.task_scheduler import TaskSchedulerRunnable

from ..core.steering_state import ActiveSubagentDispatch, clear_active_subagent_dispatch, set_active_subagent_dispatch
from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)


class A2ACompiledSubAgent(TypedDict):
    """A pre-compiled agent spec.

    !!! note

        The runnable's state schema must include a 'messages' key.

        This is required for the subagent to communicate results back to the main agent.

    When the subagent completes, the final message in the 'messages' list will be
    extracted and returned as a `ToolMessage` to the parent agent.
    """

    name: str
    description: str
    runnable: A2AClientRunnable


OrchestratorSupportedRunnables = (
    A2AClientRunnable
    | LocalA2ARunnable
    | DynamicLocalAgentRunnable
    | FoundryLocalAgentRunnable
    | FileAnalyzerRunnable
    | GPAgentRunnable
    | TaskSchedulerRunnable
)


class FileFilteringResponse(BaseModel):
    """Response model for file filtering with LLM."""

    relevant_indices: list[int]


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

    @staticmethod
    def _build_agent_list(subagent_registry: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Build agent description lines and name list from the subagent registry.

        Each agent is wrapped in ``<agent name="...">`` XML tags so that LLMs
        can clearly see where one agent's description ends and the next begins.

        Args:
            subagent_registry: Mapping of agent name to CompiledSubAgent dicts.

        Returns:
            Tuple of (description lines like ``["<agent name=...>desc</agent>", ...]``,
            name list).
        """
        descriptions: list[str] = []
        names: list[str] = []
        for name, subagent in subagent_registry.items():
            desc = subagent.get("description", f"Agent: {name}")
            descriptions.append(f'<agent name="{name}">\n{desc}\n</agent>')
            names.append(name)
        return descriptions, names

    # Marker text that SubAgentMiddleware appends to the system prompt.
    # Used to locate the "- general-purpose: ..." agent list and replace it
    # with the full set of runtime-discovered agents.
    _SYSTEM_PROMPT_AGENT_MARKER = "Available subagent types:\n"

    # Marker text that SubAgentMiddleware bakes into the task tool description
    # via TASK_TOOL_DESCRIPTION.  Used to locate the agent list inside the tool
    # description and replace it with the full registry.
    _TOOL_DESC_AGENT_MARKER = "Available agent types and the tools they have access to:\n"

    def _enhance_system_prompt_agents(
        self, system_message: SystemMessage | None, user_context: GraphRuntimeContext
    ) -> SystemMessage | None:
        """Replace the agent list in the system prompt with all runtime agents.

        ``SubAgentMiddleware`` appends a text block to the system message that
        contains ``TASK_SYSTEM_PROMPT`` followed by
        ``"Available subagent types:\n- general-purpose: ..."``.

        Because sub-agents are discovered at request time (not graph-creation
        time), the list only contains "general-purpose".  This method finds
        that marker and replaces the ``- name: desc`` lines that follow with
        the full set from ``user_context.subagent_registry``.

        Args:
            system_message: The current system message (may be ``None``).
            user_context: Runtime context carrying the subagent registry.

        Returns:
            A new ``SystemMessage`` with the agent list replaced, or the
            original message unchanged if the marker was not found or the
            registry is empty.
        """
        if system_message is None or not user_context.subagent_registry:
            return system_message

        agent_descs, _ = self._build_agent_list(user_context.subagent_registry)
        if not agent_descs:
            return system_message

        new_agent_block = "\n".join(agent_descs)
        marker = self._SYSTEM_PROMPT_AGENT_MARKER

        # The marker + subsequent agent entries may appear in any content block.
        # We replace in the first block where the marker is found.
        # Pattern: marker text followed by either:
        #   - Old format: lines starting with "- " (from SubAgentMiddleware)
        #   - New format: <agent>...</agent> XML blocks (from our enhancement)
        pattern = re.compile(
            re.escape(marker) + r"(?:(?:- .+(?:\n|$))+|(?:<agent[\s\S]*?</agent>\n?)+)",
            re.MULTILINE,
        )

        found = False
        new_blocks: list[ContentBlock] = []
        for block in system_message.content_blocks:
            if found or not isinstance(block, dict) or block.get("type") != "text":
                new_blocks.append(block)
                continue
            text: str = block.get("text", "")
            if marker in text:
                new_text = pattern.sub(marker + new_agent_block, text, count=1)
                new_blocks.append({"type": "text", "text": new_text})
                found = True
            else:
                new_blocks.append(block)

        if not found:
            # Marker not found — deepagents version may have changed the text.
            # Fall back to appending the agent list.
            logger.warning(
                "DynamicToolDispatchMiddleware: could not find '%s' marker in system prompt; "
                "appending agent list as fallback",
                marker.rstrip(),
            )
            new_blocks.append(
                {
                    "type": "text",
                    "text": f"\n\n{marker}" + new_agent_block,
                }
            )

        return SystemMessage(content_blocks=new_blocks)

    def _enhance_task_tool_schema(
        self, task_tool_dict: dict[str, Any], user_context: GraphRuntimeContext
    ) -> dict[str, Any]:
        """Replace the agent list in the task tool description and update the enum.

        ``SubAgentMiddleware`` bakes a task tool whose description contains
        ``"Available agent types and the tools they have access to:\n- general-purpose: ..."``.
        This method **replaces** that section with the full set of agents from
        ``subagent_registry`` instead of appending a duplicate section.

        Args:
            task_tool_dict: The task tool in OpenAI dict format.
            user_context: User context with ``subagent_registry``.

        Returns:
            Enhanced task tool dict with all subagents in description and enum.
        """
        if not user_context.subagent_registry:
            return task_tool_dict

        agent_descs, agent_names = self._build_agent_list(user_context.subagent_registry)
        if not agent_names:
            return task_tool_dict

        # Get the current parameters schema
        function_dict = task_tool_dict.get("function", {})
        parameters = function_dict.get("parameters", {})
        properties = parameters.get("properties", {})
        subagent_type_prop = properties.get("subagent_type", {})
        original_description: str = function_dict.get("description", "")

        # Replace the agent list that follows the marker, or fall back to append
        marker = self._TOOL_DESC_AGENT_MARKER
        new_agent_block = "\n".join(agent_descs)
        pattern = re.compile(
            re.escape(marker) + r"(?:(?:- .+(?:\n|$))+|(?:<agent[\s\S]*?</agent>\n?)+)",
            re.MULTILINE,
        )

        if marker in original_description:
            enhanced_description = pattern.sub(marker + new_agent_block, original_description, count=1)
        else:
            # Marker not found — fall back to appending
            logger.warning(
                "DynamicToolDispatchMiddleware: could not find '%s' marker in task tool "
                "description; appending agent list as fallback",
                marker.rstrip(),
            )
            enhanced_description = original_description + "\n\nAvailable agents:\n" + new_agent_block

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
           - Response-format tools (SubAgentResponseSchema) are excluded here because
             ``create_agent`` adds them implicitly when AutoStrategy/ToolStrategy is used,
             and the model's response_format binding also adds them to the Bedrock API call,
             which would cause a duplicate ``toolConfig.tools[N]`` ValidationException.
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

        # Response schema tools that are managed by the model's response_format binding
        # (AutoStrategy / ToolStrategy).  Including them in tool_dicts would add a second
        # copy to the Bedrock toolConfig (index N from tool_dicts + index N+1 from
        # AutoStrategy), causing:
        #   ValidationException: The tool SubAgentResponseSchema is already defined at
        #   toolConfig.tools.<N>.
        # These tools are still present in the graph's ToolNode (added by create_agent
        # internally) so the model can call them; they just must not appear twice in the
        # Bedrock API payload.
        _RESPONSE_FORMAT_TOOL_NAMES = {"SubAgentResponseSchema"}

        # 1. Add original tools from the request first (e.g., write_todos, task)
        # These are tools that create_deep_agent provides by default
        # The "task" tool gets enhanced with A2A agent descriptions and enum
        for tool in original_tools or []:
            if isinstance(tool, BaseTool):
                if tool.name not in seen_names and tool.name not in _RESPONSE_FORMAT_TOOL_NAMES:
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
                if name and name not in seen_names and name not in _RESPONSE_FORMAT_TOOL_NAMES:
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

    @staticmethod
    async def _emit_status(stream_writer: Any, message: str) -> None:
        """Emit an a2a_status event via stream_writer for WorkingBlock display.

        Accepts an explicit stream_writer (used when forwarding sub-agent status
        from astream_a2a_agent where the contextvars writer is the sub-agent's,
        not the orchestrator's).  Falls back to get_stream_writer() when None.
        """
        if stream_writer is None:
            try:
                stream_writer = get_stream_writer()
            except Exception:
                return
        try:
            result = stream_writer(("a2a_status", {"message": message}))
            if inspect.iscoroutine(result):
                await result
        except Exception as e:
            logger.debug(f"Failed to emit status: {e}")

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

    def _extract_subagent_response(self, event: StreamEvent, subagent_type: str) -> tuple[str, dict[str, Any] | None]:
        """Extract content and A2A metadata from a subagent StreamEvent.

        Takes the subagent's final message (last in messages list) and extracts:
        - The actual response content
        - A2A metadata if present (context_id, task_id, etc.)

        Args:
            event: The StreamEvent from subagent invocation
            subagent_type: Name of the subagent (for logging)

        Returns:
            Tuple of (content string, a2a_metadata dict or None)
        """
        if isinstance(event, ErrorEvent):
            return f"Error: {event.error}", None

        if not isinstance(event, TaskUpdate):
            return str(event), None

        data = event.data
        content = ""
        a2a_metadata = None

        if isinstance(data, TaskResponseData) and data.messages:
            messages = data.messages
            # Take only the last message - this is the subagent's final synthesized response.
            # The subagent may have had multiple internal turns (tool calls, reasoning),
            # but we only return the final answer to keep the orchestrator's context clean.
            raw_content = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])

            # Handle list content (e.g., models with extended thinking return
            # [{'type': 'thinking', ...}, {'type': 'text', ...}])
            # Extract only text parts, filtering out thinking/reasoning blocks.
            if isinstance(raw_content, list):
                text_parts = []
                for block in raw_content:
                    if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                        text_parts.append(block["text"])
                content = "\n\n".join(text_parts) if text_parts else str(raw_content)
            # Try to parse JSON-wrapped A2A metadata from content
            # Format: {"content": "...", "a2a": {...}}
            elif isinstance(raw_content, str):
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
            content = str(data)

        return content, a2a_metadata

    def _build_subagent_command(
        self,
        event: StreamEvent,
        content: str,
        a2a_metadata: dict[str, Any] | None,
        tool_call_id: str,
        excluded_keys: tuple[str, ...],
    ) -> Command:
        """Build a Command with ToolMessage from a subagent StreamEvent.

        Args:
            event: The StreamEvent from subagent invocation
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
        data = event.data if isinstance(event, TaskUpdate) else TaskResponseData()
        dump = data.model_dump(mode="json", exclude={"messages", "type", "metadata"})
        # Flatten metadata into state update so auth fields etc. propagate
        meta = data.metadata if hasattr(data, "metadata") else {}
        state_update = {k: v for k, v in {**dump, **meta}.items() if k not in excluded_keys}

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
                    streaming=False,
                )
                model = model.with_structured_output(FileFilteringResponse)
            else:
                raise ValueError("Missing agent_settings or cost_logger for LLM-based file filtering")

            response = await model.ainvoke(prompt)
            # with_structured_output ensures `response` is a FileFilteringResponse instance
            if isinstance(response, FileFilteringResponse):
                relevant_indices = response.relevant_indices
            else:
                # Fallback for dict response
                relevant_indices = response.get("relevant_indices", []) if isinstance(response, dict) else []  # type: ignore
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

    @staticmethod
    def _validate_json_input_for_agent(
        description: str, input_modes: list[str], subagent_type: str
    ) -> str | dict[str, Any]:
        """Validate JSON input for agents requiring application/json input.

        Args:
            description: Task description from tool call
            input_modes: List of supported input modes for the agent
            subagent_type: Name of the subagent (for error messages)

        Returns:
            Validated description (str or dict depending on input_modes)

        Raises:
            ValueError: If agent requires JSON but description is not JSON-serializable
        """
        if "application/json" not in input_modes:
            # Agent doesn't require JSON, return as-is
            return description

        # If description is string, try to parse it as JSON
        try:
            return json.loads(description)
        except json.JSONDecodeError as e:
            # if agent supports also text input, we can still pass the description as text and let the agent handle it
            if set(input_modes).intersection({"text", "text/plain"}):
                logger.warning(
                    f"Sub-agent '{subagent_type}' supports both JSON and text input, but description is not valid JSON: {e}. "
                    f"Passing description as text for the agent to handle."
                )
                return description
            raise ValueError(
                f"Sub-agent '{subagent_type}' requires JSON input (application/json), "
                f"but description is not valid JSON: {e}"
            )

    async def _build_subagent_human_message(
        self,
        description: str | dict[str, Any],
        user_context: GraphRuntimeContext,
        subagent: A2ACompiledSubAgent,
    ) -> HumanMessage:
        """Build a HumanMessage for sub-agent dispatch with intelligent file filtering.

        Uses LLM-based filtering to determine which files are relevant to the task,
        avoiding wasteful forwarding of all files to all sub-agents. Optimizes by
        skipping filtering entirely for non-multimodal agents.

        For JSON-input agents (with 'application/json' in input_modes), wraps the
        description dict in a NonStandardContentBlock for structured data handling.

        Args:
            description: Task description from the LLM's tool call (str or dict for JSON agents)
            user_context: Runtime context with pending_file_blocks
            subagent: A2ACompiledSubAgent with is_multimodal metadata

        Returns:
            HumanMessage with appropriate content blocks for the agent's input modes
        """
        runnable = subagent["runnable"]
        input_modes = runnable.input_modes
        subagent_name = subagent["name"]
        _TEXT_ONLY_MODES = {"text", "text/plain"}

        # Check if agent requires JSON input
        requires_json_input = "application/json" in input_modes

        if requires_json_input and isinstance(description, dict):
            # Agent requires JSON input and description is already a dict - handle structured data
            logger.debug(f"Subagent '{subagent_name}' requires JSON input, constructing JSON block")

            # Create JSON content block (NonStandardContentBlock for provider-specific JSON)
            json_block: NonStandardContentBlock = {
                "type": "non_standard",
                "value": {
                    "media_type": "application/json",
                    "data": description,
                },
            }

            # Create text block explaining the JSON input
            all_blocks: list[ContentBlock] = [json_block]

            logger.debug(f"Building JSON HumanMessage with structured data for agent '{subagent_name}'")
            return HumanMessage(content_blocks=all_blocks)

        # For text-only or non-JSON agents, use text content
        # Convert description dict to string if needed
        if isinstance(description, dict):
            description_text = json.dumps(description, indent=2)
        else:
            description_text = description

        if set(input_modes).issubset(_TEXT_ONLY_MODES):
            # Agent only supports text, send text only (optimization: skip filtering)
            logger.debug(f"Subagent '{subagent_name}' is not multimodal, sending text-only message")
            return HumanMessage(content=description_text)

        # Subagent is multimodal - check if we have files to forward
        if not user_context.pending_file_blocks:
            return HumanMessage(content=description_text)

        filtered_files = await self._filter_files_with_llm(
            description_text,
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
                description_text += f"\n\n{description_block['text']}"

        if filtered_files:
            text_block: TextContentBlock = {"type": "text", "text": description_text}
            all_blocks: list[ContentBlock] = [text_block] + filtered_files
            logger.debug(
                f"Building sub-agent HumanMessage with {len(filtered_files)} "
                f"filtered file content blocks (from {len(user_context.pending_file_blocks)} total)"
            )
            return HumanMessage(content_blocks=all_blocks)
        else:
            # No relevant files after filtering, send text only
            logger.debug("No relevant files after filtering, sending text-only message")
            return HumanMessage(content=description_text)

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

        # Filter empty AI messages (see awrap_model_call for full root cause explanation)
        filtered_messages = [
            msg
            for msg in request.messages
            if not (isinstance(msg, AIMessage) and not msg.content and not msg.tool_calls)
        ]
        if len(filtered_messages) != len(request.messages):
            logger.warning(
                f"Filtered {len(request.messages) - len(filtered_messages)} empty AI message(s) "
                f"from history for user {user_context.user_sub}"
            )
            request = request.override(messages=filtered_messages)

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

        # Enhance the system prompt so the LLM sees all runtime-discovered agents
        # (SubAgentMiddleware only listed agents known at graph creation time)
        enhanced_system = self._enhance_system_prompt_agents(request.system_message, user_context)

        # Override request with user's tools and enhanced system prompt
        # Cast needed because list is invariant in Python typing
        return handler(
            request.override(
                system_message=enhanced_system,
                tools=cast(list[BaseTool | dict], tool_dicts),
            )
        )

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

        # Filter empty AI messages from history to prevent Gemini 400 errors.
        # Root cause: When Gemini calls FinalResponseSchema (return_direct=True) alongside
        # a non-return_direct tool (e.g. write_todos), the LangGraph routing logic in
        # factory.py (_make_tools_to_model_edge) routes back to model instead of exiting,
        # because: (1) `all(return_direct)` fails due to the mixed tools, and (2) the
        # `structured_output_tools` exit path doesn't apply since Gemini uses
        # requires_response_tool=True (not ToolStrategy), leaving structured_output_tools
        # empty. The model has nothing left to say and emits an empty AI message
        # (content=[], tool_calls=[], output_tokens=0) which gets checkpointed. On the
        # next turn, langchain_google_genai converts it to Content(role="model", parts=[])
        # and Gemini rejects with 400 "must include at least one parts field".
        filtered_messages = [
            msg
            for msg in request.messages
            if not (isinstance(msg, AIMessage) and not msg.content and not msg.tool_calls)
        ]
        if len(filtered_messages) != len(request.messages):
            logger.warning(
                f"Filtered {len(request.messages) - len(filtered_messages)} empty AI message(s) "
                f"from history for user {user_context.user_sub}"
            )
            request = request.override(messages=filtered_messages)

        # Enhance the system prompt so the LLM sees all runtime-discovered agents
        # (SubAgentMiddleware only listed agents known at graph creation time)
        enhanced_system = self._enhance_system_prompt_agents(request.system_message, user_context)

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

                result = await handler(
                    request.override(
                        system_message=enhanced_system,
                        tools=cast(list[BaseTool | dict], tool_dicts),
                    )
                )
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
        def invoke_a2a_agent(agent_state: dict) -> StreamEvent:
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
        stream_writer: Any = None,
    ) -> ToolMessage | Command | None:
        """Async dispatch 'task' tool call to the appropriate subagent.

        Uses astream() instead of ainvoke() to forward intermediate working
        status updates from the sub-agent to the orchestrator's stream_writer
        (displayed as working steps in the frontend).

        Args:
            tool_call: The tool call with name, id, args
            user_context: User context with subagent_registry
            state: Current agent state
            config: Runtime config
            stream_writer: Optional stream_writer from LangGraph runtime for emitting status events

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
        runnable: OrchestratorSupportedRunnables | None = subagent.get("runnable")
        if runnable is None:
            return ToolMessage(
                content=f"Error: Subagent '{subagent_type}' has no runnable",
                name="task",
                tool_call_id=tool_call_id,
                status="error",
            )

        # Validate JSON input for agents requiring JSON
        try:
            input_modes = runnable.input_modes
            description = self._validate_json_input_for_agent(description, input_modes, subagent_type)
        except ValueError as e:
            # add runnable.description and pyndatic examples to the error message for easier debugging
            return ToolMessage(
                content=f"JSON validation error for subagent '{subagent_type}': {e}. The agent description is: {runnable.description}",
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

        # Extract checkpointer from parent config to prevent LangGraph from misinterpreting checkpoint_ns as subgraph
        # CRITICAL: Without __pregel_checkpointer, LangGraph treats checkpoint_ns as a subgraph identifier
        checkpointer = config.get("configurable", {}).get("__pregel_checkpointer") if isinstance(config, dict) else None

        # Create complete RunnableConfig for sub-agent using SDK utility
        # This ensures FilesystemMiddleware uses consistent (user_id, "filesystem") namespace
        # CRITICAL: checkpoint_ns must be "" for standalone graphs. Sub-agent graphs
        # are standalone (not subgraphs), so LangGraph writes checkpoints at the root
        # namespace. A non-empty checkpoint_ns causes get_state() to look in the wrong
        # namespace. Thread isolation is provided by unique thread_id instead.
        subagent_config = create_runnable_config(
            user_sub=user_sub,
            conversation_id=orchestrator_conversation_id or "unknown",  # Fallback to "unknown" if not available
            user_id=user_id,
            assistant_id=assistant_id or user_id,  # Fallback to user_id if not in parent config
            thread_id=f"{orchestrator_conversation_id or 'unknown'}::{subagent_type}",  # Unique thread_id for checkpoint isolation
            checkpoint_ns="",  # Empty for standalone graph — thread_id provides isolation
            checkpointer=checkpointer,  # Required to prevent checkpoint_ns being treated as subgraph
        )

        # Use a traced function for proper LangSmith visibility
        @traceable(name=f"task:{subagent_type}", run_type="tool")
        async def astream_a2a_agent(agent_state: dict, agent_config: dict) -> AsyncIterable[StreamEvent]:
            """Stream A2A agent with tracing, forwarding working status updates."""
            logger.info(f"[STREAMING] astream_a2a_agent START for {subagent_type}")

            # Emit "Delegating to..." status before starting sub-agent stream
            if stream_writer:
                try:
                    logger.info(f"[STREAMING] Emitting 'Delegating to...' status for {subagent_type}")
                    result = stream_writer(
                        ("status_history", {"message": f"Delegating to {subagent_type}…", "source": "orchestrator"})
                    )
                    if inspect.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.debug(f"Failed to emit delegation status: {e}")
            else:
                logger.warning("[STREAMING] astream_a2a_agent: stream_writer is None")

            last_event: StreamEvent | None = None
            seen_statuses: set[str] = set()  # Dedup status emissions within this stream
            item_count = 0

            logger.info("[STREAMING] Starting async for loop over runnable.astream()")
            async for item in runnable.astream(agent_state, agent_config):
                item_count += 1
                item_type = type(item).__name__
                logger.info(f"[STREAMING] astream_a2a_agent received item #{item_count}: {item_type}")
                last_event = item

                if isinstance(item, TaskUpdate):
                    # Forward activity-log events (tool calls) from sub-agents
                    if isinstance(item, TaskUpdate) and isinstance(item.event_metadata, ActivityLogMeta):
                        status_text = item.status_text
                        if status_text and stream_writer:
                            try:
                                result = stream_writer(
                                    ("status_history", {"message": status_text, "source": subagent_type})
                                )
                                if inspect.iscoroutine(result):
                                    await result
                            except Exception as e:
                                logger.debug(f"Failed to forward sub-agent status history: {e}")
                        continue  # Status history events don't update final result

                    # Forward sub-agent work-plan snapshots as hierarchical todos
                    if isinstance(item, TaskUpdate) and isinstance(item.event_metadata, WorkPlanMeta):
                        todos_raw = item.event_metadata.todos
                        # Tag todos with source for hierarchical frontend display
                        prefixed_todos = [
                            TodoItem(
                                name=t["name"] if isinstance(t, dict) else t.name,
                                state=t["state"] if isinstance(t, dict) else t.state,
                                source=subagent_type,
                            )
                            for t in todos_raw
                            if (isinstance(t, dict) and "name" in t and "state" in t) or isinstance(t, TodoItem)
                        ]
                        if prefixed_todos and stream_writer:
                            try:
                                result = stream_writer(("todo_status", {"todos": prefixed_todos}))
                                if inspect.iscoroutine(result):
                                    await result
                                logger.debug(f"Forwarded {len(prefixed_todos)} sub-agent todos from '{subagent_type}'")
                            except Exception as e:
                                logger.debug(f"Failed to forward sub-agent todos: {e}")
                        continue  # todo snapshots are not status messages

                    # Forward intermediate working-status messages to the orchestrator.
                    # Use the raw A2A protocol status text (from task.status.message)
                    # instead of the synthetic message from _create_synthetic_message_content(),
                    # which wraps content with "INCOMPLETE:" prefixes and may include
                    # protocol noise like tool_use blocks.
                    if isinstance(item, TaskUpdate) and item.data.state == TaskState.working:
                        status_text = item.status_text
                        if status_text and status_text != "Task processed":
                            # Skip duplicates and overly long content (likely full responses, not status)
                            if status_text not in seen_statuses and len(status_text) <= 200:
                                seen_statuses.add(status_text)
                                await self._emit_status(stream_writer, status_text)

                elif isinstance(item, ArtifactUpdate):
                    # Forward streaming artifact chunks from sub-agent as INTERMEDIATE OUTPUT.
                    # CRITICAL: Yield immediately for real-time streaming
                    if item.content:
                        content_len = len(item.content)
                        logger.info(f"[STREAMING] Artifact chunk received: {content_len} chars from {subagent_type}")

                        # Emit via stream_writer for optional custom handling
                        if stream_writer:
                            try:
                                event_payload = {
                                    "content": item.content,
                                    "agent_name": subagent_type,
                                }
                                logger.info(
                                    f"[STREAMING] Emitting subagent_chunk via stream_writer: {content_len} chars"
                                )
                                result = stream_writer(
                                    (
                                        "subagent_chunk",
                                        event_payload,
                                    )
                                )
                                if inspect.iscoroutine(result):
                                    await result
                                logger.info("[STREAMING] stream_writer call completed")
                            except Exception as e:
                                logger.error(f"[STREAMING] Failed to forward sub-agent chunk: {e}", exc_info=True)
                        else:
                            logger.warning("[STREAMING] stream_writer is None - cannot emit subagent_chunk event")

                        # CRITICAL: Yield immediately so this flows through the executor's stream
                        logger.info("[STREAMING] Yielding artifact chunk from astream_a2a_agent generator")
                        yield item
                    continue

                elif isinstance(item, ErrorEvent):
                    yield item
                    return

            logger.info(f"[STREAMING] astream_a2a_agent loop complete after {item_count} items")
            final_item = last_event or ErrorEvent(error="No response received from agent")
            logger.info(f"[STREAMING] Yielding final item from astream_a2a_agent: {type(final_item).__name__}")
            yield final_item

        # Register active sub-agent dispatch so the orchestrator's SteeringMiddleware
        # can forward user follow-up messages to the in-progress sub-agent.
        dispatch_info = ActiveSubagentDispatch(
            subagent_name=subagent_type,
            runnable=runnable,
            orchestrator_context_id=orchestrator_conversation_id or "unknown",
        )
        if orchestrator_conversation_id:
            set_active_subagent_dispatch(orchestrator_conversation_id, dispatch_info)

        try:
            # All runnables (both local and remote) now support astream
            final_result = None
            final_content = None
            final_a2a_metadata = None
            consumer_item_count = 0

            logger.info(f"[STREAMING] Consumer loop starting for {subagent_type}")
            async for result in astream_a2a_agent(subagent_state, subagent_config):
                consumer_item_count += 1
                result_type = type(result).__name__
                logger.info(f"[STREAMING] Consumer received item #{consumer_item_count}: {result_type}")

                # Track different event types
                if isinstance(result, TaskUpdate):
                    logger.info("[STREAMING] Consumer got TaskUpdate, storing as final_result")
                    final_result = result
                    # Update dispatch info with sub-agent's context_id/task_id for steering forwarding
                    if result.data and dispatch_info:
                        dispatch_info.subagent_context_id = result.data.context_id
                        dispatch_info.subagent_task_id = result.data.task_id
                    # CRITICAL: Exit early on terminal state instead of waiting for all items
                    # This allows artifacts (which are emitted via stream_writer) to flow through
                    # the executor's stream incrementally instead of being buffered until now
                    if result.data and result.data.state in TERMINAL_STATES:
                        logger.info(
                            f"[STREAMING] TaskUpdate reached terminal state {result.data.state}, breaking consumer loop early"
                        )
                        break
                elif isinstance(result, ErrorEvent):
                    # Return error immediately
                    logger.error(f"[STREAMING] Consumer got ErrorEvent: {result.error}")
                    return ToolMessage(
                        content=f"Error from subagent '{subagent_type}': {result.error}",
                        name="task",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                else:
                    logger.debug(f"[STREAMING] Consumer ignoring event type: {result_type}")

            logger.info(
                f"[STREAMING] Consumer loop complete after {consumer_item_count} items, final_result={final_result is not None}"
            )

            # Build the final command from the last result
            if final_result is None:
                return ToolMessage(
                    content=f"No response received from subagent '{subagent_type}'",
                    name="task",
                    tool_call_id=tool_call_id,
                    status="error",
                )

            # Extract content and A2A metadata, then build Command
            final_content, final_a2a_metadata = self._extract_subagent_response(final_result, subagent_type)
            return self._build_subagent_command(
                final_result, final_content, final_a2a_metadata, tool_call_id, excluded_keys
            )

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
        finally:
            # Clean up active sub-agent dispatch tracking
            if orchestrator_conversation_id:
                clear_active_subagent_dispatch(orchestrator_conversation_id, dispatch_info)

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
        """Dispatch tool calls to dynamic registries or pass through to ToolNode.

        This middleware is the **outermost** in the ``wrap_tool_call`` chain
        (position [0] in ``_create_middleware_stack``).  Being outermost means
        it can short-circuit before inner middlewares (Auth, Retry, …) see the
        call.  This is intentional for the ``task`` tool: sub-agent dispatch is
        handled entirely here, and the resulting ToolMessage propagates back to
        the caller without going through the inner chain.

        Cases:
        1. ``task`` tool → dispatch to A2A sub-agent via ``_adispatch_task_tool``.
           Short-circuits on success; falls back to handler for local sub-agents.
        2. ``request.tool is not None`` → tool is registered with ToolNode
           (e.g. ``write_todos``) → pass through to handler for standard execution.
        3. ``request.tool is None`` → tool is dynamic (from user's MCP registry)
           → look up from ``GraphRuntimeContext.tool_registry`` and inject into
           the request via ``override(tool=...)``, then call handler so the full
           inner middleware chain (including Auth / Retry) still runs.

        Args:
            request: Tool call request
            handler: Async callback to the next middleware / ToolNode handler

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
            # NOTE: Task delegation status ("Delegating to...") is now emitted by the
            # orchestrator's astream loop via tool call detection, not here.
            # Removed duplicate emission to prevent status history duplicates.

            # Capture the orchestrator's stream_writer now — inside the sub-agent
            # streaming loop the contextvars could shift if sub-agents manipulate them.
            try:
                orchestrator_writer = get_stream_writer()
            except Exception:
                orchestrator_writer = None

            result = await self._adispatch_task_tool(
                tool_call=tool_call,
                user_context=user_context,
                state=request.runtime.state,
                config=request.runtime.config,
                stream_writer=orchestrator_writer,
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
        # NOTE: Tool invocation status is now emitted by astream loop, not here.
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

        # NOTE: Tool invocation status is now emitted by astream loop, not here.
        return await handler(request.override(tool=tool))
