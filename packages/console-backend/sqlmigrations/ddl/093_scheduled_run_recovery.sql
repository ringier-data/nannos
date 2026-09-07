-- rambler up

-- Recovery for runs interrupted by a process death. See
-- docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md for the reasoning; this
-- header only states what each column means.

-- Last heartbeat from the dispatching process. The healer sweeps on staleness of
-- this, so a slow-but-healthy run is never mistaken for an abandoned one. NULL on
-- rows written before recovery existed; the healer gives those a much longer
-- age-based window, since a process on the previous release may still be running
-- them.
ALTER TABLE scheduled_job_runs
    ADD COLUMN last_seen_at TIMESTAMPTZ;

-- Why this run was started. 'scheduled' is an ordinary occurrence; 'retry' is the
-- one fresh attempt an interrupted scheduled run earns; 'manual' is a user's
-- run-now. Only a 'scheduled' run earns a retry, only a 'retry' run owes the user
-- a notice when it is lost, and a 'manual' run does neither.
ALTER TABLE scheduled_job_runs
    ADD COLUMN trigger TEXT NOT NULL DEFAULT 'scheduled'
        CONSTRAINT scheduled_job_runs_trigger_check
        CHECK (trigger IN ('scheduled', 'retry', 'manual'));

-- When the user is owed the notice that this run was lost for good. NULL means
-- nothing is owed, which is nearly every row, hence the partial index.
ALTER TABLE scheduled_job_runs
    ADD COLUMN notice_due_at TIMESTAMPTZ;

CREATE INDEX idx_scheduled_job_runs_notice_due
    ON scheduled_job_runs (notice_due_at)
    WHERE notice_due_at IS NOT NULL;

-- When a fresh attempt is owed. A second wake-up reason for claim_due_jobs, kept
-- out of next_run_at so the schedule users read is never rewritten by a retry.
ALTER TABLE scheduled_jobs
    ADD COLUMN retry_at TIMESTAMPTZ;

CREATE INDEX idx_scheduled_jobs_retry_at
    ON scheduled_jobs (retry_at)
    WHERE retry_at IS NOT NULL;

-- The set the healer and the claim guard scan: runs still in progress. Keyed on a
-- column the heartbeat never touches, so touch_run stays a HOT update.
CREATE INDEX idx_scheduled_job_runs_running
    ON scheduled_job_runs (job_id, started_at)
    WHERE status = 'running';

-- rambler down

DROP INDEX IF EXISTS idx_scheduled_job_runs_running;
DROP INDEX IF EXISTS idx_scheduled_jobs_retry_at;
ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS retry_at;
DROP INDEX IF EXISTS idx_scheduled_job_runs_notice_due;
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS notice_due_at;
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS trigger;
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS last_seen_at;
