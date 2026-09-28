"""Whether a user can receive on a delivery channel (#192, ADR-0011 amendment 1).

A channel is ``(client_id, installation_id)``. The client that registered it resolves the
recipient by the user's sign-in *in that installation's workspace*, so the user can receive
there exactly when a brokered sign-in of theirs (``broker_bindings``) belongs to a
workspace whose published installations (``broker_workspaces``) include the channel's.
Neither table is written by anyone but the client and the broker, so the answer is derived
on every read and never stored.

The three answers are defined once, in ``reachability_sql``, and every reader (the job
view, the checks, the channel list) goes through it.
"""

from collections.abc import Sequence
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.delivery_channel import DeliveryReachability


def reachability_sql(user_id: str, client_id: str, installation_id: str) -> str:
    """A SQL expression that is the ``DeliveryReachability`` of one user on one channel.

    The arguments are SQL expressions (columns or bind parameters), not values.

    - ``reachable``: a binding of the user with the channel's client whose workspace lists
      the installation.
    - ``unreachable``: the user has a binding with that client, and the installation is
      listed in one of the client's workspaces, but none of the user's bindings is in it.
      That includes a user still served in that workspace from an old local sign-in while
      they have a brokered one elsewhere: accepted, because it lasts only until old
      sign-ins have drained (decided on #192).
    - ``unknown``: everything else. No binding with the client at all is what an old local
      sign-in looks like, and it looks the same as never having signed in, so the backend
      cannot refuse on it. An installation no workspace lists (an older client, or one
      whose publication failed) says nothing either. A NULL installation lands here too.
    """
    return f"""
        CASE
            WHEN EXISTS (
                SELECT 1 FROM broker_bindings rb
                JOIN broker_workspaces rw ON rw.client_id = rb.client_id AND rw.workspace_id = rb.workspace_id
                WHERE rb.user_id = {user_id} AND rb.client_id = {client_id}
                  AND {installation_id} = ANY(rw.installation_ids)
            ) THEN 'reachable'
            WHEN EXISTS (
                SELECT 1 FROM broker_bindings rb WHERE rb.user_id = {user_id} AND rb.client_id = {client_id}
            ) AND EXISTS (
                SELECT 1 FROM broker_workspaces rw
                WHERE rw.client_id = {client_id} AND {installation_id} = ANY(rw.installation_ids)
            ) THEN 'unreachable'
            ELSE 'unknown'
        END"""


#: Why a subscription is off because its delivery channel cannot reach its subscriber
#: (#192): a group default put a member on the owner's channel, which they have never
#: signed in from, or the channel's client reported it found no one to deliver to
#: (#191). Holds are found by this exact text (the release, the onboarding count), so the
#: wording is load-bearing: rows already held keep the old text if it changes. Defined
#: here, beside the SQL that reads it; the scheduler writes it.
UNREACHABLE_HOLD_REASON = (
    "Nannos can't reach you on this job's delivery channel. Message Nannos there once to "
    "activate it, and the job switches back on"
)


#: The same hold after a client reported no recipient (#191) for a subscriber Nannos cannot
#: judge (``unknown``: an old local sign-in, or an unlisted installation). No sign-in the
#: backend sees will release it, so it asks the subscriber to switch the job back on,
#: which ``unknown`` never refuses. Load-bearing like the above.
UNDELIVERED_HOLD_REASON = (
    "Nannos couldn't reach you on this job's delivery channel. Message Nannos there once, "
    "then switch the job back on"
)


def unreachable_subscriptions_sql(user_id: str) -> str:
    """A SQL expression counting the live subscriptions of *user_id* (a SQL expression)
    that cannot reach them: on a channel they are ``unreachable`` on, or held for it. Held
    covers a client's report on a channel the bindings still call reachable."""
    reasons = ", ".join("'" + r.replace("'", "''") + "'" for r in (UNREACHABLE_HOLD_REASON, UNDELIVERED_HOLD_REASON))
    return f"""(
        SELECT COUNT(*) FROM scheduled_job_subscriptions us
        JOIN scheduled_job_definitions ud ON ud.id = us.definition_id AND ud.deleted_at IS NULL
        JOIN delivery_channels uc ON uc.id = us.delivery_channel_id
        WHERE us.user_id = {user_id} AND us.deleted_at IS NULL
          AND ((NOT us.enabled AND us.paused_reason IN ({reasons}))
               OR {reachability_sql(user_id, "uc.client_id", "uc.installation_id")} = 'unreachable')
    )"""


class DeliveryReachabilityRepository:
    """Reads reachability for the scheduler's checks and the console."""

    async def for_channel(
        self, db: AsyncSession, channel_id: int, user_ids: Sequence[str]
    ) -> dict[str, DeliveryReachability]:
        """Each of *user_ids* on *channel_id*. Empty when the channel does not exist."""
        if not user_ids:
            return {}
        result = await db.execute(
            text(f"""
                SELECT u.user_id, {reachability_sql("u.user_id", "c.client_id", "c.installation_id")} AS reachability
                FROM delivery_channels c
                CROSS JOIN unnest(CAST(:user_ids AS text[])) AS u(user_id)
                WHERE c.id = :channel_id
            """),
            {"channel_id": channel_id, "user_ids": list(user_ids)},
        )
        return {row.user_id: cast(DeliveryReachability, row.reachability) for row in result}

    async def for_user(
        self, db: AsyncSession, user_id: str, channel_ids: Sequence[int]
    ) -> dict[int, DeliveryReachability]:
        """*user_id* on each of *channel_ids* that exists."""
        if not channel_ids:
            return {}
        result = await db.execute(
            text(f"""
                SELECT c.id, {reachability_sql(":user_id", "c.client_id", "c.installation_id")} AS reachability
                FROM delivery_channels c
                WHERE c.id = ANY(:channel_ids)
            """),
            {"user_id": user_id, "channel_ids": list(channel_ids)},
        )
        return {row.id: cast(DeliveryReachability, row.reachability) for row in result}

    async def channel_name(self, db: AsyncSession, channel_id: int) -> str | None:
        result = await db.execute(text("SELECT name FROM delivery_channels WHERE id = :id"), {"id": channel_id})
        return result.scalar_one_or_none()

    async def channel_ids(self, db: AsyncSession, client_id: str, installation_ids: Sequence[str]) -> set[int]:
        """The channels *client_id* registered for *installation_ids*."""
        result = await db.execute(
            text("SELECT id FROM delivery_channels WHERE client_id = :client_id AND installation_id = ANY(:ids)"),
            {"client_id": client_id, "ids": list(installation_ids)},
        )
        return set(result.scalars())

    async def users_held_on(
        self, db: AsyncSession, client_id: str, installation_ids: Sequence[str], paused_reason: str
    ) -> list[str]:
        """The users with a live subscription switched off for exactly *paused_reason* on a
        channel of *client_id* in one of *installation_ids*."""
        if not installation_ids:
            return []
        result = await db.execute(
            text("""
                SELECT DISTINCT s.user_id
                FROM scheduled_job_subscriptions s
                JOIN scheduled_job_definitions d ON d.id = s.definition_id AND d.deleted_at IS NULL
                JOIN delivery_channels c ON c.id = s.delivery_channel_id
                WHERE NOT s.enabled AND s.paused_reason = :reason AND s.deleted_at IS NULL
                  AND c.client_id = :client_id AND c.installation_id = ANY(:installation_ids)
                ORDER BY s.user_id
            """),
            {"client_id": client_id, "installation_ids": list(installation_ids), "reason": paused_reason},
        )
        return list(result.scalars())
