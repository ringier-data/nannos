"""Pydantic models for SCIM token management API."""

from datetime import datetime

from pydantic import BaseModel, Field

from .user import PaginationMeta


class ScimTokenCreate(BaseModel):
    """Request to create a SCIM bearer token."""

    name: str = Field(..., min_length=1, max_length=200, description="Human-readable label for the token")
    description: str | None = Field(None, max_length=1000, description="Optional description")
    expires_at: datetime | None = Field(None, description="Optional expiry time (null = never expires)")


class ScimToken(BaseModel):
    """SCIM token metadata (token value masked)."""

    id: int
    name: str
    description: str | None = None
    token_hint: str = Field(..., description="Last 4 characters of the token for identification")
    created_by: str
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class ScimTokenCreated(BaseModel):
    """Response after creating a SCIM token. Contains the full token value (shown only once)."""

    id: int
    name: str
    description: str | None = None
    token: str = Field(..., description="The full bearer token value. Only shown at creation time.")
    expires_at: datetime | None = None
    created_at: datetime


class ScimTokenListResponse(BaseModel):
    """Paginated SCIM token list response."""

    data: list[ScimToken]
    meta: PaginationMeta


class ScimTokenDetailResponse(BaseModel):
    """Single SCIM token detail response."""

    data: ScimToken
