"""Admin user management router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import require_admin
from ..models.audit import AuditAction, AuditEntityType
from ..models.user import (
    BulkUserOperationRequest,
    BulkUserOperationResponse,
    ImpersonateStartRequest,
    PaginationMeta,
    User,
    UserAdminUpdate,
    UserDetailResponse,
    UserGroupRoleUpdate,
    UserGroupsUpdate,
    UserListResponse,
    UserRoleUpdate,
    UserStatusUpdate,
)
from ..services.audit_service import AuditService
from ..services.user_group_service import UserGroupService
from ..services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_user_service(request: Request) -> UserService:
    """Get user service from app state."""
    return request.app.state.user_service


def get_audit_service(request: Request) -> AuditService:
    """Get audit service from app state."""
    return request.app.state.audit_service


def get_user_group_service(request: Request) -> UserGroupService:
    """Get user group service from app state."""
    return request.app.state.user_group_service


@router.get("", response_model=UserListResponse)
async def list_users(
    request: Request,
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
    user_service = get_user_service(request)
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
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
) -> UserDetailResponse:
    """Get a user's details with group memberships.

    Admin only endpoint.
    """
    user_service = get_user_service(request)
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
    request: Request,
    update_request: UserAdminUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update admin-controlled user fields.

    Admin only endpoint. Allows updating is_administrator flag.
    """
    user_service = get_user_service(request)
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
        actor=admin,
        is_administrator=update_request.is_administrator,
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    # Trigger outbound SCIM push (fire-and-forget)
    if hasattr(request.app.state, "outbound_scim_push_service"):
        request.app.state.outbound_scim_push_service.push_user(user_id, "update")

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
    request: Request,
    update_request: UserGroupsUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's group memberships.

    Admin only endpoint.
    Uses user_group_service to ensure Keycloak sync.
    """
    user_service = get_user_service(request)
    user_group_service = get_user_group_service(request)

    # Verify user exists
    current_user = await user_service.get_user_with_groups(db, user_id)
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Get current group IDs
    current_group_ids = {g.group_id for g in current_user.groups}
    target_group_ids = set(update_request.group_ids)

    try:
        if update_request.operation == "set":
            # Calculate groups to add and remove
            groups_to_add = list(target_group_ids - current_group_ids)
            groups_to_remove = list(current_group_ids - target_group_ids)

            # Remove members from groups they should no longer be in
            for group_id in groups_to_remove:
                await user_group_service.remove_members(
                    db=db,
                    actor=admin,
                    group_id=group_id,
                    user_ids=[user_id],
                )

            # Add member to new groups
            for group_id in groups_to_add:
                await user_group_service.add_members(
                    db=db,
                    actor=admin,
                    group_id=group_id,
                    user_ids=[user_id],
                    role=update_request.role,
                )

        elif update_request.operation == "add":
            # Add to specified groups
            groups_to_add = list(target_group_ids - current_group_ids)
            for group_id in groups_to_add:
                await user_group_service.add_members(
                    db=db,
                    actor=admin,
                    group_id=group_id,
                    user_ids=[user_id],
                    role=update_request.role,
                )

        elif update_request.operation == "remove":
            # Remove from specified groups
            groups_to_remove = list(target_group_ids & current_group_ids)
            for group_id in groups_to_remove:
                await user_group_service.remove_members(
                    db=db,
                    actor=admin,
                    group_id=group_id,
                    user_ids=[user_id],
                )

        await db.commit()

        # Return updated user with groups
        updated_user = await user_service.get_user_with_groups(db, user_id)
        if updated_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        return UserDetailResponse(data=updated_user)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.put("/{user_id}/groups/{group_id}/role", response_model=UserDetailResponse)
async def update_user_group_role(
    user_id: str,
    group_id: int,
    request: Request,
    update_request: UserGroupRoleUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's role in a specific group.

    Admin only endpoint.
    """
    user_service = get_user_service(request)
    user_group_service = get_user_group_service(request)

    # Verify user exists
    user = await user_service.get_user(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update the member's role in the group
    try:
        member = await user_group_service.update_member_role(
            db=db,
            actor=admin,
            group_id=group_id,
            user_id=user_id,
            role=update_request.role,
        )

        if member is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of this group",
            )

        await db.commit()

        # Return updated user with groups
        user_with_groups = await user_service.get_user_with_groups(db, user_id)
        if user_with_groups is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        return UserDetailResponse(data=user_with_groups)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.put("/{user_id}/role", response_model=UserDetailResponse)
