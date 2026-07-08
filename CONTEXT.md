# Context — Embeddable Nannos

The bounded context for embedding the Nannos assistant inside third-party
applications, starting with a styleable chat integration and extending toward
ontology-aware, in-context affordances.

> Status: under active design (grilling sessions 2026-06-09, 2026-07-07 on the
> knowledge/Q&A tier). Terms below are resolved; open forks are tracked at the
> bottom.

## Glossary

### Embedded Nannos
The Nannos assistant surfaced *inside a host application's own front-end*
(rather than via Slack/Email/Console). Phase 1 is a styleable, drop-in chat
widget that behaves as another thin A2A client of the orchestrator. Later
phases add awareness of the host application's ontology.

### Host application
The third-party (or first-party) app embedding Nannos. First guinea pigs:
`nannos/console-frontend` and `rcplus-alloy-cockpit-frontend` (a campaign /
audience management cockpit: Audiences, Topics, Customers, Campaigns, Line
Items).

### Ontology
The semantic model of a host application's domain objects, their scopes
(affordances), and their relationships. **The ontology is the integration
contract.** Both the MCP action surface and the DOM grounding tags are
*projections* of the ontology — neither is the source of truth on its own.

- The MCP surface is *derived from / constrained by* the ontology, never
  freely hand-rolled per app (that risks an intractable, incoherent topology).
- DOM tags *reference* ontology types + scopes; they never invent capabilities.

### Ontology Object
A domain entity in the host app's ontology (e.g. `Campaign`, `Audience`). In
the cockpit each already exists in three places: an OpenAPI operation set, a
zod form schema, and CASL permission rules.

### Scope (affordance)
A verb attached to an ontology object describing an AI-assisted action:
`create`, `update`, `explain`, … A scope binds an ontology object to a way of
acting on it. Scopes come in two fundamentally different kinds:

- **In-form affordance** — `create`/`update` where the host renders a form.
  Fulfilled by Nannos writing values into the existing form (via DOM-exposed
  schema + setter); the host's normal validation/submit/permissions persist it.
  **No API/MCP composition required; the human submits.**
- **Headless affordance** — `explain`, query, cross-object actions with no form
  in view. Fulfilled by PTC composing raw host primitives in code (see Resolved
  forks), not by a pre-composed tool layer.

### PTC — Programmatic Tool Calling
Existing Nannos capability (`agent-common`, `CODE_INTERPRETER_PTC`): the agent
writes code in a sandbox (`execute` via `FilesystemMiddleware`) and **calls
tools programmatically from inside that code**, rather than emitting one
tool-call per step. `wrap_tool_for_ptc` puts a **per-call risk scorer + HITL
guard** on every tool invoked from the code. This is the "code execution /
code mode" pattern. **PTC is the executor path for headless affordances.**

### On-behalf-of identity
When PTC code calls a host MCP server, it runs under the **end user's own
identity**, not a Nannos service identity. The host's own authz (e.g. cockpit
CASL) stays authoritative; Nannos never holds authority the user lacks, so there
is no confused-deputy / privilege-escalation path. Matches the existing per-user
OIDC, zero-trust-JWKS model of all other Nannos clients.

**Mechanism (decided 2026-07-06): Gatana per-user token exchange.** The cockpit
API is registered in Gatana as a runnable MCP server; the orchestrator reaches
it over its existing Gatana path (`exchange_token(target_client_id="gatana")`)
and Gatana performs the on-behalf-of exchange to the upstream API. This replaces
the earlier assumption of a *shared/federated OIDC issuer + direct RFC 8693*
(ADR-0002), which did not fit reality — nannos and alloy run **different IdPs**.
Gatana delivers the ADR-0002 *principle* (per-user, host-authoritative) without
hand-built federation.

