-- rambler up
-- User status enum
CREATE TYPE user_status AS ENUM ('active', 'suspended', 'deleted');
-- Users table: stores user information from OIDC authentication
-- id is the OIDC 'sub' claim (subject identifier)
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    sub TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    company_name TEXT,
    is_administrator BOOLEAN NOT NULL DEFAULT FALSE,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'approver', 'admin')),
    status user_status NOT NULL DEFAULT 'active',
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Index for email lookups
CREATE INDEX idx_users_email ON users(email);
-- Index for role-based queries
CREATE INDEX idx_users_role ON users(role);
-- Index for active users (most common query pattern)
CREATE INDEX idx_users_active ON users(id)
WHERE status = 'active'
    AND deleted_at IS NULL;
-- rambler down
DROP INDEX IF EXISTS idx_users_active;
DROP INDEX IF EXISTS idx_users_role;
DROP INDEX IF EXISTS idx_users_email;
DROP TABLE IF EXISTS users;
DROP TYPE IF EXISTS user_status;
