-- rambler up
-- A delivery channel knows its workspace (#192). broker_workspaces (migration 108) stored
-- the relation the other way round, as a workspace's array of installations, and every
-- reachability check had to go binding -> workspace -> array -> channel. An installation
-- belongs to exactly one workspace (a Slack app to its team, a Google Chat project name to
-- its project number), so the array was the inverse of a many-to-one relation. Stored on
-- the channel, "can this user receive here" is one probe of the user's bindings:
--
--   reachable    a binding of the user with (client_id, workspace_id)
--   unreachable  workspace_id is set and the user has a binding with client_id elsewhere
--   unknown      anything else (no binding with the client, or no workspace known)
--
-- The client sends it when it registers the channel, which it already does at every boot;
-- the separate publication (PUT /api/v1/auth/broker/workspaces/{id}) is gone with the table.
ALTER TABLE delivery_channels ADD COLUMN workspace_id TEXT;

-- backfill:workspace
UPDATE delivery_channels c SET workspace_id = w.workspace_id
FROM broker_workspaces w
WHERE w.client_id = c.client_id AND c.installation_id = ANY(w.installation_ids);
-- end backfill:workspace

DROP TABLE broker_workspaces;

-- The probe: a user's bindings with one client, and whether one is in the channel's workspace.
DROP INDEX IF EXISTS idx_broker_bindings_user;
CREATE INDEX idx_broker_bindings_user_client ON broker_bindings (user_id, client_id, workspace_id);

-- rambler down
DROP INDEX IF EXISTS idx_broker_bindings_user_client;
CREATE INDEX idx_broker_bindings_user ON broker_bindings(user_id);
CREATE TABLE broker_workspaces (
    client_id         TEXT NOT NULL REFERENCES broker_clients(client_id) ON DELETE CASCADE ON UPDATE CASCADE,
    workspace_id      TEXT NOT NULL,
    installation_ids  TEXT[] NOT NULL DEFAULT '{}',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, workspace_id)
);
INSERT INTO broker_workspaces (client_id, workspace_id, installation_ids)
SELECT client_id, workspace_id, array_agg(installation_id ORDER BY installation_id)
FROM delivery_channels WHERE workspace_id IS NOT NULL AND installation_id IS NOT NULL
GROUP BY client_id, workspace_id;
ALTER TABLE delivery_channels DROP COLUMN IF EXISTS workspace_id;
