"""Socket session service for managing Socket.IO sessions in DynamoDB."""

import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
import httpx
from aiodynamo.client import Client
from aiodynamo.credentials import Credentials, Key, StaticCredentials
from aiodynamo.errors import ItemNotFound
from aiodynamo.expressions import F, UpdateExpression, Value
from aiodynamo.http.httpx import HTTPX

from ..config import config
from ..models.socket_session import SocketSession

logger = logging.getLogger(__name__)


class SocketSessionService:
    """Manages Socket.IO sessions in DynamoDB.

    Stores minimal Socket.IO session data in the same DynamoDB table as HTTP sessions,
    using a 'socket:' prefix to distinguish socket sessions. The actual httpx and A2A
    clients are cached in-memory per server instance for efficiency and cleaned up on
    disconnect.
    """

    def __init__(self) -> None:
        """Initialize the socket session service."""
        dynamodb_config = config.dynamodb
        self.table_name = dynamodb_config.sessions_table
        # Socket sessions TTL is for cleanup only - connection pool manages lifecycle
        # 48 hours is a safety buffer for orphaned records from crashes/ungraceful disconnects
        self.session_ttl_seconds = 172800  # 48 hours

        # Initialize aiodynamo client with appropriate credentials
        # Use auto credentials in ECS, static credentials locally
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

        logger.info(f"SocketSessionService initialized with table: {self.table_name}")

    async def create_session(
        self,
        socket_id: str,
        user_id: str,
        http_session_id: str,
    ) -> SocketSession:
        """Create a new socket session.

        Args:
            socket_id: The Socket.IO session ID (sid)
            user_id: The user's ID (sub from Oidc)
            http_session_id: The HTTP session ID for linking back to user session

        Returns:
            The created SocketSession
        """
        created_at = datetime.now(tz=timezone.utc)
        ttl = int((created_at + timedelta(seconds=self.session_ttl_seconds)).timestamp())

        # Use 'socket:' prefix to distinguish from HTTP sessions
        session_key = f"socket:{socket_id}"

        socket_session = SocketSession(
            socket_id=session_key,
            user_id=user_id,
            http_session_id=http_session_id,
            created_at=created_at,
            ttl=ttl,
        )

        try:
            await self.table.put_item(
                item={
                    "session_id": socket_session.socket_id,
                    "user_id": socket_session.user_id,
                    "http_session_id": socket_session.http_session_id,
                    "agent_url": socket_session.agent_url,
                    "custom_headers": socket_session.custom_headers,
                    "is_initialized": socket_session.is_initialized,
                    "created_at": socket_session.created_at.isoformat(),
                    "ttl": socket_session.ttl,
                }
            )
            logger.info(f"Created socket session for user: {user_id}, sid: {socket_id}")
            return socket_session
        except Exception as e:
            logger.error(f"Failed to create socket session: {e}")
            raise

    async def get_session(self, socket_id: str) -> SocketSession | None:
        """Retrieve a socket session by Socket.IO session ID.

        Args:
            socket_id: The Socket.IO session ID (sid)

        Returns:
            The SocketSession or None if not found
        """
        session_key = f"socket:{socket_id}"
        try:
            item = await self.table.get_item(key={"session_id": session_key})
            return SocketSession(
                socket_id=item["session_id"],
                user_id=item["user_id"],
                http_session_id=item["http_session_id"],
                agent_url=item.get("agent_url"),
                custom_headers=item.get("custom_headers", {}),
                is_initialized=item.get("is_initialized", False),
                created_at=datetime.fromisoformat(item["created_at"]),
                ttl=item["ttl"],
            )
        except ItemNotFound:
            logger.debug(f"Socket session not found: {socket_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to get socket session: {e}")
            return None

    async def initialize_client(
        self,
        socket_id: str,
        agent_url: str,
        custom_headers: dict[str, str],
    ) -> None:
        """Mark socket session as initialized and store agent URL.

        Args:
            socket_id: The Socket.IO session ID (sid)
            agent_url: The agent URL for cache lookup
            custom_headers: Custom HTTP headers
        """
        session_key = f"socket:{socket_id}"

        try:
            await self.table.update_item(
                key={"session_id": session_key},
                update_expression=UpdateExpression(
                    set_updates=[
                        (F("agent_url"), Value(agent_url)),
                        (F("custom_headers"), Value(custom_headers)),
                        (F("is_initialized"), Value(True)),
                    ]
                ),
            )
            logger.info(f"Initialized client for socket session: {socket_id}")
        except Exception as e:
            logger.error(f"Failed to initialize socket session: {e}")
            raise

    async def destroy_session(self, socket_id: str) -> None:
        """Delete a socket session.

        Args:
            socket_id: The Socket.IO session ID (sid)
        """
        session_key = f"socket:{socket_id}"
        try:
            await self.table.delete_item(key={"session_id": session_key})
            logger.info(f"Destroyed socket session: {socket_id}")
        except Exception as e:
            logger.error(f"Failed to destroy socket session: {e}")
