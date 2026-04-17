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
from collections.abc import AsyncIterable
from typing import Any, Callable, Dict, Optional

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.stream_events import (
    ActivityLogMeta,
    ArtifactUpdate,
    ErrorEvent,
    IntermediateOutputMeta,
    StreamEvent,
    TaskUpdate,
    WorkPlanMeta,
)
from agent_common.a2a.stream_utils import retrieve_final_state
from agent_common.a2a.structured_response import StructuredResponseMixin
from agent_common.core.model_factory import get_model_input_capabilities
from agent_common.models.base import ModelType, ThinkingLevel
from deepagents import CompiledSubAgent
from langchain_core.messages import AIMessageChunk
from langgraph.errors import GraphInterrupt
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.utils.streaming import (
    StreamBuffer,
    StructuredResponseStreamer,
    extract_text_from_content,
)

from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)


GP_DESCRIPTION = (
    "A general-purpose assistant capable of handling a wide variety of tasks. "
    "It has access to all available MCP tools and can perform complex multi-step operations. "
    "Use this agent when no specialized sub-agent is appropriate for the task."
)


class GPAgentRunnable(StructuredResponseMixin, LocalA2ARunnable):
    """Local A2A runnable for the general-purpose agent.

    Wraps a custom GP graph (created by GraphFactory._create_gp_graph) and handles:
    - Automatic checkpoint isolation via LocalA2ARunnable
    - Automatic cost tracking tag injection via LocalA2ARunnable
    - Context injection via graph's context_schema=GraphRuntimeContext
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

    def get_supported_input_modes(self) -> list[str]:
        """Get input modes supported by this GP agent.

        Returns the configured model's native capabilities.
        GP agent supports whatever the underlying LLM model supports.

        Returns:
            List of supported content types
        """
        try:
            return get_model_input_capabilities(self._model_type)  # type: ignore[arg-type]
        except ValueError:
            # Fallback for unknown model types
            return ["text", "image"]

    def get_model_type(self) -> str | None:
        """Return the model type for provider-specific content transforms."""
        return self._model_type

    @property
    def description(self) -> str:
        return GP_DESCRIPTION

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        """Return checkpoint namespace for GP agent."""
        return "general-purpose"

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Return identifier for cost tracking."""
        return "general-purpose"

    async def _astream_impl(self, input_data: SubAgentInput, config: Dict[str, Any]) -> AsyncIterable[StreamEvent]:
        """Stream GP agent execution with real-time status updates and content chunks.

        Streams the internal LangGraph execution to provide progress visibility for:
        - Tool selection decisions
        - Multi-step reasoning
        - Long-running operations
        - Incremental content delivery via artifact_update events

        Args:
            input_data: Validated input with messages and tracking IDs
            config: Extended config with checkpoint isolation and cost tracking

        Yields:
            Status updates and content chunks matching middleware expectations:
            - {\"type\": \"task_update\", \"state\": \"working\", \"data\": {...}, \"is_complete\": False}
            - {\"type\": \"artifact_update\", \"content\": \"...\"} for streaming content chunks
            - Terminal result in final yield

        Raises:
            ValueError: If context_id missing from input
            GraphInterrupt: If user intervention needed
        """
        # Prepare input with multi-modal support (handles content blocks)
        human_message = await self._prepare_human_message_input(input_data)
        context_id, task_id = self._extract_tracking_ids(input_data)
        if context_id is None:
            raise ValueError("Missing context_id in input data")

        try:
            # Clear any cached tool selection from a previous invocation
            self._user_context._cached_selected_tools = None

            # Get GP graph matching current model type
            gp_graph = self._gp_graph_provider(self._model_type, self._thinking_level)

            # CRITICAL: GP graph is standalone, not a subgraph
            # Middleware sets checkpoint_ns for A2A protocol, but we must clear it for standalone graphs
            # Thread isolation already provided by unique thread_id="{context_id}::general-purpose"
            gp_config = {
                **config,
                "configurable": {
                    **config.get("configurable", {}),
                    "checkpoint_ns": "",  # Empty for standalone graph (not a subgraph)
                },
            }

            logger.info(
                f"[COST TRACKING] GP agent streaming with tags: {gp_config.get('tags', [])} (inherited from parent config)"
            )

            # Shared streaming helpers
            response_streamer = StructuredResponseStreamer("SubAgentResponseSchema")
            stream_buffer = StreamBuffer()
            emitted_tool_calls: set[str] = set()  # Track tool calls to avoid duplicates

            # Stream GP graph with custom events and messages using v2 format
            # v2: every chunk is a StreamPart dict: {"type": ..., "ns": ..., "data": ...}
            async for part in gp_graph.astream(
                {"messages": [human_message]},
                config=gp_config,
                context=self._user_context,
                stream_mode=["custom", "messages"],
                version="v2",
            ):
                # Extract working-state messages from intermediate updates
                status_text = None
                part_type = part["type"]

                if part_type == "custom":
                    event_data = part["data"]
                    if isinstance(event_data, tuple) and len(event_data) == 2:
                        event_kind, payload = event_data
                        if isinstance(payload, dict):
                            # Forward work plan updates from the GP sub-agent graph
                            if event_kind == "todo_status" and "todos" in payload:
                                yield TaskUpdate(
                                    event_metadata=WorkPlanMeta(todos=payload["todos"]),
                                )
                                continue
                            if event_kind == "custom":
                                status_text = payload.get("status")
                    elif isinstance(event_data, dict):
                        status_text = event_data.get("status")
                elif part_type == "messages":
                    msg_chunk, _metadata = part["data"]
                    if not isinstance(msg_chunk, AIMessageChunk):
                        continue

                    # --- Tool call detection for activity log + structured response streaming ---
                    if msg_chunk.tool_call_chunks:
                        for tc_chunk in msg_chunk.tool_call_chunks:
                            tool_name = tc_chunk.get("name")
                            # Emit activity log for actual tool calls (not response schemas)
                            if (
                                tool_name
                                and tool_name not in ("FinalResponseSchema", "SubAgentResponseSchema", "write_todos")
                                and tool_name not in emitted_tool_calls
                            ):
                                emitted_tool_calls.add(tool_name)
                                yield TaskUpdate(
                                    status_text=f"Using {tool_name}\u2026",
                                    event_metadata=ActivityLogMeta(),
                                )
                            # Incremental structured response streaming
                            delta = response_streamer.feed(tc_chunk)
                            if delta:
                                stream_buffer.append(delta)
                                for chunk in stream_buffer.flush_ready():
                                    yield ArtifactUpdate(content=chunk)
                        continue

                    # --- Regular content streaming ---
                    if msg_chunk.content:
                        token_text, thinking_blocks = extract_text_from_content(msg_chunk.content)
                        for tb in thinking_blocks:
                            yield ArtifactUpdate(
                                content=tb["thinking"],
                                event_metadata=IntermediateOutputMeta(),
                            )
                        if token_text:
                            # Filter out FinalResponseSchema JSON that some models
                            # (e.g. Gemini) emit as plain text instead of tool calls.
                            filtered = response_streamer.feed_content(token_text)
                            if filtered:
                                stream_buffer.append(filtered)
                                for chunk in stream_buffer.flush_ready():
                                    yield ArtifactUpdate(content=chunk)
                    continue

                # Yield working-state status updates
                if status_text:
                    yield TaskUpdate(
                        status_text=status_text,
                        event_metadata=ActivityLogMeta(),
                    )
            # Flush remaining buffer
            remaining = stream_buffer.flush_all()
            if remaining:
                yield ArtifactUpdate(content=remaining)

            # Retrieve final state (checkpointer saves it after each node)
            final_values = retrieve_final_state(gp_graph, gp_config)
            result = self._translate_agent_result(final_values, context_id, task_id)

            # Yield terminal result
            yield TaskUpdate(
                data=result,
            )

        except GraphInterrupt as gi:
            logger.info(f"[GP AGENT] Graph interrupted during streaming: {gi}")
            raise

        except Exception as e:
            logger.exception(f"GP agent streaming error: {e}")
            error_result = self._build_error_response(
                f"Error streaming general-purpose agent: {e}",
                context_id=context_id,
                task_id=task_id,
            )
            yield ErrorEvent(
                error=str(e),
                data=error_result,
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
