-- rambler up
-- Version history for sub-agent configurations (primarily for local agents)
-- Stores full configuration snapshots for auditability and revert capability
CREATE TABLE sub_agent_config_versions (
    id SERIAL PRIMARY KEY,
    sub_agent_id INTEGER NOT NULL REFERENCES sub_agents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    -- Content hash (first 12 chars of SHA256) - used to identify drafts/pending versions
    version_hash VARCHAR(12),
    -- Release number - only assigned when version is approved (1, 2, 3, ...)
    release_number INTEGER,
    description TEXT NOT NULL,
    -- Agent description (skill set) - crucial for orchestrator routing
    model TEXT,
    -- LLM model to use for this version (e.g., 'gpt-4', 'claude-3-opus')
    
    -- Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url
    system_prompt TEXT,        -- For local sub-agents: the system prompt
    agent_url TEXT,            -- For remote sub-agents: the URL of the agent
    mcp_tools JSONB DEFAULT '[]'::JSONB,  -- MCP tools available to this sub-agent
    
    change_summary TEXT,
    status sub_agent_status NOT NULL DEFAULT 'draft',
    approved_by_user_id TEXT REFERENCES users(id) ON DELETE
    SET NULL,
        approved_at TIMESTAMPTZ,
        rejection_reason TEXT,
        -- Soft delete for non-approved versions
        deleted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (sub_agent_id, version),
        -- Ensure exactly one of system_prompt or agent_url is provided (local XOR remote)
        CHECK (
            (system_prompt IS NOT NULL AND agent_url IS NULL) OR
            (system_prompt IS NULL AND agent_url IS NOT NULL)
        )
);
-- Index for looking up versions of a sub-agent
CREATE INDEX idx_sub_agent_versions_lookup ON sub_agent_config_versions(sub_agent_id, version DESC);
-- Index for looking up by hash (globally unique enough with 12 hex chars)
CREATE INDEX idx_sub_agent_versions_hash ON sub_agent_config_versions(version_hash);
-- Index for looking up by release number within a sub-agent
CREATE INDEX idx_sub_agent_versions_release ON sub_agent_config_versions(sub_agent_id, release_number);
-- rambler down
DROP INDEX IF EXISTS idx_sub_agent_versions_release;
DROP INDEX IF EXISTS idx_sub_agent_versions_hash;
DROP INDEX IF EXISTS idx_sub_agent_versions_lookup;
DROP TABLE IF EXISTS sub_agent_config_versions;
