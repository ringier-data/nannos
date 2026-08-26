/**
 * `<NannosProvider>` v2 — the host-authority layer, ported from the
 * battle-tested Gatana assistant provider (assistant-context.tsx):
 *
 * - owns ONE core (connection lifecycle, client-action binding, errors);
 * - owns the PANEL STATE the old SDK kept as pub/sub on the core: open/close,
 *   pinned/docked, width (published as a CSS variable so layouts yield space
 *   without re-rendering), all persisted;
 * - seeds prompts as DRAFTS by default (the composer shows the question before
 *   it is asked; `sendOnOpen: true` is the explicit "just do it" path);
 * - `open()` is gesture-safe: when the PKCE strategy needs a login it runs
 *   `core.login()` synchronously inside the caller's click so the popup is
 *   never blocked.
 *
 * Everything here is host-facing state; the chat engine lives in
 * `../transport` and the UI in `../panel`.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  createNannos,
  mergePageContexts,
  sanitizePageContext,
  snapshotScreenOutline,
  type ApplyResult,
  type NannosAuth,
  type NannosConfig,
  type NannosCore,
  type NannosErrorEvent,
  type NannosPageContext,
  type NannosPageReader,
  type NannosStatus,
} from '../core';
import type { NannosHostAdapter } from './adapter';
import { NannosStringsProvider } from './i18n';
import type { NannosStrings } from '../i18n/keys';

/**
 * Width the pinned panel occupies, published on the ROOT element: the panel is
 * host-positioned and the layout that yields the space may share no ancestor
 * short of the root; and during a drag the width changes per frame, which as
 * React state would re-render the whole app. The drag writes the variable
 * directly; state catches up on release. `0px` while the panel is closed or
 * unpinned, so `margin-right: var(--nannos-panel-width, 0px)` is always safe.
 */
export const NANNOS_PANEL_WIDTH_VAR = '--nannos-panel-width';

const MIN_PANEL_WIDTH = 420;
const DEFAULT_PANEL_WIDTH = MIN_PANEL_WIDTH;

/**
 * Keep the panel wide enough for its composer and narrow enough to leave the
 * page usable beside it. Applied to every width on its way in — dragged, set,
 * or read back from storage — so no path can wedge the panel somewhere it
 * cannot be dragged back from.
 */
export function clampPanelWidth(
  width: number,
  opts?: { min?: number; max?: number | ((viewportWidth: number) => number) },
): number {
  const min = opts?.min ?? MIN_PANEL_WIDTH;
  const viewport = typeof window !== 'undefined' ? window.innerWidth : 1440;
  const rawMax = typeof opts?.max === 'function' ? opts.max(viewport) : opts?.max;
  const widest = Math.max(min, rawMax ?? Math.min(960, viewport - 360));
  return Math.round(Math.max(min, Math.min(width, widest)));
}

export interface OpenOptions {
  /**
   * Send the prompt as soon as the chat is ready, instead of drafting it into
   * the composer. Default FALSE: an "Ask AI" affordance proposes a question
   * and the user should see what will be asked before it is. Pass true only
   * when the user already took the decision (a menu entry that says "do it").
   */
  sendOnOpen?: boolean;
  /** Short label rendered as a muted context chip instead of the raw prompt
   *  (for host-authored prompts the user didn't type). */
  displayText?: string;
  /** Page-context key (e.g. `campaign:123`): the chat continues the active
   *  conversation only when it was started under the same key. */
  contextKey?: string;
}

export interface SeededPrompt {
  text: string;
  displayText?: string;
  contextKey?: string;
  sendOnOpen: boolean;
}

/** Handle to a registered page-context layer (see `registerPageContextLayer`). */
export interface PageContextLayerHandle {
  /** Replace this layer's contribution; it keeps its position in the stack. */
  update: (context: NannosPageContext) => void;
  /** Remove the layer (a page/tab/dialog unmounting). */
  dispose: () => void;
}

