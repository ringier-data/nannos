-- rambler up
-- Per-role default model aliases (graceful degradation). When a sub-agent
-- (or the catalog indexer / docstore) references a model alias that is no longer
-- registered on the gateway, the apps fall back to the default for that role.
-- Authoritative + runtime-editable here because LiteLLM's /model/update can't persist
-- a custom default flag in the gateway model_info (only /model/new can).
CREATE TABLE IF NOT EXISTS model_defaults (
    role TEXT PRIMARY KEY,
    model_alias TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- rambler down
DROP TABLE IF EXISTS model_defaults;
