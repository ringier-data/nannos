"""Resolving who a console-backend gateway call bills to.

`users.id` is the OIDC subject only for people onboarded before the id/sub split; for
everyone after it is a UUID that is not. Every internal handle a caller is likely to be
holding — `scheduled_jobs.user_id`, the socket session's `user_id`, `catalogs.owner_user_id`
— is that internal id, so it has to be resolved rather than passed through.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from console_backend.services.spend_attribution import (
    SERVICE_CATALOG,
    SERVICE_CONSOLE,
    SERVICE_SCHEDULER,
    billing_subject,
    resolve_user_sub,
)


def _db(sub=None, *, fails=False):
    db = AsyncMock()
    if fails:
        db.execute.side_effect = RuntimeError("db down")
    else:
        db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=sub))
    return db


@pytest.mark.asyncio
async def test_it_reads_the_subject_not_the_internal_id():
    db = _db("oidc-subject")
    assert await resolve_user_sub(db, "internal-uuid") == "oidc-subject"
    assert db.execute.await_args.args[1] == {"user_id": "internal-uuid"}


@pytest.mark.asyncio
async def test_a_user_without_a_subject_is_a_warning_not_a_failure(caplog):
    with caplog.at_level("WARNING"):
        assert await resolve_user_sub(_db(None), "u1") is None
    assert "unattributed" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_fatal(caplog):
    with caplog.at_level("WARNING"):
        assert await resolve_user_sub(_db(fails=True), "u1") is None
    assert "unattributed" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_a_failed_lookup_rolls_the_session_back():
    """The part that is easy to miss: callers pass a session the rest of their work runs
    on, and a failed statement leaves it refusing every next one. Swallowing the error
    without rolling back turns a best-effort lookup into a fatal one."""
    db = _db(fails=True)
    assert await resolve_user_sub(db, "u1") is None
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_rollback_that_fails_too_is_still_not_fatal():
    db = _db(fails=True)
    db.rollback.side_effect = RuntimeError("connection gone")
    assert await resolve_user_sub(db, "u1") is None


@pytest.mark.asyncio
async def test_the_context_reaches_the_log(caplog):
    with caplog.at_level("WARNING"):
        await resolve_user_sub(_db(None), "u1", context="job 42")
    assert "job 42" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_what_gets_billed_is_the_subject_when_there_is_one():
    assert await billing_subject(_db("oidc-subject"), "internal-uuid") == "oidc-subject"


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [{"sub": None}, {"fails": True}])
async def test_an_unreadable_subject_bills_the_internal_id_rather_than_nobody(unreadable):
    """The ingest resolves either, so the id still bills the right person. `None` would
    not: the proxy's logger discards a record carrying no subject, so the spend would
    vanish from usage_logs — the failure this attribution work exists to stop."""
    db = _db(unreadable.get("sub"), fails=unreadable.get("fails", False))
    assert await billing_subject(db, "internal-uuid") == "internal-uuid"


def test_the_service_names_match_what_the_usage_view_derives():
    # usage_repository falls back to deriving these same strings for rows that declare
    # none, so a rename here without one there would split a bucket in two.
    assert (SERVICE_SCHEDULER, SERVICE_CATALOG) == ("scheduler", "catalog")
    assert SERVICE_CONSOLE == "console"
