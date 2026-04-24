-- rambler up

-- Sessions table: stores authenticated user sessions (migrated from DynamoDB)
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    access_token_expires_at TIMESTAMPTZ,
    refresh_token TEXT NOT NULL,
    id_token TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    orchestrator_session_cookie TEXT,
    orchestrator_cookie_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);

-- Conversations table: stores conversation metadata (migrated from DynamoDB)
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    last_message_at TIMESTAMPTZ NOT NULL,
    last_updated TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT NOT NULL DEFAULT '',
    agent_url TEXT NOT NULL DEFAULT '',
    sub_agent_config_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversations_user_id ON conversations(user_id);
CREATE UNIQUE INDEX idx_conversations_user_conversation ON conversations(user_id, conversation_id);

-- Messages table: stores chat message history (migrated from DynamoDB)
CREATE TABLE messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    sort_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    parts JSONB NOT NULL DEFAULT '[]',
    task_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'unknown',
    raw_payload TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}',
    kind TEXT NOT NULL DEFAULT '',
    final BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_messages_conversation_created ON messages(conversation_id, created_at);
CREATE INDEX idx_messages_user_id ON messages(user_id);

-- rambler down

DROP INDEX IF EXISTS idx_messages_user_id;
DROP INDEX IF EXISTS idx_messages_conversation_created;
DROP TABLE IF EXISTS messages;

DROP INDEX IF EXISTS idx_conversations_user_conversation;
DROP INDEX IF EXISTS idx_conversations_user_id;
DROP TABLE IF EXISTS conversations;

DROP INDEX IF EXISTS idx_sessions_expires_at;
DROP INDEX IF EXISTS idx_sessions_user_id;
DROP TABLE IF EXISTS sessions;
