// Headless-core types. No React, no DOM-framework assumptions.

/** Ontology scopes shared across both integration surfaces (server MCP + client SDK). */
export type Scope = 'create' | 'update' | 'explain' | string;

/**
 * A self-login auth strategy the core can own (e.g. `createPkceAuth`). Split so
 * the two responsibilities never blur: `getAccessToken` is SILENT (cache /
 * refresh / null — safe to call on connect-on-mount, never opens a popup) while
 * `login` is INTERACTIVE (opens a popup and therefore MUST be invoked inside a
 * user gesture). Mutually exclusive with `NannosConfig.getToken` (host-token path).
 */
export interface NannosAuth {
  /** Cached valid token, else silent refresh, else null. NEVER interactive. */
  getAccessToken(): Promise<string | null>;
  /** Interactive login (opens a popup). CALL INSIDE A USER GESTURE. */
  login(): Promise<string>;
  /** True if a valid token or a refresh token (silent-refreshable) is held. */
  isAuthenticated(): boolean;
  /** Drop the token and return to unauthenticated. */
  logout(): void;
}

/**
 * Coarse connection status a host can render. Crucially separates
 * `unauthenticated` (no token / login needed — the fix is `login()`, not a retry)
 * from `disconnected` (network/handshake) — an opaque merge of the two was the
 * biggest debugging cost in the first real integration.
 */
export type NannosStatus =
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'unauthenticated'
  | 'authError';

/** Category of an SDK-internal failure surfaced via `onError`. */
export type NannosErrorType =
  | 'connection' // socket transport error (connect_error)
  | 'init' // initialize_client handshake failed / timed out
  | 'auth' // token fetch or interactive login failed
  | 'apply'; // a client-action apply threw

/**
 * An SDK-internal failure, forwarded to a host `onError` hook so it can pipe them
 * into its own monitoring (Sentry etc.). These are diagnostics — the SDK already
 * degrades gracefully (status flips, retries) — not exceptions the host must handle.
 */
export interface NannosErrorEvent {
  type: NannosErrorType;
  message: string;
  /** The underlying error/cause, if any (an Error, a socket error object, …). */
  cause?: unknown;
  /** Extra structured context (e.g. `{ target }` for apply, `{ timeoutMs }` for init). */
  detail?: Record<string, unknown>;
}

/**
 * A live handle the host registers for an on-screen ontology object. The `apply`
 * callback MUST write through the host's own form layer (e.g. react-hook-form's
 * `reset`/`setValue`) so validation, dirty-tracking and auto-save fire and the
 * human still submits — Nannos never persists directly for in-form scopes.
 */
/**
 * Compact typed descriptor for one settable field, surfaced to the agent so it
 * targets the right key with a valid value (name + type + enum + a short hint) —
 * without shipping the whole schema/state. Derivable from a Zod/JSON schema by
 * the host (e.g. `z.toJSONSchema`).
 */
export interface FieldSpec {
  name: string;
  /** JSON-schema-ish type: 'string' | 'number' | 'integer' | 'boolean' | 'enum' | 'array' | 'object'. */
  type?: string;
  /** Allowed values for enum/select fields. */
  enum?: string[];
  /** Short agent-facing description (units, format, semantics). */
  description?: string;
}

export interface RegisterInput<TState = unknown> {
  type: string;
  id: string;
  scope: Scope;
  /** Zod schema (or any JSON-schema-ish descriptor) of the object's fields. Pulled on demand. */
  schema?: unknown;
  /** Current state of the object, read on demand (progressive disclosure). */
  getState: () => TState;
  /** Apply agent-produced values through the host's form layer. May return an
   *  `ApplyResult` so callers can surface per-field rejections (a value that
   *  failed validation is skipped, not silently swallowed). Async handles are
   *  awaited before the result is read. */
  apply: (values: Partial<TState>) => void | ApplyResult | Promise<void | ApplyResult>;
  /** Optional human-readable label for the per-turn manifest. */
  label?: string;
  /** Optional compact field list included in the manifest (progressive
   *  disclosure: names only — full schema/state stays client-side). */
  fields?: string[];
  /** Optional TYPED field descriptors (name + type + enum + description). When
   *  present the agent gets deterministic keys/enums instead of guessing;
   *  preferred over `fields` when both are given. */
  fieldSpecs?: FieldSpec[];
  /** Include the object's CURRENT values (from getState, restricted to the
   *  declared fields) in the manifest, so the agent sees what's on screen — not
   *  just the field definitions. Opt-in: only enable for non-sensitive state the
   *  host is comfortable sending each turn. */
  includeValues?: boolean;
}

export interface ObjectHandle {
  readonly key: string; // `${type}:${id}`
  dispose: () => void;
}

/** Outcome of an `apply`: which fields landed and which were rejected (e.g. a
 *  value that failed the schema). Lets the widget say "couldn't apply X" and the
 *  agent self-correct, instead of a value silently vanishing. */
export interface ApplyResult {
  applied: string[];
  rejected: Array<{ field: string; reason?: string }>;
}

/** Compact per-turn manifest entry pushed to the agent (NOT full schema/state). */
export interface ManifestEntry {
  type: string;
  id: string;
  scope: Scope;
  label?: string;
  fields?: string[];
  /** Typed field descriptors (see FieldSpec) — surfaced to the agent when present. */
  fieldSpecs?: FieldSpec[];
  /** Current values of the declared fields (only when the object opts in via
   *  includeValues) so the agent sees on-screen state, not just definitions. */
  values?: Record<string, unknown>;
}

export interface NannosConfig {
  /** console-backend origin. Omit for same-origin (console itself; cookies auth). */
  backendUrl?: string;
  /** socket.io path; mirrors console-frontend default (`/api/v1/socket.io`). */
  socketPath?: string;
  /** On-behalf-of: host hands us the end-user's access token (see ADR-0002).
   *  Omit when same-origin cookies carry the session. The RECOMMENDED path when
   *  the host can federate its own session — no second login, no popup, no gesture.
   *  Mutually exclusive with `auth`. */
  getToken?: () => string | Promise<string>;
  /** Self-login strategy the core owns (e.g. `createPkceAuth`) — the generic
   *  fallback when the host can't hand over a token. The core connects silently
   *  via `auth.getAccessToken()` and only calls `auth.login()` from a user gesture
   *  (the widget launcher). Mutually exclusive with `getToken`. */
  auth?: NannosAuth;
  /** Extra headers forwarded in the `initialize_client` payload (console parity). */
  customHeaders?: Record<string, string>;
  /** Handshake timeout; defaults to 15s (console parity). */
  initTimeoutMs?: number;
  /** Embedded Nannos (ADR-0004): the scoped domain sub-agent this integration runs.
   *  When set, every turn is sent with `executeOnlySubAgentId` so the orchestrator
   *  runs THAT sub-agent as the top-level graph (execute-only: client_action +
   *  <client_objects>, no routing turn). The orchestrator validates the id against
   *  the authenticated user's accessible sub-agents — a wrong/inaccessible id fails
   *  closed, so the client declaring it is safe (identity is the hard boundary). */
  subAgentId?: number;
}
