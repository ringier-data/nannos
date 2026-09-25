"""ADR-0012 against a real database: an inlined skill is a config-version property, and its
body counts toward the auto-approve prompt limit on every path that writes a version.
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.config import config
from console_backend.models.sub_agent import (
    SkillDefinition,
    SubAgentCreate,
    SubAgentType,
    SubAgentUpdate,
)
from console_backend.services.sub_agent_service import PromptLimitError
from tests.test_skill_reference_modes import (
    _agent,
    _default_version,
    _edit_skill,
    _publish_skill,
    _resolved_skill,
)


async def _stored_refs(
    db: AsyncSession, agent_id: int, version: int | None = None
) -> list[dict]:
    """The SkillRefs exactly as persisted in the version's JSONB."""
    return (
        await db.execute(
            text(
                "SELECT cv.skills FROM sub_agents sa JOIN sub_agent_config_versions cv "
                "ON cv.sub_agent_id = sa.id AND cv.version = COALESCE(CAST(:v AS int), sa.current_version) "
                "WHERE sa.id = :id"
            ),
            {"id": agent_id, "v": version},
        )
    ).scalar_one()


@pytest.fixture
def prompt_limit(monkeypatch):
    def set_limit(n: int) -> None:
        monkeypatch.setattr(config.auto_approve, "max_system_prompt_length", n)

    return set_limit


@pytest.mark.asyncio
async def test_inline_is_stored_on_the_skill_ref_and_restored_by_revert(
    wired, pg_session, test_user_db
):
    svc, _, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "inline-owner")
    own = SkillDefinition(
        name="kb", description="knowledge", body="Always do X.", inline=True
    )
    await svc.update_sub_agent(
        pg_session, agent_id, SubAgentUpdate(skills=[own]), test_user_db
    )

    assert [r["inline"] for r in await _stored_refs(pg_session, agent_id)] == [True]
    skill = await _resolved_skill(svc, pg_session, agent_id)
    assert skill.inline is True and skill.body == "Always do X."

    await svc.update_sub_agent(
        pg_session,
        agent_id,
        SubAgentUpdate(skills=[skill.model_copy(update={"inline": False})]),
        test_user_db,
    )
    assert [r["inline"] for r in await _stored_refs(pg_session, agent_id)] == [False]
    # the version that inlined it still says so: a revert restores the choice
    assert [
        r["inline"] for r in await _stored_refs(pg_session, agent_id, version=2)
    ] == [True]


@pytest.mark.asyncio
async def test_activation_writes_inline_into_the_version_and_can_switch_it(
    wired, pg_session, test_user_db
):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    referrer = await _agent(svc, pg_session, test_user_db, "kb-referrer")
    registry_id, _ = await _publish_skill(
        svc, registry, pg_session, test_user_db, publisher, "v1"
    )

    await activation.activate(
        pg_session,
        registry_id,
        referrer,
        "kb-referrer",
        "sub-agent",
        test_user_db.id,
        actor=test_user_db,
        inline=True,
    )
    skill = await _resolved_skill(svc, pg_session, referrer)
    assert skill.inline is True and skill.mode == "pinned"

    before = await _default_version(pg_session, referrer)
    # same value: idempotent, no new version
    await activation.activate(
        pg_session,
        registry_id,
        referrer,
        "kb-referrer",
        "sub-agent",
        test_user_db.id,
        actor=test_user_db,
        inline=True,
    )
    assert (await _default_version(pg_session, referrer))["default_version"] == before[
        "default_version"
    ]

    await activation.activate(
        pg_session,
        registry_id,
        referrer,
        "kb-referrer",
        "sub-agent",
        test_user_db.id,
        actor=test_user_db,
        inline=False,
    )
    after = await _default_version(pg_session, referrer)
    assert after["default_version"] == before["default_version"] + 1
    assert "Stopped inlining skill 'kb'" in after["change_summary"]
    assert (await _resolved_skill(svc, pg_session, referrer)).inline is False


