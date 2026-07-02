"""Repository for voice session records with audit logging."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from ..models.voice_session import VoiceSession, VoiceSessionStatus
from .base import AuditedRepository, _serialize_for_audit

logger = logging.getLogger(__name__)

RESUME_WINDOW_MINUTES = 60


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


class VoiceSessionRepository(AuditedRepository):
    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.VOICE_SESSION,
            table_name="voice_sessions",
        )

    async def create_session(
        self,
        db: AsyncSession,
        actor: User,
        user_id: str,
        phone_number: str,
        sub_agent_id: int | None = None,
        call_sid: str | None = None,
        use_session_memory: bool = False,
    ) -> VoiceSession | None:
        now = datetime.now(timezone.utc)
        session_id = str(uuid.uuid4())
        fields = {
            "id": session_id,
            "user_id": user_id,
            "sub_agent_id": sub_agent_id,
            "phone_number": phone_number,
            "call_sid": call_sid,
            "use_session_memory": use_session_memory,
            "status": "active",
            "started_at": now,
            "created_at": now,
            "updated_at": now,
        }
        result = await db.execute(
            text("""
                INSERT INTO voice_sessions
                    (id, user_id, sub_agent_id, phone_number, call_sid,
                     use_session_memory, status, started_at, created_at, updated_at)
                VALUES
                    (:id, :user_id, :sub_agent_id, :phone_number, :call_sid,
                     :use_session_memory, 'active', :started_at, :created_at, :updated_at)
                RETURNING id, user_id, sub_agent_id, phone_number, call_sid,
                          gemini_session_handle, status, use_session_memory,
                          started_at, ended_at, created_at, updated_at
            """),
            fields,
        )
        row = result.mappings().first()
        if row is None:
            return None
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=session_id,
            action=AuditAction.CREATE,
            changes={"after": _serialize_for_audit(fields)},
        )
        return _row_to_session(row)

    async def update_handle(
        self,
        db: AsyncSession,
        actor: User,
        session_id: str,
        gemini_session_handle: str,
    ) -> bool:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                UPDATE voice_sessions
                SET gemini_session_handle = :handle, updated_at = :now
                WHERE id = :id
                RETURNING id
            """),
            {"id": session_id, "handle": gemini_session_handle, "now": now},
        )
        found = result.mappings().first() is not None
        if not found:
            logger.warning("update_handle: no voice session found with id=%s", session_id)
            return False
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=session_id,
            action=AuditAction.UPDATE,
            changes={"after": {"gemini_session_handle": "<redacted>", "updated_at": now.isoformat()}},
        )
        return True

    async def complete_session(self, db: AsyncSession, actor: User, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        await self.update(
            db=db,
            actor=actor,
            entity_id=session_id,
            fields={"status": "completed", "ended_at": now, "updated_at": now},
            fetch_before=False,
        )

    async def fail_session(self, db: AsyncSession, actor: User, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        await self.update(
            db=db,
            actor=actor,
            entity_id=session_id,
            fields={"status": "failed", "ended_at": now, "updated_at": now},
            fetch_before=False,
        )

    async def get_latest_resumable_session(
        self,
        db: AsyncSession,
        user_id: str,
        sub_agent_id: int | None = None,
    ) -> VoiceSession | None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=RESUME_WINDOW_MINUTES)
        params: dict = {"user_id": user_id, "cutoff": cutoff}
        agent_filter = ""
        if sub_agent_id is not None:
            agent_filter = "AND sub_agent_id = :sub_agent_id"
            params["sub_agent_id"] = sub_agent_id
        result = await db.execute(
            text(f"""
                SELECT id, user_id, sub_agent_id, phone_number, call_sid,
                       gemini_session_handle, status, use_session_memory,
                       started_at, ended_at, created_at, updated_at
                FROM voice_sessions
                WHERE user_id = :user_id
                  AND gemini_session_handle IS NOT NULL AND status = 'completed'
                  AND ended_at >= :cutoff
                  {agent_filter}
                ORDER BY ended_at DESC LIMIT 1
            """),
            params,
        )
        row = result.mappings().first()
        return _row_to_session(row) if row else None

    async def get_most_recent_sub_agent_ids(
        self,
        db: AsyncSession,
        user_id: str,
        limit: int = 5,
    ) -> list[int]:
        result = await db.execute(
            text("""
                SELECT sub_agent_id FROM voice_sessions
                WHERE user_id = :user_id AND sub_agent_id IS NOT NULL
                GROUP BY sub_agent_id ORDER BY MAX(started_at) DESC LIMIT :limit
            """),
            {"user_id": user_id, "limit": limit},
        )
        return [row["sub_agent_id"] for row in result.mappings().all()]
