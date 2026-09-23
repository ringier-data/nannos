"""Storage for brokered sign-ins: logins in flight (``broker_login_requests``) and which
users signed in through which client (``broker_client_users``).

Not an ``AuditedRepository``, on purpose: this is sign-in state, like ``sessions`` and
``user_offline_tokens``. A login row lives for minutes and holds only hashes of the state
and the one-time code; a link row is the fact that a person signed in through a client.
The sign-in's business effect — the user upsert and the vaulted offline token — is
recorded where it happens.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class BrokerLoginRequest:
    """A login a broker client started and the user has not finished yet."""

    state_hash: str
    client_id: str
    redirect_uri: str
    client_state: str | None


class BrokerLoginRequestRepository:
    async def purge_expired(self, db: AsyncSession, before: datetime) -> None:
        """Drop rows that expired before *before*. Opportunistic housekeeping, no timer."""
        await db.execute(text("DELETE FROM broker_login_requests WHERE expires_at < :before"), {"before": before})

    async def create_pending(
        self,
        db: AsyncSession,
        *,
        state_hash: str,
        client_id: str,
        redirect_uri: str,
        client_state: str | None,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        await db.execute(
            text("""
                INSERT INTO broker_login_requests
                    (state_hash, client_id, redirect_uri, client_state, created_at, expires_at)
                VALUES (:state_hash, :client_id, :redirect_uri, :client_state, :now, :expires_at)
            """),
            {
                "state_hash": state_hash,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "client_state": client_state,
                "now": now,
                "expires_at": expires_at,
            },
        )

    async def get_open(self, db: AsyncSession, state_hash: str, now: datetime) -> BrokerLoginRequest | None:
        """The login for this state, if it has not expired and no code was issued for it yet."""
        result = await db.execute(
            text("""
                SELECT state_hash, client_id, redirect_uri, client_state
                FROM broker_login_requests
                WHERE state_hash = :state_hash AND code_hash IS NULL AND expires_at > :now
            """),
            {"state_hash": state_hash, "now": now},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return BrokerLoginRequest(
            state_hash=row["state_hash"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            client_state=row["client_state"],
        )

    async def issue_code(
        self,
        db: AsyncSession,
        *,
        state_hash: str,
        code_hash: str,
        user_id: str,
        identity: dict[str, Any],
        now: datetime,
        code_expires_at: datetime,
    ) -> bool:
        """Attach the one-time code and who signed in. False if the login was already
        completed (a replayed callback) or has expired in the meantime."""
        result = await db.execute(
            text("""
                UPDATE broker_login_requests
                SET code_hash = :code_hash, user_id = :user_id, identity = CAST(:identity AS jsonb),
                    code_expires_at = :code_expires_at
                WHERE state_hash = :state_hash AND code_hash IS NULL AND expires_at > :now
                RETURNING state_hash
            """),
            {
                "state_hash": state_hash,
                "code_hash": code_hash,
                "user_id": user_id,
                "identity": json.dumps(identity),
                "now": now,
                "code_expires_at": code_expires_at,
            },
        )
        return result.first() is not None

    async def redeem(self, db: AsyncSession, *, code_hash: str, client_id: str, now: datetime) -> dict[str, Any] | None:
        """Consume the code in one statement, so two concurrent redeems cannot both win,
        and record that the user signed in through *client_id*.

        None when the code is unknown, expired, already used, or was issued to another
        client — deliberately indistinguishable to the caller.
        """
        result = await db.execute(
            text("""
                UPDATE broker_login_requests
                SET redeemed_at = :now
                WHERE code_hash = :code_hash AND client_id = :client_id
                  AND redeemed_at IS NULL AND code_expires_at > :now
                RETURNING identity, user_id
            """),
            {"code_hash": code_hash, "client_id": client_id, "now": now},
        )
        row = result.mappings().first()
        if row is None:
            return None
        await db.execute(
            text("""
                INSERT INTO broker_client_users (client_id, user_id, created_at)
                VALUES (:client_id, :user_id, :now)
                ON CONFLICT (client_id, user_id) DO NOTHING
            """),
            {"client_id": client_id, "user_id": row["user_id"], "now": now},
        )
        identity = row["identity"]
        return json.loads(identity) if isinstance(identity, str) else dict(identity)

    async def is_linked(self, db: AsyncSession, client_id: str, user_id: str) -> bool:
        """Whether *user_id* has signed in through *client_id* at least once."""
        result = await db.execute(
            text("SELECT 1 FROM broker_client_users WHERE client_id = :client_id AND user_id = :user_id"),
            {"client_id": client_id, "user_id": user_id},
        )
        return result.first() is not None
