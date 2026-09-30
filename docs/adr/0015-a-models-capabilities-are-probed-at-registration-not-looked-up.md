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

- Registering a chat model costs about ten small inference calls instead of one. They go through the
  gateway with the alias under test, so they are billed and attributed like any other call.
- A deployment registered before this decision carries no record. Every reader treats the absence of
  a flag as *no opinion*, not as a verdict: the hook keeps its family heuristic for thinking-off and
  leaves forced `tool_choice` to LiteLLM, the app keeps `ToolStrategy`, the default-role guard lets
  the model through. Re-running the registration test writes the record.
- The record is written with LiteLLM's merging `PATCH /model/{id}/update`, so the deployment keeps its
  id and its other `model_info` keys. The router reads it on its next database reload, so a freshly
  written record is live within the proxy's reload interval rather than instantly.
- The probe sends an explicit `thinking` value to learn which switch a deployment takes, and the hook
  now honours any `thinking` a caller sends rather than rewriting it. The app has not sent one since
  #278 and must not start: the switch is the deployment's, decided by the hook from the record.
- Config-defined deployments cannot be written through the management API, so they are probed and
  reported but never recorded. Registering models through the console is what makes the record exist.
- The next model with a new restriction fails the same probe — or passes it and breaks something the
  probe does not send. The matrix is the harness's request vocabulary and has to grow with it; a new
  shape the harness starts sending belongs in `model_capabilities` on the day it is introduced.
