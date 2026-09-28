-- rambler up
-- Why a subscription is switched off, as a code (#192). ``paused_reason`` was free text,
-- and it was also the state: releases found held rows by its exact wording, so rewording
-- one stranded every row already held, and the claim read "no reason" as "nobody stopped
-- it". The code now carries the state and the sentence a person reads is rendered from it
-- by the application (console_backend.models.scheduled_job.render_pause), so the wording
-- is free to change. ``pause_detail`` holds the values a sentence names (the failure
-- count of an auto-pause, the timezone that no longer resolves).
--
-- A switched-on subscription never has a code; the constraint makes a write that forgets
-- to clear it fail loudly instead of leaving a stop that the claim or a release would act
-- on. A switched-off one with no code was retired by its own schedule (a one-shot ran).
ALTER TABLE scheduled_job_subscriptions
    ADD COLUMN pause_code TEXT
        CONSTRAINT scheduled_job_subscriptions_pause_code_known CHECK (pause_code IN (
            'disabled_by_user', 'manually_paused', 'auto_paused', 'elapsed_on_subscribe',
            'elapsed_on_inherit', 'agent_inaccessible', 'invalid_timezone', 'condition_met_once',
            'no_offline_token', 'awaiting_sign_in', 'sign_in_expired', 'access_revoked',
            'unreachable', 'undelivered', 'legacy'
        )),
    ADD COLUMN pause_detail JSONB;

-- Every text the code has written, by its exact wording; anything else is kept verbatim.
-- backfill:pause-codes
UPDATE scheduled_job_subscriptions SET pause_code = CASE paused_reason
        WHEN 'Disabled by user' THEN 'disabled_by_user'
        WHEN 'Manually paused' THEN 'manually_paused'
        WHEN 'This one-time job already ran before you subscribed' THEN 'elapsed_on_subscribe'
        WHEN 'This one-time job had already run when this schedule took effect' THEN 'elapsed_on_inherit'
        WHEN 'Agent not accessible: you no longer have access to the sub-agent this job runs.' THEN 'agent_inaccessible'
        WHEN 'Watch condition met (one-time trigger)' THEN 'condition_met_once'
        WHEN 'No offline token stored. User must re-grant scheduler consent.' THEN 'no_offline_token'
        WHEN 'Waiting for your first sign-in to Nannos, so it can run under your account' THEN 'awaiting_sign_in'
        WHEN 'Your sign-in to Nannos has expired; sign in again so it can run under your account' THEN 'sign_in_expired'
        WHEN 'Access to this shared job was revoked' THEN 'access_revoked'
    END
    WHERE paused_reason IS NOT NULL AND NOT enabled;
UPDATE scheduled_job_subscriptions
    SET pause_code = 'auto_paused',
        pause_detail = jsonb_build_object(
            'max_failures', substring(paused_reason FROM '^Auto-paused after ([0-9]+) consecutive failures$')::int
        )
    WHERE pause_code IS NULL AND NOT enabled AND paused_reason ~ '^Auto-paused after [0-9]+ consecutive failures$';
UPDATE scheduled_job_subscriptions
    SET pause_code = 'invalid_timezone',
        pause_detail = jsonb_build_object(
            'timezone', substring(paused_reason FROM '^Invalid timezone ''(.*)'' — fix the job''s timezone and resume it\.$')
        )
    WHERE pause_code IS NULL AND NOT enabled
      AND paused_reason ~ '^Invalid timezone ''.*'' — fix the job''s timezone and resume it\.$';
UPDATE scheduled_job_subscriptions
    SET pause_code = 'legacy', pause_detail = jsonb_build_object('text', paused_reason)
    WHERE pause_code IS NULL AND NOT enabled AND paused_reason IS NOT NULL;
-- end backfill:pause-codes

-- A reason on a switched-on row described no stop and is dropped with the column; the
-- only thing it did was keep the claim's retry branch off an enabled job.
ALTER TABLE scheduled_job_subscriptions
    ADD CONSTRAINT scheduled_job_subscriptions_pause_is_off CHECK (pause_code IS NULL OR NOT enabled);
ALTER TABLE scheduled_job_subscriptions DROP COLUMN paused_reason;

-- Releases look a user's held rows up at every sign-in; few rows are ever held.
CREATE INDEX idx_scheduled_job_subscriptions_pause_code
    ON scheduled_job_subscriptions (user_id, pause_code) WHERE pause_code IS NOT NULL AND deleted_at IS NULL;

-- rambler down
DROP INDEX IF EXISTS idx_scheduled_job_subscriptions_pause_code;
ALTER TABLE scheduled_job_subscriptions ADD COLUMN paused_reason TEXT;
UPDATE scheduled_job_subscriptions SET paused_reason = CASE pause_code
        WHEN 'disabled_by_user' THEN 'Disabled by user'
        WHEN 'manually_paused' THEN 'Manually paused'
        WHEN 'auto_paused' THEN 'Auto-paused after ' || (pause_detail->>'max_failures') || ' consecutive failures'
        WHEN 'elapsed_on_subscribe' THEN 'This one-time job already ran before you subscribed'
        WHEN 'elapsed_on_inherit' THEN 'This one-time job had already run when this schedule took effect'
        WHEN 'agent_inaccessible' THEN 'Agent not accessible: you no longer have access to the sub-agent this job runs.'
        WHEN 'invalid_timezone' THEN 'Invalid timezone ''' || (pause_detail->>'timezone') || ''' — fix the job''s timezone and resume it.'
        WHEN 'condition_met_once' THEN 'Watch condition met (one-time trigger)'
        WHEN 'no_offline_token' THEN 'No offline token stored. User must re-grant scheduler consent.'
        WHEN 'awaiting_sign_in' THEN 'Waiting for your first sign-in to Nannos, so it can run under your account'
        WHEN 'sign_in_expired' THEN 'Your sign-in to Nannos has expired; sign in again so it can run under your account'
        WHEN 'access_revoked' THEN 'Access to this shared job was revoked'
        WHEN 'unreachable' THEN 'Nannos can''t reach you on this job''s delivery channel. Message Nannos there once to activate it, and the job switches back on'
        WHEN 'undelivered' THEN 'Nannos couldn''t reach you on this job''s delivery channel. Message Nannos there once, then switch the job back on'
        WHEN 'legacy' THEN pause_detail->>'text'
    END
    WHERE pause_code IS NOT NULL;
ALTER TABLE scheduled_job_subscriptions DROP CONSTRAINT IF EXISTS scheduled_job_subscriptions_pause_is_off;
ALTER TABLE scheduled_job_subscriptions DROP COLUMN IF EXISTS pause_detail;
ALTER TABLE scheduled_job_subscriptions DROP COLUMN IF EXISTS pause_code;
