-- rambler up
-- Store extra user attributes provisioned via SCIM (e.g. department, costCenter, phone).
-- Uses JSONB to flexibly accommodate any extension schema data from identity providers.
ALTER TABLE users
ADD COLUMN IF NOT EXISTS scim_attributes JSONB;
-- rambler down
ALTER TABLE users DROP COLUMN IF EXISTS scim_attributes;
