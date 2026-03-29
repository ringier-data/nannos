-- rambler up

-- Additional tables for A2A Email Client
-- Adds context tracking, pending requests, and in-flight task management

-- =============================================================================
-- Email Context Store
-- Maps email conversations (sender + subject) to A2A context IDs
-- =============================================================================
create table email_context (
    context_key text not null,                          -- Normalized key: {senderEmail}:{normalizedSubject}
    context_id text not null,                           -- A2A context ID for conversation continuity
    task_id text,                                       -- Last A2A task ID
    subject text,                                       -- Original email subject
    sender_email text not null,                         -- Sender email address
    original_message_id text,                           -- Email Message-ID for threading replies
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now()),
    primary key (context_key)
);

create trigger set_updated_at before update on email_context for each row execute procedure trigger_set_updated_at();
create index idx_email_context_sender on email_context(sender_email);

-- =============================================================================
-- Pending Requests
-- Stores inbound email data while user completes OAuth authorization
-- =============================================================================
create table pending_request (
    email text not null,                                -- Sender email (PK)
    subject text,                                       -- Email subject
    body_text text,                                     -- Plain text body
    original_message_id text,                           -- Email Message-ID for reply threading
    attachment_keys text[],                             -- S3 keys of uploaded attachments (if any)
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now()),
    expires_at timestamptz not null default (now() + interval '7 days'),
    primary key (email)
);

create trigger set_updated_at before update on pending_request for each row execute procedure trigger_set_updated_at();
create index idx_pending_request_expires_at on pending_request(expires_at);

-- =============================================================================
-- In-Flight Tasks
-- Tracks async A2A tasks awaiting webhook callback
-- =============================================================================
create table inflight_task (
    task_id text not null,                              -- A2A task ID (PK)
    context_key text not null,                          -- Context store key for conversation continuity
    context_id text,                                    -- A2A context ID
    sender_email text not null,                         -- Email address to reply to
    subject text,                                       -- Email subject for reply
    original_message_id text,                           -- Email Message-ID for In-Reply-To header
    webhook_token text,                                 -- Token for validating A2A push notifications
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now()),
    expires_at timestamptz not null default (now() + interval '1 hour'),
    primary key (task_id)
);

create trigger set_updated_at before update on inflight_task for each row execute procedure trigger_set_updated_at();
create index idx_inflight_task_sender on inflight_task(sender_email);
create index idx_inflight_task_expires_at on inflight_task(expires_at);

-- =============================================================================
-- Add cleanup for new tables to existing cleanup function
-- =============================================================================
create or replace function cleanup_expired_records()
returns void as $$
begin
    delete from oauth_state where expires_at < now();
    delete from pending_request where expires_at < now();
    delete from inflight_task where expires_at < now();
end;
$$ language plpgsql;

-- Comments
comment on table email_context is 'Maps email conversations (sender + subject) to A2A context IDs';
comment on table pending_request is 'Stores inbound email data while user completes OAuth authorization';
comment on table inflight_task is 'Tracks async A2A tasks awaiting webhook callback from A2A server';

-- rambler down
drop table if exists inflight_task;
drop table if exists pending_request;
drop table if exists email_context;
