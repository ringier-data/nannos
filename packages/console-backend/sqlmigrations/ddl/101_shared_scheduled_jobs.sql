-- rambler up
-- A scheduled job splits into a shareable DEFINITION and per-user SUBSCRIPTIONS. See
-- docs/adr/0010-shared-scheduled-jobs-run-once-per-subscriber.md for the reasoning;
-- this header only states what each table holds.
--
-- One scheduled_jobs row bundled five concerns: what the job does, when it fires,
-- whether it is on, WHOSE identity it runs under, and where its result goes. The fourth
-- cannot be shared — a run must never hold authority its recipient lacks — so sharing
-- the row was never an option. The definition keeps what the job IS (owned by one
-- user, shareable to groups with read/write exactly like a sub-agent); a subscription
-- keeps what is one user's (enabled, the trigger in force, delivery, run bookkeeping).
-- A definition never runs. Only subscriptions dispatch, each under its subscriber.
--
-- Ids are preserved: every pre-existing job becomes one definition and one
-- subscription of the same user, both carrying the old job id. The subscription id is
-- what users, clients and links have always called "the job id", so nothing keyed on
-- it — /app/scheduler/{id}, the scheduled_job_id in delivered notifications,
-- conversation adoption — has to change.

-- Whether a subscriber may change their own trigger after activation. A watch's tick
-- is part of what the watch means, so watches default to fixed and tasks to
-- overridable; an author can pin a task or relax a watch.
CREATE TYPE trigger_policy AS ENUM ('overridable', 'fixed');

