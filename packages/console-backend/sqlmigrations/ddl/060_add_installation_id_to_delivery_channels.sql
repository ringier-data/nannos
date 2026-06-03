-- rambler up

-- Add a stable client-supplied identifier to enable idempotent self-registration
-- by bot clients (one delivery channel per tenant: Slack workspace / GChat project).
ALTER TABLE delivery_channels
    ADD COLUMN installation_id TEXT NULL;

CREATE UNIQUE INDEX delivery_channels_client_installation_uidx
    ON delivery_channels (client_id, installation_id)
    WHERE installation_id IS NOT NULL;

-- rambler down

DROP INDEX IF EXISTS delivery_channels_client_installation_uidx;
ALTER TABLE delivery_channels DROP COLUMN IF EXISTS installation_id;
