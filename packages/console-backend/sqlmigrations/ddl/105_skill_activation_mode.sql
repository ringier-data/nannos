-- rambler up

-- ADR-0011: a sub-agent that activates a registry skill it does not own REFERENCES the
-- publisher's row; it never holds a copy. The activation carries how the reference moves:
--   pinned    — the content hash in the referrer's config moves only when a writer of the
--               referrer explicitly updates it (the default, and the only mode for
--               personal/group activations, whose update path is the docstore).
--   following — every content change on the publisher's row writes a new auto-approved
--               version of the referrer carrying the new hash (a "bump").
-- A bump that fails is recorded here and skipped; the referrer stays on its previous hash.
ALTER TABLE skill_activations
    ADD COLUMN mode TEXT NOT NULL DEFAULT 'pinned',
    ADD COLUMN last_bump_error TEXT,
    ADD COLUMN last_bump_at TIMESTAMPTZ;
ALTER TABLE skill_activations
    ADD CONSTRAINT skill_activations_mode_check CHECK (mode IN ('pinned', 'following'));
-- Following is a sub-agent-scope concept only.
ALTER TABLE skill_activations
    ADD CONSTRAINT skill_activations_following_is_sub_agent_scope
        CHECK (mode = 'pinned' OR scope = 'sub-agent');
-- The bump fan-out looks up "every following activation of this row".
CREATE INDEX idx_skill_activations_following
    ON skill_activations (registry_id) WHERE mode = 'following';

-- rambler down

DROP INDEX IF EXISTS idx_skill_activations_following;
ALTER TABLE skill_activations
    DROP CONSTRAINT IF EXISTS skill_activations_following_is_sub_agent_scope,
    DROP CONSTRAINT IF EXISTS skill_activations_mode_check;
ALTER TABLE skill_activations
    DROP COLUMN IF EXISTS last_bump_at,
    DROP COLUMN IF EXISTS last_bump_error,
    DROP COLUMN IF EXISTS mode;
