-- rambler up
-- Junction table for group-based sub-agent access control
-- Users gain access to sub-agents through their group memberships
-- permissions array: 'read' = can activate, 'write' = can edit and assign groups
CREATE TABLE sub_agent_permissions (
    id SERIAL PRIMARY KEY,
    sub_agent_id INTEGER NOT NULL REFERENCES sub_agents(id) ON DELETE CASCADE,
    user_group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    permissions TEXT [] NOT NULL DEFAULT ARRAY ['read']::TEXT [],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sub_agent_id, user_group_id),
    CHECK (
        permissions <@ ARRAY ['read', 'write']::TEXT []
        AND array_length(permissions, 1) > 0
    )
);
-- Index for permission lookups
CREATE INDEX idx_permissions_lookup ON sub_agent_permissions(sub_agent_id, user_group_id);
-- Index for looking up all sub-agents accessible by a group
CREATE INDEX idx_permissions_group ON sub_agent_permissions(user_group_id);
-- Index for permission-based queries
CREATE INDEX idx_sub_agent_permissions_permissions ON sub_agent_permissions USING GIN(permissions);
-- rambler down
DROP INDEX IF EXISTS idx_sub_agent_permissions_permissions;
DROP INDEX IF EXISTS idx_permissions_group;
DROP INDEX IF EXISTS idx_permissions_lookup;
DROP TABLE IF EXISTS sub_agent_permissions;
