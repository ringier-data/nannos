"""ADR-0013 against a real database: an agent's OWN skill edited outside a config save
writes the owner's config version, the owner is pinned by hash like a referrer, a revert
restores skill content, and the version goes through the auto-approve rules.
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.config import config
from console_backend.models.skills_registry import SkillFile
from console_backend.models.sub_agent import (
    SkillDefinition,
    SubAgentCreate,
    SubAgentType,
    SubAgentUpdate,
)
from console_backend.models.user import User
from console_backend.services.skill_registry_service import SkillRegistryService
from console_backend.services.sub_agent_service import PromptLimitError, SubAgentService
from tests.test_skill_reference_modes import _agent, _default_version, _publish_skill, _resolved_skill

MIGRATION = Path(__file__).parent.parent / "sqlmigrations" / "ddl" / "107_pin_owned_skill_refs.sql"


@pytest.fixture
def prompt_limit(monkeypatch):
    def set_limit(n: int) -> None:
        monkeypatch.setattr(config.auto_approve, "max_system_prompt_length", n)

    return set_limit


async def _own_skill(
    svc: SubAgentService, db: AsyncSession, actor: User, agent_id: int, body: str, *, inline: bool = False
) -> tuple[str, str]:
    """Write an own skill through a config save. Returns (registry_id, content_hash)."""
    await svc.update_sub_agent(
        db,
        agent_id,
        SubAgentUpdate(skills=[SkillDefinition(name="kb", description="knowledge", body=body, inline=inline)]),
        actor,
    )
    agent = await svc.get_sub_agent_by_id(db, agent_id)
    assert agent and agent.config_version
    skill = agent.config_version.skills[0]
    assert skill.registry_id and skill.content_hash
    return skill.registry_id, skill.content_hash


def _skill_md(body: str) -> list[SkillFile]:
    return [SkillFile(path="SKILL.md", content=f"---\nname: kb\ndescription: knowledge\n---\n{body}\n")]


async def _registry_edit(registry: SkillRegistryService, db: AsyncSession, actor: User, registry_id: str, body: str):
    """The out-of-config edit: registry UI and the MCP skill tools all end here."""
    return await registry.update_skill(db, actor, registry_id, files=_skill_md(body))


async def _resolved_at(svc: SubAgentService, db: AsyncSession, agent_id: int, version: int) -> SkillDefinition:
    agent = await svc.get_sub_agent_by_id(db, agent_id, version=version)
    assert agent and agent.config_version
    await svc.resolve_imported_skills(db, agent)
    return agent.config_version.skills[0]


async def _version_count(db: AsyncSession, agent_id: int) -> int:
    return (
        await db.execute(
            text("SELECT count(*) FROM sub_agent_config_versions WHERE sub_agent_id = :id"), {"id": agent_id}
        )
    ).scalar_one()


async def _row_hash(db: AsyncSession, registry_id: str) -> str:
    return (
        await db.execute(text("SELECT content_hash FROM skill_registry WHERE id = CAST(:id AS uuid)"), {"id": registry_id})
    ).scalar_one()


@pytest.mark.asyncio
async def test_a_registry_edit_writes_the_owners_version_signed_by_the_editor(wired, pg_session, test_user_db):
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    before = await _version_count(pg_session, agent_id)

    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()

    assert result.entry.content_hash != v1_hash
    assert result.owner_version is not None
    assert result.owner_version.sub_agent_id == agent_id
    assert result.owner_version.approved is True
    assert await _version_count(pg_session, agent_id) == before + 1

    default = await _default_version(pg_session, agent_id)
    assert default["default_version"] == result.owner_version.version
    assert default["status"] == "approved"
    assert default["approved_by_user_id"] == test_user_db.id
    assert default["change_summary"].startswith("Edited skill 'kb'")

    own = await _resolved_skill(svc, pg_session, agent_id)
    assert own.body == "v2"
    assert own.content_hash == result.entry.content_hash
    assert own.mode is None
    assert own.update_available is False


@pytest.mark.asyncio
async def test_the_owner_is_pinned_so_a_revert_restores_skill_content(wired, pg_session, test_user_db):
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    v1_version = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version
    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()
    v2_hash = result.entry.content_hash

    reverted = await svc.revert_to_version(pg_session, agent_id, v1_version, actor=test_user_db)
    assert reverted and reverted.config_version

    # The reverted version serves v1, even though the registry row still holds v2, and it
    # says that the row has moved on.
    skill = await _resolved_at(svc, pg_session, agent_id, reverted.config_version.version)
    assert skill.body == "v1"
    assert skill.content_hash == v1_hash
    assert skill.update_available is True
    assert skill.latest_hash == v2_hash
    assert skill.mode is None
    assert await _row_hash(pg_session, registry_id) == v2_hash


@pytest.mark.asyncio
async def test_a_config_save_of_an_own_skill_writes_exactly_one_version(wired, pg_session, test_user_db):
    """The owner-edit hook must not fire from the config-save path, which writes its own version."""
    svc, _, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    before = await _version_count(pg_session, agent_id)

    await svc.update_sub_agent(
        pg_session,
        agent_id,
        SubAgentUpdate(
            skills=[
                SkillDefinition(name="kb", description="knowledge", body="v2", registry_id=registry_id, scope="sub-agent")
            ]
        ),
        test_user_db,
    )
    assert await _version_count(pg_session, agent_id) == before + 1
    assert (await _resolved_skill(svc, pg_session, agent_id)).body == "v2"


@pytest.mark.asyncio
async def test_a_local_agent_over_the_limit_gets_a_pending_version_and_keeps_running_the_old_content(
    wired, pg_session, test_user_db, prompt_limit
):
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "x" * 20, inline=True)
    approved_before = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version
    prompt_limit(50)

    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "y" * 80)
    await pg_session.commit()

    assert result.owner_version is not None
    assert result.owner_version.approved is False
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id)
    assert agent.default_version == approved_before
    assert agent.current_version == result.owner_version.version

    # What runs is the approved default: the previous content, flagged as behind the row.
    running = await _resolved_at(svc, pg_session, agent_id, approved_before)
    assert running.body == "x" * 20
    assert running.content_hash == v1_hash
    assert running.update_available is True
    # The pending version holds the edit.
    pending = await _resolved_at(svc, pg_session, agent_id, result.owner_version.version)
    assert pending.body == "y" * 80
    assert pending.update_available is False


@pytest.mark.asyncio
async def test_the_summary_names_the_baselines_hash_not_the_rows(wired, pg_session, test_user_db, prompt_limit):
    """A second edit while the first owner version is pending changes the skill from the approved hash."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "x" * 20, inline=True)
    prompt_limit(50)
    first = await _registry_edit(registry, pg_session, test_user_db, registry_id, "y" * 80)
    assert first.owner_version is not None and first.owner_version.approved is False

    second = await _registry_edit(registry, pg_session, test_user_db, registry_id, "z" * 80)
    await pg_session.commit()

    assert second.owner_version is not None
    summary = (
        await pg_session.execute(
            text("SELECT change_summary FROM sub_agent_config_versions WHERE sub_agent_id = :id AND version = :v"),
            {"id": agent_id, "v": second.owner_version.version},
        )
    ).scalar_one()
    assert summary == f"Edited skill 'kb' {v1_hash[:12]} -> {second.entry.content_hash[:12]}"


