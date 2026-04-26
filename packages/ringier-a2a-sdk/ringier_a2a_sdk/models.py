"""Common models for A2A agents."""

from typing import Any, Dict, Literal, Optional

from a2a.types import TaskState
from pydantic import BaseModel, ConfigDict, Field, SecretStr

TodoState = Literal["submitted", "working", "completed", "failed"]

# Map internal todo statuses to A2A TodoState values
TODO_STATE_MAP: dict[str, str] = {
    "pending": "submitted",
    "in_progress": "working",
    "completed": "completed",
    "failed": "failed",
}


class TodoItem(BaseModel):
    """A single item in a work-plan todo checklist.

    Serialised inside a DataPart of work-plan status-update messages.
    """

    name: str = Field(..., description="Human-readable task description")
    state: TodoState = Field(..., description="Current task state")
    source: Optional[str] = Field(default=None, description="Sub-agent that owns this item")


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
    scheduled_job_id: Optional[int] = Field(
        default=None, description="Scheduled job ID for cost attribution (set by agent-runner)"
    )
    phone_number: Optional[str] = Field(
        default=None, description="User's resolved phone number (override ?? idp, from JWT phone_number claim)"
    )
    sub_agents: Optional[list] = Field(default=None, description="Discovered sub-agents")
    tools: Optional[list] = Field(default=None, description="Discovered tools")

    model_config = ConfigDict(arbitrary_types_allowed=True)
