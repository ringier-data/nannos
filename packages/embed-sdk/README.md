# @nannos/embed-sdk

Embed the Nannos assistant into any React app. You get three things:

1. **A chat panel** (Shadow-DOM isolated, restylable) your users talk to —
   docked beside the page or dropped into any container you own.
2. **In-form actions** — the agent fills/updates the form the user is looking
   at (`apply`), points at fields (`highlight`), moves them (`navigate`), or
   reads what they see (`read_current_page`), through *your* form layer, gated
   by human approval. `apply` and `read_current_page` are ROUND TRIPS: the turn
   pauses until the browser reports what actually happened (which fields
   landed vs. were rejected; the sanitized page snapshot), so the agent never
   assumes success.
3. **Headless tools** — the same agent can read/write your backend via MCP
   when the answer isn't on screen.

ONE React tree (no `mount()`, no second root — context flows into the panel), a headless-first API (the host owns all
chrome: launcher, placement, pin/dock), and the chat state machine runs on the
[Vercel AI SDK](https://ai-sdk.dev) (`useChat` over a custom
`A2AChatTransport` that bridges the Nannos A2A-over-socket.io protocol).
`ai`/`@ai-sdk/react` are exact-pinned, bundled dependencies — hosts never
install or version-manage them.

## Package map

