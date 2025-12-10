"""API router for secrets management."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..authorization import check_capability
from ..db.session import DbSession
from ..dependencies import require_auth
from ..models.secret import (
    Secret,
    SecretCreate,
    SecretGroupPermissionResponse,
    SecretListResponse,
    SecretPermissionsUpdate,
)
from ..models.user import User
from ..services.secrets_service import SecretsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])


def get_secrets_service(request: Request) -> SecretsService:
    """Get secrets service from app state."""
    return request.app.state.secrets_service


@router.post("", response_model=Secret, status_code=status.HTTP_201_CREATED)
async def create_secret(
    secret_data: SecretCreate,
    db: DbSession,
    current_user: User = Depends(require_auth),
    request: Request = None,  # type: ignore[assignment]
):
    """Create a new secret.

    Requires 'secrets:write' capability.

    The secret value will be stored securely in AWS SSM Parameter Store.
    Only metadata is stored in the database.
    """
    secrets_service = get_secrets_service(request)

    # Check write capability
    if not check_capability(current_user.role, "secrets", "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to create secrets",
        )

    try:
        secret = await secrets_service.create_secret(
            db=db,
            user_id=current_user.id,
            data=secret_data,
        )
        return secret
    except Exception as e:
        logger.error(f"Failed to create secret: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create secret: {str(e)}",
        )


@router.get("", response_model=SecretListResponse)
async def list_secrets(
    db: DbSession,
    current_user: User = Depends(require_auth),
    request: Request = None,  # type: ignore[assignment]
):
    """List all secrets accessible to the current user.

    Returns secrets owned by the user.

    Requires 'secrets:read' capability.
    """
    secrets_service = get_secrets_service(request)

    # Check read capability
    if not check_capability(current_user.role, "secrets", "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to list secrets",
        )

    try:
        secrets = await secrets_service.list_user_secrets(
            db=db,
            user_id=current_user.id,
        )
        return SecretListResponse(items=secrets, total=len(secrets))
    except Exception as e:
        logger.error(f"Failed to list secrets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list secrets: {str(e)}",
        )


@router.get("/{secret_id}", response_model=Secret)
async def get_secret(
    secret_id: int,
    db: DbSession,
    current_user: User = Depends(require_auth),
    request: Request = None,  # type: ignore[assignment]
):
    """Get a secret by ID (metadata only, not the actual secret value).

    Requires 'secrets:read' capability and ownership/access to the secret.
    """
    secrets_service = get_secrets_service(request)

    # Check read capability
    if not check_capability(current_user.role, "secrets", "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to read secrets",
        )

    # Check access to specific secret
    has_access = await secrets_service.check_user_access(
        db=db,
        secret_id=secret_id,
        user_id=current_user.id,
        action="read",
        is_admin=current_user.is_administrator,
        admin_mode=False,  # Not admin operations, just reading own secrets
    )

    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Secret not found or access denied",
        )

    try:
        secret = await secrets_service.get_secret(
            db=db,
            secret_id=secret_id,
            user_id=current_user.id,
            is_admin=current_user.is_administrator,
            admin_mode=False,
        )
        if not secret:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Secret not found",
            )
        return secret
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get secret {secret_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get secret: {str(e)}",
        )


@router.delete("/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    secret_id: int,
    db: DbSession,
    current_user: User = Depends(require_auth),
    request: Request = None,  # type: ignore[assignment]
):
    """Delete a secret (soft delete).

    Requires 'secrets:write' capability and ownership of the secret.

    The secret is soft-deleted in the database and the SSM parameter is deleted.
    """
    secrets_service = get_secrets_service(request)

    # Check write capability
    if not check_capability(current_user.role, "secrets", "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to delete secrets",
        )

    # Check access to specific secret (must be owner for delete)
    has_access = await secrets_service.check_user_access(
        db=db,
        secret_id=secret_id,
        user_id=current_user.id,
        action="write",
        is_admin=current_user.is_administrator,
        admin_mode=False,
    )

    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Secret not found or access denied",
        )

    try:
        await secrets_service.delete_secret(
            db=db,
            secret_id=secret_id,
            user_id=current_user.id,
            is_admin=current_user.is_administrator,
            admin_mode=False,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete secret {secret_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete secret: {str(e)}",
        )


@router.get("/{secret_id}/permissions", response_model=list[SecretGroupPermissionResponse])
async def get_secret_permissions(
    secret_id: int,
    db: DbSession,
    current_user: User = Depends(require_auth),
    request: Request = None,  # type: ignore[assignment]
) -> list[SecretGroupPermissionResponse]:
    """Get group permissions for a secret.

    Owner can view permissions.
    Requires 'secrets:read' capability and ownership of the secret.
    """
    secrets_service = get_secrets_service(request)

    # Check read capability
    if not check_capability(current_user.role, "secrets", "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to read secret permissions",
        )

    # Check access to specific secret
    has_access = await secrets_service.check_user_access(
        db=db,
        secret_id=secret_id,
        user_id=current_user.id,
        action="read",
        is_admin=current_user.is_administrator,
        admin_mode=False,
    )

    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Secret not found or access denied",
        )

    try:
        permissions = await secrets_service.get_permissions(db, secret_id)
        return [SecretGroupPermissionResponse(**perm) for perm in permissions]
    except Exception as e:
        logger.error(f"Failed to get permissions for secret {secret_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get permissions",
        )


@router.put("/{secret_id}/permissions", status_code=status.HTTP_204_NO_CONTENT)
async def update_secret_permissions(
    secret_id: int,
    data: SecretPermissionsUpdate,
    db: DbSession,
    current_user: User = Depends(require_auth),
    request: Request = None,  # type: ignore[assignment]
) -> None:
    """Update group permissions for a secret.

    Only the owner can update permissions.
    Requires 'secrets:write' capability and ownership of the secret.
    """
    secrets_service = get_secrets_service(request)

    # Check write capability
    if not check_capability(current_user.role, "secrets", "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to update secret permissions",
        )

    try:
        # Convert Pydantic models to dicts for service layer
        group_permissions = [
            {"user_group_id": gp.user_group_id, "permissions": gp.permissions} for gp in data.group_permissions
        ]

        success = await secrets_service.update_permissions(
            db, secret_id, group_permissions, current_user.id, is_admin=current_user.is_administrator
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Secret not found",
            )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update permissions for secret {secret_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update permissions",
        )
