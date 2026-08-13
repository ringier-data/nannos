-- rambler up
ALTER TABLE user_settings
ADD COLUMN identity_consent_grants JSONB NOT NULL DEFAULT '{}'::JSONB;
COMMENT ON COLUMN user_settings.identity_consent_grants IS 'Remembered per-(user, tool) consent answers for identity-scoped tools (ADR 0006 Gate 3). Format: {"tool_name": {"granted": true|false}} — keyed by tool name alone, unlike tool_bypass_rules. Deliberately separate from tool_bypass_rules (identity disclosure is a different axis from action-risk tolerance).';
-- rambler down
ALTER TABLE user_settings DROP COLUMN IF EXISTS identity_consent_grants;
