"""Service for sub-agent CRUD operations using PostgreSQL."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization import SYSTEM_ROLE_CAPABILITIES, check_action_allowed
from ..config import config
from ..models.notification import NotificationData, NotificationType
from ..models.sub_agent import (
    ActivationSource,
    SubAgent,
    SubAgentConfigVersion,
    SubAgentCreate,
    SubAgentOwner,
    SubAgentStatus,
    SubAgentType,
    SubAgentUpdate,
    ThinkingLevel,
)
from ..models.user import User
from ..repositories.sub_agent_repository import ApprovalContext
from ..services.notification_service import NotificationService

if TYPE_CHECKING:
    from ..repositories.sub_agent_repository import SubAgentRepository


logger = logging.getLogger(__name__)

# Models that support Extended Thinking
MODELS_SUPPORTING_THINKING = {
    "claude-sonnet-4.5",
    "claude-haiku-4-5",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
}


def _validate_automated_constraints(
    system_prompt: str | None, mcp_tools: list[str] | None, is_public: bool | None
) -> None:
    """Validate that an automated sub-agent meets the required constraints.

    Raises ValueError if any constraint is violated.
    """
    max_prompt = config.auto_approve.max_system_prompt_length
    max_tools = config.auto_approve.max_mcp_tools_count
    system_prompt_len = len(system_prompt or "")
    mcp_tools_count = len(mcp_tools or [])
    if system_prompt_len > max_prompt:
        raise ValueError(
            f"Automated sub-agent system_prompt must be ≤ {max_prompt} characters (got {system_prompt_len})."
        )
    if mcp_tools_count > max_tools:
        raise ValueError(f"Automated sub-agent may reference at most {max_tools} MCP tools (got {mcp_tools_count}).")
    if is_public:
        raise ValueError("Automated sub-agents must be private (is_public=False).")


def _meets_auto_approve_constraints(
    sub_agent_type: SubAgentType, system_prompt: str | None, mcp_tools: list[str] | None, is_public: bool | None
) -> bool:
    """Check if a sub-agent meets the constraints for auto-approval."""
    if sub_agent_type not in {SubAgentType.AUTOMATED, SubAgentType.LOCAL}:
        return False
    return (
        len(system_prompt or "") <= config.auto_approve.max_system_prompt_length
        and len(mcp_tools or []) <= config.auto_approve.max_mcp_tools_count
        and not (is_public if is_public is not None else False)
    )


def _normalize_thinking_config(
    model: str | None,
    enable_thinking: bool | None,
    thinking_level: ThinkingLevel | None,
) -> tuple[bool | None, ThinkingLevel | None]:
    """Normalize Extended Thinking configuration based on model support.

    Returns (enable_thinking, thinking_level) with automatic nullification:
    - If model doesn't support thinking: both set to None
    - If enable_thinking is False: thinking_level set to None
    - Otherwise: preserve the provided values

    Args:
        model: The model identifier (e.g., 'claude-sonnet-4.5')
        enable_thinking: Whether Extended Thinking is enabled
        thinking_level: The thinking level ('minimal', 'low', 'medium', 'high')

    Returns:
        Tuple of (normalized_enable_thinking, normalized_thinking_level)
    """
    if model is None:
        return (None, None)
    # If model doesn't support thinking, force both to None
    if model and model not in MODELS_SUPPORTING_THINKING:
        return (None, None)

    # If thinking is explicitly disabled, set level to None
    if enable_thinking is False:
        return (False, None)

    # Preserve the provided values
    return (enable_thinking, thinking_level)


class SubAgentService:
    """Service for managing sub-agents in PostgreSQL.

    The data model is normalized:
    - sub_agents table: metadata only (id, name, owner, type, current_version, default_version)
    - sub_agent_config_versions table: all configuration data (description, model, config, status)

    When fetching sub-agents, we join with the appropriate version:
    - For list views: join with current_version to show latest state
    - For orchestrator: join with default_version to get approved config
    - For specific version: join with the requested version
    """

    def __init__(
        self,
        sub_agent_repository: "SubAgentRepository | None" = None,
        notification_service: NotificationService | None = None,
    ):
        """Initialize sub-agent service.

        Args:
            sub_agent_repository: Optional sub-agent repository instance.
                If None, must be set via set_repository() before use.
            notification_service: Optional notification service instance.
        """
        self._repo = sub_agent_repository
        self._notification_service = notification_service

    def set_repository(self, sub_agent_repository: "SubAgentRepository") -> None:
        """Set the sub-agent repository (dependency injection)."""
        self._repo = sub_agent_repository

    @property
    def repo(self) -> "SubAgentRepository":
        """Get the sub-agent repository, raising error if not set."""
        if self._repo is None:
            raise RuntimeError("SubAgentRepository not injected. Call set_repository() during initialization.")
        return self._repo

    def set_notification_service(self, notification_service: NotificationService) -> None:
        """Set the notification service (dependency injection)."""
        self._notification_service = notification_service

    @property
    def notification_service(self) -> NotificationService | None:
        """Get the notification service."""
        return self._notification_service

    async def get_accessible_sub_agents(
        self,
        db: AsyncSession,
        user_id: str,
        is_admin: bool = False,
        status_filter: SubAgentStatus | None = None,
        include_owned: bool = True,
        activated_only: bool = False,
    ) -> list[SubAgent]:
        """Get sub-agents accessible to the user.

        Returns sub-agents that are:
        - Owned by the user (if include_owned=True)
        - Public sub-agents (is_public=true)
        - Assigned to user's groups
        - All sub-agents if user is admin

        Always joins with current_version to show the latest state.
        Includes is_activated field showing if user has activated the sub-agent.
        If activated_only=True, only returns activated sub-agents.
        """
        base_select = """
            SELECT sa.id, sa.name, sa.owner_user_id, sa.owner_status, sa.type,
                   sa.current_version, sa.default_version, sa.is_public, sa.deleted_at,
                   sa.created_at, sa.updated_at,
                   u.email as owner_email, u.first_name, u.last_name,
                   cv.id as cv_id, cv.version as cv_version,
                   cv.version_hash as cv_version_hash, cv.release_number as cv_release_number,
                   cv.description as cv_description,
                   cv.model as cv_model, cv.system_prompt as cv_system_prompt,
                   cv.enable_thinking as cv_enable_thinking,
                   cv.thinking_level as cv_thinking_level,
                   cv.agent_url as cv_agent_url,
                   cv.mcp_tools as cv_mcp_tools,
                   cv.foundry_hostname as cv_foundry_hostname,
                   cv.foundry_client_id as cv_foundry_client_id,
                   cv.foundry_client_secret_ref as cv_foundry_client_secret_ref,
                   s.ssm_parameter_name as cv_foundry_client_secret_ssmkey,  -- needed for the orchestrator
                   cv.foundry_ontology_rid as cv_foundry_ontology_rid,
                   cv.foundry_query_api_name as cv_foundry_query_api_name,
                   cv.foundry_scopes as cv_foundry_scopes,
                   cv.foundry_version as cv_foundry_version,
                   cv.pricing_config as cv_pricing_config,
                   cv.change_summary as cv_change_summary, cv.status as cv_status,
                   cv.submitted_by_user_id as cv_submitted_by_user_id,
                   cv.approved_by_user_id as cv_approved_by_user_id,
                   cv.approved_at as cv_approved_at, cv.rejection_reason as cv_rejection_reason,
                   cv.deleted_at as cv_deleted_at, cv.created_at as cv_created_at,
                   (usa.sub_agent_id IS NOT NULL) as is_activated,
                   usa.activated_by as activated_by,
                   usa.activated_by_groups as activated_by_groups
            FROM sub_agents sa
            JOIN users u ON sa.owner_user_id = u.id
            LEFT JOIN sub_agent_config_versions cv 
                ON sa.id = cv.sub_agent_id AND sa.default_version = cv.version
            LEFT JOIN secrets s ON cv.foundry_client_secret_ref = s.id
            LEFT JOIN user_sub_agent_activations usa 
                ON sa.id = usa.sub_agent_id AND usa.user_id = :user_id
        """

        activation_filter = (
            "AND (usa.sub_agent_id IS NOT NULL OR (sa.owner_user_id = 'system' AND sa.is_public = TRUE))"
            if activated_only
            else ""
        )

        if is_admin and status_filter is None:
            # Admins see all sub-agents
            query = text(f"""
                {base_select}
                WHERE sa.deleted_at IS NULL {activation_filter}
                ORDER BY sa.updated_at DESC
            """)
            result = await db.execute(query, {"user_id": user_id})
        elif is_admin and status_filter:
            query = text(f"""
                {base_select}
                WHERE cv.status = :status AND sa.deleted_at IS NULL {activation_filter}
                ORDER BY sa.updated_at DESC
            """)
            result = await db.execute(query, {"status": status_filter.value, "user_id": user_id})
        else:
            # Non-admins see owned + public + group-assigned sub-agents
            query_str = f"""
                SELECT DISTINCT sa.id, sa.name, sa.owner_user_id, sa.owner_status, sa.type,
                       sa.current_version, sa.default_version, sa.is_public, sa.deleted_at,
                       sa.created_at, sa.updated_at,
                       u.email as owner_email, u.first_name, u.last_name,
                       cv.id as cv_id, cv.version as cv_version,
                       cv.version_hash as cv_version_hash, cv.release_number as cv_release_number,
                       cv.description as cv_description,
                       cv.model as cv_model, cv.system_prompt as cv_system_prompt,
                       cv.enable_thinking as cv_enable_thinking,
                       cv.thinking_level as cv_thinking_level,
                       cv.agent_url as cv_agent_url,
                       cv.mcp_tools as cv_mcp_tools,
                       cv.foundry_hostname as cv_foundry_hostname,
                       cv.foundry_client_id as cv_foundry_client_id,
                       cv.foundry_client_secret_ref as cv_foundry_client_secret_ref,
                       s.ssm_parameter_name as cv_foundry_client_secret_ssmkey,  -- needed for the orchestrator
                       cv.foundry_ontology_rid as cv_foundry_ontology_rid,
                       cv.foundry_query_api_name as cv_foundry_query_api_name,
                       cv.foundry_scopes as cv_foundry_scopes,
                       cv.foundry_version as cv_foundry_version,
                       cv.pricing_config as cv_pricing_config,
                       cv.enable_thinking as cv_enable_thinking,
                       cv.thinking_level as cv_thinking_level,
                       cv.change_summary as cv_change_summary, cv.status as cv_status,
                       cv.submitted_by_user_id as cv_submitted_by_user_id,
                       cv.approved_by_user_id as cv_approved_by_user_id,
                       cv.approved_at as cv_approved_at, cv.rejection_reason as cv_rejection_reason,
                       cv.deleted_at as cv_deleted_at, cv.created_at as cv_created_at,
                       (usa.sub_agent_id IS NOT NULL) as is_activated,
                       usa.activated_by as activated_by,
                       usa.activated_by_groups as activated_by_groups
                FROM sub_agents sa
                JOIN users u ON sa.owner_user_id = u.id
                LEFT JOIN sub_agent_config_versions cv 
                    ON sa.id = cv.sub_agent_id AND sa.default_version = cv.version
                LEFT JOIN secrets s ON cv.foundry_client_secret_ref = s.id
                LEFT JOIN sub_agent_permissions sap ON sa.id = sap.sub_agent_id
                LEFT JOIN user_group_members ugm ON sap.user_group_id = ugm.user_group_id
                LEFT JOIN user_sub_agent_activations usa 
                    ON sa.id = usa.sub_agent_id AND usa.user_id = :user_id
                WHERE sa.deleted_at IS NULL AND (
                    (:include_owned AND sa.owner_user_id = :user_id)
                    OR sa.is_public = TRUE
                    OR ugm.user_id = :user_id
                ) {activation_filter}
            """
            if status_filter:
                query_str += "AND cv.status = :status "
                query_str += "ORDER BY sa.updated_at DESC"
                query = text(query_str)
                result = await db.execute(
                    query,
                    {"user_id": user_id, "include_owned": include_owned, "status": status_filter.value},
                )
            else:
                query_str += "ORDER BY sa.updated_at DESC"
                query = text(query_str)
                result = await db.execute(query, {"user_id": user_id, "include_owned": include_owned})

        rows = result.mappings().all()
        return [self._row_to_sub_agent_with_version(row) for row in rows]

    async def get_pending_approvals(self, db: AsyncSession) -> list[SubAgent]:
        """Get all sub-agents with versions pending approval (admin only)."""
        query = text("""
            SELECT sa.id, sa.name, sa.owner_user_id, sa.owner_status, sa.type,
                   sa.current_version, sa.default_version, sa.is_public, sa.deleted_at,
                   sa.created_at, sa.updated_at,
                   u.email as owner_email, u.first_name, u.last_name,
                   cv.id as cv_id, cv.version as cv_version,
                   cv.version_hash as cv_version_hash, cv.release_number as cv_release_number,
                   cv.description as cv_description,
                   cv.model as cv_model, cv.system_prompt as cv_system_prompt,
                   cv.enable_thinking as cv_enable_thinking,
                   cv.thinking_level as cv_thinking_level,
                   cv.agent_url as cv_agent_url,
                   cv.mcp_tools as cv_mcp_tools,
                   cv.foundry_hostname as cv_foundry_hostname,
                   cv.foundry_client_id as cv_foundry_client_id,
                   cv.foundry_client_secret_ref as cv_foundry_client_secret_ref,
                   cv.foundry_ontology_rid as cv_foundry_ontology_rid,
                   cv.foundry_query_api_name as cv_foundry_query_api_name,
                   cv.foundry_scopes as cv_foundry_scopes,
                   cv.foundry_version as cv_foundry_version,
                   cv.pricing_config as cv_pricing_config,
                   cv.change_summary as cv_change_summary, cv.status as cv_status,
                   cv.submitted_by_user_id as cv_submitted_by_user_id,
                   cv.approved_by_user_id as cv_approved_by_user_id,
                   cv.approved_at as cv_approved_at, cv.rejection_reason as cv_rejection_reason,
                   cv.deleted_at as cv_deleted_at, cv.created_at as cv_created_at
            FROM sub_agents sa
            JOIN users u ON sa.owner_user_id = u.id
            JOIN sub_agent_config_versions cv 
                ON sa.id = cv.sub_agent_id AND sa.current_version = cv.version
            WHERE cv.status = 'pending_approval' AND sa.deleted_at IS NULL
            ORDER BY cv.created_at ASC
        """)
        result = await db.execute(query)
        rows = result.mappings().all()
        return [self._row_to_sub_agent_with_version(row) for row in rows]

    async def get_sub_agent_by_id(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int | None = None,
    ) -> SubAgent | None:
        """Get a sub-agent by ID.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID
            version: If provided, join with this specific version.
                     Otherwise join with current_version.
        """
        query = text("""
            SELECT sa.id, sa.name, sa.owner_user_id, sa.owner_status, sa.type,
                   sa.current_version, sa.default_version, sa.is_public, sa.deleted_at,
                   sa.created_at, sa.updated_at,
                   u.email as owner_email, u.first_name, u.last_name,
                   cv.id as cv_id, cv.version as cv_version,
                   cv.version_hash as cv_version_hash, cv.release_number as cv_release_number,
                   cv.description as cv_description,
                   cv.model as cv_model, cv.system_prompt as cv_system_prompt,
                   cv.enable_thinking as cv_enable_thinking,
                   cv.thinking_level as cv_thinking_level,
                   cv.agent_url as cv_agent_url, cv.mcp_tools as cv_mcp_tools,
                   cv.foundry_hostname as cv_foundry_hostname,
                   cv.foundry_client_id as cv_foundry_client_id,
                   cv.foundry_client_secret_ref as cv_foundry_client_secret_ref,
                   cv.foundry_ontology_rid as cv_foundry_ontology_rid,
                   cv.foundry_query_api_name as cv_foundry_query_api_name,
                   cv.foundry_scopes as cv_foundry_scopes,
                   cv.foundry_version as cv_foundry_version,
                   cv.pricing_config as cv_pricing_config,
                   cv.change_summary as cv_change_summary, cv.status as cv_status,
                   cv.submitted_by_user_id as cv_submitted_by_user_id,
                   cv.approved_by_user_id as cv_approved_by_user_id,
                   cv.approved_at as cv_approved_at, cv.rejection_reason as cv_rejection_reason,
                   cv.deleted_at as cv_deleted_at, cv.created_at as cv_created_at
            FROM sub_agents sa
            JOIN users u ON sa.owner_user_id = u.id
            LEFT JOIN sub_agent_config_versions cv 
                ON sa.id = cv.sub_agent_id 
                AND cv.version = COALESCE(:version, sa.current_version)
            WHERE sa.id = :id
        """)
        result = await db.execute(query, {"id": sub_agent_id, "version": version})
        row = result.mappings().first()

        if not row:
            return None

        return self._row_to_sub_agent_with_version(row)

    async def get_sub_agent_by_config_version_id(
        self,
        db: AsyncSession,
        config_version_id: int,
    ) -> SubAgent | None:
        """Get a sub-agent by config version ID.

        Used by the orchestrator to fetch a specific version for testing.
        Returns the sub-agent with the specified version's data in config_version.
        """
        query = text("""
            SELECT sa.id, sa.name, sa.owner_user_id, sa.owner_status, sa.type,
                   sa.current_version, sa.default_version, sa.is_public, sa.deleted_at,
                   sa.created_at, sa.updated_at,
                   u.email as owner_email, u.first_name, u.last_name,
                   cv.id as cv_id, cv.version as cv_version,
                   cv.version_hash as cv_version_hash, cv.release_number as cv_release_number,
                   cv.description as cv_description,
                   cv.model as cv_model, cv.system_prompt as cv_system_prompt,
                   cv.enable_thinking as cv_enable_thinking,
                   cv.thinking_level as cv_thinking_level,
                   cv.agent_url as cv_agent_url, cv.mcp_tools as cv_mcp_tools,
                   cv.foundry_hostname as cv_foundry_hostname,
                   cv.foundry_client_id as cv_foundry_client_id,
                   cv.foundry_client_secret_ref as cv_foundry_client_secret_ref,
                   cv.foundry_ontology_rid as cv_foundry_ontology_rid,
                   cv.foundry_query_api_name as cv_foundry_query_api_name,
                   cv.foundry_scopes as cv_foundry_scopes,
                   cv.foundry_version as cv_foundry_version,
                   cv.pricing_config as cv_pricing_config,
                   cv.change_summary as cv_change_summary, cv.status as cv_status,
                   cv.submitted_by_user_id as cv_submitted_by_user_id,
                   cv.approved_by_user_id as cv_approved_by_user_id,
                   cv.approved_at as cv_approved_at, cv.rejection_reason as cv_rejection_reason,
                   cv.deleted_at as cv_deleted_at, cv.created_at as cv_created_at
            FROM sub_agent_config_versions cv
            JOIN sub_agents sa ON cv.sub_agent_id = sa.id
            JOIN users u ON sa.owner_user_id = u.id
            WHERE cv.id = :config_version_id
        """)
        result = await db.execute(query, {"config_version_id": config_version_id})
        row = result.mappings().first()

        if not row:
            return None

        return self._row_to_sub_agent_with_version(row)

    async def get_sub_agent_by_version_hash(
        self,
        db: AsyncSession,
        version_hash: str,
    ) -> SubAgent | None:
        """Get a sub-agent by config version hash.

        Used for playground mode to fetch a specific version by its hash.
        Returns the sub-agent with the specified version's data.
        """
        query = text("""
            SELECT sa.id, sa.name, sa.owner_user_id, sa.owner_status, sa.type,
                   sa.current_version, sa.default_version, sa.is_public, sa.deleted_at,
                   sa.created_at, sa.updated_at,
                   u.email as owner_email, u.first_name, u.last_name,
                   cv.id as cv_id, cv.version as cv_version,
                   cv.version_hash as cv_version_hash, cv.release_number as cv_release_number,
                   cv.description as cv_description,
                   cv.model as cv_model, cv.system_prompt as cv_system_prompt,
                   cv.enable_thinking as cv_enable_thinking,
                   cv.thinking_level as cv_thinking_level,
                   cv.agent_url as cv_agent_url, cv.mcp_tools as cv_mcp_tools,
                   cv.foundry_hostname as cv_foundry_hostname,
                   cv.foundry_client_id as cv_foundry_client_id,
                   s.ssm_parameter_name as cv_foundry_client_secret_ssmkey,  -- needed for the orchestrator
                   cv.foundry_ontology_rid as cv_foundry_ontology_rid,
                   cv.foundry_query_api_name as cv_foundry_query_api_name,
                   cv.foundry_scopes as cv_foundry_scopes,
                   cv.foundry_version as cv_foundry_version,
                   cv.pricing_config as cv_pricing_config,
                   cv.change_summary as cv_change_summary, cv.status as cv_status,
                   cv.submitted_by_user_id as cv_submitted_by_user_id,
                   cv.approved_by_user_id as cv_approved_by_user_id,
                   cv.approved_at as cv_approved_at, cv.rejection_reason as cv_rejection_reason,
                   cv.deleted_at as cv_deleted_at, cv.created_at as cv_created_at
            FROM sub_agent_config_versions cv
            JOIN sub_agents sa ON cv.sub_agent_id = sa.id
            JOIN users u ON sa.owner_user_id = u.id
            LEFT JOIN secrets s ON cv.foundry_client_secret_ref = s.id
            WHERE cv.version_hash = :version_hash AND cv.deleted_at IS NULL
        """)
        result = await db.execute(query, {"version_hash": version_hash})
        row = result.mappings().first()

        if not row:
            return None

        return self._row_to_sub_agent_with_version(row)

    async def get_config_versions(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        include_deleted: bool = False,
    ) -> list[SubAgentConfigVersion]:
        """Get all configuration versions for a sub-agent.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID
            include_deleted: If True, include soft-deleted versions
        """
        if include_deleted:
            query = text("""
                SELECT cv.id, cv.sub_agent_id, cv.version, cv.version_hash, cv.release_number,
                       cv.description, cv.model, cv.system_prompt, cv.agent_url, cv.mcp_tools, 
                       cv.foundry_hostname, cv.foundry_client_id, cv.foundry_client_secret_ref, 
                       s.ssm_parameter_name as foundry_client_secret_ssmkey,
                       cv.foundry_ontology_rid, cv.foundry_query_api_name, cv.foundry_scopes, cv.foundry_version,
                       cv.pricing_config, cv.enable_thinking, cv.thinking_level,
                       cv.change_summary, cv.status, 
                       cv.submitted_by_user_id,
                       cv.approved_by_user_id, cv.approved_at, cv.rejection_reason, cv.deleted_at, cv.created_at
                FROM sub_agent_config_versions cv
                LEFT JOIN secrets s ON cv.foundry_client_secret_ref = s.id
                WHERE cv.sub_agent_id = :sub_agent_id
                ORDER BY cv.version DESC
            """)
        else:
            query = text("""
                SELECT cv.id, cv.sub_agent_id, cv.version, cv.version_hash, cv.release_number,
                       cv.description, cv.model, cv.system_prompt, cv.agent_url, cv.mcp_tools, 
                       cv.foundry_hostname, cv.foundry_client_id, cv.foundry_client_secret_ref, 
                       s.ssm_parameter_name as foundry_client_secret_ssmkey,
                       cv.foundry_ontology_rid, cv.foundry_query_api_name, cv.foundry_scopes, cv.foundry_version,
                       cv.pricing_config, cv.enable_thinking, cv.thinking_level,
                       cv.change_summary, cv.status, 
                       cv.submitted_by_user_id,
                       cv.approved_by_user_id, cv.approved_at, cv.rejection_reason, cv.deleted_at, cv.created_at
                FROM sub_agent_config_versions cv
                LEFT JOIN secrets s ON cv.foundry_client_secret_ref = s.id
                WHERE cv.sub_agent_id = :sub_agent_id AND cv.deleted_at IS NULL
                ORDER BY cv.version DESC
            """)
        result = await db.execute(query, {"sub_agent_id": sub_agent_id})
        rows = result.mappings().all()
        return [self._row_to_config_version(row) for row in rows]

    async def create_sub_agent(
        self,
        db: AsyncSession,
        data: SubAgentCreate,
        actor: User,
    ) -> SubAgent:
        """Create a new sub-agent with initial version."""
        now = datetime.now(timezone.utc)

        # For AUTOMATED agents, validate constraints and enforce private visibility
        if data.type == SubAgentType.AUTOMATED:
            _validate_automated_constraints(data.system_prompt, data.mcp_tools, data.is_public)

        # For Foundry agents, validate that all required fields are provided
        if data.type == SubAgentType.FOUNDRY:
            missing_fields = []
            if not data.foundry_hostname:
                missing_fields.append("foundry_hostname")
            if not data.foundry_client_id:
                missing_fields.append("foundry_client_id")
            if not data.foundry_client_secret_ref:
                missing_fields.append("foundry_client_secret_ref")
            if not data.foundry_ontology_rid:
                missing_fields.append("foundry_ontology_rid")
            if not data.foundry_query_api_name:
                missing_fields.append("foundry_query_api_name")
            if not data.foundry_scopes:
                missing_fields.append("foundry_scopes")

            if missing_fields:
                raise ValueError(
                    f"Missing required Foundry fields: {', '.join(missing_fields)}. "
                    "All Foundry configuration fields must be provided."
                )

        # Insert sub-agent with automatic audit
        sub_agent_id = await self.repo.create(
            db=db,
            actor=actor,
            fields={
                "name": data.name,
                "owner_user_id": actor.id,
                "type": data.type.value,
                "is_public": data.is_public,
                "current_version": 1,
                "created_at": now,
                "updated_at": now,
            },
            returning="id",
        )

        # Normalize Extended Thinking configuration based on model support
        normalized_enable_thinking, normalized_thinking_level = _normalize_thinking_config(
            data.model,
            data.enable_thinking,
            data.thinking_level,
        )

        # Create initial version with all configuration data
        await self._create_config_version(
            db,
            actor,
            sub_agent_id,
            1,
            "Initial version",
            description=data.description,
            model=data.model,
            system_prompt=data.system_prompt,
            agent_url=data.agent_url,
            mcp_tools=data.mcp_tools,
            foundry_hostname=data.foundry_hostname,
            foundry_client_id=data.foundry_client_id,
            foundry_client_secret_ref=data.foundry_client_secret_ref,
            foundry_ontology_rid=data.foundry_ontology_rid,
            foundry_query_api_name=data.foundry_query_api_name,
            foundry_scopes=[s.value for s in data.foundry_scopes] if data.foundry_scopes else None,
            foundry_version=data.foundry_version,
            pricing_config=data.pricing_config,
            enable_thinking=normalized_enable_thinking,
            thinking_level=normalized_thinking_level,
        )
        await db.commit()

        # Auto-approve logic:
        # - AUTOMATED agents: always auto-approved (constraints already validated above)
        # - All other types: auto-approve if constraints happen to be met
        #   (system_prompt <= max chars, <= max MCP tools, private)
        should_auto_approve = data.type == SubAgentType.AUTOMATED or _meets_auto_approve_constraints(
            data.type,
            data.system_prompt,
            data.mcp_tools,
            data.is_public,
        )
        if should_auto_approve:
            approval_ctx = ApprovalContext(
                sub_agent_id=sub_agent_id,
                version=1,
                action="approve",
                release_number=1,
            )
            await self.repo.approve_version(db, actor, approval_ctx)
            await db.commit()

        return await self.get_sub_agent_by_id(db, sub_agent_id)  # type: ignore

    async def update_sub_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        data: SubAgentUpdate,
        actor: User,
    ) -> SubAgent | None:
        """Update a sub-agent.

        Users with write access can update (owner or group members with write/manager role).

        - Name updates go to sub_agents table
        - Configuration changes (description, model, config) create a new version
        """
        existing = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not existing:
            return None

        # Check if user has write permission (owner or group write access)
        has_write_permission = await self.check_user_permission(db, sub_agent_id, actor.id, "write", sub_agent=existing)
        if not has_write_permission:
            raise PermissionError("You don't have permission to update this sub-agent")

        now = datetime.now(timezone.utc)

        # Update name and/or is_public on sub_agents table if provided
        updates = {}

        if data.name is not None:
            updates["name"] = data.name

        if data.is_public is not None:
            updates["is_public"] = data.is_public

        if updates:
            updates["updated_at"] = now
            await self.repo.update_sub_agent(
                db=db,
                actor=actor,
                sub_agent_id=sub_agent_id,
                fields=updates,
            )

        # Check if we need a new version (any config-related changes)
        needs_new_version = (
            data.system_prompt is not None
            or data.agent_url is not None
            or data.description is not None
            or data.model is not None
            or data.mcp_tools is not None
            or data.foundry_hostname is not None
            or data.foundry_client_id is not None
            or data.foundry_client_secret_ref is not None
            or data.foundry_ontology_rid is not None
            or data.foundry_query_api_name is not None
            or data.foundry_scopes is not None
            or data.pricing_config is not None
            or data.thinking_level is not None
            or data.enable_thinking is not None
        )

        if needs_new_version:
            # For Foundry agents, ensure secret reference is available
            if existing.type == SubAgentType.FOUNDRY:
                if data.foundry_client_secret_ref is not None:
                    # User is updating the secret reference, use the new one
                    foundry_client_secret_ref = data.foundry_client_secret_ref
                elif existing.config_version and existing.config_version.foundry_client_secret_ref is not None:
                    # Keep the existing secret reference
                    foundry_client_secret_ref = existing.config_version.foundry_client_secret_ref
                else:
                    # No secret reference available - this is an error
                    raise ValueError(
                        "foundry_client_secret_ref is required for Foundry agents. "
                        "Please provide a valid secret reference."
                    )
            else:
                foundry_client_secret_ref = None

            # Get the actual maximum version from database to avoid conflicts
            max_version_result = await db.execute(
                text("""
                    SELECT COALESCE(MAX(version), 0) FROM sub_agent_config_versions
                    WHERE sub_agent_id = :sub_agent_id
                """),
                {"sub_agent_id": sub_agent_id},
            )
            new_version = max_version_result.scalar_one() + 1

            # Get current version values to use as defaults
            current_config = existing.config_version

            # For local/automated agents, only use system_prompt. For remote agents, only use agent_url. For Foundry agents, use foundry_* fields.
            # This ensures we don't violate the CHECK constraint.
            if existing.type in (SubAgentType.LOCAL, SubAgentType.AUTOMATED):
                version_system_prompt = (
                    data.system_prompt
                    if data.system_prompt is not None
                    else (current_config.system_prompt if current_config else None)
                )
                version_model = (
                    data.model if data.model is not None else (current_config.model if current_config else None)
                )
                version_mcp_tools = (
                    data.mcp_tools
                    if data.mcp_tools is not None
                    else (current_config.mcp_tools if current_config else [])
                )
                version_thinking_level = (
                    data.thinking_level
                    if data.thinking_level is not None
                    else (current_config.thinking_level if current_config else None)
                )
                version_enable_thinking = (
                    data.enable_thinking
                    if data.enable_thinking is not None
                    else (current_config.enable_thinking if current_config else None)
                )
                version_agent_url = None
                version_foundry_hostname = None
                version_foundry_client_id = None
                version_foundry_client_secret_ref = None
                version_foundry_ontology_rid = None
                version_foundry_query_api_name = None
                version_foundry_scopes = None
                version_foundry_version = None
                version_pricing_config = None
            elif existing.type == SubAgentType.REMOTE:
                version_agent_url = (
                    data.agent_url
                    if data.agent_url is not None
                    else (current_config.agent_url if current_config else None)
                )

                version_system_prompt = None
                version_model = None
                version_thinking_level = None
                version_enable_thinking = None
                version_mcp_tools = None

                version_foundry_hostname = None
                version_foundry_client_id = None
                version_foundry_client_secret_ref = None
                version_foundry_ontology_rid = None
                version_foundry_query_api_name = None
                version_foundry_scopes = None
                version_foundry_version = None

                version_pricing_config = (
                    data.pricing_config
                    if data.pricing_config is not None
                    else (current_config.pricing_config if current_config else None)
                )

            else:  # foundry
                version_agent_url = None

                version_system_prompt = None
                version_model = None
                version_thinking_level = None
                version_enable_thinking = None
                version_mcp_tools = None

                # For Foundry agents, gather all required fields with fallback to current config
                version_foundry_hostname = (
                    data.foundry_hostname
                    if data.foundry_hostname is not None
                    else (current_config.foundry_hostname if current_config else None)
                )
                version_foundry_client_id = (
                    data.foundry_client_id
                    if data.foundry_client_id is not None
                    else (current_config.foundry_client_id if current_config else None)
                )
                # foundry_client_secret_ref was already set above with proper validation
                version_foundry_client_secret_ref = foundry_client_secret_ref

                version_foundry_ontology_rid = (
                    data.foundry_ontology_rid
                    if data.foundry_ontology_rid is not None
                    else (current_config.foundry_ontology_rid if current_config else None)
                )
                version_foundry_query_api_name = (
                    data.foundry_query_api_name
                    if data.foundry_query_api_name is not None
                    else (current_config.foundry_query_api_name if current_config else None)
                )
                version_foundry_scopes = (
                    [s.value for s in data.foundry_scopes]
                    if data.foundry_scopes is not None
                    else (current_config.foundry_scopes if current_config else None)
                )
                version_foundry_version = (
                    data.foundry_version
                    if data.foundry_version is not None
                    else (current_config.foundry_version if current_config else None)
                )

                version_pricing_config = (
                    data.pricing_config
                    if data.pricing_config is not None
                    else (current_config.pricing_config if current_config else None)
                )

                # Validate all required Foundry fields are present (per DB constraint)
                missing_fields = []
                if version_foundry_hostname is None:
                    missing_fields.append("foundry_hostname")
                if version_foundry_client_id is None:
                    missing_fields.append("foundry_client_id")
                if version_foundry_client_secret_ref is None:
                    missing_fields.append("foundry_client_secret_ref")
                if version_foundry_ontology_rid is None:
                    missing_fields.append("foundry_ontology_rid")
                if version_foundry_query_api_name is None:
                    missing_fields.append("foundry_query_api_name")
                if version_foundry_scopes is None:
                    missing_fields.append("foundry_scopes")

                if missing_fields:
                    raise ValueError(
                        f"Missing required Foundry fields: {', '.join(missing_fields)}. "
                        "All Foundry configuration fields must be provided."
                    )

            # For AUTOMATED agents, validate constraints
            if existing.type == SubAgentType.AUTOMATED:
                is_public = data.is_public if data.is_public is not None else existing.is_public
                _validate_automated_constraints(version_system_prompt, version_mcp_tools, is_public)

            version_description = (
                data.description
                if data.description is not None
                else (current_config.description if current_config else "")
            )

            # Normalize Extended Thinking configuration based on model support
            version_enable_thinking, version_thinking_level = _normalize_thinking_config(
                version_model,
                version_enable_thinking,
                version_thinking_level,
            )

            # Create new version
            await self._create_config_version(
                db,
                actor,
                sub_agent_id,
                new_version,
                data.change_summary or f"Updated to version {new_version}",
                description=version_description,
                model=version_model,
                system_prompt=version_system_prompt,
                agent_url=version_agent_url,
                mcp_tools=version_mcp_tools,
                foundry_hostname=version_foundry_hostname,
                foundry_client_id=version_foundry_client_id,
                foundry_client_secret_ref=version_foundry_client_secret_ref,
                foundry_ontology_rid=version_foundry_ontology_rid,
                foundry_query_api_name=version_foundry_query_api_name,
                foundry_scopes=version_foundry_scopes,
                foundry_version=version_foundry_version,
                pricing_config=version_pricing_config,
                enable_thinking=version_enable_thinking,
                thinking_level=version_thinking_level,
            )

            # Update current_version pointer
            await self.repo.update_current_version(
                db=db,
                actor=actor,
                sub_agent_id=sub_agent_id,
                version=new_version,
            )

            # Auto-approve AUTOMATED agents or LOCAL agents that meet the constraints (auto-approve config)
            if existing.type == SubAgentType.AUTOMATED or _meets_auto_approve_constraints(
                existing.type,
                version_system_prompt,
                version_mcp_tools,
                data.is_public if data.is_public is not None else existing.is_public,
            ):
                # Get the release number for this version
                result = await db.execute(
                    text("""
                        SELECT COALESCE(MAX(release_number), 0) + 1
                        FROM sub_agent_config_versions
                        WHERE sub_agent_id = :sub_agent_id AND status = 'approved'
                    """),
                    {"sub_agent_id": sub_agent_id},
                )
                next_release_number = result.scalar_one()

                approval_ctx = ApprovalContext(
                    sub_agent_id=sub_agent_id,
                    version=new_version,
                    action="approve",
                    release_number=next_release_number,
                )
                await self.repo.approve_version(db, actor, approval_ctx)

        await db.commit()
        return await self.get_sub_agent_by_id(db, sub_agent_id)

    async def delete_sub_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        actor: User,
    ) -> bool:
        """Delete a sub-agent."""
        existing = await self.get_sub_agent_by_id(db, sub_agent_id)
        can_delete = self.check_user_permission(
            db, sub_agent_id, actor.id, required_permission="write", sub_agent=existing
        )
        if not can_delete:
            raise PermissionError("You don't have permission to delete this sub-agent")

        if not existing:
            return False

        # Soft delete with automatic audit
        await self.repo.delete(db=db, actor=actor, entity_id=sub_agent_id, soft=True)
        await db.commit()
        return True

    async def delete_version(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int,
        actor: User,
    ) -> bool:
        """Soft-delete a specific version.

        Only draft, pending_approval, or rejected versions can be deleted.
        Approved versions cannot be deleted to preserve history.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID
            version: The version number to delete
            actor: The user requesting deletion

        Returns:
            True if deleted, False if version not found

        Raises:
            PermissionError: If user is not the owner
            ValueError: If version is approved (cannot delete approved versions)
        """
        existing = await self.get_sub_agent_by_id(db, sub_agent_id, version=version)
        can_delete = self.check_user_permission(
            db, sub_agent_id, actor.id, required_permission="write", sub_agent=existing
        )
        if not can_delete:
            raise PermissionError("You don't have permission to delete this version")
        if not existing:
            return False

        if existing.owner_user_id != actor.id:
            raise PermissionError("Only the owner can delete versions")

        if not existing.config_version:
            return False

        if existing.config_version.status == SubAgentStatus.APPROVED:
            raise ValueError("Cannot delete approved versions - they are part of the release history")

        # Check if this is the current version - need to handle this case
        if existing.current_version == version:
            # Find the previous non-deleted version to set as current
            prev_result = await db.execute(
                text("""
                    SELECT MAX(version) FROM sub_agent_config_versions
                    WHERE sub_agent_id = :sub_agent_id 
                    AND version < :version 
                    AND deleted_at IS NULL
                """),
                {"sub_agent_id": sub_agent_id, "version": version},
            )
            prev_version = prev_result.scalar()

            if prev_version is None:
                raise ValueError("Cannot delete the only version - delete the entire sub-agent instead")

        # Soft delete the version using repository
        await self.repo.delete_version(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            version=version,
        )

        # If this was the current version, update to previous version
        if existing.current_version == version:
            await self.repo.update_current_version_to_previous(
                db=db,
                actor=actor,
                sub_agent_id=sub_agent_id,
            )
        else:
            await self.repo.update_sub_agent_timestamp(
                db=db,
                actor=actor,
                sub_agent_id=sub_agent_id,
            )

        await db.commit()
        return True

    async def submit_for_approval(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        change_summary: str,
        actor: User,
    ) -> SubAgent | None:
        """Submit the current version for approval.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID
            change_summary: Required description of changes in this version
            actor: The user submitting for approval
        """
        existing = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not existing:
            return None

        # Automated sub-agents are auto-approved on creation; manual submission is not allowed
        if existing.type == SubAgentType.AUTOMATED:
            raise ValueError("Automated sub-agents cannot be submitted for approval manually.")

        # Check if user has write permission (owner or group write access)
        has_write_permission = await self.check_user_permission(db, sub_agent_id, actor.id, "write", sub_agent=existing)
        if not has_write_permission:
            raise PermissionError("You don't have permission to submit this sub-agent for approval")

        if not existing.config_version:
            raise ValueError("No version to submit")

        current_status = existing.config_version.status
        if current_status not in (SubAgentStatus.DRAFT, SubAgentStatus.REJECTED):
            raise ValueError("Only draft or rejected versions can be submitted for approval")

        # Update the current version status and change_summary
        await self.repo.submit_version_for_approval(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            version=existing.current_version,
            change_summary=change_summary,
        )

        # Update sub_agents.updated_at
        await self.repo.update_sub_agent_timestamp(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
        )

        await db.commit()

        # Notify eligible approvers (exclude the submitter)
        if self.notification_service:
            try:
                approver_ids = await self._get_eligible_approvers_for_sub_agent(
                    db, sub_agent_id, exclude_user_id=actor.id
                )
                if approver_ids:
                    agent_name = existing.name
                    notifications = [
                        NotificationData(
                            user_id=approver_id,
                            notification_type=NotificationType.APPROVAL_REQUESTED,
                            title=f"Approval requested: '{agent_name}'",
                            message=f"Version {existing.current_version} of agent '{agent_name}' has been submitted for approval.",
                            metadata={
                                "sub_agent_id": sub_agent_id,
                                "version": existing.current_version,
                                "submitted_by": actor.id,
                            },
                        )
                        for approver_id in approver_ids
                    ]
                    await self.notification_service.bulk_create_notifications(db, notifications)
            except Exception as e:
                logger.error(f"Failed to create approval request notifications: {e}")

        return await self.get_sub_agent_by_id(db, sub_agent_id)

    async def approve_sub_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        actor: User,
        approve: bool,
        rejection_reason: str | None = None,
    ) -> SubAgent | None:
        """Approve or reject a sub-agent's current version.

        Security: This method validates that the user has approval capabilities
        per SYSTEM_ROLE_CAPABILITIES at the service level, providing defense-in-depth
        beyond router-level checks.
        """
        # Defense in depth: Verify approval capabilities at service level
        admin_check = await db.execute(
            text("SELECT is_administrator, sub, role FROM users WHERE id = :user_id"), {"user_id": actor.id}
        )
        admin_row = admin_check.first()

        if not admin_row:
            logger.error(f"SECURITY: approve_sub_agent called with non-existent user {actor.id}")
            raise PermissionError("User not found")

        is_administrator = admin_row[0]
        user_role = admin_row[2]

        # Check if user has approval capabilities per SYSTEM_ROLE_CAPABILITIES
        # System admins have all capabilities
        # Role-based: check if 'approve' or 'approve.admin' is in their role's sub_agents capabilities

        can_approve = is_administrator
        has_approve_admin = False
        if not can_approve and user_role in SYSTEM_ROLE_CAPABILITIES:
            sub_agents_capabilities = SYSTEM_ROLE_CAPABILITIES.get(user_role, {}).get("sub_agents", set())
            has_approve_admin = "approve.admin" in sub_agents_capabilities
            can_approve = "approve" in sub_agents_capabilities or has_approve_admin

        if not can_approve:
            logger.error(
                f"SECURITY: approve_sub_agent called with user {actor.id} without approval capabilities (is_admin={is_administrator}, role={user_role})"
            )
            raise PermissionError(
                "Approval requires 'approve' or 'approve.admin' capability per SYSTEM_ROLE_CAPABILITIES"
            )

        # Defense in depth: For non-admin 'approve' action, verify user has group-based access to the resource
        # 'approve.admin' bypasses intersection check, but plain 'approve' requires group access
        existing = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not is_administrator and not has_approve_admin:
            has_access = await self.check_user_permission(db, sub_agent_id, actor.id, "read", sub_agent=existing)
            if not has_access:
                logger.error(
                    f"SECURITY: approve_sub_agent called by user {actor.id} without group access to sub_agent {sub_agent_id}"
                )
                raise PermissionError("Approval with 'approve' capability requires group-based access to the sub-agent")

        if not existing:
            return None

        if not existing.config_version:
            raise ValueError("No version to approve")

        if existing.config_version.status != SubAgentStatus.PENDING_APPROVAL:
            raise ValueError("Only pending versions can be approved/rejected")

        result = await self.approve_version(
            db=db,
            sub_agent_id=sub_agent_id,
            version=existing.current_version,
            actor=actor,
            approve=approve,
            rejection_reason=rejection_reason,
        )

        # Audit logging is handled in approve_version
        return result

    async def approve_version(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int,
        approve: bool,
        actor: User,
        rejection_reason: str | None = None,
    ) -> SubAgent | None:
        """Approve or reject a specific version.

        Security: This method validates that the user has approval capabilities
        per SYSTEM_ROLE_CAPABILITIES at the service level, providing defense-in-depth
        beyond router-level checks. All approval/rejection operations are logged in the audit system.
        """
        # Defense in depth: Verify approval capabilities at service level
        admin_check = await db.execute(
            text("SELECT is_administrator, role FROM users WHERE id = :user_id"), {"user_id": actor.id}
        )
        admin_row = admin_check.first()

        if not admin_row:
            logger.error(f"SECURITY: approve_version called with non-existent user {actor.id}")
            raise PermissionError("User not found")

        is_administrator = admin_row[0]
        user_role = admin_row[1]

        # Check if user has approval capabilities per SYSTEM_ROLE_CAPABILITIES
        # System admins have all capabilities
        # Role-based: check if 'approve' or 'approve.admin' is in their role's sub_agents capabilities
        can_approve = is_administrator
        has_approve_admin = False
        if not can_approve and user_role in SYSTEM_ROLE_CAPABILITIES:
            sub_agents_capabilities = SYSTEM_ROLE_CAPABILITIES.get(user_role, {}).get("sub_agents", set())
            has_approve_admin = "approve.admin" in sub_agents_capabilities
            can_approve = "approve" in sub_agents_capabilities or has_approve_admin

        if not can_approve:
            logger.error(
                f"SECURITY: approve_version called with user {actor.id} without approval capabilities (is_admin={is_administrator}, role={user_role})"
            )
            raise PermissionError(
                "Approval requires 'approve' or 'approve.admin' capability per SYSTEM_ROLE_CAPABILITIES"
            )
        existing = await self.get_sub_agent_by_id(db, sub_agent_id, version=version)

        # Defense in depth: For non-admin 'approve' action, verify user has group-based access to the resource
        # 'approve.admin' bypasses intersection check, but plain 'approve' requires group access
        if not is_administrator and not has_approve_admin:
            has_access = await self.check_user_permission(db, sub_agent_id, actor.id, "read", sub_agent=existing)
            if not has_access:
                logger.error(
                    f"SECURITY: approve_version called by user {actor.id} without group access to sub_agent {sub_agent_id}"
                )
                raise PermissionError("Approval with 'approve' capability requires group-based access to the sub-agent")

        if not existing:
            return None

        if not existing.config_version:
            raise ValueError(f"Version {version} not found")

        if existing.config_version.status != SubAgentStatus.PENDING_APPROVAL:
            raise ValueError("Only pending versions can be approved/rejected")

        # === VALIDATION LAYER ===
        # Verify version exists and is in correct state
        status, owner_id = await self.repo.get_version_status(db, sub_agent_id, version)

        if status != SubAgentStatus.PENDING_APPROVAL.value:
            raise ValueError("Only pending versions can be approved/rejected")

        # === DATA LAYER ===
        # Build approval context
        context = ApprovalContext(
            sub_agent_id=sub_agent_id,
            version=version,
            action="approve" if approve else "reject",
            rejection_reason=rejection_reason,
        )

        if approve:
            # Get next release number
            context.release_number = await self.repo.get_next_release_number(db, sub_agent_id)
            # Execute approval (includes audit)
            await self.repo.approve_version(db, actor, context)

            # Notify owner and submitter about approval
            if self.notification_service:
                try:
                    agent_name = existing.name

                    # Get the person who submitted this version for approval
                    submitter_id = existing.config_version.submitted_by_user_id

                    # Collect unique user IDs to notify (exclude the approver)
                    notify_user_ids = set()
                    if owner_id and owner_id != actor.id:
                        notify_user_ids.add(owner_id)
                    if submitter_id and submitter_id != actor.id:
                        notify_user_ids.add(submitter_id)

                    if notify_user_ids:
                        notifications = [
                            NotificationData(
                                user_id=user_id,
                                notification_type=NotificationType.APPROVAL_COMPLETED,
                                title=f"Agent '{agent_name}' approved",
                                message=f"Version {version} of agent '{agent_name}' has been approved and is now available.",
                                metadata={
                                    "sub_agent_id": sub_agent_id,
                                    "version": version,
                                    "release_number": context.release_number,
                                },
                            )
                            for user_id in notify_user_ids
                        ]
                        await self.notification_service.bulk_create_notifications(db, notifications)
                except Exception as e:
                    logger.error(f"Failed to create approval notification: {e}")
        else:
            # Execute rejection (includes audit)
            await self.repo.reject_version(db, context, actor=actor)

            # Notify owner and submitter about rejection
            if self.notification_service:
                try:
                    agent_name = existing.name

                    # Get the person who submitted this version for approval
                    submitter_id = existing.config_version.submitted_by_user_id

                    # Collect unique user IDs to notify (exclude the approver)
                    notify_user_ids = set()
                    if owner_id and owner_id != actor.id:
                        notify_user_ids.add(owner_id)
                    if submitter_id and submitter_id != actor.id:
                        notify_user_ids.add(submitter_id)

                    if notify_user_ids:
                        notifications = [
                            NotificationData(
                                user_id=user_id,
                                notification_type=NotificationType.APPROVAL_REJECTED,
                                title=f"Agent '{agent_name}' rejected",
                                message=f"Version {version} of agent '{agent_name}' was rejected."
                                + (f" Reason: {rejection_reason}" if rejection_reason else ""),
                                metadata={
                                    "sub_agent_id": sub_agent_id,
                                    "version": version,
                                    "rejection_reason": rejection_reason,
                                },
                            )
                            for user_id in notify_user_ids
                        ]
                        await self.notification_service.bulk_create_notifications(db, notifications)
                except Exception as e:
                    logger.error(f"Failed to create rejection notification: {e}")

        await db.commit()

        # After approval, activate for all groups that have this agent as a default
        if approve:
            try:
                await self._activate_for_default_agent_groups(db, sub_agent_id, actor=actor)
            except Exception as e:
                logger.error(f"Failed to activate agent for default groups: {e}")
                # Don't fail the approval - activation can be retried

            # Also auto-activate for the owner
            try:
                await self._activate_for_owner(db, sub_agent_id, owner_id, actor=actor)
            except Exception as e:
                logger.error(f"Failed to auto-activate agent for owner: {e}")
                # Don't fail the approval - owner can activate manually

        return await self.get_sub_agent_by_id(db, sub_agent_id)

    async def _get_group_members(self, db: AsyncSession, group_id: int) -> list[str]:
        """Get all user IDs for members of a group.

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            List of user IDs
        """
        members_query = text("""
            SELECT user_id FROM user_group_members
            WHERE user_group_id = :group_id
        """)
        result = await db.execute(members_query, {"group_id": group_id})
        return [row[0] for row in result.fetchall()]

    async def _get_members_from_groups(self, db: AsyncSession, group_ids: list[int]) -> list[str]:
        """Get all distinct user IDs from multiple groups.

        Args:
            db: Database session
            group_ids: List of group IDs

        Returns:
            List of distinct user IDs
        """
        if not group_ids:
            return []

        members_query = text("""
            SELECT DISTINCT user_id
            FROM user_group_members
            WHERE user_group_id = ANY(:group_ids)
        """)
        result = await db.execute(members_query, {"group_ids": group_ids})
        return [row[0] for row in result.fetchall()]

    async def _get_eligible_approvers_for_sub_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        exclude_user_id: str | None = None,
    ) -> list[str]:
        """Get list of user IDs who can approve the sub-agent.

        Returns approvers and admins who have access to the sub-agent through:
        - Being the owner
        - Having group access with write/manager role
        - System admins (with approve.admin capability)

        Args:
            db: Database session
            sub_agent_id: Sub-agent ID
            exclude_user_id: Optional user ID to exclude (e.g., the submitter)

        Returns:
            List of user IDs eligible to approve
        """
        query = text("""
            SELECT DISTINCT u.id
            FROM users u
            WHERE 
                -- User has approver or admin role
                (u.role IN ('approver', 'admin') OR u.is_administrator = true)
                AND (
                    -- User is the owner
                    EXISTS (
                        SELECT 1 FROM sub_agents sa
                        WHERE sa.id = :sub_agent_id AND sa.owner_user_id = u.id
                    )
                    OR
                    -- User has group access with write or manager role
                    EXISTS (
                        SELECT 1
                        FROM sub_agent_permissions sap
                        JOIN user_group_members ugm ON sap.user_group_id = ugm.user_group_id
                        WHERE sap.sub_agent_id = :sub_agent_id
                          AND ugm.user_id = u.id
                          AND ugm.group_role IN ('write', 'manager')
                          AND 'write' = ANY(sap.permissions)
                    )
                    OR
                    -- System admin (can approve anything with approve.admin)
                    u.is_administrator = true
                )
        """)
        result = await db.execute(query, {"sub_agent_id": sub_agent_id})
        user_ids = [row[0] for row in result.fetchall()]

        # Filter out excluded user
        if exclude_user_id:
            user_ids = [uid for uid in user_ids if uid != exclude_user_id]

        return user_ids

    async def _get_group_name(self, db: AsyncSession, group_id: int) -> str:
        """Get the name of a group.

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            Group name or fallback string
        """
        query = text("SELECT name FROM user_groups WHERE id = :group_id")
        result = await db.execute(query, {"group_id": group_id})
        return result.scalar() or f"Group {group_id}"

    async def _notify_agent_activation(
        self,
        db: AsyncSession,
        user_ids: list[str],
        agent_id: int,
        agent_name: str,
        group_id: int,
        group_name: str,
        reason: str = "default",
        affected_user_ids: list[str] | None = None,
    ) -> None:
        """Send activation notifications to users.

        Args:
            db: Database session
            user_ids: List of user IDs to notify
            agent_id: Sub-agent ID
            agent_name: Sub-agent name
            group_id: Group ID
            group_name: Group name
            reason: Reason for activation (default, approval, etc.)
            affected_user_ids: Optional list of user IDs whose state actually changed (filters user_ids)
        """
        if not self.notification_service or not user_ids:
            return

        from ..models.notification import NotificationData, NotificationType

        # Filter to only users whose state actually changed
        if affected_user_ids is not None:
            user_ids = [uid for uid in user_ids if uid in affected_user_ids]

        if not user_ids:
            return

        notifications = [
            NotificationData(
                user_id=user_id,
                notification_type=NotificationType.AGENT_ACTIVATED,
                title=f"Agent '{agent_name}' now available",
                message=f"The agent '{agent_name}' has been approved and is now active for the group '{group_name}'.",
                metadata={
                    "sub_agent_id": agent_id,
                    "group_id": group_id,
                    "reason": reason,
                },
            )
            for user_id in user_ids
        ]
        await self.notification_service.bulk_create_notifications(db, notifications)

    async def _activate_for_default_agent_groups(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        actor: User,
    ) -> None:
        """
        Internal helper to activate a newly-approved agent for all groups that have it as a default.

        This is called after approval to automatically activate agents for users in groups
        where the agent was set as a default while still non-approved.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID that was just approved
            actor: The admin user who approved (for audit trail)
        """
        # Find all groups that have this agent as a default
        groups_query = text("""
            SELECT DISTINCT user_group_id
            FROM user_group_default_agents
            WHERE sub_agent_id = :sub_agent_id
        """)
        groups_result = await db.execute(groups_query, {"sub_agent_id": sub_agent_id})
        group_ids = [row[0] for row in groups_result.fetchall()]

        if not group_ids:
            logger.info(f"No groups have agent {sub_agent_id} as default, skipping activation")
            return

        logger.info(f"Agent {sub_agent_id} is a default for {len(group_ids)} groups, activating for members")

        # Get agent name once for all notifications
        agent_names = await self.get_agent_names(db, [sub_agent_id])
        agent_name = agent_names.get(sub_agent_id, f"Agent {sub_agent_id}")

        # For each group, get all members and bulk activate
        for group_id in group_ids:
            try:
                # Get all members of the group
                member_user_ids = await self._get_group_members(db, group_id)
                if not member_user_ids:
                    continue

                # Bulk activate the agent for all members
                await self.repo.bulk_activate_sub_agent(
                    db=db,
                    actor=actor,
                    user_ids=member_user_ids,
                    sub_agent_id=sub_agent_id,
                    activated_by=ActivationSource.GROUP,
                    group_id=group_id,
                )

                # Get group name and notify members
                group_name = await self._get_group_name(db, group_id)
                await self._notify_agent_activation(
                    db=db,
                    user_ids=member_user_ids,
                    agent_id=sub_agent_id,
                    agent_name=agent_name,
                    group_id=group_id,
                    group_name=group_name,
                    reason="approval",
                )

                logger.info(
                    f"Activated agent {sub_agent_id} for {len(member_user_ids)} members of group {group_id} (approval trigger)"
                )

            except Exception as e:
                logger.error(f"Failed to activate agent {sub_agent_id} for group {group_id}: {e}")
                # Continue to next group - don't fail the entire operation

    async def _activate_for_owner(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        owner_id: str,
        actor: User,
    ) -> None:
        """
        Internal helper to auto-activate a newly-approved agent for its owner.

        This is called after approval to automatically activate the agent for the owner,
        just as we do for group members.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID that was just approved
            owner_id: The owner's user ID
            actor: The admin user who approved (for audit trail)
        """
        try:
            # Check if owner is already activated (e.g., from group membership)
            check_query = text("""
                SELECT 1 FROM user_sub_agent_activations
                WHERE user_id = :user_id AND sub_agent_id = :sub_agent_id
            """)
            result = await db.execute(check_query, {"user_id": owner_id, "sub_agent_id": sub_agent_id})
            if result.scalar_one_or_none():
                logger.info(f"Agent {sub_agent_id} already activated for owner {owner_id}, skipping")
                return

            # Bulk activate the agent for the owner (using USER activation source)
            affected_user_ids = await self.repo.bulk_activate_sub_agent(
                db=db,
                actor=actor,
                user_ids=[owner_id],
                sub_agent_id=sub_agent_id,
                activated_by=ActivationSource.USER,  # Owner self-activates
                group_id=None,
            )

            # Create notification for the owner if activation was successful
            agent_names = await self.get_agent_names(db, [sub_agent_id])
            agent_name = agent_names.get(sub_agent_id, f"Agent {sub_agent_id}")
            if affected_user_ids and self.notification_service:
                notification = NotificationData(
                    user_id=owner_id,
                    notification_type=NotificationType.AGENT_ACTIVATED,
                    title=f"Your agent '{agent_name}' is now active",
                    message=f"Your agent '{agent_name}' has been approved and automatically activated for you.",
                    metadata={
                        "sub_agent_id": sub_agent_id,
                        "reason": "owner_auto_activation",
                    },
                )
                await self.notification_service.bulk_create_notifications(db, [notification])

            logger.info(f"Auto-activated agent {sub_agent_id} for owner {owner_id}")

        except Exception as e:
            logger.error(f"Failed to auto-activate agent {sub_agent_id} for owner {owner_id}: {e}")
            # Don't fail the approval - activation can be done manually

    async def submit_version_for_approval(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int,
        change_summary: str,
        actor: User,
    ) -> SubAgent | None:
        """Submit a specific version for approval.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID
            version: The version number to submit
            change_summary: Required description of changes in this version
            actor: The user submitting

        """
        existing = await self.get_sub_agent_by_id(db, sub_agent_id, version=version)
        if not existing:
            return None

        # Check if user has write permission (owner or group write access)
        has_write_permission = await self.check_user_permission(db, sub_agent_id, actor.id, "write", sub_agent=existing)
        if not has_write_permission:
            raise PermissionError("You don't have permission to submit this sub-agent for approval")

        if not existing.config_version:
            raise ValueError(f"Version {version} not found")

        if existing.config_version.status not in (SubAgentStatus.DRAFT, SubAgentStatus.REJECTED):
            raise ValueError("Only draft or rejected versions can be submitted for approval")

        await self.repo.submit_version_for_approval(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            version=version,
            change_summary=change_summary,
        )

        await self.repo.update_sub_agent_timestamp(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
        )

        await db.commit()

        # Notify eligible approvers (exclude the submitter)
        if self.notification_service:
            try:
                approver_ids = await self._get_eligible_approvers_for_sub_agent(
                    db, sub_agent_id, exclude_user_id=actor.id
                )
                if approver_ids:
                    agent_name = existing.name
                    notifications = [
                        NotificationData(
                            user_id=approver_id,
                            notification_type=NotificationType.APPROVAL_REQUESTED,
                            title=f"Approval requested: '{agent_name}'",
                            message=f"Version {version} of agent '{agent_name}' has been submitted for approval.",
                            metadata={
                                "sub_agent_id": sub_agent_id,
                                "version": version,
                                "submitted_by": actor.id,
                            },
                        )
                        for approver_id in approver_ids
                    ]
                    await self.notification_service.bulk_create_notifications(db, notifications)
            except Exception as e:
                logger.error(f"Failed to create approval request notifications: {e}")

        return await self.get_sub_agent_by_id(db, sub_agent_id)

    async def set_default_version(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int,
        actor: User,
    ) -> SubAgent | None:
        """Set an approved version as the default version."""
        existing = await self.get_sub_agent_by_id(db, sub_agent_id, version=version)
        if not existing:
            return None

        if existing.owner_user_id != actor.id:
            raise PermissionError("Only the owner can set the default version")

        if not existing.config_version:
            raise ValueError(f"Version {version} not found")

        if existing.config_version.status != SubAgentStatus.APPROVED:
            raise ValueError("Only approved versions can be set as default")

        await self.repo.set_default_version(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            version=version,
        )

        await db.commit()
        return await self.get_sub_agent_by_id(db, sub_agent_id)

    async def revert_to_version(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        version: int,
        actor: User,
    ) -> SubAgent | None:
        """Revert to a previous version by creating a new version with its config."""
        existing = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not existing:
            return None

        if existing.owner_user_id != actor.id:
            raise PermissionError("Only the owner can revert versions")

        # Fetch the target version
        target = await self.get_sub_agent_by_id(db, sub_agent_id, version=version)
        if not target or not target.config_version:
            raise ValueError(f"Version {version} not found")

        # Create a new version with the reverted configuration
        new_version = existing.current_version + 1

        await self._create_config_version(
            db,
            actor,
            sub_agent_id,
            new_version,
            f"Reverted to version {version}",
            description=target.config_version.description,
            model=target.config_version.model,
            system_prompt=target.config_version.system_prompt,
            agent_url=target.config_version.agent_url,
            mcp_tools=target.config_version.mcp_tools,
            foundry_hostname=target.config_version.foundry_hostname,
            foundry_client_id=target.config_version.foundry_client_id,
            foundry_client_secret_ref=target.config_version.foundry_client_secret_ref,
            foundry_ontology_rid=target.config_version.foundry_ontology_rid,
            foundry_query_api_name=target.config_version.foundry_query_api_name,
            foundry_scopes=target.config_version.foundry_scopes,
            foundry_version=target.config_version.foundry_version,
            pricing_config=target.config_version.pricing_config,
            thinking_level=target.config_version.thinking_level,
            enable_thinking=target.config_version.enable_thinking,
        )

        await self.repo.update_current_version(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            version=new_version,
        )

        await db.commit()
        return await self.get_sub_agent_by_id(db, sub_agent_id)

    async def update_permissions(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        group_permissions: list[dict[str, Any]],
        actor: User,
    ) -> bool:
        """Update group permissions with read/write granularity.

        Args:
            db: Database session
            sub_agent_id: The sub-agent ID
            group_permissions: List of dicts with user_group_id and permissions array
            actor: User making the change

        Returns:
            True if successful, False if sub-agent not found
        """
        existing = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not existing:
            return False

        # Get current permissions to detect changes
        current_perms_query = text("""
            SELECT user_group_id, permissions FROM sub_agent_permissions
            WHERE sub_agent_id = :sub_agent_id
        """)
        current_result = await db.execute(current_perms_query, {"sub_agent_id": sub_agent_id})
        current_perms = {row[0]: set(row[1]) for row in current_result.fetchall()}

        # Detect added and removed groups
        new_perms = {p["user_group_id"]: set(p["permissions"]) for p in group_permissions}
        added_groups = set(new_perms.keys()) - set(current_perms.keys())
        removed_groups = set(current_perms.keys()) - set(new_perms.keys())
        changed_groups = {gid for gid in new_perms if gid in current_perms and new_perms[gid] != current_perms[gid]}

        # Use repository for update with automatic audit logging
        await self.repo.update_permissions(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            group_permissions=group_permissions,
        )

        # Commit the permission changes BEFORE notification processing
        # This ensures permissions are persisted even if notifications fail
        await db.commit()
        logger.info(f"Successfully committed permission changes for sub-agent {sub_agent_id}")

        # Notify affected users (after commit, so failures don't rollback permissions)
        if self.notification_service and (added_groups or removed_groups):
            try:
                agent_name = existing.name

                # Notify members of groups gaining access
                if added_groups:
                    member_ids = await self._get_members_from_groups(db, list(added_groups))

                    if member_ids:
                        notifications = [
                            NotificationData(
                                user_id=member_id,
                                notification_type=NotificationType.AGENT_SHARED,
                                title=f"Agent '{agent_name}' shared with you",
                                message=f"The agent '{agent_name}' has been shared with your group.",
                                metadata={"sub_agent_id": sub_agent_id},
                            )
                            for member_id in member_ids
                            if member_id != actor.id  # Don't notify the actor
                        ]
                        await self.notification_service.bulk_create_notifications(db, notifications)

                # Notify members of groups losing access
                if removed_groups:
                    member_ids = await self._get_members_from_groups(db, list(removed_groups))

                    if member_ids:
                        notifications = [
                            NotificationData(
                                user_id=member_id,
                                notification_type=NotificationType.AGENT_ACCESS_REVOKED,
                                title=f"Access to '{agent_name}' revoked",
                                message=f"Your group's access to the agent '{agent_name}' has been revoked.",
                                metadata={"sub_agent_id": sub_agent_id},
                            )
                            for member_id in member_ids
                            if member_id != actor.id  # Don't notify the actor
                        ]
                        await self.notification_service.bulk_create_notifications(db, notifications)

                # Notify members of groups with changed permissions
                if changed_groups:
                    member_ids = await self._get_members_from_groups(db, list(changed_groups))

                    if member_ids:
                        notifications = [
                            NotificationData(
                                user_id=member_id,
                                notification_type=NotificationType.AGENT_PERMISSION_CHANGED,
                                title=f"Permissions changed for agent '{agent_name}'",
                                message=f"The permissions for the agent '{agent_name}' have been updated for your group.",
                                metadata={"sub_agent_id": sub_agent_id},
                            )
                            for member_id in member_ids
                            if member_id != actor.id  # Don't notify the actor
                        ]
                        await self.notification_service.bulk_create_notifications(db, notifications)
                        # Commit notifications separately
                        await db.commit()

            except Exception as e:
                logger.error(f"Failed to create permission change notifications: {e}")
                # Don't re-raise - permissions are already committed

        return True

    async def get_permissions(
        self,
        db: AsyncSession,
        sub_agent_id: int,
    ) -> list[dict[str, Any]]:
        """Get group permissions with read/write details for a sub-agent."""
        query = text("""
            SELECT sap.user_group_id, ug.name as user_group_name, sap.permissions
            FROM sub_agent_permissions sap
            JOIN user_groups ug ON sap.user_group_id = ug.id
            WHERE sap.sub_agent_id = :id
            ORDER BY ug.name
        """)
        result = await db.execute(query, {"id": sub_agent_id})
        rows = result.mappings().all()
        return [
            {
                "user_group_id": row["user_group_id"],
                "user_group_name": row["user_group_name"],
                "permissions": row["permissions"],
            }
            for row in rows
        ]

    async def check_user_permission(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        user_id: str,
        required_permission: str,  # "read" or "write"
        sub_agent: SubAgent | None = None,
    ) -> bool:
        """Check if user has specific permission on a sub-agent.

        Authorization model: effective_permissions = resource_permissions ∩ role_capabilities

        User has permission if:
        - User owns the sub-agent (owner has all permissions)
        - Sub-agent is public AND permission is "read" (public allows read-only access)
        - User's group role allows the action AND the group has the permission on the resource

        Args:
            db: Database session
            sub_agent_id: Sub-agent ID
            user_id: User ID
            required_permission: Permission to check ("read" or "write")

        Returns:
            True if user has the required permission
        """
        # Check if user owns the sub-agent
        if not sub_agent:
            sub_agent = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not sub_agent:
            return False

        if sub_agent.owner_user_id == user_id:
            return True

        # Check if public and requesting read permission
        if sub_agent.is_public and required_permission == "read":
            return True

        # Check group permissions with role-based access control
        query = text("""
            SELECT sap.permissions, ugm.group_role
            FROM sub_agent_permissions sap
            JOIN user_group_members ugm ON sap.user_group_id = ugm.user_group_id
            WHERE sap.sub_agent_id = :sub_agent_id 
              AND ugm.user_id = :user_id
        """)
        result = await db.execute(query, {"sub_agent_id": sub_agent_id, "user_id": user_id})

        for row in result.fetchall():
            resource_permissions = row[0]  # PostgreSQL array: what the group can do on this sub-agent
            user_group_role = row[1]  # User's role in this group

            # Check if the resource has the required permission
            if required_permission not in resource_permissions:
                continue

            # Check if user's role allows this action (intersection)
            if check_action_allowed(user_group_role, "sub_agents", required_permission):
                return True

        return False

    async def get_pending_version_approvals(self, db: AsyncSession) -> list[dict[str, Any]]:
        """Get all versions pending approval with sub-agent info (admin only)."""
        query = text("""
            SELECT 
                sa.id as sub_agent_id, sa.name, sa.type, sa.default_version, sa.owner_user_id,
                u.email as owner_email, u.first_name, u.last_name,
                v.id as version_id, v.version, v.description, v.model, 
                v.system_prompt, v.agent_url, v.mcp_tools, 
                v.foundry_hostname, v.foundry_client_id, v.foundry_client_secret_ref,
                v.foundry_ontology_rid, v.foundry_query_api_name, v.foundry_scopes, v.foundry_version,
                v.change_summary, v.created_at as version_created_at
            FROM sub_agent_config_versions v
            JOIN sub_agents sa ON v.sub_agent_id = sa.id
            JOIN users u ON sa.owner_user_id = u.id
            WHERE v.status = 'pending_approval' AND sa.deleted_at IS NULL
            ORDER BY v.created_at ASC
        """)
        result = await db.execute(query)
        rows = result.mappings().all()

        return [
            {
                "sub_agent_id": row["sub_agent_id"],
                "name": row["name"],
                "type": row["type"],
                "default_version": row["default_version"],
                "owner": {
                    "id": row["owner_user_id"],
                    "name": f"{row['first_name']} {row['last_name']}",
                    "email": row["owner_email"],
                },
                "version_id": row["version_id"],
                "version": row["version"],
                "description": row["description"],
                "model": row["model"],
                "system_prompt": row["system_prompt"],
                "agent_url": row["agent_url"],
                "mcp_tools": row["mcp_tools"],
                "change_summary": row["change_summary"],
                "version_created_at": row["version_created_at"],
            }
            for row in rows
        ]

    def _generate_version_hash(
        self, system_prompt: str | None, agent_url: str | None, mcp_tools: list[str], timestamp: datetime
    ) -> str:
        """Generate a 12-character hash for a version based on content and timestamp."""
        content_dict = {"system_prompt": system_prompt, "agent_url": agent_url, "mcp_tools": mcp_tools}
        content = json.dumps(content_dict, sort_keys=True) + timestamp.isoformat()
        return hashlib.sha256(content.encode()).hexdigest()[:12]

    async def _create_config_version(
        self,
        db: AsyncSession,
        actor: User,
        sub_agent_id: int,
        version: int,
        change_summary: str,
        status: SubAgentStatus = SubAgentStatus.DRAFT,
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
        enable_thinking: bool | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> int:
        """Create a new configuration version entry. Returns the new version ID."""
        now = datetime.now(timezone.utc)
        mcp_tools_list = mcp_tools if mcp_tools is not None else []
        version_hash = self._generate_version_hash(system_prompt, agent_url, mcp_tools_list, now)

        return await self.repo.create_config_version(
            db=db,
            actor=actor,
            sub_agent_id=sub_agent_id,
            version=version,
            version_hash=version_hash,
            change_summary=change_summary,
            status=status.value,
            description=description,
            model=model,
            system_prompt=system_prompt,
            agent_url=agent_url,
            mcp_tools=mcp_tools_list,
            foundry_hostname=foundry_hostname,
            foundry_client_id=foundry_client_id,
            foundry_client_secret_ref=foundry_client_secret_ref,
            foundry_ontology_rid=foundry_ontology_rid,
            foundry_query_api_name=foundry_query_api_name,
            foundry_scopes=foundry_scopes,
            foundry_version=foundry_version,
            pricing_config=pricing_config,
            enable_thinking=enable_thinking,
            thinking_level=thinking_level,
        )

    def _row_to_sub_agent_with_version(self, row: Any) -> SubAgent:
        """Convert a database row (with joined version info) to a SubAgent model."""
        owner = SubAgentOwner(
            id=row["owner_user_id"],
            name=f"{row['first_name']} {row['last_name']}",
            email=row["owner_email"],
        )

        # Build config_version if version data is present
        config_version = None
        if row.get("cv_id") is not None:
            config_version = SubAgentConfigVersion(
                id=row["cv_id"],
                sub_agent_id=row["id"],  # sa.id from sub_agents table (equals cv.sub_agent_id)
                version=row["cv_version"],
                version_hash=row.get("cv_version_hash"),
                release_number=row.get("cv_release_number"),
                description=row["cv_description"],
                model=row["cv_model"],
                system_prompt=row.get("cv_system_prompt"),
                enable_thinking=row.get("cv_enable_thinking"),
                thinking_level=row.get("cv_thinking_level"),
                agent_url=row.get("cv_agent_url"),
                mcp_tools=row.get("cv_mcp_tools", []),
                foundry_hostname=row.get("cv_foundry_hostname"),
                foundry_client_id=row.get("cv_foundry_client_id"),
                foundry_client_secret_ref=row.get("cv_foundry_client_secret_ref"),
                foundry_client_secret_ssmkey=row.get("cv_foundry_client_secret_ssmkey"),
                foundry_ontology_rid=row.get("cv_foundry_ontology_rid"),
                foundry_query_api_name=row.get("cv_foundry_query_api_name"),
                foundry_scopes=row.get("cv_foundry_scopes"),
                foundry_version=row.get("cv_foundry_version"),
                pricing_config=row.get("cv_pricing_config"),
                change_summary=row["cv_change_summary"],
                status=row["cv_status"],
                submitted_by_user_id=row.get("cv_submitted_by_user_id"),
                approved_by_user_id=row["cv_approved_by_user_id"],
                approved_at=row["cv_approved_at"],
                rejection_reason=row["cv_rejection_reason"],
                deleted_at=row.get("cv_deleted_at"),
                created_at=row["cv_created_at"],
            )

        return SubAgent(
            id=row["id"],
            name=row["name"],
            owner_user_id=row["owner_user_id"],
            owner=owner,
            owner_status=row.get("owner_status", "active"),
            type=row["type"],
            current_version=row["current_version"],
            default_version=row.get("default_version"),
            config_version=config_version,
            is_public=row.get("is_public", False),
            is_activated=row.get("is_activated", False),
            activated_by=row.get("activated_by"),
            activated_by_groups=row.get("activated_by_groups", []),
            deleted_at=row.get("deleted_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_config_version(self, row: Any) -> SubAgentConfigVersion:
        """Convert a database row to a SubAgentConfigVersion model."""
        return SubAgentConfigVersion(
            id=row["id"],
            sub_agent_id=row["sub_agent_id"],
            version=row["version"],
            version_hash=row.get("version_hash"),
            release_number=row.get("release_number"),
            description=row["description"],
            model=row["model"],
            system_prompt=row.get("system_prompt"),
            agent_url=row.get("agent_url"),
            mcp_tools=row.get("mcp_tools", []),
            foundry_hostname=row.get("foundry_hostname"),
            foundry_client_id=row.get("foundry_client_id"),
            foundry_client_secret_ref=row.get("foundry_client_secret_ref"),
            foundry_client_secret_ssmkey=row.get("foundry_client_secret_ssmkey"),
            foundry_ontology_rid=row.get("foundry_ontology_rid"),
            foundry_query_api_name=row.get("foundry_query_api_name"),
            foundry_scopes=row.get("foundry_scopes"),
            foundry_version=row.get("foundry_version"),
            pricing_config=row.get("pricing_config"),
            enable_thinking=row.get("enable_thinking", False),
            thinking_level=row.get("thinking_level", "low"),
            change_summary=row["change_summary"],
            status=row["status"],
            submitted_by_user_id=row.get("submitted_by_user_id"),
            approved_by_user_id=row["approved_by_user_id"],
            approved_at=row["approved_at"],
            rejection_reason=row["rejection_reason"],
            deleted_at=row.get("deleted_at"),
            created_at=row["created_at"],
        )

    async def activate_sub_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        actor: User,
        is_admin: bool = False,
    ) -> bool:
        """Activate a sub-agent for a user.

        User must have read permission on the sub-agent (owner, public, or group member with read).
        Sub-agent must be approved (default_version must be set).

        Returns:
            True if activated successfully, False if already activated

        Raises:
            PermissionError: If user doesn't have read permission
            ValueError: If sub-agent is not approved
        """
        # Check if sub-agent exists and is approved
        sub_agent = await self.get_sub_agent_by_id(db, sub_agent_id)
        if not sub_agent:
            raise ValueError("Sub-agent not found")

        if sub_agent.default_version is None:
            raise ValueError("Sub-agent must be approved before activation")

        # Check if user has read permission (unless admin)
        if not is_admin:
            has_read = await self.check_user_permission(db, sub_agent_id, actor.id, "read", sub_agent=sub_agent)
            if not has_read:
                raise PermissionError("You don't have read permission for this sub-agent")

        # Check if already activated
        check_query = text("""
            SELECT 1 FROM user_sub_agent_activations
            WHERE user_id = :user_id AND sub_agent_id = :sub_agent_id
        """)
        result = await db.execute(check_query, {"user_id": actor.id, "sub_agent_id": sub_agent_id})
        if result.scalar_one_or_none():
            return False  # Already activated

        # Insert activation record with automatic audit
        await self.repo.bulk_activate_sub_agent(db=db, actor=actor, user_ids=[actor.id], sub_agent_id=sub_agent_id)
        await db.commit()
        return True

    async def deactivate_sub_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        actor: User,
    ) -> bool:
        """Deactivate a sub-agent for a user.

        Returns:
            True if deactivated successfully, False if not activated
        """
        # Check if activated first
        check_query = text("""
            SELECT 1 FROM user_sub_agent_activations
            WHERE user_id = :user_id AND sub_agent_id = :sub_agent_id
        """)
        result = await db.execute(check_query, {"user_id": actor.id, "sub_agent_id": sub_agent_id})
        if not result.scalar_one_or_none():
            return False  # Not activated

        # Delete activation record with automatic audit
        await self.repo.bulk_deactivate_sub_agent(db=db, actor=actor, user_ids=[actor.id], sub_agent_id=sub_agent_id)
        await db.commit()
        return True

    async def get_agents_with_group_permissions(
        self,
        db: AsyncSession,
        agent_ids: list[int],
        group_id: int,
    ) -> list[dict[str, Any]]:
        """
        Get sub-agents with their approval status and group permissions.

        This read method encapsulates the complex join logic for validating
        agents in the context of a specific group.

        Args:
            db: Database session
            agent_ids: List of sub-agent IDs to fetch
            group_id: Group ID to check permissions for

        Returns:
            List of dicts with keys: id, name, status, has_permission
        """
        if not agent_ids:
            return []

        query = text("""
            SELECT 
                sa.id, 
                sa.name, 
                COALESCE(cv_default.status, cv_current.status, 'draft') as status,
                (sap.sub_agent_id IS NOT NULL) as has_permission
            FROM sub_agents sa
            LEFT JOIN sub_agent_config_versions cv_default
                ON sa.id = cv_default.sub_agent_id AND sa.default_version = cv_default.version
            LEFT JOIN sub_agent_config_versions cv_current
                ON sa.id = cv_current.sub_agent_id AND sa.current_version = cv_current.version
            LEFT JOIN sub_agent_permissions sap 
                ON sa.id = sap.sub_agent_id AND sap.user_group_id = :group_id
            WHERE sa.id = ANY(:ids)
            AND sa.deleted_at IS NULL
        """)

        try:
            result = await db.execute(
                query,
                {"group_id": group_id, "ids": agent_ids},
            )
            rows = result.mappings().all()

            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "status": row["status"],
                    "has_permission": row["has_permission"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get agents with group permissions: {e}")
            raise

    async def get_agent_names(
        self,
        db: AsyncSession,
        agent_ids: list[int],
    ) -> dict[int, str]:
        """
        Get agent names by IDs.

        Args:
            db: Database session
            agent_ids: List of sub-agent IDs

        Returns:
            Dict mapping agent_id to name
        """
        if not agent_ids:
            return {}

        query = text("SELECT id, name FROM sub_agents WHERE id = ANY(:ids) AND deleted_at IS NULL")

        try:
            result = await db.execute(query, {"ids": agent_ids})
            return {row[0]: row[1] for row in result.fetchall()}
        except Exception as e:
            logger.error(f"Failed to get agent names: {e}")
            raise

    async def validate_agents_for_group(
        self,
        db: AsyncSession,
        agent_ids: list[int],
        group_id: int,
    ) -> None:
        """
        Validate that agents exist and group has permissions.

        This encapsulates all sub-agent validation logic for group operations.
        Note: Approval status is NOT validated - non-approved agents can be set
        as defaults, but will only activate once approved.

        Args:
            db: Database session
            agent_ids: List of sub-agent IDs to validate
            group_id: Group ID to check permissions for

        Raises:
            ValueError: If validation fails with specific error message
        """
        if not agent_ids:
            return

        # Get agents with their status and permissions using repository read method
        agents = await self.get_agents_with_group_permissions(db, agent_ids, group_id)

        # Check if all requested agents were found
        if len(agents) != len(agent_ids):
            found_ids = {agent["id"] for agent in agents}
            missing = set(agent_ids) - found_ids
            raise ValueError(f"Sub-agents not found: {missing}")

        # Validate each agent has permissions (approval status no longer checked)
        for agent in agents:
            if not agent["has_permission"]:
                raise ValueError(
                    f"Group does not have permission to sub-agent '{agent['name']}'. Add permissions first."
                )

        logger.info(f"Validated {len(agent_ids)} agents for group {group_id}")
