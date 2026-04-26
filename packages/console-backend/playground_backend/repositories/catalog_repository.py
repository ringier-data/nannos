"""Repository for catalog operations with automatic audit logging."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.catalog import (
    Catalog,
    CatalogFile,
    CatalogOwner,
    CatalogPage,
    CatalogSyncJob,
)
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class CatalogRepository(AuditedRepository):
    """Repository for catalog CRUD with automatic audit logging."""

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.CATALOG,
            table_name="catalogs",
        )

    # --- Catalog CRUD ---

    async def get_catalog(self, db: AsyncSession, catalog_id: str) -> Catalog | None:
        """Get a single catalog by ID with owner info."""
        result = await db.execute(
            text("""
                SELECT c.*,
                       u.id AS owner_id,
                       CONCAT(u.first_name, ' ', u.last_name) AS owner_name,
                       u.email AS owner_email,
                       EXISTS(
                           SELECT 1 FROM catalog_connections cc
                           WHERE cc.catalog_id = c.id AND cc.status = 'active'
                       ) AS has_connection,
                       COALESCE(ps.total_pages, 0) AS total_pages,
                       COALESCE(ps.indexed_pages, 0) AS indexed_pages
                FROM catalogs c
                JOIN users u ON c.owner_user_id = u.id
                LEFT JOIN (
                    SELECT catalog_id,
                           COUNT(*) AS total_pages,
                           COUNT(indexed_at) AS indexed_pages
                    FROM catalog_pages
                    GROUP BY catalog_id
                ) ps ON ps.catalog_id = c.id
                WHERE c.id = :id
            """),
            {"id": catalog_id},
        )
        row = result.mappings().first()
        if not row:
            return None
        return self._map_catalog(row)

    async def get_accessible_catalogs(
        self,
        db: AsyncSession,
        user_id: str,
        is_admin: bool = False,
    ) -> list[Catalog]:
        """Get catalogs accessible to user (owned + group-shared + admin-all)."""
        if is_admin:
            result = await db.execute(
                text("""
                    SELECT c.*,
                           u.id AS owner_id,
                           CONCAT(u.first_name, ' ', u.last_name) AS owner_name,
                           u.email AS owner_email,
                           EXISTS(
                               SELECT 1 FROM catalog_connections cc
                               WHERE cc.catalog_id = c.id AND cc.status = 'active'
                           ) AS has_connection,
                           COALESCE(ps.total_pages, 0) AS total_pages,
                           COALESCE(ps.indexed_pages, 0) AS indexed_pages
                    FROM catalogs c
                    JOIN users u ON c.owner_user_id = u.id
                    LEFT JOIN (
                        SELECT catalog_id,
                               COUNT(*) AS total_pages,
                               COUNT(indexed_at) AS indexed_pages
                        FROM catalog_pages
                        GROUP BY catalog_id
                    ) ps ON ps.catalog_id = c.id
                    ORDER BY c.updated_at DESC
                """),
            )
        else:
            result = await db.execute(
                text("""
                    SELECT DISTINCT ON (c.id) c.*,
                           u.id AS owner_id,
                           CONCAT(u.first_name, ' ', u.last_name) AS owner_name,
                           u.email AS owner_email,
                           EXISTS(
                               SELECT 1 FROM catalog_connections cc
                               WHERE cc.catalog_id = c.id AND cc.status = 'active'
                           ) AS has_connection,
                           COALESCE(ps.total_pages, 0) AS total_pages,
                           COALESCE(ps.indexed_pages, 0) AS indexed_pages
                    FROM catalogs c
                    JOIN users u ON c.owner_user_id = u.id
                    LEFT JOIN (
                        SELECT catalog_id,
                               COUNT(*) AS total_pages,
                               COUNT(indexed_at) AS indexed_pages
                        FROM catalog_pages
                        GROUP BY catalog_id
                    ) ps ON ps.catalog_id = c.id
                    LEFT JOIN catalog_permissions cp ON cp.catalog_id = c.id
                    LEFT JOIN user_group_members ugm ON ugm.user_group_id = cp.user_group_id
                    WHERE c.owner_user_id = :user_id
                       OR ugm.user_id = :user_id
                    ORDER BY c.id, c.updated_at DESC
                """),
                {"user_id": user_id},
            )
        rows = result.mappings().all()
        return [self._map_catalog(row) for row in rows]

    # --- Catalog Files ---

    async def get_catalog_files(
        self,
        db: AsyncSession,
        catalog_id: str,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
    ) -> tuple[list[CatalogFile], int]:
        """Get paginated files in a catalog with indexed page counts."""
        conditions = ["cf.catalog_id = :catalog_id"]
        params: dict[str, Any] = {
            "catalog_id": catalog_id,
            "limit": limit,
            "offset": offset,
        }

        if search:
            conditions.append("(cf.source_file_name ILIKE :search OR cf.folder_path ILIKE :search)")
            params["search"] = f"%{search}%"

        if status:
            conditions.append("cf.sync_status = :status")
            params["status"] = status

        where = " AND ".join(conditions)

        result = await db.execute(
            text(f"""
                SELECT cf.*,
                       COALESCE(idx.indexed_pages, 0) AS indexed_pages,
                       COUNT(*) OVER() AS total_count
                FROM catalog_files cf
                LEFT JOIN (
                    SELECT file_id, COUNT(*) AS indexed_pages
                    FROM catalog_pages
                    WHERE indexed_at IS NOT NULL
                    GROUP BY file_id
                ) idx ON idx.file_id = cf.id
                WHERE {where}
                ORDER BY cf.folder_path, cf.source_file_name
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        rows = result.mappings().all()
        total = rows[0]["total_count"] if rows else 0
        files = []
        for row in rows:
            row_dict = self._stringify_uuids(dict(row))
            row_dict.pop("total_count", None)
            files.append(CatalogFile(**row_dict))
        return files, total

    async def get_file_pages(
        self,
        db: AsyncSession,
        file_id: str,
    ) -> list[CatalogPage]:
        """Get all pages for a specific file."""
        result = await db.execute(
            text("""
                SELECT * FROM catalog_pages
                WHERE file_id = :file_id
                ORDER BY page_number
            """),
            {"file_id": file_id},
        )
        return [CatalogPage(**self._stringify_uuids(dict(row))) for row in result.mappings().all()]

    async def get_catalog_pages(
        self,
        db: AsyncSession,
        catalog_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CatalogPage], int]:
        """Get paginated pages for a catalog."""
        count_result = await db.execute(
            text("SELECT COUNT(*) FROM catalog_pages WHERE catalog_id = :catalog_id"),
            {"catalog_id": catalog_id},
        )
        total = count_result.scalar() or 0

        result = await db.execute(
            text("""
                SELECT * FROM catalog_pages
                WHERE catalog_id = :catalog_id
                ORDER BY file_id, page_number
                LIMIT :limit OFFSET :offset
            """),
            {"catalog_id": catalog_id, "limit": limit, "offset": offset},
        )
        pages = [CatalogPage(**self._stringify_uuids(dict(row))) for row in result.mappings().all()]
        return pages, total

    async def update_file_indexing(
        self,
        db: AsyncSession,
        file_id: str,
        indexing_excluded: bool,
    ) -> CatalogFile | None:
        """Toggle indexing exclusion for a file."""
        result = await db.execute(
            text("""
                UPDATE catalog_files
                SET indexing_excluded = :excluded, updated_at = :now
                WHERE id = :file_id
                RETURNING *
            """),
            {
                "file_id": file_id,
                "excluded": indexing_excluded,
                "now": datetime.now(timezone.utc),
            },
        )
        row = result.mappings().first()
        if not row:
            return None
        return CatalogFile(**self._stringify_uuids(dict(row)))

    # --- Sync Jobs ---

    async def create_sync_job(
        self,
        db: AsyncSession,
        catalog_id: str,
    ) -> str:
        """Create a new sync job, returning its ID."""
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                INSERT INTO catalog_sync_jobs (catalog_id, status, started_at, created_at)
                VALUES (:catalog_id, 'pending', :now, :now)
                RETURNING id
            """),
            {"catalog_id": catalog_id, "now": now},
        )
        row = result.mappings().first()
        if row is None:
            raise RuntimeError("Failed to create sync job")
        return str(row["id"])

    async def create_sync_job_atomic(
        self,
        db: AsyncSession,
        catalog_id: str,
    ) -> str | None:
        """Atomically create a sync job only if no active job exists.

        Uses INSERT ... WHERE NOT EXISTS to eliminate the TOCTOU race where
        two concurrent callers both see "no active job" and both create one.
        Returns the new job ID, or ``None`` if an active job already exists.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                INSERT INTO catalog_sync_jobs (catalog_id, status, started_at, created_at)
                SELECT :catalog_id, 'pending', :now, :now
                WHERE NOT EXISTS (
                    SELECT 1 FROM catalog_sync_jobs
                    WHERE catalog_id = :catalog_id
                      AND status IN ('pending', 'running', 'paused', 'cancelling')
                )
                RETURNING id
            """),
            {"catalog_id": catalog_id, "now": now},
        )
        row = result.mappings().first()
        return str(row["id"]) if row else None

    async def get_sync_job(
        self,
        db: AsyncSession,
        job_id: str,
    ) -> CatalogSyncJob | None:
        """Get a sync job by ID."""
        result = await db.execute(
            text("SELECT * FROM catalog_sync_jobs WHERE id = :id"),
            {"id": job_id},
        )
        row = result.mappings().first()
        if not row:
            return None
        return self._row_to_sync_job(row)

    async def get_latest_sync_job(
        self,
        db: AsyncSession,
        catalog_id: str,
    ) -> CatalogSyncJob | None:
        """Get the most recent sync job for a catalog."""
        result = await db.execute(
            text("""
                SELECT * FROM catalog_sync_jobs
                WHERE catalog_id = :catalog_id
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"catalog_id": catalog_id},
        )
        row = result.mappings().first()
        if not row:
            return None
        return self._row_to_sync_job(row)

    @staticmethod
    def _row_to_sync_job(row) -> CatalogSyncJob:
        """Convert a DB row to CatalogSyncJob, casting UUID fields to str."""
        data = dict(row)
        data["id"] = str(data["id"])
        data["catalog_id"] = str(data["catalog_id"])
        # error_details may be stored as a JSON string; parse it for Pydantic
        if isinstance(data.get("error_details"), str):
            data["error_details"] = json.loads(data["error_details"])
        return CatalogSyncJob(**data)

    async def update_sync_job(
        self,
        db: AsyncSession,
        job_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Update sync job fields."""
        set_clause = ", ".join(f"{key} = :{key}" for key in fields.keys())
        await db.execute(
            text(f"UPDATE catalog_sync_jobs SET {set_clause} WHERE id = :id"),
            {**fields, "id": job_id},
        )

    # --- Permissions ---

    async def get_permissions(
        self,
        db: AsyncSession,
        catalog_id: str,
    ) -> list[dict[str, Any]]:
        """Get all permission entries for a catalog."""
        result = await db.execute(
            text("""
                SELECT cp.*, ug.name AS group_name
                FROM catalog_permissions cp
                JOIN user_groups ug ON cp.user_group_id = ug.id
                WHERE cp.catalog_id = :catalog_id
                ORDER BY ug.name
            """),
            {"catalog_id": catalog_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def set_permissions(
        self,
        db: AsyncSession,
        actor: User,
        catalog_id: str,
        permissions: list[dict[str, Any]],
    ) -> None:
        """Replace all permissions for a catalog."""
        # Delete existing permissions
        await db.execute(
            text("DELETE FROM catalog_permissions WHERE catalog_id = :catalog_id"),
            {"catalog_id": catalog_id},
        )
        # Insert new permissions
        for perm in permissions:
            await db.execute(
                text("""
                    INSERT INTO catalog_permissions (catalog_id, user_group_id, permissions)
                    VALUES (:catalog_id, :user_group_id, :permissions)
                """),
                {
                    "catalog_id": catalog_id,
                    "user_group_id": perm["user_group_id"],
                    "permissions": perm.get("permissions", ["read"]),
                },
            )
        # Audit
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=catalog_id,
            action=AuditAction.PERMISSION_UPDATE,
            changes={"permissions": permissions},
        )

    # --- Helpers ---

    @staticmethod
    def _stringify_uuids(data: dict) -> dict:
        """Convert UUID values to strings for Pydantic model compatibility."""
        from uuid import UUID

        return {k: str(v) if isinstance(v, UUID) else v for k, v in data.items()}

    @staticmethod
    def _map_catalog(row: Any) -> Catalog:
        """Map a database row to a Catalog model."""
        data = dict(row)
        owner = CatalogOwner(
            id=data.pop("owner_id"),
            name=data.pop("owner_name"),
            email=data.pop("owner_email"),
        )
        # Convert UUID to str for id
        data["id"] = str(data["id"])
        return Catalog(**data, owner=owner)

    # --- Scheduled Sync ---

    async def get_catalogs_due_for_sync(
        self,
        db: AsyncSession,
        sync_interval_seconds: int,
    ) -> list[Catalog]:
        """Get active catalogs with active connections that are due for sync.

        A catalog is due if:
        - Status is 'active' (not 'disabled' or 'error')
        - Has an active Google connection (catalog_connections.status = 'active')
        - last_synced_at is NULL (never synced) or older than sync_interval_seconds
        - No sync job is currently running or pending
        """
        result = await db.execute(
            text("""
                SELECT c.*,
                       u.id AS owner_id,
                       CONCAT(u.first_name, ' ', u.last_name) AS owner_name,
                       u.email AS owner_email,
                       TRUE AS has_connection
                FROM catalogs c
                JOIN users u ON c.owner_user_id = u.id
                JOIN catalog_connections cc ON cc.catalog_id = c.id AND cc.status = 'active'
                WHERE c.status = 'active'
                  AND (c.source_config->>'shared_drive_id' IS NOT NULL
                       OR jsonb_array_length(COALESCE(c.source_config->'sources', '[]'::jsonb)) > 0)
                  AND (c.last_synced_at IS NULL
                       OR c.last_synced_at < NOW() - make_interval(secs => :interval))
                  AND NOT EXISTS (
                      SELECT 1 FROM catalog_sync_jobs csj
                      WHERE csj.catalog_id = c.id
                        AND csj.status IN ('pending', 'running', 'paused', 'cancelling')
                  )
                ORDER BY c.last_synced_at ASC NULLS FIRST
            """),
            {"interval": sync_interval_seconds},
        )
        rows = result.mappings().all()
        return [self._map_catalog(row) for row in rows]
