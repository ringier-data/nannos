"""Tests for the SCIM placeholder subject and its Keycloak consequences.

A SCIM-provisioned user has no Keycloak account until their first OIDC login, so
`ScimUserService.create_user` parks the row's own id in `users.sub`. Group membership must
therefore skip Keycloak for such a user (rather than 500 on `404 User not found`) and
reconcile once the real subject arrives.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from console_backend.models.user import has_idp_identity
from console_backend.repositories.user_group_repository import UserGroupRepository
from console_backend.services.user_group_service import UserGroupService
from sqlalchemy import text


@pytest_asyncio.fixture
async def mock_keycloak_service():
    """Create a mocked KeycloakAdminService."""
    mock = AsyncMock()
    mock.create_group = AsyncMock(return_value="kc-group-123")
    mock.add_user_to_group = AsyncMock()
    mock.remove_user_from_group = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def user_group_service_with_keycloak(mock_keycloak_service):
    """Create UserGroupService with mocked Keycloak, audit and notification services."""
    from console_backend.services.audit_service import AuditService

    repo = UserGroupRepository()
    mock_audit = AsyncMock(spec=AuditService)
    repo.set_audit_service(mock_audit)

    service = UserGroupService(
        user_group_repository=repo,
        keycloak_admin_service=mock_keycloak_service,
        notification_service=AsyncMock(),
    )
    return service


async def _create_group(pg_session, group_id: int = 1, keycloak_group_id: str | None = "kc-group-123"):
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description, keycloak_group_id, created_at, updated_at)
            VALUES (:id, :name, 'Test group', :kc_id, NOW(), NOW())
        """),
        {"id": group_id, "name": f"Group {group_id}", "kc_id": keycloak_group_id},
    )
    await pg_session.commit()


async def _create_scim_user(pg_session, user_id: str, email: str):
    """Insert a SCIM-provisioned user exactly as ScimUserService.create_user does.

    Note the email is stored verbatim — SCIM does not lowercase it.
    """
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status,
                               scim_external_id, scim_user_name, created_at, updated_at)
            VALUES (:id, :id, :email, 'Scim', 'User', 'member', 'active',
                    :external_id, :user_name, NOW(), NOW())
        """),
        {"id": user_id, "email": email, "external_id": f"ext-{user_id}", "user_name": email},
    )
    await pg_session.commit()


async def _create_oidc_user(pg_session, user_id: str, sub: str, email: str):
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
            VALUES (:id, :sub, :email, 'Oidc', 'User', 'member', 'active', NOW(), NOW())
        """),
        {"id": user_id, "sub": sub, "email": email},
    )
    await pg_session.commit()


class TestHasIdpIdentity:
    """The predicate that separates a placeholder subject from a real one."""

    def test_placeholder_sub_of_scim_user_is_not_an_identity(self):
        assert has_idp_identity("uuid-1", "uuid-1", "scim@example.com") is False

    def test_real_sub_of_scim_user_is_an_identity(self):
        assert has_idp_identity("uuid-1", "keycloak-sub", "scim@example.com") is True

    def test_legacy_user_keyed_by_its_own_sub_is_an_identity(self):
        # Migration 001 keyed `users` by the OIDC sub itself, so sub == id is normal for a row
        # created back then — and that user does have a Keycloak account.
        assert has_idp_identity("legacy-sub", "legacy-sub", None) is True


