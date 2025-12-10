-- rambler up
-- User settings table for user-editable preferences
-- Created lazily via upsert when user updates their settings
CREATE TABLE user_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    language TEXT NOT NULL DEFAULT 'en',
    timezone TEXT NOT NULL DEFAULT 'Europe/Zurich',
    custom_prompt TEXT,
    mcp_tools JSONB DEFAULT '[]'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- rambler down
DROP TABLE IF EXISTS user_settings;
