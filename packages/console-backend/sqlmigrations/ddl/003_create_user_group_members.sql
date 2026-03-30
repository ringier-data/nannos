-- rambler up
-- Junction table for user-group relationships
CREATE TABLE user_group_members (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    group_role TEXT NOT NULL DEFAULT 'read' CHECK (group_role IN ('read', 'write', 'manager')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, user_group_id)
);
-- Index for looking up user's groups
CREATE INDEX idx_user_group_members_user ON user_group_members(user_id);
-- Index for looking up group's members
CREATE INDEX idx_user_group_members_group ON user_group_members(user_group_id);
-- rambler down
DROP INDEX IF EXISTS idx_user_group_members_group;
DROP INDEX IF EXISTS idx_user_group_members_user;
DROP TABLE IF EXISTS user_group_members;
