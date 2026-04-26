-- rambler up

-- Enum types for catalog system
CREATE TYPE catalog_source_type AS ENUM ('google_drive');
CREATE TYPE catalog_status AS ENUM ('active', 'syncing', 'error', 'disabled');
CREATE TYPE catalog_connection_status AS ENUM ('active', 'expired', 'revoked');
CREATE TYPE catalog_sync_job_status AS ENUM (
    'pending', 'running', 'reindexing', 'paused', 'cancelling',
    'completed', 'failed', 'cancelled'
);

-- Per-file sync progress so the UI can show queued/syncing/synced/skipped state.
CREATE TYPE catalog_file_sync_status AS ENUM ('pending', 'syncing', 'synced', 'failed', 'skipped');

-- Add 'catalog' to the audit entity type enum
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'catalog';

-- Main catalog table
CREATE TABLE catalogs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type catalog_source_type NOT NULL,
    source_config JSONB NOT NULL DEFAULT '{}',
    status catalog_status NOT NULL DEFAULT 'active',
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_catalogs_owner ON catalogs(owner_user_id);

-- Group-based catalog access control (same pattern as sub_agent_permissions)
CREATE TABLE catalog_permissions (
    id SERIAL PRIMARY KEY,
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
    user_group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    permissions TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalog_id, user_group_id),
    CHECK (
        permissions <@ ARRAY['read', 'write']::TEXT[]
        AND array_length(permissions, 1) > 0
    )
);

CREATE INDEX idx_catalog_permissions_lookup ON catalog_permissions(catalog_id, user_group_id);
CREATE INDEX idx_catalog_permissions_group ON catalog_permissions(user_group_id);

-- OAuth connection for catalog source (one per catalog)
CREATE TABLE catalog_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL UNIQUE REFERENCES catalogs(id) ON DELETE CASCADE,
    connector_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    encrypted_token BYTEA NOT NULL,
    token_expiry TIMESTAMPTZ,
    scopes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status catalog_connection_status NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Document-level metadata (intermediate between catalog and pages)
CREATE TABLE catalog_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL,
    source_file_name TEXT NOT NULL,
    mime_type TEXT,
    folder_path TEXT,
    page_count INTEGER,
    summary TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    source_modified_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ,
    indexing_excluded BOOLEAN NOT NULL DEFAULT FALSE,
    sync_status catalog_file_sync_status NOT NULL DEFAULT 'synced',
    skip_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalog_id, source_file_id)
);

CREATE INDEX idx_catalog_files_catalog ON catalog_files(catalog_id);
CREATE INDEX idx_catalog_files_source ON catalog_files(catalog_id, source_file_id);

-- Sync job tracking
CREATE TABLE catalog_sync_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
    status catalog_sync_job_status NOT NULL DEFAULT 'pending',
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    error_details JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_catalog_sync_jobs_catalog ON catalog_sync_jobs(catalog_id);
CREATE INDEX idx_catalog_sync_jobs_status ON catalog_sync_jobs(catalog_id, status);

-- Page-level data (individual slides/pages)
CREATE TABLE catalog_pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
    file_id UUID NOT NULL REFERENCES catalog_files(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    title TEXT,
    text_content TEXT,
    speaker_notes TEXT,
    content_hash TEXT,
    thumbnail_s3_key TEXT,
    source_ref JSONB,
    metadata JSONB NOT NULL DEFAULT '{}',
    indexed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (file_id, page_number)
);

CREATE INDEX idx_catalog_pages_file ON catalog_pages(file_id);
CREATE INDEX idx_catalog_pages_catalog ON catalog_pages(catalog_id);

-- rambler down

DROP TABLE IF EXISTS catalog_pages;
DROP TABLE IF EXISTS catalog_sync_jobs;
DROP TABLE IF EXISTS catalog_files;
DROP TABLE IF EXISTS catalog_connections;
DROP TABLE IF EXISTS catalog_permissions;
DROP TABLE IF EXISTS catalogs;

DROP TYPE IF EXISTS catalog_file_sync_status;
DROP TYPE IF EXISTS catalog_sync_job_status;
DROP TYPE IF EXISTS catalog_connection_status;
DROP TYPE IF EXISTS catalog_status;
DROP TYPE IF EXISTS catalog_source_type;

-- Note: Cannot remove enum value from audit_entity_type in PostgreSQL
