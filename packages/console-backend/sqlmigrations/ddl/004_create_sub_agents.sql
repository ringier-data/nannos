-- rambler up
-- Enum types for sub-agent type, status, and owner status
CREATE TYPE sub_agent_type AS ENUM ('remote', 'local');
CREATE TYPE sub_agent_status AS ENUM (
    'draft',
    'pending_approval',
    'approved',
    'rejected'
);
CREATE TYPE owner_status AS ENUM ('active', 'suspended', 'deleted');
-- Sub-agents table
-- Metadata only - configuration data lives in sub_agent_config_versions
CREATE TABLE sub_agents (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    owner_status owner_status NOT NULL DEFAULT 'active',
    type sub_agent_type NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    default_version INTEGER,
    -- NULL means no approved version yet
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Index for owner's sub-agents (excluding deleted)
CREATE INDEX idx_subagents_owner ON sub_agents(owner_user_id)
WHERE deleted_at IS NULL;
-- Index for active sub-agents
CREATE INDEX idx_subagents_active ON sub_agents(id)
WHERE deleted_at IS NULL;
-- Trigger function to sync owner_status when user status changes
CREATE OR REPLACE FUNCTION sync_sub_agent_owner_status() RETURNS TRIGGER AS $$ BEGIN IF NEW.status IS DISTINCT
FROM OLD.status THEN
UPDATE sub_agents
SET owner_status = NEW.status::text::owner_status,
    updated_at = NOW()
WHERE owner_user_id = NEW.id;
END IF;
RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- Trigger to automatically update sub_agents.owner_status when users.status changes
CREATE TRIGGER trg_sync_sub_agent_owner_status
AFTER
UPDATE OF status ON users FOR EACH ROW EXECUTE FUNCTION sync_sub_agent_owner_status();
-- rambler down
DROP TRIGGER IF EXISTS trg_sync_sub_agent_owner_status ON users;
DROP FUNCTION IF EXISTS sync_sub_agent_owner_status();
DROP INDEX IF EXISTS idx_subagents_active;
DROP INDEX IF EXISTS idx_subagents_owner;
DROP TABLE IF EXISTS sub_agents;
DROP TYPE IF EXISTS owner_status;
DROP TYPE IF EXISTS sub_agent_status;
DROP TYPE IF EXISTS sub_agent_type;
