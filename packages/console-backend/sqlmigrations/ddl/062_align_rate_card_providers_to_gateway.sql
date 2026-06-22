-- rambler up
-- Rate cards key on (provider, model_name) and cost lookup matches the provider the
-- Model Gateway reports at runtime (LiteLLM `custom_llm_provider`). The early seeds
-- (019/020/023/038/044/045) used direct-vendor API names — google_genai / google /
-- openai — but through the gateway these models are routed (and therefore logged) under
-- LiteLLM's provider names: Gemini via Vertex -> `vertex_ai`, Azure GPT-4o -> `azure`.
-- The mismatch made those usage logs fall back to $0 (no matching rate card). Claude
-- already uses `bedrock_converse`, which matches. Realign the gateway-routed models.
UPDATE rate_cards SET provider = 'vertex_ai'
 WHERE provider IN ('google_genai', 'google') AND model_name LIKE 'gemini-%';

UPDATE rate_cards SET provider = 'azure'
 WHERE provider = 'openai' AND model_name IN ('gpt-4o', 'gpt-4o-mini');

-- Claude on Bedrock: the gateway routes via `bedrock/...`, which LiteLLM reports as
-- `bedrock` (not `bedrock_converse`, which the old in-app ChatBedrockConverse used).
-- Scoped to `claude-%` to mirror the down migration exactly (every bedrock_converse seed is
-- claude-*, so this is identical on real data) — a blanket UPDATE couldn't be reversed, since
-- the down couldn't tell a converted row from one that was always `bedrock`.
UPDATE rate_cards SET provider = 'bedrock'
 WHERE provider = 'bedrock_converse' AND model_name LIKE 'claude-%';

-- rambler down
UPDATE rate_cards SET provider = 'bedrock_converse'
 WHERE provider = 'bedrock' AND model_name LIKE 'claude-%';
UPDATE rate_cards SET provider = 'google'
 WHERE provider = 'vertex_ai' AND model_name = 'gemini-embedding-2';
UPDATE rate_cards SET provider = 'google_genai'
 WHERE provider = 'vertex_ai' AND model_name LIKE 'gemini-%' AND model_name <> 'gemini-embedding-2';
UPDATE rate_cards SET provider = 'openai'
 WHERE provider = 'azure' AND model_name IN ('gpt-4o', 'gpt-4o-mini');
