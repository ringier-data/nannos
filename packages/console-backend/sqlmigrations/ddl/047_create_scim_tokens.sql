-- rambler up

-- Add SCIM external ID columns for identity provider correlation
ALTER TABLE users ADD COLUMN scim_external_id TEXT;
ALTER TABLE users ADD COLUMN scim_user_name TEXT;
CREATE UNIQUE INDEX idx_users_scim_external_id ON users(scim_external_id) WHERE scim_external_id IS NOT NULL;

ALTER TABLE user_groups ADD COLUMN scim_external_id TEXT;
CREATE UNIQUE INDEX idx_user_groups_scim_external_id ON user_groups(scim_external_id) WHERE scim_external_id IS NOT NULL;


-- Add scim_token to audit entity types
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'scim_token';

-- Add 'revoke' to audit actions
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'revoke';

-- Create SCIM bearer tokens table for provisioning authentication
CREATE TABLE scim_tokens (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    token TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one active (non-revoked) token per name
CREATE UNIQUE INDEX idx_scim_tokens_name_active ON scim_tokens(name) WHERE revoked_at IS NULL;

-- Fast lookup by token value for auth validation
CREATE INDEX idx_scim_tokens_token_active ON scim_tokens(token) WHERE revoked_at IS NULL;

-- rambler down
ALTER TABLE users DROP COLUMN IF EXISTS scim_user_name;

DROP TABLE IF EXISTS scim_tokens;

DROP INDEX IF EXISTS idx_user_groups_scim_external_id;
ALTER TABLE user_groups DROP COLUMN IF EXISTS scim_external_id;

DROP INDEX IF EXISTS idx_users_scim_external_id;
ALTER TABLE users DROP COLUMN IF EXISTS scim_external_id;
