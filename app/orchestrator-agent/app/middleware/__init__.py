"""
Middleware module for the Orchestrator Agent.

This module contains all middleware components used throughout the application,
providing cross-cutting functionality like authentication, error handling,
context management, and status tracking.

Key Components:
- AuthErrorDetectionMiddleware: Detects and handles authentication errors
- OktaAuthMiddleware: OAuth2 authentication via Okta
- TodoStatusMiddleware: Tracks and manages todo status updates
- UserContextMiddleware: Extracts and manages user context

Usage:
    from app.middleware import (
        AuthErrorDetectionMiddleware,
        OktaAuthMiddleware,
        TodoStatusMiddleware,
        UserContextMiddleware,
    )
"""

from .auth_error_middleware import AuthErrorDetectionMiddleware, AuthErrorState
from .okta_auth_middleware import OktaAuthMiddleware
from .todo_status_middleware import TodoStatusMiddleware, TodoStatusState
from .user_context_middleware import UserContextMiddleware

__all__ = [
    "AuthErrorDetectionMiddleware",
    "AuthErrorState",
    "OktaAuthMiddleware",
    "TodoStatusMiddleware",
    "TodoStatusState",
    "UserContextMiddleware",
]
