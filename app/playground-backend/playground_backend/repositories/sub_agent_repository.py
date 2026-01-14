"""Repository for sub-agent operations with automatic audit logging."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from .base import AuditedRepository

logger = logging.getLogger(__name__)


@dataclass
class ApprovalContext:
    """Context for approval operations."""

    sub_agent_id: int
    version: int
    admin_user_id: str
    admin_sub: str
    action: Literal["approve", "reject"]
    rejection_reason: str | None = None
    release_number: int | None = None


class SubAgentRepository(AuditedRepository):
    """Repository for sub-agent operations with automatic audit logging."""

    def __init__(self):
        super().__init__(entity_type=AuditEntityType.SUB_AGENT, table_name="sub_agents")

    async def get_version_status(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int,
    ) -> tuple[str, str]:
        """
        Get version status and owner.

        Args:
            db: Database session
            sub_agent_id: Sub-agent ID
            version: Version number

        Returns:
            Tuple of (status, owner_user_id)

        Raises:
            ValueError: If version not found
        """
        query = text("""
            SELECT scv.status, sa.owner_user_id
            FROM sub_agent_config_versions scv
            JOIN sub_agents sa ON sa.id = scv.sub_agent_id
            WHERE scv.sub_agent_id = :sub_agent_id 
            AND scv.version = :version
        """)
        result = await db.execute(
            query,
            {
                "sub_agent_id": sub_agent_id,
                "version": version,
            },
        )
        row = result.first()
        if not row:
            raise ValueError(f"Version {version} not found for sub-agent {sub_agent_id}")
        return row[0], row[1]

    async def get_next_release_number(
        self,
        db: AsyncSession,
        sub_agent_id: int,
    ) -> int:
        """
        Get next release number for sub-agent.

        Args:
            db: Database session
            sub_agent_id: Sub-agent ID

        Returns:
            Next release number (starts at 1)
        """
        query = text("""
            SELECT COALESCE(MAX(release_number), 0) + 1 as next_release
            FROM sub_agent_config_versions
            WHERE sub_agent_id = :sub_agent_id
        """)
        result = await db.execute(query, {"sub_agent_id": sub_agent_id})
        return result.scalar_one()

    async def approve_version(
        self,
        db: AsyncSession,
        context: ApprovalContext,
    ) -> None:
        """
        Approve a version and set as default.

        This operation:
        1. Updates version status to approved
        2. Sets release number
        3. Sets as default version
        4. Automatically audits the action

        Args:
            db: Database session
            context: Approval context with all necessary information
        """
        now = datetime.now(timezone.utc)

        try:
            # Update version status
            await db.execute(
                text("""
                    UPDATE sub_agent_config_versions
                    SET status = 'approved',
                        approved_by_user_id = :admin_id,
                        approved_at = :now,
                        rejection_reason = NULL,
                        release_number = :release_number
                    WHERE sub_agent_id = :sub_agent_id 
                    AND version = :version
                """),
                {
                    "sub_agent_id": context.sub_agent_id,
                    "version": context.version,
                    "admin_id": context.admin_user_id,
                    "now": now,
                    "release_number": context.release_number,
                },
            )

            # Set as default version
            await db.execute(
                text("""
                    UPDATE sub_agents
                    SET default_version = :version, updated_at = :now
                    WHERE id = :id
                """),
                {
                    "id": context.sub_agent_id,
                    "version": context.version,
                    "now": now,
                },
            )

            # Auto-audit with detailed changes
            await self.audit_service.log_action(
                db=db,
                actor_sub=context.admin_sub,
                entity_type=self.entity_type,
                entity_id=str(context.sub_agent_id),
                action=AuditAction.APPROVE,
                changes={
                    "sub_agent_id": context.sub_agent_id,
                    "version": context.version,
                    "action": "approve",
                    "previous_status": "pending_approval",
                    "new_status": "approved",
                    "release_number": context.release_number,
                },
            )

            logger.info(
                f"Approved sub-agent {context.sub_agent_id} version {context.version} "
                f"as release {context.release_number} by {context.admin_sub}"
            )

        except Exception as e:
            logger.error(f"Failed to approve sub-agent {context.sub_agent_id} version {context.version}: {e}")
            raise

    async def reject_version(
        self,
        db: AsyncSession,
        context: ApprovalContext,
    ) -> None:
        """
        Reject a version.

        Updates version status and automatically audits the action.

        Args:
            db: Database session
            context: Approval context with rejection reason
        """
        now = datetime.now(timezone.utc)

        try:
            # Update version status
            await db.execute(
                text("""
                    UPDATE sub_agent_config_versions
                    SET status = 'rejected',
                        approved_by_user_id = :admin_id,
                        rejection_reason = :reason
                    WHERE sub_agent_id = :sub_agent_id 
                    AND version = :version
                """),
                {
                    "sub_agent_id": context.sub_agent_id,
                    "version": context.version,
                    "admin_id": context.admin_user_id,
                    "reason": context.rejection_reason,
                },
            )

            # Update sub_agents timestamp
            await db.execute(
                text("UPDATE sub_agents SET updated_at = :now WHERE id = :id"),
                {"id": context.sub_agent_id, "now": now},
            )

            # Auto-audit
            await self.audit_service.log_action(
                db=db,
                actor_sub=context.admin_sub,
                entity_type=self.entity_type,
                entity_id=str(context.sub_agent_id),
                action=AuditAction.REJECT,
                changes={
                    "sub_agent_id": context.sub_agent_id,
                    "version": context.version,
                    "action": "reject",
                    "previous_status": "pending_approval",
                    "new_status": "rejected",
                    "rejection_reason": context.rejection_reason,
                },
            )

            logger.info(
                f"Rejected sub-agent {context.sub_agent_id} version {context.version} "
                f"by {context.admin_sub}: {context.rejection_reason}"
            )

        except Exception as e:
            logger.error(f"Failed to reject sub-agent {context.sub_agent_id} version {context.version}: {e}")
            raise

    async def update_permissions(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        group_permissions: list[dict],
    ) -> None:
        """
        Update group permissions for sub-agent.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            group_permissions: List of dicts with user_group_id and permissions
        """
        try:
            # Fetch before state
            before_query = text("""
                SELECT user_group_id, permissions
                FROM sub_agent_permissions
                WHERE sub_agent_id = :id
            """)
            result = await db.execute(before_query, {"id": sub_agent_id})
            rows = result.mappings().all()
            before_perms = [{"user_group_id": row["user_group_id"], "permissions": row["permissions"]} for row in rows]

            # Delete existing
            await db.execute(
                text("DELETE FROM sub_agent_permissions WHERE sub_agent_id = :id"),
                {"id": sub_agent_id},
            )

            # Insert new
            now = datetime.now(timezone.utc)
            for perm in group_permissions:
                await db.execute(
                    text("""
                        INSERT INTO sub_agent_permissions 
                        (sub_agent_id, user_group_id, permissions, created_at)
                        VALUES (:sub_agent_id, :user_group_id, :permissions, :now)
                    """),
                    {
                        "sub_agent_id": sub_agent_id,
                        "user_group_id": perm["user_group_id"],
                        "permissions": perm["permissions"],
                        "now": now,
                    },
                )

            # Custom audit for permission change
            await self.audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(sub_agent_id),
                action=AuditAction.PERMISSION_UPDATE,
                changes={
                    "before": {"permissions": before_perms},
                    "after": {"permissions": group_permissions},
                },
            )

            logger.info(f"Updated permissions for sub-agent {sub_agent_id} by {actor_sub}")

        except Exception as e:
            logger.error(f"Failed to update permissions for sub-agent {sub_agent_id}: {e}")
            raise

    async def activate_sub_agent(
        self,
        db: AsyncSession,
        actor_sub: str,
        user_id: str,
        sub_agent_id: int,
    ) -> None:
        """
        Activate a sub-agent for user.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            user_id: User ID
            sub_agent_id: Sub-agent ID
        """
        try:
            now = datetime.now(timezone.utc)

            query = text("""
                INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_at)
                VALUES (:user_id, :sub_agent_id, :now)
                ON CONFLICT (user_id, sub_agent_id) DO UPDATE
                SET activated_at = :now
            """)

            await db.execute(
                query,
                {"user_id": user_id, "sub_agent_id": sub_agent_id, "now": now},
            )

            # Auto-audit
            await self.audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(sub_agent_id),
                action=AuditAction.ACTIVATE,
                changes={
                    "user_id": user_id,
                    "sub_agent_id": sub_agent_id,
                    "activated_at": now.isoformat(),
                },
            )

            logger.info(f"Activated sub-agent {sub_agent_id} for user {user_id} by {actor_sub}")

        except Exception as e:
            logger.error(f"Failed to activate sub-agent {sub_agent_id} for user {user_id}: {e}")
            raise

    async def deactivate_sub_agent(
        self,
        db: AsyncSession,
        actor_sub: str,
        user_id: str,
        sub_agent_id: int,
    ) -> None:
        """
        Deactivate a sub-agent for user.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            user_id: User ID
            sub_agent_id: Sub-agent ID
        """
        try:
            query = text("""
                DELETE FROM user_sub_agent_activations 
                WHERE user_id = :user_id AND sub_agent_id = :sub_agent_id
            """)

            await db.execute(
                query,
                {"user_id": user_id, "sub_agent_id": sub_agent_id},
            )

            # Auto-audit
            await self.audit_service.log_action(
                db=db,
                actor_sub=actor_sub,
                entity_type=self.entity_type,
                entity_id=str(sub_agent_id),
                action=AuditAction.DEACTIVATE,
                changes={
                    "user_id": user_id,
                    "sub_agent_id": sub_agent_id,
                },
            )

            logger.info(f"Deactivated sub-agent {sub_agent_id} for user {user_id} by {actor_sub}")

        except Exception as e:
            logger.error(f"Failed to deactivate sub-agent {sub_agent_id} for user {user_id}: {e}")
            raise

    async def update_sub_agent(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        fields: dict[str, Any],
    ) -> None:
        """
        Update sub-agent fields (name, is_public).

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            fields: Fields to update (name, is_public)
        """
        await self.update(
            db=db,
            actor_sub=actor_sub,
            entity_id=sub_agent_id,
            fields=fields,
            fetch_before=True,
        )

    async def update_current_version(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        version: int,
    ) -> None:
        """
        Update the current_version pointer of a sub-agent.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            version: New current version number
        """
        now = datetime.now(timezone.utc)
        query = text("""
            UPDATE sub_agents 
            SET current_version = :version, updated_at = :now 
            WHERE id = :id
        """)

        await db.execute(query, {"id": sub_agent_id, "version": version, "now": now})

        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.UPDATE,
            changes={"current_version": version},
        )

        logger.info(f"Updated current_version to {version} for sub-agent {sub_agent_id}")

    async def update_sub_agent_timestamp(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
    ) -> None:
        """
        Update only the updated_at timestamp of a sub-agent.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
        """
        now = datetime.now(timezone.utc)
        query = text("UPDATE sub_agents SET updated_at = :now WHERE id = :id")
        await db.execute(query, {"id": sub_agent_id, "now": now})

        # Note: No audit log for timestamp-only updates as these are side effects of other operations

    async def delete_version(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        version: int,
    ) -> None:
        """
        Soft delete a config version.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            version: Version number to delete
        """

        now = datetime.now(timezone.utc)
        query = text("""
            UPDATE sub_agent_config_versions
            SET deleted_at = :now
            WHERE sub_agent_id = :sub_agent_id AND version = :version
        """)

        await db.execute(query, {"sub_agent_id": sub_agent_id, "version": version, "now": now})

        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.DELETE,
            changes={"version": version},
        )

        logger.info(f"Deleted version {version} of sub-agent {sub_agent_id}")

    async def update_current_version_to_previous(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
    ) -> None:
        """
        Update current_version to the maximum non-deleted version.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
        """

        now = datetime.now(timezone.utc)
        query = text("""
            UPDATE sub_agents
            SET current_version = (
                SELECT MAX(version) FROM sub_agent_config_versions
                WHERE sub_agent_id = :sub_agent_id AND deleted_at IS NULL
            ),
            updated_at = :now
            WHERE id = :sub_agent_id
        """)

        await db.execute(query, {"sub_agent_id": sub_agent_id, "now": now})

        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.UPDATE,
            changes={"current_version": "reverted_to_previous"},
        )

        logger.info(f"Updated current_version to previous for sub-agent {sub_agent_id}")

    async def submit_version_for_approval(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        version: int,
        change_summary: str | None = None,
    ) -> None:
        """
        Submit a version for approval by updating its status.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            version: Version number
            change_summary: Optional change summary
        """
        query = text("""
            UPDATE sub_agent_config_versions
            SET status = 'pending_approval', rejection_reason = NULL, change_summary = :change_summary
            WHERE sub_agent_id = :sub_agent_id AND version = :version
        """)

        await db.execute(
            query,
            {"sub_agent_id": sub_agent_id, "version": version, "change_summary": change_summary},
        )

        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.SUBMIT_FOR_APPROVAL,
            changes={"version": version, "change_summary": change_summary},
        )

        logger.info(f"Submitted version {version} of sub-agent {sub_agent_id} for approval")

    async def set_default_version(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        version: int,
    ) -> None:
        """
        Set the default version of a sub-agent.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            version: Version number to set as default
        """

        now = datetime.now(timezone.utc)
        query = text("""
            UPDATE sub_agents
            SET default_version = :version, updated_at = :now
            WHERE id = :id
        """)

        await db.execute(query, {"id": sub_agent_id, "version": version, "now": now})

        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.SET_DEFAULT,
            changes={"default_version": version},
        )

        logger.info(f"Set default_version to {version} for sub-agent {sub_agent_id}")

    async def create_config_version(
        self,
        db: AsyncSession,
        actor_sub: str,
        sub_agent_id: int,
        version: int,
        version_hash: str,
        change_summary: str,
        status: str,
        description: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        agent_url: str | None = None,
        mcp_tools: list[str] | None = None,
        foundry_hostname: str | None = None,
        foundry_client_id: str | None = None,
        foundry_client_secret_ref: int | None = None,
        foundry_ontology_rid: str | None = None,
        foundry_query_api_name: str | None = None,
        foundry_scopes: list[str] | None = None,
        foundry_version: str | None = None,
        pricing_config: dict | None = None,
    ) -> int:
        """
        Create a new configuration version with automatic audit logging.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            sub_agent_id: Sub-agent ID
            version: Version number
            version_hash: Hash of the version
            change_summary: Description of changes
            status: Version status (draft, pending_approval, approved, rejected)
            description: Version description
            model: Model name
            system_prompt: System prompt for local agents
            agent_url: Agent URL for remote agents
            mcp_tools: List of MCP tools
            foundry_hostname: Foundry hostname for Foundry agents
            foundry_client_id: Foundry client ID
            foundry_client_secret_ref: Reference to secret for Foundry client secret
            foundry_ontology_rid: Foundry ontology RID
            foundry_query_api_name: Foundry query API name
            foundry_scopes: List of Foundry scopes
            foundry_version: Foundry version

        Returns:
            The ID of the newly created version
        """

        now = datetime.now(timezone.utc)
        mcp_tools_list = mcp_tools if mcp_tools is not None else []

        query = text("""
            INSERT INTO sub_agent_config_versions
            (sub_agent_id, version, version_hash, description, model, system_prompt, agent_url, mcp_tools, 
             foundry_hostname, foundry_client_id, foundry_client_secret_ref, foundry_ontology_rid, 
             foundry_query_api_name, foundry_scopes, foundry_version, pricing_config, change_summary, status, created_at)
            VALUES (:sub_agent_id, :version, :version_hash, :description, :model, :system_prompt, :agent_url, 
                    CAST(:mcp_tools AS jsonb), :foundry_hostname, :foundry_client_id, :foundry_client_secret_ref, 
                    :foundry_ontology_rid, :foundry_query_api_name, CAST(:foundry_scopes AS text[]), 
                    :foundry_version, CAST(:pricing_config AS jsonb), :change_summary, :status, :now)
            RETURNING id
        """)
        result = await db.execute(
            query,
            {
                "sub_agent_id": sub_agent_id,
                "version": version,
                "version_hash": version_hash,
                "description": description,
                "model": model,
                "system_prompt": system_prompt,
                "agent_url": agent_url,
                "mcp_tools": json.dumps(mcp_tools_list),
                "foundry_hostname": foundry_hostname,
                "foundry_client_id": foundry_client_id,
                "foundry_client_secret_ref": foundry_client_secret_ref,
                "foundry_ontology_rid": foundry_ontology_rid,
                "foundry_query_api_name": foundry_query_api_name,
                "foundry_scopes": foundry_scopes,
                "foundry_version": foundry_version,
                "pricing_config": json.dumps(pricing_config) if pricing_config else None,
                "change_summary": change_summary,
                "status": status,
                "now": now,
            },
        )
        version_id = result.scalar_one()

        # Log audit - capture the full configuration
        changes = {
            "sub_agent_id": sub_agent_id,
            "version": version,
            "version_hash": version_hash,
            "description": description,
            "model": model,
            "system_prompt": system_prompt,
            "agent_url": agent_url,
            "mcp_tools": mcp_tools_list,
            "foundry_hostname": foundry_hostname,
            "foundry_client_id": foundry_client_id,
            "foundry_client_secret_ref": foundry_client_secret_ref,
            "foundry_ontology_rid": foundry_ontology_rid,
            "foundry_query_api_name": foundry_query_api_name,
            "foundry_scopes": foundry_scopes,
            "foundry_version": foundry_version,
            "pricing_config": pricing_config,
            "change_summary": change_summary,
            "status": status,
        }

        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.CREATE,
            changes={"after": changes},
        )

        logger.info(f"Created config version {version} (ID={version_id}) for sub-agent {sub_agent_id}")
        return version_id
