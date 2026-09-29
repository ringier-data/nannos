"""ADR-0013 against a real database: an agent's OWN skill edited outside a config save
writes the owner's config version, the owner is pinned by hash like a referrer, a revert
restores skill content, and the version goes through the auto-approve rules.
"""

import asyncio
import json
import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from console_backend.config import config
from console_backend.models.skills_registry import SkillFile
from console_backend.models.sub_agent import (
    SkillDefinition,
    SubAgentCreate,
    SubAgentType,
    SubAgentUpdate,
)
from console_backend.models.user import User
from console_backend.repositories.sub_agent_repository import SubAgentRepository
from console_backend.routers.skills_registry_router import _with_file
from console_backend.services.skill_registry_service import SkillRegistryService, _compute_content_hash
from console_backend.services.sub_agent_service import PromptLimitError, SubAgentService
from tests.test_skill_reference_modes import _agent, _default_version, _publish_skill, _resolved_skill

MIGRATION = Path(__file__).parent.parent / "sqlmigrations" / "ddl" / "114_pin_owned_skill_refs.sql"


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
    release = (
        await pg_session.execute(
            text("SELECT release_number FROM sub_agent_config_versions WHERE sub_agent_id = :id AND version = :v"),
            {"id": agent_id, "v": result.owner_version.version},
        )
    ).scalar_one()
    assert release is not None

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

    # The reverted version serves v1, and the row is back at v1 too: a revert is an edit
    # of the agent's own skill, so nothing built on the row later brings v2 back.
    skill = await _resolved_at(svc, pg_session, agent_id, reverted.config_version.version)
    assert skill.body == "v1"
    assert skill.content_hash == v1_hash
    assert skill.update_available is False
    assert skill.mode is None
    assert await _row_hash(pg_session, registry_id) == v1_hash
    assert v2_hash != v1_hash


@pytest.mark.asyncio
async def test_a_file_edit_after_a_revert_builds_on_the_reverted_content(wired, pg_session, test_user_db):
    """The MCP file tools build on the row; before a revert moved the row, this brought v2 back."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    v1_version = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version
    await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()
    await svc.revert_to_version(pg_session, agent_id, v1_version, actor=test_user_db)

    result = await registry.update_skill(
        pg_session, test_user_db, registry_id, edit_files=_with_file("notes.md", "n")
    )
    await pg_session.commit()

    files = {f.path: f.content for f in result.entry.files}
    assert files["SKILL.md"].rstrip().endswith("v1") and files["notes.md"] == "n"
    assert result.owner_version is not None
    assert (await _resolved_at(svc, pg_session, agent_id, result.owner_version.version)).body == "v1"


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
async def test_an_edit_back_to_the_approved_hash_while_a_version_is_pending_makes_the_default_current(
    wired, pg_session, test_user_db, prompt_limit
):
    """Left current, the pending version would bring the edited-away content back on approval."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "x" * 20, inline=True)
    v1_files = (await registry.get_by_id(pg_session, registry_id)).files
    approved = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version
    prompt_limit(50)
    first = await _registry_edit(registry, pg_session, test_user_db, registry_id, "y" * 80)
    assert first.owner_version is not None and first.owner_version.approved is False
    versions = await _version_count(pg_session, agent_id)

    back = await registry.update_skill(pg_session, test_user_db, registry_id, files=v1_files)
    await pg_session.commit()

    assert back.entry.content_hash == v1_hash
    assert back.owner_version is not None
    assert back.owner_version.version == approved and back.owner_version.approved is True
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id)
    assert agent.current_version == approved and agent.default_version == approved
    assert await _version_count(pg_session, agent_id) == versions  # the pending version survives, not current


@pytest.mark.asyncio
async def test_an_edit_back_to_the_approved_hash_leaves_a_draft_that_dropped_the_skill_current(
    wired, pg_session, test_user_db, prompt_limit
):
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    v1_files = (await registry.get_by_id(pg_session, registry_id)).files
    # The row has moved on while the approved default still pins v1.
    await pg_session.execute(
        text("UPDATE skill_registry SET content_hash = 'elsewhere' WHERE id = CAST(:id AS uuid)"), {"id": registry_id}
    )
    prompt_limit(20)
    await svc.update_sub_agent(
        pg_session, agent_id, SubAgentUpdate(system_prompt="p" * 100, skills=[]), test_user_db
    )  # a pending draft without the skill
    draft = (await svc.get_sub_agent_by_id(pg_session, agent_id)).current_version

    back = await registry.update_skill(pg_session, test_user_db, registry_id, files=v1_files)
    await pg_session.commit()

    assert back.owner_version is None
    assert (await svc.get_sub_agent_by_id(pg_session, agent_id)).current_version == draft


