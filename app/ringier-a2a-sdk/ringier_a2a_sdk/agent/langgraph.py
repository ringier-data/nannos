"""Generic LangGraph base agent implementation.

This module provides a provider-agnostic base class for A2A agents that use
LangGraph with MCP tools. Unlike LangGraphBedrockAgent, this class does not
hardcode any specific LLM provider or checkpoint backend — subclasses provide
their own model and checkpointer via abstract factory methods.
"""

import asyncio
import json
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
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from ..agent.base import BaseAgent
from ..middleware.credential_injector import BaseCredentialInjector
from ..models import TODO_STATE_MAP, AgentStreamResponse, TodoItem, UserConfig
from ..utils.streaming import StreamBuffer, StructuredResponseStreamer, extract_text_from_content

logger = logging.getLogger(__name__)


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
            stream_buffer = StreamBuffer()
            response_streamer = StructuredResponseStreamer("FinalResponseSchema")
            # Track per-message JSON detection to suppress FinalResponseSchema text output
            _current_msg_id = None
            _current_msg_is_json = False

            async for part in self._graph.astream(
                {"messages": input_messages}, config, stream_mode=["updates", "messages"], version="v2"
            ):
                chunk_count += 1
                part_type = part["type"]
                logger.debug(f"Graph event #{chunk_count}: type={part_type}")

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

        except Exception as e:
            logger.error(f"Error in {self.__class__.__name__}.stream: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=f"An error occurred while processing your request: {str(e)}",
                metadata={"error": str(e)},
            )
