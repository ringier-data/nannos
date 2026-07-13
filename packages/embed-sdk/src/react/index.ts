// React bindings for Nannos: a provider that owns ONE shared core (creation,
// connect, navigate/highlight wiring) and a hook that binds a form in one call.
// Framework-free logic stays in `../core`; this adds only React (already a peer)
// and — deliberately — no react-hook-form dependency (forms are typed structurally).
//
// The provider CONTEXT lives only in this entry. The root entry's <NannosWidget>
// does NOT read it (it takes `core` as a prop) — so nothing depends on the context
// crossing package entry points, and there's no duplicated-context footgun.

import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  createNannos,
  createPkceAuth,
  handleAuthCallback,
  zodFormRegistration,
  type FieldBridge,
  type FormAdapter,
  type NannosAuth,
  type NannosConfig,
  type NannosCore,
  type NannosErrorEvent,
  type NannosStatus,
  type ObjectHandle,
  type PkceAuthConfig,
  type Scope,
  type ZodObjectLike,
} from '../core';

/** PKCE self-login strategy for `<NannosProvider auth={pkce({...})}>`. Thin alias
 *  of `createPkceAuth` so hosts import one thing from `/react`. */
export function pkce(config: PkceAuthConfig): NannosAuth {
  return createPkceAuth(config);
}

/**
 * Mount at your PKCE redirect route (the `redirectUri` you registered). It runs
 * the SDK-owned callback logic — postMessage the code back to the opener and
 * close the popup — so you don't hand-write callback JS. Renders nothing (or your
 * `children`, e.g. a "Signing you in…" splash the popup shows briefly).
 *
 *   <Route path="/nannos-auth-callback" element={<NannosAuthCallback />} />
 */
export function NannosAuthCallback({
  targetOrigin,
  children,
}: {
  targetOrigin?: string;
  children?: ReactNode;
}): ReactNode {
  useEffect(() => {
    handleAuthCallback({ targetOrigin });
  }, [targetOrigin]);
  return children ?? null;
}

// ---------------------------------------------------------------------------
// Provider + context — the single shared core
// ---------------------------------------------------------------------------

// `undefined` = no provider above; `null` = provider present but disabled.
const NannosContext = createContext<NannosCore | null | undefined>(undefined);

export interface NannosProviderProps {
  children: ReactNode;
  /** Build + own a core from config. Read ONCE on first render (config is static). */
  config?: NannosConfig;
  /** …or bring your own already-created core instead of `config`. */
  core?: NannosCore;
  /** Self-login strategy (e.g. `pkce({ issuer, clientId })`) — merged into the
   *  config's `auth`. The generic fallback when the host can't hand over a token
   *  via `config.getToken`. Ignored if a `core` is supplied. */
  auth?: NannosAuth;
  /** When false, the provider yields a null core and all hooks no-op (no discovery
   *  fetch, no popup). */
  enabled?: boolean;
  /** Host navigation for `navigate` client-actions (e.g. router.push). */
  navigate?: (to: string) => void;
  /** Host highlight for `highlight` client-actions (scroll-into-view / outline). */
  highlight?: (target: { type: string; id: string }, field?: string) => void;
  /** Forward SDK-internal failures (connection / init / auth / apply) to host
   *  monitoring (Sentry etc.). Diagnostics only — the SDK still degrades gracefully. */
  onError?: (e: NannosErrorEvent) => void;
}

/**
 * Owns a single Nannos core for the subtree: creates it (from `config`, or uses a
 * provided `core`), connects on mount and disconnects on unmount, and wires
 * navigate/highlight. Every `useNannosZodForm`/`useNannos` below shares it, so the
 * agent's directives resolve against the objects those hooks registered — no
 * hand-rolled singleton, no "second core → empty registry" footgun.
 */
export function NannosProvider(props: NannosProviderProps): ReactNode {
  const { children, config, core, auth, enabled = true, navigate, highlight, onError } = props;

  // The core is created ONCE, the first time we're enabled — `config`/`auth` are
  // captured at that moment and NOT re-read afterward (change them by remounting
  // the provider with a `key`). But `enabled` IS reactive: it gates whether the
  // core is exposed, so a runtime flag (LaunchDarkly etc.) can flip the assistant
  // on/off. While disabled the core is null — no discovery fetch, no popup.
  const coreRef = useRef<NannosCore | null>(null);
  if (enabled && !coreRef.current) {
    coreRef.current = core ?? (config ? createNannos(auth ? { ...config, auth } : config) : null);
  }
  const resolved = enabled ? coreRef.current : null;

  // Connect on mount; disconnect on unmount. Mark the core provider-managed so the
  // mounted widget reuses THIS transport (single authenticated connection) rather
  // than spinning up its own — see SocketProvider.
  useEffect(() => {
    if (!resolved) return;
    resolved.transportManagedExternally = true;
    void resolved.connect();
    return () => resolved.transport.disconnect();
  }, [resolved]);

  // Route navigate/highlight directives to the host's hooks.
  useEffect(() => {
    if (!resolved || (!navigate && !highlight)) return;
    return resolved.bindClientActions({ navigate, highlight });
  }, [resolved, navigate, highlight]);

  // Forward SDK-internal errors to the host's monitoring.
  useEffect(() => {
    if (!resolved || !onError) return;
    return resolved.onError(onError);
  }, [resolved, onError]);

  return createElement(NannosContext.Provider, { value: resolved }, children);
}

