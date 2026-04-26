-- rambler up

ALTER TABLE users ADD COLUMN phone_number_idp TEXT;
ALTER TABLE user_settings ADD COLUMN phone_number_override TEXT;

-- rambler down

ALTER TABLE user_settings DROP COLUMN phone_number_override;
ALTER TABLE users DROP COLUMN phone_number_idp;
