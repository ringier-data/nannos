-- rambler up

-- ============================================================================
-- Skill Activations: tracks which skills from the registry are active on which agents.
-- Each activation pins a specific content_hash from the registry at activation time.
-- "Update available" = activation.content_hash != skill_registry.content_hash.
-- ============================================================================

CREATE TABLE skill_activations (
    id SERIAL PRIMARY KEY,
    sub_agent_id INTEGER NOT NULL REFERENCES sub_agents(id) ON DELETE CASCADE,
    registry_id UUID NOT NULL REFERENCES skill_registry(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'group')),
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES user_groups(id) ON DELETE SET NULL,
    content_hash TEXT NOT NULL,
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    config_version_id INTEGER REFERENCES sub_agent_config_versions(id) ON DELETE SET NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_by TEXT NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT chk_activation_scope CHECK (
        locked = TRUE
        OR (scope = 'personal' AND user_id IS NOT NULL)
        OR (scope = 'group' AND group_id IS NOT NULL)
    )
);

-- One activation per skill per agent per scope per user/group
CREATE UNIQUE INDEX idx_skill_activations_unique ON skill_activations (
    sub_agent_id, registry_id, scope,
    COALESCE(user_id, ''),
    COALESCE(group_id::text, '')
);

-- Fast lookup: all activations for an agent
CREATE INDEX idx_skill_activations_agent ON skill_activations (sub_agent_id);

-- Fast lookup: all activations of a registry entry (for update-available notifications)
CREATE INDEX idx_skill_activations_registry ON skill_activations (registry_id);

-- Fast lookup: all activations by a user
CREATE INDEX idx_skill_activations_user ON skill_activations (user_id)
WHERE user_id IS NOT NULL;

-- rambler down

DROP TABLE IF EXISTS skill_activations;
