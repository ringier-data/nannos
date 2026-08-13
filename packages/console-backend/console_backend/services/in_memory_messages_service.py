"""In-memory messages service — drop-in replacement for PostgreSQL-backed MessagesService.

Used when USE_IN_MEMORY_STORE is set (local development without PostgreSQL).
Data is lost on process restart.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from a2a.types import TaskState

from ..models.message import Message, MessagePage
from .messages_service import UnknownCursorError

logger = logging.getLogger(__name__)


class InMemoryMessagesService:
    """In-memory message store matching MessagesService's public API."""

    def __init__(self, conversation_service=None) -> None:
        self._conversation_service = conversation_service
        # conversation_id -> list of Message (sorted by sort_key)
        self._messages: dict[str, list[Message]] = {}
        logger.warning("Using in-memory message store — messages will not survive restarts")

    async def get_messages_by_conversation(
        self,
        conversation_id: str,
        user_id: str,
        limit: int = 100,
        before: str | None = None,
    ) -> MessagePage:
        """Return the newest `limit` messages older than the `before` cursor.

        Mirrors MessagesService's keyset pagination; insertion order stands in for
        the `(created_at, id)` ordering the PostgreSQL implementation uses.
        """
        messages = self._messages.get(conversation_id, [])
        user_msgs = [m for m in messages if m.user_id == user_id]

        if before is not None:
            cursor_idx = next((i for i, m in enumerate(user_msgs) if m.message_id == before), None)
            if cursor_idx is None:
                raise UnknownCursorError(f"Unknown cursor for conversation {conversation_id}")
            user_msgs = user_msgs[:cursor_idx]

        page = user_msgs[-limit:] if limit else []
        return MessagePage(messages=page, has_more=len(user_msgs) > len(page))

    async def insert_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        parts: list[dict[str, Any]],
        task_id: str = "",
        state: TaskState = TaskState.TASK_STATE_UNSPECIFIED,
        raw_payload: str = "",
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        kind: str = "",
    ) -> Message:
        now = datetime.now(timezone.utc)
        mid = message_id or str(uuid.uuid4())
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        sort_key = f"MSG#{ts}#{mid}"

        msg = Message(
            conversation_id=conversation_id,
            sort_key=sort_key,
            user_id=user_id,
            message_id=mid,
            role=role,
            parts=parts,
            task_id=task_id,
            created_at=ts,
            state=state,
            raw_payload=raw_payload,
            metadata=metadata or {},
            kind=kind,
        )
        self._messages.setdefault(conversation_id, []).append(msg)
        return msg

    async def save_agent_response(
        self,
        response_data: dict[str, Any],
        conversation_id: str,
        user_id: str,
    ) -> Message | None:
        try:
            from .messages_service import _parse_agent_response

            parsed = _parse_agent_response(response_data)
            return await self.insert_message(
                conversation_id=conversation_id,
                user_id=user_id,
                **parsed,
            )
        except Exception as e:
            logger.error(f"Failed to save agent response: {e}")
            return None

    async def hydrate_message_files(self, message: Message) -> Message:
        # No S3 URLs to hydrate in local mode
        return message

    async def hydrate_messages_files(self, messages: list[Message]) -> list[Message]:
        return messages
