-- rambler up
-- Add 'auth_required' to job_run_status: a run that stopped because a tool needs
-- the OWNER's credential, which nothing in the runtime can supply. Recorded until
-- now as whatever the model said — 'failed' (spending the job's max_failures
-- budget on a condition no retry can fix, and eventually auto-pausing it) or
-- 'success' (a green run that did nothing). Neither asks the owner for the
-- credential, so the job could never recover on its own.
--
-- Neutral about the job like 'interrupted': no consecutive_failures, no
-- auto-pause. Unlike 'interrupted' it earns no retry — a credential does not
-- arrive on retry_at — and it HOLDS the job's schedule until it is answered.
-- See docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.
--
-- Kept in its own file so the forward-only enum change is isolated from the
-- reversible column work in 098, exactly as 092 was split from 093.
ALTER TYPE job_run_status
ADD VALUE IF NOT EXISTS 'auth_required';
-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
