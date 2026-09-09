"""publish_managed_version (ADR-0006) against a real database: one approved default version
per call, content-addressed hash, host content written, Nannos-side settings carried forward
from the approved default."""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.sub_agent import (
    ModelTier,
    SkillDefinition,
    SubAgentCreate,
    SubAgentStatus,
    SubAgentType,
    ThinkingLevel,
)
from console_backend.models.user import User
from console_backend.repositories.skill_registry_repository import (
    SkillRegistryRepository,
)
from console_backend.services.audit_service import AuditService
from console_backend.services.skill_registry_service import SkillRegistryService
from console_backend.services.sub_agent_service import SubAgentService


@pytest.fixture
def managed_sub_agent_service(sub_agent_service: SubAgentService) -> SubAgentService:
    """The shared fixture leaves the skill registry without a repository; sub-agent-scoped
    skills are persisted through it (like service_instances.py wires it), so complete it."""
    registry_repo = SkillRegistryRepository()
    registry_repo.set_audit_service(AuditService())
    registry_service = SkillRegistryService()
    registry_service.set_repository(registry_repo)
    sub_agent_service.set_skill_registry_service(registry_service)
    return sub_agent_service


@pytest.mark.asyncio
async def test_publish_managed_version_writes_approved_default_and_carries_nannos_settings(
    managed_sub_agent_service: SubAgentService,
    pg_session: AsyncSession,
    test_user_db: User,
):
    sub_agent_service = managed_sub_agent_service
    agent = await sub_agent_service.create_sub_agent(
        pg_session,
        SubAgentCreate(
            name="Alloy-AI-Assistant",
            type=SubAgentType.LOCAL,
            description="pasted description",
            model="gpt-4o",
            system_prompt="pasted prompt",
            mcp_tools=["list_campaigns", "get_campaign"],
        ),
        test_user_db,
    )
    assert agent.default_version == 1

    # First sync: the host publishes prompt, description and one skill; no tools, no model.
    version = await sub_agent_service.publish_managed_version(
        pg_session,
        test_user_db,
        agent.id,
        version_hash="wkabc123def4",
        change_summary="well-known revision abc123def4567890 from https://riad.example",
        description="Helps campaign managers.",
        system_prompt="You are Nannos ...\n\nDomain guidance.",
        mcp_tools=None,
        model_tier=None,
        enable_thinking=None,
        thinking_level=None,
        skills=[
            SkillDefinition(
                name="book-line-items",
                description="Use when booking.",
                body="Steps.",
                scope="sub-agent",
            )
        ],
    )
    assert version == 2
    synced = await sub_agent_service.get_sub_agent_by_id(pg_session, agent.id)
    assert synced.default_version == 2 and synced.current_version == 2
    cv = synced.config_version
    assert cv.version == 2 and cv.status == SubAgentStatus.APPROVED
    assert cv.version_hash == "wkabc123def4"
    assert cv.approved_by_user_id == test_user_db.id
    assert cv.description == "Helps campaign managers."
    assert cv.system_prompt.endswith("Domain guidance.")
    assert cv.mcp_tools == [
        "list_campaigns",
        "get_campaign",
    ]  # carried forward: Nannos-side
    assert cv.model == "gpt-4o" and cv.model_tier is None  # carried forward
    await sub_agent_service.resolve_imported_skills(pg_session, synced)
    (skill,) = synced.config_version.skills
    assert (skill.name, skill.body.strip()) == ("book-line-items", "Steps.")

    # Second sync: the host now publishes a tier and thinking; tools stay Nannos-side.
    version = await sub_agent_service.publish_managed_version(
        pg_session,
        test_user_db,
        agent.id,
        version_hash="wk0123456789",
        change_summary="well-known revision 0123456789abcdef",
        description="Helps campaign managers.",
        system_prompt="You are Nannos ...\n\nMore guidance.",
        mcp_tools=None,
        model_tier="premium",
        enable_thinking=True,
        thinking_level=ThinkingLevel.HIGH,
        skills=[],
    )
    assert version == 3
    synced = await sub_agent_service.get_sub_agent_by_id(pg_session, agent.id)
    cv = synced.config_version
    assert synced.default_version == 3 and cv.version_hash == "wk0123456789"
    assert (
        cv.model_tier == ModelTier.PREMIUM and cv.model is None
    )  # tier and explicit model are exclusive
    assert cv.enable_thinking is True and cv.thinking_level == ThinkingLevel.HIGH
    assert cv.mcp_tools == ["list_campaigns", "get_campaign"]
    assert cv.skills == []


@pytest.mark.asyncio
async def test_create_managed_sub_agent_then_publish_writes_version_one(
    managed_sub_agent_service: SubAgentService,
    pg_session: AsyncSession,
    test_user_db: User,
):
    """Create-from-host (ADR-0006): the row starts without a version; the first publish is v1."""
    sub_agent_service = managed_sub_agent_service
    sub_agent_id = await sub_agent_service.create_managed_sub_agent(
        pg_session, test_user_db, name="Alloy AI Assistant"
    )
    bare = await sub_agent_service.get_sub_agent_by_id(pg_session, sub_agent_id)
    assert bare is not None and bare.config_version is None

    version = await sub_agent_service.publish_managed_version(
        pg_session,
        test_user_db,
        sub_agent_id,
        version_hash="wkabc123def4",
        change_summary="well-known revision abc123def4567890 from https://riad.example",
        description="Helps campaign managers.",
        system_prompt="You are Nannos ...",
        mcp_tools=None,
        model_tier="standard",
        enable_thinking=None,
        thinking_level=None,
        skills=[],
    )
    assert version == 1
    created = await sub_agent_service.get_sub_agent_by_id(pg_session, sub_agent_id)
    assert created.name == "Alloy AI Assistant"
    assert created.type == SubAgentType.LOCAL and created.owner_user_id == test_user_db.id
    assert created.is_public is False
    assert created.current_version == 1 and created.default_version == 1
    cv = created.config_version
    assert cv.version == 1 and cv.status == SubAgentStatus.APPROVED
    assert cv.version_hash == "wkabc123def4"
    assert cv.description == "Helps campaign managers."
    assert cv.mcp_tools == [] and cv.model is None  # nothing to carry forward
    assert cv.model_tier == ModelTier.STANDARD
