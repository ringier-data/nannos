-- rambler up

ALTER TABLE user_sub_agent_activations 
ADD COLUMN activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

COMMENT ON COLUMN user_sub_agent_activations.activated_at IS 'Timestamp when the sub-agent was last activated for this user';

-- rambler down

ALTER TABLE user_sub_agent_activations 
DROP COLUMN activated_at;
