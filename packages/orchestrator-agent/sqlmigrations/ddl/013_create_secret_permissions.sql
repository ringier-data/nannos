-- rambler up
-- Junction table for group-based secret access control
-- Users gain access to secrets through their group memberships
-- permissions array: 'read' = can view/use secret, 'write' = can edit and assign groups
CREATE TABLE secret_permissions (
    id SERIAL PRIMARY KEY,
    secret_id INTEGER NOT NULL REFERENCES secrets(id) ON DELETE CASCADE,
    user_group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    permissions TEXT [] NOT NULL DEFAULT ARRAY ['read']::TEXT [],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (secret_id, user_group_id),
    CHECK (
        permissions <@ ARRAY ['read', 'write']::TEXT []
        AND array_length(permissions, 1) > 0
    )
);

-- Index for permission lookups
CREATE INDEX idx_secret_permissions_lookup ON secret_permissions(secret_id, user_group_id);

-- Index for looking up all secrets accessible by a group
CREATE INDEX idx_secret_permissions_group ON secret_permissions(user_group_id);

-- Index for permission-based queries
CREATE INDEX idx_secret_permissions_permissions ON secret_permissions USING GIN(permissions);

-- rambler down

DROP INDEX IF EXISTS idx_secret_permissions_permissions;
DROP INDEX IF EXISTS idx_secret_permissions_group;
DROP INDEX IF EXISTS idx_secret_permissions_lookup;
DROP TABLE IF EXISTS secret_permissions;
