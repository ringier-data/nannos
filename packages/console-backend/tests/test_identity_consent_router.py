"""Integration tests for PUT /me/settings/identity-consent endpoint (ADR 0006 Gate 3)."""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest


@pytest.mark.asyncio
class TestIdentityConsentEndpoint:
    """Test PUT /api/v1/auth/me/settings/identity-consent."""

    async def test_grant_consent(self, client_with_db):
        """Granting consent records granted=True under tool::server."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "salesforce_create_note", "server_slug": "gatana-salesforce", "granted": True},
        )

        assert response.status_code == 200
        grants = response.json()["identity_consent_grants"]
        assert grants["salesforce_create_note::gatana-salesforce"] == {"granted": True}

    async def test_deny_consent(self, client_with_db):
        """A remembered denial is stored as granted=False (blocked, no re-prompt)."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "salesforce_create_note", "server_slug": "gatana-salesforce", "granted": False},
        )

        assert response.status_code == 200
        grants = response.json()["identity_consent_grants"]
        assert grants["salesforce_create_note::gatana-salesforce"] == {"granted": False}

    async def test_default_server_slug_is_self(self, client_with_db):
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "my_tool", "granted": True},
        )

        assert response.status_code == 200
        assert "my_tool::_self" in response.json()["identity_consent_grants"]

    async def test_remove_resets_answer(self, client_with_db):
        """remove=True clears a remembered answer so the gate prompts again."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "salesforce_create_note", "server_slug": "gatana-salesforce", "granted": False},
        )

        response = await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "salesforce_create_note", "server_slug": "gatana-salesforce", "remove": True},
        )

        assert response.status_code == 200
        assert "salesforce_create_note::gatana-salesforce" not in response.json()["identity_consent_grants"]

    async def test_grants_persist_and_surface_in_settings(self, client_with_db):
        """Consent grants ride on GET /me/settings alongside tool_bypass_rules."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "salesforce_create_note", "server_slug": "gatana-salesforce", "granted": True},
        )

        response = await client_with_db.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        grants = response.json()["data"]["identity_consent_grants"]
        assert grants["salesforce_create_note::gatana-salesforce"] == {"granted": True}

    async def test_separate_from_tool_bypass_rules(self, client_with_db):
        """Consent grants never leak into tool_bypass_rules (different axis, ADR 0006)."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/identity-consent",
            json={"tool_name": "salesforce_create_note", "granted": True},
        )

        response = await client_with_db.get("/api/v1/auth/me/settings")

        data = response.json()["data"]
        assert "salesforce_create_note::_self" not in data["tool_bypass_rules"]
