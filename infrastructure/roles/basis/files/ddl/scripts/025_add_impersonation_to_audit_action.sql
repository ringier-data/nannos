-- rambler up
-- Add 'impersonation_start' and 'impersonation_end' to audit_action enum
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'impersonation_start';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'impersonation_end';

-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
