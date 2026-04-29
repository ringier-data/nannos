-- rambler up

-- Initial tables for A2A Google Chat Client storage
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
-- Stores OIDC authentication tokens for Google Chat users
-- =============================================================================
create table user_auth (
    user_id text not null,                 -- Google Chat user resource name (users/XXXXX)
    project_id text not null,              -- Google chat project number
    access_token text not null,            -- OIDC access token
    refresh_token text,                    -- OIDC refresh token
    expires_at timestamptz not null,       -- When token expires
    token_type text not null,              -- Usually "Bearer"
    scope text,                            -- Granted scopes
    id_token text,                         -- OIDC ID token
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now()),
    primary key (user_id, project_id)
);


create trigger set_updated_at before update on user_auth for each row execute procedure trigger_set_updated_at();


-- =============================================================================
-- Context Store
-- Maps Google Chat threads to A2A context IDs for conversation continuity
-- =============================================================================
create table context_store (
    context_key text not null primary key,  -- Format: {projectId}:{spaceId}:{threadId}
    context_id text not null,               -- A2A context ID
    last_processed_message_id text,         -- Last processed message ID
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now())
);

create trigger set_updated_at before update on context_store for each row execute procedure trigger_set_updated_at();

-- =============================================================================
-- Pending Requests
-- Stores requests made before user has authorized (processed after OAuth)
-- =============================================================================
create table  pending_requests (
    visitor_id text not null primary key,   -- Format: {projectId}:{userId}
    text text not null,                     -- Message text
    space_id text not null,                 -- Google Chat space resource name
    thread_id text not null,                -- Thread resource name
    message_id text not null,               -- Message resource name
    source text not null,                   -- 'space_message' or 'direct_message'
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now())
);

create trigger set_updated_at before update on pending_requests for each row execute procedure trigger_set_updated_at();

-- =============================================================================
-- In-Flight Tasks
-- Stores context for active A2A tasks awaiting webhook callbacks
-- =============================================================================
create table  inflight_tasks (
    task_id text not null primary key,      -- A2A task ID
    visitor_id text not null,               -- Format: {projectId}:{userId}
    user_id text not null,                  -- Google Chat user resource name
    project_id text not null,               -- Google chat project number
    space_id text not null,                 -- Google Chat space resource name
    thread_id text not null,                -- Thread resource name
    message_id text not null,               -- Original message resource name
    status_message_id text,                 -- Status message resource name (for updates)
    context_key text not null,              -- Context store key
    webhook_token text,                     -- Token for validating A2A push notifications
    source text not null,                   -- 'space_message' or 'direct_message'
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now()),
    expires_at timestamptz                  -- For cleanup
);

create trigger set_updated_at before update on inflight_tasks for each row execute procedure trigger_set_updated_at();

create index  idx_inflight_tasks_visitor_id on inflight_tasks(visitor_id);
create index  idx_inflight_tasks_expires_at on inflight_tasks(expires_at);

-- =============================================================================
-- OAuth State
-- Stores PKCE state during OAuth authorization flow
-- =============================================================================
create table  oauth_state (
    state text not null primary key,       -- Random state parameter
    user_id text not null,                 -- Google Chat user resource name
    project_id text not null,              -- Google chat project number
    code_verifier text not null,           -- PKCE code verifier
    expires_at timestamptz not null,       -- When state expires
    created_at timestamptz not null default (now()),
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
    
    -- Clean up expired in-flight tasks
    delete from inflight_tasks where expires_at < now();
    

end;
$$ language plpgsql;

-- Comment on tables for documentation
comment on table user_auth is 'Stores OIDC authentication tokens for Google Chat users';
comment on table context_store is 'Maps Google Chat threads to A2A context IDs for conversation continuity';
comment on table pending_requests is 'Stores requests made before user has authorized';
comment on table inflight_tasks is 'Stores context for active A2A tasks awaiting webhook callbacks';
comment on table oauth_state is 'Stores PKCE state during OAuth authorization flow';

-- rambler down
