"""Integration tests for PUT /me/settings/identity-consent endpoint (ADR 0006 Gate 3)."""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest


@pytest.mark.asyncio
class TestIdentityConsentEndpoint:
    """Test PUT /api/v1/auth/me/settings/identity-consent."""

    async def test_grant_consent(self, client_with_db):
        """Granting consent records granted=True keyed by MCP server slug."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "granted": True},
        )

        assert response.status_code == 200
        grants = response.json()["identity_consent_grants"]
        assert grants["gatana-salesforce"] == {"granted": True}

    async def test_deny_consent(self, client_with_db):
        """A remembered denial is stored as granted=False (blocked, no re-prompt)."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "granted": False},
        )

        assert response.status_code == 200
        grants = response.json()["identity_consent_grants"]
        assert grants["gatana-salesforce"] == {"granted": False}

    async def test_keyed_by_server_slug_alone(self, client_with_db):
        """Keys are bare server slugs — no tool::server compound (unlike bypass rules)."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-github", "granted": True},
        )

        assert response.status_code == 200
        assert "gatana-github" in response.json()["identity_consent_grants"]

    async def test_remove_resets_answer(self, client_with_db):
        """remove=True clears a remembered answer so the gate prompts again."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "granted": False},
        )

        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "remove": True},
        )

        assert response.status_code == 200
        assert "gatana-salesforce" not in response.json()["identity_consent_grants"]

    async def test_grants_persist_and_surface_in_settings(self, client_with_db):
        """Consent grants ride on GET /me/settings alongside tool_bypass_rules."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "granted": True},
        )

        response = await client_with_db.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        grants = response.json()["data"]["identity_consent_grants"]
        assert grants["gatana-salesforce"] == {"granted": True}

    async def test_separate_from_tool_bypass_rules(self, client_with_db):
        """Consent grants never leak into tool_bypass_rules (different axis, ADR 0006)."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "granted": True},
        )

        response = await client_with_db.get("/api/v1/auth/me/settings")

        data = response.json()["data"]
        assert not any("gatana-salesforce" in key for key in data["tool_bypass_rules"])

    async def test_grant_is_audited(self, client_with_db, pg_session):
        """Whitelisting an integration must be reconstructable from the audit trail."""
        from sqlalchemy import text

        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-salesforce", "granted": True},
        )

        row = (
            await pg_session.execute(
                text(
                    "SELECT * FROM audit_logs WHERE entity_type = 'mcp_server' "
                    "AND entity_id = :slug ORDER BY created_at DESC LIMIT 1"
                ),
                {"slug": "gatana-salesforce"},
            )
        ).mappings().first()

        assert row is not None
        assert row["action"] == "approve"
        assert row["changes"]["identity_consent"] == {
            "before": None,
            "after": {"granted": True},
        }

    async def test_denial_and_removal_are_audited_with_previous_state(self, client_with_db, pg_session):
        """A denial logs `reject`; forgetting the answer logs `revoke` with what it was."""
        from sqlalchemy import text

        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-github", "granted": False},
        )
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"server_slug": "gatana-github", "remove": True},
        )

        rows = (
            await pg_session.execute(
                text(
                    "SELECT * FROM audit_logs WHERE entity_type = 'mcp_server' "
                    "AND entity_id = :slug ORDER BY created_at"
                ),
                {"slug": "gatana-github"},
            )
        ).mappings().all()

        assert [r["action"] for r in rows] == ["reject", "revoke"]
        assert rows[1]["changes"]["identity_consent"] == {
            "before": {"granted": False},
            "after": None,
        }
