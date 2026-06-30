"""Repository for delivery channels (push-notification webhook registrations)."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


def _row_to_response(row: Any) -> DeliveryChannelResponse:
    return DeliveryChannelResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        webhook_url=row["webhook_url"],
        client_id=row["client_id"],
        registered_by=row["registered_by"],
        installation_id=row["installation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class DeliveryChannelRepository(AuditedRepository):
    """CRUD repository for delivery_channels with audit logging."""

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.DELIVERY_CHANNEL,
            table_name="delivery_channels",
            # The webhook signing secret must never land in the audit trail.
            sensitive_fields={"secret"},
        )

    async def create_channel(
        self,
        db: AsyncSession,
        actor: User,
        client_id: str,
        data: DeliveryChannelCreate,
    ) -> DeliveryChannelResponse:
        """Insert a delivery channel."""
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
                "installation_id": data.installation_id,
                "created_at": now,
                "updated_at": now,
            },
        )

        row = await self._get_row(db, channel_id)
        assert row is not None
        return _row_to_response(row)

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
        resp = _row_to_response(row)
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

    async def list_all_channels(self, db: AsyncSession) -> list[DeliveryChannelResponse]:
        """Return all delivery channels.

        Channel visibility is no longer scoped by user groups: every authenticated
        console user can see all channels (the web-console is a secondary interface;
        installation-scoped filtering happens at the agent/MCP layer, not here).

        TODO(ADR): This assumes the web-console is a SINGLE INTERNAL trust domain —
        every console user is a trusted operator who may see all tenants' channels
        (name, webhook_url, client_id; secrets are never returned here). This is an
        accepted trade-off while the console is internal-only. If the console ever
        serves mutually-untrusted tenants, this endpoint must be re-scoped (by
        installation/client_id) because the agent/MCP-layer installation filter
        protects only the agent path, not a direct console GET.
        """
        result = await db.execute(text("SELECT * FROM delivery_channels ORDER BY name"))
        return [_row_to_response(row) for row in result.mappings().all()]

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
        return [_row_to_response(row) for row in result.mappings().all()]

    async def list_channels_for_installation(
        self,
        db: AsyncSession,
        installation_id: str,
    ) -> list[DeliveryChannelResponse]:
        """Return all channels tagged with a given installation_id (across all clients).

        This is the installation-scoped view the orchestrator uses to pick a notification
        target for the calling tenant: the installation comes from the request context, not
        from the client_id, because the orchestrator calls console-backend under its own
        Keycloak client — not the bot's.
        """
        result = await db.execute(
            text("SELECT * FROM delivery_channels WHERE installation_id = :installation_id ORDER BY name"),
            {"installation_id": installation_id},
        )
        return [_row_to_response(row) for row in result.mappings().all()]

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

        updated_row = await self._get_row(db, channel_id)
        assert updated_row is not None
        return _row_to_response(updated_row)

    async def delete_channel(self, db: AsyncSession, actor: User, channel_id: int) -> bool:
        """Hard-delete a delivery channel.

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

    async def get_by_installation(
        self,
        db: AsyncSession,
        client_id: str,
        installation_id: str,
    ) -> DeliveryChannelResponse | None:
        """Look up a channel by its (client_id, installation_id) idempotency key."""
        result = await db.execute(
            text(
                "SELECT * FROM delivery_channels "
                "WHERE client_id = :client_id AND installation_id = :installation_id"
            ),
            {"client_id": client_id, "installation_id": installation_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return _row_to_response(row)

    async def upsert_channel_by_installation(
        self,
        db: AsyncSession,
        actor: User,
        client_id: str,
        data: DeliveryChannelCreate,
    ) -> tuple[DeliveryChannelResponse, bool]:
        """Idempotently create or update a channel keyed by ``(client_id, installation_id)``.

        Returns ``(channel, created)`` where ``created`` is True when a new row was inserted.
        On update, the registrar-owned fields (name, description, webhook_url, secret) are
        overwritten. ``installation_id`` is required on ``DeliveryChannelCreate``; the guard
        below is a defensive internal invariant for non-validated callers.
        """
        if data.installation_id is None:
            raise ValueError("installation_id required for upsert")

        existing = await self.get_by_installation(db, client_id, data.installation_id)
        if existing is None:
            # No row yet — try to insert. A concurrent registration of the same
            # (client_id, installation_id) (e.g. several bot replicas booting at once)
            # can win the race between the SELECT above and this INSERT, at which point
            # delivery_channels_client_installation_uidx raises IntegrityError. Wrap the
            # insert in a SAVEPOINT so the conflict rolls back only the failed INSERT,
            # not the whole request transaction, and fall through to update the row the
            # winner committed — converging all replicas on one channel.
            try:
                async with db.begin_nested():
                    created = await self.create_channel(
                        db=db, actor=actor, client_id=client_id, data=data
                    )
                return created, True
            except IntegrityError:
                existing = await self.get_by_installation(db, client_id, data.installation_id)
                if existing is None:
                    raise

        update = DeliveryChannelUpdate(
            name=data.name,
            description=data.description,
            webhook_url=data.webhook_url,
            secret=data.secret,
        )
        updated = await self.update_channel(db=db, actor=actor, channel_id=existing.id, data=update)
        if updated is None:
            raise RuntimeError(
                f"upsert: channel {existing.id} disappeared during update "
                f"(client_id={client_id}, installation_id={data.installation_id})"
            )
        return updated, False
