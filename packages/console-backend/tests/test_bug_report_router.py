"""Tests for bug report router (Phase 1)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.dependencies import require_auth

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Bug reports have no FK to conversations — no need to create them.


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_bug_report(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    response = await client_with_db.post(
        "/api/v1/bug-reports",
        json={
            "conversation_id": "conv-1",
            "description": "Something broke",
            "source": "client",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["conversation_id"] == "conv-1"
    assert data["description"] == "Something broke"
    assert data["source"] == "client"
    assert data["status"] == "open"
    assert data["user_id"] == test_user_model.id

    # Verify it's actually in the database
    result = await pg_session.execute(text("SELECT * FROM bug_reports WHERE id = :id"), {"id": data["id"]})
    row = result.mappings().first()
    assert row is not None
    assert row["description"] == "Something broke"


@pytest.mark.asyncio
async def test_create_bug_report_orchestrator_source(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    response = await client_with_db.post(
        "/api/v1/bug-reports",
        json={
            "conversation_id": "conv-1",
            "message_id": "msg-42",
            "description": "Orchestrator detected failure",
            "source": "orchestrator",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["source"] == "orchestrator"
    assert data["message_id"] == "msg-42"


@pytest.mark.asyncio
async def test_create_bug_report_minimal(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Minimal report with only required fields."""
    response = await client_with_db.post(
        "/api/v1/bug-reports",
        json={"conversation_id": "conv-1"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["source"] == "client"
    assert data["description"] is None
    assert data["message_id"] is None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_bug_reports_user_sees_own_only(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    # Create two reports for the test user
    for i in range(2):
        await client_with_db.post(
            "/api/v1/bug-reports",
            json={"conversation_id": "conv-1", "description": f"Report {i}"},
        )

    # Create a report for a different user (direct SQL to bypass auth)
    await pg_session.execute(
        text("INSERT INTO users (id, sub, email, first_name, last_name) VALUES (:id, :sub, :email, 'Other', 'User')"),
        {"id": "other-user-id", "sub": "other-sub", "email": "other@example.com"},
    )

    await pg_session.execute(
        text(
            "INSERT INTO bug_reports (conversation_id, user_id, source, status) "
            "VALUES ('conv-other', 'other-user-id', 'client', 'open')"
        ),
    )
    await pg_session.commit()

    response = await client_with_db.get("/api/v1/bug-reports")
    assert response.status_code == 200
    data = response.json()
    # Non-admin user should only see their own reports
    assert data["meta"]["total"] == 2
    assert all(r["user_id"] == test_user_model.id for r in data["data"])


@pytest.mark.asyncio
async def test_list_bug_reports_status_filter(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    await client_with_db.post("/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Open 1"})
    await client_with_db.post("/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Open 2"})

    response = await client_with_db.get("/api/v1/bug-reports?status_filter=open")
    assert response.status_code == 200
    data = response.json()
    assert data["meta"]["total"] == 2

    response = await client_with_db.get("/api/v1/bug-reports?status_filter=resolved")
    assert response.status_code == 200
    data = response.json()
    assert data["meta"]["total"] == 0


# ---------------------------------------------------------------------------
# Get single
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_bug_report(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    create_resp = await client_with_db.post(
        "/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Detailed issue"}
    )
    report_id = create_resp.json()["id"]

    response = await client_with_db.get(f"/api/v1/bug-reports/{report_id}")
    assert response.status_code == 200
    assert response.json()["description"] == "Detailed issue"


@pytest.mark.asyncio
async def test_get_bug_report_not_found(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    response = await client_with_db.get("/api/v1/bug-reports/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Status update (RBAC: triage capability or self-resolve)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_status_as_admin(
    app_with_db, client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """User with triage capability can update bug report status."""
    from datetime import datetime, timezone

    from console_backend.models.user import User, UserRole, UserStatus

    approver_user = User(
        id=test_user_model.id,
        sub=test_user_model.sub,
        email=test_user_model.email,
        first_name="Approver",
        last_name="User",
        is_administrator=False,
        role=UserRole.APPROVER,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    # Override require_auth to return approver user (has triage capability)
    app_with_db.dependency_overrides[require_auth] = lambda: approver_user

    try:
        create_resp = await client_with_db.post(
            "/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "To be resolved"}
        )
        report_id = create_resp.json()["id"]

        # Update to acknowledged
        response = await client_with_db.patch(
            f"/api/v1/bug-reports/{report_id}/status",
            json={"status": "acknowledged"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "acknowledged"

        # Update to resolved
        response = await client_with_db.patch(
            f"/api/v1/bug-reports/{report_id}/status",
            json={"status": "resolved"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "resolved"

        # Verify in DB
        result = await pg_session.execute(text("SELECT status FROM bug_reports WHERE id = :id"), {"id": report_id})
        assert result.scalar() == "resolved"
    finally:
        app_with_db.dependency_overrides.pop(require_auth, None)


@pytest.mark.asyncio
async def test_update_status_audit_log(
    app_with_db, client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Status update should create an audit log entry."""
    from datetime import datetime, timezone

    from console_backend.models.user import User, UserRole, UserStatus

    approver_user = User(
        id=test_user_model.id,
        sub=test_user_model.sub,
        email=test_user_model.email,
        first_name="Approver",
        last_name="User",
        is_administrator=False,
        role=UserRole.APPROVER,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    app_with_db.dependency_overrides[require_auth] = lambda: approver_user

    try:
        create_resp = await client_with_db.post(
            "/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Audit test"}
        )
        report_id = create_resp.json()["id"]

        await client_with_db.patch(f"/api/v1/bug-reports/{report_id}/status", json={"status": "acknowledged"})

        # Verify audit log
        result = await pg_session.execute(
            text(
                "SELECT * FROM audit_logs WHERE entity_type = 'bug_report' AND entity_id = :eid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"eid": report_id},
        )
        audit_log = result.mappings().first()
        assert audit_log is not None
        assert audit_log["action"] == "update"
    finally:
        app_with_db.dependency_overrides.pop(require_auth, None)
