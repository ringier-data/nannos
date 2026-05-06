"""Repository for message feedback (no audit logging — high volume, low risk)."""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.feedback import FeedbackRating, MessageFeedbackResponse

logger = logging.getLogger(__name__)


def _row_to_response(row: Any) -> MessageFeedbackResponse:
    return MessageFeedbackResponse(
        id=str(row["id"]),
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        user_id=row["user_id"],
        rating=row["rating"],
        comment=row["comment"],
        sub_agent_id=row["sub_agent_id"],
        task_id=row["task_id"],
        created_at=row["created_at"],
    )


class FeedbackRepository:
    async def upsert(
        self,
        db: AsyncSession,
        conversation_id: str,
        message_id: str,
        user_id: str,
        rating: FeedbackRating,
        comment: str | None = None,
        sub_agent_id: str | None = None,
        task_id: str | None = None,
    ) -> MessageFeedbackResponse:
        query = text("""
            INSERT INTO message_feedback (conversation_id, message_id, user_id, rating, comment, sub_agent_id, task_id)
            VALUES (:conversation_id, :message_id, :user_id, :rating, :comment, :sub_agent_id, :task_id)
            ON CONFLICT (conversation_id, message_id, user_id)
            DO UPDATE SET rating = :rating, comment = :comment, sub_agent_id = :sub_agent_id, task_id = :task_id
            RETURNING *
        """)
        result = await db.execute(
            query,
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "user_id": user_id,
                "rating": rating.value,
                "comment": comment,
                "sub_agent_id": sub_agent_id,
                "task_id": task_id,
            },
        )
        row = result.mappings().first()
        assert row is not None
        return _row_to_response(row)

    async def get_for_conversation(
        self,
        db: AsyncSession,
        conversation_id: str,
    ) -> list[MessageFeedbackResponse]:
        query = text("""
            SELECT * FROM message_feedback
            WHERE conversation_id = :conversation_id
            ORDER BY created_at DESC
        """)
        result = await db.execute(query, {"conversation_id": conversation_id})
        rows = result.mappings().all()
        return [_row_to_response(row) for row in rows]

    async def delete(
        self,
        db: AsyncSession,
        conversation_id: str,
        message_id: str,
        user_id: str,
    ) -> bool:
        query = text("""
            DELETE FROM message_feedback
            WHERE conversation_id = :conversation_id
              AND message_id = :message_id
              AND user_id = :user_id
            RETURNING id
        """)
        result = await db.execute(
            query,
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "user_id": user_id,
            },
        )
        return result.first() is not None
