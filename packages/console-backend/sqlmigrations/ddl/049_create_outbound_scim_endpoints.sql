-- rambler up

-- Add outbound_scim_endpoint to audit entity types
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'outbound_scim_endpoint';

-- Create outbound SCIM endpoints table for push provisioning
CREATE TABLE outbound_scim_endpoints (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    bearer_token TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    push_users BOOLEAN NOT NULL DEFAULT true,
    push_groups BOOLEAN NOT NULL DEFAULT true,
    created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- Only one active endpoint per name
CREATE UNIQUE INDEX idx_outbound_scim_endpoints_name_active
    ON outbound_scim_endpoints(name) WHERE deleted_at IS NULL;

-- Fast lookup for active endpoints during push
CREATE INDEX idx_outbound_scim_endpoints_active
    ON outbound_scim_endpoints(enabled, deleted_at) WHERE deleted_at IS NULL AND enabled = true;

-- Tracking table for sync state per entity per endpoint
CREATE TABLE outbound_scim_sync_state (
    id SERIAL PRIMARY KEY,
    endpoint_id INTEGER NOT NULL REFERENCES outbound_scim_endpoints(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('user', 'group')),
    entity_id TEXT NOT NULL,
    remote_id TEXT,
    last_synced_at TIMESTAMPTZ,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One sync state entry per entity per endpoint
CREATE UNIQUE INDEX idx_outbound_scim_sync_state_unique
    ON outbound_scim_sync_state(endpoint_id, entity_type, entity_id);

-- Fast lookup for pending retries
CREATE INDEX idx_outbound_scim_sync_state_errors
    ON outbound_scim_sync_state(endpoint_id, entity_type)
    WHERE last_error IS NOT NULL;

-- rambler down

DROP TABLE IF EXISTS outbound_scim_sync_state;
DROP TABLE IF EXISTS outbound_scim_endpoints;
