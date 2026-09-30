-- rambler up
-- Ordered failover chain per chat tier (nannos#204, ADR-0008).
--
-- A "tier group" is the ordered list of aliases serving one chat tier. Its HEAD is the
-- tier's default and already lives in model_defaults; this table holds only the TAIL — the
-- aliases the gateway falls back to, in order, when the head's provider is unavailable.
-- Storing the head here too would duplicate model_defaults and create a second writer for
-- the same fact, so changing a tier's default silently re-heads its chain instead.
--
-- Deliberately NOT model_alias_tiers: that table is a historical memory of which tiers an
-- alias has served as default, written as a side effect of setting a default. Overloading it
-- would turn an accidental, long-since-repointed default assignment into a live failover
-- route. Membership here is explicit admin intent and nothing else writes it.
--
-- Chat tiers only, enforced in the service layer as well: failing an embedding call over to
-- another model writes vectors from a different embedding space into the same pgvector index
-- (both sides are pinned to EMBEDDING_DIMENSION, so they insert cleanly) and silently
-- degrades similarity search for every document embedded during the outage, permanently.
CREATE TABLE model_tier_fallbacks (
    role TEXT NOT NULL,
    -- chat-tier role: 'chat' (standard), 'chat:low', 'chat:premium'
    alias TEXT NOT NULL,
    -- 1-based rank within the tier's chain; the head (rank 0) is model_defaults.model_alias
    position INTEGER NOT NULL CHECK (position >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (role, alias),
    CONSTRAINT model_tier_fallbacks_role_position_key UNIQUE (role, position)
        DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE model_tier_fallbacks IS 'Ordered failover chain per chat tier; the head of the chain is model_defaults. Projected onto the LiteLLM proxy via POST /fallback.';
-- rambler down
DROP TABLE IF EXISTS model_tier_fallbacks;
