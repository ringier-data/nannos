"""User service for managing users in DynamoDB."""

import logging
import os

import boto3
import httpx
from aiodynamo.client import Client
from aiodynamo.credentials import Credentials, Key, StaticCredentials
from aiodynamo.errors import ItemNotFound
from aiodynamo.http.httpx import HTTPX
from pydantic import BaseModel

from ..models.config import DynamoDBConfig

logger = logging.getLogger(__name__)


class User(BaseModel):
    """User model for DynamoDB storage."""

    id: str  # Primary key (sub from OIDC)
    agent_urls: list[str] = []  # List of registered agent URLs
    tool_names: list[str] = []  # List of registered tool names


class RegistryService:
    """ReadOnly service for fetching tools and sub-agents from the registry."""

    def __init__(self) -> None:
        """Initialize the user service."""
        dynamodb_config = DynamoDBConfig()
        self.table_name = dynamodb_config.users_table

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

        self.client = Client(
            HTTPX(httpx.AsyncClient()),
            credentials,
            dynamodb_config.region,
        )
        self.table = self.client.table(self.table_name)

    async def get_user(self, user_id: str) -> User | None:
        """Retrieve a user by ID.

        Args:
            user_id: The user's ID (sub from OIDC)

        Returns:
            The user or None if not found
        """
        try:
            item = await self.table.get_item(key={"id": user_id})
            return User(
                id=item["id"],
                agent_urls=item.get("agent_urls", []),
                tool_names=item.get("tool_names", []),
            )
        except ItemNotFound:
            logger.debug(f"User not found: {user_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to get user: {e}")
            return None
