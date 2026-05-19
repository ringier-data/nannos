"""Tests for the skill activations REST API router.

Tests the /api/v1/skills/activations/ endpoints:
- GET /{sub_agent_id} — list activations
- POST / — activate a skill
- DELETE /{activation_id} — deactivate
- POST /{activation_id}/update — pull latest
- POST /bulk-update — bulk update
"""

import hashlib
import json
import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _compute_content_hash(files: list[dict]) -> str:
    hasher = hashlib.sha256()
    for f in sorted(files, key=lambda x: x["path"]):
        hasher.update(f["path"].encode())
        hasher.update(f["contents"].encode())
    return hasher.hexdigest()


async def _setup_test_data(db: AsyncSession, user_id: str = "test-user-id") -> dict:
    """Create required test data: user, group, agent, registry entry."""
    # Create group
    result = await db.execute(
        text("INSERT INTO user_groups (name, created_at, updated_at) VALUES ('test-group', NOW(), NOW()) RETURNING id")
    )
    group_id = result.scalar_one()

    # Create agent
    result = await db.execute(
        text("""
            INSERT INTO sub_agents (name, type, owner_user_id, created_at, updated_at)
            VALUES ('test-agent', 'local', :user_id, NOW(), NOW())
            RETURNING id
        """),
        {"user_id": user_id},
    )
    agent_id = result.scalar_one()

    # Create registry entry
    files = [{"path": "SKILL.md", "contents": "# Test Skill\nHello world"}]
    content_hash = _compute_content_hash(files)
    result = await db.execute(
        text("""
            INSERT INTO skill_registry (slug, name, description, source_type, files, content_hash, visibility, created_by)
            VALUES ('test-skill', 'Test Skill', 'A test skill', 'nannos', :files, :hash, 'public', :user_id)
            RETURNING id::text
        """),
        {"files": json.dumps(files), "hash": content_hash, "user_id": user_id},
    )
    registry_id = result.scalar_one()

    # Add user to group
    await db.execute(
        text("""
            INSERT INTO user_group_members (user_id, user_group_id, group_role, created_at)
            VALUES (:user_id, :group_id, 'manager', NOW())
        """),
        {"user_id": user_id, "group_id": group_id},
    )

    await db.commit()
    return {
        "group_id": group_id,
        "agent_id": agent_id,
        "registry_id": registry_id,
        "content_hash": content_hash,
    }


class TestListActivations:
    """Test GET /api/v1/skills/activations/{sub_agent_id}."""

    @pytest.mark.asyncio
    async def test_list_empty(self, client_with_db: AsyncClient, pg_session: AsyncSession):
        """Returns empty list when no activations exist."""
        data = await _setup_test_data(pg_session)
        response = await client_with_db.get(f"/api/v1/skills/activations/{data['agent_id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []

    @pytest.mark.asyncio
    async def test_list_with_activation(self, client_with_db: AsyncClient, pg_session: AsyncSession):
        """Returns activations for the agent."""
        data = await _setup_test_data(pg_session)

        # Create an activation directly
        await pg_session.execute(
            text("""
                INSERT INTO skill_activations
                    (sub_agent_id, registry_id, scope, user_id, content_hash, locked, activated_by)
                VALUES
                    (:agent_id, :reg_id, 'personal', 'test-user-id', :hash, FALSE, 'test-user-id')
            """),
            {"agent_id": data["agent_id"], "reg_id": data["registry_id"], "hash": data["content_hash"]},
        )
        await pg_session.commit()

        response = await client_with_db.get(f"/api/v1/skills/activations/{data['agent_id']}")
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["skill_name"] == "Test Skill"
        assert body["items"][0]["update_available"] is False


class TestActivateSkill:
    """Test POST /api/v1/skills/activations/."""

    @pytest.mark.asyncio
    async def test_activate_requires_registry_id_or_skill_name(
        self, client_with_db: AsyncClient, pg_session: AsyncSession
    ):
        """Request without registry_id returns 422."""
        await _setup_test_data(pg_session)

        response = await client_with_db.post(
            "/api/v1/skills/activations",
            json={"scope": "personal"},
        )
        # Should fail validation since neither registry_id nor sub_agent_id provided
        assert response.status_code == 422


class TestDeactivateSkill:
    """Test DELETE /api/v1/skills/activations/{activation_id}."""

    @pytest.mark.asyncio
    async def test_deactivate_not_found(self, client_with_db: AsyncClient, pg_session: AsyncSession):
        """Deactivating non-existent activation returns 404."""
        await _setup_test_data(pg_session)

        response = await client_with_db.delete("/api/v1/skills/activations/99999")
        assert response.status_code == 404
