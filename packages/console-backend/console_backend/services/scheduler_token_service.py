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

#: `has_consent` as a SQL expression over a `users u` row, for queries that list people
#: and report whether each is scheduler-ready in the same round trip.
HAS_OFFLINE_TOKEN_SQL = "EXISTS (SELECT 1 FROM user_offline_tokens uot WHERE uot.user_id = u.id)"


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


class NoOfflineTokenError(ValueError):
    """The user has no offline token in the vault: they never signed in through a flow
    that stores one. A ``ValueError`` so existing callers keep working."""


class OfflineTokenExpiredError(NoOfflineTokenError):
    """The vaulted offline token is dead: Keycloak refused it (``invalid_grant``: 30 days
    idle, revoked, or its offline session ended), now or on an earlier attempt.

    A ``NoOfflineTokenError`` because the remedy is the same, a sign-in, and every caller
    that handles a missing token must handle this one the same way. A caller that acts on
    it persists it with ``mark_expired(db, user_id, error.stored_at)``, so the token counts
    as absent from then on.

    *stored_at* is the refused row's ``updated_at``: it names the token that was refused,
    so a sign-in that stored a fresh one in the meantime is not marked with it.
    """

    def __init__(self, message: str, stored_at: datetime) -> None:
        super().__init__(message)
        self.stored_at = stored_at


def _is_invalid_grant(exc: httpx.HTTPStatusError) -> bool:
    """Keycloak's answer for a refresh token that will never work again."""
    if exc.response.status_code != 400:
        return False
    try:
        return exc.response.json().get("error") == "invalid_grant"
    except ValueError:
        return False


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
                    expired_at      = NULL,
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

    async def mark_expired(self, db: AsyncSession, user_id: str, stored_at: datetime) -> None:
        """Record that Keycloak refused *user_id*'s vaulted token (see
        ``OfflineTokenExpiredError``). Kept, not deleted, so an expired sign-in stays
        distinguishable from none; the next ``store_offline_token`` clears it.

        Only the token stored at *stored_at* is marked: one the user stored by signing in
        between the refusal and this call is live, and must stay so."""
        await db.execute(
            text("""
                UPDATE user_offline_tokens SET expired_at = :now
                WHERE user_id = :user_id AND updated_at = :stored_at AND expired_at IS NULL
            """),
            {"user_id": user_id, "stored_at": stored_at, "now": datetime.now(timezone.utc)},
        )
        await db.commit()
        logger.info("Marked the offline token of user %s expired", user_id)

    async def has_consent(self, db: AsyncSession, user_id: str) -> bool:
        """Return True if *user_id* has a live offline token: stored and not expired."""
        return bool(await self.users_with_consent(db, [user_id]))

    async def users_with_consent(self, db: AsyncSession, user_ids: list[str]) -> set[str]:
        """The subset of *user_ids* that have a live offline token. One query for a list."""
        if not user_ids:
            return set()
        result = await db.execute(
            text("SELECT user_id FROM user_offline_tokens WHERE user_id = ANY(:ids) AND expired_at IS NULL"),
            {"ids": list(user_ids)},
        )
        return {row[0] for row in result.all()}

    async def _load_encrypted_blob(self, db: AsyncSession, user_id: str) -> tuple[bytes, datetime]:
        """The user's live vaulted token and when it was stored. Raises ``NoOfflineTokenError``
        when none is stored, ``OfflineTokenExpiredError`` when the stored one was already refused."""
        result = await db.execute(
            text("SELECT encrypted_token, expired_at, updated_at FROM user_offline_tokens WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        row = result.mappings().first()
        if row is None:
            raise NoOfflineTokenError(f"No offline token stored for user {user_id}. User must grant consent first.")
        if row["expired_at"] is not None:
            raise OfflineTokenExpiredError(
                f"The offline token of user {user_id} has expired; they must sign in again.", row["updated_at"]
            )
        return bytes(row["encrypted_token"]), row["updated_at"]

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

    async def _refresh_access_token(self, db: AsyncSession, user_id: str) -> str:
        """Refresh the user's stored offline token into a fresh Keycloak access token.

        Raises NoOfflineTokenError if no token is stored for the user, and its subclass
        OfflineTokenExpiredError if Keycloak refuses it (``invalid_grant``), which the
        caller persists with ``mark_expired``. Raises httpx.HTTPStatusError on any other
        Keycloak error: that says nothing about the token.
        """
        blob, stored_at = await self._load_encrypted_blob(db, user_id)

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
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if _is_invalid_grant(exc):
                    raise OfflineTokenExpiredError(
                        f"Keycloak refused the offline token of user {user_id} (invalid_grant); "
                        "they must sign in again.",
                        stored_at,
                    ) from exc
                raise
            data = resp.json()

        logger.debug("Refreshed access token for user %s", user_id)
        return data["access_token"]

    async def mint_user_access_token(self, db: AsyncSession, user_id: str) -> str:
        """Return a fresh, *un-exchanged* nannos access token for the user from their
        vaulted offline token — the mint step of the cross-IdP federated-exchange
        (ADR-0002 Amendment 2). Un-exchanged on purpose: the embedded widget presents
        it on the socket, and OrchestratorAuth performs the audience exchange itself.

        Raises NoOfflineTokenError if the user has no stored offline token (not enrolled).
        """
        return await self._refresh_access_token(db, user_id)

    async def get_exchanged_token(self, db: AsyncSession, user_id: str, audience: str) -> str:
        """Refresh the user's offline token and exchange it for *audience* (RFC 8693).

        Lets a service act on behalf of the user against a specific audience (e.g.
        the MCP gateway) using only the user_id — no live user session required.
        """
        return (await self.get_exchanged_token_response(db, user_id, audience))["access_token"]

    async def get_exchanged_token_response(self, db: AsyncSession, user_id: str, audience: str) -> dict:
        """Like ``get_exchanged_token``, but the whole Keycloak token response.

        The token broker needs ``expires_in`` so its clients can cache what they are given.

        Raises NoOfflineTokenError if no token is stored for the user.
        Raises httpx.HTTPStatusError on Keycloak errors.
        """
        access_token = await self._refresh_access_token(db, user_id)
        return await self.exchange_token_response(access_token, audience=audience)

    async def get_access_token(self, db: AsyncSession, user_id: str) -> str:
        """Return a fresh access token exchanged for the agent-runner audience.

        Used by the scheduler so agent-runner's own OAuth client can perform
        downstream token exchanges (e.g. agent-runner → voice-agent).
        """
        return await self.get_exchanged_token(db, user_id, audience="agent-runner")

    async def exchange_token(self, access_token: str, audience: str) -> str:
        """RFC 8693 token exchange — obtain a token for *audience* on behalf of the user.

        Used so agent-runner can call MCP tools on behalf of the scheduled job owner.
        """
        return (await self.exchange_token_response(access_token, audience))["access_token"]

    async def exchange_token_response(self, access_token: str, audience: str) -> dict:
        """RFC 8693 token exchange, returning the whole Keycloak token response."""
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
            return resp.json()
