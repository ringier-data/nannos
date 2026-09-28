-- rambler up
-- Why the scheduler holds a subscription switched off, as a code (#192). Until now the
-- releases found their rows by the exact wording of ``paused_reason``, the sentence the
-- subscriber reads, so rewording one stranded every row already held. ``paused_reason``
-- is display text again; code matches ``hold``.
--
--   awaiting_sign_in  no vaulted offline token yet; the first sign-in releases it
--   sign_in_expired   the vaulted token was refused; the next sign-in releases it
--   access_revoked    the grant behind a self-made subscription is gone; regaining it does
--   unreachable       the delivery channel cannot reach the subscriber; a sign-in there does
--   undelivered       a client reported no recipient for a subscriber Nannos cannot judge;
--                     the subscriber switches it back on
--
-- A held subscription is always switched off. Every write that switches one on or
-- rewrites its reason clears ``hold`` in the same statement; the constraint makes a path
-- that forgets fail loudly instead of leaving a hold that a later release would act on.
ALTER TABLE scheduled_job_subscriptions
    ADD COLUMN hold TEXT
        CONSTRAINT scheduled_job_subscriptions_hold_known
        CHECK (hold IN ('awaiting_sign_in', 'sign_in_expired', 'access_revoked', 'unreachable', 'undelivered'));

-- The holds that existed before this column, by the exact texts the code wrote.
-- backfill:holds
UPDATE scheduled_job_subscriptions SET hold = 'awaiting_sign_in'
    WHERE NOT enabled AND paused_reason = 'Waiting for your first sign-in to Nannos, so it can run under your account';
UPDATE scheduled_job_subscriptions SET hold = 'sign_in_expired'
    WHERE NOT enabled AND paused_reason = 'Your sign-in to Nannos has expired; sign in again so it can run under your account';
UPDATE scheduled_job_subscriptions SET hold = 'access_revoked'
    WHERE NOT enabled AND paused_reason = 'Access to this shared job was revoked';
-- end backfill:holds

ALTER TABLE scheduled_job_subscriptions
    ADD CONSTRAINT scheduled_job_subscriptions_hold_is_off CHECK (hold IS NULL OR NOT enabled);

-- Releases look a user's held rows up at every sign-in; few rows are ever held.
CREATE INDEX idx_scheduled_job_subscriptions_hold
    ON scheduled_job_subscriptions (user_id, hold) WHERE hold IS NOT NULL AND deleted_at IS NULL;

-- rambler down
DROP INDEX IF EXISTS idx_scheduled_job_subscriptions_hold;
ALTER TABLE scheduled_job_subscriptions DROP CONSTRAINT IF EXISTS scheduled_job_subscriptions_hold_is_off;
ALTER TABLE scheduled_job_subscriptions DROP COLUMN IF EXISTS hold;
