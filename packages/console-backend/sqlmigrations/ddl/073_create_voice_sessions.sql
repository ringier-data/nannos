-- rambler up
-- Voice sessions table: tracks inbound Twilio calls and stores Gemini Live
-- session resumption handles so callers can continue a previous conversation.
CREATE TABLE voice_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    sub_agent_id INTEGER REFERENCES sub_agents(id) ON DELETE SET NULL,
    phone_number TEXT NOT NULL,
    call_sid TEXT,
    gemini_session_handle TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'failed', 'abandoned')),
    use_session_memory BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_voice_sessions_user_id ON voice_sessions (user_id);
CREATE INDEX idx_voice_sessions_user_sub_agent ON voice_sessions (user_id, sub_agent_id);
CREATE INDEX idx_voice_sessions_resumable
    ON voice_sessions (user_id, sub_agent_id, ended_at DESC)
    WHERE gemini_session_handle IS NOT NULL AND status = 'completed';

-- rambler down
DROP TABLE IF EXISTS voice_sessions;
