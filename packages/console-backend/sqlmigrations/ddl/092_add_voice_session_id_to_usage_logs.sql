-- rambler up

-- Allow usage_logs rows to be linked to the voice session (phone call) that incurred them.
-- Nullable: non-voice invocations leave this NULL. Also the discriminator behind the
-- 'voice' service in get_usage_by_service.

ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS voice_session_id UUID
        REFERENCES voice_sessions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_usage_logs_voice_session
    ON usage_logs (voice_session_id)
    WHERE voice_session_id IS NOT NULL;

-- rambler down

DROP INDEX IF EXISTS idx_usage_logs_voice_session;

ALTER TABLE usage_logs
    DROP COLUMN IF EXISTS voice_session_id;
