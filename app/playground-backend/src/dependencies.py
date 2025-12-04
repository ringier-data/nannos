"""Authentication dependencies for FastAPI routes."""

from fastapi import HTTPException, Request, status
from models.user import User


def require_auth(request: Request) -> User:
    """Dependency to require authentication.

    Raises HTTPException 401 if user is not authenticated.

    Usage:
        @app.get("/protected")
        async def protected_route(user: User = Depends(require_auth)):
            return {"user_email": user.email}
    """
    if not hasattr(request.state, 'user') or not request.state.user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Not authenticated',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    return request.state.user


def require_admin(request: Request) -> User:
    """Dependency to require admin authentication.

    Raises HTTPException 401 if user is not authenticated.
    Raises HTTPException 403 if user is not an administrator.

    Usage:
        @app.post("/admin/users")
        async def create_user(user: User = Depends(require_admin)):
            return {"created_by": user.email}
    """
    user = require_auth(request)

    if not user.is_administrator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient permissions',
        )

    return user
