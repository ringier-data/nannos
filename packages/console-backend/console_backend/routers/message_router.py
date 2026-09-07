"""Message router."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import require_auth_or_bearer_token
from ..models.user import User
from ..exceptions import UnknownCursorError

logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/messages", tags=["messages"])


@router.get("/{conversation_id}")
async def get_messages_by_conversation(
    # Bearer accepted: the embed SDK (ADR-0004) resumes conversations cross-origin
    # with the user's access token — no console session cookie exists.
    request: Request,
    conversation_id: str,
    user: User = Depends(require_auth_or_bearer_token),
    limit: int = 100,
    before: str | None = None,
) -> dict:
    """Get one page of a conversation's messages, newest page first.

    Args:
        conversation_id: The conversation ID
        limit: Maximum number of messages to return (default: 100, max: 100)
        before: Cursor — the `message_id` to page back from (exclusive). Omit to
            get the newest page; pass the previous response's `next_cursor` to
            walk further back through the history.

    Returns:
        Dictionary containing:
        - conversation_id: The conversation ID
        - messages: List of messages ordered chronologically
        - count: Number of messages returned
        - has_more: Whether older messages exist before this page
        - next_cursor: Cursor to pass as `before` for the next (older) page, or None
    """
    try:
        # Validate limit
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="Limit must be between 1 and 100")

        messages_service = request.app.state.messages_service
        try:
            page = await messages_service.get_messages_by_conversation(
                conversation_id, user.id, limit=limit, before=before
            )
        except UnknownCursorError:
            raise HTTPException(status_code=400, detail="Unknown pagination cursor") from None

        # Hydrate file parts with presigned URLs
        messages = await messages_service.hydrate_messages_files(page.messages)

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
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get messages for conversation {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve messages")
