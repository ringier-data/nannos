import { TransportClient } from './client';
import { executeClientAction, extractClientActionDirective, type ClientActionDeps } from './client-action';
import { ObjectRegistry } from './registry';
import type { NannosAuth, NannosConfig, NannosErrorEvent, NannosStatus, ObjectHandle, RegisterInput } from './types';

export * from './types';
export * from './schemas';
export * from './extensions';
export * from './protocol';
export * from './wire';
export { TransportClient, type TransportState, type IoFactory } from './client';
import type { IoFactory } from './client';
export { ObjectRegistry } from './registry';
export { executeClientAction, extractClientActionDirective } from './client-action';
export { createPkceAuth, handleAuthCallback, type PkceAuth, type PkceAuthConfig } from './auth';
export {
  zodFormRegistration,
  zodToFieldSpecs,
  jsonSchemaToFieldSpecs,
  type FormAdapter,
  type FieldBridge,
  type ZodObjectLike,
  type ZodFormRegistrationInput,
} from './zod-form';

/**
 * The headless core. Framework-free: a host can use this alone and render its
 * own UI, or feed it to the React UI kit (`mount`, see package root entry).
 */
export class NannosCore {
  readonly registry = new ObjectRegistry();
  readonly transport: TransportClient;
  private readonly promptListeners = new Set<(text: string, silent?: boolean) => void>();
  private bufferedPrompt: { text: string; silent?: boolean } | null = null;
  private opened = false;
  private readonly openListeners = new Set<(open: boolean) => void>();
  private agentUrlPromise: Promise<string | null> | null = null;
  private subAgentNamePromise: Promise<string | null> | null = null;

  /** Self-login strategy (PKCE), if the host chose the `auth` path. */
  readonly auth: NannosAuth | null;
  /** Set true by <NannosProvider> when it owns this core's connection lifecycle
   *  (connect on mount, reauth on login). Only then does the mounted widget's
   *  SocketProvider REUSE `this.transport` instead of creating its own — so a bare
   *  core (e.g. the console via HostAdapterProvider, no NannosProvider) keeps the
   *  original per-provider-transport behavior untouched. */
  transportManagedExternally = false;
  private connectAttempted = false;
  private authErrored = false;
  private lastStatus: NannosStatus = 'disconnected';
  private readonly statusListeners = new Set<(s: NannosStatus) => void>();
  private readonly errorListeners = new Set<(e: NannosErrorEvent) => void>();

  constructor(readonly config: NannosConfig, ioFactory?: IoFactory) {
    // Auth resolution: `getToken` (host-token) and `auth` (self-login) are
    // mutually exclusive. If both are given, the host-token path wins (it's the
    // recommended zero-login path) and `auth` is ignored with a warning.
    let effectiveConfig = config;
    this.auth = config.auth ?? null;
    if (config.getToken && config.auth) {
      console.warn('[nannos] both `getToken` and `auth` set — using `getToken` (host-token path); ignoring `auth`.');
      this.auth = null;
    } else if (config.auth && !config.getToken) {
      const auth = config.auth;
      // Silent token for connect-on-mount: cache/refresh/null — NEVER login().
      // Empty string when null so the socket connects into an `unauthenticated`
      // state (distinguishable) rather than throwing.
      effectiveConfig = { ...config, getToken: async () => (await auth.getAccessToken()) ?? '' };
    }
    this.transport = ioFactory
      ? new TransportClient(effectiveConfig, ioFactory)
      : new TransportClient(effectiveConfig);
    // Re-derive status whenever the transport connection state changes.
    this.transport.subscribe(() => this.emitStatus());
    // Forward transport-level errors (connection / init / auth-token) to host `onError`.
    this.transport.onError((e) => this.emitError(e));
  }

  async connect() {
    this.connectAttempted = true;
    this.emitStatus();
    await this.transport.connect();
  }

  // --- Connection status ------------------------------------------------
  // A coarse, host-renderable status that separates `unauthenticated` (login
  // needed) from `disconnected` (network) — the opaque merge of the two was the
  // biggest debugging cost in the first real integration.

  /** Current coarse status. See `NannosStatus`. */
  get status(): NannosStatus {
    return this.computeStatus();
  }

  private computeStatus(): NannosStatus {
    if (this.authErrored) return 'authError';
    const s = this.transport.getState();
    if (s.initialized) return 'connected';
    // Self-login and no silently-usable token → the fix is login(), not a retry.
    if (this.auth && !this.auth.isAuthenticated()) return 'unauthenticated';
    if (this.connectAttempted || s.socketConnected) return 'connecting';
    return 'disconnected';
  }

