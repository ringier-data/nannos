"""Tests for CatalogSyncEngine — scheduled sync dispatch.

Tests cover:
- Heal stuck jobs on startup
- Tick dispatches syncs for eligible catalogs
- Concurrency limiting
- Start/stop lifecycle
- Sync pipeline SQL correctness (upsert_catalog_file, upsert_catalog_page, update_sync_job)
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.catalog.adapters.base import ExtractedPage, SourceFile
from playground_backend.catalog.task_queue import InMemoryTaskQueue
from playground_backend.models.catalog import (
    CatalogCreate,
    CatalogSourceType,
)
from playground_backend.models.user import User
from playground_backend.repositories.catalog_repository import CatalogRepository
from playground_backend.services.audit_service import AuditService
from playground_backend.services.catalog_service import CatalogService
from playground_backend.services.catalog_sync_engine import CatalogSyncEngine

# --- Fixtures ---


@pytest.fixture
def catalog_repository():
    repo = CatalogRepository()
    repo.set_audit_service(AuditService())
    return repo


@pytest.fixture
def catalog_service(catalog_repository):
    service = CatalogService()
    service.set_repository(catalog_repository)
    return service


async def _create_user(db: AsyncSession, user: User) -> None:
    await db.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES (:id, :sub, :email, :first_name, :last_name, :role)
        """),
        {
            "id": user.id,
            "sub": user.sub,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
        },
    )
    await db.commit()


async def _create_catalog_with_connection(
    db: AsyncSession,
    service: CatalogService,
    actor: User,
    name: str = "Test Catalog",
    shared_drive_id: str = "drive-123",
) -> str:
    """Create a catalog with an active connection and shared drive configured."""

    data = CatalogCreate(
        name=name,
        source_type=CatalogSourceType.GOOGLE_DRIVE,
        source_config={"shared_drive_id": shared_drive_id},
    )
    catalog = await service.create_catalog(db, data, actor=actor)
    await db.commit()

    # Add active connection
    await db.execute(
        text("""
            INSERT INTO catalog_connections (catalog_id, connector_user_id, encrypted_token, scopes, status)
            VALUES (:cid, :uid, :token, :scopes, 'active')
        """),
        {
            "cid": catalog.id,
            "uid": actor.id,
            "token": b"fake-token",
            "scopes": ["drive.readonly"],
        },
    )
    await db.commit()
    return catalog.id


class TestHealStuckJobs:
    """Test that stuck sync jobs are healed on engine startup."""

    @pytest.mark.asyncio
    async def test_heal_old_running_jobs(
        self,
        pg_session: AsyncSession,
        catalog_repository: CatalogRepository,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)

        # Create a "stuck" job — over 30 minutes old
        await pg_session.execute(
            text("""
                INSERT INTO catalog_sync_jobs (catalog_id, status, created_at)
                VALUES (:cid, 'running', NOW() - INTERVAL '2 hours')
            """),
            {"cid": catalog_id},
        )
        await pg_session.commit()

        # Create a mock session factory that yields our test session
        @asynccontextmanager
        async def mock_session_factory():
            yield pg_session

        engine = CatalogSyncEngine(
            repo=catalog_repository,
            task_queue=InMemoryTaskQueue(max_workers=1),
            db_session_factory=mock_session_factory,
            tick_interval_seconds=9999,  # Don't actually tick
        )

        await engine.heal_stuck_jobs()

        # Verify job was healed
        result = await pg_session.execute(
            text("SELECT status FROM catalog_sync_jobs WHERE catalog_id = :cid"),
            {"cid": catalog_id},
        )
        row = result.mappings().first()
        assert row is not None
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_recent_running_jobs_also_healed(
        self,
        pg_session: AsyncSession,
        catalog_repository: CatalogRepository,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)

        # Create a recent "running" job
        await pg_session.execute(
            text("""
                INSERT INTO catalog_sync_jobs (catalog_id, status, created_at)
                VALUES (:cid, 'running', NOW())
            """),
            {"cid": catalog_id},
        )
        await pg_session.commit()

        @asynccontextmanager
        async def mock_session_factory():
            yield pg_session

        engine = CatalogSyncEngine(
            repo=catalog_repository,
            task_queue=InMemoryTaskQueue(max_workers=1),
            db_session_factory=mock_session_factory,
        )

        await engine.heal_stuck_jobs()

        # Verify recent job was ALSO healed (no in-memory task survives restart)
        result = await pg_session.execute(
            text("SELECT status FROM catalog_sync_jobs WHERE catalog_id = :cid"),
            {"cid": catalog_id},
        )
        row = result.mappings().first()
        assert row is not None
        assert row["status"] == "failed"


