-- rambler up

-- Seed initial rate cards for Claude, GPT-4o, and Gemini models
-- Prices are per million billing units based on provider pricing as of Jan 2025
-- model_name_pattern allows regex matching for model variants (e.g., gpt-4o-mini.* matches all dated versions)

-- First, insert rate cards (models) with optional regex patterns
-- Leave pattern NULL for exact match only, set pattern to match model variants
INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('bedrock_converse', 'claude-sonnet-4.5', '^(global\.anthropic\.)?claude-sonnet-4-5.*$'),
    ('bedrock_converse', 'claude-haiku-4.5', '^(global\.anthropic\.)?claude-haiku-4-5.*$'),
    ('openai', 'gpt-4o', '^gpt-4o(-\d{4}-\d{2}-\d{2})?$'),
    ('openai', 'gpt-4o-mini', '^gpt-4o-mini(-\d{4}-\d{2}-\d{2})?$')
ON CONFLICT (provider, model_name) DO NOTHING;

-- Claude Sonnet 4.5 on Bedrock (all regions)
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 3.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 15.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.30, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 3.75, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- Claude Haiku 4.5 on Bedrock
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 1.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-haiku-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 5.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-haiku-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.10, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-haiku-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 1.25, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-haiku-4.5'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- GPT-4o on Azure OpenAI
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 2.50, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-4o'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 15.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-4o'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 1.25, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-4o'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- GPT-4o-mini on Azure OpenAI
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 0.15, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-4o-mini'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 0.60, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-4o-mini'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.075, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-4o-mini'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;


-- rambler down

-- Remove seeded rate card entries first (CASCADE will handle this, but being explicit)
DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider IN ('bedrock_converse', 'openai', 'google')
      AND model_name IN (
        'claude-sonnet-4.5',
        'claude-haiku-4.5',
        'gpt-4o',
        'gpt-4o-mini'
      )
)
AND effective_from = '2025-01-01 00:00:00';

-- Remove seeded rate cards
DELETE FROM rate_cards
WHERE provider IN ('bedrock_converse', 'openai', 'google')
  AND model_name IN (
    'claude-sonnet-4.5',
    'claude-haiku-4.5',
    'gpt-4o',
    'gpt-4o-mini'
  );
