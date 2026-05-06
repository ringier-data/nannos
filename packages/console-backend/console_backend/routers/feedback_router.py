"""API routes for message feedback."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text

from ..db.session import DbSession
from ..dependencies import User, require_auth, require_auth_or_bearer_token
from ..models.feedback import (
    MessageFeedbackCreate,
    MessageFeedbackResponse,
)
from ..services.feedback_service import FeedbackService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/conversations", tags=["feedback"])


def get_feedback_service(request: Request) -> FeedbackService:
    return request.app.state.feedback_service


async def _verify_conversation_ownership(
    db: DbSession, conversation_id: str, user_id: str, *, allow_external: bool = False
) -> None:
    """Verify the user owns the conversation. Raises 404 if not found or not owned.

    When *allow_external* is True the check is skipped for conversations that
    do not exist in the database (e.g. Slack / Google Chat conversations that
    are not tracked by console-backend).
    """
    result = await db.execute(
        text("SELECT user_id FROM conversations WHERE conversation_id = :cid"),
        {"cid": conversation_id},
    )
    row = result.mappings().first()
    if row is None:
        if allow_external:
            return
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")


@router.post("/{conversation_id}/messages/{message_id}/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    request: Request,
    conversation_id: str,
    message_id: str,
    body: MessageFeedbackCreate,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> MessageFeedbackResponse:
    await _verify_conversation_ownership(db, conversation_id, user.id, allow_external=True)
    service = get_feedback_service(request)
    return await service.upsert_feedback(
        db=db,
        user_id=user.id,
        conversation_id=conversation_id,
        message_id=message_id,
        rating=body.rating,
        comment=body.comment,
        sub_agent_id=body.sub_agent_id,
        task_id=body.task_id,
    )


@router.post("/{conversation_id}/feedback", status_code=status.HTTP_201_CREATED)
async def submit_conversation_feedback(
    request: Request,
    conversation_id: str,
    body: MessageFeedbackCreate,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> MessageFeedbackResponse:
    """Submit feedback for a conversation without specifying a message_id.

    Automatically resolves the last agent message in the conversation.
    Useful for feedback-request flows triggered on terminal state where
    the client doesn't have the real backend message ID.
    """
    await _verify_conversation_ownership(db, conversation_id, user.id, allow_external=True)

    # Resolve last agent message in the conversation
    result = await db.execute(
        text(
            "SELECT message_id FROM messages "
            "WHERE conversation_id = :cid AND role = 'assistant' "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"cid": conversation_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No agent message found in conversation")

    message_id = row["message_id"]
    service = get_feedback_service(request)
    return await service.upsert_feedback(
        db=db,
        user_id=user.id,
        conversation_id=conversation_id,
        message_id=message_id,
        rating=body.rating,
        comment=body.comment,
        sub_agent_id=body.sub_agent_id,
        task_id=body.task_id,
    )


@router.get("/{conversation_id}/feedback")
async def get_conversation_feedback(
    request: Request,
    conversation_id: str,
    db: DbSession,
    user: User = Depends(require_auth),
) -> list[MessageFeedbackResponse]:
    await _verify_conversation_ownership(db, conversation_id, user.id)
    service = get_feedback_service(request)
    return await service.get_feedback_for_conversation(db=db, conversation_id=conversation_id)


@router.delete("/{conversation_id}/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    request: Request,
    conversation_id: str,
    message_id: str,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> None:
    await _verify_conversation_ownership(db, conversation_id, user.id)
    service = get_feedback_service(request)
    deleted = await service.delete_feedback(
        db=db,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=user.id,
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
