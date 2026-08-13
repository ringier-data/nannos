-- rambler up

-- Message history is read one keyset-paginated page at a time, ordered by
-- (created_at, id) DESC and filtered with (created_at, id) < cursor. The
-- existing idx_messages_conversation_created stops at created_at, so ties (two
-- messages persisted in the same instant, common when an agent turn writes a
-- burst of status updates) still need a sort. Extending the index with id makes
-- the whole page an index range scan and keeps the seam between pages stable.
CREATE INDEX idx_messages_conversation_created_id ON messages(conversation_id, created_at, id);

-- rambler down

DROP INDEX IF EXISTS idx_messages_conversation_created_id;
