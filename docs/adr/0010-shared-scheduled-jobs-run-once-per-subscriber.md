---
status: proposed (2026-09-17); implemented in console-backend, the console, the
  task-scheduler prompt and the orchestrator's origin frame, pending review
---

# Shared scheduled jobs run once per subscriber

## Context

Sub-agents can be shared to groups and auto-activated for members; scheduled
jobs cannot be shared at all. Onboarding a team therefore means every member
authoring the same job by hand, and a manager who wants "the whole group gets
the Monday report" has no lever. The obvious fix — a `shared` flag on the job —
runs into the fact that one `scheduled_jobs` row today bundles five concerns:
what the job does, when it fires, whether it is on, *whose identity it runs
under* (the owner's vaulted offline token, bypass rules and spend), and where
its result goes. Sharing the row shares all five, and the fourth cannot be
shared: a run must never hold authority its recipient lacks (on-behalf-of
identity, ADR-0002), and an authorization ask (ADR-0009) must reach the person
whose credential is missing, not whoever authored the prompt.

## Decision

1. **A job splits into a shareable definition and per-user subscriptions.**
   The *definition* is what the job is — prompt or watch check, sub-agent,
   condition, `max_failures`, trigger defaults, trigger policy — owned by one
   user and shared to groups with `read`/`write` exactly like a sub-agent. A
   *subscription* is one user's activation of it — enabled state, the trigger in
   force, delivery target, run bookkeeping, `last_check_result`. A definition
   never runs; only subscriptions dispatch.

2. **Every subscription runs under its subscriber's identity.** Their offline
   token, their bypass rules, their spend, their delivery target, their auth
   asks. N subscribers means N runs. The single owner-run with fan-out to N
   recipients was rejected: it hands every reader data fetched with the owner's
   credentials, makes per-user trigger times impossible by construction, and
   can only ever ask the owner for a missing credential.

3. **The owner is just another subscriber.** Creating a job creates the
   definition and the creator's own subscription. There is no owner-special run
   path; the pre-existing single-row job becomes one definition plus one
   subscription of the same user.

4. **Triggers propagate by inheritance, everything else by reference.**
   Definition fields are live for every subscription on its next run — nothing
   is copied. A subscription's trigger is either *inherited* (follows the
   definition's defaults, including later edits) or an *override*. A
   definition's *trigger policy* is `overridable` or `fixed` (watches default to
   fixed — the tick is part of what a watch means). "Broadcast the new defaults
   to everyone who hasn't customised" is therefore what inheritance already does;
   an explicit *reset to defaults* clears overrides — a writer's, over every
   subscription, or a subscriber's own, over theirs. Inheritance is a door that
   opens both ways: leaving the default must not be easier than returning to it,
   or a subscriber drifts out of it by accident and stays there. `enabled` never
   propagates.

