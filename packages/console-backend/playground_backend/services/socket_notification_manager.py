"""WebSocket notification manager for real-time scheduler notifications.

This manager tracks active Socket.IO connections and enables the scheduler
to send notifications directly to connected users via WebSocket, avoiding
the need for external webhooks for internal notifications.
"""

import logging
from typing import Any

import socketio

from playground_backend.utils.socket_events import SocketEvents

logger = logging.getLogger(__name__)


class SocketNotificationManager:
    """Manages WebSocket connections for real-time scheduler notifications."""

    def __init__(self, sio: socketio.AsyncServer):
        """Initialize the notification manager.

        Args:
            sio: Socket.IO server instance
        """
        self._sio = sio
        # Track active connections: user_id -> set of session_ids (SIDs)
        self._user_connections: dict[str, set[str]] = {}

    def register_connection(self, user_id: str, sid: str) -> None:
        """Register a new WebSocket connection for a user.

        Args:
            user_id: User's unique identifier
            sid: Socket.IO session ID
        """
        if user_id not in self._user_connections:
            self._user_connections[user_id] = set()
        self._user_connections[user_id].add(sid)
        logger.info(f"Registered WebSocket connection for user {user_id}: {sid}")

    def unregister_connection(self, user_id: str, sid: str) -> None:
        """Unregister a WebSocket connection for a user.

        Args:
            user_id: User's unique identifier
            sid: Socket.IO session ID
        """
        if user_id in self._user_connections:
            self._user_connections[user_id].discard(sid)
            if not self._user_connections[user_id]:
                del self._user_connections[user_id]
        logger.info(f"Unregistered WebSocket connection for user {user_id}: {sid}")

    async def emit_to_user(self, user_id: str, event: str, data: dict[str, Any]) -> bool:
        """Emit an event to all active connections for a user.

        Args:
            user_id: User's unique identifier
            event: Socket.IO event name
            data: Event payload

        Returns:
            True if event was sent to at least one connection
        """
        if user_id not in self._user_connections:
            return False
        for sid in self._user_connections[user_id]:
            try:
                await self._sio.emit(event, data, room=sid)
            except Exception as e:
                logger.error(f"Failed to emit {event} to session {sid}: {e}")
        return True

    async def send_notification(self, user_id: str, notification: dict[str, Any]) -> bool:
        """Send a notification to a user via WebSocket.

        Args:
            user_id: User's unique identifier
            notification: Notification payload to send

        Returns:
            True if the notification was sent to at least one active connection,
            False if the user has no active connections
        """
        if user_id not in self._user_connections:
            logger.debug(f"No active WebSocket connections for user {user_id}")
            return False

        sids = self._user_connections[user_id]
        logger.info(f"Sending scheduler notification to user {user_id} via {len(sids)} WebSocket connection(s)")

        for sid in sids:
            try:
                await self._sio.emit(
                    SocketEvents.SCHEDULER_NOTIFICATION,
                    notification,
                    room=sid,
                )
            except Exception as e:
                logger.error(f"Failed to send notification to session {sid}: {e}")

        return True

    def has_active_connections(self, user_id: str) -> bool:
        """Check if a user has any active WebSocket connections.

        Args:
            user_id: User's unique identifier

        Returns:
            True if the user has at least one active connection
        """
        return user_id in self._user_connections and len(self._user_connections[user_id]) > 0

    def get_connection_count(self, user_id: str) -> int:
        """Get the number of active connections for a user.

        Args:
            user_id: User's unique identifier

        Returns:
            Number of active connections
        """
        return len(self._user_connections.get(user_id, set()))
