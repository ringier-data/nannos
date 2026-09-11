-- rambler up
-- Embed bindings (ADR-0006): a sub-agent whose definition is PUBLISHED BY A HOST under
-- <base_url>/.well-known/agent-skills/ and kept in sync by console-backend. Tokens whose
-- `azp` is listed for the binding select that sub-agent for embed mode and activate the
-- user on arrival, so nobody grants permissions by hand. One azp maps to at most one
-- sub-agent per cluster (the side table's primary key enforces it).
CREATE TABLE sub_agent_embed_bindings (
    sub_agent_id   INTEGER PRIMARY KEY REFERENCES sub_agents(id) ON DELETE CASCADE,
    base_url       TEXT NOT NULL,
    revision       TEXT,                       -- last synced well-known revision (wk<rev> hash source)
    definition     JSONB,                      -- parsed agent + skill index of that revision (admin view)
    fetched_at     TIMESTAMPTZ,
    last_error     TEXT,
    last_error_at  TIMESTAMPTZ,
    last_seen_at   TIMESTAMPTZ,                -- last socket connect that used this binding
    azps_seen      JSONB NOT NULL DEFAULT '{}', -- {azp: last seen ISO timestamp}
    created_by     TEXT NOT NULL REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE sub_agent_embed_binding_azps (
    azp            TEXT PRIMARY KEY,
    sub_agent_id   INTEGER NOT NULL REFERENCES sub_agent_embed_bindings(sub_agent_id) ON DELETE CASCADE
);
CREATE INDEX idx_sub_agent_embed_binding_azps_sub_agent ON sub_agent_embed_binding_azps(sub_agent_id);

-- The sub-agent a token-authenticated socket is bound to, stamped at connect from the
-- token's azp. handle_send_message reads it from here, never from the client payload.
ALTER TABLE socket_sessions ADD COLUMN embedded_sub_agent_id INTEGER;

-- Activation rows written at connect for bound users.
ALTER TYPE activation_source ADD VALUE IF NOT EXISTS 'embed';

-- rambler down
ALTER TABLE socket_sessions DROP COLUMN IF EXISTS embedded_sub_agent_id;
DROP TABLE IF EXISTS sub_agent_embed_binding_azps;
DROP TABLE IF EXISTS sub_agent_embed_bindings;
-- 'embed' cannot be removed from activation_source (Postgres has no DROP VALUE); harmless to keep.