async def update_user_role(
    user_id: str,
    request: Request,
    update_request: UserRoleUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's role.

    Admin only endpoint. Defines system-wide capabilities.
    """
    user_service = get_user_service(request)
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
        admin,
        update_request.role.value,  # actor_sub for audit
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    # Trigger outbound SCIM push (fire-and-forget)
    if hasattr(request.app.state, "outbound_scim_push_service"):
        request.app.state.outbound_scim_push_service.push_user(user_id, "update")

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
    request: Request,
    update_request: UserStatusUpdate,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> UserDetailResponse:
    """Update a user's status.

    Admin only endpoint.
    """
    user_service = get_user_service(request)
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
        actor=admin,
        status=update_request.status,  # actor_sub for audit
    )

    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    await db.commit()

    # Trigger outbound SCIM push (fire-and-forget)
    if hasattr(request.app.state, "outbound_scim_push_service"):
        request.app.state.outbound_scim_push_service.push_user(user_id, "update")

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
    request: Request,
    bulk_request: BulkUserOperationRequest,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> BulkUserOperationResponse:
    """Perform bulk user operations.

    Admin only endpoint.
    """
    user_service = get_user_service(request)
    results = await user_service.bulk_update_users(
        db,
        actor=admin,
        operations=bulk_request.operations,
    )
    await db.commit()

    return BulkUserOperationResponse(data=results)


@router.post("/impersonate/start")
async def start_impersonation(
    impersonate_request: ImpersonateStartRequest,
    request: Request,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> None:
    """Start impersonating another user (admin only).

    This endpoint allows administrators to impersonate other users for support and troubleshooting.
    Requires admin mode to be enabled and logs the impersonation start for audit purposes.

    Note: The admin parameter will always be the actual admin user, even if already impersonating.

    Args:
        impersonate_request: Request containing the target user ID to impersonate

    Raises:
        403 Forbidden: If admin mode is not enabled.
        404 Not Found: If the target user does not exist.
    """
    # Validate target user exists
    user_service = get_user_service(request)
    target_user = await user_service.get_user(db, impersonate_request.target_user_id)
    if not target_user:
        raise HTTPException(
            status_code=404,
            detail=f"User {impersonate_request.target_user_id} not found",
        )

    # Log the impersonation start
    audit_service = get_audit_service(request)
    await audit_service.log_action(
        db=db,
        actor=admin,
        entity_type=AuditEntityType.SESSION,
        entity_id=impersonate_request.target_user_id,
        action=AuditAction.IMPERSONATION_START,
        changes={
            "admin_user_id": admin.id,
            "admin_email": admin.email,
            "target_user_id": target_user.id,
            "target_email": target_user.email,
        },
    )
    await db.commit()

    logger.info(
        f"Impersonation started: {admin.email} (admin) -> {target_user.email} (target_user_id: {target_user.id})"
    )


@router.post("/impersonate/stop")
async def stop_impersonation(
    request: Request,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> None:
    """Stop impersonating a user (admin only).

    This endpoint stops the current impersonation session and logs the event for audit purposes.
    The admin parameter will be the original admin user if called from impersonation context,
    or the admin themselves if impersonation already ended.

    Args:
        request: FastAPI request

    Returns:
        Success response.

    Raises:
        403 Forbidden: If the user is not an administrator.
    """
    # Check if there's an original user in request state (means impersonation is active)
    original_user = getattr(request.state, "original_user", None)

    # Determine who to log as (original admin if impersonating, current user otherwise)
    actor_user = original_user if original_user else admin

    # Log the impersonation stop
    audit_service = get_audit_service(request)
    await audit_service.log_action(
        db=db,
        actor=actor_user,
        entity_type=AuditEntityType.SESSION,
        entity_id=actor_user.sub,
        action=AuditAction.IMPERSONATION_END,
        changes={
            "admin_user_id": actor_user.id,
            "admin_email": actor_user.email,
        },
    )
    await db.commit()

    logger.info(f"Impersonation stopped by {actor_user.email}")
