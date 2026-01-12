"""Audit service for logging all changes."""

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType, AuditLog

logger = logging.getLogger(__name__)


class AuditService:
    """Service for managing audit logs."""

    async def log_action(
        self,
        db: AsyncSession,
        actor_sub: str,
        entity_type: AuditEntityType,
        entity_id: str,
        action: AuditAction,
        changes: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Log an audit action.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            entity_type: Type of entity being modified
            entity_id: ID of the entity being modified
            action: The action being performed
            changes: Dictionary with 'before' and 'after' state

        Returns:
            The created audit log entry
        """
        query = text("""
            INSERT INTO audit_logs (actor_sub, entity_type, entity_id, action, changes)
            VALUES (:actor_sub, :entity_type, :entity_id, :action, :changes)
            RETURNING id, actor_sub, entity_type, entity_id, action, changes, created_at
        """)

        try:
            result = await db.execute(
                query,
                {
                    "actor_sub": actor_sub,
                    "entity_type": entity_type.value,
                    "entity_id": str(entity_id),
                    "action": action.value,
                    "changes": json.dumps(changes or {}),
                },
            )
            row = result.mappings().first()

            if row is None:
                raise RuntimeError("Failed to create audit log entry")

            logger.info(f"Audit: {actor_sub} performed {action.value} on {entity_type.value}:{entity_id}")

            return AuditLog(
                id=row["id"],
                actor_sub=row["actor_sub"],
                entity_type=AuditEntityType(row["entity_type"]),
                entity_id=row["entity_id"],
                action=AuditAction(row["action"]),
                changes=row["changes"],
                created_at=row["created_at"],
            )
        except Exception as e:
            logger.error(f"Failed to log audit action: {e}")
            raise

    async def list_logs(
        self,
        db: AsyncSession,
        page: int = 1,
        limit: int = 50,
        entity_type: AuditEntityType | None = None,
        entity_id: str | None = None,
        actor_sub: str | None = None,
        action: AuditAction | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> tuple[list[AuditLog], int]:
        """List audit logs with filtering.

        Args:
            db: Database session
            page: Page number (1-indexed)
            limit: Items per page
            entity_type: Filter by entity type
            entity_id: Filter by entity ID
            actor_sub: Filter by actor sub
            action: Filter by action
            from_date: Filter by start date
            to_date: Filter by end date

        Returns:
            Tuple of (audit logs, total count)
        """
        # Build WHERE clauses
        conditions = []
        params: dict[str, Any] = {
            "limit": limit,
            "offset": (page - 1) * limit,
        }

        if entity_type:
            conditions.append("entity_type = :entity_type")
            params["entity_type"] = entity_type.value

        if entity_id:
            conditions.append("entity_id = :entity_id")
            params["entity_id"] = entity_id

        if actor_sub:
            conditions.append("actor_sub = :actor_sub")
            params["actor_sub"] = actor_sub

        if action:
            conditions.append("action = :action")
            params["action"] = action.value

        if from_date:
            conditions.append("created_at >= :from_date")
            params["from_date"] = from_date

        if to_date:
            conditions.append("created_at <= :to_date")
            params["to_date"] = to_date

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        # Count query
        count_query = text(f"""
            SELECT COUNT(*) as total
            FROM audit_logs
            {where_clause}
        """)

        # Data query
        data_query = text(f"""
            SELECT id, actor_sub, entity_type, entity_id, action, changes, created_at
            FROM audit_logs
            {where_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset
        """)

        try:
            # Get total count
            count_result = await db.execute(count_query, params)
            total = count_result.scalar() or 0

            # Get data
            result = await db.execute(data_query, params)
            rows = result.mappings().all()

            logs = [
                AuditLog(
                    id=row["id"],
                    actor_sub=row["actor_sub"],
                    entity_type=AuditEntityType(row["entity_type"]),
                    entity_id=row["entity_id"],
                    action=AuditAction(row["action"]),
                    changes=row["changes"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

            return logs, total
        except Exception as e:
            logger.error(f"Failed to list audit logs: {e}")
            raise
