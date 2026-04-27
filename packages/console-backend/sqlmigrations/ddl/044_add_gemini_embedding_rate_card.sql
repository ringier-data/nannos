-- rambler up

-- Add rate card for Gemini Embedding 2 (Vertex AI)
-- Pricing as of April 2026: https://ai.google.dev/pricing
-- $0.20 per 1M text tokens, $0.00012 per image, $0.00079 per video frame, $0.00016 per audio second
-- Images/video/audio converted to price_per_million for consistency with existing schema.

INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('google', 'gemini-embedding-2', '^gemini-embedding-2.*$')
ON CONFLICT (provider, model_name) DO NOTHING;

-- Text tokens (input only — embedding models have no output tokens)
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'input_text_tokens', 'input', 0.20, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google' AND rc.model_name = 'gemini-embedding-2'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- Images ($0.00012 each = $120 per million)
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'input_images', 'input', 120.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google' AND rc.model_name = 'gemini-embedding-2'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- Video frames ($0.00079 each = $790 per million)
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'input_video_frames', 'input', 790.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google' AND rc.model_name = 'gemini-embedding-2'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- Audio seconds ($0.00016 each = $160 per million)
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'input_audio_seconds', 'input', 160.00, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'google' AND rc.model_name = 'gemini-embedding-2'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;


-- rambler down

DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'google'
      AND model_name = 'gemini-embedding-2'
)
AND effective_from = '2025-01-01 00:00:00';

DELETE FROM rate_cards
WHERE provider = 'google'
  AND model_name = 'gemini-embedding-2';
