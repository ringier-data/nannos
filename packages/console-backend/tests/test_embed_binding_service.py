"""Embed bindings (ADR-0006): sync writes approved versions, connect activates by azp,
admin validation refuses what cannot be honoured. Collaborators are mocked; the SQL text
that reaches the session is asserted on."""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from console_backend.models.embed_binding import EmbedBinding, EmbedBindingUpsert
from console_backend.models.sub_agent import ActivationSource, SubAgentType
from console_backend.models.audit import AuditAction, AuditEntityType
from console_backend.models.user import User, UserRole, UserStatus
from console_backend.repositories.embed_binding_repository import EmbedBindingRepository
from console_backend.services import embed_binding_service as ebs
from console_backend.services.embed_binding_service import (
    EmbedBindingError,
    EmbedBindingService,
    _row_to_binding,
)
from console_backend.services.well_known_agent import (
    WellKnownAgent,
    WellKnownDefinition,
    WellKnownFetchError,
    WellKnownSkill,
    version_hash_for,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
BASE = "https://riad.example"
REV = "abc123def4567890"


def make_user(user_id: str = "admin-1", admin: bool = True) -> User:
    return User(
        id=user_id,
        sub=user_id,
        email=f"{user_id}@test",
        first_name="A",
        last_name="B",
        is_administrator=admin,
        role=UserRole.ADMIN if admin else UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


def make_binding(**over) -> EmbedBinding:
    data = dict(
        sub_agent_id=20,
        base_url=BASE,
        index_url=f"{BASE}/.well-known/agent-skills/index.json",
        azps=["nannos-embedded"],
        revision=None,
        created_by="admin-1",
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(over)
    return EmbedBinding(**data)


def make_definition(
    revision: str = REV,
    thinking: str | None = "low",
    model_tier: str | None = "standard",
    tools: list[str] | None = ("list_campaigns", "get_campaign"),
) -> WellKnownDefinition:
    agent = WellKnownAgent(
        name="Alloy AI Assistant",
        description="Helps with campaigns.",
        organization="Ringier Advertising",
        prompt_body="Domain guidance.",
        tools=list(tools) if tools is not None else None,
        model_tier=model_tier,
        thinking_level=thinking,
        url=f"{BASE}/.well-known/agent-skills/AGENT.md",
        digest="sha256:" + "a" * 64,
    )
    skills = [
        WellKnownSkill(
            name="book-line-items",
            description="Use when booking.",
            body="Steps.",
            url=f"{BASE}/.well-known/agent-skills/book-line-items/SKILL.md",
            digest="sha256:" + "b" * 64,
        )
    ]
    return WellKnownDefinition(
        base_url=BASE,
        index_url=f"{BASE}/.well-known/agent-skills/index.json",
        agent=agent,
        skills=skills,
        revision=revision,
        fetched_at=NOW,
        ttl_seconds=300,
    )


def make_service(fetch=None):
    sas = MagicMock()
    sas.publish_managed_version = AsyncMock(return_value=7)
    sas.repo.bulk_activate_sub_agent = AsyncMock(return_value=["user-1"])
    sas.get_sub_agent_by_id = AsyncMock(
        return_value=SimpleNamespace(id=20, type=SubAgentType.LOCAL, current_version=3)
    )
    users = MagicMock()
    users.get_user = AsyncMock(return_value=make_user())
    client = MagicMock()
    client.fetch = fetch or AsyncMock(return_value=make_definition())
    repo = EmbedBindingRepository()
    audit = MagicMock()
    audit.log_action = AsyncMock()
    repo.set_audit_service(audit)
    service = EmbedBindingService(
        sas, users, session_factory=MagicMock(), client=client, repository=repo
    )
    return service, sas, users, client


def make_db(first=None, all_rows=None):
    result = MagicMock()
    result.first.return_value = first
    result.all.return_value = all_rows or []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def executed_sql(db) -> list[str]:
    return [str(call.args[0]) for call in db.execute.await_args_list]


# ------------------------------------------------------------------------------- sync


@pytest.mark.asyncio
async def test_sync_new_revision_publishes_one_approved_version():
    service, sas, _, _ = make_service()
    service.get_binding = AsyncMock(
        side_effect=[make_binding(revision=None), make_binding(revision=REV)]
    )
    db = make_db()

    out = await service.sync_binding(db, 20)

    sas.publish_managed_version.assert_awaited_once()
    kwargs = sas.publish_managed_version.await_args.kwargs
    assert kwargs["version_hash"] == version_hash_for(REV) == "wkabc123def4"
    assert kwargs["description"] == "Helps with campaigns."
    assert kwargs["system_prompt"].startswith("You are Nannos, Ringier's AI assistant.")
    assert kwargs["system_prompt"].endswith("\n\nDomain guidance.")
    assert kwargs["mcp_tools"] == ["list_campaigns", "get_campaign"]
    assert kwargs["model_tier"] == "standard"
    assert kwargs["enable_thinking"] is True and kwargs["thinking_level"].value == "low"
    (skill,) = kwargs["skills"]
    assert (skill.name, skill.description, skill.body, skill.files, skill.scope) == (
        "book-line-items",
        "Use when booking.",
        "Steps.",
        [],
        "sub-agent",
    )
    assert REV in kwargs["change_summary"] and BASE in kwargs["change_summary"]
    # the actor is the admin who created the binding
    assert sas.publish_managed_version.await_args.args[1].id == "admin-1"

    assert any("revision = :revision" in sql for sql in executed_sql(db))
    assert out.revision == REV


def _binding_row(sub_agent_id: int) -> dict:
    return {
        "sub_agent_id": sub_agent_id,
        "base_url": BASE,
        "revision": REV,
        "definition": None,
        "fetched_at": NOW,
        "last_error": None,
        "last_error_at": None,
        "last_seen_at": None,
        "azps_seen": {},
        "created_by": "admin-1",
        "created_at": NOW,
        "updated_at": NOW,
        "azps": ["nannos-embedded"],
    }


@pytest.mark.asyncio
async def test_get_bindings_for_reads_a_whole_list_in_one_query():
    service, *_ = make_service()
    db = make_db()
    db.execute.return_value.mappings.return_value.all.return_value = [_binding_row(20), _binding_row(22)]

    found = await service.get_bindings_for(db, [20, 21, 22])

    assert set(found) == {20, 22} and found[20].base_url == BASE
    (sql,) = executed_sql(db)
    assert "b.sub_agent_id = ANY(:ids)" in sql
    assert db.execute.await_args.args[1] == {"ids": [20, 21, 22]}


@pytest.mark.asyncio
async def test_get_bindings_for_empty_list_skips_the_database():
    service, *_ = make_service()
    db = make_db()
    assert await service.get_bindings_for(db, []) == {}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_same_revision_only_touches_fetched_at():
    service, sas, _, _ = make_service()
    service.get_binding = AsyncMock(return_value=make_binding(revision=REV))
    db = make_db()

    await service.sync_binding(db, 20)

    sas.publish_managed_version.assert_not_awaited()
    sql = executed_sql(db)
    assert (
        len(sql) == 1
        and "fetched_at = :now" in sql[0]
        and "revision = :revision" not in sql[0]
    )


@pytest.mark.asyncio
async def test_sync_without_published_tools_leaves_them_to_nannos():
    service, sas, _, client = make_service()
    service.get_binding = AsyncMock(return_value=make_binding(revision=None))
    client.fetch = AsyncMock(return_value=make_definition(tools=None))
    await service.sync_binding(make_db(), 20)
    # None = "carry the sub-agent's own tool list forward" (publish_managed_version)
    assert sas.publish_managed_version.await_args.kwargs["mcp_tools"] is None


@pytest.mark.asyncio
async def test_sync_thinking_off_and_absent_defaults():
    service, sas, _, client = make_service()
    service.get_binding = AsyncMock(return_value=make_binding(revision=None))
    client.fetch = AsyncMock(
        return_value=make_definition(thinking="off", model_tier=None)
    )
    await service.sync_binding(make_db(), 20)
    kwargs = sas.publish_managed_version.await_args.kwargs
    assert (
        kwargs["enable_thinking"],
        kwargs["thinking_level"],
        kwargs["model_tier"],
    ) == (False, None, None)

    sas.publish_managed_version.reset_mock()
    client.fetch = AsyncMock(return_value=make_definition(thinking=None))
    await service.sync_binding(make_db(), 20)
    kwargs = sas.publish_managed_version.await_args.kwargs
    assert (kwargs["enable_thinking"], kwargs["thinking_level"]) == (None, None)


@pytest.mark.asyncio
async def test_sync_fetch_error_keeps_last_good_and_logs_once(caplog):
    error = WellKnownFetchError(BASE, "skill:book-line-items", "digest mismatch")
    service, sas, _, _ = make_service(fetch=AsyncMock(side_effect=error))
    service.get_binding = AsyncMock(
        return_value=make_binding(revision=REV, last_error=str(error))
    )
    db = make_db()

    with caplog.at_level(logging.WARNING, logger=ebs.__name__):
        out = await service.sync_binding(db, 20)
        await service.sync_binding(db, 20)

    sas.publish_managed_version.assert_not_awaited()
    assert all("last_error = :err" in sql for sql in executed_sql(db))
    assert out.revision == REV  # the previous version stays in force
    warnings = [r for r in caplog.records if "sync failed" in r.getMessage()]
    assert len(warnings) == 1, "the same error is logged once, not per tick"


@pytest.mark.asyncio
async def test_sync_falls_back_to_system_user_when_creator_is_gone():
    service, sas, users, _ = make_service()
    service.get_binding = AsyncMock(
        return_value=make_binding(revision=None, created_by="gone")
    )
    users.get_user = AsyncMock(side_effect=[None, make_user("system")])
    await service.sync_binding(make_db(), 20)
    assert sas.publish_managed_version.await_args.args[1].id == "system"
    assert [c.args[1] for c in users.get_user.await_args_list] == ["gone", "system"]


@pytest.mark.asyncio
async def test_sync_all_isolates_failures_per_binding():
    service, _, _, _ = make_service()
    ids_result = MagicMock()
    ids_result.all.return_value = [(1,), (2,)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=ids_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    service._session_factory = MagicMock(return_value=cm)
    service.sync_binding = AsyncMock(side_effect=[RuntimeError("boom"), make_binding()])

    await service.sync_all()

    assert [c.args[1] for c in service.sync_binding.await_args_list] == [1, 2]
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------- connect


@pytest.mark.asyncio
async def test_bind_connection_activates_and_records_sighting():
    service, sas, _, _ = make_service()
    db = make_db(first=(20,))
    user = make_user("user-1", admin=False)

    assert await service.bind_connection(db, user, "nannos-embedded") == 20

    sas.repo.bulk_activate_sub_agent.assert_awaited_once()
    kwargs = sas.repo.bulk_activate_sub_agent.await_args.kwargs
    assert kwargs["user_ids"] == ["user-1"] and kwargs["sub_agent_id"] == 20
    assert kwargs["activated_by"] == ActivationSource.EMBED
    assert any(
        "last_seen_at = :now" in sql and "azps_seen" in sql for sql in executed_sql(db)
    )
    assert service._azp_cache["nannos-embedded"][1] == 20


@pytest.mark.asyncio
async def test_bind_connection_unbound_azp_does_nothing():
    service, sas, _, _ = make_service()
    db = make_db(first=None)
    assert await service.bind_connection(db, make_user(), "agent-console") is None
    assert await service.bind_connection(db, make_user(), None) is None
    sas.repo.bulk_activate_sub_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_azp_lookup_is_cached_for_a_minute():
    service, _, _, _ = make_service()
    db = make_db(first=(20,))
    assert await service.sub_agent_id_for_azp("nannos-embedded", db) == 20
    assert await service.sub_agent_id_for_azp("nannos-embedded", db) == 20
    assert db.execute.await_count == 1
    service._azp_cache.clear()
    assert await service.sub_agent_id_for_azp("nannos-embedded", db) == 20
    assert db.execute.await_count == 2


# ------------------------------------------------------------------------------ admin


@pytest.mark.asyncio
async def test_upsert_refuses_http_outside_local(monkeypatch):
    service, _, _, _ = make_service()
    monkeypatch.setattr(ebs.config, "environment", "dev")
    with pytest.raises(EmbedBindingError, match="https"):
        await service.upsert_binding(
            make_db(),
            make_user(),
            20,
            EmbedBindingUpsert(
                base_url="http://localhost:3000", azps=["nannos-embedded"]
            ),
        )


def test_default_client_allows_private_authorities_only_in_local(monkeypatch):
    monkeypatch.setattr(ebs.config, "environment", "dev")
    service = EmbedBindingService(MagicMock(), MagicMock(), session_factory=MagicMock())
    assert service._client._allow_private is False

    monkeypatch.setattr(ebs.config, "environment", "local")
    service = EmbedBindingService(MagicMock(), MagicMock(), session_factory=MagicMock())
    assert service._client._allow_private is True


@pytest.mark.asyncio
async def test_upsert_refuses_non_local_sub_agent_and_taken_azp(monkeypatch):
    service, sas, _, _ = make_service()
    sas.get_sub_agent_by_id = AsyncMock(
        return_value=SimpleNamespace(id=20, type=SubAgentType.REMOTE, current_version=1)
    )
    with pytest.raises(EmbedBindingError, match="Only local"):
        await service.upsert_binding(
            make_db(), make_user(), 20, EmbedBindingUpsert(base_url=BASE, azps=["x"])
        )

    sas.get_sub_agent_by_id = AsyncMock(
        return_value=SimpleNamespace(id=20, type=SubAgentType.LOCAL, current_version=1)
    )
    db = make_db(all_rows=[("nannos-embedded", 3)])
    with pytest.raises(EmbedBindingError, match="sub-agent 3"):
        await service.upsert_binding(
            db,
            make_user(),
            20,
            EmbedBindingUpsert(base_url=BASE, azps=["nannos-embedded"]),
        )

    sas.get_sub_agent_by_id = AsyncMock(return_value=None)
    with pytest.raises(LookupError):
        await service.upsert_binding(
            make_db(), make_user(), 99, EmbedBindingUpsert(base_url=BASE, azps=["x"])
        )


@pytest.mark.asyncio
async def test_upsert_writes_rows_and_syncs_immediately(monkeypatch):
    service, _, _, _ = make_service()
    monkeypatch.setattr(ebs.config, "environment", "local")
    service._azp_cache["stale"] = (float("inf"), 1)
    service.sync_binding = AsyncMock(return_value=make_binding(revision=REV))
    db = make_db(all_rows=[])

    out = await service.upsert_binding(
        db,
        make_user(),
        20,
        EmbedBindingUpsert(
            base_url="http://localhost:3000/", azps=["nannos-embedded", "cockpit-embed"]
        ),
    )

    sql = executed_sql(db)
    assert any("INSERT INTO sub_agent_embed_bindings" in s for s in sql)
    # A new host clears the old revision AND the old definition, so the admin view never
    # attributes the previous host's agent and skills to the new URL.
    assert any("revision = CASE" in s and "definition = CASE" in s for s in sql)
    assert any("DELETE FROM sub_agent_embed_binding_azps" in s for s in sql)
    assert sum("INSERT INTO sub_agent_embed_binding_azps" in s for s in sql) == 2
    insert_params = [
        c.args[1]
        for c in db.execute.await_args_list
        if "INSERT INTO sub_agent_embed_bindings" in str(c.args[0])
    ]
    assert (
        insert_params[0]["base_url"] == "http://localhost:3000"
    )  # trailing slash normalized by the model
    service.sync_binding.assert_awaited_once_with(db, 20, force=True)
    assert service._azp_cache == {}
    assert out.revision == REV


def _create_request(azps=("nannos-embedded",)) -> EmbedBindingUpsert:
    return EmbedBindingUpsert(base_url=BASE, azps=list(azps))


@pytest.mark.asyncio
async def test_create_bound_sub_agent_fetches_before_writing_anything():
    service, sas, _, _ = make_service(
        fetch=AsyncMock(side_effect=WellKnownFetchError(BASE, "index.json", "HTTP 404"))
    )
    sas.create_managed_sub_agent = AsyncMock(return_value=21)
    db = make_db(all_rows=[])

    with pytest.raises(EmbedBindingError, match="Could not read the host definition"):
        await service.create_bound_sub_agent(db, make_user(), _create_request())

    sas.create_managed_sub_agent.assert_not_awaited()
    assert not any("INSERT" in s for s in executed_sql(db))


@pytest.mark.asyncio
async def test_create_bound_sub_agent_refuses_taken_azp_before_fetching():
    service, sas, _, client = make_service()
    sas.create_managed_sub_agent = AsyncMock(return_value=21)
    db = make_db(all_rows=[("nannos-embedded", 20)])

    with pytest.raises(EmbedBindingError, match="already bound to another sub-agent"):
        await service.create_bound_sub_agent(db, make_user(), _create_request())

    client.fetch.assert_not_awaited()
    sas.create_managed_sub_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_bound_sub_agent_creates_row_binds_and_publishes_once():
    service, sas, _, client = make_service()
    sas.create_managed_sub_agent = AsyncMock(return_value=21)
    service.get_binding = AsyncMock(
        side_effect=[
            make_binding(sub_agent_id=21),  # just written, no revision yet
            make_binding(sub_agent_id=21, revision=REV),  # after the publish
        ]
    )
    db = make_db(all_rows=[])

    out = await service.create_bound_sub_agent(
        db, make_user(), _create_request(["nannos-embedded", "cockpit-embed"])
    )

    client.fetch.assert_awaited_once_with(BASE, force=True)
    sas.create_managed_sub_agent.assert_awaited_once()
    # The row gets the derived name, not the host's display name: a sub-agent name is also
    # the orchestrator's task-tool identifier and cannot hold a space.
    assert (
        sas.create_managed_sub_agent.await_args.kwargs["name"] == "Alloy-AI-Assistant"
    )
    sas.publish_managed_version.assert_awaited_once()
    assert sas.publish_managed_version.await_args.args[2] == 21
    sql = executed_sql(db)
    assert any("INSERT INTO sub_agent_embed_bindings" in s for s in sql)
    assert sum("INSERT INTO sub_agent_embed_binding_azps" in s for s in sql) == 2
    assert any("SET revision = :revision" in s for s in sql)
    assert out.sub_agent_id == 21 and out.revision == REV


def test_upsert_model_validation():
    assert EmbedBindingUpsert(
        base_url="https://riad.alloy.ch/", azps=[" a ", "a", "b"]
    ).azps == ["a", "b"]
    assert (
        EmbedBindingUpsert(base_url="https://riad.alloy.ch/", azps=["a"]).base_url
        == "https://riad.alloy.ch"
    )
    for bad in (
        "https://riad.alloy.ch/path",
        "ftp://x",
        "https://user:pw@riad.alloy.ch",
        "riad.alloy.ch",
    ):
        with pytest.raises(ValueError):
            EmbedBindingUpsert(base_url=bad, azps=["a"])
    with pytest.raises(ValueError):
        EmbedBindingUpsert(base_url="https://riad.alloy.ch", azps=["has space"])
    with pytest.raises(ValueError):
        EmbedBindingUpsert(base_url="https://riad.alloy.ch", azps=[])


def test_row_to_binding_maps_json_columns():
    row = {
        "sub_agent_id": 20,
        "base_url": BASE,
        "revision": REV,
        "definition": '{"agent": {"name": "A", "description": "d", "prompt_url": "u", "prompt_digest": "sha256:x", "tools": ["t"]}, "skills": [{"name": "s", "url": "u2", "digest": "sha256:y"}]}',
        "fetched_at": NOW,
        "last_error": None,
        "last_error_at": None,
        "last_seen_at": NOW,
        "azps_seen": {"nannos-embedded": NOW.isoformat()},
        "created_by": "admin-1",
        "created_at": NOW,
        "updated_at": NOW,
        "azps": ["nannos-embedded"],
    }
    binding = _row_to_binding(row)
    assert binding.version_hash == "wkabc123def4"
    assert binding.index_url.endswith("/.well-known/agent-skills/index.json")
    assert binding.agent is not None and binding.agent.tools == ["t"]
    assert [s.name for s in binding.skills] == ["s"]
    assert binding.azps_seen == {"nannos-embedded": NOW.isoformat()}


# ------------------------------------------------------------------------------ probe


@pytest.mark.asyncio
async def test_probe_reports_what_the_authority_publishes_without_writing():
    service, sas, _, client = make_service()

    probe = await service.probe(f"{BASE}/")

    assert probe.ok is True
    assert probe.base_url == BASE
    assert probe.revision == REV
    assert probe.agent is not None
    assert probe.agent.name == "Alloy AI Assistant"
    assert [s.name for s in probe.skills] == ["book-line-items"]
    client.fetch.assert_awaited_once_with(BASE, force=True)
    sas.create_managed_sub_agent.assert_not_called()


@pytest.mark.asyncio
async def test_probe_returns_the_fetch_failure_instead_of_raising():
    service, _, _, _ = make_service(
        fetch=AsyncMock(side_effect=WellKnownFetchError(BASE, "index", "HTTP 404"))
    )

    probe = await service.probe(BASE)

    assert probe.ok is False
    assert probe.agent is None
    assert "HTTP 404" in (probe.error or "")


@pytest.mark.asyncio
async def test_probe_reports_a_malformed_origin_inline():
    service, _, _, client = make_service()

    probe = await service.probe("riad.example/agents")

    assert probe.ok is False
    assert "https://" in (probe.error or "")
    client.fetch.assert_not_awaited()



# ------------------------------------------------------------------------------ audit


@pytest.mark.asyncio
async def test_binding_writes_are_audited_as_an_update_of_the_sub_agent(monkeypatch):
    """Binding changes decide which token azp maps to which agent: an admin action with
    authorization impact, so it lands in audit_logs — on the sub-agent, before and after."""
    service, _, _, _ = make_service()
    monkeypatch.setattr(ebs.config, "environment", "local")
    service.sync_binding = AsyncMock(return_value=make_binding(revision=REV))
    audit = service._repo.audit_service
    # The snapshot SELECT sees a previous binding to another host.
    db = make_db(first=("https://old.example", ["old-azp"]), all_rows=[])

    await service.upsert_binding(
        db, make_user(), 20, EmbedBindingUpsert(base_url=BASE, azps=["nannos-embedded"])
    )

    audit.log_action.assert_awaited_once()
    kwargs = audit.log_action.await_args.kwargs
    assert kwargs["entity_type"] == AuditEntityType.SUB_AGENT
    assert kwargs["entity_id"] == "20" and kwargs["action"] == AuditAction.UPDATE
    assert kwargs["changes"] == {
        "before": {"embed_binding": {"base_url": "https://old.example", "azps": ["old-azp"]}},
        "after": {"embed_binding": {"base_url": BASE, "azps": ["nannos-embedded"]}},
    }


@pytest.mark.asyncio
async def test_delete_binding_is_audited_and_reports_whether_anything_was_bound():
    service, _, _, _ = make_service()
    audit = service._repo.audit_service
    service._azp_cache["nannos-embedded"] = (float("inf"), 20)

    db = make_db(first=(BASE, ["nannos-embedded"]))
    assert await service.delete_binding(db, make_user(), 20) is True
    assert any("DELETE FROM sub_agent_embed_bindings" in s for s in executed_sql(db))
    assert service._azp_cache == {}
    kwargs = audit.log_action.await_args.kwargs
    assert kwargs["action"] == AuditAction.UPDATE and kwargs["entity_id"] == "20"
    assert kwargs["changes"]["after"] == {"embed_binding": None}
    assert kwargs["changes"]["before"] == {
        "embed_binding": {"base_url": BASE, "azps": ["nannos-embedded"]}
    }

    audit.log_action.reset_mock()
    unbound_db = make_db(first=None)
    assert await service.delete_binding(unbound_db, make_user(), 20) is False
    assert not any("DELETE FROM sub_agent_embed_bindings" in s for s in executed_sql(unbound_db))
    audit.log_action.assert_not_awaited()
