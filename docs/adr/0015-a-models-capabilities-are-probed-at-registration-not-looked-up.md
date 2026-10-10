---
status: accepted (2026-09-30), nannos#318; extends ADR-0014
---

# A model's request-shape capabilities are probed at registration, not looked up

Whether a model accepts the request shapes the harness sends — a forced `tool_choice`,
`response_format`, an explicit thinking-off switch next to tools, a replayed signed thinking
block — is established by **sending it those shapes when it is registered**, and the answer is
**recorded on the deployment** (`model_info.nannos_capabilities`). The shapes are defined once,
in `ringier_a2a_sdk.model_capabilities`, and that one definition is what the registration probe
replays, what the gateway hook reads on the deployment that serves a request, and what the app
reads for the alias it is about to call. No consumer derives a capability from a model's name,
its provider, or a provider's published capability map.

A shape the harness has no alternative for (tools with `tool_choice: auto`, the tool-result
round-trip, streaming with tools) failing **refuses registration**. A shape the harness can route
around failing is **recorded, not refused**; the record is what lets the harness route around it.

## Why

- **Only a live call answers the question.** Claude Sonnet 5.5 rejects a forced `tool_choice`
  while LiteLLM's model map said `supports_tool_choice: true`; an Azure `gpt-6` deployment rejects
  function tools with reasoning on Chat Completions while nothing in any map says so. Both passed
  the four-token ping and broke the first user turn. The map is a claim about a model family; the
  probe is an observation of *this* deployment through *this* gateway, which is the only thing that
  will serve the traffic.
- **The map fix depends on a network fetch.** LiteLLM v1.103 downgrades a forced `tool_choice`
  and drops `thinking: disabled` for models its map flags — but the bundled map lags releases, and
  the current map is fetched from GitHub at proxy startup, best-effort. When that fetch fails the
  proxy silently falls back to the bundled map and the 400s return. A record written at registration
  and read from the proxy's own database has no such dependency.
- **Failover makes the alias the wrong key** (ADR-0014). Under a tier-group chain the request the
  app built for `claude-sonnet-5-5` can be served by another deployment with other limits. The
  record therefore lives on the deployment and is read by the pre-call deployment hook, per attempt,
  from the `model_info` the router stamps on that attempt. The app still reads the same record for
  the alias it requests — to *start* with the shape the model accepts — and the hook covers the
  attempts the app cannot see.
- **Record rather than reject, because rejecting is a policy the harness does not need.** A model
  that cannot be forced into a tool call still runs every agent turn; the harness has a second shape
  for each such case (`auto` plus the prompt's instruction, `between_tools` instead of `disabled`,
  the schema as an ordinary bound tool, the thinking block dropped). Refusing such models would
  have kept both Claude 5.5 models out. What *is* refused is a default role the record says the
  model cannot serve: `chat` and `chat:low` carry every classifier, summarizer and risk-scorer
  call, which use `response_format` and have no other shape — so a model recorded as rejecting it
  cannot become their default or enter their failover chain.
- **One definition, three readers.** The shapes could have been written in console-backend (which
  runs the probe), in agent-common (which sends them) and in the proxy (which rewrites them) — and
  would then drift, exactly as the embedding `dimensions` param once did before
  `ringier_a2a_sdk.embeddings.profile_for` made the runtime adapter and the registration test share
  one profile. This follows that precedent: a dependency-free SDK module, copied into the proxy
  image as a single file the way the trace-span filter already is.

## Consequences

