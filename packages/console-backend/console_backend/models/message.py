"""Message model."""

from typing import Any

from a2a.types import TaskState
from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    """Message model.

    Uses composite key structure:
    - Partition Key: conversation_id
    - Sort Key: MSG#<timestamp>#<message_id>
    """

    # Parts are held as ProtoJSON dicts for storage and as protobuf Part objects
    # when hydrated for delivery — both are non-Pydantic, so allow arbitrary types.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    conversation_id: str  # Partition key
    sort_key: str  # Sort key in format "MSG#<timestamp>#<messageId>"
    user_id: str  # User who sent the message
    message_id: str  # Unique message ID (extracted from sort_key)
    role: str  # 'user' or 'assistant'
    parts: list[Any] = Field(default_factory=list)  # A2A Part (protobuf) or ProtoJSON dict
    task_id: str = ""  # Task ID (optional)
    created_at: str  # ISO format timestamp
    # A2A v1.0+ TaskState is a protobuf int enum (value stored as its int).
    state: int = TaskState.TASK_STATE_UNSPECIFIED  # Message state as A2A TaskState
    raw_payload: str = ""  # Original JSON payload
    metadata: dict[str, Any] = Field(default_factory=dict)  # Optional metadata
    kind: str = ""  # Message kind: 'message', 'status-update', etc.
