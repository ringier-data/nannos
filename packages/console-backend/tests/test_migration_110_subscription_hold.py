"""Migration 110's backfill must give every hold written before the ``hold`` column its code.

Until then the releases found held rows by the exact wording of ``paused_reason``. A
backfill that matched a different wording would leave those rows switched off with no
code, and nothing would ever release them: silently, since they still read as paused for
a reason. The UPDATEs are read out of the shipped migration and executed here, so this
tests the statements that will actually run.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from console_backend.models.scheduled_job import SubscriptionHold
from console_backend.services.scheduler_service import (
    _ACCESS_REVOKED_REASON,
    _AWAITING_SIGN_IN_REASON,
    _SIGN_IN_EXPIRED_REASON,
)
from tests.test_scheduler_sharing import _seed_users

MIGRATION = Path(__file__).parent.parent / "sqlmigrations" / "ddl" / "110_subscription_hold.sql"

#: The texts the code wrote before the column existed, and the code each one becomes.
_BEFORE = {
    _AWAITING_SIGN_IN_REASON: SubscriptionHold.AWAITING_SIGN_IN,
    _SIGN_IN_EXPIRED_REASON: SubscriptionHold.SIGN_IN_EXPIRED,
    _ACCESS_REVOKED_REASON: SubscriptionHold.ACCESS_REVOKED,
}


def _backfill() -> list[str]:
    body = MIGRATION.read_text().split("-- backfill:holds", 1)[1].split("-- end backfill:holds", 1)[0]
    # Statements end at a line end: one of the texts has a semicolon of its own.
    statements = [s.strip() for s in body.split(";\n") if s.strip()]
    assert len(statements) == len(_BEFORE), statements
    return statements


def test_the_backfill_matches_the_texts_the_code_wrote():
    """Once the constants are free to be reworded, this is what pins the old wording."""
    body = "\n".join(_backfill())
    for reason, hold in _BEFORE.items():
        assert f"SET hold = '{hold.value}'" in body
        assert "'" + reason.replace("'", "''") + "'" in body


@pytest.mark.asyncio
async def test_the_backfill_codes_held_rows_and_leaves_the_rest(pg_session):
    db = pg_session
    owner = (await _seed_users(db))["owner"]
    rows = {**{reason: False for reason in _BEFORE}, "Manually paused": False, None: True}
    ids = {}
    for reason, enabled in rows.items():
        # A definition per row: (definition, user) is unique while live.
        did = (
            await db.execute(
                text("""
                    INSERT INTO scheduled_job_definitions
                        (owner_user_id, name, job_type, schedule_kind, cron_expr, trigger_policy, check_tool, cel_expr)
                    VALUES (:u, 'd', 'watch', 'cron', '0 9 * * *', 'fixed', 'ping', 'true') RETURNING id
                """),
                {"u": owner.id},
            )
        ).scalar_one()
        ids[reason] = (
            await db.execute(
                text("""
                    INSERT INTO scheduled_job_subscriptions (definition_id, user_id, enabled, paused_reason, next_run_at)
                    VALUES (:d, :u, :e, :r, NOW()) RETURNING id
                """),
                {"d": did, "u": owner.id, "e": enabled, "r": reason},
            )
        ).scalar_one()

    for statement in _backfill():
        await db.execute(text(statement))

    result = await db.execute(
        text("SELECT id, hold FROM scheduled_job_subscriptions WHERE id = ANY(:ids)"), {"ids": list(ids.values())}
    )
    holds = dict(result.all())
    for reason, hold in _BEFORE.items():
        assert holds[ids[reason]] == hold.value
    assert holds[ids["Manually paused"]] is None
    assert holds[ids[None]] is None
