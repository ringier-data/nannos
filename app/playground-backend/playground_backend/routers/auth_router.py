"""Authentication router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..controllers.auth_controller import AuthController, register_oauth_provider
from ..db.session import get_db_session
from ..dependencies import require_auth, require_auth_or_bearer_token
from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User, UserSettingsResponse, UserSettingsUpdate
from ..services.audit_service import AuditService
from ..services.session_service import SessionService
from ..services.user_group_service import UserGroupService
from ..services.user_service import UserService
from ..services.user_settings_service import _UNSET, UserSettingsService

logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def get_user_settings_service(request: Request) -> UserSettingsService:
    """Get user settings service from app state."""
    return request.app.state.user_settings_service


def get_session_service(request: Request) -> SessionService:
    """Get session service from app state."""
    return request.app.state.session_service


def get_user_service(request: Request) -> UserService:
    """Get user service from app state."""
    return request.app.state.user_service


def get_audit_service(request: Request) -> AuditService:
    """Get audit service from app state."""
    return request.app.state.audit_service


def get_user_group_service(request: Request) -> UserGroupService:
    """Get user group service from app state."""
    return request.app.state.user_group_service


def get_auth_controller(request: Request) -> AuthController:
    session_service = get_session_service(request)
    user_service = get_user_service(request)
    auth_controller = AuthController(session_service=session_service, user_service=user_service)
    return auth_controller


# Initialize auth controller (needs to be created at module load time for OAuth registration)
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
    auth_controller = get_auth_controller(request)
    return await auth_controller.get_login(request)


@router.get("/login-callback")
async def login_callback(request: Request, response: Response, db: DbSession) -> RedirectResponse:
    """Handle OIDC login callback.

    Query Parameters:
        code: Authorization code from OIDC
        state: State parameter for CSRF protection

    Returns:
        Redirect to the originally requested page with session cookie set
    """
    auth_controller = get_auth_controller(request)
    return await auth_controller.get_login_callback(request, response, db=db)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Initiate logout flow.

    Query Parameters:
        redirectTo: URL to redirect to after logout (optional)

    Returns:
        Redirect to OIDC logout endpoint
    """
    auth_controller = get_auth_controller(request)
    return await auth_controller.get_logout(request)


@router.get("/logout-callback")
async def logout_callback(request: Request) -> RedirectResponse:
    """Handle OIDC logout callback.

    Query Parameters:
        state: State parameter

    Returns:
        Redirect to the specified page
    """
    auth_controller = get_auth_controller(request)
    return await auth_controller.get_logout_callback(request)


