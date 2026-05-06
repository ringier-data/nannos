"""Delivery channel management router.

Provides CRUD endpoints for A2A push-notification delivery channels.
Channels are registered by A2A clients (via Keycloak client credentials) and
consumed by the scheduler when delivering job notifications.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..db.session import DbSession
from ..dependencies import get_client_id_from_request, is_admin_mode, require_auth_or_bearer_token
from ..models.delivery_channel import (
    DeliveryChannelCreate,
    DeliveryChannelListResponse,
    DeliveryChannelResponse,
    DeliveryChannelUpdate,
)
from ..models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/delivery-channels", tags=["Delivery Channels"])


def _get_delivery_channel_repository(request: Request):  # type: ignore[return]
    return request.app.state.delivery_channel_repository


def _get_user_group_service(request: Request):  # type: ignore[return]
    return request.app.state.user_group_service


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
        "``group_ids`` controls which users can see and select this channel when "
        "creating scheduled jobs."
    ),
)
async def register_channel(
    request: Request,
    data: DeliveryChannelCreate,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> DeliveryChannelResponse:
    """Register a delivery channel on behalf of an A2A client."""
    # Extract client_id from the Bearer JWT (azp claim); fall back to the user's sub
    client_id = await get_client_id_from_request(request) or current_user.sub

    repo = _get_delivery_channel_repository(request)

    # Validate that all supplied group IDs exist
    if data.group_ids:
        from sqlalchemy import text as sa_text

        result = await db.execute(
            sa_text("SELECT id FROM user_groups WHERE id = ANY(:ids) AND deleted_at IS NULL"),
            {"ids": data.group_ids},
        )
        found_ids = {row["id"] for row in result.mappings().all()}
        missing = set(data.group_ids) - found_ids
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Group ID(s) not found: {sorted(missing)}",
            )

    channel = await repo.create_channel(db=db, actor=current_user, client_id=client_id, data=data)
    await db.commit()
    return channel


@router.get(
    "",
    response_model=DeliveryChannelListResponse,
    summary="List delivery channels visible to the caller.",
    description=(
        "For A2A clients (Bearer token with ``azp`` claim): returns only the channels "
        "registered by that client.\n\n"
        "For regular session-authenticated users: returns channels owned by groups the "
        "user is a member of (admins see all channels)."
    ),
)
async def list_channels(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> DeliveryChannelListResponse:
    """List delivery channels scoped to the authenticated caller."""
    repo = _get_delivery_channel_repository(request)

    client_id = await get_client_id_from_request(request)
    if client_id:
        # A2A client — return only its own channels
        channels = await repo.list_channels_for_client(db=db, client_id=client_id)
    else:
        # Human user — group-scoped (or all for admins)
        admin = is_admin_mode(request, current_user)
        channels = await repo.list_channels_for_user(db=db, user_id=current_user.id, is_admin=admin)

    return DeliveryChannelListResponse(channels=channels)


@router.patch(
    "/{channel_id}",
    response_model=DeliveryChannelResponse,
    summary="Update a delivery channel.",
    description=(
        "Partial update — only fields provided in the request body are changed.\n\n"
        "Allowed callers:\n"
        "- The A2A client that originally registered the channel (matching ``client_id``).\n"
        "- A user who is a group manager of at least one owning group.\n"
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

    await _require_channel_write_access(request, db, channel_id, owner_client_id, current_user)

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
        "Permanently removes a delivery channel and its group associations.\n\n"
        "Allowed callers: owning A2A client, group managers of the channel's groups, or admins."
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

    await _require_channel_write_access(request, db, channel_id, owner_client_id, current_user)

    deleted = await repo.delete_channel(db=db, actor=current_user, channel_id=channel_id)
    await db.commit()
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery channel not found")


async def _require_channel_write_access(
    request: Request,
    db: DbSession,
    channel_id: int,
    owner_client_id: str,
    current_user: User,
) -> None:
    """Raise 403 if the caller does not have write access to the channel.

    Access is granted when:
    - The Bearer token's ``client_id`` (``azp``) matches the channel's ``client_id``.
    - The user is a group manager of at least one of the channel's owning groups.
    - The user is an admin with admin mode enabled.
    """
    # 1. Owning A2A client
    caller_client_id = await get_client_id_from_request(request)
    if caller_client_id and caller_client_id == owner_client_id:
        return

    # 2. Admin
    if is_admin_mode(request, current_user):
        return

    # 3. Group manager of any owning group
    repo = _get_delivery_channel_repository(request)
    group_ids = await repo.get_channel_group_ids(db, channel_id)
    if group_ids:
        ug_service = _get_user_group_service(request)
        for gid in group_ids:
            if await ug_service.is_group_manager(db=db, group_id=gid, user_id=current_user.id):
                return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to modify this delivery channel.",
    )
