-- rambler up

-- Seed rate cards for GPT-5.4 Mini and GPT-5.4 Nano on Azure OpenAI
-- Prices per million tokens based on OpenAI pricing as of Apr 2026

INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('openai', 'gpt-5.4-mini', '^gpt-5\.4-mini(-\d{4}-\d{2}-\d{2})?$'),
    ('openai', 'gpt-5.4-nano', '^gpt-5\.4-nano(-\d{4}-\d{2}-\d{2})?$')
ON CONFLICT (provider, model_name) DO NOTHING;

-- ========================================
-- GPT-5.4 Mini on Azure OpenAI
-- Input: $0.75/1M, Cached Input: $0.075/1M, Output: $4.50/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 0.75, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-5.4-mini'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 4.50, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-5.4-mini'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.075, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-5.4-mini'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- ========================================
-- GPT-5.4 Nano on Azure OpenAI
-- Input: $0.20/1M, Cached Input: $0.02/1M, Output: $1.25/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 0.20, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-5.4-nano'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 1.25, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-5.4-nano'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.02, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'openai' AND rc.model_name = 'gpt-5.4-nano'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;


-- rambler down

DELETE FROM rate_card_entries WHERE rate_card_id IN (
    SELECT id FROM rate_cards WHERE provider = 'openai' AND model_name IN ('gpt-5.4-mini', 'gpt-5.4-nano')
);
DELETE FROM rate_cards WHERE provider = 'openai' AND model_name IN ('gpt-5.4-mini', 'gpt-5.4-nano');