5. **"One run, many readers" is a delivery concern, not a sharing concern.**
   A team summary is the owner's single subscription posting to a shared space
   — one run, owner's identity, owner-chosen audience, the same disclosure act
   as a person posting a report. A *delivery target* (channel plus recipient;
   today's DM-only becomes the default) is reserved now and implemented later.
   Fanning one run out to N DMs is deliberately not offered. The target is also
   what an *activation notice* uses: a member auto-subscribed by a group default
   is told in the console and again where their results will land, because the
   console is not where most of them live.

6. **Definitions are not versioned.** Sub-agent versions exist for an approval
   flow and for things that pin to a version; jobs have neither, and a subscriber
   pinning a version would recreate the broadcast problem decision 4 avoids.
   Field history is already in the audit log. A monotonic `revision` on the
   definition is stamped on each run so a run can say which definition produced
   it. If a per-subscription *standing authorization* is ever built (planned,
   not shipped), the fact that a writer's edit can widen what a subscriber
   consented to will need an answer; the revision stamp is one available hook,
   not a decision taken here.

## Considered options

- **`shared` flag on the existing row, owner runs, results fanned out.**
  Cheapest; rejected on identity grounds above.
- **Copy-on-share** (each member gets an independent job). No propagation of
  fixes, N divergent prompts within a week. Kept as an explicit *copy* verb for
  users who *mean* to diverge, not as the sharing mechanism.
- **Versioned definitions with subscriptions pinned to a version.** Rejected in
  favour of decision 4; see decision 6.
- **Separate MCP tools for "update my subscription" and "update the
  definition".** Rejected: it pushes a distinction onto the model that the user's
  sentence ("move the report to 8") does not carry. One update path routes each
  field server-side; only the trigger is ambiguous, only once a second
  subscriber exists, and only then does an optional `scope: mine | everyone`
  mean anything. `scope` also decides what *unchanged* means: the console
  resends every trigger field prefilled, so an echo must not count as an edit —
  but the values it echoes are the subscriber's effective trigger, while
  `everyone` edits the definition's default. Each scope is therefore judged
  against the trigger it aims at. Judging both against the effective one (the
  first implementation) made "promote my own schedule to the default"
  inexpressible *and* let an echo of the default rewrite it, because an editor
  with an override differs from both.

## Consequences

- The scheduler's dispatch changes from claiming jobs to claiming
  subscriptions joined to their definition, taking identity from the
  subscription. `scheduled_job_runs` hangs off the subscription; downstream
  clients key on the run id and are unaffected.
- ADR-0009's "one outstanding ask per (job, service)" and "a parked run holds
  the schedule" become per subscription. A parked run is a subscriber's run.
- Access to the referenced sub-agent is checked per subscriber at every
  dispatch (it can be revoked after sharing); a definition may not be shared to a
  group whose members cannot reach its agent, and sharing never grants agent
  access as a side effect.
- The split is a storage fact, not a UI concept: an unshared job stays one
  form, and a second subscriber surfaces exactly one extra choice on one page.
  Two verbs replace today's "pause": *suspend* (definition, `write`, stops all
  runs, preserves each member's `enabled`) and *disable* (subscription, mine).
- Chat parity: the task-scheduler agent gains subscribe, copy, share, group
  default, suspend and reset-overrides, all behind the standard approval layer
  (share and group default are the same class — a grant on behalf of others —
  and are exposed together or not at all), plus one *read* tool on groups so an
  approval card can say "activate for N members of <group>". `is_public` is not
  exposed to the model. The agent's system prompt is a DB seed, so the
  vocabulary ships as a prompt migration in the style of 085 — the last piece of
  the work — and the orchestrator's routing block learns "share / subscribe a
  job".
- Replying under a delivered run (a threaded reply in the bot's DM) adopts the
  subscriber's own run, unchanged; requests in that conversation follow the same
  server-side routing, and the conversation-origin frame carries `shared_by` /
  `activated_by` so the model can answer "why do I get this". Those two are
  *resolved by the orchestrator from console-backend* under the authenticated
  user's token, alongside the ownership check adoption already makes — not
  carried in the client's DataPart. Who shared a job with whom is precisely the
  claim a forged origin would want to make, and the alternative cost a schema
  change in three delivery clients' provenance stores to be less trustworthy.
- The delivered result of a shared job carries one provenance line, appended by
  agent-runner from a dispatch-metadata field rather than by each delivery
  channel: one seam where every run's output is already composed.
- *Reset to defaults* (decision 4) exists in both halves: a writer resets every
  subscription, and a subscriber puts **themselves** back on the default without
  needing permission from anyone. The second was initially left out, on the
  reasoning that a member who customised their time could wait for the owner.
  QA showed the asymmetry is not survivable: an override is reached by ordinary
  editing — including an agent that sets a time and then undoes it — so leaving
  it was a one-way door out of inheritance, after which the owner's later changes
  to the default silently stopped arriving. Nothing said so, and the only way
  back moved everybody. A `null` trigger on the update path is *refused* rather
  than accepted-and-ignored, so "clear my schedule" cannot look like it worked.
- Cost scales with subscribers rather than with definitions. That is the honest
  price of per-user data, and the shared-space delivery target (decision 5) is
  the sanctioned way to pay it once when the data is genuinely shared.
