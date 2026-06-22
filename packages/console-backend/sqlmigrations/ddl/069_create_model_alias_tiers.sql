-- rambler up
-- Durable alias -> tier memory for within-tier graceful degradation.
--
-- When a sub-agent pins a CONCRETE model and that model is later retired, we want it to
-- degrade to the successor of the SAME tier the model served (e.g. a retired premium model
-- -> the current premium default), not to the generic standard chat default. The live
-- model_defaults table only holds the CURRENT alias per role, and a retired model's tier
-- slot has usually been repointed by then, so the alias->tier link is otherwise lost. This
-- table remembers it: one row per alias, holding the most-recent chat tier it was default for.
-- A model may be the default for several chat tiers at once, so the memory is keyed on
-- (alias, role) — one row per tier an alias has served (mirrors model_defaults' role keying).
CREATE TABLE model_alias_tiers (
    alias TEXT NOT NULL,
    role TEXT NOT NULL,
    -- chat-tier role: 'chat' (standard), 'chat:low', 'chat:premium'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (alias, role)
);
COMMENT ON TABLE model_alias_tiers IS 'Which chat tiers each alias has served as the fleet default; lets a retired concrete-model sub-agent degrade to its tier successor instead of the standard chat default.';
-- rambler down
DROP TABLE IF EXISTS model_alias_tiers;
