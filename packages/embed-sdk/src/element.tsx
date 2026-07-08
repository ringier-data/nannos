import { createRoot, type Root } from 'react-dom/client';
import { ChatWidget } from './ui/ChatWidget';
import type { NannosHostAdapter } from './ui/adapter';
import type { NannosCore } from './core';
// Tailwind v4 output, imported as a string so it can be adopted INTO the shadow
// root (never the host's global scope). See CONTEXT.md "CSS delivery".
import css from './ui/theme.css?inline';

export interface MountOptions {
  /** Render inside a Shadow DOM for CSS isolation (default true). The chat panel
   *  + HITL cards live here; inline "AI buttons" injected next to host fields do
   *  NOT (they live in the host light DOM and use minimal inline styles). */
  shadow?: boolean;
  /** Host adapter — auth facts, deep links, request headers, backend REST
   *  overrides. Omit for the zero-config defaults (same-origin REST). An
   *  embedded host (different origin than console-backend) SHOULD pass one so
   *  the feedback/settings/upload REST legs resolve against the core's
   *  `backendUrl` rather than the host's own origin. */
  adapter?: NannosHostAdapter;
}

export interface MountedWidget {
  unmount: () => void;
}

/**
 * Boot the React UI kit into `el`, isolated in a Shadow DOM. The compiled
 * Tailwind sheet is adopted into the shadow root via `adoptedStyleSheets`, so
 * host CSS can't bleed in and ours can't bleed out — strictly stronger than
 * class prefixing, and the reason we use Shadow DOM rather than an iframe
 * (an iframe couldn't reach the host's react-hook-form for in-form `apply`).
 */
export function mount(core: NannosCore, el: HTMLElement, opts: MountOptions = {}): MountedWidget {
  const useShadow = opts.shadow ?? true;
  let container: HTMLElement = el;
  let root: Root;

  if (useShadow) {
    // attachShadow is once-per-element and unmount() can't detach it — reuse an
    // existing shadow root so re-mounting on the same element (React StrictMode's
    // double-invoked effects, a host effect re-run) doesn't throw NotSupportedError.
    const shadow = el.shadowRoot ?? el.attachShadow({ mode: 'open' });
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    shadow.adoptedStyleSheets = [sheet];
    container = document.createElement('div');
    // Fill the host: the UI kit sizes itself with `h-full`/`flex-1`, which only
    // resolves if this container has a definite height. Without this the widget
    // collapses to content height, overflows a fixed-size host, and its scroll
    // area + lower controls get clipped (unreachable buttons, no scroll).
    container.style.height = '100%';
    container.style.width = '100%';
    // replaceChildren (not appendChild): drops a previous mount's container.
    shadow.replaceChildren(container);
  }

  root = createRoot(container);
  // In shadow mode, `container` lives inside the shadow root — pass it as the
  // portal container so Radix popovers/dialogs render INSIDE the shadow (styled,
  // inside the panel) instead of escaping to the host's document.body.
  root.render(<ChatWidget core={core} adapter={opts.adapter} portalContainer={useShadow ? container : undefined} />);
  return { unmount: () => root.unmount() };
}
