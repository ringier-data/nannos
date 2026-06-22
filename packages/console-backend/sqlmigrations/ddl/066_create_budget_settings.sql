-- rambler up
-- Single-row, admin-editable Budget Guard configuration.
--
-- Replaces the orchestrator's env-var BUDGET_* knobs: the lock decision (monthly USD
-- spend vs limit) now lives here, next to the usage_logs source of truth. The
-- orchestrator polls GET /api/v1/admin/budget/status to decide whether to lock LLM
-- traffic, and admins edit these values from the console (no redeploy).
--
-- Enforced as a singleton via the `id IS TRUE` PRIMARY KEY + CHECK: there is exactly
-- one global budget (per the agreed scope), seeded below so reads always return a row.
CREATE TABLE budget_settings (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    -- Calendar-month spend ceiling in USD (summed over usage_logs.total_cost_usd).
    monthly_limit_usd NUMERIC(12, 2) NOT NULL DEFAULT 300.00,
    -- Fractions of the limit (0..1) at which to surface warnings before the hard lock.
    warning_thresholds JSONB NOT NULL DEFAULT '[0.8, 0.9, 0.95]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT budget_settings_singleton CHECK (id IS TRUE)
);

-- Seed the single config row so GET always returns defaults (admins then edit it).
INSERT INTO budget_settings (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING;
-- rambler down
DROP TABLE IF EXISTS budget_settings;
