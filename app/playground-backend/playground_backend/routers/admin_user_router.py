"""Admin user management router."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_admin
from ..models.user import (
    BulkUserOperationRequest,
    BulkUserOperationResponse,
    PaginationMeta,
    User,
    UserAdminUpdate,
    UserDetailResponse,
    UserGroupsUpdate,
    UserListResponse,
    UserRoleUpdate,
    UserStatusUpdate,
)
from ..services.user_service import user_service

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get("", response_model=UserListResponse)
async def list_users(
    db: DbSession,
    _: User = Depends(require_admin),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    search: str | None = Query(None, description="Search by name or email"),
    group_id: int | None = Query(None, description="Filter by group membership"),
) -> UserListResponse:
    """List all users with pagination and filtering.

    Admin only endpoint.
    """
    users, total = await user_service.list_users(
        db,
        page=page,
        limit=limit,
        search=search,
        group_id=group_id,
    )

    return UserListResponse(
        data=users,
        meta=PaginationMeta(page=page, limit=limit, total=total),
    )


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user(
    user_id: str,
    db: DbSession,
    _: User = Depends(require_admin),
) -> UserDetailResponse:
    """Get a user's details with group memberships.

    Admin only endpoint.
    """
    user = await user_service.get_user_with_groups(db, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserDetailResponse(data=user)


@router.patch("/{user_id}", response_model=UserDetailResponse)
async def update_user(
    user_id: str,
    request: UserAdminUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update admin-controlled user fields.

    Admin only endpoint. Allows updating is_administrator flag.
    """
    # Get current user state for audit
    current_user = await user_service.get_user(db, user_id)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update user fields
    updated_user = await user_service.update_user_admin_fields(
        db,
        user_id,
        admin.sub,  # actor_sub for audit
        is_administrator=request.is_administrator,
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    # Return user with groups
    user_with_groups = await user_service.get_user_with_groups(db, user_id)
    if user_with_groups is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserDetailResponse(data=user_with_groups)


@router.put("/{user_id}/groups", response_model=UserDetailResponse)
async def update_user_groups(
    user_id: str,
    request: UserGroupsUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's group memberships.

    Admin only endpoint.
    """
    # Get current user state for audit
    current_user = await user_service.get_user_with_groups(db, user_id)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update groups
    updated_user = await user_service.update_user_groups(
        db,
        user_id,
        admin.sub,  # actor_sub for audit
        request.group_ids,
        request.operation,
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    return UserDetailResponse(data=updated_user)


@router.put("/{user_id}/role", response_model=UserDetailResponse)
async def update_user_role(
    user_id: str,
    request: UserRoleUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's role.

    Admin only endpoint. Defines system-wide capabilities.
    """
    # Get current user state for audit
    current_user = await user_service.get_user(db, user_id)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update role
    updated_user = await user_service.update_user_role(
        db,
        user_id,
        admin.sub,
        request.role.value,  # actor_sub for audit
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    # Return user with groups
    user_with_groups = await user_service.get_user_with_groups(db, user_id)
    if user_with_groups is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserDetailResponse(data=user_with_groups)


@router.put("/{user_id}/status", response_model=UserDetailResponse)
async def update_user_status(
    user_id: str,
    request: UserStatusUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's status.

    Admin only endpoint.
    """
    # Get current user state for audit
    current_user = await user_service.get_user(db, user_id)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update status
    updated_user = await user_service.update_user_status(
        db,
        user_id,
        admin.sub,
        request.status,  # actor_sub for audit
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    # Return user with groups
    user_with_groups = await user_service.get_user_with_groups(db, user_id)
    if user_with_groups is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserDetailResponse(data=user_with_groups)


@router.post("/bulk", response_model=BulkUserOperationResponse)
async def bulk_update_users(
    request: BulkUserOperationRequest,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> BulkUserOperationResponse:
    """Perform bulk user operations.

    Admin only endpoint.
    """
    results = await user_service.bulk_update_users(
        db,
        admin.sub,
        request.operations,  # actor_sub for audit
    )
    await db.commit()

    return BulkUserOperationResponse(data=results)
