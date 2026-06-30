-- rambler up

-- Per-installation notification secrets (cloud-agnostic default backend).
-- Used to authenticate inbound A2A push notifications. The 'db' installation
-- secret provider stores secrets here; the 'aws-ssm' provider stores them in
-- SSM Parameter Store instead and does not touch this table.
create table installation_secrets (
    installation_id text not null primary key,
    secret text not null,
    created_at timestamptz not null default (now())
);

-- rambler down
drop table if exists installation_secrets;
