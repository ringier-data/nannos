-- rambler up

-- ============================================
-- 1. Create user_group_default_agents table
-- ============================================
-- Junction table to store which sub-agents are defaults for each group
CREATE TABLE user_group_default_agents (
    id SERIAL PRIMARY KEY,
    user_group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    sub_agent_id INTEGER NOT NULL REFERENCES sub_agents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    UNIQUE (user_group_id, sub_agent_id)
);

-- Index for efficient group default lookups
CREATE INDEX idx_group_defaults_group ON user_group_default_agents(user_group_id);
CREATE INDEX idx_group_defaults_agent ON user_group_default_agents(sub_agent_id);

-- ============================================
-- 2. Add activation tracking columns
-- ============================================
-- Add enum for activation source
CREATE TYPE activation_source AS ENUM ('user', 'group', 'admin');

-- Add columns to track activation source and multi-group membership
ALTER TABLE user_sub_agent_activations 
ADD COLUMN activated_by activation_source DEFAULT 'user',
ADD COLUMN activated_by_groups JSONB DEFAULT NULL;

-- GIN index for efficient JSONB array queries
CREATE INDEX idx_activations_by_groups ON user_sub_agent_activations USING GIN (activated_by_groups);

-- ============================================
-- 3. Backfill existing activations
-- ============================================
-- Mark all existing activations as user-initiated
UPDATE user_sub_agent_activations 
SET activated_by = 'user' 
WHERE activated_by IS NULL;

-- Make activated_by NOT NULL after backfill
ALTER TABLE user_sub_agent_activations 
ALTER COLUMN activated_by SET NOT NULL;

-- ============================================
-- 4. Create user_notifications table
-- ============================================
-- In-app notification inbox for users
CREATE TYPE notification_type AS ENUM (
    'agent_activated',
    'agent_deactivated', 
    'group_added',
    'group_removed',
    'role_updated',
    'approval_requested',
    'approval_completed',
    'approval_rejected',
    'agent_shared',
    'agent_access_revoked',
    'agent_permission_changed',
    'secret_shared',
    'secret_access_revoked',
    'secret_permission_changed',
    'system_announcement'
);


CREATE TABLE user_notifications (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type notification_type NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    read_at TIMESTAMPTZ DEFAULT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for efficient notification queries
CREATE INDEX idx_notifications_user ON user_notifications(user_id, created_at DESC);
CREATE INDEX idx_notifications_unread ON user_notifications(user_id, read_at) WHERE read_at IS NULL;

-- ============================================
-- 5. Update audit_logs enums
-- ============================================
-- Add new audit actions for default agents
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'set_group_defaults';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'add_group_default';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'remove_group_default';

-- Add notification entity type
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'notification';

-- rambler down

-- Drop notification entity type (cannot be removed from enum, so we skip)
-- Drop new audit actions (cannot be removed from enum, so we skip)

-- Drop user_notifications table
DROP TABLE IF EXISTS user_notifications;
DROP TYPE IF EXISTS notification_type;

-- Drop activation tracking columns and indexes
DROP INDEX IF EXISTS idx_activations_by_groups;

ALTER TABLE user_sub_agent_activations 
DROP COLUMN IF EXISTS activated_by_groups,
DROP COLUMN IF EXISTS activated_by;

DROP TYPE IF EXISTS activation_source;

-- Drop user_group_default_agents table
DROP INDEX IF EXISTS idx_group_defaults_agent;
DROP INDEX IF EXISTS idx_group_defaults_group;
DROP TABLE IF EXISTS user_group_default_agents;
