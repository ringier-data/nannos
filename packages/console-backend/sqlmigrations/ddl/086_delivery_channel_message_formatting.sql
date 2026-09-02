-- rambler up

-- How the channel renders the text it delivers. Nothing between the agent and the
-- user rewrites its output, so a Slack channel handed GitHub-flavoured Markdown
-- shows literal '###' and '**bold**' — which is exactly what scheduled-job
-- notifications looked like until the writer was told the channel's rules.
--
-- It belongs on the channel, not the job: the renderer is a property of where the
-- message lands, and one sub-agent may notify Slack for one job and the web console
-- for the next. Clients declare it when they self-register (Slack → 'slack',
-- Google Chat → 'google-chat', email → 'markdown'); the scheduler puts it in the
-- dispatch metadata under the same key an interactive client sends, so the writer
-- obeys the same rules on both paths.
--
-- Default 'markdown' keeps every existing row (and any client that has not been
-- taught the field) on today's behaviour.
--
-- No backfill: existing rows are corrected by the clients themselves, which declare
-- their format on every boot's self-registration. Guessing from client_id would be
-- wrong as often as right — it is operator-configured (this repo's own example
-- deployment gives the Slack client OIDC_CLIENT_ID "email-client"), so a pattern
-- match can both miss a Slack channel and relabel something else as one.
ALTER TABLE delivery_channels
    ADD COLUMN message_formatting TEXT NOT NULL DEFAULT 'markdown';

ALTER TABLE delivery_channels
    ADD CONSTRAINT delivery_channels_message_formatting_chk
    CHECK (message_formatting IN ('markdown', 'slack', 'google-chat', 'plain'));

-- rambler down

ALTER TABLE delivery_channels DROP CONSTRAINT IF EXISTS delivery_channels_message_formatting_chk;
ALTER TABLE delivery_channels DROP COLUMN IF EXISTS message_formatting;
