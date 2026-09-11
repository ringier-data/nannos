"""Socket session model."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class SocketSession(BaseModel):
    """Socket.IO session model.

    Stores minimal session data for Socket.IO connections. The httpx client and
    A2A client are cached in-memory per server instance and cleaned up on
    disconnect. Agent cards are cached separately in a global cache shared
    across all connections.
    """

    socket_id: str  # The Socket.IO session ID (sid) with 'socket:' prefix
    user_id: str  # User's ID (sub from OIDC)
    http_session_id: str  # HTTP session ID for linking back to user session
    agent_url: str | None = None  # Agent URL for cache lookup
    custom_headers: dict[str, str] = Field(default_factory=dict)  # Custom HTTP headers
    is_initialized: bool = False  # Whether client has been initialized
    # Sub-agent this connection is bound to, derived at connect from the token's azp
    # (embed bindings, ADR-0006). None for console sessions and unbound tokens.
    embedded_sub_agent_id: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        """Pydantic configuration."""

        json_encoders = {datetime: lambda v: v.isoformat()}
