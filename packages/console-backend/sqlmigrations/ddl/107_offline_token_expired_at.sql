-- rambler up
-- A vaulted offline token that Keycloak refused (invalid_grant: 30 days idle, revoked, or
-- its offline session ended) is dead, but the row used to stay and read as "has consent".
-- Every run under it then failed on the refresh and counted towards auto-pause. The row is
-- now marked instead: a marked token counts as absent everywhere, the subscription waits
-- for the next sign-in like one that never had a token, and that sign-in clears the mark
-- by storing a fresh token. Kept rather than deleted so "signed in once, expired since"
-- stays distinguishable from "never signed in".
ALTER TABLE user_offline_tokens ADD COLUMN expired_at TIMESTAMPTZ;

COMMENT ON COLUMN user_offline_tokens.expired_at IS
    'Set when Keycloak refused the token (invalid_grant); a marked token counts as absent. Cleared by the next stored token.';

-- rambler down
ALTER TABLE user_offline_tokens DROP COLUMN IF EXISTS expired_at;
