-- rambler up
-- User groups table: manages teams and resource access
-- Groups define which resources (sub-agents) users can access
-- Individual permissions are determined by the user's system role and group role
CREATE TABLE user_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Index for active groups
CREATE INDEX idx_user_groups_active ON user_groups(id)
WHERE deleted_at IS NULL;
-- rambler down
DROP INDEX IF EXISTS idx_user_groups_active;
DROP TABLE IF EXISTS user_groups;
