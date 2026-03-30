-- rambler up

-- Seed rate cards for Claude models via Anthropic API (direct, not Bedrock)
-- Prices are per million billing units based on Anthropic pricing as of Mar 2026
-- Haiku 4.5 and Sonnet 4.6 prices match their Bedrock equivalents
-- Opus 4.6 uses Anthropic's standard opus pricing

INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('anthropic', 'claude-haiku-4-5-20251001', '^claude-haiku-4-5.*$'),
    ('anthropic', 'claude-sonnet-4-6', '^claude-sonnet-4-6.*$'),
    ('anthropic', 'claude-opus-4-6', '^claude-opus-4-6.*$')
ON CONFLICT (provider, model_name) DO NOTHING;

-- ========================================
-- Claude Haiku 4.5 (Anthropic API)
-- Input: $1.00/1M, Output: $5.00/1M
-- Cache Read: $0.10/1M, Cache Creation: $1.25/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 1.00, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-haiku-4-5-20251001'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 5.00, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-haiku-4-5-20251001'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.10, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-haiku-4-5-20251001'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 1.25, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-haiku-4-5-20251001'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- ========================================
-- Claude Sonnet 4.6 (Anthropic API)
-- Input: $3.00/1M, Output: $15.00/1M
-- Cache Read: $0.30/1M, Cache Creation: $3.75/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 3.00, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-sonnet-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 15.00, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-sonnet-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.30, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-sonnet-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 3.75, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-sonnet-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- ========================================
-- Claude Opus 4.6 (Anthropic API)
-- Input: $15.00/1M, Output: $75.00/1M
-- Cache Read: $1.50/1M, Cache Creation: $18.75/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 15.00, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-opus-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 75.00, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-opus-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 1.50, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-opus-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 18.75, '2026-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'anthropic' AND rc.model_name = 'claude-opus-4-6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;


-- rambler down

DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'anthropic'
      AND model_name IN (
        'claude-haiku-4-5-20251001',
        'claude-sonnet-4-6',
        'claude-opus-4-6'
      )
)
AND effective_from = '2026-03-01 00:00:00';

DELETE FROM rate_cards
WHERE provider = 'anthropic'
  AND model_name IN (
    'claude-haiku-4-5-20251001',
    'claude-sonnet-4-6',
    'claude-opus-4-6'
  );
