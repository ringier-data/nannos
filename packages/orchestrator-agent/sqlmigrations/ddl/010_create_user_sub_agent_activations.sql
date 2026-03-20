-- rambler up
-- Table to track which sub-agents each user has activated
-- Users can activate/deactivate sub-agents they have access to from settings
-- Only activated sub-agents are available to the orchestrator
CREATE TABLE user_sub_agent_activations (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sub_agent_id INTEGER NOT NULL REFERENCES sub_agents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, sub_agent_id)
);
-- Index for looking up user's activated sub-agents
CREATE INDEX idx_user_activations_user ON user_sub_agent_activations(user_id);
-- Index for looking up which users have activated a sub-agent
CREATE INDEX idx_user_activations_subagent ON user_sub_agent_activations(sub_agent_id);
-- Composite index for efficient activation checks
CREATE INDEX idx_user_activations_lookup ON user_sub_agent_activations(user_id, sub_agent_id);
-- rambler down
DROP INDEX IF EXISTS idx_user_activations_lookup;
DROP INDEX IF EXISTS idx_user_activations_subagent;
DROP INDEX IF EXISTS idx_user_activations_user;
DROP TABLE IF EXISTS user_sub_agent_activations;
