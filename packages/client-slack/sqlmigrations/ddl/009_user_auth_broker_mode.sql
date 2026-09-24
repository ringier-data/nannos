-- rambler up
-- Token broker: in broker mode console-backend runs the sign-in and keeps the user's
-- offline token, and this client keeps only who the Slack user is (oidc_sub). Tokens are
-- minted on demand, so a broker row holds none. Rows signed in the old way keep their
-- local tokens until they drain (auth_mode = 'local').
alter table user_auth
    alter column access_token drop not null,
    alter column expires_at drop not null,
    alter column token_type drop not null,
    add column auth_mode text not null default 'local'
        constraint user_auth_auth_mode_check check (auth_mode in ('local', 'broker'));

comment on column user_auth.auth_mode is
    'local: Keycloak tokens held here; broker: identity only (oidc_sub), tokens minted by the console-backend token broker';

-- rambler down
delete from user_auth where auth_mode = 'broker';
alter table user_auth
    drop column if exists auth_mode,
    alter column access_token set not null,
    alter column expires_at set not null,
    alter column token_type set not null;
