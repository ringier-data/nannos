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
from typing import Any

from a2a.types import Part, TaskState
from langchain.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..handlers import StreamHandler
from ..models import AgentFrameworkAuthError, AgentSettings, AgentStreamResponse, UserConfig, build_runtime_context
from ..models.config import GraphRuntimeContext, ModelType
from .content_builder import build_text_content
from .discovery import AgentDiscoveryService, ToolDiscoveryService
from .graph_factory import DEFAULT_MODEL, GraphFactory
from .registry import RegistryService, User
from .s3_service import get_s3_service

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
        thinking: bool = False,
    ):
        self.config = AgentSettings()
        self.thinking = thinking
        self._default_model_type: ModelType = model or DEFAULT_MODEL

        # Initialize GraphFactory - centralizes all graph-related concerns
        # (model creation, checkpointer, middleware, graph caching)
        self._graph_factory = GraphFactory(config=self.config, thinking=thinking)

        # Initialize client credentials auth for agent-to-agent communication
        self.oauth2_client = OidcOAuth2Client(
            client_id=self.config.get_oidc_client_id(),
            client_secret=self.config.get_oidc_client_secret().get_secret_value(),
            issuer=self.config.get_oidc_issuer(),
        )
        logger.info("Initialized OAuth2 client credentials authenticator")

        # Discovery services for tools and sub-agents
        # NOTE: A2A middleware is shared from GraphFactory to track task status
        self.tool_discovery_service = ToolDiscoveryService(self.config, oauth2_client=self.oauth2_client)
        self.agent_discovery_service = AgentDiscoveryService(self.config, oauth2_client=self.oauth2_client)

        # Registry service for user lookups
        self.registry_service = RegistryService()

    def _get_graph(self, model_type: ModelType | None = None) -> CompiledStateGraph:
        """Get a graph for the specified model type.

        Delegates to GraphFactory which handles model creation, caching,
        middleware setup, and graph creation.

        Args:
            model_type: The type of model ('gpt4o' or 'claude-sonnet-4.5')

        Returns:
            CompiledStateGraph: The graph instance (cached or newly created)
        """
        return self._graph_factory.get_graph(model_type)

    async def _get_user_from_registry(
        self, sub: str, access_token: str | None = None, sub_agent_config_hash: str | None = None
    ) -> User:
        """Fetch agents from a service registry using the provided sub.

        Args:
            sub: The user's sub (OIDC subject identifier)
            access_token: The user's access token for authenticated API calls
            sub_agent_config_hash: Optional config hash for playground testing mode

        Returns:
            User object with sub-agents

        Raises:
            ValueError: If user is not found in registry
        """
        user = await self.registry_service.get_user(
            sub, access_token=access_token, sub_agent_config_hash=sub_agent_config_hash
        )
        if not user:
            raise ValueError(f"User with sub {sub} not found in registry")
        return user

    async def discover_capabilities(self, user_config: UserConfig) -> UserConfig:
        """Discover tools and sub-agents for a user based on their permissions.

        Fetches user permissions from registry and discovers available capabilities:
        - Remote A2A sub-agents (with token exchange)
        - MCP tools (with token exchange)
        - Local sub-agent configurations (from playground backend)

        This method is idempotent - if capabilities are already discovered, returns immediately.

        Args:
            user_config: Base user configuration with user_id and tokens

        Returns:
            UserConfig: Enriched with discovered tools, sub_agents, and local_subagents
        """
        # Skip discovery if already done (tools and sub_agents are always set together)
        # Note: local_subagents is optional and may remain None if user has none configured
        if user_config.tools is not None and user_config.sub_agents is not None:
            return user_config

        logger.debug(f"Discovering capabilities for user_id: {user_config.user_id}")

        # Pass access token to authenticate with playground backend
        # In playground mode, only the specified sub-agent is fetched
        user = await self._get_user_from_registry(
            user_config.user_id,
            access_token=user_config.access_token.get_secret_value(),
            sub_agent_config_hash=user_config.sub_agent_config_hash,
        )

        # Discover sub-agents with token exchange and client credentials support
        user_context = {
            "user_id": user_config.user_id,
            "email": user_config.email,
            "name": user_config.name,
        }
        sub_agents = await self.agent_discovery_service.register_agents(
            agent_urls=user.agent_urls,
            token=user_config.access_token.get_secret_value(),
            user_context=user_context,
            streaming_middleware=self._graph_factory.a2a_middleware,
        )

        # Discover tools with token exchange support
        tools = await self.tool_discovery_service.discover_tools(
            user_config.access_token.get_secret_value(),
            # TODO: reason better about how and if mcp tools shall be available to the orchestrator at all
            white_list=user.tool_names if user.tool_names else None,
        )
        logger.debug(f"Discovered {len(sub_agents)} sub-agents: {[agent['name'] for agent in sub_agents]}")

        # Update user_config with discovered data
        user_config.tools = tools
        user_config.sub_agents = sub_agents
        user_config.language = user.language
        user_config.custom_prompt = user.custom_prompt

        # Pass local sub-agent configurations
        if user.local_subagents:
            user_config.local_subagents = user.local_subagents
            logger.info(
                f"Found {len(user.local_subagents)} local sub-agent configs: {[sa.name for sa in user.local_subagents]}"
            )

        logger.debug(f"Discovered {len(user_config.sub_agents) if user_config.sub_agents else 0} sub-agents")
        logger.debug(f"User preferred language: {user.language}")

        return user_config

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
        # Determine if we need Bedrock-specific static tools
        # (FinalResponseSchema is only for Bedrock models)
        is_bedrock = user_config.model == "claude-sonnet-4.5"
        static_tools = self._graph_factory.get_static_tools(is_bedrock)

        return build_runtime_context(
            user_config,
            agent_settings=self.config,
            oauth2_client=self.oauth2_client,
            checkpointer=self._graph_factory.checkpointer,
            static_tools=static_tools,
            document_store=self._graph_factory.store,
            s3_service=get_s3_service(),
            document_store_bucket=self.config.DOCUMENT_STORE_S3_BUCKET or None,
        )

    async def get_or_create_graph(self, model_type: ModelType) -> CompiledStateGraph:
        """Get or create a graph for the given user configuration.

        Architecture: ONE universal graph per model type with dynamic tool injection.
        - Tools are NOT baked into the graph
        - User tools/subagents come from GraphRuntimeContext at runtime via DynamicToolDispatchMiddleware

        Args:
            model_type: The type of model ('gpt4o' or 'claude-sonnet-4.5')

        Returns:
            CompiledStateGraph: The compiled LangGraph for this model type
        """
        # Get the graph (created lazily if needed)
        # Tools/subagents are NOT passed here - they come from GraphRuntimeContext at runtime
        return self._get_graph(model_type)

    async def stream(
        self,
        message_parts: list[Part],
        user_config: UserConfig,
        context_id: str,
        resume: Any = None,
    ) -> AsyncIterable[AgentStreamResponse]:
        """
        Stream agent responses with runtime user context injection.

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
            f"Processing {len(message_parts)} message parts, User ID: {user_config.user_id}, Context ID: {context_id}"
        )

        try:
            # Get or create graph for this model type
            # Graph is shared across users, isolated by thread_id and customized by GraphRuntimeContext
            graph = await self.get_or_create_graph(
                model_type=user_config.model if user_config.model else self._default_model_type
            )
        except AgentFrameworkAuthError as e:
            logger.error(f"Authorization error while initializing: {e}")
            yield AgentStreamResponse(
                state=TaskState.failed,
                content="Authorization error. Please check your credentials and try again.",
            )
            return

        # Build GraphRuntimeContext for runtime injection (personalizes system prompt, etc.)
        # Discovers tools/agents if not already done, then builds context with all registries
        user_config = await self.discover_capabilities(user_config)
        runtime_context = self.build_runtime_context(user_config)

        # Create config with thread_id for conversation isolation
        # GraphRuntimeContext is passed via `context` parameter, NOT stored in config or checkpointed
        config = {
            "configurable": {
                "thread_id": context_id,  # For conversation memory (checkpointed)
            }
        }
        logger.debug(f"Config created with thread_id={context_id}, runtime_context.language={runtime_context.language}")

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

            text_content = build_text_content(
                parts=message_parts,
                user_prefix=user_prefix,
            )

            input_data = {"messages": [HumanMessage(content=text_content)]}
        try:
            # Use streaming with memory for multi-turn conversation support
            chunk_count = 0
            emitted_updates = set()  # Track emitted updates to avoid duplicates

            logger.debug("Starting graph.astream with runtime context injection...")

            # Stream the response with CUSTOM EVENTS for progressive A2A status updates
            # Using stream_mode='custom' to receive both state updates and custom events
            # CRITICAL: Pass runtime_context via `context` parameter for runtime personalization
            async for event in graph.astream(input_data, config, stream_mode="custom", context=runtime_context):  # type: ignore
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
