"""Response models for the Orchestrator Deep Agent.

This module contains response models that follow the A2A protocol,
providing a clean interface for agent-client communication.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict
from a2a.types import TaskState


class AgentStreamResponse(BaseModel):
    """Standard response model for agent streaming operations.
    
    Follows the A2A protocol by using TaskState enum values to indicate
    the current state of task execution. This provides a consistent interface
    between the orchestrator agent and clients.
    
    Attributes:
        state: Current task state (working, completed, failed, input_required, auth_required, etc.)
        content: Human-readable message or result content
        interrupt_reason: Optional reason for interruption (e.g., 'graph_interrupted', 'auth_required')
        pending_nodes: Optional list of pending graph nodes (for graph interruptions)
        metadata: Optional additional metadata (auth_info, artifacts, etc.)
    """
    
    state: TaskState = Field(
        ...,
        description="Current A2A task state"
    )
    content: str = Field(
        ...,
        description="Human-readable message or result content"
    )
    interrupt_reason: Optional[str] = Field(
        default=None,
        description="Reason for task interruption (e.g., 'graph_interrupted', 'auth_required')"
    )
    pending_nodes: Optional[List[str]] = Field(
        default=None,
        description="List of pending graph nodes (for graph interruptions)"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional metadata (auth_info, artifacts, task_id, context_id, etc.)"
    )
    
    model_config = ConfigDict(
        use_enum_values=False,
        arbitrary_types_allowed=True
    )
    
    @classmethod
    def auth_required(
        cls,
        message: str,
        auth_url: str = "",
        error_code: str = "",
        **metadata: Any
    ) -> "AgentStreamResponse":
        """Factory method for creating auth required responses.
        
        Args:
            message: Human-readable auth message
            auth_url: URL for authentication flow
            error_code: Error code from auth system
            **metadata: Additional metadata to include
            
        Returns:
            AgentStreamResponse with auth_required state
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
            interrupt_reason='auth_required',
            metadata={
                "auth_url": auth_url,
                "error_code": error_code,
                "requires_auth": True,
                **metadata
            }
        )
    
    @classmethod
    def working(cls, message: str, **metadata: Any) -> "AgentStreamResponse":
        """Factory method for creating working status responses."""
        return cls(
            state=TaskState.working,
            content=message,
            metadata=metadata if metadata else None
        )
    
    @classmethod
    def completed(cls, content: str, **metadata: Any) -> "AgentStreamResponse":
        """Factory method for creating completed responses."""
        return cls(
            state=TaskState.completed,
            content=content,
            metadata=metadata if metadata else None
        )
    
    @classmethod
    def failed(cls, message: str, **metadata: Any) -> "AgentStreamResponse":
        """Factory method for creating failed responses."""
        return cls(
            state=TaskState.failed,
            content=message,
            metadata=metadata if metadata else None
        )
    
    @classmethod
    def input_required(
        cls,
        message: str,
        pending_nodes: Optional[List[str]] = None,
        **metadata: Any
    ) -> "AgentStreamResponse":
        """Factory method for creating input required responses."""
        return cls(
            state=TaskState.input_required,
            content=message,
            interrupt_reason='graph_interrupted',
            pending_nodes=pending_nodes,
            metadata=metadata if metadata else None
        )
