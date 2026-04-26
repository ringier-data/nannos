"""Router for sub-agent management endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..db.session import DbSession
from ..dependencies import (
    has_capability,
    is_admin_mode,
    require_admin,
    require_approver,
    require_auth,
    require_auth_or_bearer_token,
)
from ..models.sub_agent import (
    SubAgent,
    SubAgentApproval,
    SubAgentConfigVersion,
    SubAgentCreate,
    SubAgentGroupPermissionResponse,
    SubAgentListResponse,
    SubAgentPermissionsUpdate,
    SubAgentSetDefaultVersion,
    SubAgentStatus,
    SubAgentSubmitRequest,
    SubAgentUpdate,
    SubAgentVersionApproval,
)
from ..models.user import User
from ..services.sub_agent_service import SubAgentService

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api/v1/sub-agents", tags=["sub-agents"])


def get_sub_agent_service(request: Request) -> SubAgentService:
    """Get sub-agent service from app state."""
    return request.app.state.sub_agent_service


@router.get("", response_model=SubAgentListResponse, tags=["MCP"], operation_id="playground_list_sub_agents")
async def list_sub_agents(
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
    status: SubAgentStatus | None = Query(None, description="Filter by status"),
    owned_only: bool = Query(False, description="Only show owned sub-agents"),
    activated_only: bool = Query(False, description="Only show activated sub-agents"),
) -> SubAgentListResponse:
    """List sub-agents accessible to the current user.

    Supports both user session authentication and Bearer token authentication.
    The orchestrator can call this endpoint on behalf of a user by passing
    the user's access token as a Bearer token.

    - Regular users see their own sub-agents + public sub-agents + those assigned to their groups
    - Admins (with admin mode enabled) see all sub-agents
    - When impersonating, shows only what the impersonated user can see (not admin view)
    - Use `status` to filter by status (e.g., pending_approval for admin queue)
    - Use `owned_only=true` to see only owned sub-agents
    - Use `activated_only=true` to see only activated sub-agents (for orchestrator)
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        # When impersonating, always use impersonated user's view (not admin view)
        # Admin privileges are for operations, not for seeing everything the user sees
        is_impersonating = hasattr(request.state, "original_user") and request.state.original_user
        effective_admin = is_admin_mode(request, user) and not is_impersonating

        if owned_only:
            # Only show owned sub-agents
            sub_agents = await sub_agent_service.get_accessible_sub_agents(
                db, user.id, is_admin=False, status_filter=status, include_owned=True, activated_only=activated_only
            )
            # Filter to owned only
            sub_agents = [sa for sa in sub_agents if sa.owner_user_id == user.id]
        else:
            sub_agents = await sub_agent_service.get_accessible_sub_agents(
                db, user.id, is_admin=effective_admin, status_filter=status, activated_only=activated_only
            )

        return SubAgentListResponse(items=sub_agents, total=len(sub_agents))
    except Exception as e:
        logger.error(f"Failed to list sub-agents: {e}")
        raise HTTPException(status_code=500, detail="Failed to list sub-agents")


@router.get("/pending", response_model=SubAgentListResponse)
async def list_pending_approvals(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
) -> SubAgentListResponse:
    """List sub-agents pending approval (admin only)."""
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agents = await sub_agent_service.get_pending_approvals(db)
        return SubAgentListResponse(items=sub_agents, total=len(sub_agents))
    except Exception as e:
        logger.error(f"Failed to list pending approvals: {e}")
        raise HTTPException(status_code=500, detail="Failed to list pending approvals")


