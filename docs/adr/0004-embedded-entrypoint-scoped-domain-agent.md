---
status: accepted (2026-07-06)
---

# Embedded Nannos targets a scoped domain agent, not the orchestrator

The embedded entrypoint is a **scoped domain agent** — an `agent-runner` instance
configured with a **Domain Adapter** (`{ontology skill(s) + tools + client_action +
risk policy}`) — reached **directly** (its own A2A `agent_url`, its own token
audience). It is **not** the general orchestrator. The orchestrator is reserved for
genuinely **multi-domain** embeds; a single-domain adapter (the common case) skips
it. A "domain agent" is not a new entity — it is the existing **sub-agent config**
(`sub_agent_id → {system_prompt|agent_url, tools, skills, model_tier}`), plus auth
and risk.

Embedded *mode* is triggered by the trusted OAuth client identity (`azp =
nannos-embedded`), read at **console-backend** (the socket token-auth branch — the
one place the original `azp` survives, since the RFC 8693 exchange rewrites `azp`
to the exchanging client). A **soft, frontend-declared `app-id`** selects which
domain agent. The **hard authz boundary is the user's identity + consent**, so a
spoofed `app-id` grants nothing the user lacks; a single embedded client is
acceptable (it accrues the union of app audiences — least-privilege-by-default, not
IdP-enforced). Per-host clients are revisited only for untrusted third-party hosts.

## Why

- **A domain-less orchestrator is a bad fit for a single-domain embed.** With no
  domain knowledge it can only route — and it will route to the *same* sub-agent
  every time, burning a full LLM turn to make a fixed decision. It's an expensive,
  dumb router; worse, it can't even reason about the task, only forward it.
- **Scope by construction.** Because the entrypoint *is* the domain agent, there's
  no broad orchestrator auto-tool set to suppress — it only ever holds its adapter's
  tools. "Avoid accidental email access" is true because the email tool simply isn't
  in the adapter, not because a prompt forbids it.
- **No new infrastructure, and already proven.** Sub-agents are addressable A2A
  endpoints served by `agent-runner`. The **scheduler already dispatches a specific
  sub-agent on agent-runner, on-behalf-of the user, bypassing the orchestrator**
  (`console-backend/utils/a2a_dispatch.py`). Embedded is the interactive, streaming
  sibling of that dispatch, with a live session token instead of a vaulted offline
  token — mostly wiring existing pieces.
- **Identity-driven instrumentation, not frontend prompts.** The agent's scope,
  client-awareness, and current-context awareness come from an agent-side
  embedded-mode framing (triggered by the trusted signal) + the adapter's skill(s),
  composing with the base prompt. The frontend/SDK contributes structured data + a
  capability descriptor only — never prose.

## Alternatives considered

- **A — full orchestrator (the shipped demo).** Broad auto-tools, general scope;
  scope/safety enforced only by prompt. Rejected: cannot make tool access opt-in;
  fights "no accidental email" uphill.
- **B — a dedicated scoped agent per host.** This *is* what we chose — realized
  cheaply, because sub-agents are already addressable agent-runner endpoints, so B
  costs no extra service.
- **C — orchestrator entered in a scoped "embedded mode."** Rejected: still a
  domain-less router for the single-domain case; the domain knowledge has to live
  somewhere, and that somewhere is the domain agent.

## Consequences

- **Move the embedded machinery out of `orchestrator-agent`** into shared
  `agent-runner` middleware/tools: the `client_action` tool and `<client_objects>`
  rendering, so any domain agent can perceive on-screen objects and drive
  client-action.
- **Route the socket by `app-id`** to the domain agent's `agent_url`, and exchange
  the on-behalf token for **that agent's audience** (not the orchestrator's).
- **Define a dedicated per-domain sub-agent** (e.g. a `cockpit` agent) rather than
  piling skills onto the generic `general-purpose`.
- **Headless writes are in scope** (act-on-behalf tier), gated by the **PTC risk
  scorer → HITL approval** (human-in-the-loop moves from "save the form" to "approve
  the write"). This adds a **`refresh`/`invalidate` client-action kind** so the UI
  re-syncs after a headless mutation; backend-change-events are the robust upgrade.
- **Rewrite the `cockpit-ontology` skill** — its current "MCP read-only, mutations
  only via UI" guidance is wrong under the above.
- **The shipped demo (orchestrator path) is not wrong** — it is the multi-domain /
  orchestrator variant. The scoped domain-agent path is the target architecture; the
  migration is tracked in `task_plan.md`.

## Substrate (refined 2026-07-06 after mapping agent-runner)

"Domain agent, targeted directly" is the *intent*; the *substrate* is chosen by
interactivity:
- **Interactive embedded (client-action, streaming) → run the scoped domain
  sub-agent inside the orchestrator process**, directly as the entrypoint,
  **bypassing the routing main-graph turn** (so no dumb-router hop). This reuses
  what only the orchestrator process has: the custom-stream → `client-action` A2A
  extension emission (`executor.py`), and `DynamicLocalAgentRunnable`'s skill +
  MCP-tool + whitelist mounting.
- **agent-runner is NOT the interactive substrate.** It is built for the
  scheduler's fire-and-forget jobs: it keeps only final `values` and **drops the
  custom stream events** client-action rides on, doesn't mount skills on its LOCAL
  path, and has no `client_objects` plumbing. Reserve it for non-interactive jobs.

Shared prerequisites (substrate-agnostic, done first): the `client_action` tool
and a `<client_objects>` renderer move to **agent-common**; the renderer reads the
manifest from **RunnableConfig metadata** (not the orchestrator's typed
`GraphRuntimeContext`) so one implementation serves the main graph and any LOCAL
sub-agent, attached via the existing `extra_middlewares` seam.

See CONTEXT.md "Embedded mode", "Mode switch", and the "Domain Adapter" refinement.
