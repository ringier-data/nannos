-- rambler up
-- Add submitted_by_user_id to track who submitted a version for approval
ALTER TABLE sub_agent_config_versions
ADD COLUMN submitted_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;
-- Create index for efficient lookups
CREATE INDEX idx_sub_agent_versions_submitter ON sub_agent_config_versions(submitted_by_user_id);

-- rambler down
DROP INDEX IF EXISTS idx_sub_agent_versions_submitter;
ALTER TABLE sub_agent_config_versions
DROP COLUMN IF EXISTS submitted_by_user_id;
