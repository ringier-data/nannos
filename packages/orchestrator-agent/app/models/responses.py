"""Response models for the Orchestrator Deep Agent.

This module contains response models that follow the A2A protocol,
providing a clean interface for agent-client communication.
"""

from typing import Any, List, Optional

from a2a.types import TaskState
from langchain.agents.middleware.human_in_the_loop import ActionRequest, ReviewConfig
from pydantic import Field
from ringier_a2a_sdk import BaseAgentStreamResponse


class AgentStreamResponse(BaseAgentStreamResponse):
    """LangGraph-aware response model for agent streaming operations.

    Extends BaseAgentStreamResponse with LangGraph-specific fields for handling
    graph interruptions and node management.

    Additional Attributes:
        interrupt_reason: Reason for interruption (e.g., 'graph_interrupted', 'auth_required')
        pending_nodes: List of pending graph nodes (for graph interruptions)
        action_requests: HITL action requests from ConditionalHumanInTheLoopMiddleware
        review_configs: HITL review configs specifying allowed decisions per action
    """

    interrupt_reason: Optional[str] = Field(
        default=None, description="Reason for task interruption (e.g., 'graph_interrupted', 'auth_required')"
    )
    pending_nodes: Optional[List[str]] = Field(
        default=None, description="List of pending graph nodes (for graph interruptions)"
    )
    action_requests: Optional[List[ActionRequest]] = Field(
        default=None, description="HITL action requests requiring human review"
    )
    review_configs: Optional[List[ReviewConfig]] = Field(
        default=None, description="Review policies specifying allowed decisions per action"
    )

    @classmethod
    def auth_required(
        cls, message: str, auth_url: str = "", error_code: str = "", **metadata: Any
    ) -> "AgentStreamResponse":
        """Factory method for creating auth required responses with graph context.

        Args:
            message: Human-readable auth message
            auth_url: URL for authentication flow
            error_code: Error code from auth system
            **metadata: Additional metadata to include

        Returns:
            AgentStreamResponse with auth_required state and interrupt_reason
        """
        # TODO: Localize auth_content based on user config language
        if auth_url:
            auth_content = (
                f"{message}\n\n"
                f"Please visit the following URL to complete authentication:\n"
                f"{auth_url}\n\n"
                f"After completing authentication, you can retry your request."
            )
        else:
            # TODO: we should instruct the chat UI to show an auth widget instead
            auth_content = (
                f"{message}\n\n"
                f"Please complete the required authentication and try again. Just answer DONE when authorized."
            )

        return cls(
            state=TaskState.TASK_STATE_AUTH_REQUIRED,
            content=auth_content,
            interrupt_reason="auth_required",
            metadata={"auth_url": auth_url, "error_code": error_code, "requires_auth": True, **metadata},
        )

    @classmethod
    def from_interrupt(cls, value: Any, pending_nodes: Optional[List[str]] = None) -> "AgentStreamResponse":
        """Map a LangGraph interrupt value to the A2A response the executor emits.

        Single dispatch point shared by the routing orchestrator (``stream``,
        which reads the interrupt from the checkpointed final state) and the
        embedded execute-only path (``stream_subagent``, which catches the
        re-raised ``GraphInterrupt``), so both surface interrupts identically:

        - ``AuthErrorDetectionMiddleware`` auth interrupt → ``auth_required``
          with the authorize URL in both the message text and metadata. The
          tool's own message only *references* the URL ("go to the
          authorizeUrl"), so dropping ``auth_url`` here strands the user.
        - HITL interrupt (``action_requests``) → ``input_required`` carrying
          the action requests / review configs for the approval card.
        - Anything else → generic interrupt passthrough.

        Args:
            value: The interrupt payload (``Interrupt.value``); non-dict values
                are treated as empty.
            pending_nodes: Pending graph nodes, when the caller has read them
                from checkpoint state (unavailable on the exception path).
        """
        if not isinstance(value, dict):
            value = {}
        task_state = value.get("task_state", TaskState.TASK_STATE_INPUT_REQUIRED)

        if task_state == TaskState.TASK_STATE_AUTH_REQUIRED:
            extras = {
                k: v for k, v in value.items() if k not in ("task_state", "message", "auth_url", "error_code")
            }
            return cls.auth_required(
                message=value.get("message", "Authentication required"),
                auth_url=value.get("auth_url", ""),
                error_code=value.get("error_code", ""),
                **extras,
            )

        action_requests = value.get("action_requests")
        review_configs = value.get("review_configs")
        if action_requests and isinstance(action_requests, list):
            tool_names = [ar.get("name") for ar in action_requests if isinstance(ar, dict)]
            if "console_create_bug_report" in tool_names:
                # Bug report HITL interrupt: surface the model's reason alongside
                # the confirmation prompt.
                bug_action = next(ar for ar in action_requests if ar.get("name") == "console_create_bug_report")
                reason = bug_action.get("args", {}).get("description", "")
                description = bug_action.get("description", "")
                content = f"Reason: {reason}\n\n{description}" if reason else description
                return cls(
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    content=content or "Bug report requires your confirmation.",
                    interrupt_reason=reason,
                    pending_nodes=pending_nodes,
                    action_requests=action_requests,
                    review_configs=review_configs,
                )
            description = action_requests[0].get("description", "") if isinstance(action_requests[0], dict) else ""
            return cls(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                content=description or "Tool execution requires approval.",
                pending_nodes=pending_nodes,
                action_requests=action_requests,
                review_configs=review_configs,
            )

        # Standard interrupt (file permissions, custom interrupts, etc.)
        return cls(
            state=task_state,
            content=value.get("message", "Process interrupted. Human intervention required."),
            interrupt_reason=value.get("reason", "graph_interrupted"),
            pending_nodes=pending_nodes,
        )