export interface AssistantValue {
  /** The assistant can be offered: provider present, enabled, core created. */
  isAvailable: boolean;
  /** Connection status (`unauthenticated` is distinct from a network drop). */
  status: NannosStatus;
  isOpen: boolean;
  /**
   * Open the panel, optionally seeding a prompt (drafted by default — see
   * `OpenOptions.sendOnOpen`). Gesture-safe: call it from a real user event;
   * when the self-login strategy isn't authenticated it runs `core.login()`
   * synchronously inside that gesture (popup-legal) and opens on success.
   */
  open: (prompt?: string, opts?: OpenOptions) => void;
  close: () => void;
  toggle: () => void;
  /** Pinned = docked beside the page (layout yields width); unpinned = overlay. */
  isPinned: boolean;
  /** The host allows switching pinned/overlay. When false the panel hides its
   *  pin toggle and `togglePinned` is a no-op (mode is locked to `defaultPinned`). */
  canChangePinMode: boolean;
  togglePinned: () => void;
  /** Width in pixels of the pinned panel; the layout gives up the same width. */
  panelWidth: number;
  /** Set the pinned panel's width (clamped). Drags write the CSS variable per
   *  frame and call this once on release. */
  setPanelWidth: (width: number) => void;
  /** The prompt an "Ask AI" trigger seeded, consumed by the panel's composer. */
  seededPrompt: SeededPrompt | null;
  clearSeededPrompt: () => void;
  /**
   * The page/context the user is currently on: the registered layers MERGED
   * (later layer wins, `view` merges key by key) and SANITIZED (field caps,
   * secret-key deny list, whole-payload ceiling — see core/page-context.ts).
   * The composer shows it, every send carries it to the agent, and `open()`
   * uses its `key` as the default conversation-scoping `contextKey` for seeded
   * prompts. Null until some layer provides a `key`.
   */
  pageContext: NannosPageContext | null;
  /**
   * Publish (or clear, with null) the BASE layer — what a router bridge
   * publishes on navigation (`{key: pathname, title}`); pages/tabs/dialogs
   * layer on top via `useNannosPageContext`. Identity-stable; value-equal
   * results are dropped, so calling it from a render-coupled effect on every
   * route change is safe.
   */
  setPageContext: (context: NannosPageContext | null) => void;
  /**
   * Register a page-context LAYER above the base — what `useNannosPageContext`
   * uses. Mount order is layer order: a tab or dialog registered after the
   * page it sits in wins field-by-field over it.
   */
  registerPageContextLayer: (context: NannosPageContext) => PageContextLayerHandle;
  /**
   * Register an answer source for the agent's `read_current_page` pull — what
   * `useNannosPageReader` uses. `key` names the answer field; the reader
   * returns whatever the page holds (rows, filters, unsaved values — sync or
   * async). Sanitized (deny list at every depth + caps) before it leaves the
   * browser. `screen` is reserved: the built-in screen outline answers under
   * it (see the `screenOutline` prop). Returns the unregister function.
   */
  registerPageReader: (key: string, reader: NannosPageReader) => () => void;
  /** The shared core (registry, transport, login/logout). Null when disabled. */
  core: NannosCore | null;
  /** Host adapter, for the panel chunk. */
  adapter?: NannosHostAdapter;
}

const noop = () => {};

/**
 * Returned outside the provider (module constant, so effects depending on it
 * don't re-run): a page with an "Ask AI" link still renders when the assistant
 * isn't mounted — and host tests need no provider wrapping.
 */
const UNAVAILABLE: AssistantValue = {
  isAvailable: false,
  status: 'disconnected',
  isOpen: false,
  open: noop,
  close: noop,
  toggle: noop,
  isPinned: false,
  canChangePinMode: false,
  togglePinned: noop,
  panelWidth: DEFAULT_PANEL_WIDTH,
  setPanelWidth: noop,
  seededPrompt: null,
  clearSeededPrompt: noop,
  pageContext: null,
  setPageContext: noop,
  registerPageContextLayer: () => ({ update: noop, dispose: noop }),
  registerPageReader: () => noop,
  core: null,
};

const AssistantContext = createContext<AssistantValue | null>(null);

/** Storage access that degrades to defaults in browsers that refuse it. */
function readStorage(store: 'local' | 'session', key: string): string | null {
  try {
    return (store === 'local' ? localStorage : sessionStorage).getItem(key);
  } catch {
    return null;
  }
}
function writeStorage(store: 'local' | 'session', key: string, value: string): void {
  try {
    (store === 'local' ? localStorage : sessionStorage).setItem(key, value);
  } catch {
    // A browser that won't remember only costs the user their choice next visit.
  }
}

export interface NannosProviderProps {
  children: ReactNode;
  /** Connection config, read ONCE when first enabled (remount with a `key` to change). */
  config?: NannosConfig;
  /** …or bring your own already-created core instead of `config`. */
  core?: NannosCore;
  /** Self-login strategy (`pkce({...})`); merged into the config's `auth`. */
  auth?: NannosAuth;
  /** Reactive availability gate (feature flag). While false: null core, no
   *  fetch, no popup, hooks no-op. */
  enabled?: boolean;

