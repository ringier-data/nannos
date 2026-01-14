"""Group management router for non-admin users."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_db_session
from ..dependencies import (
    require_auth,
    require_group_admin_or_admin,
    require_group_member,
    require_group_member_management_permission,
)
from ..models.user import PaginationMeta, User
from ..models.user_group import (
    GroupMemberAdd,
    GroupMemberListResponse,
    GroupMemberUpdate,
    MemberInfo,
    UserGroupDetailResponse,
    UserGroupWithMembers,
)
from ..services.user_group_service import UserGroupService

router = APIRouter(prefix="/api/v1/groups", tags=["groups"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_user_group_service(request: Request) -> UserGroupService:
    """Get user group service from app state."""
    return request.app.state.user_group_service


@router.get("")
async def list_my_groups(
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> list[UserGroupWithMembers]:
    """List groups where the current user is a member.

    Requires groups.read permission.
    """
    user_group_service = get_user_group_service(request)
    # Check if user has groups.read permission
    has_permission = await user_group_service.check_user_permission(db, user.id, "groups", "read")
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions: groups.read required",
        )

    groups = await user_group_service.list_user_groups(db, user.id)
    return groups


@router.get("/{group_id}", response_model=UserGroupDetailResponse)
async def get_group(
    group_id: int,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> UserGroupDetailResponse:
    """Get group details.

    Members can see basic group info.
    Group admins can also see member list.
    """
    user_group_service = get_user_group_service(request)
    # Check if user is a member (or admin)
    await require_group_member(request, group_id, db)

    group = await user_group_service.get_group_with_members(db, group_id)

    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    # If not admin or group admin, hide member list
    is_group_admin = await user_group_service.is_group_admin(db, group_id, user.id)
    if not user.is_administrator and not is_group_admin:
        # Return group without detailed member info
        group.members = []

    return UserGroupDetailResponse(data=group)


@router.get("/{group_id}/members", response_model=GroupMemberListResponse)
async def list_members(
    group_id: int,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
) -> GroupMemberListResponse:
    """List members of a group.

    Requires group admin role or system admin.
    """
    user_group_service = get_user_group_service(request)
    # Check permission
    await require_group_admin_or_admin(request, group_id, db)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    members, total = await user_group_service.list_members(db, group_id, page=page, limit=limit)

    return GroupMemberListResponse(
        data=members,
        meta=PaginationMeta(page=page, limit=limit, total=total),
    )


@router.post("/{group_id}/members", response_model=GroupMemberListResponse)
async def add_members(
    group_id: int,
    request: Request,
    request_body: GroupMemberAdd,
    db: DbSession,
    user: User = Depends(require_auth),
) -> GroupMemberListResponse:
    """Add members to a group.

    Requires group manager role or system admin.
    """
    user_group_service = get_user_group_service(request)
    # Check permission
    await require_group_member_management_permission(request, group_id, db)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    members = await user_group_service.add_members(
        db,
        actor_sub=user.sub,
        group_id=group_id,
        user_ids=request_body.user_ids,
        role=request_body.role,
    )
    await db.commit()

    return GroupMemberListResponse(
        data=members,
        meta=PaginationMeta(page=1, limit=len(members), total=len(members)),
    )


@router.put("/{group_id}/members/{user_id}")
async def update_member_role(
    group_id: int,
    user_id: str,
    request: Request,
    request_body: GroupMemberUpdate,
    db: DbSession,
    user: User = Depends(require_auth),
) -> MemberInfo:
    """Update a member's role.

    Requires group manager role or system admin.
    """
    user_group_service = get_user_group_service(request)
    # Check permission
    await require_group_member_management_permission(request, group_id, db)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    member = await user_group_service.update_member_role(
        db,
        actor_sub=user.sub,
        group_id=group_id,
        user_id=user_id,
        role=request_body.role,
    )

    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in group",
        )

    await db.commit()

    return member


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: int,
    user_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> None:
    """Remove a member from a group.

    Requires group manager role or system admin.
    Cannot remove the last manager.
    """
    user_group_service = get_user_group_service(request)
    # Check permission
    await require_group_member_management_permission(request, group_id, db)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    try:
        success = await user_group_service.remove_member(
            db,
            actor_sub=user.sub,
            group_id=group_id,
            user_id=user_id,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Member not found in group",
            )

        await db.commit()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