- Registering a chat model costs about ten small inference calls instead of one, within a wall-clock
  budget. They go through the gateway under the console's management key, carry no Cost Attribution
  and are therefore not costed against a Rate Card — like the ping before them, only more of them.
  That takes several seconds, so the Test endpoint streams its progress (NDJSON: the plan, each
  request as it is sent, each shape's verdict, then the verdict of the whole test) and the console
  shows what is being probed. The verdict is in-band — the stream starts before it exists — and the
  probe runs to completion and records even when the admin closes the view.
- A 200 is not a verdict either: a routable shape is graded on whether the reply shows it was
  honoured. The gateway can rewrite a shape it knows the model refuses and answer the rewritten
  request — LiteLLM's `drop_params` downgrades a forced `tool_choice` to `auto` and drops
  `thinking: disabled` for models its map flags, both with a 200 — and in the first live QA of this
  decision that recorded Sonnet 5.5 as accepting both. So the forced shapes ask for *no* tool call
  and fail when none comes back; `response_format` fails when the reply is not the schema; a
  thinking-off switch fails when the reply still shows reasoning, and one that shows none is checked
  against a thinking-on control turn on the same question (the question must be hard enough that an
  adaptive-thinking model reasons about it at all; without that evidence the switch is recorded as
  unverified). The map is never consulted: it is what makes the rewrite happen, and it changes
  under the proxy whenever the proxy restarts.
- Otherwise only a definite provider rejection is a verdict. A rate limit, a timeout, a 5xx, a reply
  cut off at `max_tokens` before it could be judged, or a cooled-down deployment is *inconclusive*: the shape is reported as unmeasured, its earlier flag (if any)
  stays in the record, and an unmeasured unavoidable shape fails the test as "re-run" rather than
  refusing the model. A record is knowledge, and noise must neither become nor erase knowledge.
- Probe traffic is marked (`metadata.nannos_probe`) and pinned to its alias (`disable_fallbacks`),
  and the hook applies none of its record-driven rewrites to it. Otherwise a re-test of a deployment
  recorded as unable to force a tool call would have its forced probe downgraded, pass, and flip its
  own record; and a rejected shape could be answered by the next alias in a tier group.
- A deployment registered before this decision carries no record. Every reader treats the absence of
  a flag as *no opinion*, not as a verdict: the hook keeps its family heuristic for thinking-off and
  leaves forced `tool_choice` to LiteLLM, the app keeps `ToolStrategy`, the default-role guard lets
  the model through. Re-running the registration test writes the record.
- The record is written with LiteLLM's merging `PATCH /model/{id}/update`, so the deployment keeps its
  id and its other `model_info` keys. The router reads it on its next database reload, so a freshly
  written record is live within the proxy's reload interval rather than instantly.
- The probe sends an explicit `thinking` value to learn which switch a deployment takes; for every
  other caller the switch stays the deployment's, decided by the hook from the record alone and
  replacing whatever was sent. Nothing is guessed from a model's name: an unprobed deployment gets
  the effort alone (nannos#330).
- Thinking off is tried in order — `thinking: disabled`, `thinking: between_tools`, then
  `reasoning_effort: none` alone — and the first that shows no reasoning is recorded. When even the
  effort alone still reasons, nothing turns the deployment's thinking off (Gemini 3.1 Pro: LiteLLM
  maps `none` to its floor, `thinkingLevel: low`), and the record says `always_on`. The console then
  offers no "off" for that model: the Extended Thinking toggle is locked on in the user settings and
  the sub-agent form — as display only: a stored "off" is kept, the gateway sends it as the model's
  floor, and it stays right if a later probe finds a way to turn thinking off.
- The floor of an `always_on` deployment is measured too (`thinking_floor`): the probe sends the
  thinking-off question at the two lowest levels the console's picker offers for the deployment
  (`thinking_levels_for`, from the gateway's `supports_<effort>_reasoning_effort` flags) —
  `minimal`, then `low`, when it declares none — and records the first effort accepted, so the
  floor is always a level the admin also sees; the hook sends a thinking-off request as that
  effort. The trade-off is accepted: a model LiteLLM accepts `minimal` on but whose picker does not
  offer it (no `supports_minimal_reasoning_effort` flag) gets `low` as its floor, a little more
  thinking than `none` alone would map to. `none` alone is not a floor everywhere: on Claude,
  LiteLLM turns it into no thinking parameter and no effort, so Opus 5.5 ran at its default effort
  with its thinking text omitted and nothing streamed (nannos#330); a real effort comes back as
  adaptive thinking with a readable summary. A deployment whose model map does not translate an
  effort for it (a Bedrock ARN: every effort becomes a `budget_tokens` Opus 5.5 rejects) accepts no
  level — a recorded limitation, and the hook keeps sending `none` alone. A record made before the
  floor existed behaves the same until the deployment is re-tested. Known limit: with `modify_params`, LiteLLM drops
  `thinking` on a tool turn whose history carries no thinking blocks (after a replay strip, a
  cross-family failover, or a history from a non-thinking model); the turn keeps the floor effort
  but its thinking text is omitted again.
- One LiteLLM-native flag is written with every recorded Test (`litellm_flags_for`), because
  LiteLLM's own request translation reads it and nothing else sets it for a model its map does not
  list: `supports_low_reasoning_effort: true` where the picker offers `low` and the gateway's merged
  view (the deployment over LiteLLM's map) has no opinion yet. Without some level flag, under
  `drop_params`, LiteLLM drops `output_config.effort` and every effort — a user's pick and the floor
  alike — runs at the model's default (Opus 5.5 on v1.103). It is not written at registration: the
  form alone cannot see a map that excludes `low` (gpt-5.5-pro). LiteLLM registers it per backend
  model, not per deployment. Nothing the probe measures is mirrored into LiteLLM's flags:
  `supports_forced_tool_use` is not read from model_info, `supports_response_schema: false` would
  make LiteLLM rewrite `response_format` into a forced tool call the same models reject, and
  `thinking_always_on` would make LiteLLM strip the probe's own `thinking: disabled`, so a later
  Test could never see thinking turn off. Gemini 3.5 Flash is
  *not* always-on: its floor, `minimal`, measured as no reasoning, so its "off" is real. When every
  off request is refused outright, the effort alone included (OpenAI-direct gpt-5 and the o-series
  400 on `none`), the record says `unsupported`: the hook strips the effort and the switch, so a
  thinking-off request goes out with the provider default — the one shape seen to work. `always_on`
  depends on the model map too: with `drop_params`, a map miss can strip both the switch and the
  effort from every off attempt, and a model that thinks by default then records `always_on`
  falsely — a re-test after a map update corrects it.
- A user's "off" is sent as off. The app models Extended Thinking as opt-in — no level means off —
  but used to send nothing for it, which leaves the provider default: thinking ON on the Claude 5
  family and Gemini 3, so the toggle read "off" and did nothing. The orchestrator, sub-agents and
  scheduled agents now send `reasoning_effort: none` when no level is chosen — but only where the
  record of every deployment behind the (resolved) alias says how thinking goes off, decided per
  turn so a re-run Test takes effect on the next one. Unprobed, or with an `unsupported` record,
  they keep sending nothing, the provider default, as before any record existed. The hook applies
  the serving deployment's recorded switch per attempt, a failover attempt included; an attempt
  landing on an unprobed deployment gets the effort alone — every deployment is tested before the
  gateway knows more. Utility calls (HITL resume,
  indexing, tool selection) are not user choices and are unchanged. Risk scoring has since moved
  to the standard tier with reasoning asked for explicitly (nannos#361).
- A deployment recorded as rejecting the replay of its own signed thinking block has the blocks
  stripped by the hook per attempt, the way a non-Anthropic fallback does: the turn continues without
  extended thinking rather than not at all.
- An edit that keeps the deployment the same model is applied in place (its id and record stay);
  one that re-routes it, changes its mode or clears a field re-registers it from the form, carrying
  the record over only while the provider model is the same (nannos#323). Either way the edit's own
  re-test rewrites it. A re-test that newly records
  `response_format` as rejected on a model that already serves `chat` or `chat:low` cannot be
  refused after the fact; it is reported as a warning naming the affected tiers.
- Config-defined deployments cannot be written through the management API, so they are probed and
  reported but never recorded. Registering models through the console is what makes the record exist.
- The next model with a new restriction fails the same probe — or passes it and breaks something the
  probe does not send. The matrix is the harness's request vocabulary and has to grow with it; a new
  shape the harness starts sending belongs in `model_capabilities` on the day it is introduced.
