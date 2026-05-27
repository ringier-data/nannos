-- rambler up
-- Add 'tool_risk_score' to audit_entity_type enum
ALTER TYPE audit_entity_type
ADD VALUE IF NOT EXISTS 'tool_risk_score';
-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