  private emitStatus() {
    const next = this.computeStatus();
    if (next === this.lastStatus) return;
    this.lastStatus = next;
    for (const l of this.statusListeners) l(next);
  }

  /** Subscribe to status changes; fires immediately with the current status. */
  onStatusChange(cb: (s: NannosStatus) => void): () => void {
    this.statusListeners.add(cb);
    cb(this.computeStatus());
    return () => this.statusListeners.delete(cb);
  }

  /** Subscribe to SDK-internal errors (connection / init / auth / apply) so a host
   *  can forward them to its own monitoring (Sentry). Returns an unsubscribe fn.
   *  These are diagnostics — the SDK still degrades gracefully. */
  onError(cb: (e: NannosErrorEvent) => void): () => void {
    this.errorListeners.add(cb);
    return () => this.errorListeners.delete(cb);
  }

  private emitError(e: NannosErrorEvent) {
    for (const l of this.errorListeners) l(e);
  }

  /** True when a self-login strategy is set but not yet authenticated — i.e. a
   *  `login()` (from a user gesture) is required. The widget launcher checks this. */
  needsLogin(): boolean {
    return !!this.auth && !this.auth.isAuthenticated();
  }

  /**
   * Run the interactive login. MUST be called synchronously inside a user gesture
   * (it opens a popup). On success, re-auths the socket so the fresh token is
   * presented; on failure, flips status to `authError`. No-op (resolves null) if
   * there's no `auth` strategy.
   */
  async login(): Promise<string | null> {
    if (!this.auth) return null;
    this.authErrored = false;
    try {
      const token = await this.auth.login();
      // Present the freshly-minted token. A socket almost always ALREADY EXISTS
      // from the silent connect-on-mount — it was created but the server rejected
      // it for lack of a token, so `socketConnected` is false yet `this.socket` is
      // set. Gating on `socketConnected` here made connect() a no-op (socket
      // exists) and the token never got presented → stuck "Disconnected" until a
      // hard refresh. reauth() cycles the existing socket so the auth callback
      // re-runs with the token; connect() covers the rare no-socket case.
      this.transport.reauth();
      await this.connect();
      this.emitStatus();
      return token;
    } catch (e) {
      this.authErrored = true;
      this.emitStatus();
      this.emitError({ type: 'auth', message: 'interactive login failed', cause: e });
      throw e;
    }
  }

  /** Drop the token, disconnect, and return to `unauthenticated`/`disconnected`. */
  logout() {
    this.auth?.logout();
    this.authErrored = false;
    this.connectAttempted = false;
    this.transport.disconnect();
    this.emitStatus();
  }

  // --- Imperative panel control -----------------------------------------
  // So a host's OWN launcher (a button next to a form, a menu item) can open
  // the widget and optionally inject a prompt — no window CustomEvent bus. The
  // widget mirrors this state via `onOpenChange`; its built-in launcher calls
  // `toggle()`. Single source of truth = the core, so every trigger agrees.

  /** Is the widget panel currently open. */
  get isOpen(): boolean {
    return this.opened;
  }

  private setOpen(next: boolean) {
    if (this.opened === next) return;
    this.opened = next;
    for (const l of this.openListeners) l(next);
  }

  /** Open the panel. With `prompt`, also injects it (see `sendPrompt`) so a
   *  custom trigger can "open with this question" in one call. */
  open(prompt?: string, opts?: { silent?: boolean }) {
    this.setOpen(true);
    if (prompt !== undefined) this.sendPrompt(prompt, opts);
  }

  /** Close the panel. */
  close() {
    this.setOpen(false);
  }

  /** Flip open/closed (what the widget's built-in launcher calls). */
  toggle() {
    this.setOpen(!this.opened);
  }

  /** Subscribe to open/closed changes; fires immediately with the current state.
   *  Returns an unsubscribe fn. The widget uses this to mirror core state. */
  onOpenChange(cb: (open: boolean) => void): () => void {
    this.openListeners.add(cb);
    cb(this.opened);
    return () => this.openListeners.delete(cb);
  }

