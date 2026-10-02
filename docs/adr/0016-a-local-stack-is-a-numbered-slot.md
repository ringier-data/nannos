---
status: proposed (2026-10-02)
---

# A local stack is a numbered slot on one shared Postgres

Several local stacks must run side by side — one per agent or worktree — without any of them seeing
another's data, ports, gateway or browser session. We decided that **a stack is a numbered slot**
(0–8) that owns a fixed port block, its own cookie names, its own databases and its own Model
Gateway container, and that the slots **share only the Postgres servers and the identity
provider** — the real IdP, or the one local Keycloak. Slot 0
is today's `start-local` unchanged; slots 1–8 are claimed by `just up`, which runs headless and
returns when the stack is healthy.

Concretely, slot N (1–8):

| | slot 0 (today) | slot N |
|---|---|---|
| Browser cookies | `a2a-chatui`, `session` | `a2a-chatui-sN`, `session-sN` |
| console-backend | 5001 | 4N001 |
| console-frontend (vite) | 5173 | 4N173 |
| agent-runner | 5005 | 4N005 |
| orchestrator | 10001 | 4N010 |
| voice-agent | 8002 | 4N002 |
| soffice-worker | 8090 | 4N090 |
| Model Gateway | 4000, `nannos-litellm-proxy-local` | 4N400, `nannos-gw-sN` |
| debugpy | 5678/5679/5682/5683 | 4N678/4N679/4N682/4N683 |
| Databases | `console`, `docstore` | `console_sN`, `docstore_sN` on the same two servers |
| IdP group prefix | `local-` | `local-sN-` |
| Logs, PIDs, claim | `logs/` | `~/.nannos/slots/N/` |

Every block lies below the ephemeral range (49152) and clear of the shared Postgres ports (5401,
5402) and the local Keycloak (8180). Adding N×100 to today's ports was rejected because slot 4's
backend would land on 5401.

- **`just up`** claims the lowest free slot by `mkdir ~/.nannos/slots/N` (atomic, no lock server)
  and records the claim (worktree, the PID of `up` itself, flags). It starts the shared Postgres
  servers and Keycloak if they are down, creates the slot's databases if absent, applies **that
  worktree's** migrations, starts the gateway and the services as background process groups from
  the same procs file mprocs reads, waits for every health check, and prints one JSON object (URLs,
  databases, log path). It never prompts. A worktree holds at most one slot; `up` from a worktree
  whose slot runs prints that slot instead of claiming another.
- **`just down [N]`** stops the slot's processes and gateway container, drops its databases and
  releases the claim. `--keep-db` keeps the databases — with their migration hashes and, without
  S3, their uploaded files — for the next `up` **from the same worktree**: kept databases record
  the worktree that created them, and no other worktree's `up` takes that slot.
- **`just slots`** lists claims; **`just slots-gc`** releases every slot in which neither `up` nor
  any service process is alive any more, through the same path as `down`.
- **`just db-reset N`** drops and recreates the slot's databases and re-applies migrations.
- **`just up --from-slot0`** starts a slot's *new* databases as a copy of slot 0's (agents,
  registered models and rate cards, users, conversations) and then applies the worktree's pending
  migrations, so a branch that only adds migrations runs against realistic data. It is refused when
  slot 0 has applied a migration the worktree does not have, or applied one with other contents —
  slot 0 runs whichever branch started it last, so `start-local` records the contents slot 0
  applied each migration with. Migrations slot 0 applied before it kept that record are recorded
  as unknown and assumed to match, with a warning. The copy's scheduled jobs are suspended with a reason, and the work slot 0 had in flight
  (owed notices, pending retries, running runs, queued catalog syncs) is dropped.

## Why

- **The real IdP pins exact redirect URIs.** The `agent-console` client accepts
  `http://localhost:5001/api/v1/auth/login-callback` and nothing else on loopback, so a stack on any
  other port cannot sign in. Registering a fixed, finite set — the login, broker and logout
  callbacks and the web origin on `localhost:4N001` (and `127.0.0.1:4N001`), N = 1–8 — keeps the
  client free of wildcards. console-backend builds its callback with `request.url_for`, and the
  Vite proxy rewrites `Host` to the backend (`changeOrigin`), so a slot already asks for exactly
  `http://localhost:4N001/api/v1/auth/login-callback`.
- **The local Keycloak is told about each slot.** Its realm export allows slot 0's ports. `up`
  adds the slot's backend and frontend to the local `agent-console` client through the admin API,
  as it already sets the client secrets there — re-reading and retrying, since two slots may
  register at the same moment.
- **Cookies are scoped by host, not port, so a slot names its own.** Two stacks on `localhost`
  overwrite each other's `a2a-chatui` session, and the Starlette `session` cookie that carries the
  OAuth state mid-sign-in. Both names are configurable (`SESSION_COOKIE_NAME`,
  `OAUTH_STATE_COOKIE_NAME`) and a slot appends `-sN`. A hostname per slot (`sN.localhost`) was the
  first idea and does not work: the sign-in callback is the backend's own `localhost` URL whatever
  host the browser used, so the session cookie lands on `localhost` anyway. The slot's frontend
  origin joins `CORS_ALLOWED_CHAT_ORIGINS`, because Socket.IO checks `Origin` on every handshake.
