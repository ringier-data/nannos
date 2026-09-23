---
status: proposed (2026-09-15)
---

# Authorization parks a scheduled run; it does not fail it

## Context

A scheduled job runs unattended, but the credentials its tools need belong to a
*person*. When the MCP gateway answers a call with `need-credentials`, the thing
that is missing is the job owner's consent — not a service, not a configuration,
not anything the runtime can supply. The run is blocked on a human, which is the
one condition a scheduler has no vocabulary for.

Today it has none at all. Three gaps compose:

1. **Nothing detects the error.** `AuthErrorDetectionMiddleware` is
   orchestrator-private, and agent-runner builds its sub-agent graph with
   `extra_middlewares=None`. The gateway's structured payload arrives as an
   ordinary `ToolMessage` and the model paraphrases it.
2. **Nothing can report it.** `SubAgentResponseSchema.task_state` is
   `completed | input_required | failed`; `_TERMINAL_TASK_STATE_NAMES` maps the
   same three; `JobRunStatus` has no auth state. The model's own verdict is the
   only signal.
3. **The verdict is destructive.** A model that says `failed` increments
   `consecutive_failures`, and `scheduled_job_repository` auto-pauses the job at
   `max_failures`. A model that says `completed` instead leaves a silently green
   run that did nothing. Either way the owner is never asked for the credential,
   so the job cannot recover on its own — and the first outcome eventually
   disables it.

A missing credential is not evidence about the job. It is evidence about what the
owner has authorized, and it is fixable in one click — but only if somebody is
asked.

ADR-0008 changed what this costs to build. The auth-required vocabulary moved out
of the orchestrator into `agent_common.a2a` (`IN_TASK_AUTH_EXTENSION`,
`new_auth_required_message`), and `local_server.executor.interrupt_status` turns
any paused graph's `__interrupt__` into an `auth_required` status carrying
`AuthPayload.client_payload()` — generically, for any local sub-agent. Decisions
6 and 7 made a parked task durable and its resume rebuildable from the task store
and the checkpoint alone. The ADR's own consequences name agent-runner as a
caller that could mount this executor "without further change to the sub-agent
side". This decision is that caller.

## Decision

1. **agent-runner serves its scheduled sub-agent through the in-process A2A
   server, and stops building its own.** It goes through `LocalA2AServer` exactly
   as the orchestrator's dispatch does, which means it needs a `LocalA2ARunnable`,
   which it does not have — it assembles a graph inline and calls `graph.astream`.
   Rather than wrap that in a private subclass, it adopts
   `agent_common.agents.dynamic_agent.DynamicLocalAgentRunnable`, the same class
   the orchestrator's local sub-agents already use. Four things agent-runner does
   today have to survive the move, and they are named in Consequences: the stall
   watchdog, the tool-discovery failure policy, the model-call budget's env var,
   and the stateless catalogue listing.

   A run blocked on a credential is then an A2A task in `auth_required` carrying
   the in-task-auth DataPart — the same bytes an interactive chat already
   produces, produced by the same code. Nothing about the auth payload is
   assembled in agent-runner.

   The sub-agent's task id is derived, never stored: `uuid5(ns, context_id)`, proposed
   when the first run opens the task and addressed as a continuation by every answer
   that follows. This is ADR-0008's deterministic id keyed by the thing that is
   deterministic here. The orchestrator's key is the tool call because LangGraph replays
   it; agent-runner has no outer graph and no replay, so its key is the conversation.

   **The context, and not the run.** A resumed run can park again — one authorization
   leading to another — and each link is a new run row on the *same* context and the
   same `{ctx}::dynamic-{name}` thread, so there is one sub-agent task for the whole
   chain. Keying on the run produced an id nothing had ever opened as soon as a second
   answer arrived, and the resume died on `Task ... not found` with the chain
   unfinishable. One context is one sub-agent conversation: a scheduled run has a single
   sub-agent, and every run of a job gets its own context.