  /** Client actions — provider-only in v2 (the adapter carries none). */
  navigate?: (to: string) => void;
  highlight?: (target: { type: string; id: string }, field?: string) => void;
  /** Called after an `apply` that rejected at least one field — the only place
   *  a rejection surfaces (the agent gets no ack). */
  onApplyResult?: (target: { type: string; id: string }, result: ApplyResult) => void;
  /** Forward SDK-internal failures (connection/init/auth/apply) to host monitoring. */
  onError?: (e: NannosErrorEvent) => void;
  /** Include the rendered page as a markdown outline (a visibility-respecting
   *  DOM walk — core/screen-outline.ts) in every `read_current_page` answer,
   *  under the reserved `screen` key. Default true: it is what makes the read
   *  useful on pages that registered no reader. Mark elements the walk must
   *  skip with `data-nannos-ignore`, secret-bearing ones with
   *  `data-nannos-redact`; set false to send only page context + readers. */
  screenOutline?: boolean;

  /** Host adapter: REST overrides, links, notify, chatSurface (see adapter.tsx). */
  adapter?: NannosHostAdapter;

  /** Panel behavior. Pinned unless the user has said otherwise. */
  defaultPinned?: boolean;
  /** Let the user switch between pinned (docked) and unpinned (overlay).
   *  Default true. Set false for hosts where one mode makes no sense (e.g. a
   *  layout that always stretches its content and must always yield space):
   *  the mode is locked to `defaultPinned`, the panel hides its pin toggle,
   *  and the user's stored preference is kept for if the host re-enables it. */
  canChangePinMode?: boolean;
  minPanelWidth?: number;
  maxPanelWidth?: number | ((viewportWidth: number) => number);
  /** Storage-key prefix (default 'nannos'): `{prefix}:pinned`, `{prefix}:panel-width`
   *  (localStorage — ways of working), `{prefix}:open` (sessionStorage — a pinned
   *  panel survives a reload of ITS tab, but never opens itself in a new one). */
  storagePrefix?: string;
  /** Keyboard toggle (Cmd/Ctrl+J), availability-gated. `false` disables. */
  shortcut?: 'mod+j' | false;

  /** Force the panel's language (e.g. the host's own switcher). Default: the
   *  browser's preference list, resolved against the built-in tables
   *  (en, de, fr, it) with English as the fallback. */
  locale?: string | readonly string[];
  /** i18n overrides, merged over the resolved locale's table. Reactive prop. */
  strings?: Partial<NannosStrings>;
}

