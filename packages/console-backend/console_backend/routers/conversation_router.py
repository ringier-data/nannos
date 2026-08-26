"""Conversation router."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

from ..config import config
from ..dependencies import require_auth_or_bearer_token
from ..models.user import User
from ..services.conversation_summary import MAX_TITLE_CHARS

logger = logging.getLogger(__name__)


class RenameConversationRequest(BaseModel):
    """The new name for a conversation, as the user typed it."""

    title: str

    @field_validator("title")
    @classmethod
    def _clean(cls, value: str) -> str:
        """Collapse whitespace and hold the name to what a list row can show.

        The same ceiling the LLM titler works to, so a renamed conversation sits
        in the list like any other. A blank name is a 422, not an untitling —
        there is no way back to the generated name once it is gone.
        """
        title = " ".join(value.split())
        if not title:
            raise ValueError("title must not be empty")
        if len(title) > MAX_TITLE_CHARS:
            raise ValueError(f"title must be at most {MAX_TITLE_CHARS} characters")
        return title

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("/")
async def get_conversations_by_user(
    request: Request,
    user_id: str | None = None,
    limit: int = 20,
    sub_agent_config_hash: str | None = None,
    exclude_playground: bool = False,
    embedded_sub_agent_id: str | None = None,
    search: str | None = None,
    # Bearer accepted: the embed SDK (Embedded Nannos, ADR-0004) lists conversations
    # cross-origin with the user's access token — no console session cookie exists.
    user: User = Depends(require_auth_or_bearer_token),
) -> dict:
    """Get all conversations for a user.

    Args:
        limit: Maximum number of conversations to return (default: 20, max: 50)
        sub_agent_config_hash: Optional filter by sub-agent config version hash
        exclude_playground: If True, exclude conversations with sub_agent_config_hash set
        embedded_sub_agent_id: Only conversations created by the embedded widget scoped
            to this sub-agent (metadata stamp). The embed SDK passes this so a host
            application only ever receives its OWN conversations — console and
            other-app conversation titles must not reach a third-party page.
        search: Optional case-insensitive substring to filter conversations by title

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
            search=search,
        )

        # Filter by sub_agent_config_hash if provided
        if sub_agent_config_hash is not None:
            conversations = [c for c in conversations if c.sub_agent_config_hash == sub_agent_config_hash]
        elif exclude_playground:
            # Exclude playground conversations (those with sub_agent_config_hash set)
            conversations = [c for c in conversations if c.sub_agent_config_hash is None]

        # Scope to one embedded application's conversations (see docstring).
        if embedded_sub_agent_id is not None:
            conversations = [
                c for c in conversations if c.metadata.get("embedded_sub_agent_id") == embedded_sub_agent_id
            ]

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


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    request: Request,
    conversation_id: str,
    # Bearer accepted: the embed SDK (ADR-0004) deletes cross-origin with the
    # user's access token, same as the sibling feedback endpoints.
    user: User = Depends(require_auth_or_bearer_token),
) -> None:
    """Remove a conversation from the user's history (soft delete).

    The row and its messages are kept; the conversation is marked 'archived',
    which drops it out of the list. Ownership is enforced in the UPDATE, so a
    conversation belonging to someone else is indistinguishable from a missing
    one — 404 either way, and nothing leaks about whose it is.
    """
    archived = await request.app.state.conversation_service.archive_conversation(
        conversation_id=conversation_id,
        user_id=user.id,
    )
    if not archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")


@router.patch("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def rename_conversation(
    request: Request,
    conversation_id: str,
    body: RenameConversationRequest,
    # Bearer accepted: the embed SDK (ADR-0004) renames cross-origin with the
    # user's access token, same as the sibling delete endpoint.
    user: User = Depends(require_auth_or_bearer_token),
) -> None:
    """Rename a conversation.

    The name is stamped as the user's (`metadata.title_source = 'user'`), which
    stops the background titler replacing it after the next turn.

    Ownership is enforced in the UPDATE, so a conversation belonging to someone
    else is indistinguishable from a missing one — 404 either way.
    """
    renamed = await request.app.state.conversation_service.rename_conversation(
        conversation_id=conversation_id,
        user_id=user.id,
        title=body.title,
    )
    if not renamed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")


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
