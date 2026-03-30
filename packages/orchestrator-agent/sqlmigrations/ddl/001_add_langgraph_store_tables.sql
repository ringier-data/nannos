-- rambler up
-- Create LangGraph store tables for document storage with semantic search
-- Supports AsyncPostgresStore with pgvector embeddings
-- Note: pgvector extension must be installed at provisioning time
--
-- IMPORTANT: This migration creates the base schema only.
-- LangGraph's AsyncPostgresStore.setup() will handle:
-- - Running incremental MIGRATIONS for the store table
-- - Running VECTOR_MIGRATIONS with runtime configuration (dims, vector_type, index_type, etc.)
-- - Creating vector indexes with proper parameters based on the application's index config
--
-- This ensures the vector schema matches the runtime configuration exactly.

-- Migration tracking tables (LangGraph uses these to track applied migrations)
CREATE TABLE IF NOT EXISTS store_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS vector_migrations (
    v INTEGER PRIMARY KEY
);

-- Main store table for key-value storage
-- 'prefix' represents the document's namespace
CREATE TABLE IF NOT EXISTS store (
    prefix TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE,
    ttl_minutes INTEGER,
    PRIMARY KEY (prefix, key)
);

-- Index for faster prefix lookups
CREATE INDEX IF NOT EXISTS store_prefix_idx ON store USING btree (prefix text_pattern_ops);

-- Index for efficient TTL sweeping
CREATE INDEX IF NOT EXISTS idx_store_expires_at ON store (expires_at)
WHERE expires_at IS NOT NULL;

-- Note: pgvector extension is installed in the public schema at provisioning time
-- (see rds-shared.yml). It is available via the user's search_path.

-- Store vectors table for semantic search
-- Note: The embedding column type (dimensions) and vector index will be created by
-- LangGraph's setup() method based on runtime configuration:
-- - dims: Embedding model dimensions (e.g., 1024 for Titan V2, 1536 for OpenAI)
-- - vector_type: 'vector' or 'halfvec'
-- - index_type: 'hnsw' or 'ivfflat' with proper parameters
-- - distance_type: 'cosine', 'l2', or 'inner_product'
--
-- This approach ensures the schema matches the application's embedding configuration.
CREATE TABLE IF NOT EXISTS store_vectors (
    prefix TEXT NOT NULL,
    key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    embedding vector(1024),  -- Matches current Titan Embeddings V2 config (1024 dims)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prefix, key, field_name),
    FOREIGN KEY (prefix, key) REFERENCES store(prefix, key) ON DELETE CASCADE
);

-- Note: Vector index creation is handled by LangGraph's setup() method
-- This ensures index parameters (type, ops, dimensions) match the runtime configuration
-- The index will be created as: store_vectors_embedding_idx on store_vectors(embedding)

-- rambler down
DROP TABLE IF EXISTS store_vectors;
DROP INDEX IF EXISTS idx_store_expires_at;
DROP INDEX IF EXISTS store_prefix_idx;
DROP TABLE IF EXISTS store;
DROP TABLE IF EXISTS vector_migrations;
DROP TABLE IF EXISTS store_migrations;