@router.get("/configs/by-hash/{version_hash}", response_model=SubAgent)
async def get_sub_agent_by_config_hash(
    request: Request,
    version_hash: str,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> SubAgent:
    """Get a sub-agent by config version hash.

    Used by the orchestrator to fetch a specific version for playground testing.
    Returns the sub-agent with the specified version's data embedded in config_version.

    User must be owner or have group access to the sub-agent.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.get_sub_agent_by_version_hash(db, version_hash)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Config version not found")

        # Check access
        accessible = await sub_agent_service.get_accessible_sub_agents(db, user.id)
        if not any(sa.id == sub_agent.id for sa in accessible):
            raise HTTPException(status_code=403, detail="Access denied")

        return sub_agent
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get sub-agent by version hash {version_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get sub-agent")


@router.get("/configs/{config_version_id}", response_model=SubAgent)
async def get_sub_agent_by_config_version(
    request: Request,
    config_version_id: int,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> SubAgent:
    """Get a sub-agent by config version ID.

    Used by the orchestrator to fetch a specific version for playground testing.
    Returns the sub-agent with the specified version's data embedded in config_version.

    User must be owner or have group access to the sub-agent.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.get_sub_agent_by_config_version_id(db, config_version_id)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Config version not found")

        # Check access
        accessible = await sub_agent_service.get_accessible_sub_agents(db, user.id)
        if not any(sa.id == sub_agent.id for sa in accessible):
            raise HTTPException(status_code=403, detail="Access denied")

        return sub_agent
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get sub-agent by config version {config_version_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get sub-agent")


@router.post("", response_model=SubAgent, status_code=201, tags=["MCP"], operation_id="playground_create_sub_agent")
async def create_sub_agent(
    request: Request,
    data: SubAgentCreate,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> SubAgent:
    """Create a new sub-agent.

    The sub-agent will be created in 'draft' status.
    Use POST /{id}/submit to submit for approval.

    Supports both session-based authentication and Bearer token authentication.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.create_sub_agent(db, data, actor=user)
        return sub_agent
    except ValueError as e:
        logger.debug(f"Validation error creating sub-agent: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create sub-agent: {e}")
        raise HTTPException(status_code=500, detail="Failed to create sub-agent")


@router.get("/{sub_agent_id}", response_model=SubAgent)
async def get_sub_agent(
    request: Request,
    sub_agent_id: int,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
    version: int | None = Query(None, description="Specific version to fetch (defaults to current)"),
) -> SubAgent:
    """Get a sub-agent by ID.

    Returns the sub-agent with owner info and the specified version's config.
    If no version is specified, returns the current version.
    User must be owner, have group access, or be admin (with admin mode enabled).
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        effective_admin = is_admin_mode(request, user)
        sub_agent = await sub_agent_service.get_sub_agent_by_id(db, sub_agent_id, version=version)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        # Check access
        if not effective_admin and sub_agent.owner_user_id != user.id:
            # Check group access
            accessible = await sub_agent_service.get_accessible_sub_agents(db, user.id)
            if not any(sa.id == sub_agent_id for sa in accessible):
                raise HTTPException(status_code=403, detail="Access denied")

        return sub_agent
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get sub-agent")


@router.patch("/{sub_agent_id}", response_model=SubAgent, tags=["MCP"], operation_id="playground_update_sub_agent")
async def update_sub_agent(
    request: Request,
    sub_agent_id: int,
    data: SubAgentUpdate,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> SubAgent:
    """Update a sub-agent.

    Only the owner can update. For local sub-agents, configuration changes
    automatically create a new version in the history.

    Supports both session-based authentication and Bearer token authentication.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.update_sub_agent(db, sub_agent_id, data, actor=user)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update sub-agent")


@router.delete("/{sub_agent_id}", status_code=204)
async def delete_sub_agent(
    request: Request,
    sub_agent_id: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> None:
    """Delete a sub-agent."""
    sub_agent_service = get_sub_agent_service(request)
    try:
        deleted = await sub_agent_service.delete_sub_agent(db, sub_agent_id, actor=user)
        if not deleted:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete sub-agent")


@router.post("/{sub_agent_id}/activate", status_code=200)
async def activate_sub_agent(
    request: Request,
    sub_agent_id: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> dict:
    """Activate a sub-agent for the current user.

    User must have access to the sub-agent (owner, public, or group member).
    Sub-agent must be approved (have a default_version).
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        effective_admin = is_admin_mode(request, user)
        activated = await sub_agent_service.activate_sub_agent(db, sub_agent_id, is_admin=effective_admin, actor=user)
        if activated:
            return {"message": "Sub-agent activated successfully"}
        else:
            return {"message": "Sub-agent was already activated"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to activate sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to activate sub-agent")


@router.post("/{sub_agent_id}/deactivate", status_code=200)
async def deactivate_sub_agent(
    request: Request,
    sub_agent_id: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> dict:
    """Deactivate a sub-agent for the current user."""
    sub_agent_service = get_sub_agent_service(request)
    try:
        deactivated = await sub_agent_service.deactivate_sub_agent(db, sub_agent_id, actor=user)
        if deactivated:
            return {"message": "Sub-agent deactivated successfully"}
        else:
            return {"message": "Sub-agent was not activated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to deactivate sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to deactivate sub-agent")


@router.post("/{sub_agent_id}/submit", response_model=SubAgent)
async def submit_for_approval(
    request: Request,
    sub_agent_id: int,
    data: SubAgentSubmitRequest,
    db: DbSession,
    user: User = Depends(require_auth),
) -> SubAgent:
    """Submit a draft sub-agent for admin approval.

    Only the owner can submit. Sub-agent must be in 'draft' or 'rejected' status.
    Requires a change_summary describing what changed in this version.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.submit_for_approval(db, sub_agent_id, data.change_summary, actor=user)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit for approval")


@router.post("/{sub_agent_id}/approve", response_model=SubAgent)
async def approve_sub_agent(
    request: Request,
    sub_agent_id: int,
    data: SubAgentApproval,
    db: DbSession,
    user: User = Depends(require_approver),
) -> SubAgent:
    """Approve or reject a sub-agent (approver/admin only).

    - action: 'approve' or 'reject'
    - rejection_reason: Required when action is 'reject'
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        if data.action == "reject" and not data.rejection_reason:
            raise HTTPException(status_code=400, detail="Rejection reason is required when rejecting")

        sub_agent = await sub_agent_service.approve_sub_agent(
            db,
            sub_agent_id,
            actor=user,
            approve=(data.action == "approve"),
            rejection_reason=data.rejection_reason,
        )
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to approve/reject sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to approve/reject sub-agent")


@router.get("/{sub_agent_id}/permissions", response_model=list[SubAgentGroupPermissionResponse])
async def get_sub_agent_permissions(
    request: Request,
    sub_agent_id: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> list[SubAgentGroupPermissionResponse]:
    """Get group permissions (read/write) for this sub-agent.

    Owner, admin (with admin mode enabled), or users with write permission can view permissions.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        effective_admin = is_admin_mode(request, user)
        sub_agent = await sub_agent_service.get_sub_agent_by_id(db, sub_agent_id)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        # Check if user is owner or admin
        if not effective_admin and sub_agent.owner_user_id != user.id:
            # Check if user has write permission for this sub-agent
            has_write = await sub_agent_service.check_user_permission(db, sub_agent_id, user.id, "write")
            if not has_write:
                raise HTTPException(status_code=403, detail="Insufficient permissions or admin mode not enabled")

        permissions = await sub_agent_service.get_permissions(db, sub_agent_id)
        return [SubAgentGroupPermissionResponse(**perm) for perm in permissions]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get permissions for sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get permissions")


@router.get("/{sub_agent_id}/versions", response_model=list[SubAgentConfigVersion])
async def get_sub_agent_versions(
    request: Request,
    sub_agent_id: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> list[SubAgentConfigVersion]:
    """Get all configuration versions for a sub-agent.

    Returns version history with all configuration data.
    User must be owner, have group access, or be admin.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        effective_admin = is_admin_mode(request, user)
        sub_agent = await sub_agent_service.get_sub_agent_by_id(db, sub_agent_id)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        # Check access
        if not effective_admin and sub_agent.owner_user_id != user.id:
            accessible = await sub_agent_service.get_accessible_sub_agents(db, user.id)
            if not any(sa.id == sub_agent_id for sa in accessible):
                raise HTTPException(status_code=403, detail="Access denied")

        return await sub_agent_service.get_config_versions(db, sub_agent_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get versions for sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get versions")


@router.put("/{sub_agent_id}/permissions", status_code=204)
async def update_sub_agent_permissions(
    request: Request,
    sub_agent_id: int,
    data: SubAgentPermissionsUpdate,
    db: DbSession,
    user: User = Depends(require_auth),
) -> None:
    """Update group permissions (read/write) for this sub-agent.

    Owner, admin (with admin mode enabled), or users with write permission can update permissions.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        effective_admin = is_admin_mode(request, user)

        # Check if user has write permission if not owner or admin
        sub_agent = await sub_agent_service.get_sub_agent_by_id(db, sub_agent_id)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        has_permission = effective_admin or sub_agent.owner_user_id == user.id
        if not has_permission:
            has_permission = await sub_agent_service.check_user_permission(db, sub_agent_id, user.id, "write")

        if not has_permission:
            raise HTTPException(status_code=403, detail="Insufficient permissions or admin mode not enabled")

        # Convert Pydantic models to dicts for service layer
        group_permissions = [
            {"user_group_id": gp.user_group_id, "permissions": gp.permissions} for gp in data.group_permissions
        ]

        # If we get here, user is authorized
        success = await sub_agent_service.update_permissions(
            db,
            sub_agent_id,
            group_permissions,
            actor=user,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update permissions for sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update permissions")


@router.post("/{sub_agent_id}/versions/{version}/revert", response_model=SubAgent)
async def revert_to_version(
    request: Request,
    sub_agent_id: int,
    version: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> SubAgent:
    """Revert a local sub-agent to a previous configuration version.

    Creates a new version with the reverted configuration.
    Only owner can revert. Only works for local sub-agents.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.revert_to_version(db, sub_agent_id, version, actor=user)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to revert sub-agent {sub_agent_id} to version {version}: {e}")
        raise HTTPException(status_code=500, detail="Failed to revert to version")


@router.post("/{sub_agent_id}/versions/{version}/submit", response_model=SubAgent)
async def submit_version_for_approval(
    request: Request,
    sub_agent_id: int,
    version: int,
    data: SubAgentSubmitRequest,
    db: DbSession,
    user: User = Depends(require_auth),
) -> SubAgent:
    """Submit a specific version for admin approval.

    Only the owner can submit. Version must be in 'draft' or 'rejected' status.
    Requires a change_summary describing what changed in this version.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.submit_version_for_approval(
            db, sub_agent_id, version, data.change_summary, actor=user
        )
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit version {version} of sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit version for approval")


@router.delete("/{sub_agent_id}/versions/{version}", response_model=dict)
async def delete_version(
    request: Request,
    sub_agent_id: int,
    version: int,
    db: DbSession,
    user: User = Depends(require_auth),
) -> dict:
    """Delete a specific version (soft-delete).

    Only the owner can delete versions.
    Only draft, pending_approval, or rejected versions can be deleted.
    Approved versions cannot be deleted to preserve release history.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        deleted = await sub_agent_service.delete_version(db, sub_agent_id, version, actor=user)
        if not deleted:
            raise HTTPException(status_code=404, detail="Version not found")
        return {"success": True, "message": f"Version {version} deleted successfully"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete version {version} of sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete version")


@router.post("/{sub_agent_id}/versions/{version}/review", response_model=SubAgent)
async def review_version(
    request: Request,
    sub_agent_id: int,
    version: int,
    data: SubAgentVersionApproval,
    db: DbSession,
    user: User = Depends(require_auth),
) -> SubAgent:
    """Approve or reject a specific version.

    Requires approver or admin system role.
    Permission intersection ensures approvers can only approve resources they have access to
    (owned sub-agents or sub-agents in groups where they have write permission).

    - action: 'approve' or 'reject'
    - rejection_reason: Required when action is 'reject'

    When approved, the version automatically becomes the default version.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        # Check if user has approval capabilities
        can_approve = has_capability(user, "sub_agents", "approve") or has_capability(
            user, "sub_agents", "approve.admin"
        )

        if not can_approve:
            raise HTTPException(status_code=403, detail="Approval requires approver or admin role")

        if data.action == "reject" and not data.rejection_reason:
            raise HTTPException(status_code=400, detail="Rejection reason is required when rejecting")

        sub_agent = await sub_agent_service.approve_version(
            db,
            sub_agent_id,
            version,
            approve=(data.action == "approve"),
            rejection_reason=data.rejection_reason,
            actor=user,
        )
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to review version {version} of sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to review version")


@router.put("/{sub_agent_id}/default-version", response_model=SubAgent)
async def set_default_version(
    request: Request,
    sub_agent_id: int,
    data: SubAgentSetDefaultVersion,
    db: DbSession,
    user: User = Depends(require_auth),
) -> SubAgent:
    """Set an approved version as the default version.

    Only the owner can set the default version.
    The version must be in 'approved' status.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        sub_agent = await sub_agent_service.set_default_version(db, sub_agent_id, data.version, actor=user)
        if not sub_agent:
            raise HTTPException(status_code=404, detail="Sub-agent not found")
        return sub_agent
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set default version for sub-agent {sub_agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to set default version")


@router.get("/admin/pending-versions")
async def list_pending_version_approvals(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
) -> list[dict]:
    """List all versions pending approval across all sub-agents (admin only).

    Returns version info with sub-agent context for approval queue.
    """
    sub_agent_service = get_sub_agent_service(request)
    try:
        pending = await sub_agent_service.get_pending_version_approvals(db)
        return pending
    except Exception as e:
        logger.error(f"Failed to list pending version approvals: {e}")
        raise HTTPException(status_code=500, detail="Failed to list pending approvals")
