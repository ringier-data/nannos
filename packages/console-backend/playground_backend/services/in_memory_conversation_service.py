"""In-memory conversation service — drop-in replacement for PostgreSQL-backed ConversationService.

Used when USE_IN_MEMORY_STORE is set (local development without PostgreSQL).
Data is lost on process restart.
"""

import logging
import uuid
from datetime import datetime, timezone

from ..models.conversation import Conversation

logger = logging.getLogger(__name__)


def _uuid7_str() -> str:
    """Generate a UUIDv7-like string (time-ordered)."""
    return str(uuid.uuid4())  # Good enough for local dev; ordering not critical


class InMemoryConversationService:
    """In-memory conversation store matching ConversationService's public API."""

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        # Index: user_id -> list of conversation_ids (for listing)
        self._user_index: dict[str, list[str]] = {}
        logger.warning("Using in-memory conversation store — conversations will not survive restarts")

    async def get_conversation(
        self,
        conversation_id: str,
        user_id: str,
    ) -> Conversation | None:
        conv = self._conversations.get(conversation_id)
        if conv and conv.user_id == user_id:
            return conv
        return None

    async def get_conversations_by_user_id(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[Conversation]:
        conv_ids = self._user_index.get(user_id, [])
        conversations = []
        for cid in reversed(conv_ids):  # Most recent first
            conv = self._conversations.get(cid)
            if conv and conv.status == "active":
                conversations.append(conv)
                if len(conversations) >= limit:
                    break
        return conversations

    async def insert_conversation(
        self,
        user_id: str,
        title: str = "",
        agent_url: str = "",
        metadata: dict[str, str] | None = None,
        conversation_id: str | None = None,
        status: str = "active",
        sub_agent_config_hash: str | None = None,
    ) -> Conversation:
        now = datetime.now(timezone.utc)
        cid = conversation_id or _uuid7_str()

        conv = Conversation(
            conversation_id=cid,
            user_id=user_id,
            started_at=now,
            last_message_at=now,
            last_updated=now,
            status=status,
            metadata=metadata or {},
            title=title,
            agent_url=agent_url,
            sub_agent_config_hash=sub_agent_config_hash,
        )
        self._conversations[cid] = conv
        self._user_index.setdefault(user_id, []).append(cid)
        return conv

    async def get_or_create_conversation(
        self,
        conversation_id: str,
        user_id: str,
        agent_url: str = "",
        message: str | None = None,
        sub_agent_config_hash: str | None = None,
    ) -> Conversation:
        existing = await self.get_conversation(conversation_id, user_id)
        if existing:
            return existing
        title = (message[:50] + "...") if message and len(message) > 50 else (message or "")
        return await self.insert_conversation(
            user_id=user_id,
            title=title,
            agent_url=agent_url,
            conversation_id=conversation_id,
            sub_agent_config_hash=sub_agent_config_hash,
        )
