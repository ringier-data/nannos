# Embedded Nannos — integration playbook

A step-by-step guide to embedding a Nannos assistant into an application: wire the
SDK, decide the agent's "brain" (system prompt vs skills), create the scoped
sub-agent on Nannos, and integrate a knowledge base. Includes a **codebase-driven
prompt** you run against the target app to generate the whole sub-agent plan
(client objects, tools, system prompt, and any skills / KB).

Audience: an engineer (or an agent like Claude Code opened on the app repo)
integrating Nannos into a specific application.

---

## 0. Mental model — the five layers

An embedded Nannos integration is five layers. Get these straight before writing
anything; every later decision maps to one of them.

| Layer | What it is | Where it lives |
|---|---|---|
| **Ontology (domain model)** | Your business entities, their relationships, and the operations over them — `Customer → Order → LineItem`, "a Campaign has Targetings", "an Order can be canceled but not after fulfillment". This is what the agent reasons about; it's your app's domain, not a Nannos artifact. | Your app's business logic (backend models, domain services, code). |
| **Client objects** | The **on-screen projection** of the ontology the agent can read and change *directly* — the specific records/forms/selections currently in view, each registered with a schema. A slice of the ontology, surfaced client-side. | Host app frontend, via `useNannosZodForm` / `core.register`. |
| **Tools (MCP)** | Server-side operations over the ontology beyond what's on screen — fetch/list/mutate other entities. The agent calls these. | Your app's MCP server (backend), registered on the sub-agent. |
| **Client-actions** | Widget-side effects the agent invokes on the client objects: `apply` (write values into a form, human approves), `highlight` (point at a field), `navigate` (go to a page). No backend needed for in-form `apply`. | SDK; host supplies `navigate`/`highlight` hooks + the registered objects for `apply`. |
| **The brain** | What tells the agent how *your ontology* works — the entities, relationships, rules — and how to compose operations over it: the sub-agent's **system prompt**, optional **skills**, optional **knowledge base**. | The Nannos sub-agent config. |

So: the **ontology** is your business domain; **client objects** are the on-screen
slice of it the agent edits directly; **tools** reach the rest of it server-side;
the **brain** is the agent's understanding of it. The execute-only embed runs
**one scoped sub-agent** (`subAgentId`) as the top-level agent — no routing turn —
so "the brain" is that sub-agent's config.

---

## 1. Instrument the frontend (SDK)

