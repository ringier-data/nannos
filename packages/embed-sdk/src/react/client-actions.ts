import type { AppliedChange } from '../core/change-store';

/**
 * The host-DOM hooks the SDK can't run without a host, in their generic form:
 * SPA navigation and "point at this field". Both are the same in every React
 * host, so they live here and a host only overrides them when it does something
 * unusual. Absorbed from cockpit-frontend src/nannos/host/hostAdapter.ts, with
 * the field lookup made design-system-agnostic (no MUI class names).
 *
 * These are `<NannosProvider>` props, NOT adapter fields: the provider binds
 * them for as long as the app is mounted and dispatches each directive once.
 */

export interface ClientActionHandlerOptions {
  /** CSS colour of the transient outline drawn around a highlighted field. */
  highlightColor?: string;
  /**
   * Host navigation for same-origin paths. STRONGLY recommended: the fallback
   * writes history directly, which bypasses a router's own bookkeeping —
   * react-router's data router stores an index in `history.state` and silently
   * disables `useBlocker` (unsaved-changes guards) once it is missing.
   */
  navigate?: (to: string) => void | string;
  /** Label text for fields with no `[name]` in the DOM — see `resolveHighlightLabel`. */
  resolveFieldLabel?: (type: string | undefined, field: string) => string | undefined;
  /** The assistant's colours for changed-field marks: the ring and the note, and the
   *  second stop of the one-time shimmer. Use the host's accent where it has one. */
  markColors?: { primary?: string; secondary?: string };
  /** Words of the note under a changed field (defaults are English). */
  markStrings?: Partial<ChangeMarkStrings>;
}

export interface ChangeMarkStrings {
  /** "Set by Nannos" */
  setBy: string;
  /** "was" — followed by the old value */
  was: string;
  /** Shown for an old value that was empty */
  empty: string;
  undo: string;
  /** Shown when the form refused the old value */
  undoFailed: string;
}

const DEFAULT_MARK_STRINGS: ChangeMarkStrings = {
  setBy: 'Set by Nannos',
  was: 'was',
  empty: 'empty',
  undo: 'Undo',
  undoFailed: 'Could not undo',
};

export interface ClientActionHandlers {
  navigate: (to: string) => void | string;
  highlight: (target: { type: string; id: string }, field?: string) => void;
  /** Read what the user sees in the fields an `apply` is about to write. */
  beforeApply: (target: { type: string; id: string }, fields: string[]) => Record<string, string>;
  /** Mark the fields an `apply` wrote (nothing is saved yet): a one-time shimmer, a
   *  resting ring, and a note "Set by Nannos · was … · Undo" under each, until the
   *  user edits it, undoes it, or the form is saved. Scrolls the first into view. */
  markChanged: (target: { type: string; id: string }, changes: AppliedChange[], captured: unknown) => void;
  /** Drop a target's marks — after its `submit` saved it. */
  clearChanged: (target: { type: string; id: string }) => void;
}

/** Marks the assistant's own DOM (the docked panel, and whatever it portals). */
export const ASSISTANT_ATTRIBUTE = 'data-nannos-assistant';

/**
 * Whether an event target belongs to the assistant. For a host's modal: a click
 * or focus moving into the assistant is not "outside" — the user is asking for
 * help with the open form, which must stay open. With Radix:
 * `onInteractOutside={(e) => isAssistantElement(e.target) && e.preventDefault()}`.
 */
export function isAssistantElement(target: EventTarget | null | undefined): boolean {
  return target instanceof Element && target.closest(`[${ASSISTANT_ATTRIBUTE}]`) !== null;
}

const DEFAULT_HIGHLIGHT_COLOR = 'rgba(99, 102, 241, 0.6)';
const HIGHLIGHT_MS = 2000;

/**
 * Reduce a navigate target to a same-origin `path?search#hash`, or `null` when it
 * points anywhere else. Resolved against the current document, so relative paths,
 * absolute same-origin URLs and protocol-relative `//host/...` all get one answer
 * — the last would otherwise reach `history.pushState`, which throws on a
 * cross-origin URL.
 */
