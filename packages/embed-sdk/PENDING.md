# Pending / tracked gaps

Status after the v2 rewrite (AI SDK chat substrate, one-React-tree ShadowPortal,
headless-first panel). Items from the v1 list that the rewrite CLOSED are
recorded at the bottom so their history stays greppable.

---

## Open

### 1. PKCE preset / hosted callback (was v1 #3)
`redirectUri` must still be a route/asset the HOST serves and registers with
the IdP. The SDK owns the logic (`<NannosAuthCallback>`, `handleAuthCallback`),
not the hosting. A fully SDK-hosted default needs Nannos-origin infra.

### 3. Multi-tab PKCE token sharing (was v1 #6)
The token cache is `sessionStorage` (per tab); each new tab runs its own silent
refresh or first-login. Fine for the embed's short-lived tokens, annoying for
heavy multi-tab users.

### 5. Live smoke of the v2 panel against stg/prod
The engine is spike-verified headless (S1–S5) and against recorded wire
semantics; the panel needs a human pass on a real backend per environment
(dev harness: `npm run dev` → localhost:3000, env switcher + token paste).

### 6. shiki grammar fan-out under CRA (cockpit build hygiene)
The bundled streamdown/shiki stack ships every grammar as a lazy module;
webpack pre-builds an async chunk per grammar. Runtime cost is lazy/none, but
cockpit build time and chunk count should be watched; if it hurts, restrict
the code plugin's language set at vendor time.

---

## Closed 2026-08-26: the client-action round trip (was #2 and #4)

Both closed by ONE mechanism, riding the HITL rails end to end (no new socket
events, console-backend untouched):

- **#2 Apply-result ack**: `client_action` kind `apply` now `interrupt()`s with
  `{client_action_request: {id, directive}}`; the executor emits it as
  `input_required` + the client-action extension (`{"request"}` payload); the
  SDK surfaces it as a `_clientActionRequest`-marked approval part that
  `useNannosChat` AUTO-settles — executes against the registry, answers via the
  decision envelope (`client_action_result`) — and `sendAutomaticallyWhen`
  resumes the turn. The tool returns the REAL `{applied, rejected}` to the
  model. No result (user typed instead / page closed) resumes as an explicit
  `{ok:false, reason:'no-result'}`; a reload re-executes via the
  restored-interrupt path. The directive rides the interrupt value only, so the
  resume replay cannot double-execute.
- **#4 `read_current_page`**: a fourth kind on the same round trip. Answered
  SDK-side from the merged page context + host-registered readers
  (`useNannosPageReader(key, () => …)`), sanitized by the ported Gatana read
  sanitizer (deny list at every depth, caps, truncate-not-refuse —
  core/page-read.ts). Kind pinned in a2a-extensions.json + risk-scored 0.1
  (read-only, never gates). Gatana's DOM screen outline IS ported
  (core/screen-outline.ts, 2026-08-26): every read carries the rendered page
  as a markdown outline under the reserved `screen` key, sized to the budget
  the readers leave (sanitizeReadResultWithScreen). Host-agnostic walk: real
  tables + ARIA grids (MUI X DataGrid), shadcn data-slots + MUI skeletons,
  `data-nannos-ignore`/`-redact`/`-read-root`. Provider default ON
  (`screenOutline={false}` to opt out).

## Closed by the v2 rewrite

- **i18n string seam** (v1 #1): `NannosStrings` table, English defaults,
  `strings` provider prop; cockpit ships en+de.
- **Panel theming** (v1 #2): `--nannos-accent`/`--nannos-radius` now DERIVE
  `--primary`/`--ring`/`--radius`; arbitrary override sheets via
  `ShadowPortal styles`; stable `data-slot` hooks.
- **Client-action kind parity** (v1 #5): stays CI-pinned against
  `a2a-extensions.json` (schemas.test.ts).
- **`subAgentId: string | number` + dev token ergonomics** (v1 #7): type
  widened; the dev harness covers the pasted-token workflow.
- **Cross-entry-point packaging** (v1 #8): `preserveModules` — every module
  exists exactly once across entries; the provider context is a singleton; the
  `@/` alias (and its broken d.ts specifiers) is gone.
- **Hand-rolled chat state machine**: replaced by AI SDK v7 `useChat` over
  `A2AChatTransport` (exact-pinned `ai`/`@ai-sdk/react`, bundled).
- **Second React root / `mount()`**: replaced by `<ShadowPortal>` (one tree,
  context flows, style isolation kept).
- **Silently dropped socket `error` event**: now forwarded to `onError`.
