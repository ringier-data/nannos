"""Repository for token-broker client registrations (audited admin data)."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditEntityType
from ..models.broker import BrokerClient, BrokerClientCreate, BrokerClientUpdate
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


def _row_to_client(row: Any) -> BrokerClient:
    return BrokerClient(
        id=row["id"],
        client_id=row["client_id"],
        name=row["name"],
        description=row["description"],
        redirect_uris=list(row["redirect_uris"] or []),
        enabled=row["enabled"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class BrokerClientRepository(AuditedRepository):
    """CRUD repository for broker_clients with audit logging."""

    def __init__(self) -> None:
        super().__init__(entity_type=AuditEntityType.BROKER_CLIENT, table_name="broker_clients")

    async def create_client(self, db: AsyncSession, actor: User, data: BrokerClientCreate) -> BrokerClient:
        """Register a broker client. A client_id that is already registered is a ValueError."""
        now = datetime.now(timezone.utc)
        fields: dict[str, Any] = {
            "client_id": data.client_id,
            "name": data.name,
            "description": data.description,
            "redirect_uris": data.redirect_uris,
            "enabled": data.enabled,
            "created_by": actor.id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            # SAVEPOINT: a duplicate client_id rolls back only this INSERT, so the caller's
            # transaction stays usable for the 409 it answers with.
            async with db.begin_nested():
                client_pk: int = await self.create(db=db, actor=actor, fields=fields)
        except IntegrityError as e:
            raise ValueError(f"Broker client {data.client_id!r} is already registered") from e
        row = await self._get_row(db, client_pk)
        assert row is not None
        return _row_to_client(row)

    async def _get_row(self, db: AsyncSession, client_pk: int) -> Any | None:
        result = await db.execute(text("SELECT * FROM broker_clients WHERE id = :id"), {"id": client_pk})
        return result.mappings().first()

    async def get_by_id(self, db: AsyncSession, client_pk: int) -> BrokerClient | None:
        row = await self._get_row(db, client_pk)
        return _row_to_client(row) if row else None

    async def get_by_client_id(self, db: AsyncSession, client_id: str) -> BrokerClient | None:
        result = await db.execute(
            text("SELECT * FROM broker_clients WHERE client_id = :client_id"),
            {"client_id": client_id},
        )
        row = result.mappings().first()
        return _row_to_client(row) if row else None

    async def list_all(self, db: AsyncSession) -> list[BrokerClient]:
        result = await db.execute(text("SELECT * FROM broker_clients ORDER BY client_id"))
        return [_row_to_client(row) for row in result.mappings().all()]

    async def update_client(
        self, db: AsyncSession, actor: User, client_pk: int, data: BrokerClientUpdate
    ) -> BrokerClient | None:
        """Partial update. Returns None when the client does not exist."""
        if await self._get_row(db, client_pk) is None:
            return None
        fields: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        for attr in ("name", "redirect_uris", "enabled"):
            value = getattr(data, attr)
            if value is not None:
                fields[attr] = value
        # The one nullable field: an explicit null clears it, an omitted field keeps it.
        if "description" in data.model_fields_set:
            fields["description"] = data.description
        if len(fields) > 1:  # more than just updated_at
            await self.update(db=db, actor=actor, entity_id=client_pk, fields=fields)
        row = await self._get_row(db, client_pk)
        assert row is not None
        return _row_to_client(row)

    async def delete_client(self, db: AsyncSession, actor: User, client_pk: int) -> bool:
        """Hard-delete a broker client (its pending logins go with it). False when not found."""
        if await self._get_row(db, client_pk) is None:
            return False
        await self.delete(db=db, actor=actor, entity_id=client_pk, soft=False)
        return True
