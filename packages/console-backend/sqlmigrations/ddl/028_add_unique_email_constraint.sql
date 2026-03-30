-- rambler up
-- Add unique constraint on lowercased email to prevent duplicate email registrations
-- This migration should be run AFTER 026_cleanup_duplicate_emails.sql

-- Create unique index on LOWER(email) to ensure email uniqueness (case-insensitive)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique 
ON users(LOWER(email));

-- rambler down
-- Remove the unique email constraint

DROP INDEX IF EXISTS idx_users_email_unique;
