-- rambler up

-- Rate cards for the voice agent's two Vertex models.
--
-- Migration 076 states no rate cards are seeded going forward, because every model gets
-- its card at registration. These two are the exception that rule does not cover: the
-- voice agent calls Vertex AI directly (packages/voice-agent), never through the Model
-- Gateway, so no registration flow will ever run for them and nothing else would create
-- their cards. Without a card `calculate_cost` fails closed to $0.00 (usage_service.py),
-- so every voice call would silently bill nothing.
--
-- Keyed on provider 'vertex_ai' — the runtime family the agent reports (USAGE_PROVIDER in
-- voice_agent/agent.py). model_name must match GEMINI_MODEL_ID / GEMINI_RISK_SCORER_MODEL.
--
-- Prices: USD per 1M tokens, Vertex AI Standard tier, verified 2026-08-19 against
-- cloud.google.com/vertex-ai/generative-ai/pricing (the page lists the Live model as
-- "Gemini 2.5 Flash Live API"). Audio is ~6x text on output — that asymmetry is why the
-- agent preserves the modality split.
--
-- ON CONFLICT DO NOTHING throughout: gemini-2.5-flash may already be registered on the
-- gateway with a card, and that existing card stays authoritative.

INSERT INTO rate_cards (provider, model_name)
VALUES
    ('vertex_ai', 'gemini-live-2.5-flash-native-audio'),
    ('vertex_ai', 'gemini-2.5-flash')
ON CONFLICT (provider, model_name) DO NOTHING;

-- ── Gemini Live native audio (the voice session model) ───────────────────────
-- audio in $3.00 / audio out $12.00 / text in $0.50 / text out $2.00 per 1M.
-- tool_use_input_tokens is the agent's own unit for tool-use prompt tokens, priced as
-- ordinary input. reasoning_output_tokens (the platform's unit for thinking tokens, priced
-- on the gemini-3.x cards since migration 023) is set ONLY on the risk scorer: measured
-- 2026-09-09, gemini-live-2.5-flash-native-audio REJECTS thinking_config outright (1007
-- "not supported by this model") and reports thoughtsTokenCount=0 on every turn, so the
-- Live card would price a unit that never arrives. gemini-2.5-flash does think — 476 of
-- 520 output tokens on a real tool classification — and needs it or 86% of the call is $0. Cached input is deliberately NOT priced: Vertex prints "N/A" for this
-- model's cached column, so there is no published rate to enter. If a call ever reports
-- cached tokens it surfaces as a "missing rate card / partial cost" warning — the signal
-- to go find the rate, not a silent $0.
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, u.billing_unit, u.flow_direction::billing_unit_flow_direction, u.price, '2026-01-01 00:00:00', NULL
FROM rate_cards rc
CROSS JOIN (VALUES
    ('audio_input_tokens',    'input',   3.00),
    ('audio_output_tokens',   'output', 12.00),
    ('base_input_tokens',     'input',   0.50),
    ('base_output_tokens',    'output',  2.00),
    ('tool_use_input_tokens', 'input',   0.50)
) AS u(billing_unit, flow_direction, price)
WHERE rc.provider = 'vertex_ai' AND rc.model_name = 'gemini-live-2.5-flash-native-audio'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- ── Gemini 2.5 Flash (the MCP tool risk scorer) ─────────────────────────────
-- text in $0.30 / text out $2.50 / cached text in $0.03 per 1M.
INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from, effective_until)
SELECT rc.id, u.billing_unit, u.flow_direction::billing_unit_flow_direction, u.price, '2026-01-01 00:00:00', NULL
FROM rate_cards rc
CROSS JOIN (VALUES
    ('base_input_tokens',       'input',  0.30),
    ('base_output_tokens',      'output', 2.50),
    ('reasoning_output_tokens', 'output', 2.50),
    ('cache_read_input_tokens', 'input',  0.03),
    ('tool_use_input_tokens',   'input',  0.30)
) AS u(billing_unit, flow_direction, price)
WHERE rc.provider = 'vertex_ai' AND rc.model_name = 'gemini-2.5-flash'
ON CONFLICT (rate_card_id, billing_unit, effective_from) DO NOTHING;

-- rambler down

DELETE FROM rate_card_entries
WHERE rate_card_id IN (
    SELECT id FROM rate_cards
    WHERE provider = 'vertex_ai'
      AND model_name IN ('gemini-live-2.5-flash-native-audio', 'gemini-2.5-flash')
)
AND effective_from = '2026-01-01 00:00:00';

-- Only remove the cards themselves if this migration left them with no entries at all
-- (i.e. nothing else — a gateway registration — has priced them since).
DELETE FROM rate_cards rc
WHERE rc.provider = 'vertex_ai'
  AND rc.model_name IN ('gemini-live-2.5-flash-native-audio', 'gemini-2.5-flash')
  AND NOT EXISTS (SELECT 1 FROM rate_card_entries e WHERE e.rate_card_id = rc.id);
