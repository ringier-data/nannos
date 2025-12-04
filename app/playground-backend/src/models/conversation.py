"""Conversation model for DynamoDB storage."""

from datetime import datetime

from pydantic import BaseModel


class Conversation(BaseModel):
    """Conversation model for DynamoDB storage."""

    conversation_id: str  # Unique conversation ID (primary key - PK)
    user_id: str  # User who owns the conversation
    started_at: datetime  # When the conversation was started
    last_message_at: datetime  # Timestamp of the last message in the conversation
    last_updated: datetime | None = None  # Last time the conversation record was updated
    status: str = "active"  # Conversation status: 'active' or 'archived'
    metadata: dict[str, str] = {}  # Optional key/value metadata
    title: str = ""  # Conversation title/name
    agent_url: str = ""  # Agent URL used in this conversation
    ttl: int  # Unix timestamp for DynamoDB TTL
