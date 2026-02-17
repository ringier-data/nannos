"""Message model for DynamoDB storage."""

from typing import Any

from a2a.types import Part, TaskState
from pydantic import BaseModel, Field


class Message(BaseModel):
    """Message model for DynamoDB storage.

    Uses composite key structure:
    - Partition Key: conversation_id
    - Sort Key: MSG#<timestamp>#<message_id>
    """

    conversation_id: str  # Partition key
    sort_key: str  # Sort key in format "MSG#<timestamp>#<messageId>"
    user_id: str  # User who sent the message
    message_id: str  # Unique message ID (extracted from sort_key)
    role: str  # 'user' or 'assistant'
    parts: list[Part] = Field(default_factory=list)
    task_id: str = ""  # Task ID (optional)
    created_at: str  # ISO format timestamp
    state: TaskState = TaskState.unknown  # Message state as A2A TaskState
    raw_payload: str = ""  # Original JSON payload
    metadata: dict[str, Any] = Field(default_factory=dict)  # Optional metadata
    ttl: int  # Unix timestamp for DynamoDB TTL
    final: bool = False  # Indicates if this is a final message
    kind: str = ""  # Message kind: 'message', 'status-update', etc.
