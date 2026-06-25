"""Voice session service — database-backed implementation.

Tracks inbound Twilio calls and stores Gemini Live session resumption handles
so callers can continue a previous conversation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from ..models.voice_session import VoiceSession, VoiceSessionStatus

logger = logging.getLogger(__name__)


def _row_to_session(row) -> VoiceSession:
    return VoiceSession(
        id=str(row["id"]),
        user_id=row["user_id"],
        sub_agent_id=row["sub_agent_id"],
        phone_number=row["phone_number"],
        call_sid=row["call_sid"],
        gemini_session_handle=row["gemini_session_handle"],
        status=VoiceSessionStatus(row["status"]),
        use_session_memory=row["use_session_memory"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class VoiceSessionService:
    """Manages voice session records (database-backed)."""

    async def create_session(
        self,
        db,
        user_id: str,
        phone_number: str,
        sub_agent_id: int | None = None,
        call_sid: str | None = None,
        use_session_memory: bool = False,
    ) -> VoiceSession | None:
        try:
            now = datetime.now(timezone.utc)
            session_id = str(uuid.uuid4())
            query = text("""
                INSERT INTO voice_sessions
                    (id, user_id, sub_agent_id, phone_number, call_sid,
                     use_session_memory, status, started_at, created_at, updated_at)
                VALUES
                    (:id, :user_id, :sub_agent_id, :phone_number, :call_sid,
                     :use_session_memory, 'active', :now, :now, :now)
                RETURNING id, user_id, sub_agent_id, phone_number, call_sid,
                          gemini_session_handle, status, use_session_memory,
                          started_at, ended_at, created_at, updated_at
            """)
            result = await db.execute(query, {
                "id": session_id,
                "user_id": user_id,
                "sub_agent_id": sub_agent_id,
                "phone_number": phone_number,
                "call_sid": call_sid,
                "use_session_memory": use_session_memory,
                "now": now,
            })
            await db.commit()
            row = result.mappings().first()
            return _row_to_session(row) if row else None
        except Exception as e:
            logger.error("Failed to create voice session: %s", e)
            await db.rollback()
            return None

    async def get_latest_resumable_session(
        self,
        db,
        user_id: str,
        sub_agent_id: int,
    ) -> VoiceSession | None:
        """Return the most recent completed session with a Gemini resumption handle."""
        try:
            query = text("""
                SELECT id, user_id, sub_agent_id, phone_number, call_sid,
                       gemini_session_handle, status, use_session_memory,
                       started_at, ended_at, created_at, updated_at
                FROM voice_sessions
                WHERE user_id = :user_id AND sub_agent_id = :sub_agent_id
                  AND gemini_session_handle IS NOT NULL AND status = 'completed'
                ORDER BY ended_at DESC LIMIT 1
            """)
            result = await db.execute(query, {"user_id": user_id, "sub_agent_id": sub_agent_id})
            row = result.mappings().first()
            return _row_to_session(row) if row else None
        except Exception as e:
            logger.error("Failed to get latest resumable session: %s", e)
            return None

    async def get_most_recent_sub_agent_ids(
        self,
        db,
        user_id: str,
        limit: int = 5,
    ) -> list[int]:
        """Return sub_agent_ids ordered by most recently used."""
        try:
            query = text("""
                SELECT sub_agent_id FROM voice_sessions
                WHERE user_id = :user_id AND sub_agent_id IS NOT NULL
                GROUP BY sub_agent_id ORDER BY MAX(started_at) DESC LIMIT :limit
            """)
            result = await db.execute(query, {"user_id": user_id, "limit": limit})
            return [row["sub_agent_id"] for row in result.mappings().all()]
        except Exception as e:
            logger.error("Failed to get recent sub_agent_ids: %s", e)
            return []

    async def update_handle(
        self,
        db,
        session_id: str,
        gemini_session_handle: str,
    ) -> bool:
        try:
            await db.execute(text("""
                UPDATE voice_sessions SET gemini_session_handle = :handle, updated_at = :now
                WHERE id = :id
            """), {"id": session_id, "handle": gemini_session_handle, "now": datetime.now(timezone.utc)})
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to update voice session handle: %s", e)
            await db.rollback()
            return False

    async def complete_session(self, db, session_id: str) -> bool:
        try:
            now = datetime.now(timezone.utc)
            await db.execute(text("""
                UPDATE voice_sessions SET status = 'completed', ended_at = :now, updated_at = :now
                WHERE id = :id
            """), {"id": session_id, "now": now})
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to complete voice session: %s", e)
            await db.rollback()
            return False

    async def fail_session(self, db, session_id: str) -> bool:
        try:
            now = datetime.now(timezone.utc)
            await db.execute(text("""
                UPDATE voice_sessions SET status = 'failed', ended_at = :now, updated_at = :now
                WHERE id = :id
            """), {"id": session_id, "now": now})
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to mark voice session as failed: %s", e)
            await db.rollback()
            return False
