"""Service for managing secrets in AWS SSM Parameter Store."""

import logging
import os
from datetime import datetime, timezone

from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..authorization import check_action_allowed, check_capability
from ..models.secret import Secret, SecretCreate, SecretType

logger = logging.getLogger(__name__)


class SecretsService:
    """Manages secrets stored in AWS SSM Parameter Store with database metadata."""

    def __init__(self, secrets_repository=None):
        """Initialize the secrets service with aiobotocore session.

        Args:
            secrets_repository: Optional secrets repository instance.
                If None, must be set via set_repository() before use.
        """
        self.session = get_session()
        self.region_name = os.environ.get("AWS_REGION", "eu-central-1")
        self.ssm_vault_prefix = os.environ.get("SSM_VAULT_PREFIX", "/alloy/infrastructure-agents/vault")
        self.kms_key_id = os.environ.get("KMS_VAULT_KEY_ID", "alias/dev-alloy-sensitive-data-kms-key")
        self._repo = secrets_repository

    def set_repository(self, secrets_repository):
        """Set the secrets repository (dependency injection)."""
        self._repo = secrets_repository

    @property
    def repo(self):
        """Get the secrets repository, raising error if not set."""
        if self._repo is None:
            raise RuntimeError("SecretsRepository not injected. Call set_repository() during initialization.")
        return self._repo

    def _generate_ssm_parameter_name(self) -> str:
        """Generate unique SSM parameter name using vault prefix and UUID."""
        return os.path.join(self.ssm_vault_prefix, str(uuid7()))

    async def check_user_access(
        self,
        db: AsyncSession,
        secret_id: int,
        user_id: str,
        action: str,
        is_admin: bool = False,
        admin_mode: bool = False,
    ) -> bool:
        """Check if user has access to a secret.

        Authorization model: User has access if:
        1. User owns the secret (owner always has access)
        2. Secret has been granted to user's groups with GROUP_ROLE_CAPABILITIES intersection:
           - Resource has the required permission (read/write)
           - User's group role allows the action (check_action_allowed)
        3. Admin with admin-mode can access all secrets (bypasses intersection)

        Args:
            db: Database session
            secret_id: Secret ID
            user_id: User ID requesting access
            action: Action to check ('read' or 'write')
            is_admin: Whether user is a system administrator
            admin_mode: Whether admin mode is enabled

        Returns:
            True if user has access to the secret
        """
        if is_admin and admin_mode:
            return True
        # Get secret metadata
        query = text("""
            SELECT owner_user_id FROM secrets
            WHERE id = :secret_id AND deleted_at IS NULL
        """)
        result = await db.execute(query, {"secret_id": secret_id})
        row = result.fetchone()

        if not row:
            return False

        secret_owner_id = row[0]

        # Level 1: Owner access - always allowed
        if secret_owner_id == user_id:
            return True

        # Get user's role for capability checks
        user_query = text("""
            SELECT role FROM users WHERE id = :user_id
        """)
        user_result = await db.execute(user_query, {"user_id": user_id})
        user_row = user_result.fetchone()

        if not user_row:
            return False

        user_role = user_row[0]

        # Level 3: System-wide admin access - requires admin_mode
        admin_capability = f"{action}.admin"
        if check_capability(user_role, "secrets", admin_capability) and admin_mode:
            return True

        # Level 2: Group-based access with GROUP_ROLE_CAPABILITIES intersection
        # Check if user has system-level capability for the action
        if check_capability(user_role, "secrets", action):
            # Query for groups where secret has been granted AND user is a member
            query = text("""
                SELECT sp.permissions, ugm.group_role
                FROM secret_permissions sp
                JOIN user_group_members ugm ON sp.user_group_id = ugm.user_group_id
                WHERE sp.secret_id = :secret_id AND ugm.user_id = :user_id
            """)
            result = await db.execute(query, {"secret_id": secret_id, "user_id": user_id})

            for row in result.fetchall():
                resource_permissions = row[0]  # PostgreSQL array: what the group can do on this secret
                user_group_role = row[1]  # User's role in this group

                # Check if the resource has the required permission
                if action not in resource_permissions:
                    continue

                # Check if user's group role allows this action (intersection)
                if check_action_allowed(user_group_role, "secrets", action):
                    return True

        return False

    async def create_secret(
        self,
        db: AsyncSession,
        user_id: str,
        data: SecretCreate,
    ) -> Secret:
        """Create a new secret.

        Stores the actual secret value in SSM Parameter Store as SecureString
        and stores metadata (including SSM parameter name) in the database.

        Args:
            db: Database session
            user_id: Owner user ID
            data: Secret creation data

        Returns:
            Created secret (without the actual secret value)

        Raises:
            ValueError: If secret with same name already exists for this user
        """
        now = datetime.now(timezone.utc)

        # Check if secret with same name exists for this user
        check_query = text("""
            SELECT id FROM secrets
            WHERE owner_user_id = :user_id AND name = :name AND deleted_at IS NULL
        """)
        result = await db.execute(check_query, {"user_id": user_id, "name": data.name})
        if result.scalar_one_or_none():
            raise ValueError(f"Secret with name '{data.name}' already exists")

        # Generate SSM parameter name
        ssm_parameter_name = self._generate_ssm_parameter_name()

        # Store secret in SSM Parameter Store as SecureString
        try:
            async with self.session.create_client("ssm", region_name=self.region_name) as ssm_client:
                await ssm_client.put_parameter(  # type: ignore
                    Name=ssm_parameter_name,
                    Description=f"Secret for user {user_id}: {data.name}",
                    Value=data.secret_value,
                    Type="SecureString",
                    KeyId=self.kms_key_id,
                    Overwrite=False,
                    Tags=[
                        {"Key": "owner_user_id", "Value": user_id},
                        {"Key": "secret_type", "Value": data.secret_type.value},
                        {"Key": "managed_by", "Value": "playground-backend"},
                    ],
                )
            logger.info(f"Created SSM parameter {ssm_parameter_name} for user {user_id}")
        except ClientError as e:
            logger.error(f"Failed to create SSM parameter: {e}")
            raise ValueError(f"Failed to store secret in SSM Parameter Store: {e}")

        # Store metadata in database with automatic audit
        secret_id = await self.repo.create(
            db=db,
            actor_sub=user_id,
            fields={
                "owner_user_id": user_id,
                "name": data.name,
                "description": data.description,
                "secret_type": data.secret_type.value,
                "ssm_parameter_name": ssm_parameter_name,
                "created_at": now,
                "updated_at": now,
            },
            returning="id",
        )
        await db.commit()

        # Fetch and return the created secret
        return await self.get_secret(db, secret_id, user_id, is_admin=False, admin_mode=False)  # type: ignore

    async def get_secret(
        self,
        db: AsyncSession,
        secret_id: int,
        user_id: str,
        is_admin: bool = False,
        admin_mode: bool = False,
    ) -> Secret | None:
        """Get secret metadata by ID.

        Only returns metadata - does not fetch the actual secret value from SSM.
        Access control: owner, system admin, or group admin with access.

        Args:
            db: Database session
            secret_id: Secret ID
            user_id: Requesting user ID
            is_admin: Whether user is a system administrator
            admin_mode: Whether admin mode is enabled

        Returns:
            Secret metadata or None if not found/not authorized
        """
        # Check access first
        has_access = await self.check_user_access(db, secret_id, user_id, "read", is_admin, admin_mode)
        if not has_access:
            return None
        query = text("""
            SELECT id, owner_user_id, name, description, secret_type,
                   ssm_parameter_name, created_at, updated_at, deleted_at
            FROM secrets
            WHERE id = :secret_id AND deleted_at IS NULL
        """)
        result = await db.execute(query, {"secret_id": secret_id})
        row = result.fetchone()

        if not row:
            return None

        return Secret(
            id=row[0],
            owner_user_id=row[1],
            name=row[2],
            description=row[3],
            secret_type=SecretType(row[4]),
            ssm_parameter_name=row[5],
            created_at=row[6],
            updated_at=row[7],
            deleted_at=row[8],
        )

    async def list_user_secrets(
        self,
        db: AsyncSession,
        user_id: str,
        secret_type: SecretType | None = None,
    ) -> list[Secret]:
        """List all secrets accessible to a user.

        Returns secrets that are either:
        1. Owned by the user
        2. Shared with user's groups via permissions (with read or write access)

        Args:
            db: Database session
            user_id: User ID
            secret_type: Optional filter by secret type

        Returns:
            List of secret metadata (without actual secret values)
        """
        # TODO: system scope capabilities and group role checks should be enforced here as well
        # Query for secrets owned by user OR accessible via group permissions
        # Use DISTINCT to avoid duplicates if user is in multiple groups with access
        type_filter = "AND s.secret_type = :secret_type" if secret_type else ""
        query = text(f"""
            SELECT DISTINCT s.id, s.owner_user_id, s.name, s.description, s.secret_type,
                   s.ssm_parameter_name, s.created_at, s.updated_at, s.deleted_at
            FROM secrets s
            LEFT JOIN secret_permissions sp ON s.id = sp.secret_id
            LEFT JOIN user_group_members ugm ON sp.user_group_id = ugm.user_group_id AND ugm.user_id = :user_id
            WHERE s.deleted_at IS NULL 
              AND (s.owner_user_id = :user_id OR ugm.user_id IS NOT NULL)
              {type_filter}
            ORDER BY s.created_at DESC
        """)

        params = {"user_id": user_id}
        if secret_type:
            params["secret_type"] = secret_type.value

        result = await db.execute(query, params)

        rows = result.fetchall()
        return [
            Secret(
                id=row[0],
                owner_user_id=row[1],
                name=row[2],
                description=row[3],
                secret_type=SecretType(row[4]),
                ssm_parameter_name=row[5],
                created_at=row[6],
                updated_at=row[7],
                deleted_at=row[8],
            )
            for row in rows
        ]

    async def delete_secret(
        self,
        db: AsyncSession,
        secret_id: int,
        user_id: str,
        is_admin: bool = False,
        admin_mode: bool = False,
    ) -> bool:
        """Delete a secret (soft delete in DB, hard delete in SSM).

        Access control: only owner, system admin, or group admin with access can delete.

        Args:
            db: Database session
            secret_id: Secret ID
            user_id: Requesting user ID
            is_admin: Whether user is a system administrator
            admin_mode: Whether admin mode is enabled

        Returns:
            True if deleted, False if not found/not authorized

        Raises:
            ValueError: If secret is still referenced by sub-agents
            PermissionError: If user doesn't have access to delete
        """
        # Check access first
        has_access = await self.check_user_access(db, secret_id, user_id, "write", is_admin, admin_mode)
        if not has_access:
            raise PermissionError("You don't have permission to delete this secret")
        # Check if secret is referenced by any sub-agents
        check_query = text("""
            SELECT COUNT(*) FROM sub_agent_config_versions
            WHERE foundry_client_secret_ref = :secret_id AND deleted_at IS NULL
        """)
        result = await db.execute(check_query, {"secret_id": secret_id})
        count = result.scalar() or 0
        if count > 0:
            raise ValueError(f"Cannot delete secret: still referenced by {count} sub-agent(s)")

        # Get secret metadata (already access-controlled)
        secret = await self.get_secret(db, secret_id, user_id, is_admin, admin_mode)
        if not secret:
            return False

        # Delete from SSM Parameter Store
        try:
            async with self.session.create_client("ssm", region_name=self.region_name) as ssm_client:
                await ssm_client.delete_parameter(Name=secret.ssm_parameter_name)  # type: ignore
            logger.info(f"Deleted SSM parameter {secret.ssm_parameter_name}")
        except ClientError as e:
            logger.warning(f"Failed to delete SSM parameter {secret.ssm_parameter_name}: {e}")
            # Continue with soft delete even if SSM deletion fails

        # Soft delete in database with automatic audit
        await self.repo.delete(db=db, actor_sub=user_id, entity_id=secret_id, soft=True)
        await db.commit()

        return True

    async def get_secret_value(
        self,
        db: AsyncSession,
        secret_id: int,
        user_id: str,
        is_admin: bool = False,
        admin_mode: bool = False,
    ) -> str:
        """Fetch the actual secret value from SSM Parameter Store.

        This is used by the orchestrator to retrieve secrets at runtime.
        Enforces access control before retrieving the secret value.

        Args:
            db: Database session
            secret_id: Secret ID
            user_id: User ID requesting the secret
            is_admin: Whether user is a system administrator
            admin_mode: Whether admin mode is enabled

        Returns:
            Decrypted secret value

        Raises:
            ValueError: If parameter not found or access denied
            PermissionError: If user doesn't have access
        """
        # Get secret metadata with access control
        secret = await self.get_secret(db, secret_id, user_id, is_admin, admin_mode)
        if not secret:
            raise PermissionError("You don't have permission to access this secret")

        ssm_parameter_name = secret.ssm_parameter_name
        try:
            async with self.session.create_client("ssm", region_name=self.region_name) as ssm_client:
                response = await ssm_client.get_parameter(Name=ssm_parameter_name, WithDecryption=True)  # type: ignore
            return response["Parameter"]["Value"]
        except ClientError as e:
            logger.error(f"Failed to get SSM parameter {ssm_parameter_name}: {e}")
            raise ValueError(f"Failed to retrieve secret from SSM Parameter Store: {e}")

    async def get_permissions(self, db: AsyncSession, secret_id: int) -> list[dict]:
        """Get all group permissions for a secret.

        Returns list of dicts with:
        - user_group_id: Group ID
        - user_group_name: Group name
        - permissions: List of permission types ["read"] or ["write"] or ["read", "write"]

        Args:
            db: Database session
            secret_id: Secret ID

        Returns:
            List of permission dictionaries
        """
        query = text("""
            SELECT 
                sp.user_group_id,
                ug.name as user_group_name,
                sp.permissions
            FROM secret_permissions sp
            JOIN user_groups ug ON sp.user_group_id = ug.id
            WHERE sp.secret_id = :secret_id
            ORDER BY ug.name
        """)
        result = await db.execute(query, {"secret_id": secret_id})
        rows = result.fetchall()

        return [
            {
                "user_group_id": row[0],
                "user_group_name": row[1],
                "permissions": row[2] if isinstance(row[2], list) else [row[2]],
            }
            for row in rows
        ]

    async def update_permissions(
        self,
        db: AsyncSession,
        secret_id: int,
        group_permissions: list[dict],
        user_id: str,
        is_admin: bool = False,
    ) -> bool:
        """Update group permissions for a secret.

        Args:
            db: Database session
            secret_id: Secret ID
            group_permissions: List of dicts with user_group_id and permissions
            user_id: User ID performing the update
            is_admin: Whether user is admin (for access control)

        Returns:
            True if successful

        Raises:
            PermissionError: If user doesn't have permission to update
        """
        # Check if secret exists and user has access
        has_access = await self.check_user_access(
            db=db,
            secret_id=secret_id,
            user_id=user_id,
            action="write",
            is_admin=is_admin,
            admin_mode=is_admin,  # Admin mode required for permission management
        )

        if not has_access:
            raise PermissionError("You don't have permission to update permissions for this secret")

        # Use repository for update with automatic audit logging
        await self.repo.update_permissions(
            db=db,
            actor_sub=user_id,
            secret_id=secret_id,
            group_permissions=group_permissions,
        )
        await db.commit()
        return True