This covers the **tool leg** (orchestrator → host API). The **browser leg** —
the embedded widget authenticating the end user to the *nannos* console-backend
socket when that user only holds a *foreign* (Alloy) token — is resolved by a
**cross-IdP token-exchange service** in the nannos backend: trusts configured
external IdPs, validates their tokens **offline via JWKS**, maps to a nannos user
**by verified email against a provisioned link** (guardrails are load-bearing —
`email_verified` + allowlist, else refuse), and returns a nannos token. Prefer
brokering the exchange on the nannos Keycloak (RFC 8693) over a hand-built
refresh-token vault. The grounding/client-action tier needs only *a* valid nannos
token, so it demos with a locally-sourced token behind the widget's `getToken`
seam, service swapped in later with no client change. See ADR-0002 Amendment 2.

### Integration tiers
- **Act-on-behalf tier** — a host MCP server reachable under per-user identity
  (for the cockpit: registered in Gatana). Full headless affordances via PTC.
- **Grounding / read-only tier** — host adds only the Embed SDK (no
  federation, no MCP). Nannos perceives on-screen objects and can drive in-form
  affordances (`apply`, human submits) but cannot act headlessly on the API.
- **Knowledge / Q&A tier** — the *lowest-effort* tier: host adds the widget and
  points at knowledge; Nannos **answers questions about the application** but does
  not perceive or act. Two content categories (grilling 2026-07-07):
  **(3) domain/ontology explanation** rides *free* on the existing ontology skill
  (already loaded to enable action; explaining a concept is a usage, not a new
  pipeline — one line of embedded-mode framing enables it); **(1) product/help
  knowledge** (how-to, troubleshooting, policy) is the only genuinely new content.
  Data-grounding Q&A ("what campaigns do I have") is explicitly NOT this tier — it
  is the grounding/act tiers. **Mechanism = the existing Catalog subsystem** (see
  "Product KB = a Catalog").

### Product KB = a Catalog
The category-1 product/help knowledge base is **not** a new pipeline and **not** a
content-pinned skill — it is an instance of the existing **Catalog** subsystem
(`console-backend/console_backend/catalog/`, `agent_common/core/catalog_tools.py`).
A Catalog is a named document collection with its own S3-Vectors index
(`catalog-{id}`), fed by a pluggable **source adapter** (`CatalogSourceAdapter`;
Google Drive today), through a sync pipeline with **two-pass contextual retrieval**
(doc summary → contextualized page), **incremental sync**, content-hash dedup, and
deletion handling. Retrieval is the ready-made `catalog_search` tool built by
`create_catalog_search_tool(accessible_catalog_ids)`.

Consequences:
- **Freshness is already engineered** (incremental sync + content-hash). The
  earlier navigate-to-validate-docs idea is rejected as the freshness mechanism —
  reactive, per-answer, expensive. Freshness = re-sync; honesty = the agent cites
  the catalog `source_ref`. Active `navigate` is reserved for explicit *guided
  walkthroughs*, not doc validation.
