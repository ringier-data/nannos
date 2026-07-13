---
status: accepted
---

# Client integration is a two-layer Embed SDK, not declarative DOM attributes

The host-side integration contract is a **programmatic JS Embed SDK**, not
declarative `data-nannos-*` DOM attributes. The SDK is the single client-side
runtime serving *all* client surfaces — chat widget, HITL approval, work-plan,
feedback, and in-form `apply` — structured as a **headless core** (protocol
client, object registration, client-action execution) plus a **styleable UI
kit** on top (batteries-included, design-token + Shadow-DOM themed by default,
slot-overridable, or fully headless). Declarative attributes survive only as an
optional read-only grounding fallback.

## Why

The flagship in-form affordance ("auto-compile this form") cannot be delivered by
scraping/mutating DOM nodes: host forms are React-controlled
(`react-hook-form` + zod), so writes must go *through* the framework via a
registered `apply()` handle to fire validation, dirty-tracking, and submit. Only
a programmatic SDK can expose that handle. The two-layer split lets one package
serve both a brand-it-and-go marketing embed and a deep first-party app (console,
cockpit) that needs its HITL card to match its design system — without forking.

A unifying observation justifies routing everything through this one runtime:
the chat surface, HITL card, work-plan view, feedback prompt, and in-form `apply`
are all the **same kind of thing — styleable client-side renderers of negotiated
A2A extensions** (`human-in-the-loop`, `work-plan`, `feedback-request`,
`activity-log`, `client-action`). The agent emits a structured directive; the
client renders/handles it.

## Consequences

- The build is largely an **extraction** of console-frontend's `ChatApp` + its
  extension renderers into an embeddable, themeable package — not new UI.
- In-form/client affordances require the `client-action` A2A extension; the
  widget is a sandboxed executor of typed directives bound only to
  host-registered handles.
- Committing to the headless-core/UI-kit split is more upfront API-design work
  than shipping a single themeable widget, accepted to avoid a later fork.
