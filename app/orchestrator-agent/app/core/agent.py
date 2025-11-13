"""
Meta-Agent which can be instantiated with personalized configuration
for different users, enabling tailored interactions and responses.

* get_config: Retrieves and applies user-specific configuration settings to customize agent behavior.
* discover_sub_agents: Discovers and integrates sub-agents dynamically based on the user permissions.

"""

import logging
from collections.abc import AsyncIterable
from typing import Any

from a2a.types import TaskState
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.messages import HumanMessage
from langchain_openai import AzureChatOpenAI
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

# from langgraph.checkpoint.memory import MemorySaver
from langgraph_checkpoint_dynamodb import DynamoDBConfig, DynamoDBSaver, DynamoDBTableConfig

from ..handlers import StreamHandler, handle_auth_error, should_retry
from ..middleware import AuthErrorDetectionMiddleware, TodoStatusMiddleware
from ..models import AgentFrameworkAuthError, AgentSettings, AgentStreamResponse, UserConfig
from ..subagents import A2ATaskTrackingMiddleware
from .discovery import AgentDiscoveryService, ToolDiscoveryService
from .graph_manager import GraphManager

logger = logging.getLogger(__name__)


class OrchestratorDeepAgent:
    """
    OrchestratorDeepAgent - a specialized assistant for planning and orchestration.
    It should be instantiated with user-specific configuration to tailor its behavior.
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        self.config = AgentSettings()
        self.model = AzureChatOpenAI(
            azure_deployment=self.config.get_azure_deployment(),
            temperature=0,
            model=self.config.get_azure_model_name(),
        )
        # OPTIMIZED GRAPH ARCHITECTURE:
        # We maintain one graph per unique configuration (tools/subagents set),
        # NOT per thread. This ensures:
        # 1. User isolation (thread_id in checkpointer separates conversations)
        # 2. No checkpointing of tools/subagents (baked into graph, not in state)
        # 3. Dynamic discovery (new tools/subagents = new graph)
        # 4. No large payloads (only pass thread_id, not tools/subagents)
        # 5. No credentials in checkpoints (tools fetch credentials at runtime)
        # 6. Tools in system prompt (baked into graph creation)
        #
        # Key insight: Multiple users can share the same graph if they have
        # the same tools/subagents available. User isolation comes from thread_id.
        self.graphs = {}  # Cache of graphs by config_signature
        self.tool_discovery_service = ToolDiscoveryService(self.config)
        self.agent_discovery_service = AgentDiscoveryService(self.config)

        # Create memory saver for persistent conversations
        # This is shared across all users but isolates by thread_id
        # Create a checkpointer with the custom configuration
        self.memory = DynamoDBSaver(
            DynamoDBConfig(
                table_config=DynamoDBTableConfig(
                    table_name=self.config.CHECKPOINT_TABLE_NAME,
                    ttl_days=self.config.CHECKPOINT_TTL_DAYS,  # Enable TTL with 14 days expiration (set to None to disable)
                ),
                region_name=self.config.CHECKPOINT_AWS_REGION,
                max_retries=self.config.CHECKPOINT_MAX_RETRIES,
            ),
            deploy=False,
        )

        # Initialize retry middleware for sub-agent tool calls
        # Applies exponential backoff with jitter to all "task" tool invocations (all sub-agents)
        # This provides uniform retry behavior across dynamically discovered A2A sub-agents

        self.retry_middleware = ToolRetryMiddleware(
            max_retries=self.config.MAX_RETRIES,
            # tools=["task"],  # we need to retry all tools not just sub-agent tasks ("task") since otherwise the on_failure won't be called for auth errors from other tools
            backoff_factor=self.config.BACKOFF_FACTOR,  # Exponential backoff: 2s, 4s, 8s
            retry_on=should_retry,  # Custom retry condition excluding 401 errors
            on_failure=handle_auth_error,
        )

        # Initialize the A2A task tracking middleware
        # This will be passed to create_deep_agent, not used for manual wrapping
        self.a2a_middleware = A2ATaskTrackingMiddleware()

        # Initialize the authentication error detection middleware
        # This will detect auth errors from ANY tool and emit auth_required events
        self.auth_middleware = AuthErrorDetectionMiddleware()
        logger.debug(f"Initialized AuthErrorDetectionMiddleware: {self.auth_middleware}")

        # Initialize the todo status middleware
        # This will intercept write_todos tool calls to emit status updates
        self.todo_status_middleware = TodoStatusMiddleware()

        # Initialize graph manager with all middleware
        middleware_stack = [
            self.auth_middleware,  # Auth error detection (outermost)
            self.retry_middleware,  # Retry logic (inside auth)
            self.a2a_middleware,  # A2A task tracking (inside retries)
            self.todo_status_middleware,  # Todo status updates (innermost)
        ]
        self.graph_manager = GraphManager(
            model=self.model,
            checkpointer=self.memory,
            system_prompt=self.config.SYSTEM_INSTRUCTION,
            middleware=middleware_stack,
        )

    async def update_config(self, user_config: UserConfig) -> UserConfig:
        """Get configuration for the orchestrator deep agent.

        Args:
            user_config: Base user configuration with user_id and tokens

        Returns:
            UserConfig: Updated user configuration with discovered sub-agents and tools
        """
        logger.debug(f"Getting config for user_id: {user_config.user_id}")

        # Discover sub-agents with token exchange support
        sub_agents = await self.agent_discovery_service.discover_agents(
            user_config.access_token.get_secret_value(),
            self.a2a_middleware,
        )

        # Discover tools with token exchange support
        tools = await self.tool_discovery_service.discover_tools(
            user_config.access_token.get_secret_value(),
        )
        logger.debug(f"Discovered {len(sub_agents)} sub-agents: {[agent['name'] for agent in sub_agents]}")

        user_config.tools = tools
        user_config.sub_agents = sub_agents
        logger.debug(f"Created config with {len(user_config.sub_agents) if user_config.sub_agents else 0} sub_agents")
        return user_config

    async def get_or_create_graph(self, user_config: UserConfig, force_refresh: bool = False) -> CompiledStateGraph:
        """Get or create a graph for the given user configuration.

        Graphs are cached by configuration signature (tools + subagents), not by user.
        This means multiple users with the same capabilities share the same graph instance,
        but are isolated by thread_id in the checkpointer.

        Args:
            user_config: The user's configuration
            force_refresh: If True, clears cache and re-discovers agents

        Returns:
            CompiledStateGraph: The compiled LangGraph for this user's configuration
        """
        logger.debug(f"Getting or creating graph for user_id: {user_config.user_id}, force_refresh: {force_refresh}")

        # If force_refresh is requested, clear caches
        if force_refresh:
            logger.info("Force refresh requested - clearing all caches")
            self.graph_manager.clear_cache()
        if user_config.tools is None or user_config.sub_agents is None:
            user_config = await self.update_config(user_config)
        # Use graph manager to get or create graph
        compiled_graph = self.graph_manager.get_or_create_graph(
            tools=user_config.tools, subagents=user_config.sub_agents
        )

        return compiled_graph

    async def stream(
        self, query: str, user_config: UserConfig, context_id: str, resume: Any = None
    ) -> AsyncIterable[AgentStreamResponse]:
        """
        Stream agent responses using standard A2A protocol states with runtime config injection.

        ZERO-TRUST ARCHITECTURE:
        - user_config: Verified user configuration from OIDC provider (used for graph selection)
        - context_id: Conversation identifier (used for thread isolation in checkpointer)

        Uses a single shared graph per configuration with proper user isolation:
        1. User isolation (verified user_id selects graph, context_id isolates conversations)
        2. No checkpointing of tools/subagents (runtime-only)
        3. Dynamic discovery (can change between requests)
        4. No large payloads (config contains only thread_id)
        5. No credentials in checkpoints (injected at runtime)

        Args:
            query: User query to process
            user_config: Verified user configuration with tokens
            context_id: Context identifier for conversation continuity (for thread isolation)
            resume: Optional resume value for continuing from an interrupt.
                   If provided, creates Command(resume=value) instead of normal input.

        Yields:
            AgentStreamResponse: Structured response with state and content

        Examples:
            # Normal execution with zero-trust pattern
            async for response in agent.stream("Hello", user_config, "conv-456"):
                print(response.content)

            # Resume from interrupt
            async for response in agent.stream("I've authorized", user_config, "conv-456", resume="auth token"):
                print(response.content)
        """
        logger.debug(f"Query: {query}, User ID: {user_config.user_id}, Context ID: {context_id}")

        try:
            # The graph is cached by config signature (tools + subagents), not by user or conversation.
            # Multiple users with same capabilities share the same graph, isolated by thread_id.
            graph = await self.get_or_create_graph(
                user_config=user_config,
            )
        except AgentFrameworkAuthError as e:
            logger.error(f"Authorization error while initializing: {e}")
            yield AgentStreamResponse(
                state=TaskState.failed,
                content="Authorization error. Please check your credentials and try again.",
            )
            return

        # Create config with thread_id for conversation isolation
        # Tools/subagents are baked into the graph (NOT in config, NOT checkpointed)
        # This ensures:
        # - No large payloads passed with each request (only thread_id)
        # - No credentials in checkpoints (tools access them at runtime)
        # - User isolation via thread_id in checkpointer
        config = {
            "configurable": {
                "thread_id": context_id,  # For conversation memory (checkpointed)
            }
        }
        logger.debug("Config created with thread_id for isolation")

        # Determine input based on whether we're resuming or starting fresh
        if resume is not None:
            # Resume from interrupt with the provided resume value
            input_data = Command(resume=resume)
            logger.info(f"Resume input data: Command(resume={resume})")
        else:
            # Normal input format for LangChain v1.0.0 with memory
            input_data = {"messages": [HumanMessage(content=query)]}
        try:
            # Use streaming with memory for multi-turn conversation support
            chunk_count = 0
            emitted_updates = set()  # Track emitted updates to avoid duplicates

            logger.debug("Starting graph.astream...")

            # Stream the response with CUSTOM EVENTS for progressive A2A status updates
            # Using stream_mode='custom' to receive both state updates and custom events
            async for event in graph.astream(input_data, config, stream_mode="custom"):  # type: ignore
                chunk_count += 1
                logger.info(f"===== EVENT {chunk_count} =====")
                logger.info(f"Event type: {type(event)}")

                # Handle custom events emitted by middleware (progressive A2A status and todo updates)
                if isinstance(event, tuple) and len(event) == 2:
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
                        # PROGRESSIVE TODO STATUS UPDATE from todo middleware
                        status_msg = event_data.get("message", "")
                        if status_msg and status_msg not in emitted_updates:
                            emitted_updates.add(status_msg)
                            logger.info(f"[ORCHESTRATOR] Progressive todo status: {status_msg}")

                            # Yield immediately to client using A2A protocol state
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content=status_msg,
                            )
                        continue  # Process next event

                # Handle regular state chunks - cast to dict for type checking
                if not isinstance(event, dict):
                    logger.warning(f"Ignoring non-dict event: {type(event)}, value: {event}")
                    continue

                chunk = event
                logger.debug(f"Chunk keys: {list(chunk.keys())}")
                logger.debug(f"Full chunk: {chunk}")

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
                    logger.info(
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
                    yield AgentStreamResponse(
                        state=task_state,
                        content=final_state.interrupts[-1].value.get(
                            "message", "Process interrupted. Human intervention required."
                        ),
                        interrupt_reason="graph_interrupted",
                        pending_nodes=list(final_state.next) if hasattr(final_state, "next") else None,
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
