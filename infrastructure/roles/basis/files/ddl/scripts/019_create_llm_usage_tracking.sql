-- rambler up
-- Create usage tracking tables for cost metering and billing
-- Supports granular billing unit tracking with flexible rate cards
-- Billing units can be: tokens (input/output), API requests, vector searches, or any custom unit

-- Enum for billing unit flow direction (categorizes units as input, output, or other)
CREATE TYPE billing_unit_flow_direction AS ENUM ('input', 'output', 'other');

-- Rate cards table
-- Stores metadata about pricing for each model
CREATE TABLE rate_cards (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,  -- 'bedrock-anthropic', 'azure-openai', 'google'
    model_name TEXT NOT NULL,  -- 'claude-sonnet-4-20250514', 'gpt-4o', 'gemini-2.0-pro'
    model_name_pattern TEXT,  -- Optional regex pattern for matching model variants (e.g., 'gpt-4o-mini.*')
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Ensure unique rate card per (provider, model)
    CONSTRAINT uq_rate_card UNIQUE (provider, model_name)
);

COMMENT ON COLUMN rate_cards.model_name_pattern IS 'Optional regex pattern for matching model name variants. If null, exact model_name match is required. If set, pattern is used for matching when exact match fails.';

-- Rate card entries table
-- Stores pricing per billing unit with time-awareness
-- Each rate card can have multiple entries (one per billing unit type)
-- Billing units can be: 'input', 'output', 'cache_read', 'requests', 'api_calls', etc.
CREATE TABLE rate_card_entries (
    id SERIAL PRIMARY KEY,
    rate_card_id INTEGER NOT NULL REFERENCES rate_cards(id) ON DELETE CASCADE,
    billing_unit TEXT NOT NULL,  -- 'input', 'output', 'cache_read', 'requests', 'api_calls', etc.
    flow_direction billing_unit_flow_direction NOT NULL,  -- Categorizes units as input, output, or other
    price_per_million NUMERIC(12, 6) NOT NULL,  -- USD per million units
    effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_until TIMESTAMPTZ,  -- NULL means currently active
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Ensure unique rate for each (rate_card, billing_unit) at a given time
    CONSTRAINT uq_rate_card_entry UNIQUE (rate_card_id, billing_unit, effective_from),
    
    -- Validate price is non-negative
    CONSTRAINT chk_price_non_negative CHECK (price_per_million >= 0)
);

-- Index for rate card entry lookups
CREATE INDEX idx_rate_card_entries_card ON rate_card_entries(rate_card_id, effective_from DESC);

-- Usage logs table
-- Primary table for tracking agent invocations with cost (LLM and non-LLM)
CREATE TABLE usage_logs (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT,  -- Links to DynamoDB conversations table
    sub_agent_id INTEGER REFERENCES sub_agents(id) ON DELETE SET NULL,
    sub_agent_config_version_id INTEGER REFERENCES sub_agent_config_versions(id) ON DELETE SET NULL,
    
    -- Provider and model details (nullable for agent-specific rate cards)
    provider TEXT,
    model_name TEXT,
    
    -- Cost information
    total_cost_usd NUMERIC(12, 8) NOT NULL,
    
    -- Tracing identifiers
    langsmith_run_id TEXT,
    langsmith_trace_id TEXT,
    
    -- Timestamps
    invoked_at TIMESTAMPTZ NOT NULL,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Indexes for common queries
    CONSTRAINT chk_cost_non_negative CHECK (total_cost_usd >= 0)
);

-- Indexes for usage log queries
CREATE INDEX idx_usage_logs_user ON usage_logs(user_id, invoked_at DESC);
CREATE INDEX idx_usage_logs_conversation ON usage_logs(conversation_id, invoked_at DESC);
CREATE INDEX idx_usage_logs_sub_agent ON usage_logs(sub_agent_id, invoked_at DESC);
CREATE INDEX idx_usage_logs_invoked_at ON usage_logs(invoked_at DESC);
CREATE INDEX idx_usage_logs_langsmith ON usage_logs(langsmith_run_id) WHERE langsmith_run_id IS NOT NULL;

-- Usage billing units table
-- Stores granular billing unit breakdown per usage log
-- Billing units can be tokens, requests, API calls, vector searches, etc.
-- Only non-zero unit counts are stored (omit zeros)
CREATE TABLE usage_billing_units (
    id BIGSERIAL PRIMARY KEY,
    usage_log_id BIGINT NOT NULL REFERENCES usage_logs(id) ON DELETE CASCADE,
    billing_unit TEXT NOT NULL,  -- Billing unit name: 'input_tokens', 'requests', 'api_calls', etc.
    unit_count INTEGER NOT NULL,
    
    -- Validate unit count is positive (we don't store zeros)
    CONSTRAINT chk_unit_count_positive CHECK (unit_count > 0),
    
    -- Prevent duplicate billing units per usage log
    CONSTRAINT uq_billing_unit_per_log UNIQUE (usage_log_id, billing_unit)
);

-- Index for aggregation queries by billing unit type
CREATE INDEX idx_usage_billing_units_type ON usage_billing_units(billing_unit);

COMMENT ON TABLE usage_billing_units IS 'Stores billing unit breakdown for each usage log. Billing units can be tokens (input/output), API requests, vector searches, or any custom unit defined by the agent.';
COMMENT ON COLUMN usage_billing_units.billing_unit IS 'Billing unit name. For LLM usage: input_tokens, output_tokens, cache_read_tokens. For request-based pricing: requests. For custom agents: api_calls, vector_searches, etc.';

-- rambler down
DROP TABLE IF EXISTS usage_billing_units CASCADE;
DROP TABLE IF EXISTS usage_logs CASCADE;
DROP TABLE IF EXISTS rate_card_entries CASCADE;
DROP TABLE IF EXISTS rate_cards CASCADE;
DROP TYPE IF EXISTS billing_unit_flow_direction;
 