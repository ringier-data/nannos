"""Group management router for non-admin users."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..db.session import DbSession
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
    GroupMemberRemove,
    GroupMemberUpdate,
    MemberInfo,
    SubAgentAdd,
    SubAgentRefWithStatus,
    UserGroupDetailResponse,
    UserGroupWithMembers,
)
from ..services.user_group_service import UserGroupService

router = APIRouter(prefix="/api/v1/groups", tags=["groups"])


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
    await require_group_member(request, group_id, db, user)

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
    await require_group_member_management_permission(request, group_id, db, user)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    members = await user_group_service.add_members(
        db,
        actor=user,
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
    await require_group_member_management_permission(request, group_id, db, user)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    member = await user_group_service.update_member_role(
        db,
        actor=user,
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


@router.post("/{group_id}/members/remove", response_model=GroupMemberListResponse)
async def remove_members(
    group_id: int,
    request: Request,
    request_body: GroupMemberRemove,
    db: DbSession,
    user: User = Depends(require_auth),
) -> GroupMemberListResponse:
    """Remove multiple members from a group (bulk operation).

    Requires group manager role or system admin.
    Cannot remove all managers.
    All members must exist or operation fails.
    """
    user_group_service = get_user_group_service(request)
    # Check permission
    await require_group_member_management_permission(request, group_id, db, user)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    try:
        members = await user_group_service.remove_members(
            db,
            actor=user,
            group_id=group_id,
            user_ids=request_body.user_ids,
        )
        await db.commit()

        return GroupMemberListResponse(
            data=members,
            meta=PaginationMeta(page=1, limit=len(members), total=len(members)),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.get("/{group_id}/accessible-agents", response_model=list[SubAgentRefWithStatus])
async def get_group_accessible_agents(
    group_id: int,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> list[SubAgentRefWithStatus]:
    """Get all accessible approved agents for a group with default flags and status.

    Returns ALL approved agents that the group has permission to access, with indicators:
    - approval_status: approval status of the agent
    - is_default: whether this agent is set as a default for automatic activation
    - is_activated: whether the agent is currently activated for the user
    - activated_by_groups: list of group IDs that activated this agent

    Requires group member role.
    """
    user_group_service = get_user_group_service(request)
    await require_group_member(request, group_id, db, user)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    sub_agents = await user_group_service.get_group_accessible_agents(
        db=db,
        group_id=group_id,
        user_id=user.id,
    )

    return sub_agents


@router.put("/{group_id}/default-agents")
async def set_group_default_agents(
    group_id: int,
    request: Request,
    db: DbSession,
    request_body: SubAgentAdd,
    user: User = Depends(require_auth),
):
    """Set (replace) default agents for a group (bulk operation).

    Requires group manager role or system admin.
    Validates that all agents are approved and group has permissions.
    """
    user_group_service = get_user_group_service(request)
    await require_group_member_management_permission(request, group_id, db, user)

    sub_agent_ids = request_body.sub_agent_ids

    try:
        await user_group_service.set_group_default_agents(
            db=db,
            group_id=group_id,
            sub_agent_ids=sub_agent_ids,
            actor=user,
        )
        await db.commit()

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/{group_id}/default-agents/{sub_agent_id}")
async def add_group_default_agent(
    group_id: int,
    sub_agent_id: int,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
):
    """Add a single default agent to a group.

    Requires group manager role or system admin.
    Validates that the agent is approved and group has permissions.
    Activates the agent for all existing group members.
    """
    user_group_service = get_user_group_service(request)
    await require_group_member_management_permission(request, group_id, db, user)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    try:
        await user_group_service.add_group_default_agent(
            db=db,
            group_id=group_id,
            sub_agent_id=sub_agent_id,
            actor=user,
        )
        await db.commit()

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete("/{group_id}/default-agents/{sub_agent_id}")
async def remove_group_default_agent(
    group_id: int,
    sub_agent_id: int,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
):
    """Remove a single default agent from a group.

    Requires group manager role or system admin.
    Deactivates the agent for all group members.
    """
    user_group_service = get_user_group_service(request)
    await require_group_member_management_permission(request, group_id, db, user)

    # Verify group exists
    group = await user_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    try:
        await user_group_service.remove_group_default_agent(
            db=db,
            group_id=group_id,
            sub_agent_id=sub_agent_id,
            actor=user,
        )
        await db.commit()

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
