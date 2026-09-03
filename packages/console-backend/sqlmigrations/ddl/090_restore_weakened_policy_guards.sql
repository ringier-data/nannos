-- rambler up
-- Restore the static policy guards seeded in 057 that a derived profile weakened.
--
-- Found by QA against a real database: the phantom-classification path did not only
-- add junk rows, it OVERWROTE seeded policy. Seven of the thirteen guards were
-- weakened, SIX of them to below the 0.80 approval gate:
--
--   read_personal_file        1.00 -> 0.30  (+ LLM-guessed `path` globs) — reading a
--                                           user's personal file stopped asking
--   docstore_search           0.30 -> 0.10  with `risk_factors = {}`, losing the
--                                           `include_personal` factor entirely: the
--                                           "ask before searching personal documents"
--                                           gate was gone, not merely lowered
--   console_remove_skill      1.00 -> 0.60
--   console_activate_skill    1.00 -> 0.70
--   console_update_sub_agent  1.00 -> 0.70
--   console_create_bug_report 1.00 -> 0.40
--   console_import_skill      1.00 -> 0.80  (still gates, at the boundary)
--
-- So the self-improvement guards (removing a skill, activating one, editing a
-- sub-agent) stopped asking as well, not only the privacy pair.
--
-- Four also had `edit` ADDED where 057 withheld it — console_import_skill,
-- console_activate_skill, read_personal_file, docstore_search — because the LLM's
-- default action set replaced the policy's. That hands the user an argument-editing
-- affordance on an approve-or-reject-only guard, so the predicate below treats a
-- widened action set as a weakening too.
--
-- The mechanism: `upsert_score` replaces every column, so a classification of a tool
-- the gate could not fetch (scored from its name alone) landed on top of a hand-written
-- guard. 089 cannot repair this — it deletes junk rows by name, and these are the
-- policy rows themselves, wearing guessed values.
--
-- Only WEAKENINGS are repaired. The upsert re-asserts a row when the stored base_score
-- is BELOW the seeded one, when non-empty seeded risk_factors were emptied, or when the
-- stored allowed_actions are NOT contained in the seeded set (an affordance was added).
-- Each clause is one-directional, so an administrator who RAISED a score or NARROWED
-- the actions keeps their change, and re-running this migration is a no-op. That
-- asymmetry is the whole safety argument: a weakened guard is indistinguishable from an
-- admin's deliberate relaxation, but restoring it errs toward asking the user rather
-- than toward acting without asking.
--
-- schema_hash is reset to '' on purpose. That is what marks a row as policy rather than
-- derived, and it is what stops a future classification from overwriting it again
-- (`_schema_confirms_unchanged` treats an empty stored hash as "unchanged, trust it",
-- so the entry wins over a schema-derived profile).
--
-- DEPLOY: like 089, this does not reach already-warm pods — `ToolRiskCache._do_refresh`
-- merges additively. Ship with a rollout restart of orchestrator / agent-runner.
INSERT INTO tool_risk_scores (
        tool_name,
        server_slug,
        schema_hash,
        base_score,
        risk_factors,
        allowed_actions
    )
VALUES
    ('console_create_skill', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_update_skill', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_remove_skill', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_import_skill', 'console', '', 1.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    ('console_activate_skill', 'console', '', 1.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    ('console_update_playbook', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_write_skill_file', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_delete_skill_file', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_create_sub_agent', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_update_sub_agent', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('console_create_bug_report', 'console', '', 1.0, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB),
    ('read_personal_file', '_self', '', 1.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    (
        'docstore_search',
        '_self',
        '',
        0.3,
        '{"include_personal": {"risky_values": {"true": 1.0, "True": 1.0}, "default_contribution": 0.0}}'::JSONB,
        '["approve", "reject"]'::JSONB
    )
ON CONFLICT (tool_name, server_slug) DO UPDATE SET
    schema_hash = EXCLUDED.schema_hash,
    base_score = EXCLUDED.base_score,
    risk_factors = EXCLUDED.risk_factors,
    allowed_actions = EXCLUDED.allowed_actions,
    updated_at = NOW()
WHERE tool_risk_scores.base_score < EXCLUDED.base_score
   OR (tool_risk_scores.risk_factors = '{}'::JSONB AND EXCLUDED.risk_factors <> '{}'::JSONB)
   OR NOT (tool_risk_scores.allowed_actions <@ EXCLUDED.allowed_actions);

-- rambler down
-- Intentionally a no-op: this migration only ever restores a guard to the value 057
-- already declared, so there is nothing meaningful to roll back to. Rolling it back
-- would mean deliberately re-weakening a privacy guard.
SELECT 1;
