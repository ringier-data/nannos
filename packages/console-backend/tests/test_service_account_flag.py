"""Tests for the `users.is_service_account` marker (issue #198).

Machine identities have no inbox anyone reads, so audiences selected by standing must be
able to exclude them as a category. Before this flag the only handle was an id list,
which covered the seeded `system` account and missed the service accounts onboarded from
client-credentials tokens.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.dependencies import token_is_service_account

# ---------------------------------------------------------------------------
# Detecting a machine identity from token claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"preferred_username": "service-account-orchestrator"}, id="keycloak-username"),
        pytest.param(
            {"preferred_username": "service-account-agent-runner", "email": "sa@example.com"},
            id="username-wins-over-an-email",
        ),
    ],
)
def test_token_is_service_account(payload):
    assert token_is_service_account(payload) is True


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"email": "andrea@example.com"}, id="email-only"),
        pytest.param({"given_name": "Andrea"}, id="given-name-only"),
        pytest.param({"family_name": "Artaria"}, id="family-name-only"),
        pytest.param(
            {"preferred_username": "aartaria", "email": "andrea@example.com"},
            id="a-person-with-a-username",
        ),
        pytest.param(
            {"preferred_username": "service_account_lookalike", "email": "someone@example.com"},
            id="prefix-must-match-exactly",
        ),
        pytest.param({}, id="no-claims-is-a-person-with-sparse-claims"),
        pytest.param({"email": "   ", "given_name": "", "family_name": None}, id="blank-identity-claims"),
    ],
)
def test_token_is_not_a_service_account(payload):
    """Errs towards a person: wrongly silencing someone's notifications is the costly way to be wrong.

    The last two cases matter most. `require_auth_or_bearer_token` onboards a person from
    a token carrying nothing but a `sub`, filling the rest with empty strings — so absent
    identity claims cannot be read as "machine" without misclassifying that person.
    """
    assert token_is_service_account(payload) is False


# ---------------------------------------------------------------------------
# Migration backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seeded_system_user_is_flagged(pg_session: AsyncSession):
    result = await pg_session.execute(text("SELECT is_service_account FROM users WHERE id = 'system'"))
    assert result.scalar() is True, "migration 088 backfills the seeded owner of auto-provisioned agents"


@pytest.mark.asyncio
async def test_column_defaults_to_false_for_a_person(pg_session: AsyncSession):
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name)
            VALUES ('person-default', 'person-default', 'person@example.com', 'A', 'Person')
        """)
    )
    await pg_session.commit()

    result = await pg_session.execute(text("SELECT is_service_account FROM users WHERE id = 'person-default'"))
    assert result.scalar() is False


@pytest.mark.asyncio
async def test_backfill_catches_keycloak_service_account_emails(pg_session: AsyncSession):
    """The convention is only used for rows that already exist; new ones are flagged at creation.

    Re-running the backfill statement is how a row created before this migration would have
    been treated, and it is worth pinning: a deployment's existing orchestrator account is
    exactly this shape.
    """
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name)
            VALUES ('legacy-sa', 'legacy-sa', 'service-account-orchestrator@example.com', '', '')
        """)
    )
    await pg_session.execute(
        text("""
            UPDATE users SET is_service_account = TRUE
             WHERE is_service_account IS FALSE
               AND (email LIKE 'service-account-%' OR email LIKE '%@service-account.%')
        """)
    )
    await pg_session.commit()

    result = await pg_session.execute(text("SELECT is_service_account FROM users WHERE id = 'legacy-sa'"))
    assert result.scalar() is True


# ---------------------------------------------------------------------------
# The flag survives a re-onboarding
# ---------------------------------------------------------------------------


def _user_service():
    from console_backend.repositories.user_repository import UserRepository
    from console_backend.services.audit_service import AuditService
    from console_backend.services.user_service import UserService

    repository = UserRepository()
    repository.set_audit_service(AuditService())
    return UserService(user_repository=repository, audit_service=AuditService())


@pytest.mark.asyncio
async def test_upsert_never_clears_the_flag(pg_session: AsyncSession):
    """A token that lacks the claim must not re-classify a machine account as a person."""
    user_service = _user_service()

    created = await user_service.upsert_user(
        pg_session, sub="sa-upsert", email="", first_name="", last_name="", is_service_account=True
    )
    assert created.is_service_account is True

    updated = await user_service.upsert_user(
        pg_session, sub="sa-upsert", email="", first_name="", last_name="", is_service_account=False
    )
    assert updated.is_service_account is True


@pytest.mark.asyncio
async def test_upsert_raises_the_flag_on_an_existing_row(pg_session: AsyncSession):
    """A machine account missed by migration 088's backfill corrects itself on its next call.

    The backfill keys on Keycloak's `service-account-` email convention, and a
    client-credentials token need not carry an email at all — so a service account
    onboarded before the migration can exist unflagged. Insert-only semantics would leave
    it that way forever, and it is exactly the row whose notifications nobody reads.
    """
    user_service = _user_service()

    created = await user_service.upsert_user(
        pg_session, sub="sa-late", email="", first_name="", last_name="", is_service_account=False
    )
    assert created.is_service_account is False

    corrected = await user_service.upsert_user(
        pg_session, sub="sa-late", email="", first_name="", last_name="", is_service_account=True
    )
    assert corrected.is_service_account is True
