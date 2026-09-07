"""Per-user entitlement version: the stamp the orchestrator keys its discovery cache on.

The contract is behavioural, not structural: the version must be stable while nothing
relevant changes, and must move for every kind of entitlement change the orchestrator
caches the result of — including the one that previously slipped through (activating a
sub-agent for the orchestrator).
"""

import pytest
from sqlalchemy import text

from console_backend.services.entitlement_version import (
    compute_entitlement_version,
    touch_group_member_entitlements,
)


async def _create_group(pg_session, name: str) -> int:
    row = await pg_session.execute(
        text("INSERT INTO user_groups (name) VALUES (:name) RETURNING id"),
        {"name": name},
    )
    return row.scalar_one()


async def _create_sub_agent(pg_session, owner_id: str, name: str) -> int:
    row = await pg_session.execute(
        text(
            "INSERT INTO sub_agents (name, owner_user_id, type, default_version) "
            "VALUES (:name, :owner, 'local', 1) RETURNING id"
        ),
        {"name": name, "owner": owner_id},
    )
    return row.scalar_one()


@pytest.mark.asyncio
class TestComputeEntitlementVersion:
    async def test_stable_when_nothing_changes(self, pg_session, test_user_db):
        v1 = await compute_entitlement_version(pg_session, test_user_db.id)
        v2 = await compute_entitlement_version(pg_session, test_user_db.id)
        assert v1 is not None
        assert v1 == v2

    async def test_none_for_unknown_user(self, pg_session):
        assert await compute_entitlement_version(pg_session, "no-such-user") is None

    async def test_moves_on_sub_agent_activation_and_deactivation(self, pg_session, test_user_db):
        sub_agent_id = await _create_sub_agent(pg_session, test_user_db.id, "demo-agent")
        before = await compute_entitlement_version(pg_session, test_user_db.id)

        await pg_session.execute(
            text("INSERT INTO user_sub_agent_activations (user_id, sub_agent_id) VALUES (:u, :s)"),
            {"u": test_user_db.id, "s": sub_agent_id},
        )
        activated = await compute_entitlement_version(pg_session, test_user_db.id)
        assert activated != before

        await pg_session.execute(
            text("DELETE FROM user_sub_agent_activations WHERE user_id = :u AND sub_agent_id = :s"),
            {"u": test_user_db.id, "s": sub_agent_id},
        )
        deactivated = await compute_entitlement_version(pg_session, test_user_db.id)
        assert deactivated != activated

    async def test_moves_when_activated_agent_gets_new_default_version(self, pg_session, test_user_db):
        sub_agent_id = await _create_sub_agent(pg_session, test_user_db.id, "versioned-agent")
        await pg_session.execute(
            text("INSERT INTO user_sub_agent_activations (user_id, sub_agent_id) VALUES (:u, :s)"),
            {"u": test_user_db.id, "s": sub_agent_id},
        )
        before = await compute_entitlement_version(pg_session, test_user_db.id)
        await pg_session.execute(
            text("UPDATE sub_agents SET default_version = 2 WHERE id = :s"),
            {"s": sub_agent_id},
        )
        assert await compute_entitlement_version(pg_session, test_user_db.id) != before

    async def test_moves_when_live_version_content_changes(self, pg_session, test_user_db):
        # The orchestrator runs the default version's content (tools, prompt); an in-place
        # change to it is a content-hash change, which must move the stamp.
        sub_agent_id = await _create_sub_agent(pg_session, test_user_db.id, "edited-agent")
        await pg_session.execute(
            text(
                "INSERT INTO sub_agent_config_versions (sub_agent_id, version, version_hash, description, status, system_prompt) "
                "VALUES (:s, 1, 'aaaaaaaaaaaa', 'd', 'approved', 'You are a demo agent.')"
            ),
            {"s": sub_agent_id},
        )
        await pg_session.execute(
            text("INSERT INTO user_sub_agent_activations (user_id, sub_agent_id) VALUES (:u, :s)"),
            {"u": test_user_db.id, "s": sub_agent_id},
        )
        before = await compute_entitlement_version(pg_session, test_user_db.id)
        await pg_session.execute(
            text("UPDATE sub_agent_config_versions SET version_hash = 'bbbbbbbbbbbb' WHERE sub_agent_id = :s"),
            {"s": sub_agent_id},
        )
        assert await compute_entitlement_version(pg_session, test_user_db.id) != before

    async def test_moves_on_group_membership_and_group_default_agent(self, pg_session, test_user_db):
        group_id = await _create_group(pg_session, "ev-group")
        before = await compute_entitlement_version(pg_session, test_user_db.id)

        await pg_session.execute(
            text("INSERT INTO user_group_members (user_id, user_group_id) VALUES (:u, :g)"),
            {"u": test_user_db.id, "g": group_id},
        )
        joined = await compute_entitlement_version(pg_session, test_user_db.id)
        assert joined != before

        sub_agent_id = await _create_sub_agent(pg_session, test_user_db.id, "group-default")
        await pg_session.execute(
            text(
                "INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id) "
                "VALUES (:g, :s, :u)"
            ),
            {"g": group_id, "s": sub_agent_id, "u": test_user_db.id},
        )
        assert await compute_entitlement_version(pg_session, test_user_db.id) != joined

    async def test_moves_on_role_and_settings_change(self, pg_session, test_user_db):
        before = await compute_entitlement_version(pg_session, test_user_db.id)
        await pg_session.execute(
            text("UPDATE users SET role = 'approver' WHERE id = :u"),
            {"u": test_user_db.id},
        )
        role_changed = await compute_entitlement_version(pg_session, test_user_db.id)
        assert role_changed != before

        await pg_session.execute(
            text("INSERT INTO user_settings (user_id, mcp_tools) VALUES (:u, '[\"a_tool\"]'::jsonb)"),
            {"u": test_user_db.id},
        )
        assert await compute_entitlement_version(pg_session, test_user_db.id) != role_changed

    async def test_touch_group_members_moves_only_members(self, pg_session, test_user_db, test_admin_user_db):
        group_id = await _create_group(pg_session, "touch-group")
        await pg_session.execute(
            text("INSERT INTO user_group_members (user_id, user_group_id) VALUES (:u, :g)"),
            {"u": test_user_db.id, "g": group_id},
        )
        member_before = await compute_entitlement_version(pg_session, test_user_db.id)
        outsider_before = await compute_entitlement_version(pg_session, test_admin_user_db.id)

        touched = await touch_group_member_entitlements(pg_session, group_id)

        assert touched == 1
        assert await compute_entitlement_version(pg_session, test_user_db.id) != member_before
        assert await compute_entitlement_version(pg_session, test_admin_user_db.id) == outsider_before


@pytest.mark.asyncio
class TestEntitlementVersionEndpoint:
    async def test_returns_opaque_version(self, client_with_db, pg_session, test_user_model):
        response = await client_with_db.get("/api/v1/auth/me/entitlement-version")
        assert response.status_code == 200
        version = response.json()["version"]
        assert isinstance(version, str) and version
        assert version == await compute_entitlement_version(pg_session, test_user_model.id)
