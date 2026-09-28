-- rambler up
-- A broker binding is one brokered sign-in, as the client that redeemed it holds it
-- (ADR-0011 amendment 1). It carries two things the bare broker_client_users link could not:
--
-- * A secret. /token used to mint for any user linked to the calling client, given the
--   client's own credentials and a subject, and a subject is not secret (the scheduler
--   sends it on every push). /redeem now hands the client a random secret, stored here
--   only as a SHA-256, and /token checks it, so a leaked client credential alone reaches
--   no one. Enforcement is per client (broker_clients.require_binding_secret): off for the
--   clients registered before this migration, until each one sends the secret.
--
-- * Where the user can be reached. installation_ids are the installations the client says
--   this sign-in covers, in the same vocabulary its delivery channels are registered under
--   (delivery_channels.installation_id, scoped by client_id). A Slack sign-in is per team,
--   so it covers every app the client has in that team. This is what tells the scheduler
--   whether a subscriber can receive on a channel (#192).
--
-- tenant_id is the client's own name for the account the sign-in belongs to (a Slack team,
-- a Google Chat project; '' for a client with one). A later sign-in for the same tenant
-- replaces the binding, secret and installations included, instead of adding a stale one.
ALTER TABLE broker_clients ADD COLUMN require_binding_secret BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE broker_bindings (
    id                BIGSERIAL PRIMARY KEY,
    client_id         TEXT NOT NULL REFERENCES broker_clients(client_id) ON DELETE CASCADE ON UPDATE CASCADE,
    user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id         TEXT NOT NULL DEFAULT '',
    secret_hash       TEXT NOT NULL UNIQUE,
    installation_ids  TEXT[] NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, user_id, tenant_id)
);
CREATE INDEX idx_broker_bindings_user ON broker_bindings(user_id);
CREATE INDEX idx_broker_bindings_installations ON broker_bindings USING GIN (installation_ids);

-- rambler down
DROP TABLE IF EXISTS broker_bindings;
ALTER TABLE broker_clients DROP COLUMN IF EXISTS require_binding_secret;
