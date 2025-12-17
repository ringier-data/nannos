"""
Middleware module for the Orchestrator Agent.

This module contains all middleware components used throughout the application,
providing cross-cutting functionality like authentication, error handling,
context management, tool dispatch, user preferences, loop detection, and status tracking.

Key Components:
- DynamicToolDispatchMiddleware: Enables runtime tool injection per-user and A2A subagent handling
- AuthErrorDetectionMiddleware: Detects and handles authentication errors
- TodoStatusMiddleware: Tracks and manages todo status updates
- UserPreferencesMiddleware: Injects user preferences into system prompt at runtime
- RepeatedToolCallMiddleware: Detects and prevents infinite tool call loops

Usage:
    from app.middleware import (
        DynamicToolDispatchMiddleware,
        AuthErrorDetectionMiddleware,
        TodoStatusMiddleware,
        UserPreferencesMiddleware,
        RepeatedToolCallMiddleware,
    )
"""

from .auth_error_middleware import AuthErrorDetectionMiddleware, AuthErrorState
from .dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from .loop_detection_middleware import LoopDetectionState, RepeatedToolCallMiddleware
from .todo_status_middleware import TodoStatusMiddleware, TodoStatusState
from .user_preferences_middleware import UserPreferencesMiddleware

__all__ = [
    "DynamicToolDispatchMiddleware",
    "AuthErrorDetectionMiddleware",
    "AuthErrorState",
    "TodoStatusMiddleware",
    "TodoStatusState",
    "UserPreferencesMiddleware",
    "RepeatedToolCallMiddleware",
    "LoopDetectionState",
]
