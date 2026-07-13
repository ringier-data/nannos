# @nannos/embed-sdk

Embed the Nannos assistant into any web app. You get three things:

1. **A chat widget** (Shadow-DOM isolated, styleable) your users talk to.
2. **In-form actions** — the agent fills/updates the form the user is looking at
   (`apply`), points at fields (`highlight`), or moves them (`navigate`), through
   *your* form layer, gated by human approval.
3. **Headless tools** — the same agent can read/write your backend via MCP when
   the answer isn't on screen.

Two integration surfaces:

- **`@nannos/embed-sdk/core`** — framework-free (vanilla TS + Zod). Transport,
  ontology-object registration, client-action execution, PKCE auth.
- **`@nannos/embed-sdk/react`** — React bindings (`useNannosZodForm`).
- **`@nannos/embed-sdk`** — the above + the React chat UI kit and `mount()`.

> **DX status.** The core + form instrumentation are solid. The *drop-in* React
> layer (a `<NannosProvider>`/`<NannosWidget>`) isn't built yet — today you wire a
> few pieces by hand (create+share a core, mount, auth). See
> [Rough edges & the road to drop-in](#rough-edges--the-road-to-drop-in) — that
> section is the honest gap list, and PRs there are the priority.

## Install

```bash
npm i @nannos/embed-sdk
```

Peer dependencies you must already have:

| peer | range | why |
|---|---|---|
| `react`, `react-dom` | `>=18` | the widget + `useNannosZodForm` |
| `zod` | `^4` | schemas are validated/introspected with the host's Zod (shared instance) |

## Quick start (a generic app)

Say your app has an **Invoice** edit form (react-hook-form) and you want the
assistant to fill it.

### 1. Wrap your app in `<NannosProvider>` + drop in `<NannosWidget>`

The provider owns one shared core (creation, connect/disconnect, and
`navigate`/`highlight` wiring). `<NannosWidget>` is a floating launcher + panel;
pass it the shared core via `useNannos()`.

```tsx
import { NannosProvider, useNannos } from '@nannos/embed-sdk/react';
import { NannosWidget } from '@nannos/embed-sdk';

function AssistantMount() {
  return <NannosWidget core={useNannos()} />;
}

export function Root() {
  return (
    <NannosProvider
      config={{
        backendUrl: 'https://console.your-nannos.example',
        getToken: () => auth.getAccessToken(), // see Auth below
        subAgentId: 42,                          // which scoped agent runs (see below)
      }}
      navigate={(to) => router.push(to)}         // for `navigate` client-actions
    >
      <App />
      <AssistantMount />
    </NannosProvider>
  );
}
```

### 2. Register the form

Anywhere under the provider, one hook binds a form. No `core`, no lifecycle:

```tsx
import { useNannosZodForm } from '@nannos/embed-sdk/react';
import { z } from 'zod';

const invoiceSchema = z.object({
  customerName: z.string().describe('Billed customer'),
  amount: z.coerce.string().describe('Total, numeric string e.g. "1990"'),
  status: z.enum(['draft', 'sent', 'paid']).describe('Invoice status'),
});

function InvoiceForm() {
  const form = useForm<InvoiceInputs>();
  useNannosZodForm({
    form,                       // react-hook-form (or any getValues/setValue pair)
    type: 'Invoice',
    id: invoiceId ?? 'new',
    scope: invoiceId ? 'update' : 'create',
    schema: invoiceSchema,      // SDK derives field list, validation, fieldSpecs, getState
    includeValues: true,        // send current values so the agent works from real state
  });
  // ...render your form...
}
```

That's the whole in-form loop. When the user asks the agent to fill the invoice,
it proposes values, the user approves (a tool-call HITL card in the widget), and
the SDK writes them through your `setValue` — validated per-field against the
schema. The human still saves. `navigate`/`highlight` come from the provider props;
`apply` needs nothing extra (it goes through the registered handle).

> **Custom layout?** Skip `<NannosWidget>` and call `mount(useNannos()!, el)` into
> your own sized container (a definite height/width — the widget fills it).
>
> **Import from the same layer:** get `NannosProvider`/`useNannos`/`useNannosZodForm`
> from `@nannos/embed-sdk/react` and `NannosWidget` from `@nannos/embed-sdk`. The
> widget takes `core` explicitly, so nothing depends on the provider context
> crossing entry points.

## Auth

The socket connects with the end user's token (on-behalf-of — the agent acts as
the user, never with more rights). Two paths — pick by whether your host can hand
over a token. **`getToken` and `auth` are mutually exclusive** (if both are set,
`getToken` wins and `auth` is ignored with a warning).

