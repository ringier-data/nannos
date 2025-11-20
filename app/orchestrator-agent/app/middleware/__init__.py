"""
Middleware module for the Orchestrator Agent.

This module contains all middleware components used throughout the application,
providing cross-cutting functionality like authentication, error handling,
context management, and status tracking.

Key Components:
- AuthErrorDetectionMiddleware: Detects and handles authentication errors
- TodoStatusMiddleware: Tracks and manages todo status updates

Usage:
    from app.middleware import (
        AuthErrorDetectionMiddleware,
        TodoStatusMiddleware,
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
