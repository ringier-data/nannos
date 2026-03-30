-- rambler up
-- Add 'secret' to audit_entity_type enum
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'secret';

-- Add new audit actions for sub-agent and secret operations
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'submit_for_approval';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'activate';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'deactivate';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'set_default';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'revert';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'permission_update';

-- rambler down
-- Note: PostgreSQL doesn't support removing enum values
-- If rollback is needed, a new enum type must be created and migrated
-- This is left empty as enum value removal requires recreating the enum
