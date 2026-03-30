-- rambler up

-- Allow usage_logs rows to be linked to the scheduled job that triggered the invocation.
-- Nullable: regular (non-scheduled) invocations leave this NULL.

ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS scheduled_job_id INTEGER
        REFERENCES scheduled_jobs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_usage_logs_scheduled_job
    ON usage_logs (scheduled_job_id)
    WHERE scheduled_job_id IS NOT NULL;

-- rambler down

DROP INDEX IF EXISTS idx_usage_logs_scheduled_job;

ALTER TABLE usage_logs
    DROP COLUMN IF EXISTS scheduled_job_id;
