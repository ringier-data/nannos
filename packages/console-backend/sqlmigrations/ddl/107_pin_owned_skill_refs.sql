-- rambler up
-- ADR-0013: a sub-agent's OWN skills are pinned by the hash in its config version, like a
-- referenced skill, instead of resolving to the registry row's latest content. Until now
-- an own-skill edit outside a config save (registry UI, MCP skill tools) changed the row
-- but wrote no version, so the hash a version holds for an own skill can be stale while
-- the agent in fact ran the row's current content. Re-point those refs once, in EVERY
-- version, to the content each version actually served: from here on the history is an
-- audit trail, and a revert restores skill content.
--
-- Only refs whose row is owned by the version's own agent are touched. References to
-- other rows were pinned already and keep their hash.

-- backfill:pin-owned-skill-refs
UPDATE sub_agent_config_versions cv
SET skills = (
    SELECT COALESCE(
        jsonb_agg(
            CASE
                WHEN sr.id IS NOT NULL AND sr.sub_agent_id = cv.sub_agent_id
                    THEN e.skill || jsonb_build_object('content_hash', sr.content_hash)
                ELSE e.skill
            END
            ORDER BY e.ord
        ),
        '[]'::jsonb
    )
    FROM jsonb_array_elements(cv.skills) WITH ORDINALITY AS e(skill, ord)
    LEFT JOIN skill_registry sr ON sr.id::text = e.skill->>'registry_id'
)
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements(cv.skills) AS s(skill)
    JOIN skill_registry sr ON sr.id::text = s.skill->>'registry_id'
    WHERE sr.sub_agent_id = cv.sub_agent_id
      AND sr.content_hash IS DISTINCT FROM s.skill->>'content_hash'
);

-- rambler down
-- The previous hashes are not recoverable; before this migration they were never what
-- the agent ran with, so there is nothing to restore.
