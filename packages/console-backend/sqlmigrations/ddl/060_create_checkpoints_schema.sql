-- rambler up
-- Create checkpoints schema 
CREATE SCHEMA IF NOT EXISTS checkpoints;
GRANT USAGE ON SCHEMA checkpoints TO orchestrator_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints GRANT ALL ON TABLES TO orchestrator_user;
-- rambler down
DROP SCHEMA IF EXISTS checkpoints CASCADE;
