"""Session model."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class StoredSession(BaseModel):
    """Session model for storage."""

    session_id: str  # Primary key
    user_id: str  # User's sub from OIDC
    access_token: str  # Access token for token exchange
    access_token_expires_at: datetime  # When the access token expires (from expires_in)
    refresh_token: str
    id_token: str  # ID token for logout
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime  # When the session expires
    # Orchestrator session cookie (JWT from orchestrator agent)
    orchestrator_session_cookie: str | None = None
    orchestrator_cookie_expires_at: datetime | None = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