2. **`AuthErrorDetectionMiddleware` moves to `agent-common` and joins the
   sub-agent stack; nothing else may park.** Gap 1 is upstream of everything
   else: without the `KIND_AUTH` interrupt there is nothing for `interrupt_status`
   to classify. The middleware is not modified — the headless case needs the
   *same* interrupt, and what differs is who answers it.

   agent-runner passes no `risk_scorer` and no `hitl_guarded_tools`, so
   `build_sub_agent_graph` installs no `ConditionalHumanInTheLoopMiddleware` and
   no HITL interrupt can fire. That is a correctness requirement of this design
   rather than a configuration choice, and it is enforced structurally (see
   Constraints), because after this decision a stray risk scorer no longer raises
   an interrupt nobody publishes — it produces a real `input_required` task
   waiting for an approval that will never come.

3. **agent-runner installs its own durable task store — on BOTH of its handlers.** ADR-0008's constraints
   are explicit that its decision 7 holds only because the task record is
   reachable from every replica. agent-runner's `InMemoryTaskStore` would lose a
   parked run on the next restart, and agent-runner is the service that gets
   OOMKilled. A durable store is a precondition of parking, not an optimisation.

   A parked run leaves two task records, and they are not interchangeable: the OUTER
   task belongs to agent-runner's HTTP request handler and is what the owner's answer is
   addressed to by id, while the INNER task belongs to `LocalA2AServer` and is the
   sub-agent's. Making only the inner one durable looks right for as long as the process
   lives and fails at precisely the moment this decision exists for — a restart between
   the park and the answer, where the sub-agent's task is found and the outer one is
   gone, so the resume dies on `Task ... not found` and the job stays stopped for good.
   One store, installed for both.

   It is *its own*, not the orchestrator's. Both services already default to the
   same `POSTGRES_SCHEMA` against the same database, so a `DatabaseTaskStore`
   configured by default would silently share the orchestrator's table and let
   either service `tasks/get` and `tasks/cancel` the other's tasks. ADR-0008's
   shared-store constraint is about replicas of one service, and extending it
   across two is a trust boundary this decision does not need (see decision 6).

4. **A run blocked on authorization ends as `JobRunStatus.AUTH_REQUIRED`, which
   is neutral about the job and holds its schedule.** It does not touch
   `consecutive_failures` and never auto-pauses the job. Like `INTERRUPTED` it is
   a statement about the runtime rather than about the job — but unlike
   `INTERRUPTED` it earns no retry, because retrying in sixty seconds cannot
   conjure a credential. The thing that unblocks it is a person, and people do not
   arrive on `retry_at`.

   It also stops the job being claimed until it is answered. `claim_due_jobs`
   already skips a job while any of its runs is `running`, which is what stops
   runs of one job overlapping; a run awaiting authorization joins that predicate.
   The alternative — advancing `next_run_at` and letting each occurrence park in
   turn — was the original shape of this decision and it is wrong in every
   direction. Each occurrence pays for a full dispatch, graph build, tool
   resolution and model calls before failing at the same tool, so a blocked
   hourly job burns a hundred and sixty-eight partial runs a week and produces
   nothing. Each leaves a non-terminal task and a live checkpoint nobody can
   reach. And each posts its own answerable card, so the owner who presses three
   of them gets the job's work done three times. Holding the schedule makes all
   three impossible by construction rather than by a dedupe rule, a supersession
   sweep and a cancel path.

   A job stalled by an owner who neither authorizes nor declines is the cost, and
   it is paid in the open: the run sits at `AUTH_REQUIRED` on the job detail page
   with the ask rendered beside it, and declining resumes the schedule at once.
   A job with no stored offline token already stops until its owner acts, so this
   is a shape the product has.

