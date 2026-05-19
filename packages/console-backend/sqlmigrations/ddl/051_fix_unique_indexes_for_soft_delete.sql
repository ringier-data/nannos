-- rambler up
-- Fix unique indexes to exclude soft-deleted rows.
-- Previously, soft-deleted users blocked recreation of users with the same
-- email or scim_external_id because the indexes were unconditional.

-- Email uniqueness: only enforce among non-deleted users
DROP INDEX IF EXISTS idx_users_email_unique;
CREATE UNIQUE INDEX idx_users_email_unique
    ON users(LOWER(email))
    WHERE deleted_at IS NULL;

-- SCIM external ID uniqueness: only enforce among non-deleted users
DROP INDEX IF EXISTS idx_users_scim_external_id;
CREATE UNIQUE INDEX idx_users_scim_external_id
    ON users(scim_external_id)
    WHERE scim_external_id IS NOT NULL AND deleted_at IS NULL;

-- rambler down
DROP INDEX IF EXISTS idx_users_email_unique;
CREATE UNIQUE INDEX idx_users_email_unique ON users(LOWER(email));

DROP INDEX IF EXISTS idx_users_scim_external_id;
CREATE UNIQUE INDEX idx_users_scim_external_id ON users(scim_external_id) WHERE scim_external_id IS NOT NULL;
