"""Tests for the SCIM placeholder subject and its Keycloak consequences.

A SCIM-provisioned user has no Keycloak account until their first OIDC login, so
`ScimUserService.create_user` parks the row's own id in `users.sub`. Group membership must
therefore skip Keycloak for such a user (rather than 500 on `404 User not found`) and
reconcile once the real subject arrives.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from console_backend.models.user import has_idp_identity, placeholder_sub
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
            VALUES (:id, :sub, :email, 'Scim', 'User', 'member', 'active',
                    :external_id, :user_name, NOW(), NOW())
        """),
        {
            "id": user_id,
            "sub": placeholder_sub(user_id),
            "email": email,
            "external_id": f"ext-{user_id}",
            "user_name": email,
        },
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


async def _defer_membership(pg_session, group_service, keycloak_mock, test_user, user_id: str, group_ids):
    """Add `user_id` to each group through the service, as an administrator would.

    For a user with no IdP identity every Keycloak call is skipped and the row is flagged
    `keycloak_mirror_pending` — which is what the login path later acts on. Going through
    the service rather than inserting rows keeps that wiring under test.
    """
    for group_id in group_ids:
        await group_service.add_member(db=pg_session, actor=test_user, group_id=group_id, user_id=user_id)
    await pg_session.commit()
    keycloak_mock.add_user_to_group.reset_mock()


class TestHasIdpIdentity:
    """The predicate that separates a placeholder subject from a real one."""

    def test_placeholder_sub_is_not_an_identity(self):
        assert has_idp_identity(placeholder_sub("uuid-1")) is False

    def test_real_sub_is_an_identity(self):
        assert has_idp_identity("keycloak-sub") is True

    def test_legacy_user_keyed_by_its_own_sub_is_an_identity(self):
        # Migration 001 keyed `users` by the OIDC sub itself, so a row from that era has
        # sub == id while owning a real Keycloak account. The old inference (`sub == id`,
        # narrowed by `scim_user_name`) could mistake one for a placeholder; an explicit
        # prefix cannot, whatever else is written onto the row.
        assert has_idp_identity("legacy-sub") is True

    def test_placeholder_is_keyed_on_the_row_id(self):
        # `users.sub` is unique, so two pending users must not collide.
        assert placeholder_sub("uuid-1") != placeholder_sub("uuid-2")


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

    async def test_first_login_pushes_pending_memberships(
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_group(pg_session, group_id=2, keycloak_group_id=None)
        await _create_scim_user(pg_session, "scim-user-5", "scim5@example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-5", (1, 2),
        )

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
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """SCIM stores the address verbatim; the login path lowercases before matching.

        A case-sensitive match would miss the placeholder row entirely — no reconciliation, and a
        second user row that trips `idx_users_email_unique` (which is on LOWER(email)).
        """
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-8", "Scim.Eight@Example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-8", (1,),
        )

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

        count = await pg_session.execute(
            text("SELECT COUNT(*) FROM users WHERE LOWER(email) = 'scim.eight@example.com'")
        )
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

    async def test_keycloak_failure_does_not_fail_the_login(
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """The database is the authority for membership; the mirror must not cost a login."""
        from console_backend.services.keycloak_admin_service import KeycloakSyncError

        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-7", "scim7@example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-7", (1,),
        )
        mock_keycloak_service.add_user_to_group.side_effect = KeycloakSyncError("Keycloak is down")

        user = await user_service.upsert_user(
            db=pg_session,
            sub="keycloak-sub-7",
            email="scim7@example.com",
            first_name="Scim",
            last_name="User",
        )
        await pg_session.commit()

        assert user.sub == "keycloak-sub-7"


async def _mirror_pending(pg_session, user_id: str) -> bool:
    result = await pg_session.execute(
        text("SELECT keycloak_mirror_pending FROM users WHERE id = :id"), {"id": user_id}
    )
    return bool(result.scalar())


