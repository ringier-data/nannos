-- rambler up

-- ============================================================================
-- 1. Seed a "system" user to own auto-provisioned agents.
-- ============================================================================
INSERT INTO users (id, sub, email, first_name, last_name, role, status)
VALUES ('system', 'system', 'system@nannos.ai', 'System', 'Agent', 'admin', 'active')
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 2. Seed voice-agent (remote, public, approved).
-- ============================================================================
INSERT INTO sub_agents (name, owner_user_id, type, is_public, current_version, default_version)
VALUES ('voice-agent', 'system', 'remote', TRUE, 1, 1)
ON CONFLICT DO NOTHING;

INSERT INTO sub_agent_config_versions (
    sub_agent_id, version, release_number, description, agent_url, status
)
SELECT sa.id, 1, 1,
       'Voice agent for phone calls via Gemini Live',
       'http://placeholder-voice-agent',
       'approved'
FROM sub_agents sa
WHERE sa.name = 'voice-agent' AND sa.owner_user_id = 'system'
  AND NOT EXISTS (
      SELECT 1 FROM sub_agent_config_versions cv
      WHERE cv.sub_agent_id = sa.id AND cv.version = 1
  );

-- ============================================================================
-- 3. Seed agent-creator (remote, public, approved).
-- ============================================================================
INSERT INTO sub_agents (name, owner_user_id, type, is_public, current_version, default_version)
VALUES ('agent-creator', 'system', 'remote', TRUE, 1, 1)
ON CONFLICT DO NOTHING;

INSERT INTO sub_agent_config_versions (
    sub_agent_id, version, release_number, description, agent_url, status
)
SELECT sa.id, 1, 1,
       'Agent Creator for building and managing sub-agents',
       'http://placeholder-agent-creator',
       'approved'
FROM sub_agents sa
WHERE sa.name = 'agent-creator' AND sa.owner_user_id = 'system'
  AND NOT EXISTS (
      SELECT 1 FROM sub_agent_config_versions cv
      WHERE cv.sub_agent_id = sa.id AND cv.version = 1
  );

-- ============================================================================
-- 4. Add voice_call flag to scheduled_jobs.
-- ============================================================================
ALTER TABLE scheduled_jobs
ADD COLUMN voice_call BOOLEAN NOT NULL DEFAULT FALSE;

-- rambler down

ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS voice_call;

DELETE FROM sub_agent_config_versions
WHERE sub_agent_id IN (
    SELECT id FROM sub_agents WHERE owner_user_id = 'system' AND name IN ('voice-agent', 'agent-creator')
);

DELETE FROM sub_agents WHERE owner_user_id = 'system' AND name IN ('voice-agent', 'agent-creator');

DELETE FROM users WHERE id = 'system';