- **Minimal-effort sourcing (decided 2026-07-07):** **v1 = Google Drive only** —
  reuse the existing adapter, zero new code ("put your product docs in a Drive
  folder"). This proves ontology-skill + KB-catalog + embedded-agent end-to-end
  before any adapter is written.
  - **Generic web crawling is rejected** — the docs-tier cousin of the
    `DOM-as-ontology` anti-pattern (fragile, JS-SPA-defeated, boilerplate-stripping).
    Every source must be a *host-curated, clean-markdown artifact at a stable
    locator*, never rendered HTML.
  - **Follow-up 1 (primary): GitHub-markdown `CatalogSourceAdapter`.** Reuses the
    existing GitHub service-account integration (auth already shipped); `{repo, path,
    branch}` → pull `**/*.md(x)`; `detect_changes` via git commit-compare (precise,
    cheap). Covers **private/first-party** docs (console-frontend, cockpit).
  - **Follow-up 2: llms.txt adapter.** Single-URL fetch, prefer self-contained
    `llms-full.txt`, fall back to `llms.txt` + linked `.md`. Zero-auth, best for
    **public/third-party** hosts. (Not treated as a "de facto standard" — irrelevant;
    a cooperating host publishes one file for Nannos on request.)
- **Pipeline tolerance for text sources = VERIFIED (2026-07-07).** The sync pipeline
  handles text-only, thumbnail-less, non-paginated files: `get_all_thumbnails` fails
  open ("continuing without"), `get_thumbnail` may return `None`, thumbnails are
  UI-only (retrieval indexes `text_content`), and one `ExtractedPage` per file is
  valid (`0 < page_count ≤ MAX_PAGE_COUNT`). Any markdown adapter implements
  `list_files`/`extract_pages`/`detect_changes` and no-ops thumbnails. Wart:
  `list_shared_drives` is Drive-specific but sits on the base ABC — pull it off when
  a non-Drive adapter lands.
- Contrast with **ontology (cat. 3)**, which stays a codebase-derived, HITL-curated
  content-pinned **skill** (small, always-loaded) — NOT a catalog. Clean split:
  ontology = skill; product docs = catalog.

**Binding (v1, decided 2026-07-07): reuse existing catalog access, group-scoped,
manually aligned.** No new adapter-declared auto-binding. Because the embedded agent
runs on-behalf-of the user, its `catalog_search` resolves through the *same*
`get_accessible_catalogs(user)` path (RBAC `catalogs:{read}` + per-catalog
permissions). Ops: grant the embedded users' **group** read on the KB catalog and
equip the sub-agent with `catalog_search`; the two are aligned by hand. The
app-scoped `knowledge_catalog_ids`-on-the-Domain-Adapter binding (automatic, no
per-group grant) is deferred — revisit when embedding scales past manual alignment.
Safe to defer precisely because product docs are non-sensitive and identical per
user, so group-read is not a privacy boundary here.

**Retrieval wiring = already done.** `catalog_search` is an *essential tool* every
dynamic agent gets by default (`dynamic_agent._get_effective_tools`), appended even
under a scoped tool allowlist. So the embedded domain agent already has it; no new
tool wiring. Known v1 over-reach (accepted): `catalog_search` sees *all* the user's
accessible catalogs, not just the app KB — read-only over the user's own catalogs,
tightened later by the deferred app-scoped binding. Net new build for tier B is at
most a **web-docs `CatalogSourceAdapter`** (only if the host's docs aren't already
in a supported source like Google Drive).

### Domain Adapter
The per-host-app unit of integration. Not a composed tool layer — it is
`{ ontology skill + raw capability surface + auth + risk policy }`:
- **ontology skill** — a loaded skill/doc telling the agent what the domain
  objects, scopes, and relationships are, and how to compose primitives.
- **raw capability surface** — the host's primitives (projected from its
  OpenAPI), made callable inside the sandbox. Dumb/authenticated, *not*
  semantically composed.
- **auth** — credentials under which calls run (open fork: on-behalf-of).
- **risk policy** — which primitives are writes/dangerous → HITL-gated via the
  existing PTC risk scorer.

Composition is **emergent in PTC code**, not pre-baked. Tractability of a large
surface is handled by **progressive disclosure** (agent greps/reads only the
types it needs from sandbox files), not by curating a small tool set.

