"""KMS envelope encryption for Google OAuth refresh tokens used in catalog connections.

Reuses the same KMS key and encryption format as SchedulerTokenService.
Stores encrypted tokens in the catalog_connections table.
"""

from __future__ import annotations

import logging
import os
import struct
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.oauth2.credentials import Credentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _get_kms_client() -> Any:
    """Return an aiobotocore KMS client (lazy import)."""
    import aiobotocore.session

    session = aiobotocore.session.get_session()
    return session.create_client("kms", region_name=os.environ.get("AWS_REGION", "eu-central-1"))


class CatalogTokenService:
    """Manages Google OAuth tokens for catalog connections with KMS envelope encryption."""

    def __init__(self, kms_key_id: str, client_id: str, client_secret: str) -> None:
        self._kms_key_id = kms_key_id
        self._client_id = client_id
        self._client_secret = client_secret

    async def store_connection(
        self,
        db: AsyncSession,
        catalog_id: str,
        connector_user_id: str,
        refresh_token: str,
        scopes: list[str],
        token_expiry: datetime | None = None,
    ) -> None:
        """Encrypt and store a Google OAuth refresh token for a catalog connection."""
        blob = await self._encrypt(refresh_token)
        now = datetime.now(timezone.utc)

        await db.execute(
            text("""
                INSERT INTO catalog_connections (catalog_id, connector_user_id, encrypted_token,
                    token_expiry, scopes, connected_at, status, updated_at)
                VALUES (:catalog_id, :user_id, :blob, :token_expiry, :scopes, :now, 'active', :now)
                ON CONFLICT (catalog_id)
                DO UPDATE SET
                    connector_user_id = EXCLUDED.connector_user_id,
                    encrypted_token = EXCLUDED.encrypted_token,
                    token_expiry = EXCLUDED.token_expiry,
                    scopes = EXCLUDED.scopes,
                    connected_at = EXCLUDED.connected_at,
                    status = 'active',
                    updated_at = EXCLUDED.updated_at
            """),
            {
                "catalog_id": catalog_id,
                "user_id": connector_user_id,
                "blob": blob,
                "token_expiry": token_expiry,
                "scopes": scopes,
                "now": now,
            },
        )

    async def get_credentials(self, db: AsyncSession, catalog_id: str) -> Credentials | None:
        """Load and decrypt stored credentials for a catalog, returning Google Credentials."""
        result = await db.execute(
            text("""
                SELECT encrypted_token, scopes, status
                FROM catalog_connections
                WHERE catalog_id = :catalog_id
            """),
            {"catalog_id": catalog_id},
        )
        row = result.mappings().first()
        if not row or row["status"] != "active":
            return None

        refresh_token = await self._decrypt(bytes(row["encrypted_token"]))

        return Credentials(
            token=None,  # will be refreshed on first use
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=row["scopes"],
        )

    async def revoke_connection(self, db: AsyncSession, catalog_id: str) -> None:
        """Revoke a catalog connection."""
        await db.execute(
            text("""
                UPDATE catalog_connections SET status = 'revoked', updated_at = :now
                WHERE catalog_id = :catalog_id
            """),
            {"catalog_id": catalog_id, "now": datetime.now(timezone.utc)},
        )

    async def _encrypt(self, plaintext: str) -> bytes:
        """Encrypt using KMS envelope encryption (same format as SchedulerTokenService)."""
        async with _get_kms_client() as kms:
            response = await kms.generate_data_key(KeyId=self._kms_key_id, KeySpec="AES_256")
            plaintext_dek: bytes = response["Plaintext"]
            encrypted_dek: bytes = response["CiphertextBlob"]

        aesgcm = AESGCM(plaintext_dek)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)

        encrypted_dek_len = struct.pack(">H", len(encrypted_dek))
        return encrypted_dek_len + encrypted_dek + nonce + ciphertext

    async def _decrypt(self, blob: bytes) -> str:
        """Decrypt a KMS envelope-encrypted blob."""
        (encrypted_dek_len,) = struct.unpack(">H", blob[:2])
        encrypted_dek = blob[2 : 2 + encrypted_dek_len]
        nonce_and_ciphertext = blob[2 + encrypted_dek_len :]

        async with _get_kms_client() as kms:
            response = await kms.decrypt(CiphertextBlob=encrypted_dek, KeyId=self._kms_key_id)
            plaintext_dek: bytes = response["Plaintext"]

        nonce = nonce_and_ciphertext[:12]
        ciphertext = nonce_and_ciphertext[12:]
        aesgcm = AESGCM(plaintext_dek)
        return aesgcm.decrypt(nonce, ciphertext, None).decode()
