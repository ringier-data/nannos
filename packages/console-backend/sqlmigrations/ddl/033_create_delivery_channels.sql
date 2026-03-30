-- rambler up

-- Delivery channels: registered by A2A clients; used by the scheduler as push-notification targets
CREATE TABLE delivery_channels (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    webhook_url     TEXT NOT NULL,
    secret          TEXT NOT NULL,          -- sent verbatim as X-A2A-Notification-Token
    client_id       TEXT NOT NULL,          -- Keycloak azp claim from the registering client
    registered_by   TEXT NOT NULL,          -- OIDC sub of the token that registered this channel
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Each channel can be owned by one or more groups; users in those groups can see/use the channel
CREATE TABLE delivery_channel_groups (
    delivery_channel_id INTEGER NOT NULL REFERENCES delivery_channels(id) ON DELETE CASCADE,
    user_group_id       INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (delivery_channel_id, user_group_id)
);

-- Add delivery_channel to audit entity types
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'delivery_channel';

-- rambler down


DROP TABLE IF EXISTS delivery_channel_groups;
DROP TABLE IF EXISTS delivery_channels;
