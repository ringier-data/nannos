"""Authentication dependencies for FastAPI routes."""

import logging

from fastapi import HTTPException, Request, status
from ringier_a2a_sdk.auth import JWTValidationError, JWTValidator
from sqlalchemy.ext.asyncio import AsyncSession

from .authorization import SYSTEM_ROLE_CAPABILITIES, check_action_allowed
from .config import config
from .db import get_async_session_factory
from .models.user import User, UserRole, UserStatus
from .services.user_service import UserService

logger = logging.getLogger(__name__)

# Header name for admin mode toggle
ADMIN_MODE_HEADER = "X-Admin-Mode"
# Header name for user impersonation
IMPERSONATE_USER_HEADER = "X-Impersonate-User-Id"


def get_admin_mode(request: Request) -> bool:
    """Get admin mode status from request header.

    Returns True only if the X-Admin-Mode header is set to 'true' (case-insensitive).
    """
    header_value = request.headers.get(ADMIN_MODE_HEADER, "").lower()
    return header_value == "true"


def get_impersonated_user_id(request: Request) -> str | None:
    """Get impersonated user ID from request header.

    Returns the user ID if the X-Impersonate-User-Id header is set, otherwise None.
    """
    return request.headers.get(IMPERSONATE_USER_HEADER) or None


def get_user_service(request: Request) -> UserService:
    """Get UserService instance from FastAPI app state."""
    return request.app.state.user_service


def require_auth(request: Request) -> User:
    """Dependency to require authentication.

    Raises HTTPException 401 if user is not authenticated.

    Usage:
        @app.get("/protected")
        async def protected_route(user: User = Depends(require_auth)):
            return {"user_email": user.email}
    """
    if not hasattr(request.state, "user") or not request.state.user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return request.state.user


async def require_auth_or_bearer_token(request: Request) -> User:
    """Dependency to require authentication via session OR Bearer JWT token.

    First checks for session-based user authentication (request.state.user).
    If not present, attempts to validate a Bearer token against the OIDC provider,
    extracts the user's `sub` claim, and looks up the user in the database.

    This supports the orchestrator calling on behalf of a user by passing
    the user's access token.

    Returns:
        User: The authenticated user (from session or token)

    Raises:
        HTTPException 401: If neither authentication method succeeds
        HTTPException 401: If token is valid but user not found in database

    Usage:
        @app.get("/sub-agents")
        async def list_sub_agents(
            user: User = Depends(require_auth_or_bearer_token)
        ):
            # Same user object regardless of auth method
            ...
    """
    # Try session-based authentication first
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user

    # Try Bearer token authentication (used by orchestrator on behalf of user)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        try:
            # Validate the user's access token against OIDC provider
            validator = JWTValidator(
                issuer=config.oidc.issuer,
                # Don't validate azp/aud - accept any valid token from the issuer
                # The token could be issued to the frontend, orchestrator, or other clients
                # TODO: we do not validate the audience here. Consider tightening this in the future.
                # this requires the sub-agent to exchange the token with agent-console as target audience.
            )

            payload = await validator.validate(token)
            sub = payload.get("sub")

            if not sub:
                logger.warning("Token missing 'sub' claim")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token: missing subject",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Look up user in database using service from app state
            user_service = get_user_service(request)

            async_session_factory = get_async_session_factory()
            async with async_session_factory() as db:
                user = await user_service.get_user_by_sub(db, sub)

            if not user:
                # since we trust the token is valid, we can auto-onboard the user based on the token claims
                logger.info(f"User not found for sub={sub}, auto-onboarding")
                user = await user_service.upsert_user(
                    db,
                    sub=sub,
                    email=payload.get("email", ""),
                    first_name=payload.get("given_name", ""),
                    last_name=payload.get("family_name", ""),
                    company_name=payload.get("company_name", ""),
                )

            logger.info(f"Bearer token validated for user: {user.email} (sub={sub})")
            return user

        except JWTValidationError as e:
            logger.warning(f"Bearer token validation failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_active_user(request: Request) -> User:
    """Dependency to require an active (non-suspended, non-deleted) user.

    Raises HTTPException 401 if user is not authenticated.
    Raises HTTPException 403 if user is suspended or deleted.

    Usage:
        @app.post("/sub-agents")
        async def create_sub_agent(user: User = Depends(require_active_user)):
            return {"created_by": user.email}
    """
    user = require_auth(request)

    if user.status != UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User account is {user.status.value}",
        )

    return user