5. **The ask travels in the payload the delivery channel already parses.**
   agent-runner publishes its *outer* task as `auth_required` — non-terminal on purpose,
   see decision 6 — and the push sender fires on that status event, since it fires per
   event and not only on terminal states. Part zero of the status message stays the
   scheduler-payload JSON, gaining `scheduler_status: "auth_required"`, the
   `AuthPayload.client_payload()`, and decision 6's reply target.

   It is carried *inside* that payload rather than replacing it because there is no
   extension negotiation on a push notification: a webhook POST is blind, and every
   receiver reads the bytes with whatever code it has. Each client parses part zero as
   that JSON and gives up silently when it cannot, so a status message shaped by the
   auth extension alone would put prose there and the whole notification would vanish in
   three clients at once. `agent_message` therefore carries prose with the authorize URL
   in it literally: an upgraded client renders its card, an un-upgraded one posts a
   sentence with a working link, and nobody drops anything.

   **The ask must say what and why**, because unlike an interactive one it arrives cold,
   hours later, with no context but itself. That means naming the tool that needed the
   credential and the job that stopped — neither of which the payload carried at first,
   since the parsed auth payload dropped the tool and the dispatch carried only the job's
   id.

   **The ask is not adoptable.** A parked run would otherwise acquire two adoptable
   messages — the ask, and later the result — both pointing at one
   `{run_ctx}::dynamic-{name}` thread. ADR-0008 keys an adopted sub-agent's memory by the
   run only because "a run is adoptable exactly once", and names two conversations
   interleaving turns in one sub-agent's memory as what breaks it. So the invariant is
   sharpened to **one *adoptable* notification per run**, and the ask is skipped exactly
   as `condition_not_met` already is. Nothing is lost: a prose reply could not resume the
   run anyway, since it arrives as new work on a parked thread and is rejected.

   **Whatever the resumed run produces is delivered into the ask's thread** — including a
   second ask, when one authorization leads to another — so a chain reads as a chain
   rather than as loose notices the owner must connect. The ask's coordinates are known
   only to the client that rendered it and only when it is clicked, so they travel with
   the answer and come back untouched on the result; everything in between treats them as
   opaque.

   **The console shows the same ask and can answer it**, rendering it from the payload
   stored on the run rather than pointing the owner at a chat message to go and find. A
   scheduled job is set up and then lived with from a chat client, so the console is the
   second surface and never the only one — but an ask that can only be answered elsewhere
   is an ask that waits.

   A channel that cannot complete the round trip at all — email — degrades to the link:
   the owner authorizes out of band and the next occurrence succeeds once the run is
   closed. Slower, not broken.

6. **Authorization resumes the run through console-backend, addressed to the
   parked outer task.** The card's answer posts the same
   `{"authorization": {...}}` DataPart the client already builds for chat, to a
   target the payload declared. console-backend re-resolves the owner's offline
   token and dispatches to agent-runner with `message.task_id` set to the stored
   outer task id, under the same attribution scope as the original run. The outer
   task is non-terminal, so the handler accepts the message; `_stream_impl`
   recognises an authorization answer, addresses the derived inner task id as a
   continuation, and `local_server.resume.build_resume_command` delivers it to the
   pending interrupt — which already reads `{"authorization": ...}` natively. A
   declined authorization travels the identical route: the agent is told to stop,
   and the run closes on its own terms rather than staying parked on a question
   that has been answered.

   **The reply target is declared, not hardcoded.** A pushed A2A Task carries no
   statement of where its server lives — `pushNotificationConfig` addresses server to
   client and nothing addresses client back — and a chat client answers the orchestrator
   only because that is the single A2A server it is configured with, which is right for
   chat and wrong here. So the ask says where its answer goes. It rides the **scheduler
   payload**, not `AuthPayload`: where to send an answer is a property of the parked
   task, not of the authentication method, and `AuthPayload` is shared with the
   interactive path, which needs no such target. Clients **validate the declared target
   against their own configuration** rather than posting to whatever arrives, since a
   blindly trusted reply target taken off a webhook is a credential-forwarding
   primitive.

   The target is a control-plane URL, not an A2A endpoint. An agent card exists so
   something can discover a capability and delegate to it, and nothing will ever discover
   or delegate to the scheduler. The chat clients already hold exactly this kind of
   contract with console-backend for feedback and delivery-channel self-registration;
   resuming a run is run lifecycle and belongs on that wire.

   **Two routes that look more natural were rejected.**

   *The client answering agent-runner directly* is the protocol-natural one, since
   agent-runner owns the task. But nobody would create a run row when the resume
   starts, nothing would heartbeat it, and the healer would have nothing to sweep:
   an agent-runner that dies mid-resume would take the run with it and leave no
   record it was ever attempted. Having agent-runner call console-backend itself
   does not fix that — it needs three control-plane calls instead of one, moves
   the same coupling one hop out of sight, makes the executor responsible for
   recording its own death (the separation ADR-0007 exists to enforce), and
   widens agent-runner's caller set from one internal service to three
   end-user-facing clients past its `expected_azp` check. A resumed run is a run,
   and runs are dispatched by the component that knows how to survive their death.

   *The answer travelling `handleIncomingMessage` into a chat turn* is where an
   interactive card's answer goes, and it does not work here — though the reason
   usually given for that is weaker than it looks and should not be relied on.
   The naive version fails loudly: `is_continuation` is `context.current_task is
   not None`, the orchestrator's dispatch proposes a fresh
   `uuid5(conversation, tool_call_id)`, and the executor sees a new task on a
   thread with a pending interrupt and returns `TASK_STATE_REJECTED` — ADR-0008's
   decision 4 working as intended. But a dispatch that recognised a parked run and
   addressed its **existing** task id would work, and it is cheaper than it
   sounds: both services already share a checkpointer, and decision 3 gives
   agent-runner a database task store anyway, so pointing both at one table is all
   it would take. The real objections are elsewhere. A scheduled resume is
   deterministic — the click knows the run, the task and the interrupt — and
   routing it through the orchestrator turns that into a prompt whose text is
   `authResumeText`, with the resume then depending on a model choosing to
   delegate. The scheduler loses the run: the row never leaves `AUTH_REQUIRED` and
   the spend moves from the job's attribution to the conversation's. And one task
   table across two services lets either cancel the other's tasks, to carry a
   button click.

