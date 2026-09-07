---
status: accepted (2026-09-05)
---

# An interrupted scheduled run gets one fresh attempt, never a resumption

When the process executing a scheduled run dies, the run is abandoned: the agent's trace stops
mid-flight, the run row is recorded `failed` with `delivered=False`, and nobody tells the user. This
is not hypothetical — it has happened in production and cost a user their scheduled result. We
decided that **an interruption is a distinct terminal outcome, not a failure**, that **recovery is a
fresh attempt of the job rather than a resumption of the interrupted run**, and that the user hears
about it **only once recovery is exhausted**.

Concretely: `JobRunStatus.INTERRUPTED` is excluded from `consecutive_failures`; recovery is fired by
a durable `retry_at` marker that any healthy scheduler process claims through the existing
`FOR UPDATE SKIP LOCKED` path; every run records why it was started (`trigger`: `scheduled`,
`retry` or `manual`), and only a `scheduled` run's interruption earns the one retry; run liveness
is a `last_seen_at` heartbeat swept on staleness; and the terminal notice is delivered by
dispatching an **ephemeral notification-only job** — an A2A message carrying the text and the job's
push config but no `sub_agent_id`, so the runner delivers it and runs no agent. The notice is
recorded on the run as owed (`notice_due_at`) and delivered by a later tick, not sent where the loss
is detected. A `run_now` manual run is marked interrupted but never retried — the user is present,
and reviving a test press through the claim path would turn it into a scheduled execution.

The unbounded parsing of MCP tool *outputs* is a separate track and deliberately out of scope here.

## Why

- **Not resuming is what keeps side effects out of the recovery path.** The Postgres checkpoint of
  an interrupted run is forensic only. Because no half-executed run is ever continued, no
  already-sent email or already-written record can be replayed, and we never have to reason about
  where the checkpoint boundary sits relative to a tool node. The price — discarding the LLM spend
  and completed tool calls of the dead attempt, and repeating non-idempotent writes on the new one —
  is accepted knowingly.
- **An interruption is evidence about the runtime, not about the job.** Recording it as `failed` let
  process churn write itself into user state: enough restarts landing on the same job cross
  `max_failures` (default 3) and auto-disable it with a `paused_reason` blaming the job. It also
  made the run history unable to answer "did my job fail, or did the process die?" without going
  outside the product to read logs.
- **The retry must survive the death that caused it.** The process best placed to notice an
  interruption is often the one dying, so the retry cannot live in its memory. Putting the marker in
  the database and letting the existing claim loop pick it up means the retry works precisely when
  the noticing process does not. `retry_at` is a separate column from `next_run_at` because writing a
  retry into the schedule would overwrite the field users read, and for `ScheduleKind.ONCE` — where
  `compute_next_run` returns `None` — it would give a "never repeats" job a next run.
- **One retry, because whatever killed the first attempt is often still there.** A process failing
  repeatedly will fail the retry too, and an unbounded retry turns one unhealthy process into a
  stampede across every scheduled job. One retry bounds the worst case at twice normal traffic.
- **Liveness needs a heartbeat only for the death nobody is watching.** A dead `agent-runner`
  already surfaces to the dispatching process within five minutes through the stream itself
  (`dispatch_streaming` sets `read=300.0` as the inter-event timeout). A dead `console-backend`
  leaves the stream unobserved, so the run carries `last_seen_at` and the healer sweeps on staleness
  rather than age. This is also what makes the healer correct when more than one scheduler process
  is running: `_in_flight` is an in-memory `set[int]`, so a second process has no way to know a run
  is alive elsewhere, and an age-based bound alone would let it interrupt a healthy long run and
  fire a duplicate attempt. Runs written before heartbeats existed keep the old age bound, so a
  rolling deploy does not sweep a run the previous release is still executing.
- **Only the dispatch can say the agent died.** The a2a SDK wraps every httpx error before it
  reaches a caller, so classifying by httpx type in the scheduler misses the case this exists for —
  a runner still restarting when the card cache is empty. `dispatch_streaming` raises one typed
  `AgentUnreachable` for a transport error, a dropped stream or a gateway 502/503/504, and nothing
  else is an interruption: a Keycloak or database error on the way to the dispatch is a failure of
  the run, since nothing was executing.
- **The retry marker has one consumer and is never cleared by a completion.** Runs of one job can
  finish out of order — a manual run completing after the healer marked a scheduled one lost — so
  `complete_job` only writes `retry_at` (COALESCE). The claim consumes it; pausing, resuming or
  disabling the job clears it, because the user has made a decision about the job and a fresh
  attempt for an earlier interruption is not it. The retry branch ignores `enabled` (a retired
  `once` job is disabled too) and trusts `paused_reason`, so every deliberate stop writes one.
- **One loss, one attempt.** The schedule does not advance until a run completes, so a job whose
  run was stranded still has a due `next_run_at`; without a guard a restart would re-claim it as an
  ordinary run *and* the healer would then grant a retry. `claim_due_jobs` skips a job while any
  of its runs is `running`, which also means runs of one job never overlap. And a run the healer
  has called interrupted stays interrupted even if its dispatcher turns out to be alive and
  finishes: the retry is already on its way, and a row flipping back to success would hide that
  the job ran twice.
