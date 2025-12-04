"""User model."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class User(BaseModel):
    """User model for DynamoDB storage."""

    id: str  # Primary key (sub from OIDC)
    sub: str  # OIDC subject identifier
    email: str
    first_name: str
    last_name: str
    company_name: str | None = None
    is_administrator: bool = False
    agent_urls: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    language: str = 'en'
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
