-- rambler up
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'voice_session';

-- rambler down
-- NOTE: PostgreSQL does not support removing enum values; this migration is intentionally irreversible.
