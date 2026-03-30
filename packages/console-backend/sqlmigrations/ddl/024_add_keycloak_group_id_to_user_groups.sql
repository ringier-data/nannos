-- rambler up
-- Add Keycloak group ID for one-way sync (Backend → Keycloak)
-- This enables MCP Gateway to trust group claims from JWT tokens
ALTER TABLE user_groups 
ADD COLUMN keycloak_group_id TEXT UNIQUE;

-- Index for efficient Keycloak ID lookups during sync operations
CREATE INDEX idx_user_groups_keycloak_id ON user_groups(keycloak_group_id);

-- rambler down
DROP INDEX IF EXISTS idx_user_groups_keycloak_id;
ALTER TABLE user_groups DROP COLUMN IF EXISTS keycloak_group_id;
