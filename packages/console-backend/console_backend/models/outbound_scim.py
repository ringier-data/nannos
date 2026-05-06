"""Pydantic models for outbound SCIM endpoint management API."""

from datetime import datetime

from pydantic import BaseModel, Field

from .user import PaginationMeta


class OutboundScimEndpointCreate(BaseModel):
    """Request to create an outbound SCIM endpoint."""

    name: str = Field(..., min_length=1, max_length=200, description="Human-readable label")
    endpoint_url: str = Field(..., min_length=1, max_length=2000, description="Base URL of the remote SCIM server")
    bearer_token: str = Field(..., min_length=1, description="Bearer token for authenticating with the remote SCIM server")
    push_users: bool = Field(True, description="Whether to push user changes to this endpoint")
    push_groups: bool = Field(True, description="Whether to push group changes to this endpoint")


class OutboundScimEndpointUpdate(BaseModel):
    """Request to update an outbound SCIM endpoint."""

    name: str | None = Field(None, min_length=1, max_length=200)
    endpoint_url: str | None = Field(None, min_length=1, max_length=2000)
    bearer_token: str | None = Field(None, min_length=1)
    enabled: bool | None = None
    push_users: bool | None = None
    push_groups: bool | None = None


class OutboundScimEndpoint(BaseModel):
    """Outbound SCIM endpoint metadata (bearer token masked)."""

    id: int
    name: str
    endpoint_url: str
    token_hint: str = Field(..., description="Last 4 characters of the bearer token")
    enabled: bool
    push_users: bool
    push_groups: bool
    created_by: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OutboundScimEndpointCreated(BaseModel):
    """Response after creating an outbound SCIM endpoint. Contains the full token (shown only once)."""

    id: int
    name: str
    endpoint_url: str
    bearer_token: str = Field(..., description="The full bearer token. Only shown at creation time.")
    enabled: bool
    push_users: bool
    push_groups: bool
    created_at: datetime


class OutboundScimEndpointListResponse(BaseModel):
    """Paginated outbound SCIM endpoint list response."""

    data: list[OutboundScimEndpoint]
    meta: PaginationMeta


class OutboundScimEndpointDetailResponse(BaseModel):
    """Single outbound SCIM endpoint detail response."""

    data: OutboundScimEndpoint


class OutboundScimTestResult(BaseModel):
    """Result of testing connectivity to an outbound SCIM endpoint."""

    success: bool
    status_code: int | None = None
    detail: str | None = None
