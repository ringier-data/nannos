-- rambler up
-- Why a run's notification reached nobody, as the receiving chat client reported it
-- (#191): 'no_recipient' (the client holds no sign-in for the subscriber there, which
-- only they can fix) or 'send_failed' (a recipient was found and posting failed). A code,
-- not the client's error text: what failed in detail is logged with the run's ids when
-- the report arrives, and nothing reads it back from here.
--
-- The client acknowledges the push before it looks the recipient up, so the report is a
-- separate call that arrives after, or even before, the run is finalised. ``delivered``
-- alone could not carry it: finalising writes ``delivered`` from the dispatch's point of
-- view, and a report that came first would be overwritten. A run with a delivery failure
-- is never recorded as delivered (complete_run keeps it false). The run's status is
-- untouched: the work succeeded or failed on its own terms.
ALTER TABLE scheduled_job_runs
    ADD COLUMN delivery_failure TEXT
        CONSTRAINT scheduled_job_runs_delivery_failure_known CHECK (delivery_failure IN ('no_recipient', 'send_failed'));

-- rambler down
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS delivery_failure;
