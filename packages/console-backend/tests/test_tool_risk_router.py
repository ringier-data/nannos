"""Tests for tool risk scores router.

Validates auth behavior (admin-mode vs orchestrator service identity),
CRUD semantics, pagination response shape, and error handling.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.dependencies import (
    require_admin,
    require_admin_or_orchestrator,
    require_auth_or_bearer_token,
)
from console_backend.models.user import User, UserRole, UserStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# The tool risk service is wired onto ``app.state.tool_risk_service`` (with its repository
# and audit service) by ``initialize_services`` during the app lifespan — see
# ``service_instances.py`` — and the router resolves it from ``request.app.state``. The
# ``client_with_db`` fixture runs that lifespan, so no manual service injection is needed here.


@pytest.fixture
def admin_user() -> User:
    """Admin user for admin-only endpoints."""
    return User(
        id="admin-id",
        sub="admin-sub",
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
        is_administrator=True,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def orchestrator_user() -> User:
    """Synthetic orchestrator service user."""
    return User(
        id="service:orchestrator_client_id",
        sub="service:orchestrator_client_id",
        email="orchestrator_client_id@service.internal",
        first_name="Orchestrator",
        last_name="Service",
        is_administrator=True,
        role=UserRole.ADMIN,
    )


async def _insert_risk_score(
    pg_session: AsyncSession,
    tool_name: str = "test_tool",
    server_slug: str = "test-server",
    base_score: float = 0.7,
    schema_hash: str = "abc123",
    risk_factors: str = '{"level": "high"}',
    allowed_actions: str = '["approve", "reject"]',
) -> None:
    """Insert a risk score directly into the DB for read tests."""
    await pg_session.execute(
        text("""
            INSERT INTO tool_risk_scores
                (tool_name, server_slug, schema_hash, base_score, risk_factors, allowed_actions, updated_at)
            VALUES
                (:tool_name, :server_slug, :schema_hash, :base_score,
                 CAST(:risk_factors AS jsonb), CAST(:allowed_actions AS jsonb), NOW())
        """),
        {
            "tool_name": tool_name,
            "server_slug": server_slug,
            "schema_hash": schema_hash,
            "base_score": base_score,
            "risk_factors": risk_factors,
            "allowed_actions": allowed_actions,
        },
    )
    await pg_session.commit()


# ===========================================================================
# GET /api/mcp/tools/risk-scores (paginated list)
# ===========================================================================


@pytest.mark.asyncio
class TestListRiskScores:
    """Tests for the paginated list endpoint."""

    async def test_returns_seeded_scores(self, client_with_db: AsyncClient):
        """Returns the seeded static guard scores from the migration."""
        response = await client_with_db.get("/api/mcp/tools/risk-scores")
        assert response.status_code == 200
        data = response.json()
        # Migration seeds 13 static guard rows
        assert data["total"] >= 13
        assert len(data["items"]) >= 13
        assert data["limit"] == 100
        assert data["offset"] == 0

    async def test_returns_scores_with_pagination_shape(self, client_with_db: AsyncClient, pg_session: AsyncSession):
        """Returns scores with correct pagination envelope."""
        await _insert_risk_score(pg_session, tool_name="tool_a", server_slug="srv-1", base_score=0.3)
        await _insert_risk_score(pg_session, tool_name="tool_b", server_slug="srv-2", base_score=0.8)

        response = await client_with_db.get("/api/mcp/tools/risk-scores")
        assert response.status_code == 200
        data = response.json()
        # 13 seeded + 2 inserted
        assert data["total"] == 15
        assert len(data["items"]) == 15
        # Each item has the expected fields
        item = data["items"][0]
        assert "tool_name" in item
        assert "server_slug" in item
        assert "base_score" in item
        assert "schema_hash" in item
        assert "risk_factors" in item
        assert "allowed_actions" in item
        assert "updated_at" in item
        assert "created_at" in item

    async def test_respects_limit_and_offset(self, client_with_db: AsyncClient, pg_session: AsyncSession):
        """Pagination limit and offset are respected."""
        for i in range(5):
            await _insert_risk_score(pg_session, tool_name=f"tool_{i}", server_slug=f"srv-{i}", base_score=0.1 * i)

        # 13 seeded + 5 inserted = 18 total
        response = await client_with_db.get("/api/mcp/tools/risk-scores?limit=2&offset=1")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["total"] == 18
        assert data["limit"] == 2
        assert data["offset"] == 1

    async def test_validates_limit_bounds(self, client_with_db: AsyncClient):
        """Limit must be within [1, 500]."""
        response = await client_with_db.get("/api/mcp/tools/risk-scores?limit=0")
        assert response.status_code == 422

        response = await client_with_db.get("/api/mcp/tools/risk-scores?limit=501")
        assert response.status_code == 422

    async def test_validates_offset_non_negative(self, client_with_db: AsyncClient):
        """Offset must be >= 0."""
        response = await client_with_db.get("/api/mcp/tools/risk-scores?offset=-1")
        assert response.status_code == 422

    async def test_requires_authentication(self, app_with_db, client_with_db: AsyncClient):
        """Endpoint rejects unauthenticated requests."""
        # Remove the auth override so the real dependency runs (returns 401)

        app_with_db.dependency_overrides.pop(require_auth_or_bearer_token, None)

        response = await client_with_db.get("/api/mcp/tools/risk-scores")
        assert response.status_code in (401, 403)


# ===========================================================================
# GET /api/mcp/tools/risk-scores/{tool_name}/{server_slug}
# ===========================================================================


@pytest.mark.asyncio
class TestGetRiskScore:
    """Tests for the single score lookup endpoint."""

    async def test_returns_existing_score(self, client_with_db: AsyncClient, pg_session: AsyncSession):
        """Returns a score when it exists."""
        await _insert_risk_score(
            pg_session,
            tool_name="file_write",
            server_slug="github-mcp",
            base_score=0.9,
            schema_hash="sha256abc",
            risk_factors='{"destructive": true}',
            allowed_actions='["approve", "edit", "reject"]',
        )

        response = await client_with_db.get("/api/mcp/tools/risk-scores/file_write/github-mcp")
        assert response.status_code == 200
        data = response.json()
        assert data["tool_name"] == "file_write"
        assert data["server_slug"] == "github-mcp"
        assert data["base_score"] == 0.9
        assert data["schema_hash"] == "sha256abc"
        assert data["risk_factors"] == {"destructive": True}
        assert data["allowed_actions"] == ["approve", "edit", "reject"]
        assert data["updated_at"] != ""
        assert data["created_at"] != ""

    async def test_returns_404_for_nonexistent_score(self, client_with_db: AsyncClient):
        """Returns 404 when the score doesn't exist."""
        response = await client_with_db.get("/api/mcp/tools/risk-scores/nonexistent/slug")
        assert response.status_code == 404
        assert response.json()["detail"] == "Risk score not found"


