"""Unit tests for SubAgentService - versioning, approval workflows, and permissions.

Tests cover:
- Version creation and hash generation
- Version submission for approval (draft → pending_approval)
- Approval/rejection workflows (pending → approved/rejected)
- Release number assignment on approval
- Reversion to previous versions
- Default version management
- Version deletion with constraints
- Version diff comparison
- Permission validation for all operations
"""

import os

# Set up boto3 mock environment before any imports that use boto3
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from aiomoto import mock_aws
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.notification import NotificationType
from playground_backend.models.sub_agent import (
    FoundryScope,
    SubAgent,
    SubAgentCreate,
    SubAgentStatus,
    SubAgentType,
    SubAgentUpdate,
)
from playground_backend.models.user import User
from playground_backend.services.notification_service import NotificationService
from playground_backend.services.secrets_service import SecretsService
from playground_backend.services.sub_agent_service import SubAgentService


async def _create_sub_agent(
    session: AsyncSession,
    user: User,
    name: str,
    subagent_service: SubAgentService,
    system_prompt: str = "Default prompt" * 100,  # Long prompt to ensure auto-approval is not triggered
) -> SubAgent:
    """Create a test sub-agent and return it."""
    data = SubAgentCreate(
        name=name,
        type=SubAgentType.LOCAL,
        description="Test agent",
        model="gpt-4o",
        system_prompt=system_prompt,
        mcp_tools=[],
    )
    return await subagent_service.create_sub_agent(session, data, user)


