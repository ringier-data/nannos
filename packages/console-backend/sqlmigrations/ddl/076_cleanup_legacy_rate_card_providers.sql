-- rambler up

-- Rate cards must be keyed on the provider the cost logger reports at runtime
-- (custom_llm_provider, else the deployment-id prefix — families like 'bedrock',
-- 'vertex_ai'; see console-backend AGENTS.md "Rate-Card Provider Must Equal the litellm
-- Provider Family"). Earlier seeds (020/037/038) and pre-derivation registrations created
-- cards under vocabularies the runtime never emits — LiteLLM catalog tags
-- ('bedrock_converse') and legacy in-app SDK detections ('anthropic',
-- 'bedrock_embeddings') — so every call on those models bills $0.
--
-- This migration makes existing databases consistent and neutralizes the seed migrations
-- for fresh databases (it runs after them):
--   1. re-key 'bedrock_converse' cards to 'bedrock' (the family the runtime reports),
--      preserving their pricing history, unless a 'bedrock' card for the same model
--      already exists (that one is already the authoritative billing card);
--   2. delete ONLY the legacy cards that are provably redundant — a card under a runtime
--      family for the same model that has a currently-effective entry, i.e. something is
--      really pricing that model today.
--
-- Deliberately NOT deleted: legacy cards whose same-model twin is missing or has no
-- effective entries. Dropping those cascades their entries and pricing history to nothing
-- and can leave the model priced at $0 — the irreversible opposite of `rekey`, which
-- refuses to merge two histories implicitly. They are surfaced instead: the provider
-- config check reports every active card keyed outside runtime_provider_families() as an
-- `orphan_cards` finding on the Rate Cards banner, where re-keying or expiring is a
-- deliberate, audited admin action.
--
-- Historical usage rows are untouched (total_cost_usd is computed at ingest); this only
-- changes how FUTURE calls are billed. Going forward no rate cards are seeded — every
-- model gets its card at registration (register/edit derive the runtime provider).

UPDATE rate_cards rc
SET provider = 'bedrock', updated_at = NOW()
WHERE rc.provider = 'bedrock_converse'
  AND NOT EXISTS (
      SELECT 1 FROM rate_cards twin
      WHERE twin.provider = 'bedrock' AND twin.model_name = rc.model_name
  );

DELETE FROM rate_cards rc
WHERE rc.provider IN ('bedrock_converse', 'anthropic', 'bedrock_embeddings')
  AND EXISTS (
      -- A same-model card that the runtime CAN report, and that is actually pricing today:
      -- only then is dropping this one a no-op for billing.
      SELECT 1
      FROM rate_cards twin
      JOIN rate_card_entries e ON e.rate_card_id = twin.id
      WHERE twin.model_name = rc.model_name
        AND twin.id <> rc.id
        AND twin.provider NOT IN ('bedrock_converse', 'anthropic', 'bedrock_embeddings')
        AND e.effective_from <= NOW()
        AND (e.effective_until IS NULL OR e.effective_until > NOW())
  );

-- rambler down

-- Partially irreversible by nature, and deliberately kept as small as possible: the only rows
-- deleted are legacy duplicates whose same-model twin was already pricing the model, so nothing
-- billable was lost and the seed migrations can re-create them. The re-keyed 'bedrock' cards are
-- left in place — they are the correct billing keys, and reverting them would restore $0 billing.
SELECT 1;
