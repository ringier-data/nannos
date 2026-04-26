"""Service for managing catalogs."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.catalog import (
    AddSourceRequest,
    Catalog,
    CatalogCreate,
    CatalogFile,
    CatalogPage,
    CatalogSource,
    CatalogSyncJob,
    CatalogUpdate,
    UpdateSourceRequest,
)
from ..models.user import User

if TYPE_CHECKING:
    from ..catalog.sync import CatalogSyncPipeline
    from ..catalog.task_queue import SyncTaskMessage, SyncTaskQueue
    from ..catalog.token_service import CatalogTokenService
    from ..repositories.catalog_repository import CatalogRepository

logger = logging.getLogger(__name__)


class CatalogService:
    """Service for catalog CRUD and permission checks."""

    def __init__(self) -> None:
        self._repo: "CatalogRepository | None" = None
        self._sync_pipeline: "CatalogSyncPipeline | None" = None
        self._token_service: "CatalogTokenService | None" = None
        self._db_session_factory: Any = None
        self._socket_notification_manager: Any = None
        self._task_queue: "SyncTaskQueue | None" = None

    def set_repository(self, repo: "CatalogRepository") -> None:
        self._repo = repo

    def set_sync_pipeline(self, pipeline: "CatalogSyncPipeline") -> None:
        self._sync_pipeline = pipeline

    def set_token_service(self, token_service: "CatalogTokenService") -> None:
        self._token_service = token_service

    def set_db_session_factory(self, factory: Any) -> None:
        self._db_session_factory = factory

    def set_socket_notification_manager(self, manager: Any) -> None:
        self._socket_notification_manager = manager

    def set_task_queue(self, queue: "SyncTaskQueue") -> None:
        self._task_queue = queue

    @property
    def repo(self) -> "CatalogRepository":
        if self._repo is None:
            raise RuntimeError("CatalogRepository not injected. Call set_repository()")
        return self._repo

    # --- Catalog CRUD ---

    async def get_accessible_catalogs(
        self,
        db: AsyncSession,
        user: User,
        is_admin: bool = False,
    ) -> list[Catalog]:
        """Get catalogs accessible to user."""
        return await self.repo.get_accessible_catalogs(db, user.id, is_admin=is_admin)

    async def get_catalog(
        self,
        db: AsyncSession,
        catalog_id: str,
        user: User,
        is_admin: bool = False,
    ) -> Catalog | None:
        """Get a single catalog if user has access."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            return None
        if is_admin or catalog.owner_user_id == user.id:
            return catalog
        # Check group-based access
        accessible = await self.repo.get_accessible_catalogs(db, user.id)
        if any(c.id == catalog_id for c in accessible):
            return catalog
        return None

    async def create_catalog(
        self,
        db: AsyncSession,
        data: CatalogCreate,
        actor: User,
    ) -> Catalog:
        """Create a new catalog."""
        catalog_id = await self.repo.create(
            db=db,
            actor=actor,
            fields={
                "name": data.name,
                "description": data.description,
                "owner_user_id": actor.id,
                "source_type": data.source_type.value,
                "source_config": json.dumps(data.source_config),
            },
            returning="id",
        )
        catalog = await self.repo.get_catalog(db, str(catalog_id))
        if not catalog:
            raise RuntimeError("Failed to retrieve created catalog")
        return catalog

    async def update_catalog(
        self,
        db: AsyncSession,
        catalog_id: str,
        data: CatalogUpdate,
        actor: User,
        is_admin: bool = False,
    ) -> Catalog | None:
        """Update a catalog."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            return None
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can update this catalog")

        fields: dict[str, Any] = {}
        if data.name is not None:
            fields["name"] = data.name
        if data.description is not None:
            fields["description"] = data.description
        if data.source_config is not None:
            fields["source_config"] = json.dumps(data.source_config)

        if fields:
            await self.repo.update(
                db=db,
                actor=actor,
                entity_id=catalog_id,
                fields=fields,
            )

        return await self.repo.get_catalog(db, catalog_id)

    async def delete_catalog(
        self,
        db: AsyncSession,
        catalog_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> bool:
        """Delete a catalog and all associated data."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            return False
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can delete this catalog")

        # CASCADE handles permissions, files, pages, sync jobs, connections
        await self.repo.delete(db=db, actor=actor, entity_id=catalog_id, soft=False)
        return True

    # --- Permissions ---

    async def get_permissions(
        self,
        db: AsyncSession,
        catalog_id: str,
    ) -> list[dict[str, Any]]:
        """Get permissions for a catalog."""
        return await self.repo.get_permissions(db, catalog_id)

    async def set_permissions(
        self,
        db: AsyncSession,
        catalog_id: str,
        permissions: list[dict[str, Any]],
        actor: User,
        is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Set permissions for a catalog."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can manage permissions")

        await self.repo.set_permissions(db, actor, catalog_id, permissions)
        return await self.repo.get_permissions(db, catalog_id)

    # --- Files & Pages ---

    async def get_catalog_files(
        self,
        db: AsyncSession,
        catalog_id: str,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
    ) -> tuple[list[CatalogFile], int]:
        """Get paginated files in a catalog."""
        return await self.repo.get_catalog_files(db, catalog_id, limit, offset, search, status)

    async def get_file_pages(
        self,
        db: AsyncSession,
        file_id: str,
    ) -> list[CatalogPage]:
        """Get pages for a specific file."""
        return await self.repo.get_file_pages(db, file_id)

    async def update_file_indexing(
        self,
        db: AsyncSession,
        catalog_id: str,
        file_id: str,
        indexing_excluded: bool,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogFile:
        """Toggle indexing exclusion for a file and remove from vector store if excluding."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can modify file indexing")

        updated = await self.repo.update_file_indexing(db, file_id, indexing_excluded)
        if not updated:
            raise ValueError("File not found")

        # If excluding, remove file's pages from vector store
        if indexing_excluded and self._sync_pipeline:
            try:
                vector_store = self._sync_pipeline._get_vector_store(catalog_id)
                # Get all page numbers for this file to build document IDs
                pages = await self.repo.get_file_pages(db, file_id)
                if pages:
                    doc_ids = [f"{updated.source_file_id}#page_{p.page_number}" for p in pages]
                    await vector_store.adelete(doc_ids)
                    # Reset indexed_at for all pages
                    from sqlalchemy import text

                    await db.execute(
                        text("UPDATE catalog_pages SET indexed_at = NULL WHERE file_id = :file_id"),
                        {"file_id": file_id},
                    )
            except Exception:
                logger.warning("Failed to remove excluded file %s from vector store", file_id)

        await db.commit()
        return updated

    async def get_catalog_pages(
        self,
        db: AsyncSession,
        catalog_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CatalogPage], int]:
        """Get paginated pages for a catalog."""
        return await self.repo.get_catalog_pages(db, catalog_id, limit, offset)

    # --- Sources ---

    async def get_sources(self, db: AsyncSession, catalog_id: str) -> list[CatalogSource]:
        """Get the list of configured sources for a catalog."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        from ..catalog.sync import normalize_source_config

        raw_sources = normalize_source_config(catalog.source_config or {})
        return [CatalogSource(**s) for s in raw_sources]

    async def add_source(
        self,
        db: AsyncSession,
        catalog_id: str,
        data: AddSourceRequest,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSource:
        """Add a source to a catalog's source_config."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can add sources")

        from ..catalog.sync import normalize_source_config

        sources = normalize_source_config(catalog.source_config or {})

        # Check for duplicate: same drive+folder combo
        for s in sources:
            if (
                s.get("drive_id") == data.drive_id
                and s.get("folder_id") == data.folder_id
                and s.get("type") == data.type.value
            ):
                raise ValueError("This source is already configured")

        new_source: dict[str, Any] = {
            "id": str(__import__("uuid").uuid4()),
            "type": data.type.value,
        }
        if data.drive_id:
            new_source["drive_id"] = data.drive_id
            new_source["drive_name"] = data.drive_name or ""
        if data.folder_id:
            new_source["folder_id"] = data.folder_id
            new_source["folder_name"] = data.folder_name or ""
        if data.exclude_folder_patterns:
            new_source["exclude_folder_patterns"] = data.exclude_folder_patterns

        sources.append(new_source)
        await self.repo.update(
            db=db,
            actor=actor,
            entity_id=catalog_id,
            fields={"source_config": json.dumps({"sources": sources})},
        )
        await db.commit()
        return CatalogSource(**new_source)

    async def remove_source(
        self,
        db: AsyncSession,
        catalog_id: str,
        source_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> None:
        """Remove a source from a catalog's source_config."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can remove sources")

        from ..catalog.sync import normalize_source_config

        sources = normalize_source_config(catalog.source_config or {})
        new_sources = [s for s in sources if s.get("id") != source_id]
        if len(new_sources) == len(sources):
            raise ValueError("Source not found")

        await self.repo.update(
            db=db,
            actor=actor,
            entity_id=catalog_id,
            fields={"source_config": json.dumps({"sources": new_sources})},
        )
        await db.commit()

    async def update_source(
        self,
        db: AsyncSession,
        catalog_id: str,
        source_id: str,
        data: "UpdateSourceRequest",
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSource:
        """Update settings (e.g. exclusion patterns) for an existing source."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can update sources")

        from ..catalog.sync import normalize_source_config

        sources = normalize_source_config(catalog.source_config or {})
        target = None
        for s in sources:
            if s.get("id") == source_id:
                target = s
                break
        if target is None:
            raise ValueError("Source not found")

        target["exclude_folder_patterns"] = data.exclude_folder_patterns

        await self.repo.update(
            db=db,
            actor=actor,
            entity_id=catalog_id,
            fields={"source_config": json.dumps({"sources": sources})},
        )
        await db.commit()
        return CatalogSource(**target)

    # --- Sync ---

    async def get_sync_status(
        self,
        db: AsyncSession,
        catalog_id: str,
    ) -> CatalogSyncJob | None:
        """Get latest sync job status."""
        return await self.repo.get_latest_sync_job(db, catalog_id)

    async def reindex_catalog(
        self,
        db: AsyncSession,
        catalog_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSyncJob:
        """Launch background re-index for pages with indexed_at = NULL.

        Creates a sync job with 'reindexing' status so progress survives page refresh.
        Returns the created sync job.
        """
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can re-index")
        if not self._sync_pipeline:
            raise RuntimeError("Sync pipeline not configured")

        # Check if a sync/reindex is already active
        latest = await self.repo.get_latest_sync_job(db, catalog_id)
        if latest and latest.status in ("pending", "running", "reindexing", "paused", "cancelling"):
            raise ValueError("A sync or re-index is already in progress")

        # Count unindexed pages to return immediately
        count_result = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM catalog_pages cp
                JOIN catalog_files cf ON cf.id = cp.file_id
                WHERE cp.catalog_id = :catalog_id
                  AND cp.indexed_at IS NULL
                  AND cf.indexing_excluded = FALSE
            """),
            {"catalog_id": catalog_id},
        )
        total = count_result.scalar() or 0
        if total == 0:
            raise ValueError("No unindexed pages found")

        # Create sync job with 'reindexing' status
        job_id = await self.repo.create_sync_job(db, catalog_id)
        await self.repo.update_sync_job(
            db,
            job_id,
            {
                "status": "reindexing",
                "total_files": total,  # repurposed as total pages for reindex
            },
        )
        await db.commit()

        job = await self.repo.get_sync_job(db, job_id)
        if not job:
            raise RuntimeError("Failed to retrieve created sync job")

        # Register per-job state and launch background task
        self._setup_sync_progress_socket(catalog, job_id, user_sub=actor.id)

        asyncio.create_task(
            self._run_reindex_background(catalog_id, job_id, total, actor.id),
            name=f"catalog-reindex-{catalog_id}",
        )
        return job

    async def _run_reindex_background(
        self, catalog_id: str, sync_job_id: str, total: int, user_sub: str | None = None
    ) -> None:
        """Run reindex in background, tracking progress through the sync job."""
        assert self._sync_pipeline is not None
        assert self._db_session_factory is not None

        pipeline = self._sync_pipeline
        db_factory = self._db_session_factory

        async def _progress(data: dict) -> None:
            """Update sync job with reindex progress."""
            async with db_factory() as db:
                await pipeline._update_sync_job(
                    db,
                    sync_job_id,
                    processed_files=data.get("indexed", 0),  # repurposed
                    failed_files=data.get("failed", 0),  # repurposed
                )

        try:
            result = await pipeline.reindex_unindexed_pages(
                catalog_id,
                progress_callback=_progress,
                sync_job_id=sync_job_id,
            )
            async with db_factory() as db:
                await pipeline._update_sync_job(
                    db,
                    sync_job_id,
                    status="completed",
                    completed_at=datetime.now(timezone.utc),
                    processed_files=result.get("indexed", 0),
                    failed_files=result.get("failed", 0),
                )
            logger.info("Reindex completed for catalog %s (job %s)", catalog_id, sync_job_id)
        except Exception as exc:
            logger.exception("Reindex failed for catalog %s (job %s)", catalog_id, sync_job_id)
            try:
                async with db_factory() as db:
                    await pipeline._update_sync_job(
                        db,
                        sync_job_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        error_details={"error": str(exc)},
                    )
            except Exception:
                logger.exception("Failed to mark reindex job %s as failed", sync_job_id)
        finally:
            pipeline.teardown_job(sync_job_id)

    async def trigger_sync(
        self,
        db: AsyncSession,
        catalog_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSyncJob:
        """Trigger a manual sync for a catalog."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can trigger sync")

        # Verify at least one source is configured
        from ..catalog.sync import normalize_source_config

        sources = normalize_source_config(catalog.source_config or {})
        if not sources:
            raise ValueError("No sources configured. Please add at least one source first.")

        # Atomically create job only if no active job exists (eliminates TOCTOU race)
        job_id = await self.repo.create_sync_job_atomic(db, catalog_id)
        if job_id is None:
            raise ValueError("A sync is already in progress")
        await db.commit()

        job = await self.repo.get_sync_job(db, job_id)
        if not job:
            raise RuntimeError("Failed to retrieve created sync job")

        # Enqueue via task queue (preferred) or fall back to direct background task
        if self._task_queue:
            from ..catalog.task_queue import SyncTaskMessage

            await self._task_queue.enqueue(
                SyncTaskMessage(
                    catalog_id=catalog_id,
                    sync_job_id=job_id,
                    triggered_by="manual",
                    user_sub=actor.id,
                )
            )
        elif self._sync_pipeline and self._token_service and self._db_session_factory:
            asyncio.create_task(
                self._run_sync_background(catalog, job_id, user_sub=actor.id),
                name=f"catalog-sync-{catalog_id}",
            )
        else:
            logger.warning("Sync pipeline not configured; job %s will remain pending", job_id)

        return job

    # --- Sync Job State Machine ---

    # Valid state transitions (from → allowed targets).
    _VALID_TRANSITIONS: dict[str, set[str]] = {
        "pending": {"running", "reindexing", "paused", "cancelled", "failed"},
        "running": {"paused", "cancelling", "completed", "failed"},
        "reindexing": {"completed", "failed"},
        "paused": {"running", "cancelled", "failed"},
        "cancelling": {"cancelled", "failed"},
        # Terminal states — no transitions out
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }

    async def _transition_sync_job(
        self,
        db: AsyncSession,
        catalog_id: str,
        target_status: str,
    ) -> CatalogSyncJob:
        """Transition the latest sync job to a new status, enforcing the state machine."""
        latest = await self.repo.get_latest_sync_job(db, catalog_id)
        if not latest:
            raise ValueError("No sync job found")

        current = latest.status
        allowed = self._VALID_TRANSITIONS.get(current, set())
        if target_status not in allowed:
            raise ValueError(f"Cannot transition from '{current}' to '{target_status}'")

        await self.repo.update_sync_job(db, latest.id, {"status": target_status})
        await db.commit()

        updated = await self.repo.get_sync_job(db, latest.id)
        if not updated:
            raise RuntimeError("Failed to retrieve updated sync job")

        # Push state change via Socket.IO
        await self._emit_sync_event(catalog_id, {"status": target_status, "job_id": latest.id})

        return updated

    async def _emit_sync_event(self, catalog_id: str, fields: dict) -> None:
        """Emit a catalog_sync_progress socket event to the catalog owner."""
        socket_mgr = self._socket_notification_manager
        if not socket_mgr:
            return
        try:
            # Look up owner_user_id
            async with self._db_session_factory() as db:
                catalog = await self.repo.get_catalog(db, catalog_id)
            if catalog and catalog.owner_user_id:
                payload = {"catalog_id": catalog_id, **fields}
                await socket_mgr.emit_to_user(catalog.owner_user_id, "catalog_sync_progress", payload)
        except Exception:
            logger.debug("Failed to emit sync event for catalog %s", catalog_id)

    async def pause_sync(
        self,
        db: AsyncSession,
        catalog_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSyncJob:
        """Pause a running sync. The pipeline will stop after the current file."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can pause sync")
        return await self._transition_sync_job(db, catalog_id, "paused")

    async def resume_sync(
        self,
        db: AsyncSession,
        catalog_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSyncJob:
        """Resume a paused sync."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can resume sync")
        return await self._transition_sync_job(db, catalog_id, "running")

    async def cancel_sync(
        self,
        db: AsyncSession,
        catalog_id: str,
        actor: User,
        is_admin: bool = False,
    ) -> CatalogSyncJob:
        """Cancel a running or paused sync."""
        catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            raise ValueError("Catalog not found")
        if not is_admin and catalog.owner_user_id != actor.id:
            raise PermissionError("Only the owner or an admin can cancel sync")

        latest = await self.repo.get_latest_sync_job(db, catalog_id)
        if not latest:
            raise ValueError("No sync job found")

        # Pending/Paused → cancelled immediately (no pipeline running or not started)
        # Running → cancelling (pipeline will see flag and transition to cancelled)
        if latest.status in ("pending", "paused"):
            target = "cancelled"
        elif latest.status == "running":
            target = "cancelling"
        else:
            raise ValueError(f"Cannot cancel a sync in '{latest.status}' state")

        return await self._transition_sync_job(db, catalog_id, target)

    def _setup_sync_progress_socket(self, catalog: Catalog, sync_job_id: str, user_sub: str | None = None) -> None:
        """Register per-job state (socket callback + cost attribution) on the sync pipeline."""
        assert self._sync_pipeline is not None
        socket_mgr = self._socket_notification_manager
        user_id = catalog.owner_user_id
        callback = None
        if socket_mgr and user_id:

            async def _sync_progress(job_id: str, fields: dict) -> None:
                payload: dict = {}
                for k, v in fields.items():
                    if isinstance(v, datetime):
                        payload[k] = v.isoformat()
                    else:
                        payload[k] = v
                payload["job_id"] = job_id
                payload["catalog_id"] = catalog.id
                await socket_mgr.emit_to_user(user_id, "catalog_sync_progress", payload)

            callback = _sync_progress

        self._sync_pipeline.setup_job(
            sync_job_id=sync_job_id,
            user_sub=user_sub or catalog.owner_user_id,
            catalog_id=catalog.id,
            progress_callback=callback,
        )

    async def _run_sync_background(self, catalog: Catalog, sync_job_id: str, user_sub: str | None = None) -> None:
        """Run sync pipeline in a background task."""
        assert self._sync_pipeline is not None
        assert self._token_service is not None
        assert self._db_session_factory is not None

        # Register per-job state (socket callback + cost attribution)
        self._setup_sync_progress_socket(catalog, sync_job_id, user_sub=user_sub)

        try:
            async with self._db_session_factory() as db:
                credentials = await self._token_service.get_credentials(db, catalog.id)
                if not credentials:
                    logger.error("No active Google connection for catalog %s", catalog.id)
                    await self._sync_pipeline._update_sync_job(
                        db,
                        sync_job_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        error_details={"error": "No active Google connection"},
                    )
                    await db.commit()
                    return

            source_config = catalog.source_config or {}

            # Check if this is a full sync or incremental
            from ..catalog.sync import normalize_source_config

            sources = normalize_source_config(source_config)
            has_change_tokens = any(s.get("change_token") for s in sources)

            async with self._db_session_factory() as db:
                latest_completed = await db.execute(
                    __import__("sqlalchemy").text("""
                        SELECT id FROM catalog_sync_jobs
                        WHERE catalog_id = :cid AND status = 'completed'
                        ORDER BY completed_at DESC LIMIT 1
                    """),
                    {"cid": catalog.id},
                )
                has_prior_sync = latest_completed.first() is not None

            if has_prior_sync and has_change_tokens:
                new_tokens = await self._sync_pipeline.run_incremental_sync(
                    catalog_id=catalog.id,
                    source_config=source_config,
                    sync_job_id=sync_job_id,
                    credentials=credentials,
                )
                # Persist updated change tokens back to source_config
                if new_tokens:
                    await self._persist_change_tokens(catalog.id, source_config, new_tokens)
            else:
                await self._sync_pipeline.run_full_sync(
                    catalog_id=catalog.id,
                    source_config=source_config,
                    sync_job_id=sync_job_id,
                    credentials=credentials,
                )

            logger.info("Sync completed for catalog %s (job %s)", catalog.id, sync_job_id)

        except Exception:
            logger.exception("Background sync failed for catalog %s (job %s)", catalog.id, sync_job_id)
            # Ensure the job transitions to 'failed' so it doesn't stay stuck in pending/running
            try:
                async with self._db_session_factory() as db:
                    await self._sync_pipeline._update_sync_job(
                        db,
                        sync_job_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        error_details={"error": "Unexpected sync failure. Check server logs."},
                    )
                    await db.commit()
            except Exception:
                logger.exception("Failed to mark sync job %s as failed", sync_job_id)
        finally:
            self._sync_pipeline.teardown_job(sync_job_id)

    async def _persist_change_tokens(
        self, catalog_id: str, source_config: dict, new_tokens: dict[str, str | None]
    ) -> None:
        """Write updated per-source change tokens back to source_config."""
        try:
            from ..catalog.sync import normalize_source_config

            sources = normalize_source_config(source_config)
            updated = False
            for src in sources:
                token = new_tokens.get(src.get("id", ""))
                if token is not None:
                    src["change_token"] = token
                    updated = True
            if updated:
                async with self._db_session_factory() as db:
                    await db.execute(
                        text("UPDATE catalogs SET source_config = :cfg WHERE id = :cid"),
                        {"cfg": json.dumps({"sources": sources}), "cid": catalog_id},
                    )
                    await db.commit()
        except Exception:
            logger.warning("Failed to persist change tokens for catalog %s", catalog_id)

    # --- Task Queue Handler ---

    async def handle_sync_task(self, message: "SyncTaskMessage") -> None:
        """Execute a sync task from the queue.

        This is the unified handler used by the :class:`SyncTaskQueue` for
        both scheduled and manual syncs.
        """
        assert self._sync_pipeline is not None, "sync pipeline not configured"
        assert self._token_service is not None, "token service not configured"
        assert self._db_session_factory is not None, "db session factory not configured"

        catalog_id = message.catalog_id
        sync_job_id = message.sync_job_id

        # Look up catalog metadata
        async with self._db_session_factory() as db:
            catalog = await self.repo.get_catalog(db, catalog_id)
        if not catalog:
            logger.error("Catalog %s not found; failing sync job %s", catalog_id, sync_job_id)
            async with self._db_session_factory() as db:
                await self._sync_pipeline._update_sync_job(
                    db,
                    sync_job_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error_details={"error": "Catalog not found"},
                )
                await db.commit()
            return

        # Register per-job state (socket callback + cost attribution)
        self._setup_sync_progress_socket(catalog, sync_job_id, user_sub=message.user_sub)

        try:
            async with self._db_session_factory() as db:
                credentials = await self._token_service.get_credentials(db, catalog.id)
                if not credentials:
                    logger.error("No active Google connection for catalog %s", catalog.id)
                    await self._sync_pipeline._update_sync_job(
                        db,
                        sync_job_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        error_details={"error": "No active Google connection"},
                    )
                    await db.commit()
                    return

            source_config = catalog.source_config or {}

            # Check if this is a full sync or incremental
            from ..catalog.sync import normalize_source_config

            sources = normalize_source_config(source_config)
            has_change_tokens = any(s.get("change_token") for s in sources)

            async with self._db_session_factory() as db:
                latest_completed = await db.execute(
                    __import__("sqlalchemy").text("""
                        SELECT id FROM catalog_sync_jobs
                        WHERE catalog_id = :cid AND status = 'completed'
                        ORDER BY completed_at DESC LIMIT 1
                    """),
                    {"cid": catalog.id},
                )
                has_prior_sync = latest_completed.first() is not None

            if has_prior_sync and has_change_tokens:
                new_tokens = await self._sync_pipeline.run_incremental_sync(
                    catalog_id=catalog.id,
                    source_config=source_config,
                    sync_job_id=sync_job_id,
                    credentials=credentials,
                )
                if new_tokens:
                    await self._persist_change_tokens(catalog.id, source_config, new_tokens)
            else:
                await self._sync_pipeline.run_full_sync(
                    catalog_id=catalog.id,
                    source_config=source_config,
                    sync_job_id=sync_job_id,
                    credentials=credentials,
                )

            logger.info(
                "Sync completed for catalog %s (job %s, triggered_by=%s)",
                catalog.id,
                sync_job_id,
                message.triggered_by,
            )

        except Exception:
            logger.exception("Sync failed for catalog %s (job %s)", catalog.id, sync_job_id)
            try:
                async with self._db_session_factory() as db:
                    await self._sync_pipeline._update_sync_job(
                        db,
                        sync_job_id,
                        status="failed",
                        completed_at=datetime.now(timezone.utc),
                        error_details={"error": "Unexpected sync failure. Check server logs."},
                    )
                    await db.commit()
            except Exception:
                logger.exception("Failed to mark sync job %s as failed", sync_job_id)
        finally:
            self._sync_pipeline.teardown_job(sync_job_id)
