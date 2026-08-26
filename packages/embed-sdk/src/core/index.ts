import { TransportClient } from './client';
import {
  executeClientAction,
  extractClientActionDirective,
  type ClientActionDeps,
  type ClientActionResult,
} from './client-action';
import { ClientActionLog } from './client-action-log';
import { ObjectRegistry } from './registry';
import type { NannosAuth, NannosConfig, NannosErrorEvent, NannosStatus, ObjectHandle, RegisterInput } from './types';

export * from './types';
export * from './page-context';
export * from './page-read';
export * from './screen-outline';
export * from './schemas';
export * from './extensions';
export * from './protocol';
export * from './wire';
export { TransportClient, type TransportState, type IoFactory } from './client';
import type { IoFactory } from './client';
export { ObjectRegistry } from './registry';
export {
  directiveFromToolArgs,
  executeClientAction,
  extractClientActionDirective,
} from './client-action';
export {
  ClientActionLog,
  type ClientActionLogEntry,
  type ClientActionOutcome,
  type ClientActionPath,
} from './client-action-log';
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
 * The headless core. Framework-free: connection + protocol + object registry.
 * Panel/UI state (open, seeded prompts) lives in the React provider; the chat
 * engine in `../transport`.
 */
/**
 * LangSmith's EU web app — where a trace deep-link is opened. The console
 * hardcodes this same origin in its own trace links; the backend's
 * `LANGSMITH_ENDPOINT` is the API host, not the UI, so it cannot be reused here.
 */
const LANGSMITH_APP_URL = 'https://eu.smith.langchain.com';

/** The subset of `{backendUrl}/api/v1/config` this SDK reads. */
interface BackendConfig {
  orchestratorUrl?: string;
  langsmith?: { organizationId?: string; projectId?: string };
}

export class NannosCore {
  readonly registry = new ObjectRegistry();
  /** What every directive did — read by the dev inspector, never by the agent. */
  readonly clientActions = new ClientActionLog();
  readonly transport: TransportClient;
  private backendConfigPromise: Promise<BackendConfig | null> | null = null;
  private subAgentNamePromise: Promise<string | null> | null = null;

  /** Self-login strategy (PKCE), if the host chose the `auth` path. */
  readonly auth: NannosAuth | null;
  private connectAttempted = false;
  private authErrored = false;
  private lastStatus: NannosStatus = 'disconnected';
  private readonly statusListeners = new Set<(s: NannosStatus) => void>();
  private readonly errorListeners = new Set<(e: NannosErrorEvent) => void>();

  /** The RESOLVED connection config. For the `auth` (self-login) path this carries a
   *  `getToken` bridged to `auth.getAccessToken()`, so the socket AND every REST call
   *  (see `adapter.tsx` `backendFetch`, which reads `core.config.getToken`) share one
   *  token source. */
  readonly config: NannosConfig;