def require_admin(request: Request) -> User:
    """Dependency to require admin authentication with admin mode enabled.

    Requires:
    1. User must be authenticated
    2. User must have is_administrator=True
    3. X-Admin-Mode header must be set to 'true'

    This allows admin users to operate as regular users when admin mode is disabled.

    NOTE: When impersonating, this returns the ORIGINAL ADMIN user, not the impersonated user.
    This ensures admin-only endpoints remain accessible during impersonation.

    Raises HTTPException 401 if user is not authenticated.
    Raises HTTPException 403 if:
        - User is not an administrator, or
        - Admin mode header is not enabled, or
        - Non-admin attempts to use admin mode header (privilege escalation)

    Usage:
        @app.post("/admin/users")
        async def create_user(user: User = Depends(require_admin)):
            return {"created_by": user.email}
    """
    # Check if currently impersonating - if so, use the original admin user
    if hasattr(request.state, "original_user") and request.state.original_user:
        user = request.state.original_user
    else:
        user = require_auth(request)

    admin_mode = get_admin_mode(request)

    # Detect privilege escalation attempt: non-admin trying to use admin mode header
    if admin_mode and not user.is_administrator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Privilege escalation attempt: admin mode not allowed for non-administrators",
        )

    # Must be admin AND have admin mode enabled
    if not user.is_administrator or not admin_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions or admin mode not enabled",
        )

    return user


def is_admin_mode(request: Request, user: User) -> bool:
    """Check if effective admin mode is active for a user.

    Returns True only if:
    1. User is an administrator, AND
    2. X-Admin-Mode header is set to 'true'

    Use this for inline admin checks in routers where admin privileges
    are optional (e.g., list endpoints with different visibility).

    Raises HTTPException 403 if non-admin attempts to use admin mode header.

    Usage:
        @app.get("/sub-agents")
        async def list_sub_agents(request: Request, user: User = Depends(require_auth)):
            effective_admin = is_admin_mode(request, user)
            return await service.list(is_admin=effective_admin)
    """
    # Check if currently impersonating - if so, use the original admin user for admin checks
    effective_user = (
        request.state.original_user if hasattr(request.state, "original_user") and request.state.original_user else user
    )

    admin_mode = get_admin_mode(request)

    # Detect privilege escalation attempt
    if admin_mode and not effective_user.is_administrator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Privilege escalation attempt: admin mode not allowed for non-administrators",
        )

    return effective_user.is_administrator and admin_mode


def has_capability(user: User, resource_type: str, action: str) -> bool:
    """Check if a user has a specific capability based on their role.

    Checks both is_administrator flag and role-based capabilities from SYSTEM_ROLE_CAPABILITIES.
    System admins (is_administrator=True) have all capabilities.

    Args:
        user: The user to check
        resource_type: Type of resource (e.g., 'sub_agents', 'members', 'users', 'groups')
        action: Action to check (e.g., 'read', 'write', 'approve', 'read.admin', 'approve.admin')

    Returns:
        True if user has the capability
    """
    # System admins have all capabilities
    if user.is_administrator:
        return True

    # Check role-based capabilities
    role_str = user.role.value if isinstance(user.role, UserRole) else user.role
    if role_str not in SYSTEM_ROLE_CAPABILITIES:
        return False

    role_capabilities = SYSTEM_ROLE_CAPABILITIES[role_str]
    if resource_type not in role_capabilities:
        return False

    return action in role_capabilities[resource_type]


