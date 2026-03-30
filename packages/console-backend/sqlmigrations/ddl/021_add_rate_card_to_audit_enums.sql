-- rambler up
-- Add 'rate_card' to audit_entity_type enum
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'rate_card';

-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
