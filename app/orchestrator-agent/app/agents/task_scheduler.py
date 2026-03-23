"""Task Scheduler Sub-Agent.

A local sub-agent for managing scheduled tasks and automated workflows.
This is a built-in capability, not an external A2A service, providing:

1. Clean LangSmith observability (separate agent trace)
2. Dedicated scheduling expertise and context
3. Integration with scheduler and playground management tools via LangGraph
4. Consistent sub-agent interface with the rest of the system

The sub-agent handles:
- Creating scheduled jobs (task and watch types)
- Managing existing schedules (list, get, update, pause, resume)
- Creating automated sub-agents for task execution
- Validating watch conditions before scheduling
- Generating watch parameters from natural language

This module uses a full LangGraph agent (similar to GP agent) with middleware
for tool access, providing the task-scheduler with the same capabilities as
other LangGraph-based sub-agents.

Configuration:
- Model can be set via TASK_SCHEDULER_MODEL environment variable
- Default: claude-3.7-sonnet (strategic planning and scheduling)
- Tools are provided via DynamicToolDispatchMiddleware from SYSTEM_TOOLS
"""

import logging
import os
from typing import Any, Callable, Dict, Optional

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.structured_response import (
    StructuredResponseMixin,
)
from agent_common.models.base import ModelType
from deepagents import CompiledSubAgent
from langchain_core.messages import HumanMessage
from ringier_a2a_sdk.cost_tracking import CostLogger

logger = logging.getLogger(__name__)

# Sub-agent configuration
TASK_SCHEDULER_NAME = "task-scheduler"
TASK_SCHEDULER_DESCRIPTION = (
    "Manages scheduled tasks, automated workflows, and condition-based notifications. Use this agent to:\n"
    "- Create scheduled jobs (one-time, recurring, or watch-based)\n"
    "- Set up monitoring and alerts (notify when conditions are met)\n"
    "- List, view, update, pause, or resume existing schedules\n"
    "- Create automated sub-agents for task execution\n"
    "- Validate watch conditions before scheduling\n"
    "- Generate watch parameters from natural language descriptions\n"
    "\n"
    "Examples:\n"
    "- 'Schedule a daily joke at 9am'\n"
    "- 'Let me know when PR #273 is merged'\n"
    "- 'Notify me when the CI build fails'\n"
    "- 'Alert me if issue ABC-123 is closed'\n"
    "- 'Create a watch to monitor Jira tickets and notify when new P1 issues are created'\n"
    "- 'Show me all my scheduled jobs'\n"
    "- 'Pause the daily report job'\n"
    "\n"
    "The task-scheduler has access to all scheduling and sub-agent management tools."
)

