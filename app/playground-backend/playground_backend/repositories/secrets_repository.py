"""Repository for secrets operations with automatic audit logging."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..services.audit_service import audit_service
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class SecretsRepository(AuditedRepository):
    """Repository for secrets operations with automatic audit logging."""

    def __init__(self):
        super().__init__(entity_type=AuditEntityType.SECRET, table_name="secrets")

    async def update_permissions(
        self,
        db: AsyncSession,
        actor_sub: str,
        secret_id: int,
        group_permissions: list[dict],
    ) -> None:
        """
        Update group permissions for secret.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            secret_id: Secret ID
            group_permissions: List of dicts with user_group_id and permissions
        """
        try:
            # Fetch before state
            before_query = text("""
                SELECT user_group_id, permissions
                FROM secret_permissions
                WHERE secret_id = :id
            """)
            result = await db.execute(before_query, {"id": secret_id})
            rows = result.mappings().all()
            before_perms = [{"user_group_id": row["user_group_id"], "permissions": row["permissions"]} for row in rows]

            # Delete existing
            await db.execute(
                text("DELETE FROM secret_permissions WHERE secret_id = :id"),
                {"id": secret_id},
            )

            # Insert new
            for perm in group_permissions:
                await db.execute(
                    text("""
                        INSERT INTO secret_permissions (secret_id, user_group_id, permissions)
                        VALUES (:secret_id, :user_group_id, :permissions)
                    """),
                    {
                        "secret_id": secret_id,
                        "user_group_id": perm["user_group_id"],
                        "permissions": perm["permissions"],
                    },
                )

            # Custom audit for permission change
            await audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(secret_id),
                action=AuditAction.PERMISSION_UPDATE,
                changes={
                    "before": {"permissions": before_perms},
                    "after": {"permissions": group_permissions},
                },
            )

            logger.info(f"Updated permissions for secret {secret_id} by {actor_sub}")

        except Exception as e:
            logger.error(f"Failed to update permissions for secret {secret_id}: {e}")
            raise


# Module-level singleton
secrets_repository = SecretsRepository()
