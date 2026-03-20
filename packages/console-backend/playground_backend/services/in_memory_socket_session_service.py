"""In-memory socket session service — drop-in replacement for DynamoDB-backed SocketSessionService.

Used when DynamoDB is not available (local development without AWS credentials).
Data is lost on process restart.
"""

import logging
from datetime import datetime, timezone

from ..models.socket_session import SocketSession

logger = logging.getLogger(__name__)


class InMemorySocketSessionService:
    """In-memory socket session store matching SocketSessionService's public API."""

    def __init__(self) -> None:
        self._sessions: dict[str, SocketSession] = {}
        logger.warning("Using in-memory socket session store — sessions will not survive restarts")

    async def create_session(
        self,
        socket_id: str,
        user_id: str,
        http_session_id: str,
    ) -> SocketSession:
        now = datetime.now(timezone.utc)
        session = SocketSession(
            socket_id=socket_id,
            user_id=user_id,
            http_session_id=http_session_id,
            created_at=now,
            ttl=int(now.timestamp()) + 86400,
        )
        key = f"socket:{socket_id}"
        self._sessions[key] = session
        return session

    async def get_session(self, socket_id: str) -> SocketSession | None:
        key = f"socket:{socket_id}"
        session = self._sessions.get(key)
        if session and session.ttl < int(datetime.now(timezone.utc).timestamp()):
            del self._sessions[key]
            return None
        return session

    async def initialize_client(
        self,
        socket_id: str,
        agent_url: str,
        custom_headers: dict[str, str],
    ) -> None:
        key = f"socket:{socket_id}"
        session = self._sessions.get(key)
        if session:
            session.agent_url = agent_url
            session.custom_headers = custom_headers
            session.is_initialized = True

    async def destroy_session(self, socket_id: str) -> None:
        key = f"socket:{socket_id}"
        self._sessions.pop(key, None)
