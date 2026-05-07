"""Admin router for outbound SCIM endpoint management."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_admin
from ..models.outbound_scim import (
    OutboundScimEndpoint,
    OutboundScimEndpointCreate,
    OutboundScimEndpointCreated,
    OutboundScimEndpointDetailResponse,
    OutboundScimEndpointListResponse,
    OutboundScimEndpointUpdate,
    OutboundScimTestResult,
)
from ..models.user import PaginationMeta, User
from ..services.outbound_scim_endpoint_service import OutboundScimEndpointService
from ..services.outbound_scim_push_service import OutboundScimPushService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/outbound-scim-endpoints", tags=["admin-outbound-scim"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_endpoint_service(request: Request) -> OutboundScimEndpointService:
    """Get outbound SCIM endpoint service from app state."""
    return request.app.state.outbound_scim_endpoint_service


def get_push_service(request: Request) -> OutboundScimPushService:
    """Get outbound SCIM push service from app state."""
    return request.app.state.outbound_scim_push_service


@router.post("", response_model=OutboundScimEndpointCreated, status_code=status.HTTP_201_CREATED)
async def create_outbound_scim_endpoint(
    request: Request,
    db: DbSession,
    body: OutboundScimEndpointCreate,
    admin: User = Depends(require_admin),
) -> OutboundScimEndpointCreated:
    """Create a new outbound SCIM endpoint.

    Returns the full bearer token value. This is the only time the token is visible in the response.
    """
    service = get_endpoint_service(request)
    endpoint = await service.create_endpoint(
        db,
        name=body.name,
        endpoint_url=body.endpoint_url,
        bearer_token=body.bearer_token,
        push_users=body.push_users,
        push_groups=body.push_groups,
        actor=admin,
    )
    await db.commit()
    return endpoint


@router.get("", response_model=OutboundScimEndpointListResponse)
async def list_outbound_scim_endpoints(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
) -> OutboundScimEndpointListResponse:
    """List all outbound SCIM endpoints.

    Bearer token values are masked — only the last 4 characters are shown.
    """
    service = get_endpoint_service(request)
    endpoints = await service.list_endpoints(db)
    return OutboundScimEndpointListResponse(
        data=endpoints,
        meta=PaginationMeta(page=1, limit=len(endpoints), total=len(endpoints)),
    )


@router.get("/{endpoint_id}", response_model=OutboundScimEndpointDetailResponse)
async def get_outbound_scim_endpoint(
    request: Request,
    db: DbSession,
    endpoint_id: int,
    _: User = Depends(require_admin),
) -> OutboundScimEndpointDetailResponse:
    """Get details of a specific outbound SCIM endpoint (masked token)."""
    service = get_endpoint_service(request)
    endpoint = await service.get_endpoint(db, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outbound SCIM endpoint not found")
    return OutboundScimEndpointDetailResponse(data=endpoint)


@router.patch("/{endpoint_id}", response_model=OutboundScimEndpointDetailResponse)
async def update_outbound_scim_endpoint(
    request: Request,
    db: DbSession,
    endpoint_id: int,
    body: OutboundScimEndpointUpdate,
    admin: User = Depends(require_admin),
) -> OutboundScimEndpointDetailResponse:
    """Update an outbound SCIM endpoint."""
    service = get_endpoint_service(request)
    endpoint = await service.update_endpoint(
        db,
        endpoint_id,
        actor=admin,
        name=body.name,
        endpoint_url=body.endpoint_url,
        bearer_token=body.bearer_token,
        enabled=body.enabled,
        push_users=body.push_users,
        push_groups=body.push_groups,
    )
    if not endpoint:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outbound SCIM endpoint not found")
    await db.commit()
    return OutboundScimEndpointDetailResponse(data=endpoint)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_outbound_scim_endpoint(
    request: Request,
    db: DbSession,
    endpoint_id: int,
    admin: User = Depends(require_admin),
) -> None:
    """Soft-delete an outbound SCIM endpoint."""
    service = get_endpoint_service(request)
    deleted = await service.delete_endpoint(db, endpoint_id, actor=admin)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outbound SCIM endpoint not found")
    await db.commit()


@router.post("/{endpoint_id}/test", response_model=OutboundScimTestResult)
async def test_outbound_scim_endpoint(
    request: Request,
    db: DbSession,
    endpoint_id: int,
    _: User = Depends(require_admin),
) -> OutboundScimTestResult:
    """Test connectivity to an outbound SCIM endpoint.

    Attempts GET /ServiceProviderConfig on the remote endpoint to verify it's reachable.
    """
    endpoint_service = get_endpoint_service(request)
    push_service = get_push_service(request)

    # Get endpoint with full token
    result = await db.execute(
        text("""
            SELECT endpoint_url, bearer_token
            FROM outbound_scim_endpoints
            WHERE id = :id AND deleted_at IS NULL
        """),
        {"id": endpoint_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outbound SCIM endpoint not found")

    test_result = await push_service.test_endpoint(row.endpoint_url, row.bearer_token)
    return OutboundScimTestResult(**test_result)
