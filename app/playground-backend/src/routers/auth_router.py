"""Authentication router."""

import logging

from controllers.auth_controller import (
    AuthController,
    register_oauth_provider,
)
from dependencies import require_auth
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from models.user import User
from services.session_service import SessionService
from services.user_service import UserService


logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix='/api/v1/auth', tags=['auth'])

# Initialize services (these will be created once and reused)
session_service: SessionService = SessionService()
user_service: UserService = UserService()
auth_controller: AuthController = AuthController(session_service, user_service)

# Register OAuth provider (must be done once at module load time)
register_oauth_provider()


@router.get('/login')
async def login(request: Request) -> RedirectResponse:
    """Initiate OIDC login flow.

    Query Parameters:
        redirectTo: URL to redirect to after successful login

    Returns:
        Redirect to OIDC authorization endpoint
    """
    return await auth_controller.get_login(request)


@router.get('/login-callback')
async def login_callback(request: Request, response: Response) -> RedirectResponse:
    """Handle OIDC login callback.

    Query Parameters:
        code: Authorization code from OIDC
        state: State parameter for CSRF protection

    Returns:
        Redirect to the originally requested page with session cookie set
    """
    return await auth_controller.get_login_callback(request, response)


@router.get('/logout')
async def logout(request: Request) -> RedirectResponse:
    """Initiate logout flow.

    Query Parameters:
        redirectTo: URL to redirect to after logout (optional)

    Returns:
        Redirect to OIDC logout endpoint
    """
    return await auth_controller.get_logout(request)


@router.get('/logout-callback')
async def logout_callback(request: Request) -> RedirectResponse:
    """Handle OIDC logout callback.

    Query Parameters:
        state: State parameter

    Returns:
        Redirect to the specified page
    """
    return await auth_controller.get_logout_callback(request)


@router.get('/me')
async def get_current_user(user: User = Depends(require_auth)) -> dict:
    """Get the current authenticated user's information.

    Returns:
        User information including id, email, name, and settings.

    Raises:
        401 Unauthorized: If the user is not authenticated.
    """
    return {
        'id': user.id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'company_name': user.company_name,
        'is_administrator': user.is_administrator,
        'language': user.language,
    }
