-- rambler up

-- Marks a row as a machine identity rather than a person. Audiences selected by standing
-- (notification fan-outs, in particular) need to exclude these, and until now had to do it
-- by id — which is one instance of the category, not the category. See issue #198.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_service_account BOOLEAN NOT NULL DEFAULT FALSE;

-- The seeded owner of auto-provisioned agents (migration 041).
UPDATE users SET is_service_account = TRUE WHERE id = 'system';

-- Keycloak names a client's service account 'service-account-<clientId>', and console-backend
-- auto-onboards one the first time a service calls it with a client-credentials token. Matching
-- the convention backfills the rows that already exist; new ones are flagged at creation from
-- the token's own claims, so this pattern is not load-bearing going forward.
UPDATE users
   SET is_service_account = TRUE
 WHERE is_service_account IS FALSE
   AND (email LIKE 'service-account-%' OR email LIKE '%@service-account.%');

-- rambler down
ALTER TABLE users DROP COLUMN IF EXISTS is_service_account;
