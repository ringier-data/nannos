"""Skill Activations REST API endpoints.

Provides endpoints for managing skill activations on agents:
- List activations for an agent (with update-available status)
- Activate a registry skill on an agent
- Deactivate a skill from an agent
- Pull latest from registry (update activation)
- Bulk update multiple activations
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.db.session import get_db_session
from console_backend.dependencies import require_auth
from console_backend.models.skills_registry import (
    SkillActivationListResponse,
    SkillActivationRequest,
)
from console_backend.models.user import User
from console_backend.services.skill_activation_service import SkillActivationService
from console_backend.services.skill_registry_service import SkillRegistryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/skills/activations", tags=["skill-activations"])


def _get_activation_service(request: Request) -> "SkillActivationService":
    return request.app.state.skill_activation_service


def _get_registry_service(request: Request) -> "SkillRegistryService":
    return request.app.state.skill_registry_service


def _get_user_group_service(request: Request):
    return request.app.state.user_group_service


async def _get_user_group_ids(request: Request, db: AsyncSession, user: User) -> list[int]:
    """Get group IDs the user belongs to."""
    user_group_service = _get_user_group_service(request)
    groups = await user_group_service.list_user_groups(db, user.id)
    return [g.id for g in groups] if groups else []


# --- Response Models ---


class ActivationUpdateResponse(BaseModel):
    """Response after updating an activation."""

    id: int
    new_hash: str
    message: str


class BulkUpdateRequest(BaseModel):
    """Request to update multiple activations at once."""

    activation_ids: list[int] = Field(description="List of activation IDs to update")


class BulkUpdateResponse(BaseModel):
    """Response for bulk update."""

    updated: list[int] = Field(default_factory=list, description="IDs that were updated")
    failed: list[dict] = Field(default_factory=list, description="IDs that failed with reason")


# --- Endpoints ---


@router.get("/{sub_agent_id}", response_model=SkillActivationListResponse)
async def list_activations(
    sub_agent_id: int,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
) -> SkillActivationListResponse:
    """List all skill activations for an agent.

    Returns personal activations for the current user, group activations
    for user's groups, and sub-agent-scoped activations. Each item includes
    an `update_available` flag when the registry has newer content.
    """
    group_ids = await _get_user_group_ids(request, db, user)

    activation_service = _get_activation_service(request)
    items = await activation_service.list_for_agent(
        db=db,
        sub_agent_id=sub_agent_id,
        user_id=user.id,
        group_ids=group_ids,
    )

    return SkillActivationListResponse(items=items, total=len(items))


@router.post("", status_code=status.HTTP_201_CREATED)
async def activate_skill(
    body: SkillActivationRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Activate a registry skill on an agent.

    Creates an activation record and writes the skill snapshot to docstore.
    The activation is pinned to the current content hash.
    """
    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    if body.scope == "group" and not body.group_id:
        raise HTTPException(status_code=400, detail="group_id is required for group scope")

    # Verify registry entry exists
    registry_service = _get_registry_service(request)
    entry = await registry_service.get_by_id(db, body.registry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Registry skill not found")

    # Get agent name for docstore key
    from sqlalchemy import text as sa_text

    result = await db.execute(
        sa_text("SELECT name FROM sub_agents WHERE id = :id AND deleted_at IS NULL"),
        {"id": body.sub_agent_id},
    )
    agent_name = result.scalar_one_or_none()
    if not agent_name:
        raise HTTPException(status_code=404, detail="Sub-agent not found")

    # Activate
    activation_service = _get_activation_service(request)
    try:
        activation_id = await activation_service.activate(
            db=db,
            registry_id=body.registry_id,
            sub_agent_id=body.sub_agent_id,
            agent_name=agent_name,
            scope=body.scope,
            user_id=user.id,
            group_id=body.group_id,
            activated_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    await db.commit()

    return {"id": activation_id, "skill": entry.slug, "scope": body.scope, "activated": True}


@router.delete("/{activation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_skill(
    activation_id: int,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Deactivate a skill from an agent.

    Removes the activation record and cleans up appropriately based on scope:
    - Personal/group: removes docstore snapshot
    - Sub-agent: removes skill from config version
    The registry entry is preserved for other consumers.
    """
    activation_service = _get_activation_service(request)

    # Get agent name for docstore cleanup
    from sqlalchemy import text as sa_text

    result = await db.execute(
        sa_text("""
            SELECT sa.sub_agent_id, s.name as agent_name
            FROM skill_activations sa
            JOIN sub_agents s ON s.id = sa.sub_agent_id
            WHERE sa.id = :id
        """),
        {"id": activation_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Activation not found")

    try:
        deactivated = await activation_service.deactivate(
            db=db,
            activation_id=activation_id,
            agent_name=row["agent_name"],
            user_id=user.id,
            sub_agent_id=row["sub_agent_id"],
            actor=user,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deactivated:
        raise HTTPException(status_code=404, detail="Activation not found")

    await db.commit()


@router.post("/{activation_id}/update", response_model=ActivationUpdateResponse)
async def update_activation(
    activation_id: int,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
) -> ActivationUpdateResponse:
    """Pull latest content from registry for this activation.

    Updates the activation's content hash and refreshes the docstore snapshot.
    Cannot update sub-agent-scoped activations (managed by config versions).
    """
    activation_service = _get_activation_service(request)

    # Get agent name
    from sqlalchemy import text as sa_text

    result = await db.execute(
        sa_text("""
            SELECT s.name as agent_name
            FROM skill_activations sa
            JOIN sub_agents s ON s.id = sa.sub_agent_id
            WHERE sa.id = :id
        """),
        {"id": activation_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Activation not found")

    try:
        new_hash = await activation_service.update_activation(
            db=db,
            activation_id=activation_id,
            agent_name=row["agent_name"],
            user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not new_hash:
        raise HTTPException(status_code=404, detail="Activation not found")

    await db.commit()

    return ActivationUpdateResponse(
        id=activation_id,
        new_hash=new_hash,
        message="Activation updated to latest registry content.",
    )


@router.post("/bulk-update", response_model=BulkUpdateResponse)
async def bulk_update_activations(
    body: BulkUpdateRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
) -> BulkUpdateResponse:
    """Update multiple activations to their latest registry content.

    Skips sub-agent-scoped activations and reports failures individually.
    """
    activation_service = _get_activation_service(request)
    updated: list[int] = []
    failed: list[dict] = []

    for activation_id in body.activation_ids:
        # Get agent name for each
        from sqlalchemy import text as sa_text

        result = await db.execute(
            sa_text("""
                SELECT s.name as agent_name
                FROM skill_activations sa
                JOIN sub_agents s ON s.id = sa.sub_agent_id
                WHERE sa.id = :id
            """),
            {"id": activation_id},
        )
        row = result.mappings().first()
        if not row:
            failed.append({"id": activation_id, "reason": "Activation not found"})
            continue

        try:
            new_hash = await activation_service.update_activation(
                db=db,
                activation_id=activation_id,
                agent_name=row["agent_name"],
                user_id=user.id,
            )
            if new_hash:
                updated.append(activation_id)
            else:
                failed.append({"id": activation_id, "reason": "Activation not found"})
        except ValueError as e:
            failed.append({"id": activation_id, "reason": str(e)})

    await db.commit()

    return BulkUpdateResponse(updated=updated, failed=failed)
