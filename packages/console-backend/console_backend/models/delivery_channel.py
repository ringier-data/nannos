"""Pydantic models for delivery channels."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
# The one definition of a channel format, shared with every writer through the SDK.
from ringier_a2a_sdk.message_formatting import DEFAULT_MESSAGE_FORMATTING, MessageFormatting

_FORMATTING_DESCRIPTION = (
    "How this channel renders delivered text. Nothing rewrites an agent's output on the "
    "way out, so the writer is told these rules up front: 'slack' for Slack mrkdwn, "
    "'google-chat' for Google Chat markup, 'plain' for no markup, 'markdown' (default) "
    "for standard Markdown as the web console renders it."
)

#: Whether a user can receive on a delivery channel (#192, ADR-0011 amendment 1).
#: ``reachable``: a brokered sign-in of theirs through the channel's client is in a
#: workspace that lists the channel's installation. ``unreachable``: they have signed in
#: through that client, the installation belongs to a workspace the client has published,
#: and none of their sign-ins is in it. ``unknown``: nothing to go on, because they have no
#: brokered sign-in with that client (an old local sign-in looks the same as none) or the
#: client has not published the installation. Only ``unreachable`` refuses anything.
DeliveryReachability = Literal["reachable", "unreachable", "unknown"]

#: Why a scheduled run's notification reached nobody, as the receiving client reported it
#: (#191). ``no_recipient``: the client holds no sign-in for the subscriber in that
#: installation, which only they can fix. ``send_failed``: a recipient was found and
#: posting failed, which is transient.
DeliveryFailure = Literal["no_recipient", "send_failed"]

_REACHABILITY_DESCRIPTION = (
    "Whether the user can receive on this channel: 'reachable' (they signed in to Nannos from "
    "there), 'unreachable' (they have not; messaging Nannos there once activates it) or "
    "'unknown' (Nannos cannot tell yet, e.g. an older sign-in). Null where it was not asked."
)


_WORKSPACE_DESCRIPTION = (
    "The workspace this installation belongs to, in the vocabulary the client binds sign-ins "
    "under (a Slack team id, a Google Chat project number). A sign-in bound to that workspace "
    "reaches the channel. Omitted means unchanged; never set means Nannos cannot tell who it "
    "reaches, so it warns rather than refuses."
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
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200, description=_WORKSPACE_DESCRIPTION)
    message_formatting: MessageFormatting | None = Field(
        default=None,
        description=(
            _FORMATTING_DESCRIPTION
            + " Omitted means 'unchanged': a client that does not declare a format leaves "
            "the stored value alone, so re-registration on every boot cannot reset a "
            "channel that was set elsewhere. A new channel falls back to the column default."
        ),
    )


class DeliveryChannelUpdate(BaseModel):
    """Request body for updating an existing delivery channel.  All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    webhook_url: str | None = None
    secret: str | None = Field(default=None, min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200, description=_WORKSPACE_DESCRIPTION)
    message_formatting: MessageFormatting | None = Field(default=None, description=_FORMATTING_DESCRIPTION)


class DeliveryChannelResponse(BaseModel):
    """Delivery channel as returned by the API.  The secret is never included."""

    id: int
    name: str
    description: str | None = None
    webhook_url: str
    message_formatting: MessageFormatting = Field(
        default=DEFAULT_MESSAGE_FORMATTING,
        description=_FORMATTING_DESCRIPTION,
    )
    client_id: str = Field(description="Keycloak client ID of the A2A service that registered this channel.")
    registered_by: str = Field(description="OIDC subject (sub) of the token used to register this channel.")
    installation_id: str | None = Field(
        default=None,
        description="Stable client-supplied identifier (set when the channel was self-registered).",
    )
    workspace_id: str | None = Field(default=None, description=_WORKSPACE_DESCRIPTION)
    reachability: DeliveryReachability | None = Field(default=None, description=_REACHABILITY_DESCRIPTION)
    created_at: datetime
    updated_at: datetime


class DeliveryChannelListResponse(BaseModel):
    """Wrapper around a list of delivery channels."""

    channels: list[DeliveryChannelResponse]
    # Total matching channels, which exceeds len(channels) when a page was asked for.
    total: int = 0


#: How much of a client's failure detail the run record keeps.
_DETAIL_MAX = 500


class UndeliveredReport(BaseModel):
    """A chat client's report that it could not deliver a scheduled run's notification.

    The push was already acknowledged (the webhook answers before it looks the recipient
    up), so this is the only way the scheduler learns the run reached nobody.
    """

    run_id: int = Field(description="``scheduled_job_run_id`` from the scheduler payload.")
    installation_id: str = Field(
        min_length=1,
        max_length=200,
        description="The installation that received the push, as its delivery channel is registered.",
    )
    reason: DeliveryFailure = Field(
        description=(
            "'no_recipient': this installation has no sign-in for the subscriber, which only they "
            "can fix (the job is held until they sign in there). 'send_failed': a recipient was "
            "found but posting failed; the run is marked undelivered and the job keeps running."
        )
    )
    detail: str | None = Field(
        default=None,
        description="What failed, for the backend's log line; not stored. Longer than 500 characters is cut, not refused.",
    )

    @field_validator("detail", mode="before")
    @classmethod
    def _cut_detail(cls, value: object) -> object:
        """Cut, never refuse: a 422 over a diagnostic string would drop the report itself,
        leaving the run counted as delivered, which is the silent failure it exists for."""
        return value[:_DETAIL_MAX] if isinstance(value, str) else value
