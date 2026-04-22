"""Socket session service for managing Socket.IO sessions in PostgreSQL."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from ..db.connection import get_async_session_factory
from ..models.socket_session import SocketSession

logger = logging.getLogger(__name__)


class SocketSessionService:
    """Manages Socket.IO sessions in PostgreSQL.

    Stores minimal Socket.IO session data in a dedicated socket_sessions table.
    The actual httpx and A2A clients are cached in-memory per server instance
    for efficiency and cleaned up on disconnect.
    """

    def __init__(self) -> None:
        """Initialize the socket session service."""
        self._session_factory = get_async_session_factory()
        logger.info("SocketSessionService initialized (PostgreSQL)")

    async def create_session(
        self,
        socket_id: str,
        user_id: str,
        http_session_id: str,
    ) -> SocketSession:
        """Create a new socket session.

        Args:
            socket_id: The Socket.IO session ID (sid)
            user_id: The user's ID (sub from OIDC)
            http_session_id: The HTTP session ID for linking back to user session

        Returns:
            The created SocketSession
        """
        created_at = datetime.now(tz=timezone.utc)
        session_key = f"socket:{socket_id}"

        socket_session = SocketSession(
            socket_id=session_key,
            user_id=user_id,
            http_session_id=http_session_id,
            created_at=created_at,
        )

        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        "INSERT INTO socket_sessions "
                        "(socket_id, user_id, http_session_id, created_at) "
                        "VALUES (:socket_id, :user_id, :http_session_id, :created_at)"
                    ),
                    {
                        "socket_id": session_key,
                        "user_id": user_id,
                        "http_session_id": http_session_id,
                        "created_at": created_at,
                    },
                )
                await db.commit()
            logger.info(f"Created socket session for user: {user_id}, sid: {socket_id}")
            return socket_session
        except Exception as e:
            logger.error(f"Failed to create socket session: {e}")
            raise

    async def get_session(self, socket_id: str) -> SocketSession | None:
        """Retrieve a socket session by Socket.IO session ID.

        Args:
            socket_id: The Socket.IO session ID (sid)

        Returns:
            The SocketSession or None if not found
        """
        session_key = f"socket:{socket_id}"
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text("SELECT * FROM socket_sessions WHERE socket_id = :socket_id"),
                    {"socket_id": session_key},
                )
                row = result.mappings().first()

            if not row:
                logger.debug(f"Socket session not found: {socket_id}")
                return None

            return SocketSession(
                socket_id=row["socket_id"],
                user_id=row["user_id"],
                http_session_id=row["http_session_id"],
                agent_url=row["agent_url"],
                custom_headers=row["custom_headers"] or {},
                is_initialized=row["is_initialized"],
                created_at=row["created_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get socket session: {e}")
            return None

    async def initialize_client(
        self,
        socket_id: str,
        agent_url: str,
        custom_headers: dict[str, str],
    ) -> None:
        """Mark socket session as initialized and store agent URL.

        Args:
            socket_id: The Socket.IO session ID (sid)
            agent_url: The agent URL for cache lookup
            custom_headers: Custom HTTP headers
        """
        session_key = f"socket:{socket_id}"

        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        "UPDATE socket_sessions SET agent_url = :agent_url, "
                        "custom_headers = CAST(:custom_headers AS jsonb), is_initialized = TRUE "
                        "WHERE socket_id = :socket_id"
                    ),
                    {
                        "socket_id": session_key,
                        "agent_url": agent_url,
                        "custom_headers": json.dumps(custom_headers),
                    },
                )
                await db.commit()
            logger.info(f"Initialized client for socket session: {socket_id}")
        except Exception as e:
            logger.error(f"Failed to initialize socket session: {e}")
            raise

    async def destroy_session(self, socket_id: str) -> None:
        """Delete a socket session.

        Args:
            socket_id: The Socket.IO session ID (sid)
        """
        session_key = f"socket:{socket_id}"
        try:
            async with self._session_factory() as db:
                await db.execute(
                    text("DELETE FROM socket_sessions WHERE socket_id = :socket_id"),
                    {"socket_id": session_key},
                )
                await db.commit()
            logger.info(f"Destroyed socket session: {socket_id}")
        except Exception as e:
            logger.error(f"Failed to destroy socket session: {e}")
