-- rambler up
-- Why a run's notification reached nobody, as the receiving chat client reported it
-- (#191). The client acknowledges the push before it looks the recipient up, so the
-- report is a separate call that arrives after, or even before, the run is finalised.
-- ``delivered`` alone could not carry it: finalising writes ``delivered`` from the
-- dispatch's point of view, and a report that came first would be overwritten. A run
-- with a delivery error is never recorded as delivered (complete_run keeps it false).
-- The run's status is untouched: the work succeeded or failed on its own terms.
ALTER TABLE scheduled_job_runs ADD COLUMN delivery_error TEXT;

-- rambler down
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS delivery_error;