@pytest.mark.asyncio
class TestGroupMembershipWithPlaceholderSub:
    """Adding and removing a SCIM user who has never logged in."""

    async def test_add_member_skips_keycloak_for_placeholder_sub(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """The membership persists and Keycloak is not called — no 404, no rollback."""
        service = user_group_service_with_keycloak
        await _create_group(pg_session)
        await _create_scim_user(pg_session, "scim-user-1", "scim1@example.com")

        await service.add_member(db=pg_session, actor=test_user, group_id=1, user_id="scim-user-1")
        await pg_session.commit()

        mock_keycloak_service.add_user_to_group.assert_not_called()

        result = await pg_session.execute(
            text("SELECT COUNT(*) FROM user_group_members WHERE user_group_id = 1 AND user_id = 'scim-user-1'")
        )
        assert result.scalar() == 1

    async def test_add_member_still_syncs_users_with_a_real_sub(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        service = user_group_service_with_keycloak
        await _create_group(pg_session)
        await _create_oidc_user(pg_session, "uuid-2", "keycloak-sub-2", "oidc2@example.com")

        await service.add_member(db=pg_session, actor=test_user, group_id=1, user_id="uuid-2")
        await pg_session.commit()

        mock_keycloak_service.add_user_to_group.assert_called_once_with("keycloak-sub-2", "kc-group-123")

    async def test_add_member_still_syncs_legacy_user_keyed_by_its_sub(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """sub == id is not enough to call it a placeholder — legacy rows must still sync."""
        service = user_group_service_with_keycloak
        await _create_group(pg_session)
        await _create_oidc_user(pg_session, "legacy-sub-3", "legacy-sub-3", "legacy3@example.com")

        await service.add_member(db=pg_session, actor=test_user, group_id=1, user_id="legacy-sub-3")
        await pg_session.commit()

        mock_keycloak_service.add_user_to_group.assert_called_once_with("legacy-sub-3", "kc-group-123")

    async def test_remove_member_skips_keycloak_for_placeholder_sub(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        service = user_group_service_with_keycloak
        await _create_group(pg_session)
        await _create_scim_user(pg_session, "scim-user-4", "scim4@example.com")
        # remove_member refuses to empty a group, so the group keeps a second member.
        await _create_oidc_user(pg_session, "uuid-4b", "keycloak-sub-4b", "oidc4b@example.com")
        await service.add_member(db=pg_session, actor=test_user, group_id=1, user_id="scim-user-4")
        await service.add_member(db=pg_session, actor=test_user, group_id=1, user_id="uuid-4b")
        await pg_session.commit()

        await service.remove_member(db=pg_session, actor=test_user, group_id=1, user_id="scim-user-4")
        await pg_session.commit()

        mock_keycloak_service.remove_user_from_group.assert_not_called()

        result = await pg_session.execute(
            text("SELECT COUNT(*) FROM user_group_members WHERE user_group_id = 1 AND user_id = 'scim-user-4'")
        )
        assert result.scalar() == 0


@pytest.mark.asyncio
class TestFirstLoginReconciliation:
    """UserService pushes the memberships granted before the user had a Keycloak account."""

    async def test_first_login_pushes_pending_memberships(self, pg_session, user_service, mock_keycloak_service):
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_group(pg_session, group_id=2, keycloak_group_id=None)
        await _create_scim_user(pg_session, "scim-user-5", "scim5@example.com")
        for group_id in (1, 2):
            await pg_session.execute(
                text("""
                    INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                    VALUES (:group_id, 'scim-user-5', 'read', NOW())
                """),
                {"group_id": group_id},
            )
        await pg_session.commit()

        await user_service.upsert_user(
            db=pg_session,
            sub="keycloak-sub-5",
            email="scim5@example.com",
            first_name="Scim",
            last_name="User",
        )
        await pg_session.commit()

        # Only the group that exists in Keycloak is pushed, and under the real subject.
        mock_keycloak_service.add_user_to_group.assert_called_once_with("keycloak-sub-5", "kc-group-1")

    async def test_first_login_matches_a_mixed_case_scim_email(
        self, pg_session, user_service, mock_keycloak_service
    ):
        """SCIM stores the address verbatim; the login path lowercases before matching.

        A case-sensitive match would miss the placeholder row entirely — no reconciliation, and a
        second user row that trips `idx_users_email_unique` (which is on LOWER(email)).
        """
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-8", "Scim.Eight@Example.com")
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (1, 'scim-user-8', 'read', NOW())
            """)
        )
        await pg_session.commit()

        user = await user_service.upsert_user(
            db=pg_session,
            sub="keycloak-sub-8",
            email="scim.eight@example.com",
            first_name="Scim",
            last_name="User",
        )
        await pg_session.commit()

        assert user.id == "scim-user-8"  # the placeholder row was matched, not a second one
        mock_keycloak_service.add_user_to_group.assert_called_once_with("keycloak-sub-8", "kc-group-1")

        count = await pg_session.execute(text("SELECT COUNT(*) FROM users WHERE LOWER(email) = 'scim.eight@example.com'"))
        assert count.scalar() == 1

    async def test_ordinary_login_pushes_nothing(self, pg_session, user_service, mock_keycloak_service):
        """A user who already had a real subject is not re-reconciled on every login."""
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_oidc_user(pg_session, "uuid-6", "keycloak-sub-6", "oidc6@example.com")
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (1, 'uuid-6', 'read', NOW())
            """)
        )
        await pg_session.commit()

        await user_service.upsert_user(
            db=pg_session,
            sub="keycloak-sub-6",
            email="oidc6@example.com",
            first_name="Oidc",
            last_name="User",
        )
        await pg_session.commit()

        mock_keycloak_service.add_user_to_group.assert_not_called()

    async def test_keycloak_failure_does_not_fail_the_login(self, pg_session, user_service, mock_keycloak_service):
        """The database is the authority for membership; the mirror must not cost a login."""
        from console_backend.services.keycloak_admin_service import KeycloakSyncError

        user_service.set_keycloak_service(mock_keycloak_service)
        mock_keycloak_service.add_user_to_group.side_effect = KeycloakSyncError("Keycloak is down")
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-7", "scim7@example.com")
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (1, 'scim-user-7', 'read', NOW())
            """)
        )
        await pg_session.commit()

        user = await user_service.upsert_user(
            db=pg_session,
            sub="keycloak-sub-7",
            email="scim7@example.com",
            first_name="Scim",
            last_name="User",
        )
        await pg_session.commit()

        assert user.sub == "keycloak-sub-7"
