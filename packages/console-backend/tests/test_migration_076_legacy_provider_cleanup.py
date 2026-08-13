"""Migration 076 — re-keying legacy rate-card providers without losing pricing.

The DELETE half is the dangerous one: rate_card_entries cascade, so dropping a card destroys its
pricing history, and `down` cannot bring it back. These tests run the migration's own SQL against
seeded data to pin exactly which cards it is allowed to remove — only ones whose same-model twin is
genuinely pricing the model today. Everything else must survive and be left to the Rate Cards banner
(``orphan_cards``) as a deliberate admin decision.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_MIGRATION = Path(__file__).resolve().parents[1] / "sqlmigrations/ddl/076_cleanup_legacy_rate_card_providers.sql"
NOW = datetime.now(timezone.utc)


def _up_statements() -> list[str]:
    """The migration's `up` statements, comments stripped.

    Comment lines are removed BEFORE splitting on ';' — the header prose contains semicolons, and
    splitting first cuts a comment in half and hands Postgres its second half as SQL.
    """
    body = _MIGRATION.read_text().split("-- rambler up", 1)[1].split("-- rambler down", 1)[0]
    sql = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in sql.split(";") if s.strip()]


async def _card(db: AsyncSession, provider: str, model: str) -> int:
    row = await db.execute(
        text("INSERT INTO rate_cards (provider, model_name) VALUES (:p, :m) RETURNING id"),
        {"p": provider, "m": model},
    )
    return row.scalar()


async def _entry(
    db: AsyncSession,
    card_id: int,
    *,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> None:
    await db.execute(
        text("""
            INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million,
                                           effective_from, effective_until)
            VALUES (:id, 'base_input_tokens', 'input', :price, :ef, :eu)
        """),
        {
            "id": card_id,
            "price": Decimal("5.50"),
            "ef": effective_from or (NOW - timedelta(days=30)),
            "eu": effective_until,
        },
    )


async def _providers_for(db: AsyncSession, model: str) -> set[str]:
    rows = await db.execute(
        text("SELECT provider FROM rate_cards WHERE model_name = :m"), {"m": model}
    )
    return {r[0] for r in rows}


async def _run_migration(db: AsyncSession) -> None:
    await db.flush()
    for statement in _up_statements():
        await db.execute(text(statement))
    await db.flush()


@pytest.mark.asyncio
async def test_lone_legacy_tag_card_is_rekeyed_and_keeps_its_pricing(pg_session: AsyncSession):
    """The common case: a `bedrock_converse` card with no twin becomes a `bedrock` card, history intact."""
    card = await _card(pg_session, "bedrock_converse", "mig076-lonely")
    await _entry(pg_session, card)

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-lonely") == {"bedrock"}
    entries = await pg_session.execute(
        text("SELECT COUNT(*) FROM rate_card_entries WHERE rate_card_id = :id"), {"id": card}
    )
    assert entries.scalar() == 1  # same card row, same entries — a re-key, not a re-create


@pytest.mark.asyncio
async def test_redundant_legacy_card_is_deleted_when_the_twin_is_actually_pricing(pg_session: AsyncSession):
    legacy = await _card(pg_session, "bedrock_converse", "mig076-redundant")
    await _entry(pg_session, legacy)
    twin = await _card(pg_session, "bedrock", "mig076-redundant")
    await _entry(pg_session, twin)  # open-ended → pricing today

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-redundant") == {"bedrock"}


@pytest.mark.asyncio
async def test_legacy_card_survives_when_the_twin_has_no_effective_entries(pg_session: AsyncSession):
    """The data-loss case this migration used to hit: the twin exists but its pricing has lapsed, so the
    legacy card is the only thing that could price the model. Deleting it cascades the history away and
    leaves the model at $0 — keep it and let the banner surface it."""
    legacy = await _card(pg_session, "bedrock_converse", "mig076-lapsed-twin")
    await _entry(pg_session, legacy)
    twin = await _card(pg_session, "bedrock", "mig076-lapsed-twin")
    await _entry(pg_session, twin, effective_until=NOW - timedelta(days=1))  # expired

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-lapsed-twin") == {"bedrock_converse", "bedrock"}
    entries = await pg_session.execute(
        text("SELECT COUNT(*) FROM rate_card_entries WHERE rate_card_id = :id"), {"id": legacy}
    )
    assert entries.scalar() == 1  # history preserved


@pytest.mark.asyncio
async def test_empty_twin_does_not_authorize_deletion(pg_session: AsyncSession):
    """A twin card with no entries at all is not pricing anything either."""
    await _card(pg_session, "anthropic", "mig076-empty-twin")
    await _card(pg_session, "bedrock", "mig076-empty-twin")  # no entries

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-empty-twin") == {"anthropic", "bedrock"}


@pytest.mark.asyncio
async def test_legacy_sdk_vocabulary_without_any_twin_survives(pg_session: AsyncSession):
    """`anthropic` / `bedrock_embeddings` cards are not re-keyed (nothing says which family they meant),
    so deleting them would simply destroy pricing history. They stay, and the provider config check
    reports them as orphan cards."""
    card = await _card(pg_session, "bedrock_embeddings", "mig076-sdk-only")
    await _entry(pg_session, card)

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-sdk-only") == {"bedrock_embeddings"}


@pytest.mark.asyncio
async def test_a_scheduled_price_change_counts_as_pricing_today(pg_session: AsyncSession):
    """The twin's entry is closed-ended because a new price starts next month — it is still what bills
    now, so the legacy duplicate really is redundant."""
    legacy = await _card(pg_session, "bedrock_converse", "mig076-scheduled")
    await _entry(pg_session, legacy)
    twin = await _card(pg_session, "bedrock", "mig076-scheduled")
    await _entry(pg_session, twin, effective_until=NOW + timedelta(days=30))

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-scheduled") == {"bedrock"}


@pytest.mark.asyncio
async def test_correctly_keyed_cards_are_untouched(pg_session: AsyncSession):
    card = await _card(pg_session, "vertex_ai", "mig076-fine")
    await _entry(pg_session, card)

    await _run_migration(pg_session)

    assert await _providers_for(pg_session, "mig076-fine") == {"vertex_ai"}
