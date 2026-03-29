-- rambler up

-- =============================================================================
-- Add avatar binary storage to bot_installations
-- Stores avatar image data directly in PostgreSQL as bytea, replacing the
-- previous avatar_url text column approach.
-- =============================================================================
ALTER TABLE bot_installations
    ADD COLUMN IF NOT EXISTS avatar_data      BYTEA,
    ADD COLUMN IF NOT EXISTS avatar_mime_type  TEXT;