  /**
   * Resolve the orchestrator (agent) URL from `backendUrl` — the embedder only
   * knows the console-backend origin, not the agent URL the `initialize_client`
   * handshake needs. Fetches `{backendUrl}/api/v1/config` → `orchestratorUrl`
   * (what console-frontend does internally). Cached; returns null on failure or
   * when same-origin (no `backendUrl`), in which case the host's `defaults` win.
   */
  resolveAgentUrl(fetcher: (path: string) => Promise<Response>): Promise<string | null> {
    if (!this.config.backendUrl) return Promise.resolve(null);
    if (!this.agentUrlPromise) {
      this.agentUrlPromise = fetcher('/api/v1/config')
        .then((r) => (r.ok ? (r.json() as Promise<{ orchestratorUrl?: string }>) : null))
        .then((cfg) => cfg?.orchestratorUrl ?? null)
        .catch(() => null);
    }
    return this.agentUrlPromise;
  }

  /**
   * Resolve the display name of the scoped sub-agent this embed runs (`subAgentId`)
   * from `{backendUrl}/api/v1/sub-agents/{id}` → `name`. In execute-only mode the
   * A2A handshake returns the ORCHESTRATOR's card ("Orchestrator Agent"), which
   * mislabels the widget — the header should reflect the sub-agent actually
   * running. Cached; null when there's no `subAgentId` or the lookup fails (the
   * caller then falls back to the handshake's agent name).
   */
  resolveSubAgentName(fetcher: (path: string) => Promise<Response>): Promise<string | null> {
    if (this.config.subAgentId === undefined) return Promise.resolve(null);
    if (!this.subAgentNamePromise) {
      this.subAgentNamePromise = fetcher(`/api/v1/sub-agents/${this.config.subAgentId}`)
        .then((r) => (r.ok ? (r.json() as Promise<{ name?: string }>) : null))
        .then((sa) => sa?.name ?? null)
        .catch(() => null);
    }
    return this.subAgentNamePromise;
  }

  /**
   * Inject a user prompt into the widget programmatically (e.g. a suggested
   * query the host offers next to a form). The widget sends it once connected.
   * Buffers the prompt if the widget hasn't mounted/subscribed yet (first open),
   * so a click that also opens the widget doesn't lose the prompt.
   */
  sendPrompt(text: string, opts?: { silent?: boolean }) {
    const silent = opts?.silent;
    if (this.promptListeners.size > 0) {
      for (const l of this.promptListeners) l(text, silent);
    } else {
      this.bufferedPrompt = { text, silent };
    }
  }

  /** Subscribe to injected prompts (the widget's ChatContext). Drains any buffered
   *  prompt. `silent` = auto-instrumentation: send context for the agent to act on
   *  without rendering a user bubble. */
  onPrompt(cb: (text: string, silent?: boolean) => void): () => void {
    this.promptListeners.add(cb);
    if (this.bufferedPrompt !== null) {
      const b = this.bufferedPrompt;
      this.bufferedPrompt = null;
      cb(b.text, b.silent);
    }
    return () => this.promptListeners.delete(cb);
  }

  register<TState>(input: RegisterInput<TState>): ObjectHandle {
    return this.registry.register(input);
  }

  /** Wire the inbound client-action directives to host hooks (confirm/navigate/highlight). */
  bindClientActions(deps: Omit<ClientActionDeps, 'registry'>) {
    this.clientActionBindings++;
    const off = this.transport.onAgentResponse((data) => {
      // Directives ride status-update events, nested in a DataPart — unwrap the
      // envelope first (also skips streaming chunks cheaply); the Zod guard inside
      // executeClientAction then validates the directive itself.
      const directive = extractClientActionDirective(data);
      if (directive == null) return;
      void executeClientAction(directive, { registry: this.registry, ...deps }).catch((err) => {
        // An apply/highlight/navigate handler threw — surface it (rejections that
        // don't throw are already reported via onApplyResult).
        this.emitError({ type: 'apply', message: 'client-action handler threw', cause: err });
      });
    });
    return () => {
      this.clientActionBindings--;
      off();
    };
  }

  private clientActionBindings = 0;

  /** True while a `bindClientActions` subscription is live (e.g. <NannosProvider>
   *  with `navigate`/`highlight`). The mounted widget checks this so a directive
   *  executes exactly once — the core-level binding wins over the widget's own
   *  adapter-routing demux. */
  get clientActionsBound(): boolean {
    return this.clientActionBindings > 0;
  }

  manifest() {
    return this.registry.manifest();
  }
}

export function createNannos(cfg: NannosConfig, ioFactory?: IoFactory): NannosCore {
  return new NannosCore(cfg, ioFactory);
}
