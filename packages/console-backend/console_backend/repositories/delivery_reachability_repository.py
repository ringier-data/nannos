"""Whether a user can receive on a delivery channel (#192, ADR-0011 amendments 1 and 2).

A channel is ``(client_id, installation_id)`` and belongs to one workspace of its client
(``delivery_channels.workspace_id``: a Slack team, a Google Chat project). The client
resolves a push's recipient by the user's sign-in in that workspace, so the user can
receive there exactly when a brokered sign-in of theirs (``broker_bindings``) is bound to
that workspace. Neither side is written by anyone but the client and the broker, so the
answer is derived on every read and never stored.

The three answers are defined once, in ``reachability_sql``, and every reader (the job
view, the checks, the channel list, the onboarding count) goes through it.
"""

from collections.abc import Sequence
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.delivery_channel import DeliveryReachability


def reachability_sql(user_id: str, client_id: str, workspace_id: str) -> str:
    """A SQL expression that is the ``DeliveryReachability`` of one user on one channel.

    The arguments are SQL expressions (columns or bind parameters), not values: the
    channel's ``client_id`` and ``workspace_id``. One probe of the user's bindings with
    that client (index ``idx_broker_bindings_user_client``):

    - ``reachable``: one of them is bound to the channel's workspace.
    - ``unreachable``: the channel's workspace is known, the user has bindings with the
      client, and none is in it. That includes a user still served in that workspace from
      an old local sign-in while they have a brokered one elsewhere: accepted, because it
      lasts only until old sign-ins have drained (decided on #192).
    - ``unknown``: no binding with the client at all, which is what an old local sign-in
      looks like and looks the same as never having signed in, or a channel whose client
      has not said which workspace it is in. The backend cannot refuse on either.
    """
    return f"""
        CASE WHEN {workspace_id} IS NULL THEN 'unknown' ELSE COALESCE((
            SELECT CASE WHEN bool_or(rb.workspace_id = {workspace_id}) THEN 'reachable' ELSE 'unreachable' END
            FROM broker_bindings rb
            WHERE rb.user_id = {user_id} AND rb.client_id = {client_id}
            HAVING count(*) > 0
        ), 'unknown') END"""


def unreachable_subscriptions_sql(user_id: str) -> str:
    """A SQL expression counting the live subscriptions of *user_id* (a SQL expression)
    that cannot reach them: on a channel they are ``unreachable`` on, or held for it
    (``unreachable`` or ``undelivered``). Held covers a client's report on a channel the
    bindings still call reachable, or cannot judge."""
    return f"""(
        SELECT COUNT(*) FROM scheduled_job_subscriptions us
        JOIN scheduled_job_definitions ud ON ud.id = us.definition_id AND ud.deleted_at IS NULL
        JOIN delivery_channels uc ON uc.id = us.delivery_channel_id
        WHERE us.user_id = {user_id} AND us.deleted_at IS NULL
          AND (us.pause_code IN ('unreachable', 'undelivered')
               OR {reachability_sql(user_id, "uc.client_id", "uc.workspace_id")} = 'unreachable')
    )"""


class DeliveryReachabilityRepository:
    """Reads reachability for the scheduler's checks and releases."""

    async def for_channel(
        self, db: AsyncSession, channel_id: int, user_ids: Sequence[str]
    ) -> dict[str, DeliveryReachability]:
        """Each of *user_ids* on *channel_id*. Empty when the channel does not exist."""
        if not user_ids:
            return {}
        result = await db.execute(
            text(f"""
                SELECT u.user_id, {reachability_sql("u.user_id", "c.client_id", "c.workspace_id")} AS reachability
                FROM delivery_channels c
                CROSS JOIN unnest(CAST(:user_ids AS text[])) AS u(user_id)
                WHERE c.id = :channel_id
            """),
            {"channel_id": channel_id, "user_ids": list(user_ids)},
        )
        return {row.user_id: cast(DeliveryReachability, row.reachability) for row in result}

    async def channel_name(self, db: AsyncSession, channel_id: int) -> str | None:
        result = await db.execute(text("SELECT name FROM delivery_channels WHERE id = :id"), {"id": channel_id})
        return result.scalar_one_or_none()

    async def channel_ids_in(self, db: AsyncSession, client_id: str, workspace_id: str) -> set[int]:
        """The channels *client_id* registered in *workspace_id*: what a sign-in bound
        there reaches."""
        result = await db.execute(
            text("SELECT id FROM delivery_channels WHERE client_id = :client_id AND workspace_id = :workspace_id"),
            {"client_id": client_id, "workspace_id": workspace_id},
        )
        return set(result.scalars())
