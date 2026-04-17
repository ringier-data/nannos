"""Messages service for managing messages in DynamoDB."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import boto3
import httpx
from a2a.types import FilePart, FileWithUri, Part, TaskState
from aiodynamo.client import Client
from aiodynamo.credentials import Credentials, Key, StaticCredentials
from aiodynamo.expressions import F, HashAndRangeKeyCondition, HashKey
from aiodynamo.http.httpx import HTTPX
from uuid6 import uuid7

from ..config import config
from ..models.message import Message
from .file_storage_service import FileStorageService

logger = logging.getLogger(__name__)


EXPIRY_BUFFER_SECONDS = (
    300  # 5 minutes buffer to account for clock skew and ensure URLs are refreshed before they expire
)


def _serialize_part(p):
    # Already a plain mapping
    if isinstance(p, dict):
        return p
    # Pydantic v2: model_dump
    if hasattr(p, "model_dump"):
        try:
            dumped = p.model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            # Unwrap RootModel that might produce {'root': {...}}
            if "root" in dumped and isinstance(dumped["root"], dict):
                return dumped["root"]
            return dumped

    # Fallback to string representation
    return {"value": str(p)}


def _parse_status_update(response_data: dict[str, Any]) -> dict[str, Any]:
    """Parse status-update kind responses.

    Status updates can have:
    - status.message with nested message parts
    - status.state without message (pure status event)
    - artifact with parts
    """
    # A2A 1.0.0: final field removed from protocol (terminal states indicate finality)
    kind = response_data.get("kind", "status-update")
    task_id = response_data.get("taskId", response_data.get("id", ""))
    # Always generate a new message ID for status updates to avoid duplicates
    # Only use the response ID if it's explicitly set in nested message
    message_id = str(uuid7())

    parts = []
    role = "assistant"
    metadata = response_data.get("metadata", {}) or {}
    state = TaskState.completed

    status = response_data.get("status")
    if isinstance(status, dict):
        state_val = status.get("state", "completed")
        try:
            state = TaskState(state_val)
        except Exception:
            state = TaskState.unknown

        # Check for nested message in status
        nested = status.get("message")
        if isinstance(nested, dict):
            parts = nested.get("parts", [])
            # Use nested message ID if explicitly provided, otherwise keep generated UUID
            message_id = nested.get("messageId", message_id)
            role = nested.get("role", "assistant")
            metadata = nested.get("metadata", metadata) or {}
        else:
            # Pure status update without message content
            # Create a synthetic TextPart for logging (A2A Part union only supports text/file/data)
            state_text = status.get("state", "unknown")
            ts = status.get("timestamp") or status.get("time")
            status_text = f"Status: {state_text}"
            if ts:
                status_text += f" at {ts}"
            parts = [{"kind": "text", "text": status_text}]

    # Check for artifact (artifact-update kind)
    if "artifact" in response_data and isinstance(response_data.get("artifact"), dict):
        art = response_data["artifact"]
        parts = art.get("parts", parts)
        # Use artifact ID if explicitly provided, otherwise keep generated UUID
        message_id = art.get("artifactId", message_id)
        kind = "artifact-update"

    # Normalize role
    if isinstance(role, str) and role.lower() == "agent":
        role = "assistant"

    # Ensure parts is a list
    if not isinstance(parts, list):
        parts = [parts] if parts else []

    return {
        "parts": parts,
        "message_id": message_id,
        "role": role,
        "metadata": metadata,
        "state": state,
        "kind": kind,
        "task_id": task_id,
    }


def _parse_task(response_data: dict[str, Any]) -> dict[str, Any]:
    """Parse task kind responses.

    Task responses typically contain:
    - history array with previous messages
    - status with current task state
    - id as the task identifier
    """
    # A2A 1.0.0: final field removed from protocol (terminal states indicate finality)
    kind = response_data.get("kind", "task")
    task_id = response_data.get("id", response_data.get("taskId", ""))
    message_id = task_id or str(uuid7())

    role = "assistant"
    metadata = response_data.get("metadata", {}) or {}
    parts = []

    # Extract state from status
    status = response_data.get("status")
    if isinstance(status, dict):
        state_val = status.get("state", "submitted")
        try:
            state = TaskState(state_val)
        except Exception:
            state = TaskState.unknown
    else:
        state = TaskState.submitted

    # Task responses may have history which should be saved separately
    history = None
    if "history" in response_data and isinstance(response_data.get("history"), list):
        history = response_data.get("history")

    # Create a minimal task event part (use 'text' kind for compatibility)
    # Render human-friendly state text when we have a TaskState enum
    if hasattr(state, "value"):
        state_text = state.value
    else:
        state_text = str(state)

    parts = [{"kind": "text", "text": f"Task {state_text}: {task_id}"}]

    result = {
        "parts": parts,
        "message_id": message_id,
        "role": role,
        "metadata": metadata,
        "state": state,
        "kind": kind,
        "task_id": task_id,
    }

    if history is not None:
        result["history"] = history

    return result


def _parse_agent_response(response_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize various agent response shapes into a consistent dict.

    Dispatches to specialized parsers based on response kind:
    - status-update / artifact-update -> _parse_status_update
    - task -> _parse_task

    Returns keys: parts, message_id, role, metadata, state, kind, task_id, history (optional)
    Note: A2A 1.0.0 removed 'final' field - terminal states indicate finality

    Raises:
        ValueError: If response kind is unknown or unsupported
    """
    kind = response_data.get("kind", "unknown")

    # Dispatch to specialized parsers
    if kind in ("status-update", "artifact-update"):
        return _parse_status_update(response_data)
    if kind == "task":
        return _parse_task(response_data)

    # No fallback - raise exception for unknown kinds
    raise ValueError(
        f"Unsupported agent response kind: '{kind}'. "
        f"Supported kinds: 'status-update', 'artifact-update', 'task'. "
        f"Response data: {json.dumps(response_data, default=str)[:200]}"
    )


