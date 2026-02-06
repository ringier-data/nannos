"""Common models for A2A agents."""

from typing import Any, Dict, Optional

from a2a.types import TaskState
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class BaseAgentStreamResponse(BaseModel):
    """Base response model for agent streaming operations.

    Follows the A2A protocol by using TaskState enum values to indicate
    the current state of task execution. This provides a consistent interface
    between agents and clients.

    Attributes:
        state: Current task state (working, completed, failed, input_required, auth_required, etc.)
        content: Human-readable message or result content
        metadata: Optional additional metadata (auth_info, artifacts, etc.)
    """

    state: TaskState = Field(..., description="Current A2A task state")
    content: str = Field(..., description="Human-readable message or result content")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional metadata (auth_info, artifacts, task_id, context_id, etc.)"
    )

    model_config = ConfigDict(use_enum_values=False, arbitrary_types_allowed=True)


# Backwards compatibility alias
AgentStreamResponse = BaseAgentStreamResponse


class UserConfig(BaseModel):
    """User-specific configuration for personalized agent behavior.

    Contains user credentials, preferences, and discovered tools/sub-agents.
    Note: access_token is optional for downstream agents that use orchestrator JWT auth.
    """

    user_sub: str = Field(..., description="OIDC subject identifier for the user")
    access_token: Optional[SecretStr] = Field(
        default=None, description="User authentication token (optional for downstream agents)"
    )
    name: str = Field(..., description="User's full name")
    email: str = Field(..., description="User's email address")
    language: str = Field(default="en", description="User's preferred language")
    timezone: str = Field(default="Europe/Zurich", description="User's preferred timezone (IANA timezone name)")
    sub_agent_id: Optional[int] = Field(
        default=None, description="Sub-agent ID for cost attribution (set by orchestrator)"
    )
    sub_agents: Optional[list] = Field(default=None, description="Discovered sub-agents")
    tools: Optional[list] = Field(default=None, description="Discovered tools")

    model_config = ConfigDict(arbitrary_types_allowed=True)
