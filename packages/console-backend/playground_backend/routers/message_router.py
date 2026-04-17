"""Message router."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import require_auth
from ..models.user import User

logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/messages", tags=["messages"])


@router.get("/{conversation_id}")
async def get_messages_by_conversation(
    request: Request, conversation_id: str, user: User = Depends(require_auth), limit: int = 100
) -> dict:
    """Get all messages for a conversation.

    Args:
        conversation_id: The conversation ID
        limit: Maximum number of messages to return (default: 100, max: 100)

    Returns:
        Dictionary containing:
        - conversation_id: The conversation ID
        - messages: List of messages ordered chronologically
        - count: Number of messages returned
    """
    try:
        # Validate limit
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="Limit must be between 1 and 100")

        messages_service = request.app.state.messages_service
        messages = await messages_service.get_messages_by_conversation(conversation_id, user.id, limit=limit)

        # Hydrate file parts with presigned URLs
        messages = await messages_service.hydrate_messages_files(messages)

        return {
            "conversation_id": conversation_id,
            "messages": [
                {
                    "conversation_id": msg.conversation_id,
                    "sort_key": msg.sort_key,
                    "user_id": msg.user_id,
                    "message_id": msg.message_id,
                    "role": msg.role,
                    "parts": msg.parts,
                    "task_id": msg.task_id,
                    "created_at": msg.created_at,
                    "state": msg.state,
                    "metadata": msg.metadata,
                    "kind": msg.kind,
                    "raw_payload": msg.raw_payload,
                }
                for msg in messages
            ],
            "count": len(messages),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get messages for conversation {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve messages")
