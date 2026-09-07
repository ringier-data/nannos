-- rambler up

-- Marks the outbound SCIM endpoint(s) that provision groups into the MCP
-- gateway (Gatana). Console-backend used to infer this by comparing the
-- endpoint hostname's apex domain against MCP_GATEWAY_URL, which breaks for
-- on-premise gateways hosted under a corporate domain: every SCIM endpoint
-- under that domain would match. The flag makes the relationship explicit.
ALTER TABLE outbound_scim_endpoints
    ADD COLUMN is_mcp_gateway BOOLEAN NOT NULL DEFAULT false;

-- One-time seed approximating the old heuristic: Gatana endpoints (cloud
-- *.gatana.ai or on-premise hosts carrying a "gatana" label) are the only
-- ones that ever matched it. Adjust afterwards via the admin API/UI.
UPDATE outbound_scim_endpoints
SET is_mcp_gateway = true
WHERE deleted_at IS NULL
  AND endpoint_url ILIKE '%gatana%';

-- rambler down

ALTER TABLE outbound_scim_endpoints DROP COLUMN IF EXISTS is_mcp_gateway;