### Embedded mode (scoping)
Embedded Nannos targets a **scoped domain agent directly**, not the general
orchestrator. The domain agent is an **agent-runner instance configured with a
Domain Adapter** (`{skill(s) + tools + client_action + risk}`) — it *has* the
domain knowledge, so it's the worker, not a router. Routing a single-domain embed
through the orchestrator was rejected: a domain-less orchestrator is an expensive,
dumb router (always delegating to the same sub-agent). The **orchestrator is
reserved for genuinely multi-domain embeds**; the adapter declares its entrypoint.
- **Entrypoint:** `agent_url` → the domain agent's agent-runner endpoint;
  console-backend exchanges the token for *that* agent's audience. No new infra —
  agent-runner already serves sub-agents as addressable A2A endpoints. Prefer a
  **dedicated per-domain** agent-runner (e.g. a `cockpit` agent) over piling skills
  onto the generic `general-purpose` (which isn't scoped).
- **Scope by construction:** because the entrypoint is the domain agent, there is
  no orchestrator auto-tool set to suppress — it only ever had its adapter's tools.
- **Trigger (trusted):** the session's OAuth client is the embedded client
  (`azp = nannos-embedded`), validated at **console-backend** (the socket
  token-auth branch) — the one place the original `azp` is visible (the RFC 8693
  exchange rewrites `azp` to the exchanging client). console-backend enters
  embedded mode and propagates a **trusted** embedded signal to the agent.
- **App id (soft):** a frontend-declared id of *which* host app (`cockpit`, …)
  selects the Domain Adapter / entrypoint agent. Decided (2026-07): single embedded
  client + frontend app id, **not** a per-host client — the **hard authz boundary
  is the user's identity + consent**, so spoofing the app id grants no permission
  the user lacks. Consequence: the one embedded client holds the **union** of apps'
  exchange audiences (least-privilege-by-default, not IdP-enforced). Revisit
  (per-host client) only for **untrusted third-party** surfaces.
- **Whitelist unit = the sub-agent**, not a flat tool/skill list. A sub-agent
  encapsulates `{skill(s) + tools + risk}`; ontology is disclosed *inside* it via
  progressive disclosure (it greps `/skills/…`), never flattened into a caller.
  The orchestrator (when used) only sees each sub-agent's **card** to route.
- **Work item this creates:** the embedded machinery currently in `orchestrator-
  agent` (the `client_action` tool + `<client_objects>` rendering) must move to
  **shared agent-runner middleware/tools** so any domain agent can perceive
  on-screen objects and drive client-action.
- **Reuse the scheduler's dispatch pattern.** The scheduler already runs a
  *specific configured sub-agent* on agent-runner **on-behalf-of the user, bypassing
  the orchestrator** (`a2a_dispatch.py`; a sub-agent is `sub_agent_id →
  {system_prompt|agent_url, tools, skills, model_tier}` — essentially the Domain
  Adapter minus auth/risk). Embedded is the **interactive, streaming** sibling:
  `app-id → sub_agent_id`; socket `send_message` dispatches to that sub-agent (live
  streaming A2A path) instead of the orchestrator; on-behalf token exchanged for the
  sub-agent's audience. Only real difference from the scheduler: **live session
  token** (embedded) vs **vaulted offline token** (scheduler). So a **domain agent =
  a sub-agent config**, not a new entity, and embedded is mostly wiring, not new infra.

### Mode switch (client-action ↔ headless)
**Headless writes ARE in scope** — read-only MCP would defeat the act-on-behalf
tier (no bulk ops, no acting on off-screen objects, no UI-unexposed actions). Both
channels can mutate; choosing between them is layered: a **generic baseline** in
the embedded-mode framing; the **domain policy in the ontology skill** is
authoritative and host-specific; the **risk policy enforces** the safety floor.
Policy: **prefer `apply` when the user is viewing the editable object** (in-place
human review via the form), **use headless writes for off-screen / bulk /
UI-unexposed actions**.

**Safety for headless writes = HITL, not read-only.** `apply`'s safety ("user
reviews & saves the form") is replaced, for headless mutations, by the **PTC risk
scorer → HITL approval** (the interrupt / `InterruptConfirmCard` mechanism): a
flagged write is approved by the user in the widget *before it persists*. So gate 4
is the **primary** gate for headless writes, and human-in-the-loop is preserved —
it just moves from "save the form" to "approve the write". Gate 1 still bounds
*which* tools can be written (allowlist → "no accidental email").

**Rendering headless outcomes.** Split the concern: the agent maps *tool outcome →
which ontology object* (in the skill — needs ontology quality); the **host owns how
the UI re-renders it** (never dependent on the agent understanding the host's data
layer). The agent declares the ontology-level outcome; the host renders it.
- **Reads / new objects → `navigate`/`highlight`** (route there → fresh load; or
  point at it).
- **In-place writes → a `refresh`/`invalidate` kind (NEEDED, since headless writes
  are in scope).** `navigate` does NOT cover it (same-route navigate is a cached
  no-op, and coarse — loses in-place state). After a headless write the agent emits
  the mutated `{type,id}`; the host re-renders via a registered per-type handler.
- **Bulk/cascade writes + other users/tabs → the backend-change-events layer**
  (UI subscribes, re-renders regardless of actor) — the robust upgrade, since the
  agent won't always enumerate everything it touched.
- **v1 client-action kinds: `apply` + `highlight` + `navigate` + `refresh`.**

⚠️ The shipped `cockpit-ontology` skill still says "MCP read-only, mutations only
via UI" — that guidance is now **wrong** and must be rewritten to "writes allowed,
HITL-gated; prefer `apply` when on-screen".

**Instrumentation (who owns the prose).** Agent-side, never the frontend. Three
**composing** (not exclusive) layers:
1. **Embedded-mode framing** — an agent-side middleware, triggered by the trusted
   embedded signal. Host-agnostic: "you are embedded in `{app}` via the Embed SDK;
   you perceive `<client_objects>` (with current values); you can act via
   client-action (apply/highlight/navigate, human-reviewed) or headless tools; the
   user is viewing `{context}`." This is where the agent's **scope-awareness** and
   **which-client / SDK-feature awareness** come from — from the signal, not a
   frontend string.
2. **Domain Adapter skill(s)** — server-side, selected by app-id; domain knowledge
   + recipes; a **skill *set*** for large systems (progressive disclosure scales
   better than one giant prompt).
3. **Base sub-agent system prompt** — unchanged.

The **frontend/SDK contributes structured data only** — the `<client_objects>`
manifest, the current-view context, an `intent` (e.g. `"suggest"`), and a
**capability descriptor** (which client-action kinds / features exist) — **never
prose**. The agent renders the prompt. (So the hardcoded "suggest what you can do"
string in the cockpit is replaced by an `intent: "suggest"` signal the embedded-mode
middleware turns into the instruction.)

**Tool opt-in / safety — four gates.** An embedded action must clear all four:
1. **Integration allowlist** (Domain Adapter, per app-id) — the ONLY capabilities
   the embedded agent is offered; orchestrator auto-tools OFF. Primary reason
   "accidental email access" is impossible (the cockpit adapter never includes it).
2. **Identity floor** (automatic, hard) — the user's on-behalf-of token + the
   host's own authz; the agent can't exceed what the user may do.
3. **User consent** by capability *class* (`read-own` / `write-own` /
   `act-on-external`), remembered per `(user, app, class)`. **DEFERRED (2026-07):**
   v1 relies on gates 1+2+4; add first-use consent on the **external/cross-domain**
   class only when embedding higher-risk or third-party surfaces.
4. **Per-action HITL** (PTC risk policy + approval card) — writes/dangerous ops
   reviewed; effectively per-action user consent for mutations.

### Embed SDK
The single client-side runtime for Embedded Nannos (chosen over declarative DOM
attributes). It is **not just an object-registration API** — it is the one
thing a host installs, and it serves *all* client-side surfaces: the styleable
chat widget, the HITL approval widget, the work-plan / activity views, the
feedback prompt, and in-form `apply`.

**Unifying insight:** the chat surface, HITL card, work-plan view, feedback
prompt, and in-form `apply` are all the same kind of thing — **styleable
client-side renderers of negotiated A2A extensions** (`human-in-the-loop`,
`work-plan`, `feedback-request`, `activity-log`, `client-action`). The agent
emits a structured directive; the client renders/handles it. The Embed SDK is
the extraction of console-frontend's `ChatApp` + its extension renderers
(`InterruptConfirmCard`, `UnifiedTimelineBlock`, `WorkingBlock`,
`MessageFeedback`) into an embeddable, themeable package, plus object
registration + client-action execution.

**Two layers (committed):**
- **Headless core** — protocol client: session/auth bootstrap, message send,
  receipt of A2A extension directives, object registration
  (`register({type,id,scope,schema,getState,apply})`), client-action
  execution. No UI.
- **Styleable UI kit** — prebuilt components on the core. Default path is
  batteries-included: theme via design tokens, rendered inside a **Shadow DOM**
  for CSS isolation ("drop in, set brand tokens, done"). Advanced hosts
  override individual components via **slots** (e.g. their own HITL card) or go
  fully headless against the core.

The `apply` handle writes *through* the host's own form layer (e.g.
`react-hook-form`) so validation / dirty-tracking / auto-save fire and the human
still submits. Declarative `data-nannos-*` attributes remain as an optional
**read-only grounding** fallback (describe what's on screen; cannot write into a
React-controlled form).

