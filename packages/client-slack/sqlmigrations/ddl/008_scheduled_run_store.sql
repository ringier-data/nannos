-- rambler up

-- Provenance of scheduled-run notifications delivered to Slack.
-- Keyed by the delivered message ({teamId}:{channelId}:{messageTs}), so a
-- thread reply under a notification can be correlated back to the scheduled
-- job, run, and sub-agent conversation that produced it. The run's A2A
-- context_id is the sub-agent's own conversation and is forwarded to the
-- orchestrator as structured data only — never as the request contextId.
create table scheduled_run_store (
    context_key text not null primary key,  -- {teamId}:{channelId}:{messageTs}
    context_id text not null,               -- A2A context id of the scheduled run (agent-runner conversation)
    scheduled_job_id bigint,
    scheduled_job_run_id bigint,
    sub_agent_id bigint,
    sub_agent_name text,
    prompt text,                            -- the prompt the run was dispatched with
    result_summary text,                    -- the agent output delivered to the user
    created_at timestamptz not null default (now()),
    updated_at timestamptz not null default (now())
);

create trigger set_updated_at before update on scheduled_run_store for each row execute procedure trigger_set_updated_at();

-- rambler down
drop table if exists scheduled_run_store;