@pytest.mark.asyncio
async def test_an_own_skill_only_a_pending_draft_holds_gets_a_draft_version_with_the_edit(
    wired, pg_session, test_user_db, prompt_limit
):
    """Left pinned at the old hash, the draft's next config save would write the old body back over the edit."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    approved = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version
    prompt_limit(20)
    await svc.update_sub_agent(
        pg_session,
        agent_id,
        SubAgentUpdate(
            system_prompt="p" * 100, skills=[SkillDefinition(name="kb", description="knowledge", body="v1")]
        ),
        test_user_db,
    )
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id)
    registry_id = agent.config_version.skills[0].registry_id
    assert agent.current_version != approved

    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.commit()

    assert result.owner_version is not None and result.owner_version.approved is False
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id)
    assert agent.default_version == approved  # the draft is not promoted
    assert agent.current_version == result.owner_version.version
    assert agent.config_version.system_prompt == "p" * 100  # built from the draft
    assert (await _resolved_at(svc, pg_session, agent_id, agent.current_version)).body == "v2"


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


@asynccontextmanager
async def _other_session(postgres_with_migrations):
    """A second connection to the test database, for a transaction that runs alongside pg_session."""
    engine = create_async_engine(postgres_with_migrations["dsn"])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await session.execute(text(f"SET search_path TO {postgres_with_migrations['schema']}"))
            await session.commit()
            yield session
    finally:
        await engine.dispose()


async def _wait_until_blocked(db: AsyncSession) -> None:
    for _ in range(100):
        waiting = (
            await db.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        ).scalar_one()
        await db.rollback()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the edit never waited on a lock")


@pytest.mark.asyncio
async def test_an_edit_takes_the_owner_before_the_row_and_reads_the_row_again_under_it(
    wired, pg_session, test_user_db, postgres_with_migrations
):
    """Lock order agent-before-row (else it deadlocks against a config save), and the hash gate
    sees what a config save it waited behind wrote: an edit back to the old content is a change."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    v1_files = (await registry.get_by_id(pg_session, registry_id)).files
    await pg_session.commit()

    async with _other_session(postgres_with_migrations) as saver, _other_session(postgres_with_migrations) as probe:
        await SubAgentRepository.lock_for_update(saver, agent_id)  # a config save has started
        edit = asyncio.create_task(registry.update_skill(pg_session, test_user_db, registry_id, files=v1_files))
        try:
            await _wait_until_blocked(probe)
            # While the edit waits on the owner, it holds nothing on the row.
            try:
                await probe.execute(
                    text("SELECT id FROM skill_registry WHERE id = CAST(:id AS uuid) FOR UPDATE NOWAIT"),
                    {"id": registry_id},
                )
            except DBAPIError:
                pytest.fail("the edit locked the registry row before the owner")
            await probe.rollback()

            # The config save moves the row to v2 and commits; only then does the edit run.
            await svc.update_sub_agent(
                saver,
                agent_id,
                SubAgentUpdate(
                    skills=[
                        SkillDefinition(
                            name="kb", description="knowledge", body="v2", registry_id=registry_id, scope="sub-agent"
                        )
                    ]
                ),
                test_user_db,
            )
            await saver.commit()
            result = await asyncio.wait_for(edit, timeout=10)
        finally:
            if not edit.done():
                edit.cancel()
    await pg_session.commit()

    assert result.entry.content_hash == v1_hash
    assert result.owner_version is not None and result.owner_version.approved is True
    own = await _resolved_skill(svc, pg_session, agent_id)
    assert own.body == "v1" and own.content_hash == v1_hash


