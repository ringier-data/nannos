-- rambler up

-- Which service's own work an LLM call was, when the foreign keys don't imply it.
--
-- The usage views classify spend by service, and derived it from whichever id happened to be
-- set: scheduled_job_id meant 'scheduler', catalog_id without a conversation meant 'catalog',
-- and everything else fell through to 'orchestrator'. A console-backend utility call that
-- carries no id at all — naming a conversation, drafting a scheduled job, generating a watch
-- condition — therefore read as agent spend.
--
-- Nullable, and the derivation stays in place as the fallback: rows written before this
-- column existed, and every agent-path row (the orchestrator sets no service), keep
-- classifying exactly as they did.

ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS service TEXT;

COMMENT ON COLUMN usage_logs.service IS
    'Service whose own work this call was (console, scheduler, catalog, orchestrator). NULL → derived from scheduled_job_id / catalog_id.';

CREATE INDEX IF NOT EXISTS idx_usage_logs_service
    ON usage_logs (service)
    WHERE service IS NOT NULL;

-- rambler down

DROP INDEX IF EXISTS idx_usage_logs_service;

ALTER TABLE usage_logs
    DROP COLUMN IF EXISTS service;
