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


# ---------------------------------------------------------------------------
# created_after filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_bug_reports_created_after(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """created_after is strictly greater than, so the boundary report is excluded."""
    older = (
        await client_with_db.post(
            "/api/v1/bug-reports",
            json={"conversation_id": "conv-1", "description": "Older"},
        )
    ).json()
    newer = (
        await client_with_db.post(
            "/api/v1/bug-reports",
            json={"conversation_id": "conv-1", "description": "Newer"},
        )
    ).json()

    response = await client_with_db.get("/api/v1/bug-reports", params={"created_after": older["created_at"]})
    assert response.status_code == 200
    data = response.json()
    assert data["meta"]["total"] == 1
    assert [r["id"] for r in data["data"]] == [newer["id"]]

    # A window starting after the newest report returns nothing — the shape a watch
    # sees on a quiet poll.
    response = await client_with_db.get("/api/v1/bug-reports", params={"created_after": newer["created_at"]})
    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 0


@pytest.mark.asyncio
async def test_created_after_without_an_offset_is_read_as_utc(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model, monkeypatch
):
    """An offset-less timestamp must not be shifted by the server's local timezone.

    asyncpg encodes a naive datetime in the *process's* timezone against a TIMESTAMPTZ
    column, so without normalisation the window moves by the deployment's UTC offset.

    The process timezone is pinned for the duration, because on a UTC runner the bug is
    invisible: "read the naive value as local" and "read it as UTC" are then the same
    thing, and the test would pass with the normalisation deleted. UTC+14 without DST
    makes the two readings 14 hours apart — enough that the unfixed code returns both
    reports where the fixed code returns one.
    """
    import time
    from datetime import datetime, timezone

    older = (
        await client_with_db.post("/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Older"})
    ).json()
    newer = (
        await client_with_db.post("/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Newer"})
    ).json()

    # The same instant as `older["created_at"]`, expressed without an offset.
    boundary = datetime.fromisoformat(older["created_at"]).astimezone(timezone.utc).replace(tzinfo=None)

    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14, no DST
    time.tzset()
    try:
        response = await client_with_db.get("/api/v1/bug-reports", params={"created_after": boundary.isoformat()})
        assert response.status_code == 200
        # Read as local time the bound would be 14 hours earlier, taking in both reports.
        assert [r["id"] for r in response.json()["data"]] == [newer["id"]]
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.asyncio
async def test_list_rejects_out_of_range_paging(client_with_db: AsyncClient, test_user_model):
    """`page=0` used to reach the repository as a negative OFFSET and answer 500."""
    for params in ({"page": 0}, {"page": -1}, {"limit": 0}, {"limit": -1}, {"limit": 101}):
        response = await client_with_db.get("/api/v1/bug-reports", params=params)
        assert response.status_code == 422, f"{params} should be rejected, got {response.status_code}"

        response = await client_with_db.get("/api/v1/bug-reports/mcp-list", params=params)
        assert response.status_code == 422, f"{params} should be rejected on the MCP twin too"


# ---------------------------------------------------------------------------
# MCP list tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_list_bug_reports_sees_own_only(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    await client_with_db.post("/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Mine"})

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

    response = await client_with_db.get("/api/v1/bug-reports/mcp-list")
    assert response.status_code == 200
    data = response.json()
    assert data["meta"]["total"] == 1
    assert all(r["user_id"] == test_user_model.id for r in data["data"])


@pytest.mark.asyncio
async def test_mcp_list_bug_reports_filters(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    older = (
        await client_with_db.post(
            "/api/v1/bug-reports",
            json={"conversation_id": "conv-1", "description": "Older"},
        )
    ).json()
    newer = (
        await client_with_db.post(
            "/api/v1/bug-reports",
            json={"conversation_id": "conv-1", "description": "Newer"},
        )
    ).json()

    response = await client_with_db.get(
        "/api/v1/bug-reports/mcp-list",
        params={"created_after": older["created_at"], "status_filter": "open"},
    )
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["data"]] == [newer["id"]]

    response = await client_with_db.get("/api/v1/bug-reports/mcp-list", params={"status_filter": "resolved"})
    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 0


@pytest.mark.asyncio
async def test_mcp_and_rest_routes_do_not_shadow_each_other(client_with_db: AsyncClient, test_user_model):
    """Both routers share the /api/v1/bug-reports prefix; registration order decides.

    The MCP router is registered first so its literal `mcp-list` is not read as a report
    id by the REST router's `GET /{report_id}`. Going first is only safe because every
    MCP path is a literal segment — so this asserts the other direction too, that a real
    report id still reaches the REST route.
    """
    response = await client_with_db.get("/api/v1/bug-reports/mcp-list")
    assert response.status_code == 200
    assert "meta" in response.json(), "mcp-list must reach the MCP tool, not get-by-id"

    created = await client_with_db.post(
        "/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Fetch me by id"}
    )
    report_id = created.json()["id"]

    response = await client_with_db.get(f"/api/v1/bug-reports/{report_id}")
    assert response.status_code == 200
    assert response.json()["description"] == "Fetch me by id", "a report id must still reach the REST route"


