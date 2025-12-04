"""Example integration of auth system into app.py

To integrate the authentication system:

1. Import the necessary components at the top of app.py:
"""

from fastapi import Depends, HTTPException, Request

from backend.config import config
from backend.middleware import session_middleware
from backend.routers import auth_router
from backend.services import SessionService, UserService


"""
2. Initialize services after creating the FastAPI app:
"""

# Initialize auth services
session_service = SessionService()
user_service = UserService()

"""
3. Add the session middleware to the app (before mounting static files):
"""

# Add session middleware to load user from cookies
app.add_middleware(session_middleware(session_service, user_service))

"""
4. Register the auth router:
"""

# Register authentication routes
app.include_router(auth_router)

"""
5. Create a dependency for protected routes:
"""


def require_auth(request: Request):
    """Dependency to require authentication."""
    if not hasattr(request.state, 'user') or not request.state.user:
        raise HTTPException(status_code=401, detail='Not authenticated')
    return request.state.user


"""
6. Use the dependency in routes that need authentication:
"""


@app.get('/api/protected-endpoint')
async def protected_endpoint(user=Depends(require_auth)):
    """Example protected endpoint that requires authentication."""
    return {'message': f'Hello {user.email}', 'user_id': user.id, 'is_admin': user.is_administrator}


"""
7. For making requests to the orchestrator with token exchange:
"""

from backend.middleware import create_interceptor_for_user


@app.post('/api/orchestrator/task')
async def create_orchestrator_task(request: Request, task_data: dict, user=Depends(require_auth)):
    """Create a task in the orchestrator agent with automatic token exchange."""
    # Get user's access token from session
    # Note: You'll need to store the access_token in the session during login
    # For now, we'll assume it's available in request.state
    if not hasattr(request.state, 'access_token'):
        raise HTTPException(status_code=401, detail='Access token not available')

    user_token = request.state.access_token

    # Create interceptor for token exchange
    interceptor = create_interceptor_for_user(user_token)

    try:
        # Make request to orchestrator - token exchange happens automatically
        response = await interceptor.request('POST', f'https://{config.orchestrator.base_domain}/task', json=task_data)
        return response.json()
    finally:
        await interceptor.close()


"""
8. Optional: Add a current user endpoint:
"""


@app.get('/api/v1/auth/me')
async def get_current_user(user=Depends(require_auth)):
    """Get the current authenticated user."""
    return {
        'id': user.id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'company_name': user.company_name,
        'is_administrator': user.is_administrator,
    }


"""
FULL EXAMPLE APP.PY STRUCTURE:
"""

# At the top of app.py, add these imports:
# from backend.routers import auth_router
# from backend.middleware import session_middleware
# from backend.services import SessionService, UserService
# from fastapi import Depends, HTTPException

# After creating the FastAPI app:
# session_service = SessionService()
# user_service = UserService()
#
# app.add_middleware(
#     session_middleware(session_service, user_service)
# )
#
# app.include_router(auth_router)

# Then add the require_auth dependency and use it in your routes