@router.get("/me")
async def get_current_user(
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> dict:
    """Get the current authenticated user's information.

    Returns:
        User information including id, email, name, role, and group memberships.

    Raises:
        401 Unauthorized: If the user is not authenticated.
    """
    user_group_service = get_user_group_service(request)
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
    request: Request,
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
    user_settings_service = get_user_settings_service(request)
    settings = await user_settings_service.get_settings(db, user.id)
    return UserSettingsResponse(data=settings)


@router.patch("/me/settings", response_model=UserSettingsResponse)
async def update_current_user_settings(
    update_request: UserSettingsUpdate,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> UserSettingsResponse:
    """Update the current authenticated user's settings.

    Args:
        update_request: Fields to update (language, timezone, custom_prompt, etc.)
                       Fields not provided will keep current values.
                       Fields explicitly set to null will be cleared.

    Returns:
        Updated user settings.

    Raises:
        401 Unauthorized: If the user is not authenticated.
    """

    user_settings_service = get_user_settings_service(request)

    # Use model_fields_set to detect which fields were explicitly provided
    # If field is in model_fields_set, pass its value (including None)
    # If field is not in model_fields_set, pass _UNSET to keep current value
    settings = await user_settings_service.upsert_settings(
        db,
        user.id,
        language=update_request.language if "language" in update_request.model_fields_set else _UNSET,
        timezone_str=update_request.timezone if "timezone" in update_request.model_fields_set else _UNSET,
        custom_prompt=update_request.custom_prompt if "custom_prompt" in update_request.model_fields_set else _UNSET,
        mcp_tools=update_request.mcp_tools if "mcp_tools" in update_request.model_fields_set else _UNSET,
        preferred_model=update_request.preferred_model
        if "preferred_model" in update_request.model_fields_set
        else _UNSET,
        enable_thinking=update_request.enable_thinking
        if "enable_thinking" in update_request.model_fields_set
        else _UNSET,
        thinking_level=update_request.thinking_level if "thinking_level" in update_request.model_fields_set else _UNSET,
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
    toggle_request: AdminModeToggleRequest,
    request: Request,
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

    audit_service = get_audit_service(request)
    # Log the admin mode toggle for audit
    await audit_service.log_action(
        db=db,
        actor=user,
        entity_type=AuditEntityType.SESSION,
        entity_id=user.sub,
        action=AuditAction.ADMIN_MODE_ACTIVATED,
        changes={"enabled": toggle_request.enabled},
    )
    await db.commit()

    logger.info(f"Admin mode {'enabled' if toggle_request.enabled else 'disabled'} by {user.email}")

    return AdminModeToggleResponse(success=True, enabled=toggle_request.enabled)


class ImpersonateStartRequest(BaseModel):
    """Request model for starting user impersonation."""

    target_user_id: str


class ImpersonateResponse(BaseModel):
    """Response model for impersonation endpoints."""

    success: bool
    message: str


@router.post("/impersonate/start", response_model=ImpersonateResponse)
async def start_impersonation(
    impersonate_request: ImpersonateStartRequest,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> ImpersonateResponse:
    """Start impersonating another user (admin only).

    This endpoint allows administrators to impersonate other users for support and troubleshooting.
    Requires admin mode to be enabled and logs the impersonation start for audit purposes.

    Args:
        impersonate_request: Request containing the target user ID to impersonate

    Returns:
        Success response with impersonation details.

    Raises:
        401 Unauthorized: If the user is not authenticated.
        403 Forbidden: If the user is not an administrator or admin mode is not enabled.
        404 Not Found: If the target user does not exist.
    """
    from ..dependencies import get_admin_mode

    # Require admin with admin mode enabled
    admin_mode = get_admin_mode(request)
    if not user.is_administrator or not admin_mode:
        raise HTTPException(
            status_code=403,
            detail="Admin mode must be enabled to impersonate users",
        )

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
        actor=user,
        entity_type=AuditEntityType.SESSION,
        entity_id=impersonate_request.target_user_id,
        action=AuditAction.IMPERSONATION_START,
        changes={
            "admin_user_id": user.id,
            "admin_email": user.email,
            "target_user_id": target_user.id,
            "target_email": target_user.email,
        },
    )
    await db.commit()

    logger.info(
        f"Impersonation started: {user.email} (admin) -> {target_user.email} (target_user_id: {target_user.id})"
    )

    return ImpersonateResponse(
        success=True,
        message=f"Successfully started impersonating {target_user.email}",
    )


@router.post("/impersonate/stop", response_model=ImpersonateResponse)
async def stop_impersonation(
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> ImpersonateResponse:
    """Stop impersonating a user (admin only).

    This endpoint stops the current impersonation session and logs the event for audit purposes.
    The user parameter will be the original admin user if called from impersonation context,
    or the admin themselves if impersonation already ended.

    Args:
        request: FastAPI request

    Returns:
        Success response.

    Raises:
        401 Unauthorized: If the user is not authenticated.
        403 Forbidden: If the user is not an administrator.
    """
    if not user.is_administrator:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can stop impersonation",
        )

    # Check if there's an original user in request state (means impersonation is active)
    original_user = getattr(request.state, "original_user", None)

    # Determine who to log as (original admin if impersonating, current user otherwise)
    actor_user = original_user if original_user else user

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

    return ImpersonateResponse(
        success=True,
        message="Successfully stopped impersonation",
    )
