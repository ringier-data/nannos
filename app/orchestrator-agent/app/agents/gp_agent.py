"""General-purpose (GP) agent runnable for handling general-purpose tasks.

This module implements the GP agent as a LocalA2ARunnable, following the same pattern
as DynamicLocalAgentRunnable and other local sub-agents. The GP agent:

1. Is registered as "general-purpose" in subagent_registry
2. Dispatched through the standard _adispatch_task_tool path (no special-casing)
3. Wraps a custom GP graph with ToolsetSelectorMiddleware for smart tool filtering

Architecture:
- GPAgentRunnable holds a reference to GraphRuntimeContext (set at construction time)
- The GP graph is obtained lazily via gp_graph_provider (shared/cached by model type)
- ToolsetSelectorMiddleware handles both Phase 1 (server selection) and Phase 2 (tool
  selection) inside the graph — no pre-selection needed in the runnable
- The graph uses context_schema=GraphRuntimeContext for runtime tool injection

This follows the same LocalA2ARunnable pattern as all other local sub-agents,
avoiding the need for special dispatch code in DynamicToolDispatchMiddleware.
"""

import logging
from typing import Any, Callable, Dict, Optional

from deepagents import CompiledSubAgent
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphInterrupt
from ringier_a2a_sdk.agent.cost_tracking_mixin import CostTrackingMixin
from ringier_a2a_sdk.cost_tracking import CostLogger

from ..a2a_utils.base import LocalA2ARunnable, SubAgentInput
from ..a2a_utils.structured_response import StructuredResponseMixin
from ..models.base import ModelType, ThinkingLevel
from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)


GP_DESCRIPTION = (
    "A general-purpose assistant capable of handling a wide variety of tasks. "
    "It has access to all available MCP tools and can perform complex multi-step operations. "
    "Use this agent when no specialized sub-agent is appropriate for the task."
)


class GPAgentRunnable(CostTrackingMixin, StructuredResponseMixin, LocalA2ARunnable):
    """Local A2A runnable for the general-purpose agent.

    Wraps a custom GP graph (created by GraphFactory._create_gp_graph) and handles:
    - Checkpoint isolation via unique thread_id
    - Context injection via graph's context_schema=GraphRuntimeContext
    - Cost tracking via CostTrackingCallback
    - Structured output via SubAgentResponseSchema (from StructuredResponseMixin)

    Tool selection (server-level and tool-level) is handled entirely by
    ToolsetSelectorMiddleware inside the GP graph.

    Args:
        gp_graph_provider: Callable(model_type, thinking_level) -> CompiledStateGraph
        user_context: GraphRuntimeContext for this user (passed as context= to graph)
        model_type: Model type to select the right cached GP graph
        thinking_level: Optional thinking level for model selection
        cost_logger: Optional CostLogger for tracking tool-selection and graph costs
        user_sub: Optional user subject for cost attribution tags
    """

    def __init__(
        self,
        gp_graph_provider: Callable[..., Any],
        user_context: GraphRuntimeContext,
        model_type: ModelType,
        user_sub: str,
        thinking_level: Optional[ThinkingLevel] = None,
        cost_logger: Optional[CostLogger] = None,
    ):
        super().__init__()
        self._gp_graph_provider = gp_graph_provider
        self._user_context = user_context
        self._model_type = model_type
        self._thinking_level = thinking_level
        self._user_sub = user_sub
        # Configure cost tracking from the shared factory-owned CostLogger
        if cost_logger is not None:
            self.enable_cost_tracking(cost_logger=cost_logger)

    @property
    def name(self) -> str:
        return "general-purpose"

    @property
    def input_modes(self) -> list[str]:
        # TODO: for the time being let's support just text
        return ["text"]

    @property
    def description(self) -> str:
        return GP_DESCRIPTION

    async def _process(self, input_data: SubAgentInput) -> Dict[str, Any]:
        """Process a general-purpose task.

        Flow:
        1. Get GP graph from provider (cached by model_type)
        2. Invoke GP graph with context=user_context
        3. ToolsetSelectorMiddleware inside the graph handles both Phase 1 (server
           selection) and Phase 2 (tool selection), cached for the invocation
        4. Translate result to A2A protocol format

        Args:
            input_data: Validated input with messages and tracking IDs

        Returns:
            Dict with 'messages' and A2A metadata
        """
        content = self._extract_message_content(input_data)
        context_id, task_id = self._extract_tracking_ids(input_data)
        if context_id is None:
            raise ValueError("Missing context_id in input data")

        try:
            # Clear any cached tool selection from a previous invocation
            self._user_context._cached_selected_tools = None

            # Get GP graph matching current model type
            gp_graph = self._gp_graph_provider(self._model_type, self._thinking_level)

            # Build config for GP graph with checkpoint isolation and cost tracking
            # Full config with cost tracking tags, callbacks, and checkpoint isolation
            config = self.create_runnable_config(
                user_sub=self._user_sub,
                conversation_id=context_id,
                thread_id=f"{context_id}::general-purpose",
                checkpoint_ns="general-purpose",
            )
            logger.info(f"[COST TRACKING] GP agent invoking with tags: {config.get('tags', [])}")

            # Invoke GP graph with context for runtime tool injection
            result = await gp_graph.ainvoke(
                {"messages": [HumanMessage(content=content)]},
                config=config,
                context=self._user_context,
            )

            # Translate structured response to A2A protocol format
            # Uses StructuredResponseMixin._translate_agent_result which extracts
            # SubAgentResponseSchema from structured_response or tool call messages
            return self._translate_agent_result(result, context_id, task_id)

        except GraphInterrupt as gi:
            logger.info(f"[GP AGENT] Graph interrupted: {gi}")
            raise

        except Exception as e:
            logger.exception(f"GP agent execution error: {e}")
            return self._build_error_response(
                f"Error executing general-purpose agent: {e}",
                context_id=context_id,
                task_id=task_id,
            )


def create_gp_local_subagent(
    gp_graph_provider: Callable[..., Any],
    user_context: GraphRuntimeContext,
    model_type: ModelType,
    user_sub: str,
    thinking_level: Optional[ThinkingLevel] = None,
    cost_logger: Optional[Any] = None,
) -> CompiledSubAgent:
    """Create a general-purpose local sub-agent and wrap as CompiledSubAgent.

    This factory follows the same pattern as create_dynamic_local_subagent
    and create_file_analyzer_subagent.

    Args:
        gp_graph_provider: Callable(model_type, thinking_level) -> CompiledStateGraph
        user_context: GraphRuntimeContext for this user
        model_type: Model type to use for the GP graph
        user_sub: User subject for cost attribution
        thinking_level: Optional thinking level
        cost_logger: Optional CostLogger for cost tracking

    Returns:
        CompiledSubAgent with name, description, and GPAgentRunnable
    """
    runnable = GPAgentRunnable(
        gp_graph_provider=gp_graph_provider,
        user_context=user_context,
        model_type=model_type,
        user_sub=user_sub,
        thinking_level=thinking_level,
        cost_logger=cost_logger,
    )

    return CompiledSubAgent(
        name="general-purpose",
        description=GP_DESCRIPTION,
        runnable=runnable,
    )
