-- rambler up
-- Add 'interrupted' to job_run_status: a run that ended because the process
-- executing it died, rather than because the job failed. Recorded as 'failed'
-- until now, which let process churn write itself into user state — enough
-- restarts landing on the same job cross max_failures and auto-pause it with a
-- paused_reason blaming the job — and left the run history unable to answer
-- "did my job fail, or did the process die?" without reading pod logs.
--
-- Kept in its own file so the forward-only enum change is isolated from the
-- reversible column work in 092: this file's down-block is a no-op, 092's is not.
ALTER TYPE job_run_status
ADD VALUE IF NOT EXISTS 'interrupted';
-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
