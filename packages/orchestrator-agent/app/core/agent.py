"""Meta-Agent which can be instantiated with personalized configuration
for different users, enabling tailored interactions and responses.

* get_config: Retrieves and applies user-specific configuration settings to customize agent behavior.
* discover_sub_agents: Discovers and integrates sub-agents dynamically based on the user permissions.

Architecture:
- ONE universal graph per model type (not per capability set)
- User context (language, preferences) injected at runtime via `context` parameter
- Tools and sub-agents injected per-user via GraphRuntimeContext.tool_registry and subagent_registry
- DynamicToolDispatchMiddleware handles runtime tool binding and dispatch
- Dynamic system prompt personalizes responses based on GraphRuntimeContext
"""

import logging
from collections.abc import AsyncIterable
from typing import Any, Optional

from a2a.types import Part, TaskState
from object_storage import get_object_storage_service
from agent_common.models.base import DEFAULT_MODEL, DEFAULT_THINKING_LEVEL, ModelType, ThinkingLevel
from langchain.messages import HumanMessage
from langchain_core.messages import AIMessageChunk
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.streaming import (
    StreamBuffer,
    StructuredResponseStreamer,
    extract_text_from_content,
)

from ..core.graph_factory import GraphFactory
from ..handlers import StreamHandler
from ..models import AgentFrameworkAuthError, AgentStreamResponse
from ..models.config import AgentSettings, GraphRuntimeContext, UserConfig
from ..utils import build_runtime_context
from .content_builder import build_text_content
from .discovery import AgentDiscoveryService, ToolDiscoveryService

logger = logging.getLogger(__name__)


# **Role:** You are an expert Routing Delegator. Your primary function is to accurately delegate user inquiries to the appropriate specialized remote agents.

# **Instructions:**
# YOU MUST NOT literally repeat what the agent responds unless asked to do so. Add context, summarize the conversation, and add your own thoughts.
# YOU MUST engage in multi-turn conversations with the agents. NEVER ask the user for permission to engage multiple times with the same agent.
# YOU MUST ALWAYS, UNDER ALL CIRCUMSTANCES, COMMUNICATE WITH ALL AGENTS NECESSARY TO COMPLETE THE TASK.
# NEVER STOP COMMUNICATING WITH THE AGENTS UNTIL THE TASK IS COMPLETED.

# If you have tools available to display information to the user, you MUST use them.

# ${
#   additionalInstructions
#     ? `**Additional Instructions:**\n${additionalInstructions}`
#     : ""
# }

# **Core Directives:**

# * **Task Delegation:** Utilize the \`sendMessage\` function to assign actionable tasks to remote agents.
# * **Contextual Awareness for Remote Agents:** If a remote agent repeatedly requests user confirmation, assume it lacks access to the full conversation history. In such cases, enrich the task description with all necessary contextual information relevant to that specific agent.
# * **Autonomous Agent Engagement:** Never seek user permission before engaging with remote agents. If multiple agents are required to fulfill a request, connect with them directly without requesting user preference or confirmation.
# * **Transparent Communication:** Always present the complete and detailed response from the remote agent to the user.
# * **User Confirmation Relay:** If a remote agent asks for confirmation, and the user has not already provided it, relay this confirmation request to the user.
# * **Focused Information Sharing:** Provide remote agents with only relevant contextual information. Avoid extraneous details.
# * **No Redundant Confirmations:** Do not ask remote agents for confirmation of information or actions.
# * **Tool Reliance:** Strictly rely on available tools to address user requests. Do not generate responses based on assumptions. If information is insufficient, request clarification from the user.
# * **Prioritize Recent Interaction:** Focus primarily on the most recent parts of the conversation when processing requests.
# * **Active Agent Prioritization:** If an active agent is already engaged, route subsequent related requests to that agent using the appropriate task update tool.

# **Agent Roster:**

# * Available Agents:


