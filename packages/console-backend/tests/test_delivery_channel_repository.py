"""DB tests for DeliveryChannelRepository after the group_ids removal (migration 071).

Channel visibility is no longer group-scoped: `list_all_channels` returns every
channel, and channels carry a stable `(client_id, installation_id)` idempotency key
used for self-registration. There is no group plumbing left to assert against —
these tests pin the create / list / idempotent-upsert behavior the bots rely on.
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.delivery_channel import DeliveryChannelCreate
from console_backend.models.user import User
from console_backend.repositories.delivery_channel_repository import DeliveryChannelRepository
from console_backend.services.audit_service import AuditService


@pytest.fixture
def repo() -> DeliveryChannelRepository:
    r = DeliveryChannelRepository()
    r.set_audit_service(AuditService())
    return r


def _channel(name: str, installation_id: str) -> DeliveryChannelCreate:
    return DeliveryChannelCreate(
        name=name,
        description=f"desc for {name}",
        webhook_url="https://example.test/api/v1/a2a/callback",
        secret="s" * 32,
        installation_id=installation_id,
    )


@pytest.mark.asyncio
async def test_create_channel_without_groups(repo, pg_session: AsyncSession, test_user_db: User):
    """create_channel no longer needs/accepts group_ids; installation_id round-trips."""
    created = await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("ch", "inst-1")
    )
    await pg_session.commit()

    assert created.name == "ch"
    assert created.client_id == "client-a"
    assert created.installation_id == "inst-1"
    # The group concept is gone entirely — the response model no longer exposes it.
    assert not hasattr(created, "group_ids")


@pytest.mark.asyncio
async def test_message_formatting_defaults_and_round_trips(repo, pg_session: AsyncSession, test_user_db: User):
    """A client declares how its channel renders text; silence means Markdown."""
    plain = await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("web", "inst-web")
    )
    slack = await repo.create_channel(
        db=pg_session,
        actor=test_user_db,
        client_id="slack-client",
        data=_channel("slack", "inst-slack").model_copy(update={"message_formatting": "slack"}),
    )
    await pg_session.commit()

    assert plain.message_formatting == "markdown"
    assert slack.message_formatting == "slack"

    # And the scheduler sees it at dispatch time, alongside the push target.
    dispatch = await repo.get_channel_for_dispatch(pg_session, slack.id)
    assert dispatch["message_formatting"] == "slack"


@pytest.mark.asyncio
async def test_reregistration_without_a_format_keeps_the_stored_one(
    repo, pg_session: AsyncSession, test_user_db: User
):
    """A client that does not declare a format must not reset the channel on every boot.

    Bots re-register on startup, so a blanket overwrite would silently return a Slack
    channel to Markdown any time an older image (or a third-party client) came up.
    """
    first, _ = await repo.upsert_channel_by_installation(
        db=pg_session,
        actor=test_user_db,
        client_id="slack-client",
        data=_channel("bot", "inst-1").model_copy(update={"message_formatting": "slack"}),
    )
    await pg_session.commit()
    assert first.message_formatting == "slack"

    second, created = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="slack-client", data=_channel("bot", "inst-1")
    )
    await pg_session.commit()

    assert created is False
    assert second.id == first.id
    assert second.message_formatting == "slack"  # untouched, not reset to the default


@pytest.mark.asyncio
async def test_upsert_creates_then_updates_same_row(repo, pg_session: AsyncSession, test_user_db: User):
    """Re-registration with the same (client_id, installation_id) updates in place, not duplicates."""
    first, created1 = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("original", "inst-1")
    )
    await pg_session.commit()
    assert created1 is True

    second, created2 = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("renamed", "inst-1")
    )
    await pg_session.commit()

    assert created2 is False
    assert second.id == first.id  # same row, not a duplicate
    assert second.name == "renamed"  # mutable field overwritten on re-registration


@pytest.mark.asyncio
async def test_upsert_recovers_from_insert_race(repo, pg_session: AsyncSession, test_user_db: User):
    """A concurrent replica that wins the (client_id, installation_id) race must not 500.

    Several bot replicas booting at once all SELECT-miss and then INSERT; only one wins
    the partial unique index. The losers must roll back the conflicting INSERT (via the
    SAVEPOINT) and converge to an in-place update instead of surfacing an IntegrityError.
    We simulate the lost race by making the first lookup miss while a row already exists.
    """
    winner, _ = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("winner", "inst-1")
    )
    await pg_session.commit()

    real_get = repo.get_by_installation
    calls = {"n": 0}

    async def racing_get(db, client_id, installation_id):
        # First call mimics the pre-insert snapshot where the row isn't visible yet,
        # forcing the INSERT path into a unique-constraint conflict; later calls (the
        # post-conflict re-read) see the committed winner.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_get(db, client_id, installation_id)

    with patch.object(repo, "get_by_installation", new=racing_get):
        result, created = await repo.upsert_channel_by_installation(
            db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("loser", "inst-1")
        )
    await pg_session.commit()

    assert created is False  # recovered via update, not a fresh insert
    assert result.id == winner.id  # converged on the existing row
    assert result.name == "loser"  # registrar-owned fields overwritten

    # Exactly one channel for the key — no duplicate row, no IntegrityError surfaced.
    rows = await repo.list_channels_for_installation(pg_session, "inst-1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_upsert_isolates_by_client_id(repo, pg_session: AsyncSession, test_user_db: User):
    """The same installation_id under a different client_id is a distinct channel."""
    a, _ = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("a", "inst-1")
    )
    b, created = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-b", data=_channel("b", "inst-1")
    )
    await pg_session.commit()

    assert created is True
    assert a.id != b.id


def test_create_model_requires_installation_id():
    """installation_id is the (client_id, installation_id) idempotency key, so the request
    model rejects its absence — there is no way to register a channel without one."""
    with pytest.raises(ValidationError):
        DeliveryChannelCreate(
            name="no-install",
            description="desc",
            webhook_url="https://example.test/api/v1/a2a/callback",
            secret="s" * 32,
        )


@pytest.mark.asyncio
async def test_list_all_channels_is_not_group_scoped(repo, pg_session: AsyncSession, test_user_db: User):
    """Every channel is visible regardless of owning client / user groups."""
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("a", "inst-a")
    )
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-b", data=_channel("b", "inst-b")
    )
    await pg_session.commit()

    all_channels, total = await repo.list_all_channels(pg_session)
    names = {c.name for c in all_channels}
    assert {"a", "b"} <= names
    assert total == len(all_channels)


@pytest.mark.asyncio
async def test_list_channels_for_installation_scopes_across_clients(
    repo, pg_session: AsyncSession, test_user_db: User
):
    """Installation scoping matches on installation_id regardless of owning client."""
    # Two clients both register a channel for installation "acme"; a third installation differs.
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("a-slack", "acme")
    )
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-b", data=_channel("a-email", "acme")
    )
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("other", "globex")
    )
    await pg_session.commit()

    acme = await repo.list_channels_for_installation(pg_session, "acme")
    assert {c.name for c in acme} == {"a-slack", "a-email"}  # both clients, scoped to installation

    globex = await repo.list_channels_for_installation(pg_session, "globex")
    assert {c.name for c in globex} == {"other"}

    assert await repo.list_channels_for_installation(pg_session, "missing") == []


@pytest.mark.asyncio
async def test_list_all_channels_unbounded_by_default(
    repo, pg_session: AsyncSession, test_user_db: User
):
    """No limit returns every channel — the A2A and MCP callers rely on this."""
    for i in range(5):
        await repo.create_channel(
            db=pg_session, actor=test_user_db, client_id="client-a", data=_channel(f"chan-{i}", f"inst-{i}")
        )

    channels, total = await repo.list_all_channels(pg_session)
    assert len(channels) == 5
    assert total == 5


@pytest.mark.asyncio
async def test_list_all_channels_pages_with_accurate_total(
    repo, pg_session: AsyncSession, test_user_db: User
):
    """A page is capped, but total still counts every match."""
    for i in range(5):
        await repo.create_channel(
            db=pg_session, actor=test_user_db, client_id="client-a", data=_channel(f"chan-{i}", f"inst-{i}")
        )

    page1, total = await repo.list_all_channels(pg_session, page=1, limit=2)
    assert len(page1) == 2
    assert total == 5

    page3, total = await repo.list_all_channels(pg_session, page=3, limit=2)
    assert len(page3) == 1
    assert total == 5
    assert {c.id for c in page1}.isdisjoint({c.id for c in page3})


@pytest.mark.asyncio
async def test_list_all_channels_search_narrows_rows_and_total(
    repo, pg_session: AsyncSession, test_user_db: User
):
    """Search filters in SQL, so total reflects the search and not the table."""
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("ada-slack", "inst-a")
    )
    await repo.create_channel(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("nannos-email", "inst-b")
    )

    found, total = await repo.list_all_channels(pg_session, search="slack")
    assert [c.name for c in found] == ["ada-slack"]
    assert total == 1

    missing, total = await repo.list_all_channels(pg_session, search="no-such-channel")
    assert missing == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_channels_for_client_respects_search_and_scope(
    repo, pg_session: AsyncSession, test_user_db: User
):
    """Client scoping and search compose, rather than one overriding the other."""
    a, _ = await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("acme-slack", "inst-1")
    )
    await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-a", data=_channel("acme-email", "inst-2")
    )
    await repo.upsert_channel_by_installation(
        db=pg_session, actor=test_user_db, client_id="client-b", data=_channel("other-slack", "inst-3")
    )

    scoped, total = await repo.list_channels_for_client(pg_session, client_id="client-a")
    assert {c.name for c in scoped} == {"acme-slack", "acme-email"}
    assert total == 2

    narrowed, total = await repo.list_channels_for_client(
        pg_session, client_id="client-a", search="slack"
    )
    assert [c.name for c in narrowed] == ["acme-slack"]
    assert total == 1
    assert narrowed[0].id == a.id
