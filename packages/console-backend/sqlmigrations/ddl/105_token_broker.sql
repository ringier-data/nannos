-- rambler up
-- Token broker. A registered client (a chat client or the cockpit BFF) sends the user's
-- browser to console-backend, which runs the Keycloak login as agent-console, vaults the
-- offline token in user_offline_tokens, and hands the client a one-time code. The client
-- redeems the code for the user's identity, and later asks for audience-scoped access
-- tokens minted from the vault. One offline token per user, one holder.

-- Registered broker clients are admin data, audited like delivery channels.
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'broker_client';

-- Which Keycloak clients may use the broker. client_id is the `azp` of the client's own
-- client-credentials token, which is how /redeem and /token recognise the caller.
-- `id` is the surrogate key the audited base repository updates and deletes by.
CREATE TABLE broker_clients (
    id             SERIAL PRIMARY KEY,
    client_id      TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    description    TEXT,
    -- Where /authorize may send the browser back to. Exact match, except that the first
    -- host label may be a `*` pattern (cockpit PR previews).
    redirect_uris  TEXT[] NOT NULL DEFAULT '{}',
    -- Which audiences /token may mint for this client.
    audiences      TEXT[] NOT NULL DEFAULT '{}',
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_by     TEXT NOT NULL REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per brokered login. Created by /authorize (pending), completed by /callback
-- (code issued), consumed by /redeem (single use). Auth-flow state like `sessions`, not
-- business data, so it is not audited. State and code are stored as SHA-256 hashes: the
-- table never holds a value that could be replayed.
CREATE TABLE broker_login_requests (
    state_hash       TEXT PRIMARY KEY,
    client_id        TEXT NOT NULL REFERENCES broker_clients(client_id) ON DELETE CASCADE ON UPDATE CASCADE,
    redirect_uri     TEXT NOT NULL,
    client_state     TEXT,
    code_hash        TEXT UNIQUE,
    user_id          TEXT REFERENCES users(id) ON DELETE CASCADE,
    identity         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL,
    code_expires_at  TIMESTAMPTZ,
    redeemed_at      TIMESTAMPTZ
);
CREATE INDEX idx_broker_login_requests_open_code
    ON broker_login_requests(code_hash) WHERE redeemed_at IS NULL;
CREATE INDEX idx_broker_login_requests_expires ON broker_login_requests(expires_at);

-- Which users signed in through which broker client, written when the client redeems its
-- code. /token mints only for these users, so one client's credentials reach the people
-- who signed in through that client, not everyone with a vaulted token.
CREATE TABLE broker_client_users (
    client_id   TEXT NOT NULL REFERENCES broker_clients(client_id) ON DELETE CASCADE ON UPDATE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, user_id)
);
CREATE INDEX idx_broker_client_users_user ON broker_client_users(user_id);

-- rambler down
DROP TABLE IF EXISTS broker_client_users;
DROP TABLE IF EXISTS broker_login_requests;
DROP TABLE IF EXISTS broker_clients;
-- 'broker_client' cannot be removed from audit_entity_type (Postgres has no DROP VALUE);
-- harmless to keep.
