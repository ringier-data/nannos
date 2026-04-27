-- rambler up

-- Add catalog_id to usage_logs for cost attribution of catalog sync and search operations
ALTER TABLE usage_logs ADD COLUMN catalog_id UUID REFERENCES catalogs(id) ON DELETE SET NULL;

-- Index for querying costs per catalog
CREATE INDEX idx_usage_logs_catalog ON usage_logs (catalog_id, invoked_at DESC)
    WHERE catalog_id IS NOT NULL;

-- rambler down

DROP INDEX IF EXISTS idx_usage_logs_catalog;
ALTER TABLE usage_logs DROP COLUMN IF EXISTS catalog_id;
