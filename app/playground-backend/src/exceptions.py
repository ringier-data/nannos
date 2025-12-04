"""Custom exceptions for the application.

This module contains session-related and conversation-related exceptions.
"""

__all__ = [
    'ConversationError',
    'ConversationNotFoundError',
    'ConversationOwnershipError',
    'SessionError',
    'SessionNotFoundError',
    'SessionOwnershipError',
]


class SessionError(Exception):
    """Base exception for session-related errors."""


class SessionNotFoundError(SessionError):
    """Raised when a session is not found."""


class SessionOwnershipError(SessionError):
    """Raised when attempting to modify a session that doesn't belong to the user."""


class ConversationError(Exception):
    """Base exception for conversation-related errors."""


class ConversationNotFoundError(ConversationError):
    """Raised when a conversation is not found."""


class ConversationOwnershipError(ConversationError):
    """Raised when attempting to access or modify a conversation that doesn't belong to the user."""
