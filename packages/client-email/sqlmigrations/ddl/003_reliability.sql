-- rambler up

-- =============================================================================
-- Processed Email Tracking (idempotency / deduplication)
-- Prevents duplicate processing when SNS delivers the same notification twice
-- =============================================================================
create table processed_email (
    s3_object_key text not null,                        -- S3 key of the raw inbound email (unique per email)
    sns_message_id text,                                -- SNS MessageId (for diagnostics)
    status text not null default 'processing',          -- processing | completed | failed
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now()),
    primary key (s3_object_key)
);

create trigger set_updated_at before update on processed_email for each row execute procedure trigger_set_updated_at();
create index idx_processed_email_status on processed_email(status);

-- =============================================================================
-- Add status column to pending_request for claim-then-delete pattern
-- Prevents data loss if app crashes between consuming and processing
-- =============================================================================
alter table pending_request add column status text not null default 'pending';
-- Values: 'pending' (awaiting auth), 'processing' (claimed, being processed)

-- =============================================================================
-- Add s3_object_key to inflight_task for diagnostics
-- =============================================================================
alter table inflight_task add column s3_object_key text;

-- =============================================================================
-- Update cleanup function to include processed_email (keep 24h for dedup)
-- =============================================================================
create or replace function cleanup_expired_records()
returns void as $$
begin
    delete from oauth_state where expires_at < now();
    delete from pending_request where expires_at < now();
    delete from inflight_task where expires_at < now();
    delete from processed_email where created_at < now() - interval '24 hours';
end;
$$ language plpgsql;

-- Comments
comment on table processed_email is 'Idempotency table: tracks processed inbound emails to prevent duplicate processing on SNS retries';
comment on column pending_request.status is 'pending = awaiting auth, processing = claimed for processing after auth';
comment on column inflight_task.s3_object_key is 'S3 key of the original inbound email, for diagnostics';

-- rambler down
alter table inflight_task drop column if exists s3_object_key;
alter table pending_request drop column if exists status;
drop table if exists processed_email;

-- Restore original cleanup function
create or replace function cleanup_expired_records()
returns void as $$
begin
    delete from oauth_state where expires_at < now();
    delete from pending_request where expires_at < now();
    delete from inflight_task where expires_at < now();
end;
$$ language plpgsql;
