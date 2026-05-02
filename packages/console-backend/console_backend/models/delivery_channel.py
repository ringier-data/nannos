"""Pydantic models for delivery channels."""

from datetime import datetime

from pydantic import BaseModel, Field


class DeliveryChannelCreate(BaseModel):
    """Request body for registering a new delivery channel (A2A client → backend)."""

    name: str = Field(min_length=1, max_length=200, description="Human-readable channel name.")
    description: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "Optional description for the LLM to understand when this channel should be used "
            "(e.g. 'Sends push notifications to the Alloy mobile app for critical alerts')."
        ),
    )
    webhook_url: str = Field(description="HTTPS URL the scheduler will POST notifications to.")
    secret: str = Field(
        min_length=1,
        description="Shared secret sent verbatim as the X-A2A-Notification-Token header on every push.",
    )
    group_ids: list[int] = Field(
        description="IDs of user groups whose members can see and use this channel.",
    )


class DeliveryChannelUpdate(BaseModel):
    """Request body for updating an existing delivery channel.  All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    webhook_url: str | None = None
    secret: str | None = Field(default=None, min_length=1)
    group_ids: list[int] | None = None


class DeliveryChannelResponse(BaseModel):
    """Delivery channel as returned by the API.  The secret is never included."""

    id: int
    name: str
    description: str | None = None
    webhook_url: str
    client_id: str = Field(description="Keycloak client ID of the A2A service that registered this channel.")
    registered_by: str = Field(description="OIDC subject (sub) of the token used to register this channel.")
    group_ids: list[int] = Field(description="IDs of groups that can use this channel.")
    created_at: datetime
    updated_at: datetime


class DeliveryChannelListResponse(BaseModel):
    """Wrapper around a list of delivery channels."""

    channels: list[DeliveryChannelResponse]
