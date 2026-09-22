"""Migration 101's backfill must convert exactly the old-style placeholders, and no others.

The old placeholder was the row's own id, so the backfill has to *infer* which rows were
placeholders — the one and only time that inference is made, against the data as it stands.
Getting it wrong is silent in both directions: too narrow leaves a user 500ing on every group
add, too wide rewrites the subject of a real user and locks them out of their own row at the
next login.

The UPDATE is read out of the shipped migration and executed here, so this tests the statement
that will actually run rather than a copy of it.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

MIGRATION = (
    Path(__file__).parent.parent / "sqlmigrations" / "ddl" / "101_scim_placeholder_sub_and_keycloak_mirror.sql"
)


def _backfill_statement() -> str:
    """The UPDATE that follows the `-- backfill:placeholder-subs` marker."""
    body = MIGRATION.read_text().split("-- backfill:placeholder-subs", 1)[1]
    statement = body.split(";", 1)[0].strip()
    assert statement.upper().startswith("UPDATE USERS"), statement
    return statement


async def _insert(pg_session, user_id: str, sub: str, email: str, scim_user_name: str | None):
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status,
                               scim_user_name, created_at, updated_at)
            VALUES (:id, :sub, :email, 'T', 'U', 'member', 'active', :scim_user_name, NOW(), NOW())
        """),
        {"id": user_id, "sub": sub, "email": email, "scim_user_name": scim_user_name},
    )


async def _sub_of(pg_session, user_id: str) -> str:
    result = await pg_session.execute(text("SELECT sub FROM users WHERE id = :id"), {"id": user_id})
    return result.scalar_one()


def test_the_marker_is_still_there():
    """A renamed marker would make `_backfill_statement` test nothing at all."""
    assert "-- backfill:placeholder-subs" in MIGRATION.read_text()


@pytest.mark.asyncio
async def test_backfill_rewrites_only_the_scim_placeholders(pg_session):
    # A SCIM-provisioned user who never logged in: sub == id, and a SCIM userName on file.
    await _insert(pg_session, "scim-1", "scim-1", "scim1@example.com", "scim1@example.com")
    # A legacy user from when `users` was keyed by the OIDC sub: sub == id, no SCIM markers.
    await _insert(pg_session, "legacy-2", "legacy-2", "legacy2@example.com", None)
    # A SCIM user who has since logged in: real subject, SCIM userName still on file.
    await _insert(pg_session, "scim-3", "keycloak-sub-3", "scim3@example.com", "scim3@example.com")
    # An ordinary user.
    await _insert(pg_session, "uuid-4", "keycloak-sub-4", "oidc4@example.com", None)
    await pg_session.commit()

    await pg_session.execute(text(_backfill_statement()))
    await pg_session.commit()

    assert await _sub_of(pg_session, "scim-1") == "scim-pending:scim-1"
    assert await _sub_of(pg_session, "legacy-2") == "legacy-2"
    assert await _sub_of(pg_session, "scim-3") == "keycloak-sub-3"
    assert await _sub_of(pg_session, "uuid-4") == "keycloak-sub-4"


@pytest.mark.asyncio
async def test_backfill_is_idempotent(pg_session):
    """Re-running it must not prefix an already-prefixed subject."""
    await _insert(pg_session, "scim-5", "scim-5", "scim5@example.com", "scim5@example.com")
    await pg_session.commit()

    statement = text(_backfill_statement())
    await pg_session.execute(statement)
    await pg_session.execute(statement)
    await pg_session.commit()

    assert await _sub_of(pg_session, "scim-5") == "scim-pending:scim-5"


@pytest.mark.asyncio
async def test_the_prefix_matches_the_one_the_code_writes(pg_session):
    """The migration hard-codes the prefix in SQL; the code owns it in Python."""
    from console_backend.models.user import placeholder_sub

    await _insert(pg_session, "scim-6", "scim-6", "scim6@example.com", "scim6@example.com")
    await pg_session.commit()

    await pg_session.execute(text(_backfill_statement()))
    await pg_session.commit()

    assert await _sub_of(pg_session, "scim-6") == placeholder_sub("scim-6")
