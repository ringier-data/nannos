"""Delivery channel management router.

Provides CRUD endpoints for A2A push-notification delivery channels.
Channels are registered by A2A clients (via Keycloak client credentials) and
consumed by the scheduler when delivering job notifications.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from ..db.session import DbSession
from ..dependencies import (
    get_client_id_from_request,
    get_token_claims_from_request,
    is_admin_mode,
    is_own_client_credentials,
    require_auth_or_bearer_token,
)
from ..models.delivery_channel import (
    DeliveryChannelCreate,
    DeliveryChannelListResponse,
    DeliveryChannelResponse,
    DeliveryChannelUpdate,
    UndeliveredReport,
)
from ..models.user import User
from ..services.forwarded_attribution import forwarded_installation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/delivery-channels", tags=["Delivery Channels"])


def _get_delivery_channel_repository(request: Request):  # type: ignore[return]
    return request.app.state.delivery_channel_repository


@router.post(
    "",
    response_model=DeliveryChannelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new delivery channel.",
    description=(
        "Creates a push-notification delivery channel that the scheduler can use "
        "to notify recipients when a job completes.\n\n"
        "A2A clients authenticate via Keycloak client-credentials (Bearer JWT); the "
        "``azp`` claim is stored as ``client_id`` so clients can later manage their "
        "own channels.\n\n"
        "Channels are resolved to the requesting installation at the agent/MCP layer; "
        "the console lists all channels to authenticated users."
    ),
)
async def register_channel(
    request: Request,
    response: Response,
    data: DeliveryChannelCreate,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> DeliveryChannelResponse:
    """Register a delivery channel on behalf of an A2A client."""
    # Extract client_id from the Bearer JWT (azp claim); fall back to the user's sub.
    # Either way the caller has a client_id, and installation_id is required, so every
    # registration is an idempotent upsert keyed by (client_id, installation_id) — safe
    # against concurrent replica boots. Returns 200 on update, 201 on create.
    caller_client_id = await get_client_id_from_request(request)
    client_id = caller_client_id or current_user.sub

    repo = _get_delivery_channel_repository(request)

    channel, created = await repo.upsert_channel_by_installation(
        db=db, actor=current_user, client_id=client_id, data=data
    )
    await db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return channel


@router.get(
    "",
    response_model=DeliveryChannelListResponse,
    summary="List delivery channels visible to the caller.",
    description=(
        "For A2A clients (Bearer token with ``azp`` claim): returns only the channels "
        "registered by that client.\n\n"
        "For session-authenticated console users: returns all channels, each with the "
        "caller's own ``reachability`` on it."
    ),
)
async def list_channels(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
    page: int = Query(1, ge=1, description="Page number"),
    # Unbounded by default so A2A clients keep seeing all of their channels.
    limit: int | None = Query(None, ge=1, le=100, description="Items per page"),
    search: str | None = Query(None, description="Search by channel name"),
) -> DeliveryChannelListResponse:
    """List delivery channels scoped to the authenticated caller."""
    repo = _get_delivery_channel_repository(request)

    client_id = await get_client_id_from_request(request)
    if client_id:
        # A2A client — return only its own channels
        channels, total = await repo.list_channels_for_client(
            db=db, client_id=client_id, search=search, page=page, limit=limit
        )
    else:
        # Human console user — all channels (no group scoping).
        # With the caller's own reachability on each, never anyone else's: what the
        # channel picker greys out (#192).
        channels, total = await repo.list_all_channels(
            db=db, search=search, page=page, limit=limit, reachability_for=current_user.id
        )

    return DeliveryChannelListResponse(channels=channels, total=total)


@router.get(
    "/mcp-list",
    response_model=DeliveryChannelListResponse,
    tags=["MCP"],
    operation_id="console_list_delivery_channels",
    summary="List the delivery channels available for sending notifications.",
    description=(
        "List the notification delivery channels (Slack, Google Chat, email, …) that the "
        "current user can be reached on. Use this to pick where to send a scheduled-job or "
        "completion notification — e.g. when the user asks to 'notify me' or 'remind me by "
        "email'. Each channel has a human-readable name following the "
        "``{installation}-{channel-type}`` convention (e.g. ``ada-slack``, ``nannos-email``); "
        "match the user's stated preference against those names, otherwise default to the "
        "channel for the installation the request came from. Returns no secrets."
    ),
)
async def list_delivery_channels_mcp(
    request: Request,
    db: DbSession,
    _current_user: User = Depends(require_auth_or_bearer_token),
) -> DeliveryChannelListResponse:
    """MCP tool: list delivery channels scoped to the calling installation.

    The ``installation`` is auto-instrumented from the forwarded request context
    (``x-nannos-context``), NOT a model-visible argument — the agent can neither see nor
    spoof it. When no installation is present (e.g. the web-console, a secondary interface),
    all channels are returned, which is an accepted trade-off.
    """
    repo = _get_delivery_channel_repository(request)

    installation = forwarded_installation(request)
    if installation:
        channels = await repo.list_channels_for_installation(db=db, installation_id=installation)
    else:
        # MCP tool: the agent needs every channel it could send to, never a page.
        channels, _ = await repo.list_all_channels(db=db)

    return DeliveryChannelListResponse(channels=channels, total=len(channels))


@router.patch(
    "/{channel_id}",
    response_model=DeliveryChannelResponse,
    summary="Update a delivery channel.",
    description=(
        "Partial update — only fields provided in the request body are changed.\n\n"
        "Allowed callers:\n"
        "- The A2A client that originally registered the channel (matching ``client_id``).\n"
        "- System admins."
    ),
)
async def update_channel(
    channel_id: int,
    data: DeliveryChannelUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> DeliveryChannelResponse:
    """Partially update a delivery channel."""
    repo = _get_delivery_channel_repository(request)

    owner_client_id = await repo.get_owner_client_id(db, channel_id)
    if owner_client_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery channel not found")

    await _require_channel_write_access(request, owner_client_id, current_user)

    updated = await repo.update_channel(db=db, actor=current_user, channel_id=channel_id, data=data)
    await db.commit()
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery channel not found")
    return updated


@router.delete(
    "/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a delivery channel.",
    description=(
        "Permanently removes a delivery channel.\n\n"
        "Allowed callers: owning A2A client or admins."
    ),
)
async def delete_channel(
    channel_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    """Delete a delivery channel."""
    repo = _get_delivery_channel_repository(request)

    owner_client_id = await repo.get_owner_client_id(db, channel_id)
    if owner_client_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery channel not found")

    await _require_channel_write_access(request, owner_client_id, current_user)

    deleted = await repo.delete_channel(db=db, actor=current_user, channel_id=channel_id)
    await db.commit()
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery channel not found")


async def _require_channel_write_access(
    request: Request,
    owner_client_id: str,
    current_user: User,
) -> None:
    """Raise 403 if the caller does not have write access to the channel.

    Access is granted when:
    - The Bearer token's ``client_id`` (``azp``) matches the channel's ``client_id``.
    - The user is an admin with admin mode enabled.
    """
    # 1. Owning A2A client
    caller_client_id = await get_client_id_from_request(request)
    if caller_client_id and caller_client_id == owner_client_id:
        return

    # 2. Admin
    if is_admin_mode(request, current_user):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to modify this delivery channel.",
    )


@router.post(
    "/undelivered",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Report that a scheduled run's notification could not be delivered.",
    description=(
        "Called by the chat client that received a scheduled run's push and could not deliver "
        "it: it found no sign-in for the subscriber in that installation, or posting failed. "
        "The push was already acknowledged, so this is how the scheduler learns the run reached "
        "nobody. The run keeps its status and is marked undelivered; 'no_recipient' also holds "
        "the subscription until the subscriber signs in from that channel.\n\n"
        "Only the client that owns the run's delivery channel may report it, with its own "
        "client-credentials token. 404 for any run that is not on one of its channels."
    ),
)
async def report_undelivered(report: UndeliveredReport, request: Request, db: DbSession) -> None:
    """Record a client's report that a run reached nobody (#191)."""
    claims = await get_token_claims_from_request(request)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    client_id = claims.get("azp") or claims.get("client_id")
    if not is_own_client_credentials(claims, client_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A delivery report must come from the channel's client, with its own client-credentials token",
        )
    if not await request.app.state.scheduler_service.report_undelivered(db, client_id, report):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
