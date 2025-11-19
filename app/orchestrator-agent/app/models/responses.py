"""Response models for the Orchestrator Deep Agent.

This module contains response models that follow the A2A protocol,
providing a clean interface for agent-client communication.
"""

from typing import Any, List, Optional

from a2a.types import TaskState
from pydantic import Field
from ringier_a2a_sdk import BaseAgentStreamResponse


class AgentStreamResponse(BaseAgentStreamResponse):
    """LangGraph-aware response model for agent streaming operations.

    Extends BaseAgentStreamResponse with LangGraph-specific fields for handling
    graph interruptions and node management.

    Additional Attributes:
        interrupt_reason: Reason for interruption (e.g., 'graph_interrupted', 'auth_required')
        pending_nodes: List of pending graph nodes (for graph interruptions)
    """

    interrupt_reason: Optional[str] = Field(
        default=None, description="Reason for task interruption (e.g., 'graph_interrupted', 'auth_required')"
    )
    pending_nodes: Optional[List[str]] = Field(
        default=None, description="List of pending graph nodes (for graph interruptions)"
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
        auth_content = message
        if auth_url:
            auth_content = (
                f"{message}\n\n"
                f"Please visit the following URL to complete authentication:\n"
                f"{auth_url}\n\n"
                f"After completing authentication, you can retry your request."
            )
        else:
            auth_content = f"{message}\n\nPlease complete the required authentication and try again."

        return cls(
            state=TaskState.auth_required,
            content=auth_content,
            interrupt_reason="auth_required",
            metadata={"auth_url": auth_url, "error_code": error_code, "requires_auth": True, **metadata},
        )
