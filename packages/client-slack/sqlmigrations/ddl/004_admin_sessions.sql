-- rambler up

-- Admin sessions for V2 API cookie-based authentication
create table admin_sessions (
    session_id text not null primary key,
    sub text not null,                        -- OIDC subject identifier
    email text,                               -- User email from OIDC
    groups text[] not null default '{}',       -- OIDC group memberships
    access_token text not null,               -- OIDC access token
    refresh_token text,                       -- OIDC refresh token
    access_token_expires_at timestamptz not null,
    created_at timestamptz not null default (now()),
    expires_at timestamptz not null
);

create index idx_admin_sessions_expires_at on admin_sessions(expires_at);

-- rambler down
drop table if exists admin_sessions;
