"""Conversation service for managing conversations in PostgreSQL."""

import logging
from datetime import datetime, timezone

import uuid6
from sqlalchemy import text

from ..db.connection import get_async_session_factory
from ..exceptions import ConversationOwnershipError
from ..models.conversation import Conversation

logger = logging.getLogger(__name__)


class ConversationService:
    """Manages conversations in PostgreSQL."""

    def __init__(self) -> None:
        """Initialize the conversation service."""
        self._session_factory = get_async_session_factory()
        logger.info("ConversationService initialized (PostgreSQL)")

    async def get_conversation(self, conversation_id: str, user_id: str) -> Conversation | None:
        """Retrieve a conversation by ID and validate ownership.

        Args:
            conversation_id: The conversation ID

            user_id: User ID to validate ownership. This is required — the
                method will only return a conversation owned by `user_id` or
                None if not found. If the conversation exists but is owned by
                a different user, a ConversationOwnershipError will be raised.

        Returns:
            The conversation or None if not found
        """
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text(
                        "SELECT * FROM conversations "
                        "WHERE conversation_id = :conversation_id AND user_id = :user_id"
                    ),
                    {"conversation_id": conversation_id, "user_id": user_id},
                )
                row = result.mappings().first()

            if not row:
                logger.debug(f"Conversation not found for user {user_id}: {conversation_id}")
                return None

            return self._row_to_conversation(row)
        except Exception as e:
            logger.error(f"Failed to get conversation: {e}")
            return None

    async def get_conversations_by_user_id(self, user_id: str, limit: int = 20) -> list[Conversation]:
        """Retrieve conversations for a user.

        Args:
            user_id: The user ID
            limit: Maximum number of conversations to return (default: 20)

        Returns:
            List of conversations ordered by last_message_at (newest first)
        """
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text(
                        "SELECT * FROM conversations "
                        "WHERE user_id = :user_id "
                        "ORDER BY last_message_at DESC "
                        "LIMIT :limit"
                    ),
                    {"user_id": user_id, "limit": limit},
                )
                rows = result.mappings().all()

            conversations = []
            for row in rows:
                try:
                    conversations.append(self._row_to_conversation(row))
                except Exception as conv_err:
                    logger.error(f"Failed to parse conversation row: {conv_err}; row={row}")
                    continue

            logger.debug(f"Retrieved {len(conversations)} conversations for user: {user_id}")
            return conversations

        except Exception as e:
            logger.error(f"Failed to get conversations for user {user_id}: {e}", exc_info=True)
            return []

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
        """Insert a new conversation.

        Args:
            user_id: The user ID
            title: Conversation title (optional)
            agent_url: Agent URL used in this conversation (optional)
            metadata: Optional metadata dictionary
            conversation_id: Optional conversation ID (will be generated if not provided)
            status: Conversation status (default: 'active')
            sub_agent_config_hash: Optional version hash for playground mode

        Returns:
            The created conversation
        """
        if conversation_id is None:
            conversation_id = str(uuid6.uuid7())

        now = datetime.now(tz=timezone.utc)

        conversation = Conversation(
            conversation_id=conversation_id,
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

        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        "INSERT INTO conversations "
                        "(conversation_id, user_id, started_at, last_message_at, last_updated, "
                        "status, title, agent_url, sub_agent_config_hash, metadata) "
                        "VALUES (:conversation_id, :user_id, :started_at, :last_message_at, :last_updated, "
                        ":status, :title, :agent_url, :sub_agent_config_hash, CAST(:metadata AS jsonb))"
                    ),
                    {
                        "conversation_id": conversation.conversation_id,
                        "user_id": conversation.user_id,
                        "started_at": conversation.started_at,
                        "last_message_at": conversation.last_message_at,
                        "last_updated": conversation.last_updated,
                        "status": conversation.status,
                        "title": conversation.title,
                        "agent_url": conversation.agent_url,
                        "sub_agent_config_hash": conversation.sub_agent_config_hash,
                        "metadata": _json_dumps(conversation.metadata),
                    },
                )
                await db.commit()
            logger.info(f"Inserted conversation: {conversation_id} for user: {user_id}")
            return conversation

        except Exception as e:
            logger.error(f"Failed to insert conversation: {e}")
            raise

    async def get_or_create_conversation(
        self,
        conversation_id: str,
        user_id: str,
        agent_url: str = "",
        message: str | None = None,
        sub_agent_config_hash: str | None = None,
    ) -> Conversation:
        """Ensure a conversation exists, creating it if necessary.

        Args:
            conversation_id: The conversation ID
            user_id: The user ID
            agent_url: Agent URL for this conversation
            message: Optional user message text to extract title from
            sub_agent_config_hash: Optional version hash for playground mode

        Returns:
            The existing or newly created conversation
        """
        # Check if conversation already exists
        conversation = await self.get_conversation(conversation_id, user_id=user_id)

        if conversation:
            # Validate that the provided user_id owns this conversation
            if conversation.user_id != user_id:
                logger.error(
                    f"Conversation ownership mismatch: conversation {conversation_id} owned by {conversation.user_id}, attempted by {user_id}"
                )
                raise ConversationOwnershipError(f"User {user_id} does not own conversation {conversation_id}")

        if not conversation:
            # Extract title from user message
            title = ""
            if message:
                # Use first 100 characters of message as title
                title = message[:100] if message else ""

            # Create new conversation
            conversation = await self.insert_conversation(
                conversation_id=conversation_id,
                user_id=user_id,
                agent_url=agent_url,
                title=title,
                metadata={},
                sub_agent_config_hash=sub_agent_config_hash,
            )
            logger.info(f"Created new conversation: {conversation_id} with title: {title[:50]}")

        return conversation

    @staticmethod
    def _row_to_conversation(row) -> Conversation:
        """Convert a database row mapping to a Conversation model."""
        return Conversation(
            conversation_id=row["conversation_id"],
            user_id=row["user_id"],
            started_at=row["started_at"],
            last_message_at=row["last_message_at"],
            last_updated=row["last_updated"],
            status=row["status"] or "active",
            metadata=row["metadata"] or {},
            title=row["title"] or "",
            agent_url=row["agent_url"] or "",
            sub_agent_config_hash=row["sub_agent_config_hash"],
        )


def _json_dumps(obj) -> str:
    """Serialize to JSON string for JSONB columns."""
    import json

    return json.dumps(obj, default=str)
