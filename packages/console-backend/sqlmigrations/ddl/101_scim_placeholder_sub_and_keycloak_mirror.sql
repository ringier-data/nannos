-- rambler up

-- A SCIM-provisioned user has no Keycloak account until their first OIDC login, so their
-- `sub` is a placeholder. It used to be the row's own id, which meant the placeholder had to
-- be *inferred* (`sub = id`, narrowed by `scim_user_name IS NOT NULL`) — and that inference
-- could not be made sound: migration 001 keyed `users` by the OIDC sub itself, so rows from
-- that era legitimately have `sub = id`, and `scim_user_name` can be written onto any row by
-- a SCIM PUT/PATCH. A legacy user caught by both would silently stop being mirrored into
-- Keycloak. The placeholder now says what it is.

-- backfill:placeholder-subs
UPDATE users
SET sub = 'scim-pending:' || id
WHERE sub = id
  AND scim_user_name IS NOT NULL;

-- Keycloak is a mirror of the database, not the authority. While a user has no IdP account,
-- group membership changes cannot be mirrored and are skipped — this flag is what records
-- that Keycloak is behind for this user, so the push can be retried at their next login
-- instead of depending on catching the single moment their real subject arrives.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS keycloak_mirror_pending BOOLEAN NOT NULL DEFAULT FALSE;

-- Users whose memberships are diverged right now: provisioned over SCIM, never logged in,
-- and already a member of a group that exists in Keycloak. Without this they would stay
-- diverged until something touched their membership again.
UPDATE users u
SET keycloak_mirror_pending = TRUE
WHERE u.sub LIKE 'scim-pending:%'
  AND EXISTS (
      SELECT 1
      FROM user_group_members ugm
      JOIN user_groups ug ON ug.id = ugm.user_group_id
      WHERE ugm.user_id = u.id
        AND ug.deleted_at IS NULL
        AND ug.keycloak_group_id IS NOT NULL
  );

-- rambler down

ALTER TABLE users DROP COLUMN IF EXISTS keycloak_mirror_pending;

UPDATE users
SET sub = substring(sub from length('scim-pending:') + 1)
WHERE sub LIKE 'scim-pending:%';
