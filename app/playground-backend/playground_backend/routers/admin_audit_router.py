"""Admin audit log router."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_admin
from ..models.audit import AuditAction, AuditEntityType, AuditLogListResponse
from ..models.user import PaginationMeta, User
from ..services.audit_service import audit_service

router = APIRouter(prefix="/api/v1/admin/audit-logs", tags=["admin-audit"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get("", response_model=AuditLogListResponse)
async def list_audit_logs(
    db: DbSession,
    _: User = Depends(require_admin),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    entity_type: AuditEntityType | None = Query(None, description="Filter by entity type"),
    entity_id: str | None = Query(None, description="Filter by entity ID"),
    actor_sub: str | None = Query(None, alias="user_id", description="Filter by actor sub"),
    action: AuditAction | None = Query(None, description="Filter by action"),
    from_date: datetime | None = Query(None, description="Filter from date"),
    to_date: datetime | None = Query(None, description="Filter to date"),
) -> AuditLogListResponse:
    """List audit logs with filtering.

    Admin only endpoint.
    """
    logs, total = await audit_service.list_logs(
        db,
        page=page,
        limit=limit,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_sub=actor_sub,
        action=action,
        from_date=from_date,
        to_date=to_date,
    )

    return AuditLogListResponse(
        data=logs,
        meta=PaginationMeta(page=page, limit=limit, total=total),
    )
