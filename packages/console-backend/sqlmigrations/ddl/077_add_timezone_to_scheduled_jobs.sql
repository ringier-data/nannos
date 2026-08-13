-- rambler up

-- Cron expressions were historically evaluated in UTC, so a user asking for
-- "8am" got 8am UTC (= 10am Zurich in summer). Store an IANA timezone on each
-- job and evaluate cron wall-clock times in it. NULL means "use the deployment
-- default": it is resolved at evaluation time from the DEFAULT_TIMEZONE env
-- var, which SQL cannot read and which must stay the single source of the
-- system default (never hardcode a locale here).
ALTER TABLE scheduled_jobs ADD COLUMN timezone TEXT;

-- Existing jobs adopt their owner's settings timezone: stored expressions were
-- authored as the user's local wall-clock ("0 8 * * *" meaning 8am local), so
-- re-interpreting them locally fixes the offset rather than shifting intent.
-- Users without a (non-empty) settings row keep NULL and follow the deployment
-- default. Stored next_run_at values are not recomputable here (croniter is
-- Python); each job self-corrects after its next fire, which is acceptable.
UPDATE scheduled_jobs
SET timezone = us.timezone
FROM user_settings us
WHERE us.user_id = scheduled_jobs.user_id
  AND us.timezone IS NOT NULL
  AND us.timezone <> '';

-- rambler down

ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS timezone;