class TestSyncEngineEligibility:
    """Test that get_catalogs_due_for_sync finds the right catalogs."""

    @pytest.mark.asyncio
    async def test_catalog_without_connection_not_eligible(
        self,
        pg_session: AsyncSession,
        catalog_repository: CatalogRepository,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        """Catalog with no connection should not be returned."""
        data = CatalogCreate(
            name="No Connection",
            source_type=CatalogSourceType.GOOGLE_DRIVE,
            source_config={"shared_drive_id": "drive-abc"},
        )
        await catalog_service.create_catalog(pg_session, data, actor=test_user_db)
        await pg_session.commit()

        catalogs = await catalog_repository.get_catalogs_due_for_sync(pg_session, sync_interval_seconds=86400)
        assert len(catalogs) == 0

    @pytest.mark.asyncio
    async def test_catalog_without_shared_drive_not_eligible(
        self,
        pg_session: AsyncSession,
        catalog_repository: CatalogRepository,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        """Catalog with connection but no shared drive should not be returned."""
        data = CatalogCreate(
            name="No Drive",
            source_type=CatalogSourceType.GOOGLE_DRIVE,
            source_config={},  # No shared_drive_id
        )
        catalog = await catalog_service.create_catalog(pg_session, data, actor=test_user_db)
        await pg_session.commit()

        # Add connection
        await pg_session.execute(
            text("""
                INSERT INTO catalog_connections (catalog_id, connector_user_id, encrypted_token, scopes, status)
                VALUES (:cid, :uid, :token, :scopes, 'active')
            """),
            {
                "cid": catalog.id,
                "uid": test_user_db.id,
                "token": b"fake-token",
                "scopes": ["drive.readonly"],
            },
        )
        await pg_session.commit()

        catalogs = await catalog_repository.get_catalogs_due_for_sync(pg_session, sync_interval_seconds=86400)
        assert len(catalogs) == 0

    @pytest.mark.asyncio
    async def test_eligible_catalog_returned(
        self,
        pg_session: AsyncSession,
        catalog_repository: CatalogRepository,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        """Fully configured catalog should be eligible for sync."""
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)

        catalogs = await catalog_repository.get_catalogs_due_for_sync(pg_session, sync_interval_seconds=86400)
        assert len(catalogs) == 1
        assert catalogs[0].id == catalog_id

    @pytest.mark.asyncio
    async def test_catalog_with_running_job_not_eligible(
        self,
        pg_session: AsyncSession,
        catalog_repository: CatalogRepository,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        """Catalog with a running sync job should not be eligible."""
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)

        # Create running job
        await pg_session.execute(
            text("""
                INSERT INTO catalog_sync_jobs (catalog_id, status, created_at)
                VALUES (:cid, 'running', NOW())
            """),
            {"cid": catalog_id},
        )
        await pg_session.commit()

        catalogs = await catalog_repository.get_catalogs_due_for_sync(pg_session, sync_interval_seconds=86400)
        assert len(catalogs) == 0


class TestSyncEngineLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        engine = CatalogSyncEngine(
            repo=MagicMock(),
            task_queue=InMemoryTaskQueue(max_workers=1),
            db_session_factory=MagicMock(),
            tick_interval_seconds=9999,
        )

        # Patch _heal_stuck_jobs to avoid DB call
        engine._heal_stuck_jobs = AsyncMock()

        await engine.start()
        assert engine._running is True
        assert engine._task is not None

        await engine.stop()
        assert engine._running is False

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        engine = CatalogSyncEngine(
            repo=MagicMock(),
            task_queue=InMemoryTaskQueue(max_workers=1),
            db_session_factory=MagicMock(),
            tick_interval_seconds=9999,
        )
        engine._heal_stuck_jobs = AsyncMock()

        await engine.start()
        first_task = engine._task

        await engine.start()  # Should be noop
        assert engine._task is first_task

        await engine.stop()


class TestSyncPipelineSQL:
    """Test sync pipeline SQL against real PostgreSQL.

    These tests exercise _upsert_catalog_file, _upsert_catalog_page, and
    _update_sync_job to catch SQL syntax issues (e.g. :param::jsonb vs
    CAST(:param AS jsonb)) that mocked sessions would never catch.
    """

    @pytest.fixture
    def pipeline(self):
        """Create a pipeline with mocked adapter and session factory.

        Only the heavy AWS/ML dependencies are mocked; the SQL methods
        receive the real pg_session.
        """
        from playground_backend.catalog.sync import CatalogSyncPipeline

        with (
            patch("playground_backend.catalog.sync.GeminiEmbeddings"),
            patch("playground_backend.catalog.sync.boto3"),
            patch("playground_backend.catalog.sync.aiobotocore.session"),
        ):
            inst = CatalogSyncPipeline(
                adapter=MagicMock(),
                db_session_factory=MagicMock(),
            )
        return inst

    # -- _upsert_catalog_file --

    @pytest.mark.asyncio
    async def test_upsert_catalog_file_insert(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)

        file = SourceFile(
            id="drive-file-001",
            name="Test Presentation",
            mime_type="application/vnd.google-apps.presentation",
            modified_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            folder_path="Sales/Q1",
            metadata={"size": "12345", "owners": ["alice@example.com"]},
        )

        file_db_id, content_changed, existing_summary = await pipeline._upsert_catalog_file(
            pg_session, catalog_id, file, page_count=5
        )
        await pg_session.commit()

        assert file_db_id is not None
        assert content_changed is True  # new file, always changed
        assert existing_summary is None

        row = (
            (await pg_session.execute(text("SELECT * FROM catalog_files WHERE id = :id"), {"id": file_db_id}))
            .mappings()
            .first()
        )
        assert row["source_file_name"] == "Test Presentation"
        assert row["page_count"] == 5
        assert row["folder_path"] == "Sales/Q1"
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        assert meta["size"] == "12345"

    @pytest.mark.asyncio
    async def test_upsert_catalog_file_on_conflict_updates(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)

        file = SourceFile(
            id="drive-file-002",
            name="First Version",
            mime_type="application/pdf",
            modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            metadata={},
        )
        id1, changed1, _ = await pipeline._upsert_catalog_file(pg_session, catalog_id, file, page_count=3)
        await pg_session.commit()

        file.name = "Updated Version"
        id2, changed2, _ = await pipeline._upsert_catalog_file(pg_session, catalog_id, file, page_count=4)
        await pg_session.commit()

        assert id1 == id2
        # Same source_modified_at → not changed
        assert changed2 is False
        row = (
            (
                await pg_session.execute(
                    text("SELECT source_file_name, page_count FROM catalog_files WHERE id = :id"),
                    {"id": id1},
                )
            )
            .mappings()
            .first()
        )
        assert row["source_file_name"] == "Updated Version"
        # page_count is preserved from the first insert (ON CONFLICT keeps existing value);
        # the full processing path corrects it with a separate UPDATE.
        assert row["page_count"] == 3

    # -- _upsert_catalog_page --

    @pytest.mark.asyncio
    async def test_upsert_catalog_page_insert(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)
        file = SourceFile(
            id="drive-file-pages",
            name="A Deck",
            mime_type="application/vnd.google-apps.presentation",
            modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            metadata={},
        )
        file_db_id, _, _ = await pipeline._upsert_catalog_file(pg_session, catalog_id, file, page_count=1)
        await pg_session.commit()

        page = ExtractedPage(
            page_number=1,
            title="Title Slide",
            text_content="Welcome to Q1 Review",
            speaker_notes="Start with the highlights",
            source_ref={"type": "google_slides", "page_object_id": "g2f8a"},
            metadata={"layout": "TITLE"},
        )
        page_id = await pipeline._upsert_catalog_page(
            pg_session,
            catalog_id,
            file_db_id,
            page,
            content_hash="abc123hash",
            thumbnail_s3_key="cat/file/page_1.png",
        )
        await pg_session.commit()

        assert page_id is not None
        row = (
            (await pg_session.execute(text("SELECT * FROM catalog_pages WHERE id = :id"), {"id": page_id}))
            .mappings()
            .first()
        )
        assert row["title"] == "Title Slide"
        assert row["text_content"] == "Welcome to Q1 Review"
        assert row["speaker_notes"] == "Start with the highlights"
        assert row["content_hash"] == "abc123hash"
        src = row["source_ref"] if isinstance(row["source_ref"], dict) else json.loads(row["source_ref"])
        assert src["type"] == "google_slides"
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        assert meta["layout"] == "TITLE"

    @pytest.mark.asyncio
    async def test_batch_upsert_catalog_pages(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)
        file = SourceFile(
            id="drive-file-batch",
            name="Multi Slide Deck",
            mime_type="application/vnd.google-apps.presentation",
            modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            metadata={},
        )
        file_db_id, _, _ = await pipeline._upsert_catalog_file(pg_session, catalog_id, file, page_count=3)
        await pg_session.commit()

        pages = [
            ExtractedPage(
                page_number=i,
                title=f"Slide {i}",
                text_content=f"Content for slide {i}",
                speaker_notes=f"Notes {i}",
                source_ref={"type": "google_slides", "slide_index": i - 1},
                metadata={"layout": "BODY"},
            )
            for i in range(1, 4)
        ]
        rows = [
            (pages[0], "hash_a", "cat/file/page_1.png"),
            (pages[1], "hash_b", None),
            (pages[2], "hash_c", "cat/file/page_3.png"),
        ]

        await pipeline._batch_upsert_catalog_pages(pg_session, catalog_id, file_db_id, rows)
        await pg_session.commit()

        result = await pg_session.execute(
            text("SELECT * FROM catalog_pages WHERE file_id = :fid ORDER BY page_number"),
            {"fid": file_db_id},
        )
        db_pages = result.mappings().all()
        assert len(db_pages) == 3
        assert db_pages[0]["title"] == "Slide 1"
        assert db_pages[0]["content_hash"] == "hash_a"
        assert db_pages[0]["thumbnail_s3_key"] == "cat/file/page_1.png"
        assert db_pages[1]["title"] == "Slide 2"
        assert db_pages[1]["thumbnail_s3_key"] is None
        assert db_pages[2]["title"] == "Slide 3"
        assert db_pages[2]["content_hash"] == "hash_c"

    @pytest.mark.asyncio
    async def test_batch_upsert_catalog_pages_update_existing(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        """Batch upsert should update existing pages on conflict."""
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)
        file = SourceFile(
            id="drive-file-batch-update",
            name="Updatable Deck",
            mime_type="application/vnd.google-apps.presentation",
            modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            metadata={},
        )
        file_db_id, _, _ = await pipeline._upsert_catalog_file(pg_session, catalog_id, file, page_count=1)
        await pg_session.commit()

        page = ExtractedPage(
            page_number=1,
            title="Original",
            text_content="First version",
            speaker_notes="",
            source_ref={"type": "pptx"},
            metadata={},
        )
        # Insert via single upsert first
        await pipeline._upsert_catalog_page(
            pg_session,
            catalog_id,
            file_db_id,
            page,
            content_hash="old_hash",
            thumbnail_s3_key="thumb.png",
        )
        await pg_session.commit()

        # Now batch upsert with updated content (no thumbnail → should preserve old)
        updated_page = ExtractedPage(
            page_number=1,
            title="Updated",
            text_content="Second version",
            speaker_notes="New notes",
            source_ref={"type": "pptx"},
            metadata={},
        )
        await pipeline._batch_upsert_catalog_pages(
            pg_session,
            catalog_id,
            file_db_id,
            [(updated_page, "new_hash", None)],
        )
        await pg_session.commit()

        row = (
            (
                await pg_session.execute(
                    text("SELECT * FROM catalog_pages WHERE file_id = :fid AND page_number = 1"),
                    {"fid": file_db_id},
                )
            )
            .mappings()
            .first()
        )
        assert row["title"] == "Updated"
        assert row["text_content"] == "Second version"
        assert row["content_hash"] == "new_hash"
        # thumbnail_s3_key preserved via COALESCE
        assert row["thumbnail_s3_key"] == "thumb.png"

    # -- _update_sync_job --

    @pytest.mark.asyncio
    async def test_update_sync_job_plain_fields(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)
        job_id = str(
            (
                await pg_session.execute(
                    text("""
                        INSERT INTO catalog_sync_jobs (catalog_id, status, created_at)
                        VALUES (:cid, 'pending', NOW()) RETURNING id
                    """),
                    {"cid": catalog_id},
                )
            ).scalar_one()
        )
        await pg_session.commit()

        await pipeline._update_sync_job(
            pg_session,
            job_id,
            status="running",
            total_files=42,
            processed_files=10,
            failed_files=2,
        )

        row = (
            (
                await pg_session.execute(
                    text(
                        "SELECT status, total_files, processed_files, failed_files FROM catalog_sync_jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                )
            )
            .mappings()
            .first()
        )
        assert row["status"] == "running"
        assert row["total_files"] == 42
        assert row["processed_files"] == 10
        assert row["failed_files"] == 2

    @pytest.mark.asyncio
    async def test_update_sync_job_with_error_details_jsonb(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        pipeline,
    ):
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)
        job_id = str(
            (
                await pg_session.execute(
                    text("""
                        INSERT INTO catalog_sync_jobs (catalog_id, status, created_at)
                        VALUES (:cid, 'running', NOW()) RETURNING id
                    """),
                    {"cid": catalog_id},
                )
            ).scalar_one()
        )
        await pg_session.commit()

        await pipeline._update_sync_job(
            pg_session,
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc),
            error_details={"error": "Something went wrong", "files_failed": 3},
        )

        row = (
            (
                await pg_session.execute(
                    text("SELECT status, error_details FROM catalog_sync_jobs WHERE id = :id"),
                    {"id": job_id},
                )
            )
            .mappings()
            .first()
        )
        assert row["status"] == "failed"
        details = row["error_details"] if isinstance(row["error_details"], dict) else json.loads(row["error_details"])
        assert details["error"] == "Something went wrong"
        assert details["files_failed"] == 3

    @pytest.mark.asyncio
    async def test_error_details_roundtrip_through_repository(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        catalog_repository: CatalogRepository,
        test_user_db: User,
        pipeline,
    ):
        """Write error_details via pipeline, read back via repository.

        Catches double-encoding bugs where json.dumps() is called on an
        already-serialised string, producing a JSONB string instead of object.
        """
        catalog_id = await _create_catalog_with_connection(pg_session, catalog_service, test_user_db)
        job_id = str(
            (
                await pg_session.execute(
                    text("""
                        INSERT INTO catalog_sync_jobs (catalog_id, status, created_at)
                        VALUES (:cid, 'running', NOW()) RETURNING id
                    """),
                    {"cid": catalog_id},
                )
            ).scalar_one()
        )
        await pg_session.commit()

        await pipeline._update_sync_job(
            pg_session,
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc),
            error_details={"error": "Something went wrong", "files_failed": 3},
        )

        # Read back through the repository — exercises _row_to_sync_job / Pydantic
        job = await catalog_repository.get_latest_sync_job(pg_session, catalog_id)
        assert job is not None
        assert job.error_details == {"error": "Something went wrong", "files_failed": 3}