class OrchestratorDeepAgent:
    """
    OrchestratorDeepAgent - a specialized assistant for planning and orchestration.
    It should be instantiated with user-specific configuration to tailor its behavior.

    Architecture:
    - ONE universal graph per model type (Bedrock vs OpenAI)
    - User context (language, preferences) injected at runtime via `context` parameter
    - Tools and sub-agents injected per-user via GraphRuntimeContext registries
    - DynamicToolDispatchMiddleware handles runtime tool binding and dispatch
    - Dynamic system prompt personalizes responses based on GraphRuntimeContext
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(
        self,
        model: ModelType | None = None,
        thinking_level: ThinkingLevel | None = None,
        cost_logger=None,
    ):
        self.config = AgentSettings()
        self._default_thinking_level: ThinkingLevel | None = thinking_level or DEFAULT_THINKING_LEVEL
        self._default_model_type: ModelType = model or DEFAULT_MODEL

        # Initialize GraphFactory - centralizes all graph-related concerns
        # (model creation, checkpointer, middleware, graph caching)
        # Pass cost_logger during initialization for proper dependency injection
        self._graph_factory = GraphFactory(config=self.config, cost_logger=cost_logger)

        # Initialize client credentials auth for agent-to-agent communication (optional for local dev)
        oidc_client_id = self.config.get_oidc_client_id()
        oidc_client_secret = self.config.get_oidc_client_secret()
        oidc_issuer = self.config.get_oidc_issuer()

        if oidc_client_id and oidc_client_secret and oidc_issuer:
            self.oauth2_client = OidcOAuth2Client(
                client_id=oidc_client_id,
                client_secret=oidc_client_secret.get_secret_value(),
                issuer=oidc_issuer,
            )
            logger.info("Initialized OAuth2 client credentials authenticator")
        else:
            self.oauth2_client = None
            logger.warning("OIDC credentials not configured - agent-to-agent authentication disabled (local dev mode)")

        # Discovery services for tools and sub-agents
        # NOTE: A2A middleware is shared from GraphFactory to track task status
        self.tool_discovery_service = ToolDiscoveryService(self.config, oauth2_client=self.oauth2_client)
        self.agent_discovery_service = AgentDiscoveryService(self.config, oauth2_client=self.oauth2_client)

    def _get_graph(
        self, model_type: ModelType | None = None, thinking_level: ThinkingLevel | None = None
    ) -> CompiledStateGraph:
        """Get a graph for the specified model type.

        Delegates to GraphFactory which handles model creation, caching,
        middleware setup, and graph creation.

        Args:
            model_type: The type of model ('gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6', or 'claude-haiku-4-5')

        Returns:
            CompiledStateGraph: The graph instance (cached or newly created)
        """
        return self._graph_factory.get_graph(model_type, thinking_level=thinking_level)

    def build_runtime_context(self, user_config: UserConfig) -> GraphRuntimeContext:
        """Build GraphRuntimeContext from enriched user config.

        Transforms discovered tools and subagents into registries for dynamic
        tool dispatch at runtime. Call discover_capabilities() first to populate
        tools and sub_agents.

        Args:
            user_config: User configuration enriched with discovered tools/agents

        Returns:
            GraphRuntimeContext: Ready for graph invocation with all registries populated
        """

        # Pass static tools from orchestrator to sub-agents (e.g., get_current_time). We do not pass the
        # response tool since sub-agents have their own response strategy depending on their model.
        static_tools = self._graph_factory.get_static_tools(with_response_tool=False)

        # Extract backend_url from cost_logger if available
        backend_url = None
        if self._graph_factory.cost_logger and hasattr(self._graph_factory.cost_logger, "backend_url"):
            backend_url = self._graph_factory.cost_logger.backend_url

        return build_runtime_context(
            user_config,
            agent_settings=self.config,
            oauth2_client=self.oauth2_client,
            checkpointer=self._graph_factory.checkpointer,
            static_tools=static_tools,
            document_store=self._graph_factory.store,
            storage=get_object_storage_service(),
            document_store_bucket=self.config.DOCUMENT_STORE_S3_BUCKET or None,
            backend_factory=self._graph_factory.backend_factory,
            cost_logger=self._graph_factory.cost_logger,
            backend_url=backend_url,
            gp_graph_provider=self._graph_factory.get_gp_graph,
            task_scheduler_graph_provider=self._graph_factory.get_task_scheduler_graph,
        )

    async def get_or_create_graph(
        self, model_type: ModelType, thinking_level: Optional[ThinkingLevel]
    ) -> CompiledStateGraph:
        """Get or create a graph for the given user configuration.

        Architecture: ONE universal graph per model type with dynamic tool injection.
        - Tools are NOT baked into the graph
        - User tools/subagents come from GraphRuntimeContext at runtime via DynamicToolDispatchMiddleware

        Args:
            model_type: The type of model ('gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6' or 'claude-haiku-4-5')

        Returns:
            CompiledStateGraph: The compiled LangGraph for this model type
        """
        # Get the graph (created lazily if needed)
        # Tools/subagents are NOT passed here - they come from GraphRuntimeContext at runtime
        return self._get_graph(model_type, thinking_level)

    async def stream(
        self,
        message_parts: list[Part],
        user_config: UserConfig,
        config: dict[str, Any],
        resume: Any = None,
    ) -> AsyncIterable[AgentStreamResponse]:
        """
        Stream agent responses with runtime user context injection.

        Args:
            message_parts: User message parts (text + files)
            user_config: User configuration with credentials and preferences
            config: graph config from executor (contains metadata like user_sub, assistant_id).
            resume: Optional resume value for interrupt handling

        ARCHITECTURE:
        - GraphRuntimeContext: Injected at runtime via `context` parameter for personalization
        - thread_id: Used for conversation isolation in checkpointer
        - ONE graph per model type: Shared across users, customized via runtime context

        ZERO-TRUST PRINCIPLES:
        - user_config: Verified user configuration from OIDC provider
        - context_id: Conversation identifier (used for thread isolation in checkpointer)
        - No credentials in checkpoints (GraphRuntimeContext passed at runtime, not persisted)

        FILE HANDLING:
        - message_parts: A2A message parts containing text and optionally files
        - Text is extracted from TextParts
        - Files (FileParts with S3 URIs) are converted to text descriptions
        - The orchestrator decides via tools whether to:
          1. Read file content (to understand and decide next steps)
          2. Generate presigned URL and dispatch to sub-agents

        Args:
            message_parts: List of A2A message parts (text, files, etc.)
            user_config: Verified user configuration with tokens
            context_id: Context identifier for conversation continuity (for thread isolation)
            resume: Optional resume value for continuing from an interrupt.
                   If provided, creates Command(resume=value) instead of normal input.

        Yields:
            AgentStreamResponse: Structured response with state and content

        Examples:
            # Normal execution with text parts
            async for response in agent.stream(message.parts, user_config, "conv-456"):
                print(response.content)

            # Resume from interrupt
            async for response in agent.stream(message.parts, user_config, "conv-456", resume="auth token"):
                print(response.content)

            # Execution with file parts (files are described as text references)
            async for response in agent.stream(parts_with_files, user_config, "conv-456"):
                print(response.content)
        """
        logger.debug(
            f"Processing {len(message_parts)} message parts, "
            f"User sub: {user_config.user_sub}, "
            f"Context ID: {config.get('configurable', {}).get('thread_id')}"
        )

        try:
            # Get or create graph for this model type
            # Graph is shared across users, isolated by thread_id and customized by GraphRuntimeContext
            graph = await self.get_or_create_graph(
                model_type=config["metadata"].get("model_type", self._default_model_type),
                thinking_level=config["metadata"].get("thinking_level", self._default_thinking_level),
            )
        except AgentFrameworkAuthError as e:
            logger.error(f"Authorization error while initializing: {e}")
            yield AgentStreamResponse(
                state=TaskState.failed,
                content="Authorization error. Please check your credentials and try again.",
            )
            return

        # Build GraphRuntimeContext for runtime injection (personalizes system prompt, etc.)
        # UserConfig should already have tools/agents discovered by executor via discover_capabilities()
        runtime_context = self.build_runtime_context(user_config)

        # Determine input based on whether we're resuming or starting fresh
        if resume is not None:
            # Resume from interrupt with the provided resume value
            input_data = Command(resume=resume)
            logger.info(f"Resume input data: Command(resume={resume})")
        else:
            # Build text content from parts
            # Files are described as references - orchestrator decides via tools whether to:
            # 1. Read file content (to understand and decide next steps)
            # 2. Generate presigned URL and dispatch to sub-agents

            # Build user prefix for Slack multi-user attribution
            user_prefix = None
            if runtime_context.slack_user_handle:
                user_prefix = f"{runtime_context.name} {runtime_context.slack_user_handle}"

            text_content, pending_file_blocks = await build_text_content(
                parts=message_parts,
                user_prefix=user_prefix,
            )

            # Store file content blocks on runtime context for deterministic
            # forwarding to sub-agents (bypasses the LLM entirely)
            runtime_context.pending_file_blocks = pending_file_blocks

            # NOTE: we intentionally do NOT include file content in the input to the orchestrator's graph.
            # It will be a sub-agent responsibility to read the file content if needed, using the tools available to it.
            # This keeps the orchestrator's graph focused on orchestration and delegation.
            # The file-analyzer sub-agent can always be used in case the sub-agent input modalities are insufficient
            input_data = {"messages": [HumanMessage(content=text_content)]}
        try:
            # Use streaming with memory for multi-turn conversation support
            chunk_count = 0
            emitted_updates = set()  # Track emitted updates to avoid duplicates

            # Shared streaming helpers for buffer management and structured response parsing
            response_streamer = StructuredResponseStreamer("FinalResponseSchema")
            stream_buffer = StreamBuffer()

            logger.debug("Starting graph.astream with runtime context injection...")

            # The orchestrator's thread_id is used to filter out callback events
            # leaked from sub-agent graphs (GP agent, dynamic agents) that run
            # inside tool calls. Their metadata has a different thread_id
            # (e.g., "{context_id}::general-purpose") while the orchestrator's
            # own model events match the config's thread_id exactly.
            orchestrator_thread_id = config.get("configurable", {}).get("thread_id")

            # Stream the response with CUSTOM EVENTS for progressive A2A status updates
            # and MESSAGE CHUNKS for token-by-token streaming
            # Using stream_mode=['custom', 'messages'] with version="v2":
            # - 'custom': receives progressive status events from middleware
            # - 'messages': receives AIMessageChunk tokens from the LLM
            # v2 format: every chunk is a StreamPart dict:
            #   {"type": "messages"|"custom", "ns": (), "data": ...}
            # CRITICAL: Pass BOTH config and context parameters:
            # - config: Infrastructure (checkpointing via thread_id, metadata for LangSmith)
            # - context: Runtime data (tools, user preferences, sub-agents)
            async for part in graph.astream(
                input_data, config, stream_mode=["custom", "messages"], context=runtime_context, version="v2"
            ):  # type: ignore
                chunk_count += 1
                part_type = part["type"]

                if part_type == "messages":
                    # Token-level streaming from LLM
                    # v2 data: (message_chunk, metadata) tuple
                    msg_chunk, _metadata = part["data"]
                    if not isinstance(msg_chunk, AIMessageChunk):
                        continue

                    # Only process messages from the orchestrator's own graph.
                    # Sub-agent graphs (GP agent, dynamic agents) run inside tool
                    # calls with a different thread_id (e.g., "{ctx}::general-purpose").
                    # Their callback events leak into the orchestrator's stream but
                    # must be filtered out — sub-agents emit their own thinking blocks
                    # via artifact_update events through the middleware.
                    if _metadata.get("thread_id") != orchestrator_thread_id:
                        continue

                    # --- Tool call detection for status history ---
                    # Capture tool calls (excluding FinalResponseSchema and SubAgentResponseSchema) for status history display
                    # Skip "task" tool because middleware emits "Delegating to {subagent}..." instead
                    if msg_chunk.tool_call_chunks:
                        for tc_chunk in msg_chunk.tool_call_chunks:
                            tool_name = tc_chunk.get("name")
                            # Emit status for actual tool calls (not response schemas, not task tool)
                            if (
                                tool_name
                                and tool_name
                                not in ("FinalResponseSchema", "SubAgentResponseSchema", "task", "write_todos")
                                and tool_name not in emitted_updates
                            ):
                                emitted_updates.add(tool_name)
                                yield AgentStreamResponse(
                                    state=TaskState.working,
                                    content=f"Using {tool_name}\u2026",
                                    metadata={"activity_log": True},
                                )
                            # Incremental structured response streaming
                            delta = response_streamer.feed(tc_chunk)
                            if delta:
                                stream_buffer.append(delta)
                                for chunk in stream_buffer.flush_ready():
                                    yield AgentStreamResponse(
                                        state=TaskState.working,
                                        content=chunk,
                                        metadata={"streaming_chunk": True},
                                    )
                        continue

                    # --- Regular content streaming ---
                    if msg_chunk.content:
                        token_text, thinking_blocks = extract_text_from_content(msg_chunk.content)
                        for tb in thinking_blocks:
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content=tb["thinking"],
                                metadata={
                                    "streaming_chunk": True,
                                    "intermediate_output": True,
                                    "agent_name": "orchestrator",
                                },
                            )
                        if token_text:
                            # Filter out FinalResponseSchema JSON that some models
                            # (e.g. Gemini) emit as plain text instead of tool calls.
                            filtered = response_streamer.feed_content(token_text)
                            if filtered:
                                stream_buffer.append(filtered)
                                for chunk in stream_buffer.flush_ready():
                                    yield AgentStreamResponse(
                                        state=TaskState.working,
                                        content=chunk,
                                        metadata={"streaming_chunk": True},
                                    )
                    continue

                if part_type == "custom":
                    # Handle custom events emitted by middleware
                    # v2 data: the raw payload from stream_writer()
                    event = part["data"]
                    if not isinstance(event, tuple) or len(event) != 2:
                        logger.warning(f"Ignoring unexpected custom event: {type(event)}, value: {event}")
                        continue
                    event_type, event_data = event

                    if event_type == "a2a_status":
                        # PROGRESSIVE STATUS UPDATE from A2A middleware
                        status_msg = event_data.get("message", "")
                        if status_msg and status_msg not in emitted_updates:
                            emitted_updates.add(status_msg)
                            logger.info(f"[ORCHESTRATOR] Progressive A2A status: {status_msg}")

                            # Yield immediately to client using A2A protocol state
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content=status_msg,
                            )
                        continue  # Process next event

                    elif event_type == "todo_status":
                        # STRUCTURED WORK PLAN from todo middleware
                        todos = event_data.get("todos", [])
                        if todos:
                            logger.info(f"[ORCHESTRATOR] Work plan: {len(todos)} items")
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content="",
                                metadata={"work_plan": True, "todos": todos},
                            )
                        continue  # Process next event

                    elif event_type == "status_history":
                        # ACTIVITY LOG from tool calls (orchestrator or sub-agents via middleware)
                        status_msg = event_data.get("message", "")
                        source = event_data.get("source")  # sub-agent name if from sub-agent, None if orchestrator
                        if status_msg:
                            metadata = {"activity_log": True}
                            if source:
                                metadata["source"] = source
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content=status_msg,
                                metadata=metadata,
                            )
                        continue  # Process next event

                    elif event_type == "subagent_chunk":
                        # STREAMING CONTENT CHUNK from a sub-agent (via TaskArtifactUpdateEvent)
                        # These are INTERMEDIATE OUTPUTS - the orchestrator will decide whether to
                        # use them as-is, modify them, or completely rewrite them in its final response.
                        # Frontend should display these in a collapsible "Thinking..." section.
                        chunk_content = event_data.get("content", "")
                        subagent_name = event_data.get("agent_name", "sub-agent")
                        if chunk_content:
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content=chunk_content,
                                metadata={
                                    "streaming_chunk": True,
                                    "intermediate_output": True,
                                    "agent_name": subagent_name,
                                },
                            )
                        continue  # Process next event

            # Flush any remaining buffered content
            remaining = stream_buffer.flush_all()
            if remaining:
                yield AgentStreamResponse(
                    state=TaskState.working,
                    content=remaining,
                    metadata={"streaming_chunk": True},
                )

            logger.debug("===== STREAM PROCESSING COMPLETE =====")
            logger.debug(f"Total chunks processed: {chunk_count}")

            # Check if the graph was interrupted
            logger.debug("Getting final state...")
            final_state = graph.get_state(config)  # type: ignore
            logger.debug(f"Final state type: {type(final_state)}")
            logger.debug(f"Final state: {final_state}")

            # Check for general interrupt conditions (pending nodes without specific interrupts)
            # Note: Specific interrupt handling is done in agent_executor for proper A2A task state management
            if hasattr(final_state, "interrupts") and final_state.interrupts:
                task_state = final_state.interrupts[-1].value.get("task_state", TaskState.input_required)
                if task_state == TaskState.auth_required:
                    logger.debug(
                        f"[ORCHESTRATOR] Found auth_required interrupt in final state: {final_state.interrupts[-1].value}"
                    )
                    value: dict = final_state.interrupts[-1].value.copy()
                    yield AgentStreamResponse.auth_required(
                        message=value.pop("message", "Authentication required"),
                        auth_url=value.pop("auth_url", ""),
                        error_code=value.pop("error_code", ""),
                        **value,
                    )
                else:
                    logger.debug(f"[ORCHESTRATOR] Found interrupt in final state: {final_state.interrupts[-1].value}")
                    interrupt_value_dict = (
                        final_state.interrupts[-1].value if isinstance(final_state.interrupts[-1].value, dict) else {}
                    )
                    yield AgentStreamResponse(
                        state=task_state,
                        content=interrupt_value_dict.get(
                            "message", "Process interrupted. Human intervention required."
                        ),
                        interrupt_reason=interrupt_value_dict.get("reason", "graph_interrupted"),
                        pending_nodes=list(final_state.next) if hasattr(final_state, "next") else None,
                        metadata={
                            k: v
                            for k, v in {
                                "interrupt_type": interrupt_value_dict.get("type"),
                                "interrupt_reason": interrupt_value_dict.get("reason"),
                            }.items()
                            if v is not None
                        }
                        or None,
                    )
                return
            if hasattr(final_state, "next") and final_state.next:
                logger.warning(f"graph in final state but no interrupt: {final_state}")
                # If there are pending nodes, the graph was likely interrupted
                yield AgentStreamResponse(
                    state=TaskState.input_required,
                    content="Process interrupted. Human intervention required.",
                    interrupt_reason="graph_interrupted",
                    pending_nodes=list(final_state.next),
                )
                # we don't handle it with a proper interrupt() since is an unexpected state, and resuming the graph
                # might not help if the underlying issue is not resolved.
                return

            if final_state and final_state.values:
                logger.debug("Processing final state values...")
                logger.debug(f"Final state values: {final_state.values}")
                response = self.get_agent_response(final_state.values)
                logger.debug(f"Generated response: {response}")
                yield response
            else:
                logger.debug("No final state or values found")
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content="We are unable to process your request at the moment. Please try again.",
                )

        except GraphRecursionError as e:
            # TODO: should be language-specific
            # Handle recursion limit gracefully with an informative message
            logger.error(f"Recursion limit reached during stream processing: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content="I've been working on this task for a while and need to take a break. "
                "I've made some progress, but the task requires more steps than I can complete in one go. "
                "Would you like me to continue from where I left off, or would you prefer to break this down into smaller tasks?",
            )

        except Exception as e:
            # We are handling here unexpected exceptions during streaming, not handled by middlewares
            # Note: Configuration discovery and graph creation is handled by the executor
            # before calling stream(), so we don't need to re-discover here
            logger.error(f"Exception during stream processing: {e}", exc_info=True)
            # Return as failed
            yield AgentStreamResponse(
                state=TaskState.failed,
                content="An unexpected error occurred while processing your request. Please try again.",
            )

    def get_agent_response(self, final_state) -> AgentStreamResponse:
        """Parse the agent response to extract structured information and check for auth requirements."""
        return StreamHandler.parse_agent_response(final_state)

    async def close(self) -> None:
        """Close and clean up agent resources.

        This method should be called when the agent is no longer needed (e.g., on application shutdown).
        It delegates to the GraphFactory to handle cleanup of cost logger, database connections, etc.
        """
        await self._graph_factory.close()
