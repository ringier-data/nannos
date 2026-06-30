-- rambler up

-- Deleting a delivery channel must not be blocked by scheduled jobs that still
-- reference it. A channel deletion now nulls out the reference instead: the job
-- survives and simply runs without push delivery (the scheduler engine already
-- tolerates a NULL delivery_channel_id — see scheduler_engine._build_dispatch).
-- Change the FK from ON DELETE RESTRICT to ON DELETE SET NULL.

ALTER TABLE scheduled_jobs
    DROP CONSTRAINT scheduled_jobs_delivery_channel_id_fkey,
    ADD CONSTRAINT scheduled_jobs_delivery_channel_id_fkey
        FOREIGN KEY (delivery_channel_id)
        REFERENCES delivery_channels(id)
        ON DELETE SET NULL;

-- rambler down

ALTER TABLE scheduled_jobs
    DROP CONSTRAINT scheduled_jobs_delivery_channel_id_fkey,
    ADD CONSTRAINT scheduled_jobs_delivery_channel_id_fkey
        FOREIGN KEY (delivery_channel_id)
        REFERENCES delivery_channels(id)
        ON DELETE RESTRICT;