-- ---------------------------------------------------------------------------
-- 1. Definitions: what the job is.
-- ---------------------------------------------------------------------------
CREATE TABLE scheduled_job_definitions (
    id                      SERIAL PRIMARY KEY,
    owner_user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name                    TEXT NOT NULL,
    job_type                job_type NOT NULL,
    -- Task: what the agent is told. Watch with an agent: what the agent is told when
    -- the condition is met.
    prompt                  TEXT,
    sub_agent_id            INTEGER REFERENCES sub_agents(id) ON DELETE RESTRICT,
    -- Watch: the check and the condition.
    check_tool              TEXT,
    check_args              JSONB,
    check_args_exprs        JSONB,
    cel_expr                TEXT,
    llm_condition           TEXT,
    destroy_after_trigger   BOOLEAN NOT NULL DEFAULT TRUE,
    notification_message    TEXT,
    voice_call              BOOLEAN NOT NULL DEFAULT FALSE,
    max_failures            INTEGER NOT NULL DEFAULT 3,
    -- Trigger DEFAULTS: what a subscription follows unless it overrides. A NULL
    -- timezone means the SUBSCRIBER's own (user_settings.timezone), so "0 9 * * 1-5"
    -- reads as 09:00 local for every member; an explicit zone pins it for all.
    schedule_kind           schedule_kind NOT NULL,
    cron_expr               TEXT,
    interval_seconds        INTEGER,
    run_at                  TIMESTAMPTZ,
    timezone                TEXT,
    trigger_policy          trigger_policy NOT NULL,
    -- A curated org-wide template, set by an admin only. Visible to everyone; runs
    -- for nobody until they subscribe or copy.
    is_public               BOOLEAN NOT NULL DEFAULT FALSE,
    -- Bumped on every definition-field edit and stamped on each run, so a run can say
    -- which definition produced it. Definitions are otherwise not versioned.
    revision                INTEGER NOT NULL DEFAULT 1,
    -- Definition-level stop: no subscription of it dispatches while set, but each
    -- subscription's own `enabled` is left as the member set it, so lifting the
    -- suspension restores their choices.
    suspended_at            TIMESTAMPTZ,
    suspended_by_user_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
    suspended_reason        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at              TIMESTAMPTZ,

    CONSTRAINT scheduled_job_definitions_task_requires_agent
        CHECK ((job_type = 'task' AND sub_agent_id IS NOT NULL) OR job_type = 'watch'),
    CONSTRAINT scheduled_job_definitions_schedule_config
        CHECK (
            (schedule_kind = 'cron'     AND cron_expr IS NOT NULL AND interval_seconds IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'interval' AND interval_seconds IS NOT NULL AND cron_expr IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'once'     AND run_at IS NOT NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
        ),
    CONSTRAINT scheduled_job_definitions_watch_requires_check
        CHECK (job_type = 'task' OR check_tool IS NOT NULL)
);

CREATE INDEX idx_scheduled_job_definitions_owner
    ON scheduled_job_definitions (owner_user_id);
CREATE INDEX idx_scheduled_job_definitions_agent
    ON scheduled_job_definitions (sub_agent_id)
    WHERE sub_agent_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Subscriptions: one user's activation of a definition.
-- ---------------------------------------------------------------------------
CREATE TABLE scheduled_job_subscriptions (
    id                      SERIAL PRIMARY KEY,
    definition_id           INTEGER NOT NULL REFERENCES scheduled_job_definitions(id) ON DELETE CASCADE,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- Provenance of the activation, as for sub-agent activations: the user
    -- subscribed themselves, a group default activated them, or an admin did.
    activated_by            activation_source NOT NULL DEFAULT 'user',
    activated_by_groups     JSONB,
    -- Trigger OVERRIDE. All NULL means INHERITED: the subscription follows the
    -- definition's defaults, including later edits. Set, it is this user's own
    -- schedule; timezone may be overridden on its own.
    schedule_kind           schedule_kind,
    cron_expr               TEXT,
    interval_seconds        INTEGER,
    run_at                  TIMESTAMPTZ,
    timezone                TEXT,
    -- Run bookkeeping, per subscriber: each subscriber's runs are their own.
    next_run_at             TIMESTAMPTZ NOT NULL,
    last_run_at             TIMESTAMPTZ,
    retry_at                TIMESTAMPTZ,
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    paused_reason           TEXT,
    -- The `prev` a change-detecting watch compares against. Per subscriber because
    -- each subscriber's check runs as them and may return different data.
    last_check_result       JSONB,
    -- Delivery target. The channel is tenant-scoped, so the definition's channel is
    -- valid for every member of that tenant; the recipient is always the subscriber.
    delivery_channel_id     INTEGER REFERENCES delivery_channels(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at              TIMESTAMPTZ,

    -- Either no schedule override at all, or a complete one of exactly one kind —
    -- the definition's own rule, with "all NULL" added as the inherited state.
    CONSTRAINT scheduled_job_subscriptions_trigger_override
        CHECK (
            (schedule_kind IS NULL      AND cron_expr IS NULL AND interval_seconds IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'cron'     AND cron_expr IS NOT NULL AND interval_seconds IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'interval' AND interval_seconds IS NOT NULL AND cron_expr IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'once'     AND run_at IS NOT NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
        )
);

-- One live subscription per (definition, user). Partial so a re-subscribe after an
-- unsubscribe (soft delete) is a new row rather than a constraint violation.
CREATE UNIQUE INDEX idx_scheduled_job_subscriptions_one_per_user
    ON scheduled_job_subscriptions (definition_id, user_id)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_scheduled_job_subscriptions_user
    ON scheduled_job_subscriptions (user_id);
CREATE INDEX idx_scheduled_job_subscriptions_definition
    ON scheduled_job_subscriptions (definition_id);
-- The claim loop's two wake-up reasons, as idx_scheduled_jobs_next_run and
-- idx_scheduled_jobs_retry_at were.
CREATE INDEX idx_scheduled_job_subscriptions_next_run
    ON scheduled_job_subscriptions (next_run_at)
    WHERE enabled = TRUE;
CREATE INDEX idx_scheduled_job_subscriptions_retry_at
    ON scheduled_job_subscriptions (retry_at)
    WHERE retry_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Sharing: permission grants and group defaults, reused from sub-agents.
-- ---------------------------------------------------------------------------
-- 'read' = members may subscribe (and copy); 'write' = may edit the definition,
-- suspend it and share it on. Never delete — that stays with the owner or an admin.
CREATE TABLE scheduled_job_definition_permissions (
    id              SERIAL PRIMARY KEY,
    definition_id   INTEGER NOT NULL REFERENCES scheduled_job_definitions(id) ON DELETE CASCADE,
    user_group_id   INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    permissions     TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (definition_id, user_group_id),
    CHECK (
        permissions <@ ARRAY['read', 'write']::TEXT[]
        AND array_length(permissions, 1) > 0
    )
);
CREATE INDEX idx_job_definition_permissions_group
    ON scheduled_job_definition_permissions (user_group_id);

-- A group default activates every current and future member: a subscription is
-- created for each, initialised from the definition's trigger defaults and ENABLED.
-- Only a definition already shared to the group may become its default — a default
-- never grants access by itself.
CREATE TABLE user_group_default_jobs (
    id                  SERIAL PRIMARY KEY,
    user_group_id       INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    definition_id       INTEGER NOT NULL REFERENCES scheduled_job_definitions(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    UNIQUE (user_group_id, definition_id)
);
CREATE INDEX idx_group_default_jobs_group ON user_group_default_jobs (user_group_id);
CREATE INDEX idx_group_default_jobs_definition ON user_group_default_jobs (definition_id);

-- ---------------------------------------------------------------------------
-- 4. Migrate every existing job: one definition + one subscription, same id.
-- ---------------------------------------------------------------------------
-- The stored timezone stays on the definition as an explicit zone: existing
-- expressions were authored as their owner's wall-clock (077), and a job that so far
-- ran for one person keeps firing at the same hour for that person.
INSERT INTO scheduled_job_definitions (
    id, owner_user_id, name, job_type, prompt, sub_agent_id,
    check_tool, check_args, check_args_exprs, cel_expr, llm_condition,
    destroy_after_trigger, notification_message, voice_call, max_failures,
    schedule_kind, cron_expr, interval_seconds, run_at, timezone, trigger_policy,
    created_at, updated_at, deleted_at
)
SELECT
    id, user_id, name, job_type, prompt, sub_agent_id,
    check_tool, check_args, check_args_exprs, cel_expr, llm_condition,
    destroy_after_trigger, notification_message, voice_call, max_failures,
    schedule_kind, cron_expr, interval_seconds, run_at, timezone,
    CASE job_type WHEN 'watch' THEN 'fixed'::trigger_policy ELSE 'overridable'::trigger_policy END,
    created_at, updated_at, deleted_at
FROM scheduled_jobs;

-- The owner is just another subscriber. Trigger columns stay NULL: inherited.
INSERT INTO scheduled_job_subscriptions (
    id, definition_id, user_id, activated_by,
    next_run_at, last_run_at, retry_at, enabled, consecutive_failures, paused_reason,
    last_check_result, delivery_channel_id, created_at, updated_at, deleted_at
)
SELECT
    id, id, user_id, 'user'::activation_source,
    next_run_at, last_run_at, retry_at, enabled, consecutive_failures, paused_reason,
    last_check_result, delivery_channel_id, created_at, updated_at, deleted_at
FROM scheduled_jobs;

-- Both sequences continue after the ids just copied in.
SELECT setval(
    pg_get_serial_sequence('scheduled_job_definitions', 'id'),
    COALESCE((SELECT MAX(id) FROM scheduled_job_definitions), 0) + 1,
    false
);
SELECT setval(
    pg_get_serial_sequence('scheduled_job_subscriptions', 'id'),
    COALESCE((SELECT MAX(id) FROM scheduled_job_subscriptions), 0) + 1,
    false
);

-- ---------------------------------------------------------------------------
-- 5. Runs hang off subscriptions. Same ids, renamed column, re-pointed FK.
-- ---------------------------------------------------------------------------
ALTER TABLE scheduled_job_runs
    DROP CONSTRAINT scheduled_job_runs_job_id_fkey;
ALTER TABLE scheduled_job_runs
    RENAME COLUMN job_id TO subscription_id;
ALTER TABLE scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_subscription_id_fkey
        FOREIGN KEY (subscription_id)
        REFERENCES scheduled_job_subscriptions(id)
        ON DELETE CASCADE;
-- The indexes follow the renamed column by themselves; only their names are stale.
ALTER INDEX idx_scheduled_job_runs_job RENAME TO idx_scheduled_job_runs_subscription;

-- Usage attribution keys on the subscription: the spend is the subscriber's.
ALTER TABLE usage_logs
    DROP CONSTRAINT IF EXISTS usage_logs_scheduled_job_id_fkey;
ALTER TABLE usage_logs
    ADD CONSTRAINT usage_logs_scheduled_job_id_fkey
        FOREIGN KEY (scheduled_job_id)
        REFERENCES scheduled_job_subscriptions(id)
        ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- 6. The bundled row is gone. No compatibility view: every reader was rewritten.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_scheduled_jobs_retry_at;
DROP INDEX IF EXISTS idx_scheduled_jobs_next_run;
DROP INDEX IF EXISTS idx_scheduled_jobs_user;
DROP TABLE scheduled_jobs;

-- rambler down
-- Best effort. The bundled row can only hold ONE subscriber, so a definition is
-- rebuilt from its OWNER's subscription; other subscribers' subscriptions, their runs
-- and their usage attribution are dropped with the tables. A definition whose owner
-- has unsubscribed is rebuilt from its oldest live subscription instead, under that
-- subscriber, so no definition is lost.
CREATE TABLE scheduled_jobs (
    id                      SERIAL PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name                    TEXT NOT NULL,
    job_type                job_type NOT NULL,
    schedule_kind           schedule_kind NOT NULL,
    cron_expr               TEXT,
    interval_seconds        INTEGER,
    run_at                  TIMESTAMPTZ,
    next_run_at             TIMESTAMPTZ NOT NULL,
    last_run_at             TIMESTAMPTZ,
    prompt                  TEXT,
    sub_agent_id            INTEGER REFERENCES sub_agents(id) ON DELETE RESTRICT,
    check_tool              TEXT,
    check_args              JSONB,
    llm_condition           TEXT,
    destroy_after_trigger   BOOLEAN NOT NULL DEFAULT TRUE,
    last_check_result       JSONB,
    notification_message    TEXT,
    delivery_channel_id     INTEGER REFERENCES delivery_channels(id) ON DELETE SET NULL,
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    max_failures            INTEGER NOT NULL DEFAULT 3,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    paused_reason           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at              TIMESTAMPTZ,
    voice_call              BOOLEAN NOT NULL DEFAULT FALSE,
    timezone                TEXT,
    cel_expr                TEXT,
    check_args_exprs        JSONB,
    retry_at                TIMESTAMPTZ,
    CONSTRAINT scheduled_jobs_task_requires_agent
        CHECK ((job_type = 'task' AND sub_agent_id IS NOT NULL) OR job_type = 'watch'),
    CONSTRAINT scheduled_jobs_schedule_config
        CHECK (
            (schedule_kind = 'cron'     AND cron_expr IS NOT NULL AND interval_seconds IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'interval' AND interval_seconds IS NOT NULL AND cron_expr IS NULL AND run_at IS NULL) OR
            (schedule_kind = 'once'     AND run_at IS NOT NULL AND cron_expr IS NULL AND interval_seconds IS NULL)
        ),
    CONSTRAINT scheduled_jobs_watch_requires_check
        CHECK (job_type = 'task' OR check_tool IS NOT NULL)
);

WITH chosen AS (
    SELECT DISTINCT ON (s.definition_id) s.*
    FROM scheduled_job_subscriptions s
    JOIN scheduled_job_definitions d ON d.id = s.definition_id
    WHERE s.deleted_at IS NULL
    ORDER BY s.definition_id, (s.user_id = d.owner_user_id) DESC, s.created_at ASC, s.id ASC
)
INSERT INTO scheduled_jobs (
    id, user_id, name, job_type, schedule_kind, cron_expr, interval_seconds, run_at, timezone,
    next_run_at, last_run_at, retry_at, prompt, sub_agent_id, check_tool, check_args,
    check_args_exprs, cel_expr, llm_condition, destroy_after_trigger, last_check_result,
    notification_message, delivery_channel_id, voice_call, enabled, max_failures,
    consecutive_failures, paused_reason, created_at, updated_at, deleted_at
)
SELECT
    c.id, c.user_id, d.name, d.job_type,
    COALESCE(c.schedule_kind, d.schedule_kind),
    CASE WHEN c.schedule_kind IS NULL THEN d.cron_expr ELSE c.cron_expr END,
    CASE WHEN c.schedule_kind IS NULL THEN d.interval_seconds ELSE c.interval_seconds END,
    CASE WHEN c.schedule_kind IS NULL THEN d.run_at ELSE c.run_at END,
    COALESCE(c.timezone, d.timezone),
    c.next_run_at, c.last_run_at, c.retry_at, d.prompt, d.sub_agent_id, d.check_tool, d.check_args,
    d.check_args_exprs, d.cel_expr, d.llm_condition, d.destroy_after_trigger, c.last_check_result,
    d.notification_message, c.delivery_channel_id, d.voice_call, c.enabled, d.max_failures,
    c.consecutive_failures, c.paused_reason, d.created_at, d.updated_at, d.deleted_at
FROM chosen c
JOIN scheduled_job_definitions d ON d.id = c.definition_id;

SELECT setval(
    pg_get_serial_sequence('scheduled_jobs', 'id'),
    COALESCE((SELECT MAX(id) FROM scheduled_jobs), 0) + 1,
    false
);

CREATE INDEX idx_scheduled_jobs_next_run ON scheduled_jobs (next_run_at) WHERE enabled = TRUE;
CREATE INDEX idx_scheduled_jobs_user ON scheduled_jobs (user_id);
CREATE INDEX idx_scheduled_jobs_retry_at ON scheduled_jobs (retry_at) WHERE retry_at IS NOT NULL;

-- Runs of subscriptions that did not survive the collapse have no job to hang off.
DELETE FROM scheduled_job_runs WHERE subscription_id NOT IN (SELECT id FROM scheduled_jobs);
ALTER TABLE scheduled_job_runs DROP CONSTRAINT scheduled_job_runs_subscription_id_fkey;
ALTER TABLE scheduled_job_runs RENAME COLUMN subscription_id TO job_id;
ALTER TABLE scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_job_id_fkey
        FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE;
ALTER INDEX idx_scheduled_job_runs_subscription RENAME TO idx_scheduled_job_runs_job;

UPDATE usage_logs SET scheduled_job_id = NULL
WHERE scheduled_job_id IS NOT NULL AND scheduled_job_id NOT IN (SELECT id FROM scheduled_jobs);
ALTER TABLE usage_logs DROP CONSTRAINT IF EXISTS usage_logs_scheduled_job_id_fkey;
ALTER TABLE usage_logs
    ADD CONSTRAINT usage_logs_scheduled_job_id_fkey
        FOREIGN KEY (scheduled_job_id) REFERENCES scheduled_jobs(id) ON DELETE SET NULL;

DROP TABLE IF EXISTS user_group_default_jobs;
DROP TABLE IF EXISTS scheduled_job_definition_permissions;
DROP TABLE IF EXISTS scheduled_job_subscriptions;
DROP TABLE IF EXISTS scheduled_job_definitions;
DROP TYPE IF EXISTS trigger_policy;
