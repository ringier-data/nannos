-- rambler up

-- Rename gemini-3-pro-preview to gemini-3.1-pro-preview
-- The model was renamed by Google (gemini-3-pro-preview no longer exists on Vertex AI)
UPDATE rate_cards
SET model_name = 'gemini-3.1-pro-preview',
    model_name_pattern = '^gemini-3\.1-pro-preview.*$'
WHERE provider = 'google_genai'
  AND model_name = 'gemini-3-pro-preview';

-- Add Gemini 3.1 Flash Lite Preview rate card
-- Input: $0.25/1M (text, image, video), Output: $1.50/1M
INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('google_genai', 'gemini-3.1-flash-lite-preview', '^gemini-3\.1-flash-lite-preview.*$')
ON CONFLICT (provider, model_name) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 0.25, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-flash-lite-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 1.50, '2026-04-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google_genai' AND rc.model_name = 'gemini-3.1-flash-lite-preview'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- rambler down

DELETE FROM rate_card_entries WHERE rate_card_id IN (
    SELECT id FROM rate_cards WHERE provider = 'google_genai' AND model_name = 'gemini-3.1-flash-lite-preview'
);
DELETE FROM rate_cards WHERE provider = 'google_genai' AND model_name = 'gemini-3.1-flash-lite-preview';

-- Revert: rename back to gemini-3-pro-preview
UPDATE rate_cards
SET model_name = 'gemini-3-pro-preview',
    model_name_pattern = '^gemini-3-pro-preview.*$'
WHERE provider = 'google_genai'
  AND model_name = 'gemini-3.1-pro-preview';
