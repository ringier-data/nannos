-- rambler up
ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'bug_report_filed';

-- The existing (user_id, created_at DESC) index cannot serve a range scan on created_at
-- alone, and an administrator lists with user_id unset — which is exactly the scheduled
-- watch's query (WHERE created_at > $1 ORDER BY created_at DESC). Without this index its
-- cost grows with the size of the table rather than the size of the window, on every poll.
CREATE INDEX IF NOT EXISTS idx_bug_reports_created_at ON bug_reports (created_at DESC);

-- rambler down
DROP INDEX IF EXISTS idx_bug_reports_created_at;
-- NOTE: PostgreSQL does not support removing enum values, so the notification_type value
-- added above is intentionally irreversible.
