"""Per-user entitlement version: the cache-validation stamp the orchestrator keys on.

The orchestrator memoizes capability discovery (MCP tools + sub-agents) and the registry
user lookup per user. Instead of console-backend *pushing* invalidations to every
orchestrator replica whenever an entitlement changes (and having to remember to do so at
every mutation site), the orchestrator *pulls* this version once per turn and folds it
into its cache key. A changed version makes the stale entry unreachable on every replica
at once; a forgotten mutation site cannot leave the cache stale, because the version is
derived from the rows themselves, not from the code paths that write them.

The version is an opaque digest over everything in this database that decides which
tools and sub-agents a user is entitled to:

* the ``users`` row — system role, status, admin flag, ``updated_at``, and
  ``entitlements_touched_at`` (bumped for entitlements held outside this DB, see below);
* ``user_settings.updated_at`` — tool whitelist (``mcp_tools``), HITL bypass rules;
* the user's group memberships (which groups, how many, when last added);
* the default agents of those groups (count + last change);
* the user's sub-agent activations (count + last change), and the activated agents'
  ``default_version``, its ``version_hash`` (content digest, so an edit to the live
  version's tools/prompt counts) and ``updated_at``, so a newly approved version is
  picked up too.

Group → MCP-server access lives in the gateway, not here; the console bumps
``users.entitlements_touched_at`` for a group's members when it grants or revokes it
(``touch_group_member_entitlements``), which moves the version the same way.

Gateway-side catalogue changes made outside the console are not visible here and remain
bounded by the orchestrator's cache TTL, which is that TTL's only remaining job.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# One round trip, all indexed lookups by user id / group id.
_VERSION_QUERY = text("""
    SELECT
        u.role,
        u.status::text                          AS status,
        u.is_administrator,
        u.updated_at::text                      AS user_updated_at,
        u.entitlements_touched_at::text         AS touched_at,
        (SELECT us.updated_at::text
           FROM user_settings us
          WHERE us.user_id = u.id)              AS settings_updated_at,
        (SELECT count(*) || ':' || coalesce(max(m.created_at)::text, '') || ':'
                || coalesce(string_agg(m.user_group_id::text, ',' ORDER BY m.user_group_id), '')
           FROM user_group_members m
          WHERE m.user_id = u.id)               AS memberships,
        (SELECT count(*) || ':' || coalesce(max(d.created_at)::text, '')
           FROM user_group_default_agents d
           JOIN user_group_members m ON m.user_group_id = d.user_group_id
          WHERE m.user_id = u.id)               AS group_defaults,
        (SELECT count(*) || ':' || coalesce(max(a.created_at)::text, '')
           FROM user_sub_agent_activations a
          WHERE a.user_id = u.id)               AS activations,
        (SELECT coalesce(max(sa.updated_at)::text, '') || ':'
                || coalesce(string_agg(sa.id || '@' || coalesce(sa.default_version::text, '')
                                       || '#' || coalesce(cv.version_hash, ''), ','
                                       ORDER BY sa.id), '')
           FROM sub_agents sa
           JOIN user_sub_agent_activations a ON a.sub_agent_id = sa.id
           LEFT JOIN sub_agent_config_versions cv
                  ON cv.sub_agent_id = sa.id AND cv.version = sa.default_version
          WHERE a.user_id = u.id)               AS activated_agents
    FROM users u
    WHERE u.id = :user_id
""")

_TOUCH_GROUP_QUERY = text("""
    UPDATE users
       SET entitlements_touched_at = NOW()
     WHERE id IN (SELECT user_id FROM user_group_members WHERE user_group_id = :group_id)
""")


async def compute_entitlement_version(db: AsyncSession, user_id: str) -> str | None:
    """Return the user's current entitlement version, or None if the user does not exist.

    Opaque to callers: compare for equality only. Two calls return the same string as long
    as none of the underlying rows changed.
    """
    row = (await db.execute(_VERSION_QUERY, {"user_id": user_id})).mappings().first()
    if row is None:
        return None
    material = "|".join("" if v is None else str(v) for v in row.values())
    return hashlib.sha256(material.encode()).hexdigest()[:32]


async def touch_group_member_entitlements(db: AsyncSession, group_id: int) -> int:
    """Bump ``entitlements_touched_at`` for every member of ``group_id``.

    For entitlement changes this database cannot see on its own (group → MCP-server access
    on the gateway). Returns the number of users touched. Does not commit.
    """
    result = await db.execute(_TOUCH_GROUP_QUERY, {"group_id": group_id})
    return result.rowcount or 0
