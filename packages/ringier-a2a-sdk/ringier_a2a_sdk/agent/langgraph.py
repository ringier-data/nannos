"""Generic LangGraph base agent implementation.

This module provides a provider-agnostic base class for A2A agents that use
LangGraph with MCP tools. Unlike LangGraphBedrockAgent, this class does not
hardcode any specific LLM provider or checkpoint backend — subclasses provide
their own model and checkpointer via abstract factory methods.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from abc import abstractmethod
from collections.abc import AsyncIterable

from a2a.types import Message, Task, TaskState
from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import AutoStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.callbacks import Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from ringier_a2a_sdk.utils.mcp_errors import format_mcp_error, is_retryable_mcp_error
from ringier_a2a_sdk.utils.mcp_progress import on_mcp_progress

from ..agent.base import BaseAgent
from ..middleware.credential_injector import BaseCredentialInjector
from ..middleware.steering import SteeringMiddleware
from ..middleware.tool_schema_cleaning import ToolSchemaCleaningMiddleware
from ..models import TODO_STATE_MAP, AgentStreamResponse, TodoItem, UserConfig
from ..utils.a2a_part_conversion import a2a_parts_to_content
from ..utils.streaming import StreamBuffer, StructuredResponseStreamer, extract_text_from_content

logger = logging.getLogger(__name__)

# Maximum number of LangGraph steps before recursion limit is hit
_MAX_RECURSION_LIMIT = 50


def _get_default_recursion_limit() -> int:
    """Get recursion limit from environment or default."""
    return int(os.getenv("LANGGRAPH_RECURSION_LIMIT", str(_MAX_RECURSION_LIMIT)))


class FinalResponseSchema(BaseModel):
    """Schema for final response from LangGraph agents."""

    task_state: TaskState = Field(
        ...,
        description="The final state of the task: 'completed', 'failed', 'input_required', or 'working'",
    )
    message: str = Field(
        ...,
        description=(
            "A clear, helpful message to the user about the task outcome.\n"
            "This message is the ONLY output the orchestrator will see. "
            "The orchestrator has NO access to your internal state, tool results, or conversation history.\n"
            "\n"
            "YOU MUST include ALL important information, results, findings, and data in this message.\n"
            "FORMAT GUIDELINES:\n"
            "- Use markdown for structure (headers, lists, bold for key info)\n"
            "- Include specific identifiers, numbers, and names\n"
            "- Explain significance and implications, not just raw data\n"
            "- For large results: summarize key points but include all critical information\n"
            "\n"
            "Remember: The calling agent has NO other way to see your work. Make this complete!"
        ),
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

    Args:
        tool_query_regex: Optional regex pattern to filter MCP tools by name
        recursion_limit: Maximum number of LangGraph steps (default: 50, configurable via LANGGRAPH_RECURSION_LIMIT env var)
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self, tool_query_regex: str | None = None, recursion_limit: int | None = None):
        """Initialize the LangGraph Agent.

        Calls subclass factory methods to create the model and checkpointer,
        then sets up cost tracking and lazy MCP tool discovery.

        Args:
            tool_query_regex: Optional regex pattern to filter MCP tools by name
            recursion_limit: Maximum number of LangGraph steps (default: from LANGGRAPH_RECURSION_LIMIT env var or 50)
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
        self._mcp_load_error: str | None = None  # Store MCP connection error for user-friendly messaging
        logger.info("MCP tool discovery will happen on first request")

        # MCP tool refresh configuration (used by LangGraphAgent subclasses)
        self._mcp_refresh_enabled = os.getenv("MCP_TOOLS_REFRESH_ENABLED", "true").lower() == "true"
        try:
            self._mcp_refresh_interval_seconds = int(os.getenv("MCP_TOOLS_REFRESH_INTERVAL_SECONDS", "300"))
        except ValueError:
            self._mcp_refresh_interval_seconds = 300

        # MCP tool refresh state (initialized by LangGraphAgent)
        self._refresh_task: asyncio.Task | None = None
        self._refresh_stop_event: asyncio.Event | None = None
        # Track MCP server interface hash for smart refresh detection
        # (only refresh graph if tool interface actually changed)
        self._mcp_interface_hashes: dict[str, str] = {}  # server_name -> SHA256 hash of tools interface
        self._mcp_server_capabilities: dict[str, dict] = {}  # server_name -> capabilities tracking dict

        # Graph and MCP client
        self._graph: CompiledStateGraph | None = None
        self._mcp_client: MultiServerMCPClient | None = None
        self.tool_query_regex = re.compile(tool_query_regex) if tool_query_regex else None
        self.recursion_limit = recursion_limit if recursion_limit is not None else _get_default_recursion_limit()

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
        """Return agent middleware with schema cleaning and steering support.

        Includes:
        - ToolSchemaCleaningMiddleware: cleans MCP tool schemas before binding
        - SteeringMiddleware: injects pending user follow-up messages before each LLM call

        Provider-specific subclasses (Bedrock, Anthropic, Google) should call
        super()._get_middleware() and extend the list to preserve these.

        Returns:
            List of middleware: [ToolSchemaCleaningMiddleware, SteeringMiddleware]
        """

        return [
            ToolSchemaCleaningMiddleware(),
            SteeringMiddleware(get_pending_messages=self.get_pending_messages),
        ]

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

    async def _preprocess_input_messages(self, messages: list[HumanMessage]) -> list[HumanMessage]:
        """Hook for provider-specific message preprocessing before graph execution.

        Called after A2A parts are converted to LangChain HumanMessages but before
        the messages are fed to the LangGraph graph. Override in subclasses to
        transform content blocks (e.g., convert URL-based images to base64).

        Args:
            messages: List of HumanMessages with content blocks.

        Returns:
            Preprocessed list of HumanMessages (default: pass-through).
        """
        return messages

    def _filter_tools(self, tools: list[BaseTool]) -> list[BaseTool]:
        """Filter tools by query regex pattern."""
        if not self.tool_query_regex:
            return tools
        filtered_tools: list[BaseTool] = [tool for tool in tools if self.tool_query_regex.search(tool.name)]
        logger.info(
            f"Filtered tools with pattern '{self.tool_query_regex.pattern}': {[tool.name for tool in filtered_tools]}"
        )
        return filtered_tools

    def _init_cost_tracking(self):
        """Initialize cost tracking. Skipped if CONSOLE_BACKEND_URL is not set."""
        backend_url = os.getenv("CONSOLE_BACKEND_URL")
        if not backend_url:
            logger.info("CONSOLE_BACKEND_URL not set, skipping cost tracking initialization")
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

    async def _extract_mcp_server_info(self) -> None:
        """Track available MCP servers for refresh detection.

        Uses only the public API (.connections) to identify servers.
        Actual version/capability detection relies on interface hash comparison
        during refresh cycles, which is more reliable than post-hoc inspection.

        Should be called after MultiServerMCPClient is created.
        """
        if not self._mcp_client:
            return

        try:
            # Use only the public connections dict (no internal attributes)
            if hasattr(self._mcp_client, "connections"):
                connections: dict = self._mcp_client.connections  # type: ignore
                for server_name in connections.keys():
                    # Initialize server tracking with minimal info
                    # Actual detection happens via interface hash comparison in refresh cycles
                    self._mcp_server_capabilities[server_name] = {
                        "tracked": True,  # Indicates this server is being monitored
                    }
                    logger.debug(f"Tracking MCP server for refresh: {server_name}")
        except Exception as e:
            logger.debug(f"Error tracking MCP servers: {e}")

    @staticmethod
    def _compute_interface_hash(tools: list[BaseTool]) -> str:
        """Compute SHA256 hash of tool interface (names and schemas).

        This hash changes if tools are added/removed or their schemas change,
        but not if internal tool logic changes.

        Args:
            tools: List of BaseTool instances

        Returns:
            SHA256 hex digest of the tool interface
        """
        # Build a deterministic JSON representation of the tool interface
        interface_data: list[dict[str, str | dict]] = []
        for tool in sorted(tools, key=lambda t: t.name):
            # Extract name and schema
            tool_schema: dict[str, str | dict] = {
                "name": tool.name,
                "description": tool.description or "",
            }
            if hasattr(tool, "args_schema") and tool.args_schema:
                # Include args schema for change detection
                try:
                    # Get the JSON schema if available
                    if hasattr(tool.args_schema, "model_json_schema"):
                        tool_schema["schema"] = tool.args_schema.model_json_schema()
                    else:
                        tool_schema["schema"] = str(tool.args_schema)
                except Exception:
                    tool_schema["schema"] = str(tool.args_schema)
            interface_data.append(tool_schema)

        # Compute SHA256 hash of the JSON representation
        interface_json = json.dumps(interface_data, sort_keys=True)
        return hashlib.sha256(interface_json.encode()).hexdigest()

    async def _check_mcp_interface_changed(
        self, server_name: str, tools: list[BaseTool], server_info: dict | None = None
    ) -> bool:
        """Check if MCP server interface has changed since last discovery.

        Uses tool interface hash (names and schemas) for reliable change detection.
        This approach works with the public MultiServerMCPClient API and detects:
        - Tools added/removed
        - Tool schemas changed
        - Argument signatures modified

        Args:
            server_name: Name of the MCP server
            tools: List of tools discovered from the server
            server_info: Unused (kept for compatibility), hash comparison is reliable

        Returns:
            True if interface changed (refresh needed), False if unchanged
        """
        # Compute interface hash
        current_hash: str = self._compute_interface_hash(tools)
        previous_hash: str | None = self._mcp_interface_hashes.get(server_name)

        # Check if interface changed (first time or hash differs)
        if previous_hash is not None and current_hash != previous_hash:
            logger.info(
                f"MCP server '{server_name}' interface changed (hash: {previous_hash[:8]}... -> {current_hash[:8]}...)"
            )
            # Update stored hash
            self._mcp_interface_hashes[server_name] = current_hash
            return True

        # Store hash on first discovery
        if previous_hash is None:
            self._mcp_interface_hashes[server_name] = current_hash
            logger.debug(f"Stored initial interface hash for MCP server '{server_name}'")
            return False  # First time, not a "change"

        # No change detected
        logger.debug(f"MCP server '{server_name}' interface unchanged")
        return False

    async def _ensure_mcp_tools_loaded(self):
        """Ensure MCP tools are discovered and loaded (lazy, on first request).

        Gracefully handles MCP connection failures by setting self._graph = None
        and storing the error message. This allows _stream_impl to provide a
        helpful error response instead of raising an exception that stalls the request.

        Implements retry logic with exponential backoff for transient errors (502, 503, 504).
        """
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

            connections: dict[str, StreamableHttpConnection] = await self._get_mcp_connections()
            callbacks: Callbacks = Callbacks(on_progress=on_mcp_progress)
            self._mcp_client = MultiServerMCPClient(connections=connections, callbacks=callbacks)  # type: ignore

            # Track MCP servers for refresh detection
            await self._extract_mcp_server_info()

            interceptors: list = self._get_tool_interceptors()

            all_tools: list[BaseTool] = []
            for server_name, connection in connections.items():
                logger.info(f"Loading tools from MCP server: {server_name}")

                # Retry transient failures with exponential backoff
                tools: list[BaseTool] = await self._load_mcp_tools_with_retry(
                    connection=connection,
                    interceptors=interceptors,
                    server_name=server_name,
                    callbacks=callbacks,
                )

                tools = self._filter_tools(tools)
                all_tools.extend(tools)
                logger.info(f"Loaded {len(tools)} tools from {server_name}")

                # Store interface hash for this server for future change detection
                await self._check_mcp_interface_changed(server_name, tools)

            self._mcp_tools = all_tools
            logger.info(f"Discovered total of {len(self._mcp_tools)} MCP tools")

            self._graph = self._create_graph(self._mcp_tools)
            logger.info("Graph created with MCP tools")

        except Exception as e:
            # Gracefully handle MCP connection failures instead of re-raising.
            # This prevents the request from stalling and allows _stream_impl
            # to provide a user-friendly error message.
            error_message = format_mcp_error(e)
            logger.error(f"Failed to discover MCP tools: {error_message}", exc_info=True)

            # Store error message for _stream_impl to use
            self._mcp_load_error = error_message

            # Leave self._graph and self._mcp_tools as None to signal failure
            # _stream_impl will check for None and yield an error response
        finally:
            self._mcp_tools_lock = False

    async def _load_mcp_tools_with_retry(
        self,
        connection: StreamableHttpConnection,
        interceptors: list,
        server_name: str,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        callbacks: Callbacks | None = None,
    ) -> list[BaseTool]:
        """Load MCP tools with exponential backoff retry for transient errors.

        Retries on HTTP 502, 503, 504 errors with exponential backoff.
        Non-retryable errors (4xx, connection refused, etc.) fail immediately.

        Args:
            connection: MCP server connection
            interceptors: Tool interceptors for credential injection
            server_name: Server name for logging
            max_retries: Maximum number of retry attempts (default: 3)
            initial_delay: Initial delay between retries in seconds (default: 1.0)
            callbacks: Optional MCP callbacks (e.g. progress) to pass through to tool loading

        Returns:
            List of loaded MCP tools

        Raises:
            Exception: If all retries are exhausted or a non-retryable error occurs
        """

        last_error = None
        delay = initial_delay

        for attempt in range(max_retries):
            try:
                tools = await load_mcp_tools(
                    session=None,
                    connection=connection,
                    tool_interceptors=interceptors,
                    server_name=server_name,
                    callbacks=callbacks,
                )
                if attempt > 0:
                    logger.info(f"Successfully loaded MCP tools from {server_name} on attempt {attempt + 1}")
                return tools

            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                is_retryable = is_retryable_mcp_error(e)

                if not is_retryable or attempt >= max_retries - 1:
                    # Non-retryable error or exhausted retries
                    if is_retryable:
                        logger.error(f"Failed to load MCP tools from {server_name} after {attempt + 1} attempts: {e}")
                    else:
                        logger.error(f"Non-retryable error loading MCP tools from {server_name}: {e}")
                    raise

                # Retryable error - wait and retry
                logger.warning(
                    f"Transient error loading MCP tools from {server_name} (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff

        # Should never reach here, but just in case
        raise last_error or Exception("Failed to load MCP tools")

    @staticmethod
    def _create_response_tool() -> BaseTool:
        """Create FinalResponseSchema as an explicit tool with return_direct=True.

        Used instead of AutoStrategy when the model outputs structured JSON in content
        text rather than via tool_call_chunks (e.g. Gemini, or Bedrock with thinking).
        The explicit tool ensures proper tool_call_chunks streaming.

        Matches the orchestrator's approach in graph_factory.py.
        """
        return StructuredTool.from_function(
            func=lambda **kwargs: FinalResponseSchema(**kwargs),
            name="FinalResponseSchema",
            description="ALWAYS use this tool to format your final response to the user.",
            args_schema=FinalResponseSchema,
            return_direct=True,
        )

    def _create_graph(self, tools: list[BaseTool]) -> CompiledStateGraph:
        """Create LangGraph DeepAgent with tools and configuration.

        Default implementation using create_deep_agent with AutoStrategy.
        Subclasses can override to change response_format (e.g. Gemini uses
        an explicit tool instead of AutoStrategy).

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

    # --- MCP Tool Refresh (periodic re-discovery) ---

    async def _start_mcp_refresh_worker(self):
        """Background worker that periodically re-discovers MCP tools.

        Runs in an independent async task, refreshing tools at a configurable interval
        (MCP_TOOLS_REFRESH_INTERVAL_SECONDS, default 300 seconds / 5 minutes). Uses the
        existing _ensure_mcp_tools_loaded() to discover tools and acquires _mcp_tools_lock
        to prevent collision with active tool-calls.

        Gracefully handles errors and continues refreshing even if a cycle fails.
        Respects _refresh_stop_event for graceful shutdown.

        This worker is completely independent from stream processing — it runs in the
        background as long as the agent instance is active.
        """
        logger.info(
            f"MCP tool refresh worker started (interval: {self._mcp_refresh_interval_seconds}s, "
            f"enabled: {self._mcp_refresh_enabled})"
        )

        if not self._mcp_refresh_enabled:
            logger.info("MCP tool refresh is disabled, skipping worker")
            return

        try:
            while not self._refresh_stop_event.is_set():
                try:
                    # Wait for the interval or until stop signal
                    await asyncio.wait_for(
                        self._refresh_stop_event.wait(),
                        timeout=self._mcp_refresh_interval_seconds,
                    )
                    # If we get here, the stop event was set
                    break
                except asyncio.TimeoutError:
                    # Timeout is expected — time to refresh tools
                    pass

                try:
                    logger.debug("MCP tool refresh cycle started")

                    # Smart refresh: only recreate graph if interface actually changed
                    # This avoids unnecessary graph rebuilds when no schemas changed
                    if self._mcp_tools is None:
                        # Tools not yet loaded, do normal load
                        await self._ensure_mcp_tools_loaded()
                    else:
                        # Tools already loaded, check if interface changed
                        try:
                            interface_changed: bool = False
                            connections: dict[str, StreamableHttpConnection] = await self._get_mcp_connections()
                            callbacks: Callbacks = Callbacks(on_progress=on_mcp_progress)

                            for server_name, connection in connections.items():
                                logger.debug(f"Checking interface changes for MCP server: {server_name}")

                                # Discover current tools
                                tools: list[BaseTool] = await self._load_mcp_tools_with_retry(
                                    connection=connection,
                                    interceptors=self._get_tool_interceptors(),
                                    server_name=server_name,
                                    callbacks=callbacks,
                                )
                                tools = self._filter_tools(tools)

                                # Check if interface changed (hash-based detection)
                                if await self._check_mcp_interface_changed(
                                    server_name=server_name,
                                    tools=tools,
                                ):
                                    logger.info(f"Interface changes detected for {server_name}, triggering refresh")
                                    interface_changed = True
                                    break

                            if interface_changed:
                                # Interface changed, reset and reload all tools
                                previous_tool_count = len(self._mcp_tools)
                                self._mcp_tools = None
                                self._graph = None
                                logger.debug(f"Reset MCP tools ({previous_tool_count} tools) due to interface changes")

                                await self._ensure_mcp_tools_loaded()
                                if self._mcp_tools is not None:
                                    logger.info(
                                        f"MCP tools refreshed after interface change: {len(self._mcp_tools)} tools loaded"
                                    )
                            else:
                                logger.debug("No interface changes detected, skipping graph rebuild")

                        except Exception as e:
                            logger.error(f"Error checking MCP interface changes: {e}", exc_info=False)
                            # Fall back to full reload on error
                            logger.info("Falling back to full MCP tools reload")
                            self._mcp_tools = None
                            self._graph = None
                            await self._ensure_mcp_tools_loaded()

                except Exception as e:
                    logger.error(f"Error during MCP tool refresh cycle: {e}", exc_info=False)
                    # Continue with next refresh cycle even if this one failed

        except asyncio.CancelledError:
            logger.info("MCP tool refresh worker cancelled")
            raise
        finally:
            logger.info("MCP tool refresh worker stopped")

    async def _start_mcp_refresh(self) -> None:
        """Start the MCP tool refresh worker (idempotent).

        Creates an independent background task that periodically re-discovers MCP tools.
        Safe to call multiple times — only starts the worker once per instance.

        Should be called from FastAPI lifespan startup event:
            await agent.startup()  # Calls this method internally
        """
        if self._refresh_task is not None:
            logger.debug("MCP tool refresh worker already started")
            return

        if not self._mcp_refresh_enabled:
            logger.debug("MCP tool refresh is disabled, skipping startup")
            return

        logger.info("Starting MCP tool refresh worker")
        self._refresh_stop_event = asyncio.Event()
        self._refresh_task = asyncio.create_task(self._start_mcp_refresh_worker())

    async def _stop_mcp_refresh(self) -> None:
        """Stop the MCP tool refresh worker gracefully.

        Cancels the background refresh task and waits for cleanup with timeout.

        Should be called from FastAPI lifespan shutdown event:
            await agent.shutdown()  # Calls this method internally
        """
        if self._refresh_task is None:
            logger.debug("MCP tool refresh worker not running")
            return

        logger.info("Stopping MCP tool refresh worker")

        # Signal the worker to stop
        if self._refresh_stop_event:
            self._refresh_stop_event.set()

        # Cancel the task if it hasn't finished
        if not self._refresh_task.done():
            self._refresh_task.cancel()

        # Wait for task cleanup with timeout
        try:
            await asyncio.wait_for(asyncio.shield(self._refresh_task), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for MCP tool refresh worker to stop")
        except (asyncio.CancelledError, Exception):
            # Expected when task is cancelled or completes
            pass

        self._refresh_task = None
        self._refresh_stop_event = None
        logger.info("MCP tool refresh worker stopped")

    async def close(self):
        """Cleanup resources."""
        await self.flush_cost_tracking()
        logger.info(f"{self.__class__.__name__} closed")

    async def startup(self) -> None:
        """Async startup hook for lifecycle initialization.

        Starts the MCP tool refresh worker. Called from FastAPI lifespan startup event.
        Safe to call multiple times (idempotent).

        Subclasses that override this method should call super().startup() to ensure
        refresh worker is started:

            async def startup(self):
                await super().startup()
                # subclass-specific startup logic
        """
        logger.info(f"Starting up {self.__class__.__name__}")
        await self._start_mcp_refresh()
        logger.info(f"{self.__class__.__name__} startup complete")

    async def shutdown(self) -> None:
        """Async shutdown hook for graceful cleanup.

        Stops the MCP tool refresh worker and closes related resources. Called from
        FastAPI lifespan shutdown event.

        Subclasses that override this method should call super().shutdown() to ensure
        refresh worker is stopped:

            async def shutdown(self):
                await super().shutdown()
                # subclass-specific shutdown logic
        """
        logger.info(f"Shutting down {self.__class__.__name__}")
        await self._stop_mcp_refresh()
        await self.close()
        logger.info(f"{self.__class__.__name__} shutdown complete")

    async def _stream_impl(
        self, messages: list[Message], user_config: UserConfig, task: Task
    ) -> AsyncIterable[AgentStreamResponse]:
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
            messages: List of A2A Messages from the user (each may contain text, files, data)
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
                # Use the stored error message if available, otherwise fall back to generic message
                error_detail = getattr(self, "_mcp_load_error", None) or "Unknown initialization error"
                logger.error(f"Graph is None after _ensure_mcp_tools_loaded(): {error_detail}")

                # Provide user-friendly error message
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content=(
                        f"I'm unable to connect to my tooling services at the moment. {error_detail}\n\n"
                        "This is likely a temporary issue. Please try again in a few moments. "
                        "If the problem persists, please contact support."
                    ),
                    metadata={"error": "mcp_connection_failed", "error_detail": error_detail},
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
            checkpoint_ns: str = self._get_checkpoint_namespace()
            # Use natural A2A context_id with checkpoint namespace for thread isolation.
            effective_thread_id: str = f"{task.context_id}::{checkpoint_ns}"
            config = self.create_runnable_config(
                user_sub=user_config.user_sub,
                conversation_id=task.context_id,
                thread_id=effective_thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpointer=self._checkpointer,
                scheduled_job_id=user_config.scheduled_job_id,
            )

            # Convert A2A messages to LangChain HumanMessages
            input_messages: list[HumanMessage] = [
                HumanMessage(content_blocks=a2a_parts_to_content(msg.parts or [])) for msg in messages
            ]

            # Allow provider-specific preprocessing (e.g., Bedrock URL→base64 image conversion)
            input_messages = await self._preprocess_input_messages(input_messages)

            # Stream graph execution
            chunk_count = 0
            final_user_content = []
            final_response_message = None
            task_state = TaskState.completed
            stream_buffer = StreamBuffer()
            response_streamer = StructuredResponseStreamer("FinalResponseSchema")
            # Track per-message JSON detection to suppress FinalResponseSchema text output
            _current_msg_id = None
            _current_msg_is_json = False

            async for part in self._graph.with_config({"recursion_limit": self.recursion_limit}).astream(
                {"messages": input_messages}, config, stream_mode=["updates", "messages"], version="v2"
            ):
                chunk_count += 1
                part_type = part["type"]

                if part_type == "messages":
                    # Token-level streaming from LLM
                    msg_chunk, _meta = part["data"]
                    if not isinstance(msg_chunk, AIMessageChunk):
                        continue

                    # Stream FinalResponseSchema message field incrementally
                    if msg_chunk.tool_call_chunks:
                        for tc_chunk in msg_chunk.tool_call_chunks:
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

                    # Regular text content from LLM
                    # Thinking blocks are streamed as intermediate output. Text content
                    # is usually regular conversation, but as a safety net we suppress
                    # text that looks like FinalResponseSchema JSON (starts with '{').
                    # With the explicit tool approach (Gemini, Bedrock+thinking) the model
                    # should produce tool_call_chunks, but we guard against edge cases.
                    if msg_chunk.content:
                        text, thinking_blocks = extract_text_from_content(msg_chunk.content)

                        # Stream thinking blocks as intermediate output
                        if thinking_blocks:
                            for block in thinking_blocks:
                                thinking_text = block.get("thinking", "")
                                if thinking_text:
                                    stream_buffer.append(thinking_text)
                                    for chunk in stream_buffer.flush_ready():
                                        yield AgentStreamResponse(
                                            state=TaskState.working,
                                            content=chunk,
                                            metadata={
                                                "streaming_chunk": True,
                                                "intermediate_output": True,
                                            },
                                        )

                        if text:
                            # Per-message JSON detection: if the first text token of a new
                            # message starts with '{', it's likely FinalResponseSchema JSON.
                            # Suppress streaming it — the final response will be extracted
                            # from structured_response or parsed from text at completion.
                            if msg_chunk.id != _current_msg_id:
                                _current_msg_id = msg_chunk.id
                                _current_msg_is_json = text.lstrip().startswith("{")

                            if not _current_msg_is_json:
                                stream_buffer.append(text)
                                for chunk in stream_buffer.flush_ready():
                                    yield AgentStreamResponse(
                                        state=TaskState.working,
                                        content=chunk,
                                        metadata={"streaming_chunk": True},
                                    )
                    continue

                if part_type != "updates":
                    continue

                # v2 updates data: {node_name: state_update_dict}
                event = part["data"]
                if not isinstance(event, dict):
                    continue

                # Extract todos and messages from the event
                for node_name, node_data in event.items():
                    # Detect todo list updates from write_todos tool (TodoListMiddleware)
                    if isinstance(node_data, dict) and "todos" in node_data:
                        raw_todos = node_data["todos"]
                        if isinstance(raw_todos, list) and raw_todos:
                            snapshot = [
                                TodoItem(
                                    name=t.get("content", "Task")
                                    if isinstance(t, dict)
                                    else getattr(t, "content", "Task"),
                                    state=TODO_STATE_MAP.get(
                                        t.get("status", "pending")
                                        if isinstance(t, dict)
                                        else getattr(t, "status", "pending"),
                                        "submitted",
                                    ),  # type: ignore[arg-type]
                                )
                                for t in raw_todos
                            ]
                            logger.debug(f"Todo snapshot from {node_name}: {len(snapshot)} items")
                            yield AgentStreamResponse(
                                state=TaskState.working,
                                content="",
                                metadata={"work_plan": True, "todos": snapshot},
                            )

                    if isinstance(node_data, dict) and "messages" in node_data:
                        messages = node_data["messages"]
                        if isinstance(messages, list):
                            for msg in messages:
                                # Check for FinalResponseSchema tool call (complete, from updates)
                                has_response_schema = False
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
                                            has_response_schema = True
                                            logger.info(
                                                f"FinalResponseSchema captured during stream: state={task_state}, "
                                                f"message_length={len(final_response_message) if final_response_message else 0}"
                                            )
                                            break

                                # Only accumulate regular AI message content when
                                # the message does NOT carry a FinalResponseSchema.
                                # When a schema is present, Bedrock may embed raw
                                # JSON in .content which must not be streamed to
                                # the user as a "working" update.
                                if not has_response_schema and isinstance(msg, AIMessage) and msg.content:
                                    # Use extract_text_from_content to handle structured content
                                    # (e.g., Gemini thinking blocks) instead of raw str() which
                                    # would serialize the list of dicts as a JSON-like string.
                                    text, _ = extract_text_from_content(msg.content)
                                    if text:
                                        logger.debug(f"Content from {node_name}: {text[:100]}...")
                                        # Accumulate content for final response fallback;
                                        # real-time streaming is handled by the "messages" stream mode above.
                                        final_user_content.append(text)

            # Flush any remaining buffered streaming content
            remaining = stream_buffer.flush_all()
            if remaining:
                yield AgentStreamResponse(
                    state=TaskState.working,
                    content=remaining,
                    metadata={"streaming_chunk": True},
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
            # Priority 1: Check structured_response in state (populated by deepagents AutoStrategy)
            if final_response_message is None and final_state.values:
                structured_response = final_state.values.get("structured_response")
                if structured_response is not None:
                    # structured_response is a FinalResponseSchema (Pydantic model) or dict
                    if hasattr(structured_response, "message"):
                        final_response_message = structured_response.message
                        state_str = getattr(structured_response, "task_state", "completed")
                    elif isinstance(structured_response, dict):
                        final_response_message = structured_response.get("message")
                        state_str = structured_response.get("task_state", "completed")
                    else:
                        state_str = "completed"

                    if final_response_message:
                        if state_str == "input_required":
                            task_state = TaskState.input_required
                        elif state_str == "failed":
                            task_state = TaskState.failed
                        elif state_str == "working":
                            task_state = TaskState.working
                        else:
                            task_state = TaskState.completed
                        logger.info(
                            f"FinalResponseSchema from structured_response: state={task_state}, "
                            f"message_length={len(final_response_message)}"
                        )

            # Priority 2: Check tool_calls in messages (Bedrock tool call format)
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
                                    final_response_message = args.get("message")
                                    logger.info(f"FinalResponseSchema found in final state: state={task_state}")
                                    break

                        if task_state != TaskState.completed or msg.tool_calls:
                            break

            # Priority 3: Try parsing accumulated text as FinalResponseSchema JSON (safety net)
            # With the explicit tool approach this should rarely trigger, but handles edge
            # cases where a model outputs FinalResponseSchema as text despite having the tool.
            if final_response_message is None and final_user_content:
                combined_text = "\n\n".join(final_user_content).strip()
                try:
                    parsed = json.loads(combined_text)
                    if isinstance(parsed, dict) and "message" in parsed:
                        final_response_message = parsed["message"]
                        state_str = parsed.get("task_state", "completed")
                        if state_str == "input_required":
                            task_state = TaskState.input_required
                        elif state_str == "failed":
                            task_state = TaskState.failed
                        elif state_str == "working":
                            task_state = TaskState.working
                        else:
                            task_state = TaskState.completed
                        logger.info(
                            f"FinalResponseSchema parsed from text content: state={task_state}, "
                            f"message_length={len(final_response_message)}"
                        )
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass

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

        except GraphRecursionError as e:
            # Handle recursion limit gracefully with an informative message
            logger.error(f"Recursion limit reached during stream processing: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content="I've been working on this task for a while and need to take a break. "
                "I've made some progress, but the task requires more steps than I can complete in one go. "
                "Would you like me to continue from where I left off, or would you prefer to break this down into smaller tasks?",
            )
