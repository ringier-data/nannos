"""Bug report MCP tools — expose bug report management as MCP tools.

Endpoints are tagged "MCP" so FastApiMCP auto-exposes them as MCP tools,
allowing the orchestrator and debug agent to manage bug report lifecycle autonomously.

Access control uses the Two-Layer RBAC model:
- Any authenticated user can create bug reports.
- Members can update status on their own reports (self-resolve only).
- Approvers/admins with ``triage`` capability can manage any accessible report.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..authorization import check_capability
from ..db.session import DbSession
from ..dependencies import require_auth_or_bearer_token
from ..models.bug_report import BugReportResponse, BugReportStatus
from ..models.user import User
from ..services.bug_report_service import BugReportService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bug-reports")


def _get_bug_report_service(request: Request) -> BugReportService:
    return request.app.state.bug_report_service


def _has_triage(user: User) -> bool:
    """Return True if user has triage capability on bug_reports."""
    if user.is_administrator:
        return True
    return check_capability(user.role.value, "bug_reports", "triage") or check_capability(
        user.role.value, "bug_reports", "triage.admin"
    )


def _require_triage(user: User) -> None:
    """Raise 403 if user lacks triage capability on bug_reports."""
    if not _has_triage(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bug report triage capability required",
        )


@router.post(
    "/mcp-create",
    response_model=BugReportResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["MCP"],
    operation_id="console_create_bug_report",
    summary="Create a bug report for an unrecoverable error.",
    description=(
        "File a bug report when an unrecoverable error prevents fulfilling the user's request. "
        "Only use as a last resort after exhausting recovery options (retries, alternative tools, plan changes)."
    ),
)
async def create_bug_report_mcp(
    request: Request,
    conversation_id: str = Query(..., description="The conversation ID where the error occurred."),
    description: str = Query(..., description="Description of the bug — what went wrong and why it's unrecoverable."),
    task_id: str | None = Query(None, description="The A2A task ID associated with the error."),
    db: DbSession = None,
    user: User = Depends(require_auth_or_bearer_token),
) -> BugReportResponse:
    service = _get_bug_report_service(request)
    return await service.create_bug_report(
        db=db,
        actor=user,
        conversation_id=conversation_id,
        source="orchestrator",
        task_id=task_id,
        description=description,
    )


@router.patch(
    "/{report_id}/mcp-status",
    response_model=BugReportResponse,
    tags=["MCP"],
    operation_id="console_update_bug_report_status",
    summary="Update the status of a bug report.",
    description=(
        "Transition a bug report to a new status. Valid statuses: open, acknowledged, investigating, resolved."
    ),
)
async def update_bug_report_status_mcp(
    request: Request,
    report_id: str,
    new_status: BugReportStatus,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> BugReportResponse:
    service = _get_bug_report_service(request)

    existing = await service.get_bug_report(db=db, report_id=report_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")

    if _has_triage(user):
        # Triagers can update any accessible report to any valid status
        pass
    elif existing.user_id == user.id and new_status == BugReportStatus.RESOLVED:
        # Members can self-resolve their own reports
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
        new_status=new_status,
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")
    return report


@router.patch(
    "/{report_id}/mcp-external-link",
    response_model=BugReportResponse,
    tags=["MCP"],
    operation_id="console_set_bug_report_external_link",
    summary="Set the external link (e.g. GitHub issue URL) on a bug report.",
    description=(
        "Store a reference to an external issue tracker (GitHub, Jira, etc.) on the bug report so users can follow up."
    ),
)
async def set_bug_report_external_link_mcp(
    request: Request,
    report_id: str,
    external_link: str,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> BugReportResponse:
    _require_triage(user)
    service = _get_bug_report_service(request)

    if _has_triage(user):
        # Just triagers can set external links on any accessible report
        pass
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bug report triage capability required",
        )

    existing = await service.get_bug_report(db=db, report_id=report_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")

    report = await service.update_external_link(
        db=db,
        actor=user,
        report_id=report_id,
        external_link=external_link,
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found")
    return report
