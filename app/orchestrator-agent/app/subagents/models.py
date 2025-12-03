"""
A2A Models - Clean implementation using A2A SDK types.

This module provides proper response models that leverage A2A SDK types
and follow the A2A protocol specification exactly.

Also includes configuration models for dynamic local sub-agents.
"""

import json
from typing import Any, Dict, Optional

from a2a.types import Message, Task, TaskState
from langchain.messages import AIMessage
from pydantic import BaseModel, Field


class LocalSubAgentConfig(BaseModel):
    """Configuration for a dynamically provisioned local sub-agent.

    Local sub-agents are LangGraph agents that run in-process (not remote A2A servers)
    but follow the A2A protocol for response format. They are configured per-user
    and instantiated at runtime.

    Attributes:
        name: Unique identifier for the sub-agent (used in task tool enum).
        model_name: Optional model name override for this sub-agent (inherits orchestrator model if None).
        description: Human-readable description shown in task tool description.
        system_prompt: The system prompt that defines the agent's behavior.
        mcp_gateway_url: Optional MCP gateway URL for tool discovery.
            - If None: The sub-agent inherits tools from the orchestrator.
            - If set: Tools are discovered lazily from this MCP gateway on first
              invocation and override orchestrator tools entirely.

    Example DynamoDB JSON:
        {
            "local_subagents": [
                {
                    "name": "data-analyst",
                    "description": "Analyzes data and generates insights",
                    "system_prompt": "You are a data analysis expert...",
                    "mcp_gateway_url": null
                },
                {
                    "name": "code-reviewer",
                    "description": "Reviews code for best practices",
                    "system_prompt": "You are a senior code reviewer...",
                    "mcp_gateway_url": "https://code-tools.example.com/mcp"
                }
            ]
        }
    """

    model_name: Optional[str] = Field(
        default=None,
        description="Optional model name override for this sub-agent (inherits orchestrator model if None)",
    )

    name: str = Field(
        ...,
        description="Unique identifier for the sub-agent (used in task tool enum)",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$",
    )
    description: str = Field(
        ...,
        description="Human-readable description shown in task tool description",
        min_length=1,
    )
    system_prompt: str = Field(
        ...,
        description="The system prompt that defines the agent's behavior",
        min_length=1,
    )
    mcp_gateway_url: Optional[str] = Field(
        default=None,
        description="Optional MCP gateway URL for tool discovery. If None, inherits orchestrator tools.",
    )


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
        default_factory=dict, description="Application-specific metadata not part of A2A protocol"
    )

    # LangChain compatibility (for DeepAgents)
    messages: list[AIMessage] = Field(
        default_factory=list, description="LangChain messages for compatibility with DeepAgents"
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
