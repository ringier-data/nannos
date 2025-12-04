"""Standardized error responses for Socket.IO events."""

from typing import Any


class SocketError:
    """Standard error codes for Socket.IO events."""

    # Authentication errors
    AUTH_REQUIRED = 'auth_required'
    AUTH_INVALID = 'auth_invalid'
    SESSION_NOT_FOUND = 'session_not_found'

    # Client initialization errors
    INIT_URL_REQUIRED = 'init_url_required'
    INIT_FAILED = 'init_failed'
    INIT_NOT_INITIALIZED = 'init_not_initialized'

    # Message errors
    MSG_SIZE_EXCEEDED = 'msg_size_exceeded'
    MSG_SEND_FAILED = 'msg_send_failed'

    # Server errors
    SERVER_ERROR = 'server_error'


ERROR_MESSAGES = {
    SocketError.AUTH_REQUIRED: 'Authentication required',
    SocketError.AUTH_INVALID: 'Invalid authentication',
    SocketError.SESSION_NOT_FOUND: 'Session not found',
    SocketError.INIT_URL_REQUIRED: 'Agent URL is required',
    SocketError.INIT_FAILED: 'Failed to initialize client',
    SocketError.INIT_NOT_INITIALIZED: 'Client not initialized. Please initialize first.',
    SocketError.MSG_SIZE_EXCEEDED: 'Message exceeds size limit',
    SocketError.MSG_SEND_FAILED: 'Failed to send message',
    SocketError.SERVER_ERROR: 'Internal server error',
}


def create_error_response(
    error_code: str,
    details: dict[str, Any] | None = None,
    message_override: str | None = None,
) -> dict[str, Any]:
    """Create a standardized error response.

    Args:
        error_code: Error code from SocketError class
        details: Optional additional error details
        message_override: Optional custom message (overrides default)

    Returns:
        Standardized error response dictionary with error message as string
    """
    # Build the error message
    base_message = message_override or ERROR_MESSAGES.get(error_code, 'Unknown error')

    # If details contain a 'reason' field, append it to the message
    error_message = f'{base_message}: {details["reason"]}' if details and 'reason' in details else base_message

    return {
        'status': 'error',
        'error': error_message,  # Simple string for frontend consumption
        'error_code': error_code,
        'error_details': details or {},
    }


def create_success_response(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a standardized success response.

    Args:
        data: Optional data to include in response

    Returns:
        Standardized success response dictionary
    """
    response = {'status': 'success'}
    if data:
        response.update(data)
    return response
