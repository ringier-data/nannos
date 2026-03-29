-- rambler up

-- =============================================================================
-- Bot Installations
-- One row per registered Slack App persona. Multiple bots may share the same
-- team_id (workspace). Runtime routing uses app_id (Slack App ID), extracted
-- from api_app_id in every Slack request payload.
-- =============================================================================
CREATE TABLE bot_installations (
    app_id          TEXT        NOT NULL PRIMARY KEY,  -- Slack App ID
    team_id         TEXT        NOT NULL,               -- Slack workspace/team ID
    bot_token       TEXT        NOT NULL,               -- xoxb-... Slack bot token
    signing_secret  TEXT        NOT NULL,               -- Slack signing secret
    bot_name        TEXT        NOT NULL,               -- Display name (e.g. "Nannos")
    avatar_url      TEXT,                               -- Optional bot avatar URL
    slash_command   TEXT        NOT NULL,               -- e.g. '/nannos', '/mybot'
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX bot_installations_team_id_idx ON bot_installations (team_id);

CREATE TRIGGER set_updated_at
  BEFORE UPDATE ON bot_installations
  FOR EACH ROW
  EXECUTE PROCEDURE trigger_set_updated_at();

-- =============================================================================
-- Schema evolution: add app_id to inflight_tasks and pending_requests
-- Allows webhook callbacks and pending-request resumption to identify which
-- bot persona handled the original message, enabling correct token lookup
-- when a workspace has multiple bots.
-- Both columns are nullable for backward compatibility with existing rows.
-- =============================================================================
ALTER TABLE inflight_tasks
    ADD COLUMN IF NOT EXISTS app_id TEXT;

ALTER TABLE pending_requests
    ADD COLUMN IF NOT EXISTS app_id TEXT;