# Default model for task scheduling (strategic planning)
# Can be overridden via TASK_SCHEDULER_MODEL environment variable
DEFAULT_TASK_SCHEDULER_MODEL: ModelType = "claude-sonnet-4.6"
PLAYGROUND_FRONTEND_URL = os.getenv("PLAYGROUND_FRONTEND_URL", "http://localhost:5173")
# System prompt for the task scheduler agent
TASK_SCHEDULER_SYSTEM_PROMPT = """You are a task scheduling specialist responsible for managing scheduled tasks and automated workflows.

Your responsibilities:
1. **Schedule Management**: Create, list, update, pause, resume, and delete scheduled jobs
2. **Sub-Agent Creation**: Create automated sub-agents for task execution when needed
3. **Watch Jobs**: Set up watch jobs that monitor conditions and trigger actions
4. **Validation**: Validate watch conditions before scheduling to ensure they work correctly
5. **User Guidance**: Help users refine their scheduling requirements and notification preferences

Available Tools:
- `scheduler_create_job`: Create a new scheduled job (task or watch type)
- `scheduler_list_jobs`: List all scheduled jobs for the user
- `scheduler_get_job`: Get details about a specific job
- `scheduler_update_job`: Update an existing job's configuration
- `scheduler_pause_job`: Pause a job temporarily
- `scheduler_validate_watch`: Test a watch condition before scheduling
- `playground_list_mcp_servers`: List available MCP servers
- `playground_grep_mcp_tools`: Search tools details with input and optionally output schemas for a specific MCP server
- `playground_list_sub_agents`: List existing sub-agents (check before creating new ones)
- `playground_create_sub_agent`: Create a new automated sub-agent for task execution

Job Types:
1. **Task Jobs**: Execute an automated sub-agent on a schedule (cron, interval, or one-time)
   - Requires: `sub_agent_id` (reference to an automated sub-agent)
   - Schedule: `cron_expr`, `interval_seconds`, or `run_at` (ISO datetime)
   
2. **Watch Jobs**: Monitor a condition and execute actions when met
   - Requires: `check_tool`, `check_args`, `condition_expr` (JSONPath), `expected_value` (what to compare against)
   - Optional: `sub_agent_id` for actions when condition is met
   - Poll interval: `interval_seconds`

Workflow for Creating Task Jobs:
1. **Check for existing sub-agents** using `playground_list_sub_agents`
   - Look for sub-agents with matching purpose/description
   - Filter by `agent_type='automated'` and check if any match the user's intent
   
2. **Create sub-agent if needed** using `playground_create_sub_agent`
   - Use `agent_type='automated'`
   - Provide clear `name`, `description`, and `system_prompt`
   - Store the `sub_agent_id` from the response
   
3. **Create the scheduled job** using `scheduler_create_job`
   - Set `job_type='task'`
   - Reference the `sub_agent_id`
   - Configure schedule (cron, interval, or one-time)
   - Optional: Set up notification via `delivery_channel_id` (omit if not available)

Workflow for Creating Watch Jobs:
1. **Discover MCP servers** using `playground_list_mcp_servers`
   - Get a high-level overview of available integration servers
   - Choose the server that matches the monitoring target
   
2. **Explore tools in the target server** using `playground_grep_mcp_tools`
   - Pass the server_slug from step 1 and the search query to get a list of tools with details about inputs and optional outputs schemas
   - CRITICAL: Tool names are EXACT and case-sensitive - copy them exactly
   
3. **Understand the watch condition**
   - Work with the user to define what to monitor
   - Use the tool input schema from step 2 to construct valid `check_args`
   - Use the tool output schema from step 2 to construct valid `condition_expr` (JSONPath to extract the value)
   - Determine `expected_value` - what value the extracted result should match (or null to check "is not null")
   - CRITICAL: If no output schema is available try to call the tool with example args to see the output format and adjust the condition expression accordingly.
   
4. **Validate the watch** using `scheduler_validate_watch`
   - Test the condition before scheduling using the EXACT tool name from steps 2-3
   - Verify it returns expected results
   - If validation fails, review the tool schema again to fix `check_args`
   
5. **Create the watch job** using `scheduler_create_job`
   - Set `job_type='watch'`
   - Use the validated watch parameters (`check_tool`, `check_args`, `condition_expr`, `expected_value`)
   - CRITICAL: Use the EXACT tool name discovered in steps 2-3
   - Optional: Add `sub_agent_id` for actions when condition is met
   - Configure polling interval
   - Handle delivery channel (see below)

Notification Delivery (CRITICAL for watch jobs):
- Users will ALWAYS receive in-app notifications via WebSocket when jobs complete (automatic)
- When a user requests additional notifications ('let me know when', 'notify me', 'alert me'), ask for delivery channel ID
- Users must configure delivery channels in their Settings page (Slack, email, Google Chat webhooks)
- IF user provides a delivery channel ID:
  - Include `delivery_channel_id` in the job creation
  - They'll get BOTH in-app notifications AND webhook notifications
- IF user doesn't have a delivery channel ID:
  - Create the watch job WITHOUT the `delivery_channel_id` field (OMIT it entirely - do NOT use placeholders like '<UNKNOWN>')
  - Inform user: "The watch job is created and you'll receive in-app notifications when the condition is met. You can add a delivery channel later in Settings for additional notifications (Slack, email, etc.)"
  - Explain: They can update the job later using `scheduler_update_job` to add `delivery_channel_id`
- NEVER use placeholder values like '<UNKNOWN>' for delivery_channel_id - either provide a valid integer ID or omit the field entirely

Best Practices:
- Always check for existing sub-agents before creating new ones
- Validate watch conditions before scheduling
- Use descriptive names for jobs and sub-agents
- Provide clear system prompts for automated sub-agents
- Test with `scheduler_validate_watch` before creating watch jobs
- Confirm schedule details with users (timezone, frequency, etc.)

Response Format:
- Provide clear confirmation when jobs are created
- Show job IDs and next run times
- Explain what will happen when the job executes
- Guide users on how to monitor or modify schedules
- Provide a link to the newly created scheduled job in the UI: PLAYGROUND_FRONTEND_URL/app/scheduler/{scheduled_job_id}
""".replace("PLAYGROUND_FRONTEND_URL", PLAYGROUND_FRONTEND_URL)