class TestSubAgentVersionCreation:
    """Test version creation and hash generation."""

    @pytest.mark.asyncio
    async def test_create_sub_agent_creates_version_1(
        self,
        sub_agent_service: SubAgentService,
        pg_session: AsyncSession,
        test_user_db: User,
    ):
        """Test that creating a sub-agent creates version 1."""
        service = sub_agent_service
        user = test_user_db

        data = SubAgentCreate(
            name="Test Agent",
            type=SubAgentType.LOCAL,
            description="Test description",
            model="gpt-4o",
            system_prompt="You are a helpful assistant",
            mcp_tools=["tool1", "tool2"],
        )

        agent = await service.create_sub_agent(pg_session, data, user)

        assert agent is not None
        assert agent.name == "Test Agent"
        assert agent.current_version == 1
        assert agent.default_version == 1  # Auto-approved since < 500 chars and < 4 tools
        assert agent.config_version is not None
        assert agent.config_version.version == 1
        assert agent.config_version.status == SubAgentStatus.APPROVED
        assert agent.config_version.change_summary == "Initial version"
        assert agent.config_version.version_hash is not None
        assert len(agent.config_version.version_hash) == 12  # 12-char hash

    @pytest.mark.asyncio
    async def test_update_sub_agent_creates_new_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that updating configuration creates a new version."""
        service = sub_agent_service
        agent = await _create_sub_agent(pg_session, test_user_db, "Agent", sub_agent_service)

        # Update configuration
        data = SubAgentUpdate(
            description="Updated description",
            system_prompt="New system prompt" * 100,  # Long enough to prevent auto-approval
            change_summary="Updated system prompt",
        )

        updated = await service.update_sub_agent(pg_session, agent.id, data, actor=test_user_db)

        assert updated is not None
        assert updated.current_version == 2
        assert updated.config_version is not None
        assert updated.config_version.version == 2
        assert updated.config_version.system_prompt == "New system prompt" * 100  # Matches the updated value
        assert updated.config_version.change_summary == "Updated system prompt"
        assert updated.config_version.status == SubAgentStatus.DRAFT

    @pytest.mark.asyncio
    async def test_update_metadata_and_config_creates_new_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that updating metadata (name, is_public) along with config creates a new version."""
        service = sub_agent_service
        agent = await _create_sub_agent(pg_session, test_user_db, "Original Name", sub_agent_service)

        # Verify initial state
        assert agent.name == "Original Name"
        assert agent.is_public is False
        assert agent.current_version == 1

        # Update metadata fields along with description (which triggers version)
        data = SubAgentUpdate(
            name="Updated Name",
            is_public=True,
            description="Updated description",
        )

        updated = await service.update_sub_agent(pg_session, agent.id, data, actor=test_user_db)

        # Verify metadata was updated
        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.is_public is True

        # A new version was created due to description change
        assert updated.current_version == 2
        assert updated.config_version is not None
        assert updated.config_version.version == 2
        assert updated.config_version.description == "Updated description"

        # Verify the updates persisted by fetching again
        refetched = await service.get_sub_agent_by_id(pg_session, agent.id)
        assert refetched is not None
        assert refetched.name == "Updated Name"
        assert refetched.is_public is True
        assert refetched.current_version == 2

    @pytest.mark.asyncio
    async def test_update_is_public_persists_correctly(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that is_public field is properly persisted and returned after update."""
        service = sub_agent_service
        agent = await _create_sub_agent(pg_session, test_user_db, "Test Agent", sub_agent_service)

        # Initially not public
        assert agent.is_public is False

        # Set to public
        data = SubAgentUpdate(is_public=True, description="")
        updated = await service.update_sub_agent(pg_session, agent.id, data, actor=test_user_db)

        assert updated is not None
        assert updated.is_public is True

        # Verify by fetching from database
        refetched = await service.get_sub_agent_by_id(pg_session, agent.id)
        assert refetched is not None
        assert refetched.is_public is True

        # Set back to private
        data = SubAgentUpdate(is_public=False, description="")
        updated = await service.update_sub_agent(pg_session, agent.id, data, actor=test_user_db)

        assert updated is not None
        assert updated.is_public is False

        # Verify again
        refetched = await service.get_sub_agent_by_id(pg_session, agent.id)
        assert refetched is not None
        assert refetched.is_public is False

    @pytest.mark.asyncio
    async def test_version_hash_is_unique_for_different_content(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that different configurations generate different hashes."""
        service = sub_agent_service
        agent = await _create_sub_agent(pg_session, test_user_db, "Agent", sub_agent_service)

        assert agent.config_version is not None
        hash1 = agent.config_version.version_hash

        # Update to create version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(description="", system_prompt="Different prompt"),
            actor=test_user_db,
        )

        updated = await service.get_sub_agent_by_id(pg_session, agent.id)
        assert updated is not None
        assert updated.config_version is not None
        hash2 = updated.config_version.version_hash

        assert hash1 != hash2  # Different content should produce different hashes

    @pytest.mark.asyncio
    async def test_local_agent_has_system_prompt_not_agent_url(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that local agents use system_prompt, not agent_url."""
        service = sub_agent_service

        data = SubAgentCreate(
            name="Local Agent",
            description="Local agent description",
            type=SubAgentType.LOCAL,
            system_prompt="Local prompt",
            model="gpt-4o",
        )

        agent = await service.create_sub_agent(pg_session, data, test_user_db)

        assert agent is not None
        assert agent.config_version is not None
        assert agent.config_version.system_prompt == "Local prompt"
        assert agent.config_version.agent_url is None

    @pytest.mark.asyncio
    async def test_remote_agent_has_agent_url_not_system_prompt(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that remote agents use agent_url, not system_prompt."""
        service = sub_agent_service
        data = SubAgentCreate(
            name="Remote Agent",
            description="Remote agent description",
            type=SubAgentType.REMOTE,
            agent_url="https://example.com/agent",
        )

        agent = await service.create_sub_agent(pg_session, data, test_user_db)

        assert agent.config_version is not None
        assert agent.config_version.agent_url == "https://example.com/agent"
        assert agent.config_version.system_prompt is None

    @mock_aws
    @pytest.mark.asyncio
    async def test_foundry_agent_creates_secret_in_ssm(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        secrets_service: SecretsService,
        test_user_db: User,
    ):
        """Test that creating a Foundry agent stores the client_secret in SSM."""
        from playground_backend.models.secret import SecretCreate, SecretType

        service = sub_agent_service

        # Create the secret first
        secret = await secrets_service.create_secret(
            pg_session,
            data=SecretCreate(
                name="test-foundry-secret",
                description="Test secret for Foundry agent",
                secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
                secret_value="test-secret-value",
            ),
            actor=test_user_db,
        )
        assert secret is not None

        data = SubAgentCreate(
            name="Foundry Agent",
            description="Foundry agent description",
            type=SubAgentType.FOUNDRY,
            foundry_hostname="https://blumen.palantirfoundry.de",
            foundry_client_id="test-client-id",
            foundry_client_secret_ref=secret.id,
            foundry_ontology_rid="ri.ontology.main.ontology.abc123",
            foundry_query_api_name="a2ATicketWriterAgent",
            foundry_scopes=[FoundryScope.ONTOLOGIES_WRITE, FoundryScope.ONTOLOGIES_READ],
        )

        agent = await service.create_sub_agent(pg_session, data, test_user_db)

        assert agent is not None
        assert agent.type == SubAgentType.FOUNDRY
        assert agent.config_version is not None
        assert agent.config_version.foundry_hostname == "https://blumen.palantirfoundry.de"
        assert agent.config_version.foundry_client_id == "test-client-id"
        assert agent.config_version.foundry_client_secret_ref == secret.id  # Secret ID stored
        assert agent.config_version.foundry_ontology_rid == "ri.ontology.main.ontology.abc123"
        assert agent.config_version.foundry_query_api_name == "a2ATicketWriterAgent"
        assert agent.config_version.foundry_scopes == [
            "api:use-ontologies-write",
            "api:use-ontologies-read",
        ]  # Stored as strings in DB

        # Verify system_prompt and agent_url are None for Foundry agents
        assert agent.config_version.system_prompt is None
        assert agent.config_version.agent_url is None

    @mock_aws
    @pytest.mark.asyncio
    async def test_foundry_agent_update_creates_new_secret(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        secrets_service: SecretsService,
        test_user_db: User,
    ):
        """Test that updating a Foundry agent's client_secret creates a new secret."""
        from playground_backend.models.secret import SecretCreate, SecretType

        service = sub_agent_service
        user = test_user_db

        # Create initial secret
        secret1 = await secrets_service.create_secret(
            pg_session,
            data=SecretCreate(
                name="original-foundry-secret",
                description="Original secret",
                secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
                secret_value="original-secret",
            ),
            actor=user,
        )
        assert secret1 is not None

        # Create initial Foundry agent
        data = SubAgentCreate(
            name="Foundry Agent",
            description="Foundry agent description",
            type=SubAgentType.FOUNDRY,
            foundry_hostname="https://blumen.palantirfoundry.de",
            foundry_client_id="test-client-id",
            foundry_client_secret_ref=secret1.id,
            foundry_ontology_rid="ri.ontology.main.ontology.abc123",
            foundry_query_api_name="a2ATicketWriterAgent",
            foundry_scopes=[FoundryScope.ONTOLOGIES_WRITE],
        )

        agent = await service.create_sub_agent(pg_session, data, user)
        assert agent.config_version is not None
        original_secret_ref = agent.config_version.foundry_client_secret_ref

        # Create new secret
        secret2 = await secrets_service.create_secret(
            pg_session,
            data=SecretCreate(
                name="new-foundry-secret",
                description="New secret",
                secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
                secret_value="new-secret-value",
            ),
            actor=user,
        )
        assert secret2 is not None

        # Update with new client_secret reference
        update_data = SubAgentUpdate(
            description="Updated description",
            foundry_client_secret_ref=secret2.id,
            change_summary="Updated client secret",
        )

        updated = await service.update_sub_agent(pg_session, agent.id, update_data, actor=user)

        assert updated is not None
        assert updated.current_version == 2
        assert updated.config_version is not None
        assert updated.config_version.foundry_client_secret_ref != original_secret_ref  # New secret
        assert updated.config_version.foundry_client_id == "test-client-id"  # Inherited
        assert updated.config_version.foundry_hostname == "https://blumen.palantirfoundry.de"  # Inherited

    @mock_aws
    @pytest.mark.asyncio
    async def test_foundry_agent_update_without_secret_keeps_existing(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        secrets_service: SecretsService,
        test_user_db: User,
    ):
        """Test that updating a Foundry agent without providing client_secret keeps the existing one."""
        from playground_backend.models.secret import SecretCreate, SecretType

        service = sub_agent_service
        user = test_user_db

        # Create secret
        secret = await secrets_service.create_secret(
            pg_session,
            data=SecretCreate(
                name="foundry-secret",
                description="Foundry secret",
                secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
                secret_value="original-secret",
            ),
            actor=user,
        )
        assert secret is not None

        # Create initial Foundry agent
        data = SubAgentCreate(
            name="Foundry Agent",
            description="Foundry agent description",
            type=SubAgentType.FOUNDRY,
            foundry_hostname="https://blumen.palantirfoundry.de",
            foundry_client_id="test-client-id",
            foundry_client_secret_ref=secret.id,
            foundry_ontology_rid="ri.ontology.main.ontology.abc123",
            foundry_query_api_name="a2ATicketWriterAgent",
            foundry_scopes=[FoundryScope.ONTOLOGIES_WRITE],
        )

        agent = await service.create_sub_agent(pg_session, data, user)
        assert agent.config_version is not None
        original_secret_ref = agent.config_version.foundry_client_secret_ref

        # Update without providing client_secret
        update_data = SubAgentUpdate(
            description="Updated description",
            foundry_query_api_name="newQueryAPI",
            change_summary="Updated query API",
        )

        updated = await service.update_sub_agent(pg_session, agent.id, update_data, actor=user)

        assert updated is not None
        assert updated.current_version == 2
        assert updated.config_version is not None
        assert updated.config_version.foundry_client_secret_ref == original_secret_ref  # Same secret
        assert updated.config_version.foundry_query_api_name == "newQueryAPI"  # Updated

    @mock_aws
    @pytest.mark.asyncio
    async def test_foundry_agent_requires_foundry_fields(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        secrets_service: SecretsService,
        test_user_db: User,
    ):
        """Test that Foundry agents require all Foundry-specific fields including client_secret."""
        from playground_backend.models.secret import SecretCreate, SecretType

        service = sub_agent_service
        user = test_user_db

        # Create secret
        secret = await secrets_service.create_secret(
            pg_session,
            data=SecretCreate(
                name="required-foundry-secret",
                description="Required secret",
                secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
                secret_value="required-secret",
            ),
            actor=user,
        )
        assert secret is not None

        # Create Foundry agent WITH client_secret (required by DB constraint)
        data = SubAgentCreate(
            name="Foundry Agent",
            description="Foundry agent description",
            type=SubAgentType.FOUNDRY,
            foundry_hostname="https://blumen.palantirfoundry.de",
            foundry_client_id="test-client-id",
            foundry_client_secret_ref=secret.id,  # Required for Foundry agents
            foundry_ontology_rid="ri.ontology.main.ontology.abc123",
            foundry_query_api_name="a2ATicketWriterAgent",
            foundry_scopes=[FoundryScope.ONTOLOGIES_WRITE],
            foundry_version="1.0.0",
        )

        agent = await service.create_sub_agent(pg_session, data, user)
        assert agent is not None
        assert agent.config_version is not None
        # Foundry agents must have a secret reference (enforced by DB constraint)
        assert agent.config_version.foundry_client_secret_ref == secret.id
        assert agent.config_version.foundry_hostname == "https://blumen.palantirfoundry.de"
        assert agent.config_version.foundry_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_version_hash_with_different_field_combinations(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that version hash changes when different fields are modified."""
        service = sub_agent_service
        user = test_user_db

        # Create base agent
        data1 = SubAgentCreate(
            name="Agent",
            type=SubAgentType.LOCAL,
            description="Desc",
            model="gpt-4o",
            system_prompt="Prompt A",
            mcp_tools=["tool1"],
        )
        agent1 = await service.create_sub_agent(pg_session, data1, actor=user)
        assert agent1.config_version is not None
        hash1 = agent1.config_version.version_hash

        # Modify system_prompt
        update = SubAgentUpdate(description="Desc", system_prompt="Prompt B", change_summary="Changed prompt")
        agent2 = await service.update_sub_agent(pg_session, agent1.id, update, actor=user)
        assert agent2 is not None
        assert agent2.config_version is not None
        hash2 = agent2.config_version.version_hash

        # Modify model
        update = SubAgentUpdate(description="Desc", model="gpt-4o-mini", change_summary="Changed model")
        agent3 = await service.update_sub_agent(pg_session, agent2.id, update, actor=user)
        assert agent3 is not None
        assert agent3.config_version is not None
        hash3 = agent3.config_version.version_hash

        # Modify mcp_tools
        update = SubAgentUpdate(description="Desc", mcp_tools=["tool1", "tool2"], change_summary="Added tool")
        agent4 = await service.update_sub_agent(pg_session, agent3.id, update, actor=user)
        assert agent4 is not None
        assert agent4.config_version is not None
        hash4 = agent4.config_version.version_hash

        # All hashes should be different
        assert hash1 != hash2
        assert hash2 != hash3
        assert hash3 != hash4
        assert hash1 != hash4

    @pytest.mark.asyncio
    async def test_mcp_tools_none_inheritance(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that mcp_tools=None is stored as empty list (uses orchestrator defaults)."""
        service = sub_agent_service
        user = test_user_db

        # Create agent with mcp_tools=None (stored as empty list)
        data = SubAgentCreate(
            name="Agent",
            type=SubAgentType.LOCAL,
            description="Agent with default tools",
            model="gpt-4o",
            system_prompt="Prompt",
            mcp_tools=None,  # Use orchestrator defaults
        )
        agent = await service.create_sub_agent(pg_session, data, user)

        # None is converted to empty list in database
        assert agent is not None
        assert agent.config_version is not None
        assert agent.config_version.mcp_tools == []

        # Update to explicit tools
        update = SubAgentUpdate(
            description="Updated desc", mcp_tools=["custom_tool"], change_summary="Added custom tools"
        )
        agent = await service.update_sub_agent(pg_session, agent.id, update, actor=user)
        assert agent is not None
        assert agent.config_version is not None
        assert agent.config_version.mcp_tools == ["custom_tool"]

        # Update back to None/empty list (orchestrator defaults)
        update = SubAgentUpdate(description="Updated desc", mcp_tools=[], change_summary="Back to defaults")
        agent = await service.update_sub_agent(pg_session, agent.id, update, actor=user)
        assert agent is not None
        assert agent.config_version is not None
        assert agent.config_version.mcp_tools == []

    @pytest.mark.asyncio
    async def test_soft_delete_does_not_affect_versions(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that soft-deleting a sub-agent preserves versions."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Create a second version
        update = SubAgentUpdate(description="Updated desc", system_prompt="Updated", change_summary="Updated version 2")
        await service.update_sub_agent(pg_session, agent.id, update, actor=user)

        # Soft delete
        await service.delete_sub_agent(pg_session, agent.id, actor=user)

        # Versions should still be accessible (including deleted)
        versions = await service.get_config_versions(pg_session, agent.id, include_deleted=True)
        assert len(versions) == 2
        assert versions[0].version == 2
        assert versions[1].version == 1


class TestVersionSubmissionWorkflow:
    """Test version submission for approval workflow."""

    @pytest.mark.asyncio
    async def test_submit_draft_version_for_approval(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test submitting a draft version for approval."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit for approval
        result = await service.submit_for_approval(
            pg_session,
            agent.id,
            "Ready for review",
            actor=user,
        )

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.PENDING_APPROVAL
        assert result.config_version.change_summary == "Ready for review"

    @pytest.mark.asyncio
    async def test_submit_rejected_version_for_approval(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test submitting a rejected version for approval again."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit and reject
        await service.submit_for_approval(pg_session, agent.id, "First submission", actor=user)
        await service.approve_version(
            pg_session,
            agent.id,
            1,
            False,
            actor=admin,
            rejection_reason="Needs improvement",
        )

        # Resubmit
        result = await service.submit_version_for_approval(
            pg_session,
            agent.id,
            1,
            "Fixed issues",
            actor=user,
        )

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.PENDING_APPROVAL
        assert result.config_version.rejection_reason is None

    @pytest.mark.asyncio
    async def test_cannot_submit_approved_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test that approved versions cannot be resubmitted."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit and approve
        await service.submit_for_approval(pg_session, agent.id, "Submit", actor=user)
        await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Try to resubmit
        with pytest.raises(ValueError, match="Only draft or rejected versions can be submitted"):
            await service.submit_version_for_approval(
                pg_session,
                agent.id,
                1,
                "Try again",
                actor=user,
            )

    @pytest.mark.asyncio
    async def test_cannot_submit_pending_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that pending versions cannot be resubmitted."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "First submission", actor=user)

        # Try to resubmit
        with pytest.raises(ValueError, match="Only draft or rejected versions can be submitted"):
            await service.submit_for_approval(
                pg_session,
                agent.id,
                "Try again",
                actor=user,
            )

    @pytest.mark.asyncio
    async def test_non_owner_cannot_submit_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_approver_user: User
    ):
        """Test that non-owners without write permissions cannot submit versions for approval."""
        service = sub_agent_service
        owner = test_user_db
        other = test_approver_user
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Try to submit as non-owner without write permission
        with pytest.raises(PermissionError, match="don't have permission to submit"):
            await service.submit_for_approval(
                pg_session,
                agent.id,
                "Unauthorized submission",
                actor=other,
            )

    @pytest.mark.asyncio
    async def test_change_summary_required_for_submission(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that change_summary is stored when submitting."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        summary = "Added new features and fixed bugs"
        result = await service.submit_for_approval(pg_session, agent.id, summary, actor=user)

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.change_summary == summary


class TestVersionApprovalWorkflow:
    """Test version approval and rejection workflows."""

    @pytest.mark.asyncio
    async def test_approve_pending_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test approving a pending version."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit and approve
        await service.submit_for_approval(pg_session, agent.id, "Ready", actor=user)
        result = await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.APPROVED
        assert result.config_version.approved_by_user_id == admin.id
        assert result.config_version.approved_at is not None
        assert result.config_version.release_number == 1
        assert result.default_version == 1  # Set as default

    @pytest.mark.asyncio
    async def test_approve_pending_version_for_member(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that a regular member even though group admin and owner of the sub-agent can't approve a version"""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready", actor=user)

        # Non-admin user should not be able to approve (security validation at service level)
        with pytest.raises(PermissionError, match="Approval requires 'approve' or 'approve.admin' capability"):
            await service.approve_version(pg_session, agent.id, 1, True, actor=user)

    @pytest.mark.asyncio
    async def test_reject_pending_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test rejecting a pending version."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit and reject
        await service.submit_for_approval(pg_session, agent.id, "Ready", actor=user)
        result = await service.approve_version(
            pg_session,
            agent.id,
            1,
            False,
            actor=admin,
            rejection_reason="Needs improvement",
        )

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.REJECTED
        assert result.config_version.rejection_reason == "Needs improvement"
        assert result.config_version.approved_by_user_id == admin.id
        assert result.default_version is None  # Not set as default

    @pytest.mark.asyncio
    async def test_release_number_increments_per_sub_agent(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test that release numbers increment for each sub-agent."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Approve version 1
        await service.submit_for_approval(pg_session, agent.id, "V1", actor=user)
        v1 = await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Create and approve version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(system_prompt="V2 prompt" * 100, description="Version 2"),  # Long enough to prevent auto-approval
            actor=user,
        )
        await service.submit_for_approval(pg_session, agent.id, "V2", actor=user)
        v2 = await service.approve_version(pg_session, agent.id, 2, True, actor=admin)

        assert v1 is not None
        assert v1.config_version is not None
        assert v1.config_version.release_number == 1
        assert v2 is not None
        assert v2.config_version is not None
        assert v2.config_version.release_number == 2

    @pytest.mark.asyncio
    async def test_cannot_approve_draft_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test that draft versions cannot be approved directly."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Try to approve without submitting
        with pytest.raises(ValueError, match="Only pending versions can be approved/rejected"):
            await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

    @pytest.mark.asyncio
    async def test_cannot_approve_already_approved_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test that approved versions cannot be approved again."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit and approve
        await service.submit_for_approval(pg_session, agent.id, "Ready", actor=user)
        await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Try to approve again
        with pytest.raises(ValueError, match="Only pending versions can be approved/rejected"):
            await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

    @pytest.mark.asyncio
    async def test_approval_sets_default_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test that approving a version sets it as default."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Before approval
        assert agent.default_version is None

        # Submit and approve
        await service.submit_for_approval(pg_session, agent.id, "Ready", actor=user)
        result = await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # After approval
        assert result is not None
        assert result.default_version == 1


class TestVersionReversion:
    """Test reverting to previous versions."""

    @pytest.mark.asyncio
    async def test_revert_to_previous_version_creates_new_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that reverting creates a new version with old config."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service, "V1 prompt")

        # Create version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(description="", system_prompt="V2 prompt", change_summary="Update to V2"),
            actor=user,
        )

        # Revert to version 1
        result = await service.revert_to_version(pg_session, agent.id, 1, actor=user)

        assert result is not None
        assert result.current_version == 3  # New version created
        assert result.config_version is not None
        assert result.config_version.version == 3
        assert result.config_version.system_prompt == "V1 prompt"  # Same as V1
        assert result.config_version.change_summary == "Reverted to version 1"
        assert result.config_version.status == SubAgentStatus.DRAFT

    @pytest.mark.asyncio
    async def test_revert_copies_all_configuration(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that reversion copies all configuration fields."""
        service = sub_agent_service
        user = test_user_db

        data = SubAgentCreate(
            name="Agent",
            type=SubAgentType.LOCAL,
            description="V1 description",
            model="gpt-4o",
            system_prompt="V1 prompt",
            mcp_tools=["tool1", "tool2"],
        )
        agent = await service.create_sub_agent(pg_session, data, user)

        # Update to version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(
                description="V2 description",
                system_prompt="V2 prompt",
                mcp_tools=["tool3"],
            ),
            actor=user,
        )

        # Revert to version 1
        result = await service.revert_to_version(pg_session, agent.id, 1, actor=user)

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.description == "V1 description"
        assert result.config_version.system_prompt == "V1 prompt"
        assert result.config_version.mcp_tools == ["tool1", "tool2"]
        assert result.config_version.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_non_owner_cannot_revert_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_approver_user: User
    ):
        """Test that non-owners cannot revert versions."""
        service = sub_agent_service

        owner = test_user_db
        other = test_approver_user
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Try to revert as non-owner
        with pytest.raises(PermissionError, match="Only the owner can revert"):
            await service.revert_to_version(pg_session, agent.id, 1, actor=other)

    @pytest.mark.asyncio
    async def test_cannot_revert_to_nonexistent_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that reverting to a non-existent version fails."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Try to revert to non-existent version
        with pytest.raises(ValueError, match="Version 999 not found"):
            await service.revert_to_version(pg_session, agent.id, 999, actor=user)


class TestDefaultVersionManagement:
    """Test setting and managing default versions."""

    @pytest.mark.asyncio
    async def test_set_approved_version_as_default(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test setting an approved version as default."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Approve version 1
        await service.submit_for_approval(pg_session, agent.id, "V1", actor=user)
        await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Create and approve version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(system_prompt="V2 prompt" * 100, description="Version 2"),  # Long enough to prevent auto-approval
            actor=user,
        )
        await service.submit_for_approval(pg_session, agent.id, "V2", actor=user)
        await service.approve_version(pg_session, agent.id, 2, True, actor=admin)

        # Set version 1 back as default
        result = await service.set_default_version(pg_session, agent.id, 1, actor=user)

        assert result is not None
        assert result.default_version == 1
        assert result.current_version == 2  # Current version unchanged

    @pytest.mark.asyncio
    async def test_cannot_set_draft_version_as_default(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that draft versions cannot be set as default."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Try to set draft version as default
        with pytest.raises(ValueError, match="Only approved versions can be set as default"):
            await service.set_default_version(pg_session, agent.id, 1, actor=user)

    @pytest.mark.asyncio
    async def test_cannot_set_pending_version_as_default(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that pending versions cannot be set as default."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit but don't approve
        await service.submit_for_approval(pg_session, agent.id, "Submit", actor=user)

        # Try to set pending version as default
        with pytest.raises(ValueError, match="Only approved versions can be set as default"):
            await service.set_default_version(pg_session, agent.id, 1, actor=user)

    @pytest.mark.asyncio
    async def test_cannot_set_rejected_version_as_default(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test that rejected versions cannot be set as default."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit and reject
        await service.submit_for_approval(pg_session, agent.id, "Submit", actor=user)
        await service.approve_version(pg_session, agent.id, 1, False, actor=admin, rejection_reason="Rejected")

        # Try to set rejected version as default
        with pytest.raises(ValueError, match="Only approved versions can be set as default"):
            await service.set_default_version(pg_session, agent.id, 1, actor=user)

    @pytest.mark.asyncio
    async def test_non_owner_cannot_set_default_version(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        test_admin_user_db: User,
        test_user_db: User,
        test_approver_user: User,
    ):
        """Test that non-owners cannot set default version."""
        service = sub_agent_service
        owner = test_user_db
        other = test_approver_user
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Approve version 1
        await service.submit_for_approval(pg_session, agent.id, "V1", actor=owner)
        await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Try to set default as non-owner
        with pytest.raises(PermissionError, match="Only the owner can set the default version"):
            await service.set_default_version(pg_session, agent.id, 1, actor=other)


class TestVersionDeletion:
    """Test version deletion with constraints."""

    @pytest.mark.asyncio
    async def test_delete_draft_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test deleting a draft version."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Create version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(system_prompt="V2 prompt" * 100, description="Version 2"),  # Long enough to prevent auto-approval
            actor=user,
        )

        # Delete version 2
        result = await service.delete_version(pg_session, agent.id, 2, actor=user)

        assert result is True

        # Verify version is soft-deleted
        versions = await service.get_config_versions(pg_session, agent.id, include_deleted=True)
        v2 = next(v for v in versions if v.version == 2)
        assert v2.deleted_at is not None

    @pytest.mark.asyncio
    async def test_delete_pending_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test deleting a pending version."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Submit", actor=user)

        # Delete pending version (should fail as it's current version and only version)
        with pytest.raises(ValueError, match="Cannot delete the only version"):
            await service.delete_version(pg_session, agent.id, 1, actor=user)

    @pytest.mark.asyncio
    async def test_cannot_delete_approved_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test that approved versions cannot be deleted."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Approve version 1
        await service.submit_for_approval(pg_session, agent.id, "V1", actor=user)
        await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Try to delete approved version
        with pytest.raises(ValueError, match="Cannot delete approved versions"):
            await service.delete_version(pg_session, agent.id, 1, actor=user)

    @pytest.mark.asyncio
    async def test_delete_current_version_updates_to_previous(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that deleting current version updates pointer to previous version."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Create version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(system_prompt="V2 prompt" * 100, description="Version 2"),  # Long enough to prevent auto-approval
            actor=user,
        )

        # Delete current version (version 2)
        await service.delete_version(pg_session, agent.id, 2, actor=user)

        # Verify current version updated to version 1
        updated = await service.get_sub_agent_by_id(pg_session, agent.id)
        assert updated is not None
        assert updated.current_version == 1

    @pytest.mark.asyncio
    async def test_cannot_delete_only_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that the only version cannot be deleted."""
        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Try to delete the only version
        with pytest.raises(ValueError, match="Cannot delete the only version"):
            await service.delete_version(pg_session, agent.id, 1, actor=user)

    @pytest.mark.asyncio
    async def test_non_owner_cannot_delete_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_approver_user: User
    ):
        """Test that non-owners cannot delete versions."""
        service = sub_agent_service
        owner = test_user_db
        other = test_approver_user
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Create version 2
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(description="", system_prompt="V2"),
            actor=owner,
        )

        # Try to delete as non-owner
        with pytest.raises(PermissionError, match="You don't have permission to delete this version"):
            await service.delete_version(pg_session, agent.id, 2, actor=other)

    @pytest.mark.asyncio
    async def test_delete_rejected_version(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test deleting a rejected version."""
        service = sub_agent_service
        user = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Create version 2, submit and reject it
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(description="", system_prompt="V2 prompt" * 100),  # Long enough to prevent auto-approval
            actor=user,
        )
        await service.submit_version_for_approval(pg_session, agent.id, 2, "Submit V2", actor=user)
        await service.approve_version(pg_session, agent.id, 2, False, actor=admin, rejection_reason="Rejected")

        # Delete rejected version
        result = await service.delete_version(pg_session, agent.id, 2, actor=user)

        assert result is True


class TestPermissionValidation:
    """Test permission checks across all operations."""

    @pytest.mark.asyncio
    async def test_non_owner_cannot_update_sub_agent(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_approver_user: User
    ):
        """Test that non-owners cannot update sub-agents."""
        service = sub_agent_service
        owner = test_user_db
        other = test_approver_user
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Try to update as non-owner
        with pytest.raises(PermissionError, match="You don't have permission to update this sub-agent"):
            await service.update_sub_agent(
                pg_session,
                agent.id,
                SubAgentUpdate(name="New Name", description=""),
                actor=other,
            )

    @pytest.mark.asyncio
    async def test_owner_can_perform_all_owner_operations(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_admin_user_db: User, test_user_db: User
    ):
        """Test that owner can submit, revert, and set default version."""
        service = sub_agent_service
        owner = test_user_db
        admin = test_admin_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", service)

        # Owner can submit
        await service.submit_for_approval(pg_session, agent.id, "Submit", actor=owner)
        await service.approve_version(pg_session, agent.id, 1, True, actor=admin)

        # Owner can create new version
        await service.update_sub_agent(
            pg_session,
            agent.id,
            SubAgentUpdate(description="", system_prompt="V2"),
            actor=owner,
        )

        # Owner can revert
        await service.revert_to_version(pg_session, agent.id, 1, actor=owner)

        # Owner can set default (need to approve V3 first)
        await service.submit_for_approval(pg_session, agent.id, "V3", actor=owner)
        await service.approve_version(pg_session, agent.id, 3, True, actor=admin)
        await service.set_default_version(pg_session, agent.id, 1, actor=owner)

        # All operations succeeded
        final = await service.get_sub_agent_by_id(pg_session, agent.id)
        assert final is not None
        assert final.default_version == 1

    @pytest.mark.asyncio
    async def test_approver_role_can_approve_with_group_access(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        test_user_db: User,
        test_approver_user_db: User,
    ):
        """Test that users with approver role can approve sub-agents when they have group-based write access."""
        from sqlalchemy import text

        service = sub_agent_service
        owner = test_user_db
        approver = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Create a group and add approver to it
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        # Add approver to the group
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": approver.id},
        )

        # Grant group read permission on the sub-agent
        await pg_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions, created_at)
                VALUES (:sub_agent_id, :group_id, ARRAY['read'], NOW())
            """),
            {"sub_agent_id": agent.id, "group_id": group_id},
        )
        await pg_session.commit()

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready for review", actor=owner)

        # Approver can approve (has 'approve' capability and group access to resource)
        result = await service.approve_version(pg_session, agent.id, 1, True, actor=approver)

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.APPROVED
        assert result.config_version.approved_by_user_id == approver.id

    @pytest.mark.asyncio
    async def test_approver_role_cannot_approve_without_ownership_or_group_access(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        test_user_db: User,
        test_approver_user_db: User,
    ):
        """Test that approvers cannot approve sub-agents they don't own and don't have group access to."""
        service = sub_agent_service
        owner = test_user_db
        approver = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready for review", actor=owner)

        # Approver cannot approve without ownership or group-based access (defense-in-depth validation)
        with pytest.raises(PermissionError, match="requires group-based access"):
            await service.approve_version(pg_session, agent.id, 1, True, actor=approver)

    @pytest.mark.asyncio
    async def test_approver_owner_can_approve_own_sub_agent(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_approver_user_db: User
    ):
        """Test that approvers who own a sub-agent can approve it without group membership."""
        service = sub_agent_service
        agent = await _create_sub_agent(pg_session, test_approver_user_db, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready for review", actor=test_approver_user_db)
        # Owner with approver role can approve their own sub-agent
        result = await service.approve_version(pg_session, agent.id, 1, True, actor=test_approver_user_db)

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.APPROVED
        assert result.config_version.approved_by_user_id == test_approver_user_db.id

    @pytest.mark.asyncio
    async def test_admin_role_can_approve(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User, test_admin_user_db: User
    ):
        """Test that users with admin role (not is_administrator) can approve sub-agents."""
        service = sub_agent_service
        owner = test_user_db
        admin_role_user = test_admin_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready for review", actor=owner)

        # Admin role user can approve (has approve capability)
        result = await service.approve_version(pg_session, agent.id, 1, True, actor=admin_role_user)

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.APPROVED
        assert result.config_version.approved_by_user_id == admin_role_user.id

    @pytest.mark.asyncio
    async def test_member_role_cannot_approve(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        test_user_db: User,
        test_approver_user_db: User,
    ):
        """Test that users with member role cannot approve even at service level."""
        service = sub_agent_service
        owner = test_user_db
        other = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready for review", actor=owner)

        # Member cannot approve (should fail at service level even if router is bypassed)
        with pytest.raises(
            PermissionError, match="Approval with 'approve' capability requires group-based access to the sub-agen"
        ):
            await service.approve_version(pg_session, agent.id, 1, True, actor=other)


class TestSubmittedByUserTracking:
    """Test tracking of who submitted versions for approval."""

    @pytest.mark.asyncio
    async def test_submit_stores_submitter_id(
        self, pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
    ):
        """Test that submitting a version stores the submitter's user ID."""
        from sqlalchemy import text

        service = sub_agent_service
        user = test_user_db
        agent = await _create_sub_agent(pg_session, user, "Agent", sub_agent_service)

        # Submit for approval
        await service.submit_for_approval(pg_session, agent.id, "Ready for review", actor=user)
        await pg_session.commit()

        # Verify submitted_by_user_id is set in database
        result = await pg_session.execute(
            text("""
                SELECT submitted_by_user_id, status 
                FROM sub_agent_config_versions 
                WHERE sub_agent_id = :sub_agent_id AND version = 1
            """),
            {"sub_agent_id": agent.id},
        )
        row = result.mappings().first()

        assert row is not None
        assert row["submitted_by_user_id"] == user.id
        assert row["status"] == "pending_approval"

    @pytest.mark.asyncio
    async def test_non_owner_with_write_access_can_submit(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        test_user_db: User,
        test_approver_user_db: User,
    ):
        """Test that users with write access through groups can submit for approval."""
        from sqlalchemy import text

        service = sub_agent_service
        owner = test_user_db
        contributor = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Create a group and add contributor as write member
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, created_at, updated_at)
                VALUES (1, 'Test Group', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (1, :user_id, 'write', NOW())
            """),
            {"user_id": contributor.id},
        )
        # Grant group write permission to the sub-agent
        await pg_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
                VALUES (:sub_agent_id, 1, ARRAY['read', 'write'])
            """),
            {"sub_agent_id": agent.id},
        )
        await pg_session.commit()

        # Contributor submits for approval
        result = await service.submit_for_approval(pg_session, agent.id, "Changes from contributor", actor=contributor)
        await pg_session.commit()

        assert result is not None
        assert result.config_version is not None
        assert result.config_version.status == SubAgentStatus.PENDING_APPROVAL
        assert result.config_version.submitted_by_user_id == contributor.id

    @pytest.mark.asyncio
    async def test_approval_notifies_both_owner_and_submitter(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        notification_service: NotificationService,
        test_user_db: User,
        test_approver_user_db: User,
        test_admin_user_db: User,
    ):
        """Test that approval notifications are sent to both owner and submitter."""
        from unittest.mock import AsyncMock

        from sqlalchemy import text

        service = sub_agent_service
        service.set_notification_service(notification_service)

        owner = test_user_db
        submitter_id = test_admin_user_db
        approver = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Create a group and add approver to it with write access
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": approver.id},
        )

        # Grant group read permission on the sub-agent
        await pg_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions, created_at)
                VALUES (:sub_agent_id, :group_id, ARRAY['read'], NOW())
            """),
            {"sub_agent_id": agent.id, "group_id": group_id},
        )

        # Manually set submitter (simulating non-owner submission)
        await pg_session.execute(
            text("""
                UPDATE sub_agent_config_versions
                SET status = 'pending_approval', submitted_by_user_id = :submitter_id
                WHERE sub_agent_id = :sub_agent_id AND version = 1
            """),
            {"sub_agent_id": agent.id, "submitter_id": submitter_id.id},
        )
        await pg_session.commit()

        # Mock notification service
        notification_service.bulk_create_notifications = AsyncMock()

        # Approve
        await service.approve_version(pg_session, agent.id, 1, True, actor=approver)

        # Verify notifications were sent - should be called twice:
        # 1. Approval notifications for owner and submitter
        # 2. Owner auto-activation notification
        assert notification_service.bulk_create_notifications.call_count == 2

        # First call: approval notifications
        first_call_notifications = notification_service.bulk_create_notifications.call_args_list[0][0][1]
        notified_users_first = {n.user_id for n in first_call_notifications}
        assert owner.id in notified_users_first
        assert submitter_id.id in notified_users_first
        assert approver.id not in notified_users_first
        assert len(notified_users_first) == 2

        # Second call: owner auto-activation notification
        second_call_notifications = notification_service.bulk_create_notifications.call_args_list[1][0][1]
        assert len(second_call_notifications) == 1
        assert second_call_notifications[0].user_id == owner.id
        assert second_call_notifications[0].notification_type == NotificationType.AGENT_ACTIVATED

    @pytest.mark.asyncio
    async def test_rejection_notifies_both_owner_and_submitter(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        notification_service: NotificationService,
        test_user_db: User,
        test_approver_user_db: User,
        test_admin_user_db: User,
    ):
        """Test that rejection notifications are sent to both owner and submitter."""
        from unittest.mock import AsyncMock

        from sqlalchemy import text

        service = sub_agent_service
        service.set_notification_service(notification_service)

        owner = test_user_db
        submitter_id = test_admin_user_db
        approver = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Create a group and add approver to it with write access
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": approver.id},
        )

        # Grant group read permission on the sub-agent
        await pg_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions, created_at)
                VALUES (:sub_agent_id, :group_id, ARRAY['read'], NOW())
            """),
            {"sub_agent_id": agent.id, "group_id": group_id},
        )

        # Manually set submitter (simulating non-owner submission)
        await pg_session.execute(
            text("""
                UPDATE sub_agent_config_versions
                SET status = 'pending_approval', submitted_by_user_id = :submitter_id
                WHERE sub_agent_id = :sub_agent_id AND version = 1
            """),
            {"sub_agent_id": agent.id, "submitter_id": submitter_id.id},
        )
        await pg_session.commit()

        # Mock notification service
        notification_service.bulk_create_notifications = AsyncMock()

        # Reject
        await service.approve_version(
            pg_session, agent.id, 1, False, actor=approver, rejection_reason="Needs more work"
        )

        # Verify notifications were sent
        notification_service.bulk_create_notifications.assert_called_once()
        notifications = notification_service.bulk_create_notifications.call_args[0][1]

        # Should notify both owner and submitter (but not approver)
        notified_users = {n.user_id for n in notifications}
        assert owner.id in notified_users
        assert submitter_id.id in notified_users
        assert approver.id not in notified_users
        assert len(notified_users) == 2

        # Verify rejection reason is included
        for notification in notifications:
            assert notification.notification_type.value == "approval_rejected"
            assert "Needs more work" in notification.message

    @pytest.mark.asyncio
    async def test_owner_as_submitter_gets_single_notification(
        self,
        pg_session: AsyncSession,
        sub_agent_service: SubAgentService,
        notification_service: NotificationService,
        test_user_db: User,
        test_approver_user_db: User,
    ):
        """Test that when owner submits their own agent, they get only one notification."""
        from unittest.mock import AsyncMock

        from sqlalchemy import text

        service = sub_agent_service
        service.set_notification_service(notification_service)

        owner = test_user_db
        approver = test_approver_user_db
        agent = await _create_sub_agent(pg_session, owner, "Agent", sub_agent_service)

        # Create a group and add approver to it with write access
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": approver.id},
        )

        # Grant group read permission on the sub-agent
        await pg_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions, created_at)
                VALUES (:sub_agent_id, :group_id, ARRAY['read'], NOW())
            """),
            {"sub_agent_id": agent.id, "group_id": group_id},
        )
        await pg_session.commit()

        # Submit for approval (owner is submitter)
        await service.submit_for_approval(pg_session, agent.id, "Ready", actor=owner)
        await pg_session.commit()

        # Mock notification service
        notification_service.bulk_create_notifications = AsyncMock()

        # Approve
        await service.approve_version(pg_session, agent.id, 1, True, actor=approver)

        # Verify notifications were sent - should be called twice:
        # 1. Approval notification for owner (single notification since owner == submitter)
        # 2. Owner auto-activation notification
        assert notification_service.bulk_create_notifications.call_count == 2

        # First call: approval notification (owner only, no duplicate)
        first_call_notifications = notification_service.bulk_create_notifications.call_args_list[0][0][1]
        assert len(first_call_notifications) == 1
        assert first_call_notifications[0].user_id == owner.id
        assert first_call_notifications[0].notification_type == NotificationType.APPROVAL_COMPLETED

        # Second call: owner auto-activation notification
        second_call_notifications = notification_service.bulk_create_notifications.call_args_list[1][0][1]
        assert len(second_call_notifications) == 1
        assert second_call_notifications[0].user_id == owner.id
        assert second_call_notifications[0].notification_type == NotificationType.AGENT_ACTIVATED


@pytest.mark.asyncio
async def test_pricing_config_read_write_integration(
    pg_session: AsyncSession, sub_agent_service: SubAgentService, test_user_db: User
):
    """Test that pricing_config can be written to and read from the database using service layer."""

    # Define pricing config
    pricing_config = {
        "rate_card_entries": [
            {"billing_unit": "api_calls", "price_per_million": 2.5},
            {"billing_unit": "database_queries", "price_per_million": 1.0},
        ]
    }

    # Create sub-agent using service layer (tests the write path)
    sub_agent_data = SubAgentCreate(
        name="pricing-test-agent",
        type=SubAgentType.REMOTE,
        description="Test agent with pricing",
        agent_url="http://test.com",
        pricing_config=pricing_config,
    )

    sub_agent = await sub_agent_service.create_sub_agent(
        db=pg_session,
        actor=test_user_db,
        data=sub_agent_data,
    )

    # Read back using service (tests the read path)
    sub_agent_read = await sub_agent_service.get_sub_agent_by_id(pg_session, sub_agent.id)

    # Verify pricing_config was persisted and read correctly
    assert sub_agent_read is not None
    assert sub_agent_read.config_version is not None
    assert sub_agent_read.config_version.pricing_config is not None
    assert sub_agent_read.config_version.pricing_config == pricing_config
