-- rambler up
-- A2A 1.0.0 removed the message-level `final` flag; terminal TaskStates now
-- indicate finality. Drop the now-unused column.
ALTER TABLE messages DROP COLUMN final;
-- rambler down
-- Restore the `final` column (defaults to FALSE for existing rows).
ALTER TABLE messages
ADD COLUMN final BOOLEAN NOT NULL DEFAULT FALSE;
