"""Conversation service for managing conversations in PostgreSQL."""

import logging
from datetime import datetime, timezone
from typing import Any

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

    async def get_conversations_by_user_id(
        self, user_id: str, limit: int = 20, search: str | None = None
    ) -> list[Conversation]:
        """Retrieve conversations for a user.

        Args:
            user_id: The user ID
            limit: Maximum number of conversations to return (default: 20)
            search: Optional case-insensitive substring to filter conversations by title

        Returns:
            List of ACTIVE conversations ordered by last_message_at (newest first).
            Soft-deleted ('archived') conversations are excluded.
        """
        try:
            # 'archived' is the soft-delete marker written by archive_conversation:
            # the row and its messages stay, but the user has removed it from
            # their history, so it must never come back in the list.
            conditions = ["user_id = :user_id", "status <> 'archived'"]
            params: dict[str, object] = {"user_id": user_id, "limit": limit}

            if search and search.strip():
                conditions.append("title ILIKE :search")
                params["search"] = f"%{search.strip()}%"

            query = (
                "SELECT * FROM conversations "
                f"WHERE {' AND '.join(conditions)} "
                "ORDER BY last_message_at DESC "
                "LIMIT :limit"
            )

            async with self._session_factory() as db:
                result = await db.execute(text(query), params)
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
        metadata: dict[str, Any] | None = None,
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
        embedded_sub_agent_id: str | None = None,
        page_context: dict[str, Any] | None = None,
    ) -> Conversation:
        """Ensure a conversation exists, creating it if necessary.

        Args:
            conversation_id: The conversation ID
            user_id: The user ID
            agent_url: Agent URL for this conversation
            message: Optional user message text to extract title from
            sub_agent_config_hash: Optional version hash for playground mode
            embedded_sub_agent_id: Set when the conversation is created by the
                embedded widget (execute-only sub-agent). Stamped into metadata so
                the console can label it and render it read-only — its turns assume
                a live host page (registered client objects, scoped agent).
            page_context: The sending page's context (embed SDK `metadata.pageContext`).
                Its stable slice is stamped into metadata ON CREATION ONLY — "this
                conversation started on campaign 123" is a fact about the
                conversation, so later sends from other pages must not rewrite it.

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

            metadata: dict[str, Any] = {}
            if embedded_sub_agent_id:
                metadata["embedded_sub_agent_id"] = embedded_sub_agent_id
            origin = conversation_page_context(page_context)
            if origin:
                metadata["page_context"] = origin

            # Create new conversation
            conversation = await self.insert_conversation(
                conversation_id=conversation_id,
                user_id=user_id,
                agent_url=agent_url,
                title=title,
                metadata=metadata,
                sub_agent_config_hash=sub_agent_config_hash,
            )
            logger.info(f"Created new conversation: {conversation_id} with title: {title[:50]}")

        return conversation

    async def update_summary(
        self,
        conversation_id: str,
        user_id: str,
        *,
        title: str,
        summary: str,
        title_source: str = "llm",
    ) -> bool:
        """Store an LLM-written title and one-line summary on a conversation.

        Metadata is MERGED (`||`), never replaced: `page_context` and
        `embedded_sub_agent_id` were stamped at creation and must survive. The
        `title_source` marker is what stops the generator running twice.

        `title_source` stays a parameter because of the one case where the model
        writes a summary for a conversation the USER has already named: the
        caller passes 'user' back so the name keeps its protection (see
        `rename_conversation` and conversation_summary.py).

        Returns False when the row does not exist or belongs to another user.
        """
        patch = {"summary": summary, "title_source": title_source}
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text(
                        "UPDATE conversations "
                        "SET title = :title, last_updated = :now, "
                        "    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb) "
                        "WHERE conversation_id = :conversation_id AND user_id = :user_id"
                    ),
                    {
                        "title": title,
                        "now": datetime.now(tz=timezone.utc),
                        "patch": _json_dumps(patch),
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                    },
                )
                await db.commit()
            return (result.rowcount or 0) > 0
        except Exception as e:
            logger.warning(f"Failed to store summary for conversation {conversation_id}: {e}")
            return False

    async def rename_conversation(self, conversation_id: str, user_id: str, *, title: str) -> bool:
        """Give a conversation the name the user typed.

        The `title_source` marker becomes 'user', which the background titler
        reads as "this one is named, leave the name alone" — without it the next
        completed turn would replace the user's name with a written one. The
        conversation still gets a summary if it has none yet; only the title is
        protected (conversation_summary.py).

        Metadata is MERGED (`||`), never replaced — same reason as
        `update_summary`.

        Ownership lives in the WHERE clause, not in a prior read: a conversation
        belonging to another user simply matches nothing.

        Returns False when the row does not exist or belongs to another user.
        """
        patch = {"title_source": "user"}
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text(
                        "UPDATE conversations "
                        "SET title = :title, last_updated = :now, "
                        "    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb) "
                        "WHERE conversation_id = :conversation_id AND user_id = :user_id"
                    ),
                    {
                        "title": title,
                        # NOT last_message_at: renaming is not activity, and that
                        # column is what orders the list.
                        "now": datetime.now(tz=timezone.utc),
                        "patch": _json_dumps(patch),
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                    },
                )
                await db.commit()
            return (result.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"Failed to rename conversation {conversation_id}: {e}")
            return False

    async def archive_conversation(self, conversation_id: str, user_id: str) -> bool:
        """Soft-delete a conversation: remove it from the user's history.

        The row and its messages stay put — only `status` flips to 'archived',
        which `get_conversations_by_user_id` filters out. Nothing else is
        touched, so feedback and usage rows keep pointing at a live row.

        Ownership is enforced by the WHERE clause, not by a prior read: a
        conversation belonging to another user simply matches nothing.

        Returns False when the row does not exist, belongs to another user, or
        was already archived.
        """
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    text(
                        "UPDATE conversations SET status = 'archived', last_updated = :now "
                        "WHERE conversation_id = :conversation_id AND user_id = :user_id "
                        "AND status <> 'archived'"
                    ),
                    {
                        "now": datetime.now(tz=timezone.utc),
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                    },
                )
                await db.commit()
            return (result.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"Failed to archive conversation {conversation_id}: {e}")
            return False

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


def conversation_page_context(page_context: Any) -> dict[str, Any] | None:
    """The slice of a send's page context that still describes the CONVERSATION.

    A send carries the live page: route, title, the entity on screen, but also the
    open tab, the visible rows, the breadcrumb trail. Only the first three still
    mean anything a week later — the rest belongs to that one prompt. Everything
    is re-capped here rather than trusted: the caps are the client's, and the
    client is a browser.

    Returns None when nothing usable is left, so no empty object is stored.
    """
    if not isinstance(page_context, dict):
        return None

    origin: dict[str, Any] = {}
    key = page_context.get("key")
    if isinstance(key, str) and key.strip():
        origin["key"] = key.strip()[:500]
    title = page_context.get("title")
    if isinstance(title, str) and title.strip():
        origin["title"] = title.strip()[:160]

    entity = page_context.get("entity")
    if isinstance(entity, dict):
        fields = {}
        for field in ("type", "id", "name"):
            value = entity.get(field)
            if isinstance(value, (str, int)) and str(value).strip():
                fields[field] = str(value).strip()[:200]
        # A type without an id (or the reverse) names nothing — keep neither.
        if fields.get("type") and fields.get("id"):
            origin["entity"] = fields

    return origin or None
