"""Service for managing message feedback."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.feedback import FeedbackRating, MessageFeedbackResponse
from ..repositories.feedback_repository import FeedbackRepository

logger = logging.getLogger(__name__)


class FeedbackService:
    def __init__(self) -> None:
        self._repository: FeedbackRepository | None = None

    def set_repository(self, repository: FeedbackRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> FeedbackRepository:
        if self._repository is None:
            raise RuntimeError("FeedbackRepository not injected. Call set_repository() during initialization.")
        return self._repository

    async def upsert_feedback(
        self,
        db: AsyncSession,
        user_id: str,
        conversation_id: str,
        message_id: str,
        rating: FeedbackRating,
        comment: str | None = None,
        sub_agent_id: str | None = None,
        task_id: str | None = None,
    ) -> MessageFeedbackResponse:
        feedback = await self.repository.upsert(
            db=db,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            rating=rating,
            comment=comment,
            sub_agent_id=sub_agent_id,
            task_id=task_id,
        )
        await db.commit()
        logger.info(
            f"Feedback {rating.value} for message {message_id} in conversation {conversation_id} by user {user_id}"
        )
        return feedback

    async def get_feedback_for_conversation(
        self,
        db: AsyncSession,
        conversation_id: str,
    ) -> list[MessageFeedbackResponse]:
        return await self.repository.get_for_conversation(
            db=db,
            conversation_id=conversation_id,
        )

    async def delete_feedback(
        self,
        db: AsyncSession,
        conversation_id: str,
        message_id: str,
        user_id: str,
    ) -> bool:
        deleted = await self.repository.delete(
            db=db,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
        )
        if deleted:
            await db.commit()
            logger.info(f"Feedback deleted for message {message_id} by user {user_id}")
        return deleted
