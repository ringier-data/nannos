"""Base repository with automatic audit logging."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..services.audit_service import audit_service

logger = logging.getLogger(__name__)


def _serialize_for_audit(data: dict[str, Any]) -> dict[str, Any]:
    """Serialize dictionary for audit logging, converting datetimes to ISO strings."""
    result = {}
    for key, value in data.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            result[key] = [item.isoformat() if isinstance(item, datetime) else item for item in value]
        else:
            result[key] = value
    return result


class AuditedRepository:
    """
    Base class for entity repositories with automatic audit logging.

    This class provides common CRUD operations with built-in audit trail:
    - create(): INSERT with automatic audit
    - update(): UPDATE with before/after state tracking
    - delete(): Soft delete with audit

    Subclasses should:
    1. Call super().__init__() with entity_type and table_name
    2. Add domain-specific operations
    3. Use audit_service.log_action() for custom operations
    """

    def __init__(self, entity_type: AuditEntityType, table_name: str):
        """
        Initialize repository.

        Args:
            entity_type: The audit entity type for this repository
            table_name: The primary database table name
        """
        self.entity_type = entity_type
        self.table_name = table_name

    async def create(
        self,
        db: AsyncSession,
        actor_sub: str,
        fields: dict[str, Any],
        returning: str = "id",
    ) -> Any:
        """
        Generic CREATE with automatic audit logging.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            fields: Dictionary of column names and values to insert
            returning: Column name(s) to return (comma-separated)

        Returns:
            The value(s) from the RETURNING clause

        Example:
            entity_id = await repo.create(
                db=db,
                actor_sub=user.sub,
                fields={"name": "Test", "owner_id": "123"},
                returning="id"
            )
        """
        columns = ", ".join(fields.keys())
        placeholders = ", ".join(f":{key}" for key in fields.keys())

        query = text(f"""
            INSERT INTO {self.table_name} ({columns})
            VALUES ({placeholders})
            RETURNING {returning}
        """)

        try:
            result = await db.execute(query, fields)
            row = result.mappings().first()

            if row is None:
                raise RuntimeError(f"Failed to create entity in {self.table_name}")

            # Extract entity ID from first column in RETURNING
            entity_id = row[returning.split(",")[0].strip()]

            # Auto-audit
            await audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(entity_id),
                action=AuditAction.CREATE,
                changes={"after": _serialize_for_audit(fields)},
            )

            logger.info(f"Created {self.entity_type.value} {entity_id} by {actor_sub}")

            return entity_id

        except Exception as e:
            logger.error(f"Failed to create {self.entity_type.value}: {e}")
            raise

    async def update(
        self,
        db: AsyncSession,
        actor_sub: str,
        entity_id: str | int,
        fields: dict[str, Any],
        fetch_before: bool = True,
        custom_action: AuditAction | None = None,
    ) -> None:
        """
        Generic UPDATE with automatic audit logging.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            entity_id: ID of the entity to update
            fields: Dictionary of column names and new values
            fetch_before: Whether to fetch current state for audit (default True)
            custom_action: Custom audit action (default: AuditAction.UPDATE)

        Example:
            await repo.update(
                db=db,
                actor_sub=user.sub,
                entity_id=123,
                fields={"name": "Updated Name", "updated_at": now}
            )
        """
        before_state = None
        if fetch_before:
            # Fetch current state for audit
            select_query = text(f"SELECT * FROM {self.table_name} WHERE id = :id")
            result = await db.execute(select_query, {"id": entity_id})
            row = result.mappings().first()
            if row:
                before_state = dict(row)

        # Build UPDATE
        set_clause = ", ".join(f"{key} = :{key}" for key in fields.keys())
        query = text(f"""
            UPDATE {self.table_name}
            SET {set_clause}
            WHERE id = :id
        """)

        try:
            await db.execute(query, {**fields, "id": entity_id})

            # Auto-audit - serialize datetime objects
            changes = {}
            if before_state:
                changes["before"] = _serialize_for_audit(
                    {k: before_state[k] for k in fields.keys() if k in before_state}
                )
                changes["after"] = _serialize_for_audit(fields)
            else:
                changes["after"] = _serialize_for_audit(fields)

            action = custom_action or AuditAction.UPDATE

            await audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(entity_id),
                action=action,
                changes=changes,
            )

            logger.info(f"Updated {self.entity_type.value} {entity_id} by {actor_sub}")

        except Exception as e:
            logger.error(f"Failed to update {self.entity_type.value} {entity_id}: {e}")
            raise

    async def delete(
        self,
        db: AsyncSession,
        actor_sub: str,
        entity_id: str | int,
        soft: bool = True,
    ) -> None:
        """
        Generic DELETE with automatic audit logging.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            entity_id: ID of the entity to delete
            soft: Whether to soft delete (set deleted_at) or hard delete

        Example:
            await repo.delete(
                db=db,
                actor_sub=user.sub,
                entity_id=123,
                soft=True  # Soft delete
            )
        """
        try:
            if soft:
                now = datetime.now(timezone.utc)
                query = text(f"""
                    UPDATE {self.table_name}
                    SET deleted_at = :now, updated_at = :now
                    WHERE id = :id
                """)
                await db.execute(query, {"id": entity_id, "now": now})
            else:
                query = text(f"DELETE FROM {self.table_name} WHERE id = :id")
                await db.execute(query, {"id": entity_id})

            # Auto-audit
            await audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(entity_id),
                action=AuditAction.DELETE,
                changes={"soft_delete": soft},
            )

            logger.info(f"Deleted {self.entity_type.value} {entity_id} by {actor_sub}")

        except Exception as e:
            logger.error(f"Failed to delete {self.entity_type.value} {entity_id}: {e}")
            raise
