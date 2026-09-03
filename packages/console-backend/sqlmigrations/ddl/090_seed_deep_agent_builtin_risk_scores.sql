-- rambler up
-- Policy profiles for the deep-agent builtin tools.
--
-- `create_deep_agent` registers these directly with ToolNode (TodoListMiddleware +
-- FilesystemMiddleware), so they are in neither the per-user `tool_registry` nor the
-- risk gate's `platform_tools`. The gate therefore cannot fetch an instance for them,
-- and since it no longer classifies what it cannot fetch (see 089) they would fall to
-- `_deterministic_fallback` — a keyword guess on the name that lands `ls`, `glob`,
-- `grep` and `edit_file` at 0.70 and `write_todos`/`write_file` at 0.60. Arbitrary
-- numbers for the most-used tools in the system.
--
-- Seeding them makes this table the authority, exactly as it already is for the static
-- guards in 057: the cache lookup runs ahead of the unfetchable-tool guard, and an
-- empty `schema_hash` never mismatches, so these rows are honoured whether or not an
-- instance is reachable. That is the right shape here — the risk of a filesystem
-- primitive is a policy decision about the agent's own workspace, not something to be
-- re-derived from a JSON schema by an LLM on every cold cache.
--
-- These are in-process tools operating on the agent's OWN virtual filesystem, not on
-- user data or any external system, hence the low base scores. The exceptions:
--
--   * `write_file` / `edit_file` carry a `file_path` factor for `/memories/*`. That
--     subtree is the long-term memory store — indexed for `docstore_search` and read
--     back in later conversations — so a write there changes the agent's future
--     behaviour rather than just this turn's scratch space. It is set to 0.75:
--     deliberately just UNDER the 0.80 default gate, so the elevation is recorded
--     without this migration introducing a new approval card. Raise it to 0.85 to
--     make long-term-memory writes ask first — a product decision, one value away.
--   * `execute` runs shell commands. FilesystemMiddleware binds it even on backends
--     that cannot execute (where it is stripped from the model and dead), so a high
--     score costs nothing there and gates it properly where the sandbox is live.
--
-- None of these names contain delete/remove/drop/destroy, so `_destructive_floor`
-- does not override the values below.
--
-- The upsert OVERWRITES on conflict: this migration is declared the authority for
-- these eight names. It has to be. Before 089's scorer guard, an unfetchable tool was
-- classified from its name alone and the result persisted — and these tools were
-- unfetchable on exactly that path, so rows for them are likely already on record,
-- carrying an empty schema_hash and `risk_factors = {}` from a prompt that read "No
-- description available". Leaving those in place (ON CONFLICT DO NOTHING) would let
-- the junk keep shadowing the policy below, since an empty stored hash never
-- mismatches on lookup. The trade-off: an admin who had hand-tuned one of these eight
-- through the console loses that edit and must re-apply it.
INSERT INTO tool_risk_scores (
        tool_name,
        server_slug,
        schema_hash,
        base_score,
        risk_factors,
        allowed_actions
    )
VALUES
    -- Read-only over the agent's own workspace.
    ('ls', '_self', '', 0.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    ('glob', '_self', '', 0.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    ('grep', '_self', '', 0.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    ('read_file', '_self', '', 0.1, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    -- Turn-local scratchpad; never leaves the graph.
    ('write_todos', '_self', '', 0.0, '{}'::JSONB, '["approve", "reject"]'::JSONB),
    -- Workspace writes: cheap in scratch space, consequential under /memories/.
    (
        'write_file',
        '_self',
        '',
        0.3,
        '{"file_path": {"risky_values": {"/memories/*": 0.75}, "default_contribution": 0.0}}'::JSONB,
        '["approve", "edit", "reject"]'::JSONB
    ),
    (
        'edit_file',
        '_self',
        '',
        0.3,
        '{"file_path": {"risky_values": {"/memories/*": 0.75}, "default_contribution": 0.0}}'::JSONB,
        '["approve", "edit", "reject"]'::JSONB
    ),
    -- Shell execution when the backend supports it.
    ('execute', '_self', '', 0.95, '{}'::JSONB, '["approve", "edit", "reject"]'::JSONB)
ON CONFLICT (tool_name, server_slug) DO UPDATE SET
    schema_hash = EXCLUDED.schema_hash,
    base_score = EXCLUDED.base_score,
    risk_factors = EXCLUDED.risk_factors,
    allowed_actions = EXCLUDED.allowed_actions,
    updated_at = NOW();

-- rambler down
-- Removes the seeds. Anything they overwrote is not restored — the previous values
-- were name-only classifications with no schema behind them (see the note above).
DELETE FROM tool_risk_scores
WHERE server_slug = '_self'
  AND tool_name IN (
    'ls',
    'glob',
    'grep',
    'read_file',
    'write_todos',
    'write_file',
    'edit_file',
    'execute'
  );
