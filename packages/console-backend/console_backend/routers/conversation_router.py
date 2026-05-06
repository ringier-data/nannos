"""Conversation router."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import config
from ..dependencies import require_auth
from ..models.user import User

logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("/")
async def get_conversations_by_user(
    request: Request,
    user_id: str | None = None,
    limit: int = 20,
    sub_agent_config_hash: str | None = None,
    exclude_playground: bool = False,
    user: User = Depends(require_auth),
) -> dict:
    """Get all conversations for a user.

    Args:
        limit: Maximum number of conversations to return (default: 20, max: 50)
        sub_agent_config_hash: Optional filter by sub-agent config version hash
        exclude_playground: If True, exclude conversations with sub_agent_config_hash set

    Returns:
        Dictionary containing:
        - user_id: The user ID
        - conversations: List of conversations ordered by last_message_at (newest first)
        - count: Number of conversations returned
    """
    try:
        # Validate limit
        if limit < 1 or limit > 50:
            raise HTTPException(status_code=400, detail="Limit must be between 1 and 50")
        # If user_id not provided, default to authenticated user
        if not user_id:
            user_id = user.id

        # Ensure authenticated user can only request their own conversations
        if not config.is_local() and str(user_id) != user.id:
            raise HTTPException(status_code=403, detail="Insufficient permissions for requested user_id")

        conversations = await request.app.state.conversation_service.get_conversations_by_user_id(
            user_id=str(user_id),
            limit=limit,
        )

        # Filter by sub_agent_config_hash if provided
        if sub_agent_config_hash is not None:
            conversations = [c for c in conversations if c.sub_agent_config_hash == sub_agent_config_hash]
        elif exclude_playground:
            # Exclude playground conversations (those with sub_agent_config_hash set)
            conversations = [c for c in conversations if c.sub_agent_config_hash is None]

        return {
            "user_id": user_id,
            "conversations": [
                {
                    "conversation_id": conv.conversation_id,
                    "user_id": conv.user_id,
                    "started_at": conv.started_at.isoformat(),
                    "last_message_at": conv.last_message_at.isoformat(),
                    "status": conv.status,
                    "metadata": conv.metadata,
                    "title": conv.title,
                    "agent_url": conv.agent_url,
                    "sub_agent_config_hash": conv.sub_agent_config_hash,
                }
                for conv in conversations
            ],
            "count": len(conversations),
        }

    except HTTPException:
        raise
    except Exception as e:
        req_user = getattr(request.state, "user", None)
        uid = getattr(req_user, "id", "<unknown>") if req_user else "<unknown>"
        logger.error(f"Failed to get conversations for user {uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve conversations")


@router.get("/_debug/session")
async def debug_session(request: Request) -> dict:
    """Local-only debug endpoint returning session and user info.

    Use this to confirm that `SessionMiddleware` populated `request.state`.
    Only enabled in local mode to avoid leaking user data in production.
    """
    try:
        from console_backend.config import config as _config

        if not _config.is_local():
            raise HTTPException(status_code=404, detail="Not found")

        session_id = getattr(request.state, "session_id", None)
        session = getattr(request.state, "session", None)
        user = getattr(request.state, "user", None)
        id_token = getattr(request.state, "id_token", None)
        access_token = getattr(request.state, "access_token", None)

        return {
            "session_id": session_id,
            "session": None
            if session is None
            else (session.__dict__ if hasattr(session, "__dict__") else str(session)),
            "user": None if user is None else {"id": getattr(user, "id", None), "email": getattr(user, "email", None)},
            "id_token_present": bool(id_token),
            "access_token_present": bool(access_token),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("debug_session failed: %s", e)
        raise HTTPException(status_code=500, detail="Debug failed")
