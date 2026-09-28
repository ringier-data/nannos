-- rambler up
-- A broker binding is one brokered sign-in, as the client that redeemed it holds it
-- (ADR-0011 amendment 1). It carries what the bare broker_client_users link could not:
--
-- * A secret. /token used to mint for any user linked to the calling client, given the
--   client's own credentials and a subject, and a subject is not secret (the scheduler
--   sends it on every push). /redeem now hands the client a random secret, stored here
--   only as a SHA-256, and /token checks it, so a leaked client credential alone reaches
--   no one. Enforcement is per client (broker_clients.require_binding_secret): off for the
--   clients registered before this migration, until each one sends the secret.
--
-- * account_key: the client's own key for the row that holds this sign-in (a Slack user in
--   a team, an email address). A later sign-in into the same row replaces the binding and
--   its secret; two rows of one user never evict each other. A client that sends none is
--   keyed by the user.
--
-- * workspace_id: the client's account the sign-in belongs to (a Slack team, a Google Chat
--   project; '' for a client with one), which says where the user can be reached below.
ALTER TABLE broker_clients ADD COLUMN require_binding_secret BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE broker_bindings (
    id           BIGSERIAL PRIMARY KEY,
    client_id    TEXT NOT NULL REFERENCES broker_clients(client_id) ON DELETE CASCADE ON UPDATE CASCADE,
    account_key  TEXT NOT NULL,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL DEFAULT '',
    secret_hash  TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, account_key)
);
CREATE INDEX idx_broker_bindings_user ON broker_bindings(user_id);

-- Where a workspace's sign-ins can be reached: the installations its client runs there, in
-- the vocabulary its delivery channels are registered under (delivery_channels
-- .installation_id, scoped by client_id). Once per workspace, not per binding, because a
-- Slack sign-in is per team and reaches every app the client has in it, including one
-- installed after the user signed in. The client is its only writer: it publishes each
-- workspace whenever it registers its delivery channels (PUT /workspaces/{id}), so the list
-- is exactly as current as the channels. This is what tells the scheduler
-- whether a subscriber can receive on a channel (#192).
CREATE TABLE broker_workspaces (
    client_id         TEXT NOT NULL REFERENCES broker_clients(client_id) ON DELETE CASCADE ON UPDATE CASCADE,
    workspace_id      TEXT NOT NULL,
    installation_ids  TEXT[] NOT NULL DEFAULT '{}',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, workspace_id)
);
CREATE INDEX idx_broker_workspaces_installations ON broker_workspaces USING GIN (installation_ids);

-- rambler down
DROP TABLE IF EXISTS broker_workspaces;
DROP TABLE IF EXISTS broker_bindings;
ALTER TABLE broker_clients DROP COLUMN IF EXISTS require_binding_secret;
