"""Migration 110 turns every stored ``paused_reason`` into a pause code.

The code is now the state: the claim's retry branch reads "no code" as "nobody stopped
this", and the releases find held rows by code. A backfill that matched a different
wording would leave a stopped row with no code, which a pending retry could revive, or a
held row nothing would ever release. The statements are read out of the shipped
migration and executed here, against a ``paused_reason`` column recreated as it was.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from console_backend.models.scheduled_job import PauseCode, render_pause
from tests.test_scheduler_sharing import _seed_users

MIGRATION = Path(__file__).parent.parent / "sqlmigrations" / "ddl" / "110_subscription_pause_code.sql"

#: Codes written with a fixed sentence before this migration, which it matches exactly.
_FIXED = [
    PauseCode.DISABLED_BY_USER,
    PauseCode.MANUALLY_PAUSED,
    PauseCode.ELAPSED_ON_SUBSCRIBE,
    PauseCode.ELAPSED_ON_INHERIT,
    PauseCode.AGENT_INACCESSIBLE,
    PauseCode.CONDITION_MET_ONCE,
    PauseCode.NO_OFFLINE_TOKEN,
    PauseCode.AWAITING_SIGN_IN,
    PauseCode.SIGN_IN_EXPIRED,
    PauseCode.ACCESS_REVOKED,
]


def _backfill() -> list[str]:
    body = MIGRATION.read_text().split("-- backfill:pause-codes", 1)[1].split("-- end backfill:pause-codes", 1)[0]
    # Statements end at a line end: some of the texts have a semicolon of their own.
    return [s.strip() for s in body.split(";\n") if s.strip()]


def test_each_fixed_sentence_is_matched_by_the_text_it_renders_today():
    """The renderer now owns the wording; this pins it to what the rows already hold."""
    body = "\n".join(_backfill())
    for code in _FIXED:
        sentence = render_pause(code).replace("'", "''")
        assert f"WHEN '{sentence}' THEN '{code.value}'" in body, code


async def _subscription(db, owner_id: str, enabled: bool, reason: str | None) -> int:
    did = (
        await db.execute(
            text("""
                INSERT INTO scheduled_job_definitions
                    (owner_user_id, name, job_type, schedule_kind, cron_expr, trigger_policy, check_tool, cel_expr)
                VALUES (:u, 'd', 'watch', 'cron', '0 9 * * *', 'fixed', 'ping', 'true') RETURNING id
            """),
            {"u": owner_id},
        )
    ).scalar_one()
    return (
        await db.execute(
            text("""
                INSERT INTO scheduled_job_subscriptions (definition_id, user_id, enabled, paused_reason, next_run_at)
                VALUES (:d, :u, :e, :r, NOW()) RETURNING id
            """),
            {"d": did, "u": owner_id, "e": enabled, "r": reason},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_the_backfill_codes_every_stop_and_keeps_what_it_cannot_name(pg_session):
    db = pg_session
    owner = (await _seed_users(db))["owner"]
    await db.execute(text("ALTER TABLE scheduled_job_subscriptions ADD COLUMN paused_reason TEXT"))
    await db.execute(text("UPDATE scheduled_job_subscriptions SET pause_code = NULL"))

    rows = {code: await _subscription(db, owner.id, False, render_pause(code)) for code in _FIXED}
    auto = await _subscription(db, owner.id, False, "Auto-paused after 3 consecutive failures")
    # The engine wrote the zone with Python's repr, in single quotes.
    tz = await _subscription(db, owner.id, False, "Invalid timezone 'Mars/Olympus' — fix the job's timezone and resume it.")
    odd = await _subscription(db, owner.id, False, "Something an older version wrote")
    on = await _subscription(db, owner.id, True, "Disabled by user")
    retired = await _subscription(db, owner.id, False, None)

    for statement in _backfill():
        await db.execute(text(statement))

    result = await db.execute(text("SELECT id, pause_code, pause_detail FROM scheduled_job_subscriptions"))
    got = {r.id: (r.pause_code, r.pause_detail) for r in result}
    for code, sid in rows.items():
        assert got[sid] == (code.value, None), code
    assert got[auto] == ("auto_paused", {"max_failures": 3})
    assert got[tz] == ("invalid_timezone", {"timezone": "Mars/Olympus"})
    assert got[odd] == ("legacy", {"text": "Something an older version wrote"})
    assert got[on] == (None, None), "a reason on a switched-on row described no stop"
    assert got[retired] == (None, None), "a retired one-shot has no code"
    # And each renders back to what the row said.
    assert render_pause("auto_paused", {"max_failures": 3}) == "Auto-paused after 3 consecutive failures"
    assert render_pause("invalid_timezone", {"timezone": "Mars/Olympus"}).startswith("Invalid timezone 'Mars/Olympus'")
    assert render_pause("legacy", {"text": "Something an older version wrote"}) == "Something an older version wrote"
