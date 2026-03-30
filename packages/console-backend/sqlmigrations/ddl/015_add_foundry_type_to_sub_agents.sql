-- rambler up

-- Add 'foundry' value to sub_agent_type enum
ALTER TYPE sub_agent_type ADD VALUE 'foundry';

-- rambler down

-- Note: PostgreSQL does not support removing enum values
-- Manual intervention required to revert this change