def require_approver(request: Request) -> User:
    """Dependency to require approver authentication with admin mode enabled.

    According to SYSTEM_ROLE_CAPABILITIES, users with 'approver' or 'admin' role
    can approve sub-agents when admin-mode is enabled. System admins (is_administrator=True)
    can also approve.

    Requires:
    1. User must be authenticated
    2. User must have capability for 'sub_agents.approve' or 'sub_agents.approve.admin'
    3. X-Admin-Mode header must be set to 'true'

    This allows approvers to operate as regular users when admin mode is disabled,
    preventing accidental approvals.

    Raises HTTPException 401 if user is not authenticated.
    Raises HTTPException 403 if:
        - User doesn't have approval permissions, or
        - Admin mode header is not enabled, or
        - Non-approver attempts to use admin mode header (privilege escalation)

    Usage:
        @app.post("/sub-agents/{id}/approve")
        async def approve_sub_agent(user: User = Depends(require_approver)):
            return {"approved_by": user.email}
    """
    # Check if currently impersonating - if so, use the original admin user for approval checks
    if hasattr(request.state, "original_user") and request.state.original_user:
        user = request.state.original_user
    else:
        user = require_auth(request)

    admin_mode = get_admin_mode(request)

    # Check if user has approval capabilities (either regular approve or approve.admin)
    can_approve = has_capability(user, "sub_agents", "approve") or has_capability(user, "sub_agents", "approve.admin")

    # Detect privilege escalation attempt: non-approver trying to use admin mode header
    if admin_mode and not can_approve:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Privilege escalation attempt: admin mode not allowed for users without approval permissions",
        )

    # Must have approval permissions AND have admin mode enabled
    if not can_approve or not admin_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions or admin mode not enabled. Approval requires approver/admin role with admin mode enabled.",
        )

    return user


class GroupAdminChecker:
    """Dependency class to check if user is group admin or system admin.

    Usage:
        @app.post("/groups/{group_id}/members")
        async def add_member(
            group_id: int,
            user: User = Depends(require_auth),
            db: AsyncSession = Depends(get_db_session),
            _: None = Depends(GroupAdminChecker(group_id_param="group_id")),
        ):
            ...
    """

    def __init__(self, group_id_param: str = "group_id"):
        """Initialize with the name of the path parameter containing group_id."""
        self.group_id_param = group_id_param

    async def __call__(
        self,
        request: Request,
    ) -> User:
        """Check if user is group admin or system admin."""
        user_group_service = request.app.state.user_group_service

        user = require_auth(request)

        # System admins can do anything
        if user.is_administrator:
            return user

        # Get group_id from path parameters
        group_id = request.path_params.get(self.group_id_param)
        if group_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing {self.group_id_param} path parameter",
            )

        try:
            group_id = int(group_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid group_id",
            )

        # Get DB session from app state
        async_session_factory = request.app.state.async_session_factory
        async with async_session_factory() as db:
            is_admin = await user_group_service.is_group_admin(db, group_id, user.id)

        if not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Must be group admin or system admin",
            )

        return user


async def require_group_admin_or_admin(
    request: Request,
    group_id: int,
    db: AsyncSession,
) -> User:
    """Check if user is group admin or system admin.

    Args:
        request: FastAPI request
        group_id: Group ID
        db: Database session

    Returns:
        Authenticated user

    Raises:
        HTTPException 403 if user is not group admin or system admin
    """
    user_group_service = request.app.state.user_group_service

    user = require_auth(request)

    # System admins can do anything
    if user.is_administrator:
        return user

    # Check if user is group admin
    is_admin = await user_group_service.is_group_admin(db, group_id, user.id)
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Must be group admin or system admin",
        )

    return user


async def require_group_member_management_permission(
    request: Request,
    group_id: int,
    db: AsyncSession,
    user: User,
) -> User:
    """Check if user can manage members in a group.

    Requires either:
    - System admin
    - Group manager role (which has 'write' permission on members)

    Args:
        request: FastAPI request
        group_id: Group ID
        db: Database session
        user: Authenticated user (from route-level dependency injection)

    Returns:
        Authenticated user

    Raises:
        HTTPException 403 if user doesn't have permission
    """
    user_group_service = request.app.state.user_group_service

    # System admins can do anything
    if user.is_administrator:
        return user

    # Check if user is group manager
    is_manager = await user_group_service.is_group_manager(db, group_id, user.id)
    if not is_manager:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Must be group manager or system admin to manage members",
        )

    # Verify the manager role has write capability on members
    if not check_action_allowed("manager", "members", "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Group managers cannot perform write action on members",
        )

    return user


async def require_group_member(
    request: Request,
    group_id: int,
    db: AsyncSession,
    user: User,
) -> User:
    """Check if user is a member of the group.

    Args:
        request: FastAPI request
        group_id: Group ID
        db: Database session
        user: Authenticated user

    Returns:
        Authenticated user

    Raises:
        HTTPException 403 if user is not a group member
    """
    user_group_service = request.app.state.user_group_service

    # System admins can access everything
    if user.is_administrator:
        return user

    # Check if user is group member
    is_member = await user_group_service.is_group_member(db, group_id, user.id)
    if not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Must be a group member",
        )

    return user
