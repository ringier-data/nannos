-- rambler up
delete from user_auth;

alter table user_auth
    add column oidc_sub text not null;

create index idx_user_auth_oidc_sub on user_auth(oidc_sub);

-- rambler down
drop index if exists idx_user_auth_oidc_sub;
alter table user_auth
    drop column if exists oidc_sub;