7. **The resumed work is a new run, `RunTrigger.RESUMED`** — and a resumed run may park
   again, which is the case that separates two ids people assume are one. One
   authorization can lead straight to another, and then the run whose parked task is
   being CONTINUED is no longer the run the payload should correlate to: the task id
   derives from the former, while the result and, above all, the reply target of the
   follow-up ask must name the latter. Conflating them made the second card address a
   run that had already been answered, so pressing it was refused as "no longer waiting"
   and the chain could not be continued at all. The parked run closed
   as `AUTH_REQUIRED`, which is terminal, and the resumed work has a result to
   deliver and spend to attribute; reopening a closed row would contradict a status
   the console already showed. `RESUMED` joins `SCHEDULED`, `RETRY` and `MANUAL` as
   a recorded reason rather than a parameter one code path receives, and it behaves
   like `SCHEDULED`: one fresh attempt if it is interrupted, no effect on
   `consecutive_failures`. It does not advance `next_run_at`, which the parked run
   already did.

   It earns that retry for a reason worth stating, because the opposite is
   tempting: by the time a resumed run dies, the credential is stored at the
   gateway, so a fresh attempt does not hit `need-credentials` at all and recovers
   the run's actual purpose. Repeating side effects from the dead attempt is the
   cost ADR-0007 accepted knowingly for every interrupted run, and this is not a
   new risk class. When the retry is granted the parked task it supersedes is
   cancelled, since nothing will answer it and answering it would duplicate work.

