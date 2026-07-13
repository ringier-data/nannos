# Embedded Nannos — pending work

Tracking the deferred items for the embed SDK (`@nannos/embed-sdk`) and its
consumers (cockpit, console). Everything here is **known and intentionally
deferred** — the shipped surface is documented in `README.md` ("Rough edges" +
"Deploying in a host app"). Ordered by priority.

Status legend: 🔴 blocker for a real rollout · 🟡 real gap, not blocking · 🟢 nice-to-have

---

## 1. 🟡 Localization (i18n) — defer; fold into a Nannos-wide effort

**Problem.** The widget's UI strings are hardcoded English (header, launcher,
"Start a conversation", input placeholder, connection status, HITL card). A
localized multi-tenant host (cockpit runs full i18next; rule: no hardcoded
strings) gets an English assistant bolted into a German/French app. An integrator
flagged this as a rollout blocker for non-English tenants.

**Why deferred (not an embed-local fix).** The chat UI is **shared** — the console
consumes the same components ("one UI"). i18n is a Nannos-wide gap (the console
itself isn't localized), so solving it only inside the embed SDK would:
- fork string handling (an embed-only table the console doesn't use), and
- still leave the product English-only everywhere else.

The right shape is a **product-level string layer** the shared chat components
read from, which both the console and the embed inherit — not an embed-specific
seam. So this is blocked on / should be designed with the broader Nannos i18n
strategy, not shipped as a one-off here.

**If a specific tenant forces it sooner:** the tactical unblock is still the
override seam (extract the ~20 compact-surface strings, accept `messages`/`locale`
on the provider) — but treat that as a stopgap, and design it so the same string
keys/table are what the product-level solution would adopt (avoid throwaway work).

**Touches (whenever done).** Shared string table read by `ChatApp`, `ChatInput`,
`ConnectionStatus`, HITL card, MessageList empty-state; host passes locale/messages.

**Effort.** Medium-large, and cross-cutting — hence product-level, not embed-local.

---

## 2. 🟡 Deep host theming (panel tokens)

**Shipped.** `<NannosWidget accent="…" />` colors the launcher.

**Gap.** No first-class way to brand the panel's shadcn tokens (`--primary`,
surfaces, radius) from the host. The shadow DOM declares `--nannos-accent` /
`--nannos-radius` on `:host` but nothing maps them to the chat's actual accent
(`--primary`). The widget can't yet be made to fully match a host brand.

**Plan.** Accept a small `theme` object (accent/radius/surface) on the
provider/widget; set it as CSS custom properties on the mount container and map
the relevant shadcn tokens (`--primary`, `--ring`, `--primary-foreground`
contrast) to them in `theme.css`. Keep it a small, safe subset — not full token
control — to avoid contrast/accessibility regressions.

**Touches.** `NannosWidget` (accept `theme`), `element.tsx` mount (set vars on
container), `ui/theme.css` (wire `--primary`/`--ring` to `var(--nannos-accent)`
with fallback).

**Effort.** Small-medium. Main risk is contrast on auto-mapped foregrounds.

---

## 3. 🟡 Auth: bundled PKCE preset + callback

**Shipped.** `auth={pkce({ issuer, clientId, redirectUri })}`; launcher-gated
login; `handleAuthCallback()` / `<NannosAuthCallback>` own the redirect logic;
first-login reconnect fixed.

**Gap.** The host still (a) registers + serves the `redirectUri` route and (b)
supplies issuer/clientId. A fully SDK-hosted callback default would need
Nannos-origin infrastructure.

**Plan.** Ship an opinionated PKCE preset and, if we stand up Nannos-origin
hosting, a default callback so `redirectUri` becomes optional. Until then this is
documented, not automated.

**Effort.** Small (preset) + infra dependency (hosted callback).

---

## 4. 🟡 Agent self-correction on apply rejections

**Shipped.** `apply` returns `{ applied, rejected }`; `onApplyResult` (or a
`console.warn` fallback) surfaces rejected fields to the host.

**Gap.** Rejections reach the *host*, not the *agent* — so the agent can't retry
a mis-typed value. Needs a `client_action` result/ack round-trip back over the
socket into the agent's tool result.

**Plan.** Define a client-action ack message; wire `executeClientAction`'s result
back through the transport so the orchestrator feeds it to the sub-agent as the
tool result. Protocol change (agent-common + orchestrator + SDK).

**Effort.** Medium (cross-package protocol work).

---

## 5. 🟡 client-action kind parity across the three copies

**Problem.** The client-action directive `kind`s live in **three** places that
must stay in lockstep (see `a2a-extensions.json` `_clientActionKindsComment`):
1. `agent-common` `client_action_tool.py` — the tool's arg schema (what the agent can emit),
2. `embed-sdk` `schemas.ts` — the widget's zod boundary (what the client accepts),
3. the risk scorer — a deterministic score per kind.

A kind added to the Python tool but missing from the zod union is **emitted by
the agent, refused client-side, and reported to the user as done** — a silent
correctness failure. Today only prose comments guard this.

**Plan.** Make the parity enforceable, not documented: a single source of truth
for the kind list (or a test that asserts the three sets are equal + the scorer
has an entry per kind). Cheapest: a CI test in agent-common that imports the kind
list and cross-checks the scorer, plus an embed-sdk test pinning the zod union to
the same list (via `a2a-extensions.json` as the canonical list).

**Effort.** Small. High value — turns a silent footgun into a failing test.

---

## 6. 🟢 Multi-tab token sharing (PKCE)

**Gap.** The PKCE token is `sessionStorage`-backed, so a login in tab A doesn't
satisfy tab B's next reconnect (each tab logs in separately).

**Plan.** Optional `localStorage` + `storage` event (or BroadcastChannel) sync so
a login propagates across tabs. Opt-in — `sessionStorage` is the safer default
for shared machines.

**Effort.** Small.

---

## 7. 🟢 Minor config ergonomics

- **`subAgentId: string | number`.** Env vars are strings; accept both and coerce
  (host currently does `Number(...)` with NaN handling).
- **Dev `staticToken`.** A first-class dev-only static-token option with a clear
  "no token" error instead of the current empty-string → opaque socket failure.

**Effort.** Trivial.

---

## 8. 🟢 Cross-entry-point packaging

**Gap.** `<NannosWidget>` deliberately takes `core` as a prop (not context)
because vite's multi-entry lib build inlines shared modules and would duplicate
the provider context across `/react` and the root entry. It works, but the
prop-threading is a documented footgun.

**Plan.** A sharper packaging (single shared context chunk / a `peerDependencies`
-style dedupe) so the widget can read context directly.

**Effort.** Medium (build config), low urgency.

---

## Not in scope here (owned elsewhere)

- **Cockpit integration code** (`rcplus-alloy-cockpit-frontend`, branch
  `feature/nannos-embed-spike`): the host adapter, provider wiring, campaign
  schema/bridge, callback route. Committed on the cockpit side.
- **Cockpit MCP server** provisioning (cockpit backend repo).

---

## Recommended order

1. **#5 kind parity** — cheap, removes a silent correctness footgun. Do first.
2. **#2 theming** — the "feels native" finish; small, embed-local.
3. **#4 apply self-correction** and **#3 auth preset** — larger, schedule as
   protocol/infra work lands.
4. **#6/#7/#8** — opportunistic.
5. **#1 i18n** — real, but **not embed-local**: fold into a Nannos-wide i18n
   effort so the console and embed share one solution. Only do a tactical embed
   stopgap if a specific tenant forces it before the product-level work.
