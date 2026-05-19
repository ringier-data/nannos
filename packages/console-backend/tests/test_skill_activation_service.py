"""Unit tests for SkillActivationService.

Tests cover:
- Activate a registry skill on an agent
- Deactivate a skill (remove activation + docstore snapshot)
- Self-update after registry edit
- List activations with update-available detection
- Upsert locked activations during config set-default
- Error cases (duplicate activation, locked deactivation, missing registry)
"""

import hashlib
import json
import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.services.skill_activation_service import SkillActivationService

# --- Helpers ---


def _compute_content_hash(files: list[dict]) -> str:
    """Compute SHA-256 hash of files for test verification."""
    hasher = hashlib.sha256()
    for f in sorted(files, key=lambda x: x["path"]):
        hasher.update(f["path"].encode())
        hasher.update(f["contents"].encode())
    return hasher.hexdigest()


async def _create_test_user(db: AsyncSession, user_id: str = "test-user-1") -> str:
    """Insert a test user and return their ID."""
    await db.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, created_at, updated_at)
            VALUES (:id, :id, :email, 'Test', 'User', NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
        """),
        {"id": user_id, "email": f"{user_id}@test.com"},
    )
    return user_id


async def _create_test_group(db: AsyncSession, name: str = "test-group") -> int:
    """Insert a test user group and return its ID."""
    result = await db.execute(
        text("INSERT INTO user_groups (name, created_at, updated_at) VALUES (:name, NOW(), NOW()) RETURNING id"),
        {"name": name},
    )
    return result.scalar_one()


async def _create_test_agent(db: AsyncSession, name: str = "test-agent", user_id: str = "test-user-1") -> int:
    """Insert a test sub-agent and return its ID."""
    result = await db.execute(
        text("""
            INSERT INTO sub_agents (name, type, owner_user_id, created_at, updated_at)
            VALUES (:name, 'local', :user_id, NOW(), NOW())
            RETURNING id
        """),
        {"name": name, "user_id": user_id},
    )
    return result.scalar_one()


async def _create_registry_entry(
    db: AsyncSession,
    slug: str = "test-skill",
    name: str = "Test Skill",
    files: list[dict] | None = None,
    user_id: str = "test-user-1",
) -> str:
    """Insert a test skill registry entry and return its UUID."""
    if files is None:
        files = [{"path": "SKILL.md", "contents": "# Test Skill\nTest body"}]
    content_hash = _compute_content_hash(files)
    result = await db.execute(
        text("""
            INSERT INTO skill_registry (slug, name, description, source_type, files, content_hash, visibility, created_by)
            VALUES (:slug, :name, 'Test description', 'nannos', :files, :content_hash, 'public', :created_by)
            RETURNING id::text
        """),
        {
            "slug": slug,
            "name": name,
            "files": json.dumps(files),
            "content_hash": content_hash,
            "created_by": user_id,
        },
    )
    return result.scalar_one()


# --- Fixtures ---


@pytest.fixture
def activation_service() -> SkillActivationService:
    """Create a SkillActivationService with a mocked PlaybookService."""
    service = SkillActivationService()
    mock_playbook = MagicMock()
    mock_playbook.put_skill_with_files = AsyncMock()
    mock_playbook.delete_skill = AsyncMock(return_value=True)
    mock_playbook.is_available = True
    service.set_playbook_service(mock_playbook)
    return service


# --- Tests ---


class TestActivate:
    """Test skill activation."""

    @pytest.mark.asyncio
    async def test_activate_personal_scope(self, pg_session: AsyncSession, activation_service):
        """Activating a skill creates an activation record and writes to docstore."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        activation_id = await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        # Verify activation record was created
        result = await pg_session.execute(
            text("SELECT * FROM skill_activations WHERE id = :id"),
            {"id": activation_id},
        )
        row = result.mappings().first()
        assert row is not None
        assert row["sub_agent_id"] == agent_id
        assert row["scope"] == "personal"
        assert row["user_id"] == user_id
        assert row["locked"] is False

        # Verify docstore write was called
        activation_service.playbook_service.put_skill_with_files.assert_called_once()

    @pytest.mark.asyncio
    async def test_activate_group_scope(self, pg_session: AsyncSession, activation_service):
        """Activating with group scope creates activation with group_id."""
        user_id = await _create_test_user(pg_session)
        group_id = await _create_test_group(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        activation_id = await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="group",
            user_id=user_id,
            group_id=group_id,
        )
        await pg_session.commit()

        result = await pg_session.execute(
            text("SELECT * FROM skill_activations WHERE id = :id"),
            {"id": activation_id},
        )
        row = result.mappings().first()
        assert row is not None
        assert row["scope"] == "group"
        assert row["group_id"] == group_id
        assert row["user_id"] is None

    @pytest.mark.asyncio
    async def test_activate_duplicate_raises(self, pg_session: AsyncSession, activation_service):
        """Activating the same skill twice raises ValueError."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        with pytest.raises(ValueError, match="already activated"):
            await activation_service.activate(
                db=pg_session,
                registry_id=registry_id,
                sub_agent_id=agent_id,
                agent_name="test-agent",
                scope="personal",
                user_id=user_id,
            )

    @pytest.mark.asyncio
    async def test_activate_missing_registry_raises(self, pg_session: AsyncSession, activation_service):
        """Activating with a non-existent registry_id raises ValueError."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        await pg_session.commit()

        with pytest.raises(ValueError, match="not found"):
            await activation_service.activate(
                db=pg_session,
                registry_id="00000000-0000-0000-0000-000000000000",
                sub_agent_id=agent_id,
                agent_name="test-agent",
                scope="personal",
                user_id=user_id,
            )


class TestDeactivate:
    """Test skill deactivation."""

    @pytest.mark.asyncio
    async def test_deactivate_removes_record(self, pg_session: AsyncSession, activation_service):
        """Deactivating removes the activation record."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        activation_id = await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        result = await activation_service.deactivate(
            db=pg_session,
            activation_id=activation_id,
            agent_name="test-agent",
            user_id=user_id,
        )
        await pg_session.commit()

        assert result is True

        # Verify record is gone
        check = await pg_session.execute(
            text("SELECT id FROM skill_activations WHERE id = :id"),
            {"id": activation_id},
        )
        assert check.first() is None

    @pytest.mark.asyncio
    async def test_deactivate_locked_raises(self, pg_session: AsyncSession, activation_service):
        """Deactivating a locked activation raises ValueError."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        # Create a locked activation directly
        result = await pg_session.execute(
            text("""
                INSERT INTO skill_activations
                    (sub_agent_id, registry_id, scope, user_id, content_hash, locked, activated_by)
                VALUES
                    (:agent_id, :reg_id, 'personal', :user_id, 'abc123', TRUE, :user_id)
                RETURNING id
            """),
            {"agent_id": agent_id, "reg_id": registry_id, "user_id": user_id},
        )
        activation_id = result.scalar_one()
        await pg_session.commit()

        with pytest.raises(ValueError, match="locked"):
            await activation_service.deactivate(
                db=pg_session,
                activation_id=activation_id,
                agent_name="test-agent",
                user_id=user_id,
            )

    @pytest.mark.asyncio
    async def test_deactivate_not_found_returns_false(self, pg_session: AsyncSession, activation_service):
        """Deactivating a non-existent activation returns False."""
        result = await activation_service.deactivate(
            db=pg_session,
            activation_id=99999,
            agent_name="test-agent",
            user_id="user-1",
        )
        assert result is False


class TestSelfUpdate:
    """Test self-update (author's fast path after registry edit)."""

    @pytest.mark.asyncio
    async def test_self_update_refreshes_hash(self, pg_session: AsyncSession, activation_service):
        """Self-update updates the activation content_hash to match registry."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        files_v1 = [{"path": "SKILL.md", "contents": "# Version 1"}]
        registry_id = await _create_registry_entry(pg_session, files=files_v1, user_id=user_id)
        await pg_session.commit()

        activation_id = await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        # Simulate registry update (change content_hash in registry)
        files_v2 = [{"path": "SKILL.md", "contents": "# Version 2 - Updated"}]
        new_hash = _compute_content_hash(files_v2)
        await pg_session.execute(
            text("UPDATE skill_registry SET files = :files, content_hash = :hash WHERE id = CAST(:id AS uuid)"),
            {"files": json.dumps(files_v2), "hash": new_hash, "id": registry_id},
        )
        await pg_session.commit()

        # Self-update
        result = await activation_service.self_update(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            user_id=user_id,
        )
        await pg_session.commit()

        assert result is True

        # Verify hash was updated
        check = await pg_session.execute(
            text("SELECT content_hash FROM skill_activations WHERE id = :id"),
            {"id": activation_id},
        )
        assert check.scalar_one() == new_hash

    @pytest.mark.asyncio
    async def test_self_update_no_activation_returns_false(self, pg_session: AsyncSession, activation_service):
        """Self-update with no matching activation returns False."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        result = await activation_service.self_update(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            user_id=user_id,
        )
        assert result is False


