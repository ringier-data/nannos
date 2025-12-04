"""User service for managing users in DynamoDB."""

import logging
import os

from datetime import datetime, timezone

import boto3
import httpx

from aiodynamo.client import Client
from aiodynamo.credentials import Credentials, Key, StaticCredentials
from aiodynamo.errors import ItemNotFound
from aiodynamo.expressions import F
from aiodynamo.http.httpx import HTTPX
from aiodynamo.models import ReturnValues
from config import config
from models.user import User


logger = logging.getLogger(__name__)


class UserService:
    """Manages users in DynamoDB."""

    def __init__(self) -> None:
        """Initialize the user service."""
        dynamodb_config = config.dynamodb
        self.table_name = dynamodb_config.users_table

        # Initialize aiodynamo client with appropriate credentials
        # Use auto credentials in ECS, static credentials locally
        try:
            _ = os.environ['ECS_CONTAINER_METADATA_URI']
            credentials = Credentials.auto()
            logger.info('Using auto credentials (ECS environment)')
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
            user_id: The user's ID (sub from Oidc)

        Returns:
            The user or None if not found
        """
        try:
            item = await self.table.get_item(key={'id': user_id})
            return User(
                id=item['id'],
                sub=item['sub'],
                email=item['email'],
                first_name=item['first_name'],
                last_name=item['last_name'],
                company_name=item.get('company_name'),
                is_administrator=item.get('is_administrator', False),
                agent_urls=item.get('agent_urls', []),
                tool_names=item.get('tool_names', []),
                language=item.get('language', 'en'),
                created_at=datetime.fromisoformat(item['created_at']),
                updated_at=datetime.fromisoformat(item['updated_at']),
            )
        except ItemNotFound:
            logger.debug(f'User not found: {user_id}')
            return None
        except Exception as e:
            logger.error(f'Failed to get user: {e}')
            return None

    async def upsert_user(
        self,
        sub: str,
        email: str,
        first_name: str,
        last_name: str,
        company_name: str | None = None,
    ) -> User:
        """Create or update a user using atomic DynamoDB update_item.

        This uses SET for OIDC-sourced fields that should always update,
        and SET ... IF_NOT_EXISTS for user-editable fields that should
        only be initialized on first creation.

        Args:
            sub: The user's sub from OIDC
            email: The user's email
            first_name: The user's first name
            last_name: The user's last name
            company_name: The user's company name (optional)

        Returns:
            The created or updated user
        """
        now = datetime.now(tz=timezone.utc)
        now_iso = now.isoformat()

        # Build update expression:
        # - OIDC-sourced fields: always SET (overwrite with latest from IdP)
        # - User-editable fields: SET IF_NOT_EXISTS (preserve existing values)
        # - Timestamps: created_at only on creation, updated_at always
        update_expr = (
            # OIDC-sourced fields - always update from IdP
            F('sub').set(sub)
            & F('email').set(email)
            & F('first_name').set(first_name)
            & F('last_name').set(last_name)
            & F('updated_at').set(now_iso)
            # User-editable fields - only set if not exists (preserve on re-login)
            & F('is_administrator').set_if_not_exists(False)
            & F('language').set_if_not_exists('en')
            & F('agent_urls').set_if_not_exists([])
            & F('tool_names').set_if_not_exists([])
            & F('created_at').set_if_not_exists(now_iso)
        )

        # Handle company_name - always update from IdP (may be None)
        if company_name is not None:
            update_expr = update_expr & F('company_name').set(company_name)

        try:
            result = await self.table.update_item(
                key={'id': sub},
                update_expression=update_expr,
                return_values=ReturnValues.all_new,
            )
            logger.info(f'Upserted user: {sub}')

            if result is None:
                raise RuntimeError(f'update_item returned None for user {sub}')

            # Parse the returned item into a User model
            return User(
                id=result['id'],
                sub=result['sub'],
                email=result['email'],
                first_name=result['first_name'],
                last_name=result['last_name'],
                company_name=result.get('company_name'),
                is_administrator=result.get('is_administrator', False),
                agent_urls=result.get('agent_urls', []),
                tool_names=result.get('tool_names', []),
                language=result.get('language', 'en'),
                created_at=datetime.fromisoformat(result['created_at']),
                updated_at=datetime.fromisoformat(result['updated_at']),
            )
        except Exception as e:
            logger.error(f'Failed to upsert user: {e}')
            raise