export function NannosProvider(props: NannosProviderProps): ReactNode {
  const {
    children,
    config,
    core,
    auth,
    enabled = true,
    navigate,
    highlight,
    onApplyResult,
    onError,
    screenOutline = true,
    adapter,
    defaultPinned = true,
    canChangePinMode = true,
    minPanelWidth = MIN_PANEL_WIDTH,
    maxPanelWidth,
    storagePrefix = 'nannos',
    shortcut = 'mod+j',
    locale,
    strings,
  } = props;

  const pinnedKey = `${storagePrefix}:pinned`;
  const widthKey = `${storagePrefix}:panel-width`;
  const openKey = `${storagePrefix}:open`;
  const clampOpts = useMemo(
    () => ({ min: minPanelWidth, max: maxPanelWidth }),
    [minPanelWidth, maxPanelWidth],
  );

  // The core is created ONCE, the first time we're enabled — config/auth are
  // captured at that moment (change them by remounting with a `key`). `enabled`
  // IS reactive: it gates exposure, so a runtime flag can flip the assistant.
  const coreRef = useRef<NannosCore | null>(null);
  if (enabled && !coreRef.current) {
    coreRef.current = core ?? (config ? createNannos(auth ? { ...config, auth } : config) : null);
  }
  const resolved = enabled ? coreRef.current : null;
  const resolvedRef = useRef(resolved);
  resolvedRef.current = resolved;

  // Pinned unless the user said otherwise: only an explicit '0' unpins (or the
  // host default), so a first visit and a forgetful browser both start pinned.
  const [pinnedPreference, setPinned] = useState(() => {
    const stored = readStorage('local', pinnedKey);
    if (stored === '0') return false;
    if (stored === '1') return true;
    return defaultPinned;
  });
  // A host that locks the pin mode gets its default; the stored preference
  // stays untouched underneath, so re-enabling the toggle restores the choice.
  const isPinned = canChangePinMode ? pinnedPreference : defaultPinned;
  // Reopen only what a PINNED panel left on screen in THIS tab: pinned, the
  // panel is part of the page and a reload must bring it back; unpinned it is
  // an overlay the user dismissed by leaving.
  const [isOpen, setOpen] = useState(() => isPinned && readStorage('session', openKey) === '1');
  const [panelWidth, setWidthState] = useState(() =>
    clampPanelWidth(Number(readStorage('local', widthKey)) || DEFAULT_PANEL_WIDTH, clampOpts),
  );
  const [seededPrompt, setSeededPrompt] = useState<SeededPrompt | null>(null);
  const [status, setStatus] = useState<NannosStatus>(resolved?.status ?? 'disconnected');

  // The live page context: a base layer (the router bridge) + layers pages
  // register on top, folded and sanitized into ONE snapshot. State drives the
  // composer chip; the ref mirror lets identity-stable callbacks (open, and
  // the engine's send-time read) see the current value without re-binding.
  const [pageContext, setPageContextState] = useState<NannosPageContext | null>(null);
  const pageContextRef = useRef<NannosPageContext | null>(null);
  const basePageLayerRef = useRef<NannosPageContext | null>(null);
  // A Map keeps insertion order, and updating an existing key keeps its
  // position — exactly the layer semantics (a re-rendering tab stays where it
  // mounted, below any dialog that came after it).
  const pageLayersRef = useRef(new Map<number, NannosPageContext>());
  const pageLayerSeqRef = useRef(0);

  const recomputePageContext = useCallback(() => {
    const layers = [
      ...(basePageLayerRef.current ? [basePageLayerRef.current] : []),
      ...pageLayersRef.current.values(),
    ];
    const next = layers.length ? sanitizePageContext(mergePageContexts(layers)) : null;
    // Layers republish from render-coupled effects (router listeners, page
    // re-renders) with unchanged values — drop those instead of re-rendering.
    // The payload is capped small (sanitize), so JSON equality is fine.
    const previous = pageContextRef.current;
    if (next === previous) return;
    if (next && previous && JSON.stringify(next) === JSON.stringify(previous)) return;
    pageContextRef.current = next;
    setPageContextState(next);
  }, []);

  const setPageContext = useCallback(
    (context: NannosPageContext | null) => {
      basePageLayerRef.current = context;
      recomputePageContext();
    },
    [recomputePageContext],
  );

  const registerPageContextLayer = useCallback(
    (context: NannosPageContext): PageContextLayerHandle => {
      const id = pageLayerSeqRef.current++;
      pageLayersRef.current.set(id, context);
      recomputePageContext();
      return {
        update: (next: NannosPageContext) => {
          if (!pageLayersRef.current.has(id)) return; // disposed layers stay gone
          pageLayersRef.current.set(id, next);
          recomputePageContext();
        },
        dispose: () => {
          if (pageLayersRef.current.delete(id)) recomputePageContext();
        },
      };
    },
    [recomputePageContext],
  );

  // Page READERS: what the agent's `read_current_page` pull is answered from.
  // Keyed by answer name (last registration per key wins — pages re-register on
  // re-render through the hook); nothing here is reactive UI state, so a plain
  // ref suffices. Sanitization happens at execution (core/page-read.ts).
  const pageReadersRef = useRef(new Map<string, NannosPageReader>());
  const registerPageReader = useCallback((key: string, reader: NannosPageReader) => {
    pageReadersRef.current.set(key, reader);
    return () => {
      // Only the CURRENT holder of the key may remove it — a stale disposer
      // from a replaced registration must not delete its successor.
      if (pageReadersRef.current.get(key) === reader) pageReadersRef.current.delete(key);
    };
  }, []);
  const readCurrentPage = useCallback(async () => {
    const answers: Record<string, unknown> = { page: pageContextRef.current };
    for (const [key, reader] of pageReadersRef.current) {
      try {
        answers[key] = await reader();
      } catch {
        // One page's broken reader must not cost the agent the others' answers.
        answers[key] = { error: 'This reader failed.' };
      }
    }
    return answers;
  }, []);

  // --- core lifecycle ------------------------------------------------------
  useEffect(() => {
    if (!resolved) return;
    void resolved.connect();
    return () => resolved.transport.disconnect();
  }, [resolved]);

  useEffect(() => {
    if (!resolved) return;
    return resolved.onStatusChange(setStatus);
  }, [resolved]);

  useEffect(() => {
    // Always bound while a core exists (not only when the host wired hooks):
    // the binding also serves `runClientAction` — the awaited round trip — and
    // `read_current_page` is answered SDK-side (page context + readers) even on
    // a host that passes no navigate/highlight.
    if (!resolved) return;
    return resolved.bindClientActions({
      navigate,
      highlight,
      onApplyResult,
      readCurrentPage,
      screenOutline: screenOutline ? snapshotScreenOutline : undefined,
    });
  }, [resolved, navigate, highlight, onApplyResult, readCurrentPage, screenOutline]);

  useEffect(() => {
    if (!resolved || !onError) return;
    return resolved.onError(onError);
  }, [resolved, onError]);

  // --- persistence + the layout variable -----------------------------------
  useEffect(() => {
    // A locked mode is the host's, not the user's — never persist it over
    // the user's own choice.
    if (canChangePinMode) writeStorage('local', pinnedKey, isPinned ? '1' : '0');
    writeStorage('local', widthKey, String(panelWidth));
  }, [pinnedKey, widthKey, canChangePinMode, isPinned, panelWidth]);

  // Written on every change, not on close: a reload takes the tab without
  // warning, and what is in storage at that moment is all the next load has.
  useEffect(() => {
    writeStorage('session', openKey, isOpen ? '1' : '0');
  }, [openKey, isOpen]);

  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty(NANNOS_PANEL_WIDTH_VAR, isOpen && isPinned ? `${panelWidth}px` : '0px');
    return () => {
      root.style.removeProperty(NANNOS_PANEL_WIDTH_VAR);
    };
  }, [isOpen, isPinned, panelWidth]);

  // --- actions --------------------------------------------------------------
  const open = useCallback(
    (prompt?: string, opts?: OpenOptions) => {
      if (prompt !== undefined) {
        setSeededPrompt({
          text: prompt,
          displayText: opts?.displayText,
          // A seeded prompt is about the page it was asked from: when the host
          // publishes a live page context, its key scopes the conversation
          // unless the caller pins a different one.
          contextKey: opts?.contextKey ?? pageContextRef.current?.key,
          sendOnOpen: opts?.sendOnOpen === true,
        });
      }
      const current = resolvedRef.current;
      if (current?.needsLogin()) {
        // Called synchronously inside the caller's gesture so window.open in
        // login() is popup-legal; open only once the token is really there.
        current
          .login()
          .then(() => setOpen(true))
          .catch(() => {
            /* status flips to authError; host chrome renders it */
          });
        return;
      }
      setOpen(true);
    },
    [], // resolvedRef keeps this identity-stable
  );

  const close = useCallback(() => {
    setOpen(false);
    // A question the user did not act on is dropped, rather than waiting for
    // them the next time they open the panel about something else.
    setSeededPrompt(null);
  }, []);

  const toggle = useCallback(() => setOpen((current) => !current), []);
  const togglePinned = useCallback(() => {
    if (canChangePinMode) setPinned((current) => !current);
  }, [canChangePinMode]);
  const clearSeededPrompt = useCallback(() => setSeededPrompt(null), []);
  const setPanelWidth = useCallback(
    (width: number) => setWidthState(clampPanelWidth(width, clampOpts)),
    [clampOpts],
  );

  // --- keyboard shortcut ----------------------------------------------------
  const isAvailable = resolved !== null;
  useEffect(() => {
    if (!isAvailable || shortcut === false) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === 'j' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen((current) => !current);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isAvailable, shortcut]);

  const value = useMemo<AssistantValue>(
    () => ({
      isAvailable,
      status,
      isOpen,
      open,
      close,
      toggle,
      isPinned,
      canChangePinMode,
      togglePinned,
      panelWidth,
      setPanelWidth,
      seededPrompt,
      clearSeededPrompt,
      pageContext,
      setPageContext,
      registerPageContextLayer,
      registerPageReader,
      core: resolved,
      adapter,
    }),
    [
      isAvailable,
      status,
      isOpen,
      open,
      close,
      toggle,
      isPinned,
      canChangePinMode,
      togglePinned,
      panelWidth,
      setPanelWidth,
      seededPrompt,
      clearSeededPrompt,
      pageContext,
      setPageContext,
      registerPageContextLayer,
      registerPageReader,
      resolved,
      adapter,
    ],
  );

  return (
    <AssistantContext.Provider value={value}>
      <NannosStringsProvider locale={locale} strings={strings}>
        {children}
      </NannosStringsProvider>
    </AssistantContext.Provider>
  );
}

/**
 * Reach the assistant from anywhere. Returns a no-op value outside the
 * provider, so a page with an "Ask AI" link renders when the assistant is not
 * mounted (and host tests need no provider wrapping).
 */
export function useAssistant(): AssistantValue {
  return useContext(AssistantContext) ?? UNAVAILABLE;
}

/** Coarse connection status for host chrome. `'disconnected'` when disabled/absent. */
export function useNannosStatus(): NannosStatus {
  return useAssistant().status;
}