class TestListForAgent:
    """Test listing activations with update-available detection."""

    @pytest.mark.asyncio
    async def test_list_shows_personal_activations(self, pg_session: AsyncSession, activation_service):
        """List returns personal scope activations for the user."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        activations = await activation_service.list_for_agent(
            db=pg_session,
            sub_agent_id=agent_id,
            user_id=user_id,
        )
        assert len(activations) == 1
        assert activations[0].skill_name == "Test Skill"
        assert activations[0].update_available is False

    @pytest.mark.asyncio
    async def test_list_detects_update_available(self, pg_session: AsyncSession, activation_service):
        """List shows update_available=True when registry hash differs from activation."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        # Update registry (simulate new version)
        new_files = [{"path": "SKILL.md", "contents": "# Updated content"}]
        new_hash = _compute_content_hash(new_files)
        await pg_session.execute(
            text("UPDATE skill_registry SET files = :files, content_hash = :hash WHERE id = CAST(:id AS uuid)"),
            {"files": json.dumps(new_files), "hash": new_hash, "id": registry_id},
        )
        await pg_session.commit()

        activations = await activation_service.list_for_agent(
            db=pg_session,
            sub_agent_id=agent_id,
            user_id=user_id,
        )
        assert len(activations) == 1
        assert activations[0].update_available is True
        assert activations[0].latest_hash == new_hash


