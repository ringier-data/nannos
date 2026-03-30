-- rambler up
-- Enum types for audit log
CREATE TYPE audit_entity_type AS ENUM ('user', 'group', 'sub_agent', 'session');
CREATE TYPE audit_action AS ENUM (
    'create',
    'update',
    'delete',
    'approve',
    'reject',
    'assign',
    'unassign',
    'admin_mode_activated'
);
-- Audit logs table for tracking all changes
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    actor_sub TEXT NOT NULL,
    entity_type audit_entity_type NOT NULL,
    entity_id TEXT NOT NULL,
    action audit_action NOT NULL,
    changes JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Index for looking up audit logs by entity
CREATE INDEX idx_audit_entity ON audit_logs(entity_type, entity_id);
-- Index for looking up audit logs by actor
CREATE INDEX idx_audit_actor ON audit_logs(actor_sub);
-- Index for time-based queries (most recent first)
CREATE INDEX idx_audit_timestamp ON audit_logs(created_at DESC);
-- rambler down
DROP INDEX IF EXISTS idx_audit_timestamp;
DROP INDEX IF EXISTS idx_audit_actor;
DROP INDEX IF EXISTS idx_audit_entity;
DROP TABLE IF EXISTS audit_logs;
DROP TYPE IF EXISTS audit_action;
DROP TYPE IF EXISTS audit_entity_type;
