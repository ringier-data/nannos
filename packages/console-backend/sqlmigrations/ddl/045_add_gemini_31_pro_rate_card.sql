-- rambler up

-- Add rate card for Gemini 3.1 Pro Preview (replaces discontinued gemini-3-pro-preview)
-- Pricing matches gemini-3-pro-preview rates pending updated Google pricing

INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('google_genai', 'gemini-3.1-pro-preview', '^gemini-3\.1-pro-preview.*$')
ON CONFLICT (provider, model_name) DO NOTHING;

-- Input: $2.00/1M, Output: $12.00/1M, Reasoning: $12.00/1M
-- Cache Read: $0.20/1M, Cache Creation: $0.20/1M
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 2.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 12.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'reasoning_output_tokens', 'output', 12.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.20, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 0.20, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-pro-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;


-- rambler down

DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'google_genai'
      AND model_name = 'gemini-3.1-pro-preview'
);

DELETE FROM rate_cards
WHERE provider = 'google_genai'
  AND model_name = 'gemini-3.1-pro-preview';
