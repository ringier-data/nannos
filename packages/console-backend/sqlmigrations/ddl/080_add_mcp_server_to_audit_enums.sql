-- rambler up
-- Identity-disclosure consent (ADR 0006 Gate 3) is recorded per MCP server, and every
-- answer is audited: approving one hands that integration the user's verified email for
-- all of its identity-scoped tools, so who whitelisted which server (and when) must be
-- reconstructable. The audit INSERT would otherwise fail with
-- `invalid input value for enum audit_entity_type: "mcp_server"` and roll back the write.
ALTER TYPE audit_entity_type ADD VALUE IF NOT EXISTS 'mcp_server';

-- rambler down
-- NOTE: PostgreSQL does not support removing enum values; this migration is intentionally irreversible.