@pytest.mark.asyncio
async def test_a_single_file_write_keeps_a_concurrent_config_saves_body(
    wired, pg_session, test_user_db, postgres_with_migrations
):
    """The file set is derived under the row lock, not from the caller's read before it."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, _ = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    await pg_session.commit()

    v2_files = [SkillFile(path="SKILL.md", content="---\nname: kb\ndescription: knowledge\n---\nv2\n")]

    async with _other_session(postgres_with_migrations) as saver, _other_session(postgres_with_migrations) as probe:
        # A config save's write of the row, still uncommitted: owner locked, then the row.
        await SubAgentRepository.lock_for_update(saver, agent_id)
        await saver.execute(
            text(
                "UPDATE skill_registry SET files = CAST(:files AS jsonb), content_hash = :hash "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {
                "files": json.dumps([f.model_dump() for f in v2_files]),
                "hash": _compute_content_hash(v2_files),
                "id": registry_id,
            },
        )
        write = asyncio.create_task(
            registry.update_skill(pg_session, test_user_db, registry_id, edit_files=_with_file("notes.md", "n"))
        )
        try:
            await _wait_until_blocked(probe)
            await saver.commit()
            result = await asyncio.wait_for(write, timeout=10)
        finally:
            if not write.done():
                write.cancel()
    await pg_session.commit()

    files = {f.path: f.content for f in result.entry.files}
    assert files["SKILL.md"] == v2_files[0].content and files["notes.md"] == "n"


@pytest.mark.asyncio
async def test_an_embed_bound_agents_own_skill_edit_is_refused(wired, pg_session, test_user_db):
    """The host publishes its skills: the edit would never run and the next sync would overwrite it."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-bound")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "v1")
    await pg_session.execute(
        text(
            "INSERT INTO sub_agent_embed_bindings (sub_agent_id, base_url, created_by) "
            "VALUES (:id, 'https://host.example', :user)"
        ),
        {"id": agent_id, "user": test_user_db.id},
    )
    await pg_session.commit()
    before = await _version_count(pg_session, agent_id)

    with pytest.raises(ValueError, match="embed-bound"):
        await _registry_edit(registry, pg_session, test_user_db, registry_id, "v2")
    await pg_session.rollback()

    assert await _row_hash(pg_session, registry_id) == v1_hash
    assert await _version_count(pg_session, agent_id) == before


# --- Migration 114: existing owned refs are re-pointed to what the version actually served ---


def _backfill_statement(marker: str = "pin-owned-skill-refs", starts: str = "UPDATE SUB_AGENT_CONFIG_VERSIONS") -> str:
    body = MIGRATION.read_text().split(f"-- backfill:{marker}", 1)[1]
    code = "\n".join(line for line in body.splitlines() if not line.startswith("--"))
    statement = code.split(";", 1)[0].strip()
    assert statement.upper().startswith(starts), statement
    return statement


def test_the_migration_markers_are_still_there():
    assert "-- backfill:pin-owned-skill-refs" in MIGRATION.read_text()
    assert "-- backfill:snapshot-current-content" in MIGRATION.read_text()


@pytest.mark.asyncio
async def test_migration_snapshots_current_content_so_a_pinned_version_keeps_it_after_an_edit(
    wired, pg_session, test_user_db, prompt_limit
):
    """A row with no snapshot of its content: after the backfill a pending edit does not leak into the default."""
    svc, registry, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "kb-owner")
    registry_id, v1_hash = await _own_skill(svc, pg_session, test_user_db, agent_id, "x" * 20, inline=True)
    approved = (await svc.get_sub_agent_by_id(pg_session, agent_id)).default_version
    # Pre-snapshot data: the row has no snapshot of its current content.
    await pg_session.execute(
        text("DELETE FROM skill_registry_versions WHERE skill_id = CAST(:id AS uuid)"), {"id": registry_id}
    )
    await pg_session.execute(text(_backfill_statement("snapshot-current-content", "INSERT INTO SKILL_REGISTRY_VERSIONS")))
    await pg_session.commit()

    prompt_limit(50)
    result = await _registry_edit(registry, pg_session, test_user_db, registry_id, "y" * 80)
    await pg_session.commit()

    assert result.owner_version is not None and result.owner_version.approved is False
    running = await _resolved_at(svc, pg_session, agent_id, approved)
    assert running.body == "x" * 20 and running.content_hash == v1_hash


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
