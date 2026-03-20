"""Conversation service for managing conversations in DynamoDB."""

import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
import httpx
import uuid6
from aiodynamo.client import Client
from aiodynamo.credentials import Credentials, Key, StaticCredentials
from aiodynamo.errors import ItemNotFound
from aiodynamo.expressions import F, HashAndRangeKeyCondition, HashKey
from aiodynamo.http.httpx import HTTPX

from ..config import config
from ..exceptions import ConversationOwnershipError
from ..models.conversation import Conversation

logger = logging.getLogger(__name__)


class ConversationService:
    """Manages conversations in DynamoDB."""

    def __init__(self) -> None:
        """Initialize the conversation service."""
        dynamodb_config = config.dynamodb
        self.table_name = dynamodb_config.conversations_table
        # Conversations TTL - 90 days for retention
        self.conversation_ttl_seconds = 7776000  # 90 days

        try:
            _ = os.environ["ECS_CONTAINER_METADATA_URI"]
            credentials = Credentials.auto()
            logger.info("Using auto credentials (ECS environment)")
        except KeyError:
            boto_session = boto3.Session()
            boto3_credentials = boto_session.get_credentials()
            credentials = StaticCredentials(
                key=Key(
                    id=boto3_credentials.access_key,
                    secret=boto3_credentials.secret_key,
                    token=boto3_credentials.token,
                )
            )
            logger.info("Using static credentials (local environment)")

        self.client = Client(
            HTTPX(httpx.AsyncClient()),
            credentials,
            dynamodb_config.region,
        )
        self.table = self.client.table(self.table_name)

        logger.info(f"ConversationService initialized with table: {self.table_name}")

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
            # Query directly using composite key: userId (partition) + conversationId (sort)
            key_cond = HashAndRangeKeyCondition(
                hash_key=HashKey("userId", user_id),
                range_key_condition=F("conversationId").equals(conversation_id),
            )
            async for item in self.table.query(
                key_condition=key_cond,
                limit=1,
            ):
                return Conversation(
                    conversation_id=item["conversationId"],
                    user_id=item["userId"],
                    started_at=datetime.fromisoformat(item["startedAt"]),
                    last_message_at=datetime.fromisoformat(item["lastMessageAt"]),
                    status=item.get("status", "active"),
                    metadata=item.get("metadata", {}),
                    title=item.get("title", ""),
                    agent_url=item.get("agentUrl", ""),
                    sub_agent_config_hash=item.get("subAgentConfigHash"),
                    ttl=item["ttl"],
                )

            logger.debug(f"Conversation not found for user {user_id}: {conversation_id}")
            return None
        except ItemNotFound:
            logger.debug(f"Conversation not found: {conversation_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to get conversation: {e}")
            return None

    async def get_conversations_by_user_id(self, user_id: str, limit: int = 20) -> list[Conversation]:
        """Retrieve conversations for a user.

        Args:
            user_id: The user ID
            limit: Maximum number of conversations to return (default: 20)

        Returns:
            List of conversations ordered by conversationId (newest first with UUIDv7)
        """
        try:
            # Query base table by userId partition key
            # UUIDv7 conversationIds are time-ordered, so scan_forward=False gives newest first
            results = []
            async for item in self.table.query(
                key_condition=HashKey("userId", user_id),
                limit=limit,
                scan_forward=False,
            ):
                try:
                    # Parse timestamps
                    started_at_raw = item.get("startedAt")
                    last_msg_raw = item.get("lastMessageAt") or item.get("last_message_at") or started_at_raw

                    started_at = (
                        datetime.fromisoformat(started_at_raw)
                        if started_at_raw
                        else datetime.fromtimestamp(0, tz=timezone.utc)
                    )
                    last_message_at = datetime.fromisoformat(last_msg_raw) if last_msg_raw else started_at

                    results.append(
                        Conversation(
                            conversation_id=item["conversationId"],
                            user_id=item["userId"],
                            started_at=started_at,
                            last_message_at=last_message_at,
                            last_updated=last_message_at,
                            status=item.get("status", "active"),
                            metadata=item.get("metadata", {}),
                            title=item.get("title", ""),
                            agent_url=item.get("agentUrl", ""),
                            sub_agent_config_hash=item.get("subAgentConfigHash"),
                            ttl=item["ttl"],
                        )
                    )
                except Exception as conv_err:
                    logger.error(f"Failed to parse conversation item: {conv_err}; item={item}")
                    continue

            # Sort by last_message_at descending (newest first) to ensure correct order, since the uuidv7
            # conversationIds are time-ordered, but a user might update an old conversation as well.
            results.sort(key=lambda c: c.last_message_at, reverse=True)

            logger.debug(f"Retrieved {len(results)} conversations for user: {user_id}")
            return results

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
            session_ids: List of session IDs (optional)
            conversation_id: Optional conversation ID (will be generated if not provided)
            status: Conversation status (default: 'active')
            sub_agent_config_hash: Optional version hash for playground mode

        Returns:
            The created conversation
        """
        if conversation_id is None:
            conversation_id = str(uuid6.uuid7())

        now = datetime.now(tz=timezone.utc)
        ttl = int((now + timedelta(seconds=self.conversation_ttl_seconds)).timestamp())

        conversation = Conversation(
            conversation_id=conversation_id,
            user_id=user_id,
            started_at=now,
            last_message_at=now,
            status=status,
            metadata=metadata or {},
            title=title,
            agent_url=agent_url,
            sub_agent_config_hash=sub_agent_config_hash,
            ttl=ttl,
        )

        try:
            item = {
                "conversationId": conversation.conversation_id,
                "userId": conversation.user_id,
                "startedAt": conversation.started_at.isoformat(),
                "lastMessageAt": conversation.last_message_at.isoformat(),
                "status": conversation.status,
                "metadata": conversation.metadata,
                "title": conversation.title,
                "agentUrl": conversation.agent_url,
                "ttl": conversation.ttl,
            }
            if conversation.sub_agent_config_hash is not None:
                item["subAgentConfigHash"] = conversation.sub_agent_config_hash
            await self.table.put_item(item=item)
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
