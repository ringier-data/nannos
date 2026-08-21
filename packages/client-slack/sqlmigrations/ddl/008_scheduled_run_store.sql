-- rambler up

-- Provenance of scheduled-run notifications delivered to Slack.
-- Keyed by the delivered message ({channelId}:{messageTs} — the DM channel id
-- plus message ts already uniquely identify the message, and using a single id
-- source on both the write and read path avoids team-id source mismatches
-- under Enterprise Grid), so a thread reply under a notification can be
-- correlated back to the scheduled job, run, and sub-agent conversation that
-- produced it. The run's A2A context_id is the sub-agent's own conversation
-- and is forwarded to the orchestrator as structured data only — never as the
-- request contextId.
--
-- Rows are only read by the first replies under a notification, so they expire:
-- cleanup_expired_records() removes them, and reads filter on expires_at.
create table scheduled_run_store (
    context_key text not null primary key,  -- {channelId}:{messageTs}
    context_id text not null,               -- A2A context id of the scheduled run (agent-runner conversation)
    scheduled_job_id bigint,
    scheduled_job_run_id bigint,
    sub_agent_id bigint,
    sub_agent_name text,
    prompt text,                            -- the prompt the run was dispatched with
    result_summary text,                    -- the agent output delivered to the user
    scheduler_status text,                  -- success | failed (condition_not_met is never delivered)
    error_message text,                     -- set when scheduler_status = 'failed'
    task_state text,                        -- terminal A2A task state of the run: completed | input_required | failed
                                            -- (input_required = the run asked the user a question and awaits the answer)
    expires_at timestamptz not null default (now() + interval '30 days'),
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now())
);

create index idx_scheduled_run_store_expires_at on scheduled_run_store (expires_at);

create trigger set_updated_at before update on scheduled_run_store for each row execute procedure trigger_set_updated_at();

create or replace function cleanup_expired_records()
returns void as $$
begin
    -- Clean up expired OAuth states
    delete from oauth_state where expires_at < now();

    -- Clean up expired in-flight tasks
    delete from inflight_tasks where expires_at < now();

    -- Clean up expired scheduled-run provenance
    delete from scheduled_run_store where expires_at < now();
end;
$$ language plpgsql;

comment on table scheduled_run_store is 'Provenance of delivered scheduled-run notifications, for correlating thread replies back to the job/run/sub-agent';

-- rambler down
drop table if exists scheduled_run_store;

create or replace function cleanup_expired_records()
returns void as $$
begin
    delete from oauth_state where expires_at < now();
    delete from inflight_tasks where expires_at < now();
end;
$$ language plpgsql;
