"""Tests for the onboarding state administrators see on users and group members (#258).

Four people cover the states that matter: provisioned over SCIM and never signed in,
signed in but with no vaulted offline token, signed in with a token Keycloak has since
refused, and fully onboarded. Every listing that shows people to an administrator must
report the same flags for each.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from console_backend.models.user import UserOnboarding, placeholder_sub
from console_backend.repositories.user_group_repository import UserGroupRepository
from console_backend.services.user_group_service import UserGroupService
from console_backend.services.user_service import UserService
from sqlalchemy import text

GROUP_ID = 1

EXPECTED = {
    "scim-user": UserOnboarding(signed_in=False, scheduler_ready=False, sign_in_expired=False),
    "chat-only-user": UserOnboarding(signed_in=True, scheduler_ready=False, sign_in_expired=False),
    "expired-user": UserOnboarding(signed_in=True, scheduler_ready=False, sign_in_expired=True),
    "ready-user": UserOnboarding(signed_in=True, scheduler_ready=True, sign_in_expired=False),
}


@pytest_asyncio.fixture
async def people(pg_session):
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description, created_at, updated_at)
            VALUES (:id, 'Sales', 'Test group', NOW(), NOW())
        """),
        {"id": GROUP_ID},
    )
    for user_id, sub in [
        ("scim-user", placeholder_sub("scim-user")),
        ("chat-only-user", "idp-sub-chat"),
        ("expired-user", "idp-sub-expired"),
        ("ready-user", "idp-sub-ready"),
    ]:
        await pg_session.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
                VALUES (:id, :sub, :email, 'Test', :id, 'member', 'active', NOW(), NOW())
            """),
            {"id": user_id, "sub": sub, "email": f"{user_id}@example.com"},
        )
        await pg_session.execute(
            text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, :u, 'read')"),
            {"g": GROUP_ID, "u": user_id},
        )
    await pg_session.execute(
        text("INSERT INTO user_offline_tokens (user_id, encrypted_token) VALUES ('ready-user', '\\x00')"),
    )
    # Marked the way SchedulerTokenService.mark_expired marks a token Keycloak refused.
    await pg_session.execute(
        text(
            "INSERT INTO user_offline_tokens (user_id, encrypted_token, expired_at) "
            "VALUES ('expired-user', '\\x00', NOW())"
        ),
    )
    await pg_session.commit()


@pytest.fixture
def group_service():
    repo = UserGroupRepository()
    repo.set_audit_service(AsyncMock())
    return UserGroupService(
        user_group_repository=repo,
        keycloak_admin_service=AsyncMock(),
        notification_service=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_list_users_reports_onboarding(pg_session, people):
    users, _ = await UserService().list_users(pg_session, group_id=GROUP_ID)

    assert {u.id: u.onboarding for u in users} == EXPECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", list(EXPECTED))
async def test_user_detail_reports_onboarding(pg_session, people, user_id):
    user = await UserService().get_user_with_groups(pg_session, user_id)

    assert user is not None
    assert user.onboarding == EXPECTED[user_id]


@pytest.mark.asyncio
async def test_group_members_report_onboarding(pg_session, people, group_service):
    members, total = await group_service.list_members(pg_session, GROUP_ID)

    assert total == 4
    assert {m.user_id: m.onboarding for m in members} == EXPECTED


@pytest.mark.asyncio
async def test_group_detail_members_report_onboarding(pg_session, people, group_service):
    group = await group_service.get_group_with_members(pg_session, GROUP_ID)

    assert group is not None
    assert {m.user_id: m.onboarding for m in group.members} == EXPECTED


@pytest.mark.asyncio
async def test_first_sign_in_flips_scheduler_ready(pg_session, people, group_service):
    """Vaulting a token is what a sign-in does; the next listing must reflect it."""
    await pg_session.execute(
        text("INSERT INTO user_offline_tokens (user_id, encrypted_token) VALUES ('chat-only-user', '\\x00')"),
    )
    await pg_session.commit()

    members, _ = await group_service.list_members(pg_session, GROUP_ID)

    assert {m.user_id: m.onboarding for m in members}["chat-only-user"] == EXPECTED["ready-user"]


@pytest.mark.asyncio
async def test_service_account_has_no_onboarding(pg_session, people, group_service):
    """A machine identity never signs in interactively, so every listing reports none,
    whether it is reached through the user pages or as a group member."""
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status, is_service_account,
                               created_at, updated_at)
            VALUES ('svc-user', 'service-account-client', 'svc@example.com', 'Svc', 'Account', 'member',
                    'active', true, NOW(), NOW())
        """),
    )
    await pg_session.execute(
        text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, 'svc-user', 'read')"),
        {"g": GROUP_ID},
    )
    await pg_session.commit()

    users, _ = await UserService().list_users(pg_session, group_id=GROUP_ID)
    detail = await UserService().get_user_with_groups(pg_session, "svc-user")
    members, _ = await group_service.list_members(pg_session, GROUP_ID)
    group = await group_service.get_group_with_members(pg_session, GROUP_ID)

    assert {u.id: u.onboarding for u in users}["svc-user"] is None
    assert detail is not None and detail.onboarding is None
    assert {m.user_id: m.onboarding for m in members}["svc-user"] is None
    assert group is not None and {m.user_id: m.onboarding for m in group.members}["svc-user"] is None
