-- rambler up

-- Dynamic check-tool arguments. check_args are stored once and sent unchanged on
-- every run, which makes a rolling time window ("the last 7 days") inexpressible: a
-- literal date that is correct today selects the wrong window on every later run.
-- check_args_exprs maps argument names to CEL expressions over `now` (the current
-- time in the job's timezone) and `prev` (the previous check result); each is
-- evaluated fresh when the check runs and its value becomes that argument, merged
-- over the static check_args (per key, the expression wins) — e.g.
--   {"start_date": "strftime(now - duration('168h'), '%Y-%m-%d')"}
-- Per argument rather than one map-valued expression so the form can offer it where
-- it belongs: typed into the argument's own field, spreadsheet-style.
--
-- Same language and limits as cel_expr on purpose: compile-checked at write time,
-- evaluated under a timeout, and a failure fails the run rather than calling the
-- tool with half-built arguments.
ALTER TABLE scheduled_jobs ADD COLUMN check_args_exprs JSONB;

-- rambler down

ALTER TABLE scheduled_jobs DROP COLUMN IF EXISTS check_args_exprs;
