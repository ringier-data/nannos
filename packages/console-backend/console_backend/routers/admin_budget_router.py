"""Admin API for the Budget Guard (monthly USD spend cap).

- GET/PUT `/settings` — admin-only configuration (enabled, monthly limit, warning thresholds).
- GET  `/status`     — live spend vs limit + lock decision. Open to admins (renders the
  gauge in the console) OR the orchestrator service identity (polls it to enforce the lock),
  via `require_admin_or_orchestrator` — so the orchestrator's background poll needs no user
  context, just its OIDC client-credentials token (azp = orchestrator).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..db.session import DbSession
from ..dependencies import require_admin, require_admin_or_orchestrator
from ..models.budget import BudgetSettings, BudgetSettingsUpdate, BudgetStatus
from ..models.user import User
from ..services.budget_service import BudgetService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/budget", tags=["admin-budget"])


def get_budget_service(request: Request) -> BudgetService:
    """Get budget service from app state."""
    return request.app.state.budget_service


@router.get("/settings", response_model=BudgetSettings)
async def get_budget_settings(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
) -> BudgetSettings:
    """Return the current Budget Guard configuration."""
    return await get_budget_service(request).get_settings(db)


@router.put("/settings", response_model=BudgetSettings)
async def update_budget_settings(
    request: Request,
    body: BudgetSettingsUpdate,
    db: DbSession,
    user: User = Depends(require_admin),
) -> BudgetSettings:
    """Apply a partial, audited update to the Budget Guard configuration."""
    try:
        return await get_budget_service(request).update_settings(db, actor=user, update=body)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/status", response_model=BudgetStatus)
async def get_budget_status(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin_or_orchestrator),
) -> BudgetStatus:
    """Live month-to-date spend vs limit + lock decision.

    Consumed by the admin page (gauge) and the orchestrator's enforcement poll. The
    `is_locked` field is the single source of truth the orchestrator enforces against.
    """
    return await get_budget_service(request).get_status(db)
