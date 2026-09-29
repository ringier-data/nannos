"""Migration 111 moves each channel's workspace from ``broker_workspaces`` onto the channel.

A channel the backfill misses reads ``unknown`` for everyone (warned, never refused) until
its client registers it again, and one it gives the wrong workspace refuses the wrong
people. The UPDATE is read out of the shipped migration and run against the old table,
recreated here as migration 108 left it, because the migration drops it.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_scheduler_sharing import _seed_users

MIGRATION = Path(__file__).parent.parent / "sqlmigrations" / "ddl" / "111_delivery_channel_workspace.sql"


def _backfill() -> str:
    body = MIGRATION.read_text().split("-- backfill:workspace", 1)[1].split("-- end backfill:workspace", 1)[0]
    statement = body.strip().rstrip(";")
    assert statement.upper().startswith("UPDATE DELIVERY_CHANNELS"), statement
    return statement


@pytest.mark.asyncio
async def test_each_channel_takes_the_workspace_that_listed_its_installation(pg_session):
    db = pg_session
    owner = (await _seed_users(db))["owner"]
    for client_id in ("slack-client", "other-client"):
        await db.execute(
            text("INSERT INTO broker_clients (client_id, name, created_by) VALUES (:c, :c, :u)"),
            {"c": client_id, "u": owner.id},
        )
    await db.execute(
        text("""
            CREATE TEMP TABLE broker_workspaces (
                client_id TEXT NOT NULL, workspace_id TEXT NOT NULL, installation_ids TEXT[] NOT NULL DEFAULT '{}'
            )
        """)
    )
    await db.execute(
        text("""
            INSERT INTO broker_workspaces VALUES
                ('slack-client', 'T1', '{A1,A2}'), ('slack-client', 'T2', '{B1}'), ('other-client', 'T9', '{A1}')
        """)
    )
    ids = {}
    for client_id, installation in (("slack-client", "A1"), ("slack-client", "B1"), ("slack-client", "C1")):
        ids[installation] = (
            await db.execute(
                text("""
                    INSERT INTO delivery_channels (name, webhook_url, secret, client_id, registered_by, installation_id)
                    VALUES (:i, 'https://x', 's', :c, 'sa', :i) RETURNING id
                """),
                {"c": client_id, "i": installation},
            )
        ).scalar_one()

    await db.execute(text(_backfill()))

    result = await db.execute(
        text("SELECT installation_id, workspace_id FROM delivery_channels WHERE id = ANY(:ids)"),
        {"ids": list(ids.values())},
    )
    # Scoped by client: another client's T9 listing A1 does not leak onto this A1.
    assert dict(result.all()) == {"A1": "T1", "B1": "T2", "C1": None}