class TestUpdateActivation:
    """Test pulling latest from registry."""

    @pytest.mark.asyncio
    async def test_update_pulls_latest_hash(self, pg_session: AsyncSession, activation_service):
        """Update activation refreshes hash and docstore from registry."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, user_id=user_id)
        await pg_session.commit()

        activation_id = await activation_service.activate(
            db=pg_session,
            registry_id=registry_id,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            scope="personal",
            user_id=user_id,
        )
        await pg_session.commit()

        # Update registry
        new_files = [{"path": "SKILL.md", "contents": "# New version"}]
        new_hash = _compute_content_hash(new_files)
        await pg_session.execute(
            text("UPDATE skill_registry SET files = :files, content_hash = :hash WHERE id = CAST(:id AS uuid)"),
            {"files": json.dumps(new_files), "hash": new_hash, "id": registry_id},
        )
        await pg_session.commit()

        result_hash = await activation_service.update_activation(
            db=pg_session,
            activation_id=activation_id,
            agent_name="test-agent",
            user_id=user_id,
        )
        await pg_session.commit()

        assert result_hash == new_hash


class TestUpsertLocked:
    """Test locked activation creation during config set-default."""

    @pytest.mark.asyncio
    async def test_upsert_locked_creates_activations(self, pg_session: AsyncSession, activation_service):
        """Upsert locked creates locked activation records."""
        user_id = await _create_test_user(pg_session)
        agent_id = await _create_test_agent(pg_session, user_id=user_id)
        registry_id = await _create_registry_entry(pg_session, slug="locked-skill", user_id=user_id)
        await pg_session.commit()

        # Create a config version for reference
        result = await pg_session.execute(
            text("""
                INSERT INTO sub_agent_config_versions (sub_agent_id, version, version_hash, description, system_prompt, status, created_at)
                VALUES (:agent_id, 1, 'abc123456789', 'Test version', 'You are a test agent', 'approved', NOW())
                RETURNING id
            """),
            {"agent_id": agent_id},
        )
        config_version_id = result.scalar_one()
        await pg_session.commit()

        await activation_service.upsert_locked(
            db=pg_session,
            sub_agent_id=agent_id,
            agent_name="test-agent",
            registry_refs=[{"registry_id": registry_id, "name": "locked-skill"}],
            config_version_id=config_version_id,
            activated_by=user_id,
        )
        await pg_session.commit()

        # Verify locked activation exists
        check = await pg_session.execute(
            text("SELECT * FROM skill_activations WHERE sub_agent_id = :id AND locked = TRUE"),
            {"id": agent_id},
        )
        rows = check.mappings().all()
        assert len(rows) == 1
        assert rows[0]["locked"] is True
        assert str(rows[0]["registry_id"]) == registry_id
