-- rambler up
-- Per-user "entitlements changed" marker for state the console does not own.
--
-- The orchestrator keys its per-user discovery cache on an *entitlement version*
-- (GET /api/v1/auth/me/entitlement-version): a fingerprint derived from the rows that
-- decide which tools and sub-agents a user gets — role, settings, group memberships,
-- group default agents, sub-agent activations. Those rows change the fingerprint by
-- themselves. One entitlement lives outside this database: group → MCP-server access
-- is held by the gateway. When the console grants or revokes it, it bumps this column
-- for the group's members so the fingerprint moves and the orchestrator re-discovers.
ALTER TABLE users
ADD COLUMN entitlements_touched_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
-- rambler down
ALTER TABLE users DROP COLUMN IF EXISTS entitlements_touched_at;
