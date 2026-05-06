"""Pydantic models for secrets management."""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class SecretType(str, Enum):
    """Secret type enum matching database enum."""

    FOUNDRY_CLIENT_SECRET = "foundry_client_secret"


class Secret(BaseModel):
    """Secret model for SSM Parameter Store reference."""

    id: int
    owner_user_id: str
    name: str
    description: str | None = None
    secret_type: SecretType
    ssm_parameter_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: datetime | None = None

    class Config:
        from_attributes = True


class SecretCreate(BaseModel):
    """Request model for creating a secret."""

    name: str
    description: str | None = None
    secret_type: SecretType
    secret_value: str  # Plain text value - will be stored in SSM Parameter Store


class SecretListResponse(BaseModel):
    """Response model for listing secrets (without actual secret values)."""

    items: list[Secret]
    total: int


class SecretGroupPermission(BaseModel):
    """Group permission for a secret with read/write granularity."""

    user_group_id: int
    permissions: list[Literal["read", "write"]]


class SecretPermissionsUpdate(BaseModel):
    """Request model for updating secret permissions with read/write granularity."""

    group_permissions: list[SecretGroupPermission]


class SecretGroupPermissionResponse(BaseModel):
    """Response model for group permissions on a secret."""

    user_group_id: int
    user_group_name: str
    permissions: list[Literal["read", "write"]]
