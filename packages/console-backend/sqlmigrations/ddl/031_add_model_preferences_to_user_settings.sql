-- rambler up
-- Add model and thinking preferences to user_settings
ALTER TABLE user_settings
    ADD COLUMN preferred_model TEXT,
    ADD COLUMN enable_thinking BOOLEAN,
    ADD COLUMN thinking_level TEXT;

-- rambler down
ALTER TABLE user_settings
    DROP COLUMN IF EXISTS preferred_model,
    DROP COLUMN IF EXISTS enable_thinking,
    DROP COLUMN IF EXISTS thinking_level;
