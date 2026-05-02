"""Socket.IO authentication decorators and utilities."""

import logging

from collections.abc import Callable
from functools import wraps
from typing import Any

import socketio


logger = logging.getLogger(__name__)


def require_auth(sio: socketio.AsyncServer) -> Callable:
    """Decorator to require authentication for Socket.IO event handlers.

    This decorator checks if the user is authenticated by verifying that
    the socket session exists in DynamoDB (managed by SocketSessionService).
    The actual authentication check is done in the handle_connect function,
    which creates the socket session. If the session doesn't exist in DynamoDB,
    the user is not authenticated.

    Note: This decorator now assumes that authentication state is managed via
    DynamoDB through SocketSessionService, not via Socket.IO's built-in session.
    The socket session is created in handle_connect and verified here by attempting
    to retrieve it from DynamoDB.

    Usage:
        @sio.on('my_event')
        @require_auth(sio)
        async def handle_my_event(sid: str, data: dict[str, Any]) -> None:
            # This code only runs if user is authenticated
            # Access user info via: socket_session = await socket_session_service.get_session(sid)
            pass

    Args:
        sio: The Socket.IO server instance

    Returns:
        A decorator function that wraps event handlers with authentication
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            try:
                # Check if socket session exists in DynamoDB
                # Access services via the app instance attached to the Socket.IO server
                socket_session = await sio.app_instance.state.socket_session_service.get_session(sid)  # type: ignore[attr-defined]
                if not socket_session:
                    logger.warning(f'Socket.IO event {func.__name__} rejected for {sid}: Not authenticated')
                    await sio.emit('error', {'message': 'Not authenticated'}, to=sid)
                    return None

                logger.debug(f'User {socket_session.user_id} calling {func.__name__} for {sid}')

            except Exception as e:
                logger.error(f'Error checking authentication for {func.__name__} on {sid}: {e}')
                await sio.emit('error', {'message': 'Authentication error'}, to=sid)
                return None

            # User is authenticated, proceed with the handler
            return await func(sid, *args, **kwargs)

        return wrapper

    return decorator
