-- rambler up

INSERT INTO rate_cards (provider, model_name, model_name_pattern)
VALUES
    ('bedrock_converse', 'claude-sonnet-4.6', '^(global\.anthropic\.)?claude-sonnet-4-6.*$')
ON CONFLICT (provider, model_name) DO NOTHING;


-- Claude Sonnet 4.6 on Bedrock (all regions)
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_input_tokens', 'input', 3.00, '2025-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'base_output_tokens', 'output', 15.00, '2025-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_read_input_tokens', 'input', 0.30, '2025-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, 'cache_creation_input_tokens', 'input', 3.75, '2025-03-01 00:00:00', NULL
FROM rate_cards rc WHERE rc.provider = 'bedrock_converse' AND rc.model_name = 'claude-sonnet-4.6'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- rambler down

DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'bedrock_converse'
      AND model_name = 'claude-sonnet-4.6'
)
AND effective_from = '2025-03-01 00:00:00';

DELETE FROM rate_cards
WHERE provider = 'bedrock_converse'
    AND model_name = 'claude-sonnet-4.6';
