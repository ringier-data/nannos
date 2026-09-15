---
status: accepted (2026-09-11)
---

# Local sub-agents run behind an in-process A2A server; every delegation is an A2A task

## Context

Nannos treats A2A as the contract between the orchestrator and its sub-agents. Until
now that was true of the *vocabulary* everywhere and of the *task lifecycle* only at
the HTTP boundary. A remote sub-agent was reached through `A2AClientRunnable`: a
message opens a task, the task's state says whether it finished or is waiting for
someone, and the answer to a paused task is the next message addressed to it. A local
sub-agent — a LangGraph graph in the orchestrator's own process — was reached through
a parallel implementation that shared only the response envelope:

- the dispatch middleware **probed the sub-agent's checkpoint** for `__interrupt__`
  pending writes before every call, to tell a replay from a fresh delegation;
- the sub-agent's graph **re-raised `GraphInterrupt`** after its stream so the
  dispatch could catch it and call the orchestrator's `interrupt()`;
- the dispatch **built the sub-agent's `Command(resume)`** itself — id-keyed map,
  blanket-decision replication, answer/question alignment — for a graph it does not
  own;
- results travelled as a **JSON envelope inside the message text**
  (`{"content": ..., "a2a": {...}}`), with two vocabularies for one task state;
- because a sub-agent's memory is one checkpoint thread per (conversation, agent),
  **two delegations to one agent in one assistant message were refused** up front,
  with a tagged refusal message that three consumers had to know to skip and a
  prompt rule telling the model not to do it;
- a conversation adopting a **scheduled run** continued a remote agent's run by
  sending its context id, but a local agent's run by **copying its checkpoint**
  across a shared Postgres schema (`adopt_thread_from` + fork-on-adopt), because a
  raw context id would have desynchronised the probe from the execution thread.