@pytest.mark.asyncio
async def test_an_automated_agent_over_the_limit_refuses_the_edit(wired, pg_session, test_user_db, prompt_limit):
    svc, registry, _ = wired
    agent = await svc.create_sub_agent(
        pg_session,
        SubAgentCreate(
            name="kb-automated",
            type=SubAgentType.AUTOMATED,
            description="automated",
            model="gpt-4o",
            system_prompt="short prompt",
            mcp_tools=[],
            skills=[SkillDefinition(name="kb", description="knowledge", body="x" * 20, inline=True)],
        ),
        test_user_db,
    )
    resolved = await _resolved_skill(svc, pg_session, agent.id)
    registry_id, v1_hash = resolved.registry_id, resolved.content_hash
    assert registry_id and v1_hash
    versions_before = await _version_count(pg_session, agent.id)
    prompt_limit(50)

    with pytest.raises(PromptLimitError):
        await _registry_edit(registry, pg_session, test_user_db, registry_id, "y" * 80)
    await pg_session.rollback()

    # Nothing landed: neither the row nor a version.
    assert await _row_hash(pg_session, registry_id) == v1_hash
    assert await _version_count(pg_session, agent.id) == versions_before


@pytest.mark.asyncio
async def test_the_owner_version_is_built_from_the_approved_default_not_a_pending_draft(
    wired, pg_session, test_user_db, prompt_limit
):
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    approved = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version

    # A draft that waits for approval: a prompt over the limit.
    prompt_limit(20)
    await svc.update_sub_agent(pg_session, agent_id, SubAgentUpdate(system_prompt="p" * 100), test_user_db)
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id)
    draft = agent.current_version
    assert draft != approved

    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()

    assert result.owner_version is not None and result.owner_version.approved is True
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id, version=result.owner_version.version)
    assert agent.config_version.system_prompt == "short prompt"  # the approved default's prompt, not the draft's
    status = (
        await pg_session.execute(
            text("SELECT status FROM sub_agent_config_versions WHERE sub_agent_id = :id AND version = :v"),
            {"id": agent_id, "v": draft},
        )
    ).scalar_one()
    assert status != "approved"


