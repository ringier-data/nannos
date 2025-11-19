"""
Middleware module for the Orchestrator Agent.

This module contains all middleware components used throughout the application,
providing cross-cutting functionality like authentication, error handling,
context management, and status tracking.

Key Components:
- AuthErrorDetectionMiddleware: Detects and handles authentication errors
- OidcAuthMiddleware: OAuth2 authentication via OIDC
- TodoStatusMiddleware: Tracks and manages todo status updates
- UserContextMiddleware: Extracts and manages user context

Usage:
    from app.middleware import (
        AuthErrorDetectionMiddleware,
        OidcAuthMiddleware,
        TodoStatusMiddleware,
        UserContextMiddleware,
    )
"""

from .auth_error_middleware import AuthErrorDetectionMiddleware, AuthErrorState
from .todo_status_middleware import TodoStatusMiddleware, TodoStatusState

__all__ = [
    "AuthErrorDetectionMiddleware",
    "AuthErrorState",
    "TodoStatusMiddleware",
    "TodoStatusState",
]
