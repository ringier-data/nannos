"""Conversation router."""

import logging

from config import config
from dependencies import require_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from models.user import User
from services.conversation_service import ConversationService


logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix='/api/v1/conversations', tags=['conversations'])

# Initialize service
conversation_service: ConversationService = ConversationService()


@router.get('/')
async def get_conversations_by_user(
    request: Request,
    user_id: str | None = None,
    limit: int = 20,
    user: User = Depends(require_auth),
) -> dict:
    """Get all conversations for a user.

    Args:
        limit: Maximum number of conversations to return (default: 20, max: 50)

    Returns:
        Dictionary containing:
        - user_id: The user ID
        - conversations: List of conversations ordered by last_message_at (newest first)
        - count: Number of conversations returned
    """
    try:
        # Validate limit
        if limit < 1 or limit > 50:
            raise HTTPException(status_code=400, detail='Limit must be between 1 and 50')
        # If user_id not provided, default to authenticated user
        if not user_id:
            user_id = user.id

        # Ensure authenticated user can only request their own conversations
        if not config.is_local() and str(user_id) != user.id:
            raise HTTPException(status_code=403, detail='Insufficient permissions for requested user_id')

        conversations = await conversation_service.get_conversations_by_user_id(
            user_id=str(user_id),
            limit=limit,
        )

        return {
            'user_id': user_id,
            'conversations': [
                {
                    'conversation_id': conv.conversation_id,
                    'user_id': conv.user_id,
                    'started_at': conv.started_at.isoformat(),
                    'last_message_at': conv.last_message_at.isoformat(),
                    'status': conv.status,
                    'metadata': conv.metadata,
                    'title': conv.title,
                    'agent_url': conv.agent_url,
                }
                for conv in conversations
            ],
            'count': len(conversations),
        }

    except HTTPException:
        raise
    except Exception as e:
        req_user = getattr(request.state, 'user', None)
        uid = getattr(req_user, 'id', '<unknown>') if req_user else '<unknown>'
        logger.error(f'Failed to get conversations for user {uid}: {e}')
        raise HTTPException(status_code=500, detail='Failed to retrieve conversations')


@router.get('/_debug/session')
async def debug_session(request: Request) -> dict:
    """Local-only debug endpoint returning session and user info.

    Use this to confirm that `SessionMiddleware` populated `request.state`.
    Only enabled in local mode to avoid leaking user data in production.
    """
    try:
        from config import config as _config

        if not _config.is_local():
            raise HTTPException(status_code=404, detail='Not found')

        session_id = getattr(request.state, 'session_id', None)
        session = getattr(request.state, 'session', None)
        user = getattr(request.state, 'user', None)
        id_token = getattr(request.state, 'id_token', None)
        access_token = getattr(request.state, 'access_token', None)

        return {
            'session_id': session_id,
            'session': None
            if session is None
            else (session.__dict__ if hasattr(session, '__dict__') else str(session)),
            'user': None if user is None else {'id': getattr(user, 'id', None), 'email': getattr(user, 'email', None)},
            'id_token_present': bool(id_token),
            'access_token_present': bool(access_token),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception('debug_session failed: %s', e)
        raise HTTPException(status_code=500, detail='Debug failed')