8. **A watch's check tool parks the run the same way, without an agent-runner task.**
   A watch's condition is evaluated by the scheduler *before* any dispatch
   (`WatchEvaluator`), so a `need-credentials` from the check tool never reaches
   agent-runner and decisions 1–3 never see it. The first cut of this decision did not
   name that path, and it kept the old behaviour in full: a failed run with the gateway's
   raw payload — authorize URL included — as its message, nothing delivered, and a
   failure counted toward `max_failures`.

   The evaluator now recognises the payload on the same field the middleware matches
   (`errorCode: need-credentials`, never the words) and hands the engine an *ask* rather
   than an error. The run ends `AUTH_REQUIRED` with the ask on `parked_payload` in the
   `client_payload` shape, so the console's card and the schedule hold of decision 4
   apply unchanged. What differs is what the answer is addressed to. There is no task,
   so `parked_task_id` carries `watch-check:<run id>` instead: the column is what makes
   a park answerable in every place that checks (the claim, run-now's refusal, the
   console's badge), and one prefix read by the resume is cheaper than teaching each of
   them a second column. An approval re-runs the poll at once as the RESUMED run — with
   the credential stored, the check proceeds and the watch carries on; without it, the
   run parks again, having consumed the previous ask first, so there is still one. A
   decline releases the schedule without counting a failure, and the next occurrence
   asks again.

   A job with a delivery channel is told there too, with the link, as a plain
   notification dispatched through agent-runner's push sender — decision 5's
   un-upgraded-client path, deliberately: the outer task that notice completes is not
   parked, so a card posted against it would have nothing to answer. The answer comes
   back through the console.

## Constraints

- **Both parks are answered through the one resume endpoint, and the prefix is the
  only thing that tells them apart.** `is_check_park` reads `parked_task_id`; nothing
  else may branch on the status or on the job type, because a watch with an agent can
  park either way — on its check, or later on a tool the agent calls.
- **Only one run of a job is ever parked, on every dispatch path.** Decision 4's
  schedule hold makes that true for scheduled occurrences, and several things depend on
  it: no second answerable card, no accumulation of non-terminal tasks and checkpoints,
  no supersession rule. But the hold works through `claim_due_jobs`, and **run-now
  bypasses the claim entirely** — so a few presses left several parked runs each holding
  a live ask, which surfaced as an authorization card that would not go away because it
  belonged to an older run than the one just answered. Run-now therefore refuses while
  the job has an answerable park. A *resumed* run that parks again is not a second park:
  it consumed the ask it was answering before it began, so there is still exactly one.
  Answerability is `parked_task_id`, never the status, which stays `auth_required` for
  good as a true record of how that occurrence ended.
- **Nothing is resumed that was not parked.** ADR-0007's rule stands: an
  *interrupted* run is never resumed from a checkpoint, because a run that died
  mid-flight has no trustworthy position. A *parked* run is the opposite case —
  it stopped deliberately, at a known point, with its position written down. The
  two must not be conflated in the recovery paths.
- **Only `KIND_AUTH` parks, and it is enforced rather than assumed.**
  `build_sub_agent_graph` installs HITL only `if hitl_guarded_tools or
  risk_scorer`, and `DynamicLocalAgentRunnable` passes both straight through from
  constructor arguments defaulting to `None`. So the property currently rests on
  an omitted keyword argument that the next person copying the orchestrator's
  construction will supply. agent-runner refuses to publish a `KIND_HITL` pause,
  so the guard sits at the boundary rather than at the construction site.
- **Unattended runs execute sandbox code unguarded, and that is a posture, not an
  oversight.** With no risk scorer, PTC's `eval` takes the unguarded path, so a
  sandbox-enabled scheduled sub-agent runs code that calls tools with no per-call
  approval. This ADR does not change it, but it is where the question becomes
  live, because parking on an approval is now technically available. It is not
  taken because the ask machinery does not generalise: a missing credential is a
  property of the owner's authorizations and is fixed once, whereas a per-tool-call
  approval has no such key, and a job that parks on every risky call is unusable
  against an unresponsive owner. Guarding unattended runs deserves its own
  decision.
- **The task store must be separated deliberately.** The default configuration of
  both services points at one schema in one database, so decision 3's separation
  is something that has to be done rather than something that happens.
- **A blindly trusted reply target is a credential-forwarding primitive.** The
  declared target of decision 6 arrives over a webhook, so every client validates
  it against its own configuration before posting anything to it.

## Consequences

- A scheduled job that needs a credential asks for it, keeps its place, and resumes
  where it stopped, instead of paraphrasing the refusal into a green run or spending
  its `max_failures` budget on a condition no retry can fix.

- **agent-runner's sub-agent construction converges with the orchestrator's.** That is
  the larger half of the work and not only a task-lifecycle change: agent-runner stops
  assembling a graph and adopts `DynamicLocalAgentRunnable`, so one path builds every
  local sub-agent. Five behaviours were agent-runner's alone and had to survive the
  move — the stall watchdog, the tool-discovery failure policy, the per-turn model-call
  budget, the stateless catalogue listing, and channel-aware message formatting. Three
  of them moved into the shared path and now benefit the orchestrator's delegated turns
  too; the budget became a constructor argument, because an unattended run *should* get
  a larger one than a delegated turn and that was previously incidental rather than
  expressible.

  The convergence also changes **how a crash arrives**, which is easy to miss and
  matters here more than anywhere: `LocalA2ARunnable.astream` catches every exception
  and yields an `ErrorEvent` rather than raising, so the executor's `except` branch —
  until now the only writer of a failed status — is no longer reached for a local
  sub-agent. A run's status therefore follows the sub-agent's terminal task state, not
  the absence of an exception. Without that, this decision's own refactor would have
  recorded every crash as a success, which is the failure it exists to abolish.

- **`agent-runner/agent/mcp_tools.py` is left unwired.** Its listing moved into the
  shared path; it is kept one release only because its tests still document the
  console/gateway audience split and that a failing token exchange must fail discovery
  rather than degrade to zero tools. Port those, then delete it.

- **An empty tool whitelist means "everything" for the general-purpose agent.** The
  orchestrator has always read it that way and agent-runner read it as "nothing", so
  the same agent had opposite capability depending on who started it — and said so, by
  reporting that the tools it was asked to use do not exist. Scheduled runs now apply
  the same rule, handing the catalogue over lazily rather than binding it. Strictly
  outside this decision, and fixed here because a scheduled run cannot reach a
  credential-gated tool at all without it.

- **A job that auto-pauses tells its owner.** Crossing `max_failures` disables the job
  in SQL and announced nothing, so it simply stopped producing — the symptom an owner
  is least likely to notice. It cannot ride the delivery channel, which is optional and
  absent on exactly the jobs whose silence goes unnoticed, so it is a durable console
  notification (`NotificationType.SCHEDULED_JOB_PAUSED`).

- **The console gains a run outcome it must render.** `AUTH_REQUIRED` is neither a
  success nor a failure, and showing it as either is how this gap stayed invisible. It
  is also the first status whose badge cannot be derived from the status alone: a run
  keeps `auth_required` after being answered, so "waiting" is `parked_task_id`, never
  the status.

- `scheduled_job_runs` gains `parked_task_id` and `parked_payload` (JSONB). Named for
  the mechanism rather than today's only instance: a run parks because its agent stopped
  on a question, and the run's *status* says which question. Only `KIND_AUTH` parks now,
  but the task, the payload and the resume path are indifferent to the kind.

- **The scheduler still learns a run's outcome by holding an SSE stream, and the
  protocol treats that stream as disposable.** A2A makes the *task* durable and offers
  `tasks/get`, `tasks/subscribe` and push notifications as three ways back to it; the
  held stream is a pre-protocol habit. This decision leans on the durable half —
  `message/send` to a parked task works cold, from a replica that never saw the original
  run — which is why decision 6 continues the outer task rather than correlating one.
  The protocol-native fix for the other half is for console-backend to register itself
  as a second push target and stop holding anything; that would retire the held stream
  for all runs and is a larger decision than this one. Named here so the held stream is
  not read as intentional.

- **A parked run has no expiry.** A job whose owner never answers stays stopped. Closing
  a parked run after some age, releasing the schedule and letting the next occurrence ask
  again, is the natural follow-up and is deliberately not in this cut.

- **`delivered` on a run is inferred, not observed.** `_finalize` sets it from whether
  the job has a delivery channel, while the clients give up on three conditions with a
  warning and a bare return. An ask that was never delivered is therefore recorded as
  delivered, which matters more for an ask than for a result, because the run is waiting
  on it. Making the flag truthful needs a delivery outcome reported back from the
  clients; it predates this decision and is tracked separately.

- HITL approvals in scheduled runs become *representable* — the same executor would
  publish them as `input_required` with the human-in-the-loop extension. That is a
  hazard rather than a feature, and the Constraints say what stops it.