### A. Host-token — recommended when you can federate

If your app already has an SSO session, hand over its on-behalf-of token. This is
the true zero-login drop-in: no second login, no popup, no gesture. The built-in
`<NannosWidget>` launcher just works.

```tsx
<NannosProvider config={{ backendUrl, subAgentId, getToken: () => auth.getAccessToken() }}>
```

`getToken` is called on every (re)connect, so refresh is transparent (return a
fresh/cached token synchronously or as a Promise). See ADR-0002 for the token model.

### B. Self-login (PKCE) — the generic fallback

When the host can't federate, let the widget sign the user into Nannos directly.
Pass `auth={pkce(...)}` — the provider owns it:

```tsx
import { NannosProvider, pkce, NannosAuthCallback } from '@nannos/embed-sdk/react';

<NannosProvider
  config={{ backendUrl, subAgentId }}
  auth={pkce({
    issuer: 'https://login.your-nannos.example/realms/nannos',
    clientId: 'nannos-embedded',
    redirectUri: `${location.origin}/nannos-auth-callback`, // a route you serve (see below)
  })}
  navigate={router.push}
>
  <App />
  <NannosWidget core={useNannos()} />   {/* built-in launcher handles login */}
</NannosProvider>
```

**How it behaves** (this is what makes the built-in launcher work for PKCE):

- **Connect-on-mount is silent.** The provider connects using the strategy's
  *silent* token (cache → refresh → null). It **never** pops a login on mount. A
  null token yields a distinguishable `unauthenticated` status (see below), not an
  opaque "disconnected."
- **Login is gated behind the launcher click.** When the launcher is clicked and
  the strategy isn't authenticated, the widget runs `login()` *inside that gesture*
  (so the popup isn't blocked), then opens and connects. Every reconnect after that
  refreshes silently.
- **`logout()`** is on the core (`useNannos().logout()`): drops the token,
  disconnects, returns to `unauthenticated`.

> **⚠️ Popups need a gesture — inherent to browsers, not fixable in the SDK.**
> `useNannos().open(prompt)` (or `.login()`) triggers first-login fine when called
> from a **real user event** (a click on your own "Ask AI" button). Called from a
> **non-gesture context** (a route effect, an auto-suggest on page load) the popup
> is blocked. For custom triggers, call from the click handler; check
> `core.needsLogin()` if you want to branch.

**The callback route.** PKCE redirects to `redirectUri`, which must be a real route
you serve and register with the IdP. The redirect *logic* is SDK-owned — mount
`<NannosAuthCallback />` there (or call `handleAuthCallback()` from `/core` in a
plain page); you no longer hand-write the postMessage glue:

```tsx
<Route path="/nannos-auth-callback" element={<NannosAuthCallback />} />
```

### Connection status

`useNannosStatus()` returns `connecting | connected | disconnected | unauthenticated
| authError` for host-rendered chrome (a badge, a "sign in" prompt). It separates
`unauthenticated` (the fix is `login()`) from `disconnected` (network) — the opaque
merge of the two is the classic embed debugging trap.

```tsx
const status = useNannosStatus();
if (status === 'unauthenticated') return <SignInHint />;
```

> **Agent URL is auto-resolved.** You only supply `backendUrl`; the SDK discovers the
> orchestrator URL from `{backendUrl}/api/v1/config` on connect. Set
> `adapter.defaults.agentUrl` only to override it.

## Registering on-screen objects

`useNannosZodForm` is the React happy path. Under it:

- **`zodFormRegistration({ schema, adapter, overrides, … })`** (from `/core`) —
  framework-free. Returns a `RegisterInput` for `core.register(...)`. Give it a
  `FormAdapter` (`{ get(field), set(field,value), snapshot() }`) for your form lib.
- **`overrides`** — a `FieldBridge` (`{ read, write }`) per field that has no clean
  1:1 form key (e.g. two ISO date fields ↔ a single `[start, end]` tuple). The SDK
  routes those through the bridge and everything else through `adapter.set`.
- **Manual `core.register({...})`** — full control: supply your own `getState` +
  `apply` (+ optional `fieldSpecs`).
- **Fields-only (no schema)** — `core.register({ type, id, scope, fields: ['a','b'],
  getState, apply })`. Zero schema, but the agent gets no types/enums/validation
  (it guesses values). Fine for trivial cases; the schema is what makes it reliable.

**Data minimization.** `getState`/`includeValues` are bounded to the schema
contract — the declared fields plus bridge reads, never the raw `form.getValues()`.
Undeclared form fields and non-plain values (e.g. the `[Moment, Moment]` tuple
behind a bridged date) don't cross the boundary; the agent sees only what you
declared.

**Rejected fields aren't silent.** `apply` validates each field independently and
returns `{ applied, rejected }`. A value that fails the schema is skipped (it can't
block the good fields) but reported — wire `bindClientActions({ onApplyResult })` to
show "couldn't apply X"; absent a handler, rejections are `console.warn`'d.

## Which sub-agent runs

`subAgentId` in the config selects the scoped domain agent that handles the
conversation (execute-only). It's sent with each turn; the orchestrator validates
it against the signed-in user's accessible agents, so it's safe from the client —
identity is the boundary. Omit it to use the routing orchestrator.

## Config reference

`createNannos(config)`:

| field | required | notes |
|---|---|---|
| `backendUrl` | yes¹ | console-backend origin |
| `getToken` | yes¹ ² | `() => string \| Promise<string>` host-token (on-behalf-of) |
| `auth` | ² | self-login strategy (`pkce({...})`); mutually exclusive with `getToken` |
| `subAgentId` | no | scoped execute-only agent id |
| `socketPath` | no | default `/api/v1/socket.io` |
| `customHeaders` | no | extra headers on `initialize_client` |
| `initTimeoutMs` | no | handshake timeout (default 15s) |

¹ omit `backendUrl`/token only for same-origin console usage (cookie auth).
² supply exactly one of `getToken` (recommended) or `auth` — see Auth above.

## Header label

The chat header auto-resolves the scoped sub-agent's name (from `subAgentId`, via
`GET {backendUrl}/api/v1/sub-agents/{id}`) — so an execute-only embed reads as e.g.
"cockpit-assistant", not the orchestrator's A2A card name. Override it with a
friendlier label via the adapter:

