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
-- Two kinds are listed below. The camelCase names are PTC identifiers a model lifted
-- out of the `eval` prompt and emitted as native calls. The rest are the deep-agent
-- builtins (`write_todos` and the filesystem tools): `create_deep_agent` registers
-- those with ToolNode itself, so they were in neither the per-user `tool_registry`
-- nor the gate's `platform_tools` and hit the very same path — a profile guessed from
-- the name, with `risk_factors = {}` because the prompt read "No schema available".
-- They are now handed to the gate as real instances (`deep_agent_builtin_tools`), so
-- they classify from their actual description and schema like any other tool. But
-- only once these rows are gone: an empty stored `schema_hash` never mismatches on
-- lookup, so a surviving junk row would pin `write_file` to its guess forever and the
-- fix would never reach the most-used tools in the system.
--
-- Deleting one of these is cheap and self-healing — the profile is re-derived from the
-- real schema on next use. That is what makes it a different call from the 057 policy
-- rows, where a delete would lose a hand-written decision with nothing to re-derive
-- it from. An admin who had tuned one of the builtins by hand does lose that edit.
--
-- Deleted by exact name, never by `schema_hash = ''`: an empty hash does NOT mean
-- "phantom". The static guards seeded in 057 carry one on purpose (hand-written
-- policy, not derived profiles), and so does any score an admin creates by hand
-- through the console — `ToolRiskScoreUpsertRequest.schema_hash` defaults to ''
-- (routers/tool_risk_router.py). A blanket predicate would delete those too. The
-- hash test below is only a safety catch, so that a row which has since been
-- re-scored against a real schema is left alone.
--
-- KNOWN GAP (deliberately not addressed here): `ToolRiskCache.get` cannot tell the
-- two kinds of empty hash apart either — it treats an empty stored hash as
-- "unchanged, trust it" (`_schema_confirms_unchanged`), so any phantom NOT in this
-- list keeps shadowing the schema-derived profile this change makes possible. Fixing
-- that needs a column that records where a row came from; tracked separately.
--
-- DEPLOY: `ToolRiskCache._do_refresh` merges additively and never removes, so this
-- DELETE does not reach already-warm pods. Ship it with a rollout restart of the
-- orchestrator / agent-runner, or the in-memory copies survive until they expire.
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
    'SubAgentResponseSchema',
    -- Deep-agent builtins, classified without a schema on the same path. Absent on a
    -- deployment where the model never called one; the DELETE is then a no-op.
    'write_todos',
    'ls',
    'glob',
    'grep',
    'read_file',
    'write_file',
    'edit_file',
    'execute'
  );

-- rambler down
-- Intentionally irreversible: the deleted rows carried no information about any
-- tool (empty schema, empty risk_factors), so there is nothing to restore. Any
-- profile still needed will be re-derived on next use from the tool's real schema.
SELECT 1;
