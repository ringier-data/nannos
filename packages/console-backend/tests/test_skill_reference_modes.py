"""ADR-0011 against a real database: a skill another agent activates is REFERENCED, never
copied; the reference is pinned or following; following bumps an auto-approved version on
every publisher write; a referenced row cannot be withdrawn.
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.sub_agent import (
    SkillDefinition,
    SubAgentCreate,
    SubAgentType,
    SubAgentUpdate,
)
from console_backend.models.user import User
from console_backend.repositories.skill_registry_repository import SkillRegistryRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.skill_activation_service import SkillActivationService
from console_backend.services.skill_registry_service import SkillReferencedError, SkillRegistryService
from console_backend.services.sub_agent_service import SubAgentService
from console_backend.services.user_service import UserService


@pytest.fixture
def registry_service() -> SkillRegistryService:
    repo = SkillRegistryRepository()
    repo.set_audit_service(AuditService())
    service = SkillRegistryService()
    service.set_repository(repo)
    return service


@pytest.fixture
def wired(
    sub_agent_service: SubAgentService,
    registry_service: SkillRegistryService,
    user_service: UserService,
) -> tuple[SubAgentService, SkillRegistryService, SkillActivationService]:
    """The three services wired as service_instances.py wires them, hook included."""
    sub_agent_service.set_skill_registry_service(registry_service)
    activation = SkillActivationService()
    activation.set_sub_agent_service(sub_agent_service)
    activation.set_user_service(user_service)
    registry_service.set_content_changed_hook(activation.bump_following_referrers)
    return sub_agent_service, registry_service, activation


async def _agent(svc: SubAgentService, db: AsyncSession, actor: User, name: str) -> int:
    agent = await svc.create_sub_agent(
        db,
        SubAgentCreate(
            name=name,
            type=SubAgentType.LOCAL,
            description=f"{name} description",
            model="gpt-4o",
            system_prompt="short prompt",
            mcp_tools=[],
        ),
        actor,
    )
    assert agent.default_version == 1
    return agent.id


async def _publish_skill(
    svc: SubAgentService, registry: SkillRegistryService, db: AsyncSession, actor: User, agent_id: int, body: str
) -> tuple[str, str]:
    """Write an own skill on the publisher and make it public. Returns (registry_id, content_hash)."""
    await svc.update_sub_agent(
        db, agent_id, SubAgentUpdate(skills=[SkillDefinition(name="kb", description="knowledge", body=body)]), actor
    )
    agent = await svc.get_sub_agent_by_id(db, agent_id)
    assert agent and agent.config_version
    skill = agent.config_version.skills[0]
    assert skill.registry_id
    await registry.update_visibility(db, actor, skill.registry_id, "public")
    return skill.registry_id, skill.content_hash


async def _edit_skill(svc: SubAgentService, db: AsyncSession, actor: User, agent_id: int, registry_id: str, body: str):
    await svc.update_sub_agent(
        db,
        agent_id,
        SubAgentUpdate(
            skills=[
                SkillDefinition(name="kb", description="knowledge", body=body, registry_id=registry_id, scope="sub-agent")
            ]
        ),
        actor,
    )


async def _resolved_skill(svc: SubAgentService, db: AsyncSession, agent_id: int) -> SkillDefinition:
    """What the console and the orchestrator see: the default version, resolved like the router does."""
    agent = await svc.get_sub_agent_by_id(db, agent_id)
    assert agent and agent.config_version
    await svc.resolve_imported_skills(db, agent)
    return agent.config_version.skills[0]


async def _default_version(db: AsyncSession, agent_id: int) -> dict:
    row = (
        await db.execute(
            text(
                "SELECT sa.default_version, cv.status, cv.approved_by_user_id, cv.change_summary "
                "FROM sub_agents sa JOIN sub_agent_config_versions cv "
                "ON cv.sub_agent_id = sa.id AND cv.version = sa.default_version WHERE sa.id = :id"
            ),
            {"id": agent_id},
        )
    ).mappings().one()
    return dict(row)


@pytest.mark.asyncio
async def test_pinned_reference_is_the_publishers_row_and_signals_updates(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    follower = await _agent(svc, pg_session, test_user_db, "kb-follower")
    registry_id, v1_hash = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")

    await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db
    )

    # No copy: one registry row for the slug, and the follower's config names the publisher's row.
    rows = (await pg_session.execute(text("SELECT count(*) FROM skill_registry WHERE slug = 'kb'"))).scalar_one()
    assert rows == 1
    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.registry_id == registry_id
    assert skill.mode == "pinned"
    assert skill.update_available is False
    assert skill.body == "v1"

    # The publisher edits: the follower stays on v1 and is told an update exists.
    await _edit_skill(svc, pg_session, test_user_db, publisher, registry_id, "v2")
    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.body == "v1"
    assert skill.content_hash == v1_hash
    assert skill.update_available is True
    assert skill.latest_hash and skill.latest_hash != v1_hash

    # The publisher's own view is always-latest with no mode.
    own = await _resolved_skill(svc, pg_session, publisher)
    assert own.body == "v2"
    assert own.mode is None
    assert own.update_available is False


@pytest.mark.asyncio
async def test_following_bumps_an_approved_version_signed_by_system(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    follower = await _agent(svc, pg_session, test_user_db, "kb-follower")
    registry_id, _ = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")

    act_id = await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db, mode="following"
    )
    before = await _default_version(pg_session, follower)

    await _edit_skill(svc, pg_session, test_user_db, publisher, registry_id, "v2")

    after = await _default_version(pg_session, follower)
    assert after["default_version"] == before["default_version"] + 1
    assert after["status"] == "approved"
    assert after["approved_by_user_id"] == "system"
    assert "Followed skill 'kb'" in after["change_summary"]
    assert test_user_db.email in after["change_summary"]

    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.body == "v2"
    assert skill.mode == "following"
    assert skill.update_available is False
    assert skill.bump_error is None

    row = (
        await pg_session.execute(
            text("SELECT content_hash, last_bump_error, last_bump_at FROM skill_activations WHERE id = :id"),
            {"id": act_id},
        )
    ).mappings().one()
    assert row["content_hash"] == skill.content_hash
    assert row["last_bump_error"] is None
    assert row["last_bump_at"] is not None


@pytest.mark.asyncio
async def test_switching_to_following_catches_up_and_back_to_pinned_stops(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    follower = await _agent(svc, pg_session, test_user_db, "kb-follower")
    registry_id, _ = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")
    act_id = await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db
    )
    await _edit_skill(svc, pg_session, test_user_db, publisher, registry_id, "v2")
    assert (await _resolved_skill(svc, pg_session, follower)).body == "v1"

    # Re-activating with the other mode is the switch; following means current.
    same_id = await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db, mode="following"
    )
    assert same_id == act_id
    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.body == "v2" and skill.mode == "following"
    version = await _default_version(pg_session, follower)
    assert version["approved_by_user_id"] == test_user_db.id  # the switch is the user's own action

    # Back to pinned: the next publisher edit no longer moves the follower.
    await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db, mode="pinned"
    )
    await _edit_skill(svc, pg_session, test_user_db, publisher, registry_id, "v3")
    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.body == "v2" and skill.mode == "pinned" and skill.update_available is True


@pytest.mark.asyncio
async def test_withdrawal_is_refused_while_referenced(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    follower = await _agent(svc, pg_session, test_user_db, "kb-follower")
    registry_id, _ = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")
    act_id = await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db
    )

    with pytest.raises(SkillReferencedError) as exc:
        await registry.update_visibility(pg_session, test_user_db, registry_id, "private")
    assert exc.value.referrers == [(follower, "kb-follower")]
    with pytest.raises(SkillReferencedError):
        await registry.remove(pg_session, test_user_db, registry_id)
    with pytest.raises(SkillReferencedError):
        await registry.update_skill(pg_session, test_user_db, registry_id, visibility="private")

    # The publisher itself is never a referrer, and once the follower detaches the row is free.
    await activation.deactivate(pg_session, act_id, "kb-follower", test_user_db.id, sub_agent_id=follower, actor=test_user_db)
    assert await registry.referrers(pg_session, registry_id) == []
    await registry.update_visibility(pg_session, test_user_db, registry_id, "private")


@pytest.mark.asyncio
async def test_a_failing_bump_is_recorded_and_the_publisher_still_commits(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    follower = await _agent(svc, pg_session, test_user_db, "kb-follower")
    registry_id, v1_hash = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")
    act_id = await activation.activate(
        pg_session, registry_id, follower, "kb-follower", "sub-agent", test_user_db.id, actor=test_user_db, mode="following"
    )
    # Bind the follower to a host after the fact: bump_followed_skill now refuses it.
    await pg_session.execute(
        text(
            "INSERT INTO sub_agent_embed_bindings (sub_agent_id, base_url, created_by) "
            "VALUES (:id, 'https://host.example', :user)"
        ),
        {"id": follower, "user": test_user_db.id},
    )

    await _edit_skill(svc, pg_session, test_user_db, publisher, registry_id, "v2")

    own = await _resolved_skill(svc, pg_session, publisher)
    assert own.body == "v2"  # the publisher's write went through
    skill = await _resolved_skill(svc, pg_session, follower)
    assert skill.body == "v1" and skill.content_hash == v1_hash
    assert skill.update_available is True
    assert skill.bump_error and "embed-bound" in skill.bump_error
    row = (
        await pg_session.execute(text("SELECT last_bump_error FROM skill_activations WHERE id = :id"), {"id": act_id})
    ).scalar_one()
    assert "embed-bound" in row


@pytest.mark.asyncio
async def test_following_is_sub_agent_scope_only_and_never_your_own_skill(wired, pg_session, test_user_db):
    svc, registry, activation = wired
    publisher = await _agent(svc, pg_session, test_user_db, "kb-publisher")
    registry_id, _ = await _publish_skill(svc, registry, pg_session, test_user_db, publisher, "v1")

    with pytest.raises(ValueError, match="sub-agent scope"):
        await activation.activate(
            pg_session, registry_id, publisher, "kb-publisher", "personal", test_user_db.id, mode="following"
        )
    with pytest.raises(ValueError, match="own skill"):
        await activation.activate(
            pg_session, registry_id, publisher, "kb-publisher", "sub-agent", test_user_db.id, actor=test_user_db, mode="following"
        )