# ---------------------------------------------------------------------------
# Notifying triagers on filing
# ---------------------------------------------------------------------------


async def _insert_user(pg_session: AsyncSession, user_id: str, role: str, is_admin: bool = False) -> str:
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, is_administrator)
            VALUES (:id, :id, :email, 'Test', 'User', :role, :is_admin)
            ON CONFLICT (id) DO UPDATE SET role = :role, is_administrator = :is_admin
        """),
        {
            "id": user_id,
            "email": f"{user_id}@example.com",
            "role": role,
            "is_admin": is_admin,
        },
    )
    await pg_session.commit()
    return user_id


async def _filed_notifications(pg_session: AsyncSession) -> list[dict]:
    result = await pg_session.execute(
        text("SELECT user_id, title, message, metadata FROM user_notifications WHERE type = 'bug_report_filed'")
    )
    return [dict(row) for row in result.mappings().all()]


@pytest.mark.asyncio
async def test_filing_notifies_administrators(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    admin = await _insert_user(pg_session, "admin-id", "admin", is_admin=True)
    plain_member = await _insert_user(pg_session, "member-id", "member")

    create_resp = await client_with_db.post(
        "/api/v1/bug-reports",
        json={
            "conversation_id": "conv-1",
            "description": "The export button does nothing",
        },
    )
    assert create_resp.status_code == 201
    report_id = create_resp.json()["id"]

    notifications = await _filed_notifications(pg_session)
    notified = {n["user_id"] for n in notifications}
    assert notified == {admin}
    assert plain_member not in notified
    # The reporter is never told about their own report.
    assert test_user_model.id not in notified

    assert notifications[0]["message"] == "The export button does nothing"
    assert notifications[0]["metadata"]["bug_report_id"] == report_id
    assert notifications[0]["metadata"]["reported_by"] == test_user_model.id


@pytest.mark.asyncio
async def test_filing_does_not_notify_a_non_admin_triager(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """The audience matches read visibility, not the `triage` capability.

    An approver holds `triage` and may change a report's status, but every read path is
    administrator-or-owner — so a notification would hand them a description snippet for
    a report they cannot open.
    """
    await _insert_user(pg_session, "approver-id", "approver")
    admin = await _insert_user(pg_session, "admin-id", "admin", is_admin=True)

    await client_with_db.post(
        "/api/v1/bug-reports",
        json={"conversation_id": "conv-1", "description": "Approver must not see this"},
    )

    notified = {n["user_id"] for n in await _filed_notifications(pg_session)}
    assert notified == {admin}


@pytest.mark.asyncio
async def test_filing_excludes_the_reporting_administrator(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """An administrator filing a report is not notified about it, but their peers are."""
    await _insert_user(pg_session, test_user_model.id, "admin", is_admin=True)
    peer = await _insert_user(pg_session, "admin-peer-id", "admin", is_admin=True)

    await client_with_db.post(
        "/api/v1/bug-reports",
        json={"conversation_id": "conv-1", "description": "Self-filed"},
    )

    notified = {n["user_id"] for n in await _filed_notifications(pg_session)}
    assert notified == {peer}


@pytest.mark.asyncio
async def test_filing_never_notifies_the_system_user(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """The seeded 'system' account must never collect notifications.

    Migration 041 creates it with role 'admin' and `is_administrator` FALSE, but that
    default is not what deployments contain: a live one has the account promoted, and it
    was collecting these notifications while the audience relied on the flag alone. So
    the account is promoted here first — this asserts the exclusion, not the seed.
    """
    await pg_session.execute(text("UPDATE users SET is_administrator = TRUE WHERE id = 'system'"))
    await pg_session.commit()

    await client_with_db.post("/api/v1/bug-reports", json={"conversation_id": "conv-1", "description": "Noise check"})

    notified = {n["user_id"] for n in await _filed_notifications(pg_session)}
    assert "system" not in notified, "'system' owns auto-provisioned agents; no person reads its inbox"


@pytest.mark.asyncio
async def test_filing_survives_a_failing_notification(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model, app_with_db
):
    """A notification failure must not lose the bug report."""
    from unittest.mock import AsyncMock, patch

    # An administrator, so there is a recipient and the patched insert is actually
    # reached — with no recipients the fan-out returns before touching it.
    await _insert_user(pg_session, "admin-id", "admin", is_admin=True)

    with patch.object(
        app_with_db.state.notification_service,
        "bulk_create_notifications",
        AsyncMock(side_effect=RuntimeError("inbox is down")),
    ):
        response = await client_with_db.post(
            "/api/v1/bug-reports",
            json={"conversation_id": "conv-1", "description": "Still filed"},
        )

    assert response.status_code == 201
    result = await pg_session.execute(
        text("SELECT description FROM bug_reports WHERE id = :id"),
        {"id": response.json()["id"]},
    )
    assert result.scalar() == "Still filed"
    assert await _filed_notifications(pg_session) == []
