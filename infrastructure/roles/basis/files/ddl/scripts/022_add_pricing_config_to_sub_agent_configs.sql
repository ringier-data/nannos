-- rambler up
-- Add pricing_config JSONB column to sub_agent_config_versions
-- This allows remote and foundry agents to define their own rate cards
-- Always use rate_card_entries array format for consistent data structure
-- Frontend provides "simple" UI that converts to single-entry array with billing_unit="requests"
ALTER TABLE sub_agent_config_versions 
ADD COLUMN pricing_config JSONB DEFAULT NULL;

COMMENT ON COLUMN sub_agent_config_versions.pricing_config IS 'Agent-specific rate card configuration. Only applicable for remote and foundry agents. Format: {"rate_card_entries": [{"billing_unit": "requests", "price_per_million": 50000}]} for request-based pricing, or {"rate_card_entries": [{"billing_unit": "base_input_tokens", "price_per_million": 3.0}, {"billing_unit": "base_output_tokens", "price_per_million": 15.0}]} for detailed billing. IMPORTANT: Use base_input_tokens/base_output_tokens for standard tokens. Cache and specialized tokens use suffixes: cache_read_input_tokens, cache_creation_input_tokens, reasoning_tokens, audio_input_tokens, audio_output_tokens. Cost calculation falls back to base rates if specific billing unit is not found.';

-- rambler down
ALTER TABLE sub_agent_config_versions 
DROP COLUMN IF EXISTS pricing_config;
