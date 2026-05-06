"""
A2A Models - Clean implementation using A2A SDK types.

This module provides proper response models that leverage A2A SDK types
and follow the A2A protocol specification exactly.

Also includes configuration models for dynamic local sub-agents.
"""

from typing import Annotated, Any, Dict, Literal, Optional, Union

from a2a.types import Message, Task, TaskState
from langchain.messages import AIMessage
from pydantic import BaseModel, Discriminator, Field
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

from agent_common.models.base import ThinkingLevel


class BaseLocalSubAgentConfig(BaseModel):
    """Base configuration for local sub-agents."""

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
    # TODO: the agent-console doesn't support to specify input modalities per sub-agent yet, but we add this in the config for future support
    input_modes: list[str] | None = Field(
        default=None,
        description="Optional list of input modalities supported by this agent (e.g., ['text', 'image']). If None, derived from the model's capabilities.",
    )


class LocalFoundrySubAgentConfig(BaseLocalSubAgentConfig):
    """Configuration for a dynamically provisioned Foundry sub-agent.

    Foundry sub-agents are LangGraph agents that run in-process (not remote A2A servers)
    but follow the A2A protocol for response format. They are configured per-user
    and instantiated at runtime.

    Attributes:
        name: Unique identifier for the sub-agent (used in task tool enum).
        description: Human-readable description shown in task tool description.
        sub_agent_id: Optional sub_agent ID for tracking agent-created agents.
        foundry_hostname: Foundry hostname (e.g., https://blumen.palantirfoundry.de).
        foundry_client_id: OAuth2 client ID for Foundry authentication.
        foundry_client_secret_ref: SSM Parameter Store name for Foundry client secret.
        foundry_ontology_rid: Ontology RID to use for this agent.
        foundry_query_api_name: Query API name to invoke for this agent.
        foundry_scopes: List of OAuth2 scopes required for this agent.
        foundry_version: Optional version of the Foundry agent configuration.
    """

    type: Literal["foundry"] = "foundry"
    sub_agent_id: Optional[int] = Field(
        default=None, description="Console backend sub_agent ID for tracking agent-created agents"
    )
    hostname: str = Field(
        default="https://blumen.palantirfoundry.de",
        description="Foundry instance hostname (e.g., 'https://blumen.palantirfoundry.de')",
    )
    client_id: str = Field(..., description="OAuth2 client ID for Foundry authentication")
    client_secret_ref: str = Field(..., description="SSM Parameter Store name for Foundry client secret")
    ontology_rid: str = Field(..., description="Ontology RID (required)")
    query_api_name: str = Field(..., description="Query API name to execute (e.g., 'a2ATicketWriterAgent')")
    scopes: list[str] = Field(..., description="OAuth2 scopes for Foundry API access")
    version: Optional[str] = Field(None, description="Optional version of the Foundry agent configuration")


class LocalLangGraphSubAgentConfig(BaseLocalSubAgentConfig):
    """Configuration for a dynamically provisioned local sub-agent.

    Local sub-agents are LangGraph agents that run in-process (not remote A2A servers)
    but follow the A2A protocol for response format. They are configured per-user
    and instantiated at runtime.

    Attributes:
        name: Unique identifier for the sub-agent (used in task tool enum).
        model_name: Optional model name override for this sub-agent (inherits orchestrator model if None).
        description: Human-readable description shown in task tool description.
        sub_agent_id: Optional sub_agent ID for tracking agent-created agents.
        system_prompt: The system prompt that defines the agent's behavior.
        mcp_tools: Optional list of MCP tool names to enable for this sub-agent.
            - If None or empty: The sub-agent inherits tools from the orchestrator.
            - If set: Only these tools from Gatana MCP gateway are enabled for the sub-agent.

    Example DynamoDB JSON:
        {
            "local_subagents": [
                {
                    "name": "data-analyst",
                    "description": "Analyzes data and generates insights",
                    "system_prompt": "You are a data analysis expert...",
                    "mcp_tools": ["query_database", "generate_chart"]
                },
                {
                    "name": "code-reviewer",
                    "description": "Reviews code for best practices",
                    "system_prompt": "You are a senior code reviewer...",
                    "mcp_tools": null
                }
            ]
        }
    """

    type: Literal["langgraph"] = "langgraph"
    sub_agent_id: Optional[int] = Field(
        default=None, description="Console backend sub_agent ID for tracking agent-created agents"
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Optional model name override for this sub-agent (inherits orchestrator model if None)",
    )
    system_prompt: str = Field(
        ...,
        description="The system prompt that defines the agent's behavior",
        min_length=1,
    )
    mcp_tools: Optional[list[str]] = Field(
        default=None,
        description="Optional list of MCP tool names enabled for this sub-agent. If None, inherits orchestrator tools.",
    )
    enable_thinking: bool | None = Field(
        default=None,
        description="Enable extended thinking mode for Claude Sonnet and Gemini models",
    )
    thinking_level: ThinkingLevel | None = Field(
        default=None,
        description="Thinking depth level (minimal/low/medium/high) for extended thinking mode",
    )


LocalSubAgentConfig = Annotated[Union[LocalFoundrySubAgentConfig, LocalLangGraphSubAgentConfig], Discriminator("type")]


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
            return a2a_parts_to_content(self.task.status.message.parts, text_only=True)
        return ""


class A2AMessageResponse(BaseModel):
    """
    A2A Message response model using proper A2A SDK types.
    """

    message: Message = Field(..., description="The A2A Message object from the protocol")

    def extract_text_content(self) -> str:
        """Extract text content from A2A Message parts."""
        return a2a_parts_to_content(self.message.parts, text_only=True)
