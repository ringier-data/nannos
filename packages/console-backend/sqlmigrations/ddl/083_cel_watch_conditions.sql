-- rambler up

-- Watch conditions become CEL expressions. One expression both extracts the evidence
-- (what the run records and hands to the model or agent) and gates the trigger: a
-- boolean result gates directly, anything else gates on non-empty. Evaluated against
-- `result` (the tool response), `now` (current time in the job's timezone) and `prev`
-- (the previous check result), so time-relative conditions are decided by date math
-- instead of a model that was never told the time. llm_condition composes with it:
-- when both are set, the CEL gate runs first and the model judges only what the
-- expression returned.
ALTER TABLE scheduled_jobs ADD COLUMN IF NOT EXISTS cel_expr TEXT;

-- The old JSONPath conditions are rewritten onto two CEL extension functions kept for
-- exactly this: jsonpath(value, path) extracts with the old extractor's contract
-- (null / value / list), and eq_ci(a, b) is the old comparison, bug-for-bug (both
-- sides as text, case-insensitive). A mechanical string rewrite preserves semantics;
-- parsing and translating arbitrary JSONPath into native CEL would not.
--
-- Judge-driven rows (llm_condition set) deliberately get NO cel_expr: their extraction
-- becomes a gate under the new model, and an "absence" condition ("tell me when
-- nothing is scheduled") would then short-circuit to not-met without ever asking the
-- model. Judging the whole response costs a few tokens more and changes no verdict.
UPDATE scheduled_jobs
SET cel_expr = 'eq_ci(jsonpath(result, "'
        || replace(replace(condition_expr, '\', '\\'), '"', '\"')
        || '"), "'
        || replace(replace(expected_value, '\', '\\'), '"', '\"')
        || '")'
WHERE job_type = 'watch'
  AND cel_expr IS NULL
  AND (llm_condition IS NULL OR llm_condition = '')
  AND condition_expr IS NOT NULL AND condition_expr <> ''
  AND expected_value IS NOT NULL;

-- Pre-operator rows without an expected value were "the value is not null"; the
-- non-boolean gate (non-empty) is that exact check.
UPDATE scheduled_jobs
SET cel_expr = 'jsonpath(result, "'
        || replace(replace(condition_expr, '\', '\\'), '"', '\"')
        || '")'
WHERE job_type = 'watch'
  AND cel_expr IS NULL
  AND (llm_condition IS NULL OR llm_condition = '')
  AND condition_expr IS NOT NULL AND condition_expr <> '';

-- An empty condition_expr is reachable: the old CHECK (034) only required NOT NULL, and
-- the old PATCH path stored '' unvalidated. The pre-CEL runner read that as condition-met
-- on every poll, so leaving such a row with no condition at all would silently turn a
-- working (if noisy) job into one that fails every poll until it auto-pauses. `true` is
-- that old behaviour, said in CEL.
UPDATE scheduled_jobs
SET cel_expr = 'true'
WHERE job_type = 'watch'
  AND cel_expr IS NULL
  AND (llm_condition IS NULL OR llm_condition = '')
  AND (condition_expr IS NULL OR condition_expr = '');

-- A watch needs a tool and a condition (cel_expr, llm_condition, or both); the API
-- enforces the condition half, the constraint keeps requiring the tool.
ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_watch_requires_check;
ALTER TABLE scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_watch_requires_check
    CHECK (job_type = 'task' OR check_tool IS NOT NULL);

ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS condition_expr;
ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS expected_value;
-- Only ever existed on environments that ran a since-withdrawn operator migration.
ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_condition_op;
ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS condition_op;

-- rambler down

-- The JSONPath originals are not recoverable from the rewritten expressions; rows get
-- the identity path the pre-CEL runner read as "the whole response", which is the
-- best-effort the old semantics allow.
ALTER TABLE scheduled_jobs ADD COLUMN IF NOT EXISTS condition_expr TEXT;
ALTER TABLE scheduled_jobs ADD COLUMN IF NOT EXISTS expected_value TEXT;

UPDATE scheduled_jobs SET condition_expr = '$' WHERE job_type = 'watch' AND condition_expr IS NULL;

ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_watch_requires_check;
ALTER TABLE scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_watch_requires_check
    CHECK (job_type = 'task' OR (check_tool IS NOT NULL AND condition_expr IS NOT NULL));

ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS cel_expr;
