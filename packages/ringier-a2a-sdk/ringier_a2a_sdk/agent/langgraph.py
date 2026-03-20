"""Generic LangGraph base agent implementation.

This module provides a provider-agnostic base class for A2A agents that use
LangGraph with MCP tools. Unlike LangGraphBedrockAgent, this class does not
hardcode any specific LLM provider or checkpoint backend — subclasses provide
their own model and checkpointer via abstract factory methods.
"""

import asyncio
import logging
import os
import re
from abc import abstractmethod
from collections.abc import AsyncIterable

from a2a.types import Task, TaskState
from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import AutoStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from ..agent.base import BaseAgent
from ..middleware.credential_injector import BaseCredentialInjector
from ..models import AgentStreamResponse, UserConfig

logger = logging.getLogger(__name__)


class FinalResponseSchema(BaseModel):
    """Schema for final response from LangGraph agents."""

    task_state: str = Field(
        ...,
        description="The final state of the task: 'completed', 'failed', 'input_required', or 'working'",
    )
    message: str = Field(
        ...,
        description="A clear, helpful message to the user about the task outcome",
    )


class LangGraphAgent(BaseAgent):
    """Provider-agnostic base class for LangGraph agents with MCP tools.

    This base class provides common functionality for agents that:
    - Use any LLM provider (Bedrock, Azure OpenAI, local, etc.)
    - Discover and use MCP tools from a server
    - Use any LangGraph-compatible checkpointer
    - Implement LangGraph-based agent workflows
    - Stream responses with structured final state

    Subclasses must implement:
    - _create_model(): Create the LLM instance
    - _create_checkpointer(): Create the checkpoint saver
    - _get_mcp_connections(): Return MCP server connection configuration
    - _get_system_prompt(): Return agent-specific system prompt
    - _get_checkpoint_namespace(): Return unique checkpoint namespace

    Optional overrides:
    - _get_middleware(): Return agent middleware list (default: [])
    - _get_tool_interceptors(): Return tool interceptors (default: [])
    - _create_graph(): Create LangGraph with tools (has default implementation)
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self, tool_query_regex: str | None = None):
        """Initialize the LangGraph Agent.

        Calls subclass factory methods to create the model and checkpointer,
        then sets up cost tracking and lazy MCP tool discovery.
        """
        super().__init__()

        # Create checkpointer via subclass
        self._checkpointer = self._create_checkpointer()

        # Create model via subclass
        self._model = self._create_model()

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

    # --- Abstract factory methods (subclasses must implement) ---

    @abstractmethod
    def _create_model(self) -> BaseChatModel:
        """Create and return the LLM instance.

        Subclasses implement this to provide any LangChain-compatible chat model
        (ChatBedrockConverse, AzureChatOpenAI, ChatOpenAI, ChatGoogleGenerativeAI, etc.).

        Returns:
            A BaseChatModel instance
        """
        pass

    @abstractmethod
    def _create_checkpointer(self) -> BaseCheckpointSaver:
        """Create and return the checkpoint saver.

        Subclasses implement this to provide any LangGraph-compatible checkpointer
        (DynamoDBSaver, MemorySaver, PostgresSaver, etc.).

        Returns:
            A BaseCheckpointSaver instance
        """
        pass

    @abstractmethod
    async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection configuration.

        Returns:
            Dictionary of server name to StreamableHttpConnection
        """
        pass

    @abstractmethod
    def _get_system_prompt(self) -> str:
        """Return agent-specific system prompt.

        Returns:
            System prompt string
        """
        pass

    @abstractmethod
    def _get_checkpoint_namespace(self) -> str:
        """Return unique checkpoint namespace for isolation.

        Returns:
            Unique namespace string (e.g., "agent-creator", "alloy-agent")
        """
        pass

    # --- Optional methods with defaults ---

    def _get_middleware(self) -> list[AgentMiddleware]:
        """Return agent middleware list. Default: empty."""
        return []

    def _get_tool_interceptors(self) -> list:
        """Return tool interceptors for credential injection. Default: empty."""
        return []

    # --- Shared infrastructure (MCP tools, graph, streaming, cost tracking) ---

    async def get_headers(self) -> dict[str, str]:
        """Apply credential headers from injector for MCP initial handshake.

        Finds the first BaseCredentialInjector in _get_tool_interceptors() and
        returns its authorization headers.

        Returns:
            Dictionary with "Authorization" header, or empty dict if no injector found
        """
        interceptors = self._get_tool_interceptors()
        for interceptor in interceptors:
            if isinstance(interceptor, BaseCredentialInjector):
                logger.info(f"Found credential injector for MCP handshake: {interceptor.__class__.__name__}")
                return await interceptor.get_headers()

        logger.debug("No credential injector found in _get_tool_interceptors(), skipping header injection")
        return {}

    def _filter_tools(self, tools: list[BaseTool]) -> list[BaseTool]:
        """Filter tools by query regex pattern."""
        if not self.tool_query_regex:
            return tools
        filtered_tools = [tool for tool in tools if self.tool_query_regex.search(tool.name)]
        logger.info(
            f"Filtered tools with pattern '{self.tool_query_regex.pattern}': {[tool.name for tool in filtered_tools]}"
        )
        return filtered_tools

    def _init_cost_tracking(self):
        """Initialize cost tracking. Skipped if PLAYGROUND_BACKEND_URL is not set."""
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
        """Ensure MCP tools are discovered and loaded (lazy, on first request)."""
        if self._mcp_tools is not None and self._graph is not None:
            return

        if self._mcp_tools_lock:
            for _ in range(10):
                await asyncio.sleep(0.1)
                if self._mcp_tools is not None and self._graph is not None:
                    return
            logger.warning("Timeout waiting for MCP tools discovery")
            return

        self._mcp_tools_lock = True
        try:
            logger.info("Discovering MCP tools...")

            connections = await self._get_mcp_connections()
            self._mcp_client = MultiServerMCPClient(connections=connections)  # type: ignore
            interceptors = self._get_tool_interceptors()

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

            self._graph = self._create_graph(self._mcp_tools)
            logger.info("Graph created with MCP tools")

        except Exception as e:
            logger.error(f"Failed to discover MCP tools: {e}", exc_info=True)
            raise
        finally:
            self._mcp_tools_lock = False

    def _create_graph(self, tools: list[BaseTool]) -> CompiledStateGraph:
        """Create LangGraph DeepAgent with tools and configuration.

        Default implementation using create_deep_agent. Subclasses can override.

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
        """Standard LangGraph streaming with FinalResponseSchema extraction.

        Ensures MCP tools are loaded, executes graph with checkpoint namespace
        isolation, streams working state updates, and extracts structured final state.
        """
        try:
            await self._ensure_mcp_tools_loaded()

            if self._graph is None:
                logger.error("Graph is None after _ensure_mcp_tools_loaded()")
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content="The agent failed to initialize properly. Please contact support or try again later.",
                    metadata={"error": "graph_initialization_failed"},
                )
                return

            logger.info(f"Processing query for user {user_config.user_sub}")

            checkpoint_ns = self._get_checkpoint_namespace()
            effective_thread_id = f"{task.context_id}::{checkpoint_ns}"
            config = self.create_runnable_config(
                user_sub=user_config.user_sub,
                conversation_id=task.context_id,
                thread_id=effective_thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpointer=self._checkpointer,
                scheduled_job_id=user_config.scheduled_job_id,
            )

            input_messages = [HumanMessage(content=query)]

            chunk_count = 0
            final_user_content = []
            final_response_message = None
            task_state = TaskState.completed

            async for event in self._graph.astream({"messages": input_messages}, config):
                chunk_count += 1
                logger.debug(f"Graph event #{chunk_count}: {event}")

                if isinstance(event, dict):
                    for node_name, node_data in event.items():
                        if isinstance(node_data, dict) and "messages" in node_data:
                            messages = node_data["messages"]
                            if isinstance(messages, list):
                                for msg in messages:
                                    if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
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

                                                final_response_message = args.get("message")
                                                logger.info(
                                                    f"FinalResponseSchema captured during stream: state={task_state}, "
                                                    f"message_length={len(final_response_message) if final_response_message else 0}"
                                                )
                                                break

                                    if isinstance(msg, AIMessage) and msg.content:
                                        content = str(msg.content)
                                        logger.debug(f"Content from {node_name}: {content[:100]}...")
                                        final_user_content.append(content)
                                        yield AgentStreamResponse(
                                            state=TaskState.working,
                                            content=content,
                                        )

            logger.debug(f"Stream processing complete. Total chunks: {chunk_count}")

            final_state = self._graph.get_state(config)

            if final_state.interrupts:
                yield AgentStreamResponse(
                    state=TaskState.input_required,
                    content="Process interrupted. Additional input required.",
                )
                return

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