| entry | what | weight |
|---|---|---|
| `@nannos/embed-sdk` | core + React layer (provider, hooks, adapter) — what page code imports | light |
| `@nannos/embed-sdk/core` | framework-free kernel: socket transport, object registry, client-actions, zod-form, PKCE auth | light |
| `@nannos/embed-sdk/react` | same React layer as the root, explicitly | light |
| `@nannos/embed-sdk/transport` | framework-free chat engine: `A2AChatTransport`, message model, stores | light |
| `@nannos/embed-sdk/panel` | the chat UI: `<AssistantPanel>`, `<ShadowPortal>`, blocks, hooks, vendored AI Elements | **heavy — lazy-load it** |
| `@nannos/embed-sdk/styles.css` | the compiled sheet (light-DOM hosts that don't scan the source) | — |

The build is `preserveModules`: one dist file per module, every shared module
exists exactly once across entries (the provider context is a singleton — no
cross-entry footguns), and hosts code-split at module granularity. The panel
entry is the intended lazy boundary:

```tsx
const AssistantPanel = React.lazy(() =>
  import('@nannos/embed-sdk/panel').then((m) => ({ default: m.AssistantPanel })),
);
```

## Quick start

### 1. Provider at the app root

```tsx
import { NannosProvider } from '@nannos/embed-sdk/react';

<NannosProvider
  config={{
    backendUrl: 'https://console.your-nannos.example', // omit for same-origin console usage
    getToken: () => auth.getAccessToken(),             // or `auth={pkce({...})}` — see Auth
    subAgentId: process.env.MY_SUB_AGENT_ID,           // string|number; execute-only scoped agent
  }}
  navigate={(to) => router.push(to)}   // client-action `navigate`
  highlight={myHighlight}              // client-action `highlight` (host DOM knowledge)
  onApplyResult={(t, {rejected}) => rejected.length && toast.warn(...)}
  onError={(e) => Sentry.addBreadcrumb({ category: 'nannos', message: `${e.type}: ${e.message}` })}
  strings={myStringOverrides}          // i18n — see below
>
  <App />
  <MyPanelSurface />                   {/* host-owned container, see step 2 */}
</NannosProvider>
```

The provider owns the connection AND the panel state the host reads:

```ts
const {
  isAvailable, status, isOpen,
  open, close, toggle,                 // open(prompt?, {sendOnOpen?, displayText?, contextKey?})
  isPinned, togglePinned,
  panelWidth, setPanelWidth,           // + NANNOS_PANEL_WIDTH_VAR / clampPanelWidth for docked layouts
  seededPrompt, clearSeededPrompt,
  core,                                // escape hatch: registry, transport, login/logout
} = useAssistant();
```

- **`open(prompt)` DRAFTS by default** — the prompt lands in the composer for
  the user to read and send. `sendOnOpen: true` sends immediately (for triggers
  that already carry the user's decision); `displayText` renders host-authored
  prompts as a muted context chip; `contextKey` starts a fresh conversation
  when the page context changed (`campaign:B` never lands in a chat about
  `campaign:A`).
- **`open()` is gesture-safe**: call it from a real click and the PKCE
  first-login popup runs inside that gesture — never popup-blocked.
- **Pin/width/open state persists** (`localStorage`/`sessionStorage` under
  `storagePrefix`); the pinned width is published as the CSS variable
  `--nannos-panel-width` on the root element, so a docked layout is one line:
  `margin-right: var(--nannos-panel-width, 0px)`.
- **Cmd/Ctrl+J** toggles the panel (`shortcut={false}` to disable).
- Outside a provider `useAssistant()` returns a stable no-op value — pages
  with "Ask AI" affordances render fine without the integration mounted.

### 2. The panel, in a container you own

```tsx
import { AssistantPanel } from '@nannos/embed-sdk/panel';

// Shadow-DOM isolated (default) — for embedding into a foreign design system:
<div style={{ position: 'fixed', top: 0, right: 0, height: '100vh', width: panelWidth }}>
  <AssistantPanel />
</div>

// Light-DOM — for a host that shares the SDK's Tailwind tokens (the console):
<AssistantPanel shadow={false} showConversationList header={false} />
```

`<AssistantPanel>` fills its container. Props: `shadow` (default true),
`hostClassName` (e.g. `'dark'` — dark mode keys off the shadow host element),
`styles` (host override sheets — see Restyling), `header` (`false`, or your
own node), `showConversationList`, and `customHeaders`/`playground` for scoped
surfaces. There is NO built-in launcher — the host owns every trigger.

**Conversation history** comes in two shapes, and you get one of them either
way:

- `showConversationList` — the list as a permanent sidebar. For a wide surface
  (the console's full-page chat).
- Default — the header's history button drops the same list as a popover in the
  thread's top-right corner, leaving the conversation visible behind it. For a
  narrow embedded panel, where a sidebar does not fit. Picking a conversation
  closes it; so does Escape, or a click outside.

The header names the **conversation**, not the agent — that is what changes as
the user moves between chats. An unnamed one reads as `thread.newConversation`
from the strings table, so it translates; `panel.title` now names the panel
region instead. A host that wants its agent's name on screen owns that chrome:
pass your own `header`, and read `useChatEngine().adapter.agentName`.

A host that replaces `header` keeps the overlay by driving it itself:
`useConversationHistory()` returns `{ available, isOpen, open, close, toggle }`.

Each row shows what the conversation is **about**, not what was typed first: the
backend writes a short title plus a one-sentence summary after the first
exchange completes, and stamps the page the conversation started on
(`metadata.page_context`, taken from the `pageContext` your host publishes). The
row renders that origin as `Campaign Summer sale`, with the route as its
tooltip. Rows written before this — or still on their first turn — fall back to
the streamed last message and show no origin.

The list is the newest 50 conversations — the backend's ceiling, and it has no
cursor to page past it. Search filters by title server-side, which is how you
reach anything older.

### 3. Register forms (the in-form loop)

Declare your object types once, derive everything at the call site:

```ts
import { createNannosForm, type ObjectTypeRegistry } from '@nannos/embed-sdk/react';

const types: ObjectTypeRegistry = {
  Invoice: { schema: invoiceSchema, singular: 'Invoice', idShape: 'simple-numeric',
             highlightLabels: { dueDate: 'Due date' } },
};
export const useNannosForm = createNannosForm(types);

// per form — id/scope/label derived from the registry + route id:
useNannosForm({ form, type: 'Invoice', id: invoiceId });
```

(`useNannosZodForm` remains the low-level hook; `useObjectStateAdapter` binds
non-form state containers.) Validation is per field: a value the agent guessed
wrong is skipped while the rest land — wire `onApplyResult` to surface it.

### 4. Publish the current page (live context)

Tell the assistant where the user is NOW. The router only gives a path and a
page title only gives a display name — neither resolves "this campaign" to an
id the agent's tools accept, nor says which tab is open. Pages fill that gap
in LAYERS (the Gatana pattern):

```tsx
import { useAssistant, useNannosPageContext } from '@nannos/embed-sdk/react';

// BASE layer — a bridge watching your router:
useAssistant().setPageContext({ key: location.pathname, title: pageTitle });

// a details page layers the thing on screen on top:
useNannosPageContext({ entity: { type: 'Campaign', id, name } });

// a tab or dialog inside it layers its view state:
useNannosPageContext({ view: { tab: 'targetings' }, visible: rowNames });
```

Fields (`NannosPageContext`): `key` (page identity — required somewhere in the
stack), `title`, `breadcrumbs`, `entity {type, id, name?}`, `view` (active
tab/filter/selection — scalars only), `visible` (names on screen, so "the
second one" resolves). Layers merge in mount order — later wins, `view` merges
key by key — then the snapshot is SANITIZED: per-field caps, a secret-key deny
list (`token`, `password`, `apiKey`, …), and a ~2k whole-payload ceiling that
sheds `visible`, then `view`, first. Everything here reaches a model — declare
named fields; never spread a fetched object in.

What it does:

- the composer's context chip follows navigation (hosts that publish nothing
  keep the old behavior: the chip shows the key the conversation was opened
  under, frozen for its life);
- every send — new turn, steer, HITL resume — carries the merged snapshot as
  `metadata.pageContext`; the orchestrator renders it as a `<current_page>`
  block on the last human message (next to `<client_objects>`, keeping the
  cached system prefix stable), so "this page / here / this campaign" resolves;
- `open(prompt)` defaults its conversation-scoping `contextKey` to the live
  `key`, so an un-keyed "Ask AI" trigger scopes to the page it was pressed on.

**The pull half — page readers.** The snapshot above rides every send, so it
stays small. When the agent needs MORE (`client_action` kind
`read_current_page`), it asks — the turn pauses, the SDK answers, the turn
resumes — and pages answer through registered readers:

```tsx
import { useNannosPageReader } from '@nannos/embed-sdk/react';

useNannosPageReader('lineItems', () => rows.map(({ id, name, state }) => ({ id, name, state })));
useNannosPageReader('unsavedForm', () => form.getValues());
```

`key` names the field in the agent's answer; the read is asked once, on
demand, and sanitized before it leaves the browser (the same secret deny list
at every depth, plus size caps that truncate rather than refuse —
core/page-read.ts). Declare named slices — never hand over a whole fetched
object.

**The screen outline.** Every read ALSO carries the rendered page as a
markdown outline, under the reserved `screen` key — a visibility-respecting
DOM walk (core/screen-outline.ts: headings give levels; real tables AND ARIA
grids like MUI X DataGrid become markdown tables; form controls read as their
value, passwords never; hidden/unmounted content contributes nothing). So a
page with no readers still answers with what the user actually sees. The
outline takes the budget the readers leave (floor 1.5k / ceiling 7k chars) and
is the part that gets cut if the total lands over the 10k cap. Steer it with
two attributes and one marker:

- `data-nannos-ignore` — drop an element and everything under it (host chrome,
  internal nav). The SDK panel is excluded already (shadow boundary).
- `data-nannos-redact` — replace an element's content with `[redacted]`; put
  it on anything that renders a secret's value.
- `data-nannos-read-root` — mark the region worth reading (default: `<main>`,
  else `<body>`).

Open dialogs and toasts (sonner, `role="alert"`) are reported even though they
portal outside the root. Set `screenOutline={false}` on the provider to send
only page context + readers.

## Auth

Unchanged from v1 (ADR-0002): supply exactly one of

- **`getToken`** (host-token, recommended): called on every (re)connect;
  refresh is transparent. The socket AND every REST leg share it.
- **`auth: pkce({ issuer, clientId, redirectUri })`** (self-login fallback):
  connect-on-mount is silent (never pops a login); interactive login runs
  inside `useAssistant().open()`'s gesture. Serve `redirectUri` yourself and
  mount `<NannosAuthCallback />` there (or a static HTML page doing the
  postMessage — see the cockpit's `public/nannos-auth-callback.html`).

`useNannosStatus()` separates `unauthenticated` (fix = login) from
`disconnected` (network) — plus `connecting | connected | authError`.

## The chat engine (what's under the panel)

- **`A2AChatTransport`** implements the AI SDK's `ChatTransport`: one shared
  socket feeds per-conversation `UIMessage` chunk streams. Typed parts carry
  the A2A extras: `data-workplan`, `data-agent-thought`, `data-activity`
  (persisted), `data-task`, `data-feedback-request` (transient).
- **HITL is native tool approval**: an `input-required` interrupt becomes
  `approval-requested` dynamic-tool parts (risk badges from `_risk_metadata`,
  buttons gated by `review_configs`); `addToolApprovalResponse` + the AI SDK's
  `sendAutomaticallyWhen` produce exactly ONE resume send with the batched
  decisions, re-attaching the execute-only directive and the client-object
  manifest.
- **Streaming offsets are code points** (Python `len`), never `.length` —
  reconnect/replay dedupe survives emoji.
- **Steering**: sending while a turn streams routes into the RUNNING turn
  (never interrupts it); reload-mid-turn resumes via the conversation
  snapshot, including a pending approval card.

Recomposition (the console's playground is the reference):

```tsx
import { NannosChatScope, useNannosChat, useConversations,
         Thread, Composer, ApprovalCard, WorkingBlock } from '@nannos/embed-sdk/panel';

<NannosChatScope customHeaders={{ 'X-Playground-SubAgentConfig-Hash': hash }}
                 playground={{ subAgentConfigHash: hash, subAgentName: name }}>
  <MyLayout/>   {/* inside: const chat = useNannosChat(); <Thread chat={chat}/> ... */}
</NannosChatScope>
```

A default `<NannosChatScope>` mounted at the host's layout keeps streaming,
unread counts and reply toasts alive across navigation; `<AssistantPanel>`
reuses a surrounding scope instead of creating a second one.
`useSocketEvent(name, cb)` is the escape hatch for non-chat server events.

## i18n

All chrome strings come from a flat table (`NannosStrings`, English defaults).
Hosts override any subset:

```ts
import { nannosStringKeys, type NannosStrings } from '@nannos/embed-sdk/react';
const strings = Object.fromEntries(nannosStringKeys.map((k) => [k, t(`nannos.sdk.${k}`)]));
<NannosProvider strings={strings} …>
```

Placeholders are single-brace (`{label}`) so they pass through i18next
untouched. The cockpit ships en+de this way.

## Restyling (the host theming contract)

1. **`themeSheet()`** (recommended): a typed builder for the override sheet —
   the `NannosTheme` interface IS the list of what you can change (autocomplete
   + JSDoc per token), and it handles dark mode's specificity correctly:

   ```tsx
   import { themeSheet } from '@nannos/embed-sdk/react'; // eager-safe (also on /panel)

   const BRAND = themeSheet({ accent: '#bb448b', accentForeground: '#fff' });
   <AssistantPanel styles={[BRAND]} />
   ```

   The first argument applies in both color schemes (it beats the SDK's
   built-in dark palette); the second refines tokens for dark mode only:
   `themeSheet({ accent }, { background: '#111' })`.
2. **Token knobs** (what `themeSheet` writes for you): `--nannos-accent`,
   `--nannos-accent-foreground`, `--nannos-radius` derive
   `--primary`/`--ring`/`--radius` — one variable re-brands the palette. Every
   shadcn token on `:host` (see `theme.css`) can also be overridden directly.
   Raw-CSS gotcha: the SDK's `:host(.dark)` block outranks a plain `:host`
   override once `dark` is stamped — repeat overrides under `:host(.dark)`
   (or use `themeSheet`, which does).
3. **Override sheets**: `ShadowPortal`/`AssistantPanel` `styles={[css]}` are
   adopted AFTER the SDK sheet — cascade wins, full restyles possible.
4. **Stable selectors**: interactive blocks carry `data-slot="nannos-…"`.
5. **Light-DOM mode** (`shadow={false}`): host CSS applies natively (`:host`
   sheets from `themeSheet` do NOT — it's shadow-only); the host's Tailwind
   build must scan the SDK source
   (`@source '../../embed-sdk/src'` + the streamdown dist — see the console's
   `index.css`).
6. Dark mode: put `dark` on `hostClassName` (shadow) or a `.dark` ancestor
   (light DOM).

## CSP & origins

| What | Directive | Origin |
|---|---|---|
| socket.io (polling + websocket) | `connect-src` | `{backendUrl}` **and** its `wss:`/`ws:` |
| config discovery + REST legs | `connect-src` | `{backendUrl}` |
| PKCE login popup → OIDC | `connect-src` | the `issuer` origin |
| Shadow-DOM styles | none | constructed sheets (`adoptedStyleSheets`), no `style-src` needed |

Remote backends must allowlist the host origin (`EMBED_ALLOWED_ORIGINS`).

## Development

- `npm run dev` → the harness at `localhost:3000` (proxied to a local
  console-backend on 5001): env switcher (local/stg/prod + token paste), the
  real panel in shadow/light/dark/restyle modes, and a raw-transport view for
  wire debugging.
- `npm test` (vitest): kernel + transport (incl. the S1–S5 AI-SDK integration
  gates run against a scripted wire), stores, provider, i18n.
- `npm run build`: preserveModules ESM + d.ts + `dist/styles.css`.
- `scripts/vendor-ai-elements.mjs` re-vendors the AI Elements set (codemod
  applied automatically; keep local patches minimal).

Integration planning (ontology → client objects → tools → brain) lives in
[`INTEGRATION-PLAYBOOK.md`](./INTEGRATION-PLAYBOOK.md). Known gaps and their
history: [`PENDING.md`](./PENDING.md).
