"""Session service for managing user sessions in PostgreSQL."""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from ..config import config
from ..db.connection import get_async_session_factory
from ..exceptions import SessionNotFoundError, SessionOwnershipError
from ..models.session import StoredSession

logger = logging.getLogger(__name__)


class SessionService:
    """Manages user sessions in PostgreSQL."""

    def __init__(self) -> None:
        """Initialize the session service."""
        self.session_ttl_seconds = config.session_ttl_seconds
        self._session_factory = get_async_session_factory()
        logger.info("SessionService initialized (PostgreSQL)")

    async def create_session(
        self,
        user_id: str,
        refresh_token: str,
        id_token: str,
        access_token: str,
        access_token_expires_in: int = 3600,
    ) -> str:
        """Create a new session for a user.

        Args:
            user_id: The user's ID (sub from OIDC)
            refresh_token: The refresh token from OIDC
            id_token: The ID token from OIDC (needed for logout)
            access_token: The access token from OIDC (for token exchange)
            access_token_expires_in: Access token lifetime in seconds (default: 3600)

        Returns:
            The session ID
        """
        session_id = str(uuid4())
        issued_at = datetime.now(tz=timezone.utc)
        access_token_expires_at = issued_at + timedelta(seconds=access_token_expires_in)
        expires_at = issued_at + timedelta(seconds=self.session_ttl_seconds)

        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        "INSERT INTO sessions "
                        "(session_id, user_id, access_token, access_token_expires_at, "
                        "refresh_token, id_token, issued_at, expires_at) "
                        "VALUES (:session_id, :user_id, :access_token, :access_token_expires_at, "
                        ":refresh_token, :id_token, :issued_at, :expires_at)"
                    ),
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "access_token": access_token,
                        "access_token_expires_at": access_token_expires_at,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                        "issued_at": issued_at,
                        "expires_at": expires_at,
                    },
                )
                await db.commit()
            logger.info(f"Created session for user: {user_id}")
            return session_id
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    async def get_session(self, session_id: str) -> StoredSession | None:
        """Retrieve a session by ID.

        Args:
            session_id: The session ID

        Returns:
            The stored session or None if not found
        """
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text("SELECT * FROM sessions WHERE session_id = :session_id"),
                    {"session_id": session_id},
                )
                row = result.mappings().first()

            if not row:
                logger.debug(f"Session not found: {session_id}")
                return None

            return StoredSession(
                session_id=row["session_id"],
                user_id=row["user_id"],
                access_token=row["access_token"],
                access_token_expires_at=row["access_token_expires_at"],
                refresh_token=row["refresh_token"],
                id_token=row["id_token"],
                issued_at=row["issued_at"],
                expires_at=row["expires_at"],
                orchestrator_session_cookie=row["orchestrator_session_cookie"],
                orchestrator_cookie_expires_at=row["orchestrator_cookie_expires_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            return None

    async def destroy_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID to delete
        """
        try:
            async with self._session_factory() as db:
                await db.execute(
                    text("DELETE FROM sessions WHERE session_id = :session_id"),
                    {"session_id": session_id},
                )
                await db.commit()
            logger.info(f"Destroyed session: {session_id}")
        except Exception as e:
            logger.error(f"Failed to destroy session: {e}")

    async def update_session(
        self,
        session_id: str,
        user_id: str,
        access_token: str | None = None,
        access_token_expires_at: datetime | None = None,
        refresh_token: str | None = None,
        id_token: str | None = None,
        issued_at: datetime | None = None,
    ) -> None:
        """Update an existing session.

        All parameters except session_id and user_id are optional. Only provided fields will be updated.
        The user_id must match the existing session's user_id or the update will fail.

        Args:
            session_id: The session ID
            user_id: The user ID (must match existing session, prevents changing session ownership)
            access_token: The access token (optional)
            access_token_expires_at: When the access token expires (optional)
            refresh_token: The refresh token (optional)
            id_token: The ID token (optional)
            issued_at: When the session was issued (optional)

        Raises:
            SessionNotFoundError: If the session doesn't exist
            SessionOwnershipError: If user_id doesn't match the session's owner
        """
        # First verify the session exists and user_id matches
        existing_session = await self.get_session(session_id)
        if not existing_session:
            logger.error(f"Failed to update session {session_id}: session not found")
            raise SessionNotFoundError(f"Session {session_id} not found")

        if existing_session.user_id != user_id:
            logger.error(f"Failed to update session {session_id}: user_id mismatch (attempted: {user_id})")
            raise SessionOwnershipError(f"User {user_id} does not own session {session_id}")

        # Use provided issued_at or current time
        if issued_at is None:
            issued_at = datetime.now(tz=timezone.utc)

        expires_at = issued_at + timedelta(seconds=self.session_ttl_seconds)

        # Build dynamic SET clause
        set_parts = ["issued_at = :issued_at", "expires_at = :expires_at"]
        params: dict = {
            "session_id": session_id,
            "user_id": user_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }

        if access_token is not None:
            set_parts.append("access_token = :access_token")
            params["access_token"] = access_token

        if access_token_expires_at is not None:
            set_parts.append("access_token_expires_at = :access_token_expires_at")
            params["access_token_expires_at"] = access_token_expires_at

        if refresh_token is not None:
            set_parts.append("refresh_token = :refresh_token")
            params["refresh_token"] = refresh_token

        if id_token is not None:
            set_parts.append("id_token = :id_token")
            params["id_token"] = id_token

        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        f"UPDATE sessions SET {', '.join(set_parts)} "
                        "WHERE session_id = :session_id AND user_id = :user_id"
                    ),
                    params,
                )
                await db.commit()
            logger.info(f"Updated session: {session_id}")
        except Exception as e:
            logger.error(f"Failed to update session: {e}")
            raise

    async def get_orchestrator_cookie(self, session_id: str) -> tuple[str, datetime] | None:
        """Get orchestrator session cookie from session.

        Args:
            session_id: The session ID

        Returns:
            Tuple of (cookie, expires_at) if found and valid, None otherwise
        """
        try:
            session = await self.get_session(session_id)
            if not session:
                logger.debug(f"Session not found: {session_id}")
                return None

            if not session.orchestrator_session_cookie or not session.orchestrator_cookie_expires_at:
                logger.debug(f"No orchestrator cookie for session: {session_id}")
                return None

            # Check if cookie is expired
            now = datetime.now(timezone.utc)
            if session.orchestrator_cookie_expires_at <= now:
                logger.debug(f"Orchestrator cookie expired for session: {session_id}")
                return None

            return (session.orchestrator_session_cookie, session.orchestrator_cookie_expires_at)
        except Exception as e:
            logger.error(f"Failed to get orchestrator cookie: {e}")
            return None

    async def update_orchestrator_cookie(
        self,
        session_id: str,
        cookie: str,
        expires_at: datetime,
    ) -> None:
        """Update orchestrator session cookie in session.

        Args:
            session_id: The session ID
            cookie: The orchestrator session cookie value
            expires_at: When the cookie expires
        """
        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        "UPDATE sessions SET orchestrator_session_cookie = :cookie, "
                        "orchestrator_cookie_expires_at = :expires_at "
                        "WHERE session_id = :session_id"
                    ),
                    {"session_id": session_id, "cookie": cookie, "expires_at": expires_at},
                )
                await db.commit()
            logger.debug(f"Updated orchestrator cookie for session: {session_id}")
        except Exception as e:
            logger.error(f"Failed to update orchestrator cookie: {e}")
            raise

    async def clear_orchestrator_cookie(self, session_id: str) -> None:
        """Clear orchestrator session cookie from session.

        Args:
            session_id: The session ID
        """
        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        "UPDATE sessions SET orchestrator_session_cookie = NULL, "
                        "orchestrator_cookie_expires_at = NULL "
                        "WHERE session_id = :session_id"
                    ),
                    {"session_id": session_id},
                )
                await db.commit()
            logger.debug(f"Cleared orchestrator cookie for session: {session_id}")
        except Exception as e:
            logger.error(f"Failed to clear orchestrator cookie: {e}")
            raise
