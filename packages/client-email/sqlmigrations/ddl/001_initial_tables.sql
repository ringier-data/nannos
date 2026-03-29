-- rambler up

-- Initial tables for A2A Email Client storage
-- This migration creates all tables needed for the PostgresStorageProvider


create or replace function trigger_set_updated_at()
  returns trigger as
$$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- =============================================================================
-- User Auth Storage
-- Stores OIDC authentication tokens for Slack users
-- =============================================================================
create table user_auth (
    email text not null,          -- User email
    access_token text not null,            -- OIDC access token
    refresh_token text,                    -- OIDC refresh token
    expires_at timestamptz not null,            -- Unix timestamp (ms) when token expires
    token_type text not null,       -- Usually "Bearer"
    scope text,                            -- Granted scopes
    id_token text,                         -- OIDC ID token
    created_at timestamptz not null default (now()),            -- Unix timestamp (ms) when created
    updated_at timestamptz not null default (now()),            -- Unix timestamp (ms) when last updated
    primary key (email)
);


create trigger set_updated_at before update on user_auth for each row execute procedure trigger_set_updated_at();

-- =============================================================================
-- OAuth State
-- Stores PKCE state during OAuth authorization flow
-- =============================================================================
create table  oauth_state (
    state text not null primary key,       -- Random state parameter
    email text not null,                  -- Slack user ID
    team_id text not null,                  -- Slack team/workspace ID
    code_verifier text not null,           -- PKCE code verifier
    expires_at timestamptz not null,                    -- Unix timestamp (ms) when state expires
    created_at timestamptz not null default (now()),                   -- Unix timestamp (ms)
    updated_at timestamptz not null default (now())
);
create trigger set_updated_at before update on oauth_state for each row execute procedure trigger_set_updated_at();

create index  idx_oauth_state_expires_at on oauth_state(expires_at);

-- =============================================================================
-- Cleanup function for expired records (optional - can be run via cron/scheduler)
-- =============================================================================
create or replace function cleanup_expired_records()
returns void as $$
begin
    -- Clean up expired OAuth states
    delete from oauth_state where expires_at < now();
    
end;
$$ language plpgsql;

-- Comment on tables for documentation
comment on table user_auth is 'Stores OIDC authentication tokens for Slack users';
comment on table oauth_state is 'Stores PKCE state during OAuth authorization flow';

-- rambler down