@pytest.mark.asyncio
async def test_inline_is_sub_agent_scope_only(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    registry_id, _ = await _publish_skill(
        svc, registry, pg_session, test_user_db, publisher, "v1"
    )
    with pytest.raises(ValueError, match="sub-agent scope"):
        await activation.activate(
            pg_session,
            registry_id,
            publisher,
            "kb-publisher",
            "personal",
            test_user_db.id,
            inline=True,
        )


@pytest.mark.asyncio
async def test_inlined_bodies_count_toward_auto_approve_on_a_local_agent(
    wired, pg_session, test_user_db, prompt_limit
):
    """Over the limit the version is created but waits for approval, like a long prompt."""
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    referrer = await _agent(svc, pg_session, test_user_db, "kb-referrer")
    other = await _agent(svc, pg_session, test_user_db, "kb-other")
    registry_id, _ = await _publish_skill(
        svc, registry, pg_session, test_user_db, publisher, "x" * 80
    )
    prompt_limit(50)  # "short prompt" (12) fits; + 80 inlined does not

    before = await _default_version(pg_session, referrer)
    await activation.activate(
        pg_session,
        registry_id,
        referrer,
        "kb-referrer",
        "sub-agent",
        test_user_db.id,
        actor=test_user_db,
        inline=True,
    )
    agent = await svc.get_sub_agent_by_id(pg_session, referrer)
    assert agent.default_version == before["default_version"]
    assert agent.current_version == before["default_version"] + 1  # pending, not live

    # the same skill, not inlined, is auto-approved: only inlined text counts
    await activation.activate(
        pg_session,
        registry_id,
        other,
        "kb-other",
        "sub-agent",
        test_user_db.id,
        actor=test_user_db,
    )
    agent = await svc.get_sub_agent_by_id(pg_session, other)
    assert agent.default_version == agent.current_version


@pytest.mark.asyncio
async def test_an_owned_inlined_skill_counts_on_a_save_without_skills(
    wired, pg_session, test_user_db, prompt_limit
):
    """A save that carries no skills measures the stored own skill, not an empty ref."""
    svc, _, _ = wired
    agent_id = await _agent(svc, pg_session, test_user_db, "inline-owner")
    own = SkillDefinition(
        name="kb", description="knowledge", body="x" * 80, inline=True
    )
    await svc.update_sub_agent(
        pg_session, agent_id, SubAgentUpdate(skills=[own]), test_user_db
    )
    prompt_limit(50)

    await svc.update_sub_agent(
        pg_session, agent_id, SubAgentUpdate(description="renamed"), test_user_db
    )
    agent = await svc.get_sub_agent_by_id(pg_session, agent_id)
    assert agent.current_version != agent.default_version


@pytest.mark.asyncio
async def test_a_foreign_skill_counts_with_its_stored_body_not_the_payload(
    wired, pg_session, test_user_db, prompt_limit
):
    """A request body cannot shrink someone else's skill to pass the limit."""
    svc, registry, _ = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    referrer = await _agent(svc, pg_session, test_user_db, "kb-referrer")
    registry_id, v1_hash = await _publish_skill(
        svc, registry, pg_session, test_user_db, publisher, "x" * 80
    )
    prompt_limit(50)

    forged = SkillDefinition(
        name="kb",
        description="knowledge",
        body="tiny",
        registry_id=registry_id,
        content_hash=v1_hash,
        inline=True,
    )
    await svc.update_sub_agent(
        pg_session, referrer, SubAgentUpdate(skills=[forged]), test_user_db
    )
    agent = await svc.get_sub_agent_by_id(pg_session, referrer)
    assert agent.current_version != agent.default_version


@pytest.mark.asyncio
async def test_an_automated_agent_over_the_limit_is_refused(
    wired, pg_session, test_user_db, prompt_limit
):
    svc, _, _ = wired
    prompt_limit(50)
    with pytest.raises(PromptLimitError, match="plus inlined skills"):
        await svc.create_sub_agent(
            pg_session,
            SubAgentCreate(
                name="inline-automated",
                type=SubAgentType.AUTOMATED,
                description="automated",
                model="gpt-4o",
                system_prompt="short prompt",
                mcp_tools=[],
                skills=[
                    SkillDefinition(
                        name="kb", description="knowledge", body="x" * 80, inline=True
                    )
                ],
            ),
            test_user_db,
        )


@pytest.mark.asyncio
async def test_a_bump_past_the_limit_is_a_failed_bump(
    wired, pg_session, test_user_db, prompt_limit
):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    follower = await _agent(svc, pg_session, test_user_db, "kb-follower")
    registry_id, v1_hash = await _publish_skill(
        svc, registry, pg_session, test_user_db, publisher, "short v1"
    )
    prompt_limit(50)
    act_id = await activation.activate(
        pg_session,
        registry_id,
        follower,
        "kb-follower",
        "sub-agent",
        test_user_db.id,
        actor=test_user_db,
        mode="following",
        inline=True,
    )
    before = await _default_version(pg_session, follower)

    await _edit_skill(svc, pg_session, test_user_db, publisher, registry_id, "x" * 80)

    assert (await _default_version(pg_session, follower))["default_version"] == before[
        "default_version"
    ]
    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.content_hash == v1_hash and skill.update_available is True
    err = (
        await pg_session.execute(
            text("SELECT last_bump_error FROM skill_activations WHERE id = :id"),
            {"id": act_id},
        )
    ).scalar_one()
    assert "auto-approve prompt limit" in err
