-- rambler up
-- Add thinking mode configuration to sub-agent config versions

-- Create enum type for thinking levels
CREATE TYPE thinking_level AS ENUM ('minimal', 'low', 'medium', 'high');

-- Add thinking configuration columns
ALTER TABLE sub_agent_config_versions
ADD COLUMN enable_thinking BOOLEAN,
ADD COLUMN thinking_level thinking_level;

-- Add comment to document the feature
COMMENT ON COLUMN sub_agent_config_versions.enable_thinking IS 'Enable extended thinking mode for Claude Sonnet and Gemini models';
COMMENT ON COLUMN sub_agent_config_versions.thinking_level IS 'Thinking depth level (minimal/low/medium/high) mapped to token budgets';

-- rambler down
-- Remove thinking configuration columns
ALTER TABLE sub_agent_config_versions
DROP COLUMN IF EXISTS thinking_level,
DROP COLUMN IF EXISTS enable_thinking;

-- Drop the enum type
DROP TYPE IF EXISTS thinking_level;