/** The shared core (null when disabled). Throws if used outside <NannosProvider>.
 *  Pass the result to <NannosWidget core={...}/> to render the chat. */
export function useNannos(): NannosCore | null {
  const core = useContext(NannosContext);
  if (core === undefined) throw new Error('useNannos must be used within <NannosProvider>.');
  return core;
}

/**
 * Coarse connection status for host-rendered chrome (a badge, an offline notice,
 * a "sign in" button). Separates `unauthenticated` (call `login()`) from
 * `disconnected` (network). Returns `'disconnected'` when the provider is
 * disabled. Must be used within <NannosProvider>.
 */
export function useNannosStatus(): NannosStatus {
  const core = useContext(NannosContext);
  if (core === undefined) throw new Error('useNannosStatus must be used within <NannosProvider>.');
  const [status, setStatus] = useState<NannosStatus>(core?.status ?? 'disconnected');
  useEffect(() => {
    if (!core) return;
    return core.onStatusChange(setStatus);
  }, [core]);
  return status;
}

// ---------------------------------------------------------------------------
// Form binding
// ---------------------------------------------------------------------------

/** The slice of a form we touch. react-hook-form's `UseFormReturn` satisfies it;
 *  so does anything with these two methods. `any` on names/values keeps it
 *  assignable from strongly-typed form libs without variance friction. */
export interface FormLike {
  getValues: (name?: any) => any;
  setValue: (name: any, value: any, options?: any) => void;
}

export interface UseNannosZodFormOptions<TState> {
  /** The host form (react-hook-form `UseFormReturn`, or any getValues/setValue pair). */
  form: FormLike;
  type: string;
  id: string;
  scope: Scope;
  /** Zod object schema — drives fields, validation, and derived field specs. */
  schema: ZodObjectLike;
  /** Bridges for fields with no 1:1 form key (e.g. dates ↔ a tuple). */
  overrides?: Record<string, FieldBridge>;
  includeValues?: boolean;
  label?: string;
  /** setValue options (default: dirty + validate + touch, so it behaves as if typed). */
  setValueOptions?: unknown;
  /** Override the core from context (rarely needed — <NannosProvider> supplies it). */
  core?: NannosCore | null;
}

const DEFAULT_SET_OPTIONS = { shouldDirty: true, shouldValidate: true, shouldTouch: true };

/**
 * Register a host form as a Nannos ontology object for the component's lifetime.
 * The core comes from <NannosProvider> unless you pass `core` explicitly. Writes
 * go through the form's own `setValue` (dirty/validate/touch by default) so the
 * user still reviews and saves. Re-registers if the identity changes; disposes on
 * unmount.
 *
 *   useNannosZodForm({ form, type: 'Invoice', id, scope, schema, overrides });
 *
 * `schema`/`overrides` are read at registration. Re-registration is triggered by a
 * SHAPE signature (schema field names + bridge keys), so adding/removing a field or
 * a bridge takes effect even if you build them inline. It does NOT deep-compare
 * VALUES — changing a bridge's `read`/`write` body while keeping the same keys won't
 * re-register; keep bridge bodies stable (module constant) or change a key to force it.
 */
export function useNannosZodForm<TState = Record<string, unknown>>(
  options: UseNannosZodFormOptions<TState>,
): void {
  const { form, type, id, scope, schema, overrides, includeValues, label, setValueOptions } = options;
  const ctxCore = useContext(NannosContext);
  const core = options.core ?? (ctxCore === undefined ? null : ctxCore);

  // Shape signature: catches field/bridge add/remove (the natural inline-build
  // footgun) without re-registering every render on a fresh object identity.
  const shapeSig =
    Object.keys(schema.shape).join(',') + '|' + Object.keys(overrides ?? {}).join(',');

  useEffect(() => {
    if (!core) return;

    const adapter: FormAdapter = {
      get: (field) => form.getValues(field),
      set: (field, value) => form.setValue(field, value, setValueOptions ?? DEFAULT_SET_OPTIONS),
      snapshot: () => form.getValues() as Record<string, unknown>,
    };

    const handle: ObjectHandle = core.register(
      zodFormRegistration<TState>({ type, id, scope, schema, adapter, overrides, includeValues, label }),
    );
    return () => handle.dispose();
    // Re-register on identifying inputs + the schema/override SHAPE (shapeSig).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [core, form, type, id, scope, includeValues, label, shapeSig]);
}