### Client-action (A2A extension)
The agent→widget return channel, modelled as a new versioned A2A extension
(`urn:nannos:a2a:client-action`), symmetric to the existing `human-in-the-loop`
/ `work-plan` / `feedback-request` extensions. The agent (server-side) emits
typed directives (`apply` / `highlight` / `navigate` / …) targeting a
registered object id; the **widget is a sandboxed executor** that runs them
*only* against handles the host itself registered via the Embed SDK. HITL
composes on top (a write-scope `apply` may require confirmation) but `apply` is
not defined *as* HITL. Triggers are uniform: an inline AI button the widget
materialises and a chat message produce the same structured intent.

### Two integration surfaces (symmetric)
A host integrates Nannos through two surfaces sharing one scope vocabulary:

| | Server side (act) | Client side (perceive + in-form act) |
|---|---|---|
| Contract | host runs an **MCP server** | host uses the **Embed SDK** |
| Generated from | OpenAPI | zod schemas + form handles |
| Scopes | `explain`, query, cross-object → PTC over MCP | `create`/`update` on a registered form → SDK `apply` (no API call; human submits) |

## Anti-patterns (agreed)
- **Endpoint-as-tool**: 1:1 projection of REST endpoints into MCP tools.
  Becomes intractable for the LLM and loses semantic coherence.
