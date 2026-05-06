"""API routes for bug reports."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..authorization import check_capability
from ..db.session import DbSession
from ..dependencies import User, require_auth
from ..models.bug_report import (
    BugReportCreate,
    BugReportListResponse,
    BugReportResponse,
    BugReportStatus,
    BugReportStatusUpdate,
)
from ..models.user import PaginationMeta
from ..services.bug_report_service import BugReportService
from ..services.debug_agent_service import DebugAgentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bug-reports", tags=["bug-reports"])


def get_bug_report_service(request: Request) -> BugReportService:
    return request.app.state.bug_report_service


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_bug_report(
    request: Request,
    body: BugReportCreate,
    db: DbSession,
    user: User = Depends(require_auth),
) -> BugReportResponse:
    service = get_bug_report_service(request)
    return await service.create_bug_report(
        db=db,
        actor=user,
        conversation_id=body.conversation_id,
        source=body.source.value,
        message_id=body.message_id,
        task_id=body.task_id,
        description=body.description,
    )


@router.get("")
async def list_bug_reports(
    request: Request,
    db: DbSession,
    page: int = 1,
    limit: int = 50,
    status_filter: BugReportStatus | None = None,
    user: User = Depends(require_auth),
) -> BugReportListResponse:
    service = get_bug_report_service(request)
    if limit > 100:
        limit = 100

    # Admin users see all reports; regular users see only their own
    user_id_filter = None if user.is_administrator else user.id

    reports, total = await service.list_bug_reports(
        db=db,
        user_id=user_id_filter,
        status=status_filter,
        page=page,
        limit=limit,
    )
    return BugReportListResponse(
        data=reports,
        meta=PaginationMeta(page=page, limit=limit, total=total),
    )


@router.get("/{report_id}")
async def get_bug_report(
    request: Request,
    report_id: str,
    db: DbSession,
    user: User = Depends(require_auth),
) -> BugReportResponse:
    service = get_bug_report_service(request)
    report = await service.get_bug_report(db=db, report_id=report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")

    # Regular users can only view their own reports
    if not user.is_administrator and report.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")

    return report


@router.patch("/{report_id}/status")
async def update_bug_report_status(
    request: Request,
    report_id: str,
    body: BugReportStatusUpdate,
    db: DbSession,
    user: User = Depends(require_auth),
) -> BugReportResponse:
    service = get_bug_report_service(request)

    existing = await service.get_bug_report(db=db, report_id=report_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")

    has_triage = (
        user.is_administrator
        or check_capability(user.role.value, "bug_reports", "triage")
        or check_capability(user.role.value, "bug_reports", "triage.admin")
    )

    if has_triage:
        pass
    elif existing.user_id == user.id and body.status == BugReportStatus.RESOLVED:
        pass
    elif existing.user_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only resolve your own bug reports",
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bug report triage capability required",
        )

    report = await service.update_status(
        db=db,
        actor=user,
        report_id=report_id,
        new_status=body.status,
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")
    return report


def get_debug_agent_service(request: Request) -> DebugAgentService:
    return request.app.state.debug_agent_service


@router.post("/{report_id}/debug", status_code=status.HTTP_202_ACCEPTED)
async def trigger_debug_agent(
    request: Request,
    report_id: str,
    db: DbSession,
    user: User = Depends(require_auth),
) -> dict:
    has_triage = (
        user.is_administrator
        or check_capability(user.role.value, "bug_reports", "triage")
        or check_capability(user.role.value, "bug_reports", "triage.admin")
    )
    if not has_triage:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bug report triage capability required",
        )

    service = get_bug_report_service(request)
    debug_service = get_debug_agent_service(request)

    report = await service.get_bug_report(db=db, report_id=report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")

    if report.status == BugReportStatus.INVESTIGATING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bug report is already being investigated",
        )

    user_access_token = getattr(request.state, "access_token", None)
    if not user_access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No access token available for token exchange",
        )

    try:
        await debug_service.trigger_debug(db=db, actor=user, bug_report=report, user_access_token=user_access_token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return {"status": "investigating", "report_id": report_id}
