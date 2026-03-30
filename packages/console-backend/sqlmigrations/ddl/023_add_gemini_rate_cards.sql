-- rambler up

-- Seed rate cards for Gemini 3 models on Vertex AI
-- Prices are per million billing units based on Google pricing as of Jan 2025
-- Gemini 3 Pro has tiered pricing based on context window size (<= 200k vs > 200k tokens)
-- model_name_pattern allows regex matching for model variants

-- Insert rate cards (models) with regex patterns for Gemini variants
INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('google_genai', 'gemini-3-pro-preview', '^gemini-3-pro-preview.*$'),
    ('google_genai', 'gemini-3-flash-preview', '^gemini-3-flash-preview.*$')
ON CONFLICT (provider, model_name) DO NOTHING;

-- ========================================
-- Gemini 3 Pro Preview (Standard Tier: prompts <= 200k tokens)
-- Input: $2.00/1M, Output: $12.00/1M, Reasoning: $12.00/1M
-- Cache Read: $0.20/1M, Cache Creation: $0.20/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 2.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 12.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'reasoning_output_tokens', 'output', 12.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.20, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 0.20, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- ========================================
-- Gemini 3 Flash Preview (Single Tier)
-- Input: $0.50/1M, Output: $3.00/1M, Reasoning: $3.00/1M
-- Cache Read: $0.05/1M, Cache Creation: $0.05/1M
-- ========================================
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 0.50, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-flash-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 3.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-flash-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'reasoning_output_tokens', 'output', 3.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-flash-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.05, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-flash-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 0.05, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3-flash-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;


-- rambler down

-- Remove Gemini rate card entries first
DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'google_genai'
      AND model_name IN ('gemini-3-pro-preview', 'gemini-3-flash-preview')
);

-- Remove Gemini rate cards
DELETE FROM rate_cards
WHERE provider = 'google_genai'
  AND model_name IN ('gemini-3-pro-preview', 'gemini-3-flash-preview');
