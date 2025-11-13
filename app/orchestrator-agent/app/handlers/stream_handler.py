"""
Stream response handling for OrchestratorDeepAgent.

Handles parsing agent state, building response objects, and auth requirement detection.
"""

import logging
from typing import Any, Dict, Optional

from a2a.types import TaskState

from ..models import AgentStreamResponse

logger = logging.getLogger(__name__)


class StreamHandler:
    """Handles stream response generation and state parsing."""

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

        # PRIORITY: Check for structured_response from the model (FinalResponseSchema)
        # This is the agent's explicit determination of task status
        if isinstance(final_state, dict) and "structured_response" in final_state:
            structured_response = final_state.get("structured_response")
            logger.info(f"[STREAM HANDLER] Found structured_response: {structured_response}")

            if structured_response is not None:
                # Extract task_state, message, and metadata from structured response
                task_state = getattr(structured_response, "task_state", TaskState.completed)
                message = getattr(structured_response, "message", "Task completed")
                reasoning = getattr(structured_response, "reasoning", None)
                todo_summary = getattr(structured_response, "todo_summary", None)

                logger.info(f"[STREAM HANDLER] Agent determined task_state: {task_state}")
                logger.debug(f"[STREAM HANDLER] Reasoning: {reasoning}")

                # Build metadata
                metadata = {}
                if reasoning:
                    metadata["reasoning"] = reasoning
                if todo_summary:
                    metadata["todo_summary"] = todo_summary

                # Build appropriate response based on task_state
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
                else:  # completed or any other state defaults to completed
                    return AgentStreamResponse(
                        state=TaskState.completed, content=message, metadata=metadata if metadata else None
                    )

        # SECONDARY: Check for auth requirements from a2a_tracking
        if isinstance(final_state, dict) and "a2a_tracking" in final_state:
            a2a_tracking = final_state.get("a2a_tracking", {})
            logger.debug(f"[STREAM HANDLER] Found a2a_tracking: {a2a_tracking}")

            # Check each tracked agent for auth requirements
            for agent_name, tracking_data in a2a_tracking.items():
                if isinstance(tracking_data, dict) and tracking_data.get("requires_auth"):
                    logger.info(f"[STREAM HANDLER] Agent {agent_name} requires authentication")

                    auth_message = tracking_data.get("auth_message", "Authentication required")
                    auth_url = tracking_data.get("auth_url", "")
                    error_code = tracking_data.get("error_code", "AUTH_REQUIRED")

                    return StreamHandler.build_auth_response(
                        auth_message=auth_message, auth_url=auth_url, error_code=error_code, agent_name=agent_name
                    )

        # FALLBACK: Extract final message content (legacy behavior)
        logger.warning("[STREAM HANDLER] No structured_response found, falling back to legacy behavior")
        messages = final_state.get("messages", []) if isinstance(final_state, dict) else []
        if messages:
            last_message = messages[-1]
            content = getattr(last_message, "content", str(last_message))
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
