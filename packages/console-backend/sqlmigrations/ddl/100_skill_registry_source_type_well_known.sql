-- rambler up

-- Skills mirrored from a host's /.well-known/agent-skills/ tree (ADR-0006) get their
-- own source type. The embed-binding sync finds the row it wrote last time by
-- (sub_agent_id, source_type, name) and updates it in place, instead of creating a
-- fresh row with a -2, -3, … slug on every revision. A non-'nannos' source type also
-- makes the row read-only in the registry UI: the host's SKILL.md is the source of
-- truth. source_repo holds the authority base URL, source_ref the revision,
-- source_path the SKILL.md URL.
ALTER TABLE skill_registry
    DROP CONSTRAINT IF EXISTS skill_registry_source_type_check;
ALTER TABLE skill_registry
    ADD CONSTRAINT skill_registry_source_type_check
        CHECK (source_type IN ('github', 'nannos', 'well-known'));

-- rambler down

-- Rows written as 'well-known' must be re-typed before the narrower check can hold.
UPDATE skill_registry SET source_type = 'nannos' WHERE source_type = 'well-known';
ALTER TABLE skill_registry
    DROP CONSTRAINT IF EXISTS skill_registry_source_type_check;
ALTER TABLE skill_registry
    ADD CONSTRAINT skill_registry_source_type_check
        CHECK (source_type IN ('github', 'nannos'));
