"""Admin router for SCIM token management."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_admin
from ..models.scim_token import (
    ScimToken,
    ScimTokenCreate,
    ScimTokenCreated,
    ScimTokenDetailResponse,
    ScimTokenListResponse,
)
from ..models.user import PaginationMeta, User
from ..services.scim_token_service import ScimTokenService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/scim-tokens", tags=["admin-scim-tokens"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_scim_token_service(request: Request) -> ScimTokenService:
    """Get SCIM token service from app state."""
    return request.app.state.scim_token_service


@router.post("", response_model=ScimTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_scim_token(
    request: Request,
    db: DbSession,
    body: ScimTokenCreate,
    admin: User = Depends(require_admin),
) -> ScimTokenCreated:
    """Create a new SCIM bearer token.

    Returns the full token value. This is the only time the token is visible.
    Store it securely — it cannot be retrieved again.
    """
    service = get_scim_token_service(request)
    token = await service.create_token(
        db,
        name=body.name,
        description=body.description,
        expires_at=body.expires_at,
        actor=admin,
    )
    await db.commit()
    return token


@router.get("", response_model=ScimTokenListResponse)
async def list_scim_tokens(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
) -> ScimTokenListResponse:
    """List all SCIM tokens (active and revoked).

    Token values are masked — only the last 4 characters are shown.
    """
    service = get_scim_token_service(request)
    tokens = await service.list_tokens(db)
    return ScimTokenListResponse(
        data=tokens,
        meta=PaginationMeta(page=1, limit=len(tokens), total=len(tokens)),
    )


@router.get("/{token_id}", response_model=ScimTokenDetailResponse)
async def get_scim_token(
    request: Request,
    db: DbSession,
    token_id: int,
    _: User = Depends(require_admin),
) -> ScimTokenDetailResponse:
    """Get details of a specific SCIM token (masked value)."""
    service = get_scim_token_service(request)
    token = await service.get_token(db, token_id)
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SCIM token not found")
    return ScimTokenDetailResponse(data=token)


@router.delete("/{token_id}", response_model=ScimTokenDetailResponse)
async def revoke_scim_token(
    request: Request,
    db: DbSession,
    token_id: int,
    admin: User = Depends(require_admin),
) -> ScimTokenDetailResponse:
    """Revoke a SCIM token.

    The token is not deleted — it is marked as revoked and will no longer be accepted.
    """
    service = get_scim_token_service(request)
    token = await service.revoke_token(db, token_id, actor=admin)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SCIM token not found or already revoked",
        )
    await db.commit()
    return ScimTokenDetailResponse(data=token)
