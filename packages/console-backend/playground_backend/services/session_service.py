"""Session service for managing user sessions in DynamoDB."""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from aiodynamo.client import Client
from aiodynamo.errors import ItemNotFound
from aiodynamo.expressions import F, UpdateExpression, Value
from aiodynamo.http.httpx import HTTPX

from ..config import config
from ..exceptions import SessionNotFoundError, SessionOwnershipError
from ..models.session import StoredSession
from ..utils.aws_credentials import BotoRefreshableCredentials

logger = logging.getLogger(__name__)


class SessionService:
    """Manages user sessions in DynamoDB."""

    def __init__(self) -> None:
        """Initialize the session service."""
        dynamodb_config = config.dynamodb
        self.table_name = dynamodb_config.sessions_table
        self.session_ttl_seconds = config.session_ttl_seconds

        # Use boto3 refreshable credentials - handles all AWS credential sources
        # (EKS Pod Identity, IRSA, env vars, profiles) with automatic token refresh
        credentials = BotoRefreshableCredentials()

        self.client = Client(
            HTTPX(httpx.AsyncClient()),
            credentials,
            dynamodb_config.region,
        )
        self.table = self.client.table(self.table_name)

        logger.info(f"SessionService initialized with table: {self.table_name}")

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
        ttl = int((issued_at + timedelta(seconds=self.session_ttl_seconds)).timestamp())

        stored_session = StoredSession(
            session_id=session_id,
            user_id=user_id,
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
            refresh_token=refresh_token,
            id_token=id_token,
            issued_at=issued_at,
            ttl=ttl,
        )
        logger.info(f"Table name: {self.table_name}, Creating session for user: {user_id}")
        try:
            await self.table.put_item(
                item={
                    "session_id": stored_session.session_id,
                    "user_id": stored_session.user_id,
                    "access_token": stored_session.access_token,
                    "access_token_expires_at": stored_session.access_token_expires_at.isoformat(),
                    "refresh_token": stored_session.refresh_token,
                    "id_token": stored_session.id_token,
                    "issued_at": stored_session.issued_at.isoformat(),
                    "ttl": stored_session.ttl,
                }
            )
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
            item = await self.table.get_item(key={"session_id": session_id})
            return StoredSession(
                session_id=item["session_id"],
                user_id=item["user_id"],
                access_token=item["access_token"],
                access_token_expires_at=datetime.fromisoformat(item["access_token_expires_at"]),
                refresh_token=item["refresh_token"],
                id_token=item["id_token"],
                issued_at=datetime.fromisoformat(item["issued_at"]),
                ttl=item["ttl"],
                orchestrator_session_cookie=item.get("orchestrator_session_cookie"),
                orchestrator_cookie_expires_at=(
                    datetime.fromisoformat(item["orchestrator_cookie_expires_at"])
                    if item.get("orchestrator_cookie_expires_at")
                    else None
                ),
            )
        except ItemNotFound:
            logger.debug(f"Session not found: {session_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            return None

    async def destroy_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID to delete
        """
        try:
            await self.table.delete_item(key={"session_id": session_id})
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
        # Use provided issued_at or current time
        if issued_at is None:
            issued_at = datetime.now(tz=timezone.utc)

        ttl = int((issued_at + timedelta(seconds=self.session_ttl_seconds)).timestamp())

        # Build update expression using aiodynamo
        set_updates = [
            (F("issued_at"), Value(issued_at.isoformat())),
            (F("ttl"), Value(ttl)),
        ]

        if access_token is not None:
            set_updates.append((F("access_token"), Value(access_token)))

        if access_token_expires_at is not None:
            set_updates.append((F("access_token_expires_at"), Value(access_token_expires_at.isoformat())))

        if refresh_token is not None:
            set_updates.append((F("refresh_token"), Value(refresh_token)))

        if id_token is not None:
            set_updates.append((F("id_token"), Value(id_token)))

        # First verify the user_id matches
        existing_session = await self.get_session(session_id)
        if not existing_session:
            logger.error(f"Failed to update session {session_id}: session not found")
            raise SessionNotFoundError(f"Session {session_id} not found")

        if existing_session.user_id != user_id:
            logger.error(f"Failed to update session {session_id}: user_id mismatch (attempted: {user_id})")
            raise SessionOwnershipError(f"User {user_id} does not own session {session_id}")

        # Perform the update
        try:
            await self.table.update_item(
                key={"session_id": session_id},
                update_expression=UpdateExpression(set_updates=set_updates),
            )
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
            set_updates = [
                (F("orchestrator_session_cookie"), Value(cookie)),
                (F("orchestrator_cookie_expires_at"), Value(expires_at.isoformat())),
            ]

            await self.table.update_item(
                key={"session_id": session_id},
                update_expression=UpdateExpression(set_updates=set_updates),
            )
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
            set_updates = [
                (F("orchestrator_session_cookie"), Value(None)),
                (F("orchestrator_cookie_expires_at"), Value(None)),
            ]

            await self.table.update_item(
                key={"session_id": session_id},
                update_expression=UpdateExpression(set_updates=set_updates),
            )
            logger.debug(f"Cleared orchestrator cookie for session: {session_id}")
        except Exception as e:
            logger.error(f"Failed to clear orchestrator cookie: {e}")
            raise