- **DOM-as-ontology**: treating DOM markup as the source of truth for what
  Nannos can do. Fragile, view-coupled, can't describe off-screen objects, and
  client-declared capabilities can't be trusted/secured.

## Resolved forks
- **Composition is not pre-baked.** PTC (code execution + programmatic tool
  calling) is the executor for headless affordances; it composes primitives in
  code. No "composed MCP tool" layer is needed. The MCP layer thins to an
  authenticated *raw* primitive provider; semantics move into an ontology
  skill; safety stays in the per-call PTC risk/HITL guard.

- **Capability surface delivery = host runs an MCP server.** The integration
  contract is "the host exposes an MCP server," generated from its OpenAPI /
  typed SDK by off-the-shelf tooling, per Nannos integration docs. Nannos
  connects with the existing per-user OIDC path; PTC's risk/HITL guard wraps
  those MCP tools unchanged. Corollary: an **endpoint-shaped** generated server
  is acceptable — "endpoint-as-tool" is only intractable for direct
  tool-calling; under PTC + progressive disclosure the agent reads only the
  types it needs and composes in code.
  - **CHOSEN for the cockpit — Gatana-fronted MCP (2026-07-06)**: the cockpit
    API is registered in **Gatana as a runnable MCP server**. This is the
    decided delivery for the cockpit act-on-behalf tier because it collapses
    the auth question onto the path Nannos **already ships**: the orchestrator
    discovers Gatana servers and reaches them via per-user token exchange
    (`exchange_token(target_client_id="gatana")`, `discovery.py`); Gatana owns
    the on-behalf-of exchange to the upstream cockpit API. No bespoke
    cross-IdP federation, no offline-token vault, no LiteLLM-hosted variant for
    auth. The multi-week auth design (RFC 8693 realm trust vs. offline-vault-
    by-email) is **moot for the cockpit** — Gatana absorbs it.
  - **Accepted variant — gateway-hosted MCP (LiteLLM `mcp_openapi`)**: the host
    only *publishes its OpenAPI spec*, and the platform's existing LiteLLM
    Model Gateway serves it as MCP (config/DB-registered: url + spec_path →
    per-server endpoint `/{name}/mcp`; allow-listing; static upstream auth).
    Verified live on the pinned gateway (v1.90.0): cockpit spec → 207 tools,
    zero host-side code. Retained for **hosts NOT fronted by Gatana / local
    dev** — but upstream auth there is static (service identity), so it is a
    dev/read-only lane, not the cockpit production path.

