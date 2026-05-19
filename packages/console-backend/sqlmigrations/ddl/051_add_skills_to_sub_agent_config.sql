-- rambler up
ALTER TABLE sub_agent_config_versions
ADD COLUMN skills JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN sandbox_enabled BOOLEAN NOT NULL DEFAULT FALSE;
-- rambler down
ALTER TABLE sub_agent_config_versions DROP COLUMN skills,
    DROP COLUMN sandbox_enabled;
