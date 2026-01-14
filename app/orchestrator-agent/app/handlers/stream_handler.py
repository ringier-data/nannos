"""
Stream response handling for OrchestratorDeepAgent.

Handles parsing agent state, building response objects, and auth requirement detection.
"""

import json
import logging
from typing import Any, Dict, Optional

from a2a.types import TaskState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ..models import AgentStreamResponse

logger = logging.getLogger(__name__)


class StreamHandler:
    """Handles stream response generation and state parsing."""

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        """Extract text from message content, handling both string and list formats.

        Bedrock models with extended thinking return content as a list of blocks:
        [{'type': 'reasoning_content', ...}, {'type': 'text', 'text': '...'}]

        GPT-4o and other models return content as a simple string.

        Args:
            content: Message content (string or list of content blocks)

        Returns:
            Extracted text content as string
        """
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            # Extract text from content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and "text" in block:
                        text_parts.append(block["text"])
            return " ".join(text_parts) if text_parts else str(content)
        else:
            return str(content)

    @staticmethod
    def _extract_current_turn_messages(messages: list) -> list:
        """Extract messages from the current conversation turn.

        A turn starts with a HumanMessage (user input). This method finds the most
        recent HumanMessage and returns all messages that come after it, representing
        the current turn's conversation flow.

        Args:
            messages: List of conversation messages

        Returns:
            List of messages from the current turn (after the last HumanMessage)
        """
        if not messages:
            return []

        # Find the index of the most recent HumanMessage (start of current turn)
        last_human_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                last_human_idx = i
                break

        if last_human_idx is None:
            # No HumanMessage found, return all messages (shouldn't normally happen)
            logger.warning("[STREAM HANDLER] No HumanMessage found in conversation, using all messages")
            return messages
        else:
            # Return only messages after the last HumanMessage (current turn)
            current_turn = messages[last_human_idx + 1 :]
            logger.debug(
                f"[STREAM HANDLER] Current turn starts at index {last_human_idx}, {len(current_turn)} messages in turn"
            )
            return current_turn

    @staticmethod
    def _extract_recently_called_subagents(final_state: Dict[str, Any]) -> set[str]:
        """Extract the names of sub-agents that were called in the current turn.

        Only examines ToolMessages from the current turn (after the last HumanMessage)
        to ensure we're checking sub-agents that were actually called in response to
        the current user request, not stale state from previous turns.

        Args:
            final_state: Final state dict containing messages

        Returns:
            Set of sub-agent names that were called in the current turn
        """
        messages = final_state.get("messages", [])
        messages_to_check = StreamHandler._extract_current_turn_messages(messages)

        recently_called = set()

        # Collect all ToolMessages in the current turn
        for i, msg in enumerate(messages_to_check):
            if isinstance(msg, ToolMessage):
                # Find the corresponding AIMessage with tool_calls
                # Look backward from this ToolMessage within the current turn
                for prev_msg in reversed(messages_to_check[:i]):
                    if isinstance(prev_msg, AIMessage) and hasattr(prev_msg, "tool_calls") and prev_msg.tool_calls:
                        for tool_call in prev_msg.tool_calls:
                            if tool_call.get("id") == msg.tool_call_id and tool_call.get("name") == "task":
                                subagent_type = tool_call.get("args", {}).get("subagent_type")
                                if subagent_type:
                                    recently_called.add(subagent_type)
                                    logger.debug(f"[STREAM HANDLER] Found sub-agent in current turn: {subagent_type}")

        logger.debug(f"[STREAM HANDLER] Sub-agents called in current turn: {recently_called}")
        return recently_called

    @staticmethod
    def _check_all_agents_blocked(
        recently_called: set[str], a2a_tracking: Dict[str, Any]
    ) -> tuple[bool, Optional[tuple[str, Dict[str, Any]]]]:
        """Check if ALL recently-called agents are blocked or incomplete.

        DEFENSIVE DESIGN: Treats agents as "blocked" if they:
        - Require auth (requires_auth=True)
        - Require input (requires_input=True)
        - Failed (state=TaskState.failed)
        - Still working (state=TaskState.working or is_complete=False)

        This prevents the orchestrator from claiming completion when sub-agents
        haven't reached a terminal success state.

        Note: ToolMessage status='error' is now detected earlier in A2ATaskTrackingMiddleware
        and converted to state='TaskState.failed' in the a2a_tracking data.

        Args:
            recently_called: Set of agent names called in current turn
            a2a_tracking: A2A tracking data for all agents

        Returns:
            Tuple of (all_blocked: bool, prioritized_agent_info: Optional[tuple[agent_name, tracking_data]])
            If all_blocked is True, returns the agent to use for override (auth prioritized, then failed, then input)
        """
        if not recently_called:
            return False, None

        all_blocked = True
        blocked_agent_info = None  # Stores (agent_name, tracking_data)
        blocked_auth_agent_info = None  # Highest priority: auth required
        blocked_failed_agent_info = None  # Second priority: failed

        for agent_name in recently_called:
            tracking_data = a2a_tracking.get(agent_name, {})
            if not isinstance(tracking_data, dict):
                continue

            # Extract state information
            requires_auth = tracking_data.get("requires_auth", False)
            requires_input = tracking_data.get("requires_input", False)
            is_complete = tracking_data.get("is_complete", True)  # Defensive: assume incomplete if missing
            state = tracking_data.get("state", "")

            # Check if state is explicitly failed (now includes ToolMessage status='error' detected by middleware)
            is_failed = ("failed" in str(state).lower()) or (state == "TaskState.failed")

            # Agent is blocked if: requires auth, requires input, failed, or not complete
            is_blocked = requires_auth or requires_input or is_failed or not is_complete

            if is_blocked:
                # Store first blocked agent info for potential override
                if blocked_agent_info is None:
                    blocked_agent_info = (agent_name, tracking_data)
                # Prioritize auth over other issues
                if requires_auth and blocked_auth_agent_info is None:
                    blocked_auth_agent_info = (agent_name, tracking_data)
                # Prioritize failed over input_required
                if is_failed and blocked_failed_agent_info is None:
                    blocked_failed_agent_info = (agent_name, tracking_data)
            else:
                # At least one agent successfully completed
                all_blocked = False
                break

        # Return prioritized agent: auth > failed > other blocked
        if blocked_auth_agent_info:
            prioritized_agent = blocked_auth_agent_info
        elif blocked_failed_agent_info:
            prioritized_agent = blocked_failed_agent_info
        else:
            prioritized_agent = blocked_agent_info

        return all_blocked, prioritized_agent

    @staticmethod
    def _build_blocked_agent_response(
        agent_name: str, tracking_data: Dict[str, Any], messages: list
    ) -> AgentStreamResponse:
        """Build response for a blocked agent (auth, input, failed, or incomplete).

        Args:
            agent_name: Name of the blocked agent
            tracking_data: A2A tracking data for the agent
            messages: Conversation messages for context

        Returns:
            AgentStreamResponse with appropriate state (auth_required, input_required, or failed)
        """
        # Priority order: auth > failed > input > incomplete
        if tracking_data.get("requires_auth"):
            auth_message = tracking_data.get("auth_message", "Authentication required")
            auth_url = tracking_data.get("auth_url", "")
            error_code = tracking_data.get("error_code", "AUTH_REQUIRED")

            return StreamHandler.build_auth_response(
                auth_message=auth_message,
                auth_url=auth_url,
                error_code=error_code,
                agent_name=agent_name,
            )

        # Check for failed state
        state = tracking_data.get("state", "")
        is_failed = ("failed" in str(state).lower()) or (state == "TaskState.failed")

        if is_failed:
            # Extract failure message from the last tool message
            if messages:
                last_message = messages[-1]
                content = getattr(last_message, "content", "The agent failed to complete the task.")
                content = StreamHandler._extract_text_from_content(content)
            else:
                content = "The agent failed to complete the task."

            return AgentStreamResponse(
                state=TaskState.failed,
                content=f"{agent_name} failed: {content}",
                metadata={"agent_name": agent_name, "tracking_data": tracking_data},
            )

        elif tracking_data.get("requires_input"):
            if messages:
                last_message = messages[-1]
                content = getattr(last_message, "content", "Additional input required to complete the task.")
                content = StreamHandler._extract_text_from_content(content)
            else:
                content = "Additional input required to complete the task."

            return AgentStreamResponse(
                state=TaskState.input_required,
                content=content,
                interrupt_reason="subagent_input_required",
                metadata={"agent_name": agent_name, "tracking_data": tracking_data},
            )

        # Should not reach here, but return input_required as safe default
        return AgentStreamResponse(
            state=TaskState.input_required,
            content="Additional input required to complete the task.",
            interrupt_reason="subagent_input_required",
            metadata={"agent_name": agent_name, "tracking_data": tracking_data},
        )

    @staticmethod
    def build_auth_response(auth_message: str, auth_url: str, error_code: str, **metadata) -> AgentStreamResponse:
        """Build consistent auth required responses.

        Args:
            auth_message: Human-readable authentication message
            auth_url: URL for completing authentication
            error_code: Error code identifier
            **metadata: Additional metadata (tool name, subagent name, etc.)

        Returns:
            AgentStreamResponse with auth_required state
        """
        # TODO: Localize auth_content based on user config language
        if auth_url:
            auth_content = (
                f"{auth_message}\n\n"
                f"Please visit the following URL to complete authentication:\n"
                f"{auth_url}\n\n"
                f"After completing authentication, you can retry your request."
            )
        else:
            # TODO: we should instruct the chat UI to show an auth widget instead
            auth_content = (
                f"{auth_message}\n\n"
                f"Please complete the required authentication and try again. Just answer DONE when authorized."
            )

        return AgentStreamResponse(
            state=TaskState.auth_required,
            content=auth_content,
            interrupt_reason="auth_required",
            metadata={"auth_url": auth_url, "error_code": error_code, "requires_auth": True, **metadata},
        )

    @staticmethod
    def parse_agent_response(final_state: Any) -> AgentStreamResponse:
        """Parse the agent response to extract structured information.

        Checks for authentication requirements and constructs appropriate response.
        Now also extracts the task_state from the agent's structured output.

        Args:
            final_state: Final state from graph execution

        Returns:
            AgentStreamResponse with appropriate state and content
        """
        logger.debug("===== PARSING AGENT RESPONSE =====")
        logger.debug(f"Final state for response parsing: {final_state}")
        logger.debug(f"Final state type: {type(final_state)}")

        # PRIORITY: Extract FinalResponseSchema from current turn's tool_calls
        # This ensures we get the LATEST response, not a stale one from checkpointer state
        # The structured_response in final_state may be from a previous turn/model
        structured_response = None

        if isinstance(final_state, dict):
            messages = final_state.get("messages", [])
            if messages:
                # Extract current turn messages only (after last HumanMessage)
                current_turn_messages = StreamHandler._extract_current_turn_messages(messages)

                # Search backwards through current turn for FinalResponseSchema tool call
                for msg in reversed(current_turn_messages):
                    if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tool_call in msg.tool_calls:
                            if tool_call.get("name") == "FinalResponseSchema":
                                structured_response = tool_call.get("args", {})
                                logger.info(
                                    f"[STREAM HANDLER] Found FinalResponseSchema in current turn tool_calls: {structured_response}"
                                )
                                break
                        if structured_response:
                            break

        # FALLBACK: Check structured_response from final_state (may be set by AutoStrategy for OpenAI)
        # Only use if we didn't find a tool call in the current turn
        if not structured_response and isinstance(final_state, dict) and "structured_response" in final_state:
            structured_response = final_state.get("structured_response")
            logger.info(
                f"[STREAM HANDLER] Using structured_response from final_state (fallback): {structured_response}"
            )

        if structured_response is not None:
            # Parse structured response using Pydantic model validation
            # This handles both dict (from tool_calls) and object (already validated) formats
            from ..models.schemas import FinalResponseSchema

            try:
                if isinstance(structured_response, dict):
                    # Parse dict into FinalResponseSchema (validates and normalizes task_state)
                    parsed = FinalResponseSchema.model_validate(structured_response)
                elif isinstance(structured_response, FinalResponseSchema):
                    # Already a validated FinalResponseSchema
                    parsed = structured_response
                else:
                    # Try to convert to dict and parse
                    parsed = FinalResponseSchema.model_validate(structured_response.__dict__)

                # Extract validated fields
                task_state = parsed.task_state
                message = parsed.message
                # reasoning = parsed.reasoning
                todo_summary = parsed.todo_summary
            except Exception as e:
                logger.error(f"Failed to parse structured_response: {e}", exc_info=True)
                # Fallback to completed with error message
                return AgentStreamResponse(
                    state=TaskState.completed,
                    content="Task processing completed with validation errors.",
                    metadata={"parse_error": str(e)},
                )

            logger.info(
                f"[STREAM HANDLER] Agent determined task_state: {task_state} (raw: {structured_response.get('task_state') if isinstance(structured_response, dict) else 'N/A'})"
            )
            logger.debug(f"[STREAM HANDLER] Message: {message}")

            # Check if we should append sub-agent output
            include_subagent_output = getattr(parsed, "include_subagent_output", False)
            logger.info(f"[STREAM HANDLER] include_subagent_output={include_subagent_output}")
            if include_subagent_output:
                # Extract sub-agent output from the most recent "task" ToolMessage in current turn
                # The content is already stored there by DynamicToolDispatchMiddleware
                #
                # IMPORTANT: Filter out non-sub-agent tool messages:
                # - FinalResponseSchema (Bedrock structured output)
                # - Other tools (file operations, etc.)
                # Only extract from ToolMessages that correspond to "task" tool calls (sub-agents)
                if isinstance(final_state, dict):
                    messages = final_state.get("messages", [])
                    if messages:
                        # Get current turn messages (after last HumanMessage)
                        current_turn_messages = StreamHandler._extract_current_turn_messages(messages)

                        # Build a map of tool_call_id -> tool_name for filtering
                        tool_call_map = {}
                        for msg in current_turn_messages:
                            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tool_call in msg.tool_calls:
                                    tool_call_map[tool_call.get("id")] = tool_call

                        # Find the most recent "task" ToolMessage (sub-agent response)
                        subagent_content = None
                        for msg in reversed(current_turn_messages):
                            if isinstance(msg, ToolMessage):
                                tool_call = tool_call_map.get(msg.tool_call_id)
                                # Filter: only process "task" tool calls (sub-agents)
                                if tool_call and tool_call.get("name") == "task":
                                    # Found a sub-agent ToolMessage
                                    try:
                                        # Sub-agent content may be JSON-wrapped
                                        if isinstance(msg.content, str):
                                            parsed_content = json.loads(msg.content)
                                            subagent_content = parsed_content.get("message", msg.content)
                                        else:
                                            subagent_content = msg.content
                                    except json.JSONDecodeError:
                                        subagent_content = msg.content

                                    logger.info(
                                        f"[STREAM HANDLER] Found sub-agent ToolMessage (tool_call_id={msg.tool_call_id}, "
                                        f"subagent={tool_call.get('args', {}).get('subagent_type')}, "
                                        f"content_length={len(subagent_content) if subagent_content else 0})"
                                    )
                                    break

                        if subagent_content:
                            # Append sub-agent output to message
                            # Use double newline separator if message is not empty, otherwise just the content
                            if message:
                                message = f"{message}\n\n{subagent_content}"
                            else:
                                message = subagent_content
                            logger.info(
                                f"[STREAM HANDLER] Appended sub-agent output to message "
                                f"(original length: {len(parsed.message)}, appended: {len(subagent_content)}, "
                                f"total: {len(message)})"
                            )
                        else:
                            logger.warning(
                                "[STREAM HANDLER] include_subagent_output=true but no sub-agent ToolMessage found in current turn "
                                "(FinalResponseSchema and other tool messages filtered out)"
                            )
                    else:
                        logger.warning("[STREAM HANDLER] include_subagent_output=true but no messages in state")
                else:
                    logger.warning(
                        f"[STREAM HANDLER] include_subagent_output=true but final_state is not a dict: {type(final_state)}"
                    )

            # Build metadata
            metadata = {}
            # if reasoning:
            #     metadata["reasoning"] = reasoning
            if todo_summary:
                metadata["todo_summary"] = todo_summary

            # Build appropriate response based on task_state
            # Note: If LLM explicitly chose input_required/failed/working, respect that decision
            if task_state == TaskState.input_required:
                return AgentStreamResponse(
                    state=TaskState.input_required,
                    content=message,
                    interrupt_reason="input_required",
                    metadata=metadata,
                )
            elif task_state == TaskState.failed:
                return AgentStreamResponse(state=TaskState.failed, content=message, metadata=metadata)
            elif task_state == TaskState.working:
                return AgentStreamResponse(state=TaskState.working, content=message, metadata=metadata)
            elif task_state == TaskState.completed:
                # SAFETY CHECK: LLM says "completed", but verify if ALL sub-agents are actually blocked
                # This prevents hallucination where LLM thinks task is done but all agents need intervention
                #
                # SCENARIO CONTEXT - Why we can't distinguish intent:
                #
                # Parallel Execution (intentional):
                #   Agent calls Jira + Email simultaneously
                #   Jira: blocked (requires_input), Email: success
                #   → Trust LLM: Email success might satisfy the user's request
                #
                # Sequential Fallback (de-routing):
                #   Agent calls Jira, gets blocked, tries Email instead
                #   Jira: blocked (requires_input), Email: success
                #   → Trust LLM: Email success might be the alternative solution
                #
                # Hallucination (what we protect against):
                #   Agent calls Jira + Email
                #   Jira: blocked, Email: blocked
                #   LLM incorrectly says "completed"
                #   → Override: Nothing actually completed, need user intervention
                #
                # STRATEGY: Only override "completed" if ALL called agents are blocked
                # Trust LLM judgment about partial success or alternative approaches

                if isinstance(final_state, dict) and "a2a_tracking" in final_state:
                    a2a_tracking = final_state.get("a2a_tracking", {})
                    recently_called = StreamHandler._extract_recently_called_subagents(final_state)

                    all_blocked, prioritized_agent = StreamHandler._check_all_agents_blocked(
                        recently_called, a2a_tracking
                    )

                    # Override ONLY if ALL agents are blocked (safety against hallucination)
                    if all_blocked and prioritized_agent:
                        agent_name, tracking_data = prioritized_agent

                        logger.warning(
                            f"[STREAM HANDLER] LLM said 'completed' but ALL agents blocked - overriding. "
                            f"Agents: {recently_called}"
                        )

                        messages = final_state.get("messages", [])
                        return StreamHandler._build_blocked_agent_response(agent_name, tracking_data, messages)

                # No override needed - return LLM's completed response
                return AgentStreamResponse(
                    state=TaskState.completed, content=message, metadata=metadata if metadata else None
                )
            else:
                # Unknown state - default to completed
                return AgentStreamResponse(
                    state=TaskState.completed, content=message, metadata=metadata if metadata else None
                )

        # FALLBACK: No structured_response (unexpected) - default to completed
        logger.warning("[STREAM HANDLER] No structured_response found, defaulting to completed")
        messages = final_state.get("messages", []) if isinstance(final_state, dict) else []
        if messages:
            last_message = messages[-1]
            content = getattr(last_message, "content", str(last_message))
            content = StreamHandler._extract_text_from_content(content)
        else:
            content = "Task completed successfully"

        return AgentStreamResponse(state=TaskState.completed, content=content)

    @staticmethod
    def build_working_response(content: str, metadata: Optional[Dict[str, Any]] = None) -> AgentStreamResponse:
        """Build a working state response.

        Args:
            content: Progress message
            metadata: Optional additional metadata

        Returns:
            AgentStreamResponse with working state
        """
        return AgentStreamResponse(state=TaskState.working, content=content, metadata=metadata)

    @staticmethod
    def build_completed_response(content: str, metadata: Optional[Dict[str, Any]] = None) -> AgentStreamResponse:
        """Build a completed state response.

        Args:
            content: Final result message
            metadata: Optional additional metadata

        Returns:
            AgentStreamResponse with completed state
        """
        return AgentStreamResponse(state=TaskState.completed, content=content, metadata=metadata)

    @staticmethod
    def build_failed_response(content: str, metadata: Optional[Dict[str, Any]] = None) -> AgentStreamResponse:
        """Build a failed state response.

        Args:
            content: Error message
            metadata: Optional additional metadata

        Returns:
            AgentStreamResponse with failed state
        """
        return AgentStreamResponse(state=TaskState.failed, content=content, metadata=metadata)

    @staticmethod
    def build_input_required_response(
        content: str, prompt: str, metadata: Optional[Dict[str, Any]] = None
    ) -> AgentStreamResponse:
        """Build an input required state response.

        Args:
            content: Message explaining what input is needed
            prompt: Specific prompt for user input
            metadata: Optional additional metadata

        Returns:
            AgentStreamResponse with input_required state
        """
        response_metadata = {"input_prompt": prompt}
        if metadata:
            response_metadata.update(metadata)

        return AgentStreamResponse(
            state=TaskState.input_required,
            content=content,
            interrupt_reason="input_required",
            metadata=response_metadata,
        )
