-- rambler up

-- Store the SCIM userName separately from email (they can differ when IdP sends
-- a non-email userName alongside an emails array).
ALTER TABLE users ADD COLUMN IF NOT EXISTS scim_user_name TEXT;

-- rambler down

ALTER TABLE users DROP COLUMN IF EXISTS scim_user_name;