```ts
const adapter: NannosHostAdapter = { agentName: 'Alloy AI Assistant', routing: { … } };
```

Precedence: `adapter.agentName` → resolved sub-agent name → A2A handshake name → `"A2A Assistant"`.

## Deploying in a host app

Practical concerns for shipping the widget inside a real host. Some are shipped,
some are documented gaps — called out so they don't surprise you in staging.

### Bundle size & lazy-loading

Two entry points with very different footprints:

- **`@nannos/embed-sdk/core`** — the lean headless path (~97 KB / ~27 KB gzipped):
  transport, registry, client-actions, zod-form, auth. No React UI, no Radix, no
  markdown renderer. Use this if you render your own UI.
- **`@nannos/embed-sdk`** (root, includes the widget) — ~788 KB / ~194 KB gzipped.
  It carries a full UI-primitive set (13 `@radix-ui/*`), `react-markdown`, and
  `socket.io-client`. For an MUI host that's a second component system in the bundle.

The widget already **defers `createRoot` until the panel opens**, but the JS still
lands in your main chunk unless you split it. Recommended: **dynamic-import the
widget entry behind the launcher** so it's a separate async chunk:

```tsx
const NannosWidget = React.lazy(() =>
  import('@nannos/embed-sdk').then((m) => ({ default: m.NannosWidget })),
);
// render <Suspense><NannosWidget core={useNannos()} /></Suspense> only once opened,
// or gate the import on a lightweight launcher you own.
```

`@nannos/embed-sdk/core` and `/react` (the provider/hooks) are small enough to stay
in the main chunk; only the root widget entry is worth splitting.

### CSP & origins

The SDK opens these network/DOM channels — an enterprise host with a strict
Content-Security-Policy must allowlist them:

