-- rambler up

-- Stores KMS-envelope-encrypted Keycloak offline refresh tokens for scheduler user impersonation.
--
-- Encryption format (stored in encrypted_token BYTEA):
--   [2 bytes: encrypted_dek_len][encrypted_dek][12 bytes: nonce][ciphertext]
--
-- encrypted_dek  - AWS KMS-encrypted data encryption key (via kms:GenerateDataKey)
-- nonce          - AES-256-GCM nonce (12 bytes / 96 bits)
-- ciphertext     - AES-256-GCM encrypted refresh token + 16-byte GCM auth tag
--
-- To decrypt:
--   1. kms:Decrypt(encrypted_dek) -> plaintext DEK
--   2. AES-256-GCM decrypt(ciphertext, nonce, dek) -> refresh token
CREATE TABLE user_offline_tokens (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    encrypted_token BYTEA NOT NULL,
    token_expiry    TIMESTAMPTZ,    -- optional hint for monitoring; actual validity via Keycloak
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- rambler down

DROP TABLE IF EXISTS user_offline_tokens;
