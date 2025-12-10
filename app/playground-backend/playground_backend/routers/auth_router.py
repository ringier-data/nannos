"""Authentication router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..controllers.auth_controller import (
    AuthController,
    register_oauth_provider,
)
from ..db.session import get_db_session
from ..dependencies import require_auth, require_auth_or_bearer_token
from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User, UserSettingsResponse, UserSettingsUpdate
from ..services.audit_service import audit_service
from ..services.session_service import SessionService
from ..services.user_group_service import user_group_service
from ..services.user_service import UserService
from ..services.user_settings_service import user_settings_service

logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Initialize services (these will be created once and reused)
session_service: SessionService = SessionService()
user_service: UserService = UserService()
auth_controller: AuthController = AuthController(session_service, user_service)

# Register OAuth provider (must be done once at module load time)
register_oauth_provider()

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Initiate OIDC login flow.

    Query Parameters:
        redirectTo: URL to redirect to after successful login

    Returns:
        Redirect to OIDC authorization endpoint
    """
    return await auth_controller.get_login(request)


@router.get("/login-callback")
async def login_callback(request: Request, response: Response) -> RedirectResponse:
    """Handle OIDC login callback.

    Query Parameters:
        code: Authorization code from OIDC
        state: State parameter for CSRF protection

    Returns:
        Redirect to the originally requested page with session cookie set
    """
    return await auth_controller.get_login_callback(request, response)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Initiate logout flow.

    Query Parameters:
        redirectTo: URL to redirect to after logout (optional)

    Returns:
        Redirect to OIDC logout endpoint
    """
    return await auth_controller.get_logout(request)


@router.get("/logout-callback")
async def logout_callback(request: Request) -> RedirectResponse:
    """Handle OIDC logout callback.

    Query Parameters:
        state: State parameter

    Returns:
        Redirect to the specified page
    """
    return await auth_controller.get_logout_callback(request)


@router.get("/me")
async def get_current_user(
    db: DbSession,
    user: User = Depends(require_auth),
) -> dict:
    """Get the current authenticated user's information.

    Returns:
        User information including id, email, name, role, and group memberships.

    Raises:
        401 Unauthorized: If the user is not authenticated.
    """
    # Get user's group memberships with their roles
    groups = await user_group_service.get_user_group_memberships(db, user.id)

    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "company_name": user.company_name,
        "is_administrator": user.is_administrator,
        "role": user.role.value,  # Add user's system role
        "groups": groups,  # Add user's group memberships
    }


@router.get("/me/settings", response_model=UserSettingsResponse)
async def get_current_user_settings(
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> UserSettingsResponse:
    """Get the current authenticated user's settings.

    Supports both session-based authentication and Bearer token authentication.
    The orchestrator can call this endpoint on behalf of a user.

    Returns:
        User settings including language and custom_prompt.

    Raises:
        401 Unauthorized: If the user is not authenticated.
    """
    settings = await user_settings_service.get_settings(db, user.id)
    return UserSettingsResponse(data=settings)


@router.patch("/me/settings", response_model=UserSettingsResponse)
async def update_current_user_settings(
    request: UserSettingsUpdate,
    db: DbSession,
    user: User = Depends(require_auth),
) -> UserSettingsResponse:
    """Update the current authenticated user's settings.

    Args:
        request: Fields to update (language, timezone, custom_prompt)

    Returns:
        Updated user settings.

    Raises:
        401 Unauthorized: If the user is not authenticated.
    """
    settings = await user_settings_service.upsert_settings(
        db,
        user.id,
        language=request.language,
        timezone_str=request.timezone,
        custom_prompt=request.custom_prompt,
        mcp_tools=request.mcp_tools,
    )
    await db.commit()
    return UserSettingsResponse(data=settings)


class AdminModeToggleRequest(BaseModel):
    """Request model for admin mode toggle."""

    enabled: bool


class AdminModeToggleResponse(BaseModel):
    """Response model for admin mode toggle."""

    success: bool
    enabled: bool


@router.post("/admin-mode", response_model=AdminModeToggleResponse)
async def toggle_admin_mode(
    request: AdminModeToggleRequest,
    db: DbSession,
    user: User = Depends(require_auth),
) -> AdminModeToggleResponse:
    """Log admin mode toggle for audit trail.

    This endpoint is called when a user toggles admin mode on or off.
    It logs the action for audit purposes.

    Args:
        request: Admin mode toggle request with enabled state

    Returns:
        Success response with the new admin mode state.

    Raises:
        401 Unauthorized: If the user is not authenticated.
        403 Forbidden: If the user is not an administrator.
    """
    if not user.is_administrator:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can toggle admin mode",
        )

    # Log the admin mode toggle for audit
    await audit_service.log_action(
        db=db,
        actor_sub=user.sub,
        entity_type=AuditEntityType.SESSION,
        entity_id=user.sub,
        action=AuditAction.ADMIN_MODE_ACTIVATED,
        changes={"enabled": request.enabled},
    )
    await db.commit()

    logger.info(f"Admin mode {'enabled' if request.enabled else 'disabled'} by {user.email}")

    return AdminModeToggleResponse(success=True, enabled=request.enabled)
