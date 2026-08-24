-- rambler up

-- ============================================================================
-- The task-scheduler sub-agent's prompt still teaches the removed watch API.
--
-- 083 dropped condition_expr and expected_value, and ScheduledJobCreate now
-- requires a watch to carry cel_expr, llm_condition, or both. The tool *schema*
-- the agent sees was updated with them, but its seeded prompt (075) still walks
-- the model through building a JSONPath and an expected value — so an agent
-- following its own instructions emits fields that are silently ignored and no
-- condition at all, and every conversational watch creation fails validation
-- with nothing in the prompt to recover from.
--
-- Rewritten as targeted replacements rather than a whole-prompt reseed, so an
-- operator's edits to the rest of the prompt survive. Each replace() is a no-op
-- if the text has already been changed, and the WHERE clause keeps this to the
-- system-owned seed.
-- ============================================================================

UPDATE sub_agent_config_versions cv
SET system_prompt = replace(
        replace(
            replace(
                cv.system_prompt,
-- The job_type summary.
'Requires: check_tool, check_args, condition_expr (JSONPath), expected_value (what to compare against)',
'Requires: check_tool, check_args, and a condition — cel_expr (a CEL expression), llm_condition (judged by a model), or both
Optional: check_args_exprs for arguments that must move with time (see the workflow below)'
            ),
-- Steps 3 and 4: building and dry-running the condition.
'3. Understand the watch condition
   - Work with the user to define what to monitor
   - Use the tool input schema from step 2 to construct valid check_args
   - Use the tool output schema from step 2 to construct valid condition_expr (JSONPath to extract the value)
   - Determine expected_value — what value the extracted result should match (or null to check "is not null")
   - If no output schema is available, try calling the tool with example args to see the output format and adjust condition_expr accordingly

4. Dry-run the check tool
   - Before scheduling, call the EXACT check tool (from steps 2-3) with your check_args to confirm it returns the expected output
   - Verify your condition_expr (JSONPath) extracts the right value from that output
   - If it does not match, review the tool schema again and fix check_args or condition_expr',
'3. Understand the watch condition
   - Work with the user to define what to monitor
   - Use the tool input schema from step 2 to construct valid check_args
   - A condition is a CEL expression (cel_expr), a model judgement (llm_condition), or both. Set at least one; a watch with neither is rejected.
   - cel_expr is evaluated against `result` (the tool response), `now` (the current time in the job''s timezone) and `prev` (the previous response, for change detection). One expression does two jobs: it extracts the evidence the run records, and it gates the trigger — a boolean result gates directly, anything else gates on being non-empty.
     - ''any open incident'': result.incidents.size() > 0
     - ''starts within the hour'': result.events.filter(e, timestamp(e.start) - now < duration(''1h''))
     - ''the price changed'': result.price != prev.price
     - Prefer an expression that RETURNS the matching items over one that returns true/false: what it returns is what the run records and what a model or agent is handed.
   - llm_condition is for the genuinely semantic part only (''an attendee looks external to the company''). Alone it reads the whole response; with cel_expr it judges only what the expression returned, so the mechanical half costs no model call on a quiet poll.
   - Do NOT put a literal date in check_args. An argument that must move with time goes in check_args_exprs: a map of argument name to a CEL expression over `now`/`prev`, resolved fresh on every run and merged over check_args.
     - {"start_date": "strftime(now, ''%Y-%m-%d'')"}
     - {"since": "strftime(now - duration(''168h''), ''%Y-%m-%d'')"}
     - This is MANDATORY when the tool has a required date/time argument: a literal that is right today selects the wrong window on every later run.
   - If no output schema is available, call the tool with example args to see the real shape, then write cel_expr against it.

4. Dry-run the check tool
   - Before scheduling, call the EXACT check tool (from steps 2-3) with your check_args to confirm it returns the expected output
   - Check cel_expr against that real response: the field paths must exist in it, or the condition fails every run
   - If it does not match, review the tool schema again and fix check_args or cel_expr'
        ),
-- Step 5: the parameters actually sent.
'   - Use the validated watch parameters (check_tool, check_args, condition_expr, expected_value)',
'   - Use the validated watch parameters (check_tool, check_args, check_args_exprs, cel_expr, llm_condition)'
    )
FROM sub_agents sa
WHERE cv.sub_agent_id = sa.id
  AND sa.name = 'task-scheduler'
  AND sa.owner_user_id = 'system'
  AND cv.system_prompt LIKE '%condition_expr%';

-- rambler down

-- Not reversible in kind: 083 removed the columns the old prompt describes, so
-- restoring the JSONPath instructions would teach an API that no longer exists.
-- The prompt is left as the CEL version.
