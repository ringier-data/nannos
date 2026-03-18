"""LangGraph + Bedrock base agent implementation.

This module provides a base class for A2A agents that use LangGraph with AWS Bedrock
and MCP tools. It extracts common initialization, tool discovery, and streaming patterns
to eliminate code duplication across similar agent implementations.
"""

import asyncio
import logging
import os
import re
from abc import abstractmethod
from collections.abc import AsyncIterable

import boto3
from a2a.types import Task, TaskState
from botocore.config import Config as BotoConfig
from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import AutoStrategy
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.graph.state import CompiledStateGraph
from langgraph_checkpoint_aws import DynamoDBSaver
from pydantic import BaseModel, Field

from ..agent.base import BaseAgent
from ..middleware.bedrock_prompt_caching import BedrockPromptCachingMiddleware
from ..middleware.credential_injector import BaseCredentialInjector
from ..models import AgentStreamResponse, UserConfig

logger = logging.getLogger(__name__)


class FinalResponseSchema(BaseModel):
    """Schema for final response from Bedrock models."""

    task_state: str = Field(
        ...,
        description="The final state of the task: 'completed', 'failed', 'input_required', or 'working'",
    )
    message: str = Field(
        ...,
        description="A clear, helpful message to the user about the task outcome",
    )


class LangGraphBedrockAgent(BaseAgent):
    """Base class for LangGraph agents using AWS Bedrock and MCP tools.

    This base class provides common functionality for agents that:
    - Use AWS Bedrock (Claude/other models) as the LLM
    - Discover and use MCP tools from a server
    - Use DynamoDB checkpointers with S3 offloading
    - Implement LangGraph-based agent workflows
    - Stream responses with structured final state

    Subclasses must implement:
    - _get_mcp_connections(): Return async MCP server connection configuration
    - _get_system_prompt(): Return agent-specific system prompt
    - _get_checkpoint_namespace(): Return unique checkpoint namespace
    - _get_bedrock_model_id(): Return Bedrock model ID (optional, has default)
    - _get_middleware(): Return agent middleware list (optional, default [])
    - _create_graph(): Create LangGraph with tools (optional, has default implementation)

    Architecture:
    - MCP tools discovered once at initialization (lazy loading on first request)
    - Shared DynamoDB checkpointer for conversation persistence
    - Single graph instance reused across requests
    - Cost tracking integration via BaseAgent
    - Credential injection for both handshake and tool-call time
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self, tool_query_regex: str | None = None):
        """Initialize the LangGraph Bedrock Agent.

        Sets up:
        - AWS Bedrock client with timeout/retry configuration
        - DynamoDB checkpointer with S3 offloading
        - Cost tracking (optional, based on env vars)
        - MCP tool lazy discovery state
        """
        super().__init__()

        # Bedrock configuration
        self.bedrock_region = os.getenv("AWS_BEDROCK_REGION", "eu-central-1")
        self.bedrock_model_id = self._get_bedrock_model_id()

        # Create checkpointer
        self._checkpointer = self._create_checkpointer()

        # Create Bedrock client and model
        bedrock_client = self._create_bedrock_client()
        self._model = self._create_bedrock_model(bedrock_client)

        # Initialize cost tracking (optional)
        self._init_cost_tracking()

        # MCP tools will be discovered lazily on first request
        self._mcp_tools: list[BaseTool] | None = None
        self._mcp_tools_lock = False
        logger.info("MCP tool discovery will happen on first request")

        # Graph and MCP client
        self._graph: CompiledStateGraph | None = None
        self._mcp_client: MultiServerMCPClient | None = None
        self.tool_query_regex = re.compile(tool_query_regex) if tool_query_regex else None

    async def get_headers(self) -> dict[str, str]:
        """Apply credential headers from injector for MCP initial handshake.

        This helper method extracts credentials from context variables (which are
        set by BaseAgent.stream()) and applies them to MCP connection headers
        using the same credential injector logic used for tool-call-time injection.

        The method:
        1. Gets credentials from async context variables (thread-safe)
        2. Finds the first BaseCredentialInjector in _get_tool_interceptors()
        3. Calls the injector's _get_authorization_header() to format credentials
        4. Returns a headers dict containing the Authorization header

        Returns:
            Dictionary with "Authorization" header, or empty dict if no injector found

        Raises:
            ValueError: If credentials are not set in context
            (The injector will raise this if credentials are missing)

        Example:
            ```python
            async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
                headers = await self.get_headers()
                return {
                    "server": StreamableHttpConnection(
                        transport="streamable_http",
                        url="https://example.com/mcp",
                        headers=headers,
                    )
                }
            ```
        """

        # Find the first credential injector in the interceptors list
        interceptors = self._get_tool_interceptors()
        for interceptor in interceptors:
            if isinstance(interceptor, BaseCredentialInjector):
                logger.info(f"Found credential injector for MCP handshake: {interceptor.__class__.__name__}")
                return await interceptor.get_headers()

        # No credential injector found
        logger.debug("No credential injector found in _get_tool_interceptors(), skipping header injection")
        return {}

    def _filter_tools(self, tools: list[BaseTool]) -> list[BaseTool]:
        """Helper method to filter tools by query blob.

        Args:
            tools: A list of BaseTool instances
        """
        if not self.tool_query_regex:
            return tools
        filtered_tools = [tool for tool in tools if self.tool_query_regex.search(tool.name)]
        logger.info(
            f"Filtered tools with pattern '{self.tool_query_regex.pattern}': {[tool.name for tool in filtered_tools]}"
        )
        return filtered_tools

    def _create_bedrock_client(self) -> boto3.client:
        """Create configured Bedrock client from environment variables.

        Reads configuration from:
        - BEDROCK_READ_TIMEOUT (default: 300s)
        - BEDROCK_CONNECT_TIMEOUT (default: 10s)
        - BEDROCK_MAX_RETRY_ATTEMPTS (default: 3)
        - BEDROCK_RETRY_MODE (default: adaptive)

        Returns:
            Configured boto3 bedrock-runtime client
        """
        read_timeout = int(os.getenv("BEDROCK_READ_TIMEOUT", "300"))
        connect_timeout = int(os.getenv("BEDROCK_CONNECT_TIMEOUT", "10"))
        max_attempts = int(os.getenv("BEDROCK_MAX_RETRY_ATTEMPTS", "3"))
        retry_mode = os.getenv("BEDROCK_RETRY_MODE", "adaptive")

        boto_config = BotoConfig(
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            retries={
                "max_attempts": max_attempts,
                "mode": retry_mode,
            },
        )

        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=self.bedrock_region,
            config=boto_config,
        )

        logger.info(
            f"Created Bedrock client with read_timeout={read_timeout}s, "
            f"connect_timeout={connect_timeout}s, max_retry_attempts={max_attempts} ({retry_mode} mode)"
        )

        return bedrock_client

    def _create_checkpointer(self) -> DynamoDBSaver:
        """Create DynamoDB checkpointer with S3 offloading from environment.

        Reads configuration from:
        - CHECKPOINT_DYNAMODB_TABLE_NAME (required)
        - CHECKPOINT_AWS_REGION (default: eu-central-1)
        - CHECKPOINT_TTL_DAYS (default: 14)
        - CHECKPOINT_COMPRESSION_ENABLED (default: true)
        - CHECKPOINT_S3_BUCKET_NAME (optional, enables S3 offloading)

        Returns:
            Configured DynamoDBSaver instance
        """
        checkpoint_table = os.getenv("CHECKPOINT_DYNAMODB_TABLE_NAME")
        if not checkpoint_table:
            raise ValueError("CHECKPOINT_DYNAMODB_TABLE_NAME environment variable is required")

        checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
        checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
        checkpoint_s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")

        # Create S3 config if bucket is specified
        s3_config = None
        if checkpoint_s3_bucket:
            s3_config = {"bucket_name": checkpoint_s3_bucket}
            logger.info(f"S3 offloading enabled for large checkpoints: {checkpoint_s3_bucket}")

        checkpointer = DynamoDBSaver(
            table_name=checkpoint_table,
            region_name=checkpoint_region,
            ttl_seconds=checkpoint_ttl_days * 24 * 60 * 60,
            enable_checkpoint_compression=checkpoint_compression,
            s3_offload_config=s3_config,  # type: ignore[arg-type]
        )

        logger.info(f"Initialized DynamoDB checkpointer: {checkpoint_table}")
        return checkpointer

    def _create_bedrock_model(self, bedrock_client: boto3.client) -> ChatBedrockConverse:
        """Create ChatBedrockConverse model.

        NOTE: Callbacks are NOT set here - they're provided via RunnableConfig at runtime.
        This ensures only the runtime callbacks (with correct sub_agent_id) are used.

        Args:
            bedrock_client: Configured boto3 bedrock-runtime client

        Returns:
            ChatBedrockConverse model instance
        """
        model = ChatBedrockConverse(
            client=bedrock_client,
            region_name=self.bedrock_region,
            model=self.bedrock_model_id,
            temperature=0,
        )

        logger.info(f"Initialized Bedrock model (callbacks will be set at runtime): {self.bedrock_model_id}")
        return model

    def _init_cost_tracking(self):
        """Initialize cost tracking with configurable parameters.

        Reads configuration from:
        - PLAYGROUND_BACKEND_URL (required for cost tracking)
        - COST_TRACKING_BATCH_SIZE (default: 10)
        - COST_TRACKING_FLUSH_INTERVAL (default: 5.0)

        Cost tracking is optional - if PLAYGROUND_BACKEND_URL is not set,
        cost tracking will be skipped with a warning.
        """
        backend_url = os.getenv("PLAYGROUND_BACKEND_URL")
        if not backend_url:
            logger.info("PLAYGROUND_BACKEND_URL not set, skipping cost tracking initialization")
            return

        try:
            batch_size = int(os.getenv("COST_TRACKING_BATCH_SIZE", "10"))
            flush_interval = float(os.getenv("COST_TRACKING_FLUSH_INTERVAL", "5.0"))

            self.enable_cost_tracking(
                backend_url=backend_url,
                batch_size=batch_size,
                flush_interval=flush_interval,
            )
            logger.info(
                f"Cost tracking enabled (backend={backend_url}, batch_size={batch_size}, flush_interval={flush_interval}s)"
            )
        except Exception as e:
            logger.warning(f"Failed to enable cost tracking: {e}")

    async def _ensure_mcp_tools_loaded(self):
        """Ensure MCP tools are discovered and loaded.

        This is called lazily on first request to avoid blocking __init__.
        Uses a lock to prevent concurrent discovery attempts.
        """
        if self._mcp_tools is not None and self._graph is not None:
            return

        if self._mcp_tools_lock:
            # Wait for concurrent discovery to complete
            for _ in range(10):
                await asyncio.sleep(0.1)
                if self._mcp_tools is not None and self._graph is not None:
                    return
            logger.warning("Timeout waiting for MCP tools discovery")
            return

        self._mcp_tools_lock = True
        try:
            logger.info("Discovering MCP tools...")

            # Get MCP connections from subclass (async call with credential context available)
            connections = await self._get_mcp_connections()

            # Create MCP client
            self._mcp_client = MultiServerMCPClient(connections=connections)  # type: ignore

            # Get tool interceptors for credential injection
            interceptors = self._get_tool_interceptors()

            # Load tools from all connections
            all_tools = []
            for server_name, connection in connections.items():
                logger.info(f"Loading tools from MCP server: {server_name}")
                tools = await load_mcp_tools(
                    session=None,
                    connection=connection,
                    tool_interceptors=interceptors,
                    server_name=server_name,
                )
                tools = self._filter_tools(tools)
                all_tools.extend(tools)
                logger.info(f"Loaded {len(tools)} tools from {server_name}")

            self._mcp_tools = all_tools
            logger.info(f"Discovered total of {len(self._mcp_tools)} MCP tools")

            # Create graph with discovered tools
            self._graph = self._create_graph(self._mcp_tools)
            logger.info("Graph created with MCP tools")

        except Exception as e:
            logger.error(f"Failed to discover MCP tools: {e}", exc_info=True)
            raise
        finally:
            self._mcp_tools_lock = False

    def _create_graph(self, tools: list[BaseTool]) -> CompiledStateGraph:
        """Create LangGraph DeepAgent with tools and configuration.

        Default implementation using create_deep_agent with:
        - Model from _create_bedrock_model()
        - System prompt from _get_system_prompt()
        - Middleware from _get_middleware()
        - FinalResponseSchema as response format
        - DynamoDB checkpointer

        Subclasses can override this method for custom graph creation.

        Args:
            tools: List of discovered MCP tools

        Returns:
            Compiled LangGraph state graph
        """
        return create_deep_agent(
            model=self._model,
            tools=tools,
            subagents=[],
            system_prompt=self._get_system_prompt(),
            checkpointer=self._checkpointer,
            middleware=self._get_middleware(),
            response_format=AutoStrategy(schema=FinalResponseSchema),
        )

    async def close(self):
        """Cleanup resources."""
        await self.flush_cost_tracking()
        logger.info(f"{self.__class__.__name__} closed")

    async def _stream_impl(self, query: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Standard LangGraph streaming implementation with FinalResponseSchema extraction.

        The base stream() method has already handled:
        - Cost tracking setup (sub_agent_id)
        - Request credential injection (if available)

        This method:
        - Ensures MCP tools are loaded
        - Executes graph with checkpoint namespace isolation
        - Streams working state updates during execution
        - Extracts FinalResponseSchema for final task state
        - Handles interrupts and errors

        Args:
            query: The user's natural language query
            user_config: User configuration with access token
            task: The task context for the current interaction

        Yields:
            AgentStreamResponse objects with state updates and content
        """
        try:
            # Ensure MCP tools are loaded
            await self._ensure_mcp_tools_loaded()

            # Verify graph was created
            if self._graph is None:
                logger.error("Graph is None after _ensure_mcp_tools_loaded()")
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content="The agent failed to initialize properly. Please contact support or try again later.",
                    metadata={"error": "graph_initialization_failed"},
                )
                return

            logger.info(f"Processing query for user {user_config.user_sub}")

            # Execute graph with thread isolation
            # CRITICAL: Use UNIQUE thread_id to isolate sub-agent checkpoints from orchestrator.
            # All agents (orchestrator, dynamic sub-agents, remote A2A agents) share the SAME
            # DynamoDB table, so we MUST use different thread_id values to prevent checkpoint
            # pollution and "missing tool_result" errors.
            #
            # Format: {context_id}::{checkpoint_ns}
            # - Maintains relationship to conversation via context_id prefix
            # - Ensures complete isolation via unique partition key
            # - Consistent with dynamic sub-agent pattern
            #
            # For scheduled jobs, the A2A context_id is stored as conversation_id in the
            # scheduled_job_runs table for tracking and cost attribution.
            #
            # IMPORTANT: Must include __pregel_checkpointer in config to prevent LangGraph from
            # interpreting checkpoint_ns as a subgraph identifier (see LangGraph pregel /main.py:1244)
            checkpoint_ns = self._get_checkpoint_namespace()
            # Use natural A2A context_id with checkpoint namespace for thread isolation.
            effective_thread_id = f"{task.context_id}::{checkpoint_ns}"
            config = self.create_runnable_config(
                user_sub=user_config.user_sub,
                conversation_id=task.context_id,
                thread_id=effective_thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpointer=self._checkpointer,
                scheduled_job_id=user_config.scheduled_job_id,
            )

            # Convert query to messages format
            input_messages = [HumanMessage(content=query)]

            # Stream graph execution
            chunk_count = 0
            final_user_content = []
            final_response_message = None
            task_state = TaskState.completed

            async for event in self._graph.astream({"messages": input_messages}, config):
                chunk_count += 1
                logger.debug(f"Graph event #{chunk_count}: {event}")

                # LangGraph returns dict events with node names as keys
                if isinstance(event, dict):
                    # Extract messages from the event
                    for node_name, node_data in event.items():
                        if isinstance(node_data, dict) and "messages" in node_data:
                            messages = node_data["messages"]
                            if isinstance(messages, list):
                                for msg in messages:
                                    # Check for FinalResponseSchema tool call first
                                    if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                                        for tool_call in msg.tool_calls:
                                            if tool_call.get("name") == "FinalResponseSchema":
                                                args = tool_call.get("args", {})
                                                state_str = args.get("task_state", "completed")

                                                # Map string state to TaskState enum
                                                if state_str == "input_required":
                                                    task_state = TaskState.input_required
                                                elif state_str == "failed":
                                                    task_state = TaskState.failed
                                                elif state_str == "working":
                                                    task_state = TaskState.working
                                                else:
                                                    task_state = TaskState.completed

                                                # Extract the message field
                                                final_response_message = args.get("message")
                                                logger.info(
                                                    f"FinalResponseSchema captured during stream: state={task_state}, "
                                                    f"message_length={len(final_response_message) if final_response_message else 0}"
                                                )
                                                break

                                    # Also accumulate regular AI message content
                                    if isinstance(msg, AIMessage) and msg.content:
                                        content = str(msg.content)
                                        logger.debug(f"Content from {node_name}: {content[:100]}...")
                                        # Accumulate content for final response
                                        final_user_content.append(content)
                                        # Stream content as working state (progress updates)
                                        yield AgentStreamResponse(
                                            state=TaskState.working,
                                            content=content,
                                        )

            logger.debug(f"Stream processing complete. Total chunks: {chunk_count}")

            # Get final state
            final_state = self._graph.get_state(config)

            # Check for interrupts
            if final_state.interrupts:
                yield AgentStreamResponse(
                    state=TaskState.input_required,
                    content="Process interrupted. Additional input required.",
                )
                return

            # Extract final state from FinalResponseSchema (from final state if not captured during stream)
            if final_response_message is None and final_state.values and "messages" in final_state.values:
                messages = final_state.values["messages"]
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage):
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tool_call in msg.tool_calls:
                                if tool_call.get("name") == "FinalResponseSchema":
                                    args = tool_call.get("args", {})
                                    state_str = args.get("task_state", "completed")
                                    if state_str == "input_required":
                                        task_state = TaskState.input_required
                                    elif state_str == "failed":
                                        task_state = TaskState.failed
                                    elif state_str == "working":
                                        task_state = TaskState.working
                                    else:
                                        task_state = TaskState.completed
                                    logger.info(f"FinalResponseSchema found in final state: state={task_state}")
                                    break

                        if task_state != TaskState.completed or msg.tool_calls:
                            break

            # Send final completion
            final_content = (
                final_response_message
                if final_response_message
                else ("\n\n".join(final_user_content) if final_user_content else "Request processed successfully.")
            )

            logger.info(f"Sending final completion: task_state={task_state}, content_length={len(final_content)}")
            yield AgentStreamResponse(
                state=task_state,
                content=final_content,
            )
            logger.info("Final response sent successfully")

        except Exception as e:
            logger.error(f"Error in {self.__class__.__name__}.stream: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=f"An error occurred while processing your request: {str(e)}",
                metadata={"error": str(e)},
            )

    # Abstract methods that subclasses must implement

    @abstractmethod
    async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection configuration.

        Subclasses should return a dictionary mapping server names to
        StreamableHttpConnection instances with appropriate authentication
        headers and configuration.

        Example:
            {
                "my-server": StreamableHttpConnection(
                    transport="streamable_http",
                    url="https://my-mcp-server.example.com/mcp",
                    headers={"Authorization": f"Bearer {token}"}
                )
            }

        Returns:
            Dictionary of server name to StreamableHttpConnection
        """
        pass

    @abstractmethod
    def _get_system_prompt(self) -> str:
        """Return agent-specific system prompt.

        This prompt defines the agent's role, capabilities, and behavior.

        Returns:
            System prompt string
        """
        pass

    @abstractmethod
    def _get_checkpoint_namespace(self) -> str:
        """Return unique checkpoint namespace for isolation.

        This namespace isolates this agent's checkpoints from other agents
        sharing the same DynamoDB table.

        Returns:
            Unique namespace string (e.g., "agent-creator", "alloy-agent")
        """
        pass

    # Optional methods with default implementations

    def _get_bedrock_model_id(self) -> str:
        """Return Bedrock model ID.

        Default: Uses BEDROCK_MODEL_ID env var or Claude Sonnet 4.5

        Returns:
            Bedrock model ID string
        """
        return os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

    def _get_middleware(self) -> list[AgentMiddleware]:
        """Return agent middleware list.

        Default: Includes BedrockPromptCachingMiddleware for system prompt caching.

        Subclasses can override to add middleware for tool call interception,
        parameter enforcement, etc. Call super()._get_middleware() to preserve
        prompt caching.

        Returns:
            List of AgentMiddleware instances
        """
        return [BedrockPromptCachingMiddleware()]

    def _get_tool_interceptors(self) -> list:
        """Return tool interceptors for credential injection or request modification.

        Default: Empty list (no interceptors)

        Subclasses can override to provide interceptors that modify MCP tool calls,
        such as injecting authentication headers or user credentials.

        Returns:
            List of interceptor callables
        """
        return []