| What | Directive | Origin |
|---|---|---|
| socket.io (polling + websocket) | `connect-src` | `{backendUrl}` **and** its `wss:`/`ws:` |
| config discovery + REST legs (feedback, upload, sub-agent name) | `connect-src` | `{backendUrl}` |
| PKCE login popup → OIDC | `connect-src` (discovery/token fetch) | the `issuer` origin |
| PKCE popup window | (popup, not framed) | the `issuer` origin — no `frame-src` needed |
| callback `postMessage` | — | the `redirectUri` origin talks back to the app origin (same-origin by default) |
| Shadow-DOM styles (`adoptedStyleSheets`) | none | no `style-src` entry needed (constructed sheet, not injected `<style>`) |

Host-token path (`getToken`) needs only the `{backendUrl}` `connect-src` entries.

### Host theming

- **Launcher color:** `<NannosWidget accent="#4D418D" />` — set your brand color so
  the widget doesn't read as a third-party bolt-on. ✅ shipped.
- **Panel design tokens:** the shadow DOM declares `--nannos-radius` and
  `--nannos-accent` on `:host`; the shadcn tokens (`--background`, `--primary`, …)
  drive the chat surface. **Known gap:** there's no first-class prop yet to
  restyle those from the host — deeper token theming (brand-mapped `--primary`,
  surfaces) is planned. Today, launcher color + `agentName` cover most of the
  "feels native" gap.

### Localization (known gap)

