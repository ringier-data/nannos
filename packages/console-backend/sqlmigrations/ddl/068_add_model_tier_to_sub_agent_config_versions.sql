-- rambler up
-- Add a model TIER reference to sub-agent config versions (model tiers).
--
-- A sub-agent may bind to a capability tier ('low'/'standard'/'premium') instead of a
-- concrete model alias. The tier resolves to the current model_defaults slot for that tier
-- (chat:low / chat / chat:premium) at read time (services/model_status.py), so retiring or
-- upgrading a model is one slot repoint rather than editing every sub-agent. A row sets
-- EITHER model (a concrete alias) OR model_tier, never both.

ALTER TABLE sub_agent_config_versions
ADD COLUMN model_tier TEXT
    CHECK (model_tier IS NULL OR model_tier IN ('low', 'standard', 'premium'));

-- At most one of (model, model_tier) may be set on a version.
ALTER TABLE sub_agent_config_versions
ADD CONSTRAINT sub_agent_config_versions_model_xor_tier
    CHECK (model IS NULL OR model_tier IS NULL);

COMMENT ON COLUMN sub_agent_config_versions.model_tier IS
    'Capability tier (low/standard/premium) the agent binds to instead of a concrete model alias; '
    'resolved to the chat:<tier> model_defaults slot at read time. Mutually exclusive with model.';

-- rambler down
ALTER TABLE sub_agent_config_versions
DROP CONSTRAINT IF EXISTS sub_agent_config_versions_model_xor_tier;

ALTER TABLE sub_agent_config_versions
DROP COLUMN IF EXISTS model_tier;
