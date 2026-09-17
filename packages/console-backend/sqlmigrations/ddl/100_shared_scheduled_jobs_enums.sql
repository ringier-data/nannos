-- rambler up
-- Enum vocabulary for shared scheduled jobs (ADR-0010). Kept in its own file so the
-- forward-only enum additions are isolated from the reversible table work in 101,
-- exactly as 097 was split from 098 and 092 from 093.
--
-- A scheduled job splits into a shareable DEFINITION (what the job is, owned by one
-- user, shared to groups like a sub-agent) and per-user SUBSCRIPTIONS (one user's
-- activation of it, running under that user's identity). The notifications below
-- cover what changes what runs under YOUR identity or what you own; a writer editing
-- a shared definition's prompt is not one of them, as for a shared agent.

-- Mirrors the agent trio (agent_shared / agent_access_revoked / agent_permission_changed).
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_shared';
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_access_revoked';
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_permission_changed';
-- The consent moment: a group default activated a subscription under your identity.
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_subscription_activated';
-- A writer reset every subscriber's trigger override to the definition's defaults.
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_subscription_reset';
-- Definition-level stop and restart; each subscriber's own `enabled` is preserved.
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_suspended';
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_resumed';
-- The definition is gone and every subscription with it.
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'job_deleted';

-- A subscription is audited as its own entity; the definition keeps 'scheduled_job',
-- so existing audit rows of the pre-split job read as rows of its definition.
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'scheduled_job_subscription';

-- Verbs the split introduces on the definition (suspend, reset every override) and
-- on the relationship (subscribe, unsubscribe). Copy is an ordinary CREATE.
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'suspend';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'unsuspend';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'subscribe';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'unsubscribe';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'reset_overrides';

-- rambler down
-- Note: PostgreSQL does not support removing enum values
-- This is a forward-only migration
