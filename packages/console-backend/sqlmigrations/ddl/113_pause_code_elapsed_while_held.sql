-- rambler up
-- A held one-time job whose moment passed before it could be switched back on never ran
-- (#192). Its stop is ``elapsed_while_held``, not ``elapsed_on_inherit`` ("had already
-- run"). A new migration rather than an edit of 110, which databases may already have
-- applied.
ALTER TABLE scheduled_job_subscriptions DROP CONSTRAINT scheduled_job_subscriptions_pause_code_known;
ALTER TABLE scheduled_job_subscriptions
    ADD CONSTRAINT scheduled_job_subscriptions_pause_code_known CHECK (pause_code IN (
        'disabled_by_user', 'manually_paused', 'auto_paused', 'elapsed_on_subscribe',
        'elapsed_on_inherit', 'elapsed_while_held', 'agent_inaccessible', 'invalid_timezone',
        'condition_met_once', 'no_offline_token', 'awaiting_sign_in', 'sign_in_expired',
        'access_revoked', 'unreachable', 'undelivered', 'legacy'
    ));

-- rambler down
UPDATE scheduled_job_subscriptions SET pause_code = 'elapsed_on_inherit' WHERE pause_code = 'elapsed_while_held';
ALTER TABLE scheduled_job_subscriptions DROP CONSTRAINT scheduled_job_subscriptions_pause_code_known;
ALTER TABLE scheduled_job_subscriptions
    ADD CONSTRAINT scheduled_job_subscriptions_pause_code_known CHECK (pause_code IN (
        'disabled_by_user', 'manually_paused', 'auto_paused', 'elapsed_on_subscribe',
        'elapsed_on_inherit', 'agent_inaccessible', 'invalid_timezone', 'condition_met_once',
        'no_offline_token', 'awaiting_sign_in', 'sign_in_expired', 'access_revoked',
        'unreachable', 'undelivered', 'legacy'
    ));
