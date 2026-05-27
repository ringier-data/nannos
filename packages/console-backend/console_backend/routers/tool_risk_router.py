"""Router for tool risk scores API.

Provides paginated read access for the in-memory cache refresh loop,
single-score lookup, upsert (from orchestrator after LLM scoring),
and admin invalidation.

Endpoints:
- GET  /api/mcp/tools/risk-scores         — paginated scores (cache refresh)
- GET  /api/mcp/tools/risk-scores/{tool_name}/{server_slug}  — single score
- PUT  /api/mcp/tools/risk-scores          — upsert score (orchestrator)
- DELETE /api/mcp/tools/risk-scores/{tool_name}/{server_slug} — invalidate (admin)
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db.session import DbSession
from ..dependencies import require_admin, require_admin_or_orchestrator, require_auth_or_bearer_token
from ..models.user import User
from ..services.tool_risk_service import ToolRiskService

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api/mcp/tools/risk-scores", tags=["tool-risk-scores"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class ToolRiskScoreResponse(BaseModel):
    """Response model for a single tool risk score."""

    tool_name: str
    server_slug: str
    schema_hash: str
    base_score: float
    risk_factors: dict[str, Any]
    allowed_actions: list[str]
    updated_at: str  # ISO datetime string
    created_at: str


class ToolRiskScoreUpsertRequest(BaseModel):
    """Request model for upserting a tool risk score."""

    tool_name: str = Field(..., min_length=1, max_length=256)
    server_slug: str = Field(..., min_length=1, max_length=256)
    schema_hash: str = Field(default="", max_length=64)
    base_score: float = Field(..., ge=0.0, le=1.0)
    risk_factors: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=lambda: ["approve", "edit", "reject"])


class PaginatedRiskScoresResponse(BaseModel):
    """Paginated response for risk scores."""

    items: list[ToolRiskScoreResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_tool_risk_service(request: Request) -> ToolRiskService:
    """Get tool risk service from app state."""
    return request.app.state.tool_risk_service


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedRiskScoresResponse)
async def list_risk_scores(
    request: Request,
    db: DbSession,
    _user: User = Depends(require_auth_or_bearer_token),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PaginatedRiskScoresResponse:
    """Get paginated tool risk scores sorted by updated_at desc.

    Used by the orchestrator's ToolRiskCache periodic refresh.
    """
    service = get_tool_risk_service(request)
    items = await service.get_scores_paginated(db, limit=limit, offset=offset)
    total = await service.get_count(db)

    return PaginatedRiskScoresResponse(
        items=[_row_to_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{tool_name}/{server_slug}", response_model=ToolRiskScoreResponse)
async def get_risk_score(
    request: Request,
    tool_name: str,
    server_slug: str,
    db: DbSession,
    _user: User = Depends(require_auth_or_bearer_token),
) -> ToolRiskScoreResponse:
    """Get a single risk score by tool_name and server_slug."""
    service = get_tool_risk_service(request)
    row = await service.get_score(db, tool_name, server_slug)
    if row is None:
        raise HTTPException(status_code=404, detail="Risk score not found")
    return _row_to_response(row)


@router.put("", response_model=ToolRiskScoreResponse)
async def upsert_risk_score(
    request: Request,
    body: ToolRiskScoreUpsertRequest,
    db: DbSession,
    user: User = Depends(require_admin_or_orchestrator),
) -> ToolRiskScoreResponse:
    """Upsert a tool risk score.

    Requires admin privileges or orchestrator service identity.
    Called by the orchestrator after LLM scoring a new tool.
    Also used by admins to manually override scores via the UI.
    """
    service = get_tool_risk_service(request)
    row = await service.upsert_score(
        db,
        actor=user,
        tool_name=body.tool_name,
        server_slug=body.server_slug,
        schema_hash=body.schema_hash,
        base_score=body.base_score,
        risk_factors=body.risk_factors,
        allowed_actions=body.allowed_actions,
    )
    return _row_to_response(row)


@router.delete("/{tool_name}/{server_slug}", status_code=204)
async def delete_risk_score(
    request: Request,
    tool_name: str,
    server_slug: str,
    db: DbSession,
    user: User = Depends(require_admin),
) -> None:
    """Delete/invalidate a risk score (admin only).

    Forces re-scoring on next tool encounter.
    """
    service = get_tool_risk_service(request)
    deleted = await service.delete_score(db, actor=user, tool_name=tool_name, server_slug=server_slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="Risk score not found")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_response(row: dict[str, Any]) -> ToolRiskScoreResponse:
    """Convert a DB row dict to the response model."""
    updated_at = row.get("updated_at")
    created_at = row.get("created_at")
    return ToolRiskScoreResponse(
        tool_name=row["tool_name"],
        server_slug=row["server_slug"],
        schema_hash=row.get("schema_hash", ""),
        base_score=row["base_score"],
        risk_factors=row.get("risk_factors", {}),
        allowed_actions=row.get("allowed_actions", ["approve", "edit", "reject"]),
        updated_at=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
    )
