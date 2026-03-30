-- rambler up

-- Amazon Titan Text Embeddings V2 on Bedrock
-- Pricing: $0.00002 per 1K tokens = $0.02 per million tokens (input only)
-- https://aws.amazon.com/bedrock/pricing/
INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('bedrock_embeddings', 'amazon.titan-embed-text-v2:0', NULL)
ON CONFLICT (provider, model_name) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'input_tokens', 'input', 0.02, '2025-01-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_embeddings' AND rc.model_name = 'amazon.titan-embed-text-v2:0'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- rambler down

DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'bedrock_embeddings'
      AND model_name = 'amazon.titan-embed-text-v2:0'
)
AND effective_from = '2025-01-01 00:00:00';

DELETE FROM rate_cards
WHERE provider = 'bedrock_embeddings'
  AND model_name = 'amazon.titan-embed-text-v2:0';
