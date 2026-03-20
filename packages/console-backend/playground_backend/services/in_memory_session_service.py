"""In-memory session service — drop-in replacement for DynamoDB-backed SessionService.

Used when DynamoDB is not available (local development without AWS credentials).
Data is lost on process restart.
"""

import logging
import secrets
from datetime import datetime, timezone

from ..models.session import StoredSession

logger = logging.getLogger(__name__)


class InMemorySessionService:
    """In-memory session store matching SessionService's public API."""

    def __init__(self) -> None:
        self._sessions: dict[str, StoredSession] = {}
        logger.warning("Using in-memory session store — sessions will not survive restarts")

    async def create_session(
        self,
        user_id: str,
        refresh_token: str,
        id_token: str,
        access_token: str,
        access_token_expires_in: int = 3600,
    ) -> str:
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        session = StoredSession(
            session_id=session_id,
            user_id=user_id,
            access_token=access_token,
            access_token_expires_at=datetime.fromtimestamp(
                now.timestamp() + access_token_expires_in, tz=timezone.utc
            ),
            refresh_token=refresh_token,
            id_token=id_token,
            issued_at=now,
            ttl=int(now.timestamp()) + 86400,  # 24h
        )
        self._sessions[session_id] = session
        return session_id

    async def get_session(self, session_id: str) -> StoredSession | None:
        session = self._sessions.get(session_id)
        if session and session.ttl < int(datetime.now(timezone.utc).timestamp()):
            del self._sessions[session_id]
            return None
        return session

    async def destroy_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

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
        session = self._sessions.get(session_id)
        if not session:
            return
        if access_token is not None:
            session.access_token = access_token
        if access_token_expires_at is not None:
            session.access_token_expires_at = access_token_expires_at
        if refresh_token is not None:
            session.refresh_token = refresh_token
        if id_token is not None:
            session.id_token = id_token
        if issued_at is not None:
            session.issued_at = issued_at

    async def get_orchestrator_cookie(self, session_id: str) -> tuple[str, datetime] | None:
        session = self._sessions.get(session_id)
        if session and session.orchestrator_session_cookie and session.orchestrator_cookie_expires_at:
            if session.orchestrator_cookie_expires_at > datetime.now(timezone.utc):
                return session.orchestrator_session_cookie, session.orchestrator_cookie_expires_at
        return None

    async def update_orchestrator_cookie(
        self,
        session_id: str,
        cookie: str,
        expires_at: datetime,
    ) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.orchestrator_session_cookie = cookie
            session.orchestrator_cookie_expires_at = expires_at

    async def clear_orchestrator_cookie(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.orchestrator_session_cookie = None
            session.orchestrator_cookie_expires_at = None
