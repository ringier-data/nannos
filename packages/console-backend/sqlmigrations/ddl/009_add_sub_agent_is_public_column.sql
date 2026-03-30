-- rambler up
-- Add is_public column to sub_agents table
-- When true, sub-agent is accessible to all users without explicit group permissions
ALTER TABLE sub_agents
ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT FALSE;
-- Index for looking up public sub-agents
CREATE INDEX idx_sub_agents_public ON sub_agents(id)
WHERE is_public = TRUE
    AND deleted_at IS NULL;
-- rambler down
DROP INDEX IF EXISTS idx_sub_agents_public;
ALTER TABLE sub_agents DROP COLUMN IF EXISTS is_public;
