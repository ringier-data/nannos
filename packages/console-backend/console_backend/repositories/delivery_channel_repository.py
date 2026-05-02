"""Repository for delivery channels (push-notification webhook registrations)."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditEntityType
from ..models.delivery_channel import (
    DeliveryChannelCreate,
    DeliveryChannelResponse,
    DeliveryChannelUpdate,
)
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


def _row_to_response(row: Any, group_ids: list[int]) -> DeliveryChannelResponse:
    return DeliveryChannelResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        webhook_url=row["webhook_url"],
        client_id=row["client_id"],
        registered_by=row["registered_by"],
        group_ids=group_ids,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _fetch_group_ids(db: AsyncSession, channel_id: int) -> list[int]:
    """Return the list of group IDs associated with a delivery channel."""
    result = await db.execute(
        text("SELECT user_group_id FROM delivery_channel_groups WHERE delivery_channel_id = :id"),
        {"id": channel_id},
    )
    return [r["user_group_id"] for r in result.mappings().all()]


class DeliveryChannelRepository(AuditedRepository):
    """CRUD repository for delivery_channels with audit logging."""

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.DELIVERY_CHANNEL,
            table_name="delivery_channels",
        )

    async def create_channel(
        self,
        db: AsyncSession,
        actor: User,
        client_id: str,
        data: DeliveryChannelCreate,
    ) -> DeliveryChannelResponse:
        """Insert a delivery channel and its group associations."""
        now = datetime.now(timezone.utc)
        channel_id: int = await self.create(
            db=db,
            actor=actor,
            fields={
                "name": data.name,
                "description": data.description,
                "webhook_url": data.webhook_url,
                "secret": data.secret,
                "client_id": client_id,
                "registered_by": actor.sub,
                "created_at": now,
                "updated_at": now,
            },
        )

        # Insert group associations
        for gid in data.group_ids:
            await db.execute(
                text(
                    "INSERT INTO delivery_channel_groups (delivery_channel_id, user_group_id) "
                    "VALUES (:cid, :gid) ON CONFLICT DO NOTHING"
                ),
                {"cid": channel_id, "gid": gid},
            )

        row = await self._get_row(db, channel_id)
        assert row is not None
        return _row_to_response(row, list(data.group_ids))

    async def _get_row(self, db: AsyncSession, channel_id: int) -> Any | None:
        result = await db.execute(
            text("SELECT * FROM delivery_channels WHERE id = :id"),
            {"id": channel_id},
        )
        return result.mappings().first()

    async def get_channel_by_id(
        self,
        db: AsyncSession,
        channel_id: int,
        include_secret: bool = False,
    ) -> DeliveryChannelResponse | None:
        """Fetch a single channel by ID.  Secret is included only when explicitly requested (engine use)."""
        row = await self._get_row(db, channel_id)
        if row is None:
            return None
        group_ids = await _fetch_group_ids(db, channel_id)
        resp = _row_to_response(row, group_ids)
        if include_secret:
            # Attach secret as a transient attribute for the scheduler engine
            object.__setattr__(resp, "_secret", row["secret"])
        return resp

    async def get_channel_secret(self, db: AsyncSession, channel_id: int) -> str | None:
        """Return just the secret for a channel (used by the scheduler engine at dispatch time)."""
        result = await db.execute(
            text("SELECT secret, webhook_url FROM delivery_channels WHERE id = :id"),
            {"id": channel_id},
        )
        row = result.mappings().first()
        return None if row is None else row["secret"]

    async def get_channel_for_dispatch(self, db: AsyncSession, channel_id: int) -> dict | None:
        """Return the webhook_url and secret needed by the scheduler engine.

        Returns a dict with keys ``webhook_url`` and ``secret``, or None if not found.
        """
        result = await db.execute(
            text("SELECT webhook_url, secret FROM delivery_channels WHERE id = :id"),
            {"id": channel_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_channels_for_user(
        self,
        db: AsyncSession,
        user_id: str,
        is_admin: bool = False,
    ) -> list[DeliveryChannelResponse]:
        """Return channels visible to a user via group membership (or all if admin)."""
        if is_admin:
            result = await db.execute(text("SELECT * FROM delivery_channels ORDER BY name"))
            rows = result.mappings().all()
        else:
            result = await db.execute(
                text("""
                    SELECT DISTINCT dc.*
                    FROM delivery_channels dc
                    JOIN delivery_channel_groups dcg ON dcg.delivery_channel_id = dc.id
                    JOIN user_group_members ugm ON ugm.user_group_id = dcg.user_group_id
                    WHERE ugm.user_id = :user_id
                    ORDER BY dc.name
                """),
                {"user_id": user_id},
            )
            rows = result.mappings().all()

        channels = []
        for row in rows:
            group_ids = await _fetch_group_ids(db, row["id"])
            channels.append(_row_to_response(row, group_ids))
        return channels

    async def list_channels_for_client(
        self,
        db: AsyncSession,
        client_id: str,
    ) -> list[DeliveryChannelResponse]:
        """Return all channels registered by a given Keycloak client ID."""
        result = await db.execute(
            text("SELECT * FROM delivery_channels WHERE client_id = :client_id ORDER BY name"),
            {"client_id": client_id},
        )
        rows = result.mappings().all()
        channels = []
        for row in rows:
            group_ids = await _fetch_group_ids(db, row["id"])
            channels.append(_row_to_response(row, group_ids))
        return channels

    async def update_channel(
        self,
        db: AsyncSession,
        actor: User,
        channel_id: int,
        data: DeliveryChannelUpdate,
    ) -> DeliveryChannelResponse | None:
        """Partial update of a delivery channel.  Only provided fields are changed."""
        row = await self._get_row(db, channel_id)
        if row is None:
            return None

        fields: dict = {"updated_at": datetime.now(timezone.utc)}
        for attr in ("name", "description", "webhook_url", "secret"):
            val = getattr(data, attr)
            if val is not None:
                fields[attr] = val

        if len(fields) > 1:  # more than just updated_at
            await self.update(db=db, actor=actor, entity_id=channel_id, fields=fields)

        # Replace group associations if group_ids was provided
        if data.group_ids is not None:
            await db.execute(
                text("DELETE FROM delivery_channel_groups WHERE delivery_channel_id = :id"),
                {"id": channel_id},
            )
            for gid in data.group_ids:
                await db.execute(
                    text(
                        "INSERT INTO delivery_channel_groups (delivery_channel_id, user_group_id) "
                        "VALUES (:cid, :gid) ON CONFLICT DO NOTHING"
                    ),
                    {"cid": channel_id, "gid": gid},
                )

        updated_row = await self._get_row(db, channel_id)
        assert updated_row is not None
        group_ids = await _fetch_group_ids(db, channel_id)
        return _row_to_response(updated_row, group_ids)

    async def delete_channel(self, db: AsyncSession, actor: User, channel_id: int) -> bool:
        """Hard-delete a delivery channel (CASCADE removes group associations).

        Returns True if the channel existed and was deleted, False if not found.
        """
        row = await self._get_row(db, channel_id)
        if row is None:
            return False
        await self.delete(db=db, actor=actor, entity_id=channel_id, soft=False)
        return True

    async def get_owner_client_id(self, db: AsyncSession, channel_id: int) -> str | None:
        """Return the client_id that owns a channel, or None if not found."""
        row = await self._get_row(db, channel_id)
        return row["client_id"] if row else None

    async def get_channel_group_ids(self, db: AsyncSession, channel_id: int) -> list[int]:
        """Return the group IDs associated with a channel."""
        return await _fetch_group_ids(db, channel_id)