- **Auth / on-behalf-of = end-user identity, delivered by Gatana per-user token
  exchange** (see "On-behalf-of identity"; ADR-0002 principle stands). The
  user's identity reaches the cockpit via the existing Gatana exchange; the
  host's own authz stays authoritative; service-identity rejected
  (confused-deputy). **Refinement (2026-07-06):** ADR-0002 named *federated
  identity / RFC 8693* as the mechanism and a hard prerequisite — but nannos
  and alloy have **different IdPs**, so direct RFC 8693 would need realm trust.
  Routing the cockpit **through Gatana** satisfies the ADR-0002 *principle*
  (per-user, host-authoritative) without hand-building federation. Federation /
  offline-token-vault only resurface for a host that is neither Gatana-fronted
  nor able to mint per-user tokens.
- **Embed SDK stack** — headless core = **vanilla TypeScript + Zod** (Zod
  validates `client-action` payloads and host `getState()` output at the core
  boundary before the agent acts). UI kit = **React, extracted from
  console-frontend's `ChatApp`** (NOT a Lit rewrite): reuses the mature
  React/Radix/Tailwind UI and lets first-party React hosts import components
  natively. **One UI, not two (decided):** `ChatApp` is *moved* into
  `@nannos/embed-sdk` and **console-frontend consumes it back** as its own chat
  UI — console-frontend is the first/dogfood consumer, so there is no
  two-codebase drift. (This is also what makes Lit moot: with no greenfield and
  no divergence, a rewrite buys nothing.) A **`@r2wc/react-to-web-component`** shell wraps it as a
  custom element (Shadow DOM) for non-React hosts; React-in-shadow gotchas
  (Radix portal container, Tailwind `adoptedStyleSheets`) are known/solved.
  _Revisit Lit only if non-React third-party embedding at minimal bundle size
  becomes the primary target._ Distribution = **versioned npm package, statically
  compiled by the host**; a Nannos-controlled auto-updating CDN script is
  rejected — the SDK is in the on-behalf-of token path and issues production
  writes (supply-chain/CSP risk). Self-hosted IIFE bundle is the escape hatch
  for arbitrary sites. Build: Vite library mode (ESM + IIFE).
- **Ontology skill = a content-pinned skill in the existing registry**,
  authored once by an `agent-creator`-style "ontology creator" flow from the
  host's OpenAPI, then HITL-curated. Stored/activated/mutated through the
  existing `skill_registry` machinery (content-hash pinning, HITL-guarded
  mutation, docstore cache). "Automating composition" resolves to *automating
  authoring of the grounding* — PTC then composes against it at runtime.
- **Progressive disclosure on both surfaces.** Server: the agent greps sandbox
  files for only the API types it needs. Client: the widget pushes a compact
  per-turn manifest `[{type, id, scope, label}]` of on-screen objects; full
  schema + state is pulled on demand via the client-action channel. One
  philosophy: always-available cheap index, detail pulled when engaging.

_No open forks remaining — core abstraction resolved (grilling 2026-06-09)._
