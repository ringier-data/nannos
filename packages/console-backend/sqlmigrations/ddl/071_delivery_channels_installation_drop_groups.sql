-- rambler up

-- Delivery channels are now resolved by a stable client-supplied installation_id
-- (one channel per tenant: Slack workspace / GChat project) instead of by user-group
-- mapping. Add the idempotency key and drop the group-association plumbing.

ALTER TABLE delivery_channels
    ADD COLUMN installation_id TEXT NULL;

CREATE UNIQUE INDEX delivery_channels_client_installation_uidx
    ON delivery_channels (client_id, installation_id)
    WHERE installation_id IS NOT NULL;

-- Channel visibility is no longer group-scoped; the association table is obsolete.
DROP TABLE IF EXISTS delivery_channel_groups;

-- rambler down

-- Recreate the group-association table (matches migration 033).
CREATE TABLE IF NOT EXISTS delivery_channel_groups (
    delivery_channel_id INTEGER NOT NULL REFERENCES delivery_channels(id) ON DELETE CASCADE,
    user_group_id       INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (delivery_channel_id, user_group_id)
);

DROP INDEX IF EXISTS delivery_channels_client_installation_uidx;
ALTER TABLE delivery_channels DROP COLUMN IF EXISTS installation_id;
