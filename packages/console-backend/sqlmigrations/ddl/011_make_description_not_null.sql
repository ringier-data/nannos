-- rambler up
-- Make description NOT NULL in sub_agent_config_versions
-- First update any existing NULL descriptions with a default value
UPDATE sub_agent_config_versions
SET description = 'No description provided'
WHERE description IS NULL;
-- Then add the NOT NULL constraint
ALTER TABLE sub_agent_config_versions
ALTER COLUMN description
SET NOT NULL;
-- rambler down
ALTER TABLE sub_agent_config_versions
ALTER COLUMN description DROP NOT NULL;