  constructor(rawConfig: NannosConfig, ioFactory?: IoFactory) {
    // Auth resolution: `getToken` (host-token) and `auth` (self-login) are
    // mutually exclusive. If both are given, the host-token path wins (it's the
    // recommended zero-login path) and `auth` is ignored with a warning.
    let effectiveConfig = rawConfig;
    this.auth = rawConfig.auth ?? null;
    if (rawConfig.getToken && rawConfig.auth) {
      console.warn('[nannos] both `getToken` and `auth` set — using `getToken` (host-token path); ignoring `auth`.');
      this.auth = null;
    } else if (rawConfig.auth && !rawConfig.getToken) {
      const auth = rawConfig.auth;
      // Silent token for connect-on-mount: cache/refresh/null — NEVER login().
      // Empty string when null so the socket connects into an `unauthenticated`
      // state (distinguishable) rather than throwing.
      effectiveConfig = { ...rawConfig, getToken: async () => (await auth.getAccessToken()) ?? '' };
    }
    // Expose the RESOLVED config (not the raw one). Previously only the transport
    // received `effectiveConfig` while `this.config` kept the raw config, so on the
    // PKCE `auth` path the REST adapter — which reads `core.config.getToken` — sent
    // requests with NO Authorization header → 401 (e.g. GET /api/v1/sub-agents/{id}),
    // leaving the widget stuck "Disconnected" despite a valid token.
    this.config = effectiveConfig;
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

  // NOTE (v2): panel open-state (open/close/toggle/onOpenChange) and injected-
  // prompt buffering (sendPrompt/onPrompt) moved OFF the core into the React
  // provider — the SDK renders in ONE React tree now, so UI state is plain
  // React state. The core is connection + protocol + registry only.

  /**
   * Resolve the orchestrator (agent) URL from `backendUrl` — the embedder only
   * knows the console-backend origin, not the agent URL the `initialize_client`
   * handshake needs. Fetches `{backendUrl}/api/v1/config` → `orchestratorUrl`
   * (what console-frontend does internally). Cached; returns null on failure or
   * when same-origin (no `backendUrl`), in which case the host's `defaults` win.
   */
  resolveAgentUrl(fetcher: (path: string) => Promise<Response>): Promise<string | null> {
    if (!this.config.backendUrl) return Promise.resolve(null);
    return this.fetchBackendConfig(fetcher).then((cfg) => cfg?.orchestratorUrl ?? null);
  }

  /** `{backendUrl}/api/v1/config` — fetched once, shared by the resolvers below. */
  private fetchBackendConfig(fetcher: (path: string) => Promise<Response>): Promise<BackendConfig | null> {
    if (!this.backendConfigPromise) {
      this.backendConfigPromise = fetcher('/api/v1/config')
        .then((r) => (r.ok ? (r.json() as Promise<BackendConfig>) : null))
        .catch(() => null);
    }
    return this.backendConfigPromise;
  }

  /**
   * LangSmith trace URL for a conversation, derived from the console-backend's
   * `langsmith.{organizationId,projectId}` — so any host gets the dev-mode trace
   * link without wiring `adapter.links.trace` itself. Null when the backend has
   * no ids configured (LANGSMITH_ORGANIZATION_ID / LANGSMITH_PROJECT_ID unset),
   * so the caller renders NO link rather than one with empty path segments.
   */
  resolveTraceUrl(
    fetcher: (path: string) => Promise<Response>,
    conversationId: string,
  ): Promise<string | null> {
    return this.fetchBackendConfig(fetcher).then((cfg) => {
      const org = cfg?.langsmith?.organizationId;
      const project = cfg?.langsmith?.projectId;
      if (!org || !project) return null;
      return `${LANGSMITH_APP_URL}/o/${org}/projects/p/${project}/t/${encodeURIComponent(conversationId)}`;
    });
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

  register<TState>(input: RegisterInput<TState>): ObjectHandle {
    return this.registry.register(input);
  }

  /** Wire the inbound client-action directives to host hooks (confirm/navigate/highlight). */
  bindClientActions(deps: Omit<ClientActionDeps, 'registry'>) {
    this.clientActionBindings++;
    this.clientActionDeps = deps;
    const off = this.transport.onAgentResponse((data) => {
      // Directives ride status-update events, nested in a DataPart — unwrap the
      // envelope first (also skips streaming chunks cheaply); the Zod guard inside
      // executeClientAction then validates the directive itself.
      const directive = extractClientActionDirective(data);
      if (directive == null) return;
      // Logged before execution: this path leaves NO trace in the thread, so the
      // dev inspector is the only place a navigate/highlight is ever visible.
      const logged = this.clientActions.start('fire-and-forget', directive, this.registry.keys());
      void executeClientAction(directive, { registry: this.registry, ...deps })
        .then((result) => this.clientActions.settle(logged, result))
        .catch((err) => {
          this.clientActions.fail(logged, err);
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
  private clientActionDeps: Omit<ClientActionDeps, 'registry'> | null = null;

  /** True while a `bindClientActions` subscription is live (e.g. <NannosProvider>
   *  with `navigate`/`highlight`). The mounted widget checks this so a directive
   *  executes exactly once — the core-level binding wins over the widget's own
   *  adapter-routing demux. */
  get clientActionsBound(): boolean {
    return this.clientActionBindings > 0;
  }

  /**
   * Execute one directive imperatively against the registry + the host hooks
   * last bound via `bindClientActions` — the ROUND-TRIP path: the chat layer
   * runs an awaited `client_action` request through this and resumes the turn
   * with the returned result. Never throws (a thrown handler becomes an
   * `{ok:false}` result the agent can read).
   */
  async runClientAction(directive: unknown): Promise<ClientActionResult> {
    const logged = this.clientActions.start('round-trip', directive, this.registry.keys());
    try {
      const result = await executeClientAction(directive, {
        registry: this.registry,
        ...(this.clientActionDeps ?? {}),
      });
      this.clientActions.settle(logged, result);
      return result;
    } catch (err) {
      this.clientActions.fail(logged, err);
      this.emitError({ type: 'apply', message: 'client-action handler threw', cause: err });
      return { ok: false, reason: 'invalid' };
    }
  }

  manifest() {
    return this.registry.manifest();
  }
}

export function createNannos(cfg: NannosConfig, ioFactory?: IoFactory): NannosCore {
  return new NannosCore(cfg, ioFactory);
}
