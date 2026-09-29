-- rambler up
-- The channel a run was sent to (#191). A client's report that a run's notification
-- reached nobody is judged against it: the subscription's own channel is where it notifies
-- NOW, and a subscriber who moves the job while a run is in flight would otherwise have
-- that run's report refused as coming from the wrong client, leaving it recorded as
-- delivered. Copied from the subscription when the run is created. NULL for a run with no
-- channel, for one whose channel was deleted since, and for every run before this
-- migration; the report lookup falls back to the subscription's current channel for it.
ALTER TABLE scheduled_job_runs
    ADD COLUMN delivery_channel_id INTEGER REFERENCES delivery_channels(id) ON DELETE SET NULL;

-- rambler down
ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS delivery_channel_id;
