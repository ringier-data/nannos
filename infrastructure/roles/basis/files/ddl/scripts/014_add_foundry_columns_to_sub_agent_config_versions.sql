-- rambler up

-- Add Foundry agent configuration columns
ALTER TABLE sub_agent_config_versions
ADD COLUMN foundry_hostname TEXT,
ADD COLUMN foundry_client_id TEXT,
ADD COLUMN foundry_client_secret_ref INTEGER REFERENCES secrets(id) ON DELETE RESTRICT,
ADD COLUMN foundry_ontology_rid TEXT,
ADD COLUMN foundry_query_api_name TEXT,
ADD COLUMN foundry_scopes TEXT[],
ADD COLUMN foundry_version TEXT;

-- Drop the old CHECK constraint (local XOR remote)
ALTER TABLE sub_agent_config_versions
DROP CONSTRAINT IF EXISTS sub_agent_config_versions_check;

-- Add new CHECK constraint (local XOR remote XOR foundry)
ALTER TABLE sub_agent_config_versions
ADD CONSTRAINT sub_agent_config_versions_check CHECK (
    -- Local: system_prompt set, others null
    (system_prompt IS NOT NULL AND agent_url IS NULL AND foundry_query_api_name IS NULL) OR
    -- Remote: agent_url set, others null
    (system_prompt IS NULL AND agent_url IS NOT NULL AND foundry_query_api_name IS NULL) OR
    -- Foundry: foundry fields set, others null
    (system_prompt IS NULL AND agent_url IS NULL AND foundry_query_api_name IS NOT NULL AND 
     foundry_hostname IS NOT NULL AND foundry_client_id IS NOT NULL AND 
     foundry_client_secret_ref IS NOT NULL AND foundry_ontology_rid IS NOT NULL AND 
     foundry_scopes IS NOT NULL)
);

-- rambler down

-- Drop the tri-state CHECK constraint
ALTER TABLE sub_agent_config_versions
DROP CONSTRAINT IF EXISTS sub_agent_config_versions_check;

-- Restore the original CHECK constraint (local XOR remote)
ALTER TABLE sub_agent_config_versions
ADD CONSTRAINT sub_agent_config_versions_check CHECK (
    (system_prompt IS NOT NULL AND agent_url IS NULL) OR
    (system_prompt IS NULL AND agent_url IS NOT NULL)
);

-- Drop Foundry columns
ALTER TABLE sub_agent_config_versions
DROP COLUMN IF EXISTS foundry_version,
DROP COLUMN IF EXISTS foundry_scopes,
DROP COLUMN IF EXISTS foundry_query_api_name,
DROP COLUMN IF EXISTS foundry_ontology_rid,
DROP COLUMN IF EXISTS foundry_client_secret_ref,
DROP COLUMN IF EXISTS foundry_client_id,
DROP COLUMN IF EXISTS foundry_hostname;
