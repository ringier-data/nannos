"""Per-user entitlement version: the cache-validation stamp the orchestrator keys on.

The orchestrator memoizes capability discovery (MCP tools + sub-agents) and the registry
user lookup per user. Instead of console-backend *pushing* invalidations to every
orchestrator replica whenever an entitlement changes (and having to remember to do so at
every mutation site), the orchestrator *pulls* this version once per turn and folds it
into its cache key. A changed version makes the stale entry unreachable on every replica
at once; a forgotten mutation site cannot leave the cache stale, because the version is
derived from the rows themselves, not from the code paths that write them.

The version is an opaque digest over the rows in this database that decide which tools
and sub-agents the orchestrator serves a user — deliberately only those, so that writes
that do not change an entitlement (a login re-upsert, a timezone change) do not evict the
user's cache:

* ``users`` — system role, status, admin flag, and ``entitlements_touched_at`` (bumped
  for entitlements held outside this DB, see below);
* ``user_settings`` — the tool whitelist (``mcp_tools``) and HITL bypass rules, by value;
* the user's group memberships (which groups, how many, when last added);
* the default agents of those groups (count + last change);
* the sub-agents shared with those groups (``sub_agent_permissions``: count + last change),
  so an unshare/re-share is seen even though activation rows survive it;
* the catalogs shared with those groups (``catalog_permissions``) and the catalogs the user
  owns — they gate the catalog-search tool through the cached user record;
* the user's sub-agent activations (count + last change), and — for the activated agents
  as well as the system-owned public agents every user gets implicitly — the
  ``default_version``, its ``version_hash`` (content digest, so an edit to the live
  version's tools/prompt counts) and ``updated_at``, so a newly approved version is
  picked up too.

Group → MCP-server access lives in the gateway, not here; the console bumps
``users.entitlements_touched_at`` for a group's members when it grants or revokes it
(``touch_group_member_entitlements``), which moves the version the same way. That column
is cache bookkeeping, not business data, and is deliberately written outside the audited
repository pattern (the admin action that triggers it is audited on its own).

Gateway-side catalogue changes made outside the console are not visible here and remain
bounded by the orchestrator's cache TTL, which is that TTL's only remaining job.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# One round trip, all indexed lookups by user id / group id. ``ug`` is the user's group set,
# reused by every group-scoped term.
_VERSION_QUERY = text("""
    WITH ug AS (
        SELECT m.user_group_id, m.created_at
          FROM user_group_members m
         WHERE m.user_id = :user_id
    )
    SELECT
        u.role,
        u.status::text                          AS status,
        u.is_administrator,
        u.entitlements_touched_at::text         AS touched_at,
        coalesce((SELECT coalesce(us.mcp_tools::text, '[]') || '|' || coalesce(us.tool_bypass_rules::text, '{}')
                    FROM user_settings us
                   WHERE us.user_id = u.id), '[]|{}')
                                                AS settings_entitlements,
        (SELECT count(*) || ':' || coalesce(max(created_at)::text, '') || ':'
                || coalesce(string_agg(user_group_id::text, ',' ORDER BY user_group_id), '')
           FROM ug)                             AS memberships,
        (SELECT count(*) || ':' || coalesce(max(d.created_at)::text, '')
           FROM user_group_default_agents d
           JOIN ug ON ug.user_group_id = d.user_group_id)
                                                AS group_defaults,
        (SELECT count(*) || ':' || coalesce(max(p.created_at)::text, '')
           FROM sub_agent_permissions p
           JOIN ug ON ug.user_group_id = p.user_group_id)
                                                AS shared_agents,
        (SELECT count(*) || ':' || coalesce(max(cp.created_at)::text, '')
           FROM catalog_permissions cp
           JOIN ug ON ug.user_group_id = cp.user_group_id)
                                                AS shared_catalogs,
        (SELECT count(*) || ':' || coalesce(max(c.created_at)::text, '')
           FROM catalogs c
          WHERE c.owner_user_id = u.id)         AS owned_catalogs,
        (SELECT count(*) || ':' || coalesce(max(a.created_at)::text, '')
           FROM user_sub_agent_activations a
          WHERE a.user_id = u.id)               AS activations,
        (SELECT coalesce(max(sa.updated_at)::text, '') || ':'
                || coalesce(string_agg(sa.id || '@' || coalesce(sa.default_version::text, '')
                                       || '#' || coalesce(cv.version_hash, ''), ','
                                       ORDER BY sa.id), '')
           FROM sub_agents sa
           LEFT JOIN sub_agent_config_versions cv
                  ON cv.sub_agent_id = sa.id AND cv.version = sa.default_version
          WHERE sa.deleted_at IS NULL
            AND (EXISTS (SELECT 1 FROM user_sub_agent_activations a
                          WHERE a.user_id = u.id AND a.sub_agent_id = sa.id)
                 OR (sa.owner_user_id = 'system' AND sa.is_public = TRUE)))
                                                AS served_agents
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
