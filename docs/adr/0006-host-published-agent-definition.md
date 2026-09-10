---
status: proposed (2026-09-08); implemented the same day in console-backend, embed-sdk and the cockpit, pending review
---

# Embedded agent definitions are published by the host and bound to a sub-agent by azp

The system prompt, the skills and, when the host chooses, the model tier and
thinking level of an embedded domain agent are **authored and published by the host
application**, under `/.well-known/agent-skills/` on the host's own origin.

In Nannos, an admin creates the sub-agent **once** and binds it to the host with two
facts: the OAuth client ids (`azp`) whose tokens belong to that host, and the base
URL to fetch the definition from. From then on console-backend keeps the sub-agent's
content in sync with the host, digest-pinned, and every user whose token carries a
bound `azp` is activated for that sub-agent on arrival. Nobody pastes a prompt, and
nobody grants permissions by hand.

This ADR covers dimensions 1 and 3 of the 2026-09-07 embed hardening plan: prompt
and skill authority, and sub-agent permissions. Dimension 2 (login) is ADR-0002
Amendments 4 and 5.

## Context

- ADR-0004 defines "a dedicated per-domain sub-agent" whose config
  (`system_prompt`, `mcp_tools`, `skills`) lives in the Nannos database. The cockpit
  team wrote that content in `cockpit-frontend/docs/nannos-embed/` and someone
  pasted it into the console. The two copies drifted from day one.
- The cockpit now publishes the definition itself (cockpit-frontend, branch
  `feature/nannos-embed-spike`, `app/well-known/agent-skills/`, Vite plugin
  `app/vite-plugins/agentSkills.ts`). The format is the Agent Skills Discovery RFC
  0.2.0 plus one extension block. The cockpit publishes **no tool list**: which MCP
  tools the agent is mounted with stays a Nannos-side decision. See "The published
  format" below.
- The orchestrator only runs a sub-agent that is in the user's activated list
  (`GET /api/v1/sub-agents/activated`, `sub_agent_service.get_accessible_sub_agents`
  with `activated_only=True`). That list is the user's ACCESSIBLE sub-agents — owned,
  public, group-assigned, or (since this ADR) carrying an `embed` activation row for
  the user — narrowed to the activated ones. An activation row alone granted nothing
  before: a private agent activated for a user outside its owner and groups stayed
  invisible, so the embed activation had to become a grant in its own right.
  A cockpit user who arrives through the Alloy brokering of ADR-0002 Amendment 4
  is created on the fly by `_resolve_socket_user_via_token` (app.py) with no groups
  and no activations. Their first embed turn failed in
  `orchestrator-agent/app/core/executor.py`: "not among the user's 0 local
  sub-agent(s)". So every cockpit user would have needed manual activation.
- Nannos groups are authored in Nannos and synced one way **to** Keycloak
  (`user_groups.keycloak_group_id`, `services/keycloak_admin_service.py`).
  console-backend never reads a `groups` claim. A Keycloak group mapper on the
  `alloy` identity provider would therefore grant nothing.
- console-backend used to trust the client-sent `executeOnlySubAgentId` /
  `subAgentId` and the orchestrator resolves it (executor.py, `request_metadata`).
  The socket token path validates signature and issuer but neither `azp` nor `aud`.
- Sub-agent-scoped skills are registry rows by design: `_create_config_version`
  persists every skill through `_persist_and_strip_skills` into `skill_registry`
  with `scope='sub-agent'` and stores only refs in the version; the read path
  resolves them. The dynamic agent mounts `/skills/` from the resolved bodies.
- One Nannos Keycloak realm serves dev, stg and prod. The public client
  `nannos-embedded` lists all four cockpit origins
  (`rcplus-nannos-keycloak/app/keycloak-client-provisioning/client_configs.py`).
  A token therefore says which client minted it, not which stage's host it is for.
  Each cluster has its own database, which is where the mapping lives.

## Decision

1. **The host is the authority on its domain.** console-backend fetches
   `<base_url>/.well-known/agent-skills/index.json`, verifies every `digest`, and
   writes the sub-agent's config from it: description and domain prompt from
   `AGENT.md`, skills from the `SKILL.md` files, and — when the host publishes them —
   the model tier, thinking level and MCP tool scope. What the host leaves out
   stays a **Nannos-side setting** on the bound sub-agent, carried forward from its
   approved default version on every sync and still editable in the console.
