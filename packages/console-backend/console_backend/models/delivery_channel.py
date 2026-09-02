"""Pydantic models for delivery channels."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MessageFormatting = Literal["markdown", "slack", "google-chat", "plain"]

_FORMATTING_DESCRIPTION = (
    "How this channel renders delivered text. Nothing rewrites an agent's output on the "
    "way out, so the writer is told these rules up front: 'slack' for Slack mrkdwn, "
    "'google-chat' for Google Chat markup, 'plain' for no markup, 'markdown' (default) "
    "for standard Markdown as the web console renders it."
)


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
    installation_id: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Stable client-supplied identifier (e.g. the bot's installation) that scopes the "
            "channel to a tenant and is the idempotency key for re-registration. Required: "
            "every channel resolves by (client_id, installation_id)."
        ),
    )
    message_formatting: MessageFormatting = Field(
        default="markdown",
        description=_FORMATTING_DESCRIPTION,
    )


class DeliveryChannelUpdate(BaseModel):
    """Request body for updating an existing delivery channel.  All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    webhook_url: str | None = None
    secret: str | None = Field(default=None, min_length=1)
    message_formatting: MessageFormatting | None = Field(default=None, description=_FORMATTING_DESCRIPTION)


class DeliveryChannelResponse(BaseModel):
    """Delivery channel as returned by the API.  The secret is never included."""

    id: int
    name: str
    description: str | None = None
    webhook_url: str
    message_formatting: MessageFormatting = Field(
        default="markdown",
        description=_FORMATTING_DESCRIPTION,
    )
    client_id: str = Field(description="Keycloak client ID of the A2A service that registered this channel.")
    registered_by: str = Field(description="OIDC subject (sub) of the token used to register this channel.")
    installation_id: str | None = Field(
        default=None,
        description="Stable client-supplied identifier (set when the channel was self-registered).",
    )
    created_at: datetime
    updated_at: datetime


class DeliveryChannelListResponse(BaseModel):
    """Wrapper around a list of delivery channels."""

    channels: list[DeliveryChannelResponse]