class TaskSchedulerRunnable(StructuredResponseMixin, LocalA2ARunnable):
    """Local A2A runnable for the task scheduler agent.

    Wraps a custom task-scheduler graph with middleware for tool access, providing:
    - Automatic checkpoint isolation via LocalA2ARunnable
    - Automatic cost tracking tag injection via LocalA2ARunnable
    - Context injection via graph's context_schema=GraphRuntimeContext
    - Structured output via SubAgentResponseSchema

    Tool access is handled by DynamicToolDispatchMiddleware with scheduler/playground tools
    from SYSTEM_TOOLS (always available regardless of user whitelist).

    Args:
        task_scheduler_graph_provider: Callable() -> CompiledStateGraph
        user_context: GraphRuntimeContext for this user (passed as context= to graph)
        model_type: Model type to select the right cached graph
        user_sub: User subject for cost attribution tags
        cost_logger: Optional CostLogger for tracking costs
    """

    def __init__(
        self,
        task_scheduler_graph_provider: Callable[..., Any],
        user_context: Any,  # GraphRuntimeContext
        model_type: ModelType,
        user_sub: str,
        cost_logger: Optional[CostLogger] = None,
    ):
        """Initialize the task scheduler runnable."""
        super().__init__()
        self._graph_provider = task_scheduler_graph_provider
        self._user_context = user_context
        self._model_type = model_type
        self._user_sub = user_sub
        # Configure cost tracking from the shared factory-owned CostLogger
        if cost_logger is not None:
            self.enable_cost_tracking(cost_logger=cost_logger)

    @property
    def name(self) -> str:
        """Return the sub-agent name."""
        return TASK_SCHEDULER_NAME

    @property
    def description(self) -> str:
        """Return the sub-agent description."""
        return TASK_SCHEDULER_DESCRIPTION

    @property
    def input_modes(self) -> list[str]:
        """Task scheduler only needs text input."""
        return ["text"]

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        """Return checkpoint namespace for task scheduler."""
        return "task-scheduler"

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Return identifier for cost tracking."""
        return "task-scheduler"

    def _extract_message_content(self, input_data: SubAgentInput) -> str:
        """Extract text content from the input message.

        Args:
            input_data: Sub-agent input with task description

        Returns:
            Text content from the message
        """
        message = input_data.messages[0]
        if hasattr(message, "content"):
            content = message.content
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                # Extract text from content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif hasattr(block, "text"):
                        text_parts.append(block.text)  # type: ignore[attr-defined]
                return " ".join(text_parts)
        return str(message)

    async def _process(self, input_data: SubAgentInput, config: Dict[str, Any]) -> Dict[str, Any]:
        """Process a task scheduling request.

        Args:
            input_data: Sub-agent input with task description
            config: Extended config from ainvoke (checkpoint isolation + cost tracking already applied)

        Returns:
            Dict with result from the LangGraph execution
        """
        # Extract user's request
        content = self._extract_message_content(input_data)
        logger.info(f"Task scheduler invoked: {content[:100]}...")

        # Build HumanMessage
        message = HumanMessage(content=content)

        # Config is already extended by ainvoke with checkpoint isolation and cost tracking
        logger.info(f"[COST TRACKING] Invoking task-scheduler with tags: {config.get('tags', [])}")

        # Get the graph and invoke with BOTH config and context:
        # - config: Checkpoint isolation, cost tracking, metadata propagation
        # - context: User-specific tools, preferences, and sub-agents
        graph = self._graph_provider()
        result = await graph.ainvoke(
            {"messages": [message]},
            config=config,  # Infrastructure: checkpointing, tracking, metadata
            context=self._user_context,  # Runtime data: tools, preferences
        )

        # Extract tracking IDs for result translation
        context_id, task_id = self._extract_tracking_ids(input_data)

        logger.debug(
            f"[TASK-SCHEDULER] Graph result keys: {result.keys() if isinstance(result, dict) else 'not a dict'}"
        )
        logger.debug(
            f"[TASK-SCHEDULER] Has structured_response: {'structured_response' in result if isinstance(result, dict) else False}"
        )
        if isinstance(result, dict) and "messages" in result:
            logger.debug(f"[TASK-SCHEDULER] Message count: {len(result['messages'])}")
            if result["messages"]:
                last_msg = result["messages"][-1]
                logger.debug(f"[TASK-SCHEDULER] Last message type: {type(last_msg).__name__}")
                if hasattr(last_msg, "content"):
                    logger.debug(
                        f"[TASK-SCHEDULER] Last message content (first 200 chars): {str(last_msg.content)[:200]}"
                    )

        # Translate structured response to A2A protocol format
        # Uses StructuredResponseMixin._translate_agent_result which extracts
        # SubAgentResponseSchema from structured_response or tool call messages
        translated = self._translate_agent_result(result, context_id, task_id)
        logger.debug(
            f"[TASK-SCHEDULER] Translated result state: {translated.state}, is_complete: {translated.is_complete}"
        )
        return translated


def create_task_scheduler_subagent(
    task_scheduler_graph_provider: Callable[..., Any],
    user_context: Any,  # GraphRuntimeContext
    model_type: ModelType,
    user_sub: str,
    cost_logger: Optional[CostLogger] = None,
) -> CompiledSubAgent:
    """Create the task scheduler sub-agent.

    Args:
        task_scheduler_graph_provider: Callable() -> CompiledStateGraph for task-scheduler
        user_context: GraphRuntimeContext for this user
        model_type: Model type for the graph
        user_sub: User subject for cost attribution
        cost_logger: Shared CostLogger instance from GraphFactory

    Returns:
        CompiledSubAgent that can be registered with the orchestrator
    """
    runnable = TaskSchedulerRunnable(
        task_scheduler_graph_provider=task_scheduler_graph_provider,
        user_context=user_context,
        model_type=model_type,
        user_sub=user_sub,
        cost_logger=cost_logger,
    )

    # Cast to Any for CompiledSubAgent compatibility (duck typing)
    return CompiledSubAgent(
        name=runnable.name,
        description=runnable.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