class MessagesService:
    """Manages messages in DynamoDB."""

    def __init__(self, conversation_service=None) -> None:
        """Initialize the messages service.

        Args:
            conversation_service: Optional ConversationService instance for updating conversation metadata
        """
        self.conversation_service = conversation_service
        dynamodb_config = config.dynamodb
        self.table_name = dynamodb_config.messages_table
        # Messages TTL - 90 days for retention
        self.message_ttl_seconds = 7776000  # 90 days

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

        logger.info(f"MessagesService initialized with table: {self.table_name}")

    async def get_messages_by_conversation(self, conversation_id: str, user_id: str, limit: int = 100) -> list[Message]:
        """Retrieve messages for a conversation.

        Args:
            conversation_id: The conversation ID (partition key)
            limit: Maximum number of messages to return (default: 100)

        Returns:
            List of messages ordered by sort_key (chronological order)
        """
        try:
            results = []

            try:
                key_cond = HashAndRangeKeyCondition(
                    hash_key=HashKey("conversationId", conversation_id),
                    range_key_condition=F("sortKey").begins_with("MSG#"),
                )

                query_kwargs = {
                    "key_condition": key_cond,
                    "limit": limit,
                    "filter_expression": F("userId") == str(user_id),
                }

                async for item in self.table.query(**query_kwargs):
                    try:
                        stored_state = item.get("state", "unknown")
                        try:
                            stored_state_enum = TaskState(stored_state)
                        except Exception:
                            stored_state_enum = TaskState.unknown

                        results.append(
                            Message(
                                conversation_id=item["conversationId"],
                                sort_key=item["sortKey"],
                                user_id=item["userId"],
                                message_id=item["messageId"],
                                role=item["role"],
                                parts=item.get("parts", []),
                                task_id=item.get("taskId", ""),
                                created_at=item["createdAt"],
                                state=stored_state_enum,
                                raw_payload=item.get("rawPayload", ""),
                                metadata=item.get("metadata", {}),
                                ttl=item["ttl"],
                                final=item.get("final", False),
                                kind=item.get("kind", ""),
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Failed to parse message item for conversation %s: %s",
                            conversation_id,
                            item,
                        )
                        continue

            except Exception:
                logger.exception(f"Query failed for conversation {conversation_id}")
                return []

            logger.debug(f"Retrieved {len(results)} messages for conversation: {conversation_id}")

            return results

        except Exception as e:
            logger.exception(f"Failed to get messages for conversation {conversation_id}: {e}")
            return []

    async def insert_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        parts: list[dict[str, Any]],
        task_id: str = "",
        state: TaskState = TaskState.unknown,
        raw_payload: str = "",
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        final: bool = False,  # DEPRECATED: A2A 1.0.0 removes this field
        kind: str = "",
    ) -> Message:
        """Insert a new message.

        Args:
            conversation_id: The conversation ID (partition key)
            user_id: The user ID
            role: Message role ('user' or 'assistant')
            parts: Array of message parts [{'kind': 'text', 'text': '...'}, {'kind': 'file', 'uri': '...'}]
            task_id: Task ID (optional)
            state: Task processing state. This represents the message's TaskState
                and is used to track processing lifecycle.
            raw_payload: Original JSON payload (optional)
            metadata: Optional metadata dictionary
            message_id: Optional message ID (will be generated if not provided)
            final: DEPRECATED - A2A 1.0.0 removes this field (terminal states indicate finality)
            kind: Message kind ('message', 'status-update', etc.) (optional)

        Returns:
            The created message
        """
        if message_id is None:
            message_id = str(uuid7())

        created_at = datetime.now(tz=timezone.utc)
        created_at_iso = created_at.isoformat()
        timestamp_ms = int(created_at.timestamp() * 1000)  # Milliseconds for sort key

        # Construct composite sort key: MSG#<timestamp>#<messageId>
        sort_key = f"MSG#{timestamp_ms}#{message_id}"

        ttl = int((created_at + timedelta(seconds=self.message_ttl_seconds)).timestamp())

        # Serialize/normalize `parts` to plain mappings for storage and use
        db_parts = [_serialize_part(p) for p in parts]
        db_parts = cast("list[Part]", db_parts)

        message = Message(
            conversation_id=conversation_id,
            sort_key=sort_key,
            user_id=user_id,
            message_id=message_id,
            role=role,
            parts=db_parts,
            task_id=task_id,
            created_at=created_at_iso,
            state=state,
            raw_payload=raw_payload,
            metadata=metadata or {},
            ttl=ttl,
            final=final,
            kind=kind,
        )

        try:
            await self.table.put_item(
                item={
                    "conversationId": message.conversation_id,
                    "sortKey": message.sort_key,
                    "userId": message.user_id,
                    "messageId": message.message_id,
                    "role": message.role,
                    "parts": db_parts,
                    "taskId": message.task_id,
                    "createdAt": message.created_at,
                    "state": message.state.value if hasattr(message.state, "value") else str(message.state),
                    "rawPayload": message.raw_payload,
                    "metadata": message.metadata,
                    "ttl": message.ttl,
                    "final": message.final,
                    "kind": message.kind,
                }
            )

            logger.info(f"Inserted message: {message_id} in conversation: {conversation_id}")
            return message
        except Exception as e:
            logger.error(f"Failed to insert message: {e}")
            raise

    async def save_agent_response(
        self,
        response_data: dict[str, Any],
        conversation_id: str,
        user_id: str,
    ) -> Message | None:
        """Save agent response to DynamoDB.

        Args:
            response_data: Full response data from agent
            conversation_id: The conversation ID
            user_id: The user ID

        Returns:
            The created message or None if save failed
        """
        try:
            parsed = _parse_agent_response(response_data)

            parts = parsed["parts"]
            message_id = parsed["message_id"]
            role = parsed["role"]
            metadata = parsed["metadata"]
            state = parsed["state"]
            # A2A 1.0.0: final field removed (terminal states indicate finality)
            kind = parsed["kind"]
            task_id = parsed["task_id"]

            message = await self.insert_message(
                conversation_id=conversation_id,
                user_id=user_id,
                role=role,
                parts=parts,
                task_id=task_id,
                state=state,
                raw_payload=json.dumps(response_data, default=str),
                metadata=metadata or {},
                message_id=message_id,
                # final parameter omitted - A2A 1.0.0 compliance
                kind=kind,
            )

            logger.info(
                "Saved agent response (kind=%s, state=%s) message_id=%s to conversation %s",
                kind,
                state,
                message_id,
                conversation_id,
            )
            return message

        except Exception:
            logger.exception("Failed to save agent response")
            return None

    async def hydrate_message_files(self, message: Message) -> Message:
        """Hydrate file parts in a message with presigned URLs.

        For files stored with s3:// URIs, always regenerates fresh presigned URLs.
        For other URIs, only regenerates if the presigned URL has expired.

        Args:
            message: Message to hydrate

        Returns:
            Message with file parts updated with fresh presigned URLs
        """
        if not message.parts:
            return message

        storage = FileStorageService()
        updated_parts = []

        for part in message.parts:
            # Part is a RootModel union, get the actual part via .root
            actual_part = part.root if hasattr(part, "root") else part

            if not isinstance(actual_part, FilePart):
                updated_parts.append(part)
                continue
            if not isinstance(actual_part.file, FileWithUri):
                updated_parts.append(part)
                continue

            uri = actual_part.file.uri

            # Check if URI is an S3 URL (s3://bucket/key/...)
            if uri.startswith("s3://"):
                # Always regenerate presigned URLs for S3 URIs
                try:
                    # Parse s3://bucket/key... to extract key part
                    parts_list = uri.split("/", 3)  # Split to get at most 4 parts
                    if len(parts_list) >= 4:
                        # Format: s3://bucket/key/file -> reconstruct as bucket/key/file
                        bucket = parts_list[2]
                        key = "/".join(parts_list[3:])

                        # Generate fresh presigned URL
                        presigned_url = await storage.generate_presigned_get_url(key)
                        actual_part.file.uri = presigned_url
                        logger.debug(f"Regenerated presigned URL for S3 file: s3://{bucket}/{key}")
                    else:
                        logger.warning(f"Invalid S3 URI format: {uri}")
                except Exception as e:
                    logger.warning(f"Failed to regenerate presigned URL for S3 URI {uri}: {e}")

            updated_parts.append(part)

        # Create a new message with updated parts
        message.parts = updated_parts
        return message

    async def hydrate_messages_files(self, messages: list[Message]) -> list[Message]:
        """Hydrate file parts in multiple messages with presigned URLs.

        Args:
            messages: List of messages to hydrate

        Returns:
            List of messages with file parts updated with presigned URLs
        """
        return [await self.hydrate_message_files(msg) for msg in messages]