@pytest.mark.asyncio
async def test_mcp_self_update_after_an_owner_edit_writes_no_second_version(wired, pg_session, test_user_db):
    """The MCP tools call self_update after update_skill; the two must compose into one version."""
    svc, registry, activation = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    await activation.activate(
        pg_session, registry_id, agent_id, "kb-owner", "sub-agent", test_user_db.id, actor=test_user_db
    )
    before = await _version_count(pg_session, agent_id)

    await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await activation.self_update(
        pg_session, registry_id=registry_id, sub_agent_id=agent_id, agent_name="kb-owner", actor=test_user_db
    )
    await pg_session.commit()

    assert await _version_count(pg_session, agent_id) == before + 1
    assert (await _resolved_skill(svc, pg_session, agent_id)).body == "v2"


@pytest.mark.asyncio
async def test_a_referrers_pin_is_untouched_by_the_publishers_own_version(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    referrer = await _agent(svc, pg_session, test_user_db, "kb-referrer")
    registry_id, v1_hash = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")
    await activation.activate(
        pg_session, registry_id, referrer, "kb-referrer", "sub-agent", test_user_db.id, actor=test_user_db
    )

    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()

    assert result.owner_version is not None and result.owner_version.sub_agent_id == publisher
    assert (await _resolved_skill(svc, pg_session, publisher)).body == "v2"
    pinned = await _resolved_skill(svc, pg_session, referrer)
    assert pinned.body == "v1"
    assert pinned.content_hash == v1_hash
    assert pinned.update_available is True


@pytest.mark.asyncio
async def test_an_embed_bound_agent_is_left_to_the_host_sync(wired, pg_session, test_user_db):
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-bound")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    await pg_session.execute(
        text(
            "INSERT INTO sub_agent_embed_bindings (sub_agent_id, base_url, created_by) "
            "VALUES (:id, 'https://host.example', :user)"
        ),
        {"id": agent_id, "user": test_user_db.id},
    )
    before = await _version_count(pg_session, agent_id)

    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()

    assert result.owner_version is None
    assert await _version_count(pg_session, agent_id) == before


# --- Migration 107: existing owned refs are re-pointed to what the version actually served ---


def _backfill_statement() -> str:
    body = MIGRATION.read_text().split("-- backfill:pin-owned-skill-refs", 1)[1]
    statement = body.split(";", 1)[0].strip()
    assert statement.upper().startswith("UPDATE SUB_AGENT_CONFIG_VERSIONS"), statement
    return statement


def test_the_migration_marker_is_still_there():
    assert "-- backfill:pin-owned-skill-refs" in MIGRATION.read_text()


@pytest.mark.asyncio
async def test_migration_repoints_owned_refs_in_every_version_and_leaves_references_alone(
    wired, pg_session, test_user_db
):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    owner = await _agent(svc, pg_session, test_user_db, "kb-owner")
    pub_id, pub_v1 = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "pub v1")
    own_id, own_hash = await _own_skill(svc, pg_session, test_user_db, owner, "own v1")
    await activation.activate(pg_session, pub_id, owner, "kb-owner", "sub-agent", test_user_db.id, actor=test_user_db)
    # The publisher moves on; the owner's reference to it stays pinned at pub_v1.
    await _registry_edit(registry, pg_session, test_user_db, pub_id, "pub v2")
    await pg_session.commit()

    # Age the owner's refs the way pre-ADR-0013 data looks: every version's own ref stale.
    await pg_session.execute(
        text(
            "UPDATE sub_agent_config_versions SET skills = ("
            "  SELECT jsonb_agg(CASE WHEN s->>'registry_id' = :own THEN s || '{\"content_hash\": \"stale\"}' ELSE s END)"
            "  FROM jsonb_array_elements(skills) s) WHERE sub_agent_id = :id AND skills <> '[]'::jsonb"
        ),
        {"own": own_id, "id": owner},
    )
    await pg_session.commit()

    await pg_session.execute(text(_backfill_statement()))
    await pg_session.commit()

    rows = (
        await pg_session.execute(
            text("SELECT skills FROM sub_agent_config_versions WHERE sub_agent_id = :id AND skills <> '[]'::jsonb"),
            {"id": owner},
        )
    ).scalars().all()
    assert rows
    for skills in rows:
        by_id = {s["registry_id"]: s["content_hash"] for s in skills}
        assert by_id[own_id] == own_hash
        if pub_id in by_id:
            assert by_id[pub_id] == pub_v1
