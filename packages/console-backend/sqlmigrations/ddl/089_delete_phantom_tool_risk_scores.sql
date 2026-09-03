-- rambler up
-- Remove risk profiles that were never derived from a tool.
--
-- When a model emitted a camelCase PTC identifier (`tools.consoleCreateBugReport`)
-- as a *native* tool call, the risk gate could not fetch the tool — the registry is
-- keyed on the snake_case name — yet classified it anyway, from the name alone. The
-- LLM saw "No description available / No schema available", so `risk_factors` came
-- back `{}` ("this tool has no risk-bearing parameters") and the result was cached
-- AND persisted with `schema_hash = ''`, indistinguishable in the catalogue from a
-- profile derived from a real schema.
--
-- The scorer no longer classifies a call it cannot fetch, so no new such row can
-- appear. These are the ones already on record.
--
-- Deleted by exact name, never by `schema_hash = ''`: the static guards seeded in
-- 057 carry an empty hash on purpose (they are hand-written policy, not derived
-- profiles). The hash predicate here is only a safety catch, so that a row which
-- has since been re-scored against a real schema is left alone.
DELETE FROM tool_risk_scores
WHERE server_slug = '_self'
  AND schema_hash = ''
  AND tool_name IN (
    'consoleCreateBugReport',
    'consoleSearchSkills',
    'consoleListMcpServers',
    'consoleGrepMcpTools',
    'githubSearchIssues',
    'readFile',
    'FinalResponseSchema',
    'SubAgentResponseSchema'
  );

-- rambler down
-- Intentionally irreversible: the deleted rows carried no information about any
-- tool (empty schema, empty risk_factors), so there is nothing to restore. Any
-- profile still needed will be re-derived on next use from the tool's real schema.
SELECT 1;
