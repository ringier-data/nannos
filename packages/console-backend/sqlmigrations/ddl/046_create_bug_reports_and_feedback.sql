-- rambler up
-- Bug reports table: stores bug reports from orchestrator and client-triggered paths
CREATE TABLE bug_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id TEXT NOT NULL,
    message_id TEXT,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    task_id TEXT,
    external_link TEXT,
    debug_conversation_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_bug_reports_conversation_id ON bug_reports(conversation_id);
CREATE INDEX idx_bug_reports_user_id_created ON bug_reports(user_id, created_at DESC);
-- Message feedback table: per-message thumbs up/down
CREATE TABLE message_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    comment TEXT,
    sub_agent_id TEXT,
    task_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX idx_message_feedback_unique ON message_feedback(conversation_id, message_id, user_id);
-- Add bug_report to audit entity type enum
ALTER TYPE audit_entity_type
ADD VALUE 'bug_report';
ALTER TABLE sub_agents
ADD COLUMN system_role TEXT;
-- rambler down
ALTER TABLE sub_agents DROP COLUMN IF EXISTS system_role;
DROP INDEX IF EXISTS idx_message_feedback_unique;
DROP TABLE IF EXISTS message_feedback;
DROP INDEX IF EXISTS idx_bug_reports_user_id_created;
DROP INDEX IF EXISTS idx_bug_reports_conversation_id;
DROP TABLE IF EXISTS bug_reports;
-- Note: PostgreSQL does not support removing enum values.
-- The 'bug_report' value in audit_entity_type will remain.
