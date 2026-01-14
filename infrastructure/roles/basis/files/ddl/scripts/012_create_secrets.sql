-- rambler up

-- Enum type for secret types
CREATE TYPE secret_type AS ENUM ('foundry_client_secret');

-- Secrets table for storing references to AWS SSM Parameter Store secrets
-- Stores metadata only - actual secrets are in SSM Parameter Store as SecureString
CREATE TABLE secrets (
    id SERIAL PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    -- Human-readable name for the secret
    description TEXT,
    -- Optional description of what this secret is for
    secret_type secret_type NOT NULL,
    -- SSM Parameter Store path (generated via SSM_VAULT_PREFIX + uuid)
    ssm_parameter_name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- Unique constraint for name per owner (excluding soft-deleted records)
CREATE UNIQUE INDEX idx_secrets_owner_name_unique ON secrets(owner_user_id, name)
WHERE deleted_at IS NULL;

-- Index for owner's secrets (excluding deleted)
CREATE INDEX idx_secrets_owner ON secrets(owner_user_id)
WHERE deleted_at IS NULL;

-- Index for looking up by SSM parameter name
CREATE INDEX idx_secrets_ssm_param ON secrets(ssm_parameter_name)
WHERE deleted_at IS NULL;

-- rambler down

DROP INDEX IF EXISTS idx_secrets_ssm_param;
DROP INDEX IF EXISTS idx_secrets_owner;
DROP INDEX IF EXISTS idx_secrets_owner_name_unique;
DROP TABLE IF EXISTS secrets;
DROP TYPE IF EXISTS secret_type;
