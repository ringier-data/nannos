from typing import Any, Dict, Optional

from a2a.types import TaskState
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AgentStreamResponse(BaseModel):
    """Standard response model for agent streaming operations.

    Follows the A2A protocol by using TaskState enum values to indicate
    the current state of task execution. This provides a consistent interface
    between the orchestrator agent and clients.

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


class UserConfig(BaseModel):
    """User-specific configuration for personalized agent behavior.

    Contains user credentials, preferences, and discovered tools/sub-agents.
    """

    user_id: str = Field(..., description="User identifier")
    access_token: SecretStr = Field(..., description="User authentication token")
    name: str = Field(..., description="User's full name")
    email: str = Field(..., description="User's email address")
    language: str = Field(default="en", description="User's preferred language")
    sub_agents: Optional[list] = Field(default=None, description="Discovered sub-agents")
    tools: Optional[list] = Field(default=None, description="Discovered tools")

    model_config = ConfigDict(arbitrary_types_allowed=True)
