-- rambler up
ALTER TABLE user_settings
ADD COLUMN identity_consent_grants JSONB NOT NULL DEFAULT '{}'::JSONB;
COMMENT ON COLUMN user_settings.identity_consent_grants IS 'Remembered per-(user, MCP server) consent answers for identity-scoped tools (ADR 0006 Gate 3). Format: {"server_slug": {"granted": true|false}} — one answer covers every identity-scoped tool of that integration, keyed by the bare slug (not the tool::server compound of tool_bypass_rules). Deliberately separate from tool_bypass_rules (identity disclosure is a different axis from action-risk tolerance).';
-- rambler down
ALTER TABLE user_settings DROP COLUMN IF EXISTS identity_consent_grants;
