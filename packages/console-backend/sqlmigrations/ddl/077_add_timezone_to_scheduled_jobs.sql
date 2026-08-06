-- rambler up

-- Cron expressions were historically evaluated in UTC, so a user asking for
-- "8am" got 8am UTC (= 10am Zurich in summer). Store an IANA timezone on each
-- job and evaluate cron wall-clock times in it.
ALTER TABLE scheduled_jobs ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Etc/UTC';

-- Existing jobs adopt their owner's settings timezone: stored expressions were
-- authored as the user's local wall-clock ("0 8 * * *" meaning 8am local), so
-- re-interpreting them locally fixes the offset rather than shifting intent.
UPDATE scheduled_jobs
SET timezone = COALESCE(
    (SELECT us.timezone FROM user_settings us WHERE us.user_id = scheduled_jobs.user_id),
    'Europe/Zurich'
);

-- rambler down

ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS timezone;
