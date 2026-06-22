"""Admin System Status — a readiness view of every gated/optional feature.

Answers "what's enabled, what's degraded, and what's required to enable it" in one place,
so an admin doesn't have to infer from scattered banners (or, worse, from a stale catalog
that looks healthy while its embedding model is no longer registered). See
services/feature_status.py for the per-feature evaluation.
"""

import logging

from fastapi import APIRouter, Depends, Request

from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.user import User
from ..services.feature_status import collect_system_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/system-status", tags=["admin-system-status"])


@router.get("")
async def get_system_status(
    request: Request,
    db: DbSession,
    _user: User = Depends(require_admin),
) -> dict:
    """Per-feature readiness (ready / degraded / disabled) with remediation hints."""
    features = await collect_system_status(request, db)
    return {"features": [f.as_dict() for f in features]}
