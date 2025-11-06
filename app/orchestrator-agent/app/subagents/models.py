"""
A2A Models - Clean implementation using A2A SDK types.

This module provides proper response models that leverage A2A SDK types
and follow the A2A protocol specification exactly.
"""
import json
from typing import Any, Dict
from pydantic import BaseModel, Field
from a2a.types import Task, TaskState, Message
from langchain.messages import AIMessage


class A2ATaskResponse(BaseModel):
    """
    A2A Task response model using proper A2A SDK types.
    
    Uses the A2A Task object directly to maintain protocol compliance
    while adding application-specific metadata separately.
    """
    
    # Core A2A protocol data - use the SDK types directly
    task: Task = Field(..., description="The A2A Task object from the protocol")
    
    # Application-specific metadata (separate from A2A protocol)
    app_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Application-specific metadata not part of A2A protocol"
    )
    
    # LangChain compatibility (for DeepAgents)
    messages: list[AIMessage] = Field(
        default_factory=list,
        description="LangChain messages for compatibility with DeepAgents"
    )
    
    @property
    def is_complete(self) -> bool:
        """Derived property from A2A TaskState."""
        return self.task.status.state in [
            TaskState.completed,
            TaskState.failed,
            TaskState.canceled,
            TaskState.rejected,
        ]
    
    @property
    def requires_auth(self) -> bool:
        """Derived property from A2A TaskState."""
        return self.task.status.state == TaskState.auth_required
    
    @property
    def requires_input(self) -> bool:
        """Derived property from A2A TaskState."""
        return self.task.status.state == TaskState.input_required
    
    def extract_text_from_status(self) -> str:
        """Extract text content from A2A task status message."""
        if self.task.status.message and self.task.status.message.parts:
            return self._extract_text_from_parts(self.task.status.message.parts)
        return ""
    
    def _extract_text_from_parts(self, parts) -> str:
        """Extract text content from A2A Message parts."""
        for part in parts:
            inner_part = part.root
            if inner_part.kind == "text":
                return inner_part.text
            elif inner_part.kind == "data":
                return json.dumps(inner_part.data)
        return ""


class A2AMessageResponse(BaseModel):
    """
    A2A Message response model using proper A2A SDK types.
    """
    
    message: Message = Field(..., description="The A2A Message object from the protocol")
    
    def extract_text_content(self) -> str:
        """Extract text content from A2A Message parts."""
        for part in self.message.parts:
            inner_part = part.root
            if inner_part.kind == "text":
                return inner_part.text
            elif inner_part.kind == "data":
                return json.dumps(inner_part.data)
        return ""
