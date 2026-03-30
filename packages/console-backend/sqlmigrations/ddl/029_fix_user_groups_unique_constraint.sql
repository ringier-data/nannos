-- rambler up
-- Fix user_groups name uniqueness to exclude soft-deleted groups
-- This allows reusing names from deleted groups

-- Drop existing unique constraint
ALTER TABLE user_groups DROP CONSTRAINT IF EXISTS user_groups_name_key;

-- Create partial unique index that only applies to non-deleted groups
CREATE UNIQUE INDEX user_groups_name_active_key ON user_groups (name)
WHERE deleted_at IS NULL;

-- rambler down
-- Restore original behavior (though this won't work if deleted groups exist with duplicate names)
DROP INDEX IF EXISTS user_groups_name_active_key;
ALTER TABLE user_groups ADD CONSTRAINT user_groups_name_key UNIQUE (name);