export function resolveSameOriginPath(to: string): string | null {
  let url: URL;
  try {
    url = new URL(to, window.location.href);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

/**
 * Router-free same-origin navigation: the fallback for a host that supplied no
 * `navigate`. Preserves `history.state` so a router's own index survives the
 * push — but a synthetic popstate does not run route-level blockers.
 */
export function historyNavigate(to: string): void {
  window.history.pushState(window.history.state, '', to);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

/**
 * Resolve an agent field reference to the DOM, most explicit first:
 * 1. `[data-nannos-field="…"]` — the host marks a control (or its wrapper) for
 *    the agent; the only option for composite widgets with no input of their own;
 * 2. `[name="…"]` — every input a form library registers;
 * 3. the label text the type's registry entry declares — the `<label for>`
 *    target when it has one, else the label's own container.
 * Scoped to `[data-nannos-object="Type:id"]` when the host marks the form, so two
 * open forms with the same field names can't cross-match; otherwise the topmost
 * open dialog is searched first, then the whole document.
 */
export function findFieldElement(
  target: { type?: string; id?: string } | undefined,
  field: string | undefined,
  resolveFieldLabel?: ClientActionHandlerOptions['resolveFieldLabel']
): HTMLElement | null {
  if (!field) return null;
  const scope =
    target?.type && target.id !== undefined
      ? byAttribute(document, 'data-nannos-object', `${target.type}:${target.id}`)
      : null;
  // Without a marked container, an open dialog is where the user is working:
  // it sits on top of the page, and a portalled dialog comes LAST in the
  // document, so a page label of the same text would otherwise win.
  const dialog = scope ? null : topmostDialog();
  const roots: ParentNode[] = scope ? [scope] : dialog ? [dialog, document] : [document];
  const label = resolveFieldLabel?.(target?.type, field);
  for (const root of roots) {
    const found = findIn(root, field, label);
    if (found) return found;
  }
  return null;
}

function findIn(root: ParentNode, field: string, label: string | undefined): HTMLElement | null {
  const marked = byAttribute(root, 'data-nannos-field', field);
  if (marked) return marked;
  const byName = byAttribute(root, 'name', field);
  if (byName) return byName;
  if (!label) return null;
  const needle = label.toLowerCase();
  const labelEl = [...root.querySelectorAll('label')].find((l) =>
    l.textContent?.toLowerCase().includes(needle)
  );
  if (!labelEl) return null;
  const forId = labelEl.getAttribute('for');
  const control = forId ? document.getElementById(forId) : null;
  return control ?? labelEl.parentElement;
}

function topmostDialog(): HTMLElement | null {
  const dialogs = document.querySelectorAll<HTMLElement>('[role="dialog"], [role="alertdialog"], dialog[open]');
  return dialogs.length ? dialogs[dialogs.length - 1] : null;
}

/**
 * First element whose attribute equals `value`. Compared, never interpolated: the
 * value comes from the agent, so it must not reach a selector parser.
 */
function byAttribute(root: ParentNode, attribute: string, value: string): HTMLElement | null {
  for (const el of root.querySelectorAll<HTMLElement>(`[${attribute}]`)) {
    if (el.getAttribute(attribute) === value) return el;
  }
  return null;
}

/** Original inline box-shadow per element, captured on the FIRST highlight only —
 *  otherwise a second highlight within the fade window snapshots the accent
 *  outline and "restores" it forever. */
const originalShadows = new WeakMap<HTMLElement, string>();
const pendingRestores = new WeakMap<HTMLElement, number>();

/** Attribute on a field the assistant changed and the user has not touched since. */
export const CHANGED_ATTRIBUTE = 'data-nannos-changed';

/**
 * The generic client-action implementations, shaped as `<NannosProvider>` props:
 * `<NannosProvider {...createClientActionHandlers({ navigate })}>`. Build them once
 * (module scope or `useMemo`) — the provider rebinds on identity.
 */
export function createClientActionHandlers({
  highlightColor = DEFAULT_HIGHLIGHT_COLOR,
  navigate,
  resolveFieldLabel,
  markColors,
  markStrings,
}: ClientActionHandlerOptions = {}): ClientActionHandlers {
  const words: ChangeMarkStrings = { ...DEFAULT_MARK_STRINGS, ...markStrings };
  const colors = { primary: markColors?.primary ?? '#6d4aff', secondary: markColors?.secondary ?? '#2f7bff' };
  // target key -> the elements marked for it, each with the cleanup that unmarks it.
  const marks = new Map<string, Map<HTMLElement, () => void>>();
  const keyOf = (target: { type: string; id: string }) => `${target.type}:${target.id}`;

  const unmark = (key: string, el: HTMLElement) => {
    marks.get(key)?.get(el)?.();
    marks.get(key)?.delete(el);
  };

  // The fill is measured, so it is stale once the theme changes under a mark: a field
  // marked in light mode kept a white fill in dark mode, hiding its light text.
  // `style` is watched for a host that sets `color-scheme` inline, but only a change of
  // the computed scheme repaints: the dock's resize writes a width variable on <html>
  // per pointermove. The watch ends with the last mark.
  let stopWatchingTheme: (() => void) | null = null;
  const watchTheme = () => {
    if (stopWatchingTheme || typeof window === 'undefined') return;
    const root = document.documentElement;
    const scheme = () => getComputedStyle(root).colorScheme;
    let lastScheme = scheme();
    const repaint = () => {
      if (![...marks.values()].some((els) => els.size)) {
        stopWatchingTheme?.();
        return;
      }
      for (const els of marks.values()) for (const el of els.keys()) paintFill(el);
    };
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');
    media?.addEventListener?.('change', repaint);
    const observer = new MutationObserver((mutations) => {
      const themed = mutations.some((m) => m.attributeName !== 'style');
      const current = scheme();
      if (!themed && current === lastScheme) return;
      lastScheme = current;
      repaint();
    });
    observer.observe(root, { attributes: true, attributeFilter: ['class', 'style', 'data-theme'] });
    stopWatchingTheme = () => {
      observer.disconnect();
      media?.removeEventListener?.('change', repaint);
      stopWatchingTheme = null;
    };
  };

  const mark = (key: string, el: HTMLElement, change: AppliedChange, wasText: string | undefined) => {
    if (marks.get(key)?.has(el)) return;
    ensureMarkStyles(colors);
    watchTheme();
    paintFill(el);
    el.setAttribute(CHANGED_ATTRIBUTE, 'fresh');
    // The shimmer plays once; the ring stays.
    const settle = window.setTimeout(() => {
      if (el.hasAttribute(CHANGED_ATTRIBUTE)) el.setAttribute(CHANGED_ATTRIBUTE, '');
    }, 1200);

    const note = buildNote(change, wasText ?? textOf(change.previous), words);
    // A sibling, never a child: the host's framework owns the control's subtree.
    el.insertAdjacentElement('afterend', note);

    // The user taking the field over ends the mark: typing, picking, or toggling it.
    const onEdit = () => change.dismiss();
    el.addEventListener('input', onEdit);
    el.addEventListener('change', onEdit);
    const cleanup = () => {
      window.clearTimeout(settle);
      el.removeEventListener('input', onEdit);
      el.removeEventListener('change', onEdit);
      el.removeAttribute(CHANGED_ATTRIBUTE);
      el.style.removeProperty('--nannos-fill');
      el.style.removeProperty('--nannos-rest-size');
      el.style.removeProperty('--nannos-rest-pos');
      note.remove();
    };
    if (!marks.has(key)) marks.set(key, new Map());
    marks.get(key)!.set(el, cleanup);
    // Undone, dismissed, saved or unmounted: the store says so once, here.
    change.onDismiss(() => unmark(key, el));
  };

  return {
    beforeApply: (target, fields) => {
      const seen: Record<string, string> = {};
      for (const field of fields) {
        const el = findFieldElement(target, field, resolveFieldLabel);
        if (el) seen[field] = displayText(controlOf(el));
      }
      return seen;
    },
    markChanged: (target, changes, captured) => {
      const key = keyOf(target);
      // Elements that left the DOM (the form re-rendered or closed) take their marks along.
      for (const el of [...(marks.get(key)?.keys() ?? [])]) if (!el.isConnected) unmark(key, el);
      const seen = (captured ?? {}) as Record<string, string | undefined>;
      let first: HTMLElement | null = null;
      for (const change of changes) {
        const found = findFieldElement(target, change.field, resolveFieldLabel);
        if (!found) continue;
        // A labelled wrapper is marked on its control: the label is not the field.
        const el = controlOf(found);
        mark(key, el, change, seen[change.field]);
        first ??= el;
      }
      first?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    },
    clearChanged: (target) => {
      const key = keyOf(target);
      for (const el of [...(marks.get(key)?.keys() ?? [])]) unmark(key, el);
      marks.delete(key);
    },
    navigate: (to: string) => {
      const target = resolveSameOriginPath(to);
      if (target === null) {
        // The agent reads page content (customer-supplied text), so `to` is
        // untrusted: a prompt-injected off-origin URL must not move the
        // authenticated tab. Dropped, not followed; the client-action log
        // still records the directive.
        // eslint-disable-next-line no-console
        console.warn(`[nannos] navigate: refused off-origin target ${JSON.stringify(to)}`);
        return 'Not a page of this application: only paths on this site can be opened.';
      }
      return (navigate ?? historyNavigate)(target);
    },
    highlight: (target, field) => {
      const el = findFieldElement(target, field, resolveFieldLabel);
      if (!el) return;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (!originalShadows.has(el)) originalShadows.set(el, el.style.boxShadow);
      window.clearTimeout(pendingRestores.get(el));
      // Transient outline that doesn't disturb layout.
      el.style.transition = 'box-shadow 0.2s ease';
      el.style.boxShadow = `0 0 0 3px ${highlightColor}`;
      pendingRestores.set(
        el,
        window.setTimeout(() => {
          el.style.boxShadow = originalShadows.get(el) ?? '';
          originalShadows.delete(el);
          pendingRestores.delete(el);
        }, HIGHLIGHT_MS)
      );
    },
  };
}

const STYLE_ID = 'nannos-change-marks';
const SPARK_SVG =
  '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path fill="currentColor" d="M8 0c.4 3.6 1.9 5.6 6 6.2v.6C9.9 7.4 8.4 9.4 8 13h-.1C7.5 9.4 6 7.4 2 6.8v-.6C6 5.6 7.5 3.6 7.9 0z"/></svg>';

/** One stylesheet per document for every mark: injected on first use. */
function ensureMarkStyles(colors: { primary: string; secondary: string }): void {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
:root{--nannos-mark-a:${colors.primary};--nannos-mark-b:${colors.secondary}}
[${CHANGED_ATTRIBUTE}]{border-color:transparent!important;background:var(--nannos-fill),linear-gradient(120deg,var(--nannos-mark-a),var(--nannos-mark-b)) border-box!important}
[${CHANGED_ATTRIBUTE}="fresh"]{background:linear-gradient(100deg,transparent 15%,color-mix(in srgb,var(--nannos-mark-a) 22%,transparent) 42%,color-mix(in srgb,var(--nannos-mark-b) 22%,transparent) 58%,transparent 85%) padding-box,var(--nannos-fill),linear-gradient(120deg,var(--nannos-mark-a),var(--nannos-mark-b)) border-box!important;background-size:260% 100%,var(--nannos-rest-size)!important;background-repeat:no-repeat!important;animation:nannos-sweep 1.1s ease-out 1 both}
@keyframes nannos-sweep{from{background-position:115% 0,var(--nannos-rest-pos)}to{background-position:-15% 0,var(--nannos-rest-pos)}}
[data-nannos-change-note]{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:4px;font-size:12px;line-height:1.4;color:color-mix(in srgb,currentColor 68%,transparent)}
[data-nannos-change-note] .nannos-by{display:inline-flex;align-items:center;gap:3px;font-weight:500;color:var(--nannos-mark-a)}
[data-nannos-change-note] button{all:unset;cursor:pointer;color:var(--nannos-mark-a);text-decoration:underline;text-underline-offset:2px}
[data-nannos-change-note] button:focus-visible{outline:2px solid var(--nannos-mark-a);outline-offset:2px;border-radius:2px}
[data-nannos-change-note] button[disabled]{cursor:default;opacity:.6}
@media (prefers-reduced-motion:reduce){[${CHANGED_ATTRIBUTE}="fresh"]{animation:none;background:var(--nannos-fill),linear-gradient(120deg,var(--nannos-mark-a),var(--nannos-mark-b)) border-box!important}}`;
  document.head.appendChild(style);
}

function buildNote(change: AppliedChange, wasText: string, words: ChangeMarkStrings): HTMLElement {
  const note = document.createElement('div');
  note.setAttribute('data-nannos-change-note', '');
  note.setAttribute('data-nannos-ignore', ''); // not part of the page the agent reads
  const by = document.createElement('span');
  by.className = 'nannos-by';
  by.innerHTML = SPARK_SVG;
  by.append(words.setBy);
  const was = document.createElement('span');
  was.append(`${words.was} `);
  const old = document.createElement(wasText ? 's' : 'em');
  old.textContent = wasText || words.empty;
  was.append(old);
  const undo = document.createElement('button');
  undo.type = 'button';
  undo.textContent = words.undo;
  undo.addEventListener('click', async () => {
    undo.disabled = true;
    // Success removes the note (the change is dismissed); a refusal says so in place.
    if (!(await change.undo())) undo.textContent = words.undoFailed;
  });
  note.append(by, '·', was, '·', undo);
  return note;
}

/** What the user sees in a control: its value, a select's chosen label, or its text. */
function displayText(el: HTMLElement): string {
  let text: string;
  if (el instanceof HTMLSelectElement) text = el.selectedOptions[0]?.textContent ?? el.value;
  else if (el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio')) {
    text = el.checked ? 'on' : 'off';
  } else if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
    text = el.value;
  } else {
    if (el.getAttribute('role') === 'switch') return el.getAttribute('aria-checked') === 'true' ? 'on' : 'off';
    // A composite control (a select trigger, a toggle group): its first line of text.
    text = (el.innerText || el.textContent || '').split('\n').map((t) => t.trim()).find(Boolean) ?? '';
  }
  return clip(text.trim());
}

function textOf(value: unknown): string {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'string') return clip(value);
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  if (Array.isArray(value)) return clip(value.join(', '));
  return clip(JSON.stringify(value));
}

function clip(text: string, max = 60): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

const CONTROL_SELECTOR = 'input:not([type="hidden"]), textarea, select, [role="combobox"], [role="switch"], [role="checkbox"]';

/** The field itself: a found element, or the ONE control inside a labelled wrapper.
 *  A wrapper holding several controls (a tool picker) stays the field. */
function controlOf(el: HTMLElement): HTMLElement {
  if (el.matches(CONTROL_SELECTOR)) return el;
  const controls = el.querySelectorAll<HTMLElement>(CONTROL_SELECTOR);
  return controls.length === 1 ? controls[0] : el;
}

/** Paint the field's fill under its gradient edge. A marked field's own background is
 *  overridden by the mark, so it is measured with the mark lifted for the reading (in
 *  the same task, so nothing paints in between). */
function paintFill(el: HTMLElement): void {
  const state = el.getAttribute(CHANGED_ATTRIBUTE);
  if (state !== null) el.removeAttribute(CHANGED_ATTRIBUTE);
  // The gradient edge is drawn in the field's own border, over its own fill.
  const layers = fillLayers(el);
  if (state !== null) el.setAttribute(CHANGED_ATTRIBUTE, state);
  el.style.setProperty('--nannos-fill', layers.join(','));
  // Size/position lists for every layer under the sweep (fill layers + the edge):
  // CSS repeats a short list cyclically, which would offset an opaque fill layer.
  el.style.setProperty('--nannos-rest-size', Array(layers.length + 1).fill('auto').join(','));
  el.style.setProperty('--nannos-rest-pos', Array(layers.length + 1).fill('0 0').join(','));
}

/** What the field shows behind its content, as background layers: its own colour and
 *  every translucent one up the tree, down to the first opaque one. The gradient edge
 *  is painted under these, so it must only show in the border — a single translucent
 *  fill (a tinted edit-mode card) let the gradient bleed through the whole field. */
function fillLayers(el: HTMLElement): string[] {
  const colors: string[] = [];
  for (let node: HTMLElement | null = el; node; node = node.parentElement) {
    const color = getComputedStyle(node).backgroundColor;
    const alpha = alphaOf(color);
    if (alpha <= 0) continue;
    colors.push(color);
    if (alpha >= 1) break;
  }
  if (!colors.length || alphaOf(colors[colors.length - 1]) < 1) colors.push('Canvas');
  return colors.map((c) => `linear-gradient(${c},${c}) padding-box`);
}

/** Alpha of a computed colour: `rgba(r, g, b, a)`, or the modern `fn(… / a)` syntax
 *  computed values use for oklch/lab/color() (Tailwind v4's palette). */
function alphaOf(color: string | null | undefined): number {
  if (!color || color === 'transparent') return 0;
  const slash = /\/\s*([\d.]+)(%?)\s*\)$/.exec(color);
  if (slash) return slash[2] ? Number(slash[1]) / 100 : Number(slash[1]);
  const rgba = /^rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\s*\)$/.exec(color);
  if (rgba) return Number(rgba[1]);
  return 1;
}
