-- rambler up

-- A scheduled run parked on an answer only its owner can give. See
-- docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md for the
-- reasoning; this header only states what each column means.
--
-- Named for the mechanism, not for today's only instance. A run parks because its
-- agent stopped on a question, and the run STATUS says which question
-- ('auth_required'). Only KIND_AUTH parks today — a scheduled run must not stop on
-- a tool approval nobody is there to give — but the task, the payload and the
-- resume path are all indifferent to the kind, and naming them 'auth_*' would mean
-- renaming two columns the day that changes.

-- The A2A task id of the PARKED agent-runner task. The run stops with that task
-- left non-terminal on purpose: answering is a message/send addressed to it, which
-- the A2A request handler accepts precisely because it has not reached a terminal
-- state. Nothing else can address it — the sub-agent's own task lives inside
-- agent-runner's in-process server and is derived from the run, never stored.
ALTER TABLE scheduled_job_runs
    ADD COLUMN parked_task_id TEXT;

-- The extension payload delivered with the ask — for an 'auth_required' run, the
-- in-task-auth AuthPayload.client_payload(). Stored rather than left to live only
-- inside the notification so the console can render the same ask from durable
-- state: an ask the owner can only answer somewhere else is an ask that waits.
-- Carries the service name, so no separate column is needed for it.
ALTER TABLE scheduled_job_runs
    ADD COLUMN parked_payload JSONB;

-- 'resumed' joins the trigger vocabulary: the run created when an answer continues
-- a parked one. The parked run is already terminal, so reopening it would
-- contradict a status the console showed. A resumed run behaves like a scheduled
-- one — one fresh attempt if interrupted, no effect on consecutive_failures — but
-- never advances next_run_at, which the parked run already did.
ALTER TABLE scheduled_job_runs
    DROP CONSTRAINT IF EXISTS scheduled_job_runs_trigger_check;
ALTER TABLE scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_trigger_check
    CHECK (trigger IN ('scheduled', 'retry', 'manual', 'resumed'));

-- The set claim_due_jobs scans to decide whether a job is blocked on its owner.
-- Mirrors idx_scheduled_job_runs_running: a parked run holds the job's schedule
-- exactly as a running one does, so the guard needs both lookups to be cheap.
CREATE INDEX idx_scheduled_job_runs_parked
    ON scheduled_job_runs (job_id, started_at)
    WHERE status = 'auth_required';

-- rambler down

DROP INDEX IF EXISTS idx_scheduled_job_runs_parked;
ALTER TABLE scheduled_job_runs
    DROP CONSTRAINT IF EXISTS scheduled_job_runs_trigger_check;
ALTER TABLE scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_trigger_check
    CHECK (trigger IN ('scheduled', 'retry', 'manual'));
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS parked_payload;
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS parked_task_id;
