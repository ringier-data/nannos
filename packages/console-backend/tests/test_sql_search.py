"""LIKE metacharacter handling in user-supplied search terms."""

import pytest
from console_backend.services.secrets_service import SecretsService
from console_backend.models.user import User
from console_backend.utils.sql_search import like_clause, like_contains
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_secrets_service import _create_secret, _create_user


def test_like_contains_escapes_wildcards_and_the_escape_itself():
    assert like_contains("50%") == r"%50\%%"
    assert like_contains("a_b") == r"%a\_b%"
    # The backslash goes first, or escaping % would double-escape it.
    assert like_contains(r"c:\x") == r"%c:\\x%"
    assert like_contains("plain") == "%plain%"


def test_like_clause_declares_the_escape_character():
    clause = like_clause("a.name", "a.description")
    assert clause.count("ESCAPE '\\'") == 2
    assert clause.startswith("(") and clause.endswith(")")


@pytest.mark.asyncio
async def test_percent_in_a_search_term_is_a_literal(
    pg_session: AsyncSession, secrets_service: SecretsService, test_user: User, aws_mock
):
    """Searching "50%" must not behave as "everything starting with 50"."""
    user_id = await _create_user(pg_session, test_user.email, test_user.sub)
    test_user.id = user_id
    await _create_secret(secrets_service, pg_session, "discount-50%-code", test_user)
    await _create_secret(secrets_service, pg_session, "discount-5000-code", test_user)

    found, total = await secrets_service.list_user_secrets(
        db=pg_session, user_id=user_id, search="50%"
    )
    assert total == 1
    assert [s.name for s in found] == ["discount-50%-code"]

    # A bare underscore is a single-character wildcard unless escaped.
    none_found, total = await secrets_service.list_user_secrets(
        db=pg_session, user_id=user_id, search="discount_50"
    )
    assert total == 0
    assert none_found == []