- **Silence is correct when recovery works.** An interruption the system absorbed is not news. Only
  the terminal case is worth a message, which also keeps the notifier quiet during an incident
  instead of amplifying it.

## Alternatives considered

- **Resume the interrupted thread from its checkpoint.** Preserves the work already paid for, and
  the checkpointer plus S3 offload already exist. Rejected because it makes replay safety a
  permanent design obligation — every side-effecting tool call becomes a question of whether it
  already fired — and because it needs durable ownership to stop two processes driving one thread.
- **A recovery policy derived from each job's declared side effects** (read-only jobs resume,
  writing jobs do not). The most correct option and the most machinery; revisit only if discarded
  work becomes expensive enough to justify tracking which tools already fired.
- **A full lease with `owner_id`** instead of a bare `last_seen_at`. Rejected as more than the
  problem needs; the cost is that nothing fences a resurrected process, and the run carries no
  record of who owned it.
- **A Postgres advisory lock per run.** Clock-free and instant, but pins a database connection for
  the whole run and turns any connection blip into a false interrupt plus a duplicate attempt.
- **Routing every A2A client's notifications through `console-backend`.** Tempting as a single place
  for auth, retry and audit. Rejected because delivery is currently decentralised on purpose:
  `agent-runner`, `orchestrator-agent` and `voice-agent` each own a push sender and the webhook is
  registered on the A2A task at dispatch, so a `console-backend` death today strands the bookkeeping
  but **not** the user's answer. Brokering would put the scheduler on the hot path of every result
  and make it a single point of failure the A2A model does not ask for.
- **Giving `console-backend` its own narrow delivery path** for scheduler lifecycle facts only,
  sharing the payload and token contract through `ringier-a2a-sdk`. This was the original decision
  here, and it was wrong: `scheduler_engine._build_message_args` already carries a comment saying
  why, written when notification-only watches were designed — *"delivery is the push sender's job,
  and the payload is an A2A Task envelope that the delivery channels normalise. Posting it from here
  would duplicate that contract across three receivers."* A second producer would also have needed a
  new `scheduler_status` case, or a safe default, in each of the Slack, Google Chat and email
  adapters. The ephemeral dispatch needs none: the notice arrives in the envelope they already
  normalise.
- **Not notifying at all when the runner is unreachable.** The objection to the ephemeral dispatch is
  that it needs the very component whose death is being reported. In practice the runner is not gone
  — it restarts in seconds, the retry is a minute behind the interruption, and the notify dispatch is
  trivial where the failed job was not: no tools, no accumulated context, no sandbox, so it survives
  the memory pressure that killed the job. And because the notice is owed rather than sent, an agent
  that is briefly absent costs nothing: the debt is simply carried to a later tick. Only an outage
  outlasting the give-up bound loses the notice, which a direct path would have lost immediately.

## Consequences

- `scheduled_job_runs` gains `last_seen_at`, `trigger`, `notice_due_at` and a fourth terminal
  status (migrations 091/092); `scheduled_jobs` gains `retry_at`. Every consumer that branches on
  run status — the console UI and the Slack, Google Chat and email adapters — needs an
  `interrupted` case or a safe default render; the console's run badge has both.
- The number of processes that deliver to a channel stays at three. `console-backend` gains no
  outbound capability, and the client adapters need no new case.
- The notice must stay outside the run machinery it reports on: it records no run and uses a short
  timeout because nothing is being computed. A notify dispatch that created a run row would be swept
  by the healer when it went stale, marked interrupted, and earn the job another retry — a loop
  assembled out of the recovery mechanism itself.
- The notice is an **obligation recorded on the run** (`notice_due_at`), not a call made where the
  loss is detected. Delivering it inline would aim at an agent that is, by construction, mid-restart:
  the failure lands at connect, where no read timeout helps because nothing is listening. It would
  also miss half the cases — a run interrupted by the healer is swept in bulk, and the process that
  owed the notice may be the one that went away. A marker is delivered by whoever ticks next, after a
  delay that lets the agent come back. Unlike the job's retry it *is* re-attempted, because re-POSTing
  one sentence is not an agent turn; it is still bounded, and abandoned once the news is stale.
- A notice is also abandoned when **a later run of the same job has completed**, because delivering
  it then would contradict newer news the user already has — a fresh result, followed by a message
  saying the job could not run. Supersession is keyed on a later run *completing* rather than on the
  schedule coming round: if the next run is lost too, nothing has superseded anything and the notice
  is still owed. Ordering by `(completed_at, id)` means an outage that costs a job several runs
  produces one message rather than a burst.
- Three kinds of run now exist, told apart by the `trigger` recorded on the row rather than by a
  parameter only one code path receives: a scheduled run's interruption earns a retry, a retry's
  earns the user a notice, a manual run's earns neither.
- A finished run that cannot be recorded is closed with a minimal write (`close_run_minimally`)
  rather than left for the healer: a run left `running` after its result was delivered would be
  swept, called interrupted, and re-executed.
- The healer stops being a periodic backstop for a single scheduler process and becomes the
  mechanism that keeps run liveness correct when several run concurrently.
