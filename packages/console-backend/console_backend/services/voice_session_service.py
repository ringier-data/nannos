"""Voice session service — delegates writes through VoiceSessionRepository."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models.user import User
from ..models.voice_session import VoiceSession

if TYPE_CHECKING:
    from ..repositories.voice_session_repository import VoiceSessionRepository

logger = logging.getLogger(__name__)


class VoiceSessionService:
    """Manages voice session records (database-backed via VoiceSessionRepository)."""

    def __init__(self) -> None:
        self._repo: VoiceSessionRepository | None = None

    def set_repository(self, repo: VoiceSessionRepository) -> None:
        self._repo = repo

    @property
    def repo(self) -> VoiceSessionRepository:
        if self._repo is None:
            raise RuntimeError("VoiceSessionRepository not injected. Call set_repository() during initialization.")
        return self._repo

    async def create_session(
        self,
        db,
        actor: User,
        user_id: str,
        phone_number: str,
        sub_agent_id: int | None = None,
        call_sid: str | None = None,
        use_session_memory: bool = False,
    ) -> VoiceSession | None:
        try:
            session = await self.repo.create_session(
                db,
                actor,
                user_id=user_id,
                phone_number=phone_number,
                sub_agent_id=sub_agent_id,
                call_sid=call_sid,
                use_session_memory=use_session_memory,
            )
            await db.commit()
            return session
        except Exception as e:
            logger.error("Failed to create voice session: %s", e)
            await db.rollback()
            return None

    async def get_latest_resumable_session(
        self,
        db,
        user_id: str,
        sub_agent_id: int | None = None,
    ) -> VoiceSession | None:
        try:
            return await self.repo.get_latest_resumable_session(db, user_id, sub_agent_id)
        except Exception as e:
            logger.error("Failed to get latest resumable session: %s", e)
            return None

    async def get_most_recent_sub_agent_ids(
        self,
        db,
        user_id: str,
        limit: int = 5,
    ) -> list[int]:
        try:
            return await self.repo.get_most_recent_sub_agent_ids(db, user_id, limit)
        except Exception as e:
            logger.error("Failed to get recent sub_agent_ids: %s", e)
            return []

    async def update_handle(
        self,
        db,
        actor: User,
        session_id: str,
        gemini_session_handle: str,
    ) -> bool:
        try:
            found = await self.repo.update_handle(db, actor, session_id, gemini_session_handle)
            await db.commit()
            return found
        except Exception as e:
            logger.error("Failed to update voice session handle: %s", e)
            await db.rollback()
            return False

    async def complete_session(self, db, actor: User, session_id: str) -> bool:
        try:
            await self.repo.complete_session(db, actor, session_id)
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to complete voice session: %s", e)
            await db.rollback()
            return False

    async def fail_session(self, db, actor: User, session_id: str) -> bool:
        try:
            await self.repo.fail_session(db, actor, session_id)
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to mark voice session as failed: %s", e)
            await db.rollback()
            return False