> **This section is the SDK surface (mechanics) — you don't fill it in yet.** On a
> fresh integration you have no client objects; Step 2's prompt analyzes the
> codebase and *proposes* which to register (with schemas). Read this for context,
> then come back and implement that plan once Step 2 is done. (It's numbered first
> only because it's the surface the rest of the playbook refers to.)

Mechanics are in [`README.md`](./README.md) — don't duplicate here. In short:

- Wrap the app in `<NannosProvider config={{ backendUrl, subAgentId, getToken|auth }}>`.
- Drop in `<NannosWidget core={useNannos()} accent="#yourbrand" />`.
- For each client object in the Step 2 plan, register it with
  `useNannosZodForm({ form, type, id, scope, schema })` — `type` names the
  ontology entity it projects (`Customer`, `Order`, `Campaign`), and the
  **schema is the agent-settable contract** (drives the manifest, per-field
  validation, and the state the agent reads).
- Provide `navigate` (and optionally `highlight`) on the provider for those
  client-actions.

The client objects give the agent the on-screen slice; the system prompt (Step 2)
gives it the rest of the ontology — the entities those objects belong to and how
they relate.

---

## 2. Plan the sub-agent: allocate the brain (system prompt · skills · KB)

**Default posture: a system prompt is enough.** The "brain" has three possible
homes and this step *allocates* each piece of domain knowledge to one of them:
system prompt (default), a skill, or the knowledge base. Reach for a skill only
when an operation composes tool calls in a **non-obvious** way; reach for the KB
only for large/retrievable reference data. Most integrations need **zero skills**
and **no KB** — if the ontology is simple enough to explain, explain it in the
system prompt and stop.

Decision heuristic:

| Situation | Put it in… |
|---|---|
| Field semantics, enums, formats, units, "what this object is" | **System prompt** |
| Domain rules: required-together fields, status transitions, validation, uniqueness | **System prompt** |
| An operation = a single obvious tool call, or directly derivable from the schema | **System prompt** |
| An operation = **multiple** tool calls in a **specific/non-obvious order** ("to do X, first fetch Y, map Z, then call W") | **Skill** |
| A procedure with pre/post-conditions or disambiguation the agent would otherwise get wrong | **Skill** |
| Knowledge that would **bloat** the system prompt if inlined | **Skill** (procedure) or **KB** (data) |
| Large, changing, or retrievable reference data (product catalog, policies, docs) | **Knowledge base** (§4) |

Run the single prompt below **against the application codebase** — it inventories
the domain once, then allocates the brain across all three homes. Paste it into an
agent (e.g. Claude Code) opened on the target app repo. It's **checkpointed**: it
stops after the inventory (confirm the ontology before anything builds on it) and
again after the allocation, so you steer before it drafts the final plan.

````text
You are planning a Nannos "embedded" sub-agent for THIS application. It runs
execute-only inside the app's chat widget and acts on the app's business domain —
reading/updating on-screen entities (the "client objects") and calling MCP tools
for the rest. Analyze the codebase and produce a complete plan.

WORK IN CHECKPOINTS. Do the steps IN ORDER and, after each one, STOP: present your
findings and WAIT for my approval or corrections before starting the next step. Do
NOT run ahead — every later step builds on the earlier ones, and building on a
wrong ontology (Step 1) makes the whole plan meaningless. Incorporate my
corrections before proceeding.

STEP 1 — INVENTORY (do this once, cite files):
1. Ontology (the domain model): the business entities, their relationships, and
   the operations over them (e.g. Customer → Order → LineItem; a Campaign has
   Targetings; an Order can be canceled but not after fulfillment). Derive from
   backend models, domain services, and types — NOT just the UI.
2. Client objects to instrument (the on-screen slice) — you are PLANNING these;
   the SDK registrations do NOT exist yet. Identify on-screen surfaces that SHOULD
   become client objects (editable forms, record detail/edit pages, selections
   where a user would want the assistant to read or fill values). For each PROPOSE:
   type (the ontology entity it projects), scope(s) (create/update/explain), the
   agent-settable fields (name, type, enum, meaning), and the form/component it
   binds to. Flag fields needing a FieldBridge (no 1:1 form key, e.g. two ISO dates
   ↔ a date-range tuple). (If registrations already exist — `useNannosZodForm` /
   `core.register` / `zodFormRegistration` — read those instead of re-proposing.)
3. Tools — server-side operations beyond what's on screen. If an MCP server
   exists, list each tool (purpose + args). If NONE exists yet (likely), fall back
   to the app's API surface: OpenAPI/Swagger (`openapi.json`) or route defs.
   Enumerate the operations the agent would need (method, path, purpose, key
   params) and flag which SHOULD be exposed as MCP tools — the subset in this
   assistant's scope, NOT the whole API. Built-in client-actions exist regardless:
   apply (write form values, human-approved), highlight (point at a field),
   navigate (go to a route).
4. Domain rules: validation, required-together fields, status transitions,
   units/formats, uniqueness — from schemas, form validation, and backend models.

→ STOP. Present the inventory — ESPECIALLY the ontology (#1) — and wait for my
confirmation or corrections. Do not allocate the brain until the ontology is
right; that's the foundation everything else rests on.

STEP 2 — ALLOCATE THE BRAIN. For every piece of domain knowledge / operation the
agent needs, route it to exactly ONE home. Be conservative: prefer the system
prompt; most apps need zero skills and no KB.
- System prompt (DEFAULT): entity semantics, relationships, domain rules, and any
  operation that is a single obvious tool call or directly derivable from the
  schema + rules.
- Skill: an operation that composes MULTIPLE tool calls in a non-obvious order, or
  a procedure with pre/post-conditions / disambiguation the agent would otherwise
  get wrong. NOT for restating the schema.
- Knowledge base (a Nannos Catalog): large, changing, or long-tail reference data
  the agent looks things up in (product catalog, policies, entity dictionaries) —
  too big/volatile for the prompt.

→ STOP. Present the allocation (a table: each operation/knowledge item → its home,
with a one-line why) and wait for my approval before drafting the output.

STEP 3 — OUTPUT the plan:
- client objects to register (frontend plan): from inventory #2 — for each: type,
  id strategy, scope(s), the zod schema to write (fields + `.describe()` + any
  FieldBridge). This is what Step 1 (Instrument the frontend) implements — a PLAN,
  not discovered code.
- MCP tools to expose (backend plan): if no server exists, the API subset to
  surface as tools (method/path/purpose/args), implemented + registered
  separately. (If a server exists, the tools the sub-agent should be granted.)
- name: short human display name (widget header).
- description: one line — what it helps with.
- system_role (the system prompt), written FOR the agent: role + scope; the
  ONTOLOGY (entities + how they relate, so it reasons about the domain, not just
  the current form); the client objects it edits on screen + their fields (name,
  type, enum, MEANING) — enough to fill them without guessing; the domain rules;
  when to use apply vs highlight vs navigate; which tools to call and when. Keep it
  tight — the per-turn manifest already gives field lists + current values
  (progressive disclosure), so explain the model/semantics/constraints, don't dump
  raw schemas.
- skills: "None needed — the system prompt covers all operations." (+ why), OR for
  each warranted skill: title; when-to-use trigger; the step-by-step tool-call
  recipe (which tool, which args, in which order, how to use each result); a worked
  example; failure/edge handling.
- knowledge base: "None needed." OR the Catalog(s) to create, what data goes in,
  and WHEN the agent should consult it (a KB the agent isn't told to use is dead
  weight).
- model: leave default unless the domain clearly needs a specific tier.
- allocation rationale: one line per non-obvious choice (why this went to a
  skill/KB rather than the system prompt).

CONSTRAINTS: prefer the leanest brain that works. Do NOT invent tools or fields
not in the codebase. Flag anything ambiguous for a human to confirm.
````

**Iterate on the output, don't ship it raw.** It produces a strong first draft
grounded in the code; a human confirms semantics, trims, and verifies no tool or
field was hallucinated. If a skill's recipe is thin, do a focused second pass on
just that skill.

---

## 3. Create the sub-agent on Nannos

Take the artifacts from Step 2 into the Nannos console.

1. **Nannos console → Sub-Agents → New** (`/app/sub-agents`, or `POST
   /api/v1/sub-agents`). Set **name** and **system role** (paste `system_role`).
2. **Attach tools.** Select the app's MCP server so the sub-agent can call its
   tools. If no MCP server exists yet, build one first from the Step 2 plan's "MCP
   tools to expose" list (the OpenAPI subset) and register it — or ship a first version
   with **client-actions only** (in-form `apply`/`highlight`/`navigate` need no
   MCP server) and add backend tools later. The **client-action** tool
   (`apply`/`highlight`/`navigate`) is what
   drives the on-screen client objects — ensure the sub-agent is **client-action
   enabled** (the embed runs it execute-only with `<client_objects>` context).
3. **Attach skills** (if the Step 2 plan produced any). Register each in the **Skill
   Registry** and attach it to the sub-agent. Skip if the verdict was "none."
4. **Attach a knowledge base** if needed (§4).
5. **Model / permissions**: leave model default unless required; scope
   permissions so the sub-agent only reaches tools/data it needs (least
   privilege — the embed's token is the trust boundary; don't over-grant).
6. **Submit → approve → activate** the sub-agent (per your org's approval flow).
7. **Grab the sub-agent id** and put it in the embed config:
   `config={{ …, subAgentId: <id> }}`. The widget header will auto-show the
   sub-agent's name (override with `adapter.agentName`).

> Confirm exact field names/flows against your console version — the concepts
> (name, system role, tools, skills, KB, approval, id) are stable; labels may vary.

**Security note.** The client declares `subAgentId`; the orchestrator validates it
against the authenticated user's accessible sub-agents and fails closed, so a
wrong/inaccessible id is refused — identity is the boundary, not the client.

---

## 4. Knowledge base integration

**In Nannos, a knowledge base = a Catalog** (console → Catalogs). Use it for
reference data the agent should *retrieve on demand* rather than carry in its
prompt.

When to use which:

| Kind of information | Home |
|---|---|
| Stable domain semantics, small + always-relevant | **System prompt** |
| Non-obvious multi-step procedures | **Skill** |
| Large / changing / long-tail data the agent looks things up in (product catalog, policy docs, price lists, entity dictionaries) | **Knowledge base = Catalog** |

To integrate:
1. Create/populate a **Catalog** in the console with the source data.
2. Attach it to the sub-agent (so retrieval is scoped to this agent).
3. In the **system prompt**, tell the agent the catalog exists and *when to
   consult it* ("look up the product code in the catalog before setting `sku`") —
   a KB the agent doesn't know to use is dead weight.

Rule of thumb: don't put in the KB what the schema already conveys, and don't put
in the system prompt what changes often or is too large — that's the KB's job.

---

## 5. Verify end-to-end

1. Load the app, open the widget → header shows the sub-agent name, status
   **Connected** (`useNannosStatus()`), no console errors.
2. Ask it to do a representative task ("fill this form for …"). Confirm:
   - it reads current on-screen state (manifest / `getState`),
   - it proposes an `apply` → the **HITL card** appears with the values,
   - approve → values land via the form's own `setValue` (validated, dirty), the
     human still saves.
3. Exercise a tool-backed operation and (if drafted) a skill workflow.
4. Wire `onError` → your monitoring; watch for `connection`/`init`/`auth`/`apply`
   events during the run.

If the agent guesses wrong values, mislabels fields, or picks the wrong tool →
that's a **brain** problem: tighten the system prompt (Step 2) or add/adjust a
skill. If it can't reach an operation at all → a **tools** problem (MCP
registration / permissions).

---

## Quick reference — which artifact answers which failure

| Symptom | Fix in |
|---|---|
| Agent fills a field with a wrong-typed/invalid value | Schema `.describe()` + system prompt semantics |
| Agent doesn't know a rule ("status can't go draft→paid directly") | System prompt (domain rules) |
| Agent does a multi-step operation in the wrong order | Skill |
| Agent can't find/look up reference data | Knowledge base (Catalog) + a prompt pointer to it |
| Agent can't perform an operation at all | MCP tool registration / sub-agent permissions |
| Agent proposes changes but nothing lands | Frontend: object not registered / wrong `type`+`id` |
