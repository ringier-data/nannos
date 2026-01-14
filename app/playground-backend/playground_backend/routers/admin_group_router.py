"""Admin group management router."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_admin
from ..models.user import PaginationMeta, User
from ..models.user_group import (
    BulkGroupDelete,
    BulkGroupDeleteResponse,
    UserGroupCreate,
    UserGroupDetailResponse,
    UserGroupListResponse,
    UserGroupUpdate,
)
from ..services.user_group_service import UserGroupService

router = APIRouter(prefix="/api/v1/admin/groups", tags=["admin-groups"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_user_group_service(request: Request) -> UserGroupService:
    """Get user group service from app state."""
    return request.app.state.user_group_service


@router.get("", response_model=UserGroupListResponse)
async def list_groups(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    search: str | None = Query(None, description="Search by name"),
) -> UserGroupListResponse:
    """List all groups with pagination.

    Admin only endpoint.
    """
    user_group_service = get_user_group_service(request)
    groups, total = await user_group_service.list_groups(
        db,
        page=page,
        limit=limit,
        search=search,
    )

    return UserGroupListResponse(
        data=groups,
        meta=PaginationMeta(page=page, limit=limit, total=total),
    )


@router.get("/{group_id}", response_model=UserGroupDetailResponse)
async def get_group(
    group_id: int,
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
) -> UserGroupDetailResponse:
    """Get a group's details with members.

    Admin only endpoint.
    """
    user_group_service = get_user_group_service(request)
    group = await user_group_service.get_group_with_members(db, group_id)

    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    return UserGroupDetailResponse(data=group)


@router.post("", response_model=UserGroupDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    request: Request,
    create_request: UserGroupCreate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserGroupDetailResponse:
    """Create a new group.

    Admin only endpoint.
    """
    user_group_service = get_user_group_service(request)
    try:
        group = await user_group_service.create_group(
            db,
            actor_sub=admin.sub,
            name=create_request.name,
            description=create_request.description,
        )
        await db.commit()

        group_with_members = await user_group_service.get_group_with_members(db, group.id)
        if group_with_members is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve created group",
            )

        return UserGroupDetailResponse(data=group_with_members)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        if "unique constraint" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A group with this name already exists",
            )
        raise


@router.put("/{group_id}", response_model=UserGroupDetailResponse)
async def update_group(
    group_id: int,
    request: Request,
    update_request: UserGroupUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserGroupDetailResponse:
    """Update a group.

    Admin only endpoint.
    """
    user_group_service = get_user_group_service(request)
    try:
        # Build kwargs with only provided fields
        update_kwargs = {}
        if update_request.name is not None:
            update_kwargs["name"] = update_request.name
        if update_request.description is not None:
            update_kwargs["description"] = update_request.description

        updated_group = await user_group_service.update_group(
            db,
            actor_sub=admin.sub,
            group_id=group_id,
            **update_kwargs,
        )

        if updated_group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Group not found",
            )

        await db.commit()

        group_with_members = await user_group_service.get_group_with_members(db, group_id)
        if group_with_members is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve updated group",
            )

        return UserGroupDetailResponse(data=group_with_members)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        if "unique constraint" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A group with this name already exists",
            )
        raise


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    request: Request,
    db: DbSession,
    admin: User = Depends(require_admin),
    force: bool = Query(False, description="Force delete even if sub-agents are assigned"),
) -> None:
    """Delete a group (soft delete).

    Admin only endpoint.
    """
    user_group_service = get_user_group_service(request)
    try:
        success = await user_group_service.delete_group(
            db,
            actor_sub=admin.sub,
            group_id=group_id,
            force=force,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Group not found",
            )

        await db.commit()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.delete("/bulk", response_model=BulkGroupDeleteResponse)
async def bulk_delete_groups(
    request: Request,
    delete_request: BulkGroupDelete,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> BulkGroupDeleteResponse:
    """Bulk delete groups.

    Admin only endpoint.
    """
    user_group_service = get_user_group_service(request)
    results = await user_group_service.bulk_delete_groups(
        db,
        actor_sub=admin.sub,
        group_ids=delete_request.group_ids,
        force=delete_request.force,
    )
    await db.commit()

    return BulkGroupDeleteResponse(data=results)