@pytest.mark.asyncio
class TestMirrorPendingFlag:
    """`users.keycloak_mirror_pending` is what makes the push retryable.

    The trigger used to be the placeholder-to-real subject transition, which happens exactly
    once — so a Keycloak outage during that one login left the two sides diverged forever.
    """

    async def test_deferred_add_flags_the_user(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        await _create_group(pg_session)
        await _create_scim_user(pg_session, "scim-user-9", "scim9@example.com")
        assert await _mirror_pending(pg_session, "scim-user-9") is False

        await user_group_service_with_keycloak.add_member(
            db=pg_session, actor=test_user, group_id=1, user_id="scim-user-9"
        )
        await pg_session.commit()

        assert await _mirror_pending(pg_session, "scim-user-9") is True

    async def test_a_mirrored_add_flags_nobody(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        await _create_group(pg_session)
        await _create_oidc_user(pg_session, "uuid-10", "keycloak-sub-10", "oidc10@example.com")

        await user_group_service_with_keycloak.add_member(
            db=pg_session, actor=test_user, group_id=1, user_id="uuid-10"
        )
        await pg_session.commit()

        assert await _mirror_pending(pg_session, "uuid-10") is False

    async def test_successful_reconciliation_clears_the_flag(
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-11", "scim11@example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-11", (1,),
        )

        await user_service.upsert_user(
            db=pg_session, sub="keycloak-sub-11", email="scim11@example.com",
            first_name="Scim", last_name="User",
        )
        await pg_session.commit()

        assert await _mirror_pending(pg_session, "scim-user-11") is False

    async def test_a_failed_mirror_is_retried_at_the_next_login(
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """The whole point of the flag: the first login's outage must not be the only chance."""
        from console_backend.services.keycloak_admin_service import KeycloakSyncError

        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-12", "scim12@example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-12", (1,),
        )

        # First login: Keycloak is down.
        mock_keycloak_service.add_user_to_group.side_effect = KeycloakSyncError("Keycloak is down")
        await user_service.upsert_user(
            db=pg_session, sub="keycloak-sub-12", email="scim12@example.com",
            first_name="Scim", last_name="User",
        )
        await pg_session.commit()
        assert await _mirror_pending(pg_session, "scim-user-12") is True

        # Second login, Keycloak back: the same subject, so the old transition-based trigger
        # would never fire again.
        mock_keycloak_service.add_user_to_group.side_effect = None
        mock_keycloak_service.add_user_to_group.reset_mock()
        await user_service.upsert_user(
            db=pg_session, sub="keycloak-sub-12", email="scim12@example.com",
            first_name="Scim", last_name="User",
        )
        await pg_session.commit()

        mock_keycloak_service.add_user_to_group.assert_called_once_with("keycloak-sub-12", "kc-group-1")
        assert await _mirror_pending(pg_session, "scim-user-12") is False

    async def test_flag_clears_when_there_is_nothing_left_to_mirror(
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """Granted and withdrawn while pending: nothing to replay, and no flag left behind."""
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_scim_user(pg_session, "scim-user-13", "scim13@example.com")
        await _create_oidc_user(pg_session, "uuid-13b", "keycloak-sub-13b", "oidc13b@example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-13", (1,),
        )
        await user_group_service_with_keycloak.add_member(
            db=pg_session, actor=test_user, group_id=1, user_id="uuid-13b"
        )
        await user_group_service_with_keycloak.remove_member(
            db=pg_session, actor=test_user, group_id=1, user_id="scim-user-13"
        )
        await pg_session.commit()
        # The second member's own (correct) mirroring is setup noise, not the assertion.
        mock_keycloak_service.add_user_to_group.reset_mock()

        await user_service.upsert_user(
            db=pg_session, sub="keycloak-sub-13", email="scim13@example.com",
            first_name="Scim", last_name="User",
        )
        await pg_session.commit()

        mock_keycloak_service.add_user_to_group.assert_not_called()
        assert await _mirror_pending(pg_session, "scim-user-13") is False


@pytest.mark.asyncio
class TestSpendAttributionWithPlaceholder:
    """A placeholder subject bills to nobody, so the caller must fall back to the internal id."""

    async def test_billing_subject_falls_back_to_the_internal_id(self, pg_session):
        from console_backend.services.spend_attribution import billing_subject, resolve_user_sub

        await _create_scim_user(pg_session, "scim-user-14", "scim14@example.com")

        assert await resolve_user_sub(pg_session, "scim-user-14") is None
        # The usage ingest resolves either form, so the id attributes; the placeholder would not.
        assert await billing_subject(pg_session, "scim-user-14") == "scim-user-14"

    async def test_billing_subject_uses_a_real_sub(self, pg_session):
        from console_backend.services.spend_attribution import billing_subject

        await _create_oidc_user(pg_session, "uuid-15", "keycloak-sub-15", "oidc15@example.com")

        assert await billing_subject(pg_session, "uuid-15") == "keycloak-sub-15"


async def _soft_delete(pg_session, user_id: str):
    await pg_session.execute(
        text("UPDATE users SET deleted_at = NOW(), status = 'deleted' WHERE id = :id"), {"id": user_id}
    )
    await pg_session.commit()


@pytest.mark.asyncio
class TestRecycledEmailAcrossSoftDelete:
    """Migration 051 frees a soft-deleted user's address for re-provisioning on purpose.

    So the login lookup's two arms — `LOWER(email)` and `sub` — can land on different people,
    and which one wins is not a detail.
    """

    async def test_placeholder_reconciles_despite_a_soft_deleted_namesake(
        self, pg_session, user_service, user_group_service_with_keycloak, mock_keycloak_service, test_user
    ):
        """The reported break: two rows matched, the guard raised, and the user could never log in."""
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_group(pg_session, group_id=1, keycloak_group_id="kc-group-1")
        await _create_oidc_user(pg_session, "old-uuid-16", "keycloak-sub-16-old", "recycled@example.com")
        await _soft_delete(pg_session, "old-uuid-16")
        # SCIM re-provisions the same address, which 051 permits.
        await _create_scim_user(pg_session, "scim-user-16", "recycled@example.com")
        await _defer_membership(
            pg_session, user_group_service_with_keycloak, mock_keycloak_service, test_user,
            "scim-user-16", (1,),
        )

        user = await user_service.upsert_user(
            db=pg_session, sub="keycloak-sub-16-new", email="recycled@example.com",
            first_name="Scim", last_name="User",
        )
        await pg_session.commit()

        assert user.id == "scim-user-16"  # the live row, not the soft-deleted namesake
        mock_keycloak_service.add_user_to_group.assert_called_once_with("keycloak-sub-16-new", "kc-group-1")
        assert await _mirror_pending(pg_session, "scim-user-16") is False

    async def test_a_soft_deleted_user_logging_in_keeps_their_own_row(
        self, pg_session, user_service, mock_keycloak_service
    ):
        """Preferring the live row on a *subject* match would hand them somebody else's account."""
        user_service.set_keycloak_service(mock_keycloak_service)
        await _create_oidc_user(pg_session, "old-uuid-17", "keycloak-sub-17", "recycled17@example.com")
        await _soft_delete(pg_session, "old-uuid-17")
        await _create_scim_user(pg_session, "scim-user-17", "recycled17@example.com")

        # The deleted user still exists at the IdP and presents their own, unchanged subject.
        user = await user_service.upsert_user(
            db=pg_session, sub="keycloak-sub-17", email="recycled17@example.com",
            first_name="Old", last_name="User",
        )
        await pg_session.commit()

        assert user.id == "old-uuid-17"

        # ...and the live user's row is untouched.
        result = await pg_session.execute(
            text("SELECT sub FROM users WHERE id = 'scim-user-17'")
        )
        assert result.scalar_one() == placeholder_sub("scim-user-17")
