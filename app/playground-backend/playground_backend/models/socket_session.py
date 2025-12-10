"""Socket session model for DynamoDB storage."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class SocketSession(BaseModel):
    """Socket.IO session model for DynamoDB storage.

    Stores minimal session data for Socket.IO connections. The httpx client and
    A2A client are cached in-memory per server instance (not in DynamoDB) and
    cleaned up on disconnect. Agent cards are cached separately in a global cache
    shared across all connections.
    """

    socket_id: str  # The Socket.IO session ID (sid) with 'socket:' prefix as DynamoDB key
    user_id: str  # User's ID (sub from OIDC)
    http_session_id: str  # HTTP session ID for linking back to user session
    agent_url: str | None = None  # Agent URL for cache lookup
    custom_headers: dict[str, str] = Field(default_factory=dict)  # Custom HTTP headers
    is_initialized: bool = False  # Whether client has been initialized
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl: int  # Unix timestamp for DynamoDB TTL (auto-cleanup)

    class Config:
        """Pydantic configuration."""

        json_encoders = {datetime: lambda v: v.isoformat()}
