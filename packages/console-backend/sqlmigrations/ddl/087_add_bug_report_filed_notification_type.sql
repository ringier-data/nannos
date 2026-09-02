-- rambler up
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'bug_report_filed';

-- rambler down
-- NOTE: PostgreSQL does not support removing enum values; this migration is intentionally irreversible.
