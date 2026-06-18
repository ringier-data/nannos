"""Admin router for MCP gateway server access management."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.mcp_gateway_server_access import (
    McpGatewayGrantServerAccessRequest,
    McpGatewayServerPermissionsResponse,
    McpGatewayStatusResponse,
)
from ..models.user import User
from ..services.mcp_gateway_server_access_service import McpGatewayServerAccessService
from ..services.orchestrator_cache import schedule_orchestrator_discovery_cache_invalidation
from ..utils.gatana_auth import get_gatana_token

router = APIRouter(prefix="/api/v1/admin/groups", tags=["admin-mcp-gateway"])


def get_mcp_gateway_service(request: Request) -> McpGatewayServerAccessService:
    """Get MCP gateway server access service from app state."""
    return request.app.state.mcp_gateway_server_access_service


async def _invalidate_group_members_cache(
    background_tasks: BackgroundTasks, request: Request, db: DbSession, group_id: int, reason: str
) -> None:
    """Schedule a scoped discovery-cache invalidation for the members of ``group_id``."""
    member_subs = await request.app.state.user_group_service.get_group_member_subs(db, group_id)
    schedule_orchestrator_discovery_cache_invalidation(background_tasks, request, reason, member_subs)


@router.get(
    "/{group_id}/mcp-gateway-status",
    response_model=McpGatewayStatusResponse,
    summary="Check if a group is managed by the MCP gateway",
    description="Returns whether the group has been synced to the MCP gateway via outbound SCIM and its team ID.",
)
async def get_mcp_gateway_status(
    group_id: int,
    request: Request,
    user: User = Depends(require_admin),
    service: McpGatewayServerAccessService = Depends(get_mcp_gateway_service),
) -> McpGatewayStatusResponse:
    team_id = await service.resolve_gateway_team_id(group_id)
    return McpGatewayStatusResponse(managed=team_id is not None, team_id=team_id)


@router.get(
    "/{group_id}/mcp-gateway-servers",
    response_model=McpGatewayServerPermissionsResponse,
    summary="List MCP server access for a group",
    description="Returns the list of MCP servers that the group has access to via the gateway.",
)
async def list_mcp_gateway_servers(
    group_id: int,
    request: Request,
    user: User = Depends(require_admin),
    service: McpGatewayServerAccessService = Depends(get_mcp_gateway_service),
) -> McpGatewayServerPermissionsResponse:
    gatana_token = await get_gatana_token(request, user)
    try:
        permissions = await service.list_server_access(gatana_token, group_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return McpGatewayServerPermissionsResponse(permissions=permissions)


@router.put(
    "/{group_id}/mcp-gateway-servers/{server_slug}",
    status_code=204,
    summary="Grant or update MCP server access for a group",
    description="Grants or updates the access role for a group on a specific MCP server.",
)
async def grant_mcp_gateway_server_access(
    group_id: int,
    server_slug: str,
    body: McpGatewayGrantServerAccessRequest,
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
    service: McpGatewayServerAccessService = Depends(get_mcp_gateway_service),
) -> None:
    gatana_token = await get_gatana_token(request, user)
    try:
        await service.grant_server_access(gatana_token, group_id, server_slug, body.role)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _invalidate_group_members_cache(
        background_tasks, request, db, group_id, f"grant server '{server_slug}' to group {group_id}"
    )


@router.delete(
    "/{group_id}/mcp-gateway-servers/{server_slug}",
    status_code=204,
    summary="Revoke MCP server access for a group",
    description="Revokes the group's access to a specific MCP server.",
)
async def revoke_mcp_gateway_server_access(
    group_id: int,
    server_slug: str,
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
    service: McpGatewayServerAccessService = Depends(get_mcp_gateway_service),
) -> None:
    gatana_token = await get_gatana_token(request, user)
    try:
        await service.revoke_server_access(gatana_token, group_id, server_slug)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _invalidate_group_members_cache(
        background_tasks, request, db, group_id, f"revoke server '{server_slug}' from group {group_id}"
    )