- **Migrations change per branch, so each slot migrates itself.** A pre-migrated template database
  cloned per slot would be fast, but it would be the wrong schema for every branch that adds or
  edits a migration. Applying the worktree's own migrations takes about 6 s per database when
  nothing is pending, once the image builds use the local builder. Rambler records versions, not
  contents, so `up` keeps a hash of every applied file (beside the claims, since `--keep-db`
  outlives a claim) and refuses to start, pointing at `db-reset`, when an applied file has changed
  since.
- **Sharing the Postgres servers costs nothing in isolation.** A database per slot separates data
  and checkpoints completely; a server per slot would add two containers and a boot per stack on a
  machine that is already CPU-bound. The Model Gateway is per slot because it stores registered
  models in its own schema of the console database, and a shared one would share registrations.
- **Group names are the one write into shared identity.** console-backend creates IdP groups under
  `KEYCLOAK_GROUP_NAME_PREFIX`. Under one `local-` prefix, slots would rename, sync and delete one
  another's groups. A prefix per slot keeps them apart.
- **Agents need a command, not a terminal UI.** The plan confirmation and mprocs work for a person
  and stall an agent. `up` blocks until healthy and returns machine-readable output; slot 0 keeps
  mprocs. A worktree has no `.env` or local model list of its own (both are gitignored), so a stack
  started from one reads the main checkout's.

- **A copy must not act on slot 0's behalf.** Copied as is, every scheduled job would fire twice
  and notify the same people through the same bots, and slot 0's owed notices would be delivered
  again. Suspension is the scheduler's own off switch for a job, visible in the console with its
  reason and undone by an admin there, so a copy uses it rather than a slot-only flag. The notice
  queue ignores suspension, so owed notices are cleared outright. A copy also runs without the
  IdP admin client: its groups carry slot 0's IdP group ids and its users slot 0's identities, so
  renaming a group or changing a phone number in the copy would change them for slot 0. Its
  outbound SCIM endpoints are disabled and their tokens blanked, and the nightly SCIM push is off,
  for the same reason: they point at slot 0's real downstream systems.

## Alternatives considered

- **Local Kubernetes (kind or k3d) with a namespace per stack.** The cleanest isolation boundary.
  Rejected because the constraint is CPU, and a control plane in the same Docker VM adds load;
  because every code edit becomes an image build and load (or extra sync tooling) where the
  services now hot-reload in seconds; and because the deployment manifests describe production,
  not a dev loop. Manifests keep their own check (`just recon`).
- **A full compose project per stack** (own Postgres and Keycloak per slot). Simple to build, but it
  multiplies the heaviest containers and their boot time by the number of stacks, and the real IdP,
  which most stacks use, cannot be duplicated anyway.
- **One template database cloned per slot.** Rejected for the migration reason above.
- **A router on :5001 that sends each sign-in callback to the right slot.** Keeps the IdP client
  unchanged, but every callback arrives on the same host and port, so the router would have to
  recover the slot from the OIDC `state` or a cookie — a fragile layer in front of authentication.
- **Wildcard redirect URIs on the IdP.** The only wildcard the IdP offers is a trailing one, so "any
  loopback port" becomes the prefix `http://localhost*` — which also matches hosts such as
  `localhost.example.com`, on a client that production shares.
- **Caching the SSM secrets on disk between runs.** Saves seconds but leaves production secrets in
  plaintext under the home directory. Fetching them with batched `get-parameters` calls instead of
  one call per secret is fast enough.

## Consequences

- The IdP change (slots 1–8 on the `agent-console` client) must be applied before slots can sign
  in against it; `--local-idp` runs a slot on the local Keycloak meanwhile. A slot 9 needs another
  registration.
- The channel clients are slot 0 only. Slack and Google Chat each bind one external app, so slots
  1–8 start without them; the voice agent runs, but Twilio's webhook reaches only the public URL
  it was given. A slot that tests channel delivery has to take over slot 0.
- Google Drive catalog connect registers its own OAuth redirect (`localhost:5001`) with Google, so
  it works in slot 0 only until that client also lists the slot URIs.
- When object storage is S3, slots share the buckets. A slot started empty generates its own ids, so
  its keys do not collide, but `down` and `db-reset` leave its objects behind. A `--from-slot0` copy
  shares slot 0's ids and therefore slot 0's objects: deleting a file, conversation or catalog in
  the copy deletes it for slot 0 as well. Catalog auto-sync is off in a copy for the same reason.
  Without S3, a copy does not bring slot 0's local uploads along (they live in whichever worktree
  ran slot 0): its file records point at files the slot does not have.
- A copy takes as long as `pg_dump | pg_restore` of slot 0's databases — seconds on an idle
  machine, ten minutes on a saturated one.
- IdP groups created by a slot (`local-sN-*`) outlive it; `down` does not delete them. A later
  `up` in the same slot reuses the prefix.
- The shared Postgres servers and Keycloak stay up across slots; `stop-local` and `reset-local`
  affect every slot and say so. The realm file is mounted from one fixed path
  (`~/.nannos/keycloak/`): mounted relative to each checkout, it changed the compose config hash, so
  a start from another worktree re-created Keycloak under every stack using it.
- Two startup costs this rests on are fixed first, independent of slots: the migration images are
  built with the local Docker builder rather than whatever buildx builder is the default (70 s →
  about 7 s when nothing changed), and the SSM secrets are fetched with batched calls (about 60 s →
  under 10 s).
