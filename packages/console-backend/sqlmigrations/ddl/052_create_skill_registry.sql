-- rambler up

-- ============================================================================
-- Skill Registry: platform-wide catalog of known skills.
-- Supports private (user-owned) and public visibility.
-- Skills can be standalone (browsable) or scoped to a sub-agent.
-- ============================================================================

CREATE TABLE skill_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    description TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('github', 'nannos')),
    source_repo TEXT,
    source_ref TEXT DEFAULT 'main',
    source_path TEXT,
    files JSONB NOT NULL DEFAULT '[]'::JSONB,
    content_hash TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::JSONB,
    security_verdict TEXT CHECK (security_verdict IN ('safe', 'caution', 'unsafe')),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    owner_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    scope TEXT NOT NULL DEFAULT 'standalone' CHECK (scope IN ('standalone', 'sub-agent')),
    sub_agent_id INTEGER REFERENCES sub_agents(id) ON DELETE SET NULL,
    sandbox_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Full-text search index on name + description
CREATE INDEX idx_skill_registry_search ON skill_registry USING GIN (
    to_tsvector('english', name || ' ' || COALESCE(description, ''))
);

-- Global slug uniqueness
CREATE UNIQUE INDEX uq_skill_registry_slug ON skill_registry (slug);

-- Content hash for same-skill detection
CREATE INDEX idx_skill_registry_hash ON skill_registry (content_hash);

-- Visibility-scoped queries
CREATE INDEX idx_skill_registry_visibility ON skill_registry (visibility);

-- Owner lookup (my skills)
CREATE INDEX idx_skill_registry_owner ON skill_registry (owner_id);

-- Private skills: one slug per owner
CREATE UNIQUE INDEX idx_skill_registry_private_slug ON skill_registry (owner_id, slug)
WHERE visibility = 'private';

-- Sub-agent scoped skills lookup
CREATE INDEX idx_skill_registry_sub_agent ON skill_registry (sub_agent_id)
WHERE scope = 'sub-agent';

-- Add 'skill' to audit entity type enum
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'skill';

-- ============================================================================
-- Skill Registry Versions: stores snapshots keyed by content_hash + timestamp.
-- ============================================================================

CREATE TABLE skill_registry_versions (
    id SERIAL PRIMARY KEY,
    skill_id UUID NOT NULL REFERENCES skill_registry(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    files JSONB NOT NULL DEFAULT '[]'::JSONB,
    description TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (skill_id, content_hash)
);

CREATE INDEX idx_skill_registry_versions_skill
    ON skill_registry_versions (skill_id, created_at DESC);

-- Seed initial version for any pre-existing skills
INSERT INTO skill_registry_versions (skill_id, content_hash, files, description, created_by, created_at)
SELECT id, content_hash, files, description, created_by, COALESCE(updated_at, created_at)
FROM skill_registry;

-- rambler down

DROP TABLE IF EXISTS skill_registry_versions;
DROP TABLE IF EXISTS skill_registry;