# ===========================================================================
# PUT /api/mcp/tools/risk-scores (upsert)
# ===========================================================================


@pytest.mark.asyncio
class TestUpsertRiskScore:
    """Tests for the upsert endpoint (admin or orchestrator only)."""

    async def test_creates_new_score_as_admin(
        self, app_with_db, client_with_db: AsyncClient, admin_user: User, pg_session: AsyncSession
    ):
        """Admin with admin-mode can create a new score."""
        app_with_db.dependency_overrides[require_admin_or_orchestrator] = lambda: admin_user

        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "code_execute",
                "server_slug": "sandbox-mcp",
                "schema_hash": "def456",
                "base_score": 0.95,
                "risk_factors": {"privilege": "high", "side_effects": True},
                "allowed_actions": ["approve", "reject"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tool_name"] == "code_execute"
        assert data["server_slug"] == "sandbox-mcp"
        assert data["base_score"] == 0.95
        assert data["schema_hash"] == "def456"
        assert data["risk_factors"] == {"privilege": "high", "side_effects": True}
        assert data["allowed_actions"] == ["approve", "reject"]

        # Verify persisted in DB
        result = await pg_session.execute(
            text("SELECT base_score FROM tool_risk_scores WHERE tool_name = 'code_execute'")
        )
        row = result.scalar()
        assert float(row) == 0.95

    async def test_updates_existing_score(
        self, app_with_db, client_with_db: AsyncClient, admin_user: User, pg_session: AsyncSession
    ):
        """Upsert updates an existing score."""
        app_with_db.dependency_overrides[require_admin_or_orchestrator] = lambda: admin_user

        await _insert_risk_score(pg_session, tool_name="tool_x", server_slug="srv-x", base_score=0.3)

        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "tool_x",
                "server_slug": "srv-x",
                "schema_hash": "newhash",
                "base_score": 0.85,
                "risk_factors": {"updated": True},
                "allowed_actions": ["approve"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["base_score"] == 0.85
        assert data["schema_hash"] == "newhash"
        assert data["risk_factors"] == {"updated": True}

    async def test_creates_score_as_orchestrator(
        self, app_with_db, client_with_db: AsyncClient, orchestrator_user: User, pg_session: AsyncSession
    ):
        """Orchestrator service identity can create scores."""
        app_with_db.dependency_overrides[require_admin_or_orchestrator] = lambda: orchestrator_user

        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "web_search",
                "server_slug": "brave-mcp",
                "schema_hash": "hash789",
                "base_score": 0.2,
                "risk_factors": {},
                "allowed_actions": ["approve", "edit", "reject"],
            },
        )
        assert response.status_code == 200
        assert response.json()["tool_name"] == "web_search"

    async def test_rejects_non_admin_non_orchestrator(
        self, app_with_db, client_with_db: AsyncClient, test_user_model: User
    ):
        """Regular member cannot upsert scores."""
        from fastapi import HTTPException
        from fastapi import status as http_status

        def deny():
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Forbidden")

        app_with_db.dependency_overrides[require_admin_or_orchestrator] = deny

        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "denied_tool",
                "server_slug": "srv",
                "base_score": 0.5,
            },
        )
        assert response.status_code == 403

    async def test_validates_base_score_range(self, app_with_db, client_with_db: AsyncClient, admin_user: User):
        """base_score must be between 0.0 and 1.0."""
        app_with_db.dependency_overrides[require_admin_or_orchestrator] = lambda: admin_user

        # Too high
        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "t",
                "server_slug": "s",
                "base_score": 1.5,
            },
        )
        assert response.status_code == 422

        # Negative
        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "t",
                "server_slug": "s",
                "base_score": -0.1,
            },
        )
        assert response.status_code == 422

    async def test_validates_required_fields(self, app_with_db, client_with_db: AsyncClient, admin_user: User):
        """tool_name, server_slug, and base_score are required."""
        app_with_db.dependency_overrides[require_admin_or_orchestrator] = lambda: admin_user

        # Missing tool_name
        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={"server_slug": "srv", "base_score": 0.5},
        )
        assert response.status_code == 422

        # Missing base_score
        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={"tool_name": "t", "server_slug": "s"},
        )
        assert response.status_code == 422

    async def test_default_values_applied(self, app_with_db, client_with_db: AsyncClient, admin_user: User):
        """Defaults are applied for optional fields."""
        app_with_db.dependency_overrides[require_admin_or_orchestrator] = lambda: admin_user

        response = await client_with_db.put(
            "/api/mcp/tools/risk-scores",
            json={
                "tool_name": "minimal_tool",
                "server_slug": "minimal-srv",
                "base_score": 0.5,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["schema_hash"] == ""
        assert data["risk_factors"] == {}
        assert data["allowed_actions"] == ["approve", "edit", "reject"]


# ===========================================================================
# DELETE /api/mcp/tools/risk-scores/{tool_name}/{server_slug}
# ===========================================================================


@pytest.mark.asyncio
class TestDeleteRiskScore:
    """Tests for the delete endpoint (admin only)."""

    async def test_deletes_existing_score(
        self, app_with_db, client_with_db: AsyncClient, admin_user: User, pg_session: AsyncSession
    ):
        """Admin can delete an existing score."""
        app_with_db.dependency_overrides[require_admin] = lambda: admin_user

        await _insert_risk_score(pg_session, tool_name="to_delete", server_slug="srv-del")

        response = await client_with_db.delete("/api/mcp/tools/risk-scores/to_delete/srv-del")
        assert response.status_code == 204

        # Verify gone from DB
        result = await pg_session.execute(text("SELECT COUNT(*) FROM tool_risk_scores WHERE tool_name = 'to_delete'"))
        assert result.scalar() == 0

    async def test_returns_404_for_nonexistent(self, app_with_db, client_with_db: AsyncClient, admin_user: User):
        """Delete returns 404 if score doesn't exist."""
        app_with_db.dependency_overrides[require_admin] = lambda: admin_user

        response = await client_with_db.delete("/api/mcp/tools/risk-scores/nope/nope")
        assert response.status_code == 404
        assert response.json()["detail"] == "Risk score not found"

    async def test_rejects_non_admin(self, app_with_db, client_with_db: AsyncClient):
        """Non-admin users cannot delete scores."""
        from fastapi import HTTPException
        from fastapi import status as http_status

        def deny():
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Forbidden")

        app_with_db.dependency_overrides[require_admin] = deny

        response = await client_with_db.delete("/api/mcp/tools/risk-scores/any/any")
        assert response.status_code == 403

    async def test_orchestrator_cannot_delete(self, app_with_db, client_with_db: AsyncClient):
        """Orchestrator service identity cannot delete scores (admin only)."""
        from fastapi import HTTPException
        from fastapi import status as http_status

        # require_admin is used (not require_admin_or_orchestrator), so orchestrator is blocked
        def deny():
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Forbidden")

        app_with_db.dependency_overrides[require_admin] = deny

        response = await client_with_db.delete("/api/mcp/tools/risk-scores/tool/slug")
        assert response.status_code == 403