2. **Nannos always frames.** The system prompt the model sees is Nannos framing
   first, host prompt second. The framing says who the agent is (Nannos), where it
   runs (the host's base URL, the organization named in the extension), how it is
   presented there (`x-nannos-agent.name`), and that the text that follows is the
   host's domain guidance. The host cannot remove or replace this part.
3. **The entity stays. Its content is derived.** The sub-agent row, its config
   versions, the `/activated` and `/configs/by-hash` routes, the orchestrator's
   embedded lookup, conversation stamping, per-agent cost tracking and the console
   UI all keep working unchanged. Every new well-known revision becomes one new
   **approved** config version. The version history is the audit trail of what the
   host shipped, and the latest version is the fallback when the host is down.
4. **Binding is admin-authored data, not deployment config.** An admin creates the
   sub-agent in the console and sets its embed binding: a list of `azp` values and
   one base URL. There is no `EMBED_HOSTS` environment variable and no JIT creation.
   One `azp` maps to at most one sub-agent per cluster.
5. **Authorization is implied by the binding.** A socket authenticated with a token
   whose `azp` is bound gets the bound sub-agent, and the user is activated for it
   on the spot (`activated_by = embed`). console-backend stamps the sub-agent id it
   derived; the client-sent id is dropped. Only users who arrive through the host
   get the activation.
6. **Live, not imported.** For first-party hosts there is no draft, review, approve
   cycle in Nannos. The host's PR is the review. A change on the host is live in
   Nannos within one refresh interval. On a bound sub-agent the console refuses
   edits to what the host publishes and to whole-version operations; the rest
   stays editable.
7. **Public client now, confidential client later.** Until ADR-0002 Amendment 4
   stage 2 ships (parked by Amendment 5), the bound `azp` is the public client
   `nannos-embedded`, on every cluster including prod. Any user of the Nannos realm
   can mint a token with that `azp`, so any Ringier user can reach the embedded agent. This is accepted: the
   definition is public by design, tool calls are authorized per user by the MCP
   gateway, the HITL floor is Nannos-side, no host MCP server exists yet, and the
   budget guard bounds spend. When the confidential client `cockpit-embed` exists,
   the admin adds it to the binding's `azp` list, the SDK switches, and
   `nannos-embedded` is removed. The entity and its history continue.
8. **One definition per stage.** PR preview environments of the host use the
   stage's bound base URL, not their own. A host developer tests definition changes
   locally against a local console-backend bound to `http://localhost:3000`.

### Alternatives considered

- **Bind by environment config (`EMBED_HOSTS`).** Rejected: a second place to edit
  and deploy for something that is data. The admin console already exists.
- **Drop the entity and synthesize the config per session.** Rejected: it needs a
  new orchestrator branch and endpoint, loses version history and per-agent cost
  tracking, and needs a new home for model tier and thinking level. The entity
  costs nothing once its content is derived.
- **JIT-create one sub-agent per `azp`.** Rejected: `azp` alone carries no base URL,
  the value changes at the switch to the confidential client, and wildcard PR
  origins in dev would make one entity flap between contents.
- **Key by the browser `Origin` header.** Rejected: a server-side host has no
  `Origin`. Identity must come from the token.
- **Per-stage Keycloak clients with a hardcoded origin claim.** Works, but costs
  three clients now and three more later. Not needed once the mapping lives in each
  cluster's database.
- **Mark the sub-agent `system`-owned and public.** It auto-includes the agent for
  every Nannos user without an activation row. Rejected in favour of the
  activation row, so that only users who arrived through the host get it.
- **Inline skill bodies in the version JSON.** Rejected: the codebase stores
  sub-agent-scoped skills as registry rows and resolves them on read; the existing
  version path does that for free.
- **Keycloak group mapper on the `alloy` identity provider.** Does nothing, see
  Context.

## The published format

Served by the host. Only `index.json` is generated. All other files are served byte
for byte, and the digest is SHA-256 over those bytes.

| URL | Content |
|---|---|
| `/.well-known/agent-skills/index.json` | RFC 0.2.0 index: `$schema`, `skills[]` of `{name, type: "skill-md", description, url, digest}`, plus the `x-nannos-agent` extension. |
| `/.well-known/agent-skills/AGENT.md` | YAML frontmatter `name`, `description`, optional `organization`, `model-tier`, `thinking-level`; the body is the system prompt. |
| `/.well-known/agent-skills/<name>/SKILL.md` | agentskills.io skill: frontmatter `name` (equals the directory), `description` (1-1024 chars, "what and when"); the body is the instructions. |

```json
{
  "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
  "skills": [
    { "name": "book-line-items", "type": "skill-md", "description": "…",
      "url": "/.well-known/agent-skills/book-line-items/SKILL.md", "digest": "sha256:…" }
  ],
  "x-nannos-agent": {
    "name": "Alloy AI Assistant",
    "description": "…",
    "prompt": { "url": "/.well-known/agent-skills/AGENT.md", "digest": "sha256:…" },
    "organization": "Ringier Advertising",
    "model_tier": "standard",
    "thinking_level": "low"
  }
}
```

Rules the consumer relies on:

- `url` is resolved against the index URL (RFC 3986 section 5). Path-absolute in
  practice. Cross-origin references are refused.
- `digest` is `sha256:<64 lowercase hex>` over the served bytes. A mismatch is a
  hard error for that fetch. Never use the file.
- `organization`, `model_tier`, `thinking_level` and `tools` are **optional**.
  `model_tier` is one of `low`, `standard`, `premium` (`ModelTier`);
  `thinking_level` is `off` or one of `minimal`, `low`, `medium`, `high`, `xhigh`
  (`ThinkingLevel`). An unknown value is a hard error for that fetch, the same as a
  bad digest, so the last good revision stays in force. Absent means the sub-agent's
  own setting applies. The cockpit publishes no `tools`; a host that does publishes
  scope, not authorization: the MCP gateway still enforces the user's own
  permissions and the HITL floor stays. A host can narrow, never widen.
- **No tool list on either side means every tool** (2026-09-10). A bound sub-agent
  whose authority publishes no `tools` and that has none set in the console runs with
  every tool the user has — the same lazy catalog as the general-purpose agent, never
  the bind-all path. The orchestrator registry sets `all_tools` on the sub-agent
  config when `embed_binding` is present and `mcp_tools` is empty; the runtime then
  treats it like the GP agent. A plain (unbound) sub-agent with an empty list still
  gets the essential tools only. Setting a list in the console, or publishing one,
  narrows it again.
- Unknown fields must be ignored. `x-nannos-agent` is defined by Nannos, here.
- Content type of `.md` may be `text/markdown` or `application/octet-stream` (S3
  guesses by extension). Do not reject on content type.
- The host sets `Cache-Control: max-age` (cockpit prod: 3000 s) and invalidates its
  CDN on deploy. Respect `max-age`, floor 60 s, cap 1 h.
- Bodies must not contain `{{`. `resolve_prompt_placeholders`
  (`orchestrator-agent/app/core/registry.py`) substitutes whitelisted `{{TOKEN}}`
  markers on the composed prompt. Rejected at fetch time to keep it so.

Base URLs for the cockpit: `https://riad.d.alloy.ch` (dev), `https://riad.s.alloy.ch`
(stg), `https://riad.alloy.ch` (prod), `http://localhost:3000` (local). One tenant,
`riad`. Other tenants are legacy.

## Console-backend, as implemented

### Schema — `sqlmigrations/ddl/094_sub_agent_embed_bindings.sql`

- `sub_agent_embed_bindings(sub_agent_id PK → sub_agents, base_url, revision,
  definition JSONB, fetched_at, last_error, last_error_at, last_seen_at,
  azps_seen JSONB, created_by → users, created_at, updated_at)`. `definition` holds
  the parsed agent block and the skill index of the last good revision, for the
  admin view; bodies live in the config version. Changing `base_url` clears both
  `revision` and `definition`, so the admin view never shows the previous host's
  agent and skills under the new URL while its first sync fails.
- `sub_agent_embed_binding_azps(azp PK, sub_agent_id → bindings)`: the primary key
  is the "one azp → one sub-agent per cluster" rule.
- Binding writes (create, replace, remove) go through
  `repositories/embed_binding_repository.py` and are audited as an UPDATE of the
  sub-agent with the binding before and after. Sync bookkeeping (`revision`,
  `definition`, `fetched_at`, `last_error`, `last_seen_at`, `azps_seen`) is written
  directly: it records what the host published or when a token was seen.
- `socket_sessions.embedded_sub_agent_id INTEGER`: the stamp made at connect.
- `ALTER TYPE activation_source ADD VALUE 'embed'` and `ActivationSource.EMBED`.
- `skill_registry.source_type` is untouched: well-known skills become ordinary
  sub-agent-scoped registry rows through the existing version path.

### Fetcher — `services/well_known_agent.py`

`WellKnownAgentClient.fetch(base_url, force=False) → WellKnownDefinition`. Refuses
an authority that resolves to a non-public address (outside local development), streams
every file with a size cap (index 64 KiB, files 256 KiB, ≤ 20 skills), follows at
most 3 redirects and only within the origin, verifies digests, parses frontmatter
with `yaml.safe_load`, validates names, tools, tiers and levels, and caches per base
URL for `max-age` clamped to [60 s, 3600 s]. `revision` is the first 16 hex chars of
the sha256 of a canonical JSON of: `FRAMING_TEMPLATE_VERSION`, the agent block (name,
description, organization, tools, model tier, thinking level, prompt digest) and the
skills sorted by name (name, description, digest) — every served byte through the
digests, plus the index.json-only fields, so a host changing its tool list or a
description re-syncs too. `version_hash_for(revision) = "wk" + revision[:10]`. Any problem raises `WellKnownFetchError(base_url, step, detail)` and
caches nothing. `render_embed_framing` is the Nannos-owned prefix; changing its
wording bumps `FRAMING_TEMPLATE_VERSION` so bound agents re-sync.

### Sync — `services/embed_binding_service.py`

`sync_binding(db, sub_agent_id, force=False)`: fetch; on a fetch error record
`last_error` (logged once per distinct error) and keep the last version; on an
unchanged revision touch `fetched_at`; on a new revision call
`SubAgentService.publish_managed_version(...)` and store revision, definition and
`fetched_at`. `publish_managed_version` writes one version (`version_hash =
wk<rev>`) through `_create_config_version`, moves `current_version`, and approves it
as the default via `repo.approve_version` — signed by the admin who created the
binding (the seeded `system` user if they are gone). `mcp_tools=None`,
`model_tier=None`, and thinking `None/None` carry the values of the **approved
default** version forward, never a pending draft's. The entitlement fingerprint
(`services/entitlement_version.py`) already covers the served agents' default
version hash and the activations, so the orchestrator's discovery and embedded
runnable caches roll over without an explicit bump.

`sync_all()` is a lifespan task in `app.py` next to the session sweep: one pass at
startup, then every `EMBED_SYNC_INTERVAL_SECONDS` (default 300), each binding in its
own transaction.

### Connect and send — `app.py`

- `_resolve_socket_user_via_token` returns `_SocketTokenAuth(http_session_id,
  embedded_sub_agent_id)`. After the user upsert it calls
  `embed_binding_service.bind_connection(db, user, claims["azp"])`, which looks the
  `azp` up (cached 60 s), upserts the activation row (`activated_by = embed`),
  records `last_seen_at` / `azps_seen`, and returns the id. A binding failure logs
  and connects unbound; it never rejects the login. An `azp` with no binding also
  connects unbound, at WARNING: the user is authenticated and Keycloak client
  registration is the gate, but the page then runs the full orchestrator with an
  unscoped conversation list and nothing in the UI says so — the log line is how a
  misconfigured or not-yet-created binding is found.
- `handle_connect` stores the id on the socket session
  (`socket_session_service.create_session(..., embedded_sub_agent_id=)`).
- `handle_initialize_client` answers `client_initialized` with `embeddedAgent`
  alongside the A2A `agent` card: `{subAgentId, name, description, organization,
  revision}`, read from the binding (`_embedded_agent_info`), where `name` is the
  host's PUBLISHED display name, not the hyphenated row name. Absent for console
  sessions and unbound tokens. It exists because the sibling card names the
  orchestrator and the SDK's own fallback reads the well-known index from the PAGE
  origin, which need not be the binding's `base_url`. Resolving it never raises: a
  label is not worth failing the handshake for, and a bound-but-never-synced agent
  falls back to the row name.
- `handle_send_message` calls `_apply_embed_scope(metadata, socket_session)`: it
  drops `executeOnlySubAgentId` / `subAgentId` from the client metadata and sets
  `executeOnlySubAgentId` from the session when bound. The conversation stamp
  `metadata.embedded_sub_agent_id` keeps its name, so the console's labelling and
  existing conversations need no change. The orchestrator is unchanged.
- `GET /api/v1/conversations` scopes a bearer request by the token's `azp`
  (`dependencies.get_client_id_from_request` → `sub_agent_id_for_azp`), overriding
  the `embedded_sub_agent_id` query parameter when bound.

### Admin surface — `routers/sub_agent_router.py`

- `PUT /api/v1/sub-agents/{id}/embed-binding` `{ "base_url", "azps": [...] }`
  (`require_admin`): validates `https://` (plain `http://` only for loopback in
  `ENVIRONMENT=local`), local sub-agent type, `azp` uniqueness across sub-agents;
  writes the rows; syncs immediately and returns the binding with its revision or
  `last_error`. `DELETE` unbinds (the sub-agent keeps its last version and becomes
  fully editable). `POST …/embed-binding/refresh` forces a fetch.
  `GET /api/v1/sub-agents/embed-bindings` lists all bindings.
- `POST /api/v1/sub-agents/embed-bindings` (same body, `require_admin`): the create
  flow. Fetches the host definition FIRST (a bad URL fails with 400 and writes
  nothing), then creates a private local sub-agent named after the published agent
  and owned by the admin (`SubAgentService.create_managed_sub_agent`, a row with
  `current_version = 0` and no version), writes the binding rows and publishes
  version 1 as the approved default — one request, one transaction. Saves the admin
  from inventing placeholder content for a sub-agent whose content the host owns.
- `GET /api/v1/sub-agents/{id}` carries `embed_binding`: base URL, index URL, azps,
  revision, `wk…` hash, fetched at, last error, last seen, azps seen, the published
  agent block (name, description, organization, prompt URL and digest, tools,
  model tier, thinking level) and the skill URLs and digests. The orchestrator-facing
  `GET /api/v1/sub-agents/activated` and `/configs/by-hash/{hash}` carry it too
  (`EmbedBindingService.get_bindings_for`, one query per list), so the registry can
  apply the "no tool list means every tool" rule above.
- `_reject_if_embed_bound` (409): `PATCH` is refused when it touches `description`,
  `system_prompt` or `skills`, and `mcp_tools` / model / thinking only when the
  host publishes them; other fields (name, `is_public`, Nannos-side tools or model)
  stay editable and go through the normal version + approval flow. Revert, delete
  version, set default version and deleting the sub-agent itself are refused
  outright — the delete is soft, so the binding and its azps would outlive the
  agent: remove the binding first. Submit and review are not guarded — approving
  changes no content.
- `services/feature_status.py`: `embed_bindings` entry — disabled with no
  bindings, ready with one line per binding (base URL, azps, revision), degraded
  when a binding has never synced.
- console-frontend. UI vocabulary differs from this document: the UI says
  **embedded agent** for a bound sub-agent, **authority** for the base URL and
  **application** for the host, and never uses the noun "embedding" (taken by vector
  embedding models in Model Gateway). "Create Embedded Agent" on the sub-agents list
  (admin mode) opens the form (`EmbedBindingDialog.tsx`: authority origin + client
  ids) and calls the create flow, then navigates to the new sub-agent. On the
  sub-agent page (`EmbedBindingPanel.tsx`, "Embedded Agent", next to Group Access,
  local sub-agents only) admins in admin mode embed an existing sub-agent, edit,
  refresh and stop embedding; everyone with write access sees the read-only summary
  (authority,
  azps with last-seen, synced revision, published block, last fetch error). A bound
  sub-agent shows an "Embedded" badge; in edit mode the description, system
  prompt and skills render read-only, tools / model / thinking are disabled only
  when the host publishes them, the save omits those fields, and the version
  sidebar hides revert, delete and set-default.

### Tests

`tests/test_well_known_agent.py` (respx fixture tree: happy path, revision
stability, digest mismatch, redirects, sizes, shape errors, optional tools, cache),
`tests/test_embed_binding_service.py` (sync outcomes, carry-forward hand-off,
connect activation, admin validation), `tests/test_embed_binding_router.py`
(field-aware 409 rule), `tests/test_embed_scope.py` (token-path binding, send
scope, conversation scope), `tests/test_socket_connect_auth.py` (stamp at connect),
`tests/test_publish_managed_version.py` (real database: approved default, `wk…`
hash, registry-backed skill, carry-forward of tools and model, tier/model
exclusivity). Migration 091 is applied by the database fixtures.

## SDK and host, as implemented

- **embed-sdk**: `subAgentId` is gone from the config. `NannosCore.isEmbedded()` is
  the bearer-token tell (`getToken`, or `auth` bridged to one); the conversation
  store takes `embedded` and no longer sends `embedded_sub_agent_id` — the server
  scopes — and marks stamped conversations read-only only in the console.
  `executeOnlySubAgentId` left the send payload. `NannosCore.resolveHostAgentName()`
  reads `x-nannos-agent.name` from the page origin's index, same-origin and without
  credentials; `useEmbeddedAgent()` returns the bound sub-agent the handshake named
  (below); `useAgentName()` (panel) returns the adapter's `agentName`, else the bound
  name, else the page-origin index name, else the handshake name, for hosts that
  render their own header. The default header still names the conversation; the
  composer shows the bound agent next to the page-context label, so an embedded
  surface says which assistant answers without the host writing any chrome. Breaking
  for hosts that set `subAgentId`: release as a new minor (0.x).
- **cockpit**: `AGENT.md` frontmatter accepts optional `organization`, `model-tier`,
  `thinking-level`, validated against the Nannos enums at build time and emitted as
  `organization`, `model_tier`, `thinking_level`. `REACT_APP_NANNOS_SUB_AGENT_ID` and
  `subAgentId` are removed from `app/src/nannos/config.ts`, `vite.config.ts` and the
  local `.env`. Docs updated (`docs/nannos-embed/well-known.md`, README, 03-plan).

## Rollout

1. Merge and release console-backend. No behaviour change until a binding exists;
   the sync loop is idle with no bindings.
2. Per cluster, an admin in admin mode either binds the existing cockpit sub-agent
   (id 20 in dev) in its Embedded Agent panel, or — where none exists (stg, prod) —
   uses "Create Embedded Agent" on the sub-agents list. Both take
   `azps = ["nannos-embedded"]` and the base URL for that stage. The
   sync runs and writes the first `wk…` version. From this moment the pasted
   content is superseded and every cockpit user is activated on arrival.
3. Release the SDK and bump the cockpit. Until the bump, the 0.5.x SDK still sends
   `executeOnlySubAgentId`; console-backend ignores it and uses the binding, so the
   order of 2 and 3 does not matter.
4. If ADR-0002 Amendment 4 stage 2 is ever picked up (parked by Amendment 5): add `cockpit-embed` to the binding,
   switch the SDK to `getToken`, then remove `nannos-embedded` from the binding.

## Not in scope

- A definition per PR preview environment. They use the stage's bound base URL.
- Skills registry entries with `source_type='well-known'` for the skills UI.
  Well-known skills appear as the sub-agent's own (sub-agent-scoped) skills.
- Versioned import with approval for third-party hosts. A third-party host would
  need that path; this ADR is for first-party hosts an admin chooses to bind.
- Requiring a specific `azp` for embed mode beyond the binding itself. Once the
  confidential client exists, removing `nannos-embedded` from the binding is that
  requirement.

## Security

- The base URL is admin input, validated to `https://` and an origin only, and the
  fetch follows redirects only within that origin. Timeouts and size caps on every
  response.
- Outside local development the authority must resolve to public addresses only.
  Private, loopback and link-local destinations (cloud metadata included) are
  refused before the first request, on every fetch, so an admin cannot use the
  probe or a binding to reach the cluster's own services. The host is resolved for
  the check and again by the HTTP client, so a DNS answer that changes in between
  is not caught; the input is admin-only, so this is defence in depth. Local
  development lifts the rule for localhost and docker networks.
- Digest verification on every file, every fetch. The index is not trusted more
  than the files it points at.
- The prompt is host-authored prose. That is acceptable exactly because the host
  is first-party, reviewed in its own PR, and bound by a Nannos admin.
- The tool list stays Nannos-side unless the host publishes one, and then it only
  narrows: the gateway enforces per-user permissions on every call, and the HITL
  policy is Nannos-side. This is the "scope by construction" of ADR-0004.
- `model_tier` and `thinking_level` are cost decisions the host may take. The
  budget guard remains the ceiling.
- Until the confidential client ships, a bound public `azp` means any Nannos-realm
  user can reach the embedded agent. See Decision 7 for why this is accepted and
  how it ends.
- No secrets travel. The fetch is an anonymous `GET` of public files.

## What this changes in earlier ADRs

- ADR-0004 "Define a dedicated per-domain sub-agent": the entity stays, but its
  content is host-published and its access is implied by the token's `azp` through
  an admin-authored binding. ADR-0004 anticipated `azp` as the mode trigger; this
  ADR makes console-backend actually read it and stop relaying the client's id.
- ADR-0004 "the frontend/SDK contributes structured data + a capability descriptor
  only — never prose" is about **runtime** turns and still holds. This ADR adds
  **configuration-time**, digest-pinned, reviewed host content. The SDK and the
  browser never carry the prompt.
- ADR-0002 Amendments 4 and 5: unchanged. Stage 2, parked, would replace the
  public `azp` in the binding with the confidential one.