The widget chrome is currently **English-only** — UI strings are hardcoded and
there's no `locale`/messages prop. A localized host gets an English assistant
panel. **Planned:** a string-override seam (host passes its active language + a
messages map, or overrides the SDK's string table) scoped to the compact embed's
~20 visible strings. Track this before rolling out to non-English tenants.

### Errors & telemetry

Two seams:

- **`useNannosStatus()`** — coarse connection state (`connecting | connected |
  disconnected | unauthenticated | authError`) for host chrome.
- **`onError`** on `<NannosProvider>` — forwards SDK-internal failures to your
  monitoring (Sentry etc.):

  ```tsx
  <NannosProvider config={…} onError={(e) => Sentry.captureException(e.cause ?? new Error(e.message), {
    tags: { nannos_error: e.type }, extra: e.detail,
  })}>
  ```

  Event shape: `{ type: 'connection' | 'init' | 'auth' | 'apply', message, cause?, detail? }`
  — socket `connect_error` (`connection`), `initialize_client` reject/timeout
  (`init`), `getToken()`/interactive-login failure (`auth`), and a client-action
  handler that threw (`apply`). These are diagnostics; the SDK still degrades
  gracefully (status flips, retries). Headless hosts subscribe via `core.onError(cb)`.

  Per-field apply *rejections* (a value that failed validation, not a thrown
  error) come through `bindClientActions({ onApplyResult })` instead.

---

## Rough edges & the road to drop-in

Honest assessment from writing this guide. The **core API is right**, and the
React integration layer (`@nannos/embed-sdk/react` + `<NannosWidget>`) closes
most of the gap to "almost no-config drop-in". What's **shipped** vs. what's
**still config**:

**Resolved by the React layer:**

1. ✅ **Core creation + sharing** — `<NannosProvider>` owns one core in context;
   `useNannosZodForm`/`useNannos()` read it. No hand-rolled singleton, no
   "second core → empty registry" footgun.

2. ✅ **Mounting + sizing** — `<NannosWidget core={useNannos()} />` renders a
   floating launcher + panel; no `mount`, no container sizing. (Bespoke layout
   still available via `mount()`.)

3. ✅ **`connect()` lifecycle** — the provider connects on mount and disconnects
   on unmount.

4. ✅ **`navigate`/`highlight`** — provider props (`navigate={router.push}`),
   folded in instead of a separate `bindClientActions` call.

5. ✅ **`useNannosZodForm` needs no `core`** — it reads context:
   `useNannosZodForm({ form, type, id, scope, schema })`.

6. ✅ **Imperative handle for custom triggers** — `useNannos()` returns the core,
   which now carries `open(prompt?)`, `close()`, `toggle()`, `sendPrompt()`. A
   host's own launcher ("Ask AI" next to a form) opens the panel and injects a
   prompt in one call — no `window` CustomEvent bus. `<NannosWidget>` mirrors the
   same core state, so every trigger agrees.

7. ✅ **Agent-URL discovery** — the SDK resolves the orchestrator URL from
   `{backendUrl}/api/v1/config` on connect; the embedder only supplies `backendUrl`.

8. ✅ **Auth path + PKCE gesture** — `<NannosProvider auth={pkce({issuer, clientId})}>`.
   Connect-on-mount is silent (never pops a login); the widget launcher runs
   `login()` inside its click, so the built-in launcher works for PKCE with zero
   custom auth code. `handleAuthCallback()` / `<NannosAuthCallback>` own the
   redirect-page logic (you still register + serve the route). Host-token
   (`getToken`) stays the recommended zero-login path.

9. ✅ **Connection status** — `useNannosStatus()` surfaces `connecting | connected
   | disconnected | unauthenticated | authError`; `unauthenticated` is distinct from
   a network drop.

10. ✅ **Data minimization** — `getState`/`includeValues` project to the schema
    contract; the raw form snapshot (undeclared fields, `[Moment,…]` tuples) no
    longer leaks past the boundary.

11. ✅ **Rejected fields surfaced** — `apply` returns `{ applied, rejected }`;
    `onApplyResult` (or a `console.warn` fallback) makes a dropped value visible.

12. ✅ **Reactive `enabled`** — a runtime flag (LaunchDarkly etc.) can flip the
    assistant on/off; the core is created once when first enabled (no fetch/popup
    while disabled). `config`/`auth` are captured at creation — change them by
    remounting the provider with a `key`.

13. ✅ **Form re-registration** — `useNannosZodForm` re-registers on a schema/bridge
    SHAPE signature (field names + bridge keys), so an inline-built `overrides` isn't
    silently stale when a field is added/removed. (Changing a bridge *body* with the
    same keys still needs a stable reference — documented at the call site.)

14. ✅ **StrictMode-safe** — `disconnect()` nulls the socket, so React's
    mount→unmount→mount double-invoke reconnects cleanly (regression-tested).

**Still open:**

15. **Callback route still needs host wiring.** `redirectUri` must be a route you
    serve and register with the IdP — the SDK owns the *logic* (`<NannosAuthCallback>`),
    not the hosting. A fully SDK-hosted default would need Nannos-origin infra.

16. **`subAgentId` coercion + dev `staticToken`** — minor ergonomics from the field
    report (env vars are strings; a dev-only static token with a clear "no token"
    error). Not yet.

17. **Agent self-correction on rejects** — `apply` now reports rejected fields to
    the host, but they aren't yet fed back to the agent as a tool-result so it can
    retry. That's a protocol round-trip (client_action ack), tracked separately.

18. **Cross-entry-point discipline** — `<NannosWidget>` deliberately takes `core`
   as a prop (from `useNannos()`) rather than reading context, because vite's
   multi-entry lib build inlines shared modules and would duplicate the provider
   context across `/react` and the root entry. Import provider/hooks from
   `@nannos/embed-sdk/react`, widget from the root. Documented, but a sharper
   packaging (single context chunk) would remove the footgun entirely.

19. **Localization** — the widget chrome is English-only (hardcoded strings, no
    `locale`/messages prop). A blocker for localized multi-tenant hosts. Planned:
    a string-override seam scoped to the compact embed's ~20 visible strings. See
    "Deploying in a host app › Localization".

20. **Deep host theming** — launcher `accent` is a prop (shipped), but there's no
    first-class way to brand the panel's shadcn tokens (`--primary`, surfaces).
    Planned. See "Deploying in a host app › Host theming".

21. ✅ **Error/telemetry seam** — `<NannosProvider onError={…}>` (and
    `core.onError()` for headless) forwards connection/init/auth/apply failures to
    host monitoring; `useNannosStatus()` covers connection state. See "Deploying in
    a host app › Errors & telemetry".

**The drop-in shape — now real for both auth paths:**

```tsx
// once, at the app root
<NannosProvider
  config={{ backendUrl: 'https://console.your-nannos.example', subAgentId: 42 }}
  auth={pkce({ issuer, clientId: 'nannos-embedded', redirectUri })}  // or config.getToken
  navigate={router.push}
>
  <App />
  <AssistantMount />         {/* <NannosWidget core={useNannos()} /> */}
</NannosProvider>

// per form — no core, no mount, no lifecycle
useNannosZodForm({ form, type: 'Invoice', id, scope, schema });
```

Irreducible config even then: `backendUrl`, an auth path (`getToken` or `auth`), and
(optionally) `subAgentId`. Everything else — core creation/sharing, connect, mount, sizing,
adapter, field derivation, HITL — is handled by the SDK.
