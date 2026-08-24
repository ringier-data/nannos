-- rambler up

-- Why a watch run did what it did. An expression can be re-evaluated against the
-- stored response at any time, but a model's judgement cannot be reconstructed: the
-- reasoning exists only while the run is happening, and without it "Condition not met"
-- is the whole of the explanation a user ever gets.
--
-- On the run rather than the job because the explanation belongs to an occurrence — the
-- run history lists them, and each row can say why.
ALTER TABLE scheduled_job_runs ADD COLUMN condition_evaluation JSONB;

COMMENT ON COLUMN scheduled_job_runs.condition_evaluation IS
    'How the watch condition was decided: {met, mode, extracted, gate_met?, reasoning?}. NULL for task jobs and for runs that never reached evaluation.';

-- rambler down

ALTER TABLE scheduled_job_runs DROP COLUMN IF EXISTS condition_evaluation;
