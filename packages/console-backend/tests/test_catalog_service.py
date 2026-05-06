"""Tests for CatalogService — CRUD, permissions, sync triggering.

Tests cover:
- Catalog creation, retrieval, update, delete
- Permission checks (owner, admin, non-owner)
- Sync trigger with guards (no drive, already running)
- Sync job status retrieval
- File and page listing
"""

import pytest
from console_backend.models.catalog import (
    Catalog,
    CatalogCreate,
    CatalogSourceType,
    CatalogUpdate,
)
from console_backend.models.user import User
from console_backend.repositories.catalog_repository import CatalogRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.catalog_service import CatalogService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Insert a user into the database."""
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


async def _create_catalog(
    db: AsyncSession,
    service: CatalogService,
    actor: User,
    name: str = "Test Catalog",
    source_config: dict | None = None,
) -> Catalog:
    """Helper to create a catalog."""
    data = CatalogCreate(
        name=name,
        description="Test description",
        source_type=CatalogSourceType.GOOGLE_DRIVE,
        source_config=source_config or {},
    )
    catalog = await service.create_catalog(db, data, actor=actor)
    await db.commit()
    return catalog


# --- Tests ---


class TestCatalogCRUD:
    """Test basic catalog CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_catalog(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        assert catalog.id is not None
        assert catalog.name == "Test Catalog"
        assert catalog.description == "Test description"
        assert catalog.owner_user_id == test_user_db.id
        assert catalog.source_type == CatalogSourceType.GOOGLE_DRIVE
        assert catalog.status.value == "active"

    @pytest.mark.asyncio
    async def test_get_catalog_by_owner(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        created = await _create_catalog(pg_session, catalog_service, test_user_db)
        fetched = await catalog_service.get_catalog(pg_session, created.id, test_user_db)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "Test Catalog"

    @pytest.mark.asyncio
    async def test_get_catalog_nonexistent_returns_none(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        result = await catalog_service.get_catalog(pg_session, "00000000-0000-0000-0000-000000000000", test_user_db)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_catalog_non_owner_returns_none(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        test_admin_user_db: User,
    ):
        """Non-owner without group access cannot see catalog."""
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        # Create another non-admin user
        other_user = User(
            id="other-user-id",
            sub="other-user-sub",
            email="other@test.com",
            first_name="Other",
            last_name="User",
        )
        await _create_user(pg_session, other_user)

        result = await catalog_service.get_catalog(pg_session, catalog.id, other_user)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_catalog_admin_can_see_any(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        test_admin_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)
        result = await catalog_service.get_catalog(pg_session, catalog.id, test_admin_user_db, is_admin=True)
        assert result is not None
        assert result.id == catalog.id

    @pytest.mark.asyncio
    async def test_update_catalog_by_owner(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        updated = await catalog_service.update_catalog(
            pg_session,
            catalog.id,
            CatalogUpdate(name="Updated Name", description="New desc"),
            actor=test_user_db,
        )
        await pg_session.commit()

        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.description == "New desc"

    @pytest.mark.asyncio
    async def test_update_catalog_non_owner_raises(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        test_admin_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        other_user = User(
            id="other-user-id-2",
            sub="other-user-sub-2",
            email="other2@test.com",
            first_name="Other",
            last_name="User",
        )
        await _create_user(pg_session, other_user)

        with pytest.raises(PermissionError, match="Only the owner"):
            await catalog_service.update_catalog(
                pg_session,
                catalog.id,
                CatalogUpdate(name="Hacked"),
                actor=other_user,
            )

    @pytest.mark.asyncio
    async def test_update_catalog_source_config(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        updated = await catalog_service.update_catalog(
            pg_session,
            catalog.id,
            CatalogUpdate(source_config={"shared_drive_id": "drive-123", "shared_drive_name": "Sales"}),
            actor=test_user_db,
        )
        await pg_session.commit()

        assert updated is not None
        assert updated.source_config["shared_drive_id"] == "drive-123"
        assert updated.source_config["shared_drive_name"] == "Sales"

    @pytest.mark.asyncio
    async def test_delete_catalog_by_owner(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        result = await catalog_service.delete_catalog(pg_session, catalog.id, actor=test_user_db)
        await pg_session.commit()

        assert result is True

        # Verify it's gone
        fetched = await catalog_service.get_catalog(pg_session, catalog.id, test_user_db)
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_catalog_non_owner_raises(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        other_user = User(
            id="other-user-id-3",
            sub="other-user-sub-3",
            email="other3@test.com",
            first_name="Other",
            last_name="User",
        )
        await _create_user(pg_session, other_user)

        with pytest.raises(PermissionError, match="Only the owner"):
            await catalog_service.delete_catalog(pg_session, catalog.id, actor=other_user)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        result = await catalog_service.delete_catalog(
            pg_session,
            "00000000-0000-0000-0000-000000000000",
            actor=test_user_db,
        )
        assert result is False


class TestCatalogHasConnection:
    """Test the has_connection field on catalog responses."""

    @pytest.mark.asyncio
    async def test_new_catalog_has_no_connection(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)
        assert catalog.has_connection is False

    @pytest.mark.asyncio
    async def test_catalog_with_active_connection(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        # Insert a fake connection directly
        await pg_session.execute(
            text("""
                INSERT INTO catalog_connections (catalog_id, connector_user_id, encrypted_token, scopes, status)
                VALUES (:cid, :uid, :token, :scopes, 'active')
            """),
            {
                "cid": catalog.id,
                "uid": test_user_db.id,
                "token": b"fake-encrypted-token",
                "scopes": ["drive.readonly"],
            },
        )
        await pg_session.commit()

        fetched = await catalog_service.get_catalog(pg_session, catalog.id, test_user_db)
        assert fetched is not None
        assert fetched.has_connection is True


class TestCatalogAccessibleList:
    """Test listing accessible catalogs."""

    @pytest.mark.asyncio
    async def test_owner_sees_own_catalogs(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        await _create_catalog(pg_session, catalog_service, test_user_db, name="Cat 1")
        await _create_catalog(pg_session, catalog_service, test_user_db, name="Cat 2")

        catalogs = await catalog_service.get_accessible_catalogs(pg_session, test_user_db)
        assert len(catalogs) == 2
        names = {c.name for c in catalogs}
        assert "Cat 1" in names
        assert "Cat 2" in names

    @pytest.mark.asyncio
    async def test_admin_sees_all_catalogs(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
        test_admin_user_db: User,
    ):
        await _create_catalog(pg_session, catalog_service, test_user_db, name="User Cat")

        catalogs = await catalog_service.get_accessible_catalogs(pg_session, test_admin_user_db, is_admin=True)
        assert len(catalogs) >= 1
        assert any(c.name == "User Cat" for c in catalogs)


class TestCatalogSync:
    """Test sync triggering and guards."""

    @pytest.mark.asyncio
    async def test_trigger_sync_without_sources_raises(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        """Cannot sync without configuring at least one source."""
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)

        with pytest.raises(ValueError, match="No sources configured"):
            await catalog_service.trigger_sync(pg_session, catalog.id, actor=test_user_db)

    @pytest.mark.asyncio
    async def test_trigger_sync_creates_job(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        """Trigger sync creates a pending job when pipeline not configured."""
        catalog = await _create_catalog(
            pg_session,
            catalog_service,
            test_user_db,
            source_config={"shared_drive_id": "drive-123"},
        )

        job = await catalog_service.trigger_sync(pg_session, catalog.id, actor=test_user_db)
        await pg_session.commit()

        assert job is not None
        assert job.catalog_id == catalog.id
        assert job.status.value == "pending"

    @pytest.mark.asyncio
    async def test_trigger_sync_already_running_raises(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(
            pg_session,
            catalog_service,
            test_user_db,
            source_config={"shared_drive_id": "drive-123"},
        )

        # First sync
        await catalog_service.trigger_sync(pg_session, catalog.id, actor=test_user_db)
        await pg_session.commit()

        # Second sync should fail
        with pytest.raises(ValueError, match="already in progress"):
            await catalog_service.trigger_sync(pg_session, catalog.id, actor=test_user_db)

    @pytest.mark.asyncio
    async def test_trigger_sync_non_owner_raises(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(
            pg_session,
            catalog_service,
            test_user_db,
            source_config={"shared_drive_id": "drive-123"},
        )

        other_user = User(
            id="other-user-id-4",
            sub="other-user-sub-4",
            email="other4@test.com",
            first_name="Other",
            last_name="User",
        )
        await _create_user(pg_session, other_user)

        with pytest.raises(PermissionError, match="Only the owner"):
            await catalog_service.trigger_sync(pg_session, catalog.id, actor=other_user)

    @pytest.mark.asyncio
    async def test_get_sync_status_none(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)
        status = await catalog_service.get_sync_status(pg_session, catalog.id)
        assert status is None

    @pytest.mark.asyncio
    async def test_get_sync_status_after_trigger(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(
            pg_session,
            catalog_service,
            test_user_db,
            source_config={"shared_drive_id": "drive-123"},
        )

        await catalog_service.trigger_sync(pg_session, catalog.id, actor=test_user_db)
        await pg_session.commit()

        status = await catalog_service.get_sync_status(pg_session, catalog.id)
        assert status is not None
        assert status.status.value == "pending"


class TestCatalogAudit:
    """Test that catalog operations generate audit logs."""

    @pytest.mark.asyncio
    async def test_create_catalog_generates_audit(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)
        await pg_session.commit()

        result = await pg_session.execute(
            text("""
                SELECT * FROM audit_logs
                WHERE entity_type = 'catalog'
                  AND entity_id = :id
                  AND action = 'create'
            """),
            {"id": catalog.id},
        )
        audit = result.mappings().first()
        assert audit is not None
        assert audit["actor_sub"] == test_user_db.sub

    @pytest.mark.asyncio
    async def test_update_catalog_generates_audit(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)
        await pg_session.commit()

        await catalog_service.update_catalog(
            pg_session,
            catalog.id,
            CatalogUpdate(name="Updated"),
            actor=test_user_db,
        )
        await pg_session.commit()

        result = await pg_session.execute(
            text("""
                SELECT * FROM audit_logs
                WHERE entity_type = 'catalog'
                  AND entity_id = :id
                  AND action = 'update'
            """),
            {"id": catalog.id},
        )
        audit = result.mappings().first()
        assert audit is not None

    @pytest.mark.asyncio
    async def test_delete_catalog_generates_audit(
        self,
        pg_session: AsyncSession,
        catalog_service: CatalogService,
        test_user_db: User,
    ):
        catalog = await _create_catalog(pg_session, catalog_service, test_user_db)
        await pg_session.commit()

        await catalog_service.delete_catalog(pg_session, catalog.id, actor=test_user_db)
        await pg_session.commit()

        result = await pg_session.execute(
            text("""
                SELECT * FROM audit_logs
                WHERE entity_type = 'catalog'
                  AND entity_id = :id
                  AND action = 'delete'
            """),
            {"id": catalog.id},
        )
        audit = result.mappings().first()
        assert audit is not None
