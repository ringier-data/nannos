-- rambler up

-- Add automated type to sub_agent_type enum
ALTER TYPE sub_agent_type ADD VALUE IF NOT EXISTS 'automated';

-- Add scheduled_job to audit entity type
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'scheduled_job';

-- New enums for scheduler
CREATE TYPE job_type AS ENUM ('task', 'watch');
CREATE TYPE schedule_kind AS ENUM ('cron', 'once', 'interval');
CREATE TYPE job_run_status AS ENUM ('running', 'success', 'failed', 'condition_not_met');

-- Scheduled jobs table
CREATE TABLE scheduled_jobs (
    id                      SERIAL PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name                    TEXT NOT NULL,
    job_type                job_type NOT NULL,
    schedule_kind           schedule_kind NOT NULL,
    cron_expr               TEXT,           -- for schedule_kind = 'cron'
    interval_seconds        INTEGER,        -- for schedule_kind = 'interval'
    run_at                  TIMESTAMPTZ,    -- for schedule_kind = 'once'
    next_run_at             TIMESTAMPTZ NOT NULL,
    last_run_at             TIMESTAMPTZ,
    -- Task-specific columns
    prompt                  TEXT,
    sub_agent_id            INTEGER REFERENCES sub_agents(id) ON DELETE RESTRICT,  -- nullable for watch-only jobs
    -- Watch-specific columns
    check_tool              TEXT,           -- MCP tool name to call for condition evaluation
    check_args              JSONB,          -- arguments passed to check_tool
    condition_expr          TEXT,           -- JSONPath expression evaluated against tool response
    expected_value          TEXT,
    llm_condition           TEXT,
    destroy_after_trigger   BOOLEAN NOT NULL DEFAULT TRUE,
    last_check_result       JSONB,          -- last tool response (for adaptive backoff / change detection)
    notification_message    TEXT,
    -- Delivery
    delivery_channel_id     INTEGER REFERENCES delivery_channels(id) ON DELETE RESTRICT,
    -- Execution control
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    max_failures            INTEGER NOT NULL DEFAULT 3,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    paused_reason           TEXT,
    -- Timestamps
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at              TIMESTAMPTZ

    -- Tasks must reference an agent; watches may be agent-less (static message delivery)
    CONSTRAINT scheduled_jobs_task_requires_agent
        CHECK ((job_type = 'task' AND sub_agent_id IS NOT NULL) OR job_type = 'watch'),

    -- Exactly one schedule kind config must be set
    CONSTRAINT scheduled_jobs_schedule_config
        CHECK (
            (schedule_kind = 'cron'     AND cron_expr IS NOT NULL AND interval_seconds IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'interval' AND interval_seconds IS NOT NULL AND cron_expr IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'once'     AND run_at IS NOT NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
        ),

    -- Watches must define a condition
    CONSTRAINT scheduled_jobs_watch_requires_check
        CHECK (job_type = 'task' OR (check_tool IS NOT NULL AND condition_expr IS NOT NULL))
);

CREATE INDEX idx_scheduled_jobs_next_run
    ON scheduled_jobs (next_run_at)
    WHERE enabled = TRUE;

CREATE INDEX idx_scheduled_jobs_user
    ON scheduled_jobs (user_id);

-- Per-execution audit log
CREATE TABLE scheduled_job_runs (
    id                  SERIAL PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    conversation_id     TEXT,
    status              job_run_status NOT NULL DEFAULT 'running',
    result_summary      TEXT,       -- truncated response or condition result
    error_message       TEXT,       -- populated on failure
    delivered           BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_scheduled_job_runs_job
    ON scheduled_job_runs (job_id, started_at DESC);

CREATE INDEX idx_scheduled_job_runs_conversation
    ON scheduled_job_runs (conversation_id)
    WHERE conversation_id IS NOT NULL;

-- rambler down

DROP INDEX IF EXISTS idx_scheduled_job_runs_conversation;

DROP INDEX IF EXISTS idx_scheduled_job_runs_job;
DROP TABLE IF EXISTS scheduled_job_runs;

DROP INDEX IF EXISTS idx_scheduled_jobs_user;
DROP INDEX IF EXISTS idx_scheduled_jobs_next_run;
DROP TABLE IF EXISTS scheduled_jobs;

DROP TYPE IF EXISTS job_run_status;
DROP TYPE IF EXISTS schedule_kind;
DROP TYPE IF EXISTS job_type;
