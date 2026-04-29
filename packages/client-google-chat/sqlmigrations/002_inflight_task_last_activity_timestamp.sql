-- rambler up
alter table inflight_tasks
    add column last_activity_at timestamptz not null default now();

-- rambler down

alter table inflight_tasks
    drop column if exists last_activity_at;