The probe was where PR #161's regression came from; the dispatch middleware had grown
to 2,800 lines, most of it a hand-rolled task lifecycle. The upstream deepagents
discussion (langchain-ai/deepagents#6262, "supervisor over a2a") makes the same
point from the other side: the value of A2A first-class in a supervisor is not the
transport, it is that the middleware becomes protocol-aware — interrupts, task ids
and states cross the boundary instead of being lost at it.

## Decision

1. **Every local sub-agent is served by the A2A SDK's own machinery, in-process.**
   `agent_common.a2a.local_server` wires a `LocalSubAgentExecutor` (an
   `AgentExecutor`) behind a `DefaultRequestHandler` with a `TaskStore`.
   `LocalA2ARunnable.astream` is now a *client*: it converts the input to an
   A2A message, calls `message/stream` on that handler without an HTTP hop, and
   translates the events with the same `A2AStreamTranslator` the remote client uses.
   The graph is driven by `astream_graph`, which the executor (and the orchestrator's
   embedded execute-only path, which *is* the A2A server for that sub-agent) calls
   directly. A2A is the protocol; HTTP is one transport, in-process another.

2. **The executor owns the sub-agent's task lifecycle.** It reads the graph's own
   state to decide whether a message opens a task, continues the agent's
   conversation, or answers a paused interrupt; it builds the id-keyed
   `Command(resume)` and fits the answer to the question actually pending
   (`local_server.resume`); it publishes a pause as `input_required` /
   `auth_required` with the extension vocabulary any Nannos client already reads
   (`agent_common.a2a.extensions`, moved from the orchestrator) and the raw
   interrupts on the status event's metadata. The checkpoint probe, the
   `GraphInterrupt` catch-and-reraise dance, the resume builder and the JSON envelope
   are gone from the orchestrator.

3. **Every delegation is its own A2A task, named after the tool call.** The dispatch
   proposes `uuid5(conversation, tool_call_id)` for a new task
   (`SubAgentInput.proposed_task_id`, honoured by `ProposedTaskIdContextBuilder`
   when no task by that id exists). A LangGraph replay of an interrupted tool call
   therefore finds the task it opened with `tasks/get`, sees it parked, and
   delivers the user's answer to it as a message — the replay detector is the task
   store, not the sub-agent's checkpoint. Continuity is still `a2a_tracking`'s: a
   live `task_id` there means the message continues that task.

4. **Same-agent delegations in one message run, one after the other.** The
   executor serialises graph runs per conversation thread; the second call waits and
   runs as a follow-up on the agent's conversation. The only refusal left is
   server-side and A2A-native: a *new* task arriving while the thread is parked on a
   question is `rejected` with an explanation (its description is work, not an
   answer). The refusal tag, its three skip sites and the prompt rule are removed.

5. **One thread convention, one adoption mechanism.**
   `local_sub_agent_thread_id(context_id, agent_name)` = `{ctx}::dynamic-{name}` is
   used by the orchestrator's dispatch, its embedded path and agent-runner alike, so
   a conversation adopting a scheduled run seeds `{"context_id": <run ctx>}` for
   local agents exactly as for remote ones and the next delegation lands on the
   run's own thread. Fork-on-adopt and the shared-database copy are removed. A
   thread whose last turn died mid-tool is sealed in the *next turn's input*
   (`seal_dangling_tool_calls`), never by editing the checkpoint.

6. **Local tasks live in the orchestrator's task store.** `main.py` installs the
   same (Postgres-backed) store the HTTP handler uses, so a delegation parked on an
   approval survives a restart like any other task.

7. **The SDK's live task object is a cache, never the truth.** The request handler
   keeps an `ActiveTask` (a producer task, a consumer task and their event queues) per
   task it has touched, in a process-local registry, and after `input_required` that
   object idles waiting for a follow-up. Nothing depends on it arriving there: a
   resume is rebuilt from the task store and the graph's checkpoint alone (the
   executor's `local_server.resume`), which is also what happens when the follow-up
   lands on another replica. So `LocalA2AServer.send` closes the SDK's subscription
   and *releases* the task's live object as soon as its stream ends — parked or
   finished, drained or abandoned — instead of letting it wait for garbage collection.
   Closing it deterministically is what keeps its finalisation on the event loop
   (the alternative surfaced as a burst of "Task was destroyed but it is pending" /
   "Token was created in a different Context" when a Rust-backed GC thread finalised
   the orphaned coroutines). `LocalA2AServer.aclose()` cuts off whatever is still live,
   for a host tearing the server down.

## Constraints

The protocol makes only the **task** durable (id, context, status, history,
artifacts); a live subscription is ephemeral by design, which is why it offers
`tasks/get`, `tasks/subscribe` and push notifications as three ways back to a task a
client lost. The SDK splits along that line: `TaskStore` and the push-config store
have database implementations, the `ActiveTask` registry is a per-process dict and the
`queue_manager` argument of `DefaultRequestHandler` (v2) is accepted for backward
compatibility and never read. Two things follow for anyone deploying more than one
orchestrator replica:

- **`tasks/subscribe` and `tasks/cancel` on the orchestrator's own A2A server need
  routing by `task_id` to the replica running the task**, or push notifications in
  place of a held subscription. A cold replica creates an empty `ActiveTask` for
  `subscribe` and waits on a queue nobody writes to; for `cancel` it cancels a producer
  that is not there. `message/send` to a parked task works cold, because the executor
  resumes from durable state. Cancel is the one operation with no durable fallback.
- **The local task store must be the shared one.** Decision 7 holds only because the
  task record and the checkpoint are reachable from every replica; the in-memory
  default `get_local_task_store` falls back to when nothing is installed is for tests.
  `main.py` installing the Postgres-backed store (decision 6) is a precondition, not
  an optimisation.

## Consequences

- One HITL mechanism for local and remote sub-agents at the transport level; the
  orchestrator still surfaces a *local* sub-agent's interrupt as its own `interrupt()`
  (structured approval card) because only a local task's id is deterministic across
  a replay. Remote agents' pauses stay model-mediated, as before.
- The dispatch middleware loses ~600 lines of lifecycle code and no longer knows
  the sub-agent's thread name.
- `MockSubAgent` in the orchestrator's mock tier travels the real in-process server,
  so routing tests exercise the production lifecycle.
- **Scheduled runs executed before this change** live on bare `{ctx}` threads and
  can no longer be adopted into a conversation (the run's conversation starts blank,
  the degradation the fork already had for pre-contextId runs). Accepted as a
  one-time cost of a single convention. A record left by the *old* mechanism
  (`adopt_thread_from`) is read as the run's `context_id`, since the key now means
  what `context_id` means — so a conversation adopted before the deploy keeps the
  run's history instead of silently starting blank.
- **An adopted sub-agent's memory is keyed by the RUN, not by the conversation.**
  The seeded `context_id` *is* the thread, so the conversation id does not enter
  it: any conversation adopting a given run lands on the same
  `{run_ctx}::dynamic-{name}`. Per-conversation isolation is the one thing
  fork-on-adopt provided that the seed does not, and it is unnecessary only
  because a run is adoptable exactly once — `job.delivery_channel_id` is singular
  (one notification per run), adoption is a reply in that notification's thread
  (one conversation), and `_validate_scheduled_run_origin` re-resolves the job
  under the authenticated user's token (another user's job 404s). A job that could
  notify several channels would break this and let two conversations interleave
  turns in one sub-agent's memory; `test_delegation_lifecycle_regressions` and the
  Postgres test named there are where that surfaces.
- The in-process executor could be mounted behind HTTP (agent-runner serving
  interactive sub-agents) without further change to the sub-agent side. That is a
  byproduct, not the point of this decision: the case against it is per-turn
  latency, and its dominant term (a cold MCP discovery per delegated turn) was
  measured earlier and is what the orchestrator's per-user discovery cache removes.
  The HTTP path itself is unmeasured (see orchestrator `AGENTS.md`).
