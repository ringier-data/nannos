"""Scheduler token service — KMS envelope encryption for offline refresh tokens.

Encryption format (BYTEA column layout):
    [2 bytes big-endian: encrypted_dek_len]
    [encrypted_dek_len bytes: AWS-KMS-encrypted data encryption key]
    [12 bytes: AES-256-GCM nonce]
    [remaining bytes: ciphertext + 16-byte GCM auth tag]

Decryption:
    1. kms:Decrypt(encrypted_dek) → plaintext 32-byte DEK
    2. AES-256-GCM decrypt(ciphertext, nonce, dek) → refresh token
"""

import logging
import os
import struct
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# KMS key alias reused from SecretsService — already has GenerateDataKey/Decrypt IAM perms
_KMS_KEY_ID = os.environ.get("KMS_VAULT_KEY_ID", "alias/dev-nannos-sensitive-data-kms-key")
_TOKEN_ENDPOINT_SUFFIX = "/protocol/openid-connect/token"


def _get_kms_client():  # type: ignore[no-untyped-def]
    """Return an aiobotocore KMS client (lazy import to avoid heavy startup cost)."""
    import aiobotocore.session  # type: ignore[import]

    session = aiobotocore.session.get_session()
    return session.create_client("kms", region_name=os.environ.get("AWS_REGION", "eu-central-1"))


def _encrypt_token(plaintext_dek: bytes, refresh_token: str) -> bytes:
    """Encrypt *refresh_token* with *plaintext_dek* using AES-256-GCM.

    Returns the nonce + ciphertext blob (ciphertext includes the 16-byte GCM tag).
    """
    aesgcm = AESGCM(plaintext_dek)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, refresh_token.encode(), None)
    return nonce + ciphertext


def _decrypt_token(plaintext_dek: bytes, nonce_and_ciphertext: bytes) -> str:
    """Decrypt the nonce+ciphertext blob produced by _encrypt_token."""
    nonce = nonce_and_ciphertext[:12]
    ciphertext = nonce_and_ciphertext[12:]
    aesgcm = AESGCM(plaintext_dek)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


class SchedulerTokenService:
    """Manages Keycloak offline refresh tokens encrypted via AWS KMS envelope encryption."""

    def __init__(self, oidc_issuer: str, oidc_client_id: str, oidc_client_secret: str) -> None:
        self._issuer = oidc_issuer.rstrip("/")
        self._client_id = oidc_client_id
        self._client_secret = oidc_client_secret
        self._token_endpoint = self._issuer + _TOKEN_ENDPOINT_SUFFIX

    async def store_offline_token(
        self,
        db: AsyncSession,
        user_id: str,
        refresh_token: str,
        token_expiry: datetime | None = None,
    ) -> None:
        """Encrypt *refresh_token* with KMS envelope encryption and upsert into DB."""
        async with _get_kms_client() as kms:
            response = await kms.generate_data_key(KeyId=_KMS_KEY_ID, KeySpec="AES_256")
            plaintext_dek: bytes = response["Plaintext"]
            encrypted_dek: bytes = response["CiphertextBlob"]

        nonce_and_ciphertext = _encrypt_token(plaintext_dek, refresh_token)

        # Layout: [2-byte length][encrypted_dek][nonce+ciphertext]
        encrypted_dek_len = struct.pack(">H", len(encrypted_dek))
        blob = encrypted_dek_len + encrypted_dek + nonce_and_ciphertext

        now = datetime.now(timezone.utc)
        await db.execute(
            text("""
                INSERT INTO user_offline_tokens (user_id, encrypted_token, token_expiry, created_at, updated_at)
                VALUES (:user_id, :blob, :token_expiry, :now, :now)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    encrypted_token = EXCLUDED.encrypted_token,
                    token_expiry    = EXCLUDED.token_expiry,
                    updated_at      = EXCLUDED.updated_at
            """),
            {"user_id": user_id, "blob": blob, "token_expiry": token_expiry, "now": now},
        )
        await db.commit()
        logger.info("Stored offline token for user %s", user_id)

    async def revoke_token(self, db: AsyncSession, user_id: str) -> None:
        """Delete the stored offline token for a user."""
        await db.execute(
            text("DELETE FROM user_offline_tokens WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        await db.commit()
        logger.info("Revoked offline token for user %s", user_id)

    async def has_consent(self, db: AsyncSession, user_id: str) -> bool:
        """Return True if an offline token has been stored for *user_id*."""
        blob = await self._load_encrypted_blob(db, user_id)
        return blob is not None

    async def _load_encrypted_blob(self, db: AsyncSession, user_id: str) -> bytes | None:
        result = await db.execute(
            text("SELECT encrypted_token FROM user_offline_tokens WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        row = result.mappings().first()
        return bytes(row["encrypted_token"]) if row else None

    async def _decrypt_blob(self, blob: bytes) -> str:
        """Decrypt the stored KMS-envelope blob and return the plaintext refresh token."""
        # Parse layout
        (encrypted_dek_len,) = struct.unpack(">H", blob[:2])
        encrypted_dek = blob[2 : 2 + encrypted_dek_len]
        nonce_and_ciphertext = blob[2 + encrypted_dek_len :]

        async with _get_kms_client() as kms:
            response = await kms.decrypt(CiphertextBlob=encrypted_dek, KeyId=_KMS_KEY_ID)
            plaintext_dek: bytes = response["Plaintext"]

        return _decrypt_token(plaintext_dek, nonce_and_ciphertext)

    async def get_access_token(self, db: AsyncSession, user_id: str) -> str:
        """Return a fresh Keycloak access token by refreshing the stored offline token.

        Raises ValueError if no token is stored for the user.
        Raises httpx.HTTPStatusError on Keycloak errors.
        """
        blob = await self._load_encrypted_blob(db, user_id)
        if blob is None:
            raise ValueError(f"No offline token stored for user {user_id}. User must grant scheduler consent first.")

        refresh_token = await self._decrypt_blob(blob)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                    "scope": "openid profile email offline_access",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        logger.debug("Refreshed access token for user %s", user_id)
        access_token = data["access_token"]
        # Exchange for agent-runner audience so agent-runner's own OAuth client
        # (OIDC_CLIENT_ID=agent-runner) can perform downstream token exchanges
        # (e.g. agent-runner → voice-agent via SmartTokenInterceptor).
        token = await self.exchange_token(access_token, audience="agent-runner")
        return token

    async def exchange_token(self, access_token: str, audience: str) -> str:
        """RFC 8693 token exchange — obtain a token for *audience* on behalf of the user.

        Used so agent-runner can call MCP tools on behalf of the scheduled job owner.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_endpoint,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "subject_token": access_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "audience": audience,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        return data["access_token"]
